from __future__ import annotations

from pathlib import Path
from .export_rows import ExportRowData, ExportTableRow
from PySide6.QtCore import Qt, QProcess, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QFileDialog, QMessageBox, QListWidget, QListWidgetItem, QProgressBar, QGridLayout, QScrollArea, QSizePolicy,
)


class AudioDropTarget(QLabel):
    """Drop target that accepts an actual local audio file, never a mock path."""

    audio_dropped = Signal(str)
    _AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}

    def __init__(self):
        super().__init__("拖入歌曲或点击选择\nWAV / FLAC / MP3 / M4A")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        urls = event.mimeData().urls()
        if any(url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in self._AUDIO_SUFFIXES for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        path = next((Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in self._AUDIO_SUFFIXES), None)
        if path and path.is_file():
            self.audio_dropped.emit(str(path))
            event.acceptProposedAction()


class ProjectThumbnail(QFrame):
    """Small non-audio preview motif matching the reference project's wave tile."""

    def __init__(self, variant: int, parent=None):
        super().__init__(parent)
        self.variant = variant
        self.setObjectName(f"projectThumb{variant % 4}")

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        tones = (QColor("#d98a50"), QColor("#ae7956"), QColor("#d4a149"), QColor("#9e8370"))
        tone = tones[self.variant % len(tones)]
        left, right, center = 13, max(14, self.width() - 13), self.height() // 2
        for offset, opacity in ((-11, 0.22), (0, 0.76), (11, 0.35)):
            pen = QPen(tone)
            pen.setWidth(1)
            painter.setOpacity(opacity)
            painter.setPen(pen)
            painter.drawLine(left, center + offset, right, center + offset)
        painter.end()


class PreviewStemWave(QFrame):
    """Warm, deterministic stem waveform used only by the isolated preview."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("separationPreviewWave")
        self.setMinimumHeight(32)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fcfaf7"))
        painter.setPen(QPen(QColor("#eee8e1"), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        pen = QPen(QColor("#ff9a4a"))
        pen.setWidth(2)
        painter.setPen(pen)
        center = self.height() // 2
        usable = max(1, self.width() - 12)
        for x in range(6, self.width() - 6, 4):
            amplitude = 3 + ((x * 17 + self.height() * 3) % 13)
            painter.drawLine(x, center - amplitude, x, center + amplitude)
        painter.end()


class RecentProjectCard(QFrame):
    """A local project card with the HTML v4 project-grid treatment."""

    open_requested = Signal(str)

    def __init__(self, project_path: Path, title: str, meta: str, variant: int, parent=None):
        super().__init__(parent)
        self._project_path = str(project_path)
        self.setObjectName("recentProjectCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self._project_path)
        self.setMinimumHeight(166)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 11)
        layout.setSpacing(0)
        thumbnail = ProjectThumbnail(variant)
        thumbnail.setFixedHeight(78)
        layout.addWidget(thumbnail)
        heading = QLabel(title)
        heading.setObjectName("recentProjectTitle")
        heading.setWordWrap(False)
        heading.setToolTip(title)
        layout.addWidget(heading)
        caption = QLabel(meta)
        caption.setObjectName("recentProjectMeta")
        caption.setWordWrap(False)
        caption.setToolTip(meta)
        layout.addWidget(caption)

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self._project_path)
            event.accept()
            return
        super().mousePressEvent(event)


class RuntimePanel(QFrame):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        body = QLabel(message)
        body.setObjectName("pageSubtitle")
        body.setWordWrap(True)
        layout.addWidget(body)
        self.body = body


class HtmlPage(QWidget):
    """Functional desktop counterpart for the prototype's secondary pages.

    The prototype only supplied visual placeholders for these areas. This page
    deliberately exposes real local actions and reports capability failures;
    it never claims that a model task succeeded when it did not run.
    """
    audio_selected = Signal(str)
    project_selected = Signal(str)

    def __init__(self, title, subtitle, store=None, project=None, capability=""):
        super().__init__()
        self.store, self.project, self.capability = store, Path(project) if project else None, capability
        self.setObjectName("htmlPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        t = QLabel(title); t.setObjectName("pageTitle"); titles.addWidget(t)
        s = QLabel(subtitle); s.setObjectName("pageSubtitle"); s.setWordWrap(True); titles.addWidget(s)
        header.addLayout(titles, 1)
        self.state = QLabel("正在检查本地状态"); self.state.setObjectName("statusBadge"); header.addWidget(self.state, 0, Qt.AlignTop)
        root.addLayout(header)

        body = QHBoxLayout(); body.setSpacing(16)
        self.workspace = RuntimePanel("工作区", "")
        body.addWidget(self.workspace, 1)
        self.runtime = RuntimePanel("运行状态", "")
        self.runtime.setMinimumWidth(320); body.addWidget(self.runtime)
        root.addLayout(body, 1)
        self.items = QListWidget()
        self.items.setAccessibleName("本地项目与输出")
        self.workspace.layout().addWidget(self.items)
        self.items.itemDoubleClicked.connect(self._activate_item)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新状态"); self.refresh_button.setObjectName("secondaryButton"); self.refresh_button.clicked.connect(self.refresh); actions.addWidget(self.refresh_button)
        if capability == "separation":
            self.import_button = QPushButton("选择音频并检查分离能力"); self.import_button.setObjectName("primaryButton"); self.import_button.clicked.connect(self._choose_audio); actions.addWidget(self.import_button)
        elif capability == "export":
            self.open_button = QPushButton("打开当前项目导出目录"); self.open_button.setObjectName("secondaryButton"); self.open_button.clicked.connect(self._open_exports); actions.addWidget(self.open_button)
        actions.addStretch(); root.addLayout(actions)
        self.refresh()

    def refresh(self):
        self.items.clear()
        project = self.project
        exists = bool(project and project.exists())
        self.workspace.body.setText(f"当前项目：{project.name if exists else '未打开'}\n路径：{project if exists else '—'}")
        from ..runtime import EngineRuntimeResolver
        ffmpeg = EngineRuntimeResolver(self.store.paths).resolve_private_tool("ffmpeg") if self.store else None
        if self.capability == "separation":
            msg = f"私有 FFmpeg：{ffmpeg}" if ffmpeg else "私有 FFmpeg：未找到；请进入设置安装或修复本地引擎"
            self.runtime.body.setText(msg + "\n分离模型与运行环境会在提交前进一步检测。选择文件后进入真实分离流程。")
            self.state.setText("待检查模型" if ffmpeg else "未就绪")
        elif self.capability == "export":
            out = project / "covers" if exists else None
            self.runtime.body.setText(f"导出目录：{out or '—'}\n文件是否存在以磁盘扫描结果为准。")
            self.state.setText("已连接项目" if exists else "未打开项目")
            if exists and self.store:
                for record in self.store.list_generation_records(project):
                    for value in (record.wav_path, record.mp3_path):
                        if value and Path(value).is_file(): self._add_item(Path(value).name, value)
                if out and out.exists():
                    for value in out.glob("*/exports/*"):
                        if value.is_file() and value.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a"}: self._add_item(value.name, str(value))
            self.workspace.body.setText(self.workspace.body.text() + "\n双击文件打开实际输出。")
        elif self.capability == "recent":
            for entry in self.store.list_projects() if self.store else []:
                self._add_item(entry.get("name") or Path(entry["path"]).name, entry["path"])
            self.runtime.body.setText(f"本地工程：{self.items.count()} 个\n双击工程继续编辑。")
            self.state.setText("已读取本地存储")
        else:
            self.runtime.body.setText("页面状态已从本地项目上下文刷新。")
            self.state.setText("已刷新")

    def _choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择待分离音频", str(self.project or Path.home()), "音频文件 (*.wav *.mp3 *.flac *.m4a)")
        if not path: return
        self.audio_selected.emit(path)

    def _add_item(self, title, path):
        item = QListWidgetItem(title)
        item.setData(Qt.UserRole, str(path))
        item.setToolTip(str(path))
        self.items.addItem(item)

    def _activate_item(self, item):
        path = item.data(Qt.UserRole)
        if self.capability == "recent":
            self.project_selected.emit(path)
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                QMessageBox.warning(self, "无法打开文件", f"系统未能打开：{path}")

    def _open_exports(self):
        target = (self.project / "covers") if self.project else None
        if not target or not target.exists():
            QMessageBox.information(self, "暂无导出目录", "当前项目还没有导出目录。完成一次真实导出后这里会自动出现。")
            return
        QProcess.startDetached("explorer.exe", [str(target)])


class SeparationStudioPage(QWidget):
    """Native counterpart of the prototype's dedicated separation page."""

    audio_selected = Signal(str)

    def __init__(self, store, project):
        super().__init__()
        self.store, self.project, self.selected_path = store, Path(project), ""
        self.setObjectName("separationPage"); self.setMinimumHeight(0)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); page_scroll = QScrollArea(); page_scroll.setWidgetResizable(True); page_scroll.setFrameShape(QFrame.NoFrame); page_scroll.setMinimumHeight(0); page_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored); content = QWidget(); content.setObjectName("separationContent"); root = QVBoxLayout(content); root.setContentsMargins(24, 20, 24, 18); root.setSpacing(14); outer.addWidget(page_scroll); page_scroll.setWidget(content); self.content_scroll = page_scroll
        titlebar = QHBoxLayout(); copy = QVBoxLayout(); copy.setSpacing(3)
        title = QLabel("音频分离"); title.setObjectName("pageTitle"); copy.addWidget(title)
        subtitle = QLabel("独立工具，也会被 AI 翻唱流程自动调用。结果写入项目缓存，可重复使用。"); subtitle.setObjectName("pageSubtitle"); copy.addWidget(subtitle); titlebar.addLayout(copy, 1)
        cache = QPushButton("刷新缓存状态"); cache.setObjectName("secondaryButton"); cache.clicked.connect(self._refresh); titlebar.addWidget(cache); root.addLayout(titlebar)
        body = QHBoxLayout(); body.setSpacing(14)
        left = QVBoxLayout(); left.setSpacing(14)
        upload = QFrame(); upload.setObjectName("separationUpload"); upload_layout = QVBoxLayout(upload); upload_layout.setContentsMargins(16, 16, 16, 16); upload_layout.addWidget(QLabel("输入歌曲", objectName="sectionLabel"))
        self.file_label = AudioDropTarget(); self.file_label.setObjectName("separationDrop"); self.file_label.setAlignment(Qt.AlignCenter); self.file_label.setMinimumHeight(110); self.file_label.audio_dropped.connect(self._use_audio); upload_layout.addWidget(self.file_label)
        choose = QPushButton("选择音频文件…"); self.choose_button = choose; choose.setObjectName("secondaryButton"); choose.clicked.connect(self._choose); upload_layout.addWidget(choose); left.addWidget(upload)
        modes = QFrame(); modes.setObjectName("separationModes"); modes_layout = QVBoxLayout(modes); modes_layout.setContentsMargins(16, 16, 16, 16); modes_layout.addWidget(QLabel("分离模式", objectName="sectionLabel")); self.mode_buttons = []
        for key, label, detail in (("uvr5", "快速 · UVR5", "速度优先，适合普通试听"), ("roformer", "高质量 · RoFormer", "更干净的人声边缘，推荐翻唱"), ("multi", "极致 · 多轨", "能力检测后提供主唱、和声和伴奏")):
            button = QPushButton(f"{label}\n{detail}"); button.setCheckable(True); button.setProperty("mode", key); button.setObjectName("separationMode"); button.clicked.connect(lambda _checked=False, item=button: self._select_mode(item)); modes_layout.addWidget(button); self.mode_buttons.append(button)
        self.mode_buttons[1].setChecked(True)
        self.start_button = QPushButton("开始分离"); self.start_button.setObjectName("primaryButton"); self.start_button.clicked.connect(self._start); modes_layout.addWidget(self.start_button); left.addWidget(modes); left.addStretch(); body.addLayout(left, 1)
        result = QFrame(); result.setObjectName("separationResults"); result_layout = QVBoxLayout(result); result_layout.setContentsMargins(18, 16, 18, 16); result_layout.setSpacing(12)
        header = QHBoxLayout(); header.addWidget(QLabel("分离结果", objectName="cardTitle")); header.addStretch(); self.state = QLabel("等待输入"); self.state.setObjectName("statusBadge"); header.addWidget(self.state); result_layout.addLayout(header)
        self.summary = QLabel("选择真实音频后，将先检查 FFmpeg、模型和选定引擎，再提交本地任务。"); self.summary.setObjectName("cardSub"); self.summary.setWordWrap(True); result_layout.addWidget(self.summary)
        self.stems = QVBoxLayout(); result_layout.addLayout(self.stems); result_layout.addStretch(); body.addWidget(result, 2); root.addLayout(body, 1)
        self._refresh()

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择待分离音频", str(self.project), "音频文件 (*.wav *.mp3 *.flac *.m4a *.aac *.ogg)")
        if path:
            self._use_audio(path)

    def _use_audio(self, path: str) -> None:
        candidate = Path(path)
        if not candidate.is_file():
            self.state.setText("输入文件不存在")
            self.summary.setText("选择的本地音频已不存在，未创建分离任务。")
            return
        self.selected_path = str(candidate); self._refresh()

    def _select_mode(self, selected):
        for button in self.mode_buttons:
            button.setChecked(button is selected)
        self._refresh()

    def _start(self):
        if not self.selected_path:
            self.state.setText("请选择音频")
            self.summary.setText("尚未选择输入文件；不会创建任务或输出。")
            return
        self.state.setText("正在交给翻唱流程检查")
        self.audio_selected.emit(self.selected_path)

    def _refresh(self):
        while self.stems.count():
            item = self.stems.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        from ..runtime import EngineRuntimeResolver
        tool = EngineRuntimeResolver(self.store.paths).resolve_private_tool("ffmpeg")
        mode = next((button.property("mode") for button in self.mode_buttons if button.isChecked()), "roformer")
        if not self.selected_path:
            self.state.setText("等待输入" if tool else "FFmpeg 未就绪")
            self.summary.setText(("私有 FFmpeg 已检测到。" if tool else "未检测到私有 FFmpeg；请在设置中修复本地引擎。") + f" 当前模式：{mode}。")
            return
        source = Path(self.selected_path)
        self.file_label.setText(source.name)
        self.state.setText("待检查模型")
        self.summary.setText(f"输入：{source.name}\n提交前会对 {mode} 的模型资产进行真实检测。")
        for name, note in (("主唱人声", "等待真实分离"), ("伴奏", "等待真实分离"), ("和声 / 残响", "由所选引擎决定")):
            row = QFrame(); row.setObjectName("separationStem"); layout = QHBoxLayout(row); label = QLabel(name); layout.addWidget(label); layout.addStretch(); status = QLabel(note); status.setObjectName("hint"); layout.addWidget(status); self.stems.addWidget(row)


