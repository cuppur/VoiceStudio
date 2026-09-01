from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget

from ..cover import CoverProject
from ..cover.separation import UVR5RuntimeStatus
from .cover_session import SongSession, parse_lrc
from .studio_widgets import LyricView, StemTrackWidget, TaskProgress, TrackStatus, TransportWidget, VoiceSelector

RIGHTS_TEXT = "我确认自己拥有或已经获得处理、使用该音频所需的权利，并理解公开传播或商业发行可能需要额外取得歌曲、录音等相关授权。"
TRACK_NAMES = ("原曲", "原唱人声", "伴奏", "AI 人声", "最终混音")
STAGE_INDEX = {"validating": 0, "preparing_model": 1, "separating": 2, "generating_waveforms": 3, "saving_project": 4}


class _LoadThread(QThread):
    loaded = Signal(int, object); failed = Signal(int, str)
    def __init__(self, index, audio, lrc, paths, cache_dir, parent=None):
        super().__init__(parent); self.index, self.audio, self.lrc, self.paths, self.cache_dir = index, Path(audio), lrc, paths, cache_dir
    def run(self):
        try:
            value = SongSession.load(self.audio, lrc_path=self.lrc, paths=self.paths, cache_dir=self.cache_dir, cancel=self.isInterruptionRequested)
            self.loaded.emit(self.index, value)
        except Exception as exc: self.failed.emit(self.index, str(exc))


class SeparationDialog(QDialog):
    def __init__(self, runtime, parent=None):
        super().__init__(parent); self.setWindowTitle("选择歌曲分离方式"); self.setMinimumWidth(520); layout = QVBoxLayout(self)
        layout.addWidget(QLabel("歌曲分离"))
        cards = (("UVR5", "快速分离", "已安装 · 可用" if runtime.ready else ("文件损坏" if runtime.status == "corrupt" else "未安装"), runtime.ready),
                 ("RoFormer", "高质量分离", "尚未安装 · 未来版本", False), ("多轨", "主唱 / 和声 / 伴奏", "未来版本", False))
        for name, detail, state, enabled in cards:
            button = QPushButton(f"{name}\n{detail}    {state}"); button.setObjectName("separatorCard"); button.setEnabled(enabled)
            if enabled: button.clicked.connect(self.accept)
            layout.addWidget(button)
        cancel = QPushButton("取消"); cancel.clicked.connect(self.reject); layout.addWidget(cancel, alignment=Qt.AlignRight)


