# Handoff — porting the operating loop to LangGraph, over MCP, with measurements

**Date:** 2026-07-25 (overnight, ~22:30 to ~03:00 EDT) · **Track:** employment gap-closer
**Brief:** `~/Projects/.claude/job-workups/2026-07-24-hubsync/LANGGRAPH-BUILD-BRIEF.md`
**Assignment:** `GAP-CLOSING-PLAN.md` §3b, the highest-leverage item on the page.

## What this was for

An adversarial recruiter review concluded Jeff's single biggest gap-closer in the
"AI-forward staff engineer" lane is one public LangGraph artifact: he has built the
primitives LangGraph packages (versioned agent state machine, crash-safe checkpoint/resume,
durable run records, human-escalation tripwires, a schema-validated MCP server) and has zero
LangGraph or LangChain anywhere. The deliverable is an honest engineering comparison, not a
marketing win for either side.

## What shipped

| Thing | Where | State |
|---|---|---|
| Public repo, MIT | https://github.com/bigbrownjeff/loop-vs-langgraph | **live**, 4 commits on `main` |
| Demo page | https://loop-lab.pages.dev | **live**, verified 200 + browser at 390px |
| Custom domain | `loop-lab.jeffpinto.com` | **BLOCKED on Jeff**, see below |
| Note (draft) | PR https://github.com/bigbrownjeff/jeffpinto-site/pull/206 | **open**, `isDraft: true`, publishes nowhere |
| Links registry | `~/.claude/links/registry.json`, id `loop-lab` | updated, custom domain marked `todo:true` |
| Local repo | `~/Projects/loop-vs-langgraph` | clean, pushed |

## The one thing Jeff must do

**Add a DNS record.** The Pages custom domain is attached to the project but stuck at
`pending` because the CNAME does not exist:

```
CNAME  loop-lab  ->  loop-lab.pages.dev     (zone jeffpinto.com, proxied, same as mlb/rvc-taxes)
```

I attached the domain via the Pages API (that part succeeded) but could not create the DNS
record: wrangler's stored OAuth token has no DNS scope, and DNS is on the hands-on-for-Jeff
list in `CLAUDE.md` regardless. Once the CNAME exists the cert should issue on its own; then
flip `todo:true` to a verified note in the registry and update the README + note links from
`loop-lab.pages.dev` to `loop-lab.jeffpinto.com`. Everything works today on the pages.dev URL,
so this is cosmetic, not blocking.

## The headline finding

> **Superseded 2026-07-26:** the scoreboard below is the pre-hardening measurement; the accepted decision is to adopt LangGraph — see [DECISION.md](DECISION.md).

**It is close to a wash, and the ways it is not are not the ways you would guess.** LangGraph
came out 23 lines longer (25 with docstrings stripped), +27 distributions, +28.2 MB,
+0.267s per run. On crash-safe checkpoint and resume, the thing people adopt it for, the two
implementations are **exactly tied**: both recover byte-identical state at both kill points,
both re-execute the in-flight step exactly once.

The most useful finding belongs to neither framework: **the durable run record was the one
piece of state that neither checkpointer protected**, because it lived in the MCP tool layer,
outside both graphs. Durability is a property of a system, not of an orchestrator.

Full writeup: `RESULTS.md`. Raw JSON: `bench/results/*.json`.

## What worked, and why

- **One shared MCP tool layer that neither loop may bypass.** `tools/mcp_server.py` +
  `tools/mcp_client.py`; neither implementation imports `world.py`. This is what makes the
  comparison mean anything, and it is also the artifact's actual thesis (framework-portable
  tool contracts). Contract style copied from the Mattel `mcp-skeleton/server.py`: schemas
  loaded verbatim from one frozen `tool-defs.json`, SDK enforces both directions.
- **A shared `policy.py`.** Both loops use identical decision rules, so the comparison
  isolates the framework rather than measuring two translations of `LOOP.md`.
- **`bench/parity.py` as the foundation.** Runs all 4 scenarios on both and diffs digests.
  Without it every other number would be uninterpretable.
- **Server-side tool-call log** (`tool-calls.jsonl`, written by the MCP server with `fsync`).
  Ground truth for "what actually got called" that neither loop can edit. This is what
  caught the duplicate escalation record.
- **Two kill points, not one.** The external SIGKILL mid-tool-call is the boring case.
  `tools/fault.py` fires `os._exit(137)` at the end of a named node, after the side effect
  and before the commit, in the same place in both. That is the window that costs money.
- **Generating the demo page from the results JSON** (`demo/build.py`). A figure on the page
  cannot drift from its measurement. Proven useful: the hand-written `RESULTS.md` timings
  DID drift when I re-ran `static_measure.py`; the page did not.

## Dead ends and corrections

- **First parity run failed** with `records: 5` vs `6` on every kill test. I briefly thought
  this was a checkpointer finding. It was my own bug: the run-record sequence counter was a
  module-level dict in the server process, and resume spawns a new process. Fixed with a
  persisted counter. Kept as finding 8 because the lesson is real.
