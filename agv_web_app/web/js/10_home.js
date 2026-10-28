  // ── Home: ความพร้อมของระบบ (รายการเต็ม + ชิปสรุป) ──────────────────
  function renderChecks(items) {
    var key = JSON.stringify([items, I18N.getLang()]);
    if (key === lastChecksKey) { return; }     // ไม่เปลี่ยน → ไม่ต้องสร้าง DOM ใหม่
    lastChecksKey = key;

    var ul = $('checks');
    while (ul.firstChild) { ul.removeChild(ul.firstChild); }
    items.forEach(function (it) {
      var li = document.createElement('li');
      li.className = it.level;

      var mark = document.createElement('span');
      mark.className = 'mark';
      mark.textContent = MARK[it.level] || '•';

      var body = document.createElement('span');
      body.appendChild(document.createTextNode(tm(it.text)));
      if (it.hint) {
        var sub = document.createElement('span');
        sub.className = 'sub';
        sub.textContent = tm(it.hint);
        body.appendChild(sub);
      }

      li.appendChild(mark);
      li.appendChild(body);
      ul.appendChild(li);
    });
  }

  function openReadyDetails() {
    var d = $('ready-details');
    d.open = true;
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    d.scrollIntoView({ behavior: calm ? 'auto' : 'smooth', block: 'center' });
  }

  function renderChips(items, quality) {
    var key = JSON.stringify([items, quality, I18N.getLang()]);
    if (key === lastChipsKey) { return; }
    lastChipsKey = key;

    var ul = $('chips');
    while (ul.firstChild) { ul.removeChild(ul.firstChild); }
    items.forEach(function (it) {
      var label = t('chip.' + it.key);
      if (it.key === 'localization' && it.level !== 'bad' && quality && quality !== 'unknown') {
        label += ' · ' + t('quality.' + quality);
      }
      var li = document.createElement('li');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip ' + it.level;
      var mark = document.createElement('span');
      mark.className = 'chip-mark';
      mark.textContent = MARK[it.level] || '•';
      btn.appendChild(mark);
      btn.appendChild(document.createTextNode(label));
      btn.addEventListener('click', openReadyDetails);
      li.appendChild(btn);
      ul.appendChild(li);
    });
  }

  function renderHome(r, localization, activity) {
    if (!r) { return; }
    var items = r.items || [];
    // พักเครื่อง = แอปปิดระบบไว้เองโดยตั้งใจ → ชิปเป็นสีกลาง ไม่ใช่ ✖ แดงที่ดูเหมือนมีอะไรเสีย
    var standby = !!(activity && activity.state === 'standby');
    renderChips(standby ? items.map(function (it) {
      return { key: it.key, level: 'off', text: it.text, hint: it.hint };
    }) : items, standby ? null : (localization && localization.quality));
    renderChecks(items);
  }

  // ── การ์ดสถานะหุ่น ────────────────────────────────────────────────
  var lastActKey = '';                                       // ค่าที่วาดล่าสุด (ไม่เปลี่ยน → ไม่เขียน DOM ซ้ำ : การ์ดมี aria-live จะไม่อ่านซ้ำทุก 0.2 วินาที)

  function renderActivity(a) {
    if (!a) { return; }
    var key = JSON.stringify([a, I18N.getLang()]);
    if (key === lastActKey) { return; }
    lastActKey = key;
    $('activity-card').className = 'card status ' + a.level;
    $('act-icon').textContent = MARK[a.level] || '○';
    $('act-text').textContent = tm(a.text);
    $('act-detail').textContent = tm(a.detail);
  }

  // ค่าปัจจุบันที่หัวกราฟ (stat tile + trend ตามแนวทาง dataviz)
  function renderNow(s) {
    $('now-v').textContent = s.speed ? fmt(s.speed.v, 2) : '–';
    $('now-w').textContent = s.speed ? fmt(s.speed.w, 2) : '–';
  }

