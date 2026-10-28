  // ── การแสดงผลแบบ rviz2 (แถบซ้าย): เปิด/ปิด สี ขนาดจุด ความทึบ เวลาจางหายของสแกนเลเซอร์ · ความทึบแผนที่ ──
  // เป็นค่าของหน้าจอนี้เท่านั้น (เก็บในเบราว์เซอร์ ไม่ส่งไปที่หุ่น)
  var DISP_KEY = 'agv.map.display';
  var DISP_COLORS = { red: '#ff3b30', green: '#34c759', cyan: '#2bd9f2', yellow: '#ffd60a', white: '#ffffff' };
  var DISP_DEFAULT = { laser: true, size: 3, color: 'red', alpha: 100, decay: 0, mapAlpha: 100, robot: true, scalebar: true };

  function clampNum(v, lo, hi, step, fallback) {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') { return fallback; }   // (Number(null) = 0 จะกลายเป็นค่าต่ำสุดโดยไม่ตั้งใจ)
    v = Number(v);
    if (!isFinite(v)) { return fallback; }
    v = Math.max(lo, Math.min(hi, v));
    return step ? Math.round(v / step) * step : v;
  }

  function sanitizeDisp(raw) {
    var d = {};
    raw = (raw && typeof raw === 'object') ? raw : {};
    d.laser = (typeof raw.laser === 'boolean') ? raw.laser : DISP_DEFAULT.laser;
    d.robot = (typeof raw.robot === 'boolean') ? raw.robot : DISP_DEFAULT.robot;
    d.scalebar = (typeof raw.scalebar === 'boolean') ? raw.scalebar : DISP_DEFAULT.scalebar;
    d.size = clampNum(raw.size, 1, 8, 1, DISP_DEFAULT.size);
    d.alpha = clampNum(raw.alpha, 10, 100, 5, DISP_DEFAULT.alpha);
    d.decay = clampNum(raw.decay, 0, 10, 0.5, DISP_DEFAULT.decay);
    d.mapAlpha = clampNum(raw.mapAlpha, 20, 100, 5, DISP_DEFAULT.mapAlpha);
    d.color = (typeof raw.color === 'string' && DISP_COLORS[raw.color]) ? raw.color : DISP_DEFAULT.color;
    return d;
  }

  function loadDisp() {
    try { return sanitizeDisp(JSON.parse(window.localStorage.getItem(DISP_KEY))); } catch (e) { return sanitizeDisp(null); }
  }

  var disp = loadDisp();

  function saveDisp() {
    try { window.localStorage.setItem(DISP_KEY, JSON.stringify(disp)); } catch (e) { /* ไม่เป็นไร */ }
  }

  function renderDisp() {
    $('disp-laser').checked = disp.laser;
    $('disp-size').value = String(disp.size);
    $('disp-size-out').textContent = t('disp.px', { n: disp.size });
    $('disp-alpha').value = String(disp.alpha);
    $('disp-alpha-out').textContent = t('disp.pct', { n: disp.alpha });
    $('disp-decay').value = String(disp.decay);
    $('disp-decay-out').textContent = t('disp.sec', { n: fmtNum(disp.decay) });
    $('disp-map-alpha').value = String(disp.mapAlpha);
    $('disp-map-alpha-out').textContent = t('disp.pct', { n: disp.mapAlpha });
    $('disp-robot').checked = disp.robot;
    $('disp-scalebar').checked = disp.scalebar;
    var box = $('disp-colors');
    while (box.firstChild) { box.removeChild(box.firstChild); }
    Object.keys(DISP_COLORS).forEach(function (name) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'swatch';
      b.style.background = DISP_COLORS[name];
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', disp.color === name ? 'true' : 'false');
      b.setAttribute('aria-label', t('disp.color.' + name));
      b.setAttribute('title', t('disp.color.' + name));
      b.addEventListener('click', function () { disp.color = name; saveDisp(); renderDisp(); drawMap(); });
      box.appendChild(b);
    });
  }

  (function wireDisp() {
    function bind(id, key, parse) {
      $(id).addEventListener('input', function () { disp[key] = parse($(id)); saveDisp(); renderDisp(); drawMap(); });
    }
    bind('disp-laser', 'laser', function (el) { return !!el.checked; });
    bind('disp-robot', 'robot', function (el) { return !!el.checked; });
    bind('disp-scalebar', 'scalebar', function (el) { return !!el.checked; });
    bind('disp-size', 'size', function (el) { return clampNum(el.value, 1, 8, 1, DISP_DEFAULT.size); });
    bind('disp-alpha', 'alpha', function (el) { return clampNum(el.value, 10, 100, 5, DISP_DEFAULT.alpha); });
    bind('disp-decay', 'decay', function (el) { return clampNum(el.value, 0, 10, 0.5, DISP_DEFAULT.decay); });
    bind('disp-map-alpha', 'mapAlpha', function (el) { return clampNum(el.value, 20, 100, 5, DISP_DEFAULT.mapAlpha); });
    $('disp-reset').addEventListener('click', function () { disp = sanitizeDisp(null); saveDisp(); laserBuf = []; renderDisp(); drawMap(); });
  })();

  // ── สแกนเลเซอร์สด: ดึงจุด (พิกัด map) ~5 Hz แล้วเก็บย้อนหลังตาม Decay time ────────────────────────
  var LASER_POLL_MS = 200;
  var laserBuf = [];                // [{ t: เวลาที่ได้รับ (ms), pts: [[x,y]…] }]
  var laserVersion = -1;

  function pruneLaser() {
    var keep = disp.decay * 1000;
    var now = Date.now();
    laserBuf = laserBuf.filter(function (s, i) { return i === laserBuf.length - 1 || now - s.t <= keep; }).slice(-80);
  }

  function pollLaser() {
    var pg = mapPage();
    if (!pg || !mapPageLive(pg) || !disp.laser) { return; }
    api('/api/scan/live').then(function (r) {
      if (mapPage() !== pg) { return; }
      var d = r.data;
      if (r.status === 200 && d && d.available && d.age <= 2) {
        if (d.version !== laserVersion) {
          laserVersion = d.version;
          laserBuf.push({ t: Date.now(), pts: d.points });
        }
      } else {
        laserBuf = [];                                         // ไม่มีสแกนสด → ไม่วาดจุดเก่าค้าง
      }
      pruneLaser();
      drawMap();
    });
  }

  function drawLaser(ctx, X, Y) {
    if (!disp.laser || !laserBuf.length) { return; }
    var s = disp.size;
    ctx.save();
    ctx.fillStyle = DISP_COLORS[disp.color];
    ctx.globalAlpha = disp.alpha / 100;
    laserBuf.forEach(function (scan) {
      scan.pts.forEach(function (p) { ctx.fillRect(X(p[0]) - s / 2, Y(p[1]) - s / 2, s, s); });
    });
    ctx.restore();
  }

