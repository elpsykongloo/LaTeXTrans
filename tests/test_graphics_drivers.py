import shutil
import subprocess

import pytest

from src.formats.latex.utils import add_chinese_package, normalize_xetex_driver_options, normalize_xetex_graphics_dimensions, patch_xelatex_compatibility_files


def test_driver_options_preserve_other_options_comments_and_examples():
    example = r"\begin{verbatim}\usepackage[pdftex]{graphicx}\end{verbatim}"
    comment = "% \\usepackage[pdftex]{graphicx}\n"
    source = (comment + r"\usepackage[pdftex,draft]{graphicx,unrelated}" + "\n"
              + r"\RequirePackage[dvips,colorlinks=true,pdfborder={0,0,1}]{hyperref}" + "\n"
              + r"\PassOptionsToPackage{pdftex,table}{xcolor}" + example)
    result = normalize_xetex_driver_options(source)
    assert comment in result and example in result
    assert r"\usepackage[draft]{graphicx}" in result
    assert r"\usepackage[pdftex,draft]{unrelated}" in result
    assert r"\RequirePackage[colorlinks=true,pdfborder={0,0,1}]{hyperref}" in result
    assert r"\PassOptionsToPackage{table}{xcolor}" in result
    assert normalize_xetex_driver_options(result) == result


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX not installed")
def test_xetex_graphics_and_tikz_use_native_driver_including_local_style(tmp_path):
    (tmp_path / "legacy.sty").write_text(r"\RequirePackage[pdftex]{graphicx}", encoding="utf-8")
    patch_xelatex_compatibility_files(str(tmp_path))
    source = r"""\documentclass{article}
\usepackage{legacy}
\usepackage[pdftex]{graphicx}
\usepackage{tikz}
\begin{document}
中文图形\begin{tikzpicture}\draw[red] (0,0) rectangle (2,1);\end{tikzpicture}
\makeatletter\typeout{GRAPHICS-DRIVER=\Gin@driver}\typeout{PGF-DRIVER=\pgfsysdriver}\makeatother
\end{document}
"""
    (tmp_path / "main.tex").write_text(add_chinese_package(source), encoding="utf-8")
    run = subprocess.run(["xelatex", "-halt-on-error", "-interaction=nonstopmode", "main.tex"], cwd=tmp_path, capture_output=True, timeout=30)
    log = (tmp_path / "main.log").read_text(encoding="utf-8", errors="replace")
    assert run.returncode == 0, log[-3000:]
    assert "GRAPHICS-DRIVER=xetex.def" in log
    assert "PGF-DRIVER=pgfsys-xetex.def" in log


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX not installed")
@pytest.mark.parametrize("density,expected", [("", 361.354), (r"\pdfpxdimen=1in\divide\pdfpxdimen by 96", 271.016)])
def test_pixel_image_width_preserves_default_or_custom_density(tmp_path, density, expected):
    source = (r"\documentclass{article}\usepackage{graphicx}" + density
              + r"\begin{document}\setbox0=\hbox{\includegraphics[draft,width=360px]{example-image.pdf}}"
              + r"\typeout{IMAGEWIDTH=\the\wd0}\box0\end{document}")
    result = normalize_xetex_graphics_dimensions(source)
    assert normalize_xetex_graphics_dimensions(result) == result
    (tmp_path / "main.tex").write_text(result, encoding="utf-8")
    run = subprocess.run(["xelatex", "-halt-on-error", "-interaction=nonstopmode", "main.tex"], cwd=tmp_path, capture_output=True, timeout=30)
    log = (tmp_path / "main.log").read_text(encoding="utf-8", errors="replace")
    assert run.returncode == 0, log[-1500:]
    import re
    assert float(re.search(r"IMAGEWIDTH=([\d.]+)pt", log)[1]) == pytest.approx(expected, abs=0.02)


def test_pixel_text_urls_and_code_are_unchanged():
    source = (r"An image is 360px wide. \url{https://example.org/360px}"
              + "\n% \\includegraphics[width=360px]{image.pdf}\n"
              + r"\begin{verbatim}\includegraphics[width=360px]{image.pdf}\end{verbatim}")
    assert normalize_xetex_graphics_dimensions(source) == source
