import shutil
import subprocess

import pytest

from src.formats.latex.layout import suppress_build_date


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdfLaTeX not installed")
@pytest.mark.parametrize("date,expected", [("", ""), (r"\date{\today}", ""), (r"\date{November 10, 2017}", "November 10, 2017")])
def test_rebuild_date_is_suppressed_but_authored_date_survives(tmp_path, date, expected):
    source = (r"\documentclass{article}\title{Test}\author{Author}" + date
              + r"\begin{document}\makeatletter\typeout{PAPERDATE=\@date}\makeatother\maketitle Text.\end{document}")
    result = suppress_build_date(source)
    assert suppress_build_date(result) == result
    (tmp_path / "main.tex").write_text(result, encoding="utf-8")
    run = subprocess.run(["pdflatex", "-halt-on-error", "-interaction=nonstopmode", "main.tex"], cwd=tmp_path, capture_output=True, timeout=30)
    log = (tmp_path / "main.log").read_text(encoding="utf-8", errors="replace")
    assert run.returncode == 0, log[-1500:]
    assert f"PAPERDATE={expected}\n" in log
