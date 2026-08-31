from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from local_voice_studio.ui.cover_session import LyricLine
from local_voice_studio.ui.studio_widgets import LyricView, WaveformWidget


def test_waveform_accepts_real_min_max_pcm_and_clamps_position():
    QApplication.instance() or QApplication([])
    widget = WaveformWidget(); widget.resize(400, 100)
    widget.set_waveform([(-32768, 16384), (-1200, 900)], 10_000)
    assert widget.peaks == [(-1.0, .5), (-1200 / 32768, 900 / 32768)]
    widget.set_position(20_000)
    assert widget.position_ms == 10_000
    assert widget._position_from_x(-5) == 0
    assert widget._position_from_x(500) == 10_000


def test_lyric_view_uses_session_timestamps_in_milliseconds():
    QApplication.instance() or QApplication([])
    view = LyricView(); view.set_lyrics([LyricLine(1.25, "第一句"), LyricLine(3.0, "第二句")])
    assert view.lines == [(1250, "第一句"), (3000, "第二句")]
    view.set_position(3200)
    assert view.currentRow() == 1
