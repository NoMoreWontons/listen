# iPad + Apple Pencil into the listen vault

How handwritten notes and off-laptop audio reach the vault at
`C:\Users\savag\College Lectures`. Written 2026-08-25.

The short version: the intake endpoints mostly already exist. What's missing is
a way for the iPad to reach the server, a share-sheet Shortcut to hit it, and
one real gap — diagrams.

## What these notes actually are

The ink is **not** a second copy of the lecture. It's deliberately sparse: the
things audio can't carry. Diagrams and board work. Anything the professor wrote
down and never said out loud. The handful of points worth flagging as important.

That shapes everything below, and it splits the ink into two paths that are not
interchangeable:

| Kind of ink | Path | Why |
|---|---|---|
| **Companion to a lecture you recorded** — diagrams, board work, unspoken material | Attach to that recording via the listen UI's "Add notes from file". Never creates a new row. | `/label/{rid}` re-runs `analyze(transcript, notes, created_at)`, whose prompt says "giving weight to anything they flagged" (app.py:1841). Your board notes get fused into the lecture's own summary, weighted. One note, not two. |
| **Standalone material** — a handout, a worksheet, a page with no recording behind it | Share-sheet Shortcut → `/upload?kind=pdf` | Nothing to attach to. It becomes its own row and files itself. |

Sending companion ink through `/upload?kind=pdf` is the wrong move: it creates a
second `recordings` row that has to be auto-labeled from sparse handwriting, and
it pollutes the counts that flashcard and quiz generation read from.

## What already exists

| Endpoint | app.py | What it does |
|---|---|---|
| `POST /upload?kind=pdf` | 309 → 892 | PDF straight to Claude as a native document block (scanned and handwritten pages included, no OCR dependency). Auto-labels semester/class/unit/topic, summarizes, calls `write_note`. Files itself with zero interaction. |
| `POST /upload?kind=audio` | 309 | Raw audio body into the whisper pipeline, same as a live recording. |
| `POST /ocr` | 539 | Photo/PDF/pptx/docx → markdown, returned as text for the notes box. Human reviews before saving. Wired to the "Add notes from file" button in `index.html`. |
| `POST /label/{rid}` | 405 | User labels + notes as JSON. On a `done` recording whose notes changed, re-runs `analyze` so the summary absorbs them. This is the companion-ink path. |
| `POST /addendum/{rid}` | 436 | Dated append to a finished recording. Stored in the DB `addendum` column, so it survives note rewrites. |

All of these take a raw request body, not multipart — which is what makes them
easy to hit from Shortcuts.

## Step 1: make the server reachable from the iPad

`start_server.bat` used to bind `127.0.0.1`, which nothing outside the laptop
can see. It now binds the mesh IP `100.69.173.35`.

Do **not** switch that to `0.0.0.0`. There is no auth on any endpoint, the
process holds live Supabase and Anthropic keys, and `/delete_unit` is a POST
away. On campus or dorm wifi that's the whole app handed to the subnet.

Instead, put both devices on a private mesh and bind to the mesh interface
only.

**NordVPN Meshnet** (already paid for). Nord announced a Dec 1 2025 shutdown,
then reversed it in September 2025 and open-sourced the feature. It works, but
its long-term future is a shrug — Tailscale is the fallback if it rots.

1. Enable Meshnet in the NordVPN app on the laptop and on the iPad. Link the
   two devices.
2. Find the laptop's mesh IP — the Nord app's device list, or `tailscale ip -4`.
   Here it is **100.69.173.35** (CGNAT range, 100.64.0.0/10).
3. `start_server.bat` (already done):

   ```bat
   .venv\Scripts\python.exe -m uvicorn app:app --host 100.69.173.35 --port 8000
   ```

4. **Open the port in Windows Firewall.** This step is easy to miss and looks
   exactly like a broken mesh when you skip it. Loopback traffic bypasses the
   firewall entirely, so binding `127.0.0.1` never needed a rule; binding a real
   interface does, and Windows blocks inbound by default. In an **Administrator**
   PowerShell:

   ```powershell
   New-NetFirewallRule -DisplayName "listen server (NordLynx only)" `
     -Direction Inbound -Protocol TCP -LocalPort 8000 `
     -InterfaceAlias NordLynx -Action Allow -Profile Any
   ```

   `-InterfaceAlias NordLynx` scopes it to the mesh adapter. The Wi-Fi subnet
   stays blocked, and the socket doesn't exist there anyway because the bind is
   mesh-only. Two independent layers.

