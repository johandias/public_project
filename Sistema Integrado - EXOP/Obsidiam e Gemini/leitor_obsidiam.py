from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from gemini_analyzer import GeminiAnalyzer, GeminiSettings
from obsidian_cache import load_cache, mark_deleted_files, save_cache, upsert_file_metadata
from obsidian_gemini_core import DEFAULT_MODEL, DEFAULT_VAULT_PATH
from obsidian_index import build_chunk_context, build_vault_index, export_index_metadata
from obsidian_markdown import MarkdownNote, TextChunk, load_markdown_notes, sha256_text


PROMPT_VERSION = "2026-05-13-obsidian-graph-v1"

MODEL_NAME = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", str(DEFAULT_VAULT_PATH)))
MAX_CHARS_PER_CHUNK = int(os.getenv("OBSIDIAN_MAX_CHARS", "120000"))
MAX_API_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "4"))
RETRY_BASE_SECONDS = float(os.getenv("GEMINI_RETRY_BASE_SECONDS", "2"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))
MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2000"))

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = Path(os.getenv("OBSIDIAN_REPORT_FILE", str(SCRIPT_DIR / "analise_obsidiam.txt")))
CACHE_FILE = Path(os.getenv("OBSIDIAN_CACHE_FILE", str(SCRIPT_DIR / "analise_obsidiam_cache.json")))


def configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("leitor_obsidiam")


logger = configure_logging()


def cache_key(kind: str, content_hash: str) -> str:
    return sha256_text(f"{PROMPT_VERSION}|{MODEL_NAME}|{kind}|{content_hash}")


def register_notes_in_cache(cache: dict[str, Any], notes: list[MarkdownNote]) -> None:
    current_paths = {note.path for note in notes}
    for note in notes:
        upsert_file_metadata(
            cache,
            {
                "path": note.path,
                "title": note.title,
                "content_hash": note.content_hash,
                "modified_at": note.modified_at,
                "headings": note.headings,
                "tags": note.tags,
                "aliases": note.aliases,
            },
        )
    mark_deleted_files(cache, current_paths)


def summarize_chunks(
    analyzer: GeminiAnalyzer,
    chunks: list[TextChunk],
    index_contexts: dict[str, str],
    cache: dict[str, Any],
) -> list[str]:
    summaries: list[str] = []
    chunk_cache = cache.setdefault("chunks", {})

    for position, chunk in enumerate(chunks, start=1):
        context = index_contexts[chunk.chunk_id]
        context_hash = sha256_text(context)
        key = cache_key("chunk", context_hash)
        cached_summary = chunk_cache.get(key, {}).get("summary")

        if cached_summary:
            logger.info(
                "Chunk %s/%s reutilizado do cache: %s > %s",
                position,
                len(chunks),
                chunk.title,
                chunk.heading,
            )
            summary = cached_summary
        else:
            logger.info(
                "Analisando chunk %s/%s: %s > %s",
                position,
                len(chunks),
                chunk.title,
                chunk.heading,
            )
            summary = analyzer.summarize_context(context)
            chunk_cache[key] = {
                "summary": summary,
                "chunk_id": chunk.chunk_id,
                "note_id": chunk.note_id,
                "title": chunk.title,
                "path": chunk.path,
                "heading": chunk.heading,
                "content_hash": chunk.content_hash,
                "context_hash": context_hash,
                "model": MODEL_NAME,
                "prompt_version": PROMPT_VERSION,
                "tags": chunk.tags,
                # Campos reservados para embeddings e vector stores.
                "embedding": None,
                "embedding_model": None,
                "vector_store_id": None,
            }
            save_cache(cache, CACHE_FILE)

        summaries.append(f"Nota: {chunk.title}\nSecao: {chunk.heading}\nResumo:\n{summary}")

    return summaries


def generate_final_report(
    analyzer: GeminiAnalyzer,
    partial_summaries: list[str],
    cache: dict[str, Any],
) -> str:
    summaries_text = "\n\n".join(partial_summaries)
    summaries_hash = sha256_text(summaries_text)
    key = cache_key("final-report", summaries_hash)
    report_cache = cache.setdefault("final_reports", {})
    cached_report = report_cache.get(key, {}).get("report")

    if cached_report:
        logger.info("Relatorio final reutilizado do cache.")
        return cached_report

    logger.info("Gerando relatorio final consolidado.")
    report = analyzer.generate_final_report(partial_summaries)
    report_cache[key] = {
        "report": report,
        "summaries_hash": summaries_hash,
        "model": MODEL_NAME,
        "prompt_version": PROMPT_VERSION,
    }
    save_cache(cache, CACHE_FILE)
    return report


def analyze_vault(notes: list[MarkdownNote], cache: dict[str, Any]) -> str:
    register_notes_in_cache(cache, notes)
    index = build_vault_index(notes, MAX_CHARS_PER_CHUNK)
    cache["index"] = export_index_metadata(index)
    save_cache(cache, CACHE_FILE)

    logger.info(
        "Indice construido: %s notas, %s chunks, %s aliases.",
        len(index.notes),
        len(index.chunks),
        len(index.aliases),
    )

    chunk_contexts = {
        chunk.chunk_id: build_chunk_context(chunk, index)
        for chunk in index.chunks
    }
    analyzer = GeminiAnalyzer(
        settings=GeminiSettings(
            model_name=MODEL_NAME,
            max_retries=MAX_API_RETRIES,
            retry_base_seconds=RETRY_BASE_SECONDS,
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        ),
        logger=logger,
    )
    partial_summaries = summarize_chunks(analyzer, index.chunks, chunk_contexts, cache)
    return generate_final_report(analyzer, partial_summaries, cache)


def main() -> None:
    logger.info("Carregando vault Obsidian em: %s", VAULT_PATH)

    try:
        notes = load_markdown_notes(VAULT_PATH)
    except Exception:
        logger.exception("Falha ao carregar o vault.")
        raise

    if not notes:
        raise SystemExit("Nenhum arquivo .md foi encontrado no vault informado.")

    logger.info("%s notas Markdown carregadas.", len(notes))
    cache = load_cache(CACHE_FILE, PROMPT_VERSION, MODEL_NAME)
    report = analyze_vault(notes, cache)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(report, encoding="utf-8")
    save_cache(cache, CACHE_FILE)

    print(report)
    logger.info("Analise salva em: %s", OUTPUT_FILE)
    logger.info("Cache e metadados salvos em: %s", CACHE_FILE)


if __name__ == "__main__":
    main()
