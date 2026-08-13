from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..audio import AudioProbe, copy_original, scan_audio_files
from ..models import DatasetDraft, Job, JobKind, JobStatus, SourceAsset, TrainingWorkflow, VoiceProfile, WorkflowStage, WorkflowStatus, utc_now
from ..paths import AppPaths
from ..storage import StudioStore
from ..workflow import TrainingWorkflowController
from .recording import Recorder
from .worker_client import WorkerClient
from .widgets import (
    GenerationRecord, GenerationRecordCard, InlineAudioPlayer, ReviewSegmentCard,
    StepTimeline, estimate_text_work, format_timestamp,
)


def show_error(parent: QWidget, message: str) -> None:
    QMessageBox.critical(parent, "VoiceStudio", message)


def fold_group(group: QGroupBox) -> None:
    """Make a checkable QGroupBox truly collapse instead of merely disabling its fields."""
    widgets = group.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
    def apply(opened: bool) -> None:
        for widget in widgets: widget.setVisible(opened)
        group.setMaximumHeight(16777215 if opened else 36)
    group.toggled.connect(apply); apply(group.isChecked())


class ScanThread(QThread):
    completed = Signal(object); failed = Signal(str)

    def __init__(self, paths: list[Path], parent=None): super().__init__(parent); self.paths = paths
    def run(self) -> None:
        try: self.completed.emit(scan_audio_files(self.paths))
        except Exception as exc: self.failed.emit(str(exc))


class DropArea(QFrame):
    paths_dropped = Signal(object)

    def __init__(self):
        super().__init__(); self.setObjectName("dropArea"); self.setAcceptDrops(True); self.setMinimumHeight(108)
        layout = QVBoxLayout(self); title = QLabel("把一批音频或文件夹拖到这里"); title.setObjectName("dropTitle"); title.setAlignment(Qt.AlignCenter); tip = QLabel("支持 WAV、FLAC、MP3；会递归扫描并自动排除重复文件"); tip.setObjectName("hint"); tip.setAlignment(Qt.AlignCenter); layout.addStretch(); layout.addWidget(title); layout.addWidget(tip); layout.addStretch()
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls(): event.acceptProposedAction()
    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(item.toLocalFile()) for item in event.mimeData().urls() if item.isLocalFile()]
        if paths: self.paths_dropped.emit(paths); event.acceptProposedAction()


class RecordingDialog(QDialog):
    PROMPTS = (
        "清晨的阳光穿过窗帘，房间里显得格外安静。", "今天的任务已经准备好了，我们现在就开始吧。",
        "风吹过树叶，远处传来轻轻的脚步声。", "窗外的雨渐渐停了，城市重新恢复了活力。",
        "请用自然的语气，读出每一个清晰而完整的句子。", "从一到十慢慢数数，然后说出今天的日期。",
        "春天有温暖的风，夏天有明亮的阳光。", "这个角色看起来平静，心里却藏着一点惊喜。",
        "Please check the latest game build and tell me what you think.", "我们的声音模型会在本地完成训练，不会上传录音。",
    )
    TARGET_SECONDS = 60.0

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent); self.setWindowTitle("引导式声音采集"); self.resize(720, 610); self.output_dir = output_dir; self.paths: list[Path] = []; self.durations: dict[str, float] = {}; self.index = 0; self.recorder = Recorder(self); self._committed = False
        layout = QVBoxLayout(self); layout.addWidget(QLabel("建立声音模型 · 自然朗读并累计录满 60 秒，录音只保存在本机。"))
        self.total = QProgressBar(); self.total.setRange(0, round(self.TARGET_SECONDS * 10)); self.total.setFormat("已录 %v / 600（约 60 秒）"); layout.addWidget(self.total)
        form = QFormLayout(); self.microphone = QComboBox(); [self.microphone.addItem(item.description()) for item in Recorder.inputs()]; form.addRow("麦克风", self.microphone); layout.addLayout(form)
        self.quality = QLabel("录制环境：等待检测 · 声音音量：等待检测 · 背景噪声：等待检测"); self.quality.setObjectName("hint"); self.quality.setWordWrap(True); layout.addWidget(self.quality)
        self.prompt = QLabel(self.PROMPTS[0]); self.prompt.setObjectName("recordPrompt"); self.prompt.setWordWrap(True); layout.addWidget(self.prompt)
        self.level = QProgressBar(); self.level.setRange(0, 100); self.level.setTextVisible(False); layout.addWidget(self.level)
        self.status = QLabel("尚未录音"); layout.addWidget(self.status)
        controls = QHBoxLayout(); self.record = QPushButton("● 开始录音"); self.record.setObjectName("primaryButton"); self.record.clicked.connect(self._toggle); next_button = QPushButton("换一句"); next_button.clicked.connect(self._next); controls.addWidget(self.record); controls.addWidget(next_button); controls.addStretch(); layout.addLayout(controls)
        self.recordings = QListWidget(); self.recordings.currentItemChanged.connect(self._selected_recording); layout.addWidget(self.recordings, 1)
        self.preview = InlineAudioPlayer(); layout.addWidget(self.preview)
        row = QHBoxLayout(); replay = QPushButton("试听"); replay.clicked.connect(self.preview.toggle); rerecord = QPushButton("重录所选"); rerecord.clicked.connect(self._rerecord); delete = QPushButton("删除所选"); delete.clicked.connect(self._delete_selected); self.finish_button = QPushButton("完成并导入"); self.finish_button.setObjectName("primaryButton"); self.finish_button.setEnabled(False); self.finish_button.clicked.connect(self.accept); row.addWidget(replay); row.addWidget(rerecord); row.addWidget(delete); row.addStretch(); row.addWidget(self.finish_button); layout.addLayout(row)
        self.recorder.level_changed.connect(lambda value: self.level.setValue(round(value * 100))); self.recorder.quality_changed.connect(self._quality); self.recorder.stopped.connect(self._saved); self.recorder.error.connect(lambda message: show_error(self, message))

    def _toggle(self) -> None:
        if self.recorder.source is None:
            self.output_dir.mkdir(parents=True, exist_ok=True); path = self.output_dir / f"recording-{utc_now().replace(':', '-')}-{len(self.paths) + 1}.wav"; self.recorder.start(path, self.microphone.currentIndex()); self.record.setText("■ 停止并保存")
        else: self.recorder.stop(); self.record.setText("● 开始录音")

    def _saved(self, path: str, duration: float) -> None:
        target = Path(path); self.paths.append(target); self.durations[str(target)] = duration
        item = QListWidgetItem(f"{target.name} · {duration:.1f} 秒 · 音量{self.recorder.last_quality.get('volume', '未知')}"); item.setData(Qt.UserRole, str(target)); self.recordings.addItem(item); self.recordings.setCurrentItem(item)
        current = sum(self.durations.values()); self.total.setValue(round(current * 10)); self.finish_button.setEnabled(current >= self.TARGET_SECONDS); self.status.setText(f"已录 {len(self.paths)} 段，共 {current:.1f} 秒" + ("，可以导入" if self.finish_button.isEnabled() else f"，还差 {self.TARGET_SECONDS - current:.1f} 秒")); self._next()
    def _quality(self, value: dict) -> None:
        clipping = " · 检测到削波，请离麦克风远一点" if value.get("clipping") else ""
        self.quality.setText(f"录制环境：{value.get('environment', '未知')} · 声音音量：{value.get('volume', '未知')} · 背景噪声：{value.get('noise', '未知')}{clipping}")
    def _selected_recording(self, current, _previous=None) -> None: self.preview.set_source(current.data(Qt.UserRole) if current else "")
    def _delete_selected(self) -> None:
        item = self.recordings.currentItem()
        if not item: show_error(self, "请先选择一段录音"); return
        path = Path(str(item.data(Qt.UserRole)))
        self.preview.release()
        try:
            path.resolve().relative_to(self.output_dir.resolve()); path.unlink(missing_ok=True)
        except (OSError, ValueError): pass
        self.paths = [value for value in self.paths if value != path]; self.durations.pop(str(path), None); self.recordings.takeItem(self.recordings.row(item)); self.preview.set_source("")
        current = sum(self.durations.values()); self.total.setValue(round(current * 10)); self.finish_button.setEnabled(current >= self.TARGET_SECONDS); self.status.setText(f"已录 {current:.1f} 秒，还差 {max(0, self.TARGET_SECONDS - current):.1f} 秒")
    def _rerecord(self) -> None:
        if not self.recordings.currentItem(): show_error(self, "请先选择要重录的片段"); return
        self._delete_selected(); self._toggle()
    def _next(self) -> None: self.index = (self.index + 1) % len(self.PROMPTS); self.prompt.setText(self.PROMPTS[self.index])
    def accept(self) -> None:
        if self.recorder.source is not None: self.recorder.stop(); self.record.setText("● 开始录音")
        if sum(self.durations.values()) < self.TARGET_SECONDS: show_error(self, f"还差 {self.TARGET_SECONDS - sum(self.durations.values()):.1f} 秒，录满 60 秒才能用于一键训练"); return
        self.preview.release(); self._committed = True; super().accept()
    def reject(self) -> None:
        if self.paths and not self._committed and QMessageBox.question(self, "放弃录音", "这些录音尚未导入。确定关闭并删除本次录音吗？") != QMessageBox.Yes: return
        if self.recorder.source is not None: self.recorder.stop()
        self.preview.release()
        if not self._committed:
            for path in self.paths:
                try: path.resolve().relative_to(self.output_dir.resolve()); path.unlink(missing_ok=True)
                except (OSError, ValueError): pass
        super().reject()


