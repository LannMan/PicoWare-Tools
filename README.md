# PicoWare Tools

A collection of MicroPython apps for the [PicoCalc](https://github.com/clockworkpi/picoware) handheld running [Picoware](https://github.com/clockworkpi/picoware).

## Apps

| File | Description |
|---|---|
| `wifi_manager.py` | Manage saved WiFi networks on the device |
| `ftpserver.py` | FTP server — push/pull files over Wi-Fi from your desktop |
| `companion.py` | Wireless remote control for [Bitfocus Companion](https://bitfocus.io/companion) |
| `lcd_repair.py` | LCD burn-in and image retention repair utility |

## Installation

Copy the app file(s) you want to `/picoware/apps/` on the device SD card. The Picoware launcher picks them up automatically. The easiest way to transfer files is FTP (use `ftpserver.py` itself to bootstrap):

```sh
curl -T <app>.py ftp://<device-ip>/picoware/apps/<app>.py --user x:x
```

---

## wifi_manager.py — WiFi Manager

Manage saved WiFi networks directly on the device. Switch between networks, import the currently active network, or delete saved entries. Activating a network saves the credentials and immediately reboots the device so the new connection takes effect.

### Controls

| Button | Action |
|---|---|
| UP / DOWN | Navigate saved networks list |
| CENTER | Activate selected network (saves + reboots) |
| A | Import current active network into saved list |
| Z | Delete selected entry |
| BACK | Exit app |

### WiFi config files

| Path | Format | Purpose |
|---|---|---|
| `/sd/picoware/wifi/ssid.json` | `{"ssid": "..."}` | Active SSID (read/written by OS) |
| `/sd/picoware/wifi/password.json` | `{"password": "..."}` | Active password (read/written by OS) |
| `/sd/picoware/wifi/settings.json` | `{"ssid": "...", "password": "..."}` | Combined credentials written on network activation |
| `/sd/picoware/wifi/wifi_manager.json` | `[{"ssid": "...", "password": "..."}, ...]` | Saved networks list (managed by this app) |

---

## ftpserver.py — FTP Server

Serves the SD card over Wi-Fi so you can push and pull files from any desktop FTP client. Useful for installing or updating apps without removing the SD card.

### Usage

1. Launch **FTP Server** from the Picoware app list.
2. The screen shows the device IP address and port (21).
3. Connect from your desktop with any FTP client.
4. Press **BACK** to stop the server and return to the launcher.

### Connecting

| Setting | Value |
|---|---|
| Host | IP shown on device screen |
| Port | 21 |
| Protocol | FTP (plain, not FTPS) |
| Mode | Passive (PASV) |
| Username | anything |
| Password | anything |

**FileZilla:** Site Manager → New Site → Protocol: FTP, Encryption: Only use plain FTP, Transfer mode: Passive.

**lftp:**
```sh
lftp ftp://<device-ip>
```

**curl:**
```sh
curl -T myfile.py ftp://<device-ip>/picoware/apps/myfile.py --user x:x
```

### Notes

- Only one client can connect at a time.
- Passive mode only — active mode (`PORT`) is not supported.
- The device must be connected to Wi-Fi before launching.
- Data port: 50021.

---

## companion.py — Companion Remote

Turns the PicoCalc into a wireless remote control for [Bitfocus Companion](https://bitfocus.io/companion). Connects via the Satellite protocol, displays a live button grid with labels and colours from Companion, and supports multi-page layouts and a favourites system.

### Requirements

- Bitfocus Companion 4.x with Satellite API enabled (port 16622)
- Device connected to Wi-Fi

### Controls

**Main menu**

| Button | Action |
|---|---|
| UP / DOWN | Navigate menu |
| CENTER | Open selected item |
| BACK | Exit app |

**Remote view**

| Button | Action |
|---|---|
| Arrows | Navigate grid (L/R shifts column window, changes page at edge) |
| CENTER | Press selected button (hold 600 ms = long press) |
| F2 | Mark / unmark highlighted button as favourite |
| F3 | Open favourites view |
| A / Z | Previous / next page |
| 1–9 | Jump to page N |
| BACK | Return to menu |

**Favourites view**

| Button | Action |
|---|---|
| Arrows | Navigate |
| CENTER | Press button (hold 600 ms = long press) |
| F2 | Remove item from favourites |
| A | Move item up in list |
| Z | Move item down in list |
| BACK | Return to remote view |

**Setup wizard**

- **Step 1 — Network:** Enter the Companion IP and port. UP/DN switches fields; type digits/dots; BKSP deletes; CENTER saves and exits.
- **Step 2 — Browse:** Navigate the Companion button grid. Arrow keys move cursor; L/R shift column window; A/Z change page; CENTER toggles selection; BACK proceeds.
- **Step 3 — Reorder:** UP/DN navigate the list; A/Z move the selected item; BACK continues.
- **Step 4 — Confirm:** CENTER saves; BACK goes back.

Config is saved to `/sd/companion/config.json` on first setup.

---

## lcd_repair.py — LCD Repair

Runs full-screen color cycling and white-wash sequences to relieve LCD burn-in and image retention. Six modes are available ranging from a quick 10-minute flash to a thorough one-hour full-spectrum treatment. The status label rotates between screen corners every 4 color changes so it cannot itself cause new retention.

### Modes

| Mode | Duration | Description |
|---|---|---|
| RGB Cycle | 15 min | Cycles 8 colors (R/G/B/W/C/M/Y/K) to exercise all subpixels |
| White Wash | 30 min | Extended white hold followed by RGB cycling; effective for most image retention |
| Full Spectrum | 1 hr | Four phases: RGB cycle → white wash → fast B/W flash → hue gradient sweep |
| Fast Flip | 10 min | Rapid color cycling at maximum display speed |
| White/Black | 20 min | Alternates 3-second full white and full black holds |
| W/B Fast | 10 min | Rapid white/black alternation |

### Controls

**Menu**

| Button | Action |
|---|---|
| UP / DOWN | Navigate modes |
| CENTER | Start selected mode |
| BACK | Exit app |

**Running**

| Button | Action |
|---|---|
| BACK | Stop and return to menu |

The display shows elapsed time, total time, and completion percentage. Press BACK when the session ends to return to the menu.
