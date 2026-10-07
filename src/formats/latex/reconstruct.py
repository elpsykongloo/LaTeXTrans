from typing import List, Dict, Any
import os
import re
from collections import Counter
from .utils import *
from .utils import _find_balanced_group_end


def _strip_md_wrap(text: str) -> str:
    """只移除包住整段译文的 Markdown 围栏或成对单反引号。"""
    if not text:
        return text
    stripped = text.strip()
    fence = re.fullmatch(r"```[A-Za-z]*[ \t]*\r?\n(.*?)\r?\n?```", stripped, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    if (
        len(stripped) >= 2 and stripped.startswith("`") and stripped.endswith("`")
        and "`" not in stripped[1:-1]
    ):
        return stripped[1:-1].strip()
    return text


def _recover_caption_trans(content: str, trans_content: str) -> str:
    """从模型说明中恢复同名完整命令；不可恢复时保留原文。"""
    text = _strip_md_wrap(trans_content or "")
    source = re.match(r"\s*\\([A-Za-z@]+\*?)", content or "")
    if not source:
        return text
    macro = source.group(1)
    required_groups = 2 if macro.rstrip("*") == "captionof" else 1
    source_type = None
    if required_groups == 2:
        original = get_captionof_pattern().fullmatch(content.strip())
        if original:
            source_type = original.group("type")[1:-1].strip()
    boundary = re.compile(r"\\" + re.escape(macro) + r"(?![A-Za-z@*])")
    best = None
    position = 0
    while True:
        match = boundary.search(text, position)
        if match is None:
            break
        index = match.end()
        groups = []
        while len(groups) < required_groups:
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text) or text[index] not in "[{":
                break
            opening = text[index]
            end = _find_balanced_group_end(
                text, index, opening, "]" if opening == "[" else "}"
            )
            if end is None:
                break
            if opening == "{":
                groups.append(text[index + 1:end - 1])
            index = end
        if (
            len(groups) == required_groups and groups[-1].strip()
            and (source_type is None or groups[0].strip() == source_type)
        ):
            best = text[match.start():index]
            position = index
        else:
            position = match.end()
    return best if best is not None else content

