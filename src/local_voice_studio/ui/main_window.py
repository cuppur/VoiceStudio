from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QGroupBox, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from ..paths import AppPaths
from ..storage import StudioStore
from .project_session import ProjectSession
from .simple_pages import MyVoicesPage, OneClickGeneratePage, OneClickTrainingPage, SimpleSettingsPage, TaskCenterDialog
from .cover_page import CoverPage
from .theme import load_theme
from .worker_client import WorkerClient


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
        if self.optional_tools.isChecked(): arguments.append("-DownloadUVR5")
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
    def __init__(self, paths: AppPaths, store: StudioStore):
        super().__init__(); self.paths, self.store = paths, store; self.setWindowTitle("VoiceStudio · 本地 AI 声音创作工作室"); self.resize(1440, 900); self.setMinimumSize(1280, 720)
        self.session = ProjectSession(store, self); self.project = self.session.current
        self.client = WorkerClient(paths, self); self._build(); self.session.project_changed.connect(self._switch_project); self.client.start(); self.statusBar().showMessage("本地工作进程正在启动……")
        self.client.state_changed.connect(self._state); self.client.event.connect(self._worker_event)

    def _build(self) -> None:
        central = QWidget(); root = QHBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0); self.setCentralWidget(central)
        sidebar = QWidget(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(220); side_layout = QVBoxLayout(sidebar); side_layout.setContentsMargins(0, 0, 0, 0); brand = QLabel("VoiceStudio"); brand.setObjectName("brand"); side_layout.addWidget(brand); brand_sub = QLabel("LOCAL AI AUDIO STUDIO"); brand_sub.setObjectName("brandSub"); side_layout.addWidget(brand_sub); self.project_button = QToolButton(); self.project_button.setObjectName("projectPicker"); self.project_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); self.project_button.setPopupMode(QToolButton.InstantPopup); self.project_menu = QMenu(self.project_button); self.project_menu.aboutToShow.connect(self._refresh_project_menu); self.project_button.setMenu(self.project_menu); side_layout.addWidget(self.project_button)
        self.navigation = QListWidget(); self.navigation.setObjectName("navigation"); self.navigation.setFrameShape(QListWidget.NoFrame); side_layout.addWidget(self.navigation); task_center = QPushButton("任务中心"); task_center.setObjectName("sidebarButton"); task_center.clicked.connect(self._open_task_center); side_layout.addWidget(task_center); version = QLabel("GPT-SoVITS V2ProPlus\n完全本地 · 无遥测"); version.setObjectName("sidebarFoot"); side_layout.addWidget(version); root.addWidget(sidebar)
        workspace = QWidget(); workspace.setObjectName("workspace"); workspace_layout = QVBoxLayout(workspace); workspace_layout.setContentsMargins(0, 0, 0, 0); workspace_layout.setSpacing(0)
        topbar = QFrame(); topbar.setObjectName("topbar"); topbar_layout = QHBoxLayout(topbar); topbar_layout.setContentsMargins(26, 0, 26, 0); topbar_layout.setSpacing(8)
        topbar_layout.addWidget(QLabel("创作")); topbar_layout.addWidget(QLabel("›")); current_title = QLabel("AI 翻唱工作台"); current_title.setObjectName("topbarTitle"); topbar_layout.addWidget(current_title); topbar_layout.addStretch()
        self.topbar_status = QLabel("本地工作进程 · 启动中"); self.topbar_status.setObjectName("topbarStatus"); topbar_layout.addWidget(self.topbar_status)
        settings_button = QPushButton("设置"); settings_button.setObjectName("topbarButton"); settings_button.clicked.connect(lambda: self.navigation.setCurrentRow(4)); topbar_layout.addWidget(settings_button)
        workspace_layout.addWidget(topbar)
        self.stack = QStackedWidget(); workspace_layout.addWidget(self.stack, 1); root.addWidget(workspace, 1)
        for icon, name in (("cover.svg", "AI 翻唱"), ("generate.svg", "文字生成"), ("voices.svg", "我的声音"), ("training.svg", "训练声音"), ("settings.svg", "设置")):
            item = QListWidgetItem(self._navigation_icon(icon), name)
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex); self.navigation.setCurrentRow(0)
        self._build_project_pages(); self._update_project_button()

    @staticmethod
    def _navigation_icon(name: str) -> QIcon:
        if getattr(sys, "frozen", False):
            path = Path(getattr(sys, "_MEIPASS")) / "local_voice_studio" / "ui" / "resources" / "icons" / name
        else:
            path = Path(__file__).with_name("resources") / "icons" / name
        return QIcon(str(path))

    def _build_project_pages(self) -> None:
        row = max(0, self.navigation.currentRow())
        while self.stack.count():
            page = self.stack.widget(0)
            if hasattr(page, "release_resources"): page.release_resources()
            self.stack.removeWidget(page); page.deleteLater()
        self.cover_page = CoverPage(self.paths, self.store, self.project, self.client); self.generate_page = OneClickGeneratePage(self.store, self.project, self.client); self.voice_page = MyVoicesPage(self.store, self.project); self.training_page = OneClickTrainingPage(self.store, self.project, self.client); self.settings_page = SimpleSettingsPage(self.paths, self.store, self.project, self.client)
        for page in (self.cover_page, self.generate_page, self.voice_page, self.training_page, self.settings_page): self.stack.addWidget(page)
        self.stack.setCurrentIndex(row)
        self.voice_page.profiles_changed.connect(self.generate_page.refresh_profiles); self.voice_page.profiles_changed.connect(self.cover_page.refresh_profiles); self.training_page.profiles_changed.connect(self.generate_page.refresh_profiles); self.training_page.profiles_changed.connect(self.cover_page.refresh_profiles); self.training_page.profiles_changed.connect(self.voice_page.refresh); self.voice_page.generate_requested.connect(self._use_profile); self.voice_page.retrain_requested.connect(self._retrain_profile); self.generate_page.train_requested.connect(lambda: self.navigation.setCurrentRow(3)); self.settings_page.install_requested.connect(self._open_setup)

    def _update_project_button(self) -> None:
        self.project_button.setText(self.session.display_name(self.project) + "  ▾")

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
        self.project = Path(project); self._build_project_pages(); self._update_project_button(); self.statusBar().showMessage(f"已切换到项目：{self.session.display_name(self.project)}", 4000)

    def _open_task_center(self) -> None:
        TaskCenterDialog(self.store, self).exec()

    def _use_profile(self, profile_id: str) -> None:
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
        if hasattr(self, "cover_page"):
            self.cover_page.handle_worker_event(request_id, event, payload)

    def closeEvent(self, event: QCloseEvent) -> None:
        for index in range(self.stack.count()):
            page = self.stack.widget(index)
            if hasattr(page, "release_resources"): page.release_resources()
        self.client.shutdown(); super().closeEvent(event)


STYLE = load_theme()
