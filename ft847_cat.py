"""
ft847_cat.py - Core CAT control and preset-loading library for the Yaesu FT-847.

This module has no UI code in it at all -- it's imported by both the
command-line tool (ft847_cli.py) and the GUI (ft847_gui.py), so the two
front-ends can never drift apart in behaviour.

CAT protocol notes (all reverse-engineered / confirmed against real
hardware -- see README.md for the full story)
-----------------------------------------------
- The FT-847 ignores all CAT commands until a "lock on" frame (five zero
  bytes, opcode 0x00) is sent first.
- Frequency is 4 bytes of big-endian BCD in 10 Hz units, then a 5th opcode
  byte selecting which VFO to write.
- CTCSS tone is NOT a BCD frequency -- it's a lookup-table byte (0x00-0x3F)
  identifying one of 39 standard tones, sent as the first data byte.
- The CTCSS/DCS mode command (encode-only / encode+decode / DCS / off) and
  the repeater-shift command (minus/plus/simplex) both put their selector
  byte FIRST, with a fixed opcode as the final byte -- opposite of what
  the manual's example tables suggest at a glance.
- The repeater-OFFSET-amount command (opcode 0xF9) is effectively
  non-functional on real hardware. The rig instead applies its own
  internally menu-configured default offset for the current band once
  shift direction is set -- exactly what the original SuperControl
  software relies on too (confirmed by sniffing its actual CAT traffic).
"""

import configparser
import csv
import os
import re
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


# ---------------------------------------------------------------------------
# CAT command opcodes (final byte of the 5-byte command frame, unless noted)
# ---------------------------------------------------------------------------

OP_SET_FREQ_MAIN = 0x01
OP_READ_FREQ_MODE_MAIN = 0x03
OP_LOCK_ON = 0x00
OP_LOCK_OFF = 0x80
OP_RPT_SHIFT = 0x09  # fixed opcode; direction goes in the first data byte
OP_SET_CTCSS_FREQ_MAIN = 0x0B
OP_CTCSS_DCS_MODE_MAIN = 0x0A  # fixed opcode; mode selector in first data byte
OP_SATELLITE_ON = 0x4E
OP_SATELLITE_OFF = 0x8E

# VFO targeting: the FT-847 selects Main/SAT-RX/SAT-TX VFO by adding an
# offset to several base opcodes (frequency set/read, mode set, CTCSS
# set/mode). Confirmed via the manual's opcode table and Hamlib's
# opcode_vfo() bit-manipulation, which does exactly this.
VFO_OFFSETS = {"main": 0x00, "satrx": 0x10, "sattx": 0x20}

SHIFT_SELECTORS = {"+": 0x49, "-": 0x09, "S": 0x89}

D1_DCS_ON = 0x0A
D1_CTCSS_ENC_DEC_ON = 0x2A
D1_CTCSS_ENC_ON = 0x4A
D1_CTCSS_DCS_OFF = 0x8A

MODE_OPCODES = {
    "LSB": (0x00, 0x07), "USB": (0x01, 0x07), "CW": (0x02, 0x07), "CWR": (0x03, 0x07),
    "AM": (0x04, 0x07), "FM": (0x08, 0x07),
    "CWN": (0x82, 0x07), "CWNR": (0x83, 0x07), "AMN": (0x84, 0x07), "FMN": (0x88, 0x07),
}
REVERSE_MODE = {p1: name for name, (p1, _op) in MODE_OPCODES.items()}

CMD_LEN = 5
PRESET_FILE_EXTS = (".txt", ".csv")

# FT-847 CTCSS tone table -- lookup-table byte, not a BCD frequency.
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

DEFAULT_CONFIG = {
    "port": None,
    "baud": 9600,
    "stopbits": 2,
    "bytesize": 8,
    "parity": "N",
    "rts": False,
    "dtr": False,
    "command_delay": 0.05,
    "dir": ".",
}


