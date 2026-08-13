from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from local_voice_studio.downloads import (
    AssetManifestError,
    DownloadSpec,
    download_resumable,
    load_asset_manifest,
    validate_zip_members,
)
from local_voice_studio.paths import AppPaths
from local_voice_studio.runtime import EngineRuntimeResolver


ROOT = Path(__file__).resolve().parents[1]
ASSET_MANIFEST = ROOT / "manifests" / "runtime-assets-v1.json"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_runtime.ps1"


class _Response(io.BytesIO):
    status = 200
    headers: dict[str, str]

    def __init__(self, body: bytes, url: str = "https://mirror.invalid/file") -> None:
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}
        self._url = url

    def geturl(self) -> str:
        return self._url


class AssetManifestTests(unittest.TestCase):
    def test_checked_in_manifest_is_complete_and_pinned(self):
        manifest = load_asset_manifest(ASSET_MANIFEST)
        self.assertEqual(manifest["engine"]["commit"], "d523079fc05d9a8028d6085bffe4a2757c32abb6")
        self.assertEqual(manifest["engine"]["pretrained_revision"], "0c47645e02a7bc3688d7b263b0042c81e3cd82cd")
        self.assertTrue(all("resolve/main" not in url and "resolve/master" not in url for asset in manifest["assets"] for url in asset["urls"]))

    def test_unpinned_or_unsafe_spec_is_rejected(self):
        with self.assertRaises(AssetManifestError):
            DownloadSpec("https://example.invalid/a", Path("a"), "", 1)
        with self.assertRaises(AssetManifestError):
            DownloadSpec("http://example.invalid/a", Path("a"), "0" * 64, 1)

    def test_cache_is_reverified_and_replaced_atomically(self):
        payload = b"trusted bytes"
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "asset.bin"
            destination.write_bytes(b"tampered")
            spec = DownloadSpec("https://one.invalid/a", destination, hashlib.sha256(payload).hexdigest(), len(payload))
            download_resumable(spec, opener=lambda *_args, **_kwargs: _Response(payload))
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(destination.with_suffix(".bin.part").exists())

    def test_mirrors_must_produce_the_same_digest(self):
        good = b"same trusted payload"
        calls: list[str] = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            return _Response(b"wrong" if len(calls) == 1 else good)

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "asset.bin"
            spec = DownloadSpec(
                "https://one.invalid/a", destination, hashlib.sha256(good).hexdigest(), len(good), ("https://two.invalid/a",)
            )
            download_resumable(spec, opener=opener)
            self.assertEqual(calls, ["https://one.invalid/a", "https://two.invalid/a"])
            self.assertEqual(destination.read_bytes(), good)

    def test_non_https_redirect_is_rejected(self):
        payload = b"trusted"
        with tempfile.TemporaryDirectory() as tmp:
            spec = DownloadSpec("https://one.invalid/a", Path(tmp) / "a", hashlib.sha256(payload).hexdigest(), len(payload))
            with self.assertRaisesRegex(ValueError, "non-HTTPS"):
                download_resumable(spec, opener=lambda *_args, **_kwargs: _Response(payload, "http://evil.invalid/a"))


class ZipSafetyTests(unittest.TestCase):
    def test_traversal_and_drive_paths_are_rejected(self):
        for member in ("../escape.txt", "safe/../../escape.txt", "C:/escape.txt"):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "bad.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr(member, "bad")
                with self.assertRaisesRegex(ValueError, "unsafe ZIP path"):
                    validate_zip_members(archive)

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "link.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(info, "target")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                validate_zip_members(archive)


class RuntimeIntegrityTests(unittest.TestCase):
    def _paths(self, root: Path) -> AppPaths:
        return AppPaths(root, root / "projects", root / "runtime", root / "engines" / "GPT-SoVITS", root / "models", root / "logs", root / "db.sqlite")

    def test_tool_resolution_never_uses_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rogue = root / "rogue"
            rogue.mkdir()
            (rogue / "ffmpeg.exe").write_bytes(b"rogue")
            resolver = EngineRuntimeResolver(self._paths(root), frozen=False)
            with patch.dict(os.environ, {"PATH": str(rogue)}):
                self.assertIsNone(resolver.resolve_private_tool("ffmpeg"))
            trusted = root / "tools" / "ffmpeg.exe"
            trusted.parent.mkdir()
            trusted.write_bytes(b"trusted")
            self.assertEqual(resolver.resolve_private_tool("ffmpeg"), trusted.resolve())

    def test_install_manifest_v2_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tools" / "ffmpeg.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"trusted")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "install-manifest.json").write_text(json.dumps({
                "schema_version": 2,
                "asset_manifest_version": "test",
                "asset_manifest_sha256": "a" * 64,
                "engine_commit": "test",
                "pretrained_revision": "test",
                "lockfiles": {"conda": {"sha256": "b" * 64}, "pip": {"sha256": "c" * 64}},
                "verified_files": [{"path": "tools/ffmpeg.exe", "size": 7, "sha256": digest}],
            }), encoding="utf-8")
            resolver = EngineRuntimeResolver(self._paths(root), frozen=False)
            self.assertTrue(resolver.verify_install_manifest().valid)
            target.write_bytes(b"changed")
            self.assertFalse(resolver.verify_install_manifest().valid)


class BootstrapContracts(unittest.TestCase):
    def test_bootstrap_has_no_floating_or_path_fallbacks(self):
        source = BOOTSTRAP.read_text(encoding="utf-8-sig")
        self.assertNotIn("releases/latest", source)
        self.assertNotIn("Get-Command ffmpeg", source)
        self.assertIn("Test-MiniforgeSignature", source)
        self.assertIn("NumFOCUS", ASSET_MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("schema_version = 2", source)

    def test_dependency_locks_are_strict_and_nonempty(self):
        conda = (ROOT / "locks" / "conda-win-64.lock").read_text(encoding="utf-8")
        pip = (ROOT / "locks" / "requirements-win-cu128.lock").read_text(encoding="utf-8")
        self.assertIn("@EXPLICIT", conda)
        self.assertIn("ffmpeg-", conda)
        self.assertGreater(len(conda.splitlines()), 50)
        self.assertIn("--hash=sha256:", pip)
        self.assertIn("torch==2.7.1+cu128", pip)
        self.assertGreater(len(pip.splitlines()), 300)


if __name__ == "__main__":
    unittest.main()
