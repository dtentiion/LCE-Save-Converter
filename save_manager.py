"""
Minecraft LCE Save Manager - GUI
Converts Xbox 360 (.bin STFS CON) save files to Windows64 LCE format.

Usage:
    python save_manager.py                   # GUI
    python save_manager.py save.bin          # preload a file
"""

import sys
import os
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# -- optional drag-and-drop --
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False

import customtkinter as ctk
from tkinter import filedialog

from converter import convert_bin_to_win64

# -- theme --
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C_BG        = "#1c1c1c"
C_SURFACE   = "#2b2b2b"
C_STROKE    = "#3a3a3a"
C_ACCENT    = "#0078d4"
C_ACCENT_H  = "#1a86d8"
C_WARN_BG   = "#3a2e1a"
C_WARN_FG   = "#f0c060"
C_SUCCESS   = "#4caf7d"
C_TEXT      = "#f3f3f3"
C_SUBTEXT   = "#9d9d9d"
C_DROP_IDLE = "#252525"
C_DROP_HOV  = "#1e3a5a"

FONT_BODY   = ("Segoe UI", 11)
FONT_SMALL  = ("Segoe UI", 10)
FONT_MONO   = ("Consolas", 10)


# =============================================================================
# Widgets
# =============================================================================

class DropZone(ctk.CTkFrame):
    """A card the user can click or drag a .bin file onto."""

    def __init__(self, master, on_file, **kw):
        super().__init__(master,
                         fg_color=C_DROP_IDLE,
                         corner_radius=10,
                         border_width=2,
                         border_color=C_STROKE,
                         **kw)
        self._on_file = on_file
        self._file    = None

        self._icon = ctk.CTkLabel(self, text="[folder]", font=("Segoe UI Emoji", 32))
        self._icon.pack(pady=(22, 4))

        self._hint = ctk.CTkLabel(
            self,
            text="Drop your .bin file here",
            font=FONT_BODY,
            text_color=C_SUBTEXT,
        )
        self._hint.pack()

        self._btn = ctk.CTkButton(
            self,
            text="Browse for .bin file",
            font=FONT_BODY,
            fg_color=C_SURFACE,
            hover_color=C_STROKE,
            text_color=C_TEXT,
            border_width=1,
            border_color=C_STROKE,
            corner_radius=6,
            width=180,
            command=self._browse,
        )
        self._btn.pack(pady=(10, 22))

        if _HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self._on_drop)
            self.dnd_bind('<<DragEnter>>', self._drag_enter)
            self.dnd_bind('<<DragLeave>>', self._drag_leave)

        for w in (self, self._icon, self._hint):
            w.bind('<Button-1>', lambda _e: self._browse())

    def _browse(self):
        p = filedialog.askopenfilename(
            title="Select Xbox 360 Minecraft LCE save",
            filetypes=[("Xbox 360 saves", "*.bin"), ("All files", "*.*")],
        )
        if p:
            self._set_file(p)

    def _on_drop(self, event):
        self.configure(fg_color=C_DROP_IDLE)
        raw = event.data.strip()
        if raw.startswith('{') and raw.endswith('}'):
            raw = raw[1:-1]
        if os.path.isfile(raw):
            self._set_file(raw)

    def _drag_enter(self, _e):
        self.configure(fg_color=C_DROP_HOV, border_color=C_ACCENT)

    def _drag_leave(self, _e):
        self.configure(fg_color=C_DROP_IDLE, border_color=C_STROKE)

    def _set_file(self, path: str):
        self._file = path
        sz = os.path.getsize(path)
        label = f"{Path(path).name}  ({sz / 1_048_576:.1f} MB)"
        self._hint.configure(text=label, text_color=C_TEXT)
        self._icon.configure(text="[ok]")
        self.configure(border_color=C_ACCENT)
        self._on_file(path)

    @property
    def file(self): return self._file


# =============================================================================
# Main app
# =============================================================================

def _make_root():
    if _HAS_DND:
        root = TkinterDnD.Tk()
        ctk.set_appearance_mode("dark")
        root.configure(bg=C_BG)
        return root
    else:
        return ctk.CTk()


