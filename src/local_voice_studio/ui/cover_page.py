from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget

from ..cover import CoverProject
from ..cover.application import CoverApplicationService
from ..cover.mixing import CoverMixSettings, GainScale
from ..cover.preview import PlaybackMode, PreviewMixPlanner, PreviewTrack, TrackRole
from ..cover.separation import RoFormerRuntimeStatus, UVR5RuntimeStatus
from .cover_session import SongSession, parse_lrc
from .audio import PreviewAudioController
from .studio_widgets import LyricView, QuickMixerPanel, StemTrackWidget, TaskProgress, TrackStatus, TransportWidget, VoiceSelector

RIGHTS_TEXT = "我确认自己拥有或已经获得处理、使用该音频所需的权利，并理解公开传播或商业发行可能需要额外取得歌曲、录音等相关授权。"
TRACK_NAMES = ("原曲", "原唱人声", "伴奏", "AI 人声", "最终混音")
TRACK_ROLES = (TrackRole.ORIGINAL, TrackRole.VOCAL, TrackRole.INSTRUMENTAL, TrackRole.AI_VOCAL, TrackRole.FINAL_MIX)
STAGE_INDEX = {"validating": 0, "preparing_model": 1, "separating": 2, "generating_waveforms": 3, "saving_project": 4}


def _label(text: str, object_name: str = "") -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


