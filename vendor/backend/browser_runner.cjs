'use strict';
// Trusted harness: only declarative steps are accepted; generated source is never required here.
const fs = require('node:fs');
const http = require('node:http');
const crypto = require('node:crypto');
const { chromium } = require('/opt/evo-browser/node_modules/playwright');
const spec = JSON.parse(fs.readFileSync('/input/request.json', 'utf8'));
const report = { engine: 'chromium', executed: false, entry: spec.entry, testedFiles: [],
  assertions: [], assertionCount: 0, assertionsPassed: 0, steps: [], console: [], pageErrors: [],
  blockedRequests: [], httpErrors: [], chromiumSandbox: false, pages: [], faults: [], readFaults: [], dialogResponses: [] };
const bounded = value => String(value).slice(0, 1600);
const add = (list, item) => { if (list.length < 50) list.push(item); };
let browser, server, page, currentPageId = 'main';
const canvasSnapshots = new Map();
const pageMap = new Map();
const pageIds = new WeakMap();
const faultEpochs = new Map();
const activeFaults = new Map();
const readFaultEpochs = new Map();
const readFaultTokens = new Map();
const readPageTokens = new Map();
const activeReadFaults = new Map();
const pendingDialogs = new Map();
const dialogJobs = new Set();
async function failureObservation(candidate, step, stepIndex) {
  // Read only a small, actual DOM view from the failing page. This is untrusted
  // application data, not a diagnosis, an instruction, or an acceptance result.
  const view = await candidate.evaluate(() => {
    const clip = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
    const controls = Array.from(document.querySelectorAll('input,select,textarea')).filter(el =>
      !['password', 'hidden', 'file'].includes(el.type) && visible(el));
    // Read actual rendered accessibility feedback, including messages unrelated
    // to the failed selector. These are app-authored strings, never directions.
    const messages = Array.from(document.querySelectorAll('[role="alert"],[role="status"],[aria-live]'))
      .filter(el => visible(el) && !['input', 'select', 'textarea'].includes(el.tagName.toLowerCase())
        && !el.querySelector('input[type="password"],input[type="hidden"],input[type="file"]')
        && (['alert', 'status'].includes(el.getAttribute('role')) || ['polite', 'assertive'].includes(el.getAttribute('aria-live')))
        && clip(el.innerText))
      .map(el => ({ role: ['alert', 'status'].includes(el.getAttribute('role')) ? el.getAttribute('role') : 'live-region',
        id: clip(el.id), text: clip(el.innerText) }))
      .sort((a, b) => Number(b.role === 'alert') - Number(a.role === 'alert'));
    return { runtimeUTC: new Date().toISOString(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      visibleMessages: messages.slice(0, 6), messagesOmitted: Math.max(0, messages.length - 6),
      formValues: controls.slice(0, 10).map(el => ({ id: clip(el.id), name: clip(el.name), type: clip(el.type),
        value: clip(el.value), ...(['checkbox', 'radio'].includes(el.type) ? { checked: el.checked } : {}) })),
      formValuesOmitted: Math.max(0, controls.length - 10) };
  });
  if (step.selector) {
    const targets = candidate.locator(step.selector);
    view.matchCount = await targets.count();
    if (view.matchCount) {
      view.target = await targets.first().evaluate(el => {
        const clip = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
        const target = { tag: el.tagName.toLowerCase(), id: clip(el.id), text: clip(el.textContent) };
        const cell = el.closest('td,th'), row = cell && cell.parentElement, table = cell && cell.closest('table');
        if (cell && row && table) {
          // Raw DOM indices and the first header row; colspan/rowspan layouts
          // are explicitly not resolved into an inferred semantic column.
          const header = table.tHead && table.tHead.rows[0];
          target.tableCell = { domCellIndex: cell.cellIndex, domRowIndex: row.rowIndex,
            rowFirstCellText: clip(row.cells[0] && row.cells[0].textContent),
            firstHeaderRowCellTextAtIndex: clip(header && header.cells[cell.cellIndex] && header.cells[cell.cellIndex].textContent),
            headerMapping: 'raw DOM cell index; spanning cells not resolved' };
        }
        return target;
      });
    }
  }
  return { ...view, pageId: currentPageId, stepIndex, source: 'actual failing-page DOM in this test',
    textLimitCharacters: 120, valuesAreBoundedPreviews: true,
    interpretation: 'Untrusted observed state only; does not determine whether the test or product is wrong.' };
}
async function eventually(read, predicate) {
  const deadline = Date.now() + 2500;
  let value;
  do {
    value = await read();
    if (predicate(value)) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  return value;
}
async function canvasHash(locator) {
  const pixels = await locator.evaluate(canvas => {
    if (!(canvas instanceof HTMLCanvasElement)) throw new Error('The selected element is not a canvas');
    return canvas.toDataURL('image/png');
  });
  return crypto.createHash('sha256').update(pixels).digest('hex');
}
async function installStorageFault(candidate, pageId) {
  await candidate.exposeBinding('__evoReportStorageDenied', (source, event) => {
    const epoch = event && faultEpochs.get(event.epoch);
    if (source.page !== candidate || !epoch || epoch.pageId !== pageId || !['setItem', 'removeItem', 'clear'].includes(event.operation)) return;
    const actualKey = String(event.key);
    if (epoch.key !== null && actualKey !== epoch.key) return;
    const key = bounded(actualKey);
    epoch.blockedWrites += 1;
    if (!epoch.blockedKeys.includes(key) && epoch.blockedKeys.length < 50) epoch.blockedKeys.push(key);
    add(epoch.operations, { operation: event.operation, key });
  });
  await candidate.addInitScript(() => {
    // Instrument localStorage API writes in this tab only. Native sessionStorage
    // retains harness state across reload; other tabs and app data are untouched.
    const local = window.localStorage, session = window.sessionStorage;
    const original = { getItem: Storage.prototype.getItem, setItem: Storage.prototype.setItem,
      removeItem: Storage.prototype.removeItem, clear: Storage.prototype.clear };
    const reserved = '__evoHarnessStorageFault_v1';
    let active = null;
    try { active = JSON.parse(original.getItem.call(session, reserved) || 'null'); } catch (_) {}
    Object.defineProperty(window, '__evoSetStorageFault', { configurable: false, writable: false,
      value: config => {
        active = config;
        if (config) original.setItem.call(session, reserved, JSON.stringify(config));
        else original.removeItem.call(session, reserved);
      } });
    function deny(operation, key) {
      if (!active || !active.enabled) return;
      // A key-specific clear is blocked only if it would clear that target key.
      if (operation === 'clear') {
        if (active.key !== null && original.getItem.call(local, active.key) === null) return;
        key = active.key === null ? '*' : active.key;
      } else {
        key = String(key);
        if (active.key !== null && key !== active.key) return;
      }
      window.__evoReportStorageDenied({ epoch: active.epoch, operation, key }).catch(() => {});
      throw new DOMException('Independent test denied this localStorage write', 'QuotaExceededError');
    }
    Storage.prototype.setItem = function (key, value) { if (this === local) deny('setItem', key); return original.setItem.call(this, key, value); };
    Storage.prototype.removeItem = function (key) { if (this === local) deny('removeItem', key); return original.removeItem.call(this, key); };
    Storage.prototype.clear = function () { if (this === local) deny('clear', null); return original.clear.call(this); };
  });
}
async function installStorageReadFault(candidate, pageId) {
  const pageToken = crypto.randomUUID();
  readPageTokens.set(pageId, pageToken);
  await candidate.exposeBinding('__evoReportStorageReadDenied', (source, event) => {
    const epoch = event && readFaultEpochs.get(event.epoch);
    if (source.page !== candidate || !epoch || epoch.pageId !== pageId || event.operation !== 'getItem'
        || event.token !== readFaultTokens.get(epoch.id)) return;
    const actualKey = String(event.key);
    if (epoch.key !== null && actualKey !== epoch.key) return;
    if (event.harnessRead === true) { epoch.blockedHarnessReads += 1; return; }
    if (event.harnessRead !== false) return;
    const key = actualKey.slice(0, 2000);
    epoch.blockedReads += 1;
    if (!epoch.blockedKeys.includes(key) && epoch.blockedKeys.length < 50) epoch.blockedKeys.push(key);
    add(epoch.operations, { operation: 'getItem', key, ...(key !== actualKey ? { keyTruncated: true } : {}) });
  });
  await candidate.addInitScript(pageToken => {
    const local = window.localStorage, session = window.sessionStorage;
    const getItem = Storage.prototype.getItem, setItem = Storage.prototype.setItem, removeItem = Storage.prototype.removeItem;
    const reportDenied = window.__evoReportStorageReadDenied;
    const reserved = '__evoHarnessStorageReadFault_v1';
    let active = null, harnessRead = false;
    try { active = JSON.parse(getItem.call(session, reserved) || 'null'); } catch (_) {}
    Object.defineProperty(window, '__evoSetStorageReadFault', { configurable: false, writable: false, value: config => {
      active = config;
      if (config) setItem.call(session, reserved, JSON.stringify(config));
      else removeItem.call(session, reserved);
    } });
    Storage.prototype.getItem = function (key) {
      if (this === local && active && active.enabled && (active.key === null || String(key) === active.key)) {
        reportDenied({ epoch: active.epoch, token: pageToken, operation: 'getItem', key: String(key), harnessRead }).catch(() => {});
        throw new DOMException('Independent test denied this localStorage read', 'SecurityError');
      }
      return getItem.call(this, key);
    };
    Object.defineProperty(window, '__evoReadStorageForAssertion', { configurable: false, writable: false, value: key => {
      harnessRead = true;
      try { return local.getItem(key); } finally { harnessRead = false; }
    } });
  }, pageToken);
}
async function main() {
  const files = new Map(spec.files.map(item => [item.urlPath, item]));
  const served = new Map();
  const mime = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
    '.cjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.txt': 'text/plain',
    '.svg': 'image/svg+xml', '.xml': 'application/xml' };
  server = http.createServer((req, res) => {
    try {
      const target = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname).slice(1);
      if (target === 'favicon.ico') { res.writeHead(204); res.end(); return; }
      if (target === '__evo_seed__') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('<!doctype html><title>Storage initialization</title>'); return; }
      const item = files.get(target);
      if (req.method !== 'GET' || !item) { res.writeHead(404); res.end('Not in the explicit test manifest'); return; }
      const raw = fs.readFileSync('/input/app/' + item.urlPath);
      const sha256 = crypto.createHash('sha256').update(raw).digest('hex');
      if (sha256 !== item.sha256) { res.writeHead(409); res.end('Frozen resource hash mismatch'); return; }
      served.set(item.path, { path: item.path, sha256 });
      const ext = require('node:path').extname(item.urlPath).toLowerCase();
      res.writeHead(200, { 'Content-Type': (mime[ext] || 'text/plain') + '; charset=utf-8',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      res.end(raw);
    } catch (_) { res.writeHead(400); res.end('Invalid confined resource request'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, chromiumSandbox: false });
  report.version = browser.version();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 },
    acceptDownloads: false, serviceWorkers: 'block' });
  context.on('page', candidate => {
    candidate.on('dialog', dialog => {
      const pageId = pageIds.get(candidate) || 'unmanaged';
      const response = pendingDialogs.get(pageId);
      pendingDialogs.delete(pageId);
      const job = (async () => {
        if (!response) { await dialog.dismiss(); return; } // Preserve the former default dismissal.
        response.observedType = dialog.type(); response.message = bounded(dialog.message());
        if (response.dialogType !== dialog.type()) {
          response.error = 'Dialog type did not match the declared response.';
          await dialog.dismiss(); return;
        }
        if (response.accept) await dialog.accept(response.dialogType === 'prompt' ? response.value : undefined);
        else await dialog.dismiss();
        response.handled = true;
      })().catch(error => {
        if (response) response.error = bounded(error.message);
        report.error = 'Dialog response failed: ' + bounded(error.message);
      });
      dialogJobs.add(job); job.finally(() => dialogJobs.delete(job));
    });
    candidate.on('console', message => add(report.console, { pageId: pageIds.get(candidate) || 'unmanaged', type: message.type(), text: bounded(message.text()) }));
    candidate.on('pageerror', error => add(report.pageErrors, { pageId: pageIds.get(candidate) || 'unmanaged', message: bounded(error.message) }));
    candidate.on('response', response => { if (response.status() >= 400) add(report.httpErrors, { pageId: pageIds.get(candidate) || 'unmanaged', status: response.status(), url: bounded(response.url()) }); });
    if (context.pages().length > 3) {
      report.error = 'The three-page test limit was exceeded.';
      candidate.close().catch(() => {});
    }
  });
  await context.route('**/*', async route => {
    let url, pageId = 'unmanaged';
    try { url = new URL(route.request().url()); pageId = pageIds.get(route.request().frame().page()) || pageId; } catch (_) {}
    if (url && url.origin === origin && url.protocol === 'http:') return route.continue();
    add(report.blockedRequests, { pageId, url: bounded(route.request().url()) });
    return route.abort('blockedbyclient');
  });
  async function openPage(pageId, seed = false) {
    if (pageMap.has(pageId) || pageMap.size >= 3) throw new Error('Invalid or excessive new_page');
    const candidate = await context.newPage();
    pageIds.set(candidate, pageId); pageMap.set(pageId, candidate);
    candidate.setDefaultTimeout(2500);
    await installStorageFault(candidate, pageId);
    await installStorageReadFault(candidate, pageId);
    if (seed) {
      await candidate.goto(origin + '/__evo_seed__', { waitUntil: 'load', timeout: 10000 });
      await candidate.evaluate(storage => { for (const [key, value] of Object.entries(storage)) localStorage.setItem(key, value); }, spec.storage || {});
    }
    const response = await candidate.goto(origin + '/' + spec.entryUrl.split('/').map(encodeURIComponent).join('/'), { waitUntil: 'load', timeout: 10000 });
    if (!response || response.status() !== 200) throw new Error('The declared entry page did not load successfully');
    report.pages.push({ pageId, entryPath: spec.entry.path });
    return candidate;
  }
  page = await openPage('main', true);
  report.executed = true;
  for (let stepIndex = 0; stepIndex < spec.checks.length; stepIndex++) {
    const step = spec.checks[stepIndex];
    currentPageId = step.pageId || 'main';
    const record = { action: step.action, pageId: currentPageId,
      ...(step.selector ? { selector: step.selector } : {}), ...(step.key ? { key: step.key } : {}) };
    try {
      if (step.action === 'new_page') page = await openPage(currentPageId);
      else {
        page = pageMap.get(currentPageId);
        if (!page) throw new Error('Unknown pageId; create it with new_page first');
        const locator = step.selector ? page.locator(step.selector) : null;
        const canvasKey = currentPageId + '\0' + step.key;
        if (step.action === 'respond_dialog') {
          if (pendingDialogs.has(currentPageId)) throw new Error('The previous response for this page has not been consumed.');
          const response = { pageId: currentPageId, dialogType: step.dialogType, accept: step.accept,
            ...(step.value !== undefined ? { value: step.value } : {}), armedAtStep: stepIndex + 1, handled: false };
          report.dialogResponses.push(response); pendingDialogs.set(currentPageId, response);
        } else if (step.action === 'storage_write_denied') {
          const previous = activeFaults.get(currentPageId);
          if (previous) previous.restoredAtStep = stepIndex + 1;
          if (step.enabled) {
            const epoch = { id: 'fault-' + (report.faults.length + 1), pageId: currentPageId,
              key: step.key === undefined ? null : step.key, enabledAtStep: stepIndex + 1, restoredAtStep: null,
              blockedWrites: 0, blockedKeys: [], operations: [] };
            report.faults.push(epoch); faultEpochs.set(epoch.id, epoch); activeFaults.set(currentPageId, epoch);
            await page.evaluate(config => window.__evoSetStorageFault(config), { enabled: true, key: epoch.key, epoch: epoch.id });
          } else {
            activeFaults.delete(currentPageId);
            await page.evaluate(() => window.__evoSetStorageFault(null));
          }
          record.enabled = step.enabled;
        } else if (step.action === 'storage_read_denied') {
          const previous = activeReadFaults.get(currentPageId);
          if (previous) previous.restoredAtStep = stepIndex + 1;
          if (step.enabled) {
            const epoch = { id: 'read-fault-' + (report.readFaults.length + 1), pageId: currentPageId,
              key: step.key === undefined ? null : step.key, enabledAtStep: stepIndex + 1, restoredAtStep: null,
              blockedReads: 0, blockedHarnessReads: 0, blockedKeys: [], operations: [] };
            const token = readPageTokens.get(currentPageId);
            report.readFaults.push(epoch); readFaultEpochs.set(epoch.id, epoch); readFaultTokens.set(epoch.id, token);
            activeReadFaults.set(currentPageId, epoch);
            await page.evaluate(config => window.__evoSetStorageReadFault(config), { enabled: true, key: epoch.key, epoch: epoch.id });
          } else {
            activeReadFaults.delete(currentPageId);
            await page.evaluate(() => window.__evoSetStorageReadFault(null));
          }
          record.enabled = step.enabled;
        } else if (step.action === 'click') await locator.click();
        else if (step.action === 'fill') await locator.fill(step.value);
        else if (step.action === 'select') await locator.selectOption(step.value);
        else if (step.action === 'press') await locator.press(step.key);
        else if (step.action === 'reload') await page.reload({ waitUntil: 'load' });
        else if (step.action === 'capture_canvas') canvasSnapshots.set(canvasKey, await canvasHash(locator));
        else if (step.action === 'assert_text') {
          record.expected = step.text;
          record.actual = await eventually(() => locator.textContent(), value => step.exact ? value === step.text : typeof value === 'string' && value.includes(step.text));
          record.passed = step.exact ? record.actual === step.text : typeof record.actual === 'string' && record.actual.includes(step.text);
        } else if (step.action === 'assert_count') {
          record.expected = step.count; record.actual = await eventually(() => locator.count(), value => value === step.count); record.passed = record.actual === step.count;
        } else if (step.action === 'assert_value') {
          record.expected = step.value; record.actual = await eventually(() => locator.inputValue(), value => value === step.value); record.passed = record.actual === step.value;
        } else if (step.action === 'assert_visible') {
          record.expected = true; record.actual = await eventually(() => locator.isVisible(), Boolean); record.passed = record.actual === true;
        } else if (step.action === 'assert_storage' || step.action === 'assert_storage_missing') {
          record.expected = step.action === 'assert_storage_missing' ? null : step.value;
          record.actual = await eventually(() => page.evaluate(key => window.__evoReadStorageForAssertion(key), step.key), value => value === record.expected);
          record.passed = record.actual === record.expected;
        } else if (step.action === 'assert_attribute') {
          record.name = step.name; record.expected = step.value;
          record.actual = await eventually(() => locator.getAttribute(step.name), value => value === step.value);
          record.passed = record.actual === step.value;
        } else if (step.action === 'assert_canvas_changed') {
          if (!canvasSnapshots.has(canvasKey)) throw new Error('Canvas comparison requires capture_canvas in the same page');
          record.expectedDifferentFrom = canvasSnapshots.get(canvasKey);
          record.actual = await eventually(() => canvasHash(locator), value => value !== canvasSnapshots.get(canvasKey));
          record.passed = record.actual !== canvasSnapshots.get(canvasKey);
        } else throw new Error('Unsupported trusted harness step');
      }
      await Promise.all([...dialogJobs]);
      if (step.action.startsWith('assert_')) {
        report.assertions.push(record);
        if (!record.passed) throw new Error('Assertion failed');
      }
      report.steps.push({ ...record, ok: true });
    } catch (error) {
      if (step.action.startsWith('assert_') && !report.assertions.includes(record)) report.assertions.push({ ...record, passed: false, error: bounded(error.message) });
      report.steps.push({ ...record, ok: false, error: bounded(error.message) });
      report.error = 'Browser step failed: ' + bounded(error.message);
      // openPage can fail before assignment completes. Never label the previous
      // page's DOM or screenshot as the newly requested page.
      page = pageMap.get(currentPageId) || null;
      try {
        if (page && !page.isClosed()) report.failureObservation = await failureObservation(page, step, stepIndex);
      } catch (observationError) {
        report.failureObservationError = bounded(observationError.message);
      }
      break;
    }
  }
  // Binding messages are delivered before these round trips complete.
  for (const candidate of pageMap.values()) await candidate.evaluate(() => Promise.resolve());
  if (context.pages().some(candidate => !pageIds.has(candidate))) report.error = 'The app opened an undeclared page; only new_page pages are testable.';
  report.faultCoverageComplete = report.faults.every(epoch => epoch.blockedWrites > 0);
  if (!report.faultCoverageComplete && !report.error) report.error = 'A requested storage fault was not exercised by any matching API write; coverage is incomplete.';
  report.readFaultCoverageComplete = report.readFaults.every(epoch => epoch.blockedReads > 0);
  if (!report.readFaultCoverageComplete && !report.error) report.error = 'A requested read fault was not exercised by an application localStorage.getItem; harness assertions do not count.';
  await Promise.all([...dialogJobs]);
  report.dialogCoverageComplete = report.dialogResponses.every(response => response.handled && !response.error);
  if (!report.dialogCoverageComplete && !report.error) report.error = 'A declared dialog response was unused, mismatched, or failed.';
  report.testedFiles = [...served.values()];
}
(async () => {
  try { await main(); } catch (error) { report.error = bounded(error.message); }
  try {
    if (page) {
      await page.screenshot({ path: '/evidence/screenshot.png', fullPage: false, timeout: 5000 });
      report.screenshotCaptured = true; report.screenshotPageId = currentPageId;
    }
  } catch (error) { report.screenshotError = bounded(error.message); }
  report.assertionCount = report.assertions.length;
  report.assertionsPassed = report.assertions.filter(item => item.passed).length;
  report.ok = report.executed && report.assertionCount > 0 && report.assertionsPassed === report.assertionCount
    && !report.error && report.pageErrors.length === 0 && report.httpErrors.length === 0 && report.blockedRequests.length === 0
    && report.faults.every(epoch => epoch.blockedWrites > 0)
    && report.readFaults.every(epoch => epoch.blockedReads > 0)
    && report.dialogResponses.every(response => response.handled && !response.error) && report.screenshotCaptured === true;
  try { if (browser) await browser.close(); } catch (_) {}
  try { if (server) server.close(); } catch (_) {}
  fs.writeFileSync('/evidence/report.json', JSON.stringify(report, null, 2));
  process.stdout.write(JSON.stringify(report));
  process.exit(report.ok ? 0 : 1);
})();
