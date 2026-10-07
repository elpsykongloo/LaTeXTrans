import dataclasses
import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional
from pathlib import Path
import sys
import asyncio
from src.utils.paths import project_path, validate_project_tree

base_dir = os.getcwd()
sys.path.append(base_dir)

from .tool_agents.base_tool_agent import BaseToolAgent
from .tool_agents.parser_agent import ParserAgent
from .tool_agents.translator_agent import TranslatorAgent 
from .tool_agents.generator_agent import GeneratorAgent
from .tool_agents.validator_agent import ValidatorAgent
from src.formats.latex.utils import extract_text_from_tex, extract_title, find_main_tex_file
from src.utils.checkpoint import CheckpointStore
import gc


STATUS_SUCCESS = "success"
STATUS_FAILED_VALIDATION = "failed_validation"
STATUS_FAILED_GENERATION = "failed_generation"
STATUS_FAILED_COMPILE = "failed_compile"


@dataclasses.dataclass
class TranslationResult:
    """单篇论文的翻译结果。"""

    status: str
    message: str = ""
    pdf_path: Optional[str] = None
    project_name: str = ""
    output_dir: str = ""
    downloads_path: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    bilingual_pdf_path: Optional[str] = None
    resumed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


class CoordinatorAgent:
    """
    The main orchestrator agent for the translation system.
    It coordinates the workflow of various tool agents based on document format
    and configuration.
    """

    def __init__(self, 
                 config: Dict[str, Any],
                 project_dir: str = None,
                 output_dir: Optional[str] = None
                 ):
        """
        Initializes the CoordinatorAgent.
        """
        self.config = config
        self.name = config.get("sys_name", "LaTeXTrans")
        self.target_language = config.get("target_language", "ch")
        self.source_language = config.get("source_language", "en")
        self.project_dir = project_dir  # Project path for parsing
        self.output_dir = output_dir  # Output directory for parsed files
        self.loop = asyncio.new_event_loop()
        self.mode = config.get("mode", 0)
        self.copy_to_downloads = _as_bool(config.get("copy_to_downloads", True), True)
        downloads_dir = str(config.get("downloads_dir") or "").strip()
        self.downloads_dir = (
            Path(os.path.expandvars(os.path.expanduser(downloads_dir)))
            if downloads_dir
            else Path.home() / "Downloads"
        )

    def run_async(self, coro):
        """
        Run asynchronous coroutines in the existing event loop
        """
        return self.loop.run_until_complete(coro)

    async def workflow_latextrans_async(self) -> "TranslationResult":
        """
        依次执行解析、翻译、校验与生成，返回结构化结果而不是只打印日志。
        """
        base_name = os.path.basename(self.project_dir)
        output_root = Path(self.output_dir).resolve()
        transed_project_dir = os.fspath(project_path(
            output_root, output_root / f"{self.target_language}_{base_name}", writing=True,
        ))
        validate_project_tree(transed_project_dir, writing=True)

        os.makedirs(transed_project_dir, exist_ok=True)

        def result(status: str, message: str = "", pdf_path: Optional[str] = None, **extra) -> TranslationResult:
            return TranslationResult(
                status=status,
                message=message,
                pdf_path=pdf_path,
                project_name=base_name,
                output_dir=transed_project_dir,
                **extra,
            )

        checkpoint = CheckpointStore(transed_project_dir, self.project_dir, self.config)
        cached = checkpoint.completed_result()
        if cached:
            print(f"🤖⏯️ {self.name}: 复用已完成译文 {cached['pdf_path']}。")
            allowed = {field.name for field in dataclasses.fields(TranslationResult)}
            cached.update(message="复用已完成的翻译", resumed=True)
            downloads_path = cached.get("downloads_path")
            if self.copy_to_downloads and (
                not downloads_path or not Path(downloads_path).is_file()
                or Path(downloads_path).parent.resolve() != self.downloads_dir.resolve()
            ):
                cached["downloads_path"] = self._copy_pdf_to_downloads_with_title(
                    cached["pdf_path"], os.path.join(transed_project_dir, base_name), f"{self.target_language}_{base_name}",
                )
            if not self.copy_to_downloads:
                cached["downloads_path"] = None
            checkpoint.data["outcome"] = cached
            checkpoint.save()
            return TranslationResult(**{key: value for key, value in cached.items() if key in allowed})

        stage = "parse"
        restored = checkpoint.restore_maps()
        if not restored:
            checkpoint.begin(stage)
            try:
                ParserAgent(config=self.config, project_dir=self.project_dir, output_dir=transed_project_dir).execute()
            except BaseException as exc:
                checkpoint.fail(stage, str(exc))
                raise
            checkpoint.stage_completed(stage, snapshot=True)
        else:
            print(f"🤖⏯️ {self.name}: 从已保存的解析与翻译片段继续 {base_name}。")

        translator_agent = None
        validator_agent = ValidatorAgent(config=self.config, project_dir=self.project_dir, output_dir=transed_project_dir)
        usage = checkpoint.read_usage()
        errors_report = []

        def translator():
            nonlocal translator_agent
            if translator_agent is None:
                translator_agent = TranslatorAgent(config=self.config, project_dir=self.project_dir,
                                                   output_dir=transed_project_dir, trans_mode=self.mode)
                translator_agent.checkpoint = checkpoint
                translator_agent._resume_usage = usage or {}
            return translator_agent

        try:
            if not restored or not checkpoint.is_complete("translate"):
                stage = "translate"
                checkpoint.begin(stage)
                await translator().execute()
                checkpoint.stage_completed(stage, snapshot=True)
            if not restored or not checkpoint.is_complete("validate"):
                stage = "validate"
                checkpoint.begin(stage)
                errors_report = validator_agent.execute()
                MAX_RETRIES = 3
                retry_count = 0
                while errors_report and retry_count < MAX_RETRIES:
                    checkpoint.invalidate_report(errors_report)
                    retry_translator = translator()
                    retry_translator.trans_mode = 1
                    retry_translator.errors_report = errors_report
                    await retry_translator.execute(error_retry_count=retry_count, Maxtry=MAX_RETRIES)
                    errors_report = validator_agent.execute(errors_report)
                    retry_count += 1
                if not errors_report:
                    checkpoint.stage_completed(stage, snapshot=True)
        except BaseException as exc:
            checkpoint.fail(stage, str(exc))
            raise
        finally:
            # 无论校验成败都记录本篇的 token 用量（翻译请求已经发生）。
            if translator_agent is not None:
                usage = self._write_usage_report(translator_agent, transed_project_dir)
            ValidatorAgent.release_cache(transed_project_dir)

        if errors_report:
            remaining_parts = [
                f"{error.get('part')}:{error.get('num_or_ph')}"
                for error in errors_report
            ]
            message = (
                "翻译校验在最大重试次数后仍未通过，已跳过 LaTeX 生成："
                f"{remaining_parts}"
            )
            print(f"❌ {message}")
            checkpoint.fail("validate", message)
            return result(STATUS_FAILED_VALIDATION, message, usage=usage)

        checkpoint.begin("compile")
        generator_agent = GeneratorAgent(config=self.config,
                                         project_dir=self.project_dir,
                                         output_dir=transed_project_dir)
        try:
            PDF_file_path = generator_agent.execute()
        except Exception as e:
            message = f"生成 LaTeX 失败：{e}"
            print(f"🤖🚧 {self.name}: Failed to translated {base_name}.{e}")
            checkpoint.fail("compile", message)
            return result(STATUS_FAILED_GENERATION, message, usage=usage)

        if not PDF_file_path:
            message = "LaTeX 编译失败，未生成 PDF（请查看 build_* 目录中的日志）。"
            print(f"🤖🚧 {self.name}: Failed to translated {base_name}. {message}")
            checkpoint.fail("compile", message)
            return result(STATUS_FAILED_COMPILE, message, usage=usage)

        new_PDF_path = os.path.join(transed_project_dir, f"{self.target_language}_{base_name}.pdf")
        if Path(PDF_file_path).resolve() != Path(new_PDF_path).resolve():
            shutil.move(PDF_file_path, new_PDF_path)
        titled_copy_path = None
        if self.copy_to_downloads:
            titled_copy_path = self._copy_pdf_to_downloads_with_title(
                pdf_path=new_PDF_path,
                translated_latex_dir=os.path.join(transed_project_dir, base_name),
                fallback_name=f"{self.target_language}_{base_name}",
            )
            if titled_copy_path:
                print(f"🤖📄 {self.name}: Copied titled PDF to {titled_copy_path}.")
        print(f"🤖🎉 {self.name}: Successfully translated {base_name} to {new_PDF_path}.")
        outcome = result(
            STATUS_SUCCESS,
            "翻译完成",
            new_PDF_path,
            downloads_path=titled_copy_path,
            usage=usage,
            resumed=restored,
        )
        checkpoint.complete(outcome.to_dict())
        return outcome

    def _write_usage_report(self, translator_agent: Any, output_dir: str) -> Optional[Dict[str, Any]]:
        """
        容错地读取翻译代理的 token 用量（usage_summary() 或 usage 属性），
        写入 <输出目录>/usage.json 并打印一行汇总；不存在或出错时静默跳过。
        """
        try:
            usage: Any = None
            summary_fn = getattr(translator_agent, "usage_summary", None)
            if callable(summary_fn):
                usage = summary_fn()
            if usage is None:
                usage = getattr(translator_agent, "usage", None)
                if callable(usage):
                    usage = usage()
            if usage is None:
                return None
            if not isinstance(usage, dict):
                if hasattr(usage, "snapshot"):
                    usage = usage.snapshot()
                elif hasattr(usage, "to_dict"):
                    usage = usage.to_dict()
                elif dataclasses.is_dataclass(usage) and not isinstance(usage, type):
                    usage = dataclasses.asdict(usage)
                elif hasattr(usage, "__dict__"):
                    usage = {k: v for k, v in vars(usage).items() if not k.startswith("_")}
                else:
                    usage = {"usage": usage}
            if not isinstance(usage, dict):
                usage = {"usage": usage}

            usage_path = os.path.join(output_dir, "usage.json")
            with open(usage_path, "w", encoding="utf-8") as f:
                json.dump(usage, f, ensure_ascii=False, indent=2, default=str)

            parts = [
                f"{key}={usage[key]}"
                for key in ("requests", "prompt_tokens", "completion_tokens", "total_tokens")
                if key in usage
            ]
            summary = ", ".join(parts) if parts else f"已写入 {usage_path}"
            print(f"🤖📊 {self.name}: Token 用量 {os.path.basename(self.project_dir)}：{summary}")
            return usage
        except Exception as e:
            print(f"🤖⚠️ {self.name}: 记录 token 用量失败：{e}")
            return None

    def _copy_pdf_to_downloads_with_title(
        self,
        pdf_path: str,
        translated_latex_dir: str,
        fallback_name: str,
    ) -> Optional[str]:
        """
        将译文 PDF 额外复制到用户下载目录，并使用译文标题作为文件名。
        """
        try:
            title = self._extract_translated_title(translated_latex_dir) or fallback_name
            filename = self._sanitize_windows_filename(title)
            if not filename:
                filename = fallback_name

            downloads_dir = self.downloads_dir
            downloads_dir.mkdir(parents=True, exist_ok=True)
            target_path = self._unique_pdf_path(downloads_dir / f"{filename}.pdf")
            shutil.copy2(pdf_path, target_path)
            return str(target_path)
        except Exception as e:
            print(f"🤖⚠️ {self.name}: Failed to copy titled PDF to Downloads: {e}")
            return None

    def _extract_translated_title(self, translated_latex_dir: str) -> str:
        """
        从生成后的译文 LaTeX 主文件中提取真实译文标题。
        """
        main_tex_path = find_main_tex_file(translated_latex_dir)
        if not main_tex_path or not os.path.exists(main_tex_path):
            return ""

        with open(main_tex_path, "r", encoding="utf-8") as f:
            latex_code = f.read()

        raw_title = extract_title(latex_code)
        if not raw_title or raw_title == "No title":
            return ""

        text_title = extract_text_from_tex(raw_title)
        text_title = re.sub(r"\s+", " ", text_title).strip()
        return text_title

    def _sanitize_windows_filename(self, filename: str) -> str:
        """
        清理 Windows 文件名非法字符，并控制文件名长度。
        """
        sanitized = filename.replace(":", "：")
        sanitized = re.sub(r'[<>"/\\|?*\x00-\x1f]', "", sanitized)
        sanitized = re.sub(r"：\s+", "：", sanitized)
        sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")

        reserved_names = {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        }
        if sanitized.upper() in reserved_names:
            sanitized = f"{sanitized}_"

        return sanitized[:180].strip(" .")

    def _unique_pdf_path(self, path: Path) -> Path:
        """
        避免覆盖下载目录中的同名文件。
        """
        if not path.exists():
            return path

        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        index = 1
        while True:
            candidate = parent / f"{stem}_{index}{suffix}"
            if not candidate.exists():
                return candidate
            index += 1


    def workflow_latextrans(self) -> "TranslationResult":
        """
        Initialize the tool agent and execute the LaTeX conversion workflow 
        (with event loop security management)

        返回 TranslationResult；解析/翻译阶段的异常仍会向上抛出。
        """

        if hasattr(self, 'loop') and not self.loop.is_closed():
            self.loop.close()  

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            return self.loop.run_until_complete(self.workflow_latextrans_async())

        finally:
            # Complete all asynchronous resource recycling
            if tasks := asyncio.all_tasks(self.loop):
                for task in tasks:
                    task.cancel()
                self.loop.run_until_complete(
                    asyncio.gather(*tasks, return_exceptions=True)
                )

            self.loop.run_until_complete(self.loop.shutdown_asyncgens())

            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            # 每个工作线程各自的事件循环用完即关闭，避免并发处理多篇时泄漏句柄。
            self.loop.close()
            asyncio.set_event_loop(None)
