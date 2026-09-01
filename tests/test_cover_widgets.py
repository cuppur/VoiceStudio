from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from local_voice_studio.ui.cover_session import LyricLine
from local_voice_studio.ui.studio_widgets import LyricView, TaskProgress, VoiceSelector, WaveformWidget
from local_voice_studio.ui.studio_widgets.stem_track import StemTrackWidget, TrackStatus
from local_voice_studio.ui.cover_page import TRACK_NAMES


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


def test_stem_track_status_is_read_only_enum():
    QApplication.instance() or QApplication([])
    track = StemTrackWidget()
    assert isinstance(track.status_label, type(track.name_label))
    assert not hasattr(track, "status_combo")
    track.set_status(TrackStatus.PROCESSING)
    assert track.status is TrackStatus.PROCESSING
    assert track.status_label.text() == "处理中"


def test_task_progress_has_fixed_cover_stages():
    assert TaskProgress.STAGES == ("验证歌曲", "准备模型", "分离人声", "生成波形", "保存工程")
    assert TRACK_NAMES == ("原曲", "原唱人声", "伴奏", "AI 人声", "最终混音")


def test_voice_selector_disables_unconsented_and_archived_profiles():
    QApplication.instance() or QApplication([])
    selector = VoiceSelector()
    selector.set_profiles([
        {"id": "ok", "name": "已授权", "consent_confirmed": True, "archived": False},
        {"id": "no", "name": "未授权", "consent_confirmed": False, "archived": False},
        {"id": "old", "name": "已归档", "consent_confirmed": True, "archived": True},
    ])
    assert selector.count() == 3
    assert selector.model().item(0).flags() & Qt.ItemIsEnabled
    assert not selector.model().item(1).flags() & Qt.ItemIsEnabled
    assert not selector.model().item(2).flags() & Qt.ItemIsEnabled
    assert selector.itemData(0) == "ok"
    assert selector.itemData(1, Qt.ToolTipRole) == "请先确认授权"
