"""The SQLite store: exact scoring over float16 vectors, the fingerprint
and revision checks, the in-memory vectors' reload rule, and the sync
state's round trip."""

from types import SimpleNamespace

import numpy as np
import pytest

from scriptorium.config import Config
from scriptorium.index import HubIndex
from scriptorium.search import HubSearch
from scriptorium.store import VectorMatrix, from_blob, to_blob
from scriptorium.zotero import PaperRecord


def record(key, title="T", version=1, pdf="ok"):
    return PaperRecord(item_key=key, version=version, item_type="journalArticle", title=title,
                       creators="Kiddle", year="2026", doi="", venue="V",
                       citation_key=key.lower(), date_added="2026-10-23T10:00:00Z",
                       pdf_key="", pdf_path="", pdf_status=pdf)


class Embedder:
    """A fixed unit vector per text, so rankings are predictable."""

    revision = ""

    def __init__(self, table=None):
        self.table = table or {}

    def _vec(self, text):
        v = np.asarray(self.table.get(text, [1.0, 0.0, 0.0, 0.0]), dtype=np.float32)
        return (v / np.linalg.norm(v)).tolist()

    def encode_documents(self, texts):
        return [self._vec(t) for t in texts]

    def encode_query(self, text):
        return self._vec(text)

    encode_item_query = encode_query


def test_float16_round_trip_keeps_vectors_to_three_decimals():
    v = np.random.default_rng(1).normal(size=256)
    v = v / np.linalg.norm(v)
    back = np.asarray(from_blob(to_blob(v)))
    assert len(to_blob(v)) == 512 and np.abs(back - v).max() < 1e-3
    assert float(back @ v / np.linalg.norm(back)) > 0.9999


def test_matrix_scores_are_exact_cosines_best_first_within_the_admitted_rows():
    rng = np.random.default_rng(2)
    vecs = rng.normal(size=(5000, 8)).astype(np.float32)
    rows = [(i, f"K{i % 50}", to_blob(v)) for i, v in enumerate(vecs)]
    m = VectorMatrix.load(rows, 8, generation=0)
    q = rng.normal(size=8)
    stored = np.frombuffer(b"".join(r[2] for r in rows), np.float16).reshape(5000, 8)
    stored = stored.astype(np.float32)
    exact = (stored @ (q / np.linalg.norm(q))) / np.linalg.norm(stored, axis=1)
    top = m.top(q, 10, m.rows_of(None))
    assert [r for r, _ in top] == list(np.argsort(-exact)[:10])
    assert all(abs(s - exact[r]) < 1e-5 for r, s in top)
    admitted = m.rows_of({"K3", "K7"})
    assert {m.items[r] for r in admitted} == {"K3", "K7"} and len(admitted) == 200
    assert {m.items[r] for r, _ in m.top(q, 5, admitted)} <= {"K3", "K7"}
    assert m.top(q, 5, m.rows_of(set())) == []


def test_a_different_model_or_dimension_is_refused(tmp_path):
    HubIndex(Config(index_dir=tmp_path, embedding_dim=4), embedder=Embedder()).close()
    with pytest.raises(RuntimeError, match="embedding_dim 4"):
        HubIndex(Config(index_dir=tmp_path, embedding_dim=8), embedder=Embedder())
    with pytest.raises(RuntimeError, match="embedding_model"):
        HubIndex(Config(index_dir=tmp_path, embedding_dim=4, embedding_model="other/model"),
                 embedder=Embedder())


def test_a_different_model_revision_is_refused_for_writes_and_searches(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    first = Embedder()
    first.revision = "aaaa"
    hub = HubIndex(cfg, embedder=first)
    hub.upsert_catalogue([record("A")], {})
    assert hub.store.meta("embedding_revision") == "aaaa"
    hub.close()
    other = Embedder()
    other.revision = "bbbb"
    hub = HubIndex(cfg, embedder=other)
    with pytest.raises(RuntimeError, match="revision aaaa"):
        hub.upsert_catalogue([record("B")], {})
    with pytest.raises(RuntimeError, match="revision aaaa"):
        HubSearch(cfg, hub=hub, embedder=other).search_items("x")


def test_vectors_reload_after_writes_but_not_while_a_sync_writes(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    emb = Embedder({"T near": [1, 0, 0, 0], "T far": [0, 1, 0, 0], "q": [1, 0.1, 0, 0]})
    hub = HubIndex(cfg, embedder=emb)
    writing = {"now": False}
    search = HubSearch(cfg, hub=hub, embedder=emb, writing=lambda: writing["now"])
    hub.upsert_catalogue([record("FAR", "T far")], {})
    assert [h["item_key"] for h in search.search_items("q")] == ["FAR"]
    writing["now"] = True  # a sync is running: the loaded vectors are used
    hub.upsert_catalogue([record("NEAR", "T near")], {})
    assert [h["item_key"] for h in search.search_items("q")] == ["FAR"]
    writing["now"] = False  # done: the next search reloads
    assert [h["item_key"] for h in search.search_items("q")] == ["NEAR", "FAR"]
    hub.delete_catalogue("NEAR")
    assert [h["item_key"] for h in search.search_items("q")] == ["FAR"]
    search.release()
    assert not search.loaded and search.search_items("q")[0]["item_key"] == "FAR"


def test_a_replaced_chunk_is_skipped_by_a_search_with_older_vectors(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    hub = HubIndex(cfg, embedder=Embedder())
    writing = {"now": False}
    search = HubSearch(cfg, hub=hub, embedder=Embedder(), writing=lambda: writing["now"])
    hub.upsert_catalogue([record("P")], {})
    chunks = [SimpleNamespace(name=f"p pages {i}", text=f"t{i}") for i in range(3)]
    hub.add_paper(record("P"), chunks, "h1")
    assert len(search.search("q", k=10)) == 3
    writing["now"] = True
    hub.add_paper(record("P"), chunks[:1], "h2")  # re-embedded with one chunk
    hits = search.search("q", k=10)
    assert len(hits) == 0 or all(h.text == "t0" for h in hits)


def test_sync_state_round_trips_through_the_store(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    hub = HubIndex(cfg, embedder=Embedder())
    hub.upsert_catalogue([record("A"), record("B", pdf="no_pdf")], {})
    hub.add_paper(record("A"), [SimpleNamespace(name="a pages 1-2", text="x")], "hash")
    hub.record_failure("C", "PDF parsed to empty text", "h:2")
    hub.state.catalogue["B"] = 7  # e.g. a version rebase
    hub.state.library_version, hub.state.last_sync_at = 42, "2026-10-23T12:00:00Z"
    hub.save_state()
    hub.close()
    s = HubIndex(cfg, embedder=Embedder()).state
    assert s.items["A"]["chunks"] == 1 and s.items["A"]["content_hash"] == "hash"
    assert s.failed == {"C": "PDF parsed to empty text"} and s.failed_hash == {"C": "h:2"}
    assert s.catalogue == {"A": 1, "B": 7} and s.catalogue_pdf == {"A": True, "B": False}
    assert (s.library_version, s.last_sync_at) == (42, "2026-10-23T12:00:00Z")


def test_chunk_search_without_a_catalogue_row_still_finds_the_chunk(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    hub = HubIndex(cfg, embedder=Embedder())
    hub.add_paper(record("Z"), [SimpleNamespace(name="z pages 1", text="x")], "h")
    hit = HubSearch(cfg, hub=hub, embedder=Embedder()).search("q")[0]
    assert (hit.item_key, hit.citation_key, hit.name) == ("Z", "Z", "Z pages 1")
