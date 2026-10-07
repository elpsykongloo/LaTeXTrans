import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import find_main_tex_file


_ENVIRONMENT_ERROR = re.compile(
    r"(?:File\s+[`'\"].+?[`'\"]\s+not found|I can't find file|"
    r"(?:font[^\n]*(?:cannot be found|not found|not loadable))|"
    r"Package fontspec Error|(?:requires?[^\n]*(?:XeTeX|LuaTeX))|"
    r"Permission denied|Access is denied|No space left)",
    re.IGNORECASE,
)


def _join_wrapped_diagnostics(lines: list[str], main_tex: Path, project_dir: Path) -> list[str]:
    """Recover only proven project file:line paths split by TeX's print width."""
    path_start = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\]|\.\.?[/\\]|[^:\n]+[/\\])")
    file_line = re.compile(r"^(.+?\.(?:tex|sty|cls|bbl)):(\d+):\s*.+$", re.IGNORECASE)
    result = []
    index = 0
    while index < len(lines):
        first = lines[index]
        joined = None
        if path_start.match(first) and not file_line.match(first):
            candidate_text = first
            for following_index in range(index + 1, min(index + 16, len(lines))):
                following = lines[following_index]
                if not following or following[0].isspace() or following.startswith(("! ", "l.")):
                    break
                candidate_text += following
                if len(candidate_text) > 4096:
                    break
                match = file_line.match(candidate_text)
                if match:
                    path = Path(match[1].strip('"'))
                    if not path.is_absolute():
                        path = main_tex.parent / path
                    path = path.resolve()
                    # A log banner or prose cannot become a filename merely by
                    # concatenation. The resulting path must identify a real local file.
                    if path.is_relative_to(project_dir) and path.is_file():
                        joined = (candidate_text, following_index + 1)
                    break
                if re.search(r":\d+:", following):
                    break
        if joined:
            result.append(joined[0])
            index = joined[1]
        else:
            result.append(first)
            index += 1
    return result


def extract_latex_errors(text: str, main_tex: Path, project_dir: Path) -> list[Dict[str, Any]]:
    """从 TeX 的 file:line 和 !/l.N 日志中提取错误；不把普通 warning 当错误。"""
    project_dir = project_dir.resolve()
    main_tex = main_tex.resolve()
    file_line = re.compile(r"^(.+?\.(?:tex|sty|cls|bbl)):(\d+):\s*(.+)$", re.IGNORECASE)
    scopes = []
    errors = []
    lines = _join_wrapped_diagnostics(text.replace("\r", "").splitlines(), main_tex, project_dir)

    def diagnostic(file: str, line: Optional[int], message: str):
        candidate = Path(file.strip().strip('"'))
        if not candidate.is_absolute():
            candidate = main_tex.parent / candidate
        candidate = candidate.resolve()
        try:
            name = candidate.relative_to(project_dir).as_posix()
            local = True
        except ValueError:
            name, local = str(candidate), False
        environment = bool(_ENVIRONMENT_ERROR.search(message))
        item = {
            "file": name,
            "line": line,
            "message": message[:1200],
            "environment_error": environment,
            "repairable": local and candidate.suffix.lower() == ".tex" and bool(line) and not environment,
        }
        if item not in errors:
            errors.append(item)

    for index, line in enumerate(lines):
        match = file_line.match(line.strip())
        if match and not re.search(r"\b(?:Warning|Info):|^(?:Overfull|Underfull)", match[3]):
            diagnostic(match[1], int(match[2]), match[3])
        elif line.startswith("! "):
            message = [line[2:].strip()]
            number = None
            for following in lines[index + 1:index + 12]:
                at_line = re.match(r"l\.(\d+)\s*(.*)", following)
                if at_line:
                    number = int(at_line[1])
                    break
                if following.startswith("! "):
                    break
                if following.strip():
                    message.append(following.strip())
            current_file = next((file for file in reversed(scopes) if file), str(main_tex))
            diagnostic(current_file, number, "\n".join(message))

        # TeX 用括号标记输入文件的进入/退出；普通消息的括号也占据一个 scope。
        for token in re.finditer(r"\((\"[^\"\n]+\.(?:tex|sty|cls|bbl)\"|[^()\s]+\.(?:tex|sty|cls|bbl))?|\)", line):
            if token[0].startswith("("):
                scopes.append(token[1])
            elif scopes:
                scopes.pop()
    return errors


