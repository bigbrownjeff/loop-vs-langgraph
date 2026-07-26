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

**Measured on two environments**, same package versions in both: `mcp` 1.28.1,
`langgraph` 1.2.9, `langgraph-checkpoint-sqlite` 3.1.0 (the current releases
of all three as of 2026-07-26).

- **Linux x86_64 (container), Python 3.11.15**, run date 2026-07-26 UTC — the
  live `bench/results/`, measuring the committed (hardened) code.
- **Darwin 25.5.0 arm64, Python 3.14.6**, run date 2026-07-25 UTC — the first
  pass, archived in `bench/results-archive/2026-07-25-darwin-py3.14/`.

Every count and every yes/no finding is identical across the two environments.
Only wall-clock timings moved; both sets are shown in finding 9. The verdict
this file used to end with has been revised after the second pass and a
hardening pass on the LangGraph implementation; the decision and its
reasoning live in [DECISION.md](DECISION.md).

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
`records`, and the cause is findings 4 and 5: the hand-rolled loop writes its
escalation to the run record before it halts, while the committed LangGraph
node records it only after a human approves (the interrupt-first ordering).
That divergence is a result, not a defect in the port.

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
| LangGraph only (`graph.py`, `run.py`) | 286 | 265 |
| Frozen tool contract (`tool-defs.json`, non-blank) | 152 | n/a |

**The LangGraph implementation is 32 lines longer counting docstrings, and 34
lines longer with docstrings stripped.** The two counts agree on the direction,
which is the point of running both. The original port measured 277/256
(+23/+25, archived set); the difference since is the hardening pass — the
caller-side verdict write, the measurable escalation-ordering flag, and the
sync-durability call — which this file measures like everything else.

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

One choice behind the LangGraph row: the runner invokes with
`durability="sync"`, so a checkpoint is persisted before the next super-step
starts. The framework default is `"async"`, which overlaps the write with the
next step — exactly the window kill point B aims at. The kill tests here
measure the sync path, which is the setting a production run should use.

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
continues to completion. The LangGraph decide node is measured under **both
statement orderings**: the committed default puts `interrupt()` above the
record call; `LOOPLAB_ESCALATION_ORDER=record_first` restores the ordering the
port originally shipped with, where the side effect sat above the interrupt.

| | Hand-rolled | LangGraph (committed) | LangGraph (`record_first`) |
|---|---|---|---|
| Halted before the irreversible step | yes | yes | yes |
| A separate process could answer the halt | yes | yes | yes |
| Completed correctly after approval | yes | yes | yes |
| `record(kind=escalate)` calls written | **1** | **1** | **2** |

With the record call above the `interrupt()`, the escalation lands in the
durable run record twice, because resuming with `Command(resume=...)`
re-executes the interrupted node from its first line. That was the sharpest
edge in the whole port, because the code reads correctly, the run completes
correctly, the state is correct, and the only evidence of the double write is
in the tool layer's own log.

The committed ordering closes it — the escalation is recorded exactly once,
after approval — at a cost worth stating plainly: until a human answers, the
run record contains no escalation entry. The reason the run stopped lives in
the interrupt payload in the checkpointer, not in the tool layer's log. Pick
which ledger you want to be authoritative during the halt window; you do not
get both from one non-idempotent call.

This is documented LangGraph behaviour, and since this repo's first
measurement pass the official docs state the rule outright: side effects
before an `interrupt()` must be idempotent. Nothing in your editor will tell
you that; the ordering is measured here so it stays a regression test rather
than folklore.

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

Identical error surface. The difference is what survived. LangGraph is
measured twice per fault: the committed runner, which writes a verdict from
the caller after the failure, and the bare framework default
(`LOOPLAB_SKIP_VERDICT_WRITE=1`):

| | Hand-rolled | LangGraph (committed) | LangGraph (bare default) |
|---|---|---|---|
| Status in the persisted state | `failed` | `failed` | `running` |
| Reason in the persisted state | full message, e.g. `tool error in act: ...` | full message, e.g. `tool error: act: injected mid-run exception in act(s2)` | absent |
| Where it stopped | `next_node: act` | `next_nodes: ["act"]` | `next_nodes: ["act"]` |
| Resumable at the failed node | yes | yes | yes |

What is genuinely a framework property: an exception that escapes a node
commits nothing, so a node cannot record its own death, and the caller does
not own the state object and cannot simply annotate it in place. Under the
bare default, the checkpointer records where the run was, not that it died or
why; the error message lives in the process's stderr and vanishes with the
process.

What is genuinely available: `aupdate_state` from the caller. That is the
whole fix, it is two lines, the run stays resumable at the same pending node,
and the committed runner now ships it (`langgraph_impl/run.py`). So the
correct claim is **"LangGraph does not record a verdict by default and you
have to know to ask for one,"** not "LangGraph cannot record a verdict." The
default is the finding. The fix is now measured rather than merely tested.

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
Both environments are shown, because this is where the two runs actually
differ: the counts agree exactly, the wheels and the clocks do not.

