from pylatexenc.latexwalker import (
    LatexWalker, LatexMacroNode, LatexEnvironmentNode, LatexGroupNode, LatexCharsNode,
    LatexSpecialsNode, LatexMathNode
    )
from pylatexenc.latex2text import LatexNodes2Text
import os
import re
import json
import zipfile
import gzip
import tarfile
import regex
import subprocess
import os
import shutil
import threading
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from collections import Counter
from typing import Dict, List, Optional, Tuple
import time
from src.utils.progress import st
import sys
from pathlib import Path
from src.utils.paths import project_path, extract_tar, extract_zip

options = r"\[[^\[\]]*?\]"
spaces = r"[ \t]*"
get_pattern_brace = lambda index: rf"\{{((?:[^{{}}\\]++|\\[\s\S]|(?{index}))*+)\}}"

# 翻译过程中使用的结构占位符。占位符本身不能被模型翻译、增删或跨片段复制。
LATEX_PLACEHOLDER_PATTERN = re.compile(r"<PLACEHOLDER_[^>]+>")

# 发送给翻译模型的临时结构标记。它们只存在于单次请求中，恢复后不会写入
# sections_map、captions_map 或 envs_map。不能使用 ``<...>``：部分模型会把它
# 当成 XML 开始标签，并自行补出 ``</...>``，造成看似保留了标记、
# 实际却破坏 LaTeX 分组的假阳性。
LATEX_PROTECTED_TOKEN_PATTERN = re.compile(
    r"\[\[\[LATEXTRANS_[A-Z0-9_]+_\d{4,}\]\]\]"
)
_LATEX_PROTECTED_ARTIFACT_PATTERN = re.compile(
    r"(?:<\s*/?\s*LATEXTRANS_[^>\r\n]+>"
    r"|\[\[\[\s*/?\s*LATEXTRANS_[^\]\r\n]+\]\]\])",
    re.IGNORECASE,
)
_LATEX_CONTROL_SEQUENCE_PATTERN = re.compile(
    r"\\(?:[A-Za-z@]+\*?|[^\s])",
    re.DOTALL,
)

# 这些命令的参数是路径、引用键、标签或 URL，属于不可翻译的原子内容。
# 命令本身以及其它排版命令只保护命令名，保留其正文参数给模型翻译。
_LATEX_OPAQUE_COMMANDS = frozenset(
    {
        "begin",
        "end",
        "hyperref",
        "url",
        "path",
        "href",
        "captionof",
        "label",
        "ref",
        "pageref",
        "autoref",
        "nameref",
        "eqref",
        "vref",
        "Vref",
        "fref",
        "Fref",
        "sref",
        "Sref",
        "cref",
        "Cref",
        "crefrange",
        "Crefrange",
        "cpageref",
        "Cpageref",
        "cpagerefrange",
        "Cpagerefrange",
        "cite",
        "citep",
        "citet",
        "citeauthor",
        "citeyear",
        "citealp",
        "citealt",
        "includegraphics",
        "graphicspath",
        "input",
        "include",
        "inputminted",
        "lstinputlisting",
        "bibliography",
        "bibliographystyle",
    }
)
_LATEX_DELIMITED_OPAQUE_COMMANDS = frozenset({"verb", "lstinline"})


def _skip_escaped_character(text: str, index: int) -> int:
    """跳过反斜杠转义的一个字符，返回下一个扫描位置。"""
    if index < len(text) and text[index] == "\\":
        return min(index + 2, len(text))
    return index + 1


def _find_unescaped_delimiter(
    text: str,
    start: int,
    delimiter: str,
) -> Optional[int]:
    """查找未被反斜杠转义的分隔符。"""
    if delimiter.startswith("\\"):
        # ``\)`` 和 ``\]`` 本身就是 LaTeX 数学分隔符，不能再按普通
        # 反斜杠转义序列跳过。
        return text.find(delimiter, start) if delimiter in text[start:] else None

    index = start
    while index < len(text):
        if text[index] == "\\":
            index = _skip_escaped_character(text, index)
            continue
        if text.startswith(delimiter, index):
            return index
        index += 1
    return None


def _find_balanced_group_end(
    text: str,
    start: int,
    opening: str,
    closing: str,
) -> Optional[int]:
    """返回从 ``start`` 开始的平衡括号组结束位置（不含结束位置）。"""
    if start >= len(text) or text[start] != opening:
        return None

    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index = _skip_escaped_character(text, index)
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _consume_command_arguments(
    text: str,
    command_end: int,
    command_name: str,
) -> int:
    """消费不可翻译命令的选项和参数，返回原子片段的结束位置。"""
    index = command_end

    # href 的第二个参数是可翻译的显示文本，只保护第一个 URL 参数。
    # inputminted 的语言名和文件路径都不可翻译。
    max_groups = 2 if command_name == "inputminted" else 1
    consume_braced_argument = command_name != "hyperref"

    groups = 0
    consumed_optional_argument = False
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1

        if index < len(text) and text[index] == "[":
            end = _find_balanced_group_end(text, index, "[", "]")
            if end is None:
                return command_end
            index = end
            consumed_optional_argument = True
            continue

        if (
            consume_braced_argument
            and index < len(text)
            and text[index] == "{"
            and groups < max_groups
        ):
            end = _find_balanced_group_end(text, index, "{", "}")
            if end is None:
                return command_end
            index = end
            groups += 1
            if command_name == "captionof":
                break
            continue

        break

    return index if groups or consumed_optional_argument else command_end


def _consume_delimited_command_argument(
    text: str, command_end: int, command_name: str = "verb"
) -> int:
    r"""消费 ``\verb``/``\lstinline`` 的分隔符包围代码。"""
    index = command_end
    if command_name == "lstinline":
        while index < len(text) and text[index] in " \t":
            index += 1
        if index < len(text) and text[index] == "[":
            option_end = _find_balanced_group_end(text, index, "[", "]")
            if option_end is None:
                return command_end
            index = option_end
        while index < len(text) and text[index] in " \t":
            index += 1
    if index >= len(text) or text[index].isspace():
        return command_end

    if command_name == "lstinline" and text[index] == "{":
        return _find_balanced_group_end(text, index, "{", "}") or len(text)
    delimiter = text[index]
    # verbatim 的反斜杠是普通代码字符，不能把它当成分隔符转义。
    end = text.find(delimiter, index + 1)
    # 源码已经不完整时，宁可把剩余内容整体冻结，也不让模型继续改写。
    return len(text) if end == -1 else end + 1


def _find_math_fragment_end(text: str, start: int) -> Optional[int]:
    """查找行内数学片段的结束位置。"""
    if text.startswith(r"\(", start):
        end = _find_unescaped_delimiter(text, start + 2, r"\)")
        return end + 2 if end is not None else None
    if text.startswith(r"\[", start):
        end = _find_unescaped_delimiter(text, start + 2, r"\]")
        return end + 2 if end is not None else None
    if text[start] != "$":
        return None

    delimiter = "$$" if text.startswith("$$", start) else "$"
    end = _find_unescaped_delimiter(text, start + len(delimiter), delimiter)
    return end + len(delimiter) if end is not None else None


def _command_name(control_sequence: str) -> str:
    """从完整控制序列中提取命令名。"""
    if len(control_sequence) <= 1:
        return ""
    if control_sequence[1].isalpha() or control_sequence[1] == "@":
        return control_sequence[1:].rstrip("*")
    return control_sequence[1:]


def _is_self_contained_inline_math(fragment: str) -> bool:
    r"""判断片段是否为完整的 ``$...$`` 或 ``\(...\)`` 行内公式。"""
    fragment = fragment.strip()
    if fragment.startswith("$") and not fragment.startswith("$$"):
        return _find_math_fragment_end(fragment, 0) == len(fragment)
    if fragment.startswith("\\("):
        return _find_math_fragment_end(fragment, 0) == len(fragment)
    return False


@dataclass(frozen=True)
class LatexSyntaxProtection:
    """单次翻译请求的 LaTeX 结构保护记录。"""

    protected_text: str
    replacements: Dict[str, str]
    token_order: Tuple[str, ...]
    critical_token_order: Tuple[str, ...] = ()
    fixed_suffix: str = ""
    fixed_suffix_fragments: Tuple[str, ...] = ()

    def validate(self, translated_text: str) -> Optional[str]:
        """检查模型是否保留全部标记以及高风险结构的顺序。"""
        actual = tuple(LATEX_PROTECTED_TOKEN_PATTERN.findall(translated_text or ""))
        expected = self.token_order
        expected_counter = Counter(expected)
        actual_counter = Counter(actual)
        critical_expected = self.critical_token_order
        critical_actual = tuple(
            token for token in actual if token in set(critical_expected)
        )
        # 精确标记之外仍出现 LATEXTRANS 伪标记时也必须拒绝。
        # 这一检查专门拦截模型生成的 XML 闭合标签，以及带
        # 斜杠或格式被改写的新标记。
        text_without_exact_tokens = LATEX_PROTECTED_TOKEN_PATTERN.sub(
            "",
            translated_text or "",
        )
        unexpected_artifacts = _LATEX_PROTECTED_ARTIFACT_PATTERN.findall(
            text_without_exact_tokens
        )

        missing = [
            token for token, count in (expected_counter - actual_counter).items()
            for _ in range(count)
        ]
        extra = [
            token for token, count in (actual_counter - expected_counter).items()
            for _ in range(count)
        ]

        # 中文语序常需重复变量名，例如 “$z_i$ …，其中 $z_i$ 包含 …”。
        # 重复一个自成一体的行内公式不会破坏 LaTeX 结构，允许通过；
        # 缺失标记或重复命令/占位符仍然拒绝。
        tolerated_extra = all(
            token in self.replacements
            and token not in critical_expected
            and _is_self_contained_inline_math(self.replacements[token])
            for token in extra
        )
        if (
            not missing
            and (not extra or tolerated_extra)
            and critical_actual == critical_expected
            and not unexpected_artifacts
        ):
            return None
        details = [
            f"结构标记数量或顺序改变：期望 {len(expected)} 个，实际 {len(actual)} 个。"
        ]
        if missing:
            details.append(f"缺少：{missing}")
        if extra:
            details.append(f"多出：{extra}")
        if unexpected_artifacts:
            details.append(f"出现非法或伪造的结构标记：{unexpected_artifacts}")
        if (
            actual_counter == expected_counter
            and critical_actual != critical_expected
        ):
            details.append(
                "高风险 LaTeX 结构标记顺序改变："
                f"期望 {critical_expected}，实际 {critical_actual}。"
            )
        return " ".join(details)

    def restore(self, translated_text: str) -> str:
        """在校验通过后恢复原始 LaTeX 片段。"""
        error = self.validate(translated_text)
        if error:
            raise ValueError(error)

        restored_middle = LATEX_PROTECTED_TOKEN_PATTERN.sub(
            lambda match: self.replacements[match.group(0)],
            translated_text,
        )

        # 尾部结构已由程序保管。如果模型仍根据系统提示自行
        # 补出了完全相同的尾部命令，先剔除回声，再追加权威原文。
        deduplicated_middle = restored_middle.rstrip()
        removed_echo = False
        for fragment in reversed(self.fixed_suffix_fragments):
            fragment_core = fragment.strip()
            if not fragment_core:
                continue
            if not deduplicated_middle.endswith(fragment_core):
                break
            deduplicated_middle = deduplicated_middle[:-len(fragment_core)].rstrip()
            removed_echo = True
        if removed_echo:
            restored_middle = deduplicated_middle

        return f"{restored_middle}{self.fixed_suffix}"


def _restore_token_text(text: str, replacements: Dict[str, str]) -> str:
    """将一段只含临时标记和空白的文本恢复为原始 LaTeX。"""
    return LATEX_PROTECTED_TOKEN_PATTERN.sub(
        lambda match: replacements[match.group(0)],
        text,
    )