class MaterialManagerDialog(QDialog):
    def __init__(self, store: StudioStore, project: Path, profile_id: str = "", parent=None):
        super().__init__(parent); self.store, self.project, self.profile_id = store, project, profile_id
        self.setWindowTitle("管理已导入素材"); self.resize(820, 500)
        layout = QVBoxLayout(self)
        tip = QLabel("移除后不再用于后续训练，并删除项目内的导入副本；你原文件夹里的音频永远不会被删除。")
        tip.setObjectName("hint"); tip.setWordWrap(True); layout.addWidget(tip)
        self.table = QTableWidget(0, 5); self.table.setHorizontalHeaderLabels(["选择", "文件", "时长", "状态", "原始位置"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table)
        row = QHBoxLayout(); select_all = QPushButton("全选"); select_all.clicked.connect(self._select_all); remove = QPushButton("移除所选"); remove.setObjectName("primaryButton"); remove.clicked.connect(self._remove); close = QPushButton("关闭"); close.clicked.connect(self.accept)
        row.addWidget(select_all); row.addStretch(); row.addWidget(remove); row.addWidget(close); layout.addLayout(row)
        self._refresh()

    def _assets(self) -> list[SourceAsset]:
        return self.store.list_source_assets(self.project, self.profile_id or None)

    def _refresh(self) -> None:
        assets = self._assets(); self.table.setRowCount(len(assets))
        for row, asset in enumerate(assets):
            choice = QTableWidgetItem(); choice.setData(Qt.UserRole, asset.id); choice.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); choice.setCheckState(Qt.Unchecked); self.table.setItem(row, 0, choice)
            self.table.setItem(row, 1, QTableWidgetItem(Path(asset.original_path).name)); self.table.setItem(row, 2, QTableWidgetItem(f"{asset.duration_seconds:.1f} 秒")); self.table.setItem(row, 3, QTableWidgetItem(asset.processing_status)); self.table.setItem(row, 4, QTableWidgetItem(asset.original_path))

    def _select_all(self) -> None:
        for row in range(self.table.rowCount()): self.table.item(row, 0).setCheckState(Qt.Checked)

    def _remove(self) -> None:
        selected = {str(self.table.item(row, 0).data(Qt.UserRole)) for row in range(self.table.rowCount()) if self.table.item(row, 0).checkState() == Qt.Checked}
        if not selected: show_error(self, "请先勾选要移除的素材"); return
        blocking = [item for item in self.store.list_workflows(self.project) if item.status == WorkflowStatus.RUNNING and item.stage in {WorkflowStage.IMPORTING, WorkflowStage.PREPROCESSING} and selected.intersection(item.source_asset_ids)]
        if blocking: show_error(self, "这些素材正在清理或切片，请先取消当前处理任务再移除。"); return
        message = f"确定移除 {len(selected)} 个素材吗？\n\n项目内的导入副本会删除，但原始音频不会删除。"
        if any(item.status == WorkflowStatus.RUNNING and item.stage in {WorkflowStage.FEATURE_PREPARING, WorkflowStage.TRAINING, WorkflowStage.VERIFYING} for item in self.store.list_workflows(self.project)):
            message += "\n\n当前训练使用已冻结快照，会继续完成；移除从下一次训练起生效。"
        if QMessageBox.question(self, "移除训练素材", message) != QMessageBox.Yes: return
        self.store.remove_source_assets(self.project, selected, delete_project_copies=True); self._refresh()