class Ft847Error(Exception):
    """Raised for CAT/preset errors the UI layer should show to the user."""


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not path or not os.path.exists(path):
        return cfg
    parser = configparser.ConfigParser()
    parser.read(path)
    if "serial" in parser:
        s = parser["serial"]
        if s.get("port", "").strip():
            cfg["port"] = s["port"].strip()
        cfg["baud"] = s.getint("baud", fallback=cfg["baud"])
        cfg["stopbits"] = s.getfloat("stopbits", fallback=cfg["stopbits"])
        cfg["bytesize"] = s.getint("bytesize", fallback=cfg["bytesize"])
        cfg["parity"] = s.get("parity", fallback=cfg["parity"]).strip().upper()
        cfg["rts"] = s.getboolean("rts", fallback=cfg["rts"])
        cfg["dtr"] = s.getboolean("dtr", fallback=cfg["dtr"])
        cfg["command_delay"] = s.getfloat("command_delay", fallback=cfg["command_delay"])
    if "files" in parser:
        cfg["dir"] = parser["files"].get("dir", fallback=cfg["dir"]).strip()
    return cfg


def save_config(path: str, cfg: dict):
    parser = configparser.ConfigParser()
    parser["serial"] = {
        "port": cfg.get("port") or "",
        "baud": str(cfg.get("baud", 9600)),
        "stopbits": str(cfg.get("stopbits", 2)),
        "bytesize": str(cfg.get("bytesize", 8)),
        "parity": cfg.get("parity", "N"),
        "rts": str(bool(cfg.get("rts", False))),
        "dtr": str(bool(cfg.get("dtr", False))),
        "command_delay": str(cfg.get("command_delay", 0.05)),
    }
    parser["files"] = {"dir": cfg.get("dir", ".")}
    with open(path, "w") as f:
        parser.write(f)


# ---------------------------------------------------------------------------
# Low level CAT byte encoding
# ---------------------------------------------------------------------------

def freq_to_bcd(freq_hz: int) -> bytes:
    val = freq_hz // 10
    digits = f"{val:08d}"
    b = bytearray(4)
    for i in range(4):
        b[i] = (int(digits[i * 2]) << 4) | int(digits[i * 2 + 1])
    return bytes(b)


def bcd_to_freq(data: bytes) -> int:
    digits = "".join(f"{b:02x}" for b in data)
    return int(digits) * 10


# ---------------------------------------------------------------------------
# CAT commands. Every function takes an optional log(str) callback so the
# GUI can stream command activity into a text widget instead of stdout.
# ---------------------------------------------------------------------------

def _send(ser, cmd: bytes, label: str, delay: float, log=None):
    assert len(cmd) == CMD_LEN
    ser.write(cmd)
    time.sleep(delay)
    if log:
        log(f"  -> {label}: {cmd.hex(' ')}")


def cat_lock_on(ser, delay, log=None):
    _send(ser, bytes([0, 0, 0, 0, OP_LOCK_ON]), "CAT lock ON", delay, log)


def cat_lock_off(ser, delay, log=None):
    _send(ser, bytes([0, 0, 0, 0, OP_LOCK_OFF]), "CAT lock OFF", delay, log)


def cat_satellite_on(ser, delay, log=None):
    _send(ser, bytes([0, 0, 0, 0, OP_SATELLITE_ON]), "satellite mode ON", delay, log)


def cat_satellite_off(ser, delay, log=None):
    _send(ser, bytes([0, 0, 0, 0, OP_SATELLITE_OFF]), "satellite mode OFF", delay, log)


def cat_set_freq(ser, freq_hz, delay, vfo="main", log=None):
    opcode = OP_SET_FREQ_MAIN + VFO_OFFSETS[vfo]
    cmd = freq_to_bcd(freq_hz) + bytes([opcode])
    _send(ser, cmd, f"set freq {freq_hz} Hz ({vfo})", delay, log)


