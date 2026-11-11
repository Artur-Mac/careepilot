# Notas de Integração & Revisão — CareerPilot MVP (`careerpilot-gemini`)

> **Worktree / Branch de entrega**: `careerpilot-gemini` (`gemini-implementation`)  
> **Escopo**: Implementação completa e verificada do CareerPilot MVP isolada em relação a `sol-architecture` e `architecture/**`.

---

## 1. Resumo da Entrega

O CareerPilot MVP foi implementado em Python 3.12+, FastAPI, SQLAlchemy 2.x (SQLite WAL), Alembic, ReportLab + `pypdf`, Playwright (Chromium local) e interface SPA estática (`HTML`/`JS`), seguindo um fluxo human-in-the-loop rastreável:

**Descoberta e sanitização → Ranqueamento explicável → Geração de currículo PDF + Respostas rastreáveis → Solicitação e aprovação explícita imutável → Execução Playwright com verificação pós-envio e trilha de auditoria.**

---

## 2. Contratos Congelados Implementados

### 2.1 Schemas e DTOs (`backend/app/schemas/contracts.py`)
- `NormalizedJob`: normalização canônica de URL (remoção de parâmetros de rastreio como `utm_*`, `gh_src`, `ref`), validação SSRF com suporte controlado a `allow_test_origins` via `ValidationInfo` de contexto do Pydantic (usado por `FictionalTestAtsConnector`), requisitos obrigatórios/desejáveis, `apply_mode`, salário/moeda normalizados e metadados de atribuição.
- `SearchQuery`: parâmetros bilíngues (`text`, `location`, `remote_only`, `limit`).
- `BrowserAttemptResult`: status (`"filled_dry_run" | "submitted_verified" | "blocked" | "needs_user_input" | "failed"`), campos preenchidos, caminho de anexos/screenshots, evidências e URL final.
- Contratos HTTP: `UnlockRequest`, `DiscoverRequest`, `PrepareApplicationRequest`, `ApprovalActionRequest`, `ExecuteApplicationRequest`, `ManualConfirmRequest`, `AnswerQuestionsRequest`, `SettingsUpdateRequest`.

### 2.2 Máquina de Estados Centralizada (`backend/app/core/state_machine.py`)
- Enum `ApplicationStatus` com todos os 15 estados congelados:
  `discovered`, `evaluated`, `prepared`, `awaiting_approval`, `approved`, `preparing_form`, `ready_for_manual_review`, `submitted_unverified`, `verified`, `rejected`, `expired`, `blocked`, `canceled`, `failed`, `needs_user_input`.
- Toda transição é validada por `validate_transition(...)` e `ALLOWED_TRANSITIONS`, levantando `InvalidTransitionError` em saltos ilegais (ex.: executar candidatura sem passar por `approved`).

### 2.3 Banco de Dados e Migrações (`backend/app/models/tables.py`, `alembic/`)
- Migração inicial versionada em `alembic/versions/adf094dd1fee_initial_schema.py` com 22 tabelas:
  `user_settings`, `search_plans`, `source_connectors`, `employers`, `job_postings`, `job_source_records`, `job_requirements`, `profile_facts`, `fact_evidence`, `match_evaluations`, `resume_versions`, `answer_versions`, `applications`, `approval_requests`, `approval_decisions`, `application_attempts`, `browser_attempts`, `notifications`, `question_requests`, `audit_events`, `llm_usage_events`, `scheduler_runs`.
- `alembic/env.py` prioriza `os.environ.get("DATABASE_URL")` sobre `alembic.ini` (`_get_url()`), permitindo testes isolados e troca para PostgreSQL sem editar configuração estática.
- Recuperação de agendamentos interrompidos via `DiscoveryScheduler.recover_stale_runs` (`backend/app/workers/scheduler.py`).