class OneClickTrainingPage(QWidget):
    profiles_changed = Signal(); job_created = Signal(object)
    STEPS = ("检查素材", "清理切片", "识别文字", "确认数据", "训练模型", "验证并保存")

    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.store, self.project, self.client = store, project, client
        self.probes: list[AudioProbe] = []; self.scan_thread: ScanThread | None = None; self.workflow: TrainingWorkflow | None = None; self.draft: DatasetDraft | None = None; self._step_results: dict[int, str] = {}; self.review_indices: list[int] = []; self.review_position = 0
        self.controller = TrainingWorkflowController(store, project, client, self)
        self.controller.workflow_changed.connect(self._workflow_changed); self.controller.draft_ready.connect(self._show_draft); self.controller.profile_changed.connect(lambda _id: self._profiles_changed()); self.controller.job_created.connect(self.job_created)
        self._build(); self._restore()

    def _build(self) -> None:
        root = QVBoxLayout(self); title = QLabel("一键训练"); title.setObjectName("pageTitle"); root.addWidget(title); subtitle = QLabel("导入一批声音，VoiceStudio 会自动切片、识别、训练并验证。原文件永不覆盖。"); subtitle.setObjectName("hint"); root.addWidget(subtitle)
        card = QGroupBox("创建或追加声音"); form = QFormLayout(card); self.name = QLineEdit("我的声音"); self.name.setPlaceholderText("例如：我的旁白声"); self.consent = QCheckBox("我确认这是本人声音，或已经取得明确授权"); form.addRow("声音名称", self.name); form.addRow("授权确认", self.consent); self.drop = DropArea(); self.drop.paths_dropped.connect(self._scan); form.addRow(self.drop)
        row = QHBoxLayout(); files = QPushButton("选择音频"); files.clicked.connect(self._files); folder = QPushButton("选择文件夹"); folder.clicked.connect(self._folder); manage = QPushButton("管理已导入素材"); manage.clicked.connect(self._manage_assets); row.addWidget(files); row.addWidget(folder); row.addWidget(manage); row.addStretch(); form.addRow(row); root.addWidget(card)
        self.material = QLabel("尚未导入素材"); self.material.setObjectName("hint"); root.addWidget(self.material)
        self.timeline = StepTimeline(self.STEPS); self.steps = self.timeline.labels; root.addWidget(self.timeline)
        self.progress = QProgressBar(); root.addWidget(self.progress); self.status = QLabel("导入素材后即可开始"); self.status.setWordWrap(True); root.addWidget(self.status)
        self.review = QGroupBox("逐条试听并确认"); review_layout = QVBoxLayout(self.review); self.review_hint = QLabel("默认只审核异常片段；Space 播放，Enter 确认，Delete 排除，↑↓ 切换。"); self.review_hint.setObjectName("hint"); review_layout.addWidget(self.review_hint); self.review_card = ReviewSegmentCard(); self.review_card.changed.connect(self._review_changed); self.review_card.confirmed.connect(self._review_confirmed); self.review_card.navigate.connect(self._review_navigate); review_layout.addWidget(self.review_card)
        self.show_all = QCheckBox("查看全部片段表格"); self.show_all.toggled.connect(self._populate_review); review_layout.addWidget(self.show_all); self.table = QTableWidget(0, 5); self.table.setHorizontalHeaderLabels(["纳入", "片段", "时长", "识别文字", "问题"]); self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch); self.table.setVisible(False); review_layout.addWidget(self.table); self.review.setVisible(False); root.addWidget(self.review, 1)
        action_row = QHBoxLayout(); self.primary = QPushButton("导入素材"); self.primary.setObjectName("primaryButton"); self.primary.clicked.connect(self._primary_action); self.more = QPushButton("继续导入"); self.more.clicked.connect(self._files); self.record = QPushButton("录几句"); self.record.clicked.connect(self._record); self.cancel = QPushButton("取消当前任务"); self.cancel.clicked.connect(self._cancel); self.cancel.setVisible(False); action_row.addWidget(self.primary); action_row.addWidget(self.more); action_row.addWidget(self.record); action_row.addWidget(self.cancel); action_row.addStretch(); root.addLayout(action_row)
        advanced = QGroupBox("高级 / 查看详情"); advanced.setCheckable(True); advanced.setChecked(False); advanced_layout = QVBoxLayout(advanced); self.smart = QCheckBox("智能优化（人声分离与降噪）"); self.smart.setChecked(bool(self.store.get_setting("smart_optimization", True))); advanced_layout.addWidget(self.smart); self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(100); advanced_layout.addWidget(self.details); fold_group(advanced); root.addWidget(advanced)

    def _files(self) -> None:
        values, _ = QFileDialog.getOpenFileNames(self, "批量选择声音素材", str(Path.cwd()), "音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg)")
        if values: self._scan([Path(item) for item in values])
    def _folder(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择声音素材文件夹", str(Path.cwd() / "参考声音"))
        if value: self._scan([Path(value)])
    def _manage_assets(self) -> None:
        profile_id = self.workflow.voice_profile_id if self.workflow else ""
        if not profile_id:
            profile = next((item for item in self.store.list_profiles(self.project) if not item.archived and item.name == self.name.text().strip()), None)
            profile_id = profile.id if profile else ""
        dialog = MaterialManagerDialog(self.store, self.project, profile_id, self)
        dialog.exec()
        assets = self.store.list_source_assets(self.project, profile_id or None)
        if assets:
            self.material.setText(f"已导入 {len(assets)} 个素材，共 {sum(item.duration_seconds for item in assets):.1f} 秒。可随时管理或继续导入。")
        else:
            self.material.setText("尚未导入素材")
    def _record(self) -> None:
        dialog = RecordingDialog(self.project / "raw" / "recordings", self)
        if dialog.exec() == QDialog.Accepted and dialog.paths: self._scan(dialog.paths)
    def _scan(self, paths: list[Path]) -> None:
        if self.scan_thread and self.scan_thread.isRunning(): return
        self.material.setText("正在检查文件和计算重复项……"); self.primary.setEnabled(False); self.scan_thread = ScanThread(paths, self); self.scan_thread.completed.connect(self._scanned); self.scan_thread.failed.connect(lambda value: show_error(self, value)); self.scan_thread.finished.connect(lambda: self.primary.setEnabled(True)); self.scan_thread.start()
    def _scanned(self, probes: list[AudioProbe]) -> None:
        existing = {item.sha256 for item in self.store.list_source_assets(self.project)}; seen = {item.sha256 for item in self.probes}
        for probe in probes:
            if probe.sha256 in existing or probe.sha256 in seen: probe.duplicate_of = probe.duplicate_of or "项目中已有"; probe.quality_flags = list(dict.fromkeys([*probe.quality_flags, "duplicate"]))
            self.probes.append(probe); seen.add(probe.sha256)
        usable = [item for item in self.probes if not item.duplicate_of]; total = sum(item.duration_seconds for item in usable); duplicates = len(self.probes) - len(usable)
        self.material.setText(f"已找到 {len(self.probes)} 个文件，可用 {len(usable)} 个，共 {total:.1f} 秒；自动排除 {duplicates} 个重复文件。")
        self._set_step_result(0, f"{len(self.probes)} 个文件 · 排除 {duplicates} 个重复 · 有效 {total:.1f} 秒")
        self.primary.setText("开始自动处理"); self.status.setText("素材已就绪。点击一次，自动完成切片和文字识别。")

    def _primary_action(self) -> None:
        try:
            if self.workflow and self.workflow.stage == WorkflowStage.REVIEW_REQUIRED and self.draft:
                self._pull_review(); self.controller.confirm_and_train(self.workflow, self.draft); return
            if self.workflow and self.workflow.status in {WorkflowStatus.INTERRUPTED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}:
                self.controller.resume(self.workflow); return
            if not self.probes: self._files(); return
            if not self.consent.isChecked(): raise ValueError("请先勾选授权确认")
            usable = [item for item in self.probes if not item.duplicate_of]
            if not usable: raise ValueError("没有可处理的非重复素材")
            profile = next((item for item in self.store.list_profiles(self.project) if not item.archived and item.name == (self.name.text().strip() or "我的声音")), None)
            if profile is None: profile = VoiceProfile(self.name.text().strip() or "我的声音", True, consent_record="用户在一键训练页确认本人声音或已取得明确授权", consent_confirmed_at=utc_now())
            else:
                profile.consent_confirmed = True; profile.consent_record = "用户在一键训练页再次确认本人声音或已取得明确授权"; profile.consent_confirmed_at = utc_now()
            assets: list[SourceAsset] = []
            existing = {item.sha256: item for item in self.store.list_source_assets(self.project)}
            for probe in usable:
                if probe.sha256 in existing: continue
                copied = copy_original(Path(probe.path), self.project / "raw" / profile.id, probe.sha256)
                asset = SourceAsset(profile.id, probe.path, str(copied), probe.sha256, duration_seconds=probe.duration_seconds, sample_rate=probe.sample_rate, channels=probe.channels, codec=probe.codec, quality_flags=list(probe.quality_flags), enabled=True)
                assets.append(asset); profile.source_asset_ids.append(asset.id)
            if assets: self.store.save_source_assets(self.project, assets)
            self.store.save_profile(self.project, profile); self._profiles_changed()
            selected = [item.id for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
            self.workflow = self.controller.start(profile, selected, self.smart.isChecked())
        except Exception as exc: show_error(self, str(exc))

    def _workflow_changed(self, workflow: TrainingWorkflow) -> None:
        if self.workflow and workflow.id != self.workflow.id: return
        changed_workflow = not self.workflow or workflow.id != self.workflow.id; self.workflow = workflow
        if changed_workflow: self._load_step_results(workflow)
        self.progress.setValue(round(workflow.progress * 100)); self.status.setText(workflow.waiting_reason or workflow.error or workflow.message); self.details.appendPlainText(f"{workflow.stage.value}: {workflow.message or workflow.error}"); self.cancel.setVisible(workflow.status == WorkflowStatus.RUNNING)
        stage_index = {WorkflowStage.IMPORTING: 0, WorkflowStage.PREPROCESSING: 1, WorkflowStage.REVIEW_REQUIRED: 3, WorkflowStage.FREEZING: 4, WorkflowStage.FEATURE_PREPARING: 4, WorkflowStage.TRAINING: 4, WorkflowStage.VERIFYING: 5, WorkflowStage.SAVED: 6}.get(workflow.stage, 0)
        if workflow.stage == WorkflowStage.TRAINING: self._set_step_result(4, "正在训练，完成后会自动验证")
        elif workflow.stage == WorkflowStage.VERIFYING: self._set_step_result(4, "模型训练完成"); self._set_step_result(5, "正在生成固定台词试听")
        elif workflow.stage == WorkflowStage.SAVED: self._set_step_result(5, "验证通过 · 新版本已启用")
        self.timeline.update_state(stage_index, self._step_results, workflow.status == WorkflowStatus.FAILED)
        if workflow.stage == WorkflowStage.REVIEW_REQUIRED: self.primary.setText("确认并训练"); self.review.setVisible(True)
        elif workflow.status in {WorkflowStatus.INTERRUPTED, WorkflowStatus.CANCELLED}: self.primary.setText("继续上次任务")
        elif workflow.status == WorkflowStatus.FAILED: self.primary.setText("重试失败阶段")
        elif workflow.stage == WorkflowStage.SAVED: self.primary.setText("训练另一个声音"); self.status.setText("新声音已验证并启用，可以直接去“一键生成”使用。")
        else: self.primary.setText("正在自动处理…"); self.primary.setEnabled(workflow.status != WorkflowStatus.RUNNING)
        if workflow.status != WorkflowStatus.RUNNING: self.primary.setEnabled(True)

    def _show_draft(self, draft: DatasetDraft) -> None:
        if self.workflow and draft.workflow_id != self.workflow.id: return
        self.draft = draft; self.review.setVisible(True); self._populate_review()
        abnormal = sum(1 for item in draft.segments if item.quality_flags or not item.text.strip()); self._set_step_result(1, f"得到 {len(draft.segments)} 个短句片段"); self._set_step_result(2, f"识别 {len(draft.segments)} 个 · {abnormal} 个需确认"); self._set_step_result(3, f"可训练 {draft.eligible_seconds:.1f} 秒")
        self.timeline.update_state(3, self._step_results)
        self.review_hint.setText(f"可用合格素材 {draft.eligible_seconds:.1f} 秒。异常片段已默认排除；修改会即时保存。Space 播放，Enter 确认，Delete 排除。")
    def _populate_review(self) -> None:
        if not self.draft: return
        all_items = list(enumerate(self.draft.segments)); abnormal = [(i, x) for i, x in all_items if x.quality_flags or not x.text.strip()]; items = all_items if self.show_all.isChecked() else (abnormal or all_items)
        self.review_indices = [index for index, _item in items]; self.review_position = min(self.review_position, max(0, len(self.review_indices) - 1)); self.table.setVisible(self.show_all.isChecked())
        self.table.setRowCount(len(items))
        for row, (index, item) in enumerate(items):
            check = QTableWidgetItem(); check.setData(Qt.UserRole, index); check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); check.setCheckState(Qt.Checked if item.included else Qt.Unchecked); self.table.setItem(row, 0, check); self.table.setItem(row, 1, QTableWidgetItem(Path(item.audio_relative_path).name)); self.table.setItem(row, 2, QTableWidgetItem(f"{item.duration_seconds:.1f} 秒")); self.table.setItem(row, 3, QTableWidgetItem(item.text)); self.table.setItem(row, 4, QTableWidgetItem("、".join(item.quality_flags) or "通过"))
        self._show_review_position()
    def _show_review_position(self) -> None:
        if not self.draft or not self.review_indices: self.review_card.setVisible(False); return
        self.review_card.setVisible(True); index = self.review_indices[self.review_position]; self.review_card.set_segment(index, self.draft.segments[index], self.project, self.review_position, len(self.review_indices))
    def _review_changed(self, index: int, value: dict) -> None:
        if not self.draft: return
        item = self.draft.segments[index]; item.text = str(value.get("text", "")).strip(); item.included = bool(value.get("included")); self.store.save_draft(self.project, self.draft); self._set_step_result(3, f"已确认 {self.draft.confirmed_seconds:.1f} / {self.draft.eligible_seconds:.1f} 秒")
    def _review_confirmed(self, index: int) -> None:
        if not self.draft: return
        item = self.draft.segments[index]; item.human_confirmed = bool(item.included and item.text.strip() and not item.quality_flags); self.store.save_draft(self.project, self.draft); self._set_step_result(3, f"已确认 {self.draft.confirmed_seconds:.1f} / {self.draft.eligible_seconds:.1f} 秒")
    def _review_navigate(self, offset: int) -> None:
        if not self.review_indices: return
        self.review_position = (self.review_position + offset) % len(self.review_indices); self._show_review_position()
    def _pull_review(self) -> None:
        if not self.draft: return
        for row in range(self.table.rowCount()):
            index = int(self.table.item(row, 0).data(Qt.UserRole)); item = self.draft.segments[index]; item.included = self.table.item(row, 0).checkState() == Qt.Checked; item.text = self.table.item(row, 3).text().strip()
        self.store.save_draft(self.project, self.draft)
    def _cancel(self) -> None:
        if self.workflow: self.controller.cancel(self.workflow)
    def _restore(self) -> None:
        workflows = self.store.list_workflows(self.project)
        current = next((item for item in workflows if item.status not in {WorkflowStatus.COMPLETED}), None)
        if current:
            self.workflow = current; self.name.setText(current.voice_name); self._workflow_changed(current)
            if current.draft_id:
                try: self._show_draft(self.store.load_draft(self.project, current.draft_id))
                except (OSError, ValueError): pass
    def reset_for_profile(self, profile_id: str = "") -> None:
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None)
        self.review_card.release_resources(); self.workflow = None; self.draft = None; self.probes = []; self._step_results = {}; self.timeline.update_state(0, {}); self.review.setVisible(False); self.progress.setValue(0); self.name.setText(profile.name if profile else "我的声音"); self.consent.setChecked(bool(profile and profile.consent_confirmed)); self.primary.setText("导入素材"); self.primary.setEnabled(True)
    def _profiles_changed(self) -> None: self.profiles_changed.emit()
    def _step_key(self, workflow: TrainingWorkflow | None = None) -> str:
        current = workflow or self.workflow; return f"ui.workflow_step_results.{current.id}" if current else ""
    def _load_step_results(self, workflow: TrainingWorkflow) -> None:
        current = dict(self._step_results)
        stored = self.store.get_setting(self._step_key(workflow), {})
        optional = getattr(workflow, "step_results", {}) or {}
        merged = dict(stored) if isinstance(stored, dict) else {}
        if isinstance(optional, dict): merged.update(optional)
        merged.update(current)
        self._step_results = {int(key): str(value) for key, value in merged.items() if str(key).isdigit()}
    def _set_step_result(self, index: int, text: str) -> None:
        self._step_results[index] = text
        if self.workflow: self.store.set_setting(self._step_key(), {str(key): value for key, value in self._step_results.items()})

    def release_resources(self) -> None:
        self.review_card.release_resources()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources()
        super().closeEvent(event)


