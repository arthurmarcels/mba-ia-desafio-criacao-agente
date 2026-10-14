"""API FastAPI com o contrato do enunciado.

Dono: T4 (AMS-1089). Rotas de sessões, mensagens, confirmações, eventos e
verificação (apartamentos), respondendo em ``http://localhost:8000``.
Subida pelo comando ``aurora serve``.

Decisões de contrato:

- **404 antes de qualquer outra coisa**: toda rota com ``{session_id}``
  carrega a sessão primeiro; inexistente → ``404`` sem tocar o Runner.
- **409 é desta camada**: na rota de confirmações, id que não está entre as
  pendências derivadas dos eventos (nunca existiu ou já foi respondido)
  → ``409`` sem executar nada. O ADK nunca vê a resposta.
- **Sem ``GEMINI_API_KEY``**: a conversa real falha na chamada do modelo —
  a rota devolve ``500`` com mensagem clara em vez de derrubar o servidor.
- Corpos de erro no formato padrão do FastAPI (``{"detail": ...}``);
  o contrato do enunciado especifica status, não shape de erro.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google.adk.sessions import Session
from google.genai import types
from pydantic import BaseModel

from aurora import db, runner as runner_mod
from aurora.db import caminho_dados

load_dotenv()  # AURORA_DB_URL antes da criação do Runner, se .env existir

runner = runner_mod.criar_runner()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Deixa a base pronta antes da primeira requisição: seeds + tabelas ADK."""
    await db.inicializar()
    await runner.session_service.prepare_tables()
    yield


app = FastAPI(
    title="Assistente virtual do Residencial Aurora",
    version="0.1.0",
    lifespan=lifespan,
)


class CriarSessao(BaseModel):
    apartamento: str


class EnviarMensagem(BaseModel):
    texto: str


class ResponderConfirmacao(BaseModel):
    id: str
    confirmado: bool


def _apartamentos_validos() -> set[str]:
    registros = json.loads(
        caminho_dados("apartamentos.json").read_text(encoding="utf-8")
    )
    return {registro["numero"] for registro in registros}


async def _carregar_sessao(session_id: str) -> Session:
    """Carrega a sessão; inexistente → ``404`` antes de qualquer efeito."""
    sessao = await runner.session_service.get_session(
        app_name=runner_mod.APP_NAME,
        user_id=runner_mod.USER_ID,
        session_id=session_id,
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="sessão não encontrada")
    return sessao


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: CriarSessao) -> dict[str, str]:
    if corpo.apartamento not in _apartamentos_validos():
        raise HTTPException(
            status_code=422,
            detail=f"apartamento desconhecido: {corpo.apartamento}",
        )
    sessao = await runner.session_service.create_session(
        app_name=runner_mod.APP_NAME,
        user_id=runner_mod.USER_ID,
        state={"apartamento": corpo.apartamento},
    )
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: EnviarMensagem) -> dict[str, Any]:
    await _carregar_sessao(session_id)  # 404 antes de qualquer efeito
    mensagem = types.Content(
        role="user", parts=[types.Part(text=corpo.texto)]
    )
    try:
        return await runner_mod.executar(runner, session_id, mensagem)
    except HTTPException:
        raise
    except Exception as erro:  # sem GEMINI_API_KEY a falha é aqui, na chamada do modelo
        raise HTTPException(
            status_code=500,
            detail=f"falha na execução do agente: {erro}",
        ) from erro


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(
    session_id: str, corpo: ResponderConfirmacao
) -> dict[str, Any]:
    sessao = await _carregar_sessao(session_id)  # 404 antes de qualquer efeito
    pendentes = {p["id"] for p in runner_mod.pendencias(sessao.events)}
    if corpo.id not in pendentes:
        raise HTTPException(
            status_code=409,
            detail=f"confirmação pendente não encontrada com o id: {corpo.id}",
        )
    # Payload de retomada documentado pelo ADK para resposta remota.
    mensagem = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=corpo.id,
                    name=runner_mod.FUNCAO_CONFIRMACAO,
                    response={"confirmed": corpo.confirmado},
                )
            )
        ],
    )
    try:
        return await runner_mod.executar(runner, session_id, mensagem)
    except HTTPException:
        raise
    except Exception as erro:
        raise HTTPException(
            status_code=500,
            detail=f"falha na execução do agente: {erro}",
        ) from erro


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    sessao = await _carregar_sessao(session_id)
    return [evento.model_dump(mode="json", exclude_none=True) for evento in sessao.events]


@app.get("/apartamentos/{apartamento}/reservas")
async def reservas_do_apartamento(apartamento: str) -> list[dict[str, str]]:
    registros = await db.listar_reservas(apartamento)
    return [
        {"codigo": r["codigo"], "area": r["area"], "data": r["data"]}
        for r in registros
    ]


@app.get("/apartamentos/{apartamento}/visitantes")
async def visitantes_do_apartamento(apartamento: str) -> list[dict[str, str]]:
    registros = await db.listar_visitantes(apartamento)
    return [{"nome": r["nome"], "data": r["data"]} for r in registros]
