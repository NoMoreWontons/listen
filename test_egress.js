// The 5s poll pulled `transcript` (2 MB across the table) out of Supabase on every
// tick and burned the whole monthly egress quota in an afternoon -- the project got
// restricted (402 exceed_egress_quota) and the server couldn't even boot.
// These checks fail if the heavy column creeps back into the poll.
// Run: node test_egress.js
const fs = require('fs'), assert = require('assert');
const py = fs.readFileSync(__dirname + '/app.py', 'utf8');
const html = fs.readFileSync(__dirname + '/index.html', 'utf8');

// --- server: the list query must stay light ---------------------------------
const sel = py.match(/@app\.get\("\/recordings"\)[\s\S]*?\.execute\(\)/);
assert(sel, '/recordings endpoint not found');
const cols = sel[0].split('.select(')[1].split('.order(')[0];
for (const heavy of ['transcript', 'summary', 'segments'])
  assert(!cols.split('"').filter((_, i) => i % 2).join(',').split(',').includes(heavy),
    `/recordings must not select ${heavy} -- it is lazy-loaded per row`);
assert(py.includes('@app.get("/transcript/{rid}")'), 'lazy /transcript/{rid} endpoint missing');
assert(py.includes('@app.post("/summaries")'), 'batched /summaries endpoint missing');

// --- client: the card must not expect a transcript field ---------------------
const card = html.match(/<b>\$\{esc\(r\.title \|\| ''\)\}<\/b>[\s\S]*?<\/div>`\)\.join\(''\)/);
assert(card, 'list card markup not found');
assert(!/r\.transcript/.test(card[0]), 'card markup must not read r.transcript');
assert(/ontoggle="loadTranscript\(this/.test(card[0]), 'transcript <details> must lazy-load');

// --- client: idle tabs must back off ----------------------------------------
assert(/pollBusy \? 5000 : 30000/.test(html), 'idle poll backoff missing');

// --- client: summaries are fetched once per row, not per poll ----------------
assert(/summaryText\.has\(r\.id\)/.test(html), 'per-row summary cache missing');
assert(/summaryText\.delete\(rid\)/.test(html), 'a re-summarized row must drop its cached summary');

// --- server: writes must not echo the whole row back -------------------------
// PostgREST defaults to Prefer: return=representation, so every progress checkpoint
// used to drag the row's transcript + segments back over the wire for nothing.
// body(src, 'def foo') -> that definition's CODE, up to the next blank-line gap.
// Comment lines are stripped: every check below is about what the code does, and a
// comment mentioning `.limit()` or `returning="minimal"` would otherwise satisfy it.
// Plain indexOf, so these checks don't turn into a regex-escaping exercise.
const body = (src, start, stop = '\n\n\n') => {
  const i = src.indexOf(start);
  assert(i >= 0, start + ' not found');
  const j = src.indexOf(stop, i);
  return src.slice(i, j < 0 ? src.length : j)
    .split('\n').filter(l => !/^\s*(#|\/\/)/.test(l)).join('\n');
};

assert(body(py, 'def _set(rid, **fields):').includes('returning="minimal"'),
  '_set must not ask PostgREST to echo the row back');
// ...but the inserts DO need the row back: /start and /split read .data[0] for the
// new row's id, so a future "make every write minimal" pass must stop at updates.
assert(!/insert\([^)]*returning=/.test(py),
  'an insert must keep return=representation -- /start reads .data[0]["id"] from it');

// --- server: poll-driven reads stay bounded ----------------------------------
for (const [fn, why] of [['def weak_spots(', 'reads whole question+answer blobs'],
                         ['def quizzes(', 'lifetime quiz history']]) {
  assert(body(py, fn).includes('.limit('),
    `${fn}) rides the poll and ${why} -- it needs a .limit()`);
}
assert(/def assignments_open[\s\S]*?select\("id,title,due_on,klass"\)/.test(py),
  'assignments_open rides the poll: select the four columns the dropdown reads, not "*"');
assert(py.includes('@app.get("/cards/due_count")'), 'review badge must poll a count, not the deck');
assert(!/fetch\('\/cards\/due'\)[\s\S]{0,80}renderReview/.test(html), 'renderReview must not fetch the deck');

// --- client: a failed lazy load must not cache itself forever ----------------
for (const fn of ['async function loadTranscript(', 'async function loadAudioBox(',
                  'async function hydrateSummaries(']) {
  assert(body(html, fn, '\n}\n').includes('res.ok'),
    `${fn}) must check res.ok -- an error body was being cached as real data`);
}

// --- server: a deleted row must not raise (PostgREST 406 on .single()) -------
for (const fn of ['def get_transcript(', 'def get_segments(']) {
  assert(!body(py, fn).includes('.single()'),
    `${fn}) must not use .single() -- it raises 406/PGRST116 when the row is gone`);
}
assert(!/select\("status,live_transcript"\)/.test(py),
  'live_preview must not re-read the text it is itself writing');

// --- server: a dead DB must not kill startup --------------------------------
const lifespan = py.match(/async def lifespan\(app\)[\s\S]*?yield/);
assert(/try:[\s\S]*resume_stuck\(\)[\s\S]*except Exception/.test(lifespan[0]),
  'startup housekeeping must be best-effort');

console.log('test_egress: ok');