class VersionHistoryDialog(QDialog):
    changed = Signal()

    def __init__(self, store: StudioStore, project: Path, profile: VoiceProfile, parent=None):
        super().__init__(parent); self.store, self.project, self.profile = store, project, profile; self.setWindowTitle(f"{profile.name} · 版本历史与 A/B 对比"); self.resize(780, 620)
        layout = QVBoxLayout(self); layout.addWidget(QLabel("版本时间线"))
        self.timeline = QListWidget(); layout.addWidget(self.timeline)
        baseline = next((item.path for item in profile.reference_assets if item.approved and Path(item.path).is_file()), "")
        self.entries: list[tuple[str, str, str]] = [("快速克隆", "__baseline__", baseline)]
        for version in reversed(profile.model_versions):
            preview = next((item for item in version.preview_outputs if item.lower().endswith(".wav") and Path(item).is_file()), "")
            self.entries.append((version.name, version.id, preview))
            seconds = ""
            if version.dataset_snapshot_id:
                try: seconds = f" · {self.store.load_dataset_snapshot(project, version.dataset_snapshot_id).approved_seconds:.1f} 秒训练数据"
                except (OSError, ValueError): pass
            current = " · 当前" if version.id == profile.active_model_version_id else ""
            self.timeline.addItem(f"{'●' if current else '○'} {version.name}{current}\n   {format_timestamp(version.created_at)}{seconds}")
        self.timeline.insertItem(0, "○ 快速克隆\n   原始参考声音")
        compare = QGroupBox("同参数 A/B 试听"); grid = QGridLayout(compare); prompt = QLabel("固定验证台词：清晨的阳光穿过窗帘，今天的声音训练已经完成。"); prompt.setWordWrap(True); grid.addWidget(prompt, 0, 0, 1, 2)
        self.left = QComboBox(); self.right = QComboBox()
        for name, version_id, _path in self.entries: self.left.addItem(name, version_id); self.right.addItem(name, version_id)
        if self.right.count() > 1: self.right.setCurrentIndex(self.right.count() - 1)
        self.left_player = InlineAudioPlayer(); self.right_player = InlineAudioPlayer(); grid.addWidget(self.left, 1, 0); grid.addWidget(self.right, 1, 1); grid.addWidget(self.left_player, 2, 0); grid.addWidget(self.right_player, 2, 1)
        use_left = QPushButton("使用左侧版本"); use_left.clicked.connect(lambda: self._activate(self.left.currentData())); use_right = QPushButton("使用右侧版本"); use_right.setObjectName("primaryButton"); use_right.clicked.connect(lambda: self._activate(self.right.currentData())); grid.addWidget(use_left, 3, 0); grid.addWidget(use_right, 3, 1); layout.addWidget(compare)
        close = QPushButton("关闭"); close.clicked.connect(self.accept); layout.addWidget(close, alignment=Qt.AlignRight)
        self.left.currentIndexChanged.connect(self._refresh_players); self.right.currentIndexChanged.connect(self._refresh_players); self._refresh_players()

    def _path(self, version_id: str) -> str:
        return next((path for _name, item_id, path in self.entries if item_id == version_id), "")

    def _refresh_players(self) -> None:
        self.left_player.set_source(self._path(str(self.left.currentData()))); self.right_player.set_source(self._path(str(self.right.currentData())))

    def _activate(self, version_id: str) -> None:
        if version_id == "__baseline__": show_error(self, "快速克隆作为试听基线保留；当前版本可随时在时间线中恢复。"); return
        try: self.profile = self.store.activate_model_version(self.project, self.profile.id, version_id); self.changed.emit(); QMessageBox.information(self, "VoiceStudio", "声音版本已切换")
        except Exception as exc: show_error(self, str(exc))

    def done(self, result: int) -> None:
        self.left_player.release(); self.right_player.release(); super().done(result)


