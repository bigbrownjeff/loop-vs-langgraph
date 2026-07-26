"""CLI for the LangGraph loop.

  python langgraph_impl/run.py --scenario baseline --run-dir runs/lg-baseline
  python langgraph_impl/run.py --scenario baseline --run-dir runs/lg-baseline --resume

Resume semantics differ from the hand-rolled loop and that difference is the
point. LangGraph resumes a *thread*: you re-invoke the compiled graph with the
same thread_id and `None` as the input, and it replays from the last committed
checkpoint. There is no state document for the caller to load.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import policy
from graph import build_graph, initial_state, set_tools
from mcp_client import ToolError, ToolTimeout, first_leaf, open_tools

# Measured: the baseline scenario needs 24 super-steps and LangGraph's default
# recursion_limit is 25. It finishes with one to spare. One more verify retry
# (4 more super-steps) would have died with GraphRecursionError. Set it
# explicitly. See RESULTS.md, finding 6.
RECURSION_LIMIT = 200


async def main_async(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "checkpoints.sqlite"

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    exit_code = 0
    digest: dict = {}
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        app = build_graph().compile(checkpointer=saver)
        config = {
            "configurable": {"thread_id": args.run_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        try:
            async with open_tools() as tools:
                set_tools(tools)
                print(f"MARK tools-ready {time.time():.6f}", flush=True)
                if args.approve:
                    from langgraph.types import Command

                    payload = Command(resume=args.approve)
                elif args.resume:
                    payload = None
                else:
                    payload = initial_state(args.run_id, args.scenario)
                # durability="sync": persist each checkpoint before the next
                # super-step starts. The default is "async", which overlaps
                # the write with the next step and reopens exactly the crash
                # window kill point B aims at. Sync is the production setting;
                # the kill tests run against this code path.
                await app.ainvoke(payload, config=config, durability="sync")
        except BaseException as exc:  # noqa: BLE001 - unwrapped and re-classified below
            leaf = first_leaf(exc)
            print(f"FAILED {type(leaf).__name__}: {leaf}", file=sys.stderr, flush=True)
            exit_code = 3 if isinstance(leaf, (ToolError, ToolTimeout)) else 4
            # The framework persists position, not verdict: an exception that
            # escapes a node commits nothing, so after a crash the checkpoint
            # says `running` with a pending node and the reason dies with the
            # process (RESULTS.md finding 7). These lines are the caller-side
            # fix: write the verdict into the thread, which stays resumable at
            # the same pending node. The bare default stays measurable behind
            # LOOPLAB_SKIP_VERDICT_WRITE (bench/failure_modes.py runs both).
            if not os.environ.get("LOOPLAB_SKIP_VERDICT_WRITE"):
                kind = "tool timeout" if isinstance(leaf, ToolTimeout) else "tool error"
                await app.aupdate_state(
                    config, {"status": "failed", "exit_reason": f"{kind}: {leaf}"}
                )

        # Read final state back out of the checkpointer, not out of the return
        # value, so a crashed run and a clean run are read the same way.
        snap = await app.aget_state(config)
        state = dict(snap.values) if snap and snap.values else initial_state(args.run_id, args.scenario)
        interrupts = [
            {"value": i.value} for t in (snap.tasks or ()) for i in (t.interrupts or ())
        ] if snap else []
        if interrupts:
            state["status"] = "escalated"
            first = interrupts[0]["value"]
            state["exit_reason"] = first.get("reason")
            state["escalation"] = {
                "step_id": first.get("step_id"),
                "tripwire": first.get("tripwire"),
            }
        digest = policy.state_digest(state)
        digest["resumed_count"] = 1 if args.resume else 0
        digest["next_nodes"] = list(snap.next) if snap else []

    elapsed = time.perf_counter() - t0
    (run_dir / "digest.json").write_text(json.dumps(digest, indent=2, sort_keys=True))
    print(f"WALL_S {elapsed:.4f}", flush=True)
    print("DIGEST " + json.dumps(digest, sort_keys=True), flush=True)
    return exit_code


def main() -> None:
    p = argparse.ArgumentParser(description="LangGraph operating loop")
    p.add_argument("--scenario", default="baseline",
                   choices=["baseline", "irreversible", "overspend", "verify_stuck"])
    p.add_argument("--run-dir", required=True)
    p.add_argument("--run-id", default="rel-audit-1")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--approve", default=None,
                   help="answer a fired tripwire and continue (Command(resume=...))")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
