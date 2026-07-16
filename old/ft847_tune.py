#!/usr/bin/env python3
"""
ft847_tune.py - Quick-tune utility for the Yaesu FT-847 via CAT.

Purpose
-------
SuperControl and the rig's own internal memory banks are great for your
regular, frequently-used repeaters. This tool is for everything else you
don't want to burn a memory channel on: one-off repeaters, weatherfax (WMC
etc.), ATC/aviation monitoring, satellite birds, DX nets, and so on.

Presets can come from two kinds of file, and you can mix both in the same
folder -- the script reads either directly, no conversion step needed:

  * a plain-text stations.txt style file (see FORMAT below)
  * a RepeaterBook.com CSV export, read directly

If you don't pass --file, the script scans the current folder (or --dir)
for .txt/.csv preset files and lets you pick one interactively. If you
don't pass a preset name, it lists what's in the chosen file and lets you
pick one interactively too. So you can just run:

    python3 ft847_tune.py --port /dev/ttyUSB0

...and it'll walk you through file, then preset, then tune the rig.

CAT command reference used here comes from the FT-847's documented opcode
table (5-byte command frames, as implemented in Hamlib's ft847.c driver).

Serial settings (per FT-847 manual / Hamlib): 4800-57600 baud, 8 data bits,
2 stop bits, no parity, no handshaking. Default below is 4800/2-stop as
that's the rig's factory CAT default -- override with --baud if needed.

Usage
-----
    python3 ft847_tune.py                              # fully interactive
    python3 ft847_tune.py --list                        # list files+presets, exit
    python3 ft847_tune.py --port /dev/ttyUSB0 GB3TS      # scripted, one preset
    python3 ft847_tune.py --file 6m_Victoria.csv --list
    python3 ft847_tune.py --file 6m_Victoria.csv VK3RDD --dry-run

FORMAT (stations.txt style files)
----------------------------------
    name, frequency_hz, mode, shift, offset_hz, tone_hz

    name        - identifier you'll type/select (no spaces)
    frequency   - the receive/simplex frequency in Hz (e.g. 145600000)
    mode        - one of: LSB USB CW CWR AM FM AMN FMN CWN CWNR
    shift       - one of: + - S   (plus, minus, simplex)
    offset_hz   - repeater shift amount in Hz (e.g. 600000 for 600kHz)
    tone_hz     - CTCSS tone in Hz (e.g. 88.5), or NONE

Comment lines start with #; inline "# comment" after a line is also fine.

FORMAT (RepeaterBook CSV)
--------------------------
Expected columns (RepeaterBook's standard export):
    Output Freq, Input Freq, Offset, Uplink Tone, Downlink Tone, Call,
    Location, County, State, Modes, Digital Access

  - Output Freq becomes the operating (receive) frequency
  - Shift direction/offset is derived from Output vs Input freq
  - Uplink Tone becomes the CTCSS tone (the tone your rig sends to open
    the repeater). Downlink Tone (used for tone squelch on receive) isn't
    applied, since the FT-847 drives encode+decode from one tone setting;
    if it differs from the uplink tone, that's noted alongside the preset.
  - Call sign becomes the preset name
  - Modes are read directly from the CSV: "FM" maps to wide FM, "FMN"/"NFM"
    maps to narrow FM. Use --force-narrow to override and treat all FM
    entries as narrow regardless of what the CSV says.
"""

import argparse
import configparser
import csv
import os
import re
import sys
import time

try:
    import serial
except ImportError:
    sys.exit(
        "This script needs pyserial. Install it with:\n"
        "    pip install pyserial --break-system-packages"
    )

# ---------------------------------------------------------------------------
# FT-847 CAT command opcodes (final byte of the 5-byte command frame)
# Source: Yaesu FT-847 CAT reference / Hamlib yaesu/ft847.c
# ---------------------------------------------------------------------------

