#!/usr/bin/env node
/* FlexFactor journey engine (standalone CommonJS Playwright script).
 *
 * CLI contract (unchanged from the embedded _UI_EXPLORER_JS):
 *   node flexfactor_explorer.js <base_url> <artifact_dir>
 * with NODE_PATH pointing at a node_modules that contains `playwright`.
 * Prints exactly one line `FLEXFACTOR_E2E_RESULT=<json>` on stdout.
 *
 * Environment:
 *   FLEXFACTOR_E2E_ISOLATED=1      disposable environment: REAL form submissions + destructive controls
 *   FLEXFACTOR_E2E_MAX_PAGES=N     route cap (default 500); reaching it is NAMED in incomplete_reasons
 *   FLEXFACTOR_E2E_ROLES=<json>    [{name, cookies?, localStorage?, login?:{url, fields:{sel:val}, submit}}]
 *   FLEXFACTOR_E2E_VIEWPORTS=      "1280x800,390x844" (default)
 *
 * Result keys (superset of the original): pages, controls, errors, skipped, routeEvidence,
 * controlEvidence, formEvidence, accessibility, performance, artifacts, complete, plus
 * roles, viewports, authorization_matrix, findings, journeys, summary, incomplete_reasons.
 * Anything not executed is NAMED in `skipped` / `incomplete_reasons` and forces complete=false.
 */
'use strict';
const path = require('path');
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (e) {
  console.log('FLEXFACTOR_E2E_RESULT=' + JSON.stringify({
    pages: 0, controls: 0, errors: [`playwright not resolvable via NODE_PATH: ${e.message}`], skipped: [],
    routeEvidence: [], controlEvidence: [], formEvidence: [], accessibility: { checked: 0, violations: [] },
    performance: { pages: [], slow: [] }, artifacts: [], journeys: [], authorization_matrix: [], findings: [],
    summary: { passed: 0, failed: 0, skipped: 0, total: 0 },
    incomplete_reasons: ['playwright module not found'], complete: false,
  }));
  process.exit(2);
}

const DANGEROUS = /delete|remove|destroy|purchase|pay\b|send|publish|deploy|logout|log out|sign out|reset|wipe|drop|clear all/i;
const PROTECTED_PATH = /\/admin|\/settings|\/dashboard|\/account/i;
const LOGIN_PATH = /login|signin|sign-in|auth/i;
const DENIED_TEXT = /\bforbidden\b|not authori[sz]ed|access denied|unauthori[sz]ed|please (log|sign) in|permission denied/i;
const ACTION_TIMEOUT_MS = Math.max(1000, parseInt(process.env.FLEXFACTOR_E2E_ACTION_TIMEOUT_MS || '15000', 10) || 15000);
const PAGE_TIMEOUT_MS = Math.max(ACTION_TIMEOUT_MS, parseInt(process.env.FLEXFACTOR_E2E_PAGE_TIMEOUT_MS || '60000', 10) || 60000);
const RUN_TIMEOUT_MS = Math.max(5000, parseInt(process.env.FLEXFACTOR_E2E_RUN_TIMEOUT_MS || '600000', 10) || 600000);
const NAV_TIMEOUT = Math.min(30000, ACTION_TIMEOUT_MS);
const SETTLE_MS = 1500;
const T0 = Date.now();
const progress = (msg) => { try { process.stderr.write(`[explorer +${((Date.now() - T0) / 1000).toFixed(1)}s] ${msg}\n`); } catch {} };
let emitted = false;
function emit(line, code) {
  if (emitted) return;
  emitted = true;
  const done = () => process.exit(code);
  const fallback = setTimeout(done, 2000);
  try { process.stdout.write(line + '\n', () => { clearTimeout(fallback); done(); }); } catch { done(); }
}
const EXPECTED_4XX_CONSOLE = /Failed to load resource: the server responded with a status of 4\d\d/;

function parseViewports(raw) {
  const out = [];
  for (const part of String(raw || '1280x800,390x844').split(',')) {
    const m = part.trim().match(/^(\d+)x(\d+)$/);
    if (m) out.push({ width: Number(m[1]), height: Number(m[2]), label: `${m[1]}x${m[2]}` });
  }
  return out.length ? out : [{ width: 1280, height: 800, label: '1280x800' }];
}

function parseRoles(raw) {
  const roles = [{ name: 'anonymous' }];
  if (!raw) return { roles, error: null };
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return { roles, error: 'FLEXFACTOR_E2E_ROLES is not a JSON list' };
    for (const r of parsed) {
      if (!r || typeof r.name !== 'string' || !r.name.trim()) return { roles, error: 'role without a name in FLEXFACTOR_E2E_ROLES' };
      if (r.name === 'anonymous') continue;
      roles.push(r);
    }
    return { roles, error: null };
  } catch (e) {
    return { roles, error: `FLEXFACTOR_E2E_ROLES is not valid JSON: ${e.message}` };
  }
}

function rand() { return Math.random().toString(36).slice(2, 10); }
function sampleValue(kind, field) {
  switch (kind) {
    case 'email': return `flexfactor+${rand()}@example.invalid`;
    case 'number': case 'range': {
      const min = field.min !== '' && field.min != null ? Number(field.min) : 1;
      const max = field.max !== '' && field.max != null ? Number(field.max) : min + 41;
      return String(Math.min(max, Math.max(min, 42)));
    }
    case 'tel': return '5555550100';
    case 'url': return `https://example.invalid/${rand()}`;
    case 'password': return `Flex!Factor-${rand()}`;
    case 'date': return '2026-01-15';
    case 'datetime-local': return '2026-01-15T10:30';
    case 'time': return '10:30';
    case 'month': return '2026-01';
    case 'week': return '2026-W03';
    case 'color': return '#336699';
    case 'textarea': return `FlexFactor journey message ${rand()}`;
    default: return `FlexFactor ${rand()}`;
  }
}

