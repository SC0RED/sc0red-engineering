#!/usr/bin/env python3
"""Structural gate for the Patch and Scarlett workspaces.

The Agency loader (`agency validate-workspace`) checks that a workspace boots;
agency-tools' validate_workspace.py checks that every tool reference resolves.
Neither knows what this team's design promises, so this checks that:

- every stage rule is a routing.internal rule with a positive runTimeoutSecs
  (a run that hangs must end as failed, so the board check can restart it);
- every stage rule's tool allowlist is exactly the pinned set for that stage,
  so no stage can make a card move it doesn't own, and nothing outside
  agency_tools.board.* can touch the board;
- no rule anywhere (stage, schedule or preRun) reaches a tool that could move a
  card except through agency_tools.board.*;
- each schedule rule ends its preRun in predicate.always_stop and grants no
  tools, so no LLM run is ever queued for it, and the board check runs
  board.sweep every two minutes;
- every swept stage's stall_minutes outlasts its rule's runTimeoutSecs plus
  queue wait, so the sweep never counts a live run as stalled;
- the runner per agent (Patch claude-cli, Scarlett codex);
- MAX_CONCURRENT_RUNS in agent.env (Patch 2, Scarlett 1) agrees with each
  swept board's run slots;
- each agent's run token (GITHUB_RUN_TOKEN_PERMISSIONS in agent.env) has no
  Projects permission, so only the board tools move cards, Scarlett's has
  no contents write, so she cannot push, and Patch's has no statuses write;
- each agent.env points AGENCY_TOOLS_ROOT, AGENCY_PYTHON_BINARY and
  AGENCY_CONTRACTS_DIR at the release scripts/sync_agency_tools.sh installs,
  so both daemons run the agency-tools and contracts the box runs;
- only Scarlett's code-review stage holds scarlett.post_review (the one tool
  outside agency_tools.board.* any stage holds), and only Scarlett's workspace
  names it or scarlett-review, the hook it runs to post her
  status with her App key, so Patch never runs it;
- one routing.internal rule per stage, each running at most one job per card
  (singleFlightKey '{{ repo }}#{{ issue }}', fail closed, a lock TTL that
  outlasts runTimeoutSecs plus queue wait), and each agent.env names its own
  Redis database in REDIS_URL;
- Code Review dispatches Patch as the author and Scarlett as the reviewer,
  whose status counts only from the sc0red-scarlett App;
- each board.sweep step names a board in Patch's boards.json (the one config
  the sweep and the card tools read), every board there is swept, and a board
  with a Hotfix stage names a production_branch for each repo;
- every column a board sweep dispatches maps to a stage that has a rule, and
  Ready for Development is never a stage column (the owner moves cards there;
  the sweep only claims them out of it);
- every template exists and opens with an `## Intent` block, and every
  {{system-doc:...}} directive resolves; a shared/ one only in the vendored
  workspaces/shared/, which holds exactly the docs the templates cite, and no
  workspace has its own shared/ that the runtime would load instead;
- with --shared, each vendored doc is byte-identical to engineering-standards'
  shared/ (the copy is never edited here).

Usage:
    check_workspace.py --root <repo root> [--shared <engineering-standards/shared>]
    check_workspace.py --self-test
    check_workspace.py --cited-shared

Exit 0 = clean; exit 1 = findings, one per line.
"""

import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile

import yaml

BOARD = "agency_tools.board."
ALWAYS_STOP = "agency_tools.predicate.always_stop"
SWEEP = "agency_tools.board.sweep"
# The board config the sweep and the card tools read, in Patch's workspace.
BOARDS_FILE = "boards.json"
HOTFIX_COLUMN = "Hotfix"

# The pinned allowlist per (agent, stage). Changing a stage's authority means
# changing this table in the same PR, where review sees it.
ALLOWLIST = {
    ("patch", "plan"): {"read", "submit_plan", "propose_split", "block"},
    ("patch", "build"): {"read", "open_for_review", "block"},
    ("patch", "code-review"): {"read", "merged", "block"},
    ("patch", "hotfix"): {"read", "open_for_review", "block"},
    ("scarlett", "plan-review"): {"read", "post_verdict", "block"},
    ("scarlett", "code-review"): {"read", "block"},
}

RUNNER = {"patch": "claude-cli", "scarlett": "codex"}
CONCURRENCY = {"patch": 2, "scarlett": 1}
BOARD_CHECK_CRON = "*/2 * * * *"
# board.sweep's default; a stage's stall window must outlast its run timeout
# plus time queued behind a full slot.
DEFAULT_STALL_MINUTES = 240
QUEUE_WAIT_SECS = 1800
# Every stage rule runs at most one job per card (board.open_for_review's
# registration relies on one writer per card).
SINGLE_FLIGHT_KEY = "{{ repo }}#{{ issue }}"
OWNER_COLUMNS = {"Ready for Development"}
# Code Review: Patch authors, Scarlett reviews; her status counts only from her App.
REVIEWED_COLUMN = "Code Review"
AUTHOR = {"agent": "patch", "task_type": "code-review"}
REVIEWER = {"agent": "scarlett", "task_type": "code-review", "status_app": "sc0red-scarlett",
            "status": "scarlett/review"}