- **Both loops silently failed to catch their own exception type.** The MCP stdio client runs
  its transport in an anyio task group, so `ToolError` comes out wrapped in an
  `ExceptionGroup`. Both died with a raw traceback and the wrong exit code on all 3 injected
  faults until `first_leaf()` was added. Framework-independent.
- **Screenshot rabbit hole.** Headless Chrome on this Mac clamps its layout viewport to
  **485px minimum**; `--window-size=390` produces a 390px-wide *crop* of a 485px layout,
  which looks exactly like a broken mobile page. Wasted ~20 minutes "fixing" a bug that did
  not exist. **The reliable trick: load the page in a same-origin `<iframe width="390">`**
  inside a wider host page and screenshot that. `scrollWidth == clientWidth` confirmed zero
  overflow. Worth remembering for every future mobile verification.
- **Adversarial pass caught two numbers flattering Jeff**, both corrected in place rather
  than dropped:
  1. LOC included docstrings and I had written a longer one on the LangGraph side. Added a
     docstring-stripped count (`ast`-based). Direction survived: 23 vs 25.
  2. "LangGraph does not persist why a run died" overstated a framework difference. Most of
     the gap was my own asymmetry (the hand-rolled driver owns the state dict and writes a
     verdict; I never wrote the LangGraph equivalent). Tested `aupdate_state`: 2 lines,
     records the verdict, stays resumable. Narrowed the claim to "not by default."

## Key decisions

- **No LLM anywhere in the repo.** Deterministic policy. A stochastic one would make every
  measurement unreproducible. Cost: says nothing about LangGraph's model bindings or agent
  prebuilts, which is most of what people use it for. Stated loudly in three places.
- **`AsyncSqliteSaver`, not Postgres.** Single backend, named as a limitation. The WAL finding
  is specific to this backend.
- **Repo trimmed before publishing.** `LOOP.md` ships with only the Operating Loop section;
  the launchpad-lane, fast-lane and usage-guardrail sections are estate-internal and were cut
  (noted in the file). Private paths in docstrings genericized. No Meta lineage anywhere.
- **Kept the duplicate-escalation footgun in the committed code** rather than fixing it, so
  the measurement reproduces. The verified mitigation is documented instead.

## Reproduce everything

```bash
cd ~/Projects/loop-vs-langgraph
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./bench/run_all.sh                    # ~2 min; writes bench/results/*.json
.venv/bin/python demo/build.py        # regenerate the page from those results

# one run, either implementation
LOOPLAB_STATE_DIR=runs/d .venv/bin/python handrolled/run.py     --scenario baseline --run-dir runs/d
LOOPLAB_STATE_DIR=runs/d .venv/bin/python langgraph_impl/run.py --scenario baseline --run-dir runs/d

# tripwire, then approve from a separate process
LOOPLAB_STATE_DIR=runs/e .venv/bin/python handrolled/run.py --scenario irreversible --run-dir runs/e
LOOPLAB_STATE_DIR=runs/e .venv/bin/python handrolled/run.py --scenario irreversible --run-dir runs/e --resume --approve yes

# redeploy the page (from INSIDE demo/public, per the wrangler CWD gotcha)
cd demo/public && wrangler pages deploy . --project-name loop-lab --branch main --commit-dirty=true
curl -sI "https://loop-lab.pages.dev/?cb=$RANDOM" | head -1
```

Scenarios: `baseline`, `irreversible`, `overspend`, `verify_stuck`.
Fault injection: `LOOPLAB_FAULT=schema_violation|hang|crash`, `LOOPLAB_DIE_AFTER_NODE=act#2`,
`LOOPLAB_SLOW_STEP=s2`, `LOOPLAB_TOOL_TIMEOUT_S=2`.

## Open threads

1. **DNS CNAME** (above). The only blocking item, and it is Jeff's by policy.
2. **The note needs a banner image** before it publishes. Art-director job, `gen-note-image.sh`.
3. **Note review.** PR #206. Worth Jeff's eye on: the title, whether the job-hunting framing
   in paragraph 3 stays (it is honest but it dates the note), and `related:` (only
   `versioned-spreadsheet` right now).
4. **The repo has zero stars, like every other repo Jeff owns.** Per GAP-CLOSING-PLAN §5,
   distribution is the quieter gap. This artifact is the kind of thing that travels if it is
   posted; marketing-lead should sequence it. It is also a natural companion to the Presidio
   PR (§5.1) as third-party-verified evidence.
5. **Python version.** Measured on 3.14.6, which is what this Mac has; the brief asked for
   3.11 syntax and the code is 3.11-compatible, but no 3.11 interpreter exists locally to
   verify against. Worth a CI matrix if this repo ever gets one.
6. **Untested and named as such:** concurrency/fan-out, Postgres checkpointer, LangSmith,
   streaming, time travel (cited as justification for the SQLite store's size but never
   exercised).

## What I would tell the next agent

The state machine port was the easy half and took a couple of hours. Everything valuable in
this repo came from the seams: the MCP client's exception wrapping, the sequence counter that
lived outside both checkpointers, the WAL sidecar, the node that re-runs above an interrupt.
If you extend this, do not add a third framework. Add another seam.