def _detach_fixed_token_suffix(
    protected_text: str,
    replacements: Dict[str, str],
    token_order: Tuple[str, ...],
    critical_token_order: Tuple[str, ...],
) -> Tuple[
    str,
    Dict[str, str],
    Tuple[str, ...],
    Tuple[str, ...],
    str,
    Tuple[str, ...],
]:
    r"""把位于片段末尾的确定性 LaTeX 结构留在本地。

    这些尾部标记的位置不受目标语言语序影响，没有必要让模型
    再复述一遍。特别是连续出现在章节末尾的 ``\vfill``、
    ``\bibliography`` 和 ``\end{document}``，模型很容易将它们当作
    无需输出的元数据。本地拆卸后，恢复阶段会原位补回。
    前缀仍保留在模型输入中，以免 ``\caption`` 等命令上下文消失
    后被模型自行补出，最终与本地前缀重复。
    """
    if not protected_text or not token_order:
        return (
            protected_text,
            replacements,
            token_order,
            critical_token_order,
            "",
            (),
        )

    token_pattern = LATEX_PROTECTED_TOKEN_PATTERN.pattern
    middle = protected_text
    suffix_match = re.search(
        rf"(?:[ \t\r\n]*(?:{token_pattern}))+$",
        middle,
    )
    encoded_suffix = suffix_match.group(0) if suffix_match else ""
    suffix_tokens = tuple(LATEX_PROTECTED_TOKEN_PATTERN.findall(encoded_suffix))
    if encoded_suffix:
        middle = middle[:suffix_match.start()]

    active_order = tuple(LATEX_PROTECTED_TOKEN_PATTERN.findall(middle))
    active_set = set(active_order)
    active_replacements = {
        token: replacement
        for token, replacement in replacements.items()
        if token in active_set
    }
    active_critical_order = tuple(
        token for token in critical_token_order if token in active_set
    )

    return (
        middle,
        active_replacements,
        active_order,
        active_critical_order,
        _restore_token_text(encoded_suffix, replacements),
        tuple(replacements[token] for token in suffix_tokens),
    )


def find_latex_bracket_errors(content: str) -> List[str]:
    """查找未配对的大括号和方括号，供请求层和独立校验共用。"""
    content = mask_latex_opaque_content(content)
    bracket_pairs = {"[": "]", "{": "}"}
    opening_brackets = set(bracket_pairs)
    closing_brackets = set(bracket_pairs.values())
    stack = []
    errors = []

    for idx, char in enumerate(content):
        if char in opening_brackets:
            stack.append((char, idx))
        elif char in closing_brackets:
            if not stack:
                fragment = content[max(0, idx - 10):idx + 10]
                errors.append(
                    f"Extra closing bracket '{char}' at position {idx}, context: {fragment}"
                )
            else:
                last_open, open_idx = stack.pop()
                if bracket_pairs[last_open] != char:
                    fragment = content[open_idx:idx + 1]
                    errors.append(
                        f"Bracket mismatch: '{last_open}' opened at {open_idx} "
                        f"does not match '{char}' at {idx}, fragment: {fragment}"
                    )

    for open_bracket, pos in stack:
        fragment = content[pos:pos + 20]
        errors.append(
            f"Unmatched opening bracket '{open_bracket}' at position {pos}, fragment: {fragment}"
        )

    return errors


def split_latex_paragraph_chunks(text: str, max_chars: int) -> List[str]:
    r"""按顶层空行（及列表的 ``\item`` 行首）把长文本切成不超过 ``max_chars`` 的片段。

    只在花括号深度为 0、且不在 ``$...$`` 中的空行或 ``\item`` 前切分，
    保证 ``"".join(chunks) == text``，每段可以独立翻译后原样拼回。
    无法切分（没有合适边界）时返回单个片段。
    """
    if not text or len(text) <= max_chars:
        return [text]

    boundaries = []
    depth = 0
    in_math = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "$":
            in_math = not in_math
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "\n" and depth == 0 and not in_math:
            blank = re.match(r"\n[ \t]*\n\s*", text[index:])
            if blank:
                boundaries.append(index + blank.end())
                index += blank.end()
                continue
            # 长列表环境没有空行时，在行首 \item 之前切分。
            if re.match(r"\n[ \t]*\\item(?![A-Za-z])", text[index:]):
                boundaries.append(index + 1)
        index += 1

    chunks = []
    start = 0
    last_candidate = None
    for boundary in boundaries:
        if (
            boundary - start > max_chars
            and last_candidate is not None
            and last_candidate > start
        ):
            chunks.append(text[start:last_candidate])
            start = last_candidate
        last_candidate = boundary
    if (
        last_candidate is not None
        and length - start > max_chars
        and start < last_candidate < length
    ):
        chunks.append(text[start:last_candidate])
        start = last_candidate
    chunks.append(text[start:])
    return [chunk for chunk in chunks if chunk] or [text]


def protect_latex_syntax(
    text: str,
    namespace: str = "TOKEN",
    protect_group_delimiters: bool = False,
) -> LatexSyntaxProtection:
    r"""保护 LaTeX 命令、数学片段和项目占位符，供翻译模型安全处理。

    排版命令只保护命令名，因此 ``\textbf{自然语言}`` 的正文仍会翻译；
    URL、引用键、标签、图片路径等不可翻译参数则整体保护。图注等短片段
    可以额外保护分组括号，避免模型漏掉嵌套命令的参数边界。恢复前会严格
    检查标记的数量和顺序，避免模型合并或删除命令后继续生成结果。
    """
    if not text:
        return LatexSyntaxProtection("", {}, ())

    normalized_namespace = re.sub(r"[^A-Za-z0-9]", "_", namespace.upper()) or "TOKEN"
    occupied = set(LATEX_PROTECTED_TOKEN_PATTERN.findall(text))
    replacements: Dict[str, str] = {}
    token_order: List[str] = []
    critical_token_order: List[str] = []
    fragments: List[Tuple[int, int]] = []
    index = 0

    def add_fragment(start: int, end: int) -> None:
        nonlocal index
        if end <= start:
            return
        while True:
            index += 1
            token = f"[[[LATEXTRANS_{normalized_namespace}_{index:04d}]]]"
            if token not in occupied:
                break
        occupied.add(token)
        replacements[token] = text[start:end]
        token_order.append(token)
        fragment = text[start:end]
        if (
            LATEX_PLACEHOLDER_PATTERN.fullmatch(fragment)
            or (protect_group_delimiters and fragment in "{}[]")
            or re.match(
                r"\\(?:begin|end|section|subsection|subsubsection|chapter|paragraph)\b",
                fragment,
            )
        ):
            critical_token_order.append(token)
        fragments.append((start, end))

    position = 0
    opaque_fragments = {
        start: end for start, end, _ in get_latex_opaque_fragments(text)
    }
    while position < len(text):
        if position in opaque_fragments:
            end = opaque_fragments[position]
            add_fragment(position, end)
            position = end
            continue
        protected_token = LATEX_PROTECTED_TOKEN_PATTERN.match(text, position)
        if protected_token:
            add_fragment(protected_token.start(), protected_token.end())
            position = protected_token.end()
            continue

        placeholder = LATEX_PLACEHOLDER_PATTERN.match(text, position)
        if placeholder:
            add_fragment(placeholder.start(), placeholder.end())
            position = placeholder.end()
            continue

        if text[position] in {"$", "\\"}:
            math_end = _find_math_fragment_end(text, position)
            if math_end is not None:
                add_fragment(position, math_end)
                position = math_end
                continue

        if text[position] == "\\":
            command_match = _LATEX_CONTROL_SEQUENCE_PATTERN.match(text, position)
            if command_match:
                command_end = command_match.end()
                command_name = _command_name(command_match.group(0))
                if command_name in _LATEX_OPAQUE_COMMANDS:
                    fragment_end = _consume_command_arguments(
                        text,
                        command_end,
                        command_name,
                    )
                    add_fragment(position, fragment_end)
                    position = fragment_end
                    continue

                if command_name in _LATEX_DELIMITED_OPAQUE_COMMANDS:
                    fragment_end = _consume_delimited_command_argument(
                        text,
                        command_end,
                        command_name,
                    )
                    add_fragment(position, fragment_end)
                    position = fragment_end
                    continue

                # 控制词后的空白用于终止命令名。例如 ``\model is`` 若只
                # 保护 ``\model``，译文会变成 ``\model是``，被 TeX 视为
                # 另一个命令。把原有的一个空白也留在本地恢复。
                if (
                    re.fullmatch(r"\\[A-Za-z@]+\*?", command_match.group(0))
                    and command_end < len(text)
                    and text[command_end].isspace()
                ):
                    command_end += 1
                add_fragment(position, command_end)
                position = command_end
                continue

        if protect_group_delimiters and text[position] in "{}[]":
            add_fragment(position, position + 1)
            position += 1
            continue

        position += 1

    if not fragments:
        return LatexSyntaxProtection(text, {}, ())

    protected_parts: List[str] = []
    last_end = 0
    for (start, end), token in zip(fragments, token_order):
        protected_parts.append(text[last_end:start])
        protected_parts.append(token)
        last_end = end
    protected_parts.append(text[last_end:])

    return LatexSyntaxProtection(
        *_detach_fixed_token_suffix(
            "".join(protected_parts),
            replacements,
            tuple(token_order),
            tuple(critical_token_order),
        )
    )


def _is_within_dir(base_dir: Path, target_path: Path) -> bool:
    try:
        target_path.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def _safe_extract_zip(zip_ref: zipfile.ZipFile, extract_path: Path) -> None:
    extract_zip(zip_ref, extract_path)


def _safe_extract_tar(tar_ref: tarfile.TarFile, extract_path: Path) -> None:
    extract_tar(tar_ref, extract_path)


DOWNLOAD_COMPLETE_MARKER = ".latextrans_download_complete"


def _archive_extract_path(root: str, file_name: str) -> Path:
    name_lower = file_name.lower()
    if name_lower.endswith(".tar.gz"):
        stem = file_name[:-7]
    elif name_lower.endswith(".tgz"):
        stem = file_name[:-4]
    elif name_lower.endswith(".tar"):
        stem = file_name[:-4]
    elif name_lower.endswith(".zip"):
        stem = file_name[:-4]
    elif name_lower.endswith(".gz"):
        stem = file_name[:-3]
    else:
        stem = os.path.splitext(file_name)[0]
    return Path(root) / stem


def _directory_has_tex_sources(project_dir: str) -> bool:
    if not os.path.isdir(project_dir):
        return False
    for _, _, files in os.walk(project_dir):
        if any(file.lower().endswith(".tex") for file in files):
            return True
    return False


def _has_download_complete_marker(project_dir: str) -> bool:
    return os.path.isfile(os.path.join(project_dir, DOWNLOAD_COMPLETE_MARKER))


def _mark_download_complete(project_dir: Path) -> None:
    marker_path = _download_marker_path(project_dir)
    marker_path.write_text("ok\n", encoding="utf-8")


def _download_marker_path(project_dir: Path) -> Path:
    project_dir = Path(project_dir)
    project_dir = project_path(project_dir.parent, project_dir, writing=True)
    marker_path = project_path(project_dir, project_dir / DOWNLOAD_COMPLETE_MARKER, writing=True)
    if marker_path.exists() and not marker_path.is_file():
        raise ValueError(f"Download completion marker is not a regular file: {marker_path}")
    return marker_path


def _preflight_extracted_tree(extract_path: Path) -> List[Path]:
    """Validate the entire temporary tree before copying, moving or removing it."""
    extract_path = Path(extract_path)
    extract_path = project_path(extract_path.parent, extract_path, writing=True)
    if not extract_path.is_dir():
        raise ValueError(f"Extraction source is not a directory: {extract_path}")
    entries = []
    pending = [extract_path]
    while pending:
        current = pending.pop()
        for entry in sorted(current.iterdir()):
            entry = project_path(extract_path, entry, writing=True)
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise ValueError(f"Extraction contains a non-regular file: {entry}")
            entries.append(entry)
    return entries


