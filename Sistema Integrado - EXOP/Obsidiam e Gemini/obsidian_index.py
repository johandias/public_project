from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from obsidian_markdown import MarkdownNote, TextChunk, chunk_note, normalize_note_name
from obsidian_wikilinks import WikiLink


@dataclass(frozen=True)
class ResolvedLink:
    raw: str
    source_id: str
    target_id: str
    target_title: str
    target_path: str
    section: str | None
    alias: str | None


@dataclass(frozen=True)
class UnresolvedLink:
    raw: str
    source_id: str
    target_name: str
    section: str | None
    alias: str | None


@dataclass
class VaultIndex:
    notes: dict[str, MarkdownNote]
    aliases: dict[str, str]
    outgoing: dict[str, list[ResolvedLink]]
    backlinks: dict[str, list[ResolvedLink]]
    unresolved: dict[str, list[UnresolvedLink]]
    chunks: list[TextChunk]


def build_vault_index(notes: list[MarkdownNote], max_chars_per_chunk: int) -> VaultIndex:
    notes_by_id = {note.note_id: note for note in notes}
    aliases = build_alias_index(notes)
    outgoing: dict[str, list[ResolvedLink]] = defaultdict(list)
    backlinks: dict[str, list[ResolvedLink]] = defaultdict(list)
    unresolved: dict[str, list[UnresolvedLink]] = defaultdict(list)
    chunks: list[TextChunk] = []

    for note in notes:
        chunks.extend(chunk_note(note, max_chars_per_chunk))
        for wikilink in note.wikilinks:
            target_id = resolve_wikilink(wikilink, notes_by_id, aliases)
            if not target_id or target_id == note.note_id:
                unresolved[note.note_id].append(
                    UnresolvedLink(
                        raw=wikilink.raw,
                        source_id=note.note_id,
                        target_name=wikilink.note_name,
                        section=wikilink.section,
                        alias=wikilink.alias,
                    )
                )
                continue

            target_note = notes_by_id[target_id]
            resolved = ResolvedLink(
                raw=wikilink.raw,
                source_id=note.note_id,
                target_id=target_id,
                target_title=target_note.title,
                target_path=target_note.path,
                section=wikilink.section,
                alias=wikilink.alias,
            )
            outgoing[note.note_id].append(resolved)
            backlinks[target_id].append(resolved)

    return VaultIndex(
        notes=notes_by_id,
        aliases=aliases,
        outgoing=dict(outgoing),
        backlinks=dict(backlinks),
        unresolved=dict(unresolved),
        chunks=chunks,
    )


def build_alias_index(notes: list[MarkdownNote]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for note in notes:
        aliases[note.note_id] = note.note_id
        aliases[normalize_note_name(note.title)] = note.note_id
        for alias in note.aliases:
            aliases[normalize_note_name(alias)] = note.note_id
    return aliases


def resolve_wikilink(
    wikilink: WikiLink,
    notes_by_id: dict[str, MarkdownNote],
    aliases: dict[str, str],
) -> str | None:
    if wikilink.note_name in notes_by_id:
        return wikilink.note_name
    return aliases.get(wikilink.note_name)


def summarize_note(note: MarkdownNote, max_chars: int = 600) -> str:
    text = " ".join(line.strip() for line in note.content.splitlines() if line.strip())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 24].rstrip() + " ... [resumo truncado]"


def format_link(link: ResolvedLink) -> str:
    section = f" > {link.section}" if link.section else ""
    alias = f" (alias: {link.alias})" if link.alias else ""
    return f"- {link.target_title}{section}{alias} [{link.target_path}]"


def format_backlink(link: ResolvedLink, index: VaultIndex) -> str:
    source = index.notes[link.source_id]
    section = f" > {link.section}" if link.section else ""
    alias = f" (alias: {link.alias})" if link.alias else ""
    return f"- {source.title}{section}{alias} [{source.path}]"


def build_note_context(
    note: MarkdownNote,
    index: VaultIndex,
    include_related_summaries: bool = True,
    related_summary_chars: int = 500,
) -> str:
    related_links = index.outgoing.get(note.note_id, [])
    backlinks = index.backlinks.get(note.note_id, [])
    unresolved = index.unresolved.get(note.note_id, [])

    related_titles = [format_link(link) for link in related_links]
    backlink_titles = [format_backlink(link, index) for link in backlinks]

    related_summaries: list[str] = []
    if include_related_summaries:
        for link in related_links[:8]:
            related_note = index.notes[link.target_id]
            related_summaries.append(
                f"### {related_note.title}\n"
                f"Caminho: {related_note.path}\n"
                f"Resumo curto: {summarize_note(related_note, related_summary_chars)}"
            )

    headings = "\n".join(f"- {heading}" for heading in note.headings) or "- nenhum"
    tags = "\n".join(f"- #{tag}" for tag in note.tags) or "- nenhuma"
    aliases = "\n".join(f"- {alias}" for alias in note.aliases) or "- nenhum"
    related_text = "\n".join(related_titles) or "- nenhuma nota relacionada resolvida"
    backlinks_text = "\n".join(backlink_titles) or "- nenhum backlink encontrado"
    unresolved_text = "\n".join(f"- {link.raw}" for link in unresolved) or "- nenhum"
    related_summaries_text = "\n\n".join(related_summaries) or "Nenhum resumo relacionado incluido."

    return f"""NOTA ATUAL:
Titulo: {note.title}
Caminho: {note.path}
Modificado em: {note.modified_at}
Hash: {note.content_hash}

ALIASES:
{aliases}

TAGS:
{tags}

HEADINGS:
{headings}

NOTAS RELACIONADAS:
{related_text}

RESUMOS CURTOS DAS NOTAS RELACIONADAS:
{related_summaries_text}

BACKLINKS:
{backlinks_text}

WIKILINKS NAO RESOLVIDOS:
{unresolved_text}

CONTEUDO:
{note.content}
"""


def build_chunk_context(chunk: TextChunk, index: VaultIndex) -> str:
    note = index.notes[chunk.note_id]
    relational_context = build_note_context(
        note,
        index,
        include_related_summaries=True,
        related_summary_chars=350,
    )
    return f"""{relational_context}

BLOCO ANALISADO:
Heading: {chunk.heading}
Chunk ID: {chunk.chunk_id}

{chunk.content}
"""


def export_index_metadata(index: VaultIndex) -> dict[str, object]:
    notes_payload: dict[str, object] = {}
    for note_id, note in index.notes.items():
        notes_payload[note_id] = {
            "title": note.title,
            "path": note.path,
            "content_hash": note.content_hash,
            "modified_at": note.modified_at,
            "headings": note.headings,
            "tags": note.tags,
            "aliases": note.aliases,
            "links": [link.__dict__ for link in index.outgoing.get(note_id, [])],
            "backlinks": [link.__dict__ for link in index.backlinks.get(note_id, [])],
            "unresolved_links": [link.__dict__ for link in index.unresolved.get(note_id, [])],
        }

    return {
        "notes": notes_payload,
        "aliases": index.aliases,
        "chunk_count": len(index.chunks),
        "rag_ready": {
            "embedding_model": None,
            "vector_store": None,
            "supported_targets": ["ChromaDB", "FAISS", "Qdrant", "pgvector"],
        },
    }
