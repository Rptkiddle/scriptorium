"""The intake job: a venue's papers harvested from OpenAlex into the collection."""

import json

import pytest
from typer.testing import CliRunner

from scriptorium_server import admin, db as database, intake, items, jobs
from scriptorium_server.sources import openalex

WORK = {
    "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/a",
    "title": "Algorithmic news curation", "publication_date": "2022-03-01",
    "publication_year": 2022, "type": "article", "language": "en", "is_retracted": False,
    "authorships": [{"raw_author_name": "Ada Lovelace"}],
    "biblio": {"volume": "1", "issue": "2", "first_page": "3", "last_page": "20"},
    "primary_location": {"source": {"display_name": "J Comm (OpenAlex name)"}},
    "abstract_inverted_index": {"Text.": [0]},
}
NO_DOI = {**WORK, "id": "https://openalex.org/W2", "doi": None, "title": "A paper without a DOI"}
EDITORIAL = {**WORK, "id": "https://openalex.org/W3", "doi": "https://doi.org/10.1234/b",
             "title": "Editorial", "type": "editorial"}
GERMAN = {**WORK, "id": "https://openalex.org/W4", "doi": "https://doi.org/10.1234/c",
          "title": "Ein Aufsatz", "language": "de"}


@pytest.fixture
def venue(db):
    row = db.execute("INSERT INTO venues (name, adapter, source_id, publisher, category,"
                     " subscribed) VALUES ('Journal of Communication', 'openalex', 'S1',"
                     " 'Oxford University Press', 'Journals', true) RETURNING *").fetchone()
    db.commit()
    return row


def serve(monkeypatch, works):
    def fake(session, source_id, since, api_key="", on_page=None):
        if on_page:
            on_page({"count": len(works), "cost_usd": 0.0001})
        yield from works
    monkeypatch.setattr(openalex, "works", fake)


def test_backfill_admits_kept_works_and_counts_the_rest(db, venue, tmp_path, monkeypatch):
    serve(monkeypatch, [WORK, NO_DOI, EDITORIAL, GERMAN])
    result = intake.backfill(db, venue, tmp_path)
    assert result["new"] == 2 and result["type: editorial"] == 1 and result["language: de"] == 1
    assert json.loads((tmp_path / "counts.json").read_text())["fetched"] == 4
    row = db.execute("SELECT * FROM items WHERE doi = '10.1234/a'").fetchone()
    assert row["openalex_id"] == "W1" and row["provenance_kind"] == "venue"
    assert row["venue_id"] == venue["id"]  # placed under the list's name, not OpenAlex's
    assert row["publisher"] == "Oxford University Press"
    assert "2 new" in intake.summary(result)


def test_rerun_adds_nothing_new_even_without_doi(db, venue, tmp_path, monkeypatch):
    serve(monkeypatch, [WORK, NO_DOI])
    intake.backfill(db, venue, tmp_path)
    again = intake.backfill(db, venue, tmp_path)
    assert again.get("new", 0) == 0 and again["harvested before"] == 2
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 2


def test_a_held_doi_is_matched_and_marked(db, venue, tmp_path, monkeypatch):
    key = items.insert(db, {"itemType": "journalArticle", "title": "Algorithmic news curation",
                            "DOI": "10.1234/a"}, "donation")
    db.commit()
    serve(monkeypatch, [WORK])
    result = intake.backfill(db, venue, tmp_path)
    assert result["held"] == 1 and result.get("new", 0) == 0
    row = items.get(db, key)
    assert row["openalex_id"] == "W1" and row["provenance_kind"] == "donation"


