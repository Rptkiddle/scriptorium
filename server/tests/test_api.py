"""The API end to end against a small collection: authentication, the usage
log and limits, and every find and inspect request."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scriptorium_server import db as database, members, settings
from scriptorium_server.api import create_app
from scriptorium_server.embedding import FakeEmbedder, embed_pending_items, embed_pending_passages
from scriptorium_server.items import admit
from scriptorium_server.sources.crossref import RegistryUnavailable

OPENAPI = Path(__file__).resolve().parents[2] / "api" / "openapi.json"


def paper(title, surname, doi, year, item_type="journalArticle", abstract="", **more):
    return {"itemType": item_type, "title": title, "DOI": doi, "date": str(year),
            "abstractNote": abstract, "publicationTitle": "Journal of Tests",
            "creators": [{"creatorType": "author", "firstName": "A.", "lastName": surname}], **more}


PAPERS = [
    paper("Echo chambers in online news", "van Atteveldt", "10.1000/a", 2021,
          abstract="We study echo chambers and filter bubbles.", volume="12", pages="1-20"),
    paper("Filter bubbles revisited", "Trilling", "10.1000/b", 2022, abstract="A filter bubble study."),
    paper("Agent based models of opinion", "Kiddle", "10.1000/c", 2023,
          abstract="Opinion dynamics with agents."),
    paper("Protein folding with transformers", "Jumper", "10.1000/d", 2021, "conferencePaper",
          abstract="Structure prediction.", proceedingsTitle="Proceedings of Tests",
          publicationTitle=""),
    paper("News recommender diversity", "Trilling", "10.1000/e", 2023,
          abstract="Diversity in news recommender systems and echo chambers."),
]


class Registry:
    def __init__(self):
        self.calls, self.down = [], False

    def __call__(self, kind, value):
        self.calls.append((kind, value))
        if self.down:
            raise RegistryUnavailable("timeout")
        if value == "10.9000/new":
            return paper("A newly found paper", "Hopper", "10.9000/new", 2024), None
        return None


@pytest.fixture
def world(db, empty_schema, tmp_path):
    """A collection, two members, and a client holding the first member's key."""
    keys = [admit(db, p, "venue")[0] for p in PAPERS]
    db.execute("UPDATE items SET has_fulltext = true WHERE key = %s", (keys[0],))
    db.cursor().executemany(
        "INSERT INTO passages (item, idx, part, pages, text, artefact) VALUES (%s, %s, 'body', %s, %s, %s)",
        [(keys[0], 0, "1", "Echo chambers form when people only read like-minded news.", False),
         (keys[0], 1, "2", "1.1 0.2 3.3", True),
         (keys[0], 2, "3", "Recommender systems can widen exposure to diverse news.", False)])
    embed_pending_items(db, FakeEmbedder())
    embed_pending_passages(db, FakeEmbedder())
    key = members.add(db, "ada lovelace")
    other = members.add(db, "grace hopper")
    db.commit()
    settings.set_value("limits.per_request", 50, tmp_path / "s.toml")
    settings.set_value("limits.requests_per_minute", 30, tmp_path / "s.toml")
    registry = Registry()
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(),
                     settings.load(tmp_path / "s.toml"), registry)
    client = TestClient(app, headers={"Authorization": f"Bearer {key}",
                                      "X-Scriptorium-Client": "0.1.0",
                                      "X-Scriptorium-Agent": "test-agent"})
    return type("World", (), {"client": client, "keys": keys, "db": db, "key": key,
                              "other": other, "registry": registry, "app": app})


def log(world):
    world.db.rollback()
    return world.db.execute("SELECT * FROM usage_log ORDER BY id").fetchall()


# --- authentication, log, limits (Task 8) --------------------------------------