class VoiceCard(QFrame):
    generate_requested = Signal(str); retrain_requested = Signal(str); changed = Signal()
    def __init__(self, store: StudioStore, project: Path, profile: VoiceProfile, parent=None):
        super().__init__(parent); self.setObjectName("voiceCard"); self.store, self.project, self.profile = store, project, profile; assets = store.list_source_assets(project, profile.id); total = sum(item.duration_seconds for item in assets if not item.duplicate_of); current = next((item for item in profile.model_versions if item.id == profile.active_model_version_id), None)
        layout = QVBoxLayout(self); top = QHBoxLayout(); name = QLabel("🎙 " + profile.name); name.setObjectName("cardTitle"); badge = QLabel("✨ 已训练模型" if current else "⚡ 快速克隆"); badge.setObjectName("statusChip"); state = QLabel(profile.status(assets)); state.setObjectName("hint"); top.addWidget(name); top.addWidget(badge); top.addWidget(state); top.addStretch(); layout.addLayout(top)
        created = format_timestamp(current.created_at if current else profile.updated_at); layout.addWidget(QLabel(f"{total:.1f} 秒素材 · {len(assets)} 个文件 · 当前：{current.name if current else '原始参考声音'} · {created}"))
        target = self._preview_path(); self.player = InlineAudioPlayer(); self.player.set_source(target); layout.addWidget(self.player)
        row = QHBoxLayout(); preview = QPushButton("▶ 试听"); preview.clicked.connect(self.player.toggle); generate = QPushButton("用这个声音生成"); generate.setObjectName("primaryButton"); generate.clicked.connect(lambda: self.generate_requested.emit(profile.id)); rename = QPushButton("改名"); rename.clicked.connect(self._rename); retrain = QPushButton("追加训练"); retrain.clicked.connect(lambda: self.retrain_requested.emit(profile.id)); versions = QPushButton("版本历史 / A/B"); versions.clicked.connect(self._versions); row.addWidget(preview); row.addWidget(generate); row.addWidget(retrain); row.addWidget(versions); row.addWidget(rename); row.addStretch(); layout.addLayout(row)
        advanced = QGroupBox("更多操作"); advanced.setCheckable(True); advanced.setChecked(False); form = QFormLayout(advanced); open_folder = QPushButton("打开文件位置"); open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(project / "checkpoints" / profile.id)))); remove = QPushButton("删除声音配置"); remove.clicked.connect(self._remove); form.addRow(open_folder, remove); fold_group(advanced); layout.addWidget(advanced)
    def _preview_path(self) -> str:
        version = next((item for item in self.profile.model_versions if item.id == self.profile.active_model_version_id), None)
        if version:
            candidate = next((item for item in version.preview_outputs if item.lower().endswith(".wav") and Path(item).is_file()), "")
            if candidate: return candidate
        return next((item.path for item in self.profile.reference_assets if item.approved and Path(item.path).is_file()), "")
    def _preview(self) -> None:
        if self._preview_path(): self.player.toggle()
        else: show_error(self, "这个声音还没有可试听文件")
    def _rename(self) -> None:
        value, ok = RenameDialog.get(self.profile.name, self)
        if ok and value.strip(): self.profile.name = value.strip(); self.store.save_profile(self.project, self.profile); self.changed.emit()
    def _versions(self) -> None:
        dialog = VersionHistoryDialog(self.store, self.project, self.profile, self); dialog.changed.connect(self.changed); dialog.exec()
    def _remove(self) -> None:
        if QMessageBox.question(self, "删除声音配置", "只移除声音配置，原始素材、快照和模型都会保留。确定继续？") == QMessageBox.Yes: self.store.archive_profile(self.project, self.profile.id); self.changed.emit()

    def release_resources(self) -> None:
        self.player.release()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources(); super().closeEvent(event)


