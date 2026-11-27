# Likelihood: when a finding blocks

Every sc0red reviewer uses this rating to decide whether a finding blocks. Astra
(`gate/astra-verify`) uses it on a diff, and Scarlett uses it on a plan. A reviewer
reads it from here and doesn't restate it.

## The failure scenario

A finding can block only when it states a whole failure scenario:

- **who** is affected;
- the **input or event** that triggers it;
- the **harm** that follows;
- its **likelihood**, one of the four levels below;
- **how often**: why that level, from what the code, the data or the repo shows
  (what triggers it, and how often that happens).

A finding without a whole scenario doesn't block, whatever it is labelled.

## The four levels

Rate the scenario by how often its input or event happens in normal use:

- "very likely": it happens in ordinary use, on most runs or most days.
- "likely": it is expected within weeks of ordinary use; the trigger is routine, not a coincidence.
- "unlikely": it needs an unusual but plausible combination: a rare input, a specific order of events.
- "very unlikely": it needs a coincidence of independent events, misuse, or someone working around the system.

Rate what happens, not what could. A scenario that needs two things at once to
coincide is "unlikely" at most unless you show they routinely coincide.

## What blocks

Only a "very likely" or "likely" finding blocks. An "unlikely" or "very unlikely"
one is reported and doesn't block.