def test_nothing_is_served_without_a_key(world):
    anonymous = TestClient(world.app)
    assert anonymous.get("/v1/health").status_code == 401
    assert anonymous.get(f"/v1/items/{world.keys[0]}").status_code == 401
    for path in ("/", "/docs", "/openapi.json", "/v1"):
        assert anonymous.get(path).status_code == 404  # no page describes the service
    reply = world.client.get("/v1/health")
    assert reply.json() == {"status": "ok", "server": "0.1.0", "api": 1, "min_client": "0.1.0",
                            "items": 5, "fulltexts": 1}
    assert [r["request"] for r in log(world)][-1] == "health"


def test_unknown_key_is_refused(world):
    for headers in ({}, {"Authorization": "Bearer scr_wrong"}, {"Authorization": world.key}):
        reply = TestClient(world.app).post("/v1/items", json={}, headers=headers)
        assert reply.status_code == 401 and reply.json() == {"error": "unknown or revoked API key"}
    members.revoke(world.db, "ada lovelace")
    world.db.commit()
    assert world.client.post("/v1/items", json={}).status_code == 401
    rows = log(world)
    assert [r["status"] for r in rows] == [401] * 4 and {r["member"] for r in rows} == {None}


def test_log_row_holds_inputs_and_full_reply(world):
    body = {"query": "echo chambers", "filters": {"year_from": 2021}, "count": 2}
    reply = world.client.post("/v1/search/items", json=body)
    (row,) = log(world)
    assert row["request"] == "search/items" and row["status"] == 200
    assert row["inputs"] == body and row["reply"] == reply.json()
    assert len(row["reply"]["items"]) == 2 and row["duration_ms"] >= 0
    assert (row["client_version"], row["agent"]) == ("0.1.0", "test-agent")
    member = world.db.execute("SELECT name FROM members WHERE id = %s", (row["member"],)).fetchone()
    assert member["name"] == "ada lovelace"


def test_a_refused_request_is_logged_with_its_error(world):
    reply = world.client.post("/v1/items", json={"order": "random"})
    assert reply.status_code == 400 and "order must be one of" in reply.json()["error"]
    (row,) = log(world)
    assert row["status"] == 400 and row["reply"] == reply.json() and row["inputs"]["order"] == "random"


def test_log_holds_no_address(world):
    world.client.post("/v1/items", json={}, headers={"X-Forwarded-For": "203.0.113.7"})
    stored = json.dumps(log(world), default=str)
    assert "203.0.113.7" not in stored and "testclient" not in stored
    columns = world.db.execute("SELECT column_name FROM information_schema.columns WHERE"
                               " table_schema = current_schema() AND table_name = 'usage_log'")
    assert not [c for c in columns if "addr" in c["column_name"] or "ip" == c["column_name"]]


def test_rate_limit_names_itself(world):
    for _ in range(30):
        assert world.client.post("/v1/items", json={"count": 0}).status_code == 200
    reply = world.client.post("/v1/items", json={"count": 0})
    assert reply.status_code == 429
    assert "at most 30 requests a minute" in reply.json()["error"]
    other = TestClient(world.app, headers={"Authorization": f"Bearer {world.other}"})
    assert other.post("/v1/items", json={"count": 0}).status_code == 200  # per member


def test_request_over_maximum_names_the_limit(world):
    reply = world.client.post("/v1/items", json={"count": 51})
    assert reply.status_code == 400
    assert "asks for 51 results; the most one request may ask for is 50" in reply.json()["error"]
    assert world.client.post("/v1/items", json={"count": 50}).status_code == 200
    assert world.client.post("/v1/bibtex", json={"keys": ["k"] * 51}).status_code == 400


def test_an_old_client_is_told_to_update(world):
    reply = world.client.post("/v1/items", json={}, headers={"X-Scriptorium-Client": "0.0.9"})
    assert reply.status_code == 426 and reply.json()["error"].startswith("update the client")
    no_header = TestClient(world.app, headers={"Authorization": f"Bearer {world.key}"})
    assert no_header.post("/v1/items", json={}).status_code == 200


