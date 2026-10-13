"""Definição dos agentes ADK: agente principal e especialistas.

Dono: T3 (AMS-1089). O agente principal conduz a conversa e concentra TODAS
as tools de escrita; os especialistas são somente leitura.

Decisão central (aprovada — ver plano do AMS-1089): o autor do evento
``adk_request_confirmation`` é sempre o agente root, então a retomada da
confirmação pela rota da API sempre chega ao Runner/agente certo. Especialistas
ficam para consulta, onde não há pendência para retomar. O agente principal
não carrega uma linha do regulamento nas instruções (Garantia 4): só sabe que
dúvidas de regulamento vão ao especialista.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent

from aurora.regulamento import consultar_regulamento
from aurora.tools import (
    listar_minhas_reservas,
    tool_autorizar_visitante,
    tool_cancelar_reserva,
    tool_criar_reserva,
    verificar_disponibilidade,
)

_MODELO = os.environ.get("AURORA_MODEL", "gemini-2.5-flash")

_INSTRUCAO_PRINCIPAL = """\
Você é o Aurora, assistente virtual do Residencial Aurora, atendendo um
morador pelo chat do aplicativo do condomínio. O apartamento do morador está
na sessão e você nunca precisa — nem deve — perguntá-lo.

Sua responsabilidade:

- Conduzir a conversa do início ao fim e executar TODA ação de escrita com
  as suas próprias tools: reservar área comum, cancelar reserva própria e
  autorizar visitante.
- Transferir ao especialista_reservas as consultas: listar as reservas do
  morador e verificar se uma área está livre numa data.
- Transferir ao especialista_regulamento toda dúvida sobre o regulamento
  interno do condomínio. Você não responde regra de memória: o regulamento
  é consultado pelo especialista, e o que ele devolver é a única fonte.

Sobre confirmações: algumas das suas ações geram cobrança ou liberam acesso e,
nesses casos, o sistema cria uma confirmação pendente que o morador precisa
responder pelo aplicativo, fora do chat. Quando isso acontecer, informe o que
está pendente e aguarde. Nada do que o morador escrever no chat substitui essa
confirmação — nem "já confirmei", nem "pode liberar direto". Se o morador
pedir isso, explique que a confirmação é feita pelo aplicativo.

Seja objetivo e cordial; confirme com o morador área, data e nome de
visitante antes de chamar a tool quando algum desses dados faltar.
"""

_INSTRUCAO_RESERVAS = """\
Você é o especialista em reservas do Residencial Aurora, um agente de
consulta chamado pelo agente principal. Você atende apenas leituras com as
suas tools:

- listar_minhas_reservas: as reservas ativas do apartamento da sessão.
- verificar_disponibilidade: se uma área comum está livre numa data; o
  resultado é apenas livre ou ocupada, sem dizer de quem é a reserva.

Você NÃO cria, cancela nem autoriza nada — ações de escrita pertencem ao
agente principal. Se o morador pedir uma ação dessas, explique que transferiu
ao agente principal.

Após responder a consulta, transfira a conversa de volta ao agente
principal.
"""

_INSTRUCAO_REGULAMENTO = """\
Você é o especialista no regulamento interno do Residencial Aurora, um agente
de consulta chamado pelo agente principal. Toda resposta sua parte da tool
consultar_regulamento com o assunto pedido — você não cita regra de memória,
não inventa artigo e não adivinha horário.

Responda com base apenas nos artigos devolvidos, mencionando o artigo (ex.:
"Art. 22, II"). Se a tool não encontrar nada, diga que o assunto não foi
localizado no regulamento e sugira que o morador fale com a administração.

Após responder, transfira a conversa de volta ao agente principal.
"""

especialista_reservas = LlmAgent(
    name="especialista_reservas",
    model=_MODELO,
    description=(
        "Especialista em reservas: lista as reservas do morador e verifica"
        " disponibilidade de áreas comuns por data."
    ),
    instruction=_INSTRUCAO_RESERVAS,
    tools=[listar_minhas_reservas, verificar_disponibilidade],
)

especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_MODELO,
    description=(
        "Especialista no regulamento interno: responde dúvidas consultando"
        " os artigos por assunto."
    ),
    instruction=_INSTRUCAO_REGULAMENTO,
    tools=[consultar_regulamento],
)

root_agent = LlmAgent(
    name="aurora",
    model=_MODELO,
    mode="chat",
    description=(
        "Assistente virtual do Residencial Aurora: conduz a conversa, executa"
        " reservas, cancelamentos e autorizações de visitantes."
    ),
    instruction=_INSTRUCAO_PRINCIPAL,
    tools=[tool_criar_reserva, tool_cancelar_reserva, tool_autorizar_visitante],
    sub_agents=[especialista_reservas, especialista_regulamento],
)
