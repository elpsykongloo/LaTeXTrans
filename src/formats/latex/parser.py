from typing import Any, Dict, Optional
from .utils import *
import tiktoken
import sys

from src.utils.progress import st


class LatexParser:
    def __init__(self, dir: str, output_dir: str):
        self.inputs_json = []
        self.envs_json = []
        self.captions_json = []
        self.newcommands_json = []
        self.sections_json = []
        self.dir = dir # LaTex profect directory
        self.output_dir = output_dir # Output directory for parsed files
        self.env_count = 0
        self.caption_count = 0

    def parse(self):
        """
        Parse the LaTeX document and return the parsed content.
        """
        process_b = st.empty()
        with process_b:
            process_bar = st.progress(0, text="Parsing LaTeX document...")

        main_tex_file = find_main_tex_file(self.dir) 
        if not main_tex_file:
            print("⚠️ Warning: There is no main tex file to compile in this directory.")
            return False

        process_bar.progress(10, text="Finding main tex file...")

        # \input 路径相对于主文件所在目录解析（源码包常把主文件放在子目录）。
        self.main_dir = os.path.dirname(main_tex_file)
        main_tex = read_tex_file(main_tex_file)
        if not main_tex:
            print("⚠️ Warning: The main tex file is empty.")
            return False
        
        process_bar.progress(20,text="Reading main tex file...")

        main_tex = remove_comments(main_tex)
        full_tex = self._merge_inputs(main_tex)
        full_tex = self._extract_newcommands(full_tex)

        full_tex = compress_newlines(full_tex) # Delete the redundant blank lines to prevent the large model from missing placeholders during translation

        self._split_to_sections(full_tex)

        self._merge_short_sections(min_tokens=50)  # Merge short sections to avoid too many sections

        total_sections = len(self.sections_json)
        process_bar.progress(80)

        for i,section in enumerate(self.sections_json):

            process_text = f"Processing chapter：{i+1}/{total_sections}"
            process_bar.progress(80 + int(15 * (i/total_sections)), text=process_text)

            if section["section"] == "0" or section["section"] == "-1":
                section_content = self._extract_captions(section["content"])
                self.sections_json[i]["trans_content"] = self._extract_envs(section_content)
                self.sections_json[i]["content"] = self.sections_json[i]["trans_content"]
            else:
                section_content = self._extract_captions(section["content"])
                self.sections_json[i]["content"] = self._extract_envs(section_content)

        process_bar.progress(100, text="Finish Parse Sections")
        st.success("Finish Parse Sections")
        process_b.empty()
        return True

    # def parse_no_env_cap_ph(self):
    #     """
    #     Parse the LaTeX document and return the parsed content.
    #     """
    #     main_tex_file = find_main_tex_file(self.dir) 
    #     if not main_tex_file:
    #         print("⚠️ Warning: There is no main tex file to compile in this directory.")
    #         return None
    #     main_tex = read_tex_file(main_tex_file)
    #     if not main_tex:
    #         print("⚠️ Warning: The main tex file is empty.")
    #         return None
        
    #     main_tex = remove_comments(main_tex)
    #     full_tex = self._merge_inputs(main_tex)
    #     full_tex = self._extract_newcommands(full_tex)
    #     full_tex = compress_newlines(full_tex)
    #     self._split_to_sections(full_tex)
    #     self._merge_short_sections(min_tokens=20)  # Merge short sections to avoid too many sections

    def _merge_inputs(self, tex: str) -> str:
        """
        Merge all the inputs in the main tex file and genarate a json file for the inputs.
        """
        main_tex = self._freeze_opaque_content(remove_comments(tex))
        command_name = r'input|include'
        pattern_input = get_command_pattern(command_name) # \input{file.tex} or \input{file} or \include{file.tex} or \include{file}
        pos = 0
        while True:
            result = pattern_input.search(main_tex, pos)
            if result is None:
                break
            begin, end = result.span()
            pos = result.end()
            match = result.group(4)
            base_dir = getattr(self, "main_dir", None) or self.dir
            input_begin = f"<PLACEHOLDER_{match}_begin>"
            input_end = f"<PLACEHOLDER_{match}_end>"
            # 当前位置已处于同一文件的展开内容中：文件包含了自身（直接或
            # 间接），继续展开会无限循环。
            if (
                main_tex.rfind(input_begin, 0, begin) != -1
                and main_tex.find(input_end, end) != -1
                and main_tex.rfind(input_end, 0, begin) < main_tex.rfind(input_begin, 0, begin)
            ):
                print(f"⚠️ Warning: Recursive TeX input skipped: {match}")
                continue
            inputfilepath = resolve_tex_input_file(base_dir, match, project_root=self.dir)
            if inputfilepath is None:
                requested_path = os.path.join(base_dir, match)
                fallback_path = requested_path
                if Path(str(match).strip()).suffix.lower() != ".tex":
                    fallback_path = f"{requested_path}.tex"
                print(
                    "⚠️ Warning: TeX input file not found or is a directory: "
                    f"{requested_path} (also tried {fallback_path})"
                )
                pos = result.end()  # Skip this input and continue
                continue

            input_tex = read_tex_file(inputfilepath)
            input_tex = self._freeze_opaque_content(remove_comments(input_tex))
            input_tex = input_begin + input_tex + input_end
            main_tex = main_tex[:begin] + input_tex + main_tex[end:]
            self.inputs_json.append({
                "command": result.group(0),
                "begin": input_begin,
                "end": input_end,
                "path": match,
                # 实际读取的文件（相对主文件目录），重建时写回同一文件。
                "file": os.path.relpath(inputfilepath, base_dir).replace(os.sep, "/"),
            })

        return main_tex

    def _freeze_opaque_content(self, tex: str) -> str:
        """在合并输入/分章节之前冻结代码，避免解析代码里的 TeX 样例。

        代码和宏定义都按原文重建，复用 newcommands_map 的原子记录，
        因而不需要增加解析产物或向翻译器发送代码正文。
        """
        fragments = []
        position = 0
        for start, end, name in get_latex_opaque_fragments(tex):
            placeholder = f"<PLACEHOLDER_OPAQUE_{len(self.newcommands_json)}>"
            self.newcommands_json.append({
                "placeholder": placeholder,
                "name": name,
                "kind": "opaque",
                "content": tex[start:end],
            })
            fragments.extend((tex[position:start], placeholder))
            position = end
        fragments.append(tex[position:])
        return "".join(fragments)

    def _extract_envs(self, tex: str) -> str:
        """
        Extract all the environments in the full tex and generate a json file for the environments.
        The environments are replaced with placeholders in the full tex.
        """
        full_tex, environment_tokens = get_latex_environment_tokens(tex)
        placeholder_pattern_cap= r"<PLACEHOLDER_CAP_\d+>"
        no_translate_envs = [
                             'equation', 'align', 'align*', 'gather', 'gather*', 'verbatim', 'verbatim*', 'lstlisting*', 'minted', 'minted*',
                             'equation*', 'alignat', 'alignat*', 'flalign', 'flalign*', 'split', 'split*', 'cases', 'cases*', 'subequations', 
                             'figure', 'figure*', 'wrapfigure', 'SCfigure', 'tikzpicture', 'CJK', 'scope',
                             'tabularx', 'tabulary', 'longtable*', 'sidewaystable', 'table', 'table*', 'tabular', 'tabular*', 'longtable',
                             'multline', 'multline*', 'lstlisting', 'tcolorbox', 'thebibliography', 'bibliography', 'bibitem',
                             'algorithm', 'algorithmic', 'algorithmicx', 'algorithm2e', 'algorithmicx*', 'algorithmic*', 'algorithm*'
                             ]
        # 原来的非贪婪正则会在嵌套的同名环境处提前结束，例如：
        # \begin{enumerate} ... \begin{enumerate} ... \end{enumerate} ... \end{enumerate}。
        # 这里使用栈按环境名称配对，确保外层环境一直匹配到自己的 end。
        structural_envs = {"document", "center", "proof", "multicols", "samepage"}
        environment_stack = []
        matched_spans = []

        for token in environment_tokens:
            kind = token.group("kind")
            env_name = token.group("name")

            if kind == "begin":
                environment_stack.append(
                    {
                        "name": env_name,
                        "start": token.start(),
                    }
                )
                continue

            if not environment_stack:
                # 保留未匹配的 end，交给重建后的全局结构校验报告。
                continue

            if environment_stack[-1]["name"] != env_name:
                # 源文档本身已经存在交叉环境时，不擅自重排源码；未匹配部分
                # 会在最终重建阶段被准确报告。
                continue

            begin = environment_stack.pop()
            matched_spans.append(
                {
                    "name": env_name,
                    "start": begin["start"],
                    "end": token.end(),
                }
            )

        unclosed_non_structural = [
            item["name"]
            for item in environment_stack
            if item["name"] not in structural_envs
        ]
        if unclosed_non_structural:
            print(
                "⚠️ Warning: Unclosed LaTeX environment(s) during parsing: "
                + ", ".join(unclosed_non_structural)
            )

        # 只提取不属于另一个可提取环境的最大环境。这样既不会重复提取嵌套
        # 环境，也能在 document/center 等结构环境中继续提取内部环境。
        selected_spans = []
        for span in sorted(
            matched_spans,
            key=lambda item: (item["start"], -item["end"]),
        ):
            if span["name"] in structural_envs:
                continue
            if any(
                parent["start"] <= span["start"]
                and span["end"] <= parent["end"]
                for parent in selected_spans
            ):
                continue
            selected_spans.append(span)

        replacements = []
        for span in selected_spans:
            self.env_count += 1
            env_name = span["name"]
            env_content = full_tex[span["start"]:span["end"]]
            placeholders_cap_in_env = re.findall(placeholder_pattern_cap, env_content)

            need_trans = True

            if env_name in no_translate_envs or env_name in LATEX_OPAQUE_ENVIRONMENTS:
                need_trans = False

            if placeholders_cap_in_env and env_name not in LATEX_PROSE_ENVIRONMENTS:
                # If there are placeholders in the environment, we do not translate it.
                need_trans = False

            placeholder = f"<PLACEHOLDER_ENV_{self.env_count}>"
            replacements.append(
                {
                    "start": span["start"],
                    "end": span["end"],
                    "placeholder": placeholder,
                }
            )
            self.envs_json.append({
                "placeholder": placeholder,
                "env_name": env_name,
                "content": env_content,
                "trans_content": '',
                "need_trans": need_trans
            })

        # 从后往前替换，避免前面的 placeholder 改变后续 span 的位置。
        for replacement in reversed(replacements):
            start = replacement["start"]
            end = replacement["end"]
            full_tex = (
                full_tex[:start]
                + replacement["placeholder"]
                + full_tex[end:]
            )
        
        return full_tex

    def _extract_captions(self, tex: str) -> str:
        """
        Extract all the captions in the full tex and genarate a json file for the captions.
        The captions are replaced with placeholders in the full tex.
        """
        full_tex = self._freeze_opaque_content(remove_comments(tex))
        command_name = r'caption|caption\*|subcaption|subcaption\*|title|keywords|abstract|icmltitle|icmltitlerunning'
        pattern_caption = get_command_pattern(command_name) # \caption{...} or \caption*{...} or \caption[...]{...}
        pattern_captionof = get_captionof_pattern()

        while True:
            result = pattern_caption.search(full_tex)
            if result is None:
                break
            self.caption_count += 1
            placeholder = f"<PLACEHOLDER_CAP_{self.caption_count}>"
            full_tex = full_tex.replace(result.group(0), placeholder, 1)
            self.captions_json.append({
                "placeholder": placeholder,
                "cap_type":result.group(1),
                "content": result.group(0),
                "trans_content": ''
            })

        while True:
            result = pattern_captionof.search(full_tex)
            if result is None:
                break
            self.caption_count += 1
            placeholder = f"<PLACEHOLDER_CAP_{self.caption_count}>"
            full_tex = full_tex[:result.start()] + placeholder + full_tex[result.end():]
            self.captions_json.append({
                "placeholder": placeholder,
                "cap_type": result.group("command"),
                "content": result.group(0),
                "trans_content": '',
            })

        # author 的姓名、邮箱和机构信息原样保留，只提取 thanks 的正文。
        author_pattern = get_command_pattern("author")
        thanks_pattern = get_command_pattern("thanks")
        replacements = []
        for author in author_pattern.finditer(full_tex):
            for thanks in thanks_pattern.finditer(author.group(0)):
                self.caption_count += 1
                placeholder = f"<PLACEHOLDER_CAP_{self.caption_count}>"
                replacements.append((
                    author.start() + thanks.start(),
                    author.start() + thanks.end(),
                    placeholder,
                ))
                self.captions_json.append({
                    "placeholder": placeholder,
                    "cap_type": "thanks",
                    "content": thanks.group(0),
                    "trans_content": '',
                })
        for start, end, placeholder in reversed(replacements):
            full_tex = full_tex[:start] + placeholder + full_tex[end:]

        return full_tex
    
    def _extract_newcommands(self, tex: str) -> str:
        """
        Extract all the newcommands in the full tex and genarate a json file for the newcommands.
        """
        full_tex = self._freeze_opaque_content(remove_comments(tex))
        fragments = []
        position = 0
        for start, end, name in get_latex_macro_definitions(full_tex):
            # 显式的 title/caption 等文字命令即使定义在宏里也独立翻译；
            # 宏名、参数规格和其余实现源码仍保持原样。
            content = self._extract_captions(full_tex[start:end])
            placeholder = f"<PLACEHOLDER_NEWCOMMAND_{len(self.newcommands_json)}>"
            fragments.extend((full_tex[position:start], placeholder))
            self.newcommands_json.append({
                "placeholder": placeholder,
                "name": name,
                "content": content,
            })
            position = end
        fragments.append(full_tex[position:])

        return "".join(fragments)
    
    def _split_to_sections(self, tex: str) -> Any:
        """
        Split the full tex to sections and genarate a json file for the sections.
        """
        full_tex = remove_comments(tex)
        command_name_section = r'section|subsection|subsubsection|section\*|subsection\*|subsubsection\*' # \chapter is not supported yet
        pattern_section = get_command_pattern(command_name_section) # \section{...} or \subsection{...} or \subsubsection{...}
        begin_document_pattern = get_begin_document_pattern() # \begin{document}
        begin_document_match = begin_document_pattern.search(full_tex)
        preamble = full_tex[:begin_document_match.start()] if begin_document_match else full_tex #...\begin{document}

        self.sections_json.append({
            "section": "-1",
            "content": preamble,
            "trans_content": preamble
        })

        document = full_tex[begin_document_match.start():] if begin_document_match else ""

        section_count = 0
        subsection_count = 0
        subsubsection_count = 0
        first_section_match = pattern_section.search(document)

        if not first_section_match: # no section found
            print("There is no section in the full tex.")
            self.sections_json.append({
                "section": "1",
                "content": document,
                "trans_content": ''
            })
            return

        before_section = document[:first_section_match.start()] if first_section_match else document # \begin{document}...\section{...}
        sections_tex = document[first_section_match.start():] if first_section_match else document
        
        self.sections_json.append({
            "section": "0",
            "content": before_section,
            "trans_content": before_section
        })

        last_pos = 0
        last_result = first_section_match

        for result in pattern_section.finditer(sections_tex):
            if last_pos != result.start():
                if last_result.group(1) == "section" or last_result.group(1) == "section*":
                    section_count += 1
                    subsection_count = 0
                    subsubsection_count = 0
                    self.sections_json.append({
                        "section": f'{section_count}',
                        "content": sections_tex[last_pos:result.start()],
                        "trans_content": ''
                    })
                elif last_result.group(1) == "subsection" or last_result.group(1) == "subsection*":
                    subsection_count += 1
                    subsubsection_count = 0
                    self.sections_json.append({
                        "section": f'{section_count}_{subsection_count}',
                        "content": sections_tex[last_pos:result.start()],
                        "trans_content": ''
                    })
                elif last_result.group(1) == "subsubsection" or last_result.group(1) == "subsubsection*":
                    subsubsection_count += 1
                    self.sections_json.append({
                        "section": f'{section_count}_{subsection_count}_{subsubsection_count}',
                        "content": sections_tex[last_pos:result.start()],
                        "trans_content": ''
                    })
            last_pos = result.start()
            last_result = result

        if last_result.group(1) == "section" or last_result.group(1) == "section*":
            section_count += 1
            subsection_count = 0
            subsubsection_count = 0
            self.sections_json.append({
                "section": f'{section_count}',
                "content": sections_tex[last_pos:],
                "trans_content": ''
            })
        elif last_result.group(1) == "subsection" or last_result.group(1) == "subsection*":
            subsection_count += 1
            subsubsection_count = 0
            self.sections_json.append({
                "section": f'{section_count}_{subsection_count}',
                "content": sections_tex[last_pos:],
                "trans_content": ''
            })
        elif last_result.group(1) == "subsubsection" or last_result.group(1) == "subsubsection*":
            subsubsection_count += 1
            self.sections_json.append({
                "section": f'{section_count}_{subsection_count}_{subsubsection_count}',
                "content": sections_tex[last_pos:],
                "trans_content": ''
            })

    def _merge_short_sections(self, min_tokens=20):
        """
        Merge sections that are too short to save the number of api requests
        """
        enc = tiktoken.encoding_for_model("gpt-4")
        fixed_sections = [
            sec for sec in self.sections_json if sec["section"] in {"-1", "0"}
        ]
        merged_sections = []
        i = 0
        sections = [
            sec for sec in self.sections_json if sec["section"] not in {"-1", "0"}
        ]

        while i < len(sections):
            combined_content = sections[i]["content"]
            combined_section_ids = [sections[i]["section"]]
            total_tokens = len(enc.encode(combined_content))
            start_section = sections[i]
            j = i + 1

            while total_tokens < min_tokens and j < len(sections):
                combined_content += "\n" + sections[j]["content"]
                combined_section_ids.append(sections[j]["section"])
                total_tokens = len(enc.encode(combined_content))
                j += 1

            if total_tokens < min_tokens and len(merged_sections) > 0:
                merged_sections[-1]["content"] += "\n" + combined_content
                merged_sections[-1]["section"] += "+" + "+".join(combined_section_ids)
                print(merged_sections[-1]["section"])
            else:
                merged_section = start_section.copy()
                merged_section["content"] = combined_content
                merged_section["section"] = "+".join(combined_section_ids)
                merged_sections.append(merged_section)

            i = j

        self.sections_json = fixed_sections + merged_sections

        
