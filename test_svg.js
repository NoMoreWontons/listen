// A raw <svg> figure is the one thing md() does not escape into visible source.
// These check the carve-out is exactly that narrow: the svg comes back out
// intact, everything around it still goes through esc(), and the sanitizer's
// allowlists don't quietly admit a way to load or run something.
// ponytail: pulls the pieces out of index.html by regex, same as test_clip.js.
// Run: node test_svg.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = re => { const m = src.match(re); assert(m, re + ' not found in index.html'); return m[0]; };

global.window = { marked: true };
global.marked = { parse: s => '<p>' + s + '</p>' };
eval(grab(/function esc\(s\) \{[^\n]*/) + '\n' + grab(/function md\(s\) \{[\s\S]*?\n\}/));

// --- the figure survives, its neighbours still get escaped -------------------
const fig = '<svg viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8"/></svg>';
const out = md('before\n\n' + fig + '\n\nafter <b>x</b>');
assert(out.includes('<pre class="svgsrc">'), out);
assert(out.includes('&lt;svg viewBox=&quot;'.replace('&quot;', '"')), out);  // svg source, escaped
assert(out.includes('&lt;b&gt;x'), 'prose HTML must still be inert: ' + out);
assert(!out.includes('<b>x'), 'raw HTML leaked out of the escape path: ' + out);

// two figures in one note each get their own <pre>, and the prose between them
// is still handed to marked rather than swallowed by the split
const two = md(fig + '\nmiddle\n' + fig);
assert(two.match(/<pre class="svgsrc">/g).length === 2, two);
assert(two.includes('<p>\nmiddle\n</p>'), two);

// a note with no figure is untouched -- same single marked.parse(esc()) as before
assert(md('plain **text**') === '<div class="md"><p>plain **text**</p></div>', md('plain **text**'));

// an unclosed <svg> is not a figure; it must stay escaped prose, not open a hole
const bad = md('<svg onload="alert(1)">no closing tag');
assert(!bad.includes('svgsrc'), bad);
assert(bad.includes('&lt;svg onload'), bad);

// --- the sanitizer allowlists -----------------------------------------------
const lift = re => eval('(' + grab(re).replace(/^const \w+ = /, '').replace(/;$/, '') + ')');
const SVG_TAGS = lift(/const SVG_TAGS = new Set\(\[[\s\S]*?\]\);/);
const SVG_ATTRS = lift(/const SVG_ATTRS = new Set\(\[[\s\S]*?\]\);/);

// everything a figure actually needs
for (const t of ['svg', 'rect', 'line', 'circle', 'path', 'polygon', 'text']) assert(SVG_TAGS.has(t), t);
for (const a of ['viewBox', 'd', 'stroke', 'fill', 'transform', 'text-anchor']) assert(SVG_ATTRS.has(a), a);

// and nothing that fetches, runs, or styles: <use>/<image> pull external refs,
// style= smuggles url(), and href/on* are the obvious two
for (const t of ['script', 'foreignObject', 'use', 'image', 'animate', 'a', 'style', 'iframe']) {
  assert(!SVG_TAGS.has(t), 'SVG_TAGS must not allow <' + t + '>');
}
for (const a of ['href', 'xlink:href', 'style', 'onload', 'onclick', 'filter', 'mask', 'clip-path']) {
  assert(!SVG_ATTRS.has(a), 'SVG_ATTRS must not allow ' + a);
}

console.log('ok');
