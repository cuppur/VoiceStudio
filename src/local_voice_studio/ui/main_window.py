from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QGroupBox, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from ..paths import AppPaths
from ..storage import StudioStore
from .pages import GeneratePage, HistoryPage, SettingsPage, TrainingPage, VoiceLibraryPage
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
        super().__init__(); self.paths, self.store = paths, store; self.setWindowTitle("本地声音工坊"); self.resize(1180, 780); self.setMinimumSize(960, 640)
        projects = store.list_projects(); self.project = Path(projects[0]["path"]) if projects else store.create_project("默认项目")
        self.client = WorkerClient(paths, self); self._build(); self.client.start(); self.statusBar().showMessage("本地工作进程正在启动……")
        self.client.state_changed.connect(self._state); self.client.event.connect(self._worker_event)

    def _build(self) -> None:
        central = QWidget(); root = QHBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0); self.setCentralWidget(central)
        sidebar = QWidget(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(190); side_layout = QVBoxLayout(sidebar); brand = QLabel("本地声音工坊"); brand.setObjectName("brand"); side_layout.addWidget(brand); project = QLabel(self.project.name); project.setObjectName("projectName"); side_layout.addWidget(project)
        self.navigation = QListWidget(); self.navigation.setObjectName("navigation"); self.navigation.setFrameShape(QListWidget.NoFrame); side_layout.addWidget(self.navigation); version = QLabel("GPT-SoVITS V2ProPlus\n本机离线 · 无遥测"); version.setObjectName("sidebarFoot"); side_layout.addWidget(version); root.addWidget(sidebar)
        self.stack = QStackedWidget(); root.addWidget(self.stack, 1)
        self.generate_page = GeneratePage(self.store, self.project, self.client); self.voice_page = VoiceLibraryPage(self.store, self.project, self.client); self.training_page = TrainingPage(self.store, self.project, self.client); self.history_page = HistoryPage(self.store); self.settings_page = SettingsPage(self.paths, self.client)
        pages = [("生成语音", self.generate_page), ("声音库", self.voice_page), ("数据与训练", self.training_page), ("任务历史", self.history_page), ("设置", self.settings_page)]
        for name, page in pages: self.navigation.addItem(QListWidgetItem(name)); self.stack.addWidget(page)
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex); self.navigation.setCurrentRow(0)
        self.voice_page.profiles_changed.connect(self.generate_page.refresh_profiles); self.voice_page.profiles_changed.connect(self.training_page.refresh_profiles); self.training_page.profiles_changed.connect(self.generate_page.refresh_profiles); self.training_page.profiles_changed.connect(self.voice_page.refresh_profiles); self.voice_page.open_training_requested.connect(lambda: self.navigation.setCurrentRow(2)); self.generate_page.job_created.connect(lambda _: self.history_page.refresh()); self.training_page.job_created.connect(lambda _: self.history_page.refresh()); self.settings_page.install_requested.connect(self._open_setup)

    def _script_path(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS")) / "scripts" / "bootstrap_runtime.ps1"
        return Path(__file__).resolve().parents[3] / "scripts" / "bootstrap_runtime.ps1"

    def _open_setup(self) -> None:
        was_running = self.client.process.state() != QProcess.NotRunning
        if was_running: self.client.shutdown()
        dialog = SetupDialog(self._script_path(), self.paths, self); dialog.exec(); self.client.start(); self.settings_page.check_health()

    def _state(self, state: str) -> None:
        messages = {"running": "本地工作进程已启动", "stopped": "本地工作进程已停止"}; self.statusBar().showMessage(messages.get(state, state), 5000)

    def _worker_event(self, request_id: str, event: str, payload: dict) -> None:
        if request_id == "worker" and event == "ready": self.statusBar().showMessage("本地工作进程就绪", 5000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.client.shutdown(); super().closeEvent(event)


STYLE = """
QWidget { font-family: "Microsoft YaHei UI"; font-size: 14px; color: #172033; }
QMainWindow, QStackedWidget { background: #f6f8fc; }
#sidebar { background: #18233a; }
#brand { color: white; font-size: 21px; font-weight: 700; padding: 20px 14px 4px 14px; }
#projectName { color: #9fb0cc; padding: 0 14px 16px 14px; }
#sidebarFoot { color: #8394b2; padding: 16px; font-size: 12px; }
#navigation { background: transparent; color: #cbd6e8; outline: none; }
#navigation::item { padding: 13px 18px; margin: 2px 8px; border-radius: 7px; }
#navigation::item:selected { background: #2d6cdf; color: white; }
QStackedWidget > QWidget { background: #f6f8fc; }
#pageTitle { font-size: 26px; font-weight: 700; color: #111827; margin-bottom: 8px; }
#hint { color: #667085; }
#recordPrompt { background: white; border: 1px solid #d7deea; border-radius: 8px; padding: 20px; font-size: 18px; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget { background: white; border: 1px solid #d7deea; border-radius: 6px; padding: 6px; selection-background-color: #2d6cdf; }
QGroupBox { border: 1px solid #d7deea; border-radius: 8px; margin-top: 12px; padding: 12px; background: white; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QPushButton { background: white; border: 1px solid #c9d2e1; border-radius: 6px; padding: 8px 14px; }
QPushButton:hover { background: #eef3fb; }
QPushButton:disabled { color: #99a2b1; background: #eef0f4; }
#primaryButton { background: #2d6cdf; border-color: #2d6cdf; color: white; font-weight: 600; }
#primaryButton:hover { background: #245fc8; }
QProgressBar { border: 1px solid #d7deea; border-radius: 5px; text-align: center; background: white; }
QProgressBar::chunk { background: #2d6cdf; border-radius: 4px; }
"""
