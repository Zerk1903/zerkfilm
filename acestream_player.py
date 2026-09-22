#!/usr/bin/env python3
"""
Basit Acestream Player
-----------------------
Acestream linkini (acestream://<hash> ya da sadece <hash>) yapıştır,
Oynat'a bas. Arka planda çalışan Acestream Engine'e (varsayılan port 6878)
istek atıp dönen HTTP stream URL'sini VLC ile embedded olarak oynatır.

Kayıt, VLC'nin gösterdiği görüntüden bağımsız olarak ffmpeg ile yapılır
(aynı stream URL'sinden ikinci, ayrı bir okuma; -c copy ile yeniden
kodlama yapılmadan en yüksek kalitede kaydeder).

Gereksinimler:
    - Acestream Engine kurulu ve çalışıyor olmalı (arka planda).
      AUR: paru -S acestream-engine-py3
      Engine'i başlatmak için genelde: acestreamengine --client-console
    - ffmpeg kurulu olmalı (kayıt için): sudo pacman -S ffmpeg
    - pip install PyQt6 python-vlc requests
"""

import sys
import os

# VLC embedding (video'yu program penceresinin İÇİNE gömme) bir X11 pencere
# ID'si gerektirir. CachyOS gibi Wayland tabanlı sistemlerde Qt varsayılan
# olarak "wayland" backend'iyle açılırsa embedding çalışmaz ve VLC ayrı bir
# pencere açar. Bunu engellemek için Qt'yi XWayland (xcb) üzerinden
# çalışmaya zorluyoruz — bu satır her şeyden önce, QApplication
# oluşturulmadan önce çalışmalı.
if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "xcb"

import re
import signal
import shutil
import subprocess
import tempfile
import datetime
import requests

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QFrame, QMessageBox, QListWidget,
    QListWidgetItem, QInputDialog, QFileDialog, QSlider
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction

import vlc

ENGINE_HOST = "127.0.0.1"
ENGINE_PORT = 6878



def classify_link(text: str):
    """
    Linki sınıflandırır -> ("acestream", content_id) ya da ("direct", url)
    Acestream: acestream://<hash>, ...?id=<hash>, ya da çıplak 40 haneli hex hash
    Direct: http(s):// ile başlayan her şey (m3u8, mp4, vs.)
    """
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
        "Tanınmayan link. Acestream hash'i (40 haneli hex) ya da http(s):// ile başlayan bir link gir."
    )


def get_stream_url(content_id: str) -> str:
    """Engine'e content_id gönderip playback_url alır."""
    api = f"http://{ENGINE_HOST}:{ENGINE_PORT}/ace/getstream"
    params = {"id": content_id, "format": "json"}
    resp = requests.get(api, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Engine hata döndü: {data['error']}")
    return data["response"]["playback_url"]


def ffmpeg_args_for_path(path: str):
    """
    Dosya uzantısına göre ffmpeg -f (container) değerini ve gerekiyorsa
    ek argümanları döndürür. Hepsi stream copy (-c copy) ile kullanılır,
    yani yeniden kodlama yok -> orijinal kalite korunur.
    """
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "mkv":
        return "matroska", []
    if ext == "mp4":
        # Aniden kesilirse dosyanın çalışır kalması için fragmented mp4
        return "mp4", ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]
    # ts ve tanınmayan her şey için en dayanıklı seçenek
    return "mpegts", []


class AcestreamPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Acestream Player")
        self.resize(1200, 650)

        # Eklenen linkler: [{"name": str, "type": str, "value": str}]
        self.playlist_entries = []

        # Kayıt durumu
        self.current_stream_url = None
        self.current_link_type = None
        self.current_content_id = None
        self.is_recording = False
        self.record_process = None
        self.record_log_file = None
        self.record_path = None
        self.record_monitor_timer = QTimer(self)
        self.record_monitor_timer.setInterval(2000)
        self.record_monitor_timer.timeout.connect(self._check_recording_alive)

        self._build_menu()

        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QHBoxLayout(central)

        # Sol taraf: video + kontroller
        left_widget = QWidget()
        layout = QVBoxLayout(left_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Üst bar: link girişi + butonlar
        top_bar = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("acestream://... , hash, veya http(s):// linki yapıştır")
        self.link_input.returnPressed.connect(self.play_link)
        top_bar.addWidget(self.link_input)

        self.play_btn = QPushButton("Oynat")
        self.play_btn.clicked.connect(self.play_link)
        top_bar.addWidget(self.play_btn)

        self.stop_btn = QPushButton("Durdur")
        self.stop_btn.clicked.connect(self.stop_playback)
        top_bar.addWidget(self.stop_btn)

        self.add_btn = QPushButton("Listeye Ekle")
        self.add_btn.clicked.connect(self.add_current_link_to_list)
        top_bar.addWidget(self.add_btn)

        self.record_btn = QPushButton("● Kayıt Başlat")
        self.record_btn.setStyleSheet("color: #cc0000; font-weight: bold;")
        self.record_btn.clicked.connect(self.toggle_recording)
        top_bar.addWidget(self.record_btn)

        layout.addLayout(top_bar)

        # Video alanı (VLC burada embed edilecek)
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet("background-color: black;")
        # Embedding için gerçek bir native pencere handle'ı zorunlu;
        # bu bayrak Qt'ye bunu erkenden oluşturmasını söyler.
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        layout.addWidget(self.video_frame, stretch=1)

        # Durum etiketi
        self.status_label = QLabel("Hazır. Bir Acestream linki yapıştır.")
        layout.addWidget(self.status_label)

        # Ses kontrolü
        volume_bar = QHBoxLayout()
        volume_bar.addWidget(QLabel("Ses:"))
        self.mute_btn = QPushButton("🔊")
        self.mute_btn.setFixedWidth(36)
        self.mute_btn.clicked.connect(self.toggle_mute)
        volume_bar.addWidget(self.mute_btn)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.valueChanged.connect(self.change_volume)
        volume_bar.addWidget(self.volume_slider)
        layout.addLayout(volume_bar)

        outer_layout.addWidget(left_widget, stretch=3)

        # Sağ taraf: eklenen linklerin listesi
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Eklenen Linkler"))

        self.playlist_widget = QListWidget()
        self.playlist_widget.itemDoubleClicked.connect(self.play_from_list)
        right_layout.addWidget(self.playlist_widget, stretch=1)

        remove_btn = QPushButton("Seçileni Sil")
        remove_btn.clicked.connect(self.remove_selected_entry)
        right_layout.addWidget(remove_btn)

        outer_layout.addWidget(right_widget, stretch=1)

        # VLC instance
        # aout modülünü açıkça belirtiyoruz: embedded/python-vlc bazen sistemin
        # varsayılanından farklı (ve sessiz kalan) bir ses çıkışı seçebiliyor.
        self.vlc_instance = vlc.Instance("--no-xlib", "--aout=pulse")
        self.media_player = self.vlc_instance.media_player_new()
        self._embed_video()

    def _build_menu(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("Dosya")

        save_action = QAction("M3U olarak kaydet...", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.save_as_m3u)
        file_menu.addAction(save_action)

        load_action = QAction("M3U yükle...", self)
        load_action.triggered.connect(self.load_from_m3u)
        file_menu.addAction(load_action)

    def _embed_video(self):
        # Linux X11 embedding
        if sys.platform.startswith("linux"):
            self.media_player.set_xwindow(int(self.video_frame.winId()))
        elif sys.platform == "win32":
            self.media_player.set_hwnd(int(self.video_frame.winId()))
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

    def set_status(self, text: str):
        self.status_label.setText(text)
        QApplication.processEvents()

    def play_link(self):
        raw = self.link_input.text()
        if not raw.strip():
            return

        self.play_btn.setEnabled(False)
        try:
            self.set_status("Link ayrıştırılıyor...")
            link_type, value = classify_link(raw)

            if link_type == "acestream":
                self.set_status("Acestream Engine'den stream isteniyor... (engine çalışıyor mu?)")
                stream_url = get_stream_url(value)
            else:
                stream_url = value

            self.current_stream_url = stream_url
            self.current_link_type = link_type
            self.current_content_id = value if link_type == "acestream" else None

            if self.record_process is not None:
                # Yeni bir link oynatılıyor: eski kaydı da düzgünce kapat.
                self._stop_ffmpeg_recording()

            self.set_status(f"Oynatılıyor: {stream_url}")
            media = self.vlc_instance.media_new(stream_url)
            self.media_player.set_media(media)
            self.media_player.play()
            self.media_player.audio_set_mute(False)
            self.media_player.audio_set_volume(self.volume_slider.value())

        except requests.exceptions.ConnectionError:
            self.set_status("Hata: Engine'e bağlanılamadı.")
            QMessageBox.critical(
                self, "Engine bulunamadı",
                f"127.0.0.1:{ENGINE_PORT} adresinde Acestream Engine bulunamadı.\n\n"
                "Engine'in kurulu ve çalışır durumda olduğundan emin ol:\n"
                "  paru -S acestream-engine\n"
                "  acestreamengine --client-console"
            )
        except Exception as e:
            self.set_status(f"Hata: {e}")
            QMessageBox.warning(self, "Hata", str(e))
        finally:
            self.play_btn.setEnabled(True)

    def stop_playback(self):
        self.media_player.stop()
        if self.record_process is not None:
            self._stop_ffmpeg_recording()
        self.set_status("Durduruldu.")

    def change_volume(self, value: int):
        self.media_player.audio_set_volume(value)
        if value > 0 and self.media_player.audio_get_mute():
            self.media_player.audio_set_mute(False)
        self.mute_btn.setText("🔇" if value == 0 else "🔊")

    def toggle_mute(self):
        muted = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not muted)
        self.mute_btn.setText("🔇" if not muted else "🔊")

    def toggle_recording(self):
        if self.is_recording:
            self._stop_ffmpeg_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if not self.current_stream_url:
            QMessageBox.warning(self, "Yayın yok", "Önce bir yayın oynat, sonra kaydı başlat.")
            return

        if shutil.which("ffmpeg") is None:
            QMessageBox.critical(
                self, "ffmpeg bulunamadı",
                "Kayıt için ffmpeg gerekiyor ama sistemde bulunamadı.\n\n"
                "Kurmak için:\n  sudo pacman -S ffmpeg"
            )
            return

        # Ekrandaki oynatma zaten aynı URL'yi kullanıyor; Acestream Engine aynı
        # URL'ye ikinci bir eşzamanlı bağlantıyı desteklemiyor, bu yüzden kayıt
        # için engine'den bağımsız, yeni bir oturum/URL istiyoruz.
        record_url = self.current_stream_url
        if self.current_link_type == "acestream" and self.current_content_id:
            try:
                self.set_status("Kayıt için engine'den ayrı bir oturum isteniyor...")
                record_url = get_stream_url(self.current_content_id)
            except Exception as e:
                QMessageBox.critical(
                    self, "Hata",
                    f"Kayıt için engine'den yeni oturum alınamadı:\n{e}"
                )
                return

        default_name = f"kayit_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.ts"
        path, _ = QFileDialog.getSaveFileName(
            self, "Kaydı nereye yazayım?", default_name,
            "MPEG-TS - en dayanıklı (*.ts);;Matroska (*.mkv);;MP4 (*.mp4)"
        )
        if not path:
            return

        if os.path.splitext(path)[1] == "":
            path += ".ts"

        fmt, extra_args = ffmpeg_args_for_path(path)

        cmd = [
            "ffmpeg", "-y",
            "-seekable", "0",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-user_agent", "VLC/3.0.20 LibVLC/3.0.20",
            "-i", record_url,
            "-c", "copy",
            "-f", fmt,
        ] + extra_args + [path]

        try:
            self.record_log_file = tempfile.NamedTemporaryFile(
                mode="w+", suffix=".log", prefix="acestream_ffmpeg_", delete=False
            )
            self.record_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=self.record_log_file,
                stderr=self.record_log_file,
            )
            self.record_path = path

            self.is_recording = True
            self.record_btn.setText("■ Kaydı Durdur")
            self.set_status(f"Kayıt başladı: {path}")
            self.record_monitor_timer.start()

            if fmt == "mp4":
                QMessageBox.information(
                    self, "Not",
                    "MP4 seçtin: aniden kesilme ihtimaline karşı fragmented mp4 "
                    "modunda kaydediyorum, ama yine de en dayanıklı seçenek .ts."
                )
        except Exception as e:
            self.is_recording = False
            self.record_process = None
            QMessageBox.critical(self, "Hata", f"Kayıt başlatılamadı:\n{e}")

    def _check_recording_alive(self):
        """ffmpeg beklenmedik şekilde kapandıysa kullanıcıyı uyar."""
        if self.record_process is None:
            return
        ret = self.record_process.poll()
        if ret is not None:
            # Süreç kendi kendine bitti (hata ya da kaynak koptu)
            log_tail = ""
            try:
                if self.record_log_file:
                    self.record_log_file.flush()
                    with open(self.record_log_file.name, "r") as f:
                        log_tail = f.read()[-1500:]
            except Exception:
                pass
            self._stop_ffmpeg_recording(already_dead=True)
            self.set_status("Kayıt beklenmedik şekilde durdu.")
            QMessageBox.warning(
                self, "Kayıt durdu",
                f"ffmpeg beklenmedik şekilde kapandı (çıkış kodu: {ret}).\n\n"
                f"Son loglar:\n{log_tail}"
            )

    def _stop_ffmpeg_recording(self, already_dead: bool = False):
        self.record_monitor_timer.stop()
        proc = self.record_process
        self.record_process = None
        self.is_recording = False
        self.record_btn.setText("● Kayıt Başlat")

        if proc is not None and not already_dead:
            try:
                # Nazikçe durdur: ffmpeg'e SIGINT göndermek dosyayı düzgün kapatır
                # (Ctrl+C ile durdurmakla aynı etki - trailer/moov atom yazılır).
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

        if self.record_log_file is not None:
            try:
                self.record_log_file.close()
                os.unlink(self.record_log_file.name)
            except Exception:
                pass
            self.record_log_file = None

        if not already_dead:
            self.set_status("Kayıt durduruldu.")

    def add_current_link_to_list(self):
        raw = self.link_input.text()
        if not raw.strip():
            return
        try:
            link_type, value = classify_link(raw)
        except Exception as e:
            QMessageBox.warning(self, "Hata", str(e))
            return

        name, ok = QInputDialog.getText(
            self, "Kanal adı", "Bu link için bir isim gir:", text=f"Kanal {len(self.playlist_entries) + 1}"
        )
        if not ok:
            return
        if not name.strip():
            name = value

        self._add_entry(name.strip(), link_type, value)

    def _add_entry(self, name: str, link_type: str, value: str):
        self.playlist_entries.append({"name": name, "type": link_type, "value": value})
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, {"type": link_type, "value": value})
        self.playlist_widget.addItem(item)

    def remove_selected_entry(self):
        row = self.playlist_widget.currentRow()
        if row < 0:
            return
        self.playlist_widget.takeItem(row)
        del self.playlist_entries[row]

    def play_from_list(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data["type"] == "acestream":
            self.link_input.setText(f"acestream://{data['value']}")
        else:
            self.link_input.setText(data["value"])
        self.play_link()

    def save_as_m3u(self):
        if not self.playlist_entries:
            QMessageBox.information(self, "Liste boş", "Kaydedilecek link yok. Önce 'Listeye Ekle' ile link ekle.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "M3U olarak kaydet", "playlist.m3u", "M3U dosyası (*.m3u)")
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
        path, _ = QFileDialog.getOpenFileName(self, "M3U yükle", "", "M3U dosyası (*.m3u *.m3u8)")
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines()]
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Dosya okunamadı:\n{e}")
            return

        pending_name = None
        loaded = 0
        for line in lines:
            if line.startswith("#EXTINF"):
                # #EXTINF:-1,İsim
                if "," in line:
                    pending_name = line.split(",", 1)[1].strip()
                else:
                    pending_name = None
            elif line and not line.startswith("#"):
                try:
                    link_type, value = classify_link(line)
                except ValueError:
                    continue
                name = pending_name or value
                self._add_entry(name, link_type, value)
                pending_name = None
                loaded += 1

        self.set_status(f"{loaded} link M3U'dan yüklendi.")

    def closeEvent(self, event):
        if self.record_process is not None:
            self._stop_ffmpeg_recording()
        self.media_player.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = AcestreamPlayer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
