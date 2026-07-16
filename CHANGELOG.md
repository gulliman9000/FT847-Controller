# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/), and versioning follows
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):
MAJOR for breaking changes to preset file formats or CLI/GUI behavior,
MINOR for new backward-compatible features, PATCH for fixes and docs.

## [1.5.1] - 2026-07-16

### Added
- `__version__` in `ft847_cat.py` as the single source of truth
- `--version` flag for the CLI
- Version shown in the GUI window title and About dialog

## [1.5.0] - 2026-07-16

### Added
- `FAX` mode alias: resolves to real CAT mode `USB` plus a `-2000 Hz`
  dial offset automatically. No extra fields needed for the common
  weatherfax case; the 8-field form still works to override the default
  offset per-station.

## [1.4.0] - 2026-07-16

### Added
- `dial_offset_hz` field for `stations.txt` presets. `frequency_hz` stays
  the published/schedule-matching value; the tool applies the offset when
  actually tuning the rig. Built for weatherfax (tune ~2 kHz below the
  published carrier for USB demod), but generic to any similar case.
- GUI and CLI now show published frequency alongside the actual tuned
  frequency when a dial offset is set.

### Fixed
- Read-back verification now correctly compares against the actual tuned
  frequency, not the published one, when a dial offset is in play.

## [1.3.0] - 2026-07-16

### Added
- Optional 7th field (free-text note) in the plain `stations.txt` format,
  shown in both the GUI table and CLI listing.
- Duplicate preset names (e.g. the same station listed under two
  different time windows) are now auto-suffixed (`_2`, `_3`, ...) instead
  of silently overwriting the earlier entry.

## [1.2.0] - 2026-07-15

### Added
- SO-50 (two-tone arm/QSO sequence) and ISS crossband repeater examples
  in `examples/stations.example.txt`.

### Fixed
- Normal (non-crossband) presets now explicitly turn Satellite mode off.
  Previously, tuning to a crossband/satellite preset and then a regular
  repeater preset left the rig stuck in Satellite mode.

### Documentation
- Documented that the FT-847 has no CAT command to switch VFO/Memory
  mode (confirmed via Hamlib's `ft847.c`, which has no `set_vfo`
  function for this rig). Runtime reminder added to the log when a
  preset with repeater shift is applied.

## [1.1.0] - 2026-07-14 to 2026-07-15

### Added
- Crossband / satellite mode support: CAT commands generalized to target
  Main, SAT-RX, or SAT-TX VFO independently, using the FT-847's Satellite
  mode (the rig has no CAT-controllable split on the Main VFO).
- New `CROSSBAND` preset line format for independent TX/RX frequencies.

### Documentation
- Protocol notes cross-referenced against
  [Hamlib issue #1286](https://github.com/Hamlib/Hamlib/issues/1286),
  which independently confirms the lock-on frame, read-frequency command
  format, and the VFO-offset addressing scheme.

## [1.0.0] - 2026-07-13

Initial release.

### Added
- Core CAT library (`ft847_cat.py`) shared by both front-ends.
- Command-line tool (`ft847_cli.py`) with interactive and scripted modes.
- Tkinter GUI (`ft847_gui.py`) with preset table, port picker, and a
  serial settings dialog.
- Direct RepeaterBook.com CSV import — no conversion step needed.
- `ft847.ini` config file for serial port, baud, RTS/DTR, etc.
- Reverse-engineered FT-847 CAT protocol quirks, confirmed against real
  hardware:
  - CAT lock-on frame required before any other command is accepted
  - CTCSS tone is a lookup-table byte, not a BCD-encoded frequency
  - CTCSS/DCS mode and repeater-shift commands put their selector byte
    first, with a fixed opcode as the final byte
  - Repeater-offset-*amount* command is non-functional on real hardware;
    the rig applies its own menu-configured default offset per band
    instead (confirmed by sniffing FT847-SuperControl's own CAT traffic)
