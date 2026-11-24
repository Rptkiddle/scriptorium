"""Collections through the API: a member's own lists of items, histories
recorded by workflow runs, the collections filter, and what the usage log
keeps of a search."""

import pytest
from fastapi.testclient import TestClient

from scriptorium_server import db as database, members, settings
from scriptorium_server.api import create_app
from scriptorium_server.embedding import FakeEmbedder, embed_pending_items
from scriptorium_server.items import admit

TITLES = ["Echo chambers in online news", "Filter bubbles revisited",
          "Agent based models of opinion", "News recommender diversity",
          "Protein folding with transformers"]


@pytest.fixture
def world(db, empty_schema, tmp_path, monkeypatch):
    """Five items and two members, each with a client."""
    monkeypatch.setenv("SCRIPTORIUM_COMMIT", "abc1234")
    keys = [admit(db, {"itemType": "journalArticle", "title": title, "DOI": f"10.1/{n}",
                       "date": "2021", "abstractNote": title, "creators": []}, "venue")[0]
            for n, title in enumerate(TITLES)]
    embed_pending_items(db, FakeEmbedder())
    api_keys = {name: members.add(db, name) for name in ("ada", "grace")}
    db.commit()
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(),
                     settings.load(tmp_path / "none.toml"))

    def client(name):
        return TestClient(app, headers={"Authorization": f"Bearer {api_keys[name]}"})
    ada, grace = client("ada"), client("grace")

    def post(who, path, **body):
        return who.post(f"/v1/collections/{path}", json=body)
    return type("World", (), {"ada": ada, "grace": grace, "keys": keys, "db": db, "post": post})


def test_an_active_collection_is_made_by_adding_to_a_new_name(world):
    reply = world.post(world.ada, "add", collection="To read", keys=world.keys[:2] + ["NOPE"]).json()
    assert reply["kind"] == "active" and reply["name"] == "To read" and reply["new_collection"]
    assert (reply["added"], reply["item_count"], reply["not_held"]) == (2, 2, ["NOPE"])
    again = world.post(world.ada, "add", collection="to  read", keys=world.keys[1:3]).json()
    assert again["collection"] == reply["collection"] and not again["new_collection"]
    assert (again["added"], again["already_in_it"], again["item_count"]) == (1, 1, 3)
    by_number = world.post(world.ada, "add", collection=str(reply["collection"]),
                           keys=[world.keys[4]]).json()
    assert by_number["item_count"] == 4
    assert world.post(world.ada, "add", collection="999", keys=[]).status_code == 404
    assert world.post(world.ada, "add", collection="  ", keys=[]).status_code == 400


def test_a_citation_key_names_an_item_too(world):
    cited = world.db.execute("SELECT citation_key FROM items WHERE key = %s",
                             (world.keys[0],)).fetchone()["citation_key"]
    reply = world.post(world.ada, "add", collection="By citation key", keys=[cited]).json()
    assert reply["added"] == 1
    shown = world.post(world.ada, "show", collection="By citation key").json()
    assert [i["key"] for i in shown["items"]] == [world.keys[0]]


def test_a_workflow_run_is_recorded_as_a_history_with_ranks(world):
    asked = {"query": "echo chambers", "filters": {"year_from": 2020}}
    order = [world.keys[3], world.keys[0], world.keys[1]]
    reply = world.post(world.ada, "record", workflow="Search", name="echo chambers",
                       request=asked, keys=order).json()
    assert reply["kind"] == "history" and reply["workflow"] == "search" and reply["request"] == asked
    assert reply["item_count"] == 3
    more = world.post(world.ada, "record", collection=str(reply["collection"]),
                      keys=[world.keys[1], world.keys[2]]).json()  # the rest of a long run
    assert more["added"] == 1 and more["item_count"] == 4
    shown = world.post(world.ada, "show", collection=str(reply["collection"]),
                       fields=["title"]).json()
    assert [(i["rank"], i["title"]) for i in shown["items"]] == [
        (1, TITLES[3]), (2, TITLES[0]), (3, TITLES[1]), (4, TITLES[2])]
    assert "key" not in shown["items"][0] and shown["item_count"] == 4
    page = world.post(world.ada, "show", collection=str(reply["collection"]), count=2, offset=2).json()
    assert [i["rank"] for i in page["items"]] == [3, 4]
    assert world.post(world.ada, "record", name="no workflow", keys=[]).status_code == 400
    assert world.post(world.ada, "add", collection=str(reply["collection"]),
                      keys=[world.keys[4]]).status_code == 400  # a history is not added to by hand


def test_collections_are_listed_newest_first_and_narrowed(world):
    world.post(world.ada, "add", collection="Methods", keys=world.keys[:1])
    world.post(world.ada, "record", workflow="discovery", name="polarisation review",
               request={"question": "what drives polarisation"}, keys=world.keys[:3])
    listed = world.post(world.ada, "list").json()
    assert listed["total"] == 2
    assert [(c["kind"], c["name"], c["item_count"]) for c in listed["collections"]] == [
        ("history", "polarisation review", 3), ("active", "Methods", 1)]
    assert world.post(world.ada, "list", kind="active").json()["total"] == 1
    assert world.post(world.ada, "list", contains="drives polar").json()["total"] == 1  # what was asked
    assert world.post(world.ada, "list", kind="other").status_code == 400


