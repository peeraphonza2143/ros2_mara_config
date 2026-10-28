  // ── ตั้งค่า SLAM (หน้า Mapping): แสดง Default + ปรับได้ก่อนเริ่ม · อ่านอย่างเดียวระหว่างรัน ──────────
  var slamInfo = null;            // ผล /api/slam/params ล่าสุด (ค่าที่บันทึกไว้ + Default)
  var slamDraft = {};             // key → ข้อความในช่องกรอก (หรือ boolean) ที่ผู้ใช้กำลังแก้
  var slamRows = {};              // key → { input, err, changed, reset } (DOM)
  var slamRunKey = '';

  function fmtNum(v) {
    if (typeof v !== 'number') { return String(v); }
    return String(Number(v.toFixed(6)));
  }

  function slamShown(row, running) {      // ค่าที่ควรแสดง: ระหว่างรัน = ค่าที่ใช้จริง ไม่ใช่ค่าที่บันทึกไว้
    return (running && slamInfo.in_use && slamInfo.in_use[row.key] !== undefined) ? slamInfo.in_use[row.key] : row.value;
  }

  function slamEditable() {
    return !!(admin && authEnabled && connected && !mappingBusyOrRunning());
  }

  function mappingBusyOrRunning() {
    var m = lastMode;
    return !!(m && (m.mode === 'MAPPING' || m.target === 'MAPPING'));
  }

  function slamValueText(row, v) {
    return row.type === 'bool' ? t(v ? 'slam.on' : 'slam.off') : fmtNum(v) + (row.unit ? ' ' + row.unit : '');
  }

  // อ่านค่าจากช่องกรอก → { ok, value, message }
  function slamParse(row) {
    var raw = slamDraft[row.key];
    if (row.type === 'bool') { return { ok: true, value: !!raw }; }
    var text = String(raw === undefined ? '' : raw).trim();
    var n = Number(text);
    var bad = (text === '' || !isFinite(n) || n < row.min || n > row.max || (row.type === 'int' && Math.floor(n) !== n));
    if (bad) {
      return { ok: false, message: t(row.type === 'int' ? 'slam.invalid.int' : 'slam.invalid',
                                      { min: fmtNum(row.min), max: fmtNum(row.max) }) };
    }
    return { ok: true, value: n };
  }

  function slamAllRows() {
    var rows = [];
    if (slamInfo) { slamInfo.groups.forEach(function (g) { g.params.forEach(function (p) { rows.push(p); }); }); }
    return rows;
  }

  function slamDirtyKeys() {
    return slamAllRows().filter(function (row) {
      var r = slamParse(row);
      return !r.ok || r.value !== row.value;
    }).map(function (row) { return row.key; });
  }

  function slamResetDraft() {
    slamDraft = {};
    slamAllRows().forEach(function (row) { slamDraft[row.key] = (row.type === 'bool') ? row.value : fmtNum(row.value); });
  }

  // อัปเดตสถานะของแต่ละแถว (ไม่สร้าง DOM ใหม่ เพื่อไม่ให้ช่องกรอกเสียโฟกัสตอนพิมพ์)
  function updateSlamRows() {
    if (!slamInfo || !slamInfo.available) { return; }
    var running = mappingBusyOrRunning();
    var editable = slamEditable();
    var anyInvalid = false;
    var dirty = slamDirtyKeys();
    slamAllRows().forEach(function (row) {
      var ui = slamRows[row.key];
      if (!ui) { return; }
      var r = slamParse(row);
      var shown = running ? slamShown(row, true) : (r.ok ? r.value : row.value);
      var changed = running ? (shown !== row.default) : (r.ok && r.value !== row.default);
      if (!r.ok && !running) { anyInvalid = true; }
      if (running) {                                            // อ่านอย่างเดียว: แสดงค่าที่ใช้จริง
        if (row.type === 'bool') { ui.input.checked = !!shown; } else { ui.input.value = fmtNum(shown); }
      }
      ui.input.disabled = !editable;
      ui.input.setAttribute('aria-invalid', (!running && !r.ok) ? 'true' : 'false');
      ui.err.hidden = running || r.ok;
      ui.err.textContent = r.ok ? '' : r.message;
      ui.changed.hidden = !changed;
      ui.reset.hidden = !editable || (r.ok && r.value === row.default);
      if (ui.stateText) { ui.stateText.textContent = slamValueText(row, shown); }
    });
    $('slam-save').disabled = !editable || !dirty.length || anyInvalid;
    $('slam-restore').disabled = !editable || slamAllRows().every(function (row) {
      var r = slamParse(row);
      return r.ok && r.value === row.default;
    });
    $('slam-restore').hidden = running;
    $('slam-save').hidden = running;
    var parts = [slamInfo.modified ? t('ui.slam.summary_changed', { n: slamInfo.modified }) : t('ui.slam.summary_default')];
    if (!running && dirty.length) { parts.push(t('ui.slam.unsaved')); }
    $('slam-summary').textContent = parts.join(' · ');
    $('slam-hint').textContent = t(running ? 'ui.slam.hint.running' : 'ui.slam.hint.before');
    var note = $('slam-note');
    var msg = (!running && !editable && authEnabled && !admin) ? t('ui.slam.need_admin') : '';
    note.hidden = !msg;
    note.textContent = msg;
  }

  function buildSlamRow(row) {
    var wrap = document.createElement('div');
    wrap.className = 'slam-row';
    var head = document.createElement('div');
    head.className = 'slam-head';
    var name = document.createElement('label');
    name.className = 'slam-name';
    name.setAttribute('for', 'slam-in-' + row.key);
    name.textContent = t('slam.p.' + row.key + '.name');
    var changed = document.createElement('span');
    changed.className = 'slam-changed';
    changed.textContent = t('ui.slam.changed');
    head.appendChild(name);
    head.appendChild(changed);
    var desc = document.createElement('p');
    desc.className = 'slam-desc';
    desc.textContent = t('slam.p.' + row.key + '.desc');
    var def = document.createElement('p');
    def.className = 'slam-default';
    def.textContent = t('ui.slam.default', { value: slamValueText(row, row.default) });
    var edit = document.createElement('div');
    edit.className = 'slam-edit';
    var input = document.createElement('input');
    input.id = 'slam-in-' + row.key;
    var ui = { input: input, changed: changed };
    if (row.type === 'bool') {
      var label = document.createElement('label');
      label.className = 'slam-switch';
      input.type = 'checkbox';
      input.checked = !!slamDraft[row.key];
      input.addEventListener('change', function () { slamDraft[row.key] = input.checked; updateSlamRows(); });
      var text = document.createElement('span');
      label.appendChild(input);
      label.appendChild(text);
      ui.stateText = text;
      edit.appendChild(label);
    } else {
      input.type = 'text';
      input.className = 'slam-input';
      input.setAttribute('inputmode', row.type === 'int' ? 'numeric' : 'decimal');
      input.value = String(slamDraft[row.key]);
      input.addEventListener('input', function () { slamDraft[row.key] = input.value; updateSlamRows(); });
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
    reset.setAttribute('aria-label', t('ui.slam.reset_one') + ': ' + t('slam.p.' + row.key + '.name'));
    reset.setAttribute('title', t('ui.slam.reset_one'));
    reset.addEventListener('click', function () {
      slamDraft[row.key] = (row.type === 'bool') ? row.default : fmtNum(row.default);
      if (row.type === 'bool') { input.checked = !!row.default; } else { input.value = fmtNum(row.default); }
      updateSlamRows();
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
    wrap.appendChild(edit);
    wrap.appendChild(err);
    slamRows[row.key] = ui;
    return wrap;
  }

  function renderSlam() {
    var box = $('slam-groups');
    while (box.firstChild) { box.removeChild(box.firstChild); }
    slamRows = {};
    var adv = $('slam-adv-body');
    while (adv.firstChild) { adv.removeChild(adv.firstChild); }
    if (!slamInfo) { return; }
    if (!slamInfo.available) {
      $('slam-hint').textContent = t('ui.slam.unavailable');
      $('slam-summary').textContent = '';
      $('slam-save').hidden = true;
      $('slam-restore').hidden = true;
      $('slam-adv').hidden = true;
      $('slam-note').hidden = true;
      return;
    }
    $('slam-adv').hidden = false;
    slamInfo.groups.forEach(function (g) {
      var sec = document.createElement('section');
      sec.className = 'slam-group';
      var h = document.createElement('h3');
      h.textContent = t('slam.group.' + g.id);
      sec.appendChild(h);
      g.params.forEach(function (row) { sec.appendChild(buildSlamRow(row)); });
      box.appendChild(sec);
    });
    slamInfo.advanced.forEach(function (a) {
      var tr = document.createElement('tr');
      [a.key, String(a.value)].forEach(function (text) {
        var td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      });
      adv.appendChild(tr);
    });
    updateSlamRows();
  }

  // keepDraft: โหลดซ้ำเพื่ออัปเดตค่าที่บันทึกไว้/ค่าที่ใช้จริง โดยไม่ทิ้งสิ่งที่ผู้ใช้กำลังแก้ค้างอยู่
  function loadSlam(keepDraft) {
    return api('/api/slam/params').then(function (r) {
      if (r.status !== 200 || !r.data || r.data.groups === undefined) { return; }
      var oldDraft = slamDraft;
      var edited = keepDraft ? slamDirtyKeys() : [];      // เฉพาะช่องที่ผู้ใช้แก้จริง (ที่เหลือรับค่าที่บันทึกล่าสุดจากเซิร์ฟเวอร์)
      slamInfo = r.data;
      slamResetDraft();
      edited.forEach(function (key) {
        if (oldDraft[key] !== undefined && slamDraft[key] !== undefined) { slamDraft[key] = oldDraft[key]; }
      });
      renderSlam();
    });
  }

  function saveSlam() {
    var values = {};
    slamAllRows().forEach(function (row) {
      var r = slamParse(row);
      if (r.ok && r.value !== row.value) { values[row.key] = r.value; }
    });
    if (!Object.keys(values).length) { return; }
    api('/api/slam/params', { values: values }).then(function (r) {
      if (r.status === 200 && r.data && r.data.ok) {
        slamInfo = r.data.params;
        slamResetDraft();
        renderSlam();
        toast(t('toast.slam_saved'));
      } else if (r.status === 401) {
        admin = false;
        renderAuth();
        toast(t('toast.session_expired'));
        openPin();
      } else {
        toast(tm(r.data && r.data.error) || t('toast.slam_failed'));
      }
    });
  }

  $('slam-save').addEventListener('click', saveSlam);
  $('slam-restore').addEventListener('click', function () {
    slamAllRows().forEach(function (row) {
      var ui = slamRows[row.key];
      slamDraft[row.key] = (row.type === 'bool') ? row.default : fmtNum(row.default);
      if (ui) { if (row.type === 'bool') { ui.input.checked = !!row.default; } else { ui.input.value = fmtNum(row.default); } }
    });
    updateSlamRows();
  });

  // เรียกจาก renderMapPage: โหลดค่าเมื่อเข้าหน้าครั้งแรก และโหลดใหม่เมื่อสถานะรัน/ไม่รันเปลี่ยน (เพื่อได้ค่าที่ใช้จริง)
  function syncSlam() {
    var key = mappingBusyOrRunning() ? 'run' : 'idle';
    if (!slamInfo || key !== slamRunKey) {
      slamRunKey = key;
      loadSlam(true);
    } else {
      updateSlamRows();
    }
  }

