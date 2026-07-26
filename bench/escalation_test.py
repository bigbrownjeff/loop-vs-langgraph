"""Human escalation: fire a tripwire, then let a human answer it.

Scenario `irreversible`: step s3 publishes to a public index. LOOP.md says stop
and ask. Both implementations must halt BEFORE s3 runs, persist enough to be
answerable later, and then continue correctly once a human approves.

The measurement that matters is not "did it stop" -- both stop. It is what the
halt costs:
  - Does the halted run survive process death, and can a SEPARATE process
    answer it?
  - Does answering it re-execute anything? Counted from the server's own
    append-only tool log, which neither loop can edit.

Usage:  python bench/escalation_test.py   (writes bench/results/escalation.json)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
OUT = REPO / "bench" / "results"

IMPLS = {
    "handrolled": [PY, str(REPO / "handrolled" / "run.py")],
    "langgraph": [PY, str(REPO / "langgraph_impl" / "run.py")],
}

# The LangGraph decide node is measured under both statement orderings.
# Default: interrupt() first, record after resume -- the hardened ordering.
# record_first: the side effect above the interrupt(), which the node
# re-executes on resume. See RESULTS.md finding 5.
CASES = {
    "handrolled": ("handrolled", {}),
    "langgraph": ("langgraph", {}),
    "langgraph_record_first": ("langgraph", {"LOOPLAB_ESCALATION_ORDER": "record_first"}),
}


def _digest(out: str) -> dict | None:
    for line in out.splitlines():
        if line.startswith("DIGEST "):
            return json.loads(line[len("DIGEST "):])
    return None


def _calls(run_dir: Path) -> Counter:
    log = run_dir / "tool-calls.jsonl"
    c: Counter = Counter()
    if not log.exists():
        return c
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        key = r["tool"]
        if r["tool"] == "record":
            key = f"record:{r['args']['kind']}"
        elif r["tool"] == "act":
            key = f"act:{r['args']['step_id']}"
        c[key] += 1
    return c


def run_case(case: str) -> dict:
    impl, extra_env = CASES[case]
    run_dir = OUT / f"esc-{case}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["LOOPLAB_STATE_DIR"] = str(run_dir)
    env.update(extra_env)
    base = IMPLS[impl] + ["--scenario", "irreversible", "--run-dir", str(run_dir)]

    p1 = subprocess.run(base, env=env, capture_output=True, text=True, cwd=REPO)
    halted = _digest(p1.stdout)
    calls_at_halt = dict(_calls(run_dir))

    # A SEPARATE process answers the tripwire. Nothing is held in memory.
    p2 = subprocess.run(base + ["--resume", "--approve", "approved-by-jeff"],
                        env=env, capture_output=True, text=True, cwd=REPO)
    after = _digest(p2.stdout)
    calls_total = dict(_calls(run_dir))

    escalate_records = calls_total.get("record:escalate", 0)
    return {
        "halt_exit_code": p1.returncode,
        "halted_before_s3": "s3" not in (halted or {}).get("artifacts", {}),
        "halted_digest": halted,
        "calls_at_halt": calls_at_halt,
        "approve_exit_code": p2.returncode,
        "approve_stderr": p2.stderr.strip()[:400],
        "after_approval_digest": after,
        "calls_total": calls_total,
        "escalate_records_written": escalate_records,
        "escalate_record_duplicated_on_resume": escalate_records > 1,
        "completed_after_approval": (after or {}).get("status") == "done",
        "s3_ran_after_approval": "s3" in (after or {}).get("artifacts", {}),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "scenario": "irreversible (s3 publishes to a public index)",
        "implementations": {},
    }
    for impl in CASES:
        r = run_case(impl)
        results["implementations"][impl] = r
        print(f"[{impl}] halted_before_s3={r['halted_before_s3']} "
              f"completed_after_approval={r['completed_after_approval']} "
              f"escalate_records={r['escalate_records_written']} "
              f"duplicated={r['escalate_record_duplicated_on_resume']}")
    (OUT / "escalation.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"wrote {OUT / 'escalation.json'}")


if __name__ == "__main__":
    main()
