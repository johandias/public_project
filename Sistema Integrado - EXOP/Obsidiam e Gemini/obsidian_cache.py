from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_cache(prompt_version: str, model_name: str) -> dict[str, Any]:
    return {
        "version": 2,
        "prompt_version": prompt_version,
        "model": model_name,
        "updated_at": utc_now_iso(),
        "files": {},
        "chunks": {},
        "final_reports": {},
        "index": {},
        "rag_index": {
            "status": "not_built",
            "embedding_model": None,
            "vector_store": None,
            "items": [],
        },
    }


def load_cache(cache_path: Path, prompt_version: str, model_name: str) -> dict[str, Any]:
    if not cache_path.exists():
        return empty_cache(prompt_version, model_name)

    try:
        with cache_path.open("r", encoding="utf-8") as file:
            cache = json.load(file)
    except (json.JSONDecodeError, OSError):
        return empty_cache(prompt_version, model_name)

    cache.setdefault("version", 2)
    cache.setdefault("prompt_version", prompt_version)
    cache.setdefault("model", model_name)
    cache.setdefault("updated_at", utc_now_iso())
    cache.setdefault("files", {})
    cache.setdefault("chunks", {})
    cache.setdefault("blocks", {})
    cache.setdefault("final_reports", {})
    cache.setdefault("index", {})
    cache.setdefault(
        "rag_index",
        {
            "status": "not_built",
            "embedding_model": None,
            "vector_store": None,
            "items": [],
        },
    )

    # Compatibilidade com a versao anterior, que usava "blocks".
    if cache.get("blocks") and not cache.get("chunks"):
        cache["chunks"] = cache["blocks"]

    return cache


def save_cache(cache: dict[str, Any], cache_path: Path) -> None:
    cache["updated_at"] = utc_now_iso()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)


def upsert_file_metadata(cache: dict[str, Any], file_metadata: dict[str, Any]) -> None:
    files = cache.setdefault("files", {})
    files[file_metadata["path"]] = file_metadata | {"last_seen_at": utc_now_iso()}


def mark_deleted_files(cache: dict[str, Any], current_paths: set[str]) -> None:
    files = cache.setdefault("files", {})
    for path, metadata in files.items():
        if path not in current_paths:
            metadata["deleted"] = True
            metadata["last_seen_at"] = utc_now_iso()
