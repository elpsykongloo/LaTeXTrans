import gzip
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from src.formats.latex import utils


def test_legacy_arxiv_source_pdf_and_reuse_use_one_local_name(tmp_path):
    canonical = "quant-ph/9508027v2"
    local = "quant-ph_9508027v2"
    urls = []

    def download(url, destination, *args, **kwargs):
        urls.append(url)
        if "/src/" in url:
            with gzip.open(destination, "wb") as output:
                output.write(br"\documentclass{article}\begin{document}Source\end{document}")
        else:
            Path(destination).write_bytes(b"%PDF-1.5\nOriginal")

    with mock.patch.object(utils, "download_file_ranged", side_effect=download):
        source = utils.download_arxiv_source(canonical, str(tmp_path))
        assert Path(source).name == local
        assert Path(source).parent == tmp_path
        assert list(Path(source).glob("*.tex"))
        assert utils.is_already_downloaded(canonical, str(tmp_path))
        pdf = utils.download_arxiv_pdf(canonical, str(tmp_path))
        utils.place_arxiv_pdf(pdf, canonical, [source])
        assert Path(source, local + ".pdf").is_file()
        assert utils.download_arxiv_pdf(canonical, str(tmp_path)) == str(Path(source, local + ".pdf"))
    assert urls == [f"https://arxiv.org/src/{canonical}", f"https://arxiv.org/pdf/{canonical}.pdf"]
    assert utils.arxiv_project_name("1810.04805v2") == "1810.04805v2"
    assert utils.arxiv_project_name("astro-ph/9508027v2") != local


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX not installed")
def test_legacy_documentstyle_keeps_layout_options_and_loads_optional_packages(tmp_path):
    source = r"""\documentstyle[twoside,12pt,latexsym]{article}
\begin{document}中文数学 $\mho$.
\makeatletter\typeout{TWOSIDE=\if@twoside YES\else NO\fi}\makeatother
\end{document}"""
    result = utils.add_chinese_package(source)
    assert r"\documentclass[twoside,12pt]{article}" in result
    assert r"\usepackage{latexsym}" in result
    (tmp_path / "main.tex").write_text(result, encoding="utf-8")
    run = subprocess.run(["xelatex", "-halt-on-error", "-interaction=nonstopmode", "main.tex"], cwd=tmp_path, capture_output=True, timeout=30)
    log = (tmp_path / "main.log").read_text(encoding="utf-8", errors="replace")
    assert run.returncode == 0, log[-2000:]
    assert "TWOSIDE=YES" in log
    assert "Missing character" not in log


def test_legacy_and_lowercase_subjects_are_recognized():
    response = mock.Mock(text='<div class="subjects">Quantum Physics (quant-ph); Computational Physics (physics.comp-ph); Probability (math.PR)</div>')
    with mock.patch.object(utils.requests, "get", return_value=response):
        assert utils.fetch_arxiv_categories("quant-ph/9508027v2") == ["quant-ph", "physics.comp-ph", "math.PR"]
