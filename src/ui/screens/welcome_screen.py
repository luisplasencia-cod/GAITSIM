"""
welcome_screen.py

Idle/welcome screen shown before the functional app. Two purposes:
- On startup, a branded first impression instead of dropping straight
  into the Connection screen.
- In the field, once the rig is powered but not actively in use: an
  "attract screen" — anyone walking up taps anywhere to enter the app.
  No timeout-based auto-return to this screen is implemented (not asked
  for); this only covers the initial tap-to-enter.

A tap/click/keypress anywhere on the screen advances past this screen
(_advance -> continue_requested, after a brief tap-acknowledgment flash);
main_window.py owns switching to the functional screens.

Redesign (2026-07-25) — "instrument datasheet" concept, approved design
plan, purely visual (no behavior/flow changes): the CAD preview video is
no longer a bordered box floating above centered text. It anchors an
asymmetric composition — video bled to the right two-thirds of the
screen, brand/tagline/tap-hint left-aligned in a narrower left column
with a technical grid texture and a brick-red accent rule pulled from
the rig's own steel frame (see COLOR_RIG_ACCENT below). That accent is
deliberately screen-local, not added to style.py's shared palette — see
its own comment for why.
"""

import os

from PySide6.QtCore import (
    Qt, QUrl, QSizeF, QPoint, QTimer, Signal,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve,
)
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QGraphicsScene, QGraphicsView, QGraphicsRectItem, QGraphicsOpacityEffect,
)

from src.ui.style import (
    APP_NAME, APP_TAGLINE, COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_BORDER,
    COLOR_BG, FONT_SIZE_TITLE, FONT_SIZE_SUBTITLE, FONT_SIZE_HINT,
)

# Brick-red pulled directly from the physical rig's steel frame, as seen
# in the CAD preview video below — the one decisive accent on this
# screen (vertical rule + tap-hint dot only). Deliberately NOT
# style.py's COLOR_ACCENT (the app-wide blue, which means "interactive
# control" everywhere else in the app — HOME, CONNECT, RUN) and not
# added to style.py at all: this ties the welcome screen visually to
# the hardware itself without diluting blue's single meaning elsewhere,
# or touching the X/Y/angle plot palette (also untouched).
COLOR_RIG_ACCENT = "#b23b34"

# Looping preview of the physical simulator. NOT the raw source in
# assets/videos/source/MOTION360.mp4 — that file is uncompressed
# rawvideo (1900x1800@15fps, 1.2GB for 8 seconds), unusable for a
# continuously-looping idle screen on the Pi, and gitignored (see
# .gitignore / CLAUDE.md "Assets" section for the source/ convention).
# This is a transcoded H.264 copy of the same footage, regenerate with:
#   ffmpeg -i source/MOTION360.mp4 -vf "scale=760:720" -c:v libx264 \
#          -crf 23 -preset medium -pix_fmt yuv420p -movflags +faststart \
#          -an motion360_welcome.mp4
VIDEO_PATH = "assets/videos/motion360_welcome.mp4"
# 2x the pre-redesign preview size (was 380x360) — bled to the edge of a
# wider right column now instead of a small centered box; same 1900:1800
# aspect ratio throughout, so this stays undistorted.
VIDEO_DISPLAY_SIZE = (760, 720)
# Width (px, in the video's own local/scene coordinates) of the left-edge
# gradient that feathers the CAD render's navy background into COLOR_BG,
# so the video reads as bleeding into the screen rather than sitting in
# a box with a visible seam.
VIDEO_FEATHER_PX = 140

LEFT_PANEL_WIDTH = 440
LEFT_PANEL_MARGIN = 72
GRID_SPACING_PX = 48

TAP_FLASH_MS = 120           # full-screen tap-acknowledgment flash
ENTRANCE_DURATION_MS = 400
ENTRANCE_STAGGER_MS = 120
PULSE_DURATION_MS = 1600


class _PulsingDot(QWidget):
    """
    Small filled circle, continuously pulsing opacity — the one ongoing
    motion on this screen, deliberately drawing the eye to the tap-to-
    continue affordance without relying on hover (this runs on a
    touch-only kiosk panel, not a desktop with a mouse).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setKeyValueAt(0.0, 1.0)
        self._anim.setKeyValueAt(0.5, 0.4)
        self._anim.setKeyValueAt(1.0, 1.0)
        self._anim.setDuration(PULSE_DURATION_MS)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.setLoopCount(-1)

    def start(self):
        self._anim.start()

    def stop(self):
        self._anim.stop()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(COLOR_RIG_ACCENT)))
        painter.drawEllipse(self.rect())


class _TapHint(QWidget):
    """Pulsing dot + short uppercase label — the tap-to-continue
    affordance, placed bottom-left of the brand column rather than
    center-screen (see module docstring for the composition rationale)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._dot = _PulsingDot()
        layout.addWidget(self._dot, alignment=Qt.AlignVCenter)

        label = QLabel("TOCA PARA COMENZAR")
        font = QFont()
        font.setPointSize(FONT_SIZE_HINT)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 2)
        label.setFont(font)
        label.setStyleSheet(f"color: {COLOR_TEXT_MUTED()};")
        layout.addWidget(label, alignment=Qt.AlignVCenter)

    def start_pulse(self):
        self._dot.start()

    def stop_pulse(self):
        self._dot.stop()