| | `mcp` only | `mcp` + `langgraph` + `langgraph-checkpoint-sqlite` | Difference |
|---|---|---|---|
| Distributions installed (both platforms) | 29 | 56 | **+27** |
| `site-packages`, Linux x86_64 | 62.0 MB | 113.4 MB | **+51.5 MB** |
| `site-packages`, macOS arm64 (archived) | 46.9 MB | 75.1 MB | **+28.2 MB** |

Cold start, best of 7, warm page cache:

| | Hand-rolled | LangGraph | Difference |
|---|---|---|---|
| Import only, median — Linux container | 0.510s | 1.032s | **+0.522s** |
| Full baseline run, median — Linux container | 1.360s | 1.808s | **+0.448s** |
| Import only, median — macOS laptop (archived) | 0.319s | 0.562s | **+0.242s** |
| Full baseline run, median — macOS laptop (archived) | 0.713s | 0.980s | **+0.267s** |

The timings are the one part of this file that moves between runs; they are
transcribed from the committed `bench/results/static.json` and the archived
macOS set. The framework's cold-start tax roughly doubled from the laptop to
the container, and its disk weight nearly doubled with the platform's wheels.
The direction never moved. Everything else in this file is a count or a
yes/no and is identical across both environments.

The 27 extra distributions include `langsmith`, `requests`, `urllib3`,
`websockets`, `sqlite-vec`, `zstandard`, `xxhash`, `orjson` and `ormsgpack`.
Most of that is the LangChain observability and transport surface, which this
artifact never uses. On a CLI that runs every few minutes, a quarter of a
second is nothing. On a Lambda cold start it is not nothing.

---

## The verdict

**On the scoreboard it is close to a wash. As a decision it is not: adopt the
framework.** The first version of this file stopped at the scoreboard, and the
scoreboard has not moved — LangGraph is still not a shortcut at this size. It
cost 23 more lines than the loop it replaced (25 with docstrings stripped), 27
dependencies, and around half a second per run in the container. It did not
make crash-safety easier than 34 lines of atomic file write, and on the
measurement that matters most, kill and resume, the two implementations are
exactly tied.

What changed the verdict is the shape of the gaps, not their count. Every
sharp edge on the LangGraph side closed with configuration or a few lines in
the caller, and each fix is now committed *and measured* in this repo: the
escalation double-write is a statement ordering (finding 5, both orderings
benched), the missing failure verdict is two lines of `aupdate_state`
(finding 7, both defaults benched), the tight recursion limit is one argument
(finding 6), and the checkpoint-vs-next-step race is `durability="sync"`. The
gaps on the hand-rolled side are not like that. No runaway protection, no
human-in-the-loop that survives process death, no thread history — those are
unbuilt subsystems, plus 34 lines of atomic-write code with no upstream to fix
bugs in it. Configuration debt against construction debt is not a tie.

The other half of the argument sits in the section below this one, and it is
the half the first verdict underweighted. Everything this repo deliberately
does not measure — model bindings, tool-calling loops, streaming, fan-out,
retry and timeout policies, `error_handler`, graceful drain, alternative
checkpointer backends — is off-the-shelf on one side and roadmap on the
other. The deterministic policy that makes these numbers reproducible is also
LangGraph's worst case. The moment a model or a second concurrent branch
enters the loop, one side imports the machinery and the other side builds it.

The three sharp edges are still real, still defensible design consequences,
and still able to surprise someone in production; they are just closable, and
closed here. The most useful finding is unchanged by any of this: both
implementations were broken in exactly the same way by the MCP client's
`ExceptionGroup` wrapping, and the run record, the one artifact this whole
exercise is about making durable, was the one piece of state neither
checkpointer protected. **Durability is a property of a system, not of an
orchestrator.** Adopting a framework moves the boundary of what is handled
for you. It does not remove the need to know where that boundary is.

The full decision record, including what "hardened" means line by line, is in
[DECISION.md](DECISION.md). The version of this loop that should worry you is
still the one where a tripwire fires, a human approves it, and nobody ever
checks whether the approval got written down twice.

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
- **Single machine, single process, warm cache.** Two OSes now (a macOS laptop
  and a Linux container), but no cold Lambda and no network filesystem. The
  cold-start numbers are best-of-7 in each environment.
- **The kill tests use two specific kill points.** They are the two that seemed
  most informative. They are not a proof of crash safety, and no fuzzing over
  kill timing was done.
- **Time-travel and forking were not exercised**, despite being cited above as
  the justification for the SQLite store's size. That justification is from
  LangGraph's documented feature set, not from a measurement in this repo.
