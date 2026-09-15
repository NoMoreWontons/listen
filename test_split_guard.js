// Stub-DOM check for splitCard: a proposal that hasn't arrived must not render the
// Split/Keep buttons. "Keep as one" folds the split irreversibly, and a page whose
// pending_segments came from a different server version (it left the poll in
// 4942296) renders an empty list -- which is what produced "Split into 0 notes".
const fs = require('fs');
const src = fs.readFileSync('C:/Users/savag/.aiWorkspace/listen/index.html', 'utf8');

const grab = name => {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found');
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1);
  }
  throw new Error(name + ' unbalanced');
};

const esc = s => String(s).replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
const splitCard = eval('(' + grab('splitCard') + ')');

const assert = (cond, msg) => { if (!cond) throw new Error('FAIL: ' + msg); console.log('ok -', msg); };

// no proposal in hand: no choice offered at all
for (const missing of [undefined, null, []]) {
  const html = splitCard({ id: 'r1', pending_segments: missing });
  assert(!/<button/.test(html), `no buttons when pending_segments is ${JSON.stringify(missing)}`);
  assert(/loading the proposed split/.test(html), 'says the proposal is still loading');
}

// proposal in hand: both choices, counted right
const html = splitCard({ id: 'r1', pending_segments: [
  { topic: 'Functions of two variables', class: 'MATH 2110Q', unit: 'Functions of Several Variables', summary: 'a'.repeat(200) },
  { topic: 'Partial derivatives', class: 'MATH 2110Q', unit: 'Functions of Several Variables', summary: 'b' },
] });
assert(/Split into 2 notes/.test(html), 'button counts the proposed segments');
assert(/Keep as one/.test(html), 'keep-as-one offered once the segments are visible');
assert(/Partial derivatives/.test(html), 'each segment previewed');
assert(html.includes('a'.repeat(120) + '…'), 'long summaries truncated with an ellipsis');

console.log('\nsplit guard checks passed');
