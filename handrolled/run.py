"""CLI for the hand-rolled loop.

  python handrolled/run.py --scenario baseline --run-dir runs/hr-baseline
  python handrolled/run.py --scenario baseline --run-dir runs/hr-baseline --resume
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import policy
from loop import load_or_init, run
from mcp_client import ToolError, ToolTimeout, first_leaf, open_tools


async def main_async(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    state = load_or_init(run_dir, args.run_id, args.scenario, args.resume)
    if args.approve and state.get("status") == "escalated":
        # The human answered the tripwire. Clear it and re-enter at decide.
        state.setdefault("approved", []).append(state["escalation"]["step_id"])
        state["history"].append(f"human:{args.approve}")
        state["status"] = "running"
        state["escalation"] = None
        state["exit_reason"] = None
        state["next_node"] = "decide"
    exit_code = 0
    try:
        async with open_tools() as tools:
            print(f"MARK tools-ready {time.time():.6f}", flush=True)
            state = await run(state, tools, run_dir)
    except BaseException as exc:  # noqa: BLE001 - unwrapped and re-classified below
        leaf = first_leaf(exc)
        if not isinstance(leaf, (ToolError, ToolTimeout)):
            raise
        print(f"FAILED {type(leaf).__name__}: {leaf}", file=sys.stderr, flush=True)
        exit_code = 3
    elapsed = time.perf_counter() - t0
    digest = policy.state_digest(state)
    digest["resumed_count"] = state.get("resumed_count", 0)
    (run_dir / "digest.json").write_text(json.dumps(digest, indent=2, sort_keys=True))
    print(f"WALL_S {elapsed:.4f}", flush=True)
    print("DIGEST " + json.dumps(digest, sort_keys=True), flush=True)
    return exit_code


def main() -> None:
    p = argparse.ArgumentParser(description="hand-rolled operating loop")
    p.add_argument("--scenario", default="baseline",
                   choices=["baseline", "irreversible", "overspend", "verify_stuck"])
    p.add_argument("--run-dir", required=True)
    p.add_argument("--run-id", default="rel-audit-1")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--approve", default=None,
                   help="answer a fired tripwire and continue (use with --resume)")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
