  // ── Waypoint: สร้าง/แก้ Route (ลำดับจุดหมาย) ต่อแผนที่ ───────────────────────────────────────────
  // 1 แผนที่มีได้หลาย Route ; Route = ลำดับจุด (ชื่อ, x, y, ทิศ, เวลาหยุดรอ) + จำนวนรอบ ; ตอนนี้ "แก้ไขอย่างเดียว" ไม่สั่งหุ่น
  // ใช้แผนที่/มุมมองชุดเดียวกับหน้า Mapping และ Navigation (เป็นของหน้าเดียวต่อครั้ง) ; ร่าง (draft) อยู่ในหน้านี้จนกด Save
  // กติกาจริง (จุดต้องว่างและห่างผนัง ≥ clearance) ตัดสินที่เซิร์ฟเวอร์ตอน Save — หน้าเว็บแค่ชี้ว่าจุดไหนไม่ผ่าน
  var WP_POLL_MS = 4000;
  var WP_HIT_PX = 26;                                          // รัศมีแตะโดนจุด (นิ้วต้องโดนง่าย)
  var WP_MOVE_MIN_PX = 6;                                      // ลากน้อยกว่านี้ไม่ถือว่าย้ายจุด (กันนิ้วสั่น)
  var WP_LOOPS = [1, 2, 3, 5, 10, 0];                          // 1 = รอบเดียว · 0 = วนไม่จบ
  var WP_MODES = ['navigate', 'straight'];                     // navigate = Nav2 วางเส้นทางเอง · straight = ทุกช่วงเป็นเส้นตรงจริง
  var wp = { map: null, mapReady: false, loaded: false, routes: {}, limits: null, draft: null, dirty: false, sel: -1,
             tool: 'select', busy: false, drag: null, badIdx: -1, error: null, stroke: null };
  var WP_PATH_SAMPLE_M = 0.05;                                 // Draw path: เก็บจุดตัวอย่างทุกครั้งที่ลากไกลอย่างน้อยเท่านี้
  var WP_PATH_EPS_M = 0.25;                                    // ย่อเส้น (Douglas–Peucker) ยอมคลาดเคลื่อนเท่านี้
  var WP_PATH_SPACING_M = 0.6;                                 // จุดที่ห่างจุดก่อนหน้าน้อยกว่านี้ถูกทิ้ง (ไม่ให้จุดถี่จนหุ่นหยุดๆ วิ่งๆ)
  var wpDataKey = '';                                          // ลายนิ้วมือของข้อมูลจากเซิร์ฟเวอร์ (เปลี่ยนจริงถึงวาดรายการใหม่)
  var wpCtlKey = '';

  function onWpPage() { return (ROUTES[location.hash] || 'home') === 'waypoint'; }

  function onWpMapPage() { return onWpPage() && !document.hidden; }

  function wpMapReady() { return !!(connected && wp.loaded && wp.map && wp.mapReady); }       // มีแผนที่ที่บันทึกไว้และอ่านได้

  function wpEditable() { return !!(admin && authEnabled && connected && wp.map && !wp.busy && !runBusy()); }

  function wpCanDraw() {
    return !!(wpEditable() && wp.draft && mapOwner === 'wp' && mapMeta && mapMeta.available && mapImg);
  }

  function wpLimit(key, fallback) { return (wp.limits && wp.limits[key] !== undefined) ? wp.limits[key] : fallback; }

  function wpLoopText(loop) {
    return loop === 1 ? t('wp.loop.once') : (loop === 0 ? t('wp.loop.forever') : t('wp.loop.n', { n: String(loop) }));
  }

  function wpClone(route, name) {
    return { name: name, previous: name, loop: route.loop, mode: route.mode === 'straight' ? 'straight' : 'navigate', points: route.points.map(function (p) {
      return { name: p.name, x: p.x, y: p.y, yaw: p.yaw, wait: p.wait, auto: p.auto === true, stop: p.stop !== false };
    }) };
  }

  function wpIssue() {                                         // เหตุผลที่ยังบันทึกไม่ได้ ('' = พร้อม)
    var d = wp.draft;
    if (!d) { return 'none'; }
    if (!d.name.trim()) { return 'name'; }
    return d.points.length ? '' : 'empty';
  }

  function wpToScreen(x, y) {
    var size = mapCanvasSize();
    return { x: size.w / 2 + (x - mapView.cx) * mapView.scale, y: size.h / 2 - (y - mapView.cy) * mapView.scale };
  }

  function wpHit(p) {                                          // จุดที่ใกล้นิ้วที่สุดในรัศมี (ไม่มี = -1)
    var best = -1, bestD = WP_HIT_PX;
    if (!wp.draft) { return -1; }
    wp.draft.points.forEach(function (pt, i) {
      var s = wpToScreen(pt.x, pt.y);
      var d = Math.hypot(s.x - p.x, s.y - p.y);
      if (d <= bestD) { best = i; bestD = d; }
    });
    return best;
  }

  // ── ข้อมูลจากเซิร์ฟเวอร์ ──────────────────────────────────────────────────────────────────────
  function wpSig(d) { return JSON.stringify([d.map, d.routes, d.running, d.map_ready]); }

  function wpApply(data) {
    var key = wpSig(data);
    wp.limits = data.limits;
    wp.loaded = true;
    if (key === wpDataKey) { return; }
    wpDataKey = key;
    if (wp.map !== data.map && wp.draft) {                     // ร่างเป็นของแผนที่เดิม → ทิ้ง (บอกผู้ใช้ถ้ามีงานค้าง)
      if (wp.dirty) { toast(t('toast.wp_map_changed')); }
      wp.draft = null; wp.dirty = false; wp.sel = -1; wp.badIdx = -1; wp.error = null;
    }
    wp.map = data.map;
    wp.mapReady = !!data.map_ready;
    wp.routes = data.routes || {};
    if (wp.draft && wp.draft.previous && !wp.routes[wp.draft.previous] && !wp.dirty) { wp.draft = null; wp.sel = -1; }
    wpRender();
    if (onWpMapPage()) { renderWpMap(); pollMap(); }                    // แผนที่พร้อม/เปลี่ยน → ดึงภาพทันที (ไม่รอรอบ 3 วินาที)
  }

  function loadRoutes() {
    return api('/api/routes').then(function (r) {
      if (r.status === 200 && r.data && r.data.routes) { wpApply(r.data); }
    });
  }

  function pollRoutes() {
    if (onWpMapPage() && !wp.busy) { loadRoutes(); }
  }

  // ── ร่าง (draft) ───────────────────────────────────────────────────────────────────────────────
  // ทิศหุ่นอัตโนมัติ : จุดที่ตั้งเป็น auto หันไปหาจุดถัดไป เพื่อให้หุ่นเข้าจุดแล้วหันตรงทางที่จะไปเลย (ไม่ต้องหมุนเข้าหา yaw ของจุด
  // แล้วหมุนกลับอีกรอบตอนออก) ; จุดสุดท้ายของ Route รอบเดียว = ทิศที่เดินมา ; Route ที่วนรอบ จุดสุดท้ายหันไปหาจุดแรก (ช่วงถัดไปของมัน)
  function wpAutoHeadings() {
    var d = wp.draft;
    if (!d || d.points.length < 2) { return; }
    var pts = d.points, n = pts.length;
    pts.forEach(function (p, i) {
      if (!p.auto) { return; }
      var from = p, to;
      if (i < n - 1) { to = pts[i + 1]; }
      else if (d.loop !== 1) { to = pts[0]; }
      else { from = pts[i - 1]; to = p; }
      if (Math.hypot(to.x - from.x, to.y - from.y) < 0.05) { return; }       // จุดซ้อนกัน: คงทิศเดิม
      p.yaw = Math.atan2(to.y - from.y, to.x - from.x);
    });
  }

  function wpTouch() {                                         // ร่างเปลี่ยน → ล้างข้อผิดพลาดเก่า วาดใหม่
    wpAutoHeadings();
    wp.dirty = true;
    wp.badIdx = -1;
    wp.error = null;
    wpRender();
    drawMap();
  }

  function wpDiscardAsk() {                                    // มีงานค้าง → ถามก่อนทิ้ง
    if (!wp.dirty) { return Promise.resolve(true); }
    return confirmDialog(t('wp.discard.title'), t('wp.discard.msg'), t('wp.discard.ok'), { warn: true });
  }

  function wpOpen(name) {
    wpDiscardAsk().then(function (ok) {
      if (!ok || !wp.routes[name]) { return; }
      wp.draft = wpClone(wp.routes[name], name);
      wp.dirty = false; wp.sel = wp.draft.points.length ? 0 : -1; wp.badIdx = -1; wp.error = null;
      wpRender();
      drawMap();
    });
  }

  function wpNew() {
    if (!wpEditable()) { return; }
    wpDiscardAsk().then(function (ok) {
      if (!ok) { return; }
      wp.draft = { name: '', previous: null, loop: 1, mode: 'navigate', points: [] };
      wp.dirty = false; wp.sel = -1; wp.badIdx = -1; wp.error = null;
      wpRender();
      drawMap();
      $('wp-name').focus();
    });
  }

  function wpDiscard() {
    wpDiscardAsk().then(function (ok) {
      if (!ok) { return; }
      var prev = wp.draft && wp.draft.previous;
      wp.draft = (prev && wp.routes[prev]) ? wpClone(wp.routes[prev], prev) : null;
      wp.dirty = false; wp.sel = (wp.draft && wp.draft.points.length) ? 0 : -1; wp.badIdx = -1; wp.error = null;
      wpRender();
      drawMap();
    });
  }

  function wpPlace(x, y, yaw) {                                // เพิ่มจุดท้าย Route ; คืน index (-1 = เพิ่มไม่ได้)
    var pts = wp.draft.points;
    if (pts.length >= wpLimit('max_points', 50)) { toast(t('wp.max_points', { max: String(wpLimit('max_points', 50)) })); return -1; }
    var autoHeading = (yaw === undefined);                     // ไม่ระบุทิศ = อัตโนมัติ ; ผู้ใช้ลากกำหนดทิศเอง = ตั้งเอง
    if (yaw === undefined) {                                   // ทิศตั้งต้น = ทิศเดินทางจากจุดก่อนหน้า (จุดแรก = 0)
      var last = pts[pts.length - 1];
      yaw = last ? Math.atan2(y - last.y, x - last.x) : 0;
    }
    pts.push({ name: t('wp.point_default', { n: String(pts.length + 1) }), x: x, y: y, yaw: yaw, wait: 0, auto: autoHeading, stop: true });
    wp.sel = pts.length - 1;
    wpTouch();
    return wp.sel;
  }

  // ย่อเส้นที่ลาก (Douglas–Peucker แบบไม่เรียกซ้ำ) : คงจุดต้น/ท้าย ทิ้งจุดที่อยู่ใกล้แนวตรงระหว่างจุดที่เหลือไม่เกิน eps เมตร
  function wpSimplify(pts, eps) {
    if (pts.length < 3) { return pts.slice(); }
    var keep = new Array(pts.length), stack = [[0, pts.length - 1]];
    keep[0] = keep[pts.length - 1] = true;
    function offLine(p, a, b) {
      var dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy);
      return len === 0 ? Math.hypot(p.x - a.x, p.y - a.y) : Math.abs(dy * (p.x - a.x) - dx * (p.y - a.y)) / len;
    }
    while (stack.length) {
      var seg = stack.pop(), far = -1, farD = eps;
      for (var i = seg[0] + 1; i < seg[1]; i++) {
        var d = offLine(pts[i], pts[seg[0]], pts[seg[1]]);
        if (d > farD) { far = i; farD = d; }
      }
      if (far >= 0) { keep[far] = true; stack.push([seg[0], far], [far, seg[1]]); }
    }
    return pts.filter(function (_p, k) { return keep[k]; });
  }

  // เส้นที่ลาก → จุดของ Route : ย่อเส้น แล้วทิ้งจุดที่ถี่เกินไป (ยังต้องรวมจุดปลายเสมอ)
  function wpPathPoints(stroke) {
    if (!stroke || stroke.length < 2) { return []; }
    var s = wpSimplify(stroke, WP_PATH_EPS_M), out = [s[0]];
    for (var i = 1; i < s.length; i++) {
      var prev = out[out.length - 1];
      if (Math.hypot(s[i].x - prev.x, s[i].y - prev.y) >= WP_PATH_SPACING_M) { out.push(s[i]); }
      else if (i === s.length - 1 && out.length > 1) { out[out.length - 1] = s[i]; }       // จุดปลายอยู่ใกล้จุดก่อนหน้า → ใช้จุดปลายแทน
    }
    return out;
  }

  function wpAddPath(stroke) {
    var pts = wpPathPoints(stroke);
    if (pts.length < 2) { toast(t('wp.path.short')); drawMap(); return; }
    var max = wpLimit('max_points', 50), room = max - wp.draft.points.length;
    if (room <= 0) { toast(t('wp.max_points', { max: String(max) })); drawMap(); return; }
    var use = pts.slice(0, room);
    use.forEach(function (p, i) {
      var next = use[i + 1] || null, prev = use[i - 1] || null;
      var yaw = next ? Math.atan2(next.y - p.y, next.x - p.x) : (prev ? Math.atan2(p.y - prev.y, p.x - prev.x) : 0);   // ท้ายสุดคงทิศเดิม
      wp.draft.points.push({ name: t('wp.point_default', { n: String(wp.draft.points.length + 1) }), x: p.x, y: p.y, yaw: yaw, wait: 0,
                            auto: true, stop: true });
    });
    wp.sel = wp.draft.points.length - 1;
    wpTouch();
    toast(use.length < pts.length ? t('wp.path.limit', { n: String(use.length) }) : t('wp.path.added', { n: String(use.length) }));
  }

  function wpRemove(i) {
    var pts = wp.draft.points;
    if (i < 0 || i >= pts.length) { return; }
    pts.splice(i, 1);
    wp.sel = pts.length ? Math.min(i, pts.length - 1) : -1;
    wpTouch();
  }

  function wpMoveRow(i, dir) {
    var pts = wp.draft.points, j = i + dir;
    if (j < 0 || j >= pts.length) { return; }
    var tmp = pts[i]; pts[i] = pts[j]; pts[j] = tmp;
    wp.sel = j;
    wpTouch();
  }

  // ── บันทึก / ลบ ────────────────────────────────────────────────────────────────────────────────
  function wpBody() {
    var d = wp.draft;
    return { name: d.name, previous: d.previous, loop: d.loop, mode: d.mode, points: d.points.map(function (p) {
      return { name: p.name, x: p.x, y: p.y, yaw: p.yaw, wait: p.wait, auto: !!p.auto, stop: p.stop !== false };
    }) };
  }

  function wpSave() {
    if (!wp.draft || wp.busy || !wpEditable() || wpIssue()) { return; }
    wp.busy = true;
    wp.error = null;
    wpRender();
    return api('/api/routes', wpBody()).then(function (r) {
      wp.busy = false;
      if (r.status === 200 && r.data && r.data.ok) {
        var name = wp.draft.name.split(/\s+/).filter(Boolean).join(' ');          // เซิร์ฟเวอร์รวมช่องว่างซ้ำให้
        wp.routes = r.data.routes || {};
        wpDataKey = wpSig(r.data);
        wp.draft = wp.routes[name] ? wpClone(wp.routes[name], name) : null;
        wp.dirty = false; wp.badIdx = -1;
        wp.sel = (wp.draft && wp.draft.points.length) ? Math.min(Math.max(wp.sel, 0), wp.draft.points.length - 1) : -1;
        toast(t('toast.wp_saved'));
      } else if (r.status === 401) {
        libExpired();
      } else {
        var e = r.data && r.data.error;
        wp.error = e || { code: 'toast.wp_failed', params: {} };                  // เก็บเป็นข้อความ backend: แปลตอนวาด (สลับภาษาแล้วตามทัน)
        var n = e && e.params && e.params.n;
        wp.badIdx = (typeof n === 'number' && n >= 1) ? n - 1 : -1;                // ชี้จุดที่ไม่ผ่านบนแผนที่
        if (wp.badIdx >= 0) { wp.sel = wp.badIdx; }
      }
      wpRender();
      drawMap();
    });
  }

  function wpDeleteRoute() {
    var d = wp.draft;
    if (!d || !d.previous || wp.busy || !wpEditable()) { return; }
    confirmDialog(t('wp.del.title'), t('wp.del.msg', { name: d.previous }), t('ui.wp.delete'), { warn: true }).then(function (ok) {
      if (!ok) { return; }
      wp.busy = true;
      wpRender();
      api('/api/routes/delete', { name: d.previous }).then(function (r) {
        wp.busy = false;
        if (r.status === 200 && r.data && r.data.ok) {
          wp.routes = r.data.routes || {};
          wpDataKey = wpSig(r.data);
          wp.draft = null; wp.dirty = false; wp.sel = -1; wp.badIdx = -1; wp.error = null;
          toast(t('toast.wp_deleted'));
        } else if (r.status === 401) { libExpired(); }
        else { toast(tm(r.data && r.data.error) || t('toast.wp_failed')); }
        wpRender();
        drawMap();
      });
    });
  }

  // ── วาดหน้าจอ ──────────────────────────────────────────────────────────────────────────────────
  function wpSetValue(el, v) { if (document.activeElement !== el) { el.value = v; } }

  function wpClear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }

  function wpRenderRoutes() {
    var box = $('wp-routes');
    wpClear(box);
    $('wp-map-name').textContent = wp.map ? t('wp.map_name', { name: wp.map }) : t('wp.no_map');
    var names = Object.keys(wp.routes).sort();
    names.forEach(function (name) {
      var r = wp.routes[name];
      var b = libEl('button', 'wp-route');
      b.setAttribute('type', 'button');
      b.setAttribute('aria-current', (wp.draft && wp.draft.previous === name) ? 'true' : 'false');
      b.appendChild(libEl('strong', '', name));
      b.appendChild(libEl('small', '', t(r.points.length === 1 ? 'wp.route_meta.one' : 'wp.route_meta', { n: String(r.points.length), loop: wpLoopText(r.loop) })));
      b.addEventListener('click', function () { wpOpen(name); });
      box.appendChild(b);
    });
    $('wp-routes-empty').hidden = !(wp.map && !names.length);
  }

  function wpRenderLoop() {
    var sel = $('wp-loop');
    var loops = WP_LOOPS.slice();
    if (wp.draft && loops.indexOf(wp.draft.loop) < 0) { loops.push(wp.draft.loop); }   // ค่าที่ไม่อยู่ในรายการมาตรฐานก็ต้องเห็น
    wpClear(sel);
    loops.forEach(function (v) {
      var o = libEl('option', '', wpLoopText(v));
      o.value = String(v);
      sel.appendChild(o);
    });
    sel.value = String(wp.draft ? wp.draft.loop : 1);
  }

  function wpRenderMode() {                                    // โหมดการวิ่งของ Route + คำอธิบายสั้นๆ ของโหมดที่เลือก
    var sel = $('wp-mode');
    wpClear(sel);
    WP_MODES.forEach(function (m) {
      var o = libEl('option', '', t('wp.mode.' + m));
      o.value = m;
      sel.appendChild(o);
    });
    var mode = wp.draft && wp.draft.mode === 'straight' ? 'straight' : 'navigate';
    sel.value = mode;
    $('wp-mode-help').textContent = t('wp.mode.help.' + mode);
  }

  function wpRenderPoints() {
    var ol = $('wp-points');
    wpClear(ol);
    var pts = wp.draft ? wp.draft.points : [];
    var ed = wpEditable();
    pts.forEach(function (p, i) {
      var bad = (i === wp.badIdx);
      var li = libEl('li', 'wp-point');
      li.setAttribute('data-selected', i === wp.sel ? 'true' : 'false');
      li.setAttribute('data-bad', bad ? 'true' : 'false');
      var main = libEl('button', 'wp-point-main');
      main.setAttribute('type', 'button');
      main.setAttribute('aria-pressed', i === wp.sel ? 'true' : 'false');
      main.appendChild(libEl('span', 'wp-num', bad ? MARK.bad : String(i + 1)));
      var text = libEl('span', 'wp-point-text');
      text.appendChild(libEl('span', '', p.name));
      var passing = !!(wp.draft && wp.draft.mode === 'straight' && p.stop === false);
      text.appendChild(libEl('small', '', passing ? t('wp.point_meta_pass', { x: p.x.toFixed(2), y: p.y.toFixed(2) })
        : t('wp.point_meta', { s: fmtNum(p.wait), x: p.x.toFixed(2), y: p.y.toFixed(2) })));
      main.appendChild(text);
      main.addEventListener('click', function () { wp.sel = i; wpRender(); drawMap(); });
      li.appendChild(main);
      [[-1, '▲', 'wp.move_up'], [1, '▼', 'wp.move_down']].forEach(function (m) {
        var b = libEl('button', 'wp-move', m[1]);
        b.setAttribute('type', 'button');
        b.setAttribute('aria-label', t(m[2], { n: String(i + 1) }));
        b.disabled = !ed || (i + m[0] < 0) || (i + m[0] >= pts.length);
        b.addEventListener('click', function () { wpMoveRow(i, m[0]); });
        li.appendChild(b);
      });
      ol.appendChild(li);
    });
    $('wp-points-empty').hidden = !(wp.draft && !pts.length);
  }

  function wpRenderDetail() {
    var p = wp.draft && wp.draft.points[wp.sel];
    $('wp-detail').hidden = !p;
    if (!p) { return; }
    wpSetValue($('wp-pname'), p.name);
    wpSetValue($('wp-pwait'), fmtNum(p.wait));
    wpSetValue($('wp-px'), p.x.toFixed(2));
    wpSetValue($('wp-py'), p.y.toFixed(2));
    wpSetValue($('wp-pyaw'), String(poseDeg(p.yaw)));
    $('wp-pauto').checked = !!p.auto;
    $('wp-pstop').checked = p.stop !== false;
  }

  function wpRender() {                                        // วาดรายการทั้งหมดใหม่ (เรียกเมื่อข้อมูล/ร่าง/ภาษาเปลี่ยน)
    wpRenderRoutes();
    $('wp-form').hidden = !wp.draft;
    $('wp-editor-title').textContent = wp.draft
      ? t(wp.draft.points.length === 1 ? 'wp.editor_title.one' : 'wp.editor_title', { name: wp.draft.name.trim() || t('wp.untitled'),
                                                                                n: String(wp.draft.points.length) })
      : t('ui.wp.points');
    if (wp.draft) {
      wpSetValue($('wp-name'), wp.draft.name);
      wpRenderLoop();
      wpRenderMode();
      wpRenderPoints();
      wpRenderDetail();
    }
    renderLine();                                              // รายการจุดของการ์ด Straight line ตามข้อมูล Route ล่าสุด
    wpCtlKey = wpControlsKey();                                // วาดรายการใหม่แล้ว ไม่ต้องวาดซ้ำ
    wpRenderControls();
  }

  function wpControlsKey() { return [wpEditable(), !!wp.draft, wp.mapReady, runBusy()].join('|'); }

  function wpRenderControls() {                                // เปิด/ปิดปุ่ม + ข้อความสถานะ (เบา เรียกได้ถี่)
    var ed = wpEditable();
    var d = wp.draft;
    if (wpControlsKey() !== wpCtlKey) {
      wpCtlKey = wpControlsKey();
      if (d) { wpRenderPoints(); }                             // ปุ่ม ▲▼ ขึ้นกับสิทธิ์
    }
    $('wp-new').disabled = !(ed && wp.mapReady);
    $('wp-center').disabled = !(navSettled() && lastStatus && lastStatus.pose);       // หุ่นอยู่บนแผนที่นี้เฉพาะตอน Navigation รัน
    var note = '';
    if (authEnabled && !admin) { note = t('wp.need_admin'); }
    else if (!wp.map) { note = t('wp.need_map'); }
    else if (!wp.mapReady) { note = t('wp.map_unreadable', { name: wp.map }); }
    $('wp-left-note').textContent = note;
    $('wp-left-note').hidden = !note;
    $('wp-note').textContent = note;
    $('wp-note').hidden = !(note && d);
    var draw = wpCanDraw();
    ['select', 'add', 'path', 'delete'].forEach(function (name) {
      var b = $('wp-tool-' + name);
      b.setAttribute('aria-pressed', wp.tool === name ? 'true' : 'false');
      b.disabled = !draw;
    });
    var cv = $('wp-map-canvas');
    cv.classList.toggle('wp-add', draw && wp.tool === 'add');
    cv.classList.toggle('wp-path', draw && wp.tool === 'path');
    cv.classList.toggle('wp-delete', draw && wp.tool === 'delete');
    $('wp-help').textContent = !d ? t('wp.help.pick') : (!ed ? t('wp.help.view') : t('wp.help.' + wp.tool));
    ['wp-name', 'wp-loop', 'wp-mode', 'wp-pname', 'wp-pwait', 'wp-px', 'wp-py'].forEach(function (id) { $(id).disabled = !ed; });
    var sp = d && d.points[wp.sel];
    var manualYaw = !!(ed && !(sp && sp.auto));                  // ทิศอัตโนมัติ = ช่องทิศ/ปุ่มหมุนล็อก (เอาติ๊กออกก่อนถึงจะตั้งเองได้)
    var last = d ? d.points.length - 1 : -1;
    var canPass = !!(ed && d && d.mode === 'straight' && sp && wp.sel > 0 && wp.sel < last);   // จุดแรก/สุดท้ายหยุดเสมอ ; ผ่านไปเลยมีเฉพาะโหมด straight
    var passing = !!(d && d.mode === 'straight' && sp && sp.stop === false);
    $('wp-pstop').disabled = !canPass;
    $('wp-pwait').disabled = !ed || passing;                     // จุดผ่านไปเลยไม่หยุด จึงไม่มีเวลารอ
    var stopNote = !sp ? '' : (d.mode !== 'straight' ? t('wp.stop_note.navigate') : ((wp.sel === 0 || wp.sel === last) ? t('wp.stop_note.ends') : ''));
    $('wp-pstop-note').textContent = stopNote;
    $('wp-pstop-note').hidden = !stopNote;
    $('wp-pyaw').disabled = !manualYaw;
    ['wp-rot-l', 'wp-rot-r'].forEach(function (id) { $(id).disabled = !manualYaw; });
    $('wp-del-point').disabled = !ed;
    $('wp-pauto').disabled = !ed;
    $('wp-auto-all').disabled = !(ed && d && d.points.length > 1);
    var issue = d ? wpIssue() : 'none';
    $('wp-save').disabled = !(ed && wp.mapReady && d && wp.dirty && !issue);
    $('wp-discard').disabled = !(d && wp.dirty && !wp.busy);
    $('wp-delete').disabled = !(ed && d && d.previous);
    var status = '';
    if (d && wp.dirty) { status = MARK.warn + ' ' + t('wp.status.unsaved') + (issue ? ' — ' + t('wp.issue.' + issue) : ''); }
    else if (d && d.previous) { status = MARK.ok + ' ' + t('wp.status.saved'); }
    else if (d && issue) { status = t('wp.issue.' + issue); }
    $('wp-status').textContent = status;
    $('wp-error').textContent = tm(wp.error);
    $('wp-error').hidden = !wp.error;
    var p = d && d.points[wp.sel];
    $('wp-readout').hidden = !p;
    renderRun();                                                // ปุ่มเริ่มรันขึ้นกับร่าง/สิทธิ์/สถานะหุ่นด้วย
    $('wp-readout').textContent = p ? t(p.auto ? 'wp.readout_auto' : 'wp.readout', { n: String(wp.sel + 1), x: p.x.toFixed(2), y: p.y.toFixed(2),
                                                         deg: String(poseDeg(p.yaw)) }) : '';
  }

  // ข้อความบนเกตแทนแผนที่ (ยังไม่มีแผนที่ที่บันทึกไว้ให้ใช้)
  function wpGateMsg() {
    if (!connected) { return t('nav.gate.disconnected'); }
    if (!wp.loaded) { return t('wp.gate.loading'); }
    if (!wp.map) { return t('wp.gate.no_map'); }
    return t('wp.gate.unreadable', { name: wp.map });
  }

  function renderWpMap() {                                     // เรียกจาก renderMapAny (ถี่ ~5 Hz) : เบาไว้
    if (!onWpMapPage()) { return; }
    var live = wpMapReady();
    $('wp-map-live').hidden = !live;
    $('wp-map-gate').hidden = live;
    if (!live) {
      $('wp-map-gate-msg').textContent = wpGateMsg();
      if (mapOwner === 'wp') { resetMapData(); mapOwner = ''; }                // ไม่ค้างแผนที่เก่า
      mapView.auto = true;
      wp.drag = null;
      wpRenderControls();
      return;
    }
    claimMapData('wp');
    var has = !!(mapMeta && mapMeta.available);
    $('wp-map-wait').hidden = has;
    $('wp-map-info').textContent = has ? t('map.info', {
      w: mapMeta.size_m.w.toFixed(1), h: mapMeta.size_m.h.toFixed(1), res: String(mapMeta.resolution),
      time: hhmm(mapMeta.stamp) }) : '';
    wpRenderControls();
    drawMap();
  }

  function drawWpRoute(ctx, X, Y) {                            // เส้นเชื่อมจุด + วงกลมเลขลำดับ + ลูกศรทิศ + ชื่อ (วาดทับแผนที่)
    var d = wp.draft;
    if (wp.stroke && wp.stroke.length > 1) {                    // เส้นที่กำลังลาก (ยังไม่เป็นจุด) : สีส้ม
      ctx.save();
      ctx.strokeStyle = cssColor('--mode-map', '#eb6834');
      ctx.lineWidth = 4;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.beginPath();
      wp.stroke.forEach(function (q, i) { if (i) { ctx.lineTo(X(q.x), Y(q.y)); } else { ctx.moveTo(X(q.x), Y(q.y)); } });
      ctx.stroke();
      ctx.restore();
    }
    if (!d || !d.points.length) { return; }
    var pts = d.points;
    var base = cssColor('--series', '#2a78d6');
    var hl = runHighlight();                                    // ระหว่างรัน: จุดที่ผ่านแล้วจาง · จุดที่กำลังไปเป็นสีเขียว
    ctx.save();
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.strokeStyle = base;
    ctx.lineWidth = 3;
    if (ctx.setLineDash) { ctx.setLineDash(d.mode === 'straight' ? [] : [10, 6]); }   // straight = เส้นทึบ (ตรงจริง) · navigate = เส้นประ (Nav2 วางเอง)
    ctx.beginPath();
    pts.forEach(function (p, i) { if (i) { ctx.lineTo(X(p.x), Y(p.y)); } else { ctx.moveTo(X(p.x), Y(p.y)); } });
    ctx.stroke();
    if (d.loop !== 1 && pts.length > 1) {                      // วนซ้ำ: เส้นจุดจากจุดท้ายกลับจุดแรก
      ctx.globalAlpha = 0.55;
      ctx.lineWidth = 2;
      if (ctx.setLineDash) { ctx.setLineDash([2, 7]); }
      ctx.beginPath();
      ctx.moveTo(X(pts[pts.length - 1].x), Y(pts[pts.length - 1].y));
      ctx.lineTo(X(pts[0].x), Y(pts[0].y));
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    if (ctx.setLineDash) { ctx.setLineDash([]); }
    pts.forEach(function (p, i) {
      var bad = (i === wp.badIdx), on = (i === wp.sel);
      var next = !!(hl && i === hl.index), done = !!(hl && i < hl.index);
      var color = bad ? cssColor('--bad', '#cf222e') : (next ? cssColor('--ok', '#1a7f37') : (on ? cssColor('--mode-map', '#eb6834') : base));
      var cx = X(p.x), cy = Y(p.y), r = (on || next) ? 15 : 13;
      if (d.mode === 'straight' && p.stop === false) { r = (on || next) ? 11 : 9; }      // จุดผ่านไปเลย = วงเล็กกว่าจุดหยุด
      ctx.globalAlpha = done ? 0.45 : 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(p.yaw) * (r + 16), cy - Math.sin(p.yaw) * (r + 16));    // จอ y ลง = บวก ส่วนทิศโลก y ขึ้น = บวก
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = cssColor('--card', '#ffffff');
      ctx.stroke();
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 13px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(bad ? MARK.bad : String(i + 1), cx, cy + 0.5);
      ctx.font = '600 12px sans-serif';
      ctx.textBaseline = 'bottom';
      ctx.lineWidth = 3;
      ctx.strokeStyle = cssColor('--card', '#ffffff');
      ctx.strokeText(p.name, cx, cy - r - 3);                  // ขอบสีพื้น: อ่านออกทั้งบนพื้นขาว/ดำ
      ctx.fillStyle = cssColor('--text', '#18202b');
      ctx.fillText(p.name, cx, cy - r - 3);
    });
    ctx.restore();
  }

  // ── แตะ/ลากบนแผนที่ : เพิ่มจุด (แตะ = ตำแหน่ง · ลาก = ทิศ) · เลือก/ย้าย · ลบ ; สองนิ้ว = ซูม/เลื่อนแผนที่ ──────────────
  function wpDown(cv, id, p) {
    if (cv.id !== 'wp-map-canvas' || !wpCanDraw()) { return; }
    if (Object.keys(mapPointers).length !== 1) { wp.drag = null; return; }       // นิ้วที่สอง = ซูม/เลื่อน
    if (wp.tool === 'path') {                                                     // Draw path: เริ่มเส้นใหม่ที่นิ้วแตะ
      wp.stroke = [screenToWorld(p.x, p.y)];
      wp.drag = { id: id, path: true };
      return;
    }
    var hit = wpHit(p);
    if (wp.tool === 'add') { wp.drag = { id: id, sx: p.x, sy: p.y, idx: -1, placed: false }; return; }
    if (hit < 0) { wp.drag = null; return; }                                      // แตะที่ว่าง = เลื่อนแผนที่ตามปกติ
    wp.drag = { id: id, sx: p.x, sy: p.y, idx: hit, moved: false };
    if (wp.tool === 'select') { wp.sel = hit; wpRender(); drawMap(); }
  }

  // คืนจริงถ้า "ใช้การเคลื่อนไหวนี้แล้ว" (ผู้เรียกต้องไม่เลื่อนแผนที่ซ้ำ)
  function wpMove(cv, id, p, pointers) {
    if (cv.id !== 'wp-map-canvas') { return false; }
    if (pointers !== 1) { wp.drag = null; wp.stroke = null; return false; }
    var d = wp.drag;
    if (!d || d.id !== id) { return false; }
    if (d.path) {                                                                 // เก็บจุดตัวอย่างตามนิ้ว (ยังไม่เปลี่ยนร่าง)
      var q = screenToWorld(p.x, p.y), last = wp.stroke[wp.stroke.length - 1];
      if (Math.hypot(q.x - last.x, q.y - last.y) >= WP_PATH_SAMPLE_M) { wp.stroke.push(q); drawMap(); }
      return true;
    }
    var dx = p.x - d.sx, dy = p.y - d.sy;
    if (wp.tool === 'add') {
      if (Math.hypot(dx, dy) >= POSE_DRAG_MIN_PX) {
        if (!d.placed) {
          var w = screenToWorld(d.sx, d.sy);
          d.idx = wpPlace(w.x, w.y, 0);
          d.placed = true;
        }
        if (d.idx >= 0) {                                                         // ลากกำหนดทิศเอง = ไม่ใช่ทิศอัตโนมัติ
          wp.draft.points[d.idx].yaw = Math.atan2(-dy, dx);
          wp.draft.points[d.idx].auto = false;
          wpTouch();
        }
      }
    } else if (wp.tool === 'select' && d.idx >= 0) {
      if (d.moved || Math.hypot(dx, dy) >= WP_MOVE_MIN_PX) {
        d.moved = true;
        var q = screenToWorld(p.x, p.y);
        wp.draft.points[d.idx].x = q.x;
        wp.draft.points[d.idx].y = q.y;
        wpTouch();
      }
    }
    return true;
  }

  function wpUp(id, cancelled) {
    var d = wp.drag;
    if (!d || d.id !== id) { return; }
    wp.drag = null;
    if (d.path) {                                                                 // ยกนิ้ว = แปลงเส้นเป็นจุดของ Route
      var stroke = wp.stroke;
      wp.stroke = null;
      if (!cancelled && wpCanDraw()) { wpAddPath(stroke); } else { drawMap(); }
      return;
    }
    if (cancelled || !wpCanDraw()) { return; }
    if (wp.tool === 'add' && !d.placed) {                      // แตะแล้วยกนิ้ว = วางตรงจุดนั้น
      var w = screenToWorld(d.sx, d.sy);
      wpPlace(w.x, w.y);
    } else if (wp.tool === 'delete' && d.idx >= 0) {
      wpRemove(d.idx);
    }
  }

  function wpKey(cv, key) {
    if (cv.id !== 'wp-map-canvas' || !wpCanDraw()) { return false; }
    if ((key === 'Delete' || key === 'Backspace') && wp.sel >= 0) { wpRemove(wp.sel); return true; }
    return false;
  }

  function wpSetTool(name) {
    wp.tool = name;
    wp.drag = null;
    wp.stroke = null;
    wpRenderControls();
  }

  ['select', 'add', 'path', 'delete'].forEach(function (name) {
    $('wp-tool-' + name).addEventListener('click', function () { wpSetTool(name); });
  });
  $('wp-new').addEventListener('click', wpNew);
  $('wp-save').addEventListener('click', wpSave);
  $('wp-discard').addEventListener('click', wpDiscard);
  $('wp-delete').addEventListener('click', wpDeleteRoute);
  $('wp-pauto').addEventListener('change', function () {
    var p = wp.draft && wp.draft.points[wp.sel];
    if (p && wpEditable()) { p.auto = $('wp-pauto').checked; wpTouch(); }
  });
  $('wp-auto-all').addEventListener('click', function () {
    if (!wp.draft || !wpEditable()) { return; }
    wp.draft.points.forEach(function (q) { q.auto = true; });
    wpTouch();
    toast(t('wp.auto_all_done'));
  });
  $('wp-del-point').addEventListener('click', function () { if (wp.draft) { wpRemove(wp.sel); } });
  $('wp-rot-l').addEventListener('click', function () { wpSpin(1); });
  $('wp-rot-r').addEventListener('click', function () { wpSpin(-1); });
  $('wp-name').addEventListener('input', function () { if (wp.draft) { wp.draft.name = $('wp-name').value; wpTouch(); } });
  $('wp-loop').addEventListener('change', function () { if (wp.draft) { wp.draft.loop = Number($('wp-loop').value); wpTouch(); } });
  $('wp-mode').addEventListener('change', function () {
    if (wp.draft && WP_MODES.indexOf($('wp-mode').value) >= 0) {
      wp.draft.mode = $('wp-mode').value;
      if (wp.draft.mode !== 'straight') { wp.draft.points.forEach(function (q) { q.stop = true; }); }   // โหมดเดิมไม่มีจุดผ่านไปเลย
      wpRenderMode();
      wpTouch();
    }
  });
  $('wp-pstop').addEventListener('change', function () {
    var p = wp.draft && wp.draft.points[wp.sel];
    if (p && wpEditable() && wp.draft.mode === 'straight' && wp.sel > 0 && wp.sel < wp.draft.points.length - 1) {
      p.stop = $('wp-pstop').checked;
      wpTouch();
    }
  });
  $('wp-pname').addEventListener('input', function () {
    var p = wp.draft && wp.draft.points[wp.sel];
    if (p) { p.name = $('wp-pname').value; wpTouch(); }
  });

  function wpSpin(sign) {
    var p = wp.draft && wp.draft.points[wp.sel];
    if (!p || p.auto || !wpEditable()) { return; }
    p.yaw = wrapYaw(p.yaw + sign * POSE_ROT_STEP);
    wpTouch();
  }

  function wpNumberInput(id, apply) {                          // ช่องตัวเลข: ค่าที่ยังไม่เป็นตัวเลขไม่แตะร่าง
    $(id).addEventListener('input', function () {
      var p = wp.draft && wp.draft.points[wp.sel];
      var raw = $(id).value.trim();
      var v = Number(raw);
      if (!p || raw === '' || !isFinite(v)) { return; }
      apply(p, v);
      wpTouch();
    });
  }
  wpNumberInput('wp-pwait', function (p, v) { p.wait = Math.max(0, Math.min(wpLimit('max_wait', 600), v)); });
  wpNumberInput('wp-px', function (p, v) { p.x = v; });
  wpNumberInput('wp-py', function (p, v) { p.y = v; });
  wpNumberInput('wp-pyaw', function (p, v) { p.yaw = wrapYaw(v * Math.PI / 180); });

  window.addEventListener('beforeunload', function (ev) {      // ปิดแท็บ/รีเฟรชทั้งที่ยังไม่ได้บันทึก Route
    if (wp.dirty) { ev.preventDefault(); ev.returnValue = ''; }
  });

