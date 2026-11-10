import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest


SPEC = importlib.util.spec_from_file_location(
    "sdk_environment", Path(__file__).parents[1] / "scripts/sdk-environment.py")
SDK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SDK)


class SdkEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []
        self.bodies = {"sdk.whl": b"fixture SDK payload", "dependency.whl": b"fixture dependency"}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.path.lstrip("/")
                owner.requests.append(name)
                body = owner.bodies[name]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop)
        self.baseline = self.root / "baseline.json"
        self.lock = self.root / "dependencies.json"
        self.sdk = self.payload("sdk.whl")
        self.dependency = self.payload("dependency.whl")
        self.lock.write_text(json.dumps({
            "schema_version": 1, "wheels": [self.dependency]}))
        self.baseline.write_text(json.dumps({
            "schema_version": 1,
            "container": {"image": "example.invalid/python@sha256:" + "a" * 64},
            "sdk": {"name": "hrx-loom", "version": "fixture", "repository": "fixture/repo",
                    "release": "fixture", "source_commit": "b" * 40, "asset_id": 1,
                    "dependency_lock": self.lock.name, "wheel": self.sdk}}))

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def payload(self, name):
        body = self.bodies[name]
        return {"filename": name, "url": f"http://127.0.0.1:{self.server.server_port}/{name}",
                "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "name": name[:-4], "version": "fixture"}

    def invoke(self, operation, name, payloads="payloads"):
        output = self.root / name
        code = SDK.main([operation, "--baseline", str(self.baseline),
                         "--payload-root", str(self.root / payloads), "--output", str(output),
                         "--venv", str(self.root / "venv")])
        return code, output

    def test_separate_attempts_each_download_verified_payloads(self):
        for attempt in ("first", "second"):
            code, output = self.invoke("acquire", attempt, attempt + "-payloads")
            self.assertEqual(code, 0)
            report = json.loads((output / "sdk-acquisition.json").read_text())
            self.assertEqual(report["payload_policy"], "per_attempt")
            self.assertEqual(report["downloaded_bytes"], sum(map(len, self.bodies.values())))
            self.assertTrue(all("cache_hit" not in p for p in report["payloads"]))
        self.assertEqual(len(self.requests), 4)

    def test_existing_payload_directory_is_not_reused_or_overwritten(self):
        self.assertEqual(self.invoke("acquire", "first")[0], 0)
        target = SDK.payload_path(self.root / "payloads", "hrx", self.sdk)
        before = target.read_bytes()
        code, output = self.invoke("acquire", "repeat")
        self.assertEqual(code, 1)
        self.assertIn("must be new", (output / "sdk-error.json").read_text())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len(self.requests), 2)

    def test_bad_transfer_is_never_published_and_partial_file_is_removed(self):
        self.bodies["sdk.whl"] = b"x" * self.sdk["size_bytes"]
        code, output = self.invoke("acquire", "bad-transfer")
        self.assertEqual(code, 1)
        target = SDK.payload_path(self.root / "payloads", "hrx", self.sdk)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".partial-*")), [])
        self.assertFalse((output / "sdk-acquisition.json").exists())

    def test_short_transfer_is_never_ready(self):
        self.bodies["sdk.whl"] = b"short"
        self.assertEqual(self.invoke("acquire", "short")[0], 1)
        self.assertFalse(SDK.payload_path(self.root / "payloads", "hrx", self.sdk).exists())

    def test_missing_or_corrupt_payload_blocks_install_before_venv_creation(self):
        code, output = self.invoke("install", "missing")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "venv").exists())
        self.assertTrue((output / "sdk-error.json").exists())
        self.assertEqual(self.invoke("acquire", "cold")[0], 0)
        SDK.payload_path(self.root / "payloads", "python", self.dependency).write_bytes(b"bad")
        code, output = self.invoke("install", "corrupt")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "venv").exists())
        self.assertFalse((output / "sdk.json").exists())

    def test_payload_path_escape_and_unpinned_image_are_rejected(self):
        bad = dict(self.sdk, filename="../sdk.whl")
        with self.assertRaises(SDK.SdkError):
            SDK.payload_path(self.root / "payloads", "hrx", bad)
        data = json.loads(self.baseline.read_text())
        data["container"]["image"] = "python:latest"
        self.baseline.write_text(json.dumps(data))
        self.assertEqual(self.invoke("acquire", "unpinned")[0], 1)
        self.assertFalse((self.root / "payloads").exists())

    def test_symlink_payload_is_rejected_before_install(self):
        self.assertEqual(self.invoke("acquire", "first")[0], 0)
        target = SDK.payload_path(self.root / "payloads", "hrx", self.sdk)
        target.unlink()
        other = self.root / "elsewhere.whl"
        other.write_bytes(self.bodies["sdk.whl"])
        target.symlink_to(other)
        code, output = self.invoke("install", "symlink")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "venv").exists())
        self.assertIn("nonregular", (output / "sdk-error.json").read_text())
