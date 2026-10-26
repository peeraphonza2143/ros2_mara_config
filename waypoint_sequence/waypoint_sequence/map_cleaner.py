"""
map_cleaner.py — ลบ noise ในแผนที่ occupancy grid (.yaml + .pgm) แล้วบันทึกเป็นไฟล์ใหม่

ใช้งาน:
  ros2 run waypoint_sequence map_cleaner \\
    --map ~/ros2_ws/src/my_robot_bringup/map/nidec_Map.yaml

ทำ 2 อย่างอัตโนมัติ (ปรับ slider แล้วเห็นผลทันที):
  1. ลบจุดดำ (occupied) ก้อนเล็ก → เปลี่ยนเป็นพื้นที่ว่าง
  2. เติมจุดเทา (unknown) ก้อนเล็กกลางพื้นที่ว่าง → เปลี่ยนเป็นพื้นที่ว่าง
และมีแปรงระบายเองสำหรับร่องรอยใหญ่ (เช่น คนเดินผ่านตอนทำ SLAM)

เมาส์: scroll = zoom | ลากขวา/กลาง = เลื่อน | ลากซ้าย = ระบาย (เมื่อเลือกแปรง)
คีย์ลัด: Ctrl+Z = undo | Ctrl+S = save as | F = พอดีจอ
"""

import argparse
import os
import sys

import numpy as np
import yaml
from PIL import Image
from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QKeySequence, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QAction, QApplication, QButtonGroup, QCheckBox, QFileDialog,
                             QGraphicsEllipseItem, QGraphicsScene, QGraphicsView, QGridLayout,
                             QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
                             QScrollArea, QShortcut, QSizePolicy, QSlider, QSplitter, QStyle,
                             QToolBar, QVBoxLayout, QWidget)
from scipy import ndimage

from waypoint_sequence import ui_theme as theme

EIGHT = np.ones((3, 3), bool)   # 8-connectivity: pixel ที่แตะกันแค่มุมก็นับเป็นก้อนเดียวกัน
UNDO_LIMIT = 30
FIXED_FREE_THRESH = 0.196   # ค่าที่ทำให้สี unknown 205 ถูกอ่านเป็น unknown (เหมือน ROS1)


# ═════════════════════════════════════════════════════════════════════════════
#  ส่วนประมวลผล (numpy ล้วน ไม่ขึ้นกับ GUI)
# ═════════════════════════════════════════════════════════════════════════════
class MapData:
    def __init__(self, yaml_path: str):
        self.yaml_path = os.path.abspath(yaml_path)
        with open(self.yaml_path) as f:
            self.meta = yaml.safe_load(f)
        img = self.meta['image']
        if not os.path.isabs(img):
            img = os.path.join(os.path.dirname(self.yaml_path), img)
        self.image_path = os.path.abspath(img)
        self.original = np.array(Image.open(self.image_path).convert('L'))
        self.height, self.width = self.original.shape
        self.resolution = float(self.meta['resolution'])
        self.negate = int(self.meta.get('negate', 0))
        self.occ_thresh = float(self.meta.get('occupied_thresh', 0.65))
        self.free_thresh = float(self.meta.get('free_thresh', 0.25))
        self.file_free_thresh = self.free_thresh

        # ค่า pixel ที่ใช้ระบายแต่ละประเภท
        # free = ค่าที่ "ว่างที่สุด" (ขาวสุดเมื่อ negate=0) ไม่ใช้ค่าที่พบบ่อยสุด เพราะถ้า
        # free_thresh สูงเกิน สีเทา 205 จะถูกนับเป็น free และพบบ่อยกว่า 254
        occ, free, _ = self.classify(self.original)
        free_px = self.original[free]
        if free_px.size:
            self.free_value = int(free_px.min() if self.negate else free_px.max())
        else:
            self.free_value = 0 if self.negate else 254
        self.occ_value = _most_common(self.original[occ], 255 if self.negate else 0)
        rest = self.original[~occ & (self.original != self.free_value)]
        self.unknown_value = _most_common(rest, 205)

    def gray_read_as_free(self) -> bool:
        """True ถ้าด้วย free_thresh ปัจจุบัน map_server จะอ่านสี unknown เป็น free"""
        if self.unknown_value == self.free_value:
            return False
        _, free, _ = self.classify(np.array([[self.unknown_value]], np.uint8))
        return bool(free[0, 0])

    def classify(self, arr: np.ndarray):
        """แยก pixel เป็น occupied / free / unknown ตามกฎของ map_server"""
        a = arr.astype(np.float32)
        prob = a / 255.0 if self.negate else (255.0 - a) / 255.0
        occ = prob > self.occ_thresh
        free = prob < self.free_thresh
        return occ, free, ~occ & ~free


