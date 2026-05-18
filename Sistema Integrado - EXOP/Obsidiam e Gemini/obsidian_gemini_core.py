from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import networkx as nx
from google import genai


DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_API_KEY = ""
DEFAULT_VAULT_PATH = Path(
    os.getenv(
        "OBSIDIAN_VAULT_PATH",
        "C:\\Users\\d.jose.dias\\OneDrive - AeC Centro de Contatos\\Documentos\\Obsidian Vault\\Excel\u00eancia Operacional",
    )
)
DEFAULT_CONTEXT_LIMIT = int(os.getenv("OBSIDIAN_CONTEXT_CHARS", "18000"))
DEFAULT_HISTORY_LIMIT = int(os.getenv("OBSIDIAN_HISTORY_LIMIT", "6"))
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "3500"))
DEFAULT_NOTE_EXCERPT_CHARS = int(os.getenv("OBSIDIAN_NOTE_EXCERPT_CHARS", "4500"))
DEFAULT_DETAILED_NOTE_EXCERPT_CHARS = int(os.getenv("OBSIDIAN_DETAILED_NOTE_EXCERPT_CHARS", "9000"))
DETAIL_REQUEST_TERMS = (
    "detalhe",
    "detalhar",
    "explica",
    "explique",
    "relatorio",
    "relatório",
    "regra",
    "criterio",
    "critério",
    "fluxo",
    "passo",
    "documente",
    "documentar",
    "resolva",
    "resolver",
    "resolucao",
    "resoluÃ§Ã£o",
    "corrija",
    "corrigir",
    "como fazer",
    "como faco",
    "como faÃ§o",
    "diagnostico",
    "diagnóstico",
    "aprofund",
)

API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_-]{35}")
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z\u00C0-\u00FF_]{3,}")
WIKILINK_PATTERN = re.compile(r"\[\[(.*?)\]\]")
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
RETRY_SECONDS_PATTERN = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
RETRY_DELAY_PATTERN = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?([0-9]+)s", re.IGNORECASE)


class GeminiQuotaError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def normalize_api_key(raw_value: str | None) -> str | None:
    if not raw_value:
        return None

    compact_value = "".join(raw_value.split())
    match = API_KEY_PATTERN.search(compact_value)
    if match:
        return match.group(0)

    return compact_value or None


def get_api_key(explicit_key: str | None = None) -> str | None:
    return (
        normalize_api_key(explicit_key)
        or normalize_api_key(os.getenv("GEMINI_API_KEY"))
        or normalize_api_key(os.getenv("GOOGLE_API_KEY"))
        or normalize_api_key(DEFAULT_API_KEY)
    )


def get_api_keys(explicit_key: str | None = None) -> list[str]:
    raw_keys = (
        explicit_key,
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
        os.getenv("GEMINI_API_KEY_3"),
        os.getenv("GOOGLE_API_KEY"),
        DEFAULT_API_KEY,
    )
    keys: list[str] = []
    seen: set[str] = set()

    for raw_key in raw_keys:
        key = normalize_api_key(raw_key)
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)

    return keys


def create_client(api_key: str | None = None) -> genai.Client:
    keys = get_api_keys(api_key)
    if not keys:
        raise ValueError(
            "Informe a chave do Gemini no app ou defina GEMINI_API_KEY/GOOGLE_API_KEY."
        )
    return genai.Client(api_key=keys[0])


def read_markdown_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    raise UnicodeDecodeError(
        "markdown_file",
        b"",
        0,
        1,
        f"Nao foi possivel decodificar o arquivo: {path}",
    )


def clean_link_name(link_text: str) -> str:
    base_link = link_text.split("|", 1)[0].split("#", 1)[0].strip()
    return Path(base_link).stem


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_PATTERN.findall(text)]


