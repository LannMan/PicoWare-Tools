# lcd_repair.py — LCD Burn-In / Image Retention Repair for Picoware (PicoCalc)
#
# Installation: copy this file to picoware/apps/ on your SD card.
#
# Repair techniques implemented:
#   RGB Cycle    – cycles through 8 primary/secondary colors to exercise all
#                  subpixels and break static ion alignment in the LCD layer
#   White Wash   – extended white display followed by color cycling; best for
#                  temporary image retention (works in >95% of LCD cases)
#   Full Spectrum – 4-phase session: RGB cycle → white wash → fast B/W flash
#                  → hue gradient sweep; most thorough treatment
#
# A small status label moves to a new corner every 4 color changes so the
# status text itself cannot cause new retention.
#
# References:
#   https://www.smcclennon.com/posts/lcd-repair/
#   https://www.cdtech-lcd.com/news/how-to-fix-ips-panel-burn-in-and-image-retention-in-30-minutes.html

from utime import ticks_ms, ticks_diff

# ── Module-level state (avoids allocation in the hot loop) ────────────────────
_state     = 0    # 0=menu  1=running  2=done
_mode      = 0    # selected mode index
_start_ms  = 0    # when the current repair session began
_switch_ms = 0    # when the last color switch happened
_color_idx = 0    # monotonic color step counter
_corner    = 0    # 0-3, rotates to move status text around screen
_menu      = None

# ── Repair modes ──────────────────────────────────────────────────────────────
_MODE_NAMES = (
    "RGB Cycle  (15 min)",
    "White Wash (30 min)",
    "Full Spectrum (1 hr)",
    "Fast Flip  (10 min)",
    "White/Black (20 min)",
    "W/B Fast   (10 min)",
)
_MODE_DURATION_MS = (
    15 * 60 * 1000,
    30 * 60 * 1000,
    60 * 60 * 1000,
    10 * 60 * 1000,
    20 * 60 * 1000,
    10 * 60 * 1000,
)

# ── Colors (RGB565) ───────────────────────────────────────────────────────────
_BLK = 0x0000
_WHT = 0xFFFF
_RED = 0xF800
_GRN = 0x07E0
_BLU = 0x001F
_CYN = 0x07FF
_MGT = 0xF81F
_YEL = 0xFFE0

# Full 8-color rotation — starts with RED so the user sees immediate action
_RGB_SEQ   = (_RED, _GRN, _BLU, _WHT, _CYN, _MGT, _YEL, _BLK)
_FLASH_SEQ = (_BLK, _WHT)  # rapid black/white alternation

