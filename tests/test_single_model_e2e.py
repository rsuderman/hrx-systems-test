import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_SPEC = importlib.util.spec_from_file_location('test_model_config', ROOT / 'scripts/model-config.py')
CONFIG = importlib.util.module_from_spec(CONFIG_SPEC)
CONFIG_SPEC.loader.exec_module(CONFIG)

SPEC = importlib.util.spec_from_file_location("single_model_e2e", ROOT / "scripts/single-model-e2e.py")
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


class SingleModelSkeletonTests(unittest.TestCase):
    def invoke(self, root, name, *extra):
        output = root / name
        code = E2E.main(["--output", str(output), "--cache", str(root / "cache"), *extra])
        return code, json.loads((output / "report.json").read_text())

    def test_selection_matches_inventory_and_preserves_published_context(self):
        selection = CONFIG.default_baseline()[0]
        with (ROOT / "catalog/model-size-index.csv").open() as stream:
            row = next(row for row in csv.DictReader(stream) if row["model_repository"] == selection["repository"])
        self.assertEqual(row["publication_revision"], selection["publication_revision"])
        self.assertEqual(row["preset"], selection["preset"])
        self.assertEqual(selection["published_execution"]["context_capacity"], 262144)
        self.assertEqual(selection["smoke_request"]["max_tokens"], 8)

    def test_second_run_reuses_only_fixture_asset_and_stages_new_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_code, first = self.invoke(root, "first")
            second_code, second = self.invoke(root, "second")
            self.assertEqual((first_code, second_code), (0, 0))
            self.assertFalse(first["stages"]["acquire"]["fixture_asset_reused"])
            self.assertTrue(second["stages"]["acquire"]["fixture_asset_reused"])
            self.assertNotEqual(first["stages"]["acquire"]["fresh_pipeline_path"], second["stages"]["acquire"]["fresh_pipeline_path"])
            self.assertGreaterEqual(first["stages"]["serve"]["readiness_checks"], 2)
            self.assertFalse(first["model_tested"])
            self.assertTrue(first["stages"]["cleanup"]["owned_fixture_server_stopped"])

    def test_unknown_memory_blocks_before_server_start(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report = self.invoke(Path(directory), "unknown", "--fixture-failure", "memory")
            self.assertEqual(code, 2)
            self.assertEqual(report["stages"]["admit"]["decision"], "unqualified")
            self.assertEqual(report["stages"]["serve"]["status"], "not_run")
            self.assertFalse(report["stages"]["cleanup"]["server_started"])

    def test_response_failure_keeps_report_and_stops_owned_server(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report = self.invoke(Path(directory), "bad", "--fixture-failure", "answer")
            self.assertEqual(code, 1)
            self.assertEqual(report["stages"]["assert"]["status"], "failed")
            self.assertTrue(report["stages"]["cleanup"]["owned_fixture_server_stopped"])
            self.assertFalse(report["model_tested"])

    def test_readiness_requires_prepared_model_and_has_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report = self.invoke(Path(directory), "timeout", "--fixture-failure", "readiness")
            self.assertEqual(code, 1)
            self.assertEqual(report["stages"]["serve"]["status"], "failed")
            self.assertIn("TimeoutError", report["stages"]["serve"]["error"])
            self.assertTrue(report["stages"]["cleanup"]["owned_fixture_server_stopped"])

    def test_real_mode_is_blocked_without_writing_asset_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = self.invoke(root, "real", "--mode", "real")
            self.assertEqual(code, 2)
            self.assertEqual(report["outcome"], "blocked")
            self.assertFalse((root / "cache").exists())
            self.assertFalse(report["model_tested"])
            self.assertEqual(len(report["blockers"]), 3)

    def test_arbitrary_configuration_cannot_enable_unimplemented_real_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qualification = root / "qualification.json"
            qualification.write_text('{"required_bytes": 1}')
            code, report = self.invoke(root, "configured", "--mode", "real", "--image",
                                       "example.invalid/runtime@sha256:" + "a" * 64,
                                       "--qualification", str(qualification))
            self.assertEqual(code, 2)
            self.assertTrue(any("not implemented" in value for value in report["blockers"]))

    def test_free_memory_reserve_and_invalid_estimates(self):
        self.assertEqual(E2E.memory_fit(60, 100, 10), "eligible")
        self.assertEqual(E2E.memory_fit(60, 65, 10), "insufficient_memory")
        self.assertEqual(E2E.memory_fit(None, 100, 10), "unqualified")
        for value in (-1, True):
            with self.assertRaises(ValueError):
                E2E.memory_fit(value, 100, 10)
