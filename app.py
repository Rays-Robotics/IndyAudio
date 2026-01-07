#!/usr/bin/env python3
import sys
import os
import subprocess
import time
import signal
from shutil import which
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QWidget, QFileDialog, QSlider
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap, QIcon, QPainter, QPainterPath
from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.flac import FLAC

# === CONFIG — absolute asset paths (user-specified) ===
DEFAULT_ALBUM_ART = Path("/home/ray/IndyAudio/defualt_album.jpg")
APP_ICON = Path("/home/ray/IndyAudio/ico.svg")
# =======================================================

def get_duration_with_ffprobe(path):
    if which("ffprobe") is None:
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        return float(out.strip()) if out else 0.0
    except Exception:
        return 0.0

class SeekSlider(QSlider):
    def __init__(self, orientation, parent=None, click_callback=None):
        super().__init__(orientation, parent)
        self.click_callback = click_callback

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            w = self.width()
            x = event.position().x() if hasattr(event, "position") else event.x()
            ratio = max(0.0, min(1.0, x / w))
            val = int(round(ratio * (self.maximum() - self.minimum()))) + self.minimum()
            self.setValue(val)
            if callable(self.click_callback):
                self.click_callback()
        super().mousePressEvent(event)

def rounded_pixmap(src_pixmap: QPixmap, size: int, radius: int):
    if src_pixmap.isNull():
        p = QPixmap(size, size)
        p.fill(Qt.GlobalColor.darkGray)
        return p
    target = QPixmap(size, size)
    target.fill(Qt.GlobalColor.transparent)
    painter = QPainter(target)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(0.0, 0.0, float(size), float(size), float(radius), float(radius))
    painter.setClipPath(path)
    src_scaled = src_pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    sx = (src_scaled.width() - size) // 2
    sy = (src_scaled.height() - size) // 2
    painter.drawPixmap(-sx, -sy, src_scaled)
    painter.end()
    return target

class AudioPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IndyAudio")
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.setGeometry(300, 100, 600, 600)

        # playback state
        self.current_file = None
        self.process = None
        self.start_time = 0.0
        self.pause_time = 0.0
        self.length = 0.0

        # UI
        self.title_label = QLabel("No track loaded")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.album_size = 360
        self.album_radius = 20
        self.album_art_label = QLabel()
        self.album_art_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.album_art_label.setFixedSize(self.album_size, self.album_size)
        self.album_art_label.setPixmap(self.load_default_pixmap())

        # Play/Pause toggle button
        self.play_pause_btn = QPushButton()
        self.play_icon = QIcon.fromTheme("media-playback-start")
        self.pause_icon = QIcon.fromTheme("media-playback-pause")
        if not self.play_icon or self.play_icon.isNull():
            self.play_pause_btn.setText("Play")
        else:
            self.play_pause_btn.setIcon(self.play_icon)
        self.play_pause_btn.setToolTip("Play / Pause")
        self.play_pause_btn.clicked.connect(self.play_pause_toggle)

        # Open button
        self.open_btn = QPushButton()
        ico_open = QIcon.fromTheme("document-open")
        if ico_open and not ico_open.isNull():
            self.open_btn.setIcon(ico_open)
        else:
            self.open_btn.setText("Open")
        self.open_btn.setToolTip("Open file")
        self.open_btn.clicked.connect(self.open_file_dialog)

        # Slider + time
        self.slider = SeekSlider(Qt.Orientation.Horizontal, click_callback=self.scrub)
        self.slider.setRange(0, 1000)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self.slider_pressed)
        self.slider.sliderReleased.connect(self.scrub)
        self.slider.sliderMoved.connect(self.update_preview_time)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Layout
        v_layout = QVBoxLayout()
        v_layout.addWidget(self.album_art_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        v_layout.addWidget(self.title_label)
        v_layout.addWidget(self.time_label)
        v_layout.addWidget(self.slider)

        h_layout = QHBoxLayout()
        h_layout.addWidget(self.play_pause_btn)
        h_layout.addWidget(self.open_btn)
        v_layout.addLayout(h_layout)

        container = QWidget()
        container.setLayout(v_layout)
        self.setCentralWidget(container)

        # timer
        self.timer = QTimer()
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.update_slider)
        self.timer.start()

        self.slider_dragging = False

    # UI helpers
    def load_default_pixmap(self):
        if DEFAULT_ALBUM_ART.exists():
            pix = QPixmap(str(DEFAULT_ALBUM_ART))
        else:
            p = QPixmap(self.album_size, self.album_size)
            p.fill(Qt.GlobalColor.darkGray)
            pix = p
        return rounded_pixmap(pix, self.album_size, self.album_radius)

    @staticmethod
    def format_time(seconds):
        m, s = divmod(int(seconds), 60)
        return f"{m:02}:{s:02}"

    # file handling
    def open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Audio File", "", "Audio Files (*.mp3 *.flac *.wav *.ogg *.aac *.m4a *.wma)"
        )
        if path:
            self.load_file(path)

    def load_file(self, path):
        self.stop_internal()
        self.current_file = Path(path)
        self.start_time = 0.0
        self.pause_time = 0.0
        self.slider.setEnabled(False)

        self.extract_metadata(path)

        # duration detection
        audio = MutagenFile(path)
        length = 0.0
        try:
            if audio and getattr(audio, "info", None):
                length = float(audio.info.length)
        except Exception:
            length = 0.0
        if length <= 0.0001:
            length = get_duration_with_ffprobe(path)
        self.length = length if length > 0 else 0.0
        self.slider.setEnabled(self.length > 0)

        # start playback
        self.start_ffplay(self.pause_time)
        self.update_play_pause_icon(playing=True)

    def extract_metadata(self, path):
        file_name = os.path.basename(path)
        title = file_name
        artist = ""

        # Use real default image as fallback
        if DEFAULT_ALBUM_ART.exists():
            pixmap = QPixmap(str(DEFAULT_ALBUM_ART))
        else:
            pixmap = QPixmap(self.album_size, self.album_size)
            pixmap.fill(Qt.GlobalColor.darkGray)

        audio = MutagenFile(path)
        try:
            if audio:
                try:
                    tags = ID3(path)
                    if tags:
                        title = tags.get("TIT2").text[0] if tags.get("TIT2") else title
                        artist = tags.get("TPE1").text[0] if tags.get("TPE1") else artist
                        pics = tags.getall("APIC")
                        if pics:
                            tmp = QPixmap()
                            if tmp.loadFromData(pics[0].data):
                                pixmap = tmp
                except Exception:
                    if isinstance(audio, FLAC):
                        title = audio.get("title", [file_name])[0]
                        artist = audio.get("artist", [""])[0]
                        pics = audio.pictures
                        if pics:
                            tmp = QPixmap()
                            if tmp.loadFromData(pics[0].data):
                                pixmap = tmp
        except Exception as e:
            print("Metadata error:", e)

        self.title_label.setText(f"{title} - {artist}" if artist else title)
        rounded = rounded_pixmap(pixmap, self.album_size, self.album_radius)
        self.album_art_label.setPixmap(rounded)

    # ffplay control
    def start_ffplay(self, start_sec: float = 0.0):
        self.kill_ffplay()
        if not self.current_file:
            return
        cmd = [
            "ffplay", "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "quiet",
            "-ss", str(start_sec), str(self.current_file)
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            self.start_time = time.time() - start_sec
            self.update_play_pause_icon(playing=True)
        except Exception as e:
            print("Failed to start ffplay:", e)
            self.process = None
            self.update_play_pause_icon(playing=False)

    def kill_ffplay(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            try:
                self.process.wait(timeout=0.5)
            except Exception:
                pass
            self.process = None
        self.update_play_pause_icon(playing=False)

    # play/pause toggle
    def play_pause_toggle(self):
        if not self.current_file:
            return
        if self.process and self.process.poll() is None:
            # store current playback position when pausing (no doubling)
            self.pause_time = time.time() - self.start_time
            self.pause_time = max(0.0, min(self.pause_time, self.length))
            self.kill_ffplay()
        else:
            self.start_ffplay(self.pause_time)



    def update_play_pause_icon(self, playing: bool):
        if playing:
            if self.pause_icon and not self.pause_icon.isNull():
                self.play_pause_btn.setIcon(self.pause_icon)
                self.play_pause_btn.setText("")
            else:
                self.play_pause_btn.setText("Pause")
        else:
            if self.play_icon and not self.play_icon.isNull():
                self.play_pause_btn.setIcon(self.play_icon)
                self.play_pause_btn.setText("")
            else:
                self.play_pause_btn.setText("Play")

    # internal stop (no UI button)
    def stop_internal(self):
        self.kill_ffplay()
        self.start_time = 0.0
        self.pause_time = 0.0
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.time_label.setText("00:00 / " + self.format_time(self.length if self.length else 0))
        self.update_play_pause_icon(playing=False)

    # scrub/slider
    def slider_pressed(self):
        self.slider_dragging = True

    def update_preview_time(self, slider_val):
        if self.length and self.slider_dragging:
            pos_ratio = slider_val / 1000.0
            pos_seconds = pos_ratio * self.length
            self.time_label.setText(f"{self.format_time(pos_seconds)} / {self.format_time(self.length)}")

    def scrub(self):
        self.slider_dragging = False
        if not self.current_file or self.length == 0:
            return
        pos_ratio = self.slider.value() / 1000.0
        pos_seconds = pos_ratio * self.length
        self.pause_time = max(0.0, min(pos_seconds, self.length))
        self.start_ffplay(self.pause_time)

    # update slider
    def update_slider(self):
        if not self.current_file or self.length == 0 or self.slider_dragging:
            return
        if self.process and self.process.poll() is None:
            elapsed = time.time() - self.start_time
            elapsed = max(0.0, elapsed)
        else:
            elapsed = self.pause_time
        elapsed = min(elapsed, self.length)
        self.slider.blockSignals(True)
        self.slider.setValue(int((elapsed / self.length) * 1000) if self.length > 0 else 0)
        self.slider.blockSignals(False)
        self.time_label.setText(f"{self.format_time(elapsed)} / {self.format_time(self.length)}")
        if self.process and self.process.poll() is not None:
            self.pause_time = self.length
            self.kill_ffplay()

    def closeEvent(self, event):
        self.kill_ffplay()
        event.accept()

def main():
    app = QApplication(sys.argv)
    win = AudioPlayer()
    # handle file passed from desktop file or double-click
    if len(sys.argv) > 1:
        maybe_path = sys.argv[1]
        if maybe_path and os.path.exists(maybe_path):
            win.load_file(maybe_path)
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
