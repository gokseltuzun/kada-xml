#!/usr/bin/env python3
"""KADA XML - Premiere -> FCPX: small window app (tkinter, standard library only)."""
import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import premiere_to_fcpxml as core


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"KADA XML  v{core.__version__}  ·  Premiere → Final Cut Pro X")
        self.minsize(640, 0)
        try:  # window icon (works from source and from the PyInstaller bundle)
            base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            self._icon = tk.PhotoImage(file=os.path.join(base, 'assets', 'icon.png'))
            self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.src = tk.StringVar()
        self.dst = tk.StringVar()
        self.frm = tk.StringVar()
        self.to = tk.StringVar()

        pad = dict(padx=8, pady=4)
        f = ttk.Frame(self, padding=12)
        f.grid(sticky='nsew')
        self.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="1. Premiere XML\n(File → Export → Final Cut Pro XML)").grid(row=0, column=0, sticky='w', **pad)
        ttk.Entry(f, textvariable=self.src).grid(row=0, column=1, sticky='ew', **pad)
        ttk.Button(f, text="Seç / Browse…", command=self.pick_src).grid(row=0, column=2, **pad)

        ttk.Label(f, text="2. Medya klasörü (XML'deki)\nMedia folder (in XML)").grid(row=1, column=0, sticky='w', **pad)
        ttk.Entry(f, textvariable=self.frm).grid(row=1, column=1, columnspan=2, sticky='ew', **pad)

        ttk.Label(f, text="3. Mac'teki yeri (boş = aynı kalsın)\nOn the Mac (empty = keep)").grid(row=2, column=0, sticky='w', **pad)
        ttk.Entry(f, textvariable=self.to).grid(row=2, column=1, sticky='ew', **pad)
        ttk.Button(f, text="Seç / Browse…", command=self.pick_to).grid(row=2, column=2, **pad)
        ttk.Label(f, text="örn. / e.g.  /Volumes/MyDisk/Weddings", foreground='gray').grid(
            row=3, column=1, sticky='w', padx=8)

        ttk.Label(f, text="4. Çıkış / Output .fcpxml").grid(row=4, column=0, sticky='w', **pad)
        ttk.Entry(f, textvariable=self.dst).grid(row=4, column=1, sticky='ew', **pad)
        ttk.Button(f, text="Seç / Browse…", command=self.pick_dst).grid(row=4, column=2, **pad)

        ttk.Button(f, text="Dönüştür / Convert", command=self.run).grid(
            row=5, column=0, columnspan=3, sticky='ew', padx=8, pady=10, ipady=6)

        self.log = tk.Text(f, height=9, width=80, state='disabled', wrap='word')
        self.log.grid(row=6, column=0, columnspan=3, sticky='nsew', **pad)
        f.rowconfigure(6, weight=1)
        self.rowconfigure(0, weight=1)

        self.say("Premiere'de sekansı seç → File → Export → Final Cut Pro XML.\n"
                 "Sonra o XML'i burada seç ve Dönüştür'e bas. FCPX'te: File → Import → XML.\n"
                 "Taşınır: kesimler, track'ler, boşluklar, ses seviyesi. Taşınmaz: efekt, renk, geçiş, yazı.\n")

    def say(self, text, clear=False):
        self.log.configure(state='normal')
        if clear:
            self.log.delete('1.0', 'end')
        self.log.insert('end', text + '\n')
        self.log.configure(state='disabled')
        self.log.see('end')

    def pick_src(self):
        p = filedialog.askopenfilename(filetypes=[("Premiere FCP XML", "*.xml"), ("All", "*.*")])
        if not p:
            return
        self.src.set(p)
        self.dst.set(os.path.splitext(p)[0] + '.fcpxml')
        try:
            self.frm.set(core.detect_media_root(p))
        except core.ConvertError as e:
            self.say(f"HATA / ERROR: {e}")

    def pick_to(self):
        p = filedialog.askdirectory()
        if p:
            self.to.set(p)

    def pick_dst(self):
        p = filedialog.asksaveasfilename(defaultextension='.fcpxml',
                                         filetypes=[("FCPXML", "*.fcpxml")])
        if p:
            self.dst.set(p)

    def run(self):
        src, dst = self.src.get().strip(), self.dst.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showwarning("KADA XML", "Önce XML dosyasını seç.\nPick the XML file first.")
            return
        if not dst:
            dst = os.path.splitext(src)[0] + '.fcpxml'
            self.dst.set(dst)
        try:
            r = core.convert(src, dst, self.frm.get().strip() or None, self.to.get().strip() or None)
        except core.ConvertError as e:
            self.say(f"HATA / ERROR: {e}", clear=True)
            return
        except Exception as e:  # unexpected: show, don't crash
            self.say(f"Beklenmeyen hata / unexpected error: {e!r}", clear=True)
            return
        self.say("Tamam / Done\n" + core.format_report(r), clear=True)
        if messagebox.askyesno("KADA XML", "Hazır. Klasörü açayım mı?\nDone. Open the folder?"):
            folder = os.path.dirname(os.path.abspath(dst))
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.run(['open', '-R', dst])
            else:
                subprocess.run(['xdg-open', folder])


if __name__ == '__main__':
    App().mainloop()
