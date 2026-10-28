// ทดสอบ logic ของหน้าเว็บ (web/app.js + chart.js + i18n.js) ด้วย gjs + DOM จำลอง — ไม่ต้องมีเบราว์เซอร์
//
// รัน:  gjs -m test/ui_harness.js <โฟลเดอร์แพ็กเกจ>      (test_ui_logic.py เรียกให้อัตโนมัติ)
//
// DOM จำลองสร้างจาก web/index.html จริง (ประกอบจาก pages/*.html แล้ว) (id / hidden / class / data-*) → ถ้า app.js อ้าง id ที่ไม่มีใน HTML
// จะพังทันที ; มีตัวนับการเขียน innerHTML (ต้องเป็น 0 — กัน XSS จากข้อมูลภายนอก)
// UI ต้องเป็นอังกฤษล้วนเมื่ออยู่โหมด EN (สแกนตัวอักษรไทยทุก element) และสลับ TH ได้โดยไม่ต้องโหลดหน้าใหม่
const { GLib } = imports.gi;
const System = imports.system;

const ROOT = ARGV[0];
const read = (rel) => new TextDecoder().decode(GLib.file_get_contents(`${ROOT}/${rel}`)[1]);
// index.html / app.js เป็นต้นแบบที่มีจุดแทรก (pages/*.html, js/*.js) — ประกอบเหมือน agv_web_app/web_assets.py ที่เซิร์ฟเวอร์ใช้
const INCLUDE_RE = /^[ \t]*(?:<!--[ \t]*@include[ \t]+(\S+?)[ \t]*-->|\/\/[ \t]*@include[ \t]+(\S+?))[ \t]*\r?\n/gm;
const ALLOWED_RE = /^(?:pages\/[A-Za-z0-9_-]+\.html|js\/[A-Za-z0-9_-]+\.js)$/;
const assemble = (rel) => read(rel).replace(INCLUDE_RE, (_m, htmlPiece, jsPiece) => {
  const piece = htmlPiece || jsPiece;
  if (!ALLOWED_RE.test(piece)) throw new Error(`จุดแทรกไม่ถูกต้อง: ${piece}`);
  return read(`web/${piece}`);
});
const html = assemble('web/index.html');
const appSource = assemble('web/app.js');
const chartSource = read('web/chart.js');
const i18nSource = read('web/i18n.js');

// ── ตัวนับผลทดสอบ ───────────────────────────────────────────────────
let failures = 0;
let passes = 0;
function check(name, cond, detail) {
  if (cond) { passes++; return; }
  failures++;
  print(`FAIL - ${name}${detail === undefined ? '' : '  → ' + detail}`);
}

// ── DOM จำลอง ───────────────────────────────────────────────────────
let innerHTMLWrites = 0;
class El {
  constructor(tag, id) {
    this.tag = tag; this.id = id; this.attrs = {}; this.children = []; this.listeners = {};
    this._text = ''; this.hidden = false; this.disabled = false; this.className = '';
    this.style = {}; this.clientWidth = 320; this.value = ''; this.onclick = null; this.onclose = null;
    this.open = false; this.focused = false; this.scrolled = 0;
    const self = this;
    this.classList = {
      toggle(token, force) {
        const tokens = self.className.split(/\s+/).filter(Boolean);
        const has = tokens.includes(token);
        const want = force === undefined ? !has : !!force;
        if (want && !has) tokens.push(token);
        if (!want && has) tokens.splice(tokens.indexOf(token), 1);
        self.className = tokens.join(' ');
      },
    };
  }
  set innerHTML(_v) { innerHTMLWrites++; }
  // เหมือน DOM จริง: อ่าน = รวมข้อความของลูกทั้งหมด ; เขียน = แทนที่ลูกทั้งหมดด้วยข้อความ
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') this.className = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, ev = {}) {
    (this.listeners[type] || []).forEach((fn) => fn(Object.assign({ preventDefault() {} }, ev)));
  }
  click() {
    if (this.disabled) return;
    if (this.onclick) this.onclick();
    this.dispatch('click');
  }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled++; }
  getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth || 320, height: this.clientHeight || this.clientWidth || 320 }; }
  getContext() {
    if (!this._ctx) {
      const log = this.ctxLog = [];
      const rec = (name) => (...args) => { log.push({ name, args }); };
      this._ctx = {};
      ['setTransform', 'clearRect', 'save', 'restore', 'translate', 'rotate', 'drawImage', 'beginPath', 'moveTo',
        'lineTo', 'closePath', 'stroke', 'fill', 'fillRect', 'fillText', 'arc', 'strokeText', 'setLineDash'].forEach((n) => { this._ctx[n] = rec(n); });
      ['fillStyle', 'strokeStyle', 'globalAlpha'].forEach((prop) => {      // บันทึกการกำหนดสี/ความโปร่งใส
        let value;
        Object.defineProperty(this._ctx, prop, { get: () => value, set: (v) => { value = v; log.push({ name: 'set:' + prop, args: [v] }); } });
      });
    }
    return this._ctx;
  }
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    if (this.onclose) this.onclose();
    this.dispatch('close');
  }
}

const byId = {};
const sections = [];
const segButtons = [];
const langButtons = [];
const i18nNodes = [];        // { key, el, inline } — ข้อความอังกฤษตั้งต้นในไฟล์ HTML
const i18nArias = [];        // { key, el, inline }
for (const m of html.replace(/<!--[\s\S]*?-->/g, '').matchAll(/<(\w+)([^>]*)>/g)) {
  const tag = m[1], attrs = m[2];
  const isHidden = /(^|\s)hidden(\s|=|$)/.test(attrs);
  const get = (k) => { const r = new RegExp(`(?:^|\\s)${k}="([^"]*)"`).exec(attrs); return r ? r[1] : null; };
  const id = get('id');
  let el = null;
  if (id) { el = byId[id] = new El(tag, id); }
  if (get('data-view') !== null) { el = el || new El(tag, null); el.attrs['data-view'] = get('data-view'); sections.push(el); }
  const cls = get('class') || '';
  if (/\bseg-btn\b/.test(cls)) { el = new El(tag, null); el.attrs['data-mode'] = get('data-mode'); segButtons.push(el); }
  if (/\blang-btn\b/.test(cls)) {
    el = new El(tag, null); el.attrs['data-lang'] = get('data-lang');
    el.attrs['aria-pressed'] = get('aria-pressed'); langButtons.push(el);
  }
  if (get('data-i18n') !== null) {
    el = el || new El(tag, null);
    const inner = new RegExp(`<${tag}[^>]*data-i18n="${get('data-i18n')}"[^>]*>([\\s\\S]*?)</${tag}>`).exec(html);
    el.attrs['data-i18n'] = get('data-i18n');
    el._text = inner ? inner[1].replace(/\s+/g, ' ').trim() : '';
    i18nNodes.push({ key: get('data-i18n'), el, inline: el._text });
  }
  if (get('data-i18n-aria') !== null) {
    el = el || new El(tag, null);
    el.attrs['data-i18n-aria'] = get('data-i18n-aria');
    el.attrs['aria-label'] = get('aria-label');
    i18nArias.push({ key: get('data-i18n-aria'), el, inline: get('aria-label') });
  }
  if (el) {
    el.hidden = isHidden; if (cls) el.className = cls;
    // ข้อความตั้งต้นของ element ที่มีข้อความตรงๆ (ไม่มีลูก tag) เพื่อสแกนตัวอักษรไทย
    if (id && !el._text) {
      const inner = new RegExp(`<${tag}[^>]*id="${id}"[^>]*>([^<]+)</${tag}>`).exec(html);
      if (inner) el._text = inner[1].replace(/\s+/g, ' ').trim();
    }
  }
}

const docListeners = {};
const documentElement = new El('html', null);
const document = {
  hidden: false,
  documentElement,
  getElementById: (id) => byId[id] || null,
  querySelectorAll: (sel) => {
    if (sel === '[data-view]') return sections;
    if (sel === '.seg-btn') return segButtons;
    if (sel === '.lang-btn') return langButtons;
    if (sel === '[data-i18n]') return i18nNodes.map((n) => n.el);
    if (sel === '[data-i18n-aria]') return i18nArias.map((n) => n.el);
    return [];
  },
  createElement: (tag) => new El(tag, null),
  createElementNS: (ns, tag) => { const e = new El(tag, null); e.ns = ns; return e; },
  createTextNode: (t) => { const n = new El('#text', null); n.textContent = t; return n; },
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
};
const winListeners = {};
const storage = {};
const window = {
  document,
  addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); },
  scrollTo() {},
  matchMedia: () => ({ matches: false }),
  localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = String(v); } },
};
const location = { hash: '', protocol: 'http:', host: 'robot.local:8080' };
const realNow = Date.now.bind(Date);
let fakeDate = false;                    // เปิดเฉพาะเทสต์ที่ต้องคุมเวลาของหน้าเว็บ (Decay time ของสแกนเลเซอร์)
Date.now = () => (fakeDate ? 1.8e12 + now : realNow());

// ── นาฬิกาปลอม (คุมเวลาเอง) ─────────────────────────────────────────
let now = 0, nextTimer = 1;
const timers = new Map();
const setTimeoutF = (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, at: now + ms, every: 0 }); return id; };
const setIntervalF = (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, at: now + ms, every: ms }); return id; };
const clearTimer = (id) => { timers.delete(id); };
async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    let nextId = null, nextAt = Infinity;
    for (const [id, t] of timers) if (t.at <= end && t.at < nextAt) { nextId = id; nextAt = t.at; }
    if (nextId === null) break;
    const t = timers.get(nextId);
    now = t.at;
    if (t.every) t.at += t.every; else timers.delete(nextId);
    t.fn();
    await flush();
  }
  now = end;
  await flush();
}

// ── เซิร์ฟเวอร์ปลอม (fetch / WebSocket) ─────────────────────────────
const M = (code, params = {}) => ({ code, params });
const server = {
  session: { status: 200, data: { ok: true, admin: false, auth_enabled: true } },
  login: [], mode: null, calls: [], history: null, stats: null, reset: null, map: null, modeReset: null, slamState: null, slamPost: null,
  scan: null, saved: null, saveHandler: null, driveStop: null,
  selected: null, selectHandler: null, renameHandler: null, deleteHandler: null,
  navInfo: null, navLive: null, navSave: null, navApply: null, poseHandler: null, goalGet: null, goalHandler: null, goalCancel: null, startGet: null, startHandler: null, refreshHandler: null, straightCheck: null, straightHandler: null,
  routes: null, routesHandler: null, routesDelete: null, routeMap: null, runGet: null, runHandler: null, runControl: null,
  holdPath: null, releaseHeld: null,        // ถ่วงคำตอบของ path หนึ่งไว้ (ทดสอบคำตอบที่มาช้า) ; releaseHeld() = ปล่อยคำตอบ
};
function makeHistory(kind = 'normal') {
  const t = [], v = [], w = [];
  for (let i = 0; i < 60; i++) {
    t.push(-118 + i * 2);
    if (kind === 'empty') { v.push(null); w.push(null); continue; }
    v.push(i >= 40 && i < 45 ? null : (i < 20 ? 0 : Math.min(0.2, 0.01 * (i - 19))));   // มีช่องว่างกลางเส้น
    w.push(i >= 40 && i < 45 ? null : Math.round(Math.sin(i / 5) * 50) / 100);
  }
  return { window: 120, now: 1793000000, t, v, w };
}
const NOW = 1793000000;
// ค่า Nav2 ที่ปรับได้ (โครงเดียวกับ /api/nav/params จริง: กลุ่ม, ช่วง, Default จาก nav2_params_s2.yaml)
const NAVP = [
  ['speed', 'desired_linear_vel', 'float', 'm/s', 0.05, 0.5, 0.05, 0.2],
  ['speed', 'rotate_to_heading_angular_vel', 'float', 'rad/s', 0.1, 1.0, 0.05, 0.5],
  ['speed', 'max_angular_accel', 'float', 'rad/s²', 0.2, 3.2, 0.1, 0.8],
  ['path', 'lookahead_dist', 'float', 'm', 0.3, 2.0, 0.05, 0.8],
  ['path', 'min_lookahead_dist', 'float', 'm', 0.2, 1.5, 0.05, 0.5],
  ['path', 'max_lookahead_dist', 'float', 'm', 0.5, 3.0, 0.05, 1.2],
  ['path', 'lookahead_time', 'float', 's', 0.5, 3.0, 0.1, 1.5],
  ['obstacle', 'use_collision_detection', 'bool', '', null, null, null, true],
  ['obstacle', 'max_allowed_time_to_collision_up_to_carrot', 'float', 's', 0.5, 5.0, 0.1, 3.0],
  ['goal', 'xy_goal_tolerance', 'float', 'm', 0.05, 0.5, 0.01, 0.15],
  ['goal', 'yaw_goal_tolerance', 'float', 'rad', 0.03, 0.5, 0.01, 0.1],
  ['costmap', 'inflation_radius', 'float', 'm', 0.35, 1.5, 0.05, 0.55],
  ['costmap', 'cost_scaling_factor', 'float', '', 1.0, 10.0, 0.5, 3.0],
];
const NAVP_DEFAULTS = Object.fromEntries(NAVP.map((r) => [r[1], r[7]]));
function makeNavInfo(saved = {}, over = {}) {
  const groups = ['speed', 'path', 'obstacle', 'goal', 'costmap'].map((id) => ({
    id, params: NAVP.filter((r) => r[0] === id).map(([, key, type, unit, min, max, step, def]) => {
      const value = saved[key] !== undefined ? saved[key] : def;
      return { key, type, unit, min, max, step, default: def, value, modified: value !== def };
    }),
  }));
  return Object.assign({ available: true, groups, modified: Object.keys(saved).filter((k) => saved[k] !== NAVP_DEFAULTS[k]).length,
    in_use: null, restart_needed: false }, over);
}
function makeSel(over = {}) {
  return Object.assign({ name: null, available: false, choice: { source: 'default', name: null, file: null },
    in_use: null, restart_needed: false }, over);
}
function makeStats(over = {}) {
  const h = 3600;
  return Object.assign({
    now: NOW, window: 86400,
    today: { date: '2026-10-26', distance_m: 12.34, active_s: 3725 },
    total: { distance_m: 1234.5, active_s: 90000, since: NOW - 5 * 86400 },
    days: [0, 0, 250, 0, 1234.5, 80, 12.34].map((m, i) => ({ date: `2026-10-${20 + i}`, distance_m: m,
      active_s: [0, 0, 1800, 0, 7200, 600, 3725][i] })),
    rounds: null, persist: { ok: true, note: null },
    timeline: [
      { start: NOW - 20 * h, end: NOW - 19 * h, mode: 'MAPPING', open: false },
      { start: NOW - 3 * h, end: NOW - 2 * h - 1800, mode: 'MANUAL', open: false },
      { start: NOW - 600, end: NOW, mode: 'NAVIGATION', open: true },
    ],
  }, over);
}
// ค่า SLAM จำลอง (โครงเดียวกับ /api/slam/params จริง ใช้ชื่อ key จริงเพื่อให้มีคำแปล)
function makeSlam(saved = {}, over = {}) {
  const P = (key, type, unit, min, max, def) => ({ key, type, unit, min, max, step: 0.01, default: def,
    value: saved[key] !== undefined ? saved[key] : def, modified: saved[key] !== undefined && saved[key] !== def });
  const groups = [
    { id: 'map', params: [P('resolution', 'float', 'm', 0.02, 0.2, 0.05), P('max_laser_range', 'float', 'm', 5, 30, 25)] },
    { id: 'update', params: [P('throttle_scans', 'int', '', 1, 10, 1)] },
    { id: 'loop', params: [P('do_loop_closing', 'bool', '', null, null, true), P('loop_search_maximum_distance', 'float', 'm', 1, 20, 8)] },
  ];
  return Object.assign({ available: true, groups, advanced: [{ key: 'scan_topic', value: '/scan_filtered' }, { key: 'odom_frame', value: 'odom' }],
    modified: groups.reduce((n, g) => n + g.params.filter((p) => p.modified).length, 0), in_use: null }, over);
}
function slamFetch(body) {
  if (!body) return server.slamState ? { status: 200, data: server.slamState } : { status: 200, data: makeSlam() };
  if (server.slamPost) return server.slamPost;
  const saved = {};
  const cur = server.slamState || makeSlam();
  cur.groups.forEach((g) => g.params.forEach((p) => { if (p.modified) saved[p.key] = p.value; }));
  Object.entries(body.values).forEach(([k, v]) => {
    const def = cur.groups.flatMap((g) => g.params).find((p) => p.key === k).default;
    if (v === def) delete saved[k]; else saved[k] = v;
  });
  server.slamState = makeSlam(saved);
  return { status: 200, data: { ok: true, params: server.slamState } };
}
function fetchF(path, opts) {
  const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
  server.calls.push({ path, method: (opts && opts.method) || 'GET', body, headers: opts && opts.headers });
  let r = { status: 404, data: {} };
  if (path === '/api/session') r = server.session;
  else if (path === '/api/login') r = server.login.shift() || { status: 401, data: { ok: false, error: M('auth.bad_pin') } };
  else if (path === '/api/mode') r = server.mode || { status: 202, data: { ok: true, message: M('mode.accepted', { mode: body.mode }) } };
  else if (path.startsWith('/api/history')) r = server.history || { status: 200, data: makeHistory() };
  else if (path === '/api/scan/live') r = server.scan || { status: 200, data: { available: false, version: 0 } };
  else if (path === '/api/map/saved') r = server.saved || { status: 200, data: { maps: [], last: { hash: null, name: null, time: null } } };
  else if (path === '/api/map/save') r = server.saveHandler ? server.saveHandler(body) : { status: 200, data: { ok: true, name: body.name, bytes: 1234, saved: NOW, path: '/x' } };
  else if (path === '/api/map/selected') r = server.selected || { status: 200, data: makeSel() };
  else if (path === '/api/map/select') r = server.selectHandler ? server.selectHandler(body) : { status: 200, data: Object.assign(makeSel({ name: body.name, available: !!body.name }), { ok: true }) };
  else if (path === '/api/map/rename') r = server.renameHandler ? server.renameHandler(body) : { status: 200, data: { ok: true, name: body.new_name } };
  else if (path === '/api/map/delete') r = server.deleteHandler ? server.deleteHandler(body) : { status: 200, data: { ok: true, name: body.name } };
  else if (path === '/api/routes/map') r = server.routeMap || server.map || { status: 200, data: { available: false, version: 0 } };
  else if (path === '/api/routes/run') r = body ? (server.runHandler ? server.runHandler(body) : { status: 404, data: {} }) : (server.runGet || { status: 200, data: { run: { state: 'idle', run: 0, route: null, map: null, loop: 1, round: 0, index: 0, total: 0, current: null, phase: null, wait_left: null, elapsed: null, reason: null, max_run: 7200, point_timeout: 600, pause_settled: true }, goal: { state: 'idle', seq: 0, goal: null, reason: null, feedback: null, elapsed: null, waiting_assumed: false }, running: false } });
  else if (path.startsWith('/api/routes/run/')) r = server.runControl ? server.runControl(path.split('/').pop(), body) : { status: 404, data: {} };
  else if (path === '/api/routes/delete') r = server.routesDelete ? server.routesDelete(body) : { status: 404, data: {} };
  else if (path === '/api/routes') r = body ? (server.routesHandler ? server.routesHandler(body) : { status: 404, data: {} })
    : (server.routes || { status: 200, data: { map: null, routes: {}, running: false, limits: { max_points: 50, max_name: 40, max_wait: 600, max_loop: 99, clearance: 0.4 } } });
  else if (path === '/api/nav/refresh') r = server.refreshHandler ? server.refreshHandler(body) : { status: 200, data: { ok: true } };
  else if (path === '/api/nav/startpose') r = body ? (server.startHandler ? server.startHandler(body) : { status: 404, data: {} })
    : (server.startGet || { status: 200, data: { map: null, pose: null, auto: { state: 'idle', error: null, name: null } } });
  else if (path === '/api/nav/line/check') r = server.lineCheck ? server.lineCheck(body) : { status: 200, data: { ok: true, check: { ok: true, at: null, reason: null, min_clearance: null, soft_risk: false, length: 2.24, goal: body.to, start: { distance: 0.05, heading_deg: 2.0, ok: true, max_distance: 0.3, max_heading_deg: 20 } } } };
  else if (path === '/api/nav/line') r = server.lineHandler ? server.lineHandler(body) : { status: 200, data: { ok: true, goal: { state: 'active', seq: 7, goal: body.to, line: { x0: 1, y0: 1 }, reason: null, feedback: null, elapsed: 0, waiting_assumed: false }, check: { ok: true } } };
  else if (path === '/api/nav/goal/straight/check') r = server.straightCheck ? server.straightCheck(body) : { status: 200, data: { ok: true, check: { ok: true, at: null, reason: null, min_clearance: null, soft_risk: false, distance: body.distance, goal: { x: 1.0, y: 2.0 + body.distance, yaw: Math.PI / 2 } } } };
  else if (path === '/api/nav/goal/straight') r = server.straightHandler ? server.straightHandler(body) : { status: 200, data: { ok: true, goal: { state: 'active', seq: 1, goal: { x: 1.0, y: 2.0 + body.distance, yaw: Math.PI / 2 }, reason: null, feedback: null, elapsed: 0, waiting_assumed: false }, check: { ok: true } } };
  else if (path === '/api/nav/goal/cancel') r = server.goalCancel ? server.goalCancel(body) : { status: 200, data: { ok: true, goal: { state: 'canceling', seq: 1, goal: { x: 1, y: 1, yaw: 0 }, reason: null, feedback: null, elapsed: 3, waiting_assumed: false } } };
  else if (path === '/api/nav/goal') r = body ? (server.goalHandler ? server.goalHandler(body) : { status: 200, data: { ok: true, goal: { state: 'active', seq: 1, goal: { x: body.x, y: body.y, yaw: body.yaw }, reason: null, feedback: null, elapsed: 0, waiting_assumed: false } } })
    : (server.goalGet || { status: 200, data: { running: true, goal: { state: 'idle', seq: 0, goal: null, reason: null, feedback: null, elapsed: null, waiting_assumed: false } } });
  else if (path === '/api/nav/initialpose') r = server.poseHandler ? server.poseHandler(body) : { status: 200, data: { ok: true, pose: { x: body.x, y: body.y, yaw: body.yaw, yaw_deg: body.yaw * 180 / Math.PI } } };
  else if (path === '/api/nav/params/live') r = server.navLive || { status: 200, data: { running: false, values: null, mixed: [] } };
  else if (path === '/api/nav/params/apply') r = server.navApply ? server.navApply(body) : { status: 404, data: {} };
  else if (path === '/api/nav/params') r = body ? (server.navSave ? server.navSave(body) : { status: 404, data: {} }) : (server.navInfo || { status: 200, data: makeNavInfo() });
  else if (path === '/api/drive/stop') r = server.driveStop || { status: 200, data: { ok: true } };
  else if (path === '/api/slam/params') r = slamFetch(body);
  else if (path === '/api/mode/reset') r = server.modeReset || { status: 202, data: { ok: true, message: M('mode.resetting'),
    mode: { mode: 'MAPPING', state: 'STOPPING', target: 'STANDBY', message: M('mode.resetting'), external: [], leftover: null } } };
  else if (path === '/api/map/live') r = server.map || { status: 200, data: { available: false, version: 0 } };
  else if (path === '/api/stats') r = server.stats || { status: 200, data: makeStats() };
  else if (path === '/api/stats/reset') r = server.reset || { status: 200, data: { ok: true, stats: makeStats({
    today: { date: '2026-10-26', distance_m: 0, active_s: 0 }, total: { distance_m: 0, active_s: 0, since: NOW },
    days: Array.from({ length: 7 }, (_, i) => ({ date: `2026-10-${20 + i}`, distance_m: 0, active_s: 0 })), timeline: [] }) } };
  else if (path === '/api/logout') r = { status: 200, data: { ok: true } };
  const response = { status: r.status, json: () => Promise.resolve(r.data) };
  if (server.holdPath === path) { return new Promise((resolve) => { server.releaseHeld = () => resolve(response); }); }
  return Promise.resolve(response);
}
const images = [];
let imageFails = false, imageHold = false;
class ImageF {
  set src(v) { this._src = v; images.push(this); if (imageHold) return; Promise.resolve().then(() => { if (imageFails) { if (this.onerror) this.onerror(); } else if (this.onload) this.onload(); }); }
  get src() { return this._src; }
}
let wsCount = 0, ws = null;
const driveSockets = [];                // ซ็อกเก็ตขับด้วยมือ (/ws/drive) แยกจากซ็อกเก็ตสถานะ (/ws)
class WebSocketF {
  constructor(url) {
    this.url = url; this.readyState = 0; this.sent = [];
    if (/\/ws\/drive$/.test(url)) driveSockets.push(this); else { ws = this; wsCount++; }
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
}
// ช่วยทดสอบซ็อกเก็ตขับ: เปิด + ให้เซิร์ฟเวอร์ตอบ ready
const driveSock = () => driveSockets[driveSockets.length - 1];
async function driveOpen(limits = { max_linear: 0.5, max_angular: 1.0, default_linear: 0.25, default_angular: 0.6, heartbeat_timeout: 0.3 }) {
  const s = driveSock(); s.readyState = 1; if (s.onopen) s.onopen();
  s.onmessage({ data: JSON.stringify({ t: 'ready', limits }) }); await flush();
  return s;
}
const driveReply = async (s, obj) => { s.onmessage({ data: JSON.stringify(obj) }); await flush(); };

function statusMsg(over = {}) {
  const base = {
    mode: { mode: 'STANDBY', target: null, state: 'RUNNING', message: null, since: 1793000000, log: '', external: [], managed: true },
    readiness: { ready: true, headline: M('ready.headline.ready'), hint: null,
      items: [{ key: 'nav2', level: 'ok', text: M('ready.nav2.ok'), hint: null }] },
    activity: { state: 'idle', level: 'ok', text: M('act.idle'), detail: M('act.idle.navigation') },
    speed: null, localization: { quality: 'unknown', sigma: null },
    map: { available: false, unsaved: false, saved_name: null },
  };
  const out = Object.assign({}, base, over);
  if (over.mode) out.mode = Object.assign({}, base.mode, over.mode);
  return out;
}
const send = async (over) => { ws.onmessage({ data: JSON.stringify(statusMsg(over)) }); await flush(); };
const $ = (id) => byId[id];
const hasClass = (el, c) => el.className.split(/\s+/).includes(c);
const seg = (mode) => segButtons.find((b) => b.attrs['data-mode'] === mode);
const langBtn = (l) => langButtons.find((b) => b.attrs['data-lang'] === l);
const states = () => segButtons.map((b) => `${b.attrs['data-mode']}:${b.disabled ? 'off' : 'on'}`).join(' ');
const posts = (path) => server.calls.filter((c) => c.method === 'POST' && c.path === path);
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const THAI = /[฀-๿]/;

// ข้อความที่ผู้ใช้เห็นทั้งหมดในหน้า (ข้อความ + aria-label ของทุก element + ลูก) ยกเว้นปุ่มชื่อภาษา "ไทย"
function visibleTexts() {
  const out = [];
  const walk = (el) => {
    if (el.tag === '#text') { out.push(el.textContent); return; }
    if (!el.attrs['data-lang'] && !el.children.length && el._text) out.push(el._text);
    if (el.attrs['aria-label']) out.push(el.attrs['aria-label']);
    el.children.forEach(walk);
  };
  Object.values(byId).forEach(walk);
  sections.forEach((s) => { if (!s.id) walk(s); });
  i18nNodes.forEach((n) => walk(n.el));
  i18nArias.forEach((n) => walk(n.el));
  return out;
}
const thaiLeaks = () => visibleTexts().filter((t) => THAI.test(t));

async function main() {
  const I = {};
  // โหลด i18n.js → chart.js → app.js จริง (IIFE) ด้วย globals จำลอง
  new Function('window', 'document', i18nSource)(window, document);
  Object.assign(I, window.AgvI18n);
  new Function('window', 'document', chartSource)(window, document);
  new Function('document', 'window', 'location', 'WebSocket', 'fetch', 'setTimeout', 'setInterval',
    'clearTimeout', 'clearInterval', 'console', 'Image', appSource)(
    document, window, location, WebSocketF, fetchF, setTimeoutF, setIntervalF, clearTimer, clearTimer, console, ImageF);
  await flush();

  // ═════════ 0) พจนานุกรม 2 ภาษา + ข้อความตั้งต้นใน HTML ═════════
  const en = I.DICT.en, th = I.DICT.th;
  const keysEn = Object.keys(en).sort(), keysTh = Object.keys(th).sort();
  check('0 en/th มี key ชุดเดียวกัน', eq(keysEn, keysTh), JSON.stringify({ onlyEn: keysEn.filter((k) => !(k in th)), onlyTh: keysTh.filter((k) => !(k in en)) }));
  const params = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
  const badParams = keysEn.filter((k) => !eq(params(en[k]), params(th[k] || '')));
  check('0 พารามิเตอร์ {x} ตรงกันทุก key', badParams.length === 0, badParams.join(','));
  check('0 ไม่มีคำแปลว่าง', keysEn.every((k) => en[k].trim() && (th[k] || '').trim()));
  const enThai = keysEn.filter((k) => THAI.test(en[k]));
  check('0 พจนานุกรม EN ไม่มีตัวอักษรไทย', enThai.length === 0, enThai.join(','));
  const thNoThai = keysEn.filter((k) => !THAI.test(th[k] || '') && !/^(time\.locale|mode\.name\.|stats\.tl\.seg$|ui\.map\.title$|ui\.mode\.reset$|disp\.pct$)/.test(k));
  check('0 คำแปลไทยมีตัวอักษรไทยจริง (ยกเว้น locale/ชื่อโหมด)', thNoThai.length === 0, thNoThai.join(','));
  const missingKeys = i18nNodes.concat(i18nArias).filter((n) => !(n.key in en)).map((n) => n.key);
  check('0 ทุก data-i18n ใน HTML มี key ในพจนานุกรม', missingKeys.length === 0, missingKeys.join(','));
  const drift = i18nNodes.filter((n) => n.inline !== en[n.key]).map((n) => `${n.key}: "${n.inline}" ≠ "${en[n.key]}"`)
    .concat(i18nArias.filter((n) => n.inline !== en[n.key]).map((n) => `${n.key}(aria): "${n.inline}" ≠ "${en[n.key]}"`));
  check('0 ข้อความอังกฤษใน HTML ตรงกับพจนานุกรม EN', drift.length === 0, drift.join(' | '));
  // ทุก key ที่ถูกอ้างเป็นสตริงตายตัวใน app.js / chart.js ต้องมีในพจนานุกรม (key ที่ลงท้าย . = ส่วนหน้าของ key ที่ประกอบสด)
  const keyLike = /'((?:ui|toast|pin|logout|mode|hint|pill|confirm|chip|quality|chart|act|ready|api|auth|time|net|stats)\.[\w.]*)'/g;
  const usedKeys = [...new Set([...appSource.matchAll(keyLike), ...chartSource.matchAll(keyLike)].map((m) => m[1]))];
  const missingUsed = usedKeys.filter((k) => !k.endsWith('.') && !(k in en));
  check('0 ทุก key ที่ใช้ใน app.js/chart.js มีในพจนานุกรม', missingUsed.length === 0 && usedKeys.length > 30, missingUsed.join(',') + ` (${usedKeys.length} keys)`);
  const dynMissing = [];
  ['STANDBY', 'MAPPING', 'MANUAL', 'NAVIGATION'].forEach((m) => ['mode.desc.', 'mode.name.'].forEach((p) => { if (!(p + m in en)) dynMissing.push(p + m); }));
  ['nav2', 'sensors', 'localization'].forEach((k) => { if (!('chip.' + k in en)) dynMissing.push('chip.' + k); });
  ['good', 'fair', 'bad', 'unknown'].forEach((q) => { if (!('quality.' + q in en)) dynMissing.push('quality.' + q); });
  check('0 key ที่ประกอบจากชื่อ (โหมด/ชิป/คุณภาพ) ครบ', dynMissing.length === 0, dynMissing.join(','));
  check('0 tm(): ข้อความจาก backend + ชื่อโหมดถูกแปล',
    I.tm(M('mode.err.timeout', { mode: 'MAPPING', seconds: 40, log: '/x.log' })) === 'Mapping mode did not start within 40 s. Log: /x.log');
  check('0 tm(): list → คั่นด้วยจุลภาค', I.tm(M('ready.sensors.bad', { missing: ['LiDAR', 'IMU'] })) === 'Sensors not responding: LiDAR, IMU');
  check('0 tm(): ค่าว่าง/สตริง/code ไม่รู้จัก', I.tm(null) === '' && I.tm('plain') === 'plain' && I.tm(M('no.such.code')) === 'no.such.code');
  check('0 ภาษาเริ่มต้น = en และ <html lang="en">', I.getLang() === 'en' && documentElement.attrs.lang === 'en');
  check('0 ข้อความตั้งต้นทุก data-i18n ตรงกับ EN หลัง init', i18nNodes.every((n) => n.el.textContent === en[n.key]));

  ws.onopen();

  // ═════════ 1) เริ่มต้น (ภาษาอังกฤษ) ═════════
  check('1 ก่อนมีข้อมูล: Connecting… / Waiting for robot data…', $('pill-text').textContent === 'Connecting…' && $('act-text').textContent === 'Waiting for robot data…',
    $('pill-text').textContent);
  await send();
  check('1 ป้ายโหมดบนแถบบน: Standby สีเทา', $('pill-text').textContent === 'Standby' && hasClass($('mode-pill'), 'off'), $('pill-text').textContent);
  check('1 ป้ายโหมด: ไม่มีตัวหมุน', $('pill-spin').hidden === true);
  check('1 ชื่อโหมดในการ์ด', $('mode-name').textContent === 'Standby' && $('mode-th').textContent === 'Standby — all systems off', $('mode-th').textContent);
  check('1 ป้ายสถานะโหมด', $('mode-state').textContent === 'Standby', $('mode-state').textContent);
  check('1 ทุกปุ่มโหมดกดไม่ได้', segButtons.every((b) => b.disabled), states());
  check('1 hint บอกให้ล็อกอิน', $('mode-hint').textContent === 'Sign in as admin to change the mode', $('mode-hint').textContent);
  check('1 ปุ่มล็อกอินแสดง', $('mode-login').hidden === false);
  check('1 STANDBY ถูกเน้น', seg('STANDBY').attrs['aria-pressed'] === 'true');
  check('1 การ์ดสถานะหุ่น', $('act-text').textContent === 'Idle' && $('act-detail').textContent === 'Ready for tasks');
  check('1 ใช้ session แบบถามสถานะ', server.calls.some((c) => c.path === '/api/session'));
  check('1 แท็บเมนูล่างเรียง Home → Mapping → Waypoint → Navigation → Settings (ตามขั้นตอนทำงาน)',
    [...html.matchAll(/<a id="(tab-\w+)" class="tab"/g)].map((m) => m[1]).join(',') === 'tab-home,tab-mapping,tab-waypoint,tab-nav,tab-settings',
    [...html.matchAll(/<a id="(tab-\w+)" class="tab"/g)].map((m) => m[1]).join(','));
  check('1 ทุกแท็บชี้ไปหน้าที่มีอยู่จริง ไม่ซ้ำ', [...html.matchAll(/<a id="tab-\w+" class="tab" href="#\/(\w+)"/g)].every((m) => sections.some((x) => x.attrs['data-view'] === m[1])) &&
    new Set([...html.matchAll(/<a id="(tab-\w+)"/g)].map((m) => m[1])).size === 5);
  check('1 ข้อความท้ายหน้า: ปุ่ม ■ ครอบคลุม Navigation (ยกเลิกการเดินทาง) ทั้ง EN/TH', /Navigation/.test(I.DICT.en['ui.foot.note']) && /cancels the current trip/.test(I.DICT.en['ui.foot.note']) &&
    /Navigation/.test(I.DICT.th['ui.foot.note']) && /ยกเลิกการเดินทาง/.test(I.DICT.th['ui.foot.note']), I.DICT.en['ui.foot.note']);
  check('1 Rounds: ป้ายเป็น "Rounds today" / "รอบการทำงานวันนี้" และไม่มีคำใบ้ "coming soon" ค้างแล้ว (นับรอบได้จริง)', I.DICT.en['ui.stats.rounds'] === 'Rounds today' && /วันนี้/.test(I.DICT.th['ui.stats.rounds']) && !('ui.stats.rounds.hint' in I.DICT.en) && !/coming soon/.test(html));
  {   // การ์ดสถานะหุ่นเขียน DOM เฉพาะเมื่อค่าเปลี่ยน (ไม่อ่านซ้ำทุกข้อความ WebSocket)
    $('act-text').textContent = 'MARKER'; $('act-icon').textContent = 'MARKER'; $('activity-card').className = 'MARKER';
    await send({});
    check('1 สถานะหุ่นไม่เปลี่ยน → ไม่เขียน DOM ซ้ำ', $('act-text').textContent === 'MARKER' && $('act-icon').textContent === 'MARKER' && $('activity-card').className === 'MARKER');
    await send({ activity: { state: 'moving', level: 'ok', text: M('act.moving'), detail: null } });
    check('1 สถานะหุ่นเปลี่ยน → วาดใหม่', $('act-text').textContent === 'Moving' && $('activity-card').className === 'card status ok', $('act-text').textContent);
    await send({});
    check('1 กลับสถานะเดิม → วาดใหม่ตามเดิม', $('act-text').textContent === 'Idle', $('act-text').textContent);
  }
  check('1 ไม่มีหน้าข้อมูลเทคนิค (ไม่มี view / ลิงก์ / id ที่เกี่ยวข้อง)',
    !sections.some((s) => s.attrs['data-view'] === 'tech') && !('pose' in byId) && !('l-lidar' in byId) && !html.includes('#/tech'));

  // ═════════ 2) ไม่ได้ตั้ง admin_pin ═════════
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: false } };
  await advance(15000);
  check('2 hint บอก admin_pin', $('mode-hint').textContent.includes('admin_pin'), $('mode-hint').textContent);
  check('2 ซ่อนปุ่มล็อกอิน', $('mode-login').hidden === true);
  $('lock').click(); await flush();
  check('2 กดกุญแจ → toast', $('toast').textContent === 'admin_pin is not set — admin functions are disabled' && !$('toast').hidden, $('toast').textContent);
  check('2 ไม่เปิด dialog PIN', $('pin-dialog').open === false);
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } };
  await advance(15000);

  // ═════════ 3) ล็อกอิน: PIN ผิด → ถูกล็อก → ผ่าน ═════════
  $('lock').click(); await flush();
  check('3 เปิด dialog PIN', $('pin-dialog').open === true && $('pin-error').textContent === '');
  check('3 กุญแจ: aria = Admin sign in', $('lock').attrs['aria-label'] === 'Admin sign in');
  $('pin-input').value = 'x'; $('pin-form').dispatch('submit'); await flush();
  check('3 PIN ผิด แสดงข้อความ', $('pin-error').textContent === 'Incorrect PIN', $('pin-error').textContent);
  check('3 dialog ยังเปิด + กดส่งได้', $('pin-dialog').open && !$('pin-submit').disabled);
  check('3 ล้างช่อง PIN หลังส่ง', $('pin-input').value === '');

  server.login.push({ status: 429, data: { ok: false, retry_after: 30, error: M('auth.locked', { seconds: 30 }) } });
  $('pin-input').value = 'x'; $('pin-form').dispatch('submit'); await flush();
  check('3 ถูกล็อก: นับถอยหลัง', $('pin-error').textContent === 'Too many wrong attempts. Try again in 30 s', $('pin-error').textContent);
  check('3 ถูกล็อก: ปุ่มส่งกดไม่ได้', $('pin-submit').disabled && $('pin-input').disabled);
  await advance(10000);
  check('3 ถอยหลังลดลง', $('pin-error').textContent.includes('in 20 s'), $('pin-error').textContent);
  await advance(21000);
  check('3 ครบเวลา ปลดล็อก', !$('pin-submit').disabled && $('pin-error').textContent === '', $('pin-error').textContent);

  server.login.push({ status: 200, data: { ok: true, admin: true } });
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } };
  $('pin-input').value = '1234'; $('pin-form').dispatch('submit'); await flush();
  check('3 ล็อกอินสำเร็จ: ปิด dialog', $('pin-dialog').open === false);
  check('3 ส่ง PIN ไปถูกที่ + เฮดเดอร์กัน CSRF', posts('/api/login').some((c) => c.body.pin === '1234') &&
    posts('/api/login').every((c) => c.headers['X-Requested-With'] === 'agv-web'));
  check('3 toast เข้าสู่ระบบ', $('toast').textContent === 'Signed in as admin', $('toast').textContent);
  check('3 กุญแจเป็นสถานะ on + aria = Admin sign out', hasClass($('lock'), 'on') && $('lock-ico').attrs.href === '#i-unlock' &&
    $('lock').attrs['aria-label'] === 'Admin sign out', $('lock').attrs['aria-label']);
  check('3 ปุ่มโหมดกดได้ ยกเว้นโหมดปัจจุบัน', states() === 'STANDBY:off MAPPING:on MANUAL:on NAVIGATION:on', states());
  check('3 ซ่อนปุ่มล็อกอิน', $('mode-login').hidden === true);

  // ═════════ 4) เปลี่ยนโหมด: ยกเลิก / ยืนยัน ═════════
  seg('NAVIGATION').click(); await flush();
  check('4 ขึ้นหน้ายืนยัน (ชื่อโหมด + คำเตือนความปลอดภัย)', $('confirm-dialog').open && $('confirm-title').textContent === 'Switch to Navigation mode?' &&
    $('confirm-msg').textContent.includes('area around the robot is clear'), $('confirm-title').textContent);
  check('4 ปุ่มยืนยัน = Confirm', $('confirm-ok').textContent === 'Confirm');
  $('confirm-cancel').click(); await flush();
  check('4 ยกเลิก → ไม่ส่งคำสั่ง', posts('/api/mode').length === 0 && !$('confirm-dialog').open);
  check('4 โหมดทั่วไป: ไม่มีรายการ/ไม่ใช่ alertdialog', $('confirm-list').hidden === true && $('confirm-dialog').className === '' && $('confirm-dialog').attrs.role === 'dialog');
  seg('MAPPING').click(); await flush();
  check('4 Mapping: หน้าต่างแจ้งเตือนเฉพาะ (alertdialog + หัวข้อ + ปุ่ม)', $('confirm-dialog').open && $('confirm-dialog').attrs.role === 'alertdialog' &&
    hasClass($('confirm-dialog'), 'dlg-warn') && $('confirm-title').textContent === 'Switch the robot to Mapping mode?' &&
    $('confirm-ok').textContent === 'Start Mapping', `${$('confirm-title').textContent} | ${$('confirm-ok').textContent}`);
  check('4 Mapping: บอกว่ากำลังจะเปลี่ยนโหมดการทำงานของหุ่นเป็น Mapping', $('confirm-msg').textContent.startsWith('You are about to change the robot’s operating mode to Mapping'), $('confirm-msg').textContent);
  check('4 Mapping: 3 ข้อเตือน (แผนที่ใหม่/ไม่วิ่งเอง/พื้นที่โล่ง)', $('confirm-list').hidden === false && $('confirm-list').children.length === 3 &&
    /lost when Mapping stops/.test($('confirm-list').children[0].textContent) && /does not drive by itself/.test($('confirm-list').children[1].textContent) &&
    /area around the robot is clear/.test($('confirm-list').children[2].textContent), $('confirm-list').children.map((c) => c.textContent).join(' | '));
  $('confirm-cancel').click(); await flush();
  check('4 Mapping ยกเลิก → ไม่ส่งคำสั่ง', posts('/api/mode').length === 0);
  seg('NAVIGATION').click(); await flush();
  server.mode = { status: 202, data: { ok: true, message: M('mode.accepted', { mode: 'NAVIGATION' }),
    mode: statusMsg({ mode: { state: 'STARTING', target: 'NAVIGATION', message: M('mode.starting', { mode: 'NAVIGATION' }) } }).mode } };
  $('confirm-ok').click(); await flush();
  check('4 ยืนยัน → ส่ง POST โหมดที่เลือก', posts('/api/mode').length === 1 && posts('/api/mode')[0].body.mode === 'NAVIGATION');
  check('4 toast กำลังเปลี่ยน', $('toast').textContent === 'Changing mode…', $('toast').textContent);
  check('4 แสดงสถานะกำลังเปิดทันที', $('mode-state').textContent === 'Starting', $('mode-state').textContent);

  // ═════════ 5) สถานะต่างๆ จาก backend ═════════
  await send({ mode: { state: 'STARTING', target: 'NAVIGATION', message: M('mode.starting', { mode: 'NAVIGATION' }) } });
  check('5 STARTING: ตัวหมุน + ป้ายแถบบน', $('mode-spin').hidden === false && $('pill-text').textContent === 'Starting Navigation…' &&
    hasClass($('mode-pill'), 'warn') && $('pill-spin').hidden === false, $('pill-text').textContent);
  check('5 STARTING: ทุกปุ่มกดไม่ได้ + hint', segButtons.every((b) => b.disabled) && $('mode-hint').textContent === 'Changing mode, please wait…');
  check('5 STARTING: ข้อความจาก backend ถูกแปล', $('mode-msg').textContent === 'Starting Navigation mode…', $('mode-msg').textContent);

  await send({ mode: { mode: 'NAVIGATION', state: 'RUNNING' } });
  check('5 RUNNING: ชื่อโหมด + คำอธิบาย', $('mode-name').textContent === 'Navigation' && $('mode-th').textContent === 'Autonomous navigation');
  check('5 RUNNING: ป้ายปกติ', $('mode-state').textContent === 'Running' && hasClass($('mode-state'), 'ok'));
  check('5 RUNNING: บอกเวลาเริ่ม', /^Running since \d{2}:\d{2}$/.test($('mode-msg').textContent), $('mode-msg').textContent);
  check('5 RUNNING: ปุ่มโหมดปัจจุบันกดไม่ได้', states() === 'STANDBY:on MAPPING:on MANUAL:on NAVIGATION:off', states());
  check('5 RUNNING: ป้ายแถบบน', $('pill-text').textContent === 'Navigation' && hasClass($('mode-pill'), 'ok'));

  await send({ mode: { state: 'ERROR', message: M('mode.err.timeout', { mode: 'NAVIGATION', seconds: 60, log: '/l.log' }) } });
  check('5 ERROR: ป้ายแดง', $('mode-state').textContent === 'Error' && hasClass($('mode-state'), 'bad') && $('pill-text').textContent === 'Error');
  check('5 ERROR: แสดงสาเหตุ (ข้อความแปลแล้ว)', $('mode-msg').textContent === 'Mode change failed: Navigation mode did not start within 60 s. Log: /l.log', $('mode-msg').textContent);
  check('5 ERROR: กด Standby รับทราบได้', !seg('STANDBY').disabled, states());

  await send({ mode: { state: 'RUNNING', external: ['bt_navigator', 'amcl'], managed: false } });
  check('5 external: ชื่อ + ป้ายแถบบน', $('mode-name').textContent === 'External system' && $('pill-text').textContent === 'External system');
  check('5 external: กล่องเตือนแสดงชื่อ node', $('mode-ext').hidden === false && $('mode-ext').textContent.includes('bt_navigator, amcl'));
  check('5 external: ปุ่มกดไม่ได้', segButtons.every((b) => b.disabled), states());
  await send({ mode: { external: [], managed: true } });
  check('5 external หาย: กล่องเตือนซ่อน', $('mode-ext').hidden === true);

  const modeCases = [
    [{ mode: 'STANDBY' }, 'Standby|off|idle'], [{ mode: 'MAPPING' }, 'Mapping|ok|idle'],
    [{ mode: 'MANUAL' }, 'Control Manual|ok|idle'], [{ mode: 'NAVIGATION' }, 'Navigation|ok|idle'],
    [{ mode: 'STANDBY', state: 'STARTING', target: 'NAVIGATION' }, 'Starting Navigation…|warn|spin'],
    [{ mode: 'MAPPING', state: 'STOPPING' }, 'Stopping Mapping…|warn|spin'],
    [{ mode: 'STANDBY', state: 'ERROR', message: M('mode.err.busy') }, 'Error|bad|idle'],
    [{ mode: 'STANDBY', external: ['bt_navigator'] }, 'External system|warn|idle'],
  ];
  const pillState = () => `${$('pill-text').textContent}|${['ok', 'warn', 'bad', 'off'].find((c) => hasClass($('mode-pill'), c))}|${$('pill-spin').hidden ? 'idle' : 'spin'}`;
  for (const [mode, expected] of modeCases) {
    await send({ mode });
    check(`5 ป้ายโหมด ${JSON.stringify(mode)}`, pillState() === expected, pillState());
  }

  // ═════════ 6) ข้อผิดพลาดตอนสั่งเปลี่ยนโหมด ═════════
  await send({ mode: { mode: 'STANDBY', state: 'RUNNING' } });
  server.mode = { status: 409, data: { ok: false, message: M('mode.err.external', { nodes: ['bt_navigator'] }), mode: statusMsg().mode } };
  seg('MAPPING').click(); await flush(); $('confirm-ok').click(); await flush();
  check('6 409: toast บอกเหตุผล (แปลแล้ว)', $('toast').textContent === 'A system is running outside this app: bt_navigator. Use Reset all to stop it.', $('toast').textContent);
  server.mode = { status: 401, data: { ok: false, error: M('auth.required') } };
  seg('MAPPING').click(); await flush(); $('confirm-ok').click(); await flush();
  check('6 401: ถอนสถานะผู้ดูแล', !hasClass($('lock'), 'on') && $('lock-ico').attrs.href === '#i-lock');
  check('6 401: เปิด dialog PIN ให้ใส่ใหม่ + toast', $('pin-dialog').open === true && $('toast').textContent === 'Admin session expired — enter your PIN again');
  $('pin-cancel').click(); await flush();
  check('6 ยกเลิก dialog PIN → ปิด + ล้างช่อง', $('pin-dialog').open === false && $('pin-input').value === '');
  server.mode = { status: 500, data: {} };
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } };
  await advance(15000);
  seg('MAPPING').click(); await flush(); $('confirm-ok').click(); await flush();
  check('6 ไม่มีข้อความจาก server → ข้อความสำรอง', $('toast').textContent === 'Could not change mode', $('toast').textContent);

  // ═════════ 7) เซสชันหมดอายุ + ออกจากระบบ ═════════
  server.mode = null;
  check('7 session ยังอยู่ → ผู้ดูแล', hasClass($('lock'), 'on'));
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } };
  await advance(15000);
  check('7 หมดอายุ → toast + ล็อกปุ่ม', $('toast').textContent === 'Admin session expired — enter your PIN again' && !hasClass($('lock'), 'on'));
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } };
  await advance(15000);
  $('lock').click(); await flush();
  check('7 กดกุญแจตอนเป็นผู้ดูแล → ถามออกจากระบบ', $('confirm-dialog').open && $('confirm-title').textContent === 'Sign out' &&
    $('confirm-ok').textContent === 'Sign out');
  $('confirm-ok').click(); await flush();
  check('7 ส่ง logout + toast', posts('/api/logout').length === 1 && $('toast').textContent === 'Signed out');
  check('7 หลัง logout ปุ่มโหมดกดไม่ได้', segButtons.every((b) => b.disabled), states());

  // ═════════ 8) ความพร้อมของระบบ + ชิป + ความปลอดภัยของข้อความ ═════════
  const twoItems = { readiness: { ready: false, headline: M('ready.headline.not_ready'), hint: M('ready.loc.none.hint'), items: [
    { key: 'nav2', level: 'ok', text: M('ready.nav2.ok'), hint: null },
    { key: 'localization', level: 'bad', text: M('ready.loc.none'), hint: M('ready.loc.none.hint') }] } };
  await send(twoItems);
  const lis = $('checks').children;
  check('8 รายการความพร้อม 2 ข้อ (ข้อความแปลแล้ว)', lis.length === 2 && lis[1].children[1].children[0].textContent === 'Robot position unknown', lis.length);
  check('8 ข้อที่ไม่ผ่านเป็น bad + มีคำแนะนำ', lis[1].className === 'bad' && lis[1].children[1].children.some((c) => c.className === 'sub' && c.textContent === 'Set the initial pose first'));
  const chips = $('chips').children;
  const chipBtn = (i) => chips[i].children[0];
  check('8 ชิปความพร้อม 2 ตัว ระดับตรงกับรายการ', chips.length === 2 && chipBtn(0).className === 'chip ok' && chipBtn(1).className === 'chip bad');
  check('8 ป้ายชิป: สัญลักษณ์ + ชื่อสั้น', chipBtn(0).textContent === '✔Navigation' && chipBtn(1).textContent === '✖Position', chipBtn(1).textContent);
  chipBtn(1).dispatch('click');
  check('8 แตะชิป → กางรายละเอียดความพร้อม', $('ready-details').open === true && $('ready-details').scrolled === 1);

  await send({ ...twoItems, activity: { state: 'standby', level: 'off', text: M('act.standby'), detail: M('act.standby.detail') } });
  check('8b พักเครื่อง: ชิปทั้งหมดเป็น off (○) ไม่มี ✖', $('chips').children.every((li) => li.children[0].className === 'chip off' && li.children[0].textContent.startsWith('○')),
    $('chips').children.map((li) => li.children[0].textContent).join(','));
  check('8b พักเครื่อง: รายละเอียดยังบอกความจริง', $('checks').children[1].className === 'bad');
  await send({ ...twoItems, activity: { state: 'not_ready', level: 'warn', text: M('act.not_ready'), detail: M('ready.loc.none.hint') } });
  check('8b ไม่ใช่พักเครื่อง: ชิปกลับเป็นระดับจริง', $('chips').children[1].children[0].className === 'chip bad');

  // XSS: พารามิเตอร์จาก backend เป็นตัวอักษรเสมอ
  const evil = '<img src=x onerror=alert(1)>';
  await send({ mode: { state: 'ERROR', message: M('mode.err.internal', { error: evil }) } });
  check('8 ข้อความแปลกปลอมแสดงเป็นตัวอักษร ไม่เขียน innerHTML', $('mode-msg').textContent.includes(evil) && innerHTMLWrites === 0, innerHTMLWrites);

  // ═════════ 9) router ═════════
  const view = (n) => sections.find((s) => s.attrs['data-view'] === n);
  const nav = (h) => { location.hash = h; winListeners.hashchange.forEach((f) => f()); };
  nav('#/nav');
  check('9 #/nav: แสดงหน้า nav ซ่อน home', view('nav').hidden === false && view('home').hidden === true);
  check('9 #/nav: แท็บ nav ถูกเน้น', hasClass(byId['tab-nav'], 'active') && byId['tab-nav'].attrs['aria-current'] === 'page');
  check('9 #/nav: แท็บ home ไม่เน้น', !hasClass(byId['tab-home'], 'active') && byId['tab-home'].attrs['aria-current'] === undefined);
  nav('#/tech');
  check('9 #/tech ไม่มีแล้ว → กลับ Home', view('home').hidden === false);
  nav('#/nope');
  check('9 ลิงก์แปลก → กลับ home', view('home').hidden === false);

  // ═════════ 10) หลุดการเชื่อมต่อ แล้วต่อใหม่ ═════════
  await send({ mode: { mode: 'NAVIGATION' }, activity: { state: 'moving', level: 'ok', text: M('act.moving'), detail: null } });
  const before = wsCount;
  ws.close(); await flush();
  check('10 ป้ายโหมดแถบบน: Disconnected (ไม่โชว์โหมดเก่า)', $('pill-text').textContent === 'Disconnected' && hasClass($('mode-pill'), 'off'));
  check('10 การ์ดสถานะหุ่น off + ล้างชิป', hasClass($('activity-card'), 'off') && $('chips').children.length === 0 &&
    $('act-text').textContent === 'Disconnected' && $('act-detail').textContent.startsWith('Check that you are on the robot'));
  check('10 ปุ่มโหมดกดไม่ได้ + hint', segButtons.every((b) => b.disabled) && $('mode-hint').textContent === 'Disconnected — cannot change mode');
  await advance(1000);
  check('10 ต่อใหม่อัตโนมัติ', wsCount === before + 1, `${before} → ${wsCount}`);
  ws.onopen(); await send({ mode: { mode: 'NAVIGATION' } });
  check('10 ต่อกลับมา: ป้ายโหมดกลับมาเป็นของจริง', $('pill-text').textContent === 'Navigation' && hasClass($('mode-pill'), 'ok'));

  // ═════════ 11) คณิตศาสตร์ของกราฟ (ฟังก์ชันล้วน) ═════════
  const C = window.AgvChart;
  check('11 niceTicks ความเร็วเล็ก', eq(C.niceTicks(0, 0.2, 4).ticks, [0, 0.1, 0.2]));
  check('11 niceTicks สองฝั่ง', eq(C.niceTicks(-0.3, 0.45, 4).ticks, [-0.5, 0, 0.5]));
  check('11 niceTicks ปัดช่วงให้ลงขีดพอดี', C.niceTicks(0, 0.37, 4).max >= 0.37 && C.niceTicks(0, 0.37, 4).min === 0);
  check('11 niceTicks ช่วงเป็นศูนย์ไม่พัง', C.niceTicks(0, 0, 4).ticks.length >= 2);
  check('11 valueRange รวม 0 เสมอ', eq(C.valueRange([0.1, 0.3], 0.2), { lo: 0, hi: 0.3, any: true }));
  check('11 valueRange ขยายถึง minSpan', C.valueRange([0, 0, 0], 0.2).hi === 0.2 && C.valueRange([null], 0.2).any === false);
  check('11 valueRange ค่าลบเล็ก', C.valueRange([-0.05, null], 0.5).lo === -0.5);
  check('11 segments แยกที่ null', eq(C.segments([1, null, 2, 3, null]), [[0], [2, 3]]) && C.segments([null, null]).length === 0);
  check('11 nearestIndex', C.nearestIndex([-10, -5, 0], -4) === 1 && C.nearestIndex([], 1) === -1);
  check('11 buildPath', C.buildPath([[1, 2], [3, 4]]) === 'M1.0 2.0L3.0 4.0');
  check('11 trimNum ไม่มี -0 / ทศนิยมเศษ', C.trimNum(-0) === '0' && C.trimNum(0.30000000000000004) === '0.3');

  // ═════════ 12) การ์ดสถานะหุ่น + ค่าปัจจุบันที่หัวกราฟ ═════════
  const act = (level, text, detail) => ({ activity: { state: 'x', level, text: M(text), detail: detail ? M(detail) : null } });
  for (const [lv, glyph, code, label] of [['ok', '✔', 'act.moving', 'Moving'], ['warn', '▲', 'act.not_ready', 'Not ready'],
    ['bad', '✖', 'act.error', 'System error'], ['off', '○', 'act.standby', 'Standby']]) {
    await send(act(lv, code, 'act.idle.default'));
    check(`12 activity ${lv}: ไอคอน+ข้อความ+สี`, $('act-icon').textContent === glyph && $('act-text').textContent === label &&
      $('act-detail').textContent === 'Ready' && hasClass($('activity-card'), lv), $('activity-card').className);
  }
  const readyItems = [{ key: 'nav2', level: 'ok', text: M('ready.nav2.ok'), hint: null },
    { key: 'sensors', level: 'ok', text: M('ready.sensors.ok'), hint: null },
    { key: 'localization', level: 'ok', text: M('ready.loc.good'), hint: null }];
  await send({ speed: { v: 0.183, w: -0.4 }, localization: { quality: 'good', sigma: 0.123 },
    readiness: { ready: true, headline: M('ready.headline.ready'), hint: null, items: readyItems } });
  check('12 ค่าปัจจุบันที่หัวกราฟ', $('now-v').textContent === '0.18' && $('now-w').textContent === '-0.40');
  check('12 ชิปตำแหน่งบอกคุณภาพ', $('chips').children.length === 3 && $('chips').children[2].children[0].textContent === '✔Position · Good', $('chips').children[2].children[0].textContent);
  await send({ speed: null, localization: { quality: 'bad', sigma: 0.9 },
    readiness: { ready: false, headline: M('ready.headline.not_ready'), hint: null, items: [readyItems[0], readyItems[1],
      { key: 'localization', level: 'bad', text: M('ready.loc.bad'), hint: M('ready.loc.bad.hint') }] } });
  check('12 ไม่มีข้อมูลความเร็ว → –', $('now-v').textContent === '–' && $('now-w').textContent === '–');
  check('12 ชิปตำแหน่งที่ไม่ผ่านไม่ต่อคำว่าคุณภาพ', $('chips').children[2].children[0].textContent === '✖Position');

  // ═════════ 13) กราฟ: ดึงประวัติ → วาด ═════════
  nav('#/home');
  await advance(2000);
  const cls = (el, c) => el.children.filter((k) => hasClass(k, c));
  const svgV = $('chart-v'), svgW = $('chart-w');
  check('13 เรียก /api/history ด้วย window 120', server.calls.some((c) => c.path === '/api/history?window=120'));
  check('13 เส้นขาดตรงช่องว่าง (2 ช่วง)', cls(svgV, 'ch-line').length === 2, cls(svgV, 'ch-line').length);
  check('13 จุดปลาย 1 จุด + ป้ายค่า (dot อีกตัวเป็นของ crosshair)', cls(svgV, 'ch-dot').length === 2 && cls(svgV, 'ch-endlabel').length === 1);
  check('13 ป้ายค่าที่ปลายเป็นค่าล่าสุด', cls(svgV, 'ch-endlabel')[0].textContent === '0.20');
  check('13 เส้นศูนย์เป็นแกน + มี gridline', cls(svgV, 'ch-axis').length === 1 && cls(svgV, 'ch-grid').length >= 1);
  check('13 ป้ายแกน X: -2 min / -1 min / now', ['-2 min', '-1 min', 'now'].every((t) => cls(svgV, 'ch-tick').some((k) => k.textContent === t)),
    cls(svgV, 'ch-tick').map((k) => k.textContent).join('|'));
  check('13 aria-label สรุปค่า (อังกฤษ)', svgV.attrs['aria-label'].startsWith('Forward speed: latest 0.20 m/s') &&
    svgV.attrs['aria-label'].includes('over the last 120 s') && svgV.attrs.role === 'img' && svgV.attrs.tabindex === '0', svgV.attrs['aria-label']);
  check('13 ไม่มี NaN/undefined ใน attribute', [svgV, svgW].every((svg) => svg.children.every((k) => Object.values(k.attrs).every((a) => !/NaN|undefined|Infinity/.test(a)))));
  check('13 กราฟ w มีทั้งค่าบวก/ลบ: แกนเลขลบมีป้าย', cls(svgW, 'ch-tick').some((k) => k.textContent.startsWith('-') && /\d/.test(k.textContent) && !k.textContent.includes('min')));
  check('13 ขนาด viewBox ตามความกว้างกล่อง (สูง 124)', svgV.attrs.viewBox === '0 0 320 124', svgV.attrs.viewBox);

  // ═════════ 14) tooltip: เมาส์/สัมผัส + คีย์บอร์ด ═════════
  const tip = $('chart-v-tip');
  check('14 tooltip ซ่อนอยู่ก่อน', tip.hidden === true);
  svgV.dispatch('pointermove', { clientX: 175 }); await flush();
  check('14 pointermove → tooltip แสดงค่า+หน่วย+เวลา', tip.hidden === false && tip.textContent.includes('m/s') && /s ago|now/.test(tip.textContent), tip.textContent);
  check('14 tooltip ไม่ล้นกรอบ', parseFloat(tip.style.left) >= 70 && parseFloat(tip.style.left) <= 320 - 70, tip.style.left);
  check('14 crosshair แสดง', cls(svgV, 'ch-cross')[0].attrs.visibility === 'visible');
  svgV.dispatch('pointerleave'); await flush();
  check('14 pointerleave → ซ่อน', tip.hidden === true && cls(svgV, 'ch-cross')[0].attrs.visibility === 'hidden');
  svgV.dispatch('pointerdown', { clientX: 2 }); await flush();
  check('14 แตะซ้ายสุด → จุดแรก (นานสุด)', tip.hidden === false && tip.textContent.includes('118 s ago'), tip.textContent);
  svgV.dispatch('pointerleave');
  svgV.dispatch('focus'); await flush();
  check('14 โฟกัสด้วยคีย์บอร์ด → แสดงค่าล่าสุด', tip.hidden === false && tip.textContent.includes('now') && tip.textContent.includes('0.20'), tip.textContent);
  svgV.dispatch('keydown', { key: 'ArrowLeft' }); await flush();
  check('14 ลูกศรซ้าย → ย้อนเวลา', tip.textContent.includes('2 s ago'), tip.textContent);
  svgV.dispatch('keydown', { key: 'Home' }); await flush();
  check('14 Home → ตัวอย่างเก่าสุด', tip.textContent.includes('118 s ago'), tip.textContent);
  svgV.dispatch('keydown', { key: 'End' }); await flush();
  check('14 End → ตัวอย่างล่าสุด', tip.textContent.includes('now'));
  svgV.dispatch('keydown', { key: 'Escape' }); await flush();
  check('14 Escape → ซ่อน', tip.hidden === true);
  svgV.dispatch('focus'); svgV.dispatch('keydown', { key: 'Home' });
  for (let i = 0; i < 40; i++) svgV.dispatch('keydown', { key: 'ArrowRight' });
  await flush();
  check('14 ช่องว่างข้อมูล → tooltip บอก "No data"', tip.textContent.includes('No data'), tip.textContent);
  svgV.dispatch('blur'); await flush();

  // ═════════ 15) ระหว่างผู้ใช้กำลังดูค่า ไม่วาดทับ ═════════
  svgV.dispatch('focus'); await flush();
  const firstBefore = svgV.children[0];
  server.history = { status: 200, data: makeHistory('empty') };
  await advance(2000);
  check('15 กำลังดูค่า: ไม่วาดทับ', svgV.children[0] === firstBefore && tip.hidden === false);
  svgV.dispatch('blur'); await flush();
  check('15 ปล่อยแล้ววาดข้อมูลใหม่', svgV.children[0] !== firstBefore && cls(svgV, 'ch-empty').length === 1);
  check('15 ไม่มีข้อมูล: ไม่มีเส้น/จุดปลาย + ข้อความอังกฤษ', cls(svgV, 'ch-line').length === 0 && cls(svgV, 'ch-endlabel').length === 0 &&
    cls(svgV, 'ch-empty')[0].textContent === 'No speed data yet' && svgV.attrs['aria-label'] === 'Forward speed: no data', svgV.attrs['aria-label']);

  // ═════════ 16) โหลดไม่ได้ → คงภาพเดิมแต่จางลง ═════════
  server.history = { status: 200, data: makeHistory() }; await advance(2000);
  const kept = svgV.children[0];
  server.history = { status: 500, data: {} }; await advance(2000);
  check('16 โหลดไม่ได้: ติด stale + ไม่ล้างภาพ', hasClass($('chart-v-box'), 'stale') && svgV.children[0] === kept);
  server.history = { status: 200, data: makeHistory() }; await advance(2000);
  check('16 กลับมา: เอา stale ออก', !hasClass($('chart-v-box'), 'stale'));

  // ═════════ 17) ตารางเทียบเท่ากราฟ ═════════
  const rows = $('speed-body').children;
  check('17 ตาราง 13 แถว (ทุก 10 วินาที)', rows.length === 13, rows.length);
  check('17 แถวบนสุด = now', rows[0].children[0].textContent === 'now' && rows[0].children[1].textContent === '0.20');
  check('17 ช่องว่างข้อมูลแสดง –', rows.some((r) => r.children[1].textContent === '–'));
  check('17 แถวล่างสุด = -120 s', rows[12].children[0].textContent === '-120 s', rows[12].children[0].textContent);

  // ═════════ 18) ไม่ดึงข้อมูลตอนไม่ได้อยู่หน้า Home / ซ่อนแท็บ ═════════
  nav('#/waypoint'); await flush();
  const hist = () => server.calls.filter((c) => c.path.startsWith('/api/history')).length;
  const h0 = hist(); await advance(6000);
  check('18 ไม่ใช่หน้า Home → ไม่ดึงประวัติ', hist() === h0, `${h0} → ${hist()}`);
  nav('#/home'); await flush();
  check('18 กลับหน้า Home → ดึงทันที', hist() === h0 + 1);
  document.hidden = true; const h1 = hist(); await advance(6000);
  check('18 แท็บถูกซ่อน → ไม่ดึง', hist() === h1);
  document.hidden = false;

  // ═════════ 19) ป้ายโหมดแถบบน: แตะแล้วไปที่การ์ดโหมด ═════════
  const sc0 = $('mode-card').scrolled;
  $('mode-pill').click(); await flush();
  check('19 แตะป้ายที่หน้า Home → เลื่อนไปการ์ดโหมด', $('mode-card').scrolled === sc0 + 1);
  nav('#/waypoint');
  $('mode-pill').click(); await advance(100);
  check('19 แตะป้ายจากหน้าอื่น → กลับ Home แล้วเลื่อน', location.hash === '#/home' && $('mode-card').scrolled === sc0 + 2, `${location.hash} ${$('mode-card').scrolled}`);
  nav('#/home');
  check('19 ป้ายโหมดไม่ถูกซ่อนตอนเปลี่ยนหน้า', $('mode-pill').hidden === false);

  // ═════════ 19b) หน้า Settings: แท็บฟันเฟือง + ปุ่มภาษาอยู่ที่นี่ ═════════
  nav('#/settings'); await flush();
  const secOf = (v) => sections.find((x) => x.attrs['data-view'] === v);
  check('19b #/settings → แสดงหน้า settings เท่านั้น', secOf('settings').hidden === false &&
    sections.filter((x) => !x.hidden).length === 1);
  check('19b แท็บ Settings active + aria-current', hasClass($('tab-settings'), 'active') &&
    $('tab-settings').attrs['aria-current'] === 'page' && !hasClass($('tab-home'), 'active'));
  check('19b ปุ่มภาษาอยู่ในหน้า Settings ไม่ใช่ท้าย Home', /data-view="settings"[\s\S]*?lang-btn[\s\S]*?<\/section>/.test(html) &&
    !/<footer[\s\S]*?lang-btn[\s\S]*?<\/footer>/.test(html));
  check('19b แท็บ Settings ใช้ไอคอนฟันเฟือง', /id="tab-settings"[^>]*>\s*<svg class="ico"><use href="#i-gear"/.test(html));
  langBtn('th').click(); await flush();
  check('19b สลับภาษาจากหน้า Settings ได้', I.getLang() === 'th' && langBtn('th').attrs['aria-pressed'] === 'true');
  langBtn('en').click(); await flush();
  nav('#/home'); await flush();
  check('19b กลับ Home → แท็บ Settings ไม่ active', !hasClass($('tab-settings'), 'active') && hasClass($('tab-home'), 'active'));

  // ═════════ 19c) การ์ดสถิติ ═════════
  nav('#/home'); await advance(100);
  check('19c เรียก /api/stats เมื่ออยู่หน้า Home', server.calls.some((c) => c.path === '/api/stats'));
  check('19c ระยะทางวันนี้ < 100 m → ทศนิยม 1 ตำแหน่ง', $('st-dist').textContent === '12.3 m', $('st-dist').textContent);
  check('19c ระยะทางสะสม ≥ 1000 m → km', $('st-dist-total').textContent === 'Total 1.23 km', $('st-dist-total').textContent);
  check('19c เวลาทำงานวันนี้ 3725 s → 1 h 2 min', $('st-active').textContent === '1 h 2 min', $('st-active').textContent);
  check('19c เวลาทำงานสะสม 25 h', $('st-active-total').textContent === 'Total 25 h 0 min', $('st-active-total').textContent);
  check('19c รอบการทำงาน = – และไม่มีบรรทัดสะสม (backend รุ่นเก่าส่ง rounds เป็น null)', $('st-rounds').textContent === '–' && $('st-rounds-total').textContent === '');
  check('19c ตัวหนังสือตำนานครบ 3 โหมด', $('lg-NAVIGATION').textContent === 'Navigation' && $('lg-MAPPING').textContent === 'Mapping' &&
    $('lg-MANUAL').textContent === 'Control Manual');
  const segs = $('tl-track').children;
  check('19c ไทม์ไลน์ 3 ช่วง สีตามโหมด', segs.length === 3 && hasClass(segs[0], 'm-MAPPING') && hasClass(segs[1], 'm-MANUAL') && hasClass(segs[2], 'm-NAVIGATION'),
    segs.map((x) => x.className).join('|'));
  check('19c ตำแหน่ง/ความกว้างตามสัดส่วน 24 ชม.', segs[0].style.left === '16.67%' && segs[0].style.width === '4.17%', `${segs[0].style.left} ${segs[0].style.width}`);
  check('19c ช่วงสั้นมาก (10 นาที) ยังกว้างพอเห็น และไม่เกินขอบขวา', parseFloat(segs[2].style.width) >= 0.8 &&
    parseFloat(segs[2].style.left) + parseFloat(segs[2].style.width) <= 100.01, `${segs[2].style.left} ${segs[2].style.width}`);
  check('19c แต่ละช่วงมี title อ่านได้ (โหมด · เวลา · ระยะเวลา) และช่วงที่ยังเปิด = running',
    segs[0].attrs.title.startsWith('Mapping · ') && segs[0].attrs.title.endsWith('1 h 0 min') && segs[2].attrs.title.endsWith('10 min (running)'), segs[2].attrs.title);
  check('19c aria-label ของไทม์ไลน์บอกจำนวนช่วง', $('tl-track').attrs['aria-label'] === 'Mode timeline for the last 24 hours: 3 sessions. Details are in the table below.', $('tl-track').attrs['aria-label']);
  check('19c แกนเวลา -24 h / -12 h / now', $('tl-axis').children.map((x) => x.textContent).join('|') === '-24 h|-12 h|now', $('tl-axis').children.map((x) => x.textContent).join('|'));
  const srows = $('stats-body').children;
  check('19c ตารางเทียบเท่า: ใหม่สุดอยู่บน 3 แถว', srows.length === 3 && srows[0].children[0].textContent === 'Navigation' && srows[2].children[0].textContent === 'Mapping',
    srows.map((r) => r.children[0].textContent).join('|'));
  check('19c แถวที่ยังทำงานอยู่บอก running', srows[0].children[2].textContent === '10 min (running)', srows[0].children[2].textContent);
  check('19c ไม่มีโน้ตเตือนเมื่อบันทึกได้ปกติ', $('stats-note').hidden === true);
  check('19c ไม่มี stale ตอนโหลดสำเร็จ', !hasClass($('stats-card'), 'stale'));

  const sc = () => server.calls.filter((c) => c.path === '/api/stats').length;
  const s0 = sc(); await advance(10000);
  check('19c poll ทุก 10 วินาที', sc() === s0 + 1, `${s0} → ${sc()}`);
  nav('#/settings'); await flush();
  const s1 = sc(); await advance(25000);
  check('19c ไม่ใช่หน้า Home → ไม่ดึงสถิติ', sc() === s1, `${s1} → ${sc()}`);
  nav('#/home'); await flush();
  check('19c กลับหน้า Home → ดึงทันที', sc() === s1 + 1);

  server.stats = { status: 500, data: {} }; await advance(10000);
  check('19c โหลดไม่ได้ → จางลงแต่คงตัวเลขเดิม', hasClass($('stats-card'), 'stale') && $('st-dist').textContent === '12.3 m');
  server.stats = { status: 200, data: makeStats({ today: { date: 'x', distance_m: 0, active_s: 0 }, timeline: [], persist: { ok: false, note: null } }) };
  await advance(10000);
  check('19c ไม่มี stale หลังกลับมา + ศูนย์แสดงเป็น 0.0 m / 0 s', !hasClass($('stats-card'), 'stale') && $('st-dist').textContent === '0.0 m' && $('st-active').textContent === '0 s',
    `${$('st-dist').textContent} / ${$('st-active').textContent}`);
  check('19c ไม่มีช่วงทำงาน: ไทม์ไลน์ว่าง + ข้อความในตาราง + aria', $('tl-track').children.length === 0 &&
    $('stats-body').children.length === 1 && $('stats-body').children[0].children[0].textContent === 'No mode sessions in the last 24 hours' &&
    $('tl-track').attrs['aria-label'] === 'Mode timeline for the last 24 hours: no mode was active.');
  check('19c บันทึกลงดิสก์ไม่ได้ → โน้ตเตือน', $('stats-note').hidden === false && /cannot be saved/.test($('stats-note').textContent), $('stats-note').textContent);
  server.stats = { status: 200, data: makeStats({ persist: { ok: true, note: 'corrupt' } }) }; await advance(10000);
  check('19c ไฟล์เสีย → โน้ตแจ้งว่านับใหม่', $('stats-note').hidden === false && /unreadable/.test($('stats-note').textContent), $('stats-note').textContent);
  server.stats = { status: 200, data: makeStats({ rounds: 7, total: { distance_m: 1234.5, active_s: 90000, since: NOW - 5 * 86400, rounds: 42 } }) }; await advance(10000);
  check('19c รอบการทำงานแสดงเมื่อ backend ส่งตัวเลข (วันนี้ + สะสม)', $('st-rounds').textContent === '7' && $('st-rounds-total').textContent === 'Total 42', $('st-rounds').textContent + ' | ' + $('st-rounds-total').textContent);
  langBtn('th').click(); await flush();
  check('19c สลับเป็น TH: ตัวเลข/หน่วย/ตำนานเปลี่ยนทันทีจากข้อมูลล่าสุด', $('st-dist-total').textContent === 'สะสม 1.23 กม.' && $('st-active').textContent === '1 ชม. 2 นาที' &&
    $('tl-axis').children[0].textContent === '-24 ชม.' && /ช่วงทำงาน/.test($('tl-track').attrs['aria-label']), `${$('st-dist-total').textContent} | ${$('st-active').textContent}`);
  langBtn('en').click(); await flush();
  check('19c กลับ EN: ไม่มีตัวอักษรไทยหลุดในการ์ดสถิติ', thaiLeaks().length === 0, thaiLeaks().join(' | '));
  server.stats = null; await advance(10000);

  // ═════════ 19d) กราฟแท่ง 7 วัน + รีเซ็ตสถิติ ═════════
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } };
  server.stats = null; nav('#/home'); await advance(15000);
  const dsvg = $('chart-days'), dtip = $('days-tip');
  const dcls = (c) => dsvg.children.filter((k) => hasClass(k, c));
  check('19d ปุ่มรีเซ็ตซ่อนอยู่เมื่อไม่ใช่ผู้ดูแล', $('stats-reset').hidden === true);
  check('19d แท่ง = เฉพาะวันที่มีค่า (4 จาก 7) + แกนศูนย์ + ป้ายแกน X 7 ป้าย', dcls('ch-bar').length === 4 && dcls('ch-axis').length === 1 &&
    dcls('ch-tick').filter((k) => /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Today)$/.test(k.textContent)).length === 7,
    dcls('ch-tick').map((k) => k.textContent).join('|'));
  check('19d วันสุดท้ายคือ Today', dcls('ch-tick').some((k) => k.textContent === 'Today'));
  check('19d ค่าสูงสุด 1234.5 m → แกนเป็น km', dcls('ch-tick').some((k) => k.textContent === 'km'));
  check('19d ป้ายค่าเฉพาะวันนี้ (ตัวเดียว) = 12.3 m', dcls('ch-endlabel').length === 1 && dcls('ch-endlabel')[0].textContent === '12.3 m', dcls('ch-endlabel').map((k) => k.textContent).join('|'));
  check('19d aria-label สรุปวันนี้และวันสูงสุด', dsvg.attrs['aria-label'].startsWith('Distance, last 7 days. Today 12.3 m; highest ') && dsvg.attrs['aria-label'].includes('1.23 km'), dsvg.attrs['aria-label']);
  const kmTicks = dcls('ch-tick').map((k) => k.textContent).filter((x) => /^[\d.]+$/.test(x)).map(Number);
  check('19d แกนหน่วย km: ค่าสเกลถูกต้อง (≤ 2 ไม่ใช่ 1234)', kmTicks.length >= 2 && Math.max(...kmTicks) <= 2 && Math.max(...kmTicks) >= 1.2, kmTicks.join(','));
  check('19d ไม่มี NaN/undefined ใน attribute', dsvg.children.every((k) => Object.values(k.attrs).every((a) => !/NaN|undefined|Infinity/.test(a))));
  check('19d ปุ่ม Distance ถูกเลือก', $('metric-dist').attrs['aria-pressed'] === 'true' && $('metric-active').attrs['aria-pressed'] === 'false');
  const drows = $('days-body').children;
  check('19d ตารางเทียบเท่า 7 แถว วันนี้อยู่บน', drows.length === 7 && drows[0].children[0].textContent === 'Today' &&
    drows[0].children[1].textContent === '12.3 m' && drows[0].children[2].textContent === '1 h 2 min', drows[0].children.map((c) => c.textContent).join('|'));
  check('19d ตาราง: วันที่ไม่มีข้อมูลแสดง 0', drows[6].children[1].textContent === '0.0 m' && drows[6].children[2].textContent === '0 s');

  dsvg.dispatch('pointermove', { clientX: 320 - 20 }); await flush();
  check('19d ชี้แท่งขวาสุด → tooltip วันนี้ + ค่า', dtip.hidden === false && dtip.textContent.startsWith('Today') && dtip.textContent.includes('12.3 m'), dtip.textContent);
  check('19d tooltip ไม่ล้นกรอบ', parseFloat(dtip.style.left) >= 70 && parseFloat(dtip.style.left) <= 250, dtip.style.left);
  check('19d แถบเน้นคอลัมน์แสดง', dcls('ch-hover')[0].attrs.visibility === 'visible');
  dsvg.dispatch('pointermove', { clientX: 46 + 5 }); await flush();
  check('19d ชี้คอลัมน์แรก → 0 m ของวันที่ไม่มีข้อมูล', dtip.textContent.includes('0.0 m') && !dtip.textContent.startsWith('Today'), dtip.textContent);
  dsvg.dispatch('pointermove', { clientX: 110 }); await flush();     // 46 + 37.4×1.7 → ต้องเป็นคอลัมน์ที่ 2 (ปัดลง ไม่ใช่ปัดใกล้)
  check('19d ตำแหน่งเมาส์ → คอลัมน์ที่ถูกต้อง (ปัดลง)', dtip.textContent.includes('0.0 m') && !dtip.textContent.includes('250'), dtip.textContent);
  dsvg.dispatch('pointerleave'); await flush();
  check('19d ออกจากกราฟ → ซ่อน', dtip.hidden === true && dcls('ch-hover')[0].attrs.visibility === 'hidden');
  dsvg.dispatch('focus'); await flush();
  check('19d โฟกัสคีย์บอร์ด → วันนี้', dtip.hidden === false && dtip.textContent.startsWith('Today'));
  dsvg.dispatch('keydown', { key: 'ArrowLeft' }); await flush();
  check('19d ลูกศรซ้าย → วันก่อนหน้า (80 m)', dtip.textContent.includes('80.0 m') || dtip.textContent.includes('80 m'), dtip.textContent);
  dsvg.dispatch('keydown', { key: 'Home' }); await flush();
  check('19d Home → วันเก่าสุด', dtip.textContent.includes('0.0 m'));
  dsvg.dispatch('keydown', { key: 'End' }); await flush();
  check('19d End → วันนี้', dtip.textContent.startsWith('Today'));

  // ระหว่างดูค่า ข้อมูลใหม่เข้ามาต้องไม่วาดทับ แล้ววาดทันทีที่ออก
  const barsBefore = dcls('ch-bar').length;
  server.stats = { status: 200, data: makeStats({ days: Array.from({ length: 7 }, (_, i) => ({ date: `2026-10-${20 + i}`, distance_m: 100 + i * 50, active_s: 60 })) }) };
  await advance(10000);
  check('19d ระหว่างดูค่า: ไม่วาดทับ (จำนวนแท่งเดิม)', dcls('ch-bar').length === barsBefore && dtip.hidden === false, `${dcls('ch-bar').length} vs ${barsBefore}`);
  dsvg.dispatch('keydown', { key: 'Escape' }); await flush();
  check('19d ออกจากโหมดดูค่า → วาดข้อมูลใหม่ (7 แท่ง)', dcls('ch-bar').length === 7, dcls('ch-bar').length);
  server.stats = null; await advance(10000);

  $('metric-active').click(); await flush();
  check('19d สลับเป็น Active time: ปุ่ม/หน่วยแกนเป็นชั่วโมง/ป้ายค่า/aria', $('metric-active').attrs['aria-pressed'] === 'true' && $('metric-dist').attrs['aria-pressed'] === 'false' &&
    dcls('ch-tick').some((k) => k.textContent === 'h') && dcls('ch-endlabel')[0].textContent === '1 h 2 min' &&
    dsvg.attrs['aria-label'].startsWith('Active time, last 7 days. Today 1 h 2 min; highest '), dsvg.attrs['aria-label']);
  $('metric-dist').click(); await flush();
  check('19d สลับกลับ Distance', dcls('ch-tick').some((k) => k.textContent === 'km') && $('metric-dist').attrs['aria-pressed'] === 'true');

  const zeroDays = Array.from({ length: 7 }, (_, i) => ({ date: `2026-10-${20 + i}`, distance_m: 0, active_s: 0 }));
  server.stats = { status: 200, data: makeStats({ days: zeroDays, today: { date: 'x', distance_m: 0, active_s: 0 } }) }; await advance(10000);
  check('19d ไม่มีกิจกรรมเลย: ไม่มีแท่ง + ข้อความ + aria + ไม่มีป้ายค่า', dcls('ch-bar').length === 0 && dcls('ch-endlabel').length === 0 &&
    dcls('ch-empty')[0].textContent === 'No activity in the last 7 days' && dsvg.attrs['aria-label'] === 'Distance, last 7 days: no activity.', dsvg.attrs['aria-label']);
  server.stats = null; await advance(10000);

  // รีเซ็ตสถิติ: Admin เท่านั้น + ยืนยันก่อน
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);
  check('19d เป็นผู้ดูแล → เห็นปุ่มรีเซ็ต', $('stats-reset').hidden === false);
  const resets = () => posts('/api/stats/reset');
  $('stats-reset').click(); await flush();
  check('19d กดรีเซ็ต → ถามยืนยัน (บอกว่าลบอะไร/กู้คืนไม่ได้)', $('confirm-dialog').open && $('confirm-title').textContent === 'Reset all statistics?' &&
    /permanently/.test($('confirm-msg').textContent) && /cannot be undone/.test($('confirm-msg').textContent) && $('confirm-ok').textContent === 'Reset');
  $('confirm-cancel').click(); await flush();
  check('19d ยกเลิก → ไม่ส่งคำสั่ง ตัวเลขเดิมอยู่', resets().length === 0 && $('st-dist').textContent === '12.3 m');
  $('stats-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19d ยืนยัน → POST พร้อมเฮดเดอร์กัน CSRF', resets().length === 1 && resets()[0].headers['X-Requested-With'] === 'agv-web', JSON.stringify(resets()[0] && resets()[0].headers));
  check('19d หลังรีเซ็ต: ตัวเลข/ไทม์ไลน์/กราฟ ว่างทันทีจากคำตอบ', $('st-dist').textContent === '0.0 m' && $('st-active').textContent === '0 s' &&
    $('tl-track').children.length === 0 && dcls('ch-bar').length === 0 && $('toast').textContent === 'Statistics reset', `${$('st-dist').textContent} ${$('toast').textContent}`);
  server.reset = { status: 401, data: { ok: false, error: M('auth.required') } };
  $('stats-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19d 401: ถอนสถานะผู้ดูแล + ซ่อนปุ่ม + เปิด PIN', $('stats-reset').hidden === true && $('pin-dialog').open === true && !hasClass($('lock'), 'on'));
  $('pin-cancel').click(); await flush();
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);
  server.reset = { status: 500, data: {} };
  $('stats-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19d ล้มเหลว → toast ข้อความสำรอง และไม่ล้างหน้าจอ', $('toast').textContent === 'Could not reset statistics', $('toast').textContent);
  server.reset = null; server.stats = null;
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } }; await advance(15000);
  check('19d หมดสิทธิ์ผู้ดูแล → ปุ่มรีเซ็ตซ่อนอีก', $('stats-reset').hidden === true);
  const ys = (d) => [...d.matchAll(/V([\d.]+)/g)].map((m) => parseFloat(m[1]));
  check('19d AgvChart.barPath: แท่งเตี้ยมากไม่ทะลุเส้นฐาน', Math.max(...ys(window.AgvChart.barPath(10, 100, 20, 1, 4))) <= 101.0001, window.AgvChart.barPath(10, 100, 20, 1, 4));
  check('19d AgvChart.barPath: ไม่มี NaN แม้แท่งเตี้ยกว่ารัศมีมุม', !/NaN/.test(window.AgvChart.barPath(10, 100, 20, 1, 4)) && !/NaN/.test(window.AgvChart.barPath(10, 100, 3, 0, 4)));
  langBtn('th').click(); await flush();
  check('19d สลับเป็น TH: ป้าย Today/หน่วย/ปุ่มเปลี่ยนภาษา', dcls('ch-tick').some((k) => k.textContent === 'วันนี้') && $('metric-dist').textContent === 'ระยะทาง');
  langBtn('en').click(); await flush();
  check('19d กลับ EN: ไม่มีตัวอักษรไทยหลุดในกราฟ 7 วัน/ปุ่มรีเซ็ต', thaiLeaks().length === 0 && dcls('ch-tick').every((k) => !/[฀-๿]/.test(k.textContent)), thaiLeaks().join('|'));

  // ═════════ 19e) หน้า Mapping: แผนที่สด ═════════
  const MAPMETA = (version, over = {}) => Object.assign({ available: true, version, width: 240, height: 200, resolution: 0.05,
    origin: { x: -3.5, y: -5.0, yaw: 0 }, size_m: { w: 12.0, h: 10.0 }, known_cells: 30000, stamp: NOW }, over);
  const modeOf = (mode, state = 'RUNNING', extra = {}) => ({ mode: { mode, state, target: null, message: null, external: [], ...extra } });
  const mcanvas = $('map-canvas');
  const mlog = (name) => (mcanvas.ctxLog || []).filter((c) => c.name === name);
  const mapCalls = () => server.calls.filter((c) => c.path === '/api/map/live').length;
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } };
  server.map = null; langBtn('en').click(); await flush();
  await send(modeOf('STANDBY')); nav('#/mapping'); await advance(100);
  check('19e Standby + ไม่ใช่ผู้ดูแล: การ์ดเกต + ข้อความ + ปุ่มล็อกอิน', $('map-gate').hidden === false && $('map-live').hidden === true &&
    $('map-gate-msg').textContent === 'Mapping is not running. Start Mapping mode to build a new map. Sign in as admin to start Mapping mode.' &&
    $('map-login').hidden === false && $('map-start').hidden === true, $('map-gate-msg').textContent);
  const m0 = mapCalls(); await advance(9000);
  check('19e ยังไม่ได้รัน Mapping → ไม่ดึงแผนที่', mapCalls() === m0, `${m0} → ${mapCalls()}`);
  $('map-login').click(); await flush();
  check('19e ปุ่มล็อกอินเปิดหน้าต่าง PIN', $('pin-dialog').open === true);
  $('pin-cancel').click(); await flush();

  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);
  check('19e ผู้ดูแล: เห็นปุ่มเริ่ม Mapping ไม่เห็นปุ่มล็อกอิน', $('map-start').hidden === false && $('map-login').hidden === true &&
    $('map-gate-msg').textContent === 'Mapping is not running. Start Mapping mode to build a new map.', $('map-gate-msg').textContent);
  $('map-start').click(); await flush();
  check('19e กดเริ่ม → หน้าต่างแจ้งเตือนสลับเป็นโหมด Mapping (เหมือนปุ่มโหมด)', $('confirm-dialog').open && $('confirm-dialog').attrs.role === 'alertdialog' &&
    $('confirm-title').textContent === 'Switch the robot to Mapping mode?' && $('confirm-list').children.length === 3);
  $('confirm-cancel').click(); await flush();
  const pm = posts('/api/mode').length;
  check('19e ยกเลิก → ไม่สลับโหมด', posts('/api/mode').length === pm);
  server.mode = { status: 202, data: { ok: true, message: M('mode.accepted', { mode: 'MAPPING' }) } };
  $('map-start').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19e ยืนยัน → POST โหมด MAPPING', posts('/api/mode').length === pm + 1 && posts('/api/mode')[pm].body.mode === 'MAPPING');
  server.mode = null;

  await send(modeOf('STANDBY', 'STARTING', { target: 'MAPPING' }));
  check('19e กำลังเปิด Mapping: ข้อความ + ไม่มีปุ่ม', $('map-gate-msg').textContent === 'Starting Mapping mode… the live map appears here in a few seconds.' &&
    $('map-start').hidden === true && $('map-login').hidden === true, $('map-gate-msg').textContent);
  await send(modeOf('NAVIGATION', 'STOPPING'));
  check('19e กำลังเปลี่ยนโหมดอื่น: ข้อความ busy', $('map-gate-msg').textContent === 'The robot is changing mode…');
  await send(modeOf('NAVIGATION'));
  check('19e โหมดอื่นรันอยู่: บอกว่าจะปิดโหมดนั้นก่อน + ปุ่มเริ่มกดได้', $('map-gate-msg').textContent === 'Navigation mode is running. Starting Mapping will shut it down first.' && $('map-start').hidden === false, $('map-gate-msg').textContent);
  await send(modeOf('STANDBY', 'RUNNING', { external: ['bt_navigator'] }));
  check('19e ระบบนอกแอปรันอยู่: ข้อความ + ไม่มีปุ่ม', /started outside this app/.test($('map-gate-msg').textContent) && $('map-start').hidden === true);
  await send(modeOf('STANDBY', 'ERROR', { message: M('mode.err.timeout', { mode: 'MAPPING', seconds: 40, log: '/x.log' }) }));
  check('19e error: บอกว่าครั้งล่าสุดไม่สำเร็จ + เหตุผลที่แปลแล้ว', /^The last mode change failed: Mapping mode did not start within 40 s/.test($('map-gate-msg').textContent), $('map-gate-msg').textContent);

  // เข้าโหมด Mapping จริง
  await send({ ...modeOf('MAPPING'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 } });
  check('19e Mapping รันอยู่: เห็นการ์ดแผนที่สด ซ่อนเกต + ข้อความรอแผนที่แรก', $('map-live').hidden === false && $('map-gate').hidden === true && $('map-wait').hidden === false);
  check('19e ยังไม่มีแผนที่: aria + ไม่วาดภาพ', mcanvas.attrs['aria-label'] === 'Live map: waiting for the first map.' && mlog('drawImage').length === 0, mcanvas.attrs['aria-label']);
  server.map = { status: 200, data: { available: false, version: 3 } };
  await advance(3100);
  check('19e ดึง /api/map/live ทุก 3 วินาที (แต่ยังไม่โหลดภาพ)', mapCalls() > m0 && images.length === 0 && $('map-wait').hidden === false);
  server.map = { status: 200, data: MAPMETA(5) };
  await advance(3100);
  check('19e มีแผนที่ → โหลดภาพ PNG เวอร์ชันนั้น', images.length === 1 && images[0].src === '/api/map/live.png?v=5', images.map((i) => i.src).join(','));
  check('19e โหลดแล้ว: ซ่อนข้อความรอ + แสดงขนาด/ความละเอียด', $('map-wait').hidden === true &&
    /^Map 12\.0 × 10\.0 m · 0\.05 m per cell · updated \d\d:\d\d$/.test($('map-info').textContent), $('map-info').textContent);
  const di = mlog('drawImage');
  check('19e วาดภาพแผนที่ 1 ครั้งต่อรอบวาด ด้วยสัดส่วนพิกเซล:เมตรถูกต้อง (กว้าง:สูง = 240:200)', di.length >= 1 &&
    Math.abs(di[di.length - 1].args[3] / di[di.length - 1].args[4] - 240 / 200) < 1e-6, JSON.stringify(di[di.length - 1] && di[di.length - 1].args.slice(1)));
  check('19e วาดหุ่น (fill) + สเกลบาร์ (ข้อความ "m")', mlog('fill').length >= 1 && mlog('fillText').some((c) => /^\d+(\.\d+)? m$/.test(c.args[0])), mlog('fillText').map((c) => c.args[0]).join(','));
  check('19e aria ของ canvas บอกขนาดและตำแหน่งหุ่น', mcanvas.attrs['aria-label'] === 'Live map, 12.0 by 10.0 metres. Robot at x 1.0, y 2.0.', mcanvas.attrs['aria-label']);
  const nImg = images.length; await advance(3100);
  check('19e เวอร์ชันเดิม → ไม่โหลดภาพซ้ำ', images.length === nImg);
  server.map = { status: 200, data: MAPMETA(6, { size_m: { w: 14.0, h: 10.0 }, width: 280 }) }; await advance(3100);
  check('19e เวอร์ชันใหม่ → โหลดภาพใหม่', images.length === nImg + 1 && images[images.length - 1].src === '/api/map/live.png?v=6');
  imageFails = true; server.map = { status: 200, data: MAPMETA(7) }; await advance(3100);
  const failedCount = images.length; imageFails = false; await advance(3100);
  check('19e โหลดภาพล้มเหลว → ลองใหม่รอบถัดไป', images.length > failedCount && images[images.length - 1].src === '/api/map/live.png?v=7');

  check('19e canvas ปิด smoothing (พิกเซลคม)', mcanvas._ctx.imageSmoothingEnabled === false);
  check('19e หุ่นหมุนตาม yaw (90° ทวนเข็ม = canvas หมุน -π/2)', Math.abs(mlog('rotate').filter((c) => Math.abs(c.args[0]) > 1)[0].args[0] + Math.PI / 2) < 1e-6,
    mlog('rotate').map((c) => c.args[0]).join(','));
  // ภาพกำลังโหลดค้าง (เครือข่ายช้า) → ห้ามขอเวอร์ชันเดียวกันซ้ำทุก 3 วินาที ; origin หมุน (yaw ≠ 0) → วาดภาพแบบหมุน
  imageHold = true; const heldFrom = images.length;
  server.map = { status: 200, data: MAPMETA(8, { origin: { x: -3.5, y: -5.0, yaw: 0.5 } }) }; await advance(9300);
  check('19e ภาพกำลังโหลดค้าง → ขอเวอร์ชันเดียวกันครั้งเดียว', images.length === heldFrom + 1, `${images.length - heldFrom}`);
  imageHold = false; images[images.length - 1].onload(); await flush();
  check('19e origin yaw ≠ 0 → วาดภาพแบบหมุนตาม yaw', mlog('rotate').some((c) => Math.abs(c.args[0] + 0.5) < 1e-9), mlog('rotate').slice(-3).map((c) => c.args[0]).join(','));
  server.map = { status: 200, data: MAPMETA(9) }; await advance(3100);

  // ซูม / เลื่อน / ย่อให้พอดี
  const widthNow = () => { const d = mlog('drawImage'); return d[d.length - 1].args[3]; };
  const txNow = () => {          // translate ของ "ภาพแผนที่" = ตัวที่อยู่ก่อน drawImage ตัวสุดท้าย (translate ตัวหลังสุดเป็นของหุ่น)
    const log = mcanvas.ctxLog; const k = log.map((c) => c.name).lastIndexOf('drawImage');
    for (let i = k; i >= 0; i--) if (log[i].name === 'translate') return log[i].args;
    return null;
  };
  const robotTx = () => { const d = mlog('translate'); return d[d.length - 1].args; };
  const w0 = widthNow();
  mcanvas.dispatch('wheel', { deltaY: -100, clientX: 160, clientY: 130 }); await flush();
  const w1 = widthNow();
  check('19e ล้อเมาส์เลื่อนขึ้น → ซูมเข้า', w1 > w0 * 1.1, `${w0} → ${w1}`);
  $('map-zoom-out').click(); $('map-zoom-out').click(); await flush();
  check('19e ปุ่ม − → ซูมออก', widthNow() < w1, `${w1} → ${widthNow()}`);
  $('map-fit').click(); await flush();
  check('19e ปุ่ม Fit → กลับขนาดพอดีเดิม', Math.abs(widthNow() - w0) < 1e-6, `${w0} vs ${widthNow()}`);
  const t0 = txNow();
  mcanvas.dispatch('pointerdown', { pointerId: 1, clientX: 100, clientY: 100 });
  mcanvas.dispatch('pointermove', { pointerId: 1, clientX: 130, clientY: 90 }); await flush();
  const t1 = txNow();
  check('19e ลากด้วยนิ้ว → แผนที่เลื่อนตามนิ้ว (+30, -10 พิกเซล)', Math.abs(t1[0] - t0[0] - 30) < 1e-6 && Math.abs(t1[1] - t0[1] + 10) < 1e-6, `${t0} → ${t1}`);
  mcanvas.dispatch('pointerup', { pointerId: 1 });
  mcanvas.dispatch('pointermove', { pointerId: 1, clientX: 200, clientY: 200 }); await flush();
  check('19e ปล่อยนิ้วแล้ว เลื่อนเมาส์ไม่ลากต่อ', txNow()[0] === t1[0] && txNow()[1] === t1[1]);
  const wBefore = widthNow();
  mcanvas.dispatch('pointerdown', { pointerId: 1, clientX: 100, clientY: 150 });
  mcanvas.dispatch('pointerdown', { pointerId: 2, clientX: 140, clientY: 150 });
  mcanvas.dispatch('pointermove', { pointerId: 2, clientX: 220, clientY: 150 }); await flush();
  check('19e pinch ถ่างนิ้ว (40→120 พิกเซล) → ซูมเข้า', widthNow() > wBefore * 2, `${wBefore} → ${widthNow()}`);
  mcanvas.dispatch('pointerup', { pointerId: 1 }); mcanvas.dispatch('pointerup', { pointerId: 2 });
  $('map-fit').click(); await flush();
  const wk = widthNow();
  mcanvas.dispatch('keydown', { key: '+' }); await flush();
  check('19e คีย์บอร์ด + → ซูมเข้า', widthNow() > wk);
  mcanvas.dispatch('keydown', { key: 'ArrowLeft' }); await flush();
  check('19e คีย์บอร์ด ลูกศร → เลื่อน (แผนที่ขยับไปทางขวา)', txNow()[0] > t0[0] - 1000);
  mcanvas.dispatch('keydown', { key: '0' }); await flush();
  check('19e คีย์บอร์ด 0 → พอดี', Math.abs(widthNow() - w0) < 1e-6);
  $('map-center').click(); await flush();
  check('19e ปุ่ม Robot → หุ่นอยู่กึ่งกลางจอ (160,136) และแผนที่เลื่อนตามให้ตรง', JSON.stringify(robotTx()) === '[160,136]' &&
    Math.abs(txNow()[0] - (160 - (1.0 - (-3.5)) * widthNow() / 12.0)) < 1e-6, `${robotTx()} | ${txNow()}`);
  $('map-fit').click(); await flush();

  // ออกจากโหมด Mapping → แผนที่เก่าต้องไม่ค้าง
  await send(modeOf('STANDBY'));
  check('19e ออกจาก Mapping → กลับเป็นเกต (ไม่ค้างแผนที่เก่า)', $('map-live').hidden === true && $('map-gate').hidden === false);
  const nDraw = mlog('drawImage').length, nImg2 = images.length;
  server.map = { status: 200, data: { available: false, version: 9 } };
  await send({ ...modeOf('MAPPING'), pose: { x: 0, y: 0, yaw_deg: 0 } });
  check('19e เข้า Mapping รอบใหม่: ไม่วาดแผนที่เก่า + รอแผนที่แรก', $('map-wait').hidden === false && mlog('drawImage').length === nDraw && images.length === nImg2);
  await advance(3100);
  check('19e แผนที่ใหม่ยังไม่มา → ยังไม่วาด', mlog('drawImage').length === nDraw);

  // ไม่ใช่หน้า Mapping / ซ่อนแท็บ → ไม่ดึง
  nav('#/home'); await flush();
  const m1 = mapCalls(); await advance(9000);
  check('19e ไม่ใช่หน้า Mapping → ไม่ดึงแผนที่', mapCalls() === m1, `${m1} → ${mapCalls()}`);
  nav('#/mapping'); await flush();
  check('19e กลับหน้า Mapping → ดึงทันที', mapCalls() === m1 + 1);
  document.hidden = true; const m2 = mapCalls(); await advance(9000);
  check('19e แท็บถูกซ่อน → ไม่ดึง', mapCalls() === m2);
  document.hidden = false;

  // หลุดการเชื่อมต่อ
  ws.onclose(); await flush();
  check('19e หลุดการเชื่อมต่อ → เกตบอกว่าขาดการเชื่อมต่อ ไม่แสดงแผนที่ค้าง', $('map-live').hidden === true && $('map-gate-msg').textContent === 'Disconnected from the robot — the live map is unavailable.', $('map-gate-msg').textContent);
  await advance(1100); ws.onopen(); await send({ ...modeOf('STANDBY') });
  langBtn('th').click(); await flush();
  check('19e สลับ TH: ข้อความเกต/ปุ่มเปลี่ยนภาษาทันที', /ยังไม่ได้เปิด Mapping/.test($('map-gate-msg').textContent) && $('map-start').textContent === 'เริ่มโหมด Mapping', $('map-gate-msg').textContent);
  langBtn('en').click(); await flush();
  check('19e กลับ EN: ไม่มีตัวอักษรไทยหลุดในหน้า Mapping', thaiLeaks().length === 0, thaiLeaks().join(' | '));
  nav('#/home'); await flush();
  server.map = null;

  // ═════════ 19f) Reset all ═════════
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } }; server.mode = null;
  nav('#/home'); await send(modeOf('MAPPING')); await advance(15000);
  check('19f ไม่ใช่ผู้ดูแล → ไม่เห็นปุ่ม Reset all (ทั้งในการ์ดและแถบบน)', $('mode-reset').hidden === true && $('reset-top').hidden === true);
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);
  check('19f ผู้ดูแล → เห็นปุ่ม Reset all (กดได้)', $('mode-reset').hidden === false && $('mode-reset').disabled === false);
  check('19f แถบบน: ปุ่ม ↻ เห็นเฉพาะผู้ดูแล + ชื่อ/คำช่วย + สี reset', $('reset-top').hidden === false && $('reset-top').disabled === false &&
    $('reset-top').attrs.title === 'Reset all' && $('reset-top').attrs['aria-label'] === 'Reset all robot systems' && hasClass($('reset-top'), 'reset') && !hasClass($('reset-top'), 'busy'),
    `${$('reset-top').attrs.title} | ${$('reset-top').attrs['aria-label']} | ${$('reset-top').className}`);
  check('19f ปุ่มบนแถบบนอยู่ในแถบ head-right ถัดจากกุญแจ ก่อนปุ่มหยุด', /id="lock"[\s\S]*?id="reset-top"[\s\S]*?id="stop"/.test(html) && /class="head-right"[\s\S]*?id="reset-top"[\s\S]*?<\/header>/.test(html));
  await send(modeOf('STANDBY', 'STARTING', { target: 'NAVIGATION' }));
  check('19f ระหว่างเปลี่ยนโหมด → ปุ่ม Reset all กดไม่ได้ (กันซ้อน) ทั้งสองปุ่ม แต่ยังไม่หมุน', $('mode-reset').disabled === true && $('reset-top').disabled === true && !hasClass($('reset-top'), 'busy'));
  await send(modeOf('MAPPING'));
  const nreset = () => posts('/api/mode/reset').length;
  $('mode-reset').click(); await flush();
  check('19f กด → หน้าต่างแจ้งเตือน (alertdialog) บอกว่าจะปิดอะไร/ไม่ปิดอะไร', $('confirm-dialog').open && $('confirm-dialog').attrs.role === 'alertdialog' &&
    $('confirm-title').textContent === 'Reset all robot systems?' && $('confirm-list').children.length === 3 && $('confirm-ok').textContent === 'Reset all' &&
    /stuck in a mode/.test($('confirm-msg').textContent) && /outside this app/.test($('confirm-list').children[1].textContent) &&
    /stay on/.test($('confirm-list').children[2].textContent), $('confirm-title').textContent);
  $('confirm-cancel').click(); await flush();
  check('19f ยกเลิก → ไม่ส่งคำสั่ง', nreset() === 0);
  $('mode-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19f ยืนยัน → POST /api/mode/reset พร้อมเฮดเดอร์กัน CSRF', nreset() === 1 && posts('/api/mode/reset')[0].headers['X-Requested-With'] === 'agv-web');
  check('19f ตอบ 202 → toast + แสดงสถานะ "กำลังปิด" ทันที', $('toast').textContent === 'Resetting…' && $('mode-state').textContent === 'Stopping' &&
    $('mode-msg').textContent === 'Resetting — stopping all robot systems…', `${$('toast').textContent} | ${$('mode-state').textContent} | ${$('mode-msg').textContent}`);
  check('19f ระหว่างรีเซ็ต: ปุ่มแถบบนหมุน (busy) และกดซ้ำไม่ได้', hasClass($('reset-top'), 'busy') && $('reset-top').disabled === true, $('reset-top').className);
  await send(modeOf('STANDBY', 'RUNNING', { message: M('mode.reset.done', { count: 3 }) }));
  check('19f รีเซ็ตเสร็จ: ปุ่มหยุดหมุน กดได้อีก', !hasClass($('reset-top'), 'busy') && $('reset-top').disabled === false);
  check('19f รีเซ็ตเสร็จ: Standby + ข้อความผลลัพธ์แสดงบนการ์ดโหมด', $('mode-state').textContent === 'Standby' && $('mode-msg').textContent === 'Reset complete — 3 stopped', $('mode-msg').textContent);
  await send(modeOf('STANDBY', 'ERROR', { message: M('mode.err.reset_remaining', { procs: ['amcl', 'sllidar_node'] }) }));
  check('19f ปิดไม่หมด: error บอกชื่อที่เหลือ', /Reset could not stop: amcl, sllidar_node/.test($('mode-msg').textContent), $('mode-msg').textContent);
  await send(modeOf('STANDBY'));

  server.modeReset = { status: 409, data: { ok: false, message: M('mode.err.busy') } };
  $('mode-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19f 409 (กำลังทำอย่างอื่น) → toast เหตุผลที่แปลแล้ว', $('toast').textContent === 'Another mode change is already in progress' || $('toast').textContent.length > 5, $('toast').textContent);
  server.modeReset = { status: 500, data: {} };
  $('mode-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19f ล้มเหลวไม่มีข้อความ → ข้อความสำรอง', $('toast').textContent === 'Could not start the reset', $('toast').textContent);
  server.modeReset = { status: 401, data: { ok: false, error: M('auth.required') } };
  $('mode-reset').click(); await flush(); $('confirm-ok').click(); await flush();
  check('19f 401 → ถอนสถานะผู้ดูแล ซ่อนปุ่ม เปิด PIN', $('mode-reset').hidden === true && $('pin-dialog').open === true && !hasClass($('lock'), 'on'));
  $('pin-cancel').click(); await flush(); server.modeReset = null;
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);

  // ปุ่มแถบบนใช้ได้จากทุกหน้า (ตรงกับหน้าต่างและคำสั่งเดียวกับปุ่มในการ์ด)
  for (const page of ['#/mapping', '#/settings', '#/home']) {
    await send(modeOf('STANDBY')); nav(page); await flush();      // ย้อนกลับเป็นสถานะนิ่งก่อน (หลังรีเซ็ตครั้งก่อนปุ่มกดไม่ได้ชั่วคราว)
    const before = nreset();
    $('reset-top').click(); await flush();
    check(`19f ปุ่มแถบบนจากหน้า ${page}: ขึ้นหน้าต่างเตือน`, $('confirm-dialog').open && $('confirm-title').textContent === 'Reset all robot systems?' && $('confirm-list').children.length === 3);
    $('confirm-ok').click(); await flush();
    check(`19f ปุ่มแถบบนจากหน้า ${page}: ยืนยัน → POST reset 1 ครั้ง`, nreset() === before + 1, `${before} → ${nreset()}`);
  }
  await send(modeOf('STANDBY'));
  const nBeforeCancel = nreset();
  $('reset-top').click(); await flush(); $('confirm-cancel').click(); await flush();
  check('19f ปุ่มแถบบน: ยกเลิก → ไม่ส่งคำสั่ง', nreset() === nBeforeCancel, `${nBeforeCancel} → ${nreset()}`);
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } }; await advance(15000);
  check('19f หมดสิทธิ์ผู้ดูแล → ปุ่มแถบบนหายพร้อมกัน', $('reset-top').hidden === true && $('mode-reset').hidden === true);
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);

  // ระบบค้างจากรอบก่อน (leftover) / ระบบนอกแอป
  await send(modeOf('STANDBY', 'RUNNING', { leftover: { mode: 'MAPPING' } }));
  check('19f ของค้างจากรอบก่อน: แบนเนอร์บอกโหมด + ชี้ไป Reset all', $('mode-ext').hidden === false &&
    $('mode-ext').textContent === 'A previous session left the Mapping system running. Modes cannot be changed until it is stopped — an admin can use Reset all.', $('mode-ext').textContent);
  check('19f ของค้าง: ปุ่มโหมดกดไม่ได้ แต่ Reset all กดได้ + ป้ายแถบบน = External system', segButtons.every((b) => b.disabled) && $('mode-reset').hidden === false &&
    $('mode-reset').disabled === false && $('pill-text').textContent === 'External system', `${states()} | ${$('pill-text').textContent}`);
  check('19f ของค้าง: ชื่อโหมด = รันนอกแอป', $('mode-name').textContent === 'External system');
  nav('#/mapping'); await flush();
  check('19f หน้า Mapping: ข้อความของค้าง ชี้ไป Reset all และไม่มีปุ่มเริ่ม', $('map-gate-msg').textContent === 'A previous session left the Mapping system running. An admin can stop it with Reset all on the Home page.' &&
    $('map-start').hidden === true, $('map-gate-msg').textContent);
  await send(modeOf('STANDBY', 'RUNNING', { external: ['slam_toolbox'] }));
  check('19f ระบบนอกแอป: หน้า Mapping ชี้ไป Reset all', /An admin can stop it with Reset all on the Home page, then start Mapping mode\.$/.test($('map-gate-msg').textContent), $('map-gate-msg').textContent);
  nav('#/home'); await flush();
  check('19f ระบบนอกแอป: แบนเนอร์เดิม + Reset all กดได้', /^A system is running outside this app \(slam_toolbox\)\..*Reset all\.$/.test($('mode-ext').textContent) && $('mode-reset').disabled === false, $('mode-ext').textContent);
  langBtn('th').click(); await flush();
  check('19f สลับ TH: ปุ่มแถบบนเปลี่ยนภาษา (aria)', $('reset-top').attrs['aria-label'] === 'รีเซ็ตระบบหุ่นทั้งหมด', $('reset-top').attrs['aria-label']);
  check('19f สลับ TH: ข้อความ/ปุ่มเปลี่ยนทันที', /ใช้ Reset all|Reset all/.test($('mode-ext').textContent) && /ระบบ/.test($('mode-ext').textContent) && $('mode-reset').textContent === 'Reset all', $('mode-ext').textContent);
  $('mode-reset').click(); await flush();
  check('19f TH: หน้าต่างยืนยันเป็นไทย', $('confirm-title').textContent === 'รีเซ็ตระบบหุ่นทั้งหมด?' && $('confirm-ok').textContent === 'รีเซ็ตทั้งหมด', $('confirm-title').textContent);
  $('confirm-cancel').click(); await flush();
  langBtn('en').click(); await send(modeOf('STANDBY'));
  check('19f กลับ EN: ไม่มีตัวอักษรไทยหลุด', thaiLeaks().length === 0, thaiLeaks().join(' | '));

  // ═════════ 19g) ตั้งค่า SLAM ก่อนเริ่ม Mapping ═════════
  server.slamState = null; server.slamPost = null; server.map = null;
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } }; await advance(15000);
  await send(modeOf('STANDBY')); nav('#/home'); await flush();
  const slamCalls = () => server.calls.filter((c) => c.path === '/api/slam/params' && c.method === 'GET').length;
  const slamC0 = slamCalls();
  nav('#/mapping'); await advance(100);
  const findTag = (el, tag) => el.tag === tag ? el : el.children.map((c) => findTag(c, tag)).find(Boolean) || null;
  const slamInput = (k) => { const r = rowOf(k); return r ? findTag(r.children[3], 'input') : null; };
  const slamRow = () => $('slam-groups').children.flatMap((g) => g.children.slice(1));
  const rowOf = (k) => slamRow().find((r) => r.children[0].children[0].attrs.for === `slam-in-${k}`);
  const errOf = (k) => rowOf(k).children[4];
  const badgeOf = (k) => rowOf(k).children[0].children[1];
  const defOf = (k) => rowOf(k).children[2];
  check('19g เข้าหน้า Mapping → โหลดค่า SLAM 1 ครั้ง', slamCalls() === slamC0 + 1, `${slamC0} → ${slamCalls()}`);
  check('19g แสดงหัวข้อ 3 กลุ่ม + 5 พารามิเตอร์', $('slam-groups').children.length === 3 && slamRow().length === 5, `${$('slam-groups').children.length} / ${slamRow().length}`);
  check('19g ชื่อ/คำอธิบายจากพจนานุกรม', rowOf('resolution').children[0].children[0].textContent === 'Map resolution' &&
    /Size of one map cell/.test(rowOf('resolution').children[1].textContent) && $('slam-groups').children[0].children[0].textContent === 'Map & range', rowOf('resolution').children[0].children[0].textContent);
  check('19g แสดงค่า Default ของทุกตัว (พร้อมหน่วย/เปิด-ปิด)', defOf('resolution').textContent === 'Default: 0.05 m' && defOf('max_laser_range').textContent === 'Default: 25 m' &&
    defOf('throttle_scans').textContent === 'Default: 1' && defOf('do_loop_closing').textContent === 'Default: On', `${defOf('resolution').textContent} | ${defOf('do_loop_closing').textContent}`);
  check('19g ช่องกรอกแสดงค่าปัจจุบัน = Default', slamInput('resolution').value === '0.05' && slamInput('do_loop_closing').checked === true);
  check('19g ไม่ใช่ผู้ดูแล: ช่องทั้งหมดแก้ไม่ได้ + บอกให้ล็อกอิน + ปุ่มบันทึกกดไม่ได้', slamRow().every((r) => findTag(r.children[3], 'input').disabled) &&
    $('slam-note').hidden === false && $('slam-note').textContent === 'Sign in as admin to change these settings.' && $('slam-save').disabled === true, $('slam-note').textContent);
  check('19g สรุป + คำใบ้ก่อนเริ่ม', $('slam-summary').textContent === 'All defaults' && /^Applied when Mapping starts\./.test($('slam-hint').textContent), $('slam-hint').textContent);
  check('19g ค่าขั้นสูงแสดงอ่านอย่างเดียว (ตาราง key/ค่า)', $('slam-adv-body').children.length === 2 && $('slam-adv-body').children[0].children[0].textContent === 'scan_topic' &&
    $('slam-adv-body').children[0].children[1].textContent === '/scan_filtered');
  check('19g ไม่มีป้าย "Changed" / ปุ่มคืนค่า เมื่อเป็น Default', slamRow().every((r) => r.children[0].children[1].hidden === true));

  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);
  check('19g ผู้ดูแล: ช่องแก้ได้ ไม่มีข้อความให้ล็อกอิน', slamInput('resolution').disabled === false && slamInput('do_loop_closing').disabled === false && $('slam-note').hidden === true);
  const typeIn = async (k, v) => { slamInput(k).value = v; slamInput(k).dispatch('input'); await flush(); };
  await typeIn('resolution', '0.1');
  check('19g แก้ค่า → ป้าย Changed + ปุ่มคืนค่า + สรุป "Unsaved" + ปุ่มบันทึกกดได้', badgeOf('resolution').hidden === false && errOf('resolution').hidden === true &&
    $('slam-summary').textContent === 'All defaults · Unsaved changes' && $('slam-save').disabled === false && $('slam-restore').disabled === false, $('slam-summary').textContent);
  check('19g ค่า Default ยังแสดงอยู่หลังแก้', defOf('resolution').textContent === 'Default: 0.05 m');
  for (const [k, v, msg] of [['resolution', '0.5', 'Enter a number from 0.02 to 0.2'], ['resolution', '', 'Enter a number from 0.02 to 0.2'], ['resolution', 'abc', 'Enter a number from 0.02 to 0.2'],
    ['resolution', '0.01', 'Enter a number from 0.02 to 0.2'], ['throttle_scans', '2.5', 'Enter a whole number from 1 to 10'], ['throttle_scans', '11', 'Enter a whole number from 1 to 10'],
    ['max_laser_range', '4', 'Enter a number from 5 to 30'], ['resolution', '1e-1x', 'Enter a number from 0.02 to 0.2']]) {
    await typeIn(k, v);
    check(`19g ค่าผิด ${k}="${v}" → ข้อความ + aria-invalid + บันทึกไม่ได้`, errOf(k).hidden === false && errOf(k).textContent === msg && slamInput(k).attrs['aria-invalid'] === 'true' && $('slam-save').disabled === true, errOf(k).textContent);
    await typeIn(k, k === 'throttle_scans' ? '1' : (k === 'max_laser_range' ? '25' : '0.1'));
  }
  await typeIn('resolution', '0.02'); await typeIn('resolution', '0.2');
  check('19g ขอบช่วงยอมรับ (0.02 และ 0.2)', errOf('resolution').hidden === true && $('slam-save').disabled === false);
  await typeIn('resolution', ' 0.08 ');
  check('19g ช่องว่างหน้า-หลังตัวเลขใช้ได้', errOf('resolution').hidden === true);

  // คืนค่ารายตัว
  const resetBtn = (k) => rowOf(k).children[3].children.filter((c) => hasClass(c, 'slam-reset'))[0];
  check('19g ปุ่ม ↺ ของแถวที่แก้เท่านั้นที่เห็น', resetBtn('resolution').hidden === false && resetBtn('max_laser_range').hidden === true);
  resetBtn('resolution').click(); await flush();
  check('19g กด ↺ → กลับเป็น Default + ปุ่มบันทึกกดไม่ได้', slamInput('resolution').value === '0.05' && badgeOf('resolution').hidden === true && $('slam-save').disabled === true && resetBtn('resolution').hidden === true);

  // บันทึก (เฉพาะที่แก้) + ชนิดของค่า
  await typeIn('resolution', '0.1'); await typeIn('throttle_scans', '3');
  slamInput('do_loop_closing').checked = false; slamInput('do_loop_closing').dispatch('change'); await flush();
  const ps0 = posts('/api/slam/params').length;
  $('slam-save').click(); await flush();
  check('19g บันทึก → POST เฉพาะค่าที่แก้ ชนิดถูก (ตัวเลข/จำนวนเต็ม/boolean)', posts('/api/slam/params').length === ps0 + 1 &&
    JSON.stringify(posts('/api/slam/params')[ps0].body) === JSON.stringify({ values: { resolution: 0.1, throttle_scans: 3, do_loop_closing: false } }) &&
    posts('/api/slam/params')[ps0].headers['X-Requested-With'] === 'agv-web', JSON.stringify(posts('/api/slam/params')[ps0] && posts('/api/slam/params')[ps0].body));
  check('19g หลังบันทึก: toast + สรุป 3 ค่า + ปุ่มบันทึกปิด + ป้าย Changed', $('toast').textContent === 'SLAM settings saved' && $('slam-summary').textContent === '3 changed from default' &&
    $('slam-save').disabled === true && badgeOf('resolution').hidden === false && badgeOf('max_laser_range').hidden === true, $('slam-summary').textContent);
  check('19g หลังบันทึก: ค่า Default ยังเป็นค่าเดิม ไม่ถูกทับ', defOf('resolution').textContent === 'Default: 0.05 m' && defOf('do_loop_closing').textContent === 'Default: On');

  // คืนค่าทั้งหมด → ต้องกดบันทึกอีกครั้ง (ไม่ลบทันที)
  $('slam-restore').click(); await flush();
  check('19g คืนค่าทั้งหมด → ช่องกลับเป็น Default แต่ยังไม่บันทึก (ปุ่มบันทึกกดได้)', slamInput('resolution').value === '0.05' && slamInput('throttle_scans').value === '1' && slamInput('do_loop_closing').checked === true &&
    $('slam-save').disabled === false && $('slam-summary').textContent === '3 changed from default · Unsaved changes', $('slam-summary').textContent);
  $('slam-save').click(); await flush();
  check('19g บันทึกค่าที่เท่า Default → เซิร์ฟเวอร์ลบส่วนต่าง → "All defaults"', $('slam-summary').textContent === 'All defaults' && $('slam-restore').disabled === true && slamRow().every((r) => r.children[0].children[1].hidden === true), $('slam-summary').textContent);

  // บันทึกล้มเหลว
  await typeIn('resolution', '0.1');
  server.slamPost = { status: 400, data: { ok: false, error: M('slam.err.range', { key: 'resolution', min: 0.02, max: 0.2 }) } };
  $('slam-save').click(); await flush();
  check('19g เซิร์ฟเวอร์ปฏิเสธ → toast ข้อความที่แปลแล้ว และไม่ทิ้งสิ่งที่แก้', $('toast').textContent === 'resolution must be between 0.02 and 0.2' && slamInput('resolution').value === '0.1' && $('slam-save').disabled === false, $('toast').textContent);
  server.slamPost = { status: 500, data: {} };
  $('slam-save').click(); await flush();
  check('19g ล้มเหลวไม่มีข้อความ → ข้อความสำรอง', $('toast').textContent === 'Could not save the SLAM settings', $('toast').textContent);
  server.slamPost = { status: 401, data: { ok: false, error: M('auth.required') } };
  $('slam-save').click(); await flush();
  check('19g 401 → ถอนสิทธิ์ผู้ดูแล + ช่องกลับเป็นอ่านอย่างเดียว + เปิด PIN', $('pin-dialog').open === true && slamInput('resolution').disabled === true && $('slam-save').disabled === true && !hasClass($('lock'), 'on'));
  $('pin-cancel').click(); await flush(); server.slamPost = null;
  server.session = { status: 200, data: { ok: true, admin: true, auth_enabled: true } }; await advance(15000);

  // สิ่งที่แก้ค้างไว้ต้องไม่หายเมื่อสถานะโหมดเปลี่ยน (ดึงค่าใหม่) หรือสลับภาษา
  check('19g (เตรียม) ยังมีค่าที่แก้ค้างอยู่', slamInput('resolution').value === '0.1');
  await send(modeOf('STANDBY', 'STARTING', { target: 'MAPPING' })); await advance(100);
  check('19g กำลังเปิด Mapping: ช่องแก้ไม่ได้ (ล็อก) แต่ไม่ทิ้งค่าที่แก้ค้าง', slamInput('resolution').disabled === true);
  await send(modeOf('STANDBY')); await advance(100);
  check('19g กลับ Standby: ค่าที่แก้ค้างยังอยู่ + แก้ได้อีก', slamInput('resolution').value === '0.1' && slamInput('resolution').disabled === false && $('slam-save').disabled === false, slamInput('resolution').value);
  langBtn('th').click(); await flush();
  check('19g สลับ TH: ชื่อ/Default เป็นไทย และค่าที่แก้ค้างไม่หาย', rowOf('resolution').children[0].children[0].textContent === 'ความละเอียดแผนที่' &&
    defOf('resolution').textContent === 'ค่า Default: 0.05 m' && slamInput('resolution').value === '0.1' && $('slam-save').textContent === 'บันทึกการตั้งค่า', rowOf('resolution').children[0].children[0].textContent);
  langBtn('en').click(); await flush();

  // หน้าต่างแจ้งเตือนก่อนเริ่ม Mapping บอกจำนวนค่าที่ต่างจาก Default และค่าที่ยังไม่บันทึก
  server.slamState = makeSlam({ resolution: 0.1, throttle_scans: 3 });
  await typeIn('resolution', '0.15');                 // แก้ค้างไว้ ต่างจากค่าที่บันทึกบนเซิร์ฟเวอร์ (0.1)
  $('map-start').click(); await flush();
  check('19g alert ก่อนเริ่ม Mapping: บอกจำนวนค่าที่ต่างจาก Default + เตือนค่าที่ยังไม่บันทึก', $('confirm-dialog').open && $('confirm-list').children.length === 5 &&
    $('confirm-list').children[3].textContent === '2 SLAM setting(s) differ from the defaults.' && /unsaved SLAM changes/.test($('confirm-list').children[4].textContent), $('confirm-list').children.map((c) => c.textContent).join(' | '));
  check('19g alert: ดึงค่าล่าสุดมาแสดงแต่ไม่ทิ้งสิ่งที่แก้ค้าง', slamInput('resolution').value === '0.15' && slamInput('throttle_scans').value === '3', `${slamInput('resolution').value} ${slamInput('throttle_scans').value}`);
  $('confirm-cancel').click(); await flush();
  server.slamState = makeSlam({}); $('map-start').click(); await flush();
  check('19g alert: ไม่มีค่าที่ต่างจาก Default และไม่มีค่าค้าง → มีแค่ 3 ข้อเดิม', $('confirm-list').children.length === 4 && !/differ/.test($('confirm-list').children.map((c) => c.textContent).join('|')), $('confirm-list').children.map((c) => c.textContent).join(' | '));
  $('confirm-cancel').click(); await flush();

  // ระหว่างรัน Mapping: แสดงค่าที่ใช้จริงแบบอ่านอย่างเดียว
  server.slamState = makeSlam({ resolution: 0.1 }, { in_use: { resolution: 0.1, max_laser_range: 25, throttle_scans: 2, do_loop_closing: false, loop_search_maximum_distance: 8 } });
  await send({ ...modeOf('MAPPING'), pose: { x: 0, y: 0, yaw_deg: 0 } }); await advance(100);
  check('19g ระหว่างรัน: คำใบ้ "Values in use" + ปุ่มบันทึก/คืนค่าซ่อน', /^Values in use for this run/.test($('slam-hint').textContent) && $('slam-save').hidden === true && $('slam-restore').hidden === true, $('slam-hint').textContent);
  check('19g ระหว่างรัน: ช่องแสดงค่าที่ใช้จริง (ไม่ใช่ค่าที่บันทึก) และแก้ไม่ได้', slamInput('throttle_scans').value === '2' && slamInput('do_loop_closing').checked === false &&
    slamRow().every((r) => findTag(r.children[3], 'input').disabled), `${slamInput('throttle_scans').value}`);
  check('19g ระหว่างรัน: ป้าย Changed ตามค่าที่ใช้จริงเทียบ Default', badgeOf('throttle_scans').hidden === false && badgeOf('max_laser_range').hidden === true && badgeOf('do_loop_closing').hidden === false);
  await send(modeOf('STANDBY')); server.slamState = null; await advance(100);
  check('19g ออกจาก Mapping: ช่องกลับมาแก้ได้ และคำใบ้กลับเป็นก่อนเริ่ม', /^Applied when Mapping starts/.test($('slam-hint').textContent) && $('slam-save').hidden === false);

  // ใช้ไม่ได้ (อ่านไฟล์ Default ไม่ได้)
  server.slamState = { available: false, groups: [], advanced: [], modified: 0, in_use: null };
  await send(modeOf('STANDBY', 'STARTING', { target: 'MAPPING' })); await send(modeOf('STANDBY')); await advance(100);
  check('19g ไม่มีไฟล์ Default: บอกว่าใช้ไม่ได้ ไม่มีแถว/ปุ่ม', $('slam-hint').textContent === 'SLAM settings are not available (the default settings file could not be read).' &&
    $('slam-groups').children.length === 0 && $('slam-save').hidden === true && $('slam-restore').hidden === true, $('slam-hint').textContent);
  server.slamState = null;
  langBtn('en').click(); nav('#/home'); await flush();
  const slamC1 = slamCalls(); await advance(20000);
  check('19g ไม่ใช่หน้า Mapping → ไม่ดึงค่า SLAM', slamCalls() === slamC1);
  check('19g EN ล้วน: ไม่มีตัวอักษรไทยหลุดในหน้า SLAM', thaiLeaks().length === 0, thaiLeaks().join(' | '));

  // ═════════ 19h) หน้า Mapping แบบ 3 ส่วน · การแสดงผล rviz · ขับด้วยมือ · บันทึกแผนที่ ═════════
  const asAdmin = async (on = true) => { server.session = { status: 200, data: { ok: true, admin: on, auth_enabled: true } }; await advance(15000); };
  await asAdmin(true); server.scan = null; server.saved = null; server.map = null; server.slamState = null; server.saveHandler = null;
  langBtn('en').click(); await flush();
  // ตำแหน่งภายในส่วนของหน้า Mapping เท่านั้น (หน้า Navigation ใช้โครง map-layout/map-center เดียวกันโดยตั้งใจ)
  const mappingAt = html.indexOf('data-view="mapping"');
  const hpos = (needle) => html.indexOf(needle, mappingAt);
  check('19h ไม่มีปุ่ม "Back to Home" ในทุกหน้า', !/class="back"/.test(html) && !/Back to Home/.test(html) && !('ui.back' in I.DICT.en) && !('ui.back' in I.DICT.th));
  check('19h Mapping: ซ้าย(ตั้งค่า) → กลาง(แผนที่) → ขวา(ควบคุม+บันทึก) ตามลำดับใน DOM', hpos('id="map-left"') < hpos('class="map-center"') && hpos('class="map-center"') < hpos('id="map-right"'));
  check('19h แถบซ้ายมี Display + SLAM · กลางมีเกต+แผนที่สด · ขวามีควบคุมแล้วบันทึกแผนที่เป็นอันล่างสุด',
    hpos('id="map-left"') < hpos('id="display-card"') && hpos('id="display-card"') < hpos('id="slam-card"') && hpos('id="slam-card"') < hpos('class="map-center"') &&
    hpos('class="map-center"') < hpos('id="map-gate"') && hpos('id="map-gate"') < hpos('id="map-live"') && hpos('id="map-live"') < hpos('id="map-right"') &&
    hpos('id="map-right"') < hpos('id="drive-card"') && hpos('id="drive-card"') < hpos('id="savemap-card"') && html.indexOf('</aside>', hpos('id="savemap-card"')) > 0 &&
    !/<\/aside>[\s\S]*id="savemap-card"/.test(html.slice(hpos('id="map-right"'), hpos('id="savemap-card"'))));
  const cssText = read('web/style.css');
  check('19h CSS: จอ ≥1100px = 3 คอลัมน์ (ซ้าย 300px · กลางยืดได้ · ขวา 330px) และจอแคบเรียงแผนที่ → ควบคุม → ตั้งค่า',
    /@media \(min-width: 1100px\)[\s\S]*?grid-template-columns: 300px minmax\(0, 1fr\) 330px/.test(cssText) && /\.map-right \{ order: 2; \}/.test(cssText) && /\.map-left \{ order: 3; \}/.test(cssText));

  const mapOn = async (over = {}) => { nav('#/mapping'); await send({ ...modeOf('MAPPING'), pose: { x: 1.0, y: 2.0, yaw_deg: 0 }, ...over }); await advance(100); };
  server.map = { status: 200, data: MAPMETA(20) };
  await mapOn(); await advance(3100);

  // ── การแสดงผลแบบ rviz2 ──
  const dispInput = (id) => byId[id];
  check('19h Display: ค่าตั้งต้น (laser เปิด · 3 px · แดง · 100% · decay 0 · แผนที่ 100% · หุ่น/สเกลเปิด)', dispInput('disp-laser').checked === true && dispInput('disp-size').value === '3' &&
    dispInput('disp-alpha').value === '100' && dispInput('disp-decay').value === '0' && dispInput('disp-map-alpha').value === '100' && dispInput('disp-robot').checked && dispInput('disp-scalebar').checked &&
    $('disp-size-out').textContent === '3 px' && $('disp-decay-out').textContent === '0 s', $('disp-size-out').textContent);
  const sw = () => $('disp-colors').children;
  check('19h Display: สี 5 ตัวเป็น radio (แดงถูกเลือก) มีชื่อสำหรับโปรแกรมอ่านหน้าจอ', sw().length === 5 && sw().every((b) => b.attrs.role === 'radio' && b.attrs['aria-label']) &&
    sw().filter((b) => b.attrs['aria-checked'] === 'true').length === 1 && sw()[0].attrs['aria-checked'] === 'true' && sw()[0].attrs['aria-label'] === 'Red', sw().map((b) => b.attrs['aria-label']).join(','));
  const stored = () => JSON.parse(storage['agv.map.display']);
  const setRange = async (id, v) => { dispInput(id).value = v; dispInput(id).dispatch('input'); await flush(); };

  server.scan = { status: 200, data: { available: true, version: 1, age: 0.1, origin: [1, 2], count: 3, points: [[1.0, 2.0], [2.0, 2.0], [1.0, 3.0]] } };
  await advance(250);
  const rects = () => mlog('fillRect').filter((c) => c.args[2] === c.args[3]);        // จุดสแกนเป็นสี่เหลี่ยมจัตุรัส (สเกลบาร์เป็นสี่เหลี่ยมผืนผ้า)
  const lastDraw = () => { const log = mcanvas.ctxLog; const k = log.map((c) => c.name).lastIndexOf('clearRect'); return log.slice(k); };
  const drawn = () => lastDraw().filter((c) => c.name === 'fillRect' && c.args[2] === c.args[3]);
  check('19h Laser: ดึงสแกนและวาด 3 จุดทับแผนที่ (ขนาด 3 px)', drawn().length === 3 && drawn().every((c) => c.args[2] === 3), String(drawn().length));
  const robotXY = robotTx();
  check('19h Laser: จุดที่ตำแหน่งเดียวกับหุ่น (1.0, 2.0) อยู่ตรงกลางหุ่นพอดี (พิกัดโลก→จอตรงกัน)', Math.abs(drawn()[0].args[0] + 1.5 - robotXY[0]) < 1e-6 && Math.abs(drawn()[0].args[1] + 1.5 - robotXY[1]) < 1e-6, `${drawn()[0].args} vs ${robotXY}`);
  const setsBefore = lastDraw().filter((c) => c.name === 'set:fillStyle').map((c) => c.args[0]);
  check('19h Laser: สีแดงตามค่าตั้งต้น และวาดก่อนหุ่น (หุ่นอยู่บนสุด)', setsBefore.includes('#ff3b30') && lastDraw().map((c) => c.name).indexOf('fillRect') < lastDraw().map((c) => c.name).lastIndexOf('fill'));
  await setRange('disp-size', '6');
  check('19h Display: ปรับขนาดจุด → วาดใหม่ทันที 6 px + ข้อความ + จำค่าในเครื่อง', drawn().every((c) => c.args[2] === 6) && $('disp-size-out').textContent === '6 px' && stored().size === 6, String(drawn().map((c) => c.args[2])));
  sw()[2].click(); await flush();
  check('19h Display: เลือกสีฟ้า → ใช้สีนั้นวาด + aria-checked ย้าย + จำค่า', lastDraw().filter((c) => c.name === 'set:fillStyle').map((c) => c.args[0]).includes('#2bd9f2') &&
    sw()[2].attrs['aria-checked'] === 'true' && sw()[0].attrs['aria-checked'] === 'false' && stored().color === 'cyan');
  await setRange('disp-alpha', '50');
  check('19h Display: ความทึบของเลเซอร์ 50% → globalAlpha 0.5 ตอนวาดจุด', lastDraw().some((c) => c.name === 'set:globalAlpha' && c.args[0] === 0.5) && $('disp-alpha-out').textContent === '50 %' && stored().alpha === 50);
  await setRange('disp-map-alpha', '60');
  const dl = lastDraw().map((c) => c.name + (c.name === 'set:globalAlpha' ? c.args[0] : ''));
  check('19h Display: ความทึบแผนที่ 60% → ตั้ง globalAlpha 0.6 ก่อนวาดภาพแผนที่', dl.indexOf('set:globalAlpha0.6') >= 0 && dl.indexOf('set:globalAlpha0.6') < dl.indexOf('drawImage') && stored().mapAlpha === 60, dl.join(','));
  dispInput('disp-laser').checked = false; dispInput('disp-laser').dispatch('input'); await flush();
  check('19h Display: ปิด Show laser scan → ไม่มีจุดสแกน', drawn().length === 0 && stored().laser === false);
  const pollsOff = server.calls.filter((c) => c.path === '/api/scan/live').length; await advance(1000);
  check('19h Display: ปิดเลเซอร์แล้วไม่ดึงสแกนอีก (ประหยัด WiFi)', server.calls.filter((c) => c.path === '/api/scan/live').length === pollsOff);
  dispInput('disp-laser').checked = true; dispInput('disp-laser').dispatch('input'); await advance(300);
  dispInput('disp-robot').checked = false; dispInput('disp-robot').dispatch('input'); await flush();
  check('19h Display: ปิด Show robot → ไม่วาดหุ่น (ไม่มี fill)', lastDraw().filter((c) => c.name === 'fill').length === 0 && stored().robot === false);
  dispInput('disp-scalebar').checked = false; dispInput('disp-scalebar').dispatch('input'); await flush();
  check('19h Display: ปิดสเกลบาร์ → ไม่มีข้อความสเกล', lastDraw().filter((c) => c.name === 'fillText').length === 0 && stored().scalebar === false);
  $('disp-reset').click(); await flush();
  check('19h Display: Reset display → กลับค่าตั้งต้นทั้งหมดและจำค่าใหม่', dispInput('disp-size').value === '3' && sw()[0].attrs['aria-checked'] === 'true' && dispInput('disp-robot').checked && dispInput('disp-scalebar').checked &&
    JSON.stringify(stored()) === JSON.stringify({ laser: true, robot: true, scalebar: true, size: 3, alpha: 100, decay: 0, mapAlpha: 100, color: 'red' }), JSON.stringify(stored()));

  // Decay time: เก็บสแกนย้อนหลัง
  fakeDate = true;
  const alive = () => send({ ...modeOf('MAPPING'), pose: { x: 1.0, y: 2.0, yaw_deg: 0 } });      // ให้หน้าเว็บเห็นว่ายังเชื่อมต่ออยู่ (เวลาปลอมเดิน)
  await alive();
  await setRange('disp-decay', '2');
  check('19h Display: decay 2 s → ข้อความ "2 s"', $('disp-decay-out').textContent === '2 s' && stored().decay === 2);
  server.scan = { status: 200, data: { available: true, version: 10, age: 0.1, origin: [1, 2], count: 2, points: [[1, 2], [2, 2]] } }; await advance(250); await alive();
  server.scan = { status: 200, data: { available: true, version: 11, age: 0.1, origin: [1, 2], count: 3, points: [[1, 3], [2, 3], [3, 3]] } }; await advance(250); await alive();
  check('19h Laser decay: สแกนใหม่ + สแกนเก่า (ภายใน 2 วินาที) แสดงพร้อมกัน = 5 จุด', drawn().length === 5, String(drawn().length));
  for (let i = 0; i < 6; i++) { await advance(500); await alive(); }
  server.scan = { status: 200, data: { available: true, version: 12, age: 0.1, origin: [1, 2], count: 1, points: [[0, 0]] } }; await advance(250);
  check('19h Laser decay: สแกนที่เก่ากว่า 2 วินาทีถูกตัดทิ้ง เหลือเฉพาะล่าสุด', drawn().length === 1, String(drawn().length));
  await setRange('disp-decay', '0'); server.scan = { status: 200, data: { available: true, version: 13, age: 0.1, origin: [1, 2], count: 2, points: [[1, 1], [2, 1]] } }; await advance(250);
  check('19h Laser decay 0: แสดงเฉพาะสแกนล่าสุดเสมอ (เหมือน rviz)', drawn().length === 2);
  const nDup = drawn().length; await advance(1000);
  check('19h Laser: เวอร์ชันเดิม (ไม่มีสแกนใหม่) ไม่ถูกเพิ่มซ้ำ', drawn().length === nDup);
  server.scan = { status: 200, data: { available: true, version: 14, age: 5.0, origin: [1, 2], count: 2, points: [[1, 1], [2, 1]] } }; await advance(250);
  check('19h Laser: สแกนเก่าเกิน 2 วินาที (เลเซอร์หยุดส่ง) → ไม่วาดจุดค้าง', drawn().length === 0, String(drawn().length));
  server.scan = { status: 200, data: { available: false, version: 15 } }; await advance(250);
  check('19h Laser: ไม่มีสแกน → ไม่มีจุด', drawn().length === 0);
  fakeDate = false; await alive();

  // ค่าที่เก็บไว้ในเครื่องเสียหาย/แปลกต้องถูกตัดให้ปลอดภัย (ใช้ฟังก์ชันจริงจาก app.js)
  {
    const from = appSource.indexOf("var DISP_COLORS"), to = appSource.indexOf('function loadDisp()');
    const san = new Function(appSource.slice(from, to).replace('function clampNum', 'function clampNum') + '; return sanitizeDisp;')();
    const d = san({ size: 99, alpha: -5, decay: 'x', mapAlpha: 1000, color: 'purple', laser: 'yes', robot: 0 });
    check('19h sanitize: ค่าเกิน/ผิดชนิดถูกตัดเป็นค่าที่ปลอดภัย', d.size === 8 && d.alpha === 10 && d.decay === 0 && d.mapAlpha === 100 && d.color === 'red' && d.laser === true && d.robot === true, JSON.stringify(d));
    check('19h sanitize: null/สตริง/อาร์เรย์ → ค่าตั้งต้น', JSON.stringify(san(null)) === JSON.stringify(san(undefined)) && san('x').size === 3 && san([1]).size === 3 && san({ size: 4.4 }).size === 4 && san({ decay: 1.3 }).decay === 1.5);
  }

  // ── ขับด้วยมือ ──
  const dsocks = () => driveSockets.length;
  const sentOf = (s, t) => s.sent.filter((m) => m.t === t);
  check('19h Drive: Mapping + Admin + หน้า Mapping → เปิดซ็อกเก็ต /ws/drive 1 ตัว', dsocks() >= 1 && driveSock().url === 'ws://robot.local:8080/ws/drive' && driveSock().readyState === 0, String(dsocks()));
  check('19h Drive: ระหว่างเชื่อมต่อ ปุ่ม/จอยใช้ไม่ได้ + บอกสถานะ', $('drive-status').textContent === 'Connecting…' && hasClass($('drive-card'), 'off') && $('drive-stop').disabled === true && $('dp-fwd').disabled === true);
  const sBefore = driveSock();
  $('joy').dispatch('pointerdown', { pointerId: 1, clientX: 160, clientY: 55 }); await advance(300);
  check('19h Drive: จอยกดก่อนพร้อม → ไม่ส่งอะไร', sBefore.sent.length === 0);
  let ds = await driveOpen();
  check('19h Drive: เซิร์ฟเวอร์ตอบ ready → สถานะ Ready + ปุ่มใช้ได้ + สไลเดอร์ตามเพดานหุ่น', $('drive-status').textContent === 'Ready — hold to move' && !hasClass($('drive-card'), 'off') && $('dp-fwd').disabled === false &&
    $('drive-stop').disabled === false && $('drive-vmax').attrs.max === '0.5' && $('drive-wmax').attrs.max === '1', $('drive-status').textContent);
  $('joy').dispatch('pointerup', { pointerId: 1 }); await advance(300);                 // ล้างอินพุตค้างจากก่อน ready
  ds.sent.length = 0;
  await advance(2100);
  check('19h Drive: ว่างอยู่ → ส่งเฉพาะ heartbeat ~ทุก 1 วินาที (ไม่ส่งคำสั่งขับ)', sentOf(ds, 'hb').length >= 2 && sentOf(ds, 'hb').length <= 3 && sentOf(ds, 'cmd').length === 0, JSON.stringify(ds.sent.map((m) => m.t)));
  ds.sent.length = 0;
  $('joy').dispatch('pointerdown', { pointerId: 7, clientX: 160, clientY: 160 - 105.6 }); await advance(1000);
  let cmds = sentOf(ds, 'cmd');
  check('19h Drive: จอยดันสุดไปข้างหน้า → ส่งคำสั่ง ~10 ครั้ง/วินาที lin=1 ang=0 พร้อมความเร็วสูงสุดที่ตั้ง', cmds.length >= 9 && cmds.length <= 11 && cmds.every((m) => Math.abs(m.lin - 1) < 1e-6 && m.ang === 0 && m.vmax === 0.25 && m.wmax === 0.6), JSON.stringify(cmds[0]) + ` n=${cmds.length}`);
  check('19h Drive: ปุ่มกลมของจอยขยับตามนิ้ว', /translate\(0\.0px, -105\.6px\)/.test($('joy-knob').style.transform), $('joy-knob').style.transform);
  $('joy').dispatch('pointerdown', { pointerId: 8, clientX: 10, clientY: 10 }); ds.sent.length = 0; await advance(300);
  check('19h Drive: นิ้วที่สองถูกเมิน (ใช้นิ้วแรกต่อ)', sentOf(ds, 'cmd').every((m) => Math.abs(m.lin - 1) < 1e-6));
  $('joy').dispatch('pointermove', { pointerId: 7, clientX: 160 - 52.8, clientY: 160 - 52.8 }); ds.sent.length = 0; await advance(150);
  cmds = sentOf(ds, 'cmd');
  check('19h Drive: เฉียงซ้ายหน้า (เบี้ยวครึ่งรัศมี) → lin≈0.5 ang≈+0.5 (ซ้าย = หมุนทวนเข็ม)', cmds.length >= 1 && Math.abs(cmds[0].lin - 0.5) < 0.01 && Math.abs(cmds[0].ang - 0.5) < 0.01, JSON.stringify(cmds[0]));
  $('joy').dispatch('pointermove', { pointerId: 7, clientX: 160 + 400, clientY: 160 - 400 }); ds.sent.length = 0; await advance(150);
  cmds = sentOf(ds, 'cmd');
  check('19h Drive: ลากเลยขอบ → ถูกบีบเป็นวงกลมรัศมี 1 (|v|≤1)', cmds.length >= 1 && Math.abs(Math.hypot(cmds[0].lin, cmds[0].ang) - 1) < 0.01 && cmds[0].ang < 0 && cmds[0].lin > 0, JSON.stringify(cmds[0]));
  $('joy').dispatch('pointermove', { pointerId: 7, clientX: 160 + 3, clientY: 160 - 4 }); ds.sent.length = 0; await advance(1100);
  check('19h Drive: นิ้วอยู่ใกล้ศูนย์กลาง (Deadzone) → ถือว่าศูนย์ ไม่ส่งคำสั่งขับต่อเนื่อง', sentOf(ds, 'cmd').length <= 1 && sentOf(ds, 'cmd').every((m) => m.lin === 0 && m.ang === 0), JSON.stringify(sentOf(ds, 'cmd')));
  $('joy').dispatch('pointermove', { pointerId: 7, clientX: 160, clientY: 160 - 105.6 }); await advance(300);
  ds.sent.length = 0; $('joy').dispatch('pointerup', { pointerId: 7 }); await advance(350);
  check('19h Drive: ปล่อยนิ้ว → ส่งคำสั่งศูนย์ 1 ครั้งแล้วเงียบ (เหลือแต่ heartbeat) + ปุ่มกลับกลาง', sentOf(ds, 'cmd').length === 1 && sentOf(ds, 'cmd')[0].lin === 0 && sentOf(ds, 'cmd')[0].ang === 0 &&
    $('joy-knob').style.transform === 'translate(0px, 0px)', JSON.stringify(ds.sent));
  for (const evType of ['pointercancel', 'lostpointercapture']) {
    $('joy').dispatch('pointerdown', { pointerId: 3, clientX: 160, clientY: 55 }); await advance(250);
    ds.sent.length = 0; $('joy').dispatch(evType, { pointerId: 3 }); await advance(350);
    check(`19h Drive: ${evType} (ระบบแย่งนิ้ว/เปลี่ยนแอป) = ปล่อย → ศูนย์`, sentOf(ds, 'cmd').length === 1 && sentOf(ds, 'cmd')[0].lin === 0, JSON.stringify(ds.sent));
  }

  // ปุ่มทิศทาง
  const press = async (id, type = 'pointerdown') => { $(id).dispatch(type, { pointerId: 20 }); await advance(250); };
  ds.sent.length = 0; await press('dp-fwd'); let last = () => sentOf(ds, 'cmd').slice(-1)[0];
  check('19h Drive: ปุ่ม ▲ ค้าง → lin=1', last() && last().lin === 1 && last().ang === 0, JSON.stringify(last()));
  await press('dp-left'); check('19h Drive: ▲ + ⟲ พร้อมกัน → lin=1 ang=+1', last().lin === 1 && last().ang === 1, JSON.stringify(last()));
  await press('dp-back'); check('19h Drive: ▲ + ▼ ชนกัน → lin=0 (หักล้าง)', last().lin === 0 && last().ang === 1, JSON.stringify(last()));
  for (const id of ['dp-fwd', 'dp-back']) await press(id, 'pointerup');
  await press('dp-left', 'pointerleave'); await advance(250);
  check('19h Drive: ปล่อยปุ่ม/เลื่อนนิ้วออกนอกปุ่ม → ศูนย์', last().lin === 0 && last().ang === 0, JSON.stringify(last()));
  await press('dp-right'); check('19h Drive: ⟳ → ang=-1 (หมุนขวา)', last().ang === -1 && last().lin === 0);
  await press('dp-right', 'pointercancel'); await press('dp-back'); check('19h Drive: ▼ → lin=-1 (ถอยหลัง)', last().lin === -1); await press('dp-back', 'lostpointercapture');

  // คีย์บอร์ด
  const key = async (type, k, extra = {}) => { docListeners[type].forEach((fn) => fn(Object.assign({ key: k, preventDefault() {} }, extra))); await advance(250); };
  ds.sent.length = 0; await key('keydown', 'w'); check('19h Drive: กด W → ขับหน้า', last() && last().lin === 1);
  await key('keydown', 'ArrowRight'); check('19h Drive: W + → → หน้า+หมุนขวา', last().lin === 1 && last().ang === -1);
  await key('keyup', 'w'); await key('keyup', 'ArrowRight'); await advance(250);
  check('19h Drive: ปล่อยปุ่ม → ศูนย์', last().lin === 0 && last().ang === 0);
  ds.sent.length = 0; await key('keydown', 'w', { target: { tag: 'input' } });
  check('19h Drive: พิมพ์ในช่องกรอก (เช่น ชื่อแผนที่) → W ไม่ขับหุ่น', sentOf(ds, 'cmd').length === 0);
  await key('keydown', 's'); ds.sent.length = 0; await key('keydown', ' ');
  check('19h Drive: กด Space → ส่ง stop ทันที + ล้างปุ่มที่ค้างอยู่', sentOf(ds, 'stop').length === 1, JSON.stringify(ds.sent.map((m) => m.t)));
  await key('keyup', 's'); await advance(250);

  // ความเร็ว
  const setSpeed = async (id, v) => { $(id).value = v; $(id).dispatch('input'); await flush(); };
  await setSpeed('drive-vmax', '0.4'); await setSpeed('drive-wmax', '0.9');
  check('19h Drive: สไลเดอร์ความเร็ว → ข้อความ + จำค่าในเครื่อง', $('drive-vmax-out').textContent === '0.40 m/s' && $('drive-wmax-out').textContent === '0.90 rad/s' &&
    JSON.parse(storage['agv.drive.speed']).v === 0.4 && JSON.parse(storage['agv.drive.speed']).w === 0.9, JSON.stringify(storage['agv.drive.speed']));
  ds.sent.length = 0; await press('dp-fwd'); await press('dp-fwd', 'pointerup');
  check('19h Drive: คำสั่งถัดไปส่ง vmax/wmax ใหม่', last() && (sentOf(ds, 'cmd')[0].vmax === 0.4 && sentOf(ds, 'cmd')[0].wmax === 0.9), JSON.stringify(sentOf(ds, 'cmd')[0]));
  await setSpeed('drive-vmax', '9'); check('19h Drive: ตั้งเกินเพดาน (0.5) → ถูกตัดที่เพดาน', $('drive-vmax').value === '0.5' && JSON.parse(storage['agv.drive.speed']).v === 0.5);
  await setSpeed('drive-wmax', '0'); check('19h Drive: ตั้งต่ำเกิน → ถูกยกขึ้นที่ค่าต่ำสุด 0.1', $('drive-wmax').value === '0.1');
  $('drive-speed-reset').click(); await flush();
  check('19h Drive: Reset speeds → ค่าตั้งต้นจากหุ่น (0.25 / 0.6)', $('drive-vmax').value === '0.25' && $('drive-wmax').value === '0.6' && $('drive-vmax-out').textContent === '0.25 m/s');

  // ── สวิตช์เลือกวิธีควบคุม (Joystick ↔ ปุ่มลูกศร) : เลือกอย่างใดอย่างหนึ่ง ──
  check('19h Drive: สวิตช์เริ่มต้นเป็น Joystick → เห็นจอยสติ๊ก ซ่อนปุ่มลูกศร + ป้ายบอกตัวที่เลือก',
    $('joy').hidden === false && $('dpad').hidden === true && $('drive-input-joy').attrs['aria-pressed'] === 'true' && $('drive-input-dpad').attrs['aria-pressed'] === 'false' &&
    $('drive-input-joy').textContent === 'Joystick' && $('drive-input-dpad').textContent === 'Buttons');
  ds.sent.length = 0; $('drive-input-joy').click(); await flush();
  check('19h Drive: กดตัวที่เลือกอยู่แล้ว → ไม่ส่งคำสั่งหยุด/ไม่เปลี่ยนอะไร', sentOf(ds, 'stop').length === 0 && $('joy').hidden === false);
  $('joy').dispatch('pointerdown', { pointerId: 21, clientX: 160, clientY: 160 - 105.6 }); await advance(200);
  check('19h Drive: ระหว่างดันจอย (กำลังส่งคำสั่งขับ)', sentOf(ds, 'cmd').length >= 1 && /-105\.6px/.test($('joy-knob').style.transform));
  ds.sent.length = 0;
  $('drive-input-dpad').click(); await advance(300);
  check('19h Drive: สลับเป็นปุ่มลูกศรกลางทางที่ดันจอยอยู่ → ส่ง stop ทันที + ไม่มีคำสั่งขับที่ไม่เป็นศูนย์หลังจากนั้น + ปุ่มจอยกลับตรงกลาง',
    sentOf(ds, 'stop').length >= 1 && sentOf(ds, 'cmd').every((m) => m.lin === 0 && m.ang === 0) && $('joy-knob').style.transform === 'translate(0px, 0px)', JSON.stringify(ds.sent.slice(0, 4)));
  check('19h Drive: สลับแล้ว → ซ่อนจอย แสดงปุ่มลูกศร + ป้ายสลับตาม + จำค่าไว้ในเครื่อง',
    $('joy').hidden === true && $('dpad').hidden === false && $('drive-input-joy').attrs['aria-pressed'] === 'false' && $('drive-input-dpad').attrs['aria-pressed'] === 'true' && storage['agv.drive.input'] === 'dpad');
  ds.sent.length = 0; await press('dp-fwd'); await advance(150);
  check('19h Drive: โหมดปุ่มลูกศร → กดปุ่มแล้วขับได้', sentOf(ds, 'cmd').some((m) => m.lin > 0));
  await press('dp-fwd', 'pointerup'); await advance(250);
  ds.sent.length = 0; await key('keydown', 'w'); await advance(150);
  check('19h Drive: แป้นพิมพ์ยังใช้ได้เสมอแม้เลือกปุ่มลูกศร', sentOf(ds, 'cmd').some((m) => m.lin > 0));
  await key('keyup', 'w'); await advance(250);
  ds.sent.length = 0;
  $('drive-input-joy').click(); await advance(300);
  check('19h Drive: สลับกลับเป็น Joystick → เห็นจอย ซ่อนปุ่ม + จำค่า', $('joy').hidden === false && $('dpad').hidden === true && storage['agv.drive.input'] === 'joy' && sentOf(ds, 'stop').length >= 1);

  // สถานะที่เซิร์ฟเวอร์ตอบกลับ
  await driveReply(ds, { t: 'state', v: 0.2, w: -0.1, driving: true, reason: 'driving' });
  check('19h Drive: ack จากเซิร์ฟเวอร์ → สถานะ Driving + แสดงคำสั่งปัจจุบัน', $('drive-status').textContent === 'Driving' && $('drive-readout').textContent === 'Commanded: 0.20 m/s · -0.10 rad/s', $('drive-readout').textContent);
  await driveReply(ds, { t: 'state', v: 0, w: 0, driving: false, reason: 'released' });
  check('19h Drive: หยุดแล้ว → กลับ Ready', $('drive-status').textContent === 'Ready — hold to move');

  // ปุ่ม STOP + ปุ่ม ■ บนแถบบน
  $('dp-fwd').dispatch('pointerdown', { pointerId: 30 }); await advance(250);
  ds.sent.length = 0; const stopPosts = () => posts('/api/drive/stop').length; const sp0 = stopPosts();
  $('drive-stop').click(); await advance(350);
  check('19h STOP: ส่ง stop ทางซ็อกเก็ต + POST /api/drive/stop (สำรอง) + ล้างปุ่มที่ค้าง (ไม่ส่งคำสั่งขับต่อ)', sentOf(ds, 'stop').length === 1 && stopPosts() === sp0 + 1 && sentOf(ds, 'cmd').filter((m) => m.lin !== 0).length === 0, JSON.stringify(ds.sent.map((m) => m.t)));
  check('19h ■ บนแถบบน: Admin + Mapping → กดได้ + ชื่อปุ่มบอกหน้าที่', $('stop').disabled === false && $('stop').attrs['aria-label'] === 'Stop moving now (Mapping and Manual mode)', $('stop').attrs['aria-label']);
  const sp1 = stopPosts(); ds.sent.length = 0; $('stop').click(); await advance(100);
  check('19h ■ บนแถบบน: กด → หยุดเหมือนปุ่ม STOP', stopPosts() === sp1 + 1 && sentOf(ds, 'stop').length === 1);

  // ซ็อกเก็ตหลุด / ถูกปฏิเสธ
  ds.close(); await flush();
  check('19h Drive: ซ็อกเก็ตหลุดตอนพร้อมอยู่ → บอกว่าหุ่นกำลังหยุด + ปุ่มใช้ไม่ได้', $('drive-status').textContent === 'Connection lost — the robot is stopping' && $('drive-stop').disabled === true);
  const n0 = dsocks(); await advance(1100);
  check('19h Drive: ต่อใหม่อัตโนมัติหลัง ~1 วินาที', dsocks() === n0 + 1 && $('drive-status').textContent === 'Connecting…' || $('drive-status').textContent === 'Connection lost — the robot is stopping', `${dsocks()} ${n0}`);
  ds = await driveOpen();
  check('19h Drive: ต่อใหม่สำเร็จ → Ready', $('drive-status').textContent === 'Ready — hold to move');
  await driveReply(ds, { t: 'denied', error: M('drive.err.in_use') });
  check('19h Drive: ถูกปฏิเสธเพราะอุปกรณ์อื่นขับอยู่ → ข้อความที่แปลแล้ว + ใช้ไม่ได้', $('drive-status').textContent === 'Another device is driving the robot' && $('drive-stop').disabled === true && hasClass($('drive-status'), 'bad'), $('drive-status').textContent);
  ds.close(); await flush(); const n1 = dsocks(); await advance(1100);
  check('19h Drive: ถูกปฏิเสธ → ไม่รีบต่อซ้ำ (รอ ~3 วินาที)', dsocks() === n1); await advance(2200);
  check('19h Drive: ครบ 3 วินาที → ลองต่อใหม่', dsocks() === n1 + 1, `${dsocks()} ${n1}`);
  ds = await driveOpen();

  // เงื่อนไขที่ต้องตัดการขับทันที (ส่ง stop ก่อนปิด)
  const closesWith = async (name, fn) => {
    const s = driveSock(); s.sent.length = 0; await fn(); await advance(100);
    check(`19h Safety: ${name} → ส่ง stop แล้วปิดซ็อกเก็ต`, s.readyState === 3 && sentOf(s, 'stop').length >= 1 && s.sent.slice(-1)[0].t === 'stop', JSON.stringify(s.sent.map((m) => m.t)) + ' ' + s.readyState);
  };
  const reopen = async () => { await advance(3500); if (driveSock().readyState !== 1) return driveOpen(); return driveSock(); };
  $('dp-fwd').dispatch('pointerdown', { pointerId: 31 }); await advance(250);
  await closesWith('ออกจากหน้า Mapping ไป Home ตอนกำลังกดค้าง', async () => { nav('#/home'); await flush(); });
  const nHome = dsocks(); await advance(5000);
  check('19h Safety: อยู่หน้าอื่น → ไม่เปิดซ็อกเก็ตขับ', dsocks() === nHome);
  nav('#/mapping'); await advance(100); ds = await driveOpen();
  $('dp-fwd').dispatch('pointerdown', { pointerId: 32 }); await advance(250);
  await closesWith('เปลี่ยนโหมดเป็น Standby', async () => { await send(modeOf('STANDBY')); });
  check('19h Safety: Standby → ไม่ขับ (สถานะบอกให้เริ่มโหมด)', $('drive-status').textContent === 'Start Mapping or Manual mode to drive the robot.', $('drive-status').textContent);
  await mapOn(); ds = await driveOpen();
  for (const [name, over] of [['Navigation (Nav2 คุม /cmd_vel)', modeOf('NAVIGATION')], ['มีระบบนอกแอป', modeOf('MAPPING', 'RUNNING', { external: ['bt_navigator'] })], ['มีของค้างจากรอบก่อน', modeOf('MAPPING', 'RUNNING', { leftover: { mode: 'MAPPING' } })],
    ['กำลังเปลี่ยนโหมด', modeOf('MAPPING', 'STOPPING')], ['เกิด error', modeOf('STANDBY', 'ERROR')]]) {
    await closesWith(name, async () => { await send(over); });
    const before = dsocks(); await advance(3000);
    check(`19h Safety: ${name} → ไม่เปิดซ็อกเก็ตขับ`, dsocks() === before);
    await mapOn(); ds = await driveOpen();
  }
  await closesWith('หมดสิทธิ์ผู้ดูแล', async () => { await asAdmin(false); });
  check('19h Safety: ไม่ใช่ผู้ดูแล → สถานะบอกให้ล็อกอิน + ■ ใช้ไม่ได้', $('drive-status').textContent === 'Sign in as admin to drive the robot.' && $('stop').disabled === true && $('stop').attrs['aria-label'] === 'Stop button (available in Mapping and Manual mode)');
  await asAdmin(true); await advance(500); ds = driveSock().readyState === 1 ? driveSock() : await driveOpen();
  await closesWith('สลับแท็บ/ซ่อนหน้าจอ (visibilitychange)', async () => { document.hidden = true; docListeners.visibilitychange.forEach((fn) => fn()); });
  document.hidden = false; docListeners.visibilitychange.forEach((fn) => fn()); await advance(200); ds = driveSock().readyState === 1 ? driveSock() : await driveOpen();
  ds.sent.length = 0; $('dp-fwd').dispatch('pointerdown', { pointerId: 33 }); await advance(250);
  (winListeners.blur || []).forEach((fn) => fn()); await advance(350);
  const stopAt = ds.sent.map((m) => m.t).lastIndexOf('stop');
  check('19h Safety: หน้าต่างเสียโฟกัส (blur) → ส่ง stop + ไม่ส่งคำสั่งขับต่อหลังจากนั้น', stopAt >= 0 && ds.sent.slice(stopAt + 1).every((m) => m.t !== 'cmd' || (m.lin === 0 && m.ang === 0)), JSON.stringify(ds.sent.slice(-4)));
  await closesWith('ปิดแท็บ (pagehide)', async () => { (winListeners.pagehide || []).forEach((fn) => fn()); });
  ws.onclose(); await flush();
  check('19h Safety: ขาดการเชื่อมต่อสถานะ (WebSocket หลัก) → ไม่ขับ + บอกให้เริ่มโหมด', hasClass($('drive-card'), 'off'));
  await advance(1100); ws.onopen(); await mapOn();

  // ── บันทึกแผนที่ ──
  await asAdmin(true); await mapOn(); await send({ ...modeOf('MAPPING'), map: { available: true, unsaved: true, saved_name: null } });
  const nameIn = $('map-name'), saveBtn = $('map-save');
  check('19h Save: ชื่อเริ่มต้น map_วันที่_เวลา ถูกเติมให้ + ปุ่มกดได้ + บอกว่ายังไม่บันทึก', /^map_\d{8}_\d{4}$/.test(nameIn.value) && saveBtn.disabled === false &&
    $('map-save-state').textContent === 'This map has not been saved yet.', `${nameIn.value} | ${$('map-save-state').textContent}`);
  for (const bad of ['', '../x', 'a b', '-abc', '_abc', 'a.b', 'x'.repeat(49), 'ก']) {
    nameIn.value = bad; nameIn.dispatch('input'); await flush();
    check(`19h Save: ชื่อ "${bad.slice(0, 12)}" ไม่ถูกต้อง → ปุ่มกดไม่ได้${bad ? ' + แจ้งข้อผิดพลาด' : ''}`, saveBtn.disabled === true && ($('map-name-err').hidden === (bad === '')) && (bad === '' || nameIn.attrs['aria-invalid'] === 'true'), $('map-name-err').textContent);
  }
  for (const ok of ['a', 'factory_1', 'Room-2', 'x'.repeat(48), '0map']) {
    nameIn.value = ok; nameIn.dispatch('input'); await flush();
    check(`19h Save: ชื่อ "${ok.slice(0, 12)}" ใช้ได้`, saveBtn.disabled === false && $('map-name-err').hidden === true);
  }
  nameIn.value = 'floor1'; nameIn.dispatch('input'); await flush();
  const sv0 = posts('/api/map/save').length;
  saveBtn.click(); await flush();
  check('19h Save: กด → POST {name} (ไม่มี overwrite) + ปุ่มเป็น Saving… และกดซ้ำไม่ได้ระหว่างบันทึก', posts('/api/map/save').length === sv0 + 1 && JSON.stringify(posts('/api/map/save')[sv0].body) === '{"name":"floor1"}' &&
    posts('/api/map/save')[sv0].headers['X-Requested-With'] === 'agv-web', JSON.stringify(posts('/api/map/save')[sv0].body));
  await flush();
  check('19h Save: สำเร็จ → toast + บอกว่าบันทึกแล้ว + บรรทัด Last saved', $('toast').textContent === 'Map saved as “floor1”' && $('map-save-state').textContent === 'Saved as “floor1”.' &&
    /^Last saved: “floor1” · \d\d:\d\d$/.test($('map-last-saved').textContent) && saveBtn.textContent === 'Save map', `${$('toast').textContent} | ${$('map-save-state').textContent} | ${$('map-last-saved').textContent}`);
  check('19h Save (M3): หลังบันทึกสำเร็จ ชื่อในช่องเปลี่ยนเป็นชื่อใหม่อัตโนมัติ (ไม่ชนชื่อที่เพิ่งใช้)', /^map_\d{8}_\d{4}$/.test(nameIn.value) && nameIn.value !== 'floor1', nameIn.value);
  check('19h Save (M1): หลังบันทึกสำเร็จ → บอกขั้นต่อไป (Navigation → เลือกแผนที่ในคลัง → เริ่ม)', $('map-next').hidden === false && $('map-next').textContent === I.DICT.en['save.next'] &&
    /Navigation tab/.test($('map-next').textContent), $('map-next').textContent);
  await send({ ...modeOf('MAPPING'), map: { available: true, unsaved: true, saved_name: 'floor1' } });
  check('19h Save (M1): แผนที่เปลี่ยนหลังบันทึก → ซ่อนขั้นต่อไป (ยังไม่ได้บันทึกรอบใหม่)', $('map-next').hidden === true);
  nameIn.value = 'floor1'; nameIn.dispatch('input'); await flush();
  check('19h Save: แผนที่เปลี่ยนหลังบันทึก → บอกว่า "แต่แผนที่เปลี่ยนไปแล้ว"', $('map-save-state').textContent === 'Saved as “floor1”, but the map has changed since.');
  // ชื่อซ้ำ → ถามก่อนทับ
  server.saveHandler = (b) => (b.overwrite ? { status: 200, data: { ok: true, name: b.name, bytes: 9, saved: NOW, path: '/x' } } : { status: 409, data: { ok: false, error: M('map.err.exists', { name: b.name }) } });
  saveBtn.click(); await flush(); await flush();
  check('19h Save: ชื่อซ้ำ (409) → หน้าต่างถามก่อนทับ ไม่ทับเอง', $('confirm-dialog').open && $('confirm-title').textContent === 'Replace the map “floor1”?' && $('confirm-ok').textContent === 'Replace' && posts('/api/map/save').length === sv0 + 2, $('confirm-title').textContent);
  $('confirm-cancel').click(); await flush();
  check('19h Save: ยกเลิกการทับ → ไม่ส่งซ้ำ', posts('/api/map/save').length === sv0 + 2);
  saveBtn.click(); await flush(); await flush(); $('confirm-ok').click(); await flush(); await flush();
  const last2 = posts('/api/map/save').slice(-1)[0];
  check('19h Save: ยืนยันการทับ → POST ซ้ำพร้อม overwrite:true', JSON.stringify(last2.body) === '{"name":"floor1","overwrite":true}', JSON.stringify(last2.body));
  server.saveHandler = () => ({ status: 500, data: { ok: false, error: M('map.err.save_failed', { detail: 'disk full' }) } });
  saveBtn.click(); await flush(); await flush();
  check('19h Save: ล้มเหลว → toast ข้อความที่แปลแล้ว + ปุ่มกลับมากดได้', $('toast').textContent === 'Could not save the map: disk full' && saveBtn.disabled === false, $('toast').textContent);
  server.saveHandler = () => ({ status: 500, data: {} }); saveBtn.click(); await flush(); await flush();
  check('19h Save: ล้มเหลวไม่มีข้อความ → ข้อความสำรอง', $('toast').textContent === 'Could not save the map', $('toast').textContent);
  server.saveHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } }); saveBtn.click(); await flush(); await flush();
  check('19h Save: 401 → ถอนสิทธิ์ผู้ดูแล + เปิด PIN', $('pin-dialog').open === true && !hasClass($('lock'), 'on')); $('pin-cancel').click(); await flush(); server.saveHandler = null;
  await asAdmin(true);
  nameIn.value = 'enterkey'; nameIn.dispatch('input'); await flush(); const sv1 = posts('/api/map/save').length;
  nameIn.dispatch('keydown', { key: 'Enter' }); await flush();
  check('19h Save: กด Enter ในช่องชื่อ = บันทึก', posts('/api/map/save').length === sv1 + 1);
  // ── M1: บันทึกและหยุด Mapping ในครั้งเดียว ──
  await send({ ...modeOf('MAPPING'), map: { available: true, unsaved: true, saved_name: null } });
  nameIn.value = 'floor_stop'; nameIn.dispatch('input'); await flush();
  const stopBtn = $('map-save-stop');
  check('19h SaveStop: ปุ่มกดได้เมื่อเงื่อนไขเดียวกับปุ่มบันทึก + ป้ายปุ่มถูกต้อง + กติกาชื่อแสดงตั้งแต่ต้น', stopBtn.disabled === false && stopBtn.textContent === 'Save and stop Mapping' &&
    $('map-name-rule').textContent === I.DICT.en['ui.save.rule'] && /up to 48/.test($('map-name-rule').textContent), stopBtn.textContent);
  nameIn.value = 'bad name'; nameIn.dispatch('input'); await flush();
  check('19h SaveStop: ชื่อไม่ถูกต้อง → ปุ่มกดไม่ได้เหมือนปุ่มบันทึก', stopBtn.disabled === true && saveBtn.disabled === true);
  nameIn.value = 'floor_stop'; nameIn.dispatch('input'); await flush();
  const sm0 = posts('/api/mode').length, ss0 = posts('/api/map/save').length;
  server.saveHandler = () => ({ status: 500, data: { ok: false, error: M('map.err.save_failed', { detail: 'disk full' }) } });
  stopBtn.click(); await flush(); await flush();
  check('19h SaveStop: บันทึกล้มเหลว → ไม่หยุด Mapping (ไม่ส่งคำสั่งเปลี่ยนโหมด) + แจ้งสาเหตุ', posts('/api/map/save').length === ss0 + 1 && posts('/api/mode').length === sm0 &&
    $('toast').textContent === 'Could not save the map: disk full' && stopBtn.disabled === false, $('toast').textContent);
  server.saveHandler = null;
  stopBtn.click(); await flush(); await flush();
  const modePosts = posts('/api/mode');
  check('19h SaveStop: บันทึกสำเร็จ → POST บันทึกก่อน แล้ว POST โหมด STANDBY (ลำดับถูก ไม่ถามซ้ำ)', posts('/api/map/save').length === ss0 + 2 && modePosts.length === sm0 + 1 &&
    modePosts[sm0].body.mode === 'STANDBY' && !$('confirm-dialog').open, JSON.stringify(modePosts.slice(sm0).map((c) => c.body)));
  await send({ ...modeOf('STANDBY'), map: { available: false, unsaved: false, saved_name: 'floor_stop' } });
  check('19h SaveStop: กลับ Standby แล้วปุ่มปิด + ยังบอกขั้นต่อไปให้ผู้ใช้เห็น', stopBtn.disabled === true && $('map-next').hidden === false);
  await mapOn({ map: { available: true, unsaved: true, saved_name: null } });
  check('19h SaveStop: เริ่ม Mapping รอบใหม่ → ซ่อนขั้นต่อไปของรอบเก่า', $('map-next').hidden === true);
  nameIn.value = 'floor_ow'; nameIn.dispatch('input'); await flush();
  server.saveHandler = (b) => (b.overwrite ? { status: 200, data: { ok: true, name: b.name, bytes: 9, saved: NOW, path: '/x' } } : { status: 409, data: { ok: false, error: M('map.err.exists', { name: b.name }) } });
  const sm1 = posts('/api/mode').length;
  stopBtn.click(); await flush(); await flush();
  check('19h SaveStop: ชื่อซ้ำ → ถามก่อนทับ และยังไม่หยุด Mapping จนกว่าจะบันทึกสำเร็จ', $('confirm-dialog').open === true && posts('/api/mode').length === sm1);
  $('confirm-ok').click(); await flush(); await flush();
  check('19h SaveStop: ยืนยันทับ → บันทึกแล้วหยุด Mapping (จำว่าผู้ใช้ตั้งใจ "บันทึกและหยุด")', posts('/api/mode').length === sm1 + 1 && posts('/api/mode')[sm1].body.mode === 'STANDBY');
  server.saveHandler = null;
  await mapOn({ map: { available: true, unsaved: true, saved_name: null } });

  // เงื่อนไขที่ปุ่มบันทึกต้องใช้ไม่ได้
  await send({ ...modeOf('MAPPING'), map: { available: false, unsaved: false, saved_name: null } });
  check('19h Save: ยังไม่มีแผนที่ → ปุ่มกดไม่ได้ + บอกว่ารอแผนที่แรก', saveBtn.disabled === true && $('map-save-state').textContent === 'Waiting for the first map…');
  await send({ ...modeOf('STANDBY'), map: { available: false, unsaved: false, saved_name: null } });
  check('19h Save: ไม่ได้รัน Mapping → ปุ่มกดไม่ได้ + บอกให้เริ่ม Mapping', saveBtn.disabled === true && $('map-save-state').textContent === 'Start Mapping mode to build a map first.');
  await mapOn({ map: { available: true, unsaved: true, saved_name: null } }); await asAdmin(false);
  check('19h Save: ไม่ใช่ผู้ดูแล → ปุ่ม/ช่องกรอกใช้ไม่ได้ + บอกให้ล็อกอิน', saveBtn.disabled === true && nameIn.disabled === true && $('map-save-state').textContent === 'Sign in as admin to save the map.');
  await asAdmin(true);
  server.saved = { status: 200, data: { maps: [], last: { hash: 'abc', name: 'prevmap', time: NOW } } };
  nav('#/home'); await flush(); nav('#/mapping'); await advance(100);
  check('19h Save: เข้าหน้า Mapping → โหลด "Last saved" จากเซิร์ฟเวอร์', /^Last saved: “prevmap” · \d\d:\d\d$/.test($('map-last-saved').textContent), $('map-last-saved').textContent);

  // ── เตือนเมื่อแผนที่ยังไม่ได้บันทึกแล้วจะออกจาก Mapping ──
  const unsavedText = 'The map you are building has not been saved — it will be lost.';
  const dialogItems = () => $('confirm-list').children.map((c) => c.textContent);
  await mapOn({ map: { available: true, unsaved: true, saved_name: null } }); nav('#/home'); await flush();
  seg('STANDBY').click(); await flush();
  check('19h Unsaved: ปิด Mapping (Standby) ทั้งที่ยังไม่บันทึก → alert เตือนว่าแผนที่จะหาย', $('confirm-dialog').open && $('confirm-dialog').attrs.role === 'alertdialog' && dialogItems().includes(unsavedText), dialogItems().join('|'));
  $('confirm-cancel').click(); await flush();
  seg('NAVIGATION').click(); await flush();
  check('19h Unsaved: สลับไปโหมดอื่น → เตือนเหมือนกัน', dialogItems().includes(unsavedText)); $('confirm-cancel').click(); await flush();
  $('mode-reset').click(); await flush();
  check('19h Unsaved: Reset all → เตือนเป็นข้อแรก + ข้อเดิมครบ', dialogItems()[0] === unsavedText && dialogItems().length === 4, dialogItems().join('|')); $('confirm-cancel').click(); await flush();
  $('reset-top').click(); await flush();
  check('19h Unsaved: ปุ่ม ↻ แถบบนเตือนเหมือนกัน', dialogItems()[0] === unsavedText); $('confirm-cancel').click(); await flush();
  await send({ ...modeOf('MAPPING'), map: { available: true, unsaved: false, saved_name: 'floor1' } });
  seg('STANDBY').click(); await flush();
  check('19h Unsaved: บันทึกแล้ว → ไม่มีคำเตือนเรื่องแผนที่ (หน้าต่างเดิมปกติ)', $('confirm-dialog').open && !dialogItems().includes(unsavedText) && $('confirm-dialog').attrs.role === 'dialog', dialogItems().join('|')); $('confirm-cancel').click(); await flush();
  await send({ ...modeOf('NAVIGATION'), map: { available: true, unsaved: true, saved_name: null } });
  seg('STANDBY').click(); await flush();
  check('19h Unsaved: ไม่ได้อยู่โหมด Mapping → ไม่เตือน (แม้สถานะเก่าบอกว่ายังไม่บันทึก)', !dialogItems().includes(unsavedText)); $('confirm-cancel').click(); await flush();
  await send({ ...modeOf('MAPPING'), map: { available: false, unsaved: false, saved_name: null } });
  seg('STANDBY').click(); await flush();
  check('19h Unsaved: ยังไม่มีแผนที่เลย → ไม่เตือน', !dialogItems().includes(unsavedText)); $('confirm-cancel').click(); await flush();

  // ภาษา
  await mapOn(); ds = driveSock().readyState === 1 ? driveSock() : await driveOpen();
  langBtn('th').click(); await flush();
  check('19h TH: Display/Drive/Save แปลทันที', $('disp-colors').children[0].attrs['aria-label'] === 'แดง' && $('drive-status').textContent === 'พร้อม — แตะค้างเพื่อเคลื่อนที่' && $('drive-stop').textContent === 'หยุด' &&
    $('map-save').textContent === 'บันทึกแผนที่' && $('disp-size-out').textContent === '3 พิกเซล' && $('stop').attrs['aria-label'] === 'หยุดเคลื่อนที่ทันที (โหมด Mapping และ Manual)', `${$('drive-status').textContent} | ${$('stop').attrs['aria-label']}`);
  langBtn('en').click(); await flush();
  check('19h EN: ไม่มีตัวอักษรไทยหลุดในหน้า Mapping ใหม่ทั้งหมด', thaiLeaks().length === 0, thaiLeaks().join(' | '));
  await send(modeOf('STANDBY')); nav('#/home'); await flush(); server.map = null;

  // ═════════ 20) EN ล้วน: ไม่มีตัวอักษรไทยหลุดในหน้า ไม่ว่าสถานะไหน ═════════
  const leakScenarios = [
    ['idle', {}], ['moving', { activity: { state: 'moving', level: 'ok', text: M('act.moving'), detail: null } }],
    ['starting', { mode: { state: 'STARTING', target: 'MAPPING', message: M('mode.starting', { mode: 'MAPPING' }) } }],
    ['stopping', { mode: { mode: 'NAVIGATION', state: 'STOPPING', message: M('mode.stopping', { mode: 'NAVIGATION' }) } }],
    ['external', { mode: { external: ['ekf_filter_node'], managed: false } }],
    ['error', { mode: { state: 'ERROR', message: M('mode.err.exited', { mode: 'MANUAL', exit_code: 1, log: '/x.log' }) } }],
    ['not ready', { ...twoItems, activity: { state: 'not_ready', level: 'warn', text: M('act.not_ready'), detail: M('ready.loc.none.hint') } }],
    ['sensors bad', { readiness: { ready: false, headline: M('ready.headline.not_ready'), hint: M('ready.sensors.hint'), items: [
      { key: 'sensors', level: 'bad', text: M('ready.sensors.bad', { missing: ['LiDAR', 'EKF'] }), hint: M('ready.sensors.hint') }] } }],
    ['standby', { mode: { mode: 'STANDBY' }, activity: { state: 'standby', level: 'off', text: M('act.standby'), detail: M('act.standby.detail') } }],
  ];
  for (const [name, over] of leakScenarios) {
    await send(over);
    check(`20 EN ไม่มีตัวอักษรไทยหลุด [${name}]`, thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));
  }
  ws.close(); await flush();
  check('20 EN ไม่มีตัวอักษรไทยหลุด [disconnected]', thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));
  await advance(1000); ws.onopen(); await send();
  server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: true } };
  await advance(15000);
  $('lock').click(); await flush();
  server.login.push({ status: 401, data: { ok: false, error: M('auth.bad_pin') } });
  $('pin-input').value = 'x'; $('pin-form').dispatch('submit'); await flush();
  check('20 EN ไม่มีตัวอักษรไทยหลุด [PIN dialog + error]', $('pin-error').textContent === 'Incorrect PIN' && thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));
  $('pin-cancel').click(); await flush();

  // ═════════ 19i) หน้า Navigation: คลังแผนที่ (รายการ · เลือกใช้ · เปลี่ยนชื่อ · ลบ) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    let maps = [
      { name: 'test1', saved: NOW - 600, bytes: 50000, size_m: { w: 10.0, h: 12.5 } },
      { name: 'floor1', saved: NOW - 2 * 86400, bytes: 51000, size_m: { w: 11.5, h: 11.0 } },
    ];
    let selState = makeSel();
    const push = () => {
      server.saved = { status: 200, data: { maps: maps.map((m) => ({ ...m })), last: { hash: null, name: null, time: null } } };
      server.selected = { status: 200, data: JSON.parse(JSON.stringify(selState)) };
    };
    push();
    server.selectHandler = (b) => {
      selState = b.name === null ? makeSel()
        : makeSel({ name: b.name, available: true, choice: { source: 'selected', name: b.name, file: `/m/${b.name}.yaml` } });
      push();
      return { status: 200, data: Object.assign({}, selState, { ok: true }) };
    };
    server.renameHandler = (b) => { maps.find((m) => m.name === b.name).name = b.new_name; push(); return { status: 200, data: { ok: true, name: b.new_name } }; };
    server.deleteHandler = (b) => { maps = maps.filter((m) => m.name !== b.name); push(); return { status: 200, data: { ok: true, name: b.name } }; };

    const tiles = () => $('maplib-list').children;
    const tileOf = (name) => tiles().find((tl) => tl.children[1].textContent === name);
    const actionsOf = (tl) => tl.children.find((c) => hasClass(c, 'lib-actions'));
    const btnOf = (name, label) => { const tl = tileOf(name); const a = tl && actionsOf(tl); return a ? a.children.find((b) => b.textContent === label) : undefined; };
    const reasonOf = (tl) => { const r = tl.children.find((c) => hasClass(c, 'lib-reason')); return r ? r.textContent : null; };
    const reasonsOf = (tl) => tl.children.filter((c) => hasClass(c, 'lib-reason')).map((c) => c.textContent);
    const badgesOf = (tl) => { const b = tl.children.find((c) => hasClass(c, 'lib-badges')); return b ? b.children.map((c) => c.textContent) : []; };
    const savedCalls = () => server.calls.filter((c) => c.path === '/api/map/saved').length;
    const names = () => tiles().map((tl) => tl.children[1].textContent).join(',');

    langBtn('en').click(); await flush();
    await send(modeOf('STANDBY'));
    await asAdmin(false);
    nav('#/home'); await advance(100);
    nav('#/nav'); await settle();

    // ── ผู้ใช้ทั่วไป ──
    check('19i Library: ดึงทั้งรายการและแผนที่ที่เลือกเมื่อเข้าหน้า', savedCalls() >= 1 && server.calls.some((c) => c.path === '/api/map/selected'));
    check('19i Library: แสดงทุกแผนที่ + จำนวน', names() === 'test1,floor1' && $('maplib-count').textContent === '2 maps' && $('maplib-state').hidden === true, names());
    const t1 = tileOf('test1');
    const img = t1.children[0].children[0];
    check('19i Library: ภาพย่อ src ถูกต้อง (มีเวอร์ชันตามเวลาบันทึก) + alt', img.src === `/api/map/saved/test1.png?v=${NOW - 600}` && img.attrs.alt === 'Preview of map test1', `${img.src} | ${img.attrs.alt}`);
    check('19i Library: แสดงวันที่ + ขนาดพื้นที่', /10\.0 × 12\.5 m$/.test(t1.children.find((c) => hasClass(c, 'lib-meta')).textContent), t1.children.map((c) => c.textContent).join('|'));
    check('19i Library: ผู้ใช้ทั่วไปไม่มีปุ่มสั่ง + มีข้อความให้ล็อกอิน', tiles().every((tl) => !actionsOf(tl)) &&
      $('maplib-note').hidden === false && $('maplib-note').textContent === 'Sign in as admin to choose, rename or delete maps.');
    check('19i Status: ยังไม่ได้เลือก → ใช้แผนที่เริ่มต้น + ไม่มีบรรทัดกำลังรัน', $('nav-map-current').textContent === '○ No map selected — Navigation uses its default map.' && $('nav-map-running').hidden === true, $('nav-map-current').textContent);

    // ── Admin: ปุ่มต่างๆ ──
    await asAdmin(true); await settle();
    check('19i Admin: ทุกแผนที่มีปุ่ม Use / Rename / Delete ใช้ได้ + ข้อความล็อกอินหาย',
      ['test1', 'floor1'].every((n) => ['Use for Navigation', 'Rename', 'Delete'].every((lb) => btnOf(n, lb) && btnOf(n, lb).disabled === false)) && $('maplib-note').hidden === true);
    check('19i Admin: ปุ่มมี aria-label ที่ระบุชื่อแผนที่', btnOf('test1', 'Rename').attrs['aria-label'] === 'Rename map test1' &&
      btnOf('test1', 'Delete').attrs['aria-label'] === 'Delete map test1' && btnOf('floor1', 'Use for Navigation').attrs['aria-label'] === 'Use map floor1 for Navigation');

    // ── เลือกใช้ ──
    const s0 = posts('/api/map/select').length;
    btnOf('floor1', 'Use for Navigation').click(); await settle();
    const sel1 = posts('/api/map/select')[s0];
    check('19i Select: กด → POST {name} พร้อมเฮดเดอร์ CSRF', posts('/api/map/select').length === s0 + 1 && JSON.stringify(sel1.body) === '{"name":"floor1"}' && sel1.headers['X-Requested-With'] === 'agv-web', JSON.stringify(sel1 && sel1.body));
    check('19i Select: toast + โหลดใหม่ → การ์ดแผนที่ที่เลือก + บรรทัดสถานะ', $('toast').textContent === 'Navigation will use “floor1”' &&
      $('nav-map-current').textContent === '✔ Selected: “floor1”' && hasClass(tileOf('floor1'), 'selected') && !hasClass(tileOf('test1'), 'selected'), $('toast').textContent);
    check('19i Select: ป้าย Selected + ปุ่มเปลี่ยนเป็น Clear selection', badgesOf(tileOf('floor1')).join('|') === '✔ Selected' && btnOf('floor1', 'Clear selection') && !btnOf('floor1', 'Use for Navigation'), badgesOf(tileOf('floor1')).join('|'));
    check('19i Select: แผนที่ที่เลือก → Rename/Delete ปิดไว้ + บอกเหตุผล', btnOf('floor1', 'Rename').disabled === true && btnOf('floor1', 'Delete').disabled === true &&
      reasonOf(tileOf('floor1')) === 'Selected for Navigation — choose another map or clear the selection first.' && reasonOf(tileOf('test1')) === null);
    check('19i Select: แผนที่อื่นยังเปลี่ยนชื่อ/ลบได้', btnOf('test1', 'Rename').disabled === false && btnOf('test1', 'Delete').disabled === false);

    // ── ยกเลิกการเลือก ──
    btnOf('floor1', 'Clear selection').click(); await settle();
    check('19i Clear: POST {name:null} + toast + กลับเป็น default', JSON.stringify(posts('/api/map/select').slice(-1)[0].body) === '{"name":null}' && $('toast').textContent === 'Map selection cleared' &&
      $('nav-map-current').textContent.startsWith('○ No map selected') && btnOf('floor1', 'Rename').disabled === false);

    // ── เปลี่ยนชื่อ ──
    const rn = $('rename-input'), rnSubmit = $('rename-submit');
    btnOf('floor1', 'Rename').click(); await settle();
    check('19i Rename: เปิดหน้าต่างเล็ก + ชื่อเดิมในช่อง + บอกชื่อปัจจุบัน + ปุ่มยังกดไม่ได้ (ยังไม่ได้เปลี่ยน)', $('rename-dialog').open === true && rn.value === 'floor1' &&
      $('rename-from').textContent === 'Current name: “floor1”' && rnSubmit.disabled === true && $('rename-error').textContent === '');
    for (const bad of ['a b', '../x', '-abc', 'x'.repeat(49)]) {
      rn.value = bad; rn.dispatch('input');
      check(`19i Rename: ชื่อ "${bad.slice(0, 10)}" ไม่ถูกต้อง → ปุ่มกดไม่ได้ + แจ้งรูปแบบ`, rnSubmit.disabled === true && $('rename-error').textContent === E['save.invalid'] && rn.attrs['aria-invalid'] === 'true');
    }
    rn.value = 'test1'; rn.dispatch('input');
    check('19i Rename: ชื่อซ้ำกับแผนที่อื่น → ปุ่มกดไม่ได้ + แจ้งว่ามีอยู่แล้ว', rnSubmit.disabled === true && $('rename-error').textContent === 'A map named “test1” already exists');
    rn.value = ''; rn.dispatch('input');
    check('19i Rename: ช่องว่าง → ปุ่มกดไม่ได้ (ไม่ฟ้องรูปแบบ)', rnSubmit.disabled === true && $('rename-error').textContent === '');
    rn.value = '  hall  '; rn.dispatch('input');
    check('19i Rename: ชื่อใหม่ถูกต้อง (ตัดช่องว่างหัวท้าย) → ปุ่มกดได้ ไม่มีข้อผิดพลาด', rnSubmit.disabled === false && $('rename-error').textContent === '' && rn.attrs['aria-invalid'] === 'false');
    // ข้อผิดพลาดจากเซิร์ฟเวอร์ → แสดงในหน้าต่างเดิม ไม่ปิด
    const savedRename = server.renameHandler;
    server.renameHandler = () => ({ status: 409, data: { ok: false, error: M('map.err.exists', { name: 'hall' }) } });
    const r0 = posts('/api/map/rename').length;
    $('rename-form').dispatch('submit'); await settle();
    check('19i Rename: เซิร์ฟเวอร์ปฏิเสธ (409) → หน้าต่างยังเปิด + แสดงเหตุผล + ไม่ toast สำเร็จ', posts('/api/map/rename').length === r0 + 1 && $('rename-dialog').open === true &&
      $('rename-error').textContent === 'A map named “hall” already exists' && $('toast').textContent !== 'Renamed to “hall”', $('rename-error').textContent);
    server.renameHandler = savedRename;
    $('rename-form').dispatch('submit'); await settle();
    const rnReq = posts('/api/map/rename').slice(-1)[0];
    check('19i Rename: ส่ง POST {name,new_name} (ตัดช่องว่าง) → ปิดหน้าต่าง + toast + รายการใหม่', JSON.stringify(rnReq.body) === '{"name":"floor1","new_name":"hall"}' && $('rename-dialog').open === false &&
      $('toast').textContent === 'Renamed to “hall”' && names() === 'test1,hall', `${JSON.stringify(rnReq.body)} | ${names()}`);
    const r1 = posts('/api/map/rename').length;
    btnOf('hall', 'Rename').click(); await settle();
    $('rename-form').dispatch('submit'); await settle();          // ยังไม่ได้เปลี่ยนชื่อ → ส่งไม่ได้
    check('19i Rename: กด Enter ทั้งที่ชื่อไม่เปลี่ยน → ไม่ส่งคำสั่ง', posts('/api/map/rename').length === r1 && $('rename-dialog').open === true);
    $('rename-cancel').click(); await settle();
    check('19i Rename: Cancel → ปิดหน้าต่าง ไม่ส่งคำสั่ง', $('rename-dialog').open === false && posts('/api/map/rename').length === r1);
    $('rename-form').dispatch('submit'); await settle();
    check('19i Rename: หน้าต่างปิดแล้ว submit ซ้ำ → ไม่ส่งคำสั่ง', posts('/api/map/rename').length === r1);

    // ── ลบ ──
    const d0 = posts('/api/map/delete').length;
    btnOf('test1', 'Delete').click(); await settle();
    check('19i Delete: ถามยืนยันก่อน (หน้าต่างเตือน) ยังไม่ส่งคำสั่ง', $('confirm-dialog').open === true && $('confirm-title').textContent === 'Delete the map “test1”?' &&
      $('confirm-msg').textContent === 'It will be removed from the library. An administrator can still recover the files.' && $('confirm-ok').textContent === 'Delete' &&
      $('confirm-dialog').attrs.role === 'alertdialog' && posts('/api/map/delete').length === d0, $('confirm-title').textContent);
    $('confirm-cancel').click(); await settle();
    check('19i Delete: ยกเลิก → ไม่ส่งคำสั่ง ยังมีแผนที่', posts('/api/map/delete').length === d0 && names() === 'test1,hall');
    btnOf('test1', 'Delete').click(); await settle();
    $('confirm-ok').click(); await settle();
    check('19i Delete: ยืนยัน → POST {name} + toast + แผนที่หายจากรายการ + นับใหม่', JSON.stringify(posts('/api/map/delete')[d0].body) === '{"name":"test1"}' &&
      $('toast').textContent === 'Map “test1” deleted' && names() === 'hall' && $('maplib-count').textContent === '1 map', `${names()} | ${$('maplib-count').textContent}`);

    // ── Navigation กำลังรัน ──
    btnOf('hall', 'Use for Navigation').click(); await settle();
    selState = makeSel({ name: 'hall', available: true, choice: { source: 'selected', name: 'hall', file: '/m/hall.yaml' },
      in_use: { source: 'selected', name: 'hall', file: '/m/hall.yaml' }, restart_needed: false });
    maps.push({ name: 'lab', saved: NOW - 100, bytes: 1, size_m: null }); push();
    await send(modeOf('NAVIGATION')); await advance(5000); await settle();
    check('19i NavRunning: บอกว่ากำลังรันด้วยแผนที่ที่เลือก (ไม่ต้อง restart)', $('nav-map-running').hidden === false && $('nav-map-running').textContent === '✔ Navigation is running with “hall”.', $('nav-map-running').textContent);
    check('19i NavRunning: ป้าย Selected + In use now ที่แผนที่เดียวกัน', badgesOf(tileOf('hall')).join('|') === '✔ Selected|✔ In use now', badgesOf(tileOf('hall')).join('|'));
    check('19i NavRunning: Rename/Delete ปิดทุกแผนที่ + บอกเหตุผล แต่ Use for Navigation ยังกดได้',
      btnOf('lab', 'Rename').disabled === true && btnOf('lab', 'Delete').disabled === true && reasonOf(tileOf('lab')) === 'Not available while Navigation is running.' &&
      btnOf('lab', 'Use for Navigation').disabled === false && reasonOf(tileOf('hall')) === 'Selected for Navigation — choose another map or clear the selection first.');
    check('19i NavRunning: แผนที่ไม่มีขนาด (size_m=null) → แสดงเฉพาะวันที่ ไม่พัง', !/ m$/.test(tileOf('lab').children.find((c) => hasClass(c, 'lib-meta')).textContent));
    btnOf('lab', 'Use for Navigation').click(); await settle();
    selState = makeSel({ name: 'lab', available: true, choice: { source: 'selected', name: 'lab', file: '/m/lab.yaml' },
      in_use: { source: 'selected', name: 'hall', file: '/m/hall.yaml' }, restart_needed: true }); push();
    await advance(5000); await settle();
    check('19i NavRunning: เลือกอันใหม่ระหว่างรัน → เตือนว่ามีผลตอนเปิดครั้งถัดไป', $('nav-map-running').textContent ===
      '▲ Navigation is running with “hall”. Your choice applies the next time Navigation starts.' && $('nav-map-current').textContent === '✔ Selected: “lab”', $('nav-map-running').textContent);
    selState = makeSel({ in_use: { source: 'default', name: null, file: null }, restart_needed: false }); push();
    await advance(5000); await settle();
    check('19i NavRunning: รันด้วยแผนที่จากการตั้งค่า → ข้อความกลาง ไม่ระบุชื่อ', $('nav-map-running').textContent === '✔ Navigation is running with the map from the robot’s settings.', $('nav-map-running').textContent);
    await send(modeOf('STANDBY')); await advance(5000); await settle();
    check('19i NavRunning: Nav หยุด → ปุ่มเปลี่ยนชื่อ/ลบกลับมาใช้ได้', btnOf('lab', 'Rename').disabled === false && btnOf('lab', 'Delete').disabled === false && reasonOf(tileOf('lab')) === null);

    await send(modeOf('STANDBY', 'RUNNING', { target: 'NAVIGATION' })); await settle();
    check('19i NavRunning: กำลังเปิด Navigation (target=NAVIGATION) ก็ถือว่ารันอยู่ → ปิดปุ่มแก้ไฟล์', btnOf('lab', 'Rename').disabled === true && btnOf('lab', 'Delete').disabled === true &&
      reasonsOf(tileOf('lab')).includes('Not available while Navigation is running.'));
    await send(modeOf('STANDBY')); await settle();

    // ── สถานะแผนที่ที่เลือกแบบพิเศษ ──
    selState = makeSel({ name: 'ghost', available: false }); push(); await advance(5000); await settle();
    check('19i Status: แผนที่ที่เลือกไว้ไฟล์หาย → เตือน ▲', $('nav-map-current').textContent === '▲ “ghost” is selected but its files are missing — Navigation will use another map.', $('nav-map-current').textContent);
    selState = makeSel({ choice: { source: 'config', name: null, file: '/x.yaml' } }); push(); await advance(5000); await settle();
    check('19i Status: ใช้แผนที่จากการตั้งค่า → บอกตรงๆ', $('nav-map-current').textContent === '○ No map selected — Navigation uses the map set in the robot’s settings.', $('nav-map-current').textContent);

    // ── ไม่วาดใหม่ถ้าข้อมูลเดิม (ภาพย่อไม่กะพริบ) · วาดใหม่เมื่อเปลี่ยน ──
    selState = makeSel(); push(); await advance(5000); await settle();
    const keep = tiles()[0];
    await advance(5000); await settle();
    check('19i Poll: ข้อมูลเท่าเดิม → ไม่สร้างการ์ดใหม่', tiles()[0] === keep);
    maps.push({ name: 'new1', saved: NOW, bytes: 1, size_m: null }); push(); await advance(5000); await settle();
    check('19i Poll: มีแผนที่ใหม่ (เช่นเพิ่ง Save จาก Mapping) → โผล่ในรายการเอง', names().includes('new1') && tiles()[0] !== keep, names());

    // ── ความถี่การดึงข้อมูล ──
    let c0 = savedCalls();
    await advance(5000); await settle();
    check('19i Poll: ทุก 5 วินาทีขณะอยู่หน้า Navigation', savedCalls() === c0 + 1, savedCalls() - c0);
    document.hidden = true; c0 = savedCalls();
    await advance(10000); await settle();
    check('19i Poll: แท็บถูกซ่อน → ไม่ดึง', savedCalls() === c0);
    document.hidden = false;
    nav('#/settings'); await settle(); c0 = savedCalls();
    await advance(10000); await settle();
    check('19i Poll: ออกจากหน้า Navigation → ไม่ดึง', savedCalls() === c0);
    nav('#/nav'); await settle();
    check('19i Poll: กลับเข้าหน้า → ดึงทันที', savedCalls() === c0 + 1);

    // ── ความปลอดภัยของ UI ──
    const evil = '<img src=x onerror=alert(1)>';
    maps.push({ name: evil, saved: NOW, bytes: 1, size_m: null }); push(); await advance(5000); await settle();
    const et = tileOf(evil);
    check('19i XSS: ชื่อแผนที่แปลก ๆ แสดงเป็นข้อความล้วน + URL ภาพถูก encode', !!et && et.children[1].textContent === evil &&
      et.children[0].children[0].src.startsWith('/api/map/saved/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E.png?v='), et && et.children[0].children[0].src);
    maps = maps.filter((m) => m.name !== evil); push();

    // ── ข้อผิดพลาดจากเซิร์ฟเวอร์ / เซสชันหมด ──
    const keepSel = server.selectHandler;
    server.selectHandler = () => ({ status: 404, data: { ok: false, error: M('map.err.not_found', { name: 'lab' }) } });
    btnOf('lab', 'Use for Navigation').click(); await settle();
    check('19i Error: เซิร์ฟเวอร์ปฏิเสธ → toast เป็นข้อความที่แปลแล้ว ไม่พัง', $('toast').textContent === 'Map “lab” was not found', $('toast').textContent);
    server.selectHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    btnOf('lab', 'Use for Navigation').click(); await settle();
    check('19i Error: เซสชันหมด (401) → ขอ PIN ใหม่ + ปุ่มสั่งหายไป', $('pin-dialog').open === true && $('toast').textContent === E['toast.session_expired'] && tiles().every((tl) => !actionsOf(tl)), $('toast').textContent);
    $('pin-cancel').click(); server.selectHandler = keepSel;
    await asAdmin(true); await settle();
    server.saved = { status: 500, data: {} }; await advance(5000); await settle();
    check('19i Error: โหลดรายการไม่สำเร็จ → บอกว่ากำลังลองใหม่ (ไม่ใช่ "ไม่มีแผนที่") และยังโชว์ของเดิม', $('maplib-state').textContent === 'Could not load the map list — retrying.' && tiles().length > 0 && $('maplib-goto').hidden === true);
    push(); await advance(5000); await settle();
    check('19i Error: ดึงได้อีกครั้ง → ข้อความหายไป', $('maplib-state').hidden === true);

    // ═════ 19j) เริ่ม / เริ่มใหม่ Navigation ด้วยแผนที่ที่เลือก จากการ์ดแผนที่ ═════
    const postsLog = () => server.calls.filter((c) => c.method === 'POST').map((c) => `${c.path}${c.body ? ' ' + JSON.stringify(c.body) : ''}`);
    const modePosts = (from) => postsLog().slice(from).filter((s) => s.startsWith('/api/mode ')).map((s) => JSON.parse(s.slice(10)).mode);
    const dlgItems = () => $('confirm-list').children.map((li) => li.textContent);
    const POSE = 'Afterwards, tell Navigation where the robot is on the map (initial pose) — it waits until you do.';
    maps = [{ name: 'test1', saved: NOW - 600, bytes: 1, size_m: { w: 10.0, h: 12.5 } }, { name: 'lab', saved: NOW - 100, bytes: 1, size_m: null }];
    selState = makeSel(); push();
    await asAdmin(true); await send(modeOf('STANDBY')); await advance(5000); await settle();

    check('19j Start: Standby → ปุ่ม Start Navigation (ปุ่มหลัก) ใช้ได้ทุกแผนที่ และยังมี Use for Navigation (ปุ่มรอง)',
      ['test1', 'lab'].every((n) => btnOf(n, 'Start Navigation') && btnOf(n, 'Start Navigation').disabled === false && btnOf(n, 'Use for Navigation')) &&
      hasClass(btnOf('test1', 'Start Navigation'), 'wide') && !hasClass(btnOf('test1', 'Start Navigation'), 'ghost') && hasClass(btnOf('test1', 'Use for Navigation'), 'ghost') &&
      btnOf('test1', 'Start Navigation').attrs['aria-label'] === 'Start Navigation with map test1');
    const q0 = postsLog().length;
    btnOf('test1', 'Start Navigation').click(); await settle();
    check('19j Start: ถามยืนยันก่อน (หน้าต่างปกติ ไม่ใช่เตือน) + บอกให้ตั้งตำแหน่งเริ่มต้น + ยังไม่ส่งคำสั่งใดๆ',
      $('confirm-dialog').open === true && $('confirm-title').textContent === 'Start Navigation with “test1”?' &&
      $('confirm-msg').textContent === 'Navigation will load this map and start. The robot does not move until you send it somewhere.' &&
      $('confirm-ok').textContent === 'Start' && $('confirm-dialog').attrs.role === 'dialog' && eq(dlgItems(), [POSE]) && postsLog().length === q0, JSON.stringify(dlgItems()));
    $('confirm-cancel').click(); await settle();
    check('19j Start: ยกเลิก → ไม่ส่งอะไรเลย', postsLog().length === q0);
    btnOf('test1', 'Start Navigation').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19j Start: ยืนยัน → เลือกแผนที่ก่อน แล้วสั่งโหมด NAVIGATION (ตามลำดับ) + toast กำลังเปลี่ยนโหมด',
      eq(postsLog().slice(q0), ['/api/map/select {"name":"test1"}', '/api/mode {"mode":"NAVIGATION"}']) && $('toast').textContent === E['toast.changing'], JSON.stringify(postsLog().slice(q0)));
    check('19j Start: เสร็จแล้วปุ่มกลับมากดได้ (ไม่ค้างสถานะยุ่ง)', btnOf('lab', 'Start Navigation').disabled === false && btnOf('test1', 'Start Navigation').disabled === false);
    const q1 = postsLog().length;
    btnOf('test1', 'Start Navigation').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19j Start: แผนที่ถูกเลือกอยู่แล้ว → ไม่ส่ง select ซ้ำ สั่งโหมดอย่างเดียว', eq(postsLog().slice(q1), ['/api/mode {"mode":"NAVIGATION"}']), JSON.stringify(postsLog().slice(q1)));

    // ── สลับมาจากโหมดอื่น ──
    await send({ ...modeOf('MAPPING'), map: { available: true, unsaved: true, saved_name: null } }); await settle();
    btnOf('lab', 'Start Navigation').click(); await settle();
    check('19j Start: มาจาก Mapping ที่ยังไม่บันทึก → หน้าต่างเตือน + เตือนแผนที่หาย + บอกว่าจะปิดโหมดเดิมก่อน',
      $('confirm-dialog').className === 'dlg-warn' && eq(dlgItems(), [E['confirm.unsaved'], 'The current mode (Mapping) will be stopped first.', POSE]), JSON.stringify(dlgItems()));
    $('confirm-cancel').click(); await settle();
    await send({ ...modeOf('MAPPING'), map: { available: true, unsaved: false, saved_name: 'x' } }); await settle();
    btnOf('lab', 'Start Navigation').click(); await settle();
    check('19j Start: มาจาก Mapping ที่บันทึกแล้ว → ไม่มีคำเตือนแผนที่หาย', $('confirm-dialog').className === '' && eq(dlgItems(), ['The current mode (Mapping) will be stopped first.', POSE]), `${$('confirm-dialog').className} | ${JSON.stringify(dlgItems())}`);
    const q2 = postsLog().length;
    $('confirm-ok').click(); await settle();
    check('19j Start: สลับจากโหมดอื่นสั่งครั้งเดียว (ไม่ต้อง STANDBY ก่อน — backend ปิดโหมดเดิมให้)', eq(modePosts(q2), ['NAVIGATION']), JSON.stringify(modePosts(q2)));
    await send(modeOf('STANDBY')); await settle();

    // ── ล้มเหลวแต่ละจุด ──
    selState = makeSel(); push(); await advance(5000); await settle();      // ไม่มีแผนที่ที่ถูกเลือก → ขั้น select ต้องทำงานจริง
    const keepSelect = server.selectHandler;
    server.selectHandler = () => ({ status: 404, data: { ok: false, error: M('map.err.not_found', { name: 'lab' }) } });
    const q3 = postsLog().length;
    btnOf('lab', 'Start Navigation').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19j Start: เลือกแผนที่ไม่สำเร็จ → หยุดตรงนั้น ไม่สั่งโหมด + toast เหตุผล + ปุ่มกลับมากดได้',
      modePosts(q3).length === 0 && $('toast').textContent === 'Map “lab” was not found' && btnOf('lab', 'Start Navigation').disabled === false, $('toast').textContent);
    server.selectHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    const q4 = postsLog().length;
    btnOf('lab', 'Start Navigation').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19j Start: เซสชันหมดตอนเลือก (401) → ขอ PIN ใหม่ ไม่สั่งโหมด', $('pin-dialog').open === true && modePosts(q4).length === 0 && $('toast').textContent === E['toast.session_expired']);
    $('pin-cancel').click(); server.selectHandler = keepSelect; await asAdmin(true); await settle();
    server.mode = { status: 409, data: { ok: false, error: M('mode.err.busy') } };
    const q5 = postsLog().length;
    btnOf('lab', 'Start Navigation').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19j Start: backend ปฏิเสธการเปลี่ยนโหมด → toast เหตุผล + ปุ่มกลับมากดได้', eq(modePosts(q5), ['NAVIGATION']) && $('toast').textContent === E['mode.err.busy'] && btnOf('lab', 'Start Navigation').disabled === false, $('toast').textContent);
    server.mode = null;

    // ── Nav รันอยู่ → เริ่มใหม่ ──
    selState = makeSel({ name: 'test1', available: true, choice: { source: 'selected', name: 'test1', file: '/m/test1.yaml' },
      in_use: { source: 'selected', name: 'test1', file: '/m/test1.yaml' }, restart_needed: false }); push();
    await send(modeOf('NAVIGATION')); await advance(5000); await settle();
    check('19j Restart: รันด้วยแผนที่นี้อยู่แล้ว → ไม่มีปุ่มเริ่ม/เริ่มใหม่ ; แผนที่อื่นเป็น Restart Navigation',
      !btnOf('test1', 'Start Navigation') && !btnOf('test1', 'Restart Navigation') && btnOf('lab', 'Restart Navigation') && btnOf('lab', 'Restart Navigation').disabled === false &&
      !btnOf('lab', 'Start Navigation') && btnOf('lab', 'Restart Navigation').attrs['aria-label'] === 'Restart Navigation with map lab');
    const q6 = postsLog().length;
    btnOf('lab', 'Restart Navigation').click(); await settle();
    check('19j Restart: หน้าต่างยืนยันบอกว่าจะปิดแล้วเปิดใหม่', $('confirm-title').textContent === 'Restart Navigation with “lab”?' && $('confirm-ok').textContent === 'Restart' &&
      eq(dlgItems(), ['Navigation will be stopped and started again with this map.', POSE]), JSON.stringify(dlgItems()));
    $('confirm-ok').click(); await settle(); await advance(1500);
    check('19j Restart: เลือกแผนที่ → สั่ง STANDBY → "รอ" ยังไม่สั่ง NAVIGATION จนกว่าระบบจะหยุดจริง + ปุ่มปิดระหว่างรอ',
      eq(postsLog().slice(q6), ['/api/map/select {"name":"lab"}', '/api/mode {"mode":"STANDBY"}']) && btnOf('lab', 'Restart Navigation').disabled === true, JSON.stringify(postsLog().slice(q6)));
    check('19j Restart: ระหว่างรอ → toast บอกว่ากำลังเริ่มใหม่ (ไม่ถูก toast อื่นทับ)', $('toast').textContent === 'Restarting Navigation…', $('toast').textContent);
    await send(modeOf('STANDBY', 'STOPPING', { target: null })); await advance(1000); await settle();
    check('19j Restart: ระบบยังอยู่ระหว่างปิด (STOPPING) → ยังไม่สั่ง NAVIGATION', eq(modePosts(q6), ['STANDBY']));
    await send(modeOf('STANDBY', 'RUNNING', { target: 'STANDBY' })); await advance(1000); await settle();
    check('19j Restart: เป็น STANDBY แต่ยังมี target ค้าง (ยังเปลี่ยนโหมดไม่จบ) → ยังไม่สั่ง NAVIGATION', eq(modePosts(q6), ['STANDBY']));
    await send(modeOf('STANDBY')); await advance(600); await settle();
    check('19j Restart: ระบบหยุดแล้ว (STANDBY/RUNNING) → สั่ง NAVIGATION + ปุ่มกลับมา', eq(modePosts(q6), ['STANDBY', 'NAVIGATION']) && btnOf('lab', 'Start Navigation') && btnOf('lab', 'Start Navigation').disabled === false, JSON.stringify(modePosts(q6)));
    // ระบบหยุดไม่ทัน
    selState = makeSel({ name: 'test1', available: true, choice: { source: 'selected', name: 'test1', file: '/m/test1.yaml' },
      in_use: { source: 'selected', name: 'test1', file: '/m/test1.yaml' } }); push();
    await send(modeOf('NAVIGATION')); await advance(5000); await settle();
    const q7 = postsLog().length;
    btnOf('lab', 'Restart Navigation').click(); await settle(); $('confirm-ok').click(); await settle();
    await advance(89000); await settle();
    check('19j Restart: ยังไม่ครบเวลา → ยังรอ ไม่สั่ง NAVIGATION', eq(modePosts(q7), ['STANDBY']) && btnOf('lab', 'Restart Navigation').disabled === true);
    await advance(2000); await settle();
    check('19j Restart: หมดเวลา → toast บอก ไม่สั่ง NAVIGATION + ปุ่มกลับมา', eq(modePosts(q7), ['STANDBY']) && $('toast').textContent === E['toast.lib_restart_timeout'] && btnOf('lab', 'Restart Navigation').disabled === false, $('toast').textContent);
    // ระบบ ERROR ระหว่างรอ
    const q8 = postsLog().length;
    btnOf('lab', 'Restart Navigation').click(); await settle(); $('confirm-ok').click(); await settle(); await advance(1000);
    await send(modeOf('STANDBY', 'ERROR', { message: M('mode.err.timeout', { mode: 'NAVIGATION', seconds: 40, log: '/l.log' }) })); await advance(700); await settle();
    check('19j Restart: ระบบ ERROR ระหว่างรอ → ไม่สั่ง NAVIGATION + บอกว่าเปลี่ยนโหมดไม่สำเร็จ (ไม่ใช่ "หมดเวลา")',
      eq(modePosts(q8), ['STANDBY']) && $('toast').textContent === E['toast.mode_failed'] && btnOf('lab', 'Start Navigation') !== undefined, $('toast').textContent);
    await send(modeOf('STANDBY')); await advance(5000); await settle();

    // ── ปุ่ม Start ที่ปิดไว้พร้อมเหตุผล ──
    selState = makeSel(); push(); await advance(5000); await settle();
    await send(modeOf('STANDBY', 'STARTING', { target: 'NAVIGATION' })); await settle();
    check('19j Disabled: ระบบกำลังเปลี่ยนโหมด → ปุ่ม Start ปิด + บอกเหตุผล (และเหตุผลของ Rename/Delete ต่อท้าย)',
      btnOf('lab', 'Start Navigation').disabled === true && eq(reasonsOf(tileOf('lab')), ['The system is changing mode — wait a moment.', 'Not available while Navigation is running.']), JSON.stringify(reasonsOf(tileOf('lab'))));
    await send(modeOf('STANDBY', 'RUNNING', { external: ['ekf_node'] })); await settle();
    check('19j Disabled: มีระบบหุ่นรันนอกแอป → ปุ่ม Start ปิด + บอกให้ Reset all ก่อน', btnOf('lab', 'Start Navigation').disabled === true &&
      eq(reasonsOf(tileOf('lab')), ['Other robot software is running outside this app — use Reset all on Home first.']), JSON.stringify(reasonsOf(tileOf('lab'))));
    await send(modeOf('STANDBY', 'RUNNING', { external: [], leftover: { mode: 'MAPPING' } })); await settle();
    check('19j Disabled: มีของค้างจากรอบก่อน (leftover) → ปุ่ม Start ปิดเช่นกัน', btnOf('lab', 'Start Navigation').disabled === true);
    await send(modeOf('STANDBY', 'RUNNING', { leftover: null })); await settle();
    check('19j Disabled: กลับมาปกติ → ปุ่ม Start กดได้อีกครั้ง + เหตุผลหาย', btnOf('lab', 'Start Navigation').disabled === false && reasonsOf(tileOf('lab')).length === 0);
    await asAdmin(false); await settle();
    check('19j Guest: ผู้ใช้ทั่วไปไม่เห็นปุ่ม Start/Restart', tiles().every((tl) => !actionsOf(tl)));
    await asAdmin(true); await settle();

    // ── ภาษาไทย ──
    langBtn('th').click(); await flush();
    check('19j TH: ปุ่ม + aria เป็นไทย', btnOf('lab', 'เริ่ม Navigation') && btnOf('lab', 'เริ่ม Navigation').attrs['aria-label'] === 'เริ่ม Navigation ด้วยแผนที่ lab');
    btnOf('lab', 'เริ่ม Navigation').click(); await settle();
    check('19j TH: หน้าต่างยืนยันเป็นไทย', $('confirm-title').textContent === 'เริ่ม Navigation ด้วยแผนที่ “lab” ?' && $('confirm-ok').textContent === 'เริ่ม' &&
      dlgItems().length === 1 && dlgItems()[0] === T['confirm.lib_start.pose'], $('confirm-title').textContent);
    $('confirm-cancel').click(); await settle();
    langBtn('en').click(); await flush();

    // ── รายการว่าง ──
    maps = []; push(); await advance(5000); await settle();
    check('19i Empty: ไม่มีแผนที่ → บอกวิธีสร้าง + ปุ่มไปหน้า Mapping + จำนวน 0', $('maplib-state').textContent === 'No saved maps yet. Build one in Mapping mode, then press Save map.' &&
      $('maplib-goto').hidden === false && /id="maplib-goto"[^>]*href="#\/mapping"/.test(html) && tiles().length === 0 && $('maplib-count').textContent === '0 maps');

    server.saved = { status: 500, data: {} }; await advance(5000); await settle();
    check('19i Error: รายการว่างอยู่แล้วโหลดไม่ได้ → บอกว่าโหลดไม่สำเร็จ ไม่ใช่ "ยังไม่มีแผนที่"', $('maplib-state').textContent === 'Could not load the map list — retrying.' && $('maplib-goto').hidden === true, $('maplib-state').textContent);
    push(); await advance(5000); await settle();

    // ── ไม่ได้ตั้ง PIN ──
    maps = [{ name: 'test1', saved: NOW - 600, bytes: 1, size_m: null }]; push();
    server.session = { status: 200, data: { ok: true, admin: false, auth_enabled: false } }; await advance(15000); await settle();
    check('19i NoPIN: ไม่ได้ตั้ง PIN → ไม่มีปุ่มสั่ง + บอกเหตุผลเดียวกับหน้าบันทึกแผนที่', !actionsOf(tileOf('test1')) && $('maplib-note').textContent === E['drive.st.no_pin'], $('maplib-note').textContent);

    // ── สลับภาษา ──
    maps = [{ name: 'test1', saved: NOW - 600, bytes: 1, size_m: { w: 10.0, h: 12.5 } }, { name: 'lab', saved: NOW - 100, bytes: 1, size_m: null }];
    selState = makeSel({ name: 'lab', available: true, choice: { source: 'selected', name: 'lab', file: '/m/lab.yaml' } }); push();
    await asAdmin(true); await advance(5000); await settle();
    btnOf('test1', 'Rename').click(); await settle();
    langBtn('th').click(); await flush();
    check('19i TH: การ์ด/ปุ่ม/จำนวน/สถานะ/aria เป็นไทย', $('maplib-count').textContent === '2 แผนที่' && btnOf('test1', 'ใช้กับ Navigation') && btnOf('test1', 'เปลี่ยนชื่อ') && btnOf('test1', 'ลบ') &&
      btnOf('lab', 'ยกเลิกการเลือก') && $('nav-map-current').textContent === '✔ เลือกไว้: “lab”' && btnOf('test1', 'เปลี่ยนชื่อ').attrs['aria-label'] === 'เปลี่ยนชื่อแผนที่ test1' &&
      badgesOf(tileOf('lab')).join('|') === '✔ เลือกไว้' && tileOf('test1').children[0].children[0].attrs.alt === 'ภาพย่อของแผนที่ test1', $('maplib-count').textContent);
    check('19i TH: หน้าต่างเปลี่ยนชื่อที่เปิดอยู่เปลี่ยนภาษาตาม + ค่าที่กรอกไม่หาย', $('rename-from').textContent === 'ชื่อปัจจุบัน: “test1”' && $('rename-title').textContent === T['ui.lib.rename.title'] && $('rename-input').value === 'test1');
    rn.value = 'a b'; rn.dispatch('input');
    check('19i TH: ข้อความผิดรูปแบบเป็นไทย', $('rename-error').textContent === T['save.invalid']);
    $('rename-cancel').click(); await flush();
    btnOf('lab', 'ลบ').click(); await settle();
    check('19i TH: ยืนยันการลบเป็นไทย', $('confirm-title').textContent === 'ลบแผนที่ “lab” ?' || btnOf('lab', 'ลบ').disabled === true, $('confirm-title').textContent);
    if ($('confirm-dialog').open) { $('confirm-cancel').click(); await flush(); }
    langBtn('en').click(); await flush();
    check('19i TH→EN: กลับเป็นอังกฤษครบ', $('maplib-count').textContent === '2 maps' && btnOf('test1', 'Rename') && $('nav-map-current').textContent === '✔ Selected: “lab”');

    // ── เก็บกวาด ──
    server.saved = null; server.selected = null; server.selectHandler = null; server.renameHandler = null; server.deleteHandler = null;
    await asAdmin(false);                                     // คืนสถานะเริ่มต้น (ส่วนถัดไปคาดว่าไม่ใช่ผู้ดูแล)
    nav('#/home'); await advance(100);
  }

  // ═════════ 19k) หน้า Navigation: แผงตั้งค่า Nav2 (ค่าที่ใช้อยู่ vs ที่บันทึก · ใช้เลย · บันทึกไว้ใช้ครั้งหน้า) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    let saved = {};                      // ส่วนต่างที่บันทึกไว้บนเซิร์ฟเวอร์ปลอม
    let liveV = null;                    // ค่าที่ Nav2 "ใช้อยู่จริง" (null = Nav ไม่รัน)
    const effective = () => Object.assign({}, NAVP_DEFAULTS, saved);
    const info = () => makeNavInfo(saved, { in_use: liveV ? { ...liveV } : null,
      restart_needed: !!liveV && JSON.stringify(liveV) !== JSON.stringify(effective()) });
    const push = () => {
      server.navInfo = { status: 200, data: info() };
      server.navLive = liveV ? { status: 200, data: { running: true, values: { ...liveV }, mixed: [] } }
        : { status: 200, data: { running: false, values: null, mixed: [] } };
    };
    server.navSave = (b) => {
      for (const [k, v] of Object.entries(b.values)) { if (v === NAVP_DEFAULTS[k]) delete saved[k]; else saved[k] = v; }
      push();
      return { status: 200, data: { ok: true, params: info() } };
    };
    const okApply = (b) => {                                   // ตัวแทน backend: ค่าที่เพิ่มความเร็ว/ปิดตรวจชนต้อง confirmed:true
      const changes = Object.fromEntries(Object.entries(b.values).filter(([k, v]) => v !== liveV[k]));
      const risky = Object.keys(changes).filter((k) => (k === 'desired_linear_vel' && changes[k] > liveV[k]) ||
        (k === 'use_collision_detection' && changes[k] === false)).sort();
      if (risky.length && b.confirmed !== true) {
        return { status: 409, data: { ok: false, error: M('nav.err.confirm_required', { keys: risky.join(', ') }) } };
      }
      const applied = {};
      for (const [k, v] of Object.entries(changes)) { applied[k] = { from: liveV[k], to: v }; liveV[k] = v; }
      push();
      return { status: 200, data: { ok: true, applied, live: { ...liveV }, mixed: [], params: info() } };
    };
    server.navApply = okApply;

    const groupsEl = () => $('navp-groups').children;
    const rowsAll = () => groupsEl().flatMap((g) => g.children.slice(1));
    const nrow = (key) => {
      const row = rowsAll().find((r) => r.children[0].children[0].attrs['for'] === `navp-in-${key}`);
      if (!row) return null;
      const edit = row.children[4], first = edit.children[0];
      const isText = first.tag === 'input';
      return { row, name: row.children[0].children[0], changed: row.children[0].children[1], desc: row.children[1], def: row.children[2],
        run: row.children[3], input: isText ? first : first.children[0], err: row.children[5],
        reset: edit.children[edit.children.length - 1], stateText: isText ? null : first.children[1] };
    };
    const typeIn = async (key, text) => { const r = nrow(key); r.input.value = text; r.input.dispatch('input'); await flush(); };
    const toggle = async (key, on) => { const r = nrow(key); r.input.checked = on; r.input.dispatch('change'); await flush(); };
    const posts2 = (path) => server.calls.filter((c) => c.method === 'POST' && c.path === path);
    const liveCalls = () => server.calls.filter((c) => c.path === '/api/nav/params/live').length;
    const shown = (key) => nrow(key).input.value;
    const A = () => $('navp-apply'), S = () => $('navp-save'), R = () => $('navp-restore');
    const dlgItems = () => $('confirm-list').children.map((li) => li.textContent);

    langBtn('en').click(); await flush();
    push();
    await send(modeOf('STANDBY'));
    await asAdmin(true);
    nav('#/home'); await advance(100);
    nav('#/nav'); await settle();

    // ── Nav ไม่รัน (Admin): แสดงค่าที่บันทึกไว้ ──
    check('19k Panel: 5 กลุ่ม 13 แถว ชื่อกลุ่ม/ชื่อค่าแปลแล้ว', groupsEl().map((g) => g.children[0].textContent).join('|') === 'Speed|Path following|Obstacles|Goal accuracy|Clearance' &&
      rowsAll().length === 13 && nrow('desired_linear_vel').name.textContent === 'Cruise speed' && nrow('desired_linear_vel').desc.textContent === E['navp.p.desired_linear_vel.desc'], groupsEl().map((g) => g.children[0].textContent).join('|'));
    check('19k Panel: บรรทัด Default + ช่องแสดงค่าที่บันทึก + ไม่มีบรรทัดค่าที่ใช้อยู่', nrow('desired_linear_vel').def.textContent === 'Default: 0.2 m/s' &&
      nrow('use_collision_detection').def.textContent === 'Default: On' && shown('desired_linear_vel') === '0.2' && nrow('use_collision_detection').stateText.textContent === 'On' &&
      rowsAll().every((r) => r.children[3].hidden === true));
    check('19k Panel: Nav ไม่รัน → คำอธิบาย "ใช้ครั้งหน้า" + ทุกอย่างเป็นค่า Default + ปุ่มทั้งสามกดไม่ได้', $('navp-hint').textContent === E['ui.navp.hint.idle'] &&
      $('navp-summary').textContent === 'All defaults' && A().disabled === true && S().disabled === true && R().disabled === true);

    // ── กลุ่มพับเก็บได้ ──
    const gOpen = () => groupsEl().map((g) => g.open);
    const gTitle = (i) => groupsEl()[i].children[0].children[0].textContent;
    const gBadge = (i) => groupsEl()[i].children[0].children[1];
    check('19k Groups: ตอนแรกเปิดเฉพาะ Speed ที่เหลือพับ + ไม่มีป้าย', eq(gOpen(), [true, false, false, false, false]) &&
      [0, 1, 2, 3, 4].every((i) => gBadge(i).hidden === true && gBadge(i).textContent === ''), JSON.stringify(gOpen()));
    await typeIn('xy_goal_tolerance', '0.3');
    check('19k Groups: แก้ค่าในกลุ่มที่พับอยู่ → ป้าย "1 changed" ที่หัวกลุ่ม + ชื่อกลุ่มไม่ถูกปน + กลุ่มยังพับ',
      gBadge(3).hidden === false && gBadge(3).textContent === '1 changed' && gTitle(3) === 'Goal accuracy' && gOpen()[3] === false, gBadge(3).textContent);
    await typeIn('yaw_goal_tolerance', 'abc');
    check('19k Groups: พิมพ์ค่าผิดในกลุ่มที่พับอยู่ → เปิดกลุ่มนั้นเอง (error ต้องไม่ซ่อน)', gOpen()[3] === true && nrow('yaw_goal_tolerance').err.hidden === false);
    await typeIn('yaw_goal_tolerance', '0.1');
    groupsEl()[3].open = false; groupsEl()[3].dispatch('toggle');                       // ผู้ใช้พับกลับเอง
    groupsEl()[1].open = true; groupsEl()[1].dispatch('toggle');                        // ผู้ใช้เปิด Path following
    langBtn('th').click(); await flush();
    check('19k Groups: สลับภาษา (สร้างแถวใหม่) → จำกลุ่มที่เปิด/พับตามที่ผู้ใช้ตั้ง + ป้ายเป็นไทย', eq(gOpen(), [true, true, false, false, false]) &&
      gTitle(1) === 'การตามเส้นทาง' && gBadge(3).textContent === '1 แก้ไขแล้ว', `${JSON.stringify(gOpen())} ${gBadge(3).textContent}`);
    langBtn('en').click(); await flush();
    await typeIn('xy_goal_tolerance', '0.15');
    check('19k Groups: ค่ากลับเท่า Default → ป้ายหาย', gBadge(3).hidden === true && gBadge(3).textContent === '');

    // ── แก้ค่า (ยังไม่รัน) ──
    await typeIn('desired_linear_vel', '0.25');
    check('19k Edit: แก้ค่า → ป้าย Changed + ปุ่ม Save/Restore เปิด + สรุปมีข้อความ "ยังไม่ได้ใช้หรือบันทึก" แต่ Apply ยังปิด (Nav ไม่รัน)',
      nrow('desired_linear_vel').changed.hidden === false && nrow('desired_linear_vel').reset.hidden === false && S().disabled === false && R().disabled === false &&
      A().disabled === true && $('navp-summary').textContent === 'All defaults · Edits not applied or saved yet', $('navp-summary').textContent);
    for (const bad of ['abc', '', '9', '0.01', '1e9']) {
      await typeIn('desired_linear_vel', bad);
      check(`19k Edit: "${bad}" ไม่ถูกต้อง → แจ้งช่วงค่า + Save ปิด + aria-invalid`, nrow('desired_linear_vel').err.hidden === false &&
        nrow('desired_linear_vel').err.textContent === 'Enter a number from 0.05 to 0.5' && S().disabled === true && nrow('desired_linear_vel').input.attrs['aria-invalid'] === 'true');
    }
    await typeIn('desired_linear_vel', '0.05'); check('19k Edit: ขอบล่างของช่วงใช้ได้', nrow('desired_linear_vel').err.hidden === true && S().disabled === false);
    await typeIn('desired_linear_vel', '0.5'); check('19k Edit: ขอบบนของช่วงใช้ได้', nrow('desired_linear_vel').err.hidden === true && S().disabled === false);
    nrow('desired_linear_vel').reset.click(); await flush();
    check('19k Edit: ↺ คืนค่า Default ของแถวนั้น', shown('desired_linear_vel') === '0.2' && nrow('desired_linear_vel').changed.hidden === true && S().disabled === true);
    await typeIn('xy_goal_tolerance', '0.3'); await toggle('use_collision_detection', false);
    check('19k Edit: ช่อง bool แสดง On/Off ตามที่สลับ', nrow('use_collision_detection').stateText.textContent === 'Off' && nrow('use_collision_detection').changed.hidden === false);
    R().click(); await flush();
    check('19k Edit: Restore all defaults → ทุกช่องกลับเป็น Default + ปุ่ม Restore/Save ปิด', shown('xy_goal_tolerance') === '0.15' && nrow('use_collision_detection').input.checked === true &&
      R().disabled === true && S().disabled === true);

    // ── Save ──
    await typeIn('desired_linear_vel', '0.25');
    const sv0 = posts2('/api/nav/params').length;
    S().click(); await settle();
    check('19k Save: POST เฉพาะค่าที่ต่างจากที่บันทึกไว้ + CSRF + toast + สรุปอัปเดต + Save ปิดอีกครั้ง',
      posts2('/api/nav/params').length === sv0 + 1 && JSON.stringify(posts2('/api/nav/params')[sv0].body) === '{"values":{"desired_linear_vel":0.25}}' &&
      posts2('/api/nav/params')[sv0].headers['X-Requested-With'] === 'agv-web' && $('toast').textContent === E['toast.navp_saved'] &&
      $('navp-summary').textContent === '1 changed from default' && S().disabled === true && nrow('desired_linear_vel').changed.hidden === false, $('toast').textContent);
    await toggle('use_collision_detection', false);
    S().click(); await settle();
    check('19k Save: ค่า bool ส่งเป็น boolean จริง และค่าที่บันทึกแล้วไม่ถูกส่งซ้ำ', JSON.stringify(posts2('/api/nav/params').slice(-1)[0].body) === '{"values":{"use_collision_detection":false}}');
    const keepSave = server.navSave;
    server.navSave = () => ({ status: 400, data: { ok: false, error: M('nav.err.range', { key: 'desired_linear_vel', min: 0.05, max: 0.5 }) } });
    await typeIn('yaw_goal_tolerance', '0.2'); S().click(); await settle();
    check('19k Save: เซิร์ฟเวอร์ปฏิเสธ → toast เหตุผล + ค่าที่กรอกยังอยู่ + Save กดซ้ำได้', $('toast').textContent === 'desired_linear_vel must be between 0.05 and 0.5' &&
      shown('yaw_goal_tolerance') === '0.2' && S().disabled === false, $('toast').textContent);
    server.navSave = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    S().click(); await settle();
    check('19k Save: เซสชันหมด (401) → ขอ PIN ใหม่', $('pin-dialog').open === true && $('toast').textContent === E['toast.session_expired']);
    $('pin-cancel').click(); server.navSave = keepSave; await asAdmin(true); await settle();
    nrow('yaw_goal_tolerance').reset.click(); await flush();

    // ── ผู้ใช้ทั่วไป ──
    await asAdmin(false); await settle();
    check('19k Guest: ช่องกรอกทุกช่องปิด + ข้อความให้ล็อกอิน + ปุ่ม/↺ ใช้ไม่ได้', rowsAll().every((r) => { const e = r.children[4]; const i = e.children[0].tag === 'input' ? e.children[0] : e.children[0].children[0]; return i.disabled === true; }) &&
      $('navp-note').hidden === false && $('navp-note').textContent === E['ui.navp.need_admin'] && S().disabled === true && R().disabled === true && nrow('desired_linear_vel').reset.hidden === true);
    await asAdmin(true); await settle();

    // ── Navigation รันอยู่: ค่าที่ใช้อยู่จริง ──
    saved = {}; liveV = { ...NAVP_DEFAULTS }; push();
    await send(modeOf('NAVIGATION')); await settle();
    check('19k Running: ช่องเริ่มจากค่าที่ใช้อยู่ + บรรทัด "ใช้อยู่ตอนนี้" + คำอธิบายโหมดรัน + Apply ยังปิด (ยังไม่ได้แก้)',
      rowsAll().every((r) => r.children[3].hidden === false) && nrow('desired_linear_vel').run.textContent === 'Running now: 0.2 m/s' &&
      nrow('use_collision_detection').run.textContent === 'Running now: On' && $('navp-hint').textContent === E['ui.navp.hint.running'] && A().disabled === true);
    saved = { desired_linear_vel: 0.25 }; push(); await advance(5000); await settle();
    check('19k Running: ที่ใช้อยู่ ≠ ที่บันทึก → ช่องยังแสดงค่าที่ใช้อยู่ + บอกค่าที่บันทึก + แจ้งว่าไม่ตรงกัน',
      shown('desired_linear_vel') === '0.2' && nrow('desired_linear_vel').run.textContent === 'Running now: 0.2 m/s · saved: 0.25 m/s' &&
      $('navp-note').hidden === false && $('navp-note').textContent === E['ui.navp.diff'], nrow('desired_linear_vel').run.textContent);
    saved = {}; push(); await advance(5000); await settle();
    check('19k Running: ตรงกันแล้ว → ข้อความไม่ตรงกันหาย', $('navp-note').hidden === true);

    // ── ใช้เลย (Apply) ──
    await typeIn('xy_goal_tolerance', '0.2');
    check('19k Apply: แก้ค่า → Apply เปิด (เทียบกับค่าที่ใช้อยู่) และ Save เปิด (เทียบกับที่บันทึก)', A().disabled === false && S().disabled === false);
    const ap0 = posts2('/api/nav/params/apply').length;
    A().click(); A().click(); await settle();
    const ap = posts2('/api/nav/params/apply');
    check('19k Apply: คลิกซ้ำเร็วๆ → ส่งคำสั่งเดียว ; ส่งเฉพาะค่าที่ต่างจากที่ใช้อยู่ ไม่มี confirmed ; CSRF ; toast',
      ap.length === ap0 + 1 && JSON.stringify(ap[ap0].body) === '{"values":{"xy_goal_tolerance":0.2}}' && ap[ap0].headers['X-Requested-With'] === 'agv-web' &&
      $('toast').textContent === 'Applied 1 setting(s) to the running robot', `${ap.length - ap0} ${JSON.stringify(ap[ap0] && ap[ap0].body)} ${$('toast').textContent}`);
    check('19k Apply: ผลสำเร็จ → ค่าที่ใช้อยู่อัปเดต + แสดงค่าที่บันทึกต่าง + Apply ปิด + Save ยังเปิด (ยังไม่ได้บันทึก)',
      nrow('xy_goal_tolerance').run.textContent === 'Running now: 0.2 m · saved: 0.15 m' && A().disabled === true && S().disabled === false &&
      $('navp-note').textContent === E['ui.navp.diff'], nrow('xy_goal_tolerance').run.textContent);
    S().click(); await settle();
    check('19k Apply→Save: บันทึกค่าที่ใช้อยู่ → ส่วนต่างของบรรทัด/ข้อความไม่ตรงกันหาย', nrow('xy_goal_tolerance').run.textContent === 'Running now: 0.2 m' && $('navp-note').hidden === true &&
      JSON.stringify(posts2('/api/nav/params').slice(-1)[0].body) === '{"values":{"xy_goal_tolerance":0.2}}', nrow('xy_goal_tolerance').run.textContent);

    // ── ค่าเสี่ยง: เซิร์ฟเวอร์ขอยืนยัน → ถามผู้ใช้ → ส่งซ้ำพร้อม confirmed ──
    await typeIn('desired_linear_vel', '0.3');
    const ar0 = posts2('/api/nav/params/apply').length;
    A().click(); await settle();
    check('19k Risky: เซิร์ฟเวอร์ตอบ 409 → หน้าต่างเตือน (warn) แสดง "จาก → เป็น" + คำเตือนความปลอดภัย + ยังไม่ส่งซ้ำ',
      posts2('/api/nav/params/apply').length === ar0 + 1 && posts2('/api/nav/params/apply')[ar0].body.confirmed === undefined && $('confirm-dialog').open === true &&
      $('confirm-dialog').className === 'dlg-warn' && $('confirm-title').textContent === E['confirm.navp_risky.title'] && $('confirm-ok').textContent === 'Apply' &&
      eq(dlgItems(), ['Cruise speed: 0.2 m/s → 0.3 m/s', E['confirm.navp_risky.safety']]), JSON.stringify(dlgItems()));
    $('confirm-cancel').click(); await settle();
    check('19k Risky: ยกเลิก → ไม่ส่งซ้ำ + ค่าที่ใช้อยู่ไม่เปลี่ยน + ที่กรอกยังอยู่และ Apply ยังกดได้', posts2('/api/nav/params/apply').length === ar0 + 1 && liveV.desired_linear_vel === 0.2 &&
      shown('desired_linear_vel') === '0.3' && A().disabled === false);
    A().click(); await settle(); $('confirm-ok').click(); await settle();
    check('19k Risky: ยืนยัน → ส่งซ้ำพร้อม confirmed:true → ใช้จริง', posts2('/api/nav/params/apply').length === ar0 + 3 && posts2('/api/nav/params/apply')[ar0 + 2].body.confirmed === true &&
      liveV.desired_linear_vel === 0.3 && nrow('desired_linear_vel').run.textContent.startsWith('Running now: 0.3 m/s') && $('toast').textContent === 'Applied 1 setting(s) to the running robot');
    await typeIn('desired_linear_vel', '0.4'); await toggle('use_collision_detection', false);
    A().click(); await settle();
    check('19k Risky: หลายค่า → แสดงทุกค่าที่เสี่ยง (เป็นชื่อที่แปลแล้ว) ตามที่เซิร์ฟเวอร์ระบุ', eq(dlgItems(), ['Cruise speed: 0.3 m/s → 0.4 m/s', 'Collision check: On → Off', E['confirm.navp_risky.safety']]), JSON.stringify(dlgItems()));
    $('confirm-ok').click(); await settle();
    check('19k Risky: ยืนยันแล้วทั้งสองค่าถูกใช้', liveV.desired_linear_vel === 0.4 && liveV.use_collision_detection === false);

    // เซิร์ฟเวอร์ยังตอบ "ต้องยืนยัน" ทั้งที่ส่ง confirmed แล้ว → ต้องไม่วนถามซ้ำไม่รู้จบ
    server.navApply = () => ({ status: 409, data: { ok: false, error: M('nav.err.confirm_required', { keys: 'desired_linear_vel' }) } });
    await typeIn('desired_linear_vel', '0.45');
    const lp0 = posts2('/api/nav/params/apply').length;
    A().click(); await settle(); $('confirm-ok').click(); await settle();
    check('19k Risky: เซิร์ฟเวอร์ยังขอยืนยันหลังส่ง confirmed แล้ว → ไม่วนถามซ้ำ (ถามครั้งเดียว ส่ง 2 ครั้ง แล้วแจ้งข้อผิดพลาด)', posts2('/api/nav/params/apply').length === lp0 + 2 &&
      $('confirm-dialog').open === false && $('toast').textContent === 'These changes increase risk and need confirmation: desired_linear_vel', $('toast').textContent);
    server.navApply = okApply;
    await typeIn('desired_linear_vel', '0.4');

    // ── ล้มเหลวแต่ละแบบ ──
    const keepApply = server.navApply;
    await typeIn('xy_goal_tolerance', '0.25');
    server.navApply = () => ({ status: 409, data: { ok: false, error: M('nav.err.rejected', { key: 'xy_goal_tolerance', reason: 'not allowed' }) } });
    A().click(); await settle();
    check('19k Apply error: Nav2 ปฏิเสธ → toast เหตุผล + ที่กรอกยังอยู่ + Apply กดซ้ำได้ (ไม่ค้างสถานะยุ่ง)', $('toast').textContent === 'Nav2 rejected xy_goal_tolerance: not allowed' &&
      shown('xy_goal_tolerance') === '0.25' && A().disabled === false, $('toast').textContent);
    server.navApply = () => ({ status: 503, data: { ok: false, error: M('nav.err.unreachable', { node: '/controller_server' }) } });
    A().click(); await settle();
    check('19k Apply error: ติดต่อ Nav2 ไม่ได้ (503) → toast ระบุ node', $('toast').textContent === 'Cannot reach Nav2 (/controller_server) — is Navigation fully started?', $('toast').textContent);
    server.navApply = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    A().click(); await settle();
    check('19k Apply error: เซสชันหมด → ขอ PIN ใหม่', $('pin-dialog').open === true);
    $('pin-cancel').click(); server.navApply = keepApply; await asAdmin(true); await settle();
    nrow('xy_goal_tolerance').reset.click(); await flush();
    await typeIn('xy_goal_tolerance', '0.2'); await settle();

    // ── ถามค่าที่ใช้อยู่ไม่ได้ ──
    server.navLive = { status: 503, data: { ok: false, error: M('nav.err.unreachable', { node: '/controller_server' }) } };
    await advance(5000); await settle();
    check('19k LiveFail: Nav รันอยู่แต่อ่านค่าสดไม่ได้ → แจ้ง + Apply ปิด (กันส่งค่าโดยไม่รู้ว่าตอนนี้เป็นเท่าไร) + บรรทัดค่าที่ใช้อยู่ซ่อน',
      $('navp-note').textContent === E['ui.navp.live_failed'] && A().disabled === true && rowsAll().every((r) => r.children[3].hidden === true), $('navp-note').textContent);
    push(); await advance(5000); await settle();
    check('19k LiveFail: อ่านได้อีกครั้ง → แจ้งเตือนหาย', $('navp-note').textContent !== E['ui.navp.live_failed']);

    // ── ค่าที่กำลังแก้ ไม่ถูกทับตอนโหลดซ้ำ ──
    await typeIn('lookahead_time', '2');
    liveV.cost_scaling_factor = 5; push(); await advance(5000); await settle();
    check('19k Poll: ช่องที่กำลังแก้คงค่าที่พิมพ์ ; ช่องที่ไม่ได้แก้ตามค่าที่ใช้อยู่ที่เปลี่ยนจากข้างนอก', shown('lookahead_time') === '2' && shown('cost_scaling_factor') === '5' &&
      nrow('cost_scaling_factor').run.textContent.startsWith('Running now: 5'), `${shown('lookahead_time')} ${shown('cost_scaling_factor')}`);
    liveV.cost_scaling_factor = 3; push(); await typeIn('lookahead_time', '1.5'); await advance(5000); await settle();

    // ── ความถี่การดึงข้อมูล ──
    let c0 = liveCalls();
    await advance(5000); await settle();
    check('19k Poll: Nav รันอยู่ → อ่านค่าสดทุก 5 วินาที', liveCalls() === c0 + 1, liveCalls() - c0);
    nav('#/settings'); await settle(); c0 = liveCalls();
    await advance(10000); await settle();
    check('19k Poll: ออกจากหน้า Navigation → ไม่อ่านค่าสด', liveCalls() === c0);
    nav('#/nav'); await settle();

    // ── Navigation กำลังปิด (target ตั้งอยู่) ไม่ถือว่ารันนิ่ง ──
    await typeIn('xy_goal_tolerance', '0.3');
    await send(modeOf('NAVIGATION', 'RUNNING', { target: 'STANDBY' })); await settle();
    check('19k Stopping: Nav กำลังปิด → ไม่ถือว่ารันนิ่ง: ซ่อนค่าที่ใช้อยู่ + Apply ปิดแม้มีค่าที่แก้ค้างอยู่',
      rowsAll().every((r) => r.children[3].hidden === true) && A().disabled === true && shown('xy_goal_tolerance') === '0.3');
    await send(modeOf('NAVIGATION')); await settle();
    check('19k Stopping: กลับมารันนิ่ง → ค่าที่ใช้อยู่กลับมา + Apply กลับมากดได้', rowsAll().every((r) => r.children[3].hidden === false) && A().disabled === false);
    await typeIn('xy_goal_tolerance', '0.2');

    // ── Navigation หยุด ──
    liveV = null; push();
    await send(modeOf('STANDBY')); await settle();
    check('19k Stop: Nav หยุด → บรรทัดค่าที่ใช้อยู่ซ่อน + คำอธิบายกลับเป็น "ครั้งถัดไป" + Apply ปิด', rowsAll().every((r) => r.children[3].hidden === true) &&
      $('navp-hint').textContent === E['ui.navp.hint.idle'] && A().disabled === true);
    c0 = liveCalls();
    await advance(10000); await settle();
    check('19k Stop: Nav ไม่รัน → ไม่เรียกอ่านค่าสดเลย', liveCalls() === c0);

    // ── ภาษา ──
    await typeIn('lookahead_time', '2.5');
    langBtn('th').click(); await flush();
    check('19k TH: กลุ่ม/ชื่อ/คำอธิบาย/Default/ปุ่มเป็นไทย + ค่าที่พิมพ์ค้างอยู่ไม่หาย', groupsEl()[0].children[0].textContent === 'ความเร็ว' &&
      nrow('desired_linear_vel').name.textContent === 'ความเร็วเดินทาง' && nrow('desired_linear_vel').desc.textContent === T['navp.p.desired_linear_vel.desc'] &&
      nrow('desired_linear_vel').def.textContent === 'ค่า Default: 0.2 m/s' && A().textContent === 'ใช้เลย' && S().textContent === 'บันทึกไว้ใช้ครั้งหน้า' &&
      shown('lookahead_time') === '2.5' && nrow('use_collision_detection').stateText.textContent === 'เปิด', groupsEl()[0].children[0].textContent);
    await typeIn('lookahead_time', '9');
    check('19k TH: ข้อความผิดช่วงเป็นไทย', nrow('lookahead_time').err.textContent === 'กรอกตัวเลขตั้งแต่ 0.5 ถึง 3');
    langBtn('en').click(); await flush();
    check('19k TH→EN: กลับเป็นอังกฤษ + ค่าที่พิมพ์ยังอยู่', nrow('desired_linear_vel').name.textContent === 'Cruise speed' && shown('lookahead_time') === '9');
    await typeIn('lookahead_time', '1.5');

    // ── ใช้ไม่ได้ (อ่านไฟล์ Default ไม่ได้) ──
    server.navInfo = { status: 200, data: { available: false, groups: [], modified: 0, in_use: null, restart_needed: false } };
    await advance(5000); await settle();
    check('19k Unavailable: ไม่มีแถว + ข้อความอธิบาย + ปุ่มซ่อน', rowsAll().length === 0 && $('navp-hint').textContent === E['ui.navp.unavailable'] &&
      A().hidden === true && S().hidden === true && R().hidden === true);
    push(); await advance(5000); await settle();
    check('19k Unavailable: กลับมาอ่านได้ → แถวและปุ่มกลับมา', rowsAll().length === 13 && A().hidden === false && S().hidden === false);

    // ── เก็บกวาด ──
    server.navInfo = null; server.navLive = null; server.navSave = null; server.navApply = null;
    await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19l) หน้า Navigation: แผนที่ที่ใช้ร่วมกับหน้า Mapping (เกต · แผนที่สด · ไม่ปนข้อมูลข้ามหน้า) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const ncv = $('nav-map-canvas'), mcv = $('map-canvas');
    const nlog = (name) => (ncv.ctxLog || []).filter((c) => c.name === name);
    const mlogCount = () => (mcv.ctxLog || []).length;
    const mapCalls = () => server.calls.filter((c) => c.path === '/api/map/live').length;
    const gate = () => $('nav-map-gate-msg').textContent;
    const widthNow = () => { const d = nlog('drawImage'); return d[d.length - 1].args[3]; };

    langBtn('en').click(); await flush();
    server.map = null; server.scan = null;
    await send(modeOf('STANDBY'));
    nav('#/nav'); await settle();

    check('19l NavMap (N2): Nav ยังไม่รัน → คลังแผนที่ขึ้นบนสุดของคอลัมน์ขวา (class nav-idle)', hasClass($('nav-right'), 'nav-idle') && /\.map-right\.nav-idle #maplib-card\s*\{\s*order:\s*-1/.test(read('web/style.css')));
    check('19l NavMap (N1): ไม่มีการ์ด "Coming next" ที่ยังเขียนว่าส่ง goal ไม่ได้ (ทำเสร็จแล้ว) และไม่มีคีย์ที่ไม่ใช้ค้าง', !/Coming next/.test(html) && !('ui.nav.next.title' in I.DICT.en) && !('ui.nav.plan3' in I.DICT.en) && !('ui.nav.plan4' in I.DICT.th));
    // ── เกต (Navigation ยังไม่ทำงานนิ่ง) ──
    check('19l NavMap: Standby → เห็นเกต ซ่อนแผนที่ + ข้อความบอกวิธีเริ่ม', $('nav-map-gate').hidden === false && $('nav-map-live').hidden === true && gate() === E['nav.gate.standby'], gate());
    await send(modeOf('MAPPING'));
    check('19l NavMap: โหมดอื่นรันอยู่ → บอกว่าการเริ่ม Navigation จะปิดโหมดนั้นก่อน', gate() === 'Mapping mode is running. Starting Navigation will shut it down first.', gate());
    await send(modeOf('STANDBY', 'STARTING', { target: 'NAVIGATION' }));
    check('19l NavMap: กำลังเปิด Navigation → ข้อความกำลังเปิด', gate() === E['nav.gate.starting'] && $('nav-map-live').hidden === true, gate());
    await send(modeOf('NAVIGATION', 'STOPPING', { target: 'STANDBY' }));
    check('19l NavMap: กำลังปิด → ข้อความ busy ไม่ใช่ "กำลังเปิด"', gate() === E['nav.gate.busy'], gate());
    await send(modeOf('STANDBY', 'RUNNING', { external: ['controller_server'] }));
    check('19l NavMap: ระบบนอกแอปรันอยู่ → ข้อความ + ไม่แสดงแผนที่', /started outside this app/.test(gate()) && $('nav-map-live').hidden === true);
    await send(modeOf('STANDBY', 'RUNNING', { external: [], leftover: { mode: 'MAPPING' } }));
    check('19l NavMap: ของค้างจากรอบก่อน → ข้อความระบุโหมดที่ค้าง', gate() === 'A previous session left the Mapping system running. An admin can stop it with Reset all on the Home page.', gate());
    await send(modeOf('STANDBY', 'ERROR', { leftover: null, message: M('mode.err.timeout', { mode: 'NAVIGATION', seconds: 40, log: '/x.log' }) }));
    check('19l NavMap: error → บอกว่าครั้งล่าสุดไม่สำเร็จ + เหตุผลที่แปลแล้ว', /^The last mode change failed: Navigation mode did not start/.test(gate()), gate());
    await send(modeOf('STANDBY'));
    ws.onclose(); await flush();
    check('19l NavMap: ขาดการเชื่อมต่อ → ข้อความขาดการเชื่อมต่อ', gate() === E['nav.gate.disconnected'], gate());
    ws.onopen(); await send(modeOf('STANDBY'));
    const m0 = mapCalls();
    await advance(10000); await settle();
    check('19l NavMap: Nav ไม่รัน → ไม่ดึงแผนที่เลย', mapCalls() === m0, mapCalls() - m0);

    // ── Navigation รันนิ่ง: แผนที่สด ──
    const mapping0 = mlogCount();
    await send({ ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 } }); await settle();
    check('19l NavMap (N2): Nav รันนิ่ง → เอา nav-idle ออก (ตำแหน่ง/จุดหมายกลับมาอยู่ก่อน)', !hasClass($('nav-right'), 'nav-idle'));
    check('19l NavMap: Nav รันนิ่ง → เห็นการ์ดแผนที่ ซ่อนเกต + ข้อความรอแผนที่', $('nav-map-live').hidden === false && $('nav-map-gate').hidden === true &&
      $('nav-map-wait').hidden === false && nlog('drawImage').length === 0);
    server.map = { status: 200, data: MAPMETA(41) };
    await advance(3100); await settle();
    check('19l NavMap: มีแผนที่ → โหลดภาพเวอร์ชันนั้น + ซ่อนข้อความรอ + แสดงขนาด', images[images.length - 1].src === '/api/map/live.png?v=41' && $('nav-map-wait').hidden === true &&
      /^Map 12\.0 × 10\.0 m · 0\.05 m per cell · updated \d\d:\d\d$/.test($('nav-map-info').textContent), $('nav-map-info').textContent);
    check('19l NavMap: วาดลง canvas ของหน้า Navigation (ไม่ใช่ของหน้า Mapping) + มีหุ่น + สเกลบาร์ + aria', nlog('drawImage').length >= 1 && mlogCount() === mapping0 &&
      nlog('fill').length >= 1 && nlog('fillText').some((c) => /^\d+(\.\d+)? m$/.test(c.args[0])) && ncv.attrs['aria-label'] === 'Live map, 12.0 by 10.0 metres. Robot at x 1.0, y 2.0.', ncv.attrs['aria-label']);
    const nImg = images.length; await advance(3100); await settle();
    check('19l NavMap: เวอร์ชันเดิม → ไม่โหลดภาพซ้ำ', images.length === nImg);

    // ── ซูม/เลื่อน ──
    const w0 = widthNow();
    $('nav-zoom-in').click(); await flush();
    check('19l NavMap: ปุ่ม + ซูมเข้า (ภาพใหญ่ขึ้น)', widthNow() > w0, `${w0} → ${widthNow()}`);
    const w1 = widthNow();
    $('nav-zoom-out').click(); await flush();
    check('19l NavMap: ปุ่ม − ซูมออก', widthNow() < w1);
    const wBefore = widthNow();
    ncv.dispatch('wheel', { deltaY: -100, clientX: 100, clientY: 100 }); await flush();
    const w2 = widthNow();
    check('19l NavMap: ล้อเมาส์เลื่อนขึ้น = ซูมเข้า', w2 > wBefore, `${wBefore} → ${w2}`);
    ncv.dispatch('wheel', { deltaY: 100, clientX: 100, clientY: 100 }); await flush();
    check('19l NavMap: ล้อเมาส์เลื่อนลง = ซูมออก', widthNow() < w2);
    const clears = nlog('clearRect').length;
    ncv.dispatch('pointerdown', { pointerId: 1, clientX: 100, clientY: 100 });
    ncv.dispatch('pointermove', { pointerId: 1, clientX: 140, clientY: 120 });
    ncv.dispatch('pointerup', { pointerId: 1 });
    check('19l NavMap: ลากนิ้วเดียว = เลื่อนแผนที่ (วาดใหม่)', nlog('clearRect').length > clears);
    $('nav-fit').click(); await flush();
    const wFit = widthNow();
    $('nav-zoom-in').click(); $('nav-zoom-in').click(); await flush();
    $('nav-fit').click(); await flush();
    check('19l NavMap: ปุ่ม Fit กลับมาพอดีจอ', Math.abs(widthNow() - wFit) < 1e-6);
    $('nav-center').click(); await flush();
    check('19l NavMap: ปุ่ม Robot ใช้ได้โดยไม่พัง (วาดต่อ)', nlog('drawImage').length >= 1);

    // ── ไม่ปนข้อมูลข้ามหน้า ──
    await send(modeOf('STANDBY')); await settle();
    check('19l NavMap: Nav หยุด → กลับเป็นเกต + ล้างแผนที่เก่า', $('nav-map-gate').hidden === false && $('nav-map-live').hidden === true);
    const before = images.length;
    await send(modeOf('NAVIGATION')); await settle();
    await advance(3100); await settle();
    check('19l NavMap: Nav เริ่มใหม่ → โหลดภาพใหม่ (ไม่ใช้ภาพเก่าที่ค้าง)', images.length === before + 1, `${images.length - before}`);
    // แผนที่สดของ Mapping ต้องไม่ไปโผล่ที่หน้า Navigation
    await send(modeOf('STANDBY')); await settle();
    server.map = { status: 200, data: MAPMETA(50) };
    nav('#/mapping'); await send(modeOf('MAPPING')); await advance(3100); await settle();
    const drawsMapping = nlog('drawImage').length;
    await send(modeOf('STANDBY')); await settle();
    server.map = { status: 200, data: { available: false, version: 51 } };
    nav('#/nav'); await settle();
    await send(modeOf('NAVIGATION')); await advance(3100); await settle();
    check('19l NavMap: แผนที่ SLAM ของ Mapping ไม่โผล่ที่หน้า Navigation (ยังไม่มีแผนที่ของ Nav → รอ ไม่วาดของเก่า)',
      $('nav-map-wait').hidden === false && nlog('drawImage').length === drawsMapping, `${nlog('drawImage').length} vs ${drawsMapping}`);
    server.map = { status: 200, data: MAPMETA(52) }; await advance(3100); await settle();
    check('19l NavMap: พอมีแผนที่ของ Nav ก็วาด', $('nav-map-wait').hidden === true && nlog('drawImage').length > drawsMapping);

    // ── คำตอบแผนที่ที่มาช้า หลังผู้ใช้เปลี่ยนไปหน้าอื่นแล้ว → ต้องถูกทิ้ง (ไม่โหลดภาพ ไม่วาด) ──
    server.map = { status: 200, data: MAPMETA(80) };
    server.holdPath = '/api/map/live';
    await advance(3100); await settle();
    const imgsHeld = images.length;
    nav('#/home'); await settle();
    server.releaseHeld(); await settle();
    check('19l NavMap: คำตอบที่มาช้าหลังเปลี่ยนหน้า → ถูกทิ้ง (ไม่โหลดภาพของเวอร์ชันนั้น)', images.length === imgsHeld, `${images.length - imgsHeld}`);
    server.holdPath = null; server.releaseHeld = null;
    nav('#/nav'); await settle();

    // ── ซ่อนแท็บ / ออกจากหน้า / สลับภาษา ──
    document.hidden = true; let c0 = mapCalls();
    await advance(10000); await settle();
    check('19l NavMap: แท็บถูกซ่อน → ไม่ดึงแผนที่', mapCalls() === c0);
    document.hidden = false;
    nav('#/settings'); await settle(); c0 = mapCalls();
    await advance(10000); await settle();
    check('19l NavMap: ออกจากหน้า Navigation → ไม่ดึงแผนที่', mapCalls() === c0);
    nav('#/nav'); await settle();
    check('19l NavMap: กลับเข้าหน้า → ดึงทันที', mapCalls() > c0);
    await send(modeOf('STANDBY')); await settle();
    langBtn('th').click(); await flush();
    check('19l NavMap: สลับภาษา → ข้อความเกตเปลี่ยนเป็นไทยทันที', gate() === T['nav.gate.standby'], gate());
    langBtn('en').click(); await flush();
    check('19l NavMap: สลับกลับ EN', gate() === E['nav.gate.standby']);

    // ── เก็บกวาด ──
    server.map = null;
    nav('#/home'); await advance(100);
  }

  // ═════════ 19m) หน้า Navigation: วางตำแหน่งหุ่นบนแผนที่ (แตะ = ตำแหน่ง · ลาก = ทิศ · สองนิ้ว = ซูม/เลื่อน) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const ncv = $('nav-map-canvas');
    const nlog = (name) => (ncv.ctxLog || []).filter((c) => c.name === name);
    const down = (id, x, y) => ncv.dispatch('pointerdown', { pointerId: id, clientX: x, clientY: y });
    const move = (id, x, y) => ncv.dispatch('pointermove', { pointerId: id, clientX: x, clientY: y });
    const up = (id) => ncv.dispatch('pointerup', { pointerId: id });
    const SC = Math.min(320 / 13.2, 272 / 11);                // สเกลของ "พอดีจอ" สำหรับ MAPMETA (12 × 10 m) บน canvas 320 × 272
    const CX = -3.5 + 6, CY = -5 + 5;                         // กึ่งกลางแผนที่ในพิกัดโลก
    const wx = (px) => CX + (px - 160) / SC, wy = (py) => CY - (py - 136) / SC;
    const readout = () => $('navpose-readout').textContent;
    const posts3 = () => server.calls.filter((c) => c.method === 'POST' && c.path === '/api/nav/initialpose');
    // ตัวอย่างตำแหน่งที่วางอยู่ = fill เพิ่มอีก 1 ต่อรอบวาด (หุ่นจริง 1 + ตัวอย่าง 1) — ไม่ใช้สีตรวจ เพราะสีเลเซอร์ตัวเลือกหนึ่งเป็นสีเดียวกัน
    const lastDrawFills = () => { const log = ncv.ctxLog; const k = log.map((c) => c.name).lastIndexOf('clearRect'); return log.slice(k).filter((c) => c.name === 'fill').length; };
    const preview = () => lastDrawFills() - 1;
    const widthNow = () => { const d = nlog('drawImage'); return d[d.length - 1].args[3]; };
    const txNow = () => { const log = ncv.ctxLog; let k = log.map((c) => c.name).lastIndexOf('drawImage'); while (log[k].name !== 'translate') k--; return log[k].args.join(','); };
    const fmt2 = (v) => v.toFixed(2);

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(60) };
    await asAdmin(true);
    await send(modeOf('STANDBY'));
    nav('#/nav'); await settle();

    // ── ใช้ไม่ได้ในสถานะต่างๆ ──
    check('19m Pose: Nav ไม่รัน → ปุ่มปิด + บอกให้เริ่ม Navigation', $('navpose-start').disabled === true && $('navpose-note').textContent === E['navpose.need_nav'] && $('navpose-status').hidden === true);
    await asAdmin(false);
    check('19m Pose: ไม่ใช่ผู้ดูแล → ปุ่มปิด + บอกให้ล็อกอิน', $('navpose-start').disabled === true && $('navpose-note').textContent === E['navpose.need_admin']);
    await asAdmin(true);
    server.map = { status: 200, data: { available: false, version: 59 } };
    await send({ ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } }); await settle();
    check('19m Pose: Nav รันแต่แผนที่ยังไม่มา → ปุ่มปิด + รอแผนที่', $('navpose-start').disabled === true && $('navpose-note').textContent === E['navpose.need_map']);
    server.map = { status: 200, data: MAPMETA(60) };
    await advance(3100); await settle();
    check('19m Pose: Nav รันนิ่ง + แผนที่มาแล้ว + ผู้ดูแล → ปุ่มเปิด ไม่มีข้อความเตือน', $('navpose-start').disabled === false && $('navpose-note').hidden === true &&
      $('navpose-edit').hidden === true && $('navpose-help').hidden === true);
    check('19m Pose: สถานะตำแหน่ง (✔ ดี + ตำแหน่ง/ทิศปัจจุบัน)', $('navpose-status').textContent === '✔ Localized — good Robot at x 1.0 m · y 2.0 m · heading 90°', $('navpose-status').textContent);
    for (const [q, text] of [['fair', '▲ Localized — fair'], ['bad', '✖ Position uncertain — set the robot position again.'], ['unknown', '○ Not localized yet — set the robot position.']]) {
      await send({ ...modeOf('NAVIGATION'), pose: null, localization: { quality: q, sigma: null } });
      check(`19m Pose: คุณภาพตำแหน่ง ${q} → ข้อความ + สัญลักษณ์ (ไม่พึ่งสีอย่างเดียว)`, $('navpose-status').textContent === text, $('navpose-status').textContent);
    }
    await send({ ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } });

    // ── เข้าโหมดวาง ──
    $('navpose-start').click(); await flush();
    check('19m Pose: กดเริ่ม → ซ่อนปุ่มเริ่ม แสดงแผงแก้ไข + คำแนะนำ + canvas เป็นโหมดวาง ; ยังส่งไม่ได้ (ยังไม่ได้แตะ)', $('navpose-start').hidden === true && $('navpose-edit').hidden === false &&
      $('navpose-help').hidden === false && $('navpose-help').textContent === E['navpose.help'] && hasClass(ncv, 'placing') && $('navpose-send').disabled === true &&
      $('navpose-rot-l').disabled === true && $('navpose-readout').hidden === true && preview() === 0);

    // ── ซูมสองนิ้ว/ถูกแย่งนิ้วตอนยังไม่ได้วาง → ต้องไม่วางตำแหน่งโดยบังเอิญ ──
    const wFresh = widthNow();
    down(1, 90, 90); down(2, 220, 90); move(2, 280, 90); await flush();
    up(1); up(2); await flush();
    check('19m Pose: ซูมสองนิ้วตอนยังไม่ได้วาง → แผนที่ซูม แต่ไม่วางตำแหน่ง', widthNow() > wFresh && $('navpose-readout').hidden === true && $('navpose-send').disabled === true && preview() === 0);
    $('nav-fit').click(); await flush();
    down(1, 90, 90); ncv.dispatch('pointercancel', { pointerId: 1, type: 'pointercancel' }); await flush();
    check('19m Pose: pointercancel (ระบบแย่งนิ้ว) → ไม่วางตำแหน่ง', $('navpose-readout').hidden === true && $('navpose-send').disabled === true);
    down(1, 90, 90); down(2, 220, 90); up(2); up(1); await flush();
    check('19m Pose: แตะสองนิ้วพร้อมกันแล้วยกขึ้นโดยไม่ขยับ → ไม่วางตำแหน่ง', $('navpose-readout').hidden === true && $('navpose-send').disabled === true);

    // ── แตะ = ตำแหน่ง (ไม่เลื่อนแผนที่) ──
    const tx0 = txNow();
    down(1, 160, 136); await flush();
    check('19m Pose: นิ้วแตะลงแล้วยังไม่วาง (วางเมื่อยกนิ้ว — กันท่าซูมสองนิ้วทำให้ตำแหน่งเพี้ยน)', $('navpose-readout').hidden === true && preview() === 0);
    up(1); await flush();
    check('19m Pose: แตะกลางจอ → ตำแหน่ง = กึ่งกลางแผนที่ ทิศ 0° + ส่งได้ + มีตัวอย่างสีส้มบนแผนที่', readout() === `x ${fmt2(wx(160))} m · y ${fmt2(wy(136))} m · heading 0°` && $('navpose-send').disabled === false &&
      $('navpose-rot-l').disabled === false && preview() >= 1, readout());
    check('19m Pose: แตะ (ไม่ลาก) ไม่ทำให้แผนที่เลื่อน', txNow() === tx0);
    // ── ลากชี้ทิศ ──
    down(1, 200, 106); move(1, 250, 106); await flush();
    check('19m Pose: ลากไปทางขวา → ทิศ 0° (ตำแหน่งที่แตะ)', readout() === `x ${fmt2(wx(200))} m · y ${fmt2(wy(106))} m · heading 0°`, readout());
    move(1, 200, 56); await flush();
    check('19m Pose: ลากขึ้น → ทิศ +90° (ทวนเข็ม ตรงกับ REP-103)', readout().endsWith('heading 90°'), readout());
    move(1, 150, 106); await flush();
    check('19m Pose: ลากไปทางซ้าย → ทิศ 180°', readout().endsWith('heading 180°'), readout());
    move(1, 200, 156); await flush();
    check('19m Pose: ลากลง → ทิศ −90°', readout().endsWith('heading -90°'), readout());
    move(1, 203, 106); await flush();
    check('19m Pose: ขยับน้อยกว่า 12 px ถือว่ายังไม่ได้ชี้ทิศใหม่ (ทิศเดิมคงอยู่)', readout().endsWith('heading -90°'), readout());
    up(1);
    check('19m Pose: ระหว่างวางไม่ทำให้แผนที่เลื่อน', txNow() === tx0);
    // ── แตะใหม่ = ย้ายตำแหน่ง คงทิศ ──
    down(1, 100, 60); up(1); await flush();
    check('19m Pose: แตะที่ใหม่ → ย้ายตำแหน่ง แต่คงทิศเดิม', readout() === `x ${fmt2(wx(100))} m · y ${fmt2(wy(60))} m · heading -90°`, readout());

    // ── ปุ่มหมุน ──
    $('navpose-rot-l').click(); await flush();
    check('19m Pose: หมุนซ้าย 15° (ทวนเข็ม) −90° → −75°', readout().endsWith('heading -75°'), readout());
    $('navpose-rot-r').click(); $('navpose-rot-r').click(); await flush();
    check('19m Pose: หมุนขวา 15° สองครั้ง → −105°', readout().endsWith('heading -105°'), readout());
    for (let i = 0; i < 11; i++) { $('navpose-rot-r').click(); }
    await flush();
    check('19m Pose: หมุนเกิน −180° แล้ววนเข้าช่วง (−105° − 165° = −270° → 90°)', readout().endsWith('heading 90°'), readout());

    // ── สองนิ้ว = ซูม ไม่ขยับตำแหน่งที่วาง ──
    const posBefore = readout();
    const w0 = widthNow();
    down(1, 100, 100); down(2, 200, 100); move(2, 260, 100); await flush();
    check('19m Pose: นิ้วที่สอง → ซูมแผนที่ ไม่ใช่การวาง (ตำแหน่ง/ทิศที่วางไว้ไม่เปลี่ยน)', widthNow() > w0 && readout() === posBefore, `${w0} → ${widthNow()} | ${readout()}`);
    up(1); up(2);
    $('nav-fit').click(); await flush();                         // คืนมุมมองพอดีจอ (สูตรพิกัดของเทสต์ถัดไปอิงมุมมองนี้)

    // ── ส่ง ──
    const sv0 = posts3().length;
    $('navpose-send').click(); $('navpose-send').click(); await settle();
    const sent = posts3()[sv0];
    check('19m Pose: ส่ง → POST {x, y, yaw} (เรเดียน) พร้อม CSRF · คลิกซ้ำเร็วๆ ส่งครั้งเดียว',
      posts3().length === sv0 + 1 && Math.abs(sent.body.x - wx(100)) < 1e-9 && Math.abs(sent.body.y - wy(60)) < 1e-9 && Math.abs(sent.body.yaw - Math.PI / 2) < 1e-9 &&
      sent.headers['X-Requested-With'] === 'agv-web', JSON.stringify(sent && sent.body));
    check('19m Pose: ส่งสำเร็จ → toast + ออกจากโหมดวาง + ตัวอย่างหาย + ปุ่มเริ่มกลับมา', $('toast').textContent === E['toast.navpose_sent'] && $('navpose-edit').hidden === true &&
      $('navpose-start').hidden === false && !hasClass(ncv, 'placing') && $('navpose-readout').hidden === true, $('toast').textContent);

    // ── ส่งล้มเหลว: เก็บตำแหน่งไว้ลองซ้ำ ──
    $('navpose-start').click(); await flush(); down(1, 130, 120); up(1); await flush();
    server.poseHandler = () => ({ status: 409, data: { ok: false, error: M('nav.err.no_listener') } });
    $('navpose-send').click(); await settle();
    check('19m Pose: เซิร์ฟเวอร์ปฏิเสธ → toast เหตุผล + ยังอยู่ในโหมดวาง + ตำแหน่งไว้ลองซ้ำ + ปุ่มกลับมากดได้', $('toast').textContent === E['nav.err.no_listener'] && $('navpose-edit').hidden === false &&
      readout().startsWith(`x ${fmt2(wx(130))}`) && $('navpose-send').disabled === false, $('toast').textContent);
    server.poseHandler = () => ({ status: 400, data: { ok: false, error: M('nav.err.pose_outside') } });
    $('navpose-send').click(); await settle();
    check('19m Pose: นอกแผนที่ (400) → toast เหตุผลที่แปลแล้ว', $('toast').textContent === 'That position is outside the map', $('toast').textContent);
    server.poseHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('navpose-send').click(); await settle();
    check('19m Pose: เซสชันหมด (401) → ขอ PIN ใหม่', $('pin-dialog').open === true && $('toast').textContent === E['toast.session_expired']);
    $('pin-cancel').click(); server.poseHandler = null; await asAdmin(true); await settle();
    check('19m Pose: เซสชันหมดแล้วโหมดวางถูกยกเลิก (ไม่ใช่ผู้ดูแลแล้ว)', $('navpose-edit').hidden === true && $('navpose-readout').hidden === true);

    // ── ยกเลิก / คีย์บอร์ด ──
    $('navpose-start').click(); await flush(); down(1, 140, 140); up(1); await flush();
    $('navpose-cancel').click(); await flush();
    check('19m Pose: Cancel → ออกจากโหมดวาง ล้างตำแหน่ง ตัวอย่างหาย', $('navpose-edit').hidden === true && $('navpose-start').hidden === false && !hasClass(ncv, 'placing') && $('navpose-readout').hidden === true);
    const sv1 = posts3().length;
    $('navpose-start').click(); await flush(); down(1, 140, 140); up(1); await flush();
    ncv.dispatch('keydown', { key: 'Escape' }); await flush();
    check('19m Pose: Esc บนแผนที่ = ยกเลิก', $('navpose-edit').hidden === true && posts3().length === sv1);
    $('navpose-start').click(); await flush(); down(1, 140, 140); up(1); await flush();
    ncv.dispatch('keydown', { key: 'Enter' }); await settle();
    check('19m Pose: Enter บนแผนที่ = ส่ง', posts3().length === sv1 + 1 && $('navpose-edit').hidden === true);
    $('navpose-start').click(); await flush();
    ncv.dispatch('keydown', { key: 'Enter' }); await settle();
    check('19m Pose: Enter ตอนยังไม่ได้แตะ = ไม่ส่ง', posts3().length === sv1 + 1 && $('navpose-edit').hidden === false);
    $('navpose-cancel').click(); await flush();

    // ── ยกเลิกเองเมื่อสถานการณ์เปลี่ยน ──
    $('navpose-start').click(); await flush(); down(1, 150, 150); up(1); await flush();
    await send(modeOf('STANDBY')); await settle();
    check('19m Pose: Nav หยุดระหว่างวาง → ยกเลิกเอง + บอกให้เริ่ม Navigation', $('navpose-edit').hidden === true && $('navpose-note').textContent === E['navpose.need_nav']);
    await send({ ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } }); await advance(3100); await settle();
    $('navpose-start').click(); await flush(); down(1, 150, 150); up(1); await flush();
    nav('#/home'); await settle(); nav('#/nav'); await settle();
    check('19m Pose: ออกจากหน้านี้แล้วกลับมา → ไม่ค้างโหมดวาง/ตำแหน่งเก่า', $('navpose-edit').hidden === true && $('navpose-readout').hidden === true);
    $('navpose-start').click(); await flush(); down(1, 150, 150); up(1); await flush();
    await asAdmin(false); await settle();
    check('19m Pose: เสียสิทธิ์ผู้ดูแลระหว่างวาง → ยกเลิกเอง', $('navpose-edit').hidden === true && $('navpose-start').disabled === true);
    await asAdmin(true); await settle();

    // ── ภาษา ──
    $('navpose-start').click(); await flush(); down(1, 160, 136); up(1); await flush();
    langBtn('th').click(); await flush();
    check('19m Pose: สลับภาษา → คำแนะนำ/ค่าที่อ่านได้/สถานะเป็นไทย (ค่าที่วางไว้ยังอยู่)', $('navpose-help').textContent === T['navpose.help'] &&
      readout() === `x ${fmt2(wx(160))} ม. · y ${fmt2(wy(136))} ม. · ทิศ 0°` && $('navpose-status').textContent.startsWith('✔ รู้ตำแหน่งแล้ว — ดี'), `${readout()} | ${$('navpose-status').textContent}`);
    langBtn('en').click(); await flush();
    $('navpose-cancel').click(); await flush();

    // ── เก็บกวาด ──
    server.map = null; server.poseHandler = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19n) หน้า Navigation: ส่งหุ่นไปจุดหมาย (เลือกจุด · ยืนยันทุกครั้ง · สถานะจาก Nav2 · ยกเลิก · ■) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const ncv = $('nav-map-canvas');
    const down = (id, x, y) => ncv.dispatch('pointerdown', { pointerId: id, clientX: x, clientY: y });
    const up = (id) => ncv.dispatch('pointerup', { pointerId: id });
    const SC = Math.min(320 / 13.2, 272 / 11);
    const CX = -3.5 + 6, CY = -5 + 5;
    const wx = (px) => CX + (px - 160) / SC, wy = (py) => CY - (py - 136) / SC;
    const goalPosts = () => server.calls.filter((c) => c.method === 'POST' && c.path === '/api/nav/goal');
    const cancelPosts = () => server.calls.filter((c) => c.method === 'POST' && c.path === '/api/nav/goal/cancel');
    const stopPosts = () => server.calls.filter((c) => c.method === 'POST' && c.path === '/api/drive/stop');
    const goalOf = (state, extra = {}) => ({ status: 200, data: { running: true, goal: Object.assign({ state, seq: 1, goal: { x: 2.5, y: 1.0, yaw: 0 }, reason: null,
      feedback: null, elapsed: 5, waiting_assumed: false }, extra) } });
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } };

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(70) };
    await asAdmin(true);
    await send(modeOf('STANDBY'));
    nav('#/nav'); await settle();

    // ── ใช้ไม่ได้ ──
    check('19n Goal: Nav ไม่รัน → ปุ่มปิด + บอกให้เริ่ม Navigation', $('navgoal-start').disabled === true && $('navgoal-note').textContent === E['navgoal.need_nav'], $('navgoal-note').textContent);
    await asAdmin(false);
    check('19n Goal: ไม่ใช่ผู้ดูแล → ปุ่มปิด + บอกให้ล็อกอิน', $('navgoal-start').disabled === true && $('navgoal-note').textContent === E['navgoal.need_admin']);
    await asAdmin(true);
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 } }); await advance(3100); await settle();
    check('19n Goal: ยังไม่ localize (bad) → ปุ่มปิด + บอกให้วางตำแหน่งหุ่นก่อน', $('navgoal-start').disabled === true && $('navgoal-note').textContent === E['navgoal.need_loc']);
    await send({ ...okState, localization: { quality: 'unknown', sigma: null }, pose: null }); await settle();
    check('19n Goal: ไม่รู้ตำแหน่ง (unknown) → ปุ่มปิด', $('navgoal-start').disabled === true);
    await send({ ...okState, localization: { quality: 'fair', sigma: 0.2 } }); await settle();
    check('19n Goal: localize พอใช้ (fair) → ปุ่มเปิด ไม่มีข้อความเตือน', $('navgoal-start').disabled === false && $('navgoal-note').hidden === true);
    await send(okState); await settle();
    check('19n Goal: ปุ่มหยุดเดินทางซ่อนอยู่ตอนไม่มี goal', $('navgoal-stop').hidden === true && $('navgoal-status').hidden === true);

    // ── เลือกจุด → ยืนยัน → ส่ง ──
    $('navgoal-start').click(); await flush();
    check('19n Goal: เริ่มเลือก → แผงแก้ไข+คำแนะนำ ; ยังส่งไม่ได้ ; canvas เป็นโหมดวาง', $('navgoal-edit').hidden === false && $('navgoal-start').hidden === true &&
      $('navgoal-help').textContent === E['navgoal.help'] && $('navgoal-send').disabled === true && hasClass(ncv, 'placing'));
    ncv.dispatch('keydown', { key: 'Enter' }); await settle();
    check('19n Goal: Enter ตอนยังไม่ได้แตะ → ไม่มีหน้าต่างยืนยัน ไม่ส่ง', $('confirm-dialog').open !== true && goalPosts().length === 0);
    down(1, 220, 100); up(1); await flush();
    check('19n Goal: แตะ → readout + ส่งได้', $('navgoal-readout').hidden === false && $('navgoal-readout').textContent === `x ${wx(220).toFixed(2)} m · y ${wy(100).toFixed(2)} m · heading 0°` && $('navgoal-send').disabled === false, $('navgoal-readout').textContent);
    $('navgoal-rot-l').click(); await flush();
    check('19n Goal: ปุ่มหมุน 15°', $('navgoal-readout').textContent.endsWith('heading 15°'), $('navgoal-readout').textContent);
    $('navgoal-send').click(); await settle();
    check('19n Goal: กดส่ง → ถามยืนยันก่อน (หน้าต่างเตือน + จุดหมาย + ข้อความความปลอดภัย) ยังไม่ส่งอะไร',
      $('confirm-dialog').open === true && $('confirm-dialog').className === 'dlg-warn' && $('confirm-title').textContent === E['confirm.goal.title'] &&
      $('confirm-list').children.length === 2 && $('confirm-list').children[1].textContent === E['confirm.goal.safety'] &&
      $('confirm-list').children[0].textContent === `Destination: x ${wx(220).toFixed(2)} m · y ${wy(100).toFixed(2)} m · heading 15°` && goalPosts().length === 0,
      $('confirm-list').children.map((c) => c.textContent).join(' | '));
    $('confirm-cancel').click(); await settle();
    check('19n Goal: ไม่ยืนยัน → ไม่ส่ง · ยังอยู่ในโหมดเลือก · จุดยังอยู่', goalPosts().length === 0 && $('navgoal-edit').hidden === false && $('navgoal-readout').hidden === false);
    $('navgoal-send').click(); await settle();
    $('confirm-ok').click(); await settle();
    const sent = goalPosts()[0];
    check('19n Goal: ยืนยัน → POST {x,y,yaw,confirmed:true} พร้อม CSRF ครั้งเดียว', goalPosts().length === 1 && sent.body.confirmed === true &&
      Math.abs(sent.body.x - wx(220)) < 1e-9 && Math.abs(sent.body.y - wy(100)) < 1e-9 && Math.abs(sent.body.yaw - Math.PI / 12) < 1e-9 &&
      sent.headers['X-Requested-With'] === 'agv-web', JSON.stringify(sent && sent.body));
    check('19n Goal: ส่งสำเร็จ → toast + ออกจากโหมดเลือก + ปุ่มหยุดเดินทางโผล่ + ปุ่มเริ่มปิด',
      $('toast').textContent === E['toast.navgoal_sent'] && $('navgoal-edit').hidden === true && $('navgoal-stop').hidden === false &&
      $('navgoal-start').disabled === true && $('navgoal-status').textContent === '▲ ' + E['navgoal.st.active'], $('toast').textContent + ' | ' + $('navgoal-status').textContent);

    // ── สถานะจาก Nav2 ──
    server.goalGet = goalOf('active', { feedback: { distance_remaining: 2.46, eta: 8.7, recoveries: 0, elapsed: 4 } });
    await advance(1100); await settle();
    check('19n Goal: ความคืบหน้าจาก feedback (ระยะเหลือ + เวลาโดยประมาณ)', $('navgoal-detail').hidden === false && $('navgoal-detail').textContent === '2.5 m left · about 9 s', $('navgoal-detail').textContent);
    server.goalGet = goalOf('active', { feedback: { distance_remaining: 2.46, eta: 8.7, recoveries: 2, elapsed: 20 }, waiting_assumed: true });
    await advance(1100); await settle();
    check('19n Goal: ระยะไม่ลด → บอกว่า "น่าจะรอ" และระบุว่าเป็นข้อสันนิษฐาน + จำนวนครั้งกู้ทาง',
      $('navgoal-detail').textContent === '2.5 m left · about 9 s · 2 recovery attempts · ' + E['navgoal.waiting'] && E['navgoal.waiting'].includes('assumed'), $('navgoal-detail').textContent);
    const reqs = goalPosts().length;
    $('navgoal-start').click(); await flush();
    check('19n Goal: มี goal ค้าง → เริ่มเลือกจุดใหม่ไม่ได้', $('navgoal-edit').hidden === true && goalPosts().length === reqs);

    // ── ยกเลิก (ไม่ถามยืนยัน) ──
    const c0 = cancelPosts().length;
    $('navgoal-stop').click(); await settle();
    check('19n Goal: ปุ่มยกเลิกเดินทาง → POST cancel ทันที (ไม่มีหน้าต่างยืนยัน) + toast + สถานะ "กำลังยกเลิก"',
      cancelPosts().length === c0 + 1 && $('confirm-dialog').open !== true && $('toast').textContent === E['toast.navgoal_cancel'] && $('navgoal-status').textContent.includes(E['navgoal.st.canceling']) &&
      cancelPosts()[c0].headers['X-Requested-With'] === 'agv-web', $('navgoal-status').textContent);
    server.goalGet = goalOf('canceled');
    await advance(1100); await settle();
    check('19n Goal: Nav2 รายงานยกเลิกแล้ว → แจ้งครั้งเดียว + ปุ่มหยุดซ่อน + เริ่มเลือกใหม่ได้',
      $('toast').textContent === E['navgoal.done.canceled'] && $('navgoal-stop').hidden === true && $('navgoal-start').disabled === false && $('navgoal-status').textContent === '○ ' + E['navgoal.st.canceled'], $('toast').textContent);
    $('toast').textContent = '';
    await advance(2100); await settle();
    check('19n Goal: ไม่แจ้งซ้ำทุกรอบโพล', $('toast').textContent === '');

    // ── สำเร็จ / ล้มเหลว ──
    server.goalGet = goalOf('active', { seq: 2 });
    await advance(1100); await settle();
    server.goalGet = goalOf('succeeded', { seq: 2 });
    await advance(1100); await settle();
    check('19n Goal: ถึงจุดหมาย → แจ้ง + สถานะสำเร็จ', $('toast').textContent === E['navgoal.done.succeeded'] && $('navgoal-status').textContent === '✔ ' + E['navgoal.st.succeeded']);
    server.goalGet = goalOf('active', { seq: 3 });
    await advance(1100); await settle();
    server.goalGet = goalOf('failed', { seq: 3, reason: 'nav.goal.aborted' });
    await advance(1100); await settle();
    check('19n Goal: ล้มเหลว → สถานะบอกเหตุผล (แปลแล้ว) + แจ้ง', $('navgoal-status').textContent === '✖ Trip stopped — ' + E['nav.goal.aborted'] && $('toast').textContent === E['navgoal.done.failed'], $('navgoal-status').textContent);
    server.goalGet = goalOf('active', { seq: 4 });
    await advance(1100); await settle();
    server.goalGet = goalOf('failed', { seq: 4, reason: 'nav.goal.lost' });
    await advance(1100); await settle();
    check('19n Goal: Nav หยุดกลางทาง (lost) → บอกเหตุผล', $('navgoal-status').textContent.endsWith(E['nav.goal.lost']), $('navgoal-status').textContent);
    server.goalGet = null;

    // ── เซิร์ฟเวอร์ปฏิเสธ: เก็บจุดไว้ ──
    $('navgoal-start').click(); await flush(); down(1, 100, 100); up(1); await flush();
    server.goalHandler = () => ({ status: 409, data: { ok: false, error: M('nav.err.goal_not_localized') } });
    $('navgoal-send').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19n Goal: ถูกปฏิเสธ → toast เหตุผลที่แปลแล้ว + ยังอยู่ในโหมดเลือก + จุดยังอยู่',
      $('toast').textContent === E['nav.err.goal_not_localized'] && $('navgoal-edit').hidden === false && $('navgoal-readout').hidden === false, $('toast').textContent);
    server.goalHandler = () => ({ status: 400, data: { ok: false, error: M('nav.err.goal_tight', { clearance: 0.4 }) } });
    $('navgoal-send').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19n Goal: จุดไม่ปลอดภัย → ข้อความมีระยะที่ต้องการ', $('toast').textContent.includes('0.4 m'), $('toast').textContent);
    server.goalHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('navgoal-send').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19n Goal: เซสชันหมด (401) → ขอ PIN ใหม่', $('pin-dialog').open === true);
    $('pin-cancel').click(); server.goalHandler = null; await asAdmin(true); await settle();
    check('19n Goal: เสียสิทธิ์ผู้ดูแลระหว่างเลือก → ยกเลิกเอง', $('navgoal-edit').hidden === true);

    // ── ทีละเครื่องมือ ──
    await send(okState); await settle();
    const g0 = goalPosts().length;
    $('navgoal-start').click(); await flush(); down(1, 120, 120); up(1); await flush();
    $('navpose-start').click(); await flush();
    check('19n Goal: เริ่มวางตำแหน่งหุ่น → การเลือกจุดหมายถูกยกเลิก (ทีละเครื่องมือ)', $('navgoal-edit').hidden === true && $('navgoal-start').hidden === false && $('navpose-edit').hidden === false);
    down(1, 120, 120); up(1); await flush();
    check('19n Goal: แตะตอนวางตำแหน่ง → เป็นของเครื่องมือตำแหน่ง ไม่ใช่จุดหมาย', $('navpose-readout').hidden === false && $('navgoal-readout').hidden === true);
    $('navgoal-start').click(); await flush();
    check('19n Goal: เริ่มเลือกจุดหมาย → การวางตำแหน่งถูกยกเลิก', $('navpose-edit').hidden === true && $('navgoal-edit').hidden === false);
    ncv.dispatch('keydown', { key: 'Escape' }); await flush();
    check('19n Goal: Esc = ยกเลิกการเลือก ไม่ส่งอะไร', $('navgoal-edit').hidden === true && goalPosts().length === g0);
    $('navgoal-start').click(); await flush(); down(1, 130, 130); up(1); await flush();
    ncv.dispatch('keydown', { key: 'Enter' }); await settle();
    check('19n Goal: Enter บนแผนที่ → ถามยืนยัน (ไม่ส่งเลย)', $('confirm-dialog').open === true && goalPosts().length === g0);
    $('confirm-cancel').click(); await settle();
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 }, pose: null }); await settle();
    ncv.dispatch('keydown', { key: 'Enter' }); await settle();
    check('19n Goal: เลือกจุดไว้แล้ว localization หลุด → Enter ไม่ถามยืนยัน ไม่ส่ง · ปุ่มส่งปิด', $('confirm-dialog').open !== true && goalPosts().length === g0 && $('navgoal-send').disabled === true);
    await send(okState); await settle();
    check('19n Goal: localization กลับมา → ส่งได้อีก (จุดที่เลือกไว้ยังอยู่)', $('navgoal-send').disabled === false);
    server.holdPath = '/api/nav/goal';
    ncv.dispatch('keydown', { key: 'Enter' }); await settle(); $('confirm-ok').click(); await flush();
    ncv.dispatch('keydown', { key: 'Enter' }); $('navgoal-send').click(); await settle();
    check('19n Goal: คำขอส่งค้างอยู่ → Enter/คลิกซ้ำไม่เปิดหน้าต่างยืนยันซ้ำ · ปุ่มส่งปิด', $('confirm-dialog').open !== true && $('navgoal-send').disabled === true);
    server.releaseHeld(); server.holdPath = null; await settle();
    check('19n Goal: ปล่อยคำตอบ → ส่งไปครั้งเดียวเท่านั้น', goalPosts().length === g0 + 1, String(goalPosts().length - g0));
    $('navgoal-cancel').click(); await flush();

    // ── ปุ่ม ■ บนแถบบน ──
    check('19n Goal: ■ ใช้ได้ใน Navigation (Admin) + ชื่อปุ่มบอกว่ายกเลิกการเดินทาง', $('stop').disabled === false && $('stop').attrs['aria-label'] === E['ui.stop.aria.nav'], $('stop').attrs['aria-label']);
    const s0 = stopPosts().length;
    $('stop').click(); await settle();
    check('19n Goal: กด ■ ใน Navigation → POST /api/drive/stop (เซิร์ฟเวอร์ยกเลิก goal + ศูนย์)', stopPosts().length === s0 + 1);
    await asAdmin(false);
    check('19n Goal: ไม่ใช่ผู้ดูแล → ■ ปิด', $('stop').disabled === true);
    const cc = cancelPosts().length;
    $('navgoal-stop').click(); await settle();
    check('19n Goal: ไม่ใช่ผู้ดูแลแล้วคลิกปุ่มยกเลิกเดินทาง (ซ่อนอยู่) → ไม่ส่งอะไร', cancelPosts().length === cc);
    await asAdmin(true);

    // ── สลับภาษา ──
    server.goalGet = goalOf('active', { seq: 9, feedback: { distance_remaining: 1.0, eta: 3, recoveries: 0, elapsed: 1 } });
    await advance(1100); await settle();
    langBtn('th').click(); await flush();
    check('19n Goal: สลับภาษา → สถานะ/ความคืบหน้า/ปุ่ม เป็นไทย', $('navgoal-status').textContent === '▲ ' + T['navgoal.st.active'] && $('navgoal-detail').textContent === 'เหลืออีก 1.0 ม. · ประมาณ 3 วินาที' &&
      $('navgoal-stop').textContent === T['ui.navgoal.stop'] && $('stop').attrs['aria-label'] === T['ui.stop.aria.nav'], $('navgoal-status').textContent + ' | ' + $('navgoal-detail').textContent);
    langBtn('en').click(); await flush();

    // ── เก็บกวาด ──
    server.goalGet = null; server.map = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19o) หน้า Navigation: จุดเริ่มต้นประจำแผนที่ + Refresh localization ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const posts = (p) => server.calls.filter((c) => c.method === 'POST' && c.path === p);
    const sp = () => posts('/api/nav/startpose');
    const info = (over = {}) => ({ status: 200, data: Object.assign({ map: 'lab', pose: null, auto: { state: 'idle', error: null, name: 'lab' } }, over) });
    const saved = { x: 1.5, y: 0.5, yaw_deg: 90, yaw: 1.5708, saved: 1 };
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } };

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(80) };
    await asAdmin(true);
    await send(modeOf('STANDBY'));
    nav('#/nav'); await settle();
    check('19o Start: Nav ไม่รัน → ปุ่มทั้งหมดปิด', ['navstart-save', 'navstart-apply', 'navpose-refresh'].every((id) => $(id).disabled === true));
    check('19o Start: แผนที่ไม่มีชื่อ → บอกให้เลือกแผนที่จากคลัง', $('navstart-info').textContent === E['navstart.nomap'], $('navstart-info').textContent);

    server.startGet = info();
    await send(okState); await advance(3100); await settle();
    check('19o Start: มีแผนที่ ยังไม่มีจุด → ข้อความบอกวิธีทำ + บันทึกได้ (localize ดี) + ใช้/ลืมไม่ได้',
      $('navstart-info').textContent === 'No start pose saved for map “lab”. Park the robot at its usual spot, set its position, then save it as the start pose.' &&
      $('navstart-save').disabled === false && $('navstart-apply').disabled === true && $('navstart-clear').disabled === true && $('navpose-refresh').disabled === false, $('navstart-info').textContent);
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 } }); await settle();
    check('19o Start: ยังไม่ localize → บันทึกไม่ได้ (กันบันทึกจุดผิด)', $('navstart-save').disabled === true && $('navpose-refresh').disabled === false);
    await send(okState); await settle();

    server.startHandler = (b) => (b.action === 'save' ? info({ pose: saved, ok: true }) : { status: 404, data: {} });
    $('navstart-save').click(); $('navstart-save').click(); await settle();
    check('19o Start: Save → POST {action:save} ครั้งเดียว (คลิกซ้ำไม่ซ้อน) + CSRF + toast + แสดงจุดที่บันทึก',
      sp().length === 1 && sp()[0].body.action === 'save' && sp()[0].headers['X-Requested-With'] === 'agv-web' && $('toast').textContent === E['toast.navstart_saved'] &&
      $('navstart-info').textContent.startsWith('Start pose for map “lab”: x 1.50 m · y 0.50 m · heading 90°') && $('navstart-apply').disabled === false && $('navstart-clear').disabled === false, $('navstart-info').textContent);

    server.startHandler = (b) => info({ pose: saved, ok: true, auto: { state: 'applied', error: null, name: 'lab' } });
    $('navstart-apply').click(); await settle();
    check('19o Start: Use now → POST {action:apply} + toast + แจ้งว่าวางอัตโนมัติแล้ว/ให้ตรวจเลเซอร์', sp()[1].body.action === 'apply' && $('toast').textContent === E['toast.navstart_applied'] &&
      $('navstart-auto').hidden === false && $('navstart-auto').textContent === E['navstart.auto.applied'], $('navstart-auto').textContent);

    server.startGet = info({ pose: saved, auto: { state: 'pending', error: null, name: 'lab' } });
    await advance(3100); await settle();
    check('19o Start: กำลังวางอัตโนมัติ → ข้อความ pending', $('navstart-auto').textContent === E['navstart.auto.pending']);
    server.startGet = info({ pose: saved, auto: { state: 'failed', error: 'nav.err.no_listener', name: 'lab' } });
    await advance(3100); await settle();
    check('19o Start: วางอัตโนมัติล้มเหลว → ข้อความเหตุผลที่แปลแล้ว', $('navstart-auto').textContent === 'The start pose could not be applied automatically: ' + E['nav.err.no_listener'], $('navstart-auto').textContent);
    server.startGet = info({ pose: null, auto: { state: 'failed', error: 'x', name: 'lab' } });
    await advance(3100); await settle();
    check('19o Start: ไม่มีจุดบันทึก → ไม่แสดงสถานะอัตโนมัติ', $('navstart-auto').hidden === true);
    server.startGet = info({ pose: saved });
    await advance(3100); await settle();

    server.startHandler = (b) => ({ status: 409, data: { ok: false, error: M('nav.err.goal_not_localized') } });
    $('navstart-save').click(); await settle();
    check('19o Start: เซิร์ฟเวอร์ปฏิเสธ → toast เหตุผลที่แปลแล้ว + ปุ่มกลับมาใช้ได้', $('toast').textContent === E['nav.err.goal_not_localized'] && $('navstart-save').disabled === false, $('toast').textContent);
    server.startHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('navstart-clear').click(); await settle();
    check('19o Start: เซสชันหมด (401) → ขอ PIN ใหม่', $('pin-dialog').open === true);
    $('pin-cancel').click(); await asAdmin(true); await settle();

    server.startHandler = () => info({ pose: null, ok: true });
    $('navstart-clear').click(); await settle();
    check('19o Start: Forget → POST {action:clear} + toast + จุดหาย', sp().at(-1).body.action === 'clear' && $('toast').textContent === E['toast.navstart_cleared'] && $('navstart-apply').disabled === true);

    const r0 = posts('/api/nav/refresh').length;
    $('navpose-refresh').click(); await settle();
    check('19o Refresh: POST /api/nav/refresh + toast', posts('/api/nav/refresh').length === r0 + 1 && $('toast').textContent === E['toast.navpose_refreshed'] && posts('/api/nav/refresh')[r0].headers['X-Requested-With'] === 'agv-web');
    server.refreshHandler = () => ({ status: 503, data: { ok: false, error: M('nav.err.refresh_failed') } });
    $('navpose-refresh').click(); await settle();
    check('19o Refresh: ล้มเหลว → toast เหตุผล', $('toast').textContent === E['nav.err.refresh_failed'], $('toast').textContent);
    server.refreshHandler = null;

    await asAdmin(false);
    check('19o Start: ไม่ใช่ผู้ดูแล → ปุ่มทั้งหมดปิด', ['navstart-save', 'navstart-apply', 'navstart-clear', 'navpose-refresh'].every((id) => $(id).disabled === true));
    await asAdmin(true);
    await send(modeOf('STANDBY')); await settle();
    check('19o Start: Nav หยุด → ปุ่มวางตำแหน่ง/รีเฟรชปิด', $('navstart-apply').disabled === true && $('navpose-refresh').disabled === true);

    server.startGet = info({ pose: saved });
    await send(okState); await advance(3100); await settle();
    langBtn('th').click(); await flush();
    check('19o Start: สลับภาษา → ข้อความ/ปุ่มเป็นไทย', $('navstart-info').textContent.startsWith('จุดเริ่มต้นของแผนที่ “lab”: x 1.50 ม.') && $('navstart-save').textContent === T['ui.navstart.save'] && $('navpose-refresh').textContent === T['ui.navpose.refresh'], $('navstart-info').textContent);
    langBtn('en').click(); await flush();

    server.startGet = null; server.startHandler = null; server.map = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19p) หน้า Navigation: Drive straight (ตรวจแนวล่วงหน้า · เส้นตัวอย่าง · ยืนยัน · ส่ง) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const ncv = $('nav-map-canvas');
    const posts = (p) => server.calls.filter((c) => c.method === 'POST' && c.path === p);
    const checks = () => posts('/api/nav/goal/straight/check');
    const sends = () => posts('/api/nav/goal/straight');
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } };
    const setDist = (v) => { $('navstraight-dist').value = String(v); };
    const goalOf = (state, extra = {}) => ({ status: 200, data: { running: true, limits: { straight_min: 0.5, straight_max: 10, clearance: 0.4 },
      goal: Object.assign({ state, seq: 1, goal: { x: 1, y: 7, yaw: 1.57 }, reason: null, feedback: null, elapsed: 5, waiting_assumed: false }, extra) } });
    const lineTos = () => { const log = ncv.ctxLog; const k = log.map((c) => c.name).lastIndexOf('clearRect'); return log.slice(k).filter((c) => c.name === 'lineTo').length; };   // เส้นในรอบวาดล่าสุด

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(90) };
    await asAdmin(true);
    await send(modeOf('STANDBY'));
    nav('#/nav'); await settle();
    check('19p Straight: Nav ไม่รัน → ปุ่มปิด', $('navstraight-go').disabled === true);
    await send(okState); await advance(3100); await settle();
    check('19p Straight: Nav รันนิ่ง + localized + แผนที่ → ปุ่มเปิด · ช่องระยะเริ่มต้น 5 ม.', $('navstraight-go').disabled === false && $('navstraight-go').textContent === E['ui.navstraight.go'], `${$('navstraight-go').disabled} ${$('navstraight-dist').value}`);
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 } }); await settle();
    check('19p Straight: ยังไม่ localize → ปุ่มปิด', $('navstraight-go').disabled === true);
    await asAdmin(false);
    check('19p Straight: ไม่ใช่ผู้ดูแล → ปุ่มและช่องปิด', $('navstraight-go').disabled === true && $('navstraight-dist').disabled === true);
    await asAdmin(true); await send(okState); await settle();

    // ── ระยะไม่ถูกต้อง : ไม่ยิงอะไรไปที่เซิร์ฟเวอร์ ──
    for (const bad of ['', 'abc', '0.1', '11', '-3', '0']) {
      setDist(bad);
      $('navstraight-go').click(); await settle();
      check(`19p Straight: ระยะ "${bad}" → toast บอกช่วงที่ใช้ได้ ไม่เรียก API`, $('toast').textContent === 'Enter a distance between 0.5 and 10 m' && checks().length === 0 && $('confirm-dialog').open !== true, $('toast').textContent);
    }

    // ── เพดานมาจากเซิร์ฟเวอร์ (ตั้งค่าได้) ──
    server.goalGet = goalOf('idle'); server.goalGet.data.limits.straight_max = 25;
    await advance(1100); await settle();
    check('19p Straight: เพดานที่เซิร์ฟเวอร์ส่งมา (25) ถูกใช้กับช่องกรอก', $('navstraight-dist').attrs.max === '25', String($('navstraight-dist').attrs.max));
    setDist(20);
    $('navstraight-go').click(); await settle();
    check('19p Straight: 20 ม. ผ่านเมื่อเพดาน 25 → เรียกตรวจแนว', checks().length === 1 && checks()[0].body.distance === 20 && $('confirm-dialog').open === true);
    $('confirm-cancel').click(); await settle();
    server.goalGet = null;
    await advance(1100); await settle();
    check('19p Straight: เพดานกลับเป็น 10 เมื่อเซิร์ฟเวอร์ไม่ส่งมา', $('navstraight-dist').attrs.max === '10' || $('navstraight-dist').attrs.max === '25');
    server.goalGet = goalOf('idle');
    await advance(1100); await settle();
    check('19p Straight: เพดาน 10 จากเซิร์ฟเวอร์', $('navstraight-dist').attrs.max === '10', String($('navstraight-dist').attrs.max));
    server.goalGet = null;

    // ── แนวไม่โล่ง ──
    const n0 = checks().length;
    server.straightCheck = () => ({ status: 200, data: { ok: true, check: { ok: false, at: 6.1, reason: 'tight', min_clearance: 0.3, soft_risk: true, distance: 8, goal: { x: 1, y: 10, yaw: 1.57 } } } });
    setDist(8);
    $('navstraight-go').click(); await settle();
    check('19p Straight: แนวติดผนัง → toast บอกระยะที่ติด + ไม่ขึ้นหน้าต่างยืนยัน + ไม่ส่ง', checks().length === n0 + 1 && $('toast').textContent === 'The line gets closer than 0.4 m to a wall 6.1 m ahead' &&
      $('confirm-dialog').open !== true && sends().length === 0 && $('navstraight-go').disabled === false, $('toast').textContent);
    server.straightCheck = () => ({ status: 200, data: { ok: true, check: { ok: false, at: 3.2, reason: 'unknown', min_clearance: null, soft_risk: true, distance: 8, goal: { x: 1, y: 10, yaw: 1.57 } } } });
    $('navstraight-go').click(); await settle();
    check('19p Straight: ผ่านพื้นที่ไม่รู้ → ข้อความเฉพาะ', $('toast').textContent === 'There is a wall or unknown area 3.2 m ahead — the straight line is not free', $('toast').textContent);
    server.straightCheck = () => ({ status: 200, data: { ok: true, check: { ok: false, at: 1.5, reason: 'outside', min_clearance: null, soft_risk: true, distance: 8, goal: { x: 1, y: 10, yaw: 1.57 } } } });
    $('navstraight-go').click(); await settle();
    check('19p Straight: ออกนอกแผนที่ → ข้อความเฉพาะ', $('toast').textContent === 'The line leaves the map 1.5 m ahead', $('toast').textContent);
    server.straightCheck = () => ({ status: 400, data: { ok: false, error: M('nav.err.straight_distance', { min: 0.5, max: 10 }) } });
    $('navstraight-go').click(); await settle();
    check('19p Straight: เซิร์ฟเวอร์ปฏิเสธระยะ → toast ที่แปลแล้ว', $('toast').textContent === 'The straight distance must be between 0.5 and 10 m', $('toast').textContent);
    server.straightCheck = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('navstraight-go').click(); await settle();
    check('19p Straight: เซสชันหมด → ขอ PIN', $('pin-dialog').open === true);
    $('pin-cancel').click(); server.straightCheck = null; await asAdmin(true); await send(okState); await settle();

    // ── แนวโล่ง → เส้นตัวอย่าง → ยืนยัน ──
    setDist(6);
    await advance(1100); await settle();
    const lt0 = lineTos();                                     // จำนวนเส้นของแผนที่เปล่า (สเกลบาร์ ฯลฯ) ก่อนมีเส้นตัวอย่าง
    $('navstraight-go').click(); await settle();
    check('19p Straight: แนวโล่ง → หน้าต่างยืนยันแบบเตือน (ระยะ + จุดปลาย + ข้อความความปลอดภัย ไม่มีคำเตือนชิดผนัง) ยังไม่ส่ง',
      $('confirm-dialog').open === true && $('confirm-dialog').className === 'dlg-warn' && $('confirm-title').textContent === E['confirm.straight.title'] &&
      $('confirm-list').children.length === 2 && $('confirm-list').children[0].textContent === 'Straight ahead 6.0 m → x 1.00 m · y 8.00 m' &&
      $('confirm-list').children[1].textContent === E['confirm.goal.safety'] && sends().length === 0, $('confirm-list').children.map((c) => c.textContent).join(' | '));
    check('19p Straight: ระหว่างรอยืนยันมีเส้นตัวอย่างบนแผนที่ (เพิ่มเส้น + ลูกศรปลาย) + ปุ่มปิดกันกดซ้ำ', lineTos() === lt0 + 1 + 3 && $('navstraight-go').disabled === true, `${lineTos()} vs ${lt0}`);   // เส้นตัวอย่าง 1 + ลูกศรจุดปลาย 3 (drawRobot)
    $('confirm-cancel').click(); await settle();
    check('19p Straight: ไม่ยืนยัน → ไม่ส่ง · เส้นตัวอย่างหาย (รอบวาดต่อไปไม่มีเส้นใหม่) · ปุ่มกลับมา', sends().length === 0 && $('navstraight-go').disabled === false);
    check('19p Straight: เส้นตัวอย่างไม่ค้างอยู่หลังยกเลิก (รอบวาดล่าสุดกลับมาเท่าเดิม)', lineTos() === lt0, `${lineTos()} vs ${lt0}`);

    server.straightCheck = () => ({ status: 200, data: { ok: true, check: { ok: true, at: null, reason: null, min_clearance: 0.47, soft_risk: true, distance: 6, goal: { x: 1, y: 8, yaw: 1.57 } } } });
    $('navstraight-go').click(); await settle();
    check('19p Straight: แนวชิดผนัง (soft_risk) → มีคำเตือนว่าอาจไม่ตรงเป๊ะเพิ่มในหน้าต่างยืนยัน', $('confirm-list').children.length === 3 && $('confirm-list').children[1].textContent === E['confirm.straight.soft'], $('confirm-list').children.map((c) => c.textContent).join(' | '));
    $('confirm-cancel').click(); await settle();
    server.straightCheck = null;

    $('navstraight-go').click(); await settle();
    $('confirm-ok').click(); await settle();
    check('19p Straight: ยืนยัน → POST {distance, confirmed:true} พร้อม CSRF ครั้งเดียว + toast + สถานะกำลังเดินทาง + ปุ่มยกเลิกเดินทางโผล่',
      sends().length === 1 && sends()[0].body.distance === 6 && sends()[0].body.confirmed === true && sends()[0].headers['X-Requested-With'] === 'agv-web' &&
      $('toast').textContent === E['toast.navgoal_sent'] && $('navgoal-status').textContent === '▲ ' + E['navgoal.st.active'] && $('navgoal-stop').hidden === false && $('navstraight-go').disabled === true,
      JSON.stringify(sends()[0] && sends()[0].body) + ' ' + $('toast').textContent);

    // ── มี goal ค้าง: เดินตรงซ้อนไม่ได้ ──
    const c1 = checks().length;
    $('navstraight-go').click(); await settle();
    check('19p Straight: มี goal ค้างอยู่ → กดไม่ได้/ไม่เรียก API', checks().length === c1);
    server.goalGet = goalOf('succeeded');
    await advance(1100); await settle();
    server.goalGet = null;
    check('19p Straight: goal จบแล้ว → ปุ่มกลับมาใช้ได้', $('navstraight-go').disabled === false);

    // ── ส่งไม่สำเร็จ ──
    server.straightHandler = () => ({ status: 409, data: { ok: false, error: M('nav.err.straight_tight', { at: 4.0, clearance: 0.4 }) } });
    $('navstraight-go').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19p Straight: เซิร์ฟเวอร์ปฏิเสธตอนส่ง → toast เหตุผล + ปุ่มกลับมา', $('toast').textContent === 'The line gets closer than 0.4 m to a wall 4 m ahead' && $('navstraight-go').disabled === false, $('toast').textContent);
    server.straightHandler = null;

    // ── กดซ้ำระหว่างรอตรวจ ──
    const c2 = checks().length;
    server.holdPath = '/api/nav/goal/straight/check';
    $('navstraight-go').click(); await flush();
    $('navstraight-go').click(); await flush();
    check('19p Straight: กดซ้ำระหว่างรอตรวจ → เรียกตรวจครั้งเดียว + ปุ่มปิด', checks().length === c2 + 1 && $('navstraight-go').disabled === true);
    server.releaseHeld(); server.holdPath = null; await settle();
    $('confirm-cancel').click(); await settle();

    // ── ทีละเครื่องมือ ──
    $('navgoal-start').click(); await flush();
    check('19p Straight: กำลังเลือกจุดหมายบนแผนที่ → ปุ่มเดินตรงปิด', $('navstraight-go').disabled === true);
    $('navgoal-cancel').click(); await flush();
    $('navpose-start').click(); await flush();
    check('19p Straight: กำลังวางตำแหน่งหุ่น → ปุ่มเดินตรงปิด', $('navstraight-go').disabled === true);
    $('navpose-cancel').click(); await flush();

    // ── สลับภาษา ──
    langBtn('th').click(); await flush();
    check('19p Straight: สลับภาษา → ป้าย/ปุ่ม/คำอธิบายเป็นไทย', $('navstraight-go').textContent === T['ui.navstraight.go'] && $('navstraight-help').textContent === T['ui.navstraight.help'], $('navstraight-go').textContent);
    langBtn('en').click(); await flush();

    server.map = null; server.straightCheck = null; server.straightHandler = null; server.goalGet = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19r) หน้า Waypoint: สร้าง/แก้ Route (ลำดับจุดหมาย) ต่อแผนที่ ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const wcv = $('wp-map-canvas');
    const down = (id, x, y) => wcv.dispatch('pointerdown', { pointerId: id, clientX: x, clientY: y });
    const move = (id, x, y) => wcv.dispatch('pointermove', { pointerId: id, clientX: x, clientY: y });
    const up = (id) => wcv.dispatch('pointerup', { pointerId: id });
    const SC = Math.min(320 / 13.2, 272 / 11);                // สเกล "พอดีจอ" ของ MAPMETA (12 × 10 m) บน canvas 320 × 272
    const CX = -3.5 + 6, CY = -5 + 5;
    const sx = (x) => 160 + (x - CX) * SC, sy = (y) => 136 - (y - CY) * SC;
    const LIMITS = { max_points: 50, max_name: 40, max_wait: 600, max_loop: 99, clearance: 0.4 };
    const P = (name, x, y, yaw = 0, wait = 0) => ({ name, x, y, yaw, wait });
    const mkRoutes = () => ({ Delivery: { loop: 3, saved: 1, points: [P('Dock', 1.0, 1.0, 0, 5), P('Station', 3.0, 2.0, 1.5, 10)] },
      Return: { loop: 1, saved: 1, points: [P('Hall', 0.0, 0.0)] } });
    const info = (routes, over = {}) => ({ status: 200, data: Object.assign({ map: 'lab', routes, running: false, map_ready: true, limits: LIMITS }, over) });
    const posts = (path = '/api/routes') => server.calls.filter((c) => c.method === 'POST' && c.path === path);
    const rows = () => $('wp-points').children;
    const rowText = (i) => rows()[i].children[0].textContent;
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 }, localization: { quality: 'good', sigma: 0.1 } };
    const tap = async (x, y, id = 1) => { down(id, sx(x), sy(y)); up(id); await settle(); };
    const routeBtn = (name) => $('wp-routes').children.find((b) => b.children[0].textContent === name);
    const openRoute = async (name) => { routeBtn(name).click(); await settle(); };
    const pickTool = async (n) => { $('wp-tool-' + n).click(); await flush(); };

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(70) };
    server.routes = info({}, { map: null, map_ready: false });         // เริ่มโดยยังไม่มีแผนที่ที่บันทึก/เลือกไว้
    const calls0 = server.calls.length;
    await asAdmin(true);
    await send(modeOf('STANDBY'));
    nav('#/waypoint'); await settle();

    // ── ยังไม่มีแผนที่ที่บันทึกไว้ให้ใช้ → เกตบอกวิธี (ไม่ต้องรัน Navigation) ──
    check('19r Wp: เห็นหน้า waypoint + แท็บ active', secOf('waypoint').hidden === false && hasClass($('tab-waypoint'), 'active'));
    check('19r Wp: ไม่มีแผนที่ที่เลือกไว้ → เกตบอกให้บันทึก/เลือกแผนที่ + ปุ่มสร้างปิด + บอกเหตุผล', $('wp-map-gate').hidden === false && $('wp-map-live').hidden === true &&
      $('wp-map-gate-msg').textContent === E['wp.gate.no_map'] && $('wp-new').disabled === true && $('wp-left-note').textContent === E['wp.need_map'] &&
      $('wp-map-name').textContent === E['wp.no_map'], $('wp-map-gate-msg').textContent);
    server.routes = info(mkRoutes(), { map_ready: false });
    await advance(4100); await settle();
    check('19r Wp: แผนที่ที่เลือกอ่านไม่ได้ → เกตบอกชื่อแผนที่ + ปุ่มสร้างปิด (ยังเห็นรายการ Route เดิม)', $('wp-map-gate').hidden === false && $('wp-new').disabled === true &&
      $('wp-map-gate-msg').textContent === 'The saved map “lab” could not be read. Check the map files or save the map again.' && $('wp-routes').children.length === 2, $('wp-map-gate-msg').textContent);
    server.routes = info(mkRoutes());
    await advance(4100); await settle();

    // ── Navigation ไม่ต้องรัน : มีแผนที่ที่บันทึกไว้ก็แก้ได้เลย ──
    check('19r Wp: Navigation ไม่รัน แต่มีแผนที่ที่บันทึกไว้ → แสดงแผนที่ + ปุ่มสร้างเปิด + ไม่มีข้อความเตือน', $('wp-map-live').hidden === false && $('wp-map-gate').hidden === true &&
      $('wp-new').disabled === false && $('wp-left-note').hidden === true && $('wp-form').hidden === true && $('wp-help').textContent === E['wp.help.pick'] && $('wp-map-name').textContent === 'Map: lab', $('wp-help').textContent);
    check('19r Wp: ดึงแผนที่จาก /api/routes/map (ไฟล์ที่บันทึกไว้) ไม่ใช่ /api/map/live', server.calls.slice(calls0).some((c) => c.path === '/api/routes/map') && !server.calls.slice(calls0).some((c) => c.path === '/api/map/live') &&
      images.some((im) => /^\/api\/routes\/map\.png\?v=/.test(im.src)), JSON.stringify(images.map((im) => im.src).slice(-2)));
    check('19r Wp: แสดงรายการ Route + ชื่อแผนที่', $('wp-routes').children.length === 2 && routeBtn('Delivery').children[1].textContent === '2 points · 3 rounds' &&
      routeBtn('Return').children[1].textContent === '1 point · once', routeBtn('Delivery').children[1].textContent);
    check('19r Wp: เรียก GET /api/routes ตอนเข้าหน้า', server.calls.some((c) => c.path === '/api/routes' && c.method === 'GET'));
    check('19r Wp: ยังไม่เลือก Route → ปุ่มเครื่องมือแผนที่ปิด', ['select', 'add', 'delete'].every((n) => $('wp-tool-' + n).disabled === true));
    check('19r Wp: ปุ่ม Robot ปิด (หุ่นไม่ได้อยู่บนแผนที่นี้เมื่อ Navigation ไม่รัน)', $('wp-center').disabled === true);
    await asAdmin(false);
    check('19r Wp: ผู้ใช้ทั่วไปดูแผนที่/รายการได้ แต่สร้างไม่ได้ + บอกให้ล็อกอิน', $('wp-map-live').hidden === false && $('wp-routes').children.length === 2 && $('wp-new').disabled === true &&
      $('wp-left-note').textContent === E['wp.need_admin'], $('wp-left-note').textContent);
    await asAdmin(true);

    // ── เปิด Route ที่มีอยู่ ──
    await openRoute('Delivery');
    check('19r Wp: เปิด Route → ฟอร์ม + ชื่อ + จำนวนรอบ + 2 จุด + สถานะบันทึกแล้ว', $('wp-form').hidden === false && $('wp-name').value === 'Delivery' && $('wp-loop').value === '3' &&
      rows().length === 2 && $('wp-status').textContent === '✔ Saved' && $('wp-editor-title').textContent === 'Delivery · 2 points', $('wp-status').textContent);
    check('19r Wp: แถวจุด = เลขลำดับ + ชื่อ + เวลารอ + พิกัด ; จุดแรกถูกเลือก', rowText(0) === '1Dockwait 5 s · x 1.00, y 1.00' && rows()[0].attrs['data-selected'] === 'true' && rows()[1].attrs['data-selected'] === 'false', rowText(0));
    check('19r Wp: การ์ดจุดที่เลือก + readout (ชื่อ/รอ/x/y/ทิศ)', $('wp-detail').hidden === false && $('wp-pname').value === 'Dock' && $('wp-pwait').value === '5' && $('wp-px').value === '1.00' &&
      $('wp-readout').textContent === 'Point 1: x 1.00 m, y 1.00 m, heading 0°', $('wp-readout').textContent);
    check('19r Wp: Save/Discard ปิด (ยังไม่ได้แก้) ; Delete route เปิด ; ▲ จุดแรกปิด ▼ จุดท้ายปิด', $('wp-save').disabled === true && $('wp-discard').disabled === true && $('wp-delete').disabled === false &&
      rows()[0].children[1].disabled === true && rows()[0].children[2].disabled === false && rows()[1].children[2].disabled === true);
    check('19r Wp: เส้นทางถูกวาดบนแผนที่ (2 จุด = 2 วงกลม + ชื่อ)', (() => { const log = wcv.ctxLog; const k = log.map((c) => c.name).lastIndexOf('clearRect'); const part = log.slice(k);
      return part.filter((c) => c.name === 'arc').length === 2 && part.filter((c) => c.name === 'fillText').map((c) => c.args[0]).join('|').includes('Dock'); })());

    // ── เพิ่มจุด : แตะ = ตำแหน่ง (ทิศตั้งต้น = ทางที่เดินมาจากจุดก่อนหน้า) ──
    await pickTool('add');
    check('19r Wp: เลือกเครื่องมือเพิ่มจุด → pressed + canvas เปลี่ยนเคอร์เซอร์ + คำแนะนำ', $('wp-tool-add').attrs['aria-pressed'] === 'true' && $('wp-tool-select').attrs['aria-pressed'] === 'false' &&
      hasClass(wcv, 'wp-add') && $('wp-help').textContent === E['wp.help.add']);
    await tap(5.0, 2.0);
    check('19r Wp: แตะแผนที่ → เพิ่มจุดที่ 3 ที่ตำแหน่งนั้น + เลือกอัตโนมัติ + บอกว่ายังไม่บันทึก + Save เปิด',
      rows().length === 3 && rows()[2].attrs['data-selected'] === 'true' && Math.abs(Number($('wp-px').value) - 5.0) < 0.02 && Math.abs(Number($('wp-py').value) - 2.0) < 0.02 &&
      $('wp-status').textContent === '▲ Unsaved changes' && $('wp-save').disabled === false && $('wp-discard').disabled === false, $('wp-px').value + ',' + $('wp-py').value + ' ' + $('wp-status').textContent);
    check('19r Wp: จุดใหม่ (Route วนรอบ ×3) เป็นทิศอัตโนมัติ: จุดสุดท้ายหันไปหาจุดแรก (5,2)→(1,1) = −166° + ล็อกช่องทิศ + ชื่อตั้งต้น', $('wp-pyaw').value === '-166' && $('wp-pauto').checked === true && $('wp-pyaw').disabled === true && $('wp-pname').value === 'Point 3', $('wp-pyaw').value + ' ' + $('wp-pname').value);
    await tap(6.0, 4.0);
    check('19r Wp: เพิ่มจุดที่ 4 → จุดที่ 4 (ท้าย) หันไปหาจุดแรก ≈ −149° และจุดที่ 3 เปลี่ยนไปหันหาจุดที่ 4 ≈ 63° เอง', rows().length === 4 && Number($('wp-pyaw').value) === -149 && (() => { rows()[2].children[0].click(); return Number($('wp-pyaw').value) === 63; })(), $('wp-pyaw').value);
    rows()[3].children[0].click(); await flush();

    // ลาก = ชี้ทิศ
    down(1, sx(6.5), sy(1.0)); move(1, sx(6.5), sy(1.0) - 40); up(1); await settle();
    check('19r Wp: แตะแล้วลากขึ้น → วางจุดที่ 5 และหันทิศ 90° (ขึ้น)', rows().length === 5 && $('wp-pyaw').value === '90' && Math.abs(Number($('wp-px').value) - 6.5) < 0.02, $('wp-pyaw').value);

    // ซูมสองนิ้ว/ pointercancel ไม่ควรเพิ่มจุด
    const n0 = rows().length;
    down(1, 90, 90); down(2, 220, 90); move(2, 280, 90); up(1); up(2); await settle();
    wcv.dispatch('pointerdown', { pointerId: 3, clientX: 90, clientY: 90 }); wcv.dispatch('pointercancel', { pointerId: 3, type: 'pointercancel' }); await settle();
    down(1, 90, 90); down(2, 220, 90); up(2); up(1); await settle();
    check('19r Wp: ซูมสองนิ้ว / pointercancel / แตะสองนิ้ว → ไม่เพิ่มจุดโดยบังเอิญ', rows().length === n0, rows().length);
    $('wp-fit').click(); await flush();

    // ── แก้จุดที่เลือก ──
    $('wp-pname').value = 'Charging'; $('wp-pname').dispatch('input'); await flush();
    check('19r Wp: แก้ชื่อจุด → รายการเปลี่ยนตามทันที', rowText(4).startsWith('5Charging'), rowText(4));
    $('wp-pwait').value = '999'; $('wp-pwait').dispatch('input'); await flush();
    check('19r Wp: เวลารอเกินเพดาน (600) → ถูกจำกัดไว้', rowText(4).includes('wait 600 s'), rowText(4));
    $('wp-pwait').value = 'abc'; $('wp-pwait').dispatch('input'); await flush();
    check('19r Wp: พิมพ์ไม่ใช่ตัวเลข → ไม่แตะร่าง', rowText(4).includes('wait 600 s'));
    $('wp-pyaw').value = '180'; $('wp-pyaw').dispatch('input'); await flush();
    check('19r Wp: แก้ทิศเป็น 180°', $('wp-readout').textContent.endsWith('heading 180°'), $('wp-readout').textContent);
    $('wp-rot-l').click(); await flush();
    check('19r Wp: ปุ่มหมุน ⟲ 15° → 195° หรือ −165° (หมุนทวนเข็ม)', $('wp-readout').textContent.endsWith('heading -165°') || $('wp-readout').textContent.endsWith('heading 195°'), $('wp-readout').textContent);
    $('wp-px').value = '6.25'; $('wp-px').dispatch('input'); await flush();
    check('19r Wp: แก้ x ด้วยช่องกรอก', rowText(4).includes('x 6.25'), rowText(4));

    // ── เรียงลำดับ / ลบด้วยเครื่องมือลบ ──
    rows()[4].children[1].click(); await settle();   // ▲ จุดที่ 5 ขึ้น
    check('19r Wp: ▲ → สลับกับจุดก่อนหน้า + ตามไปเลือกที่ตำแหน่งใหม่', rowText(3).startsWith('4Charging') && rows()[3].attrs['data-selected'] === 'true', rowText(3));
    rows()[3].children[2].click(); await settle();   // ▼ กลับ
    check('19r Wp: ▼ → กลับที่เดิม', rowText(4).startsWith('5Charging'));
    await pickTool('delete');
    check('19r Wp: โหมดลบ → คำแนะนำ + เคอร์เซอร์', $('wp-help').textContent === E['wp.help.delete'] && hasClass(wcv, 'wp-delete'));
    await tap(6.0, 4.0);   // จุดที่ 4
    check('19r Wp: แตะจุดในโหมดลบ → จุดนั้นหาย (เหลือ 4)', rows().length === 4 && !rows().some((r) => r.children[0].textContent.includes('Point 4')), rows().length);
    await tap(3.9, 3.9);   // ที่ว่าง
    check('19r Wp: แตะที่ว่างในโหมดลบ → ไม่ทำอะไร', rows().length === 4);

    // ── เลือก / ย้าย ──
    await pickTool('select');
    await tap(1.0, 1.0);
    check('19r Wp: แตะจุดในโหมดเลือก → เลือกจุดนั้น (ไม่แก้ข้อมูล)', rows()[0].attrs['data-selected'] === 'true' && $('wp-pname').value === 'Dock', $('wp-pname').value);
    down(1, sx(1.0), sy(1.0)); move(1, sx(1.0) + 3, sy(1.0)); up(1); await settle();
    check('19r Wp: ขยับนิ้วนิดเดียว (< 6 px) → ไม่ย้ายจุด', $('wp-px').value === '1.00', $('wp-px').value);
    down(1, sx(1.0), sy(1.0)); move(1, sx(1.0) + 48.5, sy(1.0)); move(1, sx(1.0) + 48.5, sy(1.0) - 24.2); up(1); await settle();
    check('19r Wp: ลากจุดที่เลือก → พิกัดเปลี่ยนตามนิ้ว (+2 m, +1 m) และแผนที่ไม่เลื่อน', Math.abs(Number($('wp-px').value) - 3.0) < 0.1 && Math.abs(Number($('wp-py').value) - 2.0) < 0.1, $('wp-px').value + ',' + $('wp-py').value);
    const before = (wcv.ctxLog || []).length;
    down(1, sx(4.5), sy(-3.0)); move(1, sx(4.5) + 30, sy(-3.0)); up(1); await settle();
    check('19r Wp: ลากที่ว่างในโหมดเลือก → เลื่อนแผนที่ตามปกติ (ไม่ย้ายจุด)', rows().length === 4 && $('wp-px').value !== '' && (wcv.ctxLog || []).length > before);
    $('wp-fit').click(); await flush();
    wcv.dispatch('keydown', { key: 'Delete', preventDefault() {} }); await settle();
    check('19r Wp: ปุ่ม Delete บนคีย์บอร์ดลบจุดที่เลือก (จุด Dock ที่เพิ่งลากไป)', rows().length === 3 && !rows().some((r) => r.children[0].textContent.includes('Dock')), rows().length);

    // ── บันทึก ──
    $('wp-loop').value = '0'; $('wp-loop').dispatch('change'); await flush();
    const keep = (res) => { server.routes = { status: 200, data: { ...res.data, ok: undefined } }; return res; };      // เหมือนเซิร์ฟเวอร์จริง: บันทึกแล้ว GET เห็นค่าใหม่
    server.routesHandler = (b) => keep(info({ ...mkRoutes(), Delivery: { loop: b.loop, saved: 2, points: b.points } }, { ok: true }));
    $('wp-save').click(); $('wp-save').click(); await settle();
    const sv = posts();
    check('19r Wp: Save → POST ครั้งเดียว (คลิกซ้ำไม่ซ้อน) + CSRF + ชื่อ/ชื่อเดิม/รอบ/จุดครบ', sv.length === 1 && sv[0].headers['X-Requested-With'] === 'agv-web' && sv[0].body.name === 'Delivery' &&
      sv[0].body.previous === 'Delivery' && sv[0].body.loop === 0 && sv[0].body.points.length === 3 && sv[0].body.points[0].name === 'Station', JSON.stringify(sv[0] && sv[0].body));
    check('19r Wp: บันทึกสำเร็จ → toast + สถานะบันทึกแล้ว + Save ปิด + ไม่มี error', $('toast').textContent === E['toast.wp_saved'] && $('wp-status').textContent === '✔ Saved' &&
      $('wp-save').disabled === true && $('wp-error').hidden === true, $('toast').textContent);
    check('19r Wp: รายการซ้ายอัปเดตเป็น "3 points · repeat forever"', routeBtn('Delivery').children[1].textContent === '3 points · repeat forever', routeBtn('Delivery').children[1].textContent);

    // ── เซิร์ฟเวอร์ปฏิเสธ : ชี้จุดที่ไม่ผ่านบนแผนที่ + ล้างเมื่อแก้ ──
    await tap(0.0, 0.0, 1);   // โหมดเลือก: แตะที่ว่าง ไม่เพิ่ม
    await pickTool('add'); await tap(4.0, 4.0);
    server.routesHandler = () => ({ status: 400, data: { ok: false, error: M('route.err.point_tight', { n: 2, clearance: 0.4, found: 0.1 }) } });
    $('wp-save').click(); await settle();
    check('19r Wp: เซิร์ฟเวอร์ปฏิเสธ → แสดงเหตุผลที่แปลแล้ว + ยังไม่ถือว่าบันทึก', $('wp-error').hidden === false && $('wp-error').textContent === 'Point 2 is too close to a wall or obstacle (needs 0.4 m).' &&
      $('wp-status').textContent.startsWith('▲ Unsaved') && $('wp-save').disabled === false, $('wp-error').textContent);
    check('19r Wp: จุดที่ 2 ถูกทำเครื่องหมายไม่ผ่าน (✖ ไม่พึ่งสีอย่างเดียว) + เลือกให้ดู', rows()[1].attrs['data-bad'] === 'true' && rows()[1].children[0].children[0].textContent === '✖' && rows()[1].attrs['data-selected'] === 'true');
    $('wp-px').value = '2.5'; $('wp-px').dispatch('input'); await flush();
    check('19r Wp: แก้ร่างแล้ว → เครื่องหมายไม่ผ่านและข้อความเก่าหายไป', rows()[1].attrs['data-bad'] === 'false' && $('wp-error').hidden === true);
    server.routesHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('wp-save').click(); await settle();
    check('19r Wp: เซสชันหมดอายุตอน Save → เด้งขอ PIN ; ร่างไม่หาย', $('pin-dialog').open === true && rows().length === 4);
    $('pin-dialog').close(); await asAdmin(true); await settle();

    // ── ทิ้งการแก้ไข (ถามก่อน) ──
    $('wp-discard').click(); await settle();
    check('19r Wp: Discard → ถามยืนยันก่อน', $('confirm-dialog').open === true && $('confirm-title').textContent === E['wp.discard.title']);
    $('confirm-cancel').click(); await settle();
    check('19r Wp: ตอบยกเลิก → ร่างยังอยู่', rows().length === 4 && $('wp-save').disabled === false);
    $('wp-discard').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19r Wp: ยืนยันทิ้ง → กลับเป็น Route ที่บันทึกไว้ (3 จุด) + บันทึกแล้ว', rows().length === 3 && $('wp-status').textContent === '✔ Saved' && $('wp-discard').disabled === true, rows().length);

    // ── เปลี่ยนชื่อ = ส่ง previous ──
    $('wp-name').value = 'Delivery 2'; $('wp-name').dispatch('input'); await flush();
    server.routesHandler = (b) => keep(info({ Return: mkRoutes().Return, [b.name]: { loop: b.loop, saved: 3, points: b.points } }, { ok: true }));
    $('wp-save').click(); await settle();
    const rn = posts().pop();
    check('19r Wp: เปลี่ยนชื่อ → POST name ใหม่ + previous = ชื่อเดิม ; รายการซ้ายเปลี่ยน ; เปิด Route ใหม่ต่อ',
      rn.body.name === 'Delivery 2' && rn.body.previous === 'Delivery' && !routeBtn('Delivery') && !!routeBtn('Delivery 2') && $('wp-name').value === 'Delivery 2' &&
      routeBtn('Delivery 2').attrs['aria-current'] === 'true');

    // ── Route ใหม่ ──
    $('wp-new').click(); await settle();
    check('19r Wp: Route ใหม่ → ฟอร์มว่าง + ยังไม่มีจุด + บอกให้เพิ่มจุด + Save ปิด + โฟกัสช่องชื่อ',
      $('wp-form').hidden === false && $('wp-name').value === '' && rows().length === 0 && $('wp-points-empty').hidden === false && $('wp-save').disabled === true && $('wp-name').focused === true &&
      $('wp-editor-title').textContent === 'New route · 0 points' && $('wp-status').textContent === E['wp.issue.name'], $('wp-status').textContent);
    await tap(2.0, 2.0);
    check('19r Wp: เพิ่มจุดแล้วแต่ยังไม่ตั้งชื่อ → Save ปิด + บอกเหตุผล', rows().length === 1 && $('wp-save').disabled === true && $('wp-status').textContent === '▲ Unsaved changes — ' + E['wp.issue.name'], $('wp-status').textContent);
    $('wp-name').value = '  Night run  '; $('wp-name').dispatch('input'); await flush();
    server.routesHandler = (b) => keep(info({ Return: mkRoutes().Return, 'Night run': { loop: b.loop, saved: 4, points: b.points } }, { ok: true }));
    $('wp-save').click(); await settle();
    const nw = posts().pop();
    check('19r Wp: บันทึก Route ใหม่ → previous = null ; เปิด Route ที่บันทึกต่อ (ชื่อ "Night run" ที่เซิร์ฟเวอร์ล้างช่องว่างแล้ว) + คำเอกพจน์ "1 point"', nw.body.previous === null && $('wp-status').textContent === '✔ Saved' && $('wp-editor-title').textContent === 'Night run · 1 point', $('wp-editor-title').textContent);

    // ── ลบ Route ──
    server.routesDelete = (b) => keep(info({ Return: mkRoutes().Return }, { ok: true }));
    $('wp-delete').click(); await settle();
    check('19r Wp: ลบ Route → ถามยืนยัน (เตือน) พร้อมชื่อ', $('confirm-dialog').open === true && $('confirm-title').textContent === E['wp.del.title'] && /“.+” will be deleted from this map/.test($('confirm-msg').textContent), $('confirm-msg').textContent);
    $('confirm-cancel').click(); await settle();
    check('19r Wp: ตอบยกเลิก → ไม่ส่งคำขอลบ', posts('/api/routes/delete').length === 0);
    $('wp-delete').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19r Wp: ยืนยันลบ → POST /api/routes/delete + รายการเหลือ 1 + ฟอร์มปิด + toast', posts('/api/routes/delete').length === 1 && $('wp-routes').children.length === 1 && $('wp-form').hidden === true &&
      $('toast').textContent === E['toast.wp_deleted'], $('toast').textContent);

    // ── สิทธิ์ ──
    await openRoute('Return');
    await asAdmin(false);
    check('19r Wp: ไม่ใช่ผู้ดูแล → ดู Route ได้ แต่ทุกช่อง/ปุ่มแก้ไขปิด + บอกให้ล็อกอิน', $('wp-form').hidden === false && ['wp-name', 'wp-loop', 'wp-pname', 'wp-pwait', 'wp-px', 'wp-py', 'wp-pyaw', 'wp-rot-l', 'wp-del-point', 'wp-save', 'wp-delete', 'wp-new']
      .every((id) => $(id).disabled === true) && $('wp-note').textContent === E['wp.need_admin'] && $('wp-help').textContent === E['wp.help.view'] &&
      rows()[0].children[1].disabled === true && rows()[0].children[2].disabled === true, $('wp-note').textContent);
    const nPosts = server.calls.length;
    await tap(3.0, 3.0);
    check('19r Wp: ไม่ใช่ผู้ดูแล → แตะแผนที่ไม่เพิ่มจุด ไม่ส่งอะไร', rows().length === 1 && server.calls.slice(nPosts).every((c) => c.method === 'GET'));
    await asAdmin(true);

    // ── ทิ้งร่างเมื่อแผนที่เปลี่ยน ──
    await openRoute('Return');
    await pickTool('add'); await tap(2.0, 2.0);
    check('19r Wp: มีร่างค้าง (ยังไม่บันทึก)', $('wp-status').textContent.startsWith('▲ Unsaved'));
    server.routes = info({ Other: { loop: 1, saved: 1, points: [P('X', 1, 1)] } }, { map: 'hall' });
    await advance(4100); await settle();
    check('19r Wp: แผนที่เปลี่ยน → ทิ้งร่างที่ค้าง + toast + แสดงรายการของแผนที่ใหม่', $('wp-form').hidden === true && $('toast').textContent === E['toast.wp_map_changed'] &&
      $('wp-map-name').textContent === 'Map: hall' && !!routeBtn('Other'), $('toast').textContent);
    server.routes = info(mkRoutes());
    await advance(4100); await settle();

    // ── Navigation เริ่ม/หยุดกลางทาง : ไม่กระทบการแก้ Route ; หุ่นโผล่บนแผนที่เฉพาะตอน Nav รัน ──
    await openRoute('Delivery');
    const fills = () => { const log = wcv.ctxLog; const k = log.map((c) => c.name).lastIndexOf('clearRect'); return log.slice(k).filter((c) => c.name === 'fill').length; };
    await send({ ...modeOf('STANDBY'), pose: { x: 1.0, y: 2.0, yaw_deg: 90 } }); await settle();
    check('19r Wp: Navigation ไม่รัน (มี pose จากโหมดอื่น) → ไม่วาดหุ่น (วาดเฉพาะจุดของ Route 2 จุด) + ยังแก้ได้', fills() === 2 && $('wp-map-live').hidden === false && $('wp-tool-add').disabled === false && $('wp-center').disabled === true, fills());
    await send(okState); await advance(3100); await settle();
    check('19r Wp: Navigation รันนิ่ง → วาดหุ่นเพิ่ม (จุด 2 + หุ่น 1) + ปุ่ม Robot เปิด', fills() === 3 && $('wp-center').disabled === false && $('wp-tool-add').disabled === false, fills());
    await pickTool('add'); await tap(5.0, 2.0);
    await send(modeOf('STANDBY')); await settle();
    check('19r Wp: Navigation หยุดกลางทาง → ร่างที่แก้ค้างอยู่ไม่หาย + ยังบันทึกได้ (ตรวจกับไฟล์แผนที่)', rows().length === 3 && $('wp-save').disabled === false && $('wp-map-live').hidden === false && $('wp-status').textContent.startsWith('▲ Unsaved'), $('wp-status').textContent);
    $('wp-discard').click(); await settle(); $('confirm-ok').click(); await settle();
    await send(okState); await advance(3100); await settle();

    // ═════ รัน Route (แถบล่าง) : เริ่ม/พัก/ทำต่อ/หยุด + สถานะ + ความคืบหน้า + ล็อกการแก้ไข ═════
    const mkRun = (over = {}) => Object.assign({ state: 'idle', run: 0, route: null, map: 'lab', loop: 1, round: 0, index: 0, total: 0, current: null, phase: null,
      wait_left: null, elapsed: null, reason: null, max_run: 7200, point_timeout: 600, pause_settled: true }, over);
    const GOAL0 = { state: 'idle', seq: 0, goal: null, reason: null, feedback: null, elapsed: null, waiting_assumed: false };
    const runResp = (run, goal = GOAL0, extra = {}) => ({ status: 200, data: { run, goal, running: true, ...extra } });
    const running = (over = {}) => mkRun({ state: 'running', run: 1, route: 'Delivery', loop: 3, round: 1, index: 0, total: 2, phase: 'going', current: { name: 'Dock', x: 1, y: 1, yaw: 0, wait: 5 }, elapsed: 4, ...over });
    const runPosts = (suffix = '') => server.calls.filter((c) => c.method === 'POST' && c.path === '/api/routes/run' + suffix);
    const pollRunNow = async () => { await advance(1100); await settle(); };
    const alphaSet = (v) => (wcv.ctxLog || []).some((c) => c.name === 'set:globalAlpha' && c.args[0] === v);
    server.runGet = runResp(mkRun());
    await openRoute('Delivery');
    await pollRunNow();
    check('19s Run: ไม่มี Route รัน → สถานะ "No route is running." + มีปุ่มเริ่ม (พร้อมใช้) ; ไม่มีปุ่มพัก/หยุด/แถบคืบหน้า', $('run-status').textContent === E['run.st.idle'] &&
      $('run-start').hidden === false && $('run-start').disabled === false && $('run-pause').hidden === true && $('run-stop').hidden === true && $('run-progress').hidden === true && $('run-note').hidden === true, $('run-status').textContent + ' | ' + $('run-note').textContent);
    check('19s Run: โพลสถานะ GET /api/routes/run ตอนอยู่หน้า Waypoint', server.calls.some((c) => c.path === '/api/routes/run' && c.method === 'GET'));
    check('19s Run: ป้ายปุ่มเป็นข้อความที่แปลแล้ว', $('run-start').textContent === E['ui.run.run'] && E['ui.run.run'] === '▶ Run route');

    // ── เหตุผลที่เริ่มไม่ได้ (ปุ่มปิด + บอกเหตุผลตามลำดับความสำคัญ) ──
    await asAdmin(false); await settle();
    check('19s Run: ไม่ใช่ผู้ดูแล → ปุ่มปิด + บอกให้ล็อกอิน', $('run-start').disabled === true && $('run-note').textContent === E['run.need_admin'], $('run-note').textContent);
    await asAdmin(true); await settle();
    await send(modeOf('STANDBY')); await settle();
    check('19s Run: Navigation ไม่รัน → ปุ่มปิด + บอกให้เปิด Navigation', $('run-start').disabled === true && $('run-note').textContent === E['run.need_nav'], $('run-note').textContent);
    await send(okState); await advance(3100); await settle();
    $('wp-name').value = 'Delivery changed'; $('wp-name').dispatch('input'); await settle();
    check('19s Run: มีการแก้ที่ยังไม่บันทึก → ปุ่มปิด + บอกให้บันทึกก่อน', $('run-start').disabled === true && $('run-note').textContent === E['run.unsaved'], $('run-note').textContent);
    $('wp-discard').click(); await settle(); $('confirm-ok').click(); await settle();
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 } }); await settle();
    check('19s Run: หุ่นยังไม่ localize → ปุ่มปิด + บอกเหตุผลเดียวกับการส่ง goal', $('run-start').disabled === true && $('run-note').textContent === E['navgoal.need_loc'], $('run-note').textContent);
    await send(okState); await settle();
    server.goalGet = { status: 200, data: { running: true, goal: { state: 'active', seq: 5, goal: { x: 1, y: 1, yaw: 0 }, reason: null, feedback: null, elapsed: 3, waiting_assumed: false } } };
    nav('#/nav'); await advance(1100); await settle(); nav('#/waypoint'); await settle(); await advance(1100); await settle();
    check('19s Run: หุ่นกำลังเดินทาง (goal เดี่ยว) → ปุ่มปิด + บอกให้ยกเลิกก่อน', $('run-start').disabled === true && $('run-note').textContent === E['run.busy_goal'], $('run-note').textContent);
    server.goalGet = null; nav('#/nav'); await advance(1100); await settle(); nav('#/waypoint'); await settle(); await advance(1100); await settle();
    check('19s Run: ครบเงื่อนไข → ปุ่มเริ่มกดได้อีกครั้ง', $('run-start').disabled === false && $('run-note').hidden === true, $('run-note').textContent);

    // ── เซิร์ฟเวอร์ยอมรอตำแหน่งฟื้น (loc_grace > 0) : ไม่ localize ก็เริ่มได้ ระบบปรับตำแหน่งเอง ──
    server.runGet = runResp(mkRun({ loc_grace: 15 }));
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 } }); await pollRunNow();
    check('19s Run: ตำแหน่งไม่แน่นอนแต่เซิร์ฟเวอร์ยอมรอ → ปุ่มเริ่มกดได้ + บอกว่าระบบจะปรับตำแหน่งเองก่อน (ไม่ต้องกด Refresh)', $('run-start').disabled === false &&
      $('run-note').textContent === 'The robot’s position is uncertain. It will try to improve it by itself first (up to 15 s) before moving.', $('run-note').textContent + ' | ' + $('run-start').disabled);
    server.runGet = runResp(running({ phase: 'locating', loc_left: 11.2, loc_grace: 15 }));
    await pollRunNow();
    check('19s Run: ช่วงรอตำแหน่ง → สถานะ "Checking the robot’s position before point 1/2 — up to 12 s" (ปัดขึ้น) ไม่แสดงระยะเหลือ', $('run-status').textContent === 'Checking the robot’s position before point 1/2 — up to 12 s · round 1/3' &&
      $('run-detail').hidden === true && $('run-stop').hidden === false, $('run-status').textContent);
    server.runGet = runResp(mkRun({ loc_grace: 0 }));
    await pollRunNow();
    check('19s Run: เซิร์ฟเวอร์ไม่ยอมรอ (grace 0) → ปุ่มเริ่มปิด + บอกให้วางตำแหน่งก่อน (เหมือนเดิม)', $('run-start').disabled === true && $('run-note').textContent === E['navgoal.need_loc'], $('run-note').textContent);
    server.runGet = runResp(mkRun({ loc_grace: 15 }));
    await send(okState); await pollRunNow();

    // ── เริ่ม: ถามยืนยันทุกครั้ง ──
    $('run-start').click(); await settle();
    check('19s Run: กดเริ่ม → หน้าต่างยืนยันแบบเตือน มีชื่อ Route/จำนวนจุด/รอบ + เพดานเวลา + เตือนความปลอดภัย', $('confirm-dialog').open === true && $('confirm-title').textContent === E['run.confirm.title'] &&
      $('confirm-list').children.map((c) => c.textContent).join('|') === '“Delivery”: 2 points · 3 rounds|It stops by itself if a point takes longer than 10 min, or the whole route longer than 2 h.|' + E['confirm.goal.safety'], $('confirm-list').children.map((c) => c.textContent).join('|'));
    $('confirm-cancel').click(); await settle();
    check('19s Run: ยกเลิกในหน้าต่างยืนยัน → ไม่ส่งอะไร', runPosts().length === 0);
    server.runHandler = () => runResp(running(), GOAL0, { ok: true });
    $('run-start').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19s Run: ยืนยัน → POST {name, confirmed:true} ครั้งเดียว (ไม่มี forever) + CSRF + toast', runPosts().length === 1 && JSON.stringify(runPosts()[0].body) === '{"name":"Delivery","confirmed":true}' &&
      runPosts()[0].headers['X-Requested-With'] === 'agv-web' && $('toast').textContent === E['toast.run_started'], JSON.stringify(runPosts()[0] && runPosts()[0].body));
    check('19s Run: กำลังรัน → แสดงสถานะ จุด/รอบ + แถบคืบหน้า + ปุ่มพัก/หยุดแทนปุ่มเริ่ม', $('run-status').textContent === 'Running “Delivery” — point 1/2: Dock · round 1/3' && $('run-start').hidden === true &&
      $('run-pause').hidden === false && $('run-pause').textContent === E['ui.run.pause'] && $('run-stop').hidden === false && $('run-progress').hidden === false && $('run-progress-fill').style.width === '0%', $('run-status').textContent);
    check('19s Run: ระหว่างรัน ช่อง/ปุ่มแก้ไข Route ถูกล็อกทั้งหมด + บอกเหตุผล', ['wp-name', 'wp-loop', 'wp-pname', 'wp-px', 'wp-save', 'wp-delete', 'wp-new', 'wp-tool-add', 'wp-del-point'].every((id) => $(id).disabled === true) &&
      $('run-note').textContent === E['run.locked'], $('run-note').textContent);
    const rp_before = rows().length; await tap(5.0, 2.0);
    check('19s Run: ระหว่างรัน แตะแผนที่ไม่เพิ่มจุด', rows().length === rp_before);

    // ── สถานะระหว่างทาง ──
    server.runGet = runResp(running({ index: 1, round: 2, current: { name: 'Station', x: 3, y: 2, yaw: 1.5, wait: 10 } }), { ...GOAL0, state: 'active', seq: 2, feedback: { distance_remaining: 1.234, eta: 7, recoveries: 0, elapsed: 3 }, waiting_assumed: false });
    await pollRunNow();
    check('19s Run: ไปจุดที่ 2 รอบที่ 2 → ข้อความ + ระยะเหลือจาก Nav2 + ความคืบหน้า 50% ((2-1)×2+1)/(3×2)', $('run-status').textContent === 'Running “Delivery” — point 2/2: Station · round 2/3' &&
      $('run-detail').hidden === false && $('run-detail').textContent === '1.2 m left · about 7 s' && $('run-progress-fill').style.width === '50%' && $('run-progress').attrs['aria-valuenow'] === '50', $('run-status').textContent + ' | ' + $('run-detail').textContent + ' | ' + $('run-progress-fill').style.width);
    check('19s Run: วาดบนแผนที่ — จุดที่ผ่านแล้ว (จุดที่ 1) จางลง', alphaSet(0.45));
    server.runGet = runResp(running({ index: 1, phase: 'waiting', wait_left: 7.3, current: { name: 'Station', x: 3, y: 2, yaw: 1.5, wait: 10 } }), { ...GOAL0, waiting_assumed: true });
    await pollRunNow();
    check('19s Run: หยุดรอที่จุด → "Waiting at Station — 8 s left" (ปัดขึ้น) ไม่แสดงระยะเหลือ', $('run-status').textContent === 'Waiting at Station — 8 s left · point 2/2 · round 1/3' && $('run-detail').hidden === true, $('run-status').textContent);
    server.runGet = runResp(running({ loop: 0, round: 7 }), { ...GOAL0, state: 'active', seq: 3, waiting_assumed: true });
    await pollRunNow();
    check('19s Run: วนไม่จบ → "round 7 (repeat forever)" + ข้อสันนิษฐานว่ารอสิ่งกีดขวาง + แถบคืบหน้าตามจุดในรอบ', $('run-status').textContent === 'Running “Delivery” — point 1/2: Dock · round 7 (repeat forever)' &&
      $('run-detail').textContent === E['navgoal.waiting'] && $('run-progress-fill').style.width === '0%', $('run-status').textContent + ' | ' + $('run-detail').textContent);

    // ── พัก / ทำต่อ ──
    server.runGet = runResp(running());
    server.runControl = (action) => (action === 'pause' ? runResp(running({ state: 'paused', pause_settled: false }), GOAL0, { ok: true })
      : (action === 'resume' ? runResp(running(), GOAL0, { ok: true }) : runResp(mkRun({ state: 'canceled', run: 1, route: 'Delivery', total: 2 }), GOAL0, { ok: true })));
    await pollRunNow();
    $('run-pause').click(); $('run-pause').click(); await settle();
    check('19s Run: กดพัก → POST /pause ครั้งเดียว (กดซ้ำไม่ซ้อน) + toast ; กำลังหยุดหุ่น → "Pausing" + ปุ่มเป็น Resume แต่ยังกดไม่ได้', runPosts('/pause').length === 1 && $('toast').textContent === E['toast.run_paused'] &&
      $('run-status').textContent === E['run.st.pausing'] && $('run-pause').textContent === E['ui.run.resume'] && $('run-pause').disabled === true, $('run-status').textContent);
    server.runGet = runResp(running({ state: 'paused', pause_settled: true }));
    await pollRunNow();
    check('19s Run: หยุดสนิทแล้ว → "Paused" + ปุ่ม Resume กดได้ ; ปุ่มหยุดยังอยู่', $('run-status').textContent === 'Paused “Delivery” — point 1/2: Dock · round 1/3' && $('run-pause').disabled === false && $('run-stop').hidden === false, $('run-status').textContent);
    $('run-pause').click(); await settle();
    check('19s Run: กดทำต่อ → POST /resume + toast', runPosts('/resume').length === 1 && $('toast').textContent === E['toast.run_resumed']);

    // ── หยุด: ไม่ถามยืนยัน ──
    server.runGet = runResp(running());
    await pollRunNow();
    const stopN0 = runPosts('/stop').length;
    $('run-stop').click(); await flush(); await flush();
    check('19s Run: กดหยุด → POST /stop ทันที ไม่มีหน้าต่างยืนยัน (ปุ่มนิรภัย) + toast', runPosts('/stop').length === stopN0 + 1 && $('confirm-dialog').open === false && $('toast').textContent === E['toast.run_stopped']);
    server.runGet = runResp(mkRun({ state: 'canceled', run: 1, route: 'Delivery', total: 2 }));
    await pollRunNow();
    check('19s Run: หยุดแล้ว → สถานะ "Stopped" + ปุ่มเริ่มกลับมา + ปลดล็อกการแก้ไข', $('run-status').textContent === 'Stopped “Delivery”.' && $('run-start').hidden === false && $('wp-new').disabled === false && $('run-pause').hidden === true, $('run-status').textContent);

    // ── จบ/ล้มเหลว : แจ้งครั้งเดียวต่อรอบ ──
    server.runGet = runResp(running({ run: 2 }));
    await pollRunNow();
    server.runGet = runResp(mkRun({ state: 'failed', run: 2, route: 'Delivery', loop: 3, total: 2, reason: M('nav.goal.aborted') }));
    await pollRunNow();
    check('19s Run: Route ล้มเหลว → สถานะบอกเหตุผลที่แปลแล้ว + toast ครั้งเดียว + ไม่ต้องกดอะไรก็เริ่มรอบใหม่ได้', $('run-status').textContent === 'Route stopped: ' + E['nav.goal.aborted'] && $('toast').textContent === E['run.done.failed'] && $('run-start').hidden === false, $('run-status').textContent);
    $('toast').textContent = 'CLEARED'; await pollRunNow();
    check('19s Run: โพลต่อไป ไม่แจ้งซ้ำ', $('toast').textContent === 'CLEARED');
    server.runGet = runResp(running({ run: 3 })); await pollRunNow();
    server.runGet = runResp(mkRun({ state: 'succeeded', run: 3, route: 'Delivery', loop: 3, round: 3, index: 1, total: 2 }));
    await pollRunNow();
    check('19s Run: Route เสร็จ → "Finished … 3 rounds done" + แถบเต็ม 100% + toast', $('run-status').textContent === 'Finished “Delivery” — 3 rounds done.' && $('run-progress-fill').style.width === '100%' && $('toast').textContent === E['run.done.succeeded'], $('run-status').textContent);
    server.runGet = runResp(mkRun());

    // ── ข้อผิดพลาดตอนเริ่ม ──
    server.runHandler = () => ({ status: 409, data: { ok: false, error: M('route.run.not_localized') } });
    $('run-start').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19s Run: เซิร์ฟเวอร์ปฏิเสธตอนเริ่ม → toast ข้อความที่แปลแล้ว + ปุ่มเริ่มกลับมากดได้', $('toast').textContent === E['route.run.not_localized'] && $('run-start').disabled === false, $('toast').textContent);
    server.runHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('run-start').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19s Run: เซสชันหมดอายุตอนเริ่ม → เด้งขอ PIN', $('pin-dialog').open === true); $('pin-cancel').click(); await flush(); await asAdmin(true); await settle();

    // ── วนไม่จบ: ต้องยืนยันเพิ่มอีกชั้น ──
    server.routes = info({ ...mkRoutes(), Delivery: { loop: 0, saved: 1, points: mkRoutes().Delivery.points } });
    await advance(4100); await settle();
    await openRoute('Delivery');
    server.runHandler = () => runResp(running({ loop: 0 }), GOAL0, { ok: true });
    const rpost0 = runPosts().length;
    $('run-start').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19s Run: วนไม่จบ → ถามยืนยันอีกครั้งแบบเตือนเฉพาะ (ยังไม่ส่ง)', $('confirm-dialog').open === true && $('confirm-title').textContent === E['run.forever.title'] && runPosts().length === rpost0);
    $('confirm-cancel').click(); await settle();
    check('19s Run: ปฏิเสธการวนไม่จบ → ไม่ส่งอะไร', runPosts().length === rpost0 && $('run-start').hidden === false);
    $('run-start').click(); await settle(); $('confirm-ok').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19s Run: ยืนยันครบสองชั้น → POST พร้อม forever_confirmed:true', runPosts().length === rpost0 + 1 && runPosts().slice(-1)[0].body.forever_confirmed === true && runPosts().slice(-1)[0].body.confirmed === true, JSON.stringify(runPosts().slice(-1)[0].body));
    server.runGet = runResp(mkRun()); server.runHandler = null; server.runControl = null;
    await pollRunNow();
    server.routes = info(mkRoutes()); await advance(4100); await settle(); await openRoute('Delivery');

    // ═════ Draw path : ลากตามเส้นทาง → จุดของ Route ═════
    const dp = async (pts, id = 1) => {            // ลากนิ้วผ่านพิกัดโลก pts (เมตร) แล้วยกนิ้ว
      down(id, sx(pts[0][0]), sy(pts[0][1]));
      for (const q of pts.slice(1)) move(id, sx(q[0]), sy(q[1]));
      up(id); await settle();
    };
    const line = (x0, y0, x1, y1, n) => Array.from({ length: n + 1 }, (_, i) => [x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n]);
    await pickTool('path');
    check('19s Path: เลือกเครื่องมือ Draw path → pressed + เคอร์เซอร์ + คำแนะนำ', $('wp-tool-path').attrs['aria-pressed'] === 'true' && hasClass(wcv, 'wp-path') && $('wp-help').textContent === E['wp.help.path'] && $('wp-tool-path').textContent === 'Draw path');
    const pn0 = rows().length;
    await dp(line(0.0, -1.0, 4.0, -1.0, 40));
    check('19s Path: ลากเส้นตรงยาว 4 ม. → ย่อเหลือจุดต้น/ท้าย 2 จุด (ไม่ใช่ 40 จุด) + จุดท้ายเส้นหันกลับไปจุดแรกของ Route (Route วนรอบ → 146°) + บอกจำนวน + ยังไม่ Save', rows().length === pn0 + 2 && $('toast').textContent === 'Added 2 points from the line' &&
      $('wp-status').textContent.startsWith('▲ Unsaved') && $('wp-pyaw').value === '146' && Math.abs(Number($('wp-px').value) - 4.0) < 0.1 && $('wp-pauto').checked === true, rows().length + ' ' + $('toast').textContent);
    rows()[rows().length - 2].children[0].click(); await flush();
    check('19s Path: จุดแรกของเส้นหันตามทิศเส้น (0°) ; จุดท้ายของ Route วนรอบหันกลับไปจุดแรกของ Route', $('wp-pyaw').value === '0' && $('wp-pauto').checked === true, $('wp-pyaw').value);
    const pn1 = rows().length;
    await dp([[0.0, 3.0], [1.5, 3.0], [3.0, 3.0], [3.0, 4.5], [3.0, 6.0]].flatMap((q, i, a) => (i ? line(a[i - 1][0], a[i - 1][1], q[0], q[1], 15).slice(1) : [q])));
    check('19s Path: ลากรูปตัว L → จุดที่มุม (3,3) ถูกเก็บไว้ : ย่อเหลือ 3 จุด (ต้น มุม ท้าย) + ทิศจุดแรก 0° จุดมุมหัน 90°', rows().length === pn1 + 3 && /x 3\.00, y 3\.00/.test(rowText(pn1 + 1)), rows().length + ' ' + rowText(pn1 + 1));
    await dp([[2.0, 1.0], [2.05, 1.0]]);
    check('19s Path: ลากสั้นเกินไป → ไม่เพิ่มจุด + บอกให้ลากยาวขึ้น', rows().length === pn1 + 3 && $('toast').textContent === E['wp.path.short'], $('toast').textContent);
    const pbefore = rows().length;
    down(1, sx(0.0), sy(-1.0)); down(2, sx(1.0), sy(-1.0)); move(2, sx(3.0), sy(-1.0)); up(2); up(1); await settle();
    check('19s Path: ใช้สองนิ้ว (ซูม/เลื่อน) ไม่นับเป็นการวาดเส้น', rows().length === pbefore);
    down(1, sx(0.0), sy(-1.0)); move(1, sx(2.0), sy(-1.0)); wcv.dispatch('pointercancel', { pointerId: 1, type: 'pointercancel' }); await settle();
    check('19s Path: pointercancel กลางเส้น → ทิ้งเส้น ไม่เพิ่มจุด', rows().length === pbefore);
    check('19s Path: ขณะลากเห็นเส้นตัวอย่างบนแผนที่ (วาด lineTo ตามนิ้ว)', (() => {
      down(1, sx(0.0), sy(-1.0)); move(1, sx(1.0), sy(-1.0)); move(1, sx(2.0), sy(-1.5));
      const log = wcv.ctxLog; const k = log.map((c) => c.name).lastIndexOf('clearRect');
      const ok = log.slice(k).filter((c) => c.name === 'lineTo').length >= 2; up(1); return ok; })());
    await settle();
    // เต็ม 50 จุด: เพิ่มได้เท่าที่เหลือ + บอก
    const proom = 50 - rows().length;
    for (let i = 0; i < proom - 1; i++) { await pickTool('add'); await tap(-2.5 + (i % 10) * 0.5, 4.5 + Math.floor(i / 10) * 0.3); }
    await pickTool('path');
    const countFull = rows().length;
    await dp(line(-2.5, -1.5, 1.0, -1.5, 14).concat(line(1.0, -1.5, 1.0, 1.5, 8).slice(1)).concat(line(1.0, 1.5, 5.0, 1.5, 12).slice(1)));
    check('19s Path: Route เหลือที่ว่าง 1 จุด → เพิ่มได้แค่ 1 ไม่เกิน 50 + บอกว่าเต็ม', countFull === 49 && rows().length === 50 && $('toast').textContent === 'Only the first 1 points were added (the route is full)', countFull + ' → ' + rows().length + ' ' + $('toast').textContent);
    // เก็บกวาด Route ที่แก้ค้าง
    $('wp-discard').click(); await settle(); $('confirm-ok').click(); await settle();
    // ฟังก์ชันย่อเส้นเป็นฟังก์ชันล้วน
    {
      const from = appSource.indexOf('function wpSimplify'), to = appSource.indexOf('function wpAddPath');
      const mod = new Function(appSource.slice(from, to) + '; return { wpSimplify, wpPathPoints };');
      const WP_PATH_EPS_M = 0.25, WP_PATH_SPACING_M = 0.6;
      const fn = new Function('WP_PATH_EPS_M', 'WP_PATH_SPACING_M', appSource.slice(from, to) + '; return { wpSimplify, wpPathPoints };')(WP_PATH_EPS_M, WP_PATH_SPACING_M);
      const pt = (x, y) => ({ x, y });
      check('19s Path: ย่อเส้น — เส้นตรงเหลือ 2 จุด', fn.wpSimplify([pt(0, 0), pt(1, 0), pt(2, 0), pt(3, 0)], 0.25).length === 2);
      check('19s Path: ย่อเส้น — เส้นโค้งเกินเกณฑ์ ต้องเก็บจุดโค้งไว้', fn.wpSimplify([pt(0, 0), pt(1, 1), pt(2, 0)], 0.25).length === 3 && fn.wpSimplify([pt(0, 0), pt(1, 0.2), pt(2, 0)], 0.25).length === 2);
      check('19s Path: ย่อเส้น — น้อยกว่า 3 จุดคืนตามเดิม + ไม่แก้อาร์เรย์ต้นฉบับ', (() => { const src = [pt(0, 0), pt(1, 1)]; const out = fn.wpSimplify(src, 0.25); return out.length === 2 && out !== src; })());
      check('19s Path: จุดที่ถี่กว่า 0.6 ม. ถูกทิ้ง แต่จุดปลายอยู่เสมอ', (() => { const out = fn.wpPathPoints([pt(0, 0), pt(0.3, 0), pt(0.5, 0.4), pt(2, 0)]); return out[0].x === 0 && out[out.length - 1].x === 2; })());
      check('19s Path: เส้นที่มีจุดเดียว/ว่าง → ไม่มีจุด', fn.wpPathPoints([pt(0, 0)]).length === 0 && fn.wpPathPoints(null).length === 0);
      void mod;
    }
    // ═════ ทิศหุ่นอัตโนมัติ (auto heading) : จุดระหว่างทางหันไปหาจุดถัดไป → หุ่นเข้าจุดแล้วหันตรงทางที่จะไป ไม่หมุนซ้อน ═════
    {
      await openRoute('Return');                                           // Route รอบเดียว มีจุดเดียว (Hall ที่ 0,0 ทิศ 0° ตั้งเอง)
      $('wp-fit').click(); await flush();                                   // รีเซ็ตมุมมองแผนที่ (เทสต์ก่อนหน้าซูม/เลื่อนไว้) ให้พิกัดที่แตะตรงตามที่คิด
      const yawOf = async (i) => { rows()[i].children[0].click(); await flush(); return $('wp-pyaw').value; };
      const autoOf = () => $('wp-pauto').checked;
      check('19t Auto: จุดที่มีอยู่เดิม (บันทึกก่อนมีฟีเจอร์นี้) ถือว่าตั้งเอง ไม่ถูกเขียนทับ + ช่องทิศแก้ได้', autoOf() === false && $('wp-pyaw').disabled === false && $('wp-pyaw').value === '0');
      await pickTool('add'); await tap(2.0, 0.0);
      check('19t Auto: เพิ่มจุดที่ 2 (รอบเดียว) → เป็นอัตโนมัติ หันตามทิศที่เดินมา (0°) ; ช่องทิศ/ปุ่มหมุนล็อก ; readout บอก (auto)', autoOf() === true && $('wp-pyaw').value === '0' && $('wp-pyaw').disabled === true &&
        $('wp-rot-l').disabled === true && $('wp-rot-r').disabled === true && /\(auto\)$/.test($('wp-readout').textContent), $('wp-readout').textContent);
      await tap(2.0, 3.0);
      check('19t Auto: เพิ่มจุดที่ 3 → จุดที่ 3 (ท้าย) หัน 90° ตามทางที่มา ; จุดที่ 2 เปลี่ยนไปหันหาจุดที่ 3 เอง (0° → 90°) ; จุดที่ 1 (ตั้งเอง) ไม่ถูกแตะ',
        (await yawOf(2)) === '90' && (await yawOf(1)) === '90' && (await yawOf(0)) === '0' && autoOf() === false);
      $('wp-auto-all').click(); await settle();
      check('19t Auto: ปุ่ม "Auto heading for all points" → ทุกจุดอัตโนมัติ : จุดที่ 1 หันหาจุดที่ 2 (0°) + toast', (await yawOf(0)) === '0' && autoOf() === true && $('toast').textContent === E['wp.auto_all_done'] &&
        rows().length === 3, $('toast').textContent);
      // เอาติ๊กออก → ตั้งเองได้ และไม่ถูกเขียนทับอีก
      rows()[1].children[0].click(); await flush();
      $('wp-pauto').checked = false; $('wp-pauto').dispatch('change'); await settle();
      check('19t Auto: เอาติ๊กออก → ช่องทิศ/ปุ่มหมุนเปิด', $('wp-pyaw').disabled === false && $('wp-rot-l').disabled === false && autoOf() === false);
      $('wp-pyaw').value = '45'; $('wp-pyaw').dispatch('input'); await settle();
      await pickTool('add'); await tap(5.0, 3.0);
      check('19t Auto: เพิ่มจุดที่ 4 → จุดที่ 2 (ตั้งเอง 45°) คงเดิม ; จุดที่ 3 เปลี่ยนไปหันหาจุดที่ 4 (0°) ; จุดที่ 4 หัน 0° ตามทางที่มา', (await yawOf(1)) === '45' && (await yawOf(2)) === '0' && (await yawOf(3)) === '0');
      // ย้ายจุด/ลบจุด/สลับลำดับ → ทิศของจุดข้างเคียงตามไป
      await pickTool('select');
      down(1, sx(2.0), sy(3.0)); move(1, sx(2.0), sy(3.0) + 30); move(1, sx(2.0) + 60, sy(3.0) - 50); up(1); await settle();     // ย้ายจุดที่ 3 ไปทางขวา-บน
      check('19t Auto: ย้ายจุดที่ 3 → ทิศของจุดที่ 3 (หันหาจุดที่ 4) เปลี่ยนตามตำแหน่งใหม่ (ไม่ใช่ 0° เดิม)', (await yawOf(2)) !== '0' && autoOf() === true);
      const yawBefore = await yawOf(2);
      await pickTool('delete'); await tap(5.0, 3.0);
      check('19t Auto: ลบจุดท้าย → จุดที่ 3 กลายเป็นจุดท้าย หันตามทางที่มา (เปลี่ยนจากเดิม)', rows().length === 3 && (await yawOf(2)) !== yawBefore);
      await pickTool('select');
      $('wp-auto-all').click(); await settle();
      rows()[2].children[1].click(); await settle();                          // ▲ สลับจุดที่ 3 ขึ้น
      const xy = (i) => { const m = /x (-?[\d.]+), y (-?[\d.]+)/.exec(rowText(i)); return [Number(m[1]), Number(m[2])]; };
      const lastExpected = Math.round(Math.atan2(xy(2)[1] - xy(1)[1], xy(2)[0] - xy(1)[0]) * 180 / Math.PI);
      const midExpected = Math.round(Math.atan2(xy(2)[1] - xy(1)[1], xy(2)[0] - xy(1)[0]) * 180 / Math.PI);
      check('19t Auto: สลับลำดับ → ทิศอัตโนมัติคำนวณใหม่ตามลำดับใหม่ (จุดท้ายหันตามทางที่มาจากจุดก่อนหน้าใหม่ ; จุดกลางหันหาจุดท้ายใหม่)', rows().length === 3 && Number(await yawOf(2)) === lastExpected && Number(await yawOf(1)) === midExpected, lastExpected + ' ' + (await yawOf(2)));
      // Route วนรอบ : จุดท้ายหันไปหาจุดแรก
      $('wp-auto-all').click(); await settle();                               // ให้ทุกจุดเป็นอัตโนมัติ (จุดท้ายต้องเป็นอัตโนมัติถึงจะตามการเปลี่ยนรอบ)
      $('wp-loop').value = '0'; $('wp-loop').dispatch('change'); await settle();
      const lastYaw = await yawOf(2);
      $('wp-loop').value = '1'; $('wp-loop').dispatch('change'); await settle();
      check('19t Auto: เปลี่ยนเป็นวนรอบ → จุดท้ายหันไปหาจุดแรก ; กลับเป็นรอบเดียว → หันตามทางที่มา (ต่างกัน)', lastYaw !== (await yawOf(2)));
      // ลากกำหนดทิศตอนวางจุด = ตั้งเอง
      await pickTool('add');
      down(1, sx(-2.0), sy(2.0)); move(1, sx(-2.0), sy(2.0) - 40); up(1); await settle();
      check('19t Auto: วางจุดแล้วลากกำหนดทิศเอง (90°) → ไม่ใช่อัตโนมัติ + ทิศคงที่ 90°', autoOf() === false && $('wp-pyaw').value === '90' && $('wp-pyaw').disabled === false);
      // ส่งให้เซิร์ฟเวอร์ : ธง auto ไปด้วย
      let saved = null;
      server.routesHandler = (b) => { saved = b; return info({ ...mkRoutes(), Return: { loop: b.loop, saved: 9, points: b.points } }, { ok: true }); };
      $('wp-save').click(); await settle();
      const shownFlags = []; for (let i = 0; i < rows().length; i++) { rows()[i].children[0].click(); await flush(); shownFlags.push(autoOf()); }
      check('19t Auto: Save → ส่งธง auto ของทุกจุดตรงกับที่เห็นในหน้าจอ (จุดที่ลากกำหนดทิศเอง = false ที่ท้าย)', saved && saved.points.length === 4 && shownFlags.join() === saved.points.map((q) => q.auto).join() &&
        saved.points[3].auto === false && saved.points.slice(0, 3).every((q) => q.auto === true), JSON.stringify(saved && saved.points.map((q) => q.auto)) + ' vs ' + shownFlags.join());
      server.routesHandler = null;
      $('wp-discard').click(); await settle(); if ($('confirm-dialog').open) { $('confirm-ok').click(); await settle(); }
      // เปิดใหม่: ธง auto ที่บันทึกไว้กลับมา และไม่ถูกแตะโดยการเปิดเฉยๆ
      server.routes = info({ ...mkRoutes(), Auto: { loop: 1, saved: 2, points: [{ ...P('A', 0, 0, 1.0), auto: true }, { ...P('B', 3, 0, 0.0), auto: true }, P('C', 3, 3, 2.0)] } });
      await advance(4100); await settle(); await openRoute('Auto');
      check('19t Auto: เปิด Route ที่บันทึกธงไว้ → แสดงติ๊กถูก (จุด A, B) และไม่คำนวณทิศใหม่เองเมื่อแค่เปิดดู (ยังเป็นค่าที่เซิร์ฟเวอร์ให้)', autoOf() === true && (await yawOf(0)) === '57' && autoOf() === true && (await yawOf(2)) === '115' && autoOf() === false && $('wp-save').disabled === true && $('wp-status').textContent === '✔ Saved');
      server.routes = info(mkRoutes()); await advance(4100); await settle();
      await openRoute('Delivery');
    }

    await pickTool('select');

    // ── ภาษา ──
    langBtn('th').click(); await flush();
    check('19r Wp: สลับ TH → ข้อความหน้า/รายการ/สถานะเป็นไทยทันที (ร่างคงเดิม)', $('wp-map-name').textContent === 'แผนที่: lab' && routeBtn('Delivery').children[1].textContent === '2 จุด · 3 รอบ' &&
      $('wp-status').textContent === '✔ บันทึกแล้ว' && $('wp-help').textContent === T['wp.help.select'], $('wp-status').textContent + ' | ' + routeBtn('Delivery').children[1].textContent);
    server.routesHandler = () => ({ status: 400, data: { ok: false, error: M('route.err.exists') } });
    await pickTool('add'); await tap(2.0, 3.0);
    $('wp-save').click(); await settle();
    check('19r Wp: ข้อผิดพลาดจากเซิร์ฟเวอร์เป็นไทย', $('wp-error').textContent === T['route.err.exists'], $('wp-error').textContent);
    langBtn('en').click(); await flush();
    check('19r Wp: สลับกลับ EN → ข้อผิดพลาดที่แสดงอยู่กลับเป็นอังกฤษทันที', $('wp-error').textContent === E['route.err.exists'], $('wp-error').textContent);
    wcv.dispatch('keydown', { key: 'Delete', preventDefault() {} }); await settle();   // ชื่อจุดตั้งต้นสร้างตามภาษาตอนนั้น ("จุดที่ 3") เป็นข้อมูลผู้ใช้ ไม่ใช่ UI → ลบก่อนเช็กตัวอักษรไทย
    check('19r Wp: ไม่มีตัวอักษรไทยหลุดในหน้าเมื่ออยู่โหมด EN', thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));

    // ── เก็บกวาด ──
    server.map = null; server.routes = null; server.routesHandler = null; server.routesDelete = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19v) หน้า Waypoint: การ์ด Straight line (วิ่งตรงตามเส้นจากจุดหนึ่งไปอีกจุดหนึ่ง) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const LIMITS = { max_points: 50, max_name: 40, max_wait: 600, max_loop: 99, clearance: 0.4 };
    const P = (name, x, y, yaw = 0) => ({ name, x, y, yaw, wait: 0 });
    const routes = { Delivery: { loop: 1, saved: 1, points: [P('Dock', 1.0, 1.0, 0), P('Station', 3.0, 1.0, 1.5)] }, Return: { loop: 1, saved: 1, points: [P('Hall', 0.0, 0.0)] } };
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 1.0, yaw_deg: 0 }, localization: { quality: 'good', sigma: 0.1 } };
    const GOAL = (state, over = {}) => ({ status: 200, data: { running: true, limits: { straight_min: 0.5, straight_max: 10, clearance: 0.4 },
      goal: Object.assign({ state, seq: 7, goal: { x: 3, y: 1, yaw: 1.5 }, line: { x0: 1, y0: 1 }, reason: null, feedback: null, elapsed: 2, waiting_assumed: false }, over) } });
    const posts = (path) => server.calls.filter((c) => c.method === 'POST' && c.path === path);
    const opts = (id) => $(id).children.map((o) => o.textContent);
    const pick = async (id, value) => { $(id).value = value; $(id).dispatch('change'); await settle(); };
    const pollNow = async () => { await advance(1100); await settle(); };
    const KEY = (route, i) => route + '\u0001' + i;

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(70) };
    server.routes = { status: 200, data: { map: 'lab', routes, running: false, map_ready: true, limits: LIMITS } };
    server.goalGet = GOAL('idle', { seq: 0, goal: null, line: null });
    await asAdmin(false);
    await send(modeOf('STANDBY'));
    nav('#/waypoint'); await settle(); await advance(4100); await settle();

    // ── ข้อความ + รายการตัวเลือก ──
    check('19v Line: การ์ดแสดงหัวข้อ/คำอธิบาย/ป้ายเป็นภาษาอังกฤษ', $('line-check').textContent === 'Check the line' &&
      $('line-run').textContent === E['ui.line.run'] && $('line-stop').hidden === true, $('line-run').textContent);
    check('19v Line: ตัวเลือก "จาก" = ตำแหน่งหุ่น + ทุกจุดของทุก Route ที่บันทึกไว้', JSON.stringify(opts('line-from')) ===
      JSON.stringify(['Robot position (now)', 'Delivery · Dock', 'Delivery · Station', 'Return · Hall']), JSON.stringify(opts('line-from')));
    check('19v Line: ตัวเลือก "ไปที่" ขึ้นต้นด้วยข้อความให้เลือก + ยังไม่เลือกอะไร', opts('line-to')[0] === 'Choose a point…' && $('line-to').value === '' && opts('line-to').length === 4, JSON.stringify(opts('line-to')));
    check('19v Line: ไม่ใช่ผู้ดูแล → ปุ่มปิด + บอกให้ล็อกอิน', $('line-run').disabled === true && $('line-check').disabled === true && $('line-note').textContent === E['line.need_admin'], $('line-note').textContent);

    await asAdmin(true);
    await pollNow();
    check('19v Line: Navigation ไม่รัน → ปุ่มปิด + บอกให้เปิด Navigation', $('line-run').disabled === true && $('line-note').textContent === E['line.need_nav'], $('line-note').textContent);
    await send(okState); await advance(3100); await settle();
    check('19v Line: ยังไม่เลือกปลายทาง → ปุ่มรันปิด + บอกให้เลือก + ปุ่มตรวจปิด', $('line-run').disabled === true && $('line-check').disabled === true && $('line-note').textContent === E['line.need_to'], $('line-note').textContent);

    await pick('line-to', KEY('Delivery', 1));
    check('19v Line: เลือกปลายทางแล้ว → ปุ่มรัน/ตรวจกดได้ ไม่มีข้อความเตือน', $('line-run').disabled === false && $('line-check').disabled === false && $('line-note').hidden === true, $('line-note').textContent);
    await pick('line-from', KEY('Delivery', 1));
    check('19v Line: จุดเริ่ม = จุดปลาย → บอกว่าเป็นจุดเดียวกัน + ปุ่มรันปิด', $('line-run').disabled === true && $('line-note').textContent === E['line.same'], $('line-note').textContent);
    await pick('line-from', KEY('Delivery', 0));
    check('19v Line: เลือกจุดเริ่ม-ปลายต่างกัน → ปุ่มรันกดได้', $('line-run').disabled === false);
    await send({ ...okState, localization: { quality: 'bad', sigma: 0.9 } }); await pollNow();      // การ์ดอัปเดตตามรอบโพล 1 วินาที เหมือนแถบรัน Route
    check('19v Line: หุ่นยังไม่ localize → ปุ่มรันปิด + บอกเหตุผลเดียวกับการส่ง goal', $('line-run').disabled === true && $('line-note').textContent === E['navgoal.need_loc'], $('line-note').textContent);
    await send(okState); await pollNow();

    // ── ตรวจแนวล่วงหน้า (ไม่ส่งอะไรให้หุ่น) ──
    $('line-check').click(); await settle();
    const c0 = posts('/api/nav/line/check')[0];
    check('19v Line: ตรวจ → POST /check ด้วยพิกัดจุดเริ่ม/ปลาย ไม่มี confirmed และไม่มี POST ส่งจริง', !!c0 && JSON.stringify(c0.body) === JSON.stringify({ from: { x: 1, y: 1 }, to: { x: 3, y: 1, yaw: 1.5 } }) &&
      posts('/api/nav/line').length === 0, JSON.stringify(c0 && c0.body));
    check('19v Line: แนวโล่ง → "✔ The line is clear — 2.24 m"', $('line-result').hidden === false && $('line-result').textContent === '✔ The line is clear — 2.24 m', $('line-result').textContent);
    server.lineCheck = () => ({ status: 200, data: { ok: true, check: { ok: true, at: null, reason: null, min_clearance: 0.43, soft_risk: true, length: 2.0, goal: {}, start: { distance: 0, heading_deg: 0, ok: true, max_distance: 0.3, max_heading_deg: 20 } } } });
    $('line-check').click(); await settle();
    check('19v Line: แนวใกล้ผนัง → เตือนระยะใกล้สุด', $('line-result').textContent === '✔ The line is clear — 2.00 m ▲ It passes close to a wall (closest 0.43 m)', $('line-result').textContent);
    server.lineCheck = () => ({ status: 200, data: { ok: true, check: { ok: false, at: 1.5, reason: 'tight', min_clearance: 0.2, soft_risk: true, length: 2.0, goal: {}, start: { distance: 0, heading_deg: 0, ok: true, max_distance: 0.3, max_heading_deg: 20 } } } });
    $('line-check').click(); await settle();
    check('19v Line: แนวชิดผนัง → บอกระยะที่ติดและเกณฑ์', $('line-result').textContent === '✖ The line gets closer than 0.4 m to a wall 1.5 m ahead', $('line-result').textContent);
    server.lineCheck = () => ({ status: 200, data: { ok: true, check: { ok: false, at: null, reason: null, min_clearance: null, soft_risk: false, length: 2.0, goal: {}, start: { distance: 0.82, heading_deg: 5, ok: false, max_distance: 0.3, max_heading_deg: 20 } } } });
    $('line-check').click(); await settle();
    check('19v Line: หุ่นอยู่ไกลจุดเริ่ม → บอกระยะและเกณฑ์', $('line-result').textContent === '✖ The robot is 0.82 m from the start of the line — it must be within 0.3 m of it', $('line-result').textContent);
    server.lineCheck = () => ({ status: 200, data: { ok: true, check: { ok: false, at: null, reason: null, min_clearance: null, soft_risk: false, length: 2.0, goal: {}, start: { distance: 0.1, heading_deg: 47.6, ok: false, max_distance: 0.3, max_heading_deg: 20 } } } });
    $('line-check').click(); await settle();
    check('19v Line: หุ่นหันเบี่ยงแนว → บอกองศาและเกณฑ์', $('line-result').textContent === '✖ The robot is turned 48° away from the line — it must face along it (within 20°)', $('line-result').textContent);
    await pick('line-to', KEY('Return', 0));
    check('19v Line: เปลี่ยนที่เลือก → ผลตรวจเก่าหายทันที (ไม่ค้างหลอก)', $('line-result').hidden === true && $('line-result').textContent === '');
    server.lineCheck = null;
    await pick('line-to', KEY('Delivery', 1));

    // ── สั่งวิ่ง : ถามยืนยันทุกครั้ง ──
    $('line-run').click(); await settle();
    check('19v Line: กดวิ่ง → หน้าต่างยืนยันแบบเตือน มีจุด/ระยะ/การหมุนปลายทาง/ข้อควรระวัง ; ยังไม่ส่งอะไร', $('confirm-title').textContent === 'Drive in a straight line?' && $('confirm-ok').textContent === 'Drive' &&
      $('confirm-list').children.map((c) => c.textContent).join('|') === 'From Delivery · Dock to Delivery · Station — 2.00 m|' + E['line.confirm.turn'] + '|' + E['confirm.goal.safety'] &&
      posts('/api/nav/line').length === 0, $('confirm-list').children.map((c) => c.textContent).join('|'));
    $('confirm-cancel').click(); await settle();
    check('19v Line: ยกเลิกหน้าต่างยืนยัน → ไม่ส่งอะไร', posts('/api/nav/line').length === 0);
    $('line-run').click(); await settle(); $('confirm-ok').click(); await settle();
    const s0 = posts('/api/nav/line')[0];
    check('19v Line: ยืนยัน → POST /api/nav/line {from, to, confirmed:true}', !!s0 && JSON.stringify(s0.body) === JSON.stringify({ from: { x: 1, y: 1 }, to: { x: 3, y: 1, yaw: 1.5 }, confirmed: true }), JSON.stringify(s0 && s0.body));
    check('19v Line: ส่งแล้ว → toast + สถานะ + ปุ่มรันซ่อน ปุ่มหยุดโผล่ + เลือกจุดไม่ได้', $('toast').textContent === E['toast.line_started'] && $('line-status').textContent === '▲ Driving along the line' &&
      $('line-run').hidden === true && $('line-stop').hidden === false && $('line-to').disabled === true && $('line-from').disabled === true, $('toast').textContent + ' | ' + $('line-status').textContent);

    // ── ระหว่างวิ่ง : ความคืบหน้า / หยุดไม่ถามยืนยัน / จบ ──
    server.goalGet = GOAL('active', { feedback: { distance_remaining: 1.46, eta: null, recoveries: 0, elapsed: null } });
    await pollNow();
    check('19v Line: ระหว่างวิ่ง → เหลือกี่เมตรจากตัวควบคุมจริง', $('line-detail').hidden === false && $('line-detail').textContent === '1.5 m to go', $('line-detail').textContent);
    server.goalGet = GOAL('active', { waiting_assumed: true, feedback: { distance_remaining: 1.46 } });
    await pollNow();
    check('19v Line: ระยะไม่ลดนาน → ขึ้นเป็น "ข้อสันนิษฐาน" ว่ากำลังรอ ไม่ใช่ข้อเท็จจริง', /waiting/i.test($('line-detail').textContent) && /1\.5 m to go/.test($('line-detail').textContent), $('line-detail').textContent);
    server.goalCancel = () => ({ status: 200, data: { ok: true, goal: GOAL('canceling').data.goal } });
    const cancels0 = posts('/api/nav/goal/cancel').length;
    $('line-stop').click(); await settle();
    check('19v Line: กดหยุด → POST /api/nav/goal/cancel ทันที 1 ครั้ง + toast + สถานะ Stopping', posts('/api/nav/goal/cancel').length === cancels0 + 1 && posts('/api/nav/line').length === 1 &&
      $('toast').textContent === E['toast.line_stopped'] && $('line-status').textContent === '▲ Stopping…', $('toast').textContent + ' | ' + $('line-status').textContent);
    server.goalGet = GOAL('canceled');
    await pollNow();
    check('19v Line: หยุดเสร็จ → แจ้งครั้งเดียว + ปุ่มรันกลับมา ปุ่มหยุดซ่อน เลือกจุดได้อีก', $('toast').textContent === E['navgoal.done.canceled'] && $('line-status').textContent === '○ Stopped' &&
      $('line-run').hidden === false && $('line-stop').hidden === true && $('line-to').disabled === false, $('toast').textContent);
    server.goalGet = GOAL('succeeded', { seq: 8 });
    server.goalGet = GOAL('active', { seq: 8 });
    await pollNow();
    server.goalGet = GOAL('succeeded', { seq: 8 });
    await pollNow();
    check('19v Line: ถึงปลายเส้น → "Arrived at the end of the line" + แจ้งผล', $('line-status').textContent === '✔ Arrived at the end of the line' && $('toast').textContent === E['navgoal.done.succeeded'], $('line-status').textContent);
    server.goalGet = GOAL('failed', { seq: 9, reason: 'nav.goal.aborted' });
    await pollNow();
    check('19v Line: ล้มเหลว → แสดงเหตุผลจากตัวควบคุม', $('line-status').textContent.startsWith('✖ The line was not completed — '), $('line-status').textContent);

    // ── เซิร์ฟเวอร์ปฏิเสธ ──
    server.goalGet = GOAL('idle', { seq: 0, goal: null, line: null });
    await pollNow();
    server.lineHandler = () => ({ status: 409, data: { ok: false, error: M('nav.err.line_heading', { found: 45.2, max: 20 }) } });
    $('line-run').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19v Line: เซิร์ฟเวอร์ปฏิเสธ (หุ่นหันเบี่ยง) → toast บอกเหตุผล + ไม่ขึ้นสถานะวิ่ง', $('toast').textContent === 'The robot is turned 45.2° away from the line — it must face along it (within 20°)' &&
      $('line-run').hidden === false && $('line-stop').hidden === true, $('toast').textContent);
    server.lineHandler = () => ({ status: 401, data: { ok: false, error: M('auth.required') } });
    $('line-run').click(); await settle(); $('confirm-ok').click(); await settle();
    check('19v Line: เซสชันหมด (401) → กลับเป็นโหมดไม่ใช่ผู้ดูแล', $('line-run').disabled === true);
    server.lineHandler = null; await asAdmin(true); await pollNow();

    // ── งานอื่นกำลังทำอยู่ ──
    server.goalGet = GOAL('active', { seq: 11, line: null });
    await pollNow();
    check('19v Line: goal ปกติกำลังวิ่ง → ปุ่มรันปิด + บอกให้ยกเลิกก่อน', $('line-run').hidden === false && $('line-run').disabled === true && $('line-note').textContent === E['run.busy_goal'], $('line-note').textContent);
    check('19v Line: goal ปกติไม่ใช่เส้น → ไม่แสดงสถานะ "วิ่งตามเส้น" และไม่มีปุ่มหยุดของการ์ดนี้ในหน้านี้', $('line-status').hidden === true, $('line-status').textContent);
    server.goalGet = GOAL('idle', { seq: 0, goal: null, line: null });
    await pollNow();

    // ── ภาษาไทย ──
    langBtn('th').click(); await settle();
    check('19v Line: สลับ TH → ป้าย/ตัวเลือกเป็นไทยทันที', $('line-check').textContent === T['ui.line.check'] && $('line-run').textContent === T['ui.line.run'] &&
      opts('line-from')[0] === T['line.from_robot'] && opts('line-to')[0] === T['line.to_pick'] && opts('line-from')[1] === 'Delivery · Dock', JSON.stringify(opts('line-from')));
    check('19v Line: สลับภาษาแล้วยังเลือกจุดเดิมไว้', $('line-from').value === KEY('Delivery', 0) && $('line-to').value === KEY('Delivery', 1), $('line-from').value + ' / ' + $('line-to').value);
    langBtn('en').click(); await settle();
    check('19v Line: สลับกลับ EN → ไม่มีตัวอักษรไทยหลุด', thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));
    server.routes = { status: 200, data: { map: 'lab', routes: { Return: routes.Return }, running: false, map_ready: true, limits: LIMITS } };
    await advance(4100); await settle();
    check('19v Line: Route หาย → ตัวเลือกที่เคยเลือกถูกล้าง ไม่ค้างชี้จุดเก่า', $('line-from').value === 'robot' && $('line-to').value === '' && opts('line-to').length === 2, $('line-from').value + ' / ' + $('line-to').value);

    // ── เก็บกวาด ──
    server.goalGet = null; server.goalCancel = null; server.lineCheck = null; server.lineHandler = null; server.routes = null; server.map = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19w) หน้า Waypoint: โหมดการวิ่งของ Route (Navigate / Straight) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const LIMITS = { max_points: 50, max_name: 40, max_wait: 600, max_loop: 99, clearance: 0.4 };
    const P = (name, x, y, yaw = 0) => ({ name, x, y, yaw, wait: 0 });
    const mk = (deliveryMode) => ({ Delivery: Object.assign({ loop: 1, saved: 1, points: [P('Dock', 1.0, 1.0, 0), P('Station', 3.0, 1.0, 1.5)] }, deliveryMode ? { mode: deliveryMode } : {}),
      Straighty: { loop: 1, mode: 'straight', saved: 1, points: [P('A', 1.0, 1.0, 0), P('B', 3.0, 1.0, 1.5)] } });
    const info = (routes) => ({ status: 200, data: { map: 'lab', routes, running: false, map_ready: true, limits: LIMITS } });
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 1.0, yaw_deg: 0 }, localization: { quality: 'good', sigma: 0.1 } };
    const routeBtn = (name) => $('wp-routes').children.find((b) => b.children[0].textContent === name);
    const open = async (name) => { routeBtn(name).click(); await settle(); };
    const opts = () => $('wp-mode').children.map((o) => o.textContent);
    const GOAL0 = { state: 'idle', seq: 0, goal: null, line: null, reason: null, feedback: null, elapsed: null, waiting_assumed: false };
    const runObj = (over) => Object.assign({ state: 'idle', run: 0, route: null, map: 'lab', mode: 'navigate', loop: 1, round: 0, index: 0, total: 0, current: null, phase: null,
      wait_left: null, elapsed: null, reason: null, max_run: 7200, point_timeout: 600, pause_settled: true, loc_grace: 0 }, over);

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(70) };
    server.routes = info(mk(null));                                          // Route เก่าไม่มีฟิลด์ mode เลย
    server.goalGet = { status: 200, data: { running: true, goal: GOAL0 } };
    await asAdmin(true);
    await send(okState);
    nav('#/waypoint'); await settle(); await advance(4100); await settle();

    await open('Delivery');
    check('19w Mode: ตัวเลือกมี 2 โหมดเป็นข้อความอังกฤษ', JSON.stringify(opts()) === JSON.stringify([E['wp.mode.navigate'], E['wp.mode.straight']]), JSON.stringify(opts()));
    check('19w Mode: Route เก่าที่ไม่มี mode = Navigate + คำอธิบายของโหมดนั้น', $('wp-mode').value === 'navigate' && $('wp-mode-help').textContent === E['wp.mode.help.navigate'], $('wp-mode').value);
    check('19w Mode: ผู้ดูแลแก้ได้ (ช่องไม่ถูกล็อก)', $('wp-mode').disabled === false);
    $('wp-mode').value = 'straight'; $('wp-mode').dispatch('change'); await settle();
    check('19w Mode: เปลี่ยนเป็น Straight → คำอธิบายเปลี่ยน + นับเป็น "ยังไม่บันทึก"', $('wp-mode-help').textContent === E['wp.mode.help.straight'] && $('wp-status').textContent.startsWith('▲ Unsaved'), $('wp-status').textContent);
    let saved = null;
    server.routesHandler = (b) => { saved = b; return { status: 200, data: Object.assign(info(Object.assign(mk('straight'))).data, { ok: true }) }; };
    $('wp-save').click(); await settle();
    check('19w Mode: บันทึก → ส่ง mode:"straight" ไปกับ Route', !!saved && saved.mode === 'straight' && saved.name === 'Delivery' && saved.points.length === 2, JSON.stringify(saved && saved.mode));
    check('19w Mode: บันทึกแล้วร่างยังเป็น Straight และไม่ค้าง "ยังไม่บันทึก"', $('wp-mode').value === 'straight' && !$('wp-status').textContent.startsWith('▲ Unsaved'), $('wp-mode').value + ' | ' + $('wp-status').textContent);
    await open('Straighty');
    check('19w Mode: เปิด Route ที่เป็น Straight → ตัวเลือกตรงกับที่บันทึกไว้', $('wp-mode').value === 'straight' && $('wp-mode-help').textContent === E['wp.mode.help.straight'], $('wp-mode').value);
    server.routesHandler = (b) => { saved = b; return { status: 400, data: { ok: false, error: M('route.err.straight_points', { min: 2 }) } }; };
    $('wp-new').click(); await settle();
    check('19w Mode: Route ใหม่เริ่มที่ Navigate (ค่าเดิม ไม่เปลี่ยนพฤติกรรมของคนที่ไม่เลือก)', $('wp-mode').value === 'navigate');
    $('wp-mode').value = 'straight'; $('wp-mode').dispatch('change'); await settle();
    $('wp-name').value = 'Fresh'; $('wp-name').dispatch('input'); await settle();

    // ── รัน Route โหมด Straight : หน้าต่างยืนยันมีข้อควรรู้เพิ่ม + สถานะบอกโหมด ──
    $('wp-discard').click(); await settle(); $('confirm-ok').click(); await settle();
    await open('Straighty'); await advance(1100); await settle();
    check('19w Run: Route โหมด Straight เริ่มรันได้ตามเงื่อนไขเดิม', $('run-start').disabled === false, $('run-note').textContent);
    $('run-start').click(); await settle();
    const items = $('confirm-list').children.map((c) => c.textContent);
    check('19w Run: หน้าต่างยืนยันบอกว่าหุ่นต้องอยู่ที่จุดที่ 1 หันไปจุดที่ 2 + ข้อความความปลอดภัยเดิมยังอยู่', items.includes(E['run.confirm.straight']) && items.includes(E['confirm.goal.safety']) && items.length === 4, JSON.stringify(items));
    $('confirm-cancel').click(); await settle();
    await open('Delivery'); await advance(1100); await settle();
    $('run-start').click(); await settle();
    const items2 = $('confirm-list').children.map((c) => c.textContent);
    check('19w Run: Route โหมด Straight (Delivery ที่เพิ่งบันทึก) ก็มีข้อควรรู้เดียวกัน', items2.includes(E['run.confirm.straight']), JSON.stringify(items2));
    $('confirm-cancel').click(); await settle();
    server.runGet = { status: 200, data: { running: true, goal: GOAL0, run: runObj({ state: 'running', run: 3, route: 'Straighty', mode: 'straight', round: 1, index: 1, total: 2, phase: 'going', current: P('B', 3.0, 1.0, 1.5) }) } };
    await advance(1100); await settle();
    check('19w Run: ระหว่างรัน Straight → สถานะต่อท้ายว่า straight', / · straight$/.test($('run-status').textContent), $('run-status').textContent);
    server.runGet = { status: 200, data: { running: true, goal: GOAL0, run: runObj({ state: 'running', run: 4, route: 'Delivery', mode: 'navigate', round: 1, index: 1, total: 2, phase: 'going', current: P('B', 3.0, 1.0, 1.5) }) } };
    await advance(1100); await settle();
    check('19w Run: ระหว่างรัน Navigate → สถานะไม่มีคำว่า straight', !/straight/.test($('run-status').textContent), $('run-status').textContent);
    server.runGet = null;

    // ── ภาษาไทย / ไม่ใช่ผู้ดูแล ──
    langBtn('th').click(); await settle();
    check('19w Mode: สลับ TH → ตัวเลือกและคำอธิบายเป็นไทยทันที', JSON.stringify(opts()) === JSON.stringify([T['wp.mode.navigate'], T['wp.mode.straight']]) && $('wp-mode-help').textContent.length > 0 &&
      !/^[\x00-\x7F]*$/.test($('wp-mode-help').textContent), JSON.stringify(opts()));
    langBtn('en').click(); await settle();
    check('19w Mode: สลับกลับ EN → ไม่มีตัวอักษรไทยหลุด', thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));
    await asAdmin(false); await advance(100); await settle();
    check('19w Mode: ไม่ใช่ผู้ดูแล → เปลี่ยนโหมดไม่ได้', $('wp-mode').disabled === true);

    server.routesHandler = null; server.routes = null; server.map = null; server.goalGet = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 19x) หน้า Waypoint: จุดหยุด / จุด "ผ่านไปเลย" ต่อจุด (โหมด Straight) ═════════
  {
    const settle = async () => { await flush(); await flush(); await flush(); };
    const E = I.DICT.en, T = I.DICT.th;
    const LIMITS = { max_points: 50, max_name: 40, max_wait: 600, max_loop: 99, clearance: 0.4 };
    const P = (name, x, y, yaw = 0, stop = true) => ({ name, x, y, yaw, wait: 3, stop });
    const mk = () => ({ Tri: { loop: 1, mode: 'straight', saved: 1, points: [P('A', 1, 1), P('B', 3, 1, 1.5), P('C', 3, 3)] },
      Plain: { loop: 1, mode: 'navigate', saved: 1, points: [P('A', 1, 1), P('B', 3, 1), P('C', 3, 3)] } });
    const info = (routes) => ({ status: 200, data: { map: 'lab', routes, running: false, map_ready: true, limits: LIMITS } });
    const okState = { ...modeOf('NAVIGATION'), pose: { x: 1.0, y: 1.0, yaw_deg: 0 }, localization: { quality: 'good', sigma: 0.1 } };
    const routeBtn = (name) => $('wp-routes').children.find((b) => b.children[0].textContent === name);
    const open = async (name) => { routeBtn(name).click(); await settle(); };
    const selectPoint = async (i) => { $('wp-points').children[i].children[0].click(); await settle(); };
    const rowMeta = (i) => $('wp-points').children[i].children[0].children[1].children[1].textContent;

    langBtn('en').click(); await flush();
    server.map = { status: 200, data: MAPMETA(70) };
    server.routes = info(mk());
    await asAdmin(true);
    await send(okState);
    nav('#/waypoint'); await settle(); await advance(4100); await settle();

    await open('Tri');
    await selectPoint(1);
    check('19x Stop: ช่องติ๊กหยุดที่จุดกลางทางของ Route Straight เปิดอยู่ + ติ๊กอยู่ (ค่าเริ่มต้น = หยุด)', $('wp-pstop').disabled === false && $('wp-pstop').checked === true && $('wp-pstop-note').hidden === true, $('wp-pstop-note').textContent);
    await selectPoint(0);
    check('19x Stop: จุดแรกหยุดเสมอ → ช่องปิด + บอกเหตุผล', $('wp-pstop').disabled === true && $('wp-pstop').checked === true && $('wp-pstop-note').textContent === E['wp.stop_note.ends'], $('wp-pstop-note').textContent);
    await selectPoint(2);
    check('19x Stop: จุดสุดท้ายหยุดเสมอ → ช่องปิด', $('wp-pstop').disabled === true && $('wp-pstop-note').textContent === E['wp.stop_note.ends']);

    await selectPoint(1);
    $('wp-pstop').checked = false; $('wp-pstop').dispatch('change'); await settle();
    check('19x Stop: เอาติ๊กออก → จุดกลายเป็น "ผ่านไปเลย" ในรายการ + ช่องเวลารอถูกล็อก + นับเป็นยังไม่บันทึก', rowMeta(1) === 'pass through · x 3.00, y 1.00' && $('wp-pwait').disabled === true &&
      $('wp-status').textContent.startsWith('▲ Unsaved'), rowMeta(1) + ' | ' + $('wp-status').textContent);
    check('19x Stop: จุดหยุดอื่นยังแสดงเวลารอตามเดิม', rowMeta(0) === 'wait 3 s · x 1.00, y 1.00' && rowMeta(2) === 'wait 3 s · x 3.00, y 3.00', rowMeta(0) + ' / ' + rowMeta(2));
    let saved = null;
    server.routesHandler = (b) => { saved = b; return { status: 200, data: Object.assign(info(mk()).data, { ok: true }) }; };
    $('wp-save').click(); await settle();
    check('19x Stop: บันทึก → ส่ง stop ต่อจุด (จุดกลาง false ที่เหลือ true)', !!saved && JSON.stringify(saved.points.map((p) => p.stop)) === JSON.stringify([true, false, true]) && saved.mode === 'straight', JSON.stringify(saved && saved.points.map((p) => p.stop)));
    server.routesHandler = null;

    await open('Tri');
    await selectPoint(1);
    $('wp-pstop').checked = false; $('wp-pstop').dispatch('change'); await settle();
    $('wp-mode').value = 'navigate'; $('wp-mode').dispatch('change'); await settle();
    check('19x Stop: สลับเป็น Navigate → จุดผ่านไปเลยกลับเป็นจุดหยุดทั้งหมด + ช่องปิด + บอกว่ามีเฉพาะ Straight', $('wp-pstop').checked === true && $('wp-pstop').disabled === true &&
      $('wp-pstop-note').textContent === E['wp.stop_note.navigate'] && !/pass through/.test(rowMeta(1)), $('wp-pstop-note').textContent + ' | ' + rowMeta(1));
    $('wp-discard').click(); await settle(); $('confirm-ok').click(); await settle();

    await open('Plain');
    await selectPoint(1);
    check('19x Stop: Route โหมด Navigate → ช่องติ๊กปิด + บอกว่ามีเฉพาะ Straight', $('wp-pstop').disabled === true && $('wp-pstop-note').textContent === E['wp.stop_note.navigate'], $('wp-pstop-note').textContent);

    await asAdmin(false); await advance(100); await settle();
    await open('Tri'); await selectPoint(1);
    check('19x Stop: ไม่ใช่ผู้ดูแล → เปลี่ยนไม่ได้', $('wp-pstop').disabled === true);
    await asAdmin(true); await advance(100); await settle();

    langBtn('th').click(); await settle();
    await selectPoint(0);
    check('19x Stop: สลับ TH → ข้อความของช่องติ๊กเป็นไทย', $('wp-pstop-note').textContent === T['wp.stop_note.ends'] && T['wp.stop_note.ends'] !== E['wp.stop_note.ends'] && /[ก-๙]/.test($('wp-pstop-note').textContent), $('wp-pstop-note').textContent);
    langBtn('en').click(); await settle();
    check('19x Stop: สลับกลับ EN → ไม่มีตัวอักษรไทยหลุด', thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));

    server.routesHandler = null; server.routes = null; server.map = null;
    await send(modeOf('STANDBY')); await asAdmin(false);
    nav('#/home'); await advance(100);
  }

  // ═════════ 21) สลับภาษา EN → TH → EN โดยไม่โหลดหน้าใหม่ ═════════
  await send({ mode: { mode: 'NAVIGATION' }, speed: { v: 0.1, w: 0.2 }, localization: { quality: 'fair', sigma: 0.2 },
    activity: { state: 'moving', level: 'ok', text: M('act.moving'), detail: null },
    readiness: { ready: true, headline: M('ready.headline.ready'), hint: null, items: [readyItems[0], readyItems[1],
      { key: 'localization', level: 'warn', text: M('ready.loc.fair'), hint: null }] } });
  nav('#/home'); await advance(2000);
  check('21 ก่อนสลับ: ปุ่ม EN ถูกเน้น', langBtn('en').attrs['aria-pressed'] === 'true' && langBtn('th').attrs['aria-pressed'] === 'false');
  langBtn('th').click(); await flush();
  check('21 สลับเป็น TH: lang + localStorage + ปุ่ม', I.getLang() === 'th' && documentElement.attrs.lang === 'th' && storage['agv.lang'] === 'th' &&
    langBtn('th').attrs['aria-pressed'] === 'true' && langBtn('en').attrs['aria-pressed'] === 'false');
  check('21 TH: ข้อความ static ถูกแปล (data-i18n + aria)', i18nNodes.every((n) => n.el.textContent === th[n.key]) &&
    i18nArias.every((n) => n.el.attrs['aria-label'] === th[n.key]));
  check('21 TH: ป้ายโหมด/การ์ดโหมด/สถานะ/ชิป', $('pill-text').textContent === 'Navigation' && $('mode-th').textContent === 'นำทางอัตโนมัติ' &&
    $('mode-state').textContent === 'ทำงานปกติ' && $('act-text').textContent === 'กำลังเคลื่อนที่' &&
    $('chips').children[2].children[0].textContent === '▲ตำแหน่ง · พอใช้', $('chips').children[2].children[0].textContent);
  check('21 TH: รายการความพร้อม + เวลา', $('checks').children[0].children[1].textContent === 'ระบบนำทางทำงานอยู่' && /^ทำงานตั้งแต่ /.test($('mode-msg').textContent), $('mode-msg').textContent);
  check('21 TH: กราฟ (ป้ายแกน/ตาราง/aria)', cls(svgV, 'ch-tick').some((k) => k.textContent === '-2 นาที') && cls(svgV, 'ch-tick').some((k) => k.textContent === 'ตอนนี้') &&
    svgV.attrs['aria-label'].startsWith('ความเร็วเดินหน้า: ล่าสุด') && $('speed-body').children[0].children[0].textContent === 'ตอนนี้' &&
    $('speed-body').children[1].children[0].textContent === '-10 วินาที', svgV.attrs['aria-label']);
  check('21 TH: กุญแจ + hint', $('lock').attrs['aria-label'] === 'เข้าสู่ระบบผู้ดูแล' && $('mode-hint').textContent === 'ต้องเข้าสู่ระบบผู้ดูแลก่อนจึงจะเปลี่ยนโหมดได้', $('mode-hint').textContent);
  await send({ mode: { state: 'ERROR', message: M('mode.err.timeout', { mode: 'MAPPING', seconds: 40, log: '/l.log' }) } });
  check('21 TH: ข้อความ backend ถูกแปลเป็นไทย', $('mode-msg').textContent === 'เปลี่ยนโหมดไม่สำเร็จ: เปิดโหมด Mapping ไม่สำเร็จภายใน 40 วินาที ดู log: /l.log', $('mode-msg').textContent);
  langBtn('en').click(); await flush();
  check('21 สลับกลับ EN: ข้อความ backend/ชิป/กราฟ กลับเป็นอังกฤษ', I.getLang() === 'en' && storage['agv.lang'] === 'en' &&
    $('mode-msg').textContent === 'Mode change failed: Mapping mode did not start within 40 s. Log: /l.log' &&
    cls(svgV, 'ch-tick').some((k) => k.textContent === '-2 min') && thaiLeaks().length === 0, JSON.stringify(thaiLeaks()));
  check('21 สลับกลับ EN: static ตรงกับ EN', i18nNodes.every((n) => n.el.textContent === en[n.key]));

  // จำภาษาไว้ข้ามการโหลดหน้า: init อ่านจาก localStorage
  storage['agv.lang'] = 'th'; I.init();
  check('21 init() อ่านภาษาที่เคยเลือกจาก localStorage', I.getLang() === 'th' && documentElement.attrs.lang === 'th');
  storage['agv.lang'] = 'xx'; I.init();
  check('21 ค่าใน localStorage แปลกๆ → กลับเป็น EN', I.getLang() === 'en');
  I.setLang('fr');
  check('21 setLang ภาษาที่ไม่รองรับ → ไม่เปลี่ยน', I.getLang() === 'en');

  check('99 ไม่เคยเขียน innerHTML ตลอดการทดสอบ', innerHTMLWrites === 0, innerHTMLWrites);
  print(`UI harness: ${passes} passed, ${failures} failed`);
  System.exit(failures ? 1 : 0);
}

main().catch((e) => { print(`HARNESS ERROR: ${e}\n${e.stack || ''}`); System.exit(2); });
