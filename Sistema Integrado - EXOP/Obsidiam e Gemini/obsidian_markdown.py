from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from obsidian_gemini_core import read_markdown_file
from obsidian_wikilinks import WikiLink, parse_wikilinks


HEADING_PATTERN = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
TAG_PATTERN = re.compile(r"(?<![\w/])#([A-Za-z0-9_\-/\u00C0-\u00FF]+)")
FRONTMATTER_ALIAS_PATTERN = re.compile(
    r"(?ms)\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)"
)
SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class MarkdownSection:
    heading: str
    level: int
    content: str


@dataclass(frozen=True)
class MarkdownNote:
    note_id: str
    title: str
    path: str
    absolute_path: Path
    content: str
    content_hash: str
    modified_at: str
    headings: list[str]
    tags: list[str]
    aliases: list[str]
    wikilinks: list[WikiLink]
    sections: list[MarkdownSection]


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    note_id: str
    title: str
    path: str
    heading: str
    content: str
    content_hash: str
    tags: list[str]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_modified_at(path: Path) -> str:
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return modified.isoformat(timespec="seconds")


def normalize_note_name(value: str) -> str:
    cleaned = value.replace("\\", "/").rsplit("/", 1)[-1]
    return Path(cleaned).stem.strip().casefold()


def extract_headings(content: str) -> list[str]:
    return [match.group(2).strip() for match in HEADING_PATTERN.finditer(content)]


def extract_tags(content: str) -> list[str]:
    return sorted({match.group(1).strip() for match in TAG_PATTERN.finditer(content)})


def extract_aliases(content: str) -> list[str]:
    match = FRONTMATTER_ALIAS_PATTERN.match(content)
    if not match:
        return []

    aliases: list[str] = []
    lines = match.group("body").splitlines()
    collecting_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("aliases:", "alias:")):
            _, raw_value = stripped.split(":", 1)
            raw_value = raw_value.strip()
            collecting_list = not raw_value
            if raw_value:
                aliases.extend(parse_inline_aliases(raw_value))
            continue

        if collecting_list and stripped.startswith("- "):
            aliases.append(stripped[2:].strip().strip("\"'"))
        elif collecting_list and stripped:
            collecting_list = False

    return sorted({alias for alias in aliases if alias})


def parse_inline_aliases(raw_value: str) -> list[str]:
    raw_value = raw_value.strip().strip("[]")
    return [item.strip().strip("\"'") for item in raw_value.split(",") if item.strip()]


def split_markdown_sections(content: str) -> list[MarkdownSection]:
    matches = list(HEADING_PATTERN.finditer(content))
    if not matches:
        return [MarkdownSection(heading="Sem heading", level=0, content=content.strip())]

    sections: list[MarkdownSection] = []
    if matches[0].start() > 0:
        intro = content[: matches[0].start()].strip()
        if intro:
            sections.append(MarkdownSection(heading="Introducao", level=0, content=intro))

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        heading = match.group(2).strip()
        level = len(match.group(1))
        section_content = content[start:end].strip()
        if section_content:
            sections.append(MarkdownSection(heading=heading, level=level, content=section_content))

    return sections


def split_long_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_size = 0
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]

    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue

        for sentence in SENTENCE_BOUNDARY_PATTERN.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                units.append(sentence)
            else:
                units.extend(sentence[i : i + max_chars] for i in range(0, len(sentence), max_chars))

    for unit in units:
        unit_size = len(unit) + 2
        if current and current_size + unit_size > max_chars:
            parts.append("\n\n".join(current))
            current = []
            current_size = 0

        current.append(unit)
        current_size += unit_size

    if current:
        parts.append("\n\n".join(current))

    return parts


def load_markdown_notes(vault_path: Path) -> list[MarkdownNote]:
    if not vault_path.exists():
        raise FileNotFoundError(f"Vault nao encontrado: {vault_path}")
    if not vault_path.is_dir():
        raise NotADirectoryError(f"O caminho informado nao e uma pasta: {vault_path}")

    notes: list[MarkdownNote] = []
    for path in sorted(vault_path.rglob("*.md")):
        if not path.is_file():
            continue

        content = read_markdown_file(path).strip()
        relative_path = path.relative_to(vault_path).as_posix()
        title = path.stem
        note_id = normalize_note_name(title)
        notes.append(
            MarkdownNote(
                note_id=note_id,
                title=title,
                path=relative_path,
                absolute_path=path,
                content=content,
                content_hash=sha256_text(content),
                modified_at=file_modified_at(path),
                headings=extract_headings(content),
                tags=extract_tags(content),
                aliases=extract_aliases(content),
                wikilinks=parse_wikilinks(content),
                sections=split_markdown_sections(content),
            )
        )

    return notes


def chunk_note(note: MarkdownNote, max_chars: int) -> list[TextChunk]:
    if max_chars < 5000:
        raise ValueError("OBSIDIAN_MAX_CHARS precisa ser pelo menos 5000.")

    chunks: list[TextChunk] = []
    for section_index, section in enumerate(note.sections, start=1):
        section_header = f"### ARQUIVO: {note.path}\n### SECAO: {section.heading}\n"
        for part_index, part in enumerate(split_long_text(section_header + section.content, max_chars), start=1):
            content_hash = sha256_text(part)
            chunk_id = f"{note.note_id}:{section_index}:{part_index}:{content_hash[:12]}"
            chunks.append(
                TextChunk(
                    chunk_id=chunk_id,
                    note_id=note.note_id,
                    title=note.title,
                    path=note.path,
                    heading=section.heading,
                    content=part,
                    content_hash=content_hash,
                    tags=note.tags,
                )
            )

    return chunks
