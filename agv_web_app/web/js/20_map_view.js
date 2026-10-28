  // ── Mapping: แผนที่สด (อ่านอย่างเดียว) ──────────────────────────────
  // backend แปลง /map เป็น PNG (1 พิกเซล = 1 ช่อง) ; ที่นี่วางภาพบน canvas ตามพิกัดโลก (เมตร)
  // แล้ววาดตำแหน่งหุ่นจาก status.pose (map→base_link) ทับ ; ซูม/เลื่อนด้วยนิ้ว เมาส์ หรือคีย์บอร์ด
  var MAP_POLL_MS = 3000;
  var MAP_ZOOM_STEP = 1.25;
  var MAP_MIN_SCALE = 2;          // px ต่อเมตร (ต่ำสุด)
  var MAP_MAX_SCALE = 600;
  var mapMeta = null;             // ผล /api/map/live ล่าสุด (หน้า Waypoint: /api/routes/map = แผนที่ที่บันทึกไว้)
  var mapImg = null;              // Image ของ PNG ปัจจุบัน
  var mapImgVersion = -1;
  var mapLoadingVersion = -1;
  var mapView = { scale: 1, cx: 0, cy: 0, auto: true };    // (cx,cy) = พิกัดโลกที่กึ่งกลางจอ ; auto = ปรับให้พอดีเอง
  var mapPointers = {};           // pointerId → {x,y} (สำหรับลาก/ pinch)
  var mapOwner = '';              // หน้าที่เป็นเจ้าของข้อมูล mapMeta/mapImg/laserBuf ตอนนี้ ('mapping' | 'nav' | '')
  // แผนที่ใช้ร่วมกัน 2 หน้า (Mapping = แผนที่สด SLAM, Navigation = แผนที่ที่โหลดอยู่) : ข้อมูลและมุมมองตัวเดียว
  // เพราะผู้ใช้เห็นได้ทีละหน้า ; ที่ต่างกันมีแค่ DOM และเงื่อนไขว่า "ตอนนี้ควรแสดงแผนที่สดหรือยัง"
  var MAP_IDS = {
    mapping: { canvas: 'map-canvas', live: 'map-live', box: 'map-box' },
    nav: { canvas: 'nav-map-canvas', live: 'nav-map-live', box: 'nav-map-box' },
    wp: { canvas: 'wp-map-canvas', live: 'wp-map-live', box: 'wp-map-box' }
  };

  function mappingRunning() {
    var m = lastMode;
    return !!(connected && m && m.mode === 'MAPPING' && m.state === 'RUNNING' && !(m.external && m.external.length));
  }

  function onMapPage() {
    return (ROUTES[location.hash] || 'home') === 'mapping' && !document.hidden;
  }

  function onNavMapPage() {
    return (ROUTES[location.hash] || 'home') === 'nav' && !document.hidden;
  }

  function mapPage() {            // หน้าที่มีแผนที่และกำลังแสดงอยู่ ('mapping' | 'nav' | 'wp' | '')
    return onMapPage() ? 'mapping' : (onNavMapPage() ? 'nav' : (onWpMapPage() ? 'wp' : ''));
  }

  function mapPageLive(page) {    // หน้านั้นควรแสดงแผนที่สดอยู่หรือไม่
    if (page === 'mapping') { return mappingRunning(); }
    if (page === 'wp') { return wpMapReady(); }                // Waypoint ใช้แผนที่ที่บันทึกไว้ ไม่ต้องรัน Navigation
    return page === 'nav' ? navSettled() : false;
  }

  function resetMapData() {
    mapMeta = null; mapImg = null; mapImgVersion = -1; mapLoadingVersion = -1;
    laserBuf = []; laserVersion = -1;
  }

  function claimMapData(page) {   // สลับหน้า = ล้างข้อมูลของเก่า (กันแผนที่ SLAM ไปโผล่ที่หน้า Navigation และกลับกัน)
    if (mapOwner !== page) { resetMapData(); mapView.auto = true; mapOwner = page; }
  }

  function renderMapAny() {
    var pg = mapPage();
    if (pg === 'mapping') { renderMapPage(); } else if (pg === 'nav') { renderNavMap(); } else if (pg === 'wp') { renderWpMap(); }
  }

  // ข้อความ + ปุ่มของการ์ด "ยังไม่ได้เปิด Mapping"
  function mapGate() {
    var m = lastMode;
    if (!connected || !m) { return { msg: t('map.gate.disconnected') }; }
    if (m.state === 'STARTING' || m.state === 'STOPPING') {
      return { msg: t(m.state === 'STARTING' && m.target === 'MAPPING' ? 'map.gate.starting' : 'map.gate.busy') };
    }
    if (m.external && m.external.length) { return { msg: t('map.gate.external') }; }
    if (m.leftover) { return { msg: t('map.gate.leftover', { mode: modeName(m.leftover.mode) }) }; }
    var msg = (m.state === 'ERROR') ? t('map.gate.error', { detail: tm(m.message) })
      : (m.mode === 'STANDBY') ? t('map.gate.standby')
      : t('map.gate.other', { mode: modeName(m.mode) });
    if (!authEnabled) { return { msg: msg + ' ' + t('map.gate.no_pin') }; }
    if (!admin) { return { msg: msg + ' ' + t('map.gate.need_admin'), login: true }; }
    return { msg: msg, start: true };
  }

  function renderMapPage() {
    if (!onMapPage()) { return; }
    syncSlam();
    renderSave();
    var live = mappingRunning();
    $('map-live').hidden = !live;
    $('map-gate').hidden = live;
    if (!live) {
      var g = mapGate();
      $('map-gate-msg').textContent = g.msg;
      $('map-start').hidden = !g.start;
      $('map-login').hidden = !g.login;
      mapMeta = null; mapImg = null; mapImgVersion = -1; mapLoadingVersion = -1;   // ไม่ค้างแผนที่เก่า
      laserBuf = []; laserVersion = -1;                                              // และไม่ค้างจุดสแกนเก่า
      mapView.auto = true;
      mapOwner = '';
      return;
    }
    claimMapData('mapping');
    var has = !!(mapMeta && mapMeta.available);
    $('map-wait').hidden = has;
    $('map-info').textContent = has ? t('map.info', {
      w: mapMeta.size_m.w.toFixed(1), h: mapMeta.size_m.h.toFixed(1), res: String(mapMeta.resolution),
      time: hhmm(mapMeta.stamp) }) : '';
    drawMap();
  }

  function cssColor(name, fallback) {
    try {
      var v = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  function mapCanvasSize() {
    var w = Math.max(240, $(MAP_IDS[mapPage() || 'mapping'].box).clientWidth || 320);
    var vh = window.innerHeight || 800;                      // จอใหญ่ (3 ส่วน) แผนที่สูงขึ้นตามหน้าจอ
    return { w: w, h: Math.round(Math.max(260, Math.min(w * 0.85, vh * 0.7, 760))) };
  }

  function fitMapView(size) {
    var wm = mapMeta.size_m.w, hm = mapMeta.size_m.h;
    mapView.scale = Math.max(MAP_MIN_SCALE, Math.min(size.w / (wm * 1.1), size.h / (hm * 1.1)));
    mapView.cx = mapMeta.origin.x + wm / 2;
    mapView.cy = mapMeta.origin.y + hm / 2;
  }

  function drawRobot(ctx, x, y, yawDeg, scale, fill) {
    var r = Math.max(9, Math.min(22, 0.28 * scale));
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(-yawDeg * Math.PI / 180);                  // canvas หมุนตามเข็มนาฬิกา ส่วน yaw หมุนทวนเข็ม
    ctx.beginPath();
    ctx.moveTo(r, 0);
    ctx.lineTo(-r * 0.7, r * 0.65);
    ctx.lineTo(-r * 0.35, 0);
    ctx.lineTo(-r * 0.7, -r * 0.65);
    ctx.closePath();
    ctx.lineJoin = 'round';
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = cssColor('--card', '#ffffff');      // วงแหวนสีพื้น: เห็นชัดทั้งบนพื้นขาว/ดำ
    ctx.fillStyle = fill || cssColor('--series', '#2a78d6');
    ctx.stroke();
    ctx.fill();
    ctx.restore();
  }

  function drawScaleBar(ctx, size, scale) {
    var steps = [0.5, 1, 2, 5, 10, 20, 50, 100];
    var len = steps[0];
    steps.forEach(function (v) { if (v * scale <= 120) { len = v; } });
    var px = len * scale;
    var x = 12, y = size.h - 14;
    ctx.fillStyle = cssColor('--card', '#ffffff');
    ctx.globalAlpha = 0.85;
    ctx.fillRect(x - 6, y - 20, px + 12, 28);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = ctx.fillStyle = cssColor('--text', '#18202b');
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, y - 4); ctx.lineTo(x, y); ctx.lineTo(x + px, y); ctx.lineTo(x + px, y - 4);
    ctx.stroke();
    ctx.font = '600 12px sans-serif';
    ctx.fillText(t('stats.unit.m', { n: String(len) }), x, y - 8);
  }

  function drawMap() {
    var pg = mapPage();
    if (!pg || $(MAP_IDS[pg].live).hidden) { return; }
    var cv = $(MAP_IDS[pg].canvas);
    var size = mapCanvasSize();
    var dpr = window.devicePixelRatio || 1;
    var pw = Math.round(size.w * dpr), ph = Math.round(size.h * dpr);
    if (cv.width !== pw) { cv.width = pw; }                // (เปลี่ยนขนาดจะล้างภาพ → ทำเมื่อขนาดเปลี่ยนจริงเท่านั้น)
    if (cv.height !== ph) { cv.height = ph; }
    cv.style.height = size.h + 'px';
    var ctx = cv.getContext ? cv.getContext('2d') : null;
    if (!ctx) { return; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);

    var pose = lastStatus && lastStatus.pose;
    var has = !!(mapMeta && mapMeta.available && mapImg);
    cv.setAttribute('aria-label', !has ? t('map.aria.waiting') : (pose
      ? t('map.aria.map_robot', { w: mapMeta.size_m.w.toFixed(1), h: mapMeta.size_m.h.toFixed(1),
                                  x: pose.x.toFixed(1), y: pose.y.toFixed(1) })
      : t('map.aria.map', { w: mapMeta.size_m.w.toFixed(1), h: mapMeta.size_m.h.toFixed(1) })));
    if (!has) { return; }

    if (mapView.auto) { fitMapView(size); }
    var sc = mapView.scale;
    var X = function (x) { return size.w / 2 + (x - mapView.cx) * sc; };
    var Y = function (y) { return size.h / 2 - (y - mapView.cy) * sc; };
    var o = mapMeta.origin;
    var wpx = mapMeta.width * mapMeta.resolution * sc, hpx = mapMeta.height * mapMeta.resolution * sc;
    ctx.imageSmoothingEnabled = false;                     // พิกเซลคมชัด ขอบไม่เบลอ
    ctx.save();
    ctx.globalAlpha = disp.mapAlpha / 100;
    ctx.translate(X(o.x), Y(o.y));
    ctx.rotate(-o.yaw);
    ctx.drawImage(mapImg, 0, -hpx, wpx, hpx);              // แถวบนของภาพ = y มากสุด
    ctx.restore();
    drawLaser(ctx, X, Y);                                  // จุดสแกนเลเซอร์ทับแผนที่ (เหมือน LaserScan ใน rviz2)
    if (pose && disp.robot && (pg !== 'wp' || navSettled())) { drawRobot(ctx, X(pose.x), Y(pose.y), pose.yaw_deg, sc); }
    if (pg === 'nav' && poseTool.pending) {                // ตำแหน่งที่กำลังวาง (ยังไม่ส่ง) : สีส้มต่างจากหุ่นจริง
      drawRobot(ctx, X(poseTool.pending.x), Y(poseTool.pending.y), poseTool.pending.yaw * 180 / Math.PI, sc,
                cssColor('--mode-map', '#eb6834'));
    }
    if (pg === 'nav' && goalSt && goalBusy()) {            // จุดหมายที่ส่งไปแล้ว (Nav2 กำลังพาหุ่นไป) : สีเขียว
      drawRobot(ctx, X(goalSt.goal.goal.x), Y(goalSt.goal.goal.y), goalSt.goal.goal.yaw * 180 / Math.PI, sc,
                cssColor('--ok', '#1a7f37'));
    }
    if (pg === 'nav' && straightPreview) {                 // แนวเดินตรงที่รอยืนยัน : เส้นประ + จุดปลาย (สีเขียวอมฟ้า)
      ctx.save();
      ctx.strokeStyle = cssColor('--mode-man', '#1baf7a');
      ctx.lineWidth = 3;
      if (ctx.setLineDash) { ctx.setLineDash([10, 6]); }
      ctx.beginPath();
      ctx.moveTo(X(straightPreview.from.x), Y(straightPreview.from.y));
      ctx.lineTo(X(straightPreview.to.x), Y(straightPreview.to.y));
      ctx.stroke();
      ctx.restore();
      drawRobot(ctx, X(straightPreview.to.x), Y(straightPreview.to.y), straightPreview.to.yaw * 180 / Math.PI, sc,
                cssColor('--mode-man', '#1baf7a'));
    }
    if (pg === 'nav' && goalTool.pending) {                // จุดหมายที่กำลังเลือก (ยังไม่ส่ง) : สีเขียวอมฟ้า
      drawRobot(ctx, X(goalTool.pending.x), Y(goalTool.pending.y), goalTool.pending.yaw * 180 / Math.PI, sc,
                cssColor('--mode-man', '#1baf7a'));
    }
    if (pg === 'wp') { drawWpRoute(ctx, X, Y); }           // Route ที่กำลังแก้ : เส้นเชื่อม + จุดเลขลำดับ + ทิศ
    if (disp.scalebar) { drawScaleBar(ctx, size, sc); }
  }

