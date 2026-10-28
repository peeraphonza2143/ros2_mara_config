  // ── Straight line (หน้า Waypoint) : วิ่งตรงจากจุดหนึ่งไปอีกจุดหนึ่ง "ตรงจริง" ไม่ผ่าน planner ──────────────────────
  // เซิร์ฟเวอร์สร้างเส้นตรงเองแล้วให้ตัวควบคุมตามเส้นนั้น (nav_goal.send_line) ; กติกาความปลอดภัยจริงตัดสินที่เซิร์ฟเวอร์ :
  // ทั้งแนวต้องว่างและห่างผนัง · หุ่นต้องอยู่ที่จุดเริ่มและหันตรงแนว · ทีละงานเดียว · ต้องยืนยันทุกครั้ง
  // หน้านี้แค่เลือกจุด (จาก Route ที่บันทึกไว้) ตรวจล่วงหน้า สั่ง/หยุด และแสดงสถานะที่โพลมา (ทุก 1 วินาทีเฉพาะตอนเปิดหน้า Waypoint)
  var LINE_POLL_MS = 1000;
  var LINE_ROBOT = 'robot';                                  // ค่าของตัวเลือก "ตำแหน่งหุ่นตอนนี้"
  var LINE_SEP = '\u0001';
  var lineSt = null;                                         // ผล GET /api/nav/goal ล่าสุด
  var lineChoices = [];                                      // [{key, label, x, y, yaw}] จากทุก Route ที่บันทึกไว้
  var lineSel = { from: LINE_ROBOT, to: '' };                // key ที่เลือก ('' = ยังไม่เลือก)
  var lineCheckRes = null;                                   // { from, to, check } ผลตรวจล่าสุด (ล้างเมื่อเปลี่ยนที่เลือก)
  var lineReq = false;                                       // กำลังรอคำตอบของคำสั่ง (กันกดซ้ำ)
  var lineKey = '';                                          // ลายนิ้วมือของรายการที่วาดอยู่ (ไม่วาดซ้ำถ้าไม่เปลี่ยน)
  var lineLast = { seq: 0, busy: false };                    // จำไว้แจ้งผลครั้งเดียวตอนจบ
  var LINE_CODE = { outside: 'nav.err.straight_outside', unknown: 'nav.err.straight_unknown',
                    occupied: 'nav.err.straight_unknown', tight: 'nav.err.straight_tight', no_map: 'nav.err.no_map' };

  function lineGoal() { return (lineSt && lineSt.goal) || null; }

  function lineBusy() { var g = lineGoal(); return !!(g && g.line && GOAL_BUSY.indexOf(g.state) >= 0); }   // เฉพาะงานที่เป็นเส้นตรง (goal ปกติ = goalBusy)

  function lineBuildChoices() {                              // ทุกจุดของทุก Route ที่บันทึกไว้ (เรียงตามชื่อ Route แล้วลำดับจุด)
    var out = [];
    Object.keys(wp.routes).sort().forEach(function (route) {
      wp.routes[route].points.forEach(function (p, i) {
        out.push({ key: route + LINE_SEP + i, label: route + ' · ' + p.name, x: p.x, y: p.y, yaw: p.yaw });
      });
    });
    return out;
  }

  function lineChoice(key) {
    for (var i = 0; i < lineChoices.length; i++) { if (lineChoices[i].key === key) { return lineChoices[i]; } }
    return null;
  }

  function lineFill(sel, first, value) {                     // เติมตัวเลือก (ไม่ใช้ innerHTML) แล้วเลือก value ถ้ายังมีอยู่
    wpClear(sel);
    var o0 = libEl('option', '', first.text);
    o0.value = first.value;
    sel.appendChild(o0);
    lineChoices.forEach(function (c) {
      var o = libEl('option', '', c.label);
      o.value = c.key;
      sel.appendChild(o);
    });
    sel.value = value;
  }

  function lineRebuild(force) {
    lineChoices = lineBuildChoices();
    var key = [I18N.getLang(), lineChoices.map(function (c) { return c.key + '|' + c.label + '|' + c.x + '|' + c.y; }).join(';')].join('#');
    if (!force && key === lineKey) { return; }
    lineKey = key;
    if (lineSel.from !== LINE_ROBOT && !lineChoice(lineSel.from)) { lineSel.from = LINE_ROBOT; lineCheckRes = null; }
    if (lineSel.to && !lineChoice(lineSel.to)) { lineSel.to = ''; lineCheckRes = null; }
    lineFill($('line-from'), { value: LINE_ROBOT, text: t('line.from_robot') }, lineSel.from);
    lineFill($('line-to'), { value: '', text: t('line.to_pick') }, lineSel.to);
  }

  function lineEnds() {                                      // { from: {x,y}|null, to: {x,y,yaw} } ตามที่เลือก (null = ยังเลือกไม่ครบ)
    var to = lineChoice(lineSel.to);
    if (!to) { return null; }
    var from = lineSel.from === LINE_ROBOT ? null : lineChoice(lineSel.from);
    if (lineSel.from !== LINE_ROBOT && !from) { return null; }
    return { from: from ? { x: from.x, y: from.y } : null, to: { x: to.x, y: to.y, yaw: to.yaw } };
  }

  function lineLength(ends) {
    var p = lastStatus && lastStatus.pose;
    var a = ends.from || (p ? { x: p.x, y: p.y } : null);
    return a ? Math.hypot(ends.to.x - a.x, ends.to.y - a.y) : null;
  }

  function lineRunReason() {                                 // เหตุผลที่ยังสั่งไม่ได้ ('' = สั่งได้)
    if (!admin || !authEnabled) { return t('line.need_admin'); }
    if (!navSettled()) { return t('line.need_nav'); }
    if (runBusy()) { return t('line.busy_route'); }
    if (lineBusy() || goalBusy()) { return t('run.busy_goal'); }
    if (!lineEnds()) { return t('line.need_to'); }
    if (lineSel.from === lineSel.to) { return t('line.same'); }
    if (!goalLocOk()) { return t('navgoal.need_loc'); }
    return '';
  }

  function lineResultText() {
    var r = lineCheckRes;
    if (!r || r.from !== lineSel.from || r.to !== lineSel.to) { return ''; }
    var c = r.check;
    if (c.ok) {
      var text = MARK.ok + ' ' + t('line.check.ok', { len: c.length.toFixed(2) });
      if (c.soft_risk) { text += ' ' + MARK.warn + ' ' + t('line.check.near_wall', { d: c.min_clearance === null ? '–' : c.min_clearance.toFixed(2) }); }
      return text;
    }
    if (c.reason) { return MARK.bad + ' ' + t(LINE_CODE[c.reason], { at: c.at, clearance: lineClearance() }); }
    if (c.start.distance > c.start.max_distance) {
      return MARK.bad + ' ' + t('nav.err.line_start_far', { found: c.start.distance.toFixed(2), max: c.start.max_distance });
    }
    return MARK.bad + ' ' + t('nav.err.line_heading', { found: Math.round(c.start.heading_deg), max: c.start.max_heading_deg });
  }

  function lineClearance() {
    var l = goalSt && goalSt.limits;
    return l && l.clearance ? l.clearance : 0.4;
  }

  function lineStatusText() {
    var g = lineGoal();
    if (!g || !g.line || g.state === 'idle') { return { text: '', detail: '' }; }
    var text = MARK[{ sending: 'off', active: 'warn', canceling: 'warn', succeeded: 'ok', canceled: 'off', failed: 'bad' }[g.state]] +
      ' ' + t('line.st.' + g.state);
    if (g.state === 'failed' && g.reason) { text += ' — ' + t(g.reason); }
    var fb = g.feedback, detail = '';
    if (GOAL_BUSY.indexOf(g.state) >= 0 && fb && typeof fb.distance_remaining === 'number') {
      detail = t('line.progress', { d: fb.distance_remaining.toFixed(1) });
    }
    if (g.waiting_assumed) { detail += (detail ? ' · ' : '') + t('navgoal.waiting'); }
    return { text: text, detail: detail };
  }

  function renderLine(force) {
    lineRebuild(force);
    var canAct = !!(admin && authEnabled && connected);
    var reason = lineBusy() ? '' : lineRunReason();
    var res = lineResultText();
    $('line-result').textContent = res;
    $('line-result').hidden = !res;
    var st = lineStatusText();
    $('line-status').textContent = st.text;
    $('line-status').hidden = !st.text;
    $('line-detail').textContent = st.detail;
    $('line-detail').hidden = !st.detail;
    $('line-note').textContent = reason;
    $('line-note').hidden = !reason;
    $('line-from').disabled = lineBusy() || lineReq;
    $('line-to').disabled = lineBusy() || lineReq;
    $('line-check').disabled = !(canAct && lineEnds() && !lineReq && !lineBusy());
    $('line-run').hidden = lineBusy();
    $('line-run').disabled = !!(reason || lineReq);
    $('line-stop').hidden = !(lineBusy() && canAct);
  }

  function lineNotice() {                                    // งานจบ (สำเร็จ/ยกเลิก/ล้ม) → แจ้งครั้งเดียวต่อ goal
    var g = lineGoal();
    if (!g || !g.line) { return; }
    var busy = GOAL_BUSY.indexOf(g.state) >= 0;
    if (lineLast.seq === g.seq && lineLast.busy && !busy) { toast(t('navgoal.done.' + g.state)); }
    lineLast = { seq: g.seq, busy: busy };
  }

  function linePoll() {
    return api('/api/nav/goal').then(function (r) {
      if (r.status !== 200 || !r.data) { return; }
      lineSt = goalSt = r.data;                              // ตัวแปรร่วมกับหน้า Navigation (ไม่ให้ค้างเก่าเมื่อมาอยู่หน้านี้)
      lineNotice();
      renderLine();
    });
  }

  function pollLine() {
    if (!onWpMapPage() || !connected) { return; }
    return linePoll();
  }

  function lineBody(ends, confirmed) {
    var body = { from: ends.from, to: ends.to };
    if (confirmed) { body.confirmed = true; }
    return body;
  }

  function lineCheck() {
    var ends = lineEnds();
    if (!ends || lineReq) { return; }
    var snapshot = { from: lineSel.from, to: lineSel.to };
    lineReq = true;
    renderLine();
    return api('/api/nav/line/check', lineBody(ends, false)).then(function (r) {
      lineReq = false;
      if (r.status === 200 && r.data && r.data.check) { lineCheckRes = { from: snapshot.from, to: snapshot.to, check: r.data.check }; }
      else if (r.status === 401) { libExpired(); }
      else { lineCheckRes = null; toast(tm(r.data && r.data.error) || t('toast.line_failed')); }
      renderLine();
    });
  }

  function lineAsk() {                                       // ถามยืนยันทุกครั้ง (เซิร์ฟเวอร์ก็บังคับ confirmed:true ซ้ำ)
    if (lineRunReason() || lineReq) { return; }
    var ends = lineEnds(), len = lineLength(ends);
    var from = lineSel.from === LINE_ROBOT ? t('line.from_robot') : lineChoice(lineSel.from).label;
    var items = [t('line.confirm.item', { from: from, to: lineChoice(lineSel.to).label, len: len === null ? '–' : len.toFixed(2) }),
                 t('line.confirm.turn'), t('confirm.goal.safety')];
    return confirmDialog(t('line.confirm.title'), t('line.confirm.body'), t('line.confirm.ok'), { warn: true, items: items })
      .then(function (ok) { if (ok) { return lineSend(ends); } });
  }

  function lineSend(ends) {
    lineReq = true;
    renderLine();
    return api('/api/nav/line', lineBody(ends, true)).then(function (r) {
      lineReq = false;
      if (r.status === 200 && r.data && r.data.ok) {
        lineSt = { running: true, goal: r.data.goal, limits: lineSt && lineSt.limits };
        lineNotice();
        toast(t('toast.line_started'));
      } else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.line_failed')); }
      renderLine();
    });
  }

  function lineStop() {                                      // หยุดไม่ถามยืนยัน (ปุ่มนิรภัย) : ยกเลิกงานที่ตัวควบคุม + ศูนย์ความเร็ว
    if (!admin || lineReq) { return; }
    lineReq = true;
    renderLine();
    return api('/api/nav/goal/cancel', {}).then(function (r) {
      lineReq = false;
      if (r.status === 200 && r.data && r.data.ok) { lineSt = { running: true, goal: r.data.goal, limits: lineSt && lineSt.limits }; toast(t('toast.line_stopped')); }
      else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.line_stop_failed')); }
      renderLine();
    });
  }

  function lineChanged() { lineCheckRes = null; renderLine(); }

  $('line-from').addEventListener('change', function () { lineSel.from = $('line-from').value; lineChanged(); });
  $('line-to').addEventListener('change', function () { lineSel.to = $('line-to').value; lineChanged(); });
  $('line-check').addEventListener('click', lineCheck);
  $('line-run').addEventListener('click', lineAsk);
  $('line-stop').addEventListener('click', lineStop);
