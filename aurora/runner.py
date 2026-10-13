"""Runner do ADK com persistência de sessão.

Dono: T4 (AMS-1089). O Runner executa os agentes com o serviço de sessão
persistido, de modo que reinícios não apaguem conversas nem dados (Garantia
3), e trata os eventos de confirmação de tools (Garantia 1).
"""