5. On the iPad: NordVPN app open, Meshnet on, laptop listed as a linked peer.
6. Browse to `http://100.69.173.35:8000`. **Type `http://` explicitly** —
   Safari and Chrome auto-upgrade a bare `IP:port` to `https://`, nothing is
   listening on HTTPS, and the failure reads as "not a secure connection"
   rather than as a wrong scheme.

Note that binding the mesh IP means **`localhost:8000` no longer works**. Use
`http://100.69.173.35:8000` on the laptop as well. The tradeoff is that NordVPN
has to be up to use the app at all, even locally. If that chafes, the
alternative is binding `0.0.0.0` and relying on the firewall rule above as the
only thing keeping the port off Wi-Fi — one layer instead of two. Not
recommended for an app with no auth and live API keys in the process.

### Checking it works

From the laptop, before touching the iPad:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000 | Select LocalAddress,OwningProcess
Invoke-WebRequest http://100.69.173.35:8000/recordings -UseBasicParsing | % StatusCode
```

A listener on `100.69.173.35` plus a `200` means the server is healthy and any
remaining failure is firewall or mesh, not the app.

Binding to the mesh IP rather than `0.0.0.0` means the port is never bound on
the wifi interface at all. Machines on the local subnet cannot see it, scan it,
or reach it. That is the entire security story for this setup.

Two gotchas:

- The mesh has to be up **before** uvicorn starts, or the bind fails because
  the IP doesn't exist yet. Start Nord first.
- Meshnet needs the NordVPN app running on the iPad too, not just the laptop.

## Step 2: the share-sheet Shortcuts

Before building anything, confirm reachability: on the iPad, open
`http://100.69.173.35:8000` in Safari with the server running. If the listen UI
doesn't load, the Shortcuts won't work either and the problem is the mesh, not
the Shortcut.

Two Shortcuts, one per kind. Four actions each. No branching — two separate
entries in the share sheet are faster to hit than one with a menu.

### Shortcut A — "Send Notes to Listen"

**Create it**

1. Shortcuts app, tap **+**.
2. Tap the shortcut name at the top, choose **Rename**, type
   `Send Notes to Listen`.
3. Tap the name again, choose **Details**.
4. Turn on **Show in Share Sheet**.
5. Tap **Share Sheet Types**, deselect everything, then select **Files**,
   **PDFs**, and **Images**. Images matter: `_doc_block` (app.py:1635) sniffs
   magic bytes, so a photo of a page goes down the same path as a PDF even
   though the URL says `kind=pdf`.
6. Tap **Done**. A **Receive ... from Share Sheet** header now sits at the top
   of the shortcut — that's `Shortcut Input`.

**Actions**

1. Search **Get Contents of URL**, add it.
2. In the URL field, type exactly:

   ```
   http://100.69.173.35:8000/upload?kind=pdf&filename=notes.pdf
   ```

3. Tap the **chevron** (`>`) on the action to expand it, then set:
   - **Method**: `POST`
   - **Request Body**: `File`
   - a **File** field appears — tap it and pick **Shortcut Input**
   - leave **Headers** empty
4. Add **Show Notification**. Set the text to `Sent to Listen`.

That's the whole shortcut. Four actions.

### Shortcut B — "Send Audio to Listen"

Identical, with three changes:

- Name: `Send Audio to Listen`
- **Share Sheet Types**: **Files** and **Media**
- URL:

  ```
  http://100.69.173.35:8000/upload?kind=audio&filename=lecture.m4a
  ```

### Using them

These Shortcuts are for **standalone material only** — a handout, a worksheet, a
recording made away from the laptop. Companion ink for a lecture does not go
through them; see "Attaching companion ink" below.

