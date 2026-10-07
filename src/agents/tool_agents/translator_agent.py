from typing import Dict, Any, List, Optional
from src.agents.tool_agents.base_tool_agent import BaseToolAgent, TruncatedResponseError
#from TransLatex.src.formats.latex.prompts import *
import src.formats.latex.prompts as pm
from src.formats.latex.utils import *
from pathlib import Path
from importlib import resources as importlib_resources
import sys
import os
import re
import regex
import json
import asyncio
import aiohttp
import requests
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.utils.progress import st
from src.utils.rate_limit import AdaptiveConcurrencyLimiter
from src.utils.usage import UsageTracker
from src.utils.checkpoint import atomic_write_json, read_json
from src.formats.latex.chunking import split_translation_chunks, has_translatable_text

base_dir = os.getcwd()
sys.path.append(base_dir)


class _NullWidget:
    """前端组件不可用时的占位对象：任何方法调用都是空操作。"""

    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


_NULL_WIDGET = _NullWidget()


def _ui(action, *args, **kwargs):
    """调用进度条/状态栏。

    多个项目在不同线程中并发翻译，不能再通过改写全局 ``sys.stderr``
    屏蔽前端输出（会互相覆盖并泄漏文件句柄）。进度展示失败只影响界面，
    不应中断翻译，因此在这里吞掉异常并返回空操作对象。
    """
    try:
        result = action(*args, **kwargs)
    except Exception:
        return _NULL_WIDGET
    return _NULL_WIDGET if result is None else result


_SECTION_TITLE_PATTERN = re.compile(
    r"\\(?:chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*"
    r"\{((?:[^{}]|\{[^{}]*\})*)\}"
)


