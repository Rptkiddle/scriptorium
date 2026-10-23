"""PDF routes: host lookup, URL resolvers, response classes, the title
check, pacing, and the fetch loop's choices. No network."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from scriptorium import pdf_routes
from scriptorium.pdf_routes import (
    Pacer,
    classify,
    crossref_pdf_links,
    fetch_pdf,
    host_tree,
    interleave,
    ojs_galley_urls,
    plos_url,
    route_of,
    title_on_first_pages,
)

COLLS = [
    {"key": "2AJ6WQZ6", "name": "Journals", "parent": ""},
    {"key": "PUBSPR", "name": "Springer Nature", "parent": "2AJ6WQZ6"},
    {"key": "VENSPR", "name": "[✓] AI & Society", "parent": "PUBSPR"},
    {"key": "VENOLD", "name": "[✗] Dropped Journal", "parent": "PUBSPR"},
    {"key": "PUBIEE", "name": "IEEE", "parent": "2AJ6WQZ6"},
    {"key": "VENACC", "name": "[✓] IEEE Access", "parent": "PUBIEE"},
    {"key": "VENTCS", "name": "[✓] IEEE Transactions on Computational Social Systems",
     "parent": "PUBIEE"},
    {"key": "PUBNEW", "name": "Some New Press", "parent": "2AJ6WQZ6"},
    {"key": "PUBNIP", "name": "NeurIPS Foundation", "parent": "GZ4CVXPR"},
    {"key": "VENNIP", "name": "[✓] Advances in Neural Information Processing Systems",
     "parent": "PUBNIP"},
    {"key": "VENNEW", "name": "[✓] New Journal", "parent": "PUBNEW"},
]


def rec(collections, **kw):
    base = {"item_key": "K1", "doi": "10.1/x", "title": "A Study of Things", "tags": [],
            "pdf_status": "no_pdf", "collections": collections, "item_type": "journalArticle"}
    return SimpleNamespace(**(base | kw))


def make_pdf(*page_texts):
    doc = pymupdf.open()
    for text in page_texts:
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_host_tree_keeps_subscribed_venues_under_a_category():
    tree = host_tree(COLLS)
    assert tree["VENSPR"] == ("Springer Nature", "AI & Society")
    assert "VENOLD" not in tree and "PUBSPR" not in tree


def test_route_of_uses_venue_overrides_and_flags_unknown_publishers():
    tree = host_tree(COLLS)
    assert route_of(rec(["VENSPR"]), tree)[2].group == "script"
    assert route_of(rec(["VENACC"]), tree)[2] == pdf_routes.Route("script", "openalex")
    assert route_of(rec(["VENTCS"]), tree)[2].group == "account"
    assert route_of(rec(["VENNEW"]), tree)[2].group == "unrouted"
    assert route_of(rec([]), tree)[0] == ""


def test_an_acm_doi_takes_acms_route_wherever_it_is_filed():
    tree = host_tree(COLLS)
    publisher, _, route = route_of(rec(["VENSPR"], doi="10.1145/3600211.3604700"), tree)
    assert (publisher, route.group) == ("ACM (by DOI prefix)", "blocked")
    assert route_of(rec(["VENSPR"], doi="10.1609/aies.v7i1.31600"), tree)[0] == "Springer Nature"


def test_crossref_links_rank_text_mining_first_and_drop_html():
    message = {"link": [
        {"URL": "https://x/sim.pdf", "content-type": "unspecified",
         "intended-application": "similarity-checking"},
        {"URL": "https://x/page", "content-type": "text/html", "intended-application": "text-mining"},
        {"URL": "https://x/tdm.pdf", "content-type": "application/pdf",
         "intended-application": "text-mining"},
        {"URL": "https://x/sim.pdf", "content-type": "application/pdf",
         "intended-application": "syndication"},
    ]}
    assert crossref_pdf_links(message) == ["https://x/tdm.pdf", "https://x/sim.pdf"]


def test_plos_url_follows_the_journal_code():
    assert plos_url("10.1371/journal.pcsy.0000005").startswith(
        "https://journals.plos.org/complexsystems/article/file?id=10.1371/journal.pcsy.0000005")
    assert "/plosone/" in plos_url("10.1371/journal.pone.0284495")


def test_mit_press_uses_the_address_that_serves_scripts():
    record = SimpleNamespace(doi="10.1162/TACL_a_00675")
    assert pdf_routes.candidate_urls(None, record, "mitpress") == [
        "https://www.mitpressjournals.org/doi/pdf/10.1162/tacl_a_00675"]


def test_pmc_takes_the_latest_version_with_a_pdf():
    listing = ("<Key>PMC7.1/PMC7.1.pdf</Key><Key>PMC7.2/PMC7.2.xml</Key>"
               "<Key>PMC7.3/PMC7.3.pdf</Key><Key>PMC7.3/x.sapp.pdf</Key>")

    class S:
        def get(self, url, params=None, timeout=None):
            return SimpleNamespace(text=listing, raise_for_status=lambda: None)

    assert pdf_routes.pmc_pdf_url(S(), "PMC7") == f"{pdf_routes.PMC_BUCKET}/PMC7.3/PMC7.3.pdf"


def test_ojs_galley_links_become_downloads():
    html = ('<a href="https://j.org/mc/article/view/2741/1501">PDF</a>'
            '<a href="/mc/article/download/2741/1502">EPUB</a>'
            '<a href="https://j.org/mc/article/view/2741/1501">again</a>')
    assert ojs_galley_urls(html, "https://j.org/mc/article/view/2741") == [
        "https://j.org/mc/article/download/2741/1501", "https://j.org/mc/article/download/2741/1502"]


@pytest.mark.parametrize("status,body,expected", [
    (200, b"%PDF-1.7 ...", "pdf"),
    (403, b"<html>", "forbidden"),
    (403, b"<title>Just a moment...</title>", "blocked"),
    (429, b"", "blocked"),
    (200, b"<title>Just a moment...</title>", "blocked"),
    (200, b"<html><title>Article</title>", "html"),
    (404, b"", "not_found"),
    (500, b"", "http_500"),
])
def test_classify(status, body, expected):
    assert classify(status, body) == expected


def test_title_check_reads_the_first_two_pages():
    assert title_on_first_pages(make_pdf("Cover sheet", "A Study of Things"), "A Study of Things!")
    assert not title_on_first_pages(make_pdf("Something Else"), "A Study of Things")
    assert not title_on_first_pages(make_pdf("x", "y", "A Study of Things"), "A Study of Things")


def test_pacer_waits_per_web_host():
    now, slept = [0.0], []
    pacer = Pacer(clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    pacer.wait("https://a.org/1", 1.0)
    pacer.wait("https://b.org/1", 1.0)
    now[0] += 0.25
    pacer.wait("https://a.org/2", 1.0)
    assert slept == [0.75]


class FakeSession:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, url, timeout=None, allow_redirects=True):
        self.asked.append(url)
        status, body = self.answers[url]
        return SimpleNamespace(status_code=status, content=body)


def fetch(answers, urls, title="A Study of Things"):
    session = FakeSession(answers)
    pacer = Pacer(clock=lambda: 0.0, sleep=lambda s: None)
    return fetch_pdf(session, urls, title, pacer, 0), session.asked


def test_fetch_takes_the_first_checked_pdf(monkeypatch):
    monkeypatch.setattr(pdf_routes, "MIN_PDF_BYTES", 0)
    good = make_pdf("A Study of Things")
    (outcome, url, pdf), asked = fetch({"u1": (200, b"<html>"), "u2": (200, good), "u3": (200, good)},
                                       ["u1", "u2", "u3"])
    assert (outcome, url, pdf) == ("ok", "u2", good) and asked == ["u1", "u2"]


def test_fetch_stops_at_a_block(monkeypatch):
    (outcome, _, pdf), asked = fetch({"u1": (403, b"Access Denied"), "u2": (200, b"%PDF-")},
                                     ["u1", "u2"])
    assert outcome == "blocked" and asked == ["u1"] and pdf is None


def test_fetch_keeps_a_title_mismatch_for_review_after_later_failures(monkeypatch):
    monkeypatch.setattr(pdf_routes, "MIN_PDF_BYTES", 0)
    other = make_pdf("A Different Paper")
    (outcome, url, pdf), _ = fetch({"u1": (200, other), "u2": (200, b"<html>")}, ["u1", "u2"])
    assert (outcome, url, pdf) == ("title_mismatch", "u1", other)


def test_fetch_moves_past_a_forbidden_candidate(monkeypatch):
    monkeypatch.setattr(pdf_routes, "MIN_PDF_BYTES", 0)
    good = make_pdf("A Study of Things")
    (outcome, url, _), asked = fetch({"u1": (403, b"<html>"), "u2": (200, good)}, ["u1", "u2"])
    assert (outcome, url) == ("ok", "u2") and asked == ["u1", "u2"]
    assert fetch({"u1": (403, b"<html>")}, ["u1"])[0][0] == "forbidden"


def test_title_check_accepts_a_title_after_a_section_label():
    pdf = make_pdf("Scaling Down to Scale Up? When Less Moderation Claims")
    assert title_on_first_pages(pdf, "Sociotechnical Scale| Scaling Down to Scale Up? When Less Moderation")
    assert not title_on_first_pages(pdf, "Sociotechnical Scale| A Different Paper Entirely")


def test_title_check_ignores_accents():
    # The PDF stores e + combining accent; the title uses the composed é.
    assert title_on_first_pages(make_pdf("Re\u0301sume\u0301 of Things"), "R\u00e9sum\u00e9 of Things")


def test_fetch_rejects_tiny_files_and_reports_no_url():
    (outcome, _, pdf), _ = fetch({"u1": (200, b"%PDF-1.4 tiny")}, ["u1"])
    assert outcome == "too_small" and pdf is None
    assert fetch({}, [])[0][0] == "no_url"


def test_interleave_alternates_hosts():
    assert interleave({"b": [1, 2, 3], "a": ["x"]}) == ["x", 1, 2, 3]
    assert interleave({"a": ["x", "y"], "b": [1, 2]}) == ["x", 1, "y", 2]


def _script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_select_targets_leaves_out_what_the_run_must_not_touch():
    fetch_pdfs = _script("fetch_pdfs")
    tree = host_tree(COLLS)
    records = [rec(["VENSPR"], item_key="A"),
               rec(["VENSPR"], item_key="B", pdf_status="ok"),
               rec(["VENSPR"], item_key="C", tags=["own-work"]),
               rec(["VENSPR"], item_key="D", doi=""),
               rec(["VENTCS"], item_key="E"),
               rec(["VENSPR"], item_key="F", pdf_status="linked_missing")]
    targets, _, left_out, _ = fetch_pdfs.select_targets(records, tree)
    assert [r.item_key for r, _, _ in targets["Springer Nature"]] == ["A"]
    assert left_out == {"has a PDF attachment": 1, "own-work": 1, "no DOI: Springer Nature": 1,
                        "group account: IEEE": 1, "attachment file missing (linked_missing)": 1}


def test_pilot_spreads_over_publishers():
    fetch_pdfs = _script("fetch_pdfs")
    targets = {"P1": list(range(100)), "P2": list(range(100, 110)), "P3": [999]}
    picked = fetch_pdfs.pilot(targets, 7)
    assert sum(len(v) for v in picked.values()) == 7
    assert all(picked[p] for p in targets)


def test_proceedings_resolvers_build_pdf_addresses():
    r = SimpleNamespace(doi="10.18653/v1/2020.acl-main.50")
    assert pdf_routes.candidate_urls(None, r, "acl") == ["https://aclanthology.org/2020.acl-main.50.pdf"]
    assert pdf_routes.candidate_urls(None, SimpleNamespace(doi=""), "acl",
                                     "https://aclanthology.org/2023.emnlp-main.814/") == [
        "https://aclanthology.org/2023.emnlp-main.814.pdf"]
    assert pdf_routes.candidate_urls(None, SimpleNamespace(doi="10.24963/ijcai.2020/672"), "ijcai") == [
        "https://www.ijcai.org/proceedings/2020/0672.pdf"]
    assert pdf_routes.candidate_urls(None, SimpleNamespace(doi="10.36190/2020.16"), "icwsm_workshops") == [
        "https://workshop-proceedings.icwsm.org/pdf/2020_16.pdf"]
    page = "https://proceedings.neurips.cc/paper_files/paper/2024/hash/{}-Abstract{}.html"
    pdf = "https://proceedings.neurips.cc/paper_files/paper/2024/file/{}-Paper{}.pdf"
    h = "0534abc9e6db91683d82186ef0d68202"
    for track in ("", "-Conference", "-Datasets_and_Benchmarks_Track"):
        assert pdf_routes.neurips_url(page.format(h, track)) == pdf.format(h, track)
    assert pdf_routes.pmlr_urls("https://proceedings.mlr.press/v306/li26cw.html") == [
        "https://proceedings.mlr.press/v306/li26cw/li26cw.pdf",
        "https://raw.githubusercontent.com/mlresearch/v306/main/assets/li26cw/li26cw.pdf"]
    assert pdf_routes.candidate_urls(None, SimpleNamespace(doi=""), "pmlr", "https://neurips.cc") == []
    assert pdf_routes.ifaamas_url({"issued": {"date-parts": [[2020, 5, 5]]}, "page": "1706-1710"}) == (
        "https://www.ifaamas.org/Proceedings/aamas2020/pdfs/p1706.pdf")


def test_select_targets_tiers_untried_and_missed_papers():
    fetch_pdfs = _script("fetch_pdfs")
    tree = host_tree(COLLS)
    records = [rec(["VENSPR"], item_key="P", item_type="preprint"),
               rec(["VENSPR"], item_key="S"),
               rec(["VENSPR"], item_key="M"),
               rec(["VENSPR"], item_key="A", pdf_status="ok"),
               rec(["VENNIP"], item_key="U", doi=""),
               rec(["VENNIP"], item_key="N", doi="")]
    tried = {"S": ("staged", "2026-10-15-1"), "M": ("html", "2026-10-15-1"),
             "A": ("staged", "2026-10-15-1")}
    targets, retries, left_out, reasons = fetch_pdfs.select_targets(
        records, tree, urls={"U": "https://proceedings.neurips.cc/x"}, tried=tried)
    assert [r.item_key for r, _, _ in targets["NeurIPS Foundation"]] == ["U"]
    assert [r.item_key for r, _, _ in retries["Springer Nature"]] == ["M"]
    assert left_out == {"preprint (preprint PDFs are not fetched)": 1, "PDF staged in an earlier run": 1,
                        "has a PDF attachment": 1, "no DOI: NeurIPS Foundation": 1}
    assert reasons["U"] == "to fetch"
    assert reasons["S"] == "PDF staged in run 2026-10-15-1, not yet attached"
    assert reasons["M"] == "tried before: html (run 2026-10-15-1); held back, use --retry"
    _, _, _, reasons = fetch_pdfs.select_targets(records, tree, tried=tried, retry=True)
    assert reasons["M"].endswith("retried in this run")


def test_earlier_outcomes_reads_every_other_run(tmp_path):
    fetch_pdfs = _script("fetch_pdfs")

    def run(name, entries, files=()):
        d = tmp_path / name
        (d / "pdfs").mkdir(parents=True)
        (d / "pdfs-review").mkdir()
        for f in files:
            (d / f).write_bytes(b"%PDF-")
        (d / "fetch.jsonl").write_text("".join(
            json.dumps({"key": k, "outcome": o}) + "\n" for k, o in entries))
        return d

    run("2026-10-15-1", [("A", "ok"), ("B", "ok"), ("C", "no_url"), ("D", "title_mismatch")],
        files=["pdfs/A.pdf", "pdfs-review/D.pdf"])
    run("2026-10-16-1", [("C", "host_stopped"), ("E", "html")])
    current = run("2026-10-17-1", [("F", "blocked")])
    found = fetch_pdfs.earlier_outcomes(tmp_path, current)
    # B's staged file was deleted; C's later host_stopped never requested it.
    assert found == {"A": ("staged", "2026-10-15-1"), "C": ("no_url", "2026-10-15-1"),
                     "D": ("staged", "2026-10-15-1"), "E": ("html", "2026-10-16-1")}


def test_named_keys_take_an_added_json(tmp_path):
    fetch_pdfs = _script("fetch_pdfs")
    added = tmp_path / "added.json"
    added.write_text(json.dumps({"created": {"10.1/a": {"key": "K2", "citation_key": "x"},
                                             "arxiv:1": {"key": "K3", "citation_key": "y"}}}))
    assert fetch_pdfs.named_keys(["K1", "K2"], str(added)) == ["K1", "K2", "K3"]


def test_ijcai_keeps_its_crawl_delay():
    assert pdf_routes.ROUTES["IJCAI Organization"] == pdf_routes.Route("script", "ijcai", 10)


def test_nature_family_articles_fall_back_to_nature_com(monkeypatch):
    monkeypatch.setattr(pdf_routes, "request_json", lambda *a, **k: {})
    for doi, article in (("10.1038/s41598-026-12345-6", "s41598-026-12345-6"),
                         ("10.1057/s41599-026-07316-7", "s41599-026-07316-7")):
        assert pdf_routes.candidate_urls(None, SimpleNamespace(doi=doi), "crossref") == [
            f"https://www.nature.com/articles/{article}.pdf"]
    assert pdf_routes.candidate_urls(None, SimpleNamespace(doi="10.1057/palgrave.x.1"), "crossref") == []


def test_behavioral_and_brain_sciences_is_downloaded_by_hand():
    assert pdf_routes.VENUE_ROUTES["Behavioral and Brain Sciences"].group == "browser"


def test_ojs_pages_keep_only_their_own_articles_galleys():
    html = ('<a href="https://ijoc.org/index.php/ijoc/article/view/20420/4419">PDF</a>'
            '<a href="https://ijoc.org/index.php/ijoc/article/view/28982/5564">Another article</a>')
    assert ojs_galley_urls(html, "https://ijoc.org/index.php/ijoc/article/view/20420") == [
        "https://ijoc.org/index.php/ijoc/article/download/20420/4419"]


class FakePage:
    def __init__(self, url, text):
        self.url, self.text, self.ok = url, text, True


class OjsSession:
    def __init__(self):
        self.asked = []

    def get(self, url, timeout=None):
        self.asked.append(url)
        page = "https://ijoc.org/index.php/ijoc/article/view/20420"
        return FakePage(page, '<a href="/index.php/ijoc/article/view/20420/4419">PDF</a>')


def test_the_ojs_route_reads_the_items_own_page_when_it_has_no_doi():
    session = OjsSession()
    item_page = "https://ijoc.org/index.php/ijoc/article/view/20420"
    assert pdf_routes.candidate_urls(session, SimpleNamespace(doi=""), "ojs_page", item_page) == [
        "https://ijoc.org/index.php/ijoc/article/download/20420/4419"]
    assert session.asked == [item_page]
    pdf_routes.candidate_urls(session, SimpleNamespace(doi="10.1/x"), "ojs_page", item_page)
    assert session.asked[-1] == "https://doi.org/10.1/x"
    assert pdf_routes.candidate_urls(OjsSession(), SimpleNamespace(doi=""), "ojs_page", "") == []


def test_internet_policy_review_pdf_address_and_hdsr_block(monkeypatch):
    assert pdf_routes.policyreview_urls("10.14763/2024.2.1754") == [
        "https://policyreview.info/pdf/policyreview-2024-2-1754.pdf"]
    monkeypatch.setattr(pdf_routes, "request_json", lambda *a, **k: {"message": {"link": [
        {"URL": "https://www.article19.org/report.pdf", "content-type": "application/pdf", "intended-application": "text-mining"},
        {"URL": "https://policyreview.info/articles/x.pdf", "content-type": "application/pdf", "intended-application": "text-mining"}]}})
    assert pdf_routes.candidate_urls(None, SimpleNamespace(doi="10.14763/2024.2.1754"), "policyreview") == [
        "https://policyreview.info/pdf/policyreview-2024-2-1754.pdf",
        "https://policyreview.info/articles/x.pdf"]
    assert pdf_routes.VENUE_ROUTES["Harvard Data Science Review"].group == "blocked"
