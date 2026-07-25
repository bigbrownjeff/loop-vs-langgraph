"""Three ways a run goes wrong, in both implementations.

  schema_violation -- `act` returns cost_usd as a string. The frozen contract
                      says number. The MCP server must reject its own handler's
                      output before it reaches either loop.
  timeout          -- `act` sleeps 600s against a 2s per-call client deadline.
  crash            -- `act` raises inside the handler: an exception on the far
                      side of the tool boundary.

Recorded for each: exit code, the exception class the loop saw, the message,
whether a resumable checkpoint survived, and whether the run's own state file
says it failed.

Usage:  python bench/failure_modes.py     (writes bench/results/failures.json)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
OUT = REPO / "bench" / "results"

IMPLS = {
    "handrolled": [PY, str(REPO / "handrolled" / "run.py")],
    "langgraph": [PY, str(REPO / "langgraph_impl" / "run.py")],
}

FAULTS = {
    "schema_violation": {"LOOPLAB_FAULT": "schema_violation", "LOOPLAB_FAULT_STEP": "s2"},
    "timeout": {"LOOPLAB_FAULT": "hang", "LOOPLAB_FAULT_STEP": "s2", "LOOPLAB_TOOL_TIMEOUT_S": "2"},
    "crash": {"LOOPLAB_FAULT": "crash", "LOOPLAB_FAULT_STEP": "s2"},
}

HARNESS_LIMIT_S = 60


def _state_after(impl: str, run_dir: Path) -> dict:
    info: dict = {"files": sorted(p.name for p in run_dir.iterdir() if p.is_file())}
    if impl == "handrolled":
        ckpt = run_dir / "state.json"
        if ckpt.exists():
            st = json.loads(ckpt.read_text())
            info["status_in_checkpoint"] = st.get("status")
            info["exit_reason_in_checkpoint"] = st.get("exit_reason")
            info["next_node"] = st.get("next_node")
        else:
            info["status_in_checkpoint"] = None
    else:
        digest = run_dir / "digest.json"
        if digest.exists():
            d = json.loads(digest.read_text())
            info["status_in_digest"] = d.get("status")
            info["next_nodes"] = d.get("next_nodes")
            info["history_len"] = len(d.get("history", []))
    return info


def run_one(impl: str, fault: str) -> dict:
    run_dir = OUT / f"fail-{impl}-{fault}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["LOOPLAB_STATE_DIR"] = str(run_dir)
    env.update(FAULTS[fault])

    cmd = IMPLS[impl] + ["--scenario", "baseline", "--run-dir", str(run_dir)]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=REPO, start_new_session=True)
    timed_out_at_harness = False
    try:
        out, err = proc.communicate(timeout=HARNESS_LIMIT_S)
    except subprocess.TimeoutExpired:
        timed_out_at_harness = True
        os.killpg(os.getpgid(proc.pid), 9)
        out, err = proc.communicate()
    elapsed = round(time.perf_counter() - t0, 3)

    err = (err or "").strip()
    seen_class = None
    for line in err.splitlines():
        if line.startswith("FAILED "):
            seen_class = line.split()[1].rstrip(":")
    return {
        "exit_code": proc.returncode,
        "wall_s": elapsed,
        "harness_had_to_kill_it": timed_out_at_harness,
        "loop_saw_exception_class": seen_class,
        "stderr_first_line": err.splitlines()[0][:400] if err else "",
        "stderr_tail": err[-500:] if err else "",
        "state_after": _state_after(impl, run_dir),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "harness_limit_s": HARNESS_LIMIT_S,
        "cases": {},
    }
    for fault in FAULTS:
        results["cases"][fault] = {}
        for impl in IMPLS:
            r = run_one(impl, fault)
            results["cases"][fault][impl] = r
            print(f"[{fault}/{impl}] exit={r['exit_code']} {r['wall_s']}s "
                  f"saw={r['loop_saw_exception_class']} "
                  f"harness_kill={r['harness_had_to_kill_it']}")
    (OUT / "failures.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"wrote {OUT / 'failures.json'}")


if __name__ == "__main__":
    main()
