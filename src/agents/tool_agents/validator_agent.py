from typing import Dict, Any, List, Optional
from src.agents.tool_agents.base_tool_agent import BaseToolAgent
# from base_tool_agent import BaseToolAgent
from pathlib import Path
from collections import Counter
from pylatexenc.latexwalker import LatexWalker
from src.formats.latex.utils import find_latex_bracket_errors, LATEX_OPAQUE_ENVIRONMENTS
import sys
import os
import re
import threading


_PROSE_SYMBOL_COMMANDS = {
    "sim": "正文约号（\\sim / ～）",
    "ldots": "正文省略号（\\ldots / …）",
    "dots": "正文省略号（\\ldots / …）",
    "textellipsis": "正文省略号（\\ldots / …）",
}
_PROSE_UNICODE_SYMBOLS = {
    "～": _PROSE_SYMBOL_COMMANDS["sim"],
    "〜": _PROSE_SYMBOL_COMMANDS["sim"],
    "∼": _PROSE_SYMBOL_COMMANDS["sim"],
    "…": _PROSE_SYMBOL_COMMANDS["ldots"],
}
_MATH_ENVIRONMENTS = frozenset({
    "math", "displaymath", "equation", "equation*", "align", "align*",
    "aligned", "alignat", "alignat*", "gather", "gather*", "gathered",
    "multline", "multline*", "eqnarray", "eqnarray*", "flalign", "flalign*",
    "split", "array", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix",
})
_OPAQUE_MACROS = frozenset({
    "verb", "lstinline", "url", "path", "href", "label", "ref", "pageref",
    "eqref", "autoref", "cref", "Cref", "cite", "citep", "citet", "input",
    "include", "includegraphics", "bibliography", "bibliographystyle",
})

base_dir = os.getcwd()
sys.path.append(base_dir)