def _merge_extracted_dir(temp_extract_path: Path, extract_path: Path) -> None:
    temp_extract_path = Path(temp_extract_path)
    extract_path = Path(extract_path)
    entries = _preflight_extracted_tree(temp_extract_path)
    temp_extract_path = project_path(temp_extract_path.parent, temp_extract_path, writing=True)
    extract_path = project_path(extract_path.parent, extract_path, writing=True)
    if (
        extract_path == temp_extract_path
        or extract_path in temp_extract_path.parents
        or temp_extract_path in extract_path.parents
    ):
        raise ValueError("Extraction source and destination must be separate directories")
    if extract_path.exists() and not extract_path.is_dir():
        raise ValueError(f"Extraction destination is not a directory: {extract_path}")
    _download_marker_path(temp_extract_path)
    _download_marker_path(extract_path)
    # Validate every overwritten path before the first copy, including parents
    # and the completion marker written after the copy. A late unsafe member
    # must not allow earlier members to overwrite existing project files.
    for entry in entries:
        target = project_path(extract_path, extract_path / entry.relative_to(temp_extract_path), writing=True)
        if target.exists() and entry.is_dir() != target.is_dir():
            raise ValueError(f"Extraction would replace a different file type: {target}")
    if extract_path.exists():
        shutil.copytree(temp_extract_path, extract_path, dirs_exist_ok=True)
        shutil.rmtree(temp_extract_path)
    else:
        shutil.move(str(temp_extract_path), str(extract_path))
    _mark_download_complete(extract_path)


def _read_fileobj_fully(fileobj, chunk_size: int = 1024 * 1024) -> None:
    while fileobj.read(chunk_size):
        pass


def _is_gzip_file(file_path: str) -> bool:
    with open(file_path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def _is_complete_archive(file_path: str) -> bool:
    """
    验证压缩包是否能完整读完，避免把中断下载留下的半截文件当成可用源码。
    """
    if not os.path.isfile(file_path) or os.path.getsize(file_path) == 0:
        return False

    try:
        if zipfile.is_zipfile(file_path):
            with zipfile.ZipFile(file_path, "r") as zip_ref:
                return zip_ref.testzip() is None

        if not tarfile.is_tarfile(file_path) and _is_gzip_file(file_path):
            with gzip.open(file_path, "rb") as gz_ref:
                _read_fileobj_fully(gz_ref)
            return True

        with tarfile.open(file_path, "r:*") as tar_ref:
            for member in tar_ref.getmembers():
                if not member.isfile():
                    continue
                extracted = tar_ref.extractfile(member)
                if extracted is not None:
                    _read_fileobj_fully(extracted)
            return True
    except Exception:
        return False


def get_pattern_command_full(name, n=None):
    pattern = rf'\\({name})'
    if n is None:
        pattern += rf'{spaces}({options})?'
        n = 1
        begin_brace = 3
    else:
        begin_brace = 2
    for i in range(n):
        tmp = get_pattern_brace(i*2+begin_brace)
        pattern += rf'{spaces}({tmp})'
    if n == 0:
        pattern += r'(?=[^a-zA-Z])'
    return pattern

def extract_archive_file(file_path: str, top_level: bool = True) -> Optional[Path]:
    """解压单个压缩包并删除源文件，返回解压目录；不是压缩包时返回 None。

    ``top_level`` 为 True 时，arXiv 单文件投稿的 gzip .tex 也会被还原。
    """
    root, file = os.path.split(file_path)
    if file.endswith(".download") or file == DOWNLOAD_COMPLETE_MARKER:
        return None

    extract_path = _archive_extract_path(root, file)
    temp_extract_path = Path(root) / f".{extract_path.name}.extracting"
    try:
        file_path = os.fspath(project_path(root, file_path, writing=True))
        extract_path = project_path(root, extract_path, writing=True)
        temp_extract_path = project_path(root, temp_extract_path, writing=True)
        if zipfile.is_zipfile(file_path):
            if temp_extract_path.exists():
                _preflight_extracted_tree(temp_extract_path)
                shutil.rmtree(temp_extract_path)
            temp_extract_path.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                _safe_extract_zip(zip_ref, temp_extract_path)
                _merge_extracted_dir(temp_extract_path, extract_path)
                print(f"Extracted {file} to {extract_path}")
            os.remove(file_path)
            return extract_path
        if tarfile.is_tarfile(file_path):
            if temp_extract_path.exists():
                _preflight_extracted_tree(temp_extract_path)
                shutil.rmtree(temp_extract_path)
            temp_extract_path.mkdir(parents=True, exist_ok=True)
            with tarfile.open(file_path, 'r:*') as tar_ref:
                _safe_extract_tar(tar_ref, temp_extract_path)
                _merge_extracted_dir(temp_extract_path, extract_path)
                print(f"Extracted {file} to {extract_path}")
            os.remove(file_path)
            return extract_path
        if top_level and file.lower().endswith(".tar.gz") and _is_gzip_file(file_path):
            # arXiv 对单文件投稿返回 gzip 压缩的 .tex，而不是 tar 包。
            target = project_path(root, extract_path / f"{extract_path.name}.tex", writing=True)
            extract_path.mkdir(parents=True, exist_ok=True)
            with gzip.open(file_path, "rb") as gz_ref, open(target, "wb") as out:
                shutil.copyfileobj(gz_ref, out)
            print(f"Extracted single-file source {file} to {target}")
            os.remove(file_path)
            return extract_path
    except Exception as e:
        try:
            temp_extract_path = project_path(root, temp_extract_path, writing=True)
            if temp_extract_path.exists():
                _preflight_extracted_tree(temp_extract_path)
                shutil.rmtree(temp_extract_path)
        except (OSError, ValueError):
            # Preserve unsafe existing paths rather than following a link during cleanup.
            pass
        print(f"[SKIP] Failed to extract {file_path}: {e}")
    return None


def extract_compressed_files(folder_path):
    """
    Traverse the given folder and extract all compressed files (zip, tar, tar.gz, etc.).
    After extraction, delete the source compressed files.
    
    Args:
        folder_path (str): Path to the folder containing compressed files.
    """
    for root, _, files in os.walk(folder_path):
        for file in files:
            extract_archive_file(
                os.path.join(root, file),
                top_level=os.path.normpath(root) == os.path.normpath(folder_path),
            )

def get_profect_dirs(folder_path):
    """
    Get a list of all subdirectories in the given folder.
    
    Args:
        folder_path (str): Path to the folder.
    
    Returns:
        list: A list of subdirectory paths.
    """
    projects = []
    for d in os.listdir(folder_path):
        if os.path.isdir(os.path.join(folder_path, d)):
            project_path = os.path.join(folder_path, d)
            projects.append(project_path)
    return projects

def has_appendix(latex_code):
    appendix_pattern = re.compile(r"\\appendix\b")
    return bool(appendix_pattern.search(latex_code))

def remove_appendix_content(latex_code):
    appendix_pattern = re.compile(r"\\appendix\b.*?(?=\\end\{document\})", re.DOTALL)
    
    modified_code = appendix_pattern.sub("", latex_code)
    
    return modified_code

def extract_latex_nodes(tex):
    walker = LatexWalker(tex)
    nodes, npos, nlen = walker.get_latex_nodes()
    return nodes

def extract_text_from_tex(tex):
    # convert = CustomLatexNodes2Text()
    # text = convert.latex_to_text(tex)
    text = LatexNodes2Text().latex_to_text(tex)
    return text
    
def extract_structure(nodes, depth=0):
    structure = {
        'command': [],
        'environment': [],
        'special': [],
        'math': []
    }

    for node in nodes:
        if isinstance(node, LatexMacroNode):
            structure['command'].append({'name': node.macroname, 'depth': depth})
            if node.nodeargd:
                sub_structure = extract_structure(node.nodeargd.argnlist, depth + 1)
                for key in sub_structure:
                    structure[key].extend(sub_structure[key])
        elif isinstance(node, LatexEnvironmentNode):
            structure['environment'].append({'name': node.envname, 'depth': depth})
            sub_structure = extract_structure(node.nodelist, depth + 1)
            for key in sub_structure:
                structure[key].extend(sub_structure[key])
        elif isinstance(node, LatexGroupNode):
            sub_structure = extract_structure(node.nodelist, depth + 1)
            for key in sub_structure:
                structure[key].extend(sub_structure[key])
        elif isinstance(node, LatexSpecialsNode):
            structure['special'].append({'chars': node.specials_chars, 'depth': depth})
        elif isinstance(node, LatexMathNode):
            structure['math'].append({'type': node.displaytype, 'depth': depth})
            sub_structure = extract_structure(node.nodelist, depth + 1)
            for key in sub_structure:
                structure[key].extend(sub_structure[key])

    return structure

def extract_title(latex_code):
    title_start = latex_code.find(r"\title{")
    if title_start == -1:
        title_start = latex_code.find(r"\title[")
    if title_start == -1:
        return "No title"  
    
    brace_start = latex_code.find("{", title_start)
    if brace_start == -1:
        return "No title"  
    
    stack = []  
    for i in range(brace_start, len(latex_code)):
        if latex_code[i] == "{":
            stack.append(i)  
        elif latex_code[i] == "}":
            stack.pop()  
            if not stack:  
                return latex_code[brace_start + 1:i].strip()

    return "No title"  

def extract_abstract(latex_code):
    abstract_pattern = regex.compile(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", regex.DOTALL)
    
    match = abstract_pattern.search(latex_code)
    
    if match:
        abstract = match.group(1).strip() 
        return abstract
    
    abstract_start = latex_code.find(r"\abstract{")
    if abstract_start == -1:
        return "No abstract"

    brace_start = latex_code.find("{", abstract_start)
    if brace_start == -1:
        return "No abstract"

    stack = []
    for i in range(brace_start, len(latex_code)):
        if latex_code[i] == "{":
            stack.append(i)  #
        elif latex_code[i] == "}":
            stack.pop()  # 
            if not stack:  # 
                return latex_code[brace_start + 1:i].strip()

    return "No abstract" 

def extract_keywords(latex_code):
    keywords_pattern = regex.compile(r"\\keywords\{(?:\{([^{}]*)\}|([^{}]*))\}", regex.DOTALL)
    
    match = keywords_pattern.search(latex_code)
    
    keywords = match.group(1) or match.group(2) if match else None
    return keywords.strip() if keywords else None

def extract_sections(latex_code):
    section_pattern = regex.compile(r"\\(section|chapter)\b")
    match = section_pattern.search(latex_code)
    if not match:
        return latex_code, ""
    
    section_index = match.start()
    before_section = latex_code[:section_index]
    after_section = latex_code[section_index:]
    return before_section, after_section

def extract_captions(latex_code):
    caption_start = latex_code.find(r"\caption{")
    if caption_start == -1:
        caption_start = latex_code.find(r"\caption[")
    if caption_start == -1:
        return "No caption"  
    
    brace_start = latex_code.find("{", caption_start)
    if brace_start == -1:
        return "No caption"  
    
    stack = []  
    for i in range(brace_start, len(latex_code)):
        if latex_code[i] == "{":
            stack.append(i)  
        elif latex_code[i] == "}":
            stack.pop()  
            if not stack:  
                return latex_code[brace_start + 1:i].strip()

    return "No caption"  

def replace_figures(latex_code):
    figure_pattern = regex.compile(
        r"\\begin\{(figure\*?|wrapfigure|SCfigure|tikzpicture)\}.*?\\end\{\1\}",
        regex.DOTALL
    )
    
    def replace_match(match):
        figure_code = match.group(0)  
        caption = extract_captions(figure_code)  
        return f"<FIGURE: {caption}>"
    
    latex_code = figure_pattern.sub(replace_match, latex_code)
    return latex_code

def replace_tables(latex_code):
    table_pattern = regex.compile(
        r"\\begin\{(table\*?|tabular|tabularx|longtable)\}.*?\\end\{\1\}",
        regex.DOTALL
    )
    
    def replace_match(match):
        table_code = match.group(0)  
        caption = extract_captions(table_code)  
        return f"<TABLE: {caption}>"
    
    latex_code = table_pattern.sub(replace_match, latex_code)
    return latex_code

def replace_newcommand(newcommand, latex_code):
    command_name, n_arguments, content = newcommand
    pattern = regex.compile(get_pattern_command_full(command_name, n_arguments), regex.DOTALL)

    def replace_function(match):
        this_content = content
        name = match.group(1)
        assert re.match(command_name, name)
        for i in range(n_arguments):
            text = match.group(3 + i * 2)
            this_content = this_content.replace(f'#{i+1}', f' {text} ')
        return this_content

    return pattern.sub(replace_function, latex_code)

def process_newcommands(latex_code):

    def get_nonNone(*args):
        result = [arg for arg in args if arg is not None]
        assert len(result) == 1
        return result[0]

    pattern_newcommand = rf'\\(?:newcommand\*?|def|renewcommand){spaces}(?:\{{\\([a-zA-Z]+)\}}|\\([a-zA-Z]+)){spaces}(?:\[(\d)\])?{spaces}({get_pattern_brace(4)})'  # \newcommand{name}[n_arguments]{content}, group 1/2: name, group 3: n_arguments, group 5: content

    pattern = regex.compile(pattern_newcommand, regex.DOTALL)
    count = 0
    full_newcommands = []
    match = pattern.search(latex_code)
    while match:
        name1 = match.group(1)
        name2 = match.group(2)
        name = get_nonNone(name1, name2)
        n_arguments = match.group(3)
        if n_arguments is None:
            n_arguments = 0
        else:
            n_arguments = int(n_arguments)
        content = match.group(5)
        latex_code = latex_code.replace(match.group(), f'REPLACE_{count}_NEWCOMMAND')
        # print(latex_code)
        full_newcommands.append(match.group(0))
        latex_code = replace_newcommand((name, n_arguments, content), latex_code)
        count += 1
        match = pattern.search(latex_code)
    for i in range(count):
        latex_code = latex_code.replace(f'REPLACE_{i}_NEWCOMMAND', full_newcommands[i])
    return latex_code

def replace_href(latex_code):
    href_pattern = regex.compile(r"\\href\{[^{}]*\}\{(.*?)\}")
    
    latex_code = href_pattern.sub(r"\1", latex_code)
    return latex_code

def replace_includegraphics(latex_code):
    includegraphics_pattern = regex.compile(r"\\includegraphics(?:\[[^\]]*\])?\{[^\}]*\}", regex.DOTALL)
    latex_code = includegraphics_pattern.sub("", latex_code)
    return latex_code

def process_latex_to_eva(latex_code):
    latex_code = replace_href(latex_code)
    latex_code = replace_includegraphics(latex_code)
    latex_code = process_newcommands(latex_code)
    before_section, after_section = extract_sections(latex_code)
    title = extract_title(before_section) if extract_title(before_section) else 'No title'
    abstract = extract_abstract(before_section) if extract_abstract(before_section) else 'No abstract'
    keywords = extract_keywords(before_section) if extract_keywords(before_section) else ''
    after_section = replace_figures(after_section)
    after_section = replace_tables(after_section)
    tex_to_eva = f"{title}\n\n{abstract}\n\n{keywords}\n\n{after_section}"
    return tex_to_eva

def delete_ph(text) -> str:

    pattern = r'§(\.§){0,2}'
    text = re.sub(pattern, '', text)
    placeholder_pattern = r"<.*?PLACEHOLDER.*?>"
    text = re.sub(placeholder_pattern, "", text).strip()
    text = text.replace('\n', ' ')
    
    text = re.sub(r' +', ' ', text)
    
    return text.strip()


def extract_latex_placeholders(text: str) -> List[str]:
    """按出现顺序提取 LaTeX 结构占位符。"""
    if not text:
        return []
    return LATEX_PLACEHOLDER_PATTERN.findall(text)


def strip_unexpected_placeholders(
    source: str,
    translated: str,
) -> tuple[str, List[str]]:
    """删除翻译片段中不属于源片段的占位符。

    caption/title 是独立翻译片段，正常情况下不会包含结构占位符。
    模型偶尔会把相邻 figure 或 caption 的占位符一起复制进来；这些标记
    若直接写入包含文件，会逃过主文件清理并破坏最终 TeX。这里仅删除
    *多出来的* 标记，源片段中本来存在的标记仍原样保留，供后续重建。
    """
    expected = set(extract_latex_placeholders(source))
    unexpected: List[str] = []

    def replace(match: re.Match) -> str:
        placeholder = match.group(0)
        if placeholder in expected:
            return placeholder
        unexpected.append(placeholder)
        return ""

    cleaned = LATEX_PLACEHOLDER_PATTERN.sub(replace, translated or "")
    return cleaned, list(dict.fromkeys(unexpected))

def extract_pure_text(dir):
    main_file_path = find_main_tex_file(dir)
    if main_file_path is None:
        raise FileNotFoundError(f"File not found: {main_file_path}")
    full_latex_code = merge_tex_from_inputs(main_file_path)
    main_latex_code = process_latex_to_eva(full_latex_code)
    pure_text = extract_text_from_tex(main_latex_code)
    return pure_text

def get_texts_from_data(folder_path, output_folder):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    extract_compressed_files(folder_path)
    projects = get_profect_dirs(folder_path)
    # print("projects:", projects)
    total_projects = len(projects)
    for idx, project in enumerate(projects, start=1):
        print(f"[{idx}/{total_projects}] Processing {os.path.basename(project)}")
        try:
            text = extract_pure_text(project)
            project_name = os.path.basename(project)
            output_file = os.path.join(output_folder, f"{project_name}.txt")
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"Error processing project {project}: {e}")
            continue  # 跳过出错的项目

def extract_pure_tags(dir):
    main_file_path = find_main_tex_file(dir)
    if main_file_path is None:
        raise FileNotFoundError(f"File not found: {main_file_path}")
    main_latex_code = merge_tex_from_inputs(main_file_path)
    nodes = extract_latex_nodes(main_latex_code)
    tag_structure = extract_structure(nodes)
    return tag_structure

def loop_files(dir):
    all_files = []
    for root, dirs, files in os.walk(dir):
        for file in files:
            all_files.append(os.path.join(root, file))
    return all_files

def read_tex_file(path):
    """读取 TeX 普通文件，并在传入目录时给出明确错误。"""
    path = os.fspath(path)
    if os.path.isdir(path):
        raise IsADirectoryError(f"Expected a TeX file, but received a directory: {path}")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"TeX file not found: {path}")

    with open(path, 'rb') as f:
        raw = f.read()
    # 早期 arXiv 稿件常为 Latin-1/CP1252；UTF-8 BOM 也需要去掉。
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def resolve_tex_input_file(base_dir, input_name, project_root=None):
    """将 ``\\input``/``\\include`` 名称解析为普通文件路径。

    TeX 源码包中可能同时存在同名目录和 ``.tex`` 文件，例如：
    ``sections/results/generator`` 目录与 ``generator.tex`` 文件并存。
    ``os.path.exists`` 会把目录也判定为存在，之后交给 ``open`` 就会在
    Windows 上触发 ``PermissionError``。这里明确只接受普通文件；无扩展名
    的输入保留原有语义，优先使用精确文件名，再尝试追加 ``.tex``。
    """
    normalized_name = str(input_name).strip()
    if not normalized_name:
        return None

    requested_path = Path(base_dir) / normalized_name
    candidates = [requested_path]
    if requested_path.suffix.lower() != ".tex":
        candidates.append(Path(f"{requested_path}.tex"))

    for candidate in candidates:
        if project_root is not None:
            candidate = project_path(project_root, candidate)
        if candidate.is_file():
            return os.fspath(candidate)

    return None

