"""Filling missing vectors: items, passages, artefacts, the revision check."""

import math

import pytest

from scriptorium_server import embedding

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

    def embed(self, texts, batch_size=32):
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


def test_pending_items_get_vectors(db, monkeypatch):
    keys = [admit(db, paper(n, "About echo chambers." if n == 0 else ""), "venue")[0]
            for n in range(5)]
    embedder = Recording()
    monkeypatch.setattr(embedding, "BATCHES_AT_ONCE", 1)
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


def test_embed_pending_fills_items_and_passages_and_leaves_the_model_alone_otherwise(db):
    from scriptorium_server.embedding import FakeEmbedder, embed_pending

    db.execute("INSERT INTO items (key, type, title, title_norm, provenance_kind)"
               " VALUES ('K1', 'journalArticle', 'A title', 'a title', 'activity')")
    db.execute("INSERT INTO passages (item, idx, part, text) VALUES ('K1', 0, 'body', 'words here')")
    db.commit()
    assert embed_pending(db, FakeEmbedder()) == (1, 1)

    class Untouchable(FakeEmbedder):
        def embed(self, texts):
            raise AssertionError("nothing waits; the model must not be used")

    assert embed_pending(db, Untouchable()) == (0, 0)


# --- the real embedder's device handling (no model: the model is replaced) ----------


class _Model:
    """Stands in for the sentence-transformers model on one device."""

    def __init__(self, device, fails=False):
        self.device, self.fails, self.calls = device, fails, 0

    def encode(self, texts, **options):
        self.calls += 1
        if self.fails:
            raise RuntimeError("CUDA error: unknown error")
        return [FakeEmbedder()._vector(t) for t in texts]


class _Vector(list):
    def tolist(self):
        return list(self)


def _real(monkeypatch, best, fails_on=()):
    from scriptorium_server.embedding import RealEmbedder

    monkeypatch.delenv("SCRIPTORIUM_DEVICE", raising=False)
    loaded = []
    embedder = RealEmbedder(revision="r1")
    monkeypatch.setattr(embedder, "_best_device", lambda: best)
    monkeypatch.setattr(embedder, "_empty_cache", lambda: None)

    def load(device):
        model = _Model(device, fails=device in fails_on)
        real = model.encode
        model.encode = lambda texts, **o: [_Vector(v) for v in real(texts, **o)]
        loaded.append(device)
        return model

    monkeypatch.setattr(embedder, "_load", load)
    return embedder, loaded


def test_real_embedder_uses_the_best_device(monkeypatch):
    embedder, loaded = _real(monkeypatch, "cuda")
    assert len(embedder.embed(["a b", "c"])) == 2
    assert embedder.device == "cuda" and loaded == ["cuda"]


def test_a_failing_gpu_moves_the_embedder_to_the_cpu_and_keeps_it_there(monkeypatch, capsys):
    embedder, loaded = _real(monkeypatch, "cuda", fails_on=("cuda",))
    assert embedder.embed_query("echo chambers") == FakeEmbedder().embed_query(
        "Instruct: Given a research question, retrieve relevant passages from"
        " academic papers that help answer the question\nQuery: echo chambers")
    assert embedder.device == "cpu" and loaded == ["cuda", "cpu"]
    assert "continuing on the CPU" in capsys.readouterr().out
    embedder.embed(["again"])
    assert loaded == ["cuda", "cpu"]  # no second attempt at the GPU


def test_an_error_on_the_cpu_is_raised(monkeypatch):
    embedder, _ = _real(monkeypatch, "cpu", fails_on=("cpu",))
    with pytest.raises(RuntimeError):
        embedder.embed(["a"])


def test_pending_embedding_stops_at_its_limit(db):
    from scriptorium_server.embedding import embed_pending

    for n in range(5):
        admit(db, paper(n), "venue")
    embedder = Recording()
    assert embed_pending(db, embedder, batch_size=2, limit=3) == (3, 0)
    assert embed_pending(db, embedder, batch_size=2, limit=3) == (2, 0)
    assert embed_pending(db, embedder, batch_size=2, limit=3) == (0, 0)


class OutOfMemoryError(RuntimeError):
    """Named as torch's error is."""


def test_texts_are_encoded_by_length_and_returned_in_their_order(monkeypatch):
    embedder, _ = _real(monkeypatch, "cuda")
    seen = []
    real = embedder._load("cuda")
    monkeypatch.setattr(embedder, "_load", lambda device: type("M", (), {"encode": staticmethod(
        lambda texts, **o: (seen.append(list(texts)), real.encode(texts, **o))[1])})())
    texts = ["bb bb", "a", "dddd dddd dddd", "ccc ccc c"]
    assert embedder.embed(texts, batch_size=2) == FakeEmbedder().embed(texts)
    assert seen == [["dddd dddd dddd", "ccc ccc c"], ["bb bb", "a"]]


def test_a_batch_too_large_for_the_gpu_is_encoded_in_halves_on_the_gpu(monkeypatch):
    embedder, loaded = _real(monkeypatch, "cuda")
    real = embedder._load("cuda")
    sizes = []

    def encode(texts, **options):
        sizes.append(len(texts))
        if len(texts) > 2:
            raise OutOfMemoryError("CUDA out of memory")
        return real.encode(texts, **options)

    monkeypatch.setattr(embedder, "_load", lambda device: type("M", (), {"encode": staticmethod(encode)})())
    texts = ["one", "two two", "three three three", "four four four four", "five"]
    assert embedder.embed(texts, batch_size=8) == FakeEmbedder().embed(texts)
    assert sizes == [5, 2, 3, 1, 2] and embedder.device == "cuda"
