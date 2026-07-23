"""
welcome_screen.py

Idle/welcome screen shown before the functional app. Two purposes:
- On startup, a branded first impression instead of dropping straight
  into the Connection screen.
- In the field, once the rig is powered but not actively in use: an
  "attract screen" — anyone walking up taps anywhere to enter the app.
  No timeout-based auto-return to this screen is implemented (not asked
  for); this only covers the initial tap-to-enter.

A tap/click/keypress anywhere on the screen emits continue_requested;
main_window.py owns switching to the functional screens.
"""

import os

from PySide6.QtCore import Qt, QUrl, QSizeF, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGraphicsScene, QGraphicsView
)

from src.ui.style import (
    APP_NAME, APP_TAGLINE, COLOR_ACCENT, COLOR_TEXT_MUTED,
    FONT_SIZE_TITLE, FONT_SIZE_SUBTITLE, FONT_SIZE_NORMAL,
)

# Looping preview of the physical simulator, shown above the branding.
# NOT the raw source in assets/videos/source/MOTION360.mp4 — that file
# is uncompressed rawvideo (1900x1800@15fps, 1.2GB for 8 seconds),
# unusable for a continuously-looping idle screen on the Pi, and
# gitignored (see .gitignore / CLAUDE.md "Assets" section for the
# source/ convention). This is a transcoded H.264 copy of the same
# footage (760x720, ~735KB), regenerate with:
#   ffmpeg -i source/MOTION360.mp4 -vf "scale=760:720" -c:v libx264 \
#          -crf 23 -preset medium -pix_fmt yuv420p -movflags +faststart \
#          -an motion360_welcome.mp4
VIDEO_PATH = "assets/videos/motion360_welcome.mp4"
VIDEO_DISPLAY_SIZE = (380, 360)  # same 1900:1800 aspect ratio, no distortion


class WelcomeScreen(QWidget):
    """Fullscreen branded idle screen. Tap anywhere to continue."""

    continue_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self._player = None  # only set if VIDEO_PATH exists, see _build_video
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(18)

        video_widget = self._build_video()
        if video_widget is not None:
            layout.addWidget(video_widget, alignment=Qt.AlignCenter)

        wordmark = QLabel(APP_NAME)
        wordmark.setAlignment(Qt.AlignCenter)
        wordmark.setStyleSheet(
            f"font-size: {FONT_SIZE_TITLE}px; font-weight: 700; "
            f"letter-spacing: 6px; color: {COLOR_ACCENT};"
        )

        tagline = QLabel(APP_TAGLINE)
        tagline.setAlignment(Qt.AlignCenter)
        tagline.setStyleSheet(
            f"font-size: {FONT_SIZE_SUBTITLE}px; color: {COLOR_TEXT_MUTED};"
        )

        hint = QLabel("Toca la pantalla para comenzar")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED}; "
            f"border: 1px solid {COLOR_TEXT_MUTED}; border-radius: 18px; "
            f"padding: 10px 24px; margin-top: 40px;"
        )

        layout.addWidget(wordmark)
        layout.addWidget(tagline)
        layout.addWidget(hint, alignment=Qt.AlignCenter)

    def _build_video(self):
        """
        Builds the looping video preview, or returns None if the asset
        is missing (e.g. a fresh checkout without assets/) — the welcome
        screen degrades gracefully to text-only rather than crashing.

        Uses QGraphicsVideoItem/QGraphicsView rather than QVideoWidget:
        QVideoWidget composites via a native window surface, which some
        QPA backends render as a blank rectangle (confirmed on this
        machine's offscreen backend, used for automated smoke tests —
        see main_window docs/session notes). QGraphicsVideoItem paints
        through QPainter like any other graphics item, verified to
        actually render across backends.
        """
        if not os.path.isfile(VIDEO_PATH):
            return None

        width, height = VIDEO_DISPLAY_SIZE
        scene = self._video_scene = QGraphicsScene(self)
        item = QGraphicsVideoItem()
        item.setSize(QSizeF(width, height))
        scene.addItem(item)
        scene.setSceneRect(0, 0, width, height)

        view = QGraphicsView(scene)
        view.setFixedSize(width, height)
        view.setFrameShape(QGraphicsView.NoFrame)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setInteractive(False)
        view.setStyleSheet(f"border: 1px solid {COLOR_TEXT_MUTED};")
        # Mouse events must reach WelcomeScreen.mousePressEvent (tap
        # anywhere, including over the video, advances past this screen).
        view.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._player = QMediaPlayer(self)
        audio_output = QAudioOutput(self)
        audio_output.setMuted(True)  # idle/attract loop — never plays sound
        self._player.setAudioOutput(audio_output)
        self._player.setVideoOutput(item)
        self._player.setSource(QUrl.fromLocalFile(os.path.abspath(VIDEO_PATH)))
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self._player.play()

        return view

    # ------------------------------------------------------------------
    # Any interaction advances past this screen.
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        self.continue_requested.emit()

    def keyPressEvent(self, event):
        self.continue_requested.emit()

    # ------------------------------------------------------------------
    # Pause the loop while this screen isn't visible instead of wasting
    # CPU/memory bandwidth decoding video no one sees for the rest of
    # the session. Also means a future idle-timeout-return-to-welcome
    # feature (not built — not asked for) would resume playback for free.
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self._player is not None:
            self._player.play()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._player is not None:
            self._player.pause()
