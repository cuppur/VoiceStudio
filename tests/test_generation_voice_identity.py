"""Generation must use the selected voice identity, independent of display order."""
from unittest.mock import patch

from local_voice_studio.ui import simple_pages
from local_voice_studio.storage import StudioStore
from test_phase63_pages import _app, _paths, _profile, FakeClient


def make_page(tmp_path):
    _app()
    store = StudioStore(_paths(tmp_path))
    project = store.create_project("voice-identity")
    for name in ("Voice A", "Voice B"):
        profile = _profile(store, project, name)
        model_dir = project / "checkpoints" / profile.id
        model_dir.mkdir(parents=True)
        for filename in ("gpt.ckpt", "sovits.pth"):
            (model_dir / filename).write_bytes(b"unit-test-model")
        profile.active_gpt_checkpoint = str(model_dir / "gpt.ckpt")
        profile.active_sovits_checkpoint = str(model_dir / "sovits.pth")
        store.save_profile(project, profile)
    client = FakeClient()
    page = simple_pages.OneClickGeneratePage(store, project, client)
    page.text.setPlainText("Voice selection contract")
    return page, client


def test_display_order_does_not_change_requested_voice(tmp_path):
    page, client = make_page(tmp_path)
    try:
        target = page.profiles[-1]
        page.profile.clear()
        page.profile.addItem(target.name, target.id)
        page._generate()
        assert client.sent[-1][0] == "load_profile"
        assert client.sent[-1][1]["id"] == target.id
    finally:
        page.release_resources()


def test_empty_voice_selection_never_submits_last_profile(tmp_path):
    page, client = make_page(tmp_path)
    try:
        page.profile.clear()
        with patch.object(simple_pages, "show_error") as error:
            page._generate()
        assert not client.sent
        assert "请选择" in error.call_args.args[1]
    finally:
        page.release_resources()
