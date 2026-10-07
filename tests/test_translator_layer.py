import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp

import src.formats.latex.prompts as pm
from src.agents.tool_agents.translator_agent import TranslatorAgent, _ui
from src.formats.latex.chunking import (
    has_translatable_text,
    split_oversize_latex,
    split_translation_chunks,
)
from src.formats.latex.utils import protect_latex_syntax
from src.utils.rate_limit import (
    AdaptiveConcurrencyLimiter,
    backoff_delay,
    parse_retry_after,
)
from src.utils.usage import UsageTracker


def _identity(payload):
    """模拟模型：把用户输入中的受保护文本原样返回。"""
    content = payload["messages"][-1]["content"]
    marker = "\n[LaTeX 内容]\n"
    return content.split(marker, 1)[1] if marker in content else content


class _FakeResponse:
    def __init__(self, content, status=200, headers=None, usage=None):
        self.content = content
        self.status = status
        self.headers = headers or {}
        self.usage = usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status, message="error")

    async def json(self):
        result = {"choices": [{"message": {"content": self.content}}]}
        if self.usage is not None:
            result["usage"] = self.usage
        return result


class _FakeSession:
    """responses 中的元素可以是字符串、可调用对象或 _FakeResponse。"""

    def __init__(self, responses=None, default=None):
        self.responses = list(responses or [])
        self.default = default
        self.payloads = []

    def post(self, url, json, headers, timeout):
        self.payloads.append(json)
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, _FakeResponse):
            return item
        return _FakeResponse(item(json) if callable(item) else item)


def _agent(llm_overrides=None, **kwargs):
    llm_config = {
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
        "model": "test-model",
        "concurrency_limit": 4,
        "rate_limit_backoff_base": 0.01,
        "rate_limit_backoff_max": 0.02,
    }
    llm_config.update(llm_overrides or {})
    config = {
        "source_language": "en",
        "target_language": "ch",
        "llm_config": llm_config,
    }
    return TranslatorAgent(config, **kwargs)


class GlossaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pm.init_prompts("en", "ch")

    def test_selects_only_terms_present_with_word_boundaries(self):
        agent = _agent()
        agent.term_dict = {
            "reinforcement learning": "强化学习",
            "RL": "强化学习（RL）",
            "encoder": "编码器",
            "unused term": "未使用",
            "<PLACEHOLDER_ENV_1>": "<PLACEHOLDER_ENV_1>",
            float("nan"): "x",
            "decoder": float("nan"),
        }
        text = "We train with Reinforcement\nLearning (RL) and an Encoder. See the URL."
        terms = dict(agent._relevant_terms(text))
        self.assertEqual(
            terms,
            {"reinforcement learning": "强化学习", "RL": "强化学习（RL）", "encoder": "编码器"},
        )
        # URL 中的 RL 不算命中。
        self.assertEqual(agent._relevant_terms("Visit the URL page"), [])

    def test_glossary_is_capped(self):
        agent = _agent({"glossary_max_terms": 5})
        agent.term_dict = {f"term{i:02d}": f"术语{i}" for i in range(20)}
        text = " ".join(agent.term_dict)
        self.assertEqual(len(agent._relevant_terms(text)), 5)
        agent.glossary_max_terms = 0
        self.assertEqual(agent._relevant_terms(text), [])

    def test_placeholders_do_not_enter_term_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "inputs_map.json").write_text(json.dumps(
                [{"begin": "<PLACEHOLDER_a_begin>", "end": "<PLACEHOLDER_a_end>"}]), encoding="utf-8")
            Path(tmp, "envs_map.json").write_text(json.dumps(
                [{"placeholder": "<PLACEHOLDER_ENV_1>"}]), encoding="utf-8")
            Path(tmp, "captions_map.json").write_text(json.dumps(
                [{"placeholder": "<PLACEHOLDER_CAP_1>"}]), encoding="utf-8")
            Path(tmp, "newcommands_map.json").write_text("[]", encoding="utf-8")
            agent = _agent(output_dir=tmp)
            agent.term_dict = {"<PLACEHOLDER_ENV_1>": "<PLACEHOLDER_ENV_1>", "model": "模型"}
            agent.add_placeholder()
        self.assertEqual(agent.term_dict, {"model": "模型"})
        self.assertEqual(
            agent.placeholders,
            ["<PLACEHOLDER_a_begin>", "<PLACEHOLDER_a_end>", "<PLACEHOLDER_ENV_1>", "<PLACEHOLDER_CAP_1>"],
        )

    async def test_mode0_injects_only_relevant_terms(self):
        agent = _agent(trans_mode=0)
        agent.term_dict = {"encoder": "编码器", "decoder": "解码器"}
        session = _FakeSession(default=_identity)
        section = {"section": "1", "content": r"\section{Model} The encoder is simple."}
        await agent._translate_section(section, session)
        system_prompt = session.payloads[0]["messages"][0]["content"]
        self.assertIn('"encoder" - "编码器"', system_prompt)
        glossary = system_prompt.split("<Glossary>:", 1)[1]
        self.assertNotIn("decoder", glossary)
        self.assertNotIn("<PLACEHOLDER", glossary)

    async def test_no_glossary_block_without_matches(self):
        agent = _agent()
        agent.term_dict = {"decoder": "解码器"}
        session = _FakeSession(default=_identity)
        await agent._request_llm_for_trans("translate", "The encoder.", "p", "cap", session)
        self.assertNotIn("<Glossary>", session.payloads[0]["messages"][0]["content"])


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_retry_after(self):
        self.assertEqual(parse_retry_after({"Retry-After": "3"}), 3.0)
        self.assertIsNone(parse_retry_after({}))
        self.assertIsNone(parse_retry_after({"Retry-After": "soon"}))
        when = parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"}, now=1445412480.0)
        self.assertAlmostEqual(when, 10.0, places=3)

    def test_backoff_delay_is_exponential_with_jitter_and_honors_retry_after(self):
        self.assertEqual(backoff_delay(0, 2.0, 60.0, rng=lambda: 0.0), 1.0)
        self.assertEqual(backoff_delay(3, 2.0, 60.0, rng=lambda: 1.0), 16.0)
        self.assertEqual(backoff_delay(10, 2.0, 60.0, rng=lambda: 1.0), 60.0)
        self.assertEqual(backoff_delay(0, 2.0, 60.0, retry_after=7.0, rng=lambda: 0.0), 7.0)

    async def test_limiter_aimd(self):
        now = [100.0]
        limiter = AdaptiveConcurrencyLimiter(8, clock=lambda: now[0])
        self.assertTrue(limiter.on_throttle(2.0))
        self.assertEqual(limiter.limit, 4)
        # 同一冷却窗口内的后续 429 不再减半。
        self.assertFalse(limiter.on_throttle(2.0))
        self.assertEqual(limiter.limit, 4)
        self.assertTrue(limiter.throttled)
        now[0] += 3.0
        for _ in range(4):
            limiter.on_success()
        self.assertEqual(limiter.limit, 5)

    async def test_limiter_blocks_above_limit(self):
        limiter = AdaptiveConcurrencyLimiter(1)
        await limiter.acquire()
        waiter = asyncio.ensure_future(limiter.acquire())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        limiter.release()
        await asyncio.wait_for(waiter, 1)
        self.assertEqual(limiter.active, 1)

    async def test_429_is_retried_without_counting_as_model_failure(self):
        agent = _agent({"retry_backoff": 0})
        session = _FakeSession([
            _FakeResponse("", status=429, headers={"Retry-After": "0"}),
            _FakeResponse("", status=503),
            "翻译结果",
        ])
        result = await agent._request_llm_for_trans("translate", "Some text.", "1", "sec", session)
        self.assertEqual(result, "翻译结果")
        self.assertFalse(agent.have_fail_parts)
        self.assertEqual(len(session.payloads), 3)
        self.assertLess(agent._limiter.limit, 4)
        events = agent.usage.snapshot()["events"]
        self.assertEqual(events["rate_limited_retries"], 1)
        self.assertEqual(events["server_error_retries"], 1)

    async def test_exhausted_rate_limit_raises_client_error(self):
        agent = _agent({"rate_limit_retries": 1})
        session = _FakeSession([
            _FakeResponse("", status=429),
            _FakeResponse("", status=429),
        ])
        with self.assertRaises(aiohttp.ClientResponseError):
            await agent._post_chat_once(session, {"messages": []})
        self.assertEqual(len(session.payloads), 2)


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pm.init_prompts("en", "ch")

    def test_upstream_timeout_key_is_accepted(self):
        self.assertEqual(_agent().get_request_timeout(), 300.0)
        self.assertEqual(_agent({"timeout": 100}).get_request_timeout(), 100.0)
        self.assertEqual(
            _agent({"timeout": 100, "request_timeout": 200}).get_request_timeout(), 200.0
        )

    async def test_sampling_parameters_come_from_config(self):
        agent = _agent({"max_tokens": 1234, "temperature": 0.2})
        session = _FakeSession(default=_identity)
        await agent._request_llm_for_trans("translate", "Hello world.", "1", "sec", session)
        payload = session.payloads[0]
        self.assertEqual(payload["max_tokens"], 1234)
        self.assertEqual(payload["temperature"], 0.2)
        defaults = _agent()
        self.assertEqual((defaults.max_tokens, defaults.temperature), (8192, 0.7))

    def test_hedge_rate_is_configurable(self):
        payload = {"messages": [{"content": "sys"}, {"content": "x" * 1800}]}
        self.assertAlmostEqual(_agent()._hedge_delay(payload), 2.0 * (0.5 + 2.0))
        fast = _agent({"hedge_chars_per_sec": 1800, "hedge_min_delay": 0.5})
        self.assertAlmostEqual(fast._hedge_delay(payload), 2.0 * (0.5 + 1.0))


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def test_tracker_parses_and_prices_usage(self):
        tracker = UsageTracker(
            project="p", price_input_per_mtok=2.0, price_output_per_mtok=8.0,
            price_cached_input_per_mtok=0.5,
        )
        tracker.record({
            "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 400},
            "completion_tokens_details": {"reasoning_tokens": 100},
        })
        tracker.record({"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 10})
        tracker.record(None)
        data = tracker.snapshot()
        self.assertEqual(data["requests"], 3)
        self.assertEqual(data["requests_without_usage"], 1)
        self.assertEqual(data["prompt_tokens"], 1010)
        self.assertEqual(data["completion_tokens"], 505)
        self.assertEqual(data["total_tokens"], 1515)
        self.assertEqual(data["reasoning_tokens"], 100)
        self.assertEqual(data["cached_prompt_tokens"], 410)
        expected_input = (600 * 2.0 + 410 * 0.5) / 1e6
        self.assertAlmostEqual(data["cost"]["input"], expected_input, places=6)
        self.assertAlmostEqual(data["cost"]["output"], 505 * 8.0 / 1e6, places=6)
        self.assertNotIn("cost", UsageTracker().snapshot())

    async def test_requests_are_recorded_per_agent_and_written(self):
        first, second = _agent(project_dir="a"), _agent(project_dir="b")
        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        session = _FakeSession([_FakeResponse("译文", usage=usage)])
        await first._request_llm_for_trans("translate", "Text here.", "1", "sec", session)
        self.assertEqual(first.usage.snapshot()["total_tokens"], 10)
        self.assertEqual(second.usage.snapshot()["total_tokens"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            first.output_dir = tmp
            first._write_usage()
            written = json.loads(Path(tmp, "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(written["project"], "a")
        self.assertEqual(written["prompt_tokens"], 7)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pm.init_prompts("en", "ch")
        self.sections = [
            {"section": "0", "content": "\\begin{document}\n<PLACEHOLDER_ENV_1>"},
            {"section": "1", "content": "\\section{Method}\nOur method <PLACEHOLDER_ENV_2> works."},
        ]
        self.envs = [
            {"placeholder": "<PLACEHOLDER_ENV_1>", "env_name": "abstract",
             "content": "\\begin{abstract}We study critics.\\end{abstract}", "need_trans": True},
            {"placeholder": "<PLACEHOLDER_ENV_2>", "env_name": "figure",
             "content": "\\begin{figure}<PLACEHOLDER_CAP_2>\\end{figure}", "need_trans": True},
        ]
        self.captions = [
            {"placeholder": "<PLACEHOLDER_CAP_1>", "cap_type": "title",
             "content": "\\title{Stable Critic Training}"},
            {"placeholder": "<PLACEHOLDER_CAP_2>", "cap_type": "caption",
             "content": "\\caption{Overview.}"},
        ]

    async def test_summary_helpers_use_shared_request_layer(self):
        agent = _agent()
        session = _FakeSession(["a summary", "a refined summary"])
        self.assertEqual(
            await agent._request_llm_for_summary(pm.get_summary_system_prompt, "text", session),
            "a summary",
        )
        self.assertEqual(
            await agent._request_llm_for_refine_summary(
                pm.refine_summary_system_prompt, "text", "old", session),
            "a refined summary",
        )

    async def test_context_is_off_by_default(self):
        agent = _agent()
        session = _FakeSession(default=_identity)
        await agent._prepare_context(self.sections, self.envs, self.captions, session)
        await agent._request_llm_for_trans("translate", "Our method works.", "1", "sec", session)
        self.assertNotIn("<Document context>", session.payloads[0]["messages"][0]["content"])
        self.assertEqual(len(session.payloads), 1)

    async def test_context_includes_title_abstract_and_section(self):
        agent = _agent({"use_context": True})
        session = _FakeSession(default=_identity)
        await agent._prepare_context(self.sections, self.envs, self.captions, session)
        self.assertEqual(session.payloads, [])  # 默认用摘要原文，不额外请求
        await agent._translate_caption(dict(self.captions[1]), session)
        system_prompt = session.payloads[0]["messages"][0]["content"]
        self.assertIn("Paper title: Stable Critic Training", system_prompt)
        self.assertIn("We study critics.", system_prompt)
        self.assertIn("Current section: Method", system_prompt)
        self.assertIn("summary", pm.caption_system_prompt_with_sum)
        self.assertTrue(system_prompt.startswith(pm.caption_system_prompt_with_sum))

    async def test_context_summary_is_generated_once(self):
        agent = _agent({"use_context": "true", "context_summary": "true"})
        session = _FakeSession(["LLM generated summary."], default=_identity)
        await agent._prepare_context(self.sections, self.envs, self.captions, session)
        await agent._prepare_context(self.sections, self.envs, self.captions, session)
        self.assertEqual(len(session.payloads), 1)
        self.assertEqual(agent.summary, "LLM generated summary.")


class ThreadSafetyTests(unittest.TestCase):
    def test_translator_does_not_reassign_global_stderr(self):
        for name in (
            "src/agents/tool_agents/translator_agent.py",
            "src/agents/tool_agents/base_tool_agent.py",
        ):
            source = Path(name).read_text(encoding="utf-8")
            self.assertNotIn("sys.stderr =", source, name)

    def test_ui_errors_are_swallowed(self):
        def broken():
            raise RuntimeError("no script run context")

        widget = _ui(broken)
        with widget:
            widget.progress(5).text("ok")


class ChunkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pm.init_prompts("en", "ch")

    @staticmethod
    def _align(rows):
        body = "".join(
            f"x_{{{i}}} &= \\ensuremath{{\\sqrt{{{i}}}}} + y_{{{i}}} \\\\\n" for i in range(rows)
        )
        return "\\begin{align}\n" + body + "\\end{align}"

    def test_splits_align_at_row_ends_losslessly(self):
        text = self._align(120)
        chunks = split_translation_chunks(text, 1000)
        self.assertGreater(len(chunks), 3)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\\\\\n"), chunk[-20:])

    def test_never_splits_inside_math_or_braces(self):
        row = "Row text with words \\\\[2pt]\n"
        protected_math = "$a +\n" + "b + " * 300 + "c$"
        protected_group = "\\textbf{" + "word\n" * 300 + "}"
        text = row * 40 + protected_math + "\n" + row * 40 + protected_group + "\n" + row * 10
        chunks = split_oversize_latex(text, 600)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(any(protected_math in chunk for chunk in chunks))
        self.assertTrue(any(protected_group in chunk for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(chunk.count("$") % 2, 0)
            self.assertNotIn("\n[2pt]", chunk)

    def test_prefers_sentence_ends_for_prose_lines(self):
        line_a = "This is a long line of reasoning that continues\n"
        line_b = "onto the next line and ends here.\n"
        text = (line_a + line_b) * 60
        chunks = split_oversize_latex(text, 700)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("ends here.\n"))

    def test_translatable_text_detection(self):
        math_only = protect_latex_syntax(r"x_{1} &= \alpha y + \beta \\").protected_text
        prose = protect_latex_syntax(r"x &= y \quad \text{for all items} \\").protected_text
        self.assertFalse(has_translatable_text(math_only))
        self.assertTrue(has_translatable_text(prose))

    async def test_oversize_env_is_split_and_reassembled(self):
        agent = _agent({"max_chunk_chars": 1000})
        rows = []
        for i in range(80):
            if i % 20 == 0:
                rows.append(f"x_{{{i}}} &= y \\quad \\text{{holds for every node}} \\\\\n")
            else:
                rows.append(f"x_{{{i}}} &= \\ensuremath{{\\sqrt{{{i}}}}} + y_{{{i}}} \\\\\n")
        text = "\\begin{align}\n" + "".join(rows) + "\\end{align}"

        def translate(payload):
            return _identity(payload).replace("holds for every node", "对每个节点成立")

        session = _FakeSession(default=translate)
        result = await agent._request_llm_for_trans("translate", text, "<PLACEHOLDER_ENV_9>", "env", session)
        self.assertEqual(result, text.replace("holds for every node", "对每个节点成立"))
        self.assertEqual(result.count("\\ensuremath"), text.count("\\ensuremath"))
        # 只有含自然语言的分块才请求模型，纯公式分块直接保留原文。
        self.assertGreaterEqual(len(session.payloads), 2)
        self.assertLess(len(session.payloads), len(split_translation_chunks(text, 1000)) + 1)
        for payload in session.payloads:
            self.assertIn("holds for every node", payload["messages"][-1]["content"])
        self.assertFalse(agent.have_fail_parts)


if __name__ == "__main__":
    unittest.main()
