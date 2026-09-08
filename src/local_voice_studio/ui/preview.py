"""Isolated visual-preview runtime for the HTML v4 desktop recreation.

The preview intentionally has no access to a user's projects, database, audio
files, or model worker.  It supplies only presentation state so every visible
control can be inspected without ever producing a file or claiming a model
operation completed.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QListWidgetItem, QPushButton, QVBoxLayout

from ..models import VoiceProfile
from ..paths import AppPaths
from ..storage import StudioStore
from .studio_widgets import TrackStatus
from .cover_session import LyricLine
from .export_rows import ExportRowData, ExportTableRow
from .html_pages import PreviewStemWave


class PreviewWorkerClient(QObject):
    """Worker-shaped adapter which reports that processing is unavailable."""

    event = Signal(str, str, dict)
    stderr_line = Signal(str)
    state_changed = Signal(str)
    ready_changed = Signal(bool)
    request_started = Signal(str, str)
    request_finished = Signal(str, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.ready = False
        self.pending: dict[str, str] = {}

    def start(self) -> None:
        QTimer.singleShot(0, lambda: self.state_changed.emit("视觉预览 · 未连接本地 Worker"))

    def shutdown(self) -> None:
        self.pending.clear()

    def restart(self) -> None:
        self.start()

    def send(self, command: str, payload: dict | None = None, request_id: str | None = None) -> str:
        request_id = request_id or f"preview-{uuid4().hex}"
        self.pending[request_id] = command
        self.request_started.emit(request_id, command)

        def report() -> None:
            self.pending.pop(request_id, None)
            self.request_finished.emit(request_id, command)
            self.event.emit(request_id, "error", {
                "status": "visual_preview", "command": command,
                "message": "视觉预览不执行本地模型或音频任务。请在正常模式中运行此操作。",
            })

        QTimer.singleShot(120, report)
        return request_id

    def attach_pipeline_controller(self, _controller: object) -> None:
        return

    def detach_pipeline_controller(self, _controller: object) -> None:
        return

    def retry_product_job(self, _job_id: str) -> str:
        raise RuntimeError("视觉预览不重试任务；请在正常模式中操作。")


def _sample_peaks(index: int) -> list[float]:
    """Deterministic presentation waveform; it is never represented as audio."""
    return [0.14 + ((point * (index + 3)) % 17) / 23 for point in range(180)]


def apply_preview_presentation(window) -> None:
    """Populate the existing pages with display-only sample state."""
    page = window.cover_page
    page.song_title.setText("落日信号")
    page.song_meta.setText("视觉预览样例 · 03:42 · 48 kHz · 无真实音频")
    page.rights_state.setText("视觉预览：权利状态未提交")
    page.voice_capabilities.setText("视觉预览 · 不执行模型任务")
    page.workflow_steps.set_step(3)
    page.take_selector.blockSignals(True)
    page.take_selector.clear()
    page.take_selector.addItems(("Take 01 · Pitch 0 · 视觉样例", "Take 02 · Pitch −2 · 视觉样例", "Take 03 · 推荐 · 视觉样例"))
    page.take_selector.setCurrentIndex(2)
    page.take_selector.setEnabled(True)
    page.take_selector.blockSignals(False)
    for index, track in enumerate(page.stems):
        track.set_status(TrackStatus.READY)
        track.set_waveform(_sample_peaks(index), 222000)
        track.set_volume((85, 74, 78, 88, 80)[index])
    page.stems[3].name_label.setText("AI 人声 · 视觉样例")
    page.lyrics.set_lyrics([
        LyricLine(8.0, "夜色落在安静的街角"),
        LyricLine(22.0, "信号穿过没有名字的站台"),
        LyricLine(39.0, "这一段旋律，只用于界面对照"),
        LyricLine(55.0, "不会写入音频，也不会上传"),
    ])
    page.lyric_status.setText("视觉预览歌词")
    page.separate_button.setText("检查分离能力")
    page.cover_button.setText("检查翻唱能力")
    page.render_button.setText("检查最终处理能力")
    page.export_button.setText("检查导出能力")
    for button in (page.separate_button, page.cover_button, page.render_button, page.export_button):
        button.setEnabled(True)
        try:
            button.clicked.disconnect()
        except RuntimeError:
            pass
        button.clicked.connect(lambda _checked=False, p=page: p.song_meta.setText("视觉预览不执行处理；正常模式会先检测本地能力。"))
    try:
        page.import_button.clicked.disconnect()
    except RuntimeError:
        pass
    page.import_button.clicked.connect(lambda: page.song_meta.setText("视觉预览中的导入只保留原型状态，不打开文件选择器。"))
    # Song-library records are visual state only. Selecting one changes the
    # preview title but does not create a cover project or touch a file.
    visual_songs = (("落日信号", "STEMS READY"), ("晚风经过车站", "待处理"), ("星河旧梦", "已完成"), ("城市漫游", "待处理"))
    def render_visual_songs() -> None:
        query = page.song_search.text().strip().casefold()
        filter_value = page._song_filter
        visible = [(title, status) for title, status in visual_songs if (not query or query in title.casefold()) and (filter_value == "all" or (filter_value == "pending" and status == "待处理") or (filter_value == "complete" and status == "已完成"))]
        page.song_list.blockSignals(True); page.song_list.clear()
        for index, (title, status) in enumerate(visible):
            item = QListWidgetItem(f"{title}\n{status}")
            item.setData(0x0100, "")
            page.song_list.addItem(item)
            if title == "落日信号": page.song_list.setCurrentItem(item)
        page.song_list.blockSignals(False); page.song_count.setText(f"{len(visual_songs)} 首")
    render_visual_songs()
    try:
        page.song_search.textChanged.disconnect()
    except RuntimeError:
        pass
    page.song_search.textChanged.connect(lambda _query: render_visual_songs())
    for key, button in page.song_filters.items():
        try:
            button.clicked.disconnect()
        except RuntimeError:
            pass
        button.clicked.connect(lambda _checked=False, value=key: (setattr(page, "_song_filter", value), [control.setChecked(name == value) for name, control in page.song_filters.items()], render_visual_songs()))
    page.song_list.itemClicked.connect(lambda item: (page.song_title.setText(item.text().split("\n", 1)[0]), page.song_meta.setText("视觉预览样例 · 03:42 · 48 kHz · 无真实音频")))

    generate = window.generate_page
    generate.ui_preview = True
    generate.empty.hide(); generate.form.show()
    generate.profile.clear()
    for index, name in enumerate(("Studio Voice 01", "小岚", "旁白男声", "女声 A", "实验音色 02")):
        generate.profile.addItem(f"{name} · 视觉样例", f"preview-voice-{index}")
    generate._populate_voice_rows()
    generate.text.setPlainText("这里的文字、语速与风格控件可用于校验布局；视觉预览不会生成文件。")
    generate.preview_wave.set_visual_sample(True)
    generate.generation_status.setText("视觉预览：本地 Worker 已隔离")
    history_content = QFrame()
    history_layout = QVBoxLayout(history_content)
    history_layout.setContentsMargins(0, 0, 0, 0)
    history_layout.setSpacing(7)
    for stamp, excerpt in (("今天 14:42", "夜色慢慢落下来…"), ("今天 13:18", "欢迎回来，今天…"), ("昨天 22:06", "这封信写给…")):
        item = QFrame(); item.setObjectName("ttsHistoryItem")
        item_layout = QVBoxLayout(item); item_layout.setContentsMargins(9, 9, 9, 9); item_layout.setSpacing(4)
        title = QLabel(stamp); title.setObjectName("ttsHistoryItemTitle"); item_layout.addWidget(title)
        copy = QLabel(f"“{excerpt}”"); copy.setObjectName("ttsHistoryItemText"); copy.setWordWrap(True); item_layout.addWidget(copy)
        history_layout.addWidget(item)
    history_layout.addStretch()
    generate.history.setWidget(history_content)

    separator = window.separator_page
    def clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def render_preview_stems(mode="roformer") -> None:
        clear_layout(separator.stems)
        separator.file_label.setText("落日信号.wav\n视觉预览输入 · 无本地文件")
        separator.state.setText("视觉预览")
        separator.summary.setText(f"{mode} 的 stem 布局样例。不会读取音频、检测模型或创建分离任务。")
        for name, format_label in (("主唱人声", "vocals.wav"), ("伴奏", "instrumental.wav"), ("和声", "backing.wav"), ("其他残响", "residual.wav")):
            row = QFrame(); row.setObjectName("separationStem"); layout = QHBoxLayout(row); layout.setContentsMargins(10, 8, 10, 8)
            labels = QVBoxLayout(); title = QLabel(name); title.setObjectName("separationStemTitle"); labels.addWidget(title); detail = QLabel(f"{format_label} · 视觉样例 · 无本地文件"); detail.setObjectName("hint"); labels.addWidget(detail); layout.addLayout(labels)
            wave = PreviewStemWave(); layout.addWidget(wave, 1)
            status = QLabel("视觉样例"); status.setObjectName("statusBadge"); layout.addWidget(status); separator.stems.addWidget(row)
    render_preview_stems()
    try:
        separator.choose_button.clicked.disconnect()
    except RuntimeError:
        pass
    separator.choose_button.clicked.connect(lambda: separator.summary.setText("视觉预览不打开文件选择器；正常模式可选择真实本地音频。"))
    try:
        separator.start_button.clicked.disconnect()
    except RuntimeError:
        pass
    separator.start_button.clicked.connect(lambda: separator.summary.setText("视觉预览不提交分离任务；正常模式会检测输入和模型后再提交。"))
    for button in separator.mode_buttons:
        try:
            button.clicked.disconnect()
        except RuntimeError:
            pass
        button.clicked.connect(lambda _checked=False, current=button: ([item.setChecked(item is current) for item in separator.mode_buttons], render_preview_stems(str(current.property("mode")))))

    exports = window.exports_page
    preview_exports = (("WAV", "落日信号_视觉样例.wav", "翻唱"), ("MP3", "落日信号_视觉样例.mp3", "翻唱"), ("WAV", "旁白文稿_视觉样例.wav", "语音"), ("VOC", "落日信号_vocals.wav", "分离"))
    def render_preview_exports() -> None:
        clear_layout(exports.rows)
        for key, button in exports.filters.items():
            button.setChecked(key == exports.filter)
        shown = [item for item in preview_exports if exports.filter == "all" or (exports.filter == "cover" and item[2] == "翻唱") or (exports.filter == "voice" and item[2] == "语音")]
        for kind, name, source in shown:
            exports.rows.addWidget(ExportTableRow(ExportRowData(name, source, kind, "", "视觉样例")))
        exports.rows.addStretch()
        exports.storage.setText("视觉预览\n4 个布局样例 · 不占用本地存储\n\n正常模式只扫描真实本地导出文件。")
    render_preview_exports()
    for key, button in exports.filters.items():
        try:
            button.clicked.disconnect()
        except RuntimeError:
            pass
        button.clicked.connect(lambda _checked=False, value=key: (setattr(exports, "filter", value), render_preview_exports()))
    for button, message in ((exports.open_folder_button, "视觉预览不打开目录；正常模式会打开项目导出目录。"), (exports.refresh_button, "视觉预览不扫描磁盘；正常模式只显示真实输出。")):
        try:
            button.clicked.disconnect()
        except RuntimeError:
            pass
        button.clicked.connect(lambda _checked=False, text=message: exports.storage.setText(text))

    # Training uses the same five-step shell as the real workflow, but the
    # preview carries a fixed sample set so the drop zone, sample quality
    # rows, diagnosis card and training controls can be checked together.
    training = window.training_page
    training.name.setText("我的新声音")
    training.consent.setChecked(False)
    training._render_material_rows([
        ("voice_sample_01.wav", 94, "优秀", "preview:voice_sample_01"),
        ("voice_sample_02.wav", 87, "良好", "preview:voice_sample_02"),
        ("voice_sample_03.wav", 72, "可用", "preview:voice_sample_03"),
        ("voice_sample_04.wav", 43, "噪声高", "preview:voice_sample_04"),
    ])
    training.quality_summary.setText("86 / 100 · 适合训练")
    training.quality_bar.setValue(86)
    training.quality_details.setText("有效纯人声  ·  4:18\n舒适音域估计  ·  A2–E5\n预计训练相似度  ·  90–93%")
    training.quality_metrics.setText("信噪比 92   ·   混响控制 84\n音域覆盖 78   ·   削波安全 95")
    training.quality_alert.setText("⚠ voice_sample_04.wav 存在明显环境噪声；建议剔除。")
    training.quality_alert.show()
    # The reference right rail keeps only capability switches, name and
    # quality in the visible settings card.  The real profile/status rows
    # remain available in normal mode and are simply folded for this sample.
    training_form = training.singing_card.layout()
    for field in (training.singing_profile, training.singing_status, training.singing_detail, training.consent, training.singing_progress):
        label = training_form.labelForField(field)
        if label is not None:
            label.hide()
        field.hide()
    training.singing_train_button.hide()
    training.singing_cancel_button.hide()
    training.singing_manage_button.hide()
    for index in range(training.material_actions.count()):
        widget = training.material_actions.itemAt(index).widget()
        if widget is not None:
            widget.hide()
    for widget in (training.progress, training.status, training.more, training.record, training.cancel, training.training_run_label):
        widget.hide()
    for index in range(training.training_action_row.count()):
        widget = training.training_action_row.itemAt(index).widget()
        if widget is not None:
            widget.hide()
    training.status.setText("视觉预览：不会开始训练或写入声音库。")
    training.primary.setText("检查训练能力")
    try:
        training.primary.clicked.disconnect()
    except RuntimeError:
        pass
    training.primary.clicked.connect(lambda: training.status.setText("视觉预览不提交训练任务；正常模式会先检测授权和本地模型。"))
    training.singing_train_button.setText("检查歌唱训练能力")
    try:
        training.singing_train_button.clicked.disconnect()
    except RuntimeError:
        pass
    training.singing_train_button.clicked.connect(lambda: training.singing_status.setText("视觉预览不提交歌唱模型训练。"))

    window.recent_page.refresh()


def create_preview_window():
    """Create a normal MainWindow with temporary, isolated data adapters."""
    # Import lazily to avoid a circular dependency with main_window.
    from .main_window import MainWindow

    temp = TemporaryDirectory(prefix="voicestudio-ui-preview-")
    sandbox = Path(temp.name)
    root = AppPaths(
        data_root=sandbox / "data",
        projects_root=sandbox / "projects",
        runtime_root=sandbox / "runtime",
        engine_root=sandbox / "engines" / "GPT-SoVITS",
        models_root=sandbox / "models",
        logs_root=sandbox / "logs",
        database=sandbox / "data" / "studio.sqlite3",
        cache_directory=sandbox / "cache",
    )
    root.ensure()
    store = StudioStore(root)
    # These records exist only in the preview's temporary SQLite database.
    # They deliberately have no assets, models, or generated outputs.
    for name in ("晚风经过车站", "旁白文稿 · 九月", "星河旧梦 · 分轨"):
        store.create_project(name)
    project = store.create_project("HTML v4 视觉样例")
    for name in ("清栀 · 视觉样例", "夜航 · 视觉样例", "明澈 · 视觉样例", "暮山 · 视觉样例"):
        store.save_profile(project, VoiceProfile(name=name, consent_confirmed=False))
    window = MainWindow(root, store, client=PreviewWorkerClient())
    window.ui_preview = True
    window._preview_temp = temp  # retain the sandbox until the window closes
    window.setWindowTitle("VoiceStudio · HTML v4 视觉预览")
    badge = QLabel("视觉预览")
    badge.setObjectName("visualPreviewBadge")
    badge.setToolTip("视觉预览：数据与 Worker 已隔离；不会读取真实工程或执行模型任务。")
    badge.setAccessibleName("视觉预览，数据与 Worker 已隔离")
    window._header_layout.insertWidget(2, badge)
    apply_preview_presentation(window)
    window.voice_page.refresh()
    try:
        window.voice_page.import_models.clicked.disconnect()
    except RuntimeError:
        pass
    window.voice_page.import_models.clicked.connect(lambda: window.voice_page.detail_note.setText("视觉预览不打开文件选择器，也不会登记或启用模型文件。"))
    window.recent_page.refresh()
    return window
