# Pesquisa de Referências Open Source e APIs de Vagas

Data da revisão: 2026-11-10  
Escopo: Análise de licenças, riscos operacionais, arquitetura e conformidade com os requisitos de segurança do CareerPilot (sem execução de scripts externos nem cópia integral de repositórios).

## 1. Matriz de Decisão — Repositórios Open Source

| Projeto | Licença | Stack / Última atividade observada | Decisão | Motivo & Análise de Riscos |
|---|---|---|---|---|
| [feder-cr/Jobs_Applier_AI_Agent (AIHawk)](https://github.com/feder-cr/Jobs_Applier_AI_Agent) | `MIT` | Python (`2026-11-11`) | **Inspect only (não reusar em runtime)** | Foco em auto-apply em massa ("while you sleep") no LinkedIn. Alto risco de violação de ToS, banimento de conta, spam de recrutadores e respostas fabricadas sem aprovação unitária por pacote. Aproveitamos apenas a inspiração conceitual de etapas e auditoria. |
| [srbhr/Resume-Matcher](https://github.com/srbhr/Resume-Matcher) | `Apache-2.0` | Python (`2026-11-07`) | **Inspect only** | Acoplamento opinativo de stack completa para geração via LLM. Para o MVP, construímos gerador próprio determinístico + provedor LLM plugável com rastreabilidade obrigatória de fatos (`FactEvidence`) e verificação via `pypdf`, eliminando risco de alucinação. |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | `MIT` | Python (`2026-11-11`) | **Inspect only / Fallback opcional desligado por padrão** | Útil como referência de agente DOM/visão; porém promove modos cloud/stealth. No CareerPilot, usamos Playwright determinístico-first com allowlist estrita de domínios e ações; eventual fallback visual LLM fica atrás da flag `FEATURE_VISUAL_FALLBACK=false` sem bypass anti-bot. |
| [Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern) | `AGPL-3.0` | Python (`2026-11-11`) | **Reject (reuso direto)** | Licença **AGPL-3.0** impõe obrigações copyleft em serviço de rede e infra pesada (orquestração/visão acoplada). Inspecionamos apenas o padrão conceitual de análise de formulários multi-etapas com verificação antes do envio. |
| [browserbase/stagehand](https://github.com/browserbase/stagehand) | `MIT` | TypeScript (`2026-11-11`) | **Inspect only** | SDK em TypeScript voltado ao ecossistema Browserbase. Como nosso backend e driver Playwright são Python 3.12+, adotamos apenas a separação conceitual `observe` → `act` → `extract/verify`, implementada diretamente em Python. |
| [speedyapply/JobSpy](https://github.com/speedyapply/JobSpy) | `MIT` | Python (`2026-10-27`) | **Reject (reuso em runtime) / Inspect schema** | Realiza scraping direto de LinkedIn, Indeed, Glassdoor e usa rotação de proxies contra bloqueios, o que viola nossa política de segurança e ToS. Aproveitamos apenas referência de normalização de campos para o DTO congelado `NormalizedJob`. |
| [reactive-resume/reactive-resume](https://github.com/reactive-resume/reactive-resume) | `MIT` | TypeScript (`2026-11-08`) | **Inspect only** | Monorepo pesado (Node/React) para o escopo local-first do MVP. Aproveitamos o conceito de currículo estruturado em JSON focado em privacidade, convertendo para PDF de coluna única com texto selecionável via `reportlab`. |

## 2. Notas sobre APIs Públicas e Restrições de Plataformas

Verificações realizadas nas documentações oficiais em 2026-11-10:

1. **Greenhouse Job Board API** (`https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`)  
   - Endpoints `GET` são públicos para leitura do quadro da empresa.  
   - O endpoint `POST` de envio de candidatura exige Basic Auth com Job Board API Key do empregador — **não** deve ser usado como submissão genérica de candidato.  
   - `apply_mode` nunca deve assumir `automated_allowed` apenas pela disponibilidade da API pública; padrão seguro `assisted_manual_submit`.
2. **Ashby Job Postings API** (`https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true`)  
   - API pública `GET` para páginas de carreira, retornando `descriptionHtml`, `descriptionPlain`, `workplaceType`, `isRemote`, `publishedAt`, `employmentType`, `address` e compensação opcional.  
   - Padrão `assisted_manual_submit` (ou `manual_only` se formulário externo exigir verificações).
3. **Lever Postings API** (`https://api.lever.co/v0/postings/{site}?mode=json` e instância EU `https://api.eu.lever.co/v0/postings/{site}?mode=json`)  
   - Suporta instâncias global e EU; não oferece busca full-text global nem expõe perguntas customizadas em todas as rotas de listagem. Padrão `assisted_manual_submit`.
4. **SmartRecruiters**  
   - Endpoints de integração exigem credenciais/escopos de parceiro ou cliente (`API Key` / `OAuth 2.0`). Sem acesso lícito explícito, conectores autenticados permanecem desabilitados.
5. **Himalayas Remote Jobs API** (`https://himalayas.app/jobs/api` e `/jobs/api/search`)  
   - Paginação preferencial por `cursor` (`nextCursor`, limite máximo 20/req); campos `pubDate`/`expiryDate` em Unix seconds, `seniority` como lista, `locationRestrictions`, `salaryPeriod`.  
   - Termos exigem crédito visível ao Himalayas e uso de `applicationLink` nas ações de candidatura; proibido repassar para outros agregadores, Google Jobs ou LinkedIn Jobs.
6. **Remotive Public API** (`https://remotive.com/api/remote-jobs`)  
   - Atraso de 24h no feed público gratuito; termos exigem link de retorno para a URL da vaga na Remotive e menção como fonte (`source_attribution`).
7. **Arbeitnow Job Board API** (`https://www.arbeitnow.com/api/job-board-api`)  
   - Sem API key obrigatória; inclui flags `remote` e `visa_sponsorship`.
8. **Gupy, Wellfound, Remotar, Programathor, LinkedIn e Indeed**  
   - A API da Gupy (`developers.gupy.io`) é voltada a integrações autenticadas de empresas/parceiros (token corporativo), não uma API pública aberta de candidatos.  
   - LinkedIn e Indeed possuem restrições contra scraping automatizado e bots de conta; tratados via **ingestão de alertas oficiais por e-mail / importação explícita de URL pelo usuário** e classificados como `manual_only`.
9. **Buscadores (Brave Search / Tavily / Adzuna) e Google `JobPosting` JSON-LD**  
   - Desabilitados até fornecimento explícito de API key; sem Google Custom Search JSON API para contas novas e sem promessa de API oficial genérica do Google Jobs. Páginas de carreira públicas podem expor `schema.org/JobPosting` em JSON-LD respeitando limites e política SSRF.

## 3. Dependências Escolhidas (Pinadas via `uv.lock`)

- **Backend & Validação**: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `httpx`
- **Persistência**: `sqlalchemy` (2.x com SQLite WAL), `alembic`
- **Documentos**: `reportlab` (PDF coluna única com fonte TTF UTF-8 pt-BR), `pypdf` (leitura e verificação de texto extraível), `python-docx`
- **Automação Web**: `playwright` (determinístico-first em Chromium local)
- **Segurança & HTML**: `cryptography`, `beautifulsoup4`, `lxml`, `jinja2`
- **Qualidade**: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
