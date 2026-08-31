---
name: gate23-loop-vs-langgraph-review
description: 2026-08-09 GATE 23 adversarial review of the public loop-vs-langgraph repo (origin/main c85bebd) — what broke, what survived, and the reusable attack that worked
metadata:
  type: project
---

First dedicated adversarial pass on the public `loop-vs-langgraph` artifact (origin/main
`c85bebd`, 2026-07-26). Outcome: 1 P0, 4 P1, 8 P2, verdict MERGEABLE-WITH-FIXES.

**Why:** the repo sells itself as "an honest engineering comparison" and is used as a public
credibility artifact (employment track, gap-closer). Its measurement machinery held up under
attack; its *claim discipline about the second environment* did not.

**How to apply:** the attack that paid off is generic to any "reproduced on a second
environment" claim — flatten both committed artifact sets to leaf key/value pairs and diff
them. Here that refuted the flagship claim in one command: the macOS set measured
*pre-hardening* code, so 6 LOC counts, `escalate_records_written` (2 vs 1),
`escalate_record_duplicated_on_resume` (True vs False), three `status_in_digest` fields
(running vs failed) and the WAL resume exit code all differ. Second reusable move: recompute
every hand-counted number against the repo's own measurement script — "34 lines of
atomic-write code" (5 occurrences) contradicts the repo's own 38 code / 28 logic / 46 physical.

**Survived (do not re-attack cold):** LOC artifacts are exactly in sync with committed source;
dependency accounting (29/56/+27) and all cold-start cells trace to `static.json`; the
crash-safe-resume tie is real; finding 6's recursion table reproduces on a third environment
(25/24 complete, 23/22 GraphRecursionError). See also [[review-hard-rules-copied-quotes]].
