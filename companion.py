# Companion Remote for Picoware / PicoCalc
# Install: copy this file to /picoware/apps/companion.py on the device.
#
# Controls — remote:
#   Arrow keys : move cursor   CENTER : press button   BACK : return
#
# Controls — setup (Network):
#   UP/DN : switch field   type digits/dots   BKSP=delete   CENTER : connect
#
# Controls — setup (Browse):
#   Arrow keys : move cursor   L/R : shift column window (auto at edge)
#   A/Z : previous/next page   CENTER : toggle selection   BACK : done
#
# Controls — setup (Reorder):
#   UP/DN : navigate   A/Z : move item up/down   BACK : continue
#
# Controls — setup (Confirm):
#   CENTER : save   BACK : go back

import socket
import json
import os
import utime
import _thread
import ubinascii

from picoware.system.vector  import Vector
from picoware.system.buttons import (
    BUTTON_NONE, BUTTON_BACK, BUTTON_LEFT, BUTTON_RIGHT,
    BUTTON_UP, BUTTON_DOWN, BUTTON_CENTER,
    BUTTON_A, BUTTON_Z, BUTTON_BACKSPACE,
)

VERSION = "1.0.1"

# ============================================================
# Colours (RGB565)
# ============================================================
C_BLACK      = 0x0000
C_WHITE      = 0xFFFF
C_DARK_BLUE  = 0x18C3
C_DARK_GREY  = 0x2104
C_LIGHT_GREY = 0x8410
C_RED        = 0xF800
C_GREEN      = 0x07E0
C_YELLOW     = 0xFFE0

# ============================================================
# TCP client
# ============================================================
_TCP_IDLE, _TCP_CONNECTING, _TCP_CONNECTED, _TCP_ERROR, _TCP_CLOSED = 0,1,2,3,4


class _TCP:
    def __init__(self):
        self.state = _TCP_IDLE
        self._sock = None
        self._buf  = b""

    def connect_async(self, ip, port):
        self.state = _TCP_CONNECTING
        self._buf  = b""
        _thread.start_new_thread(self._do_connect, (ip, port))

    def _do_connect(self, ip, port):
        try:
            addr = socket.getaddrinfo(ip, port, 0, socket.SOCK_STREAM)[0][-1]
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect(addr)
            s.settimeout(0.005)   # short timeout: b""=closed, OSError=no data
            self._sock = s
            self.state = _TCP_CONNECTED
        except Exception:
            self.state = _TCP_ERROR
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None

    def update(self):
        if self.state != _TCP_CONNECTED or not self._sock:
            return
        try:
            data = self._sock.recv(4096)
            if data:
                self._buf += data
            else:
                self.state = _TCP_CLOSED
        except OSError:
            pass  # timeout = no data right now

    def send(self, msg):
        if self.state != _TCP_CONNECTED or not self._sock:
            return
        try:
            if isinstance(msg, str):
                msg = msg.encode()
            self._sock.send(msg)
        except Exception:
            self.state = _TCP_ERROR

    def read_line(self):
        nl = self._buf.find(b"\n")
        if nl < 0:
            return None
        line = self._buf[:nl].decode("utf-8", "ignore").rstrip("\r")
        self._buf = self._buf[nl + 1:]
        return line

    def close(self):
        if self._sock:
            try: self._sock.close()
            except: pass
            self._sock = None
        self.state = _TCP_IDLE
        self._buf  = b""

    @property
    def connected(self): return self.state == _TCP_CONNECTED
    @property
    def error(self):     return self.state in (_TCP_ERROR, _TCP_CLOSED)


# ============================================================
# Satellite protocol (Companion 4.x grid API, port 16622)
# ============================================================
_SAT_DEVICE_ID    = "picoware-comp"
_SAT_PRODUCT_NAME = "PicoCalc Remote"
_SAT_COLS         = 8
_SAT_ROWS         = 4
_SAT_KEYS         = _SAT_COLS * _SAT_ROWS   # 32


def _parse_sat_params(line):
    params = {}
    i = 0; n = len(line)
    while i < n:
        while i < n and line[i] == " ": i += 1
        eq = line.find("=", i)
        if eq < 0: break
        key = line[i:eq]; i = eq + 1
        if i >= n: params[key] = ""; break
        if line[i] == '"':
            i += 1
            end = line.find('"', i)
            if end < 0: params[key] = line[i:]; break
            params[key] = line[i:end]; i = end + 1
        else:
            end = i
            while end < n and line[end] != " ": end += 1
            params[key] = line[i:end]; i = end
    return params


def _hex_rgb565(s):
    s = s.lstrip("#")
    if len(s) < 6: return C_DARK_GREY
    try: rgb = int(s[:6], 16)
    except: return C_DARK_GREY
    r = (rgb >> 16) & 0xFF; g = (rgb >> 8) & 0xFF; b = rgb & 0xFF
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _contrast(c):
    r = ((c >> 11) & 0x1F) << 3
    g = ((c >>  5) & 0x3F) << 2
    b =  (c        & 0x1F) << 3
    return C_BLACK if (r*299 + g*587 + b*114)//1000 > 128 else C_WHITE


