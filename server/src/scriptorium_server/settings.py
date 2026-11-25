"""The server's settings: one TOML file, shown and changed through the admin
command.

Every setting is defined once below, with its default, the check a value
must pass and a one-line explanation. The file holds only the values that
differ from the defaults. Its path is the SCRIPTORIUM_SETTINGS environment
variable, else /etc/scriptorium/settings.toml. Two settings can also come
from the environment (ENVIRONMENT), so a container deployment sets them in
one place: SCRIPTORIUM_DATABASE_ADDRESS (with the database password) and
SCRIPTORIUM_GROBID_ADDRESS (GROBID's container). The file can hold keys for
outside sources, so it is written readable by its owner only, and secret
values are shown as "set" or "not set".

Each setting also says when a change takes effect (`effect`) and whether
an admin may change it through the admin menu (`host` settings and secrets
are changed with the admin command on the host only: the first say where
the installation's parts are, the second would pass through an agent's
conversation).
"""

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path("/etc/scriptorium/settings.toml")
DATABASE_ENV = "SCRIPTORIUM_DATABASE_ADDRESS"
# Settings an environment variable, when set, overrides.
ENVIRONMENT = {"database.address": DATABASE_ENV, "grobid.address": "SCRIPTORIUM_GROBID_ADDRESS"}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


AT_ONCE = "at once"
NEXT_RUN = "at the next run"
RESTART = "when the server and the worker restart"


class SettingsError(ValueError):
    """An unknown setting name, or a value its setting does not accept."""


@dataclass(frozen=True)
class Setting:
    name: str  # "section.key"
    default: str | int
    explanation: str
    check: Callable[[str | int], bool] = lambda value: True
    accepts: str = ""  # what a valid value is, for the error message
    secret: bool = False
    effect: str = RESTART  # when a changed value takes effect
    host: bool = False  # where the installation's parts are: changed on the host only


def _between(low: int, high: int) -> Callable[[int], bool]:
    return lambda value: low <= value <= high


def _absolute(value: str) -> bool:
    return value.startswith("/")


def _email_or_empty(value: str) -> bool:
    return value == "" or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is not None


