"""Exact lookup: identifiers an item answers to, input classification,
hub_lookup's matches, and the payload refresh that keeps vectors."""

from qdrant_client import QdrantClient

from scriptorium.config import Config
from scriptorium.identifiers import classify, item_identifiers, normalise_title
from scriptorium.index import (
    CATALOGUE,
    CATALOGUE_DOC_VERSION,
    CATALOGUE_PAYLOAD_VERSION,
    HubIndex,
    SyncState,
    _catalogue_id,
)
from scriptorium.search import HubSearch
from scriptorium.service import catalogue_outdated
from scriptorium.zotero import METADATA_VERSION, PaperRecord


def record(key, title, doi="", extra="", archive_id="", item_type="journalArticle"):
    return PaperRecord(item_key=key, version=1, item_type=item_type, title=title,
                       creators="Kiddle", year="2026", doi=doi, venue="V", citation_key=key.lower(),
                       date_added="2026-10-15T18:00:00Z", pdf_key="", pdf_path="",
                       pdf_status="no_pdf", extra=extra, archive_id=archive_id)


class Embedder:
    """A distinct unit vector per text length; ranking is not under test."""

    def encode_documents(self, texts):
        return [self._vec(t) for t in texts]

    def encode_item_query(self, text):
        return self._vec(text)

    @staticmethod
    def _vec(text):
        v = [1.0, float(len(text) % 7), 0.5, 0.0]
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v]


def test_classify_recognises_doi_arxiv_and_title_forms():
    assert classify("10.1093/POQ/nfab018") == ("doi", "10.1093/poq/nfab018")
    assert classify("https://doi.org/10.1093/poq/nfab018.") == ("doi", "10.1093/poq/nfab018")
    assert classify("doi:10.1000/x") == ("doi", "10.1000/x")
    assert classify("https://doi.org/10.31235/osf.io/e3kvj_v1") == ("doi", "10.31235/osf.io/e3kvj")
    for form in ("2603.13270", "arXiv:2603.13270v2", "https://arxiv.org/abs/2603.13270v1",
                 "https://arxiv.org/pdf/2603.13270.pdf", "10.48550/arXiv.2603.13270"):
        assert classify(form) == ("arxiv", "2603.13270"), form
    assert classify("The Public Sphere as a Dynamic Network") == (
        "title", "the public sphere as a dynamic network")


def test_item_identifiers_reads_own_ids_and_extra_lines_by_value():
    own, recorded = item_identifiers("10.48550/arXiv.2501.00001", "arXiv:2501.00001", "")
    assert own == ["arxiv:2501.00001"] and recorded == []
    extra = ("arXiv: 2603.13270\nSocArXiv: 10.31235/OSF.IO/ABCDE\nCitation Key: x2021\n"
             "surveillance-why: see 2021.12345 for context\nDOI: 10.1000/own")
    own, recorded = item_identifiers("", "", extra)
    assert own == ["doi:10.1000/own"]
    assert recorded == ["arxiv:2603.13270", "doi:10.31235/osf.io/abcde"]


def test_normalise_title_drops_markup_accents_and_punctuation():
    assert normalise_title("The <i>Public</i> Sphere &amp; Café\\& co.") == "the public sphere cafe co"
    assert normalise_title("If You Have Choices, Why Not Choose?") == "if you have choices why not choose"


def _hub(tmp_path, records):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    client = QdrantClient(":memory:")
    hub = HubIndex(cfg, client=client, embedder=Embedder())
    hub.upsert_catalogue(records, {})
    return hub, HubSearch(cfg, client=client, embedder=Embedder()), client


def test_lookup_matches_each_way_and_offers_candidates_for_titles(tmp_path):
    _, search, _ = _hub(tmp_path, [
        record("PUBL", "A Published Paper", doi="10.1000/a", extra="arXiv: 2603.13270"),
        record("ARXV", "An arXiv Preprint", doi="10.48550/arXiv.2501.00001",
               archive_id="arXiv:2501.00001", item_type="preprint"),
        record("TITL", "Splintering and Centralizing: A Test"),
    ])
    r = search.lookup(["https://doi.org/10.1000/A", "arXiv:2603.13270v2", "2501.00001",
                       "splintering and centralizing a test", "An Unknown Title", "10.9999/none"])
    assert [(e["found"], [m["item_key"] for m in e["matches"]]) for e in r] == [
        (True, ["PUBL"]), (True, ["PUBL"]), (True, ["ARXV"]), (True, ["TITL"]),
        (False, []), (False, [])]
    assert [e["matches"][0]["matched_on"] for e in r[:4]] == [
        "doi", "recorded preprint of this item", "arxiv", "title"]
    assert len(r[4]["candidates"]) == 3 and "candidates" not in r[5]


def test_payload_refresh_keeps_vectors_and_rewrites_fields(tmp_path):
    hub, _, client = _hub(tmp_path, [record("PUBL", "A Published Paper", doi="10.1000/a")])
    pid = _catalogue_id("PUBL")
    before = client.retrieve(CATALOGUE, ids=[pid], with_vectors=True)[0]
    assert before.payload["preprint_identifiers"] == []
    n = hub.refresh_catalogue_payloads(
        [record("PUBL", "A Published Paper", doi="10.1000/a", extra="arXiv: 2603.13270"),
         record("NEWX", "Not catalogued yet")], {})
    after = client.retrieve(CATALOGUE, ids=[pid], with_vectors=True)[0]
    assert n == 1 and after.vector == before.vector
    assert after.payload["preprint_identifiers"] == ["arxiv:2603.13270"]


def test_catalogue_outdated_follows_the_three_stamps():
    state = SyncState(library_version=1, items={}, failed={}, catalogue={},
                      catalogue_meta=METADATA_VERSION, catalogue_doc=CATALOGUE_DOC_VERSION,
                      catalogue_payload=CATALOGUE_PAYLOAD_VERSION)
    assert not catalogue_outdated(state)
    state.catalogue_payload = 0
    assert catalogue_outdated(state)
