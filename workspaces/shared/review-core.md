# The Review Core

The shared rubric every sc0red code-review critic runs — Scarlett reviewing
Patch's work, the Builder-facing critic reviewing Builder's PRs, and any future
reviewer. It is **plan-source-agnostic**: the plan it checks against may live in
a Jira comment (Patch) or a PR body (Builder); the rubric, the verdict shape, and
the discipline are identical. The adapters differ only in *where they read the
plan* and *where they route the verdict* — never in *how they judge*.

This is the reviewer's half of the constitution. The author writes against
`constitution.md` + `engineering-loop.md`; the reviewer judges against this. A
finding cites the constitution section it implicates, so the vocabulary is shared
on both sides of the desk.

## Inputs

A review takes three things:

1. **The diff** — the change under review.
2. **The plan** — the approved design the change was supposed to implement (the
   Architect-beat artifact: Jira plan comment, or the PR body's plan section).
3. **The standard** — `constitution.md` and `anti-patterns.md`.

A review with no plan to check against is not a review — request the plan first
(emit `failed`/blocked naming the missing plan rather than guessing intent).

## The six axes

Judge the diff on exactly these, in order:

1. **Correctness vs. the plan.** Does the code do what the approved plan said? A
   diff that silently diverges from the plan — adds a `switch` where the plan
   said "extract a Strategy," drops an acceptance criterion — is the first thing
   to catch. (constitution §1)
2. **Design quality.** Pattern fit: a cargo-culted abstraction is a finding; so is
   a missing pattern the code is accumulating accidentally (a growing
   switch-on-type, flags steering behavior). Name the GoF pattern. (constitution
   §5; `anti-patterns.md` Pattern-Blindness / Cargo-Cult)
3. **Consistency.** Divergence from an existing seam without explicit
   justification is a finding — the change should reuse the seam, not add an Nth
   path. (constitution §2/§4)
4. **Edge cases.** Null/empty/race/concurrent-write/auth/off-by-one. Cite
   `file:line`. Defensive spackle added to mask one of these is itself a finding,
   not a fix. (constitution §3)
5. **Tests.** A change without a test that fails before the fix is not done; a
   test that executes a line without asserting its result is not coverage. Watch
   for additions that grow a god-file even when locally correct. (constitution
   §8/§9)
6. **Promises to people.** If the change touches anything a person reads
   (email, page, message), list every sentence that promises an action or an
   outcome and trace each to the code that makes it true and the test that
   proves it. Walk the person's whole journey, not the diff: the step before and
   after this change, and what happens when they reply. A promise with no path
   is a `[must-fix]`. (constitution §12)

## The verdict

- **Binary.** Exactly `approve` or `changes_requested`. No "approve with
  comments" — that softens the signal.
- **Labeled.** Every finding is either `[must-fix]` or `[nice-to-have]`, never
  blended. A `changes_requested` is carried by its `[must-fix]`s.
- **Directed.** A `[must-fix]` names *what's wrong*, *where* (file:line or the
  plan section), and *the right shape*. Rejection without direction wastes the
  author's next turn.
- **Sectioned.** Each finding cites the constitution section it implicates
  (`§3 defensive spackle at foo.rs:120`), so the author and the next reviewer
  read the same vocabulary.

## Anchoring (what keeps the critic honest)

The critic judges against **execution and the written plan, never abstract
taste.** A finding traces to: a plan divergence, a constitution section, an
anti-pattern, a failing/absent test, or a concrete edge case with a `file:line`.
"I'd have written it differently" is not a finding. This anchoring is what makes
the verdict trustworthy enough to gate on — an unanchored aesthetic judgment is
noise.

## One round, then a human

The critic runs **exactly one round** per change. On `changes_requested`, it
routes the verdict back to the author once (the adapter decides the channel). If
the change still needs work after the author addresses it, that second round is
the signal a **human** should weigh in — not another critic loop. Don't loop
twice; don't burn human attention on what a deterministic gate already caught;
don't spend the org's tokens on AI ping-pong.

## Gating: observe → enforce

The deterministic gates (fitness function, mutation, duplication, coverage)
already *block* the mechanically-checkable constitution sections. This critic
covers the **judgment** sections (§1, §3, §5, §9, §10), which a calibrated judge
can flag but a linter cannot.

Roll it out the same way `grant_role` went live:

- **Observe (first).** The critic posts its verdict as advisory. A
  `changes_requested` is visible to the author and the human, but it does **not**
  block merge. Log every would-block verdict and watch the rate and the
  false-positive ratio.
- **Enforce (after calibration).** Promote to blocking only the high-confidence
  subset — and block only on agreement (a single low-confidence flag downgrades
  to advisory and routes to a human). Split or uncertain verdicts never
  hard-block; they escalate.

The critic is a backstop, not the first line. An author who has internalized the
constitution rarely trips it — that is the goal. When the critic and the author
disagree and it can't be resolved in one round, the human decides.