OP_SET_FREQ_MAIN = 0x01
OP_READ_FREQ_MODE_MAIN = 0x03  # confirmed via Hamlib ft847.c + FT-847 manual
OP_LOCK_ON = 0x00   # REQUIRED on the FT-847: rig ignores all other CAT
OP_LOCK_OFF = 0x80  # commands until this is sent first (undocumented in
                     # the manual's quick examples but confirmed by users)
OP_RPT_SHIFT_MINUS = 0x09
OP_RPT_SHIFT_PLUS = 0x49
OP_RPT_SHIFT_SIMPLEX = 0x89
OP_RPT_OFFSET_FREQ = 0xF9
OP_SET_CTCSS_FREQ_MAIN = 0x0B
OP_CTCSS_DCS_MODE_MAIN = 0x0A  # opcode targets Main VFO; D1 selects the mode (below)
D1_DCS_ON = 0x0A
D1_CTCSS_ENC_DEC_ON = 0x2A  # tone squelch: both encode and decode
D1_CTCSS_ENC_ON = 0x4A      # encode only (tone out, no decode squelch on rx)
D1_CTCSS_DCS_OFF = 0x8A

MODE_OPCODES = {
    "LSB": (0x00, 0x07),
    "USB": (0x01, 0x07),
    "CW": (0x02, 0x07),
    "CWR": (0x03, 0x07),
    "AM": (0x04, 0x07),
    "FM": (0x08, 0x07),
    "CWN": (0x82, 0x07),
    "CWNR": (0x83, 0x07),
    "AMN": (0x84, 0x07),
    "FMN": (0x88, 0x07),
}

SHIFT_OPCODES = {
    "+": OP_RPT_SHIFT_PLUS,
    "-": OP_RPT_SHIFT_MINUS,
    "S": OP_RPT_SHIFT_SIMPLEX,
}

REVERSE_MODE = {p1: name for name, (p1, _op) in MODE_OPCODES.items()}

CMD_LEN = 5
PRESET_FILE_EXTS = (".txt", ".csv")

