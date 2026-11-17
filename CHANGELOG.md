# Changelog

This log describes the maintained fork at [elpsykongloo/LaTeXTrans](https://github.com/elpsykongloo/LaTeXTrans).
Upstream releases remain available at [NiuTrans/LaTeXTrans](https://github.com/NiuTrans/LaTeXTrans).

## Unreleased

- Fix XeLaTeX failures when local templates load Type1 fonts after the CJK preamble (e.g. arXiv 2611.07071, `Cannot use XeTeXglyph with ptmr8c`). Restrict microtype protrusion to Unicode font encodings while keeping native-font protrusion available. Resume regenerates PDFs using saved translations.
- Preserve nested command braces when translated title/caption text moves before a final command such as `\texorpdfstring`; legitimate closing delimiters are no longer removed as supposed model echoes, avoiding unnecessary translation retries.
- Classify XeTeX font incompatibilities as environment errors instead of requesting model edits to body text. Skip redundant latexmk reruns for diagnosed engine errors, report the primary error concisely, and fall back to latexmk when direct compilation has not converged.
- Render CJK punctuation in math environments, and add an available supplementary CJK font when Fandol lacks traditional characters. Font fallback probes are isolated from template font path/extension defaults.
- Upgrade known legacy Times/Helvetica/Courier/Inconsolata default families to their bundled OpenType counterparts after local templates load and before fontspec binds math fonts, preventing Unicode author names from losing accented letters and avoiding undefined math font shapes. Explicit native font selections are preserved.
- Parse papers whose text contains literal LLM special tokens such as `<|endoftext|>`; token counting no longer raises tiktoken's disallowed-special-token error.
- Keep chat-template tokens (`<|im_start|>`, `<|endoftext|>`, …) out of translation requests and restore them verbatim, so model servers cannot treat them as control tokens.
- Detect CJK characters that XeLaTeX silently drops (`Missing character` in the log). When a template's own CJK font is a subset (e.g. arXiv 2610.19716 ships `NotoSansSC-Prompts.ttf`), recompile with the bundled Fandol fonts; restore the original source if that fails and warn about any glyphs still missing. Earlier compiled PDFs are recompiled on resume.
- Compile Chinese/Japanese output when the preamble or a local style file loads `microtype` with `expansion=true` (e.g. arXiv 2611.16933). XeTeX cannot expand fonts, so expansion is turned off once the package loads; character protrusion stays enabled.
- Translate template title commands such as `\icmltitle`, `\subtitle`, `\frametitle` or a style's own `\kztitle`, not only `\title`; layout commands (`\maketitle`, `\pretitle`, `\posttitle`) and the bodies of title-macro definitions stay untouched. The translation context falls back to such a title when `\title` is absent.
- Drop line breaks (`\\`, `\\*`, `\\[…]`, `\newline`) that the model moves to the start or end of a translated title or caption argument, unless the source argument has one there. Previously completed PDFs are regenerated once on resume without new translation requests.
- Name the Downloads copy after `\icmltitle` or a template's own title command when `\title` is missing or empty, and ignore commented-out or macro-definition titles.
- Stop piling up `_1`, `_2` copies in Downloads: a regenerated paper overwrites the copy it made last time as long as that copy is unchanged and the title is the same, and a file with identical content is reused. Edited or unrelated files are never overwritten.
- Count section tokens once instead of re-encoding merged text, and fall back to a character estimate when tiktoken or its vocabulary download is unavailable.

## 0.2.0 — 2026-10-07

- Resume parsed stages and validated translation units after an interruption; completed PDFs can be reused offline. Add `--force` and `--no-resume`.
- Export original/translated PDFs side by side or interleaved, with blank pages for unequal page counts. Add matching CLI and GUI controls.
- Translate protected LaTeX chunks with relevant glossary terms, bounded concurrency, retry backoff, and `Retry-After` support. Record per-project request and token usage.
- Add optional document context, safe private configuration overlays, environment overrides, and versioned arXiv inputs.
- Protect code environments, improve main-file selection, and handle starred lists, `captionof`, author `thanks`, and visible text inside macros.
- Improve XeLaTeX compatibility and parse errors even when TeX wraps long paths.
- Repair localized compilation errors with bounded model calls, backups, a repair report, and separate usage records. Repair is enabled by default and can be disabled with `--no-compile-repair`.
- Report failed translation, validation, compilation, and bilingual export through a nonzero CLI exit status.
- Package public defaults and glossaries in wheels; exclude API credentials, downloaded papers, and generated outputs. Add Windows/Linux tests, wheel installation checks, and a real XeLaTeX integration job.
- Support Python 3.10–3.13. Replace old exact dependency pins with compatible ranges and raise HTTP, GUI, and PDF dependencies to include upstream security fixes.

Resume and bilingual PDF work draws on [Saverm666/LaTeXTrans](https://github.com/Saverm666/LaTeXTrans).
Parser and reconstruction fixes draw on [Hydrofoooil/TeXClaudeTrans](https://github.com/Hydrofoooil/TeXClaudeTrans).
The original NiuTrans MIT copyright and license are retained.
