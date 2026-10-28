"""
mode_manager — ตัวจัดการโหมดของหุ่น (STANDBY / MAPPING / MANUAL / NAVIGATION)

หน้าที่: เปิด/ปิดระบบ (ros2 launch) ของแต่ละโหมดเป็น process ของตัวเอง และบอกสถานะที่ "จริง"

ไม่ผูกกับ ROS โดยตรง: รับฟังก์ชัน list_nodes() ที่คืนชื่อ node ใน ROS graph เข้ามา
→ ทดสอบบนโต๊ะได้ด้วยคำสั่งปลอม (ดู test/test_mode_manager.py)
ข้อควรระวัง: list_nodes() จะถูกเรียกจากหลาย thread → ควรคืนค่าจาก cache ที่ thread ของ ROS อัปเดต

กติกาความปลอดภัย
  - สั่งเปลี่ยนโหมดซ้อนกันไม่ได้ (busy) และทำงานใน thread แยก ไม่บล็อกเว็บ
  - ปิดแบบสุภาพก่อน: SIGINT → SIGTERM → SIGKILL ส่งทั้ง process group (เหมือนกด Ctrl+C)
  - ก่อนเปิดโหมดใหม่ ต้องไม่มี node ของระบบรันอยู่นอกแอป
    (กัน launch ซ้อน → node ชื่อซ้ำ / serial ถูกยึด)
  - เปิดไม่ขึ้นตามเวลา / ระบบหยุดเอง → ปิดเก็บกวาดแล้วรายงาน ERROR พร้อมที่อยู่ไฟล์ log
"""
import glob
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime

from agv_web_app import sweep as sweep_module
from agv_web_app.msg import msg

STANDBY = 'STANDBY'
MAPPING = 'MAPPING'
MANUAL = 'MANUAL'
NAVIGATION = 'NAVIGATION'
MODES = (STANDBY, MAPPING, MANUAL, NAVIGATION)

# สถานะของตัวจัดการ (ไม่ใช่โหมด)
RUNNING = 'RUNNING'      # นิ่งอยู่ในโหมดปัจจุบัน
STARTING = 'STARTING'    # กำลังเปิดโหมดเป้าหมาย
STOPPING = 'STOPPING'    # กำลังปิดโหมดเดิม
ERROR = 'ERROR'          # เปลี่ยนโหมดไม่สำเร็จ (โหมดจะกลับเป็น STANDBY)

# node ที่ "เป็นของระบบหุ่น" — ถ้าพบตอนแอปไม่ได้เปิดเอง แปลว่ามีระบบรันนอกแอป
DEFAULT_SYSTEM_NODES = (
    'ekf_filter_node', 'zlac_odom_s2_node', 'hwt901b_node', 'sllidar_node',
    'slam_toolbox', 'bt_navigator', 'amcl', 'controller_server',
    'planner_server', 'map_server',
)
# พบ node เหล่านี้ครบ = ถือว่าโหมดเริ่มทำงานแล้ว
# (หมายเหตุ: node โผล่ใน graph ก่อน lifecycle ของ Nav2 จะ active ครบ — เป็นเกณฑ์ขั้นต่ำ)
DEFAULT_READY_NODES = {
    MAPPING: ('slam_toolbox',),
    MANUAL: ('ekf_filter_node',),
    NAVIGATION: ('bt_navigator',),
}
DEFAULT_READY_TIMEOUT = {MAPPING: 40.0, MANUAL: 30.0, NAVIGATION: 60.0}