class ExportsStudioPage(QWidget):
    """Native export center which presents only real files from local storage."""

    def __init__(self, store, project):
        super().__init__()
        self.store, self.project, self.filter = store, Path(project), "all"
        self.setObjectName("exportsPage"); self.setMinimumHeight(0)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); page_scroll = QScrollArea(); page_scroll.setWidgetResizable(True); page_scroll.setFrameShape(QFrame.NoFrame); page_scroll.setMinimumHeight(0); page_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored); content = QWidget(); content.setObjectName("exportsContent"); root = QVBoxLayout(content); root.setContentsMargins(24, 20, 24, 18); root.setSpacing(14); outer.addWidget(page_scroll); page_scroll.setWidget(content); self.content_scroll = page_scroll
        titlebar = QHBoxLayout(); copy = QVBoxLayout(); copy.setSpacing(3); title = QLabel("导出中心"); title.setObjectName("pageTitle"); copy.addWidget(title); subtitle = QLabel("统一查看翻唱、文字生成与音频分离的本地输出。"); subtitle.setObjectName("pageSubtitle"); copy.addWidget(subtitle); titlebar.addLayout(copy, 1)
        folder = QPushButton("打开导出文件夹"); self.open_folder_button = folder; folder.setObjectName("secondaryButton"); folder.clicked.connect(self._open_folder); titlebar.addWidget(folder); root.addLayout(titlebar)
        body = QHBoxLayout(); body.setSpacing(14); listing = QFrame(); listing.setObjectName("exportsListing"); list_layout = QVBoxLayout(listing); list_layout.setContentsMargins(16, 14, 16, 14); head = QHBoxLayout(); head.addWidget(QLabel("最近导出", objectName="cardTitle")); head.addStretch(); self.filters = {}
        for key, label in (("all", "全部"), ("cover", "翻唱"), ("voice", "语音")):
            button = QPushButton(label); button.setCheckable(True); button.setObjectName("exportFilter"); button.clicked.connect(lambda _checked=False, value=key: self._set_filter(value)); self.filters[key] = button; head.addWidget(button)
        list_layout.addLayout(head); list_layout.addWidget(ExportTableRow()); self.rows = QVBoxLayout(); self.rows.setSpacing(0); list_layout.addLayout(self.rows); list_layout.addStretch(); body.addWidget(listing, 1)
        side = QFrame(); side.setObjectName("exportsSide"); side.setFixedWidth(290); side_layout = QVBoxLayout(side); side_layout.setContentsMargins(16, 14, 16, 14); side_layout.addWidget(QLabel("本地存储", objectName="sectionLabel")); self.storage = QLabel(); self.storage.setObjectName("exportsStorage"); self.storage.setWordWrap(True); side_layout.addWidget(self.storage); side_layout.addWidget(QLabel("默认导出格式", objectName="sectionLabel")); self.formats = QLabel("WAV  无损\nMP3  320 kbps\nFLAC  无损压缩\n\n导出文件始终保留在本地项目目录。", objectName="cardSub"); self.formats.setWordWrap(True); side_layout.addWidget(self.formats); side_layout.addStretch(); refresh = QPushButton("刷新磁盘扫描"); self.refresh_button = refresh; refresh.setObjectName("secondaryButton"); refresh.clicked.connect(self.refresh); side_layout.addWidget(refresh); body.addWidget(side); root.addLayout(body, 1); self.refresh()

    def _set_filter(self, value):
        self.filter = value; self.refresh()

    def _files(self):
        values = []
        for record in self.store.list_generation_records(self.project):
            for path in (record.wav_path, record.mp3_path):
                if path and Path(path).is_file(): values.append((Path(path), "语音"))
        cover_root = self.project / "covers"
        if cover_root.exists():
            for path in cover_root.glob("*/exports/*"):
                if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a"}: values.append((path, "翻唱"))
        return sorted({str(path.resolve()): (path, source) for path, source in values}.values(), key=lambda item: item[0].stat().st_mtime, reverse=True)

    def refresh(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        files = self._files()
        for key, button in self.filters.items(): button.setChecked(key == self.filter)
        shown = [item for item in files if self.filter == "all" or (self.filter == "voice" and item[1] == "语音") or (self.filter == "cover" and item[1] == "翻唱")]
        if not shown:
            empty = QLabel("尚无真实导出文件。完成一次本地生成或翻唱导出后会显示在这里。"); empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignCenter); self.rows.addWidget(empty)
        for path, source in shown:
            row = ExportTableRow(ExportRowData.from_file(path, source))
            row.open_requested.connect(lambda value: QDesktopServices.openUrl(QUrl.fromLocalFile(value)))
            self.rows.addWidget(row)
        try:
            import shutil
            usage = shutil.disk_usage(self.store.paths.data_root)
            self.storage.setText(f"可用 {usage.free / 1024**3:.1f} GB / 共 {usage.total / 1024**3:.1f} GB\n当前项目找到 {len(files)} 个真实导出文件。")
        except OSError:
            self.storage.setText(f"当前项目找到 {len(files)} 个真实导出文件。")

    def _open_folder(self):
        target = self.project / "covers"
        target.mkdir(parents=True, exist_ok=True)
        QProcess.startDetached("explorer.exe", [str(target)])


