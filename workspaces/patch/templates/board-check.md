## Intent

**Purpose:** Never reached. The board check runs entirely in preRun (agency_tools.board.sweep, then predicate.always_stop), so no LLM run is ever queued for it.
**Success:** The schedule rule stops in preRun every time; if this text ever reaches a model, the rule's preRun has been broken.

---

The board check ran in preRun and stopped. There is nothing to do. End the run.
