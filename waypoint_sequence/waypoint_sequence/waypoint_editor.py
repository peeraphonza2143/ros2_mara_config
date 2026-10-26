"""
waypoint_editor.py — GUI แก้ไข waypoint เฉพาะช่วงบนแผนที่ (PyQt5)

ใช้งาน:
  ros2 run waypoint_sequence waypoint_editor \\
    --map ~/ros2_ws/src/my_robot_bringup/map/nidec_Map.yaml \\
    --waypoints ~/ros2_ws/src/my_robot_bringup/config/agv_recorded_xxx.yaml \\
    --range 180 200

เมาส์: ลากซ้ายที่จุด = ย้ายจุด | ลากซ้ายที่ว่าง = เลือกหลายจุด
       ลากขวา/กลาง = เลื่อนแผนที่ | scroll = zoom
คีย์ลัด: Ctrl+Z = undo | Delete = ลบจุดที่เลือก | Ctrl+S = save | F = พอดีช่วง
"""

import argparse
import copy
import math
import os
import shutil
import sys
from datetime import datetime

import numpy as np
import yaml
from PyQt5.QtCore import QItemSelectionModel, QPointF, QRectF, QSize, Qt, QTimer
from PyQt5.QtGui import (QBrush, QColor, QFont, QImage, QKeySequence, QPainter,
                         QPainterPath, QPen, QPixmap, QPolygonF)
from PyQt5.QtWidgets import (QAbstractItemView, QAction, QApplication, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGraphicsEllipseItem, QGraphicsItem,
                             QGridLayout,
                             QGraphicsLineItem, QGraphicsScene, QGraphicsSimpleTextItem,
                             QGraphicsView, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                             QMessageBox, QPushButton, QShortcut, QSizePolicy, QSpinBox,
                             QSplitter, QStyle, QTableWidget, QTableWidgetItem, QToolBar,
                             QVBoxLayout, QWidget)

from waypoint_sequence import ui_theme as theme


# ── ค่าปรับแต่ง (ให้ตรงกับ nav2_params_agv.yaml) ──────────────────────────────
FOOTPRINT = [(0.33, -0.29), (0.33, 0.29), (-0.33, 0.29), (-0.33, -0.29)]
DANGER_CLEARANCE = math.hypot(0.33, 0.29)   # ≈0.44 m: กลางรถ → มุม footprint
WARN_CLEARANCE = 0.55                       # m: inflation_radius
CLEARANCE_SEARCH = 1.0                      # m: ค้นหาผนังในรัศมีนี้เท่านั้น
UNDO_LIMIT = 50
COMPACT_BELOW = (1280, 900)   # จอเล็กกว่านี้ (กว้าง หรือ สูง) → ใช้โหมด compact อัตโนมัติ
# ─────────────────────────────────────────────────────────────────────────────


def clearance_level(c: float) -> str:
    if c < DANGER_CLEARANCE:
        return 'danger'
    if c < WARN_CLEARANCE:
        return 'warn'
    return 'ok'


def format_clearance(c: float) -> str:
    return f'>{CLEARANCE_SEARCH:.2f}' if math.isinf(c) else f'{c:.2f}'


def load_waypoints(path: str) -> list:
    with open(path) as f:
        data = yaml.safe_load(f)
    return [{'name': str(wp['name']), 'x': float(wp['x']),
             'y': float(wp['y']), 'yaw': float(wp['yaw'])}
            for wp in data['waypoints']]


class OccupancyMap:
    """อ่าน map .yaml + .pgm และแปลงพิกัด map (เมตร) ↔ scene (pixel)"""

    def __init__(self, yaml_path: str):
        with open(yaml_path) as f:
            meta = yaml.safe_load(f)
        img_path = meta['image']
        if not os.path.isabs(img_path):
            img_path = os.path.join(os.path.dirname(os.path.abspath(yaml_path)), img_path)

        self.resolution = float(meta['resolution'])
        self.origin_x = float(meta['origin'][0])
        self.origin_y = float(meta['origin'][1])

        image = QImage(img_path)
        if image.isNull():
            raise RuntimeError(f'เปิดไฟล์ภาพแผนที่ไม่ได้: {img_path}')
        self.width = image.width()
        self.height = image.height()

        # pixel → occupied/free (ใช้กฎเดียวกับ map_server: ยิ่งดำยิ่ง occupied เมื่อ negate=0)
        gray = image.convertToFormat(QImage.Format_Grayscale8)
        ptr = gray.constBits()
        ptr.setsize(gray.bytesPerLine() * self.height)
        arr = np.frombuffer(ptr, np.uint8).reshape(self.height, gray.bytesPerLine())
        arr = arr[:, :self.width].astype(np.float32)
        occ_prob = arr / 255.0 if int(meta.get('negate', 0)) else (255.0 - arr) / 255.0
        self.occupied = occ_prob > float(meta.get('occupied_thresh', 0.65))
        self.free = occ_prob < float(meta.get('free_thresh', 0.25))

    def pixmap(self) -> QPixmap:
        return QPixmap.fromImage(theme.colorize(self.occupied, self.free))

    def world_to_scene(self, x: float, y: float):
        # แกน y ของภาพชี้ลง แต่แกน y ของ map ชี้ขึ้น จึงต้องกลับด้าน
        col = (x - self.origin_x) / self.resolution
        row = self.height - (y - self.origin_y) / self.resolution
        return col, row

    def scene_to_world(self, col: float, row: float):
        x = self.origin_x + col * self.resolution
        y = self.origin_y + (self.height - row) * self.resolution
        return x, y

    def clearance(self, x: float, y: float) -> float:
        """ระยะ (m) ถึง cell ที่ occupied ใกล้สุด; inf ถ้าไม่มีในรัศมี CLEARANCE_SEARCH"""
        col, row = self.world_to_scene(x, y)
        if not (0 <= col < self.width and 0 <= row < self.height):
            return 0.0
        r = int(math.ceil(CLEARANCE_SEARCH / self.resolution))
        c0, c1 = max(0, int(col) - r), min(self.width, int(col) + r + 1)
        r0, r1 = max(0, int(row) - r), min(self.height, int(row) + r + 1)
        ys, xs = np.nonzero(self.occupied[r0:r1, c0:c1])
        if xs.size == 0:
            return math.inf
        d = np.hypot(xs + c0 + 0.5 - col, ys + r0 + 0.5 - row).min() * self.resolution
        return float(d) if d <= CLEARANCE_SEARCH else math.inf


