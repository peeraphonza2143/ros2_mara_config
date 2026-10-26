"""
ui_theme.py — theme (Dark/Light) ที่ใช้ร่วมกันระหว่าง waypoint_editor และ map_cleaner
ค่าที่เลือกจะถูกจำไว้ด้วย QSettings เปิดโปรแกรมครั้งหน้าจะได้ theme เดิม
"""

import numpy as np
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QColor, QImage, QPalette
from PyQt5.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout

THEMES = {
    'dark': {
        'bg': '#1b1e24', 'surface': '#242830', 'surface2': '#2d323c', 'border': '#3a404c',
        'text': '#e6e9ef', 'muted': '#9aa3b2',
        'accent': '#4f8cff', 'accent_hover': '#6b9fff', 'accent_text': '#ffffff',
        'danger': '#ef5350', 'warn': '#ffa726', 'ok': '#66bb6a',
        'map_free': '#e8ebf0', 'map_unknown': '#4a505c', 'map_occ': '#111317',
        'view_bg': '#14161b', 'path': '#4f8cff', 'path_dim': '#8a93a3',
        'footprint': '#d16bff', 'select': '#ffd54f', 'label': '#1b1e24',
    },
    'light': {
        'bg': '#f3f5f8', 'surface': '#ffffff', 'surface2': '#eef1f5', 'border': '#d5dae2',
        'text': '#1f2430', 'muted': '#5f6b7a',
        'accent': '#2f6fed', 'accent_hover': '#1f5ad6', 'accent_text': '#ffffff',
        'danger': '#d32f2f', 'warn': '#ef8a00', 'ok': '#2e9d4f',
        'map_free': '#ffffff', 'map_unknown': '#c3c9d2', 'map_occ': '#1d2128',
        'view_bg': '#dde2e9', 'path': '#2f6fed', 'path_dim': '#7d8796',
        'footprint': '#a020c8', 'select': '#f5a700', 'label': '#1f2430',
    },
}

_current = 'dark'
_compact = False   # โหมดจอเล็ก (เช่น 1024×768): ตัวอักษร/ระยะขอบเล็กลง


def current() -> dict:
    return THEMES[_current]


def current_name() -> str:
    return _current


def is_compact() -> bool:
    return _compact


def color(key: str) -> QColor:
    return QColor(THEMES[_current][key])


def saved_theme() -> str:
    name = QSettings('ros2_ws', 'waypoint_tools').value('theme', 'dark')
    return name if name in THEMES else 'dark'


def apply(app: QApplication, name: str, compact=None):
    """ตั้ง palette + stylesheet ให้ทั้งโปรแกรม แล้วจำค่าไว้
    compact=None คือใช้โหมดเดิม (ตอนสลับ theme จะได้ไม่เปลี่ยนขนาด)"""
    global _current, _compact
    _current = name
    if compact is not None:
        _compact = compact
    QSettings('ros2_ws', 'waypoint_tools').setValue('theme', name)
    t = THEMES[name]

    app.setStyle('Fusion')
    pal = QPalette()
    roles = {
        QPalette.Window: 'bg', QPalette.WindowText: 'text', QPalette.Base: 'surface',
        QPalette.AlternateBase: 'surface2', QPalette.Text: 'text', QPalette.Button: 'surface2',
        QPalette.ButtonText: 'text', QPalette.Highlight: 'accent',
        QPalette.HighlightedText: 'accent_text', QPalette.ToolTipBase: 'surface2',
        QPalette.ToolTipText: 'text', QPalette.PlaceholderText: 'muted',
    }
    for role, key in roles.items():
        pal.setColor(role, QColor(t[key]))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(t['muted']))
    app.setPalette(pal)
    if _compact:
        font = app.font()
        font.setPointSizeF(9)
        app.setFont(font)
    app.setStyleSheet(_stylesheet(t, _compact))