def _most_common(values: np.ndarray, default: int) -> int:
    return int(np.bincount(values).argmax()) if values.size else default


def clean(md: MapData, arr: np.ndarray, max_speck: int, keep_near_unknown: bool,
          max_hole: int):
    """
    คืน (result, removed_mask, filled_mask, stats)
      max_speck         : ลบก้อน occupied ที่มีขนาด ≤ ค่านี้ (pixel), 0 = ไม่ลบ
      keep_near_unknown : ไม่ลบก้อนที่ติดพื้นที่ unknown ผืนใหญ่ (มักเป็นเศษผนังขอบแผนที่)
      max_hole          : เติมก้อน unknown ที่ขนาด ≤ ค่านี้ ให้เป็น free, 0 = ไม่เติม
    """
    occ, free, unknown = md.classify(arr)

    # 1) หา "รู" unknown ก้อนเล็ก (ยังไม่เติม รอดูว่าติดผนังไหมในขั้นที่ 3)
    holes = np.zeros_like(unknown)
    if max_hole > 0:
        ulab, _ = ndimage.label(unknown, structure=EIGHT)
        usize = np.bincount(ulab.ravel())
        small = usize <= max_hole
        small[0] = False
        # ก้อนที่แตะขอบภาพไม่ใช่รูกลางห้อง แต่เป็นขอบนอกแผนที่
        border = np.unique(np.concatenate([ulab[0], ulab[-1], ulab[:, 0], ulab[:, -1]]))
        small[border] = False
        holes = small[ulab]
    big_unknown = unknown & ~holes

    # 2) หาจุด noise occupied ก้อนเล็ก
    removed = np.zeros_like(occ)
    if max_speck > 0:
        olab, _ = ndimage.label(occ, structure=EIGHT)
        osize = np.bincount(olab.ravel())
        small = osize <= max_speck
        small[0] = False
        if keep_near_unknown:
            near = ndimage.binary_dilation(big_unknown, structure=EIGHT)
            small[np.unique(olab[near & occ])] = False
        removed = small[olab]

    # 3) เติมรูที่ไม่ติดผนังที่ยังเหลืออยู่ (รูข้างผนังจริงอาจเป็นมุมอับที่ LiDAR มองไม่เห็น)
    filled = np.zeros_like(unknown)
    if holes.any():
        occ_left = ndimage.binary_dilation(occ & ~removed, structure=EIGHT)
        bad = np.unique(ulab[occ_left & holes])
        filled = holes & ~np.isin(ulab, bad)

    result = arr.copy()
    result[removed | filled] = md.free_value

    stats = {
        'specks': int(np.unique(olab[removed]).size) if removed.any() else 0,
        'speck_px': int(removed.sum()),
        'holes': int(np.unique(ulab[filled]).size) if filled.any() else 0,
        'hole_px': int(filled.sum()),
        'occ_px': int(occ.sum()),
    }
    return result, removed, filled, stats


