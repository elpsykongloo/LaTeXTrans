import asyncio
import copy
import json
from functools import wraps
from pathlib import Path
from unittest import mock

import pytest

import main as cli_main
import src.agents.coordinator_agent as coordinator_module
import src.runtime as runtime
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.tool_agents.translator_agent import TranslatorAgent
from src.utils.checkpoint import CheckpointStore, MAP_FILES, atomic_write_json, read_json


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(r"\documentclass{article}\begin{document}Text\end{document}", encoding="utf-8")
    config = {
        "source_language": "en", "target_language": "ch", "copy_to_downloads": False,
        "llm_config": {"model": "test-model", "base_url": "https://example.test/v1", "api_key": "test-key", "hedge_factor": 0},
    }
    output = tmp_path / "out" / "ch_paper"
    output.mkdir(parents=True)
    return source, output, config


def write_maps(output, translated=False):
    sections = [
        {"section": "1", "content": "First paragraph.", "trans_content": "第一段。" if translated else ""},
        {"section": "2", "content": "Second paragraph.", "trans_content": "第二段。" if translated else ""},
    ]
    for filename in MAP_FILES:
        atomic_write_json(output / filename, sections if filename == "sections_map.json" else [])
    return sections


def parsed_store(source, output, config):
    store = CheckpointStore(str(output), str(source), config)
    write_maps(output)
    store.stage_completed("parse", snapshot=True)
    return store


def test_metadata_is_readable_and_secret_or_concurrency_changes_do_not_invalidate(project):
    source, output, config = project
    store = parsed_store(source, output, config)
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["metadata"]["sources"][0]["path"] == "main.tex"
    assert "mtime_ns" in saved["metadata"]["sources"][0]
    assert "api_key" not in saved["metadata"]["translation"]["llm"]
    changed = copy.deepcopy(config)
    changed["llm_config"].update(api_key="", concurrency_limit=1, retry_backoff=100)
    assert CheckpointStore(str(output), str(source), changed).valid


@pytest.mark.parametrize("changed_key,new_value", [("model", "other-model"), ("temperature", 0.1), ("use_context", True)])
def test_translation_settings_invalidate_saved_fragments(project, changed_key, new_value):
    source, output, config = project
    parsed_store(source, output, config)
    changed = copy.deepcopy(config)
    changed["llm_config"][changed_key] = new_value
    assert not CheckpointStore(str(output), str(source), changed).valid


def test_source_and_term_changes_invalidate(project, tmp_path):
    source, output, config = project
    terms = tmp_path / "terms.csv"
    terms.write_text("encoder,编码器\n", encoding="utf-8")
    config["user_term"] = str(terms)
    parsed_store(source, output, config)
    terms.write_text("encoder,编码网络\n", encoding="utf-8")
    assert not CheckpointStore(str(output), str(source), config).valid
    parsed_store(source, output, config)
    (source / "main.tex").write_text("changed source", encoding="utf-8")
    assert not CheckpointStore(str(output), str(source), config).valid


def test_validator_setting_and_force_invalidate(project):
    source, output, config = project
    parsed_store(source, output, config)
    for override in ({"resume": False}, {"force": True}, {"validation_prose_equivalences": False}):
        assert not CheckpointStore(str(output), str(source), {**config, **override}).valid


@pytest.mark.parametrize("field", ["metadata", "stages", "outcome", "bilingual"])
def test_malformed_checkpoint_data_restarts_safely(project, field):
    source, output, config = project
    store = parsed_store(source, output, config)
    data = read_json(store.path)
    data[field] = ["invalid shape"]
    atomic_write_json(store.path, data)
    recovered = CheckpointStore(str(output), str(source), config)
    assert not recovered.valid
    assert not recovered.restore_maps()


def test_original_pdf_download_does_not_invalidate_source_snapshot(project):
    source, output, config = project
    parsed_store(source, output, config)
    (source / "paper.pdf").write_bytes(b"%PDF-1.4")
    assert CheckpointStore(str(output), str(source), config).valid


