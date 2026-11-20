# Copyright (c) 2026 jasonsuda
# SPDX-License-Identifier: GPL-3.0-only

"""Pudding Terminal. Single-file PyQt6 / pyte terminal.
Persistent preferences: APP_DIR/config/pudding.ini. Logs: opt-in APP_DIR/device_logs.
Startup never connects, starts logging, terminates processes or changes services.
"""
from __future__ import annotations
import asyncio
import codecs
import concurrent.futures
import copy
import ctypes
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import asyncssh
import pyte
import serial
import serial.tools.list_ports
import telnetlib3
from telnetlib3.telopt import NAWS
from wcwidth import wcswidth
from PyQt6.QtCore import Qt, QEvent, QPoint, QPointF, QRect, QRectF, QSize, QSettings, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
 QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox,
 QCheckBox, QStackedWidget, QScrollArea, QAbstractScrollArea, QDialog, QDialogButtonBox,
 QFontComboBox, QColorDialog, QFileDialog, QInputDialog, QMessageBox, QMenu, QSizePolicy, QStyle)

# ---------------- Portable paths and preferences ----------------
APP_DIR = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent

def persistent_path(value):
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else APP_DIR / path).resolve()
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve()
    if path == windows or windows in path.parents:
        raise PermissionError('禁止將持久檔案寫入 Windows 系統目錄。請選擇其他位置。')
    return path

def system_theme():
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                return 'light' if winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] else 'dark'
        except OSError:
            pass
    return 'dark' if QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else 'light'

DEFAULTS = {
 'connection/type': 'SSH', 'serial/port': 'COM1', 'serial/baud': 9600,
 'serial/data': '8', 'serial/parity': 'None', 'serial/stop': '1', 'serial/flow': 'None',
 'ssh/host': '', 'ssh/port': 22, 'telnet/host': '', 'telnet/port': 23,
 'terminal/font': 'Consolas', 'terminal/size': 11, 'terminal/foreground': '#90ee90',
 'terminal/background': '#0e0e0e', 'terminal/encoding': 'utf-8', 'terminal/history': 5000,
 'keyboard/backspace': 'DEL', 'keyboard/enter': 'CR', 'keyboard/function': 'PuTTY',
 'mouse/mode': 'PuTTY', 'log/directory': 'device_logs', 'log/rotate': False,
 'log/interval': 1.0, 'log/unit': '小時',
 'terminal/timestamp': True,
}

