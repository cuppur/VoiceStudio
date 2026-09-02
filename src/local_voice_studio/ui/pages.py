from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QApplication, QTextBrowser, QVBoxLayout, QWidget,
)

from ..audio import AudioProbe, copy_original, scan_audio_files, sha256_file
from ..models import DatasetManifest, DatasetSegment, Job, JobKind, JobStatus, ReferenceAsset, SourceAsset, VoiceProfile, dataset_snapshot_sha256, utc_now
from ..paths import AppPaths
from ..storage import StudioStore
from ..text import split_text
from .recording import Recorder
from .worker_client import WorkerClient


def _show_error(parent: QWidget, message: str) -> None:
    QMessageBox.critical(parent, "本地声音工坊", message)


def _friendly_error(message: str) -> str:
    lowered = message.lower()
    if "no module named 'torch'" in lowered or 'no module named "torch"' in lowered:
        return "本地引擎尚未安装 PyTorch，请进入“设置”并点击“安装/修复本地引擎”。"
    if "gpt-sovits 尚未安装" in lowered or "模型文件不完整" in lowered:
        return "GPT-SoVITS 引擎或模型尚未安装完整，请进入“设置”执行修复。"
    return message


def _parse_asr_result(raw_text: str, fallback_language: str = "zh") -> tuple[str, str, list[str]]:
    """Turn SenseVoice control tags into editable text and quality metadata."""
    tags = re.findall(r"<\|([^|]+)\|>", raw_text)
    text = re.sub(r"<\|[^|]+\|>", "", raw_text).strip()
    known_languages = {"zh", "en", "yue", "ja", "ko", "auto"}
    language = next((tag.lower() for tag in tags if tag.lower() in known_languages), fallback_language.lower())
    flags: list[str] = []
    if any(tag.upper() == "BGM" for tag in tags):
        flags.append("疑似伴奏")
    return language, text, flags


class AudioScanThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, paths: list[Path], parent=None):
        super().__init__(parent); self.paths = paths

    def run(self) -> None:
        try: self.completed.emit(scan_audio_files(self.paths))
        except Exception as exc: self.failed.emit(str(exc))


class GeneratePage(QWidget):
    job_created = Signal(object)

    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__()
        self.store, self.project, self.client = store, project, client
        self.profiles: list[VoiceProfile] = []
        self.pending: dict[str, dict] = {}
        self.active_job: Job | None = None
        self.preview_request = ""
        self.replacement_preview = ""
        self.audio_output = QAudioOutput(self); self.player = QMediaPlayer(self); self.player.setAudioOutput(self.audio_output)
        self._build()
        self.player.durationChanged.connect(lambda value: self.playback_progress.setRange(0, max(0, value))); self.player.positionChanged.connect(self.playback_progress.setValue); self.player.errorOccurred.connect(lambda *_: self.preview_status.setText("试听失败：" + self.player.errorString()))
        self.refresh_profiles()
        client.event.connect(self._on_event)
        client.stderr_line.connect(lambda line: self.log.appendPlainText(line))

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("文字转语音")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.profile = QComboBox()
        self.language = QComboBox(); self.language.addItem("中文及中英混合", "zh"); self.language.addItem("自动识别", "auto")
        self.text = QPlainTextEdit(); self.text.setPlaceholderText("输入短台词或粘贴长文本……"); self.text.setMinimumHeight(180)
        self.output = QLineEdit(str(self.project / "exports"))
        output_row = QWidget(); output_layout = QHBoxLayout(output_row); output_layout.setContentsMargins(0, 0, 0, 0); output_layout.addWidget(self.output)
        browse = QPushButton("选择…"); browse.clicked.connect(self._choose_output); output_layout.addWidget(browse)
        form.addRow("声音配置", self.profile); form.addRow("语言", self.language); form.addRow("文字", self.text); form.addRow("输出目录", output_row)
        layout.addLayout(form)

        controls = QHBoxLayout()
        self.speed = QDoubleSpinBox(); self.speed.setRange(0.6, 1.6); self.speed.setSingleStep(0.05); self.speed.setValue(1.0)
        self.pause = QDoubleSpinBox(); self.pause.setRange(0.05, 2.0); self.pause.setSingleStep(0.05); self.pause.setValue(0.3); self.pause.setSuffix(" 秒")
        controls.addWidget(QLabel("语速")); controls.addWidget(self.speed); controls.addSpacing(16); controls.addWidget(QLabel("段间停顿")); controls.addWidget(self.pause); controls.addStretch()
        layout.addLayout(controls)

        advanced = QGroupBox("高级参数"); advanced.setCheckable(True); advanced.setChecked(False)
        advanced_form = QFormLayout(advanced)
        self.top_k = QSpinBox(); self.top_k.setRange(1, 100); self.top_k.setValue(15)
        self.top_p = QDoubleSpinBox(); self.top_p.setRange(0.1, 1.0); self.top_p.setSingleStep(0.05); self.top_p.setValue(1.0)
        self.temperature = QDoubleSpinBox(); self.temperature.setRange(0.1, 2.0); self.temperature.setSingleStep(0.05); self.temperature.setValue(1.0)
        self.seed = QSpinBox(); self.seed.setRange(-1, 2147483647); self.seed.setValue(-1); self.seed.setSpecialValueText("随机")
        advanced_form.addRow("Top K", self.top_k); advanced_form.addRow("Top P", self.top_p); advanced_form.addRow("Temperature", self.temperature); advanced_form.addRow("随机种子", self.seed)
        layout.addWidget(advanced)
        row = QHBoxLayout()
        self.preview = QPushButton("生成试听"); self.preview.clicked.connect(self._preview_generate)
        self.play_pause = QPushButton("播放/暂停"); self.play_pause.setEnabled(False); self.play_pause.clicked.connect(self._toggle_playback)
        self.stop_preview = QPushButton("停止"); self.stop_preview.setEnabled(False); self.stop_preview.clicked.connect(self.player.stop)
        self.generate = QPushButton("生成 WAV + MP3"); self.generate.setObjectName("primaryButton"); self.generate.clicked.connect(self._generate)
        self.resume = QPushButton("恢复上次未完成"); self.resume.clicked.connect(self._resume); self.resume.setEnabled(bool(self.store.get_setting("last_incomplete_synthesis")))
        self.cancel = QPushButton("取消"); self.cancel.setEnabled(False); self.cancel.clicked.connect(lambda: self.client.send("cancel"))
        row.addWidget(self.preview); row.addWidget(self.play_pause); row.addWidget(self.stop_preview); row.addWidget(self.generate); row.addWidget(self.resume); row.addWidget(self.cancel); row.addStretch(); layout.addLayout(row)
        self.progress = QProgressBar(); self.progress.setRange(0, 100); layout.addWidget(self.progress)
        self.preview_status = QLabel("试听：尚未生成"); self.preview_status.setObjectName("hint"); layout.addWidget(self.preview_status)
        self.playback_progress = QSlider(Qt.Horizontal); self.playback_progress.setRange(0, 0); self.playback_progress.sliderMoved.connect(self.player.setPosition); layout.addWidget(self.playback_progress)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(130); layout.addWidget(self.log)

    def refresh_profiles(self) -> None:
        self.profiles = self.store.list_profiles(self.project)
        self.profile.clear()
        for item in self.profiles:
            approved = [ref for ref in item.reference_assets if ref.approved and ref.transcript.strip()]
            self.profile.addItem(item.name + ("" if approved else "（缺少已校对参考）"), item.id)

    def _choose_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if selected:
            self.output.setText(selected)

    def _generate(self) -> None:
        self._start_synthesis(False)

    def _preview_generate(self) -> None:
        selected = self.text.textCursor().selectedText().strip()
        value = selected or (split_text(self.text.toPlainText().strip(), 120) or [""])[0]
        if len(value) > 200: _show_error(self, "试听文字不能超过 200 个字符"); return
        if self.preview_request:
            self.replacement_preview = value; self.preview_status.setText("试听：正在替换旧任务……")
            try:
                context = self.pending.get(self.preview_request, {})
                if context.get("stage") == "synthesize": self.client.send("cancel")
            except Exception as exc: self.preview_status.setText("试听取消失败：" + str(exc))
            return
        self._start_synthesis(True, value)

    def _start_synthesis(self, preview: bool, preview_text: str = "") -> None:
        index = self.profile.currentIndex()
        if index < 0:
            _show_error(self, "请先在“声音库”创建声音配置")
            return
        text = preview_text or self.text.toPlainText().strip()
        if not text:
            _show_error(self, "请输入要生成的文字")
            return
        profile = self.profiles[index]
        if not profile.consent_confirmed:
            _show_error(self, "请先在“声音库”确认该声音属于本人或已取得明确授权")
            return
        refs = [ref for ref in profile.reference_assets if ref.approved and ref.transcript.strip()]
        if not refs:
            _show_error(self, "该声音配置没有已校对的参考片段")
            return
        ref = refs[0]
        payload = {
            "text": text, "text_lang": self.language.currentData(), "ref_audio_path": ref.path,
            "prompt_text": ref.transcript, "prompt_lang": ref.language, "output_dir": self.output.text(),
            "speed_factor": self.speed.value(), "fragment_interval": self.pause.value(),
            "top_k": self.top_k.value(), "top_p": self.top_p.value(), "temperature": self.temperature.value(),
            "seed": self.seed.value(), "max_chars": 120, "profile_id": profile.id, "preview": preview,
        }
        job = Job(JobKind.SYNTHESIZE, payload)
        self.store.save_job(job); self.job_created.emit(job)
        profile_payload = profile.to_dict(); profile_payload["project_path"] = str(self.project)
        request = self.client.send("load_profile", profile_payload)
        self.pending[request] = {"stage": "load_profile", "job": job, "payload": payload}
        if preview: self.preview_request = request; self.preview_status.setText("试听：正在加载模型……")
        self.active_job = job
        self.generate.setEnabled(False); self.preview.setEnabled(False); self.cancel.setEnabled(True); self.progress.setValue(0)
        self.log.appendPlainText(f"文本将分为 {len(split_text(text))} 段，正在加载模型……")

    def _resume(self) -> None:
        saved = self.store.get_setting("last_incomplete_synthesis")
        if not saved: self.resume.setEnabled(False); return
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == saved.get("profile_id")), None)
        if not profile: _show_error(self, "原声音配置已不存在，无法恢复"); return
        job = Job(JobKind.SYNTHESIZE, saved); self.store.save_job(job); self.job_created.emit(job); profile_payload = profile.to_dict(); profile_payload["project_path"] = str(self.project); request = self.client.send("load_profile", profile_payload); self.pending[request] = {"stage": "load_profile", "job": job, "payload": saved}; self.active_job = job; self.generate.setEnabled(False); self.resume.setEnabled(False); self.cancel.setEnabled(True); self.log.appendPlainText("正在恢复上次未完成的分段……")

    def _on_event(self, request_id: str, event: str, payload: dict) -> None:
        context = self.pending.get(request_id)
        if not context: return
        stage = context["stage"]; job: Job = context["job"]; synthesis: dict = context["payload"]
        if stage == "load_profile" and event == "result":
            self.pending.pop(request_id, None)
            if synthesis.get("preview") and self.replacement_preview:
                replacement = self.replacement_preview; self.replacement_preview = ""; self.preview_request = ""; job.status = JobStatus.CANCELLED; job.message = "试听已被新请求替换"; self.store.save_job(job); self._finish(); QTimer.singleShot(0, lambda value=replacement: self._start_synthesis(True, value)); return
            request = self.client.send("synthesize", synthesis)
            self.pending[request] = {"stage": "synthesize", "job": job, "payload": synthesis}
            if synthesis.get("preview"): self.preview_request = request
            job.status = JobStatus.RUNNING; self.store.save_job(job); return
        if event == "progress":
            if stage != "synthesize": return
            job.progress = float(payload.get("progress", 0)); job.message = str(payload.get("message", "")); job.status = JobStatus.RUNNING
            if payload.get("job_dir"):
                job.payload["resume_dir"] = payload["job_dir"]; self.store.set_setting("last_incomplete_synthesis", job.payload); self.resume.setEnabled(True)
            self.progress.setValue(round(job.progress * 100)); self.log.appendPlainText(job.message); self.store.save_job(job)
        elif event == "result":
            if stage != "synthesize": return
            self.pending.pop(request_id, None); job.progress = 1; job.status = JobStatus.COMPLETED; job.outputs = list(payload.get("outputs", [])); self.store.save_job(job)
            if payload.get("preview") and job.outputs:
                preview_file = Path(job.outputs[0]); self.player.setSource(QUrl.fromLocalFile(str(preview_file))); self.player.play(); self.play_pause.setEnabled(True); self.stop_preview.setEnabled(True); self.preview_status.setText("试听：正在播放 " + preview_file.name); self.preview_request = ""
            else: self.store.set_setting("last_incomplete_synthesis", None); self.resume.setEnabled(False)
            self.progress.setValue(100); self.log.appendPlainText("生成完成：" + "，".join(job.outputs)); self._finish()
        elif event == "error":
            self.pending.pop(request_id, None); job.status = JobStatus.CANCELLED if payload.get("status") == "cancelled" else JobStatus.FAILED; job.error = str(payload.get("message", "")); self.store.save_job(job)
            replacement = self.replacement_preview if job.payload.get("preview") else ""
            if job.payload.get("preview"): self.preview_status.setText("试听失败：" + job.error); self.preview_request = ""; self.replacement_preview = ""
            self.log.appendPlainText("失败：" + job.error); self._finish()
            if replacement: QTimer.singleShot(0, lambda value=replacement: self._start_synthesis(True, value))
            elif job.status != JobStatus.CANCELLED: _show_error(self, _friendly_error(job.error))

    def _finish(self) -> None:
        self.generate.setEnabled(True); self.preview.setEnabled(True); self.cancel.setEnabled(False); self.resume.setEnabled(bool(self.store.get_setting("last_incomplete_synthesis"))); self.active_job = None

    def _toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState: self.player.pause()
        else: self.player.play()


