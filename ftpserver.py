import socket
import os
import _thread
import network
import errno
import time
import random

from picoware.system.vector import Vector
from picoware.system.buttons import BUTTON_NONE, BUTTON_BACK

# #7 – use Picoware color palette; fall back to inline constants if unavailable
try:
    from picoware.system.colors import (
        TFT_BLACK     as C_BLACK,
        TFT_WHITE     as C_WHITE,
        TFT_RED       as C_RED,
        TFT_GREEN     as C_GREEN,
        TFT_YELLOW    as C_YELLOW,
        TFT_DARKGREY  as C_DARK_GREY,
        TFT_LIGHTGREY as C_LIGHT_GREY,
    )
except ImportError:
    C_BLACK      = 0x0000
    C_WHITE      = 0xFFFF
    C_DARK_GREY  = 0x2104
    C_LIGHT_GREY = 0x8410
    C_RED        = 0xF800
    C_GREEN      = 0x07E0
    C_YELLOW     = 0xFFE0

C_DARK_BLUE = 0x18C3  # header — no equivalent in picoware palette

# #4 – use picoware.system.wifi for IP; fall back to raw network.WLAN
try:
    from picoware.system.wifi import WiFi as _PicoWiFi
    def _get_ip():
        try:
            w = _PicoWiFi()
            if w.is_connected():
                return w.device_ip
        except Exception:
            pass
        return "Not connected"
except ImportError:
    def _get_ip():
        try:
            wlan = network.WLAN(network.STA_IF)
            if wlan.isconnected():
                return wlan.ifconfig()[0]
        except Exception:
            pass
        return "Not connected"

VERSION   = "3.1"

# OSError errno values that mean "no data yet / timed out" — keep looping.
# Everything else (ECONNRESET, ENOTCONN, etc.) means the connection is gone.
_TIMEOUT_ERRNOS = frozenset([
    errno.EAGAIN,    # 11  – would block
    errno.ETIMEDOUT, # 110 – socket timeout
])
FTP_PORT  = 21
FTP_ROOT  = "/sd"
PASV_PORT = 50021

_running     = False
_server_sock = None
_status      = "Starting..."
_client_ip   = None
_device_ip   = "..."
_free_kb     = None
_dirty       = True
_last_btn    = BUTTON_NONE

_IDLE_MS     = 60000  # screensaver activates after 60 s of inactivity
_SS_N        = 45     # star count
_SS_CX       = 160.0
_SS_CY       = 160.0
_last_act_ms = 0
_ss_active   = False
_ss_stars    = []
_ss_next_ms  = 0
_ss_ip_x     = 4.0
_ss_ip_y     = 308.0
_ss_ip_dx    = 0.7
_ss_ip_dy    = 0.4
_transfers   = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_free_kb():
    try:
        s = os.statvfs(FTP_ROOT)
        return s[1] * s[3] // 1024
    except Exception:
        return None


def _safe_stat(path):
    try:
        return os.stat(path)
    except Exception:
        return None


def _ilistdir(real_path):
    real_path = real_path.rstrip("/") or "/"
    entries = []
    try:
        for e in os.ilistdir(real_path):
            entries.append(e)
    except Exception:
        pass
    return entries, real_path


def _list_dir(real_path):
    entries, real_path = _ilistdir(real_path)
    lines = []
    for entry in entries:
        name   = entry[0]
        is_dir = entry[1] == 0x4000
        size   = entry[3] if len(entry) > 3 else 0
        perm   = "drwxr-xr-x" if is_dir else "-rw-r--r--"
        try:
            ts = _fmt_mtime(os.stat(real_path + "/" + name)[8])
        except Exception:
            ts = "Jan  1 00:00"
        lines.append(perm + " 1 0 0 " + str(size) + " " + ts + " " + name)
    return ("\r\n".join(lines) + "\r\n") if lines else "\r\n"


def _list_names(real_path):
    entries, _ = _ilistdir(real_path)
    names = [e[0] for e in entries]
    return ("\r\n".join(names) + "\r\n") if names else "\r\n"


def _resolve(cwd, arg):
    """Return a normalized absolute virtual path (no FTP_ROOT prefix)."""
    raw = arg if arg.startswith("/") else (cwd.rstrip("/") + "/" + arg)
    parts = []
    for p in raw.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/" + "/".join(parts)


def _real(vpath):
    return (FTP_ROOT + "/" + vpath.lstrip("/")).rstrip("/") or FTP_ROOT


