"""Runner do ADK com persistência de sessão.

Dono: T4 (AMS-1089). O Runner executa os agentes com o serviço de sessão
persistido, de modo que reinícios não apaguem conversas nem dados (Garantia
3), e trata os eventos de confirmação de tools (Garantia 1).

Decisões que sustentam o contrato da API:

- **Fonte única de pendências: o histórico de eventos** (``session.events``,
  em ordem cronológica — o ``DatabaseSessionService`` devolve assim). Todo
  ``FunctionCall adk_request_confirmation`` sem ``FunctionResponse`` de
  usuário posterior com o mesmo ``id`` está pendente. Essa derivação só
  lê eventos gravados, então sobrevive a reinício tanto quanto a sessão.
- **Retomada pela doc oficial**: a resposta do morador vira um ``Content``
  de usuário com ``FunctionResponse(id=<id da confirmação>,
  name='adk_request_confirmation', response={'confirmed': bool})`` enviado
  como ``new_message`` ao ``Runner.run_async``. O processador do ADK valida
  contra o histórico e re-executa a tool original uma única vez; ``409``
  (id já respondido ou inexistente) é decisão da nossa camada, antes de
  tocar o Runner — ver ``aurora.api``.
- ``USER_ID`` fixo: o contrato do desafio não tem autenticação — o
  apartamento da sessão representa o morador; todas as sessões do app
  compartilham o mesmo usuário no serviço de sessão.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from aurora.agents import root_agent

APP_NAME = "aurora"
USER_ID = "morador"

# Nome da function call de confirmação emitida pelo ADK 2.9.1
# (google.adk.flows.llm_flows.functions.REQUEST_CONFIRMATION_FUNCTION_CALL_NAME).
# Literal próprio para não depender de caminho interno do pacote.
FUNCAO_CONFIRMACAO = "adk_request_confirmation"


def db_url() -> str:
    """URL do banco do serviço de sessão — a mesma de ``AURORA_DB_URL``/``db.py``."""
    return os.environ.get("AURORA_DB_URL", "sqlite+aiosqlite:///./aurora.db")


def criar_runner() -> Runner:
    """Cria o Runner com sessões persistidas na base do condomínio.

    O engine do serviço de sessão usa o mesmo arquivo SQLite das tabelas do
    condomínio (``db.py``). O ``timeout`` de conexão é o busy handler do
    sqlite3 — sem ele, escritas concorrentes (eventos de sessão de um lado,
    reservas do outro, ex.: duas aprovações simultâneas) falham na hora com
    ``database is locked`` em vez de esperar, como o ``db.py`` já faz com
    ``busy_timeout`` de 5 s.
    """
    return Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=DatabaseSessionService(
            db_url(), connect_args={"timeout": 5}
        ),
    )


def pendencias(eventos: list[Event]) -> list[dict[str, Any]]:
    """Deriva as confirmações pendentes da sessão a partir dos eventos.

    Pendente é todo ``FunctionCall adk_request_confirmation`` cujo ``id``
    ainda não apareceu em ``FunctionResponse`` de usuário. O formato segue o
    contrato: ``{"id", "acao", "detalhes"}``, com ``acao``/``detalhes``
    vindos do ``originalFunctionCall`` carregado pelo ADK na chamada.
    """
    pedidas: dict[str, dict[str, Any]] = {}
    respondidas: set[str] = set()
    for evento in eventos:
        if evento.author == "user":
            for resposta in evento.get_function_responses():
                if resposta.name == FUNCAO_CONFIRMACAO and resposta.id:
                    respondidas.add(resposta.id)
        for chamada in evento.get_function_calls():
            if chamada.name != FUNCAO_CONFIRMACAO or not chamada.id:
                continue
            original = (chamada.args or {}).get("originalFunctionCall") or {}
            pedidas[chamada.id] = {
                "id": chamada.id,
                "acao": original.get("name"),
                "detalhes": original.get("args") or {},
            }
    return [p for id_, p in pedidas.items() if id_ not in respondidas]


def _texto(evento: Event) -> str:
    """Concatena os trechos de texto do conteúdo do evento."""
    if not evento.content or not evento.content.parts:
        return ""
    return "".join(p.text for p in evento.content.parts if p.text)


def resposta_da_invocacao(eventos: list[Event]) -> str:
    """Texto do último evento de agente da invocação (``""`` se parou no gate)."""
    for evento in reversed(eventos):
        if evento.author and evento.author != "user":
            texto = _texto(evento)
            if texto:
                return texto
    return ""


async def executar(
    runner: Runner, session_id: str, mensagem: types.Content
) -> dict[str, Any]:
    """Executa uma invocação e devolve o corpo das rotas de mensagens/confirmações.

    Consome todos os eventos da invocação, extrai a resposta do agente e
    re-deriva as pendências da sessão inteira recarregada do serviço — não
    apenas dos eventos novos — porque ``confirmacoes_pendentes`` é o estado
    da sessão no momento da resposta.
    """
    eventos: list[Event] = []
    async for evento in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=mensagem
    ):
        eventos.append(evento)
    sessao = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    return {
        "resposta": resposta_da_invocacao(eventos),
        "confirmacoes_pendentes": pendencias(sessao.events if sessao else []),
    }
