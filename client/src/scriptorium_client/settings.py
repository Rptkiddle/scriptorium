"""The client's settings: one small file in the user's own config folder.

    server        the Scriptorium server's address
    api_key       the member's API key
    reply_chars   the most characters of a result a tool reply may hold;
                  a larger result is saved whole to a file instead
    results_dir   where such results (and sets) are saved
    pdf_dir       where downloaded PDFs are saved

The file holds the API key, so it is written readable by its owner only.
"""

import os
import tomllib
from pathlib import Path

import platformdirs

DEFAULTS = {"server": "", "api_key": "", "reply_chars": 24000, "results_dir": "", "pdf_dir": ""}


def settings_path() -> Path:
    """SCRIPTORIUM_CLIENT_SETTINGS, else client.toml in the user's config folder."""
    override = os.environ.get("SCRIPTORIUM_CLIENT_SETTINGS")
    return Path(override) if override else Path(platformdirs.user_config_dir("scriptorium")) / "client.toml"


def load(path: Path | None = None) -> dict:
    """The settings in force: the file's values over the defaults, with the
    two folders filled in when the file names none."""
    path = path or settings_path()
    values = dict(DEFAULTS)
    if path.exists():
        stored = tomllib.loads(path.read_text())
        values.update({k: v for k, v in stored.items() if k in DEFAULTS})
    data = Path(platformdirs.user_data_dir("scriptorium"))
    values["results_dir"] = values["results_dir"] or str(data / "results")
    values["pdf_dir"] = values["pdf_dir"] or str(data / "pdfs")
    return values


def save(values: dict, path: Path | None = None) -> Path:
    """Write the settings that differ from the defaults; the file is
    readable by its owner only."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, default in DEFAULTS.items():
        value = values.get(name, default)
        if value == default:
            continue
        if isinstance(default, int):
            lines.append(f"{name} = {int(value)}")
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{name} = "{escaped}"')
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
