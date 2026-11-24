"""The client's settings: one small file in the user's own config folder.

    server        the Scriptorium server's address
    api_key       the member's API key
    reply_chars   the most characters of a result a tool reply may hold;
                  a larger result is saved whole to a file instead
    results_dir   where such results (and sets) are saved
    pdf_dir       where downloaded PDFs are saved
    notice_accepted   set by `scriptorium-client notice --accept`: the date and
                  the notice text the member accepted, for agent apps that
                  cannot ask
    admin         "yes" shows the admin menu from the start of a session. The
                  client keeps its own note of whether the server counts the
                  member as an admin, so this is only for agent apps that
                  keep the client from its files

The file holds the API key, so it is written readable by its owner only.

Each setting can also be given as an environment variable, SCRIPTORIUM_ and
the setting's name in capitals (SCRIPTORIUM_SERVER, SCRIPTORIUM_API_KEY, ...),
which then takes the file's place for that setting. This serves agent apps
that start the client where it cannot read the user's files.
"""

import os
import tomllib
from pathlib import Path

import platformdirs

DEFAULTS = {"server": "", "api_key": "", "reply_chars": 24000, "results_dir": "", "pdf_dir": "",
            "notice_accepted": "", "admin": ""}


def settings_path() -> Path:
    """SCRIPTORIUM_CLIENT_SETTINGS, else client.toml in the user's config folder."""
    override = os.environ.get("SCRIPTORIUM_CLIENT_SETTINGS")
    return Path(override) if override else Path(platformdirs.user_config_dir("scriptorium")) / "client.toml"


def variable(name: str) -> str:
    """The environment variable that gives the setting `name`."""
    return "SCRIPTORIUM_" + name.upper()


def load(path: Path | None = None, environment: bool = True) -> dict:
    """The settings in force: the environment's values over the file's over
    the defaults, with the two folders filled in when none is named.
    `environment=False` leaves the environment out, for writing the file."""
    path = path or settings_path()
    values = dict(DEFAULTS)
    try:
        stored = tomllib.loads(path.read_text()) if path.exists() else {}
    except OSError:  # a file this process may not read counts as absent
        stored = {}
    values.update({k: v for k, v in stored.items() if k in DEFAULTS})
    for name, default in DEFAULTS.items() if environment else ():
        given = os.environ.get(variable(name), "").strip()
        if given:
            values[name] = int(given) if isinstance(default, int) else given
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