_MONTHS = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")

def _mdtm_stamp(mtime):
    try:
        t = time.gmtime(mtime)
        return "{:04d}{:02d}{:02d}{:02d}{:02d}{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5])
    except Exception:
        return "19700101000000"


def _fmt_mtime(mtime):
    try:
        t = time.gmtime(mtime)
        return "{} {:2d} {:02d}:{:02d}".format(_MONTHS[t[1] - 1], t[2], t[3], t[4])
    except Exception:
        return "Jan  1 00:00"


# ---------------------------------------------------------------------------
# Screensaver — warp starfield
# ---------------------------------------------------------------------------

def _ss_reset_star(s):
    while True:
        dx = random.uniform(-1.0, 1.0)
        dy = random.uniform(-1.0, 1.0)
        if dx * dx + dy * dy > 0.04:
            break
    s[0] = _SS_CX + random.uniform(-6.0, 6.0)
    s[1] = _SS_CY + random.uniform(-6.0, 6.0)
    s[2] = dx * 0.25
    s[3] = dy * 0.25
    return s


def _ss_make_star():
    s = _ss_reset_star([0.0, 0.0, 0.0, 0.0])
    # Pre-age so stars are spread across the screen from the first frame.
    for _ in range(random.randint(0, 35)):
        s[2] *= 1.06
        s[3] *= 1.06
        s[0] += s[2]
        s[1] += s[3]
        if s[0] < 0 or s[0] >= 320 or s[1] < 0 or s[1] >= 320:
            _ss_reset_star(s)
            break
    return s


def _ss_draw(vm):
    global _ss_ip_x, _ss_ip_y, _ss_ip_dx, _ss_ip_dy
    d = vm.draw
    d.fill_screen(C_BLACK)
    for s in _ss_stars:
        s[2] *= 1.06
        s[3] *= 1.06
        s[0] += s[2]
        s[1] += s[3]
        x, y = int(s[0]), int(s[1])
        if x < 0 or x >= 320 or y < 0 or y >= 320:
            _ss_reset_star(s)
            continue
        spd2 = s[2] * s[2] + s[3] * s[3]
        if spd2 < 1.0:
            sz, col = 1, C_DARK_GREY
        elif spd2 < 6.25:
            sz, col = 2, C_LIGHT_GREY
        elif spd2 < 25.0:
            sz, col = 3, C_WHITE
        else:
            sz, col = 4, C_YELLOW
        d.fill_rectangle(Vector(x, y), Vector(sz, sz), col)
    ip_w = len(_device_ip) * 6
    _ss_ip_x += _ss_ip_dx
    _ss_ip_y += _ss_ip_dy
    if _ss_ip_x < 0 or _ss_ip_x + ip_w > 320:
        _ss_ip_dx = -_ss_ip_dx
        _ss_ip_x  = max(0.0, min(_ss_ip_x, float(320 - ip_w)))
    if _ss_ip_y < 0 or _ss_ip_y + 8 > 320:
        _ss_ip_dy = -_ss_ip_dy
        _ss_ip_y  = max(0.0, min(_ss_ip_y, 312.0))
    d.text(Vector(int(_ss_ip_x), int(_ss_ip_y)), _device_ip, C_DARK_GREY, 1)
    d.swap()


# ---------------------------------------------------------------------------
# Per-client handler
# ---------------------------------------------------------------------------