def load_vault(vault_path: str | Path) -> tuple[nx.Graph, dict[str, dict[str, Any]]]:
    vault = Path(vault_path)
    if not vault.exists():
        raise FileNotFoundError(f"Vault nao encontrado: {vault}")

    graph = nx.Graph()
    notes: dict[str, dict[str, Any]] = {}
    title_token_index: dict[str, set[str]] = defaultdict(set)
    content_token_index: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for path in sorted(vault.rglob("*.md")):
        try:
            content = read_markdown_file(path).strip()
        except Exception as exc:
            print(f"Erro ao ler {path.name}: {exc}")
            continue

        note_name = path.stem
        relative_path = path.relative_to(vault).as_posix()
        title_lower = note_name.casefold()
        content_lower = content.casefold()
        title_tokens = set(tokenize(note_name))
        content_token_counts = Counter(tokenize(content))
        notes[note_name] = {
            "name": note_name,
            "path": relative_path,
            "content": content,
            "title_lower": title_lower,
            "content_lower": content_lower,
            "title_tokens": title_tokens,
            "content_token_counts": content_token_counts,
        }
        for token in title_tokens:
            title_token_index[token].add(note_name)
        for token, count in content_token_counts.items():
            content_token_index[token].append((note_name, count))
        graph.add_node(note_name)

    for note_name, note_data in notes.items():
        for raw_link in WIKILINK_PATTERN.findall(note_data["content"]):
            linked_name = clean_link_name(raw_link)
            if linked_name in notes and linked_name != note_name:
                graph.add_edge(note_name, linked_name)

    graph.graph["title_token_index"] = dict(title_token_index)
    graph.graph["content_token_index"] = dict(content_token_index)

    return graph, notes


def strip_frontmatter(content: str) -> str:
    return FRONTMATTER_PATTERN.sub("", content).strip()


def iter_content_lines(content: str) -> Iterable[str]:
    cleaned = strip_frontmatter(content)
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("[[") and line.endswith("]]"):
            continue
        yield line


def split_markdown_sections(content: str) -> list[tuple[str, list[str]]]:
    cleaned = strip_frontmatter(content)
    sections: list[tuple[str, list[str]]] = []
    current_title = "introducao"
    current_lines: list[str] = []

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = line[3:].strip().casefold()
            current_lines = []
            continue

        if not line or line.startswith("#"):
            continue
        current_lines.append(line)

    if current_lines:
        sections.append((current_title, current_lines))

    return sections


def extract_relevant_lines(question: str, content: str, limit: int = 4) -> list[str]:
    question_tokens = set(tokenize(question))
    candidates: list[tuple[int, str]] = []

    for line in iter_content_lines(content):
        normalized_line = line.casefold()
        score = 0
        for token in question_tokens:
            if token in normalized_line:
                score += 3
        if "resumo" in normalized_line or "objetivo" in normalized_line:
            score += 1
        if "fluxo" in normalized_line or "rotina" in normalized_line:
            score += 1
        if score > 0:
            candidates.append((score, line))

    if not candidates:
        fallback_lines = list(iter_content_lines(content))
        return fallback_lines[:limit]

    ranked = sorted(candidates, key=lambda item: (item[0], len(item[1])), reverse=True)
    selected: list[str] = []
    seen: set[str] = set()
    for _, line in ranked:
        if line in seen:
            continue
        selected.append(line)
        seen.add(line)
        if len(selected) >= limit:
            break
    return selected


def is_detailed_request(question: str) -> bool:
    normalized_question = question.casefold()
    return any(term in normalized_question for term in DETAIL_REQUEST_TERMS)


def is_ticket_responsible_question(question: str) -> bool:
    normalized_question = question.casefold()
    has_ticket_term = any(term in normalized_question for term in ("chamado", "chamados", "ticket", "solicitacao", "solicitação"))
    has_owner_term = any(term in normalized_question for term in ("responsavel", "responsável", "analista", "atendeu", "tratou", "dono"))
    has_area_term = any(term in normalized_question for term in ("excelencia operacional", "excelência operacional", "usuario_responsavel"))
    return has_ticket_term and (has_owner_term or has_area_term)


