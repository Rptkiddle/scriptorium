"""Abstract fill: parsers, source order, keys, unavailable sources."""

import requests

from scriptorium import abstracts, apis
from scriptorium.abstracts import fill_abstracts, summary_line

KEYS = {"elsevier_api_key": "ELS-KEY", "elsevier_insttoken": "", "springer_api_key": "SN-KEY"}


def _cands(*dois):
    return [{"doi": d, "abstract": ""} for d in dois]


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} for url https://x/?api_key=SN-KEY", response=response)


class FakeAPIs:
    """Answers per source from {doi: abstract} tables; records what was asked."""

    def __init__(self, openalex=None, scopus=None, springer=None, fail=None):
        self.tables = {"openalex": openalex or {}, "scopus": scopus or {},
                       "springer": springer or {}}
        self.fail = fail or {}
        self.asked = {"openalex": [], "scopus": [], "springer": []}

    def __call__(self, session, method, url, params=None, headers=None, **kw):
        if url.startswith(apis.OPENALEX):
            source = "openalex"
            dois = [d.removeprefix("https://doi.org/")
                    for d in params["filter"].removeprefix("doi:").split("|")]
        elif url == abstracts.SCOPUS_SEARCH:
            source = "scopus"
            dois = [d[5:-2] for d in params["query"].split(" OR ")]
            assert headers["X-ELS-APIKey"] == "ELS-KEY"
        else:
            source = "springer"
            dois = [d.removeprefix("doi:") for d in params["q"].split(" OR ")]
            assert params["api_key"] == "SN-KEY"
        self.asked[source] += dois
        if source in self.fail:
            raise self.fail[source]
        table = self.tables[source]
        hits = [d for d in dois if d in table]
        if source == "openalex":
            return {"results": [{"doi": f"https://doi.org/{d}", "abstract_inverted_index":
                                 {w: [i] for i, w in enumerate(table[d].split())}}
                                for d in hits]}
        if source == "scopus":
            return {"search-results": {"entry": [
                {"prism:doi": d, "dc:description": table[d]} for d in hits]
                or [{"error": "Result set was empty"}]}}
        return {"records": [{"doi": d, "abstract": table[d]} for d in hits]}


def test_parsers():
    assert abstracts.openalex_abstract({"b": [1], "a": [0], "c": [2]}) == "a b c"
    assert abstracts.scopus_abstract(
        {"dc:description": "Abstract: Text here. © 2023 Elsevier Ltd. All rights reserved."}
    ) == "Text here."
    assert abstracts.springer_abstract({"abstract": {
        "h1": "Abstract", "p": ["One.", {"h2": "Methods", "p": "Two."}]}}) == "One. Methods Two."
    assert abstracts.springer_abstract({"abstract": "<p>Plain &amp; simple</p>"}) == "Plain & simple"


def test_sources_in_turn_each_sent_only_what_is_still_missing(monkeypatch):
    fake = FakeAPIs(openalex={"10.1016/a": "from openalex"},
                    scopus={"10.1016/b": "from scopus", "10.1007/c": "scopus too"},
                    springer={"10.1007/d": "from springer"})
    monkeypatch.setattr(abstracts, "request_json", fake)
    cands = _cands("10.1016/A", "10.1016/b", "10.1007/c", "10.1007/d", "10.9999/e")
    summary = fill_abstracts(cands, KEYS, session=object())
    assert [c["abstract"] for c in cands] == [
        "from openalex", "from scopus", "scopus too", "from springer", ""]
    assert [c.get("abstract_source") for c in cands] == [
        "openalex", "scopus", "scopus", "springer", None]
    assert fake.asked["scopus"] == ["10.1016/b", "10.1007/c", "10.1007/d", "10.9999/e"]
    # Springer is asked only for Springer Nature DOIs still missing.
    assert fake.asked["springer"] == ["10.1007/d"]
    assert summary == {"missing": 5, "openalex": 1, "scopus": 2, "springer": 1,
                       "unavailable": {}, "title_only": 1}


def test_papers_with_an_abstract_are_not_looked_up(monkeypatch):
    fake = FakeAPIs()
    monkeypatch.setattr(abstracts, "request_json", fake)
    summary = fill_abstracts([{"doi": "10.1/x", "abstract": "has one"}], KEYS, session=object())
    assert fake.asked == {"openalex": [], "scopus": [], "springer": []}
    assert summary["missing"] == 0


def test_source_without_key_is_skipped(monkeypatch):
    fake = FakeAPIs()
    monkeypatch.setattr(abstracts, "request_json", fake)
    summary = fill_abstracts(_cands("10.1007/a"), {"elsevier_api_key": "",
                                                   "springer_api_key": ""}, session=object())
    assert fake.asked["scopus"] == [] and fake.asked["springer"] == []
    assert summary["unavailable"] == {"scopus": "no key", "springer": "no key"}
    assert summary["title_only"] == 1


def test_refused_source_is_recorded_without_the_key_and_others_still_run(monkeypatch):
    fake = FakeAPIs(springer={"10.1007/a": "from springer"},
                    fail={"scopus": _http_error(401)})
    monkeypatch.setattr(abstracts, "request_json", fake)
    cands = _cands("10.1007/a")
    summary = fill_abstracts(cands, KEYS, session=object())
    assert cands[0]["abstract"] == "from springer"
    assert summary["unavailable"] == {"scopus": "HTTP 401"}
    fake = FakeAPIs(fail={"springer": _http_error(403)})
    monkeypatch.setattr(abstracts, "request_json", fake)
    line = summary_line(fill_abstracts(_cands("10.1007/b"), KEYS, session=object()))
    assert "springer unavailable (HTTP 403)" in line and "SN-KEY" not in line


def test_abstracts_found_before_a_failure_are_kept(monkeypatch):
    calls = []

    def flaky(session, method, url, params=None, **kw):
        calls.append(1)
        if len(calls) > 1:
            raise requests.ConnectionError("down")
        return {"results": [{"doi": "https://doi.org/10.1/0",
                             "abstract_inverted_index": {"kept": [0]}}]}

    monkeypatch.setattr(abstracts, "request_json", flaky)
    cands = _cands(*[f"10.1/{i}" for i in range(60)])  # two OpenAlex batches of 50
    summary = fill_abstracts(cands, {}, session=object())
    assert cands[0]["abstract"] == "kept"
    assert summary["openalex"] == 1
    assert summary["unavailable"]["openalex"] == "ConnectionError"


def test_abstracts_are_cut_to_the_harvest_length(monkeypatch):
    long = " ".join(f"word{i}" for i in range(1000))
    monkeypatch.setattr(abstracts, "request_json", FakeAPIs(openalex={"10.1/a": long}))
    cands = _cands("10.1/a")
    fill_abstracts(cands, {}, session=object())
    assert len(cands[0]["abstract"]) == abstracts.MAX_CHARS


def test_keyed_requests_do_not_follow_redirects():
    seen = []

    class Session:
        def request(self, method, url, **kw):
            seen.append(kw["allow_redirects"])
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

    apis.request_json(Session(), "GET", "https://api.openalex.org/works")
    apis.request_json(Session(), "GET", abstracts.SCOPUS_SEARCH, headers={"X-ELS-APIKey": "k"})
    apis.request_json(Session(), "GET", abstracts.SPRINGER_META, params={"api_key": "k"})
    assert seen == [True, False, False]