DEFAULT_CONFIG = {
    "port": None,          # None = interactive picker
    "baud": 9600,          # MUST match the rig's CAT RATE menu setting (FT-847 menu 15/16)
    "stopbits": 2,         # FT-847 CAT spec: 2 stop bits
    "bytesize": 8,
    "parity": "N",
    "rts": False,          # some USB-serial/level-shifter cables need RTS held high to power up
    "dtr": False,          # some need DTR instead -- try True here if nothing happens
    "command_delay": 0.05, # seconds between CAT commands
    "dir": ".",            # folder to scan for preset files
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not path or not os.path.exists(path):
        return cfg

    parser = configparser.ConfigParser()
    parser.read(path)
    if "serial" in parser:
        s = parser["serial"]
        if "port" in s and s["port"].strip():
            cfg["port"] = s["port"].strip()
        cfg["baud"] = s.getint("baud", fallback=cfg["baud"])
        cfg["stopbits"] = s.getfloat("stopbits", fallback=cfg["stopbits"])
        cfg["bytesize"] = s.getint("bytesize", fallback=cfg["bytesize"])
        cfg["parity"] = s.get("parity", fallback=cfg["parity"]).strip().upper()
        cfg["rts"] = s.getboolean("rts", fallback=cfg["rts"])
        cfg["dtr"] = s.getboolean("dtr", fallback=cfg["dtr"])
        cfg["command_delay"] = s.getfloat("command_delay", fallback=cfg["command_delay"])
    if "files" in parser:
        f = parser["files"]
        cfg["dir"] = f.get("dir", fallback=cfg["dir"]).strip()
    return cfg


# ---------------------------------------------------------------------------
# Low level CAT helpers
# ---------------------------------------------------------------------------

# FT-847 CTCSS tone table -- NOT a BCD-encoded frequency. The rig expects a
# single lookup-table byte (called "D1" in the manual) identifying the tone,
# sent as the first parameter byte. Table transcribed from the FT-847
# operating manual's "CAT (Computer Aided Transceiver) System Programming"
# section, cross-checked against Hamlib's ft847.c ft847_ctcss_cat[] array.
CTCSS_TONES = {
    67.0: 0x3F, 69.3: 0x39, 71.9: 0x1F, 74.4: 0x3E, 77.0: 0x0F,
    79.7: 0x3D, 82.5: 0x1E, 85.4: 0x3C, 88.5: 0x0E, 91.5: 0x3B,
    94.8: 0x1D, 97.4: 0x3A, 100.0: 0x0D, 103.5: 0x1C, 107.2: 0x0C,
    110.9: 0x1B, 114.8: 0x0B, 118.8: 0x1A, 123.0: 0x0A, 127.3: 0x19,
    131.8: 0x09, 136.5: 0x18, 141.3: 0x08, 146.2: 0x17, 151.4: 0x07,
    156.7: 0x16, 162.2: 0x06, 167.9: 0x15, 173.8: 0x05, 179.9: 0x14,
    186.2: 0x04, 192.8: 0x13, 203.5: 0x03, 210.7: 0x12, 218.1: 0x02,
    225.7: 0x11, 233.6: 0x01, 241.8: 0x10, 250.3: 0x00,
}


def freq_to_bcd(freq_hz: int) -> bytes:
    """FT-847 wants frequency as 8-digit BCD, in 10 Hz units, big-endian
    nibbles packed into 4 bytes."""
    val = freq_hz // 10  # rig resolution is 10 Hz
    digits = f"{val:08d}"
    b = bytearray(4)
    for i in range(4):
        hi = int(digits[i * 2])
        lo = int(digits[i * 2 + 1])
        b[i] = (hi << 4) | lo
    return bytes(b)


COMMAND_DELAY = 0.05  # overwritten from config/CLI in main()


def send(ser: "serial.Serial", cmd: bytes, label: str):
    assert len(cmd) == CMD_LEN, f"CAT frame must be {CMD_LEN} bytes"
    ser.write(cmd)
    time.sleep(COMMAND_DELAY)
    print(f"  -> sent {label}: {cmd.hex(' ')}")


def cat_lock_on(ser):
    cmd = bytes([0x00, 0x00, 0x00, 0x00, OP_LOCK_ON])
    send(ser, cmd, "CAT lock ON (enable remote control)")


def cat_lock_off(ser):
    cmd = bytes([0x00, 0x00, 0x00, 0x00, OP_LOCK_OFF])
    send(ser, cmd, "CAT lock OFF")


def cat_set_freq(ser, freq_hz: int):
    cmd = freq_to_bcd(freq_hz) + bytes([OP_SET_FREQ_MAIN])
    send(ser, cmd, f"set freq {freq_hz} Hz")


def bcd_to_freq(data: bytes) -> int:
    """Reverse of freq_to_bcd: 4 BCD bytes -> frequency in Hz."""
    digits = "".join(f"{b:02x}" for b in data)
    return int(digits) * 10


def cat_read_freq_mode(ser, timeout: float = 1.0):
    """Read back Main VFO frequency + mode (opcode 0x03, confirmed against
    Hamlib's ft847.c and the FT-847 manual). Returns (freq_hz, mode_str),
    or (None, None) if the rig didn't respond -- some early FT-847 units
    shipped without read capability (see the piWebCAT notes)."""
    old_timeout = ser.timeout
    ser.timeout = timeout
    try:
        ser.reset_input_buffer()
        cmd = bytes([0x00, 0x00, 0x00, 0x00, OP_READ_FREQ_MODE_MAIN])
        ser.write(cmd)
        resp = ser.read(5)
    finally:
        ser.timeout = old_timeout

    if len(resp) < 5:
        return None, None

    freq_hz = bcd_to_freq(resp[0:4])
    mode = REVERSE_MODE.get(resp[4], f"unknown (0x{resp[4]:02X})")
    return freq_hz, mode


def cat_set_mode(ser, mode: str):
    mode = mode.upper()
    if mode not in MODE_OPCODES:
        raise ValueError(f"Unknown mode '{mode}'. Valid: {', '.join(MODE_OPCODES)}")
    p1, op = MODE_OPCODES[mode]
    cmd = bytes([p1, 0x00, 0x00, 0x00, op])
    send(ser, cmd, f"set mode {mode}")


def cat_set_rptr_shift(ser, shift: str):
    shift = shift.upper()
    if shift not in SHIFT_OPCODES:
        raise ValueError("shift must be '+', '-' or 'S' (simplex)")
    cmd = bytes([SHIFT_OPCODES[shift], 0x00, 0x00, 0x00, 0x09])
    send(ser, cmd, f"set repeater shift '{shift}'")


def cat_set_rptr_offset(ser, offset_hz: int):
    cmd = freq_to_bcd(offset_hz) + bytes([OP_RPT_OFFSET_FREQ])
    send(ser, cmd, f"set repeater offset {offset_hz} Hz")


def cat_set_ctcss_tone(ser, tone_hz: float):
    if tone_hz not in CTCSS_TONES:
        closest = min(CTCSS_TONES, key=lambda t: abs(t - tone_hz))
        raise ValueError(
            f"Tone {tone_hz} Hz isn't a standard CTCSS tone the FT-847 supports. "
            f"Closest standard tone: {closest} Hz."
        )
    code = CTCSS_TONES[tone_hz]
    cmd = bytes([code, 0x00, 0x00, 0x00, OP_SET_CTCSS_FREQ_MAIN])
    send(ser, cmd, f"set CTCSS tone {tone_hz} Hz (code 0x{code:02X})")


def cat_tone_encode_only_on(ser):
    """Send only the CTCSS tone on transmit -- no decode squelch on receive.
    This is what most repeater users want: your rig has to send a tone to
    open the repeater, but you still want to hear all repeater traffic
    rather than squelching out anyone using a different/no tone."""
    cmd = bytes([D1_CTCSS_ENC_ON, 0x00, 0x00, 0x00, OP_CTCSS_DCS_MODE_MAIN])
    send(ser, cmd, "enable CTCSS tone (encode only)")


def cat_tone_squelch_on(ser):
    """Encode AND decode: rig also squelches unless it hears the same tone
    back. Most repeater setups don't need this -- see cat_tone_encode_only_on."""
    cmd = bytes([D1_CTCSS_ENC_DEC_ON, 0x00, 0x00, 0x00, OP_CTCSS_DCS_MODE_MAIN])
    send(ser, cmd, "enable tone squelch (enc+dec)")


def cat_tone_squelch_off(ser):
    cmd = bytes([D1_CTCSS_DCS_OFF, 0x00, 0x00, 0x00, OP_CTCSS_DCS_MODE_MAIN])
    send(ser, cmd, "disable tone squelch")


# ---------------------------------------------------------------------------
# Preset loading -- plain text
# ---------------------------------------------------------------------------

def load_presets_txt(path: str) -> dict:
    presets = {}
    with open(path, "r") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()  # strip full-line and inline comments
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 6:
                print(f"warning: skipping malformed line {lineno}: {raw!r}")
                continue
            name, freq, mode, shift, offset, tone = parts
            presets[name] = {
                "frequency": int(freq),
                "mode": mode,
                "shift": shift.upper(),
                "offset": int(offset) if offset and offset != "0" else 0,
                "tone": None if tone.upper() == "NONE" else float(tone),
                "note": "",
            }
    return presets


# ---------------------------------------------------------------------------
# Preset loading -- RepeaterBook CSV, read directly
# ---------------------------------------------------------------------------

def _sanitize_name(call: str, seen: set) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "", call.strip())
    if not name:
        name = "REPEATER"
    base = name
    n = 2
    while name in seen:
        name = f"{base}_{n}"
        n += 1
    seen.add(name)
    return name


