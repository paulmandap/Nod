"""The heads-up display.

Design constraints, which are unusual enough to be worth stating:

  * It is read for about one second, in peripheral vision, while the user's
    eyes are meant to stay near the webcam. So: one strong line, optionally a
    few quiet ones, and nothing else.
  * It floats over arbitrary content — usually a bright shared slide deck. A
    dark smoked panel with a hairline edge stays legible over both extremes;
    a light panel does not.
  * Motion at the top of the screen pulls the eye away from the camera, which
    is the exact failure this product exists to avoid. The only animation is a
    140 ms crossfade when the content changes.

The signature element is the freshness rail on the left edge: it drains as the
suggestion ages. "Is what I'm reading still true?" is the most important
question here and the rail answers it at zero reading cost.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QWidget

from .bus import Bus, Suggestion

# --- tokens -----------------------------------------------------------------
PANEL = QColor(16, 22, 28, 219)      # cold slate glass, ~86%
EDGE = QColor(44, 58, 71, 200)
TEXT = QColor(230, 237, 243)
MUTED = QColor(134, 151, 166)
RULE = QColor(44, 58, 71, 160)

RAIL = {                              # rail colour encodes suggestion kind
    "summary": QColor(94, 200, 192),  # teal
    "term": QColor(140, 168, 214),    # slate blue
    "reply": QColor(227, 168, 87),    # amber — something is waiting on you
    # Green reads as "acting on your behalf" and is the one state where the
    # panel is reporting on itself rather than on the room.
    "agent": QColor(126, 200, 120),
}
STALE = QColor(85, 99, 111)

DISPLAY_STACK = ["Inter", "Segoe UI Variable Display", "SF Pro Display", "DejaVu Sans"]
UTILITY_STACK = ["IBM Plex Mono", "Cascadia Mono", "SF Mono", "DejaVu Sans Mono"]

WIDTH = 720
PAD = 18
RAIL_W = 3
RADIUS = 10
TOP_MARGIN = 26
# The metadata row: kind label on the left, and on the right what the mic heard.
# 14 rather than the eyebrow's own 12 so the echo text has room to sit on the
# same baseline without being clipped.
EYEBROW_H = 14
TTL = 45.0            # seconds until a suggestion reads as stale

# Agent cards are cleared outright once stale, not just greyed. A summary is a
# statement about the past ("Q3 slipped to November") and stays true however
# old it gets, so fading it is enough. An agent card is a claim about right now
# -- "Playing lofi hip hop radio" -- and once you have closed the tab it is
# simply false. Leaving it up means the overlay is asserting something untrue,
# which is worse than showing nothing.
AGENT_TTL = 60.0


def _font(stack: list[str], size: int, weight: QFont.Weight, spacing: float = 0.0):
    f = QFont()
    f.setFamilies(stack)
    f.setPixelSize(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


class HUD(QWidget):
    # Hotkeys fire on pynput's listener thread, and Qt widgets may only be
    # touched from the main thread. Emitting a signal hops the call across:
    # with a receiver on another thread Qt queues it automatically, so the
    # widget work still happens on the main thread. Same reason the suggestion
    # queue is drained by a QTimer rather than called into from a worker.
    toggle_visible = pyqtSignal()
    request_click_through = pyqtSignal()

    def __init__(self, bus: Bus) -> None:
        super().__init__()
        self.bus = bus
        self.current: Suggestion | None = None
        self.status = "listening"
        self._opacity = 1.0
        self._click_through = True

        self.f_eyebrow = _font(UTILITY_STACK, 9, QFont.Weight.Medium, 1.6)
        self.f_lead = _font(DISPLAY_STACK, 17, QFont.Weight.DemiBold, -0.2)
        self.f_point = _font(DISPLAY_STACK, 13, QFont.Weight.Normal)
        # The echo of what was heard. Mono, because this is a verbatim quote of
        # machine output rather than prose -- and unspaced and a size up from
        # the eyebrow, because the whole reason it is on screen is to be read
        # and checked against what you actually said.
        self.f_echo = _font(UTILITY_STACK, 11, QFont.Weight.Normal)

        self.toggle_visible.connect(self._toggle_visible)
        self.request_click_through.connect(self.toggle_click_through)

        self._apply_flags()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._relayout()

        self.poll = QTimer(self)
        self.poll.timeout.connect(self._drain)
        self.poll.start(120)

        self.tick = QTimer(self)          # drives the freshness rail
        self.tick.timeout.connect(self.update)
        self.tick.start(500)

        self.fade = QTimer(self)
        self.fade.timeout.connect(self._step_fade)

    # -- window behaviour ------------------------------------------------
    def _apply_flags(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool          # keeps it out of the taskbar/dock
            | Qt.WindowType.NoDropShadowWindowHint
        )
        if self._click_through:
            # Mouse events pass to whatever is underneath, so the overlay never
            # steals a click from the meeting app.
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)

    def _toggle_visible(self) -> None:
        self.setVisible(not self.isVisible())

    def toggle_click_through(self) -> None:
        self._click_through = not self._click_through
        self._apply_flags()
        self.show()

    # -- geometry --------------------------------------------------------
    def _height(self) -> int:
        if not self.current:
            return 46
        fm_lead = QFontMetrics(self.f_lead)
        fm_pt = QFontMetrics(self.f_point)
        h = PAD + EYEBROW_H + 4 + fm_lead.height()
        if self.current.points:
            h += 10 + len(self.current.points) * (fm_pt.height() + 5)
        return h + PAD

    def _relayout(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        h = self._height()
        self.setFixedSize(WIDTH, h)
        x, y = screen.center().x() - WIDTH // 2, screen.top() + TOP_MARGIN
        self.move(x, y)
        # Publish for the screen reader to mask out. Assigning a tuple is
        # atomic, so no lock is needed and no Qt object crosses the thread.
        self.bus.hud_rect = (x, y, WIDTH, h)

    # -- data ------------------------------------------------------------
    def _drain(self) -> None:
        # "hey Nod, hide" arrives as an Event set on the agent thread; acting
        # on it here keeps every widget call on the Qt main thread.
        if self.bus.hud_hide.is_set():
            self.bus.hud_hide.clear()
            self.hide()

        changed = False

        # Retire a stale agent card rather than leave it claiming something is
        # still happening. Back to the status line, which is honest.
        if self.current and self.current.kind == "agent":
            import time
            if time.time() - self.current.ts > AGENT_TTL:
                self.current = None
                changed = True
        while not self.bus.suggestions.empty():
            self.current = self.bus.suggestions.get_nowait()
            changed = True
        while not self.bus.status.empty():
            self.status = self.bus.status.get_nowait()
            changed = True
        if changed:
            self._relayout()
            self._opacity = 0.0
            self.fade.start(16)
            self.update()

    def _step_fade(self) -> None:
        self._opacity = min(1.0, self._opacity + 0.12)
        if self._opacity >= 1.0:
            self.fade.stop()
        self.update()

    def _age(self) -> float:
        if not self.current:
            return 1.0
        import time

        return min(1.0, (time.time() - self.current.ts) / TTL)

    # -- painting --------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._opacity)

        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()),
                            RADIUS, RADIUS)
        p.fillPath(path, PANEL)
        p.setPen(QPen(EDGE, 1))
        p.drawPath(path)

        age = self._age()
        base = RAIL.get(self.current.kind if self.current else "summary", RAIL["summary"])
        rail_col = STALE if age >= 1.0 else base

        # Freshness rail: full height at birth, drained to nothing at TTL.
        p.setClipPath(path)
        p.fillRect(QRect(0, 0, RAIL_W, rect.height()), QColor(rail_col.red(),
                                                              rail_col.green(),
                                                              rail_col.blue(), 45))
        live_h = int(rect.height() * (1.0 - age))
        p.fillRect(QRect(0, rect.height() - live_h, RAIL_W, live_h), rail_col)
        p.setClipping(False)

        x = RAIL_W + PAD
        w = rect.width() - x - PAD

        if not self.current:
            # Idle line doubles as the mode indicator: with runtime switching
            # there has to be somewhere to see which halves are actually live.
            # Read from Events on the Qt timer, so no cross-thread widget work.
            modes = []
            if self.bus.agent_on.is_set():
                modes.append("AGENT")
            if self.bus.meeting_on.is_set():
                modes.append("MEETING")
            label = " · ".join(modes) or "IDLE"
            p.setFont(self.f_eyebrow)
            p.setPen(MUTED)
            p.drawText(QRect(x, 0, w, rect.height()),
                       Qt.AlignmentFlag.AlignVCenter,
                       f"{label}   {self.status.upper()}")
            return

        y = PAD - 4

        # Eyebrow: the kind label is structure, not decoration — it tells you
        # whether this is context you can ignore or a question aimed at you.
        p.setFont(self.f_eyebrow)
        p.setPen(rail_col if age < 1.0 else STALE)
        p.drawText(QRect(x, y, w, EYEBROW_H),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.current.kind.upper())

        # Echo, right-aligned on the same row: what the mic actually heard.
        # It shares the row rather than taking one of its own so the lead stays
        # the first thing the eye lands on — this is for checking, not reading.
        if self.current.heard:
            fm_echo = QFontMetrics(self.f_echo)
            kind_w = QFontMetrics(self.f_eyebrow).horizontalAdvance(
                self.current.kind.upper())
            echo_x = x + kind_w + 12
            echo_w = w - kind_w - 12
            if echo_w > 60:
                p.setFont(self.f_echo)
                p.setPen(MUTED if age < 1.0 else STALE)
                p.drawText(
                    QRect(echo_x, y, echo_w, EYEBROW_H),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    fm_echo.elidedText(f"“{self.current.heard}”",
                                       Qt.TextElideMode.ElideRight, echo_w),
                )

        y += EYEBROW_H + 4

        fm_lead = QFontMetrics(self.f_lead)
        p.setFont(self.f_lead)
        p.setPen(TEXT if age < 1.0 else MUTED)
        p.drawText(QRect(x, y, w, fm_lead.height()),
                   Qt.AlignmentFlag.AlignLeft,
                   fm_lead.elidedText(self.current.lead,
                                      Qt.TextElideMode.ElideRight, w))
        y += fm_lead.height()

        if self.current.points:
            y += 5
            p.setPen(QPen(RULE, 1))
            p.drawLine(x, y, x + w, y)
            y += 5

            fm_pt = QFontMetrics(self.f_point)
            p.setFont(self.f_point)
            p.setPen(MUTED)
            for point in self.current.points:
                p.drawText(QRect(x, y, w, fm_pt.height()),
                           Qt.AlignmentFlag.AlignLeft,
                           fm_pt.elidedText("· " + point,
                                            Qt.TextElideMode.ElideRight, w))
                y += fm_pt.height() + 5
