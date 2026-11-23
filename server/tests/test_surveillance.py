"""Surveillance: the subscribed venues' recent papers, harvested together."""

import datetime
import json

import pytest
from typer.testing import CliRunner

from scriptorium_server import admin, surveillance
from scriptorium_server import db as database
from scriptorium_server.sources import openalex

TODAY = datetime.date(2026, 11, 23)


def work(number, source="S1", **changes):
    return {
        "id": f"https://openalex.org/W{number}", "doi": f"https://doi.org/10.1234/w{number}",
        "title": f"Paper number {number} on news", "publication_date": "2026-11-01",
        "publication_year": 2026, "type": "article", "language": "en", "is_retracted": False,
        "authorships": [{"raw_author_name": "Ada Lovelace"}], "biblio": {},
        "primary_location": {"source": {"id": f"https://openalex.org/{source}",
                                        "display_name": "OpenAlex's name"}},
        "abstract_inverted_index": {"Text.": [0]}, **changes,
    }


@pytest.fixture
def venues(db):
    rows = [db.execute("INSERT INTO venues (name, adapter, source_id, publisher, category,"
                       " subscribed, needs_filter) VALUES (%s, 'openalex', %s, 'A Press',"
                       " 'Journals', true, %s) RETURNING *", row).fetchone()
            for row in (("Journal of Communication", "S1", False),
                        ("Political Communication", "S2", False),
                        ("A Broad Journal", "S3", True))]
    db.commit()
    return rows


@pytest.fixture
def source(monkeypatch):
    """OpenAlex stood in for: served["works"] are returned in pages of two;
    every request is recorded in served["asked"]."""
    served = {"works": [], "asked": [], "fail": set(), "cost": 0.0001}

    def pages(session, source_ids, since, api_key=""):
        served["asked"].append((tuple(source_ids), since, api_key))
        if served["fail"] & set(source_ids):
            raise RuntimeError("https://api.openalex.org/works?api_key=SECRET")
        mine = [w for w in served["works"] if openalex.source_of(w) in source_ids]
        for start in range(0, len(mine), 2):
            yield mine[start:start + 2], {"count": len(mine), "cost_usd": served["cost"]}
    monkeypatch.setattr(openalex, "pages", pages)
    monkeypatch.setattr(surveillance, "fill_abstracts", lambda candidates, settings, **kw: {
        "missing": len(candidates), "scopus": 0, "springer": 0, "unavailable": {},
        "title_only": len(candidates)})
    return served


def surveil(db, venues, tmp_path, **options):
    options.setdefault("report", lambda line: None)
    return surveillance.surveil(db, venues, tmp_path, today=TODAY, api_key="k", **options)


def test_admits_new_papers_under_their_venue_and_counts_the_rest(db, venues, source, tmp_path):
    source["works"] = [work(1), work(2, "S2"), work(3, type="editorial"),
                       work(4, "S2", language="de"), work(5, is_retracted=True)]
    result = surveil(db, venues, tmp_path, days=90)
    assert result["new"] == 2 and result["fetched"] == 5 and result["since"] == "2026-08-25"
    assert result["type: editorial"] == 1 and result["language: de"] == 1
    assert result["retracted"] == 1 and result["venues"] == 3
    row = db.execute("SELECT * FROM items WHERE openalex_id = 'W2'").fetchone()
    assert row["venue_id"] == venues[1]["id"] and row["provenance_kind"] == "venue"
    assert row["publisher"] == "A Press" and row["embedding"] is None  # the worker embeds it
    lines = [json.loads(line) for line in (tmp_path / "counts.jsonl").read_text().splitlines()]
    assert [(r["venue"], r["new"]) for r in lines] == [("Journal of Communication", 1),
                                                       ("Political Communication", 1)]
    assert json.loads((tmp_path / "totals.json").read_text())["new"] == 2
    line = surveillance.summary(result)
    assert "2 new" in line and "3 not admitted (of 5 works since 2026-08-25 in 3 venues)" in line
    assert surveillance.complete(result)


def test_a_second_run_adds_nothing_and_skips_held_works_by_one_query(db, venues, source, tmp_path):
    source["works"] = [work(1), work(2, doi=None)]
    surveil(db, venues, tmp_path)
    again = surveil(db, venues, tmp_path)
    assert again.get("new", 0) == 0 and again["harvested before"] == 2
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 2


def test_a_paper_labelled_late_is_admitted_by_a_later_run(db, venues, source, tmp_path):
    late = work(1, "S3", primary_topic=None)  # a broad journal's paper, no topic yet
    source["works"] = [late, work(2, type=None)]
    first = surveil(db, venues, tmp_path)
    assert first.get("new", 0) == 0 and first["domain: none"] == 1 and first["type: None"] == 1
    late["primary_topic"] = {"domain": {"display_name": "Social Sciences"}}
    source["works"][1]["type"] = "article"
    assert surveil(db, venues, tmp_path)["new"] == 2


def test_a_held_paper_takes_the_abstract_openalex_has_later(db, venues, source, tmp_path):
    source["works"] = [work(1, abstract_inverted_index=None)]
    surveil(db, venues, tmp_path)
    db.execute("UPDATE items SET embedding = ('[' || repeat('0.1,', 255) || '0.1]')::halfvec")
    db.commit()
    source["works"][0]["abstract_inverted_index"] = {"Now": [0], "known.": [1]}
    result = surveil(db, venues, tmp_path)
    assert result["abstract from OpenAlex later"] == 1 and result["harvested before"] == 1
    row = db.execute("SELECT abstract, embedding FROM items").fetchone()
    assert row["abstract"] == "Now known." and row["embedding"] is None  # embedded again


