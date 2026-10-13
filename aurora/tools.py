"""Tools de reservas e visitantes para os agentes ADK.

Dono: T3 (AMS-1089). Reservas e visitantes são lidos e gravados sempre por
estas tools, com o apartamento vindo da sessão — nunca de um valor escolhido
pelo modelo (Garantia 2). Ações que geram cobrança ou liberam acesso ficam
pendentes até a confirmação pela rota da API (Garantia 1).
"""
