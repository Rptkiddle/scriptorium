"""Loading fetched files in bulk: the same outcomes as admitting one by one."""

import gzip
import json

from scriptorium_server import intake, items, load
from scriptorium_server.embedding import pending

from test_intake import EDITORIAL, GERMAN, NO_DOI, WORK, serve, venue  # noqa: F401


def work(n, **changes):
    return {**WORK, "id": f"https://openalex.org/W{n}", "doi": f"https://doi.org/10.1234/n{n}",
            "title": f"A paper numbered {n} on news", **changes}


def save(folder, source_id, works):
    with gzip.open(folder / f"{source_id}.jsonl.gz", "wt", encoding="utf-8") as f:
        for w in works:
            f.write(json.dumps(w) + "\n")
    return folder / f"{source_id}.jsonl.gz"


def test_new_works_are_written_in_batches_with_their_creators(db, venue, tmp_path):
    works = [work(n) for n in range(7)] + [EDITORIAL, GERMAN, work(3)]
    loader = load.Loader(db, batch=3)
    result = loader.load_venue(venue, save(tmp_path, "S1", works))
    assert result["new"] == 7 and result["fetched"] == 10
    assert result["type: editorial"] == 1 and result["language: de"] == 1
    assert result["repeated in the files"] == 1
    row = db.execute("SELECT * FROM items WHERE doi = '10.1234/n5'").fetchone()
    assert row["openalex_id"] == "W5" and row["venue_id"] == venue["id"]
    assert row["publisher"] == "Oxford University Press" and row["category"] == "Journals"
    assert row["provenance_kind"] == "venue" and row["year"] == 2022
    assert row["creators"][0]["lastName"] == "Lovelace" and row["data"]["volume"] == "1"
    assert row["embedding"] is None and pending(db)["items"]
    assert db.execute("SELECT count(*) AS n FROM item_creators WHERE item = %s",
                      (row["key"],)).fetchone()["n"] >= 1
    keys = db.execute("SELECT count(DISTINCT citation_key) AS c, count(*) AS n FROM items").fetchone()
    assert keys["c"] == keys["n"] == 7


def test_bulk_and_one_by_one_give_the_same_items(db, venue, tmp_path, monkeypatch):
    works = [work(1), work(2, doi=None), NO_DOI, EDITORIAL]
    load.Loader(db).load_venue(venue, save(tmp_path, "S1", works))
    cols = "type, title, title_norm, abstract, year, date, doi, other_ids, creators, data," \
           " venue_id, publisher, category, provenance_kind, openalex_id"
    bulk = db.execute(f"SELECT {cols} FROM items ORDER BY openalex_id").fetchall()
    creators = db.execute("SELECT name_key, position FROM item_creators"
                          " ORDER BY name_key, position").fetchall()
    db.execute("DELETE FROM items")
    db.commit()
    serve(monkeypatch, works)
    intake.backfill(db, venue, tmp_path)
    assert db.execute(f"SELECT {cols} FROM items ORDER BY openalex_id").fetchall() == bulk
    assert db.execute("SELECT name_key, position FROM item_creators"
                      " ORDER BY name_key, position").fetchall() == creators


def test_a_second_run_adds_nothing(db, venue, tmp_path):
    path = save(tmp_path, "S1", [work(1), NO_DOI])
    load.Loader(db).load_venue(venue, path)
    again = load.Loader(db).load_venue(venue, path)
    assert again.get("new", 0) == 0 and again["harvested before"] == 2
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 2


