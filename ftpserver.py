import socket
import os
import _thread
import network

from picoware.system.vector import Vector
from picoware.system.buttons import BUTTON_NONE, BUTTON_BACK

C_BLACK      = 0x0000
C_WHITE      = 0xFFFF
C_DARK_BLUE  = 0x18C3
C_DARK_GREY  = 0x2104
C_LIGHT_GREY = 0x8410
C_RED        = 0xF800
C_GREEN      = 0x07E0
C_YELLOW     = 0xFFE0

VERSION   = "1.7"
FTP_PORT  = 21
FTP_ROOT  = "/sd"
PASV_PORT = 50021

_running     = False
_server_sock = None
_status      = "Starting..."
_client_ip   = None
_device_ip   = "..."
_dirty       = True
_last_btn    = BUTTON_NONE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ip():
    try:
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            return wlan.ifconfig()[0]
    except Exception:
        pass
    return "Not connected"


def _safe_stat(path):
    try:
        return os.stat(path)
    except Exception:
        return None


def _list_dir(real_path):
    real_path = real_path.rstrip("/") or "/"
    lines = []
    try:
        entries = os.ilistdir(real_path)
        i = 0
        while i < len(entries):
            entry  = entries[i]
            i     += 1
            name   = entry[0]
            is_dir = entry[1] == 0x4000
            size   = entry[3] if len(entry) > 3 else 0
            perm   = "drwxr-xr-x" if is_dir else "-rw-r--r--"
            lines.append(perm + " 1 0 0 " + str(size) + " Jan 01 00:00 " + name)
    except Exception as e:
        return "-rw-r--r-- 1 0 0 0 Jan 01 00:00 ERR_LISTDIR:" + str(e) + "\r\n"
    return ("\r\n".join(lines) + "\r\n") if lines else "\r\n"


def _resolve(cwd, arg):
    """Return an absolute virtual path (no FTP_ROOT prefix)."""
    if arg.startswith("/"):
        return arg
    return (cwd.rstrip("/") + "/" + arg).replace("//", "/")


def _real(vpath):
    return (FTP_ROOT + "/" + vpath.lstrip("/")).rstrip("/") or FTP_ROOT


# ---------------------------------------------------------------------------
# Per-client handler
# ---------------------------------------------------------------------------

