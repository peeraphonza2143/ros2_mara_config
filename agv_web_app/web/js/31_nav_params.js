  // ── แผงตั้งค่า Nav2 (หน้า Navigation) : ค่าที่ใช้อยู่จริง vs ค่าที่บันทึก · "ใช้เลย" (สด) · "บันทึกไว้ใช้ครั้งหน้า" ──
  // ช่องกรอกเริ่มจาก "ค่าที่ Nav2 ใช้อยู่จริง" ตอน Navigation รัน (ไม่งั้นจากค่าที่บันทึกไว้)
  //   ใช้เลย  = ส่งเฉพาะช่องที่ต่างจากค่าที่ใช้อยู่ ; บันทึก = ส่งเฉพาะช่องที่ต่างจากค่าที่บันทึกไว้
  // กติกาจริง (ช่วงค่า / ต้องยืนยันค่าเสี่ยง / ย้อนค่าเมื่อพลาด) ตัดสินที่เซิร์ฟเวอร์ ; หน้านี้แค่แสดงและถามยืนยันตามที่เซิร์ฟเวอร์ขอ
  var navInfo = null;                                          // ผล /api/nav/params (ค่าที่บันทึก + Default + ช่วง)
  var navRun = { running: false, values: null, failed: false };   // ค่าที่ Nav2 ใช้อยู่จริง (อ่านสดเฉพาะตอน Nav รันนิ่ง)
  var navDraft = {};                                           // key → ข้อความในช่องกรอก (หรือ boolean)
  var navBase = {};                                            // key → ค่าฐาน ณ การซิงก์ล่าสุด (ไว้รู้ว่าผู้ใช้แก้ช่องไหนค้างอยู่)
  var navRows = {};                                            // key → { input, err, changed, reset, run, stateText } (DOM)
  var navGroups = {};                                          // id กลุ่ม → { det, badge } (DOM)
  var navOpen = { speed: true };                               // กลุ่มที่เปิดอยู่ (จำไว้ตอนสร้างแถวใหม่ เช่นสลับภาษา)
  var navBusy = false;
  var navRunKey = '';

  function navSettled() {          // เปิดเสร็จและนิ่งอยู่ (ตรงกับที่ backend ถือว่า "ปรับสดได้")
    var m = lastMode;
    return !!(connected && m && m.mode === 'NAVIGATION' && m.state === 'RUNNING' && !m.target);
  }

  function navAllRows() {
    var rows = [];
    if (navInfo && navInfo.groups) { navInfo.groups.forEach(function (g) { g.params.forEach(function (p) { rows.push(p); }); }); }
    return rows;
  }

  function navLiveKnown() { return !!(navRun.running && navRun.values); }

  function navBaseline(row) {
    return (navLiveKnown() && navRun.values[row.key] !== undefined) ? navRun.values[row.key] : row.value;
  }

  function navEditable() { return !!(admin && authEnabled && connected && !navBusy); }

  function navDraftOf(row, v) { return row.type === 'bool' ? !!v : fmtNum(v); }

  function navValueText(row, v) {
    return row.type === 'bool' ? t(v ? 'slam.on' : 'slam.off') : fmtNum(v) + (row.unit ? ' ' + row.unit : '');
  }

  function navParse(row) {         // อ่านค่าจากช่องกรอก → { ok, value, message }
    var raw = navDraft[row.key];
    if (row.type === 'bool') { return { ok: true, value: !!raw }; }
    var text = String(raw === undefined ? '' : raw).trim();
    var n = Number(text);
    if (text === '' || !isFinite(n) || n < row.min || n > row.max) {
      return { ok: false, message: t('navp.invalid', { min: fmtNum(row.min), max: fmtNum(row.max) }) };
    }
    return { ok: true, value: n };
  }

  // ช่องที่ผู้ใช้ไม่ได้แก้ค้างอยู่ → ตามค่าฐานล่าสุด (ค่าที่ใช้อยู่/ที่บันทึกเปลี่ยน) ; ช่องที่แก้ค้างอยู่คงไว้
  function navSync(force) {
    navAllRows().forEach(function (row) {
      var base = navDraftOf(row, navBaseline(row));
      if (force || navDraft[row.key] === undefined || navDraft[row.key] === navBase[row.key]) {
        navDraft[row.key] = base;
        var ui = navRows[row.key];
        if (ui) { if (row.type === 'bool') { ui.input.checked = !!base; } else { ui.input.value = String(base); } }
      }
      navBase[row.key] = base;
    });
  }

  function navChanges(against) {   // { values: {key: ค่าใหม่}, rows: [...], invalid: จริงถ้ามีช่องกรอกผิด }
    var out = { values: {}, rows: [], invalid: false };
    navAllRows().forEach(function (row) {
      var r = navParse(row);
      if (!r.ok) { out.invalid = true; return; }
      var ref = against(row);
      if (ref !== undefined && r.value !== ref) { out.values[row.key] = r.value; out.rows.push(row); }
    });
    return out;
  }

  // อัปเดตทุกแถวเข้าที่เดิม (ไม่สร้าง DOM ใหม่ → ช่องกรอกไม่เสียโฟกัสตอนพิมพ์)
  function updateNavRows() {
    var hint = $('navp-hint');
    if (!navInfo) { hint.textContent = t('ui.navp.loading'); return; }
    if (!navInfo.available) {
      hint.textContent = t('ui.navp.unavailable');
      $('navp-summary').textContent = '';
      $('navp-note').hidden = true;
      ['navp-apply', 'navp-save', 'navp-restore'].forEach(function (id) { $(id).hidden = true; });
      return;
    }
    ['navp-apply', 'navp-save', 'navp-restore'].forEach(function (id) { $(id).hidden = false; });
    var editable = navEditable();
    var live = navLiveKnown();
    var anyInvalid = false;
    navAllRows().forEach(function (row) {
      var ui = navRows[row.key];
      if (!ui) { return; }
      var r = navParse(row);
      if (!r.ok) { anyInvalid = true; }
      var shown = r.ok ? r.value : navBaseline(row);
      ui.input.disabled = !editable;
      ui.input.setAttribute('aria-invalid', r.ok ? 'false' : 'true');
      ui.err.hidden = r.ok;
      ui.err.textContent = r.ok ? '' : r.message;
      ui.changed.hidden = shown === row.default;
      ui.reset.hidden = !editable || (r.ok && r.value === row.default);
      if (ui.stateText) { ui.stateText.textContent = navValueText(row, shown); }
      ui.run.hidden = !live;
      if (live) {
        var now = navRun.values[row.key];
        var text = t('ui.navp.running_now', { value: navValueText(row, now) });
        if (now !== row.value) { text += ' · ' + t('ui.navp.saved_value', { value: navValueText(row, row.value) }); }
        ui.run.textContent = text;
      }
    });
    navInfo.groups.forEach(function (g) {
      var gu = navGroups[g.id];
      if (!gu) { return; }
      var n = 0, bad = false;
      g.params.forEach(function (row) {
        var r = navParse(row);
        if (!r.ok) { bad = true; } else if (r.value !== row.default) { n += 1; }
      });
      gu.badge.textContent = n ? t('ui.navp.group_changed', { n: n }) : '';
      gu.badge.hidden = !n;
      if (bad && !gu.det.open) { gu.det.open = true; navOpen[g.id] = true; }   // ช่องผิดต้องไม่ซ่อนอยู่ในกลุ่มที่พับ
    });
    var toApply = live ? navChanges(function (row) { return navRun.values[row.key]; }) : { rows: [] };
    var toSave = navChanges(function (row) { return row.value; });
    $('navp-apply').disabled = !(editable && live && toApply.rows.length && !anyInvalid);
    $('navp-save').disabled = !(editable && toSave.rows.length && !anyInvalid);
    $('navp-restore').disabled = !editable || navAllRows().every(function (row) {
      var r = navParse(row);
      return r.ok && r.value === row.default;
    });
    var parts = [navInfo.modified ? t('ui.navp.summary_changed', { n: navInfo.modified }) : t('ui.navp.summary_default')];
    if (toApply.rows.length || toSave.rows.length) { parts.push(t('ui.navp.edited')); }
    $('navp-summary').textContent = parts.join(' · ');
    hint.textContent = t(live || navSettled() ? 'ui.navp.hint.running' : 'ui.navp.hint.idle');
    var msg = '';
    if (authEnabled && !admin) { msg = t('ui.navp.need_admin'); }
    else if (navRun.failed) { msg = t('ui.navp.live_failed'); }
    else if (live && navInfo.restart_needed) { msg = t('ui.navp.diff'); }
    var note = $('navp-note');
    note.hidden = !msg;
    note.textContent = msg;
  }

  function buildNavRow(row) {
    var wrap = document.createElement('div');
    wrap.className = 'slam-row';
    var head = document.createElement('div');
    head.className = 'slam-head';
    var name = document.createElement('label');
    name.className = 'slam-name';
    name.setAttribute('for', 'navp-in-' + row.key);
    name.textContent = t('navp.p.' + row.key + '.name');
    var changed = document.createElement('span');
    changed.className = 'slam-changed';
    changed.textContent = t('ui.navp.changed');
    head.appendChild(name);
    head.appendChild(changed);
    var desc = document.createElement('p');
    desc.className = 'slam-desc';
    desc.textContent = t('navp.p.' + row.key + '.desc');
    var def = document.createElement('p');
    def.className = 'slam-default';
    def.textContent = t('ui.navp.default', { value: navValueText(row, row.default) });
    var run = document.createElement('p');
    run.className = 'navp-run';
    run.hidden = true;
    var edit = document.createElement('div');
    edit.className = 'slam-edit';
    var input = document.createElement('input');
    input.id = 'navp-in-' + row.key;
    var ui = { input: input, changed: changed, run: run };
    if (row.type === 'bool') {
      var label = document.createElement('label');
      label.className = 'slam-switch';
      input.type = 'checkbox';
      input.checked = !!navDraft[row.key];
      input.addEventListener('change', function () { navDraft[row.key] = input.checked; updateNavRows(); });
      var text = document.createElement('span');
      label.appendChild(input);
      label.appendChild(text);
      ui.stateText = text;
      edit.appendChild(label);
    } else {
      input.type = 'text';
      input.className = 'slam-input';
      input.setAttribute('inputmode', 'decimal');
      input.value = String(navDraft[row.key]);
      input.addEventListener('input', function () { navDraft[row.key] = input.value; updateNavRows(); });
      edit.appendChild(input);
      if (row.unit) {
        var unit = document.createElement('span');
        unit.className = 'slam-unit';
        unit.textContent = row.unit;
        edit.appendChild(unit);
      }
    }
    var reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'slam-reset';
    reset.textContent = '↺';
    reset.setAttribute('aria-label', t('ui.navp.reset_one') + ': ' + t('navp.p.' + row.key + '.name'));
    reset.setAttribute('title', t('ui.navp.reset_one'));
    reset.addEventListener('click', function () {
      navDraft[row.key] = navDraftOf(row, row.default);
      if (row.type === 'bool') { input.checked = !!row.default; } else { input.value = fmtNum(row.default); }
      updateNavRows();
    });
    edit.appendChild(reset);
    ui.reset = reset;
    var err = document.createElement('p');
    err.className = 'slam-err';
    err.setAttribute('role', 'alert');
    err.hidden = true;
    ui.err = err;
    wrap.appendChild(head);
    wrap.appendChild(desc);
    wrap.appendChild(def);
    wrap.appendChild(run);
    wrap.appendChild(edit);
    wrap.appendChild(err);
    navRows[row.key] = ui;
    return wrap;
  }

  function renderNavParams() {     // สร้างแถวใหม่ (ครั้งแรก / เปลี่ยนภาษา) — ร่างที่ผู้ใช้แก้ค้างอยู่คงไว้
    var box = $('navp-groups');
    while (box.firstChild) { box.removeChild(box.firstChild); }
    navRows = {};
    navGroups = {};
    if (navInfo && navInfo.available) {
      navInfo.groups.forEach(function (g) {
        var sec = document.createElement('details');             // พับเก็บได้ (ใช้คีย์บอร์ด/โปรแกรมอ่านหน้าจอได้โดยกำเนิด)
        sec.className = 'navp-group';
        sec.open = !!navOpen[g.id];
        sec.addEventListener('toggle', function () { navOpen[g.id] = sec.open; });
        var sum = document.createElement('summary');
        var title = document.createElement('span');
        title.textContent = t('navp.group.' + g.id);
        var badge = document.createElement('span');               // "N แก้ไขแล้ว" ให้เห็นแม้กลุ่มถูกพับอยู่
        badge.className = 'slam-changed';
        badge.hidden = true;
        sum.appendChild(title);
        sum.appendChild(badge);
        sec.appendChild(sum);
        g.params.forEach(function (row) { sec.appendChild(buildNavRow(row)); });
        navGroups[g.id] = { det: sec, badge: badge };
        box.appendChild(sec);
      });
    }
    updateNavRows();
  }

  function loadNavParams() {
    var wantLive = navSettled();
    return Promise.all([api('/api/nav/params'), wantLive ? api('/api/nav/params/live') : Promise.resolve(null)]).then(function (rs) {
      if (rs[0].status === 200 && rs[0].data && rs[0].data.groups !== undefined) {
        var first = !navInfo || navInfo.available !== rs[0].data.available;
        navInfo = rs[0].data;
        if (!wantLive) { navRun = { running: false, values: null, failed: false }; }
        else if (rs[1].status === 200 && rs[1].data && rs[1].data.running && rs[1].data.values) {
          navRun = { running: true, values: rs[1].data.values, failed: false };
        } else if (rs[1].status === 200) { navRun = { running: false, values: null, failed: false }; }
        else { navRun = { running: true, values: null, failed: true }; }          // Nav รันอยู่แต่ถามค่าสดไม่ได้
        if (first || !Object.keys(navRows).length) { navSync(true); renderNavParams(); }
        else { navSync(false); updateNavRows(); }
      }
    });
  }

  function navExpired() { libExpired(); updateNavRows(); }

  function navFail(r) {
    toast(tm(r.data && r.data.error) || t('toast.navp_failed'));
    return loadNavParams();
  }

  function navSave() {
    if (navBusy) { return; }
    var ch = navChanges(function (row) { return row.value; });
    if (!ch.rows.length || ch.invalid) { return; }
    navBusy = true;
    updateNavRows();
    return api('/api/nav/params', { values: ch.values }).then(function (r) {
      navBusy = false;
      if (r.status === 200 && r.data && r.data.ok) {
        navInfo = r.data.params;
        navSync(false);
        updateNavRows();
        toast(t('toast.navp_saved'));
      } else if (r.status === 401) { navExpired(); }
      else { updateNavRows(); navFail(r); }
    });
  }

  // ใช้เลย : ครั้งแรกส่งโดยไม่ยืนยัน ; ถ้าเซิร์ฟเวอร์บอกว่ามีค่าเสี่ยง (409) ถามผู้ใช้แล้วส่งซ้ำพร้อม confirmed:true
  function navApply(confirmed) {
    if (navBusy || !navLiveKnown()) { return; }
    var ch = navChanges(function (row) { return navRun.values[row.key]; });
    if (!ch.rows.length || ch.invalid) { return; }
    navBusy = true;
    updateNavRows();
    var body = { values: ch.values };
    if (confirmed) { body.confirmed = true; }
    return api('/api/nav/params/apply', body).then(function (r) {
      navBusy = false;
      if (r.status === 200 && r.data && r.data.ok) {
        navInfo = r.data.params;
        navRun = { running: true, values: r.data.live, failed: false };
        navSync(false);
        updateNavRows();
        toast(t('toast.navp_applied', { n: Object.keys(r.data.applied).length }));
      } else if (r.status === 409 && !confirmed && r.data && r.data.error && r.data.error.code === 'nav.err.confirm_required') {
        updateNavRows();
        navConfirmRisky(String(r.data.error.params.keys).split(', '), ch.values);
      } else if (r.status === 401) { navExpired(); }
      else { updateNavRows(); navFail(r); }
    });
  }

  function navConfirmRisky(keys, values) {
    var byKey = {};
    navAllRows().forEach(function (row) { byKey[row.key] = row; });
    var items = keys.filter(function (key) { return byKey[key]; }).map(function (key) {
      var row = byKey[key];
      return t('confirm.navp_risky.item', { name: t('navp.p.' + key + '.name'),
        from: navValueText(row, navRun.values[key]), to: navValueText(row, values[key]) });
    });
    items.push(t('confirm.navp_risky.safety'));
    confirmDialog(t('confirm.navp_risky.title'), t('confirm.navp_risky.body'), t('confirm.navp_risky.ok'),
      { warn: true, items: items }).then(function (ok) { if (ok) { navApply(true); } });
  }

  $('navp-apply').addEventListener('click', function () { navApply(false); });
  $('navp-save').addEventListener('click', navSave);
  $('navp-restore').addEventListener('click', function () {
    navAllRows().forEach(function (row) {
      var ui = navRows[row.key];
      navDraft[row.key] = navDraftOf(row, row.default);
      if (ui) { if (row.type === 'bool') { ui.input.checked = !!row.default; } else { ui.input.value = fmtNum(row.default); } }
    });
    updateNavRows();
  });

  // ข้อความบนเกตแทนแผนที่ (ตอนที่ Navigation ยังไม่ทำงานนิ่ง)
  function navMapGate() {
    var m = lastMode;
    if (!connected || !m) { return t('nav.gate.disconnected'); }
    if (m.state === 'STARTING' || m.state === 'STOPPING') {
      return t(m.state === 'STARTING' && m.target === 'NAVIGATION' ? 'nav.gate.starting' : 'nav.gate.busy');
    }
    if (m.external && m.external.length) { return t('nav.gate.external'); }
    if (m.leftover) { return t('nav.gate.leftover', { mode: modeName(m.leftover.mode) }); }
    if (m.state === 'ERROR') { return t('nav.gate.error', { detail: tm(m.message) }); }
    return m.mode === 'STANDBY' ? t('nav.gate.standby') : t('nav.gate.other', { mode: modeName(m.mode) });
  }

  function renderNavMap() {
    if (!onNavMapPage()) { return; }
    var live = navSettled();
    $('nav-right').classList.toggle('nav-idle', !live);          // Nav ยังไม่รัน → คลังแผนที่ (ที่ใช้เริ่ม Navigation) ขึ้นบนสุดของคอลัมน์
    $('nav-map-live').hidden = !live;
    $('nav-map-gate').hidden = live;
    if (!live) {
      $('nav-map-gate-msg').textContent = navMapGate();
      if (mapOwner === 'nav') { resetMapData(); mapOwner = ''; }               // ไม่ค้างแผนที่/สแกนเก่า
      mapView.auto = true;
      poseCancel();
      renderNavPose();
      return;
    }
    claimMapData('nav');
    var has = !!(mapMeta && mapMeta.available);
    $('nav-map-wait').hidden = has;
    $('nav-map-info').textContent = has ? t('map.info', {
      w: mapMeta.size_m.w.toFixed(1), h: mapMeta.size_m.h.toFixed(1), res: String(mapMeta.resolution),
      time: hhmm(mapMeta.stamp) }) : '';
    renderNavPose();
    drawMap();
  }

