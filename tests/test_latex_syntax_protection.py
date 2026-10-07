import json
import unittest
from pathlib import Path

from src.agents.tool_agents.translator_agent import TranslatorAgent
from src.agents.tool_agents.validator_agent import ValidatorAgent
from src.formats.latex.utils import find_latex_bracket_errors, protect_latex_syntax


class _FakeResponse:
    def __init__(self, content):
        self.content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def post(self, url, json, headers, timeout):
        self.payloads.append(json)
        content = self.responses.pop(0)
        return _FakeResponse(content(json) if callable(content) else content)


class LatexSyntaxProtectionTests(unittest.IsolatedAsyncioTestCase):
    def test_target_abstract_keeps_all_individual_textbf_commands(self):
        output_path = Path("outputs/ch_2608.23566/envs_map.json")
        if not output_path.is_file():
            self.skipTest("当前工作区没有该论文的解析产物")

        parts = json.loads(output_path.read_text(encoding="utf-8"))
        source = next(
            part["content"]
            for part in parts
            if part["placeholder"] == "<PLACEHOLDER_ENV_1>"
        )
        protection = protect_latex_syntax(source)

        self.assertEqual(source.count(r"\textbf"), 5)
        # 末尾位置确定的命令已留在本地，不再交给模型复述。
        self.assertEqual(len(protection.token_order), 8)
        self.assertTrue(protection.fixed_suffix)
        self.assertNotIn(r"\textbf", protection.protected_text)
        self.assertEqual(protection.restore(protection.protected_text), source)

        translated = protection.protected_text.replace(
            "Best Practice Critic Optimization", "最佳实践评论家优化"
        )
        translated = translated.replace(
            "The BPCO name also reflects its", "BPCO这一名称也反映了其"
        )
        restored = protection.restore(translated)
        self.assertEqual(restored.count(r"\textbf"), 5)
        self.assertIn(r"\textbf{B}", restored)
        self.assertIn(r"\textbf{P}", restored)
        self.assertIn(r"\textbf{C}", restored)
        self.assertIn(r"\textbf{O}", restored)

    def test_protection_covers_math_references_urls_and_placeholders(self):
        source = (
            r"\begin{figure}[ht] "
            r"\includegraphics[width=0.5\textwidth]{fig/a_b.pdf} "
            r"\citep{paper_a} \ref{sec:intro} "
            r"\href{https://example.test/a_b}{Click \textbf{here}} "
            r"$\alpha+1$ \verb|raw_command| "
            r"\hyperref[sec:one_two]{jump} <PLACEHOLDER_CAP_1> \end{figure}"
        )
        protection = protect_latex_syntax(source)

        self.assertNotIn(r"\includegraphics", protection.protected_text)
        self.assertNotIn(r"https://example.test/a_b", protection.protected_text)
        self.assertNotIn(r"$\alpha+1$", protection.protected_text)
        self.assertNotIn(r"\verb", protection.protected_text)
        self.assertNotIn(r"sec:one_two", protection.protected_text)
        self.assertIn(r"Click", protection.protected_text)
        self.assertIsNone(protection.validate(protection.protected_text))
        self.assertEqual(protection.restore(protection.protected_text), source)

    def test_protection_rejects_missing_duplicate_or_reordered_markers(self):
        source = r"start \textbf{A} and \emph{B} end"
        protection = protect_latex_syntax(source)
        first, second = protection.token_order

        self.assertIsNotNone(protection.validate(first))
        self.assertIsNotNone(protection.validate(f"{first} {first}"))
        # 普通格式命令可以随目标语言语序移动，只要每个标记都保留。
        self.assertIsNone(protection.validate(f"{second} {first}"))

        critical_source = r"prefix \begin{figure}text\end{figure} suffix"
        critical_protection = protect_latex_syntax(critical_source)
        critical_first, critical_second = critical_protection.token_order
        self.assertIsNotNone(
            critical_protection.validate(f"{critical_second}{critical_first}")
        )

    def test_non_xml_markers_and_fixed_suffix_restore_exact_source(self):
        source = (
            "\\section{Conclusion}\n"
            "\\label{sec:end}\n\n"
            "Body.\n\n"
            "\\vfill\\pagebreak\n"
            "\\bibliography{references.bib}\n"
            "\\end{document}"
        )
        protection = protect_latex_syntax(source)

        self.assertIn(r"\vfill\pagebreak", protection.fixed_suffix)
        self.assertIn(r"\end{document}", protection.fixed_suffix)
        self.assertEqual(len(protection.token_order), 2)
        self.assertNotIn("<LATEXTRANS", protection.protected_text)
        self.assertIn("[[[LATEXTRANS_TOKEN_", protection.protected_text)
        self.assertEqual(protection.restore(protection.protected_text), source)

    def test_removes_model_echo_of_locally_managed_suffix(self):
        source = "Body.\n\\vfill\\pagebreak\n\\end{document}"
        protection = protect_latex_syntax(source)
        echoed = "正文。\n\\vfill\\pagebreak\n\\end{document}"

        restored = protection.restore(echoed)

        self.assertEqual(restored.count(r"\vfill"), 1)
        self.assertEqual(restored.count(r"\pagebreak"), 1)
        self.assertEqual(restored.count(r"\end{document}"), 1)
        self.assertTrue(restored.startswith("正文。"))

    def test_rejects_model_generated_closing_marker_artifacts(self):
        source = r"prefix \textbf{value} suffix"
        protection = protect_latex_syntax(source)
        token = protection.token_order[0]
        fake_closing = token.replace("[[[", "[[[/", 1)
        wrapped = protection.protected_text.replace(
            token,
            f"{token}译文{fake_closing}",
        )
        legacy_closing = protection.protected_text + "</LATEXTRANS_TOKEN_1>"

        self.assertIn("伪造的结构标记", protection.validate(wrapped))
        self.assertIn("伪造的结构标记", protection.validate(legacy_closing))

    def test_control_word_keeps_original_separator_before_chinese_text(self):
        source = r"\model achieves state-of-the-art results."
        protection = protect_latex_syntax(source)
        translated = protection.protected_text.replace(
            "achieves state-of-the-art results.", "取得了最先进的结果。"
        )

        self.assertEqual(protection.replacements[protection.token_order[0]], r"\model ")
        self.assertEqual(
            protection.restore(translated), r"\model 取得了最先进的结果。"
        )
        validator = ValidatorAgent({}, project_dir="project", output_dir="output")
        self.assertIsNone(validator._validate_command({
            "content": source, "trans_content": protection.restore(translated)
        }))

    async def test_request_retries_after_structure_mismatch(self):
        source = r"\textbf{Best} and \textbf{Practice}."
        protection = protect_latex_syntax(source)
        session = _FakeSession(
            [
                "invalid",
                protection.protected_text.replace("Best", "最佳").replace(
                    "Practice", "实践"
                ),
            ]
        )
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        result = await agent._request_llm_for_trans(
            "translate",
            source,
            fail_part="test-part",
            type="sec",
            session=session,
        )

        self.assertEqual(result.count(r"\textbf"), 2)
        self.assertIn(r"\textbf{最佳}", result)
        self.assertIn(r"\textbf{实践}", result)
        self.assertFalse(agent.have_fail_parts)
        self.assertEqual(len(session.payloads), 2)
        self.assertEqual(session.payloads[1]["temperature"], 0.0)
        retry_prompt = session.payloads[1]["messages"][-1]["content"]
        for token in protection.token_order:
            self.assertIn(token, retry_prompt)

    async def test_request_restores_fixed_suffix_without_model_echo(self):
        source = (
            "\\section{Conclusion}\n"
            "\\label{sec:end}\n\n"
            "Body.\n\n"
            "\\vfill\\pagebreak\n"
            "\\end{document}"
        )
        protection = protect_latex_syntax(source)
        candidate = protection.protected_text.replace("Conclusion", "结论").replace(
            "Body.", "正文。"
        )
        session = _FakeSession([candidate])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        result = await agent._request_llm_for_trans(
            "translate",
            source,
            fail_part="test-boundaries",
            type="sec",
            session=session,
        )

        self.assertTrue(result.startswith(r"\section{结论}"))
        self.assertIn(r"\label{sec:end}", result)
        self.assertTrue(result.endswith("\\vfill\\pagebreak\n\\end{document}"))
        self.assertEqual(len(session.payloads), 1)

    async def test_syntax_only_part_skips_model_request(self):
        source = "\\vfill\\pagebreak\n\\end{document}"
        session = _FakeSession([])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        result = await agent._request_llm_for_trans(
            "translate",
            source,
            fail_part="syntax-only",
            type="sec",
            session=session,
        )

        self.assertEqual(result, source)
        self.assertEqual(session.payloads, [])

    async def test_retranslation_uses_clean_source_instead_of_broken_draft(self):
        source = r"\caption{{Comparison between TTS models.}}"
        broken = r"\caption\caption{TTS模型之间的比较。}"
        protection = protect_latex_syntax(source, protect_group_delimiters=True)
        candidate = protection.protected_text.replace(
            "Comparison between TTS models.",
            "TTS模型之间的比较。",
        )
        session = _FakeSession([candidate])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            },
            trans_mode=1,
        )

        result = await agent._request_llm_for_retrans_error_parts(
            "old correction prompt",
            part={"content": source, "trans_content": broken},
            error_message=r"\caption expected 1, found 2",
            fail_part="caption-retry",
            type="cap",
            session=session,
        )

        self.assertEqual(result, r"\caption{{TTS模型之间的比较。}}")
        self.assertEqual(result.count(r"\caption"), 1)
        self.assertFalse(agent.have_fail_parts)
        self.assertEqual(len(session.payloads), 1)
        self.assertEqual(session.payloads[0]["temperature"], 0.0)
        user_prompt = session.payloads[0]["messages"][-1]["content"]
        self.assertNotIn(broken, user_prompt)
        self.assertNotIn("[Translation]", user_prompt)
        for token in protection.token_order:
            self.assertIn(token, user_prompt)

    async def test_caption_retry_preserves_nested_group_boundaries(self):
        source = (
            r"\caption{\textbf{Source-clean overlap.} "
            r"Success on $498$ examples.}"
        )
        protection = protect_latex_syntax(
            source, protect_group_delimiters=True
        )
        nested_close = next(
            token for token, fragment in protection.replacements.items()
            if fragment == "}"
        )
        translated = protection.protected_text.replace(
            "Source-clean overlap.", "干净来源的重叠处理。"
        ).replace("Success on", "成功率基于")
        session = _FakeSession([
            translated.replace(nested_close, "", 1),
            "}" + translated,
            translated,
        ])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        result = await agent._request_llm_for_trans(
            "translate", source, fail_part="caption", type="cap", session=session
        )

        self.assertIn(r"\textbf{干净来源的重叠处理。}", result)
        self.assertTrue(result.endswith("examples.}"))
        self.assertEqual(find_latex_bracket_errors(result), [])
        self.assertEqual(len(session.payloads), 3)
        self.assertFalse(agent.have_fail_parts)

    async def test_request_failure_is_not_silently_treated_as_translation(self):
        source = r"\textbf{Best} and \textbf{Practice}."
        session = _FakeSession([
            "invalid", "invalid", "invalid", "not JSON", "[]"
        ])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        result = await agent._request_llm_for_trans(
            "translate",
            source,
            fail_part="test-part",
            type="sec",
            session=session,
        )

        self.assertEqual(result, source)
        self.assertTrue(agent.have_fail_parts)
        self.assertEqual(agent.fail_section_nums, ["test-part"])
        self.assertEqual(len(session.payloads), 5)

    async def test_segmented_fallback_restores_omitted_format_command(self):
        source = (
            r"\subsection{Knowledge} For \model-30B-A3B in "
            r"\emph{thinking} mode."
        )
        protection = protect_latex_syntax(source)
        omitted = protection.protected_text.replace(protection.token_order[-1], "")

        def translated_segments(payload):
            segments = json.loads(payload["messages"][-1]["content"])["segments"]
            return json.dumps([
                segment.replace("Knowledge", "知识")
                .replace("For", "对于")
                .replace("thinking", "思考")
                .replace("mode", "模式")
                for segment in segments
            ], ensure_ascii=False)

        session = _FakeSession([omitted, omitted, omitted, translated_segments])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        result = await agent._request_llm_for_trans(
            "translate", source, fail_part="8_2", type="sec", session=session
        )

        self.assertIn(r"\subsection{知识}", result)
        self.assertIn(r"\emph{思考}", result)
        self.assertIn(r"\model-30B-A3B", result)
        self.assertFalse(agent.have_fail_parts)
        self.assertEqual(len(session.payloads), 4)

    async def test_segmented_fallback_restores_repeated_escaped_ampersands(self):
        source = (
            r"\begin{itemize}\item \textbf{AIME 2025 \& 2026}~"
            r"\citep{aime2025}: 30 \& 30 problems.\end{itemize}"
        )
        protection = protect_latex_syntax(source)
        second_ampersand = [
            token for token, fragment in protection.replacements.items()
            if fragment == r"\&"
        ][1]
        omitted = protection.protected_text.replace(second_ampersand, "")

        def translated_segments(payload):
            segments = json.loads(payload["messages"][-1]["content"])["segments"]
            return json.dumps([
                segment.replace("problems", "道题") for segment in segments
            ], ensure_ascii=False)

        session = _FakeSession([omitted, omitted, omitted, translated_segments])
        agent = TranslatorAgent(
            {
                "source_language": "English",
                "target_language": "Chinese",
                "llm_config": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "concurrency_limit": 1,
                },
            }
        )

        agent.term_dict = {"problems": "道题"}
        result = await agent._request_llm_for_trans_with_terms(
            "translate", source, fail_part="env-29", type="env", session=session
        )

        self.assertEqual(result.count(r"\&"), 2)
        self.assertIn(r"\textbf{AIME 2025 \& 2026}", result)
        self.assertIn(r"\citep{aime2025}", result)
        self.assertIn("30 道题", result)
        self.assertTrue(result.endswith(r"\end{itemize}"))
        self.assertFalse(agent.have_fail_parts)
        self.assertEqual(len(session.payloads), 4)
        self.assertIn("problems", session.payloads[-1]["messages"][0]["content"])


