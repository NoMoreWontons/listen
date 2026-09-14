// postFile used to reject on a dead connection. Every caller wraps it in
// try/finally with no catch, so the rejection unwound past them, the button
// reset to its idle label, and a failed upload looked exactly like a clean one
// -- the iPad case, where the server is busy transcribing and Safari drops the
// request when the tab backgrounds. It must resolve {error} instead, and a
// non-2xx must not read as success (FastAPI's 500 is valid JSON).
// ponytail: pulls postFile out of index.html by regex, same as test_clip.js.
// Run: node test_postfile.js
const fs = require('fs'), assert = require('assert');
const src = fs.readFileSync(__dirname + '/index.html', 'utf8');
const m = src.match(/function postFile\(url, file, onProgress, onDone\) \{[\s\S]*?\n\}/);
assert(m, 'postFile not found in index.html');

let xhr;
global.XMLHttpRequest = function () {
  xhr = this;
  this.upload = {};
  this.open = () => {};
  this.send = () => {};
};
const postFile = eval('(' + m[0] + ')');

const run = drive => {
  const p = postFile('/ocr', 'file', () => {}, () => {});
  drive(xhr);
  return p;
};
const ok = r => (assert(!r.error, 'expected success, got ' + JSON.stringify(r)), r);
const failed = r => {
  assert(r && typeof r.error === 'string' && r.error, 'expected an {error}, got ' + JSON.stringify(r));
  return r;
};

(async () => {
  // the reason this exists: a dropped connection must surface, not throw
  failed(await run(x => x.onerror()));
  failed(await run(x => x.onabort()));
  failed(await run(x => x.ontimeout()));

  // FastAPI's default 500 body parses cleanly and has no `error` key
  failed(await run(x => { x.status = 500; x.responseText = '{"detail":"Internal Server Error"}'; x.onload(); }));
  assert((await run(x => { x.status = 500; x.responseText = '{"detail":"boom"}'; x.onload(); })).error.includes('boom'));
  // an {error} the endpoint returned itself still comes through
  assert((await run(x => { x.status = 400; x.responseText = '{"error":"unreadable page"}'; x.onload(); })).error === 'unreadable page');
  // a proxy/tunnel HTML error page isn't JSON at all
  failed(await run(x => { x.status = 502, x.responseText = '<html>Bad Gateway</html>'; x.onload(); }));
  // 200 with a body that isn't JSON is not a success either -- resp.text would be undefined
  failed(await run(x => { x.status = 200; x.responseText = 'not json'; x.onload(); }));

  // a real /ocr reply still passes straight through untouched
  const good = ok(await run(x => { x.status = 200; x.responseText = '{"text":"F = ma","attached":"p-1.png"}'; x.onload(); }));
  assert(good.text === 'F = ma' && good.attached === 'p-1.png', JSON.stringify(good));
  // 204/201 count as success too
  ok(await run(x => { x.status = 201; x.responseText = '{"text":"x"}'; x.onload(); }));

  // ontimeout is dead code unless a timeout is actually set
  assert(/xhr\.timeout = /.test(src), 'postFile must set xhr.timeout or ontimeout never fires');
  postFile('/ocr', 'file', () => {}, () => {});
  assert(xhr.timeout >= 5 * 60 * 1000, 'timeout must clear a slow PDF: ' + xhr.timeout);

  // and no caller may be left relying on a rejection
  const callers = src.match(/await postFile\(/g) || [];
  assert(callers.length >= 3, 'expected postFile to still have its three callers');
  assert(!/xhr\.on(error|abort|timeout) = \(\) => reject/.test(src), 'postFile must not reject');

  console.log('postFile error surfacing OK (' + callers.length + ' callers)');
})();
