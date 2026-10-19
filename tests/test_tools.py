"""Testes das tools de leitura — AMS-1101.

Cobrem ``listar_meus_visitantes`` com o ``ToolContext`` real do ADK: a lista
vem sempre do apartamento da sessão (Garantia 2 — nunca um parâmetro), o caso
vazio devolve ``{"visitantes": []}`` e sessão sem apartamento dispara
``ValueError`` em vez de consultar o banco.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools import ToolContext

from aurora import db
from aurora.tools import listar_meus_visitantes


@pytest.fixture(autouse=True)
def _banco_temporario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aponta ``AURORA_DB_URL`` para um SQLite temporário por teste."""
    monkeypatch.setenv("AURORA_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/teste.db")


def _tool_context_do(apartamento: str | None) -> ToolContext:
    """ToolContext real com sessão do apartamento dado (``None`` = sem state)."""
    state: dict[str, str] = {"apartamento": apartamento} if apartamento else {}
    sessao = Session(app_name="testes", user_id="morador", id="sessao", state=state)
    return ToolContext(
        InvocationContext(
            invocation_id="inv-teste",
            agent=LlmAgent(name="agente_teste", model="gemini-2.5-flash"),
            session=sessao,
            session_service=InMemorySessionService(),
        )
    )


async def test_lista_visitantes_do_apartamento_da_sessao() -> None:
    await db.autorizar_visitante("101", "Ada Cardoso", "2030-05-01")
    await db.autorizar_visitante("101", "Bruna Alves", "2030-05-02")
    await db.autorizar_visitante("302", "Intruso do 302", "2030-05-01")

    resultado = await listar_meus_visitantes(_tool_context_do("101"))

    nomes = [visitante["nome"] for visitante in resultado["visitantes"]]
    assert nomes == ["Ada Cardoso", "Bruna Alves"]  # ordenação por data
    assert all(v["apartamento"] == "101" for v in resultado["visitantes"])
    assert "Intruso do 302" not in nomes  # visitantes de outro apto não vazam


async def test_apartamento_sem_visitantes_devolve_lista_vazia() -> None:
    resultado = await listar_meus_visitantes(_tool_context_do("101"))
    assert resultado == {"visitantes": []}


async def test_sessao_sem_apartamento_dispara_value_error() -> None:
    with pytest.raises(ValueError, match="sessão sem apartamento"):
        await listar_meus_visitantes(_tool_context_do(None))