class TranslatorAgent(BaseToolAgent):
    def __init__(self, 
                 config: Dict[str, Any], 
                 trans_mode: int = 0,
                 project_dir: Optional[str] = None,
                 output_dir: Optional[str] = None,
                 errors_report: Optional[List[Dict]] = None,
                 ):
        super().__init__(agent_name="TranslatorAgent", config=config)
        self.config = config
        self.update_term = str(config.get("update_term", "False")).strip().lower() == "true"
        # self.update_term = config.get("update_term", False)
        self.model = config["llm_config"].get("model", "gpt-4o")
        self.base_url = self.get_chat_completions_url()
        self.API_KEY = config["llm_config"].get("api_key", None)
        self.concurrency_limit = self.get_concurrency_limit()
        self.user_term = config.get("user_term", None)
        self.target_language = config.get("target_language", "ch")
        self.category = config.get("category", None)
        self.project_dir = project_dir  # Project path for parsing
        self.output_dir = output_dir  # Output directory for parsed files
        self.fail_section_nums = []
        self.fail_caption_phs = []
        self.fail_env_phs = []
        self.have_fail_parts = False
        self.errors_report = errors_report if errors_report is not None else []
        self.trans_mode = trans_mode if trans_mode is not None else 0
        # self.term_dict = config.get("term_dict", {})  # Dictionary for terminology translation
        # 术语表只保存真实术语；结构占位符单独存放，避免污染提示词。
        self.term_dict = {}
        self.placeholders: List[str] = []
        self._term_patterns: Dict[str, "re.Pattern"] = {}
        self.summary = ''
        self.prev_text = ''
        self.prev_transed_text = ''
        self.currant_content = ''
        llm_config = self.get_llm_config()
        # 片段翻译后立即校验并就地重翻的次数，避免等整篇结束后再串行重试。
        self.inline_retries = max(0, int(llm_config.get("inline_retries", 2)))
        self.retry_backoff = max(0.0, float(llm_config.get("retry_backoff", 1.0)))
        # 备份请求：超过预计耗时 hedge_factor 倍（且不少于 hedge_min_delay 秒）时触发；0 表示关闭。
        self._hedge_factor = max(0.0, float(llm_config.get("hedge_factor", 2.0)))
        self._hedge_min_delay = max(0.5, float(llm_config.get("hedge_min_delay", 3.0)))
        # 估算正常耗时的输入速率（字符/秒）：实测 1500 字符约 2 秒。
        self._hedge_chars_per_sec = self._get_float_llm_config("hedge_chars_per_sec", 900.0, minimum=1.0)
        self.max_tokens = self.get_max_tokens()
        self.temperature = self.get_temperature()
        # 每个片段最多注入的相关术语条数；0 表示不注入术语表。
        try:
            self.glossary_max_terms = max(0, int(llm_config.get("glossary_max_terms", 50)))
        except (TypeError, ValueError):
            self.glossary_max_terms = 50
        # 只剩公式/结构、没有可翻译文字的片段直接保留原文，不请求模型。
        self.skip_untranslatable = str(
            llm_config.get("skip_untranslatable_chunks", True)
        ).strip().lower() not in {"false", "0", "no"}
        # 可选的论文级上下文（标题 + 摘要/概要 + 章节标题），默认关闭。
        self.use_context = str(llm_config.get("use_context", False)).strip().lower() == "true"
        self.context_summary = str(llm_config.get("context_summary", False)).strip().lower() == "true"
        try:
            self.context_max_chars = max(200, int(llm_config.get("context_max_chars", 1200)))
        except (TypeError, ValueError):
            self.context_max_chars = 1200
        self._context_ready = False
        self._paper_title = ''
        self._unit_section_titles: Dict[str, str] = {}
        self._limiter: Optional[AdaptiveConcurrencyLimiter] = None
        self._limiter_loop = None
        self._validator = None
        project_name = os.path.basename(str(project_dir)) if project_dir else None
        self.usage = UsageTracker.from_llm_config(llm_config, project=project_name)
        self.checkpoint = None
        self._resume_usage: Dict[str, Any] = {}

    def _get_limiter(self) -> AdaptiveConcurrencyLimiter:
        """所有 LLM 请求共用一个自适应并发闸门（按事件循环创建）。"""
        loop = asyncio.get_running_loop()
        if self._limiter is None or self._limiter_loop is not loop:
            try:
                min_limit = int(self.get_llm_config().get("min_concurrency", 1))
            except (TypeError, ValueError):
                min_limit = 1
            self._limiter = AdaptiveConcurrencyLimiter(
                self.concurrency_limit,
                min_limit=min_limit,
                cooldown_key=self.base_url,
            )
            self._limiter_loop = loop
        return self._limiter

    def _new_session(self) -> aiohttp.ClientSession:
        """连接池上限与并发上限一致；aiohttp 默认只允许 100 个连接。"""
        connector = aiohttp.TCPConnector(
            limit=self.concurrency_limit,
            limit_per_host=self.concurrency_limit,
        )
        return aiohttp.ClientSession(connector=connector)

    def _hedge_delay(self, payload: Dict[str, Any]) -> Optional[float]:
        """按输入长度估计正常耗时；超过其若干倍仍未返回时发出备份请求。

        服务端偶发排队会让个别请求慢 5~10 倍，而整篇耗时取决于最慢的请求。
        """
        factor = self._hedge_factor
        if factor <= 0:
            return None
        chars = sum(len(message.get("content", "")) for message in payload.get("messages", [])[1:])
        expected = 0.5 + chars / self._hedge_chars_per_sec  # 默认 900 字符/秒：1500 字符约 2 秒
        return max(self._hedge_min_delay, factor * expected)

    async def _post_chat(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发送请求；慢于预期时并发一个相同的备份请求，取先成功者。"""
        delay = self._hedge_delay(payload)
        primary = asyncio.create_task(self._post_chat_once(session, payload))
        tasks = [primary]
        try:
            if delay is None:
                return await primary
            done, _ = await asyncio.wait({primary}, timeout=delay)
            if done:
                return primary.result()
            # 限流期间变慢是排队/退避造成的，再发备份请求只会加重限流。
            if self._get_limiter().throttled:
                return await primary

            backup = asyncio.create_task(self._post_chat_once(session, payload))
            tasks.append(backup)
            pending = set(tasks)
            error: Optional[BaseException] = None
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None:
                        return task.result()
                    error = task.exception()
            raise error
        finally:
            await self._cancel_and_drain(tasks)

    @staticmethod
    async def _cancel_and_drain(tasks) -> None:
        """Finish owned tasks before their HTTP session or event loop closes."""
        tasks = list(tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _gather_owned(self, coroutines):
        """Cancel siblings on any failure instead of leaving gather's tasks alive."""
        tasks = [asyncio.create_task(coro) for coro in coroutines]
        try:
            return await asyncio.gather(*tasks)
        finally:
            await self._cancel_and_drain(tasks)

    async def _post_chat_once(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        """所有 LLM 请求共用一个自适应并发闸门，分块请求也不会突破上限。

        429/5xx 的退避重试在共享请求层完成，不计入模型输出失败次数。
        """
        headers = {
            "Authorization": f"Bearer {self.API_KEY}",
            "Content-Type": "application/json",
        }
        return await self.post_chat_with_backoff(
            session,
            self.base_url,
            payload,
            headers,
            limiter=self._get_limiter(),
            timeout=self.get_aiohttp_timeout(),
        )

    def _save_maps(self, sections, captions, envs) -> None:
        atomic_write_json(Path(self.output_dir, "sections_map.json"), sections)
        atomic_write_json(Path(self.output_dir, "captions_map.json"), captions)
        atomic_write_json(Path(self.output_dir, "envs_map.json"), envs)

    def _valid_saved_unit(self, kind, item) -> bool:
        translated = item.get("trans_content")
        if not isinstance(translated, str) or (item.get("content", "").strip() and not translated.strip()):
            return False
        return not self._get_validator()._validate(item)

    def _unit_is_saved(self, kind, index, item) -> bool:
        if self.checkpoint is None:
            return False
        saved = self.checkpoint.read_fragment(kind, index, item)
        if saved is None:
            return False
        if self._valid_saved_unit(kind, saved):
            item.update(saved)
            return True
        self.checkpoint.drop_fragment(kind, index)
        return False

    def _persist_unit(self, kind, index, item) -> None:
        """Persist each successful part before another concurrent request can fail."""
        if self.checkpoint is None:
            return
        identifier = item.get("section" if kind == "sec" else "placeholder")
        if not self._is_registered_failure(kind, identifier) and self._valid_saved_unit(kind, item):
            self.checkpoint.save_fragment(kind, index, item)
        else:
            self.checkpoint.drop_fragment(kind, index)

    def _require_api_key(self) -> None:
        if not str(self.API_KEY or "").strip():
            raise ValueError("仍有未完成或无效的翻译片段，请配置 API Key（config/local.toml 或 LATEXTRANS_API_KEY）。")

    def _collect_translation_units(self, sections, envs, captions) -> List[tuple]:
        """展开为互不依赖的翻译单元，使章节、环境和图注全部并发。

        可达范围与旧的逐章节流程一致：章节正文中的环境、图注，
        以及这些环境内部的图注；重复出现的占位符只翻译一次。
        """
        env_index: Dict[str, int] = {}
        for i, env in enumerate(envs):
            env_index.setdefault(env["placeholder"], i)
        cap_index: Dict[str, int] = {}
        for i, caption in enumerate(captions):
            cap_index.setdefault(caption["placeholder"], i)

        units: List[tuple] = []
        seen_envs, seen_caps = set(), set()
        for i, section in enumerate(sections):
            if section["section"] not in ("-1", "0"):
                units.append(("sec", i))
            cap_phs = re.findall(r"<PLACEHOLDER_CAP_\d+>", section["content"])
            for placeholder in re.findall(r"<PLACEHOLDER_ENV_\d+>", section["content"]):
                j = env_index.get(placeholder)
                if j is None:
                    continue
                cap_phs.extend(re.findall(r"<PLACEHOLDER_CAP_\d+>", envs[j]["content"]))
                if j not in seen_envs:
                    seen_envs.add(j)
                    units.append(("env", j))
            for placeholder in cap_phs:
                j = cap_index.get(placeholder)
                if j is not None and j not in seen_caps:
                    seen_caps.add(j)
                    units.append(("cap", j))
        return units

    async def _judge_need_trans(self, env: Dict[str, Any], session: aiohttp.ClientSession) -> bool:
        """判断环境是否需要翻译；失败时保守地返回 True。"""
        payload = self.build_chat_payload({
            "model": f"{self.model}",
            "messages": [
                {"role": "system", "content": pm.set_need_trans_for_envs_system_prompt},
                {"role": "user", "content": env["content"]},
            ],
            "temperature": 0,
            "max_tokens": 50,
        })
        for attempt in range(1, 4):
            try:
                output = self.extract_chat_content(await self._post_chat(session, payload))
                return output.strip().lower() != "false"
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
                if attempt < 3:
                    await asyncio.sleep(self.retry_backoff)
        self.log(f"⚠️ Failed to set need_trans for {env['placeholder']}, set True.", level="warning")
        return True

    def _start_need_trans_judges(self, envs, session) -> Dict[int, "asyncio.Task"]:
        """解析阶段只做标记，判定请求与章节翻译同时发出。"""
        for env in envs:
            if env.get("always_trans") or str(env.get("env_name", "")).rstrip("*") in {"abstract", "itemize", "enumerate"}:
                env["need_trans"] = True
                env.pop("need_trans_pending", None)
        return {
            i: asyncio.create_task(self._judge_need_trans(env, session))
            for i, env in enumerate(envs)
            if env.get("need_trans_pending")
        }

    @staticmethod
    async def _apply_need_trans(envs, judges, i) -> None:
        task = judges.get(i)
        if task is None or not envs[i].get("need_trans_pending"):
            return
        envs[i]["need_trans"] = await task
        envs[i].pop("need_trans_pending", None)

    def _get_validator(self):
        if self._validator is None:
            from src.agents.tool_agents.validator_agent import ValidatorAgent
            self._validator = ValidatorAgent(
                config=self.config, project_dir=self.project_dir, output_dir=self.output_dir
            )
        return self._validator

    def _is_registered_failure(self, kind: str, key: str) -> bool:
        failed = {"sec": self.fail_section_nums, "cap": self.fail_caption_phs}.get(kind, self.fail_env_phs)
        return key in failed

    async def _translate_unit(self, kind, index, sections, envs, captions, judges, session):
        """翻译单个片段并立即做结构校验；失败时在本任务内重翻。"""
        if kind == "sec":
            translate, items, key = self._translate_section, sections, sections[index]["section"]
        elif kind == "env":
            await self._apply_need_trans(envs, judges, index)
            translate, items, key = self._translate_env, envs, envs[index]["placeholder"]
        else:
            translate, items, key = self._translate_caption, captions, captions[index]["placeholder"]

        source = items[index]
        result = await translate(source, session)
        if kind == "env" and not source.get("need_trans"):
            self._persist_unit(kind, index, result)
            return kind, index, result

        validator = self._get_validator()
        for _ in range(self.inline_retries):
            # 请求层失败由 _val_fail_parts 统一处理，这里只修结构错误。
            if self._is_registered_failure(kind, key):
                break
            report = validator._validate(result)
            if not report:
                break
            result = await translate(
                source, session, error_message=self._format_error_message(report), mode=1
            )
        self._persist_unit(kind, index, result)
        return kind, index, result

    async def execute(self, error_retry_count=0, Maxtry=3):
        try:
            await self._execute(error_retry_count=error_retry_count, Maxtry=Maxtry)
        finally:
            # 请求失败中止时 token 也已计费，因此无论成败都落盘用量。
            self._write_usage()

    def _write_usage(self) -> None:
        """把本项目累计 token 用量写入 ``<output_dir>/usage.json`` 并打印摘要。"""
        if not self.output_dir:
            return
        try:
            path = Path(self.output_dir, "usage.json")
            atomic_write_json(path, self.usage_summary())
        except OSError as exc:
            self.log(f"⚠️ 写入 token 用量失败：{exc}", level="warning")
            return
        self.log(f"{self.usage.summary_line()}（已写入 {path}）")

    def usage_summary(self) -> Dict[str, Any]:
        current = self.usage.snapshot()
        previous = self._resume_usage
        for key in ("requests", "requests_without_usage", "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_prompt_tokens"):
            current[key] += int(previous.get(key, 0) or 0)
        events = dict(previous.get("events") or {})
        for key, value in current.get("events", {}).items():
            events[key] = events.get(key, 0) + value
        if events:
            current["events"] = events
        if previous.get("cost"):
            current["cost"] = {
                key: round(float(current.get("cost", {}).get(key, 0)) + float(previous["cost"].get(key, 0)), 6)
                for key in ("input", "output", "total")
            }
        return current

    async def _execute(self, error_retry_count=0, Maxtry=3):

        self.fail_section_nums.clear()
        self.fail_caption_phs.clear()
        self.fail_env_phs.clear()
        self.have_fail_parts = False

        pm.init_prompts(self.config["source_language"], self.config["target_language"])
        self.add_placeholder()
        self.build_term_dict()
        if self.checkpoint is not None and self.checkpoint.valid and self.update_term:
            previous_terms = read_json(Path(self.output_dir, "term_dict.json"), {})
            if isinstance(previous_terms, dict):
                self.term_dict.update({
                    key: value for key, value in previous_terms.items()
                    if isinstance(key, str) and isinstance(value, str) and not key.startswith("<PLACEHOLDER_")
                })

        process_b = _ui(st.empty)
        with process_b:
            process_bar = _ui(st.progress, 0)
        status_text = _ui(st.empty)

        sections = self.read_file(Path(self.output_dir, "sections_map.json"), "json")
        captions = self.read_file(Path(self.output_dir, "captions_map.json"), "json")
        envs = self.read_file(Path(self.output_dir, "envs_map.json"), "json")

        if self.trans_mode == 0 or self.trans_mode == 2:
            self.log(f"Starting translation for project: {os.path.basename(self.project_dir)}.")

            _ui(status_text.text, f"Starting translation for project: {os.path.basename(self.project_dir)}.")
            _ui(process_bar.progress, 5)

            targets = {"sec": sections, "env": envs, "cap": captions}
            units = self._collect_translation_units(sections, envs, captions)
            pending_units = [
                (kind, i) for kind, i in units
                if not self._unit_is_saved(kind, i, targets[kind][i])
            ]
            if not pending_units:
                self._save_maps(sections, captions, envs)
                _ui(process_bar.progress, 100)
                _ui(status_text.empty)
                _ui(process_b.empty)
                self.log(f"已复用 {len(units)} 个完成片段，无需翻译请求。")
                return
            self._require_api_key()
            if len(pending_units) != len(units):
                self.log(f"复用 {len(units) - len(pending_units)} 个有效片段，继续翻译 {len(pending_units)} 个片段。")
            async with self._new_session() as session:
                tasks, judges = [], {}
                try:
                    await self._prepare_context(sections, envs, captions, session)
                    judges = self._start_need_trans_judges(envs, session)
                    tasks = [
                        asyncio.create_task(self._translate_unit(kind, i, sections, envs, captions, judges, session))
                        for kind, i in pending_units
                    ]

                    completed = 0
                    total_tasks = len(tasks)
                    for future in asyncio.as_completed(tasks):
                        kind, i, translated = await future
                        targets[kind][i] = translated
                        completed += 1

                        process = int(5 + 90 * completed / total_tasks)
                        _ui(process_bar.progress, process, text=f"Translating parts: {completed}/{total_tasks}")

                    # 不可达环境的判定也要落盘，保持与旧解析阶段一致。
                    for i in list(judges):
                        await self._apply_need_trans(envs, judges, i)
                    self._save_maps(sections, captions, envs)

                    _ui(status_text.text, "Validating translation results...")
                    _ui(process_bar.progress, 95)

                    await self._val_fail_parts(Maxtry=Maxtry,
                                         sections=sections,
                                         captions=captions,
                                         envs=envs,
                                         session=session)

                    self._raise_if_request_failures()

                    _ui(process_bar.progress, 100)
                    _ui(status_text.empty)
                    _ui(process_b.empty)
                    self.log("翻译请求已完成，等待独立校验。")
                finally:
                    await self._cancel_and_drain(tasks + list(judges.values()))

        elif self.trans_mode == 1:

            if self.errors_report:
                self._require_api_key()
            status_text = _ui(st.empty)
            async with self._new_session() as session:
                await self._prepare_context(sections, envs, captions, session)
                error_parts = [error_part["num_or_ph"] for error_part in self.errors_report]
                self.log(
                    f"Starting retranslation for error parts: {error_parts}, attempt {error_retry_count + 1}/{Maxtry}.")
                _ui(status_text.text, f"Starting retranslation for error parts: {error_parts}, attempt {error_retry_count + 1}/{Maxtry}.")
                await self._retranslate_error_parts(secs=sections,
                                                    caps=captions,
                                                    envs=envs,
                                                    session=session)

                self._save_maps(sections, captions, envs)

                await self._val_fail_parts(Maxtry=Maxtry,
                                           sections=sections,
                                           captions=captions,
                                           envs=envs,
                                           session=session)

                self._raise_if_request_failures()

            _ui(status_text.empty)
            self.log("重翻请求已完成，等待独立校验。")

    async def _val_fail_parts(self, sections, captions, envs, Maxtry, session: aiohttp.ClientSession, fail_retry_count=0) -> str:
            status_text = _ui(st.empty)
            while fail_retry_count < Maxtry and self.have_fail_parts:
                fail_parts = self.fail_section_nums + self.fail_caption_phs + self.fail_env_phs
                self.log(f"Starting retranslation for failed parts: {fail_parts}, attempt {fail_retry_count+1}/{Maxtry}.")
                _ui(status_text.text, f"Starting retranslation for failed parts: {fail_parts}, attempt {fail_retry_count+1}/{Maxtry}.")
                await self._retranslate_fail_parts(secs=sections,
                                            caps=captions,
                                            envs=envs,
                                            session=session)
                self._save_maps(sections, captions, envs)
                
                fail_retry_count += 1
                status_text = _ui(st.empty)

            if self.have_fail_parts:
                fail_parts = self.fail_section_nums + self.fail_caption_phs + self.fail_env_phs
                self.log(
                    f"❌ 翻译请求在 {Maxtry} 轮内部重试后仍失败：{fail_parts}",
                    level="error",
                )

    @staticmethod
    def _format_error_message(error_report: Dict[str, Any]) -> str:
        """把校验报告中的各类错误合并成重翻提示。"""
        return "\n".join(
            error_report.get(key, "")
            for key in ("command_error", "ph_error", "bracket_error")
            if error_report.get(key)
        )

    async def _retranslate_fail_parts(self,
                                secs: List[Dict[str, Any]], 
                                caps: List[Dict[str, Any]], 
                                envs: List[Dict[str, Any]],
                                session: aiohttp.ClientSession) -> Any:
        sec_nums = self.fail_section_nums[:]
        cap_phs = self.fail_caption_phs[:]
        env_phs = self.fail_env_phs[:]
        self.fail_section_nums.clear()
        self.fail_caption_phs.clear()
        self.fail_env_phs.clear()
        self.have_fail_parts = False

        sec_dict = {s["section"]: i for i, s in enumerate(secs)}
        cap_dict = {c["placeholder"]: i for i, c in enumerate(caps)}
        env_dict = {e["placeholder"]: i for i, e in enumerate(envs)}
        error_messages = {
            (error.get("part"), error.get("num_or_ph")):
            self._format_error_message(error)
            for error in self.errors_report
        }

        jobs = []  # (目标列表, 下标, 协程)，全部并发执行
        if sec_nums:
            self.log(f"Retranslating for {sec_nums}")
            for sec_num in sec_nums:
                if sec_num == "-1" or sec_num == "0":
                    continue
                if sec_num in sec_dict:
                    i = sec_dict[sec_num]
                    jobs.append(("sec", secs, i, self._translate_section(
                        secs[i],
                        session,
                        error_message=error_messages.get(("sec", sec_num)),
                    )))
        if cap_phs:
            self.log(f"Retranslating for {cap_phs}")
            for cap_ph in cap_phs:
                if cap_ph in cap_dict:
                    i = cap_dict[cap_ph]
                    jobs.append(("cap", caps, i, self._translate_caption(
                        caps[i],
                        session,
                        error_message=error_messages.get(("cap", cap_ph)),
                    )))
        if env_phs:
            self.log(f"Retranslating for {env_phs}")
            for env_ph in env_phs:
                if env_ph in env_dict:
                    i = env_dict[env_ph]
                    jobs.append(("env", envs, i, self._translate_env(
                        envs[i],
                        session,
                        error_message=error_messages.get(("env", env_ph)),
                    )))

        async def apply_result(kind, target, index, request):
            translated = await request
            target[index] = translated
            self._persist_unit(kind, index, translated)

        await self._gather_owned(apply_result(*job) for job in jobs)

    async def _retranslate_error_parts(self, secs, caps, envs, session) -> Any:
        """只重翻校验报告中的片段，并把结果写回对应映射。"""
        sem = asyncio.Semaphore(self.concurrency_limit)

        process_b = _ui(st.empty)
        with process_b:
            process_bar = _ui(process_b.progress, 0)
        status_text = _ui(st.empty)

        async def process_error_part(error_report):
            """定位单个错误片段；避免旧实现的嵌套信号量死锁。"""
            async with sem:
                error_message = self._format_error_message(error_report)
                part_type = error_report.get("part")
                target = error_report.get("num_or_ph")

                if part_type == "sec":
                    index = next(
                        (i for i, section in enumerate(secs)
                         if section.get("section") == target),
                        None,
                    )
                    if index is None:
                        return None
                    result = await self._translate_section(
                        section=secs[index],
                        error_message=error_message,
                        session=session,
                    )
                elif part_type == "env":
                    index = next(
                        (i for i, env in enumerate(envs)
                         if env.get("placeholder") == target),
                        None,
                    )
                    if index is None:
                        return None
                    result = await self._translate_env(
                        env=envs[index],
                        error_message=error_message,
                        session=session,
                    )
                elif part_type == "cap":
                    index = next(
                        (i for i, caption in enumerate(caps)
                         if caption.get("placeholder") == target),
                        None,
                    )
                    if index is None:
                        return None
                    result = await self._translate_caption(
                        caption=caps[index],
                        error_message=error_message,
                        session=session,
                    )
                else:
                    self.log(
                        f"⚠️ 忽略未知错误片段类型：{part_type!r} ({target!r})",
                        level="warning",
                    )
                    return None

                self._persist_unit(part_type, index, result)
                return part_type, index, result

        tasks = [asyncio.create_task(process_error_part(error_report)) for error_report in self.errors_report]
        try:
            total_error_tasks = len(tasks)
            completed = 0
            for future in asyncio.as_completed(tasks):
                result = await future
                completed += 1
                _ui(process_bar.progress, completed / total_error_tasks if total_error_tasks else 1.0)
                _ui(status_text.text, f"Retranslating error parts: {completed}/{total_error_tasks}")

                if result is None:
                    continue
                part_type, index, translated = result
                if part_type == "sec":
                    secs[index] = translated
                elif part_type == "env":
                    envs[index] = translated
                else:
                    caps[index] = translated

            _ui(process_bar.progress, 1.0)
            _ui(status_text.text, "Complete a retranslation once")
            _ui(process_b.empty)
            _ui(status_text.empty)
        finally:
            await self._cancel_and_drain(tasks)

    def _base_prompt(self, type: str, mode: int) -> str:
        """按片段类型与模式选择系统提示。

        开启 ``use_context`` 时使用 *_with_sum 提示（说明会附带论文上下文）；
        模式 2 沿用 *_with_dict 提示。术语表本身在请求层按片段筛选后注入，
        与模式无关。
        """
        if self.use_context:
            return {
                "sec": pm.section_system_prompt_with_sum,
                "cap": pm.caption_system_prompt_with_sum,
                "env": pm.env_system_prompt_with_sum,
            }[type]
        if mode == 2 and self.term_dict:
            return {
                "sec": pm.section_system_prompt_with_dict,
                "cap": pm.caption_system_prompt_with_dict,
                "env": pm.env_system_prompt_with_dict,
            }[type]
        return {
            "sec": pm.section_system_prompt,
            "cap": pm.caption_system_prompt,
            "env": pm.env_system_prompt,
        }[type]

    async def _update_terms_from(self, source: str, translated: str, session: aiohttp.ClientSession) -> None:
        """模式 2 且 update_term 开启时，从译文中抽取新术语并入术语表。"""
        if not self.update_term:
            return
        try:
            src_text = self._extract_text_from_tex(source)
            tgt_text = self._extract_text_from_tex(translated)
            term_text = await self._request_llm_for_extract_terms(
                pm.extract_terminology_system_prompt,
                src_text,
                tgt_text,
                session=session,
            )
            # self._updated_term_dict(term_text)
            self._updated_term_dict_v2(term_text)
        except Exception:
            return

    async def _translate_section(self, section: Dict[str, Any], session: aiohttp.ClientSession, error_message=None, mode=None) -> Dict[str, Any]:

        mode = self.trans_mode if mode is None else mode
        transed_section = section.copy()
        section_num = section["section"]
        if mode == 1:
            transed_section["trans_content"] = await self._request_llm_for_retrans_error_parts(
            pm.retrans_error_parts_system_prompt,
            part=transed_section,
            error_message=error_message,
            fail_part=section_num,
            type="sec",
            session=session)
        elif mode in (0, 2):
            # 术语表在所有模式下都按片段筛选后注入（见 _request_llm_for_trans）。
            transed_section["trans_content"] = await self._request_llm_for_trans(
                self._base_prompt("sec", mode),
                section["content"],
                fail_part=section_num,
                type="sec",
                session=session
            )
            if mode == 2:
                await self._update_terms_from(
                    transed_section["content"], transed_section["trans_content"], session
                )

        transed_section["trans_content"] = normalize_latex_reference_arguments(
            transed_section.get("trans_content", "")
        )
        return transed_section

    async def _translate_caption(self, caption: Dict[str, Any], session: aiohttp.ClientSession, error_message=None, mode=None) -> Dict[str, Any]:
        """
        Translates the captions of the input data.
        """
        mode = self.trans_mode if mode is None else mode
        transed_caption = caption.copy()
        placeholder = caption["placeholder"]
        if mode == 1:
            # Keep current retranslating path for captions.
            transed_caption["trans_content"] = await self._request_llm_for_retrans_error_parts(pm.retrans_error_parts_system_prompt,
                                                                                         part=transed_caption,
                                                                                         error_message=error_message,
                                                                                         fail_part=placeholder,
                                                                                         type="cap",
                                                                                         session=session)
        elif mode in (0, 2):
            transed_caption["trans_content"] = await self._request_llm_for_trans(self._base_prompt("cap", mode),
                                                        caption["content"],
                                                        fail_part=placeholder,
                                                        type="cap",
                                                        session=session
                                                        )
            if mode == 2:
                await self._update_terms_from(
                    transed_caption["content"], transed_caption["trans_content"], session
                )

        trans_content, unexpected_placeholders = strip_unexpected_placeholders(
            caption.get("content", ""),
            transed_caption.get("trans_content", ""),
        )
        if unexpected_placeholders:
            self.log(
                f"⚠️ 已从 caption {placeholder} 移除多余占位符："
                f"{unexpected_placeholders}"
            )
        transed_caption["trans_content"] = normalize_latex_reference_arguments(
            trans_content
        )
        return transed_caption

    async def _translate_env(self, env: Dict[str, Any], session: aiohttp.ClientSession, error_message=None, mode=None) -> Dict[str, Any]:
        """
        Translates an environment block (env) based on whether translation is needed.
        """
        mode = self.trans_mode if mode is None else mode
        transed_env = env.copy()
        placeholder = env["placeholder"]
        if mode == 1:
                transed_env["trans_content"] = await self._request_llm_for_retrans_error_parts(pm.retrans_error_parts_system_prompt,
                                                                                         part=transed_env,
                                                                                         error_message=error_message,
                                                                                         fail_part=placeholder,
                                                                                         type="env",
                                                                                         session = session)
        elif mode in (0, 2):
            # 复用解析阶段/LLM 判定得到的 need_trans：纯公式、代码、表格结构直接保留原文。
            if env["need_trans"]:
                transed_env["trans_content"] = await self._request_llm_for_trans(self._base_prompt("env", mode),
                                                            env["content"],
                                                            fail_part=placeholder,
                                                            type="env",
                                                            session=session
                                                            )
                if mode == 2:
                    await self._update_terms_from(
                        transed_env["content"], transed_env["trans_content"], session
                    )
            else:
                transed_env["trans_content"] = env["content"]

        transed_env["trans_content"] = normalize_latex_reference_arguments(
            transed_env.get("trans_content", "")
        )
        return transed_env

    def _build_protected_input(
        self,
        protection: LatexSyntaxProtection,
        content_label: str = "LaTeX 内容",
    ) -> str:
        """构造带有结构保护说明的模型输入。"""
        if not protection.token_order and not protection.fixed_suffix:
            return protection.protected_text

        instructions = ["[LaTeX 结构保护]"]
        if protection.token_order:
            instructions.append(
                "输入中的 [[[LATEXTRANS_..._0001]]] 是临时结构标记，"
                "不是正文，也不是 XML/HTML 标签。"
                "必须逐字保留每个标记，不能删除、复制、合并或翻译；"
                "不能为标记生成闭合形式；"
                "项目占位符和环境边界标记的顺序必须保持不变；"
                "普通行内公式、引用和格式标记如因目标语言语序移动，"
                "必须仍保持各自完整。"
            )
        if protection.fixed_suffix:
            instructions.append(
                "程序已从输入末尾移除并在本地保管纯 LaTeX 结构，"
                "会在输出后自动原位恢复。不要猜测、补写或重复任何未出现在"
                "当前内容中的尾部 LaTeX 命令。"
            )
        instructions.append("输出时只返回翻译后的当前 LaTeX 内容。")
        instructions.extend(
            [
                f"[{content_label}]",
                protection.protected_text,
            ]
        )
        return "\n".join(instructions)

    @staticmethod
    def _prepare_structure_retry(
        payload: Dict[str, Any],
        original_messages: List[Dict[str, str]],
        protection: LatexSyntaxProtection,
    ) -> None:
        """在结构校验失败后收紧下一次请求。"""
        marker_manifest = "\n".join(protection.token_order)
        reminder = (
            "[上一次结果的 LaTeX 结构有误]\n"
            "请重新翻译原输入。下列每个标记都必须在输出中原样出现且只出现一次：\n"
            f"{marker_manifest}\n"
            "不要把它们改成开始/结束标签，也不要额外添加或遗漏括号。\n\n"
        )
        retry_messages = [message.copy() for message in original_messages]
        retry_messages[-1]["content"] = reminder + retry_messages[-1]["content"]
        payload["messages"] = retry_messages
        payload["temperature"] = 0.0

    def _register_request_failure(
        self,
        fail_part: str,
        part_type: str,
        reason: str,
    ) -> None:
        """记录不可恢复的请求失败，阻止未验证内容继续生成。"""
        self.have_fail_parts = True
        if part_type == "sec":
            failed_parts = self.fail_section_nums
        elif part_type == "cap":
            failed_parts = self.fail_caption_phs
        else:
            failed_parts = self.fail_env_phs

        if fail_part not in failed_parts:
            failed_parts.append(fail_part)
        self.log(
            f"❌ {fail_part} 的翻译请求失败，已保留为待重试片段：{reason}",
            level="error",
        )

    def _restore_protected_result(
        self,
        result_text: str,
        protection: LatexSyntaxProtection,
        source_text: str,
        fail_part: str,
        attempt: int,
    ) -> Optional[str]:
        """严格校验并恢复模型返回的 LaTeX 结构。"""
        protection_error = protection.validate(result_text)
        if protection_error:
            self.log(
                f"⚠️ {fail_part} 第 {attempt} 次响应未保留 LaTeX 结构标记："
                f"{protection_error}",
                level="warning",
            )
            return None

        restored = protection.restore(result_text)
        if not find_latex_bracket_errors(source_text):
            bracket_errors = find_latex_bracket_errors(restored)
            if bracket_errors:
                self.log(
                    f"⚠️ {fail_part} 第 {attempt} 次响应的 LaTeX 括号不完整："
                    f"{bracket_errors[0]}",
                    level="warning",
                )
                return None
        return restored

    async def _request_segmented_translation(
        self,
        system_prompt: str,
        source_text: str,
        fail_part: str,
        session: aiohttp.ClientSession,
    ) -> Optional[str]:
        """结构标记反复丢失时，仅让模型翻译结构之间的文本。"""
        protection = protect_latex_syntax(
            source_text, protect_group_delimiters=True
        )
        tokens = LATEX_PROTECTED_TOKEN_PATTERN.findall(protection.protected_text)
        pieces = LATEX_PROTECTED_TOKEN_PATTERN.split(protection.protected_text)
        slots = []
        source_segments = []
        for index, piece in enumerate(pieces):
            if not any(char.isalpha() for char in piece):
                continue
            match = re.fullmatch(r"(\s*)(.*?)(\s*)", piece, re.DOTALL)
            slots.append((index, match.group(1), match.group(3)))
            source_segments.append(match.group(2))

        if not source_segments:
            return protection.restore(protection.protected_text)

        payload = self.build_chat_payload({
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n"
                        "Translate the text segments in context. Return only a JSON array "
                        "of translated strings, one for each input segment in the same "
                        "order. Do not add LaTeX commands, delimiters, placeholders, "
                        "or explanations. The program will restore all LaTeX structure."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"source_latex": source_text, "segments": source_segments},
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        })
        self.log(f"{fail_part} 使用分段翻译恢复 LaTeX 结构。")
        for attempt in range(1, 3):
            try:
                result = await self._post_chat(session, payload)
                content = self.extract_chat_content(result)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                self.log(
                    f"⚠️ {fail_part} 第 {attempt} 次分段翻译请求失败：{exc}",
                    level="warning",
                )
                continue

            fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
            if fence:
                content = fence.group(1)
            try:
                translations = json.loads(content)
            except json.JSONDecodeError:
                translations = None
            if (
                not isinstance(translations, list)
                or len(translations) != len(source_segments)
                or any(
                    not isinstance(value, str)
                    or not value.strip()
                    or re.search(r"[\\{}\[\]$&#%_^]", value)
                    for value in translations
                )
            ):
                self.log(
                    f"⚠️ {fail_part} 第 {attempt} 次分段翻译未返回安全且完整的文本数组。",
                    level="warning",
                )
                continue

            translated_pieces = pieces[:]
            for (index, leading, trailing), translated in zip(slots, translations):
                translated_pieces[index] = leading + translated.strip() + trailing
            candidate = "".join(
                piece + (tokens[index] if index < len(tokens) else "")
                for index, piece in enumerate(translated_pieces)
            )
            restored = self._restore_protected_result(
                candidate, protection, source_text, fail_part, attempt
            )
            if restored is not None:
                return restored

        return None

    def _raise_if_request_failures(self) -> None:
        """请求层仍失败时中止生成，避免静默写入原文或半成品。"""
        if not self.have_fail_parts:
            return

        failed_parts = (
            self.fail_section_nums
            + self.fail_caption_phs
            + self.fail_env_phs
        )
        raise RuntimeError(
            "翻译请求未能生成可验证结果，已阻止 LaTeX 生成："
            f"{failed_parts}"
        )

    def get_max_chunk_chars(self, default: int = 1500) -> int:
        """单次翻译请求的最大源文本长度。

        请求耗时与输出长度成正比，并发请求几乎不增加总耗时，
        因此切小片段能显著缩短最长请求，从而缩短整篇翻译时间。
        """
        raw_value = self.get_llm_config().get("max_chunk_chars", default)
        try:
            return max(1000, int(raw_value))
        except (TypeError, ValueError):
            return default

    async def _translate_chunks(self, request_fn, chunks, system_prompt, fail_part, type, session, temperature):
        """分块翻译后按原顺序拼回；每块独立校验结构标记。"""
        results = await self._gather_owned(
                request_fn(
                    system_prompt,
                    chunk,
                    fail_part=fail_part,
                    type=type,
                    session=session,
                    temperature=temperature,
                )
                for chunk in chunks
        )
        # 模型输出会被 strip；恢复每块原有的首尾空白，避免段落粘连。
        merged = []
        for chunk, result in zip(chunks, results):
            leading = chunk[:len(chunk) - len(chunk.lstrip())]
            trailing = chunk[len(chunk.rstrip()):]
            merged.append(leading + (result or "").strip() + trailing)
        return "".join(merged)

    def _term_pattern(self, term: str) -> "re.Pattern":
        pattern = self._term_patterns.get(term)
        if pattern is None:
            body = r"\s+".join(re.escape(word) for word in term.split())
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE
            )
            self._term_patterns[term] = pattern
        return pattern

    def _relevant_terms(self, text: str) -> List[tuple]:
        """挑出当前片段中实际出现的术语。

        大小写不敏感、按词边界匹配（``RL`` 不会命中 ``URL``），
        最多 ``glossary_max_terms`` 条；超出时优先保留更长、更具体的术语。
        """
        if not text or not self.term_dict or self.glossary_max_terms <= 0:
            return []
        matches = []
        for source, target in list(self.term_dict.items()):
            if not isinstance(source, str) or target is None:
                continue
            if isinstance(target, float) and target != target:  # pandas 的 NaN
                continue
            source_key = source.strip()
            target_text = str(target).strip()
            if (
                not source_key
                or not target_text
                or LATEX_PLACEHOLDER_PATTERN.search(source_key)
            ):
                continue
            found = self._term_pattern(source_key).search(text)
            if found:
                matches.append((found.start(), source_key, target_text))
        if len(matches) > self.glossary_max_terms:
            matches = sorted(matches, key=lambda item: (-len(item[1]), item[0]))
            matches = matches[: self.glossary_max_terms]
        matches.sort(key=lambda item: item[0])
        return [(source, target) for _, source, target in matches]

    def _unit_context(self, type: str, key: str) -> str:
        """``use_context`` 开启时返回论文标题/概要/章节标题上下文块。"""
        if not self.use_context:
            return ""
        return pm.build_context_block(
            self._paper_title,
            self.summary,
            self._unit_section_titles.get(key, ""),
        )

    def _compose_system_prompt(self, system_prompt: str, text: str, type: str, fail_part: str) -> str:
        """系统提示 + 可选上下文 + 仅与当前片段相关的术语。"""
        return (
            f"{system_prompt}"
            f"{self._unit_context(type, fail_part)}"
            f"{pm.build_glossary_block(self._relevant_terms(text))}"
        )

    def _plain_text(self, tex: str, limit: Optional[int] = None) -> str:
        """把 LaTeX 片段转为纯文本（失败时退回粗略去命令），可选截断。"""
        if not tex:
            return ""
        try:
            text = self._extract_text_from_tex(tex)
        except Exception:
            text = re.sub(r"\\[A-Za-z@]+\*?|[{}]", " ", tex)
        text = re.sub(r"\s+", " ", text).strip()
        if limit and len(text) > limit:
            text = text[:limit].rstrip() + " ..."
        return text

    async def _prepare_context(self, sections, envs, captions, session: aiohttp.ClientSession) -> None:
        """每个项目只准备一次论文级上下文：标题、摘要（或 LLM 概要）、章节标题映射。"""
        if not self.use_context or self._context_ready:
            return
        self._context_ready = True

        title_tex = next(
            (c.get("content", "") for c in captions if c.get("cap_type") == "title"), ""
        )
        title_match = re.match(r"\s*\\title\*?\s*(?:\[[^\]]*\])?\s*\{(.*)\}\s*$", title_tex, re.DOTALL)
        self._paper_title = self._plain_text(
            title_match.group(1) if title_match else title_tex, 300
        )
        abstract_tex = next(
            (e.get("content", "") for e in envs if e.get("env_name") == "abstract"), ""
        )
        abstract_text = self._plain_text(abstract_tex)

        summary = ""
        if self.context_summary:
            first_section = next(
                (s.get("content", "") for s in sections if s.get("section") not in ("-1", "0")),
                "",
            )
            source = "\n\n".join(
                part for part in (abstract_text, self._plain_text(first_section, 6000)) if part
            )
            if source:
                summary = await self._request_llm_for_summary(
                    pm.get_summary_system_prompt, source, session
                )
                if summary == "N/A":
                    summary = ""
        summary = summary or abstract_text
        if len(summary) > self.context_max_chars:
            summary = summary[: self.context_max_chars].rstrip() + " ..."
        self.summary = summary

        env_by_ph = {e.get("placeholder"): e for e in envs}
        titles: Dict[str, str] = {}
        for section in sections:
            content = section.get("content", "")
            match = _SECTION_TITLE_PATTERN.search(content)
            title = self._plain_text(match.group(1), 200) if match else ""
            if not title:
                continue
            titles.setdefault(section.get("section"), title)
            placeholders = re.findall(r"<PLACEHOLDER_(?:ENV|CAP)_\d+>", content)
            for placeholder in list(placeholders):
                env = env_by_ph.get(placeholder)
                if env is not None:
                    placeholders.extend(
                        re.findall(r"<PLACEHOLDER_CAP_\d+>", env.get("content", ""))
                    )
            for placeholder in placeholders:
                titles.setdefault(placeholder, title)
        self._unit_section_titles = titles
        self.log("已启用论文上下文：" + ("LLM 概要" if self.context_summary and summary != abstract_text else "摘要原文"))

    async def _request_llm_for_trans(self,
                                     system_prompt: str,
                                     text: str,
                                     fail_part: str,
                                     type: str,
                                     session: aiohttp.ClientSession,
                                     temperature: Optional[float] = None) -> str:
        temperature = self.temperature if temperature is None else temperature
        if type in ("sec", "env"):
            # 先按空行/\item 切分，仍超长的片段再在 \\ 行尾、横线、换行处二次切分。
            chunks = split_translation_chunks(text, self.get_max_chunk_chars())
            if len(chunks) > 1:
                return await self._translate_chunks(
                    self._request_llm_for_trans, chunks, system_prompt, fail_part, type, session, temperature
                )

        protection = protect_latex_syntax(text, protect_group_delimiters=type == "cap")
        if not protection.protected_text.strip():
            return protection.restore(protection.protected_text)
        if self.skip_untranslatable and not has_translatable_text(protection.protected_text):
            # 只剩公式/结构（如 align 的若干行），保留原文，不请求模型。
            return text

        payload = self.build_chat_payload({
            "model": f"{self.model}",
            "messages": [
                {
                    "role": "system",
                    "content": self._compose_system_prompt(system_prompt, text, type, fail_part),
                },
                {
                    "role": "user",
                    "content": self._build_protected_input(protection),
                }
            ],
            "temperature": temperature,
            "max_tokens": self.max_tokens
        })
        original_messages = [message.copy() for message in payload["messages"]]


        for attempt in range(1, 4):
            try:
                result = await self._post_chat(session, payload)
                candidate = self.extract_chat_content(result)
                restored = self._restore_protected_result(
                    candidate,
                    protection,
                    text,
                    fail_part,
                    attempt,
                )
                if restored is not None:
                    return restored

                # 结构错误时降低随机性，并显式给出必须保留的标记清单。
                self._prepare_structure_retry(
                    payload,
                    original_messages,
                    protection,
                )
                if attempt < 3:
                    continue
                fallback = await self._request_segmented_translation(
                    original_messages[0]["content"], text, fail_part, session
                )
                if fallback is not None:
                    return fallback
                self._register_request_failure(
                    fail_part,
                    type,
                    "模型连续返回了不完整或顺序错误的 LaTeX 结构标记",
                )
                return text

            except TruncatedResponseError as e:
                halves = split_translation_chunks(text, max(1, len(text) // 2))
                if len(halves) > 1:
                    self.log(f"{fail_part} 输出被截断，拆分为 {len(halves)} 段重新翻译。")
                    return await self._translate_chunks(
                        self._request_llm_for_trans, halves, system_prompt, fail_part, type, session, temperature
                    )
                self._register_request_failure(fail_part, type, str(e))
                return text
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                KeyError,
                TypeError,
                ValueError,
            ) as e:
                # 429/5xx 已在请求层退避重试；到这里的才算一次真正的失败。
                if attempt < 3:
                    await asyncio.sleep(self.retry_backoff)
                else:
                    self._register_request_failure(fail_part, type, str(e))
                    return text

    async def _request_llm_for_trans_with_terms(self,
                                          system_prompt: str,
                                          text: str,
                                          fail_part: str,
                                          type: str,
                                          session: aiohttp.ClientSession,
                                          temperature: Optional[float] = None) -> str:
        """兼容旧接口：术语表现由 ``_request_llm_for_trans`` 按片段筛选后统一注入。"""
        return await self._request_llm_for_trans(
            system_prompt,
            text,
            fail_part=fail_part,
            type=type,
            session=session,
            temperature=temperature,
        )

    async def _request_llm_for_retrans_error_parts(self,
                                                   system_prompt: str,
                                                   part: Dict[str, Any],
                                                   error_message: str,
                                                   fail_part: str,
                                                   type: str,
                                                   session: aiohttp.ClientSession) -> str:
        """对结构校验失败的片段执行干净重翻。

        旧流程把临时 SOURCE 标记只放在 ``[Original]`` 中，却要求
        模型在修改 ``[Translation]`` 后输出这些标记。修正器按提示
        输出原生 LaTeX 时必然被判为“0 个标记”。结构错误应从
        权威原文重新翻译，不继承已损坏的译文结构。
        """
        base_prompt = {
            "sec": pm.section_system_prompt,
            "cap": pm.caption_system_prompt,
            "env": pm.env_system_prompt,
        }.get(type) or system_prompt
        retry_note = (
            "\nThis is a clean retry after an independent LaTeX structure "
            "validation failure. Translate only the authoritative source supplied "
            "in the user message. Do not copy or reconstruct the previous broken "
            "translation. Preserve every temporary structure marker exactly."
        )
        if error_message:
            retry_note += f"\nValidator report for context:\n{error_message}"
        clean_system_prompt = f"{base_prompt}{retry_note}"

        return await self._request_llm_for_trans(
            clean_system_prompt,
            part["content"],
            fail_part=fail_part,
            type=type,
            session=session,
            temperature=0.0,
        )

    async def _request_llm_for_extract_terms(self, system_prompt, src, tgt,
                                       session: aiohttp.ClientSession) -> str:

        payload = self.build_chat_payload({
            "model": f"{self.model}",
            "messages": [
                {
                    "role": "system",
                    "content": f"{system_prompt}"
                },
                {
                    "role": "user",
                    "content": f"<en source>\n{src}\n<zh translation>\n{tgt}"
                }
            ],
            "temperature": self.temperature,
            # "max_length": 100000,
            # "max_tokens": 50
        })


        for attempt in range(1, 4):
            try:
                result = await self._post_chat(session, payload)
                return self.extract_chat_content(result)

            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
                if attempt < 3:
                    await asyncio.sleep(self.retry_backoff)
                else:
                    print("Warning: failed to extract terms, set N/A.")
                    return "N/A"

    async def _request_llm_simple(self, system_prompt: str, user_content: str,
                                  session: aiohttp.ClientSession, what: str) -> str:
        """概要类请求：走共享请求层（并发闸门、429 退避、用量统计），失败返回 "N/A"。"""
        payload = self.build_chat_payload({
            "model": f"{self.model}",
            "messages": [
                {"role": "system", "content": f"{system_prompt}"},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        })
        for attempt in range(1, 4):
            try:
                return self.extract_chat_content(await self._post_chat(session, payload))
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
                if attempt < 3:
                    await asyncio.sleep(self.retry_backoff)
                else:
                    self.log(f"⚠️ {what}失败，置为 N/A：{e}", level="warning")
                    return "N/A"

    async def _request_llm_for_summary(self, system_prompt: str, text: str,
                                       session: aiohttp.ClientSession) -> str:
        """
        Requests the LLM to summarize the given text.
        """
        return await self._request_llm_simple(
            system_prompt,
            f"<Text to summarize>:\n{text}\n<Summary>:\n",
            session,
            "生成论文概要",
        )

    async def _request_llm_for_refine_summary(self, system_prompt: str, text: str, sum: str,
                                              session: aiohttp.ClientSession) -> str:
        """
        Requests the LLM to refine the given summary.
        """
        return await self._request_llm_simple(
            system_prompt,
            f"<prev_summary>:\n{sum}\n<new_section>:\n{text}\n<refined_summary>:\n",
            session,
            "更新论文概要",
        )

    def _updated_term_dict(self, text: str) -> None:
        """
        Updates the term dictionary with new terms.
        """
        pattern = r'"([^"]+)"\s*-\s*"([^"]+)"'
        matches = re.findall(pattern, text)

        seen_lower = {k.lower() for k in self.term_dict}
        
        for en, zh in matches:
            en_lower = en.lower()
            if en_lower not in seen_lower:
                self.term_dict[en] = zh  
                seen_lower.add(en_lower)

        self.save_file(Path(self.output_dir, "term_dict.json"), "json", self.term_dict)

    def _updated_term_dict_v2(self, text: str) -> None:

        new_term_dict = {}
        lines = text.split('\n')[1:]
        for line in lines:
            line = line.strip()
            if not line:
                continue  

            match = re.match(r'^"(.+?)"\s*-\s*"(.+?)"$', line)
            if match:
                english = match.group(1)
                chinese = match.group(2)
                new_term_dict[english] = chinese

        for en, zh in new_term_dict.items():
            if en not in self.term_dict:
                self.term_dict[en] = zh

    def _process_latex_to_eva(self, latex_code):
        latex_code = replace_href(latex_code)
        latex_code = replace_includegraphics(latex_code)
        return latex_code

    def _extract_text_from_tex(self, tex):
        # convert = CustomLatexNodes2Text()
        # text = convert.latex_to_text(tex)
        tex = self._process_latex_to_eva(tex)
        text = LatexNodes2Text().latex_to_text(tex)
        text = delete_ph(text)
        return text
    
    def _merge_with_prev_sections(self, sections: list[dict], idx: int) -> str:
        """
        Merge content of current section with previous two sections (if valid).
        Ignore sections whose 'section' field is "-1" or "0".

        Parameters:
            sections (list of dict): A list of sections, each with keys "section" and "content".
            idx (int): The index of the current section in the list.

        Returns:
            str: The merged content string.
        """
        if not (0 <= idx < len(sections)):
            raise IndexError("Index out of range.")

        merged_content = []
        merged_trans_content = []

        # Check second previous section
        # if idx >= 2:
        #     sec = sections[idx - 2]
        #     if sec["section"] not in {"-1", "0"}:
        #         try:
        #             content = self._extract_text_from_tex(sec["content"])
        #             transed_content = self._extract_text_from_tex(sec["trans_content"])
        #             merged_content.append(content)
        #             merged_trans_content.append(transed_content)
        #         except Exception as e:
        #             pass
                

        # Check first previous section
        if idx >= 1:
            sec = sections[idx - 1]
            if sec["section"] not in {"-1", "0"}:
                try:
                    content = self._extract_text_from_tex(sec["content"])
                    transed_content = self._extract_text_from_tex(sec["trans_content"])
                    merged_content.append(content)
                    merged_trans_content.append(transed_content)
                except Exception as e:
                    pass

        # Always include current section
        try:
            content = self._extract_text_from_tex(sections[idx]["content"])
            transed_content = self._extract_text_from_tex(sections[idx]["trans_content"])
            merged_content.append(content)
            merged_trans_content.append(transed_content)
        except Exception as e:
            pass

        return "\n".join(merged_content)

    def build_term_dict(self):
        """加载用户术语表或内置术语表。"""
        if self.user_term:
            user_term_path = self._resolve_local_path(self.user_term)
            df = pd.read_csv(
                user_term_path,
                header=None,
                names=['English Term', 'Chinese Translation'],
            )
            self.term_dict.update(zip(df['English Term'], df['Chinese Translation']))
            return

        arxiv_id = os.path.basename(self.project_dir)
        category_map = self.category or {}
        categories = category_map.get(arxiv_id, [])
        term_dict_loaded = False
        for category in categories:
            try:
                df = self._read_term_csv(f"{category}.csv")
            except FileNotFoundError:
                continue
            self.term_dict.update(zip(df['English Term'], df['Chinese Translation']))
            term_dict_loaded = True

        if not term_dict_loaded:
            try:
                df = self._read_term_csv("default.csv")
                self.term_dict.update(zip(df['English Term'], df['Chinese Translation']))
            except FileNotFoundError as exc:
                print(f"Error: Default terminology file not found: {exc}")

    @staticmethod
    def _project_root() -> Path:
        """返回源码项目根目录，不依赖进程当前工作目录。"""
        return Path(__file__).resolve().parents[3]

    @classmethod
    def _resolve_local_path(cls, value: str) -> Path:
        """解析用户传入的相对路径，兼容当前目录和项目根目录。"""
        path = Path(value)
        if path.is_absolute() or path.exists():
            return path
        return (cls._project_root() / path).resolve()

    def _read_term_csv(self, filename: str) -> pd.DataFrame:
        """从配置目录、源码目录或已安装资源中读取术语 CSV。"""
        candidate_paths = []
        configured_dir = self.config.get("terms_dir")
        if configured_dir:
            configured_path = Path(str(configured_dir))
            if not configured_path.is_absolute():
                candidate_paths.append(Path.cwd() / configured_path / filename)
                configured_path = self._project_root() / configured_path
            candidate_paths.append(configured_path / filename)

        candidate_paths.append(self._project_root() / "terms" / filename)
        seen_paths = set()
        for candidate in candidate_paths:
            candidate = candidate.resolve()
            if candidate in seen_paths:
                continue
            seen_paths.add(candidate)
            if candidate.is_file():
                return pd.read_csv(
                    candidate,
                    header=None,
                    names=['English Term', 'Chinese Translation'],
                )

        # 非 editable 安装时从 package_data 读取；as_file 保证压缩包安装也可用。
        try:
            resource = importlib_resources.files("terms").joinpath(filename)
            if resource.is_file():
                with importlib_resources.as_file(resource) as resource_path:
                    return pd.read_csv(
                        resource_path,
                        header=None,
                        names=['English Term', 'Chinese Translation'],
                    )
        except (FileNotFoundError, ModuleNotFoundError, TypeError):
            pass

        missing_path = candidate_paths[0] if candidate_paths else self._project_root() / "terms" / filename
        raise FileNotFoundError(2, "No such file or directory", str(missing_path))

    def add_placeholder(self):
        """收集 caption/env/input/newcommand 的结构占位符。

        占位符单独保存在 ``self.placeholders``，不再写入术语表：
        请求层的 LaTeX 结构保护已把它们替换为必须原样保留的标记，
        放进术语表只会让每次请求都携带整张表。
        """
        placeholder_list: List[str] = []
        for filename in ("inputs_map.json", "envs_map.json", "captions_map.json", "newcommands_map.json"):
            path = os.path.join(self.output_dir, filename)
            if not os.path.isfile(path):
                continue
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for item in data:
                for key in ("begin", "end", "placeholder"):
                    if isinstance(item, dict) and key in item:
                        placeholder_list.append(item[key])

        self.placeholders = list(dict.fromkeys(placeholder_list))
        # 兼容旧版本运行留下的占位符条目。
        for placeholder in self.placeholders:
            if self.term_dict.get(placeholder) == placeholder:
                del self.term_dict[placeholder]
