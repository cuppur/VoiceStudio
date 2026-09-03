"""Real Phase 4.1 GUI/worker acceptance and release screenshots.

The source must be an existing authorized local audio asset.  No model,
checkpoint, stem or AI vocal is synthesized by this harness: all processing is
performed by the normal worker commands used by CoverPage.
"""
from __future__ import annotations

import argparse, hashlib, json, os, shutil, sys, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path: sys.path.insert(0, str(SRC))

from PySide6.QtCore import QEventLoop, QTimer, QPoint, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.cover.mixing import GainScale
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.cover_page import ExportDialog
from local_voice_studio.ui.main_window import MainWindow


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def wait_request(window, request_id: str, timeout_ms: int) -> tuple[str, dict, float]:
    loop = QEventLoop(); result = {}; started = time.monotonic()
    def event(rid, kind, payload):
        if rid == request_id and kind in {"result", "error"}: result.update(kind=kind, payload=dict(payload)); loop.quit()
    window.client.event.connect(event); QTimer.singleShot(timeout_ms, loop.quit); loop.exec(); window.client.event.disconnect(event)
    return result.get("kind", "timeout"), result.get("payload", {}), time.monotonic() - started


def wait_load(page, app, timeout_ms=30000):
    deadline = time.monotonic() + timeout_ms / 1000
    while page._threads and time.monotonic() < deadline: app.processEvents(); time.sleep(.05)
    if page._threads: raise RuntimeError("waveform load timeout")


