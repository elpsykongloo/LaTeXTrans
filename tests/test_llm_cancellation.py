import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.agents.tool_agents.translator_agent import TranslatorAgent


def _agent(**kwargs):
    return TranslatorAgent(
        {
            "source_language": "en",
            "target_language": "ch",
            "llm_config": {
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
                "concurrency_limit": 4,
            },
        },
        project_dir="test-project",
        **kwargs,
    )


class RequestCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def _cancel_request(self, after_hedging):
        agent = _agent()
        agent._hedge_delay = lambda payload: 0.0 if after_hedging else 60.0
        started = asyncio.Event()
        requests, cleaned = [], []
        limiter = agent._get_limiter()

        async def request(session, payload):
            await limiter.acquire()
            task = asyncio.current_task()
            requests.append(task)
            if len(requests) == (2 if after_hedging else 1):
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(task)
                limiter.release()

        agent._post_chat_once = request
        parent = asyncio.create_task(agent._post_chat(None, {}))
        await asyncio.wait_for(started.wait(), 1)
        parent.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await parent
        self.assertEqual(len(requests), 2 if after_hedging else 1)
        self.assertTrue(all(task.done() for task in requests))
        self.assertEqual(len(cleaned), len(requests))
        self.assertEqual(limiter.active, 0)

    async def test_cancel_before_hedging_drains_primary_and_releases_slot(self):
        await self._cancel_request(after_hedging=False)

    async def test_cancel_after_hedging_drains_both_and_releases_slots(self):
        await self._cancel_request(after_hedging=True)

    async def test_successful_backup_drains_losing_primary_before_return(self):
        agent = _agent()
        agent._hedge_delay = lambda payload: 0.0
        requests, cleaned = [], []

        async def request(session, payload):
            task = asyncio.current_task()
            requests.append(task)
            if len(requests) == 2:
                return {"result": "backup"}
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(task)

        agent._post_chat_once = request
        result = await agent._post_chat(None, {})
        self.assertEqual(result, {"result": "backup"})
        self.assertTrue(all(task.done() for task in requests))
        self.assertEqual(cleaned, requests[:1])


class BatchCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_chunk_drains_running_sibling(self):
        agent = _agent()
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def request(system_prompt, chunk, **kwargs):
            if chunk == "bad":
                await started.wait()
                raise RuntimeError("chunk failed")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with self.assertRaisesRegex(RuntimeError, "chunk failed"):
            await agent._translate_chunks(request, ["bad", "slow"], "translate", "1", "sec", None, 0)
        self.assertTrue(cleaned.is_set())

    async def test_failed_translation_drains_units_and_judges_before_session_exit(self):
        agent = _agent(output_dir="unused-output")
        started = asyncio.Event()
        children, cleaned = [], []

        async def blocked():
            task = asyncio.current_task()
            children.append(task)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(task)

        async def translate(kind, index, *args):
            if index == 0:
                await started.wait()
                raise RuntimeError("unit failed")
            started.set()
            await blocked()

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(session, *args):
                self.assertEqual(len(cleaned), 2)
                self.assertTrue(all(task.done() for task in children))

        agent._new_session = Session
        agent._prepare_context = AsyncMock()
        agent._translate_unit = translate
        agent._start_need_trans_judges = lambda *args: {0: asyncio.create_task(blocked())}
        agent.add_placeholder = lambda: None
        agent.build_term_dict = lambda: None
        maps = {
            "sections_map.json": [
                {"section": "1", "content": "First."},
                {"section": "2", "content": "Second."},
            ],
            "envs_map.json": [],
            "captions_map.json": [],
        }
        agent.read_file = lambda path, kind: maps[Path(path).name]
        with self.assertRaisesRegex(RuntimeError, "unit failed"):
            await agent._execute()
        self.assertEqual(len(cleaned), 2)

    async def test_failed_retranslation_drains_running_sibling(self):
        agent = _agent()
        started = asyncio.Event()
        cleaned = asyncio.Event()
        agent.errors_report = [
            {"part": "sec", "num_or_ph": "1"},
            {"part": "sec", "num_or_ph": "2"},
        ]

        async def translate(section, **kwargs):
            if section["section"] == "1":
                await started.wait()
                raise RuntimeError("retry failed")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        agent._translate_section = translate
        with self.assertRaisesRegex(RuntimeError, "retry failed"):
            await agent._retranslate_error_parts(
                [{"section": "1", "content": "First."}, {"section": "2", "content": "Second."}],
                [], [], None,
            )
        self.assertTrue(cleaned.is_set())


if __name__ == "__main__":
    unittest.main()
