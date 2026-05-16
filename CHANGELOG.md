# Changelog

## companion.py — v1.1.9

- Added favourites system: F2 to mark/unmark a button, F3 to open favourites view
- Favourites view supports reordering (A/Z) and removal (F2)
- F2/F3 buttons imported with graceful fallback to A/Z on older firmware
- Long-press support: hold CENTER 600 ms to send a long press
- Page navigation: A/Z for previous/next page, digits 1–9 to jump directly
- L/R arrow now shifts the column window and auto-advances page at the edge
- Expanded and clarified in-file control reference

## ftpserver.py — v3.1

- Uses `picoware.system.colors` palette (TFT_* constants) with inline fallback for older firmware
- Uses `picoware.system.wifi` for IP address resolution with fallback to raw `network.WLAN`
- Improved OSError handling: distinguishes "no data / timed out" from genuine connection loss so the receive loop no longer drops on transient timeouts

## wifi_manager.py — v1.0.5

- Activating a network now calls `machine.reset()` after saving so the new credentials take effect immediately
- Status message updated to "Rebooting: {ssid}" to reflect the new behavior

## esv_bible.py — new

- New app: ESV Bible reader
- Fetches passages via `picoware.system.http` (ESV API)
- Scrollable, word-wrapped text display; navigate by verse, chapter, and book
- Caches fetched passages to `/sd/picoware/esv/` to reduce network calls

## lcd_repair.py — new

- New app: LCD Burn-In / Image Retention Repair utility
- Three repair modes: RGB Cycle, White Wash, and Full Spectrum
- Status label rotates between screen corners every 4 color changes to prevent the label itself from causing new retention
