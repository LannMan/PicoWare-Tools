# Companion Remote for Picoware / PicoCalc
# Install: copy this file to /picoware/apps/companion.py on the device.
#
# Controls — main menu:
#   UP / DN     : select item      CENTER : open
#   L / R       : cycle connection profiles
#
# Controls — remote view:
#   Arrows      : navigate grid (L/R shifts column window, changes page at edge)
#   CENTER      : press button  (hold 600 ms = long press)
#   F2          : mark / unmark highlighted button as favourite
#   F3          : open favourites view
#   F4 / F5     : previous / next column panel
#   '1'-'9'     : press pinned button N  (or jump to page N if N > pinned count)
#   BACK        : return to menu
#
# Controls — favourites view:
#   Arrows      : navigate
#   CENTER      : press button  (hold 600 ms = long press)
#   F2          : remove item from favourites
#   F4          : move item up in list
#   F5          : move item down in list
#   BACK        : return to remote view
#
# Controls — setup:
#   UP / DN     : switch field    type chars/digits/dots    BKSP = delete
#   CENTER      : save and exit   BACK = exit without saving
#   F2          : clear current field
#   F3          : delete current profile (if more than one)
#   F4          : add new profile (copy of current)

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
try:
    from picoware.system.buttons import BUTTON_F2, BUTTON_F3
except ImportError:
    BUTTON_F2 = BUTTON_A
    BUTTON_F3 = BUTTON_Z

try:
    from picoware.system.buttons import BUTTON_F4, BUTTON_F5
except ImportError:
    BUTTON_F4 = BUTTON_A
    BUTTON_F5 = BUTTON_Z

VERSION = "1.2.27"

# ── Colours (RGB565) ──────────────────────────────────────────
C_BLACK      = 0x0000
C_WHITE      = 0xFFFF
C_DARK_BLUE  = 0x18C3
C_DARK_GREY  = 0x2104
C_LIGHT_GREY = 0x8410
C_RED        = 0xF800
C_GREEN      = 0x07E0
C_YELLOW     = 0xFFE0
C_CYAN       = 0x07FF   # favourite marker

# ── Satellite grid constants ──────────────────────────────────
_SAT_COLS = 8
_SAT_ROWS = 4
_SAT_KEYS = _SAT_COLS * _SAT_ROWS   # 32

# ── Grid window constants (4×4 view into 8×4 page) ───────────
_PG_VCOLS  = 4
_PG_VROWS  = 4
_PG_BTN_W  = 75   # 4*75 + 5*4 = 320
_PG_BTN_H  = 56
_PG_GAP    = 4

# ============================================================
# TCP client
# ============================================================
_TCP_IDLE, _TCP_CONNECTING, _TCP_CONNECTED, _TCP_ERROR, _TCP_CLOSED = 0,1,2,3,4


def _dns_parse_a(data):
    """Return first A-record IP from a raw DNS response packet, or None."""
    if len(data) < 12: return None
    if not (data[2] & 0x80): return None          # must be a response
    ancount = (data[6] << 8) | data[7]
    if ancount == 0: return None
    pos = 12
    for _ in range((data[4] << 8) | data[5]):     # skip questions
        while pos < len(data):
            n = data[pos]
            if n == 0: pos += 1; break
            if n & 0xC0 == 0xC0: pos += 2; break
            pos += 1 + n
        pos += 4                                    # skip QTYPE + QCLASS
    for _ in range(ancount):                       # walk answers
        while pos < len(data):                     # skip name
            n = data[pos]
            if n == 0: pos += 1; break
            if n & 0xC0 == 0xC0: pos += 2; break
            pos += 1 + n
        if pos + 10 > len(data): return None
        rtype = (data[pos] << 8) | data[pos + 1]
        rdlen = (data[pos + 8] << 8) | data[pos + 9]
        pos += 10
        if rtype == 1 and rdlen == 4 and pos + 4 <= len(data):
            return f"{data[pos]}.{data[pos+1]}.{data[pos+2]}.{data[pos+3]}"
        pos += rdlen
    return None


def _dns_resolve_local(hostname):
    """Raw UDP A-record query sent directly to the network-configured DNS server.

    lwIP silently drops .local queries internally, so this bypasses it by
    talking to the DNS server the DHCP lease provided.
    """
    try:
        import network
        dns_ip = network.WLAN(network.STA_IF).ifconfig()[3]
        if not dns_ip or dns_ip == "0.0.0.0": return None
    except Exception:
        return None
    name = hostname.lower()
    qname = b""
    for part in name.split("."):
        pb = part.encode()
        qname += bytes([len(pb)]) + pb
    qname += b"\x00"
    pkt = b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3.0)
        s.sendto(pkt, (dns_ip, 53))
        data, _ = s.recvfrom(512)
        s.close()
        return _dns_parse_a(data)
    except Exception:
        return None


_TCP_ERRNO = {
    "-2": "DNS: hostname not found",  "2": "DNS: hostname not found",
    "-110": "Connection timed out",  "110": "Connection timed out",
    "-111": "Connection refused",    "111": "Connection refused",
    "-113": "Host unreachable",      "113": "Host unreachable",
}

def _tcp_err_str(e):
    s = str(e)
    return _TCP_ERRNO.get(s, f"Error: {s}")