def build_relevant_excerpt(question: str, content: str, max_chars: int) -> str:
    cleaned = strip_frontmatter(content)
    if len(cleaned) <= max_chars:
        return cleaned

    if is_detailed_request(question) or is_ticket_responsible_question(question):
        return cleaned[:max_chars].strip() + "\n[conteudo truncado por limite de contexto]"

    question_tokens = set(tokenize(question))
    lines = [(index, line.strip()) for index, line in enumerate(cleaned.splitlines()) if line.strip()]
    if not lines:
        return cleaned[:max_chars].strip()

    scored_lines: list[tuple[int, int]] = []
    for index, line in lines:
        normalized_line = line.casefold()
        score = 0
        if line.startswith("#"):
            score += 2
        for token in question_tokens:
            if token in normalized_line:
                score += 4
        if any(term in normalized_line for term in ("objetivo", "resumo", "regra", "criterio", "filtro")):
            score += 2
        if any(term in normalized_line for term in ("fluxo", "rotina", "validacao", "relatorio", "automacao")):
            score += 1
        if is_ticket_responsible_question(question) and any(
            term in normalized_line
            for term in (
                "usuario_responsavel",
                "analista responsavel",
                "analista responsável",
                "isadora",
                "gabriel",
                "alluska",
                "jose johan",
                "danilo",
                "gustavo",
            )
        ):
            score += 8
        if score:
            scored_lines.append((score, index))

    if not scored_lines:
        return cleaned[:max_chars].strip() + "\n[conteudo truncado por limite de contexto]"

    selected_indexes: set[int] = set()
    for _, index in sorted(scored_lines, reverse=True)[:20]:
        selected_indexes.update({index - 3, index - 2, index - 1, index, index + 1, index + 2, index + 3})

    excerpt_lines: list[str] = []
    current_size = 0
    for index, line in lines:
        if index not in selected_indexes:
            continue
        line_with_break = line + "\n"
        if current_size + len(line_with_break) > max_chars:
            break
        excerpt_lines.append(line)
        current_size += len(line_with_break)

    if not excerpt_lines:
        return cleaned[:max_chars].strip() + "\n[conteudo truncado por limite de contexto]"

    return "\n".join(excerpt_lines).strip() + "\n[trechos relevantes selecionados]"


def clean_answer_line(line: str) -> str:
    line = re.sub(r"`+", "", line)
    line = re.sub(r"\[\[(.*?)\]\]", r"\1", line)
    line = re.sub(r"^\s*[-*]\s*", "", line)
    line = re.sub(r"^\s*\d+\.\s*", "", line)
    return line.strip().rstrip(":").strip()


def is_useful_answer_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.endswith(":") and len(stripped) < 90:
        return False
    if stripped.casefold().endswith((" com", " para", " de")):
        return False
    if " " not in stripped and len(stripped) < 40:
        return False
    if stripped.casefold() in {
        "peop",
        "sharepoint",
        "google gemini",
        "python",
        "sqlalchemy",
    }:
        return False
    if stripped.startswith("[[") and stripped.endswith("]]"):
        return False
    return True


def build_local_fallback_answer(
    question: str,
    selected_notes: list[str],
    notes: dict[str, dict[str, Any]],
) -> str:
    if not selected_notes:
        return (
            "Nao consegui consultar o Gemini agora e tambem nao encontrei contexto suficiente "
            "para montar uma resposta local."
        )

    primary_note = selected_notes[0]
    primary_content = notes[primary_note]["content"]
    sections = split_markdown_sections(primary_content)
    preferred_section_names = (
        "resumo",
        "objetivo",
        "fluxo",
        "o que a analise faz",
        "leitura operacional",
        "saidas esperadas",
    )

    preferred_lines: list[str] = []
    for section_name, section_lines in sections:
        if section_name in preferred_section_names:
            preferred_lines.extend(section_lines[:3])

    source_lines = preferred_lines if preferred_lines else extract_relevant_lines(question, primary_content)
    relevant_lines = [clean_answer_line(line) for line in source_lines]
    relevant_lines = [line for line in relevant_lines if is_useful_answer_line(line)]

    if not relevant_lines:
        return (
            "Nao consegui consultar o Gemini agora. Pelo contexto local, existe informacao relacionada, "
            "mas ela nao estava clara o suficiente para eu resumir com seguranca."
        )

    if len(relevant_lines) == 1:
        return relevant_lines[0]

    if len(relevant_lines) == 2:
        return f"{relevant_lines[0]} {relevant_lines[1]}"

    return f"{relevant_lines[0]} {relevant_lines[1]} {relevant_lines[2]}"


