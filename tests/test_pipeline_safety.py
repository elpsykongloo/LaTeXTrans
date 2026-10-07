import contextlib
import io
import json
import logging
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import toml

import main as cli_main
import src.agents.coordinator_agent as coordinator_module
import src.runtime as runtime
from src.agents.coordinator_agent import (
    STATUS_FAILED_COMPILE,
    STATUS_FAILED_GENERATION,
    STATUS_FAILED_VALIDATION,
    STATUS_SUCCESS,
    CoordinatorAgent,
    TranslationResult,
)
from src.agents.tool_agents.validator_agent import ValidatorAgent
from src.formats.latex.utils import (
    add_chinese_package,
    add_target_language_package,
    target_language_family,
)
from src.utils import progress

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_TEX = "\\documentclass{article}\n\\usepackage[T1]{fontenc}\n\\begin{document}\nx\n\\end{document}\n"


def _write_toml(path: Path, data) -> None:
    path.write_text(toml.dumps(data), encoding="utf-8")


class LayeredConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.default = self.dir / "default.toml"
        _write_toml(
            self.default,
            {
                "target_language": "ch",
                "copy_to_downloads": True,
                "llm_config": {"model": "base-model", "api_key": "", "base_url": "https://base", "concurrency_limit": 5},
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_toml_deep_merges_over_default(self):
        _write_toml(self.dir / "local.toml", {"llm_config": {"api_key": "local-secret"}})
        config = runtime.load_layered_config(str(self.default), env={})
        self.assertEqual(config["llm_config"]["api_key"], "local-secret")
        # 深度合并：未覆盖的键保留
        self.assertEqual(config["llm_config"]["model"], "base-model")
        self.assertEqual(config["llm_config"]["concurrency_limit"], 5)

    def test_missing_local_toml_is_optional(self):
        config = runtime.load_layered_config(str(self.default), env={})
        self.assertEqual(config["llm_config"]["api_key"], "")

    def test_env_overrides_local_and_cli_overrides_env(self):
        _write_toml(self.dir / "local.toml", {"llm_config": {"api_key": "local-secret", "model": "local-model"}})
        env = {
            "LATEXTRANS_API_KEY": "env-secret",
            "LATEXTRANS_BASE_URL": "https://env",
            "LATEXTRANS_MODEL": "env-model",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = runtime.load_runtime_config(str(self.default), overrides={})
            self.assertEqual(config["llm_config"]["api_key"], "env-secret")
            self.assertEqual(config["llm_config"]["base_url"], "https://env")
            self.assertEqual(config["llm_config"]["model"], "env-model")

            config = runtime.load_runtime_config(
                str(self.default), overrides={"key": "cli-secret", "model": "cli-model"}
            )
            self.assertEqual(config["llm_config"]["api_key"], "cli-secret")
            self.assertEqual(config["llm_config"]["model"], "cli-model")

    def test_downloads_overrides(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            config = runtime.load_runtime_config(str(self.default), overrides={"copy_to_downloads": None})
            self.assertTrue(config["copy_to_downloads"])
            config = runtime.load_runtime_config(
                str(self.default), overrides={"copy_to_downloads": False, "downloads_dir": "D:/x"}
            )
            self.assertFalse(config["copy_to_downloads"])
            self.assertEqual(config["downloads_dir"], "D:/x")

    def test_tracked_default_config_has_no_secret(self):
        default = toml.load(REPO_ROOT / "config" / "default.toml")
        self.assertEqual(default["llm_config"]["api_key"], "")
        self.assertTrue(default.get("copy_to_downloads"))
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("config/local.toml", [line.strip() for line in gitignore])


class _FakeCoordinator:
    outcomes = {}

    def __init__(self, config, project_dir, output_dir):
        self.project_dir = project_dir

    def workflow_latextrans(self):
        outcome = self.outcomes[os.path.basename(self.project_dir)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class PipelineStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = []
        for name in ("ok", "bad_val", "bad_gen", "bad_compile", "boom"):
            path = root / "src" / name
            path.mkdir(parents=True)
            self.projects.append(str(path))
        self.config = {
            "tex_sources_dir": str(root / "src"),
            "output_dir": str(root / "out"),
            "paper_list": [],
            "user_term": "x",
            "project_concurrency": 2,
            "llm_config": {"base_url": ""},
        }
        _FakeCoordinator.outcomes = {
            "ok": TranslationResult(status=STATUS_SUCCESS, pdf_path="ok.pdf"),
            "bad_val": TranslationResult(status=STATUS_FAILED_VALIDATION, message="校验失败"),
            "bad_gen": TranslationResult(status=STATUS_FAILED_GENERATION, message="生成失败"),
            "bad_compile": TranslationResult(status=STATUS_FAILED_COMPILE, message="编译失败"),
            "boom": RuntimeError("解析失败"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_statuses_are_not_reported_as_completed(self):
        events = []
        with mock.patch.object(runtime, "CoordinatorAgent", _FakeCoordinator), \
                mock.patch.object(runtime, "dns_cache"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = runtime.run_pipeline(
                self.config, project_items=self.projects, event_callback=events.append
            )

        self.assertEqual([p["project_name"] for p in result["completed_projects"]], ["ok"])
        self.assertEqual(result["completed_projects"][0]["pdf_path"], "ok.pdf")
        failed = {p["project_name"]: p for p in result["failed_projects"]}
        self.assertEqual(set(failed), {"bad_val", "bad_gen", "bad_compile", "boom"})
        self.assertEqual(failed["bad_val"]["status"], STATUS_FAILED_VALIDATION)
        self.assertEqual(failed["bad_compile"]["status"], STATUS_FAILED_COMPILE)
        self.assertEqual(failed["boom"]["status"], "error")
        self.assertIn("解析失败", failed["boom"]["error"])
        types = [e["type"] for e in events]
        self.assertEqual(types.count("project_complete"), 1)
        self.assertEqual(types.count("project_error"), 4)

        summary = runtime.format_run_summary(result)
        self.assertIn("成功 1 篇，失败 4 篇", summary)

    def test_cli_exit_code_reflects_failures(self):
        failed = {"completed_projects": [], "failed_projects": [{"project_name": "p", "status": STATUS_FAILED_COMPILE, "error": "e"}]}
        ok = {"completed_projects": [{"project_name": "p", "pdf_path": "p.pdf"}], "failed_projects": []}
        for result, expected in ((failed, 1), (ok, 0)):
            with mock.patch.object(cli_main, "run_translation", return_value=result) as run, \
                    mock.patch("sys.argv", ["latextrans", "--project", "p", "--no-downloads"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main.main(), expected)
            overrides = run.call_args.kwargs["overrides"]
            self.assertIs(overrides["copy_to_downloads"], False)

    def test_cli_without_flag_keeps_downloads_default(self):
        ok = {"completed_projects": [], "failed_projects": []}
        with mock.patch.object(cli_main, "run_translation", return_value=ok) as run, \
                mock.patch("sys.argv", ["latextrans", "--project", "p"]), \
                contextlib.redirect_stdout(io.StringIO()):
            cli_main.main()
        self.assertIsNone(run.call_args.kwargs["overrides"]["copy_to_downloads"])


class _FakeParser:
    def __init__(self, **kwargs):
        pass

    def execute(self):
        pass


class _FakeTranslator:
    def __init__(self, **kwargs):
        self.trans_mode = 0

    async def execute(self, **kwargs):
        pass


class _FakeTranslatorWithUsage(_FakeTranslator):
    def usage_summary(self):
        return {"requests": 3, "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


class _FakeValidator:
    report = []
    released = []

    def __init__(self, **kwargs):
        pass

    def execute(self, errors_report=None):
        return list(self.report)

    @classmethod
    def release_cache(cls, output_dir):
        cls.released.append(output_dir)


class CoordinatorResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project_dir = root / "paper"
        self.project_dir.mkdir()
        self.output_dir = root / "out"
        self.downloads = root / "dl"
        self.pdf = root / "built.pdf"
        _FakeValidator.report = []
        _FakeValidator.released = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, generator_execute, translator=_FakeTranslator, **config):
        generator = mock.Mock()
        generator.return_value.execute.side_effect = generator_execute
        base = {"target_language": "ch", "downloads_dir": str(self.downloads)}
        base.update(config)
        with mock.patch.object(coordinator_module, "ParserAgent", _FakeParser), \
                mock.patch.object(coordinator_module, "TranslatorAgent", translator), \
                mock.patch.object(coordinator_module, "ValidatorAgent", _FakeValidator), \
                mock.patch.object(coordinator_module, "GeneratorAgent", generator), \
                contextlib.redirect_stdout(io.StringIO()):
            agent = CoordinatorAgent(base, project_dir=str(self.project_dir), output_dir=str(self.output_dir))
            return agent.workflow_latextrans()

    def _make_pdf(self):
        self.pdf.write_bytes(b"%PDF-1.4")
        return str(self.pdf)

    def test_validation_failure(self):
        _FakeValidator.report = [{"part": "sec", "num_or_ph": "1"}]
        result = self._run(lambda: self.fail("generator must not run"))
        self.assertEqual(result.status, STATUS_FAILED_VALIDATION)
        self.assertIn("sec:1", result.message)
        self.assertEqual(len(_FakeValidator.released), 1)

    def test_generation_exception(self):
        def boom():
            raise ValueError("占位符缺失")
        result = self._run(boom)
        self.assertEqual(result.status, STATUS_FAILED_GENERATION)
        self.assertIn("占位符缺失", result.message)

    def test_compile_failure(self):
        result = self._run(lambda: None)
        self.assertEqual(result.status, STATUS_FAILED_COMPILE)
        self.assertIsNone(result.pdf_path)

    def test_success_copies_to_downloads_dir(self):
        result = self._run(self._make_pdf)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertTrue(os.path.isfile(result.pdf_path))
        self.assertIsNotNone(result.downloads_path)
        self.assertEqual(Path(result.downloads_path).parent, self.downloads)

    def test_success_without_downloads_copy(self):
        result = self._run(self._make_pdf, copy_to_downloads=False)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertIsNone(result.downloads_path)
        self.assertFalse(self.downloads.exists())

    def test_string_false_disables_downloads(self):
        result = self._run(self._make_pdf, copy_to_downloads="False")
        self.assertIsNone(result.downloads_path)

    def test_usage_written_when_available(self):
        result = self._run(lambda: None, translator=_FakeTranslatorWithUsage)
        usage_path = self.output_dir / "ch_paper" / "usage.json"
        self.assertTrue(usage_path.is_file())
        self.assertEqual(json.loads(usage_path.read_text(encoding="utf-8"))["total_tokens"], 150)
        self.assertEqual(result.usage["requests"], 3)

    def test_usage_absent_is_tolerated(self):
        result = self._run(lambda: None)
        self.assertFalse((self.output_dir / "ch_paper" / "usage.json").exists())
        self.assertIsNone(result.usage)

    def test_usage_hook_errors_are_swallowed(self):
        class Broken(_FakeTranslator):
            def usage_summary(self):
                raise RuntimeError("bad")
        result = self._run(lambda: None, translator=Broken)
        self.assertEqual(result.status, STATUS_FAILED_COMPILE)


class ValidatorCacheTests(unittest.TestCase):
    def test_cache_is_partitioned_by_project_and_released(self):
        a = ValidatorAgent({}, project_dir="p", output_dir="out_a")
        a2 = ValidatorAgent({}, project_dir="p", output_dir="out_a")
        b = ValidatorAgent({}, project_dir="p", output_dir="out_b")
        part = {"section": "1", "content": "a \\textbf{x}", "trans_content": "甲 \\textbf{x}"}
        a._validate(part)
        key = (part["content"], part["trans_content"])
        self.assertIsNotNone(a2._cache_get(key))
        self.assertIsNone(b._cache_get(key))
        ValidatorAgent.release_cache("out_a")
        self.assertIsNone(a._cache_get(key))
        self.assertNotIn("_results_cache", ValidatorAgent.__dict__)

    def test_concurrent_validation_across_projects(self):
        errors = []

        def work(idx):
            try:
                agent = ValidatorAgent({}, project_dir="p", output_dir=f"out_thread_{idx % 3}")
                for n in range(300):
                    part = {"section": str(n), "content": f"t{n} $x$", "trans_content": f"译{n} $x$"}
                    self.assertIsNone(agent._validate(part))
            except Exception as exc:  # pragma: no cover - 仅在失败时记录
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for i in range(3):
            ValidatorAgent.release_cache(f"out_thread_{i}")
        self.assertEqual(errors, [])


class ThreadSafeStderrTests(unittest.TestCase):
    def test_no_global_stderr_swaps_in_pipeline_modules(self):
        for relative in (
            "src/agents/tool_agents/generator_agent.py",
            "src/agents/tool_agents/parser_agent.py",
            "src/formats/latex/parser.py",
            "src/formats/latex/utils.py",
            "src/agents/coordinator_agent.py",
            "src/runtime.py",
        ):
            source = (REPO_ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"sys\.stderr\s*=", source), relative)

    def test_streamlit_context_warning_is_filtered(self):
        progress.silence_streamlit_context_warnings()
        logger = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
        record = logger.makeRecord(
            logger.name, logging.WARNING, __file__, 1,
            "Thread '%s': missing ScriptRunContext! This warning can be ignored", ("t",), None,
        )
        self.assertFalse(logger.filter(record))
        other = logger.makeRecord(logger.name, logging.WARNING, __file__, 1, "other", (), None)
        self.assertTrue(logger.filter(other))


class TargetLanguageTests(unittest.TestCase):
    def test_family(self):
        self.assertEqual(target_language_family("ch"), "ch")
        self.assertEqual(target_language_family("zh"), "ch")
        self.assertEqual(target_language_family("ja"), "ja")
        self.assertEqual(target_language_family("fr"), "other")

    def test_chinese_path_unchanged(self):
        self.assertEqual(add_target_language_package(SAMPLE_TEX, "ch"), add_chinese_package(SAMPLE_TEX))
        self.assertNotIn("CJK JP", add_chinese_package(SAMPLE_TEX))

    def test_japanese_uses_xecjk_with_japanese_fonts(self):
        result = add_target_language_package("\\pdfoutput=1\n" + SAMPLE_TEX, "ja")
        self.assertIn("\\usepackage{xeCJK}", result)
        self.assertIn("Noto Serif CJK JP", result)
        self.assertIn("IPAexMincho", result)
        self.assertNotIn("\\pdfoutput", result)
        self.assertNotIn("fontenc", result)

    def test_japanese_respects_existing_luatexja(self):
        source = SAMPLE_TEX.replace("\\begin{document}", "\\usepackage{luatexja}\n\\begin{document}")
        self.assertEqual(add_target_language_package(source, "ja"), source)

    def test_other_language_gets_no_cjk_injection(self):
        self.assertEqual(add_target_language_package(SAMPLE_TEX, "fr"), SAMPLE_TEX)


if __name__ == "__main__":
    unittest.main()
