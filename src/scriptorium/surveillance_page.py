"""The surveillance viewer: one self-contained HTML page served by the daemon.

Shows the latest surveillance run (accepted papers, per-venue table) with a
run history to browse earlier days. Accepted papers carry checkboxes and a
"Copy ingest handoff" button that puts a ready-made /scriptorium:ingest
prompt on the clipboard — the report is a triage surface; library writes
stay in Claude sessions. Data comes from /surveillance.json (same origin);
visual identity matches the settings page. The data functions below
build /surveillance.json and the dashboard's surveillance card.
"""

import datetime
import json

from scriptorium.config import Config
from scriptorium.runs import read_manifest
from scriptorium.schedule import schedule_status
from scriptorium.settings import load_settings
from scriptorium.surveillance import state_path


def runs_payload(config: Config, chosen: str | None) -> dict:
    """Run history plus one run's accepted papers and venue summary.
    `chosen` is a run folder's name from the request; anything that is not
    an existing run folder's exact name (a path, say) selects nothing."""
    runs_dir = config.index_dir / "surveillance" / "runs"
    runs = []
    harvests: dict[str, dict] = {}
    for d in sorted(runs_dir.iterdir(), reverse=True) if runs_dir.exists() else []:
        manifest = read_manifest(d) if d.is_dir() else {}
        if "harvest" not in manifest:
            continue  # stopped before harvesting: nothing to show
        harvests[d.name] = manifest["harvest"]
        try:
            accepted = json.loads((d / "accepted.json").read_text())
        except (OSError, ValueError):
            accepted = []
        runs.append({"name": d.name, "accepted": len(accepted),
                     "status": manifest.get("status")})
        if len(runs) >= 30:
            break

    chosen = chosen or (runs[0]["name"] if runs else None)
    # Only an existing run directory's exact name: never a path.
    if chosen and (not runs_dir.exists()
                   or chosen not in {d.name for d in runs_dir.iterdir() if d.is_dir()}):
        chosen = None
    payload: dict = {
        "enabled": bool(load_settings(config)["surveillance"]["enabled"]),
        "runs": runs,
        "run": None,
    }
    if chosen in harvests:
        run_dir = runs_dir / chosen
        payload["run"] = chosen
        payload["summary"] = harvests[chosen]
        try:
            payload["accepted"] = json.loads((run_dir / "accepted.json").read_text())
        except (OSError, ValueError):
            payload["accepted"] = []
        try:
            payload["added"] = json.loads((run_dir / "added.json").read_text())["created"]
        except (OSError, ValueError, KeyError):
            payload["added"] = {}
    return payload