def extract_responsible_people_from_note(content: str) -> list[tuple[str, str]]:
    people: list[tuple[str, str]] = []
    in_section = False

    for raw_line in strip_frontmatter(content).splitlines():
        line = raw_line.strip()
        normalized_line = line.casefold()

        if normalized_line.startswith("## analistas respons"):
            in_section = True
            continue

        if in_section and line.startswith("## "):
            break

        if not in_section or not line.startswith("|"):
            continue

        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) < 2:
            continue

        name, quantity = columns[0], columns[1]
        if (
            not name
            or name.casefold().startswith("analista")
            or set(name) <= {"-", ":"}
            or set(quantity) <= {"-", ":"}
        ):
            continue

        people.append((name, quantity))

    return people


def build_ticket_responsible_answer(
    question: str,
    selected_notes: list[str],
    notes: dict[str, dict[str, Any]],
) -> str | None:
    if not is_ticket_responsible_question(question):
        return None

    note_name = next(
        (
            name
            for name in (
                "Responsáveis por Chamado - Excelência Operacional",
                "Responsaveis por Chamado - Excelencia Operacional",
                *selected_notes,
            )
            if name in notes
        ),
        None,
    )
    if not note_name:
        return None

    people = extract_responsible_people_from_note(notes[note_name]["content"])
    if not people:
        return None

    lines = [
        "Para resolver essa demanda, use o campo `USUARIO_RESPONSAVEL` como referência do analista responsável pela tratativa de cada chamado.",
        "",
        "A leitura correta é: `codigo` identifica o chamado, `SERVICO` identifica o fluxo/fila, `GRUPO` identifica a frente macro e `USUARIO_RESPONSAVEL` identifica quem tratou, herdou ou resolveu a solicitação.",
        "",
        "| Analista responsável | Qtde observada |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {quantity} |" for name, quantity in people)
    lines.extend(
        [
            "",
            "Como documentar chamado a chamado:",
            "",
            "1. Agrupe a base por `codigo`.",
            "2. Traga `SERVICO` para indicar o tipo de chamado/fluxo.",
            "3. Traga `GRUPO` para indicar a frente de atendimento.",
            "4. Use `USUARIO_RESPONSAVEL` como analista responsável.",
            "5. Se o mesmo `codigo` tiver mais de um responsável, registre todos e marque como repasse/troca interna.",
            "6. Se `USUARIO_RESPONSAVEL` vier vazio ou `NULL`, marque como sem responsável identificado na extração.",
            "",
            "Modelo final recomendado:",
            "",
            "| codigo | SERVICO | GRUPO | analistas_responsaveis | leitura |",
            "|---|---|---|---|---|",
            "| 000000 | fluxo do chamado | grupo de atendimento | Nome do analista | único responsável |",
            "| 000001 | fluxo do chamado | grupo de atendimento | Nome 1; Nome 2 | repasse/troca interna |",
            "| 000002 | fluxo do chamado | grupo de atendimento | não identificado | responsável vazio/NULL |",
            "",
            "Consulta base para gerar essa visão:",
            "",
            "```sql",
            "with base as (",
            "    select distinct",
            "        CODIGO,",
            "        GRUPO,",
            "        SERVICO,",
            "        nullif(USUARIO_RESPONSAVEL, 'NULL') as USUARIO_RESPONSAVEL",
            "    from Robbyson.dbo.tGestaoX_Chamados_VerticalRobbyson",
            "    where GRUPO like '%Excelência Operacional%'",
            "       or GRUPO like '%Robbyson Vertical%'",
            "),",
            "responsaveis as (",
            "    select distinct CODIGO, USUARIO_RESPONSAVEL",
            "    from base",
            "    where USUARIO_RESPONSAVEL is not null",
            ")",
            "select",
            "    b.CODIGO as codigo,",
            "    max(b.SERVICO) as SERVICO,",
            "    max(b.GRUPO) as GRUPO,",
            "    string_agg(r.USUARIO_RESPONSAVEL, '; ') as analistas_responsaveis,",
            "    case",
            "        when count(r.USUARIO_RESPONSAVEL) = 0 then 'sem responsável identificado'",
            "        when count(r.USUARIO_RESPONSAVEL) = 1 then 'único responsável'",
            "        else 'repasse/troca interna'",
            "    end as leitura",
            "from base b",
            "left join responsaveis r on r.CODIGO = b.CODIGO",
            "group by b.CODIGO;",
            "```",
        ]
    )
    return "\n".join(lines)


