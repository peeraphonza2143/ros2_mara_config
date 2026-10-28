"""
map_saver — บันทึกแผนที่ที่กำลังสร้าง (/map) เป็นไฟล์ .pgm + .yaml ใช้กับ Nav2 ได้ทันที
(ไม่พึ่ง ROS ในตัวโค้ด → ทดสอบได้)

เรียก `ros2 run nav2_map_server map_saver_cli -f <ไฟล์>` (ตัวเดียวกับที่คุณใช้ด้วยมือ) แต่ครอบด้วยความปลอดภัย:
  - ชื่อ: ตัวอักษร/ตัวเลข/ขีดกลาง/ขีดล่างเท่านั้น (ห้าม / .. ช่องว่าง → ไฟล์ไม่มีทางหลุดออกนอกโฟลเดอร์แผนที่)
  - บันทึกลงโฟลเดอร์ชั่วคราวก่อน ตรวจไฟล์ครบแล้วค่อยย้ายเข้าที่ → ไม่มีไฟล์ครึ่งๆ กลางๆ ;
    ถ้าล้มเหลวระหว่างทับของเดิม แผนที่เดิมยังอยู่ครบ
  - ชื่อซ้ำต้องบอกว่าอนุญาตให้ทับ (overwrite) ถึงจะทับ
  - บันทึกทีละรายการ (กดซ้ำระหว่างบันทึก = busy)
  - ข้างไฟล์ .yaml มี <ชื่อ>.json (เวลาที่บันทึก ขนาดพื้นที่ ค่า SLAM ที่ใช้ ฯลฯ) ไว้ใช้ในคลังแผนที่ภายหลัง

คลังแผนที่ (ใช้กฎความปลอดภัยชุดเดียวกับการบันทึก: ชื่อตรวจเข้ม, ทีละรายการ, ไม่ทิ้งไฟล์ครึ่งๆ กลางๆ):
  - rename: สร้างชุดไฟล์ชื่อใหม่ให้ครบก่อน (แก้ `image:` ใน .yaml และ `name` ใน .json) แล้วค่อยลบชุดเดิม
  - delete: ไม่ลบทิ้ง แต่ย้ายไป <maps_dir>/.trash/ (กู้คืนด้วยมือได้)
  - thumbnail: ภาพ PNG ย่อจาก .pgm ไว้โชว์ในรายการ
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import yaml
from PIL import Image

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
DEFAULT_COMMAND = ('ros2', 'run', 'nav2_map_server', 'map_saver_cli')
# map_saver_cli รอรับ /map แค่ 2 วินาทีตามค่าตั้งต้น
# ถ้าการค้นพบ topic (DDS) ช้ากว่านั้นจะล้มเป็นครั้งคราว จึงขยายเวลารอ
DEFAULT_EXTRA_ARGS = ('--ros-args', '-p', 'save_map_timeout:=8.0')
ATTEMPTS = 2                      # ล้มเหลวจากเครื่องมือ (ไม่ใช่หมดเวลา/ไม่พบเครื่องมือ) ลองซ้ำ 1 ครั้ง
EXTS = ('.pgm', '.yaml', '.json')
TRASH_DIR = '.trash'              # แผนที่ที่ลบแล้วอยู่ที่นี่ (list() ไม่เห็น เพราะอ่านเฉพาะไฟล์ใน maps_dir ตรงๆ)
THUMB_MAX_PX = 240                # ด้านยาวสุดของภาพย่อ


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


class MapSaverError(Exception):
    """code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