def read_json_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return data
    
def find_tex_files(dir):
    all_files = loop_files(dir)

    tex_files = [f for f in all_files if f.lower().endswith('.tex') and os.path.isfile(f)]

    return tex_files

# fancyvrb/fvextra 的大小写环境和保存代码的环境同样具有 verbatim 语义。
LATEX_OPAQUE_ENVIRONMENTS = frozenset(
    {
        "verbatim", "verbatim*", "Verbatim", "Verbatim*",
        "BVerbatim", "BVerbatim*", "LVerbatim", "LVerbatim*",
        "SaveVerbatim", "SaveVerbatim*", "lstlisting", "lstlisting*",
        "minted", "minted*",
    }
)
LATEX_PROSE_ENVIRONMENTS = frozenset(
    {"abstract", "itemize", "itemize*", "enumerate", "enumerate*"}
)
_LATEX_OPAQUE_START_PATTERN = re.compile(
    r"\\begin\s*\{\s*(?P<environment>"
    + "|".join(re.escape(name) for name in sorted(LATEX_OPAQUE_ENVIRONMENTS))
    + r")\s*\}|\\(?P<inline>verb|lstinline)\*?(?![A-Za-z@])"
)
_COMMENT_PROTECTED_PATTERN = re.compile(r"\\(?:url|href)\s*\{[^{}\n]*\}")


def get_latex_opaque_fragments(tex: str) -> List[Tuple[int, int, str]]:
    """返回代码环境及行内代码的范围，不把其正文解释为 TeX。"""
    fragments = []
    position = 0
    while True:
        match = _LATEX_OPAQUE_START_PATTERN.search(tex, position)
        if match is None:
            break
        line_start = tex.rfind("\n", 0, match.start()) + 1
        # 已消费的代码正文里的 % 不能影响同一行之后的下一段代码。
        if _latex_comment_start(tex[max(line_start, position):match.start()]) is not None:
            line_end = tex.find("\n", match.start())
            if line_end == -1:
                break
            position = line_end + 1
            continue
        environment = match.group("environment")
        if environment:
            ending = re.search(
                r"\\end\s*\{\s*" + re.escape(environment) + r"\s*\}",
                tex[match.end():],
            )
            end = match.end() + ending.end() if ending else len(tex)
            name = environment
        else:
            name = match.group("inline")
            end = _consume_delimited_command_argument(tex, match.end(), name)
            if end == match.end():
                position = end
                continue
        fragments.append((match.start(), end, name))
        position = end
    return fragments


def _latex_comment_start(line: str) -> Optional[int]:
    for match in re.finditer("%", line):
        previous = match.start() - 1
        while previous >= 0 and line[previous] == "\\":
            previous -= 1
        if (match.start() - 1 - previous) % 2 == 0:
            return match.start()
    return None


def mask_latex_opaque_content(tex: str) -> str:
    """用等长空白遮住代码片段，保留后续扫描的源码位置和行号。"""
    pieces = []
    position = 0
    for start, end, _ in get_latex_opaque_fragments(tex):
        pieces.append(tex[position:start])
        pieces.append(re.sub(r"[^\r\n]", " ", tex[start:end]))
        position = end
    pieces.append(tex[position:])
    return "".join(pieces)


def _transform_preserving_opaque_content(tex: str, transform) -> str:
    """整篇源码变换时暂存代码正文，避免包修补和引用归一化改写代码样例。"""
    pieces = []
    replacements = {}
    position = 0
    for start, end, _ in get_latex_opaque_fragments(tex):
        index = len(replacements)
        token = f"\x00LTO{index}\x00"
        while token in tex or token in replacements:
            index += 1
            token = f"\x00LTO{index}\x00"
        replacements[token] = tex[start:end]
        pieces.extend((tex[position:start], token))
        position = end
    pieces.append(tex[position:])
    transformed = transform("".join(pieces))
    if not replacements:
        return transformed
    pattern = re.compile("|".join(re.escape(token) for token in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], transformed)


