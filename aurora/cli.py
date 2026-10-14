"""CLI do projeto — entrypoint ``aurora`` (declarado em ``pyproject.toml``).

Comandos:

- ``aurora serve``: sobe a API em ``http://localhost:8000`` (T4, ``aurora.api``).
- ``aurora reset``: restaura reservas, visitantes e contadores ao estado
  inicial de ``dados/`` (T2, ``aurora.db``). Sessões de conversa são mantidas.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn
from dotenv import load_dotenv

from aurora import db


def _cmd_serve(_args: argparse.Namespace) -> int:
    uvicorn.run("aurora.api:app", host="0.0.0.0", port=8000)
    return 0


def _cmd_reset(_args: argparse.Namespace) -> int:
    asyncio.run(db.reset())
    print("Banco restaurado ao estado inicial de dados/.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurora",
        description="Assistente virtual do Residencial Aurora.",
    )
    subparsers = parser.add_subparsers(dest="comando", required=True)

    serve = subparsers.add_parser(
        "serve",
        help="Sobe a API em http://localhost:8000.",
    )
    serve.set_defaults(acao=_cmd_serve)

    reset = subparsers.add_parser(
        "reset",
        help="Restaura reservas, visitantes e contadores ao estado de dados/ (T2).",
    )
    reset.set_defaults(acao=_cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada do comando ``aurora``."""
    load_dotenv()  # AURORA_DB_URL (e GEMINI_API_KEY na T4) vêm do .env
    args = _build_parser().parse_args(argv)
    return args.acao(args)


if __name__ == "__main__":
    sys.exit(main())
