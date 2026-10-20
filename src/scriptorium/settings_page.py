"""The settings GUI: one self-contained HTML page served by the daemon.

One tab per section, rendered from settings.SCHEMA — labels, help text,
control types, and bounds all come from that one structure, so the page
doubles as the settings documentation. Values load from /settings.json and
save via POST /settings. No external assets; works in any browser on
localhost.
"""

from html import escape

from scriptorium.settings import _INT_BOUNDS, SCHEMA

_HEAD = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>scriptorium settings</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 -apple-system, sans-serif; max-width: 640px;
         margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; } h1 small { font-weight: normal; opacity: .6; }
  h1 a { text-decoration: none; color: inherit; }
  nav { display: flex; gap: .4rem; margin: 1.2rem 0; border-bottom: 1px solid
        color-mix(in srgb, currentColor 20%, transparent); }
  nav button { border: none; background: none; font: inherit; padding: .45rem .9rem;
               cursor: pointer; opacity: .55; border-bottom: 2px solid transparent; }
  nav button.active { opacity: 1; border-bottom-color: #4a9; font-weight: 600; }
  section { display: none; } section.active { display: block; }
  section > p.blurb { opacity: .65; margin: 0 0 .6rem; }
  .row { display: flex; align-items: baseline; justify-content: space-between;
         gap: 1rem; padding: .55rem 0; }
  .row label { flex: 1; }
  .row small, .col small { display: block; opacity: .6; font-weight: normal; }
  .row input, .row select { width: 9rem; font: inherit; padding: .25rem .4rem; }
  .row input.wide { width: 16rem; }
  .col { padding: .55rem 0; }
  .col textarea { width: 100%; min-height: 10rem; font: inherit;
                  padding: .4rem .5rem; box-sizing: border-box; }
  .empty { opacity: .6; padding: 2rem 0; }
  footer { margin-top: 1.6rem; display: flex; align-items: center; gap: 1rem; }
  footer button { font: inherit; padding: .5rem 1.4rem; cursor: pointer;
                  background: #4a9; color: white; border: none; border-radius: 6px; }
  #status { opacity: .7; }
</style>
</head>
<body>
<h1><a href="/">scriptorium</a> <small>settings</small></h1>
<p style="opacity:.65;margin:-0.4rem 0 0">One tab per part: the hub (daemon, index, menubar) and its spokes.</p>
"""

_FOOT = """
<footer><button id="save">Save</button><span id="status"></span></footer>

<script>
const MODELS = ["haiku", "sonnet", "opus", "fable"];
document.querySelectorAll("select.model[data-key]").forEach(s => {
  MODELS.forEach(m => s.add(new Option(m, m)));
});
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button, section").forEach(e => e.classList.remove("active"));
  b.classList.add("active");
  document.getElementById(b.dataset.tab).classList.add("active");
});
function fields() { return document.querySelectorAll("[data-key]"); }
async function load() {
  const s = await (await fetch("/settings.json")).json();
  fields().forEach(f => {
    const [sec, key] = f.dataset.key.split(".");
    if (!(s[sec] && key in s[sec])) return;
    if (f.type === "password") {
      f.value = "";
      f.placeholder = s[sec][key] === "__set__"
        ? "configured — leave blank to keep" : "not set";
    } else f.value = s[sec][key];
  });
}
document.querySelectorAll("button.clear").forEach(b => b.onclick = () => {
  const f = b.parentElement.querySelector("input");
  f.value = "__clear__"; f.type = "text";
});
document.getElementById("save").onclick = async () => {
  const out = {};
  fields().forEach(f => {
    const [sec, key] = f.dataset.key.split(".");
    if (f.type === "number" && f.value.trim() === "") return;  // blank: keep stored
    (out[sec] ||= {})[key] = f.type === "number" ? Number(f.value) : f.value;
  });
  const r = await fetch("/settings", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(out) });
  const status = document.getElementById("status");
  const body = await r.json();
  if (!r.ok) { status.textContent = "Error: " + body.error; }
  else if (body.warning) { status.textContent = "Saved, but: " + body.warning; }
  else { status.textContent = "Saved."; setTimeout(() => status.textContent = "", 2500); }
};
load();
</script>
</body>
</html>"""


def _control(section: str, f: dict) -> str:
    key = f'data-key="{section}.{f["key"]}"'
    if f["kind"] == "onoff":
        return (f'<select {key}><option value="0">off</option>'
                f'<option value="1">on</option></select>')
    if f["kind"] == "int":
        lo, hi = _INT_BOUNDS[f["key"]]
        return f'<input type="number" {key} min="{lo}" max="{hi}">'
    if f["kind"] == "model":
        return f'<select class="model" {key}></select>'
    if f["kind"] == "secret":
        return (f'<span style="display:flex;gap:.4rem">'
                f'<input type="password" class="wide" {key} autocomplete="off">'
                f'<button type="button" class="clear" title="Forget the stored key">'
                f'clear</button></span>')
    if f["kind"] == "text":
        return f'<input type="text" class="wide" {key}>'
    if f["kind"] == "textarea":
        return f'<textarea {key}></textarea>'
    raise ValueError(f"unknown field kind: {f['kind']}")


def _field(section: str, f: dict) -> str:
    label = escape(f["label"])
    doc = f'\n    <small>{escape(f["doc"])}</small>' if f.get("doc") else ""
    if f["kind"] == "textarea":
        return (f'  <div class="col"><label>{label}{doc}</label>\n'
                f'    {_control(section, f)}</div>')
    return (f'  <div class="row"><label>{label}{doc}</label>\n'
            f'    {_control(section, f)}</div>')


def _section(name: str, spec: dict, active: bool) -> str:
    cls = ' class="active"' if active else ""
    body = "\n".join(_field(name, f) for f in spec["fields"]) or (
        '  <p class="empty">No settings yet.</p>')
    blurb = f'  <p class="blurb">{escape(spec["blurb"])}</p>\n' if spec.get("blurb") else ""
    return f'<section id="{name}"{cls}>\n{blurb}{body}\n</section>'


def _nav() -> str:
    buttons = "\n".join(
        f'  <button data-tab="{name}"{" class=" + chr(34) + "active" + chr(34) if i == 0 else ""}>'
        f'{escape(spec["title"])}</button>'
        for i, (name, spec) in enumerate(SCHEMA.items())
    )
    return f"<nav>\n{buttons}\n</nav>"


PAGE = (
    _HEAD
    + _nav()
    + "\n\n"
    + "\n\n".join(
        _section(name, spec, i == 0) for i, (name, spec) in enumerate(SCHEMA.items())
    )
    + _FOOT
)
