// canDrop rules for the library tree: sibling merges + topic -> unit move.
// ponytail: pulls the function out of index.html by regex instead of a build step / module split.
// Run: node test_candrop.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const m = src.match(/function canDrop\(drag, target\) \{[\s\S]*?\n\}/);
assert(m, 'canDrop not found in index.html');
const canDrop = eval('(' + m[0].replace('function canDrop', 'function') + ')');

const K = 'F25||Calculus';
const topic = (unit, t) => ({ kind: 'topic', k: K, name: unit + '||' + t });
const unit = u => ({ kind: 'unit', k: K, name: u });

// topic -> another unit: the move gesture
assert(canDrop(topic('U1', 'Chain Rule'), unit('U2')));
// topic -> its own unit: no-op, backend would reject it
assert(!canDrop(topic('U1', 'Chain Rule'), unit('U1')));
// sibling merges still work
assert(canDrop(topic('U1', 'Chain Rule'), topic('U1', 'Product Rule')));
assert(canDrop(unit('U1'), unit('U2')));
assert(!canDrop(unit('U1'), unit('U1')));
// unit dragged onto a topic is meaningless; other classes are out of scope
assert(!canDrop(unit('U1'), topic('U2', 'Chain Rule')));
assert(!canDrop(topic('U1', 'Chain Rule'), { kind: 'unit', k: 'F25||Physics', name: 'U2' }));
assert(!canDrop(null, unit('U1')));

console.log('ok');
