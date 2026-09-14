// Meeting mode records the mixer's output, not the captures feeding it. Stopping
// only the recorded stream would leave the tab capture and the mic live -- Chrome
// keeps the "sharing this tab" bar up and the mic light on after you hit Stop.
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
  // meeting: one mixed output track out, tab + mic captures behind it
  const out = track('mix'), tab = track('tab'), tabVid = track('tabVideo'), mic = track('mic');
  for (const t of await run([out], [tab, tabVid, mic])) {
    assert(t.stopped, t.name + ' left running after stop');
  }
  // plain mic recording still stops its own track with no extra list
  const solo = track('mic-only');
  for (const t of await run([solo])) assert(t.stopped, t.name + ' left running after stop');

  assert.strictEqual(globalThis.recording, true, 'beginRec must flag the session recording');
  console.log('test_meeting.js OK');
})();
