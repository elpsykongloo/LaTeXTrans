import re
import shutil
import subprocess

import pytest

from src.agents.tool_agents.translator_agent import TranslatorAgent
from src.agents.tool_agents.validator_agent import ValidatorAgent
from src.formats.latex.coverage import untranslated_prose
from src.formats.latex.layout import add_cjk_layout_support, inspect_layout_log, reflow_wrapping_floats
from src.formats.latex.parser import LatexParser
from src.formats.latex.utils import protect_latex_syntax, remove_comments
from src.utils.checkpoint import CheckpointStore, MAP_FILES, atomic_write_json


ENGLISH = "We study how the model uses the features in its representation and show that the original detector can fail after training."


def test_successful_compilation_still_reports_serious_layout_warnings(tmp_path):
    log = tmp_path / "main.log"
    assert inspect_layout_log(log)["status"] == "unavailable"
    log.write_text("Overfull \\hbox (187.15pt too wide) in paragraph at lines 122--124\n"
                   "Overfull \\hbox (0.15pt too wide) in paragraph at lines 2--3\n"
                   "Package wrapfig Warning: Stationary wrapfigure forced to float on input line 18.\n", encoding="utf-8")
    report = inspect_layout_log(log)
    assert report["status"] == "warnings"
    assert len(report["warnings"]) == 2


def test_body_before_first_heading_and_table_cells_are_translation_units(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}" + ENGLISH
        + r"\footnote{This explanation also needs translation.}\section{Method}"
        + r"\begin{table}\begin{tabular}{ll}First label & Second label\\\end{tabular}"
        + r"\caption{Results}\end{table}\end{document}", encoding="utf-8",
    )
    parser = LatexParser(str(tmp_path), str(tmp_path))
    assert parser.parse()
    body = next(part for part in parser.sections_json if part["section"] == "0")
    assert ENGLISH in body["content"]
    assert not body.get("trans_content")
    table = next(part for part in parser.envs_json if part["env_name"] == "table")
    assert table["need_trans"] and table["always_trans"]
    # Collection has no dependency on model credentials or network calls.
    agent = object.__new__(TranslatorAgent)
    units = agent._collect_translation_units(parser.sections_json, parser.envs_json, parser.captions_json)
    assert ("sec", parser.sections_json.index(body)) in units
    assert ("sec", 0) not in units


def test_validator_rejects_untranslated_body_among_translated_paragraphs():
    validator = ValidatorAgent({"source_language": "en", "target_language": "ch"})
    part = {"section": "0", "content": "Another paragraph.\n\n" + ENGLISH,
            "trans_content": "另一段。\n\n" + ENGLISH}
    report = validator._validate(part)
    assert "coverage_error" in report
    assert validator._validate({**part, "trans_content": "另一段。\n\n我们研究模型如何使用表征中的特征，并说明训练后原始检测器可能失效。"}) is None


def test_coverage_ignores_code_math_and_short_names():
    text = r"\begin{verbatim}" + ENGLISH + r"\end{verbatim}" + "\n\n" + r"$\text{" + ENGLISH + r"}$" + "\n\nDINOv2, Waterbirds, ImageNet."
    assert untranslated_prose(text, text, "en", "ch") == []
    assert untranslated_prose(ENGLISH, ENGLISH, "en", "en") == []


def test_short_untranslated_footnote_in_translated_paragraph_is_rejected():
    note = r"\footnote{Fine-tuning code and pre-trained models are available at \url{https://example.org/model}}"
    assert untranslated_prose("Model description." + note, "模型描述。" + note, "en", "ch")
    translated = r"模型描述。\footnote{微调代码和预训练模型见 \url{https://example.org/model}}"
    assert untranslated_prose("Model description." + note, translated, "en", "ch") == []
    code = r"\begin{verbatim}" + note + r"\end{verbatim}"
    assert untranslated_prose(code, code, "en", "ch") == []
    names = r"\footnote{Alexey Dosovitskiy Lucas Beyer Alexander Kolesnikov Dirk Weissenborn Xiaohua Zhai}"
    assert untranslated_prose(names, names, "en", "ch") == []


def test_layout_arguments_and_comment_line_endings_are_protected():
    text = remove_comments("\\begin{minipage}{.8\\linewidth}% explanation\n"
                           "\\begin{tcbraster}[raster columns=3]Title\\end{tcbraster}\\end{minipage}")
    protection = protect_latex_syntax(text)
    assert "explanation" not in text
    assert "%\n" in text
    assert "raster columns" not in protection.protected_text
    assert ".8" not in protection.protected_text
    result = protection.restore(protection.protected_text.replace("Title", "标题"))
    assert "标题" in result and "%\n" in result and "raster columns=3" in result


def test_final_input_comment_cannot_swallow_file_boundary(tmp_path):
    (tmp_path / "part.tex").write_text("Intro text.% trailing comment\n", encoding="utf-8")
    parser = LatexParser(str(tmp_path), str(tmp_path))
    merged = parser._merge_inputs(r"\input{part}\section{Next}More text.")
    cleaned = remove_comments(merged)
    assert "<PLACEHOLDER_part_end>" in cleaned
    assert r"\section{Next}More text." in cleaned


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="TeX not installed")
def test_comment_removal_preserves_typeset_macro_width(tmp_path):
    # Spaces after the macro body turn a tightly packed row into extra columns.
    source = r"""\documentclass{article}
\newcommand{\card}{%
\hbox to 10pt{x\hfil}%
}
\begin{document}
\setbox0=\hbox{\card\card\card}
\typeout{CARDWIDTH=\the\wd0}
\box0
\end{document}
"""
    (tmp_path / "main.tex").write_text(remove_comments(source), encoding="utf-8")
    run = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout[-2000:]
    assert float(re.search(r"CARDWIDTH=([\d.]+)pt", run.stdout)[1]) == 30.0