def dashboard_card(config: Config) -> dict:
    """The dashboard's surveillance card: switch, last run, its digest and
    the schedule."""
    surveil_dir = config.index_dir / "surveillance"
    last_run = None
    try:
        last_run = json.loads(state_path(config).read_text()).get("last_run")
    except (OSError, ValueError):
        pass
    run = (last_run or {}).get("run")
    has_digest = bool(run) and (surveil_dir / "runs" / run / "digest.md").is_file()
    settings = load_settings(config)["surveillance"]
    schedule = schedule_status(
        last_run["date"] if last_run else None,
        datetime.datetime.now(),
        int(settings["nightly_hour"]),
        int(settings["catchup_days"]),
    )
    return {
        "enabled": bool(settings["enabled"]),
        "last_run": last_run,
        "digest": run if has_digest else None,
        "schedule": schedule,
    }


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>scriptorium surveillance</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 -apple-system, sans-serif; max-width: 760px;
         margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; } h1 small { font-weight: normal; opacity: .6; }
  h2 { font-size: 1.05rem; margin: 1.6rem 0 .6rem; }
  .sub { opacity: .65; margin: -0.4rem 0 0; }
  .runbar { display: flex; gap: .4rem; flex-wrap: wrap; margin: 1.2rem 0;
            border-bottom: 1px solid color-mix(in srgb, currentColor 20%, transparent);
            padding-bottom: .8rem; }
  .runbar button { border: none; background: none; font: inherit; padding: .3rem .7rem;
                   cursor: pointer; opacity: .55; border-radius: 6px; }
  .runbar button.active { opacity: 1; background: color-mix(in srgb, #4a9 18%, transparent);
                          font-weight: 600; }
  table { border-collapse: collapse; width: 100%; font-size: .92em; }
  th, td { text-align: left; padding: .3rem .6rem;
           border-bottom: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
  th { opacity: .6; font-weight: 600; }
  td.num, th.num { text-align: right; }
  .card { padding: .7rem .9rem; margin: .6rem 0; border-radius: 8px;
          border: 1px solid color-mix(in srgb, currentColor 15%, transparent);
          display: flex; gap: .7rem; align-items: baseline; }
  .card input { margin-top: .25rem; }
  .card .inlib { color: #4a9; font-weight: 700; }
  .card .body { flex: 1; }
  .card .title { font-weight: 600; }
  .card .meta { opacity: .65; font-size: .9em; }
  .card .why { margin-top: .25rem; font-size: .93em; }
  .why.expansion { color: #4a9; }
  .empty { opacity: .6; padding: 2rem 0; }
  footer { margin-top: 1.6rem; display: flex; align-items: center; gap: 1rem; }
  footer button { font: inherit; padding: .5rem 1.4rem; cursor: pointer;
                  background: #4a9; color: white; border: none; border-radius: 6px; }
  footer button:disabled { opacity: .45; cursor: default; }
  #status { opacity: .7; }
  a { color: inherit; }
</style>
</head>
<body>
<h1>scriptorium <small>surveillance</small></h1>
<p class="sub" id="headline">Loading…</p>

<div class="runbar" id="runbar"></div>
<div id="content"></div>

<footer>
  <button id="copy" disabled>Copy ingest handoff</button><span id="status"></span>
</footer>

<script>
let current = null;

// All data from Crossref, the LLM judges and run files is text: strip any
// markup (Crossref titles carry <i>, <sub>, MathML) and escape the rest
// before it enters HTML.
function esc(v) {
  return String(v ?? "").replace(/<[^>]*>/g, "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function loadRun(name) {
  const url = name ? `/surveillance.json?run=${encodeURIComponent(name)}` : "/surveillance.json";
  const d = await (await fetch(url)).json();
  current = d;
  document.getElementById("headline").textContent = d.runs.length
    ? `Nightly monitoring is ${d.enabled ? "on" : "off"} · ${d.runs.length} run(s) recorded`
    : "No surveillance runs recorded yet.";

  const bar = document.getElementById("runbar");
  bar.innerHTML = "";
  d.runs.forEach(r => {
    const b = document.createElement("button");
    b.textContent = `${r.name} (${r.accepted})`;
    if (r.name === d.run) b.classList.add("active");
    b.onclick = () => loadRun(r.name);
    bar.appendChild(b);
  });

  const c = document.getElementById("content");
  c.innerHTML = "";
  if (!d.run) { c.innerHTML = '<p class="empty">Nothing to show.</p>'; update(); return; }

  const s = d.summary;
  c.insertAdjacentHTML("beforeend",
    `<h2>${esc(d.run)}</h2><p class="sub">${esc(s.candidates)} new candidates ` +
    `(${esc(s.mega_candidates)} megajournal, ${esc(s.field_candidates)} field) · ` +
    `${d.accepted.length} accepted</p>`);

  if (d.accepted.length) {
    c.insertAdjacentHTML("beforeend", "<h2>Accepted</h2>");
    d.accepted.forEach((a, i) => {
      const exp = (a.why || "").startsWith("expansion:");
      const inLib = (d.added || {})[a.doi.toLowerCase()];
      c.insertAdjacentHTML("beforeend", `
        <label class="card">${inLib
          ? `<span class="inlib" title="${esc(inLib.citation_key)}">✓</span>`
          : `<input type="checkbox" data-i="${i}" checked>`}
          <span class="body">
            <span class="title">${esc(a.title)}</span>
            <div class="meta">${esc(a.authors || "authors unknown")} · ${esc(a.venue)} ·
              <a href="https://doi.org/${esc(a.doi)}" target="_blank">${esc(a.doi)}</a></div>
            <div class="why${exp ? " expansion" : ""}">${esc(a.why)}</div>
          </span></label>`);
    });
  } else {
    c.insertAdjacentHTML("beforeend", '<p class="empty">No papers accepted in this run.</p>');
  }

  const venues = (s.venues || []).filter(v => !v.error);
  if (venues.length) {
    c.insertAdjacentHTML("beforeend",
      "<h2>Venues</h2><table><tr><th>venue</th><th class=num>detected</th>" +
      "<th class=num>new</th></tr>" +
      venues.sort((a, b) => b.new - a.new).map(v =>
        `<tr><td>${esc(v.venue)}</td><td class=num>${esc(v.detected)}</td>` +
        `<td class=num>${esc(v.new)}</td></tr>`).join("") + "</table>");
  }
  const errs = (s.venues || []).filter(v => v.error);
  if (errs.length) {
    c.insertAdjacentHTML("beforeend", "<h2>Venue errors</h2>" +
      errs.map(v => `<p class="sub">${esc(v.venue)}: ${esc(v.error)}</p>`).join(""));
  }
  c.addEventListener("change", update);
  update();
}

function selected() {
  if (!current) return [];
  return [...document.querySelectorAll("input[data-i]:checked")]
    .map(el => current.accepted[Number(el.dataset.i)]);
}

function update() {
  const n = selected().length;
  const btn = document.getElementById("copy");
  btn.disabled = n === 0;
  btn.textContent = n ? `Copy ingest handoff (${n})` : "Copy ingest handoff";
}

document.getElementById("copy").onclick = async () => {
  const picks = selected();
  const text = "/scriptorium:ingest — reference mode. Add these surveillance accepts "
    + "(Crossref-originated, so Crossref verification alone suffices); place each in "
    + "its venue's collection per the surveillance registry:\\n"
    + picks.map(a => `- ${a.doi} — ${a.title} (${a.venue})`).join("\\n");
  await navigator.clipboard.writeText(text);
  const status = document.getElementById("status");
  status.textContent = `Copied ${picks.length} paper(s) — paste into a Claude session.`;
  setTimeout(() => status.textContent = "", 4000);
};

loadRun(null);
</script>
</body>
</html>"""