def capture(window, output: Path, name: str, width: int, height: int):
    window.resize(width, height); QApplication.processEvents(); image = window.grab().toImage().scaled(width, height); image.save(str(output / name))


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--project", required=True); parser.add_argument("--profile-id", required=True); parser.add_argument("--source", required=True); parser.add_argument("--export-dir", required=True); parser.add_argument("--report", required=True); parser.add_argument("--timeout", type=int, default=1800); args = parser.parse_args()
    project, source, export_dir = Path(args.project).resolve(), Path(args.source).resolve(), Path(args.export_dir).resolve(); export_dir.mkdir(parents=True, exist_ok=True)
    paths = AppPaths.default(); store = StudioStore(paths); profile = next((p for p in store.list_profiles(project) if p.id == args.profile_id), None)
    if not profile or profile.singing_status(project) != "ready": raise SystemExit("authorized verified singing profile is required")
    if not source.is_file(): raise SystemExit("source audio missing")
    app = QApplication.instance() or QApplication(sys.argv); window = MainWindow(paths, store); window._switch_project(project); window.show(); page = window.cover_page
    screenshots = Path(__file__).resolve().parents[1] / "docs" / "screenshots"; screenshots.mkdir(parents=True, exist_ok=True)
    report = {"project": str(project), "profile_id": profile.id, "source": str(source), "source_sha256": digest(source), "started_at": time.time(), "steps": {}}
    empty_project = None
    try:
        # Capture the real empty product state in a clean, temporary project
        # so a previously restored cover cannot race the screenshot.
        empty_project = store.create_project("Phase4 Empty Screenshot")
        window._switch_project(empty_project); app.processEvents()
        capture(window, screenshots, "phase4-empty-1440x900.png", 1440, 900)
        window._switch_project(project); app.processEvents(); page = window.cover_page
        page.set_song(str(source)); wait_load(page, app); cover = page.cover_project; cover.attest_rights(True); page.cover_project = CoverProject.load(project, cover.id); page.rights_state.setText("歌曲权利：已确认")
        index = page.profile_combo.findData(profile.id)
        if index < 0: raise RuntimeError("profile not selectable")
        page.profile_combo.setCurrentIndex(index)
        payload = {"project_path": str(project), "cover_id": cover.id, "source_relative_path": cover.source_relative_path, "source_sha256": cover.source_sha256, "mode": "uvr5"}
        page._separation_request = window.client.send("separate_song", payload)
        QTimer.singleShot(300, page.cancel_separation)
        kind, value, elapsed = wait_request(window, page._separation_request, args.timeout * 1000)
        report["steps"]["separation_cancel"] = {"event": kind, "elapsed_seconds": elapsed, **value}
        if kind != "error" or "取消" not in str(value.get("message", "")):
            raise RuntimeError("UVR5 cancel did not stop the active worker task: " + str(value))
        # A cancelled cover can be retried and must publish a complete pair.
        page._separation_request = window.client.send("separate_song", payload); kind, value, elapsed = wait_request(window, page._separation_request, args.timeout * 1000); report["steps"]["separation"] = {"event": kind, "elapsed_seconds": elapsed, **value}
        if kind != "result": raise RuntimeError("separation failed: " + str(value))
        wait_load(page, app); capture(window, screenshots, "phase4-separated-1440x900.png", 1440, 900)
        page.generate_ai_vocal(); kind, value, elapsed = wait_request(window, page._ai_request, args.timeout * 1000); report["steps"]["ai_vocal"] = {"event": kind, "elapsed_seconds": elapsed, **value}
        if kind != "result": raise RuntimeError("AI vocal failed: " + str(value))
        wait_load(page, app); capture(window, screenshots, "phase4-ai-vocal-ready-1440x900.png", 1440, 900); capture(window, screenshots, "phase4-mixer-1440x900.png", 1440, 900); capture(window, screenshots, "phase4-mixer-1280x720.png", 1280, 720)
        page.request_final_render(); capture(window, screenshots, "phase4-rendering-1440x900.png", 1440, 900); kind, value, elapsed = wait_request(window, page._render_request, args.timeout * 1000); report["steps"]["render"] = {"event": kind, "elapsed_seconds": elapsed, **value}
        if kind != "result": raise RuntimeError("render failed: " + str(value))
        wait_load(page, app); capture(window, screenshots, "phase4-final-ready-1440x900.png", 1440, 900); capture(window, screenshots, "phase4-final-ready-1280x720.png", 1280, 720)
        final = CoverProject.load(project, cover.id).get_asset(role="final_mix")
        dialog = ExportDialog(page.cover_project.title, window); dialog.destination.setText(str(export_dir)); dialog.rights.setChecked(True); dialog.show(); app.processEvents()
        # Compose the modal over the application window.  Capturing the whole
        # desktop is non-deterministic on a developer workstation and may
        # include unrelated browser content.
        image = window.grab().toImage(); overlay = dialog.grab().toImage(); image.setDevicePixelRatio(1.0); overlay.setDevicePixelRatio(1.0); overlay = overlay.scaled(min(1100, image.width() - 80), min(1700, image.height() - 80), Qt.KeepAspectRatio, Qt.SmoothTransformation); painter = QPainter(image); painter.drawImage(QPoint(max(0, (image.width() - overlay.width()) // 2), max(0, (image.height() - overlay.height()) // 2)), overlay); painter.end(); image.scaled(1440, 900).save(str(screenshots / "phase4-export-dialog-1440x900.png")); dialog.close()
        export_payload = {"project_path": str(project), "cover_id": cover.id, "final_asset_id": final.id, "format": "both", "file_name": page.cover_project.title + " - " + profile.name, "destination": str(export_dir), "existing_policy": "replace", "publication_rights_acknowledged": True}
        page._export_request = window.client.send("export_cover", export_payload); kind, value, elapsed = wait_request(window, page._export_request, args.timeout * 1000); report["steps"]["export"] = {"event": kind, "elapsed_seconds": elapsed, **value}
        if kind != "result": raise RuntimeError("export failed: " + str(value))
        page.request_final_render(); kind, value, elapsed = wait_request(window, page._render_request, args.timeout * 1000); report["steps"]["cache"] = {"event": kind, "elapsed_seconds": elapsed, **value}
        page._select_track(1); page._seek_all(1000); app.processEvents(); plan = page.preview_controller.plan; active_tracks = plan.active_tracks if plan else (); positions = [page.preview_controller.channels[track.role].position() for track in active_tracks if track.role in page.preview_controller.channels]
        requested_db = {
            "ai_vocal": GainScale.slider_to_db(page.mixer.sliders[0].value()),
            "instrumental": GainScale.slider_to_db(page.mixer.sliders[1].value()),
            "vocal": GainScale.slider_to_db(page.mixer.sliders[2].value()),
        }
        report["steps"]["preview_sync"] = {
            "mode": plan.mode.value if plan else "",
            "active_roles": [track.role.value for track in active_tracks],
            "sources": {track.role.value: track.path for track in active_tracks},
            "requested_db": requested_db,
            "preview_linear": {track.role.value: track.gain for track in active_tracks},
            "positions_ms": positions,
            "max_drift_ms": max(positions) - min(positions) if positions else 0,
            "selected_track": page._selected_track,
            "roles": [track.role.value for track in active_tracks],
            "available_roles": [role.value for role in plan.tracks] if plan else [],
            "tracks": [{"role": track.role.value, "source": track.path, "asset_id": track.asset_id, "gain": track.gain, "mute": track.muted, "solo": track.solo, "position_ms": page.preview_controller.channels[track.role].position()} for track in active_tracks if track.role in page.preview_controller.channels],
        }
        page.toggle_playback(); app.processEvents(); time.sleep(.2); page.toggle_playback(); report["steps"]["preview_playback"] = {"played_and_paused": True}
        window.close(); app.processEvents()
        restored = MainWindow(paths, store); restored._switch_project(project); restored.show(); restored_page = restored.cover_page; wait_load(restored_page, app)
        restored_cover = CoverProject.load(project, cover.id); restored_final = restored_cover.get_asset(role="final_mix")
        report["steps"]["restart_restore"] = {"final_asset_restored": bool(restored_final and 4 in restored_page.track_paths), "cover_id": cover.id}
        restored.close(); app.processEvents()
        report["cover_id"] = cover.id; report["final_asset"] = final.to_dict(); report["success"] = kind == "result" and bool(value.get("cache_hit"))
    except Exception as exc: report["success"] = False; report["error"] = str(exc)
    finally:
        if empty_project is not None:
            try:
                with store._connect() as db: db.execute("DELETE FROM projects WHERE path = ?", (str(empty_project),))
                shutil.rmtree(empty_project, ignore_errors=True)
            except OSError:
                pass
        report["completed_at"] = time.time(); Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"); window.close()
    return 0 if report.get("success") else 2


if __name__ == "__main__": raise SystemExit(main())
