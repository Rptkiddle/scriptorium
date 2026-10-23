"""Persistent user settings for the hub and spokes.

Stored as settings.json in the index directory (so they live with the data,
outside the repo). Structure mirrors the hub-and-spoke architecture: one
section per tab in the settings GUI (general, zotero, surveillance,
interrogation, discovery). Unknown keys are dropped on save; missing keys
fall back to DEFAULTS, so a new setting needs a DEFAULTS entry and a SCHEMA
entry (the GUI and the dashboard reference render from SCHEMA).

These are runtime preferences (retrieval depth, subagent models, poll
intervals). Parameters whose change would obligate a re-index (chunking,
embedding model, bibliography detection) are deliberately NOT here — they
live in config.py / code.
"""

import json
import os
from pathlib import Path

from scriptorium.config import Config

MODELS = ("haiku", "sonnet", "opus", "fable")

DEFAULTS: dict = {
    "general": {
        "menubar_health_interval": 5,  # seconds between /health polls
        "menubar_staleness_interval": 60,  # seconds between Zotero version checks
        "model_idle_minutes": 15,  # unload the embedding model after idle (0 = never)
        "index_idle_minutes": 15,  # release the in-memory search vectors after idle (0 = never)
        "auto_sync": 1,  # sync automatically when the index is behind Zotero
        "auto_sync_interval": 60,  # seconds between the daemon's staleness checks
    },
    "zotero": {
        # Reads use Zotero's local API; writes go through the Scriptorium
        # Bridge plugin. Both require the Zotero app to be running.
        # Linked-file attachments live here; must match Zotero's Linked
        # Attachment Base Directory (Settings -> Advanced -> Files and Folders).
        "linked_attachments_dir": "~/Desktop/libr/Papers",
        "bibtex_format": "biblatex",  # default format of hub_bibtex
    },
    "surveillance": {
        # Master switch: saving reconciles the nightly LaunchAgent, so the
        # run happens automatically per this config — no manual triggering.
        "enabled": 0,
        # Research-interest profile read verbatim by the filter and appraise
        # judges. Owned and edited by the user.
        "profile": "",
        "model_filter": "sonnet",  # undemanding binary screen (megajournals)
        "model_appraise": "opus",  # demanding binary: highly vs peripherally relevant
        "filter_batch": 250,  # candidates per filter subagent (title/venue only)
        "appraise_batch": 15,  # candidates per appraise subagent
        "nightly_hour": 2,  # hour (0-23) of the daily run
        "catchup_days": 7,  # a run covers missed nights back this many days
        "auto_add": 0,  # add each run's accepts to Zotero (metadata-only)
        # Keys for filling abstracts Crossref lacks (abstracts.py); stored
        # locally, never synced. Scopus also needs the VU network or the token.
        "elsevier_api_key": "",
        "elsevier_insttoken": "",
        "springer_api_key": "",
    },
    "interrogation": {
        "k": 10,  # chunks retrieved per query (PaperQA2 evidence_k)
        "force_chunks": 1,  # chunks forced per green-lit paper
        "summary_words": 100,  # target length of each chunk's evidence summary
        "rcs_cutoff": 2,  # minimum anchored relevance level (0-3) to survive
        "rcs_chunks_per_judge": 12,  # max chunks per scoring subagent (0 = single judge)
        "model_greenlight": "opus",
        "model_rcs": "opus",
        "model_answer": "opus",
        "model_brief": "opus",
    },
    "discovery": {
        "s2_api_key": "",  # Semantic Scholar key; stored locally, never synced
        "model_screen": "opus",  # judges candidates against the protocol criteria
        "model_arbiter": "fable",  # resolves unsure verdicts (must differ from screen)
        "screen_batch": 25,  # candidates per screening subagent
        "per_query_cap": 100,  # results kept per query formulation
        "forward_cap": 100,  # forward citations kept per snowball seed
        "screen_round_cap": 350,  # candidates screened per round (rest logged as dropped)
        "snowball_rounds": 2,  # maximum snowball-screen iterations
    },
}