def remove_comments(tex: str) -> str:
    """
    Remove both % line comments and \\begin{comment} ... \\end{comment} blocks from LaTeX code.

    代码环境、\\url/\\href 参数和 \\verb 中的 % 是正文而不是注释，
    例如 ``\\url{https://a.com/%7Euser}``，必须原样保留。
    """
    protected = []

    def protect(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00LTC{len(protected) - 1}\x00"

    # 先冻结代码，再移除 comment 环境，避免删除代码样例里的伪 comment。
    for start, end, _ in reversed(get_latex_opaque_fragments(tex)):
        protected.append(tex[start:end])
        tex = tex[:start] + f"\x00LTC{len(protected) - 1}\x00" + tex[end:]
    tex = _COMMENT_PROTECTED_PATTERN.sub(protect, tex)
    tex = re.sub(r'\\begin\s*\{comment\}.*?\\end\s*\{comment\}', '', tex, flags=re.DOTALL)

    lines = tex.splitlines()
    cleaned = []
    for line in lines:
        stripped_line = line.lstrip()
        # Skip full-line comments (ignoring leading whitespace)
        if stripped_line.startswith('%'):
            continue
        # Remove inline comments (ignore escaped %)
        comment_start = _latex_comment_start(line)
        if comment_start is not None:
            line = line[:comment_start]
        cleaned.append(line.rstrip())  # Optionally remove trailing whitespace

    tex = '\n'.join(cleaned)
    if protected:
        tex = re.sub(r"\x00LTC(\d+)\x00", lambda m: protected[int(m.group(1))], tex)
    return tex

def compress_newlines(tex):
    """
    Replace consecutive newlines (including spaces) exceeding four with exactly two newlines.

    Args:
        content (str): The input string to process.

    Returns:
        str: The processed string with normalized newlines.
    """
    return re.sub(r'(\s*\n\s*){3,}', '\n\n', tex)


LATEX_ENV_TOKEN_PATTERN = re.compile(
    r"\\(?P<kind>begin|end)\s*\{\s*(?P<name>[^{}\s]+)\s*\}"
)


def get_latex_environment_tokens(tex: str):
    """返回去除注释后的环境标记，并忽略代码环境内部的伪标记。

    返回值为 ``(cleaned_tex, tokens)``，其中 ``tokens`` 是按源码顺序排列的
    ``re.Match`` 对象。代码环境只保留自身的 begin/end，正文里的命令不会被
    当成外层文档结构。
    """
    cleaned_tex = remove_comments(tex)
    # 行内代码也可能包含 \begin/\end 的示例文字。
    inline_spans = [
        (start, end)
        for start, end, name in get_latex_opaque_fragments(cleaned_tex)
        if name not in LATEX_OPAQUE_ENVIRONMENTS
    ]
    tokens = []
    opaque_environment = None

    for match in LATEX_ENV_TOKEN_PATTERN.finditer(cleaned_tex):
        if any(start <= match.start() < end for start, end in inline_spans):
            continue
        kind = match.group("kind")
        name = match.group("name")

        if opaque_environment is not None:
            if kind == "end" and name == opaque_environment:
                tokens.append(match)
                opaque_environment = None
            continue

        tokens.append(match)
        if kind == "begin" and name in LATEX_OPAQUE_ENVIRONMENTS:
            opaque_environment = name

    return cleaned_tex, tokens


LATEX_LIST_ENVIRONMENTS = frozenset(
    {
        "enumerate",
        "enumerate*",
        "itemize",
        "itemize*",
        "description",
        "description*",
        "list",
        "trivlist",
        "thebibliography",
        # threeparttable 的表注环境内部也通过 \item 声明每条注释。
        "tablenotes",
    }
)


_LATEX_REFERENCE_ARGUMENT_PATTERN = re.compile(
    r"\\(?P<command>"
    r"label|ref|pageref|autoref|nameref|eqref|vref|Vref|fref|Fref|sref|Sref|"
    r"cref|Cref|crefrange|Crefrange|"
    r"cpageref|Cpageref|cpagerefrange|Cpagerefrange|cite[a-zA-Z]*"
    r")"
    r"(?P<star>\*?)"
    r"(?P<spacing>[ \t]*)"
    r"(?P<option>\[[^\]\r\n]*\][ \t]*)?"
    r"\{(?P<argument>[^{}\r\n]*)\}"
)
_LATEX_HYPERREF_OPTION_PATTERN = re.compile(
    r"\\hyperref(?P<star>\*?)(?P<spacing>[ \t]*)"
    r"\[(?P<argument>[^\]\r\n]*)\]"
)


def normalize_latex_reference_arguments(latex_code: str) -> str:
    r"""修复引用/标签参数中被错误转义的下划线。

    LaTeX 翻译模型有时会把 ``\cref{tab:multi_token}`` 输出为
    ``\cref{tab:multi\_token}``。正文中 ``\_`` 是合法的下划线写法，
    但 ``cleveref`` 会把引用标签放入内部 ``\csname``，其中的
    ``\_`` 会展开为受保护命令并触发 ``Missing \\endcsname inserted``。
    因此这里只处理引用、标签和 BibTeX 键的参数，不触碰正文或 URL。
    """
    if not latex_code:
        return latex_code
    if get_latex_opaque_fragments(latex_code):
        return _transform_preserving_opaque_content(
            latex_code, normalize_latex_reference_arguments
        )

    def normalize_identifier(identifier: str) -> str:
        return re.sub(r"(?<!\\)\\_", "_", identifier)

    def replace_argument(match: re.Match) -> str:
        argument = match.group("argument")
        # 标签和 BibTeX 键应使用原始下划线；限制在命令参数内，避免
        # 把正文中用于排版的 ``\_`` 改坏。
        argument = normalize_identifier(argument)
        return (
            f"\\{match.group('command')}"
            f"{match.group('star')}"
            f"{match.group('spacing')}"
            f"{match.group('option') or ''}"
            f"{{{argument}}}"
        )

    latex_code = _LATEX_REFERENCE_ARGUMENT_PATTERN.sub(replace_argument, latex_code)

    def replace_hyperref_option(match: re.Match) -> str:
        argument = normalize_identifier(match.group("argument"))
        return (
            r"\hyperref"
            f"{match.group('star')}"
            f"{match.group('spacing')}"
            f"[{argument}]"
        )

    return _LATEX_HYPERREF_OPTION_PATTERN.sub(replace_hyperref_option, latex_code)


def _collect_latex_structure_issues(tex: str) -> List[Tuple[Tuple[str, ...], int, str]]:
    """返回 ``(与行号无关的问题签名, 行号, 错误描述)`` 列表。"""
    cleaned_tex, environment_tokens = get_latex_environment_tokens(tex)
    item_tokens = list(re.finditer(
        r"\\item(?![A-Za-z@])", mask_latex_opaque_content(cleaned_tex)
    ))
    events = [
        (token.start(), 0, "environment", token)
        for token in environment_tokens
    ]
    events.extend(
        (token.start(), 1, "item", token)
        for token in item_tokens
    )
    events.sort(key=lambda event: (event[0], event[1]))

    stack = []
    issues = []

    def line_number(position: int) -> int:
        return cleaned_tex.count("\n", 0, position) + 1

    for position, _, event_type, token in events:
        line = line_number(position)

        if event_type == "item":
            # 代码环境中的 \\item 只是文本，不应当触发列表结构错误。
            if stack and stack[-1][0] in LATEX_OPAQUE_ENVIRONMENTS:
                continue
            if not any(name in LATEX_LIST_ENVIRONMENTS for name, _ in stack):
                active = ", ".join(name for name, _ in stack) or "(none)"
                issues.append((
                    ("item", active), line,
                    f"第 {line} 行：\\item 位于列表环境之外；当前环境栈：{active}。",
                ))
            continue

        kind = token.group("kind")
        name = token.group("name")
        if kind == "begin":
            stack.append((name, line))
            continue

        if not stack:
            issues.append((
                ("unmatched_end", name), line,
                f"第 {line} 行：未匹配的 \\end{{{name}}}。",
            ))
            continue

        if stack[-1][0] != name:
            expected = stack[-1][0]
            issues.append((
                ("order", name, expected), line,
                f"第 {line} 行：环境结束顺序错误，遇到 \\end{{{name}}}，"
                f"但当前应结束 \\end{{{expected}}}。",
            ))
            continue

        stack.pop()

    for name, begin_line in reversed(stack):
        issues.append((
            ("unclosed", name), begin_line,
            f"第 {begin_line} 行：\\begin{{{name}}} 没有对应的 \\end{{{name}}}。",
        ))

    return issues


def validate_latex_structure(tex: str, source_tex: Optional[str] = None) -> List[str]:
    r"""检查最终 TeX 的环境配对以及 ``\item`` 所在的列表环境。

    该检查必须在所有 section/environment/caption/input placeholder 完成替换
    后运行，因为单独检查各个片段无法发现跨 placeholder 边界的结构错误。

    传入 ``source_tex`` 时只报告译文新引入的问题。源文档中本就存在的
    “伪不配对”标记（例如 ``\patchcmd{..}{\begin{tcolorbox}}{..}`` 或在宏
    定义里开启、在正文里关闭的环境）能被 TeX 正常编译，不应阻止生成。
    """
    issues = _collect_latex_structure_issues(tex)
    if source_tex is None or not issues:
        return [message for _, _, message in issues]

    baseline = Counter(
        signature for signature, _, _ in _collect_latex_structure_issues(source_tex)
    )
    # 按行号从前往后抵消：导言区等未翻译部分在源文与译文中位置一致，
    # 这样剩下的错误才会指向译文真正出问题的位置。
    tolerated = set()
    for index, (signature, _, _) in sorted(
        enumerate(issues), key=lambda item: item[1][1]
    ):
        if baseline[signature] > 0:
            baseline[signature] -= 1
            tolerated.add(index)
    return [
        message
        for index, (_, _, message) in enumerate(issues)
        if index not in tolerated
    ]


def get_env_pattern(command_name):
    """
    Get the regex pattern for matching environments.
    """
    get_command_env = lambda name: rf"\\begin{spaces}\{{(?!document\b|center\b|proof\b|multicols\b)({name})\}}{spaces}({options})?(.*?)\\end{spaces}\{{\1\}}"
    command_env = get_command_env(command_name)
    env_pattern = regex.compile(command_env, regex.DOTALL)
    return env_pattern

def get_abstract_pattern():
    r"""
    Get the regex pattern for matching \begin{abstract} and \end{abstract} commands.
    """
    command_name = r'abstract'
    get_command_env = lambda name: rf"\\begin{spaces}\{{({name})\}}{spaces}({options})?(.*?)\\end{spaces}\{{\1\}}"
    command_abstract = get_command_env(command_name)
    abstract_pattern = regex.compile(command_abstract, regex.DOTALL)
    return abstract_pattern

def get_keywords_pattern():
    r"""
    Get the regex pattern for matching \keywords commands.
    """
    command_name = r'keywords'
    command = get_pattern_command_full(command_name)
    keywords_pattern = regex.compile(command, regex.DOTALL)
    return keywords_pattern

def get_section_pattern():
    """
    Get the regex pattern for matching section commands.
    """
    command_name = r'section|subsection|subsubsection' # add chapter
    command = get_pattern_command_full(command_name)
    section_pattern = regex.compile(command, regex.DOTALL)
    return section_pattern

def get_begin_document_pattern():
    """
    Get the regex pattern for matching \begin{document} command.
    """
    pattern = regex.compile(r'\\begin\s*\{\s*document\s*\}', regex.DOTALL)
    return pattern

def get_newcommand_pattern():
    """
    Get the regex pattern for matching \newcommand commands.
    """
    newcommand = rf'\\(?:newcommand\*?|def|renewcommand|newenvironment|renewenvironment){spaces}(?:\{{\\([a-zA-Z]+)\}}|\\([a-zA-Z]+)){spaces}(?:\[(\d)\])?{spaces}({get_pattern_brace(4)})'  # \newcommand{name}[n_arguments]{content}, group 1/2: name, group 3: n_arguments, group 5: content
    newcommand_pattern = regex.compile(newcommand, regex.DOTALL)
    return newcommand_pattern

def get_latex_macro_definitions(tex: str) -> List[Tuple[int, int, str]]:
    """定位完整宏/环境定义，包括默认参数、TeX def 参数和第二段环境定义。"""
    starts = re.compile(
        r"\\(?P<kind>newcommand|renewcommand|providecommand|DeclareRobustCommand|"
        r"newenvironment|renewenvironment|def|gdef|edef|xdef)\*?(?![A-Za-z@])"
    )
    definitions = []
    position = 0
    searchable = mask_latex_opaque_content(tex)
    while True:
        match = starts.search(searchable, position)
        if match is None:
            break
        index = match.end()
        while index < len(tex) and tex[index].isspace():
            index += 1
        is_environment = match.group("kind").endswith("environment")
        if index < len(tex) and tex[index] == "{":
            name_end = _find_balanced_group_end(tex, index, "{", "}")
            if name_end is None:
                position = match.end()
                continue
            name = tex[index + 1:name_end - 1].strip().lstrip("\\")
            index = name_end
        else:
            name_match = _LATEX_CONTROL_SEQUENCE_PATTERN.match(tex, index)
            if name_match is None or is_environment:
                position = match.end()
                continue
            name = _command_name(name_match.group(0))
            index = name_match.end()
        if match.group("kind") in {"def", "gdef", "edef", "xdef"}:
            # 参数规格 #1/#2 等不是正文，保留到第一个定义体之前。
            body_start = tex.find("{", index)
            if body_start == -1:
                position = match.end()
                continue
            index = body_start
        else:
            for _ in range(2):
                while index < len(tex) and tex[index].isspace():
                    index += 1
                if index >= len(tex) or tex[index] != "[":
                    break
                option_end = _find_balanced_group_end(tex, index, "[", "]")
                if option_end is None:
                    break
                index = option_end
        complete = True
        for _ in range(2 if is_environment else 1):
            while index < len(tex) and tex[index].isspace():
                index += 1
            body_end = _find_balanced_group_end(tex, index, "{", "}")
            if body_end is None:
                complete = False
                break
            index = body_end
        if complete:
            definitions.append((match.start(), index, name))
            position = index
        else:
            position = match.end()
    return definitions


def get_command_pattern(name):
    """
    Get the regex pattern for matching LaTeX commands.
    """
    command = get_pattern_command_full(name)
    command_pattern = regex.compile(command, regex.DOTALL)
    return command_pattern

def get_captionof_pattern():
    r"""
    Match \captionof{env}{text} structure using regex with support for nested braces.
    """
    pattern = regex.compile(r"""
        (?(DEFINE)(?P<brace>\{(?:[^{}\\]++|\\[\s\S]|(?&brace))*+\}))
        \\(?P<command>captionof\*?)(?![A-Za-z@])
        \s*
        (?P<type>(?&brace))
        \s*(?:\[[^\[\]]*\]\s*)?
        (?P<text>(?&brace))
    """, regex.VERBOSE | regex.DOTALL)
    return pattern

def _documentclass_name(latex_code: str) -> str:
    """提取文档类名称，用于选择兼容的中文排版方案。"""
    match = re.search(
        r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}",
        latex_code,
    )
    return match.group(1).strip() if match else ""