def test_papers_without_abstract_are_looked_up_elsewhere(db, venues, source, tmp_path, monkeypatch):
    asked = {}

    def fill(candidates, settings, *, sources, max_chars):
        asked.update(dois=sorted(c["doi"] for c in candidates), sources=sources,
                     settings=settings, max_chars=max_chars)
        for candidate in candidates:
            if candidate["doi"].endswith("w1"):
                candidate["abstract"] = "From Scopus."
        return {"missing": len(candidates), "scopus": 1, "springer": 0, "unavailable": {},
                "title_only": len(candidates) - 1}
    monkeypatch.setattr(surveillance, "fill_abstracts", fill)
    source["works"] = [work(1, abstract_inverted_index=None), work(2, abstract_inverted_index=None),
                       work(3), work(4, doi=None, abstract_inverted_index=None)]
    result = surveil(db, venues, tmp_path, sources={"elsevier_api_key": "E"})
    assert asked == {"dois": ["10.1234/w1", "10.1234/w2"], "sources": ("scopus", "sciencedirect", "springer"),
                     "settings": {"elsevier_api_key": "E"}, "max_chars": None}
    assert db.execute("SELECT abstract FROM items WHERE openalex_id = 'W1'").fetchone()[
        "abstract"] == "From Scopus."
    assert "1 from scopus, 0 from sciencedirect, 0 from springer, 1 still without" in surveillance.summary(result)
    again = surveil(db, venues, tmp_path)  # the one still without is asked for again
    assert asked["dois"] == ["10.1234/w2"] and again["harvested before"] == 4


def test_a_dry_run_writes_nothing(db, venues, source, tmp_path):
    source["works"] = [work(1), work(2, type="editorial")]
    result = surveil(db, venues, tmp_path, dry_run=True)
    assert result["would be admitted"] == 1 and result["type: editorial"] == 1
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 0
    assert "dry run: 1 would be admitted" in surveillance.summary(result)


def test_venues_are_asked_for_in_groups_and_an_unread_group_is_named(db, venues, source,
                                                                    tmp_path, monkeypatch):
    monkeypatch.setattr(surveillance, "GROUP", 2)
    source["works"] = [work(1), work(2, "S3", primary_topic={
        "domain": {"display_name": "Social Sciences"}})]
    source["fail"] = {"S3"}
    said = []
    result = surveil(db, venues, tmp_path, report=said.append)
    assert sorted(ids for ids, _, _ in source["asked"]) == [("S1", "S2"), ("S3",)]
    assert result["new"] == 1 and result["unread_venues"] == ["A Broad Journal"]
    assert not surveillance.complete(result)
    assert "1 venues could not be read" in surveillance.summary(result)
    assert "SECRET" not in "".join(said) and "RuntimeError" in "".join(said)


def test_the_run_stops_asking_at_the_cost_limit(db, venues, source, tmp_path):
    source["works"] = [work(n) for n in range(1, 41)]  # twenty replies
    source["cost"] = 0.6
    result = surveil(db, venues, tmp_path, max_cost=1.0, parallel=1)
    # replies already paid for are still settled; no further ones are asked for
    assert result["stopped_at_cost"] and 4 <= result["fetched"] < 40
    assert result["new"] == result["fetched"]
    assert "stopped at the cost limit" in surveillance.summary(result)


def test_pages_names_many_sources_in_one_request(monkeypatch):
    seen = []

    def fake(session, method, url, params=None, **kw):
        seen.append(dict(params))
        return {"results": [{"id": "W1"}], "meta": {"count": 1, "next_cursor": None}}
    monkeypatch.setattr(openalex, "request_json", fake)
    got = list(openalex.pages(None, ["S1", "S2"], "2026-08-25", api_key="k"))
    assert got == [([{"id": "W1"}], {"count": 1, "next_cursor": None})]
    assert seen[0]["filter"] == "primary_location.source.id:S1|S2,from_publication_date:2026-08-25"
    assert seen[0]["per_page"] == 200 and seen[0]["api_key"] == "k"
    with pytest.raises(ValueError):
        list(openalex.pages(None, [f"S{n}" for n in range(101)], "2026-08-25"))


def test_surveil_command_runs_and_records_the_run(db, empty_schema, venues, source, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr(admin, "connect", lambda: database.connect(*empty_schema))
    (tmp_path / "settings.toml").write_text(f'[data]\nfolder = "{tmp_path}"\n')
    monkeypatch.setenv("SCRIPTORIUM_SETTINGS", str(tmp_path / "settings.toml"))
    assert CliRunner().invoke(admin.app, ["surveil"]).exit_code == 1  # no key
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    source["works"] = [work(1)]
    result = CliRunner().invoke(admin.app, ["surveil", "--days", "30"])
    assert result.exit_code == 0 and "1 new" in result.output
    assert source["asked"][0][1:] == ((datetime.date.today()
                                       - datetime.timedelta(days=30)).isoformat(), "k")
    run_dir = next((tmp_path / "runs" / "surveillance").glob("2*"))
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["status"] == "ok" and "1 new" in manifest["summary"]
    db.rollback()
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 1
