# Changelog

This log describes the maintained fork at [elpsykongloo/LaTeXTrans](https://github.com/elpsykongloo/LaTeXTrans).
Upstream releases remain available at [NiuTrans/LaTeXTrans](https://github.com/NiuTrans/LaTeXTrans).

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
