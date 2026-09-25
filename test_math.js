// marked runs before katex, and markdown eats LaTeX: \, \{ \\ are CommonMark
// escapes (the backslash is dropped) and $z_a$ ... $z_b$ pairs into <em>. These
// check md() hands every math span to katex byte-for-byte.
// ponytail: pulls the pieces out of index.html by regex, same as test_svg.js.
// The stub marked does exactly the two mangles above -- the real one does more.
// Run: node test_math.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = re => { const m = src.match(re); assert(m, re + ' not found in index.html'); return m[0]; };

global.window = { marked: true };
global.marked = {
  // CommonMark: a backslash before ANY ascii punctuation is an escape
  parse: s => '<p>' + s.replace(/\\([!-\/:-@\[-`{-~])/g, '$1').replace(/_([^_\n]+)_/g, '<em>$1</em>') + '</p>',
};
eval(grab(/function esc\(s\) \{[^\n]*/) + '\n' + grab(/function md\(s\) \{[\s\S]*?\n\}/));

// --- the 2026-09-25 Exponential/Gamma note: \, became a literal comma --------
const gamma = '$\\Gamma(\\alpha) = \\int_0^{\\infty} x^{\\alpha-1}e^{-x}\\,dx$';
assert(md('- ' + gamma).includes(gamma), md('- ' + gamma));

// --- two subscripted spans on one line must not pair into <em> ---------------
const line = 'Standardize with $z_a$ and $z_b$ then take $\\Phi(z_b) - \\Phi(z_a)$';
const out = md(line);
assert(!out.includes('<em>'), 'math underscores became emphasis: ' + out);
assert(out.includes('$\\Phi(z_b) - \\Phi(z_a)$'), out);

// --- display math: cases line breaks and escaped braces survive -------------
const cases = '$$F(x) = \\begin{cases} 0 & x < a \\\\ 1 & x \\ge a \\end{cases}$$';
assert(md(cases).includes(esc(cases)),
  'cases \\\\ or & < lost: ' + md(cases));
const set = '$y \\not\\in \\{1, 2, 3\\}$';
assert(md(set).includes(set), md(set));

// --- an escaped \$ is a dollar sign, never a math delimiter ------------------
const rent = 'Optimal rent: $P = 800 + 10(10) = \\$900$ per month';
assert(md(rent).includes('$P = 800 + 10(10) = \\$900$'), md(rent));
const prose = 'rent is \\$800 and $x_1$ plus \\$10 and $x_2$';
assert(!md(prose).includes('<em>') && md(prose).includes('$x_1$'), md(prose));
assert(md(prose).includes('\\$800') && md(prose).includes('\\$10'), 'escaped dollar lost its backslash: ' + md(prose));

// --- math is still HTML-escaped on the way back in ---------------------------
assert(!md('$x <b>y</b>$').includes('<b>'), 'raw HTML leaked through a math span');

// --- prose outside math is still markdown, and mermaid fences are untouched ---
assert(md('a _b_ c').includes('<em>b</em>'), 'prose emphasis should still render');
const fence = '```mermaid\nA["cost $5"] --> B["$x_1$ and $x_2$"]\n```';
assert(md(fence).includes('<em>'), 'fenced code must go to marked unparked: ' + md(fence));

console.log('test_math: ok');