def test_a_history_is_kept_as_an_active_collection_renamed_and_deleted(world):
    history = world.post(world.ada, "record", workflow="search", name="echo chambers",
                         keys=world.keys[:2]).json()["collection"]
    world.post(world.ada, "add", collection="Reading", keys=world.keys[:1])
    kept = world.post(world.ada, "edit", collection=str(history), keep=True, name="Echo chambers").json()
    assert kept["kind"] == "active" and kept["name"] == "Echo chambers" and kept["item_count"] == 2
    taken = world.post(world.ada, "edit", collection="Echo chambers", name="reading")
    assert taken.status_code == 400 and "already" in taken.json()["error"]
    assert world.post(world.ada, "edit", collection="Reading", name="Read later").json()[
        "name"] == "Read later"
    assert world.post(world.ada, "edit", collection="Read later").status_code == 400
    gone = world.post(world.ada, "edit", collection="Read later", delete=True).json()
    assert gone["name"] == "Read later" and gone["items_it_held"] == 1
    assert world.post(world.ada, "show", collection="Read later").status_code == 404
    world.db.rollback()
    assert world.db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 5  # items untouched


def test_items_are_removed_from_a_collection(world):
    world.post(world.ada, "add", collection="Reading", keys=world.keys[:3])
    reply = world.post(world.ada, "remove", collection="Reading",
                       keys=[world.keys[0], world.keys[4], "NOPE"]).json()
    assert (reply["removed"], reply["not_in_it"], reply["item_count"]) == (1, 2, 2)


def test_one_collection_takes_the_items_of_another(world):
    history = world.post(world.ada, "record", workflow="search", name="q",
                         keys=world.keys[:3]).json()["collection"]
    reply = world.post(world.ada, "add", collection="Kept", keys=[world.keys[4]],
                       from_collection=str(history)).json()
    assert reply["added"] == 4 and reply["item_count"] == 4


def test_no_member_sees_or_changes_anothers_collections(world):
    number = str(world.post(world.ada, "add", collection="Private", keys=world.keys[:2]).json()[
        "collection"])
    assert world.post(world.grace, "list").json() == {"total": 0, "collections": []}
    for path, body in (("show", {}), ("add", {"keys": [world.keys[3]]}),
                       ("remove", {"keys": [world.keys[0]]}), ("edit", {"delete": True}),
                       ("record", {"keys": [world.keys[3]]})):
        assert world.post(world.grace, path, collection=number, **body).status_code == 404
    assert world.post(world.grace, "show", collection="Private").status_code == 404
    assert world.grace.post("/v1/items", json={"filters": {"collections": [number]}}).status_code == 404
    own = world.post(world.grace, "add", collection="Private", keys=world.keys[4:]).json()
    assert own["new_collection"] and own["collection"] != int(number)  # the same name, their own
    assert world.post(world.ada, "show", collection=number).json()["item_count"] == 2
    assert world.grace.get(f"/v1/items/{world.keys[0]}").json()["collections"] == []


def test_an_item_shows_the_members_labels_and_the_questions_that_found_it(world):
    world.post(world.ada, "record", workflow="search", name="echo chambers",
               request={"query": "echo chambers"}, keys=[world.keys[1], world.keys[0]])
    world.post(world.ada, "add", collection="Reading", keys=[world.keys[0]])
    held = world.ada.get(f"/v1/items/{world.keys[0]}").json()["collections"]
    assert [(c["kind"], c["name"]) for c in held] == [("active", "Reading"),
                                                      ("history", "echo chambers")]
    assert held[1]["rank"] == 2 and held[1]["request"] == {"query": "echo chambers"}
    assert "rank" not in held[0]
    assert world.ada.get(f"/v1/items/{world.keys[4]}").json()["collections"] == []


def test_the_find_requests_can_be_confined_to_collections(world):
    world.post(world.ada, "add", collection="Reading", keys=world.keys[:2])
    world.post(world.ada, "add", collection="Empty", keys=[])
    inside = {"filters": {"collections": ["Reading"]}}
    assert world.ada.post("/v1/items", json=inside).json()["total"] == 2
    found = world.ada.post("/v1/search/items", json={"query": "news", "count": 5, **inside}).json()
    assert {i["key"] for i in found["items"]} == set(world.keys[:2])
    both = {"filters": {"collections": ["Reading"], "item_keys": [world.keys[1], world.keys[4]]}}
    assert [i["key"] for i in world.ada.post("/v1/items", json=both).json()["items"]] == [world.keys[1]]
    assert world.ada.post("/v1/items", json={"filters": {"collections": ["Empty"]}}).json()["total"] == 0
    assert world.ada.post("/v1/facets", json={"field": "year", **inside}).json()["values"] == [
        {"value": "2021", "items": 2}]


def test_the_usage_log_keeps_keys_ranks_scores_and_the_code_version(world):
    reply = world.ada.post("/v1/search/items", json={"query": "echo chambers", "count": 3,
                                                     "fields": ["title"]}).json()
    assert "ranking" not in reply and "key" not in reply["items"][0]  # the reply is as asked
    world.ada.post("/v1/items", json={"count": 2, "offset": 1, "fields": ["title"]})
    world.db.rollback()
    rows = world.db.execute("SELECT request, reply, server_commit FROM usage_log ORDER BY id").fetchall()
    search, listing = rows[-2], rows[-1]
    ranking = search["reply"]["ranking"]
    assert [r["rank"] for r in ranking] == [1, 2, 3] and set(ranking[0]) == {"key", "rank", "score"}
    assert [r["score"] for r in ranking] == [i["score"] for i in reply["items"]]
    assert {r["key"] for r in ranking} <= set(world.keys) and search["server_commit"] == "abc1234"
    assert [r["rank"] for r in listing["reply"]["ranking"]] == [2, 3]


def test_deleting_an_item_takes_it_out_of_collections(world):
    world.post(world.ada, "add", collection="Reading", keys=world.keys[:2])
    world.db.execute("DELETE FROM items WHERE key = %s", (world.keys[0],))
    world.db.commit()
    assert world.post(world.ada, "show", collection="Reading").json()["item_count"] == 1