class RenameDialog:
    @staticmethod
    def get(value: str, parent):
        from PySide6.QtWidgets import QInputDialog
        return QInputDialog.getText(parent, "重命名声音", "新名称", text=value)


class MyVoicesPage(QWidget):
    profiles_changed = Signal(); generate_requested = Signal(str); retrain_requested = Signal(str)
    def __init__(self, store: StudioStore, project: Path):
        super().__init__(); self.store, self.project = store, project; root = QVBoxLayout(self); title = QLabel("我的声音"); title.setObjectName("pageTitle"); root.addWidget(title); root.addWidget(QLabel("试听、生成、追加素材或回退到旧版本。删除配置不会删除原始文件。")); self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); root.addWidget(self.scroll); self.refresh()
    def refresh(self) -> None:
        self.release_resources()
        content = QWidget(); layout = QVBoxLayout(content); profiles = [item for item in self.store.list_profiles(self.project) if not item.archived]
        if not profiles: empty = QLabel("还没有声音。去“一键训练”导入一批素材即可开始。"); empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignCenter); layout.addWidget(empty)
        for profile in profiles:
            card = VoiceCard(self.store, self.project, profile); card.generate_requested.connect(self.generate_requested); card.retrain_requested.connect(self.retrain_requested); card.changed.connect(self._changed); layout.addWidget(card)
        layout.addStretch(); self.scroll.setWidget(content)
    def _changed(self) -> None: self.refresh(); self.profiles_changed.emit()

    def release_resources(self) -> None:
        for card in self.scroll.findChildren(VoiceCard): card.release_resources()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources(); super().closeEvent(event)