def test_a_mistyped_input_is_refused_not_ignored(world):
    assert world.client.post("/v1/items", json={"filters": {"yaers": [2021]}}).status_code == 422
    assert world.client.post("/v1/search/items", json={"query": "x", "limit": 3}).status_code == 422


# --- retrieval (Task 9) --------------------------------------------------------


def titles(reply):
    return [i["title"] for i in reply.json()["items"]]


def test_filters_combine_with_and(world):
    def total(**filters):
        return world.client.post("/v1/items", json={"filters": filters, "count": 0}).json()["total"]

    assert total() == 5
    assert total(years=[2021]) == 2 and total(years=[2021, 2022]) == 3  # any within a filter
    assert total(years=[2021], types=["journalArticle"]) == 1  # all across filters
    assert total(authors=["Trilling"], year_from=2023) == 1
    assert total(venues=["journal of tests"]) == 4 and total(categories=["Proceedings"]) == 1
    assert total(has_fulltext=True) == 1 and total(provenance=["donation"]) == 0
    assert total(item_keys=world.keys[:2], year_to=2021) == 1
    assert total(added_since="2020-01-01") == 5 and total(added_since="2999-01-01") == 0
    assert total(authors_all=["Trilling", "Kiddle"]) == 0


def test_author_matches_any_name_form(world):
    for typed in ("van Atteveldt", "Atteveldt", "Wouter van Atteveldt", "atteveldt, w."):
        reply = world.client.post("/v1/items", json={"filters": {"authors": [typed]}})
        assert titles(reply) == ["Echo chambers in online news"], typed


def test_list_gives_chosen_fields_order_and_pages(world):
    reply = world.client.post("/v1/items", json={"order": "year", "count": 2, "offset": 1,
                                                 "fields": ["title", "year", "details"]}).json()
    assert reply["total"] == 5 and [i["year"] for i in reply["items"]] == [2023, 2022]
    assert set(reply["items"][0]) == {"title", "year", "details"}
    assert world.client.post("/v1/items", json={"fields": ["member"]}).status_code == 400


def test_search_items_ranks_by_meaning_within_filters_and_phrase(world):
    assert titles(world.client.post("/v1/search/items", json={
        "query": "echo chambers online news", "count": 1})) == ["Echo chambers in online news"]
    filtered = world.client.post("/v1/search/items", json={
        "query": "echo chambers", "filters": {"years": [2023]}, "count": 5})
    assert titles(filtered)[0] == "News recommender diversity" and len(titles(filtered)) == 2
    phrase = world.client.post("/v1/search/items", json={"query": "news", "phrase": "filter bubble"})
    assert titles(phrase) == ["Filter bubbles revisited"]  # "filter bubbles" is not the phrase
    assert 0 < filtered.json()["items"][0]["score"] <= 1


def test_search_within_item_keys_is_exact(world):
    fake, query = FakeEmbedder(), "diversity of news and echo chambers"
    chosen = world.keys[1:]
    rows = world.db.execute("SELECT key, title, abstract FROM items WHERE key = ANY(%s)", (chosen,))
    q = fake.embed_query(query)

    def similarity(row):
        v = fake.embed([row["title"] + "\n" + row["abstract"]])[0]
        return sum(a * b for a, b in zip(q, v, strict=True))

    score = {r["key"]: similarity(r) for r in rows.fetchall()}
    reply = world.client.post("/v1/search/items", json={
        "query": query, "filters": {"item_keys": chosen}, "count": 50, "fields": ["key"]})
    found = [i["key"] for i in reply.json()["items"]]
    assert sorted(found) == sorted(chosen)  # every chosen item ...
    # ... best first; vectors are stored at half precision, so near-ties may swap
    assert all(score[a] >= score[b] - 1e-3 for a, b in zip(found, found[1:]))


