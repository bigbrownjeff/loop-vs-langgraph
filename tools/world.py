"""The synthetic world the tools act on, plus the fault injector.

Deterministic by construction: the same (scenario, step_id, attempt) always
produces the same artifact and the same cost. There is no LLM anywhere in this
repo. That is deliberate. The question under test is what the two orchestration
frameworks do with the same state machine, and a stochastic policy would make
every measurement below unreproducible. See RESULTS.md, "What this does not
measure".

Fault injection is env-driven so a single scenario can be replayed with one
thing broken:

  LOOPLAB_FAULT=none|schema_violation|hang|crash    (default: none)
  LOOPLAB_FAULT_STEP=<step id>                      (default: s2)
  LOOPLAB_SLOW_STEP=<step id>                       (default: unset)
  LOOPLAB_SLOW_SECONDS=<float>                      (default: 2.0)
  LOOPLAB_STATE_DIR=<dir>                           (server-side call log)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_DEFS_PATH = Path(__file__).resolve().parent / "tool-defs.json"
_DEFS = json.loads(_DEFS_PATH.read_text())

TOOL_LIST: list[dict[str, Any]] = _DEFS["tools"]
_BY_NAME = {t["name"]: t for t in TOOL_LIST}
CONTRACT_VERSION: str = _DEFS["contract_version"]


def tool_input_schema(name: str) -> dict[str, Any]:
    return _BY_NAME[name]["parameters"]


def tool_output_schema(name: str) -> dict[str, Any]:
    return _BY_NAME[name]["returns"]


# --------------------------------------------------------------------------
# Objective + scenarios
# --------------------------------------------------------------------------
OBJECTIVE = "Audit the pending release manifest and record the result."
SUCCESS_CRITERIA = [
    "every planned step produced an artifact",
    "every artifact passed the verify gate",
    "the run record contains one entry per loop node",
]
BUDGET_CAP_USD = 5.00

_COMMON_HEAD = [
    {"id": "s1", "title": "collect release artifacts", "reversible": True, "est_cost_usd": 0.20},
    {"id": "s2", "title": "diff the manifest against the last release", "reversible": True, "est_cost_usd": 0.40},
]
_COMMON_TAIL = [
    {"id": "s4", "title": "write the audit summary", "reversible": True, "est_cost_usd": 0.30},
]

SCENARIOS: dict[str, list[dict[str, Any]]] = {
    # Happy path, except s2 fails its verify gate once and passes on retry.
    "baseline": _COMMON_HEAD
    + [{"id": "s3", "title": "scan dependency licences", "reversible": True, "est_cost_usd": 0.50}]
    + _COMMON_TAIL,
    # s3 is irreversible -> the irreversible-action tripwire must fire before it runs.
    "irreversible": _COMMON_HEAD
    + [{"id": "s3", "title": "publish the release to the public index", "reversible": False, "est_cost_usd": 0.50}]
    + _COMMON_TAIL,
    # s3 costs more than the remaining budget -> the uncapped-spend tripwire must fire.
    "overspend": _COMMON_HEAD
    + [{"id": "s3", "title": "full-corpus deep scan", "reversible": True, "est_cost_usd": 8.00}]
    + _COMMON_TAIL,
    # s2 never passes its gate -> the repeated-verification-failure tripwire must fire.
    "verify_stuck": _COMMON_HEAD
    + [{"id": "s3", "title": "scan dependency licences", "reversible": True, "est_cost_usd": 0.50}]
    + _COMMON_TAIL,
}

BLAST_RADIUS = {
    "baseline": "project",
    "irreversible": "cross-project",
    "overspend": "project",
    "verify_stuck": "project",
}


# --------------------------------------------------------------------------
# Server-side call log: the ground truth for "what actually got called".
# Written by the SERVER, so neither loop can flatter itself in its own state.
# --------------------------------------------------------------------------
def _state_dir() -> Path | None:
    raw = os.environ.get("LOOPLAB_STATE_DIR")
    if not raw:
        return None
    d = Path(raw)
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_call(name: str, arguments: dict[str, Any], outcome: str) -> None:
    d = _state_dir()
    if d is None:
        return
    line = json.dumps(
        {"ts": time.time(), "tool": name, "args": arguments, "outcome": outcome},
        sort_keys=True,
    )
    with (d / "tool-calls.jsonl").open("a") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------
def _fault() -> tuple[str, str]:
    return (
        os.environ.get("LOOPLAB_FAULT", "none"),
        os.environ.get("LOOPLAB_FAULT_STEP", "s2"),
    )


def _maybe_slow(step_id: str) -> None:
    slow = os.environ.get("LOOPLAB_SLOW_STEP")
    if slow and slow == step_id:
        time.sleep(float(os.environ.get("LOOPLAB_SLOW_SECONDS", "2.0")))


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def do_orient(args: dict[str, Any]) -> dict[str, Any]:
    scenario = args["scenario"]
    return {
        "objective": OBJECTIVE,
        "success_criteria": list(SUCCESS_CRITERIA),
        "budget_cap_usd": BUDGET_CAP_USD,
        "blast_radius": BLAST_RADIUS[scenario],
    }


def do_plan(args: dict[str, Any]) -> dict[str, Any]:
    return {"steps": [dict(s) for s in SCENARIOS[args["scenario"]]]}


def do_act(args: dict[str, Any]) -> dict[str, Any]:
    step_id = args["step_id"]
    attempt = args["attempt"]
    scenario = args["scenario"]
    fault, fault_step = _fault()

    if fault == "crash" and step_id == fault_step:
        raise RuntimeError(f"injected mid-run exception in act({step_id})")
    if fault == "hang" and step_id == fault_step:
        time.sleep(600)

    _maybe_slow(step_id)

    step = next(s for s in SCENARIOS[scenario] if s["id"] == step_id)
    result = {
        "step_id": step_id,
        "artifact": f"{step_id}:{step['title']}:attempt-{attempt}",
        "cost_usd": step["est_cost_usd"],
    }
    if fault == "schema_violation" and step_id == fault_step:
        # cost_usd must be a number per the frozen outputSchema. Send a string.
        result["cost_usd"] = "0.40"
    return result


def do_verify(args: dict[str, Any]) -> dict[str, Any]:
    scenario = args["scenario"]
    step_id = args["step_id"]
    attempt = args["attempt"]

    if scenario == "verify_stuck" and step_id == "s2":
        return {"passed": False, "reasons": ["manifest diff is unresolvable against the criteria"]}
    if scenario == "baseline" and step_id == "s2" and attempt == 1:
        return {"passed": False, "reasons": ["diff omitted the transitive dependency set"]}
    return {"passed": True, "reasons": []}


_SEQ: dict[str, int] = {}


def do_record(args: dict[str, Any]) -> dict[str, Any]:
    """Append to the durable run record and hand back the sequence number.

    The sequence counter is persisted to disk, not held in server memory. The
    first version of this held it in a module-level dict, and every kill/resume
    test came back with records=5 against a clean run's records=6 -- because
    resume starts a NEW server process and the counter silently restarted.
    Neither framework's checkpointer would have caught that: the counter lived
    outside both of them. See RESULTS.md, finding 8.
    """
    run_id = args["run_id"]
    d = _state_dir()
    if d is None:
        _SEQ[run_id] = _SEQ.get(run_id, 0) + 1
        return {"seq": _SEQ[run_id], "run_id": run_id}

    path = d / "record-seq.json"
    counters: dict[str, int] = {}
    if path.exists():
        counters = json.loads(path.read_text())
    counters[run_id] = counters.get(run_id, 0) + 1
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(counters, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return {"seq": counters[run_id], "run_id": run_id}


DISPATCH = {
    "orient": do_orient,
    "plan": do_plan,
    "act": do_act,
    "verify": do_verify,
    "record": do_record,
}


def summarize(name: str, result: dict[str, Any]) -> str:
    """One human-readable line alongside every structured result."""
    if name == "orient":
        return f"objective loaded; cap ${result['budget_cap_usd']:.2f}; blast radius {result['blast_radius']}"
    if name == "plan":
        return f"{len(result['steps'])} steps planned"
    if name == "act":
        return f"{result['step_id']} produced an artifact"
    if name == "verify":
        return "gate passed" if result["passed"] else f"gate FAILED: {'; '.join(result['reasons'])}"
    if name == "record":
        return f"record seq {result['seq']}"
    return "ok"