def test_corrupt_later_snapshots_rewind_later_stage_flags(project):
    source, output, config = project
    store = parsed_store(source, output, config)
    write_maps(output, translated=True)
    store.stage_completed("translate", snapshot=True)
    store.stage_completed("validate", snapshot=True)
    (store.run_dir / "validate" / "sections_map.json").write_text("invalid JSON", encoding="utf-8")
    (store.run_dir / "translate" / "sections_map.json").unlink()
    resumed = CheckpointStore(str(output), str(source), config)
    assert resumed.restore_maps()
    assert resumed.is_complete("parse")
    assert not resumed.is_complete("translate") and not resumed.is_complete("validate")
    assert read_json(output / "sections_map.json")[0]["trans_content"] == ""


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@_async_test
async def test_interrupt_saves_successful_fragment_and_restart_only_translates_remaining(project, monkeypatch):
    source, output, config = project
    store = parsed_store(source, output, config)
    store.begin("translate")
    first_agent = TranslatorAgent(config, project_dir=str(source), output_dir=str(output))
    first_agent.checkpoint = store
    monkeypatch.setattr(first_agent, "_new_session", FakeSession)

    async def interrupted(section, session, **kwargs):
        if section["section"] == "2":
            await asyncio.sleep(0.02)
            raise RuntimeError("simulated interruption")
        return {**section, "trans_content": "第一段。"}

    monkeypatch.setattr(first_agent, "_translate_section", interrupted)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await first_agent.execute()
    assert store.read_fragment("sec", 0, {"section": "1"})["trans_content"] == "第一段。"
    assert not store.is_complete("translate")

    resumed = CheckpointStore(str(output), str(source), config)
    assert resumed.restore_maps()
    second_agent = TranslatorAgent(config, project_dir=str(source), output_dir=str(output))
    second_agent.checkpoint = resumed
    monkeypatch.setattr(second_agent, "_new_session", FakeSession)
    requested = []

    async def finish(section, session, **kwargs):
        requested.append(section["section"])
        return {**section, "trans_content": "第二段。"}

    monkeypatch.setattr(second_agent, "_translate_section", finish)
    await second_agent.execute()
    assert requested == ["2"]
    sections = read_json(output / "sections_map.json")
    assert [section["trans_content"] for section in sections] == ["第一段。", "第二段。"]


@_async_test
async def test_invalid_completed_fragment_is_retranslated(project, monkeypatch):
    source, output, config = project
    store = parsed_store(source, output, config)
    store.save_fragment("sec", 0, {"section": "1", "content": "First paragraph.", "trans_content": "{"})
    resumed = CheckpointStore(str(output), str(source), config)
    assert resumed.restore_maps()
    agent = TranslatorAgent(config, project_dir=str(source), output_dir=str(output))
    agent.checkpoint = resumed
    monkeypatch.setattr(agent, "_new_session", FakeSession)
    requested = []

    async def translate(section, session, **kwargs):
        requested.append(section["section"])
        return {**section, "trans_content": "有效译文。"}

    monkeypatch.setattr(agent, "_translate_section", translate)
    await agent.execute()
    assert sorted(requested) == ["1", "2"]


@_async_test
async def test_fully_saved_fragments_can_finish_without_api_key(project, monkeypatch):
    source, output, config = project
    store = parsed_store(source, output, config)
    for index, section in enumerate(write_maps(output, translated=True)):
        store.save_fragment("sec", index, section)
    no_key = copy.deepcopy(config)
    no_key["llm_config"]["api_key"] = ""
    resumed = CheckpointStore(str(output), str(source), no_key)
    assert resumed.restore_maps()
    agent = TranslatorAgent(no_key, project_dir=str(source), output_dir=str(output))
    agent.checkpoint = resumed
    monkeypatch.setattr(agent, "_new_session", mock.Mock(side_effect=AssertionError("session not needed")))
    await agent.execute()
    assert read_json(output / "sections_map.json")[0]["trans_content"] == "第一段。"


@_async_test
async def test_incomplete_work_requires_key_after_checkpoint_is_checked(project, monkeypatch):
    source, output, config = project
    store = parsed_store(source, output, config)
    config["llm_config"]["api_key"] = ""
    resumed = CheckpointStore(str(output), str(source), config)
    assert resumed.valid and resumed.restore_maps()
    agent = TranslatorAgent(config, project_dir=str(source), output_dir=str(output))
    agent.checkpoint = resumed
    monkeypatch.setattr(agent, "_new_session", mock.Mock(side_effect=AssertionError("no network before key check")))
    with pytest.raises(ValueError, match="未完成或无效"):
        await agent.execute()


def _completed_checkpoint(project):
    source, output, config = project
    store = parsed_store(source, output, config)
    write_maps(output, translated=True)
    store.stage_completed("translate", snapshot=True)
    store.stage_completed("validate", snapshot=True)
    pdf = output / "ch_paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    store.complete({"status": "success", "pdf_path": str(pdf), "project_name": "paper", "output_dir": str(output)})
    return store, pdf


