from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl, QTimer, QSize
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QGroupBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSizePolicy, QStackedWidget, QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QVBoxLayout, QWidget,
)

from ..paths import AppPaths
from ..storage import StudioStore
from .project_session import ProjectSession
from .simple_pages import MyVoicesPage, OneClickGeneratePage, OneClickTrainingPage, SimpleSettingsPage, TaskCenterDialog, TaskDrawer
from .cover_page import CoverPage
from .theme import load_theme
from .worker_client import WorkerClient
from .audio import PreviewAudioController
from .audio.global_player import GlobalPlayerSession
from .html_pages import HtmlPage, RuntimePanel, SeparationStudioPage, ExportsStudioPage, RecentProjectsPage
from .prototype_icons import prototype_icon


class GlobalPlayerBar(QFrame):
    """Persistent real-audio transport for the shared desktop player."""
    def __init__(self, session: GlobalPlayerSession, parent=None):
        super().__init__(parent); self.session = session; self._visual_preview = False; self.setObjectName("globalPlayerBar")
        row = QHBoxLayout(self); row.setContentsMargins(16, 8, 16, 8); row.setSpacing(10)
        self.title = QLabel("未加载试听"); self.title.setObjectName("playerTitle"); row.addWidget(self.title, 1)
        self.side = QLabel("—"); self.side.setObjectName("playerSide"); row.addWidget(self.side)
        self.play_button = QPushButton("播放"); self.play_button.setObjectName("playerPlay"); self.play_button.clicked.connect(self._toggle); row.addWidget(self.play_button)
        self.slider = QSlider(Qt.Horizontal); self.slider.setRange(0, 0); self.slider.setObjectName("playerSeek"); self.slider.sliderMoved.connect(self._seek); row.addWidget(self.slider, 3)
        self.time = QLabel("00:00 / 00:00"); row.addWidget(self.time)
        self.timer = QTimer(self); self.timer.setInterval(200); self.timer.timeout.connect(self._tick); self.timer.start()
    def _channel(self):
        return self.session.controller.channels.get(self.session.controller.master_role)
    def _toggle(self):
        if self._visual_preview:
            self.play_button.setText("Ⅱ" if self.play_button.text() == "▶" else "▶")
            self.side.setText("视觉预览 · 无真实音频")
            return
        if self.session.controller.playing: self.session.pause(); self.play_button.setText("播放")
        else:
            self.session.play()
            self.play_button.setText("暂停" if self.session.controller.playing else "播放")
            if not self.session.controller.playing: self.title.setText("请先在创作页面载入试听音频")
    def _seek(self, value):
        if self._visual_preview:
            self.time.setText(f"{self._fmt(value)} / {self._fmt(self.slider.maximum())}"); return
        self.session.seek(int(value))
    def _tick(self):
        if self._visual_preview:
            return
        ch = self._channel()
        if ch is None:
            self.slider.setRange(0, 0); self.time.setText("00:00 / 00:00"); self.play_button.setText("播放"); return
        player = ch.player; pos = ch.position()
        duration = int(player.duration()) if callable(getattr(player, "duration", None)) else 0
        self.slider.setRange(0, max(0, duration))
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True); self.slider.setValue(min(pos, max(0, self.slider.maximum()))); self.slider.blockSignals(False)
        source = player.source().toLocalFile() if callable(getattr(player, "source", None)) else ""
        self.title.setText(Path(source).name if source else "未加载试听")
        self.time.setText(f"{self._fmt(pos)} / {self._fmt(duration)}")
        self.play_button.setText("暂停" if self.session.controller.playing else "播放")
    @staticmethod
    def _fmt(ms):
        sec=max(0,int(ms)//1000); return f"{sec//60:02d}:{sec%60:02d}"
    def sync(self, title="", side=""):
        self.title.setText(title or "未加载试听"); self.side.setText(side or "—")

    def set_visual_sample(self, title="落日信号 · AI Preview", side="Studio Voice 01", current=85000, total=269000):
        """Show the HTML reference transport without claiming an audio file exists."""
        self._visual_preview = True
        self.title.setText(title); self.side.setText(side); self.play_button.setText("▶")
        self.slider.setRange(0, int(total)); self.slider.setValue(int(current)); self.time.setText(f"{self._fmt(current)} / {self._fmt(total)}")


class SetupDialog(QDialog):
    STEP_NAMES = (
        "私有 Python 3.11", "固定提交 GPT-SoVITS", "PyTorch 2.7.1+cu128",
        "GPT-SoVITS 依赖", "FFmpeg 与预训练模型", "Torch/CUDA/GPU 验证", "安装清单",
    )
    STATE_TEXT = {"waiting": "等待", "running": "正在执行", "completed": "已完成", "skipped": "已完成（跳过）", "retrying": "重试中", "failed": "失败"}
    ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

    def __init__(self, script: Path, paths: AppPaths, parent=None):
        super().__init__(parent); self.setWindowTitle("安装本地引擎"); self.resize(780, 650); self.process = QProcess(self)
        layout = QVBoxLayout(self); info = QLabel("将安装私有 Python 3.11/CUDA 12.8 环境，并下载固定版本的 GPT-SoVITS V2ProPlus。不会修改系统 PATH。"); info.setWordWrap(True); layout.addWidget(info)
        self.optional_tools = QCheckBox("同时安装伴奏分离/去混响模型（参考素材含配乐时需要）"); self.optional_tools.setChecked(True); layout.addWidget(self.optional_tools)
        status_box = QGroupBox("安装步骤"); status_layout = QVBoxLayout(status_box); self.step_labels = []
        for number, name in enumerate(self.STEP_NAMES, 1):
            label = QLabel(f"{number}. {name} — 等待"); self.step_labels.append(label); status_layout.addWidget(label)
        layout.addWidget(status_box)
        self.summary = QLabel(""); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); layout.addWidget(self.progress)
        log_box = QGroupBox("详细日志"); log_layout = QVBoxLayout(log_box); self.log = QPlainTextEdit(); self.log.setReadOnly(True); log_layout.addWidget(self.log); layout.addWidget(log_box, 1)
        row = QHBoxLayout(); self.start_button = QPushButton("开始安装"); self.start_button.setObjectName("primaryButton"); self.start_button.clicked.connect(self.start); self.close_button = QPushButton("关闭"); self.close_button.clicked.connect(self.reject); row.addWidget(self.start_button); row.addStretch(); row.addWidget(self.close_button); layout.addLayout(row)
        self.script, self.paths, self._line_buffer, self._current_step = script, paths, "", 0
        self.process.setProcessChannelMode(QProcess.MergedChannels); self.process.readyReadStandardOutput.connect(self._read); self.process.finished.connect(self._finished)

    def start(self) -> None:
        if not self.script.exists(): QMessageBox.critical(self, "本地声音工坊", f"安装脚本不存在：{self.script}"); return
        self.start_button.setEnabled(False); self.close_button.setEnabled(False); self.log.clear(); self.summary.setText(""); self.progress.setValue(0); self._line_buffer = ""; self._current_step = 0
        for number, name in enumerate(self.STEP_NAMES, 1): self.step_labels[number - 1].setText(f"{number}. {name} — 等待")
        arguments = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), "-DataRoot", str(self.paths.data_root)]
        if self.optional_tools.isChecked(): arguments.extend(("-DownloadUVR5", "-DownloadRoFormer"))
        env = QProcessEnvironment.systemEnvironment(); env.insert("PYTHONUTF8", "1"); env.insert("PYTHONIOENCODING", "utf-8"); self.process.setProcessEnvironment(env)
        self.process.setProgram("powershell.exe"); self.process.setArguments(arguments); self.process.start()

    def _read(self) -> None:
        value = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        value = self.ANSI_RE.sub("", value).replace("\b", "")
        self._line_buffer += value
        lines = self._line_buffer.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self._line_buffer = lines.pop()
        visible = []
        for line in lines:
            if line.startswith("LVS_EVENT "):
                try: self._apply_event(json.loads(line[len("LVS_EVENT "):]))
                except (ValueError, TypeError): visible.append(line)
            else:
                visible.append(line)
                match = re.search(r"(?<!\d)(100|[1-9]?\d)\s*%", line)
                if match and self._current_step:
                    self.progress.setValue(min(99, round((self._current_step - 1) * 100 / 7 + int(match.group(1)) / 7)))
        if visible:
            self.log.moveCursor(QTextCursor.End); self.log.insertPlainText("\n".join(visible) + "\n")

    def _apply_event(self, event: dict) -> None:
        if event.get("type") != "step": return
        step, state = int(event["step"]), str(event["state"]); self._current_step = step
        if 1 <= step <= len(self.STEP_NAMES):
            self.step_labels[step - 1].setText(f"{step}. {self.STEP_NAMES[step - 1]} — {self.STATE_TEXT.get(state, state)}")
        if state in {"completed", "skipped"}: self.progress.setValue(round(step * 100 / 7))
        elif state == "running": self.progress.setValue(round((step - 1) * 100 / 7))
        elif state == "failed": self.summary.setText(f"{event.get('message', '安装失败')}。可复制详细日志或重新安装。")

    def _finished(self, code: int, _status) -> None:
        if self._line_buffer.strip(): self.log.appendPlainText(self.ANSI_RE.sub("", self._line_buffer).replace("\b", ""))
        self.progress.setValue(100 if code == 0 else self.progress.value()); self.start_button.setEnabled(True); self.close_button.setEnabled(True)
        if code == 0: self.summary.setText("安装完成。本地引擎已通过实机验证。"); self.log.appendPlainText("\n安装完成。")
        else:
            if not self.summary.text(): self.summary.setText(f"本地引擎安装失败（退出码 {code}）。可复制详细日志或重新安装。")
            self.log.appendPlainText(f"\n安装失败，退出码 {code}。原始异常已保留在详细日志中。")


