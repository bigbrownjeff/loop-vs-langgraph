"""The loop's decision rules, shared verbatim by both implementations.

This is a deliberate methodological choice. If each implementation had its own
copy of the routing and tripwire logic, the comparison would measure my two
translations of LOOP.md rather than the two frameworks. Everything here is a
pure function over the run state. What differs between the implementations is
only how state is held, how nodes are wired, and how the run is persisted.

The rules are a direct transcription of LOOP.md, reproduced in this repo.
"""

from __future__ import annotations

from typing import Any

MAX_VERIFY_ATTEMPTS = 3


def tripwires(step: dict[str, Any], spent_usd: float, budget_cap_usd: float) -> list[str]:
    """LOOP.md's human-escalation tripwires, evaluated BEFORE a step runs.

    Only the two the brief names are implemented: irreversible action, and
    spend that would breach the declared cap. The others in LOOP.md (authority
    required, ambiguous intent, ungroundable public claim) need a human or a
    model in the loop and are out of scope for a deterministic harness.
    """
    fired: list[str] = []
    if not step["reversible"]:
        fired.append(f"irreversible action: {step['title']}")
    if spent_usd + step["est_cost_usd"] > budget_cap_usd:
        fired.append(
            f"spend beyond cap: ${spent_usd:.2f} spent + ${step['est_cost_usd']:.2f} "
            f"est > ${budget_cap_usd:.2f} cap"
        )
    return fired


def verify_exhausted(attempts: int) -> bool:
    """LOOP.md: 'Verification fails ~2-3 times with no new hypothesis.'"""
    return attempts >= MAX_VERIFY_ATTEMPTS


def next_action(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The `decide` node. Returns (action, payload).

    action is one of: "act", "escalate", "done".
    """
    steps: list[dict[str, Any]] = state["steps"]
    cursor: int = state["cursor"]

    if cursor >= len(steps):
        return "done", {"exit_reason": "done: all success criteria met and gated"}

    step = steps[cursor]
    attempts = state["attempts"].get(step["id"], 0)

    if verify_exhausted(attempts):
        return "escalate", {
            "exit_reason": "blocked: verification failed the maximum number of times",
            "tripwire": [f"verify gate failed {attempts}x on {step['id']} with no new hypothesis"],
            "step_id": step["id"],
        }

    fired = tripwires(step, state["spent_usd"], state["budget_cap_usd"])
    if fired and step["id"] in state.get("approved", []):
        fired = []  # a human already said yes to this exact step
    if fired:
        return "escalate", {
            "exit_reason": "blocked: human-escalation tripwire fired",
            "tripwire": fired,
            "step_id": step["id"],
        }

    return "act", {"step_id": step["id"], "attempt": attempts + 1}


def initial_state(run_id: str, scenario: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "scenario": scenario,
        "objective": "",
        "success_criteria": [],
        "budget_cap_usd": 0.0,
        "blast_radius": "",
        "steps": [],
        "cursor": 0,
        "spent_usd": 0.0,
        "attempts": {},
        "artifacts": {},
        "verified": [],
        "records": 0,
        "status": "running",
        "exit_reason": None,
        "escalation": None,
        "approved": [],
        "history": [],
    }


def state_digest(state: dict[str, Any]) -> dict[str, Any]:
    """The comparable subset of run state, for the kill/resume diff.

    Both implementations emit exactly this, so 'did resume recover the same
    state' is a dict equality check rather than a judgement call.
    """
    return {
        "run_id": state["run_id"],
        "scenario": state["scenario"],
        "cursor": state["cursor"],
        "spent_usd": round(state["spent_usd"], 4),
        "attempts": dict(state["attempts"]),
        "artifacts": dict(state["artifacts"]),
        "verified": list(state["verified"]),
        "records": state["records"],
        "status": state["status"],
        "exit_reason": state["exit_reason"],
        "escalation": state["escalation"],
        "approved": list(state.get("approved", [])),
        "history": list(state["history"]),
    }
