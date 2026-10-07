import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.agents.tool_agents.generator_agent import GeneratorAgent
from src.formats.latex.compile import LaTexCompiler, extract_latex_errors
from src.formats.latex.repair import LatexCompileRepairAgent


SAMPLE = "\\documentclass{article}\n\\begin{document}\nA & B\n\\end{document}\n"


def failure_for(root, line=3, message="Misplaced alignment tab character &."):
    return {
        "engine": "xelatex", "kind": "tex_error", "returncode": 1,
        "message": message,
        "errors": extract_latex_errors(f"{root.as_posix()}/main.tex:{line}: {message}", root / "main.tex", root),
    }


def edit(old="A & B", new=r"A \& B", file="main.tex", line=3):
    return {"file": file, "start_line": line, "end_line": line, "old_text": old, "new_text": new, "reason": "Escape the text ampersand."}


class FakeCompiler:
    def __init__(self, root, results=(True,)):
        self.output_latex_dir = str(root)
        self.root = root
        self.last_failure = failure_for(root)
        self.failures = [self.last_failure]
        self.results = list(results)
        self.calls = 0

    def compile(self):
        self.calls += 1
        if self.results and self.results.pop(0):
            path = self.root / "build_xelatex" / "main.pdf"
            path.parent.mkdir(exist_ok=True)
            path.write_text("a newly compiled test PDF", encoding="utf-8")
            self.last_failure = None
            self.failures = []
            return str(path)
        self.last_failure = failure_for(self.root)
        self.failures = [self.last_failure]
        return None


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.generated = root / "generated"
        self.source.mkdir()
        self.generated.mkdir()
        (self.source / "main.tex").write_text(SAMPLE, encoding="utf-8")
        (self.generated / "main.tex").write_text(SAMPLE, encoding="utf-8")
        self.agent = LatexCompileRepairAgent({}, str(self.generated), str(self.source))

    def tearDown(self):
        self.temp.cleanup()

    def run_repair(self, compiler, responses, attempts=None):
        self.agent._request_edits = mock.AsyncMock(side_effect=responses)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.agent.execute(compiler, max_attempts=attempts)

    def test_local_repair_preserves_backup_source_and_audit_report(self):
        compiler = FakeCompiler(self.generated)
        pdf = self.run_repair(compiler, [{"explanation": "Unescaped ampersand", "edits": [edit()]}])
        self.assertTrue(Path(pdf).is_file())
        self.assertEqual((self.source / "main.tex").read_text(encoding="utf-8"), SAMPLE)
        self.assertIn(r"A \& B", (self.generated / "main.tex").read_text(encoding="utf-8"))
        backup = self.generated / ".compile_repair_backups" / "attempt_01" / "main.tex.bak"
        self.assertEqual(backup.read_text(encoding="utf-8"), SAMPLE)
        report = json.loads(self.agent.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "repaired")
        self.assertEqual(report["attempts"][0]["edits"][0]["start_line"], 3)
        self.assertTrue(self.agent.usage_path.is_file())

    def test_all_edits_validated_before_any_file_changes(self):
        compiler = FakeCompiler(self.generated)
        bad = edit(file="../source/main.tex")
        self.assertIsNone(self.run_repair(compiler, [{"edits": [edit(), bad]}]))
        self.assertEqual((self.generated / "main.tex").read_text(encoding="utf-8"), SAMPLE)
        self.assertEqual((self.source / "main.tex").read_text(encoding="utf-8"), SAMPLE)
        self.assertEqual(compiler.calls, 0)
        self.assertEqual(self.agent.report["status"], "repair_failed")

    def test_unsafe_paths_and_non_tex_files_are_rejected(self):
        for name in ("../source/main.tex", "/main.tex", "C:/main.tex", "C:main.tex", "dir\\main.tex", "style.sty", "build_xelatex/main.tex", "main.tex:stream", "./main.tex", "CON.tex"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.agent._safe_tex_path(name)

    def test_symlink_to_source_is_not_editable(self):
        link = self.generated / "linked.tex"
        try:
            link.symlink_to(self.source / "main.tex")
        except OSError:
            self.skipTest("This Windows account cannot create symlinks")
        with self.assertRaises(ValueError):
            self.agent._safe_tex_path("linked.tex")

    def test_atomic_repair_does_not_modify_a_hardlinked_source(self):
        translated = self.generated / "main.tex"
        translated.unlink()
        os.link(self.source / "main.tex", translated)
        self.run_repair(FakeCompiler(self.generated), [{"edits": [edit()]}])
        self.assertEqual((self.source / "main.tex").read_text(encoding="utf-8"), SAMPLE)
        self.assertIn(r"\&", translated.read_text(encoding="utf-8"))

    def test_environment_error_makes_no_model_request(self):
        compiler = FakeCompiler(self.generated)
        compiler.last_failure = failure_for(self.generated, 3, "LaTeX Error: File `missing.sty' not found.")
        compiler.last_failure["kind"] = "environment"
        compiler.failures = [compiler.last_failure]
        self.assertIsNone(self.run_repair(compiler, []))
        self.agent._request_edits.assert_not_awaited()
        self.assertEqual(self.agent.report["status"], "skipped_unrepairable")
        self.assertEqual(compiler.calls, 0)

    def test_retries_are_bounded_and_failure_remains_failure(self):
        compiler = FakeCompiler(self.generated, results=(False, False, False))
        responses = [{"edits": [edit(new=r"A \& B")]}, {"edits": [edit(old=r"A \& B", new=r"A \&{} B")]}]
        self.assertIsNone(self.run_repair(compiler, responses))
        self.assertEqual(compiler.calls, 2)
        self.assertEqual(self.agent._request_edits.await_count, 2)
        self.assertEqual(self.agent.report["status"], "failed")
        self.assertIsNotNone(self.agent.report["final_failure"])
        self.assertTrue((self.generated / ".compile_repair_backups" / "attempt_02" / "main.tex.bak").is_file())

    def test_large_attempt_configuration_cannot_loop_forever(self):
        compiler = FakeCompiler(self.generated, results=(False,) * 10)
        responses = []
        previous = "A & B"
        for index in range(self.agent.MAX_ATTEMPTS):
            following = "A " + "{}" * (index + 1) + " B"
            responses.append({"edits": [edit(old=previous, new=following)]})
            previous = following
        self.run_repair(compiler, responses, attempts=999)
        self.assertEqual(compiler.calls, 5)

    def test_context_is_a_small_window_in_large_document(self):
        lines = ["\\documentclass{article}", "\\begin{document}"] + ["A paragraph." for _ in range(300)] + ["\\end{document}"]
        lines[150] = "A & B"
        (self.generated / "main.tex").write_text("\n".join(lines), encoding="utf-8")
        compiler = FakeCompiler(self.generated)
        compiler.last_failure = failure_for(self.generated, line=151)
        context = self.agent._build_context(compiler)
        self.assertEqual(len(context["contexts"][0]["lines"]), 17)
        self.assertNotIn("documentclass", json.dumps(context))
        self.assertLess(len(json.dumps(context)), self.agent.MAX_CONTEXT_CHARS)

    def test_patch_cannot_change_unseen_lines_or_import_dependencies(self):
        contexts = self.agent._build_context(FakeCompiler(self.generated))["contexts"]
        for patch in (edit(line=100), edit(new=r"\input{../../secret}"), edit(new=r"\csname input\endcsname{../../secret}"), edit(new=""), edit(new="% removed paragraph"), edit(old="wrong line")):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.agent._validate_edits([patch], contexts)

    def test_source_tree_or_overlapping_project_roots_are_rejected(self):
        for root in (self.source, self.source / "output", self.source.parent):
            with self.subTest(root=root), self.assertRaises(ValueError):
                LatexCompileRepairAgent({}, str(root), str(self.source))

    def test_generated_folder_cannot_delete_source(self):
        generator = GeneratorAgent({}, project_dir=str(self.source), output_dir=str(self.source.parent))
        with self.assertRaises(ValueError):
            generator._creat_transed_latex_folder(str(self.source))
        self.assertTrue((self.source / "main.tex").is_file())

    def test_switch_disables_repair_and_existing_success_does_not_call_model(self):
        for setting in (False, "false", "off", 0):
            with self.subTest(setting=setting):
                generator = GeneratorAgent({"compile_repair": setting}, project_dir=str(self.source), output_dir=str(self.generated.parent))
                with mock.patch("src.formats.latex.compile.LaTexCompiler", return_value=FakeCompiler(self.generated, (False,))), \
                     mock.patch("src.formats.latex.repair.LatexCompileRepairAgent") as repair:
                    self.assertIsNone(generator._compile_generated_project(str(self.generated)))
                    repair.assert_not_called()
        generator = GeneratorAgent({}, project_dir=str(self.source), output_dir=str(self.generated.parent))
        with mock.patch("src.formats.latex.compile.LaTexCompiler", return_value=FakeCompiler(self.generated)), \
             mock.patch("src.formats.latex.repair.LatexCompileRepairAgent") as repair:
            self.assertTrue(generator._compile_generated_project(str(self.generated)))
            repair.assert_not_called()


class CompilerDiagnosticsTests(unittest.TestCase):
    def test_wrapped_existing_tex_paths_are_recovered_at_different_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = root / "main.tex"
            main.write_text(SAMPLE, encoding="utf-8")
            path = main.as_posix()
            for split in (len(path) - 6, len(path) - 4, len(path) - 3):
                with self.subTest(split=split):
                    text = f"{path[:split]}\n{path[split:]}:3: Misplaced alignment tab character &.\nl.3 A & B\n"
                    errors = extract_latex_errors(text, main, root)
                    self.assertEqual(len(errors), 1)
                    self.assertEqual(errors[0]["file"], "main.tex")
                    self.assertEqual(errors[0]["line"], 3)
                    self.assertTrue(errors[0]["repairable"])

    def test_ordinary_log_lines_cannot_be_joined_into_tex_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = root / "main.tex"
            main.write_text(SAMPLE, encoding="utf-8")
            for text in (
                f"See {root.as_posix()}/main\n.tex:3: Ordinary message.\n",
                f"{root.as_posix()}/nonexistent\n.tex:3: Ordinary message.\n",
                f"{root.as_posix()}/main\n  .tex:3: Indented text.\n",
            ):
                with self.subTest(text=text):
                    self.assertEqual(extract_latex_errors(text, main, root), [])

    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("latexmk"), "XeLaTeX/latexmk are unavailable")
    def test_real_long_path_xelatex_failure_with_old_and_new_print_width(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "generated_very_long_document_directory_name_for_diagnostic_wrapping" / "chapter_materials_translated_version"
            root.mkdir(parents=True)
            main = root / "main.tex"
            main.write_text("\\documentclass{article}\n\\usepackage{fontspec}\n\\begin{document}\nA & B\n\\end{document}\n", encoding="utf-8")
            compiler = LaTexCompiler(str(root))
            old_environment = dict(os.environ, max_print_line="79")
            with mock.patch.object(compiler, "_preferred_engines", return_value=["xelatex"]), \
                 mock.patch.object(compiler, "_tex_environment", return_value=old_environment), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(compiler.compile())
            old_log = (root / "build_xelatex" / "main.log").read_text(encoding="utf-8", errors="replace")
            full_diagnostic_path = main.as_posix().lower() + ":4:"
            self.assertNotIn(full_diagnostic_path, old_log.lower())
            self.assertEqual(compiler.last_failure["kind"], "tex_error")
            self.assertEqual(compiler.last_failure["errors"][0]["file"], "main.tex")
            self.assertEqual(compiler.last_failure["errors"][0]["line"], 4)
            with mock.patch.object(compiler, "_preferred_engines", return_value=["xelatex"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(compiler.compile())
            current_log = (root / "build_xelatex" / "main.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn(full_diagnostic_path, current_log.lower())
            self.assertEqual(compiler.last_failure["errors"][0]["line"], 4)

    def test_aux_convergence_uses_parsed_meaning_and_nested_include_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = root / "main.tex"
            compiler = LaTexCompiler(str(root))
            aux = root / "main.aux"
            aux.write_text("\\relax\n\\newlabel{sec:a}{{1}{2}}\n\\@input{sub.aux}\n", encoding="utf-8")
            sub = root / "sub.aux"
            sub.write_text("\\newlabel{sec:b}{{2}{3}}\n", encoding="utf-8")
            before = compiler._aux_snapshot(root, main)
            aux.write_text("% an irrelevant comment\n\\relax\n\\newlabel {sec:a} {{1}{2}}\n\\@input {sub.aux}\n", encoding="utf-8")
            self.assertEqual(compiler._aux_snapshot(root, main), before)
            sub.write_text("\\newlabel{sec:b}{{2}{4}}\n", encoding="utf-8")
            self.assertNotEqual(compiler._aux_snapshot(root, main), before)

    def test_bibtex_arguments_and_toc_order_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = root / "main.tex"
            aux = root / "main.aux"
            aux.write_text("\\citation{a,b}\n\\bibdata{refs}\n\\bibstyle{plain}\n", encoding="utf-8")
            before = LaTexCompiler._bibtex_inputs(root, main)
            aux.write_text("% comment\n\\citation {a,b}\n\\bibdata {refs}\n\\bibstyle {plain}\n", encoding="utf-8")
            self.assertEqual(LaTexCompiler._bibtex_inputs(root, main), before)
            aux.write_text("\\citation{a,c}\n\\bibdata{refs}\n\\bibstyle{plain}\n", encoding="utf-8")
            self.assertNotEqual(LaTexCompiler._bibtex_inputs(root, main), before)
            compiler = LaTexCompiler(str(root))
            toc = root / "main.toc"
            toc.write_text("\\contentsline{section}{A}{1}\n\\contentsline{section}{B}{2}\n", encoding="utf-8")
            first = compiler._aux_snapshot(root, main)
            toc.write_text("\\contentsline{section}{B}{2}\n\\contentsline{section}{A}{1}\n", encoding="utf-8")
            self.assertNotEqual(compiler._aux_snapshot(root, main), first)

    def test_nested_file_error_and_class_error_are_localized(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            text = "(./main.tex\n(./sections/body.tex\n! Undefined control sequence.\nl.23 \\broken\n)\n./template.cls:9: Undefined control sequence.\n"
            errors = extract_latex_errors(text, root / "main.tex", root)
            self.assertEqual(errors[0]["file"], "sections/body.tex")
            self.assertEqual(errors[0]["line"], 23)
            self.assertTrue(errors[0]["repairable"])
            self.assertFalse(errors[1]["repairable"])

    def test_missing_package_and_font_are_environment_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for message in ("LaTeX Error: File `missing.sty' not found.", 'Package fontspec Error: The font "Nonexistent" cannot be found.'):
                error = extract_latex_errors(f"./main.tex:2: {message}", root / "main.tex", root)[0]
                self.assertTrue(error["environment_error"])
                self.assertFalse(error["repairable"])

    def test_nonzero_returncode_with_pdf_is_a_real_failure_and_log_is_fresh(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.tex").write_text(SAMPLE, encoding="utf-8")
            out = root / "build_xelatex"
            out.mkdir()
            (out / "main.pdf").write_text("old PDF", encoding="utf-8")
            (out / "main.log").write_text("./main.tex:2: Old error.", encoding="utf-8")
            (root / "success.txt").write_text("old success", encoding="utf-8")
            compiler = LaTexCompiler(str(root))

            def failed(command, **kwargs):
                (out / "main.pdf").write_text("partial PDF despite failure", encoding="utf-8")
                (out / "main.log").write_text("./main.tex:3: Misplaced alignment tab character &.", encoding="utf-8")
                return subprocess.CompletedProcess(command, 1, "", "")

            with mock.patch.object(compiler, "_preferred_engines", return_value=["xelatex"]), \
                 mock.patch.object(compiler, "_can_use_fast_path", return_value=False), \
                 mock.patch("src.formats.latex.compile.subprocess.run", side_effect=failed), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(compiler.compile())
            self.assertFalse((out / "main.pdf").exists())
            self.assertFalse((root / "success.txt").exists())
            self.assertEqual(compiler.last_failure["returncode"], 1)
            self.assertEqual(compiler.last_failure["errors"][0]["line"], 3)
            self.assertNotIn("Old error", json.dumps(compiler.last_failure))

    def test_unicode_pretex_applies_to_fast_and_latexmk_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.tex").write_text(SAMPLE, encoding="utf-8")
            compiler = LaTexCompiler(str(root))
            with mock.patch("src.formats.latex.compile.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                compiler._compile_with_engine(str(root / "main.tex"), str(root / "build_xelatex"), "xelatex")
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn("-jobname=main", commands[0])
            self.assertIn(r"\providecommand\pdfinfo[1]{}", commands[0][-1])
            self.assertTrue(any(arg.startswith("-usepretex=") for arg in commands[-1]))
            for call in run.call_args_list:
                self.assertGreaterEqual(int(call.kwargs["env"]["max_print_line"]), 10000)
                self.assertEqual(call.kwargs["env"].get("PATH"), os.environ.get("PATH"))

    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("latexmk"), "XeLaTeX/latexmk are unavailable")
    def test_real_xelatex_failure_is_repaired_in_generated_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, generated = root / "source", root / "generated"
            source.mkdir()
            generated.mkdir()
            text = "\\documentclass{article}\n\\usepackage{fontspec}\n\\pdfinfo{/Author (Test) /Title (Metadata)}\n\\pdfoutput=1\n\\begin{document}\nA & B\n\\end{document}\n"
            (source / "main.tex").write_text(text, encoding="utf-8")
            (generated / "main.tex").write_text(text, encoding="utf-8")
            compiler = LaTexCompiler(str(generated))
            agent = LatexCompileRepairAgent({}, str(generated), str(source))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(compiler.compile())
                self.assertEqual(compiler.last_failure["errors"][0]["line"], 6)
                agent._request_edits = mock.AsyncMock(return_value={"edits": [edit(line=6)]})
                pdf = agent.execute(compiler)
            self.assertTrue(Path(pdf).is_file())
            self.assertTrue((generated / "success.txt").is_file())
            self.assertEqual((source / "main.tex").read_text(encoding="utf-8"), text)
            self.assertEqual(agent.report["status"], "repaired")


class _Response:
    def __init__(self, status, result):
        self.status, self.result, self.headers = status, result, {"Retry-After": "0"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status != 200:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.result


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class RepairRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_generator_execute_can_repair_inside_running_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "source", root / "output"
            source.mkdir()
            (source / "main.tex").write_text(SAMPLE, encoding="utf-8")
            generator = GeneratorAgent({}, project_dir=str(source), output_dir=str(output))
            compiler_factory = lambda **kwargs: FakeCompiler(Path(kwargs["output_latex_dir"]), (False, True))
            with mock.patch.object(generator, "read_file", return_value=[]), \
                 mock.patch("src.formats.latex.reconstruct.LatexConstructor"), \
                 mock.patch("src.formats.latex.compile.LaTexCompiler", side_effect=compiler_factory), \
                 mock.patch.object(LatexCompileRepairAgent, "_request_edits", new=mock.AsyncMock(return_value={"edits": [edit()]})) as request, \
                 contextlib.redirect_stdout(io.StringIO()):
                pdf = generator.execute()
            self.assertTrue(Path(pdf).is_file())
            request.assert_awaited_once()
            self.assertIsNone(generator.compile_failure)
            self.assertEqual(json.loads(Path(generator.repair_report_path).read_text(encoding="utf-8"))["status"], "repaired")
            self.assertEqual((source / "main.tex").read_text(encoding="utf-8"), SAMPLE)

    async def test_reuses_compatibility_timeout_backoff_and_usage_tracking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").mkdir()
            (root / "generated").mkdir()
            config = {"llm_config": {"base_url": "https://example.invalid/v1/", "api_key": "test-only-key", "model": "test-model", "request_timeout": 41, "thinking_type": "disabled", "rate_limit_backoff_base": 0}}
            agent = LatexCompileRepairAgent(config, str(root / "generated"), str(root / "source"))
            result = {"choices": [{"message": {"content": json.dumps({"edits": []})}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
            session = _Session([_Response(429, {}), _Response(200, result)])
            with mock.patch("aiohttp.ClientSession", return_value=session), mock.patch("asyncio.sleep", new=mock.AsyncMock()) as sleep:
                response = await agent._request_edits({"errors": [], "contexts": []})
            self.assertEqual(response["edits"], [])
            self.assertEqual(len(session.calls), 2)
            self.assertEqual(session.calls[0][0], "https://example.invalid/v1/chat/completions")
            self.assertEqual(session.calls[0][1]["timeout"].sock_read, 41)
            self.assertEqual(session.calls[0][1]["json"]["thinking"], {"type": "disabled"})
            sleep.assert_awaited_once()
            self.assertEqual(agent.usage.snapshot()["total_tokens"], 150)
            self.assertEqual(agent.usage.snapshot()["events"]["rate_limited_retries"], 1)


if __name__ == "__main__":
    unittest.main()
