# ble-midi-bridge

This is a single-file Python script that bridges any **Bluetooth LE MIDI** device
(Casio WU-BT10 dongle, Yamaha MD-BT01, Roland WM-1, etc.) to a virtual
MIDI port on your computer. Once configured, your DAW or any MIDI app
sees the device as a normal MIDI device, in both directions.

Cross-platform: this should works on **macOS**, **Linux**, and **Windows 11**, but I made it on Windows and haven't tested it on the other two.

This was inspired by [Perfect Bluetooth MIDI](https://mayerwin.github.io/Perfect-Bluetooth-MIDI-For-Windows/), which was a great concept but didn't work with my Casio piano.

This project was built with the help of Claude. 

## Quickstart

You need the [uv](https://docs.astral.sh/uv/) package manager.

On **Windows** you also need [Windows MIDI Services][wms], because
Windows can't create virtual MIDI ports natively. The `midi` CLI from
WMS must be on PATH. Note that most modern Windows 11 installations will have this automatically

There's nothing to install or download — `uv` runs the script straight
from GitHub and caches it (and its dependencies) so subsequent runs
are fast:

```
# 1. First time only - discover your device and save the config:
uv run https://raw.githubusercontent.com/manavbabel/ble-midi-bridge/main/ble_midi_bridge.py scan

# 2. From now on, one command starts the bridge:
uv run https://raw.githubusercontent.com/manavbabel/ble-midi-bridge/main/ble_midi_bridge.py
```

The config is saved in your user directory (see [Commands](#commands)
below), so the same config works whether you run from URL or from a
local copy. If you'd rather have a local file, just download
`ble_midi_bridge.py` and use that path instead — everything else is
the same.

While the bridge is running, open your DAW and select the MIDI port
named in step 1. Press **Ctrl+C** to stop; the virtual port is removed
and the BLE link is closed.

[wms]: https://aka.ms/MIDI

## Commands

| Command | What it does |
| --- | --- |
| `scan [--timeout N]` | Find nearby BLE devices, pick one, save config |
| (no command) or `run` | Bridge the saved device to a virtual MIDI port |
| `run --address ADDR --port-name NAME` | One-shot bridge without saving config |
| `cleanup` | (Windows) remove an orphaned virtual port from a prior crash |
| `config` | Show the saved config |
| `config --reset` | Delete the saved config |

The config lives at:

- **Windows**: `%APPDATA%\ble-midi-bridge\config.json`
- **macOS**: `~/Library/Application Support/ble-midi-bridge/config.json`
- **Linux**: `${XDG_CONFIG_HOME:-~/.config}/ble-midi-bridge/config.json`

## Platform notes

**macOS / Linux:** virtual MIDI ports are created directly via CoreMIDI
or ALSA. Nothing to install beyond `uv`. Your app will see the port
named in `scan` as both a source and a destination, like the macOS IAC
bus.

**Windows:** install [Windows MIDI Services][wms] if not installed already. The bridge uses WMS's
loopback feature to expose the virtual port. If a previous run crashed
and left an orphan port, run `cleanup` (or, worst case,
`Restart-Service midisrv -Force` from an admin PowerShell). On
Windows 11, the public port is the one named what you chose during
`scan`; **don't** open the one ending in `[do not use - internal]` from
your DAW — that's the bridge's own end of the loopback.

## How it works (and is it legal?)

Yes, it's legal. **BLE-MIDI is a public standard** published by Apple
and the MIDI Manufacturers Association in 2015 — no vendor-specific or
reverse-engineered code. The script implements that standard exactly as
documented. See the
[official spec](https://www.midi.org/specifications/midi-transports-specifications/bluetooth-le-midi).

Internally:

- `bleak` connects to the BLE-MIDI service (`03b80e5a-…`) and
  subscribes to notifications on the standard BLE-MIDI characteristic
  (`7772e5db-…`).
- Inbound BLE packets are unframed (timestamp byte, running status) and
  emitted to the virtual MIDI port via `python-rtmidi`.
- Outbound MIDI from your DAW is wrapped in a BLE-MIDI packet and
  written to the same characteristic.

## Troubleshooting

- **`midi` command not found (Windows).** Windows MIDI Services isn't
  installed. Get it from [aka.ms/MIDI][wms].
- **`python-rtmidi` import crashes silently (Windows).** Python 3.13
  has issues with `python-rtmidi` 1.5.x on Windows. Use Python 3.12 —
  this repo pins it via `.python-version`.
- **Bridge connects but no MIDI flows in either direction (Windows).**
  Almost always: the app is connected to the `[do not use - internal]`
  port instead of the public one. Switch ports.
- **`scan` doesn't show your device.** Make sure: the device is
  powered on, advertising (some pianos need a button press), and
  **nothing else is currently connected to it** (a phone or tablet's
  MIDI app will hold the BLE link). Disconnect other clients, then
  retry. Try `scan --timeout 16` for a longer window.
- **Crash left an orphan MIDI port behind (Windows).** Run
  `uv run ble_midi_bridge.py cleanup`. If that doesn't clear it, the
  process lost track of the association id;
  `Restart-Service midisrv -Force` from admin PowerShell will reset
  WMS. Orphans are harmless until then.

## Debugging

Set `BLE_MIDI_BRIDGE_DEBUG=1` to log every BLE and MIDI byte in both
directions:

```
# bash / zsh
BLE_MIDI_BRIDGE_DEBUG=1 uv run https://raw.githubusercontent.com/manavbabel/ble-midi-bridge/main/ble_midi_bridge.py

# PowerShell
$env:BLE_MIDI_BRIDGE_DEBUG=1; uv run https://raw.githubusercontent.com/manavbabel/ble-midi-bridge/main/ble_midi_bridge.py
```

## License

MIT — see [LICENSE](LICENSE).
