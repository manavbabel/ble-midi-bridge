# casio-ble-midi

Bridge a **Casio BLE-MIDI piano** (PX-S7000 + WU-BT10 dongle) to a regular
Windows MIDI port over Bluetooth. Replaces a USB MIDI cable: any DAW,
browser Web MIDI app, or virtual instrument sees the piano as a normal MIDI
device, both directions.

Tested on Windows 11 with the new [Windows MIDI Services][wms]. Won't work
on Windows 10 — it relies on WMS's native loopback transport.

[wms]: https://aka.ms/MIDI

---

## What's in here

| File | What it does |
| --- | --- |
| `casio_midi_bridge.py` | The main thing. Run it, get a Windows MIDI port called `Casio PX-S7000`. |
| `casio_ble_probe.py`   | Diagnostic + record/play utility. `scan`, `listen`, `poke`, `record`, `play`. |
| `midi_test_client.py`  | Sanity-checks the bridge end-to-end without needing a DAW. |

---

## Bridge: turn the piano into a MIDI port

```
uv run casio_midi_bridge.py
```

This scans for the WU-BT10, creates a Windows MIDI port called
`Casio PX-S7000`, and bridges it bidirectionally to the piano. Open that
port in any MIDI app. Press **Ctrl+C** to stop — the port is removed and
the BLE connection is closed.

Other modes:

```
uv run casio_midi_bridge.py --address 78:5E:A2:63:AE:7D    # skip BLE scan
uv run casio_midi_bridge.py --port "My Piano"              # custom port name
uv run casio_midi_bridge.py --cleanup                       # remove leftover port from a crash
CASIO_BRIDGE_DEBUG=1 uv run casio_midi_bridge.py            # log every MIDI message in/out
```

When in your DAW or app, **use the port literally named `Casio PX-S7000`** —
not the one ending in `[do not use - internal]`. The "internal" one is the
script's own end of the loopback; opening it as an app does nothing.

## Record and play back

`casio_ble_probe.py` has `record` and `play` subcommands (independent of
the bridge — they talk to BLE directly):

```
uv run casio_ble_probe.py scan
uv run casio_ble_probe.py record --address 78:5E:A2:63:AE:7D --duration 30 --out song.jsonl
uv run casio_ble_probe.py play   --address 78:5E:A2:63:AE:7D --in  song.jsonl
```

The recording is JSON Lines: one line per MIDI message with a host-relative
timestamp. Replay preserves original timing.

## Sanity test

If the bridge isn't working, run this in a second terminal *while the bridge
is running*:

```
uv run midi_test_client.py
```

It listens on the public port for 5 seconds (play notes during that window),
then sends a middle-C NoteOn/Off so you should hear the piano. If this works
but your real app doesn't, the bridge is fine and the issue is in your app.

---

## Troubleshooting

- **`midi` command not found.** Windows MIDI Services isn't installed.
  Get it from [aka.ms/MIDI][wms]. The bridge needs the `midi` CLI on PATH.

- **`python-rtmidi` import crashes silently (exit 127).** Python 3.13 is
  broken with `python-rtmidi` 1.5.x on Windows. Use Python 3.12. Pin it in
  `.python-version`.

- **Bridge connects to the piano but no MIDI flows in either direction.**
  Almost always: the app is connected to `Casio PX-S7000 [do not use - internal]`
  instead of `Casio PX-S7000`. Switch ports.

- **Web MIDI app doesn't see the port.** Some browser-based MIDI apps don't
  request `software: true` access. Try a different web app, or use a native
  app (Reaper, Ableton, MIDI-OX) to confirm the port itself is fine.

- **Bridge says "No WU-BT10 found."** Make sure: the dongle is plugged into
  the piano, the piano is on, and **nothing else is currently connected to it**
  (Casio's Music Space app on iPad will hold the BLE link). Close other clients,
  then retry.

- **Crash left an orphan MIDI port behind.** Run `uv run casio_midi_bridge.py
  --cleanup`. If that doesn't clear it, the bridge has lost track of the
  association id; the cleanest fix is `Restart-Service midisrv -Force` from
  an admin PowerShell, or just reboot. Orphans are harmless until then.

- **loopMIDI / TeVirtualMIDI virtual ports don't show up.** Don't use them
  on Windows 11 — they bypass WMS and silently fail to register. The bridge
  uses WMS's native loopback transport instead, which works.

---

## How it works

### BLE side

The WU-BT10 dongle exposes **two GATT services**:

- The standard Bluetooth MIDI service (`03b80e5a-ede8-4b33-a751-6ce34ec4c700`)
  — the one defined by the MMA / Apple spec. Carries normal MIDI 1.0 byte
  streams in standard BLE-MIDI packets. **This is what the bridge uses.**
- A proprietary Casio "iroha BLE" service (`5052494d-2dab-0341-...`,
  ASCII-decodes to `PRIM-…iroha BLE`). Used by Casio's Music Space app for
  app-specific features, not for general MIDI. The bridge ignores it.

The piano sends each NoteOn preceded by `CC 88` (high-resolution velocity
prefix) — that's standard MIDI 1.0 for the Smooth Sound Engine, not a quirk.

### MIDI port side

Windows MIDI Services has a built-in **virtual loopback transport**: ask it
to create a pair of named endpoints A and B, and anything written to A comes
out at B (and vice versa). The bridge:

1. Creates a loopback pair with `midi loopback create`.
2. Names the public side `Casio PX-S7000` (what apps connect to).
3. Names the internal side `Casio PX-S7000 [do not use - internal]` (what
   the script itself connects to).
4. Forwards BLE notifications → internal port (apps see them as input).
5. Forwards messages from the internal port → BLE writes (apps' output).

Apps see only one device that does both directions, just like USB.

### Cleanup

The association id of the loopback is written to a sidecar file in `%TEMP%`.
On startup, if the sidecar is present from a prior crash, the bridge tries
to remove that loopback before creating a new one. On clean Ctrl+C, the
loopback is removed via `try/finally` and the sidecar is deleted.

If the script is killed without running `finally` (SIGKILL, power loss),
the orphan loopback persists until midisrv restarts or the PC reboots.
The `--cleanup` flag handles this when the sidecar is intact; otherwise
restart `midisrv`.
