"""Counted, not estimated: lines of code, dependency weight, cold start.

Lines of code
  Physical lines, minus blank lines and minus whole-line `#` comments.
  Docstrings ARE counted, because they are lines someone has to maintain.
  Reported in three buckets -- shared, hand-rolled only, LangGraph only --
  because the shared bucket (MCP layer, policy, fault injection) is identical
  for both and folding it into either total would be dishonest.

Dependency weight
  Two throwaway virtualenvs are built from scratch: one with just `mcp`, one
  with `mcp` plus `langgraph` and `langgraph-checkpoint-sqlite`. Reported as
  distributions installed and bytes of site-packages on disk. The interesting
  number is the difference: what adopting the framework costs.

Cold start
  Wall time of `python -c "<the imports that implementation needs>"`, and wall
  time of a complete baseline run end to end. Best of N, min and median both
  reported, because a single timing on a laptop is noise.

Usage:  python bench/static_measure.py [--skip-venvs]
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
OUT = REPO / "bench" / "results"
TMP = REPO / "bench" / ".venvtmp"

BUCKETS = {
    "shared": [
        "tools/mcp_server.py", "tools/mcp_client.py", "tools/world.py",
        "tools/policy.py", "tools/fault.py",
    ],
    "handrolled_only": ["handrolled/loop.py", "handrolled/run.py", "handrolled/atomic_io.py"],
    "langgraph_only": ["langgraph_impl/graph.py", "langgraph_impl/run.py"],
}

N_TIMING = 7


def count_loc(rel: str) -> dict:
    lines = (REPO / rel).read_text().splitlines()
    code = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return {"physical": len(lines), "code": len(code)}


def loc_report() -> dict:
    out: dict = {}
    for bucket, files in BUCKETS.items():
        per_file = {f: count_loc(f) for f in files}
        out[bucket] = {
            "files": per_file,
            "total_code_lines": sum(v["code"] for v in per_file.values()),
            "total_physical_lines": sum(v["physical"] for v in per_file.values()),
        }
    contract = (REPO / "tools" / "tool-defs.json").read_text().splitlines()
    out["tool_contract_json_lines"] = len([ln for ln in contract if ln.strip()])
    return out


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def build_venv(name: str, packages: list[str]) -> dict:
    venv = TMP / name
    shutil.rmtree(venv, ignore_errors=True)
    venv.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    subprocess.run([str(venv / "bin" / "pip"), "install", "-q", *packages],
                   check=True, capture_output=True)
    install_s = round(time.perf_counter() - t0, 2)
    sp = next((venv / "lib").glob("python*/site-packages"))
    freeze = subprocess.run([str(venv / "bin" / "pip"), "freeze"],
                            check=True, capture_output=True, text=True).stdout
    dists = sorted(ln.split("==")[0] for ln in freeze.splitlines() if "==" in ln)
    return {
        "requested": packages,
        "distributions_installed": len(dists),
        "distributions": dists,
        "site_packages_bytes": _dir_bytes(sp),
        "site_packages_mb": round(_dir_bytes(sp) / 1_000_000, 1),
        "fresh_install_wall_s": install_s,
    }


def time_cmd(cmd: list[str], n: int, cwd: Path, env: dict | None = None) -> dict:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, cwd=cwd, env=env)
        times.append(time.perf_counter() - t0)
        if p.returncode != 0:
            return {"error": p.stderr.decode()[-400:], "returncode": p.returncode}
    return {
        "n": n,
        "min_s": round(min(times), 4),
        "median_s": round(statistics.median(times), 4),
        "max_s": round(max(times), 4),
    }


def cold_start() -> dict:
    import os

    hr_imports = (
        "import sys; sys.path.insert(0,'tools'); sys.path.insert(0,'handrolled');"
        "import policy, atomic_io, fault, mcp_client, loop"
    )
    lg_imports = (
        "import sys; sys.path.insert(0,'tools'); sys.path.insert(0,'langgraph_impl');"
        "import policy, fault, mcp_client, graph;"
        "from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver"
    )
    env = dict(os.environ)
    return {
        "import_only": {
            "handrolled": time_cmd([PY, "-c", hr_imports], N_TIMING, REPO),
            "langgraph": time_cmd([PY, "-c", lg_imports], N_TIMING, REPO),
            "note": "python -c importing exactly what each implementation needs, from a warm page cache",
        },
        "end_to_end_baseline": {
            "handrolled": _timed_run("handrolled", env),
            "langgraph": _timed_run("langgraph", env),
            "note": "full baseline scenario: process start, MCP server spawn, 24 loop steps, exit",
        },
    }


def _timed_run(impl: str, env: dict) -> dict:
    script = "handrolled/run.py" if impl == "handrolled" else "langgraph_impl/run.py"
    times = []
    for i in range(N_TIMING):
        run_dir = OUT / f"timing-{impl}-{i}"
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        e = dict(env)
        e["LOOPLAB_STATE_DIR"] = str(run_dir)
        t0 = time.perf_counter()
        p = subprocess.run([PY, script, "--scenario", "baseline", "--run-dir", str(run_dir)],
                           capture_output=True, cwd=REPO, env=e)
        times.append(time.perf_counter() - t0)
        if p.returncode != 0:
            return {"error": p.stderr.decode()[-400:], "returncode": p.returncode}
        shutil.rmtree(run_dir, ignore_errors=True)
    return {
        "n": N_TIMING,
        "min_s": round(min(times), 4),
        "median_s": round(statistics.median(times), 4),
        "max_s": round(max(times), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-venvs", action="store_true",
                    help="skip the two throwaway venv builds (network + ~1 min)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    results = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "platform": subprocess.run(["uname", "-mrs"], capture_output=True, text=True).stdout.strip(),
        "loc": loc_report(),
        "cold_start": cold_start(),
    }
    if not args.skip_venvs:
        results["dependencies"] = {
            "handrolled": build_venv("hr", ["mcp"]),
            "langgraph": build_venv("lg", ["mcp", "langgraph", "langgraph-checkpoint-sqlite"]),
        }
        hr = results["dependencies"]["handrolled"]
        lg = results["dependencies"]["langgraph"]
        results["dependencies"]["framework_cost"] = {
            "extra_distributions": lg["distributions_installed"] - hr["distributions_installed"],
            "extra_site_packages_mb": round(
                (lg["site_packages_bytes"] - hr["site_packages_bytes"]) / 1_000_000, 1
            ),
            "extra_distribution_names": sorted(
                set(lg["distributions"]) - set(hr["distributions"])
            ),
        }
        shutil.rmtree(TMP, ignore_errors=True)

    (OUT / "static.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in results.items() if k != "loc"}, indent=2)[:2500])
    print("LOC:", json.dumps(
        {b: results["loc"][b]["total_code_lines"] for b in BUCKETS}, indent=2))
    print(f"wrote {OUT / 'static.json'}")


if __name__ == "__main__":
    main()
