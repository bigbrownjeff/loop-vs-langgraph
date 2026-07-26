# loop-vs-langgraph

**The same agent operating loop, built twice: once by hand, once on LangGraph, both driving one shared MCP tool layer. The scoreboard says close to a wash. The decision, after measuring everything twice, is to adopt LangGraph — and the reasoning is the interesting part.** The LangGraph version is 32 lines longer (34 with docstrings stripped), pulls in 27 extra dependencies, and adds about half a second to every run in a container. On crash-safe checkpoint and resume, the measurement people actually care about, the two are exactly tied: both recover byte-identical state under a `kill -9`, and both re-execute the in-flight step exactly once. But the gaps are not symmetric. Every sharp edge on the LangGraph side closed with configuration or a few lines in the caller — each fix committed here and *measured*, with the original footgun reproducible behind a flag. Every gap on the hand-rolled side is an unbuilt subsystem: no runaway guard, no human-in-the-loop that survives process death, no thread history. The full decision record is **[DECISION.md](DECISION.md)**. The most useful finding still belongs to neither framework: the durable run record, the one artifact this was all about protecting, was the one piece of state that neither checkpointer protected, because it lived in the tool layer.

Every count and yes/no finding reproduced identically across two environments
(macOS arm64 / Python 3.14 and a Linux x86_64 container / Python 3.11); only
wall-clock timings moved. The first environment's raw JSON is archived in
`bench/results-archive/`.

Full measurements, with the command that produced each one: **[RESULTS.md](RESULTS.md)**.
Live comparison: **https://loop-lab.pages.dev** (custom domain `loop-lab.jeffpinto.com` pending a DNS record)

---

## What is being ported

A written operating loop that a set of production agents run: orient, plan, act,
verify, record, decide, with a hard verify gate, named exit conditions, and
human-escalation tripwires for irreversible actions and uncapped spend. It is
reproduced verbatim in [`LOOP.md`](LOOP.md).

```
        orient ──▶ plan ──▶ decide ──┬──▶ act ──▶ verify ──▶ record ──┐
                                     │                                │
                                     │            ◀───────────────────┘
                                     │
                                     ├──▶ END (done: every criterion met and gated)
                                     └──▶ HALT (a tripwire fired: ask a human)
```

Tripwires, evaluated before every step:

- **irreversible action** ("publish the release to the public index")
- **spend beyond the declared cap** (est. cost + spent > cap)
- **repeated verification failure** (the gate failed 3 times with no new hypothesis)

Four scenarios exercise all three plus the happy path, including one step that
fails its verify gate once and passes on retry.

## The point of the artifact

The tools are exposed over **MCP**, not as Python functions, and neither loop
imports the tool implementations. Both spawn `tools/mcp_server.py` over stdio and
call it through `tools/mcp_client.py`. Swapping the orchestrator does not touch
the tools.

Tool names, descriptions, input schemas and output schemas are loaded verbatim
from one frozen [`tools/tool-defs.json`](tools/tool-defs.json). The low-level MCP
server validates every call's arguments against `inputSchema` before a handler
runs, and every structured result against `outputSchema` before it goes back on
the wire. There is no second copy of a schema to drift.

That contract layer is what caught the injected schema violation in both
implementations, identically, before either loop saw it.

## Layout

```
tools/                     the shared layer, identical for both loops
  tool-defs.json           the frozen contract: 5 tools, input + output schemas
  mcp_server.py            schema-validated MCP server, stdio and Streamable HTTP
  mcp_client.py            the only way either loop reaches a tool
  policy.py                the loop's decision rules, shared verbatim
  world.py                 the synthetic task world, plus server-side fault injection
  fault.py                 aimable process death, for the durability tests

handrolled/                no framework
  loop.py                  six nodes, a while loop, a checkpoint after every node
  atomic_io.py             temp -> fsync -> os.replace, plus the directory fsync
  run.py                   CLI

langgraph_impl/            LangGraph 1.2.9, hardened (see DECISION.md)
  graph.py                 the same six nodes as a StateGraph, interrupt() for escalation;
                           interrupt-first ordering, footgun behind LOOPLAB_ESCALATION_ORDER
  run.py                   CLI, AsyncSqliteSaver checkpointer, durability="sync",
                           caller-side verdict write, bare default behind LOOPLAB_SKIP_VERDICT_WRITE

bench/                     every number in RESULTS.md
  parity.py                do both implementations actually do the same thing
  kill_test.py             two kill points, real SIGKILL, resume, diff
  failure_modes.py         schema violation, timeout, mid-run exception
  escalation_test.py       tripwire fires, a separate process approves, run continues
  wal_test.py              copy the checkpoint file alone and try to resume
  static_measure.py        lines of code, dependency weight, cold start
  run_all.sh               all of the above
```

## Run it

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# one run, either implementation
LOOPLAB_STATE_DIR=runs/demo .venv/bin/python handrolled/run.py     --scenario baseline --run-dir runs/demo
LOOPLAB_STATE_DIR=runs/demo .venv/bin/python langgraph_impl/run.py --scenario baseline --run-dir runs/demo

# watch a tripwire fire, then answer it from a separate process
LOOPLAB_STATE_DIR=runs/esc .venv/bin/python handrolled/run.py --scenario irreversible --run-dir runs/esc
LOOPLAB_STATE_DIR=runs/esc .venv/bin/python handrolled/run.py --scenario irreversible --run-dir runs/esc --resume --approve yes

# every measurement in RESULTS.md
./bench/run_all.sh
```

Scenarios: `baseline`, `irreversible`, `overspend`, `verify_stuck`.

The MCP server also runs standalone over Streamable HTTP if you want to point
your own client at it:

```
.venv/bin/python tools/mcp_server.py --transport http   # 127.0.0.1:8850/mcp
```

## Honesty notes

- **There is no LLM in this repository.** The policy is deterministic, by design,
  because a stochastic policy would make every measurement unreproducible. It
  also means nothing here evaluates LangGraph's model bindings, message state, or
  agent prebuilts, which is most of what people use LangGraph for.
- All data is synthetic and lives in `tools/world.py`.
- The full list of what this does not measure is at the end of
  [RESULTS.md](RESULTS.md), and it is longer than the list of findings.

## Licence

MIT. See [LICENSE](LICENSE).