KEEP_LOGS = 20


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_starttime(pid):
    """เวลาเริ่มของ process (หน่วย tick ของเคอร์เนล, ฟิลด์ที่ 22 ของ /proc/<pid>/stat) ; None = ไม่มี process นี้"""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            return int(f.read().decode(errors='replace').rsplit(')', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


class ModeManager:
    """state machine ของโหมด + ตัวควบคุม process ของระบบที่แอปเปิดเอง"""

    def __init__(self, commands, list_nodes, log_dir,
                 system_nodes=DEFAULT_SYSTEM_NODES, ready_nodes=None,
                 ready_timeout=None, sigint_timeout=20.0, sigterm_timeout=10.0,
                 settle_timeout=30.0, poll_interval=0.5, run_dir=None, stop_motion=None,
                 sweep=None, extra_args=None):
        self._commands = dict(commands)          # {MAPPING|MANUAL|NAVIGATION: [argv...]}
        self._list_nodes = list_nodes
        self._log_dir = os.path.expanduser(log_dir)
        self._system_nodes = frozenset(system_nodes)
        self._ready_nodes = dict(DEFAULT_READY_NODES)
        self._ready_nodes.update(ready_nodes or {})
        self._ready_timeout = dict(DEFAULT_READY_TIMEOUT)
        self._ready_timeout.update(ready_timeout or {})
        self._sigint_timeout = sigint_timeout
        self._sigterm_timeout = sigterm_timeout
        self._settle_timeout = settle_timeout
        self._poll = poll_interval
        # run_dir: จดกลุ่ม process ที่แอปเปิดไว้ (managed.json) → แอปตายแบบ SIGKILL/ไฟดับแล้วเปิดใหม่ยังรู้ว่ามีของค้าง
        self._record_path = os.path.join(os.path.expanduser(run_dir), 'managed.json') if run_dir else None
        self._stop_motion = stop_motion          # เรียกก่อนปิดระบบตอน Reset all / ปิดแอป (ส่งความเร็ว 0)
        self._sweep = sweep                      # ฟังก์ชัน () → (stopped_pids, remaining) ; None = ไม่กวาด process อื่น
        # {โหมด: ฟังก์ชัน () → [อาร์กิวเมนต์เพิ่ม]} เรียกตอนเปิดโหมดทุกครั้ง (เช่น slam_params_file:=… ของ Mapping)
        self._extra_args = dict(extra_args or {})

        self._lock = threading.RLock()
        self._mode = STANDBY
        self._target = None
        self._state = RUNNING
        self._message = None        # msg() หรือ None
        self._since = time.time()
        self._proc = None
        self._log_path = ''
        self._busy = False
        self._thread = None
        self.last_stop = ''      # วิธีที่ใช้ปิดครั้งล่าสุด: SIGINT / SIGTERM / SIGKILL / ended
        self._leftover = self._load_leftover()   # กลุ่ม process ค้างจากรอบก่อน {'pid','pgid','mode',...} หรือ None

    # ── ส่วนที่เรียกจากภายนอก ──────────────────────────────────────────
    def request_mode(self, target):
        """ขอเปลี่ยนโหมด คืน (ok, msg); ok=True = "รับคำสั่งแล้ว" ผลจริงดูจาก snapshot()"""
        if target not in MODES:
            return False, msg('mode.err.unknown', mode=str(target))
        with self._lock:
            if self._busy:
                return False, msg('mode.err.busy')
            if target != STANDBY and target not in self._commands:
                return False, msg('mode.err.no_command', mode=target)
            if target == self._mode and self._state in (RUNNING, ERROR):
                if self._state == ERROR:          # สั่งโหมดเดิมซ้ำ = รับทราบ error
                    self._state, self._message = RUNNING, None
                return True, msg('mode.already', mode=target)
            if self._proc is None:
                try:
                    external = self._external_nodes()
                except Exception as e:            # ตรวจ graph ไม่ได้ → ไม่เสี่ยงเปิด
                    return False, msg('mode.err.check_failed', error=str(e))
                if external:
                    if target == STANDBY:
                        return True, msg('mode.standby.external')
                    return False, msg('mode.err.external', nodes=external)
                if self._leftover_alive():
                    if target == STANDBY:
                        return True, msg('mode.standby.external')
                    return False, msg('mode.err.leftover_session', mode=self._leftover['mode'])
                if target == STANDBY:
                    self._mode, self._state, self._message = STANDBY, RUNNING, None
                    return True, msg('mode.standby.already')
            self._busy = True
            self._target = target
            self._thread = threading.Thread(
                target=self._worker, args=(target,), daemon=True)
            self._thread.start()
        return True, msg('mode.accepted', mode=target)

    def snapshot(self):
        self._check_crash()
        with self._lock:
            managed_proc = self._proc is not None
            snap = {
                'mode': self._mode,
                'target': self._target,
                'state': self._state,
                'message': self._message,
                'since': self._since,
                'log': self._log_path,
            }
        external = [] if managed_proc else self._safe_external()
        snap['external'] = external
        snap['managed'] = not external
        snap['leftover'] = None
        if not managed_proc and self._leftover_alive():
            snap['leftover'] = {'mode': self._leftover['mode']}
        return snap

    def request_reset(self):
        """Reset all: ปิดทุกอย่างของระบบหุ่น (ที่แอปเปิด + ที่ค้างจากรอบก่อน + ที่เปิดนอกแอป) ; คืน (ok, msg)
        ทำงานใน thread แยก ผลดูจาก snapshot() (STOPPING → STANDBY หรือ ERROR)"""
        with self._lock:
            if self._busy:
                return False, msg('mode.err.busy')
            self._busy = True
            self._target = STANDBY
            self._state, self._message = STOPPING, msg('mode.resetting')
            self._since = time.time()
            self._thread = threading.Thread(target=self._reset_worker, daemon=True)
            self._thread.start()
        return True, msg('mode.resetting')

    def shutdown(self, timeout=90.0):
        """ปิดระบบที่แอปเปิดไว้ (เรียกตอนปิดเว็บแอป กันระบบกำพร้ายึดมอเตอร์)"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            self._call_stop_motion()
            self.last_stop = self._stop_process(proc)
        with self._lock:
            self._mode, self._target, self._state = STANDBY, None, RUNNING

    # ── บันทึกกลุ่ม process ที่แอปเปิด (ให้รู้จักของค้างหลังแอปตาย) ───────────────
    def _write_record(self, proc, mode):
        if not self._record_path:
            return
        record = {'pid': proc.pid, 'pgid': proc.pid, 'mode': mode, 'argv': list(self._commands[mode]),
                  'starttime': _proc_starttime(proc.pid), 'written': time.time()}
        tmp = self._record_path + '.tmp'
        try:
            os.makedirs(os.path.dirname(self._record_path), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(record, f)
            os.replace(tmp, self._record_path)
        except OSError:
            pass                                  # จดไม่ได้ไม่ทำให้เปิดโหมดล้ม (แค่ไม่มีเครื่องช่วยกู้)

    def _forget_record(self):
        if self._record_path:
            try:
                os.remove(self._record_path)
            except OSError:
                pass

    def _load_leftover(self):
        """อ่านบันทึกของรอบก่อน: กลุ่ม process ยังอยู่และเป็นของเรา (เวลาเริ่มตรง/สมาชิกเป็นระบบหุ่น) = ของค้าง"""
        if not self._record_path:
            return None
        try:
            with open(self._record_path, encoding='utf-8') as f:
                rec = json.load(f)
            pid, pgid, mode = int(rec['pid']), int(rec['pgid']), str(rec['mode'])
            starttime = rec.get('starttime')
        except (OSError, ValueError, KeyError, TypeError):
            self._forget_record()
            return None
        if not _group_alive(pgid):
            self._forget_record()
            return None
        now_start = _proc_starttime(pid)
        if now_start is not None:
            # ผู้นำกลุ่มยังอยู่: เวลาเริ่มต้องตรง (กัน pid ซ้ำ)
            mine = (starttime is not None and now_start == starttime)
        else:
            # ผู้นำตายแล้ว: ดูว่าสมาชิกที่เหลือเป็นระบบหุ่นไหม
            mine = bool(sweep_module.group_processes(pgid))
        if not mine:
            self._forget_record()
            return None
        return {'pid': pid, 'pgid': pgid, 'mode': mode}

    def _leftover_alive(self):
        rec = self._leftover
        if rec is None:
            return False
        if _group_alive(rec['pgid']):
            return True
        self._leftover = None                     # หายไปเองแล้ว (เช่นผู้ใช้ปิดเอง)
        self._forget_record()
        return False

    def _call_stop_motion(self):
        if self._stop_motion is not None:
            try:
                self._stop_motion()
            except Exception:                     # noqa: BLE001 — ส่งความเร็ว 0 ไม่ได้ต้องไม่ขวางการปิดระบบ
                pass

    # ── Reset all ───────────────────────────────────────────────────────
    def _reset_worker(self):
        try:
            self._do_reset()
        except Exception as e:                    # noqa: BLE001
            self._fail(msg('mode.err.internal', error=str(e)))
        finally:
            with self._lock:
                self._busy = False

    def _do_reset(self):
        count = 0
        self._call_stop_motion()                  # สั่งหยุดก่อน (หุ่นไม่วิ่งค้างด้วยคำสั่งล่าสุดตอนระบบถูกปิด)
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            self.last_stop = self._stop_process(proc)
            count += 1
        leftover, self._leftover = self._leftover, None
        if leftover is not None and _group_alive(leftover['pgid']):
            self._stop_pgid(leftover['pgid'])
            count += 1
        self._forget_record()
        remaining = []
        if self._sweep is not None:
            stopped, remaining = self._sweep()
            count += len(stopped)
        self._call_stop_motion()                  # อีกครั้งหลังปิด: กันคำสั่งสุดท้ายที่หลุดมาระหว่างปิดระบบ
        if not self._wait_nodes_gone():
            self._fail(msg('mode.err.leftover', nodes=self._safe_external()))
            return
        if remaining:
            self._fail(msg('mode.err.reset_remaining', procs=sorted({r.why for r in remaining})))
            return
        self._set(mode=STANDBY, target=None, state=RUNNING,
                  message=msg('mode.reset.done', count=count) if count else msg('mode.reset.nothing'))

    # ── ภายใน ──────────────────────────────────────────────────────────
    def _external_nodes(self):
        return sorted(set(self._list_nodes()) & self._system_nodes)

    def _safe_external(self):
        try:
            return self._external_nodes()
        except Exception:
            return []

    def _set(self, **fields):
        with self._lock:
            for key, value in fields.items():
                setattr(self, '_' + key, value)
            self._since = time.time()

    def _fail(self, message):
        """เปลี่ยนไม่สำเร็จ: ระบบถูกปิดแล้ว → กลับ STANDBY พร้อมแจ้ง ERROR"""
        with self._lock:
            self._proc = None
            self._mode, self._target = STANDBY, None
            self._state, self._message = ERROR, message
            self._since = time.time()

    def _worker(self, target):
        try:
            self._transition(target)
        except Exception as e:      # กันเธรดตายเงียบ ๆ
            self._fail(msg('mode.err.internal', error=str(e)))
        finally:
            with self._lock:
                self._busy = False

    def _transition(self, target):
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._set(state=STOPPING, message=msg('mode.stopping', mode=self._mode))
            self.last_stop = self._stop_process(proc)
            with self._lock:
                self._proc = None
            if not self._wait_nodes_gone():
                self._fail(msg('mode.err.leftover', nodes=self._safe_external()))
                return

        if target == STANDBY:
            self._set(mode=STANDBY, target=None, state=RUNNING, message=None)
            return

        external = self._external_nodes()
        if external:
            self._fail(msg('mode.err.external', nodes=external))
            return

        self._set(mode=STANDBY, state=STARTING, message=msg('mode.starting', mode=target))
        try:
            proc = self._spawn(target)
        except FileNotFoundError:
            self._fail(msg('mode.err.no_ros2'))
            return
        with self._lock:
            self._proc = proc

        deadline = time.monotonic() + self._ready_timeout[target]
        need = set(self._ready_nodes[target])
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                self.last_stop = self._stop_process(proc)
                self._fail(msg('mode.err.exited', mode=target, exit_code=code, log=self._log_path))
                return
            if need <= set(self._list_nodes()):
                self._set(mode=target, target=None, state=RUNNING, message=None)
                return
            time.sleep(self._poll)

        self.last_stop = self._stop_process(proc)
        self._fail(msg('mode.err.timeout', mode=target,
                       seconds=int(self._ready_timeout[target]), log=self._log_path))

    def _spawn(self, target):
        os.makedirs(self._log_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._log_path = os.path.join(self._log_dir, f'mode_{target}_{stamp}.log')
        self._prune_logs()
        log = open(self._log_path, 'wb')
        try:
            # start_new_session → process group ของตัวเอง (ส่งสัญญาณทั้งกลุ่มได้ ไม่กระทบเว็บแอป)
            argv = list(self._commands[target])
            if target in self._extra_args:
                argv += self._extra_args[target]()
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        self._write_record(proc, target)
        return proc

    def _prune_logs(self):
        logs = sorted(glob.glob(os.path.join(self._log_dir, 'mode_*.log')))
        for old in logs[:-KEEP_LOGS]:
            try:
                os.remove(old)
            except OSError:
                pass

    def _stop_process(self, proc):
        """ปิดทั้ง process group แบบสุภาพก่อน คืนชื่อสัญญาณสุดท้ายที่ต้องใช้"""
        used = self._stop_pgid(proc.pid, leader=proc)
        if not _group_alive(proc.pid):
            self._forget_record()                 # ปิดสะอาดแล้ว ไม่ต้องจำ
        return used

    def _stop_pgid(self, pgid, leader=None):
        """SIGINT → SIGTERM → SIGKILL ทั้งกลุ่ม ; leader = Popen ของผู้นำ (ถ้าเป็นลูกของเรา ต้อง poll เพื่อเก็บศพ)"""
        def gone():
            if leader is not None and leader.poll() is None:
                return False
            return not _group_alive(pgid)

        if gone():
            return 'ended'
        used = 'ended'
        for sig, wait in ((signal.SIGINT, self._sigint_timeout),
                          (signal.SIGTERM, self._sigterm_timeout),
                          (signal.SIGKILL, 5.0)):
            used = sig.name
            _signal_group(pgid, sig)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if gone():
                    return used
                time.sleep(0.05)
        return used

    def _wait_nodes_gone(self):
        """รอจน node ของระบบหายจาก ROS graph (process จบ ≠ graph อัปเดตแล้ว)"""
        deadline = time.monotonic() + self._settle_timeout
        while time.monotonic() < deadline:
            if not self._external_nodes():
                return True
            time.sleep(self._poll)
        return not self._external_nodes()

    def _check_crash(self):
        """ถ้าระบบที่เปิดไว้ตายเอง (นิ่งอยู่ในโหมดแล้วค่อยตาย) ให้เก็บกวาดแล้วรายงาน ERROR"""
        with self._lock:
            proc = self._proc
            if (self._busy or proc is None or self._state != RUNNING
                    or proc.poll() is None):
                return
            self._busy = True
            crashed_mode, code = self._mode, proc.returncode
            self._state, self._message = STOPPING, msg('mode.crash.cleanup')
            self._since = time.time()
            self._thread = threading.Thread(
                target=self._cleanup_after_crash,
                args=(proc, crashed_mode, code), daemon=True)
            self._thread.start()

    def _cleanup_after_crash(self, proc, mode, code):
        try:
            self.last_stop = self._stop_process(proc)
            self._wait_nodes_gone()
            self._fail(msg('mode.err.crashed', mode=mode, exit_code=code, log=self._log_path))
        finally:
            with self._lock:
                self._busy = False
