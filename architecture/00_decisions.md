# CareerPilot — decisões (2026-11-10)

Estado: arquitetura proposta, sem implementação recebida. Ambiente inicial vazio, Git e dois worktrees criados a partir do mesmo commit. Nenhuma dependência de aplicação instalada.

| ADR | Decisão | Razão / alternativa rejeitada |
|---|---|---|
| 01 | Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy/Alembic, SQLite WAL, HTTPX e Playwright Python; UI pt-BR em mesma origem, bind 127.0.0.1 | Stack pequena; não incorporar produtos completos. |
| 02 | MVP offline com vagas/perfil sintéticos, PDF, painel de aprovação e ATS fictício real no navegador | Não exigir CV, CPF, RG, endereço ou credenciais reais. |
| 03 | Determinismo para autorização, dedup e estados; LLM opcional sugere texto com fact_ids | LLM não decide permissões nem controla shell. |
| 04 | Aprovação autenticada no painel local | Reply SIM por email adiado: identidade e pacote exato não estão demonstrados. |
| 05 | DRY_RUN=true; LIVE_SUBMISSION_ENABLED=false | Dry-run nunca faz submit. Teste de envio usa modo fixture separado, sem egress externo e com recibo sintético. |
| 06 | Separar descoberta, política, preparação e envio | API pública de vagas não concede permissão de candidatura. |
| 07 | Pacote imutável com nonce, prazo, hashes e claim transacional | Retry de envio incerto gera duplicatas; conciliar primeiro. |
| 08 | Armazenamento local e minimizado; vault criptografado opcional futuro | Sem informações sensíveis em logs, prompts ou notificações. |
| 09 | LLM desligado na demo; adapter registra modelo/tokens/custo e reserva teto por operação/dia antes da chamada | Preço desconhecido bloqueia chamada paga; nunca declarar custo zero sem evidência. |

## Reconhecimento compacto

Fontes primárias consultadas em 2026-11-10. Observações de documentação, não testes de integração. Versões/releases e licenças transitivas não auditadas: antes de incorporar código, fixar commit/versão e conferir LICENSE/NOTICE do artefato. Licença de código não autoriza acesso ao serviço.

| Fonte | Observação verificada | Escolha |
|---|---|---|
| [AIHawk](https://github.com/feder-cr/Jobs_Applier_AI_Agent) | Repositório de automação de candidaturas, indica MIT | Referência, sem incorporar stack. |
| [Resume Matcher](https://github.com/srbhr/Resume-Matcher) | Currículos locais; indica Apache-2.0 | Referência para comparação, sem importação integral. |
| [browser-use](https://github.com/browser-use/browser-use) | Biblioteca MIT e serviços cloud separados | Fora do MVP: agente genérico aumenta superfície. |
| [Skyvern](https://github.com/Skyvern-AI/skyvern) | Automação visual, indica AGPL-3.0 | Fora do MVP; revisar obrigações antes de eventual uso. |
| [Stagehand](https://github.com/browserbase/stagehand) | SDK de interação/extração, indica MIT | Evitar runtime/serviço adicional. |
| [JobSpy](https://github.com/speedyapply/JobSpy) | Scraping de portais, indica MIT | Não usar no MVP; permissão do serviço não demonstrada. |
| [Playwright](https://playwright.dev/python/docs/input) | Preenchimento e upload documentados | Escolha para fixture e adapters expressamente autorizados. |
| [Greenhouse](https://docs.greenhouse.io/job-board) | GET público; submit requer chave Job Board | Leitura opcional; não assumir posse de chave de empregador. |
| [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api) | API de vagas publicadas | Leitura opcional; envio exige avaliação própria. |
| [Lever](https://github.com/lever/postings-api) | Listagens e candidaturas documentadas, com limites | Avaliar por organização; não inferir autorização irrestrita. |

MVP: fonte sintética, PDF rastreável, aprovação, upload e recibo no ATS fictício, testes de sucesso/negação. Futuro: conectores reais, OAuth/Gmail, vault e envio externo autorizado. LinkedIn/Indeed: alertas importáveis; fluxos restritos têm handoff manual. Métrica: relevância e oportunidades de entrevista, não volume de envios.
