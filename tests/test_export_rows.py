from PySide6.QtWidgets import QApplication
from local_voice_studio.ui.export_rows import ExportRowData, ExportTableRow


def test_real_export_metadata_and_open_target(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "clip.wav"
    path.write_bytes(b"metadata fixture, not playable audio")
    data = ExportRowData.from_file(path, "语音")
    assert data.name == path.name and data.format == "WAV"
    row = ExportTableRow(data)
    opened = []; row.open_requested.connect(opened.append)
    row.action.click()
    assert opened == [str(path)]
    row.close()


def test_visual_export_cannot_open_a_fabricated_path():
    app = QApplication.instance() or QApplication([])
    row = ExportTableRow(ExportRowData("视觉样例.wav", "翻唱", "WAV", "", "视觉样例"))
    opened = []; row.open_requested.connect(opened.append)
    row.action.click()
    assert not row.action.isEnabled() and not opened
    row.close()


def test_removed_export_shows_missing_state(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "removed.wav"
    path.write_bytes(b"metadata fixture")
    row = ExportTableRow(ExportRowData.from_file(path, "语音"))
    path.unlink()
    opened = []; row.open_requested.connect(opened.append)
    row.action.click()
    assert not opened and not row.action.isEnabled()
    row.close()
