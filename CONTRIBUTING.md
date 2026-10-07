# Contributing

This repository maintains an enhanced fork of [NiuTrans/LaTeXTrans](https://github.com/NiuTrans/LaTeXTrans).
Please open issues and feature pull requests here. Small fixes useful to upstream should also be proposed as independent patches against upstream's latest `main`.

## Development environment

Use Python 3.10–3.13 and a virtual environment:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pip check
python -m pytest -q
```

Tests use temporary projects and mocked model responses. No API key or downloaded paper is required. Tests that need XeLaTeX and latexmk skip automatically when the tools are absent; the Linux integration job installs both.

## Reviewable changes

Keep each pull request focused on one behavior. Explain the failing case, the resulting behavior, and the verification performed. Add regression tests for parser, pipeline, or compilation changes. Update both READMEs when an interface or default changes. Retain attribution when adapting another fork's work.

Keep API credentials in ignored `config/local.toml` or environment variables. Downloaded sources, generated PDFs, test environments, and tool caches are excluded from commits. Inspect the staged file list before publishing.

## Release checks

```bash
python -m build
# Linux/macOS:
LATEXTRANS_DIST_DIR=dist python -m pytest tests/test_packaging.py -q
# Windows PowerShell:
# $env:LATEXTRANS_DIST_DIR = "dist"
# python -m pytest tests/test_packaging.py -q
```

Build into an empty output directory to avoid inspecting old versions. The package tests inspect archive member names, package metadata, public defaults, and CLI entry points. Install the built wheel in a separate environment and run `latextrans --help` and `load_layered_config()` from outside the checkout. Before tagging a release, require green CI, review the final diff, and update `pyproject.toml`, `src/__init__.py`, `config/default.toml`, and the changelog together.

For an upstream contribution, start a separate branch from `upstream/main`, port only the intended fix and its tests, then run the applicable checks. The enhanced fork's release must not be a prerequisite for the upstream patch.
