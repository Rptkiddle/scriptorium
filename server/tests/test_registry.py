"""The registries: a DOI at Crossref, an arXiv ID at DataCite, as items."""

import pytest
import requests

from scriptorium_server.sources import apis, crossref
from scriptorium_server.sources.crossref import RegistryUnavailable, fetch

WORK = {"DOI": "10.1000/J.1", "type": "journal-article", "title": ["Echo <i>Chambers</i>"],
        "author": [{"given": "Ada", "family": "Lovelace"}, {"name": "A Consortium"}],
        "issued": {"date-parts": [[2025, 3, 4]]}, "container-title": ["Journal of Tests"],
        "volume": "12", "issue": "3", "page": "1-20", "ISSN": ["1234-5678"],
        "abstract": "<jats:p>What we found.</jats:p>", "publisher": "Test Press",
        "relation": {"has-preprint": [{"id-type": "doi", "id": "10.31235/osf.io/abcde"}]}}
DATACITE = {"data": {"attributes": {
    "titles": [{"title": "Attention Is All You Need"}], "publicationYear": 2017,
    "creators": [{"givenName": "Ashish", "familyName": "Vaswani"}, {"name": "Google Brain"}],
    "descriptions": [{"descriptionType": "Abstract", "description": "The dominant\nmodels."}]}}}


class Session:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error, self.urls = status, body, error, []

    def request(self, method, url, **kwargs):
        self.urls.append(url)
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        response.json = lambda: self.body
        return response


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(apis.time, "sleep", lambda _: None)


def test_a_doi_known_to_crossref_becomes_an_item_with_its_relations():
    session = Session(body={"message": WORK})
    record, relation = fetch("doi", "10.1000/J.1", session)
    assert session.urls == ["https://api.crossref.org/works/10.1000/j.1"]
    assert record["itemType"] == "journalArticle" and record["title"] == "Echo Chambers"
    assert record["creators"] == [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]
    assert (record["date"], record["DOI"], record["volume"], record["pages"]) == (
        "2025-03-04", "10.1000/j.1", "12", "1-20")
    assert record["abstractNote"] == "What we found." and record["publicationTitle"] == "Journal of Tests"
    assert relation == WORK["relation"]


def test_an_arxiv_id_is_asked_of_datacite():
    session = Session(body=DATACITE)
    record, relation = fetch("arxiv", "1706.03762", session)
    assert session.urls == ["https://api.datacite.org/dois/10.48550/arxiv.1706.03762"]
    assert record == {
        "itemType": "preprint", "title": "Attention Is All You Need", "date": "2017",
        "creators": [{"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"},
                     {"creatorType": "author", "name": "Google Brain"}],
        "abstractNote": "The dominant models.", "repository": "arXiv",
        "archiveID": "arXiv:1706.03762", "DOI": "10.48550/arXiv.1706.03762",
        "url": "https://arxiv.org/abs/1706.03762"}
    assert relation is None


def test_an_identifier_the_registry_does_not_know_gives_none():
    assert fetch("doi", "10.1000/nowhere", Session(status=404)) is None
    assert fetch("arxiv", "9999.99999", Session(status=404)) is None
    assert fetch("doi", "10.1000/untitled", Session(body={"message": {"DOI": "10.1000/untitled"}})) is None


def test_an_unreachable_registry_is_said_to_be_unreachable():
    with pytest.raises(RegistryUnavailable):
        fetch("doi", "10.1000/j.1", Session(error=requests.ConnectionError("no route")))
    with pytest.raises(RegistryUnavailable):
        fetch("doi", "10.1000/j.1", Session(status=503))


def test_crossref_types_map_to_item_types():
    def kind(crossref_type):
        return crossref.crossref_record_to_item({**WORK, "type": crossref_type}, {})["itemType"]

    assert [kind(t) for t in ("proceedings-article", "posted-content", "book-chapter", "other")] == [
        "conferencePaper", "preprint", "bookSection", "journalArticle"]
