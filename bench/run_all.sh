#!/usr/bin/env bash
# Reproduce every number in RESULTS.md from a clean checkout.
#
#   python3 -m venv .venv
#   .venv/bin/pip install -r requirements.txt
#   ./bench/run_all.sh
#
# Writes bench/results/{parity,kill,failures,escalation,wal,static}.json.
# Takes about two minutes; static_measure.py builds two throwaway venvs and
# needs network. Pass --skip-venvs to skip that one measurement.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || { echo "no .venv -- see the header of this script" >&2; exit 1; }

rm -rf bench/results
mkdir -p bench/results

echo "== 1/6 parity: do both implementations do the same thing =="
$PY bench/parity.py

echo; echo "== 2/6 kill and resume, two kill points, both implementations =="
$PY bench/kill_test.py

echo; echo "== 3/6 failure modes: schema violation, timeout, mid-run exception =="
$PY bench/failure_modes.py

echo; echo "== 4/6 human escalation and approval =="
$PY bench/escalation_test.py

echo; echo "== 5/6 checkpoint file portability =="
$PY bench/wal_test.py

echo; echo "== 6/6 lines of code, dependency weight, cold start =="
$PY bench/static_measure.py "$@"

echo; echo "done. results in bench/results/"
