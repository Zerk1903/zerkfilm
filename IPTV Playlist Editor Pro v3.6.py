#!/usr/bin/env python3
import re, tkinter as tk, threading, subprocess, sys, os, platform
from tkinter import filedialog, messagebox, ttk, simpledialog
import urllib.request

THEMES = {
    "Matrix": {"bg": "#000000", "fg": "#00FF41", "sel": "#003B00", "font": "Courier New"},
    "Karanlık": {"bg": "#1e1e1e", "fg": "#ffffff", "sel": "#4f8ef7", "font": "Arial"},
    "Klasik": {"bg": "#f0f0f0", "fg": "#000000", "sel": "#0078d7", "font": "Segoe UI"}
}

class IPTVEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("Zerk IPTV Playlist Editor Pro v3.6")
        self.root.geometry("1400x800")
        
        self.all_channels = []
        self.active_entry = None
        self.sort_reverse = False
        self._click_timer = None   # tek/çift tık ayrımı için
        
        self._build_ui()
        self.apply_theme("Matrix")

    def _build_ui(self):
        self.tb = tk.Frame(self.root, height=60)
        self.tb.pack(fill="x", side="top")
        
        btn_data = [
            ("📁 AÇ", self.open_file), ("🌐 URL", self.open_url),
            ("💾 KAYDET", self.save_file), ("➕ EKLE", self.add_row_top),
            ("🗑️ SİL", self.delete_row)
        ]
        
        self.btns = []
        for txt, cmd in btn_data:
            b = tk.Button(self.tb, text=txt, command=cmd, relief="flat", padx=10, font=("Arial", 9, "bold"))
            b.pack(side="left", padx=5, pady=10)
            self.btns.append(b)

        self.btn_check = tk.Button(self.tb, text="✅ LİNKLERİ KONTROL ET", command=self.start_link_checker, bg="#2c3e50", fg="white", relief="flat")
        self.btn_check.pack(side="left", padx=20)
        
        self.btn_dup = tk.Button(self.tb, text="🧹 AYNI OLANLARI SİL", command=self.remove_duplicates, bg="#c0392b", fg="white", relief="flat")
        self.btn_dup.pack(side="left", padx=5)

        self.theme_var = tk.StringVar(value="Matrix")
        self.theme_combo = ttk.Combobox(self.tb, textvariable=self.theme_var, values=list(THEMES.keys()), state="readonly", width=10)
        self.theme_combo.pack(side="right", padx=10)
        self.theme_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_theme(self.theme_var.get()))

        search_frame = tk.Frame(self.root, bg="#111")
        search_frame.pack(fill="x", padx=10, pady=5)
        tk.Label(search_frame, text="🔍 HIZLI ARA:", fg="white", bg="#111", font=("Arial", 10, "bold")).pack(side="left", padx=5)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.filter_list())
        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var, bg="#222", fg="white", insertbackground="white", bd=0, font=("Arial", 11))
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=5)

        self.tree_frame = tk.Frame(self.root)
        self.tree_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("logo", "name", "group", "url", "status")
        self.tree = ttk.Treeview(self.tree_frame, columns=columns, show="headings", selectmode="extended")
        
        for col, head in zip(columns, ["LOGO URL", "KANAL ADI", "GRUP", "YAYIN ADRESİ", "DURUM"]):
            self.tree.heading(col, text=head, command=lambda c=col: self.sort_by_column(c))
        
        self.tree.column("logo", width=120)
        self.tree.column("name", width=200)
        self.tree.column("group", width=120)
        self.tree.column("url", width=400)
        self.tree.column("status", width=80, anchor="center")

        self.tree.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(self.tree_frame, orient="vertical", command=self.tree.yview).pack(side="right", fill="y")

        # ── Mouse bağlamaları ──────────────────────────────────────────
        # Tek tık → hücre düzenleme (çift tık gelirse iptal edilir)
        self.tree.bind("<Button-1>", self.on_single_click)
        # Çift tık → düzenlemeyi iptal et (yanlışlıkla açılmasın)
        self.tree.bind("<Double-Button-1>", self.on_double_click)
        # Sağ tık → context menü
        self.tree.bind("<Button-3>", self.show_context_menu)

        # Context menü
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="▶ Önizle (Oynat)", command=self.preview_stream)
        self.menu.add_separator()
        self.menu.add_command(label="Kopyala (URL)", command=self.copy_text)
        self.menu.add_command(label="Seçili Olanları Gruba Taşı", command=self.bulk_group_change)
        self.menu.add_separator()
        self.menu.add_command(label="Seçili Satırları Sil", command=self.delete_row)

        # Durum çubuğu
        self.status_bar = tk.Label(self.root, text="Hazır  |  İpucu: Çift tıkla hücreyi düzenle · Sağ tık → Önizle",
                                   anchor="w", padx=8, font=("Arial", 9))
        self.status_bar.pack(fill="x", side="bottom")

    # ── Önizleme ──────────────────────────────────────────────────────

    def preview_stream(self):
        """Seçili satırın URL'sini sistem oynatıcısıyla / VLC ile açar."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Uyarı", "Lütfen önce bir kanal seçin.")
            return
        url = self.tree.item(sel[0])['values'][3]
        if not url or url == "http://":
            messagebox.showwarning("Uyarı", "Geçerli bir URL bulunamadı.")
            return

        self.status_bar.config(text=f"▶ Oynatılıyor: {url[:80]}...")

        def _play():
            system = platform.system()
            players = []
            if system == "Windows":
                players = [
                    ["vlc", "--one-instance", url],
                    ["C:\\Program Files\\VideoLAN\\VLC\\vlc.exe", url],
                    ["C:\\Program Files (x86)\\VideoLAN\\VLC\\vlc.exe", url],
                ]
                # Hiçbiri bulunamazsa Windows varsayılanıyla aç
                fallback = lambda: os.startfile(url)
            elif system == "Darwin":  # macOS
                players = [["vlc", url], ["iina", url]]
                fallback = lambda: subprocess.Popen(["open", url])
            else:  # Linux
                players = [["vlc", url], ["mpv", url], ["mplayer", url], ["totem", url]]
                fallback = lambda: subprocess.Popen(["xdg-open", url])

            launched = False
            for cmd in players:
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    launched = True
                    break
                except (FileNotFoundError, OSError):
                    continue

            if not launched:
                try:
                    fallback()
                    launched = True
                except Exception as e:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Hata",
                        f"Oynatıcı bulunamadı.\nVLC veya mpv yüklü olduğundan emin olun.\n\nURL:\n{url}"
                    ))

        threading.Thread(target=_play, daemon=True).start()

    # ── Mouse tıklama ─────────────────────────────────────────────────

    def on_single_click(self, event):
        """Tek tıkta seçim yap, 250ms sonra hücreyi düzenle."""
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        # Aktif entry varsa önce kapat
        if self.active_entry:
            try:
                self.active_entry.event_generate("<FocusOut>")
            except Exception:
                pass

        column = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item or not column:
            return

        if self._click_timer:
            self.root.after_cancel(self._click_timer)
            self._click_timer = None

        # 250ms bekle; bu süre içinde çift tık gelirse timer iptal olur
        self._click_timer = self.root.after(
            250, lambda: self.edit_cell(item, column)
        )

    def on_double_click(self, event):
        """Çift tıkta bekleyen tek-tık düzenlemesini iptal et (seçim yeterli)."""
        if self._click_timer:
            self.root.after_cancel(self._click_timer)
            self._click_timer = None

    # ── Sütun sıralama ────────────────────────────────────────────────

    def sort_by_column(self, col):
        data = [(self.tree.set(child, col), child) for child in self.tree.get_children('')]
        data.sort(reverse=self.sort_reverse)
        for index, (val, child) in enumerate(data):
            self.tree.move(child, '', index)
        self.sort_reverse = not self.sort_reverse
        self.update_all_channels_from_tree()

    # ── Link kontrolü ─────────────────────────────────────────────────

    def start_link_checker(self):
        threading.Thread(target=self.link_checker_worker, daemon=True).start()

    def link_checker_worker(self):
        items = self.tree.get_children()
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        for item in items:
            url = self.tree.item(item)['values'][3]
            status = "OFFLINE"
            try:
                req = urllib.request.Request(url, headers=headers, method='HEAD')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200: status = "ONLINE"
            except:
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status == 200: status = "ONLINE"
                except:
                    status = "OFFLINE"
            self.root.after(0, lambda i=item, s=status: self.update_status_ui(i, s))

    def update_status_ui(self, item, status):
        if not self.tree.exists(item): return
        vals = list(self.tree.item(item)['values'])
        vals[4] = status
        tag = "online" if status == "ONLINE" else "offline"
        self.tree.item(item, values=vals, tags=(tag,))

    # ── Hücre düzenleme ───────────────────────────────────────────────

    def edit_cell(self, item, column):
        if not item or not column: return
        col_idx = int(column[1:]) - 1
        if col_idx > 3: return

        x, y, w, h = self.tree.bbox(item, column)
        value = self.tree.item(item)['values'][col_idx]
        
        t = THEMES[self.theme_var.get()]
        entry = tk.Entry(self.tree_frame, font=(t["font"], 10), bg=t["bg"], fg=t["fg"],
                         insertbackground=t["fg"], bd=1)
        entry.insert(0, value)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, tk.END)
        self.active_entry = entry

        def save_and_move(direction=0):
            if not self.active_entry: return
            new_val = entry.get()
            vals = list(self.tree.item(item)['values'])
            vals[col_idx] = new_val
            self.tree.item(item, values=vals)
            self.update_all_channels_from_tree()
            entry.destroy()
            self.active_entry = None
            if direction != 0:
                target_item = self.tree.next(item) if direction == 1 else self.tree.prev(item)
                if target_item:
                    self.tree.selection_set(target_item)
                    self.root.after(10, lambda: self.edit_cell(target_item, column))

        entry.bind("<Return>", lambda e: save_and_move(0))
        entry.bind("<Down>", lambda e: save_and_move(1))
        entry.bind("<Up>", lambda e: save_and_move(-1))
        entry.bind("<FocusOut>", lambda e: save_and_move(0))

    # ── Tema ──────────────────────────────────────────────────────────

    def apply_theme(self, theme_name):
        t = THEMES[theme_name]
        self.root.configure(bg=t["bg"])
        self.tb.configure(bg=t["bg"])
        self.status_bar.configure(bg=t["bg"], fg=t["fg"])
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=t["bg"], foreground=t["fg"],
                        fieldbackground=t["bg"], rowheight=32, font=(t["font"], 10))
        style.configure("Treeview.Heading", background=t["bg"], foreground=t["fg"],
                        font=(t["font"], 10, "bold"))
        style.map("Treeview", background=[('selected', t["sel"])], foreground=[('selected', "#ffffff")])
        self.tree.tag_configure("online", foreground="#2ecc71")
        self.tree.tag_configure("offline", foreground="#e74c3c")

    # ── Liste yönetimi ────────────────────────────────────────────────

    def filter_list(self):
        query = self.search_var.get().lower()
        self.tree.delete(*self.tree.get_children())
        for row in self.all_channels:
            if any(query in str(item).lower() for item in row):
                self.tree.insert("", "end", values=row)

    def update_all_channels_from_tree(self):
        self.all_channels = [self.tree.item(item)['values'] for item in self.tree.get_children()]

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.menu.post(event.x_root, event.y_root)

    def copy_text(self):
        sel = self.tree.selection()
        if sel:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.tree.item(sel[0])['values'][3])

    def bulk_group_change(self):
        selections = self.tree.selection()
        if not selections: return
        new_group = simpledialog.askstring("Grup Değiştir", "Yeni grup ismini girin:")
        if new_group:
            for item in selections:
                vals = list(self.tree.item(item)['values'])
                vals[2] = new_group
                self.tree.item(item, values=vals)
            self.update_all_channels_from_tree()

    def remove_duplicates(self):
        seen_urls = set()
        to_delete = []
        for item in self.tree.get_children():
            url = self.tree.item(item)['values'][3]
            if url in seen_urls: to_delete.append(item)
            else: seen_urls.add(url)
        for item in to_delete: self.tree.delete(item)
        self.update_all_channels_from_tree()
        messagebox.showinfo("Bitti", f"{len(to_delete)} kopya silindi.")

    # ── Dosya işlemleri ───────────────────────────────────────────────

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("M3U", "*.m3u*")])
        if path:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                self.process_m3u(f.read())

    def open_url(self):
        url = simpledialog.askstring("URL", "M3U Linkini Girin:")
        if url:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as r:
                    self.process_m3u(r.read().decode('utf-8', errors='ignore'))
            except:
                messagebox.showerror("Hata", "Link açılamadı.")

    def process_m3u(self, text):
        lines = text.splitlines()
        self.all_channels = []
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF"):
                name = line.split(",")[-1].strip()
                url = lines[i+1].strip() if i+1 < len(lines) else ""
                group = re.search(r'group-title="([^"]*)"', line)
                logo = re.search(r'tvg-logo="([^"]*)"', line)
                self.all_channels.append((
                    logo.group(1) if logo else "",
                    name,
                    group.group(1) if group else "",
                    url,
                    "BEKLIYOR"
                ))
        self.filter_list()
        self.status_bar.config(text=f"{len(self.all_channels)} kanal yüklendi  |  Çift tıkla düzenle · Sağ tık → Önizle")

    def add_row_top(self):
        new_row = ("", "Yeni Kanal", "Grup", "http://", "YENİ")
        self.all_channels.insert(0, new_row)
        self.filter_list()

    def delete_row(self):
        for item in self.tree.selection():
            self.tree.delete(item)
        self.update_all_channels_from_tree()

    def save_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".m3u")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for item in self.tree.get_children():
                    v = self.tree.item(item)['values']
                    f.write(f'#EXTINF:-1 tvg-logo="{v[0]}" group-title="{v[2]}",{v[1]}\n{v[3]}\n')
            self.status_bar.config(text=f"Kaydedildi: {path}")

if __name__ == "__main__":
    root = tk.Tk()
    app = IPTVEditor(root)
    root.mainloop()
