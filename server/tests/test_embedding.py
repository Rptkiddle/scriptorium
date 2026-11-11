"""Filling missing vectors: items, passages, artefacts, the revision check."""

import math

import pytest

from scriptorium_server.embedding import (
    DIM,
    FakeEmbedder,
    embed_pending_items,
    embed_pending_passages,
    item_document,
)
from scriptorium_server.items import admit


def paper(n, abstract=""):
    return {"itemType": "journalArticle", "title": f"Paper number {n}", "DOI": f"10.1000/{n}",
            "abstractNote": abstract, "creators": []}


class Recording(FakeEmbedder):
    def __init__(self, revision=""):
        self.revision = revision
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return super().embed(texts)


def test_fake_embedder_is_deterministic_unit_length_and_word_sensitive():
    fake = FakeEmbedder()
    a, b, c = fake.embed(["echo chambers online", "online echo chambers", "protein folding"])
    assert len(a) == DIM and math.isclose(sum(x * x for x in a), 1.0)
    assert a == b and a != c
    assert fake.embed_query("echo chambers online") == a
    assert math.isclose(sum(x * x for x in fake.embed([""])[0]), 1.0)


def test_item_document_is_title_then_abstract_on_one_line_each():
    assert item_document("A Title", " Two\n lines  here ") == "A Title\nTwo lines here"
    assert item_document("A Title", None) == "A Title"


def test_pending_items_get_vectors(db):
    keys = [admit(db, paper(n, "About echo chambers." if n == 0 else ""), "venue")[0]
            for n in range(5)]
    embedder = Recording()
    assert embed_pending_items(db, embedder, batch_size=2) == 5
    assert [len(batch) for batch in embedder.batches] == [2, 2, 1]
    assert "Paper number 0\nAbout echo chambers." in embedder.batches[0] + embedder.batches[1] \
        + embedder.batches[2]
    assert db.execute("SELECT count(*) AS n FROM items WHERE embedding IS NULL").fetchone()["n"] == 0
    assert embed_pending_items(db, embedder, batch_size=2) == 0  # nothing left to do
    nearest = db.execute("SELECT key FROM items ORDER BY embedding <=> %s::halfvec LIMIT 1",
                         (str(FakeEmbedder().embed_query("echo chambers")),)).fetchone()
    assert nearest["key"] == keys[0]


def test_artefact_passages_get_no_vector(db):
    key, _ = admit(db, paper(1), "donation")
    db.cursor().executemany(
        "INSERT INTO passages (item, idx, part, text, embed_text, artefact)"
        " VALUES (%s, %s, 'body', %s, %s, %s)",
        [(key, 0, "Prose with a marker [3].", "Prose with a marker.", False),
         (key, 1, "1.1 0.1 2.2 0.2", None, True),
         (key, 2, "More prose.", None, False)])
    embedder = Recording()
    assert embed_pending_passages(db, embedder) == 2
    assert embedder.batches == [["Prose with a marker.", "More prose."]]  # the marker-free text
    rows = db.execute("SELECT idx, embedding IS NULL AS empty FROM passages ORDER BY idx").fetchall()
    assert [(r["idx"], r["empty"]) for r in rows] == [(0, False), (1, True), (2, False)]
    assert embed_pending_passages(db, embedder) == 0


def test_an_embedder_of_another_revision_is_refused(db):
    admit(db, paper(1), "venue")
    embed_pending_items(db, Recording("rev-a"))
    admit(db, paper(2), "venue")
    with pytest.raises(RuntimeError, match="rev-a.*rev-b"):
        embed_pending_items(db, Recording("rev-b"))
    assert embed_pending_items(db, Recording("rev-a")) == 1
    assert embed_pending_items(db, Recording()) == 0  # one without a revision is not checked
