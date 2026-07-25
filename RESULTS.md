# Results

Every number here came from a script in `bench/`. Each finding names the file
that produced it and the command that regenerates it. Nothing is estimated,
rounded up, or extrapolated. Where a thing was not tested, it says so.

Reproduce all of it:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./bench/run_all.sh
```

**Measured on:** Darwin 25.5.0 arm64, Python 3.14.6, `mcp` 1.28.1,
`langgraph` 1.2.9, `langgraph-checkpoint-sqlite` 3.1.0. Run date 2026-07-25 UTC.
Raw JSON in `bench/results/`.

---

## 0. Is the comparison valid at all?

Both implementations run the same six-node loop, share the same decision rules
(`tools/policy.py`), and drive the same MCP tool layer. Neither imports the
world module directly. If they diverged on behaviour, none of the numbers below
would be comparable.

`python bench/parity.py` runs all four scenarios on both and diffs the run digest.

| Scenario | Outcome fields identical | Full digest identical | Status | Spent |
|---|---|---|---|---|
| baseline | yes | yes | done | $1.80 |
| irreversible | yes | **no** | escalated | $0.60 |
| overspend | yes | **no** | escalated | $0.60 |
| verify_stuck | yes | **no** | escalated | $1.40 |

Outcome fields are `status`, `exit_reason`, `escalation`, `approved`, `cursor`,
`spent_usd`, `artifacts`, `attempts`, `verified`. Both loops stop in the same
place, before the same step, having spent the same amount, on every scenario.

The three escalating scenarios differ on exactly two fields, `history` and
`records`, and the cause is finding 4. That divergence is a result, not a
defect in the port.

---

## 1. The port was easy, and it did not save any code

`python bench/static_measure.py`. Two counts per bucket. **Code** is physical
lines minus blanks minus whole-line comments, and includes docstrings. **Logic**
additionally strips every docstring, because the first count would flatter
whichever implementation I happened to write more prose in, and I wrote a longer
module docstring on the LangGraph side.

| Bucket | Code lines | Logic lines |
|---|---|---|
| Shared (MCP server, MCP client, policy, world, fault injection) | 472 | 374 |
| Hand-rolled only (`loop.py`, `run.py`, `atomic_io.py`) | 254 | 231 |
| LangGraph only (`graph.py`, `run.py`) | 277 | 256 |
| Frozen tool contract (`tool-defs.json`, non-blank) | 152 | n/a |

**The LangGraph implementation is 23 lines longer counting docstrings, and 25
lines longer with docstrings stripped.** The two counts agree on the direction,
which is the point of running both.

That is not a knock on LangGraph so much as a statement about scale. The
hand-rolled loop is a `while` loop over a dict of six functions plus 34 lines of
atomic file write. There is not enough machinery here for a framework to
amortise. The lines LangGraph removed (the driver loop, the checkpoint call
sites) came back as graph construction, a `TypedDict` channel schema, partial
state merging, and reading final state back out of the checkpointer.

Elapsed time for the port itself was a few hours, most of it spent on the three
sharp edges below rather than on the state machine.

## 2. Crash-safe resume: a genuine tie

`python bench/kill_test.py`. Two kill points, both implementations, baseline
scenario, compared against a clean uninterrupted run.

**Kill point A** is an external `SIGKILL` to the process group, sent 1.0s into a
3.0s tool call on step s2. **Kill point B** is `os._exit(137)` at the end of the
`act` node's second visit, after the tool returned and before either
implementation commits anything. B is the window that costs money.

| | Hand-rolled | LangGraph |
|---|---|---|
| A: resumed state matches clean run | yes | yes |
| A: side-effecting tool calls duplicated | none | none |
| B: resumed state matches clean run | yes | yes |
| B: side-effecting tool calls duplicated | `act(s2, attempt 1)` twice | `act(s2, attempt 1)` twice |
| Temp files left behind after kill | none | n/a |
| Truncated or unparseable checkpoint | never observed | never observed |

Both recover exactly. Both re-run the in-flight step exactly once and no more.
At-least-once execution at the node boundary is the semantics in both cases, and
neither offers exactly-once without idempotent tools.

The honest read: LangGraph gives you this for free and the hand-rolled version
needed `atomic_io.py` to be written and gotten right, including the directory
`fsync` that people skip. That is 34 lines of well-understood code, but they are
34 lines you can get subtly wrong once and never notice.

## 3. The file you would back up is not the checkpoint

`python bench/wal_test.py`. Kill a run with `SIGKILL`, then copy the one obvious
"the state is in here" file to a clean directory on its own and resume from it.
This is what a backup script, an `rsync`, or a volume snapshot does.

| | Hand-rolled | LangGraph |
|---|---|---|
| Canonical file | `state.json` | `checkpoints.sqlite` |
| Its size after the kill | 1,407 bytes | 4,096 bytes |
| Sidecars | none | `-wal` 251,352 bytes, `-shm` 32,768 bytes |
| Resume from that file alone | completes, 23 nodes, $1.80 | **fails: `EmptyInputError: Received no input for __start__`** |
| Resume with all files present | completes, 23 nodes | completes, 23 nodes |

The SQLite store was in WAL mode, so the entire run lived in the `-wal` sidecar
and the main database file was one empty page. Copying `checkpoints.sqlite`
alone loses the run silently and completely.

**This is not LangGraph's bug.** It is standard SQLite behaviour and it is
inherited from `langgraph-checkpoint-sqlite`. It is worth knowing anyway,
because "the checkpoint is in checkpoints.sqlite" is what the filename tells you
and it is wrong.

Fair caveat on the sizes: these two stores are not like for like. `state.json`
holds only the current head state. The SQLite store holds every checkpoint in
the thread, which is what makes LangGraph's time travel and forking possible.
The 178x is a feature difference, not bloat.

## 4. `interrupt()` throws away the interrupting node's state update

`python bench/parity.py`, escalating scenarios.

LangGraph's `interrupt()` raises `GraphInterrupt` out of the node. The partial
state that node was going to return is discarded and never committed. So after
a tripwire fires:

- The hand-rolled loop's own state says `status: escalated`, with the tripwire
  text and `decide:escalate` in its history.
- LangGraph's persisted state says nothing about the escalation. The reason
  exists only in `snapshot.tasks[].interrupts[].value`, and `langgraph_impl/run.py`
  has to read it back and re-attach it to the digest by hand.

Measured effect: `history` and `records` are the only two fields that diverge
between the implementations, on exactly the three scenarios where a tripwire
fires.

This is a design consequence, not a bug. Interrupt is meant to be re-entrant, so
committing partial work before it would break the resume contract. But it means
**the framework's own persisted state does not record why it stopped.** You have
to know to go looking somewhere else.

## 5. Resuming an interrupt re-runs the whole node, including its side effects

`python bench/escalation_test.py`, `irreversible` scenario. The loop halts
before publishing to a public index, a separate process approves it, and the run
continues to completion.

| | Hand-rolled | LangGraph |
|---|---|---|
| Halted before the irreversible step | yes | yes |
| A separate process could answer the halt | yes | yes |
| Completed correctly after approval | yes | yes |
| `record(kind=escalate)` calls written | **1** | **2** |

Both loops did the right thing. But LangGraph wrote the escalation to the
durable run record twice, because resuming with `Command(resume=...)` re-executes
the interrupted node from its first line, and the `record` call sat above the
`interrupt()` call.

**Verified mitigation:** moving `interrupt()` above the side effect brings it
back to 1 escalation record. Measured directly by reordering the two statements
and re-running:

```
escalate records with interrupt() FIRST: 1
```

This is documented LangGraph behaviour. It is also the sharpest edge in the
whole port, because the code reads correctly, the run completes correctly, the
state is correct, and the only evidence of the double write is in the tool
layer's own log. Anything above an `interrupt()` in a node must be idempotent.

Credit where due: the hand-rolled loop had no human-approval path at all before
this test. Adding one cost 14 lines across `policy.py` and `run.py`. LangGraph
already had the primitive, and its version survives process death without a
custom flag. The framework wins the feature and loses the footgun.

## 6. The default recursion limit is one super-step away from the baseline run

`python langgraph_impl/run.py` with `recursion_limit` varied.

The baseline scenario needs **24 super-steps**. LangGraph's default
`recursion_limit` is **25**. Measured directly:

| Limit | Outcome |
|---|---|
| 25 (default) | completes |
| 24 | completes |
| 23 | `GraphRecursionError` |
| 22 | `GraphRecursionError` |

A single extra verify retry is 4 super-steps. The baseline run has one retry in
it. A second retry, on any step, would have taken a production run past the
default and killed it with an error about recursion, which is not what a
practitioner would go looking for when a retry loop is the actual cause.
`langgraph_impl/run.py` sets it to 200 explicitly.

The flip side, stated plainly: **the hand-rolled loop has no runaway protection
at all.** Its `while` loop would spin forever. LangGraph is right to have this
limit. The default is just tight for any loop that retries.

## 7. LangGraph persists position; it does not persist the verdict

`python bench/failure_modes.py`. Three injected faults, both implementations.

| Fault | Injected as | Both loops saw | Exit code (both) |
|---|---|---|---|
| Tool schema violation | `act` returns `cost_usd` as the string `"0.40"` | `ToolError: Output validation error: '0.40' is not of type 'number'` | 3 |
| Tool timeout | `act` sleeps 600s against a 2s client deadline | `ToolTimeout: Timed out while waiting for response to ClientRequest. Waited 2.0 seconds.` | 3 |
| Mid-run exception | `act` raises inside the handler | `ToolError: injected mid-run exception in act(s2)` | 3 |

Identical error surface. The difference is what survived:

| | Hand-rolled | LangGraph |
|---|---|---|
| Status in the persisted state | `failed` | `running` |
| Reason in the persisted state | full message, e.g. `tool error in act: act: Output validation error: ...` | absent |
| Where it stopped | `next_node: act` | `next_nodes: ["act"]` |

LangGraph's checkpointer records where the run was, not that it died or why. The
error message lives in the process's stderr and vanishes with the process.

**This row overstates a framework difference, and the honest version is
narrower.** The hand-rolled driver owns the state dict, so its `except` clause
writes `status: failed` into the checkpoint on the way out. I did not write the
equivalent in the LangGraph version, and most of the gap in that table is that
choice, not the framework.

What is genuinely a framework property: an exception that escapes a node commits
nothing, so a node cannot record its own death, and the caller does not own the
state object and cannot simply annotate it. What is genuinely available:
`aupdate_state` from the caller. Tested directly, after the same injected
exception:

```
status: failed
exit_reason: tool error: act: injected mid-run exception in act(s2)
next: ('act',)
```

Two lines, and the run stays resumable at the same pending node. So the correct
claim is **"LangGraph does not record a verdict by default and you have to know
to ask for one,"** not "LangGraph cannot record a verdict." The default is the
finding. The gap is not.

Timing note: on the timeout case both took about 4.8s wall against a 2.0s
deadline. The extra time is MCP client and subprocess teardown, not the loop.

## 8. Two things bit both implementations equally, and neither framework would have caught either

**The exception you raise is not the exception you catch.** The MCP stdio client
runs its transport inside an anyio task group, so a `ToolError` raised inside
`async with open_tools()` comes out wrapped in an `ExceptionGroup`. Both
implementations originally caught `except (ToolError, ToolTimeout)`, both caught
nothing, and both died with a raw traceback and the wrong exit code on all three
faults. The fix is `tools/mcp_client.py: first_leaf()`, ten lines, and it is
identical for both. This is a property of the MCP client, not of either
orchestrator.

**The durable run record was the one thing that was not durable.** The tool
layer's sequence counter started life as a module-level dict in the server
process. Every kill-and-resume test came back reporting 5 records against a
clean run's 6, because resume spawns a new server process and the counter
silently restarted from zero. Neither checkpointer would ever have caught this,
because the counter lived outside both of them. It is now persisted with
`fsync` and `os.replace` in `tools/world.py`.

That second one is the most transferable lesson in the whole exercise. The
framework makes the graph's state durable. It says nothing about the durability
of anything your tools own, and a run record is exactly the sort of thing tools
own.

## 9. What adopting the framework costs, counted

`python bench/static_measure.py` builds two throwaway virtualenvs from scratch.

| | `mcp` only | `mcp` + `langgraph` + `langgraph-checkpoint-sqlite` | Difference |
|---|---|---|---|
| Distributions installed | 29 | 56 | **+27** |
| `site-packages` on disk | 46.9 MB | 75.1 MB | **+28.2 MB** |
| Fresh install wall time | 4.52s | 6.89s | +2.37s |

Cold start, best of 7, warm page cache:

| | Hand-rolled | LangGraph | Difference |
|---|---|---|---|
| Import only, median | 0.319s | 0.562s | **+0.242s** |
| Import only, min | 0.315s | 0.559s | +0.244s |
| Full baseline run, median | 0.713s | 0.980s | **+0.267s** |
| Full baseline run, min | 0.708s | 0.978s | +0.270s |

These four timings are the one part of this file that moves between runs. They
are transcribed from the committed `bench/results/static.json`; re-running
`bench/static_measure.py` will shift them by a few tens of milliseconds without
changing the gap. Everything else in this file is a count or a yes/no and is
stable across runs.

The 27 extra distributions include `langsmith`, `requests`, `urllib3`,
`websockets`, `sqlite-vec`, `zstandard`, `xxhash`, `orjson` and `ormsgpack`.
Most of that is the LangChain observability and transport surface, which this
artifact never uses. On a CLI that runs every few minutes, a quarter of a
second is nothing. On a Lambda cold start it is not nothing.

---

## The verdict

LangGraph is not a shortcut at this size. It cost 23 more lines than the loop it
replaced (25 with docstrings stripped), 27 dependencies, and about a quarter of
a second per run. It did not make
crash-safety easier than 34 lines of atomic file write, and on the measurement
that matters most, kill and resume, the two implementations are exactly tied.

What it does buy is real: a persistence model that keeps the whole thread rather
than the head, a human-in-the-loop primitive that survives process death without
a bespoke flag, and a runaway guard the hand-rolled loop simply does not have.
Those are things worth having, and two of the three would have taken real
thought to build.

What it costs beyond the counted numbers is three sharp edges that are invisible
until something goes wrong: an interrupt that discards the interrupting node's
state, a resume that re-executes side effects above the interrupt, and a
checkpointer that records position without verdict. All three are defensible
design consequences. All three will surprise someone in production.

The most useful finding is none of those. It is that both implementations were
broken in exactly the same way by the MCP client's `ExceptionGroup` wrapping, and
that the run record, the one artifact this whole exercise is about making
durable, was the one piece of state neither checkpointer protected. **Durability
is a property of a system, not of an orchestrator.** Adopting a framework moves
the boundary of what is handled for you. It does not remove the need to know
where that boundary is.

If you have already built these primitives, porting is a day and the outcome is
a wash. If you have not, adopt the framework. The version of this loop that
should worry you is neither of these two. It is the one where a tripwire fires,
a human approves it, and nobody ever checks whether the approval got written
down twice.

---

## What this does not measure

Stated plainly, because the list matters as much as the results.

- **No LLM is involved anywhere in this repo.** The policy is deterministic. That
  is deliberate: a stochastic policy would make every number above
  unreproducible. It also means this says nothing about LangGraph's model
  bindings, message state, tool-calling agents, or `create_react_agent`, which
  is the majority of what most people use LangGraph for.
- **No concurrency.** The graph is linear. LangGraph's fan-out, `Send`, map
  reduce, and state reducers under parallel writes are untested here, and they
  are a large part of what a framework earns its keep on.
- **No LangSmith, no LangGraph Platform, no streaming.** LangSmith is installed
  as a transitive dependency and never used.
- **One checkpointer backend.** `AsyncSqliteSaver` only. The Postgres saver has
  different durability and portability characteristics and was not tested.
- **Single machine, single process, warm cache, one OS.** No container, no cold
  Lambda, no network filesystem. The cold-start numbers are best-of-7 on a laptop.
- **The kill tests use two specific kill points.** They are the two that seemed
  most informative. They are not a proof of crash safety, and no fuzzing over
  kill timing was done.
- **Time-travel and forking were not exercised**, despite being cited above as
  the justification for the SQLite store's size. That justification is from
  LangGraph's documented feature set, not from a measurement in this repo.
