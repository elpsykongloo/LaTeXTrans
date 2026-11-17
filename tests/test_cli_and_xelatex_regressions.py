import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.formats.latex.utils import (
    add_chinese_package,
    extract_arxiv_ids,
    extract_arxiv_ids_V2,
    normalize_latex_reference_arguments,
    patch_xelatex_compatibility_files,
    strip_unexpected_placeholders,
    validate_latex_structure,
)
from src.formats.latex.reconstruct import LatexConstructor
from src.formats.latex.parser import LatexParser


class CliAndXeLaTeXRegressionTests(unittest.TestCase):
    def test_samepage_can_span_sections_without_parser_warning(self):
        parser = LatexParser(dir="", output_dir="")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            first = parser._extract_envs(r"\begin{samepage}\section{A}")
            second = parser._extract_envs(r"Text\end{samepage}")

        self.assertNotIn("Unclosed LaTeX environment", output.getvalue())
        self.assertEqual(
            validate_latex_structure(
                r"\begin{document}" + first + second + r"\end{document}"
            ),
            [],
        )

    def test_threeparttable_tablenotes_accepts_items(self):
        source = (
            "\\begin{document}\n"
            "\\begin{table*}\n"
            "\\begin{threeparttable}\n"
            "\\begin{tablenotes}\n"
            "\\item[1] First note.\n"
            "\\item[2] Second note.\n"
            "\\end{tablenotes}\n"
            "\\end{threeparttable}\n"
            "\\end{table*}\n"
            "\\end{document}\n"
        )

        self.assertEqual(validate_latex_structure(source), [])
        broken = source.replace(
            "\\end{tablenotes}\n",
            "\\end{tablenotes}\n\\item[3] Outside.\n",
            1,
        )
        errors = validate_latex_structure(broken)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("第 8 行：\\item 位于列表环境之外"))

    def test_constructor_configures_nested_preamble_before_restoring_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "main.tex").write_text(r"\input{template}\begin{document}\end{document}", encoding="utf-8")
            source = (
                "<PLACEHOLDER_template_begin><PLACEHOLDER_class_begin>"
                "\\documentclass{article}\n<PLACEHOLDER_class_end>\n"
                "\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n"
                "\\usepackage{CJKutf8}\n"
                "\\DeclareUnicodeCharacter{2192}{\\ensuremath{\\rightarrow}}\n"
                "<PLACEHOLDER_template_end>\\begin{document}中文\\end{document}"
            )
            inputs = [dict(command=rf"\input{{{name}}}", path=name,
                           begin=f"<PLACEHOLDER_{name}_begin>",
                           end=f"<PLACEHOLDER_{name}_end>") for name in ("template", "class")]
            LatexConstructor(
                sections=[dict(section="-1", content=source, trans_content=source)],
                captions=[], envs=[], inputs=inputs, newcommands=[], output_latex_dir=temp_dir,
            ).construct()
            preamble = (root / "class.tex").read_text(encoding="utf-8")
            packages = (root / "template.tex").read_text(encoding="utf-8")
            self.assertIn(r"\usepackage{xeCJK}", preamble)
            self.assertIn(r"\usepackage{newunicodechar}", preamble)
            self.assertIn(r"\newunicodechar{→}{\ensuremath{\rightarrow}}", packages)
            self.assertNotIn("CJKutf8", packages)
            self.assertNotIn("inputenc", packages)
            self.assertNotIn("fontenc", packages)
            self.assertIn(r"\input{template}", (root / "main.tex").read_text(encoding="utf-8"))

    def test_extracts_ids_from_all_supported_arxiv_url_routes(self):
        inputs = [
            "https://arxiv.org/html/2504.11343v2",
            "https://arxiv.org/abs/2504.11343v2",
            "https://arxiv.org/pdf/2504.11343v2.pdf",
            "https://arxiv.org/e-print/2504.11343v2",
            "2504.11343v2",
        ]

        self.assertEqual(
            extract_arxiv_ids(inputs),
            ["2504.11343v2"] * len(inputs),
        )
        self.assertEqual(
            extract_arxiv_ids_V2("https://arxiv.org/html/2504.11343v2"),
            "2504.11343v2",
        )

    def test_removes_pdftex_output_directive_for_unicode_engine(self):
        source = (
            r"\documentclass{article}"
            "\n\\pdfoutput=1\n"
            r"\begin{document}中文\end{document}"
        )

        translated = add_chinese_package(source)

        self.assertIn(r"\usepackage{xeCJK}", translated)
        self.assertNotRegex(translated, r"(?m)^[ \t]*\\pdfoutput\s*=")

    def test_disables_microtype_expansion_when_loaded_after_preamble_block(self):
        source = (
            "\\documentclass{article}\n"
            "\\usepackage[protrusion=true,expansion=true]{microtype}\n"
            r"\begin{document}中文\end{document}"
        )

        translated = add_chinese_package(source)

        hook = r"\AddToHook{package/microtype/after}"
        self.assertIn(hook, translated)
        self.assertIn(r"\microtypesetup{expansion=false}", translated)
        self.assertIn(r"\DeclareMicrotypeSet[protrusion]{latextrans-unicode}{encoding={TU,EU1}}", translated)
        self.assertLess(
            translated.index(hook),
            translated.index(r"\usepackage[protrusion=true,expansion=true]{microtype}"),
        )

    @unittest.skipUnless(shutil.which("xelatex"), "XeLaTeX is unavailable")
    def test_xelatex_compiles_local_style_requesting_microtype_expansion(self):
        # arXiv 2611.16933: kezek.sty 以 expansion=true 加载 microtype，
        # XeTeX 在 \begin{document} 报 "Font expansion does not work with xetex"。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "paperstyle.sty").write_text(
                "\\RequirePackage[protrusion=true,expansion=true]{microtype}\n",
                encoding="utf-8",
            )
            source = (
                "\\documentclass{article}\n"
                "\\usepackage{paperstyle}\n"
                "\\begin{document}\n中文 text.\n\\end{document}\n"
            )
            (root / "main.tex").write_text(add_chinese_package(source), encoding="utf-8")

            completed = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "-no-pdf", "main.tex"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=300,
            )

            log = (root / "main.log").read_text(encoding="utf-8", errors="replace")
            self.assertEqual(completed.returncode, 0, log[-2000:])
            self.assertNotIn("Font expansion does not work with xetex", log)
            self.assertIn("Character protrusion enabled", log)

    @unittest.skipUnless(shutil.which("xelatex"), "XeLaTeX is unavailable")
    def test_xelatex_compiles_type1_fonts_loaded_from_local_style_or_class(self):
        # arXiv 2611.07071: a style selects Times and T1, then \textbullet
        # selects TS1/ptmr8c and microtype calls XeTeXglyph on a Type1 font.
        for loaded_by_class in (False, True):
            with self.subTest(loaded_by_class=loaded_by_class), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "legacy.sty").write_text(
                    "\\renewcommand{\\rmdefault}{ptm}\n"
                    "\\renewcommand{\\sfdefault}{phv}\n"
                    "\\renewcommand{\\ttdefault}{pcr}\n"
                    "\\RequirePackage[T1]{fontenc}\n"
                    "\\RequirePackage{amsmath}\n"
                    "\\RequirePackage[protrusion=true,expansion=true]{microtype}\n",
                    encoding="utf-8",
                )
                (root / "paper.cls").write_text(
                    "\\LoadClass{article}\n\\RequirePackage{legacy}\n", encoding="utf-8"
                )
                source = (
                    ("\\documentclass{paper}\n" if loaded_by_class else
                     "\\documentclass{article}\n\\usepackage{legacy}\n")
                    + "\\begin{document}\n\\begin{itemize}\n"
                    "\\item \\textbf{中文字体兼容测试} text. $x：y。$\n"
                    "ą \\textbf{ą} \\textsf{ą} \\texttt{ą}\n"
                    "$\\mathrm{Model}+\\mathbf{Model}+\\mathsf{Model}$\n"
                    "\\end{itemize}\n\\end{document}\n"
                )
                (root / "main.tex").write_text(add_chinese_package(source), encoding="utf-8")
                completed = subprocess.run(
                    ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                    cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    stdin=subprocess.DEVNULL, timeout=300,
                )
                log = (root / "main.log").read_text(encoding="utf-8", errors="replace")
                self.assertEqual(completed.returncode, 0, log[-2500:])
                self.assertNotIn("Cannot use XeTeXglyph", log)
                self.assertNotIn("Missing character:", log)
                self.assertNotRegex(log, r"Font shape `TU/(?:ptm|phv|pcr)/[^\n]+undefined")

    def test_removes_directive_even_when_ctex_already_exists(self):
        source = (
            r"\documentclass{article}"
            "\n\\usepackage{ctex}\n"
            "\\pdfoutput = 1\n"
            r"\begin{document}中文\end{document}"
        )

        translated = add_chinese_package(source)

        self.assertNotRegex(translated, r"(?m)^[ \t]*\\pdfoutput\s*=")

    @unittest.skipUnless(shutil.which("xelatex"), "XeLaTeX is unavailable")
    def test_native_font_choices_survive_legacy_font_compatibility_hook(self):
        source = (
            "\\documentclass{article}\n"
            "\\setmainfont{Latin Modern Roman}\n"
            "\\setsansfont{Latin Modern Sans}\n"
            "\\setmonofont{Latin Modern Mono}\n"
            "\\let\\savedmain\\rmdefault\n"
            "\\let\\savedsans\\sfdefault\n"
            "\\let\\savedmono\\ttdefault\n"
            "\\begin{document}\n"
            "\\ifx\\savedmain\\rmdefault\\else\\errmessage{Main font changed}\\fi\n"
            "\\ifx\\savedsans\\sfdefault\\else\\errmessage{Sans font changed}\\fi\n"
            "\\ifx\\savedmono\\ttdefault\\else\\errmessage{Mono font changed}\\fi\n"
            "中文 text.\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.tex").write_text(add_chinese_package(source), encoding="utf-8")
            completed = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=300,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout[-2500:])

    def test_normalizes_escaped_underscores_only_in_reference_arguments(self):
        translated = (
            r"\cref{tab:multi\_token} \label{fig:one\_two} "
            r"\eqref{eq:one\_two} \cite{paper\_id} "
            r"\hyperref[sec:one\_two]{link} text\_token "
            r"\href{https://example.test/a\_b}{link}"
        )

        result = normalize_latex_reference_arguments(translated)

        self.assertIn(r"\cref{tab:multi_token}", result)
        self.assertIn(r"\label{fig:one_two}", result)
        self.assertIn(r"\eqref{eq:one_two}", result)
        self.assertIn(r"\cite{paper_id}", result)
        self.assertIn(r"\hyperref[sec:one_two]{link}", result)
        self.assertIn(r"text\_token", result)
        self.assertIn(r"\href{https://example.test/a\_b}{link}", result)

    def test_strips_cross_caption_placeholders(self):
        source = r"\caption{Width analysis of the CoI.}"
        translated = (
            r"<PLACEHOLDER_CAP_1>\caption{CoI 的宽度分析。}"
            r"<PLACEHOLDER_CAP_2>"
        )

        result, unexpected = strip_unexpected_placeholders(source, translated)

        self.assertEqual(result, r"\caption{CoI 的宽度分析。}")
        self.assertEqual(
            unexpected,
            ["<PLACEHOLDER_CAP_1>", "<PLACEHOLDER_CAP_2>"],
        )

    def test_patches_pdftex_only_ligature_directive_in_local_class(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            class_file = Path(temp_dir, "gtech.cls")
            class_file.write_text(
                "\\RequirePackage{microtype}\n"
                "\\DisableLigatures[f]{family=sf*}\n"
                "% \\DisableLigatures should remain a comment\n",
                encoding="utf-8",
            )

            patched = patch_xelatex_compatibility_files(temp_dir)
            result = class_file.read_text(encoding="utf-8")

            self.assertEqual(patched, [str(class_file)])
            self.assertIn(
                "% LaTeXTrans: disabled for XeLaTeX: \\DisableLigatures",
                result,
            )
            self.assertNotRegex(result, r"(?m)^\\DisableLigatures")
            self.assertIn("% \\DisableLigatures should remain a comment", result)

    def test_constructor_rejects_residual_placeholders_in_included_tex(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            constructor = LatexConstructor(
                sections=[
                    {
                        "section": "-1",
                        "content": (
                            r"\begin{document}<PLACEHOLDER_file_begin>"
                            r"<PLACEHOLDER_CAP_99><PLACEHOLDER_file_end>"
                            r"\end{document}"
                        ),
                        "trans_content": (
                            r"\begin{document}<PLACEHOLDER_file_begin>"
                            r"<PLACEHOLDER_CAP_99><PLACEHOLDER_file_end>"
                            r"\end{document}"
                        ),
                    }
                ],
                captions=[],
                envs=[],
                inputs=[
                    {
                        "command": r"\input{file}",
                        "begin": "<PLACEHOLDER_file_begin>",
                        "end": "<PLACEHOLDER_file_end>",
                        "path": "file",
                    }
                ],
                newcommands=[],
                output_latex_dir=temp_dir,
            )

            with self.assertRaises(ValueError):
                constructor.construct()

            self.assertFalse(Path(temp_dir, "file.tex").exists())


if __name__ == "__main__":
    unittest.main()
