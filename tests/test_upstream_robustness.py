from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.tool_agents.parser_agent import ParserAgent
from src.formats.latex.parser import LatexParser
from src.formats.latex.reconstruct import (
    LatexConstructor,
    _recover_caption_trans,
    _strip_md_wrap,
)
from src.formats.latex.utils import (
    LATEX_PROSE_ENVIRONMENTS,
    MAIN_TEX_MARKER,
    add_target_language_package,
    find_latex_bracket_errors,
    find_main_tex_file,
    get_captionof_pattern,
    normalize_latex_reference_arguments,
    patch_xelatex_compatibility_files,
    protect_latex_syntax,
    remove_comments,
    validate_latex_structure,
)


def _constructor(parser, output_dir):
    return LatexConstructor(
        sections=parser.sections_json,
        captions=parser.captions_json,
        envs=parser.envs_json,
        inputs=parser.inputs_json,
        newcommands=parser.newcommands_json,
        output_latex_dir=str(output_dir),
        target_language="English",
    )


@pytest.mark.parametrize("environment", [
    "verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim",
    "LVerbatim", "SaveVerbatim", "lstlisting", "minted",
])
def test_code_is_frozen_before_inputs_sections_captions_and_macros(tmp_path, environment):
    code = (
        f"\\begin{{{environment}}}\n"
        "100% literal percent\n\n\n\n"
        "\\input{must_not_be_loaded}\n"
        "\\section{Code example}\n\\caption{Code caption}\n"
        "\\newcommand{\\example}{Code macro}\n"
        "\\begin{comment}literal sample\\end{comment}\n"
        "\\item not a real list item\n"
        f"\\end{{{environment}}}"
    )
    source = (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\section{Real section}\nBefore.\n" + code
        + "\nAfter.\n\\end{document}"
    )
    main = tmp_path / "main.tex"
    main.write_text(source, encoding="utf-8")
    parser = LatexParser(str(tmp_path), str(tmp_path))

    assert parser.parse() is True
    assert parser.inputs_json == []
    assert parser.captions_json == []
    assert len(parser.sections_json) == 3
    assert len(parser.newcommands_json) == 1
    assert parser.newcommands_json[0]["kind"] == "opaque"
    for section in parser.sections_json:
        section["trans_content"] = section["content"].replace("Before.", "前文。")
    _constructor(parser, tmp_path).construct()
    restored = main.read_text(encoding="utf-8")
    assert "前文。" in restored
    assert "100% literal percent\n\n\n\n" in restored
    assert r"\input{must_not_be_loaded}" in restored
    assert r"\caption{Code caption}" in restored
    assert r"\begin{comment}literal sample\end{comment}" in restored
    assert "<PLACEHOLDER_" not in restored


def test_code_nested_in_translatable_list_restores_after_caption(tmp_path):
    source = (
        r"\documentclass{article}\begin{document}\section{Methods}"
        r"\begin{itemize}\item Explanation. "
        r"\caption{Snippet: \verb|raw { % \section{fake}|}"
        r"\begin{Verbatim}raw % \item \end{bad}\end{Verbatim}"
        r"\end{itemize}\end{document}"
    )
    (tmp_path / "main.tex").write_text(source, encoding="utf-8")
    parser = LatexParser(str(tmp_path), str(tmp_path))
    assert parser.parse()
    assert parser.envs_json[0]["need_trans"] is True
    for section in parser.sections_json:
        section["trans_content"] = section["content"]
    for env in parser.envs_json:
        env["trans_content"] = env["content"].replace("Explanation.", "解释。")
    for caption in parser.captions_json:
        caption["trans_content"] = caption["content"].replace("Snippet:", "代码：")

    _constructor(parser, tmp_path).construct()
    restored = (tmp_path / "main.tex").read_text(encoding="utf-8")
    assert "解释。" in restored
    assert r"\caption{代码： \verb|raw { % \section{fake}|}" in restored
    assert r"\begin{Verbatim}raw % \item \end{bad}\end{Verbatim}" in restored
    assert "<PLACEHOLDER_" not in restored


