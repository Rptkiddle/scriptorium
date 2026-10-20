"""The dashboard: scriptorium's home page, served by the daemon at /.

One page with three parts: live status cards (hub, surveillance, missing
PDFs), quick links to every other page, and the reference documentation —
an architecture overview plus a settings reference rendered from
settings.SCHEMA, so the docs always match what the settings page offers.
Status data loads from /dashboard.json; the missing-PDF count loads lazily
from /pdfs.json because it queries Zotero live.
"""

from html import escape

from scriptorium.settings import DEFAULTS, SCHEMA

_STYLE = """
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 -apple-system, sans-serif; max-width: 760px;
         margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; } h1 small { font-weight: normal; opacity: .6; }
  h2 { font-size: 1.05rem; margin: 2rem 0 .6rem; }
  a { color: #4a9; text-decoration: none; } a:hover { text-decoration: underline; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
           gap: .8rem; margin: 1.2rem 0; }
  .card { border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
          border-radius: 8px; padding: .8rem 1rem; }
  .card h3 { margin: 0 0 .4rem; font-size: .95rem; }
  .card .big { font-size: 1.5rem; font-weight: 600; }
  .card small { display: block; opacity: .65; }
  .ok { color: #3a8a5f; } .warn { color: #c08a2d; } .bad { color: #c0392b; }
  table { border-collapse: collapse; width: 100%; font-size: .92em; }
  th, td { text-align: left; padding: .35rem .6rem; vertical-align: top;
           border-bottom: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
  th { opacity: .65; font-weight: 600; }
  td.key { white-space: nowrap; font-family: ui-monospace, monospace; font-size: .9em; }
  details { margin: .6rem 0; }
  summary { cursor: pointer; font-weight: 600; padding: .3rem 0; }
  .blurb { opacity: .7; margin: .2rem 0 .6rem; }
  #status-note { opacity: .6; }
"""


def _settings_reference() -> str:
    parts = []
    for name, spec in SCHEMA.items():
        if not spec["fields"]:
            continue
        rows = "\n".join(
            f'<tr><td class="key">{name}.{f["key"]}</td>'
            f"<td>{escape(f['label'])}</td>"
            f"<td>{escape(f.get('doc', ''))}</td>"
            f"<td>{escape(str(DEFAULTS[name][f['key']]))}</td></tr>"
            for f in spec["fields"]
        )
        parts.append(
            f'<details><summary>{escape(spec["title"])}</summary>'
            f'<p class="blurb">{escape(spec.get("blurb", ""))}</p>'
            f"<table><tr><th>setting</th><th>name</th><th>meaning</th>"
            f"<th>default</th></tr>{rows}</table></details>"
        )
    return "\n".join(parts)


_OVERVIEW = """
<details><summary>How scriptorium fits together</summary>
<p class="blurb">A hub-and-spoke system over your Zotero library.</p>
<table>
<tr><th>part</th><th>what it does</th></tr>
<tr><td class="key">hub (daemon)</td><td>The always-on core: a semantic index of the
library (metadata and PDF full text) behind MCP tools that Claude Code sessions
call, plus these web pages. Auto-syncs when Zotero's library changes.</td></tr>
<tr><td class="key">surveillance</td><td>Nightly sweep of subscribed venues
(the [&#10003;]-prefixed collections in Zotero). Harvests papers published since
the last successful run (missed nights are caught up, up to the catch-up window) via
Crossref, judges them in two LLM stages (Filter, Appraise) against your research profile, writes a
report and digest, and — when enabled — adds accepts to Zotero (metadata only;
PDFs are left to retrieve).</td></tr>
<tr><td class="key">search</td><td>Spot searches over the library: whether a
paper is there (DOI, arXiv ID, title), everything by an author or in a venue or
year, counts, topical searches with filters, passages. Invoked as
/scriptorium:search.</td></tr>
<tr><td class="key">interrogation</td><td>Citation-grounded answers from the
library: retrieval, per-chunk relevance scoring, and answer writing, following
PaperQA2's procedure. Invoked as /scriptorium:interrogate.</td></tr>
<tr><td class="key">retrieve</td><td>PDF acquisition for items that lack one: the
version of record downloaded from each publisher's site (from the VU network),
staged in a run and attached to Zotero on your go; no preprint PDFs. Invoked as
/scriptorium:retrieve.</td></tr>
<tr><td class="key">ingest</td><td>Adding papers properly: verified metadata,
collection placement, PDFs as linked files, indexing. Invoked as
/scriptorium:ingest.</td></tr>
<tr><td class="key">discovery</td><td>Structured literature discovery beyond the
library.</td></tr>
</table></details>
"""

PAGE = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>scriptorium</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>scriptorium <small>literature hub</small></h1>
<p style="opacity:.65;margin:-0.4rem 0 0">
  <a href="/surveillance">surveillance</a> · <a href="/pdfs">missing PDFs</a> ·
  <a href="/settings">settings</a></p>

<div class="cards">
  <div class="card"><h3>Hub</h3>
    <div class="big" id="hub-state">…</div>
    <small id="hub-detail"></small></div>
  <div class="card"><h3>Zotero</h3>
    <div class="big" id="zotero-state">…</div>
    <small id="zotero-detail"></small></div>
  <div class="card"><h3>Surveillance</h3>
    <div class="big" id="surveillance-state">…</div>
    <small id="surveillance-detail"></small></div>
  <div class="card"><h3>Missing PDFs</h3>
    <div class="big" id="pdfs-state">…</div>
    <small id="pdfs-detail">counting (queries Zotero live)</small></div>
