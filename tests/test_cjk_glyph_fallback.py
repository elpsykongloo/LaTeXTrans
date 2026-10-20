from pathlib import Path

from src.formats.latex.compile import LaTexCompiler, find_missing_cjk_glyphs
from src.formats.latex.utils import CJK_FONT_FALLBACK_MARKER, add_cjk_font_fallback


# 摘自 arXiv 2610.19716 译文编译日志：作者自带的子集字体缺少大部分汉字。
_SUBSET_FONT_LOG = (
    "Missing character: There is no 种 (U+79CD) in font [fonts/NotoSansSC-Prompts.ttf]/OT:script=hani;language=dflt;!\n"
    "Missing character: There is no 用 (U+7528) in font [fonts/NotoSansSC-Prompts.ttf]/OT:script=hani;language=dflt;!\n"
    "Missing character: There is no 用 (U+7528) in font [fonts/NotoSansSC-Prompts.ttf]/OT:script=hani;language=dflt;!\n"
    "Missing character: There is no ≈ (U+2248) in font [lmroman10-regular]:mapping=tex-text;!\n"
    "Missing character: There is no 。 in font [fonts/NotoSansSC-Prompts.ttf]!\n"
)

_SOURCE = (
    "\\documentclass{mosi}\n"
    "\\setCJKmainfont[Extension=.ttf,Path=fonts/]{NotoSansSC-Prompts.ttf}\n"
    "\\begin{document}\n"
    "中文正文\n"
    "\\end{document}\n"
)


def test_missing_cjk_glyphs_are_counted_and_latin_symbols_ignored():
    assert find_missing_cjk_glyphs(_SUBSET_FONT_LOG) == {"种": 1, "用": 2, "。": 1}
    assert find_missing_cjk_glyphs("") == {}


def test_font_fallback_is_inserted_before_document_once():
    patched = add_cjk_font_fallback(_SOURCE)

    assert patched.index(CJK_FONT_FALLBACK_MARKER) < patched.index("\\begin{document}")
    assert patched.index("NotoSansSC-Prompts") < patched.index(CJK_FONT_FALLBACK_MARKER)
    # 原稿正文是黑体，回退字体保持无衬线风格。
    assert "\\setCJKmainfont[Extension=.otf,UprightFont=*-Regular,BoldFont=*-Bold]{FandolHei}" in patched
    assert "\\@ifpackageloaded{xeCJK}" in patched
    assert add_cjk_font_fallback(patched) == patched


def test_serif_documents_fall_back_to_song_and_japanese_to_japanese_fonts():
    serif = add_cjk_font_fallback("\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n")
    assert "{FandolSong}" in serif
    japanese = add_cjk_font_fallback(_SOURCE, "ja")
    assert "IPAexMincho" in japanese and "Fandol" not in japanese


def test_document_without_body_is_unchanged():
    assert add_cjk_font_fallback("\\documentclass{article}\n") == "\\documentclass{article}\n"


def _compiler_with_logs(tmp_path, logs, results):
    tex = tmp_path / "paper.tex"
    tex.write_bytes(_SOURCE.replace("\n", "\r\n").encode("utf-8"))
    out_dir = tmp_path / "build_xelatex"
    out_dir.mkdir()
    compiler = LaTexCompiler(str(tmp_path))
    calls = []

    def fake_compile(tex_file, out, engine):
        calls.append(Path(tex_file).read_text(encoding="utf-8"))
        (out_dir / "paper.log").write_text(logs.pop(0), encoding="utf-8")
        return results.pop(0)

    compiler._compile_with_engine = fake_compile
    return compiler, tex, str(out_dir), calls


def test_missing_glyphs_trigger_full_font_recompile(tmp_path):
    compiler, tex, out_dir, calls = _compiler_with_logs(tmp_path, [""], ["fixed.pdf"])
    (Path(out_dir) / "paper.log").write_text(_SUBSET_FONT_LOG, encoding="utf-8")

    pdf = compiler._ensure_cjk_glyphs(str(tex), out_dir, "xelatex", "broken.pdf")

    assert pdf == "fixed.pdf"
    assert compiler.missing_glyphs == {}
    assert CJK_FONT_FALLBACK_MARKER in calls[0]


def test_failed_fallback_restores_original_source_byte_for_byte(tmp_path):
    compiler, tex, out_dir, calls = _compiler_with_logs(
        tmp_path, ["", _SUBSET_FONT_LOG], [None, "restored.pdf"]
    )
    original = tex.read_bytes()
    (Path(out_dir) / "paper.log").write_text(_SUBSET_FONT_LOG, encoding="utf-8")

    pdf = compiler._ensure_cjk_glyphs(str(tex), out_dir, "xelatex", "broken.pdf")

    assert pdf == "restored.pdf"
    assert tex.read_bytes() == original
    assert compiler.missing_glyphs == {"种": 1, "用": 2, "。": 1}


def test_complete_fonts_skip_recompile(tmp_path):
    compiler, tex, out_dir, calls = _compiler_with_logs(tmp_path, [], [])
    (Path(out_dir) / "paper.log").write_text("Output written on paper.pdf\n", encoding="utf-8")

    assert compiler._ensure_cjk_glyphs(str(tex), out_dir, "xelatex", "ok.pdf") == "ok.pdf"
    assert calls == []