def extract_retry_after_seconds(message: str) -> int | None:
    for pattern in (RETRY_SECONDS_PATTERN, RETRY_DELAY_PATTERN):
        match = pattern.search(message)
        if match:
            try:
                return max(1, int(float(match.group(1))))
            except ValueError:
                return None
    return None


def should_try_next_api_key(exc: Exception) -> bool:
    message = str(exc).casefold()
    retryable_terms = (
        "resource_exhausted",
        "quota",
        "rate limit",
        "429",
        "temporarily",
        "unavailable",
        "deadline",
        "timeout",
        "500",
        "502",
        "503",
        "504",
    )
    return any(term in message for term in retryable_terms)


def score_notes(
    question: str,
    notes: dict[str, dict[str, Any]],
    graph: nx.Graph,
    preferred_note: str | None = None,
) -> Counter[str]:
    scores: Counter[str] = Counter()
    question_lower = question.casefold()
    tokens = tuple(dict.fromkeys(tokenize(question)))
    title_token_index: dict[str, set[str]] = graph.graph.get("title_token_index", {})
    content_token_index: dict[str, list[tuple[str, int]]] = graph.graph.get("content_token_index", {})

    if title_token_index and content_token_index and tokens:
        for token in tokens:
            for note_name in title_token_index.get(token, set()):
                scores[note_name] += 6
            for note_name, count_in_content in content_token_index.get(token, []):
                scores[note_name] += min(count_in_content, 5)

        candidate_notes = set(scores)
        if question_lower:
            for note_name in list(candidate_notes):
                note_data = notes[note_name]
                if question_lower in note_data["title_lower"]:
                    scores[note_name] += 12
                if question_lower in note_data["content_lower"]:
                    scores[note_name] += 10
    else:
        for note_name, note_data in notes.items():
            title_lower = note_data["title_lower"]
            content_lower = note_data["content_lower"]
            title_tokens = note_data.get("title_tokens") or set(tokenize(title_lower))
            content_token_counts = note_data.get("content_token_counts") or Counter(tokenize(content_lower))

            note_score = 0
            if question_lower and question_lower in title_lower:
                note_score += 12

            for token in tokens:
                if token in title_tokens:
                    note_score += 6
                count_in_content = content_token_counts.get(token, 0)
                if count_in_content:
                    note_score += min(count_in_content, 5)

            if note_score and question_lower and question_lower in content_lower:
                note_score += 10

            if note_score:
                scores[note_name] = note_score

    if not tokens and question_lower:
        for note_name, note_data in notes.items():
            if question_lower in note_data["title_lower"]:
                scores[note_name] += 12
            if question_lower in note_data["content_lower"]:
                scores[note_name] += 10

    if preferred_note and preferred_note in notes:
        scores[preferred_note] += 20
        for neighbor in graph.neighbors(preferred_note):
            scores[neighbor] += 5

    if scores:
        top_seed = scores.most_common(1)[0][0]
        for neighbor in graph.neighbors(top_seed):
            scores[neighbor] += 2

    return scores