class ValidatorCommandTests(unittest.TestCase):
    def setUp(self):
        self.validator = ValidatorAgent(
            config={},
            project_dir="project",
            output_dir="output",
        )

    def test_reports_missing_repeated_commands(self):
        error = self.validator._validate_command(
            {
                "content": r"\textbf{B}ounded \textbf{P}rivileged \textbf{C}ritic \textbf{O}ptimization",
                "trans_content": r"\textbf{有界特权评论家优化}",
            }
        )

        self.assertIsNotNone(error)
        self.assertIn(r"'\textbf'", error)
        self.assertIn("expected 4", error)
        self.assertIn("found 1", error)

    def test_reports_extra_commands(self):
        error = self.validator._validate_command(
            {
                "content": r"\emph{source}",
                "trans_content": r"\emph{翻译} \textbf{额外}",
            }
        )

        self.assertIsNotNone(error)
        self.assertIn(r"'\textbf'", error)
        self.assertIn("source expected 0", error)

    def test_reports_command_order_change(self):
        error = self.validator._validate_command(
            {
                "content": r"\begin{figure}a\end{figure} \begin{table}b\end{table}",
                "trans_content": r"\begin{table}甲\end{table} \begin{figure}乙\end{figure}",
            }
        )

        self.assertIsNotNone(error)
        self.assertIn("环境边界顺序", error)

    def test_allows_inline_commands_to_follow_target_language_word_order(self):
        error = self.validator._validate_command(
            {
                "content": r"\textbf{A} \emph{B}",
                "trans_content": r"\emph{甲} \textbf{乙}",
            }
        )

        self.assertIsNone(error)

    def test_reports_duplicate_or_reordered_placeholders(self):
        duplicate_error = self.validator._validate_placeholder(
            {
                "content": "<PLACEHOLDER_ENV_1><PLACEHOLDER_CAP_1>",
                "trans_content": (
                    "<PLACEHOLDER_ENV_1><PLACEHOLDER_ENV_1>"
                    "<PLACEHOLDER_CAP_1>"
                ),
            }
        )
        reordered_error = self.validator._validate_placeholder(
            {
                "content": "<PLACEHOLDER_ENV_1><PLACEHOLDER_CAP_1>",
                "trans_content": "<PLACEHOLDER_CAP_1><PLACEHOLDER_ENV_1>",
            }
        )

        self.assertIn("Extra placeholders", duplicate_error)
        self.assertIn("order changed", reordered_error)

    def test_ignores_round_parentheses_used_as_prose_punctuation(self):
        part = {
            "content": (
                r"\noindent\textbf{Image} capabilities include: "
                r"1) understanding and 2) reasoning."
            ),
            "trans_content": (
                r"\noindent\textbf{图像}能力包括：1) 理解能力和 2) 推理能力。"
            ),
        }

        self.assertIsNone(self.validator._validate_closed_brackets(part))


if __name__ == "__main__":
    unittest.main()