class RecentProjectsPage(QWidget):
    """Prototype-style local project browser; it never fabricates activity."""
    project_selected = Signal(str)
    project_create_requested = Signal()

    def __init__(self, store, project):
        super().__init__()
        self.store, self.project = store, Path(project)
        self.setObjectName("recentProjectsPage"); self.setMinimumHeight(0)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); page_scroll = QScrollArea(); page_scroll.setWidgetResizable(True); page_scroll.setFrameShape(QFrame.NoFrame); page_scroll.setMinimumHeight(0); page_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored); content = QWidget(); content.setObjectName("recentProjectsContent"); root = QVBoxLayout(content); root.setContentsMargins(24, 20, 24, 18); root.setSpacing(14); outer.addWidget(page_scroll); page_scroll.setWidget(content); self.content_scroll = page_scroll
        titlebar = QHBoxLayout(); copy = QVBoxLayout(); copy.setSpacing(3); title = QLabel("最近工程"); title.setObjectName("pageTitle"); copy.addWidget(title); subtitle = QLabel("继续之前的翻唱、语音生成和分离任务。"); subtitle.setObjectName("pageSubtitle"); copy.addWidget(subtitle); titlebar.addLayout(copy, 1); self.create_button = QPushButton("＋ 新建 AI 翻唱工程"); self.create_button.setObjectName("primaryButton"); self.create_button.clicked.connect(self.project_create_requested); titlebar.addWidget(self.create_button); root.addLayout(titlebar)
        body = QHBoxLayout(); body.setSpacing(14); projects = QFrame(); projects.setObjectName("recentProjectsList"); projects_layout = QVBoxLayout(projects); projects_layout.setContentsMargins(16, 14, 16, 14); head = QHBoxLayout(); head.addWidget(QLabel("工程", objectName="cardTitle")); head.addStretch(); refresh = QPushButton("最近编辑"); refresh.setObjectName("miniButton"); refresh.clicked.connect(self.refresh); head.addWidget(refresh); projects_layout.addLayout(head); self.grid = QGridLayout(); self.grid.setSpacing(12); projects_layout.addLayout(self.grid); projects_layout.addStretch(); body.addWidget(projects, 1)
        activity = QFrame(); activity.setObjectName("recentActivity"); activity.setFixedWidth(290); activity_layout = QVBoxLayout(activity); activity_layout.setContentsMargins(16, 14, 16, 14); activity_layout.addWidget(QLabel("最近活动", objectName="sectionLabel")); self.activity = QVBoxLayout(); activity_layout.addLayout(self.activity); activity_layout.addStretch(); body.addWidget(activity); root.addLayout(body, 1); self.refresh()

    def refresh(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        while self.activity.count():
            item = self.activity.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        entries = self.store.list_projects()
        if not entries:
            empty = QLabel("还没有本地工程。导入一首歌曲后会自动创建工程数据。"); empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignCenter); self.grid.addWidget(empty, 0, 0, 1, 3)
        for index, entry in enumerate(entries[:12]):
            path = Path(entry["path"]); name = entry.get("name") or path.name
            card = RecentProjectCard(path, name, "本地工程 · 点击继续编辑", index)
            card.open_requested.connect(self.project_selected.emit)
            self.grid.addWidget(card, index // 3, index % 3)
            note = QLabel(f"编辑工程：{name}\n本地项目"); note.setObjectName("recentActivityItem"); note.setToolTip(str(path)); note.setWordWrap(True); self.activity.addWidget(note)
        if not entries:
            self.activity.addWidget(QLabel("暂无本地活动记录。", objectName="hint"))
