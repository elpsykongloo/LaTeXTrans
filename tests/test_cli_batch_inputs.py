from src.runtime import split_cli_items
from src.formats.latex.utils import extract_arxiv_ids


def test_multiple_cli_ids_are_not_merged_or_silently_dropped():
    values = ["1706.03762v7", "https://arxiv.org/abs/1512.03385v1,2010.11929v2", "quant-ph/9508027v2"]
    assert extract_arxiv_ids(split_cli_items(values)) == ["1706.03762v7", "1512.03385v1", "2010.11929v2", "quant-ph/9508027v2"]


def test_separately_quoted_paths_keep_spaces_and_argument_boundaries():
    paths = [r"D:\My Papers\first", r"D:\My Papers\second"]
    assert split_cli_items(paths) == paths
    assert split_cli_items([",", ""]) == []