def test_held_papers_go_one_by_one(db, venue, tmp_path):
    held = items.insert(db, {"itemType": "journalArticle", "title": "Something else entirely",
                             "DOI": "10.1234/n1"}, "donation")
    preprint = items.insert(db, {
        "itemType": "preprint", "title": "A paper numbered 2 on news", "repository": "SocArXiv",
        "DOI": "10.31235/osf.io/abcde",
        "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]},
        "donation")
    db.commit()
    result = load.Loader(db).load_venue(venue, save(tmp_path, "S1", [work(1), work(2), work(3)]))
    assert result["held"] == 1 and result["replaced another version"] == 1 and result["new"] == 1
    assert items.get(db, held)["openalex_id"] == "W1"
    replaced = items.get(db, preprint)
    assert replaced["doi"] == "10.1234/n2" and replaced["type"] == "journalArticle"
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 3


def test_the_same_paper_twice_in_the_files_is_one_item(db, venue, tmp_path):
    twice = [work(1), work(9, doi="https://doi.org/10.1234/N1"), NO_DOI,
             {**NO_DOI, "id": "https://openalex.org/W8"}]
    result = load.Loader(db).load_venue(venue, save(tmp_path, "S1", twice))
    assert result["new"] == 2 and result["held"] == 2


def test_abstracts_fill_where_openalex_has_none(db, venue, tmp_path):
    (tmp_path / "found.jsonl").write_text(
        json.dumps({"doi": "10.1234/N1", "abstract": "<p>Abstract: Found elsewhere.</p>"}) + "\n"
        + json.dumps({"doi": "10.1234/n2", "abstract": "Not used."}) + "\n")
    found = load.read_abstracts([tmp_path / "found.jsonl"])
    works = [work(1, abstract_inverted_index=None), work(2)]
    load.Loader(db, found).load_venue(venue, save(tmp_path, "S1", works))
    got = {r["doi"]: r["abstract"] for r in db.execute("SELECT doi, abstract FROM items")}
    assert got == {"10.1234/n1": "Found elsewhere.", "10.1234/n2": "Text."}


def test_a_batch_that_cannot_be_written_is_admitted_one_by_one(db, venue, tmp_path):
    loader = load.Loader(db)
    items.insert(db, {"itemType": "journalArticle", "title": "Added meanwhile",
                      "DOI": "10.1234/n2"}, "activity")  # after the loader read the collection
    db.commit()
    result = loader.load_venue(venue, save(tmp_path, "S1", [work(1), work(2), work(3)]))
    assert result["new"] == 2 and result["held"] == 1
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 3


def test_a_social_science_only_venue_is_loaded_by_domain(db, venue, tmp_path):
    mixed = {**venue, "needs_filter": True}
    social = {"primary_topic": {"domain": {"display_name": "Social Sciences"}}}
    physics = {"primary_topic": {"domain": {"display_name": "Physical Sciences"}}}
    works = [work(1, **social), work(2, **physics), work(3), work(4), work(5)]
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"id": "W3", "domain": "Social Sciences"}\n'
                      '{"id": "W4", "domain": "Life Sciences"}\n'
                      '{"id": "W2", "domain": "Social Sciences"}\n')
    loader = load.Loader(db, domains=load.read_domains([labels]))
    result = loader.load_venue(mixed, save(tmp_path, "S1", works))
    assert result["new"] == 2  # W1 by its own label, W3 by the labels file
    assert result["domain: Physical Sciences"] == 1  # a work's own label counts, not the file's
    assert result["domain: Life Sciences"] == 1 and result["domain: none"] == 1
    held = {r["openalex_id"] for r in db.execute("SELECT openalex_id FROM items").fetchall()}
    assert held == {"W1", "W3"}
    whole = load.Loader(db).load_venue(venue, save(tmp_path, "S1", works))
    assert whole["new"] == 3  # the same file for a venue taken whole


def test_load_files_writes_counts_and_reports_a_missing_file(db, venue, tmp_path):
    save(tmp_path, "S1", [work(1), EDITORIAL])
    other = {**venue, "id": venue["id"], "source_id": "S404", "name": "No file"}
    lines = []
    result = load.load_files(db, [venue, other], tmp_path, tmp_path, report=lines.append)
    assert result["new"] == 1 and result["no file"] == 1 and len(lines) == 2
    rows = [json.loads(line) for line in (tmp_path / "counts.jsonl").read_text().splitlines()]
    assert rows[0]["new"] == 1 and rows[1]["no file"] == 1
    assert json.loads((tmp_path / "totals.json").read_text())["fetched"] == 2
