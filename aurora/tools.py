"""Tools de reservas e visitantes para os agentes ADK.

Dono: T3 (AMS-1089). Reservas e visitantes são lidos e gravados sempre por
estas tools, com o apartamento vindo da sessão — nunca de um valor escolhido
pelo modelo (Garantia 2). Ações que geram cobrança ou liberam acesso ficam
pendentes até a confirmação pela rota da API (Garantia 1).

Como as garantias estão no código, não no prompt:

- **Garantia 2 — nenhuma tool aceita ``apartamento``**: o parâmetro não
  existe em nenhuma assinatura, então o modelo não tem como escolhê-lo. Toda
  tool lê ``tool_context.session.state["apartamento"]`` — valor definido uma
  única vez na criação da sessão (T4) e imune a prompt injection.
- **Garantia 1 — gates no registro da tool**: ``criar_reserva`` é
  ``FunctionTool`` com ``require_confirmation`` **callable** que cobra
  confirmação somente quando a taxa da área (``dados/areas.json``) é maior
  que zero; a decisão é da taxa, nunca do texto da conversa.
  ``autorizar_visitante`` exige confirmação sempre (libera acesso);
  ``cancelar_reserva`` nunca (reserva própria, sem cobrança).
- **Sanitização de saída**: ``verificar_disponibilidade`` devolve apenas
  ``{"livre": bool}`` — sem apartamento, morador ou código da reserva
  existente. Conflito em ``criar_reserva`` devolve erro sem revelar de quem
  é a reserva que ocupa a data.

Todas as tools de escrita chamam as funções async de ``aurora.db`` (T2),
única porta para os dados do condomínio.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from google.adk.tools import FunctionTool, ToolContext

from aurora import db
from aurora.db import caminho_dados

_areas: dict[str, dict[str, Any]] | None = None
_lock = threading.Lock()

_CHAVE_APARTAMENTO = "apartamento"


def _garantir_areas() -> dict[str, dict[str, Any]]:
    """Carrega ``dados/areas.json`` uma única vez, indexado por ``id``."""
    global _areas
    if _areas is None:
        with _lock:
            if _areas is None:
                registros = json.loads(
                    caminho_dados("areas.json").read_text(encoding="utf-8")
                )
                _areas = {registro["id"]: registro for registro in registros}
    return _areas


def _taxa_da_area(area: str) -> float:
    """Taxa da área; área desconhecida conta como taxa zero (sem cobrança)."""
    return float(_garantir_areas().get(area, {}).get("taxa", 0) or 0)


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
    """Apartamento autenticado da sessão (Garantia 2) — único fonte possível."""
    apartamento = tool_context.session.state.get(_CHAVE_APARTAMENTO)
    if not apartamento:
        raise ValueError(
            "sessão sem apartamento associado; crie a sessão com o"
            " apartamento do morador"
        )
    return str(apartamento)


def _exige_confirmacao_se_taxa(area: str, **_: Any) -> bool:
    """Gate de cobrança (Garantia 1): confirma somente se a taxa da área > 0.

    Recebe os mesmos argumentos da chamada da tool; só a ``area`` decide.
    """
    return _taxa_da_area(area) > 0


async def criar_reserva(
    area: str, data: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Cria uma reserva do apartamento da sessão para a área comum na data.

    Gera cobrança quando a área tem taxa maior que zero; nesse caso a
    execução aguarda confirmação do morador pela rota de confirmações.

    Args:
        area: id da área comum ("salao-de-festas", "churrasqueira" ou
            "quadra").
        data: data da reserva no formato AAAA-MM-DD.
    """
    if area not in _garantir_areas():
        return {"erro": f"área desconhecida: {area}"}
    return await db.criar_reserva(_apartamento_da_sessao(tool_context), area, data)


async def cancelar_reserva(
    area: str, data: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Cancela a reserva ativa do próprio apartamento para a área na data.

    Sem confirmação: é sempre reserva do apartamento da sessão e não gera
    cobrança. Se não houver reserva correspondente, devolve erro sem
    revelar código ou dono de outra reserva.

    Args:
        area: id da área comum da reserva a cancelar.
        data: data da reserva no formato AAAA-MM-DD.
    """
    return await db.cancelar_reserva(
        _apartamento_da_sessao(tool_context), area, data
    )


async def autorizar_visitante(
    nome: str, data: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Registra a autorização de entrada de um visitante na data.

    Libera acesso ao prédio, então a execução sempre aguarda confirmação
    do morador pela rota de confirmações — mesmo que ele diga na conversa
    que já confirmou.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    return await db.autorizar_visitante(
        _apartamento_da_sessao(tool_context), nome, data
    )


async def listar_minhas_reservas(tool_context: ToolContext) -> dict[str, Any]:
    """Lista as reservas ativas do apartamento da sessão, ordenadas por data."""
    reservas = await db.listar_reservas(_apartamento_da_sessao(tool_context))
    return {"reservas": reservas}


async def listar_meus_visitantes(tool_context: ToolContext) -> dict[str, Any]:
    """Lista os visitantes autorizados pelo apartamento da sessão.

    São os visitantes cuja entrada o próprio apartamento autenticado na
    sessão autorizou, ordenados por data da visita e nome.
    """
    visitantes = await db.listar_visitantes(_apartamento_da_sessao(tool_context))
    return {"visitantes": visitantes}


async def verificar_disponibilidade(
    area: str, data: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Verifica se a área comum está livre na data.

    Devolve apenas se está livre ou ocupada — nunca de quem é a reserva
    existente, nem o código dela.

    Args:
        area: id da área comum a consultar.
        data: data a consultar no formato AAAA-MM-DD.
    """
    livre = await db.verificar_disponibilidade(area, data)
    return {"livre": livre}


# Ferramentas de escrita do agente principal, com os gates de confirmação
# (Garantia 1). As de leitura ficam como funções puras: os especialistas as
# recebem direto e nunca gravam nada.

tool_criar_reserva = FunctionTool(
    criar_reserva, require_confirmation=_exige_confirmacao_se_taxa
)
tool_cancelar_reserva = FunctionTool(
    cancelar_reserva, require_confirmation=False
)
tool_autorizar_visitante = FunctionTool(
    autorizar_visitante, require_confirmation=True
)
