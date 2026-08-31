from __future__ import annotations

import http.server
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_runtime.ps1"
COMMIT = "d523079fc05d9a8028d6085bffe4a2757c32abb6"


class DownloadHandler(http.server.BaseHTTPRequestHandler):
    payload = b"download through redirect and chunked transfer"
    valid_zip = b""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/chunked"); self.end_headers(); return
        if self.path == "/chunked":
            self.send_response(200); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            for part in (self.payload[:12], self.payload[12:]):
                self.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n"); return
        if self.path == "/no-range":
            self.send_response(200); self.send_header("Content-Length", str(len(self.payload))); self.end_headers(); self.wfile.write(self.payload); return
        body = self.valid_zip if self.path == "/valid.zip" else (b"not a zip" if self.path == "/broken.zip" else b"<html>error</html>")
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


@unittest.skipUnless(os.name == "nt", "PowerShell bootstrap is Windows-only")
class InstallerDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as raw:
            cls.zip_path = Path(raw.name)
        try:
            with zipfile.ZipFile(cls.zip_path, "w") as archive:
                prefix = f"GPT-SoVITS-{COMMIT}/"
                archive.writestr(prefix + "requirements.txt", "pytest")
                archive.writestr(prefix + "install.ps1", "# test")
                archive.writestr(prefix + "GPT_SoVITS/__init__.py", "")
            DownloadHandler.valid_zip = cls.zip_path.read_bytes()
        finally:
            cls.zip_path.unlink(missing_ok=True)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def invoke(self, body: str, root: Path) -> subprocess.CompletedProcess[str]:
        command = f". '{SCRIPT}' -DataRoot '{root}' -FunctionsOnly; {body}"
        return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], text=True, encoding="utf-8", capture_output=True, timeout=60)

    def test_redirect_and_chunked_response_download_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "测试目录" / "小游戏" / "声音库" / "file.bin"
            result = self.invoke(f"Invoke-RobustDownload '{self.base}/redirect' '{destination}'", Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(destination.read_bytes(), DownloadHandler.payload); self.assertFalse(Path(str(destination) + ".partial").exists())

    def test_stale_partial_falls_back_to_full_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "resume.bin"; Path(str(destination) + ".partial").write_bytes(b"stale")
            result = self.invoke(f"Invoke-RobustDownload '{self.base}/no-range' '{destination}'", Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(destination.read_bytes(), DownloadHandler.payload)

    def test_html_and_corrupt_zip_are_rejected_without_final_file(self):
        for endpoint in ("html", "broken.zip"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                destination = Path(tmp) / "engine.zip"
                result = self.invoke(f"Invoke-RobustDownload '{self.base}/{endpoint}' '{destination}' ${{function:Test-GptSoVitsArchive}}", Path(tmp))
                self.assertNotEqual(result.returncode, 0); self.assertFalse(destination.exists()); self.assertFalse(Path(str(destination) + ".partial").exists())

    def test_valid_pinned_zip_in_chinese_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "测试目录" / "小游戏" / "声音库" / "engine.zip"
            result = self.invoke(f"Invoke-RobustDownload '{self.base}/valid.zip' '{destination}' ${{function:Test-GptSoVitsArchive}}", Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr); self.assertTrue(destination.exists())


class InstallerSourceContractTests(unittest.TestCase):
    def test_no_bits_and_pinned_commit_and_atomic_manifest(self):
        source = SCRIPT.read_text(encoding="utf-8-sig")
        self.assertNotIn("Start-BitsTransfer", source)
        self.assertIn(COMMIT, source)
        self.assertIn(".partial", source)
        self.assertLess(source.index("if ($Validator) { & $Validator $partial }"), source.index("Move-Item -LiteralPath $partial"))
        self.assertIn("Test-PrivatePython $envPython", source)
        self.assertGreater(source.index("install-manifest.json"), source.index("Start-Step 7"))

    def test_gui_explicit_utf8_and_argument_list(self):
        source = (ROOT / "src/local_voice_studio/ui/main_window.py").read_text(encoding="utf-8")
        self.assertIn('decode("utf-8", errors="replace")', source)
        self.assertIn('env.insert("PYTHONIOENCODING", "utf-8")', source)
        self.assertIn('self.process.setArguments(arguments)', source)

    def test_packaged_repair_includes_and_probes_security_resources(self):
        build = (ROOT / "scripts/build.ps1").read_text(encoding="utf-8-sig")
        for relative in (
            "manifests\\runtime-assets-v1.json",
            "locks\\conda-win-64.lock",
            "locks\\requirements-win-cu128.lock",
        ):
            self.assertIn(relative, build)
        self.assertIn("Packaged runtime resource missing", build)
        self.assertIn("-FunctionsOnly", build)


if __name__ == "__main__":
    unittest.main()