def test_a_source_error_does_not_reveal_the_request(db, venue, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("GET https://api.openalex.org/works?api_key=SECRET failed")
        yield  # pragma: no cover
    monkeypatch.setattr(openalex, "works", broken)
    with pytest.raises(intake.HarvestError) as caught:
        intake.backfill(db, venue, tmp_path, api_key="SECRET")
    assert "SECRET" not in str(caught.value) and "RuntimeError" in str(caught.value)


def test_venue_not_harvested_through_openalex_is_refused(db, tmp_path):
    with pytest.raises(ValueError):
        intake.backfill(db, {"name": "X", "adapter": "crossref", "source_id": None}, tmp_path)


@pytest.fixture
def run(db, empty_schema, tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "connect", lambda: database.connect(*empty_schema))
    (tmp_path / "settings.toml").write_text('[embedding]\nwhen_idle = "off"\n')  # no model in these tests
    monkeypatch.setenv("SCRIPTORIUM_SETTINGS", str(tmp_path / "settings.toml"))

    def invoke(*args):
        result = CliRunner().invoke(admin.app, list(args))
        db.rollback()
        return result
    return invoke


def test_venues_import_and_backfill_queue_jobs(run, db, tmp_path):
    listing = tmp_path / "list.csv"
    listing.write_text(
        "openalex_id,journal,issns,publisher,coded_class\n"
        "S1,Economics,1234-5678,Kiel Institute,PEOPLE\n"
        "S2,Economics,2345-6789,De Gruyter,PEOPLE\n"
        "S3,Scientific Reports,2045-2322,Nature Portfolio,MIXED\n")
    result = run("venues", "import", str(listing))
    assert result.exit_code == 0 and "1 named with their publisher" in result.output
    names = {r["source_id"]: (r["name"], r["needs_filter"])
             for r in db.execute("SELECT * FROM venues").fetchall()}
    assert names["S2"] == ("Economics (De Gruyter)", False)
    assert names["S3"][1] is True
    result = run("backfill", "--all")
    assert result.exit_code == 0 and "Queued 2" in result.output  # the MIXED journal waits
    queued = db.execute("SELECT payload FROM jobs WHERE kind = 'backfill'").fetchall()
    assert len(queued) == 2 and queued[0]["payload"]["since"] == "2020-01-01"
    assert run("backfill", "--source-id", "S9").exit_code == 1
    assert run("backfill", "--source-id", "S3").exit_code == 1  # waits for a filter


def test_worker_runs_a_backfill_job(run, db, venue, monkeypatch, tmp_path):
    serve(monkeypatch, [WORK])
    run("settings", "set", "data.folder", str(tmp_path))
    jobs.enqueue(db, "backfill", {"venue_id": venue["id"], "since": "2020-01-01"})
    db.commit()
    result = run("worker", "--until-idle")
    assert result.exit_code == 0, result.output
    job = db.execute("SELECT status, summary FROM jobs WHERE kind = 'backfill'").fetchone()
    assert job["status"] == "done" and job["summary"].startswith("1 new")


def test_a_paper_without_doi_matches_the_same_paper_held(db, venue, tmp_path, monkeypatch):
    key = items.insert(db, {"itemType": "journalArticle", "title": "A paper without a DOI",
                            "creators": [{"creatorType": "author", "firstName": "Ada",
                                          "lastName": "Lovelace"}]}, "donation")
    db.commit()
    serve(monkeypatch, [NO_DOI])
    result = intake.backfill(db, venue, tmp_path)
    assert result["held"] == 1 and db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 1
    assert items.get(db, key)["openalex_id"] == "W2"


def test_a_held_preprint_is_counted_as_replaced(db, venue, tmp_path, monkeypatch):
    items.insert(db, {"itemType": "preprint", "title": "Algorithmic news curation",
                      "DOI": "10.31235/osf.io/abcde", "repository": "SocArXiv",
                      "creators": [{"creatorType": "author", "firstName": "Ada",
                                    "lastName": "Lovelace"}]}, "donation")
    db.commit()
    serve(monkeypatch, [WORK])
    result = intake.backfill(db, venue, tmp_path)
    assert result["replaced another version"] == 1


# --- fetching to files ------------------------------------------------------------

import gzip


def test_fetch_venue_saves_works_and_checks_completeness(tmp_path, monkeypatch):
    serve(monkeypatch, [WORK, NO_DOI, EDITORIAL])
    row = intake.fetch_venue("S1", tmp_path, api_key="SECRET")
    assert row["status"] == "ok" and row["fetched"] == row["unique"] == 3
    lines = gzip.open(tmp_path / "S1.jsonl.gz", "rt").read().splitlines()
    assert [json.loads(l)["id"] for l in lines] == [WORK["id"], NO_DOI["id"], EDITORIAL["id"]]
    assert "SECRET" not in json.dumps(row)


def test_fetch_venue_flags_repeats_and_keeps_no_final_file(tmp_path, monkeypatch):
    serve(monkeypatch, [WORK, WORK])
    row = intake.fetch_venue("S1", tmp_path)
    assert row["status"] == "incomplete" and row["duplicates"] == 1
    assert not (tmp_path / "S1.jsonl.gz").exists()


def test_fetch_venue_failure_names_only_the_error_class(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("https://api.openalex.org/works?api_key=SECRET")
        yield  # pragma: no cover
    monkeypatch.setattr(openalex, "works", broken)
    row = intake.fetch_venue("S1", tmp_path, api_key="SECRET")
    assert row["status"] == "failed" and row["problem"] == "RuntimeError"
    assert not list(tmp_path.iterdir())


def test_fetch_list_skips_complete_journals_and_writes_a_checklist(tmp_path, monkeypatch):
    serve(monkeypatch, [WORK])
    journals = [{"source_id": "S1", "journal": "One"}, {"source_id": "S2", "journal": "Two"}]
    first = intake.fetch_list(journals, tmp_path, parallel=2, report=lambda m: None)
    assert {r["source_id"] for r in first} == {"S1", "S2"}
    again = intake.fetch_list(journals, tmp_path, report=lambda m: None)
    assert again == []
    assert len((tmp_path / "checklist.jsonl").read_text().splitlines()) == 2


def test_load_command_loads_fetched_files(run, db, venue, tmp_path):
    with gzip.open(tmp_path / "S1.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(json.dumps(WORK) + "\n" + json.dumps(EDITORIAL) + "\n")
    run("settings", "set", "data.folder", str(tmp_path))
    result = run("load", str(tmp_path), "--source-id", "S1")
    assert result.exit_code == 0, result.output
    assert "1 new" in result.output and "1 not admitted" in result.output
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 1
    assert run("load", str(tmp_path)).exit_code == 1  # no venues named
