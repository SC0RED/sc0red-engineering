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
- the runner per agent (Patch claude-cli, Scarlett codex);
- MAX_CONCURRENT_RUNS in agent.env (Patch 2, Scarlett 1) agrees with
  boards.yaml's run slots;
- every column in boards.yaml dispatches a stage that has a rule, and no
  column has an agent claim a card into Ready for Development (the owner's
  move) or claim anything out of Plan Review;
- every template exists and opens with an `## Intent` block, and (with
  --shared) every {{system-doc:...}} directive resolves against the
  workspace or the engineering-standards shared/ directory.

Usage:
    check_workspace.py --root <repo root> [--shared <engineering-standards/shared>]
    check_workspace.py --self-test

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
    ("patch", "plan"): {"read", "comment", "submit_plan", "propose_split", "block"},
    ("patch", "build"): {"read", "comment", "open_for_review", "block"},
    ("patch", "code-review"): {"read", "comment", "merged", "block"},
    ("patch", "hotfix"): {"read", "comment", "open_for_review", "block"},
    ("scarlett", "plan-review"): {"read", "post_verdict", "block"},
    ("scarlett", "code-review"): {"read", "comment", "block"},
}

RUNNER = {"patch": "claude-cli", "scarlett": "codex"}
CONCURRENCY = {"patch": 2, "scarlett": 1}
BOARD_CHECK_CRON = "*/2 * * * *"
OWNER_COLUMNS = {"Ready for Development"}
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


def check_template(agent, ws, rule_name, template, shared, findings):
    path = os.path.join(ws, template or "")
    if not template or not os.path.isfile(path):
        findings.append(f"{agent}/{rule_name}: messageTemplate {template!r} does not exist")
        return
    text = open(path, encoding="utf-8").read()
    if not text.lstrip().startswith("## Intent"):
        findings.append(f"{agent}/{rule_name}: {template} does not open with an `## Intent` block")
    if shared is None:
        return
    for doc in SYSTEM_DOC.findall(text):
        if not (os.path.isfile(os.path.join(ws, doc)) or os.path.isfile(os.path.join(shared, doc))):
            findings.append(f"{agent}/{rule_name}: {{{{system-doc:{doc}}}}} resolves neither in the workspace nor in {shared}")


def check_agent(agent, ws, shared, findings):
    config_path = os.path.join(ws, "agency.yaml")
    if not os.path.isfile(config_path):
        findings.append(f"{agent}: {config_path} missing")
        return set()
    config = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    runner = (config.get("runner") or {}).get("type")
    if runner != RUNNER[agent]:
        findings.append(f"{agent}: runner.type is {runner!r}, expected {RUNNER[agent]!r}")
    if not config.get("requireIntent"):
        findings.append(f"{agent}: requireIntent must be true")

    routing = config.get("routing") or {}
    stages = set()
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
            stages.add(stage)
            timeout = rule.get("runTimeoutSecs")
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
            check_template(agent, ws, name, rule.get("messageTemplate"), shared, findings)

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
        check_template(agent, ws, name, rule.get("messageTemplate"), shared, findings)

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
    if env.get("AGENCY_AGENT_ID") != agent:
        findings.append(f"{agent}: agent.env AGENCY_AGENT_ID is {env.get('AGENCY_AGENT_ID')!r}, expected {agent!r}")
    return stages


def check_boards(root, stages_by_agent, findings):
    path = os.path.join(root, "workspaces", "patch", "boards.yaml")
    if not os.path.isfile(path):
        findings.append(f"boards: {path} missing")
        return
    boards = yaml.safe_load(open(path, encoding="utf-8")) or {}
    for agent, expected in CONCURRENCY.items():
        slots = ((boards.get("agents") or {}).get(agent) or {}).get("runSlots")
        if slots != expected:
            findings.append(f"boards: agents.{agent}.runSlots is {slots!r}, expected {expected} (agent.env)")
    for board in boards.get("boards") or []:
        name = board.get("name", "?")
        for column, entries in (board.get("columns") or {}).items():
            for entry in entries if isinstance(entries, list) else [entries]:
                agent, stage = entry.get("agent"), entry.get("stage")
                if stage not in stages_by_agent.get(agent, set()):
                    findings.append(f"boards/{name}/{column}: {agent} has no rule for stage {stage!r}")
                claim = entry.get("claim")
                if claim in OWNER_COLUMNS:
                    findings.append(f"boards/{name}/{column}: claims into {claim}, which only the owner moves to")
                if claim and column == "Plan Review":
                    findings.append(f"boards/{name}/Plan Review: no agent claims a card out of Plan Review")


def run(root, shared):
    findings = []
    stages = {}
    for agent in RUNNER:
        stages[agent] = check_agent(agent, os.path.join(root, "workspaces", agent), shared, findings)
    check_boards(root, stages, findings)
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
    with tempfile.TemporaryDirectory() as scratch:
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
        _write(os.path.join(root, "workspaces", "patch", "agent.env"), "AGENCY_AGENT_ID=patch\nMAX_CONCURRENT_RUNS=4\n")
        if not any("MAX_CONCURRENT_RUNS" in f for f in run(root, None)):
            failures.append("concurrency: MAX_CONCURRENT_RUNS=4 not flagged")
        root = os.path.join(scratch, "owner-column")
        shutil.copytree(base, root)
        boards_path = os.path.join(root, "workspaces", "patch", "boards.yaml")
        boards = yaml.safe_load(open(boards_path, encoding="utf-8"))
        boards["boards"][0]["columns"]["Plan Review"]["claim"] = "Ready for Development"
        _write(boards_path, yaml.safe_dump(boards, sort_keys=False))
        found = run(root, None)
        if not any("only the owner moves to" in f for f in found):
            failures.append(f"owner-column: claim into Ready for Development not flagged: {found}")
        root = os.path.join(scratch, "intent")
        shutil.copytree(base, root)
        _write(os.path.join(root, "workspaces", "scarlett", "templates", "plan-review.md"), "no intent\n")
        if not any("## Intent" in f for f in run(root, None)):
            failures.append("intent: a template without `## Intent` not flagged")
        shared = os.path.join(scratch, "empty-shared")
        os.makedirs(shared)
        if not any("system-doc" in f for f in run(base, shared)):
            failures.append("system-doc: unresolvable directive not flagged")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--shared", help="engineering-standards shared/ directory, to resolve system-doc directives")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
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