class MapSaver:

    def __init__(self, maps_dir, command=DEFAULT_COMMAND, timeout=25.0, on_error=None, extra_args=None):
        self.maps_dir = os.path.expanduser(maps_dir)
        self.command = list(command)
        self.extra_args = list(DEFAULT_EXTRA_ARGS if (extra_args is None and tuple(command) == DEFAULT_COMMAND)
                               else (extra_args or []))
        self.timeout = float(timeout)
        self._on_error = on_error or (lambda text: None)
        self._busy = threading.Lock()

    @staticmethod
    def valid_name(name):
        return isinstance(name, str) and bool(NAME_RE.fullmatch(name))

    def path(self, name, ext='.yaml'):
        return os.path.join(self.maps_dir, name + ext)

    def exists(self, name):
        return self.valid_name(name) and os.path.exists(self.path(name))

    def list(self):
        """แผนที่ที่บันทึกไว้ (ใหม่สุดก่อน) : [{name, saved, bytes, meta}]"""
        out = []
        try:
            names = [f[:-5] for f in os.listdir(self.maps_dir) if f.endswith('.yaml')]
        except OSError:
            return out
        for name in names:
            if not self.valid_name(name) or not os.path.exists(self.path(name, '.pgm')):
                continue
            try:
                meta = json.load(open(self.path(name, '.json'), encoding='utf-8'))
            except (OSError, ValueError):
                meta = None
            size = sum(os.path.getsize(self.path(name, e)) for e in EXTS if os.path.exists(self.path(name, e)))
            out.append({'name': name, 'saved': os.path.getmtime(self.path(name)), 'bytes': size, 'meta': meta})
        return sorted(out, key=lambda m: m['saved'], reverse=True)

    def is_complete(self, name):
        """มีไฟล์ครบพอจะใช้งานได้ (เกณฑ์เดียวกับที่ list() ใช้)"""
        return self.valid_name(name) and all(os.path.exists(self.path(name, e)) for e in ('.yaml', '.pgm'))

    def thumbnail(self, name, max_px=THUMB_MAX_PX):
        """ภาพ PNG ย่อของแผนที่ (bytes) ; ไม่พบ → map.err.not_found"""
        if not self.valid_name(name):
            raise MapSaverError('map.err.name')
        if not self.is_complete(name):
            raise MapSaverError('map.err.not_found', name=name)
        try:
            with Image.open(self.path(name, '.pgm')) as src:
                img = src.convert('L')
            # BOX = เฉลี่ยพิกเซลที่ถูกย่อ → เส้นผนังบางๆ ไม่หายตอนย่อ ; เก่ากว่า Pillow 9.1 ไม่มี Image.Resampling
            img.thumbnail((max_px, max_px), getattr(Image, 'Resampling', Image).BOX)
            out = io.BytesIO()
            img.save(out, 'PNG')
            return out.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            raise MapSaverError('map.err.io', detail=str(e)[:160])

    def rename(self, old, new):
        """เปลี่ยนชื่อแผนที่ ; คืน {'name': new} ; ผิดพลาดโยน MapSaverError (แผนที่เดิมยังอยู่ครบเสมอ)
        ไม่ใช่แค่ย้ายไฟล์: .yaml มี `image: <ชื่อเดิม>.pgm` ถ้าไม่แก้ map_server จะหาภาพไม่เจอ"""
        if not (self.valid_name(old) and self.valid_name(new)):
            raise MapSaverError('map.err.name')
        if old == new:
            raise MapSaverError('map.err.same_name')
        if not self._busy.acquire(blocking=False):
            raise MapSaverError('map.err.busy')
        tmp = None
        placed = []
        try:
            if not self.is_complete(old):
                raise MapSaverError('map.err.not_found', name=old)
            if self.exists(new):
                raise MapSaverError('map.err.exists', name=new)
            tmp = os.path.join(self.maps_dir, f'.renaming-{os.getpid()}-{int(time.time() * 1000)}')
            os.makedirs(tmp)
            try:
                with open(self.path(old), encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    raise ValueError('ไฟล์ yaml ไม่ครบ')
                data['image'] = new + '.pgm'
                with open(os.path.join(tmp, new + '.yaml'), 'w', encoding='utf-8') as f:
                    yaml.safe_dump(data, f, sort_keys=False)
                try:
                    with open(self.path(old, '.json'), encoding='utf-8') as f:
                        info = json.load(f)
                    if not isinstance(info, dict):
                        info = {}
                except (OSError, ValueError):
                    info = {}                                                  # ไม่มี/เสีย = สร้างใหม่ (ตรงกับ list())
                info['name'] = new
                with open(os.path.join(tmp, new + '.json'), 'w', encoding='utf-8') as f:
                    json.dump(info, f)
                shutil.copy2(self.path(old, '.pgm'), os.path.join(tmp, new + '.pgm'))
                stat = os.stat(self.path(old))
                # วันที่บันทึกคงเดิม (ลำดับในรายการไม่เปลี่ยนเพราะเปลี่ยนชื่อ)
                os.utime(os.path.join(tmp, new + '.yaml'), ns=(stat.st_atime_ns, stat.st_mtime_ns))
                for ext in ('.pgm', '.json', '.yaml'):                         # .yaml ท้ายสุด = มีไฟล์นี้แปลว่าครบ
                    os.replace(os.path.join(tmp, new + ext), self.path(new, ext))
                    placed.append(ext)
            except (OSError, ValueError, yaml.YAMLError) as e:
                if '.yaml' not in placed:                                      # ยังไม่ครบ → เก็บกวาดของใหม่ที่วางไปแล้ว
                    for ext in placed:
                        _remove_quietly(self.path(new, ext))
                raise MapSaverError('map.err.io', detail=str(e)[:160])
            for ext in ('.yaml', '.json', '.pgm'):                             # ชุดใหม่ครบแล้วจึงลบชุดเดิม (.yaml ก่อน)
                try:
                    os.remove(self.path(old, ext))
                except FileNotFoundError:
                    pass
                except OSError as e:
                    self._on_error(f'ลบไฟล์เดิมของแผนที่ {old}{ext} ไม่สำเร็จ: {e}')
            return {'name': new}
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            self._busy.release()

    def delete(self, name):
        """ลบแผนที่ = ย้ายไป .trash/ ; คืน {'name', 'trash'} ; .yaml ย้ายก่อน → หายจากรายการทันที"""
        if not self.valid_name(name):
            raise MapSaverError('map.err.name')
        if not self._busy.acquire(blocking=False):
            raise MapSaverError('map.err.busy')
        try:
            if not self.is_complete(name):
                raise MapSaverError('map.err.not_found', name=name)
            try:
                trash_root = os.path.join(self.maps_dir, TRASH_DIR)
                os.makedirs(trash_root, exist_ok=True)
                # mkdtemp = ชื่อไม่ซ้ำแน่นอน (ลบแผนที่ชื่อเดิมซ้ำถี่ๆ ก็ไม่ชนกัน)
                trash = tempfile.mkdtemp(prefix=f'{name}-{int(time.time())}-', dir=trash_root)
                for ext in ('.yaml', '.json', '.pgm'):
                    if os.path.exists(self.path(name, ext)):
                        os.replace(self.path(name, ext), os.path.join(trash, name + ext))
            except OSError as e:
                raise MapSaverError('map.err.io', detail=str(e)[:160])
            return {'name': name, 'trash': trash}
        finally:
            self._busy.release()

    def save(self, name, overwrite=False, meta=None):
        """บันทึกแผนที่ ; คืน {'name', 'yaml', 'pgm', 'bytes', 'saved'} ; ผิดพลาดโยน MapSaverError (ไม่มีไฟล์ค้าง)"""
        if not self.valid_name(name):
            raise MapSaverError('map.err.name')
        if not self._busy.acquire(blocking=False):
            raise MapSaverError('map.err.busy')
        tmp = None
        try:
            if self.exists(name) and not overwrite:
                raise MapSaverError('map.err.exists', name=name)
            os.makedirs(self.maps_dir, exist_ok=True)
            tmp = os.path.join(self.maps_dir, f'.saving-{os.getpid()}-{int(time.time() * 1000)}')
            os.makedirs(tmp)
            self._run_saver(os.path.join(tmp, name))
            pgm, ymlf = os.path.join(tmp, name + '.pgm'), os.path.join(tmp, name + '.yaml')
            self._verify(name, pgm, ymlf)
            saved = time.time()
            info = dict(meta or {})
            info.update({'name': name, 'saved': saved})
            with open(os.path.join(tmp, name + '.json'), 'w', encoding='utf-8') as f:
                json.dump(info, f)
            for ext in ('.pgm', '.json', '.yaml'):                        # .yaml ท้ายสุด = มีไฟล์นี้แปลว่าครบ
                os.replace(os.path.join(tmp, name + ext), self.path(name, ext))
            size = sum(os.path.getsize(self.path(name, e)) for e in EXTS)
            return {'name': name, 'yaml': self.path(name), 'pgm': self.path(name, '.pgm'),
                    'bytes': size, 'saved': saved}
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            self._busy.release()

    def _run_saver(self, prefix):
        for attempt in range(1, ATTEMPTS + 1):
            try:
                done = subprocess.run(self.command + ['-f', prefix] + self.extra_args, capture_output=True, text=True,
                                      timeout=self.timeout, stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise MapSaverError('map.err.no_saver')
            except subprocess.TimeoutExpired:
                raise MapSaverError('map.err.timeout', seconds=int(self.timeout))
            except OSError as e:
                raise MapSaverError('map.err.save_failed', detail=str(e))
            if done.returncode == 0:
                return
            lines = [ln for ln in (done.stderr + '\n' + done.stdout).splitlines() if ln.strip()]
            self._on_error(f'map_saver_cli ล้มเหลว ครั้งที่ {attempt} (code {done.returncode}): '
                           + ' | '.join(lines[-3:]))
        errors = [ln for ln in lines if re.search(r'fail|error|timeout', ln, re.I)]
        detail = (errors or lines or [f'exit code {done.returncode}'])[-1]
        raise MapSaverError('map.err.save_failed', detail=detail[:160])

    @staticmethod
    def _verify(name, pgm, ymlf):
        try:
            if os.path.getsize(pgm) <= 0:
                raise ValueError('ไฟล์ภาพว่าง')
            with open(ymlf, encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict) or 'image' not in data or 'resolution' not in data:
                raise ValueError('ไฟล์ yaml ไม่ครบ')
        except (OSError, ValueError, yaml.YAMLError) as e:
            raise MapSaverError('map.err.save_failed', detail=f'incomplete output: {e}')
