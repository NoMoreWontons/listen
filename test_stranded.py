"""A transcription worker can die without ever marking its row errored (its own
error write can fail too), leaving the row reading 'transcribing' with nothing
running -- a 76-minute lecture sat like that on 2026-09-16. _retry_if_stranded,
hung off the list poll, is what notices. Run: .venv/Scripts/python.exe test_stranded.py
"""
import os
import threading

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


class FakePath:
    def __init__(self, there):
        self.there = there

    def exists(self):
        return self.there


def stub_audio(present):
    app.audio_path = lambda rid: FakePath(rid in present)


def row(rid, status="transcribing"):
    return {"id": rid, "status": status}


# _spawn_process claims the gate BEFORE the thread exists -- otherwise a poll
# landing between /stop's status write and the worker's first line sees no gate
# and starts a second decode of the same recording.
ran = threading.Event()
app.process = lambda rid: ran.set()
app._spawn_process("live")
assert "live" in app._transcribe_gates, "gate must be claimed by _spawn_process itself"
assert ran.wait(2), "worker thread never ran"
app._transcribe_gates.pop("live", None)

# claiming the gate early must not break the battery-saver pause: the gate
# /transcribe/pause toggles is the one the worker is about to block on, so a
# recording paused between /stop and the model load stays paused.
blocked = threading.Event()


def waiting_worker(rid):
    blocked.set()
    app._gate(rid).wait()


app.process = waiting_worker
app._spawn_process("pausable")
assert app.transcribe_pause("pausable") == {"paused": True}, "pause must take effect"
assert not app._gate("pausable").is_set(), "paused worker must be holding at its gate"
assert app.transcribe_pause("pausable") == {"paused": False}, "second toggle resumes"
assert blocked.wait(2)
app._transcribe_gates.pop("pausable", None)

spawned = []
app._spawn_process = lambda rid: spawned.append(rid)

# stranded: transcribing, no worker holding a gate, audio still on disk
stub_audio({"a"})
app._retry_if_stranded(row("a"), None)
assert spawned == ["a"], spawned

# once per run -- a worker that dies instantly must not respawn on every 5s poll
app._retry_if_stranded(row("a"), None)
assert spawned == ["a"], spawned

# a live worker holds its gate for the whole of process(): leave it alone
stub_audio({"b"})
app._retry_if_stranded(row("b"), threading.Event())
assert spawned == ["a"], spawned

# PDF rows and still-downloading YouTube rows read 'transcribing' with no audio file
stub_audio(set())
app._retry_if_stranded(row("c"), None)
assert spawned == ["a"], spawned

# anything that isn't mid-transcription is not stranded
stub_audio({"d"})
for other in ("done", "error", "split_pending", "recording"):
    app._retry_if_stranded(row("d", other), None)
assert spawned == ["a"], spawned

print("test_stranded.py OK")
