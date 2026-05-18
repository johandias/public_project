from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass

from google import genai

from obsidian_gemini_core import DEFAULT_MAX_OUTPUT_TOKENS, create_client


CHUNK_PROMPT = """Voce vai analisar uma parte de um vault do Obsidian.

Use o contexto relacional entre notas para entender dependencias, origem das informacoes,
conexoes operacionais e temas recorrentes.

Responda com:
- temas principais
- padroes recorrentes
- relacoes importantes entre notas
- regras de negocio identificadas, incluindo criterios, filtros, excecoes e dependencias operacionais
- gargalos operacionais
- oportunidades de melhoria
- sugestoes para futura busca RAG

Gere uma resposta detalhada o suficiente para preservar a logica de negocio. Use bullets, mas explique
o que cada rotina controla, de onde vem a informacao, quais criterios parecem reger o processo e como
isso impacta relatorios, automacoes ou acompanhamento operacional.

Contexto estruturado:
{contexto}
"""

FINAL_PROMPT = """Com base nos resumos parciais abaixo, gere uma analise consolidada.

Inclua:
1. resumo executivo
2. mapa dos temas principais
3. relacoes importantes entre notas
4. regras de negocio dos relatorios, automacoes e rotinas identificadas
5. origem, transformacao, validacao e consumo das informacoes nos relatorios
6. gargalos encontrados
7. sugestoes praticas de melhoria operacional
8. prioridades recomendadas para os proximos passos
9. recomendacoes para evoluir para RAG/chatbot online

De enfase especial as regras de negocio: explique criterios de entrada, filtros, periodicidade,
dependencias entre sistemas, excecoes conhecidas, pontos de validacao e como cada resultado deve ser
interpretado pela operacao. Gere um texto mais completo, com detalhes suficientes para apoiar manutencao
de relatorios, automacoes e documentacao operacional.

Resumos parciais:
{resumos}
"""


@dataclass(frozen=True)
class GeminiSettings:
    model_name: str
    max_retries: int = 4
    retry_base_seconds: float = 2.0
    request_timeout_seconds: float = 120.0
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


class GeminiAnalyzer:
    def __init__(
        self,
        settings: GeminiSettings,
        logger: logging.Logger,
        client: genai.Client | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.client = client or create_client()

    def summarize_context(self, context: str) -> str:
        return self.generate_text(CHUNK_PROMPT.format(contexto=context))

    def generate_final_report(self, partial_summaries: list[str]) -> str:
        return self.generate_text(FINAL_PROMPT.format(resumos="\n\n".join(partial_summaries)))

    def generate_text(self, prompt: str) -> str:
        last_exception: Exception | None = None

        for attempt in range(1, self.settings.max_retries + 1):
            try:
                response = self._generate_content_with_timeout(prompt)
                text = getattr(response, "text", "")
                if not text:
                    raise RuntimeError("A API retornou uma resposta vazia.")
                return text.strip()
            except Exception as exc:
                last_exception = exc
                if not is_temporary_error(exc) or attempt == self.settings.max_retries:
                    self.logger.exception("Falha definitiva ao chamar Gemini na tentativa %s.", attempt)
                    break

                wait_seconds = self.settings.retry_base_seconds * (2 ** (attempt - 1))
                wait_seconds += random.uniform(0, 0.75)
                self.logger.warning(
                    "Erro temporario na chamada Gemini. Tentativa %s/%s. Nova tentativa em %.1fs. Erro: %s",
                    attempt,
                    self.settings.max_retries,
                    wait_seconds,
                    exc,
                )
                time.sleep(wait_seconds)

        raise RuntimeError("Nao foi possivel obter resposta do Gemini.") from last_exception

    def _generate_content_with_timeout(self, prompt: str) -> object:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.client.models.generate_content,
                model=self.settings.model_name,
                contents=prompt,
                config={
                    "temperature": 0.2,
                    "max_output_tokens": self.settings.max_output_tokens,
                },
            )
            try:
                return future.result(timeout=self.settings.request_timeout_seconds)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Timeout apos {self.settings.request_timeout_seconds:.0f}s na chamada Gemini."
                ) from exc


def is_temporary_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True

    message = str(exc).casefold()
    temporary_terms = (
        "timeout",
        "temporarily",
        "unavailable",
        "deadline",
        "rate limit",
        "resource exhausted",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(term in message for term in temporary_terms)
