import os
import sys
import tarfile
import threading
import warnings
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from importlib import resources
from urllib.parse import urlparse
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import toml

warnings.filterwarnings("ignore", category=SyntaxWarning)

from src.agents.coordinator_agent import CoordinatorAgent
from src.utils import dns_cache
from src.utils.paths import extract_tar, extract_zip
from src.utils.checkpoint import CheckpointStore, file_metadata
from src.formats.latex.bilingual import create_bilingual_pdf, ensure_original_pdf
from src.formats.latex.utils import (
    batch_download_arxiv_tex,
    download_arxiv_pdf,
    download_arxiv_source,
    extract_arxiv_ids,
    extract_compressed_files,
    fetch_arxiv_categories,
    get_arxiv_category,
    get_profect_dirs,
    place_arxiv_pdf,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ProjectEventCallback = Callable[[Dict[str, Any]], None]


def workspace_root() -> Path:
    """Use the checkout for source runs and the user's directory for installed runs."""
    return PROJECT_ROOT if (PROJECT_ROOT / "setup.py").is_file() else Path.cwd()


def resolve_config_path(path_value: str) -> Path:
    p = Path(path_value)
    if p.is_absolute() or p.exists():
        return p
    candidate = workspace_root() / p
    if candidate.is_file() or p.as_posix() != "config/default.toml":
        return candidate.resolve()
    return Path(str(resources.files("config").joinpath("default.toml")))


def resolve_path(path_value: str) -> Path:
    p = Path(path_value)
    if p.is_absolute():
        return p
    return (workspace_root() / p).resolve()


def is_local_archive(path: str) -> bool:
    p = Path(path)
    if not path or not p.is_file():
        return False
    lower = p.name.lower()
    return lower.endswith((".zip", ".tar", ".tar.gz", ".tgz"))


def archive_project_dir(archive_path: str, projects_dir: str) -> str:
    name = os.path.basename(archive_path)
    lower = name.lower()
    if lower.endswith(".tar.gz"):
        stem = name[:-7]
    elif lower.endswith(".tgz"):
        stem = name[:-4]
    elif lower.endswith(".tar"):
        stem = name[:-4]
    elif lower.endswith(".zip"):
        stem = name[:-4]
    else:
        stem = os.path.splitext(name)[0]
    return os.path.join(projects_dir, stem)


def ensure_unique_dir(base_dir: Path) -> Path:
    if not base_dir.exists():
        return base_dir
    index = 1
    while True:
        candidate = base_dir.parent / f"{base_dir.name}_{index}"
        if not candidate.exists():
            return candidate
        index += 1


def is_within_dir(base_dir: Path, target_path: Path) -> bool:
    try:
        target_path.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def safe_extract_zip(zip_ref: zipfile.ZipFile, target_dir: Path) -> None:
    extract_zip(zip_ref, target_dir)


def safe_extract_tar(tar_ref: tarfile.TarFile, target_dir: Path) -> None:
    extract_tar(tar_ref, target_dir)


def extract_local_archive(archive_path: str, projects_dir: str) -> str:
    target_dir = ensure_unique_dir(Path(archive_project_dir(archive_path, projects_dir)))
    target_dir.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            safe_extract_zip(zip_ref, target_dir)
        return str(target_dir)

    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as tar_ref:
            safe_extract_tar(tar_ref, target_dir)
        return str(target_dir)

    raise ValueError(f"Unsupported archive format: {archive_path}")


def split_cli_items(values: Sequence[str]) -> List[str]:
    raw = " ".join(values)
    return [item.strip() for item in raw.split(",") if item.strip()]


def split_multivalue_text(value: str) -> List[str]:
    if not value:
        return []
    normalized = value.replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


LOCAL_CONFIG_NAME = "local.toml"
# 环境变量 → llm_config 键；优先级高于配置文件，低于命令行参数。
ENV_LLM_OVERRIDES = {
    "LATEXTRANS_API_KEY": "api_key",
    "LATEXTRANS_BASE_URL": "base_url",
    "LATEXTRANS_MODEL": "model",
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 递归合并进 base（就地修改并返回 base）。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def local_config_path(config_path: str = "config/default.toml") -> Path:
    """与主配置同目录的未跟踪私有配置（默认 config/local.toml）。"""
    if Path(config_path).as_posix() == "config/default.toml" and workspace_root() != PROJECT_ROOT:
        return Path.cwd() / "config" / LOCAL_CONFIG_NAME
    return resolve_config_path(config_path).with_name(LOCAL_CONFIG_NAME)


def load_layered_config(
    config_path: str = "config/default.toml",
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """分层加载配置：主配置 → 同目录 local.toml → 环境变量。

    命令行/GUI 参数由 ``load_runtime_config`` 在此基础上再覆盖。
    """
    path = resolve_config_path(config_path)
    config = toml.load(path)

    local_path = local_config_path(config_path)
    if local_path.is_file() and local_path.resolve() != path.resolve():
        try:
            deep_merge(config, toml.load(local_path))
        except (OSError, toml.TomlDecodeError) as e:
            print(f"[WARNING] 无法读取本地配置 {local_path}：{e}")

    env = os.environ if env is None else env
    llm_config = config.setdefault("llm_config", {})
    for env_name, key in ENV_LLM_OVERRIDES.items():
        value = (env.get(env_name) or "").strip()
        if value:
            llm_config[key] = value
    return config


def as_bool(value: Any, default: bool = False) -> bool:
    """兼容 TOML 布尔值与 "True"/"False" 等字符串写法。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def load_runtime_config(
    config_path: str = "config/default.toml",
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = load_layered_config(config_path)
    overrides = overrides or {}

    if overrides.get("copy_to_downloads") is not None:
        config["copy_to_downloads"] = as_bool(overrides["copy_to_downloads"], True)
    if overrides.get("downloads_dir"):
        config["downloads_dir"] = overrides["downloads_dir"]

    llm_config = config.setdefault("llm_config", {})
    if overrides.get("url"):
        llm_config["base_url"] = overrides["url"]
    if overrides.get("model"):
        llm_config["model"] = overrides["model"]
    if overrides.get("key"):
        llm_config["api_key"] = overrides["key"]
    if overrides.get("concurrency") is not None:
        llm_config["concurrency_limit"] = overrides["concurrency"]
    if overrides.get("thinking"):
        llm_config["thinking_type"] = overrides["thinking"]

    for key in ("source", "output", "source_language", "target_language", "user_term"):
        if overrides.get(key):
            mapped_key = {
                "source": "tex_sources_dir",
                "output": "output_dir",
                "source_language": "source_language",
                "target_language": "target_language",
                "user_term": "user_term",
            }[key]
            config[mapped_key] = overrides[key]

    if overrides.get("mode") is not None:
        config["mode"] = overrides["mode"]
    if overrides.get("update_term") is not None:
        config["update_term"] = overrides["update_term"]
    for key in ("resume", "force", "bilingual", "compile_repair"):
        if overrides.get(key) is not None:
            config[key] = as_bool(overrides[key])
    for key in ("bilingual_layout", "original_pdf"):
        if overrides.get(key):
            config[key] = overrides[key]
    if overrides.get("compile_repair_attempts") is not None:
        config["compile_repair_attempts"] = overrides["compile_repair_attempts"]

    extra_papers = overrides.get("paper_list") or []
    if extra_papers:
        config.setdefault("paper_list", [])
        config["paper_list"].extend(extra_papers)

    return config


def normalize_project_result(result: Any) -> Dict[str, Any]:
    """把 CoordinatorAgent 的返回值统一成 dict；旧实现返回 None 时按成功处理。"""
    if result is None:
        return {"status": "success", "message": "", "pdf_path": None}
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    if not isinstance(result, dict):
        return {"status": "success", "message": str(result), "pdf_path": None}
    normalized = dict(result)
    normalized.setdefault("status", "success")
    normalized.setdefault("message", "")
    normalized.setdefault("pdf_path", None)
    return normalized


def format_run_summary(result: Dict[str, Any]) -> str:
    """生成 CLI 结束时的成功/失败汇总。"""
    completed = result.get("completed_projects", [])
    failed = result.get("failed_projects", [])
    lines = [f"===== 翻译汇总：成功 {len(completed)} 篇，失败 {len(failed)} 篇 ====="]
    for item in completed:
        pdf = item.get("pdf_path") or "-"
        lines.append(f"[OK]   {item['project_name']} -> {pdf}")
        if item.get("bilingual_pdf_path"):
            lines.append(f"       双语 PDF -> {item['bilingual_pdf_path']}")
    for item in failed:
        status = item.get("status", "error")
        message = (item.get("error") or "").strip().splitlines()
        lines.append(f"[FAIL] {item['project_name']} ({status}): {message[0] if message else ''}")
    return "\n".join(lines)


def _add_bilingual_result(
    outcome: Dict[str, Any], project_dir: str, output_dir: str, config: Dict[str, Any], allow_download=True,
) -> Dict[str, Any]:
    if outcome.get("status") != "success" or not as_bool(config.get("bilingual", False)):
        return outcome
    base_name = os.path.basename(project_dir)
    target_language = config.get("target_language", "ch")
    project_output = Path(output_dir) / f"{target_language}_{base_name}"
    # Fresh-run flags apply to translation. Reopen the checkpoint just produced
    # by that translation so bilingual output does not discard its saved stages.
    checkpoint = CheckpointStore(str(project_output), project_dir, {**config, "resume": True, "force": False})
    layout = str(config.get("bilingual_layout", "side_by_side"))
    translated_pdf = outcome.get("pdf_path")

    def download_original():
        arxiv_ids = extract_arxiv_ids([base_name])
        if not allow_download or not arxiv_ids:
            return None
        downloaded = download_arxiv_pdf(arxiv_ids[0], str(Path(project_dir).parent))
        place_arxiv_pdf(downloaded, arxiv_ids[0], [project_dir, str(project_output / base_name)])
        candidate = Path(project_dir) / f"{arxiv_ids[0]}.pdf"
        return str(candidate) if candidate.is_file() else None

    try:
        explicit_original = str(config.get("original_pdf") or "").strip()
        original = ensure_original_pdf(
            project_dir=project_dir, translated_pdf=translated_pdf,
            original_pdf=str(resolve_path(explicit_original)) if explicit_original else None,
            translated_project_dir=str(project_output / base_name), download_original=download_original,
        )
        if not original:
            raise FileNotFoundError("没有找到原文 PDF；请将原文 PDF 放入项目目录，或使用 --original-pdf 指定路径。")
        bilingual_pdf = project_output / f"{target_language}_{base_name}_bilingual_{layout}.pdf"
        metadata = {"layout": layout, "original": file_metadata(original), "translated": file_metadata(translated_pdf)}
        cached = checkpoint.data.get("bilingual") or {}
        if (
            checkpoint.valid and cached.get("status") == "completed" and cached.get("metadata") == metadata
            and cached.get("pdf_path") == str(bilingual_pdf) and bilingual_pdf.is_file() and bilingual_pdf.stat().st_size > 0
        ):
            print(f"[RESUME] 复用双语 PDF：{bilingual_pdf}")
        else:
            checkpoint.begin("bilingual")
            create_bilingual_pdf(original, translated_pdf, str(bilingual_pdf), layout=layout)
            checkpoint.data["bilingual"] = {"status": "completed", "metadata": metadata, "pdf_path": str(bilingual_pdf)}
            checkpoint.stage_completed("bilingual")
            print(f"[SUCCESS] 双语 PDF：{bilingual_pdf}")
        return {**outcome, "bilingual_pdf_path": str(bilingual_pdf), "original_pdf_path": original}
    except Exception as exc:
        message = f"双语 PDF 生成失败：{exc}；译文 PDF 已保留：{translated_pdf}"
        checkpoint.data["bilingual"] = {"status": "failed", "message": message}
        checkpoint.fail("bilingual", message)
        return {**outcome, "status": "failed_bilingual", "message": message, "bilingual_pdf_path": None}


def prepare_projects(
    config: Dict[str, Any],
    project_items: Optional[Iterable[str]] = None,
    all_existing: bool = False,
) -> tuple[List[str], Dict[str, Any], str, str]:
    input_items = config.get("paper_list", [])
    projects_dir = str(resolve_path(config.get("tex_sources_dir", "tex source")))
    output_dir = str(resolve_path(config.get("output_dir", "outputs")))

    os.makedirs(projects_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    paper_list = extract_arxiv_ids(input_items)
    project_items = [item for item in (project_items or []) if item]

    if paper_list or project_items:
        projects: List[str] = []

        if paper_list:
            projects.extend(batch_download_arxiv_tex(paper_list, projects_dir))
            if not config.get("user_term"):
                config["category"] = get_arxiv_category(paper_list)
            extract_compressed_files(projects_dir)

        for project_path in project_items:
            resolved_project_path = str(resolve_path(project_path))
            if os.path.isdir(resolved_project_path):
                projects.append(os.path.abspath(resolved_project_path))
                continue
            if is_local_archive(resolved_project_path):
                try:
                    projects.append(extract_local_archive(resolved_project_path, projects_dir))
                except Exception as e:
                    print(f"[SKIP] Failed to extract local archive {project_path}: {e}")
                continue
            print(f"[SKIP] Invalid local project path: {project_path}")
    elif all_existing:
        print("No explicit inputs. Processing all existing projects in the specified directory.")
        extract_compressed_files(projects_dir)
        projects = get_profect_dirs(projects_dir)
        if not projects:
            raise ValueError("No projects found. Check 'tex_sources_dir' and 'paper_list' in config.")
    else:
        raise ValueError("No input provided. Use --arxiv or --project. To process existing projects, pass --all-existing.")

    projects = [os.path.abspath(p) for p in projects if isinstance(p, (str, os.PathLike))]
    projects = list(dict.fromkeys(projects))
    if not projects:
        raise ValueError("No valid TeX projects available for processing.")

    return projects, config, projects_dir, output_dir


def run_projects(
    config: Dict[str, Any],
    projects: Sequence[str],
    output_dir: str,
    event_callback: Optional[ProjectEventCallback] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    completed_projects: List[Dict[str, Any]] = []
    failed_projects: List[Dict[str, Any]] = []
    total_projects = len(projects)
    for idx, project_dir in enumerate(projects, start=1):
        project_name = os.path.basename(project_dir)
        print(f"[{idx}/{total_projects}] Processing {project_name}")
        if event_callback:
            event_callback(
                {
                    "type": "project_start",
                    "index": idx,
                    "total": total_projects,
                    "project_name": project_name,
                    "project_dir": project_dir,
                }
            )

        try:
            latex_trans = CoordinatorAgent(
                config=config,
                project_dir=project_dir,
                output_dir=output_dir,
            )
            outcome = normalize_project_result(latex_trans.workflow_latextrans())
            outcome = _add_bilingual_result(outcome, project_dir, output_dir, config)
        except Exception as e:
            outcome = {"status": "error", "message": str(e), "pdf_path": None}

        info = {
            "index": idx,
            "total": total_projects,
            "project_name": project_name,
            "project_dir": project_dir,
        }
        _record_outcome(info, outcome, completed_projects, failed_projects, event_callback)
    return {
        "completed_projects": completed_projects,
        "failed_projects": failed_projects,
    }


def _record_outcome(
    info: Dict[str, Any],
    outcome: Dict[str, Any],
    completed_projects: List[Dict[str, Any]],
    failed_projects: List[Dict[str, Any]],
    event_callback: Optional[ProjectEventCallback] = None,
) -> None:
    """按协调器返回的状态把项目归入成功或失败，并触发对应事件。"""
    status = outcome.get("status", "success")
    details = {
        **info,
        "status": status,
        "pdf_path": outcome.get("pdf_path"),
    }
    if outcome.get("downloads_path"):
        details["downloads_path"] = outcome["downloads_path"]
    for key in ("bilingual_pdf_path", "original_pdf_path", "resumed"):
        if outcome.get(key) is not None:
            details[key] = outcome[key]
    if status == "success":
        completed_projects.append(details)
        if event_callback:
            event_callback({"type": "project_complete", **details})
        return

    message = outcome.get("message") or status
    print(f"Error processing project {info['project_name']} [{status}]: {message}")
    details["error"] = message
    failed_projects.append(details)
    if event_callback:
        event_callback({"type": "project_error", **details})


def _thread_initializer() -> Optional[Callable[[], None]]:
    """Streamlit 界面调用需要脚本上下文；把它带到工作线程中。"""
    if "streamlit" not in sys.modules:
        return None
    try:
        from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
    except ImportError:
        return None
    ctx = get_script_run_ctx()
    if ctx is None:
        return None
    return lambda: add_script_run_ctx(threading.current_thread(), ctx)


def _resolve_local_projects(
    project_items: Iterable[str], projects_dir: str,
    errors: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    projects: List[str] = []
    for project_path in project_items:
        resolved_project_path = str(resolve_path(project_path))
        if os.path.isdir(resolved_project_path):
            projects.append(os.path.abspath(resolved_project_path))
            continue
        if is_local_archive(resolved_project_path):
            try:
                projects.append(extract_local_archive(resolved_project_path, projects_dir))
            except Exception as e:
                message = f"Failed to extract local archive {project_path}: {e}"
                if errors is not None:
                    errors.append({"project_dir": resolved_project_path, "message": message})
                else:
                    print(f"[FAIL] {message}")
            continue
        message = f"Invalid local project path: {project_path}"
        if errors is not None:
            errors.append({"project_dir": resolved_project_path, "message": message})
        else:
            print(f"[FAIL] {message}")
    return projects


def _project_concurrency(config: Dict[str, Any], default: int = 4) -> int:
    try:
        return max(1, int(config.get("project_concurrency", default)))
    except (TypeError, ValueError):
        return default


def run_pipeline(
    config: Dict[str, Any],
    project_items: Optional[Iterable[str]] = None,
    all_existing: bool = False,
    event_callback: Optional[ProjectEventCallback] = None,
) -> Dict[str, Any]:
    """流水线：每篇论文源码一解压就立刻进入解析、翻译和编译。

    源码、学科分类和原文 PDF 三个下载并发进行；分类只在翻译前等待，
    原文 PDF 在编译结束后才放入结果目录，二者都不阻塞关键路径。
    多篇论文之间最多 ``project_concurrency`` 篇同时处理。
    """
    projects_dir = str(resolve_path(config.get("tex_sources_dir", "tex source")))
    output_dir = str(resolve_path(config.get("output_dir", "outputs")))
    os.makedirs(projects_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    paper_list = list(dict.fromkeys(extract_arxiv_ids(config.get("paper_list", []))))
    project_items = [item for item in (project_items or []) if item]
    input_errors: List[Dict[str, str]] = []

    if paper_list or project_items:
        local_projects = _resolve_local_projects(project_items, projects_dir, input_errors)
    elif all_existing:
        print("No explicit inputs. Processing all existing projects in the specified directory.")
        extract_compressed_files(projects_dir)
        local_projects = get_profect_dirs(projects_dir)
        if not local_projects:
            raise ValueError("No projects found. Check 'tex_sources_dir' and 'paper_list' in config.")
    else:
        raise ValueError("No input provided. Use --arxiv or --project. To process existing projects, pass --all-existing.")

    target_language = config.get("target_language", "ch")
    fetch_categories = not config.get("user_term")
    if fetch_categories:
        config["category"] = dict(config.get("category") or {})
    cached_papers = {}
    for arxiv_id in paper_list:
        project = Path(projects_dir) / arxiv_id
        if not project.is_dir():
            continue
        cached = CheckpointStore(str(Path(output_dir) / f"{target_language}_{arxiv_id}"), str(project), config)
        if cached.valid:
            cached_papers[arxiv_id] = cached
            if fetch_categories:
                config["category"].setdefault(arxiv_id, cached.metadata["translation"]["categories"])
    all_reusable = all(
        arxiv_id in cached_papers and cached_papers[arxiv_id].completed_result() is not None
        for arxiv_id in paper_list
    ) and all(
        CheckpointStore(
            str(Path(output_dir) / f"{target_language}_{Path(project).name}"), project, config,
        ).completed_result() is not None
        for project in local_projects
    )
    total_projects = len(paper_list) + len(local_projects) + len(input_errors)
    projects: List[str] = []
    completed_projects: List[Dict[str, Any]] = []
    failed_projects: List[Dict[str, Any]] = []
    attempted_inputs = 0

    def project_info(project_dir: str) -> Dict[str, Any]:
        nonlocal attempted_inputs
        attempted_inputs += 1
        return {
            "index": attempted_inputs, "total": total_projects,
            "project_name": os.path.basename(project_dir), "project_dir": project_dir,
        }

    def record_input_failure(project_dir: str, message: str, status: str = "failed_input"):
        info = project_info(project_dir)
        _record_outcome(info, {"status": status, "message": message}, completed_projects, failed_projects, event_callback)

    for error in input_errors:
        record_input_failure(error["project_dir"], error["message"])

    llm_url = urlparse(str(config.get("llm_config", {}).get("base_url", "")))
    llm_host = llm_url.hostname
    llm_port = llm_url.port or (80 if llm_url.scheme == "http" else 443)
    dns_cache.install([llm_host, "arxiv.org", "export.arxiv.org"])

    initializer = _thread_initializer()
    io_workers = max(2, min(16, 3 * len(paper_list) + 1))
    with ThreadPoolExecutor(max_workers=io_workers, initializer=initializer) as io_pool, \
            ThreadPoolExecutor(max_workers=_project_concurrency(config), initializer=initializer) as work_pool:
        # LLM 服务的 DNS 解析与论文下载并行，翻译开始时无需再等待。
        if llm_host and not all_reusable:
            io_pool.submit(dns_cache.prewarm, llm_host, llm_port)
        source_futures = {
            (io_pool.submit(str, Path(projects_dir) / arxiv_id) if arxiv_id in cached_papers
             else io_pool.submit(download_arxiv_source, arxiv_id, projects_dir)): arxiv_id
            for arxiv_id in paper_list
        }
        category_futures = {
            arxiv_id: io_pool.submit(fetch_arxiv_categories, arxiv_id)
            for arxiv_id in paper_list
            if fetch_categories and arxiv_id not in cached_papers and arxiv_id not in config.get("category", {})
        }
        # 带宽有限：原文 PDF 不在关键路径上，等全部源码下载完再开始。
        sources_done = threading.Event()

        def download_pdf_after_sources(arxiv_id: str) -> Optional[str]:
            sources_done.wait()
            return download_arxiv_pdf(arxiv_id, projects_dir)

        pdf_futures = {
            arxiv_id: io_pool.submit(download_pdf_after_sources, arxiv_id)
            for arxiv_id in paper_list
            if as_bool(config.get("bilingual", False)) or arxiv_id not in cached_papers
            or cached_papers[arxiv_id].completed_result() is None
        }
        work_futures: Dict[Any, Dict[str, Any]] = {}
        output_owners: Dict[str, str] = {}
        submitted_projects = set()

        def process(project_dir: str, arxiv_id: Optional[str]) -> Dict[str, Any]:
            project_config = config
            if arxiv_id in category_futures:
                try:
                    categories = category_futures[arxiv_id].result()
                except Exception as e:
                    print(f"[WARNING] Failed to fetch categories for {arxiv_id}: {e}")
                    categories = []
                config["category"][arxiv_id] = categories
                project_config = dict(config, category={arxiv_id: categories})
            try:
                outcome = normalize_project_result(
                    CoordinatorAgent(
                        config=project_config,
                        project_dir=project_dir,
                        output_dir=output_dir,
                    ).workflow_latextrans()
                )
            finally:
                if arxiv_id in pdf_futures:
                    base_name = os.path.basename(project_dir)
                    try:
                        place_arxiv_pdf(
                            pdf_futures[arxiv_id].result(),
                            arxiv_id,
                            [project_dir, os.path.join(output_dir, f"{target_language}_{base_name}", base_name)],
                        )
                    except OSError as e:
                        print(f"[WARNING] Failed to place original PDF for {arxiv_id}: {e}")
            return _add_bilingual_result(outcome, project_dir, output_dir, project_config, allow_download=arxiv_id not in pdf_futures)

        def submit(project_dir: str, arxiv_id: Optional[str] = None):
            project_dir = os.path.abspath(project_dir)
            project_key = os.path.normcase(str(Path(project_dir).resolve()))
            if project_key in submitted_projects:
                return None
            submitted_projects.add(project_key)
            output_key = os.path.normcase(str((Path(output_dir) / f"{target_language}_{Path(project_dir).name}").resolve()))
            if output_key in output_owners:
                record_input_failure(
                    project_dir, f"Output directory conflict with {output_owners[output_key]}; use distinct project names.",
                    "failed_output_conflict",
                )
                return None
            output_owners[output_key] = project_dir
            projects.append(project_dir)
            info = project_info(project_dir)
            print(f"[{info['index']}/{total_projects}] Processing {info['project_name']}")
            if event_callback:
                event_callback({"type": "project_start", **info})
            future = work_pool.submit(process, project_dir, arxiv_id)
            work_futures[future] = info
            return future

        pending = set(source_futures)
        for project_dir in local_projects:
            future = submit(project_dir)
            if future is not None:
                pending.add(future)

        # 事件回调只在主线程触发，界面组件无需关心线程安全。
        try:
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    if future in source_futures:
                        arxiv_id = source_futures[future]
                        try:
                            project_dir = future.result()
                            download_error = "Source download returned no TeX project."
                        except Exception as e:
                            download_error = f"Source download failed for {arxiv_id}: {e}"
                            project_dir = None
                        if not project_dir:
                            record_input_failure(str(Path(projects_dir) / arxiv_id), download_error, "failed_download")
                        work_future = submit(project_dir, arxiv_id) if project_dir else None
                        if work_future is not None:
                            pending.add(work_future)
                        if all(f.done() for f in source_futures):
                            sources_done.set()
                        continue

                    info = work_futures[future]
                    try:
                        outcome = normalize_project_result(future.result())
                    except Exception as e:
                        outcome = {"status": "error", "message": str(e), "pdf_path": None}
                    _record_outcome(info, outcome, completed_projects, failed_projects, event_callback)
        finally:
            sources_done.set()

    if not projects and not failed_projects:
        raise ValueError("No valid TeX projects available for processing.")

    return {
        "config": config,
        "projects": projects,
        "projects_dir": projects_dir,
        "output_dir": output_dir,
        "completed_projects": completed_projects,
        "failed_projects": failed_projects,
    }


def run_translation(
    config_path: str = "config/default.toml",
    overrides: Optional[Dict[str, Any]] = None,
    project_items: Optional[Iterable[str]] = None,
    all_existing: bool = False,
    event_callback: Optional[ProjectEventCallback] = None,
) -> Dict[str, Any]:
    config = load_runtime_config(config_path=config_path, overrides=overrides)
    return run_pipeline(
        config=config,
        project_items=project_items,
        all_existing=all_existing,
        event_callback=event_callback,
    )
