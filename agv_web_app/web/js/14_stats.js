  // ── สถิติ: ระยะทาง / เวลาทำงาน / ไทม์ไลน์โหมด (ข้อมูลจาก /api/stats ทุก 10 วินาที) ──
  var STATS_POLL_MS = 10000;
  var STATS_MODES = ['NAVIGATION', 'MAPPING', 'MANUAL'];
  var MAX_SESSION_ROWS = 10;
  var lastStats = null;

  function fmtDistance(m) {
    return m < 1000 ? t('stats.unit.m', { n: m < 100 ? m.toFixed(1) : String(Math.round(m)) })
                    : t('stats.unit.km', { n: (m / 1000).toFixed(2) });
  }

  function fmtDuration(sec) {
    var s = Math.max(0, Math.round(sec));
    if (s < 60) { return t('stats.dur.s', { s: s }); }
    var m = Math.floor(s / 60);
    if (m < 60) { return t('stats.dur.m', { m: m }); }
    return t('stats.dur.hm', { h: Math.floor(m / 60), m: m % 60 });
  }

  function fmtClock(epoch) {
    return new Date(epoch * 1000).toLocaleString(t('time.locale'), {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false
    });
  }

  function sessionLabel(seg) {
    return t('stats.tl.seg', {
      mode: t('mode.name.' + seg.mode), start: fmtClock(seg.start),
      dur: fmtDuration(seg.end - seg.start) + (seg.open ? ' (' + t('stats.tl.running') + ')' : '')
    });
  }

  function renderStats(data) {
    lastStats = data;
    $('st-dist').textContent = fmtDistance(data.today.distance_m);
    $('st-dist-total').textContent = t('stats.total', { value: fmtDistance(data.total.distance_m) });
    $('st-active').textContent = fmtDuration(data.today.active_s);
    $('st-active-total').textContent = t('stats.total', { value: fmtDuration(data.total.active_s) });
    var roundsKnown = (data.rounds !== null && data.rounds !== undefined);
    $('st-rounds').textContent = roundsKnown ? String(data.rounds) : '–';
    $('st-rounds-total').textContent = (roundsKnown && typeof data.total.rounds === 'number')
      ? t('stats.total', { value: String(data.total.rounds) }) : '';

    STATS_MODES.forEach(function (m) { $('lg-' + m).textContent = t('mode.name.' + m); });

    var track = $('tl-track');
    while (track.firstChild) { track.removeChild(track.firstChild); }
    var from = data.now - data.window;
    data.timeline.forEach(function (seg) {
      var el = document.createElement('span');
      var width = Math.max(0.8, (seg.end - seg.start) / data.window * 100);    // สั้นมากก็ยังต้องเห็น
      var left = Math.min(Math.max(0, (seg.start - from) / data.window * 100), 100 - width);
      el.className = 'tl-seg m-' + seg.mode;
      el.style.left = left.toFixed(2) + '%';
      el.style.width = width.toFixed(2) + '%';
      el.setAttribute('title', sessionLabel(seg));
      track.appendChild(el);
    });
    track.setAttribute('aria-label', data.timeline.length
      ? t('stats.tl.aria', { n: data.timeline.length }) : t('stats.tl.aria.none'));

    var axis = $('tl-axis');
    while (axis.firstChild) { axis.removeChild(axis.firstChild); }
    [t('stats.axis.h', { n: 24 }), t('stats.axis.h', { n: 12 }), t('chart.now')].forEach(function (label) {
      var el = document.createElement('span');
      el.textContent = label;
      axis.appendChild(el);
    });

    var body = $('stats-body');
    while (body.firstChild) { body.removeChild(body.firstChild); }
    var rows = data.timeline.slice().reverse().slice(0, MAX_SESSION_ROWS);     // ใหม่สุดอยู่บน
    if (!rows.length) {
      var tr0 = document.createElement('tr');
      var td0 = document.createElement('td');
      td0.setAttribute('colspan', '3');
      td0.textContent = t('stats.empty');
      tr0.appendChild(td0);
      body.appendChild(tr0);
    }
    rows.forEach(function (seg) {
      var tr = document.createElement('tr');
      [t('mode.name.' + seg.mode), fmtClock(seg.start),
       fmtDuration(seg.end - seg.start) + (seg.open ? ' (' + t('stats.tl.running') + ')' : '')
      ].forEach(function (text) {
        var td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });

    if (dayTipIndex === null) { drawDays(data); }          // กำลังดูค่าอยู่ → ไม่วาดทับให้แท่งไม่ขยับหนี

    var note = $('stats-note');
    var persist = data.persist || {};
    var key = persist.ok === false ? 'stats.note.unsaved' : (persist.note === 'corrupt' ? 'stats.note.corrupt' : null);
    note.hidden = (key === null);
    note.textContent = key === null ? '' : t(key);
  }

  // ── กราฟแท่ง 7 วัน (สลับระยะทาง / เวลาทำงาน ทีละกราฟ ไม่ใช้สองแกน) ──
  var daysMetric = 'dist';
  var dayTipIndex = null;         // ดัชนีแท่งที่ tooltip แสดงอยู่ (null = ซ่อน) ; ระหว่างดูค่าไม่วาดทับ
  var dayCtl = null;

  function dayDate(iso) { return new Date(iso + 'T12:00:00'); }     // เที่ยงวัน → ไม่ถูกเลื่อนวันเพราะ time zone

  function dayAxisLabel(iso, isToday) {
    return isToday ? t('stats.today') : dayDate(iso).toLocaleDateString(t('time.locale'), { weekday: 'short' });
  }

  function dayFullLabel(iso) {
    return dayDate(iso).toLocaleDateString(t('time.locale'), { weekday: 'short', day: 'numeric', month: 'short' });
  }

  // คืนค่าสำหรับวาด: หน่วยแกนที่อ่านง่ายที่สุดตามค่าสูงสุด + ฟังก์ชันจัดรูปค่าเต็ม
  function dayMetricSpec(days) {
    if (daysMetric === 'dist') {
      var maxM = Math.max.apply(null, days.map(function (d) { return d.distance_m; }));
      var km = maxM >= 1000;
      return { name: t('ui.stats.metric.dist'), pick: function (d) { return d.distance_m; }, fmt: fmtDistance,
               scale: km ? 1 / 1000 : 1, unit: km ? 'km' : 'm', decimals: km ? 2 : 0, minSpan: km ? 0.5 : 10 };
    }
    var maxS = Math.max.apply(null, days.map(function (d) { return d.active_s; }));
    var hours = maxS >= 3600;
    return { name: t('ui.stats.metric.active'), pick: function (d) { return d.active_s; }, fmt: fmtDuration,
             scale: hours ? 1 / 3600 : 1 / 60, unit: t(hours ? 'stats.axisunit.h' : 'stats.axisunit.min'),
             decimals: 1, minSpan: hours ? 0.5 : 5 };
  }

  function drawDays(data) {
    var days = data.days || [];
    if (!days.length) { return; }
    var spec = dayMetricSpec(days);
    var values = days.map(function (d) { return spec.pick(d) * spec.scale; });
    var raw = days.map(spec.pick);
    var last = days.length - 1;
    var peak = raw.indexOf(Math.max.apply(null, raw));
    var any = Math.max.apply(null, raw) > 0;
    dayCtl = window.AgvChart.renderBars({
      svg: $('chart-days'), width: $('days-box').clientWidth,
      labels: days.map(function (d, i) { return dayAxisLabel(d.date, i === last); }),
      values: values, unit: spec.unit, decimals: spec.decimals, minSpan: spec.minSpan,
      endLabel: spec.fmt(raw[last]), emptyText: t('stats.days.empty'),
      summary: any ? t('stats.days.aria', { metric: spec.name, today: spec.fmt(raw[last]),
                                            peak: dayFullLabel(days[peak].date) + ' ' + spec.fmt(raw[peak]) })
                   : t('stats.days.aria.none', { metric: spec.name })
    });
    $('metric-dist').setAttribute('aria-pressed', daysMetric === 'dist' ? 'true' : 'false');
    $('metric-active').setAttribute('aria-pressed', daysMetric === 'active' ? 'true' : 'false');

    var body = $('days-body');
    while (body.firstChild) { body.removeChild(body.firstChild); }
    days.slice().reverse().forEach(function (d, i) {      // ใหม่สุดอยู่บน
      var tr = document.createElement('tr');
      [i === 0 ? t('stats.today') : dayFullLabel(d.date), fmtDistance(d.distance_m), fmtDuration(d.active_s)]
        .forEach(function (text) {
          var td = document.createElement('td');
          td.textContent = text;
          tr.appendChild(td);
        });
      body.appendChild(tr);
    });
  }

  function showDayTip(index) {
    var data = lastStats;
    if (!dayCtl || !data || index < 0 || index >= dayCtl.count) { hideDayTip(); return; }
    var spec = dayMetricSpec(data.days);
    var p = dayCtl.show(index);
    dayTipIndex = index;
    var tip = $('days-tip');
    while (tip.firstChild) { tip.removeChild(tip.firstChild); }
    var when = document.createElement('span');
    when.className = 'tip-when';
    when.textContent = index === dayCtl.count - 1 ? t('stats.today') + ' · ' + dayFullLabel(data.days[index].date)
                                                  : dayFullLabel(data.days[index].date);
    var row = document.createElement('span');
    row.className = 'tip-row';
    var val = document.createElement('strong');
    val.textContent = spec.fmt(spec.pick(data.days[index]));
    row.appendChild(val);
    tip.appendChild(when);
    tip.appendChild(row);
    tip.hidden = false;
    var half = 70;
    var w = $('days-box').clientWidth || dayCtl.width;
    tip.style.left = Math.max(half, Math.min(w - half, p.x)) + 'px';
    tip.style.top = Math.max(28, p.y - 10) + 'px';
  }

  function hideDayTip() {
    dayTipIndex = null;
    $('days-tip').hidden = true;
    if (dayCtl) { dayCtl.hide(); }
    if (lastStats) { drawDays(lastStats); }                // ข้อมูลที่เข้ามาระหว่างดูค่า วาดตอนนี้
  }

  (function wireDays() {
    var svg = $('chart-days');
    function at(ev) { if (dayCtl) { showDayTip(dayCtl.indexFromClientX(ev.clientX)); } }
    svg.addEventListener('pointermove', at);
    svg.addEventListener('pointerdown', at);
    svg.addEventListener('pointerleave', hideDayTip);
    svg.addEventListener('focus', function () { if (dayCtl) { showDayTip(dayCtl.lastIndex()); } });
    svg.addEventListener('blur', hideDayTip);
    svg.addEventListener('keydown', function (ev) {
      if (!dayCtl) { return; }
      var i = (dayTipIndex === null) ? dayCtl.lastIndex() : dayTipIndex;
      if (ev.key === 'ArrowLeft') { showDayTip(Math.max(0, i - 1)); ev.preventDefault(); }
      else if (ev.key === 'ArrowRight') { showDayTip(Math.min(dayCtl.count - 1, i + 1)); ev.preventDefault(); }
      else if (ev.key === 'Home') { showDayTip(0); ev.preventDefault(); }
      else if (ev.key === 'End') { showDayTip(dayCtl.lastIndex()); ev.preventDefault(); }
      else if (ev.key === 'Escape') { hideDayTip(); }
    });
    ['dist', 'active'].forEach(function (m) {
      $('metric-' + m).addEventListener('click', function () {
        daysMetric = m;
        dayTipIndex = null;
        $('days-tip').hidden = true;
        if (lastStats) { drawDays(lastStats); }
      });
    });
  })();

  // ── รีเซ็ตสถิติ (Admin เท่านั้น) ──
  $('stats-reset').addEventListener('click', function () {
    confirmDialog(t('stats.reset.title'), t('stats.reset.body'), t('stats.reset.ok')).then(function (ok) {
      if (!ok) { return; }
      api('/api/stats/reset', {}).then(function (r) {
        if (r.status === 200 && r.data && r.data.ok && r.data.stats) {
          renderStats(r.data.stats);
          toast(t('toast.stats_reset'));
        } else if (r.status === 401) {
          admin = false;
          renderAuth();
          toast(t('toast.session_expired'));
          openPin();
        } else {
          toast(tm(r.data && r.data.error) || t('toast.stats_reset_failed'));
        }
      });
    });
  });

  function pollStats() {
    if (!homeVisible()) { return; }
    api('/api/stats').then(function (r) {
      var ok = r.status === 200 && r.data && r.data.today && r.data.timeline;
      $('stats-card').classList.toggle('stale', !ok);      // โหลดไม่ได้ → คงตัวเลขเดิมไว้ จางลง
      if (ok) { renderStats(r.data); }
    });
  }

