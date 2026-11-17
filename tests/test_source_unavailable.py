from pathlib import Path
from unittest import mock

import pytest

from src import runtime
from src.formats.latex import utils


def test_pdf_only_source_is_classified_without_retry_or_archive_error(tmp_path):
    def download(url, destination, *args, **kwargs):
        Path(destination).write_bytes(b"%PDF-1.5\nPDF-only source response")
    with mock.patch.object(utils, "download_file_ranged", side_effect=download) as request, \
         mock.patch.object(utils, "get_tex_url") as fallback:
        with pytest.raises(utils.ArxivSourceUnavailableError, match="没有可用的 TeX 源码"):
            utils.download_arxiv_source("1602.03837v1", str(tmp_path))
    assert request.call_count == 1
    fallback.assert_not_called()
    assert not list(tmp_path.glob("*.download"))


def test_pdf_only_project_has_actionable_status_and_no_redundant_pdf_request(tmp_path):
    config = {"paper_list": ["1602.03837v1"], "tex_sources_dir": str(tmp_path / "src"),
              "output_dir": str(tmp_path / "out"), "user_term": "default", "llm_config": {"base_url": ""}}
    with mock.patch.object(runtime, "download_arxiv_source", side_effect=utils.ArxivSourceUnavailableError("仅提供 PDF，没有可用的 TeX 源码")), \
         mock.patch.object(runtime, "download_arxiv_pdf") as pdf, mock.patch.object(runtime, "CoordinatorAgent") as translate, \
         mock.patch.object(runtime, "dns_cache"):
        result = runtime.run_pipeline(config)
    assert result["failed_projects"][0]["status"] == "source_unavailable"
    assert "TeX" in result["failed_projects"][0]["error"]
    assert result["completed_projects"] == []
    pdf.assert_not_called()
    translate.assert_not_called()