_INT_BOUNDS = {
    "menubar_health_interval": (2, 300),
    "menubar_staleness_interval": (10, 3600),
    "model_idle_minutes": (0, 1440),
    "index_idle_minutes": (0, 1440),
    "nightly_hour": (0, 23),
    "catchup_days": (1, 30),
    "auto_add": (0, 1),
    "auto_sync": (0, 1),
    "auto_sync_interval": (10, 3600),
    "k": (1, 50),
    "force_chunks": (1, 10),
    "summary_words": (50, 300),
    "rcs_cutoff": (0, 3),
    "rcs_chunks_per_judge": (0, 200),
    "filter_batch": (1, 1000),
    "appraise_batch": (1, 100),
    "enabled": (0, 1),
    "screen_batch": (1, 100),
    "per_query_cap": (10, 1000),
    "forward_cap": (10, 1000),
    "screen_round_cap": (25, 2000),
    "snowball_rounds": (0, 5),
}


def _path(config: Config) -> Path:
    return config.index_dir / "settings.json"


class SettingsFileError(RuntimeError):
    """The settings file exists but cannot be read as JSON."""


def _read_stored(path: Path) -> dict:
    try:
        stored = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsFileError(f"settings file {path} is unreadable: {exc}") from exc
    if not isinstance(stored, dict):
        raise SettingsFileError(f"settings file {path} does not hold a JSON object")
    return stored


def load_settings(config: Config) -> dict:
    """DEFAULTS overlaid with whatever the settings file holds (per section).
    An unreadable file yields DEFAULTS (so the daemon keeps running);
    save_settings refuses to write over it."""
    merged = {section: dict(values) for section, values in DEFAULTS.items()}
    path = _path(config)
    if path.exists():
        try:
            stored = _read_stored(path)
        except SettingsFileError:
            return merged
        for section, values in merged.items():
            for key in values:
                if isinstance(stored.get(section), dict) and key in stored[section]:
                    values[key] = stored[section][key]
    return merged


def secret_keys() -> set[tuple[str, str]]:
    """(section, key) pairs whose kind is secret, from SCHEMA."""
    return {(section, f["key"]) for section, spec in SCHEMA.items()
            for f in spec["fields"] if f["kind"] == "secret"}


def mask_settings(settings: dict) -> dict:
    """A copy safe to serve over HTTP: secret values become the sentinel
    "__set__" (or stay empty). The stored value never leaves the file."""
    masked = {section: dict(values) for section, values in settings.items()}
    for section, key in secret_keys():
        if masked.get(section, {}).get(key):
            masked[section][key] = "__set__"
    return masked


