  // ── คลังแผนที่ (หน้า Navigation): ภาพย่อ + เลือกใช้ / เปลี่ยนชื่อ / ลบ ─────────────────────────────
  // กฎจริง (ห้ามแก้ตอน Nav รัน / ห้ามแตะแผนที่ที่ถูกเลือก) ตัดสินที่เซิร์ฟเวอร์ ; หน้านี้แค่ปิดปุ่มและบอกเหตุผลล่วงหน้า
  var LIB_POLL_MS = 5000;
  // รอ Navigation หยุดจริงก่อนเริ่มใหม่ : backend ปิด SIGINT 20 s → SIGTERM 10 s → SIGKILL 5 s แล้วรอ node หายจาก graph ≤ 30 s
  // (รวมเลวร้ายสุด ~65 s) จึงรอ 90 s — สั้นกว่านี้จะบอกว่า "ปิดไม่ทัน" ทั้งที่ระบบแค่ปิดช้า
  var LIB_RESTART_WAIT_MS = 90000;
  var lib = { maps: null, sel: null, failed: false, busy: false, key: '' };    // maps/sel = คำตอบล่าสุดของเซิร์ฟเวอร์
  var renameFrom = null;                                                          // ชื่อที่กำลังเปลี่ยน (null = หน้าต่างปิดอยู่)
  var renameBusy = false;

  function onNavPage() { return (ROUTES[location.hash] || 'home') === 'nav'; }

  function navRunning() {            // Navigation รันหรือกำลังเปิด/ปิดอยู่
    return !!(lastMode && (lastMode.mode === 'NAVIGATION' || lastMode.target === 'NAVIGATION'));
  }

  function modeKey() {               // ส่วนของสถานะโหมดที่มีผลกับปุ่มในคลัง (เปลี่ยน = วาดการ์ดใหม่)
    var m = lastMode || {};
    return [m.mode, m.state, m.target, (m.external || []).length, !!m.leftover];
  }

  function libEl(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }

  function libChip(cls, mark, text) {
    var chip = libEl('span', 'chip ' + cls);
    chip.appendChild(libEl('span', 'chip-mark', mark));
    chip.appendChild(document.createTextNode(' ' + text));
    return chip;
  }

  function libButton(label, aria, cls, onClick, disabled) {
    var b = libEl('button', 'btn ' + cls, label);
    b.setAttribute('type', 'button');
    b.setAttribute('aria-label', aria);
    b.disabled = !!disabled;
    b.addEventListener('click', onClick);
    return b;
  }

  function loadLibrary() {
    return Promise.all([api('/api/map/saved'), api('/api/map/selected')]).then(function (rs) {
      var ok = rs[0].status === 200 && !!rs[0].data && Array.isArray(rs[0].data.maps);
      lib.failed = !ok;
      if (ok) { lib.maps = rs[0].data.maps; }
      if (rs[1].status === 200 && rs[1].data) { lib.sel = rs[1].data; }
      renderLibrary(false);
    });
  }

  function pollLibrary() {
    if (onNavPage() && !document.hidden) { loadLibrary(); loadNavParams(); }
  }

  function renderNavMapStatus() {
    var sel = lib.sel;
    var cur = $('nav-map-current');
    var run = $('nav-map-running');
    if (!sel) { cur.textContent = ''; run.hidden = true; return; }
    if (sel.name && sel.available) { cur.textContent = MARK.ok + ' ' + t('nav.map.selected', { name: sel.name }); }
    else if (sel.name) { cur.textContent = MARK.warn + ' ' + t('nav.map.missing', { name: sel.name }); }
    else {
      var cfg = sel.choice && sel.choice.source === 'config';
      cur.textContent = MARK.off + ' ' + t(cfg ? 'nav.map.config' : 'nav.map.default');
    }
    var inUse = sel.in_use;
    run.hidden = !inUse;
    if (inUse) {
      var running = (inUse.source === 'selected') ? t('nav.map.running', { name: inUse.name }) : t('nav.map.running_other');
      run.textContent = sel.restart_needed ? (MARK.warn + ' ' + running + ' ' + t('nav.map.restart')) : (MARK.ok + ' ' + running);
    }
  }

  function libTile(m) {
    var sel = lib.sel || {};
    var isSel = !!(sel.name && sel.name === m.name);
    var inUse = !!(sel.in_use && sel.in_use.source === 'selected' && sel.in_use.name === m.name);
    var tile = libEl('div', 'lib-tile' + (isSel ? ' selected' : ''));

    var thumb = libEl('div', 'lib-thumb');
    var img = document.createElement('img');
    img.setAttribute('alt', t('lib.thumb_alt', { name: m.name }));
    img.setAttribute('loading', 'lazy');
    img.onerror = function () { img.hidden = true; };
    img.src = '/api/map/saved/' + encodeURIComponent(m.name) + '.png?v=' + Math.floor(m.saved || 0);
    thumb.appendChild(img);
    tile.appendChild(thumb);

    tile.appendChild(libEl('h3', 'lib-name', m.name));
    if (isSel || inUse) {
      var badges = libEl('p', 'lib-badges');
      if (isSel) { badges.appendChild(libChip('ok', MARK.ok, t('lib.badge.selected'))); }
      if (inUse) { badges.appendChild(libChip('ok', MARK.ok, t('lib.badge.in_use'))); }
      tile.appendChild(badges);
    }
    var meta = [];
    if (typeof m.saved === 'number') { meta.push(fmtClock(m.saved)); }
    if (m.size_m && typeof m.size_m.w === 'number' && typeof m.size_m.h === 'number') {
      meta.push(t('lib.size', { w: fmt(m.size_m.w, 1), h: fmt(m.size_m.h, 1) }));
    }
    tile.appendChild(libEl('p', 'note lib-meta', meta.join(' · ')));

    if (admin && authEnabled) {
      var locked = isSel || navRunning();                       // เซิร์ฟเวอร์จะปฏิเสธอยู่แล้ว → ปิดปุ่มพร้อมบอกเหตุผล
      var acts = libEl('div', 'lib-actions');
      var reasons = [];
      // เริ่ม (หรือเริ่มใหม่) Navigation ด้วยแผนที่นี้ในครั้งเดียว — ถ้า Nav รันด้วยแผนที่นี้อยู่แล้วไม่ต้องมีปุ่ม
      if (!inUse) {
        var mode = lastMode || {};
        var restart = (mode.mode === 'NAVIGATION' && mode.state === 'RUNNING' && !mode.target);
        var modeBusy = (mode.state === 'STARTING' || mode.state === 'STOPPING' || !!mode.target);
        var foreign = !!((mode.external && mode.external.length) || mode.leftover);
        if (modeBusy) { reasons.push(t('lib.reason.mode_busy')); }
        else if (foreign) { reasons.push(t('lib.reason.external')); }
        acts.appendChild(libButton(t(restart ? 'lib.btn.restart' : 'lib.btn.start'),
          t(restart ? 'lib.aria.restart' : 'lib.aria.start', { name: m.name }), 'wide',
          function () { libStart(m.name, restart); }, lib.busy || modeBusy || foreign));
      }
      if (isSel) {
        acts.appendChild(libButton(t('lib.btn.clear'), t('lib.aria.clear', { name: m.name }), 'ghost wide',
          function () { libSelect(null); }, lib.busy));
      } else {
        acts.appendChild(libButton(t('lib.btn.use'), t('lib.aria.use', { name: m.name }), 'ghost wide',
          function () { libSelect(m.name); }, lib.busy));
      }
      acts.appendChild(libButton(t('lib.btn.rename'), t('lib.aria.rename', { name: m.name }), 'ghost',
        function () { openRename(m.name); }, lib.busy || locked));
      acts.appendChild(libButton(t('lib.btn.delete'), t('lib.aria.delete', { name: m.name }), 'ghost danger',
        function () { libDelete(m.name); }, lib.busy || locked));
      tile.appendChild(acts);
      if (locked) { reasons.push(t(isSel ? 'lib.reason.selected' : 'lib.reason.nav_running')); }
      reasons.forEach(function (text) { tile.appendChild(libEl('p', 'note lib-reason', text)); });
    }
    return tile;
  }

  function renderLibrary(force) {
    if (!onNavPage()) { return; }
    var key = JSON.stringify([lib.maps, lib.sel, lib.failed, lib.busy, admin, authEnabled, modeKey()]);
    if (!force && key === lib.key) { return; }                  // ไม่เปลี่ยน = ไม่วาดใหม่ (กันภาพย่อกะพริบ)
    lib.key = key;
    renderNavMapStatus();
    var maps = lib.maps || [];
    var stateText = '';
    if (lib.failed) { stateText = t('lib.load_failed'); }       // โหลดไม่ได้ต้องมาก่อน "ว่างเปล่า" (ไม่ให้เข้าใจผิดว่าไม่มีแผนที่)
    else if (lib.maps === null) { stateText = t('lib.loading'); }
    else if (!maps.length) { stateText = t('lib.empty'); }
    $('maplib-state').textContent = stateText;
    $('maplib-state').hidden = !stateText;
    $('maplib-goto').hidden = !(!lib.failed && lib.maps !== null && !maps.length);
    $('maplib-count').textContent = (lib.maps === null) ? ''
      : (maps.length === 1 ? t('lib.count_one') : t('lib.count_many', { count: maps.length }));
    var noteText = !authEnabled ? t('drive.st.no_pin') : (!admin ? t('lib.need_admin') : '');
    $('maplib-note').textContent = noteText;
    $('maplib-note').hidden = !noteText;
    var list = $('maplib-list');
    while (list.firstChild) { list.removeChild(list.firstChild); }
    maps.forEach(function (m) { list.appendChild(libTile(m)); });
  }

  // คำสั่งของ Admin: หมดอายุ (401) → กลับไปให้ใส่ PIN ; ผลลัพธ์อื่นให้ผู้เรียกจัดการ
  function libExpired() {
    admin = false;
    renderAuth();
    toast(t('toast.session_expired'));
    openPin();
  }

  function libRequest(path, body) {
    lib.busy = true;
    renderLibrary(false);
    return api(path, body).then(function (r) {
      lib.busy = false;
      if (r.status === 401) { libExpired(); }
      return r;
    });
  }

  // รอจนระบบเข้าโหมดที่ต้องการจริง (ดูจากสถานะที่ WebSocket ส่งมา) ; คืน 'ok' | 'error' | 'timeout'
  function waitMode(mode, timeoutMs) {
    return new Promise(function (resolve) {
      var left = timeoutMs;
      (function check() {
        var m = lastMode;
        if (m && m.mode === mode && m.state === 'RUNNING' && !m.target) { resolve('ok'); return; }
        if (m && m.state === 'ERROR') { resolve('error'); return; }
        left -= 300;
        if (left <= 0) { resolve('timeout'); return; }
        setTimeout(check, 300);
      })();
    });
  }

  // เริ่ม Navigation ด้วยแผนที่นี้ในครั้งเดียว : เลือกแผนที่ (ถ้ายังไม่ได้เลือก) → สั่งโหมด
  // restart = Nav รันด้วยแผนที่อื่นอยู่ → ต้องหยุดให้จบก่อน (backend ไม่รีสตาร์ตเองเมื่อสั่งโหมดเดิมซ้ำ)
  function libStart(name, restart) {
    if (lib.busy) { return; }
    var mode = lastMode || {};
    var items = [];
    if (restart) { items.push(t('confirm.lib_restart.stop')); }
    else if (mode.mode && mode.mode !== 'STANDBY' && mode.mode !== 'NAVIGATION') {
      items.push(t('confirm.lib_start.switch', { mode: modeName(mode.mode) }));
    }
    items.push(t('confirm.lib_start.pose'));
    var unsaved = mapUnsaved();                                 // ออกจาก Mapping โดยยังไม่บันทึก → เตือนเหมือนปุ่มโหมดบน Home
    if (unsaved) { items.unshift(t('confirm.unsaved')); }
    confirmDialog(t(restart ? 'confirm.lib_restart.title' : 'confirm.lib_start.title', { name: name }),
      t('confirm.lib_start.body'), t(restart ? 'confirm.lib_restart.ok' : 'confirm.lib_start.ok'),
      { warn: unsaved, items: items }).then(function (ok) {
      if (!ok || lib.busy) { return; }
      lib.busy = true;
      renderLibrary(false);
      var selected = (lib.sel && lib.sel.name === name)
        ? Promise.resolve(true)
        : api('/api/map/select', { name: name }).then(function (r) {
          if (r.status === 200 && r.data && r.data.ok === true) { return true; }
          if (r.status === 401) { libExpired(); }
          else { toast(tm(r.data && r.data.error) || t('toast.lib_failed')); }
          return false;
        });
      selected.then(function (good) {
        if (!good) { return false; }
        if (!restart) { return sendMode('NAVIGATION'); }
        return sendMode('STANDBY').then(function (sent) {
          if (!sent) { return false; }
          toast(t('toast.lib_restarting'));                     // หลังเซิร์ฟเวอร์รับคำสั่งหยุดแล้ว (ไม่ให้ toast ของ sendMode ทับ)
          return waitMode('STANDBY', LIB_RESTART_WAIT_MS).then(function (result) {
            if (result !== 'ok') {
              toast(t(result === 'timeout' ? 'toast.lib_restart_timeout' : 'toast.mode_failed'));
              return false;
            }
            return sendMode('NAVIGATION');
          });
        });
      }).then(function () {
        lib.busy = false;
        return loadLibrary();
      });
    });
  }

  function libDone(r, okText) {
    if (r.status === 200 && r.data && r.data.ok === true) { toast(okText); }
    else if (r.status !== 401) { toast(tm(r.data && r.data.error) || t('toast.lib_failed')); }
    return loadLibrary();
  }

  function libSelect(name) {
    if (lib.busy) { return; }
    libRequest('/api/map/select', { name: name }).then(function (r) {
      libDone(r, name === null ? t('toast.nav_map_cleared') : t('toast.nav_map_selected', { name: name }));
    });
  }

  function libDelete(name) {
    if (lib.busy) { return; }
    confirmDialog(t('confirm.lib_delete.title', { name: name }), t('confirm.lib_delete.body'),
      t('confirm.lib_delete.ok'), { warn: true }).then(function (ok) {
      if (!ok) { return; }
      libRequest('/api/map/delete', { name: name }).then(function (r) {
        libDone(r, t('toast.map_deleted', { name: name }));
      });
    });
  }

  // ── เปลี่ยนชื่อ (หน้าต่างเล็ก) : ตรวจรูปแบบ/ชื่อซ้ำทันที ส่วนกฎจริงให้เซิร์ฟเวอร์ตัดสิน ──────────
  function renameCheck() {
    if (renameFrom === null) { return; }
    var v = $('rename-input').value.trim();
    var valid = NAME_RE.test(v);
    var err = '';
    if (v !== '' && !valid) { err = t('save.invalid'); }
    else if (valid && v !== renameFrom && (lib.maps || []).some(function (m) { return m.name === v; })) {
      err = t('map.err.exists', { name: v });
    }
    $('rename-error').textContent = err;
    $('rename-input').setAttribute('aria-invalid', err ? 'true' : 'false');
    $('rename-submit').disabled = !(valid && v !== renameFrom && !err) || renameBusy;
  }

  function openRename(name) {
    renameFrom = name;
    renameBusy = false;
    $('rename-from').textContent = t('lib.rename.from', { name: name });
    $('rename-input').value = name;
    renameCheck();
    openDialog($('rename-dialog'));
    setTimeout(function () { $('rename-input').focus(); }, 50);
  }

  $('rename-input').addEventListener('input', renameCheck);
  $('rename-cancel').addEventListener('click', function () { closeDialog($('rename-dialog')); });
  $('rename-dialog').addEventListener('close', function () {
    renameFrom = null;
    renameBusy = false;
    // ล้างข้อความ (กันค้างเป็นภาษาเดิมถ้าผู้ใช้สลับภาษาตอนหน้าต่างปิดอยู่ — เปิดครั้งหน้าจะเติมใหม่)
    $('rename-from').textContent = '';
    $('rename-error').textContent = '';
    $('rename-input').value = '';
  });

  $('rename-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (renameFrom === null || $('rename-submit').disabled) { return; }
    var from = renameFrom;
    var to = $('rename-input').value.trim();
    renameBusy = true;
    renameCheck();
    libRequest('/api/map/rename', { name: from, new_name: to }).then(function (r) {
      renameBusy = false;
      if (r.status === 200 && r.data && r.data.ok === true) {
        closeDialog($('rename-dialog'));
        libDone(r, t('toast.map_renamed', { name: to }));
      } else if (r.status === 401) {
        closeDialog($('rename-dialog'));
      } else {
        $('rename-error').textContent = tm(r.data && r.data.error) || t('toast.lib_failed');   // เหตุผลจากเซิร์ฟเวอร์ แสดงในหน้าต่างเดิม
        $('rename-submit').disabled = false;
        loadLibrary();
      }
    });
  });

