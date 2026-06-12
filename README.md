# Buzzer Tag — Home Assistant integration

A custom [Home Assistant](https://www.home-assistant.io/) integration for the
**Buzzer Tag** BLE peripheral — a tiny battery-powered device that plays a
looping melody on command. Installable through [HACS](https://hacs.xyz/).

The tag uses **no pairing, bonding, or encryption**: Home Assistant simply
connects over Bluetooth Low Energy and writes a one-byte command to a plain
GATT characteristic.

## Features

- **Auto-discovery** — Home Assistant's Bluetooth panel finds nearby Buzzer
  Tags by name or service UUID and offers to add them. Multiple tags are told
  apart by a short suffix of their address (e.g. `Buzzer Tag EEFF`).
- **Switch** — turn the looping melody on/off. The firmware auto-stops after
  ~2 minutes, so the switch arms an optimistic timer that flips it back off if
  the device's own "ended" notification is missed.
- **Battery sensor** — coarse CR2032 battery percentage.
- **Buzzing binary sensor** — reflects the device's reported playback state.
- **Held low-power connection** — the integration keeps a single connection
  open per device over the low-power link the firmware requests, so commands
  land within a couple of seconds.
- **Graceful reconnect** — if the link drops, the integration reconnects the
  moment the tag advertises again. The tag sleeps for long stretches and only
  advertises in ~10-second windows (then every ~5 minutes in recovery), so the
  integration waits for those windows rather than busy-polling.

## Installation (HACS)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/JohnJocoo/hacs-buzzer` as an **Integration**.
3. Search for **Buzzer Tag**, download it, and restart Home Assistant.
4. The tag should be auto-discovered under **Settings → Devices & Services**.
   If not, press the device's button to wake it (it advertises for ~10 s) and
   add it manually via **Add Integration → Buzzer Tag**.

## Manual installation

Copy `custom_components/buzzer_tag/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Requirements

- Home Assistant with a working Bluetooth adapter (the
  [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/)).
- Python dependency `bleak-retry-connector` (installed automatically).

## Device contract

| Property        | Value |
|-----------------|-------|
| Advertised name | `Buzzer Tag` |
| Service UUID    | `12340000-1234-5678-1234-56789abcdef0` |
| Buzz char       | `12340001-1234-5678-1234-56789abcdef0` (write, plain) |
| Status char     | `12340002-1234-5678-1234-56789abcdef0` (read/notify: `[buzz_state, battery%]`) |
| Commands        | `0x01` buzz on · `0x00` buzz off · `0x02` request status |

## Notes

- Playback is **independent of the connection**: writing `0x01` then
  disconnecting leaves the melody playing until the 2-minute timeout. To stop
  early, turn the switch off (the integration reconnects if needed).
- There is **no pairing prompt**, ever.
- Restarting the tag does **not** require re-adding it in Home Assistant — its
  address is stable.

## License

See [LICENSE](LICENSE).
