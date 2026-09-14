// A page attached to a still-transcribing recording used to be invisible: the
// upload button's progress text was wiped by the 5s re-render, and nothing on
// the card ever said a file had landed. These two checks cover both halves.
// ponytail: pulls the pieces out of index.html by regex, same as test_scope.js.
// Run: node test_clip.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = re => { const m = src.match(re); assert(m, re + ' not found in index.html'); return m[0]; };

// --- the persistent chip ----------------------------------------------------
const header = grab(/<b>\$\{esc\(r\.title \|\| ''\)\}<\/b>[^\n]*/);
const card = eval(
  grab(/function esc\(s\) \{[^\n]*/) + '\n' +
  grab(/function escAttr\(s\) \{[^\n]*/) + '\n' +
  '(r => `' + header + '`)');

const base = { id: 'x', title: 'Waves', status: 'transcribing', source: 'local' };
assert(!card(base).includes('clip'), 'no attachments must mean no chip');
assert(!card({ ...base, attachments: [] }).includes('clip'), 'an empty list must not render a chip');

const one = card({ ...base, attachments: ['fbd-a3f9c2e1-1.pdf'] });
assert(one.includes('&#128206; 1 page<'), one);
assert(one.includes('fbd-a3f9c2e1-1.pdf'), 'the filename belongs in the tooltip: ' + one);

const two = card({ ...base, attachments: ['a-1.pdf', 'b-2.png'] });
assert(two.includes('&#128206; 2 pages<'), two);

// the chip rides on /recordings, so it paints on a plain page load too -- not
// only in the tab that did the upload
assert(card({ ...base, status: 'done', attachments: ['a-1.pdf'] }).includes('clip'));

// a filename with a quote in it must not break out of the title attribute
assert(!card({ ...base, attachments: ['we"ird-1.pdf'] }).includes('we"ird'),
  'attachment names go through escAttr');

// --- the re-render gate -----------------------------------------------------
// iOS Safari does not focus a tapped <button>, so refresh()'s activeElement
// guard never held on the iPad: #list was rewritten mid-upload and the button
// holding the progress text was detached before it could show anything.
assert(/if \(!audioPlaying && !uploading\) document\.getElementById\('list'\)\.innerHTML/.test(src),
  '#list rewrite is no longer gated on an in-flight upload');
assert(/^let uploading = 0;$/m.test(src), 'uploading counter is gone');
const ocr = grab(/async function ocrNotes\(rid, btn, sel = '\.lbl-notes'\) \{[\s\S]*?\n\}/);
assert(/uploading\+\+;/.test(ocr) && /uploading--;/.test(ocr), 'the counter must be paired');
assert(ocr.indexOf('uploading--;') > ocr.indexOf('} finally {') ||
  /finally \{\n\s*uploading--;/.test(ocr), 'the decrement must sit in finally, or a failed upload freezes the list');
assert(/if \(!resp\.attached\)/.test(ocr), 'a file that was not stored must say so');
assert(/if \(!ta\)/.test(ocr), 'the label editor can be gone by the time the upload returns');

// Browsing Files takes seconds and the counter only moves once a file is picked,
// so the button captured at render time is normally detached by then -- onchange
// has to re-resolve it before writing any progress to it.
const reresolve = ocr.indexOf('btn = document.querySelector');
assert(reresolve > 0, 'the stale button is never re-resolved against the live DOM');
assert(reresolve < ocr.indexOf('const old = btn.textContent'),
  'old label must be read from the re-resolved button, not the detached one');
assert(reresolve < ocr.indexOf('uploading++'), 're-resolve before the gate closes');
assert(/\.ocr-btn\[data-sel="\$\{sel\}"\]/.test(ocr), 'the re-resolve needs a selector to match on');
assert(/class="ocr-btn" data-sel="\.lbl-notes"/.test(src) &&
  /class="ocr-btn" data-sel="\.lbl-addendum"/.test(src),
  'both ocr buttons must carry the data-sel the re-resolve looks up');

// refresh() bails while focus is inside #list, so focusing the textarea first
// made the repaint a no-op and the chip waited for the next tick.
assert(ocr.indexOf('await refresh()') < ocr.lastIndexOf('.focus()'),
  'repaint before taking focus back, or refresh() returns early and the chip never paints');
assert(ocr.indexOf('blur()') < ocr.indexOf('await refresh()'),
  'blur first: the button still holds focus in #list and would skip the repaint');
assert(/const live = document\.querySelector/.test(ocr),
  'refresh() detaches the textarea -- focus has to go to the rebuilt one');
assert(ocr.indexOf('localStorage.setItem') < ocr.indexOf('await refresh()'),
  'the draft must be stored before the repaint, or the rebuilt textarea comes back empty');

console.log('ok');
