# Aceite do plano CareerPilot

Registro de 13 de novembro de 2026 para a árvore de trabalho `integration-review`. O aceite integral continua aberto.

| Fase | Implementação e evidência | Pendências de aceite |
| --- | --- | --- |
| 1. Dados e importação | Migração `d3f5a7c9e103`, sobre `c2e4a6b8d002`. Importador por seções, evidência e prévia de reparo em `services/profile/`. Testes em `test_phase1_migration.py`, `test_phase2_migration.py`, `test_core_contracts.py` e `test_api_and_connectors.py`. | Completar os casos do guia para falha injetada no backfill, restauração e reparo de registros editados, além da matriz de formatos. |
| 2. Pré-voo e decisões | Tema escuro, status global, diagnóstico das abas, fila, confirmação salarial e preenchimento explícito. Teste Chrome/CDP em `test_browser_end_to_end.py` cobre valor, faixa e lista. | Escopo por organização, persistência das observações além da sessão do processo, validação real dos portais, cobertura de corridas e falhas de escrita e auditoria de acessibilidade. Campos cujo formato ou unidade são ambíguos exigem revisão no Chrome. |
| 3. IA, respostas e validação | Proposta de um fato com consentimento, criptografia em SQLite, expiração, aprovação ou rejeição. Arsenal por assinatura e contexto, variantes, placeholders, oito exemplos de referência e seleção restrita ao ID de uma resposta aprovada com Qwen local. Validação de restrições do controle no Chrome. | Edição e operações múltiplas nas propostas, validação com o modelo Qwen Uncensored em execução e matriz completa de validação de formulários. |
| 4. Busca e currículos | Feedback reversível, ordenação e filtro por compatibilidade, deduplicação por identidade, cancelamento de busca e exportação ATS. Integração cobre troca de idioma, PDF de uma página e Markdown correspondente. | Cobertura das páginas completas de detalhes em todos os coletores, revisão completa dos critérios de pontuação e elegibilidade, medições em 5.000 vagas e teste unificado do roteiro. |

Arquivos da interface em `backend/app/templates/index.html`, `backend/app/static/app.js` e `backend/app/static/styles.css`. Rotas em `backend/app/api/routes.py`, contratos em `backend/app/schemas/`, persistência em `backend/app/models/tables.py` e `alembic/versions/`. Implementações em `backend/app/browser_automation/` e `backend/app/services/{applications,discovery,profile,resume}/`.

## Verificação local

- `./scripts/smoke.sh` executa Ruff, mypy e a suíte pytest, incluindo Chromium e formulários sintéticos.
- `node --check backend/app/static/app.js` verifica a sintaxe do cliente.
- `git diff --check` verifica espaços e marcadores de conflito.

## Validação externa

O endpoint local padrão do CDP não respondeu na verificação anterior. As verificações abaixo exigem a sessão correspondente no perfil do Chrome do CareerPilot e, para a IA remota, um provedor configurado com consentimento para um fato de teste. Os testes automatizados usam instâncias e dados isolados.

| Escopo | Resultado em 13/11/2026 | Observação |
| --- | --- | --- |
| LinkedIn, Gupy, InfoJobs | Bloqueado | A instância CDP padrão não estava acessível. |
| Google e páginas de carreira | Bloqueado | A instância CDP padrão não estava acessível. |
| Inhire, Greenhouse, Lever, Workday, Vagas.com | Bloqueado | A instância CDP padrão não estava acessível para verificação da conta ou organização. |
| Qwen Uncensored e provedor remoto | Bloqueado para validação ao vivo | O arquivo Qwen3.8-27B-Uncensored-noMTP-IQ4_XS.gguf está disponível. O endpoint local de inferência não respondeu e ainda precisa de um servidor compatível com qwen35 e IQ4_XS neste Linux. A integração remota requer um provedor autorizado. |