### 2.4 Rotas da API (`backend/app/api/routes.py` e `backend/app/main.py`)
- Interface & saúde: `GET /`, `GET /api/health`
- Autenticação local loopback (cookie `HttpOnly` + token CSRF + checagem de `Origin`): `GET /api/auth/session`, `POST /api/auth/unlock`, `POST /api/auth/logout`
- Radar e vagas: `GET /api/jobs`, `POST /api/jobs/discover`, `GET /api/jobs/{job_id}`
- Perfil e proveniência: `GET /api/profile`, `POST /api/profile/import`, `POST /api/profile/facts/{fact_id}/verify`
- Candidaturas e aprovação: `POST /api/applications/prepare`, `POST /api/applications/{application_id}/approve`, `POST /api/applications/{application_id}/reject`, `POST /api/applications/{application_id}/request-changes`, `POST /api/applications/{application_id}/answer-questions`, `POST /api/applications/{application_id}/mark-manual`, `POST /api/applications/{application_id}/execute`, `GET /api/applications`
- Currículos: `GET /api/resumes/{resume_id}/download`, `GET /api/resumes/{resume_id}/preview`
- Configurações, conectores, buscas, notificações, scheduler, auditoria e privacidade: `GET/PUT /api/settings`, `POST /api/connectors/{name}/toggle`, `POST /api/search-queries/preview`, `GET /api/notifications`, `POST /api/scheduler/{action}`, `GET /api/audit`, `GET /api/export`, `DELETE /api/data`

---

## 3. Matriz de Conectores, ATS e Notificações

| Componente | Classe / Identificador | Modo Padrão | Estado no MVP |
|---|---|---|---|
| Greenhouse Board API | `GreenhouseConnector` (`greenhouse`) | HTTP `GET` + fallback offline | Parsers reais + retry 429/5xx testados com `MockTransport`; `apply_mode` nunca assume auto-submit |
| Ashby Posting API | `AshbyConnector` (`ashby`) | HTTP `GET` + fallback offline | Parser real + retry 503 testado com `MockTransport`; padrão `assisted_manual_submit` / `manual_only` |
| Lever Postings API | `LeverConnector` (`lever`) | HTTP `GET` global & EU + fallback | Parser real + detecção EU `api.eu.lever.co` testada com `MockTransport` |
| Himalayas Remote API | `HimalayasConnector` (`himalayas`) | HTTP `GET` (`cursor`) + fallback | Paginação por cursor e atribuição de fonte testadas com `MockTransport` |
| Remotive Public API | `RemotiveConnector` (`remotive`) | HTTP `GET` + fallback | Normalização e `source_attribution` obrigatória testadas com `MockTransport` |
| Arbeitnow Job Board | `ArbeitnowConnector` (`arbeitnow`) | HTTP `GET` + fallback | Flags `remote`/`visa_sponsorship` e schema normalizado |
| ATS Fictício Local (E2E) | `FictionalTestAtsConnector` (`fictional_ats`) | Loopback `127.0.0.1:8010` | **Ativo para E2E real** contra `backend/app/browser_automation/fake_ats.py` |
| Alertas / Boards Manuais | `ManualBoardAlertsConnector` (`manual_alerts`) | Fixtures offline (`manual_only`) | Nunca automatiza envio; preserva `manual_only` |
| Gmail Alertas / Respostas | `GmailConnector` | `local_fixture_only` | OAuth Desktop opcional com escopo mínimo `gmail.readonly` quando configurado |
| SmartRecruiters | `SmartRecruitersConnector` (`smartrecruiters`) | Desabilitado (`enabled=False`) | Exige credenciais corporativas/parceiro; bloqueado por padrão |
| Brave Search | `BraveSearchConnector` (`brave_search`) | Desabilitado até `BRAVE_SEARCH_API_KEY` | Parser HTTP validado via `MockTransport`; exige chave real |
| Adzuna | `AdzunaConnector` (`adzuna`) | Desabilitado até `ADZUNA_APP_ID/KEY` | Parser HTTP validado via `MockTransport`; exige credenciais reais |
| Tavily Search | `TavilySearchConnector` (`tavily_search`) | Desabilitado até `TAVILY_API_KEY` | Placeholder conservador (retorna `[]` no MVP offline) |
| Notificações | `LocalOutboxAdapter` | `local_outbox` | Ativo no painel (`/api/notifications`); `TelegramBotAdapter` (allowlist + nonce) e `GmailNotificationAdapter` opcionais; WhatsApp documentado em `docs/whatsapp-future.md` |