def _handle_client(conn, addr):
    global _status, _client_ip, _dirty, _free_kb, _transfers

    _client_ip = addr[0]
    _status    = "Connected: " + addr[0]
    _dirty     = True

    cwd         = "/"
    rename_from = None
    pasv_sock   = None
    active_addr = None
    rest_offset = 0

    def send(msg):
        conn.sendall((msg + "\r\n").encode())

    def open_pasv():
        nonlocal pasv_sock
        # Keep the socket bound for the whole session to avoid close/rebind
        # races after a transfer.  Only create it once; on subsequent PASV
        # commands drain any stale queued connection before re-listening.
        if not pasv_sock:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", PASV_PORT))
            pasv_sock = s
        else:
            pasv_sock.settimeout(0)
            try:
                stale, _ = pasv_sock.accept()
                stale.close()
            except OSError:
                pass
        try:
            pasv_sock.listen(1)
            pasv_sock.settimeout(30)
        except OSError:
            _close_pasv()
            raise

    def _close_pasv():
        nonlocal pasv_sock
        if pasv_sock:
            try:
                pasv_sock.close()
            except Exception:
                pass
            pasv_sock = None

    def get_data_conn():
        nonlocal pasv_sock, active_addr
        if pasv_sock:
            try:
                dc, _ = pasv_sock.accept()
                dc.settimeout(30)
                return dc  # pasv_sock stays alive for the next transfer
            except Exception:
                return None
        if active_addr:
            try:
                dc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                dc.settimeout(10)
                dc.connect(active_addr)
                active_addr = None
                dc.settimeout(30)
                return dc
            except Exception:
                active_addr = None
                return None
        return None

    try:
        send("220 PicoWare FTP Server")
        buf = b""
        conn.settimeout(1.0)

        while _running:
            try:
                chunk = conn.recv(512)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 1024:
                    break
            except OSError as e:
                if e.args[0] in _TIMEOUT_ERRNOS:
                    continue
                break  # connection reset or other unrecoverable error

            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                cmd_line = line.decode("utf-8", "ignore").strip()
                if not cmd_line:
                    continue

                parts = cmd_line.split(None, 1)
                cmd   = parts[0].upper()
                arg   = parts[1] if len(parts) > 1 else ""

                if cmd not in ("PASS", "LIST", "NLST"):
                    _status = cmd + (" " + arg[:18] if arg else "")
                    _dirty  = True

                if cmd != "RNTO":
                    rename_from = None

                # --- AUTH ---
                if cmd == "USER":
                    send("331 Password required")
                elif cmd == "PASS":
                    send("230 Login successful")

                # --- INFO ---
                elif cmd == "SYST":
                    send("215 UNIX Type: L8")
                elif cmd == "FEAT":
                    send("211-Features:\r\n PASV\r\n EPSV\r\n PORT\r\n SIZE\r\n MDTM\r\n REST STREAM\r\n211 End")
                elif cmd in ("OPTS", "NOOP", "CLNT"):
                    send("200 OK")
                elif cmd == "QUIT":
                    send("221 Goodbye")
                    return

                # --- TYPE ---
                elif cmd == "TYPE":
                    send("200 Type set to " + arg)

                # --- NAVIGATION ---
                elif cmd == "PWD":
                    send('257 "' + cwd + '" is current directory')
                elif cmd in ("CWD", "XCWD"):
                    if not arg or arg == ".":
                        send("250 OK")
                        continue
                    vpath = _resolve(cwd, arg)
                    st = _safe_stat(_real(vpath))
                    if st and st[0] & 0x4000:
                        cwd = vpath
                        send("250 OK")
                    elif st:
                        send("550 Not a directory")
                    else:
                        send("550 No such directory")
                elif cmd in ("CDUP", "XCUP"):
                    cwd = _resolve(cwd, "..")
                    send("200 OK")

                # --- DIRECTORY OPS ---
                elif cmd in ("MKD", "XMKD"):
                    if not arg:
                        send("501 Missing directory name")
                    else:
                        vpath = _resolve(cwd, arg)
                        try:
                            os.mkdir(_real(vpath))
                            send('257 "' + vpath + '" created')
                        except Exception as e:
                            send("550 " + str(e))
                elif cmd in ("RMD", "XRMD"):
                    if not arg:
                        send("501 Missing directory name")
                    else:
                        vpath = _resolve(cwd, arg)
                        try:
                            os.rmdir(_real(vpath))
                            send("250 Directory removed")
                            _free_kb = _get_free_kb()
                            _dirty   = True
                        except Exception as e:
                            send("550 " + str(e))

                # --- FILE OPS ---
                elif cmd == "DELE":
                    if not arg:
                        send("501 Missing filename")
                    else:
                        vpath = _resolve(cwd, arg)
                        rpath = _real(vpath)
                        try:
                            os.remove(rpath)
                        except Exception:
                            pass
                        if _safe_stat(rpath):
                            send("550 Delete failed")
                        else:
                            send("250 File deleted")
                            _free_kb = _get_free_kb()
                            _dirty   = True
                elif cmd == "RNFR":
                    if not arg:
                        send("501 Missing filename")
                    else:
                        rename_from = _resolve(cwd, arg)
                        send("350 Ready for RNTO")
                elif cmd == "RNTO":
                    if not rename_from:
                        send("503 RNFR required first")
                    elif not arg:
                        send("501 Missing filename")
                        rename_from = None
                    else:
                        dst = _resolve(cwd, arg)
                        try:
                            os.rename(_real(rename_from), _real(dst))
                            send("250 Renamed")
                        except Exception as e:
                            send("550 " + str(e))
                        rename_from = None
                elif cmd == "SIZE":
                    if not arg:
                        send("501 Missing filename")
                    else:
                        vpath = _resolve(cwd, arg)
                        st = _safe_stat(_real(vpath))
                        if st and not (st[0] & 0x4000):
                            send("213 " + str(st[6]))
                        else:
                            send("550 No such file")
                elif cmd == "REST":
                    try:
                        rest_offset = int(arg)
                        if rest_offset < 0:
                            raise ValueError
                        send("350 Restarting at " + arg)
                    except ValueError:
                        rest_offset = 0
                        send("501 Invalid REST argument")
                elif cmd == "MDTM":
                    if not arg:
                        send("501 Missing filename")
                    else:
                        vpath = _resolve(cwd, arg)
                        st = _safe_stat(_real(vpath))
                        if st:
                            send("213 " + _mdtm_stamp(st[8]))
                        else:
                            send("550 No such file")

                # --- PASSIVE / EXTENDED PASSIVE MODE ---
                elif cmd == "EPSV":
                    try:
                        open_pasv()
                        send("229 Entering Extended Passive Mode (|||" + str(PASV_PORT) + "|)")
                    except Exception as e:
                        send("425 Cannot open data connection: " + str(e))

                elif cmd == "PASV":
                    if _device_ip.count(".") != 3:
                        send("425 No valid IP address")
                    else:
                        try:
                            open_pasv()
                            ip_csv = _device_ip.replace(".", ",")
                            p1 = PASV_PORT >> 8
                            p2 = PASV_PORT & 0xFF
                            send("227 Entering Passive Mode ({},{},{})".format(ip_csv, p1, p2))
                        except Exception as e:
                            send("425 Cannot open data connection: " + str(e))

                elif cmd == "PORT":
                    # PORT h1,h2,h3,h4,p1,p2
                    try:
                        p = arg.split(",")
                        active_addr = (".".join(p[:4]), int(p[4]) * 256 + int(p[5]))
                        send("200 PORT command successful")
                    except Exception as e:
                        send("501 Invalid PORT argument: " + str(e))

                elif cmd == "LPRT":
                    # RFC 1639: |af|n_addr|a1|...|n_port|p1|...|
                    try:
                        p = [x for x in arg.split("|") if x]
                        n_addr = int(p[1])
                        ip     = ".".join(p[2:2 + n_addr])
                        n_port = int(p[2 + n_addr])
                        port   = 0
                        for b in p[3 + n_addr:3 + n_addr + n_port]:
                            port = port * 256 + int(b)
                        active_addr = (ip, port)
                        send("200 LPRT command successful")
                    except Exception as e:
                        send("501 Invalid LPRT argument: " + str(e))

                # --- DATA TRANSFERS ---
                elif cmd in ("LIST", "NLST"):
                    larg = arg
                    while larg.startswith("-"):
                        parts = larg.split(None, 1)
                        larg = parts[1] if len(parts) > 1 else ""
                    vpath = _resolve(cwd, larg) if larg else cwd
                    dc = get_data_conn()
                    if not dc:
                        send("425 No data connection")
                        continue
                    send("150 Opening data connection")
                    try:
                        listing = _list_names(_real(vpath)) if cmd == "NLST" else _list_dir(_real(vpath))
                        dc.sendall(listing.encode())
                        send("226 Transfer complete")
                    except Exception as e:
                        send("426 Transfer aborted: " + str(e))
                    finally:
                        dc.close()

                elif cmd == "RETR":
                    if not arg:
                        send("501 Missing filename")
                        rest_offset = 0
                        continue
                    vpath = _resolve(cwd, arg)
                    st = _safe_stat(_real(vpath))
                    if not st:
                        send("550 No such file")
                        rest_offset = 0
                        continue
                    if st[0] & 0x4000:
                        send("550 Not a file")
                        rest_offset = 0
                        continue
                    if rest_offset and rest_offset > st[6]:
                        send("554 REST offset beyond end of file")
                        rest_offset = 0
                        continue
                    dc = get_data_conn()
                    if not dc:
                        send("425 No data connection")
                        rest_offset = 0
                        continue
                    send("150 Opening data connection")
                    fname = arg.split("/")[-1][:16]
                    offset = rest_offset
                    rest_offset = 0
                    try:
                        total = offset
                        with open(_real(vpath), "rb") as f:
                            if offset:
                                f.seek(offset)
                            dc.settimeout(10)
                            t0 = time.ticks_ms()
                            while True:
                                data = f.read(2048)
                                if not data:
                                    break
                                dc.sendall(data)
                                total += len(data)
                                elapsed = time.ticks_diff(time.ticks_ms(), t0)
                                kbps = ((total - offset) * 1000 // elapsed // 1024) if elapsed > 200 else 0
                                _status = "Sending: {0} {1}KB @ {2}KB/s".format(fname, total // 1024, kbps)
                                _dirty  = True
                        elapsed = time.ticks_diff(time.ticks_ms(), t0)
                        sent = total - offset
                        kbps = (sent * 1000 // elapsed // 1024) if elapsed > 0 else 0
                        send("226 Transfer complete")
                        _transfers += 1
                        _status = "Sent: {0} {1}KB @ {2}KB/s".format(fname, sent // 1024, kbps)
                        _dirty  = True
                    except Exception as e:
                        send("426 Transfer aborted: " + str(e))
                    finally:
                        dc.close()

                elif cmd in ("STOR", "APPE"):
                    if not arg:
                        send("501 Missing filename")
                        continue
                    vpath = _resolve(cwd, arg)
                    st = _safe_stat(_real(vpath))
                    if st and st[0] & 0x4000:
                        send("550 Not a file")
                        continue
                    dc = get_data_conn()
                    if not dc:
                        send("425 No data connection")
                        continue
                    send("150 Opening data connection")
                    mode  = "wb" if cmd == "STOR" else "ab"
                    fname = arg.split("/")[-1][:16]
                    try:
                        total = 0
                        with open(_real(vpath), mode) as f:
                            dc.settimeout(10)
                            t0 = time.ticks_ms()
                            while True:
                                try:
                                    data = dc.recv(2048)
                                except OSError as e:
                                    if e.args[0] == errno.ETIMEDOUT and total > 0:
                                        break  # delayed FIN — treat as EOF
                                    raise
                                if not data:
                                    break
                                f.write(data)
                                total += len(data)
                                elapsed = time.ticks_diff(time.ticks_ms(), t0)
                                kbps = (total * 1000 // elapsed // 1024) if elapsed > 200 else 0
                                _status = "Storing: {0} {1}KB @ {2}KB/s".format(fname, total // 1024, kbps)
                                _dirty  = True
                        elapsed = time.ticks_diff(time.ticks_ms(), t0)
                        kbps = (total * 1000 // elapsed // 1024) if elapsed > 0 else 0
                        send("226 Transfer complete")
                        _transfers += 1
                        _status  = "Stored: {0} {1}KB @ {2}KB/s".format(fname, total // 1024, kbps)
                        _free_kb = _get_free_kb()
                        _dirty   = True
                    except Exception as e:
                        if cmd == "STOR":
                            try:
                                os.remove(_real(vpath))
                            except Exception:
                                pass
                        send("426 Transfer aborted: " + str(e))
                    finally:
                        dc.close()

                elif cmd == "ABOR":
                    _close_pasv()
                    send("225 No transfer to abort")

                elif cmd == "AUTH":
                    send("534 Security mechanisms not supported")
                else:
                    send("502 Command not implemented: " + cmd)

    except Exception:
        pass
    finally:
        _close_pasv()
        conn.close()
        _client_ip = None
        _status    = "Waiting for connection..."
        _dirty     = True


# ---------------------------------------------------------------------------
# Server thread
# ---------------------------------------------------------------------------

def _server_thread():
    global _running, _server_sock, _status, _device_ip, _dirty

    _device_ip = _get_ip()
    _dirty     = True

    try:
        _server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server_sock.bind(("0.0.0.0", FTP_PORT))
        _server_sock.listen(1)
        _server_sock.settimeout(1.0)
        _status = "Waiting for connection..."
        _dirty  = True

        while _running:
            try:
                conn, addr = _server_sock.accept()
            except OSError:
                continue
            _device_ip = _get_ip()
            _dirty     = True
            try:
                _handle_client(conn, addr)
            except Exception:
                pass

    except Exception as e:
        _status = "Error: " + str(e)
        _dirty  = True
    finally:
        if _server_sock:
            try:
                _server_sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# App entry points
# ---------------------------------------------------------------------------

def start(vm):
    global _running, _dirty, _last_btn, _status, _client_ip, _device_ip, _free_kb
    global _last_act_ms, _ss_active, _transfers
    _running     = True
    _dirty       = True
    _last_btn    = BUTTON_NONE
    _client_ip   = None
    _status      = "Starting..."
    _device_ip   = _get_ip()
    _free_kb     = _get_free_kb()
    _last_act_ms = time.ticks_ms()
    _ss_active   = False
    _transfers   = 0
    _thread.start_new_thread(_server_thread, ())
    return True


def run(vm):
    global _dirty, _last_btn, _running
    global _ss_active, _ss_stars, _ss_next_ms, _last_act_ms
    global _ss_ip_x, _ss_ip_y, _ss_ip_dx, _ss_ip_dy

    btn = vm.button
    if btn != _last_btn and btn != BUTTON_NONE:
        _last_btn = btn
        if btn == BUTTON_BACK:
            _running = False
            vm.back()
            return
        vm.input_manager.reset()
        _last_act_ms = time.ticks_ms()
        if _ss_active:
            _ss_active = False
            _dirty = True
            return
    elif btn == BUTTON_NONE:
        _last_btn = BUTTON_NONE

    now = time.ticks_ms()

    # FTP activity or an active connection keeps the idle timer alive.
    if _dirty or _client_ip:
        _last_act_ms = now
        if _ss_active:
            _ss_active = False

    if _ss_active:
        if time.ticks_diff(now, _ss_next_ms) >= 33:  # ~30 fps
            _ss_next_ms = now
            _ss_draw(vm)
        return

    if time.ticks_diff(now, _last_act_ms) > _IDLE_MS:
        _ss_active  = True
        _ss_stars   = [_ss_make_star() for _ in range(_SS_N)]
        _ss_next_ms = now
        _ss_ip_x    = random.uniform(10, 200)
        _ss_ip_y    = random.uniform(10, 250)
        _ss_ip_dx   = 0.7
        _ss_ip_dy   = 0.4
        return

    if _dirty:
        _draw(vm)
        _dirty = False


def stop(vm):
    global _running
    _running = False
    if _server_sock:
        try:
            _server_sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _draw(vm):
    d = vm.draw
    d.fill_screen(C_BLACK)

    # Header
    d.fill_rectangle(Vector(0, 0), Vector(320, 28), C_DARK_BLUE)
    d.text(Vector(6, 6), "FTP Server", C_WHITE, 2)
    d.text(Vector(6, 30), "v" + VERSION, C_LIGHT_GREY, 1)

    # Connection indicator dot
    dot_color = C_GREEN if _client_ip else C_RED
    d.fill_circle(Vector(308, 14), 7, dot_color)

    # Info section
    y = 40
    d.text(Vector(8, y),      "IP:   " + _device_ip, C_WHITE, 1)
    d.text(Vector(8, y + 14), "Port: " + str(FTP_PORT), C_WHITE, 1)
    d.text(Vector(8, y + 28), "PASV: " + str(PASV_PORT), C_LIGHT_GREY, 1)
    # #6 – SD free space
    if _free_kb is None:
        free_str = "unknown"
    elif _free_kb >= 1048576:
        free_str = "{:.1f} GB".format(_free_kb / 1048576)
    elif _free_kb >= 1024:
        free_str = "{:.1f} MB".format(_free_kb / 1024)
    else:
        free_str = str(_free_kb) + " KB"
    d.text(Vector(8, y + 42), "SD:   " + free_str + " free", C_LIGHT_GREY, 1)
    d.text(Vector(8, y + 56), "Files: " + str(_transfers), C_LIGHT_GREY, 1)

    # Divider
    d.fill_rectangle(Vector(0, y + 72), Vector(320, 1), C_DARK_GREY)

    # Status
    y2 = y + 82
    d.text(Vector(8, y2), "Status:", C_LIGHT_GREY, 1)
    status_color = C_GREEN if _client_ip else C_YELLOW
    status       = _status
    d.text(Vector(8, y2 + 14), status[:36],    status_color, 1)
    if len(status) > 36:
        d.text(Vector(8, y2 + 28), status[36:72], status_color, 1)

    # Footer
    d.fill_rectangle(Vector(0, 296), Vector(320, 24), C_DARK_GREY)
    d.text(Vector(8, 302), "BACK: stop & exit", C_LIGHT_GREY, 1)

    d.swap()
