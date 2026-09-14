// Stub-DOM check for the merge panel's From/To selects: option text must carry
// only unit(/topic) with the class on the optgroup, and mergeFromChanged must
// still see every option through select.options despite the optgroup nesting.
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

class Opt { constructor(v, t, g) { this.value = v; this.text = t; this.group = g; } }
const parseOptions = html => {
  const out = [];
  let group = null;
  const re = /<optgroup label="([^"]*)">|<\/optgroup>|<option value="([^"]*)">([^<]*)<\/option>/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[1] !== undefined) group = m[1];
    else if (m[0] === '</optgroup>') group = null;
    else out.push(new Opt(m[2], m[3], group));
  }
  return out;
};
const els = {};
const mk = id => (els[id] ??= {
  id, innerHTML: '', value: '', hidden: true,
  get options() { return parseOptions(this.innerHTML); },
  contains: () => false, addEventListener() {},
});
mk('merge'); mk('mergeFrom'); mk('mergeTo');
global.document = { getElementById: mk, activeElement: null };
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.esc = esc; global.escAttr = esc;
let mergeLevel = 'topic';

// mergeSuggestions leans on module-level consts; eval scopes const/let to itself,
// so hoist that block onto global before eval'ing the functions that read it.
const consts = src.slice(src.indexOf('const MERGE_BAR'), src.indexOf('function mergeSuggestions'));
eval(consts.replace(/^const /gm, 'global.'));
eval(grab('mergeSuggestions') + ';' + grab('renderMerge') + ';' + grab('mergeFromChanged'));

// renderMerge writes the whole panel into #merge; a real browser then parses the
// nested <select id="mergeFrom"> into its own element. Do that by hand.
const render = rows => {
  renderMerge(rows);
  const h = els.merge.innerHTML;
  const i = h.indexOf('<select id="mergeFrom"');
  els.mergeFrom.innerHTML = i < 0 ? '' : h.slice(h.indexOf('>', i) + 1, h.indexOf('</select>', i));
};

const ok = [];
const check = (name, cond, extra) => {
  ok.push(cond);
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (!cond && extra ? ' -- ' + extra : ''));
};

const LONG = 'ENGR 1000 Orientation to Engineering';
const rows = [
  { status: 'done', semester: 'Fall 26', class: LONG, unit: 'Syllabus', topic: 'Course syllabus and policies' },
  { status: 'done', semester: 'Fall 26', class: LONG, unit: 'Syllabus', topic: 'Grading and attendance policy' },
  { status: 'done', semester: 'Fall 26', class: 'MATH 2110Q Multivariable Calculus', unit: 'Vectors', topic: 'Dot product' },
];
render(rows);

const from = els.mergeFrom;
const opts = from.options;
check('all 3 options reachable through select.options', opts.length === 3, 'got ' + opts.length);
check('class name is NOT repeated in option text', opts.every(o => !o.text.includes(LONG)),
  JSON.stringify(opts.map(o => o.text)));
check('option text is "unit / topic"', opts.some(o => o.text === 'Syllabus / Course syllabus and policies'),
  JSON.stringify(opts.map(o => o.text)));
check('class rides on the optgroup label', opts.some(o => o.group === LONG),
  JSON.stringify(opts.map(o => o.group)));
check('option value still carries the full key', opts.length > 0 && opts.every(o => o.value.split('||').length === 4),
  JSON.stringify(opts.map(o => o.value)));

// To must stay scoped to same-class siblings, across the optgroup split
const pick = opts.find(o => o.text.indexOf('Syllabus /') === 0);
if (pick) {
  from.value = pick.value;
  mergeFromChanged();
  const to = els.mergeTo.options;
  check('To lists only same-class siblings', to.length === 1, JSON.stringify(to.map(o => o.text)));
  check('To excludes the other class', to.every(o => !o.value.includes('MATH')),
    JSON.stringify(to.map(o => o.value)));
} else {
  check('To scoping (skipped: no From option)', false);
}