class Preferences:
    def __init__(self, path=None):
        self.path = persistent_path(path or 'config/pudding.ini')
        self.store = QSettings(str(self.path), QSettings.Format.IniFormat)
        self.store.setFallbacksEnabled(False)

    def get(self, key, default=None):
        default = DEFAULTS.get(key, default)
        value = self.store.value(key, default)
        if isinstance(default, bool):
            return str(value).lower() in ('true', '1')
        try:
            return type(default)(value) if isinstance(default, (int, float)) else value
        except (TypeError, ValueError):
            return default

    def save(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for key, value in values.items():
            self.store.setValue(key, value)
        self.store.sync()
        if self.store.status() != QSettings.Status.NoError:
            raise OSError(f'無法儲存設定：{self.path}')

# ---------------- Stateful VT parser and xterm modes ----------------
class TerminalStream(pyte.Stream):
    escape = dict(pyte.Stream.escape, **{'=': 'application_keypad', '>': 'normal_keypad'})
    events = pyte.Stream.events | {'application_keypad', 'normal_keypad'}

class RetainedGrid:
    """Resize storage without replaying VT text or truncating existing cells.

    Primary: displaced top rows move to the bounded history deque and are
    reclaimed when growing. Alternate: retain clipped rows at their coordinates.
    Explicit remote erase/edit operations retain their normal screen semantics.
    """
    def reset(self):
        self._resize_cursor = None
        self.history_evictions = 0
        super().reset()

    def _archive_row(self, row):
        if len(self.history.top) == self.history.top.maxlen:
            self.history_evictions += 1
        self.history.top.append(row)

    def index(self):
        if hasattr(self, 'history'):
            _, bottom = self.margins or (0, self.lines-1)
            if self.cursor.y == bottom and len(self.history.top) == self.history.top.maxlen:
                self.history_evictions += 1
        super().index()

    def resize(self, lines=None, columns=None):
        lines, columns = max(2, lines or self.lines), max(2, columns or self.columns)
        if (lines, columns) == (self.lines, self.columns):
            return
        if self._resize_cursor is None:
            self._resize_cursor = (self.cursor.x, self.buffer[self.cursor.y])
        delta = 0
        if hasattr(self, 'history'):
            rows = [self.buffer[y] for y in range(self.lines)]
            if lines < self.lines:
                excess = self.lines-lines
                # Blank space below the live cursor is not received output.
                while excess and len(rows)-1 > self.cursor.y:
                    row = rows[-1]
                    if getattr(row,'receive_timestamp',None) or any(c != self.default_char for c in row.values()):
                        break
                    rows.pop()
                    excess -= 1
                for row in rows[:excess]:
                    self._archive_row(row)
                rows = rows[excess:]
                delta = -excess
            elif lines > self.lines:
                count = min(lines-self.lines, len(self.history.top))
                restored = [self.history.top.pop() for _ in range(count)]
                rows = list(reversed(restored)) + rows
                delta = count
            self.buffer.clear()
            self.buffer.update(enumerate(rows))
        # Neither primary nor alternate throws away right-hand cells. A wider
        # viewport can render them again, with the original Char and timestamp.
        self.lines, self.columns = lines, columns
        self.cursor.y = max(0, min(lines-1, self.cursor.y+delta))
        hint_x, hint_row = self._resize_cursor
        for y, row in self.buffer.items():
            if row is hint_row and y < lines:
                self.cursor.y = y
                break
        # pyte's x == columns is its pending-wrap state. Clamping instead to
        # columns-1 would overwrite an old cell on the next serial text chunk.
        self.cursor.x = max(0, min(columns, hint_x))
        for point in self.savepoints:
            point.cursor.y += delta
        self.set_margins()
        self.dirty.update(range(lines))

    def erase_in_line(self, how=0, private=False):
        super().erase_in_line(how, private=private)
        # EL to the end of a retained row must also clear a hidden wide suffix.
        if how in (0,2):
            row = self.buffer[self.cursor.y]
            for x in list(row):
                if x >= self.columns:
                    row[x] = self.cursor.attrs

    def erase_in_display(self, how=0, *args, **kwargs):
        super().erase_in_display(how, *args, **kwargs)
        if how in (0,2,3):
            for y, row in self.buffer.items():
                if y >= self.lines:
                    for x in row:
                        row[x] = self.cursor.attrs
                    row.receive_timestamp = None

    def insert_characters(self, *args, **kwargs):
        self._forget_edited_suffix()
        super().insert_characters(*args, **kwargs)

    def delete_characters(self, *args, **kwargs):
        self._forget_edited_suffix()
        super().delete_characters(*args, **kwargs)

    def _forget_edited_suffix(self):
        row = self.buffer[self.cursor.y]
        for x in list(row):
            if x >= self.columns:
                del row[x]


class RetainedHistoryScreen(RetainedGrid, pyte.HistoryScreen):
    pass


class RetainedScreen(RetainedGrid, pyte.Screen):
    pass


class TerminalModel:
    """pyte owns VT parsing; route cached events to active screen at call time.
    pyte 0.8.2 has no alternate screen/mouse/bracketed paste implementation.
    Keep primary scrollback and alternate buffer separate, including split CSI.
    """
    def __init__(self, columns=80, lines=24, history=5000, reply=None, encoding='utf-8'):
        self.primary = RetainedHistoryScreen(columns, lines, history=history)
        self.alternate = RetainedScreen(columns, lines)
        self.screen = self.primary
        self.private_modes = set()
        self.keypad = False
        self.reply = reply or (lambda data: None)
        self.encoding = encoding
        self.decoder = codecs.getincrementaldecoder(encoding)(errors='replace')
        self.stream = TerminalStream(self)

    def __getattr__(self, event):
        if event in TerminalStream.events:
            return lambda *args, **kwargs: getattr(self.screen, event)(*args, **kwargs)
        raise AttributeError(event)

    @property
    def in_alternate(self):
        return self.screen is self.alternate

    @property
    def application_cursor(self):
        return 1 in self.private_modes

    def application_keypad(self):
        self.keypad = True

    def normal_keypad(self):
        self.keypad = False

    def reset(self):
        self.primary.reset()
        self.alternate.reset()
        self.screen = self.primary
        self.private_modes.clear()
        self.keypad = False

    def set_mode(self, *modes, private=False):
        self._mode(True, modes, private)

    def reset_mode(self, *modes, private=False):
        self._mode(False, modes, private)

    def _mode(self, enabled, modes, private):
        for mode in modes:
            if private:
                if enabled:
                    self.private_modes.add(mode)
                else:
                    self.private_modes.discard(mode)
                if mode in (47, 1047, 1049):
                    if enabled and not self.in_alternate:
                        if mode == 1049:
                            self.primary.save_cursor()
                        if mode != 47:
                            self.alternate.reset()
                        self.alternate.mode = self.primary.mode.copy()
                        self.screen = self.alternate
                    elif not enabled and self.in_alternate:
                        self.screen = self.primary
                        if mode == 1049:
                            self.primary.restore_cursor()
                    continue
                if mode == 1048:
                    (self.screen.save_cursor if enabled else self.screen.restore_cursor)()
                    continue
            (self.screen.set_mode if enabled else self.screen.reset_mode)(mode, private=private)

    def report_device_status(self, mode=0, **kwargs):
        if mode == 5:
            self.reply(b'\x1b[0n')
        elif mode == 6:
            c = self.screen.cursor
            self.reply(f'\x1b[{c.y+1};{min(c.x, self.screen.columns-1)+1}R'.encode())

    def report_device_attributes(self, mode=0, **kwargs):
        if mode == 0:
            self.reply(b'\x1b[?6c')

    def write_process_input(self, data):
        self.reply(data.encode(self.encoding))

    def resize(self, columns, lines):
        for screen in (self.primary, self.alternate):
            screen.resize(lines=max(2, lines), columns=max(2, columns))

    def feed(self, data):
        stamp = datetime.now().strftime('[%H:%M:%S]')
        text = self.decoder.decode(data) if isinstance(data, bytes) else data
        if text:
            self.primary._resize_cursor = self.alternate._resize_cursor = None
        self.stream.feed(text)
        self._stamp_received_rows(stamp)
        return text

    @staticmethod
    def _stamp_row(row, stamp):
        # Attributes on pyte's row object travel with scrolling and deepcopy,
        # without inserting cells or changing the device's parser input.
        if not getattr(row, 'receive_timestamp', None):
            row.receive_timestamp = stamp if any(c.data.strip() for c in row.values()) else None

    def _stamp_received_rows(self, stamp):
        for row in self.primary.buffer.values():
            self._stamp_row(row, stamp)
        # Only newly scrolled rows need inspection; old rows already carry it.
        for row in reversed(self.primary.history.top):
            previously_seen = hasattr(row, 'receive_timestamp')
            self._stamp_row(row, stamp)
            if previously_seen:
                break

    def erase_in_line(self, *args, **kwargs):
        self.screen.erase_in_line(*args, **kwargs)
        row = self.screen.buffer[self.screen.cursor.y]
        if not any(c.data.strip() for c in row.values()):
            row.receive_timestamp = None

    def erase_in_display(self, *args, **kwargs):
        self.screen.erase_in_display(*args, **kwargs)
        for row in self.screen.buffer.values():
            if not any(c.data.strip() for c in row.values()):
                row.receive_timestamp = None

    def finish(self):
        text = self.decoder.decode(b'', final=True)
        self.stream.feed(text)
        self._stamp_received_rows(datetime.now().strftime('[%H:%M:%S]'))
        return text


def key_bytes(key, modifiers, text, model, options):
    """C0 handling precedes text. No editor shortcut can consume Ctrl+C."""
    K, M = Qt.Key, Qt.KeyboardModifier
    ctrl, shift, alt = bool(modifiers & M.ControlModifier), bool(modifiers & M.ShiftModifier), bool(modifiers & M.AltModifier)
    if ctrl and alt and text and all(c.isprintable() for c in text):
        return text.encode(model.encoding, errors='replace')
    if ctrl:
        if K.Key_A <= key <= K.Key_Z:
            return bytes([key-K.Key_A+1])
        controls = {K.Key_Space:0, K.Key_At:0, K.Key_2:0, K.Key_BracketLeft:27, K.Key_3:27,
         K.Key_Backslash:28, K.Key_4:28, K.Key_BracketRight:29, K.Key_5:29,
         K.Key_AsciiCircum:30, K.Key_6:30, K.Key_Underscore:31, K.Key_7:31, K.Key_Question:127, K.Key_8:127}
        if key in controls:
            return bytes([controls[key]])
    if modifiers & M.KeypadModifier and model.keypad:
        keypad = {K.Key_0:'p', K.Key_1:'q', K.Key_2:'r', K.Key_3:'s', K.Key_4:'t', K.Key_5:'u',
         K.Key_6:'v', K.Key_7:'w', K.Key_8:'x', K.Key_9:'y', K.Key_Period:'n', K.Key_Minus:'m',
         K.Key_Plus:'k', K.Key_Asterisk:'j', K.Key_Slash:'o', K.Key_Enter:'M', K.Key_Return:'M'}
        if key in keypad:
            return ('\x1bO'+keypad[key]).encode()
    modifier = 1 + int(shift) + 2*int(alt) + 4*int(ctrl)
    arrow = {K.Key_Up:'A', K.Key_Down:'B', K.Key_Right:'C', K.Key_Left:'D'}
    if key in arrow:
        suffix = arrow[key]
        return (f'\x1b[1;{modifier}{suffix}' if modifier > 1 else '\x1b'+('O' if model.application_cursor else '[')+suffix).encode()
    enter = {'CR':b'\r', 'LF':b'\n', 'CRLF':b'\r\n'}[options.get('keyboard/enter', 'CR')]
    fixed = {K.Key_Return:enter, K.Key_Enter:enter,
     K.Key_Backspace:b'\x08' if options.get('keyboard/backspace') == 'BS' else b'\x7f',
     K.Key_Tab:b'\x1b[Z' if shift else b'\t', K.Key_Backtab:b'\x1b[Z', K.Key_Escape:b'\x1b'}
    if key in fixed:
        return (b'\x1b' if alt else b'')+fixed[key]
    putty = options.get('keyboard/function', 'PuTTY') == 'PuTTY'
    if key in (K.Key_Home, K.Key_End) and not putty:
        suffix = 'H' if key == K.Key_Home else 'F'
        return (f'\x1b[1;{modifier}{suffix}' if modifier > 1 else '\x1b'+('O' if model.application_cursor else '[')+suffix).encode()
    numbers = {K.Key_Insert:2, K.Key_Delete:3, K.Key_Home:1, K.Key_End:4, K.Key_PageUp:5, K.Key_PageDown:6}
    if K.Key_F1 <= key <= K.Key_F12:
        n = key-K.Key_F1
        if not putty and n < 4:
            return (f'\x1b[1;{modifier}{"PQRS"[n]}' if modifier > 1 else '\x1bO'+'PQRS'[n]).encode()
        numbers[key] = [11,12,13,14,15,17,18,19,20,21,23,24][n]
    if key in numbers:
        return (f'\x1b[{numbers[key]};{modifier}~' if modifier > 1 else f'\x1b[{numbers[key]}~').encode()
    if text and all(c.isprintable() for c in text):
        return (b'\x1b' if alt else b'')+text.encode(model.encoding, errors='replace')
    return b''

# ---------------- Terminal cell renderer and input ----------------
ANSI_COLORS = dict(zip(
 ['black','red','green','brown','blue','magenta','cyan','white','brightblack','brightred','brightgreen','brightbrown','brightblue','brightmagenta','brightcyan','brightwhite'],
 ['#000000','#cd3131','#0dbc79','#e5e510','#2472c8','#bc3fbc','#11a8cd','#e5e5e5','#666666','#f14c4c','#23d18b','#f5f543','#3b8eea','#d670d6','#29b8db','#ffffff']))

class TerminalWidget(QAbstractScrollArea):
    transmit = pyqtSignal(bytes)
    sizeChanged = pyqtSignal(int, int)
    textReceived = pyqtSignal(str)

    def __init__(self, options=None, parent=None):
        super().__init__(parent)
        self.options = dict(DEFAULTS, **(options or {}))
        self.model = TerminalModel(history=self.options['terminal/history'], reply=self.transmit.emit, encoding=self.options['terminal/encoding'])
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setAccessibleName('終端機')
        self.anchor = self.selection_end = self.frozen_rows = None
        self.dragging = False
        self.selection_kind = 'char'
        self.last_double = 0.0
        self.last_double_pos = QPoint()
        self.report_button = None
        self.preedit = ''
        self._following = True
        self._scroll_anchor = None
        self._primary_view_state = None
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.set_appearance()
        self.blink_on = True
        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self._blink)
        self.blink_timer.start(550)
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._resize_grid)
        self.paint_timer = QTimer(self)
        self.paint_timer.setSingleShot(True)
        self.paint_timer.timeout.connect(self.viewport().update)

    def set_appearance(self):
        self.cell_font = QFont(self.options['terminal/font'], int(self.options['terminal/size']))
        self.cell_font.setStyleHint(QFont.StyleHint.Monospace)
        metrics = QFontMetricsF(self.cell_font)
        self.cell_width = max(1.0, metrics.horizontalAdvance('M'))
        self.cell_height = max(1, math.ceil(metrics.height())+2)
        self.ascent = metrics.ascent()
        self.timestamp_font = QFont(self.cell_font)
        self.timestamp_font.setPointSizeF(max(8, self.cell_font.pointSizeF()-2))
        timestamp_metrics = QFontMetricsF(self.timestamp_font)
        self.timestamp_baseline = (self.cell_height-timestamp_metrics.height())/2 + timestamp_metrics.ascent()
        self.gutter_width = math.ceil(timestamp_metrics.horizontalAdvance('[00:00:00]')) + 14
        self.viewport().update()
        if hasattr(self, 'resize_timer'):
            self.resize_timer.start(50)

    def reset_session(self):
        self.model = TerminalModel(self.model.screen.columns, self.model.screen.lines,
         self.options['terminal/history'], self.transmit.emit, self.options['terminal/encoding'])
        self.clear_selection()
        self._following = True
        self._scroll_anchor = None
        self._primary_view_state = None
        self.verticalScrollBar().setRange(0, 0)
        self.viewport().update()

    def feed(self, data):
        was_alt = self.model.in_alternate
        state = self._view_state()
        text = self.model.feed(data)
        self.textReceived.emit(text)
        if was_alt != self.model.in_alternate:
            self.clear_selection()
            if self.model.in_alternate:
                self._primary_view_state = state
                state = (True,None)
            else:
                state = self._primary_view_state or (True,None)
                self._primary_view_state = None
        self._sync_scrollbar(state)
        if not self.paint_timer.isActive():
            self.paint_timer.start(16)

    def _blink(self):
        self.blink_on = not self.blink_on
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'resize_timer'):
            self.resize_timer.start(60)

    def _resize_grid(self):
        cols = max(2, int((self.viewport().width()-self.grid_left()-6)/self.cell_width))
        rows = max(2, int((self.viewport().height()-8)/self.cell_height))
        if (cols, rows) != (self.model.screen.columns, self.model.screen.lines):
            state = self._view_state()
            self.clear_selection()
            self.model.resize(cols, rows)
            self._sync_scrollbar(state)
            self.sizeChanged.emit(cols, rows)
            self.viewport().update()

    def _scroll_changed(self):
        self._following = self.verticalScrollBar().value() == self.verticalScrollBar().maximum()
        self._scroll_anchor = None
        if not self._following and not self.model.in_alternate:
            self._scroll_anchor = self._primary_rows()[self.verticalScrollBar().value()]
        self.clear_selection()
        self.viewport().update()

    def _primary_rows(self):
        screen = self.model.primary
        return list(screen.history.top) + [screen.buffer[y] for y in range(screen.lines)]

    def _view_state(self):
        if self.model.in_alternate or self._following:
            return True,None
        if self._scroll_anchor is not None:
            return False,self._scroll_anchor
        rows = self._primary_rows()
        value = self.verticalScrollBar().value()
        return False, rows[value] if value < len(rows) else None

    def _sync_scrollbar(self, state):
        following, anchor = state
        bar = self.verticalScrollBar()
        count = 0 if self.model.in_alternate else len(self.model.primary.history.top)
        position = count if following else 0
        rows = []
        if not following and anchor is not None:
            rows = self._primary_rows()
            position = next((i for i,row in enumerate(rows) if row is anchor),-1)
            if position < 0:  # The anchor was explicitly cleared or capacity-evicted.
                position = 0
                anchor = rows[0] if rows else None
        bar.blockSignals(True)
        bar.setRange(0,count)
        bar.setPageStep(self.model.screen.lines)
        bar.setValue(min(position,count))
        bar.blockSignals(False)
        # A clamped maximum is not a user request to follow new output.
        self._following = following
        self._scroll_anchor = None if following else anchor

    def rows(self):
        if self.frozen_rows is not None:
            return self.frozen_rows
        screen = self.model.screen
        if self.model.in_alternate:
            return [screen.buffer[y] for y in range(screen.lines)]
        all_rows = self._primary_rows()
        start = min(self.verticalScrollBar().value(), len(self.model.primary.history.top))
        return all_rows[start:start+screen.lines]

    def color(self, value, default):
        if value == 'default':
            return QColor(default)
        return QColor(ANSI_COLORS.get(value, '#'+value if re.fullmatch(r'[0-9a-fA-F]{6}', value) else default))

    def grid_left(self):
        return 6 + (self.gutter_width if self.options['terminal/timestamp'] else 0)

    def visible_timestamp(self, row):
        if self.options['terminal/timestamp'] and not self.model.in_alternate:
            return getattr(row,'receive_timestamp',None) or ''
        return ''

    def set_timestamps(self, enabled):
        if self.options['terminal/timestamp'] == bool(enabled):
            return
        self.options['terminal/timestamp'] = bool(enabled)
        self.clear_selection()
        # A manual column toggle is a real viewport change, including PTY/NAWS.
        self._resize_grid()
        self.viewport().update()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        bg, fg = self.options['terminal/background'], self.options['terminal/foreground']
        painter.fillRect(self.viewport().rect(), QColor(bg))
        screen = self.model.screen
        left = self.grid_left()
        if self.options['terminal/timestamp']:
            separator = QColor(fg)
            separator.setAlpha(55)
            painter.setPen(separator)
            painter.drawLine(int(left-7),0,int(left-7),self.viewport().height())
        selected = sorted((self.anchor, self.selection_end)) if self.anchor is not None else None
        for y, row in enumerate(self.rows()):
            stamp = self.visible_timestamp(row)
            if stamp:
                muted = QColor(fg)
                muted.setAlpha(160)
                painter.setPen(muted)
                painter.setFont(self.timestamp_font)
                painter.drawText(QPointF(6,4+y*self.cell_height+self.timestamp_baseline),stamp)
            glyphs = []
            for x in range(screen.columns):
                char = row[x]
                back, fore = self.color(char.bg, bg), self.color(char.fg, fg)
                if char.reverse:
                    back, fore = fore, back
                if selected and selected[0] <= (y,x) < selected[1]:
                    back, fore = QColor('#326ff5'), QColor('#ffffff')
                rect = QRectF(left+x*self.cell_width, 4+y*self.cell_height, self.cell_width+.2, self.cell_height)
                painter.fillRect(rect, back)
                if char.data and char.data != ' ':
                    glyphs.append((char, fore, rect))
            # Paint all backgrounds first: a CJK continuation cell must not erase
            # the right half of the preceding wide glyph.
            for char, fore, rect in glyphs:
                    font = QFont(self.cell_font)
                    font.setBold(char.bold)
                    font.setItalic(char.italics)
                    font.setUnderline(char.underscore)
                    font.setStrikeOut(char.strikethrough)
                    painter.setFont(font)
                    painter.setPen(fore)
                    painter.drawText(QPointF(rect.x(), rect.y()+self.ascent+1), char.data)
        if self.verticalScrollBar().value() == self.verticalScrollBar().maximum() and self.frozen_rows is None:
            c = screen.cursor
            rect = QRectF(left+min(c.x,screen.columns-1)*self.cell_width, 4+c.y*self.cell_height, self.cell_width, self.cell_height)
            if not c.hidden and (self.blink_on or not self.hasFocus()):
                painter.setPen(QColor(fg))
                painter.drawRect(rect.adjusted(0,0,-1,-1))
                if self.hasFocus():
                    painter.fillRect(rect.adjusted(0,self.cell_height-3,0,0), QColor(fg))
            if self.preedit:
                painter.setFont(self.cell_font)
                painter.setPen(QColor(fg))
                painter.drawText(QPointF(rect.x(),rect.y()+self.ascent), self.preedit)

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if event.type() == QEvent.Type.KeyPress:
            self.keyPressEvent(event)  # Also intercept Tab instead of focus traversal.
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        K, M = Qt.Key, Qt.KeyboardModifier
        key, mods = event.key(), event.modifiers()
        ctrl, shift = bool(mods & M.ControlModifier), bool(mods & M.ShiftModifier)
        if (ctrl and key == K.Key_Insert) or (ctrl and shift and key == K.Key_C):
            self.copy_selection()
        elif (shift and key == K.Key_Insert) or (ctrl and shift and key == K.Key_V):
            self.paste()
        elif self.options['mouse/mode'] == 'Windows' and ctrl and key == K.Key_V:
            self.paste()
        elif shift and not ctrl and key in (K.Key_PageUp,K.Key_PageDown):
            bar = self.verticalScrollBar()
            bar.setValue(bar.value()+(-1 if key == K.Key_PageUp else 1)*max(1,self.model.screen.lines-1))
        else:
            data = key_bytes(key, mods, event.text(), self.model, self.options)
            if data:
                self.follow_output()
                self.transmit.emit(data)
        event.accept()

    def inputMethodEvent(self, event):
        if event.commitString():
            self.transmit.emit(event.commitString().encode(self.model.encoding, errors='replace'))
        self.preedit = event.preeditString()
        self.viewport().update()
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            c = self.model.screen.cursor
            return QRect(int(self.grid_left()+c.x*self.cell_width),int(4+c.y*self.cell_height),int(self.cell_width),self.cell_height)
        return super().inputMethodQuery(query)

    def paste(self, text=None):
        text = QApplication.clipboard().text() if text is None or isinstance(text,bool) else text
        if not text:
            return
        text = ''.join(c for c in text if c in '\r\n\t' or (ord(c)>=32 and ord(c)!=127))
        text = text.replace('\r\n','\n').replace('\r','\n')
        newline = {'CR':'\r','LF':'\n','CRLF':'\r\n'}[self.options['keyboard/enter']]
        data = text.replace('\n',newline).encode(self.model.encoding,errors='replace')
        if 2004 in self.model.private_modes:
            data = b'\x1b[200~'+data+b'\x1b[201~'
        self.transmit.emit(data)

    def clear_selection(self):
        self.anchor = self.selection_end = self.frozen_rows = None
        self.viewport().update()

    def selected_text(self):
        if self.anchor is None or self.anchor == self.selection_end:
            return ''
        start, end = sorted((self.anchor,self.selection_end))
        rows, result = self.rows(), []
        for y in range(start[0], min(end[0]+1,len(rows))):
            left = start[1] if y == start[0] else 0
            right = end[1] if y == end[0] else self.model.screen.columns
            result.append(''.join(rows[y][x].data for x in range(left,right)).rstrip())
        return '\n'.join(result)

    def copy_selection(self):
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def cell_at(self, pos):
        return (max(0,min(self.model.screen.lines-1,int((pos.y()-4)/self.cell_height))),
                max(0,min(self.model.screen.columns-1,int((pos.x()-self.grid_left())/self.cell_width))))

    def mouse_reporting(self, modifiers):
        return bool(self.model.private_modes & {9,1000,1002,1003}) and not modifiers & Qt.KeyboardModifier.ShiftModifier and self.verticalScrollBar().value() == self.verticalScrollBar().maximum()

    def report_mouse(self, button, pos, modifiers, release=False, motion=False):
        y,x = self.cell_at(pos)
        M = Qt.KeyboardModifier
        button += (4 if modifiers & M.ShiftModifier else 0)+(8 if modifiers & M.AltModifier else 0)+(16 if modifiers & M.ControlModifier else 0)
        if motion:
            button += 32
        if 1006 in self.model.private_modes:
            data = f'\x1b[<{button};{x+1};{y+1}{"m" if release else "M"}'.encode()
        else:
            if release:
                button = 3+(button & 28)
            values = (32+button,33+x,33+y)
            data = b'\x1b[M'+(''.join(chr(v) for v in values).encode('utf-8') if 1005 in self.model.private_modes else bytes(min(255,v) for v in values))
        self.transmit.emit(data)

    def _word_bounds(self, cell):
        y,x = cell
        row = self.rows()[y]
        def group(char):
            return 0 if char.isspace() else 1 if char.isalnum() or char == '_' else 2
        kind = group(row[x].data or ' ')
        left,right = x,x+1
        while left>0 and group(row[left-1].data or ' ') == kind:
            left -= 1
        while right<self.model.screen.columns and group(row[right].data or ' ') == kind:
            right += 1
        return (y,left),(y,right)

    def mousePressEvent(self, event):
        self.setFocus()
        if event.position().x() < self.grid_left()-3:
            return  # The timestamp gutter is neither a paste nor a remote mouse target.
        B,M = Qt.MouseButton,Qt.KeyboardModifier
        if event.button() == B.RightButton and (event.modifiers() & M.ControlModifier or self.options['mouse/mode']=='Windows'):
            self.open_menu(event.globalPosition().toPoint())
            return
        if self.mouse_reporting(event.modifiers()):
            button = {B.LeftButton:0,B.MiddleButton:1,B.RightButton:2}.get(event.button())
            if button is not None:
                self.report_button = button
                self.report_mouse(button,event.position(),event.modifiers())
            return
        if event.button() == B.RightButton:
            self.paste()
        elif event.button() == B.LeftButton:
            triple = time.monotonic()-self.last_double < QApplication.doubleClickInterval()/1000 and (event.position().toPoint()-self.last_double_pos).manhattanLength()<QApplication.startDragDistance()
            self.clear_selection()
            self.frozen_rows = copy.deepcopy(self.rows())
            self.anchor = self.selection_end = self.cell_at(event.position())
            self.selection_kind = 'line' if triple else 'char'
            if triple:
                y = self.anchor[0]
                self.anchor,self.selection_end = (y,0),(y,self.model.screen.columns)
                self.last_double = 0
            self.dragging = True
            self.viewport().update()

    def mouseDoubleClickEvent(self, event):
        if event.position().x() < self.grid_left()-3:
            return
        if event.button()!=Qt.MouseButton.LeftButton or self.mouse_reporting(event.modifiers()):
            self.mousePressEvent(event)
            return
        self.last_double,self.last_double_pos = time.monotonic(),event.position().toPoint()
        self.frozen_rows = copy.deepcopy(self.rows())
        self.anchor,self.selection_end = self._word_bounds(self.cell_at(event.position()))
        self.selection_kind,self.dragging = 'word',True
        self.viewport().update()

    def mouseMoveEvent(self, event):
        if event.position().x() < self.grid_left()-3 and not self.dragging:
            return
        if self.dragging:
            cell = self.cell_at(event.position())
            if self.selection_kind == 'word':
                bounds = self._word_bounds(cell)
                self.selection_end = bounds[0] if cell<self.anchor else bounds[1]
            elif self.selection_kind == 'line':
                self.selection_end = (cell[0],0 if cell<self.anchor else self.model.screen.columns)
            else:
                self.selection_end = cell
            self.viewport().update()
        elif self.mouse_reporting(event.modifiers()):
            modes = self.model.private_modes
            if 1003 in modes or (1002 in modes and self.report_button is not None):
                self.report_mouse(self.report_button if self.report_button is not None else 3,event.position(),event.modifiers(),motion=True)

    def mouseReleaseEvent(self, event):
        if self.dragging:
            self.dragging = False
            if self.options['mouse/mode']=='PuTTY':
                self.copy_selection()
        elif self.report_button is not None:
            if 9 not in self.model.private_modes:
                self.report_mouse(self.report_button,event.position(),event.modifiers(),release=True)
            self.report_button = None

    def wheelEvent(self, event):
        steps = event.angleDelta().y()/120
        if event.position().x() >= self.grid_left()-3 and self.mouse_reporting(event.modifiers()):
            for _ in range(max(1,abs(round(steps)))):
                self.report_mouse(64 if steps>0 else 65,event.position(),event.modifiers())
        else:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value()-round(steps*3))
        event.accept()

    def contextMenuEvent(self, event):
        if event.reason() == event.Reason.Keyboard:
            self.open_menu(event.globalPos())
        event.accept()

    def open_menu(self, position):
        menu = QMenu(self)
        menu.addAction('複製  Ctrl+Insert',self.copy_selection).setEnabled(bool(self.selected_text()))
        menu.addAction('貼上  Shift+Insert',self.paste)
        menu.addSeparator()
        menu.addAction('回到最新輸出',self.follow_output)
        menu.exec(position)

    def follow_output(self):
        self._following = True
        self._scroll_anchor = None
        self.clear_selection()
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

