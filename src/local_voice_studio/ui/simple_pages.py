from __future__ import annotations

import shutil
from uuid import uuid4
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QStackedWidget, QSlider,
)

from ..audio import AudioProbe, copy_original, scan_audio_files
from ..models import DatasetDraft, Job, JobKind, JobStatus, SourceAsset, TrainingWorkflow, VoiceProfile, WorkflowStage, WorkflowStatus, utc_now
from ..models import GenerationRecord as StoredGenerationRecord
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


class GenerationHistoryMiniCard(GenerationRecordCard):
    """Compact real-generation record used by the HTML v4 preview history rail."""

    def __init__(self, record: GenerationRecord, parent: QWidget | None = None):
        QFrame.__init__(self, parent)
        self.record = record
        self.setObjectName("ttsHistoryItem")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        title = QLabel(f"{format_timestamp(record.job.updated_at)} · {record.profile_name}")
        title.setObjectName("ttsHistoryItemTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        excerpt = record.text.replace("\n", " ").strip()
        text = QLabel(f"“{excerpt[:34]}{'…' if len(excerpt) > 34 else ''}”")
        text.setObjectName("ttsHistoryItemText")
        text.setWordWrap(True)
        layout.addWidget(text)
        actions = QHBoxLayout()
        open_button = QPushButton("打开")
        open_button.setObjectName("miniButton")
        open_button.clicked.connect(self._open)
        retry = QPushButton("重试")
        retry.setObjectName("miniButton")
        retry.clicked.connect(lambda: self.retry_requested.emit(record.job))
        actions.addWidget(open_button)
        actions.addWidget(retry)
        actions.addStretch()
        layout.addLayout(actions)

    def release_resources(self) -> None:
        """The compact card has no player to release."""


class TtsPreviewWave(QFrame):
    """Visual-only waveform motif, enabled exclusively by the isolated preview adapter."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._visual_sample = False

    def set_visual_sample(self, enabled: bool) -> None:
        self._visual_sample = bool(enabled)
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        if not self._visual_sample:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setClipRect(self.rect().adjusted(10, 8, -10, -8))
        pen = QPen(QColor("#d98548"))
        pen.setWidth(2)
        painter.setPen(pen)
        midpoint = self.height() // 2
        pattern = (8, 17, 30, 12, 24, 38, 16, 27, 10, 34, 20, 14, 29, 42, 18, 25)
        for index, x in enumerate(range(14, max(15, self.width() - 14), 6)):
            height = pattern[index % len(pattern)]
            painter.drawLine(x, midpoint - height // 2, x, midpoint + height // 2)
        painter.end()


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
    # These are the legacy narration/voice-data steps.  Singing training is
    # deliberately presented in its own card above and does not pass through
    # the transcription/review flow below.
    STEPS = ("检查素材", "清理切片", "旁白识别", "确认数据", "训练模型", "验证并保存")

    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.store, self.project, self.client = store, project, client
        self.probes: list[AudioProbe] = []; self.scan_thread: ScanThread | None = None; self.workflow: TrainingWorkflow | None = None; self.draft: DatasetDraft | None = None; self._step_results: dict[int, str] = {}; self.review_indices: list[int] = []; self.review_position = 0
        self.controller = TrainingWorkflowController(store, project, client, self)
        self.controller.workflow_changed.connect(self._workflow_changed); self.controller.draft_ready.connect(self._show_draft); self.controller.profile_changed.connect(lambda _id: self._profiles_changed()); self.controller.job_created.connect(self.job_created)
        self.singing_request = ""; client.event.connect(self._singing_event)
        self._build(); self._restore()

    def _build(self) -> None:
        self.setMinimumHeight(0); outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setMinimumHeight(0); scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored); content = QWidget(); root = QVBoxLayout(content); root.setContentsMargins(26, 22, 26, 24); root.setSpacing(18); outer.addWidget(scroll); scroll.setWidget(content); self.content_scroll = scroll
        self.setObjectName("trainingPage")
        title_bar = QFrame(); title_bar.setObjectName("trainingTitleBar"); title_layout = QHBoxLayout(title_bar); title_layout.setContentsMargins(0, 0, 0, 0); copy = QVBoxLayout(); copy.setSpacing(4); title = QLabel("训练声音"); title.setObjectName("pageTitle"); subtitle = QLabel("从原始录音到可用于文字生成和 AI 翻唱的完整训练流程。所有原始音频保持不变。"); subtitle.setObjectName("pageSubtitle"); subtitle.setWordWrap(True); copy.addWidget(title); copy.addWidget(subtitle); title_layout.addLayout(copy); title_layout.addStretch(); restore = QPushButton("载入上次草稿"); restore.setObjectName("trainingRestore"); restore.clicked.connect(self._restore); title_layout.addWidget(restore); root.addWidget(title_bar)
        workspace = QHBoxLayout(); workspace.setSpacing(18); root.addLayout(workspace, 1)
        steps_panel = QFrame(); steps_panel.setObjectName("trainingStepsPanel"); steps_panel.setMinimumWidth(210); steps_panel.setMaximumWidth(250); steps_layout = QVBoxLayout(steps_panel); steps_layout.setContentsMargins(16, 16, 16, 16); steps_layout.setSpacing(10); steps_label = QLabel("训练流程"); steps_label.setObjectName("trainingSectionLabel"); steps_layout.addWidget(steps_label)
        self.timeline = StepTimeline(self.STEPS); self.steps = self.timeline.labels; steps_layout.addWidget(self.timeline); steps_layout.addStretch(); steps_note = QLabel("当前流程只会在本机处理授权素材。训练完成后仍需通过模型验证，才会加入声音库。"); steps_note.setObjectName("trainingStepsNote"); steps_note.setWordWrap(True); steps_layout.addWidget(steps_note); workspace.addWidget(steps_panel)
        center = QVBoxLayout(); center.setSpacing(14); workspace.addLayout(center, 1)
        self.singing_card = QGroupBox("AI 翻唱模型训练"); self.singing_card.setObjectName("trainingSingingCard"); singing_form = QFormLayout(self.singing_card)
        self.singing_profile = QComboBox(); self.singing_profile.currentIndexChanged.connect(self._refresh_singing_card)
        self.singing_status = QLabel("未选择声音"); self.singing_status.setObjectName("statusChip")
        self.singing_detail = QLabel("训练完成并通过验证后，才可在 AI 翻唱中使用。现在的训练流程会自动保存歌唱模型状态。"); self.singing_detail.setWordWrap(True); self.singing_detail.setObjectName("hint")
        singing_form.addRow("声音配置", self.singing_profile); singing_form.addRow("状态", self.singing_status); singing_form.addRow(self.singing_detail)
        singing_actions = QHBoxLayout(); self.singing_train_button = QPushButton("训练歌唱模型"); self.singing_train_button.setObjectName("primaryButton"); self.singing_train_button.clicked.connect(self._start_singing_training); singing_actions.addWidget(self.singing_train_button); self.singing_cancel_button = QPushButton("取消训练"); self.singing_cancel_button.clicked.connect(self._cancel_singing_training); self.singing_cancel_button.hide(); singing_actions.addWidget(self.singing_cancel_button); self.singing_manage_button = QPushButton("管理版本"); self.singing_manage_button.clicked.connect(self._manage_singing_versions); singing_actions.addWidget(self.singing_manage_button); singing_form.addRow(singing_actions)
        self.singing_progress = QProgressBar(); self.singing_progress.setRange(0, 100); self.singing_progress.setValue(0); singing_form.addRow("训练进度", self.singing_progress)
        self.singing_model_card = self.singing_card; self.singing_model_status = self.singing_status; self.singing_model_profile = self.singing_profile; self.train_singing = self.singing_train_button
        card = QGroupBox("导入声音素材"); card.setObjectName("trainingMaterialsCard"); form = QFormLayout(card); self.name = QLineEdit("我的声音"); self.name.setPlaceholderText("例如：我的旁白声"); self.consent = QCheckBox("我确认这是本人声音，或已经取得明确授权"); form.addRow("声音名称", self.name); form.addRow("授权确认", self.consent); self.drop = DropArea(); self.drop.paths_dropped.connect(self._scan); form.addRow(self.drop)
        row = QHBoxLayout(); files = QPushButton("选择音频"); files.clicked.connect(self._files); folder = QPushButton("选择文件夹"); folder.clicked.connect(self._folder); manage = QPushButton("管理已导入素材"); manage.clicked.connect(self._manage_assets); row.addWidget(files); row.addWidget(folder); row.addWidget(manage); row.addStretch(); form.addRow(row); center.addWidget(card)
        samples_card = QFrame(); samples_card.setObjectName("trainingSamplesCard"); samples_layout = QVBoxLayout(samples_card); samples_layout.setContentsMargins(16, 14, 16, 14); samples_head = QHBoxLayout(); sample_copy = QVBoxLayout(); sample_title = QLabel("素材列表"); sample_title.setObjectName("cardTitle"); sample_subtitle = QLabel("自动探测格式、时长和重复项；可随时管理已导入素材。"); sample_subtitle.setObjectName("hint"); sample_copy.addWidget(sample_title); sample_copy.addWidget(sample_subtitle); samples_head.addLayout(sample_copy); samples_head.addStretch(); clean = QPushButton("管理素材"); clean.clicked.connect(self._manage_assets); samples_head.addWidget(clean); samples_layout.addLayout(samples_head); self.material = QLabel("尚未导入素材"); self.material.setObjectName("trainingMaterialState"); self.material.setWordWrap(True); samples_layout.addWidget(self.material); center.addWidget(samples_card)
        self.review = QGroupBox("逐条试听并确认"); review_layout = QVBoxLayout(self.review); self.review_hint = QLabel("默认只审核异常片段；Space 播放，Enter 确认，Delete 排除，↑↓ 切换。"); self.review_hint.setObjectName("hint"); review_layout.addWidget(self.review_hint); self.review_card = ReviewSegmentCard(); self.review_card.changed.connect(self._review_changed); self.review_card.confirmed.connect(self._review_confirmed); self.review_card.navigate.connect(self._review_navigate); review_layout.addWidget(self.review_card)
        self.show_all = QCheckBox("查看全部片段表格"); self.show_all.toggled.connect(self._populate_review); review_layout.addWidget(self.show_all); self.table = QTableWidget(0, 5); self.table.setHorizontalHeaderLabels(["纳入", "片段", "时长", "识别文字", "问题"]); self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch); self.table.setVisible(False); review_layout.addWidget(self.table); self.review.setVisible(False); center.addWidget(self.review)
        advanced = QGroupBox("高级 / 查看旁白流程详情"); advanced.setCheckable(True); advanced.setChecked(False); advanced_layout = QVBoxLayout(advanced); self.smart = QCheckBox("智能优化（人声分离与降噪）"); self.smart.setChecked(bool(self.store.get_setting("smart_optimization", True))); advanced_layout.addWidget(self.smart); self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(100); advanced_layout.addWidget(self.details); fold_group(advanced); center.addWidget(advanced); center.addStretch()
        right = QVBoxLayout(); right.setSpacing(14); workspace.addLayout(right); diagnostics = QFrame(); diagnostics.setObjectName("trainingDiagnosticsCard"); diagnostics.setMinimumWidth(245); diagnostics.setMaximumWidth(300); diagnostics_layout = QVBoxLayout(diagnostics); diagnostics_layout.setContentsMargins(16, 16, 16, 16); diagnostics_layout.setSpacing(9); diagnostic_label = QLabel("素材质量诊断"); diagnostic_label.setObjectName("trainingSectionLabel"); diagnostics_layout.addWidget(diagnostic_label); self.quality_summary = QLabel("等待导入授权素材"); self.quality_summary.setObjectName("trainingQualitySummary"); diagnostics_layout.addWidget(self.quality_summary); self.quality_bar = QProgressBar(); self.quality_bar.setRange(0, 100); self.quality_bar.setValue(0); self.quality_bar.setTextVisible(False); diagnostics_layout.addWidget(self.quality_bar); self.quality_details = QLabel("音频探测会显示有效时长、重复项与基础格式信息。不会把未检测到的数据标记为质量通过。"); self.quality_details.setObjectName("trainingQualityDetails"); self.quality_details.setWordWrap(True); diagnostics_layout.addWidget(self.quality_details); right.addWidget(diagnostics); right.addWidget(self.singing_card)
        run_card = QFrame(); run_card.setObjectName("trainingRunCard"); run_layout = QVBoxLayout(run_card); run_layout.setContentsMargins(16, 16, 16, 16); run_layout.setSpacing(10); run_label = QLabel("旁白训练"); run_label.setObjectName("trainingSectionLabel"); run_layout.addWidget(run_label); self.progress = QProgressBar(); self.progress.setRange(0, 100); run_layout.addWidget(self.progress); self.status = QLabel("导入素材后即可开始"); self.status.setObjectName("trainingRunStatus"); self.status.setWordWrap(True); run_layout.addWidget(self.status); self.primary = QPushButton("导入素材"); self.primary.setObjectName("primaryButton"); self.primary.clicked.connect(self._primary_action); self.more = QPushButton("继续导入"); self.more.clicked.connect(self._files); self.record = QPushButton("录几句"); self.record.clicked.connect(self._record); self.cancel = QPushButton("取消当前任务"); self.cancel.clicked.connect(self._cancel); self.cancel.setVisible(False); run_layout.addWidget(self.primary); action_row = QHBoxLayout(); action_row.addWidget(self.more); action_row.addWidget(self.record); action_row.addStretch(); run_layout.addLayout(action_row); run_layout.addWidget(self.cancel); right.addWidget(run_card); right.addStretch()
        self._refresh_singing_profiles()

    def _refresh_singing_profiles(self) -> None:
        current = self.singing_profile.currentData(); self.singing_profile.clear()
        for profile in self.store.list_profiles(self.project):
            if not profile.archived: self.singing_profile.addItem(profile.name, profile.id)
        if current:
            index = self.singing_profile.findData(current)
            if index >= 0: self.singing_profile.setCurrentIndex(index)
        self._refresh_singing_card()

    def _refresh_singing_card(self) -> None:
        profile_id = self.singing_profile.currentData()
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None)
        if not profile:
            self.singing_status.setText("未选择声音"); return
        try: state = profile.singing_status(self.project)
        except TypeError: state = profile.singing_status()
        assets = [item for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
        duration = sum(item.duration_seconds for item in assets)
        labels = {"ready": "就绪", "training": "训练中", "untrusted": "未验证", "verification_failed": "训练完成，但模型验证失败", "model_missing": "模型缺失", "not_ready": "未生成"}
        self.singing_status.setText(labels.get(state, str(state)))
        minutes, seconds = divmod(round(duration), 60); elapsed = f"{minutes}:{seconds:02d}"
        if duration < 180:
            amount = f"素材不足 · {elapsed} / 最低 3:00"
        elif duration < 600:
            amount = f"可训练 · {elapsed} · 建议准备 10 分钟以上素材"
        else:
            amount = f"数据量充足 · {elapsed}"
        prefix = "模型已验证，可用于 AI 翻唱。" if state == "ready" else "训练完成并通过验证后，才可在 AI 翻唱中使用。"
        self.singing_detail.setText(f"{amount}\n{prefix} 当前共 {len(assets)} 个授权素材。")
        self.singing_train_button.setEnabled(not self.singing_request and duration >= 180 and bool(profile.consent_confirmed))

    def _start_singing_training(self) -> None:
        profile_id = self.singing_profile.currentData()
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id), None)
        if not profile: self.singing_status.setText("请先选择声音"); return
        if not profile.consent_confirmed: self.singing_status.setText("未授权，无法训练"); return
        assets = [item for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
        if not assets: self.singing_status.setText("没有可用训练素材"); return
        self.singing_request = uuid4().hex
        payload = {"project_path": str(self.project), "profile_id": profile.id, "source_asset_ids": [item.id for item in assets], "training_run_id": uuid4().hex, "engine": "rvc_v2"}
        try:
            self.singing_request = self.client.send("train_singing_model", payload, request_id=self.singing_request)
        except Exception as exc:
            self.singing_request = ""; self.singing_status.setText("训练启动失败：" + str(exc)); self.singing_train_button.setEnabled(True); return
        self.singing_train_button.setEnabled(False); self.singing_cancel_button.show(); self.singing_status.setText("训练中（0/8）"); self.singing_progress.setValue(0)

    def _cancel_singing_training(self) -> None:
        if not self.singing_request: return
        self.singing_cancel_button.setEnabled(False); self.singing_status.setText("正在停止歌唱模型训练…")
        try: self.client.send("cancel", {"target_request_id": self.singing_request})
        except Exception as exc: self.singing_detail.setText(str(exc)); self.singing_cancel_button.setEnabled(True)

    def _manage_singing_versions(self) -> None:
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == self.singing_profile.currentData()), None)
        if not profile: return
        dialog = QDialog(self); dialog.setWindowTitle(profile.name + " · 歌唱模型版本"); dialog.resize(760, 420); root = QVBoxLayout(dialog)
        table = QTableWidget(len(profile.singing_models), 6); table.setHorizontalHeaderLabels(["版本", "创建时间", "训练时长", "数据 Hash", "状态", "当前"]); table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setStyleSheet("QTableWidget { background: #121621; color: #f3f5ff; gridline-color: #30364a; } QHeaderView::section { background: #23283a; color: #f3f5ff; padding: 8px; border: 1px solid #30364a; }")
        for row, model in enumerate(reversed(profile.singing_models)):
            seconds = sum(float(item.get("duration_seconds", 0)) for item in model.training_lineage)
            values = [model.id[:12], format_timestamp(model.created_at), f"{seconds:.1f} 秒", model.training_dataset_sha256[:12], model.trust_status, "是" if model.id == profile.active_singing_model_id else ""]
            for column, value in enumerate(values): table.setItem(row, column, QTableWidgetItem(value))
            table.item(row, 0).setData(Qt.UserRole, model.id)
        root.addWidget(table); actions = QHBoxLayout(); activate = QPushButton("设为当前"); remove = QPushButton("删除非当前版本"); close = QPushButton("关闭"); actions.addWidget(activate); actions.addWidget(remove); actions.addStretch(); actions.addWidget(close); root.addLayout(actions)
        def selected_model():
            row = table.currentRow(); return next((item for item in profile.singing_models if row >= 0 and item.id == table.item(row, 0).data(Qt.UserRole)), None)
        def set_active():
            model = selected_model()
            if not model: return
            if model.trust_status != "verified": QMessageBox.warning(dialog, "VoiceStudio", "只有已验证模型可以设为当前版本"); return
            profile.active_singing_model_id = model.id; self.store.save_profile(self.project, profile); dialog.accept()
        def delete_version():
            model = selected_model()
            if not model or model.id == profile.active_singing_model_id: QMessageBox.warning(dialog, "VoiceStudio", "当前版本不能删除"); return
            profile.singing_models = [item for item in profile.singing_models if item.id != model.id]; self.store.save_profile(self.project, profile); dialog.accept()
        activate.clicked.connect(set_active); remove.clicked.connect(delete_version); close.clicked.connect(dialog.reject); dialog.exec(); self._refresh_singing_card()

    def _singing_event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id != getattr(self, "singing_request", ""): return
        if event == "progress":
            progress = float(payload.get("progress", 0)); self.singing_progress.setValue(round(progress * 100 if progress <= 1 else progress)); self.singing_status.setText(str(payload.get("message") or "训练中")); self.singing_detail.setText(f"歌唱模型训练阶段：{payload.get('stage', '进行中')} · {payload.get('step', '8')} 阶段")
        elif event == "result":
            self.singing_request = ""; self.singing_train_button.setEnabled(True); self.singing_cancel_button.hide(); self.singing_cancel_button.setEnabled(True); self.singing_status.setText("就绪"); self.singing_progress.setValue(100); self.singing_detail.setText("歌唱模型已训练并通过验证，可用于 AI 翻唱。")
            self._refresh_singing_profiles()
        elif event == "error":
            self.singing_request = ""; self.singing_train_button.setEnabled(True); self.singing_cancel_button.hide(); self.singing_cancel_button.setEnabled(True); self.singing_progress.setValue(0); self.singing_status.setText("已取消" if payload.get("status") == "cancelled" else "训练失败"); self.singing_detail.setText(str(payload.get("message") or "训练失败，请重试。"))

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
        if usable:
            formats = ", ".join(sorted({item.codec.upper() for item in usable if item.codec})) or "已探测"
            self.quality_summary.setText("已完成基础素材探测")
            self.quality_bar.setValue(min(100, round(min(total, 600) / 6)))
            self.quality_details.setText(f"有效纯人声：{total:.1f} 秒\n可用文件：{len(usable)} 个 · 重复项：{duplicates} 个\n格式：{formats}\n详细的噪声与可训练性会在预处理阶段由本地 Worker 给出。")
        else:
            self.quality_summary.setText("未找到可用素材")
            self.quality_bar.setValue(0)
            self.quality_details.setText("所有文件均为重复项或无法使用。请导入新的授权音频后重新检测。")
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
            self.store.save_profile(self.project, profile); self._profiles_changed(); self._refresh_singing_profiles()
            selected = [item.id for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
            self.workflow = self.controller.start(profile, selected, self.smart.isChecked())
        except Exception as exc: show_error(self, str(exc))

    def _workflow_changed(self, workflow: TrainingWorkflow) -> None:
        if self.workflow and workflow.id != self.workflow.id: return
        changed_workflow = not self.workflow or workflow.id != self.workflow.id; self.workflow = workflow
        if changed_workflow: self._load_step_results(workflow)
        self.progress.setValue(round(workflow.progress * 100)); self.status.setText(workflow.waiting_reason or workflow.error or workflow.message); self.details.appendPlainText(f"{workflow.stage.value}: {workflow.message or workflow.error}"); self.cancel.setVisible(workflow.status == WorkflowStatus.RUNNING); self._refresh_singing_profiles()
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
        self.review_card.release_resources(); self.workflow = None; self.draft = None; self.probes = []; self._step_results = {}; self.timeline.update_state(0, {}); self.review.setVisible(False); self.progress.setValue(0); self.name.setText(profile.name if profile else "我的声音"); self.consent.setChecked(bool(profile and profile.consent_confirmed)); self.primary.setText("导入素材"); self.primary.setEnabled(True); self._refresh_singing_profiles()
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
    generate_requested = Signal(str); retrain_requested = Signal(str); selected = Signal(str); changed = Signal()
    def __init__(self, store: StudioStore, project: Path, profile: VoiceProfile, parent=None):
        super().__init__(parent); self.setObjectName("voiceCard"); self.store, self.project, self.profile = store, project, profile; assets = store.list_source_assets(project, profile.id); total = sum(item.duration_seconds for item in assets if not item.duplicate_of); current = next((item for item in profile.model_versions if item.id == profile.active_model_version_id), None)
        layout = QVBoxLayout(self); top = QHBoxLayout(); name = QLabel(profile.name); name.setObjectName("cardTitle"); badge = QLabel("已训练模型" if current else "快速克隆"); badge.setObjectName("statusChip"); state = QLabel(profile.status(assets)); state.setObjectName("hint"); top.addWidget(name); top.addWidget(badge); top.addWidget(state); top.addStretch(); layout.addLayout(top)
        created = format_timestamp(current.created_at if current else profile.updated_at); layout.addWidget(QLabel(f"{total:.1f} 秒素材 · {len(assets)} 个文件 · 当前：{current.name if current else '原始参考声音'} · {created}"))
        target = self._preview_path(); self.player = InlineAudioPlayer(); self.player.set_source(target); layout.addWidget(self.player)
        row = QHBoxLayout(); preview = QPushButton("▶ 试听"); preview.clicked.connect(self._preview); generate = QPushButton("用这个声音生成"); generate.setObjectName("primaryButton"); generate.clicked.connect(lambda: self.generate_requested.emit(profile.id)); rename = QPushButton("改名"); rename.clicked.connect(self._rename); retrain = QPushButton("追加训练"); retrain.clicked.connect(lambda: self.retrain_requested.emit(profile.id)); versions = QPushButton("版本历史 / A/B"); versions.clicked.connect(self._versions); row.addWidget(preview); row.addWidget(generate); row.addWidget(retrain); row.addWidget(versions); row.addWidget(rename); row.addStretch(); layout.addLayout(row)
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

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.selected.emit(self.profile.id)
        super().mouseReleaseEvent(event)

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
        super().__init__(); self.store, self.project, self._filter, self._selected_profile_id = store, project, "all", ""; self.setObjectName("voicesPage"); self.setMinimumHeight(0)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); page_scroll = QScrollArea(); page_scroll.setWidgetResizable(True); page_scroll.setFrameShape(QFrame.NoFrame); page_scroll.setMinimumHeight(0); page_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored); content = QWidget(); root = QVBoxLayout(content); root.setContentsMargins(24, 20, 24, 18); root.setSpacing(14); outer.addWidget(page_scroll); page_scroll.setWidget(content); self.content_scroll = page_scroll
        titlebar = QHBoxLayout(); copy = QVBoxLayout(); copy.setSpacing(3); title = QLabel("我的声音"); title.setObjectName("pageTitle"); copy.addWidget(title); subtitle = QLabel("统一管理文字生成与 AI 翻唱音色能力。"); subtitle.setObjectName("pageSubtitle"); copy.addWidget(subtitle); titlebar.addLayout(copy, 1)
        self.new_voice = QPushButton("＋ 训练新声音"); self.new_voice.setObjectName("primaryButton"); self.new_voice.clicked.connect(lambda: self.retrain_requested.emit("")); titlebar.addWidget(self.new_voice)
        self.import_models = QPushButton("导入模型…"); self.import_models.setObjectName("secondaryButton"); self.import_models.clicked.connect(self._choose_model_files); titlebar.addWidget(self.import_models); root.addLayout(titlebar)
        workspace = QHBoxLayout(); workspace.setSpacing(14); library = QFrame(); library.setObjectName("voicesLibrary"); library_layout = QVBoxLayout(library); library_layout.setContentsMargins(16, 14, 16, 14); library_layout.setSpacing(10)
        toolbar = QHBoxLayout(); self.voice_search = QLineEdit(); self.voice_search.setObjectName("voicesSearch"); self.voice_search.setPlaceholderText("搜索声音"); self.voice_search.setClearButtonEnabled(True); self.voice_search.textChanged.connect(self.refresh); toolbar.addWidget(self.voice_search, 1); self.filter_buttons = {}
        for key, label in (("all", "全部"), ("tts", "文字生成"), ("cover", "AI 翻唱")):
            button = QPushButton(label); button.setObjectName("voiceFilter"); button.setCheckable(True); button.clicked.connect(lambda _checked=False, value=key: self._set_filter(value)); self.filter_buttons[key] = button; toolbar.addWidget(button)
        library_layout.addLayout(toolbar); self.scroll = QScrollArea(); self.scroll.setObjectName("voicesScroll"); self.scroll.setWidgetResizable(True); library_layout.addWidget(self.scroll, 1); workspace.addWidget(library, 1)
        self.detail = QFrame(); self.detail.setObjectName("voiceDetail"); self.detail.setFixedWidth(320); detail_layout = QVBoxLayout(self.detail); detail_layout.setContentsMargins(18, 18, 18, 18); detail_layout.setSpacing(11)
        detail_hero = QFrame(); detail_hero.setObjectName("voiceDetailHero"); hero_layout = QVBoxLayout(detail_hero); hero_layout.setContentsMargins(0, 0, 0, 12); hero_layout.setSpacing(5)
        self.detail_avatar = QLabel("◉"); self.detail_avatar.setObjectName("voiceDetailAvatar"); self.detail_avatar.setAlignment(Qt.AlignCenter); hero_layout.addWidget(self.detail_avatar, alignment=Qt.AlignCenter)
        self.detail_name = QLabel("选择一个声音"); self.detail_name.setObjectName("voiceDetailName"); self.detail_name.setAlignment(Qt.AlignCenter); hero_layout.addWidget(self.detail_name)
        self.detail_meta = QLabel("从声音库中选择卡片，可试听、生成或管理训练版本。"); self.detail_meta.setObjectName("cardSub"); self.detail_meta.setWordWrap(True); self.detail_meta.setAlignment(Qt.AlignCenter); hero_layout.addWidget(self.detail_meta); detail_layout.addWidget(detail_hero)
        stats = QWidget(); stats.setObjectName("voiceStats"); stats_layout = QGridLayout(stats); stats_layout.setContentsMargins(0, 0, 0, 0); stats_layout.setHorizontalSpacing(8); stats_layout.setVerticalSpacing(8); self.detail_stats = []
        for index, caption in enumerate(("授权素材", "可用时长", "文字生成", "AI 翻唱")):
            card = QFrame(); card.setObjectName("voiceStat"); card_layout = QVBoxLayout(card); card_layout.setContentsMargins(9, 8, 9, 8); card_layout.setSpacing(2); value = QLabel("—"); value.setObjectName("voiceStatValue"); caption_label = QLabel(caption); caption_label.setObjectName("voiceStatCaption"); card_layout.addWidget(value); card_layout.addWidget(caption_label); stats_layout.addWidget(card, index // 2, index % 2); self.detail_stats.append(value)
        detail_layout.addWidget(stats)
        capability_label = QLabel("能力状态"); capability_label.setObjectName("sectionLabel"); detail_layout.addWidget(capability_label); self.detail_capabilities = QLabel("尚未选择声音"); self.detail_capabilities.setObjectName("voiceCapabilities"); self.detail_capabilities.setWordWrap(True); detail_layout.addWidget(self.detail_capabilities); version_label = QLabel("模型版本"); version_label.setObjectName("sectionLabel"); detail_layout.addWidget(version_label); self.detail_versions = QLabel("没有可显示的版本记录"); self.detail_versions.setObjectName("voiceVersions"); self.detail_versions.setWordWrap(True); detail_layout.addWidget(self.detail_versions); detail_layout.addStretch(); self.detail_note = QLabel("所有操作均使用本地项目中的授权、素材和模型记录。"); self.detail_note.setObjectName("hint"); self.detail_note.setWordWrap(True); detail_layout.addWidget(self.detail_note); workspace.addWidget(self.detail); root.addLayout(workspace, 1); self.refresh()
    def _set_filter(self, value: str) -> None:
        self._filter = value; self.refresh()
    def refresh(self) -> None:
        self.release_resources()
        content = QWidget(); content.setObjectName("voiceGridContent"); layout = QGridLayout(content); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12); layout.setAlignment(Qt.AlignTop); profiles = [item for item in self.store.list_profiles(self.project) if not item.archived]
        query = self.voice_search.text().strip().casefold() if hasattr(self, "voice_search") else ""
        profiles = [item for item in profiles if not query or query in item.name.casefold()]
        if self._filter != "all":
            profiles = [item for item in profiles if (item.default_model_mode != "" if self._filter == "tts" else bool(item.singing_models))]
        for button_key, button in getattr(self, "filter_buttons", {}).items(): button.setChecked(button_key == self._filter)
        if not profiles:
            empty = QLabel("没有符合条件的声音。训练完成并确认授权后会显示在这里。"); empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignCenter); layout.addWidget(empty, 0, 0, 1, 2)
        for index, profile in enumerate(profiles):
            card = VoiceCard(self.store, self.project, profile); card.generate_requested.connect(self.generate_requested); card.retrain_requested.connect(self.retrain_requested); card.selected.connect(self._select_profile); card.changed.connect(self._changed); layout.addWidget(card, index // 2, index % 2)
        if profiles:
            if not any(item.id == self._selected_profile_id for item in profiles): self._selected_profile_id = profiles[0].id
            self._select_profile(self._selected_profile_id)
        else:
            self._selected_profile_id = ""; self.detail_name.setText("选择一个声音"); self.detail_meta.setText("从声音库中选择卡片，可试听、生成或管理训练版本。"); self.detail_capabilities.setText("尚未选择声音"); self.detail_versions.setText("没有可显示的版本记录")
            for stat in self.detail_stats: stat.setText("—")
        self.scroll.setWidget(content)
    def _select_profile(self, profile_id: str) -> None:
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id and not item.archived), None)
        if profile is None: return
        self._selected_profile_id = profile.id
        assets = [item for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
        seconds = sum(item.duration_seconds for item in assets)
        self.detail_name.setText(profile.name)
        self.detail_meta.setText(f"{len(assets)} 个授权素材 · {seconds:.1f} 秒\n创建于 {format_timestamp(profile.created_at)}")
        tts = "可用" if profile.active_model_version_id else "未完成训练"
        try: singing = "可用" if profile.singing_status(self.project) == "ready" else "未就绪"
        except TypeError: singing = "可用" if profile.singing_status() == "ready" else "未就绪"
        authorized = "已确认" if profile.consent_confirmed else "未确认"
        self.detail_capabilities.setText(f"授权：{authorized}\n文字生成：{tts}\nAI 翻唱：{singing}")
        for stat, value in zip(self.detail_stats, (str(len(assets)), f"{seconds:.0f} 秒", tts, singing)):
            stat.setText(value)
        versions = []
        if profile.model_versions: versions.append("文字生成 · " + profile.model_versions[-1].name)
        if profile.singing_models:
            active = next((item for item in profile.singing_models if item.id == profile.active_singing_model_id), profile.singing_models[-1])
            versions.append(f"AI 翻唱 · {active.engine} · {active.trust_status}")
        self.detail_versions.setText("\n".join(versions) if versions else "尚无已保存的模型版本")
    def _changed(self) -> None: self.refresh(); self.profiles_changed.emit()

    def _choose_model_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择待登记的本地模型文件", str(self.project), "声音模型 (*.pth *.ckpt *.pt *.index);;所有文件 (*)")
        if not paths:
            return
        self.detail_note.setText(f"已选择 {len(paths)} 个本地模型文件。当前版本不直接启用未经登记的文件；请在训练流程中完成授权、版本和适用能力确认。")

    def release_resources(self) -> None:
        for card in self.scroll.findChildren(VoiceCard): card.release_resources()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources(); super().closeEvent(event)


class OneClickGeneratePage(QWidget):
    job_created = Signal(object); train_requested = Signal();
    def __init__(self, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.store, self.project, self.client = store, project, client; self.pending: dict[str, tuple[str, Job, dict]] = {}; self.output_audio = QAudioOutput(self); self.player = QMediaPlayer(self); self.player.setAudioOutput(self.output_audio); self._build(); self.refresh_profiles(); self.refresh_history(); client.event.connect(self._event)
    def _build(self) -> None:
        self.setObjectName("ttsPage")
        root = QVBoxLayout(self); root.setContentsMargins(24, 20, 24, 18); root.setSpacing(14)
        titlebar = QHBoxLayout(); copy = QVBoxLayout(); copy.setSpacing(3)
        title = QLabel("文字生成"); title.setObjectName("pageTitle"); copy.addWidget(title)
        subtitle = QLabel("使用已训练声音快速生成自然语音，支持情绪、语速与停顿控制。"); subtitle.setObjectName("pageSubtitle"); copy.addWidget(subtitle); titlebar.addLayout(copy, 1)
        new_document = QPushButton("新建文稿"); new_document.setObjectName("secondaryButton"); new_document.clicked.connect(lambda: self.text.clear()); titlebar.addWidget(new_document)
        template = QPushButton("模板"); template.setObjectName("secondaryButton"); template.clicked.connect(lambda: self.text.setPlainText("夜色慢慢落下来，城市的灯一盏一盏亮起。\n\n我想把今天发生的故事，慢一点讲给你听。")); titlebar.addWidget(template); root.addLayout(titlebar)

        self.empty = QFrame(); self.empty.setObjectName("ttsEmpty")
        empty_layout = QVBoxLayout(self.empty); empty_label = QLabel("还没有可用于文字生成的已授权声音"); empty_label.setObjectName("emptyState"); empty_label.setAlignment(Qt.AlignCenter); train = QPushButton("去训练声音"); train.setObjectName("primaryButton"); train.clicked.connect(self.train_requested); empty_layout.addStretch(); empty_layout.addWidget(empty_label); empty_layout.addWidget(train, alignment=Qt.AlignCenter); empty_layout.addStretch(); root.addWidget(self.empty, 1)

        self.form = QWidget(); self.form.setObjectName("ttsWorkspace"); layout = QHBoxLayout(self.form); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(14)
        voice_panel = QFrame(); voice_panel.setObjectName("ttsVoicePanel"); voice_panel.setFixedWidth(235); voices = QVBoxLayout(voice_panel); voices.setContentsMargins(14, 14, 14, 14); voices.setSpacing(9)
        voice_title = QLabel("声音"); voice_title.setObjectName("cardTitle"); voices.addWidget(voice_title)
        voice_subtitle = QLabel("选择已验证的生成音色"); voice_subtitle.setObjectName("cardSub"); voices.addWidget(voice_subtitle)
        self.voice_search = QLineEdit(); self.voice_search.setObjectName("ttsVoiceSearch"); self.voice_search.setPlaceholderText("搜索声音"); self.voice_search.setClearButtonEnabled(True); self.voice_search.textChanged.connect(self._filter_profiles); voices.addWidget(self.voice_search)
        self.profile = QComboBox(self); self.profile.setObjectName("ttsVoicePicker"); self.profile.hide()
        self.profile.currentIndexChanged.connect(lambda _index: self._populate_voice_rows())
        self.voice_list = QListWidget(); self.voice_list.setObjectName("ttsVoiceList"); self.voice_list.setSpacing(4); self.voice_list.currentRowChanged.connect(self._select_voice_row); voices.addWidget(self.voice_list, 1)
        voice_note = QLabel("声音只会在授权记录、参考素材和训练模型均完整时出现在这里。"); voice_note.setObjectName("hint"); voice_note.setWordWrap(True); voices.addWidget(voice_note); layout.addWidget(voice_panel)

        editor = QVBoxLayout(); editor.setSpacing(14)
        script_card = QFrame(); script_card.setObjectName("ttsScriptCard"); script = QVBoxLayout(script_card); script.setContentsMargins(16, 14, 16, 14); script.setSpacing(10)
        script_head = QHBoxLayout(); script_copy = QVBoxLayout(); script_label = QLabel("生成文本"); script_label.setObjectName("cardTitle"); script_copy.addWidget(script_label); script_sub = QLabel("支持长文本自动分段"); script_sub.setObjectName("cardSub"); script_copy.addWidget(script_sub); script_head.addLayout(script_copy, 1)
        clear = QPushButton("清空"); clear.setObjectName("miniButton"); clear.clicked.connect(lambda: self.text.clear()); script_head.addWidget(clear); script.addLayout(script_head)
        self.text = QPlainTextEdit(); self.text.setObjectName("ttsScriptText"); self.text.setPlaceholderText("输入要生成的文字，支持中文和中英混合长文本……"); self.text.setMinimumHeight(185); self.text.textChanged.connect(self._update_text_info); script.addWidget(self.text)
        foot = QHBoxLayout(); self.text_info = QLabel("0 字 · 等待输入"); self.text_info.setObjectName("hint"); foot.addWidget(self.text_info); foot.addStretch(); shortcut = QLabel("Ctrl + Enter 生成"); shortcut.setObjectName("hint"); foot.addWidget(shortcut); script.addLayout(foot); editor.addWidget(script_card)
        preview = QFrame(); preview.setObjectName("ttsPreviewCard"); preview_layout = QHBoxLayout(preview); preview_layout.setContentsMargins(0, 0, 0, 0); preview_layout.setSpacing(0)
        preview_main = QWidget(); preview_main.setObjectName("ttsPreviewMain"); preview_main_layout = QVBoxLayout(preview_main); preview_main_layout.setContentsMargins(16, 14, 16, 14); preview_main_layout.setSpacing(9)
        preview_title = QLabel("最近生成预览"); preview_title.setObjectName("sectionLabel"); preview_main_layout.addWidget(preview_title)
        self.preview_wave = TtsPreviewWave(); self.preview_wave.setObjectName("ttsPreviewWave"); self.preview_wave.setMinimumHeight(72); preview_main_layout.addWidget(self.preview_wave)
        self.progress = QProgressBar(); self.progress.setObjectName("ttsProgress"); self.progress.setRange(0, 100); preview_main_layout.addWidget(self.progress)
        self.generation_status = QLabel("等待生成"); self.generation_status.setObjectName("ttsGenerationStatus"); self.generation_status.setWordWrap(True); preview_main_layout.addWidget(self.generation_status)
        preview_actions = QHBoxLayout(); self.preview_play = QPushButton("试听"); self.preview_play.setObjectName("miniButton"); self.preview_play.clicked.connect(self._toggle_preview_playback); preview_actions.addWidget(self.preview_play); self.cancel_generation = QPushButton("取消生成"); self.cancel_generation.setObjectName("secondaryButton"); self.cancel_generation.clicked.connect(self._cancel_generation); preview_actions.addWidget(self.cancel_generation); preview_actions.addStretch(); preview_main_layout.addLayout(preview_actions); preview_layout.addWidget(preview_main, 1)
        history_panel = QFrame(); history_panel.setObjectName("ttsHistoryMini"); history_layout = QVBoxLayout(history_panel); history_layout.setContentsMargins(13, 14, 13, 14); history_layout.setSpacing(8); history_title = QLabel("生成历史"); history_title.setObjectName("sectionLabel"); history_layout.addWidget(history_title); self.history = QScrollArea(); self.history.setObjectName("ttsHistory"); self.history.setWidgetResizable(True); self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.history.setMinimumWidth(178); history_layout.addWidget(self.history, 1); open_folder = QPushButton("打开输出文件夹"); open_folder.setObjectName("miniButton"); open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.output.text()))); history_layout.addWidget(open_folder); preview_layout.addWidget(history_panel); editor.addWidget(preview, 1); layout.addLayout(editor, 1)

        right = QWidget(); right.setObjectName("ttsRightColumn"); right.setFixedWidth(320)
        right_layout = QVBoxLayout(right); right_layout.setContentsMargins(0, 0, 0, 0); right_layout.setSpacing(14)
        parameters = QFrame(); parameters.setObjectName("ttsParameters"); params = QVBoxLayout(parameters); params.setContentsMargins(15, 15, 15, 15); params.setSpacing(10)
        right_layout.addWidget(parameters)
        params.addWidget(QLabel("表达方式", objectName="sectionLabel"))
        self.emotion = QComboBox(self); self.emotion.addItems(("自然", "温柔", "活泼", "沉稳", "开心", "低语")); self.emotion.hide()
        emotion_grid = QGridLayout(); emotion_grid.setSpacing(7); self.emotion_buttons = []
        for index in range(self.emotion.count()):
            button = QPushButton(self.emotion.itemText(index)); button.setObjectName("ttsEmotionButton")
            button.setCheckable(True); button.setChecked(index == 0); button.setFixedHeight(34)
            button.clicked.connect(lambda _checked=False, value=index: self._select_emotion(value))
            emotion_grid.addWidget(button, index // 3, index % 3); self.emotion_buttons.append(button)
        params.addLayout(emotion_grid)
        self.emotion.currentIndexChanged.connect(self._sync_emotion_buttons)
        self.emotion_notice = QLabel(); self.emotion_notice.setObjectName("ttsEmotionNotice"); self.emotion_notice.setWordWrap(True); self.emotion_notice.hide(); params.addWidget(self.emotion_notice)
        self.speed = QDoubleSpinBox(self); self.speed.setRange(.6, 1.6); self.speed.setValue(1); self.speed.setSingleStep(.05); self.speed.hide()
        self.speed_slider, self.speed_label = self._parameter_slider(params, "语速", self.speed, "×")
        self.pitch = QDoubleSpinBox(self); self.pitch.setRange(-6, 6); self.pitch.setDecimals(0); self.pitch.hide()
        self.pitch_slider, self.pitch_label = self._parameter_slider(params, "音高", self.pitch, "")
        self.pitch_slider.setSingleStep(100); self.pitch_slider.setPageStep(100)
        self.pitch_notice = QLabel("音高调整尚未接入文字生成引擎；保持 0 可正常生成。")
        self.pitch_notice.setObjectName("ttsEmotionNotice"); self.pitch_notice.setWordWrap(True); self.pitch_notice.hide(); params.addWidget(self.pitch_notice)
        self.pitch.valueChanged.connect(lambda value: (self.pitch_notice.setText("视觉预览 · 仅展示音高参数" if getattr(self, "ui_preview", False) else "音高调整尚未接入文字生成引擎；保持 0 可正常生成。"), self.pitch_notice.setVisible(value != 0)))
        self.pause = QDoubleSpinBox(self); self.pause.setRange(.05, 2); self.pause.setValue(.3); self.pause.setSingleStep(.05); self.pause.hide()
        self.pause_slider, self.pause_label = self._parameter_slider(params, "停顿", self.pause, "秒")
        params.addWidget(QLabel("语言", objectName="sectionLabel")); self.language = QComboBox(); self.language.addItem("自动识别中英混合", "auto"); params.addWidget(self.language)
        output_card = QFrame(); output_card.setObjectName("ttsOutputCard"); output_layout = QVBoxLayout(output_card); output_layout.setContentsMargins(15, 15, 15, 15); output_layout.setSpacing(10)
        output_layout.addWidget(QLabel("输出", objectName="sectionLabel")); self.output = QLineEdit(str(self.store.get_setting("default_output_dir", str(self.project / "exports")))); self.output.setReadOnly(True); output_layout.addWidget(self.output); browse = QPushButton("选择输出目录…"); browse.setObjectName("secondaryButton"); browse.clicked.connect(self._browse); output_layout.addWidget(browse)
        right_layout.addWidget(output_card)
        self.seed = QSpinBox(); self.seed.setRange(-1, 2147483647); self.seed.setValue(-1); self.seed.hide(); params.addWidget(self.seed)
        right_layout.addStretch()
        generate_card = QFrame(); generate_card.setObjectName("ttsGenerateCard"); generate_layout = QVBoxLayout(generate_card); generate_layout.setContentsMargins(15, 15, 15, 15)
        self.generate = QPushButton("生成语音"); self.generate.setObjectName("primaryButton"); self.generate.setMinimumHeight(44); self.generate.clicked.connect(self._generate); generate_layout.addWidget(self.generate); right_layout.addWidget(generate_card); layout.addWidget(right)
        self._cancel_requested = set(); root.addWidget(self.form, 1)
    def _select_emotion(self, index):
        self.emotion.setCurrentIndex(index)
        self._sync_emotion_buttons(index)

    def _sync_emotion_buttons(self, index):
        for number, button in enumerate(self.emotion_buttons):
            button.setChecked(number == index)
        visual = getattr(self, "ui_preview", False)
        self.emotion_notice.setText("视觉预览 · 仅展示表达方式选中状态" if visual else "当前文字生成引擎未接入情绪控制，请选择自然，或在计算与模型设置中查看引擎配置。")
        self.emotion_notice.setVisible(index != 0)

    def _parameter_slider(self, layout, title, value, suffix):
        row = QHBoxLayout(); row.setSpacing(7)
        label = QLabel(title); label.setObjectName("ttsParameterName"); label.setFixedWidth(40)
        row.addWidget(label)
        slider = QSlider(Qt.Horizontal); slider.setObjectName("ttsParameterSlider")
        slider.setAccessibleName(title)
        slider.setRange(round(value.minimum() * 100), round(value.maximum() * 100))
        slider.setSingleStep(5); slider.setPageStep(10)
        display = QLabel(); display.setObjectName("ttsParameterValue"); display.setFixedWidth(50); display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        def sync(number):
            slider.blockSignals(True)
            slider.setValue(round(number * 100))
            slider.blockSignals(False)
            display.setText(f"{number:.0f}" if title == "音高" else f"{number:.2f}{suffix}")
        slider.valueChanged.connect(lambda number: value.setValue(number / 100))
        value.valueChanged.connect(sync)
        sync(value.value())
        row.addWidget(slider, 1); row.addWidget(display); layout.addLayout(row)
        return slider, display

    def refresh_profiles(self, selected_id: str = "") -> None:
        current = selected_id or self.profile.currentData() if hasattr(self, "profile") else selected_id; self.profiles = []
        for profile in self.store.list_profiles(self.project):
            if profile.archived: continue
            refs = [item for item in profile.reference_assets if item.approved and item.transcript.strip() and Path(item.path).is_file()]
            tuned = bool(profile.active_gpt_checkpoint and profile.active_sovits_checkpoint and Path(profile.active_gpt_checkpoint).is_file() and Path(profile.active_sovits_checkpoint).is_file())
            if refs and tuned and profile.consent_confirmed and profile.consent_record.strip() and profile.consent_confirmed_at:
                self.profiles.append(profile)
        self.empty.setVisible(not self.profiles); self.form.setVisible(bool(self.profiles))
        self._populate_profile_picker(current)

    def _filter_profiles(self, _value: str = "") -> None:
        if hasattr(self, "profiles"):
            self._populate_profile_picker(self.profile.currentData())

    def _populate_profile_picker(self, selected_id: str = "") -> None:
        query = self.voice_search.text().strip().casefold() if hasattr(self, "voice_search") else ""
        current = str(selected_id or "")
        self.profile.blockSignals(True); self.profile.clear()
        for profile in self.profiles:
            if not query or query in profile.name.casefold():
                self.profile.addItem(profile.name, profile.id)
        if current:
            index = self.profile.findData(current)
            if index >= 0:
                self.profile.setCurrentIndex(index)
        self.profile.blockSignals(False)
        self._populate_voice_rows()

    def _populate_voice_rows(self) -> None:
        if not hasattr(self, "voice_list"):
            return
        selected_id = self.profile.currentData()
        self.voice_list.blockSignals(True)
        self.voice_list.clear()
        selected_row = -1
        for index in range(self.profile.count()):
            title = self.profile.itemText(index)
            profile_id = self.profile.itemData(index)
            subtitle = "视觉样例 · 不执行生成" if getattr(self, "ui_preview", False) else "已训练 · 可文字生成"
            item = QListWidgetItem(f"{title}\n{subtitle}")
            item.setData(Qt.UserRole, profile_id)
            self.voice_list.addItem(item)
            if profile_id == selected_id:
                selected_row = index
        if selected_row >= 0:
            self.voice_list.setCurrentRow(selected_row)
        self.voice_list.blockSignals(False)

    def _select_voice_row(self, row: int) -> None:
        if row >= 0 and row != self.profile.currentIndex():
            self.profile.setCurrentIndex(row)
    def select_profile(self, profile_id: str) -> None: self.refresh_profiles(profile_id)
    def _update_text_info(self) -> None:
        characters, segments = estimate_text_work(self.text.toPlainText(), 120); self.text_info.setText(f"{characters:,} 字 · 将自动分成 {segments} 段" if segments else "0 字 · 等待输入")
    def _browse(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if value: self.output.setText(value); self.store.set_setting("default_output_dir", value)
    def _generate(self) -> None:
        job = None
        try:
            if self.pending: raise ValueError("已有生成任务正在执行，请等待或取消当前任务")
            if self.pitch.value() != 0:
                raise ValueError("音高调整尚未接入文字生成引擎，请将音高恢复为 0；可在设置 → 计算与模型查看引擎配置。")
            if self.emotion.currentIndex() != 0:
                raise ValueError("当前文字生成引擎未接入情绪控制，请选择自然后生成；可在设置 → 计算与模型查看引擎配置。")
            if not self.profiles: raise ValueError("请先训练一个可用声音")
            text = self.text.toPlainText().strip()
            if not text: raise ValueError("请输入要生成的文字")
            profile = next((item for item in self.profiles if item.id == self.profile.currentData()), None)
            if profile is None:
                raise ValueError("请选择搜索结果中的可用声音")
            current = next((item for item in self.store.list_profiles(self.project) if item.id == profile.id), None)
            if current is None or current.archived or not current.consent_confirmed or not current.consent_record.strip() or not current.consent_confirmed_at:
                raise ValueError("声音授权已失效，请重新确认素材与声音授权")
            if not current.active_gpt_checkpoint or not current.active_sovits_checkpoint or not Path(current.active_gpt_checkpoint).is_file() or not Path(current.active_sovits_checkpoint).is_file():
                raise ValueError("声音尚未完成训练，或模型文件已缺失，请重新训练或恢复版本")
            profile = current
            ref = next((item for item in profile.reference_assets if item.approved and item.transcript.strip() and Path(item.path).is_file()), None)
            if ref is None: raise ValueError("声音缺少已审核的参考音频与文本，请追加或修复素材")
            payload = {"text": text, "text_lang": self.language.currentData(), "ref_audio_path": ref.path, "prompt_text": ref.transcript, "prompt_lang": ref.language, "output_dir": self.output.text(), "speed_factor": self.speed.value(), "fragment_interval": self.pause.value(), "seed": self.seed.value(), "max_chars": 120, "profile_id": profile.id}
            job = Job(JobKind.SYNTHESIZE, payload); self.store.save_job(job); self.job_created.emit(job)
            profile_payload = profile.to_dict(); profile_payload["project_path"] = str(self.project)
            request = self.client.send("load_profile", profile_payload); self.pending[request] = ("load", job, payload); self.generate.setEnabled(False); self.progress.setValue(0)
            job.payload["active_request_id"] = request; self.store.save_job(job)
            self.generation_status.setText("正在加载声音模型…")
        except Exception as exc:
            if job is not None:
                job.status = JobStatus.FAILED; job.error = str(exc); self.store.save_job(job)
            show_error(self, str(exc))

    def _toggle_preview_playback(self) -> None:
        source = self.player.source()
        if not source.isLocalFile() or not Path(source.toLocalFile()).is_file():
            self.generation_status.setText("没有可试听的真实本地输出")
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.preview_play.setText("试听")
        else:
            self.player.play()
            self.preview_play.setText("暂停")

    def _cancel_generation(self):
        if not self.pending:
            self.generation_status.setText("没有正在执行的生成任务"); return
        for request, (stage, job, _payload) in tuple(self.pending.items()):
            try:
                if stage != "load": self.client.send("cancel", {"target_request_id": request})
                self._cancel_requested.add(job.id)
                self.generation_status.setText("正在取消，等待 Worker 确认…")
            except (RuntimeError, ValueError) as exc:
                show_error(self, str(exc))
    def _event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id not in self.pending: return
        stage, job, synthesis = self.pending[request_id]
        if event == "progress": job.status = JobStatus.RUNNING; job.progress = float(payload.get("progress", 0)); job.message = str(payload.get("message", "")); self.progress.setValue(round(job.progress * 100)); self.store.save_job(job); return
        if event == "error":
            self.pending.pop(request_id, None)
            cancelled = bool(payload.get("cancelled")) or payload.get("status") == "cancelled" or (stage == "load" and job.id in self._cancel_requested)
            job.status = JobStatus.CANCELLED if cancelled else JobStatus.FAILED
            job.error = str(payload.get("message", "")); self.store.save_job(job)
            self._cancel_requested.discard(job.id); self.generate.setEnabled(True); self.refresh_history()
            self.generation_status.setText("已取消" if cancelled else "生成失败：" + job.error)
            if not cancelled: show_error(self, job.error)
            return
        if event != "result": return
        self.pending.pop(request_id, None)
        if stage == "load":
            if job.id in self._cancel_requested:
                self._cancel_requested.discard(job.id); job.status = JobStatus.CANCELLED; self.store.save_job(job)
                self.generate.setEnabled(True); self.generation_status.setText("已取消"); self.refresh_history(); return
            try: request = self.client.send("synthesize", synthesis)
            except (RuntimeError, ValueError) as exc:
                job.status = JobStatus.FAILED; job.error = str(exc); self.store.save_job(job)
                self.generate.setEnabled(True); self.generation_status.setText("生成失败：" + str(exc)); show_error(self, str(exc)); return
            self.pending[request] = ("synth", job, synthesis)
            job.payload["active_request_id"] = request; self.store.save_job(job); return
        outputs = list(payload.get("outputs", []))
        if not outputs or any(not Path(value).is_file() or Path(value).stat().st_size == 0 for value in outputs):
            job.status = JobStatus.FAILED; job.error = "Worker 返回的输出不存在或为空，未标记为生成成功"; self.store.save_job(job)
            self.generate.setEnabled(True); self.generation_status.setText(job.error); self.refresh_history(); return
        job.status = JobStatus.COMPLETED; job.progress = 1; job.outputs = outputs; self.store.save_job(job); self.progress.setValue(100); self.generate.setEnabled(True)
        wav = next((item for item in job.outputs if item.lower().endswith(".wav") and Path(item).is_file()), "")
        self._cancel_requested.discard(job.id)
        self.generation_status.setText("生成完成：" + "、".join(job.outputs))
        record = StoredGenerationRecord(
            project_uid=self.store.load_project(self.project)["project_uid"],
            voice_profile_id=str(job.payload.get("profile_id", "")), text=str(job.payload.get("text", "")),
            parameters={key: value for key, value in job.payload.items() if key != "active_request_id"},
            wav_path=wav, mp3_path=next((item for item in job.outputs if item.lower().endswith(".mp3")), ""),
            status="completed", id=job.id,
        )
        self.store.save_generation_record(self.project, record)
        if wav:
            self.player.setSource(QUrl.fromLocalFile(wav))
            if self.store.get_setting("generation.autoplay", True) and not getattr(self, "_background_project", False): self.player.play()
        self.refresh_history()
    def refresh_history(self) -> None:
        for card in self.history.findChildren(GenerationRecordCard): card.release_resources()
        content = QWidget(); layout = QVBoxLayout(content); names = {item.id: item.name for item in self.store.list_profiles(self.project)}; count = 0
        for job in self.store.list_jobs(80):
            if job.kind != JobKind.SYNTHESIZE or str(job.payload.get("profile_id", "")) not in names: continue
            record = GenerationRecord.from_job(job, names[str(job.payload.get("profile_id"))]); card = GenerationHistoryMiniCard(record); card.retry_requested.connect(self._retry); layout.addWidget(card); count += 1
            if count >= 5: break
        if not count:
            empty = QLabel("生成完成后会在这里保留文字、声音和试听记录"); empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignCenter); empty.setWordWrap(True); layout.addWidget(empty)
        layout.addStretch(); self.history.setWidget(content)
    def _retry(self, job: Job) -> None:
        self.text.setPlainText(str(job.payload.get("text", ""))); index = self.profile.findData(str(job.payload.get("profile_id", "")))
        if index < 0:
            show_error(self, "原任务的声音已不可用，请重新训练或恢复已授权的模型版本。"); return
        self.profile.setCurrentIndex(index)
        self.output.setText(str(job.payload.get("output_dir", self.output.text())))
        self.pause.setValue(float(job.payload.get("fragment_interval", .3)))
        self.seed.setValue(int(job.payload.get("seed", -1)))
        self.speed.setValue(float(job.payload.get("speed_factor", 1.0))); self._generate()

    def release_resources(self) -> None:
        self.player.stop(); self.player.setSource(QUrl())
        for card in self.history.findChildren(GenerationRecordCard): card.release_resources()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources(); super().closeEvent(event)


class TaskCenterDialog(QDialog):
    retry_generation_requested = Signal(object)
    def __init__(self, store: StudioStore, client=None, parent=None):
        super().__init__(parent); self.store = store; self.client = client; self._rows = []; self._queued = {}; self.setObjectName("taskCenterDialog"); self.setWindowTitle("任务中心"); self.resize(880, 520); layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 20); layout.setSpacing(12)
        toolbar = QHBoxLayout(); task_title = QLabel("GPU 任务与本地处理历史"); task_title.setObjectName("taskCenterTitle"); toolbar.addWidget(task_title); toolbar.addStretch(); refresh = QPushButton("刷新"); refresh.clicked.connect(self.refresh); toolbar.addWidget(refresh); retry = QPushButton("重试选中任务"); retry.clicked.connect(self.retry_selected); toolbar.addWidget(retry); cancel = QPushButton("取消选中任务"); cancel.clicked.connect(self.cancel_selected); toolbar.addWidget(cancel); layout.addLayout(toolbar)
        # The standalone visual preview must never imply that a model job has
        # run.  Keep that distinction in the dialog itself, where the HTML
        # reference otherwise uses populated example task rows.
        if getattr(parent, "ui_preview", False):
            preview_note = QLabel("视觉预览 · 此窗口未连接本地 Worker，因此不显示演示任务或输出文件。")
            preview_note.setObjectName("taskPreviewNote")
            preview_note.setWordWrap(True)
            layout.addWidget(preview_note)
        self.table = QTableWidget(0, 7); self.table.setHorizontalHeaderLabels(["时间", "类型", "状态", "进度", "当前阶段", "说明 / 失败原因", "输出位置"]); self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch); self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch); self.table.cellDoubleClicked.connect(self._open); layout.addWidget(self.table);
        self.empty_state = QFrame(); self.empty_state.setObjectName("taskEmptyState")
        empty_layout = QVBoxLayout(self.empty_state); empty_layout.setAlignment(Qt.AlignCenter); empty_layout.setSpacing(6)
        empty_title = QLabel("暂无可显示的本地任务" if getattr(parent, "ui_preview", False) else "暂无本地任务")
        empty_title.setObjectName("taskEmptyTitle"); empty_title.setAlignment(Qt.AlignCenter); empty_layout.addWidget(empty_title)
        empty_text = QLabel("视觉预览不会创建处理记录。" if getattr(parent, "ui_preview", False) else "开始生成、训练、翻唱或分离后，进度与本地日志会显示在这里。")
        empty_text.setObjectName("taskEmptyText"); empty_text.setAlignment(Qt.AlignCenter); empty_layout.addWidget(empty_text)
        layout.addWidget(self.empty_state, 1)
        actions = QHBoxLayout()
        for label, callback in (("查看详情", self.show_details), ("打开输出目录", self.open_selected), ("删除历史记录", self.delete_selected)):
            button = QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(); layout.addLayout(actions)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        if self.client is not None: self.client.event.connect(self._worker_event)
        self.refresh_timer = QTimer(self); self.refresh_timer.setInterval(1000); self.refresh_timer.timeout.connect(self.refresh); self.refresh_timer.start()
        self.refresh()
    def refresh(self) -> None:
        selected = self._selected_id(False)
        product_jobs = self.store.list_product_jobs()
        legacy_jobs = self.store.list_jobs()
        self._jobs = {job.id: job for job in (*product_jobs, *legacy_jobs)}
        self._product_ids = {job.id for job in product_jobs}
        rows = []
        for job in product_jobs:
            stage = job.current_stage or next((item.name for item in job.stages if item.status.value in {"running", "preparing", "cancelling"}), "-")
            rows.append((job.id, job.updated_at, job.kind, job.status.value, f"{job.progress * 100:.0f}%", stage, job.error, "、".join(job.outputs)))
        for job in legacy_jobs:
            if str(job.kind).startswith("product:"):
                continue
            rows.append((job.id, job.updated_at, job.kind.value, job.status.value, f"{job.progress * 100:.0f}%", "-", job.error or job.message, "、".join(job.outputs)))
        for request_id, item in self._queued.items():
            rows.append((request_id, item.get("time", ""), item.get("command", "GPU"), "queued", "0%", f"GPU 队列第 {item.get('queue_position', '?')} 位", "等待 GPU 任务槽位", ""))
        rows.sort(key=lambda item: item[1], reverse=True)
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(str(values[1])))
            for column, value in enumerate(values[2:], 1):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self._rows = rows
        self.table.setVisible(bool(rows))
        self.empty_state.setVisible(not rows)
        for row, values in enumerate(rows):
            if values[0] == selected: self.table.selectRow(row); break
    def _worker_event(self, request_id: str, event: str, payload: dict) -> None:
        if event == "queued":
            self._queued[str(request_id)] = {**payload, "time": utc_now()}
            self.refresh()
        elif str(request_id) in self._queued and event in {"result", "error"}:
            self._queued.pop(str(request_id), None)
            self.refresh()
    def retry_selected(self) -> None:
        job_id = self._selected_id()
        if not job_id: return
        try:
            job = self._jobs.get(job_id)
            if job is None or job.status.value not in {"recoverable", "failed", "interrupted"}:
                raise ValueError("仅失败或中断的任务可以重试")
            if self.client is None: raise RuntimeError("未连接本地 Worker，请从主窗口打开任务中心")
            if job_id in self._product_ids:
                self.client.retry_product_job(job_id)
            elif job.kind == JobKind.SYNTHESIZE and self.receivers("2retry_generation_requested(PyObject)"):
                self.retry_generation_requested.emit(job)
                self.accept()
            else:
                raise RuntimeError("此任务需要原始工作流上下文，请打开训练声音页面，从对应声音的未完成工作流继续")
        except (RuntimeError, KeyError, ValueError) as exc:
            QMessageBox.warning(self, "无法重试任务", str(exc))
        self.refresh()

    def cancel_selected(self) -> None:
        job_id = self._selected_id()
        if not job_id: return
        try:
            if self.client is None: raise RuntimeError("未连接本地 Worker")
            job = self._jobs.get(job_id)
            if job_id in self._queued: request_id = job_id
            else:
                if job is None or job.status.value not in {"queued", "preparing", "running", "waiting_dependency", "cancelling"}:
                    raise ValueError("任务已经结束，不能取消")
                request_id = str(job.payload.get("active_request_id", ""))
                for controller in getattr(self.client, "_pipeline_controllers", ()):
                    if controller.job.id == job_id:
                        request_id = controller.pipeline.request_ids.get(controller.job.current_stage, request_id)
            if not request_id: raise RuntimeError("没有找到活动请求；请在原始工作流中取消，或重新检测 Worker 状态")
            self.client.send("cancel", {"target_request_id": request_id})
        except (RuntimeError, KeyError, ValueError) as exc:
            QMessageBox.warning(self, "无法取消任务", str(exc))
        self.refresh()

    def _open(self, row: int, _column: int) -> None:
        if row < 0 or row >= len(self._rows): return
        job = self._jobs.get(self._rows[row][0])
        target = next((Path(value) for value in job.outputs if Path(value).exists()), None) if job else None
        if target is None:
            QMessageBox.information(self, "暂无输出", "任务没有仍然存在的输出文件，可查看详情中的失败原因和原始输出路径。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent if target.is_file() else target))):
            QMessageBox.warning(self, "无法打开目录", str(target))

    def _selected_id(self, notify=True):
        row = self.table.currentRow()
        if 0 <= row < len(self._rows): return self._rows[row][0]
        if notify: QMessageBox.information(self, "选择任务", "请先选中一条任务记录。")
        return ""

    def open_selected(self):
        if self._selected_id(): self._open(self.table.currentRow(), 6)

    def show_details(self):
        job_id = self._selected_id()
        if not job_id: return
        import json
        job = self._jobs.get(job_id)
        dialog = QDialog(self); dialog.setWindowTitle("任务详情 · " + job_id); dialog.resize(760, 560)
        layout = QVBoxLayout(dialog); detail = QPlainTextEdit(); detail.setReadOnly(True)
        detail.setPlainText(json.dumps(job.to_dict() if job else self._queued.get(job_id, {}), ensure_ascii=False, indent=2))
        layout.addWidget(detail); close = QPushButton("关闭"); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec()

    def delete_selected(self):
        job_id = self._selected_id()
        if not job_id: return
        if QMessageBox.question(self, "删除历史记录", "只删除这条任务记录，保留项目、模型和全部输出文件。") != QMessageBox.Yes: return
        try: self.store.delete_job_history(job_id)
        except (KeyError, ValueError) as exc: QMessageBox.warning(self, "无法删除记录", str(exc))
        self.refresh()

    def done(self, result):
        self.refresh_timer.stop()
        if self.client is not None:
            try: self.client.event.disconnect(self._worker_event)
            except (RuntimeError, TypeError): pass
        super().done(result)


class TaskDrawer(QFrame):
    """Compact task history surface used by the top-bar GPU control.

    The full task-center dialog remains available for row selection, retry,
    cancellation and file actions.  The drawer mirrors the HTML reference's
    glanceable task cards and always reads the same local task records.
    """

    full_center_requested = Signal()

    def __init__(self, store: StudioStore, client=None, parent=None):
        super().__init__(parent); self.store = store; self.client = client
        self.setObjectName("taskDrawer"); self.setFixedWidth(360); self.setMinimumHeight(280)
        layout = QVBoxLayout(self); layout.setContentsMargins(14, 14, 14, 14); layout.setSpacing(10)
        header = QHBoxLayout(); title = QLabel("GPU 任务中心"); title.setObjectName("taskDrawerTitle"); header.addWidget(title)
        header.addStretch(); refresh = QPushButton("刷新"); refresh.setObjectName("taskDrawerButton"); refresh.clicked.connect(self.refresh); header.addWidget(refresh)
        close = QPushButton("关闭"); close.setObjectName("taskDrawerButton"); close.clicked.connect(self.hide); header.addWidget(close); layout.addLayout(header)
        subtitle = QLabel("GPU、生成、训练与导出均保存在本机。")
        subtitle.setObjectName("taskDrawerSubtitle"); subtitle.setWordWrap(True); layout.addWidget(subtitle)
        self.scroll = QScrollArea(); self.scroll.setObjectName("taskDrawerScroll"); self.scroll.setWidgetResizable(True)
        self.cards = QWidget(); self.cards.setObjectName("taskDrawerCards"); self.cards_layout = QVBoxLayout(self.cards); self.cards_layout.setContentsMargins(0, 0, 0, 0); self.cards_layout.setSpacing(8); self.scroll.setWidget(self.cards); layout.addWidget(self.scroll, 1)
        full = QPushButton("打开完整任务中心"); full.setObjectName("taskDrawerFullButton"); full.clicked.connect(self.full_center_requested); layout.addWidget(full)
        self.timer = QTimer(self); self.timer.setInterval(1200); self.timer.timeout.connect(self.refresh); self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0); widget = item.widget()
            if widget is not None: widget.deleteLater()
        rows = []
        for job in self.store.list_product_jobs():
            stage = job.current_stage or next((item.name for item in job.stages if item.status.value in {"running", "preparing", "cancelling"}), "等待处理")
            rows.append((job.updated_at, str(job.kind), job.status.value, int(job.progress * 100), stage, job.error))
        for job in self.store.list_jobs():
            if str(job.kind).startswith("product:"): continue
            rows.append((job.updated_at, job.kind.value, job.status.value, int(job.progress * 100), job.message or "等待处理", job.error))
        rows.sort(key=lambda item: item[0], reverse=True)
        preview = bool(getattr(self.parentWidget(), "ui_preview", False))
        if preview and not rows:
            # Presentation-only states satisfy the visual-preview contract
            # without manufacturing persisted jobs, outputs or model results.
            rows = [
                ("视觉预览", "落日信号 · 视觉样例", "预览中", 63, "任务卡片布局样例", "仅用于视觉核对，不代表本地处理已经运行。"),
                ("视觉预览", "声音训练 · 视觉样例", "等待", 0, "GPU 队列状态样例", "预览不会创建模型任务、音频文件或任务历史。"),
            ]
        if not rows:
            empty = QFrame(); empty.setObjectName("taskDrawerEmpty"); empty_layout = QVBoxLayout(empty); empty_layout.setAlignment(Qt.AlignCenter)
            title = QLabel("暂无本地任务")
            title.setObjectName("taskDrawerEmptyTitle"); title.setAlignment(Qt.AlignCenter); empty_layout.addWidget(title)
            detail = QLabel("开始处理后会在这里显示进度、阶段和失败原因。")
            detail.setObjectName("taskDrawerEmptyDetail"); detail.setWordWrap(True); detail.setAlignment(Qt.AlignCenter); empty_layout.addWidget(detail); self.cards_layout.addWidget(empty)
        else:
            for updated, kind, status, progress, stage, error in rows[:8]:
                card = QFrame(); card.setObjectName("taskDrawerCard"); card_layout = QVBoxLayout(card); card_layout.setContentsMargins(10, 10, 10, 10); card_layout.setSpacing(6)
                top = QHBoxLayout(); title = QLabel(kind.replace("_", " ")); title.setObjectName("taskDrawerCardTitle"); top.addWidget(title, 1)
                state = QLabel(status); state.setObjectName("taskDrawerState"); top.addWidget(state); card_layout.addLayout(top)
                detail = QLabel(error or stage); detail.setObjectName("taskDrawerCardDetail"); detail.setWordWrap(True); card_layout.addWidget(detail)
                bar = QProgressBar(); bar.setObjectName("taskDrawerProgress"); bar.setRange(0, 100); bar.setValue(max(0, min(100, progress))); bar.setTextVisible(False); card_layout.addWidget(bar)
                stamp = QLabel(str(updated)); stamp.setObjectName("taskDrawerStamp"); card_layout.addWidget(stamp)
                self.cards_layout.addWidget(card)
        self.cards_layout.addStretch()

    def show_for_parent(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            width = min(self.width(), max(300, parent.width() - 36)); self.setFixedWidth(width)
            self.setFixedHeight(max(280, parent.height() - 172)); self.move(max(18, parent.width() - width - 18), 88)
        self.refresh(); self.show(); self.raise_()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.timer.stop(); super().closeEvent(event)


class SimpleSettingsPage(QWidget):
    install_requested = Signal()
    def __init__(self, paths: AppPaths, store: StudioStore, project: Path, client: WorkerClient):
        super().__init__(); self.paths, self.store, self.project, self.client = paths, store, project, client; self.health_request = ""; self.raw = {}; self.setObjectName("settingsPage"); root = QVBoxLayout(self); root.setContentsMargins(26, 22, 26, 20); root.setSpacing(18)
        title_bar = QFrame(); title_bar.setObjectName("settingsTitleBar"); title_layout = QHBoxLayout(title_bar); title_layout.setContentsMargins(0, 0, 0, 0)
        title_text = QVBoxLayout(); title_text.setSpacing(4); title = QLabel("设置"); title.setObjectName("pageTitle"); title_subtitle = QLabel("管理本机引擎、工程位置与工作室外观。所有更改即时保存到本地。"); title_subtitle.setObjectName("settingsSubtitle"); title_text.addWidget(title); title_text.addWidget(title_subtitle); title_layout.addLayout(title_text); title_layout.addStretch()
        self.settings_save_state = QLabel("本地设置"); self.settings_save_state.setObjectName("settingsSaveState"); title_layout.addWidget(self.settings_save_state); self.save_settings = QPushButton("保存设置"); self.save_settings.setObjectName("primaryButton"); self.save_settings.clicked.connect(self._save_preferences); title_layout.addWidget(self.save_settings); root.addWidget(title_bar)
        self.health_card = QFrame(); self.health_card.setObjectName("healthCard"); health_layout = QHBoxLayout(self.health_card); text = QVBoxLayout(); top = QHBoxLayout(); self.health_title = QLabel("本地引擎"); self.health_title.setObjectName("cardTitle"); self.health_badge = QLabel("正在检测"); self.health_badge.setObjectName("statusChip"); top.addWidget(self.health_title); top.addWidget(self.health_badge); top.addStretch(); text.addLayout(top); self.health_detail = QLabel("正在检查显卡、CUDA、模型和 FFmpeg…"); self.health_detail.setObjectName("hint"); self.health_detail.setWordWrap(True); text.addWidget(self.health_detail); self.health_specs = QLabel(""); self.health_specs.setWordWrap(True); text.addWidget(self.health_specs); health_layout.addLayout(text, 1); actions = QVBoxLayout(); detect = QPushButton("重新检测"); detect.clicked.connect(self.check_health); repair = QPushButton("修复本地引擎"); repair.setObjectName("primaryButton"); repair.clicked.connect(self._repair); actions.addWidget(detect); actions.addWidget(repair); actions.addStretch(); health_layout.addLayout(actions); root.addWidget(self.health_card)
        common = QGroupBox("普通设置"); form = QFormLayout(common); self.output = QLineEdit(str(store.get_setting("default_output_dir", str(project / "exports")))); browse = QPushButton("选择…"); browse.clicked.connect(self._browse); row = QHBoxLayout(); row.addWidget(self.output); row.addWidget(browse); self.smart = QCheckBox("默认开启智能优化"); self.smart.setChecked(bool(store.get_setting("smart_optimization", True))); self.smart.toggled.connect(lambda value: store.set_setting("smart_optimization", value)); disk = shutil.disk_usage(paths.data_root); self.disk = QLabel(f"可用 {disk.free / 1024**3:.1f} GB / 共 {disk.total / 1024**3:.1f} GB"); cache = QPushButton("清理缓存"); cache.clicked.connect(self._clean_cache); form.addRow("默认输出目录", row); form.addRow("声音处理", self.smart); form.addRow("磁盘空间", self.disk); form.addRow("缓存", cache); root.addWidget(common)
        singing = QGroupBox("歌唱转换引擎"); singing_form = QFormLayout(singing); self.rvc_status = QLabel("正在检测"); self.rmvpe_status = QLabel("正在检测"); self.hubert_status = QLabel("正在检测"); self.singing_runtime = QLabel("正在检测"); self.gpu_status = QLabel("正在检测"); singing_form.addRow("RVC v2", self.rvc_status); singing_form.addRow("RMVPE", self.rmvpe_status); singing_form.addRow("HuBERT", self.hubert_status); singing_form.addRow("运行环境", self.singing_runtime); singing_form.addRow("GPU", self.gpu_status); root.addWidget(singing)
        advanced = QGroupBox("高级 / 原始诊断"); advanced.setCheckable(True); advanced.setChecked(False); advanced_layout = QFormLayout(advanced); advanced_layout.addRow("私有 Python", QLabel(str(paths.private_python))); advanced_layout.addRow("模型目录", QLabel(str(paths.models_root))); self.report = QPlainTextEdit(); self.report.setReadOnly(True); advanced_layout.addRow(self.report); copy = QPushButton("复制诊断"); copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText())); advanced_layout.addRow(copy); fold_group(advanced); root.addWidget(advanced); root.addStretch(); client.event.connect(self._event); self.check_health()
        self._organize_settings(root, common, singing, advanced)

    def _organize_settings(self, root, common, singing, advanced):
        from .theme import apply_preferences
        for widget in (self.health_card, common, singing, advanced): root.removeWidget(widget)
        # Remove the old expanding spacer before installing the tabbed body.
        if root.count() and root.itemAt(root.count() - 1).spacerItem(): root.takeAt(root.count() - 1)
        self.settings_navigation = QListWidget(); self.settings_navigation.setObjectName("settingsNavigation"); self.settings_navigation.setFixedWidth(188)
        self.settings_stack = QStackedWidget()
        self.settings_content = QFrame(); self.settings_content.setObjectName("settingsContentCard"); content_layout = QVBoxLayout(self.settings_content); content_layout.setContentsMargins(22, 18, 22, 18); content_layout.setSpacing(12)
        content_heading = QHBoxLayout(); heading_text = QVBoxLayout(); heading_text.setSpacing(2); self.settings_section_title = QLabel(); self.settings_section_title.setObjectName("settingsSectionTitle"); self.settings_section_subtitle = QLabel(); self.settings_section_subtitle.setObjectName("settingsSectionSubtitle"); heading_text.addWidget(self.settings_section_title); heading_text.addWidget(self.settings_section_subtitle); content_heading.addLayout(heading_text); content_heading.addStretch(); content_layout.addLayout(content_heading); content_layout.addWidget(self.settings_stack, 1)
        body = QHBoxLayout(); body.setSpacing(18); body.addWidget(self.settings_navigation); body.addWidget(self.settings_content, 1); root.addLayout(body, 1)
        sections = []
        for title in ("常规", "计算与模型", "存储", "外观", "隐私与数据"):
            self.settings_navigation.addItem(title)
            page = QWidget(); page.setObjectName("settingsSection"); layout = QVBoxLayout(page); layout.setContentsMargins(16, 12, 16, 12); layout.setSpacing(16)
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); self.settings_stack.addWidget(scroll); sections.append(layout)
        self.settings_navigation.currentRowChanged.connect(self.settings_stack.setCurrentIndex)
        self.settings_navigation.currentRowChanged.connect(lambda value: self.store.set_setting("ui.settings_tab", value))
        self.settings_navigation.currentRowChanged.connect(self._update_settings_heading)
        self.settings_navigation.setCurrentRow(max(0, min(4, int(self.store.get_setting("ui.settings_tab", 0)))))

        general = QGroupBox("创作偏好"); form = QFormLayout(general)
        self.restore_workspace = QCheckBox("启动时恢复上次页面"); self.restore_workspace.setObjectName("settingsToggle")
        self.restore_workspace.setToolTip("保存下一次启动时要恢复的工作区偏好。")
        self.restore_workspace.setChecked(bool(self.store.get_setting("ui.restore_workspace", True)))
        self.restore_workspace.toggled.connect(lambda value: self.store.set_setting("ui.restore_workspace", value))
        form.addRow(self.restore_workspace)
        self.task_notifications = QCheckBox("任务完成后显示本地提示"); self.task_notifications.setObjectName("settingsToggle")
        self.task_notifications.setToolTip("保存本机任务完成提示偏好；不会向网络发送通知。")
        self.task_notifications.setChecked(bool(self.store.get_setting("ui.task_notifications", True)))
        self.task_notifications.toggled.connect(lambda value: self.store.set_setting("ui.task_notifications", value))
        form.addRow(self.task_notifications)
        self.autoplay = QCheckBox("生成完成后自动播放"); self.autoplay.setObjectName("settingsToggle"); self.autoplay.setChecked(bool(self.store.get_setting("generation.autoplay", True)))
        self.autoplay.toggled.connect(lambda value: self.store.set_setting("generation.autoplay", value)); form.addRow(self.autoplay)
        self.ui_language = QComboBox(); self.ui_language.addItem("简体中文", "zh-CN"); self.ui_language.addItem("English（未就绪）", "en")
        self.ui_language.currentIndexChanged.connect(self._language_changed); form.addRow("界面语言", self.ui_language)
        sections[0].addWidget(general)
        sections[1].addWidget(self.health_card); sections[1].addWidget(singing); sections[1].addWidget(advanced)
        sections[2].addWidget(common)
        locations = QGroupBox("工程与缓存位置"); locations_form = QFormLayout(locations)
        model_locations = QGroupBox("模型资产位置"); model_form = QFormLayout(model_locations)
        self.directory_fields = {}
        overrides = self.store.get_setting("paths.overrides", {})
        for key, title, current, target_form in (("projects", "工程目录", self.paths.projects_root, locations_form), ("cache", "缓存目录", self.paths.cache_root, locations_form), ("models", "模型资产目录", self.paths.models_root, model_form)):
            field = QLineEdit(str(overrides.get(key, current))); field.setReadOnly(True); self.directory_fields[key] = field
            row = QHBoxLayout(); row.addWidget(field)
            browse = QPushButton("更改…"); browse.clicked.connect(lambda _checked=False, value=key: self._choose_directory(value)); row.addWidget(browse); target_form.addRow(title, row)
        note = QLabel("路径更改在重启后生效。已有工程和模型保留在原位置；新目录需要具备所需资产。缓存使用所选目录下的专用子目录。")
        note.setWordWrap(True); locations_form.addRow(note)
        sections[2].addWidget(locations); sections[1].insertWidget(0, model_locations)
        appearance = QGroupBox("界面外观"); form = QFormLayout(appearance)
        self.theme_choice = QComboBox(); self.theme_choice.addItem("暖橙白", "light"); self.theme_choice.addItem("深色工作室", "dark")
        self.density_choice = QComboBox()
        for label, value in (("标准", "standard"), ("紧凑", "compact"), ("宽松", "comfortable")): self.density_choice.addItem(label, value)
        for control, key, default in ((self.theme_choice, "ui.theme", "light"), (self.density_choice, "ui.density", "standard")):
            control.setCurrentIndex(max(0, control.findData(self.store.get_setting(key, default))))
            control.currentIndexChanged.connect(lambda _index, widget=control, setting=key: (self.store.set_setting(setting, widget.currentData()), apply_preferences(self.store)))
        form.addRow("主题", self.theme_choice); form.addRow("布局密度", self.density_choice); sections[3].addWidget(appearance)
        privacy = QGroupBox("本地数据"); form = QFormLayout(privacy)
        local = QLabel("音频、模型和任务数据仅在本机处理。当前应用没有音频上传服务。")
        local.setWordWrap(True); form.addRow(local)
        export = QPushButton("导出界面偏好…"); export.clicked.connect(self._export_preferences); form.addRow(export)
        for label, path in (("打开本地数据目录", self.paths.data_root), ("打开工程目录", self.paths.projects_root), ("打开模型目录", self.paths.models_root)):
            button = QPushButton(label); button.clicked.connect(lambda _checked=False, value=path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(value)))); form.addRow(button)
        sections[4].addWidget(privacy)
        for layout in sections: layout.addStretch()
        self.output.editingFinished.connect(self._save_output)

    def _update_settings_heading(self, index: int) -> None:
        titles = ("常规", "计算与模型", "存储", "外观", "隐私与数据")
        subtitles = (
            "设置生成后的播放方式和界面语言。",
            "检查 GPU、CUDA、模型资产和本地音频工具。",
            "选择工程、缓存、模型与默认导出位置。",
            "调整工作室主题和页面间距。",
            "查看本机数据范围并导出界面偏好。",
        )
        if 0 <= index < len(titles):
            self.settings_section_title.setText(titles[index])
            self.settings_section_subtitle.setText(subtitles[index])

    def _save_preferences(self) -> None:
        self.store.set_setting("ui.last_saved_at", utc_now())
        self.settings_save_state.setText("已保存到本机")
        QTimer.singleShot(2600, lambda: self.settings_save_state.setText("本地设置"))

    def _language_changed(self, index):
        if index:
            QMessageBox.information(self, "语言未就绪", "当前版本尚未提供完整英文翻译资源，界面继续使用简体中文。")
            self.ui_language.setCurrentIndex(0)
        self.store.set_setting("ui.language", "zh-CN")

    def _choose_directory(self, key):
        import tempfile
        selected = QFileDialog.getExistingDirectory(self, "选择目录（重启后生效）", self.directory_fields[key].text())
        if not selected: return
        try:
            directory = Path(selected).expanduser()
            if not directory.is_absolute() or not directory.is_dir(): raise ValueError("需要选择已存在的绝对目录")
            directory = directory.resolve()
            if key == "cache":
                directory = directory / ".voicestudio-cache"
                directory.mkdir(exist_ok=True)
            with tempfile.TemporaryFile(dir=directory): pass
            values = self.store.get_setting("paths.overrides", {})
            values[key] = str(directory)
            self.store.set_setting("paths.overrides", values)
            self.directory_fields[key].setText(str(directory))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "目录不可用", str(exc))

    def _save_output(self):
        import tempfile
        try:
            path = Path(self.output.text()).expanduser()
            if not path.is_absolute() or not path.is_dir(): raise ValueError("请选择已存在的绝对目录")
            with tempfile.TemporaryFile(dir=path): pass
            self.store.set_setting("default_output_dir", str(path.resolve()))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "目录不可用", str(exc))

    def _export_preferences(self):
        import json
        path, _ = QFileDialog.getSaveFileName(self, "导出界面偏好", str(self.project / "界面偏好.json"), "JSON (*.json)")
        if not path: return
        try:
            values = {key: self.store.get_setting(key, default) for key, default in (("ui.theme", "light"), ("ui.density", "standard"), ("ui.language", "zh-CN"), ("generation.autoplay", True), ("smart_optimization", True))}
            Path(path).write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc: QMessageBox.warning(self, "导出失败", str(exc))

    def check_health(self) -> None:
        self.health_badge.setText("正在检测"); self.health_badge.setObjectName("statusChip")
        try: self.health_request = self.client.send("health")
        except Exception as exc:
            self.health_badge.setText("需要修复"); self.health_badge.setObjectName("dangerChip"); self.health_detail.setText(str(exc))
            for label in (self.rvc_status, self.rmvpe_status, self.hubert_status, self.singing_runtime, self.gpu_status): label.setText("未验证：检测未能启动")
    def _event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id != self.health_request: return
        self.raw = payload
        compatible = event == "result" and payload.get("compatible")
        self.health_badge.setText("● 正常" if compatible else "需要修复"); self.health_badge.setObjectName("statusChip" if compatible else "dangerChip"); self.health_badge.style().unpolish(self.health_badge); self.health_badge.style().polish(self.health_badge)
        gpu = str(payload.get("gpu_name") or "未检测到可用 GPU"); cuda = str(payload.get("cuda_version") or "-"); acceleration = "GPU 加速已启用" if payload.get("tensor_test_passed") else "GPU 加速未就绪"; models = "模型完整" if payload.get("models_ready") else "模型不完整"
        self.rvc_status.setText("已就绪" if payload.get("rvc_ready") else "未安装 / 未就绪")
        self.rmvpe_status.setText("已就绪" if payload.get("rmvpe_ready") else "未安装 / 未就绪")
        self.hubert_status.setText("已验证" if payload.get("hubert_ready") else "未安装 / 未就绪")
        self.singing_runtime.setText(f"Python 3.11 / Torch {payload.get('rvc_torch_version') or '-'}")
        self.gpu_status.setText(gpu + (" · CUDA 可用" if payload.get("tensor_test_passed") else " · 不可用"))
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
        if value: self.output.setText(value); self._save_output()
    def _clean_cache(self) -> None:
        cache = self.paths.cache_root
        if QMessageBox.question(self, "清理缓存", "只清理可重新生成的试听缓存，不会删除声音、训练数据或输出。") == QMessageBox.Yes:
            if any(command not in {"health", "load_profile"} for command in getattr(self.client, "pending", {}).values()):
                QMessageBox.warning(self, "正在处理音频", "请等待活动任务结束后清理缓存。"); return
            from ..paths import ensure_within
            try:
                for name in ("preview", "waveforms"):
                    target = ensure_within(cache, cache / name)
                    if target.is_dir(): shutil.rmtree(target)
                cache.mkdir(parents=True, exist_ok=True)
                QMessageBox.information(self, "VoiceStudio", "试听和波形缓存已清理")
            except (OSError, ValueError) as exc: QMessageBox.warning(self, "清理失败", str(exc))