</div>
<p id="status-note"></p>

<h2>Reference</h2>
{_OVERVIEW}
{_settings_reference()}

<script>
function set(id, text, cls) {{
  const e = document.getElementById(id); e.textContent = text;
  if (cls) e.className = "big " + cls;
}}
async function loadStatus() {{
  try {{
    const d = await (await fetch("/dashboard.json")).json();
    set("hub-state", d.hub.papers + " papers", "ok");
    document.getElementById("hub-detail").textContent =
      d.hub.catalogue_items + " items · " + d.hub.chunks + " chunks" +
      (d.hub.failed ? " · " + d.hub.failed + " failed" : "");
    if (!d.zotero.reachable) set("zotero-state", "closed", "bad");
    else if (d.zotero.index_behind) set("zotero-state", "index behind", "warn");
    else set("zotero-state", "in sync", "ok");
    document.getElementById("zotero-detail").textContent =
      d.zotero.reachable ? "library version " + d.zotero.library_version
                         : "start Zotero for writes and live views";
    const s = d.surveillance, sc = s.schedule;
    const ddmmyy = iso => iso.slice(8, 10) + "-" + iso.slice(5, 7) + "-" + iso.slice(2, 4);
    const next = ddmmyy(sc.next_run) + " " + sc.next_run.slice(11, 16);
    let detail;
    if (!s.enabled) {{
      set("surveillance-state", "off", "warn");
      detail = "nightly runs disabled in settings";
    }} else if (sc.missed) {{
      set("surveillance-state", "missed", "bad");
      detail = "gap exceeds the catch-up window — some days need a manual harvest";
    }} else if (sc.pending) {{
      set("surveillance-state", sc.pending + (sc.pending == 1 ? " night" : " nights") + " pending", "warn");
      detail = "caught up at the next run, " + next;
    }} else {{
      set("surveillance-state", "scheduled", "ok");
      detail = "next run " + next;
    }}
    if (s.last_run) {{
      detail += "<br>last run " + ddmmyy(s.last_run.date) + " · " +
        s.last_run.candidates + " candidates · " + s.last_run.accepted + " accepted" +
        (s.digest ? ' · <a href="/digest">digest</a>' : "");
    }}
    document.getElementById("surveillance-detail").innerHTML = detail;
  }} catch (e) {{
    set("hub-state", "unreachable", "bad");
    document.getElementById("status-note").textContent = String(e);
  }}
}}
async function loadPdfs() {{
  try {{
    const d = await (await fetch("/pdfs.json")).json();
    if (d.error) {{ set("pdfs-state", "?", "warn");
      document.getElementById("pdfs-detail").textContent = d.error; return; }}
    set("pdfs-state", String(d.items.length), d.items.length ? "warn" : "ok");
    document.getElementById("pdfs-detail").innerHTML =
      'items without a usable PDF · <a href="/pdfs">manage</a>';
  }} catch (e) {{
    set("pdfs-state", "?", "warn");
    document.getElementById("pdfs-detail").textContent = "count failed: " + e;
  }}
}}
loadStatus(); loadPdfs();
</script>
</body>
</html>"""


PDFS_PAGE = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>scriptorium · missing PDFs</title>
<style>{_STYLE}</style>
</head>
<body>
<h1><a href="/" style="color:inherit">scriptorium</a> <small>missing PDFs</small></h1>
<p class="blurb">Every regular item without a usable PDF, live from Zotero,
including surveillance additions (which arrive metadata-only). Run
/scriptorium:retrieve to download versions of record from publishers' sites; for
the rest, copy the click-list, download into
<span class="key">~/Desktop/to_file/</span> with your browser, then run
/scriptorium:ingest (mop-up) to attach and index.</p>
<p><button id="copy">Copy click-list</button>
   <span id="note">loading (queries Zotero live)…</span></p>
<table id="tbl" style="display:none">
<tr><th>key</th><th>title</th><th>venue</th><th>status</th><th>DOI</th></tr>
</table>
<script>
let rows = [];
async function load() {{
  const d = await (await fetch("/pdfs.json")).json();
  const note = document.getElementById("note");
  if (d.error) {{ note.textContent = d.error; return; }}
  rows = d.items;
  note.textContent = rows.length + " items · statuses: " +
    Object.entries(d.by_status).map(([k, v]) => k + " " + v).join(", ");
  const tbl = document.getElementById("tbl");
  tbl.style.display = "";
  for (const r of rows) {{
    const tr = tbl.insertRow();
    tr.insertCell().textContent = r.citation_key || r.key;
    tr.insertCell().textContent = r.title;
    tr.insertCell().textContent = r.venue || "";
    tr.insertCell().textContent = r.status;
    const c = tr.insertCell();
    if (r.doi) {{
      const a = document.createElement("a");
      a.href = "https://doi.org/" + r.doi;
      a.target = "_blank";
      a.textContent = r.doi;
      c.appendChild(a);
    }}
  }}
}}
document.getElementById("copy").onclick = () => {{
  const text = rows.map(r => "- [" + (r.citation_key || r.key) + "] " +
    r.title + (r.doi ? " — https://doi.org/" + r.doi : "")).join("\\n");
  navigator.clipboard.writeText(text);
  document.getElementById("note").textContent = "click-list copied (" + rows.length + " items)";
}};
load();
</script>
</body>
</html>"""
