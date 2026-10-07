r"""超长翻译片段的二次切分。

``split_latex_paragraph_chunks`` 只在空行和 ``\item`` 前切分。没有空行的
大环境（例如包含上百个 ``\ensuremath`` 的 align/表格/长段推理面板）会被
整体发给模型，模型容易漏掉命令。这里在其结果之上，对仍超过
``max_chars`` 的片段继续在安全边界处切分：

- ``\\`` 行尾（含可选的 ``*`` 与 ``[..]`` 参数）；
- ``\hline``/``\midrule`` 等表格横线命令之后；
- 花括号深度为 0 的换行（优先选择句末、空行或结构命令开头的行）。

切分点永远不会落在 ``$...$``、``$$...$$``、``\(...\)``、``\[...\]``、
``{...}``、``\verb`` 参数或注释内部，因此每段都能独立做结构保护
（标记顺序校验以片段为单位进行），并保证 ``"".join(chunks) == text``。
"""
import re
from typing import List, Optional, Sequence, Tuple

from src.formats.latex.utils import (
    LATEX_PROTECTED_TOKEN_PATTERN,
    split_latex_paragraph_chunks,
)

_COMMAND_PATTERN = re.compile(r"\\([A-Za-z@]+)\*?")
_ENV_NAME_PATTERN = re.compile(r"[ \t]*\{([^{}\n]*)\}")
_RULE_COMMANDS = frozenset(
    {
        "hline", "midrule", "toprule", "bottomrule", "cmidrule", "cline",
        "specialrule", "addlinespace", "morecmidrules",
    }
)
_DELIMITED_VERBATIM_COMMANDS = frozenset({"verb", "lstinline"})
# 上一行以句末标点结束时，在其后换行处切分不会把句子拆开。
_SENTENCE_END = re.compile(r"""[.!?;:。！？；：]['"’”)\]}]*[ \t]*$""")
# 下一行以结构命令开头时，切分点同样不会落在句子中间。
_STRUCTURAL_LINE_START = re.compile(
    r"[ \t]*(?:\\(?:begin|end|item|hline|midrule|toprule|bottomrule|cmidrule|cline"
    r"|section|subsection|subsubsection|paragraph|caption|label|centering"
    r"|vspace|hspace|noindent|par)(?![A-Za-z])|<PLACEHOLDER_)"
)
_TRANSLATABLE_WORD = re.compile(r"[^\W\d_]{3,}")
_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")

STRONG = 0
WEAK = 1


def _skip_inline_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _boundary_after(text: str, index: int) -> int:
    """边界放在行尾空白及一个换行之后，使下一段从新行开始。"""
    index = _skip_inline_spaces(text, index)
    if index < len(text) and text[index] == "\n":
        index += 1
    return index


def _skip_same_line_group(text: str, index: int, opening: str, closing: str) -> int:
    r"""跳过同一行内的 ``(..)``/``[..]``/``{..}`` 参数（如 ``\cmidrule(lr){2-3}``）。"""
    probe = _skip_inline_spaces(text, index)
    if probe >= len(text) or text[probe] != opening:
        return index
    close = text.find(closing, probe + 1)
    if close == -1 or "\n" in text[probe:close]:
        return index
    return close + 1