class LaTexCompiler:
    """负责选择合适的 LaTeX 引擎并严格验证编译结果。"""

    # 旧模板无条件使用 pdfTeX primitives 时，XeTeX/LuaTeX 会把它们的参数
    # 当正文或报 Undefined control sequence。只补齐当前引擎中未定义的命令。
    _UNICODE_PRETEX = (
        r"\providecommand\pdfglyphtounicode[2]{}"
        r"\providecommand\pdfcatalog[1]{}"
        r"\providecommand\pdfinfo[1]{}"
        r"\providecommand\pdfpageattr[1]{}"
        r"\providecommand\pdfpagesattr[1]{}"
        r"\providecommand\epstopdfDeclareGraphicsRule[4]{}"
        r"\ifdefined\pdfcompresslevel\else\newcount\pdfcompresslevel\fi"
        r"\ifdefined\pdfoptionpdfminorversion\else\newcount\pdfoptionpdfminorversion\fi"
        r"\ifdefined\pdfminorversion\else\newcount\pdfminorversion\fi"
        r"\ifdefined\pdfoutput\else\newcount\pdfoutput\fi"
        r"\ifdefined\pdfgentounicode\else\newcount\pdfgentounicode\fi"
    )

    def __init__(self, output_latex_dir: str):
        self.output_latex_dir = output_latex_dir
        self.failures: list[Dict[str, Any]] = []
        self.last_failure: Optional[Dict[str, Any]] = None

    @staticmethod
    def _tex_environment() -> Dict[str, str]:
        """Keep Web2C diagnostics intact even under long Windows output paths."""
        env = dict(os.environ)
        try:
            width = int(env.get("max_print_line", 0))
        except ValueError:
            width = 0
        env["max_print_line"] = str(max(10000, width))
        return env

    def compile(self) -> Optional[str]:
        """编译翻译后的文档，成功时返回本次生成的 PDF 路径。"""
        self.failures = []
        self.last_failure = None
        self._remove_success_marker()
        tex_file = find_main_tex_file(self.output_latex_dir)
        if not tex_file:
            self._record_failure("", None, None, reason="未找到待编译的主 LaTeX 文件。", read_log=False)
            print("⚠️ 未找到待编译的主 LaTeX 文件。")
            return None

        engines = self._preferred_engines(tex_file)
        for engine in engines:
            out_dir = os.path.join(self.output_latex_dir, f"build_{engine}")
            pdf_path = self._compile_with_engine(tex_file, out_dir, engine)
            if pdf_path:
                self.last_failure = None
                self._write_success_marker()
                print(f"✅ 已使用 {engine} 成功生成 PDF：{pdf_path}")
                return pdf_path

        # 直接引擎的实际 TeX 错误比随后缺少 latexmk 的启动错误更适合定位。
        self.last_failure = next(
            (failure for failure in reversed(self.failures) if failure["kind"] == "tex_error"),
            self.last_failure,
        )
        print(f"❌ 所有候选 LaTeX 引擎均编译失败：{', '.join(engines)}")
        return None

    def compile_ja(self) -> Optional[str]:
        """编译日文文档。

        日文导言区由 add_ja_package 配置：默认 xeCJK（自动选择 XeLaTeX，与中文
        一致）；原稿自带 luatexja 时自动选择 LuaLaTeX。引擎选择统一由
        _preferred_engines 完成。
        """
        return self.compile()

    def compile_source(self, pdf_dir: Optional[str]) -> Optional[str]:
        """将源文档编译到指定目录，并严格检查引擎返回码。"""
        self.failures = []
        self.last_failure = None
        if pdf_dir is None:
            pdf_dir = self.output_latex_dir
        os.makedirs(pdf_dir, exist_ok=True)

        tex_file = find_main_tex_file(self.output_latex_dir)
        if not tex_file:
            print("⚠️ 未找到待编译的主 LaTeX 文件。")
            return None

        for engine in self._preferred_engines(tex_file):
            pdf_path = self._compile_with_engine(tex_file, pdf_dir, engine)
            if pdf_path:
                self.last_failure = None
                print(f"✅ 已使用 {engine} 生成源 PDF：{pdf_path}")
                return pdf_path

        print("❌ 源文档的所有候选引擎均编译失败。")
        return None

    def _preferred_engines(self, tex_file: str) -> list[str]:
        """根据整个项目是否包含中文或 Unicode 宏包选择引擎顺序。"""
        source = self._read_tex_project()
        package_markers = (
            "ctex",
            "xeCJK",
            "CJKutf8",
            "fontspec",
            "unicode-math",
            "luatexja",
        )
        contains_cjk = bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", source))
        uses_unicode_package = any(marker in source for marker in package_markers)

        # xeCJK 只能由 XeTeX 驱动，不能把 LuaLaTeX 作为后备引擎。
        if "luatexja" in source:
            return ["lualatex"]
        if "xeCJK" in source:
            return ["xelatex"]
        if "CJKutf8" in source:
            return ["pdflatex", "xelatex"]
        if "ctex" in source or contains_cjk:
            return ["xelatex"]
        if uses_unicode_package:
            return ["xelatex", "lualatex"]
        return ["pdflatex", "xelatex"]

    def _read_tex_project(self) -> str:
        """读取项目中的 TeX 文件，用于检测是否需要 Unicode 引擎。"""
        chunks = []
        root = Path(self.output_latex_dir)
        if not root.exists():
            return ""

        for tex_path in root.rglob("*.tex"):
            if any(part.startswith("build_") for part in tex_path.parts):
                continue
            try:
                chunks.append(tex_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(chunks)

    def _compile_with_engine(self, tex_file: str, out_dir: str, engine: str) -> Optional[str]:
        """执行一次可审计的 latexmk 编译。"""
        tex_path = Path(tex_file).resolve()
        if not tex_path.is_relative_to(Path(self.output_latex_dir).resolve()):
            self._record_failure(engine, tex_path, None, reason="主 TeX 文件位于项目目录之外。", read_log=False)
            return None
        output_path = Path(out_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        expected_pdf = output_path / f"{tex_path.stem}.pdf"

        # 删除同名旧产物，避免失败后误返回上一次的 PDF。
        if expected_pdf.exists():
            try:
                expected_pdf.unlink()
            except OSError as exc:
                print(f"⚠️ 无法删除旧 PDF，取消本次 {engine} 编译：{exc}")
                self._record_failure(engine, tex_path, output_path, reason=f"无法删除旧 PDF：{exc}", environment=True, read_log=False)
                return None

        # 错误诊断只读取本次编译的日志，防止旧日志触发错误的局部修复。
        log_path = output_path / f"{tex_path.stem}.log"
        try:
            log_path.unlink(missing_ok=True)
            if engine == "xelatex":
                (output_path / f"{tex_path.stem}.xdv").unlink(missing_ok=True)
        except OSError as exc:
            self._record_failure(engine, tex_path, output_path, reason=f"无法删除旧日志：{exc}", environment=True, read_log=False)
            return None

        if self._can_use_fast_path(tex_path):
            pdf_path = self._fast_compile(tex_path, output_path, engine, expected_pdf)
            if pdf_path:
                return pdf_path
            print(f"⚠️ {engine} 直接编译未成功，回退到 latexmk。")

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
        if engine in {"xelatex", "lualatex"}:
            command.insert(-1, "-usepretex=" + self._UNICODE_PRETEX)

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(tex_path.parent),
                env=self._tex_environment(),
                # 防止 MiKTeX 安装提示或 TeX 死循环让整个批处理永久挂起。
                timeout=900,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            self._record_failure(engine, tex_path, output_path, reason="编译超过 900 秒。", stdout=exc.stdout, stderr=exc.stderr, environment=True, read_log=False)
            print(f"❌ {engine} 编译超过 900 秒，已终止。")
            return None
        except OSError as exc:
            self._record_failure(engine, tex_path, output_path, reason=f"无法启动 latexmk：{exc}", environment=True, read_log=False)
            print(f"❌ 无法启动 {engine}：{exc}")
            return None

        if completed.returncode != 0:
            self._record_failure(engine, tex_path, output_path, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
            print(f"❌ {engine} 返回非零状态码 {completed.returncode}。")
            self._print_compile_tail(completed.stdout, completed.stderr)
            if expected_pdf.exists():
                try:
                    expected_pdf.unlink()
                except OSError:
                    pass
            return None

        if not expected_pdf.is_file() or expected_pdf.stat().st_size == 0:
            self._record_failure(engine, tex_path, output_path, returncode=0, reason="引擎返回成功但没有生成有效 PDF。", read_log=False)
            print(f"❌ {engine} 返回成功但没有生成有效 PDF：{expected_pdf}")
            return None

        return str(expected_pdf)

    # 标签、引用、目录的变化由辅助文件中的语义记录判断；
    # 这里只识别宏包明确要求重跑的提示（如 rerunfilecheck 的书签）。
    _RERUN_PATTERN = re.compile(r"Rerun to get|Please rerun LaTeX|Rerun LaTeX")
    # 这两类提示完全来自 .aux，已由语义记录覆盖；它们按行序比较，会误报。
    _AUX_DERIVED_WARNING = re.compile(r"(Label|Citation)\(s\) may have changed")
    _AUX_EXTS = (".aux", ".toc", ".lof", ".lot", ".out")

    def _can_use_fast_path(self, tex_path: Path) -> bool:
        """不需要 biber/makeindex/glossaries 时直接调用引擎，省去 latexmk 的额外开销。

        arXiv 源码通常自带 .bbl；没有时由 _needs_bibtex 在首轮后调用 bibtex。
        """
        source = self._read_tex_project()
        uses_biblatex = re.search(r"\\addbibresource\s*[\[{]|\\usepackage\s*(\[[^\]]*\])?\s*\{[^}]*biblatex", source)
        if uses_biblatex and not (tex_path.parent / f"{tex_path.stem}.bbl").is_file():
            return False
        return not re.search(r"\\(printindex|makeindex|makeglossaries|printglossary)\b", source)

    def _needs_bibtex(self, tex_path: Path) -> bool:
        if (tex_path.parent / f"{tex_path.stem}.bbl").is_file():
            return False
        return bool(re.search(r"\\bibliography\s*\{", self._read_tex_project()))

    @staticmethod
    def _aux_records(text: str, commands: str) -> tuple:
        """Parse relevant TeX commands and brace arguments, ignoring comments/spacing."""
        text = re.sub(r"(?<!\\)%[^\n]*", "", text)
        records = []
        for match in re.finditer(r"\\(" + commands + r")(?![A-Za-z@])", text):
            index = match.end()
            arguments = []
            while index < len(text):
                while index < len(text) and text[index].isspace():
                    index += 1
                if index >= len(text) or text[index] not in "{[":
                    break
                start = index + 1
                stack = ["}" if text[index] == "{" else "]"]
                index += 1
                while index < len(text) and stack:
                    character = text[index]
                    if character == "\\":
                        index += 2
                        continue
                    if character in "{[":
                        stack.append("}" if character == "{" else "]")
                    elif character == stack[-1]:
                        stack.pop()
                    index += 1
                if stack:
                    # Incomplete auxiliary data must not look converged.
                    arguments.append(("incomplete", text[start:]))
                    break
                arguments.append(re.sub(r"\s+", " ", text[start:index - 1]).strip())
            records.append((match[1], tuple(arguments)))
        return tuple(records)

    @classmethod
    def _bibtex_inputs(cls, output_path: Path, tex_path: Path) -> tuple:
        """Bibliography command arguments; meaningful changes require another BibTeX run."""
        try:
            aux = (output_path / f"{tex_path.stem}.aux").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ()
        return cls._aux_records(aux, "citation|bibdata|bibstyle")

    def _run_bibtex(self, tex_path: Path, output_path: Path) -> bool:
        """在输出目录运行 bibtex，并让它能找到源码目录中的 .bib/.bst。"""
        env = self._tex_environment()
        for var in ("BIBINPUTS", "BSTINPUTS"):
            # 末尾的分隔符保留 kpathsea 的默认搜索路径。
            env[var] = os.pathsep.join([str(tex_path.parent), env.get(var, "")])
        try:
            completed = subprocess.run(
                ["bibtex", tex_path.stem],
                check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(output_path), env=env, timeout=900, stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._record_failure("bibtex", tex_path, output_path, reason=f"无法运行 bibtex：{exc}", environment=True, read_log=False)
            print(f"❌ 无法运行 bibtex：{exc}")
            return False
        # bibtex 对缺失条目等警告返回 1，仍会生成可用的 .bbl；只有致命错误才回退。
        if completed.returncode > 1 or not (output_path / f"{tex_path.stem}.bbl").is_file():
            self._record_failure("bibtex", tex_path, output_path, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr, reason="BibTeX 未生成可用的参考文献文件。", environment=True, read_log=False)
            self._print_compile_tail(completed.stdout, completed.stderr)
            return False
        return True

    def _run_engine(self, tex_path: Path, output_path: Path, engine: str, final: bool):
        command = [
            engine,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            f"-output-directory={output_path}",
        ]
        if not final:
            # 首轮只为生成 .aux，不输出 PDF。
            command.insert(1, "-no-pdf" if engine == "xelatex" else "-draftmode")
        if engine in {"xelatex", "lualatex"}:
            command.append(f"-jobname={tex_path.stem}")
            command.append(self._UNICODE_PRETEX + r'\input{"' + tex_path.as_posix() + '"}')
        else:
            command.append(str(tex_path))
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(tex_path.parent),
                env=self._tex_environment(),
                timeout=900,
                stdin=subprocess.DEVNULL,
            )
            if completed.returncode != 0:
                self._record_failure(engine, tex_path, output_path, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
            return completed
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._record_failure(engine, tex_path, output_path, reason=f"无法完成编译：{exc}", environment=True, read_log=False)
            print(f"❌ 无法完成 {engine} 编译：{exc}")
            return None

    def _aux_snapshot(self, output_path: Path, tex_path: Path) -> Dict[str, tuple]:
        """Read the label/citation/TOC/bookmark state needed for convergence, without byte checks."""
        snapshot = {}
        parsed_aux = {}
        aux_commands = r"newlabel|bibcite|citation|bibdata|bibstyle|@writefile|@input|abx@aux@[A-Za-z@]+"
        ordered_commands = r"contentsline|addvspace|BOOKMARK|babel@toc|select@language"
        for ext in self._AUX_EXTS:
            path = output_path / f"{tex_path.stem}{ext}"
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                records = self._aux_records(content, aux_commands if ext == ".aux" else ordered_commands)
                if ext == ".aux":
                    parsed_aux[path.resolve()] = records
                    # .aux 行序不影响标签查询（目录顺序由 .toc 单独保留）。
                    records = tuple(sorted(set(records), key=repr))
                snapshot[ext] = records

        # \include 的标签保存在子 .aux 中，主文件的 \@input 本身可能始终不变。
        visited = set()
        pending = [output_path / f"{tex_path.stem}.aux"]
        while pending and len(visited) < 256:
            path = pending.pop().resolve()
            if path in visited or not path.is_relative_to(output_path.resolve()):
                continue
            visited.add(path)
            try:
                records = parsed_aux[path] if path in parsed_aux else self._aux_records(path.read_text(encoding="utf-8", errors="replace"), aux_commands)
            except OSError:
                continue
            if path.name != f"{tex_path.stem}.aux" or path.parent != output_path.resolve():
                snapshot[path.relative_to(output_path.resolve()).as_posix()] = tuple(sorted(set(records), key=repr))
            for command, arguments in records:
                if command == "@input" and arguments and isinstance(arguments[0], str):
                    pending.append(output_path / arguments[0])
        return snapshot

    def _needs_rerun(self, output_path: Path, tex_path: Path, before: Dict[str, tuple]) -> bool:
        if before != self._aux_snapshot(output_path, tex_path):
            return True
        try:
            log = (output_path / f"{tex_path.stem}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        # 日志按 79 列折行：以非空白开头的行是新消息，其余行拼回上一条。
        messages = re.split(r"\n(?=\S)", log.replace("\r", ""))
        return any(
            self._RERUN_PATTERN.search(message.replace("\n", ""))
            and not self._AUX_DERIVED_WARNING.search(message.replace("\n", ""))
            for message in messages
        )

    def _first_pass_is_final(self, output_path: Path, tex_path: Path) -> bool:
        """首轮没有写出任何需要回读的交叉引用/目录信息时，不必再排版一轮。"""
        snapshot = self._aux_snapshot(output_path, tex_path)
        if any(records for ext, records in snapshot.items() if ext != ".aux"):
            return False
        return not any(
            command in {"newlabel", "bibcite", "@writefile"} or command.startswith("abx@aux")
            for command, _ in snapshot.get(".aux", ())
        )

    _MAX_PASSES = 5

    def _fast_compile(self, tex_path: Path, output_path: Path, engine: str, expected_pdf: Path) -> Optional[str]:
        """按 latexmk 的收敛规则直接调用引擎。

        xelatex 各轮都只输出 .xdv，收敛后统一转换一次 PDF；
        其他引擎首轮用 draftmode，后续轮次直接输出 PDF。
        """
        xetex = engine == "xelatex"

        def run_pass(final: bool) -> bool:
            completed = self._run_engine(tex_path, output_path, engine, final=final)
            if completed is None or completed.returncode != 0:
                if completed is not None:
                    self._print_compile_tail(completed.stdout, completed.stderr)
                return False
            return True

        if not run_pass(final=False):
            return None
        passes = 1
        rerun = not self._first_pass_is_final(output_path, tex_path)

        bib_inputs = None
        if self._needs_bibtex(tex_path):
            bib_inputs = self._bibtex_inputs(output_path, tex_path)
            if not self._run_bibtex(tex_path, output_path):
                return None
            rerun = True

        pdf_written = False
        while rerun and passes < self._MAX_PASSES:
            before = self._aux_snapshot(output_path, tex_path)
            if not run_pass(final=not xetex):
                return None
            passes += 1
            pdf_written = not xetex
            rerun = self._needs_rerun(output_path, tex_path, before)
            if bib_inputs is not None and self._bibtex_inputs(output_path, tex_path) != bib_inputs:
                bib_inputs = self._bibtex_inputs(output_path, tex_path)
                if not self._run_bibtex(tex_path, output_path):
                    return None
                rerun = True

        if xetex:
            xdv = output_path / f"{tex_path.stem}.xdv"
            try:
                converted = subprocess.run(
                    ["xdvipdfmx", "-q", "-o", str(expected_pdf), str(xdv)],
                    check=False, capture_output=True, cwd=str(tex_path.parent),
                    timeout=900, stdin=subprocess.DEVNULL,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                self._record_failure("xdvipdfmx", tex_path, output_path, reason=f"无法转换 XDV：{exc}", environment=True, read_log=False)
                return None
            if converted.returncode != 0:
                self._record_failure("xdvipdfmx", tex_path, output_path, returncode=converted.returncode, stdout=converted.stdout, stderr=converted.stderr, reason="XDV 转 PDF 失败。", environment=True, read_log=False)
                return None
        elif not pdf_written and not run_pass(final=True):
            return None

        if expected_pdf.is_file() and expected_pdf.stat().st_size > 0:
            return str(expected_pdf)
        self._record_failure(engine, tex_path, output_path, reason="引擎没有生成有效 PDF。", read_log=False)
        return None

    def _record_failure(
        self, engine: str, tex_path: Optional[Path], output_path: Optional[Path],
        returncode: Optional[int] = None, stdout: Any = "", stderr: Any = "",
        reason: str = "", environment: bool = False, read_log: bool = True,
    ) -> None:
        """保留结构化诊断供 UI、审计与有界局部修复使用。"""
        def as_text(value):
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value if isinstance(value, str) else ""

        output = "\n".join(filter(None, [as_text(stdout), as_text(stderr)]))
        log_path = output_path / f"{tex_path.stem}.log" if output_path and tex_path else None
        if read_log and log_path:
            try:
                # 日志可能很长，只需要读取有错误的消息；输出给报告时限制尾部长度。
                output = log_path.read_text(encoding="utf-8", errors="replace") + "\n" + output
            except OSError:
                pass
        errors = extract_latex_errors(output, tex_path, Path(self.output_latex_dir)) if tex_path else []
        environment = environment or any(error["environment_error"] for error in errors)
        kind = "environment" if environment else ("tex_error" if errors else "compile_failure")
        failure = {
            "engine": engine,
            "kind": kind,
            "returncode": returncode,
            "message": reason or (errors[0]["message"] if errors else f"{engine} 编译失败。"),
            "errors": errors,
            "log_path": str(log_path) if log_path else None,
            "output_tail": output[-4000:],
        }
        self.failures.append(failure)
        self.last_failure = failure

    @staticmethod
    def _print_compile_tail(stdout: str, stderr: str, limit: int = 3000) -> None:
        """失败时输出命令尾部，便于 UI 和终端直接定位首要原因。"""
        output = "\n".join(part for part in (stdout, stderr) if part)
        if output:
            print(output[-limit:])

    def _remove_success_marker(self) -> None:
        marker = Path(self.output_latex_dir) / "success.txt"
        if marker.exists():
            try:
                marker.unlink()
            except OSError:
                pass

    def _write_success_marker(self) -> None:
        marker = Path(self.output_latex_dir) / "success.txt"
        marker.write_text("Compilation successful\n", encoding="utf-8")

    # 保留旧的私有方法入口，避免外部脚本调用时发生不必要的兼容性破坏。
    def _compile_with_pdflatex(
        self,
        tex_file: str,
        out_dir: str,
        engine: str = "pdflatex",
    ) -> Optional[str]:
        return self._compile_with_engine(tex_file, out_dir, engine)

    def _compile_with_xelatex(
        self,
        tex_file: str,
        out_dir: str,
        engine: str = "xelatex",
    ) -> Optional[str]:
        return self._compile_with_engine(tex_file, out_dir, engine)

    def _compile_with_lualatex(
        self,
        tex_file: str,
        out_dir: str,
        engine: str = "lualatex",
    ) -> Optional[str]:
        return self._compile_with_engine(tex_file, out_dir, engine)
