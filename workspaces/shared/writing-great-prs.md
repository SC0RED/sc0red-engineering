# Writing Great Pull Requests

## Audience

A PR body has two readers, and they want different things.

The **operator** decides whether this ships. They read first, read least, and
often have no prior context on the ticket. They need to know what was wrong and
what now happens instead — in the words they would use, not the words the code
uses.

The **reviewing agent** (Scarlett) checks the diff against the stated plan. It
needs the design, the file list, and the decisions — after the operator's
question is already answered.

Both are served by the same order: **problem first, mechanism second.** A body
that opens with the seam, the pattern, or the module being extended has answered
the reviewer's question and skipped the operator's.

This document defines the PR body for every change type. The type-specific
opening sections are in *Opening by change type* below.

---

## Title Rules

A title's job: an unfamiliar reader says what the change does, in plain English,
in five seconds.

- **Write a sentence, not a branch slug.** The branch name is a separate field.
- **Verb-first or subject-first**, whichever reads plainly. Both work.
- **8-word heuristic** for the core claim. Clauses after a comma are free.
- **Name the effect, not the mechanism.** The mechanism belongs in Approach.

| Bad | Good |
| --- | --- |
| `suppress-codes-needed-email-after-superbill-sent` | `Stop asking parents for codes they already sent` |
| `fix-singleflight-race-handle-superbill` | `Two billing runs for one client no longer collide` |
| `practice-owner-greeting-parent-family-school-exclamation` | `Family greetings use the practice owner's name` |
| `Fix bug in digest` | `Therapists with a full day saw an empty digest` |

---

## The Spine

Every PR body uses these sections in this order. Sections marked *(conditional)*
appear only when they apply.

1. **`Closes <issue_url>`** — first line, when an issue exists.
2. **Problem** — the type-specific opening (below). Plain language, one
   paragraph. The reader learns what was wrong here and nowhere else.
3. **Approach** — what changes, in plain English first, then the files. This is
   where the Architect beat's design lands: the seam being followed, the shape
   of the change, the pattern applied, what stays untouched. Name mechanisms
   freely here — a plain sentence has already carried the meaning.
4. **Verification** — the commands run and what each one proves. When a
   behavior is argued from a contract rather than executed, say which, and say
   why no harness covers it.
5. **Decisions log** — choices made and the reason, including any departure
   from what the dispatch asked for. Reviewer feedback that reversed an earlier
   claim gets a line here.
6. **Open questions** *(conditional)* — what needs an operator's answer, and
   what follow-up work this deliberately leaves undone.

**Length.** The Problem section earns as many words as it takes to be
understood; everything else is sized to the diff. A forty-line change with a
seven-hundred-word body is arguing that the work was hard, which is not a
question the reader asked.

---

## Opening by change type

The Problem section takes one of four shapes. Pick by what the change does.

### Bug fix → **Symptom**

What a person experienced, in their terms, before any diagnosis. Name who saw
it, what they saw, and what they expected instead. The root cause goes in
Approach.

> **Problem:** A parent was asked to send diagnosis codes they had already
> provided, minutes after we emailed them their superbill. Two of our billing
> steps each read the client's record, then acted on what they read a few
> seconds later, after the other had already updated it.

### Enhancement → **Limitation**

What the current behavior costs someone. The feature already exists; this says
where it falls short and for whom.

> **Problem:** Superbills name the therapist currently assigned to a client,
> not the one who actually ran the visit. When a client changes therapists
> mid-period, the superbill credits the wrong clinician and the family's
> insurer rejects it.

### New capability → **Job to be Done**

*When [context], [actor] wants to [motivation], so they can [outcome].* Names
the actor and the outcome, not the implementation.

> **Problem:** When a family goes quiet during fall scheduling, the front desk
> wants one last outreach pass before the slot is released, so they can fill
> the schedule without giving up on families who simply missed the first email.

### Mechanical / chore → **Motivation**

One or two lines. Refactors, renames, dependency bumps, and CI changes need the
reason, not a symptom.

> **Problem:** The practice's name is typed as a literal in eleven templates.
> Each new template copies one of them, so a rename would need eleven correct
> edits.

---

## Vocabulary

Mechanism names are precise and worth using — after a plain sentence has
carried the meaning. `singleFlightKey`, `preRun`, seam, shadow, fail-soft, and
route names all belong in Approach, Verification, and the Decisions log.

In the Problem section, describe what happened in the world. "Two runs for the
same client overlapped" reaches every reader; "the route was missing a
`singleFlightKey`" reaches the one who already knew.

When a rollout runs in stages, each stage states its risk-reason and its exit
condition in plain words: what could go wrong, and what has to be true before
it advances.

---

## Worked Example

> **Title:** Stop asking parents for codes they already sent
>
> Closes https://github.com/SC0RED/winston-agency/issues/832
>
> **Problem:** A parent was asked to send diagnosis codes they had already
> provided, minutes after we emailed them their superbill. Two of our billing
> steps each read the client's record, then acted on what they read a few
> seconds later — by which point the other step had already filled the codes
> in. Each was working from a stale copy.
>
> **Approach:** Both steps now re-check the client's record immediately before
> sending, and the same client's billing no longer runs twice at once.
>
> Three files. `agency.yaml` — `handle-superbill` and `request-diagnosis-codes`
> each gain a `singleFlightKey` (`{{ clientId }}~{{ period }}` and
> `{{ clientId }}`), the same serialization the sibling routes `assign-therapist`
> and `confirm-own-cpt` already use. `templates/handle-superbill.md` — when
> `build_superbill` reports missing CPT/ICD-10 codes, call it once more before
> declaring a hold; the tool re-reads the client on every call
> (`impl.py::_fetch_client`), so the retry is the live re-check.
> `templates/request-diagnosis-codes.md` — one fresh `entity.get` before
> composing the ask; codes present cancels the pending reminder and sends
> nothing, codes missing scopes the ask to what is actually absent.
>
> Untouched: the superbill tool (it already reads live state and fails loud),
> and the cheap early-exit checks in Steps 0–2, which can safely use the older
> snapshot now that the authoritative check sits at the send.
>
> **Verification:** `make check-all` green (cadence, dispatch-directive,
> preRun-coords, preRun-reachability, practice-name). `agency.yaml` parses via
> the registry `verifyCommand`. Template lint 628 → 625 hits, zero new. The
> concurrency behavior is argued from the serialization contract documented on
> the sibling keys — the repo's gates are static, so no harness executes the
> interleaving.
>
> **Decisions log:**
> - `request-diagnosis-codes` also gained a key, though the dispatch named only
>   `handle-superbill`. Two ask-chain runs for the same client can interleave,
>   and the pre-send re-check only sees a concurrent write once the runs
>   serialize — the issue's "no scenario produces both emails" is unreachable
>   otherwise.
> - Retried the tool instead of adding an `entity.get` branch. The tool's own
>   re-read re-validates everything and either succeeds or re-raises with
>   current truth, which avoids restating the missing-field logic in prose.
>
> **Open questions:**
> - Duplicate triggers spaced beyond the lock window (~80s apart against ~60s
>   runs) still deliver the superbill twice. The emails are identical rather
>   than contradictory, so it sits outside #832, and closing it needs a
>   send-once marker on the conversation — an interface change worth its own
>   PR. Say the word and I'll file it.
