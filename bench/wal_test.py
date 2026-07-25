"""Is the checkpoint file you would back up actually the checkpoint?

A run is killed with SIGKILL mid-tool-call. Then, for each implementation, the
single obvious "the state is in here" file is copied to a clean directory on
its own -- `state.json` for the hand-rolled loop, `checkpoints.sqlite` for
LangGraph -- and the run is resumed from that copy alone.

This is not a contrived test. It is what a backup script, an rsync, a docker
volume snapshot, or `scp the-db-file` does. SQLite in WAL mode keeps recent
commits in a sibling `-wal` file until a checkpoint folds them back into the
main database, so the main file can be stale or empty while the run looks fine
on the original machine.

Usage:  python bench/wal_test.py     (writes bench/results/wal.json)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
OUT = REPO / "bench" / "results"

IMPLS = {
    "handrolled": (
        [PY, str(REPO / "handrolled" / "run.py")],
        "state.json",
        ["state.json", "record-seq.json"],
    ),
    "langgraph": (
        [PY, str(REPO / "langgraph_impl" / "run.py")],
        "checkpoints.sqlite",
        ["checkpoints.sqlite", "checkpoints.sqlite-wal", "checkpoints.sqlite-shm", "record-seq.json"],
    ),
}


def _digest(out: str) -> dict | None:
    for line in out.splitlines():
        if line.startswith("DIGEST "):
            return json.loads(line[len("DIGEST "):])
    return None


def _kill_midrun(cmd: list[str], run_dir: Path) -> None:
    env = dict(os.environ)
    env["LOOPLAB_STATE_DIR"] = str(run_dir)
    env["LOOPLAB_SLOW_STEP"] = "s2"
    env["LOOPLAB_SLOW_SECONDS"] = "3.0"
    log = run_dir / "run1.log"
    full = cmd + ["--scenario", "baseline", "--run-dir", str(run_dir)]
    with log.open("w") as fh:
        proc = subprocess.Popen(full, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                cwd=REPO, start_new_session=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if log.read_text().count("MARK act") >= 2:
                break
            time.sleep(0.02)
        else:
            proc.kill()
            raise RuntimeError("never reached the second act")
        time.sleep(1.0)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)


def _resume_from(impl: str, files: list[str], src: Path, tag: str) -> dict:
    dst = OUT / f"wal-{impl}-{tag}"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    copied = []
    for name in files:
        p = src / name
        if p.exists():
            shutil.copy2(p, dst / name)
            copied.append(name)
    env = dict(os.environ)
    env["LOOPLAB_STATE_DIR"] = str(dst)
    cmd = IMPLS[impl][0] + ["--scenario", "baseline", "--run-dir", str(dst), "--resume"]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=REPO)
    d = _digest(p.stdout)
    return {
        "copied_files": copied,
        "exit_code": p.returncode,
        "resumed_history_len": len(d.get("history", [])) if d else None,
        "resumed_status": d.get("status") if d else None,
        "resumed_spent_usd": d.get("spent_usd") if d else None,
        "stderr_tail": p.stderr.strip()[-300:],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "implementations": {},
    }
    for impl, (cmd, single, whole) in IMPLS.items():
        src = OUT / f"wal-src-{impl}"
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir(parents=True)
        _kill_midrun(cmd, src)
        sizes = {p.name: p.stat().st_size for p in sorted(src.iterdir()) if p.is_file()}
        single_only = _resume_from(impl, [single, "record-seq.json"], src, "singlefile")
        everything = _resume_from(impl, whole, src, "allfiles")
        results["implementations"][impl] = {
            "canonical_checkpoint_file": single,
            "note_on_sizes": (
                "The two checkpoint stores are not like for like. state.json holds ONLY the "
                "current head state. The SQLite store holds every checkpoint in the thread's "
                "history, which is what makes LangGraph's time-travel and fork features "
                "possible. Compare the file sizes with that in mind."
            ),
            "file_sizes_after_kill": sizes,
            "resume_from_canonical_file_only": single_only,
            "resume_from_all_files": everything,
            "canonical_file_is_self_sufficient":
                single_only["resumed_history_len"] == everything["resumed_history_len"],
        }
        print(f"[{impl}] {single}={sizes.get(single)}B  "
              f"single-file resume history_len={single_only['resumed_history_len']}  "
              f"all-files resume history_len={everything['resumed_history_len']}  "
              f"self_sufficient={results['implementations'][impl]['canonical_file_is_self_sufficient']}")
    (OUT / "wal.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"wrote {OUT / 'wal.json'}")


if __name__ == "__main__":
    main()