def _parse_freq_mhz(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_presets_csv(path: str, force_narrow: bool = False) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print(f"warning: no rows found in {path}")
        return {}

    missing_cols = {"Output Freq", "Call"} - set(rows[0].keys())
    if missing_cols:
        sys.exit(
            f"'{path}' doesn't look like a RepeaterBook export -- "
            f"missing columns: {missing_cols}. Found: {list(rows[0].keys())}"
        )

    presets = {}
    seen_names = set()

    for i, row in enumerate(rows, start=2):  # header is line 1
        call = (row.get("Call") or "").strip()
        out_f = _parse_freq_mhz(row.get("Output Freq"))
        in_f = _parse_freq_mhz(row.get("Input Freq"))

        if out_f is None:
            print(f"warning: skipping row {i} ({call or 'unnamed'}): missing Output Freq")
            continue

        name = _sanitize_name(call or f"ROW{i}", seen_names)
        freq_hz = int(round(out_f * 1_000_000))

        if in_f is None or in_f == out_f:
            shift, offset_hz = "S", 0
        else:
            diff_hz = int(round((out_f - in_f) * 1_000_000))
            offset_field = _parse_freq_mhz(row.get("Offset"))
            offset_hz = int(round(offset_field * 1_000_000)) if offset_field else abs(diff_hz)
            shift = "-" if diff_hz > 0 else "+"

        modes_field = (row.get("Modes") or "").strip().upper()
        if "FMN" in modes_field or "NFM" in modes_field:
            mode = "FMN"  # CSV explicitly indicates narrow
        elif "FM" in modes_field or not modes_field:
            mode = "FMN" if force_narrow else "FM"
        else:
            mode = modes_field.split()[0]

        uplink_tone = (row.get("Uplink Tone") or "").strip()
        downlink_tone = (row.get("Downlink Tone") or "").strip()
        tone = float(uplink_tone) if uplink_tone else None

        location = (row.get("Location") or "").strip()
        note_bits = [location] if location else []
        if downlink_tone and downlink_tone != uplink_tone:
            note_bits.append(f"downlink tone {downlink_tone} not applied")
        note = ", ".join(note_bits)

        presets[name] = {
            "frequency": freq_hz,
            "mode": mode,
            "shift": shift,
            "offset": offset_hz,
            "tone": tone,
            "note": note,
        }

    return presets


def load_presets(path: str, force_narrow: bool = False) -> dict:
    if path.lower().endswith(".csv"):
        return load_presets_csv(path, force_narrow=force_narrow)
    return load_presets_txt(path)


# ---------------------------------------------------------------------------
# Interactive pickers
# ---------------------------------------------------------------------------

def discover_preset_files(directory: str) -> list:
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        sys.exit(f"Directory not found: {directory}")
    files = sorted(
        f for f in entries
        if f.lower().endswith(PRESET_FILE_EXTS) and os.path.isfile(os.path.join(directory, f))
    )
    return files


def choose_file_interactive(directory: str) -> str:
    files = discover_preset_files(directory)
    if not files:
        sys.exit(f"No .txt or .csv preset files found in '{directory}'.")
    if len(files) == 1:
        print(f"Using preset file: {files[0]}")
        return os.path.join(directory, files[0])

    print(f"Preset files found in '{directory}':\n")
    for i, f in enumerate(files, 1):
        kind = "RepeaterBook CSV" if f.lower().endswith(".csv") else "stations file"
        print(f"  {i}. {f}  ({kind})")
    print()
    choice = input("Select a file by number: ").strip()
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(files)):
            raise ValueError
    except ValueError:
        sys.exit("Invalid selection.")
    return os.path.join(directory, files[idx])


