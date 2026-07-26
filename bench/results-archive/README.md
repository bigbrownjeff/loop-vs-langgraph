# Archived measurement sets

Each directory is a complete `bench/results/` snapshot from one environment,
kept so the cross-platform claims in RESULTS.md stay auditable rather than
taken on faith.

| Directory | Environment | Code measured |
|---|---|---|
| `2026-07-25-darwin-py3.14/` | Darwin 25.5.0 arm64, Python 3.14.6 | pre-hardening: escalation record above `interrupt()`, no caller-side verdict write, default durability |

The live `bench/results/` directory always holds the most recent run of
`bench/run_all.sh` against the committed code. Package versions were identical
across all sets: `mcp` 1.28.1, `langgraph` 1.2.9,
`langgraph-checkpoint-sqlite` 3.1.0.