def cat_set_mode(ser, mode, delay, vfo="main", log=None):
    mode = mode.upper()
    if mode not in MODE_OPCODES:
        raise Ft847Error(f"Unknown mode '{mode}'. Valid: {', '.join(MODE_OPCODES)}")
    p1, base_op = MODE_OPCODES[mode]
    opcode = base_op + VFO_OFFSETS[vfo]
    _send(ser, bytes([p1, 0, 0, 0, opcode]), f"set mode {mode} ({vfo})", delay, log)


def cat_set_rptr_shift(ser, shift, delay, log=None):
    shift = shift.upper()
    if shift not in SHIFT_SELECTORS:
        raise Ft847Error("shift must be '+', '-' or 'S'")
    cmd = bytes([SHIFT_SELECTORS[shift], 0, 0, 0, OP_RPT_SHIFT])
    _send(ser, cmd, f"set repeater shift '{shift}'", delay, log)


def cat_set_ctcss_tone(ser, tone_hz, delay, vfo="main", log=None):
    if tone_hz not in CTCSS_TONES:
        closest = min(CTCSS_TONES, key=lambda t: abs(t - tone_hz))
        raise Ft847Error(f"{tone_hz} Hz isn't a standard CTCSS tone. Closest: {closest} Hz.")
    code = CTCSS_TONES[tone_hz]
    opcode = OP_SET_CTCSS_FREQ_MAIN + VFO_OFFSETS[vfo]
    cmd = bytes([code, 0, 0, 0, opcode])
    _send(ser, cmd, f"set CTCSS tone {tone_hz} Hz (0x{code:02X}) ({vfo})", delay, log)


def cat_tone_encode_only_on(ser, delay, vfo="main", log=None):
    opcode = OP_CTCSS_DCS_MODE_MAIN + VFO_OFFSETS[vfo]
    cmd = bytes([D1_CTCSS_ENC_ON, 0, 0, 0, opcode])
    _send(ser, cmd, f"CTCSS encode only ON ({vfo})", delay, log)


def cat_tone_squelch_on(ser, delay, vfo="main", log=None):
    opcode = OP_CTCSS_DCS_MODE_MAIN + VFO_OFFSETS[vfo]
    cmd = bytes([D1_CTCSS_ENC_DEC_ON, 0, 0, 0, opcode])
    _send(ser, cmd, f"CTCSS encode+decode ON ({vfo})", delay, log)


def cat_tone_squelch_off(ser, delay, vfo="main", log=None):
    opcode = OP_CTCSS_DCS_MODE_MAIN + VFO_OFFSETS[vfo]
    cmd = bytes([D1_CTCSS_DCS_OFF, 0, 0, 0, opcode])
    _send(ser, cmd, f"CTCSS/DCS OFF ({vfo})", delay, log)


def cat_read_freq_mode(ser, vfo="main", timeout=1.0):
    """Returns (freq_hz, mode_str) or (None, None) if no response."""
    opcode = OP_READ_FREQ_MODE_MAIN + VFO_OFFSETS[vfo]
    old_timeout = ser.timeout
    ser.timeout = timeout
    try:
        ser.reset_input_buffer()
        ser.write(bytes([0, 0, 0, 0, opcode]))
        resp = ser.read(5)
    finally:
        ser.timeout = old_timeout
    if len(resp) < 5:
        return None, None
    freq_hz = bcd_to_freq(resp[0:4])
    mode = REVERSE_MODE.get(resp[4], f"unknown(0x{resp[4]:02X})")
    return freq_hz, mode


