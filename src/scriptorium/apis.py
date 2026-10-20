"""The web APIs the arms share: Crossref, Semantic Scholar, OpenAlex.

One retrying JSON request with per-caller retry settings, the Crossref
wrapper, the Semantic Scholar key header, and the text normalisers more
than one arm needs. Requests that carry an API key never follow redirects,
so a key reaches only the host it was meant for.
"""

import os
import re
import time

import requests

from scriptorium.config import CONTACT_EMAIL as MAILTO

CROSSREF = "https://api.crossref.org"
S2 = "https://api.semanticscholar.org/graph/v1"
OPENALEX = "https://api.openalex.org"
KEY_HEADERS = {"x-api-key", "x-els-apikey", "x-els-insttoken"}


def http_session(agent: str) -> requests.Session:
    """A session identifying the calling arm (Crossref's polite pool)."""
    s = requests.Session()
    s.headers["User-Agent"] = f"scriptorium-{agent} (mailto:{MAILTO})"
    return s


def s2_headers() -> dict:
    """The Semantic Scholar key header: S2_API_KEY from the environment,
    else the discovery settings; empty when neither is set. Send it to
    api.semanticscholar.org only, and never with redirects followed."""
    key = os.environ.get("S2_API_KEY", "")
    if not key:
        from scriptorium.config import load_config
        from scriptorium.settings import load_settings

        key = load_settings(load_config())["discovery"].get("s2_api_key", "")
    return {"x-api-key": key} if key else {}


def request_json(
    session: requests.Session, method: str, url: str, *,
    params: dict | None = None, json_body: dict | None = None,
    headers: dict | None = None, attempts: int = 4, wait_429: float = 5, wait_error: float = 2,
    not_found: dict | None = None, timeout: float = 45,
) -> dict:
    """GET or POST url and return the JSON body, retrying rate limits
    (429: wait_429 × attempt seconds) and request errors: timeouts (408),
    server errors, network errors and unreadable bodies (wait_error ×
    attempt seconds, re-raised on the last attempt). Other client errors
    (4xx) raise at once, since asking again cannot change the answer; a
    404 returns not_found instead when it is given. Semantic Scholar calls
    carry the key header. A call carrying a key (a key header, or an
    api_key parameter) does not follow redirects, so the key never reaches
    another host."""
    headers = {**(s2_headers() if url.startswith(S2) else {}), **(headers or {})}
    keyed = (any(h.lower() in KEY_HEADERS for h in headers)
             or "api_key" in (params or {}))
    for attempt in range(attempts):
        try:
            resp = session.request(method, url, params=params, json=json_body,
                                   headers=headers, timeout=timeout,
                                   allow_redirects=not keyed)
            if resp.status_code == 429:
                time.sleep(wait_429 * (attempt + 1))
                continue
            if resp.status_code == 404 and not_found is not None:
                return not_found
            if 400 <= resp.status_code < 500 and resp.status_code != 408:
                break
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(wait_error * (attempt + 1))
    else:
        raise RuntimeError(f"rate-limited after {attempts} attempts: {url}")
    # A client error ended the loop.
    raise requests.HTTPError(f"{resp.status_code} Client Error for url: {url}", response=resp)


def crossref_get(session: requests.Session, url: str, **params) -> dict:
    """A Crossref API call (polite pool), returning the response's message."""
    params["mailto"] = MAILTO
    return request_json(session, "GET", url, params=params)["message"]


def strip_jats(text: str) -> str:
    """Plain text from Crossref's JATS-tagged abstracts."""
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


def norm_doi(doi: str) -> str:
    """A DOI lowercased, without a doi.org URL prefix."""
    doi = (doi or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