def _b64decode(s):
    try:
        return ubinascii.a2b_base64(s).decode("utf-8", "ignore").strip("\x00")
    except:
        return s


class _BtnState:
    __slots__ = ("text", "bg", "valid")
    def __init__(self): self.text = ""; self.bg = C_DARK_GREY; self.valid = False


class _Satellite:
    def __init__(self):
        self._tcp      = _TCP()
        self.registered = False
        self.page       = 1
        self.page_keys  = {}   # {page_num: [_BtnState x 32]}
        self._ping_ts   = 0

    def connect(self, ip, port, page=1):
        self.registered = False
        self.page       = page
        self.page_keys  = {}
        self._ping_ts   = 0
        self._tcp.connect_async(ip, port)

    def _keys_for(self, page):
        if page not in self.page_keys:
            self.page_keys[page] = [_BtnState() for _ in range(_SAT_KEYS)]
        return self.page_keys[page]

    def update(self):
        self._tcp.update()
        if self._tcp.connected and not self.registered:
            self._tcp.send(
                f'ADD-DEVICE DEVICEID="{_SAT_DEVICE_ID}" PRODUCT_NAME="{_SAT_PRODUCT_NAME}" '
                f'KEYS_TOTAL={_SAT_KEYS} KEYS_PER_ROW={_SAT_COLS} COLORS=hex TEXT=1\n'
            )
            self.registered = True
            self._ping_ts = utime.ticks_ms()
        if self._tcp.connected and self.registered:
            now = utime.ticks_ms()
            if utime.ticks_diff(now, self._ping_ts) > 3000:
                self._tcp.send("PING\n")
                self._ping_ts = now
        line = self._tcp.read_line()
        while line is not None:
            self._parse(line)
            line = self._tcp.read_line()

    def set_page(self, page):
        self.page = page
        self._tcp.send(f'CHANGE-PAGE DEVICEID="{_SAT_DEVICE_ID}" PAGE={page}\n')

    def key_press(self, key_idx, pressed):
        self._tcp.send(
            f'KEY-PRESS DEVICEID="{_SAT_DEVICE_ID}" KEY={key_idx}'
            f' PRESSED={1 if pressed else 0}\n'
        )

    def disconnect(self):
        self._tcp.close()
        self.registered = False

    @property
    def connected(self): return self._tcp.connected
    @property
    def error(self):     return self._tcp.error

    def _parse(self, line):
        if line.startswith("PONG") or line.startswith("PING"):
            return
        if not line.startswith("KEY-STATE"):
            return
        p = _parse_sat_params(line[10:])
        try:
            idx = int(p.get("KEY", "-1"))
        except:
            return
        if 0 <= idx < _SAT_KEYS:
            b = self._keys_for(self.page)[idx]
            b.text  = _b64decode(p.get("TEXT", ""))
            b.bg    = _hex_rgb565(p.get("COLOR", ""))
            b.valid = True


# ============================================================
# Config  (/sd/companion/config.json)
# ============================================================
_CFG_PATH = "/sd/companion/config.json"
_cfg = {"ip": "192.168.1.100", "port": 16622, "buttons": [], "valid": False}


def _cfg_load():
    try:
        with open(_CFG_PATH) as f:
            d = json.load(f)
        _cfg["ip"]      = d.get("ip",      _cfg["ip"])
        _cfg["port"]    = int(d.get("port", _cfg["port"]))
        _cfg["buttons"] = d.get("buttons", [])
        _cfg["valid"]   = True
        return True
    except: return False


def _cfg_save():
    try:
        try: os.mkdir("/sd/companion")
        except: pass
        with open(_CFG_PATH, "w") as f:
            json.dump({"ip": _cfg["ip"], "port": _cfg["port"],
                       "buttons": _cfg["buttons"]}, f)
        _cfg["valid"] = True
        return True
    except: return False


# ============================================================
# Shared drawing helpers
# ============================================================
_PG_VCOLS = 4
_PG_VROWS = 4
_PG_BW    = 75
_PG_BH    = 62
_PG_GAP   = 5
_PG_HDR_H = 30
_PG_FTR_H = 24


def _hdr(d, title):
    d.fill_rectangle(Vector(0, 0), Vector(320, _PG_HDR_H), C_DARK_BLUE)
    d.text(Vector(4, 8), title, C_WHITE, 1)