class VoiceLibraryPage(QWidget):
    profiles_changed = Signal()
    open_training_requested = Signal()

    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.store, self.project, self.client = store, project, client; self.probes: list[AudioProbe] = []; self.scan_thread: AudioScanThread | None = None; self.cleanup_requests: dict[str, str] = {}; self._build(); self.refresh_profiles(); client.event.connect(self._on_worker_event)

    def _build(self) -> None:
        layout = QVBoxLayout(self); title = QLabel("声音库"); title.setObjectName("pageTitle"); layout.addWidget(title)
        create = QGroupBox("创建声音配置"); form = QFormLayout(create)
        self.name = QLineEdit("我的声音"); self.consent = QCheckBox("我确认这是本人声音或已经取得明确授权")
        form.addRow("名称", self.name); form.addRow("授权", self.consent)
        actions = QHBoxLayout(); self.import_files = QPushButton("导入音频"); self.import_files.clicked.connect(self._import_files); self.import_folder = QPushButton("导入文件夹"); self.import_folder.clicked.connect(self._import_folder); self.denoise = QPushButton("生成降噪副本"); self.denoise.clicked.connect(lambda: self._cleanup("denoise")); self.uvr = QPushButton("分离人声副本"); self.uvr.clicked.connect(lambda: self._cleanup("uvr")); actions.addWidget(self.import_files); actions.addWidget(self.import_folder); actions.addWidget(self.denoise); actions.addWidget(self.uvr); actions.addStretch(); form.addRow(actions)
        layout.addWidget(create)
        self.table = QTableWidget(0, 6); self.table.setHorizontalHeaderLabels(["使用", "文件", "时长", "格式", "质检", "准确转写"]); self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch); self.table.cellDoubleClicked.connect(self._preview); layout.addWidget(self.table)
        hint = QLabel("零样本参考建议选择 5–10 秒干净单人片段。MP3、立体声和疑似背景声必须试听确认；重复文件会自动排除。"); hint.setWordWrap(True); hint.setObjectName("hint"); layout.addWidget(hint)
        save = QPushButton("保存声音配置"); save.setObjectName("primaryButton"); save.clicked.connect(self._save); layout.addWidget(save, alignment=Qt.AlignLeft)
        layout.addWidget(QLabel("已有声音")); self.profiles_table = QTableWidget(0, 7); self.profiles_table.setHorizontalHeaderLabels(["名称", "原始素材", "已处理", "零样本参考", "已确认训练时长", "训练状态", "默认模型"]); self.profiles_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch); layout.addWidget(self.profiles_table)
        profile_actions = QHBoxLayout(); open_training = QPushButton("打开数据准备"); open_training.clicked.connect(self.open_training_requested); preview_ref = QPushButton("试听参考片段"); preview_ref.clicked.connect(self._preview_reference); confirm_consent = QPushButton("确认所选声音授权"); confirm_consent.clicked.connect(self._confirm_existing_consent); profile_actions.addWidget(open_training); profile_actions.addWidget(preview_ref); profile_actions.addWidget(confirm_consent); profile_actions.addStretch(); layout.addLayout(profile_actions)

    def _import_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择参考音频", str(Path.cwd()), "音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg)")
        if files: self._scan([Path(item) for item in files])

    def _import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择参考声音文件夹", str(Path.cwd() / "参考声音"))
        if folder: self._scan([Path(folder)])

    def _scan(self, paths: list[Path]) -> None:
        if self.scan_thread and self.scan_thread.isRunning(): return
        self.import_files.setEnabled(False); self.import_folder.setEnabled(False); self.table.setRowCount(1); self.table.setItem(0, 1, QTableWidgetItem("正在扫描、计算哈希并检查重复……"))
        self.scan_thread = AudioScanThread(paths, self); self.scan_thread.completed.connect(self._populate_scan); self.scan_thread.failed.connect(self._scan_failed); self.scan_thread.finished.connect(lambda: (self.import_files.setEnabled(True), self.import_folder.setEnabled(True))); self.scan_thread.start()

    def _scan_failed(self, message: str) -> None:
        self.table.setRowCount(0); _show_error(self, message)

    def _populate_scan(self, probes: list[AudioProbe]) -> None:
        self.probes = probes; self.table.setRowCount(len(self.probes))
        for row, probe in enumerate(self.probes):
            use = QTableWidgetItem(); use.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); use.setCheckState(Qt.Unchecked if probe.duplicate_of else Qt.Checked); self.table.setItem(row, 0, use)
            self.table.setItem(row, 1, QTableWidgetItem(Path(probe.path).name)); self.table.item(row, 1).setToolTip(probe.path)
            self.table.setItem(row, 2, QTableWidgetItem(f"{probe.duration_seconds:.2f}s")); self.table.setItem(row, 3, QTableWidgetItem(f"{probe.codec} / {probe.sample_rate or '?'}Hz / {probe.channels or '?'}ch"))
            flags = "、".join(probe.quality_flags) or "通过"; flag_item = QTableWidgetItem(flags); flag_item.setForeground(QColor("#b45309") if probe.quality_flags else QColor("#15803d")); self.table.setItem(row, 4, flag_item)
            self.table.setItem(row, 5, QTableWidgetItem(""))

    def _selected_probes(self) -> list[AudioProbe]:
        return [probe for row, probe in enumerate(self.probes) if self.table.item(row, 0) and self.table.item(row, 0).checkState() == Qt.Checked and not probe.duplicate_of]

    def _cleanup(self, action: str) -> None:
        selected = self._selected_probes()
        if not selected: _show_error(self, "请先导入并勾选要处理的音频"); return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S"); base = self.project / "processed" / f"{action}-{stamp}"; input_dir = base / "input"; output_dir = base / "output"
        for probe in selected: copy_original(Path(probe.path), input_dir, probe.sha256)
        try: request = self.client.send("prepare_dataset", {"action": action, "input_dir": str(input_dir), "output_dir": str(output_dir)})
        except Exception as exc: _show_error(self, str(exc)); return
        self.cleanup_requests[request] = action; self.denoise.setEnabled(False); self.uvr.setEnabled(False)

    def _on_worker_event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id not in self.cleanup_requests: return
        action = self.cleanup_requests[request_id]
        if event == "progress":
            self.denoise.setText(str(payload.get("message", "处理中…"))[:16])
        elif event == "result":
            self.cleanup_requests.pop(request_id, None); self.denoise.setText("生成降噪副本"); self.denoise.setEnabled(True); self.uvr.setEnabled(True)
            outputs = payload.get("outputs", []); folder = Path(outputs[0]) if outputs else None
            if folder and action == "uvr" and (folder / "vocal").exists(): folder = folder / "vocal"
            if folder and folder.exists(): self._scan([folder])
        elif event == "error":
            self.cleanup_requests.pop(request_id, None); self.denoise.setText("生成降噪副本"); self.denoise.setEnabled(True); self.uvr.setEnabled(True); _show_error(self, str(payload.get("message", "处理失败")))

    def _preview(self, row: int, _column: int) -> None:
        if 0 <= row < len(self.probes): QDesktopServices.openUrl(QUrl.fromLocalFile(self.probes[row].path))

    def _save(self) -> None:
        if not self.consent.isChecked(): _show_error(self, "必须确认声音授权后才能创建配置"); return
        refs: list[ReferenceAsset] = []; selected: list[tuple[AudioProbe, Path]] = []
        for row, probe in enumerate(self.probes):
            if self.table.item(row, 0).checkState() != Qt.Checked: continue
            copied = copy_original(Path(probe.path), self.project / "raw", probe.sha256)
            selected.append((probe, copied)); transcript = self.table.item(row, 5).text().strip()
            if transcript and 5 <= probe.duration_seconds <= 10 and not probe.quality_flags:
                refs.append(ReferenceAsset(str(copied), probe.sha256, transcript, "zh", probe.duration_seconds, True, list(probe.quality_flags)))
        if not selected: _show_error(self, "至少选择一个非重复素材"); return
        profile = VoiceProfile(self.name.text().strip() or "我的声音", True, reference_assets=refs, consent_record="用户在声音库创建配置时确认本人声音或已取得明确授权", consent_confirmed_at=utc_now())
        assets = [SourceAsset(profile.id, probe.path, str(copied), probe.sha256, duration_seconds=probe.duration_seconds, sample_rate=probe.sample_rate, channels=probe.channels, codec=probe.codec, duplicate_of=probe.duplicate_of, quality_flags=list(probe.quality_flags), enabled=not bool(probe.duplicate_of)) for probe, copied in selected]
        profile.source_asset_ids = [item.id for item in assets]; self.store.save_source_assets(self.project, assets)
        self.store.save_profile(self.project, profile); self.refresh_profiles(); self.profiles_changed.emit(); QMessageBox.information(self, "本地声音工坊", "声音配置已保存")

    def _confirm_existing_consent(self) -> None:
        row = self.profiles_table.currentRow()
        profiles = self.store.list_profiles(self.project)
        if row < 0 or row >= len(profiles):
            _show_error(self, "请先在已有声音列表中选择一个声音配置")
            return
        profile = profiles[row]
        profile.consent_confirmed = True
        profile.consent_record = "用户在声音库中确认该声音属于本人或已取得明确授权"
        profile.consent_confirmed_at = utc_now()
        self.store.save_profile(self.project, profile)
        self.refresh_profiles(); self.profiles_changed.emit()
        QMessageBox.information(self, "本地声音工坊", "授权确认已记录")

    def refresh_profiles(self) -> None:
        profiles = self.store.list_profiles(self.project); self.profiles_table.setRowCount(len(profiles))
        for row, item in enumerate(profiles):
            assets = self.store.list_source_assets(self.project, item.id)
            processed = sum(1 for asset in assets if asset.processing_status not in {"未处理", "待重新准备"}); confirmed = sum(asset.confirmed_seconds for asset in assets); reference = next((ref for ref in item.reference_assets if ref.approved and ref.transcript.strip()), None)
            values = [item.name, str(len(assets)), str(processed), Path(reference.path).name if reference else "未准备", f"{confirmed:.1f}s", item.status(assets), "微调模型" if item.default_model_mode == "fine_tuned" else "零样本"]
            for col, value in enumerate(values): self.profiles_table.setItem(row, col, QTableWidgetItem(value))

    def _preview_reference(self) -> None:
        row = self.profiles_table.currentRow(); profiles = self.store.list_profiles(self.project)
        if row < 0 or row >= len(profiles): _show_error(self, "请先选择一个声音配置"); return
        reference = next((item for item in profiles[row].reference_assets if item.approved and item.transcript.strip() and Path(item.path).is_file()), None)
        if not reference: _show_error(self, "该声音配置还没有已确认的 5–10 秒参考片段"); return
        QDesktopServices.openUrl(QUrl.fromLocalFile(reference.path))


