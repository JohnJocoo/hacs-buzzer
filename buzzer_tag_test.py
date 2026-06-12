#!/usr/bin/env python3
"""Bleak test client for the "Buzzer Tag" BLE peripheral.

NO-ENCRYPTION build: the buzz characteristic is a PLAIN write - there is no
pairing, bonding or encryption. Just connect and write.

Device contract:
  - Advertised name : "Buzzer Tag"
  - Service UUID    : 12340000-1234-5678-1234-56789abcdef0
  - Buzz char UUID  : 12340001-1234-5678-1234-56789abcdef0 (Write / WriteNoResp)
  - Status char UUID: 12340002-1234-5678-1234-56789abcdef0 (Read / Notify,
                      2 bytes [buzz_state, battery_percent])
  - Write payload   : exactly 1 byte. 0x01 -> buzz_on (melody loops up to 2 min),
                      0x00 -> buzz_off, 0x02 -> status-only. Other values are
                      rejected (ATT 0x13).

Connection model:
  - The device is meant to be HELD CONNECTED: once connected it stays connected
    and requests a low-power link, so commands land within ~2 s (macOS caps the
    interval at ~2 s; BlueZ/HA may grant slower). Keep the connection open and
    write on demand. If it drops it advertises ~10 s, then ~10 s every ~5 min
    (recovery), so reconnect with retries.

Subcommands:
    scan                     List nearby Buzzer Tags (by name or service UUID).
    buzz-on                  Connect (with retries), write 0x01, disconnect.
    buzz-off                 Connect (with retries), write 0x00, disconnect.
    demo     [--duration N]  buzz_on, wait N s, then buzz_off (held connection).
    test                     Interactive: send commands; reconnects as needed.

Each command accepts --address to target a specific device.

Requires:  pip install "bleak>=0.21"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

# --- Device contract constants ------------------------------------------------
DEVICE_NAME = "Buzzer Tag"
SERVICE_UUID = "12340000-1234-5678-1234-56789abcdef0"
BUZZ_CHAR_UUID = "12340001-1234-5678-1234-56789abcdef0"
STATUS_CHAR_UUID = "12340002-1234-5678-1234-56789abcdef0"
BUZZ_ON = bytes([0x01])
BUZZ_OFF = bytes([0x00])
STATUS_CMD = bytes([0x02])

DEFAULT_SCAN_TIMEOUT = 10.0
DEFAULT_CONNECT_TIMEOUT = 20.0
# The tag advertises ~10 s out of every ~70 s, so retry connecting across at
# least one full cycle before giving up.
DEFAULT_RETRY_TOTAL = 80.0


def _matches(device: BLEDevice, adv: AdvertisementData) -> bool:
    """True if an advertisement looks like a Buzzer Tag."""
    if device.name == DEVICE_NAME or adv.local_name == DEVICE_NAME:
        return True
    return SERVICE_UUID.lower() in {u.lower() for u in (adv.service_uuids or [])}


async def find_device(timeout: float) -> BLEDevice | None:
    """Scan for the first device matching the Buzzer Tag name or service UUID."""
    return await BleakScanner.find_device_by_filter(_matches, timeout=timeout)


async def scan(timeout: float) -> None:
    """List every matching Buzzer Tag seen during the scan window."""
    print(f"Scanning {timeout:.0f}s for '{DEVICE_NAME}' / service {SERVICE_UUID} ...")
    found: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def _cb(device: BLEDevice, adv: AdvertisementData) -> None:
        if _matches(device, adv):
            found[device.address] = (device, adv)

    scanner = BleakScanner(detection_callback=_cb, service_uuids=[SERVICE_UUID])
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    if not found:
        print("No Buzzer Tag found. It only advertises ~10s out of ~70s - "
              "try again, or press the board button to wake it.")
        return

    print(f"\nFound {len(found)} Buzzer Tag(s):")
    for address, (device, adv) in found.items():
        name = device.name or adv.local_name or "(unknown)"
        print(f"  {address}  {adv.rssi:>4} dBm  {name}")


async def _find(address: str | None, scan_timeout: float) -> BLEDevice | None:
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=scan_timeout)
    return await find_device(scan_timeout)


async def connect_with_retry(
    address: str | None,
    scan_timeout: float,
    connect_timeout: float,
    total: float = DEFAULT_RETRY_TOTAL,
) -> BleakClient:
    """Scan + connect, retrying until the tag shows up in a wake window.

    If the link dropped the device is only advertising in ~10 s windows (after a
    drop, or every ~5 min in recovery), so a single attempt may land in a gap.
    We keep scanning/connecting until ``total`` seconds.
    """
    target = address or f"'{DEVICE_NAME}'"
    deadline = time.monotonic() + total
    attempt = 0
    while True:
        attempt += 1
        device = await _find(address, scan_timeout)
        if device is not None:
            try:
                client = BleakClient(device, timeout=connect_timeout)
                await client.connect()
                print(f"Connected to {device.address}")
                return client
            except Exception as exc:  # noqa: BLE001 - transient; retry
                print(f"Connect attempt {attempt} failed ({exc}); retrying ...",
                      flush=True)
        else:
            print(f"{target} not visible (attempt {attempt}); the device only "
                  "advertises after a drop - retrying ...", flush=True)

        if time.monotonic() >= deadline:
            raise BleakError(
                f"Could not connect to {target} within {total:.0f}s. If it dropped, "
                "the device only advertises in ~10s windows (after a drop, or every "
                "~5 min in recovery); run 'scan' to confirm it is alive, or press "
                "the board button to wake it."
            )
        await asyncio.sleep(3.0)


async def _write_buzz(client: BleakClient, payload: bytes, *, response: bool = True,
                      label: str = "") -> None:
    """Plain (unencrypted) write to the buzz characteristic."""
    await client.write_gatt_char(BUZZ_CHAR_UUID, payload, response=response)


async def buzz_on(address: str | None, scan_timeout: float, connect_timeout: float,
                  response: bool = True) -> None:
    """Start the melody: connect, write 0x01, disconnect."""
    client = await connect_with_retry(address, scan_timeout, connect_timeout)
    try:
        mode = "with response" if response else "without response"
        print(f"Writing buzz_on = 0x{BUZZ_ON.hex()} to {BUZZ_CHAR_UUID} ({mode})")
        await _write_buzz(client, BUZZ_ON, response=response, label="buzz_on")
        print("Write OK.")
    finally:
        await client.disconnect()


async def buzz_off(address: str | None, scan_timeout: float, connect_timeout: float,
                   response: bool = True) -> None:
    """Stop the melody: connect, write 0x00, disconnect."""
    client = await connect_with_retry(address, scan_timeout, connect_timeout)
    try:
        print(f"Writing buzz_off = 0x{BUZZ_OFF.hex()} to {BUZZ_CHAR_UUID}")
        await _write_buzz(client, BUZZ_OFF, response=response, label="buzz_off")
        print("Write OK.")
    finally:
        await client.disconnect()


async def demo(address: str | None, duration: float, scan_timeout: float,
               connect_timeout: float) -> None:
    """buzz_on, wait, then buzz_off - over a single held connection.

    The device stays connected (option A), so we keep the link for the whole demo.
    If it does drop during the wait, we reconnect for the stop.
    """
    client = await connect_with_retry(address, scan_timeout, connect_timeout)
    try:
        print(f"buzz_on (0x{BUZZ_ON.hex()})")
        await _write_buzz(client, BUZZ_ON, label="buzz_on")
        print(f"Playing for {duration:.0f}s ...")
        await asyncio.sleep(duration)
        if not client.is_connected:
            print("Link dropped; reconnecting to stop ...")
            await client.disconnect()
            client = await connect_with_retry(address, scan_timeout, connect_timeout)
        print(f"buzz_off (0x{BUZZ_OFF.hex()})")
        await _write_buzz(client, BUZZ_OFF, label="buzz_off")
        print("Done.")
    finally:
        await client.disconnect()


async def test(address: str | None, scan_timeout: float,
               connect_timeout: float) -> None:
    """Interactive mode: send commands and receive status updates.

    Because the tag disconnects before each sleep, this reconnects on demand: if
    the link has dropped when you send a command, it transparently reconnects and
    re-subscribes to status notifications.

    Commands: buzz_on, buzz_off, status, quit
    """
    state: dict[str, BleakClient | None] = {"client": None}

    def _on_status_notify(_sender: int, data: bytearray) -> None:
        if len(data) >= 2:
            print(f"\n[NOTIFY] buzz_state=0x{data[0]:02x}, battery={data[1]}%",
                  flush=True)
        else:
            print(f"\n[NOTIFY] {data.hex()}", flush=True)
        print(">>> ", end="", flush=True)

    async def ensure_connected() -> BleakClient:
        client = state["client"]
        if client is not None and client.is_connected:
            return client
        if client is not None:
            print("(tag had disconnected - reconnecting)", flush=True)
        client = await connect_with_retry(address, scan_timeout, connect_timeout)
        state["client"] = client
        try:
            await client.start_notify(STATUS_CHAR_UUID, _on_status_notify)
            print(f"Listening for status updates on {STATUS_CHAR_UUID}")
        except BleakError as exc:
            print(f"Warning: could not enable notifications: {exc}")
        return client

    await ensure_connected()
    print("\nEnter commands: buzz_on, buzz_off, status, quit")
    print("-" * 50)

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                command = (await loop.run_in_executor(None, input, ">>> ")).lower().strip()
            except EOFError:
                print("EOF, exiting.")
                break

            if command == "quit":
                print("Disconnecting...")
                break

            payloads = {"buzz_on": BUZZ_ON, "buzz_off": BUZZ_OFF, "status": STATUS_CMD}
            if command not in payloads:
                if command:
                    print("Unknown command. Try: buzz_on, buzz_off, status, quit",
                          flush=True)
                continue

            try:
                client = await ensure_connected()
                print(f"Sending {command} (0x{payloads[command].hex()})...", flush=True)
                await _write_buzz(client, payloads[command], label=command)
                print("Write OK.", flush=True)
            except BleakError as exc:
                print(f"Error: {exc}", flush=True)
    finally:
        client = state["client"]
        if client is not None and client.is_connected:
            try:
                await client.stop_notify(STATUS_CHAR_UUID)
            except BleakError:
                pass
            await client.disconnect()
        print("Disconnected.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bleak test client for the Buzzer Tag.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="List nearby Buzzer Tags.")
    p_scan.add_argument("--timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)

    for name, helptext in (("buzz-on", "Start the melody (write 0x01)."),
                           ("buzz-off", "Stop the melody (write 0x00).")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--address", help="Target a specific device address.")
        p.add_argument("--no-response", action="store_true",
                       help="Use write-without-response.")
        p.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
        p.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)

    p_demo = sub.add_parser("demo", help="buzz_on -> wait --duration -> buzz_off (held link).")
    p_demo.add_argument("--address", help="Target a specific device address.")
    p_demo.add_argument("--duration", type=float, default=8.0,
                        help="Seconds to play before stopping.")
    p_demo.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    p_demo.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)

    p_test = sub.add_parser("test", help="Interactive mode (reconnects on demand).")
    p_test.add_argument("--address", help="Target a specific device address.")
    p_test.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    p_test.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)

    return parser


async def run(args: argparse.Namespace) -> None:
    if args.command == "scan":
        await scan(args.timeout)
    elif args.command == "buzz-on":
        await buzz_on(args.address, args.scan_timeout, args.connect_timeout,
                      response=not args.no_response)
    elif args.command == "buzz-off":
        await buzz_off(args.address, args.scan_timeout, args.connect_timeout,
                       response=not args.no_response)
    elif args.command == "demo":
        await demo(args.address, args.duration, args.scan_timeout, args.connect_timeout)
    elif args.command == "test":
        await test(args.address, args.scan_timeout, args.connect_timeout)


def main() -> int:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 0
    except BleakError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(
            "\nThe Buzzer Tag is meant to be held connected. If it dropped, it only "
            "advertises in ~10 s windows (after a drop, or every ~5 min in recovery), "
            "so just retry connecting - or press the board button to wake it into its "
            "2-minute setup window. (No pairing is needed; the buzz char is unencrypted.)",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - surface any runtime error to the CLI user
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
