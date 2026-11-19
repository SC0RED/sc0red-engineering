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
- only Scarlett's workspace names scarlett-review, the tool that posts her
  status with her App key, so Patch never runs it;
- one routing.internal rule per stage;
- Code Review dispatches Patch as the author and Scarlett as the reviewer,
  whose status counts only from the sc0red-scarlett App;
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
import os
import re
import shutil
import sys
import tempfile

import yaml

BOARD = "agency_tools.board."
ALWAYS_STOP = "agency_tools.predicate.always_stop"
SWEEP = "agency_tools.board.sweep"

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
# The tool that posts Scarlett's status with her App key. Only her workspace
# may name it.
REVIEW_POSTER = "scarlett-review"
REVIEW_POSTER_OWNER = "scarlett"
SHARED_PREFIX = "shared/"
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
            pinned = ALLOWLIST.get((agent, stage))
            granted = tool_names(rule.get("tools"))
            outside = [t for t in granted if not t.startswith(BOARD)]
            if outside:
                findings.append(f"{agent}/{name}: tools outside {BOARD}*: {', '.join(outside)}")
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
    check_run_token(agent, env, findings)
    check_review_poster(agent, ws, findings)
    return stages


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
    never runs it, so Patch never needs her App key."""
    if agent == REVIEW_POSTER_OWNER:
        return
    for dirpath, _, files in os.walk(ws):
        for file in sorted(files):
            path = os.path.join(dirpath, file)
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            if REVIEW_POSTER in text:
                findings.append(f"{agent}: {os.path.relpath(path, os.path.dirname(ws))} names {REVIEW_POSTER}; "
                                f"only Scarlett posts her status")


def sweep_boards(root):
    """The `board` argument of every board.sweep step in Patch's schedule."""
    config = yaml.safe_load(open(os.path.join(root, "workspaces", "patch", "agency.yaml"), encoding="utf-8")) or {}
    boards = []
    for rule in ((config.get("routing") or {}).get("schedule") or {}).get("rules") or []:
        for step in rule.get("preRun") or []:
            if step.get("tool") == SWEEP:
                boards.append((step.get("name", "?"), (step.get("args") or {}).get("board") or {}))
    return boards


def check_boards(root, stages_by_agent, findings):
    boards = sweep_boards(root)
    if not boards:
        findings.append("boards: no board.sweep step sweeps any board")
    for name, board in boards:
        if not re.fullmatch(r"[A-Za-z0-9-]+/[0-9]+", str(board.get("project", ""))):
            findings.append(f"boards/{name}: project must be 'owner/number', got {board.get('project')!r}")
        if not board.get("repos"):
            findings.append(f"boards/{name}: lists no repos")
        for agent, expected in CONCURRENCY.items():
            slots = (board.get("slots") or {}).get(agent)
            if slots != expected:
                findings.append(f"boards/{name}: slots.{agent} is {slots!r}, expected {expected} (agent.env)")
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


def self_test(repo_root):
    """Each case breaks one promise in a copy of the real workspaces and
    expects a finding naming it; the unmodified copy must be clean."""
    def internal(config, stage):
        return next(r for r in config["routing"]["internal"]["rules"] if stage_of(r) == stage)

    cases = [
        ("missing timeout", "patch", lambda c: internal(c, "build").pop("runTimeoutSecs"), "runTimeoutSecs"),
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
        root = os.path.join(scratch, "owner-column")
        shutil.copytree(base, root)

        def owner_column(config):
            board = config["routing"]["schedule"]["rules"][0]["preRun"][0]["args"]["board"]
            board["stages"]["Ready for Development"] = {"agent": "patch", "task_type": "build"}
        _mutate(root, "patch", owner_column)
        found = run(root, None)
        if not any("owner's move" in f for f in found):
            failures.append(f"owner-column: Ready for Development as a stage column not flagged: {found}")
        root = os.path.join(scratch, "slots")
        shutil.copytree(base, root)
        _mutate(root, "patch", lambda c: c["routing"]["schedule"]["rules"][0]["preRun"][0]["args"]["board"]
                ["slots"].__setitem__("scarlett", 3))
        if not any("slots.scarlett" in f for f in run(root, None)):
            failures.append("slots: a board's run slots disagreeing with agent.env not flagged")
        root = os.path.join(scratch, "stall")
        shutil.copytree(base, root)
        _mutate(root, "patch", lambda c: c["routing"]["schedule"]["rules"][0]["preRun"][0]["args"]["board"]
                ["stages"]["In Development"].__setitem__("stall_minutes", 60))
        if not any("stall_minutes" in f for f in run(root, None)):
            failures.append("stall: a stall window shorter than the run timeout not flagged")
        root = os.path.join(scratch, "reviewer-app")
        shutil.copytree(base, root)
        _mutate(root, "patch", lambda c: c["routing"]["schedule"]["rules"][0]["preRun"][0]["args"]["board"]
                ["stages"]["Code Review"]["reviewer"].__setitem__("status_app", "sc0red-patch"))
        if not any("the reviewer must be" in f for f in run(root, None)):
            failures.append("reviewer-app: a reviewer status from another App not flagged")
        root = os.path.join(scratch, "stage-list")
        shutil.copytree(base, root)
        _mutate(root, "patch", lambda c: c["routing"]["schedule"]["rules"][0]["preRun"][0]["args"]["board"]
                ["stages"].__setitem__("Code Review", [{"agent": "patch", "task_type": "code-review"}]))
        if not any("one mapping" in f for f in run(root, None)):
            failures.append("stage-list: a list of agents for one column not flagged")
        root = os.path.join(scratch, "code-review-stall")
        shutil.copytree(base, root)
        _mutate(root, "patch", lambda c: c["routing"]["schedule"]["rules"][0]["preRun"][0]["args"]["board"]
                ["stages"]["Code Review"].__setitem__("stall_minutes", 140))
        if not any("stall_minutes" in f for f in run(root, None)):
            failures.append("code-review-stall: a stall window shorter than the author's run timeout not flagged")
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
        cited = set()
        for agent in RUNNER:
            check_agent(agent, os.path.join(args.root, "workspaces", agent), cited, [])
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