TOKEN_APP = {"patch": "sc0red-patch", "scarlett": "sc0red-scarlett"}
# Neither run token may move a card; Scarlett's may not push.
FORBIDDEN_PERMISSIONS = {
    "patch": {"organization_projects", "repository_projects"},
    "scarlett": {"organization_projects", "repository_projects"},
}
# Scarlett pushes nothing; Patch posts no status, so scarlett/review/<base>
# comes only from Scarlett's own run.
READ_ONLY_PERMISSIONS = {
    "scarlett": {"contents": "Scarlett pushes nothing"},
    "patch": {"statuses": "only Scarlett posts scarlett/review"},
}
# The tool that posts Scarlett's status with her App key (agency-tools'
# scarlett.post_review, which runs the gate's scarlett-review hook). Only her
# workspace may name either.
POST_REVIEW = "agency_tools.scarlett.post_review"
REVIEW_POSTER = "scarlett-review"
REVIEW_POSTERS = (REVIEW_POSTER, "scarlett_post_review", "scarlett.post_review")
REVIEW_POSTER_OWNER = "scarlett"
# The one tool outside agency_tools.board.* a stage may hold, pinned per stage.
NON_BOARD_TOOLS = {("scarlett", "code-review"): {POST_REVIEW}}
# Scarlett runs as the user that can read her App key, so she never executes
# a PR's code: her workspace never names the make recorder.
NEVER_RUNS_PR_CODE = {"scarlett": "make-verified"}
SHARED_PREFIX = "shared/"
# Both daemons run the agency-tools and contracts the box runs, as
# scripts/sync_agency_tools.sh installs them on this Mac (never a dev checkout).
RUNTIME_CURRENT = "/Volumes/SSD/agency-runtime/current"
RUNTIME_ENV = {
    "AGENCY_TOOLS_ROOT": f"{RUNTIME_CURRENT}/agency-tools",
    "AGENCY_PYTHON_BINARY": f"{RUNTIME_CURRENT}/agency-tools/.venv/bin/python",
    "AGENCY_CONTRACTS_DIR": f"{RUNTIME_CURRENT}/contracts",
}
SYSTEM_DOC = re.compile(r"\{\{\s*system-doc:\s*([^}\s]+)\s*\}\}")


def tool_names(refs):
    return [ref.get("module.python", "") if isinstance(ref, dict) else str(ref) for ref in refs or []]


def stage_of(rule):
    condition = rule.get("condition") or {}
    equals = condition.get("equals") or {}
    if equals.get("field") == "taskType":
        return equals.get("value")
    return None


def read_env(path):
    values = {}
    if not os.path.exists(path):
        return values
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def check_template(agent, ws, rule_name, template, cited, findings):
    path = os.path.join(ws, template or "")
    if not template or not os.path.isfile(path):
        findings.append(f"{agent}/{rule_name}: messageTemplate {template!r} does not exist")
        return
    text = open(path, encoding="utf-8").read()
    if not text.lstrip().startswith("## Intent"):
        findings.append(f"{agent}/{rule_name}: {template} does not open with an `## Intent` block")
    # systemDocRoots is [..]: a directive resolves in the workspace, then
    # beside it (workspaces/shared/ for a shared/ path). A shared/ doc must
    # resolve only in the vendored copy: a workspace-local one would load
    # first and skip the drift check (check_vendored rejects it).
    for doc in SYSTEM_DOC.findall(text):
        cited.add(doc)
        if doc.startswith(SHARED_PREFIX):
            if not os.path.isfile(os.path.join(os.path.dirname(ws), doc)):
                findings.append(f"{agent}/{rule_name}: {{{{system-doc:{doc}}}}} is not vendored in workspaces/{doc}")
        elif not (os.path.isfile(os.path.join(ws, doc)) or os.path.isfile(os.path.join(os.path.dirname(ws), doc))):
            findings.append(f"{agent}/{rule_name}: {{{{system-doc:{doc}}}}} resolves neither in the workspace "
                            f"nor in workspaces/{doc}")


