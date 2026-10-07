"""Combine original and translated PDFs without invoking a TeX compiler."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from pypdf import PdfReader, PdfWriter, Transformation


LAYOUTS = ("side_by_side", "interleaved")


def pair_pdf_pages(original_count: int, translated_count: int):
    """Zero-based page pairs, including empty counterparts for unequal counts."""
    return [
        (index if index < original_count else None, index if index < translated_count else None)
        for index in range(max(original_count, translated_count))
    ]


def _display_size(page) -> Tuple[float, float]:
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    return (height, width) if page.rotation % 180 else (width, height)


def find_original_pdf(project_dir: str, translated_pdf: Optional[str] = None) -> Optional[str]:
    project = Path(project_dir)
    if not project.is_dir():
        return None
    excluded = Path(translated_pdf).resolve() if translated_pdf else None
    preferred = project / f"{project.name}.pdf"
    if preferred.is_file() and preferred.resolve() != excluded:
        return str(preferred)
    candidates = sorted(
        path for path in project.glob("*.pdf")
        if path.resolve() != excluded and not path.name.startswith("._")
        and not any(part in path.stem.lower() for part in ("_bilingual", "_sidebyside", "_mono"))
    )
    from src.formats.latex.utils import find_main_tex_file
    main_tex = find_main_tex_file(str(project))
    if main_tex:
        main_pdf = Path(main_tex).with_suffix(".pdf")
        if main_pdf.is_file() and main_pdf.resolve() != excluded:
            return str(main_pdf)
    # Ambiguous root PDFs may be figure assets; let the caller request an explicit path.
    return str(candidates[0]) if len(candidates) == 1 else None


def ensure_original_pdf(
    project_dir: str,
    translated_pdf: Optional[str] = None,
    original_pdf: Optional[str] = None,
    download_original: Optional[Callable[[], Optional[str]]] = None,
    translated_project_dir: Optional[str] = None,
) -> Optional[str]:
    if original_pdf:
        explicit = Path(original_pdf)
        if not explicit.is_file():
            raise FileNotFoundError(f"指定的原文 PDF 不存在：{explicit}")
        if translated_pdf and explicit.resolve() == Path(translated_pdf).resolve():
            raise ValueError("原文 PDF 与译文 PDF 必须是不同文件。")
        return str(explicit)
    for directory in (project_dir, translated_project_dir):
        if directory:
            found = find_original_pdf(directory, translated_pdf)
            if found:
                return found
    if download_original is not None:
        downloaded = download_original()
        if downloaded and Path(downloaded).is_file():
            destination = Path(project_dir) / f"{Path(project_dir).name}.pdf"
            if Path(downloaded).resolve() != destination.resolve():
                shutil.copy2(downloaded, destination)
            return str(destination)
    return None


def _write_pdf(writer: PdfWriter, output_pdf: str) -> str:
    destination = Path(output_pdf)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, suffix=".pdf.tmp", delete=False) as handle:
            temporary = Path(handle.name)
            writer.write(handle)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return str(destination)


def create_bilingual_pdf(original_pdf: str, translated_pdf: str, output_pdf: str, layout: str = "side_by_side") -> str:
    if layout not in LAYOUTS:
        raise ValueError(f"不支持的双语布局：{layout}；可用值：{', '.join(LAYOUTS)}")
    if Path(output_pdf).resolve() in {Path(original_pdf).resolve(), Path(translated_pdf).resolve()}:
        raise ValueError("双语 PDF 的输出路径不能覆盖原文或译文。")
    original, translated = PdfReader(original_pdf), PdfReader(translated_pdf)
    if not original.pages or not translated.pages:
        raise ValueError("原文与译文 PDF 均须至少包含一页。")
    writer = PdfWriter()
    rotation_writer = PdfWriter()
    for left_index, right_index in pair_pdf_pages(len(original.pages), len(translated.pages)):
        left = original.pages[left_index] if left_index is not None else None
        right = translated.pages[right_index] if right_index is not None else None
        if layout == "interleaved":
            for page, counterpart in ((left, right), (right, left)):
                if page is None:
                    width, height = _display_size(counterpart)
                    writer.add_blank_page(width=width, height=height)
                else:
                    writer.add_page(page)
            continue
        if left is not None and left.rotation:
            left = rotation_writer.add_page(left)
            left.transfer_rotation_to_content()
        if right is not None and right.rotation:
            right = rotation_writer.add_page(right)
            right.transfer_rotation_to_content()
        left_wh = _display_size(left) if left is not None else _display_size(right)
        right_wh = _display_size(right) if right is not None else left_wh
        height = max(left_wh[1], right_wh[1])
        if min(*left_wh, *right_wh) <= 0:
            raise ValueError("PDF 页面尺寸必须为正数。")
        left_scale, right_scale = height / left_wh[1], height / right_wh[1]
        left_width = left_wh[0] * left_scale
        spread = writer.add_blank_page(width=left_width + right_wh[0] * right_scale, height=height)
        for page, scale, offset in ((left, left_scale, 0), (right, right_scale, left_width)):
            if page is not None:
                transformation = (
                    Transformation().translate(-float(page.mediabox.left), -float(page.mediabox.bottom))
                    .scale(scale).translate(offset, 0)
                )
                spread.merge_transformed_page(page, transformation, expand=False, over=True)
    return _write_pdf(writer, output_pdf)


def compile_side_by_side_pdf(original_pdf: str, translated_pdf: str, output_pdf: str) -> str:
    return create_bilingual_pdf(original_pdf, translated_pdf, output_pdf, "side_by_side")


def compile_bilingual_pdf(original_pdf: str, translated_pdf: str, output_pdf: str) -> str:
    return create_bilingual_pdf(original_pdf, translated_pdf, output_pdf, "interleaved")