def _has_latex_package(latex_code: str, package_name: str) -> bool:
    """检查导言区中是否已经加载指定宏包。"""
    pattern = re.compile(
        rf"\\(?:usepackage|RequirePackage)(?:\s*\[[^\]]*\])?\s*"
        rf"\{{[^}}]*\b{re.escape(package_name)}\b[^}}]*\}}",
        re.IGNORECASE,
    )
    return bool(pattern.search(latex_code))


def _remove_standalone_latex_package(latex_code: str, package_name: str) -> str:
    """移除单独占一行的宏包声明，保留其它导言内容。"""
    pattern = re.compile(
        rf"^[ \t]*\\(?:usepackage|RequirePackage)"
        rf"(?:\s*\[[^\]\r\n]*\])?\s*\{{\s*{re.escape(package_name)}\s*\}}"
        rf"[ \t]*(?:%[^\r\n]*)?(?:\r?\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    return pattern.sub("", latex_code)


def _insert_after_documentclass(latex_code: str, package_block: str) -> str:
    """将宏包块插入第一个 documentclass 声明之后。"""
    pattern = re.compile(
        r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{[^{}]+\}",
        re.DOTALL,
    )
    match = pattern.search(latex_code)
    if not match:
        return latex_code
    position = match.end()
    return latex_code[:position] + "\n" + package_block + "\n" + latex_code[position:]


def _remove_pdfoutput_directives(latex_code: str) -> str:
    """移除 XeTeX/LuaTeX 不支持的活动 ``\\pdfoutput`` 赋值。"""
    # ``\\pdfoutput`` 是 pdfTeX 原语，arXiv 源码经常用
    # ``\\pdfoutput=1`` 强制走 PDF 输出。中文翻译会切换到 XeLaTeX，
    # 此时该原语不存在，必须只删除行首的活动指令；注释和正文中的
    # 示例代码不应被误改。
    pattern = re.compile(
        r"^[ \t]*\\pdfoutput\s*=\s*[+-]?\d+[ \t]*(?:\\relax[ \t]*)?",
        re.IGNORECASE | re.MULTILINE,
    )
    return pattern.sub("", latex_code)


CHINESE_TARGET_LANGUAGES = {"ch", "zh", "cn", "zh-cn", "zh_cn", "zh-hans", "chinese"}
JAPANESE_TARGET_LANGUAGES = {"ja", "jp", "ja-jp", "ja_jp", "japanese"}


def target_language_family(target_language: Optional[str]) -> str:
    """把目标语言代码归类为 "ch"、"ja" 或 "other"。"""
    code = str(target_language or "ch").strip().lower()
    if code in CHINESE_TARGET_LANGUAGES:
        return "ch"
    if code in JAPANESE_TARGET_LANGUAGES:
        return "ja"
    return "other"


def _font_fallback_chain(command: str, fonts: List[str]) -> str:
    """生成 \\IfFontExistsTF 级联：依次尝试候选字体，全部缺失时保留 xeCJK 默认字体。"""
    chain = "{}"
    for font in reversed(fonts):
        chain = rf"{{\IfFontExistsTF{{{font}}}{{{command}{{{font}}}}}{chain}}}"
    return chain[1:-1]


# 日文字形：优先 Noto CJK JP，其次 TeX Live 自带的 IPAex，再次 Windows 自带字体；
# 都不可用时退回 xeCJK 默认的 Fandol（含假名，但汉字为中文字形）。
JAPANESE_CJK_FONT_LINES = [
    _font_fallback_chain(
        r"\setCJKmainfont[AutoFakeBold]",
        ["Noto Serif CJK JP", "IPAexMincho", "Yu Mincho", "MS Mincho"],
    ),
    _font_fallback_chain(
        r"\setCJKsansfont[AutoFakeBold]",
        ["Noto Sans CJK JP", "IPAexGothic", "Yu Gothic", "MS Gothic"],
    ),
]


def add_chinese_package(latex_code: str) -> str:
    """
    为翻译后的中文文档添加 Unicode 中文支持。

    acmart 会检查 baselinestretch，而 ctex 会修改它；两者组合会在文档
    结束时触发 acmart 的硬错误。因此这里使用 XeLaTeX + xeCJK，并把
    旧式字体声明转换为原生 Unicode 字体配置。
    """
    return _add_xecjk_support(latex_code)


def add_target_language_package(latex_code: str, target_language: Optional[str] = "ch") -> str:
    """按目标语言注入排版支持：中文/日文走 XeLaTeX + xeCJK，其它语言保持原样。"""
    family = target_language_family(target_language)
    if family == "ch":
        return add_chinese_package(latex_code)
    if family == "ja":
        return add_ja_package(latex_code)
    return latex_code


def _add_xecjk_support(latex_code: str, cjk_font_lines: Optional[List[str]] = None) -> str:
    """中文与日文共用的 XeLaTeX + xeCJK 导言区处理；cjk_font_lines 为额外的 CJK 字体设置。"""
    if get_latex_opaque_fragments(latex_code):
        return _transform_preserving_opaque_content(
            latex_code, lambda text: _add_xecjk_support(text, cjk_font_lines)
        )
    # 中文输出统一使用 Unicode 引擎，因此先清理原稿中只属于 pdfTeX
    # 的输出模式声明；这一步必须位于所有提前返回分支之前。
    latex_code = _remove_pdfoutput_directives(latex_code)

    documentclass = _documentclass_name(latex_code)
    is_acmart = any(name.strip().lower() == "acmart" for name in documentclass.split(","))

    # 翻译流程曾经自动插入 ctex。对 acmart 必须移除该声明，否则即使改用
    # XeLaTeX 也会触发 acmart 对 baselinestretch 的检查。
    if is_acmart:
        latex_code = _remove_standalone_latex_package(latex_code, "ctex")

    has_xecjk = _has_latex_package(latex_code, "xeCJK")

    # 非 acmart 文档若已有 ctex，保留原作者的排版配置；编译器会自动选择
    # Unicode 引擎。acmart 则必须走上面的 xeCJK 路径。
    if not has_xecjk and not is_acmart and _has_latex_package(latex_code, "ctex"):
        return latex_code

    # XeLaTeX 使用 Unicode 字体编码。旧式 inputenc/fontenc 不再需要，
    # 而 times/mathptmx 会把正文映射到 Type1 字体，触发 XeTeX 下的
    # microtype 字符 protrusion 错误。
    has_legacy_text_font = any(
        _has_latex_package(latex_code, package_name)
        for package_name in ("times", "mathptmx")
    )
    has_legacy_mono_font = _has_latex_package(latex_code, "inconsolata")
    has_nvidia_ttf = (
        "nvidiatechreport" in documentclass.lower()
        or (
            "NVIDIA-Sans-Font-TTF" in latex_code
            and "NVIDIASans_Rg.ttf" in latex_code
            and "NVIDIASans_Bd.ttf" in latex_code
        )
    )
    had_cjk_package = any(
        _has_latex_package(latex_code, package_name)
        for package_name in ("CJKutf8", "CJK")
    )
    for package_name in ("inputenc", "fontenc", "CJKutf8", "CJK"):
        latex_code = _remove_standalone_latex_package(latex_code, package_name)
    if has_legacy_text_font:
        for package_name in ("times", "mathptmx"):
            latex_code = _remove_standalone_latex_package(latex_code, package_name)
    if has_legacy_mono_font:
        latex_code = _remove_standalone_latex_package(latex_code, "inconsolata")

    package_lines = [
        # 某些模板会在 documentclass 内部提前加载 microtype，
        # PassOptionsToPackage 此时已经来不及生效。XeLaTeX 处理模板自带的
        # Type1 字体时，microtype 的 XeTeXglyph 路径会直接触发编译错误，
        # 因此在宏包已加载时关闭全部微排版功能。
        r"\ifcsname microtypesetup\endcsname",
        r"\microtypesetup{activate=false}",
        r"\fi",
    ]
    if not has_xecjk:
        package_lines.append(r"\usepackage{xeCJK}")
        # 原稿已自带 xeCJK 时尊重作者的字体配置，只在我们加载 xeCJK 后设置字体。
        package_lines.extend(cjk_font_lines or [])
    if had_cjk_package:
        # CJK/CJKutf8 只适用于 pdfTeX，已被移除；原文正文中的
        # \begin{CJK}{UTF8}{gkai} 由 xeCJK 直接排版，这里提供空环境
        # 兼容。放在 \AtBeginDocument 中，避免与之后加载的宏包冲突。
        package_lines.extend(
            [
                r"\makeatletter",
                r"\AtBeginDocument{%",
                r"  \@ifundefined{CJK}{\newenvironment{CJK}[2]{}{}}{}%",
                r"  \@ifundefined{CJK*}{\newenvironment{CJK*}[2]{}{}}{}%",
                r"  \providecommand{\CJKfamily}[1]{}%",
                r"}",
                r"\makeatother",
            ]
        )
    # inputenc 的字符映射命令在 XeLaTeX 下不存在。保留原作者的替换
    # 内容（包括数学符号），用支持 Unicode 引擎的 newunicodechar 注册。
    unicode_declaration = re.compile(
        r"(?m)^([ \t]*)\\DeclareUnicodeCharacter\s*\{([0-9A-Fa-f]{4,6})\}"
    )
    if unicode_declaration.search(latex_code):
        package_lines.append(r"\usepackage{newunicodechar}")
        latex_code = unicode_declaration.sub(
            lambda match: match.group(1) + r"\newunicodechar{" + chr(int(match.group(2), 16)) + "}",
            latex_code,
        )
    if has_legacy_text_font and not has_xecjk:
        package_lines.append(
            r"\IfFontExistsTF{TeX Gyre Termes}{\setmainfont{TeX Gyre Termes}}{}"
        )
    if has_legacy_mono_font and not has_xecjk:
        # 原始稿件可能声明 inconsolata，但它不一定安装为 fontspec 可用的
        # 系统字体。直接使用 TeX Live 自带的 Latin Modern Mono，避免
        # fontspec/kpathsea 触发 mktextfm 去生成不存在的 Inconsolata.mf。
        package_lines.append(r"\setmonofont{Latin Modern Mono}")
    if has_nvidia_ttf:
        # nvidiatechreport.cls 通过 T1 FD 和 pdfmapline 注册旧式 Type1 字体，
        # XeLaTeX 在生成 XDV 后交给 xdvipdfmx 时可能找不到对应 TFM。源码包
        # 已经提供同一套 TTF，直接注册 native 字体可保留原版字形并绕过该映射。
        package_lines.extend(
            [
                r"\setmainfont[",
                r"  Path = NVIDIA-Sans-Font-TTF/,",
                r"  Extension = .ttf,",
                r"  UprightFont = NVIDIASans_Rg,",
                r"  BoldFont = NVIDIASans_Bd,",
                r"  ItalicFont = NVIDIASans_It,",
                r"  BoldItalicFont = NVIDIASans_BdIt",
                r"]{NVIDIASans}",
                r"\setsansfont[",
                r"  Path = NVIDIA-Sans-Font-TTF/,",
                r"  Extension = .ttf,",
                r"  UprightFont = NVIDIASans_Rg,",
                r"  BoldFont = NVIDIASans_Bd,",
                r"  ItalicFont = NVIDIASans_It,",
                r"  BoldItalicFont = NVIDIASans_BdIt",
                r"]{NVIDIASans}",
            ]
        )
    # 不主动注入 SimSun/SimHei。Windows 上的 SimSun 通常位于 TTC 容器中，
    # 而某些论文模板（例如 XCharter）会把 fontspec 的全局扩展名固定为
    # .otf，导致“探测到了 SimSun”但实际被拼成 SimSun.otf 后加载失败。
    # xeCJK 自带的配置会使用 TeX Live 内置 Fandol 字体（显式 .otf），
    # 不依赖用户机器上的 Windows 字体，也不会与模板的字体选项冲突。
    package_block = "\n".join(package_lines)
    return _insert_after_documentclass(latex_code, package_block)


_XETEX_UNSUPPORTED_LIGATURE_LINE = re.compile(
    r"^(?P<indent>[ \t]*)\\DisableLigatures\b"
    r"(?P<body>[^\r\n]*)(?P<newline>\r?\n|$)",
    re.MULTILINE,
)


def patch_xelatex_compatibility_files(project_dir: str) -> List[str]:
    """Disable pdfTeX-only ligature directives in local class/style files.

    Some arXiv templates put ``\\DisableLigatures`` in a ``.cls`` file.  The
    command is accepted by pdfTeX but microtype raises a fatal error when the
    same class is loaded by XeLaTeX, which is required for translated Chinese
    output.  The main document cannot override a command executed while
    ``\\documentclass`` is loading, so patch the copied project-local support
    files before compilation.  Only active, whole-line directives are touched;
    comments and ordinary document content remain unchanged.
    """
    root = Path(project_dir)
    if not root.is_dir():
        return []

    patched_files: List[str] = []
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.suffix.lower() not in {".cls", ".sty"}
            or any(part.startswith("build_") for part in path.parts)
        ):
            continue

        try:
            with path.open("r", encoding="utf-8", newline="") as source_file:
                original = source_file.read()
        except (OSError, UnicodeDecodeError):
            continue

        def replace_directive(match: re.Match[str]) -> str:
            if any(start <= match.start() < end for start, end, _ in opaque_fragments):
                return match.group(0)
            return (
                f"{match.group('indent')}% LaTeXTrans: disabled for XeLaTeX: "
                f"\\DisableLigatures{match.group('body')}"
                f"{match.group('newline')}"
            )

        opaque_fragments = get_latex_opaque_fragments(original)
        patched = _XETEX_UNSUPPORTED_LIGATURE_LINE.sub(
            replace_directive,
            original,
        )
        if patched == original:
            continue

        try:
            with path.open("w", encoding="utf-8", newline="") as target_file:
                target_file.write(patched)
        except OSError:
            continue
        patched_files.append(str(path))

    return patched_files


