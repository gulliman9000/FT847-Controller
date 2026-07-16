#!/usr/bin/env python3
"""
ft847_cli.py - Command-line quick-tune tool for the Yaesu FT-847.

Reads presets from a stations.txt file or a RepeaterBook.com CSV export
(either directly, no conversion needed) and tunes the rig via CAT:
frequency, mode, repeater shift, and CTCSS tone.

Run with no arguments for a fully interactive walkthrough (pick a preset
file, pick a preset, pick a serial port). Or pass everything on the
command line for scripted use. See README.md for full details.

Usage
-----
    python3 ft847_cli.py                                 # interactive
    python3 ft847_cli.py --list
    python3 ft847_cli.py --file 6m_Victoria.csv VK3RDD
    python3 ft847_cli.py --status
"""

import argparse
import sys

import ft847_cat as cat


def choose_file_interactive(directory: str) -> str:
    files = cat.discover_preset_files(directory)
    if not files:
        sys.exit(f"No .txt or .csv preset files found in '{directory}'.")
    if len(files) == 1:
        print(f"Using preset file: {files[0]}")
        return f"{directory.rstrip('/')}/{files[0]}" if directory != "." else files[0]

    print(f"Preset files found in '{directory}':\n")
    for i, f in enumerate(files, 1):
        kind = "RepeaterBook CSV" if f.lower().endswith(".csv") else "stations file"
        print(f"  {i}. {f}  ({kind})")
    choice = input("\nSelect a file by number: ").strip()
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(files)):
            raise ValueError
    except ValueError:
        sys.exit("Invalid selection.")
    fname = files[idx]
    return f"{directory.rstrip('/')}/{fname}" if directory != "." else fname


def print_preset_table(presets: dict):
    print(f"\nAvailable presets ({len(presets)}):\n")
    for i, (name, p) in enumerate(presets.items(), 1):
        if p.get("type") == "crossband":
            tx_tone_str = f"{p['tx_tone']} Hz" if p.get("tx_tone") else "none"
            note = f"  ({p['note']})" if p.get("note") else ""
            print(f"  {i:>3}. {name:<16} CROSSBAND  "
                  f"RX {p['rx_frequency']/1e6:>10.5f} MHz {p['rx_mode']:<4}  "
                  f"TX {p['tx_frequency']/1e6:>10.5f} MHz {p['tx_mode']:<4}  "
                  f"tx_tone={tx_tone_str}{note}")
            continue
        tone_str = f"{p['tone']} Hz" if p["tone"] else "none"
        shift_str = {"+": "positive", "-": "negative", "S": "simplex"}[p["shift"]]
        note = f"  ({p['note']})" if p.get("note") else ""
        dial_offset = p.get("dial_offset_hz", 0)
        mode_str = p.get("display_mode", p["mode"])
        if dial_offset:
            tuned = (p["frequency"] + dial_offset) / 1e6
            freq_str = f"{p['frequency']/1e6:>10.5f} MHz (tune {tuned:.5f})"
        else:
            freq_str = f"{p['frequency']/1e6:>10.5f} MHz"
        print(f"  {i:>3}. {name:<16} {freq_str}  {mode_str:<4}  "
              f"shift={shift_str:<9} offset={p['offset']/1e3:.0f} kHz  tone={tone_str}{note}")


def choose_preset_interactive(presets: dict) -> str:
    names = list(presets.keys())
    print_preset_table(presets)
    choice = input("\nSelect a preset by number or name: ").strip()
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
    ports = cat.get_serial_ports()
    if not ports:
        return input("No serial ports auto-detected. Enter one manually: ").strip()
    if len(ports) == 1:
        print(f"Using serial port: {ports[0][0]}")
        return ports[0][0]
    print("Available serial ports:\n")
    for i, (dev, desc) in enumerate(ports, 1):
        print(f"  {i}. {dev}" + (f" - {desc}" if desc and desc != "n/a" else ""))
    choice = input("\nSelect a port by number: ").strip()
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(ports)):
            raise ValueError
    except ValueError:
        sys.exit("Invalid selection.")
    return ports[idx][0]


def main():
    ap = argparse.ArgumentParser(description="Quick-tune the FT-847 to a saved preset via CAT.")
    ap.add_argument("preset", nargs="?", help="Preset name (omit to pick interactively)")
    ap.add_argument("--file", help="Preset file: .txt or RepeaterBook .csv")
    ap.add_argument("--dir", default=None, help="Directory to scan for preset files")
    ap.add_argument("--force-narrow", action="store_true", help="Treat all CSV FM entries as narrow")
    ap.add_argument("--config", default="ft847.ini", help="Config file path (default: ft847.ini)")
    ap.add_argument("--port", default=None, help="Serial port, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=None, help="Baud rate")
    ap.add_argument("--list", action="store_true", help="List available presets and exit")
    ap.add_argument("--status", action="store_true", help="Read back rig's current freq/mode and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be sent, don't open the port")
    ap.add_argument("--version", action="version", version=f"ft847-tune {cat.__version__}")
    args = ap.parse_args()

    cfg = cat.load_config(args.config)
    if args.port is not None:
        cfg["port"] = args.port
    if args.baud is not None:
        cfg["baud"] = args.baud
    if args.dir is not None:
        cfg["dir"] = args.dir

    if args.status:
        port = cfg["port"] or choose_port_interactive()
        try:
            ser = cat.open_rig_serial(cfg, port)
        except cat.Ft847Error as e:
            sys.exit(str(e))
        try:
            cat.cat_lock_on(ser, cfg["command_delay"])
            freq, mode = cat.cat_read_freq_mode(ser)
            if freq is None:
                print("Rig did not respond to the read-back request.")
            else:
                print(f"Rig currently reports: {freq/1e6:.5f} MHz, mode {mode}")
        finally:
            ser.close()
        return

    file_path = args.file or choose_file_interactive(cfg["dir"])
    try:
        presets = cat.load_presets(file_path, force_narrow=args.force_narrow)
    except (FileNotFoundError, cat.Ft847Error) as e:
        sys.exit(str(e))

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
    try:
        ser = cat.open_rig_serial(cfg, port)
    except cat.Ft847Error as e:
        sys.exit(str(e))

    print(f"Connected: {port} @ {cfg['baud']} baud\n")
    print(f"Tuning to '{preset_name}':")
    try:
        cat.apply_preset(ser, preset, cfg["command_delay"], log=print)
    finally:
        ser.close()
    print("Done.")


if __name__ == "__main__":
    main()