### Rotas do ATS Fictício Local (`backend/app/browser_automation/fake_ats.py`)
- `GET /health`
- `GET /jobs/single-step` e `POST /jobs/single-step/submit` (recibo `REC-...`)
- `GET /jobs/multi-step` e `POST /jobs/multi-step/submit` (recibo `REC-MS-...`)
- `GET /jobs/captcha-gate` (simula desafio anti-bot que deve disparar bloqueio, nunca bypass)
- `GET /jobs/manual-demo` (vaga `manual_only`)
- `GET /jobs/redirect-offdomain` (redireciona para fora de loopback para validar proteção de domínio)
- `GET /receipts/{receipt_id}` (verificação pós-envio real no servidor ATS)

---

## 4. Checklist de Segurança Crítica para Revisão do Sol

Recomenda-se focar a revisão de arquitetura/integração nestes pontos críticos:

1. **Aprovação Imutável, Uso Único e Proteção contra Replay (`backend/app/services/approval/service.py`)**:
   - `consume_approval_atomically` executa `UPDATE approval_requests SET consumed=TRUE WHERE id = :id AND consumed IS FALSE` verificando `res.rowcount == 1`.
   - `packet_hash` vincula vaga + descrição sanitizada + SHA-256 do currículo PDF + versão das respostas (`AnswerVersion`). Qualquer mutação após aprovação invalida o pacote e move a candidatura para `blocked`.
   - Aprovações expiradas ou forjadas retornam HTTP `409`/bloqueio auditado.
2. **Duplo Portão `DRY_RUN=true` + `LIVE_MODE_ENABLED=false` (`backend/app/services/applications/service.py`)**:
   - Nenhuma execução envia formulário sem `dry_run=False`, `live_mode_enabled=True`, `submit=True` e `job.apply_mode == "automated_allowed"`.
   - Em `ATS_ADAPTERS` (`backend/app/browser_automation/adapters.py`), somente `fictional_ats` tem `automated_submit_permitted=True`.
3. **SSRF, Path Traversal e Confinamento de Domínio (`backend/app/core/ssrf.py`, `backend/app/browser_automation/driver.py`)**:
   - `ssrf.py` bloqueia esquemas não HTTP(S), IPs privados/loopback externos e metadados de nuvem (`169.254.169.254`). Origem do ATS de teste só é aceita quando injetada via contexto explícito.
   - `BrowserDriver` impõe `_session_allowed_domains` em `_route_guard`, verificação pós-navegação/redirect e pós-envio, bloqueando redirecionamentos fora do domínio (`domain_blocked`).
   - Validação de caminho de anexo rejeita path traversal ou arquivos fora de `ARTIFACTS_DIR`.
4. **Prompt Injection & HTML Oculto (`backend/app/security/prompt_guard.py`)**:
   - `sanitize_untrusted_text` detecta padrões em elementos ocultos (`display:none`, `visibility:hidden`, `[hidden]`) antes de removê-los do HTML, adicionando sinal `"hidden_html"` e removendo os blocos invisíveis da string final.
   - Conteúdo suspeito nunca promove aprovação automática nem altera respostas do candidato.
5. **Privacidade, Redação e Criptografia (`backend/app/security/redaction.py`, `backend/app/security/vault.py`)**:
   - Valores secretos configurados e padrões sensíveis (CPF, RG, tokens) são mascarados via `redact_secrets` em logs, auditoria, notificações, `/api/settings` e `/api/export`.
   - `vault.py` classifica sensibilidade (`SensitivityLevel`) e cifra credenciais locais via Fernet.
6. **Bloqueio Anti-Bot e Consentimento Explícito (`backend/app/browser_automation/driver.py`, `backend/app/services/applications/service.py`)**:
   - Detecção de CAPTCHA em `/jobs/captcha-gate` transita imediatamente para `blocked` sem tentativa de contorno.
   - Ausência de `consent_privacy` suspende a preparação/execução em `needs_user_input`.