def scan_split_candidates(text: str) -> List[Tuple[int, int, int]]:
    """返回 ``(位置, 环境嵌套深度, 强弱)`` 形式的安全切分点。"""
    candidates: List[Tuple[int, int, int]] = []
    length = len(text)
    brace = 0
    math: Optional[str] = None
    env_depth = 0
    index = 0

    def add(position: int, weakness: int) -> None:
        if 0 < position < length:
            candidates.append((position, env_depth, weakness))

    while index < length:
        char = text[index]

        if char == "%":
            # 注释内容不参与括号/数学状态统计；换行本身仍可作为边界。
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue

        if char == "\\":
            if text.startswith("\\\\", index):
                end = index + 2
                if end < length and text[end] == "*":
                    end += 1
                end = _skip_same_line_group(text, end, "[", "]")
                if brace == 0 and math is None:
                    boundary = _boundary_after(text, end)
                    add(boundary, STRONG)
                    index = max(end, boundary)
                    continue
                index = end
                continue
            if text.startswith("\\(", index) or text.startswith("\\[", index):
                if math is None:
                    math = "\\)" if text[index + 1] == "(" else "\\]"
                index += 2
                continue
            if text.startswith("\\)", index) or text.startswith("\\]", index):
                if math == text[index:index + 2]:
                    math = None
                index += 2
                continue
            command = _COMMAND_PATTERN.match(text, index)
            if command is None:
                index += 2  # 转义字符，如 \$、\{、\%
                continue
            name = command.group(1)
            end = command.end()
            if name in ("begin", "end"):
                env_name = _ENV_NAME_PATTERN.match(text, end)
                if env_name:
                    if name == "begin":
                        env_depth += 1
                    else:
                        env_depth = max(0, env_depth - 1)
                    end = env_name.end()
                index = end
                continue
            if name in _DELIMITED_VERBATIM_COMMANDS and end < length:
                delimiter = text[end]
                if not delimiter.isspace() and delimiter not in "{[*":
                    close = text.find(delimiter, end + 1)
                    index = length if close == -1 else close + 1
                    continue
            if name in _RULE_COMMANDS and brace == 0 and math is None:
                for opening, closing in (("[", "]"), ("(", ")"), ("{", "}")):
                    end = _skip_same_line_group(text, end, opening, closing)
                boundary = _boundary_after(text, end)
                add(boundary, STRONG)
                index = max(end, boundary)
                continue
            index = end
            continue

        if char == "$":
            if text.startswith("$$", index):
                if math is None:
                    math = "$$"
                elif math == "$$":
                    math = None
                index += 2
                continue
            if math is None:
                math = "$"
            elif math == "$":
                math = None
            index += 1
            continue

        if char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "\n" and brace == 0 and math is None:
            position = index + 1
            line_start = text.rfind("\n", 0, index) + 1
            previous_line = text[line_start:index]
            strong = (
                not previous_line.strip()
                or _SENTENCE_END.search(previous_line) is not None
                or _STRUCTURAL_LINE_START.match(text, position) is not None
            )
            add(position, STRONG if strong else WEAK)
        index += 1

    return candidates


def _pack(start: int, end: int, positions: Sequence[int], max_chars: int) -> List[Tuple[int, int]]:
    """贪心地把 [start, end) 切成尽量不超过 ``max_chars`` 的区间。"""
    pieces: List[Tuple[int, int]] = []
    current = start
    last: Optional[int] = None
    for boundary in positions:
        if boundary <= current or boundary >= end:
            continue
        if boundary - current > max_chars and last is not None and last > current:
            pieces.append((current, last))
            current = last
        last = boundary
    if last is not None and end - current > max_chars and current < last < end:
        pieces.append((current, last))
        current = last
    pieces.append((current, end))
    return pieces


def split_oversize_latex(text: str, max_chars: int) -> List[str]:
    """在安全边界处切分超长 LaTeX 片段；无合适边界时原样返回。

    先只用强边界（行尾 ``\\``、横线、句末换行）和较浅的环境层级，
    仍超长的部分才逐级放宽到更深层级和普通换行。
    """
    if not text or len(text) <= max_chars:
        return [text]
    candidates = scan_split_candidates(text)
    if not candidates:
        return [text]

    tiers = sorted({(weakness, depth) for _, depth, weakness in candidates})
    segments: List[Tuple[int, int]] = [(0, len(text))]
    for tier in tiers:
        if all(end - start <= max_chars for start, end in segments):
            break
        allowed = sorted(
            position
            for position, depth, weakness in candidates
            if (weakness, depth) <= tier
        )
        refined: List[Tuple[int, int]] = []
        for start, end in segments:
            if end - start <= max_chars:
                refined.append((start, end))
                continue
            refined.extend(_pack(start, end, allowed, max_chars))
        segments = refined

    chunks = [text[start:end] for start, end in segments if end > start]
    return chunks or [text]


def split_translation_chunks(text: str, max_chars: int) -> List[str]:
    """先按段落切分，再对仍超长的段落做结构化二次切分。"""
    chunks: List[str] = []
    for chunk in split_latex_paragraph_chunks(text, max_chars):
        if len(chunk) > max_chars:
            chunks.extend(split_oversize_latex(chunk, max_chars))
        else:
            chunks.append(chunk)
    return [chunk for chunk in chunks if chunk] or [text]


def has_translatable_text(protected_text: str) -> bool:
    """结构保护之后是否还剩可翻译的自然语言。

    只剩单字母变量、数字、运算符（如 align 环境中的 ``x &= y \\``）时
    返回 False，调用方可直接保留原文而不请求模型。
    """
    remainder = LATEX_PROTECTED_TOKEN_PATTERN.sub(" ", protected_text or "")
    return bool(_TRANSLATABLE_WORD.search(remainder) or _CJK_CHAR.search(remainder))
