#!/usr/bin/env python3
"""
Acestream Player
----------------
Acestream linkini (acestream://<hash> ya da sadece <hash>) veya http(s)
linkini yapıştır, Oynat'a bas. Arka planda çalışan Acestream Engine'e
(varsayılan port 6878) istek atıp dönen HTTP stream URL'sini VLC ile
embedded olarak oynatır.

Özellikler:
  - Oynat / Duraklat / Durdur, ilerleme çubuğu, hız kontrolü
  - Tam ekran (F veya çift tık), klavye kısayolları
  - Ses + sessiz
  - Yeniden boyutlandırılabilir / daraltılabilir playlist (splitter)
  - Sürükle-bırak sıralama, sonraki kanala otomatik geçiş
  - Engine durumu kontrolü, ayarlar (QSettings)
  - Sistem tepsisi, karanlık tema seçeneği
  - M3U kaydet / yükle

Gereksinimler:
    - Acestream Engine kurulu ve çalışıyor olmalı (arka planda).
      AUR: paru -S acestream-engine-py3
      Engine'i başlatmak için genelde: acestreamengine --client-console
    - pip install PyQt6 python-vlc requests
"""

import sys
import os

# VLC embedding için X11 pencere ID'si gerekir. Wayland'da Qt varsayılan
# backend ile açılırsa embedding çalışmaz. XWayland (xcb) zorlanır.
if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "xcb"

import re
import logging
import shutil
import subprocess
import json

import requests

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QFrame, QMessageBox, QListWidget,
    QListWidgetItem, QInputDialog, QFileDialog, QSlider, QSplitter,
    QSystemTrayIcon, QMenu, QStyle, QCheckBox, QSpinBox, QComboBox,
    QDialog, QFormLayout, QDialogButtonBox, QGroupBox, QAbstractItemView,
    QButtonGroup, QRadioButton,
)
from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QAction, QKeySequence, QShortcut

import vlc

# ---------------------------------------------------------------------------
# Sabitler / log
# ---------------------------------------------------------------------------
ENGINE_HOST_DEFAULT = "127.0.0.1"
ENGINE_PORT_DEFAULT = 6878
APP_NAME = "AcestreamPlayer"
ORG_NAME = "AcestreamPlayer"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(APP_NAME)


def classify_link(text: str):
    """Linki sınıflandırır -> ("acestream", content_id) ya da ("direct", url)"""
    text = text.strip()

    m = re.match(r"acestream://([0-9a-fA-F]{40})", text)
    if m:
        return "acestream", m.group(1)

    m = re.search(r"[?&]id=([0-9a-fA-F]{40})", text)
    if m:
        return "acestream", m.group(1)

    if re.match(r"^[0-9a-fA-F]{40}$", text):
        return "acestream", text

    if re.match(r"^https?://", text, re.IGNORECASE):
        return "direct", text

    raise ValueError(
        "Tanınmayan link. Acestream hash'i (40 haneli hex) ya da "
        "http(s):// ile başlayan bir link gir."
    )


def get_stream_url(content_id: str, host: str, port: int) -> str:
    """Engine'e content_id gönderip playback_url alır."""
    api = f"http://{host}:{port}/ace/getstream"
    params = {"id": content_id, "format": "json"}
    resp = requests.get(api, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Engine hata döndü: {data['error']}")
    return data["response"]["playback_url"]


def check_engine(host: str, port: int, timeout: float = 2.0) -> bool:
    """Engine'in ayakta olup olmadığını basitçe kontrol eder."""
    try:
        r = requests.get(f"http://{host}:{port}/webui/api/service", timeout=timeout)
        return r.status_code < 500
    except Exception:
        try:
            requests.get(f"http://{host}:{port}/", timeout=timeout)
            return True
        except Exception:
            return False


def format_seconds(secs: float) -> str:
    if secs is None or secs < 0:
        return "--:--"
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Ayarlar diyaloğu
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Ayarlar")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        eng = QGroupBox("Acestream Engine")
        form = QFormLayout(eng)
        self.host_edit = QLineEdit(settings.value("engine/host", ENGINE_HOST_DEFAULT))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(settings.value("engine/port", ENGINE_PORT_DEFAULT)))
        form.addRow("Host:", self.host_edit)
        form.addRow("Port:", self.port_spin)
        layout.addWidget(eng)

        play = QGroupBox("Oynatma")
        form2 = QFormLayout(play)
        self.auto_next = QCheckBox("Liste bitince sonraki kanala geç")
        self.auto_next.setChecked(settings.value("playback/auto_next", False, type=bool))
        self.reconnect = QCheckBox("Stream kopunca otomatik yeniden dene")
        self.reconnect.setChecked(settings.value("playback/auto_reconnect", True, type=bool))
        self.dark_theme = QCheckBox("Karanlık tema")
        self.dark_theme.setChecked(settings.value("ui/dark_theme", True, type=bool))
        form2.addRow(self.auto_next)
        form2.addRow(self.reconnect)
        form2.addRow(self.dark_theme)
        layout.addWidget(play)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self):
        self.settings.setValue("engine/host", self.host_edit.text().strip() or ENGINE_HOST_DEFAULT)
        self.settings.setValue("engine/port", self.port_spin.value())
        self.settings.setValue("playback/auto_next", self.auto_next.isChecked())
        self.settings.setValue("playback/auto_reconnect", self.reconnect.isChecked())
        self.settings.setValue("ui/dark_theme", self.dark_theme.isChecked())


