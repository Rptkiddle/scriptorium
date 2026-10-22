"""PDF routes: how the version of record of a library paper is fetched.

A paper's host is the publisher folder its venue folder sits under in
Zotero (<category> > <publisher> > [✓] <venue>). Each host has a route
group and a resolver that turns a paper into candidate PDF URLs, in order
of preference. Groups:

  script    fetched by script (from a network with the institution's subscriptions)
  account   fetched by script once an account or key exists (not yet)
  blocked   the host refuses scripts; left to a publisher request
  browser   downloaded by hand
  skip      no PDF to fetch (the articles are HTML)

The table follows routes.md in proj/1_SELF/scriptorium/retrieval-routes-2026-10/,
which holds the evidence for each route. Requests identify themselves
honestly and are paced per web host; a host that blocks is never worked
around.
"""

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from scriptorium.apis import CROSSREF, MAILTO, OPENALEX, request_json

USER_AGENT = f"scriptorium/0.1 (VU Amsterdam research library tool; mailto:{MAILTO})"
PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
CATEGORY_KEYS = {"2AJ6WQZ6": "Journals", "GZ4CVXPR": "Proceedings", "3ISRFHE2": "Preprints"}
SUBSCRIBED = "[✓] "
MIN_PDF_BYTES = 20_000
BLOCK_BREAKER = 5  # consecutive blocked answers that stop a host for the run


@dataclass(frozen=True)
class Route:
    group: str
    resolver: str = ""
    delay: float = 1.0  # minimum seconds between requests to one web host


SCRIPT = {
    "Springer Nature": "crossref", "Cambridge University Press": "crossref",
    "AAAI": "crossref", "PLOS": "plos", "MIT Press": "mitpress",
    "Journal of Artificial Societies and Social Simulation": "openalex",
    "Alexander von Humboldt Institute for Internet and Society": "openalex",
    "Shorenstein Center (Harvard Kennedy School)": "crossref",
    "Stanford Internet Observatory": "crossref",
    "Journal of Quantitative Description: Digital Media": "crossref",
    "DIGSUM (Umeå University)": "crossref", "AI Access Foundation": "crossref",
    "Society for Sociological Science": "crossref", "The Open Journal": "crossref",
    "University of Westminster Press": "crossref", "De Gruyter": "crossref",
    "USC Annenberg Press": "ojs_page", "Frontiers": "frontiers",
    "UK Zhende Publishing": "crossref", "Cogitatio": "ojs_page",
    "National Academy of Sciences": "pmc",
    # Proceedings hosts.
    "ACL Anthology": "acl", "IJCAI Organization": "ijcai", "IFAAMAS": "ifaamas",
    "NeurIPS Foundation": "neurips", "PMLR": "pmlr",
}
ROUTES = {name: Route("script", resolver) for name, resolver in SCRIPT.items()}
ROUTES |= {name: Route("account") for name in ("Elsevier", "Wiley", "IEEE")}
ROUTES |= {name: Route("blocked") for name in (
    "SAGE", "Taylor & Francis", "Oxford University Press", "ACM", "AAAS", "MDPI",
    "Amsterdam University Press", "American Physical Society", "Annual Reviews",
    "Penn State University Press", "Communication and Social Robotics Labs",
    "OpenReview")}
ROUTES |= {name: Route("browser") for name in (
    "University of Chicago Press", "INFORMS", "MIS Quarterly",
    "Johns Hopkins University Press",
    "IOS Press")}  # the e-book pages show scripts no PDF link
ROUTES["University of Illinois Libraries"] = Route("skip")  # First Monday: HTML articles
ROUTES["IJCAI Organization"] = Route("script", "ijcai", delay=10)  # robots.txt Crawl-delay: 10