# ═════════════════════════════════════════════════════════════════════════════
#  GUI
# ═════════════════════════════════════════════════════════════════════════════
class CleanerView(QGraphicsView):
    """scroll = zoom, ลากขวา/กลาง = pan, ลากซ้าย = ระบาย (โหมดแปรง) หรือ pan (โหมดดู)"""

    def __init__(self, scene, cleaner):
        super().__init__(scene)
        self.cleaner = cleaner
        self._pan_last = None
        self._painting = False
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setRenderHint(QPainter.Antialiasing)

    def wheelEvent(self, event):
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)

    def _start_pan(self, event):
        self._pan_last = event.pos()
        self.viewport().setCursor(Qt.ClosedHandCursor)

    def mousePressEvent(self, event):
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            self._start_pan(event)
        elif event.button() == Qt.LeftButton:
            if self.cleaner.brush_mode is None:
                self._start_pan(event)
            else:
                self._painting = True
                self.cleaner.begin_stroke(self.mapToScene(event.pos()))

    def mouseMoveEvent(self, event):
        pos = self.mapToScene(event.pos())
        self.cleaner.on_cursor(pos)
        if self._pan_last is not None:
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        elif self._painting:
            self.cleaner.continue_stroke(pos)

    def mouseReleaseEvent(self, event):
        if self._pan_last is not None:
            self._pan_last = None
            self.cleaner.update_cursor_shape()
        if self._painting and event.button() == Qt.LeftButton:
            self._painting = False
            self.cleaner.end_stroke()

    def leaveEvent(self, event):
        self.cleaner.brush_cursor.hide()
        super().leaveEvent(event)


