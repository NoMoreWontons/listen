"""E2E: POST /upload creates upload_audio row + audio file on disk."""
from fastapi.testclient import TestClient
import app

c = TestClient(app.app)
r = c.post("/upload?kind=audio&filename=memo test.m4a", content=b"not real audio")
rid = r.json()["id"]
try:
    row = app.sb.table("recordings").select("title,status,source").eq("id", rid).single().execute().data
    assert row["title"] == "memo test", row
    assert row["source"] == "upload_audio", row
    assert app.audio_path(rid).read_bytes() == b"not real audio"
    # unknown kind must be rejected *before* the insert — otherwise every test run
    # leaks a permanent "[error: unsupported file type]" row into the real DB
    bad = c.post("/upload?kind=nope&filename=x.pdf", content=b"x")
    assert bad.json() == {"error": "unknown kind 'nope'"}, bad.json()
    print("ok: upload creates row + file, unknown kind rejected")
finally:
    app.sb.table("recordings").delete().eq("id", rid).execute()
    app.audio_path(rid).unlink(missing_ok=True)