# A venue whose route differs from its publisher's.
VENUE_ROUTES = {
    "IEEE Access": Route("script", "openalex"),
    # Crossref does not hold these DOIs; the workshop site serves the PDFs.
    "Workshop Proceedings of the International AAAI Conference on Web and Social Media"
    " (ICWSM Workshops)": Route("script", "icwsm_workshops"),
    # The ALIFE proceedings are on direct.mit.edu only, which refuses scripts.
    "Conference on Artificial Life (ALIFE)": Route("blocked"),
    # Every BBS DOI (target article and each commentary) serves the whole
    # published bundle, 53-109 pages; papers are downloaded by hand instead.
    "Behavioral and Brain Sciences": Route("browser"),
    # HDSR (hdsr.mitpress.mit.edu) answers scripts with a Cloudflare check.
    "Harvard Data Science Review": Route("blocked"),
    # Venue-specific: a PDF address built from the DOI (see policyreview_urls).
    "Internet Policy Review": Route("script", "policyreview"),
}
# A DOI prefix whose registrant serves the PDF wherever the paper is filed:
# such a paper takes that publisher's route (AIES, under AAAI, holds AAAI's
# own DOIs and ACM's 10.1145 ones).
PREFIX_ROUTES = {"10.1145": "ACM"}
# Resolvers that work from an item's URL, so items without a DOI can be fetched.
URL_RESOLVERS = {"acl", "neurips", "ojs_page", "pmlr"}

PLOS_JOURNALS = {"pone": "plosone", "pcsy": "complexsystems", "pcbi": "ploscompbiol",
                 "pbio": "plosbiology", "pmed": "plosmedicine", "pgen": "plosgenetics"}


def host_tree(collections: list[dict]) -> dict:
    """Venue collection key -> (publisher folder, venue name without the
    mark), for subscribed venues under a category folder."""
    by_key = {c["key"]: c for c in collections}
    tree = {}
    for c in collections:
        parent = by_key.get(c["parent"])
        if (c["name"].startswith(SUBSCRIBED) and parent
                and parent["parent"] in CATEGORY_KEYS):
            tree[c["key"]] = (parent["name"], c["name"].removeprefix(SUBSCRIBED))
    return tree


def route_of(record, tree: dict) -> tuple[str, str, Route]:
    """(publisher, venue, route) for a paper; the route group is
    "unrouted" when its venue or publisher has no entry. A DOI whose prefix
    is in PREFIX_ROUTES takes that publisher's route, reported as
    "<publisher> (by DOI prefix)"."""
    for key in record.collections:
        if key in tree:
            publisher, venue = tree[key]
            if (owner := PREFIX_ROUTES.get(record.doi.split("/")[0])) and owner != publisher:
                return f"{owner} (by DOI prefix)", venue, ROUTES[owner]
            route = VENUE_ROUTES.get(venue) or ROUTES.get(publisher) or Route("unrouted")
            return publisher, venue, route
    return "", "", Route("unrouted")


# ---------------------------------------------------------------- resolvers

def crossref_pdf_links(message: dict) -> list[str]:
    """PDF links from a Crossref work record, best first: text-mining,
    then syndication, then similarity-checking. HTML links are left out."""
    ranked = []
    for rank, purpose in enumerate(("text-mining", "syndication", "similarity-checking")):
        for link in message.get("link", []):
            if (link.get("intended-application") == purpose
                    and link.get("content-type") in ("application/pdf", "unspecified")):
                ranked.append((rank, link["URL"]))
    return list(dict.fromkeys(url for _, url in sorted(ranked, key=lambda r: r[0])))


def openalex_locations(session: requests.Session, doi: str) -> list[dict]:
    work = request_json(session, "GET", f"{OPENALEX}/works/doi:{doi}",
                        params={"mailto": MAILTO, "select": "locations"}, not_found={})
    return work.get("locations") or []


def plos_url(doi: str) -> str:
    code = re.search(r"journal\.(\w+)\.", doi)
    journal = PLOS_JOURNALS.get(code.group(1) if code else "", "plosone")
    return f"https://journals.plos.org/{journal}/article/file?id={doi}&type=printable"


