// Label datalists are scoped to the row: units belong to a class, topics to a unit.
// ponytail: pulls the function out of index.html by regex instead of a build step / module split.
// Run: node test_scope.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = re => { const m = src.match(re); assert(m, re + ' not found in index.html'); return m[0]; };
const scopeVals = eval(
  grab(/const PARENTS = \{[^\n]*\}/) + ';\n' +
  grab(/const norm = v => [^\n]*/) + ';\n' +
  '(' + grab(/function scopeVals\(rows, field, sel, tree\) \{[\s\S]*?\n\}/).replace('function scopeVals', 'function') + ')');

const rows = [
  { semester: 'F25', class: 'Calculus', unit: 'U1', topic: 'Chain Rule' },
  { semester: 'F25', class: 'calculus', unit: 'U2', topic: 'Integrals' },   // case drift is real
  { semester: 'F25', class: 'Physics',  unit: 'Waves', topic: 'Doppler' },
  { semester: 'S26', class: 'Calculus', unit: 'U9', topic: 'Series' },
];
const tree = { F25: ['Calculus', 'Physics', 'Latin'], S26: ['Calculus'] };
const at = (field, sel) => scopeVals(rows, field, sel, tree);

// the bug: picking a class must not still offer every other class's units
assert.deepStrictEqual(at('unit', { semester: 'F25', class: 'Calculus' }), ['U1', 'U2']);
assert.deepStrictEqual(at('unit', { semester: 'F25', class: 'Physics' }), ['Waves']);
// case-insensitive, or a lowercase class would blank the list instead of scoping it
assert.deepStrictEqual(at('unit', { semester: 'f25', class: 'CALCULUS' }), ['U1', 'U2']);
// semester is a parent too: same class name, different term
assert.deepStrictEqual(at('unit', { semester: 'S26', class: 'Calculus' }), ['U9']);
// topics scope to the unit, not just the class
assert.deepStrictEqual(at('topic', { semester: 'F25', class: 'Calculus', unit: 'U2' }), ['Integrals']);
// blank parent = no filter, the old union behaviour
assert.deepStrictEqual(at('unit', {}), ['U1', 'U2', 'U9', 'Waves']);
assert.deepStrictEqual(at('unit', { class: 'Calculus' }), ['U1', 'U2', 'U9']);
// a class with no recordings yet has no units -- empty is the honest answer, not "show everything"
assert.deepStrictEqual(at('unit', { semester: 'F25', class: 'Latin' }), []);
// classes come from the vault too, scoped to the semester, matched case-insensitively
assert.deepStrictEqual(at('class', { semester: 'f25' }), ['Calculus', 'Latin', 'Physics', 'calculus']);
assert.deepStrictEqual(at('class', {}), ['Calculus', 'Latin', 'Physics', 'calculus']);
// semester has no parents: always the full list
assert.deepStrictEqual(at('semester', { class: 'Physics' }), ['F25', 'S26']);

console.log('ok');
