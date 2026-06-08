# -*- coding: utf-8 -*-
"""
Tkinter GUI for Zotero2Github.

Point it at a Zotero export folder (the one holding your .bib and files/) and a
copy of zotero.sqlite, then click Sort. It rebuilds the Collections/ tree exactly
like scripts/zotero_sync.py, but with a window instead of the command line.
After sorting it prints the exact git commands (or website steps) to put the
folder on GitHub — it does not run git for you.

Run:
    python zotero_gui.py

Tkinter ships with CPython, so the GUI runs with no install. For drag-and-drop
of folders/files onto the fields, install the optional extra:
    pip install tkinterdnd2
Without it the GUI still works fully through the Browse buttons.
"""
import os
import sys
import glob
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Import the sorting core from zotero_sync (same folder).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zotero_sync

# Optional drag-and-drop support. If tkinterdnd2 is missing, fall back to a
# plain Tk root and the Browse buttons still cover everything.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _BASE = TkinterDnD.Tk
    HAVE_DND = True
except Exception:
    _BASE = tk.Tk
    DND_FILES = None
    HAVE_DND = False


def zotero_data_dir():
    """Default Zotero data folder, where zotero.sqlite normally lives."""
    return os.path.join(os.path.expanduser("~"), "Zotero")


def default_db():
    """Best guess at the local zotero.sqlite path."""
    guess = os.path.join(zotero_data_dir(), "zotero.sqlite")
    return guess if os.path.isfile(guess) else ""


# Color palette (v4 — sober neutral black, single cyan accent).
BG = "#0a0a0b"          # page background (near-black)
CARD = "#141416"        # card surface
INK = "#e8e8ea"         # primary text
MUTED = "#8a8a90"       # secondary text
ACCENT = "#22b8cf"      # calm cyan
ACCENT_DK = "#1a96aa"
ACCENT_SOFT = "#16313a"  # soft button bg
ACCENT_TXT = "#5fd3e6"   # accent text / title
BAND = "#0e0e10"        # header band
HEAD_SUB = "#8a8a90"
BORDER = "#262629"
CHIP = "#222226"        # neutral button bg
CHIP_HI = "#303036"     # neutral button hover
FIELD = "#0e0e10"       # entry background
STATUSBG = "#0c0c0e"
CONSOLE = "#0b0b0d"     # log console bg


