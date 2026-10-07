"""Readable stage and fragment checkpoints, using filesystem metadata only."""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from src.utils.paths import project_path


CHECKPOINT_VERSION = 1
MAP_FILES = (
    "sections_map.json", "captions_map.json", "envs_map.json",
    "newcommands_map.json", "inputs_map.json",
)
MAP_KINDS = {"sec": "sections_map.json", "cap": "captions_map.json", "env": "envs_map.json"}
_TRANSIENT_SUFFIXES = (".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".synctex.gz", ".xdv", ".dvi")
_LLM_SETTINGS = (
    "model", "base_url", "thinking_type", "temperature", "max_tokens", "max_chunk_chars",
    "glossary_max_terms", "skip_untranslatable_chunks", "use_context", "context_summary",
    "context_max_chars", "extra_body", "reasoning_effort", "prompt",
)


def atomic_write_json(path, data: Any) -> None:
    """Replace a JSON file atomically so interrupted writes cannot corrupt it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return default


def file_metadata(path) -> Dict[str, Any]:
    path = Path(path).resolve()
    data: Dict[str, Any] = {"path": str(path)}
    try:
        stat = path.stat()
        data.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    except OSError:
        data["missing"] = True
    return data


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or path.exists():
        return path.resolve()
    return (Path(__file__).resolve().parents[2] / path).resolve()


def _enabled(value: Any, default=True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"false", "0", "no", "off", ""}


def build_metadata(project_dir: str, config: Dict[str, Any], previous: Optional[Dict[str, Any]] = None, output_dir=None) -> Dict[str, Any]:
    project = Path(project_dir).resolve()
    output = Path(output_dir).resolve() if output_dir else None
    sources = []
    for path in sorted(project.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project)
        if any(part in {".git", ".latextrans", "__pycache__", ".pytest_cache"} or part.startswith("build_") for part in relative.parts[:-1]):
            continue
        if output is not None and path.is_relative_to(output):
            continue
        # The separately downloaded original PDF is not a TeX source dependency.
        if relative == Path(f"{project.name}.pdf") or path.name.lower().endswith(_TRANSIENT_SUFFIXES):
            continue
        stat = path.stat()
        sources.append({"path": relative.as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})

    category_map = config.get("category") or {}
    categories = category_map.get(project.name) if isinstance(category_map, dict) else None
    if categories is None:
        previous = previous if isinstance(previous, dict) else {}
        previous_translation = previous.get("translation")
        previous_translation = previous_translation if isinstance(previous_translation, dict) else {}
        categories = previous_translation.get("categories", [])
    if isinstance(categories, str):
        categories = [categories]
    if not isinstance(categories, (list, tuple)):
        categories = []
    categories = list(categories or [])
    user_term = str(config.get("user_term") or "").strip()
    terms = []
    if user_term:
        terms.append(file_metadata(_resolve_path(user_term)))
    else:
        term_roots = []
        if config.get("terms_dir"):
            term_roots.append(_resolve_path(str(config["terms_dir"])))
        term_roots.append(Path(__file__).resolve().parents[2] / "terms")
        for filename in [f"{category}.csv" for category in categories]:
            candidate = next((root / filename for root in term_roots if (root / filename).is_file()), None)
            if candidate is not None:
                terms.append(file_metadata(candidate))
        if not terms:
            candidate = next((root / "default.csv" for root in term_roots if (root / "default.csv").is_file()), term_roots[0] / "default.csv")
            terms.append(file_metadata(candidate))
    llm_config = config.get("llm_config") or {}
    translation = {
        "source_language": config.get("source_language", "en"),
        "target_language": config.get("target_language", "ch"),
        "mode": config.get("mode", 0),
        "update_term": _enabled(config.get("update_term", False), False),
        "validation_prose_equivalences": _enabled(config.get("validation_prose_equivalences", True)),
        "categories": categories,
        "user_term": str(_resolve_path(user_term)) if user_term else "",
        "llm": {key: llm_config[key] for key in _LLM_SETTINGS if key in llm_config},
    }
    return {"project_dir": str(project), "sources": sources, "translation": translation, "term_files": terms}


def compile_metadata(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "compile_repair": _enabled(config.get("compile_repair", True)),
        "compile_repair_attempts": config.get("compile_repair_attempts", 2),
        **{key: value for key, value in config.items() if key.startswith("compiler_")},
    }


class CheckpointStore:
    def __init__(self, output_dir: str, project_dir: str, config: Dict[str, Any]):
        self.output_dir = Path(output_dir).resolve()
        self.path = self._safe_path(self.output_dir / ".latextrans" / "checkpoint.json")
        old = read_json(self.path, {})
        old = old if isinstance(old, dict) else {}
        if (
            not isinstance(old.get("metadata", {}), dict)
            or not isinstance(old.get("stages", {}), dict)
            or not isinstance(old.get("outcome", {}), dict)
            or ("bilingual" in old and not isinstance(old["bilingual"], dict))
        ):
            old = {}
        self.metadata = build_metadata(project_dir, config, old.get("metadata"), self.output_dir)
        self.compile_settings = compile_metadata(config)
        self.valid = (
            _enabled(config.get("resume", True)) and not _enabled(config.get("force", False), False)
            and old.get("version") == CHECKPOINT_VERSION and old.get("metadata") == self.metadata
        )
        self.data = old if self.valid else {
            "version": CHECKPOINT_VERSION, "run_id": uuid.uuid4().hex,
            "metadata": self.metadata, "stages": {}, "stage": "parse", "status": "pending",
        }
        run_id = str(self.data.get("run_id", ""))
        # A malformed checkpoint cannot direct writes outside this project.
        try:
            uuid.UUID(hex=run_id)
        except (ValueError, AttributeError):
            self.valid = False
            self.data.update(run_id=uuid.uuid4().hex, stages={}, stage="parse", status="pending")
        self.run_dir = self._safe_path(self.output_dir / ".latextrans" / "runs" / self.data["run_id"])

    def _safe_path(self, path) -> Path:
        return project_path(self.output_dir, path, writing=True)

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self._safe_path(self.path), self.data)

    def begin(self, stage: str) -> None:
        self.data.update(stage=stage, status="in_progress")
        self.data.setdefault("stages", {})[stage] = "in_progress"
        self.save()

    def fail(self, stage: str, message: str = "") -> None:
        self.data.update(stage=stage, status="failed", message=message)
        self.data.setdefault("stages", {})[stage] = "failed"
        self.save()

    def stage_completed(self, stage: str, snapshot=False) -> None:
        if snapshot:
            for filename in MAP_FILES:
                value = read_json(self._safe_path(self.output_dir / filename))
                if isinstance(value, list):
                    atomic_write_json(self._safe_path(self.run_dir / stage / filename), value)
        self.data.update(stage=stage, status="completed")
        self.data.setdefault("stages", {})[stage] = "completed"
        self.save()

    def is_complete(self, stage: str) -> bool:
        return self.data.get("stages", {}).get(stage) == "completed"

    def completed_result(self) -> Optional[Dict[str, Any]]:
        outcome = self.data.get("outcome") or {}
        if not self.valid or not self.is_complete("compile") or self.data.get("compile_settings") != self.compile_settings:
            return None
        pdf = outcome.get("pdf_path")
        if outcome.get("status") != "success" or not isinstance(pdf, (str, os.PathLike)) or not pdf:
            return None
        try:
            if not Path(pdf).is_file() or Path(pdf).stat().st_size == 0:
                return None
        except OSError:
            return None
        cached = dict(outcome)
        if not isinstance(cached.get("downloads_path"), (str, os.PathLike)):
            cached["downloads_path"] = None
        return cached

    def complete(self, outcome: Dict[str, Any]) -> None:
        pdf = outcome.get("pdf_path")
        if outcome.get("status") != "success" or not pdf or not Path(pdf).is_file() or Path(pdf).stat().st_size == 0:
            raise ValueError("Only a successful, existing PDF can complete a checkpoint.")
        self.data.update(outcome=outcome, compile_settings=self.compile_settings, stage="completed", status="completed")
        self.data.setdefault("stages", {})["compile"] = "completed"
        self.save()

    def restore_maps(self) -> bool:
        if not self.valid:
            return False
        for stage in ("validate", "translate", "parse"):
            if not self.is_complete(stage):
                continue
            maps = {name: read_json(self._safe_path(self.run_dir / stage / name)) for name in MAP_FILES}
            if not all(isinstance(value, list) for value in maps.values()) or not maps["sections_map.json"]:
                continue
            malformed = False
            for kind, filename in MAP_KINDS.items():
                identifier = "section" if kind == "sec" else "placeholder"
                if any(
                    not isinstance(item, dict) or not isinstance(item.get(identifier), (str, int) if kind == "sec" else str)
                    or not isinstance(item.get("content"), str)
                    for item in maps[filename]
                ):
                    malformed = True
                    break
            if malformed:
                continue
            for kind, filename in MAP_KINDS.items():
                for index, item in enumerate(maps[filename]):
                    fragment = self.read_fragment(kind, index, item)
                    if fragment is not None:
                        maps[filename][index] = fragment
            for filename, value in maps.items():
                atomic_write_json(self._safe_path(self.output_dir / filename), value)
            # Falling back to an earlier snapshot also rewinds later stage flags.
            for later in {"parse": ("translate", "validate", "compile"), "translate": ("validate", "compile"), "validate": ()}[stage]:
                self.data["stages"][later] = "pending"
            return True
        return False

    def _fragment_path(self, kind: str, index: int) -> Path:
        if kind not in MAP_KINDS or index < 0:
            raise ValueError("Invalid translation fragment location.")
        return self._safe_path(self.run_dir / "fragments" / f"{kind}-{index:06d}.json")

    def read_fragment(self, kind: str, index: int, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        value = read_json(self._fragment_path(kind, index))
        identifier = "section" if kind == "sec" else "placeholder"
        if not isinstance(value, dict) or value.get(identifier) != item.get(identifier):
            return None
        if not isinstance(value.get("trans_content"), str) or not isinstance(value.get("content"), str):
            return None
        return value

    def save_fragment(self, kind: str, index: int, item: Dict[str, Any]) -> None:
        atomic_write_json(self._fragment_path(kind, index), item)

    def drop_fragment(self, kind: str, index: int) -> None:
        self._fragment_path(kind, index).unlink(missing_ok=True)

    def invalidate_report(self, reports) -> None:
        for kind, filename in MAP_KINDS.items():
            identifier = "section" if kind == "sec" else "placeholder"
            targets = {report.get("num_or_ph") for report in reports if report.get("part") == kind}
            for index, item in enumerate(read_json(self._safe_path(self.output_dir / filename), []) or []):
                if isinstance(item, dict) and item.get(identifier) in targets:
                    self.drop_fragment(kind, index)

    def read_usage(self) -> Optional[Dict[str, Any]]:
        value = read_json(self._safe_path(self.output_dir / "usage.json"))
        return value if self.valid and isinstance(value, dict) else None
