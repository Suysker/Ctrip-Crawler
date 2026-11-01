"""Contracts for producing a transparent, standalone Python artifact."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ("imports", "config", "files", "processing", "browser", "worker", "main")


class SingleFileBuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        source = self.root / "source_sections"
        source.mkdir()
        for name in SECTIONS:
            (source / f"{name}.py.inc").write_text("\n", encoding="utf-8")
        self.write("imports", 'from __future__ import annotations\nimport argparse\n')
        self.write("config", 'DEFAULT_CITY = "上海"\n')
        self.write("worker", 'def worker_main():\n    return DEFAULT_CITY\n')
        self.write("main", 'def main():\n    parser = argparse.ArgumentParser()\n    parser.parse_args()\n    print(worker_main())\n\nif __name__ == "__main__":\n    main()\n')

    def tearDown(self):
        self.directory.cleanup()

    def write(self, section, value):
        (self.root / "source_sections" / f"{section}.py.inc").write_text(value, encoding="utf-8")

    def build(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/build_single_file.py"), "--root", str(self.root), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def test_result_runs_when_only_artifact_is_copied_and_build_is_deterministic(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = self.root / "ctrip_flights_scraper.py"
        original = artifact.read_bytes()
        self.assertEqual(self.build().returncode, 0)
        self.assertEqual(artifact.read_bytes(), original)
        self.assertNotIn(b"\r\n", original)
        with tempfile.TemporaryDirectory() as isolated:
            copied = Path(isolated) / artifact.name
            copied.write_bytes(original)
            run = subprocess.run([sys.executable, "-I", "-X", "utf8", str(copied)], cwd=isolated, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "上海")

    def test_check_detects_stale_artifact_without_replacing_user_file(self):
        self.assertEqual(self.build().returncode, 0)
        artifact = self.root / "ctrip_flights_scraper.py"
        artifact.write_text("# user modified\n", encoding="utf-8")
        result = self.build("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "# user modified\n")

    def test_duplicate_top_level_symbols_and_local_imports_fail(self):
        for content in ('def worker_main():\n    pass\n', 'from .browser import Browser\n', 'import source_sections\n'):
            with self.subTest(content=content):
                self.write("processing", content)
                self.assertNotEqual(self.build().returncode, 0)

    def test_second_main_guard_is_rejected(self):
        self.write("browser", 'if __name__ == "__main__":\n    pass\n')
        self.assertNotEqual(self.build().returncode, 0)

    def test_missing_section_is_not_silently_skipped(self):
        (self.root / "source_sections/files.py.inc").unlink()
        self.assertNotEqual(self.build().returncode, 0)


if __name__ == "__main__":
    unittest.main()