def test_code_from_windows_source_keeps_single_line_breaks(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text(
        "\\documentclass{article}\r\n\\begin{document}\r\n"
        "\\section{Intro}\r\n\\begin{Verbatim}\r\nfirst\r\nsecond\r\n"
        "\\end{Verbatim}\r\n\\end{document}",
        encoding="utf-8", newline="",
    )
    parser = LatexParser(str(tmp_path), str(tmp_path))
    assert parser.parse()
    for section in parser.sections_json:
        section["trans_content"] = section["content"]
    _constructor(parser, tmp_path).construct()
    assert "\\begin{Verbatim}\nfirst\nsecond\n\\end{Verbatim}" in main.read_text(encoding="utf-8")


@pytest.mark.parametrize("code", [
    r"\verb|raw % { \item \begin{bad}|",
    r"\verb*|raw % } \end{bad}|",
    r"\lstinline[language=Python]|print('100%') { \item|",
    r"\lstinline{print('100%') \begin{bad}}",
    "\\begin{Verbatim}\nraw % { \\item \\begin{bad}\n\\end{Verbatim}",
])
def test_inline_and_environment_code_is_atomic_in_translation(code):
    source = "Intro " + code + " ending."
    protection = protect_latex_syntax(source)
    assert "raw" not in protection.protected_text
    assert "print" not in protection.protected_text
    assert "Intro" in protection.protected_text
    assert protection.restore(protection.protected_text.replace("Intro", "引言")) == (
        "引言 " + code + " ending."
    )
    assert validate_latex_structure(source) == []
    assert find_latex_bracket_errors(source) == []


def test_comment_removal_keeps_code_samples_and_distinguishes_escaped_percent():
    source = (
        "% \\begin{Verbatim} commented sample\n"
        "Real. % remove this\n"
        "\\begin{Verbatim}\n\\begin{comment}sample\\end{comment}\n"
        "100% keep this\n\\end{Verbatim}\n"
        "\\url{https://example.test/%7Euser}\n"
        "escaped \\% stays, double slash \\\\% remove tail\n"
        "\\begin{comment}remove block\\end{comment}\nTail."
    )
    cleaned = remove_comments(source)
    assert "commented sample" not in cleaned
    assert "remove this" not in cleaned
    assert "remove block" not in cleaned
    assert "remove tail" not in cleaned
    assert "100% keep this" in cleaned
    assert r"\begin{comment}sample\end{comment}" in cleaned
    assert r"\url{https://example.test/%7Euser}" in cleaned
    assert r"escaped \% stays" in cleaned
    assert "Tail." in cleaned


@pytest.mark.parametrize("environment", sorted(LATEX_PROSE_ENVIRONMENTS))
def test_prose_environments_translate_with_nested_caption(environment):
    parser = LatexParser("", "")
    source = rf"\begin{{{environment}}}Text. \keywords{{Key}}\end{{{environment}}}"
    parser._extract_envs(parser._extract_captions(source))
    assert len(parser.envs_json) == 1
    assert parser.envs_json[0]["need_trans"] is True
    assert "<PLACEHOLDER_CAP_" in parser.envs_json[0]["content"]


def test_parser_agent_does_not_send_prose_lists_to_need_trans_judge(tmp_path):
    parser = LatexParser("", "")
    parser.envs_json = [
        {"env_name": name, "need_trans": True, "placeholder": f"ENV_{index}"}
        for index, name in enumerate(sorted(LATEX_PROSE_ENVIRONMENTS) + ["theorem"])
    ]
    config = {
        "source_language": "English", "target_language": "Chinese",
        "llm_config": {"model": "test-model", "api_key": "", "base_url": "https://example.test/v1"},
    }
    with patch("src.formats.latex.parser.LatexParser", return_value=parser), \
         patch.object(parser, "parse", return_value=True):
        agent = ParserAgent(config, project_dir=str(tmp_path), output_dir=str(tmp_path))
        agent.execute()
    for env in parser.envs_json:
        assert bool(env.get("need_trans_pending")) == (env["env_name"] == "theorem")


@pytest.mark.parametrize("suffix", ["supp", "supplementary", "appendix", "rebuttal", "response", "paper_si"])
def test_main_tex_avoids_supplementary_material(tmp_path, suffix):
    source = r"\documentclass{article}\begin{document}Text.\end{document}"
    (tmp_path / "experiment.tex").write_text(source, encoding="utf-8")
    (tmp_path / f"{suffix}.tex").write_text(
        source.replace("Text.", r"\title{Long supplement}" + "More." * 100), encoding="utf-8"
    )
    assert Path(find_main_tex_file(tmp_path)).name == "experiment.tex"


def test_main_tex_prefers_root_over_long_nested_copy_but_honors_metadata(tmp_path):
    source = r"\documentclass{article}\begin{document}Text.\end{document}"
    root = tmp_path / "root.tex"
    root.write_text(source, encoding="utf-8")
    duplicate = tmp_path / "old_source" / "main.tex"
    duplicate.parent.mkdir()
    duplicate.write_text(source.replace("Text.", r"\section{Many}" * 100), encoding="utf-8")
    assert Path(find_main_tex_file(tmp_path)) == root
    (tmp_path / "00README.json").write_text(
        '{"sources": [{"usage": "toplevel", "filename": "old_source/main.tex"}]}', encoding="utf-8"
    )
    assert Path(find_main_tex_file(tmp_path)) == duplicate
    (tmp_path / MAIN_TEX_MARKER).write_text("root.tex", encoding="utf-8")
    assert Path(find_main_tex_file(tmp_path)) == root


def test_main_tex_rejects_standalone_and_code_documentclass_examples(tmp_path):
    (tmp_path / "figure.tex").write_text(
        r"\documentclass{standalone}\begin{document}Image.\end{document}", encoding="utf-8"
    )
    (tmp_path / "example.tex").write_text(
        r"\begin{Verbatim}\documentclass{article}\begin{document}fake\end{document}\end{Verbatim}",
        encoding="utf-8",
    )
    nested = tmp_path / "src" / "paper.tex"
    nested.parent.mkdir()
    nested.write_text(r"\documentclass{article}\begin{document}Paper.\end{document}", encoding="utf-8")
    assert Path(find_main_tex_file(tmp_path)) == nested


@pytest.mark.parametrize("source", [
    r"\captionof{figure}{Different text with \textbf{nested} and \{literal\}}",
    r"\captionof*{table}[Short title]{Long \emph{caption}}",
])
def test_captionof_two_distinct_arguments_are_extracted_and_type_is_protected(source):
    match = get_captionof_pattern().fullmatch(source)
    assert match is not None
    parser = LatexParser("", "")
    result = parser._extract_captions(source)
    assert result == parser.captions_json[0]["placeholder"]
    assert parser.captions_json[0]["content"] == source
    protection = protect_latex_syntax(source, protect_group_delimiters=True)
    assert "figure" not in protection.protected_text
    assert "table" not in protection.protected_text
    assert "text" in protection.protected_text or "Long" in protection.protected_text


def test_author_thanks_and_visible_commands_in_macros_translate_without_names(tmp_path):
    source = (
        "\\documentclass{article}\n"
        "\\newcommand{\\displaytitle}[1][Default]{\\title{Paper title #1}}\n"
        "\\def\\ack#1{\\author{Alice\\thanks{Thanks #1 and \\textbf{support}.}}}\n"
        "\\newenvironment{example}[1][Default]{\\caption{Begin title #1}\\begin{quote}}"
        "{\\end{quote}\\caption{End title}}\n"
        "\\author{Bob\\thanks{Equal contribution.} and Carol\\thanks{Support.}}\n"
        "\\begin{document}\\section{Intro}Text.\\end{document}"
    )
    main = tmp_path / "main.tex"
    main.write_text(source, encoding="utf-8")
    parser = LatexParser(str(tmp_path), str(tmp_path))
    assert parser.parse()
    assert len(parser.newcommands_json) == 3
    assert len(parser.captions_json) == 6
    for section in parser.sections_json:
        section["trans_content"] = section["content"]
    for caption in parser.captions_json:
        caption["trans_content"] = caption["content"].replace(" title", " 标题").replace("Thanks", "致谢").replace(
            "Equal contribution.", "贡献相同。"
        )
    _constructor(parser, tmp_path).construct()
    restored = main.read_text(encoding="utf-8")
    assert r"\newcommand{\displaytitle}[1][Default]{\title{Paper 标题 #1}}" in restored
    assert r"\def\ack#1{\author{Alice\thanks{致谢 #1 and \textbf{support}.}}}" in restored
    assert r"\newenvironment{example}[1][Default]" in restored
    assert r"{\end{quote}\caption{End 标题}}" in restored
    assert r"\author{Bob\thanks{贡献相同。} and Carol\thanks{Support.}}" in restored
    assert "<PLACEHOLDER_" not in restored


@pytest.mark.parametrize("wrapped", [
    "`\\title{中文标题}`", "```latex\n\\title{中文标题}\n```",
])
def test_reconstruction_strips_full_markdown_wrappers(wrapped):
    assert _strip_md_wrap(wrapped) == r"\title{中文标题}"
    assert _strip_md_wrap("Text `literal` quotes.") == "Text `literal` quotes."
    assert _strip_md_wrap("``TeX quotation''") == "``TeX quotation''"


def test_caption_recovery_handles_nested_groups_empty_mentions_and_trailing_noise():
    source = r"\caption[Short]{Original.}"
    translated = (
        "Use \\caption{} for captions.\n```latex\n"
        "\\caption[短标题]{译文 \\textbf{嵌套} 和 \\{花括号\\}。}\n```\nExplanation."
    )
    assert _recover_caption_trans(source, translated) == (
        r"\caption[短标题]{译文 \textbf{嵌套} 和 \{花括号\}。}"
    )
    assert _recover_caption_trans(source, r"\caption{译文。} Explanation.") == r"\caption{译文。}"


def test_title_translates_normally_but_invalid_command_falls_back_to_source():
    source = r"\title{Original title}"
    assert _recover_caption_trans(source, r"\title{译文标题}") == r"\title{译文标题}"
    assert _recover_caption_trans(source, "Per rule, preserve the title.") == source
    assert _recover_caption_trans(source, r"\title{unfinished") == source
    assert _recover_caption_trans(r"\caption*{Original}", r"\caption{wrong form}") == r"\caption*{Original}"
    captionof = r"\captionof{figure}{Original}"
    assert _recover_caption_trans(captionof, r"\captionof{图}{译文}") == captionof


@pytest.mark.parametrize("language", ["Chinese", "Japanese"])
def test_package_and_reference_repairs_preserve_code_examples(language):
    code = (
        "\\begin{Verbatim}\n"
        "\\usepackage{luatexja}\n\\usepackage[utf8]{inputenc}\n"
        "\\pdfoutput=1\n\\ref{literal\\_label}\n\\end{Verbatim}"
    )
    source = (
        "\\documentclass{article}\n\\usepackage[utf8]{inputenc}\n"
        "\\pdfoutput=1\n\\begin{document}\n"
        "\\ref{real\\_label}\n" + code + "\n\\end{document}"
    )
    repaired = normalize_latex_reference_arguments(add_target_language_package(source, language))
    assert r"\usepackage{xeCJK}" in repaired
    assert r"\ref{real_label}" in repaired
    assert r"\ref{literal\_label}" in repaired
    assert repaired.count(r"\pdfoutput=1") == 1
    assert repaired.count(r"\usepackage[utf8]{inputenc}") == 1
    assert r"\usepackage{luatexja}" in repaired


def test_class_compatibility_patch_keeps_verbatim_directive_samples(tmp_path):
    class_file = tmp_path / "example.cls"
    class_file.write_text(
        "\\DisableLigatures[f]{family=sf*}\n"
        "\\begin{Verbatim}\n\\DisableLigatures[f]{family=sample}\n\\end{Verbatim}\n",
        encoding="utf-8",
    )
    assert patch_xelatex_compatibility_files(str(tmp_path)) == [str(class_file)]
    result = class_file.read_text(encoding="utf-8")
    assert "% LaTeXTrans: disabled for XeLaTeX:" in result
    assert "\\begin{Verbatim}\n\\DisableLigatures[f]{family=sample}" in result


def test_fragment_graph_rejects_cycles_before_generating_files(tmp_path):
    parser = LatexParser("", "")
    parser.sections_json = [{"section": "1", "content": "<PLACEHOLDER_ENV_1>", "trans_content": "<PLACEHOLDER_ENV_1>"}]
    parser.envs_json = [{
        "placeholder": "<PLACEHOLDER_ENV_1>", "content": "<PLACEHOLDER_ENV_1>",
        "trans_content": "<PLACEHOLDER_ENV_1>", "need_trans": True,
    }]
    with pytest.raises(ValueError, match="Cyclic"):
        _constructor(parser, tmp_path).construct()
    assert not list(tmp_path.glob("*.tex"))
