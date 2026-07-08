"""_doc_block magic-byte sniffing + /upload kind gate. Run: python test_hw.py"""
import io
import zipfile

import app


def demo():
    # PDF magic -> native document block
    b = app._doc_block(b"%PDF-1.7 rest of file")
    assert b["type"] == "document" and b["source"]["media_type"] == "application/pdf"

    # jpeg / png / webp photos -> image blocks
    assert app._doc_block(b"\xff\xd8\xff\xe0 jpeg body")["source"]["media_type"] == "image/jpeg"
    assert app._doc_block(b"\x89PNG\r\n\x1a\n png body")["source"]["media_type"] == "image/png"
    assert app._doc_block(b"RIFF\x00\x00\x00\x00WEBPVP8 ")["source"]["media_type"] == "image/webp"

    # docx (zip magic) -> extracted text block
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:t>homework text</w:t></w:document>")
    b = app._doc_block(buf.getvalue())
    assert b["type"] == "text" and "homework text" in b["text"]

    # junk -> ValueError, not a crash later inside the Claude call
    try:
        app._doc_block(b"\x00\x01\x02garbage")
        assert False, "expected ValueError"
    except ValueError:
        pass

    print("test_hw: OK")


if __name__ == "__main__":
    demo()