class ValidatorAgent(BaseToolAgent):
    def __init__(self, 
                 config: Dict[str, Any],
                 project_dir: str = None,
                 output_dir: str = None,
                 ):
        super().__init__(agent_name="ValidatorAgent", config=config)
        self.config = config
        self.project_dir = project_dir
        self.output_dir = output_dir
        self.prose_equivalences = str(
            config.get("validation_prose_equivalences", True)
        ).strip().lower() not in {"false", "0", "no", "off"}

    def execute(self, errors_report : Optional[List[Dict]]=None) -> List[Dict]:

        self.log(f"🤖💬 Start validating for project...⏳: {os.path.basename(self.project_dir)}.")
        sections = self.read_file(Path(self.output_dir, "sections_map.json"), "json")
        captions = self.read_file(Path(self.output_dir, "captions_map.json"), "json")
        envs = self.read_file(Path(self.output_dir, "envs_map.json"), "json")

        if errors_report is None:
            parts_need_val = self._extract_parts_need_validate(secs=sections, # secs caps envs
                                                               caps=captions,
                                                               envs=envs)
        else:
            parts_need_val = self._extract_parts_from_report(secs=sections, 
                                                               caps=captions,
                                                               envs=envs,
                                                               errors_report=errors_report)
        errors_report = []
        for part in parts_need_val:
            error_report = self._validate(part)
            if error_report:
                errors_report.append(error_report)
        # 始终刷新报告，避免上一次失败留下的 errors_report.json 被误认为当前状态。
        self.save_file(
            Path(self.output_dir, "errors_report.json"),
            "json",
            errors_report,
        )
        if errors_report:
            self.log(
                f"⚠️ Verification Complete for {os.path.basename(self.project_dir)}, "
                f"remaining Errors: {len(errors_report)}.",
                level="warning",
            )
        else:
            self.log(
                f"✅ Verification Complete for {os.path.basename(self.project_dir)}, "
                "remaining Errors: 0."
            )
        return errors_report

    # 翻译阶段已就地校验过的片段在独立校验时直接复用结果（纯函数，按原文/译文缓存）。
    # 缓存按项目输出目录分区：同一项目的 TranslatorAgent 内联校验与 Coordinator
    # 的独立校验共享，不同项目（并发线程）互不干扰；所有访问都在锁内完成。
    _CACHE_LIMIT = 20000
    _caches: Dict[str, Dict[tuple, tuple]] = {}
    _caches_lock = threading.Lock()

    @staticmethod
    def _cache_key(output_dir: Optional[str]) -> str:
        return os.path.normcase(os.path.abspath(output_dir)) if output_dir else ""

    @classmethod
    def release_cache(cls, output_dir: Optional[str]) -> None:
        """项目结束后释放其校验缓存。"""
        with cls._caches_lock:
            cls._caches.pop(cls._cache_key(output_dir), None)

    def _cache_get(self, key: tuple) -> Optional[tuple]:
        with self._caches_lock:
            return self._caches.get(self._cache_key(self.output_dir), {}).get(
                (self.prose_equivalences, key)
            )

    def _cache_put(self, key: tuple, value: tuple) -> None:
        with self._caches_lock:
            cache = self._caches.setdefault(self._cache_key(self.output_dir), {})
            if len(cache) > self._CACHE_LIMIT:
                cache.clear()
            cache[(self.prose_equivalences, key)] = value

    def _validate(self, part :Dict[str, Any]) -> Dict[str, Any]:
        key = (part.get("content", ""), part.get("trans_content", ""))
        cached = self._cache_get(key)
        if cached is None:
            cached = (
                self._validate_command(part),
                self._validate_placeholder(part),
                self._validate_closed_brackets(part),
            )
            self._cache_put(key, cached)
        command_error, ph_error, bracket_error = cached
        error_report = {}

        if not command_error and not ph_error and not bracket_error:
            return None
        else: 
            if "section" in part:
                error_report["part"] = "sec"
                error_report["num_or_ph"] = part["section"]
            elif "env_name" in part:
                error_report["part"] = "env"
                error_report["num_or_ph"] = part["placeholder"]
            elif "cap_type" in part:
                error_report["part"] = "cap"
                error_report["num_or_ph"] = part["placeholder"]

            if command_error:
                error_report["command_error"] = command_error
            if ph_error:
                error_report["ph_error"] = ph_error
            if bracket_error:
                error_report["bracket_error"] = bracket_error

        return error_report

    def _validate_command(self, part :Dict[str, Any])-> Optional[str]:
        content = part.get("content", "")
        trans = part.get("trans_content", "")

        try:
            src_symbols, trans_symbols = [], []
            if self.prose_equivalences:
                content, src_symbols = self._normalize_prose_symbols(content)
                trans, trans_symbols = self._normalize_prose_symbols(trans)
            src_sequence = self.extract_command_sequence(content)
            trans_sequence = self.extract_command_sequence(trans)
        except Exception as exc:
            return f"LaTeX 命令解析失败：{exc}"

        # 单字符命令整体被忽略，但数学定界符出错会直接导致编译失败，单独核对数量。
        src_sequence = src_sequence + self.extract_math_delimiters(content) + src_symbols
        trans_sequence = trans_sequence + self.extract_math_delimiters(trans) + trans_symbols
        src_counter = Counter(src_sequence)
        trans_counter = Counter(trans_sequence)
        if src_counter == trans_counter:
            if self.extract_environment_sequence(src_sequence) == self.extract_environment_sequence(trans_sequence):
                return None

        errors = []
        for elem, count in (src_counter - trans_counter).items():
            errors.append(
                f"'{elem}' — expected {count + trans_counter.get(elem, 0)}, "
                f"found {trans_counter.get(elem, 0)}"
            )

        for elem, count in (trans_counter - src_counter).items():
            errors.append(
                f"'{elem}' — source expected {src_counter.get(elem, 0)}, "
                f"found {count + src_counter.get(elem, 0)}"
            )

        if not errors and self.extract_environment_sequence(src_sequence) != self.extract_environment_sequence(trans_sequence):
            errors.append(
                "LaTeX 环境边界顺序或嵌套结构发生变化；"
                f"原始环境数 {len(self.extract_environment_sequence(src_sequence))}，"
                f"翻译后环境数 {len(self.extract_environment_sequence(trans_sequence))}"
            )

        if errors:
            return "LaTeX 命令翻译结果不一致：\n" + "\n".join(errors)
        return None        

    @staticmethod
    def _normalize_prose_symbols(latex_code: str) -> tuple:
        r"""只归一化正文标点；公式、代码、引用和宏定义里的命令保留。

        独立的 ``$\sim$`` / ``\(\ldots\)`` / ``\ensuremath{\sim}`` 可作为
        正文符号。``$x\sim y$``、display math 和数学环境不参与等价处理。
        另行计数这些符号，使删除或重复它们仍会产生错误。
        """
        nodes, _, _ = LatexWalker(latex_code).get_latex_nodes()
        spans, symbols = [], []

        def add(start, end, symbol):
            spans.append((start, end))
            symbols.append(symbol)

        def recurse(items):
            for node in items or []:
                name = node.__class__.__name__
                raw = latex_code[node.pos:node.pos + node.len]
                if name == "LatexMathNode":
                    opening, closing = node.delimiters
                    if (opening, closing) in {("$", "$"), (r"\(", r"\)")} and raw.endswith(closing):
                        inner = raw[len(opening):-len(closing)].strip()
                        match = re.fullmatch(r"\\(sim|ldots|dots)\s*", inner)
                        if match:
                            add(node.pos, node.pos + node.len, _PROSE_SYMBOL_COMMANDS[match.group(1)])
                    continue
                if name == "LatexEnvironmentNode":
                    if node.environmentname in LATEX_OPAQUE_ENVIRONMENTS | _MATH_ENVIRONMENTS:
                        continue
                    recurse(node.nodelist)
                elif name == "LatexMacroNode":
                    macro = node.macroname
                    if macro in _PROSE_SYMBOL_COMMANDS:
                        add(node.pos, node.pos + node.len, _PROSE_SYMBOL_COMMANDS[macro])
                        continue
                    if macro == "ensuremath":
                        match = re.fullmatch(r"\\ensuremath\s*\{\s*\\(sim|ldots|dots)\s*\}", raw)
                        if match:
                            add(node.pos, node.pos + node.len, _PROSE_SYMBOL_COMMANDS[match.group(1)])
                        continue
                    if macro in _OPAQUE_MACROS or macro in {
                        "newcommand", "renewcommand", "providecommand", "def", "gdef", "edef", "xdef",
                    }:
                        continue
                    if node.nodeargd:
                        recurse(arg for arg in node.nodeargd.argnlist if arg is not None)
                elif name == "LatexCharsNode":
                    index = 0
                    while index < len(raw):
                        char = raw[index]
                        if char in _PROSE_UNICODE_SYMBOLS:
                            end = index + 1
                            if char == "…" and raw[index:end + 1] == "……":
                                end += 1  # 中文六点省略号表示一个正文标点。
                            add(node.pos + index, node.pos + end, _PROSE_UNICODE_SYMBOLS[char])
                            index = end
                        else:
                            index += 1
                elif getattr(node, "nodelist", None):
                    recurse(node.nodelist)

        recurse(nodes)
        for start, end in sorted(spans, reverse=True):
            latex_code = latex_code[:start] + " " + latex_code[end:]
        return latex_code, symbols

    def _validate_placeholder(self, part :Dict[str, Any])-> Optional[str]:
        original_placeholders = self._extract_placeholder_sequence(part.get("content", ""))
        translated_placeholders = self._extract_placeholder_sequence(
            part.get("trans_content", "")
        )
        original_counter = Counter(original_placeholders)
        translated_counter = Counter(translated_placeholders)
        missing = original_counter - translated_counter
        extra = translated_counter - original_counter
        errors = []
        if missing:
            errors.append(
                "Missing placeholders: "
                + ", ".join(
                    f"{placeholder} (x{count})"
                    for placeholder, count in sorted(missing.items())
                )
                + " translation error or is missing!"
            )
        if extra:
            errors.append(
                "Extra placeholders: "
                + ", ".join(
                    f"{placeholder} (x{count})"
                    for placeholder, count in sorted(extra.items())
                )
                + " translation error or is redundant"
            )
        if not errors and original_placeholders != translated_placeholders:
            errors.append(
                "Placeholders are present but their order changed: "
                f"expected {original_placeholders}, found {translated_placeholders}"
            )
        return "\n".join(errors) if errors else None
        
    def _validate_closed_brackets(self, part: Dict[str, Any]) -> Optional[str]:

        content = part.get("content", "")
        trans_content = part.get("trans_content", "")
        org_errors = self._find_brackets_errors(content, org=1)
        errors = self._find_brackets_errors(trans_content)

        if errors and not org_errors:
            return "Brackets error:\n" + "\n".join(errors)
        else:
            return None
        
    def _find_brackets_errors(self, content, org=None):
        # Round parentheses are ordinary prose punctuation, not a reliable
        # indicator of LaTeX structure.  In particular, translated lists may
        # contain ``1)``/``2)`` even when the source uses the same notation;
        # checking them here made the source and translated text asymmetric
        # because ``org`` intentionally omitted parentheses.  LaTeX command
        # names and their arguments are validated separately, so this check
        # only needs to cover braces and square brackets.
        return find_latex_bracket_errors(content)

    def _extract_latex_elements_with_counts(self, content: str) -> Counter:
        elements = []

        elements += re.findall(r"\\begin\{.*?\}", content)
        elements += re.findall(r"\\end\{.*?\}", content)

        elements += re.findall(r"\\[a-zA-Z]+\*?", content)

        math_inline = re.findall(r"\$(.+?)\$", content, re.DOTALL)
        # math_display = re.findall(r"\\\[(.+?)\\\]", content, re.DOTALL)
        elements += [expr.strip() for expr in math_inline  if expr.strip()]

        return Counter(elements)
    
    def extract_command_sequence(self, latex_code: str) -> List[str]:
        """按源码顺序提取 LaTeX 命令，包含嵌套参数中的命令。"""
        walker = LatexWalker(latex_code)
        nodes, _, _ = walker.get_latex_nodes()
        commands = []
        
        ignored_commands = {'eg', 'ie'}

        def recurse(nodes):
            for node in nodes:
                clsname = node.__class__.__name__

                if clsname == "LatexMacroNode":
                    macro_name = node.macroname

                    if macro_name in ignored_commands:
                        continue
                    if len(macro_name) == 1 and not macro_name.isalpha():
                        continue

                    command = f"\\{macro_name}"
                    commands.append(command)

                    if node.nodeargd:
                        for arg in node.nodeargd.argnlist:
                            if arg is not None:
                                recurse([arg])

                elif clsname == "LatexEnvironmentNode":
                    env_name = node.environmentname
                    commands.append(f"\\begin{{{env_name}}}")
                    recurse(node.nodelist)
                    commands.append(f"\\end{{{env_name}}}")

                elif hasattr(node, 'nodelist') and node.nodelist:
                    recurse(node.nodelist)

        recurse(nodes)
        return commands

    @staticmethod
    def extract_math_delimiters(latex_code: str) -> List[str]:
        r"""核对数学定界符；忽略注释/代码及 ``\\[2pt]`` 换行参数。"""
        code = latex_code or ""
        nodes, _, _ = LatexWalker(code).get_latex_nodes()
        hidden = []

        def recurse(items):
            for node in items or []:
                name = node.__class__.__name__
                if name == "LatexCommentNode" or (
                    name == "LatexEnvironmentNode" and node.environmentname in LATEX_OPAQUE_ENVIRONMENTS
                ) or (name == "LatexMacroNode" and node.macroname in {"verb", "lstinline"}):
                    hidden.append((node.pos, node.pos + node.len))
                    continue
                if getattr(node, "nodeargd", None):
                    recurse(arg for arg in node.nodeargd.argnlist if arg is not None)
                if getattr(node, "nodelist", None):
                    recurse(node.nodelist)

        recurse(nodes)
        for start, end in sorted(hidden, reverse=True):
            code = code[:start] + " " * (end - start) + code[end:]
        return re.findall(r"(?<!\\)(?:\\\\)*(\\[\[\]()]|\$\$|\$)", code)

    def extract_command_counts(self, latex_code: str) -> Counter:
        """统计 LaTeX 命令，供结构一致性校验使用。"""
        return Counter(self.extract_command_sequence(latex_code))

    @staticmethod
    def extract_environment_sequence(command_sequence: List[str]) -> List[str]:
        """提取环境边界，允许普通行内命令随目标语言语序移动。"""
        return [
            command
            for command in command_sequence
            if command.startswith("\\begin{") or command.startswith("\\end{")
        ]

    def _extract_placeholders(self, content):
        """兼容旧调用方，返回结构占位符集合。"""
        return set(self._extract_placeholder_sequence(content))

    def _extract_placeholder_sequence(self, content: str) -> List[str]:
        """按出现顺序提取全部结构占位符，保留重复项。"""
        return re.findall(r"<PLACEHOLDER_[^>]+>", content or "")

    def _extract_parts_need_validate(self, secs, caps, envs):
        secs_need_val = [sec for sec in secs if sec["section"] != 0]
        caps_need_val = caps
        if envs:
            if "need_trans" in envs[0]:
                envs_need_val = [env for env in envs if env["need_trans"]]
            else:
                envs_need_val = [env for env in envs if env["content"] != env["trans_content"]]
        else:
            envs_need_val = []

        return secs_need_val + caps_need_val + envs_need_val
    
    def _extract_parts_from_report(
        self,
        secs: List[Dict],
        caps: List[Dict],
        envs: List[Dict],
        errors_report: List[Dict]) -> List[Dict]:

        section_lookup = {s["section"]: s for s in secs}
        caption_lookup = {c["placeholder"]: c for c in caps}
        environment_lookup = {e["placeholder"]: e for e in envs}
        
        parts_to_validate = []
        
        for error in errors_report:
            part_type = error.get("part")
            identifier = error.get("num_or_ph")
            
            if not part_type or not identifier:
                continue
                
            part = None
            if part_type == "sec":
                part = section_lookup.get(identifier)
            elif part_type == "cap":
                part = caption_lookup.get(identifier)
            elif part_type == "env":
                part = environment_lookup.get(identifier)
            
            if part:
                parts_to_validate.append(part)
                
        return parts_to_validate
    

# import toml
# import argparse
# from tqdm import tqdm
# from src.formats.latex.utils import get_profect_dirs


# parser = argparse.ArgumentParser()
# parser.add_argument("--config", type=str, default="config/default.toml")
# args = parser.parse_args()

# config = toml.load(args.config)
# dir = "D:\code\AutoLaTexTrans\outputs"
# projects = get_profect_dirs(dir)
# for project_dir in tqdm(projects, desc="Processing projects", unit="project"):
#     Validator = ValidatorAgent(config=config,
#                             project_dir=project_dir,
#                             output_dir=project_dir
#                             )
#     errors_report = Validator.execute()
#     if errors_report:
#         for error_report in errors_report:
#             error = ''
#             if "command_error" in error_report:
#                 error += error_report["command_error"] + '\n'
#             if "ph_error" in error_report:
#                 error += error_report["ph_error"] + '\n'
#             if "bracket_error" in error_report:
#                 error += error_report["bracket_error"] + '\n'
#             print(error_report["num_or_ph"])
#             print('\n')
#             print(error)
