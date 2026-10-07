import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import runtime
from src.agents.tool_agents.generator_agent import GeneratorAgent
from src.agents.coordinator_agent import CoordinatorAgent
from src.utils.checkpoint import CheckpointStore
from src.gui import streamlit_app
from src.formats.latex import utils
from src.formats.latex.parser import LatexParser
from src.formats.latex.reconstruct import LatexConstructor


TEX = r"\documentclass{article}\begin{document}Original\end{document}"


class ReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "src" / "paper"
        self.source.mkdir(parents=True)
        (self.source / "main.tex").write_text(TEX, encoding="utf-8")
        self.outside = self.root / "outside.tex"
        self.outside.write_text("Original", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parser_rejects_external_input_without_reading_it(self):
        parser = LatexParser(str(self.source), str(self.root / "out"))
        with self.assertRaisesRegex(ValueError, "escapes project"):
            parser._merge_inputs(r"\input{../../outside}")
        self.assertEqual(self.outside.read_text(), "Original")

    def test_internal_parent_input_remains_supported(self):
        nested = self.source / "chapters"
        nested.mkdir()
        (self.source / "intro.tex").write_text("Internal text", encoding="utf-8")
        parser = LatexParser(str(self.source), str(self.root / "out"))
        parser.main_dir = str(nested)
        self.assertIn("Internal text", parser._merge_inputs(r"\input{../intro}"))

    def test_main_metadata_cannot_select_external_file(self):
        for name, value in (
            (utils.MAIN_TEX_MARKER, "../../outside.tex"),
            ("00README.json", json.dumps({"sources": [{"usage": "toplevel", "filename": "../../outside.tex"}]})),
        ):
            with self.subTest(name=name):
                metadata = self.source / name
                metadata.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "escapes project"):
                    utils.find_main_tex_file(str(self.source))
                metadata.unlink()

    def test_constructor_rejects_external_input_write(self):
        item = {"begin": "<PLACEHOLDER_x_begin>", "end": "<PLACEHOLDER_x_end>",
                "file": "../../outside.tex", "path": "../../outside", "command": r"\input{../../outside}"}
        constructor = LatexConstructor([], [], [], [item], [], str(self.source), target_language="en")
        text = item["begin"] + "Translated" + item["end"]
        with self.assertRaisesRegex(ValueError, "escapes project"):
            constructor._revert_inputs(text)
        self.assertEqual(self.outside.read_text(), "Original")

    def test_tar_hardlink_then_file_is_rejected_before_extracting(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            link = tarfile.TarInfo("leak.tex")
            link.type = tarfile.LNKTYPE
            link.linkname = "../outside.tex"
            archive.addfile(link)
            content = b"Overwritten"
            regular = tarfile.TarInfo("leak.tex")
            regular.size = len(content)
            archive.addfile(regular, io.BytesIO(content))
        target = self.root / "extract"
        target.mkdir()
        for extract in (runtime.safe_extract_tar, utils._safe_extract_tar):
            payload.seek(0)
            with tarfile.open(fileobj=payload) as archive, self.assertRaisesRegex(ValueError, "links and special"):
                extract(archive, target)
        self.assertEqual(self.outside.read_text(), "Original")
        self.assertFalse((target / "leak.tex").exists())

    def test_source_symlink_is_rejected_before_copying(self):
        link = self.source / "linked.tex"
        try:
            link.symlink_to(self.outside)
        except OSError:
            self.skipTest("Symbolic links unavailable")
        agent = GeneratorAgent({}, str(self.source), str(self.root / "out"))
        with self.assertRaisesRegex(ValueError, "escapes project|traverses a link"):
            agent._creat_transed_latex_folder(str(self.source))
        self.assertFalse((self.root / "out" / "paper").exists())

    def create_directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic links unavailable")

    def test_derived_project_output_link_is_rejected_before_work_starts(self):
        output = self.root / "out"
        output.mkdir()
        external = self.root / "external"
        external.mkdir()
        self.create_directory_link(output / "ch_paper", external)
        with self.assertRaises(ValueError):
            CoordinatorAgent({}, str(self.source), str(output)).workflow_latextrans()
        self.assertFalse((external / ".latextrans").exists())

    def test_checkpoint_parent_link_is_rejected(self):
        output = self.root / "out"
        output.mkdir()
        external = self.root / "external"
        external.mkdir()
        self.create_directory_link(output / ".latextrans", external)
        with self.assertRaises(ValueError):
            CheckpointStore(str(output), str(self.source), {}).begin("parse")
        self.assertFalse((external / "checkpoint.json").exists())

    def test_fragment_directory_link_added_after_initialization_is_rejected(self):
        output = self.root / "out"
        store = CheckpointStore(str(output), str(self.source), {})
        store.begin("parse")
        store.run_dir.mkdir(parents=True)
        external = self.root / "external"
        external.mkdir()
        self.create_directory_link(store.run_dir / "fragments", external)
        with self.assertRaises(ValueError):
            store.save_fragment("sec", 0, {"content": "x"})
        self.assertFalse(list(external.iterdir()))


class PipelineInputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.first = self.root / "a" / "paper"
        self.second = self.root / "b" / "paper"
        self.first.mkdir(parents=True)
        self.second.mkdir(parents=True)
        self.config = {"tex_sources_dir": str(self.root / "src"), "output_dir": str(self.root / "out"),
                       "user_term": "default", "llm_config": {"base_url": ""}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, inputs):
        events = []
        with mock.patch.object(runtime, "CoordinatorAgent") as coordinator, mock.patch.object(runtime, "dns_cache"), \
                contextlib.redirect_stdout(io.StringIO()):
            coordinator.return_value.workflow_latextrans.return_value = {"status": "success"}
            result = runtime.run_pipeline(self.config, project_items=inputs, event_callback=events.append)
        return result, events, coordinator

    def test_same_name_projects_never_run_in_same_output(self):
        result, events, coordinator = self.run_pipeline([str(self.first), str(self.second)])
        self.assertEqual(coordinator.call_count, 1)
        self.assertEqual(len(result["completed_projects"]), 1)
        self.assertEqual(result["failed_projects"][0]["status"], "failed_output_conflict")
        self.assertEqual(sum(e["type"] == "project_error" for e in events), 1)

    def test_missing_local_and_download_failures_are_in_summary(self):
        self.config["paper_list"] = ["2609.14057"]
        with mock.patch.object(runtime, "download_arxiv_source", return_value=None), \
                mock.patch.object(runtime, "download_arxiv_pdf", return_value=None):
            result, events, coordinator = self.run_pipeline([str(self.first), str(self.root / "missing")])
        self.assertEqual(coordinator.call_count, 1)
        self.assertEqual({e["status"] for e in result["failed_projects"]}, {"failed_input", "failed_download"})
        self.assertEqual(sum(e["type"] == "project_error" for e in events), 2)

    def test_only_invalid_input_returns_failure(self):
        result, _, coordinator = self.run_pipeline([str(self.root / "missing")])
        self.assertEqual(coordinator.call_count, 0)
        self.assertEqual(len(result["failed_projects"]), 1)


class InstalledGuiConfigTests(unittest.TestCase):
    def test_gui_start_uses_bundled_default_outside_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            cwd = Path(folder)
            backend = mock.MagicMock()
            backend.session_state = {}
            backend.button.return_value = True
            params = {"config_path": "config/default.toml", "all_existing": False}
            inputs = {"paper_list": [], "project_items": ["valid-project"]}
            with mock.patch.object(runtime, "PROJECT_ROOT", cwd / "installed"), \
                    mock.patch.object(runtime.Path, "cwd", return_value=cwd), \
                    mock.patch.object(streamlit_app, "streamlit_backend", backend), \
                    mock.patch.object(streamlit_app, "_ensure_session_state"), \
                    mock.patch.object(streamlit_app, "_inject_style"), \
                    mock.patch.object(streamlit_app, "_load_defaults", return_value={}), \
                    mock.patch.object(streamlit_app, "_sidebar_form", return_value=params), \
                    mock.patch.object(streamlit_app, "_collect_inputs", return_value=inputs), \
                    mock.patch.object(streamlit_app, "_render_history"), \
                    mock.patch.object(streamlit_app, "_run_streamlit_job") as run:
                streamlit_app.main()
            run.assert_called_once_with(params=params, inputs=inputs, title="Current Run")
            backend.error.assert_not_called()
