  // ── รัน Route : ปุ่มเริ่ม / พัก-ทำต่อ / หยุด + แถบสถานะตรึงล่างหน้า Waypoint ────────────────────────────────
  // ตัวรันอยู่ที่เซิร์ฟเวอร์ (route_run.py) — หน้านี้แค่สั่งและแสดงสถานะที่โพลมา (ทุก 1 วินาทีเฉพาะตอนเปิดหน้า Waypoint)
  // กติกาความปลอดภัยจริงตัดสินที่เซิร์ฟเวอร์ ; หน้านี้ช่วยบอกเหตุผลที่ปุ่มเริ่มกดไม่ได้ + ถามยืนยันทุกครั้ง
  var RUN_POLL_MS = 1000;
  var run = { run: null, goal: null, running: false };      // ผล GET /api/routes/run ล่าสุด
  var runLast = { id: 0, busy: false };                     // จำไว้แจ้งผลครั้งเดียวตอน Route จบ
  var runReq = false;                                       // กำลังรอคำตอบของคำสั่ง (กันกดซ้ำ)

  function runActive() { return !!(run.run && (run.run.state === 'running' || run.run.state === 'paused')); }

  function runBusy() { return runActive(); }

  function runHighlight() {                                 // จุดที่กำลังไป/ที่ผ่านแล้ว (เฉพาะ Route ที่เปิดอยู่ในตัวแก้ไข)
    var r = run.run;
    if (!runActive() || !wp.draft || wp.draft.previous !== r.route) { return null; }
    return { index: r.index, phase: r.phase };
  }

  function runRoundText(r) {
    return r.loop === 0 ? t('run.round.forever', { r: String(r.round) })
      : t('run.round', { r: String(r.round), total: String(r.loop) });
  }

  function runStatusText() {
    var r = run.run;
    if (!r || r.state === 'idle') { return t('run.st.idle'); }
    var tag = (r.mode === 'straight' && (r.state === 'running' || r.state === 'paused')) ? ' · ' + t('run.mode.straight') : '';
    return runStatusCore(r) + tag;
  }

  function runStatusCore(r) {
    var base = { route: r.route, i: String(r.index + 1), n: String(r.total), point: r.current ? r.current.name : '',
                 rounds: runRoundText(r) };
    if (r.state === 'running' && r.phase === 'locating') {
      return t('run.st.locating', { i: base.i, n: base.n, s: String(Math.ceil(r.loc_left || 0)), rounds: base.rounds });
    }
    if (r.state === 'running') {
      return r.phase === 'waiting'
        ? t('run.st.waiting', { point: base.point, s: String(Math.ceil(r.wait_left || 0)), i: base.i, n: base.n, rounds: base.rounds })
        : t('run.st.running', base);
    }
    if (r.state === 'paused') { return r.pause_settled ? t('run.st.paused', base) : t('run.st.pausing'); }
    if (r.state === 'succeeded') {
      return t('run.st.succeeded', { route: r.route, rounds: t(r.loop === 1 ? 'run.rounds_done.one' : 'run.rounds_done.many', { n: String(r.loop) }) });
    }
    if (r.state === 'canceled') { return t('run.st.canceled', { route: r.route }) + (r.reason ? ' ' + tm(r.reason) : ''); }
    return t('run.st.failed', { reason: tm(r.reason) });
  }

  function runDetailText() {                                // ระยะเหลือ/ข้อสันนิษฐานว่ารอสิ่งกีดขวาง (จาก Nav2 จริง ผ่านสถานะ goal)
    var r = run.run, g = run.goal;
    if (!r || r.state !== 'running' || r.phase !== 'going' || !g) { return ''; }
    var fb = g.feedback, text = '';
    if (fb && typeof fb.distance_remaining === 'number') {
      text = t('navgoal.progress', { d: fb.distance_remaining.toFixed(1), s: Math.round(fb.eta || 0) });
    }
    if (g.waiting_assumed) { text += (text ? ' · ' : '') + t('navgoal.waiting'); }
    return text;
  }

  function runPercent() {
    var r = run.run;
    if (!r || r.state === 'idle' || !r.total) { return 0; }
    if (r.state === 'succeeded') { return 100; }
    var done = (r.loop === 0 ? 0 : (r.round - 1) * r.total) + r.index;
    var all = (r.loop === 0 ? 1 : r.loop) * r.total;
    return Math.max(0, Math.min(100, Math.round(done / all * 100)));
  }

  function runStartReason() {                               // เหตุผลที่เริ่มไม่ได้ ('' = เริ่มได้)
    if (!admin || !authEnabled) { return t('run.need_admin'); }
    if (!navSettled()) { return t('run.need_nav'); }
    var saved = wp.draft && wp.draft.previous && wp.routes[wp.draft.previous];
    if (!saved) { return t('run.need_route'); }
    if (wp.dirty) { return t('run.unsaved'); }
    if (goalBusy()) { return t('run.busy_goal'); }
    if (!goalLocOk() && !runLocGrace()) { return t('navgoal.need_loc'); }       // ไม่มี grace = เข้มเหมือนเดิม (เซิร์ฟเวอร์ปฏิเสธ)
    return '';
  }

  function runLocGrace() {                                  // วินาทีที่เซิร์ฟเวอร์ยอมรอตำแหน่งฟื้น (0 = ไม่รอ)
    return (run.run && typeof run.run.loc_grace === 'number') ? run.run.loc_grace : 0;
  }

  function renderRun() {
    var active = runActive(), r = run.run;
    var canAct = !!(admin && authEnabled && connected);
    var reason = active ? '' : runStartReason();
    $('run-status').textContent = connected ? runStatusText() : t('nav.gate.disconnected');
    var detail = runDetailText();
    $('run-detail').textContent = detail;
    $('run-detail').hidden = !detail;
    var showBar = !!(r && r.state !== 'idle');
    $('run-progress').hidden = !showBar;
    $('run-progress-fill').style.width = runPercent() + '%';
    $('run-progress').setAttribute('aria-valuenow', String(runPercent()));
    var willLocate = (!active && !reason && canAct && runLocGrace() > 0 && !goalLocOk()) ? t('run.will_locate', { s: String(Math.round(runLocGrace())) }) : '';
    var note = active ? (wp.draft ? t('run.locked') : '') : (canAct ? (reason || willLocate) : (authEnabled && !admin ? t('run.need_admin') : ''));
    $('run-note').textContent = note;
    $('run-note').hidden = !note;
    $('run-start').hidden = active;
    $('run-start').disabled = !!(reason || runReq);
    $('run-pause').hidden = !active;
    var paused = !!(r && r.state === 'paused');
    $('run-pause').textContent = t(paused ? 'ui.run.resume' : 'ui.run.pause');
    $('run-pause').disabled = !canAct || runReq || (paused && !r.pause_settled);
    $('run-stop').hidden = !active;
    $('run-stop').disabled = !canAct;
    var root = document.documentElement;
    if (root && root.style && typeof root.style.setProperty === 'function' && $('run-bar').offsetHeight) {
      root.style.setProperty('--run-bar-h', $('run-bar').offsetHeight + 'px');
    }
  }

  function runApply(data) {
    run = { run: data.run, goal: data.goal, running: !!data.running };
    var r = data.run, busy = runActive();
    if (runLast.id === r.run && runLast.busy && !busy && r.state !== 'idle') { toast(t('run.done.' + r.state)); }
    runLast = { id: r.run, busy: busy };
    renderRun();
    wpRender();                                             // ปุ่ม/ช่องแก้ไขล็อกหรือปลดตามสถานะรัน
    if (onWpMapPage()) { drawMap(); }
  }

  function pollRun() {
    if (!onWpMapPage() || !connected) { return; }
    return api('/api/routes/run').then(function (r) {
      if (r.status === 200 && r.data && r.data.run) { runApply(r.data); }
    });
  }

  function runAsk() {                                       // ถามยืนยันทุกครั้ง (เซิร์ฟเวอร์ก็บังคับ confirmed:true ซ้ำ)
    if (runStartReason() || runReq) { return; }
    var route = wp.routes[wp.draft.previous], name = wp.draft.previous;
    var info = run.run || {};
    var items = [t('run.confirm.route', { route: name, n: String(route.points.length), loop: wpLoopText(route.loop) }),
                 t('run.confirm.timeout', { min: String(Math.round((info.point_timeout || 600) / 60)),
                                            hours: String(Math.round((info.max_run || 7200) / 360) / 10) }),
                 t('confirm.goal.safety')];
    if (route.mode === 'straight') { items.splice(1, 0, t('run.confirm.straight')); }
    return confirmDialog(t('run.confirm.title'), t('run.confirm.body'), t('run.confirm.ok'), { warn: true, items: items })
      .then(function (ok) {
        if (!ok) { return; }
        if (route.loop !== 0) { return runSend(name, false); }
        return confirmDialog(t('run.forever.title'), t('run.forever.body'), t('run.forever.ok'), { warn: true })
          .then(function (again) { if (again) { return runSend(name, true); } });
      });
  }

  function runSend(name, forever) {
    runReq = true;
    renderRun();
    var body = { name: name, confirmed: true };
    if (forever) { body.forever_confirmed = true; }
    return api('/api/routes/run', body).then(function (r) {
      runReq = false;
      if (r.status === 200 && r.data && r.data.ok) { runApply(r.data); toast(t('toast.run_started')); }
      else if (r.status === 401) { libExpired(); renderRun(); }
      else { toast(tm(r.data && r.data.error) || t('toast.run_failed')); renderRun(); }
    });
  }

  function runControl(action, okKey) {                      // pause | resume | stop (หยุดไม่ถามยืนยัน — ปุ่มนิรภัย)
    if (runReq) { return; }
    runReq = true;
    renderRun();
    return api('/api/routes/run/' + action, {}).then(function (r) {
      runReq = false;
      if (r.status === 200 && r.data && r.data.ok) { runApply(r.data); toast(t(okKey)); }
      else if (r.status === 401) { libExpired(); renderRun(); }
      else { toast(tm(r.data && r.data.error) || t('toast.run_control_failed')); renderRun(); }
    });
  }

  $('run-start').addEventListener('click', runAsk);
  $('run-pause').addEventListener('click', function () {
    var paused = !!(run.run && run.run.state === 'paused');
    runControl(paused ? 'resume' : 'pause', paused ? 'toast.run_resumed' : 'toast.run_paused');
  });
  $('run-stop').addEventListener('click', function () { runControl('stop', 'toast.run_stopped'); });

