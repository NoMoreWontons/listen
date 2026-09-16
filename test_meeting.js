// Meeting mode records the share's own audio track and nothing else. Stopping
// only the recorded stream would leave the capture's video track live -- Chrome
// keeps the "sharing this tab" bar up after you hit Stop. The second half checks
// peakLevel, which is how a share with system audio left off gets caught.
// ponytail: pulls the function out of index.html by regex instead of a build step / module split.
// Run: node test_meeting.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const m = src.match(/async function beginRec\(stream, extra = \[\]\) \{[\s\S]*?\n\}/);
assert(m, 'beginRec not found in index.html');
const beginRec = eval('(' + m[0].replace('async function beginRec', 'async function') + ')');

let recorder;
globalThis.fetch = async () => ({ json: async () => ({ id: 'rid-1' }) });
globalThis.MediaRecorder = class { constructor() { recorder = this; } start() {} };
globalThis.startMeters = () => {};
globalThis.refresh = () => {};
const stub = () => ({ classList: { add() {}, remove() {} } });
globalThis.btn = stub(); globalThis.pauseBtn = stub(); globalThis.meetBtn = stub();

const track = name => ({ name, stopped: false, stop() { this.stopped = true; } });
const streamOf = tracks => ({ getTracks: () => tracks });

async function run(mixed, extra) {
  await beginRec(streamOf(mixed), extra);
  recorder.onstop();
  return [...mixed, ...(extra || [])];
}

(async () => {
  // meeting: the share's audio track is what's recorded, its video track rides along
  const out = track('shareAudio'), tabVid = track('shareVideo');
  for (const t of await run([out], [out, tabVid])) {
    assert(t.stopped, t.name + ' left running after stop');
  }
  // plain mic recording still stops its own track with no extra list
  const solo = track('mic-only');
  for (const t of await run([solo])) assert(t.stopped, t.name + ' left running after stop');

  assert.strictEqual(globalThis.recording, true, 'beginRec must flag the session recording');

  // A share with "Also share system audio" off still yields an audio track, so
  // silence is the only tell -- peakLevel is what watchShareAudio decides on.
  const pm = src.match(/function peakLevel\(buf\) \{[\s\S]*?\n\}/);
  assert(pm, 'peakLevel not found in index.html');
  const peakLevel = eval('(' + pm[0].replace('function peakLevel', 'function') + ')');
  const SILENT_PEAK = Number(src.match(/const SILENT_PEAK = (\d+)/)[1]);

  const silent = new Uint8Array(2048).fill(128);       // what a muted share sends
  assert.strictEqual(peakLevel(silent), 0, 'flat 128 frame must read as silence');
  assert(peakLevel(silent) < SILENT_PEAK, 'silence must trip the warning');

  const speech = Uint8Array.from({ length: 2048 },
    (_, i) => 128 + Math.round(40 * Math.sin(i / 8)));  // audible, ~ -10 dBFS
  assert.strictEqual(peakLevel(speech), 40, 'peak is the largest swing from 128');
  assert(peakLevel(speech) >= SILENT_PEAK, 'real audio must not warn');

  const oneBlip = new Uint8Array(2048).fill(128); oneBlip[7] = 128 - 9;
  assert.strictEqual(peakLevel(oneBlip), 9, 'a negative swing counts as loudly as a positive one');

  console.log('test_meeting.js OK');
})();