def select_relevant_notes(
    question: str,
    notes: dict[str, dict[str, Any]],
    graph: nx.Graph,
    preferred_note: str | None = None,
    limit: int = 4,
) -> list[str]:
    if not notes:
        return []

    scores = score_notes(question, notes, graph, preferred_note)

    if is_ticket_responsible_question(question):
        priority_notes = (
            "Responsáveis por Chamado - Excelência Operacional",
            "Responsaveis por Chamado - Excelencia Operacional",
            "Base de Chamados - Excelência Operacional",
            "Base de Chamados - Excelencia Operacional",
        )
        for note_name in priority_notes:
            if note_name in notes:
                scores[note_name] += 200

    selected = [note_name for note_name, score in scores.most_common(limit) if score > 0]

    if preferred_note and preferred_note in notes and preferred_note not in selected:
        selected.insert(0, preferred_note)

    if not selected:
        ranked_by_degree = sorted(
            notes,
            key=lambda name: (graph.degree(name), name.casefold()),
            reverse=True,
        )
        selected = ranked_by_degree[:limit]

    return selected[:limit]


def build_context(
    selected_notes: list[str],
    notes: dict[str, dict[str, Any]],
    graph: nx.Graph,
    max_chars: int = DEFAULT_CONTEXT_LIMIT,
    question: str = "",
) -> str:
    sections: list[str] = []
    current_size = 0

    for note_name in selected_notes:
        note_data = notes[note_name]
        neighbors = ", ".join(sorted(graph.neighbors(note_name))) or "nenhum"
        header = (
            f"## Nota: {note_name}\n"
            f"Caminho: {note_data['path']}\n"
            f"Links diretos: {neighbors}\n\n"
        )
        available = max_chars - current_size - len(header)
        if available <= 0:
            break

        if is_ticket_responsible_question(question):
            excerpt_limit = 12000
        else:
            excerpt_limit = (
                DEFAULT_DETAILED_NOTE_EXCERPT_CHARS
                if is_detailed_request(question)
                else DEFAULT_NOTE_EXCERPT_CHARS
            )
        content = build_relevant_excerpt(
            question=question,
            content=note_data["content"],
            max_chars=min(excerpt_limit, max(available - 32, 0)),
        )
        if len(content) > available:
            content = content[: max(available - 32, 0)] + "\n[conteudo truncado]"

        section = header + content
        sections.append(section)
        current_size += len(section) + 2

    return "\n\n".join(sections)


