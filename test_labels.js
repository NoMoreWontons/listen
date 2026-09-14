// /recordings used to return every recording on every tick, so the poll grew with
// the library forever. It now returns a page of cards, and the whole-library label
// set the tree/merge/scope panels need comes from /labels -- fetched once and kept.
// These check the keep-and-refetch rule, since a cache that never refetches shows a
// stale tree and one that always refetches puts the growth right back.
// ponytail: pulls the function out of index.html by regex, same as test_wavejank.js.
// Run: node test_labels.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = re => { const m = src.match(re); assert(m, re + ' not found in index.html'); return m[0]; };

let fetches = 0;
let served = [];
global.fetch = url => {
  assert.strictEqual(url, '/labels');
  fetches++;
  return Promise.resolve({ ok: true, json: () => Promise.resolve(served) });
};

eval(grab(/let labelRows = \[\];[\s\S]*?\nlet labelsStale = true;/).replace(/^let /gm, 'global.')
   + '\n' + grab(/const LABEL_KEYS = [^\n]*/).replace('const ', 'global.')
   + '\n' + grab(/async function hydrateLabels\(rows\) \{[\s\S]*?\n\}/));

const lab = (id, over = {}) => ({ id, created_at: '2026-09-0' + id, status: 'done',
  semester: 'Fall 26', class: 'Physics', unit: 'u1', topic: 't' + id, title: 'lecture ' + id,
  obsidian_uri: 'obsidian://' + id, ...over });

(async () => {
  // --- first call fetches; a second call with the same rows must not -------------
  served = [lab('3'), lab('2'), lab('1')];
  let all = await hydrateLabels([lab('3')]);
  assert.strictEqual(fetches, 1, 'the first refresh fetches the label set');
  assert.deepStrictEqual(all.map(r => r.id), ['3', '2', '1'], 'newest first, whole library');

  all = await hydrateLabels([lab('3')]);
  assert.strictEqual(fetches, 1, 'an unchanged library must not refetch every tick');
  assert.strictEqual(all.length, 3, 'the panels still see rows the card page dropped');

  // --- a label that moves invalidates it ----------------------------------------
  await hydrateLabels([lab('3', { topic: 'renamed' })]);   // drift spotted -> stale
  served = [lab('3', { topic: 'renamed' }), lab('2'), lab('1')];
  await hydrateLabels([lab('3', { topic: 'renamed' })]);   // ...so this one refetches
  assert.strictEqual(fetches, 2, 'a relabelled row must refresh the tree');
  await hydrateLabels([lab('3', { topic: 'renamed' })]);
  assert.strictEqual(fetches, 2, 'and then settle again');

  // --- so does a status change, which is how a finished recording gets filed -----
  await hydrateLabels([lab('3', { topic: 'renamed', status: 'transcribing' })]);
  served = [lab('4'), lab('3', { topic: 'renamed' }), lab('2'), lab('1')];
  await hydrateLabels([lab('3', { topic: 'renamed' })]);
  assert.strictEqual(fetches, 3, 'a status change must refresh the tree');

  // --- a row nobody has seen before does too -------------------------------------
  const before = fetches;
  await hydrateLabels([lab('9')]);              // id 9 is in no label set yet
  served = [lab('9'), ...served];
  await hydrateLabels([lab('9')]);
  assert.strictEqual(fetches, before + 1, 'a brand new recording must refresh the tree');

  // --- the live row wins over the cached copy for the ids it covers ---------------
  const merged = await hydrateLabels([lab('9', { status: 'transcribing' })]);
  assert.strictEqual(merged.find(r => r.id === '9').status, 'transcribing',
    'the page of live rows is this tick&apos;s truth, not the kept label copy');

  // --- a failed /labels keeps the old tree rather than blanking it ----------------
  global.fetch = () => Promise.reject(new Error('offline'));
  labelsStale = true;
  const kept = await hydrateLabels([lab('9')]);
  assert(kept.length > 1, 'a failed label fetch must not blank the tree');

  console.log('ok');
})();