class OneClickGeneratePage(QWidget):
    job_created = Signal(object); train_requested = Signal();
    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.store, self.project, self.client = store, project, client; self.pending: dict[str, tuple[str, Job, dict]] = {}; self.output_audio = QAudioOutput(self); self.player = QMediaPlayer(self); self.player.setAudioOutput(self.output_audio); self._build(); self.refresh_profiles(); self.refresh_history(); client.event.connect(self._event)
    def _build(self) -> None:
        root = QVBoxLayout(self); title = QLabel("一键生成"); title.setObjectName("pageTitle"); root.addWidget(title); self.empty = QFrame(); empty_layout = QVBoxLayout(self.empty); empty_label = QLabel("还没有可用声音"); empty_label.setObjectName("emptyState"); empty_label.setAlignment(Qt.AlignCenter); train = QPushButton("去一键训练"); train.setObjectName("primaryButton"); train.clicked.connect(self.train_requested); empty_layout.addWidget(empty_label); empty_layout.addWidget(train, alignment=Qt.AlignCenter); root.addWidget(self.empty)
        self.form = QGroupBox("生成语音"); form = QFormLayout(self.form); self.profile = QComboBox(); self.text = QPlainTextEdit(); self.text.setPlaceholderText("输入文字，支持中文和中英混合长文本……"); self.text.setMinimumHeight(125); self.text.textChanged.connect(self._update_text_info); self.text_info = QLabel("0 字 · 等待输入"); self.text_info.setObjectName("hint"); self.speed = QDoubleSpinBox(); self.speed.setRange(.6, 1.6); self.speed.setValue(1); self.speed.setSingleStep(.05); form.addRow("声音", self.profile); form.addRow("文字", self.text); form.addRow("", self.text_info); form.addRow("语速", self.speed); self.generate = QPushButton("生成并播放"); self.generate.setObjectName("primaryButton"); self.generate.clicked.connect(self._generate); form.addRow(self.generate); self.progress = QProgressBar(); form.addRow(self.progress); root.addWidget(self.form)
        advanced = QGroupBox("高级设置"); advanced.setCheckable(True); advanced.setChecked(False); advanced_form = QFormLayout(advanced); self.output = QLineEdit(str(self.store.get_setting("default_output_dir", str(self.project / "exports")))); browse = QPushButton("选择…"); browse.clicked.connect(self._browse); output_row = QHBoxLayout(); output_row.addWidget(self.output); output_row.addWidget(browse); self.language = QComboBox(); self.language.addItem("自动识别中英混合", "auto"); self.pause = QDoubleSpinBox(); self.pause.setRange(.05, 2); self.pause.setValue(.3); self.seed = QSpinBox(); self.seed.setRange(-1, 2147483647); self.seed.setValue(-1); advanced_form.addRow("输出目录", output_row); advanced_form.addRow("语言", self.language); advanced_form.addRow("段间停顿", self.pause); advanced_form.addRow("随机种子", self.seed); fold_group(advanced); root.addWidget(advanced)
        history_title = QHBoxLayout(); history_title.addWidget(QLabel("最近 5 次生成")); history_title.addStretch(); open_folder = QPushButton("打开输出文件夹"); open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.output.text()))); history_title.addWidget(open_folder); root.addLayout(history_title); self.history = QScrollArea(); self.history.setWidgetResizable(True); self.history.setMinimumHeight(180); root.addWidget(self.history, 1)
    def refresh_profiles(self, selected_id: str = "") -> None:
        current = selected_id or self.profile.currentData() if hasattr(self, "profile") else selected_id; self.profile.clear(); self.profiles = []
        for profile in self.store.list_profiles(self.project):
            if profile.archived: continue
            refs = [item for item in profile.reference_assets if item.approved and item.transcript.strip() and Path(item.path).is_file()]
            tuned = bool(profile.active_gpt_checkpoint and profile.active_sovits_checkpoint and Path(profile.active_gpt_checkpoint).is_file() and Path(profile.active_sovits_checkpoint).is_file())
            if refs and (tuned or profile.default_model_mode == "zero_shot"):
                self.profiles.append(profile); self.profile.addItem(profile.name, profile.id)
        self.empty.setVisible(not self.profiles); self.form.setVisible(bool(self.profiles))
        if current:
            index = self.profile.findData(current)
            if index >= 0: self.profile.setCurrentIndex(index)
    def select_profile(self, profile_id: str) -> None: self.refresh_profiles(profile_id)
    def _update_text_info(self) -> None:
        characters, segments = estimate_text_work(self.text.toPlainText(), 120); self.text_info.setText(f"{characters:,} 字 · 将自动分成 {segments} 段" if segments else "0 字 · 等待输入")
    def _browse(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if value: self.output.setText(value); self.store.set_setting("default_output_dir", value)
    def _generate(self) -> None:
        try:
            if not self.profiles: raise ValueError("请先训练一个可用声音")
            text = self.text.toPlainText().strip()
            if not text: raise ValueError("请输入要生成的文字")
            profile = self.profiles[self.profile.currentIndex()]; ref = next(item for item in profile.reference_assets if item.approved and item.transcript.strip() and Path(item.path).is_file())
            payload = {"text": text, "text_lang": self.language.currentData(), "ref_audio_path": ref.path, "prompt_text": ref.transcript, "prompt_lang": ref.language, "output_dir": self.output.text(), "speed_factor": self.speed.value(), "fragment_interval": self.pause.value(), "seed": self.seed.value(), "max_chars": 120, "profile_id": profile.id}
            job = Job(JobKind.SYNTHESIZE, payload); self.store.save_job(job); self.job_created.emit(job)
            profile_payload = profile.to_dict(); profile_payload["project_path"] = str(self.project)
            request = self.client.send("load_profile", profile_payload); self.pending[request] = ("load", job, payload); self.generate.setEnabled(False); self.progress.setValue(0)
        except Exception as exc: show_error(self, str(exc))
    def _event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id not in self.pending: return
        stage, job, synthesis = self.pending[request_id]
        if event == "progress": job.status = JobStatus.RUNNING; job.progress = float(payload.get("progress", 0)); job.message = str(payload.get("message", "")); self.progress.setValue(round(job.progress * 100)); self.store.save_job(job); return
        if event == "error": self.pending.pop(request_id, None); job.status = JobStatus.FAILED; job.error = str(payload.get("message", "")); self.store.save_job(job); self.generate.setEnabled(True); self.refresh_history(); show_error(self, job.error); return
        if event != "result": return
        self.pending.pop(request_id, None)
        if stage == "load":
            request = self.client.send("synthesize", synthesis); self.pending[request] = ("synth", job, synthesis); return
        job.status = JobStatus.COMPLETED; job.progress = 1; job.outputs = list(payload.get("outputs", [])); self.store.save_job(job); self.progress.setValue(100); self.generate.setEnabled(True)
        wav = next((item for item in job.outputs if item.lower().endswith(".wav") and Path(item).is_file()), "")
        if wav: self.player.setSource(QUrl.fromLocalFile(wav)); self.player.play()
        self.refresh_history()
    def refresh_history(self) -> None:
        for card in self.history.findChildren(GenerationRecordCard): card.release_resources()
        content = QWidget(); layout = QVBoxLayout(content); names = {item.id: item.name for item in self.store.list_profiles(self.project)}; count = 0
        for job in self.store.list_jobs(80):
            if job.kind != JobKind.SYNTHESIZE or str(job.payload.get("profile_id", "")) not in names: continue
            record = GenerationRecord.from_job(job, names[str(job.payload.get("profile_id"))]); card = GenerationRecordCard(record); card.retry_requested.connect(self._retry); layout.addWidget(card); count += 1
            if count >= 5: break
        if not count:
            empty = QLabel("生成完成后会在这里保留文字、声音和试听记录"); empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignCenter); layout.addWidget(empty)
        layout.addStretch(); self.history.setWidget(content)
    def _retry(self, job: Job) -> None:
        self.text.setPlainText(str(job.payload.get("text", ""))); index = self.profile.findData(str(job.payload.get("profile_id", "")))
        if index >= 0: self.profile.setCurrentIndex(index)
        self.speed.setValue(float(job.payload.get("speed_factor", 1.0))); self._generate()

    def release_resources(self) -> None:
        self.player.stop(); self.player.setSource(QUrl())
        for card in self.history.findChildren(GenerationRecordCard): card.release_resources()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources(); super().closeEvent(event)