def test_search_passages_skips_artefacts_and_names_the_item(world):
    reply = world.client.post("/v1/search/passages", json={"query": "recommender systems diverse news"})
    found = reply.json()["passages"]
    assert [p["passage"] for p in found] == [2, 0]  # the artefact has no vector
    assert found[0]["item"] == world.keys[0] and found[0]["pages"] == "3"
    assert found[0]["title"] == "Echo chambers in online news" and found[0]["authors"] == ["A. van Atteveldt"]
    none = world.client.post("/v1/search/passages", json={
        "query": "news", "filters": {"item_keys": [world.keys[1]]}})
    assert none.json() == {"passages": []}
    phrase = world.client.post("/v1/search/passages", json={"query": "news", "phrase": "like-minded news"})
    assert [p["passage"] for p in phrase.json()["passages"]] == [0]


def test_lookup_finds_by_identifier_and_title(world):
    admit(world.db, paper("Echo chambers in online news", "van Atteveldt", "10.31235/osf.io/pre", 2020,
                          "preprint", repository="SocArXiv"), "donation")
    world.db.commit()
    reply = world.client.post("/v1/lookup", json={"inputs": [
        "https://doi.org/10.1000/A", "10.31235/osf.io/pre", "Filter Bubbles, Revisited!", "No such title"]})
    by_doi, by_preprint, by_title, missing = reply.json()["results"]
    assert by_doi["found"] and by_doi["items"][0]["key"] == world.keys[0] and "note" not in by_doi
    assert by_preprint["items"][0]["key"] == world.keys[0]
    assert by_preprint["note"] == "held as the published version of this preprint"
    assert by_title["kind"] == "title" and by_title["items"][0]["key"] == world.keys[1]
    assert missing == {"input": "No such title", "kind": "title", "found": False, "items": []}
    assert world.registry.calls == []  # nothing held is asked of the registry


def test_lookup_of_unknown_doi_admits_it_once(world):
    first = world.client.post("/v1/lookup", json={"inputs": ["10.9000/NEW"]}).json()["results"][0]
    assert first["found"] and first["note"] == "added to the collection from the registry"
    row = world.db.execute("SELECT key, provenance_kind, provenance_member FROM items"
                           " WHERE doi = '10.9000/new'").fetchone()
    assert first["items"][0]["key"] == row["key"] and row["provenance_kind"] == "activity"
    assert row["provenance_member"] is not None
    again = world.client.post("/v1/lookup", json={"inputs": ["10.9000/new"]}).json()["results"][0]
    assert again["items"][0]["key"] == row["key"] and "note" not in again
    assert world.registry.calls == [("doi", "10.9000/new")]  # asked once
    unknown = world.client.post("/v1/lookup", json={"inputs": ["10.9000/nowhere"]}).json()["results"][0]
    assert not unknown["found"] and "registry does not know" in unknown["note"]


def test_lookup_with_registry_down_admits_nothing(world):
    world.registry.down = True
    result = world.client.post("/v1/lookup", json={"inputs": ["10.9000/new"]}).json()["results"][0]
    assert not result["found"] and "could not be reached, so nothing was added" in result["note"]
    assert world.db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 5


def test_item_in_full_by_either_key(world):
    full = world.client.get(f"/v1/items/{world.keys[0]}").json()
    assert full["details"]["volume"] == "12" and full["passages"] == 3 and full["pdf_held"] is False
    assert full["creators"][0]["lastName"] == "van Atteveldt" and full["provenance"] == "venue"
    assert world.client.get(f"/v1/items/{full['citation_key']}").json()["key"] == world.keys[0]
    missing = world.client.get("/v1/items/NOPE")
    assert missing.status_code == 404 and "NOPE" in missing.json()["error"]