# ---------------------------------------------------------------------------
# Ana pencere
# ---------------------------------------------------------------------------
class AcestreamPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Acestream Player")
        self.resize(1280, 720)

        self.settings = QSettings(ORG_NAME, APP_NAME)

        self.playlist_entries = []  # [{"name", "type", "value"}]
        self.current_stream_url = None
        self.current_link_type = None
        self.current_content_id = None
        self.current_playlist_index = -1
        self.is_fullscreen = False
        self._was_playing_before_seek = False
        self._user_seeking = False
        self.user_stopped = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 6

        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(500)
        self.ui_timer.timeout.connect(self._update_ui)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._try_reconnect)

        self._build_menu()
        self._build_ui()
        self._build_shortcuts()
        self._build_tray()
        self._apply_theme()
        self._restore_geometry()
        self._update_engine_status()
        # Program açılırken engine kapalıysa kullanıcıya başlatmayı teklif et.
        QTimer.singleShot(300, self._prompt_start_engine_if_needed)

        self.vlc_instance = vlc.Instance("--no-xlib", "--aout=pulse")
        self.media_player = self.vlc_instance.media_player_new()
        self._embed_video()

        self.event_manager = self.media_player.event_manager()
        self.event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end_reached)
        self.event_manager.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_player_error)

        # Daha önce eklenen playlist kayıtlarını (acestream + IPTV, tüm modlar) geri yükle.
        self.m3u_sources = self._load_m3u_sources()
        self._load_playlist_state()

        # Son izlenen kanalı göster (otomatik oynatmadan) — sadece link kutusuna
        # dolduruyoruz ve durum satırında hatırlatıyoruz, Oynat'a basmak kullanıcıya kalıyor.
        last_raw = self.settings.value("last/raw", "")
        if last_raw:
            self.link_input.setText(last_raw)
            last_name = self.settings.value("last/name", "")
            label = last_name if last_name else last_raw
            self.status_label.setText(f"Son izlenen: {label} — devam etmek için Oynat'a bas.")

        self.ui_timer.start()

    # ------------------------------------------------------------------ UI
    def _build_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("Dosya")
        act_save = QAction("M3U olarak kaydet…", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self.save_as_m3u)
        file_menu.addAction(act_save)

        act_load = QAction("M3U yükle…", self)
        act_load.setShortcut("Ctrl+O")
        act_load.triggered.connect(self.load_from_m3u)
        file_menu.addAction(act_load)

        act_load_url = QAction("M3U URL'den yükle…", self)
        act_load_url.setShortcut("Ctrl+Shift+O")
        act_load_url.triggered.connect(self.load_from_m3u_url)
        file_menu.addAction(act_load_url)

        act_refresh_sources = QAction("M3U Kaynaklarını Yenile", self)
        act_refresh_sources.triggered.connect(self.refresh_m3u_sources)
        file_menu.addAction(act_refresh_sources)

        file_menu.addSeparator()
        act_settings = QAction("Ayarlar…", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self.open_settings)
        file_menu.addAction(act_settings)

        file_menu.addSeparator()
        act_quit = QAction("Çıkış", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        play_menu = mb.addMenu("Oynatma")
        self.act_play_pause = QAction("Oynat / Duraklat", self)
        self.act_play_pause.setShortcut("Space")
        self.act_play_pause.triggered.connect(self.toggle_play_pause)
        play_menu.addAction(self.act_play_pause)

        act_stop = QAction("Durdur", self)
        act_stop.setShortcut("S")
        act_stop.triggered.connect(self.stop_playback)
        play_menu.addAction(act_stop)

        act_fs = QAction("Tam ekran", self)
        act_fs.setShortcut("F")
        act_fs.triggered.connect(self.toggle_fullscreen)
        play_menu.addAction(act_fs)

        play_menu.addSeparator()
        speed_menu = play_menu.addMenu("Hız")
        for rate, label in [
            (0.5, "0.5x"), (0.75, "0.75x"), (1.0, "1.0x"),
            (1.25, "1.25x"), (1.5, "1.5x"), (2.0, "2.0x"),
        ]:
            a = QAction(label, self)
            a.triggered.connect(lambda checked=False, r=rate: self.set_rate(r))
            speed_menu.addAction(a)

        help_menu = mb.addMenu("Yardım")
        act_about = QAction("Hakkında", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # Üst bar: link + oynat + listeye ekle (mode seçiciden önce kuruluyor
        # çünkü mod radyolarının ilk 'toggled' sinyali _set_mode'u hemen
        # tetikler ve o da link_input'a erişir)
        self.top_bar_widget = QWidget()
        top = QHBoxLayout(self.top_bar_widget)
        top.setContentsMargins(0, 0, 0, 0)
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("acestream://… , hash veya http(s):// linki yapıştır")
        self.link_input.returnPressed.connect(self.play_link)
        top.addWidget(self.link_input, stretch=1)

        self.play_btn = QPushButton("▶ Oynat")
        self.play_btn.clicked.connect(self.play_link)
        top.addWidget(self.play_btn)

        self.add_btn = QPushButton("＋ Listeye Ekle")
        self.add_btn.clicked.connect(self.add_current_link_to_list)
        top.addWidget(self.add_btn)

        # Mod seçici: Acestream / IPTV Filmler / IPTV Diziler / IPTV Canlı
        self.mode_widget = QWidget()
        mode_row = QHBoxLayout(self.mode_widget)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(QLabel("Mod:"))

        self.mode_group = QButtonGroup(self)
        self.mode_radios = {}
        for code, label in (
            ("acestream", "Acestream"),
            ("movies", "IPTV Filmler"),
            ("series", "IPTV Diziler"),
            ("live", "IPTV Canlı"),
        ):
            radio = QRadioButton(label)
            self.mode_group.addButton(radio)
            mode_row.addWidget(radio)
            self.mode_radios[code] = radio
            radio.toggled.connect(lambda checked, c=code: checked and self._set_mode(c))
        # setChecked burada YAPILMIYOR: henüz m3u butonları, arama kutusu ve
        # playlist widget'ı oluşturulmadı; _set_mode bunlara erişmeye çalışır.
        # İlk seçim _build_ui'nin sonunda, her şey hazır olduktan sonra yapılıyor.

        self.m3u_open_file_btn = QPushButton("📂 M3U Dosyası Aç")
        self.m3u_open_file_btn.clicked.connect(self.load_from_m3u)
        self.m3u_open_url_btn = QPushButton("🌐 M3U URL'den Aç")
        self.m3u_open_url_btn.clicked.connect(self.load_from_m3u_url)
        mode_row.addWidget(self.m3u_open_file_btn)
        mode_row.addWidget(self.m3u_open_url_btn)

        mode_row.addStretch()

        # Görsel sıra: önce mod seçici, sonra link/oynat/ekle satırı
        left_layout.addWidget(self.mode_widget)
        left_layout.addWidget(self.top_bar_widget)

        # Video
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet("background-color: black;")
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_frame.setMinimumHeight(200)
        self.video_frame.mouseDoubleClickEvent = lambda e: self.toggle_fullscreen()
        left_layout.addWidget(self.video_frame, stretch=1)

        # Alt kontrol paneli: ilerleme çubuğu + oynatma kontrolleri
        # (tam ekranda tek parça olarak gizlenebilsin diye bir widget içinde)
        self.controls_widget = QWidget()
        controls_layout = QVBoxLayout(self.controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(4)

        # İlerleme çubuğu
        progress_row = QHBoxLayout()
        self.time_label = QLabel("00:00")
        self.time_label.setMinimumWidth(50)
        progress_row.addWidget(self.time_label)

        self.progress_slider = QSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.setValue(0)
        self.progress_slider.sliderPressed.connect(self._on_seek_press)
        self.progress_slider.sliderReleased.connect(self._on_seek_release)
        self.progress_slider.sliderMoved.connect(self._on_seek_moved)
        progress_row.addWidget(self.progress_slider, stretch=1)

        self.duration_label = QLabel("--:--")
        self.duration_label.setMinimumWidth(50)
        progress_row.addWidget(self.duration_label)
        controls_layout.addLayout(progress_row)

        # Alt kontrol: Duraklat / Durdur + ses + hız + engine
        bottom = QHBoxLayout()

        self.pause_btn = QPushButton("⏸ Duraklat")
        self.pause_btn.clicked.connect(self.toggle_play_pause)
        bottom.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("⏹ Durdur")
        self.stop_btn.clicked.connect(self.stop_playback)
        bottom.addWidget(self.stop_btn)

        bottom.addSpacing(16)

        bottom.addWidget(QLabel("Ses:"))
        self.mute_btn = QPushButton("🔊")
        self.mute_btn.setFixedWidth(36)
        self.mute_btn.clicked.connect(self.toggle_mute)
        bottom.addWidget(self.mute_btn)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(self.settings.value("playback/volume", 100)))
        self.volume_slider.setMaximumWidth(140)
        self.volume_slider.valueChanged.connect(self.change_volume)
        bottom.addWidget(self.volume_slider)

        bottom.addSpacing(12)

        self.rate_combo = QComboBox()
        self.rate_combo.addItems(["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self.rate_combo.setCurrentText("1.0x")
        self.rate_combo.setFixedWidth(70)
        self.rate_combo.currentTextChanged.connect(self._on_rate_changed)
        bottom.addWidget(QLabel("Hız:"))
        bottom.addWidget(self.rate_combo)

        bottom.addStretch()

        self.engine_label = QLabel("Engine: ?")
        self.engine_label.setStyleSheet("color: gray;")
        bottom.addWidget(self.engine_label)

        self.start_engine_btn = QPushButton("▶ Engine'i Başlat")
        self.start_engine_btn.clicked.connect(self._start_engine_process)
        self.start_engine_btn.setVisible(False)
        bottom.addWidget(self.start_engine_btn)

        controls_layout.addLayout(bottom)

        left_layout.addWidget(self.controls_widget)

        self.status_label = QLabel("Hazır. Bir Acestream veya http linki yapıştır.")
        left_layout.addWidget(self.status_label)

        # Sağ: playlist
        right = QWidget()
        right.setMinimumWidth(0)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Playlist"))
        self.toggle_playlist_btn = QPushButton("◀")
        self.toggle_playlist_btn.setFixedWidth(28)
        self.toggle_playlist_btn.setToolTip("Playlist panelini gizle / göster")
        self.toggle_playlist_btn.clicked.connect(self.toggle_playlist_panel)
        hdr.addStretch()
        hdr.addWidget(self.toggle_playlist_btn)
        right_layout.addLayout(hdr)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔎 Kanal ara…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_playlist)
        right_layout.addWidget(self.search_input)

        self.playlist_widget = QListWidget()
        self.playlist_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.playlist_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.playlist_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.playlist_widget.itemDoubleClicked.connect(self.play_from_list)
        self.playlist_widget.model().rowsMoved.connect(self._on_playlist_reordered)
        right_layout.addWidget(self.playlist_widget, stretch=1)

        pl_btns = QHBoxLayout()
        remove_btn = QPushButton("Sil")
        remove_btn.clicked.connect(self.remove_selected_entry)
        pl_btns.addWidget(remove_btn)
        clear_btn = QPushButton("Temizle")
        clear_btn.clicked.connect(self.clear_playlist)
        pl_btns.addWidget(clear_btn)
        right_layout.addLayout(pl_btns)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, True)
        sizes = self.settings.value("ui/splitter_sizes")
        if sizes:
            try:
                self.splitter.setSizes([int(x) for x in sizes])
            except Exception:
                self.splitter.setSizes([900, 280])
        else:
            self.splitter.setSizes([900, 280])

        outer.addWidget(self.splitter)

        self._playlist_visible = True
        self._saved_playlist_width = 280

        self.mode = "acestream"
        self.mode_radios["acestream"].blockSignals(True)
        self.mode_radios["acestream"].setChecked(True)
        self.mode_radios["acestream"].blockSignals(False)
        self._set_mode("acestream")

    CATEGORY_LABELS = {
        "acestream": "Acestream",
        "movies": "IPTV Filmler",
        "series": "IPTV Diziler",
        "live": "IPTV Canlı",
    }

    def _set_mode(self, mode: str):
        self.mode = mode
        if mode == "acestream":
            self.link_input.setPlaceholderText("acestream://… veya hash yapıştır")
            self.m3u_open_file_btn.setVisible(False)
            self.m3u_open_url_btn.setVisible(False)
        else:
            self.link_input.setPlaceholderText("Tek bir IPTV yayın linki (http/https) yapıştırabilir "
                                                "ya da M3U aç butonlarını kullanabilirsin")
            self.m3u_open_file_btn.setVisible(True)
            self.m3u_open_url_btn.setVisible(True)
        self.status_label.setText(f"Mod: {self.CATEGORY_LABELS.get(mode, mode)}")
        self._refresh_playlist_visibility()

    def _build_shortcuts(self):
        QShortcut(QKeySequence("M"), self, self.toggle_mute)
        QShortcut(QKeySequence("Left"), self, lambda: self.seek_relative(-10))
        QShortcut(QKeySequence("Right"), self, lambda: self.seek_relative(10))
        QShortcut(QKeySequence("Up"), self, lambda: self.adjust_volume(5))
        QShortcut(QKeySequence("Down"), self, lambda: self.adjust_volume(-5))
        QShortcut(QKeySequence("Escape"), self, self.exit_fullscreen_if_needed)

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.tray.setIcon(icon)
        self.tray.setToolTip("Acestream Player")

        menu = QMenu()
        menu.addAction("Göster", self.showNormal)
        menu.addAction("Oynat / Duraklat", self.toggle_play_pause)
        menu.addAction("Durdur", self.stop_playback)
        menu.addSeparator()
        menu.addAction("Çıkış", self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.showNormal()
                self.activateWindow()

    # ------------------------------------------------------------------ Tema / ayarlar
    def _apply_theme(self):
        dark = self.settings.value("ui/dark_theme", True, type=bool)
        if dark:
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #1e1e1e; color: #e0e0e0; }
                QLineEdit, QListWidget, QComboBox, QSpinBox {
                    background-color: #2d2d2d; color: #e0e0e0;
                    border: 1px solid #444; border-radius: 3px; padding: 3px;
                }
                QPushButton {
                    background-color: #333; color: #e0e0e0;
                    border: 1px solid #555; border-radius: 4px; padding: 4px 10px;
                }
                QPushButton:hover { background-color: #404040; }
                QPushButton:pressed { background-color: #505050; }
                QSlider::groove:horizontal {
                    height: 6px; background: #444; border-radius: 3px;
                }
                QSlider::handle:horizontal {
                    width: 14px; margin: -5px 0; background: #888; border-radius: 7px;
                }
                QMenuBar { background-color: #252525; }
                QMenuBar::item:selected { background-color: #3a3a3a; }
                QMenu { background-color: #2d2d2d; }
                QMenu::item:selected { background-color: #404040; }
                QStatusBar { background-color: #252525; }
                QSplitter::handle { background-color: #444; width: 4px; }
            """)
        else:
            self.setStyleSheet("")

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.apply()
            self._apply_theme()
            self._update_engine_status()
            self.set_status("Ayarlar kaydedildi.")

    def _restore_geometry(self):
        geo = self.settings.value("ui/geometry")
        if geo:
            self.restoreGeometry(geo)

    def _save_geometry(self):
        self.settings.setValue("ui/geometry", self.saveGeometry())
        self.settings.setValue("ui/splitter_sizes", self.splitter.sizes())
        self.settings.setValue("playback/volume", self.volume_slider.value())

    # ------------------------------------------------------------------ Engine
    def _engine_host(self) -> str:
        return self.settings.value("engine/host", ENGINE_HOST_DEFAULT)

    def _engine_port(self) -> int:
        return int(self.settings.value("engine/port", ENGINE_PORT_DEFAULT))

    def _update_engine_status(self):
        ok = check_engine(self._engine_host(), self._engine_port())
        if ok:
            self.engine_label.setText(f"Engine: ● {self._engine_host()}:{self._engine_port()}")
            self.engine_label.setStyleSheet("color: #4caf50;")
            self.start_engine_btn.setVisible(False)
        else:
            self.engine_label.setText(f"Engine: ○ kapalı ({self._engine_host()}:{self._engine_port()})")
            self.engine_label.setStyleSheet("color: #f44336;")
            # Sadece localhost için başlatma anlamlı; uzak bir engine'i buradan başlatamayız.
            self.start_engine_btn.setVisible(self._engine_host() in ("127.0.0.1", "localhost"))

    def _start_engine_process(self):
        if shutil.which("acestreamengine") is None:
            QMessageBox.critical(
                self, "acestreamengine bulunamadı",
                "Sistemde 'acestreamengine' komutu bulunamadı.\n\n"
                "Kurmak için:\n  paru -S acestream-engine-py3\n\n"
                "(Klasik 'acestream-engine' paketi python2'ye bağımlı ve artık "
                "obsolete/orphan olduğu için 'acestream-engine-py3' önerilir.)"
            )
            return

        self.start_engine_btn.setEnabled(False)
        self.set_status("Acestream Engine başlatılıyor…")
        try:
            subprocess.Popen(
                ["acestreamengine", "--client-console"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # Program kapansa bile engine çalışmaya devam etsin
            )
        except Exception as e:
            self.start_engine_btn.setEnabled(True)
            QMessageBox.critical(self, "Hata", f"Engine başlatılamadı:\n{e}")
            return

        self._engine_start_attempts = 0
        self._engine_start_timer = QTimer(self)
        self._engine_start_timer.timeout.connect(self._poll_engine_startup)
        self._engine_start_timer.start(1500)

    def _poll_engine_startup(self):
        self._engine_start_attempts += 1
        if check_engine(self._engine_host(), self._engine_port()):
            self._engine_start_timer.stop()
            self.start_engine_btn.setEnabled(True)
            self._update_engine_status()
            self.set_status("Engine hazır.")
            return
        if self._engine_start_attempts >= 15:  # ~22 saniye
            self._engine_start_timer.stop()
            self.start_engine_btn.setEnabled(True)
            self.set_status("Engine başlatıldı ama henüz yanıt vermiyor; biraz daha bekleyip tekrar dene.")
            self._update_engine_status()

    def _prompt_start_engine_if_needed(self):
        if check_engine(self._engine_host(), self._engine_port()):
            return
        if shutil.which("acestreamengine") is None:
            return  # Kurulu değilse sessizce geç, buton/menü zaten uyarıyor
        reply = QMessageBox.question(
            self, "Engine çalışmıyor",
            "Acestream Engine şu an çalışmıyor görünüyor.\n\n"
            "Şimdi başlatmamı ister misin? (acestreamengine --client-console)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_engine_process()

    # ------------------------------------------------------------------ Oynatma
    def set_status(self, text: str):
        self.status_label.setText(text)
        QApplication.processEvents()

    def play_link(self):
        raw = self.link_input.text()
        if not raw.strip():
            return
        self._play_raw(raw, from_playlist_index=-1)

    def _play_raw(self, raw: str, from_playlist_index: int = -1):
        self.play_btn.setEnabled(False)
        self.reconnect_timer.stop()
        self.user_stopped = False
        self.reconnect_attempts = 0
        try:
            self.set_status("Link ayrıştırılıyor…")
            link_type, value = classify_link(raw)

            if from_playlist_index == -1:
                if self.mode == "acestream" and link_type != "acestream":
                    self.play_btn.setEnabled(True)
                    QMessageBox.information(
                        self, "Mod uyuşmazlığı",
                        "Şu an Acestream modundasın, bu yüzden sadece acestream:// linki/hash "
                        "oynatabilirsin. Normal bir yayın linki oynatmak için üstten IPTV Filmler/"
                        "Diziler/Canlı modlarından birine geç."
                    )
                    self.set_status("Mod: Acestream (http link reddedildi)")
                    return
                if self.mode != "acestream" and link_type == "acestream":
                    self.play_btn.setEnabled(True)
                    QMessageBox.information(
                        self, "Mod uyuşmazlığı",
                        f"Şu an {self.CATEGORY_LABELS.get(self.mode, self.mode)} modundasın, bu yüzden "
                        "acestream linki oynatamazsın. Acestream oynatmak için üstten Acestream moduna geç."
                    )
                    self.set_status(f"Mod: {self.CATEGORY_LABELS.get(self.mode, self.mode)} (acestream link reddedildi)")
                    return

            if link_type == "acestream":
                self.set_status("Acestream Engine'den stream isteniyor…")
                stream_url = get_stream_url(value, self._engine_host(), self._engine_port())
            else:
                stream_url = value

            self.current_stream_url = stream_url
            self.current_link_type = link_type
            self.current_content_id = value if link_type == "acestream" else None
            self.current_playlist_index = from_playlist_index

            self.set_status("Oynatılıyor…")
            media = self.vlc_instance.media_new(stream_url)
            self.media_player.set_media(media)
            self.media_player.play()
            self.media_player.audio_set_mute(False)
            self.media_player.audio_set_volume(self.volume_slider.value())
            rate_text = self.rate_combo.currentText().replace("x", "")
            try:
                self.media_player.set_rate(float(rate_text))
            except Exception:
                pass

            self._highlight_playlist_index(from_playlist_index)
            self._update_engine_status()

            # Son izlenen kanalı hatırlamak için kaydet (bir sonraki açılışta
            # otomatik oynatmadan sadece hatırlatmak için kullanılıyor).
            self.settings.setValue("last/raw", raw)
            if 0 <= from_playlist_index < len(self.playlist_entries):
                self.settings.setValue("last/name", self.playlist_entries[from_playlist_index]["name"])
            else:
                self.settings.setValue("last/name", "")

        except requests.exceptions.ConnectionError:
            self.set_status("Hata: Engine'e bağlanılamadı.")
            self._update_engine_status()
            QMessageBox.critical(
                self, "Engine bulunamadı",
                f"{self._engine_host()}:{self._engine_port()} adresinde Acestream Engine bulunamadı.\n\n"
                "Engine'in kurulu ve çalışır durumda olduğundan emin ol:\n"
                "  paru -S acestream-engine\n"
                "  acestreamengine --client-console"
            )
        except Exception as e:
            self.set_status(f"Hata: {e}")
            log.exception("Oynatma hatası")
            QMessageBox.warning(self, "Hata", str(e))
        finally:
            self.play_btn.setEnabled(True)

    def stop_playback(self):
        self.reconnect_timer.stop()
        self.user_stopped = True
        self.reconnect_attempts = 0
        self.media_player.stop()
        self.progress_slider.setValue(0)
        self.time_label.setText("00:00")
        self.duration_label.setText("--:--")
        self.set_status("Durduruldu.")

    def toggle_play_pause(self):
        if self.media_player.is_playing():
            self.media_player.pause()
            self.pause_btn.setText("▶ Devam")
            self.set_status("Duraklatıldı.")
        else:
            if self.media_player.get_media() is None and self.link_input.text().strip():
                self.play_link()
            else:
                self.user_stopped = False
                self.reconnect_attempts = 0
                self.media_player.play()
                self.pause_btn.setText("⏸ Duraklat")
                self.set_status("Oynatılıyor…")

    def set_rate(self, rate: float):
        self.media_player.set_rate(rate)
        mapping = {
            0.5: "0.5x", 0.75: "0.75x", 1.0: "1.0x",
            1.25: "1.25x", 1.5: "1.5x", 2.0: "2.0x",
        }
        text = mapping.get(rate, f"{rate}x")
        idx = self.rate_combo.findText(text)
        if idx >= 0:
            self.rate_combo.blockSignals(True)
            self.rate_combo.setCurrentIndex(idx)
            self.rate_combo.blockSignals(False)

    def _on_rate_changed(self, text: str):
        try:
            rate = float(text.replace("x", ""))
            self.media_player.set_rate(rate)
        except Exception:
            pass

    def change_volume(self, value: int):
        self.media_player.audio_set_volume(value)
        if value > 0 and self.media_player.audio_get_mute():
            self.media_player.audio_set_mute(False)
        self.mute_btn.setText("🔇" if value == 0 else "🔊")

    def adjust_volume(self, delta: int):
        v = max(0, min(100, self.volume_slider.value() + delta))
        self.volume_slider.setValue(v)

    def toggle_mute(self):
        muted = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not muted)
        self.mute_btn.setText("🔇" if not muted else "🔊")

    def seek_relative(self, seconds: int):
        length = self.media_player.get_length()
        if length <= 0:
            return
        t = self.media_player.get_time() + seconds * 1000
        t = max(0, min(length - 1000, t))
        self.media_player.set_time(t)

    def _on_seek_press(self):
        self._user_seeking = True
        self._was_playing_before_seek = self.media_player.is_playing()

    def _on_seek_moved(self, value: int):
        length = self.media_player.get_length()
        if length > 0:
            t = (value / 1000.0) * length
            self.time_label.setText(format_seconds(t / 1000.0))

    def _on_seek_release(self):
        length = self.media_player.get_length()
        if length > 0:
            t = int((self.progress_slider.value() / 1000.0) * length)
            self.media_player.set_time(t)
        self._user_seeking = False

    def _update_ui(self):
        if not self._user_seeking:
            length = self.media_player.get_length()
            t = self.media_player.get_time()
            if length > 0:
                self.progress_slider.setEnabled(True)
                self.progress_slider.setValue(int((t / length) * 1000))
                self.duration_label.setText(format_seconds(length / 1000.0))
            else:
                # Canlı yayın: süre bilinmiyor, kaydırma çubuğunun etkileşimi
                # anlamsız olduğu için devre dışı bırakılıyor.
                self.progress_slider.setEnabled(False)
                self.progress_slider.setValue(0)
                self.duration_label.setText("CANLI")
            self.time_label.setText(format_seconds(t / 1000.0 if t >= 0 else 0))

        if self.media_player.is_playing():
            if self.pause_btn.text() != "⏸ Duraklat":
                self.pause_btn.setText("⏸ Duraklat")
        else:
            if self.media_player.get_state() == vlc.State.Paused:
                self.pause_btn.setText("▶ Devam")

    def _on_end_reached(self, event):
        QTimer.singleShot(0, self._handle_end_reached)

    def _handle_end_reached(self):
        auto = self.settings.value("playback/auto_next", False, type=bool)
        if auto and self.playlist_entries:
            nxt = self.current_playlist_index + 1
            if 0 <= nxt < len(self.playlist_entries):
                self._play_entry_at(nxt)
                return
        self.set_status("Yayın bitti.")

    def _on_player_error(self, event):
        QTimer.singleShot(0, self._handle_player_error)

    def _handle_player_error(self):
        self.set_status("Oynatıcı hatası.")
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self.user_stopped:
            return
        if not (self.settings.value("playback/auto_reconnect", True, type=bool) and self.current_stream_url):
            return
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            self.set_status(
                f"Yeniden bağlanma {self.max_reconnect_attempts} denemeden sonra durduruldu. "
                "Engine'i kontrol edip elle 'Oynat'a bas."
            )
            return
        self.reconnect_attempts += 1
        # Artan bekleme süresi: 3, 5, 8, 12, 17, 23 sn (üst sınır 25 sn)
        delay_ms = min(3000 + (self.reconnect_attempts - 1) * 2000 + self.reconnect_attempts * 500, 25000)
        self.set_status(
            f"Yeniden bağlanılacak ({self.reconnect_attempts}/{self.max_reconnect_attempts}, "
            f"{delay_ms // 1000} sn)…"
        )
        self.reconnect_timer.start(delay_ms)

    def _try_reconnect(self):
        if self.user_stopped:
            return
        try:
            if self.current_link_type == "acestream" and self.current_content_id:
                self.set_status("Yeniden bağlanılıyor…")
                url = get_stream_url(self.current_content_id, self._engine_host(), self._engine_port())
                self.current_stream_url = url
                media = self.vlc_instance.media_new(url)
                self.media_player.set_media(media)
                self.media_player.play()
                self.set_status("Yeniden bağlandı.")
                self.reconnect_attempts = 0
            elif self.current_stream_url:
                media = self.vlc_instance.media_new(self.current_stream_url)
                self.media_player.set_media(media)
                self.media_player.play()
                self.reconnect_attempts = 0
        except Exception as e:
            # get_stream_url gibi bir adım burada patlarsa VLC hiç devreye
            # girmediği için MediaPlayerEncounteredError tetiklenmez; zincirin
            # kopmaması için tekrar denemeyi burada elle planlıyoruz.
            self.set_status(f"Yeniden bağlanılamadı: {e}")
            self._schedule_reconnect()

    # ------------------------------------------------------------------ Tam ekran
    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        self.is_fullscreen = True
        self.menuBar().hide()
        self.top_bar_widget.hide()
        self.controls_widget.hide()
        self.status_label.hide()
        self._fs_sizes = self.splitter.sizes()
        self.splitter.setSizes([1, 0])
        self.showFullScreen()
        self._embed_video()

    def exit_fullscreen(self):
        self.is_fullscreen = False
        self.showNormal()
        self.menuBar().show()
        self.top_bar_widget.show()
        self.controls_widget.show()
        self.status_label.show()
        if hasattr(self, "_fs_sizes"):
            self.splitter.setSizes(self._fs_sizes)
        self._embed_video()

    def exit_fullscreen_if_needed(self):
        if self.is_fullscreen:
            self.exit_fullscreen()

    def _embed_video(self):
        if sys.platform.startswith("linux"):
            self.media_player.set_xwindow(int(self.video_frame.winId()))
        elif sys.platform == "win32":
            self.media_player.set_hwnd(int(self.video_frame.winId()))
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

    # ------------------------------------------------------------------ Playlist
    def toggle_playlist_panel(self):
        sizes = self.splitter.sizes()
        if sizes[1] > 20:
            self._saved_playlist_width = sizes[1]
            self.splitter.setSizes([sizes[0] + sizes[1], 0])
            self.toggle_playlist_btn.setText("▶")
            self._playlist_visible = False
        else:
            w = self._saved_playlist_width or 280
            total = sum(sizes)
            self.splitter.setSizes([max(100, total - w), w])
            self.toggle_playlist_btn.setText("◀")
            self._playlist_visible = True

    def _filter_playlist(self, text: str):
        self._refresh_playlist_visibility()

    def _refresh_playlist_visibility(self):
        text = self.search_input.text().strip().lower()
        for i in range(self.playlist_widget.count()):
            item = self.playlist_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            category = data.get("category", "acestream")
            matches_category = (category == self.mode)
            matches_text = (not text) or (text in item.text().lower())
            item.setHidden(not (matches_category and matches_text))

    def add_current_link_to_list(self):
        raw = self.link_input.text()
        if not raw.strip():
            return
        try:
            link_type, value = classify_link(raw)
        except Exception as e:
            QMessageBox.warning(self, "Hata", str(e))
            return

        if self.mode == "acestream" and link_type != "acestream":
            QMessageBox.information(
                self, "Mod uyuşmazlığı",
                "Acestream modundasın; listeye sadece acestream linki ekleyebilirsin. "
                "Normal bir yayın linki eklemek için üstten IPTV moduna geç."
            )
            return
        if self.mode != "acestream" and link_type == "acestream":
            QMessageBox.information(
                self, "Mod uyuşmazlığı",
                "IPTV modundasın; listeye acestream linki ekleyemezsin. "
                "Acestream eklemek için üstten Acestream moduna geç."
            )
            return

        name, ok = QInputDialog.getText(
            self, "Kanal adı", "Bu link için bir isim gir:",
            text=f"Kanal {len(self.playlist_entries) + 1}"
        )
        if not ok:
            return
        if not name.strip():
            name = value[:20] + "…" if len(value) > 20 else value

        self._add_entry(name.strip(), link_type, value, category=self.mode)
        self._save_playlist_state()

    def _add_entry(self, name: str, link_type: str, value: str, category: str = None):
        category = category or self.mode
        # Aynı (tip, değer, kategori) zaten varsa tekrar ekleme (M3U yenilemede yinelenmesin)
        for e in self.playlist_entries:
            if e["type"] == link_type and e["value"] == value and e.get("category") == category:
                return
        self.playlist_entries.append({"name": name, "type": link_type, "value": value, "category": category})
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, {"type": link_type, "value": value, "category": category})
        item.setHidden(category != self.mode)
        self.playlist_widget.addItem(item)

    def remove_selected_entry(self):
        row = self.playlist_widget.currentRow()
        if row < 0:
            return
        self.playlist_widget.takeItem(row)
        del self.playlist_entries[row]
        if self.current_playlist_index == row:
            self.current_playlist_index = -1
        elif self.current_playlist_index > row:
            self.current_playlist_index -= 1
        self._save_playlist_state()

    def clear_playlist(self):
        if not self.playlist_entries:
            return
        if QMessageBox.question(self, "Onay", "Tüm liste silinsin mi?") != QMessageBox.StandardButton.Yes:
            return
        self.playlist_widget.clear()
        self.playlist_entries.clear()
        self.current_playlist_index = -1
        self._save_playlist_state()

    def _on_playlist_reordered(self, parent, start, end, dest, row):
        new_entries = []
        for i in range(self.playlist_widget.count()):
            item = self.playlist_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            new_entries.append({
                "name": item.text(),
                "type": data["type"],
                "value": data["value"],
                "category": data.get("category", "acestream"),
            })
        self.playlist_entries = new_entries
        self.current_playlist_index = -1
        self._save_playlist_state()

    def play_from_list(self, item: QListWidgetItem):
        row = self.playlist_widget.row(item)
        self._play_entry_at(row)

    def _play_entry_at(self, index: int):
        if index < 0 or index >= len(self.playlist_entries):
            return
        entry = self.playlist_entries[index]
        if entry["type"] == "acestream":
            self.link_input.setText(f"acestream://{entry['value']}")
        else:
            self.link_input.setText(entry["value"])
        self._play_raw(self.link_input.text(), from_playlist_index=index)

    def _highlight_playlist_index(self, index: int):
        for i in range(self.playlist_widget.count()):
            item = self.playlist_widget.item(i)
            font = item.font()
            if i == index:
                font.setBold(True)
                item.setForeground(Qt.GlobalColor.cyan)
            else:
                font.setBold(False)
                item.setForeground(
                    Qt.GlobalColor.white
                    if self.settings.value("ui/dark_theme", True, type=bool)
                    else Qt.GlobalColor.black
                )
            item.setFont(font)
            if i == index:
                self.playlist_widget.setCurrentRow(i)

    # ------------------------------------------------------------------ Kalıcı saklama
    def _save_playlist_state(self):
        try:
            self.settings.setValue("playlist/entries_json", json.dumps(self.playlist_entries))
        except Exception as e:
            logging.warning("Playlist kaydedilemedi: %s", e)

    def _load_playlist_state(self):
        raw = self.settings.value("playlist/entries_json", "")
        if not raw:
            return
        try:
            entries = json.loads(raw)
        except Exception as e:
            logging.warning("Playlist okunamadı: %s", e)
            return
        for e in entries:
            name = e.get("name", "")
            link_type = e.get("type")
            value = e.get("value")
            category = e.get("category", "acestream")
            if not (name and link_type and value):
                continue
            self.playlist_entries.append({"name": name, "type": link_type, "value": value, "category": category})
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, {"type": link_type, "value": value, "category": category})
            item.setHidden(category != self.mode)
            self.playlist_widget.addItem(item)

    def _remember_m3u_source(self, source: dict):
        # Aynı konum + kategori zaten kayıtlıysa tekrar ekleme
        for s in self.m3u_sources:
            if s.get("location") == source.get("location") and s.get("category") == source.get("category"):
                return
        self.m3u_sources.append(source)
        self._save_m3u_sources()

    def _save_m3u_sources(self):
        try:
            self.settings.setValue("m3u/sources_json", json.dumps(self.m3u_sources))
        except Exception as e:
            logging.warning("M3U kaynakları kaydedilemedi: %s", e)

    def _load_m3u_sources(self):
        raw = self.settings.value("m3u/sources_json", "")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    def refresh_m3u_sources(self):
        """Hatırlanan tüm M3U dosya/URL kaynaklarını tekrar okuyup eksik kanalları ekler."""
        if not self.m3u_sources:
            QMessageBox.information(self, "Kaynak yok", "Henüz kaydedilmiş bir M3U dosyası/URL'si yok.")
            return

        prev_mode = self.mode
        total_before = len(self.playlist_entries)
        errors = []
        for src in self.m3u_sources:
            kind = src.get("kind")
            location = src.get("location")
            category = src.get("category", "acestream")
            try:
                if kind == "file":
                    with open(location, "r", encoding="utf-8") as f:
                        content = f.read()
                else:
                    resp = requests.get(location, timeout=20)
                    resp.raise_for_status()
                    resp.encoding = resp.encoding or "utf-8"
                    content = resp.text
            except Exception as e:
                errors.append(f"{location}: {e}")
                continue

            # _load_m3u_content mevcut self.mode'a göre filtrelediği için,
            # kaynağın kendi kategorisini geçici olarak aktif moda alıyoruz.
            self.mode = category
            self._load_m3u_content(content, source=location, remember_source=None, warn_if_empty=False)

        self.mode = prev_mode
        self._set_mode(prev_mode)
        for radio_code, radio in self.mode_radios.items():
            radio.setChecked(radio_code == prev_mode)
        self._save_playlist_state()

        added = len(self.playlist_entries) - total_before
        msg = f"Kaynaklar yenilendi: {added} yeni kanal eklendi."
        if errors:
            msg += f" {len(errors)} kaynak okunamadı."
        self.set_status(msg)
        if errors:
            QMessageBox.warning(self, "Bazı kaynaklar okunamadı", "\n".join(errors))

    # ------------------------------------------------------------------ M3U
    def save_as_m3u(self):
        if not self.playlist_entries:
            QMessageBox.information(self, "Liste boş", "Kaydedilecek link yok.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "M3U olarak kaydet", "playlist.m3u", "M3U (*.m3u)")
        if not path:
            return
        if not path.lower().endswith(".m3u"):
            path += ".m3u"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for entry in self.playlist_entries:
                    f.write(f"#EXTINF:-1,{entry['name']}\n")
                    if entry["type"] == "acestream":
                        f.write(f"acestream://{entry['value']}\n")
                    else:
                        f.write(f"{entry['value']}\n")
            self.set_status(f"M3U kaydedildi: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Dosya kaydedilemedi:\n{e}")

    def load_from_m3u(self):
        path, _ = QFileDialog.getOpenFileName(self, "M3U yükle", "", "M3U (*.m3u *.m3u8)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Dosya okunamadı:\n{e}")
            return
        self._load_m3u_content(content, source=path, remember_source={"kind": "file", "location": path})

    def load_from_m3u_url(self):
        url, ok = QInputDialog.getText(
            self, "M3U URL'den yükle", "M3U/M3U8 linkini yapıştır:"
        )
        if not ok or not url.strip():
            return
        url = url.strip()

        self.set_status(f"M3U indiriliyor: {url}")
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            content = resp.text
        except Exception as e:
            self.set_status("M3U indirilemedi.")
            QMessageBox.critical(self, "Hata", f"M3U indirilemedi:\n{e}")
            return

        self._load_m3u_content(content, source=url, remember_source={"kind": "url", "location": url})

    def _load_m3u_content(self, content: str, source: str = "", remember_source: dict = None,
                           warn_if_empty: bool = True):
        lines = [line.strip() for line in content.splitlines()]

        pending_name = None
        loaded = 0
        skipped = 0
        for line in lines:
            if line.startswith("#EXTINF"):
                if "," in line:
                    pending_name = line.split(",", 1)[1].strip()
                else:
                    pending_name = None
            elif line and not line.startswith("#"):
                try:
                    link_type, value = classify_link(line)
                except ValueError:
                    skipped += 1
                    pending_name = None
                    continue
                if self.mode == "acestream" and link_type != "acestream":
                    skipped += 1
                    pending_name = None
                    continue
                if self.mode != "acestream" and link_type == "acestream":
                    skipped += 1
                    pending_name = None
                    continue
                name = pending_name or (value[:24] + "…" if len(value) > 24 else value)
                self._add_entry(name, link_type, value, category=self.mode)
                pending_name = None
                loaded += 1

        msg = f"{loaded} link M3U'dan yüklendi."
        if skipped:
            msg += f" ({skipped} atlandı — tanınmayan ya da mevcut moda uymayan satır.)"
        self.set_status(msg)

        if loaded == 0:
            if warn_if_empty:
                QMessageBox.warning(
                    self, "Boş sonuç",
                    "Bu M3U içinde tanınan ve mevcut modla uyumlu hiçbir link bulunamadı."
                )
            return

        if remember_source is not None:
            remember_source["category"] = self.mode
            self._remember_m3u_source(remember_source)
        self._save_playlist_state()

    # ------------------------------------------------------------------ Diğer
    def show_about(self):
        QMessageBox.about(
            self, "Hakkında",
            "<b>Acestream Player</b><br><br>"
            "VLC + PyQt6 tabanlı Acestream / HTTP oynatıcı.<br><br>"
            "Kısayollar:<br>"
            "Space – Oynat/Duraklat &nbsp; F – Tam ekran<br>"
            "M – Sessiz &nbsp; ←/→ – 10 sn atla &nbsp; ↑/↓ – Ses<br>"
            "Esc – Tam ekrandan çık &nbsp; S – Durdur"
        )

    def closeEvent(self, event):
        self._save_geometry()
        self.media_player.stop()
        if self.tray:
            self.tray.hide()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setQuitOnLastWindowClosed(True)

    window = AcestreamPlayer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
