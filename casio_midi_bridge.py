# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22", "python-rtmidi>=1.5"]
# ///
"""
casio_midi_bridge.py — Bridge a Casio BLE-MIDI piano to a Windows MIDI port.

  uv run casio_midi_bridge.py                  # scan, connect, bridge
  uv run casio_midi_bridge.py --address ADDR   # skip scan
  uv run casio_midi_bridge.py --port "Name"    # custom MIDI port name
  uv run casio_midi_bridge.py --cleanup        # remove leftover loopback only

What it does: scans for the WU-BT10, creates a Windows MIDI port, and
bridges it bidirectionally to the piano. Any Windows MIDI app sees the
port as a normal MIDI device. Ctrl+C tears everything down.

Requires Windows MIDI Services (https://aka.ms/MIDI). The 'midi' CLI
must be on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
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

STD_MIDI_CHAR = "7772e5db-3868-4112-a1a9-f2669d106bf3"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Sidecar file holding the association id of the loopback we created, so
# a fresh run after a crash can remove the orphan it left behind.
SIDECAR = Path(os.environ.get("TEMP") or ".") / "casio_midi_bridge_assoc.txt"

DEBUG = bool(os.environ.get("CASIO_BRIDGE_DEBUG"))


def dbg(*a, **kw):
    if DEBUG:
        print(*a, **kw, flush=True)


# ---------------------------------------------------------------------------
# BLE-MIDI framing
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
# WMS loopback (lifecycle managed via sidecar file)
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
    # twice — one per endpoint), then the actual Association Id last.
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

async def discover_piano(timeout: float) -> str:
    print(f"Scanning for WU-BT10 ({timeout:.0f}s)…")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hits = []
    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        if "WU-BT10" in name:
            hits.append((addr, name, adv.rssi))
    if not hits:
        raise SystemExit(
            "No WU-BT10 found. Make sure the piano is on, the dongle is\n"
            "plugged in, and nothing else is currently connected to it."
        )
    if len(hits) > 1:
        print("Multiple WU-BT10 devices found. Re-run with --address to pick one:")
        for addr, name, rssi in hits:
            print(f"  --address {addr}  ({name!r}, rssi={rssi})")
        raise SystemExit(2)
    addr, name, rssi = hits[0]
    print(f"  found {name!r} at {addr} (rssi={rssi})")
    return addr


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
        self.bridge_port_name = f"{port_name} [do not use - internal]"
        self.assoc_id: str | None = None
        self.midi_in = rtmidi.MidiIn()
        self.midi_out = rtmidi.MidiOut()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.send_queue: asyncio.Queue[bytes] | None = None
        self.client: BleakClient | None = None
        self.write_response = False
        self.shutdown = asyncio.Event()

    def _open_midi(self) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            in_idx = find_port(self.midi_in, self.bridge_port_name)
            out_idx = find_port(self.midi_out, self.bridge_port_name)
            if in_idx is not None and out_idx is not None:
                self.midi_in.open_port(in_idx)
                self.midi_out.open_port(out_idx)
                self.midi_in.ignore_types(sysex=False, timing=False, active_sense=False)
                self.midi_in.set_callback(self._on_midi_in)
                return
            time.sleep(0.2)
        raise SystemExit("Loopback port did not appear in WinMM after 5s.")

    # rtmidi callback (rtmidi worker thread)
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
                        STD_MIDI_CHAR,
                        make_ble_midi_packet(midi),
                        response=self.write_response,
                    )
                except Exception as e:
                    print(f"  write_gatt_char failed: {e}", flush=True)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.send_queue = asyncio.Queue(maxsize=512)
        # Make Ctrl+C wake any await rather than just bubbling up at the
        # next sync point. Falls back gracefully on Windows ProactorEventLoop
        # (which doesn't support add_signal_handler).
        with suppress(NotImplementedError):
            self.loop.add_signal_handler(signal.SIGINT, self.shutdown.set)

        # Clean up any orphan loopback from a prior crashed run.
        if cleanup_sidecar_loopback():
            print("Cleared orphan loopback from a prior run.")

        print("Setting up Windows MIDI port…")
        self.assoc_id = loopback_create(self.bridge_port_name, self.app_port_name)
        SIDECAR.write_text(self.assoc_id)
        print(f"  loopback created (id {self.assoc_id})")

        try:
            self._open_midi()
            sender = asyncio.create_task(self._sender())
            try:
                while not self.shutdown.is_set():
                    print(f"\nConnecting to piano {self.address}…")
                    try:
                        async with BleakClient(self.address) as client:
                            self.client = client
                            print(f"  connected: {client.is_connected}")
                            wc = client.services.get_characteristic(STD_MIDI_CHAR)
                            if wc is None:
                                print("  ERROR: standard BLE-MIDI characteristic missing.")
                                return
                            self.write_response = "write-without-response" not in wc.properties
                            await client.start_notify(STD_MIDI_CHAR, self._on_ble_notify)
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
                    print("Disconnected. Reconnecting in 3s…")
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
            print("Removing MIDI port…")
            if self.assoc_id and loopback_remove(self.assoc_id):
                SIDECAR.unlink(missing_ok=True)
            print("Stopped.")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--address", help="Skip scan; connect directly to this BD_ADDR")
    p.add_argument("--port", default="Casio PX-S7000",
                   help='Public MIDI port name your apps will see (default: "Casio PX-S7000")')
    p.add_argument("--scan-timeout", type=float, default=8.0,
                   help="Seconds to scan for the piano (default: 8)")
    p.add_argument("--cleanup", action="store_true",
                   help="Remove a leftover loopback (from sidecar) and exit. "
                        "Use this if a previous run crashed and left a port behind.")
    args = p.parse_args()

    if args.cleanup:
        if cleanup_sidecar_loopback():
            print("Removed leftover loopback.")
        else:
            print("No sidecar found, or its loopback was already gone.")
            print("If a port still lingers, restart the midi service from an")
            print("admin PowerShell:  Restart-Service midisrv -Force")
        return 0

    address = args.address
    if not address:
        try:
            address = asyncio.run(discover_piano(args.scan_timeout))
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 130

    bridge = Bridge(address, args.port)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        # Cleanup runs in bridge.run()'s finally block; nothing more to do.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
