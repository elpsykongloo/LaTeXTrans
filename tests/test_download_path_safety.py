import gzip
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from src.formats.latex import utils


class DownloadPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / ".paper.extracting"
        self.destination = self.root / "paper"
        self.source.mkdir()
        self.destination.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.sentinel = self.outside / "untouched.tex"
        self.sentinel.write_text("Original", encoding="utf-8")
        (self.source / "a.tex").write_text("Translated", encoding="utf-8")
        (self.destination / "a.tex").write_text("Existing", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest("Symbolic links unavailable for this account")

    def directory_link(self, link, target):
        if os.name != "nt":
            return self.symlink(link, target, directory=True)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("Directory junctions unavailable for this account")

    def assert_untouched(self):
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "Original")
        self.assertEqual((self.destination / "a.tex").read_text(encoding="utf-8"), "Existing")
        self.assertTrue(self.source.is_dir())
        self.assertFalse((self.destination / utils.DOWNLOAD_COMPLETE_MARKER).exists())

    def test_safe_merge_updates_regular_files_and_marks_complete(self):
        nested = self.source / "sections"
        nested.mkdir()
        (nested / "results.tex").write_text("Results", encoding="utf-8")
        utils._merge_extracted_dir(self.source, self.destination)
        self.assertEqual((self.destination / "a.tex").read_text(), "Translated")
        self.assertEqual((self.destination / "sections/results.tex").read_text(), "Results")
        self.assertEqual((self.destination / utils.DOWNLOAD_COMPLETE_MARKER).read_text(), "ok\n")
        self.assertFalse(self.source.exists())

    def test_safe_new_destination_moves_complete_tree(self):
        target = self.root / "new-paper"
        utils._merge_extracted_dir(self.source, target)
        self.assertEqual((target / "a.tex").read_text(), "Translated")
        self.assertTrue((target / utils.DOWNLOAD_COMPLETE_MARKER).is_file())
        self.assertFalse(self.source.exists())

    def test_late_destination_hardlink_rejects_entire_merge_before_overwrite(self):
        (self.source / "z.tex").write_text("Unsafe replacement", encoding="utf-8")
        os.link(self.sentinel, self.destination / "z.tex")
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_late_destination_symlink_rejects_entire_merge_before_overwrite(self):
        (self.source / "z.tex").write_text("Unsafe replacement", encoding="utf-8")
        self.symlink(self.destination / "z.tex", self.sentinel)
        with self.assertRaises(ValueError):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_late_destination_directory_junction_rejects_before_overwrite(self):
        nested = self.source / "z-linked"
        nested.mkdir()
        (nested / "untouched.tex").write_text("Unsafe replacement", encoding="utf-8")
        self.directory_link(self.destination / "z-linked", self.outside)
        with self.assertRaises(ValueError):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_late_temporary_source_hardlink_rejects_before_any_copy(self):
        os.link(self.sentinel, self.source / "z.tex")
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_late_temporary_source_symlink_rejects_before_any_copy(self):
        self.symlink(self.source / "z.tex", self.sentinel)
        with self.assertRaises(ValueError):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_temporary_source_directory_junction_is_not_followed(self):
        self.directory_link(self.source / "z-linked", self.outside)
        with self.assertRaises(ValueError):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_destination_root_junction_is_not_followed(self):
        target = self.root / "linked-destination"
        self.directory_link(target, self.outside)
        with self.assertRaises(ValueError):
            utils._merge_extracted_dir(self.source, target)
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertFalse((self.outside / "a.tex").exists())
        self.assertTrue(self.source.exists())

    def test_marker_hardlink_rejects_merge_before_any_copy(self):
        os.link(self.sentinel, self.destination / utils.DOWNLOAD_COMPLETE_MARKER)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assertEqual((self.destination / "a.tex").read_text(), "Existing")
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertTrue(self.source.exists())

    def test_direct_marker_writer_rejects_hardlink(self):
        os.link(self.sentinel, self.destination / utils.DOWNLOAD_COMPLETE_MARKER)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            utils._mark_download_complete(self.destination)
        self.assertEqual(self.sentinel.read_text(), "Original")

    def test_direct_marker_writer_rejects_project_junction(self):
        target = self.root / "linked-destination"
        self.directory_link(target, self.outside)
        with self.assertRaises(ValueError):
            utils._mark_download_complete(target)
        self.assertFalse((self.outside / utils.DOWNLOAD_COMPLETE_MARKER).exists())

    def test_marker_directory_conflict_rejects_before_any_copy(self):
        (self.source / utils.DOWNLOAD_COMPLETE_MARKER).mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()

    def test_file_directory_conflict_rejects_before_any_copy(self):
        (self.source / "z.tex").write_text("New", encoding="utf-8")
        (self.destination / "z.tex").mkdir()
        with self.assertRaisesRegex(ValueError, "different file type"):
            utils._merge_extracted_dir(self.source, self.destination)
        self.assert_untouched()
        self.assertFalse((self.destination / "z.tex/z.tex").exists())

    def test_nested_source_destination_rejects_before_copy(self):
        with self.assertRaisesRegex(ValueError, "separate directories"):
            utils._merge_extracted_dir(self.source, self.source / "nested")
        self.assertFalse((self.source / "nested").exists())

    def zip_archive(self):
        archive = self.root / "paper.zip"
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr("a.tex", "Updated from archive")
            target.writestr("z.tex", "Unsafe replacement")
        return archive

    def gzip_archive(self):
        archive = self.root / "paper.tar.gz"
        with gzip.open(archive, "wb") as target:
            target.write(b"\\documentclass{article}\\begin{document}Downloaded\\end{document}")
        return archive

    def test_archive_merge_failure_preserves_archive_and_existing_files(self):
        archive = self.zip_archive()
        os.link(self.sentinel, self.destination / "z.tex")
        self.assertIsNone(utils.extract_archive_file(str(archive)))
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertEqual((self.destination / "a.tex").read_text(), "Existing")
        self.assertTrue(archive.is_file())
        self.assertFalse((self.destination / utils.DOWNLOAD_COMPLETE_MARKER).exists())

    def test_existing_extracting_junction_is_not_deleted_or_followed(self):
        archive = self.zip_archive()
        target = self.root / ".linked.extracting"
        self.directory_link(target, self.outside)
        linked_archive = self.root / "linked.zip"
        archive.rename(linked_archive)
        self.assertIsNone(utils.extract_archive_file(str(linked_archive)))
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertTrue(target.exists())
        self.assertTrue(linked_archive.is_file())
        self.assertFalse((self.root / "linked").exists())

    def test_unsafe_existing_extracting_tree_is_preserved_during_cleanup(self):
        archive = self.zip_archive()
        os.link(self.sentinel, self.source / "z.tex")
        self.assertIsNone(utils.extract_archive_file(str(archive)))
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertEqual((self.source / "a.tex").read_text(), "Translated")
        self.assertEqual((self.destination / "a.tex").read_text(), "Existing")
        self.assertTrue(archive.exists())

    def test_gzip_hardlinked_target_is_rejected_without_truncation(self):
        archive = self.gzip_archive()
        os.link(self.sentinel, self.destination / "paper.tex")
        self.assertIsNone(utils.extract_archive_file(str(archive)))
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertTrue(archive.exists())

    def test_gzip_symlink_target_is_rejected_without_truncation(self):
        archive = self.gzip_archive()
        self.symlink(self.destination / "paper.tex", self.sentinel)
        self.assertIsNone(utils.extract_archive_file(str(archive)))
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertTrue(archive.exists())

    def test_gzip_destination_junction_is_rejected_without_external_write(self):
        self.directory_link(self.root / "linked", self.outside)
        archive = self.root / "linked.tar.gz"
        with gzip.open(archive, "wb") as target:
            target.write(b"Downloaded")
        self.assertIsNone(utils.extract_archive_file(str(archive)))
        self.assertEqual(self.sentinel.read_text(), "Original")
        self.assertFalse((self.outside / "linked.tex").exists())
        self.assertTrue(archive.exists())

    def test_safe_single_file_gzip_is_extracted(self):
        archive = self.gzip_archive()
        result = utils.extract_archive_file(str(archive))
        self.assertEqual(result, self.destination)
        self.assertIn("Downloaded", (self.destination / "paper.tex").read_text())
        self.assertFalse(archive.exists())

    def test_safe_zip_merge_remains_supported(self):
        archive = self.zip_archive()
        result = utils.extract_archive_file(str(archive))
        self.assertEqual(result, self.destination)
        self.assertEqual((self.destination / "a.tex").read_text(), "Updated from archive")
        self.assertTrue((self.destination / utils.DOWNLOAD_COMPLETE_MARKER).is_file())
        self.assertFalse(archive.exists())

    def test_download_does_not_report_existing_project_after_extraction_failure(self):
        self.gzip_archive()
        with mock.patch.object(utils, "is_already_downloaded", return_value=True), \
                mock.patch.object(utils, "extract_archive_file", return_value=None), \
                mock.patch.object(utils, "_extract_nested_archives") as nested:
            self.assertIsNone(utils.download_arxiv_source("paper", str(self.root)))
        nested.assert_not_called()
        self.assertEqual((self.destination / "a.tex").read_text(), "Existing")

    def test_cached_project_without_archive_remains_usable(self):
        with mock.patch.object(utils, "is_already_downloaded", return_value=True), \
                mock.patch.object(utils, "download_tex") as download:
            result = utils.download_arxiv_source("paper", str(self.root))
        self.assertEqual(result, str(self.destination))
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
