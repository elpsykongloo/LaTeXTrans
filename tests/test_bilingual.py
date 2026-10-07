from pathlib import Path
from unittest import mock

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, RectangleObject

import src.runtime as runtime
from src.formats.latex.bilingual import create_bilingual_pdf, ensure_original_pdf, find_original_pdf
from src.gui.streamlit_app import _collect_result_pdfs
from src.utils.checkpoint import CheckpointStore


def make_pdf(path, sizes, rotation=0, color="1 0 0"):
    writer = PdfWriter()
    for width, height in sizes:
        page = writer.add_blank_page(width=width, height=height)
        stream = DecodedStreamObject()
        stream.set_data(f"q {color} rg 10 10 20 20 re f Q".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        if rotation:
            page.rotate(rotation)
    writer.write(str(path))
    return str(path)


@pytest.mark.parametrize("layout,expected_pages", [("side_by_side", 2), ("interleaved", 4)])
@pytest.mark.parametrize("longer", ["original", "translated"])
def test_unequal_counts_keep_paired_slots(tmp_path, layout, expected_pages, longer):
    original = make_pdf(tmp_path / "original.pdf", [(100, 200)] * (2 if longer == "original" else 1))
    translated = make_pdf(tmp_path / "translated.pdf", [(200, 400)] * (2 if longer == "translated" else 1), color="0 0 1")
    output = create_bilingual_pdf(original, translated, str(tmp_path / "bilingual.pdf"), layout)
    pages = PdfReader(output).pages
    assert len(pages) == expected_pages
    if layout == "side_by_side":
        assert float(pages[0].mediabox.width) == 400
        assert float(pages[0].mediabox.height) == 400
        assert float(pages[1].mediabox.width) == (200 if longer == "original" else 400)
    else:
        blank_index = 3 if longer == "original" else 2
        assert pages[blank_index].get_contents() is None
        assert pages[1].mediabox.width == 200


def test_side_by_side_normalizes_rotation_and_nonzero_page_origin(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", [(100, 200)], rotation=90)
    translated = make_pdf(tmp_path / "translated.pdf", [(200, 400)], color="0 0 1")
    output = create_bilingual_pdf(original, translated, str(tmp_path / "rotated.pdf"))
    page = PdfReader(output).pages[0]
    assert float(page.mediabox.width) == 1000
    assert float(page.mediabox.height) == 400
    assert page.rotation == 0

    reader = PdfReader(original)
    reader.pages[0].rotation = 0
    reader.pages[0].mediabox = RectangleObject((10, 20, 110, 220))
    reader.pages[0].cropbox = RectangleObject((10, 20, 110, 220))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    shifted = tmp_path / "shifted.pdf"
    writer.write(str(shifted))
    output = create_bilingual_pdf(str(shifted), translated, str(tmp_path / "offset.pdf"))
    assert PdfReader(output).pages[0].mediabox.width == 400


def test_layout_errors_are_explicit_and_sources_are_preserved(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", [(100, 200)])
    translated = make_pdf(tmp_path / "translated.pdf", [(100, 200)])
    with pytest.raises(ValueError, match="不支持"):
        create_bilingual_pdf(original, translated, str(tmp_path / "output.pdf"), "unknown")
    with pytest.raises(ValueError, match="不能覆盖"):
        create_bilingual_pdf(original, translated, original)
    assert len(PdfReader(original).pages) == 1


def test_original_pdf_selection_prefers_existing_and_explicit_paths(tmp_path):
    project = tmp_path / "paper"
    project.mkdir()
    original = make_pdf(project / "paper.pdf", [(100, 200)])
    make_pdf(project / "figure.pdf", [(10, 20)])
    download = mock.Mock(side_effect=AssertionError("already available"))
    assert find_original_pdf(str(project)) == original
    assert ensure_original_pdf(str(project), download_original=download) == original
    explicit = make_pdf(tmp_path / "provided.pdf", [(300, 400)])
    assert ensure_original_pdf(str(project), original_pdf=explicit) == explicit
    with pytest.raises(FileNotFoundError, match="原文 PDF 不存在"):
        ensure_original_pdf(str(project), original_pdf=str(tmp_path / "missing.pdf"))


def test_original_download_callback_is_reused(tmp_path):
    project = tmp_path / "paper"
    project.mkdir()
    downloaded = make_pdf(tmp_path / "downloaded.pdf", [(100, 200)])
    download = mock.Mock(return_value=downloaded)
    original = ensure_original_pdf(str(project), download_original=download)
    assert original == str(project / "paper.pdf")
    download.assert_called_once()
    assert len(PdfReader(original).pages) == 1


@pytest.fixture
def runtime_project(tmp_path):
    project = tmp_path / "paper"
    project.mkdir()
    (project / "main.tex").write_text(r"\documentclass{article}\begin{document}x\end{document}", encoding="utf-8")
    output = tmp_path / "out"
    project_output = output / "ch_paper"
    project_output.mkdir(parents=True)
    translated = make_pdf(project_output / "ch_paper.pdf", [(100, 200)])
    config = {"source_language": "en", "target_language": "ch", "bilingual": True, "llm_config": {"model": "test-model"}}
    outcome = {"status": "success", "pdf_path": translated, "project_name": "paper", "output_dir": str(project_output)}
    CheckpointStore(str(project_output), str(project), config).complete(outcome)
    return project, output, config, outcome


def test_runtime_exposes_bilingual_path_and_reuses_valid_existing_pdf(runtime_project):
    project, output, config, outcome = runtime_project
    make_pdf(project / "paper.pdf", [(100, 200)])
    first = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    assert first["status"] == "success" and Path(first["bilingual_pdf_path"]).is_file()
    with mock.patch.object(runtime, "create_bilingual_pdf", side_effect=AssertionError("should reuse")):
        second = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    assert second["status"] == "success" and second["bilingual_pdf_path"] == first["bilingual_pdf_path"]
    config["bilingual_layout"] = "interleaved"
    third = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    assert len(PdfReader(third["bilingual_pdf_path"]).pages) == 2


@pytest.mark.parametrize("fresh", [{"resume": False}, {"force": True}])
def test_fresh_run_bilingual_output_preserves_completed_translation_checkpoint(runtime_project, fresh):
    project, output, config, outcome = runtime_project
    make_pdf(project / "paper.pdf", [(100, 200)])
    config.update(fresh)
    result = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    assert result["status"] == "success"
    resumed = CheckpointStore(outcome["output_dir"], str(project), {**config, "resume": True, "force": False})
    assert resumed.completed_result()["pdf_path"] == outcome["pdf_path"]


def test_runtime_bilingual_failure_reports_failure_and_preserves_translated_pdf(runtime_project):
    project, output, config, outcome = runtime_project
    failed = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    assert failed["status"] == "failed_bilingual"
    assert "原文 PDF" in failed["message"] and Path(failed["pdf_path"]).is_file()
    checkpoint = CheckpointStore(outcome["output_dir"], str(project), config)
    assert checkpoint.data["status"] == "failed"
    assert checkpoint.data["bilingual"]["status"] == "failed"
    # The completed translation can still be reused when only bilingual output failed.
    assert checkpoint.completed_result()["pdf_path"] == outcome["pdf_path"]
    result = {"output_dir": str(output), "config": config, "completed_projects": [], "failed_projects": [{**failed, "project_dir": str(project)}]}
    assert outcome["pdf_path"] in _collect_result_pdfs(result)


def test_runtime_invalid_original_is_not_reported_as_success(runtime_project):
    project, output, config, outcome = runtime_project
    (project / "paper.pdf").write_text("not a pdf", encoding="utf-8")
    failed = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    assert failed["status"] == "failed_bilingual"
    assert failed["bilingual_pdf_path"] is None


def test_gui_collects_bilingual_and_explicit_original_pdfs(runtime_project):
    project, output, config, outcome = runtime_project
    explicit = make_pdf(project.parent / "original.pdf", [(100, 200)])
    config["original_pdf"] = explicit
    combined = runtime._add_bilingual_result(outcome, str(project), str(output), config)
    result = {"output_dir": str(output), "config": config, "completed_projects": [{**combined, "project_dir": str(project)}], "failed_projects": []}
    selected = _collect_result_pdfs(result)
    assert outcome["pdf_path"] in selected and combined["bilingual_pdf_path"] in selected and explicit in selected
