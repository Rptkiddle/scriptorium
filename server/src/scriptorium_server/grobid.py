"""GROBID, the program that parses a PDF into structured text (TEI XML).

GROBID runs as a service of its own (in the deployment, the `grobid`
container), at the address in the setting grobid.address. This module is
its client: it sends one PDF and gets back the TEI, or GROBID's reason
for refusing the PDF. The request is the same for every PDF
(tei.GROBID_FORM), and is recorded beside each stored TEI.

GROBID takes a minute or so to load its models after it starts, and
answers "busy" (HTTP 503) when it has more PDFs at once than it is set to
take. parse() therefore waits for GROBID to answer, and tries a busy
request again, before it gives up with GrobidUnavailable: a reason about
GROBID, never about the PDF.
"""

import time
from pathlib import Path

import requests

from scriptorium_server.tei import GROBID_FORM

WAIT_SECONDS = 300  # how long parse() waits for GROBID to answer at all
BUSY_TRIES = 5  # how often a request GROBID calls busy is sent again
PARSE_TIMEOUT = 600  # seconds for one PDF; long books take minutes


class GrobidUnavailable(RuntimeError):
    """GROBID did not answer, or stayed busy."""


def default_session() -> requests.Session:
    return requests.Session()


class Grobid:
    """The GROBID service at `address` (e.g. http://grobid:8070)."""

    def __init__(self, address: str, session: requests.Session | None = None,
                 wait_seconds: float = WAIT_SECONDS, pause: float = 5.0):
        self.address = address.rstrip("/")
        self._session = session
        self.wait_seconds = wait_seconds
        self.pause = pause
        self._version = ""

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = default_session()
        return self._session

    def alive(self) -> bool:
        try:
            r = self.session.get(f"{self.address}/api/isalive", timeout=5)
            return r.ok and r.text.strip() == "true"
        except requests.RequestException:
            return False

    def wait(self) -> None:
        """Return once GROBID answers; GrobidUnavailable after wait_seconds."""
        deadline = time.monotonic() + self.wait_seconds
        while not self.alive():
            if time.monotonic() >= deadline:
                raise GrobidUnavailable(f"GROBID at {self.address} did not answer within"
                                        f" {self.wait_seconds:.0f} s")
            time.sleep(self.pause)

    def version(self) -> str:
        if not self._version:
            r = self.session.get(f"{self.address}/api/version", timeout=5)
            self._version = r.json().get("version", "")
        return self._version

    def parse(self, pdf: str | Path) -> tuple[int, bytes]:
        """(HTTP status, body) for one PDF: on 200 the body is the TEI; on
        another status, GROBID's message (e.g. [NO_BLOCKS] for a scan with
        no text). GrobidUnavailable when GROBID does not answer or stays busy."""
        self.wait()
        for _ in range(BUSY_TRIES):
            try:
                with open(pdf, "rb") as f:
                    r = self.session.post(f"{self.address}/api/processFulltextDocument",
                                          files={"input": f}, data=list(GROBID_FORM),
                                          timeout=PARSE_TIMEOUT)
            except requests.RequestException as exc:
                raise GrobidUnavailable(f"GROBID at {self.address}: {type(exc).__name__}:"
                                        f" {exc}") from exc
            if r.status_code != 503:
                return r.status_code, r.content
            time.sleep(self.pause)
        raise GrobidUnavailable(f"GROBID at {self.address} stayed busy")
