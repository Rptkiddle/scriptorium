"""Passages from GROBID's TEI (scriptorium.tei): which parts count, the size
bounds, pages, the two texts, artefacts, and the stored-TEI record. The
TEI here is written for the tests; no library text."""

import pytest

from scriptorium.tei import (
    GROBID_REQUEST,
    MAX_WORDS,
    EmptyBody,
    TeiStore,
    passages,
    prose_share,
)

PROSE = ("the model of the network is a way to see how the group and its members"
         " act in the world")  # 20 words, prose


def s(text: str, page: int = 1) -> str:
    return f'<s coords="{page},1,1,1,1">{text}</s>'


def p(*sentences: str) -> str:
    return "<p>" + "".join(sentences) + "</p>"


def tei(body: str = "", back: str = "") -> bytes:
    return ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text>'
            f"<body>{body}</body><back>{back}</back></text></TEI>").encode()


def words(n: int, word: str = "the") -> str:
    return " ".join([word] * n)


def test_body_footnotes_and_appendix_count_and_nothing_else():
    doc = tei(
        body=(f'<div><head n="1">Intro</head>{p(s(PROSE))}</div>'
              f'<figure><head>Fig. 1</head><figDesc>{PROSE}</figDesc>{p(s("caption text"))}</figure>'
              f'<formula>x = y</formula>'
              f'<note place="foot" n="1">{p(s(PROSE + " in a footnote", 2))}</note>'),
        back=(f'<div type="acknowledgement">{p(s(PROSE + " thanks"))}</div>'
              f'<div type="annex"><div>{p(s(PROSE + " appendix", 9))}</div></div>'
              f'<div type="references"><listBibl><biblStruct>{PROSE}</biblStruct></listBibl></div>'))
    got = passages(doc)
    assert [(x.part, x.pages) for x in got] == [("body", "1"), ("footnote", "2"), ("appendix", "9")]
    assert "caption" not in " ".join(x.text for x in got)
    assert all("thanks" not in x.text for x in got)


def test_a_fragment_joins_the_next_block_and_a_trailing_one_the_block_before():
    doc = tei(body=p(s("where")) + p(s(PROSE, 2)) + p(s(PROSE, 3)) + p(s("Short end.", 4)))
    got = passages(doc)
    assert [x.text for x in got] == [f"where {PROSE}", f"{PROSE} Short end."]
    assert [x.pages for x in got] == ["1-2", "3-4"]


def test_a_part_whose_whole_text_is_short_stays_one_passage():
    doc = tei(body=p(s(PROSE)), back='<div type="annex">' + p(s("A1 only")) + "</div>")
    assert [x.text for x in passages(doc) if x.part == "appendix"] == ["A1 only"]


def test_a_long_paragraph_is_cut_between_sentences_into_equal_pieces():
    sentence = words(29) + " end."  # 30 words
    doc = tei(body=p(*[s(sentence, 1 + i // 10) for i in range(15)]))  # 450 words
    got = passages(doc)
    assert [len(x.text.split()) for x in got] == [240, 210]
    assert all(x.text.endswith("end.") for x in got)
    assert [x.pages for x in got] == ["1", "1-2"]  # sentences 1-10 on page 1, 11-15 on 2


def test_a_sentence_over_the_maximum_is_cut_at_word_boundaries():
    doc = tei(body=p(s(words(700))))
    got = passages(doc)
    assert [len(x.text.split()) for x in got] == [234, 234, 232]
    assert sum(len(x.text.split()) for x in got) == 700
    assert max(len(x.text.split()) for x in got) <= MAX_WORDS


def test_sentences_are_joined_with_a_space():
    doc = tei(body=p(s("First sentence of the text is here and it is long enough."),
                     s("Second sentence follows it in the same paragraph.")))
    assert "here and it is long enough. Second" in passages(doc)[0].text


def test_markers_stay_in_the_text_but_leave_the_embedded_text():
    body = p(s('the effect of the group is <ref type="bibr" target="#b1">(Smith 2020)</ref>'
               ' large<ref type="foot" target="#foot_0">1</ref> and the way of it'
               ' is <ref type="figure">Fig. 2</ref> as seen in the data of the work'))
    got = passages(tei(body=body))[0]
    assert "(Smith 2020)" in got.text and "large1 and" in got.text
    assert "Smith" not in got.embed_text and "large and" in got.embed_text
    assert "Fig. 2" in got.embed_text


def test_grobids_footnote_placeholder_leaves_the_stored_text():
    body = p(s('the effect of the group is large<ref type="foot">foot_0</ref> and the way'
               ' of it is as seen in the data of the work'))
    got = passages(tei(body=body))[0]
    assert "foot_0" not in got.text and "large and" in got.text


def test_tables_read_as_text_are_marked_artefacts():
    table = " ".join(f"{i}.{i} 0.{i}" for i in range(30))
    got = passages(tei(body=p(s(PROSE)) + p(s(table))))
    assert [x.artefact for x in got] == [False, True]
    assert prose_share(PROSE) > 0.3 and prose_share(table) == 0


def test_quoted_german_counts_as_prose():
    german = ("Du argumentierst immer zuerst über die möglichen Optionen und es ist"
              " nicht so dass die Antwort mit der Frage auf den Punkt kommt")
    assert not passages(tei(body=p(s(german))))[0].artefact


def test_no_body_text_is_an_error():
    with pytest.raises(EmptyBody):
        passages(tei(back='<div type="annex">' + p(s(PROSE)) + "</div>"))


def test_a_stored_tei_is_used_only_with_the_same_pdf_and_settings(tmp_path):
    store = TeiStore(tmp_path)
    store.put("K1", "/x/K1.pdf", "md5a", "0.9.1", 200, 1.5, tei=b"<TEI/>")
    store.put("K2", "/x/K2.pdf", "md5b", "0.9.1", 500, 0.2, error="[NO_BLOCKS]")
    again = TeiStore(tmp_path)
    assert again.usable("K1", "md5a") and again.read("K1") == b"<TEI/>"
    assert not again.usable("K1", "other md5")
    assert not again.usable("K2", "md5b")
    assert again.latest()["K1"]["settings"] == GROBID_REQUEST
    assert not list(tmp_path.glob("*.part"))
    row = again.latest()["K1"]
    row["settings"] = "something else"
    assert not again.usable("K1", "md5a")


def test_the_request_string_matches_the_bulk_parse_record():
    # The 2026-10-24 bulk parse recorded this exact string beside each TEI;
    # the index adopts those files only if it is unchanged.
    assert GROBID_REQUEST == ("processFulltextDocument;consolidateHeader=0;"
                              "consolidateCitations=0;consolidateFunders=0;"
                              "segmentSentences=1;teiCoordinates=p;teiCoordinates=s")