def pmc_pdf_url(session: requests.Session, pmcid: str) -> str | None:
    """The PDF of the latest version of a PMC article in the PMC Cloud
    Service bucket, or None when the bucket holds no PDF for it."""
    resp = session.get(PMC_BUCKET, params={"list-type": "2", "prefix": f"{pmcid}."}, timeout=60)
    resp.raise_for_status()
    pdfs = re.findall(rf"<Key>({pmcid}\.(\d+)/{pmcid}\.\2\.pdf)</Key>", resp.text)
    if not pdfs:
        return None
    key, _ = max(pdfs, key=lambda p: int(p[1]))
    return f"{PMC_BUCKET}/{key}"


def ojs_galley_urls(html: str, base_url: str) -> list[str]:
    """Candidate PDF downloads on an OJS article page: the galley links
    (article/view/<id>/<galley> or article/download/<id>/<galley>) turned
    into their download form. When the page's own address names its article
    (article/view/<id>), only that article's galleys are kept: OJS pages
    also link other articles' files."""
    root = "{0.scheme}://{0.netloc}".format(urlsplit(base_url))
    page = re.search(r"/article/view/(\d+)", base_url)
    found = re.findall(r'href="([^"]*/article/(?:view|download)/(\d+)/\d+)"', html)
    urls = [(u if u.startswith("http") else root + u).replace("/article/view/", "/article/download/")
            for u, article in found if page is None or article == page.group(1)]
    return list(dict.fromkeys(urls))


def policyreview_urls(doi: str) -> list[str]:
    """Internet Policy Review: DOI 10.14763/<year>.<issue>.<id> names the PDF
    policyreview.info/pdf/policyreview-<year>-<issue>-<id>.pdf (every PDF
    fetched from the site on 2026-10-15 had this address). Venue-specific;
    the fallback is the journal's own links in Crossref only."""
    m = re.match(r"10\.14763/(\d{4})\.(\d+)\.(\d+)$", doi)
    return [f"https://policyreview.info/pdf/policyreview-{m[1]}-{m[2]}-{m[3]}.pdf"] if m else []


def acl_url(doi: str, url: str) -> str | None:
    """ACL Anthology: the DOI 10.18653/v1/<anthology id> or the page
    aclanthology.org/<anthology id>/ names the PDF <anthology id>.pdf."""
    m = (re.match(r"10\.18653/v1/(.+)$", doi)
         or re.match(r"https?://aclanthology\.org/([^/]+?)/?$", url))
    return f"https://aclanthology.org/{m.group(1)}.pdf" if m else None


def ijcai_url(doi: str) -> str | None:
    m = re.match(r"10\.24963/ijcai\.(\d{4})/(\d+)$", doi)
    return f"https://www.ijcai.org/proceedings/{m.group(1)}/{int(m.group(2)):04d}.pdf" if m else None


def icwsm_workshops_url(doi: str) -> str | None:
    m = re.match(r"10\.36190/(\d{4})\.(\d+)$", doi)
    return f"https://workshop-proceedings.icwsm.org/pdf/{m.group(1)}_{m.group(2)}.pdf" if m else None


def neurips_url(url: str) -> str | None:
    """The abstract page .../hash/<hash>-Abstract[-<track>].html names the PDF
    .../file/<hash>-Paper[-<track>].pdf."""
    m = re.match(r"(https://proceedings\.neurips\.cc/paper_files/paper/\d{4})/hash/"
                 r"([0-9a-f]{32})-Abstract(.*)\.html$", url)
    return f"{m.group(1)}/file/{m.group(2)}-Paper{m.group(3)}.pdf" if m else None


def pmlr_urls(url: str) -> list[str]:
    """PMLR serves a paper's PDF next to its page, or, for newer volumes,
    from the volume's GitHub repository; both are tried."""
    m = re.match(r"https://proceedings\.mlr\.press/(v\d+)/([^/]+)\.html$", url)
    if not m:
        return []
    volume, paper = m.groups()
    return [f"https://proceedings.mlr.press/{volume}/{paper}/{paper}.pdf",
            f"https://raw.githubusercontent.com/mlresearch/{volume}/main/assets/{paper}/{paper}.pdf"]