def add_ctex_package(latex_code: str) -> str:
    """兼容旧调用方，实际使用不修改 acmart 行距的中文支持方案。"""
    return add_chinese_package(latex_code)

def add_ja_package(latex_code: str) -> str:
    """
    为翻译后的日文文档添加支持。

    原稿已使用 luatexja 时保持不变（编译器会自动选择 LuaLaTeX）；否则与中文
    一致走 XeLaTeX + xeCJK（兼容 acmart、清理 pdfTeX 专用声明），并优先选用
    日文字形字体。
    """
    if get_latex_opaque_fragments(latex_code):
        return _transform_preserving_opaque_content(latex_code, add_ja_package)
    if _has_latex_package(latex_code, "luatexja"):
        return latex_code
    return _add_xecjk_support(latex_code, cjk_font_lines=JAPANESE_CJK_FONT_LINES)

MAIN_TEX_MARKER = ".latextrans_main"


def record_main_tex_file(dir, main_file_path) -> None:
    """记录重建时写入的主文件。

    译文比原文短，按长度打分可能在翻译后误选其他带 \\documentclass 的文件，
    编译与标题提取都应沿用重建时的选择。
    """
    try:
        relative = os.path.relpath(main_file_path, dir)
        Path(dir, MAIN_TEX_MARKER).write_text(relative, encoding="utf-8")
    except (OSError, ValueError):
        pass


def find_main_tex_file(dir):
    """
    Find the main LaTeX file in the given directory.
    """
    marker_path = os.path.join(dir, MAIN_TEX_MARKER)
    if os.path.isfile(marker_path):
        try:
            recorded = os.path.join(dir, Path(marker_path).read_text(encoding="utf-8").strip())
        except OSError:
            recorded = ""
        if recorded:
            recorded = project_path(dir, recorded)
            if recorded.suffix.lower() == ".tex" and recorded.is_file():
                return os.fspath(recorded)

    readme_path = os.path.join(dir, '00README.json')
    if os.path.isfile(readme_path):
        config = read_json_file(readme_path)
        for source in config.get("sources", []):
            if source.get("usage") == "toplevel":
                main_file_name = source.get("filename")
                if not isinstance(main_file_name, str) or not main_file_name.strip():
                    continue
                main_file_path = os.path.join(dir, main_file_name)
                main_file_path = project_path(dir, main_file_path)
                if main_file_path.suffix.lower() == ".tex" and main_file_path.is_file():
                    return os.fspath(main_file_path)

    tex_files = find_tex_files(dir)
    documentclass_pattern = re.compile(
        r"\\document(?:class|style)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", re.DOTALL
    )
    preferred_names = {"main", "ms", "paper", "article", "manuscript", "root"}

    candidates = []
    for tex_file in sorted(tex_files, key=lambda path: str(path).casefold()):
        tex_file = os.fspath(project_path(dir, tex_file))
        try:
            latex_code = read_tex_file(tex_file)
        except OSError:
            continue
        latex_code = mask_latex_opaque_content(remove_comments(latex_code))

        match = documentclass_pattern.search(latex_code)
        if not match:
            continue
        class_name = match.group(1).strip().lower()
        stem = Path(tex_file).stem.lower()
        supplementary = bool(re.search(
            r"(?:^|[_\-.])(?:supp(?:lement(?:ary|al)?)?|appendix|rebuttal|response|si)"
            r"(?:$|[_\-.0-9])", stem
        ))
        score = (
            # 含 \begin{document} 的才是可编译主文件。
            int(bool(re.search(r"\\begin\s*\{document\}", latex_code))),
            # standalone 通常是单独的图片文件。
            int(class_name != "standalone"),
            int(not supplementary),
            # 嵌套副本可能比主稿更长；先选浅层，再看命名和正文分数。
            -len(Path(os.path.relpath(tex_file, dir)).parts),
            int(stem in preferred_names),
            int(bool(re.search(r"\\(?:maketitle|icmltitle|title)\b", latex_code))),
            len(re.findall(r"\\(?:input|include|section)\b", latex_code)),
            len(latex_code),
        )
        candidates.append((score, tex_file))

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None

def merge_tex_from_inputs(main_file_path):

    if main_file_path is None:
        return None
    dirname = os.path.dirname(main_file_path)
    maincontent = read_tex_file(main_file_path)
    maincontent = remove_comments(maincontent) 
    pattern_input = re.compile(r'\\(input|include){(.*?)}')
    while True:
        result = pattern_input.search(maincontent)
        if result is None:
            break
        begin, end = result.span()
        match = result.group(2)
        inputfilepath = resolve_tex_input_file(dirname, match)
        if inputfilepath is None:
            raise FileNotFoundError(
                f"File not found (or path is a directory): {os.path.join(dirname, match)}"
            )
        input_tex = read_tex_file(inputfilepath)
        input_tex = remove_comments(input_tex)
        maincontent = maincontent[:begin] + input_tex + maincontent[end:]
        # print('merging', inputfilepath)

    return maincontent

def save_to_tex(data, output_file):
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(data)

def save_to_json(data, output_file):
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def compile_with_latexmk(tex_file: str, out_dir: str = "out", engine: str = "pdflatex"):
    """使用指定引擎严格编译单个 TeX 文件，并返回本次生成的 PDF。"""
    tex_path = Path(tex_file).resolve()
    output_path = Path(out_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    expected_pdf = output_path / f"{tex_path.stem}.pdf"

    if expected_pdf.exists():
        expected_pdf.unlink()

    command = [
        "latexmk",
        f"-{engine}",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-synctex=1",
        "-gg",
        f"-outdir={output_path}",
        str(tex_path),
    ]

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(tex_path.parent),
        )
    except OSError as exc:
        print(f"❌ 无法启动 {engine}：{exc}")
        return None

    if completed.returncode != 0:
        print(f"❌ {engine} 返回非零状态码 {completed.returncode}。")
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if output:
            print(output[-3000:])
        if expected_pdf.exists():
            expected_pdf.unlink()
        return None

    if not expected_pdf.is_file() or expected_pdf.stat().st_size == 0:
        print(f"❌ {engine} 返回成功但没有生成有效 PDF：{expected_pdf}")
        return None

    print(f"✅ 编译成功：{expected_pdf}")
    return str(expected_pdf)

