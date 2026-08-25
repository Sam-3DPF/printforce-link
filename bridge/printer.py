"""One Bambu printer, wrapping `bambulabs_api`.

The library is imported lazily inside `connect()` so the pure logic (`map_status`,
`decode_hms`, `parse_telemetry`, `merge_status_payload`, and `ams.parse_ams`) can be
unit-tested without it installed. All library-specific accessor names live in this
one file — confirmed against real P1S hardware (2026-07-13, bambulabs-api 2.6.6) and
isolated here on purpose, so a naming difference only touches `_raw_status()` (the
status payload) and `_is_connected()` (the MQTT link). Those two methods are the
entire coupling to `bambulabs_api`; everything else consumes plain dicts.
"""