# ---------------- Explicit receive-only session logs ----------------
class SessionLogger:
    def __init__(self):
        self.handle = None
        self.state = 'text'
        self.at_line_start = True
        self.interval = 0

    def start(self, directory, interval=0, timestamp=False):
        self.stop()
        self.directory = persistent_path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.interval,self.timestamp = interval,timestamp
        self.state,self.at_line_start = 'text',True
        self._open()

    def _open(self):
        name = 'Session_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.txt'
        self.handle = (self.directory/name).open('x',encoding='utf-8',newline='')
        self.started = time.monotonic()

    def stop(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def write(self, text):
        if self.handle is None:
            return
        if self.interval and time.monotonic()-self.started>=self.interval:
            self.stop()
            self._open()
        output = []
        for c in text:
            if self.state == 'text':
                if c == '\x1b':
                    self.state = 'esc'
                    continue
                if c not in '\r\n\t' and not c.isprintable():
                    continue
                if self.at_line_start and c not in '\r\n':
                    if self.timestamp:
                        output.append(datetime.now().strftime('[%H:%M:%S] '))
                    self.at_line_start = False
                output.append(c)
                if c == '\n':
                    self.at_line_start = True
            elif self.state == 'esc':
                self.state = 'csi' if c=='[' else 'osc' if c==']' else 'charset' if c in '()*+%' else 'text'
            elif self.state == 'charset':
                self.state = 'text'
            elif self.state == 'csi' and '@'<=c<='~':
                self.state = 'text'
            elif self.state == 'osc':
                if c == '\x07':
                    self.state = 'text'
                elif c == '\x1b':
                    self.state = 'osc_esc'
            elif self.state == 'osc_esc':
                self.state = 'text' if c=='\\' else 'osc'
        self.handle.write(''.join(output))
        self.handle.flush()

# ---------------- Connection worker; UI communication only through signals ----------------
class ConnectionWorker(QThread):
    received = pyqtSignal(bytes)
    connected = pyqtSignal()
    problem = pyqtSignal(str)
    prompt = pyqtSignal(object)

    def __init__(self, config, size, parent=None):
        super().__init__(parent)
        self.config,self.size = dict(config),size
        self.loop = self.task = None
        self.stop_requested = False
        self.pending_prompt = None
        self.connection = self.writer = self.process = self.output = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self._session())
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.stop_requested:
                if isinstance(exc,(serial.SerialException, PermissionError)) and self.config.get('type') == 'Serial':
                    self.problem.emit('Serial 連線失敗：請確認 COM 埠、存取權限、接線及參數。若 COM 埠已被占用，請手動關閉占用該埠的程式後再重試。')
                elif isinstance(exc,asyncssh.HostKeyNotVerifiable):
                    self.problem.emit('SSH 主機金鑰未獲信任或已變更，已拒絕連線。請核對主機身分。')
                else:
                    self.problem.emit(f'連線失敗或中斷（{type(exc).__name__}）。請檢查位址、連接埠、驗證及網路。')
        finally:
            self._close_transport()
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            self.loop = None
            self.config.clear()

    async def ask(self, kind, message, echo=False):
        future = concurrent.futures.Future()
        self.pending_prompt = future
        self.prompt.emit({'kind':kind,'message':message,'echo':echo,'future':future})
        try:
            return await asyncio.wrap_future(future)
        finally:
            self.pending_prompt = None

    def trust_host(self, host, port, key):
        endpoint = f'{host}:{port}'
        public = key.export_public_key().decode().strip()
        trusted = self.config.get('host_keys',{}).get(endpoint)
        if trusted is not None:
            return trusted == public
        future = concurrent.futures.Future()
        self.pending_prompt = future
        self.prompt.emit({'kind':'hostkey','message':f'主機：{endpoint}\n類型：{key.get_algorithm()}\n指紋：{key.get_fingerprint()}\n\n請與管理者核對後決定是否信任並保存。',
                         'endpoint':endpoint,'public':public,'future':future})
        try:
            return bool(future.result(timeout=120))
        except (concurrent.futures.TimeoutError,concurrent.futures.CancelledError):
            return False
        finally:
            self.pending_prompt = None

    async def _session(self):
        if self.stop_requested:
            return
        self.output = asyncio.Queue(maxsize=1024)
        kind = self.config['type']
        if kind == 'Serial':
            cfg,flow = self.config,self.config['serial/flow']
            self.connection = serial.Serial(cfg['serial/port'],int(cfg['serial/baud']),
             bytesize=int(cfg['serial/data']),parity={'None':'N','Even':'E','Odd':'O','Mark':'M','Space':'S'}[cfg['serial/parity']],
             stopbits=float(cfg['serial/stop']),xonxoff=flow=='XON/XOFF',rtscts=flow=='RTS/CTS',dsrdtr=flow=='DSR/DTR',timeout=0,write_timeout=.5)
            self.connected.emit()
            while not self.stop_requested:
                for _ in range(64):
                    try:
                        data = self.output.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if self.connection.write(data)!=len(data):
                        raise serial.SerialTimeoutException('Incomplete serial write')
                data = self.connection.read(min(65536,self.connection.in_waiting))
                if data:
                    self.received.emit(data)
                await asyncio.sleep(.01)
            return
        if kind == 'SSH':
            owner = self
            class InteractiveClient(asyncssh.SSHClient):
                def validate_host_public_key(self, host, addr, port, key):
                    return owner.trust_host(host,port,key)
                async def password_auth_requested(self):
                    return await owner.ask('credential','SSH 密碼',False)
                def kbdint_auth_requested(self):
                    return ''
                async def kbdint_challenge_received(self, name, instructions, lang, prompts):
                    responses = []
                    for prompt,echo in prompts:
                        answer = await owner.ask('credential',f'{name}\n{instructions}\n{prompt}',echo)
                        if answer is None:
                            return None
                        responses.append(answer)
                    return responses
            username = await self.ask('credential','SSH 使用者名稱',True)
            if username is None:
                return
            self.connection = await asyncssh.connect(self.config['host'],port=self.config['port'],username=username,
             client_factory=InteractiveClient,known_hosts=(),client_keys=[],agent_path=None,login_timeout=120,connect_timeout=12)
            self.process = await self.connection.create_process(term_type='xterm-256color',term_size=self.size,encoding=None,stderr=asyncssh.STDOUT)
            reader,self.writer = self.process.stdout,self.process.stdin
        else:
            owner = self
            class SizedTelnetClient(telnetlib3.TelnetClient):
                def send_naws(self):
                    return owner.size[1],owner.size[0]
            reader,self.writer = await telnetlib3.open_connection(self.config['host'],self.config['port'],
             client_factory=SizedTelnetClient,encoding=False,term='xterm-256color',cols=self.size[0],rows=self.size[1],connect_timeout=12,connect_maxwait=1.0)
        self.connected.emit()
        sender = asyncio.create_task(self._send_network())
        read_task = None
        try:
            while not self.stop_requested:
                read_task = asyncio.create_task(reader.read(65536))
                done,_ = await asyncio.wait((read_task,sender),return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    read_task.cancel()
                    await asyncio.gather(read_task,return_exceptions=True)
                    await sender
                data = read_task.result()
                if not data:
                    break
                self.received.emit(data)
        finally:
            sender.cancel()
            if read_task:
                read_task.cancel()
            await asyncio.gather(sender,*([read_task] if read_task else []),return_exceptions=True)
            self._close_transport()
            if kind=='SSH' and self.connection:
                await self.connection.wait_closed()

    async def _send_network(self):
        while True:
            data = await self.output.get()
            self.writer.write(data)
            await self.writer.drain()

    def send(self, data):
        if self.loop and self.output is not None and not self.stop_requested:
            def enqueue():
                try:
                    self.output.put_nowait(data)
                except asyncio.QueueFull:
                    self.problem.emit('傳送佇列已滿，已停止連線；請減少貼上資料量後重連。')
                    self.task.cancel()
            try:
                self.loop.call_soon_threadsafe(enqueue)
            except RuntimeError:
                pass

    def resize_terminal(self, columns, lines):
        self.size = columns,lines
        def apply():
            try:
                if self.process:
                    self.process.change_terminal_size(columns,lines)
                elif self.writer and self.config.get('type')=='Telnet' and self.writer.local_option.enabled(NAWS):
                    # telnetlib3 5.0.1 has no public resize notifier. The callback is
                    # public; pinned _send_naws emits IAC-escaped negotiated NAWS.
                    self.writer._send_naws()
            except (OSError,RuntimeError):
                self.problem.emit('終端尺寸同步失敗，請重新連線。')
        if self.loop:
            try:
                self.loop.call_soon_threadsafe(apply)
            except RuntimeError:
                pass

    def stop(self):
        self.stop_requested = True
        if self.pending_prompt and not self.pending_prompt.done():
            try:
                self.pending_prompt.set_result(None)
            except concurrent.futures.InvalidStateError:
                pass
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass

    def _close_transport(self):
        for item in (self.writer,self.connection):
            if item is not None:
                try:
                    item.close()
                except (OSError,RuntimeError):
                    pass

# ---------------- UI 2.0 palette and vector actions ----------------
MODERN_COLORS = {
 'dark':dict(bg='#10151C',sidebar='#131B25',card='#19222E',input='#111A24',hover='#243348',border='#35465C',text='#E8EEF7',muted='#B0BFD2',accent='#96C0FF',primary='#285CC4',selected='#233E65'),
 'light':dict(bg='#F5F8FD',sidebar='#F2F7FE',card='#FFFFFF',input='#FBFCFF',hover='#EDF3FE',border='#DBE4F1',text='#202D46',muted='#61728D',accent='#326FF5',primary='#2F6EF6',selected='#E5EEFF')}

def line_icon(name, color):
    pix = QPixmap(24,24)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(color),1.65))
    if name=='sun':
        p.drawEllipse(QPointF(12,12),4,4)
        for angle in range(0,360,45):
            r = math.radians(angle)
            p.drawLine(QPointF(12+7*math.cos(r),12+7*math.sin(r)),QPointF(12+10*math.cos(r),12+10*math.sin(r)))
    elif name=='moon':
        path = QPainterPath(QPointF(17,3))
        path.cubicTo(1,0,0,22,17,21)
        path.cubicTo(7,17,7,7,17,3)
        p.drawPath(path)
    elif name=='close':
        p.drawLine(7,7,17,17)
        p.drawLine(7,17,17,7)
    elif name=='minimize':
        p.drawLine(6,15,18,15)
    elif name in ('maximize','restore'):
        if name=='restore':
            p.drawRect(9,5,10,10)
        p.drawRect(5,9 if name=='restore' else 6,10 if name=='restore' else 13,10 if name=='restore' else 12)
    elif name=='menu':
        for y in (6,12,18):
            p.drawLine(4,y,20,y)
    elif name=='terminal':
        p.drawRoundedRect(QRectF(2,4,20,16),2,2)
        p.drawLine(6,9,9,12)
        p.drawLine(9,12,6,15)
        p.drawLine(12,16,18,16)
    elif name=='settings':
        for y,x in ((6,8),(12,16),(18,10)):
            p.drawLine(3,y,21,y)
            p.drawEllipse(QPointF(x,y),2,2)
    elif name=='log':
        p.drawRoundedRect(QRectF(5,2,14,20),2,2)
        for y in (7,12,17):
            p.drawLine(8,y,16,y)
    elif name=='clock':
        p.drawEllipse(QPointF(12,12),9,9)
        p.drawLine(12,6,12,12)
        p.drawLine(12,12,17,15)
    elif name=='refresh':
        p.drawArc(QRectF(4,4,16,16),35*16,290*16)
        p.drawLine(20,3,20,10)
        p.drawLine(20,10,13,10)
    elif name=='exit':
        p.drawLine(10,3,3,3)
        p.drawLine(3,3,3,21)
        p.drawLine(3,21,10,21)
        p.drawLine(9,12,21,12)
        p.drawLine(16,7,21,12)
        p.drawLine(21,12,16,17)
    p.end()
    return QIcon(pix)

