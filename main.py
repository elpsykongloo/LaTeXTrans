import argparse
import sys
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

from src.runtime import format_run_summary, run_translation, split_cli_items


def main():
    """
    Main function to run the LaTeXTrans application.
    Allows overriding paper_list from command-line arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="config/default.toml", help="Path to the config TOML file.")
    parser.add_argument("--model", type=str, default="", help="Model for translating.")
    parser.add_argument("--url", type=str, default="", help="Model url.")
    parser.add_argument(
        "--key",
        type=str,
        default="",
        help="Model key. Prefer config/local.toml or the LATEXTRANS_API_KEY env var.",
    )
    parser.add_argument("--concurrency", type=int, default=None, help="Maximum concurrent LLM requests.")
    parser.add_argument(
        "--thinking",
        choices=["enabled", "disabled"],
        default="",
        help="Thinking mode for OpenAI-compatible payloads.",
    )
    parser.add_argument("--arxiv", nargs="+", default=[], help="arXiv ID(s), comma-separated.")
    parser.add_argument(
        "--project",
        nargs="+",
        default=[],
        help="Local project path(s) or archive path(s), comma-separated.",
    )
    parser.add_argument("--output", type=str, default="", help="output directory.")
    parser.add_argument("--source", type=str, default="", help="tex source directory.")
    parser.add_argument(
        "--all-existing",
        action="store_true",
        help="Process all existing projects under tex source directory when no --arxiv/--project is provided.",
    )
    parser.add_argument(
        "--no-downloads",
        action="store_true",
        help="Do not copy the translated PDF to the Downloads directory.",
    )
    parser.add_argument(
        "--downloads-dir",
        type=str,
        default="",
        help="Directory for the titled PDF copy (default: ~/Downloads).",
    )
    parser.add_argument("--no-resume", action="store_true", help="Translate afresh instead of resuming saved stages and fragments.")
    parser.add_argument("--force", action="store_true", help="Force a fresh parse and translation, ignoring completed checkpoints.")
    bilingual_group = parser.add_mutually_exclusive_group()
    bilingual_group.add_argument("--bilingual", action="store_true", default=None, help="Also generate an original/translation bilingual PDF.")
    bilingual_group.add_argument("--no-bilingual", dest="bilingual", action="store_false", help="Disable bilingual PDF output.")
    parser.add_argument("--bilingual-layout", choices=["side_by_side", "interleaved"], default="", help="Bilingual page layout.")
    parser.add_argument("--original-pdf", default="", help="Original PDF path for bilingual output (otherwise detected or downloaded).")
    parser.add_argument("--no-compile-repair", action="store_true", help="Disable automatic repair after TeX compilation errors.")
    parser.add_argument("--compile-repair-attempts", type=int, default=None, help="Maximum compilation repair attempts.")

    args = parser.parse_args()
    if args.compile_repair_attempts is not None and args.compile_repair_attempts < 0:
        parser.error("--compile-repair-attempts must be zero or greater.")
    # 与 GUI 共用 src.runtime 的流水线：下载完成的论文立即进入解析/翻译/编译。
    overrides = {
        "url": args.url,
        "model": args.model,
        "key": args.key,
        "concurrency": args.concurrency,
        "thinking": args.thinking,
        "source": args.source,
        "output": args.output,
        "paper_list": split_cli_items(args.arxiv),
        "copy_to_downloads": False if args.no_downloads else None,
        "downloads_dir": args.downloads_dir,
        "resume": False if args.no_resume else None,
        "force": True if args.force else None,
        "bilingual": args.bilingual,
        "bilingual_layout": args.bilingual_layout,
        "original_pdf": args.original_pdf,
        "compile_repair": False if args.no_compile_repair else None,
        "compile_repair_attempts": args.compile_repair_attempts,
    }
    result = run_translation(
        config_path=args.config,
        overrides=overrides,
        project_items=split_cli_items(args.project),
        all_existing=args.all_existing,
    )
    print(format_run_summary(result))
    # 任意项目失败（校验/生成/编译）时以非零状态码退出，便于脚本判断。
    return 1 if result.get("failed_projects") else 0


if __name__ == "__main__":
    sys.exit(main())
