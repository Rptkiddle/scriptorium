"""The PDF hash cache: a sync re-reads only PDFs whose size or mtime changed."""

import os
from types import SimpleNamespace

from scriptorium import parsing
from scriptorium.parsing import PdfHashes


def _counting_md5(monkeypatch):
    calls = []

    def fake(path):
        calls.append(path)
        return f"md5:{open(path, 'rb').read().decode()}"

    monkeypatch.setattr(parsing, "md5sum", fake)
    return calls


def _rec(path):
    return SimpleNamespace(pdf_path=str(path))


def test_unchanged_files_are_not_rehashed(tmp_path, monkeypatch):
    calls = _counting_md5(monkeypatch)
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_text("A")
    b.write_text("B")
    cache_file = tmp_path / "pdf_hashes.json"

    first = PdfHashes(cache_file)
    assert first.of(_rec(a)) == "md5:A" and first.of(_rec(b)) == "md5:B"
    first.save(prune=True)
    assert len(calls) == 2

    second = PdfHashes(cache_file)
    assert second.of(_rec(a)) == "md5:A" and second.of(_rec(b)) == "md5:B"
    assert len(calls) == 2  # both served from the cache


def test_a_touched_file_is_rehashed(tmp_path, monkeypatch):
    calls = _counting_md5(monkeypatch)
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_text("A")
    b.write_text("B")
    cache_file = tmp_path / "pdf_hashes.json"
    first = PdfHashes(cache_file)
    first.of(_rec(a))
    first.of(_rec(b))
    first.save()

    a.write_text("A2")
    st = a.stat()
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    second = PdfHashes(cache_file)
    assert second.of(_rec(a)) == "md5:A2"
    assert second.of(_rec(b)) == "md5:B"
    assert calls[2:] == [str(a)]


def test_prune_drops_paths_not_seen(tmp_path, monkeypatch):
    _counting_md5(monkeypatch)
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_text("A")
    b.write_text("B")
    cache_file = tmp_path / "pdf_hashes.json"
    first = PdfHashes(cache_file)
    first.of(_rec(a))
    first.of(_rec(b))
    first.save()

    second = PdfHashes(cache_file)
    second.of(_rec(a))
    second.save()  # no prune: b stays
    assert str(b) in PdfHashes(cache_file)._cache
    third = PdfHashes(cache_file)
    third.of(_rec(a))
    third.save(prune=True)
    assert set(PdfHashes(cache_file)._cache) == {str(a)}


def test_missing_or_corrupt_cache_is_empty(tmp_path, monkeypatch):
    _counting_md5(monkeypatch)
    cache_file = tmp_path / "pdf_hashes.json"
    assert PdfHashes(cache_file)._cache == {}
    cache_file.write_text("{not json")
    assert PdfHashes(cache_file)._cache == {}


def test_unreadable_file_raises_oserror(tmp_path, monkeypatch):
    _counting_md5(monkeypatch)
    try:
        PdfHashes(tmp_path / "c.json").of(_rec(tmp_path / "gone.pdf"))
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