| App | Path |
|---|---|
| GoodNotes | Share → Export → PDF → pick the pages → Share → *Send Notes to Listen* |
| Apple Notes | `...` → **Send a Copy** → *Send Notes to Listen*. If the shortcut doesn't appear, `...` → **Print**, pinch outward on the preview to open it as a PDF, then share. |
| Voice Memos | select the recording → `...` → **Share** → *Send Audio to Listen* |

### Attaching companion ink to a lecture

1. On the iPad, open `http://100.69.173.35:8000` in Safari.
2. Find the lecture's card in the list.
3. Tap **Add notes from file** (`ocrNotes`, index.html:1042) and pick the
   exported PDF or a photo of the page.
4. `/ocr` returns markdown into the notes box. **Read it before saving** — this
   is the review step, and handwriting transcription is where it earns its keep.
5. Save. `/label/{rid}` re-runs `analyze` and the summary absorbs the notes,
   weighted toward whatever you flagged.

For a lecture already finished and filed, the same button appears next to the
correction box and routes to `/addendum` instead — a dated append that survives
note rewrites.

### What to expect

`/upload` inserts the row and returns `{"id": "..."}` immediately, then works on
a background thread. The notification fires in about a second regardless of how
long the lecture is — it confirms the upload landed, not that transcription
finished. Watch the listen UI for that.

If the mesh is down or the server isn't running, Shortcuts throws its own
connection error dialog. There is no silent-failure case.

### On the hardcoded filename

The `filename` query param only supplies the note title, and for audio a
harmless `.webm` rename — ffmpeg sniffs the real container from content (see
the ponytail comment at app.py:322). Claude renames the note during
`process_pdf` anyway, so the placeholder rarely survives.

To pass the real name instead: insert a **Get Details of Files** action
(property **Name**) before the request, then build the URL with a **Text**
action containing
`http://100.69.173.35:8000/upload?kind=pdf&filename=` followed by that Name
variable, and feed that Text into the URL field. Skip it until the placeholder
titles actually annoy you.

## Step 3: which notes app

The integration point is the share sheet and the file picker, not the app's
storage format. Anything that exports PDF works. So pick on two things: Pencil
feel, and how cleanly it exports **a few selected pages**.

That second criterion is what decides it. Companion ink for one lecture is two
or three pages out of a running notebook, and you need exactly those pages —
not the whole notebook, not one endless scroll.

- **GoodNotes** — start here. Selected-page PDF export is the deciding feature,
  ink latency is the best of the bunch, and folders per class mirror the vault.
  Paid.
- **Apple Notes** — free and installed, and fine if you keep one note per
  lecture. But it exports the whole note, so the discipline has to come from
  you. Reasonable way to try the pipeline before paying for anything.
- **Notability** — records audio synced to ink, which is tempting, but that
  means recording on the iPad. See Step 4.
- **Notion, Joplin** — no serious Pencil inking, and both become a second silo
  you still have to export from. Skip.
- **Obsidian + Excalidraw** — ink lands in the vault directly, which sounds
  ideal for diagrams specifically. But Excalidraw is an infinite drawing canvas
  rather than a notebook, and it's slow to drive mid-lecture. Possible later for
  redrawing a diagram cleanly after the fact; wrong tool for live capture.

## Step 4: audio, and the Pencil noise problem

This matters more here than in a normal setup. Because the ink is sparse and
diagram-heavy by design, the audio is carrying most of the content. A degraded
transcript isn't a partial loss — it's the loss of everything the notes
deliberately didn't duplicate.

Recording on the same iPad you're writing on is a real risk, not a theoretical
one. Pencil taps are structure-borne: they travel through the chassis into
mics sitting inches away, while the lecturer is meters off. Whisper handles
broadband transients badly — they produce hallucinated text, not just dropouts.
A matte or Paperlike screen protector makes it worse, not better.

Options, best first:

1. **Record on the iPhone, write on the iPad.** Separate device, problem gone.
   Given how much the audio is carrying, treat this as the default rather than
   one option among three.
2. **Record on the iPad but prop it in a stand** instead of flat under your
   writing hand. Partial mitigation.
3. **Record on the iPad and accept it.** Test one lecture first: ten minutes of
   writing while recording, upload it, read the transcript before committing a
   semester to it.