def apply_normal_preset(ser, preset: dict, delay: float, log=None) -> dict:
    """Sends the full command sequence for a normal (same-band, single-VFO)
    preset -- frequency, mode, repeater shift, CTCSS. Returns a dict with
    readback results: {'freq': int|None, 'mode': str|None, 'freq_ok': bool,
    'mode_ok': bool}."""
    cat_lock_on(ser, delay, log)
    if log and preset["shift"] in ("+", "-"):
        log("(reminder: rig must be in VFO mode, not Memory mode, for "
            "repeater shift/ARS to work -- this can't be set via CAT, "
            "check the front panel VFO/MEM button if shift doesn't apply)")
    # If a previous crossband/satellite preset left the rig in Satellite
    # mode, it stays there until explicitly turned off -- the rig doesn't
    # revert automatically just because a new frequency is set. Always
    # force it off for a normal preset so Main VFO behaves as expected.
    cat_satellite_off(ser, delay, log)
    cat_set_mode(ser, preset["mode"], delay, log=log)
    cat_set_freq(ser, preset["frequency"], delay, log=log)

    if preset["shift"] in ("+", "-"):
        cat_set_rptr_shift(ser, preset["shift"], delay, log)
    else:
        cat_set_rptr_shift(ser, "S", delay, log)

    if preset["tone"] is not None:
        cat_set_ctcss_tone(ser, preset["tone"], delay, log=log)
        cat_tone_encode_only_on(ser, delay, log=log)
    else:
        cat_tone_squelch_off(ser, delay, log=log)

    freq, mode = cat_read_freq_mode(ser)
    result = {
        "freq": freq,
        "mode": mode,
        "freq_ok": freq == preset["frequency"] if freq is not None else None,
        "mode_ok": mode == preset["mode"].upper() if mode is not None else None,
    }
    if log:
        if freq is None:
            log("(no read-back response from rig)")
        else:
            status = "OK" if result["freq_ok"] and result["mode_ok"] else "MISMATCH"
            log(f"Read back: {freq/1e6:.5f} MHz, {mode}  [{status}]")
    return result


def apply_crossband_preset(ser, preset: dict, delay: float, log=None) -> dict:
    """Sends the full command sequence for a crossband preset using the
    FT-847's Satellite mode: independent SAT RX VFO (receive) and SAT TX
    VFO (transmit), which can be on completely different bands. This is
    the CAT-controllable mechanism for crossband nets, repeaters, and
    actual satellite work alike -- the FT-847 doesn't support true CAT
    "split" on the Main VFO, but Satellite mode covers the same need.

    Preset dict must have: rx_frequency, rx_mode, tx_frequency, tx_mode,
    and optionally tx_tone (CTCSS sent on transmit)."""
    cat_lock_on(ser, delay, log)
    cat_satellite_on(ser, delay, log)

    cat_set_mode(ser, preset["rx_mode"], delay, vfo="satrx", log=log)
    cat_set_freq(ser, preset["rx_frequency"], delay, vfo="satrx", log=log)

    cat_set_mode(ser, preset["tx_mode"], delay, vfo="sattx", log=log)
    cat_set_freq(ser, preset["tx_frequency"], delay, vfo="sattx", log=log)

    tx_tone = preset.get("tx_tone")
    if tx_tone is not None:
        cat_set_ctcss_tone(ser, tx_tone, delay, vfo="sattx", log=log)
        cat_tone_encode_only_on(ser, delay, vfo="sattx", log=log)
    else:
        cat_tone_squelch_off(ser, delay, vfo="sattx", log=log)

    rx_freq, rx_mode = cat_read_freq_mode(ser, vfo="satrx")
    tx_freq, tx_mode = cat_read_freq_mode(ser, vfo="sattx")
    result = {
        "rx_freq": rx_freq, "rx_mode": rx_mode,
        "tx_freq": tx_freq, "tx_mode": tx_mode,
        "rx_ok": rx_freq == preset["rx_frequency"] if rx_freq is not None else None,
        "tx_ok": tx_freq == preset["tx_frequency"] if tx_freq is not None else None,
    }
    if log:
        if rx_freq is None or tx_freq is None:
            log("(no read-back response from rig for one or both VFOs)")
        else:
            status = "OK" if result["rx_ok"] and result["tx_ok"] else "MISMATCH"
            log(f"Read back: RX {rx_freq/1e6:.5f} MHz {rx_mode}, "
                f"TX {tx_freq/1e6:.5f} MHz {tx_mode}  [{status}]")
    return result