class _LoadThread(QThread):
    loaded = Signal(int, object); failed = Signal(int, str)
    def __init__(self, index, audio, lrc, paths, cache_dir, parent=None):
        super().__init__(parent); self.index, self.audio, self.lrc, self.paths, self.cache_dir = index, Path(audio), lrc, paths, cache_dir
    def run(self):
        try:
            if self.isInterruptionRequested():
                raise InterruptedError("音频读取已取消")
            value = SongSession.load(self.audio, lrc_path=self.lrc, paths=self.paths, cache_dir=self.cache_dir, cancel=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                raise InterruptedError("音频读取已取消")
            self.loaded.emit(self.index, value)
        except Exception as exc: self.failed.emit(self.index, str(exc))


class SeparationDialog(QDialog):
    def __init__(self, runtime, roformer_runtime=None, parent=None):
        super().__init__(parent); self.setWindowTitle("选择歌曲分离方式"); self.setMinimumWidth(520); layout = QVBoxLayout(self)
        self.engine_id = ""
        layout.addWidget(QLabel("歌曲分离"))
        cards = (("uvr5", "UVR5", "快速分离", "已安装 · 可用" if runtime.ready else ("文件损坏" if runtime.status == "corrupt" else "未安装"), runtime.ready),
                 ("roformer", "RoFormer", "高质量分离", "已安装 · 可用" if roformer_runtime and roformer_runtime.ready else ("文件损坏" if roformer_runtime and roformer_runtime.status in {"corrupt", "hash_mismatch"} else "未安装"), bool(roformer_runtime and roformer_runtime.ready)),
                 ("", "多轨", "主唱 / 和声 / 伴奏", "未来版本", False))
        for engine_id, name, detail, state, enabled in cards:
            button = QPushButton(f"{name}\n{detail}    {state}"); button.setObjectName("separatorCard"); button.setEnabled(enabled)
            if enabled: button.clicked.connect(lambda _checked=False, value=engine_id: self._choose(value))
            layout.addWidget(button)
        cancel = QPushButton("取消"); cancel.clicked.connect(self.reject); layout.addWidget(cancel, alignment=Qt.AlignRight)

    def _choose(self, engine_id: str) -> None:
        self.engine_id = engine_id
        self.accept()


class ExportDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent); self.setObjectName("exportDialog"); self.setWindowTitle("导出 AI 翻唱"); self.setMinimumWidth(480)
        layout = QVBoxLayout(self); layout.addWidget(_label("导出 AI 翻唱", "dialogTitle"))
        layout.addWidget(QLabel("文件名")); self.file_name = QLineEdit(title); layout.addWidget(self.file_name)
        layout.addWidget(QLabel("格式")); self.format = QComboBox(); self.format.addItem("WAV", "wav"); self.format.addItem("MP3 320kbps", "mp3"); self.format.addItem("WAV + MP3", "both"); layout.addWidget(self.format)
        layout.addWidget(QLabel("保存位置")); folder_row = QHBoxLayout(); self.destination = QLineEdit(); self.destination.setReadOnly(True); folder_row.addWidget(self.destination, 1); choose = QPushButton("选择…"); choose.clicked.connect(self._choose); folder_row.addWidget(choose); layout.addLayout(folder_row)
        self.ai_marker = QCheckBox("在导出信息中标记 AI 生成"); self.ai_marker.setChecked(True); self.ai_marker.setEnabled(False); layout.addWidget(self.ai_marker)
        self.rights = QCheckBox("我理解公开发布或商业使用可能需要额外授权"); layout.addWidget(self.rights)
        note = QLabel("VoiceStudio 不代用户取得词曲、录音、表演者或其他第三方授权。"); note.setWordWrap(True); note.setObjectName("muted"); layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok); buttons.button(QDialogButtonBox.Ok).setText("导出"); buttons.button(QDialogButtonBox.Cancel).setText("取消"); buttons.accepted.connect(self._accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def _choose(self):
        value = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if value: self.destination.setText(value)

    def _accept(self):
        if not self.file_name.text().strip() or not self.destination.text() or not self.rights.isChecked():
            QMessageBox.warning(self, "导出信息不完整", "请选择保存位置、填写文件名并确认发布权利提醒。"); return
        self.accept()


class CoverPage(QWidget):
    profileChanged = Signal(str)
    render_requested = Signal(dict)
    export_requested = Signal()
    def __init__(self, paths, store, project, worker=None, parent=None):
        super().__init__(parent); self.setObjectName("coverPage"); self.paths, self.store, self.project, self.worker = paths, store, Path(project), worker
        self.cover_project = None; self.sessions = {}; self.track_paths = {}; self._threads = set(); self._separation_request = ""; self._ai_request = ""; self._render_request = ""; self._export_request = ""; self._last_export_payload = {}; self._selected_track = 0; self._playback_mode = PlaybackMode.MIX_PREVIEW
        self.cover_service = CoverApplicationService(self.project, paths=self.paths, store=self.store)
        self.preview_controller = PreviewAudioController.create_qt(self, drift_tolerance_ms=50)
        self.sync_timer = QTimer(self); self.sync_timer.setInterval(750); self.sync_timer.timeout.connect(self._resync_preview)
        self._build(); self.refresh_profiles(); self.refresh_runtime_status(); self.restore_cover()
        for role, channel in self.preview_controller.channels.items():
            channel.player.positionChanged.connect(lambda value, r=role: self._on_player_position(r, value))
            channel.player.durationChanged.connect(lambda _value: self.transport.set_timeline(self._position_ms(), self._timeline_duration()))
            channel.player.playbackStateChanged.connect(lambda state, r=role: self._on_playback_state(r, state))

    # Project lifecycle access stays behind this small UI boundary.  The page
    # handles presentation and dialogs; persistence mutations are centralized
    # here so workflow handlers do not encode storage rules themselves.
    def _new_cover_project(self, title: str) -> CoverProject:
        return CoverProject.create(self.project, title=title)

    def _copy_source(self, cover: CoverProject, source: Path) -> Path:
        return cover.copy_source(source)

    def _copy_lyrics(self, cover: CoverProject, source: Path) -> Path:
        destination = cover.root / "lyrics" / "lyrics.lrc"
        shutil.copy2(source, destination)
        cover.set_lyrics(destination)
        return destination

    def _save_cover(self, cover: CoverProject) -> None:
        cover.save()

    def _set_waveform(self, cover: CoverProject, path: Path, track: str) -> None:
        cover.set_waveform(path, track)

    def _attest_rights(self, cover: CoverProject) -> None:
        cover.attest_rights(True, version=1)

    def _list_cover_projects(self) -> list[CoverProject]:
        return CoverProject.list(self.project)

    def _load_cover_project(self, cover_id: str) -> CoverProject:
        return CoverProject.load(self.project, cover_id)

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(22, 18, 22, 14); root.setSpacing(12)
        header = QFrame(); header.setObjectName("songHeader"); header_layout = QHBoxLayout(header); header_layout.setContentsMargins(0, 0, 0, 0)
        self.cover_art = QFrame(); self.cover_art.setObjectName("coverArt"); self.cover_art.setFixedSize(68, 68); header_layout.addWidget(self.cover_art)
        copy = QVBoxLayout(); eyebrow = QLabel("当前歌曲工程  ·  STEMS"); eyebrow.setObjectName("eyebrow"); copy.addWidget(eyebrow)
        self.song_title = QLabel("还没有导入歌曲"); self.song_title.setObjectName("songTitle"); copy.addWidget(self.song_title)
        self.song_meta = QLabel("支持 WAV / MP3 / FLAC · 导入后自动寻找同名 LRC"); self.song_meta.setObjectName("muted"); copy.addWidget(self.song_meta); header_layout.addLayout(copy, 1)
        self.separate_button = QPushButton("重新分离"); self.separate_button.setObjectName("secondaryButton"); self.separate_button.clicked.connect(self.separate_song); header_layout.addWidget(self.separate_button)
        self.import_button = QPushButton("导入歌曲"); self.import_button.setObjectName("secondaryButton"); self.import_button.clicked.connect(self.import_song); header_layout.addWidget(self.import_button); root.addWidget(header)

        center = QHBoxLayout(); center.setSpacing(14); left = QVBoxLayout(); left.setSpacing(12); center.addLayout(left, 1)
        timeline = QFrame(); timeline.setObjectName("timelineCard"); timeline_layout = QVBoxLayout(timeline); timeline_layout.setContentsMargins(0, 0, 0, 0); timeline_layout.setSpacing(0)
        timeline_head = QHBoxLayout(); timeline_head.setContentsMargins(16, 0, 12, 0); timeline_head.addWidget(_label("多轨波形", "cardTitle")); timeline_head.addWidget(_label("· 点击或拖拽波形可定位播放", "cardSub")); timeline_head.addStretch(); self.zoom_label = QLabel("适应宽度"); self.zoom_label.setObjectName("miniChip"); timeline_head.addWidget(self.zoom_label); timeline_layout.addLayout(timeline_head)
        tracks = QWidget(); tracks.setObjectName("tracks"); tracks_layout = QVBoxLayout(tracks); tracks_layout.setContentsMargins(12, 8, 12, 10); tracks_layout.setSpacing(5); self.stems = []
        for index, name in enumerate(TRACK_NAMES):
            track = StemTrackWidget(name); track.set_status(TrackStatus.EMPTY); track.seek_requested.connect(self._seek_all)
            track.solo_changed.connect(lambda _v, i=index: self._track_mix_changed(i)); track.mute_changed.connect(lambda _v, i=index: self._track_mix_changed(i)); track.volume_changed.connect(lambda _v, i=index: self._track_mix_changed(i)); self.stems.append(track); tracks_layout.addWidget(track)
        timeline_layout.addWidget(tracks, 1); left.addWidget(timeline, 1)
        lower = QHBoxLayout(); lower.setSpacing(12)
        lyrics_card = QFrame(); lyrics_card.setObjectName("lyricsCard"); lyrics_layout = QVBoxLayout(lyrics_card); lyrics_layout.setContentsMargins(0, 0, 0, 0); lyric_header = QHBoxLayout(); lyric_header.setContentsMargins(14, 0, 10, 0); lyric_header.addWidget(_label("同步歌词", "cardTitle")); lyric_header.addStretch(); self.lyric_status = QLabel("未载入"); self.lyric_status.setObjectName("muted"); lyric_header.addWidget(self.lyric_status); self.lyric_import = QPushButton("导入 LRC"); self.lyric_import.setObjectName("miniButton"); self.lyric_import.clicked.connect(self.import_lyrics); lyric_header.addWidget(self.lyric_import); lyrics_layout.addLayout(lyric_header); self.lyrics = LyricView(); self.lyrics.seek_requested.connect(self._seek_all); lyrics_layout.addWidget(self.lyrics, 1); lower.addWidget(lyrics_card, 1)
        self.mixer = QuickMixerPanel(); self.mixer.volume_changed.connect(self._mixer_volume_changed)
        # Keep the compact mixer and timeline controls on one dB/slider scale;
        # the original vocal lane is intentionally silent by default.
        for track_index, mixer_index in ((3, 0), (2, 1), (1, 2)):
            self.stems[track_index].set_volume(self.mixer.sliders[mixer_index].value())
        lower.addWidget(self.mixer); left.addLayout(lower, 1)
        center.addWidget(self._build_settings_panel()); root.addLayout(center, 1)
        self.progress = TaskProgress(); self.progress.hide(); root.addWidget(self.progress)
        actions = QHBoxLayout(); actions.setSpacing(8); self.cancel_button = QPushButton("取消分离"); self.cancel_button.clicked.connect(self.cancel_separation); self.cancel_button.hide(); actions.addWidget(self.cancel_button); self.cancel_ai_button = QPushButton("取消 AI 生成"); self.cancel_ai_button.clicked.connect(self.cancel_ai_vocal); self.cancel_ai_button.hide(); actions.addWidget(self.cancel_ai_button); self.cancel_final_button = QPushButton("取消最终处理"); self.cancel_final_button.clicked.connect(self.cancel_final_task); self.cancel_final_button.hide(); actions.addWidget(self.cancel_final_button); actions.addStretch(); root.addLayout(actions)
        self.transport = TransportWidget(); self.transport.setObjectName("globalTransport"); self.transport.play_requested.connect(self.toggle_playback); self.transport.seek_relative_requested.connect(lambda d: self._seek_all(max(0, min(self._timeline_duration(), self._position_ms() + d)))); self.transport.timeline.sliderMoved.connect(self._seek_all); self.transport.volume_changed.connect(self._set_master_volume); root.addWidget(self.transport)

    def _build_settings_panel(self):
        panel = QFrame(); panel.setObjectName("coverSettings"); panel.setMinimumWidth(286); panel.setMaximumWidth(340); form = QVBoxLayout(panel); form.setContentsMargins(16, 14, 16, 14); form.setSpacing(9)
        form.addWidget(_label("目标声音", "sectionLabel")); self.profile_combo = VoiceSelector(project_root=self.project); self.profile_combo.voice_selected.connect(self._profile_selected); form.addWidget(self.profile_combo)
        self.voice_capabilities = QLabel("✓ AI 翻唱    ✓ 本地处理"); self.voice_capabilities.setObjectName("capabilities"); form.addWidget(self.voice_capabilities)
        form.addWidget(_label("翻唱设置", "cardTitle")); pitch_row = QHBoxLayout(); pitch_row.addWidget(QLabel("音调")); self.pitch = QSpinBox(); self.pitch.setRange(-12, 12); self.pitch.setSuffix(" 半音"); pitch_row.addWidget(self.pitch); form.addLayout(pitch_row)
        self.normalize_toggle = QCheckBox("混音归一化"); self.normalize_toggle.setChecked(True); self.normalize_toggle.setObjectName("settingToggle"); form.addWidget(self.normalize_toggle)
        self.limiter_toggle = QCheckBox("防削波限制器"); self.limiter_toggle.setChecked(True); self.limiter_toggle.setObjectName("settingToggle"); form.addWidget(self.limiter_toggle)
        form.addStretch(); self.rights_state = QLabel("歌曲权利：未确认"); self.rights_state.setWordWrap(True); self.rights_state.setObjectName("rightsState"); form.addWidget(self.rights_state); self.uvr_status = QLabel(); self.uvr_status.setObjectName("muted"); self.uvr_status.setWordWrap(True); form.addWidget(self.uvr_status); note = QLabel("本地处理 · 不上传音频\n普通分离音轨不是 AI 翻唱成品"); note.setObjectName("settingsNote"); note.setWordWrap(True); form.addWidget(note)
        self.cover_button = QPushButton("开始 AI 翻唱"); self.cover_button.setObjectName("primaryButton"); self.cover_button.setMinimumHeight(42); self.cover_button.setEnabled(False); self.cover_button.clicked.connect(self.generate_ai_vocal); form.addWidget(self.cover_button)
        self.render_button = QPushButton("生成最终翻唱"); self.render_button.setObjectName("primaryButton"); self.render_button.setEnabled(False); self.render_button.clicked.connect(self.request_final_render); form.addWidget(self.render_button)
        self.export_button = QPushButton("导出最终混音"); self.export_button.setObjectName("secondaryButton"); self.export_button.setEnabled(False); self.export_button.clicked.connect(self.export_final); form.addWidget(self.export_button)
        return panel

    def _mixer_volume_changed(self, index: int, value: int) -> None:
        track_index = (3, 2, 1)[index] if 0 <= index < 3 else -1
        if track_index >= 0:
            self.stems[track_index].set_volume(value)
            self._refresh_preview_plan()

    def request_final_render(self) -> None:
        profile = self._selected_profile()
        if not self.worker or not self.cover_project or not profile: return
        settings = CoverMixSettings(
            ai_gain_db=GainScale.slider_to_db(self.mixer.sliders[0].value()),
            instrumental_gain_db=GainScale.slider_to_db(self.mixer.sliders[1].value()),
            original_vocal_gain_db=GainScale.slider_to_db(self.mixer.sliders[2].value()),
            master_gain_db=0.0, normalize=self.normalize_toggle.isChecked(), limiter=self.limiter_toggle.isChecked(),
        )
        try:
            payload = self.cover_service.create_render_command(self.cover_project.id, profile.id, settings).to_worker_payload()
        except Exception as exc:
            self._render_failed(str(exc)); return
        self.render_requested.emit(payload); self.render_button.setEnabled(False); self.render_button.setText("正在生成最终翻唱…"); self.cancel_final_button.show(); self.progress.show(); self.progress.set_stage(0)
        try: self._render_request = self.worker.send("render_cover", payload)
        except Exception as exc: self._render_failed(str(exc))

    def export_final(self):
        if not self.cover_project or not self.worker: return
        final = self.cover_project.get_asset(role="final_mix")
        if not final: return
        dialog = ExportDialog(self.cover_project.title, self)
        if dialog.exec() != QDialog.Accepted: return
        try:
            payload = self.cover_service.create_export_command(
                self.cover_project.id, final_asset_id=final.id, format=dialog.format.currentData(),
                file_name=dialog.file_name.text().strip(), destination=Path(dialog.destination.text()),
                existing_policy="reject", publication_rights_acknowledged=dialog.rights.isChecked(),
            ).to_worker_payload()
        except Exception as exc:
            QMessageBox.warning(self, "无法准备导出", str(exc)); return
        self._last_export_payload = dict(payload); self.export_requested.emit(); self.export_button.setEnabled(False); self.export_button.setText("正在导出…"); self.cancel_final_button.show()
        try: self._export_request = self.worker.send("export_cover", payload)
        except Exception as exc: self._export_failed(str(exc))

    def import_song(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入歌曲", str(self.project), "音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg)")
        if path: self.set_song(path)

    def set_song(self, path):
        try:
            self.cover_project = self._new_cover_project(Path(path).stem); self.rights_state.setText("歌曲权利：未确认"); copied = self._copy_source(self.cover_project, Path(path)); self.track_paths = {0: str(copied)}; lrc = Path(path).with_suffix(".lrc"); destination = None
            if lrc.is_file(): destination = self._copy_lyrics(self.cover_project, lrc)
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
        if index == 3: self.stems[index].name_label.setText("AI 人声  AI生成")
        if index == 0:
            self.song_title.setText(self.cover_project.title if self.cover_project else Path(session.audio_path).stem); m = session.metadata; self.song_meta.setText(f"{Path(session.audio_path).suffix.upper().lstrip('.')} · {m.duration_seconds:.1f}s · {m.sample_rate} Hz · {m.channels}ch")
            self.lyrics.set_lyrics(session.lyrics); self.lyric_status.setText("已载入 LRC" if session.lyrics else "未载入")
            if self.cover_project: self.cover_project.duration_ms = duration; self._save_cover(self.cover_project)
            self._select_track(0, False)
        if self.cover_project:
            key = ("original", "vocals", "instrumental")[index] if index < 3 else str(index); cache = self.cover_project.root / "waveform" / f"{session.sha256}.json"
            if cache.is_file(): self._set_waveform(self.cover_project, cache, key)

    def _load_failed(self, index, error): self.stems[index].set_status(TrackStatus.ERROR); self.song_meta.setText("读取失败：" + error)
    def _load_finished(self, thread): self._threads.discard(thread); self.import_button.setEnabled(not self._threads)

    def import_lyrics(self):
        if not self.cover_project: return
        path, _ = QFileDialog.getOpenFileName(self, "导入歌词", str(self.project), "歌词 (*.lrc *.txt)")
        if path:
            destination = self._copy_lyrics(self.cover_project, Path(path)); self.lyrics.set_lyrics(parse_lrc(destination.read_text(encoding="utf-8-sig", errors="replace"))); self.lyric_status.setText("已手动载入 LRC")

    def refresh_profiles(self):
        try: self.profile_combo.project_root = self.project; self.profile_combo.set_profiles(self.store.list_profiles(self.project))
        except (AttributeError, OSError, TypeError): self.profile_combo.set_profiles([])
        self._update_cover_button()

    def _profile_selected(self, profile_id):
        self.profileChanged.emit(str(profile_id or "")); self._update_cover_button()

    def _selected_profile(self):
        identifier = self.profile_combo.currentData()
        try: return next((p for p in self.store.list_profiles(self.project) if p.id == identifier), None)
        except (AttributeError, OSError, TypeError): return None

    def _update_cover_button(self):
        vocal = self.cover_project.get_asset(role="vocal") if self.cover_project else None
        vocal_path = self.cover_project.root / vocal.relative_path if vocal else None
        vocal_valid = bool(vocal and vocal.content_origin == "separated" and vocal_path and vocal_path.is_file() and vocal.sha256 == hashlib.sha256(vocal_path.read_bytes()).hexdigest())
        profile = self._selected_profile(); ready = bool(self.cover_project and self.cover_project.rights_confirmed and vocal_valid and profile and getattr(profile, "consent_confirmed", False) and getattr(profile, "consent_record", "") and getattr(profile, "consent_confirmed_at", "") and not getattr(profile, "archived", False) and self.worker and not self._separation_request and not self._ai_request)
        if profile and hasattr(profile, "singing_status"):
            try: ready = ready and profile.singing_status(self.project) == "ready"
            except TypeError: ready = ready and profile.singing_status() == "ready"
        self.cover_button.setEnabled(ready)

    def generate_ai_vocal(self):
        profile = self._selected_profile()
        if not self.cover_project or not profile: self.song_meta.setText("请选择已授权且已验证歌唱模型的声音"); return
        try: status = profile.singing_status(self.project)
        except TypeError: status = profile.singing_status()
        if status != "ready": self.song_meta.setText("目标声音的歌唱模型尚未就绪或未验证"); return
        if not self.worker: self.song_meta.setText("本地 Worker 未连接"); return
        try:
            payload = self.cover_service.create_ai_vocal_command(
                self.cover_project.id, profile.id, pitch_shift=self.pitch.value()
            ).to_worker_payload()
        except Exception as exc:
            self.song_meta.setText(str(exc)); return
        self.cover_button.setText("AI 人声生成中…"); self.cover_button.setEnabled(False); self.cancel_ai_button.setEnabled(True); self.cancel_ai_button.show(); self.song_meta.setText("正在生成 AI 人声")
        try: self._ai_request = self.worker.send("convert_vocal", payload)
        except Exception as exc: self._ai_failed(str(exc))

    def refresh_runtime_status(self):
        self.runtime_status = UVR5RuntimeStatus.detect(self.paths); self.roformer_runtime_status = RoFormerRuntimeStatus.detect(self.paths)
        available = [name for name, state in (("UVR5", self.runtime_status), ("RoFormer", self.roformer_runtime_status)) if state.ready]
        self.uvr_status.setText("分离引擎已就绪：" + " / ".join(available) if available else "歌曲分离引擎未安装")
        self.separate_button.setEnabled(bool(available))

    def _confirm_rights(self):
        if self.cover_project and self.cover_project.rights_confirmed and self.cover_project.rights_attestation_text_hash: return True
        answer = QMessageBox.question(self, "歌曲权利确认", RIGHTS_TEXT + "\n\n这只是您的权利声明，VoiceStudio 不会替您取得版权。", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes: return False
        self._attest_rights(self.cover_project); self.rights_state.setText("歌曲权利：已确认"); return True

    def restore_cover(self):
        covers = self._list_cover_projects()
        if not covers: return
        self.cover_project = max(covers, key=lambda item: item.updated_at); self.rights_state.setText("歌曲权利：已确认" if self.cover_project.rights_confirmed else "歌曲权利：未确认"); self.track_paths = {}; source = self.cover_project.root / self.cover_project.source_relative_path; lrc = self.cover_project.root / self.cover_project.lyrics_path if self.cover_project.lyrics_path else None; self._reset_tracks(); self._load_track(0, source, lrc)
        for index, relative in ((1, self.cover_project.vocal_path), (2, self.cover_project.instrumental_path)):
            if relative: self._load_track(index, self.cover_project.root / relative)
        ai_asset = self.cover_project.get_asset(role="ai_vocal") if hasattr(self.cover_project, "get_asset") else None
        if ai_asset: self._load_track(3, self.cover_project.root / ai_asset.relative_path)
        final_asset = self.cover_project.get_asset(role="final_mix")
        if final_asset:
            final_path = self.cover_project.root / final_asset.relative_path
            if final_path.is_file(): self._load_track(4, final_path); self.export_button.setEnabled(True)
        self._update_cover_button()

    def separate_song(self):
        if not self.cover_project or not self.worker: self.song_meta.setText("请先导入歌曲"); return
        self.refresh_runtime_status()
        dialog = SeparationDialog(self.runtime_status, self.roformer_runtime_status, self)
        if dialog.exec() != QDialog.Accepted or not dialog.engine_id or not self._confirm_rights(): return
        try:
            payload = self.cover_service.create_separation_command(self.cover_project.id, mode=dialog.engine_id).to_worker_payload()
        except Exception as exc:
            self._separation_failed(str(exc)); return
        self.stems[1].set_status(TrackStatus.PROCESSING); self.stems[2].set_status(TrackStatus.PROCESSING); self.progress.show(); self.progress.set_stage(0); self.separate_button.setEnabled(False); self.cancel_button.show()
        try: self._separation_request = self.worker.send("separate_song", payload)
        except Exception as exc: self._separation_failed(str(exc))

    def cancel_separation(self):
        if self._separation_request and self.worker:
            self.cancel_button.setEnabled(False); self.song_meta.setText("正在停止歌曲分离…")
            self.worker.send("cancel", {"target_request_id": self._separation_request})

    def handle_worker_event(self, request_id, event, payload):
        if request_id == self._render_request:
            if event == "progress": self.song_meta.setText(str(payload.get("message", "正在生成最终翻唱")))
            elif event == "result":
                self._render_request = ""; self.render_button.setText("生成最终翻唱"); self.cancel_final_button.hide(); self.progress.hide(); self.cover_project = self._load_cover_project(self.cover_project.id); asset = self.cover_project.get_asset(str(payload.get("asset_id", "")))
                if not asset: self._render_failed("最终混音未登记为项目资产"); return
                self._load_track(4, self.cover_project.root / asset.relative_path); self.export_button.setEnabled(True); self.song_meta.setText("最终翻唱已生成" + (" · 已复用缓存" if payload.get("cache_hit") else " · AI生成"))
            elif event == "error": self._render_failed(str(payload.get("message", "最终混音失败")))
            return
        if request_id == self._export_request:
            if event == "progress": self.song_meta.setText(str(payload.get("message", "正在导出")))
            elif event == "result":
                self._export_request = ""; self.export_button.setText("导出最终混音"); self.export_button.setEnabled(True); self.cancel_final_button.hide(); self.song_meta.setText("导出完成：" + "、".join(Path(x).name for x in payload.get("outputs", [])))
            elif event == "error":
                message = str(payload.get("message", "导出失败"))
                if "已存在" in message and QMessageBox.question(self, "文件已经存在", "文件已经存在。是否覆盖？", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
                    retry = dict(self._last_export_payload); retry["existing_policy"] = "replace"; self._export_request = self.worker.send("export_cover", retry)
                else: self._export_failed(message)
            return
        if request_id == self._ai_request:
            if event == "progress": self.song_meta.setText(str(payload.get("message", "正在生成 AI 人声")))
            elif event == "result":
                self._ai_request = ""; self.cover_button.setText("生成 AI 人声"); self.cancel_ai_button.hide(); self.cover_project = self._load_cover_project(self.cover_project.id); self.rights_state.setText("歌曲权利：已确认" if self.cover_project.rights_confirmed else "歌曲权利：未确认"); ai_asset = self.cover_project.get_asset(str(payload.get("asset_id", ""))) or self.cover_project.get_asset(role="ai_vocal"); ai_path = self.cover_project.root / ai_asset.relative_path if ai_asset else None
                if not ai_asset or not ai_path.is_file(): self._ai_failed("生成结果未登记为项目资产"); return
                self._load_track(3, ai_path); self.song_meta.setText("AI 人声已生成 · " + ("已复用缓存" if payload.get("cache_hit") else "AI生成")); self.render_button.setEnabled(True); self._update_cover_button()
            elif event == "error": self._ai_failed(str(payload.get("message", "生成失败")))
            return
        if not self._separation_request or request_id != self._separation_request: return
        if event == "progress": self.progress.set_stage(STAGE_INDEX.get(str(payload.get("stage", "")), 2)); self.song_meta.setText(str(payload.get("message", "正在分离")))
        elif event == "result":
            self._separation_request = ""; self.separate_button.setEnabled(True); self.cancel_button.hide(); self.cancel_button.setEnabled(True); self.progress.set_stage(5); self.cover_project = self._load_cover_project(self.cover_project.id); self._load_track(1, Path(payload["vocal_path"])); self._load_track(2, Path(payload["instrumental_path"])); self.song_meta.setText("分离完成" + (" · 已复用缓存" if payload.get("cache_hit") else ""))
        elif event == "error": self._separation_failed(str(payload.get("message", "分离失败")))

    def _ai_failed(self, message):
        self._ai_request = ""; self.cover_button.setText("生成 AI 人声"); self.cancel_ai_button.hide(); self.cancel_ai_button.setEnabled(True); self.song_meta.setText("AI 人声生成失败：" + message); self._update_cover_button()

    def _render_failed(self, message):
        self._render_request = ""; self.render_button.setText("生成最终翻唱"); self.render_button.setEnabled(bool(self.cover_project and self.cover_project.get_asset(role="ai_vocal"))); self.cancel_final_button.hide(); self.cancel_final_button.setEnabled(True); self.progress.hide(); self.song_meta.setText("最终混音失败：" + message)

    def _export_failed(self, message):
        self._export_request = ""; self.export_button.setText("导出最终混音"); self.export_button.setEnabled(bool(self.cover_project and self.cover_project.get_asset(role="final_mix"))); self.cancel_final_button.hide(); self.cancel_final_button.setEnabled(True); self.song_meta.setText("导出失败：" + message)

    def cancel_final_task(self):
        target = self._render_request or self._export_request
        if target and self.worker:
            self.cancel_final_button.setEnabled(False); self.song_meta.setText("正在停止最终处理…")
            self.worker.send("cancel", {"target_request_id": target})

    def cancel_ai_vocal(self):
        if self._ai_request and self.worker:
            self.cancel_ai_button.setEnabled(False); self.song_meta.setText("正在停止 AI 人声生成…")
            try: self.worker.send("cancel", {"target_request_id": self._ai_request})
            except Exception as exc: self._ai_failed(str(exc))

    def _separation_failed(self, message):
        self._separation_request = ""; self.separate_button.setEnabled(self.runtime_status.ready); self.cancel_button.hide(); self.cancel_button.setEnabled(True); self.stems[1].set_status(TrackStatus.ERROR); self.stems[2].set_status(TrackStatus.ERROR); self.song_meta.setText(message)

    def _track_role(self, index: int) -> TrackRole:
        return TRACK_ROLES[max(0, min(len(TRACK_ROLES) - 1, int(index)))]

    def _position_ms(self) -> int:
        return int(self.preview_controller.master_position_ms)

    def _timeline_duration(self) -> int:
        return max((int(session.metadata.duration_seconds * 1000) for session in self.sessions.values()), default=int(getattr(self.cover_project, "duration_ms", 0) if self.cover_project else 0))

    def _preview_plan(self, mode: PlaybackMode | None = None):
        tracks = {}
        for index, path in self.track_paths.items():
            if index < 0 or index >= len(self.stems):
                continue
            role = self._track_role(index)
            stem = self.stems[index]
            gain_db = GainScale.slider_to_db(stem.volume.value())
            tracks[role] = PreviewTrack(
                role, path=str(path), duration_ms=self._timeline_duration(),
                gain=GainScale.db_to_linear(gain_db), muted=stem.mute.isChecked(), solo=stem.solo.isChecked(),
            )
        selected = self._track_role(self._selected_track)
        return PreviewMixPlanner(tracks).plan(selected, mode or self._playback_mode)

    def _refresh_preview_plan(self, mode: PlaybackMode | None = None, preserve_position: bool = True) -> None:
        was_playing = self.preview_controller.playing
        position = self._position_ms() if preserve_position else 0
        self.preview_controller.apply_plan(self._preview_plan(mode))
        self.preview_controller.seek(position)
        self._apply_preview_gains()
        if was_playing:
            self.preview_controller.play()

    def _track_mix_changed(self, changed):
        solos = [i for i, track in enumerate(self.stems[:4]) if track.solo.isChecked() and i in self.track_paths]
        if solos:
            self._playback_mode = PlaybackMode.SOLO_TRACK
            self._selected_track = solos[0]
        else:
            self._playback_mode = PlaybackMode.MIX_PREVIEW
            if changed in self.track_paths:
                self._selected_track = changed
        self._refresh_preview_plan()

    def _select_track(self, index, preserve=True):
        if index not in self.track_paths:
            return
        self._selected_track = int(index)
        self._refresh_preview_plan(preserve_position=preserve)

    def toggle_playback(self):
        if self.preview_controller.playing:
            self.preview_controller.pause(); self.sync_timer.stop(); return
        if self._selected_track not in self.track_paths:
            return
        if self._selected_track == 4:
            self._playback_mode = PlaybackMode.FINAL_MIX
        elif all(item in self.track_paths for item in (2, 3)) and not any(track.solo.isChecked() for track in self.stems[:4]):
            self._playback_mode = PlaybackMode.MIX_PREVIEW
        else:
            self._playback_mode = PlaybackMode.SOLO_TRACK
        self._refresh_preview_plan()
        self.preview_controller.play()
        if len(self.preview_controller.plan.active_tracks if self.preview_controller.plan else ()) > 1:
            self.sync_timer.start()

    def _seek_all(self, value):
        self.preview_controller.seek(int(value)); self._position(int(value))

    def _set_master_volume(self, value):
        self._apply_preview_gains(value)

    def _apply_preview_gains(self, master=None):
        slider = self.transport.volume.value() if master is None and hasattr(self, "transport") else (80 if master is None else int(round(float(master) * 100)))
        master_linear = GainScale.db_to_linear(GainScale.slider_to_db(slider))
        solos = {self._track_role(i) for i, track in enumerate(self.stems[:4]) if track.solo.isChecked()}
        plan = self.preview_controller.plan
        requested: dict[TrackRole, float] = {}
        for index in self.track_paths:
            role = self._track_role(index); stem = self.stems[index]
            track = plan.tracks.get(role) if plan else None
            if track is None:
                continue
            audible = not stem.mute.isChecked() and (not solos or role in solos)
            requested[role] = track.gain * master_linear if audible else 0.0
        peak = max(requested.values(), default=0.0)
        scale = 1.0 / peak if peak > 1.0 else 1.0
        for role, gain in requested.items():
            self.preview_controller.set_gain(role, gain * scale)

    def _on_player_position(self, role: TrackRole, value: int) -> None:
        if self.preview_controller.master_role in (None, role):
            self.preview_controller.master_position_ms = int(value)
            self._position(int(value))

    def _on_playback_state(self, role: TrackRole, state) -> None:
        if self.preview_controller.master_role in (None, role):
            self.transport.set_playing(state == QMediaPlayer.PlayingState)

    def _resync_preview(self):
        self.preview_controller.resync()

    def _position(self, value):
        for track in self.stems: track.set_position(value)
        self.lyrics.set_position(value); self.transport.set_timeline(value, self._timeline_duration())

    def release_resources(self):
        for thread in tuple(self._threads):
            thread.requestInterruption()
            if not thread.wait(5000) and thread.isRunning(): thread.terminate(); thread.wait(1000)
        self.sync_timer.stop(); self.preview_controller.stop()
        for channel in self.preview_controller.channels.values():
            channel.player.setSource(QUrl()); channel.player.setAudioOutput(None)
    def closeEvent(self, event): self.release_resources(); super().closeEvent(event)
