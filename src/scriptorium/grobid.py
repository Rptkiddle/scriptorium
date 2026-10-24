"""GROBID, the program that reads a PDF into structured text (TEI XML).

GROBID is a Java service. scriptorium runs GROBID's standalone install
(its start script plus its models folder) on this machine, on a local
port, only while there are PDFs to read: `ensure()` starts it when it is
not running, and whoever owns the Grobid object stops it when it has been
idle (the daemon's reaper, after general.grobid_idle_minutes) or when the
process exits (a command-line sync, a toolkit run). It uses 3-4 GB of
memory while running and none otherwise.

Install (once, outside the repo): build GROBID from its release and unpack
the two distribution zips (grobid-service, grobid-home) into one folder,
the `grobid_dir` setting; give pdfalto back its execute bit
(`chmod +x grobid-home/pdfalto/*/pdfalto*`, the zip drops it); bind the
server to 127.0.0.1 in grobid-home/config/grobid.yaml. Java 21 is found
from the `grobid_java_home` setting, else JAVA_HOME, else Homebrew's
openjdk@21.
"""

import atexit
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

from scriptorium.tei import GROBID_FORM

_JAVA_CANDIDATES = (
    "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
)
START_TIMEOUT = 300  # seconds; models load in 10-80 s depending on the machine's load


class GrobidUnavailable(RuntimeError):
    """GROBID is not installed, will not start, or its port is taken."""


def java_home(configured: str = "") -> str:
    for candidate in (configured, os.environ.get("JAVA_HOME", ""), *_JAVA_CANDIDATES):
        if candidate and (Path(candidate).expanduser() / "bin" / "java").exists():
            return str(Path(candidate).expanduser())
    raise GrobidUnavailable("Java 21 not found: set grobid_java_home in the settings"
                            " (e.g. `brew install openjdk@21`)")


class Grobid:
    """One GROBID service on 127.0.0.1:<port>, started on demand."""

    def __init__(self, install_dir: str | Path, port: int = 8070, java: str = "",
                 log_path: Path | None = None):
        self.install_dir = Path(install_dir).expanduser()
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.java = java
        self.log_path = log_path
        self.last_used = 0.0
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._version = ""

    # -- state ---------------------------------------------------------------

    def alive(self) -> bool:
        try:
            r = requests.get(f"{self.url}/api/isalive", timeout=5)
            return r.ok and r.text.strip() == "true"
        except requests.RequestException:
            return False

    @property
    def running(self) -> bool:
        """Whether this object started a GROBID that is still running."""
        return self._proc is not None and self._proc.poll() is None

    def version(self) -> str:
        if not self._version:
            r = requests.get(f"{self.url}/api/version", timeout=5)
            self._version = r.json().get("version", "")
        return self._version

    def _port_taken(self) -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    # -- start and stop --------------------------------------------------------

    def ensure(self) -> None:
        """Make GROBID answer, starting it if nothing runs on the port."""
        with self._lock:
            self.last_used = time.monotonic()
            if self.alive():
                return
            if self._port_taken():
                raise GrobidUnavailable(f"port {self.port} is in use by something that"
                                        " does not answer as GROBID")
            script = self.install_dir / "grobid-service" / "bin" / "grobid-service"
            if not script.exists():
                raise GrobidUnavailable(f"GROBID is not installed at {self.install_dir}"
                                        " (setting grobid_dir)")
            env = os.environ | {"JAVA_HOME": java_home(self.java), "LC_ALL": "C",
                                "JAVA_OPTS": "-Xmx3072m"}
            out = open(self.log_path, "a") if self.log_path else subprocess.DEVNULL  # noqa: SIM115
            self._proc = subprocess.Popen([str(script)], cwd=self.install_dir, env=env,
                                          stdout=out, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            atexit.register(self.stop)
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                if self.alive():
                    self.last_used = time.monotonic()
                    return
                if self._proc.poll() is not None:
                    self._proc = None
                    raise GrobidUnavailable("GROBID exited while starting"
                                            + (f"; see {self.log_path}" if self.log_path else ""))
                time.sleep(1)
            self.stop()
            raise GrobidUnavailable(f"GROBID did not answer within {START_TIMEOUT} s")

    def stop(self) -> None:
        """Stop the GROBID this object started (never one it did not)."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)

    # -- reading a PDF ---------------------------------------------------------

    def parse(self, pdf: str | Path) -> tuple[int, bytes]:
        """(HTTP status, body) for one PDF: on 200 the body is the TEI; on
        an error, GROBID's message (e.g. [NO_BLOCKS] for a scan with no text)."""
        self.ensure()
        with open(pdf, "rb") as f:
            r = requests.post(f"{self.url}/api/processFulltextDocument",
                              files={"input": f}, data=list(GROBID_FORM), timeout=600)
        self.last_used = time.monotonic()
        return r.status_code, r.content
