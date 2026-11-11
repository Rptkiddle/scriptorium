"""Item identity: identifiers, author name keys, and preprint and published
versions (matching, Extra lines, pairs)."""

from scriptorium_server.identity import (
    VersionIndex,
    classify,
    creator_keys,
    item_identifiers,
    keys_from_formatted,
    normalise_title,
    pair_action,
    query_key,
    rank,
    record_line,
    version_pairs,
    with_line,
)


# --- identifiers ---------------------------------------------------------------


def test_classify_recognises_doi_arxiv_and_title_forms():
    assert classify("10.1093/POQ/nfab018") == ("doi", "10.1093/poq/nfab018")
    assert classify("https://doi.org/10.1093/poq/nfab018.") == ("doi", "10.1093/poq/nfab018")
    assert classify("doi:10.1000/x") == ("doi", "10.1000/x")
    assert classify("https://doi.org/10.31235/osf.io/e3kvj_v1") == ("doi", "10.31235/osf.io/e3kvj")
    for form in ("2603.13270", "arXiv:2603.13270v2", "https://arxiv.org/abs/2603.13270v1",
                 "https://arxiv.org/pdf/2603.13270.pdf", "10.48550/arXiv.2603.13270"):
        assert classify(form) == ("arxiv", "2603.13270"), form
    assert classify("The Public Sphere as a Dynamic Network") == (
        "title", "the public sphere as a dynamic network")


def test_item_identifiers_reads_own_ids_and_extra_lines_by_value():
    own, recorded = item_identifiers("10.48550/arXiv.2501.00001", "arXiv:2501.00001", "")
    assert own == ["arxiv:2501.00001"] and recorded == []
    extra = ("arXiv: 2603.13270\nSocArXiv: 10.31235/OSF.IO/ABCDE\nCitation Key: x2021\n"
             "surveillance-why: see 2021.12345 for context\nDOI: 10.1000/own")
    own, recorded = item_identifiers("", "", extra)
    assert own == ["doi:10.1000/own"]
    assert recorded == ["arxiv:2603.13270", "doi:10.31235/osf.io/abcde"]


def test_normalise_title_drops_markup_accents_and_punctuation():
    assert normalise_title("The <i>Public</i> Sphere &amp; Café\\& co.") == "the public sphere cafe co"
    assert normalise_title("If You Have Choices, Why Not Choose?") == "if you have choices why not choose"


# --- author name keys ----------------------------------------------------------


def person(last, first=""):
    return {"creatorType": "author", "lastName": last, "firstName": first}


def test_creator_keys_for_each_stored_pattern():
    assert creator_keys([person("van Atteveldt", "Wouter")]) == ["atteveldt"]
    assert creator_keys([person("Atteveldt", "Wouter van")]) == ["atteveldt"]
    assert creator_keys([person("Wouter van Atteveldt")]) == ["atteveldt", "wouter"]
    assert creator_keys([person("Damian Trilling")]) == ["damian", "trilling"]
    assert creator_keys([person("C. Kroon")]) == ["kroon"]
    assert creator_keys([person("Flöck", "Fabian")]) == ["flock"]
    assert creator_keys([person("García Márquez", "Gabriel")]) == ["garcia", "marquez"]
    assert creator_keys([person("Müller-Lüdenscheidt", "A.")]) == [
        "ludenscheidt", "muller", "muller-ludenscheidt"]
    assert creator_keys([person("Le", "Thi")]) == ["le"]
    assert creator_keys([person("Pham Huong Anh")]) == ["anh", "huong", "pham"]
    assert creator_keys([{"name": 'Fr{\\"o}hling, Leon'}]) == ["frohling", "leon"]
    assert keys_from_formatted("van Atteveldt, Kiddle & Welbers") == ["atteveldt", "kiddle", "welbers"]


def test_query_key_reduces_any_typed_form_to_one_key():
    for typed in ("van Atteveldt", "Van Atteveldt", "Wouter van Atteveldt", "Atteveldt, W.",
                  "atteveldt", "W. van Atteveldt"):
        assert query_key(typed) == "atteveldt", typed
    assert query_key("Flöck") == "flock" and query_key("Le") == "le"
    assert query_key("Müller-Lüdenscheidt") == "muller-ludenscheidt"
    assert query_key("Gabriel García Márquez") == "marquez"


# --- versions ------------------------------------------------------------------


def _item(key, item_type, title, surname, doi="", **extra_fields):
    return {"key": key, "itemType": item_type, "title": title, "DOI": doi,
            "creators": [{"creatorType": "author", "firstName": "A", "lastName": surname}],
            **extra_fields}


PUBLISHED = _item("PUB1", "journalArticle", "Echo Chambers Revisited", "Trilling",
                  "10.1000/journal.1")
PREPRINT = _item("PRE1", "preprint", "Echo chambers revisited!", "Trilling",
                 "10.31235/osf.io/abcde", repository="SocArXiv")


def test_title_and_a_shared_author_pair_a_preprint_with_its_published_version():
    index = VersionIndex([PUBLISHED])
    match = index.other_version(PREPRINT)
    assert match.item is PUBLISHED and match.how == "title + shared author"
    assert VersionIndex([PREPRINT]).other_version(PUBLISHED).item is PREPRINT


def test_two_published_items_with_the_same_title_and_author_are_not_versions():
    extension = _item("CONF", "conferencePaper", "Echo Chambers Revisited", "Trilling")
    assert VersionIndex([PUBLISHED]).other_version(extension) is None


def test_no_shared_author_is_not_a_match():
    other = _item("PRE2", "preprint", "Echo Chambers Revisited", "Welbers")
    assert VersionIndex([PUBLISHED]).other_version(other) is None