def test_facets_count_values_within_filters(world):
    years = world.client.post("/v1/facets", json={"field": "year"}).json()
    assert years["values"] == [{"value": "2021", "items": 2}, {"value": "2023", "items": 2},
                               {"value": "2022", "items": 1}] and years["more"] is False
    within = world.client.post("/v1/facets", json={"field": "author", "filters": {"years": [2023]}})
    assert within.json()["values"] == [{"value": "kiddle", "items": 1}, {"value": "trilling", "items": 1}]
    venues = world.client.post("/v1/facets", json={"field": "venue", "contains": "proceed"}).json()
    assert venues["values"] == [{"value": "Proceedings of Tests", "items": 1}]
    assert world.client.post("/v1/facets", json={"field": "year", "count": 1}).json()["more"] is True
    assert world.client.post("/v1/facets", json={"field": "member"}).status_code == 400


def test_bibtex_in_both_formats(world):
    cite = world.client.get(f"/v1/items/{world.keys[0]}").json()["citation_key"]
    reply = world.client.post("/v1/bibtex", json={"keys": [cite, "NOPE", world.keys[3]]}).json()
    assert (reply["format"], reply["entries"], reply["missing"]) == ("biblatex", 2, ["NOPE"])
    assert reply["bibtex"].startswith(f"@article{{{cite},\n\tauthor = {{van Atteveldt, A.}},\n"
                                      "\ttitle = {{Echo chambers in online news}},\n"
                                      "\tjournaltitle = {Journal of Tests},\n\tdate = {2021},\n"
                                      "\tvolume = {12},\n\tpages = {1--20},\n\tdoi = {10.1000/a},\n}\n")
    assert "@inproceedings{" in reply["bibtex"] and "booktitle = {Proceedings of Tests}" in reply["bibtex"]
    assert "abstract" not in reply["bibtex"]
    old = world.client.post("/v1/bibtex", json={"keys": [cite], "format": "bibtex",
                                                "abstracts": True}).json()["bibtex"]
    assert "\tjournal = {Journal of Tests},\n\tyear = {2021}," in old and "abstract = {We study" in old
    assert world.client.post("/v1/bibtex", json={"keys": [cite], "format": "ris"}).status_code == 400


def test_reply_never_contains_a_member_name(world):
    admit(world.db, paper("A donated paper", "Lovelace", "10.1000/z", 2024), "donation",
          world.db.execute("SELECT id FROM members WHERE name = 'ada lovelace'").fetchone()["id"])
    world.db.commit()
    key = world.db.execute("SELECT key FROM items WHERE doi = '10.1000/z'").fetchone()["key"]
    replies = [
        world.client.post("/v1/items", json={"fields": list(__import__("scriptorium_server.retrieval", fromlist=["FIELDS"]).FIELDS)}),
        world.client.post("/v1/search/items", json={"query": "donated paper"}),
        world.client.get(f"/v1/items/{key}"),
        world.client.post("/v1/lookup", json={"inputs": ["10.1000/z"]}),
        world.client.post("/v1/facets", json={"field": "provenance"}),
        world.client.post("/v1/bibtex", json={"keys": [key]}),
        world.client.get("/v1/health"),
    ]
    text = " ".join(r.text for r in replies).lower()
    assert "ada lovelace" not in text and "grace hopper" not in text
    assert "provenance_member" not in text and '"member"' not in text
    assert '"provenance":"donation"' in text.replace(" ", "")  # the kind is shown


def test_openapi_file_is_current(world):
    """api/openapi.json is the contract the client is checked against. When
    the API changes, write it anew with SCRIPTORIUM_WRITE_OPENAPI=1."""
    spec = json.dumps(world.app.openapi(), indent=1, sort_keys=True) + "\n"
    if os.environ.get("SCRIPTORIUM_WRITE_OPENAPI"):
        OPENAPI.parent.mkdir(exist_ok=True)
        OPENAPI.write_text(spec)
    assert OPENAPI.exists() and OPENAPI.read_text() == spec, (
        "the API changed: run the tests once with SCRIPTORIUM_WRITE_OPENAPI=1")
