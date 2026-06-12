"""Constants for the Buzzer Tag integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "buzzer_tag"

# --- Device contract (authoritative, from the firmware) ----------------------
DEVICE_NAME: Final = "Buzzer Tag"
SERVICE_UUID: Final = "12340000-1234-5678-1234-56789abcdef0"
BUZZ_CHAR_UUID: Final = "12340001-1234-5678-1234-56789abcdef0"
STATUS_CHAR_UUID: Final = "12340002-1234-5678-1234-56789abcdef0"

# Write payloads (exactly one byte each). Only these three are accepted; any
# other value is rejected by the firmware with ATT 0x13 (Value Not Allowed).
BUZZ_ON: Final = b"\x01"
BUZZ_OFF: Final = b"\x00"
BUZZ_STATUS: Final = b"\x02"

# The melody loops for up to ~2 minutes, then the firmware stops it by itself.
# Used as an optimistic timeout so the switch flips back off if we miss the
# "playback ended" status notification.
MELODY_TIMEOUT_S: Final = 120

# After a link drop the device advertises for ~10 s, then enters a recovery
# cycle (advertise ~10 s, sleep ~5 min). We reconnect when we see it advertise,
# but the retry loop also tries again on this cadence as a backstop.
RECONNECT_BACKOFF_S: Final = 20

# Periodic check that flips us into "reconnect" mode if the held link has gone
# stale without a disconnect callback ever firing (e.g. after a battery swap).
HEALTH_CHECK_INTERVAL_S: Final = 30

# The device only pushes a status notification on events (subscribe, play start/
# stop, before sleep). To keep the battery reading fresh on an otherwise idle
# device, poll the status char (write 0x02) on this cadence. Battery drifts
# slowly, so once a day is plenty.
STATUS_POLL_INTERVAL_H: Final = 24
