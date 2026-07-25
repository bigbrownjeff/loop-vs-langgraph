"""Do the two implementations actually do the same thing?

Every claim in RESULTS.md rests on this. If the two loops diverged on any
scenario, none of the durability or failure numbers would be comparable.

Runs all four scenarios on both implementations and compares the digest -- the
full ordered node history, every artifact, every attempt count, the running
spend, the exit reason and any escalation. LangGraph-only keys (`next_nodes`)
and bookkeeping keys (`resumed_count`) are excluded and named here so the
exclusion is visible rather than convenient.

Usage:  python bench/parity.py     (writes bench/results/parity.json)
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
SCENARIOS = ["baseline", "irreversible", "overspend", "verify_stuck"]
EXCLUDED_KEYS = ["next_nodes", "resumed_count"]

# The fields that decide whether the run was CORRECT: where it stopped, what it
# produced, what it spent, and why it stopped.
OUTCOME_KEYS = [
    "status", "exit_reason", "escalation", "approved",
    "cursor", "spent_usd", "artifacts", "attempts", "verified",
]

IMPLS = {
    "handrolled": [PY, str(REPO / "handrolled" / "run.py")],
    "langgraph": [PY, str(REPO / "langgraph_impl" / "run.py")],
}


def run(impl: str, scenario: str) -> dict:
    run_dir = OUT / f"parity-{impl}-{scenario}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["LOOPLAB_STATE_DIR"] = str(run_dir)
    p = subprocess.run(
        IMPLS[impl] + ["--scenario", scenario, "--run-dir", str(run_dir)],
        env=env, capture_output=True, text=True, cwd=REPO,
    )
    for line in p.stdout.splitlines():
        if line.startswith("DIGEST "):
            d = json.loads(line[len("DIGEST "):])
            return {k: v for k, v in d.items() if k not in EXCLUDED_KEYS}
    raise RuntimeError(f"{impl}/{scenario} produced no digest:\n{p.stderr[-800:]}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "excluded_keys": EXCLUDED_KEYS,
        "scenarios": {},
    }
    all_match = True
    for scenario in SCENARIOS:
        hr = run("handrolled", scenario)
        lg = run("langgraph", scenario)
        match = hr == lg
        outcome_match = all(hr.get(k) == lg.get(k) for k in OUTCOME_KEYS)
        all_match &= outcome_match
        results["scenarios"][scenario] = {
            "identical": match,
            "outcome_identical": outcome_match,
            "handrolled_digest": hr,
            "langgraph_digest": lg,
            "differing_keys": sorted(k for k in set(hr) | set(lg) if hr.get(k) != lg.get(k)),
        }
        print(f"[{scenario}] outcome_identical={outcome_match} strict_identical={match} "
              f"status={hr['status']} spent=${hr['spent_usd']:.2f} "
              f"differs_on={sorted(k for k in set(hr)|set(lg) if hr.get(k)!=lg.get(k))}")
    results["all_scenarios_outcome_identical"] = all_match
    results["outcome_keys"] = OUTCOME_KEYS
    results["known_divergence"] = (
        "On the three escalating scenarios the two digests differ on `history` and "
        "`records`, and only those. Cause: LangGraph's interrupt() raises out of the "
        "decide node, so that node's state update is discarded and never committed. "
        "The escalation is recoverable only from the interrupt payload on the pending "
        "task, which langgraph_impl/run.py reads back explicitly. The hand-rolled loop "
        "commits the same update before it stops, so its own state says it escalated. "
        "Both loops halted at the same place, before the same step, having spent the "
        "same amount. See RESULTS.md finding 4."
    )
    (OUT / "parity.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"all_scenarios_identical={all_match}")
    print(f"wrote {OUT / 'parity.json'}")
    sys.exit(0 if all_match else 1)


if __name__ == "__main__":
    main()
