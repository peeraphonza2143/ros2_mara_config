// AgvChart — กราฟเส้น SVG เขียนเอง (ไม่พึ่ง CDN เพราะ hotspot ของหุ่นไม่มีเน็ต)
//
// ตามแนวทาง dataviz: เส้น 2px, จุดปลายเส้น r=4 พร้อมวงแหวนสีพื้น 2px, gridline เป็นเส้นบางทึบ,
// ป้ายตัวเลขเฉพาะจุดปลาย (ไม่ใส่ทุกจุด), ตัวอักษรใช้สีข้อความไม่ใช้สีของ series,
// เส้นขาดเมื่อไม่มีข้อมูล (null), crosshair + tooltip ใช้ได้ทั้งเมาส์/สัมผัส/คีย์บอร์ด
// สี/สไตล์ทั้งหมดอยู่ใน style.css (class ch-*) เพื่อสลับสว่าง/มืดที่เดียว
//
// ส่วนคณิตศาสตร์ล้วน (niceTicks, valueRange, segments, nearestIndex ฯลฯ) ไม่แตะ DOM → ทดสอบได้ใน gjs
(function (root, doc) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var M = { l: 46, r: 62, t: 12, b: 26 };     // ขอบ: ซ้ายมีป้ายแกน Y, ขวาเผื่อป้ายค่าที่จุดปลาย (เช่น -0.35)
  var HEADROOM = 1.06;                        // เว้นที่ว่าง 6% เหนือ/ใต้ค่าสุดขีด ไม่ให้เส้นชนขอบกราฟ
  var HEIGHT = 124;
  var RECENT_S = 5;                           // จุดปลายต้องใหม่กว่านี้ถึงจะวาดจุด/ป้ายค่า

  // ── คณิตศาสตร์ล้วน ──────────────────────────────────────────────────
  function trimNum(v) {
    var s = String(Number(v.toFixed(6)));
    return (s === '-0') ? '0' : s;
  }

  // ช่วงค่าที่รวม 0 เสมอ (ความเร็วมีฐานที่ 0) และกว้างอย่างน้อย minSpan กันเส้นเรียบถูกขยายจนดูเหมือนผันผวน
  function valueRange(values, minSpan) {
    var lo = 0, hi = 0, any = false;
    values.forEach(function (v) {
      if (typeof v !== 'number') { return; }
      any = true;
      if (v < lo) { lo = v; }
      if (v > hi) { hi = v; }
    });
    if (hi - lo < minSpan) {
      if (lo === 0) { hi = minSpan; }
      else if (hi === 0) { lo = -minSpan; }
      else { var mid = (lo + hi) / 2; lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
    }
    return { lo: lo, hi: hi, any: any };
  }

  // ขีดแกนเป็นตัวเลขกลม (1 / 2 / 5 × 10^n) และปัดช่วงออกไปให้ลงที่ขีดพอดี
  function niceTicks(lo, hi, maxTicks) {
    var span = hi - lo;
    if (!(span > 0)) { hi = lo + 1; span = 1; }
    var rough = span / Math.max(1, maxTicks - 1);
    var pow = Math.pow(10, Math.floor(Math.log10(rough)));
    var f = rough / pow;
    var step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
    var min = Math.floor(lo / step + 1e-9) * step;
    var max = Math.ceil(hi / step - 1e-9) * step;
    var ticks = [];
    for (var v = min; v <= max + step / 2; v += step) { ticks.push(Number(v.toFixed(6))); }
    return { ticks: ticks, min: min, max: max, step: step };
  }

  // แบ่งดัชนีเป็นช่วงที่ต่อเนื่องกันและไม่ใช่ null (เส้นขาดตรงช่องว่าง)
  function segments(values) {
    var out = [], cur = [];
    values.forEach(function (v, i) {
      if (typeof v === 'number') { cur.push(i); }
      else if (cur.length) { out.push(cur); cur = []; }
    });
    if (cur.length) { out.push(cur); }
    return out;
  }

  function buildPath(points) {
    return points.map(function (p, i) {
      return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1);
    }).join('');
  }

  function nearestIndex(xs, x) {
    var best = -1, bestD = Infinity;
    for (var i = 0; i < xs.length; i++) {
      var d = Math.abs(xs[i] - x);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  // ── วาดลง SVG ───────────────────────────────────────────────────────
  function el(tag, attrs, text) {
    var e = doc.createElementNS(NS, tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) { e.setAttribute(k, attrs[k]); }
    }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }

  /**
   * opts: svg, width(px), t[] (วินาที ≤ 0 เทียบ "ตอนนี้"), values[] (null = ไม่มีข้อมูล),
   *       window(วินาที), title, unit, decimals, minSpan, xTicks[{t,label}], emptyText
   * คืนตัวควบคุม: { count, indexFromClientX(), show(i), hide(), lastIndex(), info(i) }
   */
  function render(opts) {
    var svg = opts.svg;
    var win = opts.window;
    var W = Math.max(240, Math.round(opts.width || 320));
    var pw = W - M.l - M.r;
    var ph = HEIGHT - M.t - M.b;
    var dec = (opts.decimals === undefined) ? 2 : opts.decimals;
    var t = opts.t, values = opts.values;

    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + HEIGHT);
    svg.setAttribute('role', 'img');
    svg.setAttribute('tabindex', '0');

    var range = valueRange(values, opts.minSpan || 0.2);
    var nt = niceTicks(range.lo < 0 ? range.lo * HEADROOM : 0, range.hi > 0 ? range.hi * HEADROOM : 0, 4);
    var X = function (s) { return M.l + (s + win) / win * pw; };
    var Y = function (v) { return M.t + (nt.max - v) / (nt.max - nt.min) * ph; };

    // gridline + ป้ายแกน Y (เส้นศูนย์เป็นแกนอ้างอิงเข้มกว่าเล็กน้อย)
    nt.ticks.forEach(function (tick) {
      var y = Y(tick);
      svg.appendChild(el('line', { 'class': tick === 0 ? 'ch-axis' : 'ch-grid',
        x1: M.l, x2: M.l + pw, y1: y.toFixed(1), y2: y.toFixed(1) }));
      svg.appendChild(el('text', { 'class': 'ch-tick', x: M.l - 7, y: (y + 4.5).toFixed(1),
        'text-anchor': 'end' }, trimNum(tick)));
    });

    // ป้ายแกน X (ขอบซ้าย/ขวาชิดขอบ ไม่ให้ล้นกรอบ)
    (opts.xTicks || []).forEach(function (xt, i, all) {
      var anchor = (i === 0) ? 'start' : (i === all.length - 1 ? 'end' : 'middle');
      svg.appendChild(el('text', { 'class': 'ch-tick', x: X(xt.t).toFixed(1), y: HEIGHT - 7,
        'text-anchor': anchor }, xt.label));
    });

    var last = -1;
    for (var i = values.length - 1; i >= 0; i--) {
      if (typeof values[i] === 'number') { last = i; break; }
    }

    if (!range.any) {
      svg.appendChild(el('text', { 'class': 'ch-empty', x: (M.l + pw / 2).toFixed(1),
        y: (M.t + ph / 2).toFixed(1), 'text-anchor': 'middle' }, opts.emptyText || ''));
    }

    // เส้น: ขาดตรงช่องว่าง ; ช่วงที่มีจุดเดียวแสดงเป็นจุดเล็ก
    segments(values).forEach(function (seg) {
      var pts = seg.map(function (idx) { return [X(t[idx]), Y(values[idx])]; });
      if (pts.length > 1) { svg.appendChild(el('path', { 'class': 'ch-line', d: buildPath(pts) })); }
      else { svg.appendChild(el('circle', { 'class': 'ch-pt', cx: pts[0][0].toFixed(1),
        cy: pts[0][1].toFixed(1), r: 2.5 })); }
    });

    // จุดปลาย + ป้ายค่า (เฉพาะเมื่อข้อมูลล่าสุดยังใหม่ — ถ้าเส้นขาดตอนท้าย ไม่มีค่า "ตอนนี้")
    var recent = last >= 0 && t[last] >= -RECENT_S;
    if (recent) {
      var ex = X(t[last]), ey = Y(values[last]);
      svg.appendChild(el('circle', { 'class': 'ch-dot', cx: ex.toFixed(1), cy: ey.toFixed(1), r: 4 }));
      svg.appendChild(el('text', { 'class': 'ch-endlabel', x: (ex + 9).toFixed(1),
        y: (ey + 4).toFixed(1) }, values[last].toFixed(dec)));
    }

    // crosshair (ซ่อนไว้ก่อน)
    var cross = el('line', { 'class': 'ch-cross', x1: 0, x2: 0, y1: M.t, y2: M.t + ph, visibility: 'hidden' });
    var cdot = el('circle', { 'class': 'ch-dot', cx: 0, cy: 0, r: 4, visibility: 'hidden' });
    svg.appendChild(cross);
    svg.appendChild(cdot);

    // ข้อความสรุปสำหรับโปรแกรมอ่านหน้าจอ: แปลผ่าน opts.tr (ฟังก์ชันแปลภาษาจาก AgvI18n.t)
    var tr = opts.tr;
    var summary = last >= 0
      ? tr('chart.aria.latest', { title: opts.title, value: values[last].toFixed(dec), unit: opts.unit })
      : tr('chart.aria.none', { title: opts.title });
    if (range.any) {
      var nums = values.filter(function (v) { return typeof v === 'number'; });
      summary += tr('chart.aria.range', { min: Math.min.apply(null, nums).toFixed(dec),
        max: Math.max.apply(null, nums).toFixed(dec), seconds: Math.round(win) });
    }
    svg.setAttribute('aria-label', summary);

    function info(i) {
      var v = values[i];
      return { index: i, t: t[i], v: (typeof v === 'number') ? v : null,
               x: X(t[i]), y: (typeof v === 'number') ? Y(v) : null };
    }
    return {
      count: values.length,
      width: W,
      height: HEIGHT,
      lastIndex: function () { return values.length - 1; },
      info: info,
      indexFromClientX: function (clientX) {
        var rect = svg.getBoundingClientRect();
        var vx = (clientX - rect.left) / (rect.width || W) * W;
        return nearestIndex(t.map(X), vx);
      },
      show: function (i) {
        if (i < 0 || i >= values.length) { return null; }
        var p = info(i);
        cross.setAttribute('x1', p.x.toFixed(1));
        cross.setAttribute('x2', p.x.toFixed(1));
        cross.setAttribute('visibility', 'visible');
        if (p.y !== null) {
          cdot.setAttribute('cx', p.x.toFixed(1));
          cdot.setAttribute('cy', p.y.toFixed(1));
          cdot.setAttribute('visibility', 'visible');
        } else {
          cdot.setAttribute('visibility', 'hidden');
        }
        return p;
      },
      hide: function () {
        cross.setAttribute('visibility', 'hidden');
        cdot.setAttribute('visibility', 'hidden');
      }
    };
  }

  // ── แท่งรายวัน (กราฟแท่งชุดเดียว ฐานที่ 0 ชิด baseline หัวแท่งมน 4px) ──────────────
  var BM = { l: 46, r: 12, t: 24, b: 26 };    // เผื่อด้านบนไว้ให้ป้ายค่าของวันล่าสุดและหน่วยแกน

  function barPath(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return 'M' + x.toFixed(1) + ' ' + (y + h).toFixed(1) + 'V' + (y + r).toFixed(1) +
      'Q' + x.toFixed(1) + ' ' + y.toFixed(1) + ' ' + (x + r).toFixed(1) + ' ' + y.toFixed(1) +
      'H' + (x + w - r).toFixed(1) +
      'Q' + (x + w).toFixed(1) + ' ' + y.toFixed(1) + ' ' + (x + w).toFixed(1) + ' ' + (y + r).toFixed(1) +
      'V' + (y + h).toFixed(1) + 'Z';
  }

  // opts: svg, width, labels[] (ป้ายแกน X ต่อแท่ง), values[] (ตัวเลข ≥ 0), unit, decimals, minSpan,
  //       endLabel (ป้ายค่าเหนือแท่งสุดท้าย), summary (aria-label), emptyText
  // คืนตัวควบคุมเหมือนกราฟเส้น: { count, width, height, lastIndex, info, indexFromClientX, show, hide }
  function renderBars(opts) {
    var svg = opts.svg;
    var W = Math.max(240, Math.round(opts.width || 320));
    var pw = W - BM.l - BM.r;
    var ph = HEIGHT - BM.t - BM.b;
    var values = opts.values;
    var n = values.length;
    var slot = pw / n;
    var bw = Math.min(slot * 0.6, 34);

    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + HEIGHT);
    svg.setAttribute('role', 'img');
    svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-label', opts.summary || '');

    var range = valueRange(values, opts.minSpan || 1);
    var nt = niceTicks(0, range.hi * HEADROOM, 4);
    var Y = function (v) { return BM.t + (nt.max - v) / (nt.max - nt.min) * ph; };
    var cx = function (i) { return BM.l + slot * (i + 0.5); };

    if (opts.unit) {
      svg.appendChild(el('text', { 'class': 'ch-tick', x: BM.l - 7, y: 11, 'text-anchor': 'end' }, opts.unit));
    }
    nt.ticks.forEach(function (tick) {
      var y = Y(tick);
      svg.appendChild(el('line', { 'class': tick === 0 ? 'ch-axis' : 'ch-grid',
        x1: BM.l, x2: BM.l + pw, y1: y.toFixed(1), y2: y.toFixed(1) }));
      svg.appendChild(el('text', { 'class': 'ch-tick', x: BM.l - 7, y: (y + 4.5).toFixed(1),
        'text-anchor': 'end' }, trimNum(tick)));
    });

    var hover = el('rect', { 'class': 'ch-hover', x: 0, y: BM.t, width: slot.toFixed(1), height: ph,
      visibility: 'hidden' });
    svg.appendChild(hover);

    values.forEach(function (v, i) {
      svg.appendChild(el('text', { 'class': 'ch-tick', x: cx(i).toFixed(1), y: HEIGHT - 7,
        'text-anchor': 'middle' }, opts.labels[i]));
      if (v > 0) {
        var top = Y(v);
        svg.appendChild(el('path', { 'class': 'ch-bar', d: barPath(cx(i) - bw / 2, top, bw, Y(0) - top, 4) }));
      }
    });

    var peak = Math.max.apply(null, values);    // (range.hi ถูกดันขึ้นเป็น minSpan เสมอ จึงใช้ตัดสินว่า "ว่าง" ไม่ได้)
    if (!(peak > 0)) {
      svg.appendChild(el('text', { 'class': 'ch-empty', x: (BM.l + pw / 2).toFixed(1),
        y: (BM.t + ph / 2).toFixed(1), 'text-anchor': 'middle' }, opts.emptyText || ''));
    } else if (opts.endLabel && values[n - 1] > 0) {
      svg.appendChild(el('text', { 'class': 'ch-endlabel', x: (BM.l + pw).toFixed(1),
        y: (Y(values[n - 1]) - 6).toFixed(1), 'text-anchor': 'end' }, opts.endLabel));
    }

    function info(i) {
      return { index: i, x: cx(i), y: Y(values[i]), v: values[i] };
    }
    return {
      count: n,
      width: W,
      height: HEIGHT,
      lastIndex: function () { return n - 1; },
      info: info,
      indexFromClientX: function (clientX) {
        var rect = svg.getBoundingClientRect();
        var vx = (clientX - rect.left) / (rect.width || W) * W;
        return Math.max(0, Math.min(n - 1, Math.floor((vx - BM.l) / slot)));
      },
      show: function (i) {
        if (i < 0 || i >= n) { return null; }
        hover.setAttribute('x', (BM.l + slot * i).toFixed(1));
        hover.setAttribute('visibility', 'visible');
        return info(i);
      },
      hide: function () { hover.setAttribute('visibility', 'hidden'); }
    };
  }

  root.AgvChart = {
    render: render, renderBars: renderBars, barPath: barPath, niceTicks: niceTicks, valueRange: valueRange, segments: segments,
    buildPath: buildPath, nearestIndex: nearestIndex, trimNum: trimNum, HEIGHT: HEIGHT
  };
})(window, document);
