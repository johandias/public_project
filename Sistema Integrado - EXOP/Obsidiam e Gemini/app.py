from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter, sleep
from datetime import datetime, timezone
from uuid import uuid4

import streamlit as st

from obsidian_gemini_core import (
    DEFAULT_MODEL,
    DEFAULT_VAULT_PATH,
    GeminiQuotaError,
    ask_gemini,
    build_local_fallback_answer,
    build_context,
    build_ticket_responsible_answer,
    get_api_key,
    load_vault,
    select_relevant_notes,
)


DEFAULT_MAX_NOTES = 6
VAULT_CACHE_VERSION = "2026-05-15-respostas-resolutivas-v4"
VAULT_REFRESH_SECONDS = 60
MAX_ANSWER_CACHE_ITEMS = 80
USER_AVATAR = ":material/person:"
ASSISTANT_AVATAR = ":material/smart_toy:"
CHAT_STORE_PATH = Path(__file__).with_name("chat_conversas.json")
ACCESS_CODES_PATH = Path(__file__).with_name("usuarios_acesso.json")
ACCESS_CODES_ENV = "USUARIOS_ACESSO_JSON"
FAVICON_PATH = Path(__file__).with_name("assets").joinpath("favicon-robot.png")
ADMIN_PASSWORD = "admin123"
ACTIVE_USERS: dict[str, dict[str, str]] = {}
ACTIVE_USERS_LOCK = Lock()


