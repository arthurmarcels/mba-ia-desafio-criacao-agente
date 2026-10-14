# Regra é regra: o assistente virtual do Residencial Aurora

O Residencial Aurora ganhou um assistente no aplicativo dos moradores. Pelo chat, cada morador reserva o salão de festas, a churrasqueira e a quadra, cancela as próprias reservas, autoriza a entrada de visitantes e tira dúvidas sobre o regulamento interno — com uma condição da síndica: nenhuma mensagem pode convencer o assistente a quebrar regra.

A tensão central do desafio é essa: o modelo conduz a conversa, mas as regras críticas moram no código e valem não importa o que o morador escreva, nem quando dois moradores pedem a mesma coisa no mesmo minuto. Em uma frase: o modelo decide o caminho, o código decide o que é permitido.

- **Stack:** Python 3.12+ · [uv](https://docs.astral.sh/uv/) · Google ADK `2.9.1` (série 2, fixado) · FastAPI · SQLite.
- **Entrega:** uma API em `http://localhost:8000` com um agente principal e dois especialistas, as cinco garantias em código, comando de subida e de restauração.

## Arquitetura

O assistente é construído com Google ADK em torno de uma decisão estrutural: **todo evento de confirmação nasce no agente root**, então a resposta do morador pela rota de confirmações sempre chega ao Runner/agente certo — a armadilha silenciosa do desafio (a rota aceita, nenhum erro aparece e a ação não executa) fica fora de alcance por construção.

```
                         ┌────────────────────────────┐
    POST /sessoes/{id}/  │  aurora (root, mode=chat) │  tools de ESCRITA (todas aqui):
    mensagens      ───▶  │  conduz a conversa         │  tool_criar_reserva · tool_cancelar_reserva
                         └──────┬─────────┬──────────┘  tool_autorizar_visitante
                                │         │
              transferência     │         │     transferência
              (sub_agents)      ▼         ▼     (sub_agents)
                         ┌─────────────┐ ┌────────────────────┐
                         │ especialista│ │ especialista       │  tools de LEITURA (só aqui):
                         │ _reservas   │ │ _regulamento       │  listar_minhas_reservas ·
                         │             │ │                    │  verificar_disponibilidade ·
                         └─────────────┘ └────────────────────┘  consultar_regulamento
```

### Agentes e por que assim

Definição em `aurora/agents.py` (`root_agent`, `especialista_reservas`, `especialista_regulamento`).

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| **`aurora` (principal)** | Conduz a conversa do início ao fim e executa **toda ação de escrita**: reservar, cancelar, autorizar visitante. | É o root — recebe a mensagem do morador via Runner. | O evento `adk_request_confirmation` tem como autor o agente que chamou a tool. Mantendo toda escrita no root, **a retomada da confirmação sempre volta ao agente certo**, mesmo com sessão persistida e pós-reinício. Além disso, o principal não carrega uma linha do regulamento nas instruções (Garantia 4): só sabe que dúvidas de regra vão ao especialista. |
| **`especialista_reservas`** | Consultas de agenda: listar as reservas do apartamento da sessão e verificar se uma área está livre numa data. | Transferência automática pelo principal (`sub_agents`), conforme a instrução. | Especialistas de leitura isolam o contexto: a consulta de disponibilidade devolve apenas `{"livre": bool}` — sem dono, código ou dados de outra unidade (Garantia 2). |
| **`especialista_regulamento`** | Dúvidas sobre o regulamento interno, respondidas **somente** a partir da tool `consultar_regulamento`. | Transferência automática pelo principal (`sub_agents`). | O regulamento é longo; consultá-lo por trecho em um subagente impede que o texto inteiro entre no histórico da sessão e encareça cada mensagem seguinte (Garantia 4). |

### Fluxo de uma mensagem

1. `POST /sessoes/{id}/mensagens` (`aurora/api.py`) carrega a sessão (404 se inexistente) e entrega o texto ao Runner.
2. O Runner (`aurora/runner.py`) executa o `root_agent` com o `DatabaseSessionService` (sessões persistidas no mesmo SQLite do condomínio). Se o modelo transferir para um especialista, a transferência é interna; a resposta volta ao morador pelo mesmo fluxo.
3. Ao chamar uma tool de escrita sujeita a gate, o ADK grava o evento `adk_request_confirmation` (com o `originalFunctionCall` completo) e **para** — a tool não roda.
4. A rota deriva as pendências do histórico de eventos e devolve `confirmacoes_pendentes` na resposta.
5. `POST /sessoes/{id}/confirmacoes` responde com um `FunctionResponse(id=..., name='adk_request_confirmation', response={'confirmed': bool})` — o formato de retomada documentado do ADK. O processador de confirmação valida contra o histórico, deduplica e **re-executa a tool original uma única vez**.

## Garantias

Cada garantia, o arquivo e o trecho que a implementam, e por que não depende do que o modelo decide.

### Garantia 1 — Cobrança ou acesso só com confirmação

- **Onde:** `aurora/tools.py`, linhas 163–171 — os gates no registro das tools:
  - `tool_criar_reserva = FunctionTool(criar_reserva, require_confirmation=_exige_confirmacao_se_taxa)`: a função `_exige_confirmacao_se_taxa` (linhas 75–80) lê a taxa em `dados/areas.json` e exige confirmação **somente quando taxa > 0** — salão (R$ 150) e churrasqueira (R$ 80) cobram; quadra (taxa 0) não.
  - `tool_autorizar_visitante = FunctionTool(autorizar_visitante, require_confirmation=True)`: libera acesso, sempre confirma.
  - `tool_cancelar_reserva = FunctionTool(cancelar_reserva, require_confirmation=False)`: reserva própria sem cobrança, nunca confirma.
- **Pendências e retomada:** `aurora/runner.py` — `pendencias()` (linhas 71–95) deriva as pendências do histórico de eventos gravados; `aurora/api.py`, rota `responder_confirmacao` (linhas 119–151): id fora das pendências → **409 sem tocar o Runner**; o id respondido sai do conjunto, então reenvio também é 409.
- **Por que não depende do modelo:** o gate é avaliado pelo ADK na execução da tool (`FunctionTool.run_async` → `check_require_confirmation`), **antes** de o modelo ver qualquer resposta da tool. O texto da conversa — "já estou confirmando aqui", "pode liberar direto" — nunca é entrada dessa decisão. A negação (`confirmed=false`) faz a tool devolver `{"error": "This tool call is rejected."}` sem executar (semântica do ADK 2.9.1, `google/adk/tools/function_tool.py`), e o ADK só re-executa a tool com a validação tripla em `_resolve_confirmation_targets` (tool registrada + realmente exige confirmação + args idênticos ao histórico).

### Garantia 2 — Cada sessão pertence a um apartamento

- **Onde:** `aurora/tools.py` — **nenhuma tool tem `apartamento` na assinatura** (`criar_reserva(area, data)`, `cancelar_reserva(area, data)`, `autorizar_visitante(nome, data)`, `listar_minhas_reservas()`, `verificar_disponibilidade(area, data)`). O valor vem sempre de `_apartamento_da_sessao(tool_context)` (linhas 64–72), que lê `tool_context.session.state["apartamento"]`.
- **Estado da sessão:** `aurora/api.py`, `criar_sessao` (linhas 87–99) grava `state={"apartamento": apartamento}` **uma única vez** na criação.
- **Vazamento:** `verificar_disponibilidade` (tools.py, linhas 143–156) devolve só `{"livre": bool}`; conflito em `criar_reserva` devolve erro sem revelar de quem é a reserva (`aurora/db.py`, `_ERRO_DATA_OCUPADA`).
- **Por que não depende do modelo:** o parâmetro não existe, então o modelo não tem como escolher — e prompt injection que "muda" o apartamento não altera o state da sessão. "Sou do 302" numa sessão do 101 continua operando sobre o 101.

### Garantia 3 — Nada se perde no reinício

- **Onde:** `aurora/runner.py`, `criar_runner` (linhas 52–68): `DatabaseSessionService(AURORA_DB_URL)` persiste sessões e eventos no mesmo SQLite das reservas. `aurora/api.py` carrega a sessão por id a cada rota — depois do reinício, os eventos anteriores estão lá e a retomada de confirmação segue funcionando (a pendência é derivada dos eventos persistidos, não de memória).
- **Contadores atômicos:** `aurora/db.py`, `criar_reserva` (linhas 172–207): código `RSV-{n}` gerado com `UPDATE ... RETURNING` na mesma transação do `INSERT`; linha cancelada permanece (código nunca reusado); `PRAGMA user_version` distingue banco novo de já inicializado — cancelamento jamais "ressuscita" no reinício.
- **Por que não depende do modelo:** persistência e restauração são do banco; o modelo só conversa. (Validado empiricamente: retomada de confirmação aprovada após SIGINT + re-subida executa a ação gravada no evento.)

### Garantia 4 — O regulamento é consultado, não carregado

- **Onde:** `aurora/regulamento.py` — `consultar_regulamento(topico)` (linhas 175–221) faz o casamento em duas fases (título do capítulo; em seguida palavras-chave nos artigos) e devolve **somente artigos do(s) capítulo(s) do assunto pedido**, com limite de tamanho (`_LIMITE_CARACTERES = 4000`) e de artigos (`_LIMITE_ARTIGOS = 8`).
- **Agente principal limpo:** `aurora/agents.py`, `_INSTRUCAO_PRINCIPAL` — zero conteúdo normativo; só a regra de rotear dúvidas ao especialista. `consultar_regulamento` é tool exclusiva do `especialista_regulamento`, e o resultado da tool entra no histórico **do subagente**, não carregado no root.
- **Por que não depende do modelo:** a restrição é do que a tool devolve (código), não do que o modelo promete. A tool nunca devolve capítulos inteiros nem artigos de assuntos outros.

### Garantia 5 — Dois moradores, uma reserva

- **Onde:** `aurora/db.py` — índice parcial `ux_reservas_ativas` (linhas 56–58 do `_DDL`): `CREATE UNIQUE INDEX ... ON reservas (area, data) WHERE status = 'ativa'`. Quem rejeita a duplicidade é **o banco, no instante do INSERT**, não uma conferência anterior.
- **Transação de escrita:** `criar_reserva` abre `BEGIN IMMEDIATE` + `busy_timeout` 5 s (linhas 172–207); o `IntegrityError` é traduzido para `{"erro": "data já reservada para esta área"}` — a outra aprovação vira resposta normal, sem 500.
- **Runner concorrente:** `aurora/runner.py`, `criar_runner` — `connect_args={"timeout": 5}` no serviço de sessão para que escritas de eventos concorrentes esperem em vez de falhar com `database is locked`.
- **Por que não depende do modelo:** a exclusividade vale no banco, no momento da gravação, com writers serializados. Entre a conferência da agenda e o INSERT, a outra reserva pode entrar — e o banco ainda rejeita a segunda.

## Como rodar

Pré-requisitos: Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
# 1. Dependências
uv sync

# 2. Variáveis de ambiente
cp .env.example .env    # preencha GEMINI_API_KEY com sua chave do Google AI Studio
                       # (AURORA_MODEL e AURORA_DB_URL têm padrões; ver tabela)

# 3. Restaurar os dados iniciais (reservas, visitantes e contadores de dados/)
uv run aurora reset

# 4. Subir a API em http://localhost:8000
uv run aurora serve
```

Comandos:

| Comando | Efeito |
|---|---|
| `uv sync` | Instala o projeto e dependências (ADK fixado em 2.9.1). |
| `uv run aurora reset` | Restaura reservas, visitantes e contadores ao estado de `dados/*.json`. Não toca nas sessões de conversa. |
| `uv run aurora serve` | Sobe a API em `http://localhost:8000` (uvicorn). |
| `uv run python scripts/validar_e2e.py` | Harness de validação E2E dos 15 passos do avaliador (`--sem-modelo` para tudo que não exige chave). |

Variáveis do `.env`:

| Variável | Padrão | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | — | Chave do Google AI Studio (obrigatória para conversar; sem ela as rotas de verificação e a criação de sessão funcionam, e as mensagens devolvem 500 com causa clara). |
| `AURORA_MODEL` | `gemini-2.5-flash` | Modelo Gemini usado pelos três agentes. |
| `AURORA_DB_URL` | `sqlite+aiosqlite:///./aurora.db` | Banco único para reservas/visitantes/contadores e sessões ADK. |

O banco (`aurora.db`) nasce no primeiro acesso com os seeds de `dados/`; o `.gitignore` o mantém fora do repositório. Os arquivos de `dados/` são somente leitura para a aplicação e permanecem idênticos aos do repositório base.

## Validação

O harness `scripts/validar_e2e.py` executa o fluxo do avaliador (passos 1–15) contra a API real e também o cenário de risco nº 1 (retomada de confirmação com sessão persistida e pós-reinício). Sem `GEMINI_API_KEY`, os passos que exigem modelo são reportados como `BLOCKED` — o resto roda por inteiro. Vereditos por execução no card T5 (AMS-1092).

**Chave em free tier:** a cota do `gemini-2.5-flash` no free tier tem dois andares — 5 requisições/minuto e 20 requisições/**dia** por projeto. Cada turno de conversa faz 1–3 chamadas ao modelo, então uma rodada completa (≈60 chamadas) não cabe na cota diária do 2.5-flash. Por isso o harness espaça os turnos (padrão 60 s; `AURORA_PACING_S` ajusta) e, se um 429 `RESOURCE_EXHAUSTED` por minuto ainda escapar, recupera de forma segura: em mensagens reenvia o mesmo texto (o histórico persistido impede execução duplicada de tool); em confirmações — onde a ação já executou e o id foi consumido — fecha o ciclo com um turno de acompanhamento na mesma sessão, registrado no relatório final como recuperação de cota; 429 da cota **diária** é reportado como falha real do ambiente, com recomendação de trocar o modelo. As cotas são por modelo: esgotando o dia do 2.5-flash, `AURORA_MODEL=gemini-3.5-flash-lite` (verificado nesta entrega) executa a rodada completa pela mesma integração ADK. Nunca repete uma confirmação pelo mesmo id: o reenvio legítimo daria 409.
