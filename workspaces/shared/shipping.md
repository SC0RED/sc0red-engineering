# Shipping: from local work to a deploy

This is how work goes from a branch on the Mac to production. It applies to every repo the fleet touches, and to every agent and session working in one.

The principle: **verification happens locally; a pull request is a deploy.** GitHub Actions confirm work we already know passes. They are not where we find out whether it passes.

## 1. `make` is the CI

- Bare `make` runs every check the repo's Actions run: lint, types, tests, coverage gates, security scans, codegen drift, and stack synth. A repo where it runs less has a defect; the open ones are in [#20](https://github.com/SC0RED/agency-standards/issues/20).
- Workflows only call make targets, and contain no check logic of their own. A check that exists only in workflow YAML is a defect, so move it into make.
- A check workflow should cancel a run its own newer push replaces, and a deploy workflow never should. [#20](https://github.com/SC0RED/agency-standards/issues/20) tracks the repos that don't yet.
- A local run is faster than an Actions run: it has no runner queue, no cold cache and no upload. It also costs nothing, while Actions minutes are a budget.

## 2. Work locally until the work is done

- Commit as often as you like. Commits are cheap and local, and a commit is not a PR. Before review, squash work in progress so each commit left is one concern. Only the head is verified, so `git bisect` can land on a commit that doesn't build.
- While fixing a failure, run only the failing target, for example `make lint`, `cargo test <name>` or `pytest <file>`. Never rerun the full `make` just to find the next failure.
- Run fast gates before slow ones.
- When you think you're done, run **one** full bare `make` on the final head.

## 3. Review happens before the PR

- An independent reviewer reads the branch: the local diff, or a GitHub compare view of a pushed branch that has no PR. The author never reviews their own work.
- **An adversarial Astra review is mandatory**, on the exact commit that will open the PR and on the exact commit that will merge. Run `~/.claude/hooks/astra-verify`. It runs Astra (the model in the gate's `codex-config.toml`) read-only against the base branch, with the gate's own Codex home so no personal Codex instructions reach the review, and records a pass only when the verdict is "merge". Astra is the only reviewer. Give it `--context` with the plan, why the change exists, and anything it must attack outside the repo: hooks, settings, other repos, the rollout. A diff alone hides the plan, which is where the worst findings were.
- A "do not merge" verdict is a finding, never a judgment call: fix it, commit, and run `astra-verify --previous <verdict>` until it says "merge". If you think a finding is wrong, say why in the context and let the next run judge it. If it comes back after that, take it to the lead or the owner rather than run again. This is a gate on reproduced defects, separate from the critic's single round in [engineering-loop.md](engineering-loop.md). Fold every fix into the branch, then run the full `make` once more.
- Review a plan before building it, too. A design the owner is asked to approve goes to Astra first, and its verdict goes with the proposal.
- Pushing a branch without opening a PR is fine where the repo's workflows trigger only on PRs and on deploy branches. Check the repo's workflows before relying on that.

## 4. Open a PR only for a meaningful, finished unit of work

**Open a PR when all three of these are true:**
- the work is a deployable unit of value, such as a feature, a fix or a migration;
- local `make` is clean on the head commit;
- the review has passed.

**A small fix does not earn its own PR.** Commit it to the repo's working branch and let it ship with the next substantial change. Three things ship now, on their own, and the PR says which applies:
- something broken in production;
- a wrong result reaching a real person, or about to: a defect that would charge someone wrongly or expose data on its next run counts;
- an owner ruling, which must not wait inside a larger change. Verify it on live runs after it deploys.

**A gate on Claude sessions enforces this.** Its source, tests and installer are in [gate/](https://github.com/SC0RED/engineering-standards/blob/main/gate/README.md): run `gate/install.sh` from `main` on every machine where Claude Code works on SC0RED repos. `~/.claude/hooks/gh-gate` is installed as `~/.local/bin/gh`, ahead of the real `gh`, so it checks every `gh` a Claude session runs, however the shell got there. A Bash hook, `pr-gate.py`, refuses commands that would step around it: running `gh` by its path, or changing `PATH` or `CLAUDECODE` in a command that runs `gh`. The gate vouches only for github.com: it pins `gh` there and refuses PR operations it gates, and `gh api` writes, on any other host. Reads elsewhere pass. Writes through `gh api` are allowlisted, not screened: comments, replies, reactions, labels, reviews, review requests, issue create and edit, branch deletion, and workflow re-runs and cancels pass. Every other write is refused, including PR creation, merges, assignment, repository dispatches and check runs; use `gh pr create` and `gh pr merge`, and ask the lead to widen the list when a real need comes up. Assigning Copilot is refused on every route, because it opens a PR the gate never sees, and so is `gh agent-task create`. `gh pr revert` is refused too: a revert is a change like any other, so revert on a branch and ship it the same way. `gh pr create` is refused unless, for the commit GitHub will use, `~/.claude/hooks/make-verified` recorded a clean bare `make` and `~/.claude/hooks/astra-verify` recorded a passing review. A repo can have many open PRs; each is built in its own worktree, and merges are what go one at a time (section 5). `gh pr reopen`, `gh pr ready` and `gh issue reopen` on a PR open it again, so they need the same records for its head. New draft PRs are refused, because a draft still runs Actions. Work on hold is parked with `gh pr ready --undo`.

`make-verified` refuses a repo whose default goal isn't `check-all`, `check` or `verify`: set `.DEFAULT_GOAL` on the branch. A goal's name doesn't prove it runs everything Actions run, so keeping them equal is the repo's job and a review item. A record covers tracked files, not ignored config, dependencies or toolchains. The gate is a guardrail against habit, not a security boundary: it doesn't see a person's terminal or two sessions opening PRs at the same moment.

For the three cases above, the `make` and the review still apply: a production fix is when an unreviewed change does the most harm. Urgency never lifts a freeze: a change to frozen paths needs the lead's decision.

**Size:**
- **One concern** ([constitution §6](constitution.md)) applies to each commit. A PR is a deploy and can carry many commits. Steps toward one change are commits on one branch, not a stack of tiny PRs.
- A backlog of already-reviewed changes waiting on the same deploy can ship as **one batch PR**, with one `make` and one deploy, when they are safe to deploy together. Its body lists each change, names any that depend on another, and says how each recovers if it goes wrong.
- A change that moves money or sends messages to people, a data change a code revert can't undo, and an owner ruling don't ride in a batch with unrelated work.
- A batch can exceed a repo's blast-radius budget, which measures the whole branch: state the exception in the repo's gate configuration, as the budget's rule requires. Use a `branch=limit` entry in `BLAST_RADIUS_EXCEPTIONS` where the gate supports one. Where it doesn't yet, raise the limit in that batch, say why in the PR body, and put it back in the next change ([#20](https://github.com/SC0RED/agency-standards/issues/20)).
- Once the batch merges, close each original with "Merged in #N (sha)".

**Every push to an open PR should already be known to pass:**
- Never push to see whether something passes.
- Never run `gh pr update-branch` unless there is a real conflict.
- Never re-run Actions hoping for green.

## 5. Merge means deploy

- **Ready to merge** means:
  - Actions are green on the head commit;
  - 0 unresolved review threads, checked through the GraphQL `reviewThreads` field, not the checks summary;
  - Scarlett's `scarlett/review/<base>` status is green on the head commit. Only `scarlett-review` posts it, as the `sc0red-scarlett` GitHub App, after checking that every required GitHub CI check is green on that exact commit (CI runs bare `make`, so that is the `make` evidence; Scarlett never runs the PR's code) and that an Astra "merge" is recorded for it against that base. The reviewer runs it with the head sha she reviewed, and it refuses if the PR's head has moved since. When GitHub already shows it on the head, `gh pr merge` goes ahead without touching Scarlett's App key; when it doesn't, the gate runs `scarlett-review` itself for that head, which works only for a caller who can read the key and refuses the merge otherwise. That fallback goes once Scarlett runs as an agent. Never post it any other way. The status shows the check ran in a normal merge; it doesn't prove it, for the reason in the paragraph after this list;
  - `make-verified` passed on the PR's head, and `astra-verify` passed on it against the base branch's current commit. A push needs both again; a base that moved since needs a new review, because what lands is the head merged with the current base.
- Merge with `--match-head-commit <full sha>`, so that nothing lands that wasn't verified. The gate refuses `gh pr merge` without that pin, when the pinned head lacks either record, or when Scarlett's `scarlett/review/<base>` success from `sc0red-scarlett` is neither on that exact head already (her newest status for it decides) nor posted by `scarlett-review` at the merge, and refuses `--auto` and `--admin`. It also refuses a PR whose base uses a merge queue, because the queue merges later against whatever the base is then. The gate checks the base when the merge starts; GitHub's merge takes no base precondition, so a merge landing in the seconds between is caught only by the branch rule below, where a repo has it. A batch uses a merge commit, which keeps each change's history. That doesn't make a change safe to revert: a later change can depend on it, and no revert undoes a charge or a sent message, which is why the PR states recovery. A single-concern PR can squash.
- Watch the deploy to completion, including a *failed* run, not just the new sha appearing. Then confirm the product works, not only the health checks.
- Never use `--admin` or `--no-verify`, and never dismiss a security finding, without the lead's decision.

**On a trunk with the ruleset, GitHub enforces the same rules**, so they hold for the web UI and any machine, not only Claude sessions on the Mac. A merge into that trunk needs: each required check green, the branch up to date with the trunk, every review thread resolved, and nobody bypasses it, admins included. Every required check is pinned to the App that must post it: Actions checks to GitHub Actions, and `scarlett/review/<trunk>` to `sc0red-scarlett`. A status of the same name from anyone else doesn't count. Scarlett's is posted only by `scarlett-review` (run by the reviewer, or by the gate at a merge that doesn't find it), after it checks CI and the Astra record, so a PR merged the normal way has green CI on its head and an Astra pass recorded for it against that trunk; the builder's merge also needs the local `make` record. Like the gate, that guards against mistakes, not a deliberate workaround: anything running as the owner can read Scarlett's App key (through the gate's service-account token in the login keychain, or the owner's own unlocked 1Password), as it can write the record files. An agent that must not have that power runs as a user without access to the key. A status belongs to one commit and names one base, so a new push, or a PR into another branch, needs it again. A repo without the ruleset relies on the gate alone, which sees only Claude sessions on the Mac. The gate refuses retargeting a PR (`gh pr edit --base`); close it and open a new one against the other base. A rollback of the gate goes with removing the ruleset's `scarlett/review/<trunk>` requirement, or nothing can merge.

## 6. Order across PRs and repos

- An additive or backward-compatible cross-repo change ships producer first: agency-tools, then Agency, then winston-agency.
- A change that removes or breaks a contract ships in the reverse order: consumers stop depending on it first, then the producer removes it.
- Don't stack PRs. A PR on another PR's branch would have to be retargeted, which the gate refuses; open it against the trunk after the first one merges.
- During a freeze, diff every PR's files against the frozen paths before merging (`gh pr diff N --name-only`). A label is a claim; the diff is the evidence.

## 7. The Mac is shared

This section is for the shared Mac only.

- Heavy checks run one at a time through the lock. A deep queue is something to raise with the lead, never a reason to push early and let Actions run the check.
- Builds, worktrees and scratch work go on `/Volumes/SSD`, never the boot disk. Delete `target/` dirs after a merge.

## Why

- Actions minutes are a budget, and PR-by-PR pushing has used up the org's monthly allowance within days.
- A PR per small step multiplies everything: Actions runs, review rounds, and production deploys of half-finished work.
- Verifying locally first makes the PR's Actions run a confirmation. Each merge then ships something meaningful, once.