def save_settings(config: Config, incoming: dict) -> dict:
    """Validate incoming values against DEFAULTS' shape, persist, return result.

    Unknown sections/keys are ignored; invalid values raise ValueError.
    Secret fields: an empty or "__set__" incoming value keeps the stored
    one; "__clear__" empties it. The file is written atomically and is
    mode 600 from creation. If the existing file cannot be parsed, saving
    raises SettingsFileError instead of overwriting it with defaults.
    """
    if not isinstance(incoming, dict):
        raise ValueError("settings must be a JSON object")
    path = _path(config)
    if path.exists():
        _read_stored(path)  # raises SettingsFileError on a corrupt file
    secrets = secret_keys()
    kinds = {(section, f["key"]): f["kind"]
             for section, spec in SCHEMA.items() for f in spec["fields"]}
    choices = {(section, f["key"]): f["options"]
               for section, spec in SCHEMA.items() for f in spec["fields"]
               if f["kind"] == "choice"}
    current = load_settings(config)
    for section, values in current.items():
        if not isinstance(incoming.get(section), dict):
            continue
        for key in values:
            if key not in incoming[section]:
                continue
            value = incoming[section][key]
            if (section, key) in secrets:
                if value == "__clear__":
                    values[key] = ""
                elif value and value != "__set__":
                    values[key] = str(value)
                continue
            if key in _INT_BOUNDS:
                lo, hi = _INT_BOUNDS[key]
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError(f"{section}.{key} must be a whole number") from None
                if not lo <= value <= hi:
                    raise ValueError(f"{section}.{key} must be between {lo} and {hi}")
            elif key.startswith("model_"):
                if value not in MODELS:
                    raise ValueError(f"{section}.{key} must be one of {MODELS}")
            elif (section, key) in choices:
                if value not in choices[(section, key)]:
                    raise ValueError(f"{section}.{key} must be one of {choices[(section, key)]}")
            elif kinds.get((section, key)) in ("text", "textarea") and not isinstance(value, str):
                raise ValueError(f"{section}.{key} must be text")
            values[key] = value
    disc = current["discovery"]
    if disc["model_arbiter"] == disc["model_screen"]:
        raise ValueError("discovery.model_arbiter must differ from discovery.model_screen")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(current, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    path.chmod(0o600)
    return current


# Rendering and documentation schema for the GUI. One entry per DEFAULTS
# key, in display order; the settings page renders its rows from this and
# the dashboard renders its settings reference from the same data, so a
# new setting needs exactly two entries repo-wide: DEFAULTS and here.
# kind: onoff | int | model | text | textarea | secret | choice (with "options").
SCHEMA: dict = {
    "general": {
        "title": "Hub",
        "blurb": "The daemon, the index, and the menubar companion.",
        "fields": [
            {"key": "menubar_health_interval", "kind": "int",
             "label": "Health poll interval (s)",
             "doc": "How often the menubar checks the daemon"},
            {"key": "menubar_staleness_interval", "kind": "int",
             "label": "Staleness check interval (s)",
             "doc": "How often the menubar compares index vs Zotero versions"},
            {"key": "model_idle_minutes", "kind": "int",
             "label": "Model idle unload (min)",
             "doc": "Free the embedding model after this long unused; 0 keeps it loaded"},
            {"key": "index_idle_minutes", "kind": "int",
             "label": "Index idle release (min)",
             "doc": "Free the search vectors held in memory after this long unused; 0 keeps them"},
            {"key": "auto_sync", "kind": "onoff", "label": "Auto-sync",
             "doc": "Sync automatically whenever the index falls behind Zotero"},
            {"key": "auto_sync_interval", "kind": "int",
             "label": "Auto-sync check interval (s)",
             "doc": "How often the daemon compares index vs Zotero versions"},
        ],
    },
    "zotero": {
        "title": "Zotero",
        "blurb": ("Reads use Zotero's local API; writes go through the"
                  " Scriptorium Bridge plugin. Both need the Zotero app running."),
        "fields": [
            {"key": "linked_attachments_dir", "kind": "text",
             "label": "Linked attachments folder",
             "doc": ("Where PDFs live; must equal Zotero's Linked Attachment"
                     " Base Directory (Settings > Advanced > Files and Folders)")},
            {"key": "bibtex_format", "kind": "choice", "options": ["biblatex", "bibtex"],
             "label": "BibTeX export format",
             "doc": ("Default format of hub_bibtex: biblatex (\\addbibresource)"
                     " or bibtex (\\bibliography)")},
        ],
    },
    "surveillance": {
        "title": "Surveillance",
        "blurb": ("Nightly sweep of subscribed venues: harvest via Crossref,"
                  " LLM judgement in two stages (Filter, Appraise) against your research profile,"
                  " morning report and digest."),
        "fields": [
            {"key": "enabled", "kind": "onoff", "label": "Nightly monitoring",
             "doc": ("Master switch. Saving reconciles the LaunchAgent, so runs"
                     " happen automatically at the set hour; reports at /surveillance")},
            {"key": "auto_add", "kind": "onoff", "label": "Add accepts to library",
             "doc": ("After the report, create each accepted paper in Zotero"
                     " (metadata-only, tagged, filed under its venue's collection)")},
            {"key": "profile", "kind": "textarea", "label": "Research profile",
             "doc": ("Read verbatim by the filter and appraise judges. Describe"
                     " what you work on and why; this text IS the relevance standard")},
            {"key": "model_filter", "kind": "model", "label": "Filter model",
             "doc": ("Undemanding binary screen of megajournal candidates,"
                     " judged on title and venue only")},
            {"key": "model_appraise", "kind": "model", "label": "Appraise model",
             "doc": ("Demanding binary with abstracts: highly vs peripherally"
                     " relevant to the profile")},
            {"key": "filter_batch", "kind": "int", "label": "Candidates per filter judge",
             "doc": ("Batch size per filter subagent; candidates are shuffled across"
                     " batches so each sees a mix of venues")},
            {"key": "appraise_batch", "kind": "int", "label": "Candidates per appraise judge",
             "doc": "Batch size per appraise subagent (each candidate carries its abstract)"},
            {"key": "nightly_hour", "kind": "int", "label": "Nightly run hour (0-23)",
             "doc": "Runs on wake if the hour was missed while asleep"},
            {"key": "catchup_days", "kind": "int", "label": "Catch-up window (days)",
             "doc": ("Each run covers every night since the last successful one,"
                     " back this many days, and retries failed additions from that"
                     " period; longer gaps show as missed on the dashboard")},
            {"key": "elsevier_api_key", "kind": "secret", "label": "Elsevier (Scopus) API key",
             "doc": ("Fills missing abstracts from Scopus before Appraise. Works on"
                     " the VU network; elsewhere, including the nightly run at home,"
                     " it also needs the institutional token. Stored in the local data"
                     " directory only")},
            {"key": "elsevier_insttoken", "kind": "secret",
             "label": "Elsevier institutional token",
             "doc": ("Lets the Scopus key work off the VU network; issued by the VU"
                     " library for this key")},
            {"key": "springer_api_key", "kind": "secret", "label": "Springer Nature API key",
             "doc": ("Fills missing abstracts of Springer Nature papers from its Meta"
                     " API (500 requests a day). Stored in the local data directory only")},
        ],
    },
    "interrogation": {
        "title": "Interrogation",
        "blurb": ("Citation-grounded answers from your own library"
                  " (PaperQA2's procedure with Claude subagents as the judges)."),
        "fields": [
            {"key": "k", "kind": "int", "label": "k - chunks per query",
             "doc": "Retrieved per query formulation (PaperQA2 evidence_k)"},
            {"key": "force_chunks", "kind": "int", "label": "Forced chunks per green-lit paper",
             "doc": "Chunks forced into the pool for each paper the green-light pass names"},
            {"key": "summary_words", "kind": "int", "label": "Evidence summary length (words)",
             "doc": "Target length of each chunk's scored summary"},
            {"key": "rcs_cutoff", "kind": "int", "label": "Relevance cutoff",
             "doc": "1 = contextual+, 2 = supporting+, 3 = central only; all survivors become sources"},
            {"key": "rcs_chunks_per_judge", "kind": "int", "label": "Chunks per scoring judge",
             "doc": "Chunks are split across this-many-chunk parallel subagents; 0 = one judge for all"},
            {"key": "model_greenlight", "kind": "model", "label": "Green-light model",
             "doc": "Recall insurance: reviews the pool's citations for papers ranking would miss"},
            {"key": "model_rcs", "kind": "model", "label": "Relevance-scoring model",
             "doc": "Scores every retrieved chunk's evidence summary"},
            {"key": "model_answer", "kind": "model", "label": "Answer model",
             "doc": "Writes the final cited answer - the hardest job in the pipeline"},
            {"key": "model_brief", "kind": "model", "label": "Discovery-brief model",
             "doc": "Drafts the literature-review handoff when the library lacks coverage"},
        ],
    },
    "discovery": {
        "title": "Discovery",
        "blurb": ("Systematic search beyond the library: protocol-driven"
                  " querying of Semantic Scholar and OpenAlex, citation"
                  " snowballing, criteria-based LLM screening, PRISMA-style"
                  " audit trail. See docs/discovery.md."),
        "fields": [
            {"key": "s2_api_key", "kind": "secret", "label": "Semantic Scholar API key",
             "doc": ("Dedicated 1 req/s allowance for discovery and PDF retrieval"
                     " (anonymous access is unreliable). Stored in the local data"
                     " directory only — never in the repo or any sync")},
            {"key": "model_screen", "kind": "model", "label": "Screening model",
             "doc": "Judges each candidate against the protocol's written criteria"},
            {"key": "model_arbiter", "kind": "model", "label": "Arbiter model",
             "doc": ("Resolves unsure verdicts; must differ from the screening"
                     " model (same-model second passes change nothing)")},
            {"key": "screen_batch", "kind": "int", "label": "Candidates per screening judge",
             "doc": "Small shuffled batches keep judgement consistent across a batch"},
            {"key": "per_query_cap", "kind": "int", "label": "Results per query",
             "doc": "Kept per query formulation, per source"},
            {"key": "forward_cap", "kind": "int", "label": "Forward citations per seed",
             "doc": "Citing papers kept per snowball seed, ranked by term overlap"},
            {"key": "screen_round_cap", "kind": "int", "label": "Candidates screened per round",
             "doc": "Cost ceiling per round; candidates beyond it are ranked out and logged"},
            {"key": "snowball_rounds", "kind": "int", "label": "Snowball rounds (max)",
             "doc": "Iterations of citation chasing; runs stop earlier when yield dries up"},
        ],
    },
}