class _TCP:
    def __init__(self):
        self.state       = _TCP_IDLE
        self._sock       = None
        self._buf        = b""
        self.resolved_ip = None
        self.error_msg   = ""

    def connect_async(self, ip, port):
        self.state        = _TCP_CONNECTING
        self._buf         = b""
        self.resolved_ip  = None
        self.error_msg    = ""
        _thread.start_new_thread(self._do_connect, (ip, port))

    def _do_connect(self, ip, port):
        try:
            # lwIP silently drops .local queries; bypass it with a raw DNS query
            if ip.lower().endswith(".local"):
                resolved = _dns_resolve_local(ip)
                if resolved:
                    self.resolved_ip = resolved
                    ip = resolved
            info   = socket.getaddrinfo(ip, port, 0, socket.SOCK_STREAM)[0]
            family = info[0]
            addr   = info[-1]
            if not self.resolved_ip:
                self.resolved_ip = addr[0]
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect(addr)
            s.settimeout(0.005)
            self._sock = s
            self.state = _TCP_CONNECTED
        except Exception as e:
            self.error_msg = _tcp_err_str(e)
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
            pass

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
# Satellite protocol
# ============================================================
_SAT_DEVICE_ID    = "picoware-comp"
_SAT_PRODUCT_NAME = "PicoCalc Remote"


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
        self._tcp        = _TCP()
        self.registered  = False
        self.page        = 1
        self.page_keys   = {}
        self._ping_ts    = 0
        self.last_key_ts = 0   # timestamp of most-recent KEY-STATE received

    def connect(self, ip, port):
        self.registered  = False
        self.page        = 1
        self.page_keys   = {}
        self._ping_ts    = 0
        self.last_key_ts = 0
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
        self._tcp.send(f'CHANGE-PAGE DEVICEID="{_SAT_DEVICE_ID}" PAGE={page - 1}\n')

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
            self.last_key_ts = utime.ticks_ms()


# ============================================================
# Config  (/sd/picoware/settings/companion.json)
# ============================================================
_CFG_PATH        = "/sd/picoware/settings/companion.json"
_CFG_PATH_LEGACY = "/sd/companion/config.json"
_cfg = {
    "profiles":  [{"name": "Default", "ip": "192.168.1.100", "port": 16622, "favorites": []}],
    "active":    0,
    "valid":     False,
}


def _active_profile():
    ps  = _cfg.get("profiles", [])
    idx = _cfg.get("active", 0)
    if ps and 0 <= idx < len(ps):
        return ps[idx]
    return {"name": "Default", "ip": "192.168.1.100", "port": 16622, "favorites": []}


def _favs():
    p = _active_profile()
    if "favorites" not in p:
        p["favorites"] = []
    return p["favorites"]


def _cfg_load():
    for path in (_CFG_PATH, _CFG_PATH_LEGACY):
        try:
            with open(path) as f:
                d = json.load(f)
            if "profiles" in d:
                _cfg["profiles"] = d["profiles"]
                _cfg["active"]   = int(d.get("active", 0))
                # migrate: if old format had top-level favorites, move them into active profile
                top_favs = d.get("favorites", d.get("buttons", []))
                if top_favs:
                    ap = _cfg["profiles"][_cfg["active"]]
                    if "favorites" not in ap or not ap["favorites"]:
                        ap["favorites"] = top_favs
            else:
                ip   = d.get("ip",   "192.168.1.100")
                port = int(d.get("port", 16622))
                favs = d.get("favorites", d.get("buttons", []))
                _cfg["profiles"] = [{"name": "Default", "ip": ip, "port": port, "favorites": favs}]
                _cfg["active"]   = 0
            _cfg["valid"] = True
            if path == _CFG_PATH_LEGACY:
                _cfg_save()  # migrate to new location
            return True
        except:
            pass
    return False


def _cfg_save():
    try:
        try: os.mkdir("/sd/picoware/settings")
        except: pass
        with open(_CFG_PATH, "w") as f:
            json.dump({"profiles": _cfg["profiles"],
                       "active":   _cfg["active"]}, f)
        _cfg["valid"] = True
        return True
    except: return False


# ============================================================
# Shared drawing helpers
# ============================================================
def _hdr(d, title):
    d.fill_rectangle(Vector(0, 0), Vector(320, 24), C_DARK_BLUE)
    d.text(Vector(4, 8), title, C_WHITE, 1)


def _btn_text_lines(text, max_chars):
    if len(text) <= max_chars:
        return [text]
    best = -1; mid = len(text) // 2
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
    if not text:
        return
    pad = 4; avail_w = bw - pad * 2
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


def _fmt_ago(ts):
    if ts == 0: return "---"
    s = utime.ticks_diff(utime.ticks_ms(), ts) // 1000
    if s < 2:  return "live"
    if s < 60: return f"{s}s"
    return "old"


# ============================================================
# Favourites helpers
# ============================================================
def _is_favourite(page, row, col):
    return any(f["page"] == page and f["row"] == row and f["col"] == col
               for f in _favs())


def _toggle_favourite(page, row, col):
    favs = _favs()
    for i, f in enumerate(favs):
        if f["page"] == page and f["row"] == row and f["col"] == col:
            favs.pop(i)
            return False
    if len(favs) < 32:
        favs.append({"page": page, "row": row, "col": col})
        return True
    return False


# ============================================================
# Cursor navigation (arrow keys, page change at edge)
# ============================================================
def _nav_cursor(cursor, col_off, btn, page, sat):
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
    return cursor, col_off, page, False


# ============================================================
# ── REMOTE VIEW ──────────────────────────────────────────────
# ============================================================
_REM_HDR_H  = 24
_REM_PIN_H  = 50   # pinned-row height (display only)
_REM_GRID_Y = _REM_HDR_H + _REM_PIN_H + _PG_GAP   # = 78

_rem_sat         = None
_rem_page        = 1
_rem_col_off     = 0
_rem_cursor      = 0
_rem_prev_page   = 1
_rem_state       = "idle"   # connecting/syncing/ready/reconnecting/error
_rem_retry       = 0
_rem_retry_ts    = 0
_rem_sync_pages  = []
_rem_sync_idx    = 0
_rem_sync_ts     = 0
_rem_resync_idx  = -1
_rem_resync_ts   = 0
_rem_page_sync_ts = 0   # set on manual page nav; re-sends CHANGE-PAGE after 300 ms
_rem_center_ts   = 0
_rem_center_long = False
_rem_center_row  = 0
_rem_center_col  = 0
_rem_center_page = 1
_rem_err_msg     = ""
_rem_mark_msg    = ""   # brief toast: "Marked!" / "Removed!"
_rem_mark_ts     = 0
_rem_dirty       = True
_rem_last_btn    = BUTTON_NONE
_rem_draw_ts     = 0