def test_wrapping_float_reflows_but_keeps_side_and_width():
    source = r"\begin{wrapfigure}[12]{r}{.41\linewidth}\vspace{-4mm}\includegraphics[width=\linewidth]{plot}\caption{图注}\vspace{-24mm}\end{wrapfigure}"
    result = reflow_wrapping_floats(source)
    assert r"\begin{wrapfigure}{R}{.41\linewidth}" in result
    assert r"\vspace" not in result
    assert r"\caption{图注}" in result
    macro = r"\newcommand{\example}{" + source + "}"
    assert reflow_wrapping_floats(macro) == macro


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX not installed")
def test_wrap_reserves_page_height_and_chinese_underline_can_break(tmp_path):
    source = r"""\documentclass{article}
\usepackage[textheight=8cm,textwidth=10cm]{geometry}
\usepackage{xeCJK,xcolor,wrapfig}
\usepackage{tcolorbox}\tcbuselibrary{raster}
\setCJKmainfont{FandolSong-Regular.otf}
\definecolor{primary}{rgb}{0.5,0,0}
\ExplSyntaxOn
\cs_new:Npn\fancyul_word:n#1{\mbox{#1}}
\cs_new:Npn\fancyul_space:{\space}
\NewDocumentCommand{\fancyul}{O{primary}O{white}m}{\fancyul_word:n{#3}}
\ExplSyntaxOff
\newenvironment{testbox}{\typeout{BOX-WIDTH=\the\linewidth}}{}
\begin{document}
\newsavebox{\cards}
\sbox{\cards}{\begin{minipage}{10cm}\begin{tcbraster}[raster columns=3]
\begin{tcolorbox}甲\end{tcolorbox}
\begin{tcolorbox}乙\end{tcolorbox}
\begin{tcolorbox}丙\end{tcolorbox}
\begin{tcolorbox}丁\end{tcolorbox}
\begin{tcolorbox}戊\end{tcolorbox}
\begin{tcolorbox}己\end{tcolorbox}
\end{tcbraster}\end{minipage}}
\typeout{CARD-HEIGHT=\the\dimexpr\ht\cards+\dp\cards\relax}
第一页正文。\par\vspace{6cm}
\section{图文标题}\label{wrap-heading}
\begin{wrapfigure}{r}{.4\linewidth}
\rule{\linewidth}{3cm}\caption{环绕图注}
\end{wrapfigure}
\typeout{WRAP-PAGE=\thepage}
\fancyul{这是一段很长的中文测试文字，需要自动在行内换行，不应把整段文字作为一个不可断行的盒子。数学表达式 $x+y$ 也应保持完整。这是继续测试自动换行的文字。}
\begin{testbox}全宽总结。\end{testbox}
\end{document}
"""
    (tmp_path / "main.tex").write_text(add_cjk_layout_support(reflow_wrapping_floats(source)), encoding="utf-8")
    run = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout[-2000:]
    assert "WRAP-PAGE=2" in run.stdout
    assert "Overfull" not in run.stdout
    assert float(re.search(r"BOX-WIDTH=([\d.]+)pt", run.stdout)[1]) > 280
    assert float(re.search(r"CARD-HEIGHT=([\d.]+)pt", run.stdout)[1]) < 100
    assert re.search(r"\\newlabel\{wrap-heading\}\{\{1\}\{2\}", (tmp_path / "main.aux").read_text(encoding="utf-8"))


def test_parser_upgrade_reuses_only_compatible_fragments(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    (source / "main.tex").write_text(r"\documentclass{article}\begin{document}Body\end{document}")
    config = {"source_language": "en", "target_language": "ch", "llm_config": {}}
    old = {"section": "1", "content": ENGLISH, "trans_content": "已有译文。"}
    table = {"placeholder": "<PLACEHOLDER_ENV_1>", "content": "Table text", "need_trans": False, "trans_content": "Table text"}
    for filename in MAP_FILES:
        atomic_write_json(output / filename, [old] if filename == "sections_map.json" else [table] if filename == "envs_map.json" else [])
    store = CheckpointStore(str(output), str(source), config)
    store.stage_completed("parse", snapshot=True)
    store.stage_completed("translate", snapshot=True)
    store.data.pop("parse_checks")
    store.save()
    resumed = CheckpointStore(str(output), str(source), config)
    assert resumed.needs_reparse and not resumed.restore_maps()
    previous = resumed.reusable_translation_maps()
    atomic_write_json(output / "sections_map.json", [{**old, "trans_content": ""}])
    atomic_write_json(output / "envs_map.json", [{**table, "need_trans": True, "trans_content": ""}])
    assert resumed.reuse_after_parse(previous) == 1
    assert resumed.read_fragment("sec", 0, old)["trans_content"] == "已有译文。"
    assert resumed.read_fragment("env", 0, {**table, "need_trans": True}) is None
    assert resumed.read_fragment("sec", 0, {**old, "content": "Different text."}) is None
