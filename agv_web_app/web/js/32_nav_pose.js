  // ── วางตำแหน่งหุ่น / เลือกจุดหมาย บนแผนที่ (หน้า Navigation) : แตะ = ตำแหน่ง · ลาก = ทิศ · สองนิ้ว = ซูม/เลื่อนแผนที่ ──
  // ตำแหน่งหุ่น: แค่บอกระบบหาตำแหน่ง (AMCL) ว่าหุ่นอยู่ตรงไหน — ไม่ทำให้หุ่นเคลื่อนที่
  // จุดหมาย: ส่งให้ Nav2 พาหุ่นไปเอง (ต้องยืนยันทุกครั้ง) ; ทั้งสองอย่างกติกาจริงตัดสินที่เซิร์ฟเวอร์
  // ใช้เครื่องมือวาง (ชี้ตำแหน่ง+ทิศ) ร่วมกัน แต่ทำงานได้ทีละอย่าง (placing() = อันที่กำลังใช้)
  var POSE_ROT_STEP = Math.PI / 12;                            // ปุ่มหมุน 15°
  var POSE_DRAG_MIN_PX = 12;                                   // ลากน้อยกว่านี้ถือว่าแค่แตะ (ไม่เปลี่ยนทิศ)
  var poseTool = { active: false, pending: null, busy: false, drag: null };   // pending = {x, y, yaw(rad)}
  var goalTool = { active: false, pending: null, busy: false, drag: null };
  var goalSt = null;                                           // ผล /api/nav/goal ล่าสุด { running, goal }
  var goalLast = { seq: 0, busy: false };                      // จำไว้แจ้งผลครั้งเดียวตอน goal จบ
  var GOAL_POLL_MS = 1000;
  var GOAL_BUSY = ['sending', 'active', 'canceling'];
  var GOAL_LOC_OK = ['good', 'fair'];                          // ตรงกับ LOCALIZED ฝั่งเซิร์ฟเวอร์ (ช่วยแสดงเท่านั้น)

  function placing() { return poseTool.active ? poseTool : (goalTool.active ? goalTool : null); }

  function navPoseUsable() {       // วางได้เมื่อ: ผู้ดูแล + Nav รันนิ่ง + แผนที่โหลดแล้ว (พิกัดจอ → โลกต้องมีมุมมองที่ถูกต้อง)
    return !!(admin && authEnabled && connected && navSettled() && mapOwner === 'nav' && mapMeta && mapMeta.available && mapImg);
  }

  function screenToWorld(px, py) {
    var size = mapCanvasSize();
    return { x: mapView.cx + (px - size.w / 2) / mapView.scale, y: mapView.cy - (py - size.h / 2) / mapView.scale };
  }

  function wrapYaw(yaw) {
    while (yaw > Math.PI) { yaw -= 2 * Math.PI; }
    while (yaw <= -Math.PI) { yaw += 2 * Math.PI; }
    return yaw;
  }

  function poseDeg(yaw) { return Math.round(wrapYaw(yaw) * 180 / Math.PI); }

  // วางตำแหน่ง "เมื่อยกนิ้ว (แตะ)" หรือ "เมื่อลากเกิน POSE_DRAG_MIN_PX (ชี้ทิศ)" — ไม่วางทันทีที่นิ้วแตะ
  // เพราะท่าซูมสองนิ้วเริ่มด้วยนิ้วเดียวเสมอ ถ้าวางทันทีตำแหน่งที่วางไว้แล้วจะเพี้ยนไปที่จุดที่นิ้วแรกแตะ
  function poseDown(cv, id, p) {
    var tl = placing();
    if (cv.id !== 'nav-map-canvas' || !tl || tl.busy) { return; }
    if (Object.keys(mapPointers).length !== 1) { tl.drag = null; return; }   // นิ้วที่สอง = ซูม/เลื่อน ไม่ใช่วาง
    tl.drag = { id: id, sx: p.x, sy: p.y, placed: false };
  }

  function poseHere(tl, drag) {    // ตำแหน่งที่นิ้วแตะลง (คงทิศเดิมไว้ ถ้าเคยตั้ง)
    var w = screenToWorld(drag.sx, drag.sy);
    tl.pending = { x: w.x, y: w.y, yaw: tl.pending ? tl.pending.yaw : 0 };
    drag.placed = true;
  }

  // คืนจริงถ้า "ใช้การเคลื่อนไหวนี้ไปชี้ทิศแล้ว" (ผู้เรียกต้องไม่เลื่อนแผนที่ซ้ำ)
  function poseMove(cv, id, p, pointers) {
    var tl = placing();
    if (cv.id !== 'nav-map-canvas' || !tl) { return false; }
    if (pointers !== 1) { tl.drag = null; return false; }
    var drag = tl.drag;
    if (!drag || drag.id !== id) { return false; }
    var dx = p.x - drag.sx, dy = p.y - drag.sy;
    if (Math.hypot(dx, dy) >= POSE_DRAG_MIN_PX) {
      if (!drag.placed) { poseHere(tl, drag); }
      tl.pending.yaw = Math.atan2(-dy, dx);                      // จอ y ลง = บวก ส่วนทิศโลก y ขึ้น = บวก
      renderNavPose();
      drawMap();
    }
    return true;                                                 // นิ้วเดียวในโหมดวาง: ไม่เลื่อนแผนที่
  }

  function poseUp(id, cancelled) {
    var tl = [poseTool, goalTool].filter(function (x) { return x.drag && x.drag.id === id; })[0];
    if (!tl) { return; }
    var drag = tl.drag;
    tl.drag = null;
    if (!drag.placed && !cancelled && tl.active) {               // แตะแล้วยกนิ้ว = วางตรงจุดนั้น
      poseHere(tl, drag);
      renderNavPose();
      drawMap();
    }
  }

  function toolReset(tl) {
    tl.active = false;
    tl.pending = null;
    tl.drag = null;
    tl.busy = false;
  }

  function poseCancel() {
    if (!poseTool.active && !poseTool.pending) { return; }
    toolReset(poseTool);
    renderNavPose();
    if (onNavMapPage()) { drawMap(); }
  }

  function goalCancelPick() {
    if (!goalTool.active && !goalTool.pending) { return; }
    toolReset(goalTool);
    renderNavPose();
    if (onNavMapPage()) { drawMap(); }
  }

  function poseKey(cv, key) {      // Enter = ส่ง · Esc = ยกเลิก (เฉพาะตอนกำลังวาง)
    var tl = placing();
    if (cv.id !== 'nav-map-canvas' || !tl) { return false; }
    if (key === 'Enter') { if (tl === poseTool) { poseSend(); } else { goalAsk(); } return true; }
    if (key === 'Escape') { if (tl === poseTool) { poseCancel(); } else { goalCancelPick(); } return true; }
    return false;
  }

  function poseRotate(sign) {
    var tl = placing();
    if (!tl || !tl.pending || tl.busy) { return; }
    tl.pending.yaw = wrapYaw(tl.pending.yaw + sign * POSE_ROT_STEP);
    renderNavPose();
    drawMap();
  }

  function poseSend() {
    if (!poseTool.pending || poseTool.busy || !navPoseUsable()) { return; }
    var pose = poseTool.pending;
    poseTool.busy = true;
    renderNavPose();
    return api('/api/nav/initialpose', { x: pose.x, y: pose.y, yaw: pose.yaw }).then(function (r) {
      poseTool.busy = false;
      if (r.status === 200 && r.data && r.data.ok) {
        poseTool.active = false;
        poseTool.pending = null;
        toast(t('toast.navpose_sent'));
      } else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.navpose_failed')); }          // ผิดพลาดอื่น: เก็บตำแหน่งไว้ให้ลองส่งซ้ำ
      renderNavPose();
      drawMap();
    });
  }

  function renderNavPose() {
    var usable = navPoseUsable();
    if (poseTool.active && !usable) { toolReset(poseTool); }
    var pose = lastStatus && lastStatus.pose;
    var loc = lastStatus && lastStatus.localization;
    var q = (navSettled() && loc && loc.quality) ? loc.quality : null;
    var status = '';
    if (q) {
      status = ({ good: MARK.ok, fair: MARK.warn, bad: MARK.bad, unknown: MARK.off }[q] || MARK.off) + ' ' + t('navpose.loc.' + q);
      if (pose) { status += ' ' + t('navpose.pose', { x: fmt(pose.x, 1), y: fmt(pose.y, 1), deg: Math.round(pose.yaw_deg) }); }
    }
    $('navpose-status').textContent = status;
    $('navpose-status').hidden = !status;
    var msg = '';
    if (authEnabled && !admin) { msg = t('navpose.need_admin'); }
    else if (!navSettled()) { msg = t('navpose.need_nav'); }
    else if (!usable && !poseTool.active) { msg = t('navpose.need_map'); }
    $('navpose-note').textContent = msg;
    $('navpose-note').hidden = !msg;
    $('navpose-start').hidden = poseTool.active;
    $('navpose-start').disabled = !usable;
    $('navpose-edit').hidden = !poseTool.active;
    $('navpose-help').textContent = t('navpose.help');
    $('navpose-help').hidden = !poseTool.active;
    var p = poseTool.pending;
    $('navpose-readout').hidden = !(poseTool.active && p);
    $('navpose-readout').textContent = p ? t('navpose.readout', { x: p.x.toFixed(2), y: p.y.toFixed(2), deg: poseDeg(p.yaw) }) : '';
    $('navpose-send').disabled = !(poseTool.active && p && !poseTool.busy);
    $('navpose-rot-l').disabled = !(p && !poseTool.busy);
    $('navpose-rot-r').disabled = !(p && !poseTool.busy);
    $('navpose-cancel').disabled = poseTool.busy;
    renderNavGoal();
    renderNavStart();
    $('nav-map-canvas').classList.toggle('placing', !!placing());
  }

  $('navpose-start').addEventListener('click', function () {
    if (!navPoseUsable()) { return; }
    toolReset(goalTool);                                       // ทีละเครื่องมือ
    poseTool.active = true;
    poseTool.pending = null;
    renderNavPose();
    $('nav-map-canvas').focus();
  });
  $('navpose-cancel').addEventListener('click', poseCancel);
  $('navpose-send').addEventListener('click', poseSend);
  $('navpose-rot-l').addEventListener('click', function () { poseRotate(1); });
  $('navpose-rot-r').addEventListener('click', function () { poseRotate(-1); });