class WaypointItem(QGraphicsEllipseItem):
    """จุด waypoint 1 จุด ขนาดคงที่บนจอไม่ว่าจะ zoom เท่าไร"""

    RADIUS = 6

    def __init__(self, editor, index: int, editable: bool):
        r = self.RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.editor = editor
        self.index = index
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(10)

        self.arrow = QGraphicsLineItem(0, 0, r * 3, 0, self)
        self.arrow.setZValue(-1)
        self.label = QGraphicsSimpleTextItem('', self)
        self.label.setPos(r + 3, -r - 14)
        self.label.setFont(QFont('Sans', 8, QFont.DemiBold))

        if editable:
            self.setFlag(QGraphicsItem.ItemIsMovable)
            self.setFlag(QGraphicsItem.ItemIsSelectable)
            self.setAcceptHoverEvents(True)
            self.setCursor(Qt.OpenHandCursor)
            self.setZValue(20)
        else:
            self.setOpacity(0.4)
            self.label.hide()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and not self.editor.syncing:
            self.editor.on_point_dragged(self.index, self.pos())
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):
        self.setScale(1.5)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setScale(1.0)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        self.editor.begin_drag()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.editor.end_drag()


class MapView(QGraphicsView):
    """scroll = zoom, ลากปุ่มขวา/กลาง = pan, ลากซ้ายที่ว่าง = rubber band"""

    def __init__(self, scene, editor):
        super().__init__(scene)
        self.editor = editor
        self._pan_last = None
        self.setRenderHint(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setMouseTracking(True)

    def wheelEvent(self, event):
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            self._pan_last = event.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_last is not None:
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            return
        self.editor.show_cursor(self.mapToScene(event.pos()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan_last is not None and event.button() in (Qt.RightButton, Qt.MiddleButton):
            self._pan_last = None
            self.viewport().unsetCursor()
            return
        super().mouseReleaseEvent(event)


class WaypointEditor(QMainWindow):

    COLS = ['name', 'x (m)', 'y (m)', 'yaw (°)', 'ห่าง (m)']
    COLS_COMPACT = ['name', 'x', 'y', 'yaw°', 'ห่าง']
    EDITABLE = {1: 'x', 2: 'y', 3: 'yaw'}

    def __init__(self, map_path: str, wp_path: str, wp_range):
        super().__init__()
        self.compact = theme.is_compact()
        self.map = OccupancyMap(map_path)
        self.map_path = os.path.abspath(map_path)
        self.wp_path = os.path.abspath(wp_path)
        self.wps = load_waypoints(self.wp_path)
        self.set_range(wp_range)

        self.items = []
        self.undo_stack = []
        self.dirty = False
        self.syncing = False       # กัน signal วนกลับ ตอนอัปเดตด้วย code
        self._drag_snapshot = None
        self._last_spin_edit = None  # รวมการกด spinbox ต่อเนื่องเป็น undo ครั้งเดียว

        self.scene = QGraphicsScene(self)
        self.map_item = self.scene.addPixmap(self.map.pixmap())
        self.map_item.setTransformationMode(Qt.FastTransformation)
        self.scene.setSceneRect(QRectF(0, 0, self.map.width, self.map.height))

        self.path_all = self.scene.addPath(QPainterPath())
        self.path_range = self.scene.addPath(QPainterPath())
        self.path_all.setZValue(1)
        self.path_range.setZValue(2)
        self.footprint_item = self.scene.addPolygon(QPolygonF())
        self.footprint_item.setZValue(5)
        self.footprint_item.hide()
        self.scene.selectionChanged.connect(self.on_scene_selection)

        self._build_ui()
        self.apply_theme(theme.current_name())
        self.rebuild()
        self._fitted = False

    def showEvent(self, event):
        # ต้องรอให้หน้าต่างแสดงก่อน view ถึงจะรู้ขนาดจริง แล้วค่อยซูมให้พอดี
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            QTimer.singleShot(0, self.fit_range)

    def set_range(self, wp_range):
        """ช่วงที่แก้ไขได้ เก็บเป็น index 0-based (inclusive)"""
        n = len(self.wps)
        start, end = wp_range if wp_range else (1, n)
        self.range_start = min(max(min(start, end), 1), n) - 1
        self.range_end = min(max(max(start, end), 1), n) - 1

    # ── UI ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _cosmetic_pen(color, width):
        pen = QPen(color, width)
        pen.setCosmetic(True)   # ความหนาเส้นคงที่บนจอ ไม่ขึ้นกับ zoom
        return pen

    def _build_ui(self):
        self._build_toolbar()
        self.view = MapView(self.scene, self)

        panel = QWidget()
        if self.compact:
            panel.setMinimumWidth(320)
            panel.setMaximumWidth(380)
        else:
            panel.setMinimumWidth(430)
            panel.setMaximumWidth(560)
        vbox = QVBoxLayout(panel)
        gap = 6 if self.compact else 10
        vbox.setContentsMargins(gap, gap, gap, gap)
        vbox.setSpacing(gap)
        vbox.addWidget(self._build_range_card())
        vbox.addWidget(self._build_risk_card())
        vbox.addWidget(self._build_point_card())
        vbox.addWidget(self._build_table_card(), 1)

        splitter = QSplitter()
        splitter.addWidget(self.view)
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([680, 340] if self.compact else [1050, 450])
        self.setCentralWidget(splitter)

        self.coord_label = QLabel('x = –  y = –')
        self.legend_label = QLabel()
        self.statusBar().addPermanentWidget(self.legend_label)
        self.statusBar().addPermanentWidget(self.coord_label)

        QShortcut(QKeySequence.Delete, self, self.delete_selected)
        QShortcut(QKeySequence('F'), self, self.fit_range)

        # ขนาดเริ่มต้นไม่เกินพื้นที่จอที่ใช้ได้จริง (หักแถบเมนู/taskbar แล้ว)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1500, int(avail.width() * 0.95)), min(920, int(avail.height() * 0.95)))
        self.update_title()

    def _build_toolbar(self):
        tb = QToolBar('หลัก')
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        if self.compact:
            tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)
        style = self.style()

        def add(text, slot, icon=None, shortcut=None, tip=None):
            # text เป็น (ปกติ, compact) ได้ → โหมดจอเล็กใช้ข้อความสั้น คำอธิบายเต็มอยู่ใน tooltip
            if isinstance(text, tuple):
                text = text[1] if self.compact else text[0]
            act = QAction(text, self)
            if icon is not None:
                act.setIcon(style.standardIcon(icon))
            if shortcut:
                act.setShortcut(shortcut)
            act.setToolTip(tip or text)
            act.triggered.connect(slot)
            tb.addAction(act)
            return act

        add('เปิด', self.open_waypoints, QStyle.SP_DialogOpenButton, tip='เปิดไฟล์ waypoint อื่น')
        add('Save', self.save, QStyle.SP_DialogSaveButton, QKeySequence.Save,
            'บันทึกทับไฟล์เดิม (Ctrl+S) ไฟล์เก่าจะถูกเก็บเป็น .bak')
        add('Save As', self.save_as, tip='บันทึกเป็นไฟล์ใหม่')
        tb.addSeparator()
        add('↶ Undo', self.undo, shortcut=QKeySequence.Undo, tip='ย้อนกลับ (Ctrl+Z)')
        add(('＋ แทรกจุด', '＋ แทรก'), self.insert_after,
            tip='แทรกจุดใหม่ระหว่างจุดที่เลือกกับจุดถัดไป')
        add(('✕ ลบจุด', '✕ ลบ'), self.delete_selected, tip='ลบจุดที่เลือก (Delete)')
        tb.addSeparator()
        add('≈ Smooth', self.smooth_range, tip='ทำเส้นทางในช่วงให้เรียบ แล้วคำนวณ yaw ใหม่')
        if not self.compact:
            tb.addWidget(QLabel('รอบ'))
        self.spin_smooth = QSpinBox()
        if self.compact:
            self.spin_smooth.setPrefix('×')
        self.spin_smooth.setRange(1, 10)
        self.spin_smooth.setValue(2)
        self.spin_smooth.setToolTip('จำนวนรอบ smooth: ยิ่งมากเส้นยิ่งเรียบ '
                                    'แต่โค้งจะถูกตัดมุมมากขึ้น')
        tb.addWidget(self.spin_smooth)
        add(('↻ คำนวณ yaw', '↻ yaw'), self.recompute_yaw_range,
            tip='คำนวณ yaw ใหม่: yaw = ทิศไปยังจุดถัดไป')
        tb.addSeparator()
        add(('พอดีช่วง', 'พอดี'), self.fit_range, tip='ซูมให้เห็นช่วงที่แก้ไข (F)')
        add(('ทั้งแผนที่', 'ทั้งหมด'), self.fit_all, tip='ซูมให้เห็นทั้งแผนที่')

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.theme_action = add('', self.toggle_theme, tip='สลับ Dark / Light')

    def _build_range_card(self):
        card, lay = theme.make_card('ช่วงที่แก้ไข')
        row = QHBoxLayout()
        self.spin_start = QSpinBox()
        self.spin_end = QSpinBox()
        for spin in (self.spin_start, self.spin_end):
            spin.setPrefix('P')
            spin.setMinimumWidth(72 if self.compact else 90)
        btn = QPushButton('แสดง' if self.compact else 'แสดงช่วง')
        btn.setProperty('primary', True)
        btn.clicked.connect(self.apply_range)
        row.addWidget(self.spin_start)
        row.addWidget(QLabel('ถึง'))
        row.addWidget(self.spin_end)
        row.addStretch()
        row.addWidget(btn)
        lay.addLayout(row)
        self.range_info = theme.muted_label()
        lay.addWidget(self.range_info)
        self.range_info.setVisible(not self.compact)   # compact: ดูได้จาก tooltip ของการ์ด
        self.range_card = card
        return card

    def _build_risk_card(self):
        card, lay = theme.make_card('ระยะห่างจากผนัง')
        row = QHBoxLayout()
        self.chip_danger = QLabel()
        self.chip_warn = QLabel()
        self.chip_ok = QLabel()
        rule = (f'แดง < {DANGER_CLEARANCE:.2f} m (มุมรถชน)  ·  ส้ม < {WARN_CLEARANCE:.2f} m '
                '(อยู่ใน inflation)')
        for chip in (self.chip_danger, self.chip_warn, self.chip_ok):
            chip.setToolTip(rule)
            row.addWidget(chip)
        row.addStretch()
        lay.addLayout(row)
        if self.compact:
            # ปุ่มกระโดดเป็นลูกศรเล็กอยู่แถวเดียวกับ chip ประหยัดความสูง
            nav = row
            prev_btn = QPushButton('◀')
            next_btn = QPushButton('▶')
            prev_btn.setToolTip('จุดเสี่ยงก่อนหน้า')
            next_btn.setToolTip('จุดเสี่ยงถัดไป')
            for btn in (prev_btn, next_btn):
                btn.setFixedWidth(28)
        else:
            note = theme.muted_label(rule)
            note.setWordWrap(True)
            lay.addWidget(note)
            nav = QHBoxLayout()
            lay.addLayout(nav)
            prev_btn = QPushButton('◀ จุดเสี่ยงก่อนหน้า')
            next_btn = QPushButton('จุดเสี่ยงถัดไป ▶')
        prev_btn.clicked.connect(lambda: self.goto_risk(-1))
        next_btn.clicked.connect(lambda: self.goto_risk(+1))
        nav.addWidget(prev_btn)
        nav.addWidget(next_btn)
        return card

    def _build_point_card(self):
        card, lay = theme.make_card('จุดที่เลือก')
        head = QHBoxLayout()
        self.point_name = QLabel('—')
        self.point_name.setObjectName('bigName')
        self.point_chip = QLabel()
        head.addWidget(self.point_name)
        head.addStretch()
        head.addWidget(self.point_chip)
        lay.addLayout(head)
        self.point_hint = theme.muted_label()
        lay.addWidget(self.point_hint)
        self.point_hint.setVisible(not self.compact)

        self.spin_x = QDoubleSpinBox()
        self.spin_y = QDoubleSpinBox()
        self.spin_yaw = QDoubleSpinBox()
        for spin in (self.spin_x, self.spin_y):
            spin.setRange(-10000, 10000)
            spin.setDecimals(3)
            spin.setSingleStep(0.02)
        self.spin_yaw.setRange(-180, 180)
        self.spin_yaw.setDecimals(1)
        self.spin_yaw.setSingleStep(2)
        self.spin_yaw.setWrapping(True)
        self.spin_x.valueChanged.connect(lambda v: self.on_spin_edited('x', v))
        self.spin_y.valueChanged.connect(lambda v: self.on_spin_edited('y', v))
        self.spin_yaw.valueChanged.connect(lambda v: self.on_spin_edited('yaw', math.radians(v)))
        if self.compact:
            # x / y / yaw วางเรียงในแถวเดียว ชื่อช่องอยู่ด้านบน
            grid = QGridLayout()
            grid.setHorizontalSpacing(4)
            grid.setVerticalSpacing(1)
            for col, (label, spin) in enumerate((('x (m)', self.spin_x), ('y (m)', self.spin_y),
                                                 ('yaw (°)', self.spin_yaw))):
                grid.addWidget(theme.muted_label(label), 0, col)
                grid.addWidget(spin, 1, col)
                grid.setColumnStretch(col, 1)
            lay.addLayout(grid)
        else:
            for spin in (self.spin_x, self.spin_y):
                spin.setSuffix(' m')
            self.spin_yaw.setSuffix(' °')
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignRight)
            form.addRow('x', self.spin_x)
            form.addRow('y', self.spin_y)
            form.addRow('yaw', self.spin_yaw)
            lay.addLayout(form)
        return card

    def _build_table_card(self):
        card, lay = theme.make_card('Waypoints ในช่วง')
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS_COMPACT if self.compact else self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22 if self.compact else 26)
        self.table.setMinimumHeight(120)   # อย่างน้อย ~4 แถว
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.table.itemChanged.connect(self.on_table_edited)
        self.table.itemSelectionChanged.connect(self.on_table_selection)
        lay.addWidget(self.table)
        return card

    def update_title(self):
        mark = '● ' if self.dirty else ''
        self.setWindowTitle(f'{mark}Waypoint Editor — {os.path.basename(self.wp_path)} '
                            f'@ {os.path.basename(self.map_path)}')

    # ── theme ────────────────────────────────────────────────────────────────
    def toggle_theme(self):
        self.apply_theme('light' if theme.current_name() == 'dark' else 'dark')

    def apply_theme(self, name: str):
        theme.apply(QApplication.instance(), name)
        t = theme.current()
        if self.compact:
            self.theme_action.setText('☀' if name == 'dark' else '☾')
        else:
            self.theme_action.setText('☀ Light' if name == 'dark' else '☾ Dark')
        self.map_item.setPixmap(self.map.pixmap())
        self.path_all.setPen(self._cosmetic_pen(QColor(t['path_dim']), 1))
        self.path_range.setPen(self._cosmetic_pen(QColor(t['path']), 2.5))
        fp_color = QColor(t['footprint'])
        self.footprint_item.setPen(self._cosmetic_pen(fp_color, 2))
        fp_color.setAlpha(40)
        self.footprint_item.setBrush(QBrush(fp_color))
        dot = '<span style="color:{}">●</span>'
        if self.compact:
            self.legend_label.setText(
                f'{dot.format(t["danger"])} ชน {dot.format(t["warn"])} ใกล้ '
                f'{dot.format(t["ok"])} ปลอดภัย')
        else:
            self.legend_label.setText(
                f'{dot.format(t["danger"])} ชนผนัง  {dot.format(t["warn"])} ใน inflation  '
                f'{dot.format(t["ok"])} ปลอดภัย  '
                f'<span style="color:{t["footprint"]}">▭</span> footprint  ·  '
                'ลากขวา = เลื่อน · scroll = zoom')
        if self.items:
            for i in range(len(self.items)):
                self.style_point(i)
            self.fill_table()
            self.update_risk()
            self.update_point_card()

    # ── ช่วยเหลือ ───────────────────────────────────────────────────────────
    def in_range(self, i: int) -> bool:
        return self.range_start <= i <= self.range_end

    def range_indices(self):
        return range(self.range_start, self.range_end + 1)

    def selected_indices(self) -> list:
        rows = self.table.selectionModel().selectedRows()
        return sorted(self.range_start + r.row() for r in rows)

    def clearance_of(self, i: int) -> float:
        return self.map.clearance(self.wps[i]['x'], self.wps[i]['y'])

    def snapshot(self):
        return copy.deepcopy(self.wps), self.range_start, self.range_end

    def push_undo(self, snap=None):
        self._last_spin_edit = None
        self.undo_stack.append(snap if snap is not None else self.snapshot())
        del self.undo_stack[:-UNDO_LIMIT]

    def mark_dirty(self):
        self.dirty = True
        self.update_title()

    # ── วาด scene ────────────────────────────────────────────────────────────
    def rebuild(self):
        """สร้าง item ใหม่ทั้งหมด ใช้เมื่อจำนวนจุดหรือช่วงเปลี่ยน"""
        self.syncing = True
        self.table.clearSelection()   # index เดิมใช้ไม่ได้แล้วหลังเพิ่ม/ลบจุด
        for it in self.items:
            self.scene.removeItem(it)
        self.items = []
        for i in range(len(self.wps)):
            it = WaypointItem(self, i, self.in_range(i))
            self.scene.addItem(it)
            self.items.append(it)
            self.update_point(i)
        self.syncing = False

        n = len(self.wps)
        for spin, value in ((self.spin_start, self.range_start), (self.spin_end, self.range_end)):
            spin.blockSignals(True)
            spin.setRange(1, n)
            spin.setValue(value + 1)
            spin.blockSignals(False)
        info = (f'{self.wps[self.range_start]["name"]} – {self.wps[self.range_end]["name"]}  ·  '
                f'{self.range_end - self.range_start + 1} จุด จากทั้งหมด {n} จุด')
        self.range_info.setText(info)
        self.range_card.setToolTip(info)

        self.update_paths()
        self.fill_table()
        self.update_footprint()
        self.update_risk()
        self.update_point_card()

    def refresh(self):
        """อัปเดตตำแหน่ง/สีโดยไม่สร้าง item ใหม่ (selection ยังอยู่)"""
        for i in range(len(self.wps)):
            self.update_point(i)
        self.update_paths()
        self.fill_table()
        self.update_footprint()
        self.update_risk()
        self.update_point_card()

    def update_point(self, i: int):
        wp = self.wps[i]
        it = self.items[i]
        was_syncing = self.syncing
        self.syncing = True
        it.setPos(QPointF(*self.map.world_to_scene(wp['x'], wp['y'])))
        self.syncing = was_syncing
        # scene มีแกน y ชี้ลง → มุมบนจอ = -yaw
        it.arrow.setRotation(-math.degrees(wp['yaw']))
        it.label.setText(wp['name'])
        self.style_point(i)

    def style_point(self, i: int):
        it = self.items[i]
        t = theme.current()
        if self.in_range(i):
            c = self.clearance_of(i)
            color = QColor(t[clearance_level(c)])
            it.setToolTip(f'{self.wps[i]["name"]}  ·  ห่างผนัง {format_clearance(c)} m')
        else:
            color = QColor(t['path_dim'])
        it.setBrush(QBrush(color))
        if it.isSelected():
            it.setPen(QPen(QColor(t['select']), 3))
        else:
            it.setPen(QPen(QColor('#ffffff'), 1.5))
        it.arrow.setPen(QPen(color.darker(140), 2.5, Qt.SolidLine, Qt.RoundCap))
        it.label.setBrush(QBrush(QColor(t['label'])))

    def update_paths(self):
        def build(indices):
            path = QPainterPath()
            for k, i in enumerate(indices):
                p = QPointF(*self.map.world_to_scene(self.wps[i]['x'], self.wps[i]['y']))
                path.moveTo(p) if k == 0 else path.lineTo(p)
            return path

        n = len(self.wps)
        self.path_all.setPath(build(range(n)))
        # ต่อเส้นไปจุดข้างเคียงนอกช่วง 1 จุด ให้เห็นรอยต่อของเส้นทาง
        self.path_range.setPath(build(range(max(0, self.range_start - 1),
                                            min(n, self.range_end + 2))))

    def update_footprint(self):
        idx = self.selected_indices()
        if not idx:
            self.footprint_item.hide()
            return
        wp = self.wps[idx[0]]
        c, s = math.cos(wp['yaw']), math.sin(wp['yaw'])
        poly = QPolygonF()
        for fx, fy in FOOTPRINT:
            x = wp['x'] + fx * c - fy * s
            y = wp['y'] + fx * s + fy * c
            poly.append(QPointF(*self.map.world_to_scene(x, y)))
        self.footprint_item.setPolygon(poly)
        self.footprint_item.show()

    # ── การ์ดสรุปความเสี่ยง / จุดที่เลือก ────────────────────────────────────
    def risk_indices(self) -> list:
        return [i for i in self.range_indices()
                if clearance_level(self.clearance_of(i)) != 'ok']

    def update_risk(self):
        t = theme.current()
        counts = {'danger': 0, 'warn': 0, 'ok': 0}
        for i in self.range_indices():
            counts[clearance_level(self.clearance_of(i))] += 1
        for chip, key, text in ((self.chip_danger, 'danger', 'ชน'),
                                (self.chip_warn, 'warn', 'ใกล้'),
                                (self.chip_ok, 'ok', 'ปลอดภัย')):
            chip.setText(f'{counts[key]}  {text}')
            chip.setStyleSheet(theme.chip_style(t[key]))

    def goto_risk(self, direction: int):
        risky = self.risk_indices()
        if not risky:
            self.statusBar().showMessage('ไม่มีจุดเสี่ยงในช่วงนี้', 3000)
            return
        idx = self.selected_indices()
        cur = idx[0] if idx else (self.range_start - 1 if direction > 0 else self.range_end + 1)
        if direction > 0:
            target = next((i for i in risky if i > cur), risky[0])
        else:
            target = next((i for i in reversed(risky) if i < cur), risky[-1])
        self.select_rows([target])
        self.view.centerOn(self.items[target])
        self.table.scrollToItem(self.table.item(target - self.range_start, 0))

    def update_point_card(self):
        idx = self.selected_indices()
        spins = (self.spin_x, self.spin_y, self.spin_yaw)
        for spin in spins:
            spin.setEnabled(bool(idx))
            spin.blockSignals(True)
        if not idx:
            self.point_name.setText('—')
            self.point_chip.setText('คลิกจุดเพื่อแก้ไข' if self.compact else '')
            self.point_chip.setStyleSheet(f'color: {theme.current()["muted"]};')
            self.point_hint.setText('คลิกจุดบนแผนที่หรือในตารางเพื่อแก้ไข')
            for spin in spins:
                spin.setValue(0)
        else:
            i = idx[0]
            wp = self.wps[i]
            c = self.clearance_of(i)
            self.point_name.setText(wp['name'])
            self.point_chip.setText(f'ห่างผนัง {format_clearance(c)} m')
            self.point_chip.setStyleSheet(theme.chip_style(theme.current()[clearance_level(c)]))
            extra = f'  ·  เลือกอยู่ {len(idx)} จุด (แก้ค่าได้ทีละจุดแรก)' if len(idx) > 1 else ''
            self.point_hint.setText(f'ลำดับที่ {i + 1}{extra}')
            self.spin_x.setValue(wp['x'])
            self.spin_y.setValue(wp['y'])
            self.spin_yaw.setValue(math.degrees(wp['yaw']))
        for spin in spins:
            spin.blockSignals(False)

    def on_spin_edited(self, key: str, value: float):
        idx = self.selected_indices()
        if not idx:
            return
        i = idx[0]
        # กดลูกศร spinbox ซ้ำๆ ที่ช่องเดิม → undo ครั้งเดียวย้อนได้ทั้งหมด
        if self._last_spin_edit != (i, key):
            self.push_undo()
            self._last_spin_edit = (i, key)
        self.wps[i][key] = round(value, 4)
        self.update_point(i)
        self.update_table_row(i)
        self.update_paths()
        self.update_footprint()
        self.update_risk()
        c = self.clearance_of(i)
        self.point_chip.setText(f'ห่างผนัง {format_clearance(c)} m')
        self.point_chip.setStyleSheet(theme.chip_style(theme.current()[clearance_level(c)]))
        self.mark_dirty()

    # ── ตาราง ────────────────────────────────────────────────────────────────
    def fill_table(self):
        selected = self.selected_indices()
        self.table.blockSignals(True)
        self.table.setRowCount(self.range_end - self.range_start + 1)
        for i in self.range_indices():
            self.update_table_row(i)
        self.table.blockSignals(False)
        self.select_rows(selected)

    def update_table_row(self, i: int):
        row = i - self.range_start
        wp = self.wps[i]
        c = self.clearance_of(i)
        values = [wp['name'], *self.cell_texts(wp), format_clearance(c)]
        blocked = self.table.blockSignals(True)
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            if col not in self.EDITABLE:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if col == 4:
                item.setForeground(QColor(theme.current()[clearance_level(c)]))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.table.setItem(row, col, item)
        self.table.blockSignals(blocked)

    def select_rows(self, indices):
        self.syncing = True
        self.table.clearSelection()
        model = self.table.selectionModel()
        for i in indices:
            if self.in_range(i):
                index = self.table.model().index(i - self.range_start, 0)
                model.select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.scene.clearSelection()
        for i in indices:
            if self.in_range(i):
                self.items[i].setSelected(True)
        self.syncing = False
        for i in range(len(self.items)):
            self.style_point(i)
        self.update_footprint()
        self.update_point_card()

    @staticmethod
    def cell_texts(wp):
        """ข้อความที่แสดงในช่อง x, y, yaw ของตาราง (yaw แสดงเป็นองศา)"""
        return f'{wp["x"]:.3f}', f'{wp["y"]:.3f}', f'{math.degrees(wp["yaw"]):.1f}'

    def on_table_edited(self, item):
        i = self.range_start + item.row()
        key = self.EDITABLE.get(item.column())
        if key is None:
            return
        # กด Enter โดยไม่ได้เปลี่ยนค่า → ไม่ต้องทำอะไร (กันค่าถูกปัดเศษจากที่แสดง 3 ตำแหน่ง)
        if item.text() == self.cell_texts(self.wps[i])[item.column() - 1]:
            return
        try:
            value = float(item.text())
        except ValueError:
            self.update_table_row(i)
            self.statusBar().showMessage('ค่าต้องเป็นตัวเลข', 3000)
            return
        if key == 'yaw':
            value = math.atan2(math.sin(math.radians(value)), math.cos(math.radians(value)))
        value = round(value, 4)
        self.push_undo()
        self.wps[i][key] = value
        self.update_point(i)
        self.update_table_row(i)
        self.update_paths()
        self.update_footprint()
        self.update_risk()
        self.update_point_card()
        self.mark_dirty()

    def on_table_selection(self):
        if self.syncing:
            return
        idx = self.selected_indices()
        self.syncing = True
        self.scene.clearSelection()
        for i in idx:
            self.items[i].setSelected(True)
        self.syncing = False
        for i in range(len(self.items)):
            self.style_point(i)
        if idx:
            self.view.centerOn(self.items[idx[0]])
        self.update_footprint()
        self.update_point_card()

    def on_scene_selection(self):
        if self.syncing:
            return
        idx = sorted(it.index for it in self.scene.selectedItems()
                     if isinstance(it, WaypointItem))
        self.select_rows(idx)
        if idx:
            self.table.scrollToItem(self.table.item(idx[0] - self.range_start, 0))

    # ── ลากจุดบนแผนที่ ───────────────────────────────────────────────────────
    def begin_drag(self):
        self._drag_snapshot = self.snapshot()

    def end_drag(self):
        if self._drag_snapshot is not None and self._drag_snapshot[0] != self.wps:
            self.push_undo(self._drag_snapshot)
            self.fill_table()
        self._drag_snapshot = None

    def on_point_dragged(self, i: int, pos: QPointF):
        x, y = self.map.scene_to_world(pos.x(), pos.y())
        self.wps[i]['x'] = round(x, 4)
        self.wps[i]['y'] = round(y, 4)
        self.style_point(i)
        self.update_table_row(i)
        self.update_paths()
        self.update_footprint()
        self.update_risk()
        self.update_point_card()
        self.mark_dirty()

    def show_cursor(self, pos: QPointF):
        x, y = self.map.scene_to_world(pos.x(), pos.y())
        c = self.map.clearance(x, y)
        if self.compact:
            self.coord_label.setText(f'x {x:.2f}  y {y:.2f}  ห่าง {format_clearance(c)}')
        else:
            self.coord_label.setText(
                f'x = {x:.3f}   y = {y:.3f}   ·   ห่างผนัง {format_clearance(c)} m')

    # ── คำสั่งแก้ไข ──────────────────────────────────────────────────────────
    def delete_selected(self):
        idx = self.selected_indices()
        if not idx:
            self.statusBar().showMessage('ยังไม่ได้เลือกจุด', 3000)
            return
        if len(self.wps) - len(idx) < 2:
            self.statusBar().showMessage('ต้องเหลืออย่างน้อย 2 จุด', 3000)
            return
        self.push_undo()
        for i in reversed(idx):
            del self.wps[i]
        self.range_end = max(self.range_start, self.range_end - len(idx))
        self.range_end = min(self.range_end, len(self.wps) - 1)
        self.range_start = min(self.range_start, self.range_end)
        self.mark_dirty()
        self.rebuild()

    def insert_after(self):
        idx = self.selected_indices()
        if not idx:
            self.statusBar().showMessage('เลือกจุดก่อน แล้วจะแทรกจุดใหม่ต่อท้ายจุดนั้น', 3000)
            return
        i = idx[-1]
        cur = self.wps[i]
        if i + 1 < len(self.wps):
            nxt = self.wps[i + 1]
            x, y = (cur['x'] + nxt['x']) / 2.0, (cur['y'] + nxt['y']) / 2.0
            yaw = math.atan2(nxt['y'] - cur['y'], nxt['x'] - cur['x'])
        else:
            yaw = cur['yaw']
            x, y = cur['x'] + 0.2 * math.cos(yaw), cur['y'] + 0.2 * math.sin(yaw)
        self.push_undo()
        self.wps.insert(i + 1, {'name': 'NEW', 'x': round(x, 4), 'y': round(y, 4),
                                'yaw': round(yaw, 4)})
        self.range_end += 1
        self.mark_dirty()
        self.rebuild()
        self.select_rows([i + 1])

    def smooth_range(self):
        """ถ่วงน้ำหนัก (1, 2, 1)/4 กับจุดข้างเคียง ลด noise จาก AMCL/การขับมือ
        จุดนอกช่วงไม่ขยับ จึงต่อกับเส้นทางเดิมได้พอดี"""
        n = len(self.wps)
        lo, hi = max(self.range_start, 1), min(self.range_end, n - 2)
        if lo > hi:
            return
        self.push_undo()
        for _ in range(self.spin_smooth.value()):
            old = [(wp['x'], wp['y']) for wp in self.wps]
            for i in range(lo, hi + 1):
                self.wps[i]['x'] = round((old[i - 1][0] + 2 * old[i][0] + old[i + 1][0]) / 4, 4)
                self.wps[i]['y'] = round((old[i - 1][1] + 2 * old[i][1] + old[i + 1][1]) / 4, 4)
        self._recompute_yaw()
        self.mark_dirty()
        self.refresh()
        self.statusBar().showMessage(f'Smooth {self.spin_smooth.value()} รอบแล้ว', 3000)

    def recompute_yaw_range(self):
        self.push_undo()
        self._recompute_yaw()
        self.mark_dirty()
        self.refresh()

    def _recompute_yaw(self):
        """yaw = ทิศจากจุดนี้ไปจุดถัดไป (จุดสุดท้ายใช้ทิศจากจุดก่อนหน้า)"""
        n = len(self.wps)
        for i in self.range_indices():
            a, b = (i, i + 1) if i + 1 < n else (i - 1, i)
            if a < 0:
                continue
            dx = self.wps[b]['x'] - self.wps[a]['x']
            dy = self.wps[b]['y'] - self.wps[a]['y']
            if math.hypot(dx, dy) > 1e-6:
                self.wps[i]['yaw'] = round(math.atan2(dy, dx), 4)

    def undo(self):
        if not self.undo_stack:
            self.statusBar().showMessage('ไม่มีอะไรให้ undo', 3000)
            return
        self.wps, self.range_start, self.range_end = self.undo_stack.pop()
        self._last_spin_edit = None
        self.mark_dirty()
        self.rebuild()

    def apply_range(self):
        a, b = self.spin_start.value() - 1, self.spin_end.value() - 1
        self.range_start, self.range_end = min(a, b), max(a, b)
        self.rebuild()
        self.fit_range()

    def fit_range(self):
        pts = [self.map.world_to_scene(self.wps[i]['x'], self.wps[i]['y'])
               for i in self.range_indices()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        margin = 1.5 / self.map.resolution
        rect = QRectF(min(xs) - margin, min(ys) - margin,
                      max(xs) - min(xs) + 2 * margin, max(ys) - min(ys) + 2 * margin)
        self.view.fitInView(rect, Qt.KeepAspectRatio)

    def fit_all(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    # ── ไฟล์ ────────────────────────────────────────────────────────────────
    def confirm_discard(self) -> bool:
        """คืน True ถ้าทิ้งงาน/บันทึกแล้วไปต่อได้"""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, 'ยังไม่ได้บันทึก', 'มีการแก้ไขที่ยังไม่ได้บันทึก ต้องการบันทึกก่อนไหม?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def open_waypoints(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'เปิดไฟล์ waypoint',
                                              os.path.dirname(self.wp_path), 'YAML (*.yaml)')
        if not path:
            return
        try:
            wps = load_waypoints(path)
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as e:
            QMessageBox.critical(self, 'เปิดไฟล์ไม่สำเร็จ', str(e))
            return
        self.wps = wps
        self.wp_path = os.path.abspath(path)
        self.set_range(None)
        self.undo_stack = []
        self.dirty = False
        self.update_title()
        self.rebuild()
        self.fit_range()

    def save(self, path=None) -> bool:
        path = path or self.wp_path
        # เรียงชื่อใหม่ให้ต่อเนื่อง (จุดที่แทรก/ลบจะทำให้ชื่อเดิมขาดช่วง)
        for k, wp in enumerate(self.wps):
            wp['name'] = f'P{k + 1:03d}'
        try:
            if os.path.exists(path):
                shutil.copy2(path, path + '.bak')
            with open(path, 'w') as f:
                f.write('# Recorded path — แก้ไขด้วย waypoint_editor.py\n')
                f.write(f'# แก้ไขเมื่อ: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
                f.write(f'# จำนวนจุด: {len(self.wps)}\n\n')
                yaml.dump({'waypoints': self.wps}, f,
                          default_flow_style=False, allow_unicode=True, sort_keys=False)
        except OSError as e:
            QMessageBox.critical(self, 'บันทึกไม่สำเร็จ', str(e))
            return False
        self.wp_path = path
        self.dirty = False
        self.update_title()
        self.rebuild()
        self.statusBar().showMessage(f'บันทึกแล้ว: {path} (ไฟล์เดิมอยู่ที่ .bak)', 8000)
        return True

    def save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, 'Save As', self.wp_path, 'YAML (*.yaml)')
        if not path:
            return False
        if not path.endswith('.yaml'):
            path += '.yaml'
        return self.save(path)

    def closeEvent(self, event):
        if self.confirm_discard():
            event.accept()
        else:
            event.ignore()


def main():
    parser = argparse.ArgumentParser(description='แก้ไข waypoint เฉพาะช่วงบนแผนที่')
    parser.add_argument('--map', required=True, help='ไฟล์ map .yaml')
    parser.add_argument('--waypoints', required=True, help='ไฟล์ waypoints .yaml')
    parser.add_argument('--range', nargs=2, type=int, metavar=('START', 'END'),
                        help='ช่วงลำดับจุดที่จะแก้ (เริ่มนับที่ 1) เช่น --range 180 200')
    size = parser.add_mutually_exclusive_group()
    size.add_argument('--compact', dest='compact', action='store_true', default=None,
                      help='บังคับใช้ layout จอเล็ก (เช่น 1024×768)')
    size.add_argument('--no-compact', dest='compact', action='store_false',
                      help='บังคับใช้ layout ปกติ')
    # parse_known_args: ไม่ error เมื่อ ros2 run แนบ --ros-args มาด้วย
    args, _ = parser.parse_known_args()

    app = QApplication(sys.argv[:1])
    avail = app.primaryScreen().availableGeometry()
    compact = args.compact
    if compact is None:   # ไม่ได้ระบุ → ดูจากขนาดจอ
        compact = avail.width() < COMPACT_BELOW[0] or avail.height() < COMPACT_BELOW[1]
    theme.apply(app, theme.saved_theme(), compact)
    try:
        editor = WaypointEditor(os.path.expanduser(args.map),
                                os.path.expanduser(args.waypoints), args.range)
    except (OSError, RuntimeError, KeyError, yaml.YAMLError) as e:
        print(f'[ERROR] {e}')
        sys.exit(1)
    if compact:
        editor.showMaximized()   # จอเล็ก: ใช้พื้นที่ทั้งหมด
    else:
        editor.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
