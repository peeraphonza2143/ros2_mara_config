#!/usr/bin/env python3
"""
system_launcher.py

ห่อ QProcess สำหรับเปิด/ปิด "ros2 launch <package> <launch_file>" จากหน้าจอ

การปิด:
  ส่ง SIGINT ไปที่ process ของ ros2 launch เท่านั้น (เหมือนกด Ctrl+C)
  ros2 launch จะปิด node ลูกทุกตัวให้เอง ถ้าเงียบเกินเวลาจึงค่อยส่ง SIGTERM / SIGKILL

  หมายเหตุ: ไม่ส่งไปทั้ง process group เพราะ ros2 launch อยู่ใน group เดียวกับตัว app
  ถ้าส่ง killpg ตัว app จะโดนปิดไปด้วย
"""

import os
import signal

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

SIGTERM_AFTER_MS = 10000   # SIGINT แล้วยังไม่ปิดใน 10 s → SIGTERM
SIGKILL_AFTER_MS = 15000   # ยังไม่ปิดอีกใน 15 s → SIGKILL


class SystemLauncher(QObject):
    output = pyqtSignal(str)          # ข้อความจาก launch ทีละบรรทัด
    running_changed = pyqtSignal(bool)

    def __init__(self, package: str, launch_file: str, parent=None):
        super().__init__(parent)
        self.package = package
        self.launch_file = launch_file

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONUNBUFFERED', '1')          # ให้ log ออกมาทันที ไม่ค้างใน buffer
        env.insert('RCUTILS_COLORIZED_OUTPUT', '0')  # ตัดรหัสสี ANSI ออกจาก log
        self.proc.setProcessEnvironment(env)

        self.proc.readyReadStandardOutput.connect(self._read_output)
        self.proc.started.connect(lambda: self.running_changed.emit(True))
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

        self._stopping = False
        self._stop_gen = 0       # กัน timer ของการ stop รอบก่อนไปยิงใส่ process รอบใหม่
        self._partial = ''

    def is_running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self):
        if self.is_running():
            return
        self._stopping = False
        self.output.emit(f'$ ros2 launch {self.package} {self.launch_file}')
        self.proc.start('ros2', ['launch', self.package, self.launch_file])

    def stop(self):
        if not self.is_running() or self._stopping:
            return
        self._stopping = True
        self._stop_gen += 1
        gen = self._stop_gen
        pid = int(self.proc.processId())
        self.output.emit('>>> ส่ง SIGINT (Ctrl+C) ไปที่ ros2 launch ...')
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            return
        QTimer.singleShot(SIGTERM_AFTER_MS, lambda: self._escalate_term(gen))
        QTimer.singleShot(SIGKILL_AFTER_MS, lambda: self._escalate_kill(gen))

    def _escalate_term(self, gen):
        if gen == self._stop_gen and self._stopping and self.is_running():
            self.output.emit('>>> ยังไม่ปิด ส่ง SIGTERM')
            self.proc.terminate()

    def _escalate_kill(self, gen):
        if gen == self._stop_gen and self._stopping and self.is_running():
            self.output.emit('>>> ยังไม่ปิด ส่ง SIGKILL (node ลูกอาจค้าง ตรวจด้วย pgrep)')
            self.proc.kill()

    def _read_output(self):
        data = bytes(self.proc.readAllStandardOutput()).decode('utf-8', errors='replace')
        text = self._partial + data
        lines = text.split('\n')
        self._partial = lines.pop()   # บรรทัดสุดท้ายอาจยังไม่จบ เก็บไว้รอรอบหน้า
        for line in lines:
            if line.strip():
                self.output.emit(line.rstrip())

    def _on_finished(self, exit_code, _exit_status):
        if self._partial.strip():
            self.output.emit(self._partial.rstrip())
        self._partial = ''
        self._stopping = False
        self.output.emit(f'>>> ros2 launch จบการทำงาน (exit code {exit_code})')
        self.running_changed.emit(False)

    def _on_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.output.emit('>>> เปิด ros2 launch ไม่ได้ — source workspace แล้วหรือยัง?')
            self.running_changed.emit(False)
