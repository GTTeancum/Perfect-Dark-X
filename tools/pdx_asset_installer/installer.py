"""Perfect Dark X user-owned asset installer GUI."""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk

if __package__:
    from .backend import InstallCancelled, InstallRequest, install_game
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backend import InstallCancelled, InstallRequest, install_game


APP_TITLE = "Perfect Dark X Asset Installer"


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


class InstallerWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        root.title(APP_TITLE)
        root.geometry("780x575")
        root.minsize(700, 535)
        root.protocol("WM_DELETE_WINDOW", self._close)

        self.xbe_var = tk.StringVar(value=self._default_xbe())
        self.rom_var = tk.StringVar(value=self._default_rom())
        self.texture_var = tk.StringVar(value=self._default_texture_pack())
        self.output_var = tk.StringVar(value=self._default_output())
        self.xiso_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Select your legally owned ROM to begin.")
        self.progress_var = tk.IntVar(value=0)

        self._build_ui()
        self.root.after(100, self._poll_events)

    def _default_xbe(self) -> str:
        base = application_directory()
        candidates = (base / "default.xbe", base / "build-xbox" / "default.xbe")
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return str(candidates[0])

    def _default_rom(self) -> str:
        candidates = sorted(application_directory().glob("*.z64"))
        return str(candidates[0]) if len(candidates) == 1 else ""

    def _default_texture_pack(self) -> str:
        base = application_directory()
        candidates = (
            base / "ext_tex.pak",
            base / "texturepack-work" / "release" / "ext_tex.pak",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return str(candidates[0])

    def _default_output(self) -> str:
        base = application_directory()
        if getattr(sys, "frozen", False):
            return str(base / "Perfect Dark X")
        return str(base / "build-xbox" / "Perfect Dark X")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(8, weight=1)

        title = ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 17, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 5))
        intro = ttk.Label(
            outer,
            text=(
                "Build a ROM-free Xbox folder from your own NTSC-final ROM. "
                "The source ROM is read only and is never copied to the output."
            ),
            wraplength=720,
        )
        intro.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(0, 16))

        self._path_row(
            outer,
            2,
            "Core XBE",
            self.xbe_var,
            lambda: self._browse_file(
                self.xbe_var, "Select default.xbe", (("Xbox executable", "*.xbe"),)
            ),
        )
        self._path_row(
            outer,
            3,
            "N64 ROM",
            self.rom_var,
            lambda: self._browse_file(
                self.rom_var,
                "Select your NTSC-final Perfect Dark ROM",
                (("Nintendo 64 ROM", "*.z64"), ("All files", "*.*")),
            ),
        )
        self._path_row(
            outer,
            4,
            "Texture pack",
            self.texture_var,
            lambda: self._browse_file(
                self.texture_var,
                "Select an optional separate texture pack",
                (("Perfect Dark X texture pack", "*.pak"), ("All files", "*.*")),
            ),
        )
        self._path_row(
            outer,
            5,
            "Output folder",
            self.output_var,
            self._browse_output,
        )

        ttk.Checkbutton(
            outer,
            text="Also create a user-local XISO for XEMU or compatible Xbox loaders",
            variable=self.xiso_var,
        ).grid(row=6, column=1, columnspan=2, sticky=tk.W, pady=(5, 0))

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(18, 10))
        progress_frame.columnconfigure(0, weight=1)
        ttk.Label(progress_frame, textvariable=self.status_var).grid(row=0, column=0, sticky=tk.W)
        self.progress = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress.grid(row=1, column=0, sticky=tk.EW, pady=(6, 0))

        self.log = scrolledtext.ScrolledText(
            outer,
            height=9,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
        )
        self.log.grid(row=8, column=0, columnspan=3, sticky=tk.NSEW, pady=(0, 12))

        buttons = ttk.Frame(outer)
        buttons.grid(row=9, column=0, columnspan=3, sticky=tk.E)
        self.install_button = ttk.Button(buttons, text="Install", command=self._start)
        self.install_button.pack(side=tk.LEFT, padx=(0, 8))
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self._cancel, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Close", command=self._close).pack(side=tk.LEFT)

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command,
        optional: bool = False,
    ) -> None:
        text = f"{label} (optional)" if optional else label
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky=tk.W, padx=(0, 12), pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky=tk.EW, pady=5)
        ttk.Button(parent, text="Browse...", command=command).grid(
            row=row, column=2, padx=(10, 0), pady=5
        )

    def _browse_file(self, variable: tk.StringVar, title: str, filetypes) -> None:
        selected = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if selected:
            variable.set(selected)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(title="Choose a new or empty output folder")
        if selected:
            self.output_var.set(selected)

    def _append_log(self, message: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, message.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if (
            not self.xbe_var.get().strip()
            or not self.rom_var.get().strip()
            or not self.texture_var.get().strip()
            or not self.output_var.get().strip()
        ):
            messagebox.showerror(
                APP_TITLE,
                "Core XBE, N64 ROM, texture pack, and output folder are required.",
            )
            return

        request = InstallRequest(
            xbe=Path(self.xbe_var.get().strip()),
            rom=Path(self.rom_var.get().strip()),
            output=Path(self.output_var.get().strip()),
            texture_pack=Path(self.texture_var.get().strip()),
            create_xiso=self.xiso_var.get(),
        )
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.status_var.set("Starting installation...")
        self._append_log("Starting a transactional install. The destination is updated only after success.")
        self.install_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)

        def run() -> None:
            try:
                result = install_game(
                    request,
                    progress=lambda percent, message: self.events.put(
                        ("progress", (percent, message))
                    ),
                    cancel_event=self.cancel_event,
                )
                self.events.put(("success", result))
            except InstallCancelled as exc:
                self.events.put(("cancelled", str(exc)))
            except BaseException as exc:
                self.events.put(("error", (str(exc), traceback.format_exc())))

        self.worker = threading.Thread(target=run, name="pdx-asset-install", daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state=tk.DISABLED)
            self.status_var.set("Cancelling safely...")
            self._append_log("Cancellation requested; cleaning the temporary install folder...")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    percent, message = payload
                    self.progress_var.set(percent)
                    self.status_var.set(message)
                    self._append_log(f"{percent:3d}%  {message}")
                elif kind == "success":
                    result = payload
                    self._finished()
                    texture = "with the separate texture pack" if result.texture_pack_installed else "with stock textures"
                    message = (
                        f"Installed {result.file_count:,} ROM files and "
                        f"{result.segment_count} runtime segments {texture}.\n\n"
                        f"Output: {result.output}"
                    )
                    if result.xiso:
                        message += f"\nLocal XISO: {result.xiso}"
                    self.status_var.set("Installation complete.")
                    self._append_log(message.replace("\n\n", "\n"))
                    messagebox.showinfo(APP_TITLE, message)
                elif kind == "cancelled":
                    self._finished()
                    self.progress_var.set(0)
                    self.status_var.set(str(payload))
                    self._append_log(str(payload))
                elif kind == "error":
                    message, details = payload
                    self._finished()
                    self.status_var.set("Installation failed.")
                    self._append_log(details)
                    messagebox.showerror(APP_TITLE, message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _finished(self) -> None:
        self.install_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                APP_TITLE,
                "An installation is active. Cancel it and close after cleanup?",
            ):
                return
            self.cancel_event.set()
            self.root.after(100, self._wait_then_close)
            return
        self.root.destroy()

    def _wait_then_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._wait_then_close)
        else:
            self.root.destroy()


def self_test() -> int:
    import tkinter

    if sys.stdout:
        print(f"{APP_TITLE}: Python {sys.version.split()[0]}, Tk {tkinter.TkVersion}")
        print(f"Application directory: {application_directory()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="validate imports without opening a window")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    InstallerWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