def ifaamas_url(message: dict) -> str | None:
    """AAMAS papers are filed by year and first page on ifaamas.org; the
    Crossref record gives both."""
    year = ((message.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    first = re.match(r"\d+", message.get("page", ""))
    return (f"https://www.ifaamas.org/Proceedings/aamas{year}/pdfs/p{first.group(0)}.pdf"
            if year and first else None)


def candidate_urls(session: requests.Session, record, resolver: str, url: str = "") -> list[str]:
    """Candidate PDF URLs for a paper, best first (may be empty). `url` is
    the item's URL field, which the URL resolvers read."""
    doi = record.doi.lower()
    by_address = {"acl": lambda: acl_url(doi, url), "ijcai": lambda: ijcai_url(doi),
                  "icwsm_workshops": lambda: icwsm_workshops_url(doi),
                  "neurips": lambda: neurips_url(url)}
    if resolver == "pmlr":
        return pmlr_urls(url)
    if resolver in by_address:
        found = by_address[resolver]()
        return [found] if found else []
    if resolver == "ifaamas":
        message = request_json(session, "GET", f"{CROSSREF}/works/{doi}",
                               params={"mailto": MAILTO}, not_found={}).get("message", {})
        found = ifaamas_url(message)
        return [found] if found else []
    if resolver == "plos":
        return [plos_url(doi)]
    if resolver == "frontiers":
        return [f"https://www.frontiersin.org/articles/{doi}/pdf"]
    if resolver == "mitpress":
        # direct.mit.edu, Crossref's link, refuses scripts; the older
        # address redirects to the same PDF on MIT Press's PDF host.
        return [f"https://www.mitpressjournals.org/doi/pdf/{doi}"]
    if resolver == "pmc":
        for loc in openalex_locations(session, doi):
            match = re.search(r"PMC\d+", loc.get("pdf_url") or loc.get("landing_page_url") or "")
            if match and (loc.get("source") or {}).get("display_name") == "PubMed Central":
                url = pmc_pdf_url(session, match.group(0))
                return [url] if url else []
        return []
    if resolver == "ojs_page":
        # The article page: the DOI's landing page, or the item's own address.
        page = f"https://doi.org/{doi}" if doi else url
        if not page:
            return []
        resp = session.get(page, timeout=60)
        return ojs_galley_urls(resp.text, resp.url) if resp.ok else []
    if resolver == "policyreview":
        message = request_json(session, "GET", f"{CROSSREF}/works/{doi}",
                               params={"mailto": MAILTO}, not_found={}).get("message", {})
        own = [u for u in crossref_pdf_links(message) if urlsplit(u).netloc.endswith("policyreview.info")]
        return list(dict.fromkeys(policyreview_urls(doi) + own))
    urls = []
    if resolver == "openalex":
        urls += [loc["pdf_url"] for loc in openalex_locations(session, doi)
                 if (loc.get("source") or {}).get("type") == "journal" and loc.get("pdf_url")]
    message = request_json(session, "GET", f"{CROSSREF}/works/{doi}",
                           params={"mailto": MAILTO}, not_found={}).get("message", {})
    urls += crossref_pdf_links(message)
    # Nature-family articles (Nature Portfolio and Palgrave's 10.1057/s4...)
    # are served at nature.com/articles/<id>.pdf; Crossref lists no PDF link
    # for the newest ones.
    nature = re.match(r"10\.(?:1038/|1057/(?=s4))(.+)$", doi)
    if nature:
        urls.append(f"https://www.nature.com/articles/{nature.group(1)}.pdf")
    return list(dict.fromkeys(urls))


# ------------------------------------------------------------- fetch + check

def norm_text(text: str) -> str:
    """Letters and digits only, lower case, accents removed."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text).casefold())


def title_on_first_pages(pdf: bytes, title: str, pages: int = 2) -> bool:
    """Whether the paper's title appears on the PDF's first pages (a cover
    sheet may come first). Compares the first 60 letters and digits of the
    title, ignoring case, spacing and punctuation. A title that opens with
    a section label ("Special Section| Title", as in IJoC) also matches on
    the part after the label."""
    import pymupdf

    forms = [title] + ([title.split("|", 1)[1]] if "|" in title else [])
    wants = [w for w in (norm_text(f)[:60] for f in forms) if w]
    if not wants:
        return False
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        text = norm_text("".join(doc[i].get_text() for i in range(min(pages, doc.page_count))))
    return any(w in text for w in wants)


def first_lines(pdf: bytes, chars: int = 160) -> str:
    """The start of a PDF's first page on one line, to judge by eye what
    the file is."""
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        text = doc[0].get_text() if doc.page_count else ""
    return " ".join(text.split())[:chars]


# A fetch run's review decisions on its held PDFs (pdfs-review/): one JSON
# line per decision, {"key", "decision": "accept" | "reject", "at", "by",
# "note"}; the latest line per key holds.
REVIEW_LOG = "review.jsonl"


def review_decisions(run_dir: Path) -> dict[str, dict]:
    path = run_dir / REVIEW_LOG
    decisions: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            decisions[entry["key"]] = entry
    return decisions


BLOCK_MARKERS = (b"Just a moment...", b"Access Denied", b"Attention Required", b"captcha")


def classify(status: int, body: bytes) -> str:
    """One response's outcome: pdf; blocked (a bot check, or 429/503);
    forbidden (a plain 401/403, often content the network has no subscription to);
    html; not_found; or http_<n>."""
    if status == 200 and body[:5] == b"%PDF-":
        return "pdf"
    if status in (429, 503) or any(m in body[:20_000] for m in BLOCK_MARKERS):
        return "blocked"
    if status in (401, 403):
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 200:
        return "html"
    return f"http_{status}"


class Pacer:
    """At most one request per `delay` seconds to each web host."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.last: dict[str, float] = {}
        self.clock, self.sleep = clock, sleep

    def wait(self, url: str, delay: float) -> None:
        host = urlsplit(url).netloc
        if host in self.last:
            remaining = self.last[host] + delay - self.clock()
            if remaining > 0:
                self.sleep(remaining)
        self.last[host] = self.clock()


def fetch_pdf(session: requests.Session, urls: list[str], title: str, pacer: Pacer,
              delay: float) -> tuple[str, str, bytes | None]:
    """Try the candidate URLs in order. Returns (outcome, url, pdf bytes):
    outcome is ok, title_mismatch (a PDF, but not visibly this paper),
    too_small, or the last failure's class; blocked stops at once."""
    outcome, last_url = "no_url", ""
    mismatch = None
    for url in urls:
        pacer.wait(url, delay)
        last_url = url
        try:
            resp = session.get(url, timeout=90, allow_redirects=True)
        except requests.RequestException as exc:
            outcome = f"error_{type(exc).__name__}"
            continue
        kind = classify(resp.status_code, resp.content)
        if kind == "blocked":
            return "blocked", url, None
        if kind != "pdf":
            outcome = kind
            continue
        if len(resp.content) < MIN_PDF_BYTES:
            outcome = "too_small"
            continue
        try:
            matches = title_on_first_pages(resp.content, title)
        except Exception:  # noqa: BLE001 — an unreadable PDF is a miss, not a crash
            outcome = "invalid_pdf"
            continue
        if matches:
            return "ok", url, resp.content
        mismatch = mismatch or (url, resp.content)
        outcome = "title_mismatch"
    if mismatch:
        return "title_mismatch", mismatch[0], mismatch[1]
    return outcome, last_url, None


def interleave(groups: dict[str, list]) -> list:
    """Items taken in turn from each group, so consecutive requests go to
    different hosts and pacing rarely waits."""
    queues = [list(items) for _, items in sorted(groups.items())]
    out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out

