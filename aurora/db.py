"""Camada de dados do Residencial Aurora — SQLite assíncrono via aiosqlite.

Dono: T2 (AMS-1089). Este módulo é a única porta de entrada para reservas,
visitantes e contadores: a API (T4) e as tools ADK (T3) consomem apenas as
funções públicas daqui, com nomes estáveis.

Decisões que sustentam as garantias do desafio:

- **Garantia 5 — exclusividade no instante da gravação**: o índice parcial
  ``ux_reservas_ativas`` torna o par (área, data) único entre reservas com
  status ``'ativa'``. Quem rejeita a duplicidade é o banco, não a aplicação;
  o ``IntegrityError`` é traduzido aqui para ``{"erro": ...}``, então a
  camada de API nunca recebe exceção (nem 500) por conflito.
- **Códigos nunca reusados**: ``codigo`` é PRIMARY KEY e o contador
  ``'reserva'`` é incrementado com ``UPDATE ... RETURNING`` **na mesma
  transação** do ``INSERT``. Linha cancelada permanece na tabela — o par
  (área, data) é liberado, mas o código nunca volta.
- **Escritas concorrentes**: ``BEGIN IMMEDIATE`` + ``busy_timeout`` de 5 s
  serializam transações de escrita (SQLite tem um único writer por vez).
- ``dados/`` é imutável: apenas lido, no primeiro acesso a um banco novo e
  no ``reset()``.
- ``reset()`` **mantém sessões** (decisão da T2): sessões ADK são estado de
  conversa, não de dados; o reset restaura apenas reservas, visitantes e
  contadores. Ids de visitantes continuam crescendo (``AUTOINCREMENT`` nunca
  reusa), o que é aceitável — o critério verificável (C2) é sobre reservas
  e visitantes por apartamento, não sobre ids.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1
_CONTADOR_RESERVA = "reserva"
_CONTADOR_RESERVA_SEED = 4821

_ERRO_DATA_OCUPADA = "data já reservada para esta área"
_ERRO_SEM_RESERVA_ATIVA = "nenhuma reserva ativa encontrada para este apartamento, área e data"

_DDL = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL,
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reservas_ativas
    ON reservas (area, data)
    WHERE status = 'ativa';
CREATE TABLE IF NOT EXISTS visitantes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento  TEXT NOT NULL,
    nome         TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contadores (
    nome  TEXT PRIMARY KEY,
    valor INTEGER NOT NULL
);
"""

_RAIZ_PROJETO = Path(__file__).resolve().parent.parent


def _caminho_banco() -> Path:
    """Extrai o caminho do arquivo SQLite de ``AURORA_DB_URL``.

    Formas aceitas (authority vazia): ``sqlite+aiosqlite:///caminho``
    (relativo) e ``sqlite+aiosqlite:////caminho`` (absoluto).
    """
    url = os.environ.get("AURORA_DB_URL", "sqlite+aiosqlite:///./aurora.db")
    if "://" not in url:
        raise ValueError(f"AURORA_DB_URL sem scheme sqlite: {url!r}")
    scheme, _, resto = url.partition("://")
    if scheme not in ("sqlite", "sqlite+aiosqlite"):
        raise ValueError(f"AURORA_DB_URL com scheme não suportado: {scheme!r}")
    if not resto.startswith("/"):
        raise ValueError(f"AURORA_DB_URL deve ter authority vazia: {url!r}")
    # '/caminho' após a authority vazia é relativo; '//caminho' é absoluto.
    # Ambos perdem exatamente a barra que separa a authority do path.
    return Path(resto[1:])


def _caminho_dados(arquivo: str) -> Path:
    """Resolve ``dados/<arquivo>``: primeiro no CWD, depois na raiz do projeto."""
    no_cwd = Path.cwd() / "dados" / arquivo
    if no_cwd.exists():
        return no_cwd
    return _RAIZ_PROJETO / "dados" / arquivo


@asynccontextmanager
async def _conectar() -> AsyncIterator[aiosqlite.Connection]:
    """Abre conexão em modo autocommit com ``busy_timeout`` e rows por nome."""
    db = await aiosqlite.connect(_caminho_banco(), isolation_level=None)
    try:
        await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        db.row_factory = aiosqlite.Row
        yield db
    finally:
        await db.close()


async def _popular_de_dados(db: aiosqlite.Connection) -> None:
    """Insere os seeds de ``dados/*.json``; transação fica a cargo do chamador."""
    reservas = json.loads(_caminho_dados("reservas.json").read_text(encoding="utf-8"))
    visitantes = json.loads(_caminho_dados("visitantes.json").read_text(encoding="utf-8"))
    await db.executemany(
        "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
        [(r["codigo"], r["apartamento"], r["area"], r["data"]) for r in reservas],
    )
    await db.executemany(
        "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
        [(v["apartamento"], v["nome"], v["data"]) for v in visitantes],
    )
    await db.execute(
        "INSERT INTO contadores (nome, valor) VALUES (?, ?)",
        (_CONTADOR_RESERVA, _CONTADOR_RESERVA_SEED),
    )


async def _garantir_schema(db: aiosqlite.Connection) -> None:
    """Cria tabelas e índices que faltarem (idempotente)."""
    await db.executescript(_DDL)


