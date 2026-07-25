"""The hand-rolled operating loop: orient, plan, act, verify, record, decide.

A direct port of LOOP.md (reproduced in this repo) into code. No framework. The
whole state machine is the `NODES` table plus the `run` driver below.

Persistence model: the full run state is one JSON document, rewritten
atomically after every node returns. Resume reads it back and re-enters the
loop at `state["next_node"]`. There is no partial-write window: either the
previous checkpoint is on disk intact, or the new one is.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import policy
from atomic_io import read_json, write_json_atomic
from fault import maybe_die
from mcp_client import ToolError, ToolTimeout, Tools

CHECKPOINT_NAME = "state.json"


def _mark(node: str) -> None:
    """Progress marker on stdout, so the kill harness can time a signal."""
    print(f"MARK {node} {time.time():.6f}", flush=True)


async def node_orient(state: dict[str, Any], tools: Tools) -> str:
    res = await tools.call(
        "orient", {"objective_id": state["run_id"], "scenario": state["scenario"]}
    )
    state["objective"] = res["objective"]
    state["success_criteria"] = res["success_criteria"]
    state["budget_cap_usd"] = res["budget_cap_usd"]
    state["blast_radius"] = res["blast_radius"]
    state["history"].append("orient")
    maybe_die("orient")
    return "plan"


async def node_plan(state: dict[str, Any], tools: Tools) -> str:
    res = await tools.call(
        "plan", {"objective_id": state["run_id"], "scenario": state["scenario"]}
    )
    state["steps"] = res["steps"]
    state["history"].append("plan")
    maybe_die("plan")
    return "decide"


async def node_act(state: dict[str, Any], tools: Tools) -> str:
    step_id = state["pending"]["step_id"]
    attempt = state["pending"]["attempt"]
    res = await tools.call(
        "act",
        {
            "objective_id": state["run_id"],
            "scenario": state["scenario"],
            "step_id": step_id,
            "attempt": attempt,
        },
    )
    state["artifacts"][step_id] = res["artifact"]
    state["spent_usd"] += res["cost_usd"]
    state["attempts"][step_id] = attempt
    state["history"].append(f"act:{step_id}#{attempt}")
    maybe_die("act")
    return "verify"


async def node_verify(state: dict[str, Any], tools: Tools) -> str:
    step_id = state["pending"]["step_id"]
    attempt = state["pending"]["attempt"]
    res = await tools.call(
        "verify",
        {
            "objective_id": state["run_id"],
            "scenario": state["scenario"],
            "step_id": step_id,
            "artifact": state["artifacts"][step_id],
            "attempt": attempt,
        },
    )
    # The hard gate: advance the cursor only on a pass. Never on unverified output.
    if res["passed"]:
        state["verified"].append(step_id)
        state["cursor"] += 1
        state["history"].append(f"verify:{step_id}:pass")
    else:
        state["history"].append(f"verify:{step_id}:fail")
    maybe_die("verify")
    return "record"


async def node_record(state: dict[str, Any], tools: Tools) -> str:
    last = state["history"][-1] if state["history"] else "start"
    res = await tools.call(
        "record", {"run_id": state["run_id"], "kind": "act", "detail": last}
    )
    state["records"] = res["seq"]
    state["history"].append("record")
    maybe_die("record")
    return "decide"


async def node_decide(state: dict[str, Any], tools: Tools) -> str:
    action, payload = policy.next_action(state)
    state["history"].append(f"decide:{action}")
    if action == "act":
        state["pending"] = payload
        return "act"
    if action == "escalate":
        state["status"] = "escalated"
        state["exit_reason"] = payload["exit_reason"]
        state["escalation"] = {"step_id": payload["step_id"], "tripwire": payload["tripwire"]}
        await tools.call(
            "record",
            {
                "run_id": state["run_id"],
                "kind": "escalate",
                "detail": "; ".join(payload["tripwire"]),
            },
        )
        state["records"] += 1
        return "END"
    state["status"] = "done"
    state["exit_reason"] = payload["exit_reason"]
    await tools.call(
        "record", {"run_id": state["run_id"], "kind": "exit", "detail": payload["exit_reason"]}
    )
    state["records"] += 1
    return "END"


NODES = {
    "orient": node_orient,
    "plan": node_plan,
    "act": node_act,
    "verify": node_verify,
    "record": node_record,
    "decide": node_decide,
}


def load_or_init(run_dir: Path, run_id: str, scenario: str, resume: bool) -> dict[str, Any]:
    ckpt = run_dir / CHECKPOINT_NAME
    if resume:
        loaded = read_json(ckpt)
        if loaded is not None:
            loaded["resumed_count"] = loaded.get("resumed_count", 0) + 1
            return loaded
    state = policy.initial_state(run_id, scenario)
    state["next_node"] = "orient"
    state["pending"] = None
    state["resumed_count"] = 0
    return state


async def run(state: dict[str, Any], tools: Tools, run_dir: Path) -> dict[str, Any]:
    ckpt = run_dir / CHECKPOINT_NAME
    write_json_atomic(ckpt, state)
    while state["next_node"] != "END":
        node = state["next_node"]
        _mark(node)
        try:
            nxt = await NODES[node](state, tools)
        except ToolTimeout as exc:
            state["status"] = "failed"
            state["exit_reason"] = f"tool timeout in {node}: {exc}"
            write_json_atomic(ckpt, state)
            raise
        except ToolError as exc:
            state["status"] = "failed"
            state["exit_reason"] = f"tool error in {node}: {exc}"
            write_json_atomic(ckpt, state)
            raise
        state["next_node"] = nxt
        # Checkpoint after every node. This is the whole durability story.
        write_json_atomic(ckpt, state)
    return state