class MainWindow(QMainWindow):
    def __init__(self, paths: AppPaths, store: StudioStore, client=None):
        super().__init__(); self.paths, self.store = paths, store; self.setWindowTitle("VoiceStudio · 本地 AI 声音创作工作室"); self.resize(1440, 900); self.setMinimumSize(1280, 720)
        self.session = ProjectSession(store, self); self.project = self.session.current
        self.client = client or WorkerClient(paths, self); self.global_player = GlobalPlayerSession(PreviewAudioController.create_qt(self)); self._build(); self.session.project_changed.connect(self._switch_project); self.client.start(); self.statusBar().showMessage("本地工作进程正在启动……"); self.statusBar().hide()
        self.client.state_changed.connect(self._state); self.client.event.connect(self._worker_event)

    def _build(self) -> None:
        central = QWidget(); central.setObjectName("appShell"); root = QVBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0); self.setCentralWidget(central)
        header = QFrame(); header.setObjectName("appHeader"); header.setFixedHeight(78); h = QHBoxLayout(header); h.setContentsMargins(20, 0, 20, 0); h.setSpacing(16); self._header_layout = h
        brand_box = QWidget(); self.brand_box = brand_box; brand_box.setFixedWidth(205); brand_row = QHBoxLayout(brand_box); brand_row.setContentsMargins(0, 0, 0, 0); brand_row.setSpacing(10)
        mark = QLabel(); mark.setObjectName("brandMark"); mark.setFixedSize(39, 39); mark.setAlignment(Qt.AlignCenter); mark.setPixmap(prototype_icon("brand", 22).pixmap(22, 22)); brand_row.addWidget(mark)
        brand_text = QVBoxLayout(); brand_text.setSpacing(2); brand = QLabel("VoiceStudio"); self.brand_label = brand; brand.setObjectName("brand"); brand.setContentsMargins(0, 0, 0, 0); brand.setFixedWidth(130); brand_text.addWidget(brand)
        sub = QLabel("LOCAL AI AUDIO"); sub.setObjectName("brandSub"); sub.setContentsMargins(0, 0, 0, 0); sub.setFixedWidth(130); brand_text.addWidget(sub); brand_row.addLayout(brand_text); h.addWidget(brand_box)
        self.navigation = QListWidget(); self.navigation.setObjectName("topNavigation"); self.navigation.setAccessibleName("主导航"); self.navigation.setFlow(QListWidget.LeftToRight); self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.navigation.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.navigation.setFrameShape(QListWidget.NoFrame); self.navigation.setFixedHeight(42); h.addWidget(self.navigation, 1)
        self.navigation.setFixedHeight(48); self.navigation.setWrapping(False); self.navigation.setIconSize(QSize(16, 16))
        self.topbar_status = QLabel("本地工作进程 · 启动中"); self.topbar_status.hide()
        gpu_button = QPushButton("GPU\n点击查看任务"); gpu_button.setObjectName("gpuPill"); gpu_button.clicked.connect(self._open_task_center); h.addWidget(gpu_button)
        self.project_button = QToolButton(); self.project_button.setObjectName("projectPicker"); self.project_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); self.project_button.setPopupMode(QToolButton.InstantPopup); self.project_menu = QMenu(self.project_button); self.project_menu.aboutToShow.connect(self._refresh_project_menu); self.project_button.setMenu(self.project_menu); h.addWidget(self.project_button)
        h.removeWidget(self.project_button); self.project_button.hide()
        recent = QPushButton("最近工程"); recent.setObjectName("topbarButton"); recent.clicked.connect(lambda: self.navigation.setCurrentRow(6)); h.addWidget(recent)
        quick_import = QPushButton("＋ 导入"); quick_import.setObjectName("primaryButton"); quick_import.clicked.connect(self._quick_import); h.addWidget(quick_import)
        settings_button = QPushButton(); settings_button.setObjectName("topbarButton"); settings_button.setIcon(self._navigation_icon("settings.svg")); settings_button.setToolTip("设置"); settings_button.setAccessibleName("设置"); settings_button.setFixedWidth(44); settings_button.clicked.connect(lambda: self.navigation.setCurrentRow(7)); h.addWidget(settings_button)
        root.addWidget(header)
        content = QHBoxLayout(); content.setContentsMargins(0, 0, 0, 0); content.setSpacing(0)
        self.stack = QStackedWidget(); self.stack.setObjectName("pageStack"); self.stack.setMinimumHeight(0); self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored); content.addWidget(self.stack, 1)
        root.addLayout(content, 1)
        self.global_player_bar = GlobalPlayerBar(self.global_player); root.addWidget(self.global_player_bar)
        labels = ["AI 翻唱", "文字生成", "我的声音", "训练声音", "音频分离", "导出中心", "最近工程", "设置"]
        for name in labels:
            item = QListWidgetItem(name); item.setSizeHint(QSize(104, 38)); self.navigation.addItem(item)
        self.navigation.item(6).setHidden(True); self.navigation.item(7).setHidden(True)
        for index, name in enumerate(("cover", "tts", "voices", "train", "separator", "exports")): self.navigation.item(index).setIcon(prototype_icon(name))
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex); self.navigation.setCurrentRow(0)
        self._build_project_pages(); self._update_project_button()
        self._apply_header_density()

    def _apply_header_density(self) -> None:
        """Mirror the prototype's compact header breakpoint without hiding pages."""
        if not hasattr(self, "navigation"):
            return
        compact = self.width() <= 1300
        self.brand_box.setFixedWidth(175 if compact else 205)
        self.brand_label.setStyleSheet("font-size:17px;" if compact else "")
        self._header_layout.setContentsMargins(16 if compact else 20, 0, 16 if compact else 20, 0)
        self._header_layout.setSpacing(8 if compact else 16)
        self.navigation.setIconSize(QSize(14 if compact else 16, 14 if compact else 16))
        item_width = 92 if compact else 104
        for index in range(self.navigation.count()):
            self.navigation.item(index).setSizeHint(QSize(item_width, 38))
        self.navigation.setStyleSheet("QListWidget::item { font-size: 9px; padding: 0 2px; }" if compact else "")

    @staticmethod
    def _navigation_icon(name: str) -> QIcon:
        if getattr(sys, "frozen", False):
            path = Path(getattr(sys, "_MEIPASS")) / "local_voice_studio" / "ui" / "resources" / "icons" / name
        else:
            path = Path(__file__).with_name("resources") / "icons" / name
        return QIcon(str(path))

    def _build_project_pages(self) -> None:
        row = max(0, self.navigation.currentRow())
        if not hasattr(self, "_page_groups"): self._page_groups = {}
        key = str(self.project.resolve())
        while self.stack.count():
            page = self.stack.widget(0)
            page._background_project = True
            self.stack.removeWidget(page); page.hide()
        if key in self._page_groups:
            pages, self.global_player = self._page_groups[key]
            self._install_project_pages(pages, row)
            return
        if self._page_groups:
            self.global_player = GlobalPlayerSession(PreviewAudioController.create_qt(self))
        self.cover_page = CoverPage(self.paths, self.store, self.project, self.client, global_player=self.global_player)
        self.generate_page = OneClickGeneratePage(self.store, self.project, self.client)
        self.voice_page = MyVoicesPage(self.store, self.project)
        self.training_page = OneClickTrainingPage(self.store, self.project, self.client)
        self.settings_page = SimpleSettingsPage(self.paths, self.store, self.project, self.client)
        self.separator_page = SeparationStudioPage(self.store, self.project)
        self.exports_page = ExportsStudioPage(self.store, self.project)
        self.recent_page = RecentProjectsPage(self.store, self.project)
        self.separator_page.audio_selected.connect(self._import_for_separation)
        self.recent_page.project_selected.connect(lambda path: self.session.activate(Path(path)))
        self.recent_page.project_create_requested.connect(self._new_project)
        pages = (self.cover_page, self.generate_page, self.voice_page, self.training_page, self.separator_page, self.exports_page, self.recent_page, self.settings_page)
        self._page_groups[key] = (pages, self.global_player)
        self._install_project_pages(pages, row)
        self.voice_page.profiles_changed.connect(self.generate_page.refresh_profiles); self.voice_page.profiles_changed.connect(self.cover_page.refresh_profiles); self.training_page.profiles_changed.connect(self.generate_page.refresh_profiles); self.training_page.profiles_changed.connect(self.cover_page.refresh_profiles); self.training_page.profiles_changed.connect(self.voice_page.refresh); self.voice_page.generate_requested.connect(self._use_profile); self.voice_page.retrain_requested.connect(self._retrain_profile); self.generate_page.train_requested.connect(lambda: self.navigation.setCurrentRow(3)); self.settings_page.install_requested.connect(self._open_setup)

    def _install_project_pages(self, pages, row):
        names = ("cover_page", "generate_page", "voice_page", "training_page", "separator_page", "exports_page", "recent_page", "settings_page")
        for name, page in zip(names, pages):
            setattr(self, name, page); page._background_project = False; self.stack.addWidget(page)
        self.global_player_bar.session = self.global_player
        self.stack.setCurrentIndex(row)

    def _update_project_button(self) -> None:
        self.project_button.setText(self.session.display_name(self.project) + "  ▾")

    def _quick_import(self):
        self.navigation.setCurrentRow(0)
        self.cover_page.import_song()

    def _activate_dashboard_project(self, path):
        try:
            self.session.activate(Path(path)); self.navigation.setCurrentRow(0)
        except (OSError, ValueError) as exc: QMessageBox.warning(self, "无法打开工程", str(exc))

    def _import_for_separation(self, path: str) -> None:
        self.navigation.setCurrentRow(0)
        self.cover_page.set_song(path)
        self.cover_page.separate_song()

    def _refresh_project_menu(self) -> None:
        self.project_menu.clear()
        for item in self.session.projects():
            path = Path(item["path"]); action = self.project_menu.addAction(str(item.get("name") or path.name)); action.setCheckable(True); action.setChecked(path.resolve() == self.project.resolve()); action.triggered.connect(lambda _checked=False, value=path: self.session.activate(value))
        self.project_menu.addSeparator(); new_project = self.project_menu.addAction("＋ 新建项目"); new_project.triggered.connect(self._new_project); open_project = self.project_menu.addAction("打开已有项目…"); open_project.triggered.connect(self._open_project); rename = self.project_menu.addAction("重命名当前项目"); rename.triggered.connect(self._rename_project); folder = self.project_menu.addAction("打开项目文件夹"); folder.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project))))

    def _new_project(self) -> None:
        name, ok = QInputDialog.getText(self, "新建项目", "项目名称", text="我的有声项目")
        if ok and name.strip(): self.session.create(name)

    def _open_project(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "打开 VoiceStudio 项目", str(self.paths.projects_root))
        if not value: return
        try: self.session.open_existing(Path(value))
        except Exception as exc: QMessageBox.critical(self, "VoiceStudio", f"无法打开项目：{exc}")

    def _rename_project(self) -> None:
        value, ok = QInputDialog.getText(self, "重命名项目", "新名称", text=self.session.display_name(self.project))
        if not ok: return
        try: self.session.rename(value); self._update_project_button()
        except Exception as exc: QMessageBox.critical(self, "VoiceStudio", str(exc))

    def _switch_project(self, project: Path) -> None:
        self.global_player.pause()
        self.generate_page.player.pause()
        self.project = Path(project); self._build_project_pages(); self._update_project_button(); self.statusBar().showMessage(f"已切换到项目：{self.session.display_name(self.project)}", 4000)

    def _open_task_center(self) -> None:
        if hasattr(self, "task_drawer") and self.task_drawer.isVisible():
            self.task_drawer.hide(); return
        if not hasattr(self, "task_drawer"):
            self.task_drawer = TaskDrawer(self.store, self.client, self)
            self.task_drawer.full_center_requested.connect(self._open_full_task_center)
        self.task_drawer.show_for_parent()

    def _open_full_task_center(self) -> None:
        dialog = TaskCenterDialog(self.store, self.client, self)
        dialog.retry_generation_requested.connect(self._retry_generation)
        dialog.exec()

    def resizeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._apply_header_density()
        if hasattr(self, "task_drawer") and self.task_drawer.isVisible():
            self.task_drawer.show_for_parent()

    def _retry_generation(self, job) -> None:
        profile_id = str(job.payload.get("profile_id", ""))
        for entry in self.session.projects():
            project = Path(entry["path"])
            if any(profile.id == profile_id for profile in self.store.list_profiles(project)):
                self.session.activate(project)
                self.navigation.setCurrentRow(1)
                self.generate_page._retry(job)
                return
        QMessageBox.warning(self, "无法重试", "找不到原任务使用的声音；声音可能已删除，请选择已授权且训练完成的声音重新生成。")

    def _use_profile(self, profile_id: str) -> None:
        self.generate_page.refresh_profiles()
        if self.generate_page.profile.findData(profile_id) < 0:
            QMessageBox.warning(self, "声音尚不可用于生成", "此声音缺少完整授权、已训练模型或可用参考素材。请使用“追加训练”补齐声音后重试。")
            return
        self.generate_page.select_profile(profile_id); self.navigation.setCurrentRow(1)

    def _retrain_profile(self, profile_id: str) -> None:
        self.training_page.reset_for_profile(profile_id); self.navigation.setCurrentRow(3)

    def _script_path(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS")) / "scripts" / "bootstrap_runtime.ps1"
        return Path(__file__).resolve().parents[3] / "scripts" / "bootstrap_runtime.ps1"

    def _open_setup(self) -> None:
        was_running = self.client.process.state() != QProcess.NotRunning
        if was_running: self.client.shutdown()
        dialog = SetupDialog(self._script_path(), self.paths, self); dialog.exec(); self.client.start(); self.settings_page.check_health()

    def _state(self, state: str) -> None:
        messages = {"running": "本地工作进程 · 就绪", "stopped": "本地工作进程 · 已停止"}; message = messages.get(state, state); self.statusBar().showMessage(message, 5000); self.topbar_status.setText(message)

    def _worker_event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id == "worker" and event == "ready": self.statusBar().showMessage("本地工作进程就绪", 5000); self.topbar_status.setText("本地工作进程 · 就绪")
        for pages, _player in getattr(self, "_page_groups", {}).values():
            pages[0].handle_worker_event(request_id, event, payload)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.client.shutdown()
        for pages, _player in getattr(self, "_page_groups", {}).values():
            for page in pages:
                if hasattr(page, "release_resources"): page.release_resources()
        super().closeEvent(event)


STYLE = load_theme()


class _NavigationDelegate(QStyledItemDelegate):
    """Keep the existing five-row navigation API while showing groups."""

    _groups = {0: "创作", 2: "声音", 4: "工具"}

    def sizeHint(self, option: QStyleOptionViewItem, index):
        size = super().sizeHint(option, index)
        if index.row() in self._groups:
            size.setHeight(size.height() + 28)
        return size

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        group = self._groups.get(index.row())
        item_option = QStyleOptionViewItem(option)
        if group:
            painter.save()
            painter.setPen("#697180")
            painter.setFont(option.font)
            painter.drawText(option.rect.adjusted(18, 3, -10, -9), Qt.AlignLeft | Qt.AlignVCenter, group)
            painter.restore()
            item_option.rect = option.rect.adjusted(0, 28, 0, 0)
        super().paint(painter, item_option, index)
