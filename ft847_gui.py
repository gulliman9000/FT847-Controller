#!/usr/bin/env python3
"""
ft847_gui.py - Desktop GUI for the Yaesu FT-847 quick-tune tool.

Built on Tkinter (bundled with Python -- nothing extra to install beyond
pyserial). Loads presets from a stations.txt file or a RepeaterBook.com
CSV export, lets you pick one from a list, and tunes the rig over CAT.

Run:
    python3 ft847_gui.py
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import ft847_cat as cat

CONFIG_PATH = "ft847.ini"


class Ft847App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FT-847 Quick Tune")
        self.geometry("820x560")
        self.minsize(700, 450)

        self.cfg = cat.load_config(CONFIG_PATH)
        self.presets = {}
        self.current_file = None
        self.log_queue = queue.Queue()

        self._build_menu()
        self._build_layout()
        self._refresh_ports()
        self._auto_load_folder()
        self.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self)

        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open preset file...", command=self._browse_file)
        filemenu.add_command(label="Open folder...", command=self._browse_folder)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=filemenu)

        settingsmenu = tk.Menu(menubar, tearoff=0)
        settingsmenu.add_command(label="Serial settings...", command=self._open_settings)
        menubar.add_cascade(label="Settings", menu=settingsmenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)

        self.config(menu=menubar)

    def _build_layout(self):
        # --- Top bar: preset file selection ---
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Preset file:").pack(side="left")
        self.file_var = tk.StringVar()
        self.file_combo = ttk.Combobox(top, textvariable=self.file_var, state="readonly", width=40)
        self.file_combo.pack(side="left", padx=(4, 8))
        self.file_combo.bind("<<ComboboxSelected>>", lambda e: self._load_selected_file())

        ttk.Button(top, text="Browse...", command=self._browse_file).pack(side="left")
        ttk.Button(top, text="Reload", command=self._reload_current_folder).pack(side="left", padx=(4, 0))

        # --- Preset table ---
        table_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        table_frame.pack(fill="both", expand=True)

        columns = ("name", "freq", "mode", "shift", "offset", "tone", "note")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headers = {
            "name": ("Name", 110), "freq": ("Freq (MHz)", 190), "mode": ("Mode", 80),
            "shift": ("Shift", 70), "offset": ("Offset (kHz)", 90), "tone": ("Tone (Hz)", 80),
            "note": ("Note", 180),
        }
        for col, (label, width) in headers.items():
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._tune_selected())

        # --- Connection bar ---
        conn = ttk.Frame(self, padding=8)
        conn.pack(fill="x")

        ttk.Label(conn, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=self.cfg.get("port") or "")
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, state="readonly", width=16)
        self.port_combo.pack(side="left", padx=(4, 4))
        ttk.Button(conn, text="Refresh", command=self._refresh_ports).pack(side="left")

        ttk.Label(conn, text="  Baud:").pack(side="left")
        self.baud_var = tk.StringVar(value=str(self.cfg.get("baud", 9600)))
        ttk.Entry(conn, textvariable=self.baud_var, width=8).pack(side="left", padx=(4, 12))

        self.tune_btn = ttk.Button(conn, text="Tune to Selected", command=self._tune_selected)
        self.tune_btn.pack(side="left")
        ttk.Button(conn, text="Read Rig Status", command=self._read_status).pack(side="left", padx=(6, 0))
        ttk.Button(conn, text="Dry Run", command=self._dry_run_selected).pack(side="left", padx=(6, 0))

        # --- Log area ---
        log_frame = ttk.LabelFrame(self, text="Activity log", padding=6)
        log_frame.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="none", font=("Consolas", 9))
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_vsb.pack(side="right", fill="y")

        # --- Status bar ---
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    # Logging helpers (thread-safe: worker threads push to a queue,
    # the main loop drains it on a timer)
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------
    # File / preset loading
    # ------------------------------------------------------------------

    def _auto_load_folder(self):
        directory = self.cfg.get("dir", ".")
        files = cat.discover_preset_files(directory)
        self.file_combo["values"] = files
        if files:
            self.file_var.set(files[0])
            self._load_selected_file()
        else:
            self.status_var.set(f"No .txt/.csv preset files found in '{directory}'. Use File > Open.")

    def _reload_current_folder(self):
        directory = self.cfg.get("dir", ".")
        files = cat.discover_preset_files(directory)
        self.file_combo["values"] = files
        if self.current_file:
            self._load_file(self.current_file)

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Open preset file",
            filetypes=[("Preset files", "*.txt *.csv"), ("All files", "*.*")],
        )
        if path:
            self._load_file(path)
            self.cfg["dir"] = os.path.dirname(path) or "."
            files = cat.discover_preset_files(self.cfg["dir"])
            self.file_combo["values"] = files
            self.file_var.set(os.path.basename(path))

    def _browse_folder(self):
        directory = filedialog.askdirectory(title="Open folder with preset files")
        if directory:
            self.cfg["dir"] = directory
            files = cat.discover_preset_files(directory)
            self.file_combo["values"] = files
            if files:
                self.file_var.set(files[0])
                self._load_selected_file()
            else:
                messagebox.showinfo("No presets found", f"No .txt/.csv files found in {directory}")

    def _load_selected_file(self):
        directory = self.cfg.get("dir", ".")
        fname = self.file_var.get()
        if not fname:
            return
        path = os.path.join(directory, fname) if directory != "." else fname
        self._load_file(path)

    def _load_file(self, path: str):
        try:
            presets = cat.load_presets(path)
        except (FileNotFoundError, cat.Ft847Error) as e:
            messagebox.showerror("Error loading presets", str(e))
            return
        self.current_file = path
        self.presets = presets
        self._populate_table()
        self.status_var.set(f"Loaded {len(presets)} presets from {os.path.basename(path)}")
        self._log(f"Loaded {len(presets)} presets from {path}")

    def _populate_table(self):
        self.tree.delete(*self.tree.get_children())
        for name, p in self.presets.items():
            if p.get("type") == "crossband":
                tone_label = f"{p['tx_tone']}" if p.get("tx_tone") else "-"
                self.tree.insert("", "end", iid=name, values=(
                    name,
                    f"RX {p['rx_frequency']/1e6:.5f} / TX {p['tx_frequency']/1e6:.5f}",
                    f"{p['rx_mode']}/{p['tx_mode']}", "X-BAND", "-", tone_label,
                    p.get("note", ""),
                ))
                continue
            shift_label = {"+": "+", "-": "-", "S": "simplex"}[p["shift"]]
            tone_label = f"{p['tone']}" if p["tone"] else "-"
            dial_offset = p.get("dial_offset_hz", 0)
            mode_label = p.get("display_mode", p["mode"])
            if dial_offset:
                tuned = (p["frequency"] + dial_offset) / 1e6
                freq_label = f"{p['frequency']/1e6:.5f} (tune {tuned:.5f})"
            else:
                freq_label = f"{p['frequency']/1e6:.5f}"
            self.tree.insert("", "end", iid=name, values=(
                name, freq_label, mode_label, shift_label,
                f"{p['offset']/1e3:.0f}", tone_label, p.get("note", ""),
            ))

    def _get_selected_preset(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No preset selected", "Select a preset from the list first.")
            return None, None
        name = sel[0]
        return name, self.presets.get(name)

    # ------------------------------------------------------------------
    # Serial port handling
    # ------------------------------------------------------------------

    def _refresh_ports(self):
        ports = cat.get_serial_ports()
        values = [dev for dev, _desc in ports]
        self.port_combo["values"] = values
        if values and not self.port_var.get():
            self.port_var.set(values[0])
        if not values:
            self.status_var.set("No serial ports detected. Check cable/driver.")

    def _current_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg["port"] = self.port_var.get()
        try:
            cfg["baud"] = int(self.baud_var.get())
        except ValueError:
            cfg["baud"] = 9600
        return cfg

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _dry_run_selected(self):
        name, preset = self._get_selected_preset()
        if not preset:
            return
        self._log(f"[dry run] would tune to '{name}': {preset}")

    def _tune_selected(self):
        name, preset = self._get_selected_preset()
        if not preset:
            return
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No port selected", "Choose a serial port first.")
            return
        self._run_in_thread(self._do_tune, name, preset, port)

    def _read_status(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No port selected", "Choose a serial port first.")
            return
        self._run_in_thread(self._do_read_status, port)

    def _run_in_thread(self, target, *args):
        self.tune_btn.configure(state="disabled")
        self.status_var.set("Working...")
        t = threading.Thread(target=self._thread_wrapper, args=(target, *args), daemon=True)
        t.start()

    def _thread_wrapper(self, target, *args):
        try:
            target(*args)
        finally:
            self.after(0, lambda: self.tune_btn.configure(state="normal"))
            self.after(0, lambda: self.status_var.set("Ready."))

    def _do_tune(self, name, preset, port):
        cfg = self._current_cfg()
        try:
            ser = cat.open_rig_serial(cfg, port)
        except cat.Ft847Error as e:
            self._log(f"ERROR: {e}")
            self.after(0, lambda: messagebox.showerror("Connection error", str(e)))
            return
        self._log(f"Connected: {port} @ {cfg['baud']} baud")
        self._log(f"Tuning to '{name}':")
        try:
            cat.apply_preset(ser, preset, cfg["command_delay"], log=self._log)
        except cat.Ft847Error as e:
            self._log(f"ERROR: {e}")
        finally:
            ser.close()
        self._log("Done.\n")

    def _do_read_status(self, port):
        cfg = self._current_cfg()
        try:
            ser = cat.open_rig_serial(cfg, port)
        except cat.Ft847Error as e:
            self._log(f"ERROR: {e}")
            self.after(0, lambda: messagebox.showerror("Connection error", str(e)))
            return
        self._log(f"Connected: {port} @ {cfg['baud']} baud")
        try:
            cat.cat_lock_on(ser, cfg["command_delay"], log=self._log)
            freq, mode = cat.cat_read_freq_mode(ser)
            if freq is None:
                self._log("Rig did not respond to the read-back request.")
            else:
                self._log(f"Rig currently reports: {freq/1e6:.5f} MHz, mode {mode}")
        finally:
            ser.close()

    # ------------------------------------------------------------------
    # Settings dialog
    # ------------------------------------------------------------------

    def _open_settings(self):
        win = tk.Toplevel(self)
        win.title("Serial settings")
        win.resizable(False, False)
        win.transient(self)

        fields = {}
        rows = [
            ("Baud", "baud", str(self.cfg.get("baud", 9600))),
            ("Stop bits (1/1.5/2)", "stopbits", str(self.cfg.get("stopbits", 2))),
            ("Byte size", "bytesize", str(self.cfg.get("bytesize", 8))),
            ("Parity (N/E/O)", "parity", str(self.cfg.get("parity", "N"))),
            ("Command delay (s)", "command_delay", str(self.cfg.get("command_delay", 0.05))),
        ]
        for i, (label, key, default) in enumerate(rows):
            ttk.Label(win, text=label + ":").grid(row=i, column=0, sticky="w", padx=8, pady=4)
            var = tk.StringVar(value=default)
            ttk.Entry(win, textvariable=var, width=12).grid(row=i, column=1, padx=8, pady=4)
            fields[key] = var

        rts_var = tk.BooleanVar(value=bool(self.cfg.get("rts", False)))
        dtr_var = tk.BooleanVar(value=bool(self.cfg.get("dtr", False)))
        ttk.Checkbutton(win, text="Assert RTS", variable=rts_var).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", padx=8)
        ttk.Checkbutton(win, text="Assert DTR", variable=dtr_var).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=8)

        def save_and_close():
            try:
                self.cfg["baud"] = int(fields["baud"].get())
                self.cfg["stopbits"] = float(fields["stopbits"].get())
                self.cfg["bytesize"] = int(fields["bytesize"].get())
                self.cfg["parity"] = fields["parity"].get().strip().upper()
                self.cfg["command_delay"] = float(fields["command_delay"].get())
                self.cfg["rts"] = rts_var.get()
                self.cfg["dtr"] = dtr_var.get()
                self.cfg["port"] = self.port_var.get()
                cat.save_config(CONFIG_PATH, self.cfg)
                self.baud_var.set(str(self.cfg["baud"]))
                self._log(f"Settings saved to {CONFIG_PATH}")
            except ValueError as e:
                messagebox.showerror("Invalid value", str(e))
                return
            win.destroy()

        ttk.Button(win, text="Save", command=save_and_close).grid(
            row=len(rows) + 2, column=0, columnspan=2, pady=8)

    def _show_about(self):
        messagebox.showinfo(
            "About",
            "FT-847 Quick Tune\n\n"
            "A free CAT-control tool for the Yaesu FT-847: repeater "
            "shift, CTCSS, and frequency/mode presets loaded from a "
            "stations.txt file or a RepeaterBook.com CSV export.\n\n"
            "https://github.com/YOUR_USERNAME/ft847-tune"
        )


if __name__ == "__main__":
    app = Ft847App()
    app.mainloop()
