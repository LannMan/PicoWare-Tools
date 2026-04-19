import os
import json
from picoware.system.vector import Vector
from picoware.system.buttons import (
    BUTTON_NONE, BUTTON_BACK, BUTTON_UP, BUTTON_DOWN, BUTTON_CENTER,
    BUTTON_A, BUTTON_Z,
)

VERSION = "1.0.3"

C_BLACK      = 0x0000
C_WHITE      = 0xFFFF
C_DARK_BLUE  = 0x18C3
C_DARK_GREY  = 0x2104
C_LIGHT_GREY = 0x8410
C_RED        = 0xF800
C_GREEN      = 0x07E0
C_YELLOW     = 0xFFE0

WIFI_SSID_PATH = "/sd/picoware/wifi/ssid.json"
WIFI_PWD_PATH  = "/sd/picoware/wifi/password.json"
SAVED_CFG_PATH = "/sd/picoware/wifi/wifi_manager.json"

# max visible list rows in the scrollable area
MAX_VISIBLE = 9

_last_btn   = BUTTON_NONE
_dirty      = True
_networks   = []   # list of {"ssid": ..., "password": ...}
_cursor     = 0    # selected index in _networks
_scroll_off = 0    # first visible index
_status_msg = ""   # brief status shown at bottom
_status_ok  = True # True=green, False=red


def _load_wifi_cfg():
    try:
        with open(WIFI_SSID_PATH) as f:
            ssid = json.load(f).get("ssid", "")
        with open(WIFI_PWD_PATH) as f:
            pwd = json.load(f).get("password", "")
        return {"ssid": ssid, "password": pwd}
    except Exception:
        return {"ssid": "", "password": ""}


def _save_wifi_cfg(entry):
    with open(WIFI_SSID_PATH, "w") as f:
        json.dump({"ssid": entry.get("ssid", "")}, f)
    with open(WIFI_PWD_PATH, "w") as f:
        json.dump({"password": entry.get("password", "")}, f)


def _load_networks():
    try:
        with open(SAVED_CFG_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_networks(nets):
    with open(SAVED_CFG_PATH, "w") as f:
        json.dump(nets, f)


def _set_status(msg, ok=True):
    global _status_msg, _status_ok
    _status_msg = msg
    _status_ok  = ok


def _import_current():
    global _networks, _cursor
    cur = _load_wifi_cfg()
    ssid = cur.get("ssid", "")
    if not ssid:
        _set_status("No current SSID to save", ok=False)
        return
    for net in _networks:
        if net.get("ssid") == ssid:
            _set_status("Already saved: " + ssid)
            return
    _networks.append(cur)
    _save_networks(_networks)
    _cursor = len(_networks) - 1
    _set_status("Saved: " + ssid)


def _activate_selected():
    if not _networks:
        _set_status("No networks saved", ok=False)
        return
    entry = _networks[_cursor]
    _save_wifi_cfg(entry)
    _set_status("Active: " + entry.get("ssid", "?"))


def _delete_selected():
    global _networks, _cursor
    if not _networks:
        return
    removed = _networks[_cursor].get("ssid", "?")
    _networks.pop(_cursor)
    _save_networks(_networks)
    if _cursor >= len(_networks) and _cursor > 0:
        _cursor -= 1
    _set_status("Deleted: " + removed, ok=False)


def start(vm):
    global _networks, _cursor, _scroll_off, _dirty, _last_btn, _status_msg
    _networks   = _load_networks()
    _cursor     = 0
    _scroll_off = 0
    _dirty      = True
    _last_btn   = BUTTON_NONE
    _status_msg = ""
    return True


def run(vm):
    global _last_btn, _cursor, _scroll_off, _dirty

    btn = vm.button
    if btn != _last_btn and btn != BUTTON_NONE:
        _last_btn = btn
        _dirty = True

        if btn == BUTTON_BACK:
            vm.back()
            return

        elif btn == BUTTON_UP:
            if _cursor > 0:
                _cursor -= 1
                if _cursor < _scroll_off:
                    _scroll_off = _cursor

        elif btn == BUTTON_DOWN:
            if _cursor < len(_networks) - 1:
                _cursor += 1
                if _cursor >= _scroll_off + MAX_VISIBLE:
                    _scroll_off = _cursor - MAX_VISIBLE + 1

        elif btn == BUTTON_CENTER:
            _activate_selected()

        elif btn == BUTTON_A:
            _import_current()

        elif btn == BUTTON_Z:
            _delete_selected()

        vm.input_manager.reset()

    elif btn == BUTTON_NONE:
        _last_btn = BUTTON_NONE

    if _dirty:
        _draw(vm)
        _dirty = False


def stop(vm):
    pass


def _draw(vm):
    d = vm.draw

    d.fill_screen(C_BLACK)

    # header
    d.fill_rectangle(Vector(0, 0), Vector(320, 28), C_DARK_BLUE)
    d.text(Vector(4, 8), "WiFi Manager", C_WHITE, 1)
    d.text(Vector(248, 10), "v" + VERSION, C_LIGHT_GREY, 1)

    # current active SSID
    cur = _load_wifi_cfg()
    cur_ssid = cur.get("ssid", "") or "(none)"
    d.text(Vector(4, 34), "Active:", C_LIGHT_GREY, 1)
    d.text(Vector(60, 34), cur_ssid[:26], C_GREEN, 1)

    # divider
    d.fill_rectangle(Vector(0, 46), Vector(320, 1), C_DARK_GREY)

    # saved networks list
    row_h = 22
    list_top = 50
    visible = _networks[_scroll_off:_scroll_off + MAX_VISIBLE]
    for i, net in enumerate(visible):
        idx = _scroll_off + i
        y = list_top + i * row_h
        if idx == _cursor:
            d.fill_rectangle(Vector(0, y - 1), Vector(320, row_h), C_DARK_BLUE)
        ssid = net.get("ssid", "?")
        color = C_YELLOW if idx == _cursor else C_WHITE
        d.text(Vector(8, y + 4), ssid[:38], color, 1)

    if not _networks:
        d.text(Vector(8, list_top + 8), "No saved networks", C_LIGHT_GREY, 1)

    # scroll indicators
    if _scroll_off > 0:
        d.text(Vector(308, list_top), "^", C_LIGHT_GREY, 1)
    if _scroll_off + MAX_VISIBLE < len(_networks):
        d.text(Vector(308, list_top + (MAX_VISIBLE - 1) * row_h), "v", C_LIGHT_GREY, 1)

    # divider above hints
    d.fill_rectangle(Vector(0, 271), Vector(320, 1), C_DARK_GREY)

    # hints
    d.text(Vector(4, 275), "A:import  Ctr:activate  Z:del", C_LIGHT_GREY, 1)

    # status bar
    if _status_msg:
        color = C_GREEN if _status_ok else C_RED
        d.text(Vector(4, 288), _status_msg[:38], color, 1)

    d.swap()