def check_agent(agent, ws, cited, findings):
    config_path = os.path.join(ws, "agency.yaml")
    if not os.path.isfile(config_path):
        findings.append(f"{agent}: {config_path} missing")
        return {}
    config = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    runner = (config.get("runner") or {}).get("type")
    if runner != RUNNER[agent]:
        findings.append(f"{agent}: runner.type is {runner!r}, expected {RUNNER[agent]!r}")
    if not config.get("requireIntent"):
        findings.append(f"{agent}: requireIntent must be true")
    if config.get("systemDocRoots") != [".."]:
        findings.append(f"{agent}: systemDocRoots must be ['..'] (engineering-standards' loading contract), "
                        f"got {config.get('systemDocRoots')!r}")

    routing = config.get("routing") or {}
    stages = {}
    for group, body in routing.items():
        if group == "schedule":
            continue
        for rule in (body or {}).get("rules") or []:
            name = rule.get("name", "?")
            if group != "internal":
                findings.append(f"{agent}/{name}: stage rules belong in routing.internal, not routing.{group}")
                continue
            stage = stage_of(rule)
            if stage is None:
                findings.append(f"{agent}/{name}: condition must be equals taskType")
                continue
            timeout = rule.get("runTimeoutSecs")
            if stage in stages:
                findings.append(f"{agent}/{name}: a second rule for stage {stage!r}; one rule per stage")
            stages[stage] = timeout if isinstance(timeout, int) else None
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                findings.append(f"{agent}/{name}: runTimeoutSecs must be a positive integer, got {timeout!r}")
            check_single_flight(agent, name, rule, stages[stage], findings)
            pinned = ALLOWLIST.get((agent, stage))
            granted = tool_names(rule.get("tools"))
            outside = [t for t in granted if not t.startswith(BOARD)]
            extras = NON_BOARD_TOOLS.get((agent, stage), set())
            if set(outside) != extras:
                findings.append(f"{agent}/{name}: tools outside {BOARD}* are {sorted(outside)}; "
                                f"the pinned set is {sorted(extras)}")
            if pinned is None:
                findings.append(f"{agent}/{name}: stage {stage!r} has no pinned allowlist in check_workspace.py")
            else:
                actual = {t[len(BOARD):] for t in granted if t.startswith(BOARD)}
                if actual != pinned or len(granted) != len(set(granted)):
                    findings.append(
                        f"{agent}/{name}: allowlist {sorted(actual)} differs from the pinned {sorted(pinned)}")
            if rule.get("preRun") or rule.get("preRunTools"):
                findings.append(f"{agent}/{name}: stage rules take no preRun")
            check_template(agent, ws, name, rule.get("messageTemplate"), cited, findings)

    for (pinned_agent, pinned_stage) in ALLOWLIST:
        if pinned_agent == agent and pinned_stage not in stages:
            findings.append(f"{agent}: no routing.internal rule for stage {pinned_stage!r}")

    schedule_rules = ((routing.get("schedule") or {}).get("rules")) or []
    for rule in schedule_rules:
        name = rule.get("name", "?")
        steps = rule.get("preRun") or []
        if not steps or steps[-1].get("tool") != ALWAYS_STOP:
            findings.append(f"{agent}/{name}: schedule preRun must end in {ALWAYS_STOP}")
        if rule.get("tools"):
            findings.append(f"{agent}/{name}: schedule rule grants tools to an LLM run that must never happen")
        declared = set(tool_names(rule.get("preRunTools")))
        for step in steps:
            if step.get("tool") not in declared:
                findings.append(f"{agent}/{name}: preRun step {step.get('name')!r} calls an undeclared tool")
        for tool in declared:
            if tool != ALWAYS_STOP and not tool.startswith(BOARD):
                findings.append(f"{agent}/{name}: preRun tool {tool} is outside {BOARD}*")
        check_template(agent, ws, name, rule.get("messageTemplate"), cited, findings)

    if agent == "patch":
        board_check = [r for r in schedule_rules if r.get("name") == "board-check"]
        if not board_check:
            findings.append("patch: no board-check schedule rule")
        else:
            rule = board_check[0]
            if rule.get("cron") != BOARD_CHECK_CRON:
                findings.append(f"patch/board-check: cron is {rule.get('cron')!r}, expected {BOARD_CHECK_CRON!r}")
            if not any(step.get("tool") == SWEEP for step in rule.get("preRun") or []):
                findings.append(f"patch/board-check: preRun never calls {SWEEP}")
    elif schedule_rules:
        findings.append(f"{agent}: has schedule rules; only Patch runs the board check")

    env = read_env(os.path.join(ws, "agent.env"))
    runs = env.get("MAX_CONCURRENT_RUNS")
    if runs != str(CONCURRENCY[agent]):
        findings.append(f"{agent}: agent.env MAX_CONCURRENT_RUNS is {runs!r}, expected {CONCURRENCY[agent]}")
    if not env.get("BOARD_NOTIFY_LOGIN"):
        findings.append(f"{agent}: agent.env has no BOARD_NOTIFY_LOGIN, so a block would mention nobody")
    if env.get("AGENCY_AGENT_ID") != agent:
        findings.append(f"{agent}: agent.env AGENCY_AGENT_ID is {env.get('AGENCY_AGENT_ID')!r}, expected {agent!r}")
    for key, want in RUNTIME_ENV.items():
        if env.get(key) != want:
            findings.append(f"{agent}: agent.env {key} is {env.get(key)!r}, expected {want!r} "
                            "(the release scripts/sync_agency_tools.sh installs)")
    redis = env.get("REDIS_URL") or ""
    if redis_database(redis) is None:
        findings.append(f"{agent}: agent.env REDIS_URL must be redis://127.0.0.1:6379/<db> (db 0-15, no "
                        f"leading zeros), the agent's own database, got {redis!r}; the single-flight "
                        f"locks live there")
    check_run_token(agent, env, findings)
    check_review_poster(agent, ws, findings)
    return stages


#: Both daemons use the Mac's one local Redis, spelled one way, so two
#: agents' URLs name the same database exactly when their db numbers match.
REDIS_URL_FORM = re.compile(r"redis://127\.0\.0\.1:6379/(0|[1-9][0-9]?)")


def redis_database(url):
    """The database number of a canonical ``redis://127.0.0.1:6379/<db>``
    URL (no leading zeros, 0-15), or None for any other spelling: another
    spelling of the same server (``localhost``, ``127.1``, ``/01``) would
    hide two agents sharing one database."""
    match = REDIS_URL_FORM.fullmatch(url or "")
    if not match or int(match.group(1)) > 15:
        return None
    return int(match.group(1))