async def _inicializar(db: aiosqlite.Connection) -> None:
    """Garante o schema e semeia ``dados/`` em banco recém-criado.

    A marcação ``PRAGMA user_version`` (transacional) distingue banco novo
    (versão 0 → popular seeds) de banco já inicializado: reservas canceladas
    pelo usuário jamais "ressuscitam" num reinício do processo.
    """
    await _garantir_schema(db)
    cursor = await db.execute("PRAGMA user_version")
    (versao,) = await cursor.fetchone()
    if versao != 0:
        return
    await db.execute("BEGIN IMMEDIATE")
    try:
        await _popular_de_dados(db)
        await db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        await db.commit()
    except sqlite3.IntegrityError:
        # corrida de bootstrap: outro processo povoou primeiro — estado já é o
        # desejado, apenas descartamos a transação perdedora.
        await db.rollback()
    except BaseException:
        await db.rollback()
        raise


async def criar_reserva(apartamento: str, area: str, data: str) -> dict[str, Any]:
    """Cria uma reserva ativa com código atômico ``RSV-{contador}``.

    Retorna a reserva criada ou ``{"erro": ...}`` se o par (área, data) já
    tiver reserva ativa — o conflito nunca sobe como exceção (Garantia 5).
    """
    async with _conectar() as db:
        await _inicializar(db)
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "UPDATE contadores SET valor = valor + 1 WHERE nome = ? RETURNING valor",
                (_CONTADOR_RESERVA,),
            )
            linha = await cursor.fetchone()
            if linha is None:
                raise RuntimeError(
                    f"contador {_CONTADOR_RESERVA!r} ausente — rode 'aurora reset'"
                )
            codigo = f"RSV-{int(linha[0])}"
            await db.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data)"
                " VALUES (?, ?, ?, ?)",
                (codigo, apartamento, area, data),
            )
            await db.commit()
        except sqlite3.IntegrityError:
            await db.rollback()
            return {"erro": _ERRO_DATA_OCUPADA}
        return {
            "codigo": codigo,
            "apartamento": apartamento,
            "area": area,
            "data": data,
            "status": "ativa",
        }


async def cancelar_reserva(apartamento: str, area: str, data: str) -> dict[str, Any]:
    """Cancela a reserva ativa do apartamento para o par (área, data).

    A linha permanece com status ``'cancelada'`` (código nunca reusado) e
    sai do índice parcial, liberando a data para nova reserva.
    """
    async with _conectar() as db:
        await _inicializar(db)
        cursor = await db.execute(
            "UPDATE reservas SET status = 'cancelada'"
            " WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'"
            " RETURNING codigo, apartamento, area, data",
            (apartamento, area, data),
        )
        linha = await cursor.fetchone()
        if linha is None:
            return {"erro": _ERRO_SEM_RESERVA_ATIVA}
        return {
            "codigo": linha["codigo"],
            "apartamento": linha["apartamento"],
            "area": linha["area"],
            "data": linha["data"],
            "status": "cancelada",
        }


async def listar_reservas(apartamento: str, so_ativas: bool = True) -> list[dict[str, Any]]:
    """Lista as reservas do apartamento, ativas por padrão, ordenadas por data."""
    sql = "SELECT codigo, apartamento, area, data, status FROM reservas WHERE apartamento = ?"
    if so_ativas:
        sql += " AND status = 'ativa'"
    sql += " ORDER BY data"
    async with _conectar() as db:
        await _inicializar(db)
        cursor = await db.execute(sql, (apartamento,))
        return [dict(linha) for linha in await cursor.fetchall()]


async def verificar_disponibilidade(area: str, data: str) -> bool:
    """Retorna ``True`` se o par (área, data) não tem reserva ativa.

    Leitura de cortesia para UX; a verdade é o INSERT com o índice único —
    entre checar e gravar outra reserva pode entrar, e o banco rejeita.
    """
    async with _conectar() as db:
        await _inicializar(db)
        cursor = await db.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa' LIMIT 1",
            (area, data),
        )
        return await cursor.fetchone() is None


async def listar_visitantes(apartamento: str) -> list[dict[str, Any]]:
    """Lista os visitantes autorizados do apartamento, ordenados por data e nome."""
    async with _conectar() as db:
        await _inicializar(db)
        cursor = await db.execute(
            "SELECT id, apartamento, nome, data FROM visitantes"
            " WHERE apartamento = ? ORDER BY data, nome",
            (apartamento,),
        )
        return [dict(linha) for linha in await cursor.fetchall()]


async def autorizar_visitante(apartamento: str, nome: str, data: str) -> dict[str, Any]:
    """Registra a autorização de um visitante e retorna o registro com id."""
    async with _conectar() as db:
        await _inicializar(db)
        cursor = await db.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?) RETURNING id",
            (apartamento, nome, data),
        )
        (id_,) = await cursor.fetchone()
        return {"id": int(id_), "apartamento": apartamento, "nome": nome, "data": data}


async def reset() -> None:
    """Trunca reservas, visitantes e contadores e repovoa de ``dados/*.json``.

    Sessões de conversa (T3/T4) **não** são tocadas — decisão documentada no
    cabeçalho do módulo.
    """
    async with _conectar() as db:
        await _garantir_schema(db)
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DELETE FROM reservas")
            await db.execute("DELETE FROM visitantes")
            await db.execute("DELETE FROM contadores")
            await _popular_de_dados(db)
            await db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
