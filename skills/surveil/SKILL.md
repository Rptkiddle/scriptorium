---
name: surveil
description: Run one cycle of scriptorium's literature surveillance - harvest newly published work from subscribed venues via Crossref, filter and appraise it with forced-binary LLM judgement against the user's research profile, write the daily report, add accepted papers to Zotero when surveillance.auto_add is on, and write the morning digest. The whole cycle is one script; this skill only starts it and relays the outcome. Invoke on explicit request (/scriptorium:surveil (or /surveil), "run surveillance").
---

# surveil

One surveillance cycle: **harvest → filter → appraise → report → add →
digest**, run entirely by one script. Claude is called inside it only for
the filter and appraise judgements, as tool-less calls on rendered prompt
files. The nightly run (launchd, 02:00 by default) runs the same cycle
through `scriptorium.launcher`; this skill is for an interactive run.

From the scriptorium repository (the `repo` folder `hub_status` reports):

```
uv run python scripts/surveil.py [--from YYYY-MM-DD]
```

`--from` harvests every venue from that date instead of its catch-up
window (a deliberate longer horizon). The script creates a run folder,
runs every step, and prints the run folder, the final status (`ok`,
`partial` while additions are pending, or `failed`) and a one-line
summary. Relay those three lines verbatim, plus the paths of `report.md`
and `digest.md` in the run folder.

If the status is `failed`, relay the summary and the path of
`work/error.txt`, and stop. Do not read or change code, do not re-run
steps by hand, and do not retry. A run that fails before its report step
changes no state, so the next run covers the same days.

When `surveillance.auto_add` is off, accepted papers are not added; they
are handed to `/scriptorium:ingest` from the surveillance viewer
(`http://localhost:8765/surveillance`). Subscriptions are managed in
Zotero ([✓]/[✗] prefixes on venue collections); if the user has changed
them, `scripts/surveil_registry.py` must be re-run and confirmed before
the next cycle.
