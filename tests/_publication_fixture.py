"""Captured native contracts for offline SDK-policy tests."""
import base64
import importlib.util
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.sdk import publication_abi as ABI
FIXTURES = ROOT / 'tests/fixtures/epoch-publication-abi'


class SourceClient:
    def __init__(self, profile='legacy', mutate=None):
        self.profile, self.mutate, self.calls = profile, mutate, []

    def json(self, path):
        self.calls.append(path)
        name = path.split('/')[-1].split('?')[0]
        raw = (FIXTURES / (self.profile + '-' + name)).read_bytes()
        if self.mutate:
            raw = self.mutate(name, raw)
        return {'type': 'file', 'path': 'loom/src/loom/serve/service/text/' + name,
                'encoding': 'base64', 'content': base64.b64encode(raw).decode()}


def publication_abi(snapshot, root=None, profile='legacy', client=None):
    client = client or SourceClient(profile)
    if root is not None:
        return ABI.stage(root, snapshot, client)
    with tempfile.TemporaryDirectory() as tmp:
        return ABI.stage(tmp, snapshot, client)