def _rem_fav_pages():
    pages = sorted(set(f["page"] for f in _favs()))
    return pages or [1]


def _rem_draw_header(d):
    c0 = _rem_col_off + 1; c1 = _rem_col_off + _PG_VCOLS
    left = f"Pg{_rem_page} C:{c0}-{c1}"
    if _rem_state == "ready":
        dot_col = C_GREEN;  st = "CON"
    elif _rem_state in ("connecting", "syncing"):
        dot_col = C_YELLOW; st = "SYN"
    elif _rem_state == "reconnecting":
        dot_col = C_YELLOW; st = f"R{_rem_retry}/3"
    else:
        dot_col = C_RED;    st = "ERR"
    ago   = _fmt_ago(_rem_sat.last_key_ts if _rem_sat else 0)
    right = f"{st} {ago}"
    d.fill_rectangle(Vector(0, 0), Vector(320, _REM_HDR_H), C_DARK_BLUE)
    d.text(Vector(4, 8), left, C_WHITE, 1)
    dot_x = 320 - len(right) * 8 - 16
    d.fill_circle(Vector(dot_x, 12), 4, dot_col)
    d.text(Vector(dot_x + 10, 8), right, C_WHITE, 1)


def _rem_draw_pinned(d):
    favs = _favs()[:_PG_VCOLS]
    if not favs:
        return
    for i, f in enumerate(favs):
        x  = i * (_PG_BTN_W + _PG_GAP) + _PG_GAP
        y  = _REM_HDR_H + 2
        pg = f["page"]; key = f["row"] * _SAT_COLS + f["col"]
        bg = C_DARK_GREY; lbl = ""
        if _rem_sat and pg in _rem_sat.page_keys:
            bs = _rem_sat.page_keys[pg][key]
            if bs.valid: bg = bs.bg or C_DARK_GREY; lbl = bs.text
        bh = _REM_PIN_H - 4
        d.fill_rectangle(Vector(x, y), Vector(_PG_BTN_W, bh), bg)
        _draw_btn_text(d, x, y, _PG_BTN_W, bh, lbl, _contrast(bg))
        d.text(Vector(x + 2, y + 2), str(i + 1), C_LIGHT_GREY, 1)


def _rem_draw_grid(d):
    for r in range(_PG_VROWS):
        for c in range(_PG_VCOLS):
            cur_idx = r * _PG_VCOLS + c
            key_idx = r * _SAT_COLS + _rem_col_off + c
            x = c * (_PG_BTN_W + _PG_GAP) + _PG_GAP
            y = _REM_GRID_Y + r * (_PG_BTN_H + _PG_GAP)
            bg = C_DARK_GREY; lbl = ""
            if _rem_sat and _rem_page in _rem_sat.page_keys:
                bs = _rem_sat.page_keys[_rem_page][key_idx]
                if bs.valid: bg = bs.bg or C_DARK_GREY; lbl = bs.text
            fav = _is_favourite(_rem_page, r, _rem_col_off + c)
            if cur_idx == _rem_cursor:
                # cyan border when current button is a favourite, white otherwise
                border = C_CYAN if fav else C_WHITE
                d.fill_rectangle(Vector(x-2, y-2), Vector(_PG_BTN_W+4, _PG_BTN_H+4), border)
            d.fill_rectangle(Vector(x, y), Vector(_PG_BTN_W, _PG_BTN_H), bg)
            if fav:
                # filled cyan bar across the top of the button
                d.fill_rectangle(Vector(x, y), Vector(_PG_BTN_W, 5), C_CYAN)
            if lbl:
                _draw_btn_text(d, x, y, _PG_BTN_W, _PG_BTN_H, lbl, _contrast(bg))


