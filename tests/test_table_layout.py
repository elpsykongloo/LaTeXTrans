import re
import shutil
import subprocess

import pytest

from src.formats.latex.layout import add_cjk_layout_support, fit_floating_tables


def test_only_outer_float_tabulars_are_fitted():
    author = r"\author{\begin{tabular}{c}Name\end{tabular}}"
    table = r"\begin{table*}\begin{tabular}{cc}A&\begin{tabular}{c}B\end{tabular}\\\end{tabular}\end{table*}"
    longtable = r"\begin{longtable}{cc}A&B\\\end{longtable}"
    source = r"\documentclass{article}" + author + r"\begin{document}" + table + longtable + r"\end{document}"
    result = fit_floating_tables(source)
    assert author in result and longtable in result
    assert result.count(r"\begin{latextranstable}") == 1
    assert fit_floating_tables(result) == result


def test_existing_table_sizing_and_savebox_groups_are_preserved():
    resized = r"\resizebox{\linewidth}{!}{\begin{tabular}{cc}A&B\\\end{tabular}}"
    scaled = r"\scalebox{.8}[1]{\begin{tabular}{cc}A&B\\\end{tabular}}"
    adjusted = r"\begin{adjustbox}{max width=\linewidth}\begin{tabular}{cc}A&B\\\end{tabular}\end{adjustbox}"
    source = (r"\documentclass{article}\begin{document}\begin{table}" + resized + scaled + adjusted
              + r"\begin{tabular}{ll}C&D\\\end{tabular}\end{table}"
              + r"\begin{wrapfigure}{R}{.3\linewidth}Figure\end{wrapfigure}Text.\end{document}")
    result = add_cjk_layout_support(fit_floating_tables(source))
    assert resized in result and scaled in result and adjusted in result
    assert result.count(r"\begin{latextranstable}") == 1
    assert "env/lrbox/before" not in result


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX not installed")
def test_wide_table_fits_current_column_without_enlarging_small_table(tmp_path):
    source = r"""\documentclass[twocolumn]{article}
\usepackage{xeCJK}\setCJKmainfont{FandolSong-Regular.otf}
\begin{document}
\begin{table}
\centering
\begin{tabular}{llll}
训练步数 & 参数数量与训练配置 & 开发集上的困惑度 & 评估结果与运行时间\\
100K & Large & 4.3 & 100\\
\end{tabular}
\end{table}
\begin{table}\begin{tabular}{ll}A&B\\1&2\\\end{tabular}\end{table}
\begin{table}\resizebox{\linewidth}{!}{\begin{tabular}{ll}Already sized&A\\1&2\\\end{tabular}}\end{table}
Text.\end{document}
"""
    (tmp_path / "main.tex").write_text(fit_floating_tables(source), encoding="utf-8")
    run = subprocess.run(["xelatex", "-halt-on-error", "-interaction=nonstopmode", "main.tex"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout[-3000:]
    assert "Overfull" not in run.stdout
    sizes = re.findall(r"scaled table from ([\d.]+)pt to ([\d.]+)pt", run.stdout)
    assert len(sizes) == 1  # the small table's original font size is retained
    assert float(sizes[0][0]) > float(sizes[0][1])
    assert float(sizes[0][1]) < 250  # one column, not the whole page
