  var I18N = window.AgvI18n;
  var t = I18N.t;
  var tm = I18N.tm;
  var $ = function (id) { return document.getElementById(id); };

  var STALE_MS = 2000;            // ไม่ได้ข้อความนานกว่านี้ถือว่าเชื่อมต่อหลุด
  var SESSION_POLL_MS = 15000;    // ถามสถานะเซสชันผู้ดูแล (ไม่ต่ออายุ — ดู AuthManager.validate)
  var lastMsg = 0;
  var socket = null;
  var retryMs = 1000;
  var lastChecksKey = '';
  var lastChipsKey = '';
  var phase = 'connecting';       // connecting | live | down
  var connected = false;
  var admin = false;              // เป็นผู้ดูแลอยู่หรือไม่ (ตามที่ backend ยืนยัน)
  var authEnabled = true;         // backend ตั้ง admin_pin แล้วหรือยัง
  var lastMode = null;
  var lastStatus = null;
  var toastTimer = null;
  var pinTimer = null;

  // ── router แบบ hash ───────────────────────────────────────────────
  var ROUTES = {
    '#/home': 'home',
    '#/nav': 'nav',
    '#/mapping': 'mapping',
    '#/waypoint': 'waypoint',
    '#/settings': 'settings'
  };
  var TAB_OF = {
    home: 'tab-home',
    nav: 'tab-nav',
    mapping: 'tab-mapping',
    waypoint: 'tab-waypoint',
    settings: 'tab-settings'
  };
  var TABS = ['tab-home', 'tab-nav', 'tab-mapping', 'tab-waypoint', 'tab-settings'];

  function route() {
    var view = ROUTES[location.hash] || 'home';
    var sections = document.querySelectorAll('[data-view]');
    for (var i = 0; i < sections.length; i++) {
      sections[i].hidden = (sections[i].getAttribute('data-view') !== view);
    }
    TABS.forEach(function (id) {
      var on = (id === TAB_OF[view]);
      $(id).classList.toggle('active', on);
      if (on) { $(id).setAttribute('aria-current', 'page'); }
      else { $(id).removeAttribute('aria-current'); }
    });
    window.scrollTo(0, 0);
    wp.drag = null;
    if (view === 'home') { pollHistory(); pollStats(); }
    if (view === 'mapping') { slamRunKey = ''; renderMapPage(); pollMap(); loadSaved(); }
    if (view === 'nav') { renderLibrary(true); loadLibrary(); loadNavParams(); renderNavMap(); pollMap(); pollGoal(); pollStart(); }
    else { poseCancel(); }                                                         // ออกจากหน้า Navigation = เลิกวางตำแหน่ง                    // เข้าหน้า = วาดทันทีจากของเดิม แล้วดึงใหม่
    if (view === 'waypoint') { loadRoutes(); renderWpMap(); pollMap(); pollRun(); renderRun(); }
    syncDrive();                                                                   // ออกจากหน้า Mapping = ตัดการขับทันที       // เข้าหน้าทุกครั้ง = ดึงค่า SLAM ใหม่ 1 ครั้ง
  }
  window.addEventListener('hashchange', route);

  // ── ตัวช่วยทั่วไป ─────────────────────────────────────────────────
  var MARK = { ok: '✔', warn: '▲', bad: '✖', off: '○' };    // ทุกสถานะมีสัญลักษณ์ ไม่พึ่งสีอย่างเดียว

  function setBadge(el, text, cls) {
    el.textContent = text;
    el.className = 'badge ' + cls;
  }

  function fmt(v, digits) {
    return (typeof v === 'number') ? v.toFixed(digits) : '–';
  }

  function toast(text) {
    var el = $('toast');
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 4500);
  }

  function openDialog(dlg) {
    if (typeof dlg.showModal === 'function') { dlg.showModal(); }
    else { dlg.setAttribute('open', ''); }
  }

  function closeDialog(dlg) {
    if (typeof dlg.close === 'function') { dlg.close(); }
    else { dlg.removeAttribute('open'); }
  }

  // ส่งคำขอไป backend (POST ต้องมีเฮดเดอร์ X-Requested-With — backend ใช้กัน CSRF)
  function api(path, body) {
    var opts = {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'agv-web', 'Content-Type': 'application/json' }
    };
    if (body !== undefined) { opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { status: res.status, data: data };
      });
    }).catch(function () {
      return { status: 0, data: { error: { code: 'net.error', params: {} } } };
    });
  }

  // opts: { warn: true → หน้าต่างแจ้งเตือนเด่น (alertdialog + แถบเหลือง) , items: [ข้อความ…] → รายการหัวข้อย่อย }
  function confirmDialog(title, message, okLabel, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = $('confirm-dialog');
      var done = false;
      $('confirm-title').textContent = title;
      $('confirm-msg').textContent = message;
      $('confirm-ok').textContent = okLabel || t('ui.confirm.ok');
      var list = $('confirm-list');
      while (list.firstChild) { list.removeChild(list.firstChild); }
      (opts.items || []).forEach(function (text) {
        var li = document.createElement('li');
        li.textContent = text;
        list.appendChild(li);
      });
      list.hidden = !(opts.items && opts.items.length);
      dlg.className = opts.warn ? 'dlg-warn' : '';
      dlg.setAttribute('role', opts.warn ? 'alertdialog' : 'dialog');

      function finish(value) {
        if (done) { return; }
        done = true;
        $('confirm-ok').onclick = null;
        $('confirm-cancel').onclick = null;
        dlg.onclose = null;
        closeDialog(dlg);
        // ล้างข้อความเก่า (กันค้างเป็นภาษาเดิมถ้าผู้ใช้สลับภาษาตอนปิดอยู่ แล้วเปิดครั้งหน้า)
        $('confirm-title').textContent = '';
        $('confirm-msg').textContent = '';
        $('confirm-ok').textContent = t('ui.confirm.ok');
        var items = $('confirm-list');
        while (items.firstChild) { items.removeChild(items.firstChild); }
        resolve(value);
      }
      $('confirm-ok').onclick = function () { finish(true); };
      $('confirm-cancel').onclick = function () { finish(false); };
      dlg.onclose = function () { finish(false); };      // กด Esc
      openDialog(dlg);
    });
  }

  // ── แถบบน: ป้ายโหมดปัจจุบัน (อยู่ทุกหน้า) cls = ok | warn | bad | off ────
  function setPill(cls, text, busy) {
    $('mode-pill').className = 'mode-pill ' + cls;
    $('pill-text').textContent = text;
    $('pill-spin').hidden = !busy;
  }

  function scrollToModeCard() {
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    $('mode-card').scrollIntoView({ behavior: calm ? 'auto' : 'smooth', block: 'center' });
  }

  // แตะป้ายโหมด → ไปที่การ์ดโหมดระบบ (ถ้าอยู่หน้าอื่นให้กลับ Home ก่อน)
  $('mode-pill').addEventListener('click', function () {
    if ((ROUTES[location.hash] || 'home') !== 'home') {
      location.hash = '#/home';
      setTimeout(scrollToModeCard, 80);
    } else {
      scrollToModeCard();
    }
  });