class TitleBar(QFrame):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setObjectName('titleBar')
        self.setFixedHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14,0,6,0)
        layout.setSpacing(4)
        logo = QLabel()
        logo.setPixmap(window.windowIcon().pixmap(QSize(26,26),self.devicePixelRatioF()))
        layout.addWidget(logo)
        layout.addWidget(QLabel('Pudding Terminal'))
        layout.addStretch()
        self.sun,self.moon = QPushButton(),QPushButton()
        for button,name,theme in ((self.sun,'淺色模式','light'),(self.moon,'深色模式','dark')):
            button.setFixedSize(38,36)
            button.setObjectName('themeButton')
            button.setToolTip(name)
            button.setAccessibleName(name)
            button.setCheckable(True)
            button.clicked.connect(lambda checked,value=theme: window.set_theme(value))
            layout.addWidget(button)
        self.min_btn,self.max_btn,self.close_btn = QPushButton(),QPushButton(),QPushButton()
        for button,label in ((self.min_btn,'最小化'),(self.max_btn,'最大化'),(self.close_btn,'關閉')):
            button.setFixedSize(38,36)
            button.setObjectName('windowButton')
            button.setToolTip(label)
            button.setAccessibleName(label)
            layout.addWidget(button)
        self.close_btn.setObjectName('closeButton')
        self.close_btn.installEventFilter(self)
        for button in (self.sun,self.moon,self.min_btn,self.max_btn,self.close_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.min_btn.clicked.connect(window.showMinimized)
        self.max_btn.clicked.connect(lambda checked=False: window.toggle_maximize_window())
        self.close_btn.clicked.connect(window.close)

    def eventFilter(self, watched, event):
        if watched is self.close_btn and event.type() in (QEvent.Type.Enter,QEvent.Type.Leave):
            color = '#ffffff' if event.type()==QEvent.Type.Enter else MODERN_COLORS[self.window.effective_theme]['text']
            self.close_btn.setIcon(line_icon('close',color))
        return super().eventFilter(watched,event)

    def mousePressEvent(self, event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.window.toggle_maximize_window()
class ModernWindow(QMainWindow):
    """保留原生 Windows frame style，僅移除標題列的 client inset。"""
    def __init__(self):
        super().__init__()
        self._native_ready = False
        self._native_attempted = False
        self._snap_pressed = False
        self._maximize_toggle_pending = False
        if sys.platform == "win32":
            # Keep Qt's Windows platform plugin in native-frame mode. A frameless
            # Qt flag plus manual WS_CAPTION made showMaximized change geometry
            # without setting the native WS_MAXIMIZE state (IsZoomed=False).
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowTitleHint
                                | Qt.WindowType.WindowSystemMenuHint
                                | Qt.WindowType.WindowMinMaxButtonsHint
                                | Qt.WindowType.WindowCloseButtonHint)

    def toggle_maximize_window(self, maximized=None):
        # Native caption double-click and Qt buttons must use the same Qt state.
        if self._maximize_toggle_pending:
            return
        self._maximize_toggle_pending = True
        def apply():
            try:
                target = not self.isMaximized() if maximized is None else maximized
                self.showMaximized() if target else self.showNormal()
                self._update_window_chrome()
            finally:
                self._maximize_toggle_pending = False
        QTimer.singleShot(0, apply)

    def showMaximized(self):
        if not self.isMaximized() and not self.isMinimized():
            # Native Windows normal placement includes its invisible resize frame;
            # preserve the actual Qt client rectangle for preference restoration.
            self._normal_rect_before_maximize = QRect(self.geometry())
        super().showMaximized()

    def _enable_native_frame(self):
        if self._native_attempted or sys.platform != "win32":
            return
        self._native_attempted = True
        try:
            from ctypes import wintypes
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            self._user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            self._user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            self._user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_int, wintypes.UINT]
            self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            self._user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            self._user32.IsZoomed.argtypes = [wintypes.HWND]
            self._user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            self._user32.MonitorFromWindow.restype = wintypes.HANDLE
            self._user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            hwnd = int(self.winId())
            self._native_ready = True
            if not self._user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037):
                raise ctypes.WinError(ctypes.get_last_error())
        except (OSError, AttributeError):
            self._native_ready = False
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
            self.show()  # 無法提供可靠原生命中時退回系統 frame，不自行模擬拖曳。
        self._update_window_chrome()

    def _monitor_work_area(self, hwnd):
        from ctypes import wintypes
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        monitor = self._user32.MonitorFromWindow(hwnd, 2)
        return info if self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)) else None

    def _update_window_chrome(self):
        if hasattr(self, "title_bar"):
            colors = MODERN_COLORS[getattr(self, "effective_theme", "dark")]
            self.title_bar.min_btn.setIcon(line_icon("minimize", colors["text"]))
            self.title_bar.max_btn.setIcon(line_icon("restore" if self.isMaximized() else "maximize", colors["text"]))
            self.title_bar.max_btn.setToolTip((("Restore" if self.isMaximized() else "Maximize") if getattr(self, "current_lang", "zh_tw") == "en" else ("還原" if self.isMaximized() else "最大化")))
            self.title_bar.max_btn.setAccessibleName(self.title_bar.max_btn.toolTip())
            self.title_bar.close_btn.setIcon(line_icon("close", colors["text"]))
        if sys.platform == "win32" and self._native_ready:
            try:
                from ctypes import wintypes
                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                     ctypes.c_void_p, wintypes.DWORD]
                dark = ctypes.c_int(getattr(self, "effective_theme", "dark") == "dark")
                corner = ctypes.c_int(1 if self.isMaximized() else 2)
                dwm.DwmSetWindowAttribute(int(self.winId()), 20, ctypes.byref(dark), 4)
                self._corner_result = dwm.DwmSetWindowAttribute(int(self.winId()), 33,
                                                               ctypes.byref(corner), 4)
            except (OSError, AttributeError):
                self._corner_result = -1

    def nativeEvent(self, event_type, message):
        if not self._native_ready:
            return False, 0
        from ctypes import wintypes
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == 0x0112 and (msg.wParam & 0xFFF0) in (0xF030, 0xF120):
            # Route the system menu/Windows command through the same Qt state as
            # buttons and caption double-clicks; DefWindowProc alone can leave
            # Qt's WindowMaximized flag behind the native geometry.
            target = (msg.wParam & 0xFFF0) == 0xF030
            if not target and self.isMinimized():
                target = bool(self.windowState() & Qt.WindowState.WindowMaximized)
            self.toggle_maximize_window(target)
            return True, 0
        if msg.message == 0x00A3 and msg.wParam == 2:  # WM_NCLBUTTONDBLCLK / HTCAPTION
            self.toggle_maximize_window()
            return True, 0
        if msg.message == 0x0083 and msg.wParam:  # WM_NCCALCSIZE
            if self._user32.IsZoomed(msg.hWnd):
                info = self._monitor_work_area(msg.hWnd)
                if info:
                    rect = wintypes.RECT.from_address(msg.lParam)
                    rect.left, rect.top = info.work.left, info.work.top
                    rect.right, rect.bottom = info.work.right, info.work.bottom
            return True, 0
        if msg.message == 0x0024:  # WM_GETMINMAXINFO：physical pixel 工作區
            class MinMaxInfo(ctypes.Structure):
                _fields_ = [(n, wintypes.POINT) for n in ("reserved", "max_size", "max_pos", "min_track", "max_track")]
            info = self._monitor_work_area(msg.hWnd)
            if info:
                mm = MinMaxInfo.from_address(msg.lParam)
                mm.max_pos.x = info.work.left - info.monitor.left
                mm.max_pos.y = info.work.top - info.monitor.top
                mm.max_size.x = info.work.right - info.work.left
                mm.max_size.y = info.work.bottom - info.work.top
                mm.min_track.x = round(self.minimumWidth() * self.devicePixelRatioF())
                mm.min_track.y = round(self.minimumHeight() * self.devicePixelRatioF())
                return True, 0
        if msg.message == 0x0084 and hasattr(self, "title_bar"):  # WM_NCHITTEST
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            self._user32.GetWindowRect(msg.hWnd, ctypes.byref(rect))
            border = max(6, round(6 * self.devicePixelRatioF()))
            if not self.isMaximized() and not self.isFullScreen():
                left, right = x < rect.left + border, x >= rect.right - border
                top, bottom = y < rect.top + border, y >= rect.bottom - border
                for hit, active in ((13, top and left), (14, top and right),
                                    (16, bottom and left), (17, bottom and right),
                                    (10, left), (11, right), (12, top), (15, bottom)):
                    if active:
                        return True, hit
            point = wintypes.POINT(x, y)
            self._user32.ScreenToClient(msg.hWnd, ctypes.byref(point))
            local = QPoint(round(point.x / self.devicePixelRatioF()), round(point.y / self.devicePixelRatioF()))
            child = self.childAt(local)
            if child is self.title_bar.max_btn:
                return True, 9  # HTMAXBUTTON：Windows 11 Snap Layout 的原生入口
            if self.title_bar.geometry().contains(local) and not isinstance(child, (QPushButton, QComboBox)):
                return True, 2  # HTCAPTION：原生拖曳、雙擊、貼齊與系統選單
        if msg.message == 0x00A1 and msg.wParam == 9:  # WM_NCLBUTTONDOWN
            self._snap_pressed = True
            self.title_bar.max_btn.setDown(True)
            return True, 0
        if msg.message in (0x00A0, 0x02A2):  # WM_NCMOUSEMOVE／WM_NCMOUSELEAVE
            hovered = msg.message == 0x00A0 and msg.wParam == 9
            button = self.title_bar.max_btn
            if bool(button.property("snapHover")) != hovered:
                button.setProperty("snapHover", hovered)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
            if hovered:
                class TrackMouseEvent(ctypes.Structure):
                    _fields_ = [("size", wintypes.DWORD), ("flags", wintypes.DWORD),
                                ("window", wintypes.HWND), ("hover_time", wintypes.DWORD)]
                tracking = TrackMouseEvent(ctypes.sizeof(TrackMouseEvent), 0x12, msg.hWnd, 0)
                self._user32.TrackMouseEvent.argtypes = [ctypes.c_void_p]
                self._user32.TrackMouseEvent(ctypes.byref(tracking))
            elif self._snap_pressed:
                self._snap_pressed = False
                button.setDown(False)
        if msg.message == 0x00A2 and msg.wParam == 9:  # WM_NCLBUTTONUP
            pressed, self._snap_pressed = self._snap_pressed, False
            self.title_bar.max_btn.setDown(False)
            if pressed:
                self.toggle_maximize_window()
            return True, 0
        if msg.message in (0x001F, 0x0215):  # CANCELMODE／CAPTURECHANGED
            self._snap_pressed = False
            if hasattr(self, "title_bar"):
                self.title_bar.max_btn.setDown(False)
        return False, 0

    def show_system_menu(self):
        if not self._native_ready:
            return
        from ctypes import wintypes
        self._user32.GetSystemMenu.argtypes = [wintypes.HWND, wintypes.BOOL]
        self._user32.GetSystemMenu.restype = wintypes.HMENU
        self._user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                               wintypes.HWND, ctypes.c_void_p]
        self._user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        point = wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(point))
        menu = self._user32.GetSystemMenu(int(self.winId()), False)
        command = self._user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y,
                                              0, int(self.winId()), None)
        if command:
            self._user32.PostMessageW(int(self.winId()), 0x0112, command, 0)

    def showEvent(self, event):
        super().showEvent(event)
        self._enable_native_frame()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_window_chrome()