class LatexConstructor:
    def __init__(self, 
                 sections: List[Dict[str, Any]], 
                 captions: List[Dict[str, Any]], 
                 envs: List[Dict[str, Any]],
                 inputs: List[Dict[str, Any]],
                 newcommands: List[Dict[str, Any]],
                 output_latex_dir: str,
                 target_language: str = "ch",
                 ):
        self.sections = sections
        self.captions = captions
        self.envs = envs
        self.inputs = inputs
        self.newcommands = newcommands
        self.output_latex_dir = output_latex_dir
        self.target_language = target_language or "ch"

    def construct(self):
        """
        construct the translated latex  project  from the sections, envs, captions and inputs
        """
        self._validate_translation_placeholders()
        tex = self._merge_sections()
        tex = self._restore_fragments(tex)

        # process japanese specific packages ----------
        # tex = self._comment_out_latex_packages_for_ja(tex)
        # tex = self._add_lualatex_option_to_documentclass_for_ja(tex)
        # ---------------------------------------------


        self._revert_inputs(tex)
    
    def _merge_sections(self, content_key: str = "trans_content") -> str:
        """
        Merge all the sections to a tex
        """
        tex = ""
        for section in self.sections:
            content = section.get(content_key) or ""
            if content_key == "trans_content":
                content = _strip_md_wrap(content)
            tex += content + "\n"
        return tex

    def _build_source_tex(self) -> str:
        """按相同流程还原未翻译的全文，作为结构校验的基线。"""
        tex = self._merge_sections("content")
        return self._restore_fragments(tex, source=True)

    @staticmethod
    def _translated_fragment(kind: str, record: Dict[str, Any]) -> str:
        if kind == "caption":
            return _recover_caption_trans(
                record.get("content", ""), record.get("trans_content", "")
            )
        if kind == "newcommand" or record.get("need_trans") is False:
            return record.get("content", "")
        return _strip_md_wrap(record.get("trans_content", "") or "")

    def _restore_fragments(self, tex: str, source: bool = False) -> str:
        records = []
        for kind, items in (
            ("environment", self.envs), ("caption", self.captions),
            ("newcommand", self.newcommands),
        ):
            records.extend({
                **record,
                "replacement": record.get("content", "") if source
                else self._translated_fragment(kind, record),
            } for record in items)
        return self._replace_records(tex, records, "replacement")

    def _revert_envs(self, tex: str) -> str:
        """
        Revert all the envs to tex
        """
        records = [{**record, "replacement": self._translated_fragment("environment", record)}
                   for record in self.envs]
        return self._replace_records(tex, records, "replacement")
             
    def _revert_captions(self, tex: str) -> str:
        """
        Revert all the captions to tex
        """
        records = [{**record, "replacement": self._translated_fragment("caption", record)}
                   for record in self.captions]
        return self._replace_records(tex, records, "replacement")
    
    def _revert_newcommands(self, tex: str) -> str:
        """
        Revert all the newcommands to tex
        """
        return self._replace_records(tex, self.newcommands, "content")

    @staticmethod
    def _replace_records(
        tex: str,
        records: List[Dict[str, Any]],
        content_key: str,
    ) -> str:
        """按源文记录的依赖展开嵌套片段，再一次性替换全文占位符。"""
        replacements = {
            record["placeholder"]: record.get(content_key, "") or ""
            for record in records
            if record.get("placeholder")
        }
        if not replacements:
            return tex

        dependencies = {
            record["placeholder"]: set(extract_latex_placeholders(record.get("content", "")))
            for record in records if record.get("placeholder")
        }
        expanded = {}

        def expand(placeholder: str, visiting: set) -> str:
            if placeholder in expanded:
                return expanded[placeholder]
            if placeholder in visiting:
                raise ValueError(f"Cyclic LaTeX placeholder dependency: {placeholder}")
            path = visiting | {placeholder}
            permitted = dependencies[placeholder]
            value = LATEX_PLACEHOLDER_PATTERN.sub(
                lambda match: expand(match.group(0), path)
                if match.group(0) in permitted and match.group(0) in replacements
                else match.group(0),
                replacements[placeholder],
            )
            expanded[placeholder] = value
            return value

        for placeholder in replacements:
            expand(placeholder, set())

        # 长占位符优先，避免未来出现无尖括号边界的相似标记时产生歧义。
        pattern = re.compile(
            "|".join(
                re.escape(placeholder)
                for placeholder in sorted(replacements, key=len, reverse=True)
            )
        )
        return pattern.sub(lambda match: expanded[match.group(0)], tex)

    def _validate_translation_placeholders(self) -> None:
        """在写入主文件或包含文件前校验片段的占位符边界。"""
        errors = []
        collections = (
            ("section", self.sections),
            ("caption", self.captions),
            ("environment", self.envs),
        )
        for kind, records in collections:
            for record in records:
                expected = Counter(extract_latex_placeholders(record.get("content", "")))
                actual = Counter(extract_latex_placeholders(
                    self._translated_fragment(kind, record)
                    if kind != "section" else _strip_md_wrap(record.get("trans_content", ""))
                ))
                missing = list((expected - actual).elements())
                unexpected = list((actual - expected).elements())
                if missing or unexpected:
                    identifier = record.get("section") or record.get("placeholder")
                    details = []
                    if missing:
                        details.append(f"缺少 {missing}")
                    if unexpected:
                        details.append(f"多出 {unexpected}")
                    errors.append(f"{kind} {identifier}: " + "; ".join(details))

        if errors:
            details = "\n".join(f"- {error}" for error in errors)
            raise ValueError("翻译结果的占位符校验失败，已阻止写入 TeX：\n" + details)
                                          
    def _revert_inputs(self, tex: str):
        # 写出包含 documentclass 的模板后，重新搜索可能把模板误判为主文件。
        main_file_path = find_main_tex_file(self.output_latex_dir)
        if main_file_path:
            main_file_path = os.fspath(project_path(self.output_latex_dir, main_file_path, writing=True))
        # 与解析阶段一致：\input 路径相对于主文件所在目录。
        input_base_dir = (
            os.path.dirname(main_file_path)
            if main_file_path
            else self.output_latex_dir
        )
        # 导言区可能位于多层 input 中；必须在还原文件边界前按目标语言配置
        # 排版支持（中文/日文：XeLaTeX + xeCJK；其它语言不注入 CJK 字体）。
        if target_language_family(self.target_language) == "other":
            print(
                f"⚠️ 目标语言 {self.target_language!r} 未内置排版支持，"
                "不注入 CJK 宏包，由编译器自动选择引擎。"
            )
        tex = add_target_language_package(tex, self.target_language)

        # 翻译模型可能在 \ref/\cref/\cite 等不可翻译参数中转义下划线。
        # 在拆分包含文件前修复，使主文件和包含文件都得到处理。
        tex = normalize_latex_reference_arguments(tex)

        # 在合并后的全文上校验：环境可能跨越主文件与包含文件。只阻止译文
        # 新引入的问题；源文档中本就存在、TeX 能正常处理的伪不配对标记
        # （例如 \patchcmd 参数里的 \begin{tcolorbox}）不应阻止生成。
        structure_errors = validate_latex_structure(
            tex, source_tex=self._build_source_tex()
        )
        if structure_errors:
            details = "\n".join(f"- {error}" for error in structure_errors)
            raise ValueError(
                "生成的 LaTeX 结构校验失败，已阻止继续编译：\n" + details
            )

        begin_map = {sec["begin"]: sec for sec in self.inputs}
        end_map = {sec["end"]: sec for sec in self.inputs}
        pattern = re.compile(r"<PLACEHOLDER_[^>]+?_begin>|<PLACEHOLDER_[^>]+?_end>")

        stack = []
        pos = 0  

        while True:
            match = pattern.search(tex, pos)
            if not match:
                break

            tag = match.group()

            if tag in begin_map:
                stack.append((tag, match.start()))
                pos = match.end()  
            elif tag in end_map:
                if not stack:
                    raise ValueError(f"Unmatched end tag: {tag}")
                begin_tag, begin_pos = stack.pop()
                if end_map[tag] != begin_map[begin_tag]:
                    raise ValueError(f"Mismatched tags: {begin_tag} vs {tag}")

                input_info = begin_map[begin_tag]
                end_pos = match.end()

                inner_start = begin_pos + len(begin_tag)
                inner_end = match.start()
                inner_content = tex[inner_start:inner_end].strip()

                # 优先使用解析时实际读取的文件名（例如 fig.tikz 不能被写成
                # fig.tikz.tex，否则编译仍会读到未翻译的原文件）。
                relative_path = input_info.get("file") or input_info["path"]
                if not input_info.get("file") and not relative_path.endswith(".tex"):
                    relative_path += ".tex"
                residual_matches = extract_latex_placeholders(inner_content)
                if residual_matches:
                    raise ValueError(
                        f"包含文件 {relative_path} 中存在未替换占位符："
                        f"{list(dict.fromkeys(residual_matches))}"
                    )
                output_path = project_path(self.output_latex_dir, os.path.join(input_base_dir, relative_path), writing=True)
                os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                with open(output_path, "w", encoding="utf-8", newline="") as f:
                    f.write(inner_content + "\n")

                tex = tex[:begin_pos] + input_info["command"] + tex[end_pos:]

                pos = begin_pos + len(input_info["command"])

            else:
                pos = match.end()

        if stack:
            unclosed_tags = [tag for tag, _ in stack]
            print(f"⚠️ Warning: Unclosed begin placeholder(s) found and skipped: {unclosed_tags}")
        
        residual_matches = extract_latex_placeholders(tex)
        if residual_matches:
            raise ValueError(
                "主 TeX 中存在未替换占位符："
                f"{list(dict.fromkeys(residual_matches))}"
            )

        if target_language_family(self.target_language) != "other":
            # 中文/日文输出固定走 XeLaTeX，需要修补模板中的 pdfTeX 专用指令。
            patch_xelatex_compatibility_files(self.output_latex_dir)

        if main_file_path and os.path.exists(main_file_path):
            with open(main_file_path, "w", encoding="utf-8", newline="") as f:
                f.write(tex)
        else:
            print(f"⚠️ Warning: No main.tex file found in {self.output_latex_dir}, creating a new one.")
            main_file_path = os.path.join(self.output_latex_dir, "main.tex")
            main_file_path = os.fspath(project_path(self.output_latex_dir, main_file_path, writing=True))
            with open(main_file_path, "w", encoding="utf-8", newline="") as f:
                f.write(tex)
        record_main_tex_file(self.output_latex_dir, main_file_path)

    def _comment_out_latex_packages_for_ja(self, tex):

        packages_to_comment = [
            r'\usepackage[utf8]{inputenc}',
            r'\usepackage[T1]{fontenc}',
            r'\usepackage{times}',
            r'\usepackage{mathptmx}',
            r'\pdfoutput=1'
        ]
        
        lines = tex.splitlines()
        
        for i, line in enumerate(lines):
            stripped_line = line.strip()
            for package in packages_to_comment:
                if stripped_line.startswith(package) and not stripped_line.startswith('%'):
                    lines[i] = line.replace(package, f'% {package}')
                    break 
        
        return '\n'.join(lines)        

    def _add_lualatex_option_to_documentclass_for_ja(self, tex):

        import re
        
        pattern = re.compile(r'\\documentclass(?:\[([^\]]*)\])?(\{.*?\})')
        
        def replacer(match):
            options = match.group(1)
            class_name = match.group(2)
            
            if options:
                if 'lualatex' not in options:
                    new_options = options + ', lualatex'
                else:
                    new_options = options
                return f'\\documentclass[{new_options}]{class_name}'
            else:
                return f'\\documentclass[lualatex]{class_name}'
        
        modified_source = pattern.sub(replacer, tex)
        
        return modified_source
    


# caption_dir = "D:\code\AutoLaTexTrans\output\ch_arXiv-2504.10471v1\captions_map.json"
# section_dir = "D:\code\AutoLaTexTrans\output\ch_arXiv-2504.10471v1\sections_map.json"
# input_dir = "D:\code\AutoLaTexTrans\output\ch_arXiv-2504.10471v1\inputs_map.json"
# envs_dir = "D:\code\AutoLaTexTrans\output\ch_arXiv-2504.10471v1\envs_map.json"
# sections = read_json_file(section_dir)
# captions= read_json_file(caption_dir)
# inputs = read_json_file(input_dir)
# envs = read_json_file(envs_dir)
# dir = "D:\code\AutoLaTexTrans/tests/10471"


# latexconstructor = LatexConstructor(
#     sections=sections,
#     captions=captions,
#     envs=envs,
#     inputs=inputs,
#     output_latex_dir=dir
# )
# latexconstructor.construct()
