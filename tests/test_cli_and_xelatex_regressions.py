import contextlib
import io
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

    def test_removes_directive_even_when_ctex_already_exists(self):
        source = (
            r"\documentclass{article}"
            "\n\\usepackage{ctex}\n"
            "\\pdfoutput = 1\n"
            r"\begin{document}中文\end{document}"
        )

        translated = add_chinese_package(source)

        self.assertNotRegex(translated, r"(?m)^[ \t]*\\pdfoutput\s*=")

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