def print_preset_table(presets: dict):
    print(f"\nAvailable presets ({len(presets)}):\n")
    for i, (name, p) in enumerate(presets.items(), 1):
        tone_str = f"{p['tone']} Hz" if p["tone"] else "none"
        shift_str = {"+": "positive", "-": "negative", "S": "simplex"}[p["shift"]]
        note = f"  ({p['note']})" if p.get("note") else ""
        print(f"  {i:>3}. {name:<16} {p['frequency']/1e6:>10.5f} MHz  {p['mode']:<4}  "
              f"shift={shift_str:<9} offset={p['offset']/1e3:.0f} kHz  tone={tone_str}{note}")


def choose_preset_interactive(presets: dict) -> str:
    names = list(presets.keys())
    print_preset_table(presets)
    print()
    choice = input("Select a preset by number or name: ").strip()
    if choice in presets:
        return choice
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(names):
            return names[idx]
    except ValueError:
        pass
    sys.exit("Invalid selection.")


def choose_port_interactive() -> str:
    try:
        from serial.tools import list_ports
    except ImportError:
        return input("Enter serial port (e.g. COM3 or /dev/ttyUSB0): ").strip()

    ports = list(list_ports.comports())
    if not ports:
        return input(
            "No serial ports auto-detected. Enter one manually "
            "(e.g. COM3 on Windows, /dev/ttyUSB0 on Linux/Mac): "
        ).strip()
    if len(ports) == 1:
        print(f"Using serial port: {ports[0].device}")
        return ports[0].device

    print("Available serial ports:\n")
    for i, p in enumerate(ports, 1):
        desc = f" - {p.description}" if p.description and p.description != "n/a" else ""
        print(f"  {i}. {p.device}{desc}")
    print()
    choice = input("Select a port by number: ").strip()
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(ports)):
            raise ValueError
    except ValueError:
        sys.exit("Invalid selection.")
    return ports[idx].device


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports detected. Check the rig's USB/serial cable is plugged in "
              "and its driver is installed (e.g. FTDI/Prolific/CH340 driver on Windows).")
        return
    print("Available serial ports:")
    for p in ports:
        desc = f" - {p.description}" if p.description and p.description != "n/a" else ""
        print(f"  {p.device}{desc}")
    print("\nOn Windows this will look like COM3, COM4, etc. -- use e.g. --port COM3")