def _rem_draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    if _rem_state in ("connecting", "reconnecting"):
        _hdr(d, "Companion Remote")
        p = _active_profile()
        resolved = _rem_sat._tcp.resolved_ip if _rem_sat else None
        if _rem_state == "connecting":
            d.text(Vector(4, 50), f"Connecting to {p['ip']}...", C_YELLOW, 1)
        else:
            d.text(Vector(4, 50), f"Reconnecting... ({_rem_retry}/3)", C_YELLOW, 1)
        if resolved and resolved != p["ip"]:
            d.text(Vector(4, 68), f"Resolved: {resolved}:{p['port']}", C_LIGHT_GREY, 1)
        else:
            d.text(Vector(4, 68), f"Port {p['port']}", C_LIGHT_GREY, 1)
        d.swap(); return
    if _rem_state == "error":
        _hdr(d, "Companion Remote")
        p = _active_profile()
        msg = _rem_err_msg or "Connection error."
        d.text(Vector(4, 50), msg[:39], C_RED, 1)
        if len(msg) > 39:
            d.text(Vector(4, 64), msg[39:78], C_RED, 1)
        d.text(Vector(4, 84), f"{p['ip']}:{p['port']}", C_LIGHT_GREY, 1)
        d.text(Vector(4, 110), "CENTER: open Setup", C_YELLOW, 1)
        d.text(Vector(4, 126), "BACK: return to menu", C_LIGHT_GREY, 1)
        d.swap(); return
    _rem_draw_header(d)
    _rem_draw_pinned(d)
    if _rem_state == "syncing":
        n  = len(_rem_sync_pages)
        pg = _rem_sync_pages[_rem_sync_idx] if _rem_sync_idx < n else "?"
        d.text(Vector(4, _REM_GRID_Y + 20),
               f"Syncing page {pg}  ({_rem_sync_idx+1}/{n})", C_YELLOW, 1)
    else:
        _rem_draw_grid(d)
        # show loading hint while waiting for KEY-STATE from Companion
        if _rem_page_sync_ts or (
                _rem_sat and _rem_page not in _rem_sat.page_keys):
            d.text(Vector(4, _REM_GRID_Y + 20),
                   f"Loading page {_rem_page}...", C_YELLOW, 1)
        if _rem_mark_msg and utime.ticks_diff(utime.ticks_ms(), _rem_mark_ts) < 1500:
            tw = len(_rem_mark_msg) * 8
            d.fill_rectangle(Vector(0, 300), Vector(320, 20), C_DARK_BLUE)
            d.text(Vector((320 - tw) // 2, 304), _rem_mark_msg, C_CYAN, 1)
    d.swap()


def _rem_press(row, col, page, long_press):
    if not _rem_sat or not _rem_sat.connected:
        return
    key = row * _SAT_COLS + col
    if _rem_sat.page != page:
        _rem_sat.set_page(page)
        utime.sleep_ms(80)
    if long_press:
        _rem_sat.key_press(key, True)   # released later on button-up
    else:
        _rem_sat.key_press(key, True)
        utime.sleep_ms(50)
        _rem_sat.key_press(key, False)


def _rem_start(vm):
    global _rem_sat, _rem_page, _rem_col_off, _rem_cursor, _rem_prev_page
    global _rem_state, _rem_retry, _rem_retry_ts
    global _rem_sync_pages, _rem_sync_idx, _rem_sync_ts
    global _rem_resync_idx, _rem_resync_ts, _rem_page_sync_ts
    global _rem_center_ts, _rem_center_long, _rem_center_row, _rem_center_col, _rem_center_page
    global _rem_err_msg, _rem_mark_msg, _rem_mark_ts, _rem_dirty, _rem_last_btn, _rem_draw_ts

    _rem_col_off = 0; _rem_cursor = 0
    _rem_retry = 0; _rem_retry_ts = 0
    _rem_sync_pages = []; _rem_sync_idx = 0; _rem_sync_ts = 0
    _rem_resync_idx = -1; _rem_resync_ts = 0; _rem_page_sync_ts = 0
    _rem_center_ts = 0; _rem_center_long = False
    _rem_center_row = 0; _rem_center_col = 0; _rem_center_page = 1
    _rem_err_msg = ""; _rem_mark_msg = ""; _rem_mark_ts = 0
    _rem_dirty = True; _rem_last_btn = BUTTON_NONE; _rem_draw_ts = 0

    if not _cfg["valid"]:
        _rem_err_msg = "Not configured — run Setup first."
        _rem_state = "error"; return True

    p = _active_profile()
    if _rem_sat and _rem_sat.connected:
        # returning from fav view — reuse existing connection, trigger re-sync
        _rem_state = "connecting"
    else:
        if _rem_sat: _rem_sat.disconnect()
        _rem_sat = _Satellite()
        _rem_sat.connect(p["ip"], p["port"])
        _rem_state = "connecting"
    return True


def _rem_run(vm):
    global _rem_sat
    global _rem_page, _rem_col_off, _rem_cursor, _rem_prev_page
    global _rem_state, _rem_retry, _rem_retry_ts
    global _rem_sync_pages, _rem_sync_idx, _rem_sync_ts
    global _rem_resync_idx, _rem_resync_ts, _rem_page_sync_ts
    global _rem_center_ts, _rem_center_long, _rem_center_row, _rem_center_col, _rem_center_page
    global _rem_err_msg, _rem_mark_msg, _rem_mark_ts, _rem_dirty, _rem_last_btn, _rem_draw_ts

    if _rem_sat:
        _rem_sat.update()

    now = utime.ticks_ms()

    # ── Connection state machine ──────────────────────────────
    if _rem_sat and _rem_sat.error and _rem_state not in ("error", "reconnecting"):
        if _rem_retry < 3:
            _rem_retry += 1; _rem_state = "reconnecting"
            _rem_retry_ts = now; _rem_dirty = True
        else:
            p = _active_profile()
            tcp_err = _rem_sat._tcp.error_msg if _rem_sat else ""
            _rem_err_msg = tcp_err or f"Cannot reach {p['ip']}:{p['port']}"
            _rem_state = "error"; _rem_dirty = True

    if _rem_state == "reconnecting":
        if utime.ticks_diff(now, _rem_retry_ts) > 5000:
            if _rem_sat: _rem_sat.disconnect()
            _rem_sat = _Satellite()
            p = _active_profile()
            _rem_sat.connect(p["ip"], p["port"])
            _rem_state = "connecting"; _rem_dirty = True

    elif _rem_state == "connecting" and _rem_sat and _rem_sat.connected:
        pages = _rem_fav_pages()
        _rem_sync_pages = pages; _rem_sync_idx = 0; _rem_sync_ts = now
        _rem_sat.set_page(pages[0])
        _rem_state = "syncing"; _rem_dirty = True

    elif _rem_state == "syncing":
        if utime.ticks_diff(now, _rem_sync_ts) > 300:
            _rem_sync_idx += 1
            if _rem_sync_idx >= len(_rem_sync_pages):
                _rem_sat.set_page(_rem_page)
                _rem_state = "ready"; _rem_dirty = True
            else:
                _rem_sat.set_page(_rem_sync_pages[_rem_sync_idx])
                _rem_sync_ts = now; _rem_dirty = True

    elif _rem_state == "ready":
        if utime.ticks_diff(now, _rem_draw_ts) > 150:
            _rem_dirty = True
        if _rem_mark_msg and utime.ticks_diff(now, _rem_mark_ts) >= 1500:
            _rem_mark_msg = ""; _rem_dirty = True
        # re-confirm page 300 ms after manual navigation so Companion pushes fresh KEY-STATE
        if _rem_page_sync_ts and utime.ticks_diff(now, _rem_page_sync_ts) >= 300:
            if _rem_sat: _rem_sat.set_page(_rem_page)
            _rem_page_sync_ts = 0; _rem_dirty = True
        # background resync of favourite pages
        if len(_rem_sync_pages) > 1:
            if _rem_resync_idx < 0 and utime.ticks_diff(now, _rem_resync_ts) > 5000:
                _rem_resync_idx = 0; _rem_resync_ts = now
                _rem_sat.set_page(_rem_sync_pages[0])
            elif _rem_resync_idx >= 0 and utime.ticks_diff(now, _rem_resync_ts) > 300:
                _rem_resync_idx += 1
                if _rem_resync_idx >= len(_rem_sync_pages):
                    _rem_resync_idx = -1; _rem_resync_ts = now
                    _rem_sat.set_page(_rem_page); _rem_dirty = True
                else:
                    _rem_sat.set_page(_rem_sync_pages[_rem_resync_idx])
                    _rem_resync_ts = now

    btn = vm.button

    # ── Long-press tracking for CENTER ────────────────────────
    if btn == BUTTON_CENTER:
        if _rem_center_ts == 0:
            _rem_center_ts   = now
            _rem_center_row  = _rem_cursor // _PG_VCOLS
            _rem_center_col  = _rem_col_off + _rem_cursor % _PG_VCOLS
            _rem_center_page = _rem_page
        elif not _rem_center_long and utime.ticks_diff(now, _rem_center_ts) > 600:
            _rem_center_long = True
            if _rem_state == "ready":
                _rem_press(_rem_center_row, _rem_center_col, _rem_center_page, long_press=True)
    elif _rem_center_ts > 0:
        # CENTER released
        if _rem_state == "ready" and _rem_sat:
            if _rem_center_long:
                key = _rem_center_row * _SAT_COLS + _rem_center_col
                _rem_sat.key_press(key, False)   # release long press
            else:
                _rem_press(_rem_center_row, _rem_center_col, _rem_center_page, long_press=False)
            if _rem_sat.page != _rem_page:
                _rem_sat.set_page(_rem_page)
        elif _rem_state == "error":
            from picoware.system.view import View
            if vm.get_view("_comp_setup") is None:
                vm.add(View("_comp_setup", _sw_run, _sw_start, _sw_stop))
            _rem_center_ts = 0; _rem_center_long = False
            vm.switch_to("_comp_setup"); return
        _rem_center_ts = 0; _rem_center_long = False; _rem_dirty = True

    # ── Edge-triggered input for all other buttons ────────────
    if btn != _rem_last_btn and btn not in (BUTTON_NONE, BUTTON_CENTER):
        _rem_last_btn = btn

        if btn == BUTTON_BACK:
            vm.back(); return

        elif btn in (BUTTON_UP, BUTTON_DOWN, BUTTON_LEFT, BUTTON_RIGHT):
            nc, no, np, changed = _nav_cursor(
                _rem_cursor, _rem_col_off, btn, _rem_page, _rem_sat)
            if changed:
                if np != _rem_page:
                    _rem_prev_page = _rem_page
                    _rem_resync_idx = -1; _rem_resync_ts = now
                    _rem_page_sync_ts = now
                _rem_page = np; _rem_cursor = nc; _rem_col_off = no
                _rem_dirty = True

        elif btn == BUTTON_F2:
            r = _rem_cursor // _PG_VCOLS
            c = _rem_col_off + _rem_cursor % _PG_VCOLS
            added = _toggle_favourite(_rem_page, r, c)
            _rem_mark_msg = "Marked!" if added else "Removed!"
            _rem_mark_ts  = now
            _rem_dirty = True

        elif btn == BUTTON_F3:
            if _rem_state == "ready":
                from picoware.system.view import View
                if vm.get_view("_comp_fav") is None:
                    vm.add(View("_comp_fav", _fav_run, _fav_start, _fav_stop))
                vm.switch_to("_comp_fav")
                return

        elif btn == BUTTON_F4:
            if _rem_col_off >= _PG_VCOLS:
                _rem_col_off -= _PG_VCOLS; _rem_dirty = True

        elif btn == BUTTON_F5:
            if _rem_col_off + _PG_VCOLS < _SAT_COLS:
                _rem_col_off += _PG_VCOLS; _rem_dirty = True

        else:
            ch = vm.input_manager.button_to_char(btn)
            if ch and '1' <= ch <= '9':
                n = int(ch)
                favs = _favs()
                if n <= len(favs):
                    f = favs[n - 1]
                    if _rem_state == "ready":
                        _rem_press(f["row"], f["col"], f["page"], long_press=False)
                        if _rem_sat and _rem_sat.page != _rem_page:
                            _rem_sat.set_page(_rem_page)
                    _rem_dirty = True
                else:
                    new_pg = n
                    if new_pg != _rem_page:
                        _rem_prev_page = _rem_page
                        _rem_page = new_pg
                        if _rem_sat: _rem_sat.set_page(new_pg)
                        _rem_resync_idx = -1; _rem_resync_ts = now
                        _rem_dirty = True

        vm.input_manager.reset()

    elif btn == BUTTON_NONE and _rem_center_ts == 0:
        _rem_last_btn = BUTTON_NONE

    if _rem_dirty or utime.ticks_diff(now, _rem_draw_ts) > 100:
        _rem_draw(vm)
        _rem_dirty = False; _rem_draw_ts = now


def _rem_stop(vm):
    global _rem_state
    _cfg_save()
    _rem_state = "idle"


# ============================================================
# ── FAVOURITES VIEW ──────────────────────────────────────────
# ============================================================
_FAV_HDR_H = 24
_FAV_COLS  = 3
_FAV_BTN_W = 102   # (320 - 4*4) / 3 ≈ 101 → use 102 with 3px gaps: 3*102+4*3=318
_FAV_BTN_H = 72
_FAV_GAP   = 3
_FAV_FTR_H = 20

_fav_cursor      = 0
_fav_scroll      = 0
_fav_dirty       = True
_fav_draw_ts     = 0
_fav_last_btn    = BUTTON_NONE
_fav_center_ts   = 0
_fav_center_long = False
_fav_center_idx  = -1
_fav_sync_pages  = []
_fav_sync_idx    = -1
_fav_sync_ts     = 0


def _fav_rows_vis():
    return (320 - _FAV_HDR_H - _FAV_FTR_H) // (_FAV_BTN_H + _FAV_GAP)


def _fav_max_vis():
    return _fav_rows_vis() * _FAV_COLS


def _fav_btn_state(f):
    pg  = f["page"]; key = f["row"] * _SAT_COLS + f["col"]
    if _rem_sat and pg in _rem_sat.page_keys:
        return _rem_sat.page_keys[pg][key]
    return None


def _fav_draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    favs = _favs()
    n    = len(favs)
    _hdr(d, f"Favourites  ({n})")
    if n == 0:
        d.text(Vector(4, 50), "No favourites yet.", C_LIGHT_GREY, 1)
        d.text(Vector(4, 70), "BACK to return.", C_LIGHT_GREY, 1)
        d.swap(); return
    visible = _fav_max_vis()
    end     = min(_fav_scroll + visible, n)
    for i in range(_fav_scroll, end):
        local = i - _fav_scroll
        col   = local % _FAV_COLS
        row   = local // _FAV_COLS
        x = col * (_FAV_BTN_W + _FAV_GAP) + _FAV_GAP
        y = _FAV_HDR_H + row * (_FAV_BTN_H + _FAV_GAP) + _FAV_GAP
        bg = C_DARK_GREY; lbl = ""
        bs = _fav_btn_state(favs[i])
        if bs and bs.valid: bg = bs.bg or C_DARK_GREY; lbl = bs.text
        if i == _fav_cursor:
            d.fill_rectangle(Vector(x-2, y-2), Vector(_FAV_BTN_W+4, _FAV_BTN_H+4), C_WHITE)
        d.fill_rectangle(Vector(x, y), Vector(_FAV_BTN_W, _FAV_BTN_H), bg)
        _draw_btn_text(d, x, y, _FAV_BTN_W, _FAV_BTN_H, lbl, _contrast(bg))
    hint = "F2:remove  F4/F5:reorder  CTR:press"
    d.text(Vector(4, 320 - _FAV_FTR_H + 4), hint, C_LIGHT_GREY, 1)
    d.swap()


def _fav_press(idx, long_press):
    if not _rem_sat or not _rem_sat.connected: return
    favs = _favs()
    if idx < 0 or idx >= len(favs): return
    f   = favs[idx]
    key = f["row"] * _SAT_COLS + f["col"]
    if _rem_sat.page != f["page"]:
        _rem_sat.set_page(f["page"]); utime.sleep_ms(80)
    if long_press:
        _rem_sat.key_press(key, True)
    else:
        _rem_sat.key_press(key, True)
        utime.sleep_ms(50)
        _rem_sat.key_press(key, False)


def _fav_start(vm):
    global _fav_cursor, _fav_scroll, _fav_dirty, _fav_draw_ts, _fav_last_btn
    global _fav_center_ts, _fav_center_long, _fav_center_idx
    global _fav_sync_pages, _fav_sync_idx, _fav_sync_ts
    _fav_cursor = 0; _fav_scroll = 0
    _fav_dirty  = True; _fav_draw_ts = 0; _fav_last_btn = BUTTON_NONE
    _fav_center_ts = 0; _fav_center_long = False; _fav_center_idx = -1
    # request each favourite page from Companion so KEY-STATE data arrives
    pages = sorted(set(f["page"] for f in _favs()))
    if pages and _rem_sat and _rem_sat.connected:
        _fav_sync_pages = pages
        _fav_sync_idx   = 0
        _fav_sync_ts    = utime.ticks_ms()
        _rem_sat.set_page(pages[0])
    else:
        _fav_sync_pages = []; _fav_sync_idx = -1; _fav_sync_ts = 0
    return True


def _fav_run(vm):
    global _fav_cursor, _fav_scroll, _fav_dirty, _fav_last_btn
    global _fav_center_ts, _fav_center_long, _fav_center_idx

    global _fav_sync_pages, _fav_sync_idx, _fav_sync_ts, _fav_draw_ts

    if _rem_sat: _rem_sat.update()   # keep connection alive, receive KEY-STATE

    favs    = _favs()
    n       = len(favs)
    visible = _fav_max_vis()
    now     = utime.ticks_ms()

    # advance mini page-sync so KEY-STATE arrives for all favourite pages
    if _fav_sync_idx >= 0 and _rem_sat and _rem_sat.connected:
        if utime.ticks_diff(now, _fav_sync_ts) > 300:
            _fav_sync_idx += 1
            if _fav_sync_idx >= len(_fav_sync_pages):
                _fav_sync_idx = -1
                if _rem_sat.page != _rem_page:
                    _rem_sat.set_page(_rem_page)
            else:
                _rem_sat.set_page(_fav_sync_pages[_fav_sync_idx])
                _fav_sync_ts = now
            _fav_dirty = True

    # redraw periodically so buttons populate as KEY-STATE arrives
    if utime.ticks_diff(now, _fav_draw_ts) > 200:
        _fav_dirty = True

    btn     = vm.button

    # ── Long-press tracking ───────────────────────────────────
    if btn == BUTTON_CENTER:
        if _fav_center_ts == 0:
            _fav_center_ts  = now
            _fav_center_idx = _fav_cursor
        elif not _fav_center_long and utime.ticks_diff(now, _fav_center_ts) > 600:
            _fav_center_long = True
            _fav_press(_fav_center_idx, long_press=True)
    elif _fav_center_ts > 0:
        if _fav_center_long:
            if _rem_sat:
                f   = favs[_fav_center_idx] if 0 <= _fav_center_idx < len(favs) else None
                if f:
                    key = f["row"] * _SAT_COLS + f["col"]
                    _rem_sat.key_press(key, False)
                if _rem_sat.page != _rem_page:
                    _rem_sat.set_page(_rem_page)
        else:
            _fav_press(_fav_center_idx, long_press=False)
            if _rem_sat and _rem_sat.page != _rem_page:
                _rem_sat.set_page(_rem_page)
        _fav_center_ts = 0; _fav_center_long = False; _fav_center_idx = -1
        _fav_dirty = True

    # ── Edge-triggered input ──────────────────────────────────
    if btn != _fav_last_btn and btn not in (BUTTON_NONE, BUTTON_CENTER):
        _fav_last_btn = btn

        if btn == BUTTON_BACK:
            vm.back(); return

        elif btn == BUTTON_UP:
            if _fav_cursor >= _FAV_COLS:
                _fav_cursor -= _FAV_COLS
                if _fav_cursor < _fav_scroll:
                    _fav_scroll = (_fav_cursor // _FAV_COLS) * _FAV_COLS
                _fav_dirty = True

        elif btn == BUTTON_DOWN:
            if _fav_cursor + _FAV_COLS < n:
                _fav_cursor += _FAV_COLS
                while _fav_cursor >= _fav_scroll + visible:
                    _fav_scroll += _FAV_COLS
                _fav_dirty = True

        elif btn == BUTTON_LEFT:
            if _fav_cursor % _FAV_COLS > 0:
                _fav_cursor -= 1; _fav_dirty = True

        elif btn == BUTTON_RIGHT:
            if _fav_cursor % _FAV_COLS < _FAV_COLS - 1 and _fav_cursor + 1 < n:
                _fav_cursor += 1; _fav_dirty = True

        elif btn == BUTTON_F4:
            if n > 0 and _fav_cursor > 0:
                i = _fav_cursor
                favs[i], favs[i-1] = favs[i-1], favs[i]
                _fav_cursor -= 1; _fav_dirty = True

        elif btn == BUTTON_F5:
            if n > 0 and _fav_cursor < n - 1:
                i = _fav_cursor
                favs[i], favs[i+1] = favs[i+1], favs[i]
                _fav_cursor += 1; _fav_dirty = True

        elif btn == BUTTON_F2:
            if 0 <= _fav_cursor < n:
                favs.pop(_fav_cursor)
                if _fav_cursor >= len(favs) and _fav_cursor > 0:
                    _fav_cursor -= 1
                _fav_dirty = True

        vm.input_manager.reset()

    elif btn == BUTTON_NONE and _fav_center_ts == 0:
        _fav_last_btn = BUTTON_NONE

    if _fav_dirty:
        _fav_draw(vm)
        _fav_dirty = False; _fav_draw_ts = now


def _fav_stop(vm):
    _cfg_save()


# ============================================================
# ── SETUP (Network only) ─────────────────────────────────────
# ============================================================
_sw_dirty     = True
_sw_last_btn  = BUTTON_NONE
_sw_name_buf  = []
_sw_ip_buf    = []
_sw_port_buf  = []
_sw_net_field = 0   # 0=name, 1=host/IP, 2=port
_sw_err       = ""


def _sw_draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    ps  = _cfg.get("profiles", [])
    idx = _cfg.get("active", 0)
    _hdr(d, f"Setup  ({idx + 1}/{len(ps)})")
    name_s  = "".join(_sw_name_buf) or "_"
    ip_s    = "".join(_sw_ip_buf)
    port_s  = "".join(_sw_port_buf) or "_"
    # truncate long host for display — show tail so you can see what you're typing
    ip_disp = (ip_s if len(ip_s) <= 23 else ".." + ip_s[-21:]) if ip_s else "_"

    d.text(Vector(8, 32), "Profile Name:", C_LIGHT_GREY, 1)
    if _sw_net_field == 0:
        d.fill_rectangle(Vector(6, 44), Vector(204, 26), C_WHITE)
    d.fill_rectangle(Vector(8, 46), Vector(200, 22),
                     C_DARK_BLUE if _sw_net_field == 0 else C_DARK_GREY)
    d.text(Vector(12, 52), name_s, C_WHITE, 1)

    d.text(Vector(8, 78), "Host / IP:", C_LIGHT_GREY, 1)
    if _sw_net_field == 1:
        d.fill_rectangle(Vector(6, 90), Vector(204, 26), C_WHITE)
    d.fill_rectangle(Vector(8, 92), Vector(200, 22),
                     C_DARK_BLUE if _sw_net_field == 1 else C_DARK_GREY)
    d.text(Vector(12, 98), ip_disp, C_WHITE, 1)

    d.text(Vector(8, 124), "Port:", C_LIGHT_GREY, 1)
    if _sw_net_field == 2:
        d.fill_rectangle(Vector(6, 136), Vector(124, 26), C_WHITE)
    d.fill_rectangle(Vector(8, 138), Vector(120, 22),
                     C_DARK_BLUE if _sw_net_field == 2 else C_DARK_GREY)
    d.text(Vector(12, 144), port_s, C_WHITE, 1)

    d.text(Vector(8, 178), "UP/DN: field  BKSP: del char  F2: clear", C_LIGHT_GREY, 1)
    d.text(Vector(8, 194), "CTR: save & exit  BACK: cancel", C_LIGHT_GREY, 1)
    d.text(Vector(8, 210), "F3: delete profile  F4: add profile", C_LIGHT_GREY, 1)
    if _sw_err:
        d.text(Vector(8, 240), _sw_err, C_RED, 1)
    d.swap()


def _sw_start(vm):
    global _sw_dirty, _sw_last_btn, _sw_name_buf, _sw_ip_buf, _sw_port_buf, _sw_net_field, _sw_err
    _sw_dirty = True; _sw_last_btn = BUTTON_NONE; _sw_err = ""
    _sw_net_field = 0
    p = _active_profile()
    _sw_name_buf = list(p.get("name", "Default"))
    _sw_ip_buf   = list(p.get("ip",   "192.168.1.100"))
    _sw_port_buf = list(str(p.get("port", 16622)))
    return True


def _sw_run(vm):
    global _sw_dirty, _sw_last_btn, _sw_name_buf, _sw_ip_buf, _sw_port_buf, _sw_net_field, _sw_err

    btn = vm.button
    if btn != _sw_last_btn and btn != BUTTON_NONE:
        _sw_last_btn = btn

        if btn in (BUTTON_UP, BUTTON_DOWN):
            _sw_net_field = (_sw_net_field + (1 if btn == BUTTON_DOWN else -1)) % 3
            _sw_dirty = True

        elif btn == BUTTON_BACKSPACE:
            buf = [_sw_name_buf, _sw_ip_buf, _sw_port_buf][_sw_net_field]
            if buf: buf.pop(); _sw_dirty = True

        elif btn == BUTTON_BACK:
            vm.back(); return

        elif btn == BUTTON_CENTER:
            name = "".join(_sw_name_buf).strip() or "Default"
            ip   = "".join(_sw_ip_buf)   or "192.168.1.100"
            port = "".join(_sw_port_buf) or "16622"
            try: port = int(port)
            except: _sw_err = "Invalid port."; _sw_dirty = True; vm.input_manager.reset(); return
            p = _active_profile()
            p["name"] = name; p["ip"] = ip; p["port"] = port
            if _cfg_save():
                vm.back()
            else:
                _sw_err = "Failed to save config."; _sw_dirty = True
            return

        elif btn == BUTTON_F2:
            if _sw_net_field == 0: _sw_name_buf = []
            elif _sw_net_field == 1: _sw_ip_buf = []
            else: _sw_port_buf = []
            _sw_dirty = True

        elif btn == BUTTON_F3:
            ps = _cfg.get("profiles", [])
            if len(ps) > 1:
                idx = _cfg.get("active", 0)
                ps.pop(idx)
                _cfg["active"] = max(0, idx - 1)
                _cfg_save()
                p = _active_profile()
                _sw_name_buf = list(p.get("name", "Default"))
                _sw_ip_buf   = list(p.get("ip",   "192.168.1.100"))
                _sw_port_buf = list(str(p.get("port", 16622)))
                _sw_err = "Profile deleted."; _sw_dirty = True
            else:
                _sw_err = "Cannot delete the only profile."; _sw_dirty = True

        elif btn == BUTTON_F4 and not vm.input_manager.button_to_char(btn):
            p = _active_profile().copy()
            p["name"] = p.get("name", "Default") + "2"
            _cfg["profiles"].append(p)
            _cfg["active"] = len(_cfg["profiles"]) - 1
            _sw_name_buf = list(p["name"])
            _sw_ip_buf   = list(p["ip"])
            _sw_port_buf = list(str(p["port"]))
            _sw_net_field = 0
            _cfg_save()
            _sw_err = "New profile — edit name, then CTR to save."; _sw_dirty = True

        else:
            ch = vm.input_manager.button_to_char(btn)
            if ch:
                if _sw_net_field == 0 and len(_sw_name_buf) < 20:
                    _sw_name_buf.append(ch); _sw_dirty = True
                elif _sw_net_field == 1 and 33 <= ord(ch) <= 126 and len(_sw_ip_buf) < 128:
                    _sw_ip_buf.append(ch); _sw_dirty = True
                elif _sw_net_field == 2 and ch in "0123456789" and len(_sw_port_buf) < 5:
                    _sw_port_buf.append(ch); _sw_dirty = True

        vm.input_manager.reset()

    elif btn == BUTTON_NONE:
        _sw_last_btn = BUTTON_NONE

    if _sw_dirty:
        _sw_draw(vm)
        _sw_dirty = False


def _sw_stop(vm):
    pass


# ============================================================
# ── MAIN MENU ────────────────────────────────────────────────
# ============================================================
_MENU_ITEMS    = ["Remote", "Setup"]
_menu_cursor   = 0
_menu_dirty    = True
_menu_last_btn = BUTTON_NONE


def _menu_draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)
    _hdr(d, f"Companion Remote  v{VERSION}")
    if _cfg["valid"]:
        p   = _active_profile()
        ps  = _cfg.get("profiles", [])
        nf  = len(_favs())
        fav = f"{nf} favourite{'s' if nf != 1 else ''}"
        if len(ps) > 1:
            idx = _cfg.get("active", 0)
            d.text(Vector(4, 36),
                   f"< {p['name']}  ({idx + 1}/{len(ps)}) >",
                   C_YELLOW, 1)
            d.text(Vector(4, 52), f"{p['ip']}:{p['port']}  {fav}", C_LIGHT_GREY, 1)
        else:
            d.text(Vector(4, 36),
                   f"{p['ip']}:{p['port']}  {fav}",
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
    ps = _cfg.get("profiles", [])
    if len(ps) > 1:
        d.text(Vector(4, 290), "UP/DN: select  L/R: profile  CTR: open", C_LIGHT_GREY, 1)
    else:
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
    global _menu_cursor, _menu_dirty, _menu_last_btn, _rem_sat

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
        elif btn == BUTTON_LEFT:
            ps = _cfg.get("profiles", [])
            if len(ps) > 1:
                _cfg["active"] = (_cfg.get("active", 0) - 1) % len(ps)
                if _rem_sat: _rem_sat.disconnect(); _rem_sat = None
                _menu_dirty = True
        elif btn == BUTTON_RIGHT:
            ps = _cfg.get("profiles", [])
            if len(ps) > 1:
                _cfg["active"] = (_cfg.get("active", 0) + 1) % len(ps)
                if _rem_sat: _rem_sat.disconnect(); _rem_sat = None
                _menu_dirty = True
        elif btn == BUTTON_CENTER:
            if _menu_cursor == 0:
                if vm.get_view("_comp_remote") is None:
                    vm.add(View("_comp_remote", _rem_run, _rem_start, _rem_stop))
                vm.switch_to("_comp_remote")
            else:
                if vm.get_view("_comp_setup") is None:
                    vm.add(View("_comp_setup", _sw_run, _sw_start, _sw_stop))
                vm.switch_to("_comp_setup")
            _menu_dirty = True
            return
        vm.input_manager.reset()
    elif btn == BUTTON_NONE:
        _menu_last_btn = BUTTON_NONE

    if _menu_dirty:
        _menu_draw(vm)
        _menu_dirty = False


def stop(vm):
    global _rem_sat
    if _rem_sat: _rem_sat.disconnect(); _rem_sat = None