class MapCleaner(QMainWindow):

    BRUSHES = [  # (ข้อความปุ่ม, โหมด)
        ('✋ เลื่อน/ดู', None),
        ('⬜ ว่าง', 'free'),
        ('⬛ ผนัง', 'occ'),
        ('▒ unknown', 'unknown'),
    ]

    def __init__(self, map_path: str):
        super().__init__()
        self.md = MapData(map_path)
        self.manual = np.full(self.md.original.shape, -1, np.int16)  # -1 = ไม่ได้ระบาย
        self.undo_stack = []
        self.brush_mode = None
        self.dirty = False
        self._last_scene_pos = None
        self._show_original = False

        self.scene = QGraphicsScene(self)
        self.map_item = self.scene.addPixmap(QPixmap())
        self.map_item.setTransformationMode(Qt.FastTransformation)
        self.scene.setSceneRect(QRectF(0, 0, self.md.width, self.md.height))
        self.brush_cursor = QGraphicsEllipseItem()
        self.brush_cursor.setZValue(10)
        self.brush_cursor.hide()
        self.scene.addItem(self.brush_cursor)

        self._build_ui()
        self.apply_theme(theme.current_name())
        self.recompute()
        self._fitted = False

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        tb = QToolBar('หลัก')
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        def add(text, slot, icon=None, shortcut=None, tip=None):
            act = QAction(text, self)
            if icon is not None:
                act.setIcon(self.style().standardIcon(icon))
            if shortcut:
                act.setShortcut(shortcut)
            act.setToolTip(tip or text)
            act.triggered.connect(slot)
            tb.addAction(act)
            return act

        add('Save As', self.save_as, QStyle.SP_DialogSaveButton, QKeySequence.Save,
            'บันทึกเป็นแผนที่ใหม่ (Ctrl+S) — ไม่เขียนทับแผนที่เดิม')
        tb.addSeparator()
        add('↶ Undo แปรง', self.undo, shortcut=QKeySequence.Undo, tip='ย้อนการระบาย (Ctrl+Z)')
        add('ล้างการระบายทั้งหมด', self.clear_manual)
        tb.addSeparator()
        add('พอดีจอ', self.fit_all, tip='F')
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.theme_action = add('', self.toggle_theme, tip='สลับ Dark / Light')

        self.view = CleanerView(self.scene, self)

        panel = QWidget()
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(10, 10, 10, 10)
        vbox.setSpacing(10)
        vbox.addWidget(self._build_info_card())
        vbox.addWidget(self._build_auto_card())
        vbox.addWidget(self._build_brush_card())
        vbox.addWidget(self._build_view_card())
        vbox.addStretch()
        # แผงขวาเลื่อนขึ้นลงได้ ข้อความจะไม่ถูกบีบซ้อนกันเมื่อหน้าจอเตี้ย
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(420)
        scroll.setMaximumWidth(520)

        splitter = QSplitter()
        splitter.addWidget(self.view)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1060, 440])
        self.setCentralWidget(splitter)

        self.coord_label = QLabel('x = –  y = –')
        self.statusBar().addPermanentWidget(self.coord_label)
        QShortcut(QKeySequence('F'), self, self.fit_all)

        self.resize(1500, 920)
        self.update_title()

    def _slider(self, lo, hi, value, on_change):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        s.valueChanged.connect(on_change)
        return s

    def _build_auto_card(self):
        card, lay = theme.make_card('ลบ noise อัตโนมัติ')
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)

        self.speck_slider = self._slider(0, 30, 3, self.on_settings_changed)
        self.speck_label = QLabel()
        grid.addWidget(QLabel('จุดดำ ≤'), 0, 0)
        grid.addWidget(self.speck_slider, 0, 1)
        grid.addWidget(self.speck_label, 0, 2)

        self.hole_slider = self._slider(0, 30, 3, self.on_settings_changed)
        self.hole_label = QLabel()
        grid.addWidget(QLabel('จุดเทา ≤'), 1, 0)
        grid.addWidget(self.hole_slider, 1, 1)
        grid.addWidget(self.hole_label, 1, 2)
        lay.addLayout(grid)

        self.keep_unknown = QCheckBox('ไม่ลบจุดดำที่ติดพื้นที่ unknown (แนะนำ)')
        self.keep_unknown.setChecked(True)
        self.keep_unknown.setToolTip(
            'จุดดำที่ขอบพื้นที่ที่ยังไม่ได้สแกน มักเป็นเศษผนังจริง\n'
            'ถ้าลบทิ้ง planner ที่ allow_unknown อาจวางเส้นทางทะลุออกไปได้')
        self.keep_unknown.toggled.connect(self.on_settings_changed)
        lay.addWidget(self.keep_unknown)

        chips = QHBoxLayout()
        self.chip_removed = QLabel()
        self.chip_filled = QLabel()
        chips.addWidget(self.chip_removed)
        chips.addWidget(self.chip_filled)
        chips.addStretch()
        lay.addLayout(chips)
        self.stats_hint = theme.muted_label()
        self.stats_hint.setWordWrap(True)
        lay.addWidget(self.stats_hint)
        return card

    def _build_brush_card(self):
        card, lay = theme.make_card('แปรงระบายเอง')
        row = QGridLayout()
        self.brush_group = QButtonGroup(self)
        self.brush_group.setExclusive(True)
        for k, (text, mode) in enumerate(self.BRUSHES):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(mode is None)
            self.brush_group.addButton(btn, k)
            row.addWidget(btn, k // 2, k % 2)
        self.brush_group.buttonClicked[int].connect(self.on_brush_selected)
        lay.addLayout(row)

        size_row = QHBoxLayout()
        self.brush_slider = self._slider(1, 40, 6, self.on_brush_size)
        self.brush_label = QLabel()
        size_row.addWidget(QLabel('ขนาด'))
        size_row.addWidget(self.brush_slider, 1)
        size_row.addWidget(self.brush_label)
        lay.addLayout(size_row)
        self.manual_hint = theme.muted_label()
        self.manual_hint.setWordWrap(True)
        lay.addWidget(self.manual_hint)
        self.on_brush_size(self.brush_slider.value())
        return card

    def _build_view_card(self):
        card, lay = theme.make_card('การแสดงผล')
        self.highlight = QCheckBox('ไฮไลต์จุดที่จะถูกเปลี่ยน')
        self.highlight.setChecked(True)
        self.highlight.toggled.connect(self.render)
        lay.addWidget(self.highlight)
        self.legend = QLabel()
        lay.addWidget(self.legend)
        compare = QPushButton('กดค้างไว้เพื่อดูแผนที่ต้นฉบับ')
        compare.pressed.connect(lambda: self.set_show_original(True))
        compare.released.connect(lambda: self.set_show_original(False))
        lay.addWidget(compare)
        return card

    def _build_info_card(self):
        card, lay = theme.make_card('ไฟล์แผนที่')
        md = self.md
        info = theme.muted_label(
            f'{os.path.basename(md.yaml_path)}  ·  {md.width}×{md.height} px  ·  '
            f'{md.resolution} m/px\n'
            f'บันทึกเป็นไฟล์ใหม่เสมอ แผนที่เดิมจะไม่ถูกแก้')
        info.setWordWrap(True)
        lay.addWidget(info)

        # map_server: occ = (255 - pixel)/255 ถ้า occ < free_thresh → free
        # สี unknown 205 ได้ occ ≈ 0.196 จึงต้องใช้ free_thresh ≤ 0.196 (ค่าเดียวกับ ROS1)
        self.fix_thresh = QCheckBox(f'ใช้ free_thresh: {FIXED_FREE_THRESH} ในไฟล์ใหม่')
        self.fix_thresh.setToolTip('สี unknown (205) จะถูกอ่านเป็น unknown จริง\n'
                                   'แทนที่จะกลายเป็นพื้นที่ว่าง')
        self.fix_thresh.toggled.connect(self.on_fix_thresh)
        self.thresh_warning = QLabel()
        self.thresh_warning.setWordWrap(True)
        if md.gray_read_as_free():
            lay.addWidget(self.thresh_warning)
            lay.addWidget(self.fix_thresh)
        return card

    def on_fix_thresh(self, on: bool):
        self.md.free_thresh = FIXED_FREE_THRESH if on else self.md.file_free_thresh
        self.on_settings_changed()

    def update_title(self):
        mark = '● ' if self.dirty else ''
        self.setWindowTitle(f'{mark}Map Cleaner — {os.path.basename(self.md.yaml_path)}')

    # ── theme ────────────────────────────────────────────────────────────────
    def toggle_theme(self):
        self.apply_theme('light' if theme.current_name() == 'dark' else 'dark')

    def apply_theme(self, name: str):
        theme.apply(QApplication.instance(), name)
        t = theme.current()
        self.theme_action.setText('☀ Light' if name == 'dark' else '☾ Dark')
        pen = QPen(QColor(t['accent']), 1.5)
        pen.setCosmetic(True)
        self.brush_cursor.setPen(pen)
        self.legend.setText(
            f'<span style="color:{t["danger"]}">■</span> จุดดำที่จะถูกลบ   '
            f'<span style="color:{t["ok"]}">■</span> จุดเทาที่จะถูกเติม')
        self.thresh_warning.setText(
            f'<span style="color:{t["warn"]}">⚠ free_thresh {self.md.file_free_thresh} '
            f'ในไฟล์นี้ทำให้ map_server อ่านสีเทา ({self.md.unknown_value}) เป็น'
            '<b>พื้นที่ว่าง</b> ทั้งหมด ไม่มี unknown เลย</span>')
        if hasattr(self, 'auto'):   # ยังไม่ได้ประมวลผลครั้งแรกตอนสร้างหน้าต่าง
            self.update_stats()
            self.render()

    # ── ประมวลผล / วาด ───────────────────────────────────────────────────────
    def on_settings_changed(self, *_):
        self.dirty = True
        self.update_title()
        self.recompute()

    def recompute(self):
        res = self.md.resolution
        self.speck_label.setText(f'{self.speck_slider.value()} px')
        self.hole_label.setText(f'{self.hole_slider.value()} px')
        area = self.speck_slider.value() * res * res
        self.speck_slider.setToolTip(f'ก้อนที่พื้นที่ ≤ {area:.4f} m²')
        self.auto, self.removed, self.filled, self.stats = clean(
            self.md, self.md.original, self.speck_slider.value(),
            self.keep_unknown.isChecked(), self.hole_slider.value())
        self.update_stats()
        self.render()

    def update_stats(self):
        t = theme.current()
        s = self.stats
        self.chip_removed.setText(f'ลบ {s["specks"]:,} ก้อน')
        self.chip_removed.setStyleSheet(theme.chip_style(t['danger']))
        self.chip_filled.setText(f'เติม {s["holes"]:,} ก้อน')
        self.chip_filled.setStyleSheet(theme.chip_style(t['ok']))
        pct = 100.0 * s['speck_px'] / max(1, s['occ_px'])
        self.stats_hint.setText(f'{s["speck_px"]:,} px จาก occupied ทั้งหมด {s["occ_px"]:,} px '
                                f'({pct:.1f}%)  ·  เติม {s["hole_px"]:,} px')
        n_manual = int((self.manual >= 0).sum())
        self.manual_hint.setText(f'ระบายแล้ว {n_manual:,} px' if n_manual
                                 else 'เลือกแปรงแล้วลากซ้ายบนแผนที่ (ลากขวาเพื่อเลื่อน)')

    def result(self) -> np.ndarray:
        """แผนที่สุดท้าย = ผล auto แล้วทับด้วยสิ่งที่ระบายเอง"""
        out = self.auto.copy()
        m = self.manual >= 0
        out[m] = self.manual[m]
        return out

    def render(self, *_):
        t = theme.current()
        if self._show_original:
            occ, free, _ = self.md.classify(self.md.original)
            img = theme.colorize(occ, free)
        else:
            occ, free, _ = self.md.classify(self.result())
            overlays = []
            if self.highlight.isChecked():
                untouched = self.manual < 0
                overlays = [(self.removed & untouched, t['danger']),
                            (self.filled & untouched, t['ok'])]
            img = theme.colorize(occ, free, overlays)
        self.map_item.setPixmap(QPixmap.fromImage(img))

    def set_show_original(self, on: bool):
        self._show_original = on
        self.render()
        if on:
            self.statusBar().showMessage('กำลังแสดงแผนที่ต้นฉบับ')
        else:
            self.statusBar().clearMessage()

    # ── แปรง ────────────────────────────────────────────────────────────────
    def on_brush_selected(self, k: int):
        self.brush_mode = self.BRUSHES[k][1]
        self.update_cursor_shape()

    def update_cursor_shape(self):
        if self.brush_mode is None:
            self.view.viewport().setCursor(Qt.OpenHandCursor)
            self.brush_cursor.hide()
        else:
            self.view.viewport().setCursor(Qt.CrossCursor)

    def on_brush_size(self, size: int):
        self.brush_label.setText(f'{size} px ({size * self.md.resolution:.2f} m)')
        if self._last_scene_pos is not None:
            self.on_cursor(self._last_scene_pos)

    def brush_value(self) -> int:
        return {'free': self.md.free_value, 'occ': self.md.occ_value,
                'unknown': self.md.unknown_value}[self.brush_mode]

    def begin_stroke(self, pos: QPointF):
        self.undo_stack.append(self.manual.copy())
        del self.undo_stack[:-UNDO_LIMIT]
        self._stroke_last = pos
        self._stamp(pos)
        self.render()

    def continue_stroke(self, pos: QPointF):
        # เติมจุดระหว่างตำแหน่งเมาส์ 2 ครั้ง ไม่ให้เส้นขาดเมื่อลากเร็ว
        last = self._stroke_last
        dist = max(abs(pos.x() - last.x()), abs(pos.y() - last.y()))
        steps = max(1, int(dist / max(1.0, self.brush_slider.value() / 3)))
        for k in range(1, steps + 1):
            f = k / steps
            self._stamp(QPointF(last.x() + (pos.x() - last.x()) * f,
                                last.y() + (pos.y() - last.y()) * f))
        self._stroke_last = pos
        self.render()

    def end_stroke(self):
        self.dirty = True
        self.update_title()
        self.update_stats()

    def _stamp(self, pos: QPointF):
        r = max(self.brush_slider.value() / 2.0, 0.5)
        cx, cy = pos.x(), pos.y()
        c0, c1 = max(0, int(cx - r)), min(self.md.width, int(cx + r) + 1)
        r0, r1 = max(0, int(cy - r)), min(self.md.height, int(cy + r) + 1)
        if c0 >= c1 or r0 >= r1:
            return
        yy, xx = np.mgrid[r0:r1, c0:c1]
        mask = (xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2 <= r * r
        self.manual[r0:r1, c0:c1][mask] = self.brush_value()

    def undo(self):
        if not self.undo_stack:
            self.statusBar().showMessage('ไม่มีการระบายให้ undo', 3000)
            return
        self.manual = self.undo_stack.pop()
        self.dirty = True
        self.update_title()
        self.update_stats()
        self.render()

    def clear_manual(self):
        if not (self.manual >= 0).any():
            return
        self.undo_stack.append(self.manual.copy())
        self.manual[:] = -1
        self.dirty = True
        self.update_title()
        self.update_stats()
        self.render()

    def on_cursor(self, pos: QPointF):
        self._last_scene_pos = pos
        x = self.md.meta['origin'][0] + pos.x() * self.md.resolution
        y = self.md.meta['origin'][1] + (self.md.height - pos.y()) * self.md.resolution
        self.coord_label.setText(f'x = {x:.2f}   y = {y:.2f}')
        if self.brush_mode is not None:
            r = max(self.brush_slider.value() / 2.0, 0.5)
            self.brush_cursor.setRect(pos.x() - r, pos.y() - r, 2 * r, 2 * r)
            self.brush_cursor.show()

    def fit_all(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def showEvent(self, event):
        # ต้องรอให้หน้าต่างแสดงก่อน view ถึงจะรู้ขนาดจริง แล้วค่อยซูมให้พอดี
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            QTimer.singleShot(0, self.fit_all)

    # ── บันทึก ──────────────────────────────────────────────────────────────
    def save_as(self) -> bool:
        md = self.md
        stem = os.path.splitext(os.path.basename(md.yaml_path))[0]
        default = os.path.join(os.path.dirname(md.yaml_path), f'{stem}_clean.yaml')
        path, _ = QFileDialog.getSaveFileName(self, 'บันทึกแผนที่ใหม่', default,
                                              'Map YAML (*.yaml)')
        if not path:
            return False
        if not path.endswith('.yaml'):
            path += '.yaml'
        try:
            yaml_out, pgm_out = save_map(md, self.result(), path)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, 'บันทึกไม่สำเร็จ', str(e))
            return False
        self.dirty = False
        self.update_title()
        QMessageBox.information(
            self, 'บันทึกแล้ว',
            f'บันทึกแผนที่ใหม่แล้ว:\n{yaml_out}\n{pgm_out}\n\n'
            'อย่าลืมเปลี่ยน path แผนที่ใน launch file ให้ชี้ไฟล์ใหม่\n'
            'แล้ว colcon build my_robot_bringup อีกครั้ง')
        return True

    def closeEvent(self, event):
        if not self.dirty:
            event.accept()
            return
        answer = QMessageBox.question(
            self, 'ยังไม่ได้บันทึก', 'แผนที่ที่ล้างแล้วยังไม่ได้บันทึก ต้องการบันทึกก่อนปิดไหม?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Save and self.save_as():
            event.accept()
        elif answer == QMessageBox.Discard:
            event.accept()
        else:
            event.ignore()


def save_map(md: MapData, arr: np.ndarray, yaml_out: str):
    """เขียน .pgm + .yaml คู่กัน ห้ามทับไฟล์แผนที่ต้นฉบับ"""
    yaml_out = os.path.abspath(yaml_out)
    pgm_out = os.path.splitext(yaml_out)[0] + '.pgm'
    if yaml_out == md.yaml_path or pgm_out == md.image_path:
        raise ValueError('ห้ามบันทึกทับแผนที่ต้นฉบับ กรุณาตั้งชื่อไฟล์ใหม่')
    Image.fromarray(arr.astype(np.uint8), 'L').save(pgm_out)
    meta = dict(md.meta)
    meta['image'] = os.path.basename(pgm_out)
    meta['free_thresh'] = md.free_thresh
    with open(yaml_out, 'w') as f:
        # default_flow_style=None → origin เขียนเป็น [x, y, yaw] บรรทัดเดียวแบบ map_saver
        yaml.dump(meta, f, default_flow_style=None, sort_keys=False)
    return yaml_out, pgm_out


def main():
    parser = argparse.ArgumentParser(description='ลบ noise ในแผนที่ occupancy grid')
    parser.add_argument('--map', help='ไฟล์ map .yaml (ไม่ใส่ = เลือกจากหน้าต่าง)')
    args, _ = parser.parse_known_args()

    app = QApplication(sys.argv[:1])
    theme.apply(app, theme.saved_theme())
    path = os.path.expanduser(args.map) if args.map else None
    if not path:
        path, _ = QFileDialog.getOpenFileName(
            None, 'เลือกไฟล์แผนที่', os.path.expanduser('~/ros2_ws/src/my_robot_bringup/map'),
            'Map YAML (*.yaml)')
        if not path:
            sys.exit(0)
    try:
        win = MapCleaner(path)
    except (OSError, KeyError, ValueError, yaml.YAMLError) as e:
        print(f'[ERROR] {e}')
        sys.exit(1)
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