# ---------------------------------------------------------------------------
# Apply a preset to the rig
# ---------------------------------------------------------------------------

def apply_preset(ser, name: str, preset: dict):
    print(f"Tuning to '{name}':")
    cat_lock_on(ser)  # FT-847 ignores everything below until this is sent
    # Mode set before frequency: if the rig's ARS (Automatic Repeater Shift)
    # only auto-applies shift when already in FM at the moment the frequency
    # changes into a repeater sub-band, setting freq-then-mode (the old
    # order) would never trigger it, since the rig would still be in
    # whatever mode it was in before this command sequence ran.
    cat_set_mode(ser, preset["mode"])
    cat_set_freq(ser, preset["frequency"])

    if preset["shift"] in ("+", "-"):
        cat_set_rptr_offset(ser, preset["offset"])
        cat_set_rptr_shift(ser, preset["shift"])
    else:
        cat_set_rptr_shift(ser, "S")

    if preset["tone"] is not None:
        cat_set_ctcss_tone(ser, preset["tone"])
        cat_tone_encode_only_on(ser)
    else:
        cat_tone_squelch_off(ser)

    readback_freq, readback_mode = cat_read_freq_mode(ser)
    if readback_freq is None:
        print("(rig did not respond to read-back request -- can't confirm)")
    else:
        freq_ok = readback_freq == preset["frequency"]
        mode_ok = readback_mode == preset["mode"].upper()
        status = "OK" if freq_ok and mode_ok else "MISMATCH"
        print(f"Read back from rig: {readback_freq/1e6:.5f} MHz, {readback_mode}  [{status}]")
        if not freq_ok:
            print(f"  expected frequency {preset['frequency']/1e6:.5f} MHz")
        if not mode_ok:
            print(f"  expected mode {preset['mode'].upper()}")

    print("Done.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def open_rig_serial(cfg: dict, port: str):
    stopbits_map = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}
    bytesize_map = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}
    parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}

    try:
        ser = serial.Serial(
            port=port,
            baudrate=cfg["baud"],
            bytesize=bytesize_map.get(cfg["bytesize"], serial.EIGHTBITS),
            parity=parity_map.get(cfg["parity"], serial.PARITY_NONE),
            stopbits=stopbits_map.get(cfg["stopbits"], serial.STOPBITS_TWO),
            timeout=1,
        )
        if cfg["rts"]:
            ser.rts = True
        if cfg["dtr"]:
            ser.dtr = True
    except serial.SerialException as e:
        print(f"Couldn't open port '{port}': {e}\n")
        list_serial_ports()
        sys.exit(1)

    print(f"Connected: {port} @ {cfg['baud']} baud, {cfg['bytesize']}{cfg['parity']}{cfg['stopbits']}, "
          f"RTS={cfg['rts']}, DTR={cfg['dtr']}\n")
    return ser


