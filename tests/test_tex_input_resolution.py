import tempfile
import unittest
from pathlib import Path

from src.formats.latex.parser import LatexParser
from src.formats.latex.utils import (
    add_chinese_package,
    find_main_tex_file,
    merge_tex_from_inputs,
    read_tex_file,
    resolve_tex_input_file,
)


class TexInputResolutionTests(unittest.TestCase):
    def test_directory_name_falls_back_to_tex_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "sections" / "results" / "generator").mkdir(
                parents=True
            )
            generator_file = project_dir / "sections" / "results" / "generator.tex"
            generator_file.write_text("GENERATOR_CONTENT", encoding="utf-8")

            resolved = resolve_tex_input_file(
                project_dir, "sections/results/generator"
            )

            self.assertEqual(Path(resolved), generator_file)

    def test_parser_merges_input_when_exact_path_is_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "sections" / "results" / "generator").mkdir(
                parents=True
            )
            (project_dir / "sections" / "results" / "generator.tex").write_text(
                "GENERATOR_CONTENT", encoding="utf-8"
            )

            parser = LatexParser(str(project_dir), str(project_dir / "output"))
            merged = parser._merge_inputs(
                r"\documentclass{article}\begin{document}"
                r"\input{sections/results/generator}\end{document}"
            )

            self.assertIn("GENERATOR_CONTENT", merged)
            self.assertEqual(len(parser.inputs_json), 1)

    def test_main_file_and_legacy_merge_require_regular_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "00README.json").write_text(
                '{"sources": [{"usage": "toplevel", "filename": "main.tex"}]}',
                encoding="utf-8",
            )
            (project_dir / "main.tex").write_text(
                r"\documentclass{article}\input{sections/results/generator}",
                encoding="utf-8",
            )
            (project_dir / "sections" / "results" / "generator").mkdir(
                parents=True
            )
            (project_dir / "sections" / "results" / "generator.tex").write_text(
                "GENERATOR_CONTENT", encoding="utf-8"
            )

            main_file = find_main_tex_file(project_dir)
            merged = merge_tex_from_inputs(main_file)

            self.assertEqual(Path(main_file), project_dir / "main.tex")
            self.assertIn("GENERATOR_CONTENT", merged)

            with self.assertRaises(IsADirectoryError):
                read_tex_file(project_dir / "sections" / "results" / "generator")

    def test_nvidia_report_uses_native_fonts_and_disables_microtype(self):
        source = (
            r"\documentclass[10pt]{nvidiatechreport}"
            r"\begin{document}中文\end{document}"
        )

        result = add_chinese_package(source)

        self.assertIn(r"\microtypesetup{activate=false}", result)
        self.assertIn(r"Path = NVIDIA-Sans-Font-TTF/", result)
        self.assertIn(r"Extension = .ttf", result)
        self.assertIn(r"UprightFont = NVIDIASans_Rg", result)
        self.assertNotIn("SimSun", result)
        self.assertNotIn("SimHei", result)


if __name__ == "__main__":
    unittest.main()
