"""
map_selection — จำว่า "ครั้งหน้าที่เปิด Navigation ให้ใช้แผนที่ไหน" (ไม่พึ่ง ROS → ทดสอบได้)

เก็บชื่อแผนที่ที่ผู้ดูแลเลือกไว้ในไฟล์เล็กๆ (เขียนแบบ atomic: ไฟล์ชั่วคราว → os.replace) แล้วตอนเปิดโหมด Navigation
ModeManager จะต่อ `map_file:=<พาธ .yaml>` ให้ nav_s2.launch.py ผ่าน launch_args()

ลำดับความสำคัญของแผนที่ที่ Navigation จะใช้ (choice):
  1. selected : แผนที่ที่เลือกผ่านเว็บ (ต้องยังมีไฟล์ครบ)
  2. config   : nav_map_file ใน web_app.yaml (ถ้าตั้งไว้)
  3. default  : แผนที่ default ของ nav_s2.launch.py (ไม่ส่ง map_file:= เลย)

การเลือกมีผลตอน "เปิด Navigation ครั้งถัดไป" เท่านั้น ไม่สลับแผนที่สดระหว่างที่ Nav รันอยู่ (AMCL ต้อง localize ใหม่)
"""
import json
import os
import threading
import time

from agv_web_app.map_saver import MapSaverError


class MapSelection:

    def __init__(self, path, saver, fallback=None, on_error=None):
        """path = ไฟล์ที่จำค่าที่เลือก ; saver = MapSaver (ตรวจชื่อ/ความครบของแผนที่) ;
        fallback = พาธ nav_map_file จาก config (ว่าง = ใช้ default ของ launch)"""
        self.path = os.path.expanduser(path)
        self._saver = saver
        self._fallback = os.path.expanduser(fallback) if fallback else None
        self._on_error = on_error or (lambda text: None)
        self._lock = threading.Lock()

    def name(self):
        """ชื่อแผนที่ที่เลือกไว้ (หรือ None) ; ไฟล์หาย/เสีย/ชื่อไม่ถูกต้อง = None (ไม่โยนข้อผิดพลาด)"""
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        name = data.get('name') if isinstance(data, dict) else None
        return name if self._saver.valid_name(name) else None

    def select(self, name):
        """เลือกแผนที่ (name=None = ยกเลิกการเลือก กลับไปใช้ config/default) ; ผิดพลาดโยน MapSaverError"""
        if name is not None and not self._saver.valid_name(name):
            raise MapSaverError('map.err.name')
        with self._lock:
            if name is None:
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    raise MapSaverError('map.err.io', detail=str(e)[:160])
                return
            if not self._saver.is_complete(name):
                raise MapSaverError('map.err.not_found', name=name)
            tmp = self.path + '.tmp'
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump({'name': name, 'time': time.time()}, f)
                os.replace(tmp, self.path)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise MapSaverError('map.err.io', detail=str(e)[:160])

    def choice(self):
        """แผนที่ที่จะใช้ตอนเปิด Navigation ครั้งถัดไป : {'source', 'name', 'file'}
        (file=None → ไม่ส่ง map_file:= ใช้ default ของ launch)"""
        name = self.name()
        if name is not None and self._saver.is_complete(name):
            return {'source': 'selected', 'name': name, 'file': self._saver.path(name)}
        if self._fallback:
            return {'source': 'config', 'name': None, 'file': self._fallback}
        return {'source': 'default', 'name': None, 'file': None}

    def launch_args(self):
        """อาร์กิวเมนต์ที่ต่อท้ายคำสั่งเปิด Navigation ; คืน (args, choice)
        แผนที่ที่เลือกไว้แต่ไฟล์หาย (เช่นถูกลบด้วยมือ) → เตือนแล้วถอยไปใช้ลำดับถัดไป ไม่ทำให้เปิดโหมดล้ม"""
        name = self.name()
        choice = self.choice()
        if name is not None and choice['source'] != 'selected':
            self._on_error(f'แผนที่ที่เลือกไว้ ({name}) ไม่ครบหรือถูกลบ — ใช้ {choice["source"]} แทน')
        return ([f'map_file:={choice["file"]}'] if choice['file'] else []), choice

    def info(self, in_use=None):
        """สถานะให้หน้าเว็บ : name ที่เลือก, available (ไฟล์ยังครบ), choice (ครั้งหน้า),
        in_use (ที่ Nav เปิดอยู่ตอนนี้ หรือ None), restart_needed (Nav รันอยู่แต่ใช้ไม่ตรงกับที่เลือกไว้)"""
        name = self.name()
        choice = self.choice()
        return {'name': name, 'available': name is not None and self._saver.is_complete(name),
                'choice': choice, 'in_use': in_use, 'restart_needed': in_use is not None and in_use != choice}