def test_coordinator_completed_reuse_is_offline_and_does_not_require_key(project):
    source, output, config = project
    _completed_checkpoint(project)
    no_key = copy.deepcopy(config)
    no_key["llm_config"]["api_key"] = ""
    with mock.patch.object(coordinator_module, "ParserAgent", side_effect=AssertionError("parse must not run")), \
            mock.patch.object(coordinator_module, "TranslatorAgent", side_effect=AssertionError("translation must not run")), \
            mock.patch.object(coordinator_module, "GeneratorAgent", side_effect=AssertionError("compile must not run")), \
            mock.patch.object(coordinator_module, "ValidatorAgent", side_effect=AssertionError("validation must not run")):
        result = CoordinatorAgent(no_key, str(source), str(output.parent)).workflow_latextrans()
    assert result.status == "success" and result.resumed


@pytest.mark.parametrize("reason", ["missing_pdf", "compile_setting"])
def test_missing_completed_pdf_or_compile_setting_recompiles_without_retranslation(project, reason):
    source, output, config = project
    _, pdf = _completed_checkpoint(project)
    if reason == "missing_pdf":
        pdf.unlink()
    else:
        config["compile_repair"] = False
    config["llm_config"]["api_key"] = ""
    generated = output.parent / "generated.pdf"
    generated.write_bytes(b"%PDF-1.4")
    with mock.patch.object(coordinator_module, "ParserAgent", side_effect=AssertionError("parse must not run")), \
            mock.patch.object(coordinator_module, "TranslatorAgent", side_effect=AssertionError("translation must not run")), \
            mock.patch.object(coordinator_module, "GeneratorAgent") as generator:
        generator.return_value.execute.return_value = str(generated)
        result = CoordinatorAgent(config, str(source), str(output.parent)).workflow_latextrans()
    assert result.status == "success" and pdf.is_file()


def test_failed_compile_does_not_mark_checkpoint_completed(project):
    source, output, config = project
    store, pdf = _completed_checkpoint(project)
    pdf.unlink()
    with mock.patch.object(coordinator_module, "GeneratorAgent") as generator:
        generator.return_value.execute.return_value = None
        result = CoordinatorAgent(config, str(source), str(output.parent)).workflow_latextrans()
    assert result.status == "failed_compile"
    checkpoint = CheckpointStore(str(output), str(source), config)
    assert checkpoint.data["status"] == "failed"
    assert checkpoint.completed_result() is None


def test_runtime_completed_arxiv_reuse_does_not_download_or_resolve_dns(project):
    source, output, config = project
    source.rename(source.with_name("2508.18791"))
    source = source.with_name("2508.18791")
    output = output.with_name("ch_2508.18791")
    output.mkdir()
    config.update(tex_sources_dir=str(source.parent), output_dir=str(output.parent), paper_list=[source.name], category={source.name: ["cs.LG"]})
    store = CheckpointStore(str(output), str(source), config)
    pdf = output / "ch_2508.18791.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    store.complete({"status": "success", "pdf_path": str(pdf), "project_name": source.name, "output_dir": str(output)})
    config["category"] = {}
    config["llm_config"]["api_key"] = ""
    with mock.patch.object(runtime, "download_arxiv_source", side_effect=AssertionError("offline")), \
            mock.patch.object(runtime, "fetch_arxiv_categories", side_effect=AssertionError("offline")), \
            mock.patch.object(runtime, "download_arxiv_pdf", side_effect=AssertionError("offline")), \
            mock.patch.object(runtime.dns_cache, "prewarm", side_effect=AssertionError("offline")):
        result = runtime.run_pipeline(config)
    assert len(result["completed_projects"]) == 1


def test_cli_checkpoint_bilingual_and_repair_flags_are_forwarded():
    result = {"completed_projects": [], "failed_projects": []}
    with mock.patch.object(cli_main, "run_translation", return_value=result) as run, \
            mock.patch("sys.argv", ["latextrans", "--project", "p", "--no-resume", "--force", "--bilingual", "--bilingual-layout", "interleaved", "--original-pdf", "source.pdf", "--no-compile-repair", "--compile-repair-attempts", "1"]):
        assert cli_main.main() == 0
    overrides = run.call_args.kwargs["overrides"]
    assert overrides["resume"] is False and overrides["force"] is True
    assert overrides["bilingual"] is True and overrides["bilingual_layout"] == "interleaved"
    assert overrides["original_pdf"] == "source.pdf"
    assert overrides["compile_repair"] is False and overrides["compile_repair_attempts"] == 1