class SaveManagerApp:
    def __init__(self):
        self.root = _make_root()
        self._bin_path  = None
        self._game_dir  = None
        self._running   = False
        self._build_ui()

    def _build_ui(self):
        r = self.root
        r.title("Minecraft LCE Save Manager")
        r.geometry("680x720")
        r.resizable(False, False)
        r.configure(bg=C_BG)

        # title
        header = ctk.CTkFrame(r, fg_color=C_BG, corner_radius=0)
        header.pack(fill='x', padx=28, pady=(24, 0))

        ctk.CTkLabel(
            header, text="Minecraft LCE",
            font=("Segoe UI", 22, "bold"), text_color=C_TEXT,
        ).pack(side='left')
        ctk.CTkLabel(
            header, text="Save Manager",
            font=("Segoe UI", 22), text_color=C_SUBTEXT,
        ).pack(side='left', padx=(6, 0))
        ctk.CTkLabel(
            header, text="Xbox 360 -> Windows 64",
            font=FONT_SMALL, text_color=C_SUBTEXT,
        ).pack(side='right', pady=(8, 0))

        ctk.CTkFrame(r, height=1, fg_color=C_STROKE, corner_radius=0).pack(
            fill='x', padx=28, pady=(14, 0))

        # warning banner
        banner = ctk.CTkFrame(r, fg_color=C_WARN_BG, corner_radius=8)
        banner.pack(fill='x', padx=28, pady=(16, 0))
        ctk.CTkLabel(
            banner,
            text="[!]  TU19 or older saves are recommended.  "
                 "Later saves may not load correctly in the TU19 dev build.",
            font=FONT_SMALL, text_color=C_WARN_FG,
            wraplength=600, justify='left',
        ).pack(padx=14, pady=8, anchor='w')

        # section 1: bin file
        self._section_label(r, "1   XBOX 360 SAVE FILE  (.bin)")
        self._drop = DropZone(r, on_file=self._on_bin_selected, height=140)
        self._drop.pack(fill='x', padx=28)
        self._bin_status = ctk.CTkLabel(r, text="", font=FONT_SMALL, text_color=C_SUBTEXT)
        self._bin_status.pack(anchor='w', padx=30, pady=(4, 0))

        # section 2: output dir
        self._section_label(r, "2   GAMEHDD FOLDER")
        dir_row = ctk.CTkFrame(r, fg_color=C_SURFACE, corner_radius=8,
                               border_width=1, border_color=C_STROKE)
        dir_row.pack(fill='x', padx=28)
        dir_row.columnconfigure(0, weight=1)

        self._dir_entry = ctk.CTkEntry(
            dir_row, placeholder_text="...\\Windows64\\GameHDD",
            font=FONT_BODY, fg_color="transparent", border_width=0, text_color=C_TEXT,
        )
        self._dir_entry.grid(row=0, column=0, sticky='ew', padx=(12, 4), pady=8)

        ctk.CTkButton(
            dir_row, text="Browse", font=FONT_SMALL, width=80,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H, corner_radius=6,
            command=self._browse_game_dir,
        ).grid(row=0, column=1, padx=(4, 10), pady=8)

        ctk.CTkLabel(
            r, text="[i]  Point this to the GameHDD folder - save goes in  GameHDD\\<save name>\\saveData.ms",
            font=FONT_SMALL, text_color=C_SUBTEXT,
        ).pack(anchor='w', padx=30, pady=(4, 0))

        # convert button
        self._convert_btn = ctk.CTkButton(
            r, text="Convert & Install",
            font=("Segoe UI", 13, "bold"), height=44,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H, corner_radius=8,
            state='disabled', command=self._start_conversion,
        )
        self._convert_btn.pack(fill='x', padx=28, pady=(22, 0))

        # progress bar
        self._progress = ctk.CTkProgressBar(
            r, mode='indeterminate', height=4,
            fg_color=C_STROKE, progress_color=C_ACCENT, corner_radius=0,
        )
        self._progress.pack(fill='x', padx=28, pady=(6, 0))
        self._progress.set(0)

        # log area
        self._section_label(r, "LOG")
        self._log = ctk.CTkTextbox(
            r, font=FONT_MONO, fg_color=C_SURFACE,
            border_color=C_STROKE, border_width=1, corner_radius=8,
            text_color="#b0c8d8", state='disabled', height=160,
        )
        self._log.pack(fill='both', expand=True, padx=28, pady=(0, 24))
        self._log_write("Ready.  Drop a .bin file above to get started.\n")

    def _section_label(self, parent, text: str):
        ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 10, "bold"), text_color=C_SUBTEXT,
        ).pack(anchor='w', padx=30, pady=(18, 6))

    # -- event handlers --

    def _on_bin_selected(self, path: str):
        self._bin_path = path
        sz = os.path.getsize(path)
        self._bin_status.configure(
            text=f"Selected: {Path(path).name}  ({sz/1_048_576:.2f} MB)",
            text_color=C_SUCCESS,
        )
        self._check_ready()

    def _browse_game_dir(self):
        p = filedialog.askdirectory(title="Select the GameHDD folder  (e.g. ...\\Windows64\\GameHDD)")
        if p:
            self._game_dir = p
            self._dir_entry.delete(0, 'end')
            self._dir_entry.insert(0, p)
            self._check_ready()

    def _check_ready(self):
        typed = self._dir_entry.get().strip()
        if typed:
            self._game_dir = typed
        ready = bool(self._bin_path) and bool(self._game_dir) and not self._running
        self._convert_btn.configure(state='normal' if ready else 'disabled')

    def _start_conversion(self):
        typed = self._dir_entry.get().strip()
        if typed:
            self._game_dir = typed
        if not self._bin_path or not self._game_dir:
            return

        self._running = True
        self._convert_btn.configure(state='disabled', text="Converting...")
        self._progress.start()
        self._log_clear()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            out_dir = convert_bin_to_win64(
                self._bin_path, self._game_dir, log=self._log_write)
            self.root.after(0, self._on_success, out_dir)
        except Exception as exc:
            self.root.after(0, self._on_error, str(exc))

    def _on_success(self, out_dir: str):
        self._progress.stop()
        self._progress.set(1)
        self._running = False
        self._convert_btn.configure(
            state='normal', text="[ok]  Done - Convert another",
            fg_color=C_SUCCESS, hover_color="#3d9e6a",
        )
        self._log_write(f"\n[ok]  Save installed to:\n   {out_dir}\n")

    def _on_error(self, msg: str):
        self._progress.stop()
        self._progress.set(0)
        self._running = False
        self._convert_btn.configure(
            state='normal', text="Convert & Install",
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
        )
        self._log_write(f"\nError: {msg}\n")

    # -- log helpers --

    def _log_write(self, text: str):
        def _do():
            self._log.configure(state='normal')
            self._log.insert('end', text + ("" if text.endswith('\n') else '\n'))
            self._log.see('end')
            self._log.configure(state='disabled')
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.root.after(0, _do)

    def _log_clear(self):
        self._log.configure(state='normal')
        self._log.delete('1.0', 'end')
        self._log.configure(state='disabled')

    # -- run --

    def run(self):
        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.root.after(200, lambda: self._drop._set_file(sys.argv[1]))
        self.root.mainloop()


if __name__ == '__main__':
    SaveManagerApp().run()