def test_a_changed_author_order_or_a_typographic_hyphen_still_matches():
    def authors(*surnames):
        return [{"creatorType": "author", "firstName": "A", "lastName": s} for s in surnames]
    published = {**PUBLISHED, "creators": authors("Kroon", "Elliott-Graves")}
    reordered = {**PREPRINT, "creators": authors("Welbers", "Kroon")}
    hyphen = {**PREPRINT, "key": "PRE4", "creators": authors("Elliott\u2010Graves")}
    index = VersionIndex([published])
    assert index.other_version(reordered).item is published
    assert index.other_version(hyphen).item is published


def test_crossref_links_match_whatever_the_title():
    retitled = _item("PRE3", "preprint", "An early draft", "Kroon", "10.33774/apsa-1-v2")
    record = {"relation": {"is-preprint-of": [{"id-type": "doi", "id": "10.1000/JOURNAL.1"}]}}
    match = VersionIndex([PUBLISHED]).other_version(retitled, record)
    assert match.item is PUBLISHED and match.how == "crossref is-preprint-of"
    record = {"relation": {"has-preprint": [{"id-type": "doi", "id": "10.33774/apsa-1"},
                                            {"id-type": "doi", "id": "10.33774/apsa-1-v2"}]}}
    match = VersionIndex([retitled]).other_version(PUBLISHED, record)
    assert match.item is retitled and match.how == "crossref has-preprint"


def test_an_arxiv_doi_in_a_crossref_link_finds_the_arxiv_item():
    arxiv = _item("ARX", "preprint", "Something else", "Kroon", "10.48550/arXiv.2207.00868",
                  archiveID="arXiv:2207.00868", repository="arXiv")
    record = {"relation": {"has-preprint": [{"id-type": "doi", "id": "10.48550/arxiv.2207.00868"}]}}
    assert VersionIndex([arxiv]).other_version(PUBLISHED, record).item is arxiv


def test_a_published_version_is_preferred_over_a_preprint():
    index = VersionIndex([PREPRINT, PUBLISHED])
    newer = _item("PRE9", "preprint", "Echo Chambers Revisited", "Trilling", "10.1101/x")
    assert index.other_version(newer).item is PUBLISHED


def test_recorded_preprints_are_found_by_their_identifier():
    index = VersionIndex([{**PUBLISHED, "extra": "SocArXiv: 10.31235/osf.io/abcde"}])
    assert index.holder("10.31235/osf.io/abcde")["key"] == "PUB1"
    assert index.holder("10.1000/journal.1")["key"] == "PUB1"
    assert index.holder("10.1000/other") is None


def test_record_lines_use_the_repository_never_doi():
    assert record_line(PREPRINT) == ("doi:10.31235/osf.io/abcde",
                                     "SocArXiv: 10.31235/osf.io/abcde")
    arxiv = {"itemType": "preprint", "DOI": "10.48550/arXiv.2207.00868",
             "archiveID": "arXiv:2207.00868", "repository": "arXiv"}
    assert record_line(arxiv) == ("arxiv:2207.00868", "arXiv: 2207.00868")
    assert record_line({"itemType": "preprint", "DOI": "10.1000/x", "repository": "DOI"})[1] \
        == "Preprint: 10.1000/x"


def test_with_line_appends_once():
    extra = with_line("surveillance-why: fits", "doi:10.1000/x", "SocArXiv: 10.1000/x")
    assert extra == "surveillance-why: fits\nSocArXiv: 10.1000/x"
    assert with_line(extra, "doi:10.1000/x", "SocArXiv: 10.1000/x") == extra
    assert with_line("", "doi:10.1000/x", "SocArXiv: 10.1000/x") == "SocArXiv: 10.1000/x"


# --- pairs across the collection -------------------------------------------------


def test_pairs_drop_a_preprint_for_its_published_version_and_rank_preprints():
    arxiv = _item("ARX", "preprint", "Echo Chambers Revisited", "Trilling",
                  "10.48550/arXiv.2603.13270", archiveID="arXiv:2603.13270")
    philsci = _item("PHIL", "preprint", "Model Anarchism", "Veit", archiveID="PhilSci:1")
    arxiv2 = _item("ARX2", "preprint", "Model anarchism", "Veit",
                   "10.48550/arXiv.2001.00001", archiveID="arXiv:2001.00001")
    assert [rank(i) for i in (PREPRINT, arxiv, philsci)] == [0, 1, 2]
    pairs = version_pairs([PUBLISHED, PREPRINT, philsci, arxiv2])
    assert [(k["key"], d["key"]) for k, d, _ in pairs] == [("PUB1", "PRE1"), ("ARX2", "PHIL")]


def test_two_published_items_are_never_paired():
    extension = _item("CONF", "conferencePaper", "Echo Chambers Revisited", "Trilling")
    assert version_pairs([PUBLISHED, extension]) == []


def test_pair_actions():
    assert pair_action(PUBLISHED, PREPRINT, []) == {
        "line": "SocArXiv: 10.31235/osf.io/abcde", "extra": "SocArXiv: 10.31235/osf.io/abcde",
        "trash": True, "action": "recorded; trashed"}
    kept = pair_action(PUBLISHED, PREPRINT, [{"item_type": "attachment"}])
    assert not kept["trash"] and kept["action"].endswith("it has attachment child items")
    already = pair_action({**PUBLISHED, "extra": "SocArXiv: 10.31235/osf.io/abcde"}, PREPRINT, [])
    assert already["extra"] is None and already["trash"]
    philsci = _item("PHIL", "preprint", "Echo Chambers Revisited", "Trilling", archiveID="PhilSci:1")
    flagged = pair_action(PUBLISHED, philsci, [])
    assert not flagged["trash"] and flagged["extra"] is None
    assert flagged["action"].startswith("flagged")
