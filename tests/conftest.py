"""Shared test setup."""

import pytest

from scriptorium.grobid import Grobid, GrobidUnavailable


@pytest.fixture(autouse=True)
def no_real_grobid(monkeypatch):
    """No test may start or call the real GROBID (a 3-4 GB Java service):
    tests that need one pass a fake."""
    def refuse(self, *args, **kwargs):
        raise GrobidUnavailable("tests never start the real GROBID")

    monkeypatch.setattr(Grobid, "ensure", refuse)
    monkeypatch.setattr(Grobid, "parse", refuse)