class CoverPage(QWidget):
    profileChanged = Signal(str)
    def __init__(self, paths, store, project, worker=None, parent=None):
        super().__init__(parent); self.paths, self.store, self.project, self.worker = paths, store, Path(project), worker
        self.cover_project = None; self.sessions = {}; self.track_paths = {}; self._threads = set(); self._separation_request = ""; self._ai_request = ""; self._selected_track = 0
        self.audio_output = QAudioOutput(self); self.audio_output.setVolume(.75); self.player = QMediaPlayer(self); self.player.setAudioOutput(self.audio_output)
        self._build(); self.refresh_profiles(); self.refresh_runtime_status(); self.restore_cover()
        self.player.positionChanged.connect(self._position); self.player.durationChanged.connect(lambda v: self.transport.set_timeline(self.player.position(), v)); self.player.playbackStateChanged.connect(lambda s: self.transport.set_playing(s == QMediaPlayer.PlayingState))

    def _build(self):
        root = QHBoxLayout(self); root.setContentsMargins(24, 20, 24, 20); root.setSpacing(18); left = QVBoxLayout(); root.addLayout(left, 1)
        header = QHBoxLayout(); self.song_title = QLabel("还没有导入歌曲"); self.song_title.setObjectName("songTitle"); header.addWidget(self.song_title); header.addStretch()
        self.import_button = QPushButton("导入歌曲"); self.import_button.clicked.connect(self.import_song); header.addWidget(self.import_button); left.addLayout(header)
        self.song_meta = QLabel("支持 WAV / MP3 / FLAC · 导入后自动寻找同名 LRC"); self.song_meta.setObjectName("muted"); left.addWidget(self.song_meta)
        self.transport = TransportWidget(); self.transport.play_requested.connect(self.toggle_playback); self.transport.seek_relative_requested.connect(lambda d: self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + d)))); self.transport.timeline.sliderMoved.connect(self.player.setPosition); self.transport.volume_changed.connect(lambda v: self.audio_output.setVolume(v / 100)); left.addWidget(self.transport)
        self.stems = []
        for index, name in enumerate(TRACK_NAMES):
            track = StemTrackWidget(name); track.set_status(TrackStatus.EMPTY); track.seek_requested.connect(self.player.setPosition)
            track.solo_changed.connect(lambda _v, i=index: self._track_mix_changed(i)); track.mute_changed.connect(lambda _v, i=index: self._track_mix_changed(i)); track.volume_changed.connect(lambda _v, i=index: self._track_mix_changed(i))
            self.stems.append(track); left.addWidget(track)
        lyric_header = QHBoxLayout(); lyric_header.addWidget(QLabel("歌词")); lyric_header.addStretch(); self.lyric_status = QLabel("未载入"); self.lyric_status.setObjectName("muted"); lyric_header.addWidget(self.lyric_status)
        self.lyric_import = QPushButton("手动导入 LRC"); self.lyric_import.clicked.connect(self.import_lyrics); lyric_header.addWidget(self.lyric_import); left.addLayout(lyric_header)
        self.lyrics = LyricView(); self.lyrics.seek_requested.connect(self.player.setPosition); left.addWidget(self.lyrics, 1)
        self.progress = TaskProgress(); self.progress.hide(); left.addWidget(self.progress)
        actions = QHBoxLayout(); self.separate_button = QPushButton("分离人声 / 伴奏"); self.separate_button.clicked.connect(self.separate_song); actions.addWidget(self.separate_button)
        self.cancel_button = QPushButton("取消分离"); self.cancel_button.clicked.connect(self.cancel_separation); self.cancel_button.hide(); actions.addWidget(self.cancel_button); actions.addStretch()
        self.cover_button = QPushButton("生成 AI 人声"); self.cover_button.setEnabled(False); self.cover_button.clicked.connect(self.generate_ai_vocal); actions.addWidget(self.cover_button); left.addLayout(actions)
        panel = QFrame(); panel.setObjectName("coverSettings"); panel.setFixedWidth(270); form = QFormLayout(panel); form.addRow(QLabel("翻唱设置"))
        self.profile_combo = VoiceSelector(); self.profile_combo.voice_selected.connect(self._profile_selected); form.addRow("目标声音", self.profile_combo)
        self.pitch = QSpinBox(); self.pitch.setRange(-12, 12); self.pitch.setSuffix(" 半音"); form.addRow("音调", self.pitch)
        self.rights_state = QLabel("歌曲权利：未确认"); self.rights_state.setWordWrap(True); form.addRow(self.rights_state)
        self.uvr_status = QLabel(); self.uvr_status.setObjectName("muted"); self.uvr_status.setWordWrap(True); form.addRow("伴奏分离", self.uvr_status)
        note = QLabel("本地处理 · 不上传音频\n普通分离音轨不是 AI 翻唱成品"); note.setWordWrap(True); form.addRow(note); root.addWidget(panel)

    def import_song(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入歌曲", str(self.project), "音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg)")
        if path: self.set_song(path)

    def set_song(self, path):
        try:
            self.cover_project = CoverProject.create(self.project, title=Path(path).stem); copied = self.cover_project.copy_source(Path(path)); self.track_paths = {0: str(copied)}; lrc = Path(path).with_suffix(".lrc"); destination = None
            if lrc.is_file(): destination = self.cover_project.root / "lyrics" / "lyrics.lrc"; shutil.copy2(lrc, destination); self.cover_project.set_lyrics(destination)
            self._reset_tracks(); self._load_track(0, copied, destination)
        except Exception as exc: self.song_meta.setText("导入失败：" + str(exc))

    def _reset_tracks(self):
        self.sessions.clear()
        for track in self.stems: track.set_status(TrackStatus.EMPTY); track.set_waveform([], 0)
        self.stems[0].set_status(TrackStatus.PROCESSING)

    def _load_track(self, index, path, lrc=None):
        path = Path(path)
        if not path.is_file(): return
        self.stems[index].set_status(TrackStatus.PROCESSING); self.import_button.setEnabled(False); cache = self.cover_project.root / "waveform" if self.cover_project else self.paths.data_root / "cache" / "waveforms"
        thread = _LoadThread(index, path, lrc, self.paths, cache, self); self._threads.add(thread); thread.loaded.connect(self._loaded); thread.failed.connect(self._load_failed); thread.finished.connect(lambda t=thread: self._load_finished(t)); thread.start()

    def _loaded(self, index, session):
        self.sessions[index] = session; self.track_paths[index] = session.audio_path; duration = round(session.metadata.duration_seconds * 1000); self.stems[index].set_waveform(session.peaks, duration); self.stems[index].set_status(TrackStatus.READY)
        if index == 0:
            self.song_title.setText(self.cover_project.title if self.cover_project else Path(session.audio_path).stem); m = session.metadata; self.song_meta.setText(f"{Path(session.audio_path).suffix.upper().lstrip('.')} · {m.duration_seconds:.1f}s · {m.sample_rate} Hz · {m.channels}ch")
            self.lyrics.set_lyrics(session.lyrics); self.lyric_status.setText("已载入 LRC" if session.lyrics else "未载入")
            if self.cover_project: self.cover_project.duration_ms = duration; self.cover_project.save()
            self._select_track(0, False)
        if self.cover_project:
            key = ("original", "vocals", "instrumental")[index] if index < 3 else str(index); cache = self.cover_project.root / "waveform" / f"{session.sha256}.json"
            if cache.is_file(): self.cover_project.set_waveform(cache, key)

    def _load_failed(self, index, error): self.stems[index].set_status(TrackStatus.ERROR); self.song_meta.setText("读取失败：" + error)
    def _load_finished(self, thread): self._threads.discard(thread); self.import_button.setEnabled(not self._threads)

    def import_lyrics(self):
        if not self.cover_project: return
        path, _ = QFileDialog.getOpenFileName(self, "导入歌词", str(self.project), "歌词 (*.lrc *.txt)")
        if path:
            destination = self.cover_project.root / "lyrics" / "lyrics.lrc"; shutil.copy2(path, destination); self.cover_project.set_lyrics(destination); self.lyrics.set_lyrics(parse_lrc(destination.read_text(encoding="utf-8-sig", errors="replace"))); self.lyric_status.setText("已手动载入 LRC")

    def refresh_profiles(self):
        try: self.profile_combo.set_profiles(self.store.list_profiles(self.project))
        except (AttributeError, OSError, TypeError): self.profile_combo.set_profiles([])
        self._update_cover_button()

    def _profile_selected(self, profile_id):
        self.profileChanged.emit(str(profile_id or "")); self._update_cover_button()

    def _selected_profile(self):
        identifier = self.profile_combo.currentData()
        try: return next((p for p in self.store.list_profiles(self.project) if p.id == identifier), None)
        except (AttributeError, OSError, TypeError): return None

    def _update_cover_button(self):
        profile = self._selected_profile(); ready = bool(profile and getattr(profile, "consent_confirmed", False))
        if profile and hasattr(profile, "singing_status"):
            try: ready = ready and profile.singing_status() == "ready"
            except TypeError: ready = ready and profile.singing_status(None) == "ready"
        self.cover_button.setEnabled(bool(self.cover_project and getattr(self.cover_project, "vocal_path", "") and ready and not self._separation_request))

    def generate_ai_vocal(self):
        profile = self._selected_profile()
        if not self.cover_project or not profile: self.song_meta.setText("请选择已授权且已验证歌唱模型的声音"); return
        try: status = profile.singing_status()
        except TypeError: status = profile.singing_status(None)
        if status != "ready": self.song_meta.setText("目标声音的歌唱模型尚未就绪或未验证"); return
        if not self.worker: self.song_meta.setText("本地 Worker 未连接"); return
        payload = {"project_path": str(self.project), "cover_id": self.cover_project.id, "source_relative_path": self.cover_project.vocal_path, "source_sha256": self.cover_project.source_sha256, "profile_id": profile.id, "singing_model_id": profile.active_singing_model_id, "content_origin": "ai_generated"}
        self.cover_button.setText("AI 人声生成中…"); self.cover_button.setEnabled(False); self.song_meta.setText("正在生成 AI 人声")
        try: self._ai_request = self.worker.send("convert_vocal", payload)
        except Exception as exc: self._ai_failed(str(exc))

    def refresh_runtime_status(self):
        self.runtime_status = UVR5RuntimeStatus.detect(self.paths); self.uvr_status.setText("UVR5 已就绪" if self.runtime_status.ready else ("UVR5 文件损坏" if self.runtime_status.status == "corrupt" else ("UVR5 Hash 不匹配" if self.runtime_status.status == "hash_mismatch" else "UVR5 未安装"))); self.separate_button.setEnabled(self.runtime_status.ready)

    def _confirm_rights(self):
        if self.cover_project and self.cover_project.rights_confirmed: return True
        answer = QMessageBox.question(self, "歌曲权利确认", RIGHTS_TEXT + "\n\n这只是您的权利声明，VoiceStudio 不会替您取得版权。", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes: return False
        self.cover_project.attest_rights(True, version=1); self.rights_state.setText("歌曲权利：已确认"); return True

    def restore_cover(self):
        covers = CoverProject.list(self.project)
        if not covers: return
        self.cover_project = max(covers, key=lambda item: item.updated_at); self.rights_state.setText("歌曲权利：已确认" if self.cover_project.rights_confirmed else "歌曲权利：未确认"); self.track_paths = {}; source = self.cover_project.root / self.cover_project.source_relative_path; lrc = self.cover_project.root / self.cover_project.lyrics_path if self.cover_project.lyrics_path else None; self._reset_tracks(); self._load_track(0, source, lrc)
        for index, relative in ((1, self.cover_project.vocal_path), (2, self.cover_project.instrumental_path)):
            if relative: self._load_track(index, self.cover_project.root / relative)
        ai_asset = self.cover_project.get_asset(role="ai_vocal") if hasattr(self.cover_project, "get_asset") else None
        if ai_asset: self._load_track(3, self.cover_project.root / ai_asset.relative_path)
        self._update_cover_button()

    def separate_song(self):
        if not self.cover_project or not self.worker: self.song_meta.setText("请先导入歌曲"); return
        self.refresh_runtime_status()
        if not self.runtime_status.ready or SeparationDialog(self.runtime_status, self).exec() != QDialog.Accepted or not self._confirm_rights(): return
        payload = {"project_path": str(self.project), "cover_id": self.cover_project.id, "source_relative_path": self.cover_project.source_relative_path, "source_sha256": self.cover_project.source_sha256, "mode": "uvr5"}
        self.stems[1].set_status(TrackStatus.PROCESSING); self.stems[2].set_status(TrackStatus.PROCESSING); self.progress.show(); self.progress.set_stage(0); self.separate_button.setEnabled(False); self.cancel_button.show()
        try: self._separation_request = self.worker.send("separate_song", payload)
        except Exception as exc: self._separation_failed(str(exc))

    def cancel_separation(self):
        if self._separation_request and self.worker:
            self.cancel_button.setEnabled(False); self.song_meta.setText("正在停止 UVR5…")
            self.worker.send("cancel", {"target_request_id": self._separation_request})

    def handle_worker_event(self, request_id, event, payload):
        if request_id == self._ai_request:
            if event == "progress": self.song_meta.setText(str(payload.get("message", "正在生成 AI 人声")))
            elif event == "result":
                self._ai_request = ""; self.cover_button.setText("生成 AI 人声"); self._load_track(3, Path(payload.get("ai_vocal_path") or payload["output_path"])); self.song_meta.setText("AI 人声已生成 · AI生成"); self._update_cover_button()
            elif event == "error": self._ai_failed(str(payload.get("message", "生成失败")))
            return
        if not self._separation_request or request_id != self._separation_request: return
        if event == "progress": self.progress.set_stage(STAGE_INDEX.get(str(payload.get("stage", "")), 2)); self.song_meta.setText(str(payload.get("message", "正在分离")))
        elif event == "result":
            self._separation_request = ""; self.separate_button.setEnabled(True); self.cancel_button.hide(); self.cancel_button.setEnabled(True); self.progress.set_stage(5); self.cover_project = CoverProject.load(self.project, self.cover_project.id); self._load_track(1, Path(payload["vocal_path"])); self._load_track(2, Path(payload["instrumental_path"])); self.song_meta.setText("分离完成" + (" · 已复用缓存" if payload.get("cache_hit") else ""))
        elif event == "error": self._separation_failed(str(payload.get("message", "分离失败")))

    def _ai_failed(self, message):
        self._ai_request = ""; self.cover_button.setText("生成 AI 人声"); self.song_meta.setText("AI 人声生成失败：" + message); self._update_cover_button()

    def _separation_failed(self, message):
        self._separation_request = ""; self.separate_button.setEnabled(self.runtime_status.ready); self.cancel_button.hide(); self.cancel_button.setEnabled(True); self.stems[1].set_status(TrackStatus.ERROR); self.stems[2].set_status(TrackStatus.ERROR); self.song_meta.setText(message)

    def _track_mix_changed(self, changed):
        solos = [i for i, track in enumerate(self.stems[:3]) if track.solo.isChecked() and i in self.track_paths]; target = solos[0] if solos else (changed if changed in self.track_paths else self._selected_track); self._select_track(target); current = self.stems[self._selected_track]; self.audio_output.setVolume(0 if current.mute.isChecked() else current.volume.value() / 100)

    def _select_track(self, index, preserve=True):
        if index not in self.track_paths: return
        position, playing = self.player.position(), self.player.playbackState() == QMediaPlayer.PlayingState; self._selected_track = index; self.player.setSource(QUrl.fromLocalFile(self.track_paths[index]))
        if preserve: self.player.setPosition(position)
        if playing: self.player.play()

    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState: self.player.pause()
        elif self._selected_track in self.track_paths: self.player.play()
    def _position(self, value):
        for track in self.stems: track.set_position(value)
        self.lyrics.set_position(value); self.transport.set_timeline(value, self.player.duration())
    def release_resources(self):
        for thread in tuple(self._threads): thread.requestInterruption(); thread.wait()
        self.player.stop(); self.player.setSource(QUrl()); self.player.setAudioOutput(None)
    def closeEvent(self, event): self.release_resources(); super().closeEvent(event)