SETTINGS: tuple[Setting, ...] = (
    Setting("database.address", "postgresql:///scriptorium",
            "Where the PostgreSQL database is. The default is the local database"
            " `scriptorium`, entered as the account the server runs as. The environment"
            " variable SCRIPTORIUM_DATABASE_ADDRESS, when set, takes its place.",
            lambda v: v.startswith(("postgresql://", "postgres://")),
            "an address starting with postgresql://", host=True),
    Setting("data.folder", "/srv/scriptorium",
            "Folder for PDFs, the holding area for uploaded PDFs, parsed full texts and run"
            " folders.", _absolute, "an absolute path", host=True),
    Setting("backup.folder", "/srv/scriptorium/backups",
            "Folder the backup writes to.", _absolute, "an absolute path", host=True),
    Setting("backup.days_kept", 14,
            "Backups older than this many days are removed.", _between(1, 3650),
            "a whole number from 1 to 3650", effect=NEXT_RUN),
    Setting("surveillance.day", "monday",
            "Day of the week on which the worker harvests new items from the subscribed"
            " venues and then writes the backup. `off` switches the schedule off (a run"
            " can still be started by hand).",
            lambda v: v in DAYS or v == "off",
            "a day of the week in lower case, e.g. monday, or off", effect=AT_ONCE),
    Setting("surveillance.hour", 3,
            "Hour of that day (0-23, server time) at which the harvest starts.",
            _between(0, 23), "a whole number from 0 to 23", effect=AT_ONCE),
    Setting("surveillance.window_days", 90,
            "How many days back, by publication date, each harvest asks the venues for."
            " Papers already held are skipped, so a wide window costs little and catches"
            " papers the source lists or labels late.", _between(7, 3650),
            "a whole number from 7 to 3650", effect=NEXT_RUN),
    Setting("surveillance.max_cost", 1,
            "A harvest stops asking the source once its requests have cost this many US"
            " dollars; the next run takes up what was left.", _between(1, 1000),
            "a whole number from 1 to 1000", effect=NEXT_RUN),
    Setting("surveillance.parallel", 8,
            "How many requests a harvest sends to the source at once.", _between(1, 16),
            "a whole number from 1 to 16", effect=NEXT_RUN),
    Setting("login.issuer", "",
            "Address of the OpenID Connect provider members log in at, e.g."
            " https://auth.example.org/realms/lab. Empty switches login off: members"
            " then use API keys only. Anyone the provider lets log in becomes a member"
            " at their first request.",
            lambda v: v == "" or v.startswith("https://"),
            "an address starting with https://, or empty", effect=AT_ONCE),
    Setting("login.client_id", "scriptorium-client",
            "The name the provider knows the Scriptorium client by (a public client"
            " with PKCE). The server accepts only tokens issued to it.",
            lambda v: bool(str(v).strip()), "a client id", effect=AT_ONCE),
    Setting("login.audience", "scriptorium-api",
            "The audience a token must name to be accepted here; the provider adds it"
            " to the tokens of the Scriptorium client.",
            lambda v: bool(str(v).strip()), "an audience name", effect=AT_ONCE),
    Setting("login.keys", "on",
            "Whether API keys are still accepted while a login provider is set. Set to"
            " off once every member logs in.",
            lambda v: v in ("on", "off"), "on or off", effect=AT_ONCE),
    Setting("limits.per_request", 1000,
            "The most items or passages one request may ask for.", _between(1, 100_000),
            "a whole number from 1 to 100000", effect=AT_ONCE),
    Setting("limits.requests_per_minute", 120,
            "The most requests one member may send in a minute.", _between(1, 100_000),
            "a whole number from 1 to 100000", effect=AT_ONCE),
    Setting("limits.upload_megabytes", 100,
            "The largest PDF a member may upload, in megabytes.", _between(1, 2000),
            "a whole number from 1 to 2000", effect=AT_ONCE),
    Setting("files.pdf_download", "on",
            "Whether the server hands held PDFs to members. Set to off where members may"
            " not receive them; full texts stay searchable. Takes effect at once.",
            lambda v: v in ("on", "off"), "on or off", effect=AT_ONCE),
    Setting("files.pdf_upload", "on",
            "Whether the server accepts PDFs from members (donations). Takes effect at"
            " once.", lambda v: v in ("on", "off"), "on or off", effect=AT_ONCE),
    Setting("embedding.batch_size", 32,
            "How many texts the embedding model takes at once. Larger is faster and uses"
            " more GPU memory.", _between(1, 1024), "a whole number from 1 to 1024",
            effect="within a minute"),
    Setting("embedding.when_idle", "on",
            "Whether the worker, when no job waits, embeds the items and passages that"
            " have no vector yet. Set to off to pause it (for instance to leave the GPU"
            " to others); it takes effect within a minute, without a restart.",
            lambda v: v in ("on", "off"), "on or off", effect="within a minute"),
    Setting("grobid.address", "http://127.0.0.1:8070",
            "Where GROBID, the program that parses PDFs, answers. The environment"
            " variable SCRIPTORIUM_GROBID_ADDRESS, when set, takes its place.",
            lambda v: v.startswith(("http://", "https://")),
            "an address starting with http:// or https://", host=True),
    Setting("sources.contact_email", "",
            "Contact address sent to Crossref and OpenAlex with each request, which they"
            " ask of regular users. Empty sends none.", _email_or_empty,
            "an email address, or empty"),
    Setting("sources.openalex_api_key", "",
            "OpenAlex API key, for harvesting journals (OpenAlex charges per request"
            " beyond a small free daily allowance).", secret=True),
    Setting("sources.s2_api_key", "", "Semantic Scholar API key (abstracts).", secret=True),
    Setting("sources.elsevier_api_key", "", "Elsevier API key for Scopus (abstracts).",
            secret=True),
    Setting("sources.elsevier_insttoken", "",
            "Elsevier institutional token, needed for Scopus outside the university"
            " network.", secret=True),
    Setting("sources.springer_api_key", "", "Springer Nature API key (abstracts).",
            secret=True),
)
_BY_NAME = {s.name: s for s in SETTINGS}


class Settings:
    """The settings in force: every defined setting, by its full name."""

    def __init__(self, values: dict[str, str | int], path: Path | None = None):
        self._values = values
        self.path = path  # the file they were read from

    def __getitem__(self, name: str) -> str | int:
        return self._values[name]

    def with_value(self, name: str, value: str | int) -> "Settings":
        """These settings with one value changed (nothing is written)."""
        return Settings({**self._values, name: value}, self.path)

    def section(self, section: str) -> dict[str, str | int]:
        """One section's settings by their short names, e.g. the keys of "sources"."""
        prefix = section + "."
        return {n.removeprefix(prefix): v for n, v in self._values.items()
                if n.startswith(prefix)}


def settings_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("SCRIPTORIUM_SETTINGS") or DEFAULT_PATH)


