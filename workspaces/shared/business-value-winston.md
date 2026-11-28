# Business Value for Winston issues

This guide turns the Business Value levels in [`estimation.md`](estimation.md#business-value) into rules for Winston's board (SC0RED project 1). Each project has its own guide; NeuroBloom's is its co-founder's. Each criterion names its source: an owner statement with its date, the board triage of 2026-11-27 that applied those statements to Winston's backlog, or the level definitions in `estimation.md`. The level definitions themselves (High, Medium, Low, None) and how Business Value combines with Velocity into a priority live in `estimation.md`, and this guide uses them as written there.

## Who rates, and who decides

Product owns Business Value (`estimation.md`). For us, Product is the owner.

When an issue arrives on a board, the agent that triages it proposes a level and writes the reason into the issue. That level is a proposal. The owner sets the final level by changing the board's Business Value field, and his level stands:

- A card whose Business Value field already holds a level keeps it. The triage agent writes its section for that level and never changes the field.
- The triage tool refuses a level that differs from the one on the board.
- A `## Business value` section that a person wrote into the issue stays as written.

## The section every issue gets

Triage adds this section to the end of the issue body:

```markdown
## Business value

**Level: High** (proposed at triage; the owner sets the final level on the board)

Who is affected, and what it costs if the issue waits. Two or three sentences,
in plain words a non-engineer can judge: which families, staff or the owner,
how often, and in dollars where the issue shows them.

Rests on: the source this guide gives for the criterion (an owner ruling with its date and a short quote, the 2026-11-27 board triage, or estimation.md).
```

Write the cost from the issue's own evidence. When the issue gives no number, describe who sees the problem and how often, and leave the number out.

## The levels

Winston is the practice's office admin. He bills families, keeps the calendars, answers family and staff email, runs intake and files paperwork. Since November 2026 he is the only system that bills: the owner said on 2026-11-05, "we are switching to Winston for billing this month ... we need to find and fix any discrepancies for November ASAP", and TheraNest shuts off in November (owner, 2026-10-14).

### High

Rate High when the issue does any of these today, with no person catching it:

- **Charges a family wrongly, drops a charge, or charges twice.** The owner, 2026-11-20, on a billing fix held for a month boundary: "So you want to let a bug fix that impacts billing wait until the next month so that it impacts the entire month?" Billing fixes ship as soon as they are ready.
  - winston-agency#1936: billing keeps the first duplicate copy, so a cancellation tagged on the other copy still bills.
  - winston-agency#1983: a #no-show on one copy of an evaluation bills about $139 instead of holding.
  - winston-agency#1928: a client name followed by an extra word reads as nobody, and the session goes unbilled with nothing said.
  - winston-agency#1886: a monthly-invoice family would have been charged weekly.
- **Bills a session type in a way the owner's ruling forbids.** Evaluations bill like sessions and are charged on Sunday (owner, 2026-11-15). "Unavailable" bills like a cancelled session at the full rate (owner, 2026-11-17). Families who pay by ACH are always on monthly invoice and chase (owner, 2026-11-03).
- **Ties one client to another.** The owner, 2026-11-05: what alarmed the office lead most "was the assertion that Winston mixed up two clients". A claim that links two clients is the worst thing Winston can say, even when it is false, and a real wrong-client action is worse still.
  - winston-agency#1945: a shared client name was narrowed to one child before anyone was asked.
  - winston-agency#1944: a document search reads another client's folder without checking the bound client.
- **Sends a family something false, or something they cannot act on.** Every promise in family copy needs a working path behind it (owner, 2026-10-15: "why do we keep missing these little corner cases?"). This covers a promised link or retry with no code behind it, the wrong template or language (owner, 2026-11-08), and any mail to a family under a do-not-email hold.
- **Sends as someone else.** The owner, 2026-10-19: "I don't think Winston should ever send as someone else. That is a recipe for disaster."
- **Leaves staff without an answer.** The owner, 2026-11-06: "why did she go radio silent that is never ok". A staff request that fails is re-run by engineering, and staff are never asked twice (owner, 2026-10-27).
  - winston-agency#1998: staff requests on the catch-all end undone every few days.
  - winston-agency#2058: a cancellation run ended undone, and an unrecorded cancellation can bill a family.
- **Leaves a job TheraNest used to do with nothing doing it.** TheraNest shuts off in November (owner, 2026-10-14). The same holds for the promises that replace it: superbills for every family from 2026-12-01 (owner's direction, relayed 2026-10-24) and no missing billing codes (owner, 2026-10-27: "we need to eradicate the 'missing billing code' problem").
  - winston-agency#1842: a child's diagnosis code is asked of the treating therapist on the wrap-up.

### Medium

Rate Medium when the harm is real and someone sees it, but no charge is wrong and no family or staff member is misled today:

- **Money reporting the owner reads and acts on.** winston-agency#2030: the Sunday check lists a family as "behind" when the 5 PM run will charge them, and the owner read one as arrears.
- **Wrong data that will reach a bill, a superbill or a family later.**
  - winston-agency#2042: a sibling's evaluation in the same folder can set a child's re-evaluation date.
  - winston-agency#1617: intake writes a placeholder second guardian named "None" as a contact.
  - winston-agency#1797: the diagnosis code set must be updated before 2027-10-01, or every diagnosis write and superbill fails that day.
- **A staff reply that is doubled, or work done twice.** winston-agency#2011: one message with two asks becomes two runs that each act on the whole message.
- **Noise that staff or the owner read every day.** The owner, 2026-09-22: Winston "reads an email that [the office lead] can read herself and then creates a NEW email which doubles [her] work." Taking such a message away is Medium. winston-agency#1953: staff were told 27 items needed review when 3 did.

### Low

Rate Low when nobody sees the problem today. The board triage of 2026-11-27 ranked these last among real issues:

- An error that happened once and was re-run, with no family or staff harm.
- A gap whose case does not exist yet. winston-agency#2021: two clients could share a documentation folder, and none do today.
- A test gap on a path that works. winston-agency#2014: four evaluation scenarios pass when Winston says nothing.

### None

Rate None for work with no effect on families, staff or money. The first two kinds are `estimation.md`'s own None examples ("purely technical work", internal tooling, refactors):

- Refactors, schema cleanup and internal docs. winston-agency#1779 (split one function), winston-agency#1954 (drop unused Xero columns).
- Deploy and developer tooling. winston-agency#1586: deploy-config cannot install the systemd units it ships.
- Work outside Winston's job. He is the office admin (owner, 2026-10-06). He may read clinical files for dates and filing only (owner, 2026-11-26), and clinical or marketing writing belongs to people.
- A new check against a case that has never happened. The owner, 2026-11-20: "defensive checks are the devil's work." A key rotation with no evidence of a leak is the same (owner, 2026-10-26: "drop it").

Velocity Impact still counts for None and Low work: a tooling fix that unblocks every run scores well on the grid in `estimation.md`.

## Tie-breakers

These rules choose a level for one Winston issue that fits two. They rate the issue only. The order of work across issues stays with the priority grid in `estimation.md` (Business Value × Velocity Impact), whose ties Story Points and dependencies break.

1. **Money first.** A wrong charge or a dropped one takes the higher level (owner, 2026-11-05 and 2026-11-20).
2. **Then what a family or staff member sees.** A false claim, a missed reply or silence rates above wrong data nobody has read yet. Among these, a claim linking two clients is the worst (owner, 2026-11-05).
3. **Then data, then noise.** Data that will reach a bill or a family rates above operational noise and tooling.
4. **Recurring beats one occurrence.** An error that keeps firing rates above one that fired once.

These four are the order the board triage of 2026-11-27 used on Winston's backlog, built from the rulings above.

When you cannot tell which of two levels fits, name both in the section and say what would decide it. The owner settles it on the board.
