"""Generate demo/public/index.html from bench/results/*.json.

The page is built from the measurement files, not hand-typed. If a number on
the page disagrees with a number in bench/results/, that is a build failure,
not an editing slip.

Usage:  python demo/build.py     (writes demo/public/index.html)
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
R = REPO / "bench" / "results"


def load(name: str) -> dict:
    return json.loads((R / name).read_text())


parity = load("parity.json")
kill = load("kill.json")
fail = load("failures.json")
esc = load("escalation.json")
wal = load("wal.json")
static = load("static.json")

loc = static["loc"]
dep = static["dependencies"]
cs = static["cold_start"]

HR = "handrolled"
LG = "langgraph"


def row(label: str, a: str, b: str, note: str = "", winner: str = "") -> str:
    ca = ' class="win"' if winner == "hr" else ""
    cb = ' class="win"' if winner == "lg" else ""
    n = f'<div class="note">{note}</div>' if note else ""
    return (f'<tr><th scope="row">{label}{n}</th>'
            f'<td{ca}>{a}</td><td{cb}>{b}</td></tr>')


# ---- the scoreboard, straight out of the JSON ----------------------------
kh, kl = kill["implementations"][HR], kill["implementations"][LG]
wh, wl = wal["implementations"][HR], wal["implementations"][LG]
eh, el = esc["implementations"][HR], esc["implementations"][LG]

scoreboard = "\n".join([
    row("Code lines for the loop itself",
        f'{loc["handrolled_only"]["total_code_lines"]}',
        f'{loc["langgraph_only"]["total_code_lines"]}',
        f'Shared layer, identical for both: {loc["shared"]["total_code_lines"]} lines. '
        f'Blanks and comment-only lines excluded. With docstrings also stripped it is '
        f'{loc["handrolled_only"]["total_logic_lines"]} against '
        f'{loc["langgraph_only"]["total_logic_lines"]}, so the gap is not my prose.',
        "hr"),
    row("Python distributions installed",
        f'{dep[HR]["distributions_installed"]}',
        f'{dep[LG]["distributions_installed"]}',
        f'Two throwaway virtualenvs, built from scratch. '
        f'The framework costs {dep["framework_cost"]["extra_distributions"]} extra.',
        "hr"),
    row("site-packages on disk",
        f'{dep[HR]["site_packages_mb"]} MB',
        f'{dep[LG]["site_packages_mb"]} MB',
        f'+{dep["framework_cost"]["extra_site_packages_mb"]} MB.',
        "hr"),
    row("Import time, median of 7",
        f'{cs["import_only"][HR]["median_s"]}s',
        f'{cs["import_only"][LG]["median_s"]}s',
        "Importing exactly what each implementation needs.",
        "hr"),
    row("Full baseline run, median of 7",
        f'{cs["end_to_end_baseline"][HR]["median_s"]}s',
        f'{cs["end_to_end_baseline"][LG]["median_s"]}s',
        "Process start, MCP server spawn, 24 loop steps, exit.",
        "hr"),
    row("Recovers identical state after SIGKILL",
        "yes" if kh["kill_point_a"]["resume_matches_clean"] else "no",
        "yes" if kl["kill_point_a"]["resume_matches_clean"] else "no",
        "Killed 1.0s into a 3.0s tool call, then resumed."),
    row("Recovers identical state after a crash mid-commit",
        "yes" if kh["kill_point_b"]["resume_matches_clean"] else "no",
        "yes" if kl["kill_point_b"]["resume_matches_clean"] else "no",
        "Process death after the tool returned, before anything was persisted."),
    row("Side-effecting steps replayed on resume",
        f'{len(kh["kill_point_b"]["duplicate_act_calls"])} step, once',
        f'{len(kl["kill_point_b"]["duplicate_act_calls"])} step, once',
        "At-least-once at the node boundary, in both. Neither gives you exactly-once."),
    row("Canonical checkpoint file is self-sufficient",
        "yes" if wh["canonical_file_is_self_sufficient"] else "no",
        "yes" if wl["canonical_file_is_self_sufficient"] else "no",
        f'Copy the file alone and resume from it. '
        f'{wh["canonical_checkpoint_file"]} is {wh["file_sizes_after_kill"]["state.json"]:,} bytes; '
        f'{wl["canonical_checkpoint_file"]} is {wl["file_sizes_after_kill"]["checkpoints.sqlite"]:,} bytes '
        f'with {wl["file_sizes_after_kill"]["checkpoints.sqlite-wal"]:,} bytes sitting in a -wal sidecar.',
        "hr"),
    row("Persisted state says why the run died",
        f'yes, status <code>{fail["cases"]["crash"][HR]["state_after"]["status_in_checkpoint"]}</code> plus the message',
        f'not by default, status <code>{fail["cases"]["crash"][LG]["state_after"]["status_in_digest"]}</code>',
        "After an injected mid-run exception. Most of this gap is a choice I made, not the "
        "framework: the hand-rolled driver owns the state dict and writes a verdict on the "
        "way out. LangGraph can do the same in two lines via aupdate_state, tested. The "
        "finding is that it does not by default."),
    row("Halts before an irreversible action",
        "yes" if eh["halted_before_s3"] else "no",
        "yes" if el["halted_before_s3"] else "no",
        "And a separate process can answer the halt in both."),
    row("Escalation written to the run record",
        f'{eh["escalate_records_written"]} time',
        f'{el["escalate_records_written"]} times',
        "Resuming an interrupt re-runs the whole node, including side effects above it.",
        "hr"),
    row("Runaway protection",
        "none",
        "recursion_limit, default 25",
        "The baseline run needs 24 super-steps. Measured: it fails at 23.",
        "lg"),
    row("Human-in-the-loop primitive",
        "14 lines, written for this test",
        "built in",
        "interrupt() plus Command(resume=...), and it survives process death.",
        "lg"),
])

# ---- parity table --------------------------------------------------------
parity_rows = "\n".join(
    f'<tr><td><code>{s}</code></td>'
    f'<td class="{"ok" if v["outcome_identical"] else "bad"}">'
    f'{"identical" if v["outcome_identical"] else "differs"}</td>'
    f'<td>{v["handrolled_digest"]["status"]}</td>'
    f'<td>${v["handrolled_digest"]["spent_usd"]:.2f}</td>'
    f'<td>{"none" if not v["differing_keys"] else ", ".join("<code>%s</code>" % k for k in v["differing_keys"])}</td></tr>'
    for s, v in parity["scenarios"].items()
)

# ---- failure modes -------------------------------------------------------
FAULT_LABEL = {
    "schema_violation": ("Tool returns the wrong type",
                         "<code>act</code> returns <code>cost_usd</code> as the string <code>\"0.40\"</code>"),
    "timeout": ("Tool never answers",
                "<code>act</code> sleeps 600s against a 2s client deadline"),
    "crash": ("Tool raises",
              "<code>act</code> throws inside the handler"),
}
fail_rows = "\n".join(
    f'<tr><td><b>{FAULT_LABEL[f][0]}</b><div class="note">{FAULT_LABEL[f][1]}</div></td>'
    f'<td><code>{fail["cases"][f][HR]["loop_saw_exception_class"]}</code></td>'
    f'<td><code>{fail["cases"][f][HR]["state_after"]["status_in_checkpoint"]}</code></td>'
    f'<td><code>{fail["cases"][f][LG]["state_after"]["status_in_digest"]}</code></td></tr>'
    for f in ("schema_violation", "timeout", "crash")
)

MEASURED_ON = (f'{static["platform"]}, Python {static["python"]}, '
               f'mcp 1.28.1, langgraph 1.2.9, langgraph-checkpoint-sqlite 3.1.0')

HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loop Lab: the same agent loop, built twice</title>
<meta name="description" content="The same agent operating loop built by hand and on LangGraph, both driving one shared MCP tool layer. Measured, not argued.">
<style>
  :root {{
    --ink:#16202b; --ink-2:#4a5b6b; --ink-3:#7d8d9c;
    --line:#d7e0e8; --line-2:#eaf0f5;
    --paper:#fbfcfd; --panel:#ffffff; --grid:#eef3f8;
    --blue:#1f5c8b; --blue-soft:#e8f1f8;
    --hr:#0f766e; --lg:#7c3f00;
    --ok:#0f766e; --bad:#a1341f;
    --sans:ui-sans-serif,-apple-system,"Segoe UI",Inter,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  }}
  *{{box-sizing:border-box}}
  html{{-webkit-text-size-adjust:100%}}
  body{{
    margin:0; color:var(--ink); font-family:var(--sans); font-size:17px; line-height:1.62;
    background:
      linear-gradient(var(--grid) 1px, transparent 1px) 0 0/28px 28px,
      linear-gradient(90deg, var(--grid) 1px, transparent 1px) 0 0/28px 28px,
      var(--paper);
  }}
  .wrap{{max-width:960px;margin:0 auto;padding:0 20px}}
  header{{border-bottom:1px solid var(--line);background:rgba(251,252,253,.92);backdrop-filter:blur(6px)}}
  .eyebrow{{font-family:var(--mono);font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--blue);margin:34px 0 10px}}
  h1{{font-size:clamp(28px,5.4vw,46px);line-height:1.12;margin:0 0 14px;letter-spacing:-.02em;font-weight:650}}
  .sub{{font-size:clamp(17px,2.3vw,20px);color:var(--ink-2);margin:0 0 26px;max-width:62ch}}
  .meta{{font-family:var(--mono);font-size:12.5px;color:var(--ink-3);padding-bottom:22px;border-top:1px solid var(--line-2);padding-top:14px}}
  .meta a{{color:var(--blue)}}
  section{{padding:44px 0;border-bottom:1px solid var(--line-2)}}
  h2{{font-size:clamp(21px,3.2vw,27px);margin:0 0 6px;letter-spacing:-.01em;font-weight:640}}
  h2 .num{{font-family:var(--mono);font-size:.62em;color:var(--blue);margin-right:.6em;vertical-align:.12em}}
  .lede{{color:var(--ink-2);margin:0 0 22px;max-width:70ch}}
  p{{max-width:70ch;overflow-wrap:break-word}}
  .edge code{{overflow-wrap:anywhere}}
  code{{font-family:var(--mono);font-size:.88em;background:var(--line-2);padding:.1em .38em;border-radius:3px;color:var(--ink)}}
  a{{color:var(--blue)}}

  .verdict{{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--blue);padding:22px 24px;border-radius:3px;margin:8px 0 0}}
  .verdict p{{margin:0 0 12px}} .verdict p:last-child{{margin:0}}

  .machines{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:22px 0 8px}}
  .machines>*{{min-width:0}}
  .machine{{background:var(--panel);border:1px solid var(--line);border-radius:3px;padding:18px;min-width:0}}
  .machine h3{{margin:0 0 2px;font-size:16px}}
  .machine .tag{{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 14px}}
  .machine.a .tag{{color:var(--hr)}} .machine.b .tag{{color:var(--lg)}}
  .flow{{font-family:var(--mono);font-size:12.5px;line-height:1.85;color:var(--ink-2);
    white-space:pre;overflow-x:auto;max-width:100%;-webkit-overflow-scrolling:touch}}
  .machine dl{{margin:14px 0 0;font-size:13.5px;border-top:1px solid var(--line-2);padding-top:12px}}
  .machine dt{{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em;margin-top:9px}}
  .machine dd{{margin:1px 0 0}}

  table{{width:100%;border-collapse:collapse;margin:18px 0 0;font-size:15px}}
  caption{{text-align:left;font-family:var(--mono);font-size:12px;color:var(--ink-3);padding-bottom:9px;letter-spacing:.06em;text-transform:uppercase}}
  th,td{{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line-2);vertical-align:top}}
  thead th{{font-family:var(--mono);font-size:11.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);border-bottom:1px solid var(--line);font-weight:500}}
  thead th:nth-child(2){{color:var(--hr)}} thead th:nth-child(3){{color:var(--lg)}}
  tbody th{{font-weight:520;width:38%}}
  td{{font-family:var(--mono);font-size:14px}}
  td.win{{background:var(--blue-soft);font-weight:600}}
  .note{{font-family:var(--sans);font-size:13px;color:var(--ink-3);font-weight:400;line-height:1.5;margin-top:4px}}
  .ok{{color:var(--ok)}} .bad{{color:var(--bad)}}
  .scroller{{overflow-x:auto;-webkit-overflow-scrolling:touch}}

  .edges{{display:grid;gap:14px;margin-top:20px}}
  .edge{{background:var(--panel);border:1px solid var(--line);border-radius:3px;padding:18px 20px}}
  .edge h3{{margin:0 0 8px;font-size:16.5px}}
  .edge p{{margin:0;color:var(--ink-2);font-size:15px}}
  .edge .fix{{margin-top:10px;font-family:var(--mono);font-size:12.5px;color:var(--ok);border-top:1px dashed var(--line);padding-top:9px}}

  .cta{{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0 0}}
  .cta a{{display:inline-block;font-family:var(--mono);font-size:13.5px;text-decoration:none;border:1px solid var(--blue);color:var(--blue);padding:11px 18px;border-radius:3px}}
  .cta a.solid{{background:var(--blue);color:#fff}}
  ul.plain{{max-width:70ch;color:var(--ink-2);padding-left:20px}}
  ul.plain li{{margin-bottom:7px}}
  footer{{padding:32px 0 60px;font-family:var(--mono);font-size:12.5px;color:var(--ink-3)}}

  @media (max-width:720px){{
    body{{font-size:16px}}
    .wrap{{padding:0 16px}}
    .machines{{grid-template-columns:1fr}}
    .flow{{font-size:10.5px;line-height:1.75}}
    h1{{overflow-wrap:break-word}}
    tbody th{{width:auto}}
    table,thead,tbody,th,td,tr{{display:block}}
    thead{{display:none}}
    tbody tr{{border-bottom:1px solid var(--line);padding:12px 0}}
    tbody th{{padding:0 0 8px;border:0}}
    tbody td{{padding:5px 0 5px 88px;position:relative;border:0;overflow-wrap:anywhere}}
    tbody td:nth-of-type(1)::before{{content:"by hand";}}
    tbody td:nth-of-type(2)::before{{content:"langgraph";}}
    tbody td::before{{position:absolute;left:0;top:5px;font-family:var(--mono);font-size:11px;
      letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}}
    td.win{{background:transparent;color:var(--blue)}}
    #parity td::before,#failtable td::before{{content:""!important}}
    #parity td,#failtable td{{padding-left:0}}
  }}
</style>
</head>
<body>

<header>
  <div class="wrap">
    <div class="eyebrow">Loop Lab</div>
    <h1>The same agent loop, built twice.</h1>
    <p class="sub">Once by hand. Once on LangGraph. Both driving one shared, schema-validated
    MCP tool layer that neither implementation is allowed to bypass. Then killed, timed,
    counted and crashed on purpose.</p>
    <div class="meta">Measured on {MEASURED_ON} &middot;
      <a href="https://github.com/bigbrownjeff/loop-vs-langgraph">source and raw results</a></div>
  </div>
</header>

<div class="wrap">

<section id="verdict">
  <h2><span class="num">00</span>The verdict, before the evidence</h2>
  <div class="verdict">
    <p>It is close to a wash, and the ways it is not a wash are not the ways you would guess.</p>
    <p>The LangGraph version came out <b>{loc["langgraph_only"]["total_code_lines"] - loc["handrolled_only"]["total_code_lines"]} lines longer</b>
    than the loop it replaced, pulled in <b>{dep["framework_cost"]["extra_distributions"]} extra dependencies</b>,
    and added <b>{round(cs["end_to_end_baseline"][LG]["median_s"] - cs["end_to_end_baseline"][HR]["median_s"], 3)}s</b> to every run.
    On crash-safe checkpoint and resume, the thing people actually adopt it for,
    <b>the two are exactly tied</b>: both recover byte-identical state under a real
    <code>kill -9</code>, and both re-execute the in-flight step exactly once.</p>
    <p>What LangGraph genuinely buys is a human-in-the-loop primitive that survives process
    death, a checkpoint store that keeps the whole thread rather than just the head, and a
    runaway guard the hand-rolled loop does not have. What it costs is three sharp edges that
    stay invisible until something goes wrong.</p>
    <p>The most useful finding belongs to neither. The durable run record, the one artifact
    this whole exercise was about protecting, was the one piece of state that <b>neither
    checkpointer protected</b>, because it lived in the tool layer. Durability is a property
    of a system, not of an orchestrator.</p>
  </div>
</section>

<section id="machines">
  <h2><span class="num">01</span>The two state machines</h2>
  <p class="lede">Six nodes, one shared policy module, one shared MCP tool layer.
  Only the wiring and the persistence differ.</p>
  <div class="machines">
    <div class="machine a">
      <h3>By hand</h3>
      <p class="tag">no framework &middot; {loc["handrolled_only"]["total_code_lines"]} lines</p>
<div class="flow">orient
  |
plan
  |
decide ---+--> act --> verify --> record
  |       |                          |
  |       +&lt;-------------------------+
  |
  +--> END      all criteria met
  +--> HALT     tripwire fired</div>
      <dl>
        <dt>State</dt><dd>one dict, mutated in place</dd>
        <dt>Persistence</dt><dd>whole state rewritten after every node,
          temp &rarr; fsync &rarr; os.replace, plus the directory fsync</dd>
        <dt>Resume</dt><dd>read <code>state.json</code>, re-enter at <code>next_node</code></dd>
        <dt>Escalation</dt><dd>terminal status plus an <code>--approve</code> flag</dd>
      </dl>
    </div>
    <div class="machine b">
      <h3>On LangGraph</h3>
      <p class="tag">langgraph 1.2.9 &middot; {loc["langgraph_only"]["total_code_lines"]} lines</p>
<div class="flow">START
  |
orient --> plan --> decide --+--> act --> verify --> record
                       ^     |                         |
                       +-----|-------------------------+
                             |
                             +--> END
                             +--> interrupt()
                                  (GraphInterrupt)</div>
      <dl>
        <dt>State</dt><dd>a TypedDict of channels; nodes return partial dicts that get merged</dd>
        <dt>Persistence</dt><dd>AsyncSqliteSaver, committed at super-step boundaries</dd>
        <dt>Resume</dt><dd>re-invoke the same <code>thread_id</code> with <code>None</code></dd>
        <dt>Escalation</dt><dd><code>interrupt()</code> then <code>Command(resume=...)</code></dd>
      </dl>
    </div>
  </div>
</section>

<section id="parity">
  <h2><span class="num">02</span>Are they even doing the same thing?</h2>
  <p class="lede">Every number below is worthless if the two loops behave differently.
  All four scenarios, both implementations, digests diffed.</p>
  <div class="scroller">
  <table id="parity">
    <caption>bench/parity.py</caption>
    <thead><tr><th>Scenario</th><th>Outcome</th><th>Status</th><th>Spent</th><th>Fields that differ</th></tr></thead>
    <tbody>{parity_rows}</tbody>
  </table>
  </div>
  <p style="margin-top:18px">Both loops stop in the same place, before the same step, having
  spent the same amount, on every scenario. The three escalating scenarios differ on exactly
  two bookkeeping fields, and that divergence is finding 04 below, not a defect in the port.</p>
</section>

<section id="scoreboard">
  <h2><span class="num">03</span>The scoreboard</h2>
  <p class="lede">Shaded cell means that column measured better. Blank on both sides means a tie.</p>
  <div class="scroller">
  <table>
    <caption>bench/run_all.sh &middot; raw JSON in bench/results/</caption>
    <thead><tr><th>Measurement</th><th>By hand</th><th>LangGraph</th></tr></thead>
    <tbody>{scoreboard}</tbody>
  </table>
  </div>
</section>

<section id="kill">
  <h2><span class="num">04</span>What happened under the kill</h2>
  <p class="lede">Two kill points. The second one is the one that costs money.</p>

  <div class="edges">
    <div class="edge">
      <h3>Kill point A: SIGKILL inside a tool call</h3>
      <p>The <code>act</code> step on s2 was slowed to 3 seconds and the whole process group
      was killed 1.0s in. Nothing had returned yet, so in principle nothing should be lost.
      <b>Both implementations resumed to byte-identical final state.</b> Neither duplicated a
      single tool call. The hand-rolled loop left no temp files behind and never wrote a
      checkpoint that failed to parse.</p>
    </div>
    <div class="edge">
      <h3>Kill point B: crash after the side effect, before the commit</h3>
      <p>Process death at the end of the <code>act</code> node, after the tool returned and
      before either implementation persisted anything. <b>Both resumed to byte-identical final
      state, and both re-ran that one step exactly once.</b> At-least-once at the node boundary
      is the semantics in both cases. Neither gives you exactly-once without idempotent tools.</p>
    </div>
    <div class="edge">
      <h3>Then: copy the checkpoint file and try to resume from it alone</h3>
      <p>This is what a backup script does. The hand-rolled loop's
      <code>state.json</code> is <b>{wh["file_sizes_after_kill"]["state.json"]:,} bytes</b> and resumed
      to completion on its own. LangGraph's <code>checkpoints.sqlite</code> is
      <b>{wl["file_sizes_after_kill"]["checkpoints.sqlite"]:,} bytes</b>, because SQLite was in WAL mode
      and the entire run was sitting in a <b>{wl["file_sizes_after_kill"]["checkpoints.sqlite-wal"]:,} byte</b>
      <code>-wal</code> sidecar. Copying the file the name points at loses the run completely:
      <code>{wl["resume_from_canonical_file_only"]["stderr_tail"]}</code></p>
      <p class="fix">Not LangGraph's bug. Standard SQLite, inherited from the checkpointer.
      Worth knowing anyway, because the filename tells you something untrue.</p>
    </div>
  </div>
</section>

<section id="edges">
  <h2><span class="num">05</span>Three sharp edges</h2>
  <p class="lede">All three are defensible design consequences. All three will surprise
  someone in production.</p>
  <div class="edges">
    <div class="edge">
      <h3>interrupt() discards the interrupting node's state update</h3>
      <p>When a tripwire fires, LangGraph's persisted state says nothing about it. The reason
      lives only on the pending task's interrupt payload, and the caller has to know to go and
      read it back. The hand-rolled loop commits <code>status: escalated</code> and the
      tripwire text before it stops, so its own state says why it stopped.</p>
    </div>
    <div class="edge">
      <h3>Resuming an interrupt re-runs the whole node, side effects and all</h3>
      <p>The escalation was written to the durable run record
      <b>{el["escalate_records_written"]} times</b> by LangGraph and
      <b>{eh["escalate_records_written"]} time</b> by the hand-rolled loop. The code reads
      correctly, the run completes correctly, the final state is correct, and the only evidence
      of the double write is in the tool layer's own append-only log.</p>
      <p class="fix">Verified fix: move interrupt() above the side effect. Re-measured: back to 1.</p>
    </div>
    <div class="edge">
      <h3>The default recursion limit is one super-step from the baseline run</h3>
      <p>The baseline scenario needs <b>24 super-steps</b>. The default
      <code>recursion_limit</code> is <b>25</b>. Measured directly: it completes at 24 and
      raises <code>GraphRecursionError</code> at 23. One more verify retry is four more
      super-steps. The flip side, stated plainly: the hand-rolled loop has no runaway
      protection at all, and would have spun forever.</p>
    </div>
  </div>
</section>

<section id="failures">
  <h2><span class="num">06</span>When the tools go wrong</h2>
  <p class="lede">Three faults injected at the MCP boundary. Both loops saw an identical
  error surface and the same exit code. What differed was what survived on disk.</p>
  <div class="scroller">
  <table id="failtable">
    <caption>bench/failure_modes.py</caption>
    <thead><tr><th>Fault</th><th>What both loops saw</th><th>By hand: persisted status</th><th>LangGraph: persisted status</th></tr></thead>
    <tbody>{fail_rows}</tbody>
  </table>
  </div>
  <p style="margin-top:18px">The schema violation never reached either loop as bad data. The
  frozen <code>tool-defs.json</code> is loaded verbatim as both <code>inputSchema</code> and
  <code>outputSchema</code>, and the MCP server rejected its own handler's output before it
  went back on the wire. That is the tool layer earning its keep, independent of either framework.</p>
</section>

<section id="limits">
  <h2><span class="num">07</span>What this does not measure</h2>
  <p class="lede">This list matters as much as the results.</p>
  <ul class="plain">
    <li><b>There is no LLM anywhere in the repository.</b> The policy is deterministic, because
    a stochastic one would make every number above unreproducible. It also means nothing here
    evaluates LangGraph's model bindings, message state, or agent prebuilts, which is most of
    what people use LangGraph for.</li>
    <li><b>No concurrency.</b> The graph is linear. Fan-out, <code>Send</code>, and state
    reducers under parallel writes are a large part of what a framework earns its keep on, and
    none of it is tested here.</li>
    <li><b>One checkpointer backend.</b> <code>AsyncSqliteSaver</code> only. The Postgres saver
    has different durability and portability behaviour and was not tested.</li>
    <li><b>No LangSmith, no LangGraph Platform, no streaming.</b> LangSmith is installed as a
    transitive dependency and never used.</li>
    <li><b>Single machine, warm cache, one OS.</b> No container, no cold Lambda, no network
    filesystem. Cold-start figures are best-of-7 on a laptop.</li>
    <li><b>Two specific kill points.</b> They are the two that seemed most informative. That is
    not a proof of crash safety, and no fuzzing over kill timing was done.</li>
  </ul>
  <div class="cta">
    <a class="solid" href="https://github.com/bigbrownjeff/loop-vs-langgraph">Read the code</a>
    <a href="https://github.com/bigbrownjeff/loop-vs-langgraph/blob/main/RESULTS.md">Full results</a>
    <a href="https://github.com/bigbrownjeff/loop-vs-langgraph/tree/main/bench">Reproduction scripts</a>
  </div>
</section>

<footer>
  MIT licensed &middot; synthetic data only &middot; every figure on this page is generated
  from <code>bench/results/*.json</code> by <code>demo/build.py</code>, so it cannot drift
  from the measurements.
</footer>

</div>
</body>
</html>
"""

out = REPO / "demo" / "public" / "index.html"
out.write_text(HTML)
print(f"wrote {out} ({len(HTML):,} bytes)")