def ask_gemini(
    question: str,
    context: str,
    conversation_history: list[dict[str, str]] | None = None,
    api_key: str | None = None,
    model_name: str = DEFAULT_MODEL,
) -> str:
    api_keys = get_api_keys(api_key)
    if not api_keys:
        raise ValueError(
            "Informe a chave do Gemini no app ou defina GEMINI_API_KEY/GOOGLE_API_KEY."
        )

    history_lines: list[str] = []
    for item in (conversation_history or [])[-DEFAULT_HISTORY_LIMIT:]:
        role = "Usuario" if item.get("role") == "user" else "Assistente"
        content = item.get("content", "").strip()
        if content:
            history_lines.append(f"{role}: {content}")

    history_text = "\n".join(history_lines) if history_lines else "Sem historico anterior."
    prompt = f"""Voce e um assistente de IA da Excelencia Operacional.

Sua funcao:
- responder perguntas com base no conhecimento operacional disponivel
- ajudar em duvidas sobre relatorios, automacoes, rotinas, regras de negocio e sistemas de acompanhamento
- falar como alguem que conhece o ambiente e explica de forma natural, direta e util

Regras obrigatorias:
- responda em portugues do Brasil
- nao fale sobre notas, arquivos, markdown, vault, contexto ou fontes
- nao diga "segundo a nota", "a nota mostra", "no arquivo", ou similares
- evite citar nomes tecnicos internos, como tabelas, views, schemas, scripts ou identificadores, a menos que o usuario peça isso explicitamente
- prefira explicar o processo operacional por tras da informacao
- quando houver informacao suficiente, responda de forma assertiva e objetiva
- quando a informacao estiver incompleta, diga isso de forma simples e sem inventar detalhes
- se fizer sentido, cite frequencia, objetivo, fluxo ou horario da automacao
- por padrao, responda de forma direta, mas completa o bastante para a pessoa conseguir agir
- so gere respostas detalhadas quando o usuario pedir explicacao, relatorio, passo a passo, regra de negocio, diagnostico, aprofundamento ou detalhes
- em respostas detalhadas, organize a explicacao em objetivo, regra de negocio, fluxo operacional, excecoes/cuidados e impacto para acompanhamento
- quando o usuario pedir para documentar, corrigir, resolver ou apoiar uma decisao operacional, entregue uma resposta acionavel: explique o que usar, a regra, o passo a passo, excecoes e o resultado esperado
- nao esconda nomes, campos, tabelas ou consultas quando eles forem necessarios para resolver a pergunta
- para relatorios, quando o usuario pedir detalhe, de enfase na regra de negocio: criterios de entrada, filtros, periodicidade, origem da informacao, transformacoes, validacoes e como o resultado deve ser interpretado
- nao alongue respostas simples apenas porque existe contexto adicional disponivel, mas tambem nao resuma demais quando a pergunta exigir resolucao pratica
- quando a pergunta envolver atas, auditorias ou tabulacoes, explique a finalidade pratica da rotina, como captura, consolidacao, atualizacao e avaliacao de qualidade
- quando existir sequencia operacional, descreva no formato processo: origem, tabulacao, atualizacao e onde a informacao aparece no dia seguinte
- exemplo de tom esperado: "As atas sao tabuladas no SharePoint para avaliar a qualidade. No dia seguinte, essas informacoes sobem no relatorio do PEOP."

Historico recente:
{history_text}

Pergunta atual:
{question}

Base de conhecimento disponivel:
{context}
"""
    config = {
        "temperature": 0.2,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    }

    last_exception: Exception | None = None
    active_api_key = api_keys[0]
    response = None
    for position, current_api_key in enumerate(api_keys, start=1):
        client = genai.Client(api_key=current_api_key)
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )
            active_api_key = current_api_key
            break
        except Exception as exc:
            last_exception = exc
            is_last_key = position == len(api_keys)
            if should_try_next_api_key(exc) and not is_last_key:
                continue

            message = str(exc)
            if "RESOURCE_EXHAUSTED" in message or "quota" in message.casefold():
                retry_after_seconds = extract_retry_after_seconds(message)
                raise GeminiQuotaError(message, retry_after_seconds=retry_after_seconds) from exc
            raise

    if response is None:
        message = str(last_exception) if last_exception else "Nenhuma chave do Gemini respondeu."
        retry_after_seconds = extract_retry_after_seconds(message)
        raise GeminiQuotaError(message, retry_after_seconds=retry_after_seconds) from last_exception

    response_text = getattr(response, "text", "")
    if response_text:
        final_text = response_text.strip()
        finish_reason = ""
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                finish_reason = str(getattr(candidates[0], "finish_reason", "") or "")
        except Exception:
            finish_reason = ""

        if finish_reason and "MAX_TOKENS" in finish_reason.upper():
            client = genai.Client(api_key=active_api_key)
            continuation_prompt = f"""Continue exatamente de onde parou, sem reiniciar a resposta e sem repetir trechos.

Pergunta original:
{question}

Trecho ja gerado:
{final_text}
"""
            continuation_response = client.models.generate_content(
                model=model_name,
                contents=continuation_prompt,
                config=config,
            )
            continuation_text = getattr(continuation_response, "text", "").strip()
            if continuation_text:
                final_text = f"{final_text} {continuation_text}".strip()

        return final_text
    raise RuntimeError("O Gemini retornou uma resposta vazia.")


def collect_documents(vault_path: str | Path) -> list[str]:
    vault = Path(vault_path)
    if not vault.exists():
        raise FileNotFoundError(f"Vault nao encontrado: {vault}")

    documents: list[str] = []
    for path in sorted(vault.rglob("*.md")):
        try:
            content = read_markdown_file(path).strip()
        except Exception as exc:
            print(f"Erro ao ler {path.name}: {exc}")
            continue

        relative_path = path.relative_to(vault).as_posix()
        documents.append(f"### ARQUIVO: {relative_path}\n{content}")

    return documents