class TrainingPage(QWidget):
    """Legacy compatibility page used only by older contract tests/tools.

    The formal desktop product uses ``simple_pages.OneClickTrainingPage``.
    Do not add or extend Singing Model product behavior here.
    """
    job_created = Signal(object)
    profiles_changed = Signal()
    PROMPTS = [
        "清晨的阳光穿过窗帘，房间里显得格外安静。",
        "今天的任务已经准备好了，我们现在就开始吧。",
        "Please check the new game build before Friday afternoon.",
        "数字一二三四五，日期二零二六年八月二日。",
        "风吹过树叶，远处传来轻轻的脚步声。",
    ]

    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.store, self.project, self.client = store, project, client; self.probes: list[AudioProbe] = []; self.source_assets: list[SourceAsset] = []; self.recorder = Recorder(self); self.review_output = QAudioOutput(self); self.review_player = QMediaPlayer(self); self.review_player.setAudioOutput(self.review_output); self.review_queue: list[str] = []; self.review_index = 0; self.prompt_index = 0; self.active_requests: dict[str, Job] = {}; self.asr_map: dict[str, int] = {}; self.latest_checkpoints: dict[str, str] = {}; self.ab_requests: dict[str, tuple[str, dict]] = {}; self.ab_outputs: list[str] = []; self._build(); self._wire(); self.refresh_profiles()

    def _build(self) -> None:
        layout = QVBoxLayout(self); title = QLabel("数据与训练"); title.setObjectName("pageTitle"); layout.addWidget(title)
        singing = QGroupBox("歌唱模型（RVC v2）")
        singing_form = QFormLayout(singing)
        self.singing_profile = QComboBox(); self.singing_profile.setObjectName("singingProfile"); self.singing_profile.currentIndexChanged.connect(self._update_singing_status)
        self.singing_status = QLabel("未生成"); self.singing_status.setObjectName("hint")
        self.train_singing = QPushButton("训练歌唱模型"); self.train_singing.setObjectName("primaryButton"); self.train_singing.clicked.connect(self._train_singing_model)
        singing_form.addRow("声音配置", self.singing_profile); singing_form.addRow("状态", self.singing_status); singing_form.addRow("操作", self.train_singing)
        note = QLabel("需要已确认授权的声音素材；RVC v2 + RMVPE 在独立运行环境中执行。训练完成后还需验证模型才能用于 AI 人声生成。")
        note.setWordWrap(True); note.setObjectName("hint"); singing_form.addRow("说明", note); layout.addWidget(singing)
        profile_row = QHBoxLayout(); self.training_profile = QComboBox(); self.training_profile.currentIndexChanged.connect(self._load_profile_assets); profile_row.addWidget(QLabel("声音配置")); profile_row.addWidget(self.training_profile, 1); layout.addLayout(profile_row)
        tabs = QTabWidget(); layout.addWidget(tabs)
        sources = QWidget(); sources_layout = QVBoxLayout(sources)
        self.source_table = QTableWidget(0, 9); self.source_table.setHorizontalHeaderLabels(["使用", "文件名", "时长", "声道/采样率", "重复", "质量问题", "处理状态", "片段数", "已确认时长"]); self.source_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); sources_layout.addWidget(self.source_table)
        source_actions = QHBoxLayout(); self.separate_vocals = QCheckBox("人声分离"); self.noise_reduce = QCheckBox("降噪"); self.prepare_sources = QPushButton("使用所选音频准备训练数据"); self.prepare_sources.setObjectName("primaryButton"); self.prepare_sources.clicked.connect(self._prepare_sources); clean_runs = QPushButton("清理旧准备运行"); clean_runs.clicked.connect(self._cleanup_preparation_runs); source_actions.addWidget(self.separate_vocals); source_actions.addWidget(self.noise_reduce); source_actions.addWidget(self.prepare_sources); source_actions.addWidget(clean_runs); source_actions.addStretch(); sources_layout.addLayout(source_actions)
        sources_layout.addWidget(QLabel("导入音频与录音使用同一数据链路；原始文件不会覆盖。完成 VAD 和 ASR 后请在下方校对表人工确认。")); tabs.addTab(sources, "使用声音库音频")
        record = QWidget(); record_layout = QVBoxLayout(record)
        self.microphone = QComboBox(); [self.microphone.addItem(device.description()) for device in Recorder.inputs()]
        self.prompt = QLabel(self.PROMPTS[0]); self.prompt.setWordWrap(True); self.prompt.setObjectName("recordPrompt")
        self.level = QProgressBar(); self.level.setRange(0, 100); self.level.setTextVisible(False)
        row = QHBoxLayout(); self.record_button = QPushButton("开始录音"); self.record_button.clicked.connect(self._toggle_record); next_prompt = QPushButton("下一句"); next_prompt.clicked.connect(self._next_prompt); row.addWidget(self.record_button); row.addWidget(next_prompt); row.addStretch()
        record_layout.addWidget(QLabel("麦克风")); record_layout.addWidget(self.microphone); record_layout.addWidget(QLabel("请自然朗读（补充训练素材）")); record_layout.addWidget(self.prompt); record_layout.addWidget(self.level); record_layout.addLayout(row); record_layout.addStretch(); tabs.addTab(record, "引导录音")

        dataset = QWidget(); dataset_layout = QVBoxLayout(dataset); actions = QHBoxLayout(); import_button = QPushButton("导入训练音频"); import_button.clicked.connect(self._import); self.slice = QPushButton("自动切分"); self.slice.clicked.connect(self._run_slice); self.asr = QPushButton("本地自动转写"); self.asr.clicked.connect(self._run_asr); self.freeze = QPushButton("冻结数据集"); self.freeze.clicked.connect(self._freeze); self.prepare = QPushButton("准备训练特征"); self.prepare.clicked.connect(self._prepare); self.train = QPushButton("开始微调"); self.train.clicked.connect(self._train); self.cancel = QPushButton("取消任务"); self.cancel.clicked.connect(lambda: self.client.send("cancel")); actions.addWidget(import_button); actions.addWidget(self.slice); actions.addWidget(self.asr); actions.addWidget(self.freeze); actions.addWidget(self.prepare); actions.addWidget(self.train); actions.addWidget(self.cancel); actions.addStretch(); dataset_layout.addLayout(actions)
        edit_actions = QHBoxLayout(); play_all = QPushButton("连续播放"); play_all.clicked.connect(self._play_all); confirm_all = QPushButton("批量确认"); confirm_all.clicked.connect(self._confirm_all); exclude_bad = QPushButton("排除低质量片段"); exclude_bad.clicked.connect(self._exclude_bad); choose_ref = QPushButton("设为零样本参考"); choose_ref.clicked.connect(self._choose_reference); edit_actions.addWidget(play_all); edit_actions.addWidget(confirm_all); edit_actions.addWidget(exclude_bad); edit_actions.addWidget(choose_ref); edit_actions.addStretch(); dataset_layout.addLayout(edit_actions)
        self.dataset_table = QTableWidget(0, 10); self.dataset_table.setHorizontalHeaderLabels(["播放", "纳入", "人工确认", "起止时间", "音频片段", "ASR/校对文本", "语言", "置信度", "质量标记", "时长"]); self.dataset_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch); self.dataset_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch); dataset_layout.addWidget(self.dataset_table)
        self.duration = QLabel("已通过 0.0 秒；不足 60 秒不能训练，推荐 30–60 分钟。"); self.duration.setObjectName("hint"); dataset_layout.addWidget(self.duration); self.training_progress = QProgressBar(); dataset_layout.addWidget(self.training_progress); self.training_log = QPlainTextEdit(); self.training_log.setReadOnly(True); dataset_layout.addWidget(self.training_log); tabs.addTab(dataset, "数据集与微调")
        ab = QGroupBox("训练结果 A/B 验收"); ab_layout = QVBoxLayout(ab); ab_controls = QHBoxLayout(); self.ab_profile = QComboBox(); self.ab_profile.currentIndexChanged.connect(self._restore_candidate_state); self.ab_button = QPushButton("生成底模 / 微调后对比"); self.ab_button.setEnabled(False); self.ab_button.clicked.connect(self._start_ab); self.play_base = QPushButton("播放零样本"); self.play_base.setEnabled(False); self.play_base.clicked.connect(lambda: self._play_ab(False)); self.play_tuned = QPushButton("播放微调后"); self.play_tuned.setEnabled(False); self.play_tuned.clicked.connect(lambda: self._play_ab(True)); self.promote_button = QPushButton("确认并设为默认声音"); self.promote_button.setEnabled(False); self.promote_button.clicked.connect(self._promote); self.reject_button = QPushButton("拒绝候选"); self.reject_button.setEnabled(False); self.reject_button.clicked.connect(self._reject_candidate); ab_controls.addWidget(QLabel("声音配置")); ab_controls.addWidget(self.ab_profile); ab_controls.addWidget(self.ab_button); ab_controls.addWidget(self.play_base); ab_controls.addWidget(self.play_tuned); ab_controls.addWidget(self.promote_button); ab_controls.addWidget(self.reject_button); ab_controls.addStretch(); ab_layout.addLayout(ab_controls); self.candidate_info = QLabel("没有待验收候选模型"); self.candidate_info.setObjectName("hint"); ab_layout.addWidget(self.candidate_info); layout.addWidget(ab)

    def _wire(self) -> None:
        self.recorder.level_changed.connect(lambda value: self.level.setValue(round(value * 100))); self.recorder.stopped.connect(self._recorded); self.recorder.error.connect(lambda message: _show_error(self, message)); self.review_player.mediaStatusChanged.connect(self._review_status); self.client.event.connect(self._on_event); self.dataset_table.itemChanged.connect(lambda *_: self._update_duration())

    def refresh_profiles(self) -> None:
        selected = self.training_profile.currentData(); ab_selected = self.ab_profile.currentData(); profiles = self.store.list_profiles(self.project); self.ab_profile.blockSignals(True); self.ab_profile.clear(); self.training_profile.blockSignals(True); self.training_profile.clear()
        for profile in profiles:
            assets = self.store.list_source_assets(self.project, profile.id); label = f"{profile.name}（{profile.status(assets)}）"; self.ab_profile.addItem(label, profile.id); self.training_profile.addItem(label, profile.id)
        if selected:
            index = self.training_profile.findData(selected)
            if index >= 0: self.training_profile.setCurrentIndex(index)
        candidate_id = ab_selected or next((item.id for item in profiles if item.ab_status in {"awaiting_ab", "ab_generated"}), "")
        candidate_index = self.ab_profile.findData(candidate_id)
        if candidate_index >= 0: self.ab_profile.setCurrentIndex(candidate_index)
        self.ab_profile.blockSignals(False); self.training_profile.blockSignals(False); self._load_profile_assets()
        self.singing_profile.blockSignals(True); self.singing_profile.clear()
        for profile in profiles:
            status = profile.singing_status(self.project) if hasattr(profile, "singing_status") else "not_ready"
            self.singing_profile.addItem(profile.name, profile.id)
            if not profile.consent_confirmed: self.singing_profile.model().item(self.singing_profile.count() - 1).setEnabled(False)
        self.singing_profile.blockSignals(False); self._update_singing_status()
        self._restore_candidate_state()

    def _update_singing_status(self):
        identifier = self.singing_profile.currentData(); profile = next((p for p in self.store.list_profiles(self.project) if p.id == identifier), None)
        status = profile.singing_status(self.project) if profile and hasattr(profile, "singing_status") else "not_ready"
        labels = {"ready": "就绪", "training": "训练中", "untrusted": "未验证", "model_missing": "模型缺失", "not_ready": "未生成"}
        self.singing_status.setText(labels.get(status, status)); self.train_singing.setEnabled(bool(profile and profile.consent_confirmed and status != "training"))

    def _train_singing_model(self):
        profile_id = self.singing_profile.currentData()
        profile = next((p for p in self.store.list_profiles(self.project) if p.id == profile_id), None)
        if not profile or not profile.consent_confirmed:
            _show_error(self, "未确认声音授权，不能训练歌唱模型"); return
        assets = self.store.list_source_assets(self.project, profile.id)
        selected = [a.to_dict() for a in assets if a.enabled and not a.duplicate_of]
        if not selected: _show_error(self, "请先准备并确认声音素材"); return
        payload = {"project_path": str(self.project), "profile_id": profile.id, "source_assets": selected, "engine": "rvc_v2", "f0_method": "rmvpe", "training_run_id": uuid4().hex, "dataset_snapshot_id": profile.dataset_snapshot_id, "snapshot_sha256": ""}
        try:
            request = self.client.send("train_singing_model", payload)
            profile.training_state = "training_singing_model"; self.store.save_profile(self.project, profile); self.singing_status.setText("训练中"); self.train_singing.setEnabled(False)
            self.active_requests[request] = Job(JobKind.TRAIN, payload)
        except Exception as exc: _show_error(self, str(exc))

    def _load_profile_assets(self) -> None:
        profile_id = self.training_profile.currentData(); self.source_assets = self.store.list_source_assets(self.project, profile_id) if profile_id else []; self.source_table.setRowCount(len(self.source_assets))
        for row, asset in enumerate(self.source_assets):
            use = QTableWidgetItem(); use.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); use.setCheckState(Qt.Checked if asset.enabled and not asset.duplicate_of else Qt.Unchecked); self.source_table.setItem(row, 0, use)
            values = [Path(asset.project_path or asset.original_path).name, f"{asset.duration_seconds:.2f}s", f"{asset.channels}ch / {asset.sample_rate}Hz", "是" if asset.duplicate_of else "否", "、".join(asset.quality_flags) or "通过", asset.processing_status, str(asset.segment_count), f"{asset.confirmed_seconds:.1f}s"]
            for col, value in enumerate(values, 1): self.source_table.setItem(row, col, QTableWidgetItem(value))

    def _prepare_sources(self) -> None:
        profile_id = self.training_profile.currentData(); selected = [asset.id for row, asset in enumerate(self.source_assets) if self.source_table.item(row, 0).checkState() == Qt.Checked and not asset.duplicate_of]
        if not profile_id or not selected: _show_error(self, "请选择声音配置和至少一个非重复声音库素材"); return
        preparation_id = uuid4().hex; payload = {"action": "pipeline", "preparation_id": preparation_id, "profile_id": profile_id, "source_asset_ids": selected, "source_assets": [item.to_dict() for item in self.source_assets], "project_path": str(self.project), "processing_options": {"language": "zh", "separate_vocals": self.separate_vocals.isChecked(), "denoise": self.noise_reduce.isChecked()}}
        job = Job(JobKind.PREPARE_DATASET, payload); self.store.save_job(job); self.job_created.emit(job)
        try: request = self.client.send("prepare_dataset", payload)
        except Exception as exc: _show_error(self, _friendly_error(str(exc))); return
        self.active_requests[request] = job; self.prepare_sources.setEnabled(False); self.training_log.appendPlainText(f"本次选择 {len(selected)} 个素材；运行 ID：{preparation_id}")
        profile = next(item for item in self.store.list_profiles(self.project) if item.id == profile_id); profile.training_state = "preparing"; self.store.save_profile(self.project, profile); self.profiles_changed.emit()

    def _cleanup_preparation_runs(self) -> None:
        profile_id = self.training_profile.currentData()
        if not profile_id: return
        if QMessageBox.question(self, "本地声音工坊", "删除未被当前成功结果、参考片段或冻结快照引用的旧准备运行？") != QMessageBox.Yes: return
        removed = self.store.cleanup_preparation_runs(self.project, profile_id); self.training_log.appendPlainText(f"已清理 {len(removed)} 个旧运行目录；受保护的数据未删除。")

    def _toggle_record(self) -> None:
        if self.recorder.source is None:
            path = self.project / "raw" / "recordings" / f"prompt-{self.prompt_index + 1:03d}.wav"; self.recorder.start(path, self.microphone.currentIndex()); self.record_button.setText("停止并保存")
        else:
            self.recorder.stop(); self.record_button.setText("开始录音")

    def _recorded(self, path: str, duration: float) -> None:
        probe = scan_audio_files([Path(path)])[0]; profile_id = self.training_profile.currentData()
        if not profile_id: _show_error(self, "请先创建并选择声音配置"); return
        asset = SourceAsset(profile_id, probe.path, probe.path, probe.sha256, source_kind="recording", duration_seconds=probe.duration_seconds, sample_rate=probe.sample_rate, channels=probe.channels, codec=probe.codec, quality_flags=list(probe.quality_flags))
        self.store.save_source_assets(self.project, [asset]); profile = next(item for item in self.store.list_profiles(self.project) if item.id == profile_id); profile.source_asset_ids.append(asset.id); self.store.save_profile(self.project, profile); self._load_profile_assets(); self._next_prompt()

    def _next_prompt(self) -> None:
        self.prompt_index = (self.prompt_index + 1) % len(self.PROMPTS); self.prompt.setText(self.PROMPTS[self.prompt_index])

    def _import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "导入训练音频", str(Path.cwd()), "音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg)")
        for probe in scan_audio_files([Path(item) for item in files]):
            if not probe.duplicate_of: self.probes.append(probe); self._append_probe(probe, "")

    def _append_probe(self, probe: AudioProbe, text: str) -> None:
        row = self.dataset_table.rowCount(); self.dataset_table.insertRow(row)
        play = QPushButton("▶"); play.clicked.connect(lambda _=False, p=probe.path: self._play_path(p)); self.dataset_table.setCellWidget(row, 0, play)
        included = QTableWidgetItem(); included.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); included.setCheckState(Qt.Checked); self.dataset_table.setItem(row, 1, included)
        confirmed = QTableWidgetItem(); confirmed.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); confirmed.setCheckState(Qt.Unchecked); self.dataset_table.setItem(row, 2, confirmed)
        self.dataset_table.setItem(row, 3, QTableWidgetItem(f"0.00–{probe.duration_seconds:.2f}")); self.dataset_table.setItem(row, 4, QTableWidgetItem(probe.path)); self.dataset_table.setItem(row, 5, QTableWidgetItem(text)); self.dataset_table.setItem(row, 6, QTableWidgetItem("zh")); self.dataset_table.setItem(row, 7, QTableWidgetItem("-")); self.dataset_table.setItem(row, 8, QTableWidgetItem("、".join(probe.quality_flags))); self.dataset_table.setItem(row, 9, QTableWidgetItem(f"{probe.duration_seconds:.2f}")); self._update_duration()

    def _approved_rows(self) -> list[dict]:
        rows = []
        for row in range(self.dataset_table.rowCount()):
            if any(self.dataset_table.item(row, col) is None for col in range(1, 10)): continue
            if self.dataset_table.item(row, 1).checkState() != Qt.Checked: continue
            rows.append({"path": self.dataset_table.item(row, 4).text(), "duration": float(self.dataset_table.item(row, 9).text()), "language": self.dataset_table.item(row, 6).text(), "text": self.dataset_table.item(row, 5).text().strip(), "human_confirmed": self.dataset_table.item(row, 2).checkState() == Qt.Checked, "quality_flags": [item for item in self.dataset_table.item(row, 8).text().split("、") if item]})
        return rows

    def _update_duration(self) -> None:
        seconds = sum(item["duration"] for item in self._approved_rows() if item["human_confirmed"] and item["text"] and not item["quality_flags"]); warning = "可以冻结并训练" if seconds >= 60 else "不足 60 秒不能训练"; recommendation = "；建议继续录到 30–60 分钟" if seconds < 600 else ""
        self.duration.setText(f"已通过 {seconds:.1f} 秒；{warning}{recommendation}。")
        profile_id = self.training_profile.currentData(); profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None) if profile_id else None
        self.freeze.setEnabled(seconds >= 60); self.train.setEnabled(seconds >= 60 and bool(profile and profile.dataset_snapshot_id))

    def _confirm_all(self) -> None:
        for row in range(self.dataset_table.rowCount()):
            if self.dataset_table.item(row, 5) and self.dataset_table.item(row, 5).text().strip() and not self.dataset_table.item(row, 8).text().strip(): self.dataset_table.item(row, 2).setCheckState(Qt.Checked)
        self._update_duration()

    def _exclude_bad(self) -> None:
        for row in range(self.dataset_table.rowCount()):
            if self.dataset_table.item(row, 8) and self.dataset_table.item(row, 8).text().strip(): self.dataset_table.item(row, 1).setCheckState(Qt.Unchecked)
        self._update_duration()

    def _choose_reference(self) -> None:
        row = self.dataset_table.currentRow()
        if row < 0: _show_error(self, "请先在校对表选择一个片段"); return
        duration = float(self.dataset_table.item(row, 9).text()); text = self.dataset_table.item(row, 5).text().strip(); path = Path(self.dataset_table.item(row, 4).text())
        if not 5 <= duration <= 10: _show_error(self, "零样本参考片段必须为 5–10 秒"); return
        if self.dataset_table.item(row, 2).checkState() != Qt.Checked or not text: _show_error(self, "请先试听、校对文本并勾选人工确认"); return
        if self.dataset_table.item(row, 8).text().strip(): _show_error(self, "该片段存在质量问题，不能作为参考"); return
        profile_id = self.training_profile.currentData(); profile = next(item for item in self.store.list_profiles(self.project) if item.id == profile_id); profile.reference_assets = [ReferenceAsset(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), text, self.dataset_table.item(row, 6).text(), duration, True, [])]; self.store.save_profile(self.project, profile); self.profiles_changed.emit(); QMessageBox.information(self, "本地声音工坊", "零样本参考片段已更新")

    def _play_all(self) -> None:
        self.review_queue = [item["path"] for item in self._approved_rows()]; self.review_index = 0
        if self.review_queue: self._play_path(self.review_queue[0], keep_queue=True)

    def _play_path(self, path: str, keep_queue: bool = False) -> None:
        if not keep_queue: self.review_queue = [path]; self.review_index = 0
        self.review_player.setSource(QUrl.fromLocalFile(path)); self.review_player.play()

    def _review_status(self, status) -> None:
        if status == QMediaPlayer.EndOfMedia and self.review_index + 1 < len(self.review_queue):
            self.review_index += 1; self._play_path(self.review_queue[self.review_index], keep_queue=True)

    def _freeze(self) -> None:
        rows = self._approved_rows()
        valid = [item for item in rows if item["human_confirmed"] and item["text"] and not item["quality_flags"]]
        if not valid: _show_error(self, "没有已人工确认且通过质量检查的片段"); return
        if sum(item["duration"] for item in valid) < 60: _show_error(self, "已人工确认且通过质量检查的片段不足 60 秒"); return
        profile_id = self.training_profile.currentData(); dataset = DatasetManifest(f"{profile_id}-snapshot", profile_id, frozen=True)
        dataset_dir = self.project / "datasets" / dataset.id; dataset_dir.mkdir(parents=True, exist_ok=True); wav_dir = dataset_dir / "audio"; wav_dir.mkdir(exist_ok=True)
        lines = []
        for index, item in enumerate(valid, 1):
            source = Path(item["path"]); copied = copy_original(source, wav_dir); copied_sha256 = sha256_file(copied); relative = copied.resolve().relative_to(self.project.resolve()).as_posix(); lines.append(f"{relative}|speaker|{item['language']}|{item['text']}"); dataset.segments.append(DatasetSegment(copied_sha256, str(copied), 0, item["duration"], item["language"], item["text"], item["text"], None, [], True, True, True, audio_relative_path=relative))
        list_path = dataset_dir / "dataset.list"; list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        dataset.list_path = str(list_path); dataset.wav_dir = str(wav_dir); dataset.list_relative_path = list_path.relative_to(self.project.resolve()).as_posix(); dataset.list_sha256 = sha256_file(list_path); dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset); self.store.save_dataset_snapshot(self.project, dataset)
        profile = next(item for item in self.store.list_profiles(self.project) if item.id == profile_id); profile.dataset_snapshot_id = dataset.id
        if not any(ref.approved and ref.transcript.strip() for ref in profile.reference_assets):
            candidate = next((segment for segment in dataset.segments if 5 <= segment.duration_seconds <= 10), None)
            if candidate: profile.reference_assets = [ReferenceAsset(candidate.audio_path, hashlib.sha256(Path(candidate.audio_path).read_bytes()).hexdigest(), candidate.text, candidate.language, candidate.duration_seconds, True, [])]
        self.store.save_profile(self.project, profile)
        assets = self.store.list_source_assets(self.project, profile_id); selected_assets = [item for item in assets if item.enabled and not item.duplicate_of]
        if selected_assets:
            share = dataset.approved_seconds / len(selected_assets)
            for asset in selected_assets: asset.confirmed_seconds = share
            self.store.save_source_assets(self.project, selected_assets); self._load_profile_assets()
        self.training_log.appendPlainText("数据集已冻结：" + str(dataset_dir)); self.profiles_changed.emit(); self._update_duration()

    def _run_slice(self) -> None:
        if not self.probes: _show_error(self, "请先录音或导入训练音频"); return
        base = self.project / "datasets" / "current"; input_dir = base / "slice-input"; output_dir = base / "slice-output"
        for probe in self.probes: copy_original(Path(probe.path), input_dir, probe.sha256)
        payload = {"action": "slice", "input_dir": str(input_dir), "output_dir": str(output_dir)}; job = Job(JobKind.PREPARE_DATASET, payload); self.store.save_job(job); self.job_created.emit(job)
        try: request = self.client.send("prepare_dataset", payload)
        except Exception as exc: _show_error(self, str(exc)); return
        self.active_requests[request] = job

    def _run_asr(self) -> None:
        if not self.probes: _show_error(self, "请先录音或导入训练音频"); return
        base = self.project / "datasets" / "current"; input_dir = base / "asr-input"; output_dir = base / "asr-output"; self.asr_map.clear()
        for row in range(self.dataset_table.rowCount()):
            source = Path(self.dataset_table.item(row, 4).text()); copied = copy_original(source, input_dir); self.asr_map[copied.name] = row
        payload = {"action": "asr", "input_dir": str(input_dir), "output_dir": str(output_dir), "language": "zh"}; job = Job(JobKind.PREPARE_DATASET, payload); self.store.save_job(job); self.job_created.emit(job)
        try: request = self.client.send("prepare_dataset", payload)
        except Exception as exc: _show_error(self, str(exc)); return
        self.active_requests[request] = job

    def _apply_asr(self, list_path: Path) -> None:
        if not list_path.is_file(): self.training_log.appendPlainText("自动转写完成，但没有找到 .list 结果"); return
        for line in list_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4: continue
            name = Path(parts[0]).name; row = self.asr_map.get(name)
            if row is None: continue
            language, text, flags = _parse_asr_result(parts[3], parts[2] or "zh")
            self.dataset_table.item(row, 6).setText(language); self.dataset_table.item(row, 5).setText(text)
            existing = [item for item in self.dataset_table.item(row, 8).text().split("、") if item]
            self.dataset_table.item(row, 8).setText("、".join(dict.fromkeys(existing + flags)))
        self.training_log.appendPlainText("自动转写已填入表格，请逐句人工校对后再冻结。")

    def _dataset_payload(self) -> dict:
        profile_id = self.training_profile.currentData(); profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None)
        if not profile or not profile.dataset_snapshot_id: raise ValueError("请先冻结数据集")
        if not profile.consent_confirmed: raise ValueError("训练前必须先在声音库确认声音授权")
        path = self.project / "datasets" / profile.dataset_snapshot_id / "manifest.json"
        if not path.exists(): raise ValueError("冻结的数据集快照不存在")
        dataset = self.store.load_dataset_snapshot(self.project, profile.dataset_snapshot_id); return {**dataset.to_dict(), "approved_seconds": dataset.approved_seconds, "project_path": str(self.project), "profile_id": profile_id, "dataset_snapshot_id": profile.dataset_snapshot_id, "consent_confirmed": profile.consent_confirmed, "consent_record": profile.consent_record, "experiment_name": f"{self.project.name}-{profile_id[:8]}-{dataset.snapshot_sha256[:12]}", "checkpoint_dir": str(self.project / "checkpoints" / profile_id)}

    def _prepare(self) -> None:
        try: payload = self._dataset_payload()
        except ValueError as exc: _show_error(self, str(exc)); return
        job = Job(JobKind.PREPARE_DATASET, payload); self.store.save_job(job); self.job_created.emit(job); request = self.client.send("prepare_dataset", payload); self.active_requests[request] = job

    def _train(self) -> None:
        try: payload = self._dataset_payload()
        except ValueError as exc: _show_error(self, str(exc)); return
        if float(payload.get("approved_seconds", 0)) < 60: _show_error(self, "已通过音频不足 60 秒"); return
        payload.update({"training_run_id": uuid4().hex, "training_mode": "new"})
        job = Job(JobKind.TRAIN, payload); self.store.save_job(job); self.job_created.emit(job); request = self.client.send("train", payload); self.active_requests[request] = job; profile = next(item for item in self.store.list_profiles(self.project) if item.id == payload["profile_id"]); profile.training_state = "training"; self.store.save_profile(self.project, profile); self.profiles_changed.emit()

    def _on_event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id in self.ab_requests:
            self._on_ab_event(request_id, event, payload); return
        if request_id not in self.active_requests: return
        job = self.active_requests[request_id]
        if event == "progress":
            job.status = JobStatus.RUNNING; job.progress = float(payload.get("progress", 0)); job.message = str(payload.get("message", "")); self.training_progress.setValue(round(job.progress * 100)); self.training_log.appendPlainText(job.message); self.store.save_job(job)
        elif event == "result":
            job.status = JobStatus.COMPLETED; job.progress = 1; job.outputs = list(payload.get("outputs", [])); self.store.save_job(job); self.active_requests.pop(request_id); self.training_progress.setValue(100); self.training_log.appendPlainText("完成：" + "，".join(job.outputs))
            if job.payload.get("action") == "asr" and job.outputs: self._apply_asr(Path(job.outputs[0]))
            if job.payload.get("action") == "pipeline" and job.outputs:
                self._load_preparation(Path(job.outputs[0])); self.prepare_sources.setEnabled(True); profile = next(item for item in self.store.list_profiles(self.project) if item.id == job.payload["profile_id"]); profile.training_state = ""; self.store.save_profile(self.project, profile); self.profiles_changed.emit()
            if job.payload.get("action") == "slice" and job.outputs:
                sliced = scan_audio_files([Path(job.outputs[0])]); self.probes = [item for item in sliced if not item.duplicate_of]; self.dataset_table.setRowCount(0)
                for item in self.probes: self._append_probe(item, "")
                self.training_log.appendPlainText("切分结果已替换表格，请执行自动转写并逐句校对。")
            if job.kind == JobKind.TRAIN:
                profile = next(item for item in self.store.list_profiles(self.project) if item.id == job.payload["profile_id"]); profile.training_state = ""; self.store.save_profile(self.project, profile); self.profiles_changed.emit()
                checkpoint_result = dict(payload.get("checkpoints") or {}); gpt = str(checkpoint_result.get("gpt") or next((item for item in job.outputs if item.lower().endswith(".ckpt")), "")); sovits = str(checkpoint_result.get("sovits") or next((item for item in job.outputs if item.lower().endswith(".pth")), ""))
                if gpt and sovits:
                    self.latest_checkpoints = {"gpt": gpt, "sovits": sovits}; profile.candidate_gpt_checkpoint = gpt; profile.candidate_sovits_checkpoint = sovits; profile.candidate_training_run_id = job.payload["training_run_id"]; profile.candidate_dataset_snapshot_id = job.payload["dataset_snapshot_id"]; profile.candidate_snapshot_sha256 = job.payload["snapshot_sha256"]; profile.candidate_created_at = utc_now(); profile.ab_status = "awaiting_ab"; profile.ab_base_outputs = []; profile.ab_tuned_outputs = []; self.store.save_profile(self.project, profile); self.ab_button.setEnabled(True); self.reject_button.setEnabled(True); self.training_log.appendPlainText("训练检查点已持久化，请先生成 A/B 对比，确认后再设为默认。")
        elif event == "error":
            job.status = JobStatus.CANCELLED if payload.get("status") == "cancelled" else JobStatus.FAILED; job.error = str(payload.get("message", "")); self.store.save_job(job); self.active_requests.pop(request_id); self.prepare_sources.setEnabled(True)
            profile_id = job.payload.get("profile_id")
            if profile_id:
                profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None)
                if profile: profile.training_state = ""; self.store.save_profile(self.project, profile); self.profiles_changed.emit()
            self.training_log.appendPlainText("失败：" + job.error); _show_error(self, _friendly_error(job.error))

    def _load_preparation(self, path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8")); list_path = Path(value["asr_list"]); self.probes = [item for item in scan_audio_files([Path(value["segments_dir"])]) if not item.duplicate_of]; texts = {}
        for line in list_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4: texts[Path(parts[0]).name] = _parse_asr_result(parts[3], parts[2] or "zh")
        self.dataset_table.setRowCount(0)
        for probe in self.probes:
            language, text, flags = texts.get(Path(probe.path).name, ("zh", "", [])); self._append_probe(probe, text); row = self.dataset_table.rowCount() - 1; self.dataset_table.item(row, 6).setText(language)
            existing = [item for item in self.dataset_table.item(row, 8).text().split("、") if item]
            self.dataset_table.item(row, 8).setText("、".join(dict.fromkeys(existing + flags)))
        assets = self.store.list_source_assets(self.project, value["profile_id"]); selected = set(value.get("source_asset_ids", []))
        for asset in assets:
            if asset.id in selected: asset.processing_status = "已切片并转写"; asset.segment_count = len(self.probes)
        self.store.save_source_assets(self.project, assets); self._load_profile_assets()
        profile = next(item for item in self.store.list_profiles(self.project) if item.id == value["profile_id"]); profile.current_preparation_id = value.get("preparation_id", ""); profile.current_preparation_manifest = str(path); self.store.save_profile(self.project, profile); self.profiles_changed.emit()
        self.training_log.appendPlainText(f"已生成 {len(self.probes)} 个片段并填入 ASR 文本；当前成功运行：{profile.current_preparation_id}。请逐条试听、修改和人工确认。")

    def _selected_ab_profile(self) -> VoiceProfile | None:
        profile_id = self.ab_profile.currentData()
        return next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None)

    def _restore_candidate_state(self) -> None:
        profile = self._selected_ab_profile() if hasattr(self, "ab_profile") else None
        self.latest_checkpoints = {}; self.ab_outputs = []; available = bool(profile and profile.candidate_gpt_checkpoint and profile.candidate_sovits_checkpoint and Path(profile.candidate_gpt_checkpoint).is_file() and Path(profile.candidate_sovits_checkpoint).is_file())
        if available:
            self.latest_checkpoints = {"gpt": profile.candidate_gpt_checkpoint, "sovits": profile.candidate_sovits_checkpoint}; self.ab_outputs = list(profile.ab_base_outputs) + list(profile.ab_tuned_outputs)
        self.ab_button.setEnabled(available and profile.ab_status in {"awaiting_ab", "ab_generated"}); self.reject_button.setEnabled(available and profile.ab_status in {"awaiting_ab", "ab_generated"}); generated = available and profile.ab_status == "ab_generated" and all(Path(item).is_file() for item in self.ab_outputs)
        self.play_base.setEnabled(generated); self.play_tuned.setEnabled(generated); self.promote_button.setEnabled(generated)
        if available: self.candidate_info.setText(f"候选运行：{profile.candidate_training_run_id}；快照：{profile.candidate_dataset_snapshot_id} / {profile.candidate_snapshot_sha256[:12]}；状态：{profile.ab_status}")
        else: self.candidate_info.setText("候选模型不可用：检查点文件不存在。" if profile and (profile.candidate_gpt_checkpoint or profile.candidate_sovits_checkpoint) else "没有待验收候选模型")

    def _start_ab(self) -> None:
        profile = self._selected_ab_profile()
        if not profile or not self.latest_checkpoints: _show_error(self, "请选择声音配置并先完成训练"); return
        refs = [item for item in profile.reference_assets if item.approved and item.transcript.strip()]
        if not refs: _show_error(self, "声音配置缺少已校对参考片段"); return
        base_profile = profile.to_dict(); base_profile["active_gpt_checkpoint"] = ""; base_profile["active_sovits_checkpoint"] = ""
        tuned_profile = profile.to_dict(); tuned_profile["active_gpt_checkpoint"] = self.latest_checkpoints["gpt"]; tuned_profile["active_sovits_checkpoint"] = self.latest_checkpoints["sovits"]
        context = {"base": base_profile, "tuned": tuned_profile, "ref": refs[0].__dict__, "text": "清晨的风吹过树梢，新的冒险即将开始。Please check the latest game build.", "root": str(self.project / "exports" / "ab-comparison")}
        request = self.client.send("load_profile", base_profile); self.ab_requests[request] = ("load_base", context); self.ab_button.setEnabled(False); self.promote_button.setEnabled(False); self.ab_outputs.clear(); self.training_log.appendPlainText("正在生成底模 A/B 样本……")

    def _on_ab_event(self, request_id: str, event: str, payload: dict) -> None:
        stage, context = self.ab_requests.pop(request_id)
        if event == "error": self.ab_button.setEnabled(True); _show_error(self, str(payload.get("message", "A/B 生成失败"))); return
        if event == "progress": self.ab_requests[request_id] = (stage, context); return
        if event != "result": self.ab_requests[request_id] = (stage, context); return
        ref = context["ref"]
        synthesis = {"text": context["text"], "text_lang": "zh", "ref_audio_path": ref["path"], "prompt_text": ref["transcript"], "prompt_lang": ref.get("language", "zh"), "speed_factor": 1.0, "fragment_interval": 0.3, "output_dir": context["root"]}
        if stage == "load_base":
            request = self.client.send("synthesize", synthesis); self.ab_requests[request] = ("synth_base", context)
        elif stage == "synth_base":
            base_outputs = list(payload.get("outputs", [])); self.ab_outputs.extend(base_outputs); profile = self._selected_ab_profile(); profile.ab_base_outputs = base_outputs; self.store.save_profile(self.project, profile); request = self.client.send("load_profile", context["tuned"]); self.ab_requests[request] = ("load_tuned", context); self.training_log.appendPlainText("底模样本完成，正在生成微调后样本……")
        elif stage == "load_tuned":
            tuned_payload = {**synthesis, "output_dir": str(Path(context["root"]) / "tuned")}; request = self.client.send("synthesize", tuned_payload); self.ab_requests[request] = ("synth_tuned", context)
        elif stage == "synth_tuned":
            tuned_outputs = list(payload.get("outputs", [])); self.ab_outputs.extend(tuned_outputs); profile = self._selected_ab_profile(); profile.ab_tuned_outputs = tuned_outputs; profile.ab_status = "ab_generated"; self.store.save_profile(self.project, profile); self.play_base.setEnabled(True); self.play_tuned.setEnabled(True); self.promote_button.setEnabled(True); self.ab_button.setEnabled(True); self.reject_button.setEnabled(True); self.training_log.appendPlainText("A/B 样本已完成：\n" + "\n".join(self.ab_outputs))

    def _play_ab(self, tuned: bool) -> None:
        wavs = [item for item in self.ab_outputs if item.lower().endswith(".wav")]
        if len(wavs) < 2: _show_error(self, "A/B WAV 文件尚未生成完整"); return
        self._play_path(wavs[-1] if tuned else wavs[0])

    def _promote(self) -> None:
        profile = self._selected_ab_profile()
        if not profile or not self.latest_checkpoints or not self.ab_outputs: _show_error(self, "请先完成 A/B 对比"); return
        profile.active_gpt_checkpoint = self.latest_checkpoints["gpt"]; profile.active_sovits_checkpoint = self.latest_checkpoints["sovits"]; profile.default_model_mode = "fine_tuned"; profile.ab_status = "accepted"; self.store.save_profile(self.project, profile); self.promote_button.setEnabled(False); self.reject_button.setEnabled(False); self.profiles_changed.emit(); QMessageBox.information(self, "本地声音工坊", "微调模型已设为该声音的默认检查点")

    def _reject_candidate(self) -> None:
        profile = self._selected_ab_profile()
        if not profile: return
        profile.ab_status = "rejected"; self.store.save_profile(self.project, profile); self.ab_button.setEnabled(False); self.promote_button.setEnabled(False); self.reject_button.setEnabled(False); self.training_log.appendPlainText("候选模型已拒绝；训练文件仍保留，默认模型未改变。")


