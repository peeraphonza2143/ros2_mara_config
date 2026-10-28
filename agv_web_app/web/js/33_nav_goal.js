  // ── ส่งหุ่นไปจุดหมาย ─────────────────────────────────────────────────────────────────────────────
  // สถานะมาจาก Nav2 จริง (GET /api/nav/goal ทุก 1 วินาทีบนหน้านี้) ; "รอ/ติด" เป็นข้อสันนิษฐานจากระยะที่ไม่ลดลง
  function goalBusy() { return !!(goalSt && goalSt.goal && GOAL_BUSY.indexOf(goalSt.goal.state) >= 0); }

  function goalLocOk() {
    var loc = lastStatus && lastStatus.localization;
    return !!(navSettled() && loc && GOAL_LOC_OK.indexOf(loc.quality) >= 0 && lastStatus.pose);
  }

  function goalStatusText() {
    var g = goalSt && goalSt.goal;
    if (!g || g.state === 'idle') { return { text: '', detail: '' }; }
    var text = MARK[{ sending: 'off', active: 'warn', canceling: 'warn', succeeded: 'ok', canceled: 'off', failed: 'bad' }[g.state]] +
      ' ' + t('navgoal.st.' + g.state);
    if (g.state === 'failed' && g.reason) { text += ' — ' + t(g.reason); }
    var detail = '';
    var fb = g.feedback;
    if (GOAL_BUSY.indexOf(g.state) >= 0 && fb && typeof fb.distance_remaining === 'number') {
      detail = t('navgoal.progress', { d: fb.distance_remaining.toFixed(1), s: Math.round(fb.eta || 0) });
      if (fb.recoveries) { detail += ' · ' + t('navgoal.recoveries', { n: fb.recoveries }); }
    }
    if (g.waiting_assumed) { detail += (detail ? ' · ' : '') + t('navgoal.waiting'); }
    return { text: text, detail: detail };
  }

  function renderNavGoal() {
    var usable = navPoseUsable();
    if (goalTool.active && !usable) { toolReset(goalTool); }
    var busy = goalBusy();
    var st = goalStatusText();
    $('navgoal-status').textContent = st.text;
    $('navgoal-status').hidden = !st.text;
    $('navgoal-detail').textContent = st.detail;
    $('navgoal-detail').hidden = !st.detail;
    var msg = '';
    if (authEnabled && !admin) { msg = t('navgoal.need_admin'); }
    else if (!navSettled()) { msg = t('navgoal.need_nav'); }
    else if (!usable && !goalTool.active) { msg = t('navgoal.need_map'); }
    else if (!goalLocOk() && !busy) { msg = t('navgoal.need_loc'); }
    $('navgoal-note').textContent = msg;
    $('navgoal-note').hidden = !msg;
    $('navgoal-start').hidden = goalTool.active;
    $('navgoal-start').disabled = !(usable && goalLocOk() && !busy);
    $('navgoal-edit').hidden = !goalTool.active;
    $('navgoal-help').textContent = t('navgoal.help');
    $('navgoal-help').hidden = !goalTool.active;
    var p = goalTool.pending;
    $('navgoal-readout').hidden = !(goalTool.active && p);
    $('navgoal-readout').textContent = p ? t('navpose.readout', { x: p.x.toFixed(2), y: p.y.toFixed(2), deg: poseDeg(p.yaw) }) : '';
    $('navgoal-send').disabled = !(goalTool.active && p && !goalTool.busy && goalLocOk() && !busy);
    $('navgoal-rot-l').disabled = !(p && !goalTool.busy);
    $('navgoal-rot-r').disabled = !(p && !goalTool.busy);
    $('navgoal-cancel').disabled = goalTool.busy;
    $('navgoal-stop').hidden = !(busy && admin && authEnabled);
    renderStraight();
    renderStopButton();
  }

  function goalAsk() {             // ถามยืนยันทุกครั้งก่อนส่ง (เซิร์ฟเวอร์ก็บังคับ confirmed:true ซ้ำ)
    var pose = goalTool.pending;
    if (!pose || goalTool.busy || !navPoseUsable() || !goalLocOk() || goalBusy()) { return; }
    var shown = { x: pose.x.toFixed(2), y: pose.y.toFixed(2), deg: poseDeg(pose.yaw) };
    return confirmDialog(t('confirm.goal.title'), t('confirm.goal.body'), t('confirm.goal.ok'), {
      warn: true,
      items: [t('confirm.goal.item', shown), t('confirm.goal.safety')]
    }).then(function (ok) { if (ok) { return goalSend(pose); } });
  }

  function goalSend(pose) {
    goalTool.busy = true;
    renderNavPose();
    return api('/api/nav/goal', { x: pose.x, y: pose.y, yaw: pose.yaw, confirmed: true }).then(function (r) {
      goalTool.busy = false;
      if (r.status === 200 && r.data && r.data.ok) {
        toolReset(goalTool);
        goalSt = { running: true, goal: r.data.goal, limits: goalSt && goalSt.limits };
        goalNotice();
        toast(t('toast.navgoal_sent'));
      } else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.navgoal_failed')); }        // ผิดพลาดอื่น: เก็บจุดไว้ให้แก้/ลองใหม่
      renderNavPose();
      drawMap();
    });
  }

  function goalStop() {            // ยกเลิกการเดินทาง: ไม่ถามยืนยัน (ปุ่มนิรภัย ต้องกดแล้วหยุดทันที)
    if (!admin) { return; }
    return api('/api/nav/goal/cancel', {}).then(function (r) {
      if (r.status === 200 && r.data && r.data.ok) { goalSt = { running: true, goal: r.data.goal, limits: goalSt && goalSt.limits }; toast(t('toast.navgoal_cancel')); }
      else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.navgoal_cancel_failed')); }
      renderNavPose();
      drawMap();
    });
  }

  function goalNotice() {          // goal จบ (สำเร็จ/ยกเลิก/ล้ม) → แจ้งครั้งเดียวต่อ goal
    var g = goalSt && goalSt.goal;
    if (!g) { return; }
    var busy = GOAL_BUSY.indexOf(g.state) >= 0;
    if (goalLast.seq === g.seq && goalLast.busy && !busy) { toast(t('navgoal.done.' + g.state)); }
    goalLast = { seq: g.seq, busy: busy };
  }

  function pollGoal() {
    if (!onNavPage() || document.hidden || !connected) { return; }
    return api('/api/nav/goal').then(function (r) {
      if (r.status !== 200 || !r.data) { return; }
      goalSt = r.data;
      goalNotice();
      renderNavPose();
      if (onNavMapPage()) { drawMap(); }
    });
  }

  $('navgoal-start').addEventListener('click', function () {
    if (!navPoseUsable() || !goalLocOk() || goalBusy()) { return; }
    toolReset(poseTool);                                       // ทีละเครื่องมือ
    goalTool.active = true;
    goalTool.pending = null;
    renderNavPose();
    $('nav-map-canvas').focus();
  });
  $('navgoal-cancel').addEventListener('click', goalCancelPick);
  $('navgoal-send').addEventListener('click', goalAsk);
  $('navgoal-stop').addEventListener('click', goalStop);
  $('navgoal-rot-l').addEventListener('click', function () { poseRotate(1); });
  $('navgoal-rot-r').addEventListener('click', function () { poseRotate(-1); });

  // ── เดินตรง (Drive straight) : จุดหมายอยู่ข้างหน้าตามทิศหุ่น ระยะ N เมตร ──────────────────────────────
  // ตรวจแนวล่วงหน้าที่เซิร์ฟเวอร์ (ทุกช่องตลอดแนวต้องว่างและห่างผนัง) → แสดงเส้นตัวอย่างบนแผนที่ → ยืนยัน → ส่ง
  var straightPreview = null;                                  // { from: {x,y}, to: {x,y,yaw}, soft_risk }
  var straightBusy = false;
  var STRAIGHT_CODE = { outside: 'nav.err.straight_outside', unknown: 'nav.err.straight_unknown',
                        occupied: 'nav.err.straight_unknown', tight: 'nav.err.straight_tight', no_map: 'nav.err.no_map' };

  function straightLimits() {
    var l = goalSt && goalSt.limits;
    return { min: l ? l.straight_min : 0.5, max: l ? l.straight_max : 10, clearance: l ? l.clearance : 0.4 };
  }

  function straightDistance() {            // ค่าในช่อง ; ไม่ใช่ตัวเลขหรือนอกช่วง = null
    var v = Number($('navstraight-dist').value);
    var lim = straightLimits();
    return ($('navstraight-dist').value.trim() !== '' && isFinite(v) && v >= lim.min && v <= lim.max) ? v : null;
  }

  function renderStraight() {
    var lim = straightLimits();
    $('navstraight-dist').setAttribute('min', String(lim.min));
    $('navstraight-dist').setAttribute('max', String(lim.max));
    var ok = navPoseUsable() && goalLocOk() && !goalBusy() && !straightBusy && !goalTool.active && !poseTool.active;
    $('navstraight-go').disabled = !ok;
    $('navstraight-dist').disabled = !(admin && authEnabled && navSettled());
  }

  function straightAsk() {
    if (!navPoseUsable() || !goalLocOk() || goalBusy()) { return; }
    var lim = straightLimits();
    var d = straightDistance();
    if (d === null) { toast(t('navstraight.invalid', { min: lim.min, max: lim.max })); return; }
    straightBusy = true;
    renderNavPose();
    return api('/api/nav/goal/straight/check', { distance: d }).then(function (r) {
      var c = r.data && r.data.check;
      if (r.status === 401) { straightBusy = false; libExpired(); return; }
      if (r.status !== 200 || !c) {
        straightBusy = false;
        toast(tm(r.data && r.data.error) || t('toast.navstraight_failed'));
        return;
      }
      if (!c.ok) {                                             // แนวไม่โล่ง : บอกว่าติดที่ระยะเท่าไร
        straightBusy = false;
        toast(t(STRAIGHT_CODE[c.reason] || 'nav.err.straight_unknown', { at: c.at, clearance: lim.clearance }));
        return;
      }
      var pose = lastStatus && lastStatus.pose;
      straightPreview = { from: { x: pose.x, y: pose.y }, to: c.goal, soft_risk: c.soft_risk };
      drawMap();
      var items = [t('confirm.straight.item', { d: d.toFixed(1), x: c.goal.x.toFixed(2), y: c.goal.y.toFixed(2) })];
      if (c.soft_risk) { items.push(t('confirm.straight.soft')); }
      items.push(t('confirm.goal.safety'));
      return confirmDialog(t('confirm.straight.title'), t('confirm.straight.body'), t('confirm.straight.ok'),
        { warn: true, items: items }).then(function (yes) {
        if (!yes) { return; }
        return api('/api/nav/goal/straight', { distance: d, confirmed: true }).then(function (r2) {
          if (r2.status === 200 && r2.data && r2.data.ok) {
            goalSt = { running: true, goal: r2.data.goal, limits: goalSt && goalSt.limits };
            goalNotice();
            toast(t('toast.navgoal_sent'));
          } else if (r2.status === 401) { libExpired(); }
          else { toast(tm(r2.data && r2.data.error) || t('toast.navgoal_failed')); }
        });
      });
    }).then(function () {
      straightBusy = false;
      straightPreview = null;
      renderNavPose();
      drawMap();
    });
  }

  $('navstraight-go').addEventListener('click', straightAsk);

