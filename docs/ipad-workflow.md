# iPad + Apple Pencil into the listen vault

How handwritten notes and off-laptop audio reach the vault at
`C:\Users\savag\College Lectures`. Written 2026-08-25.

The short version: nothing needs building in `app.py`. The intake endpoints
already exist. What's missing is a way for the iPad to reach the server and a
share-sheet Shortcut to hit it in one tap.

## What already exists

| Endpoint | app.py | What it does |
|---|---|---|
| `POST /upload?kind=pdf` | 309 → 892 | PDF straight to Claude as a native document block (scanned and handwritten pages included, no OCR dependency). Auto-labels semester/class/unit/topic, summarizes, calls `write_note`. Files itself with zero interaction. |
| `POST /upload?kind=audio` | 309 | Raw audio body into the whisper pipeline, same as a live recording. |
| `POST /ocr` | 539 | Photo/PDF/pptx/docx → markdown, returned as text for the notes box. Human reviews before saving. Wired to the "Add notes from file" button in `index.html`. |
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

4. On the iPad, open `http://100.69.173.35:8000`. The full listen UI loads in Safari.

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

- **GoodNotes**: open the notebook, **Share** → **Export** → **PDF** → pick the
  pages for this lecture → **Share** → tap *Send Notes to Listen*.
- **Apple Notes**: `...` menu → **Send a Copy** → tap *Send Notes to Listen*.
  If the shortcut doesn't appear, use `...` → **Print**, pinch outward on the
  page preview to open it as a PDF, then share from there.
- **Voice Memos**: select the recording → `...` → **Share** → tap
  *Send Audio to Listen*.

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

The integration point is the share sheet, not the app's storage format —
`/upload?kind=pdf` takes whatever exports a PDF. So pick on Pencil feel alone.

- **Apple Notes** — free, installed, share sheet is right there, handwriting
  search works. Start here.
- **GoodNotes / Notability** — better ink, paid. Move to one of these only if
  Notes chafes.
- **Notion, Joplin** — no real Pencil inking, and both would be a second silo
  that still needs exporting. Skip.
- **Obsidian + Excalidraw plugin** — ink lands inside the vault directly, which
  sounds ideal, but Excalidraw is a drawing canvas rather than a notebook. Poor
  fit for fifty minutes of fast lecture writing.

## Step 4: audio, and the Pencil noise problem

Recording on the same iPad you're writing on is a real risk, not a theoretical
one. Pencil taps are structure-borne: they travel through the chassis into
mics sitting inches away, while the lecturer is meters off. Whisper handles
broadband transients badly — they produce hallucinated text, not just dropouts.
A matte or Paperlike screen protector makes it worse, not better.

Options, best first:

1. **Record on the iPhone, write on the iPad.** Separate device, problem gone.
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
