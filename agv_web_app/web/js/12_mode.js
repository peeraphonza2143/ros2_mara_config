  // ── โหมดระบบ ──────────────────────────────────────────────────────
  function modeName(mode) {
    return I18N.DICT.en['mode.name.' + mode] !== undefined ? t('mode.name.' + mode) : String(mode || '');
  }

  function hhmm(epochSeconds) {
    if (typeof epochSeconds !== 'number') { return ''; }
    return new Date(epochSeconds * 1000)
      .toLocaleTimeString(t('time.locale'), { hour: '2-digit', minute: '2-digit' });
  }

  function renderMode(m) {
    if (!m) { return; }
    lastMode = m;
    renderLibrary(false);                                    // Nav เริ่ม/หยุด → ปุ่มเปลี่ยนชื่อ/ลบเปลี่ยนสถานะ
    navModeChanged();
    var leftover = m.leftover || null;                       // ระบบที่แอปเปิดไว้ในรอบก่อน แล้วค้างมา
    var external = (m.external && m.external.length) ? m.external : (leftover ? ['—'] : null);
    var busy = (m.state === 'STARTING' || m.state === 'STOPPING');

    $('mode-name').textContent = external ? t('mode.ext.name') : modeName(m.mode);
    $('mode-th').textContent = external ? t('mode.ext.desc') : t('mode.desc.' + m.mode);

    var cls = 'ok';
    var label = t('mode.state.running');
    var msg = '';
    if (busy) {
      cls = 'warn';
      label = t(m.state === 'STARTING' ? 'mode.state.starting' : 'mode.state.stopping');
      msg = tm(m.message);
    } else if (m.state === 'ERROR') {
      cls = 'bad';
      label = t('mode.state.error');
      msg = t('mode.failed', { detail: tm(m.message) });
    } else if (external) {
      cls = 'warn';
      label = t('mode.state.external');
    } else if (m.mode === 'STANDBY') {
      cls = 'off';
      label = t('mode.state.standby');
      msg = m.message ? tm(m.message) : '';                 // เช่น ผลของ Reset all
    } else {
      msg = t('mode.since', { time: hhmm(m.since) });
    }
    setBadge($('mode-state'), label, cls);
    $('mode-msg').textContent = msg;
    $('mode-spin').hidden = !busy;

    $('mode-ext').hidden = !external;
    if (external) {
      $('mode-ext').textContent = (m.external && m.external.length)
        ? t('mode.ext.banner', { nodes: external }) : t('mode.leftover.banner', { mode: modeName(leftover.mode) });
    }

    var canSwitch = connected && admin && !busy && !external;
    var buttons = document.querySelectorAll('.seg-btn');
    for (var i = 0; i < buttons.length; i++) {
      var mode = buttons[i].getAttribute('data-mode');
      var current = (!external && m.mode === mode && m.state === 'RUNNING');
      buttons[i].setAttribute('aria-pressed', current ? 'true' : 'false');
      buttons[i].disabled = !canSwitch || current;
    }

    var hint = '';
    if (!connected) { hint = t('hint.disconnected'); }
    else if (!authEnabled) { hint = t('hint.no_pin'); }
    else if (!admin) { hint = t('hint.need_admin'); }
    else if (busy) { hint = t('hint.busy'); }
    $('mode-hint').textContent = hint;
    $('mode-login').hidden = !(authEnabled && !admin);
    $('mode-reset').hidden = !(connected && authEnabled && admin);       // Reset all: เฉพาะผู้ดูแล
    $('mode-reset').disabled = busy;
    var resetting = !!(busy && m.message && m.message.code === 'mode.resetting');
    var top = $('reset-top');                                            // ปุ่มเดียวกันบนแถบบน (อยู่ทุกหน้า)
    top.hidden = $('mode-reset').hidden;
    top.disabled = busy;
    top.className = 'icon-btn reset' + (resetting ? ' busy' : '');
    top.setAttribute('title', t('ui.mode.reset'));

    // ป้ายโหมดบนแถบบน: ขาดการเชื่อมต่อ = เทา บอกตรงๆ ไม่แสดงโหมดเก่าที่อาจไม่จริงแล้ว
    if (!connected) {
      setPill('off', t('pill.disconnected'), false);
    } else if (busy) {
      setPill('warn', t(m.state === 'STARTING' ? 'pill.starting' : 'pill.stopping',
                        { mode: modeName(m.state === 'STARTING' ? m.target : m.mode) }), true);
    } else if (m.state === 'ERROR') {
      setPill('bad', t('pill.error'), false);
    } else if (external) {
      setPill('warn', t('pill.external'), false);
    } else {
      setPill(m.mode === 'STANDBY' ? 'off' : 'ok', modeName(m.mode), false);
    }
    syncDrive();
    renderMapAny();
  }

  // โหมด Mapping มีหน้าต่างแจ้งเตือนเฉพาะ (บอกว่ากำลังจะสลับโหมด + แผนที่ที่ไม่บันทึกจะหาย + ต้องเคลื่อนหุ่นเอง)
  function requestMode(mode) {
    var mapping = (mode === 'MAPPING');
    // Mapping: ดึงค่า SLAM ล่าสุดก่อน เพื่อบอกในหน้าต่างเตือนว่ามีกี่ค่าที่ต่างจาก Default (ดึงไม่ได้ก็ใช้ค่าที่จำไว้)
    (mapping ? loadSlam(true) : Promise.resolve()).then(function () {
      var items = [t('confirm.map.1'), t('confirm.map.2'), t('confirm.map.3')];
      if (mapping && slamInfo && slamInfo.available) {
        if (slamInfo.modified) { items.push(t('confirm.map.4', { n: slamInfo.modified })); }
        if (slamDirtyKeys().length) { items.push(t('confirm.map.5')); }
      }
      return confirmDialog(mapping ? t('confirm.title.MAPPING') : t('confirm.title', { mode: modeName(mode) }),
                  mapping ? t('confirm.body.MAPPING')
                          : t(mode === 'STANDBY' ? 'confirm.body.standby' : 'confirm.body.switch'),
                  mapping ? t('confirm.ok.MAPPING') : t('ui.confirm.ok'),
                  mapping ? { warn: true, items: items }
                          : (mapUnsaved() ? { warn: true, items: [t('confirm.unsaved')] } : undefined));   // ออกจาก Mapping โดยยังไม่บันทึก
    })
      .then(function (ok) {
        if (!ok) { return; }
        sendMode(mode);
      });
  }

  // ส่งคำสั่งเปลี่ยนโหมดไป backend + แจ้งผล (ใช้ร่วมกับปุ่มเริ่ม Navigation ในคลังแผนที่) ; คืน Promise<true ถ้าเซิร์ฟเวอร์รับคำสั่ง>
  function sendMode(mode) {
    return api('/api/mode', { mode: mode }).then(function (r) {
      if (r.status === 202) {
        toast(t('toast.changing'));
        if (r.data.mode) { renderMode(r.data.mode); }
        return true;
      }
      if (r.status === 401) {
        admin = false;
        renderAuth();
        toast(t('toast.session_expired'));
        openPin();
      } else {
        toast(tm(r.data.error) || tm(r.data.message) || t('toast.mode_failed'));
      }
      return false;
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('.seg-btn'), function (btn) {
    btn.addEventListener('click', function () { requestMode(btn.getAttribute('data-mode')); });
  });

  // Reset all: ปิดทุกอย่างของระบบหุ่น (ที่แอปเปิด + ที่ค้าง + ที่เปิดนอกแอป) — ใช้เมื่อระบบค้างอยู่ในโหมดใดโหมดหนึ่ง
  function requestReset() {
    var resetItems = [t('confirm.reset.1'), t('confirm.reset.2'), t('confirm.reset.3')];
    if (mapUnsaved()) { resetItems.unshift(t('confirm.unsaved')); }
    confirmDialog(t('confirm.reset.title'), t('confirm.reset.body'), t('confirm.reset.ok'),
                  { warn: true, items: resetItems })
      .then(function (ok) {
        if (!ok) { return; }
        api('/api/mode/reset', {}).then(function (r) {
          if (r.status === 202) {
            toast(t('toast.resetting'));
            if (r.data.mode) { renderMode(r.data.mode); }
          } else if (r.status === 401) {
            admin = false;
            renderAuth();
            toast(t('toast.session_expired'));
            openPin();
          } else {
            toast(tm(r.data.error) || tm(r.data.message) || t('toast.reset_failed'));
          }
        });
      });
  }
  $('mode-reset').addEventListener('click', requestReset);
  $('reset-top').addEventListener('click', requestReset);

