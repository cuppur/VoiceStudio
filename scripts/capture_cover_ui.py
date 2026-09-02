from __future__ import annotations

import math
import hashlib
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from local_voice_studio.models import SourceAsset, VoiceProfile
from local_voice_studio.singing.models import SingingModelVersion
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.cover.separation import SongSeparationPipeline
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow, STYLE
from local_voice_studio.ui.worker_client import WorkerClient


def sample_song(root: Path) -> Path:
    path = root / "星光练习曲.wav"
    rate = 22_050
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", ""))
        frames = bytearray()
        for index in range(rate * 12):
            envelope = 0.25 + 0.65 * abs(math.sin(index / rate * math.pi / 2))
            value = int(17_000 * envelope * math.sin(2 * math.pi * (180 + index / rate * 18) * index / rate))
            frames.extend(struct.pack("<h", value))
        stream.writeframes(frames)
    path.with_suffix(".lrc").write_text("[00:00.00]夜色落在安静的窗台\n[00:03.00]让声音沿着星光醒来\n[00:06.00]这一刻只属于音乐\n[00:09.00]下一段旅程即将展开\n", encoding="utf-8")
    return path


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="voice-studio-capture-"))
    paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data" / "studio.sqlite3")
    # Reuse the installed private media tools without copying them.
    installed = AppPaths.default()
    paths = AppPaths(installed.data_root, paths.projects_root, installed.runtime_root, installed.engine_root, installed.models_root, installed.logs_root, paths.database)
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    store = StudioStore(paths); project = store.create_project("AI 翻唱演示")
    profile = VoiceProfile("澄澈女声", True, consent_record="本人声音或已取得明确授权", consent_confirmed_at="2026-09-02T00:00:00Z")
    store.save_profile(project, profile)
    app = QApplication.instance() or QApplication(sys.argv); app.setStyle("Fusion")
    cjk_font = "Microsoft YaHei UI"
    if cjk_font not in QFontDatabase.families():
        # Keep CI/offscreen capture usable when the Windows CJK font is not
        # installed; production machines still use the intended font.
        cjk_font = "Arial"
    app.setFont(QFont(cjk_font, 10)); app.setStyleSheet(STYLE)
    WorkerClient.start = lambda self: None; WorkerClient.shutdown = lambda self: None
    window = MainWindow(paths, store); window.show()
    output = Path(__file__).resolve().parents[1] / "docs" / "screenshots"; output.mkdir(parents=True, exist_ok=True)
    def wait_load():
        loop = QEventLoop(); QTimer.singleShot(5000, loop.quit)
        for thread in tuple(window.cover_page._threads): thread.finished.connect(loop.quit)
        loop.exec(); app.processEvents()
    def capture(name: str, width: int, height: int):
        window.resize(width, height); app.processEvents()
        image = window.grab().toImage()
        # grab() is in physical pixels on high-DPI Windows.  Normalize the
        # saved artifact to the requested logical viewport dimensions.
        image = image.scaled(width, height)
        if image.size().width() != width or image.size().height() != height:
            raise RuntimeError(f"capture size mismatch: {image.size().width()}x{image.size().height()}")
        image.save(str(output / name))

    def capture_model_management():
        def grab_dialog():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QDialog):
                window.resize(1280, 720); app.processEvents()
                # Grab the real modal QDialog itself (rather than recreating
                # its contents), normalized to the artifact's requested size.
                image = dialog.grab().toImage().scaled(1280, 720)
                image.save(str(output / "phase3.1-singing-model-management-1280x720.png"))
                dialog.accept()
        QTimer.singleShot(250, grab_dialog)
        window.training_page.singing_manage_button.click()
    capture("phase2-empty-1440x900.png", 1440, 900)
    song = sample_song(root); window.cover_page.set_song(str(song)); wait_load()
    capture("phase2-imported-1440x900.png", 1440, 900)
    capture("phase2-imported-1280x720.png", 1280, 720)
    cover = window.cover_page.cover_project; cover.attest_rights(True)
    window.cover_page.rights_state.setText("歌曲权利：已确认")
    window.cover_page._update_cover_button()
    result = SongSeparationPipeline(project, paths=paths).separate(cover.id, cover.source_relative_path, cover.source_sha256)
    window.cover_page._load_track(1, Path(result["vocal_path"])); window.cover_page._load_track(2, Path(result["instrumental_path"])); wait_load()
    capture("phase2-separated-1440x900.png", 1440, 900)
    capture("phase2-separated-1280x720.png", 1280, 720)
    # Phase 3.1 captures are driven by persisted product records and normal
    # refresh/event paths.  No label text or button state is overwritten.
    training_audio = project / "raw" / profile.id / "authorized.wav"; training_audio.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(training_audio), "wb") as stream:
        stream.setparams((1, 2, 100, 0, "NONE", "")); stream.writeframes(b"\x01\x00" * 60_320)
    source = SourceAsset(profile.id, str(training_audio), str(training_audio), hashlib.sha256(training_audio.read_bytes()).hexdigest(), duration_seconds=603.2, sample_rate=100, channels=1, codec="pcm")
    store.save_source_assets(project, [source]); profile.source_asset_ids = [source.id]; store.save_profile(project, profile)
    window.navigation.setCurrentRow(3); window.training_page._refresh_singing_profiles(); app.processEvents()
    capture("phase3.1-singing-not-ready-1440x900.png", 1440, 900)
    profile.training_state = "training_singing_model"; store.save_profile(project, profile); window.training_page._refresh_singing_profiles(); app.processEvents()
    capture("phase3.1-singing-training-1440x900.png", 1440, 900)
    model_root = project / "models" / "singing" / profile.id / "capture"; model_root.mkdir(parents=True, exist_ok=True)
    checkpoint = model_root / "model.pth"; index = model_root / "model.index"; checkpoint.write_bytes(b"verified checkpoint"); index.write_bytes(b"verified index")
    model = SingingModelVersion(profile_id=profile.id, engine="rvc_v2", engine_version="8f2fdbf", checkpoint_relative_path=checkpoint.relative_to(project).as_posix(), checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(), index_relative_path=index.relative_to(project).as_posix(), index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(), training_dataset_sha256=hashlib.sha256(b"capture dataset").hexdigest(), training_dataset_id="capture", training_source_asset_ids=[source.id], training_lineage=[{"id": source.id, "duration_seconds": 603.2}], trust_status="verified")
    profile.training_state = "trained_singing_model"; profile.singing_models = [model]; profile.active_singing_model_id = model.id; store.save_profile(project, profile); window.training_page._refresh_singing_profiles(); app.processEvents()
    capture("phase3.1-singing-ready-1440x900.png", 1440, 900)
    window.navigation.setCurrentRow(0); window.cover_page.cover_project = CoverProject.load(project, cover.id); window.cover_page.refresh_profiles(); window.cover_page._load_track(1, Path(result["vocal_path"])); window.cover_page._load_track(2, Path(result["instrumental_path"])); wait_load()
    window.cover_page.worker.send = lambda command, payload=None, request_id=None: request_id or ("ai-generating" if command == "convert_vocal" else "request")
    window.cover_page.generate_ai_vocal(); app.processEvents(); capture("phase3.1-ai-vocal-generating-1440x900.png", 1440, 900)
    ai_path = cover.root / "generated" / "ai-vocal" / "capture-ai.wav"; ai_path.parent.mkdir(parents=True, exist_ok=True); ai_path.write_bytes(Path(result["vocal_path"]).read_bytes())
    cover = CoverProject.load(project, cover.id); ai_asset = CoverAsset(id="capture-ai", role="ai_vocal", relative_path=ai_path.relative_to(cover.root).as_posix(), sha256=hashlib.sha256(ai_path.read_bytes()).hexdigest(), content_origin="ai_generated", producer="rvc_v2", model_id=model.id, model_sha256=model.checkpoint_sha256, source_asset_ids=["vocal"]); cover.add_asset(ai_asset)
    window.cover_page.handle_worker_event("ai-generating", "result", {"asset_id": ai_asset.id, "cache_hit": False}); wait_load()
    capture("phase3.1-ai-vocal-ready-1440x900.png", 1440, 900); capture("phase3.1-ai-vocal-ready-1280x720.png", 1280, 720)
    window.navigation.setCurrentRow(3); window.training_page._refresh_singing_profiles(); app.processEvents(); capture_model_management()
    window.close(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