def collect_latex_errors_with_logpath(folder: str):
    """
    遍历每个项目，读取最近一次编译的 .log 文件，统计真正的 LaTeX 错误。

    旧实现只搜索字面量 ``LaTeX Error``，会漏掉 ``Class acmart Error``、
    ``Package ... Error`` 和 TeX 引擎以 ``!`` 开头报告的致命错误。
    仅将包含错误的项目记录到 JSON 文件中，并输出错误项目总数。
    """
    error_patterns = [
        re.compile(r"^![ \t]*", re.MULTILINE),
        re.compile(
            r"(?:LaTeX|Package|Class)\s+.*?\bError\s*:",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:LuaTeX|XeTeX|pdfTeX)\s+.*?\berror\s*:",
            re.IGNORECASE,
        ),
        re.compile(r"Emergency stop|Fatal error", re.IGNORECASE),
    ]
    normalized_error_pattern = re.compile(
        r"(?:LaTeX|Package|Class)\s+[^.!?]{0,200}?\bError\s*:",
        re.IGNORECASE,
    )
    summary = {}
    error_project_count = 0

    for project_name in os.listdir(folder):
        project_path = os.path.join(folder, project_name)
        if not os.path.isdir(project_path):
            continue

        build_dirs = ["build_xelatex", "build_lualatex", "build_pdflatex", "build"]
        log_paths = []
        for build_dir in build_dirs:
            candidate = os.path.join(project_path, build_dir)
            if not os.path.isdir(candidate):
                continue
            log_paths.extend(
                os.path.join(candidate, file_name)
                for file_name in os.listdir(candidate)
                if file_name.lower().endswith(".log")
            )

        if not log_paths:
            continue  # 没有找到编译日志

        # 同一项目可能保留多个引擎的日志，按修改时间选择本次最新的日志。
        log_path = max(log_paths, key=os.path.getmtime)
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                error_lines = []
                seen_lines = set()

                def add_error_line(line: str) -> None:
                    normalized_line = line.strip()
                    if normalized_line and normalized_line not in seen_lines:
                        seen_lines.add(normalized_line)
                        error_lines.append(normalized_line)

                for line in content.splitlines():
                    if any(pattern.search(line) for pattern in error_patterns):
                        add_error_line(line)

                # TeX 可能把 ``Class acmart Error`` 拆成 ``Class ac`` 和
                # ``mart Error`` 两行；去掉换行后再匹配一次，避免漏报。
                normalized_content = re.sub(r"\s+", " ", content)
                for match in normalized_error_pattern.finditer(normalized_content):
                    add_error_line(match.group(0))
        except Exception as e:
            print(f"Error reading {log_path}: {e}")
            continue

        error_count = len(error_lines)
        if error_count > 0:
            summary[project_name] = {
                "total_errors": error_count,
                "log_path": log_path,
                "errors": error_lines[:10],
            }
            error_project_count += 1

    # 写入 JSON 文件（仅包含有错误的项目）
    output_path = os.path.join(folder, "latex_error_summary.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"Summary saved to: {output_path}")
    print(f"🔍 共有 {error_project_count} 个项目存在 LaTeX Error。")

def get_tex_url(arxiv_id: str, headers: dict) -> str:
    """
    获取 TeX 源码下载链接
    """
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        resp = requests.get(abs_url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return ""
    
    soup = BeautifulSoup(resp.text, "html.parser")
    link = soup.find("a", class_="abs-button download-eprint")
    if link and link.get("href"):
        return f"https://arxiv.org{link['href']}"
    return ""

def is_already_downloaded(arxiv_id: str, save_dir: str) -> bool:
    """
    检查 TeX 源码是否已经完整下载或完整解压。
    """
    tar_path = os.path.join(save_dir, f"{arxiv_id}.tar.gz")
    extracted_dir = os.path.join(save_dir, arxiv_id)
    invalid_archive_removed = False

    if os.path.exists(tar_path):
        if _is_complete_archive(tar_path):
            return True
        print(f"[INFO] Incomplete archive detected, redownloading: {tar_path}")
        os.remove(tar_path)
        invalid_archive_removed = True

    if os.path.isdir(extracted_dir):
        has_tex_sources = _directory_has_tex_sources(extracted_dir)
        if _has_download_complete_marker(extracted_dir) and has_tex_sources:
            return True
        if has_tex_sources and not invalid_archive_removed:
            # 兼容旧版本已经解压但没有完成标记的目录。
            return True
        print(f"[INFO] Existing source directory is incomplete, redownloading: {extracted_dir}")

    return False

# 首个请求只取前 4MB，并由 Content-Range 得知总大小；更大的文件剩余部分分段并发下载。
# 实测单连接吞吐受限，4 路以上并发可把 30MB 源码的下载时间缩短一半以上。
RANGE_FIRST_CHUNK = 4 * 1024 * 1024
RANGE_PARALLEL_PARTS = 6


def download_file_ranged(url: str, target_path: str, headers: dict, on_progress=None) -> int:
    """下载到 ``target_path``，返回字节数；服务器支持 Range 时并发分段下载。"""
    from concurrent.futures import ThreadPoolExecutor

    first_headers = {**headers, "Range": f"bytes=0-{RANGE_FIRST_CHUNK - 1}"}
    with requests.get(url, headers=first_headers, stream=True, timeout=20) as r:
        r.raise_for_status()
        total = None
        if r.status_code == 206:
            match = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
            total = int(match.group(1)) if match else None
        expected_first = int(r.headers.get("Content-Length", 0))
        with open(target_path, "wb") as f:
            written = 0
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
                written += len(chunk)
        if expected_first and written != expected_first:
            raise IOError(f"Incomplete download: expected {expected_first} bytes, got {written} bytes")
    if on_progress:
        on_progress(written, total or written)
    if not total or total <= written:
        return written

    remaining = total - written
    step = -(-remaining // RANGE_PARALLEL_PARTS)
    ranges = [(start, min(total, start + step) - 1) for start in range(written, total, step)]
    with open(target_path, "r+b") as f:
        f.truncate(total)

    done = [written]
    lock = threading.Lock()

    def fetch(byte_range):
        start, end = byte_range
        with requests.get(url, headers={**headers, "Range": f"bytes={start}-{end}"}, stream=True, timeout=20) as part:
            part.raise_for_status()
            if part.status_code != 206:
                raise IOError("Server ignored the Range request")
            offset = start
            with open(target_path, "r+b") as f:
                f.seek(start)
                for chunk in part.iter_content(1024 * 256):
                    f.write(chunk)
                    offset += len(chunk)
                    with lock:
                        done[0] += len(chunk)
                        current = done[0]
                    if on_progress:
                        on_progress(current, total)
        if offset != end + 1:
            raise IOError(f"Incomplete range {start}-{end}: got {offset - start} bytes")

    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        list(pool.map(fetch, ranges))
    return total


def download_tex(arxiv_id: str, tex_url: str, save_dir: str, headers: dict):
    """
    下载 TeX 源码 .tar.gz 文件
    """
    os.makedirs(save_dir, exist_ok=True)
    file_path = os.path.join(save_dir, f"{arxiv_id}.tar.gz")
    temp_file_path = f"{file_path}.download"

    if os.path.exists(temp_file_path):
        os.remove(temp_file_path)

    try:
        st_progress = st.progress(0)
        status_text = st.empty()
        last_report = [0.0]

        def report(downloaded, total_size):
            # Update with throttling to avoid noisy terminal output.
            now = time.time()
            if now - last_report[0] < 0.5 and downloaded != total_size:
                return
            last_report[0] = now
            st_progress.progress(downloaded / total_size if total_size else 1.0)
            status_text.text(
                f"Downloading {arxiv_id}: {downloaded/1024/1024:.2f}MB / {total_size/1024/1024:.2f}MB"
            )

        download_file_ranged(tex_url, temp_file_path, headers, on_progress=report)

        if not _is_complete_archive(temp_file_path):
            raise IOError(f"Downloaded archive is incomplete or unreadable: {temp_file_path}")

        os.replace(temp_file_path, file_path)

        st.success(f"[SUCCESS] {arxiv_id} successfully downloaded to {file_path}.")

        return os.path.join(save_dir, f"{arxiv_id}")

    except (requests.RequestException, OSError) as e:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        st.error(f"[FAIL] {arxiv_id} download failed: {e}")
        return None

ARXIV_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _extract_nested_archives(project_dir: str) -> None:
    """保持旧行为：源码目录内部的压缩包也一并解压。"""
    for root, _, files in os.walk(project_dir):
        for file in files:
            extract_archive_file(os.path.join(root, file), top_level=False)


def download_arxiv_source(arxiv_id: str, save_dir: str) -> Optional[str]:
    """下载并解压单篇论文的 TeX 源码，返回源码目录。

    直接请求 ``/src/{id}``，省去先抓取 abs 页面的一次往返；
    失败时再按旧方式从 abs 页面解析下载链接。
    """
    os.makedirs(save_dir, exist_ok=True)
    project_dir = os.path.join(save_dir, arxiv_id)
    tar_path = os.path.join(save_dir, f"{arxiv_id}.tar.gz")

    if is_already_downloaded(arxiv_id, save_dir):
        print(f"[SKIP] Already downloaded: {arxiv_id}")
    elif download_tex(arxiv_id, f"https://arxiv.org/src/{arxiv_id}", save_dir, ARXIV_HEADERS) is None:
        tex_url = get_tex_url(arxiv_id, ARXIV_HEADERS)
        if not tex_url:
            print(f"[SKIP] No TeX source found for {arxiv_id}. Please check the arXiv ID or the availability of the source.")
            return None
        if download_tex(arxiv_id, tex_url, save_dir, ARXIV_HEADERS) is None:
            print(f"[SKIP] Source download failed for {arxiv_id}, skip TeX processing.")
            return None

    if os.path.isfile(tar_path):
        if extract_archive_file(tar_path, top_level=True) is None:
            print(f"[SKIP] Source extraction failed for {arxiv_id}, skip TeX processing.")
            return None
        if os.path.isdir(project_dir):
            _extract_nested_archives(project_dir)
    return project_dir if os.path.isdir(project_dir) else None


def download_arxiv_pdf(arxiv_id: str, save_dir: str) -> Optional[str]:
    """下载原文 PDF 到源码目录之外的临时文件，返回其路径。

    放在源码目录外，避免与源码解压、目录复制并发时互相干扰；
    由调用方在合适的时机移动到最终位置。
    """
    final_path = os.path.join(save_dir, arxiv_id, f"{arxiv_id}.pdf")
    if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
        return final_path

    os.makedirs(save_dir, exist_ok=True)
    temp_path = os.path.join(save_dir, f".{arxiv_id}.pdf.download")
    try:
        download_file_ranged(f"https://arxiv.org/pdf/{arxiv_id}.pdf", temp_path, ARXIV_HEADERS)
        print(f"[SUCCESS] Downloaded PDF for {arxiv_id}")
        return temp_path
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"[ERROR] Failed to download PDF for {arxiv_id}: {str(e)}")
        return None


def place_arxiv_pdf(pdf_path: Optional[str], arxiv_id: str, target_dirs: List[str]) -> None:
    """把原文 PDF 复制到各目标目录（缺失时才写入），随后清理临时下载文件。"""
    if not pdf_path or not os.path.isfile(pdf_path):
        return
    for target_dir in target_dirs:
        target = os.path.join(target_dir, f"{arxiv_id}.pdf")
        if os.path.isdir(target_dir) and not os.path.isfile(target):
            shutil.copyfile(pdf_path, target)
    if pdf_path.endswith(".pdf.download"):
        os.remove(pdf_path)


def batch_download_arxiv_tex(arxiv_ids: List[str], save_dir: str = "./tex_sources"):
    """
    并发下载多个 arXiv 论文的 TeX 源码（已解压）和原文 PDF
    """
    from concurrent.futures import ThreadPoolExecutor

    if not arxiv_ids:
        return []
    with ThreadPoolExecutor(max_workers=min(16, 2 * len(arxiv_ids))) as pool:
        source_futures = [pool.submit(download_arxiv_source, arxiv_id, save_dir) for arxiv_id in arxiv_ids]
        pdf_futures = [pool.submit(download_arxiv_pdf, arxiv_id, save_dir) for arxiv_id in arxiv_ids]
        source_dirs = [future.result() for future in source_futures]
        for arxiv_id, source_dir, pdf_future in zip(arxiv_ids, source_dirs, pdf_futures):
            if source_dir:
                place_arxiv_pdf(pdf_future.result(), arxiv_id, [source_dir])
    return [source_dir for source_dir in source_dirs if source_dir]


def fetch_arxiv_categories(arxiv_id: str) -> List[str]:
    """从 abs 页面读取单篇论文的学科分类。"""
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    categories = []
    try:
        resp = requests.get(abs_url, headers=ARXIV_HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        subjects_div = soup.find("div", class_="subjects")
        if subjects_div:
            categories.extend(re.findall(r"\(([a-z]+\.[A-Z]+)\)", subjects_div.text))
        else:
            td_subjects = soup.find("td", class_="tablecell subjects")
            if td_subjects:
                categories.extend(re.findall(r'\(([a-z]+\.[A-Z]+)\)', td_subjects.text))

        if not categories:
            print(f"[WARNING] No categories found for {arxiv_id}")
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {arxiv_id}: {e}")
        categories = []
    return categories


def get_arxiv_category(arxiv_ids: List[str]) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    if not arxiv_ids:
        return {}
    # 小并发代替逐个请求 + sleep(1)，对 arXiv 的压力仍然有限。
    with ThreadPoolExecutor(max_workers=min(4, len(arxiv_ids))) as pool:
        return dict(zip(arxiv_ids, pool.map(fetch_arxiv_categories, arxiv_ids)))

_ARXIV_ID_PATTERN = r"(?:[\w-]+/\d{7}(?:v\d+)?|\d{4}\.\d{5,7}(?:v\d+)?)"
_ARXIV_URL_PATTERN = re.compile(
    rf"(?:https?://)?(?:www\.|export\.)?arxiv\.org/"
    rf"(?:abs|pdf|e-print|html)/(?P<id>{_ARXIV_ID_PATTERN})"
    rf"(?:\.pdf)?(?:[/?#]|$)",
    re.IGNORECASE,
)


def is_valid_arxiv_id(id_str):
    """判断输入是否为现代或旧格式的 arXiv ID。"""
    if not isinstance(id_str, str):
        return False
    return re.fullmatch(_ARXIV_ID_PATTERN, id_str.strip()) is not None


def _extract_arxiv_id(item):
    """从裸 ID 或 arXiv 页面/下载 URL 中提取规范 ID。"""
    if not isinstance(item, str):
        return ""

    normalized_item = item.strip()
    if is_valid_arxiv_id(normalized_item):
        return normalized_item

    match = _ARXIV_URL_PATTERN.search(normalized_item)
    return match.group("id") if match else ""


def extract_arxiv_ids(arxiv_list):
    """从多个裸 ID 或 URL 中提取 arXiv ID。"""
    ids = []
    for item in arxiv_list or []:
        arxiv_id = _extract_arxiv_id(item)
        if arxiv_id:
            ids.append(arxiv_id)
    return ids


def extract_arxiv_ids_V2(item):
    """兼容旧调用方的单值 arXiv ID 提取入口。"""
    return _extract_arxiv_id(item)