def _handle_client(conn, addr):
    global _status, _client_ip, _dirty

    _client_ip = addr[0]
    _status    = "Connected: " + addr[0]
    _dirty     = True

    cwd         = "/"
    rename_from = None
    pasv_sock   = None
    active_addr = None

    def send(msg):
        conn.sendall((msg + "\r\n").encode())

    def open_pasv():
        nonlocal pasv_sock
        _close_pasv()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", PASV_PORT))
        s.listen(1)
        s.settimeout(30)
        pasv_sock = s

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
                _close_pasv()
                dc.settimeout(30)
                return dc
            except Exception:
                _close_pasv()
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

        while _running:
            conn.settimeout(1.0)
            try:
                chunk = conn.recv(512)
                if not chunk:
                    break
                buf += chunk
            except OSError:
                continue

            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                cmd_line = line.decode("utf-8", "ignore").strip()
                if not cmd_line:
                    continue

                parts = cmd_line.split(" ", 1)
                cmd   = parts[0].upper()
                arg   = parts[1] if len(parts) > 1 else ""

                if cmd not in ("PASS", "LIST", "NLST"):
                    _status = cmd + (" " + arg[:18] if arg else "")
                    _dirty  = True

                # --- AUTH ---
                if cmd == "USER":
                    send("331 Password required")
                elif cmd == "PASS":
                    send("230 Login successful")

                # --- INFO ---
                elif cmd == "SYST":
                    send("215 UNIX Type: L8")
                elif cmd == "FEAT":
                    send("211-Features:\r\n PASV\r\n PORT\r\n SIZE\r\n MDTM\r\n211 End")
                elif cmd == "OPTS":
                    send("200 OK")
                elif cmd == "NOOP":
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
                    if arg == "..":
                        if cwd != "/":
                            cwd = "/" + "/".join(cwd.strip("/").split("/")[:-1])
                            cwd = cwd.rstrip("/") or "/"
                        send("250 OK")
                    else:
                        vpath = _resolve(cwd, arg)
                        if _safe_stat(_real(vpath)):
                            cwd = vpath
                            send("250 OK")
                        else:
                            send("550 No such directory")
                elif cmd in ("CDUP", "XCUP"):
                    if cwd != "/":
                        cwd = "/" + "/".join(cwd.strip("/").split("/")[:-1])
                        cwd = cwd.rstrip("/") or "/"
                    send("200 OK")

                # --- DIRECTORY OPS ---
                elif cmd in ("MKD", "XMKD"):
                    vpath = _resolve(cwd, arg)
                    try:
                        os.mkdir(_real(vpath))
                        send('257 "' + vpath + '" created')
                    except Exception as e:
                        send("550 " + str(e))
                elif cmd in ("RMD", "XRMD"):
                    vpath = _resolve(cwd, arg)
                    try:
                        os.rmdir(_real(vpath))
                        send("250 Directory removed")
                    except Exception as e:
                        send("550 " + str(e))

                # --- FILE OPS ---
                elif cmd == "DELE":
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
                elif cmd == "RNFR":
                    rename_from = _resolve(cwd, arg)
                    send("350 Ready for RNTO")
                elif cmd == "RNTO":
                    if not rename_from:
                        send("503 RNFR required first")
                    else:
                        dst = _resolve(cwd, arg)
                        try:
                            os.rename(_real(rename_from), _real(dst))
                            send("250 Renamed")
                        except Exception as e:
                            send("550 " + str(e))
                        rename_from = None
                elif cmd == "SIZE":
                    vpath = _resolve(cwd, arg)
                    st = _safe_stat(_real(vpath))
                    if st:
                        send("213 " + str(st[6]))
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
                    vpath = _resolve(cwd, arg) if arg else cwd
                    dc = get_data_conn()
                    if not dc:
                        send("425 No data connection")
                        continue
                    send("150 Opening data connection")
                    try:
                        dc.sendall(_list_dir(_real(vpath)).encode())
                        send("226 Transfer complete")
                    except Exception as e:
                        send("426 Transfer aborted: " + str(e))
                    finally:
                        dc.close()

                elif cmd == "RETR":
                    vpath = _resolve(cwd, arg)
                    dc = get_data_conn()
                    if not dc:
                        send("425 No data connection")
                        continue
                    send("150 Opening data connection")
                    try:
                        with open(_real(vpath), "rb") as f:
                            while True:
                                data = f.read(2048)
                                if not data:
                                    break
                                dc.sendall(data)
                        send("226 Transfer complete")
                        _status = "Sent: " + arg.split("/")[-1][:22]
                        _dirty  = True
                    except Exception as e:
                        send("426 Transfer aborted: " + str(e))
                    finally:
                        dc.close()

                elif cmd in ("STOR", "APPE"):
                    vpath = _resolve(cwd, arg)
                    dc = get_data_conn()
                    if not dc:
                        send("425 No data connection")
                        continue
                    send("150 Opening data connection")
                    mode = "wb" if cmd == "STOR" else "ab"
                    try:
                        with open(_real(vpath), mode) as f:
                            while True:
                                data = dc.recv(2048)
                                if not data:
                                    break
                                f.write(data)
                        send("226 Transfer complete")
                        _status = "Stored: " + arg.split("/")[-1][:20]
                        _dirty  = True
                    except Exception as e:
                        send("426 Transfer aborted: " + str(e))
                    finally:
                        dc.close()

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
    global _running, _dirty, _last_btn, _status, _client_ip, _device_ip
    _running    = True
    _dirty      = True
    _last_btn   = BUTTON_NONE
    _client_ip  = None
    _status     = "Starting..."
    _device_ip  = _get_ip()
    _thread.start_new_thread(_server_thread, ())
    return True


def run(vm):
    global _dirty, _last_btn, _running

    btn = vm.button
    if btn != _last_btn and btn != BUTTON_NONE:
        _last_btn = btn
        if btn == BUTTON_BACK:
            _running = False
            vm.back()
            return
        vm.input_manager.reset()
    elif btn == BUTTON_NONE:
        _last_btn = BUTTON_NONE

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

    # Divider
    d.fill_rectangle(Vector(0, y + 48), Vector(320, 1), C_DARK_GREY)

    # Status
    y2 = y + 58
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
