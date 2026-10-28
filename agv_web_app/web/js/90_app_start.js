  // ── ภาษา: สลับ EN/TH แล้ววาดทุกอย่างใหม่จากสถานะล่าสุด (ไม่ต้องโหลดหน้าใหม่) ──
  function syncLangButtons() {
    Array.prototype.forEach.call(document.querySelectorAll('.lang-btn'), function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-lang') === I18N.getLang() ? 'true' : 'false');
    });
  }

  function renderPlaceholders() {
    if (phase === 'down') { renderDisconnected(); return; }
    if (phase === 'connecting') {
      setPill('off', t('pill.connecting'), false);
      $('act-text').textContent = t('ui.status.waiting');
      lastActKey = '';                                 // ข้อความถูกเขียนทับตรงๆ → ครั้งหน้าต้องวาดการ์ดสถานะใหม่
    }
  }

  function rerender() {
    lastChecksKey = '';
    lastChipsKey = '';
    syncLangButtons();
    renderAuth();                                    // วาดการ์ดโหมด + ป้ายโหมดใหม่ด้วย
    if (lastStatus) {
      renderHome(lastStatus.readiness, lastStatus.localization, lastStatus.activity);
      renderActivity(lastStatus.activity);
    }
    renderPlaceholders();
    if (lastHistory && !inspecting()) { drawCharts(lastHistory); }
    if (lastStats) { renderStats(lastStats); }
    renderMapAny();
    renderDisp();
    applySpeedUi();
    renderDrive();
    renderSave();
    renderLibrary(true);
    renderNavPose();
    wpRender();                                         // ข้อความภาษาใหม่ของหน้า Waypoint (ร่างที่กำลังแก้คงไว้)
    renderRun();
    renderLine(true);
    if (navInfo) { renderNavParams(); }                 // ข้อความภาษาใหม่ (คงค่าที่กำลังแก้ไว้)
    if (renameFrom !== null) { $('rename-from').textContent = t('lib.rename.from', { name: renameFrom }); renameCheck(); }
    renderSlam();                                       // ข้อความภาษาใหม่ (คงค่าที่กำลังแก้ไว้)
    if (!$('confirm-dialog').open) { $('confirm-ok').textContent = t('ui.confirm.ok'); }    // ปุ่มของหน้าต่างที่ปิดอยู่ตามภาษาใหม่
  }

  Array.prototype.forEach.call(document.querySelectorAll('.lang-btn'), function (b) {
    b.addEventListener('click', function () { I18N.setLang(b.getAttribute('data-lang')); });
  });
  I18N.onChange(rerender);

  // ── การเชื่อมต่อ ──────────────────────────────────────────────────
  function renderDisconnected() {
    setPill('off', t('pill.disconnected'), false);
    renderChips([], null);
    renderActivity({ level: 'off', text: { code: 'act.disconnected' },
                     detail: { code: 'act.disconnected.detail' } });
  }

  function setConnected(ok) {
    var changed = (connected !== ok);
    connected = ok;
    phase = ok ? 'live' : 'down';
    if (!ok) {
      lastChecksKey = '';
      renderDisconnected();
      setStale(true);
    }
    if (changed) { renderMode(lastMode); }
  }

  function connect() {
    var proto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
    socket = new WebSocket(proto + location.host + '/ws');

    socket.onopen = function () { retryMs = 1000; };
    socket.onmessage = function (ev) {
      lastMsg = Date.now();
      setConnected(true);
      try {
        var s = JSON.parse(ev.data);
        lastStatus = s;
        renderHome(s.readiness, s.localization, s.activity);
        renderMode(s.mode);
        renderActivity(s.activity);
        renderNow(s);
      } catch (e) {
        console.error('bad status message', e);
      }
    };
    socket.onclose = function () {
      setConnected(false);
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
    socket.onerror = function () { socket.close(); };
  }

  // กรณี socket ยังเปิดอยู่แต่ backend เงียบ (เช่น node ค้าง)
  setInterval(function () {
    if (lastMsg && Date.now() - lastMsg > STALE_MS) { setConnected(false); }
  }, 1000);

  setInterval(refreshSession, SESSION_POLL_MS);
  setInterval(pollHistory, HISTORY_POLL_MS);
  setInterval(pollStats, STATS_POLL_MS);
  setInterval(pollMap, MAP_POLL_MS);
  setInterval(pollLaser, LASER_POLL_MS);
  setInterval(pollLibrary, LIB_POLL_MS);
  setInterval(pollGoal, GOAL_POLL_MS);
  setInterval(pollStart, 3000);
  setInterval(pollRoutes, WP_POLL_MS);
  setInterval(pollRun, RUN_POLL_MS);
  setInterval(pollLine, LINE_POLL_MS);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { refreshSession(); pollHistory(); pollStats(); renderMapAny(); pollMap(); pollLibrary(); }
    syncDrive();                                       // แท็บถูกซ่อน = หยุดและตัดการขับทันที
  });

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (lastHistory && !inspecting()) { drawCharts(lastHistory); }
      drawMap();
    }, 150);
  });

  I18N.init();
  syncLangButtons();
  renderDisp();
  applySpeedUi();
  renderDrive();
  renderSave();
  renderPlaceholders();
  route();
  connect();
  refreshSession();