class HistoryPage(QWidget):
    def __init__(self, store: StudioStore):
        super().__init__(); self.store = store; layout = QVBoxLayout(self); title = QLabel("任务历史"); title.setObjectName("pageTitle"); layout.addWidget(title); refresh = QPushButton("刷新"); refresh.clicked.connect(self.refresh); layout.addWidget(refresh, alignment=Qt.AlignLeft); self.table = QTableWidget(0, 6); self.table.setHorizontalHeaderLabels(["时间", "类型", "状态", "进度", "消息", "输出"]); self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch); self.table.cellDoubleClicked.connect(self._open_output); layout.addWidget(self.table); self.refresh()

    def refresh(self) -> None:
        jobs = self.store.list_jobs(); self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            values = [job.updated_at[:19].replace("T", " "), job.kind.value, job.status.value, f"{job.progress*100:.0f}%", job.error or job.message, job.outputs[0] if job.outputs else ""]
            for col, value in enumerate(values): self.table.setItem(row, col, QTableWidgetItem(value))

    def _open_output(self, row: int, _column: int) -> None:
        value = self.table.item(row, 5).text()
        if value: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(value).parent)))


class SettingsPage(QWidget):
    install_requested = Signal()

    def __init__(self, paths: AppPaths, client: WorkerClient):
        super().__init__(); self.paths, self.client = paths, client; self.health_request = ""; layout = QVBoxLayout(self); title = QLabel("设置"); title.setObjectName("pageTitle"); layout.addWidget(title)
        form = QFormLayout(); form.addRow("数据目录", QLabel(str(paths.data_root))); form.addRow("项目目录", QLabel(str(paths.projects_root))); form.addRow("私有 Python", QLabel(str(paths.private_python))); form.addRow("GPT-SoVITS", QLabel(str(paths.engine_root))); layout.addLayout(form)
        buttons = QHBoxLayout(); install = QPushButton("安装/修复本地引擎"); install.setObjectName("primaryButton"); install.clicked.connect(self.install_requested); health = QPushButton("重新检测"); health.clicked.connect(self.check_health); details = QPushButton("复制详细诊断信息"); details.clicked.connect(self._copy_details); buttons.addWidget(install); buttons.addWidget(health); buttons.addWidget(details); buttons.addStretch(); layout.addLayout(buttons); self.raw_health = {}
        self.report = QTextBrowser(); layout.addWidget(self.report); client.event.connect(self._on_event); self.check_health()

    def check_health(self) -> None:
        try: self.health_request = self.client.send("health"); self.report.setPlainText("正在检测本地引擎与显卡……")
        except Exception as exc: self.report.setPlainText(str(exc))

    def _on_event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id != self.health_request: return
        if event == "result":
            self.raw_health = payload; engine = payload.get("engine", {}); lines = [f"兼容状态：{'可以使用' if payload.get('compatible') else '尚未就绪'}", f"私有工作进程：{payload.get('python_executable') or payload.get('worker_python')}", f"Python：{payload.get('python_version') or '-'}", f"PyTorch：{payload.get('torch_version') or '未安装'} / CUDA {payload.get('cuda_version') or '-'}", f"显卡：{payload.get('gpu_name') or '不可用'}", f"计算能力：{payload.get('compute_capability') or '-'}", f"CUDA 张量测试：{'通过' if payload.get('tensor_test_passed') else '未通过'}", f"GPT-SoVITS 导入：{'通过' if payload.get('gpt_sovits_imported') else '未通过'}", f"模型文件：{'完整' if payload.get('models_ready') else '不完整'}", f"FFmpeg：{'可用' if payload.get('ffmpeg_ready') else '不可用'}"]
            errors = payload.get("actionable_errors") or []
            if errors: lines.append("\n需要处理：\n" + "\n".join(f"• {item}" for item in errors))
            if engine.get("missing"): lines.append("\n缺少文件数量：" + str(len(engine["missing"])))
            self.report.setPlainText("\n".join(lines))
        elif event == "error": self.raw_health = payload; self.report.setPlainText(_friendly_error(str(payload.get("message", "检测失败"))))

    def _copy_details(self) -> None:
        QApplication.clipboard().setText(json.dumps(self.raw_health, ensure_ascii=False, indent=2))
