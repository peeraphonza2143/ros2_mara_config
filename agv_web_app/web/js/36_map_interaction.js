  function pollMap() {
    var pg = mapPage();
    if (!pg || !mapPageLive(pg)) { return; }
    var src = (pg === 'wp') ? '/api/routes/map' : '/api/map/live';          // Waypoint = ไฟล์แผนที่ที่บันทึกไว้
    api(src).then(function (r) {
      if (mapPage() !== pg) { return; }                    // ผู้ใช้เปลี่ยนหน้าระหว่างรอคำตอบ → ทิ้งผล (เป็นของอีกหน้า)
      if (r.status !== 200 || !r.data) { return; }
      var version = r.data.version;
      mapMeta = r.data;
      if (r.data.available && version !== mapImgVersion && version !== mapLoadingVersion) {
        mapLoadingVersion = version;
        var img = new Image();
        img.onload = function () {
          if (mapPage() !== pg) { mapLoadingVersion = -1; return; }
          mapImg = img; mapImgVersion = version; mapLoadingVersion = -1;
          renderMapAny();
        };
        img.onerror = function () { mapLoadingVersion = -1; };
        img.src = src + '.png?v=' + version;
      } else if (!r.data.available) {
        mapImg = null; mapImgVersion = -1;
      }
      renderMapAny();
    });
  }

  function zoomMap(factor, px, py) {
    var size = mapCanvasSize();
    if (px === undefined) { px = size.w / 2; py = size.h / 2; }
    var next = Math.max(MAP_MIN_SCALE, Math.min(MAP_MAX_SCALE, mapView.scale * factor));
    var wx = mapView.cx + (px - size.w / 2) / mapView.scale;       // จุดใต้นิ้ว/เมาส์ต้องอยู่ที่เดิมหลังซูม
    var wy = mapView.cy - (py - size.h / 2) / mapView.scale;
    mapView.scale = next;
    mapView.cx = wx - (px - size.w / 2) / next;
    mapView.cy = wy + (py - size.h / 2) / next;
    mapView.auto = false;
    drawMap();
  }

  function panMap(dx, dy) {
    mapView.cx -= dx / mapView.scale;
    mapView.cy += dy / mapView.scale;
    mapView.auto = false;
    drawMap();
  }

  function wireMapCanvas(cv) {
    function local(ev) {
      var rect = cv.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    }
    function spread() {
      var ids = Object.keys(mapPointers);
      var a = mapPointers[ids[0]], b = mapPointers[ids[1]];
      return { d: Math.hypot(a.x - b.x, a.y - b.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }
    cv.addEventListener('pointerdown', function (ev) {
      mapPointers[ev.pointerId] = local(ev);
      if (cv.setPointerCapture) { try { cv.setPointerCapture(ev.pointerId); } catch (e) { /* ไม่เป็นไร */ } }
      poseDown(cv, ev.pointerId, mapPointers[ev.pointerId]);
      wpDown(cv, ev.pointerId, mapPointers[ev.pointerId]);
    });
    cv.addEventListener('pointermove', function (ev) {
      if (!mapPointers[ev.pointerId]) { return; }
      var now = local(ev);
      var ids = Object.keys(mapPointers);
      if (poseMove(cv, ev.pointerId, now, ids.length) || wpMove(cv, ev.pointerId, now, ids.length)) {   // กำลังวาง/ชี้ทิศ/ย้ายจุด ไม่เลื่อนแผนที่
        mapPointers[ev.pointerId] = now;
        return;
      }
      if (ids.length === 1) {
        panMap(now.x - mapPointers[ev.pointerId].x, now.y - mapPointers[ev.pointerId].y);
        mapPointers[ev.pointerId] = now;
      } else if (ids.length === 2) {
        var before = spread();
        mapPointers[ev.pointerId] = now;
        var after = spread();
        if (before.d > 0) { zoomMap(after.d / before.d, after.x, after.y); }
        panMap(after.x - before.x, after.y - before.y);
      }
    });
    function release(ev) {
      delete mapPointers[ev.pointerId];
      poseUp(ev.pointerId, ev.type === 'pointercancel');
      wpUp(ev.pointerId, ev.type === 'pointercancel');
    }
    cv.addEventListener('pointerup', release);
    cv.addEventListener('pointercancel', release);
    cv.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      var p = local(ev);
      zoomMap(ev.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y);
    });
    cv.addEventListener('keydown', function (ev) {
      var k = ev.key, step = 40;
      if (poseKey(cv, k) || wpKey(cv, k)) { ev.preventDefault(); return; }
      if (k === 'ArrowLeft') { panMap(step, 0); }
      else if (k === 'ArrowRight') { panMap(-step, 0); }
      else if (k === 'ArrowUp') { panMap(0, step); }
      else if (k === 'ArrowDown') { panMap(0, -step); }
      else if (k === '+' || k === '=') { zoomMap(MAP_ZOOM_STEP); }
      else if (k === '-' || k === '_') { zoomMap(1 / MAP_ZOOM_STEP); }
      else if (k === '0') { mapView.auto = true; drawMap(); }
      else { return; }
      ev.preventDefault();
    });
  }

  wireMapCanvas($('map-canvas'));
  wireMapCanvas($('nav-map-canvas'));
  wireMapCanvas($('wp-map-canvas'));
  ['map', 'nav', 'wp'].forEach(function (p) {                    // ปุ่มซูม/พอดี/หุ่น ของทั้งสองหน้า
    $(p + '-zoom-in').addEventListener('click', function () { zoomMap(MAP_ZOOM_STEP); });
    $(p + '-zoom-out').addEventListener('click', function () { zoomMap(1 / MAP_ZOOM_STEP); });
    $(p + '-fit').addEventListener('click', function () { mapView.auto = true; drawMap(); });
    $(p + '-center').addEventListener('click', function () {
      var pose = lastStatus && lastStatus.pose;
      if (!pose) { return; }
      mapView.cx = pose.x; mapView.cy = pose.y; mapView.auto = false;
      drawMap();
    });
  });
  $('map-start').addEventListener('click', function () { requestMode('MAPPING'); });
  $('map-login').addEventListener('click', openPin);

