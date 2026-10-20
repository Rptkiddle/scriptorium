"""Interrogation rules: scoring replies, judge split, citations, BibLaTeX."""

from scriptorium.interrogation import (
    bib_entry,
    number_citations,
    parse_rcs,
    split_judges,
)


def test_parse_rcs_reads_the_anchored_score():
    assert parse_rcs('{"summary": "Useful.", "relevance_score": 3}') == (3, "Useful.")
    assert parse_rcs('```json\n{"summary": "", "relevance_score": "0"}\n```') == (0, "")


def test_parse_rcs_rejects_scores_off_the_scale_or_missing():
    assert parse_rcs('{"summary": "x", "relevance_score": 7}') is None
    assert parse_rcs('{"summary": "x"}') is None


def test_split_judges_is_reproducible_and_complete():
    a = split_judges(10, 4, seed="2026-10-10-1000-q")
    assert a == split_judges(10, 4, seed="2026-10-10-1000-q")
    assert len(a) == 3 and sorted(i for j in a for i in j) == list(range(10))
    assert split_judges(5, 0, seed="s") == [[0, 1, 2, 3, 4]]


def test_number_citations_numbers_by_first_appearance():
    sources = {
        "smith2020-c1": {"name": "smith2020 pages 3-4", "citation": "Smith (2020)"},
        "lee2021-c0": {"name": "lee2021", "citation": "Lee (2021)"},
    }
    final, stray = number_citations(
        "A holds (lee2021-c0). B holds (smith2020-c1, lee2021-c0). C (gone2019-c2).",
        sources)
    assert final.startswith("A holds [1]. B holds [2, 1]. C (gone2019-c2).")
    assert "[1] lee2021-c0 — Lee (2021) (pages ?)" in final
    assert "[2] smith2020-c1 — Smith (2020) (pages 3-4)" in final
    assert stray == ["gone2019-c2"]


def test_bib_entry_fields_and_escaping():
    entry = bib_entry("wattsShould2017", {
        "itemType": "journalArticle", "title": "Should {social} science & more?",
        "creators": [{"creatorType": "author", "lastName": "Watts", "firstName": "Duncan J."}],
        "date": "2017-01-10", "publicationTitle": "Nature Human Behaviour",
        "DOI": "10.1038/s41562-016-0015"})
    assert entry.startswith("@article{wattsShould2017,")
    assert "author = {Watts, Duncan J.}" in entry
    assert "title = {Should social science \\& more?}" in entry
    assert "date = {2017}" in entry
