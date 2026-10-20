from unittest.mock import patch

import pytest

from src.formats.latex import parser as parser_module
from src.formats.latex.parser import LatexParser, count_tokens
from src.formats.latex.utils import protect_latex_syntax


# 论文 2610.19716 在表格和对话模板示例中字面包含这些 token。
_SPECIAL_TOKEN_TEXT = (
    r"\texttt{<|endoftext|>} & Sequence padding \\" "\n"
    r"<|im\_start|>system\\" "\n"
    "You are a helpful assistant.<|im_end|> <|fim_prefix|>"
)


def test_count_tokens_treats_special_tokens_as_plain_text():
    assert count_tokens(_SPECIAL_TOKEN_TEXT) > 0
    assert count_tokens("") == 0


def test_count_tokens_falls_back_when_tiktoken_unavailable():
    with patch.object(parser_module, "_get_token_encoder", return_value=None):
        assert count_tokens("a" * 40) == 10


def test_merge_short_sections_accepts_special_token_text():
    parser = LatexParser("", "")
    parser.sections_json = [
        {"section": "0", "content": "front", "trans_content": ""},
        {"section": "1", "content": "<|endoftext|>", "trans_content": ""},
        {"section": "2", "content": "<|im_end|>", "trans_content": ""},
        {"section": "3", "content": "word " * 200, "trans_content": ""},
    ]

    parser._merge_short_sections(min_tokens=50)

    assert [sec["section"] for sec in parser.sections_json] == ["0", "1+2+3"]
    assert "<|endoftext|>" in parser.sections_json[1]["content"]


@pytest.mark.parametrize("token", [
    "<|endoftext|>",
    r"<|im\_start|>",
    "<|im_end|>",
    "<|begin_of_text|>",
    r"<|vision\_pad|>",
])
def test_chat_template_tokens_are_never_sent_to_model(token):
    source = f"Use {token} to pad the sequence."
    protection = protect_latex_syntax(source)

    assert "<|" not in protection.protected_text
    assert protection.restore(protection.protected_text) == source
    translated = protection.protected_text.replace("Use", "使用").replace(
        "to pad the sequence.", "填充序列。"
    )
    assert token in protection.restore(translated)


def test_plain_comparison_bars_are_not_treated_as_chat_tokens():
    source = "the norm < |x| > holds"
    assert protect_latex_syntax(source).protected_text == source