def apply_preset(ser, preset: dict, delay: float, log=None) -> dict:
    """Dispatches to the right command sequence based on preset['type']."""
    if preset.get("type") == "crossband":
        return apply_crossband_preset(ser, preset, delay, log)
    return apply_normal_preset(ser, preset, delay, log)


# ---------------------------------------------------------------------------
# Serial port discovery / opening
# ---------------------------------------------------------------------------

def get_serial_ports() -> list:
    """Returns list of (device, description) tuples for available ports."""
    if list_ports is None:
        return []
    return [(p.device, p.description or "") for p in list_ports.comports()]


def open_rig_serial(cfg: dict, port: str):
    if serial is None:
        raise Ft847Error("pyserial is not installed. Run: pip install pyserial")
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
        if cfg.get("rts"):
            ser.rts = True
        if cfg.get("dtr"):
            ser.dtr = True
    except serial.SerialException as e:
        raise Ft847Error(f"Couldn't open port '{port}': {e}")
    return ser


# ---------------------------------------------------------------------------
# Preset loading -- plain text stations.txt style
# ---------------------------------------------------------------------------

def load_presets_txt(path: str) -> dict:
    """Parses two line formats:

    Normal (6 fields):
        name, frequency_hz, mode, shift, offset_hz, tone_hz

    Crossband / satellite-mode (8 fields, 2nd field literally "CROSSBAND"):
        name, CROSSBAND, rx_freq_hz, rx_mode, tx_freq_hz, tx_mode, tx_tone_hz, note
    """
    presets = {}
    with open(path, "r") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]

            if len(parts) >= 2 and parts[1].upper() == "CROSSBAND":
                if len(parts) < 7:
                    continue
                name = parts[0]
                rx_freq, rx_mode, tx_freq, tx_mode, tx_tone = parts[2:7]
                note = parts[7] if len(parts) > 7 else ""
                presets[name] = {
                    "type": "crossband",
                    "rx_frequency": int(rx_freq),
                    "rx_mode": rx_mode,
                    "tx_frequency": int(tx_freq),
                    "tx_mode": tx_mode,
                    "tx_tone": None if tx_tone.upper() == "NONE" else float(tx_tone),
                    "note": note,
                }
                continue

            if len(parts) != 6:
                continue
            name, freq, mode, shift, offset, tone = parts
            presets[name] = {
                "type": "normal",
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
    name = re.sub(r"[^A-Za-z0-9_]", "", call.strip()) or "REPEATER"
    base, n = name, 2
    while name in seen:
        name = f"{base}_{n}"
        n += 1
    seen.add(name)
    return name


def _parse_freq_mhz(s):
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
        return {}
    missing = {"Output Freq", "Call"} - set(rows[0].keys())
    if missing:
        raise Ft847Error(
            f"'{path}' doesn't look like a RepeaterBook export -- missing columns: {missing}"
        )

    presets = {}
    seen = set()
    for i, row in enumerate(rows, start=2):
        call = (row.get("Call") or "").strip()
        out_f = _parse_freq_mhz(row.get("Output Freq"))
        in_f = _parse_freq_mhz(row.get("Input Freq"))
        if out_f is None:
            continue

        name = _sanitize_name(call or f"ROW{i}", seen)
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
            mode = "FMN"
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

        presets[name] = {
            "type": "normal",
            "frequency": freq_hz,
            "mode": mode,
            "shift": shift,
            "offset": offset_hz,
            "tone": tone,
            "note": ", ".join(note_bits),
        }
    return presets


def load_presets(path: str, force_narrow: bool = False) -> dict:
    if path.lower().endswith(".csv"):
        return load_presets_csv(path, force_narrow=force_narrow)
    return load_presets_txt(path)


def discover_preset_files(directory: str) -> list:
    if not os.path.isdir(directory):
        return []
    return sorted(
        f for f in os.listdir(directory)
        if f.lower().endswith(PRESET_FILE_EXTS) and os.path.isfile(os.path.join(directory, f))
    )