st.set_page_config(
    page_title="Conhecimento Operacional",
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.exists() else "🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


class VaultMemoryCache:
    def __init__(self, vault_path: str, refresh_seconds: int) -> None:
        self.vault_path = vault_path
        self.refresh_seconds = refresh_seconds
        self._lock = Lock()
        self._graph = None
        self._notes = None
        self._last_error: Exception | None = None
        self._refresh()

        thread = Thread(target=self._refresh_loop, daemon=True)
        thread.start()

    def _refresh(self) -> None:
        try:
            graph, notes = load_vault(self.vault_path)
        except Exception as exc:
            with self._lock:
                self._last_error = exc
            return

        with self._lock:
            self._graph = graph
            self._notes = notes
            self._last_error = None

    def _refresh_loop(self) -> None:
        while True:
            sleep(self.refresh_seconds)
            self._refresh()

    def get(self):
        with self._lock:
            graph = self._graph
            notes = self._notes
            last_error = self._last_error

        if graph is None or notes is None:
            if last_error:
                raise last_error
            raise RuntimeError("Base de conhecimento ainda nao foi carregada.")

        return graph, notes


@st.cache_resource(show_spinner=False)
def carregar_vault(vault_path: str, cache_version: str, refresh_seconds: int):
    _ = cache_version
    return VaultMemoryCache(vault_path, refresh_seconds)


def inicializar_estado() -> None:
    st.session_state.setdefault("session_id", str(uuid4()))
    st.session_state.setdefault("user_name", "")
    st.session_state.setdefault("active_conversation_id", "")
    st.session_state.setdefault("answer_cache", {})
    st.session_state.setdefault("admin_unlocked", False)
    st.session_state.setdefault("sidebar_collapsed", False)


def build_answer_cache_key(question: str, context: str, model_name: str) -> str:
    payload = f"{model_name}\n{question.strip().casefold()}\n{context}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_answer_cache(cache_key: str, answer: str) -> None:
    answer_cache = st.session_state.answer_cache
    answer_cache[cache_key] = answer
    while len(answer_cache) > MAX_ANSWER_CACHE_ITEMS:
        oldest_key = next(iter(answer_cache))
        del answer_cache[oldest_key]


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def carregar_conversas() -> list[dict]:
    if not CHAT_STORE_PATH.exists():
        return []

    try:
        data = json.loads(CHAT_STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []
    return data


def salvar_conversas(conversas: list[dict]) -> None:
    CHAT_STORE_PATH.write_text(
        json.dumps(conversas, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def titulo_da_conversa(pergunta: str) -> str:
    titulo = " ".join(pergunta.strip().split())
    if not titulo:
        return "Nova conversa"
    return titulo[:58] + ("..." if len(titulo) > 58 else "")


def criar_conversa(usuario: str) -> dict:
    conversa = {
        "id": str(uuid4()),
        "titulo": "Nova conversa",
        "usuario": usuario,
        "criada_em": agora_iso(),
        "atualizada_em": agora_iso(),
        "mensagens": [],
    }
    conversas = carregar_conversas()
    conversas.insert(0, conversa)
    salvar_conversas(conversas)
    st.session_state.active_conversation_id = conversa["id"]
    return conversa


def obter_conversa_ativa(usuario: str) -> dict:
    conversas = carregar_conversas()
    active_id = st.session_state.active_conversation_id
    conversa = next((item for item in conversas if item.get("id") == active_id), None)
    if conversa is None:
        conversa = criar_conversa(usuario)
    return conversa


def atualizar_conversa(conversa_atualizada: dict) -> None:
    conversas = carregar_conversas()
    restantes = [item for item in conversas if item.get("id") != conversa_atualizada.get("id")]
    conversa_atualizada["atualizada_em"] = agora_iso()
    salvar_conversas([conversa_atualizada, *restantes])
    st.session_state.active_conversation_id = conversa_atualizada["id"]


def excluir_conversa(conversation_id: str) -> None:
    conversas = [item for item in carregar_conversas() if item.get("id") != conversation_id]
    salvar_conversas(conversas)
    st.session_state.active_conversation_id = conversas[0]["id"] if conversas else ""


def registrar_usuario_online(usuario: str) -> None:
    if not usuario:
        return

    agora = datetime.now(timezone.utc)
    with ACTIVE_USERS_LOCK:
        expirados = [
            session_id
            for session_id, item in ACTIVE_USERS.items()
            if (agora - datetime.fromisoformat(item["visto_em"])).total_seconds() > 120
        ]
        for session_id in expirados:
            del ACTIVE_USERS[session_id]

        ACTIVE_USERS[st.session_state.session_id] = {
            "nome": usuario,
            "visto_em": agora.isoformat(),
        }
        nomes = sorted({item["nome"] for item in ACTIVE_USERS.values()})

    try:
        nomes_ascii = [nome.encode("ascii", errors="ignore").decode("ascii") for nome in nomes]
        print(f"[online users] {len(nomes_ascii)} user(s): {', '.join(nomes_ascii)}", flush=True)
    except Exception:
        pass


def usuarios_online() -> list[dict[str, str]]:
    with ACTIVE_USERS_LOCK:
        return list(ACTIVE_USERS.values())


def sair_usuario() -> None:
    st.session_state.user_name = ""
    st.session_state.active_conversation_id = ""
    st.session_state.admin_unlocked = False
    st.rerun()


def formatar_data(valor: str) -> str:
    try:
        data = datetime.fromisoformat(valor)
    except ValueError:
        return ""
    return data.astimezone().strftime("%d/%m %H:%M")


def grupo_data_conversa(valor: str) -> str:
    try:
        data = datetime.fromisoformat(valor).astimezone().date()
    except ValueError:
        return "Anteriores"

    hoje = datetime.now().astimezone().date()
    delta = (hoje - data).days
    if delta <= 0:
        return "Hoje"
    if delta == 1:
        return "Ontem"
    if delta <= 7:
        return "Ultimos 7 dias"
    return "Anteriores"


def normalizar_matricula(valor: str) -> str:
    return valor.strip()


def normalizar_codigo_acesso(valor: str) -> str:
    return "".join(char for char in valor if char.isdigit())


@st.cache_data(show_spinner=False)
def carregar_usuarios_acesso(caminho_json: str, usuarios_json: str = "") -> dict[str, str]:
    if usuarios_json.strip():
        try:
            dados = json.loads(usuarios_json)
        except json.JSONDecodeError:
            return {}
        return normalizar_usuarios_acesso(dados)

    caminho = Path(caminho_json)
    if not caminho.exists():
        return {}

    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    return normalizar_usuarios_acesso(dados)


def normalizar_usuarios_acesso(dados) -> dict[str, str]:
    if isinstance(dados, dict):
        dados = dados.get("usuarios", [])
    if not isinstance(dados, list):
        return {}

    usuarios = {}
    for item in dados:
        if not isinstance(item, dict):
            continue
        matricula = normalizar_matricula(str(item.get("matricula", "")))
        codigo = normalizar_codigo_acesso(str(item.get("codigo", "")))
        if matricula and codigo:
            usuarios[matricula] = codigo
    return usuarios


def senha_mensal(codigo_base: str, mes: int | None = None) -> str:
    codigo = normalizar_codigo_acesso(codigo_base)
    if not codigo:
        return ""
    mes_atual = mes or datetime.now().astimezone().month
    codigo_gerado = int(codigo) * 5 * mes_atual
    return str(codigo_gerado)[:4]


def validar_login_matricula(matricula: str, senha: str) -> bool:
    usuarios = carregar_usuarios_acesso(str(ACCESS_CODES_PATH), os.getenv(ACCESS_CODES_ENV, ""))
    codigo_base = usuarios.get(matricula)
    if not codigo_base:
        return False
    return senha == senha_mensal(codigo_base)


def render_login() -> None:
    st.markdown(
        """
        <div class="login-shell">
            <div class="login-card">
                <div class="hero-badge">Acesso interno</div>
                <div class="hero-title">Entre com sua matrícula.</div>
                <div class="hero-copy">
                    Informe sua matrícula e a senha mensal para usar o EXOP AI.
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("login_matricula", clear_on_submit=False):
        matricula = st.text_input("Matrícula", placeholder="Digite sua matrícula")
        senha = st.text_input(
            "Senha mensal",
            placeholder="Digite a senha de 4 dígitos",
            type="password",
            max_chars=4,
        )
        entrar = st.form_submit_button("Entrar")

    if not entrar:
        return

    matricula_normalizada = normalizar_matricula(matricula)
    senha_normalizada = normalizar_codigo_acesso(senha)
    if not matricula_normalizada or len(senha_normalizada) != 4:
        st.error("Informe a matrícula e a senha mensal com 4 dígitos.")
        return

    usuarios = carregar_usuarios_acesso(str(ACCESS_CODES_PATH), os.getenv(ACCESS_CODES_ENV, ""))
    if not usuarios:
        st.error("Arquivo de acessos não encontrado ou inválido.")
        return

    acesso_liberado = validar_login_matricula(matricula_normalizada, senha_normalizada)
    if not acesso_liberado:
        st.error("Matrícula ou senha mensal inválida.")
        return

    st.session_state.user_name = matricula_normalizada
    st.session_state.active_conversation_id = ""
    registrar_usuario_online(st.session_state.user_name)
    st.rerun()


def aplicar_estilo(sidebar_collapsed: bool) -> None:
    _ = sidebar_collapsed
    sidebar_width = "24.375rem"
    sidebar_open_width = "24.375rem"
    sidebar_display = "block"
    sidebar_visibility_css = ""

    st.markdown(
        f"""
        <style>
        :root {{
            --bg: #07080d;
            --bg-deep: #05060a;
            --panel: rgba(255, 255, 255, 0.055);
            --panel-2: rgba(255, 255, 255, 0.08);
            --panel-3: rgba(255, 255, 255, 0.035);
            --border: rgba(255, 255, 255, 0.10);
            --border-strong: rgba(255, 255, 255, 0.16);
            --text: #f4f4f5;
            --muted: #a1a1aa;
            --muted-2: #71717a;
            --accent-a: #8b5cf6;
            --accent-b: #2563eb;
            --accent-c: #22d3ee;
            --danger: #fb7185;
            --sidebar-width: {sidebar_width};
            --sidebar-open-width: {sidebar_open_width};
            --content-max: 880px;
            --composer-max: 860px;
            --radius-lg: 24px;
            --radius-md: 16px;
            --shadow-soft: 0 24px 90px rgba(0,0,0,.42);
        }}

        html, body, .stApp {{
            color-scheme: dark !important;
            background: var(--bg-deep) !important;
        }}

        .stApp {{
            color: var(--text) !important;
            font-family: Inter, Geist, Satoshi, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            overflow-x: hidden;
            background:
                radial-gradient(circle at 16% 8%, rgba(139, 92, 246, 0.20), transparent 22rem),
                radial-gradient(circle at 72% 4%, rgba(37, 99, 235, 0.18), transparent 26rem),
                radial-gradient(circle at 65% 90%, rgba(244, 63, 94, 0.10), transparent 28rem),
                linear-gradient(180deg, #07080d 0%, #090a10 48%, #05060a 100%) !important;
        }}

        .stApp::before,
        .stApp::after {{
            content: "";
            position: fixed;
            border-radius: 999px;
            pointer-events: none;
            filter: blur(70px);
            opacity: .42;
            z-index: 0;
            will-change: transform;
        }}
        .stApp::before {{
            width: 32rem;
            height: 32rem;
            left: 17%;
            top: 4%;
            background: rgba(124, 58, 237, .22);
            animation: driftOne 30s ease-in-out infinite alternate;
        }}
        .stApp::after {{
            width: 28rem;
            height: 28rem;
            right: 6%;
            bottom: 0%;
            background: rgba(37, 99, 235, .17);
            animation: driftTwo 34s ease-in-out infinite alternate;
        }}

        .stApp > div::before {{
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            opacity: .10;
            background-image:
                linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px);
            background-size: 48px 48px;
            mask-image: radial-gradient(circle at 50% 20%, black, transparent 68%);
            z-index: 0;
        }}

        [data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stDeployButton"],
        [data-testid="stStatusWidget"],
        .stDeployButton,
        footer {{
            display: none !important;
            visibility: hidden !important;
        }}

        .block-container {{
            position: relative;
            z-index: 1;
            max-width: var(--content-max);
            padding-top: 1.2rem;
            padding-bottom: 9.5rem;
            animation: pageIn .28s ease both;
        }}

        h1, h2, h3, h4, h5, h6, p, label, span, div, li {{
            color: var(--text);
        }}

        section[data-testid="stSidebar"] {{
            display: {sidebar_display} !important;
            width: 18rem !important;
            min-width: 18rem !important;
        }}
        [data-testid="stSidebar"] > div {{
            background: linear-gradient(180deg, rgba(8,9,14,.90), rgba(8,9,14,.74)) !important;
            border-right: 1px solid var(--border);
            box-shadow: 20px 0 70px rgba(0,0,0,.36);
            backdrop-filter: blur(24px) saturate(145%);
        }}
        [data-testid="stSidebar"] h1 {{
            font-size: 1.08rem !important;
            letter-spacing: -.02em;
            margin-top: 2.3rem;
        }}
        [data-testid="stSidebar"] .stCaptionContainer,
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {{
            color: var(--muted) !important;
            font-size: .78rem;
            letter-spacing: .02em;
            text-transform: none;
        }}
        [data-testid="stSidebar"] hr {{
            margin: .9rem 0 !important;
            border-color: rgba(255,255,255,.07) !important;
        }}
        [data-testid="stSidebar"] button {{
            justify-content: flex-start !important;
            text-align: left !important;
            border-radius: 12px !important;
            min-height: 2.55rem !important;
            padding: .52rem .7rem !important;
            color: var(--text) !important;
            background: transparent !important;
            border: 1px solid transparent !important;
            box-shadow: none !important;
            transition: background .16s ease, border-color .16s ease, transform .16s ease;
        }}
        [data-testid="stSidebar"] button p {{
            white-space: pre-line !important;
            overflow: hidden;
            text-overflow: ellipsis;
            line-height: 1.34 !important;
            font-size: .90rem !important;
        }}
        [data-testid="stSidebar"] button:hover {{
            background: rgba(255,255,255,.065) !important;
            border-color: rgba(255,255,255,.09) !important;
            transform: translateX(2px);
        }}

        .stButton > button,
        [data-testid="stFormSubmitButton"] button {{
            border-radius: 14px !important;
            min-height: 2.7rem;
            color: var(--text) !important;
            border: 1px solid var(--border) !important;
            background: rgba(255,255,255,.045) !important;
            box-shadow: none !important;
            transition: transform .16s ease, border-color .16s ease, background .16s ease, box-shadow .16s ease;
        }}
        .stButton > button:hover,
        [data-testid="stFormSubmitButton"] button:hover {{
            transform: translateY(-1px);
            border-color: rgba(139,92,246,.36) !important;
            background: rgba(139,92,246,.11) !important;
            box-shadow: 0 12px 32px rgba(0,0,0,.24) !important;
        }}
        [data-testid="stFormSubmitButton"] button {{
            width: 100%;
            min-height: 3.1rem;
            font-weight: 760 !important;
            color: white !important;
            background: linear-gradient(135deg, var(--accent-a), var(--accent-b)) !important;
            border-color: rgba(255,255,255,.18) !important;
            box-shadow: 0 18px 42px rgba(37,99,235,.28) !important;
        }}
        [data-testid="stFormSubmitButton"] button:disabled {{
            opacity: .50 !important;
            color: rgba(255,255,255,.76) !important;
            background: rgba(255,255,255,.075) !important;
            box-shadow: none !important;
        }}

        .st-key-sidebar_toggle {{
            position: fixed;
            top: .85rem;
            left: .85rem;
            z-index: 9999;
            width: 2.75rem;
            height: 2.75rem;
        }}
        .st-key-sidebar_toggle button {{
            width: 2.75rem !important;
            min-width: 2.75rem !important;
            height: 2.75rem !important;
            min-height: 2.75rem !important;
            padding: 0 !important;
            justify-content: center !important;
            border-radius: 14px !important;
            background: rgba(255,255,255,.075) !important;
            border: 1px solid var(--border) !important;
            color: var(--text) !important;
            box-shadow: 0 18px 45px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.08) !important;
            backdrop-filter: blur(18px) saturate(150%);
        }}
        .element-container:has(.st-key-sidebar_toggle) {{
            height: 0 !important;
        }}

        .login-shell {{
            min-height: auto;
            display: grid;
            align-content: center;
            padding: 4rem 0 1rem;
        }}
        .login-card {{
            max-width: 610px;
            margin: 0 auto 1rem auto;
            padding: 1.8rem;
            border-radius: 26px;
            border: 1px solid var(--border);
            background: linear-gradient(180deg, rgba(255,255,255,.075), rgba(255,255,255,.035));
            box-shadow: var(--shadow-soft), inset 0 1px 0 rgba(255,255,255,.08);
            backdrop-filter: blur(24px) saturate(150%);
            animation: riseIn .32s ease both;
        }}
        .hero-badge {{
            display: inline-flex;
            align-items: center;
            gap: .45rem;
            padding: .38rem .72rem;
            border-radius: 999px;
            margin-bottom: 1.1rem;
            font-size: .78rem;
            color: #ddd6fe !important;
            background: rgba(139,92,246,.14);
            border: 1px solid rgba(139,92,246,.25);
        }}
        .hero-badge::before {{
            content: "";
            width: .45rem;
            height: .45rem;
            border-radius: 999px;
            background: linear-gradient(135deg, var(--accent-a), var(--accent-c));
            box-shadow: 0 0 16px rgba(139,92,246,.9);
        }}
        .hero-title {{
            font-size: clamp(2rem, 4vw, 3.6rem);
            line-height: 1.05;
            font-weight: 820;
            letter-spacing: -.045em;
            margin-bottom: .9rem;
            text-wrap: balance;
        }}
        .hero-copy {{
            color: var(--muted) !important;
            font-size: 1rem;
            line-height: 1.65;
        }}
        .auth-email {{
            margin-top: 1.15rem;
            padding: .85rem .95rem;
            border-radius: 8px;
            color: var(--text) !important;
            background: rgba(255,255,255,.045);
            border: 1px solid rgba(255,255,255,.09);
            font-size: .92rem;
            overflow-wrap: anywhere;
        }}
        .unauthorized-card {{
            border-color: rgba(248,113,113,.28) !important;
            background: linear-gradient(180deg, rgba(248,113,113,.09), rgba(255,255,255,.035)) !important;
        }}
        [data-testid="stForm"] {{
            max-width: 610px;
            margin: 0 auto;
            padding: 1rem;
            border-radius: 22px;
            border: 1px solid rgba(255,255,255,.075);
            background: rgba(255,255,255,.03);
            box-shadow: 0 18px 55px rgba(0,0,0,.30);
            backdrop-filter: blur(18px) saturate(145%);
        }}
        [data-testid="stForm"] label {{
            color: var(--muted) !important;
            font-size: .86rem;
        }}
        input, textarea, [data-baseweb="input"] input {{
            color: var(--text) !important;
            background: rgba(255,255,255,.055) !important;
            border: 1px solid var(--border) !important;
            border-radius: 16px !important;
            box-shadow: none !important;
        }}
        input::placeholder, textarea::placeholder {{
            color: var(--muted) !important;
            opacity: 1 !important;
        }}
        [data-baseweb="input"]:focus-within,
        textarea:focus {{
            border-color: rgba(139,92,246,.50) !important;
            box-shadow: 0 0 0 1px rgba(139,92,246,.24), 0 0 28px rgba(139,92,246,.16) !important;
        }}
        [data-testid="stForm"] input {{
            min-height: 3.15rem;
            padding: 0 .95rem !important;
        }}

        .welcome-shell {{
            min-height: calc(100vh - 15rem);
            display: grid;
            align-content: center;
            justify-items: center;
            gap: 1rem;
            text-align: center;
            animation: riseIn .36s ease both;
        }}
        .premium-kicker {{
            display: inline-flex;
            align-items: center;
            gap: .5rem;
            color: var(--muted) !important;
            font-size: .78rem;
            letter-spacing: .12em;
            text-transform: uppercase;
        }}
        .premium-kicker::before {{
            content: "";
            width: .5rem;
            height: .5rem;
            border-radius: 999px;
            background: linear-gradient(135deg, #8b5cf6, #22d3ee);
            box-shadow: 0 0 18px rgba(139,92,246,.9);
        }}
        .welcome-title {{
            max-width: 850px;
            font-size: clamp(2.25rem, 5.4vw, 4.5rem);
            line-height: .98;
            font-weight: 830;
            letter-spacing: -.06em;
            text-wrap: balance;
        }}
        .premium-title-glow {{ position: relative; }}
        .premium-title-glow::before {{
            content: "";
            position: absolute;
            inset: -1.5rem -2rem;
            background: radial-gradient(circle, rgba(139,92,246,.18), transparent 58%);
            filter: blur(20px);
            z-index: -1;
        }}
        .welcome-copy {{
            max-width: 660px;
            color: var(--muted) !important;
            font-size: 1rem;
            line-height: 1.65;
        }}
        .hero-examples {{
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: .65rem;
            margin-top: 1rem;
        }}
        .hero-chip {{
            padding: .55rem .82rem;
            border-radius: 999px;
            background: rgba(255,255,255,.045);
            border: 1px solid var(--border);
            color: var(--text) !important;
            font-size: .88rem;
            transition: transform .16s ease, border-color .16s ease, background .16s ease;
        }}
        .hero-chip:hover {{
            transform: translateY(-2px);
            background: rgba(139,92,246,.11);
            border-color: rgba(139,92,246,.34);
        }}
        .hero-shell {{ padding: .2rem 0 1rem 0; }}
        .hero-card,
        .conversation-note {{
            width: min(100%, 850px);
            margin: 0 auto .9rem;
            padding: 1rem 1.1rem;
            border-radius: 22px;
            border: 1px solid var(--border);
            background: linear-gradient(180deg, rgba(255,255,255,.062), rgba(255,255,255,.032));
            box-shadow: var(--shadow-soft), inset 0 1px 0 rgba(255,255,255,.06);
            backdrop-filter: blur(18px) saturate(145%);
        }}
        .hero-card .hero-title {{
            font-size: clamp(1.35rem, 3vw, 2.1rem);
            margin-top: .65rem;
        }}
        .conversation-note strong {{ display: block; margin-bottom: .2rem; }}
        .conversation-note span {{ color: var(--muted) !important; font-size: .82rem; }}

        [data-testid="stChatMessage"] {{
            width: min(100%, 850px);
            margin: 0 auto 1rem auto;
            padding: .78rem 1rem;
            border-radius: 20px;
            border: 1px solid var(--border);
            background: linear-gradient(180deg, rgba(255,255,255,.060), rgba(255,255,255,.034));
            box-shadow: 0 18px 60px rgba(0,0,0,.30), inset 0 1px 0 rgba(255,255,255,.055);
            backdrop-filter: blur(18px) saturate(138%);
            animation: messageIn .22s ease both;
        }}
        [data-testid="stChatMessage"] [data-testid="chatAvatarIcon-user"] {{
            background: rgba(37,99,235,.18) !important;
        }}
        [data-testid="stChatMessageContent"],
        [data-testid="stChatMessageContent"] *,
        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] strong,
        [data-testid="stMarkdownContainer"] em,
        [data-testid="stMarkdownContainer"] span,
        [data-testid="stMarkdownContainer"] mark {{
            color: var(--text) !important;
            background: transparent !important;
            background-color: transparent !important;
        }}
        [data-testid="stChatMessageContent"] p,
        [data-testid="stChatMessageContent"] li {{
            font-size: .98rem;
            line-height: 1.72;
        }}
        [data-testid="stChatMessageContent"] ul,
        [data-testid="stChatMessageContent"] ol {{
            margin-top: .55rem;
            margin-bottom: .55rem;
            padding-left: 1.35rem;
        }}
        [data-testid="stMarkdownContainer"] code {{
            color: #dbeafe !important;
            background: rgba(255,255,255,.075) !important;
            background-color: rgba(255,255,255,.075) !important;
            border: 1px solid rgba(255,255,255,.08);
            border-radius: 8px;
            padding: .12rem .34rem;
        }}
        [data-testid="stMarkdownContainer"] pre,
        [data-testid="stMarkdownContainer"] pre code {{
            display: block;
            color: #e5e7eb !important;
            background: #0d1117 !important;
            background-color: #0d1117 !important;
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: .85rem;
            overflow-x: auto;
        }}
        [data-testid="stCaptionContainer"] {{
            color: var(--muted-2) !important;
            font-size: .78rem !important;
        }}

        .answer-loader {{
            position: relative;
            display: flex;
            align-items: center;
            gap: .82rem;
            width: min(100%, 35rem);
            min-height: 3.5rem;
            padding: .78rem .92rem;
            overflow: hidden;
            color: rgba(244,244,245,.88) !important;
            font-size: .93rem;
            border: 1px solid rgba(167,139,250,.16);
            border-radius: 8px;
            background:
                linear-gradient(
                    120deg,
                    rgba(7, 5, 13, .98),
                    rgba(13, 7, 24, .98),
                    rgba(21, 16, 36, .96),
                    rgba(42, 24, 80, .70),
                    rgba(10, 8, 18, .98)
                );
            background-size: 300% 300%;
            box-shadow:
                0 16px 44px rgba(0,0,0,.34),
                inset 0 1px 0 rgba(255,255,255,.045),
                0 0 28px rgba(42,24,80,.18);
            animation: thinkingGradient 8s ease infinite, loaderIn .22s ease both;
        }}
        .answer-loader::before {{
            content: "";
            position: absolute;
            inset: 0;
            pointer-events: none;
            opacity: .42;
            background:
                radial-gradient(circle at 18% 45%, rgba(139,92,246,.18), transparent 28%),
                linear-gradient(90deg, transparent, rgba(255,255,255,.045), transparent);
            transform: translateX(-55%);
            animation: thinkingShimmer 3.8s ease-in-out infinite;
        }}
        .answer-loader-orb {{
            position: relative;
            flex: 0 0 1.55rem;
            width: 1.55rem;
            height: 1.55rem;
            border-radius: 999px;
            background:
                radial-gradient(circle at 35% 30%, rgba(219,234,254,.95), rgba(139,92,246,.88) 34%, rgba(42,24,80,.92) 70%, rgba(7,5,13,.98));
            box-shadow:
                0 0 0 1px rgba(255,255,255,.12),
                0 0 22px rgba(139,92,246,.36);
            animation: pulseOrb 1.65s ease-in-out infinite;
        }}
        .answer-loader-orb::after {{
            content: "";
            position: absolute;
            inset: -.34rem;
            border-radius: inherit;
            border: 1px solid rgba(167,139,250,.18);
            animation: orbRing 1.65s ease-out infinite;
        }}
        .answer-loader-body {{
            position: relative;
            display: grid;
            gap: .24rem;
            min-width: 0;
        }}
        .answer-loader-kicker {{
            color: rgba(196,181,253,.62) !important;
            font-size: .68rem;
            font-weight: 720;
            letter-spacing: .08em !important;
            line-height: 1;
            text-transform: uppercase;
        }}
        .answer-loader-text {{
            position: relative;
            display: inline-grid;
            min-width: min(22rem, 72vw);
            min-height: 1.25rem;
            color: rgba(244,244,245,.88) !important;
            font-weight: 650;
            line-height: 1.35;
        }}
        .answer-loader-text span {{
            grid-area: 1 / 1;
            opacity: 0;
            transform: translateY(.25rem);
            animation: loadingPhrase 7.5s ease-in-out infinite;
        }}
        .answer-loader-text span:nth-child(2) {{ animation-delay: 2.5s; }}
        .answer-loader-text span:nth-child(3) {{ animation-delay: 5s; }}
        .answer-loader-dots {{ position: relative; display: inline-flex; gap: .22rem; margin-left: .02rem; }}
        .answer-loader-dots span {{
            width: 6px;
            height: 6px;
            border-radius: 999px;
            background: rgba(196,181,253,.86);
            box-shadow: 0 0 12px rgba(139,92,246,.34);
            opacity: .38;
            animation: loaderDot 1.22s ease-in-out infinite;
        }}
        .answer-loader-dots span:nth-child(2) {{ animation-delay: .15s; }}
        .answer-loader-dots span:nth-child(3) {{ animation-delay: .30s; }}

        [data-testid="stChatInput"] {{
            position: fixed;
            left: calc(var(--sidebar-width) + ((100vw - var(--sidebar-width)) - min(850px, calc(100vw - var(--sidebar-width) - 3rem))) / 2);
            bottom: 1.1rem;
            width: min(850px, calc(100vw - var(--sidebar-width) - 3rem));
            z-index: 60;
            min-height: 4rem;
            padding: .42rem .66rem;
            border-radius: 22px;
            border: 1px solid rgba(255,255,255,.14);
            background: linear-gradient(180deg, rgba(255,255,255,.086), rgba(255,255,255,.050));
            box-shadow:
                0 0 0 1px rgba(255,255,255,.03),
                0 0 36px rgba(139,92,246,.16),
                0 28px 82px rgba(0,0,0,.50);
            backdrop-filter: blur(26px) saturate(155%);
            transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
        }}
        [data-testid="stChatInput"]:focus-within {{
            transform: translateY(-2px);
            border-color: rgba(139,92,246,.52);
            box-shadow:
                0 0 0 1px rgba(139,92,246,.20),
                0 0 42px rgba(139,92,246,.22),
                0 30px 90px rgba(0,0,0,.52);
        }}
        [data-testid="stChatInput"] textarea {{
            min-height: 2.8rem !important;
            color: var(--text) !important;
            caret-color: #c4b5fd;
            font-size: .98rem !important;
            background: transparent !important;
        }}
        [data-testid="stChatInput"] button {{
            width: 2.55rem !important;
            height: 2.55rem !important;
            border-radius: 14px !important;
            color: white !important;
            background: linear-gradient(135deg, var(--accent-a), var(--accent-b)) !important;
            box-shadow: 0 12px 28px rgba(37,99,235,.28) !important;
        }}


        /* Keep the native Streamlit sidebar controls available */
        [data-testid="collapsedControl"] {{
            display: flex !important;
            visibility: visible !important;
            opacity: 1 !important;
        }}

        /* PREMIUM CHAT COMPOSER - removes the white band and improves the input */
        [data-testid="stBottom"],
        [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"],
        div[data-testid="stBottomBlockContainer"],
        div:has(> [data-testid="stChatInput"]),
        div:has([data-testid="stChatInput"]) {{
            background: transparent !important;
            background-color: transparent !important;
            border: 0 !important;
            box-shadow: none !important;
        }}

        [data-testid="stBottom"]::before {{
            content: "";
            position: fixed;
            left: var(--sidebar-width);
            right: 0;
            bottom: 0;
            height: 9rem;
            pointer-events: none;
            background: linear-gradient(180deg, rgba(5,6,10,0) 0%, rgba(5,6,10,.72) 38%, rgba(5,6,10,.98) 100%);
            z-index: -1;
        }}

        [data-testid="stChatInput"] {{
            bottom: 1.25rem !important;
            min-height: 4.7rem !important;
            padding: .62rem .72rem !important;
            border-radius: 26px !important;
            border: 1px solid rgba(255,255,255,.16) !important;
            background:
                linear-gradient(180deg, rgba(18,20,30,.92), rgba(10,11,17,.92)) !important;
            box-shadow:
                0 0 0 1px rgba(255,255,255,.04),
                0 0 0 5px rgba(139,92,246,.055),
                0 22px 70px rgba(0,0,0,.58),
                0 0 46px rgba(139,92,246,.14) !important;
            backdrop-filter: blur(26px) saturate(155%) !important;
        }}

        [data-testid="stChatInput"]:focus-within {{
            transform: translateY(-2px) !important;
            border-color: rgba(139,92,246,.58) !important;
            box-shadow:
                0 0 0 1px rgba(139,92,246,.24),
                0 0 0 5px rgba(139,92,246,.075),
                0 28px 86px rgba(0,0,0,.62),
                0 0 52px rgba(139,92,246,.22) !important;
        }}

        [data-testid="stChatInput"] [data-baseweb="textarea"],
        [data-testid="stChatInput"] [data-baseweb="base-input"],
        [data-testid="stChatInput"] textarea {{
            background: rgba(255,255,255,.035) !important;
            background-color: rgba(255,255,255,.035) !important;
            border: 1px solid rgba(255,255,255,.08) !important;
            border-radius: 18px !important;
            color: var(--text) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.04) !important;
        }}

        [data-testid="stChatInput"] textarea {{
            min-height: 3.05rem !important;
            padding: .78rem 1rem !important;
            line-height: 1.35 !important;
            font-size: .98rem !important;
        }}

        [data-testid="stChatInput"] button {{
            width: 2.9rem !important;
            height: 2.9rem !important;
            min-width: 2.9rem !important;
            min-height: 2.9rem !important;
            border-radius: 18px !important;
            background: linear-gradient(135deg, #8b5cf6, #2563eb) !important;
            color: #fff !important;
            box-shadow: 0 14px 34px rgba(37,99,235,.34), inset 0 1px 0 rgba(255,255,255,.22) !important;
            border: 1px solid rgba(255,255,255,.16) !important;
        }}

        [data-testid="stChatInput"] button:hover {{
            transform: translateY(-1px) scale(1.02) !important;
            box-shadow: 0 18px 42px rgba(37,99,235,.42), 0 0 24px rgba(139,92,246,.32) !important;
        }}

        [data-testid="InputInstructions"] {{ display: none !important; }}

        [data-testid="stAlert"] {{
            background: rgba(255,255,255,.06) !important;
            border: 1px solid var(--border) !important;
            color: var(--text) !important;
        }}
        hr {{ border-color: rgba(255,255,255,.07) !important; }}
        * {{ scrollbar-color: rgba(255,255,255,.22) transparent; }}
        ::selection {{ background: rgba(139,92,246,.35); color: var(--text); }}

        @keyframes pageIn {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes messageIn {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes riseIn {{ from {{ opacity: 0; transform: translateY(12px) scale(.985); }} to {{ opacity: 1; transform: translateY(0) scale(1); }} }}
        @keyframes driftOne {{ from {{ transform: translate3d(-2rem,-1rem,0) scale(1); }} to {{ transform: translate3d(4rem,2rem,0) scale(1.08); }} }}
        @keyframes driftTwo {{ from {{ transform: translate3d(2rem,1rem,0) scale(1); }} to {{ transform: translate3d(-3rem,-2rem,0) scale(1.05); }} }}
        @keyframes loaderDot {{ 0%,100% {{ opacity:.32; transform: translateY(0); }} 50% {{ opacity:1; transform: translateY(-3px); }} }}
        @keyframes pulseOrb {{ 0%,100% {{ transform: scale(.94); opacity:.76; }} 50% {{ transform: scale(1.06); opacity:1; }} }}
        @keyframes orbRing {{ 0% {{ opacity:.42; transform: scale(.72); }} 100% {{ opacity:0; transform: scale(1.55); }} }}
        @keyframes loaderIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes thinkingGradient {{ 0% {{ background-position: 0% 50%; }} 50% {{ background-position: 100% 50%; }} 100% {{ background-position: 0% 50%; }} }}
        @keyframes thinkingShimmer {{ 0%, 100% {{ transform: translateX(-62%); opacity: .12; }} 45%, 55% {{ opacity: .42; }} 100% {{ transform: translateX(62%); }} }}
        @keyframes typingDot {{ 0%, 80%, 100% {{ opacity: .35; transform: translateY(0); }} 40% {{ opacity: 1; transform: translateY(-4px); }} }}
        @keyframes loadingPhrase {{
            0%, 8% {{ opacity: 0; transform: translateY(.25rem); }}
            12%, 30% {{ opacity: 1; transform: translateY(0); }}
            36%, 100% {{ opacity: 0; transform: translateY(-.22rem); }}
        }}



        /* HARD DARK MODE PATCH: removes every remaining Streamlit light surface */
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"],
        [data-testid="stVerticalBlock"],
        [data-testid="stHorizontalBlock"],
        [data-testid="stBottom"],
        [data-testid="stBottomBlockContainer"],
        .main,
        main,
        body {{
            background: transparent !important;
            color: var(--text) !important;
        }}

        [data-testid="stBottom"] {{
            background:
                linear-gradient(180deg, rgba(5,6,10,0) 0%, rgba(5,6,10,.78) 28%, rgba(5,6,10,.96) 100%) !important;
            border-top: 1px solid rgba(255,255,255,.055) !important;
            box-shadow: 0 -22px 70px rgba(0,0,0,.34) !important;
            backdrop-filter: blur(18px) saturate(140%) !important;
            z-index: 55 !important;
        }}

        [data-testid="stChatInput"] *,
        [data-testid="stChatInput"] > div,
        [data-testid="stChatInput"] section,
        [data-testid="stChatInput"] form,
        [data-testid="stChatInput"] label,
        [data-testid="stChatInput"] div[data-baseweb],
        [data-testid="stChatInput"] [data-baseweb="textarea"],
        [data-testid="stChatInput"] [data-baseweb="base-input"] {{
            background: transparent !important;
            background-color: transparent !important;
            color: var(--text) !important;
            box-shadow: none !important;
        }}

        [data-testid="stChatInput"] textarea,
        [data-testid="stChatInput"] textarea:focus,
        [data-testid="stChatInput"] textarea:disabled {{
            background: transparent !important;
            background-color: transparent !important;
            color: var(--text) !important;
            -webkit-text-fill-color: var(--text) !important;
        }}

        [data-testid="stChatInput"] textarea::placeholder,
        input::placeholder,
        textarea::placeholder {{
            color: rgba(244,244,245,.48) !important;
            opacity: 1 !important;
        }}

        input,
        textarea,
        [data-baseweb="input"],
        [data-baseweb="textarea"],
        [data-baseweb="base-input"] {{
            background: rgba(255,255,255,.055) !important;
            background-color: rgba(255,255,255,.055) !important;
            color: var(--text) !important;
            border-color: rgba(255,255,255,.12) !important;
            box-shadow: none !important;
        }}

        [data-testid="stChatMessage"],
        [data-testid="stChatMessage"] > div,
        [data-testid="stChatMessageContent"],
        [data-testid="stChatMessageContent"] > div,
        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] span,
        [data-testid="stMarkdownContainer"] strong,
        [data-testid="stMarkdownContainer"] em,
        [data-testid="stMarkdownContainer"] a {{
            background: transparent !important;
            background-color: transparent !important;
        }}

        [data-testid="stMarkdownContainer"] table,
        [data-testid="stMarkdownContainer"] thead,
        [data-testid="stMarkdownContainer"] tbody,
        [data-testid="stMarkdownContainer"] tr,
        [data-testid="stMarkdownContainer"] td,
        [data-testid="stMarkdownContainer"] th {{
            background: rgba(255,255,255,.035) !important;
            color: var(--text) !important;
            border-color: rgba(255,255,255,.10) !important;
        }}

        [data-testid="stMarkdownContainer"] blockquote {{
            background: rgba(255,255,255,.04) !important;
            border-left: 3px solid rgba(139,92,246,.62) !important;
            color: var(--text) !important;
        }}

        [data-testid="stForm"] *,
        [data-testid="stForm"] [data-baseweb="input"],
        [data-testid="stForm"] [data-baseweb="base-input"] {{
            background-color: transparent !important;
        }}
        [data-testid="stForm"] input {{
            background: rgba(255,255,255,.06) !important;
            background-color: rgba(255,255,255,.06) !important;
        }}

        .st-emotion-cache-ue6h4q,
        .st-emotion-cache-1dp5vir,
        .st-emotion-cache-1y4p8pa,
        .st-emotion-cache-13k62yr {{
            background: transparent !important;
        }}



        /* FINAL COMPOSER OVERRIDE - must come after the hard dark patch */
        [data-testid="stBottom"],
        [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"],
        div:has([data-testid="stChatInput"]) {{
            background: transparent !important;
            background-color: transparent !important;
        }}
        [data-testid="stChatInput"] {{
            background: linear-gradient(180deg, rgba(18,20,30,.94), rgba(9,10,16,.94)) !important;
            background-color: rgba(12,14,22,.94) !important;
            border: 1px solid rgba(255,255,255,.16) !important;
        }}
        [data-testid="stChatInput"] [data-baseweb="textarea"],
        [data-testid="stChatInput"] [data-baseweb="base-input"],
        [data-testid="stChatInput"] textarea {{
            background: rgba(255,255,255,.04) !important;
            background-color: rgba(255,255,255,.04) !important;
            border-radius: 18px !important;
            border: 1px solid rgba(255,255,255,.08) !important;
        }}
        section[data-testid="stSidebar"][aria-expanded="true"] {{
            display: block !important;
            visibility: visible !important;
        }}

        /* POLISH PASS: stable layout, cleaner sidebar, centered composer */
        *, *::before, *::after {{
            box-sizing: border-box;
        }}

        html, body, .stApp, [data-testid="stAppViewContainer"] {{
            width: 100%;
            max-width: 100%;
            overflow-x: clip !important;
        }}

        h1, h2, h3, h4, h5, h6,
        .welcome-title,
        .hero-title,
        .sidebar-brand-title {{
            letter-spacing: 0 !important;
        }}

        .stApp::before,
        .stApp::after,
        .stApp > div::before {{
            display: none !important;
        }}

        .block-container {{
            width: min(var(--content-max), calc(100vw - var(--sidebar-width) - 3rem)) !important;
            max-width: var(--content-max) !important;
            padding: 1.5rem 0 8.5rem !important;
        }}

        section[data-testid="stSidebar"] {{
            width: var(--sidebar-width) !important;
            min-width: var(--sidebar-width) !important;
            max-width: var(--sidebar-width) !important;
            border-right: 1px solid rgba(255,255,255,.08) !important;
        }}

        [data-testid="stSidebar"] > div {{
            background:
                linear-gradient(180deg, rgba(10,12,18,.96), rgba(7,8,13,.94)) !important;
            box-shadow: 14px 0 48px rgba(0,0,0,.34) !important;
            overflow-x: hidden !important;
        }}

        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
            gap: .55rem !important;
        }}

        .sidebar-brand {{
            display: flex;
            align-items: center;
            gap: .78rem;
            padding: 1rem .25rem .7rem;
        }}

        .sidebar-brand-mark {{
            display: grid;
            place-items: center;
            width: 2.35rem;
            height: 2.35rem;
            border-radius: 8px;
            font-size: .78rem;
            font-weight: 800;
            color: #f8fafc !important;
            background: linear-gradient(135deg, rgba(139,92,246,.92), rgba(37,99,235,.82));
            border: 1px solid rgba(255,255,255,.18);
            box-shadow: 0 14px 34px rgba(37,99,235,.22);
        }}

        .sidebar-brand-title {{
            font-size: 1.05rem;
            font-weight: 800;
            line-height: 1.1;
        }}

        .sidebar-brand-subtitle {{
            margin-top: .16rem;
            color: var(--muted) !important;
            font-size: .76rem;
            line-height: 1.25;
        }}

        .sidebar-user-card {{
            margin: .15rem 0 .2rem;
            padding: .72rem .8rem;
            border: 1px solid rgba(255,255,255,.08);
            border-radius: 8px;
            background: rgba(255,255,255,.035);
        }}

        .sidebar-user-card span {{
            display: block;
            color: var(--muted-2) !important;
            font-size: .72rem;
            line-height: 1.2;
        }}

        .sidebar-user-card strong {{
            display: block;
            margin-top: .18rem;
            overflow: hidden;
            color: var(--text) !important;
            font-size: .91rem;
            line-height: 1.3;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        [data-testid="stSidebar"] input {{
            min-height: 2.45rem !important;
            border-radius: 8px !important;
            font-size: .86rem !important;
        }}

        [data-testid="stSidebar"] .st-key-new_conversation button {{
            justify-content: center !important;
            min-height: 2.85rem !important;
            margin: .2rem 0 .35rem !important;
            border-color: rgba(139,92,246,.28) !important;
            background: linear-gradient(135deg, rgba(139,92,246,.22), rgba(37,99,235,.14)) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.08) !important;
            font-weight: 760 !important;
        }}

        [data-testid="stSidebar"] button {{
            width: 100% !important;
            border-radius: 8px !important;
            min-height: 2.7rem !important;
            padding: .58rem .7rem !important;
        }}

        [data-testid="stSidebar"] button p {{
            width: 100%;
            color: inherit !important;
            font-size: .87rem !important;
            line-height: 1.32 !important;
        }}

        [data-testid="stSidebar"] button:hover {{
            background: rgba(255,255,255,.07) !important;
            border-color: rgba(255,255,255,.14) !important;
            transform: translateX(1px) !important;
        }}

        [data-testid="stSidebar"] button:disabled {{
            opacity: 1 !important;
            color: #f8fafc !important;
            border-color: rgba(139,92,246,.36) !important;
            background: linear-gradient(135deg, rgba(139,92,246,.20), rgba(37,99,235,.10)) !important;
            box-shadow: inset 3px 0 0 rgba(139,92,246,.95), inset 0 1px 0 rgba(255,255,255,.08) !important;
        }}

        [data-testid="stSidebar"] .st-key-delete_conversation button {{
            color: #fecdd3 !important;
            border-color: rgba(251,113,133,.18) !important;
            background: rgba(251,113,133,.055) !important;
        }}

        .welcome-shell {{
            min-height: min(620px, calc(100vh - 13rem)) !important;
            gap: 1.05rem !important;
            padding: 2.25rem 0 1.5rem !important;
        }}

        .premium-kicker {{
            letter-spacing: .08em !important;
            font-weight: 720 !important;
        }}

        .welcome-title {{
            max-width: 780px !important;
            font-size: clamp(2.25rem, 5vw, 4.25rem) !important;
            line-height: 1.04 !important;
            font-weight: 820 !important;
        }}

        .welcome-copy {{
            max-width: 620px !important;
            color: rgba(244,244,245,.70) !important;
            font-size: 1.02rem !important;
            line-height: 1.62 !important;
        }}

        .hero-examples {{
            gap: .58rem !important;
            margin-top: .75rem !important;
        }}

        .hero-chip {{
            border-radius: 999px !important;
            padding: .58rem .84rem !important;
            background: rgba(255,255,255,.052) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.055);
            cursor: default;
        }}

        .hero-chip:hover {{
            transform: translateY(-1px) !important;
            background: rgba(139,92,246,.13) !important;
            border-color: rgba(139,92,246,.38) !important;
        }}

        .hero-card,
        .conversation-note,
        [data-testid="stChatMessage"],
        .login-card,
        [data-testid="stForm"] {{
            border-radius: 8px !important;
        }}

        [data-testid="stChatMessage"] {{
            width: min(100%, 850px) !important;
            padding: .9rem 1rem !important;
            margin-bottom: .85rem !important;
        }}

        [data-testid="stBottom"] {{
            position: fixed !important;
            left: var(--sidebar-width) !important;
            right: 0 !important;
            bottom: 0 !important;
            z-index: 80 !important;
            display: flex !important;
            justify-content: center !important;
            padding: 1rem 1.5rem 1.2rem !important;
            background:
                linear-gradient(180deg, rgba(5,6,10,0) 0%, rgba(5,6,10,.80) 34%, rgba(5,6,10,.98) 100%) !important;
            border-top: 1px solid rgba(255,255,255,.055) !important;
            box-shadow: 0 -24px 70px rgba(0,0,0,.28) !important;
            pointer-events: none !important;
        }}

        [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"] {{
            width: min(var(--content-max), 100%) !important;
            max-width: var(--content-max) !important;
            margin: 0 auto !important;
            padding: 0 !important;
            pointer-events: auto !important;
        }}

        [data-testid="stChatInput"] {{
            position: fixed !important;
            left: 50vw !important;
            right: auto !important;
            bottom: 1.05rem !important;
            width: min(var(--composer-max), calc(100vw - 2rem)) !important;
            max-width: var(--composer-max) !important;
            min-height: 3.8rem !important;
            margin: 0 !important;
            padding: .55rem .62rem !important;
            transform: translateX(-50%) !important;
            border-radius: 18px !important;
            border: 1px solid rgba(255,255,255,.14) !important;
            background: linear-gradient(180deg, rgba(18,20,29,.96), rgba(9,10,16,.96)) !important;
            box-shadow:
                0 0 0 1px rgba(255,255,255,.035),
                0 18px 58px rgba(0,0,0,.54),
                0 0 32px rgba(37,99,235,.11) !important;
            backdrop-filter: blur(22px) saturate(145%) !important;
        }}

        [data-testid="stChatInput"]:focus-within {{
            transform: translateX(-50%) translateY(-1px) !important;
            border-color: rgba(139,92,246,.50) !important;
        }}

        [data-testid="stChatInput"] [data-baseweb="textarea"],
        [data-testid="stChatInput"] [data-baseweb="base-input"],
        [data-testid="stChatInput"] textarea {{
            min-width: 0 !important;
            border-radius: 14px !important;
            border: 1px solid rgba(255,255,255,.08) !important;
            background: rgba(255,255,255,.045) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.04) !important;
        }}

        [data-testid="stChatInput"] textarea {{
            min-height: 2.85rem !important;
            max-height: 9rem !important;
            padding: .73rem .95rem !important;
            line-height: 1.42 !important;
        }}

        [data-testid="stChatInput"] button {{
            flex: 0 0 2.75rem !important;
            width: 2.75rem !important;
            height: 2.75rem !important;
            min-width: 2.75rem !important;
            min-height: 2.75rem !important;
            border-radius: 14px !important;
        }}

        @media (max-width: 900px) {{
            :root {{ --sidebar-width: 0px; }}
            section[data-testid="stSidebar"] {{ width: 15.75rem !important; min-width: 15.75rem !important; }}
            [data-testid="stBottom"] {{ left: 0 !important; padding: .85rem .9rem 1rem !important; }}
            [data-testid="stChatInput"] {{
                left: 50vw !important;
                bottom: .85rem !important;
                width: calc(100vw - 2rem) !important;
                transform: translateX(-50%) !important;
            }}
            .block-container {{
                width: calc(100vw - 2rem) !important;
                padding-left: 0 !important;
                padding-right: 0 !important;
            }}
            .welcome-shell {{ min-height: calc(100vh - 12rem) !important; }}
            .welcome-title {{ font-size: clamp(2rem, 11vw, 3.2rem) !important; }}
            .welcome-copy {{ font-size: .96rem !important; }}
        }}

        @media (min-width: 901px) {{
            [data-testid="stChatInput"] {{
                left: calc(var(--sidebar-width) + ((100vw - var(--sidebar-width)) / 2)) !important;
                width: min(var(--composer-max), calc(100vw - var(--sidebar-width) - 2.25rem)) !important;
                transform: translateX(-50%) !important;
            }}

            [data-testid="stChatInput"]:focus-within {{
                transform: translateX(-50%) translateY(-1px) !important;
            }}
        }}

        /* FINAL UI REFINEMENT: compact composer, lighter sidebar, readable chat cards */
        html,
        body,
        .stApp,
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"] {{
            max-width: 100vw !important;
            overflow-x: hidden !important;
        }}

        [data-testid="stSidebar"] > div {{
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,.20) transparent;
        }}

        [data-testid="stSidebar"] > div::-webkit-scrollbar {{
            width: 8px;
        }}

        [data-testid="stSidebar"] > div::-webkit-scrollbar-thumb {{
            background: rgba(255,255,255,.16);
            border: 2px solid transparent;
            border-radius: 999px;
            background-clip: padding-box;
        }}

        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
            gap: .45rem !important;
        }}

        [data-testid="stSidebar"] hr {{
            margin: .7rem 0 .55rem !important;
            opacity: .65;
        }}

        .sidebar-brand {{
            padding: .82rem .1rem .55rem !important;
        }}

        .sidebar-user-card {{
            margin: .05rem 0 .1rem !important;
            padding: .62rem .72rem !important;
            background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025)) !important;
            border-color: rgba(255,255,255,.075) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.045);
        }}

        .sidebar-user-card span {{
            font-size: .68rem !important;
            text-transform: uppercase;
            letter-spacing: .05em !important;
        }}

        .sidebar-user-card strong {{
            font-size: .9rem !important;
            font-weight: 720 !important;
        }}

        [data-testid="stSidebar"] [data-testid="stExpander"] {{
            border: 1px solid rgba(255,255,255,.07) !important;
            border-radius: 8px !important;
            background: rgba(255,255,255,.025) !important;
            overflow: hidden !important;
        }}

        [data-testid="stSidebar"] [data-testid="stExpander"] details summary {{
            min-height: 2.15rem !important;
            padding: .28rem .55rem !important;
            font-size: .78rem !important;
            color: var(--muted) !important;
        }}

        [data-testid="stSidebar"] [data-testid="stExpander"] input {{
            min-height: 2.25rem !important;
        }}

        [data-testid="stSidebar"] .st-key-new_conversation button {{
            min-height: 2.55rem !important;
            margin: .28rem 0 .2rem !important;
        }}

        [data-testid="stSidebar"] button {{
            min-height: 2.48rem !important;
            padding: .48rem .62rem !important;
            background: rgba(255,255,255,.026) !important;
            border-color: rgba(255,255,255,.07) !important;
            box-shadow: none !important;
        }}

        [data-testid="stSidebar"] button p {{
            font-size: .84rem !important;
            line-height: 1.26 !important;
            white-space: pre-line !important;
        }}

        [data-testid="stSidebar"] button:disabled {{
            background: linear-gradient(135deg, rgba(139,92,246,.16), rgba(37,99,235,.075)) !important;
            border-color: rgba(139,92,246,.32) !important;
            box-shadow: inset 2px 0 0 rgba(139,92,246,.9), inset 0 1px 0 rgba(255,255,255,.06) !important;
        }}

        [data-testid="stSidebar"] .stCaptionContainer p {{
            margin-top: .35rem !important;
            font-size: .72rem !important;
            font-weight: 680 !important;
            letter-spacing: .04em !important;
            text-transform: uppercase !important;
            color: rgba(244,244,245,.48) !important;
        }}

        /* Allow the native Streamlit sidebar close/open behavior */
        [data-testid="collapsedControl"] {{
            display: flex !important;
            visibility: visible !important;
            opacity: 1 !important;
            z-index: 120 !important;
        }}

        [data-testid="collapsedControl"] button {{
            border-radius: 8px !important;
            background: rgba(17,19,28,.92) !important;
            border: 1px solid rgba(255,255,255,.12) !important;
            box-shadow: 0 10px 28px rgba(0,0,0,.30) !important;
        }}

        {sidebar_visibility_css}

        .block-container {{
            width: min(820px, calc(100vw - var(--sidebar-width) - 3.25rem)) !important;
            max-width: 820px !important;
            margin-left: auto !important;
            margin-right: auto !important;
            padding-bottom: 7.5rem !important;
        }}

        .conversation-note {{
            margin: .2rem auto 1rem !important;
            padding: .72rem .85rem !important;
            max-width: 820px !important;
            background: rgba(255,255,255,.035) !important;
            border-color: rgba(255,255,255,.075) !important;
        }}

        [data-testid="stChatMessage"] {{
            width: min(100%, 820px) !important;
            margin: 0 auto .72rem !important;
            padding: .82rem .9rem !important;
            border-color: rgba(255,255,255,.075) !important;
            background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.026)) !important;
            box-shadow: 0 12px 38px rgba(0,0,0,.24), inset 0 1px 0 rgba(255,255,255,.045) !important;
        }}

        [data-testid="stChatMessage"] [data-testid="stChatMessageContent"] p,
        [data-testid="stChatMessage"] [data-testid="stChatMessageContent"] li {{
            font-size: .96rem !important;
            line-height: 1.68 !important;
        }}

        [data-testid="stChatMessage"] [data-testid*="chatAvatarIcon"],
        [data-testid="stChatMessage"] [data-testid*="stAvatar"] {{
            margin-top: .08rem !important;
        }}

        [data-testid="stBottom"] {{
            position: fixed !important;
            left: 0 !important;
            right: 0 !important;
            bottom: 0 !important;
            z-index: 90 !important;
            display: flex !important;
            justify-content: center !important;
            padding: .78rem 1.15rem .9rem !important;
            border-top: 0 !important;
            background: linear-gradient(180deg, rgba(5,6,10,0), rgba(5,6,10,.82) 44%, rgba(5,6,10,.98)) !important;
            pointer-events: none !important;
        }}

        [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"] {{
            width: min(820px, calc(100vw - 2rem)) !important;
            max-width: 820px !important;
            margin: 0 auto !important;
            padding: 0 !important;
            pointer-events: auto !important;
        }}

        [data-testid="stChatInput"] {{
            position: relative !important;
            left: auto !important;
            right: auto !important;
            bottom: auto !important;
            width: 100% !important;
            max-width: 100% !important;
            min-height: 3.28rem !important;
            margin: 0 auto !important;
            padding: .38rem .42rem !important;
            transform: none !important;
            border-radius: 16px !important;
            border: 1px solid rgba(255,255,255,.12) !important;
            outline: 0 !important;
            background: linear-gradient(180deg, rgba(17,19,28,.98), rgba(8,10,16,.98)) !important;
            box-shadow: 0 16px 48px rgba(0,0,0,.46), inset 0 1px 0 rgba(255,255,255,.055) !important;
        }}

        [data-testid="stChatInput"],
        [data-testid="stChatInput"] *,
        [data-testid="stChatInput"] *:focus,
        [data-testid="stChatInput"] *:focus-visible,
        [data-testid="stChatInput"] *:focus-within {{
            outline: 0 !important;
            outline-color: transparent !important;
        }}

        [data-testid="stChatInput"]:focus-within {{
            transform: translateY(-1px) !important;
            border-color: rgba(139,92,246,.36) !important;
            box-shadow: 0 18px 54px rgba(0,0,0,.50), 0 0 0 1px rgba(139,92,246,.12), inset 0 1px 0 rgba(255,255,255,.06) !important;
        }}

        [data-testid="stChatInput"] [data-baseweb="textarea"],
        [data-testid="stChatInput"] [data-baseweb="base-input"] {{
            border: 0 !important;
            box-shadow: none !important;
            background: transparent !important;
        }}

        [data-testid="stChatInput"] textarea {{
            min-height: 2.34rem !important;
            max-height: 7.5rem !important;
            padding: .52rem .72rem !important;
            border: 0 !important;
            border-radius: 12px !important;
            background: transparent !important;
            box-shadow: none !important;
            line-height: 1.45 !important;
            font-size: .94rem !important;
        }}

        [data-testid="stChatInput"] button {{
            align-self: center !important;
            flex: 0 0 2.38rem !important;
            width: 2.38rem !important;
            height: 2.38rem !important;
            min-width: 2.38rem !important;
            min-height: 2.38rem !important;
            margin-right: .04rem !important;
            border-radius: 12px !important;
            border: 1px solid rgba(255,255,255,.13) !important;
            background: linear-gradient(135deg, #8b5cf6, #2563eb) !important;
            box-shadow: 0 10px 24px rgba(37,99,235,.28), inset 0 1px 0 rgba(255,255,255,.20) !important;
            transition: transform .16s ease, box-shadow .16s ease, filter .16s ease !important;
        }}

        [data-testid="stChatInput"] button:hover {{
            transform: translateY(-1px) !important;
            filter: brightness(1.07) !important;
            box-shadow: 0 14px 30px rgba(37,99,235,.36), 0 0 18px rgba(139,92,246,.22), inset 0 1px 0 rgba(255,255,255,.24) !important;
        }}

        @media (max-width: 900px) {{
            .block-container {{
                width: calc(100vw - 1.5rem) !important;
                max-width: calc(100vw - 1.5rem) !important;
                padding-bottom: 7rem !important;
            }}

            [data-testid="stBottom"] {{
                left: 0 !important;
                padding: .68rem .75rem .82rem !important;
            }}

            [data-testid="stBottom"] > div,
            [data-testid="stBottomBlockContainer"] {{
                width: calc(100vw - 1.5rem) !important;
            }}

            [data-testid="stChatInput"] {{
                left: auto !important;
                width: 100% !important;
                transform: none !important;
            }}

            .st-key-sidebar_toggle {{
                left: .75rem !important;
                top: .75rem !important;
            }}
        }}

        /* Final chat alignment guard: match the message column */
        [data-testid="stBottom"] {{
            left: var(--sidebar-width) !important;
        }}

        [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"] {{
            width: min(820px, calc(100vw - var(--sidebar-width) - 2rem)) !important;
        }}

        [data-testid="stChatInput"],
        [data-testid="stChatInput"]:focus,
        [data-testid="stChatInput"]:focus-within,
        [data-testid="stChatInput"] *,
        [data-testid="stChatInput"] *:focus,
        [data-testid="stChatInput"] *:focus-visible,
        [data-testid="stChatInput"] [data-baseweb],
        [data-testid="stChatInput"] [data-baseweb]:focus,
        [data-testid="stChatInput"] [data-baseweb]:focus-within,
        [data-testid="stChatInput"] textarea,
        [data-testid="stChatInput"] textarea:focus {{
            outline: none !important;
            box-shadow: none !important;
            border-color: rgba(255,255,255,.10) !important;
            --border-color: rgba(255,255,255,.10) !important;
        }}

        [data-testid="stChatInput"] {{
            box-shadow: 0 16px 48px rgba(0,0,0,.46), inset 0 1px 0 rgba(255,255,255,.055) !important;
        }}

        [data-testid="stChatInput"]:focus-within {{
            border-color: rgba(139,92,246,.30) !important;
            box-shadow: 0 18px 54px rgba(0,0,0,.50), 0 0 0 1px rgba(139,92,246,.10), inset 0 1px 0 rgba(255,255,255,.06) !important;
        }}

        /* Native sidebar controls: close inside the sidebar, open at top-left */
        [data-testid="stSidebarCollapseButton"] {{
            display: inline-flex !important;
            visibility: visible !important;
            opacity: 1 !important;
        }}

        [data-testid="collapsedControl"] {{
            position: fixed !important;
            top: .75rem !important;
            left: .75rem !important;
            z-index: 9999 !important;
            display: flex !important;
            visibility: visible !important;
            opacity: 1 !important;
        }}

        [data-testid="collapsedControl"] button,
        [data-testid="stSidebarCollapseButton"] button {{
            width: 2.45rem !important;
            min-width: 2.45rem !important;
            height: 2.45rem !important;
            min-height: 2.45rem !important;
            padding: 0 !important;
            border-radius: 10px !important;
            color: rgba(244,244,245,.88) !important;
            background: rgba(18,20,29,.92) !important;
            border: 1px solid rgba(255,255,255,.12) !important;
            box-shadow: 0 12px 32px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.06) !important;
            backdrop-filter: blur(16px) saturate(145%) !important;
        }}

        section[data-testid="stSidebar"][aria-expanded="true"] {{
            width: var(--sidebar-open-width) !important;
            min-width: var(--sidebar-open-width) !important;
            max-width: var(--sidebar-open-width) !important;
            visibility: visible !important;
            opacity: 1 !important;
            transform: translateX(0) !important;
        }}

        section[data-testid="stSidebar"][aria-expanded="false"] {{
            width: 0 !important;
            min-width: 0 !important;
            max-width: 0 !important;
            border-right: 0 !important;
            overflow: hidden !important;
        }}

        body:has(section[data-testid="stSidebar"][aria-expanded="false"]) .block-container {{
            width: min(820px, calc(100vw - 2rem)) !important;
            max-width: 820px !important;
        }}

        body:has(section[data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stBottom"] {{
            left: 0 !important;
        }}

        body:has(section[data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stBottom"] > div,
        body:has(section[data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stBottomBlockContainer"] {{
            width: min(820px, calc(100vw - 2rem)) !important;
        }}

        /* Unified assistant thinking card: removes the nested-card effect from st.chat_message */
        [data-testid="stChatMessage"]:has(.assistant-thinking-card) {{
            width: min(100%, 820px) !important;
            margin: 0 auto .85rem !important;
            padding: 0 !important;
            border: 0 !important;
            background: transparent !important;
            box-shadow: none !important;
        }}

        [data-testid="stChatMessage"]:has(.assistant-thinking-card) [data-testid*="stAvatar"],
        [data-testid="stChatMessage"]:has(.assistant-thinking-card) [data-testid*="chatAvatarIcon"] {{
            margin-top: .08rem !important;
        }}

        [data-testid="stChatMessage"]:has(.assistant-thinking-card) [data-testid="stChatMessageContent"],
        [data-testid="stChatMessage"]:has(.assistant-thinking-card) [data-testid="stMarkdownContainer"],
        [data-testid="stChatMessage"]:has(.assistant-thinking-card) [data-testid="stMarkdownContainer"] > div {{
            width: 100% !important;
            max-width: 100% !important;
            margin: 0 !important;
            padding: 0 !important;
            background: transparent !important;
            border: 0 !important;
            box-shadow: none !important;
        }}

        .assistant-thinking-card {{
            position: relative;
            width: 100%;
            max-width: 820px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            min-height: 4.05rem;
            padding: 1rem 1.22rem;
            overflow: hidden;
            border-radius: 16px;
            border: 1px solid rgba(150,110,255,.30);
            color: rgba(255,255,255,.92) !important;
            background:
                linear-gradient(
                    120deg,
                    rgba(7, 5, 13, .98),
                    rgba(13, 7, 24, .98),
                    rgba(21, 16, 36, .96),
                    rgba(28, 18, 51, .90),
                    rgba(42, 24, 80, .76),
                    rgba(10, 8, 18, .98)
                );
            background-size: 300% 300%;
            box-shadow:
                0 14px 34px rgba(0,0,0,.30),
                0 0 24px rgba(120,80,255,.12),
                inset 0 1px 0 rgba(255,255,255,.055);
            animation: thinkingGradient 8s ease infinite, loaderIn .22s ease both;
        }}

        .assistant-thinking-card::before {{
            content: "";
            position: absolute;
            inset: 0;
            pointer-events: none;
            opacity: .36;
            background:
                radial-gradient(circle at 20% 52%, rgba(150,110,255,.18), transparent 30%),
                linear-gradient(90deg, transparent, rgba(255,255,255,.03), transparent);
            transform: translateX(-58%);
            animation: thinkingShimmer 4.2s ease-in-out infinite;
        }}

        .assistant-thinking-card::after {{
            content: "";
            position: absolute;
            left: 10%;
            right: 6%;
            bottom: -.25rem;
            height: 1px;
            opacity: .62;
            background: linear-gradient(90deg, transparent, rgba(150,110,255,.46), transparent);
            box-shadow: 0 0 18px rgba(150,110,255,.18);
        }}

        .assistant-thinking-content {{
            position: relative;
            z-index: 1;
            display: flex;
            flex-direction: column;
            justify-content: center;
            gap: .18rem;
            min-width: 0;
        }}

        .assistant-thinking-title {{
            color: #b99cff !important;
            font-size: .76rem;
            font-weight: 800;
            letter-spacing: .06em !important;
            line-height: 1.15;
            text-transform: uppercase;
        }}

        .assistant-thinking-row {{
            display: flex;
            align-items: center;
            gap: .52rem;
            min-width: 0;
            color: rgba(255,255,255,.92) !important;
            font-size: .98rem;
            font-weight: 620;
            line-height: 1.38;
        }}

        .assistant-thinking-row > span:first-child {{
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .typing-dots {{
            display: inline-flex;
            flex: 0 0 auto;
            align-items: center;
            gap: 4px;
        }}

        .typing-dots span {{
            width: 5px;
            height: 5px;
            border-radius: 50%;
            background: rgba(176,132,255,.95);
            box-shadow: 0 0 8px rgba(155,108,255,.22);
            animation: typingDot 1.2s ease-in-out infinite;
        }}

        .typing-dots span:nth-child(2) {{ animation-delay: .15s; }}
        .typing-dots span:nth-child(3) {{ animation-delay: .3s; }}

        @media (max-width: 640px) {{
            .assistant-thinking-card {{
                min-height: 3.55rem;
                padding: .84rem .92rem;
                border-radius: 14px;
            }}

            .assistant-thinking-row {{
                font-size: .92rem;
            }}
        }}
        @media (prefers-reduced-motion: reduce) {{
            .stApp::before,
            .stApp::after,
            .block-container,
            [data-testid="stChatMessage"],
            .answer-loader,
            .answer-loader::before,
            .answer-loader-orb,
            .answer-loader-orb::after,
            .answer-loader-dots span,
            .answer-loader-text span,
            .assistant-thinking-card,
            .assistant-thinking-card::before,
            .typing-dots span {{
                animation: none !important;
            }}
            .answer-loader-text span:first-child {{
                opacity: 1 !important;
                transform: none !important;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_cabecalho(total_notes: int, tem_mensagens: bool) -> None:
    if tem_mensagens:
        return

    st.markdown(
        f"""
        <div class="welcome-shell">
            <div class="premium-kicker">Excelencia Operacional AI</div>
            <div class="welcome-title premium-title-glow">O que vamos resolver hoje?</div>
            <div class="welcome-copy">
                IA corporativa para consultar rotinas, relatorios, automacoes e regras de negocio.
                {total_notes} documentos indexados.
            </div>
            <div class="hero-examples">
                <div class="hero-chip">Automacao Hominum</div>
                <div class="hero-chip">Robbyson</div>
                <div class="hero-chip">Relatorios operacionais</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar_toggle() -> None:
    return


def render_sidebar(usuario: str) -> None:
    conversas = carregar_conversas()
    usuario_html = html.escape(usuario)

    with st.sidebar:
        st.markdown(
            f"""
            <div class="sidebar-brand">
                <div class="sidebar-brand-mark">EX</div>
                <div>
                    <div class="sidebar-brand-title">EXOP AI</div>
                    <div class="sidebar-brand-subtitle">Conhecimento operacional</div>
                </div>
            </div>
            <div class="sidebar-user-card">
                <span>Logado como</span>
                <strong>{usuario_html}</strong>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.button("Sair", key="logout", use_container_width=True):
            sair_usuario()

        if st.button("＋ Nova conversa", key="new_conversation", use_container_width=True):
            criar_conversa(usuario)
            st.rerun()

        st.divider()
        if not conversas:
            st.caption("Nenhuma conversa salva ainda.")
        else:
            grupo_atual = ""
            for conversa in conversas:
                grupo = grupo_data_conversa(conversa.get("atualizada_em", ""))
                if grupo != grupo_atual:
                    grupo_atual = grupo
                    st.caption(grupo)

                titulo = conversa.get("titulo", "Nova conversa")
                total = len(conversa.get("mensagens", []))
                data = formatar_data(conversa.get("atualizada_em", ""))
                active = conversa.get("id") == st.session_state.active_conversation_id
                prefixo = "● " if active else ""
                label = f"{prefixo}{titulo[:44]}{'...' if len(titulo) > 44 else ''}\n{total} msg - {data}"
                if st.button(
                    label,
                    key=f"open_{conversa.get('id')}",
                    use_container_width=True,
                    disabled=active,
                ):
                    st.session_state.active_conversation_id = conversa.get("id", "")
                    st.rerun()

        st.divider()
        conversa_ativa = obter_conversa_ativa(usuario)
        if st.button("Apagar conversa atual", key="delete_conversation", use_container_width=True):
            excluir_conversa(conversa_ativa["id"])
            st.rerun()

        with st.expander("Admin"):
            senha = st.text_input("Senha", type="password", key="admin_password")
            if st.button("Entrar no admin", use_container_width=True):
                st.session_state.admin_unlocked = senha == ADMIN_PASSWORD
                if not st.session_state.admin_unlocked:
                    st.warning("Senha incorreta.")

            if st.session_state.admin_unlocked:
                st.success("Admin liberado.")
                usuarios = usuarios_online()
                st.write(f"Usuarios online: {len(usuarios)}")
                for item in usuarios:
                    st.caption(f"- {item['nome']} ({formatar_data(item['visto_em'])})")
                st.write(f"Conversas salvas: {len(conversas)}")


def exibir_historico(mensagens: list[dict]) -> None:
    for mensagem in mensagens:
        avatar = ASSISTANT_AVATAR if mensagem["role"] == "assistant" else USER_AVATAR
        with st.chat_message(mensagem["role"], avatar=avatar):
            st.markdown(mensagem["content"])
            response_time = mensagem.get("response_time")
            if response_time is not None and mensagem["role"] == "assistant":
                st.caption(f"Tempo de resposta: {response_time:.1f}s")


def render_resposta_animando() -> str:
    return """
    <div class="assistant-thinking-card" role="status" aria-live="polite" aria-label="Consultor AI analisando contexto">
        <div class="assistant-thinking-content">
            <div class="assistant-thinking-title">Consultor AI</div>
            <div class="assistant-thinking-row">
                <span>Analisando contexto...</span>
                <span class="typing-dots" aria-hidden="true">
                    <span></span>
                    <span></span>
                    <span></span>
                </span>
            </div>
        </div>
    </div>
    """


def render_thinking_message() -> None:
    st.markdown(render_resposta_animando(), unsafe_allow_html=True)


def main() -> None:
    inicializar_estado()
    aplicar_estilo(st.session_state.sidebar_collapsed)
    render_sidebar_toggle()

    if not st.session_state.user_name:
        render_login()
        st.stop()

    usuario = st.session_state.user_name
    registrar_usuario_online(usuario)

    vault_path = str(DEFAULT_VAULT_PATH)
    model_name = DEFAULT_MODEL
    max_notes = DEFAULT_MAX_NOTES
    api_key = get_api_key()

    if not Path(vault_path).exists():
        st.error(f"Vault nao encontrado: {vault_path}")
        st.stop()

    try:
        vault_cache = carregar_vault(vault_path, VAULT_CACHE_VERSION, VAULT_REFRESH_SECONDS)
        graph, notes = vault_cache.get()
    except Exception as exc:
        st.error(f"Erro ao carregar a base de conhecimento: {exc}")
        st.stop()

    if not notes:
        st.warning("Nenhum arquivo Markdown foi encontrado no vault informado.")
        st.stop()

    conversa = obter_conversa_ativa(usuario)
    render_sidebar(usuario)
    tem_mensagens = bool(conversa.get("mensagens", []))
    render_cabecalho(len(notes), tem_mensagens)
    if tem_mensagens:
        st.markdown(
            f"""
            <div class="conversation-note">
                <strong>{conversa.get("titulo", "Nova conversa")}</strong>
                <span>{len(conversa.get("mensagens", []))} mensagens salvas nesta conversa</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    exibir_historico(conversa.get("mensagens", []))

    question = st.chat_input("Pergunte sobre processos, relatorios ou automacoes...")
    if not question:
        return

    conversa = obter_conversa_ativa(usuario)
    if conversa.get("titulo") == "Nova conversa":
        conversa["titulo"] = titulo_da_conversa(question)

    conversa.setdefault("mensagens", []).append({"role": "user", "content": question})
    atualizar_conversa(conversa)

    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(question)

    selected_notes = select_relevant_notes(
        question=question,
        notes=notes,
        graph=graph,
        preferred_note=None,
        limit=max_notes,
    )
    context = build_context(selected_notes, notes, graph, question=question)
    answer_cache_key = build_answer_cache_key(question, context, model_name)
    local_answer = build_ticket_responsible_answer(question, selected_notes, notes)

    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        loading_placeholder = st.empty()
        started_at = perf_counter()
        cached_answer = st.session_state.answer_cache.get(answer_cache_key)
        if cached_answer:
            answer = cached_answer
        elif local_answer:
            answer = local_answer
            save_answer_cache(answer_cache_key, answer)
        else:
            loading_placeholder.markdown(render_resposta_animando(), unsafe_allow_html=True)
            try:
                answer = ask_gemini(
                    question=question,
                    context=context,
                    conversation_history=conversa.get("mensagens", [])[:-1],
                    api_key=api_key,
                    model_name=model_name,
                )
                save_answer_cache(answer_cache_key, answer)
            except GeminiQuotaError as exc:
                fallback_answer = build_local_fallback_answer(question, selected_notes, notes)
                if exc.retry_after_seconds:
                    answer = (
                        f"{fallback_answer}\n\n"
                        f"Observacao: o Gemini atingiu o limite de uso agora. "
                        f"Tente novamente em cerca de {exc.retry_after_seconds}s."
                    )
                else:
                    answer = (
                        f"{fallback_answer}\n\n"
                        "Observacao: o Gemini atingiu o limite de uso agora. "
                        "Tente novamente em instantes."
                    )
            except Exception as exc:
                answer = (
                    "Nao consegui responder agora. "
                    f"Revise a conexao com o Gemini e tente novamente.\n\nDetalhe tecnico: {exc}"
                )
        response_time = perf_counter() - started_at
        loading_placeholder.empty()

        st.markdown(answer)
        st.caption(f"Tempo de resposta: {response_time:.1f}s")

    conversa = obter_conversa_ativa(usuario)
    conversa.setdefault("mensagens", []).append(
        {
            "role": "assistant",
            "content": answer,
            "response_time": response_time,
        }
    )
    atualizar_conversa(conversa)


if __name__ == "__main__":
    main()
