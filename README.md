# PicoWare Tools

A collection of MicroPython apps for the [PicoCalc](https://github.com/clockworkpi/picoware) handheld running [Picoware](https://github.com/clockworkpi/picoware).

## Apps

| File | Description |
|---|---|
| `wifi_manager.py` | Manage saved WiFi networks on the device |
| `ftpserver.py` | FTP server — push/pull files over Wi-Fi from your desktop |
| `companion.py` | Wireless remote control for [Bitfocus Companion](https://bitfocus.io/companion) |

## Installation

Copy the app file(s) you want to `/picoware/apps/` on the device SD card. The Picoware launcher picks them up automatically. The easiest way to transfer files is FTP (use `ftpserver.py` itself to bootstrap):

```sh
curl -T <app>.py ftp://<device-ip>/picoware/apps/<app>.py --user x:x
```

---

## wifi_manager.py — WiFi Manager

Manage saved WiFi networks directly on the device. Switch between networks, import the currently active network, or delete saved entries.

### Controls

| Button | Action |
|---|---|
| UP / DOWN | Navigate saved networks list |
| CENTER | Activate selected network |
| A | Import current active network into saved list |
| Z | Delete selected entry |
| BACK | Exit app |

### WiFi config files

| Path | Format | Purpose |
|---|---|---|
| `/sd/picoware/wifi/ssid.json` | `{"ssid": "..."}` | Active SSID (read/written by OS) |
| `/sd/picoware/wifi/password.json` | `{"password": "..."}` | Active password (read/written by OS) |
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

Turns the PicoCalc into a wireless remote control for [Bitfocus Companion](https://bitfocus.io/companion). Connects via the Satellite protocol, displays a live button grid with labels and colours from Companion, and supports multi-page layouts.

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
| Arrow keys | Move cursor |
| CENTER | Press selected button |
| BACK | Return to menu |

**Setup wizard**

- **Step 1 — Network:** Enter the Companion IP and port. UP/DN switches fields; type digits/dots; BKSP deletes; CENTER connects.
- **Step 2 — Browse:** Navigate the Companion button grid. Arrow keys move cursor; L/R shift column window; A/Z change page; CENTER toggles selection; BACK proceeds.
- **Step 3 — Reorder:** UP/DN navigate the list; A/Z move the selected item; BACK continues.
- **Step 4 — Confirm:** CENTER saves; BACK goes back.

Config is saved to `/sd/companion/config.json` on first setup.