class TaskCenterDialog(QDialog):
    def __init__(self, store: StudioStore, parent=None):
        super().__init__(parent); self.store = store; self.setWindowTitle("任务中心"); self.resize(880, 480); layout = QVBoxLayout(self); self.table = QTableWidget(0, 6); self.table.setHorizontalHeaderLabels(["时间", "类型", "状态", "进度", "说明 / 失败原因", "输出位置"]); self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch); self.table.cellDoubleClicked.connect(self._open); layout.addWidget(self.table); self.refresh()
    def refresh(self) -> None:
        jobs = self.store.list_jobs(); self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            values = [job.updated_at[:19].replace("T", " "), job.kind.value, job.status.value, f"{job.progress * 100:.0f}%", job.error or job.message, "、".join(job.outputs)]
            for column, value in enumerate(values): self.table.setItem(row, column, QTableWidgetItem(value))
    def _open(self, row: int, _column: int) -> None:
        value = self.table.item(row, 5).text().split("、")[0]
        if value: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(value).parent if Path(value).suffix else Path(value))))


class SimpleSettingsPage(QWidget):
    install_requested = Signal()
    def __init__(self, paths: AppPaths, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.paths, self.store, self.project, self.client = paths, store, project, client; self.health_request = ""; self.raw = {}; root = QVBoxLayout(self); title = QLabel("设置"); title.setObjectName("pageTitle"); root.addWidget(title); self.health_card = QFrame(); self.health_card.setObjectName("healthCard"); health_layout = QHBoxLayout(self.health_card); text = QVBoxLayout(); top = QHBoxLayout(); self.health_title = QLabel("本地引擎"); self.health_title.setObjectName("cardTitle"); self.health_badge = QLabel("正在检测"); self.health_badge.setObjectName("statusChip"); top.addWidget(self.health_title); top.addWidget(self.health_badge); top.addStretch(); text.addLayout(top); self.health_detail = QLabel("正在检查显卡、CUDA、模型和 FFmpeg…"); self.health_detail.setObjectName("hint"); self.health_detail.setWordWrap(True); text.addWidget(self.health_detail); self.health_specs = QLabel(""); self.health_specs.setWordWrap(True); text.addWidget(self.health_specs); health_layout.addLayout(text, 1); actions = QVBoxLayout(); detect = QPushButton("重新检测"); detect.clicked.connect(self.check_health); repair = QPushButton("修复本地引擎"); repair.setObjectName("primaryButton"); repair.clicked.connect(self._repair); actions.addWidget(detect); actions.addWidget(repair); actions.addStretch(); health_layout.addLayout(actions); root.addWidget(self.health_card)
        common = QGroupBox("普通设置"); form = QFormLayout(common); self.output = QLineEdit(str(store.get_setting("default_output_dir", str(project / "exports")))); browse = QPushButton("选择…"); browse.clicked.connect(self._browse); row = QHBoxLayout(); row.addWidget(self.output); row.addWidget(browse); self.smart = QCheckBox("默认开启智能优化"); self.smart.setChecked(bool(store.get_setting("smart_optimization", True))); self.smart.toggled.connect(lambda value: store.set_setting("smart_optimization", value)); disk = shutil.disk_usage(paths.data_root); self.disk = QLabel(f"可用 {disk.free / 1024**3:.1f} GB / 共 {disk.total / 1024**3:.1f} GB"); cache = QPushButton("清理缓存"); cache.clicked.connect(self._clean_cache); form.addRow("默认输出目录", row); form.addRow("声音处理", self.smart); form.addRow("磁盘空间", self.disk); form.addRow("缓存", cache); root.addWidget(common)
        advanced = QGroupBox("高级 / 原始诊断"); advanced.setCheckable(True); advanced.setChecked(False); advanced_layout = QFormLayout(advanced); advanced_layout.addRow("私有 Python", QLabel(str(paths.private_python))); advanced_layout.addRow("模型目录", QLabel(str(paths.models_root))); self.report = QPlainTextEdit(); self.report.setReadOnly(True); advanced_layout.addRow(self.report); copy = QPushButton("复制诊断"); copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText())); advanced_layout.addRow(copy); fold_group(advanced); root.addWidget(advanced); root.addStretch(); client.event.connect(self._event); self.check_health()
    def check_health(self) -> None:
        self.health_badge.setText("正在检测"); self.health_badge.setObjectName("statusChip")
        try: self.health_request = self.client.send("health")
        except Exception as exc: self.health_badge.setText("需要修复"); self.health_badge.setObjectName("dangerChip"); self.health_detail.setText(str(exc))
    def _event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id != self.health_request: return
        self.raw = payload
        compatible = event == "result" and payload.get("compatible")
        self.health_badge.setText("● 正常" if compatible else "需要修复"); self.health_badge.setObjectName("statusChip" if compatible else "dangerChip"); self.health_badge.style().unpolish(self.health_badge); self.health_badge.style().polish(self.health_badge)
        gpu = str(payload.get("gpu_name") or "未检测到可用 GPU"); cuda = str(payload.get("cuda_version") or "-"); acceleration = "GPU 加速已启用" if payload.get("tensor_test_passed") else "GPU 加速未就绪"; models = "模型完整" if payload.get("models_ready") else "模型不完整"
        integrity = "完整性已验证" if payload.get("runtime_integrity") else "完整性需修复"
        issue = str(payload.get("message") or "") or "；".join(payload.get("runtime_integrity_errors") or []) or "引擎或显卡检测未通过"
        self.health_detail.setText(gpu if compatible else issue); self.health_specs.setText(f"CUDA {cuda} · {acceleration} · {models} · {integrity}\n运行目录：{self.paths.data_root}")
        import json; self.report.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
    def _repair(self) -> None:
        if self.raw.get("compatible"):
            self.check_health(); QMessageBox.information(self, "VoiceStudio", "本地引擎已可用，正在重新检测。")
        else:
            self.install_requested.emit()
    def _browse(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "默认输出目录", self.output.text())
        if value: self.output.setText(value); self.store.set_setting("default_output_dir", value)
    def _clean_cache(self) -> None:
        cache = self.paths.data_root / "cache"
        if QMessageBox.question(self, "清理缓存", "只清理可重新生成的试听缓存，不会删除声音、训练数据或输出。") == QMessageBox.Yes:
            if cache.is_dir(): shutil.rmtree(cache)
            cache.mkdir(parents=True, exist_ok=True); QMessageBox.information(self, "VoiceStudio", "缓存已清理")
