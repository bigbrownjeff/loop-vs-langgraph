"""Kill and resume, for real, at two different kill points, for both loops.

Kill point A -- external SIGKILL landing INSIDE a tool call.
  `act` on step s2 is slowed to 3s. A supervisor sends SIGKILL 1s in. Nothing
  has been returned, so in principle nothing should be lost.

Kill point B -- os._exit(137) landing AFTER a tool call returns and BEFORE the
  implementation commits anything.
  This is the durability window that actually costs money: the side effect
  happened, and the run state that knows about it has not reached disk.

For each (implementation, kill point) the harness records:
  - what the final digest was after resume, vs a clean uninterrupted run
  - whether they are identical
  - how many times each side-effecting tool was called, from the SERVER's own
    append-only log, so neither loop can grade its own homework
  - what artefacts were on disk at the moment of the kill

Usage:  python bench/kill_test.py            (writes bench/results/kill.json)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
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


def _env(run_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["LOOPLAB_STATE_DIR"] = str(run_dir)
    env.pop("LOOPLAB_DIE_AFTER_NODE", None)
    env.pop("LOOPLAB_SLOW_STEP", None)
    env.update(extra)
    return env


def _cmd(impl: str, run_dir: Path, scenario: str, resume: bool) -> list[str]:
    cmd = IMPLS[impl] + ["--scenario", scenario, "--run-dir", str(run_dir)]
    if resume:
        cmd.append("--resume")
    return cmd


def _digest_from(out: str) -> dict | None:
    for line in out.splitlines():
        if line.startswith("DIGEST "):
            return json.loads(line[len("DIGEST "):])
    return None


def _comparable(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if k not in ("next_nodes", "resumed_count")}


def _tool_calls(run_dir: Path) -> Counter:
    log = run_dir / "tool-calls.jsonl"
    c: Counter = Counter()
    if not log.exists():
        return c
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        key = rec["tool"]
        if rec["tool"] == "act":
            key = f"act:{rec['args']['step_id']}#{rec['args']['attempt']}"
        c[key] += 1
    return c


def _on_disk(impl: str, run_dir: Path) -> dict:
    """What the crash left behind, before any resume touches it."""
    info: dict = {"files": sorted(p.name for p in run_dir.iterdir() if p.is_file())}
    if impl == "handrolled":
        ckpt = run_dir / "state.json"
        info["checkpoint_bytes"] = ckpt.stat().st_size if ckpt.exists() else 0
        if ckpt.exists():
            try:
                st = json.loads(ckpt.read_text())
                info["checkpoint_parses"] = True
                info["next_node"] = st.get("next_node")
                info["history_len"] = len(st.get("history", []))
                info["spent_usd"] = st.get("spent_usd")
            except json.JSONDecodeError:
                info["checkpoint_parses"] = False
        info["temp_files_left"] = sorted(
            p.name for p in run_dir.iterdir() if p.name.startswith(".state.json.")
        )
    else:
        db = run_dir / "checkpoints.sqlite"
        info["checkpoint_bytes"] = db.stat().st_size if db.exists() else 0
        info["sqlite_sidecars"] = sorted(
            p.name for p in run_dir.iterdir() if p.name.startswith("checkpoints.sqlite-")
        )
    return info


def clean_run(impl: str, scenario: str) -> dict:
    run_dir = OUT / f"clean-{impl}-{scenario}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    t0 = time.perf_counter()
    p = subprocess.run(
        _cmd(impl, run_dir, scenario, resume=False),
        env=_env(run_dir), capture_output=True, text=True, cwd=REPO,
    )
    return {
        "wall_s": round(time.perf_counter() - t0, 4),
        "exit_code": p.returncode,
        "digest": _digest_from(p.stdout),
        "tool_calls": dict(_tool_calls(run_dir)),
    }


def kill_point_a(impl: str) -> dict:
    """External SIGKILL 1.0s into a 3.0s tool call on s2."""
    run_dir = OUT / f"killA-{impl}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    env = _env(run_dir, LOOPLAB_SLOW_STEP="s2", LOOPLAB_SLOW_SECONDS="3.0")
    log = run_dir / "run1.log"
    with log.open("w") as fh:
        proc = subprocess.Popen(
            _cmd(impl, run_dir, "baseline", resume=False),
            env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO,
            start_new_session=True,  # own process group, so the MCP child dies too
        )
        # Wait for the SECOND `MARK act` (= s2), then let it get 1s in.
        deadline = time.time() + 30
        while time.time() < deadline:
            text = log.read_text() if log.exists() else ""
            if text.count("MARK act") >= 2:
                break
            time.sleep(0.02)
        else:
            proc.kill()
            raise RuntimeError("never reached the second act")
        time.sleep(1.0)
        killed_at = time.time()
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        rc = proc.wait(timeout=10)

    on_disk = _on_disk(impl, run_dir)
    calls_before = dict(_tool_calls(run_dir))

    p2 = subprocess.run(
        _cmd(impl, run_dir, "baseline", resume=True),
        env=_env(run_dir), capture_output=True, text=True, cwd=REPO,
    )
    return {
        "kill_signal": "SIGKILL to the process group, 1.0s into a 3.0s act(s2)",
        "killed_exit_code": rc,
        "killed_at_epoch": round(killed_at, 3),
        "on_disk_after_kill": on_disk,
        "tool_calls_before_kill": calls_before,
        "resume_exit_code": p2.returncode,
        "resume_digest": _digest_from(p2.stdout),
        "resume_stderr": p2.stderr.strip()[:600],
        "tool_calls_total": dict(_tool_calls(run_dir)),
    }


def kill_point_b(impl: str) -> dict:
    """os._exit(137) at the end of act's 2nd visit: after the side effect, before the commit."""
    run_dir = OUT / f"killB-{impl}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    p1 = subprocess.run(
        _cmd(impl, run_dir, "baseline", resume=False),
        env=_env(run_dir, LOOPLAB_DIE_AFTER_NODE="act#2"),
        capture_output=True, text=True, cwd=REPO,
    )
    on_disk = _on_disk(impl, run_dir)
    calls_before = dict(_tool_calls(run_dir))

    p2 = subprocess.run(
        _cmd(impl, run_dir, "baseline", resume=True),
        env=_env(run_dir), capture_output=True, text=True, cwd=REPO,
    )
    return {
        "kill_signal": "os._exit(137) at the end of act's 2nd visit (step s2, attempt 1)",
        "killed_exit_code": p1.returncode,
        "on_disk_after_kill": on_disk,
        "tool_calls_before_kill": calls_before,
        "resume_exit_code": p2.returncode,
        "resume_digest": _digest_from(p2.stdout),
        "resume_stderr": p2.stderr.strip()[:600],
        "tool_calls_total": dict(_tool_calls(run_dir)),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "implementations": {},
    }
    for impl in IMPLS:
        clean = clean_run(impl, "baseline")
        a = kill_point_a(impl)
        b = kill_point_b(impl)
        for k in (a, b):
            k["resume_matches_clean"] = _comparable(k["resume_digest"]) == _comparable(clean["digest"])
            k["duplicate_act_calls"] = {
                kk: v for kk, v in k["tool_calls_total"].items()
                if kk.startswith("act:") and v > 1
            }
        results["implementations"][impl] = {"clean": clean, "kill_point_a": a, "kill_point_b": b}
        print(f"[{impl}] clean exit={clean['exit_code']} "
              f"A.match={a['resume_matches_clean']} A.dupacts={a['duplicate_act_calls']} "
              f"B.match={b['resume_matches_clean']} B.dupacts={b['duplicate_act_calls']}")

    (OUT / "kill.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"wrote {OUT / 'kill.json'}")


if __name__ == "__main__":
    main()
