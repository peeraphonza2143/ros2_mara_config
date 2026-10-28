  // ── บันทึกแผนที่ (ล่างสุดของแถบขวา) + เตือนเมื่อแผนที่ยังไม่ได้บันทึก ──────────────────────────────
  var NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$/;
  var saveBusy = false;
  var lastSaved = null;            // { name, time(epoch s) } ล่าสุดที่บันทึก (จากเซิร์ฟเวอร์)

  function mapStatus() { return (lastStatus && lastStatus.map) ? lastStatus.map : { available: false, unsaved: false, saved_name: null }; }

  function mapUnsaved() {          // กำลังสร้างแผนที่อยู่ และยังไม่ได้บันทึก (หรือแผนที่เปลี่ยนไปหลังบันทึก)
    return !!(lastMode && lastMode.mode === 'MAPPING' && lastMode.state === 'RUNNING' &&
              mapStatus().available && mapStatus().unsaved);
  }

  function defaultMapName() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return 'map_' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '_' + p(d.getHours()) + p(d.getMinutes());
  }

  var namePrefilled = false;
  var savedHint = false;           // เพิ่งบันทึกแผนที่นี้ → แสดงขั้นต่อไป (ล้างเมื่อแผนที่เปลี่ยน หรือเริ่ม Mapping รอบใหม่)
  var wasMapping = false;

  function renderSave() {
    var input = $('map-name');
    if (!namePrefilled && admin && input.value === '') { input.value = defaultMapName(); namePrefilled = true; }
    var name = input.value.trim();
    var nameOk = NAME_RE.test(name);
    var err = $('map-name-err');
    err.hidden = (name === '' || nameOk);
    err.textContent = err.hidden ? '' : t('save.invalid');
    input.setAttribute('aria-invalid', err.hidden ? 'false' : 'true');
    input.disabled = !admin || !authEnabled;
    var ms = mapStatus();
    var running = mappingRunning();
    if ((running && !wasMapping) || ms.unsaved) { savedHint = false; }
    wasMapping = running;
    var hasMap = !!(running && ms.available);
    var text;
    if (!authEnabled) { text = t('drive.st.no_pin'); }
    else if (!admin) { text = t('save.state.need_admin'); }
    else if (!running) { text = t('save.state.need_mapping'); }
    else if (!hasMap) { text = t('save.state.waiting'); }
    else if (ms.saved_name && !ms.unsaved) { text = t('save.state.saved', { name: ms.saved_name }); }
    else if (ms.saved_name) { text = t('save.state.changed', { name: ms.saved_name }); }
    else { text = t('save.state.unsaved'); }
    $('map-save-state').textContent = text;
    $('map-save').disabled = !(admin && authEnabled && hasMap && nameOk && !saveBusy);
    $('map-save').textContent = t(saveBusy ? 'save.saving' : 'ui.save.button');
    $('map-save-stop').disabled = $('map-save').disabled;                 // บันทึกแล้วกลับ Standby ในครั้งเดียว (เงื่อนไขเดียวกับปุ่มบันทึก)
    $('map-next').textContent = savedHint ? t('save.next') : '';
    $('map-next').hidden = !savedHint;
    $('map-last-saved').textContent = lastSaved ? t('save.last', { name: lastSaved.name, time: hhmm(lastSaved.time) }) : '';
  }

  function loadSaved() {
    return api('/api/map/saved').then(function (r) {
      if (r.status === 200 && r.data && r.data.last && r.data.last.name) {
        lastSaved = { name: r.data.last.name, time: r.data.last.time };
        renderSave();
      }
    });
  }

  function doSave(overwrite, thenStop) {
    var name = $('map-name').value.trim();
    if (!NAME_RE.test(name) || saveBusy) { return; }
    saveBusy = true;
    renderSave();
    api('/api/map/save', overwrite ? { name: name, overwrite: true } : { name: name }).then(function (r) {
      saveBusy = false;
      if (r.status === 200 && r.data && r.data.ok) {
        lastSaved = { name: r.data.name, time: r.data.saved };
        if (lastStatus && lastStatus.map) { lastStatus.map.unsaved = false; lastStatus.map.saved_name = r.data.name; }
        toast(t('toast.map_saved', { name: r.data.name }));
        savedHint = true;
        if ($('map-name').value.trim() === r.data.name) { $('map-name').value = defaultMapName(); }   // ครั้งหน้าไม่ชนชื่อเดิม (ต้องเปลี่ยนเองเมื่อจะทับ)
        if (thenStop) { renderSave(); sendMode('STANDBY'); return; }
      } else if (r.status === 409 && r.data && r.data.error && r.data.error.code === 'map.err.exists') {
        renderSave();
        confirmDialog(t('confirm.overwrite.title', { name: name }), t('confirm.overwrite.body'), t('confirm.overwrite.ok'))
          .then(function (ok) { if (ok) { doSave(true, thenStop); } });
        return;
      } else if (r.status === 401) {
        admin = false;
        renderAuth();
        toast(t('toast.session_expired'));
        openPin();
      } else {
        toast(tm(r.data && r.data.error) || t('toast.map_save_failed'));
      }
      renderSave();
    });
  }

  $('map-save').addEventListener('click', function () { doSave(false); });
  $('map-save-stop').addEventListener('click', function () { doSave(false, true); });
  $('map-name').addEventListener('input', renderSave);
  $('map-name').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !$('map-save').disabled) { doSave(false); }
  });