// unit level: one key segment, bare unit name, no topic
mergeLevel = 'unit';
render(rows.concat([{ status: 'done', semester: 'Fall 26', class: LONG, unit: 'Projects', topic: 'x' }]));
const u = els.mergeFrom.options;
check('unit level option text is the bare unit',
  u.some(o => o.text === 'Syllabus') && u.every(o => o.text.indexOf('/') < 0),
  JSON.stringify(u.map(o => o.text)));

// ---- topic suggestions score the summaries, not the names ----
// An intro lecture that mentions a topic in passing and the lecture that covers it
// properly: different titles (the old name-overlap rule saw nothing) and wildly
// different lengths (Jaccard scores them near zero). Containment on the SHORTER
// side is what catches it, and it also says which way the merge goes.
const CLS = 'PHYS 1201 Mechanics';
const deep = 'Newton second law relates acceleration to the net force on a body. '
  + 'Free body diagrams isolate each force: gravity, normal, friction, tension. '
  + 'Solving with vector components along inclined surfaces, friction coefficient, '
  + 'equilibrium conditions, worked pulley and incline problems, tension in cables.';
const sRows = [
  { status: 'done', semester: 'Fall 26', class: CLS, unit: 'Intro', topic: 'Welcome to the course',
    summary: 'Course overview. We will touch on force, acceleration and friction later.' },
  { status: 'done', semester: 'Fall 26', class: CLS, unit: 'Dynamics', topic: 'Newton laws', summary: deep },
  { status: 'done', semester: 'Fall 26', class: CLS, unit: 'Waves', topic: 'Standing waves',
    summary: 'Wavelength, frequency, nodes and antinodes on a fixed string, harmonic series.' },
  { status: 'done', semester: 'Fall 26', class: CLS, unit: 'Optics', topic: 'Refraction',
    summary: 'Snell law, index of refraction, total internal reflection, prisms and lenses.' },
];
const sugg = mergeSuggestions(sRows, 'topic');
const hit = sugg.find(x => x.a === 'Intro||Welcome to the course');
check('thin intro is suggested against the in-depth lecture', !!hit,
  JSON.stringify(sugg.map(x => x.a + ' -> ' + x.b)));
check('direction is thin -> deep', !!hit && hit.b === 'Dynamics||Newton laws', hit && hit.b);
check('unrelated topics are not suggested',
  !sugg.some(x => x.a.includes('Standing waves') || x.b.includes('Standing waves')),
  JSON.stringify(sugg.map(x => x.a + ' -> ' + x.b)));

// a topic spanning two recordings is one candidate, not two identical suggestions
const dup = mergeSuggestions(sRows.concat([{ ...sRows[1], summary: deep }]), 'topic');
check('per-topic aggregation, not per-row', dup.length === sugg.length,
  dup.length + ' vs ' + sugg.length);

// A shared title word must not outrank real content overlap: content scores bunch
// near 1.0, so a bonus added to the score made every rename-dupe win the cap.
const rank = [];
for (let n = 1; n <= 5; n++) rank.push(
  { status: 'done', semester: 'F', class: 'C', unit: 'U' + n, topic: 'Thin' + n, summary: `t${n}one t${n}two t${n}three` },
  { status: 'done', semester: 'F', class: 'C', unit: 'U' + n, topic: 'Deep' + n,
    summary: `t${n}one t${n}two t${n}three extra${n}a extra${n}b extra${n}c extra${n}d` });
rank.push({ status: 'done', semester: 'F', class: 'C', unit: 'X', topic: 'Vector operations', summary: 'vecterm vecmore vecthird' },
          { status: 'done', semester: 'F', class: 'C', unit: 'Y', topic: 'Vector operation', summary: 'vecterm vecmore vecthird andmore andagain andthird' });
const ranked = mergeSuggestions(rank, 'topic');
check('suggestions capped', ranked.length === 4, String(ranked.length));
check('name-dupe does not outrank content pairs', ranked[0].a !== 'X||Vector operations',
  ranked.map(x => x.a + ' @' + x.score).join(', '));

process.exit(ok.every(Boolean) ? 0 : 1);
