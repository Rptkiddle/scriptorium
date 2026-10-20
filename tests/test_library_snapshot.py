"""One library read, several derivations; and create_items reusing it."""

from scriptorium.config import Config
from scriptorium.discovery import library_titles
from scriptorium.zotero import citation_key_of, citekeys_of, dois_of
from scriptorium.zotero_write import ZoteroWriter

ITEMS = [
    {"title": "A Paper", "DOI": "https://doi.org/10.1/ABC", "citationKey": "aPaper2026"},
    {"title": "A Report", "extra": "Citation Key: rep2025\ndoi: 10.2/xyz"},
    {"title": "", "extra": ""},
]


def test_derivations():
    assert dois_of(ITEMS) == {"10.1/abc", "10.2/xyz"}
    assert citekeys_of(ITEMS) == {"aPaper2026", "rep2025"}
    assert citation_key_of(ITEMS[1]) == "rep2025"
    assert library_titles(ITEMS) == {"apaper", "areport"}


def test_create_items_uses_the_given_keys_without_reading(tmp_path):
    class _Reader:
        def citation_keys(self):
            raise AssertionError("library read although keys were given")

    class _Bridge:
        def create_items(self, items):
            return {"created": ["K1"], "failed": {}}

    w = ZoteroWriter(Config(index_dir=tmp_path), reader=_Reader())
    w._bridge = _Bridge()
    taken = {"smithPaper2026"}
    w.create_items([{"itemType": "journalArticle", "title": "Paper",
                     "creators": [{"creatorType": "author", "lastName": "Smith"}],
                     "date": "2026"}], taken=taken)
    assert len(taken) == 2  # the generated key was added
