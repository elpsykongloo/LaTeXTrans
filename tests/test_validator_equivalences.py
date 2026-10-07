import pytest

from src.agents.tool_agents.validator_agent import ValidatorAgent


def _command_error(source, translation, **config):
    validator = ValidatorAgent(config, project_dir="paper")
    return validator._validate_command({"content": source, "trans_content": translation})


@pytest.mark.parametrize(("source", "translation"), [
    (r"Between A\sim B.", "在 A～B 之间。"),
    (r"Between A$\sim$B.", "在 A〜B 之间。"),
    (r"Between A\(\sim\)B.", "在 A∼B 之间。"),
    (r"Between A\ensuremath{\sim}B.", "在 A～B 之间。"),
    (r"More\ldots later.", "稍后…继续。"),
    (r"More\dots later.", "稍后……继续。"),
    (r"More$\ldots$ later.", "稍后…继续。"),
    (r"More\(\ldots\) later.", "稍后…继续。"),
    (r"\emph{More\textellipsis later.}", r"\emph{稍后…继续。}"),
])
def test_allows_equivalent_symbols_in_prose(source, translation):
    assert _command_error(source, translation) is None


@pytest.mark.parametrize(("source", "translation"), [
    (r"Relation $x\sim y$.", "关系 $x～y$。"),
    (r"List $a_1,\ldots,a_n$.", "列表 $a_1,…,a_n$。"),
    (r"\[\ldots\]", r"\[…\]"),
    (r"\begin{align}x\sim y\end{align}", r"\begin{align}x～y\end{align}"),
    (r"The expression \ensuremath{x\sim y}.", r"表达式 \ensuremath{x～y}。"),
    (r"\newcommand{\range}{\sim}", r"\newcommand{\range}{～}"),
    (r"Wait\ldots", "请稍等。"),
    (r"Wait\ldots", "请稍等………"),
    (r"First\ldots second\ldots", "第一…第二。"),
    (r"Value \(x\).", "数值 x。"),
    (r"Value $x$.", "数值 $x。"),
])
def test_equivalences_do_not_hide_math_or_symbol_loss(source, translation):
    assert _command_error(source, translation) is not None


def test_strict_mode_preserves_command_comparison():
    assert _command_error(r"Wait\ldots", "请稍等…", validation_prose_equivalences=False)


def test_comments_do_not_create_math_delimiter_false_positives():
    source = "Text. % \\( commented example\n$x$"
    translation = "正文。 % 注释中的 \\)\n$x$"
    assert _command_error(source, translation) is None


def test_escaped_dollars_and_linebreak_options_are_not_math_delimiters():
    source = r"Cost \$5.\\[2pt] Value $x$."
    translation = r"费用 \$5。\\[2pt] 数值 $x$。"
    assert _command_error(source, translation) is None


def test_math_delimiters_in_verbatim_are_ignored():
    code = r"\begin{verbatim}\( $\end{verbatim}"
    assert ValidatorAgent.extract_math_delimiters(code) == []


def test_cache_separates_equivalence_settings_for_same_project(tmp_path):
    normal = ValidatorAgent({}, project_dir="paper", output_dir=str(tmp_path))
    strict = ValidatorAgent({"validation_prose_equivalences": False}, project_dir="paper", output_dir=str(tmp_path))
    part = {"section": "1", "content": r"Wait\ldots", "trans_content": "稍等…"}
    assert normal._validate(part) is None
    assert strict._validate(part)["command_error"]
    ValidatorAgent.release_cache(str(tmp_path))
