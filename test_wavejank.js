// The 5s refresh() wipes #list, so every finished lecture's summary -- markdown with
// $math$, mermaid decision trees and inline SVG -- was re-parsed and re-rendered on
// every tick. That burst is what froze the page and stalled the waveform.
// These check fillSummaries() renders each summary exactly once and re-adopts the same
// node afterwards, that /resummarize still repaints, and that deleted rows don't leak.
// ponytail: pulls the function out of index.html by regex, same as test_svg.js.
// Run: node test_wavejank.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = re => { const m = src.match(re); assert(m, re + ' not found in index.html'); return m[0]; };

// --- tiny DOM: slots live in a list, replaceWith swaps them for the summary node ----
let slots = [];
let typesetCalls = [];
global.md = s => '<p>' + s + '</p>';
global.typeset = node => typesetCalls.push(node);
global.document = {
  querySelectorAll: sel => (assert.strictEqual(sel, '#list .sum-slot'), slots.splice(0)),
  createElement: () => ({ innerHTML: '', tag: 'div' }),
};
const slotFor = id => ({ dataset: { sum: id }, replaceWith(n) { this.got = n; } });

// `const` inside eval() stays local to it, so hoist the caches onto global to assert on them
eval(grab(/const summaryCache = new Map\(\);[^\n]*/).replace('const ', 'global.') + '\n'
   + grab(/const audioBoxCache = new Map\(\);[^\n]*/).replace('const ', 'global.') + '\n'
   + grab(/const transcriptCache = new Map\(\);[^\n]*/).replace('const ', 'global.') + '\n'
   + grab(/function fillSummaries\(rows\) \{[\s\S]*?\n\}/));

// --- first render: the summary is parsed and typeset once ---------------------------
const rows = [{ id: 'a', summary: '```mermaid\ngraph TD\n```' }, { id: 'b', summary: '$x^2$' }];
let sa = slotFor('a'), sb = slotFor('b');
slots = [sa, sb];
fillSummaries(rows);
assert.strictEqual(typesetCalls.length, 2, 'both summaries render on the first pass');
assert.strictEqual(sa.got.innerHTML, '<p>```mermaid\ngraph TD\n```</p>');
const firstNodeA = sa.got, firstNodeB = sb.got;

// --- the tick that used to cost a full mermaid layout now costs nothing -------------
// same rows, fresh slots (refresh() rebuilt #list): the SAME nodes come back, untouched.
typesetCalls = [];
sa = slotFor('a'); sb = slotFor('b');
slots = [sa, sb];
fillSummaries(rows);
assert.strictEqual(typesetCalls.length, 0, 'unchanged summaries must not be re-rendered');
assert.strictEqual(sa.got, firstNodeA, 'the already-rendered node is re-adopted, not rebuilt');
assert.strictEqual(sb.got, firstNodeB);

// --- /resummarize changes the text, so that one row repaints and the other does not --
typesetCalls = [];
const resummarized = [{ id: 'a', summary: 'a brand new summary' }, rows[1]];
sa = slotFor('a'); sb = slotFor('b');
slots = [sa, sb];
fillSummaries(resummarized);
assert.strictEqual(typesetCalls.length, 1, 'only the changed summary re-renders');
assert.notStrictEqual(sa.got, firstNodeA, 'new summary text must produce a new node');
assert.strictEqual(sb.got, firstNodeB, 'the untouched row keeps its cached node');

// --- a row that disappears must not be held by the cache forever ---------------------
assert.strictEqual(summaryCache.size, 2);
slots = [];
fillSummaries([resummarized[0]]);          // 'b' deleted
assert.strictEqual(summaryCache.size, 1, 'deleted recordings are evicted');
assert(summaryCache.has('a') && !summaryCache.has('b'));

// --- a slot with no matching row is skipped, not crashed on ---------------------------
const ghost = slotFor('gone');
slots = [ghost];
fillSummaries([resummarized[0]]);
assert.strictEqual(ghost.got, undefined, 'an orphan slot is left alone');

// --- the row template must actually emit a slot, not inline md() ----------------------
const tmpl = grab(/\$\{r\.status === 'done' && r\.summary \?[^\n]*/);
assert(tmpl.includes('sum-slot'), 'row template still inlines the summary: ' + tmpl);
assert(!tmpl.includes('md(r.summary)'), 'row template still calls md() every tick: ' + tmpl);

// --- an open audio box must not refetch /segments on every 5s tick --------------------
{
  let fetches = 0;
  global.fetch = () => { fetches++; return Promise.resolve({ ok: true, json: () => Promise.resolve([]) }); };
  global.esc = s => s; global.fmt = s => String(s);
  eval(grab(/async function loadAudioBox\(details, rid\) \{[\s\S]*?\n\}/));

  // first open: the slot is blank, so it fetches and fills
  const filled = { className: 'audio-slot', dataset: {}, innerHTML: '',
    querySelector: () => ({ onerror: null }), querySelectorAll: () => [] };
  const box = { open: true, querySelector: () => filled };
  return loadAudioBox(box, 'a').then(() => {
    assert.strictEqual(fetches, 1, 'the first open fetches segments');
    assert.strictEqual(audioBoxCache.get('a'), filled, 'the filled slot is cached');

    // refresh() wiped #list: a blank slot comes back, and it must be swapped for the
    // cached node rather than refetching and rebuilding the <audio> element
    const blank = { dataset: {}, replaceWith(n) { this.got = n; } };
    return loadAudioBox({ open: true, querySelector: () => blank }, 'a').then(() => {
      assert.strictEqual(fetches, 1, 'a rebuilt slot must reuse the cache, not refetch');
      assert.strictEqual(blank.got, filled, 'the already-filled node is re-adopted');
      tail();
    });
  });
}

function tail() {
// --- drawWave must not resize the canvas unconditionally every frame ------------------
const draw = grab(/function drawWave\(\) \{[\s\S]*?\n\}/);
assert(!/canvas\.width = canvas\.clientWidth;/.test(draw), 'unconditional per-frame resize is back');
assert(/if \(!w \|\| !h\) return;/.test(draw), 'a 0-width read must bail, not stack every point at x=0');
assert(!/new Uint8Array\(/.test(draw), 'the sample buffer must be allocated once, not per frame');

console.log('ok');
}
