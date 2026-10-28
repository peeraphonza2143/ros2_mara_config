  // ── จุดเริ่มต้นประจำแผนที่ + รีเฟรชการหาตำแหน่ง ──────────────────────────────────────────────────
  // บันทึกตำแหน่งที่หุ่นรู้ตอนนี้ไว้ต่อแผนที่ ; ทุกครั้งที่ Navigation เริ่ม เซิร์ฟเวอร์วางจุดนี้ให้ AMCL เอง
  var startInfo = null;                                        // ผล /api/nav/startpose { map, pose, auto }
  var startBusy = false;

  function startAutoText() {
    var a = startInfo && startInfo.auto;
    if (!a || !startInfo.pose) { return ''; }
    if (a.state === 'pending') { return t('navstart.auto.pending'); }
    if (a.state === 'applied') { return t('navstart.auto.applied'); }
    if (a.state === 'failed') { return t('navstart.auto.failed', { why: a.error ? t(a.error) : '' }); }
    return '';
  }

  function renderNavStart() {
    var usable = !!(admin && authEnabled && connected && navSettled()) && !startBusy;
    var info = startInfo;
    var text;
    if (!info || !info.map) { text = t('navstart.nomap'); }
    else if (!info.pose) { text = t('navstart.none', { map: info.map }); }
    else { text = t('navstart.saved', { map: info.map, x: info.pose.x.toFixed(2), y: info.pose.y.toFixed(2), deg: Math.round(info.pose.yaw_deg) }); }
    $('navstart-info').textContent = text;
    var auto = startAutoText();
    $('navstart-auto').textContent = auto;
    $('navstart-auto').hidden = !auto;
    $('navstart-save').disabled = !(usable && info && info.map && goalLocOk());
    $('navstart-apply').disabled = !(usable && info && info.pose);
    $('navstart-clear').disabled = !(admin && authEnabled && connected && !startBusy && info && info.pose);
    $('navpose-refresh').disabled = !usable;
  }

  function pollStart() {
    if (!onNavPage() || document.hidden || !connected) { return; }
    return api('/api/nav/startpose').then(function (r) {
      if (r.status !== 200 || !r.data) { return; }
      startInfo = r.data;
      renderNavStart();
    });
  }

  function startAction(action, okKey) {
    startBusy = true;
    renderNavStart();
    return api('/api/nav/startpose', { action: action }).then(function (r) {
      startBusy = false;
      if (r.status === 200 && r.data && r.data.ok) { startInfo = r.data; toast(t(okKey)); }
      else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.navstart_failed')); }
      renderNavStart();
    });
  }

  function navRefresh() {
    startBusy = true;
    renderNavStart();
    return api('/api/nav/refresh', {}).then(function (r) {
      startBusy = false;
      if (r.status === 200 && r.data && r.data.ok) { toast(t('toast.navpose_refreshed')); }
      else if (r.status === 401) { libExpired(); }
      else { toast(tm(r.data && r.data.error) || t('toast.navpose_refresh_failed')); }
      renderNavStart();
    });
  }

  $('navstart-save').addEventListener('click', function () { startAction('save', 'toast.navstart_saved'); });
  $('navstart-apply').addEventListener('click', function () { startAction('apply', 'toast.navstart_applied'); });
  $('navstart-clear').addEventListener('click', function () { startAction('clear', 'toast.navstart_cleared'); });
  $('navpose-refresh').addEventListener('click', navRefresh);

  // Navigation เริ่ม/หยุด → ค่าฐานเปลี่ยน (ค่าที่ใช้อยู่ ↔ ค่าที่บันทึก) : อ่านใหม่ถ้าอยู่หน้านี้
  function navModeChanged() {
    var key = navSettled() ? 'run' : 'idle';
    if (key === navRunKey) { return; }
    navRunKey = key;
    if (onNavPage()) { loadNavParams(); }
    else { navRun = { running: false, values: null, failed: false }; }
  }

