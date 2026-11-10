"""Native SDK fixtures; run in the pinned SDK container for acquisition coverage."""
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("seed_models", ROOT / "scripts/seed-models.py")
SEED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEED)
try:
    import loom
except ImportError:
    loom = None


@unittest.skipUnless(loom, "requires the pinned installed SDK; execute native-container gate")
class NativeSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.cache = self.root / "cache"
        for name in ("huggingface", "loom-assets"):
            (self.cache / name).mkdir(parents=True)
        self.baseline = json.loads((ROOT / "config/single-model-e2e.json").read_text())
        card = {"schema_version": 1, "model": {"id": self.baseline["model_id"], "name": "Tiny fixture"},
            "programs": {"source": {"inputs": ["control.loom"], "libraries": []}},
            "services": {"chat": {"kind": "text", "version": 1}},
            "variants": {"gguf": {"services": {"chat": {"programs": ["source"],
                "parameters": {"model": {"files": [{"path": "model.gguf", "format": "gguf"}]}},
                "tokenizers": {"text": "tokenizer.json"}}}}},
            "presets": {"chat": {"service": "chat", "variant": "gguf", "programs": "source"}}}
        (self.remote / "model.json").write_text(json.dumps(card))
        self.assets = {"model.gguf": b"small model fixture", "tokenizer.json": b'{"fixture":true}'}
        for name, content in {**self.assets, "control.loom": b"first program"}.items():
            (self.remote / name).write_bytes(content)
        from loom.model.lock import FileIdentity, ModelLock
        identities = [FileIdentity(name, len(content), hashlib.sha256(content).hexdigest())
                      for name, content in self.assets.items()]
        self.lock = ModelLock.snapshot({}, identities, scope="assets")
        (self.remote / "model.json.lock").write_text(json.dumps(self.lock))
        self.requests = []
        requests = self.requests
        self.interrupt = {"enabled": False}
        interrupt = self.interrupt
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if interrupt["enabled"] and self.path == "/model.gguf":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b"small model fixture")))
                    self.end_headers()
                    self.wfile.write(b"small")
                    self.close_connection = True
                    return
                super().do_GET()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(self.remote)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/model.json"

    def attempt(self, name, offline=False):
        work, output = self.root / name, self.root / (name + "-results")
        work.mkdir()
        (output / "sdk-seed").mkdir(parents=True)
        SEED.write_json(output / "sdk-seed/sdk.json", {"identity": {"fixture": True}})
        SEED.prepare(self.baseline, work, self.cache, location=self.url)
        SEED.fill(self.baseline, work, self.cache, output, offline)
        return work, output, SEED.read_json(output / "seed-manifest.json")

    def test_repeat_seed_refreshes_program_and_reuses_verified_assets_offline(self):
        first, _, manifest = self.attempt("one")
        (self.remote / "control.loom").write_bytes(b"second program")
        before = len(self.requests)
        second, output, repeated = self.attempt("two", offline=True)
        self.assertEqual((second / "installed/control.loom").read_bytes(), b"second program")
        self.assertIn("/control.loom", self.requests[before:])
        self.assertNotIn("/model.gguf", self.requests[before:])
        self.assertNotIn("/tokenizer.json", self.requests[before:])
        self.assertEqual(manifest["files"], repeated["files"])
        self.assertEqual(len(repeated["files"]), 2)
        self.assertEqual(SEED.read_json(output / "seed-report.json")["asset_network"], "offline")
        self.assertNotEqual(first, second)
        for record in repeated["files"]:
            cached = self.cache / record["cache_root"] / record["cache_path"]
            self.assertEqual(SEED.digest(cached), record["sha256"])

    def test_bad_digest_rejects_without_publishing_manifest(self):
        self.lock["files"][0]["sha256"] = "0" * 64
        (self.remote / "model.json.lock").write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(Exception, "SHA-256|integrity"):
            self.attempt("bad")
        self.assertFalse((self.root / "bad-results/seed-manifest.json").exists())

    def test_missing_lock_coverage_rejects_before_assets(self):
        self.lock["files"].pop()
        (self.remote / "model.json.lock").write_text(json.dumps(self.lock))
        with self.assertRaises(Exception):
            self.attempt("missing")
        self.assertNotIn("/model.gguf", self.requests)

    def test_corrupt_cached_asset_rejects_offline(self):
        _, _, manifest = self.attempt("one")
        item = manifest["files"][0]
        (self.cache / item["cache_root"] / item["cache_path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(Exception, "integrity"):
            self.attempt("corrupt", offline=True)
        self.assertFalse((self.root / "corrupt-results/seed-manifest.json").exists())

    def test_truncated_asset_does_not_publish_partial_cache_blob(self):
        self.interrupt["enabled"] = True
        with self.assertRaises(Exception):
            self.attempt("short")
        self.assertFalse((self.root / "short-results/seed-manifest.json").exists())
        temporary = self.cache / "loom-assets/temporary"
        self.assertFalse(temporary.exists() and any(temporary.iterdir()))

    def test_external_asset_path_is_rejected_before_index_publication(self):
        work, output = self.root / "escape", self.root / "escape-results"
        work.mkdir()
        (output / "sdk-seed").mkdir(parents=True)
        SEED.write_json(output / "sdk-seed/sdk.json", {"identity": {"fixture": True}})
        SEED.prepare(self.baseline, work, self.cache, location=self.url)
        prepared = SEED.read_json(work / "prepared.json")
        prepared["sources"]["model.gguf"]["transport"] = {
            "kind": "local", "path": str(self.remote / "model.gguf")}
        SEED.write_json(work / "prepared.json", prepared)
        with self.assertRaisesRegex(ValueError, "escapes designated roots"):
            SEED.fill(self.baseline, work, self.cache, output, False)
        self.assertFalse((output / "seed-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