# ---------------- Compact settings dialog; no credential fields ----------------
class SettingsCategoryCombo(QComboBox):
    """Keep the category list below its control, independent of QSS menu mode."""
    def showPopup(self):
        super().showPopup()
        self._place_popup()
        # The internal container completes its layout on the next Qt event turn.
        QTimer.singleShot(0,self._place_popup)

    def _place_popup(self):
        popup = self.view().window()
        if not popup.isVisible():
            return
        origin = self.mapToGlobal(QPoint())
        screen = QApplication.screenAt(origin) or self.screen()
        area = screen.availableGeometry()
        below = origin.y()+self.height()
        below_space = max(0,area.bottom()+1-below)
        above_space = max(0,origin.y()-area.top())
        height = popup.height()
        width = min(self.width(),area.width())
        if height <= below_space:
            y = below
        elif height <= above_space:
            y = origin.y()-height
        elif below_space >= above_space:
            height,y = max(1,below_space),below
        else:
            height = max(1,above_space)
            y = origin.y()-height
        x = max(area.left(),min(origin.x(),area.right()-width+1))
        popup.setGeometry(x,y,width,height)


class SettingsDialog(QDialog):
    def __init__(self, values, page='terminal', connected=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle('設定')
        self.resize(590,590)
        self.controls = {}
        layout = QVBoxLayout(self)
        pages = SettingsCategoryCombo()
        self.category = pages
        pages.addItems(['終端與操作','Serial 進階','日誌'])
        layout.addWidget(pages)
        stack = QStackedWidget()
        layout.addWidget(stack,1)
        forms = []
        for _ in range(3):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            form = QFormLayout(content)
            form.setSpacing(14)
            form.setContentsMargins(18,18,18,18)
            scroll.setWidget(content)
            stack.addWidget(scroll)
            forms.append(form)
        def combo(form, key, label, items):
            control = QComboBox()
            control.addItems(items)
            control.setCurrentText(str(values[key]))
            self.controls[key] = control
            form.addRow(label,control)
            return control
        def spin(form, key, label, low, high, fractional=False):
            control = QDoubleSpinBox() if fractional else QSpinBox()
            control.setRange(low,high)
            control.setValue(values[key])
            self.controls[key] = control
            form.addRow(label,control)
            return control
        f = forms[0]
        timestamps = QCheckBox('顯示時間戳記')
        timestamps.setChecked(values['terminal/timestamp'])
        self.controls['terminal/timestamp'] = timestamps
        f.addRow(timestamps)
        timestamp_hint = QLabel('本機時間 [HH:MM:SS] 顯示於獨立時間欄，日誌每行時間戳同步開關；\n不加入設備字元串流，Alternate Screen 期間隱藏畫面時間文字。')
        timestamp_hint.setWordWrap(True)
        f.addRow(timestamp_hint)
        font = QFontComboBox()
        font.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        font.setCurrentFont(QFont(values['terminal/font']))
        self.controls['terminal/font'] = font
        f.addRow('等寬字型',font)
        spin(f,'terminal/size','字體大小',8,36)
        for key,label in (('terminal/foreground','預設文字顏色'),('terminal/background','終端背景顏色')):
            button = QPushButton(values[key])
            button.clicked.connect(lambda checked=False,b=button: self.choose_color(b))
            self.controls[key] = button
            f.addRow(label,button)
        combo(f,'terminal/encoding','文字編碼',['utf-8','big5','gb18030','cp437','latin-1']).setEnabled(not connected)
        spin(f,'terminal/history','歷史行數',100,50000).setEnabled(not connected)
        combo(f,'keyboard/backspace','Backspace',['DEL','BS'])
        combo(f,'keyboard/enter','Enter / 貼上換行',['CR','LF','CRLF'])
        combo(f,'keyboard/function','功能鍵 / Home / End',['PuTTY','xterm'])
        combo(f,'mouse/mode','滑鼠與貼上模式',['PuTTY','Windows'])
        hint = QLabel('Ctrl+C 永遠傳送中斷。PuTTY 模式 Ctrl+V 傳送 0x16；\nWindows 模式 Ctrl+V 貼上，右鍵開啟選單。\nShift+Insert 貼上；Ctrl+Insert / Ctrl+Shift+C 複製。')
        hint.setWordWrap(True)
        f.addRow(hint)
        f = forms[1]
        for key,label,items in (
            ('serial/data','Data Bits',['5','6','7','8']),
            ('serial/parity','Parity',['None','Even','Odd','Mark','Space']),
            ('serial/stop','Stop Bits',['1','1.5','2']),
            ('serial/flow','Flow Control',['None','RTS/CTS','XON/XOFF','DSR/DTR'])):
            combo(f,key,label,items).setEnabled(not connected)
        hint = QLabel('連線中無法更改 Serial 參數、編碼及歷史容量。\nCOM 埠存取遭拒時，可選擇處理可能占用的程序。')
        hint.setWordWrap(True)
        f.addRow(hint)
        f = forms[2]
        directory = QLineEdit(str(values['log/directory']))
        self.controls['log/directory'] = directory
        row = QHBoxLayout()
        row.addWidget(directory,1)
        browse = QPushButton('瀏覽')
        browse.clicked.connect(lambda: self.browse_directory(directory))
        row.addWidget(browse)
        f.addRow('記錄目錄',row)
        control = QCheckBox('啟用自動換檔')
        control.setChecked(values['log/rotate'])
        self.controls['log/rotate'] = control
        f.addRow(control)
        spin(f,'log/interval','換檔間隔',.1,999,True)
        combo(f,'log/unit','單位',['分鐘','小時'])
        hint = QLabel('日誌只在手動開始後寫入，重新啟動或重新連線不會自動記錄。\n只記錄接收文字，移除 ANSI 控制碼。日誌每行時間戳跟隨上方「時間戳記」按鈕，\n不改動終端機收到的字元串流。\n請在登入完成後開始紀錄；遠端回顯的敏感文字仍可能被記錄。')
        hint.setWordWrap(True)
        f.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        pages.currentIndexChanged.connect(stack.setCurrentIndex)
        pages.setCurrentIndex({'terminal':0,'serial':1,'log':2}[page])

    def choose_color(self, button):
        color = QColorDialog.getColor(QColor(button.text()),self,'選擇顏色')
        if color.isValid():
            button.setText(color.name())

    def browse_directory(self, entry):
        try:
            directory = str(persistent_path(entry.text() or 'device_logs'))
        except (ValueError,OSError):
            directory = str(APP_DIR)
        selected = QFileDialog.getExistingDirectory(self,'選擇日誌目錄',directory)
        if selected:
            entry.setText(selected)

    def values(self):
        result = {}
        for key,c in self.controls.items():
            if isinstance(c,QFontComboBox):
                result[key] = c.currentFont().family()
            elif isinstance(c,QComboBox):
                result[key] = c.currentText()
            elif isinstance(c,QCheckBox):
                result[key] = c.isChecked()
            elif isinstance(c,(QSpinBox,QDoubleSpinBox)):
                result[key] = c.value()
            else:
                result[key] = c.text()
        persistent_path(result['log/directory'])
        return result


class MainWindow(ModernWindow):
    def __init__(self, preferences=None):
        super().__init__()
        self.preferences = preferences or Preferences()
        self.values = {key:self.preferences.get(key) for key in DEFAULTS}
        self.effective_theme = self.preferences.get('appearance/theme')
        if self.effective_theme not in ('light','dark'):
            self.effective_theme = system_theme()
        self.worker = None
        self.auth_dialog = None
        self.closing = False
        self.connected_state = False
        self.logger = SessionLogger()
        self.setWindowTitle('Pudding Terminal')
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.setMinimumSize(780,480)
        self.resize(1160,790)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)
        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)
        body = QHBoxLayout()
        body.setContentsMargins(0,0,0,0)
        body.setSpacing(0)
        root.addLayout(body,1)
        self.setCentralWidget(central)
        work = QWidget()
        layout = QVBoxLayout(work)
        layout.setContentsMargins(12,10,12,8)
        layout.setSpacing(8)
        body.addWidget(work,1)
        self.connection_bar = QFrame()
        self.connection_bar.setObjectName('card')
        row = QHBoxLayout(self.connection_bar)
        row.setContentsMargins(8,8,8,8)
        row.setSpacing(6)
        self.connection_fields = QWidget()
        fields = QHBoxLayout(self.connection_fields)
        fields.setContentsMargins(0,0,0,0)
        self.kind = QComboBox()
        self.kind.addItems(['Serial','SSH','Telnet'])
        self.kind.setMaximumWidth(96)
        self.kind.setCurrentText(self.values['connection/type'])
        self.kind.setAccessibleName('連線類型')
        fields.addWidget(self.kind)
        self.connection_pages = QStackedWidget()
        fields.addWidget(self.connection_pages,1)
        serial_page = QWidget()
        sr = QHBoxLayout(serial_page)
        sr.setContentsMargins(0,0,0,0)
        sr.setSpacing(6)
        self.com = QComboBox()
        self.com.setEditable(True)
        self.com.setMinimumWidth(74)
        self.com.setMaximumWidth(170)
        self.com.setAccessibleName('COM Port')
        self.com.currentTextChanged.connect(self.com.setToolTip)
        self.refresh_ports()
        self.com.setCurrentText(self.values['serial/port'])
        sr.addWidget(self.com,1)
        self.refresh_button = QPushButton()
        self.refresh_button.setFixedSize(30,32)
        self.refresh_button.setToolTip('重新整理 COM Port')
        self.refresh_button.setAccessibleName('重新整理 COM Port')
        self.refresh_button.clicked.connect(self.refresh_ports)
        sr.addWidget(self.refresh_button)
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(['1200','2400','4800','9600','19200','38400','57600','115200','230400','460800','921600'])
        self.baud.setCurrentText(str(self.values['serial/baud']))
        self.baud.setAccessibleName('Baud Rate')
        self.baud.setMinimumWidth(80)
        self.baud.setMaximumWidth(110)
        sr.addWidget(self.baud)
        sr.addStretch(1)
        self.connection_pages.addWidget(serial_page)
        self.host_entries,self.port_entries = {},{}
        for kind in ('SSH','Telnet'):
            page = QWidget()
            net = QHBoxLayout(page)
            net.setContentsMargins(0,0,0,0)
            net.setSpacing(6)
            host = QLineEdit(str(self.values[kind.lower()+'/host']))
            host.setPlaceholderText('主機名稱或 IP 位址')
            host.setAccessibleName(kind+' 主機')
            port = QSpinBox()
            port.setRange(1,65535)
            port.setValue(self.values[kind.lower()+'/port'])
            port.setAccessibleName(kind+' Port')
            port.setMaximumWidth(100)
            net.addWidget(host,1)
            net.addWidget(port)
            self.host_entries[kind],self.port_entries[kind] = host,port
            self.connection_pages.addWidget(page)
        self.kind.currentIndexChanged.connect(self.connection_pages.setCurrentIndex)
        self.connection_pages.setCurrentIndex(self.kind.currentIndex())
        row.addWidget(self.connection_fields,1)
        self.connect_button = QPushButton('連線')
        self.connect_button.setObjectName('primary')
        self.connect_button.setMinimumWidth(64)
        self.connect_button.setFixedHeight(34)
        self.connect_button.clicked.connect(self.toggle_connection)
        row.addWidget(self.connect_button)
        tools = QWidget()
        tools.setObjectName('connectionTools')
        tool_layout = QHBoxLayout(tools)
        tool_layout.setContentsMargins(0,0,0,0)
        tool_layout.setSpacing(4)
        self.settings_button,self.timestamp_button = QPushButton(),QPushButton('時間戳記')
        for button,label in ((self.settings_button,'設定'),(self.timestamp_button,'顯示時間戳記')):
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setFixedHeight(34)
            button.setIconSize(QSize(18,18))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            tool_layout.addWidget(button)
        self.settings_button.setFixedWidth(34)
        self.settings_button.clicked.connect(lambda: self.open_settings('terminal'))
        self.timestamp_button.setCheckable(True)
        self.timestamp_button.setChecked(self.values['terminal/timestamp'])
        self.timestamp_button.toggled.connect(self.set_timestamps)
        row.addWidget(tools)
        self.log_button = QPushButton('開始紀錄')
        self.log_button.setFixedHeight(34)
        self.log_button.setMinimumWidth(88)
        self.log_button.setAccessibleName('開始或停止日誌紀錄')
        self.log_button.clicked.connect(self.toggle_logging)
        row.addWidget(self.log_button)
        layout.addWidget(self.connection_bar)
        self.terminal = TerminalWidget(self.values)
        layout.addWidget(self.terminal,1)
        self.terminal.transmit.connect(self.send_data)
        self.terminal.sizeChanged.connect(self.terminal_resized)
        self.terminal.textReceived.connect(self.log_received)
        footer = QHBoxLayout()
        self.status = QLabel('未連線')
        self.status.setAccessibleName('連線狀態')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        footer.addWidget(self.status,1)
        self.grid_size = QLabel('80 × 24')
        footer.addWidget(self.grid_size)
        layout.addLayout(footer)
        self.apply_theme()
        self.sync_timestamp_button()
        self.restore_window()
        QTimer.singleShot(0,self.terminal.setFocus)

    def refresh_ports(self):
        current = self.com.currentText() if hasattr(self,'com') else ''
        if not hasattr(self,'com'):
            return
        self.com.clear()
        self.com.addItems([port.device for port in serial.tools.list_ports.comports()])
        self.com.setCurrentText(current or self.values['serial/port'])

    def set_theme(self, theme):
        self.effective_theme = theme
        self.apply_theme()
        try:
            self.preferences.save({'appearance/theme':theme})
        except OSError as exc:
            QMessageBox.warning(self,'無法儲存',str(exc))

    def apply_theme(self):
        c = MODERN_COLORS[self.effective_theme]
        palette = QPalette()
        for role,key in ((QPalette.ColorRole.Window,'bg'),(QPalette.ColorRole.WindowText,'text'),
            (QPalette.ColorRole.Base,'input'),(QPalette.ColorRole.AlternateBase,'card'),
            (QPalette.ColorRole.Text,'text'),(QPalette.ColorRole.Button,'card'),(QPalette.ColorRole.ButtonText,'text'),
            (QPalette.ColorRole.Highlight,'primary'),(QPalette.ColorRole.HighlightedText,None),
            (QPalette.ColorRole.ToolTipBase,'card'),(QPalette.ColorRole.ToolTipText,'text')):
            palette.setColor(role,QColor(c[key] if key else '#ffffff'))
        palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.Text,QColor(c['muted']))
        palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.ButtonText,QColor(c['muted']))
        QApplication.instance().setPalette(palette)
        self.setStyleSheet(f'''
          QWidget {{ color:{c['text']}; font-family:"Segoe UI","Microsoft JhengHei UI"; font-size:10pt; }}
          QMainWindow,QDialog {{ background:{c['bg']}; }}
          #titleBar {{ background:{c['sidebar']}; }}
          #card {{ background:{c['card']}; border:1px solid {c['border']}; border-radius:10px; }}
          QPushButton {{ background:{c['card']}; border:1px solid {c['border']}; border-radius:6px; padding:6px 10px; }}
          QPushButton:hover {{ background:{c['hover']}; }}
          QPushButton:checked {{ background:{c['selected']}; border-color:{c['accent']}; }}
          QPushButton:focus {{ border-color:{c['accent']}; }}
          QPushButton:disabled {{ color:{c['muted']}; }}
          #primary {{ background:{c['primary']}; color:white; border:0; }}
          #titleBar QPushButton {{ background:transparent; border:1px solid transparent; border-radius:6px; padding:0; }}
          #titleBar QPushButton:hover,#titleBar QPushButton[snapHover="true"] {{ background:{c['hover']}; }}
          #titleBar QPushButton:checked {{ background:{c['selected']}; border-color:{c['border']}; }}
          #titleBar QPushButton:focus {{ border-color:{c['accent']}; }}
          #titleBar #closeButton:hover {{ background:#C42B3D; }}
          #titleBar #closeButton {{ border:0; }}
          #connectionTools QPushButton {{ padding:3px; border-radius:6px; }}
          QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox {{ background:{c['input']}; border:1px solid {c['border']}; border-radius:6px; padding:5px; min-height:20px; }}
          QComboBox QAbstractItemView,QMenu,QToolTip {{ background:{c['card']}; color:{c['text']}; border:1px solid {c['border']}; }}
          QMenu::item:selected {{ background:{c['selected']}; }}
          QScrollArea {{ border:0; }}
          QScrollBar:vertical {{ background:{c['bg']}; width:12px; }}
          QScrollBar::handle:vertical {{ background:{c['border']}; min-height:28px; border-radius:5px; }}
          QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
        ''')
        self.title_bar.sun.setIcon(line_icon('sun',c['text']))
        self.title_bar.moon.setIcon(line_icon('moon',c['text']))
        self.title_bar.sun.setChecked(self.effective_theme=='light')
        self.title_bar.moon.setChecked(self.effective_theme=='dark')
        for button,name in ((self.settings_button,'settings'),(self.timestamp_button,'clock')):
            button.setIcon(line_icon(name,c['text']))
        self.refresh_button.setIcon(line_icon('refresh',c['text']))
        self._update_window_chrome()

    def sync_timestamp_button(self):
        enabled = self.values['terminal/timestamp']
        self.timestamp_button.blockSignals(True)
        self.timestamp_button.setChecked(enabled)
        self.timestamp_button.setToolTip('畫面與日誌時間戳記：' + ('開啟' if enabled else '關閉'))
        self.timestamp_button.blockSignals(False)

    def set_timestamps(self, enabled):
        try:
            self.preferences.save({'terminal/timestamp':bool(enabled)})
        except OSError as exc:
            self.sync_timestamp_button()
            QMessageBox.warning(self,'無法儲存',str(exc))
            return
        self.values['terminal/timestamp'] = bool(enabled)
        if self.logger.handle:
            self.logger.timestamp = bool(enabled)
        self.terminal.set_timestamps(enabled)
        self.sync_timestamp_button()
        self.terminal.setFocus()

    def collect_values(self):
        self.values.update({'connection/type':self.kind.currentText(),'serial/port':self.com.currentText().strip(),
            'serial/baud':int(self.baud.currentText())})
        for kind in ('SSH','Telnet'):
            self.values[kind.lower()+'/host'] = self.host_entries[kind].text().strip()
            self.values[kind.lower()+'/port'] = self.port_entries[kind].value()
        return dict(self.values)

    def open_settings(self, page='terminal'):
        try:
            values = self.collect_values()
        except ValueError:
            QMessageBox.warning(self,'參數錯誤','Baud Rate 必須為正整數。')
            return
        dialog = SettingsDialog(values,page,self.worker is not None,self)
        if dialog.exec()!=QDialog.DialogCode.Accepted:
            return
        try:
            changes = dialog.values()
            self.preferences.save(changes)
        except (ValueError,OSError) as exc:
            QMessageBox.warning(self,'設定未套用',str(exc))
            return
        self.values.update(changes)
        old_encoding = self.terminal.options['terminal/encoding']
        old_history = self.terminal.options['terminal/history']
        self.terminal.options.update({key:value for key,value in changes.items() if key!='terminal/timestamp'})
        if old_encoding!=changes['terminal/encoding'] or old_history!=changes['terminal/history']:
            self.terminal.reset_session()
        self.terminal.set_appearance()
        self.terminal.set_timestamps(changes['terminal/timestamp'])
        if self.logger.handle:
            self.logger.timestamp = changes['terminal/timestamp']
        self.sync_timestamp_button()
        self.terminal.setFocus()
        if self.logger.handle:
            interval = changes['log/interval']*(60 if changes['log/unit']=='分鐘' else 3600) if changes['log/rotate'] else 0
            try:
                directory = persistent_path(changes['log/directory'])
                if directory != self.logger.directory:
                    replacement = SessionLogger()
                    replacement.start(directory,interval,changes['terminal/timestamp'])
                    self.logger.stop()
                    self.logger = replacement
                    self.log_button.setToolTip(str(directory))
                else:
                    self.logger.interval = interval
                    self.logger.timestamp = changes['terminal/timestamp']
            except OSError as exc:
                QMessageBox.warning(self,'日誌仍使用原設定',str(exc))

    def toggle_connection(self):
        if self.worker is not None:
            self.connect_button.setEnabled(False)
            self.status.setText('正在中斷連線…')
            self.worker.stop()
            return
        try:
            values = self.collect_values()
            if not values['serial/port'] or int(values['serial/baud'])<=0:
                raise ValueError('請確認 COM Port 與 Baud Rate。')
            kind = self.kind.currentText()
            config = dict(values,type=kind)
            if kind!='Serial':
                config.update(host=values[kind.lower()+'/host'],port=values[kind.lower()+'/port'])
                if not config['host']:
                    raise ValueError('請輸入主機名稱或 IP 位址。')
            keys = json.loads(self.preferences.get('ssh/host_keys','{}'))
            if not isinstance(keys,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in keys.items()):
                raise ValueError('SSH 主機金鑰設定格式不正確；原檔保留。')
            config['host_keys'] = keys
        except ValueError as exc:
            QMessageBox.warning(self,'無法連線',str(exc))
            return
        if kind == 'Telnet':
            answer = QMessageBox.warning(
                self, 'Telnet 明文連線',
                'Telnet 不會加密登入資訊或工作階段內容。請只在你有權使用的隔離環境連線。確定要繼續嗎？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self.preferences.save(values)
        except OSError as exc:
            QMessageBox.warning(self,'無法連線',str(exc))
            return
        self.terminal.reset_session()
        self.status.setText('連線中…')
        self.connection_fields.setEnabled(False)
        self.connect_button.setText('中斷')
        self.connected_state = False
        worker = ConnectionWorker(config,(self.terminal.model.screen.columns,self.terminal.model.screen.lines),self)
        self.worker = worker
        worker.received.connect(self.receive_data)
        worker.connected.connect(self.connection_ready)
        worker.problem.connect(self.connection_problem)
        worker.prompt.connect(self.authentication_prompt)
        worker.finished.connect(self.connection_finished)
        worker.start()
        self.terminal.setFocus()

    def connection_ready(self):
        self.connected_state = True
        kind = self.kind.currentText()
        endpoint = self.com.currentText() if kind=='Serial' else self.host_entries[kind].text()
        self.status.setText(f'已連線 · {kind} · {endpoint}')
        self.terminal.setFocus()

    def connection_problem(self, message):
        self.status.setText(message)

    def receive_data(self, data):
        self.terminal.feed(data)

    def send_data(self, data):
        if self.worker is not None and self.connected_state:
            self.worker.send(data)

    def terminal_resized(self, columns, lines):
        self.grid_size.setText(f'{columns} × {lines}')
        if self.worker:
            self.worker.resize_terminal(columns,lines)

    def authentication_prompt(self, request):
        future = request['future']
        if future.done() or self.closing:
            return
        answer = None
        if request['kind']=='hostkey':
            box = QMessageBox(QMessageBox.Icon.Question,'SSH 主機金鑰確認',request['message'],
             QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,self)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            self.auth_dialog = box
            if box.exec()==QMessageBox.StandardButton.Yes:
                try:
                    keys = json.loads(self.preferences.get('ssh/host_keys','{}'))
                    keys[request['endpoint']] = request['public']
                    self.preferences.save({'ssh/host_keys':json.dumps(keys)})
                    answer = True
                except (ValueError,OSError) as exc:
                    QMessageBox.warning(self,'無法保存信任',str(exc))
        else:
            dialog = QInputDialog(self)
            dialog.setWindowTitle('SSH 互動登入')
            dialog.setLabelText(request['message'])
            dialog.setTextEchoMode(QLineEdit.EchoMode.Normal if request['echo'] else QLineEdit.EchoMode.Password)
            self.auth_dialog = dialog
            if dialog.exec()==QDialog.DialogCode.Accepted:
                answer = dialog.textValue()
            dialog.setTextValue('')
            dialog.deleteLater()
        self.auth_dialog = None
        if not future.done():
            try:
                future.set_result(answer)
            except concurrent.futures.InvalidStateError:
                pass
        if answer is None and self.worker:
            self.worker.stop()

    def connection_finished(self):
        if self.auth_dialog:
            self.auth_dialog.reject()
        worker,self.worker = self.worker,None
        self.connected_state = False
        if worker:
            worker.deleteLater()
        tail = self.terminal.model.finish()
        if tail:
            self.log_received(tail)
        self.logger.stop()
        self.log_button.setText('開始紀錄')
        self.connect_button.setText('連線')
        self.connect_button.setEnabled(True)
        self.connection_fields.setEnabled(True)
        if self.status.text().startswith(('已連線','連線中','正在中斷')):
            self.status.setText('連線已中斷')
        if self.closing:
            QTimer.singleShot(0,self.close)

    def toggle_logging(self):
        if self.logger.handle:
            self.logger.stop()
            self.log_button.setText('開始紀錄')
            self.log_button.setToolTip('')
            return
        try:
            values = self.collect_values()
            interval = values['log/interval']*(60 if values['log/unit']=='分鐘' else 3600) if values['log/rotate'] else 0
            self.preferences.save(values)
            self.logger.start(values['log/directory'],interval,values['terminal/timestamp'])
            self.log_button.setText('停止紀錄')
            self.log_button.setToolTip(str(self.logger.directory))
        except (ValueError,OSError) as exc:
            self.logger.stop()
            QMessageBox.warning(self,'無法開始紀錄',str(exc))

    def log_received(self, text):
        try:
            self.logger.write(text)
        except OSError as exc:
            self.logger.stop()
            self.log_button.setText('開始紀錄')
            QMessageBox.warning(self,'日誌寫入失敗',str(exc))

    def restore_window(self):
        geometry = self.preferences.get('window/geometry')
        if geometry is not None:
            self.restoreGeometry(geometry)
        screens = QApplication.screens()
        if not any(screen.availableGeometry().intersects(self.frameGeometry()) for screen in screens):
            area = QApplication.primaryScreen().availableGeometry()
            self.move(area.topLeft()+QPoint(30,30))
        area = self.screen().availableGeometry()
        if self.width()>area.width() or self.height()>area.height():
            self.resize(min(self.width(),area.width()),min(self.height(),area.height()))
        if self.preferences.get('window/maximized',False):
            QTimer.singleShot(0,self.showMaximized)

    def closeEvent(self, event):
        if self.worker is not None:
            self.closing = True
            self.worker.stop()
            if self.auth_dialog:
                self.auth_dialog.reject()
            event.ignore()
            return
        self.logger.stop()
        try:
            values = self.collect_values()
            values.update({'window/geometry':self.saveGeometry(),'window/maximized':self.isMaximized()})
            self.preferences.save(values)
        except (ValueError,OSError) as exc:
            self.closing = False
            QMessageBox.warning(self,'無法儲存設定',str(exc))
            event.ignore()
            return
        event.accept()


def main():
    if sys.platform=='win32':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('PuddingTerminal.SourceCandidate')
        except (AttributeError,OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName('Pudding Terminal')
    app.setStyle('Fusion')
    app.setFont(QFont('Segoe UI',10))
    window = MainWindow()
    window.show()
    return app.exec()

if __name__=='__main__':
    sys.exit(main())