class App(_BASE):
    def __init__(self):
        super().__init__()
        self.title("Zotero2Github")
        self.minsize(760, 620)
        self.configure(bg=BG)

        self.repo = tk.StringVar()
        self.db = tk.StringVar(value=default_db())
        self.bib = tk.StringVar()  # empty = auto-detect
        self.output = tk.StringVar(value="(choose an export folder)")
        self.status = tk.StringVar(value="Ready.")
        self.do_backup = tk.BooleanVar(value=False)

        self.repo.trace_add("write", self._update_output)

        self._log_q = queue.Queue()
        self._busy = False

        self._make_styles()
        self._build()
        self._size_to_content()
        self.after(100, self._drain_log)

    def _size_to_content(self):
        """Open as tall as the content needs, capped to the screen height.

        Avoids the log being squeezed off-screen on first launch; the log card
        is the one that expands, so any extra room goes to it.
        """
        self.update_idletasks()
        sh = self.winfo_screenheight()
        need = self.winfo_reqheight()
        h = min(need + 8, int(sh * 0.90))
        self.geometry(f"920x{max(h, 620)}")

    # ---------- theming ----------
    def _make_styles(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=INK)
        st.configure("Card.TLabel", background=CARD, foreground=INK,
                     font=("Segoe UI", 10))
        st.configure("CardTitle.TLabel", background=CARD, foreground=INK,
                     font=("Segoe UI Semibold", 11))
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED,
                     font=("Segoe UI", 9))
        st.configure("Status.TLabel", background=STATUSBG, foreground=MUTED,
                     font=("Segoe UI", 9))
        st.configure("TCheckbutton", background=CARD, foreground=INK)
        st.map("TCheckbutton", background=[("active", CARD)])
        st.configure("TEntry", fieldbackground=FIELD, foreground=INK,
                     bordercolor=BORDER, insertcolor=INK, padding=4)
        st.map("TEntry",
               bordercolor=[("focus", ACCENT)],
               fieldbackground=[("readonly", FIELD)],
               foreground=[("readonly", MUTED)])
        # Buttons
        st.configure("Accent.TButton", background=ACCENT, foreground="#06222b",
                     font=("Segoe UI Semibold", 11), padding=(26, 10),
                     borderwidth=0, focusthickness=0)
        st.map("Accent.TButton",
               background=[("active", ACCENT_DK), ("disabled", "#1e3a44")],
               foreground=[("disabled", "#64748b")])
        st.configure("Soft.TButton", background=ACCENT_SOFT, foreground=ACCENT_TXT,
                     font=("Segoe UI", 10), padding=(14, 8), borderwidth=0)
        st.map("Soft.TButton", background=[("active", "#155e75")])
        st.configure("Browse.TButton", background=CHIP, foreground=INK,
                     padding=(12, 6), borderwidth=0)
        st.map("Browse.TButton", background=[("active", CHIP_HI)])
        st.configure("Link.TButton", background=CHIP, foreground=INK,
                     padding=(10, 5), borderwidth=0)
        st.map("Link.TButton", background=[("active", CHIP_HI)])

    # ---------- card helper ----------
    def _card(self, num, title, subtitle="", expand=False):
        """A bordered card with a round number badge + title. Return body."""
        outer = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        outer.pack(fill="both" if expand else "x", expand=expand,
                   padx=20, pady=6)

        header = tk.Frame(outer, bg=CARD)
        header.pack(fill="x", padx=14, pady=(10, 0))
        # Filled circled-number glyph (❶❷❸): crisp, anti-aliased by the font
        # engine, unlike a hand-drawn Canvas oval. fg tints the disc; the digit
        # is knocked out to the card colour.
        glyph = chr(0x2775 + num) if 1 <= num <= 9 else str(num)
        tk.Label(header, text=glyph, bg=CARD, fg=ACCENT,
                 font=("Segoe UI", 20)).pack(side="left")
        ttk.Label(header, text=title, style="CardTitle.TLabel").pack(
            side="left", padx=(8, 0))
        if subtitle:
            ttk.Label(header, text=subtitle, style="Muted.TLabel").pack(
                side="left", padx=2)

        body = ttk.Frame(outer, style="Card.TFrame")
        body.pack(fill="both", expand=expand, padx=14, pady=(4, 12))
        body.columnconfigure(1, weight=1)
        return body

    # ---------- layout ----------
    def _build(self):
        # Header band (full-width dark slate)
        band = tk.Frame(self, bg=BAND)
        band.pack(fill="x")
        inner = tk.Frame(band, bg=BAND)
        inner.pack(fill="x", padx=20, pady=(14, 12))
        tk.Label(inner, text="Zotero2Github", bg=BAND, fg=ACCENT_TXT,
                 font=("Segoe UI Semibold", 18)).pack(anchor="w")
        sub = "Sort your Zotero export into collection folders, ready to push."
        if HAVE_DND:
            sub += "   Drag items onto a field, or use Browse."
        tk.Label(inner, text=sub, bg=BAND, fg=HEAD_SUB,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 0))
        # thin cyan accent line under the band
        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # Card 1 — Inputs
        inp = self._card(1, "Inputs", "what to sort")
        self._row(inp, 0, "Export folder", self.repo, self._pick_repo,
                  "the folder with your .bib and files/")
        self._row(inp, 1, "zotero.sqlite", self.db, self._pick_db,
                  "a copy of your Zotero database")
        self._row(inp, 2, ".bib (optional)", self.bib, self._pick_bib,
                  "leave empty to auto-detect")
        # Faithful-backup option
        chk = ttk.Checkbutton(
            inp, style="TCheckbutton", variable=self.do_backup,
            text="Also back up database + storage/ (lets you fully restore "
                 "Zotero later)")
        chk.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(inp, text="copies zotero.sqlite and your PDFs into "
                  "Zotero-backup/ — larger repo, but a complete restore.",
                  style="Muted.TLabel").grid(row=7, column=0, columnspan=3,
                                             sticky="w", pady=(0, 2))

        # Card 2 — Locations
        loc = self._card(2, "Where things are")
        self._path_bar(loc, 0, "Zotero data folder",
                       tk.StringVar(value=zotero_data_dir()),
                       "zotero.sqlite normally lives here")
        self._path_bar(loc, 1, "Sorted output", self.output,
                       "your Collections/ tree is built here")

        # Action bar
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=20, pady=(2, 6))
        self.sort_btn = ttk.Button(btns, text="Sort  →", style="Accent.TButton",
                                   command=self._run)
        self.sort_btn.pack(side="left")
        ttk.Button(btns, text="Clear log", style="Soft.TButton",
                   command=self._clear_log).pack(side="left", padx=8)
        ttk.Button(btns, text="Help: how to export", style="Soft.TButton",
                   command=self._show_help).pack(side="right")
        ttk.Button(btns, text="Restore…", style="Soft.TButton",
                   command=self._restore_dialog).pack(side="right", padx=8)

        # Card 3 — Log
        logc = self._card(3, "Log", "progress and next steps", expand=True)
        self.log = tk.Text(logc, height=12, wrap="word", state="disabled",
                           font=("Cascadia Mono", 10), bg=CONSOLE,
                           fg="#cbd5e1", insertbackground="#e2e8f0",
                           relief="flat", padx=12, pady=10,
                           spacing1=1, spacing3=3, tabs="2c")
        self.log.grid(row=0, column=0, columnspan=3, sticky="nsew")
        logc.rowconfigure(0, weight=1)
        # Syntax colours for the log lines (see _line_tag).
        self.log.tag_configure("head", foreground=ACCENT_TXT,
                               font=("Segoe UI Semibold", 10), spacing1=8)
        self.log.tag_configure("cmd", foreground="#5fe08a",
                               background="#15201a", lmargin1=24, lmargin2=24)
        self.log.tag_configure("err", foreground="#f87171",
                               font=("Cascadia Mono", 10, "bold"))
        self.log.tag_configure("ok", foreground="#34d399",
                               font=("Cascadia Mono", 10, "bold"))
        self.log.tag_configure("warn", foreground="#fbbf24")
        self.log.tag_configure("muted", foreground="#7c8aa5")
        self.log.tag_configure("step", foreground="#e2e8f0",
                               font=("Segoe UI", 10))

        # Status bar
        ttk.Label(self, textvariable=self.status, style="Status.TLabel",
                  anchor="w", padding=(20, 5)).pack(fill="x", side="bottom")

    def _row(self, parent, r, label, var, cmd, hint):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=r * 2, column=0, sticky="w", pady=(4, 0))
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=r * 2, column=1, sticky="ew", padx=8, pady=(4, 0))
        ttk.Button(parent, text="Browse…", style="Browse.TButton",
                   command=cmd).grid(row=r * 2, column=2, pady=(4, 0))
        ttk.Label(parent, text=hint, style="Muted.TLabel").grid(
            row=r * 2 + 1, column=1, sticky="w", padx=8, pady=(0, 4))
        if HAVE_DND:
            entry.drop_target_register(DND_FILES)
            entry.dnd_bind("<<Drop>>", lambda e, v=var: self._on_drop(e, v))

    def _path_bar(self, parent, r, label, var, hint):
        """A read-only, selectable path with Copy and Open buttons."""
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=r * 2, column=0, sticky="w", pady=(4, 0))
        ent = ttk.Entry(parent, textvariable=var, state="readonly")
        ent.grid(row=r * 2, column=1, sticky="ew", padx=8, pady=(4, 0))
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.grid(row=r * 2, column=2, sticky="e", pady=(4, 0))
        ttk.Button(bar, text="Copy", style="Link.TButton",
                   command=lambda v=var: self._copy(v.get())).pack(side="left")
        ttk.Button(bar, text="Open", style="Link.TButton",
                   command=lambda v=var: self._open(v.get())).pack(
                       side="left", padx=(4, 0))
        ttk.Label(parent, text=hint, style="Muted.TLabel").grid(
            row=r * 2 + 1, column=1, sticky="w", padx=8, pady=(0, 4))

    def _on_drop(self, event, var):
        """Set `var` to the first path dropped onto its field."""
        paths = self.tk.splitlist(event.data)  # handles {paths with spaces}
        if paths:
            var.set(paths[0])
        return event.action

    # ---------- path helpers ----------
    def _update_output(self, *_):
        repo = self.repo.get().strip()
        self.output.set(os.path.join(repo, "Collections")
                        if repo else "(choose an export folder)")

    def _copy(self, text):
        if not text or text.startswith("("):
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("Copied to clipboard: " + text)

    def _open(self, path):
        path = (path or "").strip()
        if not path or path.startswith("("):
            return
        target = path if os.path.exists(path) else os.path.dirname(path)
        if not target or not os.path.exists(target):
            self.status.set("Path does not exist yet: " + path)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(target)  # noqa: B606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            self.status.set("Opened: " + target)
        except Exception as ex:
            self.status.set("Could not open: " + str(ex))

    # ---------- help popup ----------
    def _show_help(self):
        win = tk.Toplevel(self)
        win.title("How to export from Zotero")
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)

        steps = ("1.  In Zotero: File › Export Library…\n"
                 "2.  Format BibLaTeX, tick Export Files (Include Annotations\n"
                 "     and Use Journal Abbreviation are optional).\n"
                 "3.  Click OK and save into a new, empty folder.\n\n"
                 "Then point this app at that folder.")
        ttk.Label(win, text=steps, style="Card.TLabel",
                  justify="left", font=("Segoe UI", 10)).pack(
                      anchor="w", padx=16, pady=(14, 8))

        # PNG lives in ../docs relative to this script.
        img_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "zotero-export.png")
        if os.path.isfile(img_path):
            try:
                self._help_img = tk.PhotoImage(file=img_path)  # keep ref
                tk.Label(win, image=self._help_img, bg=CARD).pack(
                    padx=16, pady=4)
            except Exception as ex:
                ttk.Label(win, text="(could not load image: %s)" % ex,
                          style="Muted.TLabel").pack(padx=16)
        else:
            ttk.Label(win, text="(image not found: docs/zotero-export.png)",
                      style="Muted.TLabel").pack(padx=16)

        ttk.Button(win, text="Got it", style="Accent.TButton",
                   command=win.destroy).pack(pady=(6, 14))

        win.update_idletasks()
        # Center over the main window.
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + 40
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        win.focus_set()

    # ---------- pickers ----------
    def _pick_repo(self):
        d = filedialog.askdirectory(title="Select the Zotero export folder")
        if d:
            self.repo.set(d)
            if not self.bib.get():
                cands = sorted(glob.glob(os.path.join(d, "*.bib")))
                if cands:
                    self._emit("Detected .bib: " + os.path.basename(cands[0]))

    def _pick_db(self):
        f = filedialog.askopenfilename(
            title="Select zotero.sqlite",
            filetypes=[("SQLite database", "*.sqlite"), ("All files", "*.*")])
        if f:
            self.db.set(f)

    def _pick_bib(self):
        start = self.repo.get() or os.path.expanduser("~")
        f = filedialog.askopenfilename(
            title="Select the .bib file", initialdir=start,
            filetypes=[("BibLaTeX", "*.bib"), ("All files", "*.*")])
        if f:
            self.bib.set(f)

    # ---------- logging ----------
    def _emit(self, msg):
        self._log_q.put(msg)

    @staticmethod
    def _line_tag(msg):
        """Pick a colour tag for a log line based on its shape."""
        s = msg.strip()
        if not s:
            return None
        if s.startswith("ERROR") or s.startswith("Failed"):
            return "err"
        if s.startswith("---") and s.endswith("---"):
            return "head"
        if s.lstrip().startswith(("git ", "$ git", "pip ")):
            return "cmd"
        if s in ("All done.", "Done.") or s.startswith("Done."):
            return "ok"
        if s.startswith("!") or "missing source" in s or "already sorted" in s:
            return "warn"
        if " : " in s or s.rstrip().endswith(":"):
            return "muted"
        if s[:2] in ("1.", "2.", "3.") or s[:3].strip().rstrip(".").isdigit():
            return "step"
        return None

    def _drain_log(self):
        while True:
            try:
                msg = self._log_q.get_nowait()
            except queue.Empty:
                break
            tag = self._line_tag(msg)
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n", tag if tag else ())
            self.log.see("end")
            self.log.config(state="disabled")
        self.after(100, self._drain_log)

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ---------- run ----------
    def _run(self):
        if self._busy:
            return
        repo = self.repo.get().strip()
        db = self.db.get().strip()
        bib = self.bib.get().strip() or None
        if not repo:
            messagebox.showerror("Missing input", "Choose the export folder first.")
            return
        if not db:
            messagebox.showerror("Missing input", "Choose your zotero.sqlite copy.")
            return

        # If bib was picked as a full path outside repo logic, pass just its name
        # when it lives in the repo; otherwise pass the absolute path through.
        if bib and os.path.isabs(bib):
            try:
                if os.path.dirname(os.path.abspath(bib)) == os.path.abspath(repo):
                    bib = os.path.basename(bib)
            except Exception:
                pass

        do_backup = self.do_backup.get()

        self._busy = True
        self.sort_btn.config(state="disabled", text="Sorting…")
        self.status.set("Sorting…")
        t = threading.Thread(target=self._work, args=(repo, db, bib, do_backup),
                             daemon=True)
        t.start()

    def _set_status(self, text):
        self.after(0, lambda: self.status.set(text))

    def _work(self, repo, db, bib, do_backup):
        try:
            self._emit("--- Sorting ---")
            zotero_sync.rebuild(repo, db, bib, log=self._emit)
            self._emit("All done.")
            if do_backup:
                self._emit("")
                self._emit("--- Faithful backup ---")
                dest = os.path.join(os.path.abspath(repo),
                                    zotero_sync.BACKUP_DIRNAME)
                zotero_sync.backup_data_dir(db, dest, log=self._emit)
            self._next_steps(repo)
            self._set_status("Done. Sorted folders in " +
                             os.path.join(repo, "Collections"))
        except Exception as ex:
            self._emit("ERROR: " + str(ex))
            self._set_status("Failed: " + str(ex))
        finally:
            self._busy = False
            self.after(0, lambda: self.sort_btn.config(state="normal", text="Sort  →"))

    def _next_steps(self, repo):
        """Tell the user how to put the sorted folder on GitHub. No git is run."""
        has_git = os.path.isdir(os.path.join(repo, ".git"))
        self._emit("")
        self._emit("--- Next: put it on GitHub ---")
        if has_git:
            self._emit("This folder is already a git repo. To upload the changes,")
            self._emit("open a terminal here and run:")
            self._emit("")
            self._emit('    git add -A')
            self._emit('    git commit -m "Update Zotero library"')
            self._emit('    git push')
        else:
            self._emit("1. Create a PRIVATE repo on the website: "
                       "https://github.com/new")
            self._emit("   (research PDFs are usually copyrighted — keep it private).")
            self._emit("2. Open a terminal in this folder:")
            self._emit("   " + repo)
            self._emit("3. Run, replacing YOU/REPO with your repo:")
            self._emit("")
            self._emit('    git init')
            self._emit('    git branch -M main')
            self._emit('    git add -A')
            self._emit('    git commit -m "My Zotero library"')
            self._emit('    git remote add origin '
                       'https://github.com/YOU/REPO.git')
            self._emit('    git push -u origin main')
        self._emit("")
        self._emit("Prefer no terminal? Use GitHub Desktop, or drag the files into")
        self._emit("the repo's 'Add file > Upload files' page on github.com.")

    # ---------- restore ----------
    def _restore_dialog(self):
        """Restore a Zotero data directory from a Zotero-backup/ folder.

        This overwrites the target's zotero.sqlite and storage/, so it asks
        twice and refuses while Zotero holds the database open. The previous
        files are renamed to *.old-N, never deleted.
        """
        if self._busy:
            return
        win = tk.Toplevel(self)
        win.title("Restore from backup")
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)

        backup = tk.StringVar()
        target = tk.StringVar(value=zotero_data_dir())

        ttk.Label(win, text="Restore a full Zotero library from a backup",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 2))
        warn = ("This replaces the target zotero.sqlite and storage/ with the "
                "backup.\nClose Zotero first. Your current files are kept as "
                "*.old-N (not deleted),\nso you can undo by hand.")
        ttk.Label(win, text=warn, style="Muted.TLabel", justify="left").pack(
            anchor="w", padx=16, pady=(0, 10))

        form = ttk.Frame(win, style="Card.TFrame")
        form.pack(fill="x", padx=16)
        form.columnconfigure(1, weight=1)

        def field(r, label, var, picker, hint):
            ttk.Label(form, text=label, style="Card.TLabel").grid(
                row=r * 2, column=0, sticky="w", pady=(4, 0))
            ttk.Entry(form, textvariable=var, width=46).grid(
                row=r * 2, column=1, sticky="ew", padx=8, pady=(4, 0))
            ttk.Button(form, text="Browse…", style="Browse.TButton",
                       command=picker).grid(row=r * 2, column=2, pady=(4, 0))
            ttk.Label(form, text=hint, style="Muted.TLabel").grid(
                row=r * 2 + 1, column=1, sticky="w", padx=8, pady=(0, 4))

        def pick_backup():
            d = filedialog.askdirectory(
                title="Select the Zotero-backup folder (from your cloned repo)")
            if d:
                backup.set(d)

        def pick_target():
            d = filedialog.askdirectory(title="Select the Zotero data folder")
            if d:
                target.set(d)

        field(0, "Backup folder", backup, pick_backup,
              "the 'Zotero-backup' folder inside your cloned repo")
        field(1, "Zotero data folder", target, pick_target,
              "where zotero.sqlite will be written (close Zotero first)")

        btns = ttk.Frame(win, style="Card.TFrame")
        btns.pack(fill="x", padx=16, pady=14)
        ttk.Button(btns, text="Cancel", style="Soft.TButton",
                   command=win.destroy).pack(side="right")

        def go():
            b, t = backup.get().strip(), target.get().strip()
            if not b or not os.path.isfile(os.path.join(b, "zotero.sqlite")):
                messagebox.showerror(
                    "Restore", "Pick a backup folder that contains zotero.sqlite.",
                    parent=win)
                return
            if not t:
                messagebox.showerror("Restore", "Pick the Zotero data folder.",
                                     parent=win)
                return
            if not messagebox.askyesno(
                    "Overwrite Zotero library?",
                    "This will overwrite the library in:\n\n%s\n\n"
                    "Make sure Zotero is closed. Continue?" % t,
                    icon="warning", parent=win):
                return
            win.destroy()
            self._busy = True
            self.status.set("Restoring…")
            threading.Thread(target=self._restore_work, args=(b, t),
                             daemon=True).start()

        ttk.Button(btns, text="Restore (overwrite)", style="Accent.TButton",
                   command=go).pack(side="right", padx=8)

        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + 60
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        win.grab_set()
        win.focus_set()

    def _restore_work(self, backup, target):
        try:
            self._emit("")
            self._emit("--- Restore ---")
            moved = zotero_sync.restore_data_dir(backup, target, log=self._emit)
            self._set_status("Restore done. Reopen Zotero.")
            if moved:
                self._emit("Old files you can delete once Zotero opens fine:")
                for m in moved:
                    self._emit("   " + m)
        except Exception as ex:
            self._emit("ERROR: " + str(ex))
            self._set_status("Restore failed: " + str(ex))
        finally:
            self._busy = False


if __name__ == "__main__":
    App().mainloop()
