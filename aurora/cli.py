"""CLI do projeto — entrypoint ``aurora`` (declarado em ``pyproject.toml``).

Comandos:

- ``aurora serve``: sobe a API em ``http://localhost:8000`` (T4, ``aurora.api``).
- ``aurora reset``: restaura reservas e visitantes ao estado inicial de
  ``dados/`` (T2, ``aurora.db``).

Nesta etapa (T1) os comandos apenas registram a chamada; as tarefas donas de
cada módulo preenchem o comportamento real.
"""

from __future__ import annotations

import argparse
import sys


def _cmd_serve(_args: argparse.Namespace) -> int:
    # T4 substitui pela subida real da API (uvicorn + aurora.api).
    print("aurora serve: implementação chega na T4 (aurora.api).")
    return 0


def _cmd_reset(_args: argparse.Namespace) -> int:
    # T2 substitui pela restauração real dos dados (aurora.db).
    print("aurora reset: implementação chega na T2 (aurora.db).")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurora",
        description="Assistente virtual do Residencial Aurora.",
    )
    subparsers = parser.add_subparsers(dest="comando", required=True)

    serve = subparsers.add_parser(
        "serve",
        help="Sobe a API em http://localhost:8000 (implementação na T4).",
    )
    serve.set_defaults(acao=_cmd_serve)

    reset = subparsers.add_parser(
        "reset",
        help="Restaura reservas e visitantes ao estado de dados/ (T2).",
    )
    reset.set_defaults(acao=_cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada do comando ``aurora``."""
    args = _build_parser().parse_args(argv)
    return args.acao(args)


if __name__ == "__main__":
    sys.exit(main())