7. **Veracidade e Extração do Currículo PDF (`backend/app/services/resume/service.py`)**:
   - PDFs de coluna única são gerados com fonte TTF UTF-8 (`reportlab`) a partir apenas de fatos em `ProfileFact`/`FactEvidence` e relidos via `pypdf` para confirmar texto selecionável sem perda de acentuação em pt-BR e zero afirmações fabricadas.

---

## 5. Verificação Automatizada e Evidência E2E Real

A suíte é executada integralmente por `./scripts/smoke.sh`:
- **Lint (`ruff check backend scripts`)**: 0 erros.
- **Tipagem (`mypy backend/app`)**: 0 erros nos 62 arquivos de `backend/app`.
- **Testes (`pytest`)**: **35 testes** cobrindo:
  - `backend/tests/unit/test_core_contracts.py` (9 testes): geração de queries bilíngues, políticas de `apply_mode`, JSON-LD sanitizado, SSRF, normalização de URL/salário, máquina de estados, redação/vault, prompt guard/allowlist visual, PDFs rastreáveis e uso único de aprovação.
  - `backend/tests/integration/test_api_and_connectors.py` (7 testes): conectores com `MockTransport` e retry 429/503, isolamento de falhas + deduplicação + expiração, parsing de alertas/respostas Gmail, `alembic upgrade head` + `DiscoveryScheduler.recover_stale_runs`, `LlmGateway` offline + limite de orçamento diário, AuthN 401 / CSRF 403 / Origin 403, descoberta de 30+ vagas + Top 10 + importação TXT + 3 PDFs validados com `pypdf`.
  - `backend/tests/security/test_security_boundaries.py` (16 testes): matriz SSRF/metadata/allowlist, prompt injection/HTML oculto, aprovações forjadas/expiradas/replay/adulteradas -> `blocked`, vazamento zero em `/api/export` & notificações, path traversal em upload, bloqueio de redirect fora do domínio (`/jobs/redirect-offdomain`), ausência de consentimento (`needs_user_input`).
  - `backend/tests/e2e/test_browser_end_to_end.py` (3 testes): Chromium headless real operando o painel UI (`http://127.0.0.1:...`) contra o ATS Fictício Local — incluindo o fluxo completo **desbloqueio → busca de vagas → seleção da vaga `single-step` → consentimento + geração de PDF → aprovação explícita → ativação controlada de modo live → preenchimento do formulário + upload de PDF + submissão → recibo confirmado `REC-...` verificado em `/receipts/{id}` + screenshots de evidência**, fluxo multi-etapas com resolução de perguntas (`REC-MS-...`), e portões de segurança (`DRY_RUN`, replay HTTP 409, rejeição, falta de consentimento, CAPTCHA -> `blocked`, `manual_only` -> confirmação manual).
- **Seed CLI (`scripts/seed_demo.py`)** validado sobre banco migrado via Alembic: `26 fatos, 40 vagas inseridas (1 duplicadas), 40 avaliações`.

---

## 6. Limitações Conhecidas & Passos Manuais Futuros

1. **Tráfego de rede externo desligado por padrão**: o comportamento HTTP real dos conectores externos (`greenhouse`, `ashby`, `lever`, `himalayas`, `remotive`, `arbeitnow`, `brave_search`, `adzuna`) foi testado via `httpx.MockTransport`; antes de ativar `use_live_network=true`, revisar rate limits e termos de cada provedor.
2. **Conectores dependentes de credencial / placeholders**:
   - `TavilySearchConnector` está em modo conservador (retorna `[]`) e `SmartRecruitersConnector` permanece desabilitado até integração autenticada lícita.
   - `GmailConnector` opera apenas com fixture local até fornecimento de `GMAIL_CLIENT_SECRETS_PATH` e `GMAIL_TOKEN_PATH` (OAuth Desktop, escopo inicial recomendado `gmail.readonly`).
   - `FEATURE_VISUAL_FALLBACK=false` mantém a allowlist rígida de ações visuais (`validate_visual_action`), sem cliente VLM plugado por padrão.
3. **WhatsApp**: não implementado neste MVP local; requisitos de API oficial Meta, HMAC `X-Hub-Signature-256`, janela de 24h e templates constam em `docs/whatsapp-future.md`.