def _valid(setting: Setting, value) -> str | int:
    """The value in the setting's type, or SettingsError saying what is accepted."""
    kind = type(setting.default)
    if kind is int and isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        value = int(value.strip())
    wrong_type = type(value) is not kind  # a bool is not a whole number here
    if wrong_type or not setting.check(value):
        accepts = setting.accepts or ("a whole number" if kind is int else "text")
        raise SettingsError(f"{setting.name} must be {accepts}; got {value!r}")
    return value


def _read(path: Path) -> dict[str, str | int]:
    """The values the file sets, by full name; every one checked."""
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path} is not valid TOML: {exc}") from exc
    values = {}
    for section, entries in data.items():
        if not isinstance(entries, dict):
            raise SettingsError(f"{path}: {section!r} is outside a section")
        for key, value in entries.items():
            name = f"{section}.{key}"
            if name not in _BY_NAME:
                raise SettingsError(f"{path}: unknown setting {name!r}")
            values[name] = _valid(_BY_NAME[name], value)
    return values


def load(path: str | os.PathLike | None = None) -> Settings:
    """The settings in force: the file's values over the defaults. A file
    with an unknown name or an unacceptable value is refused, naming it."""
    source = settings_path(path)
    values = {s.name: s.default for s in SETTINGS} | _read(source)
    for name, variable in ENVIRONMENT.items():
        if value := os.environ.get(variable):
            values[name] = _valid(_BY_NAME[name], value)
    return Settings(values, source)


def check_value(name: str, value: str | int) -> str | int:
    """The value in the setting's type; SettingsError for an unknown name
    or a value the setting does not accept."""
    if name not in _BY_NAME:
        raise SettingsError(f"unknown setting {name!r}; `settings show` lists them")
    return _valid(_BY_NAME[name], value)


def _shown(setting: Setting, value: str | int) -> str:
    if setting.secret:
        return "set" if value else "not set"
    if setting.name == "database.address":  # a password in the address is not shown
        return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", str(value))
    return str(value)


def show(path: str | os.PathLike | None = None) -> list[tuple[str, str, str]]:
    """(name, value, explanation) for every setting, in the order defined;
    secret values appear as "set" or "not set"."""
    current = load(path)
    return [(s.name, _shown(s, current[s.name]), s.explanation) for s in SETTINGS]


def describe(path: str | os.PathLike | None = None) -> list[dict]:
    """Every setting as the admin menu shows it: name, value (a secret as
    "set" or "not set"), default, what it does, what a value must be, when
    a change takes effect, and whether the menu may change it."""
    current = load(path)
    out = []
    for s in SETTINGS:
        kind = "a whole number" if isinstance(s.default, int) else "text"
        out.append({
            "name": s.name, "value": _shown(s, current[s.name]),
            "default": "not set" if s.secret else _shown(s, s.default),
            "explanation": s.explanation, "accepts": s.accepts or kind,
            "takes_effect": s.effect,
            "changed_here": menu_may_change(s.name),
            **({} if menu_may_change(s.name) else {"changed": HOST_ONLY})})
    return out


HOST_ONLY = "on the host only, with `scriptorium settings set <name> <value>`"


def menu_may_change(name: str) -> bool:
    """Whether an admin may change this setting through the admin menu."""
    setting = _BY_NAME.get(name)
    return setting is not None and not setting.secret and not setting.host


def shown(name: str, value: str | int) -> str:
    """A setting's value as it may be displayed."""
    return _shown(_BY_NAME[name], value)


def _toml_value(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if re.search(r"[\x00-\x1f\x7f]", escaped):
        raise SettingsError("a value cannot hold line breaks or control characters")
    return f'"{escaped}"'


def set_value(name: str, value: str | int, path: str | os.PathLike | None = None) -> None:
    """Change one setting in the file (created if missing, readable by its
    owner only). Setting the default removes the entry. SettingsError for
    an unknown name or a value the setting does not accept."""
    if name not in _BY_NAME:
        raise SettingsError(f"unknown setting {name!r}; `settings show` lists them")
    target = settings_path(path)
    values = _read(target)
    values[name] = _valid(_BY_NAME[name], value)
    lines: list[str] = []
    section = None
    for setting in SETTINGS:  # the file follows the order of definition
        if setting.name not in values or values[setting.name] == setting.default:
            continue
        head, key = setting.name.split(".", 1)
        if head != section:
            lines += ([""] if lines else []) + [f"[{head}]"]
            section = head
        lines.append(f"{key} = {_toml_value(values[setting.name])}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
