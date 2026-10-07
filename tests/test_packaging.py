"""Check built release contents without digest or bytewise verification."""

import os
import tarfile
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

import toml

from src import __version__


ROOT = Path(__file__).resolve().parents[1]


class PublicPackageTests(unittest.TestCase):
    def test_release_versions_and_public_defaults_agree(self):
        metadata = toml.load(ROOT / "pyproject.toml")
        defaults = toml.load(ROOT / "config" / "default.toml")
        self.assertEqual(metadata["project"]["version"], __version__)
        self.assertEqual(defaults["version"], __version__)
        self.assertEqual(defaults["llm_config"]["api_key"], "")
        self.assertEqual(defaults["llm_config"]["model"], "")
        self.assertEqual(defaults["llm_config"]["base_url"], "")
        self.assertEqual(defaults["llm_config"]["concurrency_limit"], 10)


@unittest.skipUnless(os.environ.get("LATEXTRANS_DIST_DIR"), "Set LATEXTRANS_DIST_DIR after building release archives")
class BuiltPackageTests(unittest.TestCase):
    def setUp(self):
        self.dist = Path(os.environ["LATEXTRANS_DIST_DIR"]).resolve()
        self.wheels = sorted(self.dist.glob("*.whl"))
        self.sdists = sorted(self.dist.glob("*.tar.gz"))
        self.assertEqual(len(self.wheels), 1)
        self.assertEqual(len(self.sdists), 1)

    def assert_public_files_only(self, names):
        for name in names:
            path = PurePosixPath(name)
            self.assertNotIn(path.name, {"local.toml", ".env"}, name)
            self.assertFalse(path.name.startswith(".env."), name)
            self.assertFalse({"outputs", "tex source", "tmp", ".venv"}.intersection(path.parts), name)
            self.assertNotEqual(path.suffix.lower(), ".pdf", name)

    def test_wheel_contains_defaults_glossaries_entry_points_and_license(self):
        with zipfile.ZipFile(self.wheels[0]) as archive:
            names = archive.namelist()
            self.assert_public_files_only(names)
            for required in ("main.py", "config/default.toml", "terms/default.csv", "src/gui/streamlit_app.py"):
                self.assertIn(required, names)
            metadata = Parser().parsestr(archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode())
            self.assertEqual(metadata["Version"], __version__)
            self.assertTrue(any(n.endswith("/licenses/LICENSE") for n in names))
            scripts = archive.read(next(n for n in names if n.endswith("/entry_points.txt"))).decode()
            self.assertIn("latextrans = main:main", scripts)
            self.assertIn("latextrans-gui = src.gui.launcher:main", scripts)
            defaults = toml.loads(archive.read("config/default.toml").decode())
            self.assertEqual(defaults["llm_config"]["api_key"], "")

    def test_sdist_contains_build_inputs_and_public_resources(self):
        with tarfile.open(self.sdists[0], "r:gz") as archive:
            names = archive.getnames()
            self.assert_public_files_only(names)
            relative = {str(PurePosixPath(n).relative_to(PurePosixPath(n).parts[0])) for n in names}
            for required in ("pyproject.toml", "requirements.txt", "config/default.toml", "terms/default.csv", "LICENSE", "tests/test_packaging.py"):
                self.assertIn(required, relative)
