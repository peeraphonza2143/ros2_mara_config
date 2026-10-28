  // ── ผู้ดูแล: ปุ่มกุญแจ + PIN ──────────────────────────────────────
  function renderAuth() {
    $('lock').className = 'icon-btn' + (admin ? ' on' : '');
    $('lock').setAttribute('aria-label', t(admin ? 'ui.lock.out' : 'ui.lock.in'));
    $('lock-ico').setAttribute('href', admin ? '#i-unlock' : '#i-lock');
    $('stats-reset').hidden = !admin;                          // ปุ่มรีเซ็ตสถิติเห็นเฉพาะผู้ดูแล
    renderMode(lastMode);
    renderLibrary(false);                                      // ปุ่มจัดการแผนที่เห็นเฉพาะผู้ดูแล
    renderNavPose();
    wpRenderControls();                                        // ปุ่มแก้ Route เปิดเฉพาะผู้ดูแล
    renderRun();
  }

  function refreshSession() {
    return api('/api/session').then(function (r) {
      if (r.status !== 200) { return; }
      var was = admin;
      admin = !!r.data.admin;
      authEnabled = (r.data.auth_enabled !== false);
      if (was && !admin) { toast(t('toast.session_expired')); }
      renderAuth();
    });
  }

  function setPinBusy(busy) {
    $('pin-submit').disabled = busy;
    $('pin-input').disabled = busy;
  }

  function startLockCountdown(seconds) {
    var left = seconds;
    clearInterval(pinTimer);
    setPinBusy(true);
    function tick() {
      if (left <= 0) {
        clearInterval(pinTimer);
        $('pin-error').textContent = '';
        setPinBusy(false);
        return;
      }
      $('pin-error').textContent = t('auth.locked', { seconds: left });
      left -= 1;
    }
    tick();
    pinTimer = setInterval(tick, 1000);
  }

  function openPin() {
    clearInterval(pinTimer);
    $('pin-input').value = '';
    $('pin-error').textContent = '';
    setPinBusy(false);
    openDialog($('pin-dialog'));
    setTimeout(function () { $('pin-input').focus(); }, 50);
  }

  $('pin-dialog').addEventListener('close', function () {
    clearInterval(pinTimer);
    $('pin-input').value = '';
  });
  $('pin-cancel').addEventListener('click', function () { closeDialog($('pin-dialog')); });

  $('pin-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var pin = $('pin-input').value;
    if (!pin) { return; }
    setPinBusy(true);
    api('/api/login', { pin: pin }).then(function (r) {
      $('pin-input').value = '';
      if (r.status === 200) {
        closeDialog($('pin-dialog'));
        toast(t('toast.signed_in'));
        refreshSession();
      } else if (r.status === 429) {
        startLockCountdown(r.data.retry_after || 30);
      } else {
        setPinBusy(false);
        $('pin-error').textContent = tm(r.data.error) || t('pin.failed');
        $('pin-input').focus();
      }
    });
  });

  function onLockClick() {
    if (admin) {
      confirmDialog(t('logout.title'), t('logout.body'), t('logout.ok'))
        .then(function (ok) {
          if (!ok) { return; }
          api('/api/logout', {}).then(function () {
            admin = false;
            renderAuth();
            toast(t('toast.signed_out'));
          });
        });
    } else if (!authEnabled) {
      toast(t('auth.disabled'));
    } else {
      openPin();
    }
  }
  $('lock').addEventListener('click', onLockClick);
  $('mode-login').addEventListener('click', openPin);