Storage on the iPhone is not a constraint. Check
`Settings > Voice Memos > Audio Quality` reads **Compressed** — a 2-hour
lecture is then roughly 30–60 MB (AAC mono). On **Lossless** the same lecture
is 600 MB to 1.2 GB. Upload and delete after each lecture and the peak
footprint is one file.

Voice Memos also syncs to iCloud by default. If iCloud is tight,
`Settings > [name] > iCloud > Voice Memos` turns it off.

## When the laptop has to be on

Transcription is local `faster_whisper` (app.py:137–161, CUDA with a CPU
fallback), not a hosted API. So:

| Moment | Laptop |
|---|---|
| Lecture — recording, writing | Off is fine. Everything sits on the iPad/iPhone. |
| Upload, transcribe, file to vault | Must be on. Whisper decodes locally; Claude and Supabase calls go out from the laptop process. |
| Reading vault notes on the iPad | Off is fine if the vault is synced. On if browsing through the listen UI. |

Nothing expires while it waits. Batch the uploads when you get home.

If the mesh is down, AirDrop the files to the laptop and use the existing
upload buttons in the listen UI. Meshnet's own file transfer works as a
backup too.

## Do not hand-edit topic notes on the iPad

`_refile_group` (app.py:2238) rebuilds each combined topic note from the DB:

```python
dest.write_text(_note_md(group), encoding="utf-8")
```

That's a full overwrite. Any edit made to a topic `.md` in Obsidian on the
iPad is destroyed the next time that topic group refiles — which happens on
merge, split, delete, label change, or a new recording landing on the topic.
It fails silently. You lose the text with no error.

Safe ways to add text from the iPad:

- **`POST /addendum/{rid}`** — via the listen UI's correction box. Persists in
  the DB `addendum` column and is re-emitted on every rewrite.
- **The notes box** before a recording is filed.
- **New files** you create in the vault yourself, anywhere the app doesn't
  generate. The app only rewrites paths it owns.

## Obsidian on the iPad: Sync or not

The vault is already a private git repo, `NoMoreWontons/college-lectures`.

- **Read-only on the iPad** — clone the repo with Working Copy (free) and open
  it in Obsidian iOS. Enough for reading notes and timelines. Obsidian Sync
  buys nothing here.
- **Read and write** — Obsidian Sync's price is really paying for conflict
  merging, since `obsidian-git` doesn't run on iOS. Worth it only if you're
  editing on the iPad regularly, and even then the clobber rule above still
  applies to topic notes.

Start read-only. Upgrade if it chafes.

## Resulting flow

| Input | Path | Taps |
|---|---|---|
| Handwritten notes, standalone | Export PDF → share → "Send Notes to Listen" → auto-labeled and filed | 2 |
| Handwritten notes, for an existing lecture | listen UI in Safari → the recording's "Add notes from file" → review → save | 4, with review |
| iPhone/iPad audio | Voice Memos → share → "Send Audio to Listen" | 2 |
| Correction to a filed note | listen UI → correction box → `/addendum` | 3 |

## Open

- Neither Shortcut has been built or run. The endpoint contracts above are read
  from source, not exercised from an iPad.
- The Pencil-noise test (option 3 in Step 4) hasn't been done.
- The mesh bind is set but has never been hit from the iPad.

### The diagram gap — unsolved

`/ocr` (app.py:539) reads the request body, sends it to Claude, and returns
markdown. It never writes the image to disk. Grep the vault: the only `![[...]]`
embeds `app.py` produces are the Timeline bases at 2095 and 2099. **No image has
ever been written into the vault.**

For prose notes that's fine. For a free-body diagram it is not: Claude's
description replaces the drawing, the drawing is discarded, and there is no
artifact to go back to. Given that diagrams are a main reason these notes exist
at all, this is the real gap in the workflow.

Minimum fix, if it's worth building: write the uploaded image into the vault
beside the note and emit an `![[...]]` line so Obsidian renders it — keeping
both the picture and Claude's reading of it.

The design constraint that makes this non-trivial: `_refile_group` rebuilds the
note with a wholesale `dest.write_text(_note_md(group))`, so the embed line must
be **DB-derived** like everything else in that note. A new column, or reuse of
`addendum`. A hand-added line in the file gets eaten on the next refile.

Not built. Not scoped beyond this paragraph.
