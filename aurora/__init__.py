"""Aurora — assistente virtual do Residencial Aurora.

Assistente construído com Google ADK (agente principal + especialistas),
exposto por uma API FastAPI conforme o contrato do enunciado.

Módulos e seus donos (tarefas do AMS-1089):

- ``cli``: entrypoint ``aurora`` com os comandos ``serve`` e ``reset``.
- ``db``: acesso aos dados do condomínio (T2).
- ``regulamento``: consulta ao regulamento interno (T3).
- ``tools``: tools de reservas e visitantes dos agentes (T3).
- ``agents``: agente principal e especialistas (T3).
- ``runner``: Runner do ADK com sessão persistida (T4).
- ``api``: API FastAPI com o contrato do enunciado (T4).
"""

__version__ = "0.1.0"
