"""Consulta ao regulamento interno (``dados/regulamento.md``).

Dono: T3 (AMS-1089). O regulamento é longo e não entra nas instruções do
agente principal: este módulo implementa a consulta por assunto usada pelo
especialista de regulamento, devolvendo só o trecho pertinente (Garantia 4).

Decisões que sustentam a Garantia 4:

- **Parse único e lazy**: o arquivo é lido e fatiado em capítulos/artigos uma
  única vez, sob demanda; chamadas seguintes usam o cache em memória.
- **Duas fases de casamento**: primeiro o tópico contra os títulos dos
  capítulos; sem casamento, varredura por palavra-chave nos artigos.
- **Somente o assunto pedido**: o resultado traz apenas artigos do(s)
  capítulo(s) casado(s) — nunca capítulos inteiros de outros assuntos —
  afinado por pontuação de palavras-chave e limitado em tamanho, para não
  inflar o histórico da sessão (e o custo por mensagem).
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any

from aurora.db import caminho_dados

_LIMITE_CARACTERES = 4000
_LIMITE_ARTIGOS = 8

# Palavras funcionais e interrogativas que não distinguem assunto.
_STOPWORDS = frozenset(
    """a o e é de da do das dos ao aos à às no na nos nas em um uma uns umas
    por para com sem sobre que qual quais quando onde como quanto se seu seus
    sua suas meu minha meus minhas ate este esta esse essa isso ele ela eu
    voce você vocês nos tenho tem tenho ser sao são pode posso precisa
   """.split()
)

_RE_CAPITULO = re.compile(r"^##\s+Capítulo\s+([IVXLCDM]+)\s*:\s*(.+?)\s*$")
_RE_ARTIGO = re.compile(r"^\*\*Art\.\s*(\d+)\s*(º|\.)\s*\*\*\s*(.*)$")


@dataclass(frozen=True)
class Artigo:
    """Um artigo do regulamento, com parágrafos e incisos no ``texto``."""

    rotulo: str  # "Art. 22."
    texto: str


@dataclass(frozen=True)
class Capitulo:
    """Um capítulo do regulamento, identificado por rótulo e título."""

    rotulo: str  # "Capítulo IV: Piscina"
    titulo: str  # "Piscina"
    artigos: tuple[Artigo, ...]


_capitulos: list[Capitulo] | None = None
_lock = threading.Lock()


def _normalizar(texto: str) -> str:
    """Minúsculas sem acentos, para casar "Salão" com "salao"."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).lower()


def _palavras_chave(texto: str) -> set[str]:
    """Extrai palavras normalizadas, sem stopwords nem termos curtos."""
    brutas = re.split(r"[^a-z0-9çãáàâéêíóôõúü]+", _normalizar(texto))
    return {
        palavra
        for palavra in brutas
        if len(palavra) >= 3 and palavra not in _STOPWORDS
    }


def _analisar(conteudo: str) -> list[Capitulo]:
    """Fatiar o markdown em capítulos e artigos numerados."""
    capitulos: list[Capitulo] = []
    capitulo_atual: Capitulo | None = None
    artigo_atual: Artigo | None = None
    linhas_do_artigo: list[str] = []

    def fechar_artigo() -> None:
        nonlocal artigo_atual, linhas_do_artigo
        if artigo_atual is not None and capitulo_atual is not None:
            texto = "\n".join(linhas_do_artigo).strip()
            artigo = Artigo(rotulo=artigo_atual.rotulo, texto=texto)
            capitulo_atual.artigos.append(artigo)  # type: ignore[union-attr]
        artigo_atual = None
        linhas_do_artigo = []

    for linha in conteudo.splitlines():
        m_cap = _RE_CAPITULO.match(linha)
        if m_cap:
            fechar_artigo()
            capitulo_atual = Capitulo(
                rotulo=f"Capítulo {m_cap.group(1)}: {m_cap.group(2)}",
                titulo=m_cap.group(2),
                artigos=[],
            )
            capitulos.append(capitulo_atual)
            continue
        m_art = _RE_ARTIGO.match(linha)
        if m_art:
            fechar_artigo()
            numero, marca, inicio = m_art.groups()
            sufixo = "º" if marca == "º" else "."
            artigo_atual = Artigo(rotulo=f"Art. {numero}{sufixo}", texto="")
            linhas_do_artigo = [inicio]
            continue
        if artigo_atual is not None:
            if linha.strip():
                linhas_do_artigo.append(linha.strip())
    fechar_artigo()
    return [
        Capitulo(rotulo=c.rotulo, titulo=c.titulo, artigos=tuple(c.artigos))
        for c in capitulos
    ]


