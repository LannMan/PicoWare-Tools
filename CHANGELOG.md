# Changelog

## companion.py — v1.2.27

- Connection profiles: create, rename, and delete multiple host profiles; cycle with L/R on the main menu
- Config migrated to `/sd/picoware/settings/companion.json`; legacy `/sd/companion/config.json` auto-migrated on first load
- Favorites stored per profile
- Hostname / `.local` support: raw UDP DNS query bypasses lwIP which silently drops `.local` lookups
- F4/F5 function keys replace A/Z for column-panel scrolling and favorites reordering
- F4/F5 in main remote view scroll the column panel left/right instead of changing Companion page; page navigation moved back to A/Z
- F2 clears current setup field; F3 deletes the active profile (if more than one); F4 duplicates the profile
- Readable connection error messages translated from lwIP errno codes (-2, -110, -111, -113)
- Shows resolved IP address while connecting; shows error detail string on error screen
- CENTER on error screen now opens Setup directly
- Page sync re-confirmed 300 ms after manual navigation so Companion pushes fresh KEY-STATE
- Digits 1–9 activate pinned favorites (if N ≤ count) or jump to page N
- Fix: send 0-indexed PAGE index to Companion (was off by one)

## companion.py — v1.1.9 *(previous)*

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

## wifi_manager.py — v1.0.7

- Writes `/sd/picoware/wifi/settings.json` (combined `{"ssid": ..., "password": ...}`) on network activation; required by OS v1.8.3+ which reads this file for WiFi credentials on boot

## wifi_manager.py — v1.0.5 *(previous)*

- Activating a network now calls `machine.reset()` after saving so the new credentials take effect immediately
- Status message updated to "Rebooting: {ssid}" to reflect the new behavior

## lcd_repair.py — new

- New app: LCD Burn-In / Image Retention Repair utility
- Three repair modes: RGB Cycle, White Wash, and Full Spectrum
- Status label rotates between screen corners every 4 color changes to prevent the label itself from causing new retention
