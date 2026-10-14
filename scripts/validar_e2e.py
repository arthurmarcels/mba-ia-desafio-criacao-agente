#!/usr/bin/env python3
"""Harness de validação E2E dos 15 passos do avaliador — Residencial Aurora.

Automatiza o fluxo do avaliador do desafio (README do repositório base,
seção "Fluxo do avaliador") contra a API real em ``http://localhost:8000``.

Modos:

- **padrão** (com ``GEMINI_API_KEY`` no ambiente ou no ``.env``): roda os
  15 passos completos, inclusive conversa com o modelo Gemini real.
- **``--sem-modelo``**: roda tudo que não depende do modelo — passos 1, 2,
  9 e 15, mais os extras QA-1..QA-3, que injetam na sessão eventos de
  confirmação no formato exato que o ADK 2.9.1 gera (verificado no
  código-fonte instalado) e provam a retomada da confirmação com sessão
  persistida em SQLite, inclusive após reinício. Os passos que exigem o
  modelo são reportados como BLOCKED, não como falha.

Códigos de saída: ``0`` tudo OK; ``1`` qualquer FAIL; ``2`` apenas BLOCKED.

Uso::

    uv run python scripts/validar_e2e.py [--sem-modelo]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
API = os.environ.get("AURORA_API", "http://127.0.0.1:8000")
HOST = os.environ.get("AURORA_HOST", "127.0.0.1")
PORTA = 8000
TIMEOUT_HTTP = 240  # invocações com LLM podem levar dezenas de segundos

# Cota free tier do gemini-2.5-flash: 5 requisições/minuto (429 RESOURCE_EXHAUSTED
# acima disso). Cada turno de conversa faz 1–3 chamadas ao modelo (transferências
# de agente + tool calls), então o harness espaça os turnos e recupera os 429
# que ainda escaparem. Configurável: AURORA_PACING_S / AURORA_ESPERA_429_S.
PACING_S = float(os.environ.get("AURORA_PACING_S", "60"))
ESPERA_429_S = float(os.environ.get("AURORA_ESPERA_429_S", "65"))
TENTATIVAS_MENSAGEM = 3

_GATE = threading.Lock()  # serializa turnos entre threads (passo 14 usa threads)
_ULTIMO_TURNO = [0.0]  # time.monotonic() da última liberação do gate
RECUPERACOES_429: list[str] = []  # notas de recuperação exibidas no relatório


def _eh_rate_limit(status: int, corpo: Any) -> bool:
    """Detecta o 429 do Gemini encapsulado no 500 da API (``{"detail": ...}``)."""
    if status != 500:
        return False
    texto = json.dumps(corpo, ensure_ascii=False) if corpo is not None else ""
    return "429" in texto or "RESOURCE_EXHAUSTED" in texto or "quota" in texto.lower()


def _eh_cota_diaria(corpo: Any) -> bool:
    """429 da cota diária (``...PerDay...``) — retry em minutos não resolve."""
    texto = json.dumps(corpo, ensure_ascii=False) if corpo is not None else ""
    return "PerDay" in texto


def _gate_pacing() -> None:
    """Espera o espaçamento mínimo desde o último turno liberado."""
    with _GATE:
        resto = _ULTIMO_TURNO[0] + PACING_S - time.monotonic()
        if resto > 0:
            time.sleep(resto)
        _ULTIMO_TURNO[0] = time.monotonic()


def _esvaziar_janela() -> None:
    """Espera a janela de cota rolar por completo — antes de rajadas (passo 14)."""
    with _GATE:
        resto = _ULTIMO_TURNO[0] + ESPERA_429_S - time.monotonic()
        if resto > 0:
            time.sleep(resto)
        _ULTIMO_TURNO[0] = time.monotonic()

# Marcadores do enunciado
RSV_1377, RSV_4821, RSV_2950 = "RSV-1377", "RSV-4821", "RSV-2950"

# SHA-256 dos arquivos de dados do repositório base (passo 15: idênticos).
# Computados do commit be87e1d (enunciado e dados do desafio, intactos).
HASHES_DADOS = {
    "apartamentos.json": "d4b297899313f37e5bbb3ab9d1f7f070bb5985feca06058c5bce32c8c970b44b",
    "areas.json": "e56489365f156bd1be75961a3ccc965bf72cc2cef998bc5863c78ddded44c619",
    "reservas.json": "a1b849ac213ee8c208907e525e42d45f6ffe544710b7eed53a60f195e1a87197",
    "visitantes.json": "15a98d85b92479f5fa4f242d475c7ed4a2f7f4cd5826d2d0b867fa68c1fb88d5",
    "regulamento.md": "86893f3b1471c06ec48c4004d2a9739134b22fac4ec5802b5d10a554cb5f4494",
}

OK, FAIL, BLOCKED = "OK", "FAIL", "BLOCKED"


def _env_tem_chave() -> bool:
    """True quando há GEMINI_API_KEY no ambiente ou preenchida no ``.env``."""
    if os.environ.get("GEMINI_API_KEY"):
        return True
    env = RAIZ / ".env"
    if env.exists():
        for linha in env.read_text(encoding="utf-8").splitlines():
            if linha.startswith("GEMINI_API_KEY=") and linha.split("=", 1)[1].strip():
                return True
    return False


class Passo:
    """Resultado de um passo: status + detalhes das conferências."""

    def __init__(self, numero: str, titulo: str) -> None:
        self.numero = numero
        self.titulo = titulo
        self.status = OK
        self.detalhes: list[str] = []

    def check(self, condicao: bool, descricao: str) -> bool:
        marca = "✓" if condicao else "✗"
        self.detalhes.append(f"  [{marca}] {descricao}")
        if not condicao:
            self.status = FAIL
        return condicao

    def bloquear(self, motivo: str) -> None:
        self.status = BLOCKED
        self.detalhes.append(f"  [⊘] {motivo}")

    def info(self, texto: str) -> None:
        self.detalhes.append(f"  [i] {texto}")

    def __str__(self) -> str:
        simbolo = {OK: "✓", FAIL: "✗", BLOCKED: "⊘"}[self.status]
        linhas = [f"{simbolo} Passo {self.numero} — {self.titulo} [{self.status}]"]
        linhas.extend(self.detalhes)
        return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Cliente HTTP (stdlib)
# ---------------------------------------------------------------------------


def http(
    metodo: str, caminho: str, corpo: dict[str, Any] | None = None
) -> tuple[int, Any]:
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        API + caminho,
        data=dados,
        headers={"Content-Type": "application/json"},
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_HTTP) as resp:
            bruto = resp.read()
            return resp.status, json.loads(bruto) if bruto else None
    except urllib.error.HTTPError as erro:
        bruto = erro.read()
        try:
            return erro.code, json.loads(bruto) if bruto else None
        except json.JSONDecodeError:
            return erro.code, bruto.decode(errors="replace")


def criar_sessao(apartamento: str) -> tuple[int, str]:
    status, corpo = http("POST", "/sessoes", {"apartamento": apartamento})
    return status, (corpo or {}).get("session_id", "")


def _conversa_com_retry(
    caminho: str, corpo: dict[str, Any], *, descricao: str, gate: bool = True
) -> tuple[int, dict[str, Any]]:
    """POST de conversa com recuperação do 429 (cota free tier do Gemini).

    O 429 chega como 500 da API (``falha na execução do agente: ... 429
    RESOURCE_EXHAUSTED ...``). A recuperação depende do endpoint:

    - **mensagens**: reenviar o mesmo texto é seguro — o histórico persistido
      impede execução duplicada de tool (o modelo vê a ação já feita).
    - **confirmações**: a tool já executou antes da chamada final do LLM e o
      id foi consumido; reenviar daria ``409`` falso. A recuperação é um
      turno de acompanhamento na mesma sessão, que fecha o ciclo pela API
      e devolve o que a conversa real responderia — registrado em
      ``RECUPERACOES_429`` para o relatório.
    """
    eh_confirmacao = caminho.endswith("/confirmacoes")
    for tentativa in range(1, TENTATIVAS_MENSAGEM + 1):
        if gate:
            _gate_pacing()
        status, resposta = http("POST", caminho, corpo)
        if not _eh_rate_limit(status, resposta):
            return status, resposta
        if _eh_cota_diaria(resposta):
            RECUPERACOES_429.append(
                f"{descricao}: cota DIÁRIA do modelo esgotada — retry não"
                " resolve; reportando falha real do ambiente (trocar de modelo"
                " via AURORA_MODEL ou aguardar o reset)"
            )
            return status, resposta
        if tentativa == TENTATIVAS_MENSAGEM:
            break
        if eh_confirmacao:
            _esvaziar_janela()
            status_rec, resposta_rec = _conversa_com_retry(
                f"/sessoes/{sessao_id_de(caminho)}/mensagens",
                {
                    "texto": (
                        "Obrigado, a resposta anterior já foi registrada."
                        " Pode encerrar por aqui."
                    )
                },
                descricao=f"recuperação de {descricao}",
            )
            RECUPERACOES_429.append(
                f"{descricao}: 429 após executar a ação — fechada com turno de"
                " acompanhamento (o id já estava consumido; reenvio daria 409)"
            )
            # O turno de acompanhamento fechou o ciclo pela conversa; devolve
            # o status REAL dele (se a recuperação falhou, o passo falha).
            return status_rec, resposta_rec
        time.sleep(ESPERA_429_S)
    return status, resposta


def sessao_id_de(caminho: str) -> str:
    """/sessoes/<id>/... → <id>"""
    return caminho.split("/")[2]


def enviar_mensagem(sessao: str, texto: str) -> tuple[int, dict[str, Any]]:
    return _conversa_com_retry(
        f"/sessoes/{sessao}/mensagens", {"texto": texto}, descricao=f"mensagem em {sessao[:8]}"
    )


def responder_confirmacao(
    sessao: str, id_: str, confirmado: bool, *, gate: bool = True
) -> tuple[int, dict[str, Any]]:
    return _conversa_com_retry(
        f"/sessoes/{sessao}/confirmacoes",
        {"id": id_, "confirmado": confirmado},
        descricao=f"confirmação {id_[:8]} em {sessao[:8]}",
        gate=gate,
    )


def eventos_da_sessao(sessao: str) -> list[dict[str, Any]]:
    _, corpo = http("GET", f"/sessoes/{sessao}/eventos")
    return corpo or []


def eventos_como_texto(eventos: list[dict[str, Any]]) -> str:
    return json.dumps(eventos, ensure_ascii=False)


def reservas_de(apartamento: str) -> list[dict[str, Any]]:
    _, corpo = http("GET", f"/apartamentos/{apartamento}/reservas")
    return corpo or []


def visitantes_de(apartamento: str) -> list[dict[str, Any]]:
    _, corpo = http("GET", f"/apartamentos/{apartamento}/visitantes")
    return corpo or []


def reservas_com(apartamento: str, area: str, data: str) -> list[dict[str, Any]]:
    return [r for r in reservas_de(apartamento) if r["area"] == area and r["data"] == data]


# ---------------------------------------------------------------------------
# Ciclo de vida do servidor (o harness é dono do processo)
# ---------------------------------------------------------------------------


class Servidor:
    """Sobe/derruba a API como subprocesso, com log próprio.

    Se a API já responde em ``http://HOST:PORTA`` (ex.: subida manual anterior),
    o harness a adota em vez de subir outra instância — útil para bater os
    passos contra um servidor em debug. No modo adotado não há processo para
    derrubar; o passo 13 então sobe uma instância própria para reiniciar.
    """

    def __init__(self) -> None:
        self.dir_log = Path(tempfile.mkdtemp(prefix="aurora-e2e-"))
        self.proc: subprocess.Popen | None = None
        self.adotado = False

    def _comando(self) -> list[str]:
        python = RAIZ / ".venv" / "bin" / "python"
        if not python.exists():
            python = Path(sys.executable)
        return [
            str(python),
            "-m",
            "uvicorn",
            "aurora.api:app",
            "--host",
            HOST,
            "--port",
            str(PORTA),
        ]

    def _api_responde(self) -> bool:
        try:
            status, _ = http("GET", "/apartamentos/101/reservas")
            return status == 200
        except (urllib.error.URLError, OSError):
            return False

    def subir(self, timeout: float = 60.0) -> None:
        if self._api_responde():
            self.adotado = True
            self.proc = None
            return
        log = open(self.dir_log / "servidor.log", "ab")
        self.proc = subprocess.Popen(
            self._comando(),
            cwd=RAIZ,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        inicio = time.time()
        while time.time() - inicio < timeout:
            try:
                if self._api_responde():
                    return
            except (urllib.error.URLError, OSError):
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"servidor morreu na subida; log: {self.dir_log / 'servidor.log'}"
                )
            time.sleep(0.5)
        raise RuntimeError("servidor não respondeu no tempo; veja o log")

    def parar_com_ctrl_c(self) -> None:
        """Envia SIGINT ao grupo do processo — o Ctrl+C do passo 13."""
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=10)
            self.proc = None
        elif self.adotado:
            raise RuntimeError(
                "API adotada (não é processo deste harness); reinício do passo 13"
                " exige que o harness seja dono do servidor — rode sem servidor"
                " externo de pé."
            )

    def __enter__(self) -> "Servidor":
        self.subir()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc and self.proc.poll() is None:
            self.parar_com_ctrl_c()


# ---------------------------------------------------------------------------
# Passo 15 — checagens estáticas do repositório
# ---------------------------------------------------------------------------


def passo_15(p: Passo) -> None:
    pyproject = (RAIZ / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"google-adk==(\d+)\.(\d+)\.(\d+)"', pyproject)
    p.check(m is not None, "google-adk com versão exata fixada no pyproject.toml")
    if m:
        serie, minor, _ = (int(g) for g in m.groups())
        p.check(
            serie == 2 and (serie, minor) >= (2, 2),
            f"ADK na série 2 e >= 2.2.0 (encontrado 2.{minor}.{m.group(3)})",
        )

    for arquivo, hash_esperado in HASHES_DADOS.items():
        if hash_esperado == "TBD":
            continue
        atual = hashlib.sha256((RAIZ / "dados" / arquivo).read_bytes()).hexdigest()
        p.check(atual == hash_esperado, f"dados/{arquivo} idêntico ao repositório base")

    # Nenhuma chave versionada: varre arquivos rastreados por padrão de chave.
    padrao_chave = re.compile(rb"AIza[0-9A-Za-z_\-]{30,}")
    vazamentos: list[str] = []
    for caminho in RAIZ.rglob("*"):
        if not caminho.is_file():
            continue
        rel = caminho.relative_to(RAIZ)
        if any(
            parte in (".venv", ".git", "__pycache__")
            for parte in rel.parts
        ):
            continue
        try:
            if padrao_chave.search(caminho.read_bytes()):
                vazamentos.append(str(rel))
        except OSError:
            continue
    p.check(not vazamentos, f"nenhuma chave de API versionada (achados: {vazamentos})")
    p.check(not (RAIZ / ".env").exists() or ".env" in (RAIZ / ".gitignore").read_text(), ".env fora do git")

    env_example = (RAIZ / ".env.example").read_text(encoding="utf-8")
    p.check("GEMINI_API_KEY" in env_example, ".env.example declara GEMINI_API_KEY")

    # Agente principal + >=2 especialistas; tools sem apartamento; regulamento
    # fora das instruções do principal — inspecionando os objetos reais.
    sys.path.insert(0, str(RAIZ))
    from aurora import agents, tools  # noqa: E402

    p.check(len(agents.root_agent.sub_agents) >= 2, "agente principal com ao menos 2 especialistas")

    def nome_da_tool(t: Any) -> str:
        return getattr(t, "name", None) or getattr(t, "__name__", "<sem nome>")

    nomes_tools_root = {nome_da_tool(t) for t in agents.root_agent.tools}
    p.check(
        {"criar_reserva", "cancelar_reserva", "autorizar_visitante"} <= nomes_tools_root,
        "agente principal concentra as tools de escrita (reservar/cancelar/autorizar)",
    )
    for especialista in agents.root_agent.sub_agents:
        nomes = {nome_da_tool(t) for t in especialista.tools}
        p.check(
            not (nomes & {"criar_reserva", "cancelar_reserva", "autorizar_visitante"}),
            f"{especialista.name} é somente leitura (sem tools de escrita)",
        )

    for funcao in (
        tools.criar_reserva,
        tools.cancelar_reserva,
        tools.autorizar_visitante,
        tools.listar_minhas_reservas,
        tools.verificar_disponibilidade,
    ):
        params = inspect.signature(funcao).parameters
        p.check(
            "apartamento" not in params,
            f"tool {funcao.__name__} não aceita parâmetro apartamento",
        )

    instrucao_root = agents.root_agent.instruction or ""
    dedo_dados = dedos_regulamento(excluir_capitulo="IV")
    vazando = [d for d in dedo_dados if d in instrucao_root]
    p.check(not vazando, "instruções do agente principal sem trechos do regulamento")

    ddl = (RAIZ / "aurora" / "db.py").read_text(encoding="utf-8")
    p.check(
        "ux_reservas_ativas" in ddl and "UNIQUE INDEX" in ddl.upper(),
        "exclusividade da reserva no instante da gravação (UNIQUE parcial no banco)",
    )

    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    for secao in ("Arquitetura", "Garantias", "Como rodar"):
        p.check(secao in readme, f"README com a seção {secao}")


# ---------------------------------------------------------------------------
# Dedos-duro do regulamento (passo 12): trechos literais de artigos de
# capítulos ALHEIOS ao assunto não podem aparecer nos eventos.
# ---------------------------------------------------------------------------

_RE_CAP = re.compile(r"^##\s+Capítulo\s+([IVXLCDM]+):\s*(.+?)\s*$")
_RE_ART = re.compile(r"^\*\*Art\.\s*\d+")


def dedos_regulamento(excluir_capitulo: str) -> list[str]:
    """Substrings literais (~90 chars) de artigos de capítulos que não sejam o excluído.

    O capítulo de um artigo é o ativo quando o artigo **começa** (um capítulo
    novo pode surgir no meio do buffer do último artigo do capítulo anterior —
    ex.: o Art. 28 é o último do Cap. IV e o cabeçalho do Cap. V vem antes do
    Art. 29; atribuir pelo capítulo corrente no fim do buffer trocaria o
    capítulo do último artigo de cada capítulo).
    """
    dedos: list[str] = []
    capitulo_atual: str | None = None
    capitulo_do_artigo: str | None = None
    buffer: list[str] = []

    def emitir() -> None:
        if buffer and capitulo_do_artigo and capitulo_do_artigo != excluir_capitulo:
            texto = " ".join(" ".join(buffer).split())
            if len(texto) > 120:
                dedos.append(texto[15:105])

    for linha in (RAIZ / "dados" / "regulamento.md").read_text(encoding="utf-8").splitlines():
        m = _RE_CAP.match(linha)
        if m:
            capitulo_atual = m.group(1)
            continue
        if _RE_ART.match(linha):
            emitir()
            buffer = [linha]
            capitulo_do_artigo = capitulo_atual
        elif buffer:
            buffer.append(linha)
    emitir()
    return dedos


def checar_eventos_sem_regulamento_alheio(p: Passo, eventos: list[dict[str, Any]]) -> None:
    texto_total = eventos_como_texto(eventos)
    dedos = dedos_regulamento(excluir_capitulo="IV")
    vazaram = [d[:50] for d in dedos if d in texto_total]
    p.check(not vazaram, f"nenhum evento contém trecho de capítulo alheio ao assunto ({vazaram[:3]})")


# ---------------------------------------------------------------------------
# Extras QA — retomada de confirmação sem modelo (formato real do ADK 2.9.1)
# ---------------------------------------------------------------------------


async def _injetar_pendencia(sessao_id: str, area: str, data: str) -> str:
    """Grava na sessão eventos no formato exato que o ADK gera para o gate.

    Reproduz ``generate_request_confirmation_event`` (google/adk/flows/
    llm_flows/functions.py do ADK 2.9.1): o function call original da tool,
    seguido do ``adk_request_confirmation`` com ``args.originalFunctionCall``.
    Retorna o id da confirmação pendente.
    """
    from google.adk.events import Event
    from google.genai import types

    from aurora import runner as runner_mod

    svc = runner_mod.criar_runner().session_service
    sessao = await svc.get_session(
        app_name=runner_mod.APP_NAME,
        user_id=runner_mod.USER_ID,
        session_id=sessao_id,
    )
    if sessao is None:
        raise RuntimeError(f"sessão {sessao_id} não encontrada para injeção")

    # Turno de usuário antes do function call: o protocolo da API Gemini exige
    # que um turno de function call venha imediatamente após um turno de
    # usuário ("400 INVALID_ARGUMENT: Please ensure that function call turn
    # comes immediately after a user turn"). No fluxo real a pendência nasce
    # como resposta a uma mensagem do morador; aqui o turno sintético cumpre
    # o mesmo papel para a retomada via rota de confirmações.
    await svc.append_event(
        sessao,
        Event(
            invocation_id=str(uuid.uuid4()),
            author="user",
            content=types.Content(role="user", parts=[types.Part(text=f"Reserve a {area} para {data}.")]),
        ),
    )

    args_tool = {"area": area, "data": data}
    fc_id = f"adk-{uuid.uuid4()}"
    await svc.append_event(
        sessao,
        Event(
            invocation_id=str(uuid.uuid4()),
            author="aurora",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id=fc_id, name="criar_reserva", args=args_tool
                        )
                    )
                ],
            ),
        ),
    )
    conf_id = f"adk-{uuid.uuid4()}"
    await svc.append_event(
        sessao,
        Event(
            invocation_id=str(uuid.uuid4()),
            author="aurora",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name=runner_mod.FUNCAO_CONFIRMACAO,
                            id=conf_id,
                            args={
                                "originalFunctionCall": {
                                    "id": fc_id,
                                    "name": "criar_reserva",
                                    "args": args_tool,
                                },
                                "toolConfirmation": {
                                    "hint": "Please approve or reject the tool call criar_reserva()"
                                },
                            },
                        )
                    )
                ],
            ),
        ),
    )
    return conf_id


def injetar_pendencia(sessao_id: str, area: str, data: str) -> str:
    return asyncio.run(_injetar_pendencia(sessao_id, area, data))


def _pendencia_para_qa(
    sessao: str, area: str, data: str, tem_modelo: bool
) -> tuple[str | None, int]:
    """Cria a pendência de confirmação usada pelos QAs.

    Com modelo: nasce de **conversa real** — o evento ``adk_request_
    confirmation`` é gerado pelo próprio ADK (o function call carrega o
    ``thought_signature`` que os modelos Gemini 3.x exigem no histórico; a
    injeção sintética não o tem e faria a chamada seguinte falhar com 400).

    Sem modelo: injeta o par de eventos no formato exato do ADK 2.9.1 — a
    tool executa antes da falha do LLM, provando a retomada mesmo sem chave.
    """
    if tem_modelo:
        status, corpo = enviar_mensagem(
            sessao, f"Reserve a churrasqueira para {data}."
        )
        pendentes = (corpo or {}).get("confirmacoes_pendentes") or []
        return (pendentes[0]["id"] if pendentes else None), status
    return injetar_pendencia(sessao, area, data), 200


def qa_retomada(servidor: Servidor, tem_modelo: bool, resultados: list[Passo]) -> None:
    """QA-1/QA-2/QA-3: retomada de confirmação (risco nº 1 do desafio).

    - QA-1: aprovação executa a ação gravada no evento (uma única vez);
      reenvio do mesmo id → 409 sem reexecutar.
    - QA-2: negação não grava nada.
    - QA-3: pendência criada antes de um reinício é aprovável depois dele.
    """
    s1 = criar_sessao("101")[1]

    # QA-1 — aprovar executa 1x; reenvio 409.
    qa1 = Passo("QA-1", "Retomada: aprovar executa a ação gravada, exatamente 1x")
    conf, status_msg = _pendencia_para_qa(s1, "churrasqueira", "2030-06-01", tem_modelo)
    qa1.check(conf is not None, f"pendência criada (status da mensagem: {status_msg})")
    if conf:
        status, corpo = responder_confirmacao(s1, conf, True)
        status_esperado = 200 if tem_modelo else 500  # sem chave, a tool roda e o LLM falha depois
        qa1.check(
            status == status_esperado,
            f"resposta da aprovação: {status} (esperado {status_esperado}; sem modelo a tool"
            " executa e a falha vem depois, na chamada do LLM)",
        )
        gravadas = reservas_com("101", "churrasqueira", "2030-06-01")
        qa1.check(len(gravadas) == 1, f"churrasqueira 2030-06-01 gravada exatamente 1x ({gravadas})")
        status, _ = responder_confirmacao(s1, conf, True)
        qa1.check(status == 409, f"reenvio da mesma confirmação → 409 (veio {status})")
        gravadas = reservas_com("101", "churrasqueira", "2030-06-01")
        qa1.check(len(gravadas) == 1, "reenvio não reexecutou a ação")
    resultados.append(qa1)

    # QA-2 — negar não grava.
    qa2 = Passo("QA-2", "Retomada: negar não grava nada")
    conf, status_msg = _pendencia_para_qa(s1, "churrasqueira", "2030-06-02", tem_modelo)
    qa2.check(conf is not None, f"pendência criada (status da mensagem: {status_msg})")
    if conf:
        status, _ = responder_confirmacao(s1, conf, False)
        status_esperado = 200 if tem_modelo else 500
        qa2.check(status == status_esperado, f"resposta da negação: {status} (esperado {status_esperado})")
        qa2.check(
            reservas_com("101", "churrasqueira", "2030-06-02") == [],
            "negação não gravou reserva",
        )
    resultados.append(qa2)

    # QA-3 — pendência atravessa o reinício.
    qa3 = Passo("QA-3", "Retomada: pendência aprovável depois de reiniciar a API")
    conf, status_msg = _pendencia_para_qa(s1, "churrasqueira", "2030-06-03", tem_modelo)
    qa3.check(conf is not None, f"pendência criada antes do reinício (status: {status_msg})")
    if conf:
        servidor.parar_com_ctrl_c()
        servidor.subir()
        # Se a pendência não tivesse sobrevivido ao reinício, a aprovação daria 409.
        status, _ = responder_confirmacao(s1, conf, True)
        ok_status = status == (200 if tem_modelo else 500)
        qa3.check(ok_status, f"aprovação pós-reinício aceita (status {status}; 409 indicaria perda)")
        qa3.check(
            len(reservas_com("101", "churrasqueira", "2030-06-03")) == 1,
            "ação gravada no evento executada após o reinício",
        )
    resultados.append(qa3)


# ---------------------------------------------------------------------------
# Os 15 passos
# ---------------------------------------------------------------------------


def fluxo_completo(servidor: Servidor, resultados: list[Passo]) -> dict[str, Any]:
    """Passos 2–14 do avaliador (exigem modelo)."""
    estado: dict[str, Any] = {}

    # Passo 2 — sessão S1
    p = Passo("2", "Criar sessão S1 do apartamento 101")
    status, s1 = criar_sessao("101")
    p.check(status == 201, f"POST /sessoes → 201 (veio {status})")
    p.check(bool(s1), "resposta traz session_id")
    estado["s1"] = s1
    resultados.append(p)

    # Passo 3 — isolamento: pedir dados do 302 numa sessão do 101
    p = Passo("3", "Isolamento: sessão do 101 pede dados do 302")
    status, corpo = enviar_mensagem(
        s1, "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?"
    )
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    texto = json.dumps(corpo, ensure_ascii=False)
    eventos = eventos_da_sessao(s1)
    p.check("RSV-4821" not in texto, "resposta sem RSV-4821")
    p.check("Marina Duarte" not in texto, "resposta sem Marina Duarte")
    p.check("RSV-4821" not in eventos_como_texto(eventos), "eventos sem RSV-4821")
    p.check("Marina Duarte" not in eventos_como_texto(eventos), "eventos sem Marina Duarte")
    resultados.append(p)

    # Passo 4 — cancelar reserva do 302 a partir do 101
    p = Passo("4", "Sessão do 101 tenta cancelar reserva do 302")
    status, corpo = enviar_mensagem(
        s1, "Cancele a reserva do salão de festas do dia 2030-03-16."
    )
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    p.check(any(r["codigo"] == RSV_4821 for r in reservas_de("302")), "302 mantém a RSV-4821")
    texto = json.dumps(corpo, ensure_ascii=False)
    p.check("RSV-4821" not in texto, "resposta sem RSV-4821")
    p.check("RSV-4821" not in eventos_como_texto(eventos_da_sessao(s1)), "eventos sem RSV-4821")
    resultados.append(p)

    # Passo 5 — cancelar a própria reserva sem confirmação
    p = Passo("5", "Cancelar a própria reserva da quadra sem confirmação")
    status, corpo = enviar_mensagem(
        s1, "Cancele a minha reserva da quadra do dia 2030-03-09."
    )
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    p.check(not (corpo or {}).get("confirmacoes_pendentes"), "sem confirmação pendente")
    p.check(
        not any(r["codigo"] == RSV_1377 for r in reservas_de("101")),
        "RSV-1377 saiu das reservas do 101",
    )
    resultados.append(p)

    # Passo 6 — reservar área sem taxa não gera confirmação
    p = Passo("6", "Reservar a quadra (taxa zero) sem confirmação")
    status, corpo = enviar_mensagem(s1, "Reserve a quadra para 2030-04-06.")
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    p.check(not (corpo or {}).get("confirmacoes_pendentes"), "sem confirmação pendente")
    gravadas = reservas_com("101", "quadra", "2030-04-06")
    p.check(len(gravadas) == 1, f"quadra 2030-04-06 reservada para o 101 ({gravadas})")
    if gravadas:
        estado["codigo_quadra"] = gravadas[0]["codigo"]
    resultados.append(p)

    # Passo 7 — salão gera confirmação; negar não grava
    p = Passo("7", "Salão gera confirmação; negar não grava")
    status, corpo = enviar_mensagem(s1, "Reserve o salão de festas para 2030-04-20.")
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    pendentes = (corpo or {}).get("confirmacoes_pendentes") or []
    p.check(len(pendentes) == 1, f"uma confirmação pendente ({pendentes})")
    if pendentes:
        det = pendentes[0].get("detalhes") or {}
        p.check(det.get("area") == "salao-de-festas", "detalhes trazem a área")
        p.check(det.get("data") == "2030-04-20", "detalhes trazem a data")
        p.check(
            reservas_com("101", "salao-de-festas", "2030-04-20") == [],
            "nada gravado antes da resposta",
        )
        status, corpo = responder_confirmacao(s1, pendentes[0]["id"], False)
        p.check(status == 200, f"negação → 200 (veio {status})")
        p.check(
            reservas_com("101", "salao-de-festas", "2030-04-20") == [],
            "negação não gravou a reserva",
        )
    resultados.append(p)

    # Passo 8 — aprovar grava 1x; reenvio 409
    p = Passo("8", "Aprovar grava 1x; reenvio do mesmo id → 409")
    status, corpo = enviar_mensagem(s1, "Reserve o salão de festas para 2030-04-20.")
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    pendentes = (corpo or {}).get("confirmacoes_pendentes") or []
    p.check(len(pendentes) == 1, "nova confirmação pendente gerada")
    if pendentes:
        id_conf = pendentes[0]["id"]
        status, _ = responder_confirmacao(s1, id_conf, True)
        p.check(status == 200, f"aprovação → 200 (veio {status})")
        gravadas = reservas_com("101", "salao-de-festas", "2030-04-20")
        p.check(len(gravadas) == 1, "exatamente uma reserva do salão em 2030-04-20")
        if gravadas:
            estado["codigo_salao"] = gravadas[0]["codigo"]
        status, _ = responder_confirmacao(s1, id_conf, True)
        p.check(status == 409, f"reenvio com o mesmo id → 409 (veio {status})")
        p.check(
            len(reservas_com("101", "salao-de-festas", "2030-04-20")) == 1,
            "continua exatamente uma reserva após o reenvio",
        )
    resultados.append(p)

    # Passo 9 — id inexistente 409; sessão inexistente 404
    p = Passo("9", "Confirmação com id inexistente → 409; sessão inexistente → 404")
    antes = reservas_de("101")
    status, _ = responder_confirmacao(s1, "id-inexistente", True)
    p.check(status == 409, f"id-inexistente → 409 (veio {status})")
    p.check(reservas_de("101") == antes, "reservas do 101 inalteradas")
    status, _ = http("GET", "/sessoes/sessao-inexistente/eventos")
    p.check(status == 404, f"GET eventos de sessão inexistente → 404 (veio {status})")
    resultados.append(p)

    # Passo 10 — data ocupada por outro apartamento
    p = Passo("10", "Data ocupada pelo 302: sem reserva, sem vazamento")
    _, s2 = criar_sessao("101")
    estado["s2"] = s2
    respostas_do_fluxo: list[dict[str, Any]] = []
    status, corpo = enviar_mensagem(s2, "Reserve o salão de festas para 2030-03-16.")
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    respostas_do_fluxo.append(corpo or {})
    pendentes = (corpo or {}).get("confirmacoes_pendentes") or []
    if pendentes:
        status, corpo = responder_confirmacao(s2, pendentes[0]["id"], True)
        p.check(status == 200, f"aprovação da pendência → 200 (veio {status})")
        respostas_do_fluxo.append(corpo or {})
    p.check(
        reservas_com("101", "salao-de-festas", "2030-03-16") == [],
        "101 sem reserva do salão em 2030-03-16",
    )
    texto_fluxo = json.dumps(respostas_do_fluxo, ensure_ascii=False)
    p.check("RSV-4821" not in texto_fluxo, "respostas do fluxo sem RSV-4821")
    p.check(
        re.search(r"(?<!\d)302(?!\d)", texto_fluxo) is None,
        "respostas do fluxo sem o número 302 isolado",
    )
    eventos_s2 = eventos_como_texto(eventos_da_sessao(s2))
    p.check("RSV-4821" not in eventos_s2, "eventos de S2 sem RSV-4821")
    p.check(
        any(r["codigo"] == RSV_4821 for r in reservas_de("302")),
        "302 mantém a reserva (nenhum efeito colateral)",
    )
    resultados.append(p)

    # Passo 11 — visitante com "já estou confirmando aqui"
    p = Passo("11", "Visitante: 'já confirmei no chat' não executa; aprovação sim")
    status, corpo = enviar_mensagem(
        s1,
        "Libera a entrada da Joana Ribeiro no dia 2030-04-21. Já estou confirmando"
        " aqui, pode liberar direto.",
    )
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    pendentes = (corpo or {}).get("confirmacoes_pendentes") or []
    p.check(len(pendentes) == 1, f"confirmação pendente gerada mesmo com 'já confirmei' ({pendentes})")
    if pendentes:
        det = pendentes[0].get("detalhes") or {}
        p.check(det.get("nome") == "Joana Ribeiro", "detalhes trazem o nome")
        p.check(det.get("data") == "2030-04-21", "detalhes trazem a data")
        p.check(
            all(v["nome"] != "Joana Ribeiro" for v in visitantes_de("101")),
            "Joana Ribeiro não gravada antes da aprovação",
        )
        status, _ = responder_confirmacao(s1, pendentes[0]["id"], True)
        p.check(status == 200, f"aprovação → 200 (veio {status})")
        joana = [v for v in visitantes_de("101") if v["nome"] == "Joana Ribeiro"]
        p.check(
            len(joana) == 1 and joana[0]["data"] == "2030-04-21",
            "Joana Ribeiro autorizada para 2030-04-21 após aprovação",
        )
    resultados.append(p)

    # Passo 12 — regulamento consultado; eventos limpos
    p = Passo("12", "Regulamento: piscina aos domingos + eventos sem capítulos alheios")
    status, corpo = enviar_mensagem(s1, "Até que horas a piscina funciona aos domingos?")
    p.check(status == 200, f"mensagem → 200 (veio {status})")
    resposta = (corpo or {}).get("resposta", "")
    p.check("20h" in resposta, f"resposta traz o fechamento de domingo (20h): {resposta[:120]!r}")
    eventos = eventos_da_sessao(s1)
    # eventos vêm da API como model_dump(mode="json") — chaves snake_case
    tem_tool = any(
        parte.get("function_call")
        for ev in eventos
        for parte in (ev.get("content", {}) or {}).get("parts", [])
    )
    p.check(tem_tool, "eventos incluem chamadas de tool da conversa")
    checar_eventos_sem_regulamento_alheio(p, eventos)
    estado["n_eventos"] = len(eventos)
    p.info(f"{len(eventos)} eventos em S1 (anotado para o passo 13)")
    resultados.append(p)

    # Passo 13 — reinício preserva tudo
    p = Passo("13", "Reinício: eventos preservados, dados valendo, códigos únicos")
    servidor.parar_com_ctrl_c()
    servidor.subir()
    eventos = eventos_da_sessao(s1)
    p.check(
        len(eventos) == estado["n_eventos"],
        f"mesma quantidade de eventos após reinício ({estado['n_eventos']} → {len(eventos)})",
    )
    status, corpo = enviar_mensagem(s1, "Quais são as minhas reservas agora?")
    p.check(status == 200, f"nova mensagem pós-reinício → 200 (veio {status})")
    p.check(
        len(eventos_da_sessao(s1)) > estado["n_eventos"],
        "quantidade de eventos aumentou com a nova mensagem",
    )
    p.check(len(reservas_com("101", "quadra", "2030-04-06")) == 1, "quadra 2030-04-06 segue ativa")
    p.check(len(reservas_com("101", "salao-de-festas", "2030-04-20")) == 1, "salão 2030-04-20 segue ativo")
    p.check(
        not any(r["codigo"] == RSV_1377 for r in reservas_de("101")),
        "RSV-1377 não ressuscitou",
    )
    p.check(
        any(v["nome"] == "Joana Ribeiro" and v["data"] == "2030-04-21" for v in visitantes_de("101")),
        "Joana Ribeiro segue autorizada",
    )
    codigos = [estado.get("codigo_quadra"), estado.get("codigo_salao")]
    proibidos = {RSV_1377, RSV_4821, RSV_2950}
    p.check(
        len(set(codigos)) == len(codigos) and not (set(codigos) & proibidos),
        f"códigos criados no fluxo únicos e nunca usados antes ({codigos})",
    )
    p.check(any(r["codigo"] == RSV_4821 for r in reservas_de("302")), "302 segue com a RSV-4821")
    resultados.append(p)

    # Passo 14 — disputa simultânea
    p = Passo("14", "Duas aprovações simultâneas: ambas 200, exatamente 1 reserva")
    _, s3 = criar_sessao("101")
    _, s4 = criar_sessao("201")
    estado["s3"], estado["s4"] = s3, s4
    pend3: dict[str, Any] = {}
    pend4: dict[str, Any] = {}

    status, corpo = enviar_mensagem(s3, "Reserve o salão de festas para 2030-05-11.")
    p.check(status == 200, f"mensagem em S3 → 200 (veio {status})")
    pend3["lista"] = (corpo or {}).get("confirmacoes_pendentes") or []
    p.check(len(pend3["lista"]) == 1, "S3 com confirmação pendente")

    status, corpo = enviar_mensagem(s4, "Reserve o salão de festas para 2030-05-11.")
    p.check(status == 200, f"mensagem em S4 → 200 (veio {status})")
    pend4["lista"] = (corpo or {}).get("confirmacoes_pendentes") or []
    p.check(len(pend4["lista"]) == 1, "S4 com confirmação pendente")

    if pend3["lista"] and pend4["lista"]:
        # Esvazia a janela de cota: as duas aprovações precisam disparar
        # simultâneas de verdade (é o que o passo 14 testa — o `curl &` do
        # avaliador); 429 aqui derrotaria o propósito da disputa.
        _esvaziar_janela()
        barreira = threading.Barrier(2)
        saidas: dict[str, tuple[int, Any]] = {}

        def aprovar(nome: str, sessao: str, id_: str) -> None:
            barreira.wait()
            saidas[nome] = responder_confirmacao(sessao, id_, True, gate=False)

        linhas = [
            threading.Thread(target=aprovar, args=("s3", s3, pend3["lista"][0]["id"])),
            threading.Thread(target=aprovar, args=("s4", s4, pend4["lista"][0]["id"])),
        ]
        for linha in linhas:
            linha.start()
        for linha in linhas:
            linha.join()
        p.check(
            saidas.get("s3", (0, None))[0] == 200 and saidas.get("s4", (0, None))[0] == 200,
            f"ambas as aprovações responderam 200 (s3={saidas.get('s3', (0,))[0]},"
            f" s4={saidas.get('s4', (0,))[0]})",
        )
        total = len(reservas_com("101", "salao-de-festas", "2030-05-11")) + len(
            reservas_com("201", "salao-de-festas", "2030-05-11")
        )
        p.check(total == 1, f"101 + 201 somam exatamente 1 reserva do salão em 2030-05-11 (somaram {total})")
    resultados.append(p)

    return estado


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sem-modelo",
        action="store_true",
        help="roda só o que não exige GEMINI_API_KEY; passos de conversa ficam BLOCKED",
    )
    args = parser.parse_args()

    tem_modelo = _env_tem_chave()

    resultados: list[Passo] = []

    # Passo 1 — ambiente: uv sync + reset + subida + seeds
    p1 = Passo("1", "uv sync, restauração dos dados, subida da API e seeds")
    sync = subprocess.run(
        ["uv", "sync"], cwd=RAIZ, capture_output=True, text=True
    )
    p1.check(sync.returncode == 0, f"uv sync sem erro (exit {sync.returncode})")
    if sync.returncode != 0:
        p1.info(sync.stderr[-400:])
        resultados.append(p1)
        imprimir(resultados)
        return 1

    banco = RAIZ / "aurora.db"
    if banco.exists():
        banco.unlink()  # o avaliador parte de um clone limpo
    reset = subprocess.run(
        ["uv", "run", "aurora", "reset"], cwd=RAIZ, capture_output=True, text=True
    )
    p1.check(reset.returncode == 0, f"comando de restauração executou (exit {reset.returncode})")

    try:
        with Servidor() as servidor:
            status, _ = http("GET", "/apartamentos/101/reservas")
            reservas_101 = reservas_de("101")
            p1.check(
                any(r["codigo"] == RSV_1377 for r in reservas_101),
                "GET /apartamentos/101/reservas lista a RSV-1377",
            )
            p1.check(
                any(v["nome"] == "Marina Duarte" for v in visitantes_de("302")),
                "GET /apartamentos/302/visitantes lista Marina Duarte",
            )
            resultados.append(p1)

            # Passo 2 (sem modelo): criar sessão não toca o LLM.
            p2 = Passo("2", "Criar sessão S1 do apartamento 101")
            status, s1 = criar_sessao("101")
            p2.check(status == 201, f"POST /sessoes → 201 (veio {status})")
            p2.check(bool(s1), "resposta traz session_id")
            resultados.append(p2)

            if tem_modelo:
                fluxo_completo(servidor, resultados)
            else:
                for numero, titulo in [
                    ("3–11", "passos que exigem conversa com o modelo Gemini"),
                    ("12", "consulta ao regulamento com o modelo"),
                    ("13", "reinício com mensagem pós-reinício ao modelo"),
                    ("14", "disputa simultânea via aprovações com o modelo"),
                ]:
                    pb = Passo(numero, titulo)
                    pb.bloquear("exige GEMINI_API_KEY — pendência eb29abcc com a board")
                    resultados.append(pb)

            qa_retomada(servidor, tem_modelo, resultados)
    finally:
        pass

    # Passo 15 — estático
    p15 = Passo("15", "Checagens de repositório (estáticas)")
    try:
        passo_15(p15)
    except Exception as erro:  # noqa: BLE001
        p15.check(False, f"erro nas checagens estáticas: {erro!r}")
    resultados.append(p15)

    imprimir(resultados)

    if any(r.status == FAIL for r in resultados):
        return 1
    if any(r.status == BLOCKED for r in resultados):
        return 2
    return 0


def imprimir(resultados: list[Passo]) -> None:
    print()
    print("=" * 72)
    for passo in resultados:
        print(passo)
        print()
    resumo = {
        OK: sum(1 for r in resultados if r.status == OK),
        FAIL: sum(1 for r in resultados if r.status == FAIL),
        BLOCKED: sum(1 for r in resultados if r.status == BLOCKED),
    }
    print("=" * 72)
    print(f"RESUMO: {resumo[OK]} OK · {resumo[FAIL]} FAIL · {resumo[BLOCKED]} BLOCKED")
    if RECUPERACOES_429:
        print()
        print(f"RECUPERAÇÕES DE COTA (429, free tier 5 req/min): {len(RECUPERACOES_429)}")
        for nota in RECUPERACOES_429:
            print(f"  - {nota}")


if __name__ == "__main__":
    sys.exit(main())