def _garantir_capitulos() -> list[Capitulo]:
    """Carrega e fatia o regulamento uma única vez (lazy, thread-safe)."""
    global _capitulos
    if _capitulos is None:
        with _lock:
            if _capitulos is None:
                conteudo = caminho_dados("regulamento.md").read_text(
                    encoding="utf-8"
                )
                _capitulos = _analisar(conteudo)
    return _capitulos


def _pontuar(texto: str, palavras: set[str]) -> int:
    """Quantas palavras-chave do tópico aparecem no texto (plural incluso)."""
    normalizado = _normalizar(texto)
    return sum(1 for palavra in palavras if palavra in normalizado)


def _responder(
    topico: str,
    capitulos_casados: list[Capitulo],
    artigos: list[tuple[Capitulo, Artigo]],
) -> dict[str, Any]:
    resposta: dict[str, Any] = {
        "topico": topico,
        "capitulos": [c.rotulo for c in capitulos_casados],
        "artigos": [],
    }
    if not artigos:
        resposta["mensagem"] = (
            "Nenhum artigo do regulamento foi encontrado para este tópico."
        )
        return resposta
    tamanho = 0
    for capitulo, artigo in artigos[:_LIMITE_ARTIGOS]:
        if tamanho + len(artigo.texto) > _LIMITE_CARACTERES:
            resposta["mensagem"] = (
                "Resultado limitado aos artigos mais pertinentes ao tópico."
            )
            break
        resposta["artigos"].append(
            {"artigo": artigo.rotulo, "texto": artigo.texto}
        )
        tamanho += len(artigo.texto)
    return resposta


def consultar_regulamento(topico: str) -> dict[str, Any]:
    """Consulta o regulamento interno por assunto e devolve só os artigos que tratam dele.

    Nunca devolve capítulos inteiros nem artigos de assuntos outros: o
    casamento é contra títulos de capítulos e, em segunda fase, por
    palavra-chave nos artigos. É a única fonte de normas para os agentes —
    nenhum agente responde regra de memória.

    Args:
        topico: assunto procurado, com as palavras-chave da dúvida
            (ex.: "piscina domingos", "mudança horário").
    """
    palavras = _palavras_chave(topico or "")
    if not palavras:
        return _responder(topico, [], [])

    capitulos = _garantir_capitulos()

    # Fase 1: tópico contra os títulos dos capítulos. O assunto localizado
    # pelo título responde com os artigos daquele capítulo, mesmo sem
    # repetir a palavra do título.
    casados = [
        capitulo
        for capitulo in capitulos
        if any(palavra in _normalizar(capitulo.titulo) for palavra in palavras)
    ]
    if casados:
        return _responder(topico, casados, [
            (capitulo, artigo) for capitulo in casados for artigo in capitulo.artigos
        ])

    # Fase 2: nenhuma casou com título — varredura por palavra-chave nos
    # artigos. Sem nenhuma palavra-chave presente, o assunto não existe no
    # regulamento e a resposta é vazia.
    pontuados = [
        (_pontuar(artigo.texto, palavras), capitulo, artigo)
        for capitulo in capitulos
        for artigo in capitulo.artigos
    ]
    maximo = max((pontuacao for pontuacao, _, _ in pontuados), default=0)
    if maximo == 0:
        return _responder(topico, [], [])
    return _responder(topico, casados, [
        (capitulo, artigo)
        for pontuacao, capitulo, artigo in pontuados
        if pontuacao == maximo
    ])
