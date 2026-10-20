"""Web pages: data from Crossref, judges and run files never enters HTML raw."""

import re

from scriptorium.dashboard_page import PDFS_PAGE
from scriptorium.surveillance_page import PAGE as SURVEILLANCE_PAGE


def test_surveillance_page_escapes_every_data_field():
    # Every ${...} interpolation of a data field that reaches HTML goes
    # through esc(). Excluded: the clipboard handoff (plain text), numeric
    # .length values, and the headline (set via textContent).
    html_part = SURVEILLANCE_PAGE.split('document.getElementById("copy").onclick')[0]
    raw = [m for m in re.findall(r"\$\{(?!esc\()((?:a|v|d|s|inLib)\.[^}]*)\}", html_part)
           if not m.endswith(".length") and not m.startswith("d.enabled")]
    assert raw == [], raw


def test_pdfs_page_builds_links_without_innerhtml():
    assert "innerHTML" not in PDFS_PAGE


def test_no_script_runs_on_import():
    # Loading a script (e.g. to test one function) must never execute it:
    # at top level only definitions, imports, assignments and the
    # __main__ guard are allowed.
    import ast
    from pathlib import Path
    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
               ast.ClassDef, ast.Assign, ast.AnnAssign)
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    offenders = []
    for p in sorted(scripts.glob("*.py")):
        body = ast.parse(p.read_text()).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # the docstring
        for node in body:
            guard = (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                     and getattr(node.test.left, "id", "") == "__name__")
            if not (isinstance(node, allowed) or guard):
                offenders.append(f"{p.name}:{node.lineno}")
    assert offenders == [], offenders
