"""The same operating loop, on LangGraph.

Same six nodes, same shared policy module, same MCP tool layer. What changes:
LangGraph owns the state object, the edges, and the persistence.

Two LangGraph-specific choices worth naming, because they are where the port
stopped being mechanical:

1. Nodes return a PARTIAL state dict which LangGraph merges, rather than
   mutating state in place. Any nested dict (`attempts`, `artifacts`) has to be
   copied and returned whole, because the default channel reducer overwrites
   rather than merges.

2. Escalation uses `interrupt()`, which is LangGraph's human-in-the-loop
   primitive, instead of returning a terminal status. See RESULTS.md for what
   that buys and what it costs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import policy
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from fault import maybe_die
from mcp_client import Tools

# The open MCP session for the current process. LangGraph nodes receive
# (state, config); a live network session is not serialisable into either, so
# it lives beside the graph rather than inside it.
_TOOLS: Tools | None = None


def set_tools(tools: Tools) -> None:
    global _TOOLS
    _TOOLS = tools


def _t() -> Tools:
    if _TOOLS is None:
        raise RuntimeError("MCP tools not bound; call set_tools() before invoking the graph")
    return _TOOLS


def _append(xs: list[Any], x: Any) -> list[Any]:
    return list(xs) + [x]


class LoopState(TypedDict, total=False):
    run_id: str
    scenario: str
    objective: str
    success_criteria: list[str]
    budget_cap_usd: float
    blast_radius: str
    steps: list[dict[str, Any]]
    cursor: int
    spent_usd: float
    attempts: dict[str, int]
    artifacts: dict[str, str]
    verified: list[str]
    records: int
    status: str
    exit_reason: str | None
    escalation: dict[str, Any] | None
    approved: list[str]
    history: Annotated[list[str], lambda a, b: b]
    pending: dict[str, Any] | None


def _mark(node: str) -> None:
    print(f"MARK {node} {time.time():.6f}", flush=True)


async def orient(state: LoopState) -> dict[str, Any]:
    _mark("orient")
    res = await _t().call(
        "orient", {"objective_id": state["run_id"], "scenario": state["scenario"]}
    )
    out = {
        "objective": res["objective"],
        "success_criteria": res["success_criteria"],
        "budget_cap_usd": res["budget_cap_usd"],
        "blast_radius": res["blast_radius"],
        "history": _append(state["history"], "orient"),
    }
    maybe_die("orient")
    return out


async def plan(state: LoopState) -> dict[str, Any]:
    _mark("plan")
    res = await _t().call("plan", {"objective_id": state["run_id"], "scenario": state["scenario"]})
    out = {"steps": res["steps"], "history": _append(state["history"], "plan")}
    maybe_die("plan")
    return out


async def act(state: LoopState) -> dict[str, Any]:
    _mark("act")
    step_id = state["pending"]["step_id"]
    attempt = state["pending"]["attempt"]
    res = await _t().call(
        "act",
        {
            "objective_id": state["run_id"],
            "scenario": state["scenario"],
            "step_id": step_id,
            "attempt": attempt,
        },
    )
    out = {
        "artifacts": {**state["artifacts"], step_id: res["artifact"]},
        "spent_usd": state["spent_usd"] + res["cost_usd"],
        "attempts": {**state["attempts"], step_id: attempt},
        "history": _append(state["history"], f"act:{step_id}#{attempt}"),
    }
    maybe_die("act")
    return out


async def verify(state: LoopState) -> dict[str, Any]:
    _mark("verify")
    step_id = state["pending"]["step_id"]
    attempt = state["pending"]["attempt"]
    res = await _t().call(
        "verify",
        {
            "objective_id": state["run_id"],
            "scenario": state["scenario"],
            "step_id": step_id,
            "artifact": state["artifacts"][step_id],
            "attempt": attempt,
        },
    )
    if res["passed"]:
        out = {
            "verified": _append(state["verified"], step_id),
            "cursor": state["cursor"] + 1,
            "history": _append(state["history"], f"verify:{step_id}:pass"),
        }
    else:
        out = {"history": _append(state["history"], f"verify:{step_id}:fail")}
    maybe_die("verify")
    return out


async def record(state: LoopState) -> dict[str, Any]:
    _mark("record")
    last = state["history"][-1] if state["history"] else "start"
    res = await _t().call("record", {"run_id": state["run_id"], "kind": "act", "detail": last})
    out = {"records": res["seq"], "history": _append(state["history"], "record")}
    maybe_die("record")
    return out


async def decide(state: LoopState) -> dict[str, Any]:
    _mark("decide")
    action, payload = policy.next_action(dict(state))
    hist = _append(state["history"], f"decide:{action}")

    if action == "act":
        return {"pending": payload, "history": hist, "status": "running"}

    if action == "escalate":
        record_args = {
            "run_id": state["run_id"],
            "kind": "escalate",
            "detail": "; ".join(payload["tripwire"]),
        }
        # Resuming an interrupt re-executes this whole node from its first
        # line, so anything above the interrupt() call must be idempotent.
        # The record call is not: with it above, the escalation lands in the
        # durable run record twice. Both orderings are measured
        # (bench/escalation_test.py, RESULTS.md finding 5); the footgun
        # ordering stays reproducible behind LOOPLAB_ESCALATION_ORDER.
        record_first = os.environ.get("LOOPLAB_ESCALATION_ORDER") == "record_first"
        if record_first:
            await _t().call("record", record_args)
        # LangGraph's human-in-the-loop primitive. Raises GraphInterrupt, which
        # the checkpointer persists; the run can only continue via
        # Command(resume=...). This is the closest thing to LOOP.md's
        # "stop and ask".
        decision = interrupt(
            {
                "reason": payload["exit_reason"],
                "tripwire": payload["tripwire"],
                "step_id": payload["step_id"],
            }
        )
        # Only reached if a human resumes with Command(resume=...).
        if not record_first:
            await _t().call("record", record_args)
        return {
            "approved": _append(state.get("approved", []), payload["step_id"]),
            "status": "running",
            "escalation": None,
            "history": _append(hist, f"human:{decision}"),
        }

    await _t().call(
        "record", {"run_id": state["run_id"], "kind": "exit", "detail": payload["exit_reason"]}
    )
    return {
        "status": "done",
        "exit_reason": payload["exit_reason"],
        "history": hist,
        "records": state["records"] + 1,
    }


def _route(state: LoopState) -> str:
    last = state["history"][-1]
    if last == "decide:act":
        return "act"
    if last.startswith("human:"):
        return "decide"  # re-decide now that the tripwire is answered
    return END


def build_graph():
    g = StateGraph(LoopState)
    g.add_node("orient", orient)
    g.add_node("plan", plan)
    g.add_node("act", act)
    g.add_node("verify", verify)
    g.add_node("record", record)
    g.add_node("decide", decide)

    g.add_edge(START, "orient")
    g.add_edge("orient", "plan")
    g.add_edge("plan", "decide")
    g.add_edge("act", "verify")
    g.add_edge("verify", "record")
    g.add_edge("record", "decide")
    g.add_conditional_edges("decide", _route, {"act": "act", "decide": "decide", END: END})
    return g


def initial_state(run_id: str, scenario: str) -> dict[str, Any]:
    s = policy.initial_state(run_id, scenario)
    s["pending"] = None
    return s
