  // ── Dashboard: กราฟความเร็ว (ประวัติเก็บที่ backend → ดึงมาวาดทุก 2 วินาที) ──
  var HISTORY_WINDOW = 120;
  var HISTORY_POLL_MS = 2000;
  var CHARTS = [
    { key: 'v', svg: 'chart-v', box: 'chart-v-box', tip: 'chart-v-tip',
      titleKey: 'ui.chart.fwd', unit: 'm/s', decimals: 2, minSpan: 0.2 },
    { key: 'w', svg: 'chart-w', box: 'chart-w-box', tip: 'chart-w-tip',
      titleKey: 'ui.chart.turn', unit: 'rad/s', decimals: 2, minSpan: 0.5 }
  ];
  var chartCtl = {};              // key → ตัวควบคุมกราฟ
  var tipIndex = {};              // key → ดัชนีที่ tooltip กำลังแสดง (null = ซ่อน)
  var lastHistory = null;
  var pendingHistory = null;      // ข้อมูลใหม่ที่รอวาด (ระหว่างผู้ใช้กำลังดูค่า เราไม่วาดทับให้กราฟไม่ขยับหนี)

  function ageLabel(seconds) {    // seconds ≤ 0
    var s = Math.round(-seconds);
    return s <= 1 ? t('chart.now') : t('chart.ago', { seconds: s });
  }

  function axisLabel(seconds) {   // ป้ายแกนเวลา: 0 → now , -120 → -2 min
    var s = Math.round(-seconds);
    if (s === 0) { return t('chart.now'); }
    return (s % 60 === 0) ? t('chart.axis.min', { n: s / 60 }) : t('chart.axis.sec', { n: s });
  }

  function inspecting() {
    return CHARTS.some(function (c) { return tipIndex[c.key] !== null && tipIndex[c.key] !== undefined; });
  }

  function setStale(on) {
    CHARTS.forEach(function (c) { $(c.box).classList.toggle('stale', on); });
  }

  function drawCharts(data) {
    lastHistory = data;
    pendingHistory = null;
    CHARTS.forEach(function (c) {
      chartCtl[c.key] = window.AgvChart.render({
        svg: $(c.svg), width: $(c.box).clientWidth,
        t: data.t, values: data[c.key], window: data.window,
        title: t(c.titleKey), unit: c.unit, decimals: c.decimals, minSpan: c.minSpan,
        xTicks: [{ t: -data.window, label: axisLabel(-data.window) },
                 { t: -data.window / 2, label: axisLabel(-data.window / 2) },
                 { t: 0, label: axisLabel(0) }],
        emptyText: t('chart.empty'), tr: t
      });
    });
    renderTable(data);
  }

  function showTip(c, index) {
    var ctl = chartCtl[c.key];
    var tip = $(c.tip);
    if (!ctl || index < 0 || index >= ctl.count) { hideTip(c); return; }
    var p = ctl.show(index);
    tipIndex[c.key] = index;

    while (tip.firstChild) { tip.removeChild(tip.firstChild); }
    var when = document.createElement('span');
    when.className = 'tip-when';
    when.textContent = ageLabel(p.t);
    var row = document.createElement('span');
    row.className = 'tip-row';
    var key = document.createElement('span');
    key.className = 'tip-key';
    var val = document.createElement('strong');
    val.textContent = (p.v === null) ? t('chart.nodata') : p.v.toFixed(c.decimals) + ' ' + c.unit;
    row.appendChild(key);
    row.appendChild(val);
    tip.appendChild(when);
    tip.appendChild(row);
    tip.hidden = false;

    var half = 70;                                  // ครึ่งความกว้างโดยประมาณของ tooltip
    var w = $(c.box).clientWidth || ctl.width;
    tip.style.left = Math.max(half, Math.min(w - half, p.x)) + 'px';
    tip.style.top = (p.y === null ? 30 : Math.max(28, p.y - 10)) + 'px';
  }

  function hideTip(c) {
    tipIndex[c.key] = null;
    $(c.tip).hidden = true;
    if (chartCtl[c.key]) { chartCtl[c.key].hide(); }
    if (pendingHistory && !inspecting()) { drawCharts(pendingHistory); }
  }

  CHARTS.forEach(function (c) {
    var svg = $(c.svg);
    function at(ev) {
      var ctl = chartCtl[c.key];
      if (ctl) { showTip(c, ctl.indexFromClientX(ev.clientX)); }
    }
    svg.addEventListener('pointermove', at);
    svg.addEventListener('pointerdown', at);
    svg.addEventListener('pointerleave', function () { hideTip(c); });
    svg.addEventListener('focus', function () {
      var ctl = chartCtl[c.key];
      if (ctl) { showTip(c, ctl.lastIndex()); }
    });
    svg.addEventListener('blur', function () { hideTip(c); });
    svg.addEventListener('keydown', function (ev) {
      var ctl = chartCtl[c.key];
      if (!ctl) { return; }
      var i = tipIndex[c.key];
      if (i === null || i === undefined) { i = ctl.lastIndex(); }
      if (ev.key === 'ArrowLeft') { showTip(c, Math.max(0, i - 1)); ev.preventDefault(); }
      else if (ev.key === 'ArrowRight') { showTip(c, Math.min(ctl.count - 1, i + 1)); ev.preventDefault(); }
      else if (ev.key === 'Home') { showTip(c, 0); ev.preventDefault(); }
      else if (ev.key === 'End') { showTip(c, ctl.lastIndex()); ev.preventDefault(); }
      else if (ev.key === 'Escape') { hideTip(c); }
    });
  });

  // ตารางเทียบเท่ากราฟ (ค่าทุกค่าเข้าถึงได้โดยไม่ต้องพึ่ง tooltip): ทุก 10 วินาที ใหม่สุดอยู่บน
  function renderTable(data) {
    var body = $('speed-body');
    while (body.firstChild) { body.removeChild(body.firstChild); }
    var nearest = function (target) {
      var best = -1, bestD = 2.5;
      for (var i = 0; i < data.t.length; i++) {
        var d = Math.abs(data.t[i] - target);
        if (d <= bestD) { bestD = d; best = i; }
      }
      return best;
    };
    for (var sec = 0; sec <= data.window; sec += 10) {
      var idx = nearest(-sec);
      var tr = document.createElement('tr');
      [sec === 0 ? t('chart.now') : t('chart.axis.sec', { n: sec }),
       idx >= 0 && data.v[idx] !== null ? data.v[idx].toFixed(2) : '–',
       idx >= 0 && data.w[idx] !== null ? data.w[idx].toFixed(2) : '–'].forEach(function (text) {
        var td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    }
  }

  function homeVisible() {
    return (ROUTES[location.hash] || 'home') === 'home' && !document.hidden;
  }

  function pollHistory() {
    if (!homeVisible()) { return; }
    api('/api/history?window=' + HISTORY_WINDOW).then(function (r) {
      if (r.status === 200 && r.data && r.data.t) {
        setStale(false);
        if (inspecting()) { pendingHistory = r.data; } else { drawCharts(r.data); }
      } else {
        setStale(true);                              // โหลดไม่ได้ → คงภาพเดิม จางลง ไม่กระพริบ/ไม่ล้าง
      }
    });
  }