def check_single_flight(agent, name, rule, timeout, findings):
    """One job per card per stage: a second dispatch for the card coalesces
    behind the live run, the lock outlasts the run and its queue wait, and a
    dispatch the gate cannot check is held, never run beside a live one."""
    key = rule.get("singleFlightKey")
    if key != SINGLE_FLIGHT_KEY:
        findings.append(f"{agent}/{name}: singleFlightKey must be {SINGLE_FLIGHT_KEY!r} (one job per card), "
                        f"got {key!r}")
    if rule.get("singleFlightFailClosed") is not True:
        findings.append(f"{agent}/{name}: singleFlightFailClosed must be true, so a dispatch the gate cannot "
                        f"check is held instead of run beside a live one")
    ttl = rule.get("singleFlightTtlSecs")
    floor = (timeout or 0) + QUEUE_WAIT_SECS
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < floor:
        findings.append(f"{agent}/{name}: singleFlightTtlSecs {ttl!r} must be at least runTimeoutSecs plus "
                        f"{QUEUE_WAIT_SECS}s of queue wait ({floor}), or the lock lapses under a live run")


def check_run_token(agent, env, findings):
    if env.get("GITHUB_RUN_TOKEN_APP") != TOKEN_APP[agent]:
        findings.append(f"{agent}: agent.env GITHUB_RUN_TOKEN_APP is {env.get('GITHUB_RUN_TOKEN_APP')!r}, "
                        f"expected {TOKEN_APP[agent]!r}")
    raw = env.get("GITHUB_RUN_TOKEN_PERMISSIONS")
    if not raw:
        findings.append(f"{agent}: agent.env has no GITHUB_RUN_TOKEN_PERMISSIONS, so the run token's scope is unstated")
        return
    permissions = {}
    for item in raw.split(","):
        name, _, level = item.strip().partition(":")
        if level not in ("read", "write"):
            findings.append(f"{agent}: GITHUB_RUN_TOKEN_PERMISSIONS entry {item.strip()!r} is not name:read|write")
            continue
        permissions[name] = level
    for name in sorted(set(permissions) & FORBIDDEN_PERMISSIONS[agent]):
        findings.append(f"{agent}: run token has {name}; only the board tools may move a card")
    for name, reason in sorted(READ_ONLY_PERMISSIONS.get(agent, {}).items()):
        if permissions.get(name) == "write":
            findings.append(f"{agent}: run token has {name}:write; {reason}")


def check_review_poster(agent, ws, findings):
    """Only Scarlett's workspace names the tool that posts her status: Patch
    never runs it, so Patch never needs her App key. Scarlett's never names
    the make recorder, since running a PR's make would run its code as her."""
    if agent == REVIEW_POSTER_OWNER:
        forbidden, reason = (NEVER_RUNS_PR_CODE[agent],), "Scarlett never runs a PR's code"
    else:
        forbidden, reason = REVIEW_POSTERS, "only Scarlett posts her status"
    for dirpath, _, files in os.walk(ws):
        for file in sorted(files):
            path = os.path.join(dirpath, file)
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            for name in forbidden:
                if name in text:
                    findings.append(
                        f"{agent}: {os.path.relpath(path, os.path.dirname(ws))} names {name}; {reason}")


def board_config(root):
    """Patch's boards.json: each board's name to its config, the one file the
    sweep and the card tools read."""
    path = os.path.join(root, "workspaces", "patch", BOARDS_FILE)
    try:
        boards = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"boards: cannot read workspaces/patch/{BOARDS_FILE}: {exc}"
    if not isinstance(boards, dict) or not all(isinstance(b, dict) for b in boards.values()):
        return None, f"boards: workspaces/patch/{BOARDS_FILE} must map each board's name to its config"
    return boards, None