class _LeftPanel(QWidget):
    """
    Left column: brand + tagline + tap hint, over a faint technical-grid
    texture with a brick-red vertical rule along the left edge — see
    module docstring for the full design rationale.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(LEFT_PANEL_WIDTH)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            LEFT_PANEL_MARGIN, LEFT_PANEL_MARGIN, 24, LEFT_PANEL_MARGIN
        )
        layout.setSpacing(0)
        layout.addStretch(2)

        self.wordmark = QLabel(APP_NAME)
        wordmark_font = QFont("DejaVu Sans Condensed")
        wordmark_font.setBold(True)
        wordmark_font.setPointSize(FONT_SIZE_TITLE)
        # Verified (see style.py's FONT_SIZE_TITLE comment) that
        # FONT_SIZE_TITLE at this spacing fits within LEFT_PANEL_WIDTH
        # minus its margins without QLabel silently clipping the text.
        wordmark_font.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        self.wordmark.setFont(wordmark_font)
        self.wordmark.setStyleSheet(f"color: {COLOR_TEXT()};")

        self.tagline = QLabel(APP_TAGLINE.upper())
        # Wraps rather than relying on a precisely-tuned font size to
        # just barely fit LEFT_PANEL_WIDTH — a QLabel silently CLIPS
        # (not wraps) by default when its content exceeds the space the
        # layout gives it, which is exactly what happened here before
        # setWordWrap was added (verified: "MARCHA" was being cut off).
        self.tagline.setWordWrap(True)
        tagline_font = QFont()
        tagline_font.setPointSize(FONT_SIZE_SUBTITLE)
        tagline_font.setLetterSpacing(QFont.AbsoluteSpacing, 2)
        self.tagline.setFont(tagline_font)
        self.tagline.setStyleSheet(f"color: {COLOR_TEXT_MUTED()}; margin-top: 10px;")

        layout.addWidget(self.wordmark)
        layout.addWidget(self.tagline)
        layout.addStretch(3)

        self.tap_hint = _TapHint()
        layout.addWidget(self.tap_hint, alignment=Qt.AlignLeft)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLOR_BG()))

        # Faint blueprint-style grid — evokes an engineering drawing;
        # low-contrast enough to read as texture, not a literal grid.
        grid_color = QColor(COLOR_BORDER())
        grid_color.setAlpha(60)
        painter.setPen(QPen(grid_color, 1))
        for gx in range(0, self.width(), GRID_SPACING_PX):
            painter.drawLine(gx, 0, gx, self.height())
        for gy in range(0, self.height(), GRID_SPACING_PX):
            painter.drawLine(0, gy, self.width(), gy)

        # Brick-red vertical rule, left edge — the one decisive accent,
        # echoing the physical rig's steel frame.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(COLOR_RIG_ACCENT)))
        painter.drawRect(0, 0, 3, self.height())


class WelcomeScreen(QWidget):
    """Fullscreen branded idle screen. Tap anywhere to continue."""

    continue_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self._player = None  # only set if VIDEO_PATH exists, see _build_video
        self._advancing = False
        self._entrance_anims = []
        self._flash_anim = None
        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet(f"background-color: {COLOR_BG()};")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._left_panel = _LeftPanel()
        layout.addWidget(self._left_panel, 0)

        video_container = self._build_video()
        if video_container is not None:
            layout.addWidget(video_container, 1)
        else:
            layout.addStretch(1)

        # Full-screen tap-acknowledgment overlay (see _play_tap_flash) —
        # created last so raise_() always puts it above the panel/video.
        self._flash_overlay = QWidget(self)
        self._flash_overlay.setStyleSheet("background-color: black;")
        self._flash_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._flash_effect = QGraphicsOpacityEffect(self._flash_overlay)
        self._flash_overlay.setGraphicsEffect(self._flash_effect)
        self._flash_effect.setOpacity(0.0)
        self._flash_overlay.hide()

    def _build_video(self):
        """
        Builds the looping video preview, bled to the right two-thirds
        of the screen, or returns None if the asset is missing (e.g. a
        fresh checkout without assets/) — the welcome screen degrades
        gracefully to left-column-only rather than crashing.

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
        scene.setBackgroundBrush(QColor(COLOR_BG()))
        item = QGraphicsVideoItem()
        item.setSize(QSizeF(width, height))
        scene.addItem(item)

        # Feathers the video's left edge into the surrounding app
        # background (the CAD render's own navy background is close but
        # not identical to COLOR_BG) — part of the SAME scene as the
        # video item (not a separate overlay widget), so it composites
        # for free through whatever backend renders QGraphicsVideoItem.
        feather = QGraphicsRectItem(0, 0, VIDEO_FEATHER_PX, height)
        bg = QColor(COLOR_BG())
        transparent_bg = QColor(bg)
        transparent_bg.setAlpha(0)
        gradient = QLinearGradient(0, 0, VIDEO_FEATHER_PX, 0)
        gradient.setColorAt(0.0, bg)
        gradient.setColorAt(1.0, transparent_bg)
        feather.setBrush(QBrush(gradient))
        feather.setPen(Qt.NoPen)
        feather.setZValue(1)
        scene.addItem(feather)

        scene.setSceneRect(0, 0, width, height)

        view = QGraphicsView(scene)
        view.setFixedSize(width, height)
        view.setFrameShape(QGraphicsView.NoFrame)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setInteractive(False)
        # No border/frame (unlike the pre-redesign boxed preview) — the
        # feather gradient above does the visual-integration work instead.
        view.setStyleSheet(f"background-color: {COLOR_BG()}; border: none;")
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

        # Vertically centers the (roughly square) video within the full
        # screen height, letterboxing top/bottom against COLOR_BG rather
        # than stretching/cropping — same aspect ratio as the source.
        container = QWidget()
        container.setStyleSheet(f"background-color: {COLOR_BG()};")
        vlayout = QVBoxLayout(container)
        vlayout.setContentsMargins(0, 0, 0, 0)
        vlayout.addStretch(1)
        vlayout.addWidget(view, alignment=Qt.AlignHCenter)
        vlayout.addStretch(1)
        return container

    # ------------------------------------------------------------------
    # Any interaction advances past this screen, with a brief tap-
    # acknowledgment flash first so the touch feels immediately
    # registered (see _play_tap_flash) rather than relying solely on the
    # screen swap itself as feedback.
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        self._advance()

    def keyPressEvent(self, event):
        self._advance()

    def _advance(self):
        if self._advancing:
            return
        self._advancing = True
        self._play_tap_flash()
        QTimer.singleShot(TAP_FLASH_MS, self.continue_requested.emit)

    def _play_tap_flash(self):
        self._flash_overlay.setGeometry(self.rect())
        self._flash_overlay.raise_()
        self._flash_overlay.show()
        anim = QPropertyAnimation(self._flash_effect, b"opacity", self)
        anim.setKeyValueAt(0.0, 0.0)
        anim.setKeyValueAt(0.4, 0.35)
        anim.setKeyValueAt(1.0, 0.0)
        anim.setDuration(TAP_FLASH_MS)
        anim.finished.connect(self._flash_overlay.hide)
        anim.start()
        self._flash_anim = anim  # keep alive past this method's return

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._flash_overlay.setGeometry(self.rect())

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
        self._advancing = False
        self._left_panel.tap_hint.start_pulse()
        self._start_entrance_animation()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._player is not None:
            self._player.pause()
        self._left_panel.tap_hint.stop_pulse()

    def _start_entrance_animation(self):
        """
        Staggered fade+rise reveal on each show (wordmark, then tagline)
        — approved design plan's entrance motion. Widgets are already
        positioned by their QVBoxLayout by the time showEvent fires;
        this nudges each one 12px below its final spot while invisible
        (opacity 0, so the offset itself is never seen), then animates
        back to that exact layout-assigned position. Safe because
        nothing triggers a relayout during the ~500ms this plays out
        (fixed-size kiosk window, no resize mid-animation).

        Deliberately excludes tap_hint (unlike the original plan's
        3-element stagger): tap_hint contains _PulsingDot, which has its
        OWN separate, perpetually-running QGraphicsOpacityEffect for the
        pulse. Verified empirically (offscreen QPA backend) that EVER
        attaching a second QGraphicsOpacityEffect to tap_hint itself —
        even opacity-only, even after the dot's pulse starts later —
        causes a QPainter reentrancy ("Painter not active" / dropped
        paints) once the dot's own effect is animating. Simplest robust
        fix: tap_hint gets no entrance effect at all and starts pulsing
        immediately (see showEvent) — still a legitimate reveal via
        wordmark+tagline, without risking corrupted rendering.
        """
        widgets = [
            self._left_panel.wordmark,
            self._left_panel.tagline,
        ]
        self._entrance_anims = []
        for i, widget in enumerate(widgets):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            effect.setOpacity(0.0)

            final_pos = widget.pos()
            start_pos = final_pos + QPoint(0, 12)
            widget.move(start_pos)

            fade = QPropertyAnimation(effect, b"opacity", self)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setDuration(ENTRANCE_DURATION_MS)
            fade.setEasingCurve(QEasingCurve.OutCubic)

            move = QPropertyAnimation(widget, b"pos", self)
            move.setStartValue(start_pos)
            move.setEndValue(final_pos)
            move.setDuration(ENTRANCE_DURATION_MS)
            move.setEasingCurve(QEasingCurve.OutCubic)

            group = QParallelAnimationGroup(self)
            group.addAnimation(fade)
            group.addAnimation(move)

            QTimer.singleShot(i * ENTRANCE_STAGGER_MS, group.start)
            self._entrance_anims.append(group)  # keep alive past this method's return
