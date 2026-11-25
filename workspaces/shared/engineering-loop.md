# The Engineering Loop

How a coding agent turns an approved request into shipped code: four beats —
**Architect → Editor → Verify → Review** — run as a single, full-context line of
work (one agent, start to finish; sub-agents only for read-only review or
search, never parallel editors of the same change). This is the process spine;
`constitution.md` is the judgment that governs it; `anti-patterns.md` is the slop
it exists to prevent.

It is agent-agnostic. Patch runs it with the plan in a Jira comment and Scarlett
as the reviewer; Builder runs it with the plan in the PR body and the
code-review critic as the reviewer; a future system-agent runs it the same way.
The surfaces differ; the beats do not.

---

## Beat 1 — Architect (design before code, §1)

Output: a written design, signed off before any implementation.

1. **Investigate, evidence first (§7).** Establish the facts — logs, data, error
   output, actual behavior — before reading code, and read code with the
   evidence in hand. Diagnosis names symptom → cause → structural deficiency,
   each with `file:line`.
2. **Map the existing seam (§4).** For Agency, agency-tools and winston-agency,
   consult the [capability owners index](capability-owners.md) and follow
   [constitution §4](constitution.md#4--reuse-before-you-build-find-the-existing-seam-first).
   Before designing anything new, find what
   already exists: `grep` for the abstraction/helper/pattern that does this job,
   and for other places doing the same thing. Reuse beats reinvention; reconcile
   divergence rather than adding to it. The repo you are about to touch is the
   first source you read, not the last.
3. **Write the design.** The seam you are changing; the pattern you are applying
   and why it fits (§5), or a plain statement that none does; *Alternatives
   Considered*; the blast radius and what you are deliberately NOT touching; and
   **the existing tests that must stay green** (your regression guard). Keep it
   to the decisions a reviewer needs — silent checks don't get a paragraph.
4. **Gate.** The design goes to a reviewer (human or critic) before code. This is
   "name the seam, get sign-off." Trivial, no-structural-choice changes may skip
   the gate; when in doubt, you are not in the trivial case.

## Beat 2 — Editor (implement against the approved design)

Implement exactly the approved design — no scope creep, no unsolicited refactors
(§6, §10). The discipline that keeps this from drifting into slop is the
**execution feedback loop**, not willpower:

- **Test-first for new behavior, regression-test-first for a bug (§8).** Write the
  failing test, watch it fail, then make it pass. The value is the *evidence* a
  failing test gives you, not the ritual.
- **Lint + run the relevant tests on every edit**, and feed failures straight
  back into the next edit — don't batch it to the end. CI is the last line of
  defense, not the first.
- **Protect the pass-to-pass set.** The existing tests named in the design stay
  green; a change that breaks unrelated tests is not done.
- **Match the house.** Existing patterns, naming, commit style — read recent
  history if unsure. You are writing the code the next agent will mimic (§9).

## Beat 3 — Verify (prove it locally before asking for review)

Run the full local gate before opening anything for review — bare `make` /
the agent's configured `verifyCommand`: format, lint (no warnings), type-check,
tests, and the deterministic quality gates. A failure on code you didn't touch is
still your problem to surface, not to route around. Never bypass a commit-time
gate (`--no-verify`, `--no-gpg-sign`) or weaken a lint to pass. Bounded retries:
if the gate stays red after a small fixed number of honest attempts, stop and
escalate with the failing output — don't grind tokens on pinball.

When and how the verified work becomes a pull request and a deploy is in
[shipping.md](shipping.md): local `make` first, review before the PR, one PR
per meaningful unit of work, and Actions as confirmation only.

The deterministic gates carry the mechanically-checkable parts of the
constitution so the reviewer doesn't have to: changed-line coverage and mutation
score (§8), diff size / one-concern signals (§6), and — where a seam is encoded —
the architecture fitness function that fails when code special-cases around it
(§2/§4). Passing these is necessary, not sufficient.

## Beat 4 — Review (an independent critic, anchored to the design)

A reviewer that did not write the code checks the diff against three anchors, in
order: **the approved design** (did it build what was signed off, or silently
drift?), **the constitution** (§1–§12), and **`anti-patterns.md`**. The six axes:
correctness-vs-plan, design quality (pattern fit, §5), consistency (§2/§4), edge
cases, tests (§8), and promises to people (§12). The verdict is binary — `approve` or `changes_requested` —
and every finding is labeled `[must-fix]` or `[nice-to-have]`, names the section
it implicates, and for a must-fix states the *right shape*, because rejection
without direction wastes the next turn.

The critic reviews against execution and the written plan, never abstract taste;
it blocks only on high-confidence `[must-fix]`s and routes ambiguous calls to a
human. It runs **one round, then hands to a human** — if a second round is needed
after the author addresses it, that itself is the signal a human should decide.
Don't loop twice; don't burn human attention on what a gate could catch; don't
burn infinite tokens on AI ping-pong. The mandatory Astra review in [shipping.md](shipping.md)
is a gate, not this critic: each "do not merge" names a P1 it reproduced or showed from the
code, or an earlier P1 that isn't fixed. It runs at most twice; shipping.md says what happens
after the second "do not merge". Earlier P2s are reported again but never block.

---

### Why four beats and not "just write the code"

Shallow, defensive, duplicate-laden code is what an agent produces when it jumps
straight to editing: no design beat (so no deliberate seam, reflexive guards
instead), no reuse map (so parallel paths), no execution loop (so "almost-right"
logic that never ran). The loop is the structural fix for the slop, and the gates
+ critic are the forcing function that makes it non-optional. An agent that has
internalized the constitution rarely trips them — that's the goal; they are the
backstop.