def _btn_text_lines(text, max_chars):
    """Split text into at most 2 lines of max_chars, breaking at spaces when possible."""
    if len(text) <= max_chars:
        return [text]
    best = -1
    mid = len(text) // 2
    for i in range(mid, -1, -1):
        if text[i] == ' ' and i <= max_chars:
            best = i; break
    if best < 0:
        for i in range(mid + 1, len(text)):
            if text[i] == ' ' and i <= max_chars:
                best = i; break
    if best > 0:
        l1 = text[:best]; l2 = text[best+1:best+1+max_chars]
    else:
        l1 = text[:max_chars]; l2 = text[max_chars:max_chars*2]
    return [l1, l2] if l2 else [l1]


def _draw_btn_text(d, bx, by, bw, bh, text, color):
    """Draw button label scaled to fit: scale 2 if short, else scale 1 with word-wrap."""
    if not text:
        return
    pad = 4
    avail_w = bw - pad * 2
    if len(text) * 16 <= avail_w:
        tw = len(text) * 16
        tx = bx + pad + (avail_w - tw) // 2
        ty = by + (bh - 16) // 2
        d.text(Vector(tx, ty), text, color, 2)
        return
    max1 = avail_w // 8
    lines = _btn_text_lines(text, max1)
    gap = 2; line_h = 8
    total_h = len(lines) * line_h + (len(lines) - 1) * gap
    ty = by + (bh - total_h) // 2
    for line in lines:
        tw = len(line) * 8
        tx = bx + pad + max(0, (avail_w - tw) // 2)
        d.text(Vector(tx, ty), line, color, 1)
        ty += line_h + gap


def _draw_page_grid(d, sat, cursor, col_off, sel_page, sel_fn=None):
    """Draw 4×4 window into the current satellite page. sel_fn(key_idx)->bool for green dot."""
    keys = sat._keys_for(sat.page) if sat else None
    for r in range(_PG_VROWS):
        for c in range(_PG_VCOLS):
            cur = r * _PG_VCOLS + c
            key = r * _SAT_COLS + col_off + c
            x   = c * (_PG_BW + _PG_GAP) + _PG_GAP
            y   = _PG_HDR_H + r * (_PG_BH + _PG_GAP) + _PG_GAP
            bg = C_DARK_GREY; lbl = ""
            if keys and 0 <= key < _SAT_KEYS:
                bs = keys[key]
                if bs.valid: bg = bs.bg or C_DARK_GREY; lbl = bs.text
            if cur == cursor:
                d.fill_rectangle(Vector(x-2, y-2), Vector(_PG_BW+4, _PG_BH+4), C_WHITE)
            d.fill_rectangle(Vector(x, y), Vector(_PG_BW, _PG_BH), bg)
            row_abs = r; col_abs = col_off + c
            if sel_fn and sel_fn(sel_page, row_abs, col_abs):
                d.fill_circle(Vector(x + _PG_BW - 8, y + 8), 5, C_GREEN)
            if lbl:
                _draw_btn_text(d, x, y, _PG_BW, _PG_BH, lbl, _contrast(bg))


def _nav_cursor(cursor, col_off, btn, page, sat):
    """
    Handle arrow key navigation for a 4×4 grid window.
    Returns (new_cursor, new_col_off, new_page, changed).
    LEFT/RIGHT move cursor; shift column window at edge.
    A/Z change page.
    """
    c_col = cursor % _PG_VCOLS
    c_row = cursor // _PG_VCOLS

    if btn == BUTTON_UP:
        if c_row > 0:
            return cursor - _PG_VCOLS, col_off, page, True
    elif btn == BUTTON_DOWN:
        if c_row < _PG_VROWS - 1:
            return cursor + _PG_VCOLS, col_off, page, True
    elif btn == BUTTON_LEFT:
        if c_col > 0:
            return cursor - 1, col_off, page, True
        elif col_off >= _PG_VCOLS:
            return cursor + _PG_VCOLS - 1, col_off - _PG_VCOLS, page, True
        elif page > 1:
            if sat: sat.set_page(page - 1)
            return cursor + _PG_VCOLS - 1, _SAT_COLS - _PG_VCOLS, page - 1, True
    elif btn == BUTTON_RIGHT:
        if c_col < _PG_VCOLS - 1:
            return cursor + 1, col_off, page, True
        elif col_off + _PG_VCOLS < _SAT_COLS:
            return cursor - _PG_VCOLS + 1, col_off + _PG_VCOLS, page, True
        else:
            if sat: sat.set_page(page + 1)
            return cursor - _PG_VCOLS + 1, 0, page + 1, True
    elif btn == BUTTON_A:
        if page > 1:
            if sat: sat.set_page(page - 1)
            return cursor, col_off, page - 1, True
    elif btn == BUTTON_Z:
        if sat: sat.set_page(page + 1)
        return cursor, col_off, page + 1, True

    return cursor, col_off, page, False


# ============================================================
# ── REMOTE VIEW ──────────────────────────────────────────────
# ============================================================
_REM_COLS  = 3
_REM_BTN_W = 102
_REM_BTN_H = 72
_REM_GAP   = 4
_REM_HDR_H = 28
_REM_FTR_H = 20

_rem_sat        = None
_rem_cursor     = 0
_rem_scroll     = 0
_rem_state      = "idle"   # connecting / syncing / ready / error
_rem_sync_pages = []
_rem_sync_idx   = 0
_rem_sync_ts    = 0
_rem_resync_ts  = 0   # timestamp for background re-sync
_rem_resync_idx = -1  # -1 = not re-syncing
_rem_dirty      = True
_rem_last_btn   = BUTTON_NONE
_rem_draw_ts    = 0


def _rem_rows_vis(): return (320 - _REM_HDR_H - _REM_FTR_H) // (_REM_BTN_H + _REM_GAP)
def _rem_max_vis():  return _rem_rows_vis() * _REM_COLS


def _rem_btn_state(b):
    if not _rem_sat: return None
    pg = b["page"]; key = b["row"] * _SAT_COLS + b["col"]
    if pg not in _rem_sat.page_keys: return None
    bs = _rem_sat.page_keys[pg][key]
    return bs if bs.valid else None


def _rem_draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    _hdr(d, "Companion Remote")

    if not _cfg["valid"] or not _cfg["buttons"]:
        d.text(Vector(4, 50), "No config — run Setup first.", C_RED, 1)
        d.swap(); return

    if _rem_state == "error":
        d.text(Vector(4, 50), "Connection error. BACK to exit.", C_RED, 1)
        d.swap(); return

    if _rem_state == "connecting":
        d.text(Vector(4, 50), f"Connecting to {_cfg['ip']} ...", C_YELLOW, 1)
        d.swap(); return

    if _rem_state == "syncing":
        n = len(_rem_sync_pages)
        d.text(Vector(4, 50),
               f"Syncing page {_rem_sync_pages[_rem_sync_idx] if _rem_sync_idx < n else '?'}"
               f"  ({_rem_sync_idx+1}/{n})", C_YELLOW, 1)
        d.swap(); return

    buttons = _cfg["buttons"]
    n       = len(buttons)
    visible = _rem_max_vis()
    end     = min(_rem_scroll + visible, n)

    for i in range(_rem_scroll, end):
        local = i - _rem_scroll
        col   = local % _REM_COLS
        row   = local // _REM_COLS
        x = col * (_REM_BTN_W + _REM_GAP) + _REM_GAP
        y = _REM_HDR_H + row * (_REM_BTN_H + _REM_GAP) + _REM_GAP
        bg = C_DARK_GREY; lbl = "---"
        bs = _rem_btn_state(buttons[i])
        if bs: bg = bs.bg or C_DARK_GREY; lbl = bs.text or "---"
        if i == _rem_cursor:
            d.fill_rectangle(Vector(x-2, y-2), Vector(_REM_BTN_W+4, _REM_BTN_H+4), C_WHITE)
        d.fill_rectangle(Vector(x, y), Vector(_REM_BTN_W, _REM_BTN_H), bg)
        _draw_btn_text(d, x, y, _REM_BTN_W, _REM_BTN_H, lbl, _contrast(bg))

    if n > visible:
        d.text(Vector(4, 320-_REM_FTR_H+4),
               f"{_rem_cursor+1}/{n}  UP/DN scroll", C_LIGHT_GREY, 1)
    d.swap()


def _rem_start(vm):
    global _rem_sat, _rem_cursor, _rem_scroll, _rem_state
    global _rem_sync_pages, _rem_sync_idx, _rem_sync_ts
    global _rem_resync_ts, _rem_resync_idx
    global _rem_dirty, _rem_last_btn, _rem_draw_ts
    _rem_cursor = 0; _rem_scroll = 0
    _rem_state  = "connecting"
    _rem_sync_pages = []; _rem_sync_idx = 0; _rem_sync_ts = 0
    _rem_resync_ts = 0; _rem_resync_idx = -1
    _rem_dirty = True; _rem_last_btn = BUTTON_NONE; _rem_draw_ts = 0
    if not _cfg["valid"] or not _cfg["buttons"]: return True
    _rem_sat = _Satellite()
    _rem_sat.connect(_cfg["ip"], _cfg["port"])
    return True


def _rem_run(vm):
    global _rem_cursor, _rem_scroll, _rem_state
    global _rem_sync_pages, _rem_sync_idx, _rem_sync_ts
    global _rem_resync_ts, _rem_resync_idx
    global _rem_dirty, _rem_last_btn, _rem_draw_ts

    if _rem_sat:
        _rem_sat.update()
        if _rem_sat.error:
            _rem_state = "error"; _rem_dirty = True

    if _rem_state == "connecting" and _rem_sat and _rem_sat.connected:
        pages = sorted(set(b["page"] for b in _cfg["buttons"]))
        _rem_sync_pages = pages
        _rem_sync_idx   = 0
        _rem_sync_ts    = utime.ticks_ms()
        if pages: _rem_sat.set_page(pages[0])
        _rem_state = "syncing"; _rem_dirty = True

    elif _rem_state == "syncing":
        now = utime.ticks_ms()
        if utime.ticks_diff(now, _rem_sync_ts) > 300:
            _rem_sync_idx += 1
            if _rem_sync_idx >= len(_rem_sync_pages):
                _rem_state = "ready"; _rem_dirty = True
            else:
                _rem_sat.set_page(_rem_sync_pages[_rem_sync_idx])
                _rem_sync_ts = now; _rem_dirty = True

    elif _rem_state == "ready":
        now = utime.ticks_ms()
        if utime.ticks_diff(now, _rem_draw_ts) > 150:
            _rem_dirty = True
        # Silent background re-sync every 5s so all pages stay live
        if len(_rem_sync_pages) > 1:
            if _rem_resync_idx < 0 and utime.ticks_diff(now, _rem_resync_ts) > 5000:
                _rem_resync_idx = 0
                _rem_resync_ts  = now
                _rem_sat.set_page(_rem_sync_pages[0])
            elif _rem_resync_idx >= 0 and utime.ticks_diff(now, _rem_resync_ts) > 300:
                _rem_resync_idx += 1
                if _rem_resync_idx >= len(_rem_sync_pages):
                    _rem_resync_idx = -1
                    _rem_resync_ts  = now
                    _rem_dirty = True
                else:
                    _rem_sat.set_page(_rem_sync_pages[_rem_resync_idx])
                    _rem_resync_ts = now

    btn = vm.button
    if btn != _rem_last_btn and btn != BUTTON_NONE:
        _rem_last_btn = btn
        n       = len(_cfg["buttons"])
        visible = _rem_max_vis()

        if btn == BUTTON_BACK:
            vm.back(); return
        elif btn == BUTTON_UP:
            if _rem_cursor >= _REM_COLS:
                _rem_cursor -= _REM_COLS
                if _rem_cursor < _rem_scroll:
                    _rem_scroll = (_rem_cursor // _REM_COLS) * _REM_COLS
                _rem_dirty = True
        elif btn == BUTTON_DOWN:
            if _rem_cursor + _REM_COLS < n:
                _rem_cursor += _REM_COLS
                while _rem_cursor >= _rem_scroll + visible:
                    _rem_scroll += _REM_COLS
                _rem_dirty = True
        elif btn == BUTTON_LEFT:
            if _rem_cursor % _REM_COLS > 0:
                _rem_cursor -= 1; _rem_dirty = True
        elif btn == BUTTON_RIGHT:
            if _rem_cursor % _REM_COLS < _REM_COLS - 1 and _rem_cursor + 1 < n:
                _rem_cursor += 1; _rem_dirty = True
        elif btn == BUTTON_CENTER:
            if _rem_state == "ready" and _rem_sat and 0 <= _rem_cursor < n:
                b   = _cfg["buttons"][_rem_cursor]
                pg  = b["page"]
                key = b["row"] * _SAT_COLS + b["col"]
                if _rem_sat.page != pg:
                    _rem_sat.set_page(pg)
                    utime.sleep_ms(80)
                _rem_sat.key_press(key, True)
                utime.sleep_ms(50)
                _rem_sat.key_press(key, False)

        vm.input_manager.reset()
    elif btn == BUTTON_NONE:
        _rem_last_btn = BUTTON_NONE

    now = utime.ticks_ms()
    if _rem_dirty or utime.ticks_diff(now, _rem_draw_ts) > 100:
        _rem_draw(vm)
        _rem_dirty   = False
        _rem_draw_ts = now


def _rem_stop(vm):
    global _rem_sat, _rem_state
    if _rem_sat: _rem_sat.disconnect(); _rem_sat = None
    _rem_state = "idle"


# ============================================================
# ── SETUP WIZARD ─────────────────────────────────────────────
# ============================================================
_SW_NET     = "net"
_SW_BROWSE  = "browse"
_SW_REORDER = "reorder"
_SW_CONFIRM = "confirm"
_SW_SAVING  = "saving"
_SW_ERROR   = "error"

_sw_step      = _SW_NET
_sw_sat       = None
_sw_sat_ready = False
_sw_dirty     = True
_sw_last_btn  = BUTTON_NONE
_sw_err_msg   = ""
_sw_browse_ts = 0

_sw_ip_buf    = []
_sw_port_buf  = []
_sw_net_field = 0

_sw_page    = 1
_sw_col_off = 0
_sw_cursor  = 0
_sw_sel     = []   # [{"page","row","col","label"}, ...]
_sw_rcur    = 0


def _sw_is_sel(page, row, col):
    return any(s["page"] == page and s["row"] == row and s["col"] == col
               for s in _sw_sel)


def _sw_toggle(page, row, col, label):
    for i, s in enumerate(_sw_sel):
        if s["page"] == page and s["row"] == row and s["col"] == col:
            _sw_sel.pop(i); return
    if len(_sw_sel) < 32:
        _sw_sel.append({"page": page, "row": row, "col": col, "label": label})


def _sw_draw_net(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    _hdr(d, "Setup  1/4: Network")
    ip_s   = "".join(_sw_ip_buf)   or "192.168.1.100"
    port_s = "".join(_sw_port_buf) or "16622"
    d.text(Vector(8, 48),  "Companion IP:", C_LIGHT_GREY, 1)
    if _sw_net_field == 0:
        d.fill_rectangle(Vector(6, 64), Vector(204, 26), C_WHITE)
    d.fill_rectangle(Vector(8, 66), Vector(200, 22),
                     C_DARK_BLUE if _sw_net_field == 0 else C_DARK_GREY)
    d.text(Vector(12, 72), ip_s, C_WHITE, 1)
    d.text(Vector(8, 104), "Port:", C_LIGHT_GREY, 1)
    if _sw_net_field == 1:
        d.fill_rectangle(Vector(6, 120), Vector(124, 26), C_WHITE)
    d.fill_rectangle(Vector(8, 122), Vector(120, 22),
                     C_DARK_BLUE if _sw_net_field == 1 else C_DARK_GREY)
    d.text(Vector(12, 128), port_s, C_WHITE, 1)
    d.text(Vector(8, 175), "UP/DN: switch field", C_LIGHT_GREY, 1)
    d.text(Vector(8, 193), "Type digits/dots  BKSP=delete", C_LIGHT_GREY, 1)
    d.text(Vector(8, 211), "CENTER: connect & continue", C_LIGHT_GREY, 1)
    d.swap()


def _sw_draw_browse(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    c0 = _sw_col_off + 1; c1 = _sw_col_off + _PG_VCOLS
    _hdr(d, f"Setup  2/4  Pg:{_sw_page}  C:{c0}-{c1}")
    if not _sw_sat_ready:
        d.text(Vector(4, 60), f"Connecting to {_cfg['ip']}...", C_YELLOW, 1)
        d.swap(); return
    _draw_page_grid(d, _sw_sat, _sw_cursor, _sw_col_off, _sw_page,
                    sel_fn=_sw_is_sel)
    d.text(Vector(4, 320-_PG_FTR_H+4),
           f"Arrows/A/Z:nav  CTR:sel  BCK:done({len(_sw_sel)})",
           C_LIGHT_GREY, 1)
    d.swap()


def _sw_draw_reorder(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    _hdr(d, "Setup  3/4  A/Z:move  BACK:done")
    if not _sw_sel:
        d.text(Vector(8, 60), "Nothing selected. BACK to browse.", C_RED, 1)
        d.swap(); return
    rh = 28; mv = (320 - _PG_HDR_H) // rh
    st = max(0, _sw_rcur - mv + 1) if _sw_rcur >= mv else 0
    for i in range(st, min(len(_sw_sel), st + mv)):
        y   = _PG_HDR_H + (i - st) * rh + 4
        cur = (i == _sw_rcur)
        if cur: d.fill_rectangle(Vector(0, y-2), Vector(320, rh), C_DARK_BLUE)
        s = _sw_sel[i]
        d.text(Vector(4, y+4),
               f"{i+1}. [{s['page']}/{s['row']}/{s['col']}] {s['label'][:26]}",
               C_WHITE if cur else C_LIGHT_GREY, 1)
    d.swap()


def _sw_draw_confirm(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    _hdr(d, "Setup  4/4  Confirm & Save")
    d.text(Vector(8, 50), f"IP:      {_cfg['ip']}",    C_WHITE, 1)
    d.text(Vector(8, 70), f"Port:    {_cfg['port']}",  C_WHITE, 1)
    d.text(Vector(8, 90), f"Buttons: {len(_sw_sel)}",  C_WHITE, 1)
    d.text(Vector(8, 135), "CENTER = Save", C_GREEN, 1)
    d.text(Vector(8, 155), "BACK   = Go back", C_LIGHT_GREY, 1)
    d.swap()


def _sw_draw(vm):
    global _sw_dirty
    if not _sw_dirty: return
    _sw_dirty = False
    if   _sw_step == _SW_NET:     _sw_draw_net(vm)
    elif _sw_step == _SW_BROWSE:  _sw_draw_browse(vm)
    elif _sw_step == _SW_REORDER: _sw_draw_reorder(vm)
    elif _sw_step == _SW_CONFIRM: _sw_draw_confirm(vm)
    elif _sw_step == _SW_SAVING:
        vm.draw.fill_screen(C_BLACK); vm.draw.text(Vector(8, 60), "Saving...", C_YELLOW, 1)
        vm.draw.swap()
    elif _sw_step == _SW_ERROR:
        vm.draw.fill_screen(C_BLACK)
        vm.draw.text(Vector(8, 60), "Error:", C_RED, 1)
        vm.draw.text(Vector(8, 80), _sw_err_msg, C_WHITE, 1)
        vm.draw.text(Vector(8, 140), "BACK to retry", C_LIGHT_GREY, 1)
        vm.draw.swap()


def _sw_input_net(vm, btn):
    global _sw_step, _sw_sat, _sw_sat_ready, _sw_page, _sw_col_off
    global _sw_cursor, _sw_net_field, _sw_sel
    buf = _sw_ip_buf if _sw_net_field == 0 else _sw_port_buf
    if btn in (BUTTON_UP, BUTTON_DOWN):
        _sw_net_field = 1 - _sw_net_field; return
    if btn == BUTTON_BACKSPACE:
        if buf: buf.pop(); return
    if btn == BUTTON_CENTER:
        ip   = "".join(_sw_ip_buf)   or "192.168.1.100"
        port = int("".join(_sw_port_buf) or "16622")
        _cfg["ip"] = ip; _cfg["port"] = port
        _sw_step = _SW_BROWSE; _sw_sat_ready = False
        _sw_page = 1; _sw_col_off = 0; _sw_cursor = 0; _sw_sel = []
        _sw_sat = _Satellite(); _sw_sat.connect(ip, port)
        return
    c = vm.input_manager.button_to_char(btn)
    if c and c in "0123456789." and len(buf) < 15:
        buf.append(c)


def _sw_input_browse(vm, btn):
    global _sw_step, _sw_sat, _sw_page, _sw_col_off, _sw_cursor, _sw_rcur

    if btn == BUTTON_BACK:
        if _sw_sat: _sw_sat.disconnect(); _sw_sat = None
        _sw_rcur = 0; _sw_step = _SW_REORDER; return

    if btn == BUTTON_CENTER:
        row = _sw_cursor // _PG_VCOLS
        col = _sw_col_off + _sw_cursor % _PG_VCOLS
        lbl = ""
        if _sw_sat and _sw_sat.page in _sw_sat.page_keys:
            key = row * _SAT_COLS + col
            bs  = _sw_sat.page_keys[_sw_sat.page][key]
            if bs.valid: lbl = bs.text
        _sw_toggle(_sw_page, row, col, lbl)
        return

    new_cur, new_off, new_pg, changed = _nav_cursor(
        _sw_cursor, _sw_col_off, btn, _sw_page, _sw_sat)
    if changed:
        _sw_cursor = new_cur; _sw_col_off = new_off
        if new_pg != _sw_page:
            _sw_page = new_pg


def _sw_input_reorder(vm, btn):
    global _sw_step, _sw_rcur
    n = len(_sw_sel)
    if btn == BUTTON_BACK:  _sw_step = _SW_CONFIRM; return
    if btn == BUTTON_UP   and _sw_rcur > 0:   _sw_rcur -= 1; return
    if btn == BUTTON_DOWN and _sw_rcur < n-1: _sw_rcur += 1; return
    if btn == BUTTON_A and _sw_rcur > 0:
        i = _sw_rcur; _sw_sel[i], _sw_sel[i-1] = _sw_sel[i-1], _sw_sel[i]
        _sw_rcur -= 1; return
    if btn == BUTTON_Z and _sw_rcur < n-1:
        i = _sw_rcur; _sw_sel[i], _sw_sel[i+1] = _sw_sel[i+1], _sw_sel[i]
        _sw_rcur += 1


def _sw_input_confirm(vm, btn):
    global _sw_step
    if btn == BUTTON_BACK:   _sw_step = _SW_REORDER; return
    if btn == BUTTON_CENTER:
        _cfg["buttons"] = [
            {"page": s["page"], "row": s["row"], "col": s["col"]}
            for s in _sw_sel
        ]
        _sw_step = _SW_SAVING


def _sw_start(vm):
    global _sw_step, _sw_sat, _sw_sat_ready, _sw_dirty, _sw_last_btn
    global _sw_ip_buf, _sw_port_buf, _sw_net_field, _sw_sel, _sw_rcur
    global _sw_page, _sw_col_off, _sw_cursor, _sw_err_msg, _sw_browse_ts
    _sw_step = _SW_NET; _sw_sat = None; _sw_sat_ready = False
    _sw_dirty = True; _sw_last_btn = BUTTON_NONE; _sw_browse_ts = 0
    _sw_sel = []; _sw_rcur = 0
    _sw_page = 1; _sw_col_off = 0; _sw_cursor = 0
    _sw_err_msg = ""; _sw_net_field = 0
    _sw_ip_buf   = list(_cfg["ip"])
    _sw_port_buf = list(str(_cfg["port"]))
    return True


def _sw_run(vm):
    global _sw_step, _sw_sat, _sw_sat_ready, _sw_dirty, _sw_last_btn
    global _sw_err_msg, _sw_browse_ts

    if _sw_step == _SW_BROWSE and _sw_sat:
        _sw_sat.update()
        if not _sw_sat_ready and _sw_sat.connected:
            _sw_sat_ready = True; _sw_dirty = True
        if _sw_sat.error:
            _sw_err_msg = "Failed to connect to Companion."
            _sw_step = _SW_ERROR; _sw_dirty = True
        if _sw_sat_ready:
            now = utime.ticks_ms()
            if utime.ticks_diff(now, _sw_browse_ts) > 150:
                _sw_dirty = True; _sw_browse_ts = now

    if _sw_step == _SW_SAVING:
        _sw_draw(vm)
        if _cfg_save():
            if _sw_sat: _sw_sat.disconnect(); _sw_sat = None
            vm.back(); return
        else:
            _sw_err_msg = "Failed to write config."
            _sw_step = _SW_ERROR; _sw_dirty = True

    btn = vm.button
    if btn != _sw_last_btn and btn != BUTTON_NONE:
        _sw_last_btn = btn
        if _sw_step == _SW_ERROR and btn == BUTTON_BACK:
            _sw_step = _SW_NET; _sw_dirty = True
        elif _sw_step == _SW_NET:     _sw_input_net(vm, btn);     _sw_dirty = True
        elif _sw_step == _SW_BROWSE:  _sw_input_browse(vm, btn);  _sw_dirty = True
        elif _sw_step == _SW_REORDER: _sw_input_reorder(vm, btn); _sw_dirty = True
        elif _sw_step == _SW_CONFIRM: _sw_input_confirm(vm, btn); _sw_dirty = True
        vm.input_manager.reset()
    elif btn == BUTTON_NONE:
        _sw_last_btn = BUTTON_NONE

    _sw_draw(vm)


def _sw_stop(vm):
    global _sw_sat, _sw_sat_ready
    if _sw_sat: _sw_sat.disconnect(); _sw_sat = None
    _sw_sat_ready = False


# ============================================================
# ── MAIN MENU ────────────────────────────────────────────────
# ============================================================
_MENU_ITEMS = ["Remote", "Setup"]
_menu_cursor   = 0
_menu_dirty    = True
_menu_last_btn = BUTTON_NONE


def _menu_draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    _hdr(d, f"Companion Remote  v{VERSION}")
    if _cfg["valid"]:
        d.text(Vector(4, 36),
               f"{_cfg['ip']}:{_cfg['port']}  {len(_cfg['buttons'])} buttons",
               C_LIGHT_GREY, 1)
    else:
        d.text(Vector(4, 36), "Not configured — run Setup", C_RED, 1)
    for i, item in enumerate(_MENU_ITEMS):
        y = 75 + i * 55
        if i == _menu_cursor:
            d.fill_rectangle(Vector(8, y-4), Vector(180, 38), C_DARK_BLUE)
            d.text(Vector(14, y+5), item, C_WHITE, 1)
        else:
            d.text(Vector(14, y+5), item, C_LIGHT_GREY, 1)
    d.text(Vector(4, 290), "UP/DN: select   CENTER: open", C_LIGHT_GREY, 1)
    d.swap()


# ============================================================
# App entry points (required by Picoware AppLoader)
# ============================================================

def start(vm):
    global _menu_cursor, _menu_dirty, _menu_last_btn
    _menu_cursor   = 0
    _menu_dirty    = True
    _menu_last_btn = BUTTON_NONE
    if not _cfg["valid"]:
        _cfg_load()
    return True


def run(vm):
    global _menu_cursor, _menu_dirty, _menu_last_btn

    from picoware.system.view import View

    btn = vm.button
    if btn != _menu_last_btn and btn != BUTTON_NONE:
        _menu_last_btn = btn
        if btn == BUTTON_BACK:
            vm.back(); return
        elif btn == BUTTON_UP:
            if _menu_cursor > 0: _menu_cursor -= 1; _menu_dirty = True
        elif btn == BUTTON_DOWN:
            if _menu_cursor < len(_MENU_ITEMS)-1: _menu_cursor += 1; _menu_dirty = True
        elif btn == BUTTON_CENTER:
            if _menu_cursor == 0:
                if vm.get_view("_comp_remote") is None:
                    vm.add(View("_comp_remote", _rem_run, _rem_start, _rem_stop))
                vm.switch_to("_comp_remote")
            else:
                if vm.get_view("_comp_setup") is None:
                    vm.add(View("_comp_setup", _sw_run, _sw_start, _sw_stop))
                vm.switch_to("_comp_setup")
            return
        vm.input_manager.reset()
    elif btn == BUTTON_NONE:
        _menu_last_btn = BUTTON_NONE

    if _menu_dirty:
        _menu_draw(vm)
        _menu_dirty = False


def stop(vm):
    pass
