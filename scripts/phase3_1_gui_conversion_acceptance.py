"""Phase 3.1 conversion acceptance through the real CoverPage/WorkerClient path.

The script deliberately does not instantiate SingingPipeline or WorkerService.
It prepares small, real WAV fixtures as project-owned CoverProject assets, then
drives the same button and wire payload used by the desktop UI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wav_info(path: Path) -> tuple[float, int, int]:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate(), stream.getframerate(), stream.getnchannels()


def prepare_cover(project: Path, source: Path) -> tuple[CoverProject, dict]:
    """Create a normal CoverProject and register a fixture-derived separated stem."""
    cover = CoverProject.create(project, title=source.stem, cover_id="accept-" + uuid.uuid4().hex[:20])
    owned_source = cover.copy_source(source)
    vocal = cover.root / "stems" / "vocals.wav"
    vocal.write_bytes(owned_source.read_bytes())
    cover.set_stem("vocal", vocal)
    cover.attest_rights(True)
    duration, rate, channels = wav_info(vocal)
    return cover, {
        "cover_id": cover.id,
        "fixture_source": str(source),
        "fixture_source_sha256": sha256(source),
        "source_owned_path": str(owned_source),
        "vocal_path": str(vocal),
        "vocal_sha256": sha256(vocal),
        "duration_seconds": duration,
        "sample_rate": rate,
        "channels": channels,
        "content_origin": "separated",
        "provenance": "fixture copied and registered with CoverProject.set_stem",
    }


def wait_for_conversion(window: MainWindow, page, timeout_ms: int, cancel_after: float | None = None) -> tuple[str, dict]:
    result: dict = {}
    event_loop = QEventLoop()
    request = {"id": ""}

    def on_event(request_id: str, event: str, payload: dict) -> None:
        if request_id != request["id"]:
            return
        result.update(type=event, payload=dict(payload))
        if event in {"result", "error"}:
            event_loop.quit()

    window.client.event.connect(on_event)
    started = time.monotonic()
    page.generate_ai_vocal()
    request["id"] = page._ai_request
    if not request["id"]:
        window.client.event.disconnect(on_event)
        return "error", {"message": "CoverPage 未发出 convert_vocal 请求", "ui": ui_state(page), "elapsed_seconds": 0.0}
    if cancel_after is not None:
        QTimer.singleShot(max(1, round(cancel_after * 1000)), page.cancel_ai_vocal)
    QTimer.singleShot(timeout_ms, event_loop.quit)
    event_loop.exec()
    elapsed = time.monotonic() - started
    window.client.event.disconnect(on_event)
    if not result:
        result = {"type": "timeout", "payload": {"message": "conversion timeout"}}
    result["payload"]["elapsed_seconds"] = elapsed
    result["payload"]["ui"] = ui_state(page)
    return result["type"], result["payload"]


def ui_state(page) -> dict:
    return {
        "request_empty": not bool(page._ai_request),
        "cover_button_enabled": bool(page.cover_button.isEnabled()),
        "cancel_button_visible": bool(page.cancel_ai_button.isVisible()),
        "cover_button_text": page.cover_button.text(),
    }


def select_profile(page, profile_id: str) -> None:
    index = page.profile_combo.findData(profile_id)
    if index < 0:
        raise RuntimeError(f"正式 CoverPage 未显示 profile_id: {profile_id}")
    page.profile_combo.setCurrentIndex(index)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="已有 VoiceStudio project 目录")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--audio", nargs=3, required=True, metavar="WAV", help="三个真实短 WAV fixture")
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--cancel-after", type=float, default=None, help="额外 conversion 点击取消的延迟秒数")
    args = parser.parse_args()
    project = Path(args.project).resolve(); report_path = Path(args.report).resolve()
    paths = AppPaths.default(); store = StudioStore(paths)
    profile: VoiceProfile = next((p for p in store.list_profiles(project) if p.id == args.profile_id), None)
    if profile is None:
        raise SystemExit(f"profile 不存在: {args.profile_id}")
    status = profile.singing_status(project)
    if status != "ready":
        raise SystemExit(f"active singing model 未就绪: {status}")
    fixtures = [Path(item).resolve() for item in args.audio]
    for path in fixtures:
        if not path.is_file(): raise SystemExit(f"WAV 不存在: {path}")
        wav_info(path)  # reject non-WAV input before creating any cover records

    covers, cover_records = [], []
    for fixture in fixtures:
        cover, record = prepare_cover(project, fixture)
        covers.append(cover); cover_records.append(record)

    report = {"project": str(project), "profile_id": args.profile_id, "active_singing_model_id": profile.active_singing_model_id, "model_status": status, "covers": cover_records, "conversions": [], "started_at": time.time()}
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(paths, store); window._switch_project(project); window.show()
    page = window.cover_page
    app.processEvents()

    def run() -> None:
        try:
            for index, cover in enumerate(covers):
                page.cover_project = CoverProject.load(project, cover.id); page.refresh_profiles(); select_profile(page, args.profile_id)
                for pitch in (0, 2):
                    page.pitch.setValue(pitch); before = CoverProject.load(project, cover.id)
                    event, payload = wait_for_conversion(window, page, args.timeout * 1000, None)
                    restored = CoverProject.load(project, cover.id); asset = restored.get_asset(role="ai_vocal")
                    output = Path(payload["output_path"]) if payload.get("output_path") else None
                    row = {"segment": index + 1, "cover_id": cover.id, "pitch": pitch, "event": event, "cache_hit": bool(payload.get("cache_hit")), "elapsed_seconds": payload.get("elapsed_seconds"), "input_duration_seconds": cover_records[index]["duration_seconds"], "input_sha256": cover_records[index]["vocal_sha256"], "output_duration_seconds": None, "output_sha256": payload.get("output_sha256"), "asset_id": payload.get("asset_id"), "ui": payload.get("ui")}
                    if output and output.is_file(): row["output_duration_seconds"] = wav_info(output)[0]; row["output_sha256"] = sha256(output)
                    report["conversions"].append(row)
                    if event != "result": raise RuntimeError(f"conversion failed: {payload}")
            # Re-run one exact request through the button to prove the worker's
            # persisted conversion cache is used (this is intentionally kept
            # separate from the six required segment/pitch conversions).
            page.cover_project = CoverProject.load(project, covers[0].id); page.refresh_profiles(); select_profile(page, args.profile_id); page.pitch.setValue(0)
            event, payload = wait_for_conversion(window, page, args.timeout * 1000, None)
            report["cache_probe"] = {"cover_id": covers[0].id, "pitch": 0, "event": event, "cache_hit": bool(payload.get("cache_hit")), "elapsed_seconds": payload.get("elapsed_seconds"), "output_sha256": payload.get("output_sha256"), "ui": payload.get("ui")}
            if event != "result" or not payload.get("cache_hit"):
                raise RuntimeError(f"cache probe did not hit persisted cache: {payload}")
            if args.cancel_after is not None:
                cover = covers[-1]; page.cover_project = CoverProject.load(project, cover.id); page.refresh_profiles(); select_profile(page, args.profile_id); page.pitch.setValue(4)
                before_ids = {a.id for a in page.cover_project.assets}; generated = cover.root / "generated" / "ai-vocal"; before_files = {p.name for p in generated.glob("*")} if generated.is_dir() else set()
                event, payload = wait_for_conversion(window, page, args.timeout * 1000, args.cancel_after)
                after = CoverProject.load(project, cover.id); after_files = {p.name for p in generated.glob("*")} if generated.is_dir() else set()
                report["cancel"] = {"event": event, "payload": payload, "no_new_asset": not any(a.id not in before_ids for a in after.assets), "no_staging": not any(p.name.endswith(".staging.wav") for p in generated.glob("*") if generated.is_dir()), "new_files": sorted(after_files - before_files), "ui_restored": ui_state(page)}
                if event != "error" or not report["cancel"]["no_new_asset"] or not report["cancel"]["no_staging"] or not report["cancel"]["ui_restored"]["request_empty"]:
                    raise RuntimeError("cancel acceptance failed")
            cancel_ok = args.cancel_after is None or bool(report.get("cancel", {}).get("no_new_asset") and report.get("cancel", {}).get("no_staging"))
            report["completed_at"] = time.time(); report["success"] = len(report["conversions"]) == 6 and bool(report.get("cache_probe", {}).get("cache_hit")) and cancel_ok
        except Exception as exc:
            report["success"] = False; report["error"] = str(exc)
        finally:
            report_path.parent.mkdir(parents=True, exist_ok=True); report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"); window.close(); app.exit(0 if report.get("success") else 2)
    QTimer.singleShot(300, run)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
