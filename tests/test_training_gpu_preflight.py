from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from local_voice_studio.engine import GptSovitsEngine
from local_voice_studio.paths import AppPaths


def test_training_health_skips_inference_stack_import(monkeypatch, tmp_path: Path):
    data = tmp_path / "data"
    paths = AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine",
                     data / "models", data / "logs", data / "db.sqlite3")
    engine = GptSovitsEngine(paths)
    engine.ffmpeg = lambda: Path("ffmpeg.exe")
    engine.readiness = lambda: {"ready": True}

    class Tensor:
        def __mul__(self, _value):
            return self

        def cpu(self):
            return self

        def item(self):
            return 2.0

    cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_properties=lambda _index: SimpleNamespace(
            name="Test GPU", major=12, minor=0, total_memory=16 * 1024**3,
        ),
        get_arch_list=lambda: ["sm_120"],
        synchronize=lambda: None,
    )
    torch = types.ModuleType("torch")
    torch.__version__ = "2.7.1+cu128"
    torch.version = SimpleNamespace(cuda="12.8")
    torch.cuda = cuda
    torch.tensor = lambda *_args, **_kwargs: Tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)

    imported = []
    original_import = builtins.__import__

    def record_import(name, *args, **kwargs):
        if name.startswith("GPT_SoVITS.TTS_infer_pack.TTS"):
            imported.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", record_import)

    health = engine.training_health()

    assert health["compatible"] is True
    assert health["tensor_test_passed"] is True
    assert health["models_ready"] is True
    assert imported == []
