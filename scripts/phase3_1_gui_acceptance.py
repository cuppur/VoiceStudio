"""Drive a real Phase 3.1 singing training run through MainWindow controls."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--project", required=True); parser.add_argument("--profile-id", required=True); parser.add_argument("--report", required=True); parser.add_argument("--timeout", type=int, default=7200); parser.add_argument("--cancel-after", type=float, default=0)
    args = parser.parse_args(); project = Path(args.project).resolve(); report_path = Path(args.report).resolve()
    paths = AppPaths.default(); store = StudioStore(paths); assets = [item for item in store.list_source_assets(project, args.profile_id) if item.enabled and not item.duplicate_of]
    report = {"project": str(project), "profile_id": args.profile_id, "source_asset_count": len(assets), "total_duration_seconds": sum(item.duration_seconds for item in assets), "epoch": 20, "events": [], "started_at": time.time(), "vram_peak_mib": 0}
    app = QApplication.instance() or QApplication(sys.argv); window = MainWindow(paths, store); window._switch_project(project); window.navigation.setCurrentRow(3); window.resize(1440, 900); window.show()
    stop_monitor = threading.Event()
    def monitor_vram():
        while not stop_monitor.wait(1):
            try:
                output = subprocess.check_output(["nvidia-smi", "--query-compute-apps=used_memory", "--format=csv,noheader,nounits"], text=True, timeout=5)
                values = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
                report["vram_peak_mib"] = max(report["vram_peak_mib"], max(values, default=0))
            except Exception: pass
    threading.Thread(target=monitor_vram, daemon=True).start()
    timed_out = {"value": False}
    def finish(code: int):
        if stop_monitor.is_set(): return
        stop_monitor.set(); report["completed_at"] = time.time(); report["total_time_seconds"] = report["completed_at"] - report["started_at"]
        report_path.parent.mkdir(parents=True, exist_ok=True); report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"); window.close(); app.exit(code)
    def event(request_id, event_type, payload):
        if request_id != report.get("request_id"): return
        report["events"].append({"type": event_type, "payload": payload, "at": time.time()})
        if event_type == "progress" and not report.get("training_screenshot"):
            path = Path(__file__).resolve().parents[1] / "docs" / "screenshots" / "phase3.1-singing-training-1440x900.png"
            window.grab().toImage().scaled(1440, 900).save(str(path)); report["training_screenshot"] = str(path)
        if event_type == "result":
            report["result"] = payload; QTimer.singleShot(500, lambda: finish(0))
        elif event_type == "error":
            report["error"] = payload; expected_cancel = bool(args.cancel_after and payload.get("status") == "cancelled"); QTimer.singleShot(100, lambda: finish(0 if expected_cancel else 2))
    window.client.event.connect(event)
    def start():
        page = window.training_page; index = page.singing_profile.findData(args.profile_id)
        report["window_project"] = str(window.project); report["visible_profile_ids"] = [page.singing_profile.itemData(i) for i in range(page.singing_profile.count())]
        if index < 0: report["error"] = {"message": "profile not visible in formal UI"}; finish(3); return
        page.singing_profile.setCurrentIndex(index); page.singing_train_button.click(); report["request_id"] = page.singing_request
        if args.cancel_after:
            def cancel_training():
                report["cancel_requested_at"] = time.time(); page.singing_cancel_button.click()
            QTimer.singleShot(round(args.cancel_after * 1000), cancel_training)
    QTimer.singleShot(500, start)
    QTimer.singleShot(args.timeout * 1000, lambda: (report.update(timeout=True), finish(4)))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
