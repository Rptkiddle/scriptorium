"""The file store keeps a stripped copy of each PDF, once. The test PDF is
built here: one page with a highlight, a sticky note and a link, an author
in the document information, XMP metadata, a form and an embedded file."""

import pikepdf
import pytest

from scriptorium_server.filestore import FileStore, UnreadablePdf, digest, strip_pdf


def annotation(pdf, subtype, **more):
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name(f"/{subtype}"),
        Rect=pikepdf.Array([10, 10, 50, 50]), **more))


@pytest.fixture
def marked_pdf(tmp_path):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    page = pdf.pages[0]
    page.obj.Annots = pikepdf.Array([
        annotation(pdf, "Highlight", Contents=pikepdf.String("my private remark")),
        annotation(pdf, "Text", Contents=pikepdf.String("sticky note")),
        annotation(pdf, "Link", A=pikepdf.Dictionary(S=pikepdf.Name.URI,
                                                     URI=pikepdf.String("https://example.org"))),
    ])
    with pdf.open_metadata() as meta:
        meta["dc:creator"] = ["Jane Doe"]
    pdf.docinfo["/Author"] = "Jane Doe"
    pdf.docinfo["/Creator"] = "Jane's PDF editor"
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array())
    pdf.attachments["notes.txt"] = pikepdf.AttachedFileSpec(pdf, b"private notes")
    path = tmp_path / "marked.pdf"
    pdf.save(path)
    return path


def test_annotations_are_stripped(marked_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    strip_pdf(marked_pdf, out)
    with pikepdf.open(out) as pdf:
        annots = pdf.pages[0].obj.get("/Annots")
        assert [a.Subtype for a in annots] == [pikepdf.Name.Link]
        assert "/Info" not in pdf.trailer or "/Author" not in pdf.docinfo
        assert "/Metadata" not in pdf.Root and "/AcroForm" not in pdf.Root
        assert len(pdf.attachments) == 0
    raw = out.read_bytes()
    for private in (b"Jane", b"private remark", b"sticky note", b"private notes"):
        assert private not in raw


def test_stripping_the_same_file_twice_gives_the_same_bytes(marked_pdf, tmp_path):
    strip_pdf(marked_pdf, tmp_path / "a.pdf")
    strip_pdf(marked_pdf, tmp_path / "b.pdf")
    assert (tmp_path / "a.pdf").read_bytes() == (tmp_path / "b.pdf").read_bytes()


def test_same_file_twice_is_stored_once(marked_pdf, tmp_path):
    store = FileStore(tmp_path / "data")
    first = store.put(marked_pdf)
    second = store.put(marked_pdf)
    assert first == second
    stored = list((tmp_path / "data" / "pdfs").rglob("*.pdf"))
    assert stored == [store.path(first[0])]
    assert digest(stored[0], "sha256") == first[0]
    assert marked_pdf.exists()  # the source is left as it is


def test_a_file_that_is_not_a_pdf_is_refused_and_nothing_is_stored(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a PDF")
    store = FileStore(tmp_path / "data")
    with pytest.raises(UnreadablePdf):
        store.put(bad)
    assert not any((tmp_path / "data" / "pdfs").rglob("*"))


def test_any_failure_while_rewriting_one_file_counts_as_unreadable(marked_pdf, tmp_path, monkeypatch):
    import scriptorium_server.filestore as filestore

    def odd(*args, **kwargs):
        raise KeyError("/Annots")  # not one of pikepdf's own errors

    monkeypatch.setattr(filestore.pikepdf.Pdf, "save", odd)
    with pytest.raises(UnreadablePdf, match="KeyError"):
        strip_pdf(marked_pdf, tmp_path / "out.pdf")
