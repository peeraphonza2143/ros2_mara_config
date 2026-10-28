  // ── ควบคุมหุ่นด้วยมือ (แถบขวา): จอยสติ๊ก / ปุ่มทิศทาง / คีย์บอร์ด → WebSocket /ws/drive ───────────────
  // หลักการ: แตะค้างเท่านั้นถึงขยับ (deadman) · ส่งคำสั่ง ~10 Hz · ปล่อย/สลับแท็บ/ออกจากหน้า/สัญญาณหลุด = หยุดทันที
  // เพดานความเร็วจริงอยู่ที่หุ่น (หน้านี้แค่ส่ง "สัดส่วน" × ค่าสูงสุดที่ผู้ใช้ตั้ง)
  var DRIVE_SEND_MS = 100;
  var DRIVE_HB_MS = 1000;
  var DRIVE_SPEED_KEY = 'agv.drive.speed';
  var DRIVE_INPUT_KEY = 'agv.drive.input';
  var DEADZONE = 0.08;
  var driveLimits = { max_linear: 0.5, max_angular: 1.0, default_linear: 0.25, default_angular: 0.6 };
  var driveSpeed = loadDriveSpeed();
  var driveSock = null;
  var driveReady = false;
  var driveDenied = null;           // ข้อความที่เซิร์ฟเวอร์ปฏิเสธ (msg)
  var driveLost = false;
  var driveAck = null;              // state ล่าสุดจากเซิร์ฟเวอร์ { v, w, driving, reason }
  var driveRetryTimer = null;
  var driveSendTimer = null;
  var driveHbTimer = null;
  var driveLastSentZero = true;
  var driveIn = { joyX: 0, joyY: 0, dp: { fwd: false, back: false, left: false, right: false },
                  keys: { fwd: false, back: false, left: false, right: false } };

  function loadDriveSpeed() {
    try {
      var raw = JSON.parse(window.localStorage.getItem(DRIVE_SPEED_KEY));
      return { v: clampNum(raw && raw.v, 0.05, 0.5, 0.01, 0.25), w: clampNum(raw && raw.w, 0.1, 1, 0.05, 0.6) };
    } catch (e) { return { v: 0.25, w: 0.6 }; }
  }

  // สวิตช์เลือกวิธีควบคุม (เลือกอย่างใดอย่างหนึ่ง) : 'joy' = จอยสติ๊ก · 'dpad' = ปุ่มลูกศร ; แป้นพิมพ์ใช้ได้เสมอ
  var driveInputMode = 'joy';

  function loadDriveInput() {
    try { return window.localStorage.getItem(DRIVE_INPUT_KEY) === 'dpad' ? 'dpad' : 'joy'; } catch (e) { return 'joy'; }
  }

  function renderDriveInput() {
    $('joy').hidden = driveInputMode !== 'joy';
    $('dpad').hidden = driveInputMode !== 'dpad';
    $('drive-input-joy').setAttribute('aria-pressed', driveInputMode === 'joy' ? 'true' : 'false');
    $('drive-input-dpad').setAttribute('aria-pressed', driveInputMode === 'dpad' ? 'true' : 'false');
  }

  function setDriveInput(mode) {
    if (mode !== driveInputMode) {
      driveStopAll();                    // วิธีเดิมที่กำลังถูกกดอยู่ถูกซ่อน → หยุดทันที ไม่ปล่อยคำสั่งค้าง
      driveInputMode = mode;
      try { window.localStorage.setItem(DRIVE_INPUT_KEY, mode); } catch (e) { /* ไม่เป็นไร */ }
    }
    renderDriveInput();
  }

  function saveDriveSpeed() {
    try { window.localStorage.setItem(DRIVE_SPEED_KEY, JSON.stringify(driveSpeed)); } catch (e) { /* ไม่เป็นไร */ }
  }

  function driveModeOk() {
    var m = lastMode;
    return !!(m && (m.mode === 'MAPPING' || m.mode === 'MANUAL') && m.state === 'RUNNING' &&
              !(m.external && m.external.length) && !m.leftover);
  }

  function driveWanted() {
    return !!(admin && authEnabled && connected && driveModeOk() && onMapPage());
  }

  function driveVector() {                  // สัดส่วนที่ผู้ใช้สั่งอยู่ตอนนี้ { lin, ang } (หน้า/ซ้ายเป็นบวก)
    var lin = -driveIn.joyY, ang = -driveIn.joyX;
    [driveIn.dp, driveIn.keys].forEach(function (g) {
      lin += (g.fwd ? 1 : 0) - (g.back ? 1 : 0);
      ang += (g.left ? 1 : 0) - (g.right ? 1 : 0);
    });
    return { lin: Math.max(-1, Math.min(1, lin)), ang: Math.max(-1, Math.min(1, ang)) };
  }

  function driveClearInputs() {
    driveIn.joyX = 0; driveIn.joyY = 0;
    ['dp', 'keys'].forEach(function (g) { Object.keys(driveIn[g]).forEach(function (k) { driveIn[g][k] = false; }); });
    joyPointer = null;
    $('joy-knob').style.transform = 'translate(0px, 0px)';
  }

  function driveSend(obj) {
    if (driveSock && driveSock.readyState === 1) {
      driveSock.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function driveTick() {                      // ทุก 100 ms: ส่งคำสั่งขับ (ถ้ามี) · หรือ heartbeat ทุก 1 วินาที
    if (!driveReady) { return; }
    var v = driveVector();
    var zero = (v.lin === 0 && v.ang === 0);
    if (!zero) {
      driveSend({ t: 'cmd', lin: v.lin, ang: v.ang, vmax: driveSpeed.v, wmax: driveSpeed.w });
      driveLastSentZero = false;
    } else if (!driveLastSentZero) {
      driveSend({ t: 'cmd', lin: 0, ang: 0, vmax: driveSpeed.v, wmax: driveSpeed.w });   // ปล่อยแล้ว → ลดลงศูนย์
      driveLastSentZero = true;
    }
  }

  function driveHeartbeat() {                  // ว่างอยู่ก็ส่ง heartbeat ทุก 1 วินาที (บอกว่ายังอยู่ + ตรวจว่าซ็อกเก็ตยังมีชีวิต)
    if (driveReady && driveLastSentZero) { driveSend({ t: 'hb' }); }
  }

  function driveStopAll() {                   // หยุดทันที: ล้างอินพุต + สั่งหยุดผ่านซ็อกเก็ต + REST (กันกรณีซ็อกเก็ตมีปัญหา)
    driveClearInputs();
    driveLastSentZero = true;
    driveSend({ t: 'stop' });
    renderDrive();
  }

  function driveHardStop() {
    driveStopAll();
    if (admin) { api('/api/drive/stop', {}).then(function () { pollGoal(); }); }      // ใน Navigation = ยกเลิก goal ด้วย
  }

  function closeDrive() {
    clearInterval(driveSendTimer);
    driveSendTimer = null;
    clearInterval(driveHbTimer);
    driveHbTimer = null;
    clearTimeout(driveRetryTimer);
    driveRetryTimer = null;
    if (driveSock) {
      var s = driveSock;
      driveSock = null;
      driveReady = false;
      try { if (s.readyState === 1) { s.send(JSON.stringify({ t: 'stop' })); } s.close(); } catch (e) { /* ไม่เป็นไร */ }
    }
    driveClearInputs();
    driveLastSentZero = true;
  }

  function connectDrive() {
    var proto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
    var sock = new WebSocket(proto + location.host + '/ws/drive');
    driveSock = sock;
    driveReady = false;
    driveDenied = null;
    sock.onmessage = function (ev) {
      if (sock !== driveSock) { return; }
      var d;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.t === 'ready') {
        driveReady = true;
        driveLost = false;
        driveDenied = null;
        if (d.limits) { driveLimits = d.limits; applySpeedUi(); }
        clearInterval(driveSendTimer);
        driveSendTimer = setInterval(driveTick, DRIVE_SEND_MS);
        clearInterval(driveHbTimer);
        driveHbTimer = setInterval(driveHeartbeat, DRIVE_HB_MS);
      } else if (d.t === 'state') {
        driveAck = d;
      } else if (d.t === 'denied') {
        driveReady = false;
        driveDenied = d.error || null;
        driveClearInputs();
      }
      renderDrive();
    };
    sock.onclose = function () {
      if (sock !== driveSock) { return; }
      var wasReady = driveReady;
      driveSock = null;
      driveReady = false;
      clearInterval(driveSendTimer);
      driveSendTimer = null;
      clearInterval(driveHbTimer);
      driveHbTimer = null;
      driveClearInputs();
      driveLost = wasReady;                   // เคยพร้อมแล้วหลุด = บอกผู้ใช้ว่าหุ่นกำลังหยุด
      renderDrive();
      if (driveWanted()) {
        driveRetryTimer = setTimeout(function () { driveRetryTimer = null; syncDrive(); }, driveDenied ? 3000 : 1000);
      }
    };
    sock.onerror = function () { try { sock.close(); } catch (e) { /* ไม่เป็นไร */ } };
  }

  function syncDrive() {                      // เรียกเมื่อสถานะที่เกี่ยวข้องเปลี่ยน (โหมด/สิทธิ์/หน้า/การเชื่อมต่อ)
    if (driveWanted()) {
      if (!driveSock && !driveRetryTimer) { connectDrive(); }
    } else if (driveSock || driveRetryTimer) {
      closeDrive();
    }
    renderDrive();
  }

  function driveStatus() {                    // → { key, cls, text }
    if (!authEnabled) { return { cls: 'warn', text: t('drive.st.no_pin') }; }
    if (!admin) { return { cls: 'warn', text: t('drive.st.need_admin') }; }
    if (!connected || !driveModeOk()) { return { cls: 'warn', text: t('drive.st.need_mode') }; }
    if (driveDenied) { return { cls: 'bad', text: tm(driveDenied) }; }
    if (driveLost && !driveReady) { return { cls: 'bad', text: t('drive.st.lost') }; }
    if (!driveReady) { return { cls: 'warn', text: t('drive.st.connecting') }; }
    if (driveAck && driveAck.driving) { return { cls: 'ok', text: t('drive.st.driving') }; }
    return { cls: 'ok', text: t('drive.st.ready') };
  }

  function renderDrive() {
    var st = driveStatus();
    var el = $('drive-status');
    el.textContent = st.text;
    el.className = 'drive-status ' + st.cls;
    var usable = driveReady && !driveDenied;
    $('drive-card').className = 'card' + (usable ? '' : ' off');
    ['dp-fwd', 'dp-back', 'dp-left', 'dp-right', 'drive-stop'].forEach(function (id) { $(id).disabled = !usable; });
    var out = $('drive-readout');
    if (usable && driveAck) {
      out.textContent = t('drive.readout', { v: t('drive.unit.v', { n: Number(driveAck.v).toFixed(2) }),
                                             w: t('drive.unit.w', { n: Number(driveAck.w).toFixed(2) }) });
    } else {
      out.textContent = '';
    }
    renderStopButton();
  }

  function renderStopButton() {                // ปุ่ม ■ บนแถบบน: ใช้ได้ตอนขับด้วยมือได้ (Admin + Mapping/Manual)
    var on = !!(admin && authEnabled && connected && (driveModeOk() || navSettled()));
    var b = $('stop');
    b.disabled = !on;
    var label = t(on ? (driveModeOk() ? 'ui.stop.aria' : 'ui.stop.aria.nav') : 'ui.stop.aria.off');
    b.setAttribute('aria-label', label);
    b.setAttribute('title', label);
  }

  // ความเร็วสูงสุดที่ผู้ใช้ตั้ง (สไลเดอร์) — ไม่เกินเพดานของหุ่น
  function applySpeedUi() {
    var vmax = driveLimits.max_linear, wmax = driveLimits.max_angular;
    $('drive-vmax').setAttribute('max', String(vmax));
    $('drive-wmax').setAttribute('max', String(wmax));
    driveSpeed.v = Math.min(driveSpeed.v, vmax);
    driveSpeed.w = Math.min(driveSpeed.w, wmax);
    $('drive-vmax').value = String(driveSpeed.v);
    $('drive-wmax').value = String(driveSpeed.w);
    $('drive-vmax-out').textContent = t('drive.unit.v', { n: driveSpeed.v.toFixed(2) });
    $('drive-wmax-out').textContent = t('drive.unit.w', { n: driveSpeed.w.toFixed(2) });
  }

  var joyPointer = null;
  (function wireDrive() {
    driveInputMode = loadDriveInput();
    renderDriveInput();
    $('drive-input-joy').addEventListener('click', function () { setDriveInput('joy'); });
    $('drive-input-dpad').addEventListener('click', function () { setDriveInput('dpad'); });
    var joy = $('joy'), knob = $('joy-knob');

    function joyMove(ev) {
      var rect = joy.getBoundingClientRect();
      var r = rect.width / 2 * 0.66;                      // ระยะที่ปุ่มกลมขยับได้
      var dx = (ev.clientX - (rect.left + rect.width / 2)) / r;
      var dy = (ev.clientY - (rect.top + (rect.height || rect.width) / 2)) / r;
      var mag = Math.sqrt(dx * dx + dy * dy);
      if (mag > 1) { dx /= mag; dy /= mag; mag = 1; }
      knob.style.transform = 'translate(' + (dx * r).toFixed(1) + 'px, ' + (dy * r).toFixed(1) + 'px)';
      if (mag < DEADZONE) { dx = 0; dy = 0; }
      driveIn.joyX = dx;
      driveIn.joyY = dy;
    }
    function joyRelease(ev) {
      if (joyPointer === null || (ev && ev.pointerId !== undefined && ev.pointerId !== joyPointer)) { return; }
      joyPointer = null;
      driveIn.joyX = 0; driveIn.joyY = 0;
      knob.style.transform = 'translate(0px, 0px)';
    }
    joy.addEventListener('pointerdown', function (ev) {
      if (!driveReady || joyPointer !== null) { return; }
      joyPointer = ev.pointerId;
      if (joy.setPointerCapture) { try { joy.setPointerCapture(ev.pointerId); } catch (e) { /* ไม่เป็นไร */ } }
      joyMove(ev);
      if (ev.preventDefault) { ev.preventDefault(); }
    });
    joy.addEventListener('pointermove', function (ev) { if (joyPointer === ev.pointerId) { joyMove(ev); } });
    joy.addEventListener('pointerup', joyRelease);
    joy.addEventListener('pointercancel', joyRelease);
    joy.addEventListener('lostpointercapture', joyRelease);

    [['dp-fwd', 'fwd'], ['dp-back', 'back'], ['dp-left', 'left'], ['dp-right', 'right']].forEach(function (pair) {
      var b = $(pair[0]), key = pair[1];
      b.addEventListener('pointerdown', function (ev) {
        if (!driveReady) { return; }
        driveIn.dp[key] = true;
        if (b.setPointerCapture && ev.pointerId !== undefined) { try { b.setPointerCapture(ev.pointerId); } catch (e) { /* ไม่เป็นไร */ } }
        if (ev.preventDefault) { ev.preventDefault(); }
      });
      ['pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave'].forEach(function (type) {
        b.addEventListener(type, function () { driveIn.dp[key] = false; });
      });
    });

    $('drive-stop').addEventListener('click', driveHardStop);
    $('stop').addEventListener('click', driveHardStop);
    $('drive-vmax').addEventListener('input', function () {
      driveSpeed.v = clampNum($('drive-vmax').value, 0.05, driveLimits.max_linear, 0.01, 0.25);
      saveDriveSpeed(); applySpeedUi();
    });
    $('drive-wmax').addEventListener('input', function () {
      driveSpeed.w = clampNum($('drive-wmax').value, 0.1, driveLimits.max_angular, 0.05, 0.6);
      saveDriveSpeed(); applySpeedUi();
    });
    $('drive-speed-reset').addEventListener('click', function () {
      driveSpeed = { v: driveLimits.default_linear, w: driveLimits.default_angular };
      saveDriveSpeed(); applySpeedUi();
    });

    // คีย์บอร์ด (เดสก์ท็อป): W A S D / ลูกศร = ขับ · Space = หยุด ; ไม่ทำงานตอนพิมพ์ในช่องกรอก/ปรับสไลเดอร์
    var KEYMAP = { w: 'fwd', arrowup: 'fwd', s: 'back', arrowdown: 'back', a: 'left', arrowleft: 'left', d: 'right', arrowright: 'right' };
    function typing(ev) {
      var tag = ev.target && ev.target.tag !== undefined ? String(ev.target.tag).toUpperCase()
        : (ev.target && ev.target.tagName ? String(ev.target.tagName).toUpperCase() : '');
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
    }
    document.addEventListener('keydown', function (ev) {
      if (!driveReady || typing(ev)) { return; }
      var key = String(ev.key || '').toLowerCase();
      if (key === ' ' || key === 'spacebar') { driveHardStop(); if (ev.preventDefault) { ev.preventDefault(); } return; }
      if (KEYMAP[key]) { driveIn.keys[KEYMAP[key]] = true; if (ev.preventDefault) { ev.preventDefault(); } }
    });
    document.addEventListener('keyup', function (ev) {
      var key = String(ev.key || '').toLowerCase();
      if (KEYMAP[key]) { driveIn.keys[KEYMAP[key]] = false; }
    });
    window.addEventListener('blur', function () { driveStopAll(); });          // เปลี่ยนหน้าต่าง = หยุด
    window.addEventListener('pagehide', function () { closeDrive(); });
  })();

