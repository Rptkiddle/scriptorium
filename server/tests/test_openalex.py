"""OpenAlex as a harvest source: record form, harvest rules, paging."""

from scriptorium_server.sources import openalex

WORK = {
    "id": "https://openalex.org/W123",
    "doi": "https://doi.org/10.1093/JOC/JQAB001",
    "title": "Algorithmic  news curation",
    "publication_date": "2022-03-01",
    "publication_year": 2022,
    "type": "article",
    "language": "en",
    "is_retracted": False,
    "authorships": [{"author": {"display_name": "J. van Houten"},
                     "raw_author_name": "Jan van Houten"},
                    {"author": {"display_name": "Ada Lovelace"}},
                    {"author": {"display_name": "Plato"}}],
    "biblio": {"volume": "72", "issue": "2", "first_page": "101", "last_page": "120"},
    "primary_location": {"source": {"id": "https://openalex.org/S107737141",
                                    "display_name": "Journal of Communication",
                                    "issn": ["0021-9916", "1460-2466"]}},
    "abstract_inverted_index": {"News": [0], "matters.": [1]},
}


def test_to_record_maps_fields():
    r = openalex.to_record(WORK)
    assert r["itemType"] == "journalArticle"
    assert r["title"] == "Algorithmic news curation"
    assert r["DOI"] == "10.1093/joc/jqab001"
    assert r["date"] == "2022-03-01"
    assert r["publicationTitle"] == "Journal of Communication"
    assert (r["volume"], r["issue"], r["pages"]) == ("72", "2", "101-120")
    assert r["ISSN"] == "0021-9916, 1460-2466"
    assert r["abstractNote"] == "News matters."
    assert r["extra"] == "OpenAlex: W123"


def test_names_split_with_particles():
    creators = openalex.to_record(WORK)["creators"]
    assert creators[0] == {"creatorType": "author", "firstName": "Jan",
                           "lastName": "van Houten"}
    assert creators[1]["lastName"] == "Lovelace"
    assert creators[2] == {"creatorType": "author", "name": "Plato"}


def test_rules():
    assert openalex.rule(WORK) is None
    assert openalex.rule({**WORK, "type": "review"}) is None
    assert openalex.rule({**WORK, "type": "conference-paper"}) is None
    assert openalex.rule({**WORK, "type": "software-paper"}) is None
    assert openalex.rule({**WORK, "type": "peer-review"}) == "type: peer-review"
    assert openalex.rule({**WORK, "type": "editorial"}) == "type: editorial"
    assert openalex.rule({**WORK, "type": "book-review"}) == "type: book-review"
    assert openalex.rule({**WORK, "is_retracted": True}) == "retracted"
    assert openalex.rule({**WORK, "language": "de"}) == "language: de"


def test_book_review_signs():
    assert openalex.book_review_signs(WORK) == []
    priced = {**WORK, "title": "The Algorithm. Cambridge: MIT Press, 2021. 320 pp. $35.00"}
    assert "title" in openalex.book_review_signs(priced)
    short = {**WORK, "abstract_inverted_index": None,
             "biblio": {"first_page": "5", "last_page": "6"}}
    assert openalex.book_review_signs(short) == ["short, no abstract"]


def test_works_pages_until_cursor_runs_out(monkeypatch):
    pages = [{"meta": {"count": 3, "next_cursor": "c2"}, "results": [WORK, WORK]},
             {"meta": {"count": 3, "next_cursor": None}, "results": [WORK]}]
    sent = []

    def fake(session, method, url, params=None, **kw):
        sent.append(dict(params))
        return pages[len(sent) - 1]

    monkeypatch.setattr(openalex, "request_json", fake)
    metas = []
    got = list(openalex.works(None, "S1", "2020-01-01", api_key="k", on_page=metas.append))
    assert len(got) == 3 and len(metas) == 2
    assert sent[0]["cursor"] == "*" and sent[1]["cursor"] == "c2"
    assert sent[0]["filter"] == "primary_location.source.id:S1,from_publication_date:2020-01-01"
    assert sent[0]["api_key"] == "k"