# Per-color hold times
_MS_RGB   = 2000   # 2 s — slow enough to see each color settle
_MS_WHITE = 6000   # 6 s — longer white hold for ion layer relaxation
_MS_FLASH =  300   # 0.3 s — fast flash to stress-test stuck pixels
_MS_GRAD  =  800   # 0.8 s — gradient hue sweep
_MS_FAST  =   50   # 0.05 s — as fast as the display can keep up with


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hue_rgb565(h):
    """Convert hue value 0-255 to an RGB565 color."""
    h6 = h * 6
    s  = (h6 >> 8) % 6
    f  = h6 & 0xFF
    i  = 255 - f
    if   s == 0: r, g, b = 255, f,   0
    elif s == 1: r, g, b = i,   255, 0
    elif s == 2: r, g, b = 0,   255, f
    elif s == 3: r, g, b = 0,   i,   255
    elif s == 4: r, g, b = f,   0,   255
    else:        r, g, b = 255, 0,   i
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _fmt(ms):
    """Format milliseconds as MM:SS."""
    s = ms // 1000
    return "{:02d}:{:02d}".format(s // 60, s % 60)


def _status_pos(draw, corner):
    """Return a Vector for one of four screen corners (moves status text)."""
    from picoware.system.vector import Vector
    w, h = draw.size.x, draw.size.y
    m = 4
    if corner == 0: return Vector(m,      m)
    if corner == 1: return Vector(w // 2, m)
    if corner == 2: return Vector(m,      h - 18)
    return             Vector(w // 2, h - 18)


def _paint(draw, color, elapsed_ms, total_ms):
    """Fill screen with repair color, then overlay a small moving status label."""
    draw.fill_screen(color)
    pct   = min(100, elapsed_ms * 100 // total_ms)
    label = "{}/{} {:3d}%  BACK=stop".format(_fmt(elapsed_ms), _fmt(total_ms), pct)
    # Use black ink on white, white ink on everything else
    ink = _BLK if color == _WHT else _WHT
    draw.text(_status_pos(draw, _corner), label, ink)
    draw.swap()


def _next_color(elapsed_ms):
    """Return (color, hold_ms) for the current mode phase and step counter."""
    if _mode == 0:  # ── RGB Cycle ─────────────────────────────────────────
        return _RGB_SEQ[_color_idx % len(_RGB_SEQ)], _MS_RGB

    elif _mode == 1:  # ── White Wash ──────────────────────────────────────
        # First 2/3 of session: white hold; last 1/3: RGB cycle
        if elapsed_ms < _MODE_DURATION_MS[1] * 2 // 3:
            return _WHT, _MS_WHITE
        return _RGB_SEQ[_color_idx % len(_RGB_SEQ)], _MS_RGB

    elif _mode == 2:  # ── Full Spectrum ─────────────────────────────────────
        # Divide session into 4 equal phases
        total = _MODE_DURATION_MS[2]
        phase = elapsed_ms * 4 // total   # 0 .. 3
        if   phase == 0: return _RGB_SEQ[_color_idx % len(_RGB_SEQ)], _MS_RGB
        elif phase == 1: return _WHT, _MS_WHITE
        elif phase == 2: return _FLASH_SEQ[_color_idx % 2],           _MS_FLASH
        else:            return _hue_rgb565((_color_idx * 4) % 256),  _MS_GRAD

    elif _mode == 3:  # ── Fast Flip ─────────────────────────────────────────
        return _RGB_SEQ[_color_idx % len(_RGB_SEQ)], _MS_FAST

    elif _mode == 4:  # ── White / Black ─────────────────────────────────────
        return _FLASH_SEQ[_color_idx % 2], 3000   # 3 s full white, 3 s full black

    else:  # ── W/B Fast ─────────────────────────────────────────────────────
        return _FLASH_SEQ[_color_idx % 2], _MS_FLASH


def _build_menu(view_manager):
    from picoware.gui.menu import Menu
    global _menu
    d = view_manager.draw
    _menu = Menu(
        d, "LCD Repair",
        0, d.size.y,
        view_manager.foreground_color,
        view_manager.background_color,
        view_manager.selected_color,
        view_manager.foreground_color,
        2,
    )
    for name in _MODE_NAMES:
        _menu.add_item(name)
    _menu.set_selected(_mode)
    _menu.draw()


# ── Picoware app lifecycle ─────────────────────────────────────────────────────

def start(view_manager) -> bool:
    global _state, _mode, _menu
    _state = 0
    _mode  = 0
    _menu  = None
    _build_menu(view_manager)
    return True


def run(view_manager) -> None:
    from picoware.system.buttons import (
        BUTTON_BACK, BUTTON_UP, BUTTON_DOWN, BUTTON_CENTER,
    )
    global _state, _mode, _start_ms, _switch_ms
    global _color_idx, _corner, _menu

    btn  = view_manager.button
    draw = view_manager.draw

    # ── Menu ──────────────────────────────────────────────────────────────────
    if _state == 0:
        if   btn == BUTTON_UP:     _menu.scroll_up()
        elif btn == BUTTON_DOWN:   _menu.scroll_down()
        elif btn == BUTTON_BACK:   view_manager.back()
        elif btn == BUTTON_CENTER:
            _mode = _menu.selected_index
            # Destroy the menu widget before we take over the display,
            # otherwise LVGL keeps re-rendering it on top of our frames.
            del _menu
            _menu  = None
            _state     = 1
            _start_ms  = ticks_ms()
            _color_idx = 0
            _corner    = 0
            # Paint the first color immediately so the user sees action at once
            color, _ = _next_color(0)
            _paint(draw, color, 0, _MODE_DURATION_MS[_mode])
            _color_idx = 1
            _switch_ms = ticks_ms()
        return

    # ── BACK from running or done → return to menu ────────────────────────────
    if btn == BUTTON_BACK:
        _state = 0
        _build_menu(view_manager)
        return

    # ── Done — show completion screen, wait for BACK ──────────────────────────
    if _state == 2:
        return

    # ── Running ───────────────────────────────────────────────────────────────
    now     = ticks_ms()
    elapsed = ticks_diff(now, _start_ms)
    total   = _MODE_DURATION_MS[_mode]

    if elapsed >= total:
        from picoware.system.vector import Vector
        draw.fill_screen(_BLK)
        cx, cy = draw.size.x // 2, draw.size.y // 2
        draw.text(Vector(cx - 64, cy - 12), "Repair complete!", _WHT)
        draw.text(Vector(cx - 44, cy + 8),  "Press BACK",      _WHT)
        draw.swap()
        _state = 2
        return

    color, hold_ms = _next_color(elapsed)
    since = ticks_diff(now, _switch_ms)

    if since >= hold_ms:
        # Rotate status corner every 4 switches so the label doesn't stay put
        if _color_idx > 0 and _color_idx % 4 == 0:
            _corner = (_corner + 1) % 4

        _paint(draw, color, elapsed, total)
        _color_idx += 1
        _switch_ms  = now


def stop(view_manager) -> None:
    global _menu, _state
    from gc import collect
    _state = 0
    if _menu is not None:
        del _menu
        _menu = None
    collect()