def _stylesheet(t: dict, compact: bool = False) -> str:
    p = {   # ขนาดที่ต่างกันระหว่างโหมดปกติ / compact
        'tb_pad': '3px 6px' if compact else '5px 9px',
        'btn_pad': '3px 8px' if compact else '6px 12px',
        'spin_pad': '2px 4px' if compact else '4px 6px',
        'hdr_pad': '3px' if compact else '5px',
        'title_pt': '9.5pt' if compact else '10.5pt',
        'big_pt': '12pt' if compact else '15pt',
    }
    return f"""
    QMainWindow {{ background: {t['bg']}; }}
    QSplitter::handle {{ background: {t['border']}; width: 1px; }}

    QToolBar {{ background: {t['surface']}; border: none;
               border-bottom: 1px solid {t['border']}; padding: 4px 6px; spacing: 2px; }}
    QToolBar QToolButton {{ background: transparent; color: {t['text']};
               border: 1px solid transparent; border-radius: 6px; padding: {p['tb_pad']}; }}
    QToolBar QToolButton:hover {{ background: {t['surface2']}; border-color: {t['border']}; }}
    QToolBar QToolButton:pressed, QToolBar QToolButton:checked {{
               background: {t['accent']}; color: {t['accent_text']}; }}
    QToolBar::separator {{ background: {t['border']}; width: 1px; margin: 5px 6px; }}
    QToolBar QLabel {{ color: {t['muted']}; padding: 0 2px 0 6px; }}

    QFrame#card {{ background: {t['surface']}; border: 1px solid {t['border']};
                  border-radius: 10px; }}
    QFrame#card QLabel, QFrame#card QCheckBox, QFrame#card QRadioButton {{
                  background: transparent; }}
    QLabel#cardTitle {{ font-weight: 600; font-size: {p['title_pt']}; color: {t['text']}; }}
    QLabel#bigName {{ font-size: {p['big_pt']}; font-weight: 700; color: {t['text']}; }}
    QLabel[muted="true"] {{ color: {t['muted']}; }}

    QPushButton {{ background: {t['surface2']}; color: {t['text']};
                  border: 1px solid {t['border']}; border-radius: 6px; padding: {p['btn_pad']}; }}
    QPushButton:hover {{ border-color: {t['accent']}; }}
    QPushButton:checked {{ background: {t['accent']}; color: {t['accent_text']};
                  border-color: {t['accent']}; }}
    QPushButton:disabled {{ color: {t['muted']}; }}
    QPushButton[primary="true"] {{ background: {t['accent']}; color: {t['accent_text']};
                  border: 1px solid {t['accent']}; font-weight: 600; }}
    QPushButton[primary="true"]:hover {{ background: {t['accent_hover']}; }}

    QSpinBox, QDoubleSpinBox {{ background: {t['bg']}; color: {t['text']};
                  border: 1px solid {t['border']}; border-radius: 6px; padding: {p['spin_pad']}; }}
    QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {t['accent']}; }}
    QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {t['muted']}; }}
    QSpinBox, QDoubleSpinBox {{ padding-right: 20px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
                  subcontrol-origin: border; width: 18px; background: {t['surface2']};
                  border-left: 1px solid {t['border']}; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right;
                  border-top-right-radius: 6px; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right;
                  border-bottom-right-radius: 6px; }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
                  background: {t['border']}; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ width: 0; height: 0;
                  border-left: 4px solid transparent; border-right: 4px solid transparent;
                  border-bottom: 5px solid {t['muted']}; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ width: 0; height: 0;
                  border-left: 4px solid transparent; border-right: 4px solid transparent;
                  border-top: 5px solid {t['muted']}; }}

    QTableWidget {{ background: {t['surface']}; alternate-background-color: {t['surface2']};
                  border: 1px solid {t['border']}; border-radius: 8px; gridline-color: transparent;
                  selection-background-color: {t['accent']};
                  selection-color: {t['accent_text']}; }}
    QHeaderView::section {{ background: {t['surface2']}; color: {t['muted']}; border: none;
                  border-bottom: 1px solid {t['border']}; padding: {p['hdr_pad']};
                  font-weight: 600; }}
    QTableCornerButton::section {{ background: {t['surface2']}; border: none; }}

    QSlider::groove:horizontal {{ height: 4px; background: {t['border']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {t['accent']}; width: 14px; height: 14px;
                  margin: -5px 0; border-radius: 7px; }}

    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
                  border: 1.5px solid {t['muted']}; background: {t['bg']}; }}
    QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
    QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']};
                  image: url(none); }}

    QStatusBar {{ background: {t['surface']}; border-top: 1px solid {t['border']};
                 color: {t['muted']}; }}
    QStatusBar QLabel {{ background: transparent; color: {t['muted']}; padding: 0 6px; }}

    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                 background: {t['border']}; border-radius: 5px;
                 min-height: 24px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QToolTip {{ background: {t['surface2']}; color: {t['text']};
               border: 1px solid {t['border']}; padding: 4px; }}
    QGraphicsView {{ border: none; background: {t['view_bg']}; }}
    """


def chip_style(hex_color: str) -> str:
    """ป้ายตัวเลขแบบ pill ขอบสี (ใช้แสดงจำนวนจุดเสี่ยง/จำนวน noise)"""
    c = QColor(hex_color)
    return (f'color: {hex_color}; background: rgba({c.red()},{c.green()},{c.blue()},38);'
            f'border: 1px solid {hex_color}; border-radius: 10px;'
            f'padding: {"1px 6px" if _compact else "2px 9px"};'
            'font-weight: 600;')


def make_card(title: str):
    """การ์ดพื้นหลังโค้งมน มีหัวข้อ → คืน (frame, layout ภายใน)"""
    frame = QFrame()
    frame.setObjectName('card')
    layout = QVBoxLayout(frame)
    if _compact:
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)
    else:
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
    label = QLabel(title)
    label.setObjectName('cardTitle')
    layout.addWidget(label)
    return frame, layout


def muted_label(text: str = '') -> QLabel:
    label = QLabel(text)
    label.setProperty('muted', True)
    return label


def colorize(occupied: np.ndarray, free: np.ndarray, overlays=()) -> QImage:
    """สร้างภาพแผนที่สีตาม theme: free / unknown / occupied
    overlays = [(mask, '#rrggbb'), ...] ระบายทับตามลำดับ"""
    t = current()
    h, w = occupied.shape
    rgb = np.empty((h, w, 3), np.uint8)
    rgb[:] = _rgb(t['map_unknown'])
    rgb[free] = _rgb(t['map_free'])
    rgb[occupied] = _rgb(t['map_occ'])
    for mask, hex_color in overlays:
        rgb[mask] = _rgb(hex_color)
    rgb = np.ascontiguousarray(rgb)
    # .copy() ให้ QImage มีหน่วยความจำของตัวเอง ไม่อ้างอิง numpy array ที่จะถูกทิ้ง
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def _rgb(hex_color: str):
    c = QColor(hex_color)
    return c.red(), c.green(), c.blue()
