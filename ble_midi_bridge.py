# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22", "python-rtmidi>=1.5"]
# ///
"""
ble_midi_bridge.py - Bridge a BLE-MIDI device to a virtual MIDI port.

  uv run ble_midi_bridge.py scan       # find a device, save config
  uv run ble_midi_bridge.py            # run the bridge using saved config
  uv run ble_midi_bridge.py cleanup    # (Windows) remove orphan loopback
  uv run ble_midi_bridge.py config     # show / reset saved config

BLE-MIDI is a public standard (Apple + MMA, 2015). On macOS and Linux,
virtual MIDI ports are created directly via CoreMIDI / ALSA. On Windows,
the bridge uses Windows MIDI Services (https://aka.ms/MIDI); the 'midi'
CLI must be on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import rtmidi
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

# Standard BLE-MIDI service & characteristic UUIDs.
# https://www.midi.org/specifications/midi-transports-specifications/bluetooth-le-midi
BLE_MIDI_SERVICE_UUID = "03b80e5a-ede8-4b33-a7e1-5bbe7e5e1cb6"
BLE_MIDI_CHAR_UUID = "7772e5db-3868-4112-a1a9-f2669d106bf3"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

DEBUG = bool(os.environ.get("BLE_MIDI_BRIDGE_DEBUG"))
IS_WINDOWS = sys.platform == "win32"

APP_NAME = "ble-midi-bridge"


def dbg(*a, **kw):
    if DEBUG:
        print(*a, **kw, flush=True)


# ---------------------------------------------------------------------------
# Config & sidecar paths
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


CONFIG_PATH = _config_dir() / "config.json"
# Sidecar (Windows only): the loopback association id of a live run, so that
# a crashed-then-restarted process can clean up its orphan.
SIDECAR = Path(os.environ.get("TEMP") or ".") / "ble_midi_bridge_assoc.txt"


def load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"Config at {CONFIG_PATH} is unreadable ({e}); ignoring.")
        return None


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# BLE-MIDI framing (public spec)
# ---------------------------------------------------------------------------

def make_ble_midi_packet(midi: bytes) -> bytes:
    ts = int(time.monotonic() * 1000) & 0x1FFF
    return bytes([0x80 | ((ts >> 7) & 0x3F), 0x80 | (ts & 0x7F)]) + midi


def parse_ble_midi(packet: bytes):
    """Yield raw MIDI message bytes from a standard BLE-MIDI packet."""
    if not packet or not (packet[0] & 0x80):
        return
    i = 1
    last_status: int | None = None
    while i < len(packet):
        b = packet[i]
        if b & 0x80:
            i += 1
            if i >= len(packet):
                return
            status = packet[i]
            if not (status & 0x80):
                return
            last_status = status
            i += 1
        else:
            if last_status is None:
                return
            status = last_status
        hi = status & 0xF0
        if status >= 0xF0:
            yield bytes([status]) + packet[i:]
            return
        n_data = 1 if hi in (0xC0, 0xD0) else 2
        if i + n_data > len(packet):
            return
        yield bytes([status]) + packet[i:i + n_data]
        i += n_data


# ---------------------------------------------------------------------------
# Windows MIDI Services loopback (lifecycle managed via sidecar file)
# ---------------------------------------------------------------------------

def _midi_cli() -> str:
    cli = shutil.which("midi")
    if cli is None:
        raise SystemExit(
            "The Windows MIDI Services 'midi' CLI is not on PATH.\n"
            "Install Windows MIDI Services from https://aka.ms/MIDI."
        )
    return cli


def _run_midi(*args) -> tuple[int, str]:
    cp = subprocess.run(
        [_midi_cli(), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return cp.returncode, ANSI_RE.sub("", (cp.stdout or "") + (cp.stderr or ""))


def loopback_create(name_a: str, name_b: str) -> str:
    rc, out = _run_midi("loopback", "create", "--name-a", name_a, "--name-b", name_b)
    if rc != 0:
        raise SystemExit(f"midi loopback create failed:\n{out}")
    guids = GUID_RE.findall(out)
    if not guids:
        raise SystemExit(f"Could not parse association id from output:\n{out}")
    # Output prints endpoint device-path GUIDs first (the same interface GUID
    # twice, one per endpoint), then the actual Association Id last.
    return guids[-1].lower()


def loopback_remove(assoc_id: str) -> bool:
    rc, out = _run_midi("loopback", "remove", "--association-id", assoc_id)
    return "removed" in out.lower()


def cleanup_sidecar_loopback() -> bool:
    """If a sidecar file exists, try to remove its loopback. Returns True iff
    something was actually removed."""
    if not SIDECAR.exists():
        return False
    aid = SIDECAR.read_text().strip()
    SIDECAR.unlink(missing_ok=True)
    return loopback_remove(aid) if aid else False


# ---------------------------------------------------------------------------
# BLE discovery
# ---------------------------------------------------------------------------

async def scan_devices(timeout: float) -> list[dict]:
    """Scan for nearby BLE devices. Returns dicts sorted by:
    advertises BLE-MIDI service first, then RSSI strength."""
    print(f"Scanning for BLE devices ({timeout:.0f}s)...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        service_uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_midi = BLE_MIDI_SERVICE_UUID in service_uuids
        if not name and not is_midi:
            continue  # skip unnamed non-MIDI noise
        devices.append({
            "address": addr,
            "name": name or "(unnamed)",
            "rssi": adv.rssi if adv.rssi is not None else -999,
            "is_midi": is_midi,
        })
    devices.sort(key=lambda d: (not d["is_midi"], -d["rssi"]))
    return devices


def prompt_pick(devices: list[dict]) -> dict | None:
    if not devices:
        print("No BLE devices found. Make sure your piano is on, the BLE-MIDI")
        print("dongle is plugged in, and nothing else is currently connected.")
        return None
    print()
    print(f"  {'#':>2}  {'address':<18}  {'rssi':>5}  {'midi?':<5}  name")
    for i, d in enumerate(devices, 1):
        midi_marker = " yes " if d["is_midi"] else "  -  "
        print(f"  {i:>2}  {d['address']:<18}  {d['rssi']:>5}  {midi_marker}  {d['name']}")
    print()
    while True:
        ans = input(f"Pick a device [1-{len(devices)}], or q to quit: ").strip()
        if ans.lower() in ("q", "quit", ""):
            return None
        try:
            idx = int(ans)
        except ValueError:
            continue
        if 1 <= idx <= len(devices):
            return devices[idx - 1]


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

def find_port(io, name: str) -> int | None:
    for i, n in enumerate(io.get_ports()):
        if name in n:
            return i
    return None


class Bridge:
    def __init__(self, address: str, port_name: str):
        self.address = address
        self.app_port_name = port_name
        # Windows creates a *loopback* (name-pair). The bridge connects to one
        # side; user apps see the other side. macOS/Linux create a single
        # virtual port directly, no loopback dance.
        self.bridge_port_name = f"{port_name} [do not use - internal]"
        self.assoc_id: str | None = None
        self.midi_in = rtmidi.MidiIn()
        self.midi_out = rtmidi.MidiOut()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.send_queue: asyncio.Queue[bytes] | None = None
        self.client: BleakClient | None = None
        self.write_response = False
        self.shutdown = asyncio.Event()

    def _open_midi_windows(self) -> None:
        # The WMS loopback is created in run() before this is called.
        # Wait for the WinMM-side ports to appear.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            in_idx = find_port(self.midi_in, self.bridge_port_name)
            out_idx = find_port(self.midi_out, self.bridge_port_name)
            if in_idx is not None and out_idx is not None:
                self.midi_in.open_port(in_idx)
                self.midi_out.open_port(out_idx)
                self._configure_midi_in()
                return
            time.sleep(0.2)
        raise SystemExit("Loopback port did not appear in WinMM after 5s.")

    def _open_midi_unix(self) -> None:
        # CoreMIDI / ALSA support virtual ports natively. Apps see the same
        # name as both a source (to read from) and a destination (to write
        # to), like the macOS IAC bus.
        self.midi_out.open_virtual_port(self.app_port_name)
        self.midi_in.open_virtual_port(self.app_port_name)
        self._configure_midi_in()

    def _configure_midi_in(self) -> None:
        self.midi_in.ignore_types(sysex=False, timing=False, active_sense=False)
        self.midi_in.set_callback(self._on_midi_in)

    def _on_midi_in(self, event, _data=None):
        message, _delta = event
        midi = bytes(message)
        dbg(f"[app->bridge]   {midi.hex(' ')}")
        if self.loop and self.send_queue:
            self.loop.call_soon_threadsafe(self._enqueue, midi)

    def _enqueue(self, midi: bytes) -> None:
        if self.send_queue is None:
            return
        try:
            self.send_queue.put_nowait(midi)
        except asyncio.QueueFull:
            pass

    def _on_ble_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        dbg(f"[piano->bridge] raw={bytes(data).hex(' ')}")
        for msg in parse_ble_midi(bytes(data)):
            dbg(f"[bridge->app]   {msg.hex(' ')}")
            try:
                self.midi_out.send_message(list(msg))
            except Exception as e:
                print(f"  midi_out.send_message failed: {e}", flush=True)

    async def _sender(self) -> None:
        assert self.send_queue is not None
        while True:
            midi = await self.send_queue.get()
            if self.client and self.client.is_connected:
                dbg(f"[bridge->piano] {midi.hex(' ')}")
                try:
                    await self.client.write_gatt_char(
                        BLE_MIDI_CHAR_UUID,
                        make_ble_midi_packet(midi),
                        response=self.write_response,
                    )
                except Exception as e:
                    print(f"  write_gatt_char failed: {e}", flush=True)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.send_queue = asyncio.Queue(maxsize=512)
        # Make Ctrl+C wake any await rather than just bubbling at the next
        # sync point. Falls back gracefully on Windows ProactorEventLoop.
        with suppress(NotImplementedError):
            self.loop.add_signal_handler(signal.SIGINT, self.shutdown.set)

        if IS_WINDOWS:
            if cleanup_sidecar_loopback():
                print("Cleared orphan loopback from a prior run.")
            print("Setting up Windows MIDI port...")
            self.assoc_id = loopback_create(self.bridge_port_name, self.app_port_name)
            SIDECAR.write_text(self.assoc_id)
            print(f"  loopback created (id {self.assoc_id})")
        else:
            print("Setting up virtual MIDI port...")

        try:
            if IS_WINDOWS:
                self._open_midi_windows()
            else:
                self._open_midi_unix()
            sender = asyncio.create_task(self._sender())
            try:
                while not self.shutdown.is_set():
                    print(f"\nConnecting to piano {self.address}...")
                    try:
                        async with BleakClient(self.address) as client:
                            self.client = client
                            print(f"  connected: {client.is_connected}")
                            wc = client.services.get_characteristic(BLE_MIDI_CHAR_UUID)
                            if wc is None:
                                print("  ERROR: standard BLE-MIDI characteristic missing.")
                                return
                            self.write_response = "write-without-response" not in wc.properties
                            await client.start_notify(BLE_MIDI_CHAR_UUID, self._on_ble_notify)
                            print(f"\nBridge running. In your DAW / app, open the MIDI "
                                  f"port {self.app_port_name!r}.\nCtrl+C to stop.")
                            while client.is_connected and not self.shutdown.is_set():
                                with suppress(asyncio.TimeoutError):
                                    await asyncio.wait_for(self.shutdown.wait(), timeout=1.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"  connection error: {e}")
                    finally:
                        self.client = None
                    if self.shutdown.is_set():
                        break
                    print("Disconnected. Reconnecting in 3s...")
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.shutdown.wait(), timeout=3.0)
            finally:
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender
                with suppress(Exception):
                    self.midi_in.close_port()
                with suppress(Exception):
                    self.midi_out.close_port()
        finally:
            if IS_WINDOWS:
                print("Removing MIDI port...")
                if self.assoc_id and loopback_remove(self.assoc_id):
                    SIDECAR.unlink(missing_ok=True)
            print("Stopped.")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_scan(args) -> int:
    try:
        devices = asyncio.run(scan_devices(args.timeout))
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    chosen = prompt_pick(devices)
    if not chosen:
        return 1
    default_port = chosen["name"] if chosen["name"] != "(unnamed)" else "BLE MIDI"
    try:
        port_name = input(
            f"MIDI port name your apps will see [{default_port}]: "
        ).strip() or default_port
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130
    cfg = {
        "address": chosen["address"],
        "device_name": chosen["name"],
        "port_name": port_name,
    }
    save_config(cfg)
    print(f"Saved config to {CONFIG_PATH}")
    print("Done. Re-run the bridge command (without 'scan') to start.")
    return 0


def cmd_run(args) -> int:
    address = args.address
    port_name = args.port_name
    if not address or not port_name:
        cfg = load_config()
        if cfg:
            address = address or cfg.get("address")
            port_name = port_name or cfg.get("port_name")
    if not address:
        print(f"No --address given and no saved config at {CONFIG_PATH}.")
        print("Run with the 'scan' subcommand first to set up.")
        return 2
    port_name = port_name or "BLE MIDI"

    bridge = Bridge(address, port_name)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        # Cleanup runs in bridge.run()'s finally block.
        pass
    return 0


def cmd_cleanup(args) -> int:
    if not IS_WINDOWS:
        print("'cleanup' only applies on Windows. Other platforms tear down")
        print("their virtual MIDI port automatically when the process exits.")
        return 0
    if cleanup_sidecar_loopback():
        print("Removed leftover loopback.")
    else:
        print("No sidecar found, or its loopback was already gone.")
        print("If a port still lingers, restart the midi service from an")
        print("admin PowerShell:  Restart-Service midisrv -Force")
    return 0


def cmd_config(args) -> int:
    if args.reset:
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
            print(f"Deleted {CONFIG_PATH}")
        else:
            print("No saved config.")
        return 0
    cfg = load_config()
    if cfg is None:
        print(f"No saved config at {CONFIG_PATH}.")
        return 1
    print(f"Config at {CONFIG_PATH}:")
    print(json.dumps(cfg, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        prog="ble_midi_bridge.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="Discover a device and save config")
    p_scan.add_argument("--timeout", type=float, default=8.0,
                        help="Seconds to scan (default: 8)")
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser("run", help="Start the bridge (default if no command)")
    p_run.add_argument("--address", default=None,
                       help="BD_ADDR (overrides saved config)")
    p_run.add_argument("--port-name", dest="port_name", default=None,
                       help="MIDI port name (overrides saved config)")
    p_run.set_defaults(func=cmd_run)

    p_cleanup = sub.add_parser("cleanup",
                               help="(Windows) remove a leftover loopback")
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_config = sub.add_parser("config", help="Show or reset saved config")
    p_config.add_argument("--reset", action="store_true",
                          help="Delete the config file")
    p_config.set_defaults(func=cmd_config)

    # No subcommand defaults to 'run' with no overrides.
    p.set_defaults(func=cmd_run, address=None, port_name=None, reset=False)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