(async () => {
  const base = new URL(process.argv[2]);
  const artifactDir = path.resolve(process.argv[3] || process.cwd());
  const isolated = process.env.FLEXFACTOR_E2E_ISOLATED === '1';
  const maxPages = Math.max(1, parseInt(process.env.FLEXFACTOR_E2E_MAX_PAGES || '500', 10) || 500);
  const viewports = parseViewports(process.env.FLEXFACTOR_E2E_VIEWPORTS);
  const { roles, error: roleError } = parseRoles(process.env.FLEXFACTOR_E2E_ROLES);

  const errors = [], skipped = [], incomplete = [], findings = [], journeys = [];
  const routeEvidence = [], controlEvidence = [], formEvidence = [], authz = [];
  const accessibility = { checked: 0, violations: [] };
  const performance = { pages: [], slow: [] };
  const artifacts = [];
  const routes = [];            // ordered union of discovered same-origin URLs
  const routeSet = new Set();
  const unvisitedByCap = new Set();
  const formsSeen = new Map();  // key -> {role, url, index, action, method, fields, destructive, submitLabel}
  const pendingDestructiveControls = [];
  let controls = 0, journeyId = 0, shotId = 0;
  if (roleError) incomplete.push(roleError);
  const timeouts = [];
  // every awaited browser action races a deadline: a hang becomes a named `timeout` finding, never a stall
  function withDeadline(promise, ms, label) {
    let timer;
    const gate = new Promise((_, reject) => { timer = setTimeout(() => {
      findings.push({ kind: 'timeout', label, ms });
      timeouts.push(label);
      reject(new Error(`timeout after ${ms}ms: ${label}`));
    }, ms); });
    return Promise.race([Promise.resolve(promise), gate]).finally(() => clearTimeout(timer));
  }
  const act = (promise, label) => withDeadline(promise, ACTION_TIMEOUT_MS, label);

  const addJourney = (row) => { const j = { id: `j${++journeyId}`, ...row }; journeys.push(j); return j; };
  const shot = async (page, name) => {
    const file = `${name}-${++shotId}.png`;
    try { await act(page.screenshot({ path: path.join(artifactDir, file), fullPage: true, timeout: ACTION_TIMEOUT_MS }), `screenshot ${name}`); artifacts.push(file); return file; }
    catch (e) { errors.push(`screenshot ${name}: ${e.message}`); return null; }
  };
  const normalize = (href) => { try { const u = new URL(href, base); u.hash = ''; return u.href; } catch { return null; } };
  const addRoute = (href) => {
    const u = normalize(href);
    if (!u || !u.startsWith(base.origin)) return;
    if (routeSet.has(u)) return;
    if (routes.length >= maxPages) { unvisitedByCap.add(u); return; }
    routeSet.add(u); routes.push(u);
  };
  addRoute(base.href);

  let browser = null;
  const buildResult = () => {
    if (unvisitedByCap.size && !incomplete.some((x) => x.startsWith('page cap '))) incomplete.push(`page cap ${maxPages} reached; ${unvisitedByCap.size} discovered routes unvisited`);
    if (!routes.length && !incomplete.includes('no routes discovered')) incomplete.push('no routes discovered');
    const summary = { passed: 0, failed: 0, skipped: 0, total: journeys.length };
    for (const j of journeys) summary[j.status] = (summary[j.status] || 0) + 1;
    const complete = errors.length === 0 && skipped.length === 0 && incomplete.length === 0 && summary.failed === 0
      && accessibility.violations.length === 0 && performance.slow.length === 0 && routes.length > 0 && timeouts.length === 0;
    return {
      pages: routes.length, controls, errors, skipped, routeEvidence, controlEvidence, formEvidence, accessibility, performance,
      artifacts, roles: roles.map((r) => r.name), viewports: viewports.map((v) => v.label), isolated, maxPages,
      authorization_matrix: authz.map(({ route, role, outcome, signal, httpStatus }) => ({ route, role, outcome, signal, httpStatus })),
      findings, journeys, summary, timeouts, incomplete_reasons: incomplete, complete,
      elapsedMs: Date.now() - T0, runTimeoutMs: RUN_TIMEOUT_MS,
    };
  };
  // whole-run watchdog: emit whatever evidence exists, then leave - the browser is closed on a best-effort deadline
  const watchdog = setTimeout(async () => {
    progress(`RUN TIMEOUT after ${RUN_TIMEOUT_MS}ms - emitting partial result`);
    incomplete.push('run timeout');
    incomplete.push(`run timeout: FLEXFACTOR_E2E_RUN_TIMEOUT_MS=${RUN_TIMEOUT_MS} elapsed before the engine finished`);
    emit('FLEXFACTOR_E2E_RESULT=' + JSON.stringify(buildResult()), 1);
    try { if (browser) await withDeadline(browser.close(), 1500, 'browser.close (watchdog)'); } catch {}
    process.exit(1);
  }, RUN_TIMEOUT_MS);
  progress(`start base=${base.href} isolated=${isolated} roles=${roles.map((r) => r.name).join(',')} viewports=${viewports.map((v) => v.label).join(',')} maxPages=${maxPages} runTimeout=${RUN_TIMEOUT_MS}ms`);
  browser = await withDeadline(chromium.launch({ headless: true }), Math.max(ACTION_TIMEOUT_MS, 60000), 'chromium.launch');

  // ---- per-role context setup -------------------------------------------------------------
  async function openContext(role, viewport) {
    const ctx = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, ignoreHTTPSErrors: true });
    ctx.on('dialog', (d) => d.dismiss().catch(() => {}));
    ctx.on('page', (p) => { if (p.listenerCount('dialog') === 0) p.on('dialog', (d) => d.dismiss().catch(() => {})); });
    if (Array.isArray(role.cookies) && role.cookies.length) {
      await ctx.addCookies(role.cookies.map((c) => (c.url || c.domain) ? c : { ...c, url: base.origin }));
    }
    if (role.localStorage && typeof role.localStorage === 'object') {
      const entries = Object.entries(role.localStorage);
      await ctx.addInitScript((kv) => { for (const [k, v] of kv) try { localStorage.setItem(k, String(v)); } catch {} }, entries);
    }
    return ctx;
  }

  async function performLogin(role, ctx) {
    const login = role.login;
    if (!login || !login.url) return true;
    const page = await ctx.newPage();
    page.removeAllListeners('dialog');
    page.on('dialog', (d) => d.dismiss().catch(() => {}));
    const target = normalize(login.url);
    const started = Date.now();
    let ok = true, reason = null;
    try {
      const resp = await page.goto(target, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
      for (const [sel, val] of Object.entries(login.fields || {})) await page.fill(sel, String(val), { timeout: 10000 });
      const submitted = page.waitForLoadState('load', { timeout: 10000 }).catch(() => {});
      if (login.submit) await page.click(login.submit, { timeout: 10000 });
      else await page.keyboard.press('Enter');
      await submitted;
      await page.waitForTimeout(300);
      const cookies = await ctx.cookies();
      const stillOnLogin = normalize(page.url()) === target;
      if (!cookies.length && stillOnLogin) { ok = false; reason = `login for role ${role.name} left no cookies and stayed on ${target}`; }
      if (resp && resp.status() >= 400) { ok = false; reason = `login page ${target} returned HTTP ${resp.status()}`; }
    } catch (e) { ok = false; reason = `login for role ${role.name} failed: ${e.message}`; }
    const screenshot = await shot(page, `login-${role.name}`);
    addJourney({ kind: 'login', role: role.name, viewport: viewports[0].label, target, status: ok ? 'passed' : 'failed', reason, screenshot, durationMs: Date.now() - started, postUrl: page.url() });
    if (!ok) errors.push(reason);
    await page.close();
    return ok;
  }

  function attachListeners(page, state, { acceptDialogs = false } = {}) {
    state.dialogs = state.dialogs || [];
    page.removeAllListeners('dialog');
    page.on('dialog', (d) => { state.dialogs.push({ type: d.type(), message: d.message(), action: acceptDialogs ? 'accept' : 'dismiss' }); (acceptDialogs ? d.accept() : d.dismiss()).catch(() => {}); });
    page.on('pageerror', (e) => state.errors.push(`pageerror: ${e.message}`));
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const loc = (m.location() || {}).url || '';
      if (EXPECTED_4XX_CONSOLE.test(m.text()) && normalize(loc) === normalize(page.url())) { state.denied4xx.push(m.text()); return; }
      state.errors.push(`console: ${m.text()}`);
    });
    page.on('requestfailed', (r) => {
      const txt = (r.failure() || {}).errorText || '';
      if (/ERR_ABORTED/.test(txt)) return; // navigation superseded by the explorer itself
      state.errors.push(`request failed: ${r.url()} ${txt}`);
    });
  }

  function classify(resp, finalUrl, bodyText) {
    const status = resp ? resp.status() : null;
    if (status == null) return { outcome: 'error', signal: 'no response' };
    if (status >= 500) return { outcome: 'error', signal: `HTTP ${status}` };
    if (status === 401 || status === 403) return { outcome: 'denied', signal: `HTTP ${status}` };
    if (status >= 400) return { outcome: 'denied', signal: `HTTP ${status}` };
    const fu = new URL(finalUrl);
    if (LOGIN_PATH.test(fu.pathname) && !LOGIN_PATH.test(new URL(resp.url()).pathname || '')) return { outcome: 'denied', signal: `redirect-to-login ${fu.pathname}` };
    if (resp.request().redirectedFrom() && LOGIN_PATH.test(fu.pathname)) return { outcome: 'denied', signal: `redirect-to-login ${fu.pathname}` };
    if (DENIED_TEXT.test(bodyText.slice(0, 4000))) return { outcome: 'denied', signal: 'forbidden text' };
    return { outcome: 'permitted', signal: 'rendered' };
  }

  async function a11yCheck(page) {
    return page.evaluate(() => {
      const out = [];
      if (!document.title.trim()) out.push('document has no title');
      if (!document.documentElement.lang) out.push('html has no lang attribute');
      for (const el of document.querySelectorAll('img:not([alt])')) out.push('image missing alt');
      for (const el of document.querySelectorAll('button, [role="button"]')) {
        if (!(el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim()) out.push('button missing accessible name');
      }
      for (const el of document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea')) {
        const id = el.id && CSS.escape(el.id);
        const labelled = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || (id && document.querySelector(`label[for="${id}"]`)) || el.closest('label');
        if (!labelled) out.push(`${el.tagName.toLowerCase()} missing accessible label`);
      }
      return [...new Set(out)];
    }).catch(() => []);
  }

  async function describeForms(page) {
    return page.locator('form').evaluateAll((forms) => forms.map((f, index) => {
      const fields = [];
      for (const el of f.querySelectorAll('input, select, textarea')) {
        const tag = el.tagName.toLowerCase();
        const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
        if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
        fields.push({ tag, type, name: el.getAttribute('name') || '', id: el.id || '', required: el.required, min: el.getAttribute('min'), max: el.getAttribute('max'), maxlength: el.getAttribute('maxlength') });
      }
      const submit = f.querySelector('button:not([type="button"]):not([type="reset"]), input[type="submit"]');
      return {
        index, action: f.getAttribute('action') || '', method: (f.getAttribute('method') || 'get').toUpperCase(), fields,
        submitLabel: submit ? ((submit.innerText || submit.value || submit.getAttribute('aria-label') || '').trim()) : '',
        validEmpty: f.checkValidity(),
      };
    })).catch(() => []);
  }

  // ---- route visit (crawl + controls + form discovery) ----------------------------------
  async function visitRoute(role, ctx, url, { crawl }) {
    const page = await ctx.newPage();
    const state = { errors: [], denied4xx: [] };
    attachListeners(page, state);
    const started = Date.now();
    let resp = null;
    try { resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT }); }
    catch (e) { state.errors.push(`navigation: ${e.message}`); }
    const durationMs = Date.now() - started;
    const bodyText = await act(page.evaluate(() => document.body ? document.body.innerText : ''), `bodyText ${url}`).catch(() => '');
    const { outcome, signal } = classify(resp, page.url(), bodyText);
    if (outcome === 'error') state.errors.push(`${signal}`);
    const httpStatus = resp ? resp.status() : null;
    authz.push({ route: url, role: role.name, outcome, signal, httpStatus, finalUrl: page.url() });
    if (role.name === 'anonymous' && outcome === 'permitted' && PROTECTED_PATH.test(new URL(url).pathname)) {
      findings.push({ kind: 'authz-suspect', route: url, role: role.name, detail: `anonymous rendered ${new URL(url).pathname} (HTTP ${httpStatus})` });
    }
    let screenshot = null;
    if (crawl) {
      const perf = await page.evaluate(() => {
        const n = performance.getEntriesByType('navigation')[0];
        return n ? { domContentLoaded: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd) } : {};
      }).catch(() => ({}));
      performance.pages.push({ url, role: role.name, durationMs, ...perf });
      if (durationMs > 5000) performance.slow.push({ url, role: role.name, durationMs, thresholdMs: 5000 });
      const contentType = resp ? String((resp.headers() || {})['content-type'] || '') : '';
      if (outcome === 'permitted' && /text\/html|xhtml/i.test(contentType)) {
        const a11y = await act(a11yCheck(page), `a11y ${url}`).catch(() => []);
        accessibility.checked++;
        for (const v of a11y) accessibility.violations.push({ url, role: role.name, message: v });
      } else if (outcome === 'permitted') {
        accessibility.notApplicable = (accessibility.notApplicable || []).concat([{ url, role: role.name, contentType }]);
      }
      screenshot = await shot(page, `page-${role.name}`);
      const links = await act(page.locator('a[href]').evaluateAll((els) => els.map((e) => e.href)), `links ${url}`).catch(() => []);
      for (const href of links) if (!/^(mailto|tel|javascript):/i.test(href)) addRoute(href);
      // forms: discover (dedupe by signature); executed in a later phase
      if (outcome === 'permitted') {
        for (const f of await act(describeForms(page), `describeForms ${url}`).catch(() => [])) {
          const key = `${normalize(f.action || url)}|${f.method}|${f.fields.map((x) => x.name || x.id || x.type).join(',')}`;
          if (!formsSeen.has(key)) {
            const destructive = DANGEROUS.test(f.submitLabel) || DANGEROUS.test(new URL(f.action || url, base).pathname);
            formsSeen.set(key, { key, role: role.name, url, ...f, destructive });
          }
        }
        await exerciseControls(role, page, url, state);
      }
      try { await act(page.keyboard.press('Tab'), `tab ${url}`); } catch (e) { state.errors.push(`keyboard navigation: ${e.message}`); }
    }
    for (const e of state.errors) errors.push(`${url} [${role.name}]: ${e}`);
    const row = { url, role: role.name, status: state.errors.length ? 'failed' : 'passed', outcome, signal, httpStatus, durationMs, screenshot, errors: state.errors, denied4xxConsole: state.denied4xx };
    if (state.dialogs && state.dialogs.length) row.dialogs = state.dialogs;
    routeEvidence.push(row);
    addJourney({ kind: crawl ? 'route' : 'authz', role: role.name, viewport: viewports[0].label, target: url, status: row.status, reason: state.errors[0] || null, screenshot, outcome, httpStatus });
    await act(page.close(), `close ${url}`).catch(() => {});
  }
  // per-page deadline: a hung page becomes a failed journey + timeout finding, the run moves on
  async function guardedVisit(role, ctx, url, opts) {
    try { await withDeadline(visitRoute(role, ctx, url, opts), PAGE_TIMEOUT_MS, `page ${url} [${role.name}]`); }
    catch (e) {
      errors.push(`${url} [${role.name}]: ${e.message}`);
      if (!authz.some((a) => a.route === url && a.role === role.name)) authz.push({ route: url, role: role.name, outcome: 'error', signal: 'page timeout', httpStatus: null, finalUrl: null });
      addJourney({ kind: opts.crawl ? 'route' : 'authz', role: role.name, viewport: viewports[0].label, target: url, status: 'failed', reason: e.message });
    }
  }

  // controls outside forms (form submit buttons are covered by the form journeys)
  async function exerciseControls(role, page, url, state) {
    const selector = 'button, [role="button"], [role="tab"], [role="menuitem"], input[type="button"], summary, [role="switch"], [role="checkbox"][tabindex]';
    const count = await page.locator(selector).count();
    for (let i = 0; i < count; i++) {
      const control = page.locator(selector).nth(i);
      const inForm = await control.evaluate((el) => !!el.closest('form') && (el.tagName === 'BUTTON' ? (el.getAttribute('type') || 'submit') !== 'button' : el.tagName !== 'INPUT' ? false : true)).catch(() => false);
      if (inForm) continue;
      if (!await control.isVisible().catch(() => false) || await control.isDisabled().catch(() => true)) continue;
      const label = ((await control.innerText().catch(() => '')) || (await control.getAttribute('aria-label').catch(() => '')) || (await control.getAttribute('value').catch(() => '')) || (await control.getAttribute('title').catch(() => '')) || '').trim();
      const roleAttr = (await control.getAttribute('role').catch(() => null)) || (await control.evaluate((el) => el.tagName.toLowerCase()).catch(() => 'unknown'));
      const target = { url, role: role.name, controlRole: roleAttr, label, index: i, targeting: 'semantic-role-and-name' };
      if (!label) {
        const reason = `${url}: unnamed ${roleAttr} control ${i} has low-confidence targeting`;
        skipped.push(reason); controlEvidence.push({ ...target, status: 'blocked-low-confidence', reason });
        addJourney({ kind: 'control', role: role.name, viewport: viewports[0].label, target: `${url} #${i}`, status: 'skipped', reason });
        continue;
      }
      if (DANGEROUS.test(label)) {
        if (!isolated) {
          const reason = `destructive control "${label}" on ${url} not clicked: FLEXFACTOR_E2E_ISOLATED not set`;
          skipped.push(reason); controlEvidence.push({ ...target, status: 'blocked-destructive', reason });
          addJourney({ kind: 'destructive', role: role.name, viewport: viewports[0].label, target: `${url} "${label}"`, status: 'skipped', reason });
        } else {
          pendingDestructiveControls.push({ role, url, selector, index: i, label, target });
        }
        continue;
      }
      controls++;
      let status = 'passed', error = null, navigatedTo = null;
      const responses = [];
      const onResp = (r) => responses.push({ url: r.url(), status: r.status(), method: r.request().method() });
      page.on('response', onResp);
      try { await act(control.click({ timeout: 5000 }).then(() => page.waitForTimeout(150)), `click "${label}" on ${url}`); }
      catch (e) { status = 'failed'; error = e.message; state.errors.push(`control "${label}": ${e.message}`); }
      page.off('response', onResp);
      if (normalize(page.url()) !== normalize(url)) {
        navigatedTo = page.url(); addRoute(navigatedTo);
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT }).catch((e) => state.errors.push(`return navigation after "${label}": ${e.message}`));
      }
      controlEvidence.push({ ...target, status, error, navigatedTo, responses: responses.slice(0, 20) });
      addJourney({ kind: 'control', role: role.name, viewport: viewports[0].label, target: `${url} "${label}"`, status, reason: error, navigatedTo });
    }
  }

  // ---- forms ----------------------------------------------------------------------------
  async function fillForm(page, form, mode) {
    // mode: valid | empty | oversized | malformed-email ; returns {data, touched}
    const data = {};
    const fieldLocators = form.locator('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), select, textarea');
    const n = await fieldLocators.count();
    let oversizedDone = false, malformedDone = false;
    for (let i = 0; i < n; i++) {
      const el = fieldLocators.nth(i);
      const meta = await el.evaluate((e) => ({ tag: e.tagName.toLowerCase(), type: e.tagName === 'INPUT' ? (e.getAttribute('type') || 'text').toLowerCase() : e.tagName.toLowerCase(), name: e.getAttribute('name') || e.id || `field${Math.random()}`, min: e.getAttribute('min'), max: e.getAttribute('max'), disabled: e.disabled, readOnly: e.readOnly })).catch(() => null);
      if (!meta || meta.disabled || meta.readOnly) continue;
      if (mode === 'empty') {
        if (['checkbox', 'radio', 'select', 'file'].includes(meta.type)) continue;
        await el.fill('').catch(() => {}); data[meta.name] = ''; continue;
      }
      if (meta.type === 'file') { data[meta.name] = '(file input left empty)'; continue; }
      if (meta.type === 'checkbox') { await el.check({ timeout: 3000 }).catch(() => {}); data[meta.name] = true; continue; }
      if (meta.type === 'radio') { await el.check({ timeout: 3000 }).catch(() => {}); data[meta.name] = 'first'; continue; }
      if (meta.type === 'select') {
        const v = await el.evaluate((s) => { const o = [...s.options].find((x) => x.value !== '' && !x.disabled); return o ? o.value : null; }).catch(() => null);
        if (v != null) { await el.selectOption(v).catch(() => {}); data[meta.name] = v; }
        continue;
      }
      let value;
      if (mode === 'oversized' && ['text', 'textarea', 'search'].includes(meta.type)) { value = 'X'.repeat(10000); oversizedDone = true; }
      else if (mode === 'malformed-email' && !malformedDone && meta.type === 'email') { value = 'not-an-email@'; malformedDone = true; }
      else value = sampleValue(meta.type, meta);
      await el.fill(value, { timeout: 3000 }).catch(async () => { await el.evaluate((e, v) => { e.value = v; e.dispatchEvent(new Event('input', { bubbles: true })); }, value).catch(() => {}); });
      data[meta.name] = value.length > 64 ? `${value.slice(0, 32)}…(${value.length} chars)` : value;
    }
    return { data, oversizedDone, malformedDone };
  }

  async function submitAndObserve(page, form, meta) {
    const responses = [];
    const onResp = (r) => {
      const rt = r.request().resourceType();
      if (['document', 'xhr', 'fetch'].includes(rt)) responses.push({ url: r.url(), status: r.status(), method: r.request().method(), resourceType: rt });
    };
    page.on('response', onResp);
    const before = await page.evaluate(() => ({ url: location.href, title: document.title, textLength: (document.body && document.body.innerText || '').length })).catch(() => ({}));
    const clientValidity = await form.evaluate((f) => ({
      valid: f.checkValidity(),
      messages: [...f.querySelectorAll('input, select, textarea')].filter((e) => !e.validity.valid).map((e) => `${e.name || e.id || e.type}: ${e.validationMessage}`),
    })).catch(() => ({ valid: null, messages: [] }));
    // exercise the backend regardless of client constraints (evidence for both layers)
    await form.evaluate((f) => { f.noValidate = true; }).catch(() => {});
    const settle = page.waitForLoadState('load', { timeout: 8000 }).catch(() => {});
    const submitBtn = form.locator('button:not([type="button"]):not([type="reset"]), input[type="submit"]').first();
    let submitError = null;
    if (await submitBtn.count()) await submitBtn.click({ timeout: 5000 }).catch((e) => { submitError = e.message; });
    else await form.evaluate((f) => f.requestSubmit ? f.requestSubmit() : f.submit()).catch((e) => { submitError = e.message; });
    await settle;
    await page.waitForTimeout(SETTLE_MS);
    page.off('response', onResp);
    const after = await page.evaluate(() => ({
      url: location.href, title: document.title, textLength: (document.body && document.body.innerText || '').length,
      alert: [...document.querySelectorAll('[role="alert"], .error, .errors, .invalid-feedback, .field-error, [aria-invalid="true"]')].map((e) => (e.innerText || e.getAttribute('aria-invalid') || '').trim()).filter(Boolean).slice(0, 5),
      invalid: [...document.querySelectorAll('input:invalid, select:invalid, textarea:invalid')].map((e) => `${e.name || e.id || e.type}: ${e.validationMessage}`).slice(0, 10),
    })).catch(() => ({}));
    const wanted = responses.filter((r) => r.method === meta.method) ;
    const primary = (wanted.length ? wanted : responses).slice(-1)[0] || null;
    return {
      submitError, clientValidity, response: primary, responses: responses.slice(0, 20),
      postUrl: after.url, domDelta: { urlChanged: before.url !== after.url, titleBefore: before.title, titleAfter: after.title, textLengthBefore: before.textLength, textLengthAfter: after.textLength },
      observedValidation: [...(clientValidity.messages || []), ...(after.alert || []), ...(after.invalid || [])].slice(0, 10),
    };
  }

  async function runFormCase(role, ctx, form, mode) {
    const page = await ctx.newPage();
    const state = { errors: [], denied4xx: [] };
    attachListeners(page, state);
    const started = Date.now();
    let result = { mode, status: 'failed', reason: null };
    try {
      await page.goto(form.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
      const loc = page.locator('form').nth(form.index);
      if (!await loc.count()) throw new Error(`form index ${form.index} no longer present on ${form.url}`);
      const filled = await fillForm(page, loc, mode);
      if (mode === 'malformed-email' && !filled.malformedDone) {
        await page.close();
        return { mode, status: 'not-applicable', reason: `form ${form.action || form.url}: no email field for malformed-email case` };
      }
      if (mode === 'oversized' && !filled.oversizedDone) {
        await page.close();
        return { mode, status: 'not-applicable', reason: `form ${form.action || form.url}: no free-text field for oversized case` };
      }
      const obs = await submitAndObserve(page, loc, form);
      const status = obs.response ? obs.response.status : null;
      const backendAccepted = status != null && status < 400;
      result = {
        mode, data: filled.data, status: (obs.submitError || (status != null && status >= 500) || state.errors.length) ? 'failed' : 'passed',
        reason: obs.submitError || state.errors[0] || (status >= 500 ? `HTTP ${status}` : null),
        httpStatus: status, responseUrl: obs.response ? obs.response.url : null, responses: obs.responses,
        clientValid: obs.clientValidity.valid, observedValidation: obs.observedValidation, postUrl: obs.postUrl, domDelta: obs.domDelta,
        backendAccepted,
      };
      if (mode !== 'valid' && backendAccepted && !obs.observedValidation.length) {
        findings.push({ kind: 'validation-gap', form: form.action || form.url, case: mode, detail: `backend answered HTTP ${status} to ${mode} input with no visible validation` });
      }
    } catch (e) {
      result = { mode, status: 'failed', reason: e.message };
    }
    result.screenshot = await shot(page, `form-${mode}`);
    result.durationMs = Date.now() - started;
    for (const e of state.errors) errors.push(`${form.url} form ${form.action} [${mode}]: ${e}`);
    await page.close();
    return result;
  }

  async function runForms(contexts) {
    const all = [...formsSeen.values()];
    const nonDestructive = all.filter((f) => !f.destructive);
    const destructive = all.filter((f) => f.destructive);
    let duplicateDone = false;
    for (const form of nonDestructive) {
      const role = roles.find((r) => r.name === form.role) || roles[0];
      const ctx = contexts.get(role.name);
      const row = { url: form.url, role: role.name, index: form.index, action: form.action, method: form.method, validEmpty: form.validEmpty, destructive: false, fields: form.fields.map((f) => f.name || f.id || f.type), cases: [] };
      if (!isolated) {
        const reason = `form ${form.action || form.url} not submitted: FLEXFACTOR_E2E_ISOLATED not set`;
        skipped.push(reason); row.status = 'constraints-executed'; row.reason = reason;
        addJourney({ kind: 'form', role: role.name, viewport: viewports[0].label, target: `${form.method} ${form.action || form.url}`, status: 'skipped', reason });
        formEvidence.push(row); continue;
      }
      row.status = 'submitted';
      const modes = ['valid', 'empty', 'oversized', 'malformed-email'];
      for (const mode of modes) {
        const c = await withDeadline(runFormCase(role, ctx, form, mode), PAGE_TIMEOUT_MS, `form ${form.action || form.url} [${mode}]`).catch((e) => ({ mode, status: 'failed', reason: e.message }));
        row.cases.push(c);
        if (c.status === 'not-applicable') continue;
        addJourney({ kind: mode === 'valid' ? 'form' : 'form-case', role: role.name, viewport: viewports[0].label, target: `${form.method} ${form.action || form.url} [${mode}]`, status: c.status, reason: c.reason || null, screenshot: c.screenshot || null, httpStatus: c.httpStatus ?? null });
      }
      formEvidence.push(row);
      // duplicate-action check: replay the exact valid payload of the FIRST form the backend accepted
      const valid = row.cases.find((c) => c.mode === 'valid');
      if (!duplicateDone && valid && valid.backendAccepted) {
        duplicateDone = true;
        const c = await withDeadline(runDuplicate(role, ctx, { ...form, replayData: valid.data }), PAGE_TIMEOUT_MS, `form ${form.action || form.url} [duplicate]`).catch((e) => ({ mode: 'duplicate', status: 'failed', reason: e.message }));
        row.cases.push(c);
        addJourney({ kind: 'duplicate', role: role.name, viewport: viewports[0].label, target: `${form.method} ${form.action || form.url} [duplicate]`, status: c.status, reason: c.reason || null, screenshot: c.screenshot || null, httpStatus: c.httpStatus ?? null });
      }
    }
    if (isolated && !duplicateDone && nonDestructive.length) {
      const reason = `duplicate-action check not run: no non-destructive form accepted a valid submission (${nonDestructive.map((f) => f.action || f.url).join(', ')})`;
      incomplete.push(reason);
      addJourney({ kind: 'duplicate', role: roles[0].name, viewport: viewports[0].label, target: 'first accepted form', status: 'skipped', reason });
    }
    for (const form of destructive) {
      const role = roles.find((r) => r.name === form.role) || roles[0];
      const row = { url: form.url, role: role.name, index: form.index, action: form.action, method: form.method, validEmpty: form.validEmpty, destructive: true, fields: form.fields.map((f) => f.name || f.id || f.type), cases: [] };
      if (!isolated) {
        const reason = `destructive form ${form.action || form.url} ("${form.submitLabel}") not submitted: FLEXFACTOR_E2E_ISOLATED not set`;
        skipped.push(reason); row.status = 'blocked-destructive'; row.reason = reason;
        addJourney({ kind: 'destructive', role: role.name, viewport: viewports[0].label, target: `${form.method} ${form.action || form.url}`, status: 'skipped', reason });
        formEvidence.push(row); continue;
      }
      const fresh = await openContext(role, viewports[0]);
      const ok = await withDeadline(performLoginQuiet(role, fresh), PAGE_TIMEOUT_MS, `re-login ${role.name}`).catch(() => false);
      const c = ok ? await withDeadline(runFormCase(role, fresh, form, 'valid'), PAGE_TIMEOUT_MS, `destructive form ${form.action || form.url}`).catch((e) => ({ mode: 'valid', status: 'failed', reason: e.message })) : { mode: 'valid', status: 'failed', reason: `could not re-login role ${role.name} in fresh context` };
      if (!ok) errors.push(c.reason);
      await fresh.close().catch(() => {});
      row.status = 'submitted-destructive'; row.cases.push(c);
      formEvidence.push(row);
      addJourney({ kind: 'destructive', role: role.name, viewport: viewports[0].label, target: `${form.method} ${form.action || form.url} "${form.submitLabel}"`, status: c.status, reason: c.reason || null, screenshot: c.screenshot || null, httpStatus: c.httpStatus ?? null });
    }
  }

  async function performLoginQuiet(role, ctx) {
    if (!role.login) return true;
    try {
      const page = await ctx.newPage();
      page.removeAllListeners('dialog');
      page.on('dialog', (d) => d.dismiss().catch(() => {}));
      await act(page.goto(normalize(role.login.url), { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT }), 'login goto');
      for (const [sel, val] of Object.entries(role.login.fields || {})) await page.fill(sel, String(val), { timeout: 10000 });
      const settle = page.waitForLoadState('load', { timeout: 10000 }).catch(() => {});
      if (role.login.submit) await page.click(role.login.submit, { timeout: 10000 }); else await page.keyboard.press('Enter');
      await settle; await page.close();
      return true;
    } catch { return false; }
  }

  async function runDuplicate(role, ctx, form) {
    const page = await ctx.newPage();
    const state = { errors: [], denied4xx: [] };
    attachListeners(page, state);
    let result;
    try {
      await page.goto(form.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
      const loc = page.locator('form').nth(form.index);
      // replay the same values (only short scalar values can be replayed exactly)
      const data = form.replayData || {};
      for (const [name, value] of Object.entries(data)) {
        if (typeof value !== 'string' || value.includes('…(')) continue;
        const el = loc.locator(`[name="${name.replace(/"/g, '\\"')}"], #${CSS_escape(name)}`).first();
        if (await el.count()) await el.fill(value, { timeout: 3000 }).catch(() => {});
      }
      const obs = await submitAndObserve(page, loc, form);
      const status = obs.response ? obs.response.status : null;
      const backendAccepted = status != null && status < 400;
      result = { mode: 'duplicate', data, status: (obs.submitError || (status != null && status >= 500) || state.errors.length) ? 'failed' : 'passed', reason: obs.submitError || state.errors[0] || null, httpStatus: status, responseUrl: obs.response ? obs.response.url : null, responses: obs.responses, observedValidation: obs.observedValidation, postUrl: obs.postUrl, domDelta: obs.domDelta, backendAccepted, rejected: !backendAccepted };
      findings.push({ kind: 'duplicate-submission', form: form.action || form.url, detail: backendAccepted ? `second identical submission ACCEPTED (HTTP ${status})` : `second identical submission rejected (HTTP ${status})`, rejected: !backendAccepted });
    } catch (e) { result = { mode: 'duplicate', status: 'failed', reason: e.message }; }
    result.screenshot = await shot(page, 'form-duplicate');
    for (const e of state.errors) errors.push(`${form.url} form ${form.action} [duplicate]: ${e}`);
    await page.close();
    return result;
  }
  function CSS_escape(s) { return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`); }

  // ---- destructive controls (isolated only) ---------------------------------------------
  async function runDestructiveControls() {
    for (const item of pendingDestructiveControls) {
      const fresh = await openContext(item.role, viewports[0]);
      const page = await fresh.newPage();
      const state = { errors: [], denied4xx: [] };
      attachListeners(page, state, { acceptDialogs: true });
      const responses = [];
      fresh.on('response', (r) => responses.push({ url: r.url(), status: r.status(), method: r.request().method() }));
      let status = 'passed', error = null, before = null, after = null;
      try {
        const ok = await withDeadline(performLoginQuiet(item.role, fresh), PAGE_TIMEOUT_MS, `re-login ${item.role.name}`).catch(() => false);
        if (!ok) throw new Error(`could not re-login role ${item.role.name} in fresh context`);
        await withDeadline((async () => {
          await page.goto(item.url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
          before = await shot(page, `destructive-before`);
          await page.locator(item.selector).nth(item.index).click({ timeout: 5000 });
          await page.waitForLoadState('load', { timeout: 8000 }).catch(() => {});
          await page.waitForTimeout(SETTLE_MS);
          after = await shot(page, `destructive-after`);
        })(), PAGE_TIMEOUT_MS, `destructive control "${item.label}" on ${item.url}`);
      } catch (e) { status = 'failed'; error = e.message; errors.push(`${item.url}: destructive control "${item.label}": ${e.message}`); }
      controls++;
      controlEvidence.push({ ...item.target, status: status === 'passed' ? 'executed-destructive' : 'failed', error, responses: responses.slice(0, 20), screenshotBefore: before, screenshotAfter: after });
      addJourney({ kind: 'destructive', role: item.role.name, viewport: viewports[0].label, target: `${item.url} "${item.label}"`, status, reason: error, screenshot: after || before, responses: responses.slice(0, 10) });
      if (state.dialogs.length) controlEvidence[controlEvidence.length - 1].dialogs = state.dialogs;
      await act(fresh.close(), 'close destructive context').catch(() => {});
    }
  }

  // ---- viewports ------------------------------------------------------------------------
  async function runViewports() {
    for (const vp of viewports) {
      for (const url of routes) {
        const permittedRow = authz.find((a) => a.route === url && a.outcome === 'permitted');
        const role = permittedRow ? roles.find((r) => r.name === permittedRow.role) : roles[0];
        const ctx = await openContext(role, vp);
        if (role.login) await withDeadline(performLoginQuiet(role, ctx), PAGE_TIMEOUT_MS, `re-login ${role.name} @${vp.label}`).catch(() => {});
        const page = await ctx.newPage();
        const state = { errors: [], denied4xx: [] };
        attachListeners(page, state);
        let status = 'passed', reason = null, overflow = null, screenshot = null;
        try {
          await withDeadline((async () => {
            await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
            overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, overflow: document.documentElement.scrollWidth > window.innerWidth }));
            screenshot = await shot(page, `vp-${vp.label}`);
            if (overflow.overflow) findings.push({ kind: 'horizontal-overflow', route: url, role: role.name, viewport: vp.label, scrollWidth: overflow.scrollWidth, innerWidth: overflow.innerWidth });
          })(), PAGE_TIMEOUT_MS, `viewport ${vp.label} ${url}`);
        } catch (e) { status = 'failed'; reason = e.message; errors.push(`${url} @${vp.label}: ${e.message}`); }
        for (const e of state.errors) errors.push(`${url} @${vp.label} [${role.name}]: ${e}`);
        if (state.errors.length && status === 'passed') { status = 'failed'; reason = state.errors[0]; }
        addJourney({ kind: 'viewport', role: role.name, viewport: vp.label, target: url, status, reason, screenshot, overflow });
        await act(page.close(), 'close viewport page').catch(() => {}); await act(ctx.close(), 'close viewport context').catch(() => {});
      }
    }
  }

  // ---- main -----------------------------------------------------------------------------
  const contexts = new Map();
  try {
    // phase 1: crawl per role (shared route union), controls + form discovery at the primary viewport
    for (const role of roles) {
      const ctx = await openContext(role, viewports[0]);
      await ctx.tracing.start({ screenshots: true, snapshots: true, sources: false }).catch(() => {});
      contexts.set(role.name, ctx);
      progress(`phase1 crawl role=${role.name}`);
      const loggedIn = await withDeadline(performLogin(role, ctx), PAGE_TIMEOUT_MS, `login ${role.name}`).catch((e) => { errors.push(`login ${role.name}: ${e.message}`); return false; });
      if (!loggedIn) { incomplete.push(`role ${role.name} could not log in; its matrix rows are unverified`); }
      const visited = new Set();
      let cursor = 0;
      while (cursor < routes.length) {
        const url = routes[cursor++];
        if (visited.has(url)) continue;
        visited.add(url);
        progress(`visit ${url} [${role.name}]`);
        await guardedVisit(role, ctx, url, { crawl: true });
      }
    }
    // phase 2: fill the authorization matrix for routes a role never reached during its own crawl
    progress('phase2 authorization matrix fill');
    for (const role of roles) {
      const ctx = contexts.get(role.name);
      for (const url of routes) {
        if (!authz.some((a) => a.route === url && a.role === role.name)) await guardedVisit(role, ctx, url, { crawl: false });
      }
    }
    // phase 3: forms (non-destructive first, duplicate check, then destructive in fresh contexts)
    progress(`phase3 forms (${formsSeen.size} discovered, isolated=${isolated})`);
    await runForms(contexts);
    // phase 4: viewports
    progress(`phase4 viewports (${routes.length} routes x ${viewports.length})`);
    await runViewports();
    // phase 5: destructive controls last, each in a fresh context
    progress(`phase5 destructive controls (${pendingDestructiveControls.length} pending)`);
    await runDestructiveControls();
    progress('phases done');
  } catch (e) {
    errors.push(`explorer crashed: ${e.stack || e.message}`);
    incomplete.push(`explorer crashed before finishing: ${e.message}`);
  } finally {
    // teardown runs on EVERY path (cap reached, crash, normal) and is itself deadline-bounded
    progress('teardown: traces + contexts + browser');
    for (const [name, ctx] of contexts) {
      const file = `playwright-trace-${name}.zip`;
      await withDeadline(ctx.tracing.stop({ path: path.join(artifactDir, file) }), Math.max(ACTION_TIMEOUT_MS, 30000), `tracing.stop ${name}`).then(() => artifacts.push(file)).catch((e) => errors.push(`trace ${name}: ${e.message}`));
      await act(ctx.close(), `context.close ${name}`).catch(() => {});
    }
    if (browser) await act(browser.close(), 'browser.close').catch((e) => { errors.push(`browser.close: ${e.message}`); });
    clearTimeout(watchdog);
  }

  const result = buildResult();
  progress(`done complete=${result.complete} journeys=${result.summary.total} errors=${errors.length} skipped=${skipped.length} timeouts=${timeouts.length}`);
  emit('FLEXFACTOR_E2E_RESULT=' + JSON.stringify(result), result.complete ? 0 : 1);
})().catch((e) => {
  console.error(e.stack || String(e));
  // even a crash before the engine body emits a machine-readable, complete=false result
  if (!emitted) emit('FLEXFACTOR_E2E_RESULT=' + JSON.stringify({ pages: 0, controls: 0, errors: [`explorer crashed: ${e.message}`], skipped: [], routeEvidence: [], controlEvidence: [], formEvidence: [], accessibility: { checked: 0, violations: [] }, performance: { pages: [], slow: [] }, artifacts: [], journeys: [], authorization_matrix: [], findings: [], summary: { passed: 0, failed: 0, skipped: 0, total: 0 }, incomplete_reasons: [`explorer crashed: ${e.message}`], complete: false }), 2);
  else process.exit(2);
});
