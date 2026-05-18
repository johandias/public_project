from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


WIKILINK_PATTERN = re.compile(r"(?<!!)\[\[([^\[\]]+?)\]\]")


@dataclass(frozen=True)
class WikiLink:
    raw: str
    target: str
    note_name: str
    section: str | None
    alias: str | None


def normalize_link_target(value: str) -> str:
    cleaned = value.replace("\\", "/").rsplit("/", 1)[-1]
    return Path(cleaned).stem.strip().casefold()


def parse_wikilink(raw_value: str) -> WikiLink | None:
    raw_value = raw_value.strip()
    if not raw_value:
        return None

    target_part, alias = split_alias(raw_value)
    note_part, section = split_section(target_part)
    note_name = normalize_link_target(note_part)
    if not note_name:
        return None

    return WikiLink(
        raw=f"[[{raw_value}]]",
        target=target_part.strip(),
        note_name=note_name,
        section=section,
        alias=alias,
    )


def split_alias(raw_value: str) -> tuple[str, str | None]:
    if "|" not in raw_value:
        return raw_value, None

    target, alias = raw_value.split("|", 1)
    alias = alias.strip()
    return target, alias or None


def split_section(target: str) -> tuple[str, str | None]:
    if "#" not in target:
        return target.strip(), None

    note_name, section = target.split("#", 1)
    section = section.strip()
    return note_name.strip(), section or None


def parse_wikilinks(content: str) -> list[WikiLink]:
    links: list[WikiLink] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for match in WIKILINK_PATTERN.finditer(content):
        parsed = parse_wikilink(match.group(1))
        if not parsed:
            continue

        key = (parsed.note_name, parsed.section, parsed.alias)
        if key in seen:
            continue
        seen.add(key)
        links.append(parsed)

    return links