def main():
    ap = argparse.ArgumentParser(description="Quick-tune the FT-847 to a saved preset via CAT.")
    ap.add_argument("preset", nargs="?", help="Preset name (omit to pick interactively)")
    ap.add_argument("--file", help="Preset file: .txt or RepeaterBook .csv (omit to pick interactively)")
    ap.add_argument("--dir", default=None, help="Directory to scan for preset files (overrides config)")
    ap.add_argument("--force-narrow", action="store_true", help="CSV import: treat all FM entries as narrow, ignoring what the CSV says")
    ap.add_argument("--config", default="ft847.ini", help="Config file path (default: ft847.ini)")
    ap.add_argument("--port", default=None,
                     help="Serial port, e.g. COM3 on Windows or /dev/ttyUSB0 on Linux/Mac. "
                          "Overrides config file. If omitted everywhere, you'll be prompted.")
    ap.add_argument("--baud", type=int, default=None, help="Baud rate. Overrides config file.")
    ap.add_argument("--list", action="store_true", help="List available presets and exit")
    ap.add_argument("--status", action="store_true", help="Just read back the rig's current freq/mode and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print CAT frames without opening the serial port")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # CLI flags win over the config file when both are given
    if args.port is not None:
        cfg["port"] = args.port
    if args.baud is not None:
        cfg["baud"] = args.baud
    if args.dir is not None:
        cfg["dir"] = args.dir

    global COMMAND_DELAY
    COMMAND_DELAY = cfg["command_delay"]

    if args.status:
        port = cfg["port"] or choose_port_interactive()
        ser = open_rig_serial(cfg, port)
        try:
            cat_lock_on(ser)
            freq, mode = cat_read_freq_mode(ser)
            if freq is None:
                print("Rig did not respond to the read-back request.\n"
                      "Some early FT-847 units shipped without CAT read capability.")
            else:
                print(f"Rig currently reports: {freq/1e6:.5f} MHz, mode {mode}")
        finally:
            ser.close()
        return

    file_path = args.file or choose_file_interactive(cfg["dir"])

    try:
        presets = load_presets(file_path, force_narrow=args.force_narrow)
    except FileNotFoundError:
        sys.exit(f"Preset file not found: {file_path}")

    if not presets:
        sys.exit(f"No presets loaded from {file_path}.")

    if args.list:
        print_preset_table(presets)
        return

    preset_name = args.preset or choose_preset_interactive(presets)

    if preset_name not in presets:
        sys.exit(f"Unknown preset '{preset_name}'. Use --list to see options.")

    preset = presets[preset_name]

    if args.dry_run:
        print(f"[dry run] would tune to '{preset_name}': {preset}")
        return

    port = cfg["port"] or choose_port_interactive()
    ser = open_rig_serial(cfg, port)

    try:
        apply_preset(ser, preset_name, preset)
    finally:
        ser.close()


if __name__ == "__main__":
    main()