def sweep_boards(root, findings):
    """Each board.sweep step in Patch's schedule, with the config its `board`
    name resolves to in boards.json."""
    try:
        with open(os.path.join(root, "workspaces", "patch", "agency.yaml"), encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except OSError:
        return []  # check_agent already reported the missing agency.yaml
    boards, error = board_config(root)
    if error:
        findings.append(error)
        return []
    swept = []
    for rule in ((config.get("routing") or {}).get("schedule") or {}).get("rules") or []:
        for step in rule.get("preRun") or []:
            if step.get("tool") != SWEEP:
                continue
            name = (step.get("args") or {}).get("board")
            if not isinstance(name, str) or name not in boards:
                findings.append(f"boards/{step.get('name', '?')}: board must name a board in {BOARDS_FILE} "
                                f"({sorted(boards)}), got {name!r}")
                continue
            swept.append((name, boards[name]))
    for name in sorted(set(boards) - {n for n, _ in swept}):
        findings.append(f"boards/{name}: in {BOARDS_FILE} but no board.sweep step sweeps it")
    return swept


def check_boards(root, stages_by_agent, findings):
    boards = sweep_boards(root, findings)
    if not boards and not any(f.startswith("boards") for f in findings):
        findings.append("boards: no board.sweep step sweeps any board")
    for name, board in boards:
        if HOTFIX_COLUMN in (board.get("stages") or {}):
            for entry in board.get("repos") or []:
                if not isinstance(entry, dict) or not str(entry.get("production_branch") or "").strip():
                    repo = entry.get("repo") if isinstance(entry, dict) else entry
                    findings.append(f"boards/{name}: {repo} names no production_branch, and the board has "
                                    f"a {HOTFIX_COLUMN} stage (its fix must target that branch)")
        if not re.fullmatch(r"[A-Za-z0-9-]+/[0-9]+", str(board.get("project", ""))):
            findings.append(f"boards/{name}: project must be 'owner/number', got {board.get('project')!r}")
        if not board.get("repos"):
            findings.append(f"boards/{name}: lists no repos")
        for agent, expected in CONCURRENCY.items():
            slots = (board.get("slots") or {}).get(agent)
            if slots != expected:
                findings.append(f"boards/{name}: slots.{agent} is {slots!r}, expected {expected} (agent.env)")
        if REVIEWED_COLUMN not in (board.get("stages") or {}):
            findings.append(f"boards/{name}: no {REVIEWED_COLUMN} stage, so no PR on it gets "
                            f"Scarlett's review")
        for column, entries in (board.get("stages") or {}).items():
            if column in OWNER_COLUMNS:
                findings.append(f"boards/{name}: {column} is the owner's move, never a stage column")
            if not isinstance(entries, dict):
                findings.append(f"boards/{name}/{column}: a stage is one mapping (a second agent is its reviewer:), "
                                f"got {type(entries).__name__}")
                continue
            reviewer = entries.get("reviewer")
            if column == REVIEWED_COLUMN:
                author = {k: entries.get(k) for k in AUTHOR}
                if author != AUTHOR:
                    findings.append(f"boards/{name}/{column}: the author must be {AUTHOR}, got {author}")
                if not isinstance(reviewer, dict) or {k: reviewer.get(k, "scarlett/review" if k == "status" else None)
                                                      for k in REVIEWER} != REVIEWER:
                    findings.append(f"boards/{name}/{column}: the reviewer must be {REVIEWER}, got {reviewer!r}")
            elif reviewer is not None:
                findings.append(f"boards/{name}/{column}: only {REVIEWED_COLUMN} has a reviewer")
            runs = [entries] + ([reviewer] if isinstance(reviewer, dict) else [])
            longest = 0
            for run_entry in runs:
                agent, stage = run_entry.get("agent"), run_entry.get("task_type")
                timeouts = stages_by_agent.get(agent, {})
                if stage not in timeouts:
                    findings.append(f"boards/{name}/{column}: {agent} has no rule for stage {stage!r}")
                    continue
                longest = max(longest, timeouts[stage] or 0)
            stall = entries.get("stall_minutes", DEFAULT_STALL_MINUTES)
            if not isinstance(stall, int) or isinstance(stall, bool) or stall * 60 <= longest + QUEUE_WAIT_SECS:
                findings.append(f"boards/{name}/{column}: stall_minutes {stall!r} must exceed the longest "
                                f"runTimeoutSecs {longest} plus {QUEUE_WAIT_SECS}s of queue wait")


def check_vendored(root, cited, upstream, findings):
    """workspaces/shared/ holds exactly the shared/ docs the templates cite,
    each unedited from engineering-standards (checked when upstream is given).
    No workspace holds its own shared/: the runtime would load that copy first,
    and it is not the one compared with engineering-standards."""
    for agent in RUNNER:
        local = os.path.join(root, "workspaces", agent, "shared")
        if os.path.lexists(local):
            findings.append(f"shared: workspaces/{agent}/shared/ shadows the vendored workspaces/shared/ at "
                            f"runtime; remove it")
    vendored_dir = os.path.join(root, "workspaces", "shared")
    vendored = set()
    if os.path.isdir(vendored_dir):
        for dirpath, _, files in os.walk(vendored_dir):
            for file in files:
                vendored.add(os.path.relpath(os.path.join(dirpath, file), os.path.join(root, "workspaces")))
    wanted = {doc for doc in cited if doc.startswith(SHARED_PREFIX)}
    for doc in sorted(vendored - wanted):
        findings.append(f"shared: workspaces/{doc} is vendored but no template cites it")
    if upstream is None:
        return
    for doc in sorted(vendored & wanted):
        source = os.path.join(upstream, doc[len(SHARED_PREFIX):])
        if not os.path.isfile(source):
            findings.append(f"shared: workspaces/{doc} is not in {upstream}")
        elif open(source, "rb").read() != open(os.path.join(root, "workspaces", doc), "rb").read():
            findings.append(f"shared: workspaces/{doc} differs from {source}; run `make vendor-shared`")


def run(root, upstream):
    findings = []
    stages = {}
    cited = set()
    for agent in RUNNER:
        stages[agent] = check_agent(agent, os.path.join(root, "workspaces", agent), cited, findings)
    # Agency's Redis keys (the run-concurrency gate, single-flight locks) are
    # not namespaced by agent: two agents on one database share them.
    redis = {agent: read_env(os.path.join(root, "workspaces", agent, "agent.env")).get("REDIS_URL")
             for agent in RUNNER}
    databases = [redis_database(url) for url in redis.values()]
    if None not in databases and len(set(databases)) != len(databases):
        findings.append(f"agent.env: REDIS_URL must name a different database per agent, got {redis}")
    check_boards(root, stages, findings)
    check_vendored(root, cited, upstream, findings)
    return findings


# --- self-test -------------------------------------------------------------

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _mutate(root, agent, change):
    path = os.path.join(root, "workspaces", agent, "agency.yaml")
    config = yaml.safe_load(open(path, encoding="utf-8"))
    change(config)
    _write(path, yaml.safe_dump(config, sort_keys=False))


def _mutate_boards(root, change):
    path = os.path.join(root, "workspaces", "patch", BOARDS_FILE)
    boards = json.load(open(path, encoding="utf-8"))
    change(boards)
    _write(path, json.dumps(boards, indent=2))


def self_test(repo_root):
    """Each case breaks one promise in a copy of the real workspaces and
    expects a finding naming it; the unmodified copy must be clean."""
    def internal(config, stage):
        return next(r for r in config["routing"]["internal"]["rules"] if stage_of(r) == stage)

    cases = [
        ("missing timeout", "patch", lambda c: internal(c, "build").pop("runTimeoutSecs"), "runTimeoutSecs"),
        ("no single flight", "patch", lambda c: internal(c, "hotfix").pop("singleFlightKey"),
         "singleFlightKey must be"),
        ("single flight per repo", "scarlett", lambda c: internal(c, "code-review").__setitem__(
            "singleFlightKey", "{{ repo }}"), "singleFlightKey must be"),
        ("single flight fails open", "patch", lambda c: internal(c, "build").pop("singleFlightFailClosed"),
         "singleFlightFailClosed must be true"),
        ("single flight lapses", "patch", lambda c: internal(c, "build").__setitem__(
            "singleFlightTtlSecs", 600), "singleFlightTtlSecs 600"),
        ("extra board move", "patch", lambda c: internal(c, "plan")["tools"].append(
            {"module.python": "agency_tools.board.merged"}), "differs from the pinned"),
        ("non-board tool", "scarlett", lambda c: internal(c, "plan-review")["tools"].append(
            {"module.python": "agency_tools.github.projects_move"}), "outside agency_tools.board"),
        ("schedule spawns LLM", "patch", lambda c: c["routing"]["schedule"]["rules"][0]["preRun"].pop(),
         "must end in"),
        ("schedule grants tools", "patch", lambda c: c["routing"]["schedule"]["rules"][0].__setitem__(
            "tools", [{"module.python": "agency_tools.board.block"}]), "grants tools"),
        ("wrong cadence", "patch", lambda c: c["routing"]["schedule"]["rules"][0].__setitem__(
            "cron", "*/30 * * * *"), "cron is"),
        ("wrong runner", "scarlett", lambda c: c["runner"].__setitem__("type", "claude-cli"), "runner.type"),
        ("stage outside internal", "patch", lambda c: c["routing"].__setitem__(
            "github", {"rules": [copy.deepcopy(internal(c, "build"))]}), "belong in routing.internal"),
        ("missing stage", "scarlett", lambda c: c["routing"]["internal"]["rules"].pop(), "no routing.internal rule"),
        ("patch posts review", "patch", lambda c: internal(c, "code-review")["tools"].append(
            {"module.python": POST_REVIEW}), "only Scarlett posts her status"),
        ("post review off code-review", "scarlett", lambda c: internal(c, "plan-review")["tools"].append(
            {"module.python": POST_REVIEW}), "the pinned set is []"),
        ("code-review without post review", "scarlett", lambda c: internal(c, "code-review").__setitem__(
            "tools", [t for t in internal(c, "code-review")["tools"] if t.get("module.python") != POST_REVIEW]),
         "the pinned set is ['agency_tools.scarlett.post_review']"),
    ]
    failures = []
    # Scratch under the repo (gitignored .validate.*/), never the boot disk.
    with tempfile.TemporaryDirectory(prefix=".validate.selftest.", dir=repo_root) as scratch:
        base = os.path.join(scratch, "base")
        shutil.copytree(os.path.join(repo_root, "workspaces"), os.path.join(base, "workspaces"))
        clean = run(base, None)
        if clean:
            failures.append("unmodified workspaces are not clean: " + "; ".join(clean))
        for label, agent, change, expect in cases:
            root = os.path.join(scratch, label.replace(" ", "-"))
            shutil.copytree(base, root)
            _mutate(root, agent, change)
            found = run(root, None)
            if not any(expect in f for f in found):
                failures.append(f"{label}: expected a finding containing {expect!r}, got {found}")
        # File-level cases.
        root = os.path.join(scratch, "concurrency")
        shutil.copytree(base, root)
        _write(os.path.join(root, "workspaces", "patch", "agent.env"),
               "AGENCY_AGENT_ID=patch\nMAX_CONCURRENT_RUNS=4\nBOARD_NOTIFY_LOGIN=x\n")
        if not any("MAX_CONCURRENT_RUNS" in f for f in run(root, None)):
            failures.append("concurrency: MAX_CONCURRENT_RUNS=4 not flagged")
        root = os.path.join(scratch, "dev-tools-root")
        shutil.copytree(base, root)
        path = os.path.join(root, "workspaces", "scarlett", "agent.env")
        env = open(path, encoding="utf-8").read()
        env = re.sub(r"(?m)^AGENCY_TOOLS_ROOT=.*$", "AGENCY_TOOLS_ROOT=/Volumes/SSD/Code/agency-tools", env)
        _write(path, env)
        if not any(f.startswith("scarlett:") and "AGENCY_TOOLS_ROOT" in f for f in run(root, None)):
            failures.append("dev-tools-root: an agent.env AGENCY_TOOLS_ROOT off the synced release not flagged")
        for label, agent, edit, expect in [
            ("no-redis", "patch", lambda text: re.sub(r"(?m)^REDIS_URL=.*\n", "", text), "REDIS_URL must be"),
            # Scarlett on Patch's database, whichever that is.
            ("shared-redis", "scarlett", lambda text: re.sub(
                r"(?m)^REDIS_URL=.*$",
                "REDIS_URL=" + read_env(os.path.join(base, "workspaces", "patch", "agent.env"))["REDIS_URL"],
                text), "a different database per agent"),
            *[(f"redis-spelling-{i}", "scarlett",
               (lambda url: lambda text: re.sub(r"(?m)^REDIS_URL=.*$", f"REDIS_URL={url}", text))(url),
               "REDIS_URL must be")
              for i, url in enumerate(["redis://127.0.0.1:6379/01", "redis://localhost:6379/1",
                                       "redis://127.1:6379/1", "redis://127.0.0.1:65536/2",
                                       "redis://127.0.0.1/2", "redis://127.0.0.1:6379/16"])],
        ]:
            root = os.path.join(scratch, label)
            shutil.copytree(base, root)
            path = os.path.join(root, "workspaces", agent, "agent.env")
            _write(path, edit(open(path, encoding="utf-8").read()))
            found = run(root, None)
            if not any(expect in f for f in found):
                failures.append(f"{label}: expected a finding containing {expect!r}, got {found}")
        def sweep_arg(name):
            return lambda c: c["routing"]["schedule"]["rules"][0]["preRun"][0]["args"].__setitem__("board", name)

        # The board the real schedule sweeps, by whatever name it has.
        swept = sweep_boards(base, [])
        if not swept:
            failures.append("board cases: the unmodified schedule sweeps no board in boards.json")
            swept = [("", {})]
        name = swept[0][0]

        def no_production(boards):
            boards[name]["repos"][0].pop("production_branch")

        board_cases = [
            # (label, change to boards.json (all boards), change to Patch's agency.yaml, expected finding)
            ("owner-column", lambda b: b[name]["stages"].__setitem__(
                "Ready for Development", {"agent": "patch", "task_type": "build"}), None, "owner's move"),
            ("slots", lambda b: b[name]["slots"].__setitem__("scarlett", 3), None, "slots.scarlett"),
            ("stall", lambda b: b[name]["stages"]["In Development"].__setitem__("stall_minutes", 60),
             None, "stall_minutes"),
            ("reviewer-app", lambda b: b[name]["stages"]["Code Review"]["reviewer"].__setitem__(
                "status_app", "sc0red-patch"), None, "the reviewer must be"),
            ("stage-list", lambda b: b[name]["stages"].__setitem__(
                "Code Review", [{"agent": "patch", "task_type": "code-review"}]), None, "one mapping"),
            ("code-review-stall", lambda b: b[name]["stages"]["Code Review"].__setitem__(
                "stall_minutes", 140), None, "stall_minutes"),
            ("no-code-review", lambda b: b[name]["stages"].pop("Code Review"), None,
             "no Code Review stage"),
            ("hotfix-without-production", no_production, None, "names no production_branch"),
            ("unknown-board", None, sweep_arg("winston"), "must name a board in boards.json"),
            ("inline-board", None, sweep_arg({"project": "neurobloom-lilly/2"}),
             "must name a board in boards.json"),
            ("unswept-board", lambda b: b.__setitem__("spare", copy.deepcopy(b[name])), None,
             "no board.sweep step sweeps it"),
        ]
        for label, boards_change, config_change, expect in board_cases:
            root = os.path.join(scratch, label)
            shutil.copytree(base, root)
            if boards_change:
                _mutate_boards(root, boards_change)
            if config_change:
                _mutate(root, "patch", config_change)
            found = run(root, None)
            if not any(expect in f for f in found):
                failures.append(f"{label}: expected a finding containing {expect!r}, got {found}")
        root = os.path.join(scratch, "no-boards-file")
        shutil.copytree(base, root)
        os.remove(os.path.join(root, "workspaces", "patch", BOARDS_FILE))
        if not any("cannot read" in f for f in run(root, None)):
            failures.append("no-boards-file: a missing boards.json not flagged")
        root = os.path.join(scratch, "no-patch-config")
        shutil.copytree(base, root)
        os.remove(os.path.join(root, "workspaces", "patch", "agency.yaml"))
        try:
            found = run(root, None)
        except OSError as exc:
            failures.append(f"no-patch-config: raised {exc!r} instead of reporting it")
        else:
            if not any("patch" in f and "agency.yaml" in f for f in found):
                failures.append(f"no-patch-config: a missing agency.yaml not flagged, got {found}")
        for label, agent, permissions, expect in [
            ("projects-token", "patch", "contents:write,organization_projects:write", "organization_projects"),
            ("scarlett-push", "scarlett", "contents:write,issues:write", "contents:write"),
        ]:
            root = os.path.join(scratch, label)
            shutil.copytree(base, root)
            path = os.path.join(root, "workspaces", agent, "agent.env")
            env = open(path, encoding="utf-8").read()
            env = re.sub(r"(?m)^GITHUB_RUN_TOKEN_PERMISSIONS=.*$", f"GITHUB_RUN_TOKEN_PERMISSIONS={permissions}", env)
            _write(path, env)
            if not any(expect in f for f in run(root, None)):
                failures.append(f"{label}: run token permissions {permissions!r} not flagged")
        root = os.path.join(scratch, "intent")
        shutil.copytree(base, root)
        _write(os.path.join(root, "workspaces", "scarlett", "templates", "plan-review.md"), "no intent\n")
        if not any("## Intent" in f for f in run(root, None)):
            failures.append("intent: a template without `## Intent` not flagged")
        root = os.path.join(scratch, "patch-statuses")
        shutil.copytree(base, root)
        path = os.path.join(root, "workspaces", "patch", "agent.env")
        env = open(path, encoding="utf-8").read().replace("statuses:read", "statuses:write")
        _write(path, env)
        if not any("statuses:write" in f for f in run(root, None)):
            failures.append("patch-statuses: a Patch run token that can post statuses not flagged")
        root = os.path.join(scratch, "patch-poster")
        shutil.copytree(base, root)
        path = os.path.join(root, "workspaces", "patch", "templates", "code-review.md")
        _write(path, open(path, encoding="utf-8").read() + "\nRun ~/.claude/hooks/" + REVIEW_POSTER + ".\n")
        if not any(REVIEW_POSTER in f and f.startswith("patch:") for f in run(root, None)):
            failures.append(f"patch-poster: a Patch template naming {REVIEW_POSTER} not flagged")
        root = os.path.join(scratch, "scarlett-runs-make")
        shutil.copytree(base, root)
        path = os.path.join(root, "workspaces", "scarlett", "templates", "code-review.md")
        _write(path, open(path, encoding="utf-8").read() + "\nRun ~/.claude/hooks/make-verified.\n")
        if not any("make-verified" in f and f.startswith("scarlett:") for f in run(root, None)):
            failures.append("scarlett-runs-make: a Scarlett template running make-verified not flagged")
        root = os.path.join(scratch, "duplicate-stage")
        shutil.copytree(base, root)
        _mutate(root, "patch", lambda c: c["routing"]["internal"]["rules"].append(
            dict(copy.deepcopy(internal(c, "build")), name="build-again")))
        if not any("second rule for stage" in f for f in run(root, None)):
            failures.append("duplicate-stage: two rules for one stage not flagged")
        root = os.path.join(scratch, "local-shadow")
        shutil.copytree(base, root)
        _write(os.path.join(root, "workspaces", "patch", "shared", "constitution.md"), "edited\n")
        if not any("shadows the vendored" in f for f in run(root, None)):
            failures.append("local-shadow: a workspace-local shared doc not flagged")
        root = os.path.join(scratch, "unvendored")
        shutil.copytree(base, root)
        os.remove(os.path.join(root, "workspaces", "shared", "constitution.md"))
        if not any("system-doc:shared/constitution.md" in f for f in run(root, None)):
            failures.append("unvendored: a directive with no vendored doc not flagged")
        root = os.path.join(scratch, "stray")
        shutil.copytree(base, root)
        _write(os.path.join(root, "workspaces", "shared", "jira-write-auth.md"), "stray\n")
        if not any("no template cites" in f for f in run(root, None)):
            failures.append("stray: a vendored doc no template cites not flagged")
        upstream = os.path.join(scratch, "upstream")
        shutil.copytree(os.path.join(base, "workspaces", "shared"), upstream)
        if run(base, upstream):
            failures.append("drift: an unedited vendored copy is flagged: " + "; ".join(run(base, upstream)))
        with open(os.path.join(upstream, "shipping.md"), "a", encoding="utf-8") as handle:
            handle.write("\nupstream moved on\n")
        if not any("differs from" in f for f in run(base, upstream)):
            failures.append("drift: a vendored doc that differs from engineering-standards not flagged")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--shared", help="engineering-standards shared/ directory the vendored copy must match")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--cited-shared", action="store_true",
                        help="print the shared/ docs the rule templates cite, one per line (for make vendor-shared)")
    args = parser.parse_args()
    if args.cited_shared:
        cited, found = set(), []
        for agent in RUNNER:
            check_agent(agent, os.path.join(args.root, "workspaces", agent), cited, found)
        # The list is complete only if every workspace and template was read:
        # any finding but "not vendored yet" (what vendoring fixes) refuses.
        blocking = [f for f in found if "is not vendored in" not in f]
        if blocking:
            for finding in blocking:
                print(finding, file=sys.stderr)
            print("--cited-shared: fix these first; the cited list may be incomplete", file=sys.stderr)
            return 1
        for doc in sorted(d for d in cited if d.startswith(SHARED_PREFIX)):
            print(doc[len(SHARED_PREFIX):])
        return 0
    if args.self_test:
        failures = self_test(args.root)
        for failure in failures:
            print(f"self-test: {failure}")
        if failures:
            return 1
        print("check_workspace self-test: OK")
        return 0
    if args.shared and not os.path.isdir(args.shared):
        print(f"--shared {args.shared} is not a directory")
        return 1
    findings = run(args.root, args.shared)
    for finding in findings:
        print(finding)
    if findings:
        return 1
    print("check_workspace: OK — stages, allowlists, timeouts, board check and concurrency match the design")
    return 0


if __name__ == "__main__":
    sys.exit(main())
