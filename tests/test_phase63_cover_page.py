from __future__ import annotations

"""Phase 6.3 cover_page coverage hardening.

Drives the CoverPage UI methods offscreen without any real FFmpeg, GPU, or
Worker process.  Audio files are tiny generated WAVs; every modal (QMessageBox,
QFileDialog, ExportDialog.exec, SeparationDialog.exec) is patched so the
offscreen run can never block on a dialog nobody will close.
"""

import hashlib
import os
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QFileDialog

from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.cover.preview import PlaybackMode, TrackRole
from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.singing.models import SingingModelVersion
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.cover_page import CoverPage, SeparationDialog, ExportDialog
from local_voice_studio.ui.cover_session import AudioMetadata, LyricLine, SongSession
from local_voice_studio.ui.studio_widgets.stem_track import TrackStatus


class FakeClient(QObject):
    event = Signal(str, str, dict)
    state_changed = Signal(str)
    ready_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, command, payload=None, request_id=None):
        self.sent.append((command, dict(payload or {}), request_id))
        return request_id or "request"


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(data, root / "projects", data / "runtime", data / "engine",
                    data / "models", data / "logs", data / "studio.sqlite3")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wav(path: Path, seconds: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(100)
        stream.writeframes(b"\x01\x00" * seconds * 100)


def _sha(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _fake_session(path: Path, duration: float = 2.0, with_lyrics: bool = False) -> SongSession:
    lyrics = [LyricLine(0.5, "第一句"), LyricLine(1.5, "第二句")] if with_lyrics else []
    return SongSession(str(path), lyrics, AudioMetadata(duration, 48000, 1, "pcm_s16le", 768000),
                       _sha(path), [(i, i * 100) for i in range(50)])


def _ready_profile(project: Path, tmp_path: Path, name: str = "授权声音") -> VoiceProfile:
    checkpoint = project / "models" / "singing" / "profile" / "run" / "model.pth"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_bytes(b"checkpoint")
    index = checkpoint.with_suffix(".index")
    index.write_bytes(b"index")
    profile = VoiceProfile(name, True, id="profile", consent_record="本人授权", consent_confirmed_at="now")
    model = SingingModelVersion(
        profile_id=profile.id, engine="rvc_v2",
        checkpoint_relative_path=checkpoint.relative_to(project).as_posix(),
        checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        index_relative_path=index.relative_to(project).as_posix(),
        index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
        trust_status="verified",
    )
    profile.singing_models = [model]
    profile.active_singing_model_id = model.id
    return profile


def _build_ready_cover(project: Path, tmp_path: Path, title: str = "song") -> CoverProject:
    original = tmp_path / f"{title}.wav"
    _wav(original)
    cover = CoverProject.create(project, title=title)
    cover.copy_source(original)
    vocal = cover.root / "stems" / "vocals.wav"
    _wav(vocal)
    cover.set_stem("vocal", vocal)
    inst = cover.root / "stems" / "instrumental.wav"
    _wav(inst)
    cover.set_stem("instrumental", inst)
    ai = cover.root / "stems" / "ai.wav"
    _wav(ai)
    cover.add_asset(CoverAsset(id="ai1", role="ai_vocal",
                               relative_path=ai.relative_to(cover.root).as_posix(),
                               sha256=_sha(ai), content_origin="ai_generated", producer="convert"))
    final = cover.root / "outputs" / "final.wav"
    _wav(final)
    cover.add_asset(CoverAsset(id="final1", role="final_mix",
                               relative_path=final.relative_to(cover.root).as_posix(),
                               sha256=_sha(final), content_origin="ai_generated", producer="render"))
    cover.attest_rights(True)
    return cover


def _wait_threads(page: CoverPage) -> None:
    app = _app()
    for _ in range(100):
        app.processEvents()
        if not page._threads:
            return
        import time
        time.sleep(0.01)
    page.release_resources()


def _ready_page(store: StudioStore, project: Path, tmp_path: Path) -> CoverPage:
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._profile_selected(page.profile_combo.currentData())
    page._update_cover_button()
    return page


# ---------------------------------------------------------------------------
# Construction and class-level wiring
# ---------------------------------------------------------------------------


def test_cover_page_construction_sets_up_widgets(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-construct")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    assert page.cover_button is not None
    assert page.render_button is not None
    assert page.export_button is not None
    assert page.import_button is not None
    assert page.separate_button is not None
    assert len(page.stems) == 5
    assert page.cover_button.isEnabled() is False
    assert page.render_button.isEnabled() is False
    assert page.export_button.isEnabled() is False
    assert "还没有导入歌曲" in page.song_title.text()
    # Runtime engine detection on empty tmp paths yields "missing", no crash.
    assert page.runtime_status is not None
    assert page.roformer_runtime_status is not None
    page.release_resources()
    _app().processEvents()


def test_cover_page_restore_cover_without_covers_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-restore-empty")
    page = CoverPage(store.paths, store, project, FakeClient())
    assert page.cover_project is None
    page.release_resources()
    _app().processEvents()


def test_cover_page_restores_interrupted_cover(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-restore-stale")
    original = tmp_path / "stale.wav"
    _wav(original)
    cover = CoverProject.create(project, title="stale")
    cover.copy_source(original)
    cover.separation_status = "running"
    cover.save()
    page = CoverPage(store.paths, store, project, FakeClient())
    # restore_cover picked the interrupted cover and marked it interrupted.
    assert page.cover_project is not None
    assert page.cover_project.separation_status == "interrupted"
    assert "未完成" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Import / song loading
# ---------------------------------------------------------------------------


def test_import_song_uses_dialog_and_creates_cover(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-import")
    song = tmp_path / "mytune.wav"
    _wav(song)
    page = CoverPage(store.paths, store, project, FakeClient())
    with patch.object(QFileDialog, "getOpenFileName", return_value=(str(song), "")), \
         patch("local_voice_studio.ui.cover_session.SongSession.load",
               return_value=_fake_session(song)):
        page.import_song()
        _wait_threads(page)
    assert page.cover_project is not None
    assert page.cover_project.title == "mytune"
    assert page.track_paths.get(0)
    assert page.stems[0].status is TrackStatus.READY
    page.release_resources()
    _app().processEvents()


def test_import_song_cancel_does_nothing(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-import-cancel")
    page = CoverPage(store.paths, store, project, FakeClient())
    with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
        page.import_song()
    assert page.cover_project is None
    page.release_resources()
    _app().processEvents()


def test_set_song_copies_lyrics_and_saves_duration(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-set-song")
    song = tmp_path / "withlyrics.wav"
    _wav(song)
    lrc = song.with_suffix(".lrc")
    lrc.write_text("[00:00.50]你好\n[00:01.50]世界\n", encoding="utf-8")
    page = CoverPage(store.paths, store, project, FakeClient())
    session = _fake_session(song, duration=2.5, with_lyrics=True)
    with patch("local_voice_studio.ui.cover_session.SongSession.load", return_value=session):
        page.set_song(str(song))
        _wait_threads(page)
    assert page.cover_project.duration_ms == 2500
    assert page.cover_project.lyrics_path
    assert page.lyric_status.text() == "已载入 LRC"
    assert page.lyrics.lines  # lyrics loaded
    page.release_resources()
    _app().processEvents()


def test_set_song_load_failure_reports_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-set-fail")
    song = tmp_path / "broken.wav"
    _wav(song)
    page = CoverPage(store.paths, store, project, FakeClient())
    with patch("local_voice_studio.ui.cover_session.SongSession.load", side_effect=RuntimeError("no ffmpeg")):
        page.set_song(str(song))
        _wait_threads(page)
    assert "读取失败" in page.song_meta.text()
    assert page.stems[0].status is TrackStatus.ERROR
    page.release_resources()
    _app().processEvents()


def test_load_track_missing_path_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-load-missing")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._load_track(0, tmp_path / "nope.wav")
    assert not page._threads
    page.release_resources()
    _app().processEvents()


def test_loaded_index3_marks_ai_generated(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-loaded3")
    page = CoverPage(store.paths, store, project, FakeClient())
    path = tmp_path / "ai.wav"
    _wav(path)
    page._loaded(3, _fake_session(path))
    assert "AI生成" in page.stems[3].name_label.text()
    page.release_resources()
    _app().processEvents()


def test_load_finished_reenables_import_button(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-load-finished")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.import_button.setEnabled(False)
    thread = object()
    page._threads.add(thread)
    page._load_finished(thread)
    assert not page._threads
    assert page.import_button.isEnabled() is True
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Lyrics
# ---------------------------------------------------------------------------


def test_import_lyrics_reloads_session(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-lyrics-import")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    lrc = tmp_path / "manual.lrc"
    lrc.write_text("[00:00.50]手动歌词\n", encoding="utf-8")
    with patch.object(QFileDialog, "getOpenFileName", return_value=(str(lrc), "")):
        page.import_lyrics()
    assert "已手动载入 LRC" in page.lyric_status.text()
    assert page.lyrics.lines
    page.release_resources()
    _app().processEvents()


def test_import_lyrics_without_project_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-lyrics-none")
    page = CoverPage(store.paths, store, project, FakeClient())
    with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
        page.import_lyrics()  # no cover_project -> early return, no crash
    page.release_resources()
    _app().processEvents()


def test_lyric_navigation_buttons(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-lyrics-nav")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.lyrics.set_lyrics([LyricLine(0.5, "第一句"), LyricLine(1.5, "第二句")], editable=True)
    page._lyric_next()
    page._lyric_previous()
    assert page.lyrics.currentRow() >= 0
    page.release_resources()
    _app().processEvents()


def test_edit_lyric_line_persists_changes(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-edit-lyric")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    path = cover.root / "lyrics" / "lyrics.lrc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[00:00.50]旧句\n[00:01.50]第二句\n", encoding="utf-8")
    page.cover_project.set_lyrics(path, origin="manual")
    page.sessions[0] = _fake_session(path, with_lyrics=True)
    page._edit_lyric_line(500, 700, "新句")
    assert "已手动编辑" in page.lyric_status.text()
    assert "歌词已保存" in page.song_meta.text()
    assert page.cover_project.lyrics_origin == "manual"
    page.release_resources()
    _app().processEvents()


def test_edit_lyric_line_missing_target_reports(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-edit-miss")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    path = cover.root / "lyrics" / "lyrics.lrc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[00:00.50]旧句\n[00:01.50]第二句\n", encoding="utf-8")
    page.cover_project.set_lyrics(path)
    page.sessions[0] = _fake_session(path, with_lyrics=True)
    page._edit_lyric_line(9999, 700, "新句")
    assert "找不到对应歌词行" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_reload_lyrics_session(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-lyrics-reload")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    path = cover.root / "lyrics" / "lyrics.lrc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[00:00.50]重载\n", encoding="utf-8")
    page.cover_project.set_lyrics(path)
    page.sessions[0] = _fake_session(path)
    page._reload_lyrics_session()
    assert page.sessions[0].lyrics and page.sessions[0].lyrics[0].text == "重载"
    page.release_resources()
    _app().processEvents()


def test_auto_transcribe_lyrics_sends_command(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-transcribe")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    page.cover_project = page._load_cover_project(_build_ready_cover(project, tmp_path).id)
    page.auto_transcribe_lyrics()
    assert client.sent and client.sent[-1][0] == "transcribe_lyrics"
    assert client.sent[-1][1]["language"] == "zh"
    page.release_resources()
    _app().processEvents()


def test_auto_transcribe_lyrics_without_project(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-transcribe-none")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.auto_transcribe_lyrics()
    assert "请先导入歌曲" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_lyrics_finished_sets_status(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-lyrics-done")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.sessions[0] = _fake_session(tmp_path / "x.wav")
    page._lyrics_finished("歌词已识别")
    assert "歌词已识别" in page.song_meta.text()
    assert page.lyric_auto.isEnabled()
    assert page.lyric_auto.text() == "自动识别歌词"
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Worker event branches
# ---------------------------------------------------------------------------


def test_handle_lyrics_progress_and_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-lyrics-events")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._lyrics_request = "L1"
    page.handle_worker_event("L1", "progress", {"message": "正在识别"})
    assert "正在识别" in page.song_meta.text()
    page.handle_worker_event("L1", "error", {"message": "炸了"})
    assert "歌词识别失败" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_pitch_result_and_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-pitch-events")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._pitch_request = "P1"
    page.handle_worker_event("P1", "result", {"suggested_transpose": 3})
    assert page.pitch.value() == 3
    assert "+3 半音" in page.song_meta.text()
    page._pitch_request = "P2"
    page.handle_worker_event("P2", "error", {"message": "失败"})
    assert "失败" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_cleanup_result_then_ai(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-cleanup-events")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._cleanup_request = "C1"
    page._pending_ai_payload = {"cover_id": cover.id, "pitch_shift": 0}
    vocal_asset = cover.get_asset(role="vocal")
    page.handle_worker_event("C1", "result", {"asset_id": vocal_asset.id})
    # cleanup result reloaded the cover and fired convert_vocal.
    assert client.sent[-1][0] == "convert_vocal"
    page.release_resources()
    _app().processEvents()


def test_handle_cleanup_result_missing_asset(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-cleanup-miss")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._cleanup_request = "C1"
    page.handle_worker_event("C1", "result", {"asset_id": "bogus"})
    assert "清理结果未登记" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_render_result_enables_export(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-render-event")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._render_request = "R1"
    final_asset = cover.get_asset(role="final_mix")
    page.handle_worker_event("R1", "result", {"asset_id": final_asset.id})
    assert page.export_button.isEnabled() is True
    assert "最终翻唱已生成" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_render_missing_asset_fails(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-render-miss")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._render_request = "R1"
    page.handle_worker_event("R1", "result", {"asset_id": "bogus"})
    assert "最终混音未登记" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_export_result_and_progress(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-export-event")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.cover_project = page._load_cover_project(_build_ready_cover(project, tmp_path).id)
    page._export_request = "E1"
    page.handle_worker_event("E1", "progress", {"message": "正在导出"})
    assert "正在导出" in page.song_meta.text()
    page.handle_worker_event("E1", "result", {"outputs": ["out.wav", "out.mp3"]})
    assert page.export_button.isEnabled() is True
    assert "导出完成" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_export_error_prompts_replace(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-export-replace")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    page.cover_project = page._load_cover_project(_build_ready_cover(project, tmp_path).id)
    page._export_request = "E1"
    page._last_export_payload = {"cover_id": "x", "existing_policy": "reject"}
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        page.handle_worker_event("E1", "error", {"message": "目标文件已存在"})
    # Retry with replace policy was sent.
    assert client.sent and client.sent[-1][0] == "export_cover"
    assert client.sent[-1][1]["existing_policy"] == "replace"
    page.release_resources()
    _app().processEvents()


def test_handle_export_error_no_replace(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-export-noreplace")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.cover_project = page._load_cover_project(_build_ready_cover(project, tmp_path).id)
    page._export_request = "E1"
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
        page.handle_worker_event("E1", "error", {"message": "磁盘错误"})
    assert "导出失败" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_ai_result_loads_track_and_enables_render(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-ai-event")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    ai_asset = cover.get_asset(role="ai_vocal")
    page._ai_request = "A1"
    with patch("local_voice_studio.ui.cover_session.SongSession.load",
               return_value=_fake_session(cover.root / ai_asset.relative_path)):
        page.handle_worker_event("A1", "result", {"asset_id": ai_asset.id, "cache_hit": True})
        _wait_threads(page)
    assert page.render_button.isEnabled() is True
    assert "已复用缓存" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_ai_error_calls_failed(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-ai-error")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.cover_project = page._load_cover_project(_build_ready_cover(project, tmp_path).id)
    page._ai_request = "A1"
    page.handle_worker_event("A1", "error", {"message": "模型失败"})
    assert "AI 人声生成失败" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_separation_result_loads_stems(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-sep-event")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    vocal = cover.root / "stems" / "vocals.wav"
    inst = cover.root / "stems" / "instrumental.wav"
    page._separation_request = "S1"
    with patch("local_voice_studio.ui.cover_session.SongSession.load",
               return_value=_fake_session(vocal)):
        page.handle_worker_event("S1", "result",
                                 {"vocal_path": str(vocal), "instrumental_path": str(inst),
                                  "cache_hit": True})
        _wait_threads(page)
    assert "分离完成" in page.song_meta.text()
    assert page.separate_button.isEnabled() is True
    page.release_resources()
    _app().processEvents()


def test_handle_separation_error_fails(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-sep-error")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._separation_request = "S1"
    page.handle_worker_event("S1", "error", {"message": "引擎崩溃"})
    assert "引擎崩溃" in page.song_meta.text()
    assert page.stems[1].status is TrackStatus.ERROR
    page.release_resources()
    _app().processEvents()


def test_handle_separation_progress_sets_stage(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-sep-progress")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._separation_request = "S1"
    page.handle_worker_event("S1", "progress", {"stage": "preparing_model", "message": "正在准备"})
    assert "正在准备" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_handle_unknown_request_is_ignored(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-unknown-event")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.handle_worker_event("unrelated", "result", {})  # must not raise
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# AI vocal generation
# ---------------------------------------------------------------------------


def test_generate_ai_vocal_sends_convert(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-gen-ai")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._profile_selected(page.profile_combo.currentData())
    page.pitch.setValue(-2)
    page.autotune.setCurrentIndex(1)
    page.generate_ai_vocal()
    command, payload, _rid = client.sent[-1]
    assert command == "convert_vocal"
    assert payload["pitch_shift"] == -2
    assert payload["inference_settings"]["autotune"] == "light"
    assert "AI 人声生成中" in page.cover_button.text()
    page.release_resources()
    _app().processEvents()


def test_generate_ai_vocal_with_cleanup(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-gen-cleanup")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._profile_selected(page.profile_combo.currentData())
    page.cleanup_toggle.setChecked(True)
    page.dereverb.setCurrentIndex(1)
    page.generate_ai_vocal()
    command, payload, _rid = client.sent[-1]
    assert command == "cleanup_vocal"
    assert payload["cleanup_settings"]["dereverb"] == "light"
    assert page._pending_ai_payload["pitch_shift"] == 0
    page.release_resources()
    _app().processEvents()


def test_generate_ai_vocal_without_profile_reports(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-gen-noprofile")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.generate_ai_vocal()
    assert "请选择已授权" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Transpose suggestion
# ---------------------------------------------------------------------------


def test_suggest_transpose_sends_command(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-suggest")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._profile_selected(page.profile_combo.currentData())
    page.suggest_transpose()
    assert client.sent[-1][0] == "suggest_transpose"
    assert "分析中" in page.auto_pitch_button.text()
    page.release_resources()
    _app().processEvents()


def test_suggest_transpose_without_worker_reports(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-suggest-none")
    page = CoverPage(store.paths, store, project, None)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.suggest_transpose()
    assert "请选择已验证歌唱模型" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Final render
# ---------------------------------------------------------------------------


def test_request_final_render_sends_render(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-render-req")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._profile_selected(page.profile_combo.currentData())
    page.mixer.sliders[0].setValue(60)
    page.normalize_toggle.setChecked(False)
    page.limiter_toggle.setChecked(False)
    page.request_final_render()
    command, payload, _rid = client.sent[-1]
    assert command == "render_cover"
    assert payload["mix_settings"]["normalize"] is False
    assert payload["mix_settings"]["limiter"] is False
    assert page.render_button.isEnabled() is False
    page.release_resources()
    _app().processEvents()


def test_request_final_render_without_profile_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-render-noprofile")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.request_final_render()
    assert not page.worker.sent  # no profile -> early return
    page.release_resources()
    _app().processEvents()


def test_render_failed_restores_button(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-render-fail")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._render_failed("boom")
    assert "最终混音失败" in page.song_meta.text()
    assert page.render_button.isEnabled() is True
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_final_sends_export(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-export-req")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    dlg = ExportDialog("song_AI_VoiceStudio")
    dlg.file_name.setText("out")
    dlg.destination.setText(str(tmp_path / "outdir"))
    dlg.rights.setChecked(True)
    dlg.format.setCurrentIndex(1)
    with patch("local_voice_studio.ui.cover_page.ExportDialog", return_value=dlg), \
         patch.object(ExportDialog, "exec", return_value=QDialog.Accepted):
        page.export_final()
    command, payload, _rid = client.sent[-1]
    assert command == "export_cover"
    assert payload["file_name"] == "out"
    assert payload["format"] == "mp3"
    assert page.export_button.isEnabled() is False
    assert page._last_export_payload["cover_id"] == cover.id
    page.release_resources()
    _app().processEvents()


def test_export_final_cancelled_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-export-cancel")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    dlg = ExportDialog("x")
    with patch("local_voice_studio.ui.cover_page.ExportDialog", return_value=dlg), \
         patch.object(ExportDialog, "exec", return_value=QDialog.Rejected):
        page.export_final()
    assert not client.sent
    page.release_resources()
    _app().processEvents()


def test_export_failed_restores_button(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-export-fail")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._export_failed("boom")
    assert "导出失败" in page.song_meta.text()
    assert page.export_button.isEnabled() is True
    page.release_resources()
    _app().processEvents()


def test_export_dialog_choose_sets_destination(tmp_path: Path):
    _app()
    dlg = ExportDialog("x")
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(tmp_path / "dir")):
        dlg._choose()
    assert dlg.destination.text() == str(tmp_path / "dir")
    dlg.destination.clear()
    with patch.object(QFileDialog, "getExistingDirectory", return_value=""):
        dlg._choose()  # empty -> no change
    assert dlg.destination.text() == ""


def test_export_dialog_accept_requires_fields(tmp_path: Path):
    _app()
    dlg = ExportDialog("x")
    dlg.destination.setText(str(tmp_path))
    dlg.file_name.setText("out")
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warn:
        dlg._accept()  # rights not checked
    warn.assert_called_once()
    dlg.rights.setChecked(True)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warn2:
        dlg._accept()
    warn2.assert_not_called()


def test_export_dialog_accept_ok():
    _app()
    dlg = ExportDialog("x")
    dlg.file_name.setText("out")
    dlg.destination.setText("C:/out")
    dlg.rights.setChecked(True)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warn:
        dlg._accept()
    warn.assert_not_called()


# ---------------------------------------------------------------------------
# Separation
# ---------------------------------------------------------------------------


def test_separation_dialog_builds_cards(tmp_path: Path):
    _app()
    from local_voice_studio.cover.separation import UVR5RuntimeStatus, RoFormerRuntimeStatus
    ready = UVR5RuntimeStatus("ready")
    roformer = RoFormerRuntimeStatus("ready")
    dlg = SeparationDialog(ready, roformer)
    assert dlg.engine_id == ""
    dlg._choose("roformer")
    assert dlg.engine_id == "roformer"
    dlg.reject()


def test_separate_song_sends_command(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-sep-req")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._confirm_rights()  # already confirmed
    dlg = SeparationDialog(page.runtime_status, page.roformer_runtime_status)
    dlg.engine_id = "uvr5"
    with patch("local_voice_studio.ui.cover_page.SeparationDialog", return_value=dlg), \
         patch.object(SeparationDialog, "exec", return_value=QDialog.Accepted), \
         patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        page.separate_song()
    command, payload, _rid = client.sent[-1]
    assert command == "separate_song"
    assert payload["mode"] == "uvr5"
    assert page._separation_request
    assert not page.cancel_button.isHidden()
    page.release_resources()
    _app().processEvents()


def test_separate_song_without_cover_reports(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-sep-none")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.separate_song()
    assert "请先导入歌曲" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_separate_song_dialog_rejected_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-sep-reject")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    dlg = SeparationDialog(page.runtime_status, page.roformer_runtime_status)
    dlg.engine_id = "uvr5"
    with patch("local_voice_studio.ui.cover_page.SeparationDialog", return_value=dlg), \
         patch.object(SeparationDialog, "exec", return_value=QDialog.Rejected):
        page.separate_song()
    assert not client.sent
    page.release_resources()
    _app().processEvents()


def test_confirm_rights_attests_via_question(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-rights")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    # drop the rights confirmation to force the question prompt
    cover.attest_rights(False)
    page.cover_project = page._load_cover_project(cover.id)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        assert page._confirm_rights() is True
    assert page.cover_project.rights_confirmed is True
    assert "已确认" in page.rights_state.text()
    page.release_resources()
    _app().processEvents()


def test_confirm_rights_cancelled(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-rights-cancel")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    cover.attest_rights(False)
    page.cover_project = page._load_cover_project(cover.id)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
        assert page._confirm_rights() is False
    assert page.cover_project.rights_confirmed is False
    page.release_resources()
    _app().processEvents()


def test_cancel_separation_sends_cancel(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-cancel-sep")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    page._separation_request = "S9"
    page.cancel_separation()
    assert client.sent[-1][0] == "cancel"
    assert client.sent[-1][1]["target_request_id"] == "S9"
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Cancellation buttons
# ---------------------------------------------------------------------------


def test_cancel_final_task_and_ai_vocal(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-cancel-final")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    page._render_request = "R9"
    page.cancel_final_task()
    assert client.sent[-1][1]["target_request_id"] == "R9"
    page._ai_request = "A9"
    page.cancel_ai_vocal()
    assert client.sent[-1][1]["target_request_id"] == "A9"
    page.release_resources()
    _app().processEvents()


def test_cancel_ai_vocal_without_request_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-cancel-ai-none")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.cancel_ai_vocal()  # no request -> no send
    assert not page.worker.sent
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Worker state / ready
# ---------------------------------------------------------------------------


def test_on_worker_state_stopped_disables_actions(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-worker-stop")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._on_worker_state("stopped")
    assert page.cover_button.isEnabled() is False
    assert page.render_button.isEnabled() is False
    assert page.export_button.isEnabled() is False
    assert page.separate_button.isEnabled() is False
    assert "已停止" in page.song_meta.text()
    page.release_resources()
    _app().processEvents()


def test_on_worker_ready_refreshes_runtime(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-worker-ready")
    page = CoverPage(store.paths, store, project, FakeClient())
    with patch.object(page, "refresh_runtime_status") as refresh:
        page._on_worker_ready(False)
        refresh.assert_called_once()
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Playback / mixing controls
# ---------------------------------------------------------------------------


def test_toggle_playback_solo_and_preview_modes(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-playback")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.track_paths = {0: str(tmp_path / "a.wav"), 2: str(tmp_path / "inst.wav"),
                        3: str(tmp_path / "ai.wav")}
    for path in page.track_paths.values():
        _wav(Path(path))
    page._selected_track = 3
    page.toggle_playback()
    assert page._playback_mode is PlaybackMode.MIX_PREVIEW
    page.toggle_playback()  # pause
    assert page.preview_controller.playing is False
    page.release_resources()
    _app().processEvents()


def test_toggle_playback_final_mix_and_seek(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-playback-final")
    page = CoverPage(store.paths, store, project, FakeClient())
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page.track_paths = {4: str(tmp_path / "final.wav")}
    _wav(Path(page.track_paths[4]))
    page._selected_track = 4
    page.toggle_playback()
    assert page._playback_mode is PlaybackMode.FINAL_MIX
    page._seek_all(1500)
    assert page.preview_controller.master_position_ms == 1500
    page.release_resources()
    _app().processEvents()


def test_toggle_playback_no_track_is_noop(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-playback-none")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.track_paths = {}
    page._selected_track = 0
    page.toggle_playback()  # no track -> no-op
    assert page.preview_controller.playing is False
    page.release_resources()
    _app().processEvents()


def test_mixer_volume_changed_updates_stem(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-mixer")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._mixer_volume_changed(0, 40)
    assert page.stems[3].volume.value() == 40
    page._mixer_volume_changed(2, 55)
    assert page.stems[1].volume.value() == 55
    page.release_resources()
    _app().processEvents()


def test_track_mix_changed_solo_selection(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-track-mix")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.track_paths = {0: "a.wav", 3: "ai.wav"}
    page.stems[3].solo.setChecked(True)
    page._track_mix_changed(3)
    assert page._playback_mode is PlaybackMode.SOLO_TRACK
    assert page._selected_track == 3
    page.stems[3].solo.setChecked(False)
    page._track_mix_changed(0)
    assert page._playback_mode is PlaybackMode.MIX_PREVIEW
    page.release_resources()
    _app().processEvents()


def test_select_track_refreshes_plan(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-select")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.track_paths = {2: "inst.wav"}
    page._select_track(2, preserve=False)
    assert page._selected_track == 2
    page._select_track(5, preserve=True)  # not in track_paths -> noop
    assert page._selected_track == 2
    page.release_resources()
    _app().processEvents()


def test_set_master_volume_and_preview_gains(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-master-vol")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.track_paths = {2: "inst.wav", 3: "ai.wav"}
    page._set_master_volume(0.5)
    page._refresh_preview_plan(preserve_position=False)
    assert page.preview_controller.plan is not None
    page.release_resources()
    _app().processEvents()


def test_on_player_position_and_playback_state(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-player-signals")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.track_paths = {2: "inst.wav"}
    page._refresh_preview_plan()
    page._on_player_position(page._track_role(2), 900)
    assert page.preview_controller.master_position_ms == 900
    states = []
    with patch.object(page.transport, "set_playing", side_effect=lambda v: states.append(v)):
        page._on_playback_state(page._track_role(2), QMediaPlayer.PlayingState)
        page._on_playback_state(page._track_role(2), QMediaPlayer.StoppedState)
    assert states == [True, False]
    page.release_resources()
    _app().processEvents()


def test_resync_preview(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-resync")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.track_paths = {2: "inst.wav"}
    page._resync_preview()  # no plan -> resync returns 0, no crash
    page.release_resources()
    _app().processEvents()


def test_position_updates_stems_and_lyrics(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-position")
    page = CoverPage(store.paths, store, project, FakeClient())
    page.lyrics.set_lyrics([LyricLine(0.5, "第一句")], editable=True)
    page._position(1200)
    assert page.transport.time_label.text()
    page.release_resources()
    _app().processEvents()


# ---------------------------------------------------------------------------
# Profile / cover button / workflow
# ---------------------------------------------------------------------------


def test_update_cover_button_enabled_with_ready_state(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-button")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._profile_selected(page.profile_combo.currentData())
    page._update_cover_button()
    assert page.cover_button.isEnabled() is True
    assert page.workflow_steps is not None
    page.release_resources()
    _app().processEvents()


def test_update_cover_button_disabled_without_worker(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-button-noworker")
    page = CoverPage(store.paths, store, project, None)
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    profile = _ready_profile(project, tmp_path)
    store.save_profile(project, profile)
    page.refresh_profiles()
    page.profile_combo.setCurrentIndex(0)
    page._update_cover_button()
    assert page.cover_button.isEnabled() is False
    page.release_resources()
    _app().processEvents()


def test_profile_selected_emits_signal(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-profile-sel")
    page = CoverPage(store.paths, store, project, FakeClient())
    emitted = []
    page.profileChanged.connect(emitted.append)
    page._profile_selected("profile-1")
    assert emitted == ["profile-1"]
    page.release_resources()
    _app().processEvents()


def test_update_workflow_step_stages(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("cover-workflow")
    page = CoverPage(store.paths, store, project, FakeClient())
    page._update_workflow_step()  # no cover -> step 0
    cover = _build_ready_cover(project, tmp_path)
    page.cover_project = page._load_cover_project(cover.id)
    page._update_workflow_step()  # fully ready -> step 6
    page.release_resources()
    _app().processEvents()
