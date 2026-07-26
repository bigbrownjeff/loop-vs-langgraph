# Decision: adopt LangGraph as the operating loop's orchestrator

**Date:** 2026-07-26 · **Status:** accepted · **Supersedes:** the "close to a
wash" verdict in the first published RESULTS.md, which is preserved in this
file's History section and in `bench/results-archive/`.

## Decision

New agent-loop work builds on the LangGraph implementation
(`langgraph_impl/`), hardened as committed here. The hand-rolled loop stays in
the repo as the measurement baseline, not as a production path.

## Why the first verdict said "wash" and this one does not

The first verdict weighed what the benchmarks could see: lines of code,
dependencies, cold start, and crash-safe resume, where the two implementations
are exactly tied. On those axes it *is* a wash, and the numbers still say so.

The re-evaluation weighs two things the scoreboard underweights:

1. **The asymmetry of what each side is missing.** Everything LangGraph lacked
   here was recoverable by configuration or a few lines in the caller: the
   escalation double-write (statement ordering, measured fix), the missing
   failure verdict (`aupdate_state`, two lines, measured), the tight default
   recursion limit (one argument). Everything the hand-rolled loop lacked was
   *structural*: no runaway protection of any kind, no human-in-the-loop
   primitive that survives process death, no thread history, and 34 lines of
   bespoke atomic-write code that must stay correct forever with no upstream
   fixing bugs in it. One side's gaps close with flags; the other side's gaps
   are unbuilt subsystems.

2. **Everything this repo deliberately does not measure is on LangGraph's side
   of the ledger.** The deterministic policy makes the numbers reproducible,
   and it is also LangGraph's worst case: none of the model bindings, tool
   loops, streaming, fan-out, retry/timeout policies (`RetryPolicy`,
   `TimeoutPolicy`, `error_handler`, `RunControl` as of 1.2), middleware, or
   checkpointer backends are exercised. The moment a real model or a second
   concurrent branch enters this loop, the hand-rolled side has to grow new
   subsystems; the LangGraph side has to import them.

A framework earns adoption not on the workload where you can match it by hand
but on the roadmap where you cannot. The measured tie plus the structural
asymmetry is the argument, and the tie is what makes the asymmetry visible.

## What "hardened" means here, concretely

All three sharp edges from RESULTS.md are closed in the committed code, and
each mitigation is *measured*, with the original behaviour reproducible behind
an environment flag so the findings do not become folklore:

| Edge | Fix in this repo | Reproduce the footgun |
|---|---|---|
| Resume re-runs side effects above `interrupt()` (finding 5) | `interrupt()` moved above the record call in `decide`; escalation recorded exactly once, after approval | `LOOPLAB_ESCALATION_ORDER=record_first` |
| Checkpoint records position, not verdict (finding 7) | runner writes `status: failed` + reason via `aupdate_state`; run stays resumable at the pending node | `LOOPLAB_SKIP_VERDICT_WRITE=1` |
| Default recursion limit one super-step from a normal run (finding 6) | `recursion_limit=200` set explicitly | pass a smaller limit |

Plus one hardening the original port did not have: `durability="sync"` on
invoke, so a checkpoint is persisted before the next super-step starts. The
default (`"async"`) overlaps the write with the next step, which reopens
exactly the window kill point B aims at. The kill tests pass against the sync
path; sync is the production setting.

## Standing operational rules that adoption does not remove

- **Anything above an `interrupt()` must be idempotent.** Now also the
  official documented guidance.
- **Back up the SQLite checkpointer with `VACUUM INTO` or the backup API,
  never by copying `checkpoints.sqlite`** — the run lives in the `-wal`
  sidecar (finding 3, unchanged).
- **Durability of tool-layer state is the tools' own problem.** Neither
  checkpointer protects anything outside the graph (finding 8). The run
  record keeps its own fsync discipline in `tools/world.py`.

## History

- 2026-07-25: first measurement pass (macOS, Python 3.14.6). Verdict: close to
  a wash; adopt if the primitives are unbuilt, port only as a learning
  exercise if they are built. Archived in
  `bench/results-archive/2026-07-25-darwin-py3.14/`.
- 2026-07-26: full reproduction on a second environment (Linux, Python
  3.11.15, same package versions — every count and yes/no finding identical;
  timing gaps roughly doubled in the container but stayed sub-second).
  Hardened implementation committed, mitigations measured, verdict revised to
  the above.
