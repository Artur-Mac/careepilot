# Contratos v2 — CareerPilot (Copiloto CDP por Etapa)

Contratos implementados na aplicação após o pivô descrito em `architecture/05_pivot_blueprint.md`.

## DTOs e Estados Principais

```text
NormalizedJob:
  source: str
  source_job_id: str | null
  company: str
  title: str
  location: str | null
  workplace_type: remote | hybrid | onsite | unknown
  description: str
  job_url: str
  apply_url: str | null
  posted_at: datetime | null
  discovered_at: datetime
  employment_type: str | null
  seniority: str | null
  salary_min: float | null
  salary_max: float | null
  salary_currency: str | null
  source_attribution: str
  discovery_channel: str (default "browser")

SearchQuery: {text: str, location?: str|null, remote_only?: bool, include_remote?: bool, limit?: int}
```

Regras de normalização e validação:
- `job_url` e `apply_url` passam por `validate_external_url` (HTTPS externo obrigatório em produção; origens locais permitidas somente via `allow_test_origins` em testes isolados).
- `salary_max < salary_min` é normalizado em `_check_salary_range`; nenhum salário/data/elegibilidade é inventado.
- Deduplicação preserva proveniência e conteúdo sanitizado contra prompt injection.
- Contratos de mutação de descoberta, preparação, anexo de aba, respostas de etapa e perguntas usam `ConfigDict(extra="forbid")`.

## Estados de Candidatura (`applications.status`)

| Estado | Significado | Transições principais |
| --- | --- | --- |
| `ready` | PDF gerado e aterrado ao `MasterProfile` atual | -> `review` / `needs_user` (após preencher etapa), ou `done` |
| `review` | Etapa atual preenchida sem bloqueios pendentes para revisão visual no Chrome | -> `review` / `needs_user` (nova etapa), ou `done` |
| `needs_user` | Há bloqueios humanos (perguntas sem resposta, consentimento, documento sensível, CAPTCHA, login/OTP ou aba perdida) | -> `review` / `needs_user` (após responder/re-inspecionar), ou `done` |
| `done` | Candidatura declarada concluída manualmente pelo usuário | terminal ativo no fluxo |
| `closed` | Registros históricos migrados de estados encerrados (`rejected`/`expired`/`canceled`), omitidos da lista ativa padrão | histórico |

Regras invariantes:
- Nenhuma rota ou método do driver clica em *Avançar*, *Continuar*, *Próximo* ou *Enviar candidatura*.
- Não há máquina de aprovação com nonce/TTL, recibos automáticos de submissão nem verificação remota de protocolo.

## Tabelas Principais (SQLite WAL / Alembic `head`)

- `master_profiles`: `user_id` PK, `document` JSON (contato, experiências com bullets/métricas, educação, projetos, habilidades, idiomas, preferências, `standard_answers`, `imports`), `revision` (controle otimista CAS), `updated_at`.
- `job_postings`, `job_sources`, `discovery_runs`: vagas descobertas no navegador, deduplicação, resumos sob demanda e execuções de descoberta.
- `resume_versions`: versões de PDF geradas e vinculadas a `profile_revision` e `job_description_hash`, além de texto extraído para preview/auditoria.
- `applications`: `id`, `job_id`, `resume_id`, `status`, `answers_json`, `last_step_json`, timestamps (sem handles de navegador, credenciais ou recibos).
- `user_settings`, `connector_states`, `audit_events`, `llm_usage_events`.

## Rotas HTTP (`/api`)

Todas as rotas privadas exigem cookie de sessão válido; mutações exigem CSRF + validação de `Origin`.

| Método / Rota | Entrada / Query | Resposta / Semântica |
| --- | --- | --- |
| `GET /api/health` | — | `{"status":"ok", ...}` (sem dados privados) |
| `GET /api/auth/session` | — | Estado da sessão e token CSRF (quando autenticado) |
| `POST /api/auth/unlock` | `UnlockRequest` | Troca passcode por sessão `HttpOnly` |
| `POST /api/auth/logout` | — | Encerra sessão |
| `GET /api/browser/status` | — | Disponibilidade atual do Chrome CDP |
| `GET /api/jobs` | filtros (`q`, `workplace`, `include_restricted`, `cursor`, `limit`) | Feed paginado de vagas |
| `POST /api/jobs/discover` | `DiscoverRequest` | HTTP `202` com `run_id` para descoberta via CDP em background |
| `GET /api/jobs/discovery-runs/{run_id}` | — | Status, progresso e erros por conector |
| `POST /api/jobs/{job_id}/summary` | — | Gera/retorna resumo em cache do trecho da vaga |
| `POST /api/jobs/{job_id}/dismiss` | — | Dispensa vaga do feed ativo |
| `GET /api/jobs/{job_id}` | — | Detalhes completos e proveniências da vaga |
| `GET /api/profile` | — | Documento JSON completo + `revision` |
| `PUT /api/profile` | documento + `expected_revision` | Atualiza com CAS (`409` se revisão divergir) |
| `POST /api/profile/import` | multipart/JSON | Extrai texto e anexa importação ao perfil |
| `POST /api/applications/prepare` | `PrepareApplicationRequest` | Gera PDF aterrado ao `MasterProfile` e cria candidatura `ready` |
| `GET /api/applications` | — | Lista candidaturas ativas com `bound_to_browser` e `profile_revision_stale` |
| `GET /api/applications/{application_id}` | — | Detalhe da candidatura, último snapshot de etapa e bloqueios |
| `POST /api/applications/{application_id}/open` | — | Abre a URL da vaga no Chrome CDP e vincula a aba em memória |
| `GET /api/applications/{application_id}/pages` | — | Lista abas elegíveis abertas no Chrome com `tab_id` opaco e URL sanitizada (sem query/fragmento) |
| `POST /api/applications/{application_id}/attach-page` | `AttachPageRequest` | Vincula `tab_id` opaco após validar compatibilidade de origem e presença no registro |
| `POST /api/applications/{application_id}/fill-current-page` | — | Inspeciona a etapa atual no Chrome, preenche apenas campos permitidos/aterrados e retorna bloqueios/notices |
| `POST /api/applications/{application_id}/answer-questions` | `AnswerQuestionsRequest` | Salva respostas na candidatura (e opcionalmente em `standard_answers` via CAS `profile_revision`) |
| `POST /api/applications/{application_id}/complete` | — | Marca candidatura como concluída manualmente (`done`) |
| `GET /api/resumes/{resume_id}/download` | — | Download protegido do PDF gerado |
| `GET /api/resumes/{resume_id}/preview` | — | Texto extraído do PDF e resumo de proveniência |
| `GET /api/settings`, `PUT /api/settings` | `SettingsUpdateRequest` | Preferências de busca, canais e status do LLM |
| `POST /api/connectors/{name}/toggle` | — | Habilita/desabilita conector de busca |
| `POST /api/search-queries/preview` | `DiscoverRequest` | Pré-visualiza queries derivadas do perfil/override |
| `GET /api/audit`, `GET /api/export`, `DELETE /api/data` | — | Auditoria, exportação sanitizada e limpeza local |

Erros de etapa/concorrência principais:
- `404` (`ApplicationNotFoundError`, `TabNotFoundError`)
- `409` (`operation_in_progress`, `tab_lost`, `origin_mismatch`, `multiple_containers`, `step_changed`, `resume_prepare_required`, `profile_conflict`)
- `422` (`invalid_answer`)
- `503` (`browser_unavailable`)
