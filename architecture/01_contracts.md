# Contratos v1

Preservar nomes/rotas compartilhados. Detalhes de payload abaixo são proposta a comparar com implementação Gemini, não acordo já verificado.

## Schema completo

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
  apply_mode: automated_allowed | assisted_manual_submit | manual_only | unsupported
  source_attribution: str

SearchQuery: {text: str, location?: str|null, remote_only?: bool|null, limit?: int|null}
JobSource.discover(query: SearchQuery) -> list[NormalizedJob]
BrowserDriver.prepare(approved_packet, resume_file, target_url, dry_run=True) -> BrowserAttemptResult
```

Todos opcionais aceitam null; nenhum salário/data/eligibilidade inventado. IDs internos UUID separados. Datas UTC com timezone; salários finitos >=0 e min<=max, moeda ISO 4217 quando conhecida. Strings obrigatórias não vazias; título/empresa <=300, descrição <=100000, URL <=4096. Pesquisa <=500 caracteres, limit 1..100 (default 20). HTTPS externo; HTTP apenas fixture isolada. Escapar HTML ao exibir.

Dedup por (source,source_job_id) quando presente; fallback URL normalizada por regras específicas do provedor. Preservar URL original e todas proveniências. Não remover parâmetros desconhecidos nem fundir vagas apenas pelo título. Merge entre fontes exige identidade forte do posting/empregador. Mudança material invalida aprovação.

BrowserAttemptResult: {status, fields_detected: list[str], fields_filled: list[str], missing_fields: list[str], evidence: list[Evidence], final_url: str|null, confirmed_submission: bool}. Status: prepared, ready_for_manual_review, paused, submitted_unverified, verified, failed. Evidence: {kind, artifact_id, receipt_id?, observed_at, synthetic: bool}. prepare nunca envia nem retorna confirmed_submission=true. submit interno separado repete gate. Somente verified com recibo/evidência específica do adapter confirma envio.

## Pacote e banco

ApprovalPacket: {version:1, application_id, canonical_job_id, employer_id, user_id, job_snapshot_hash, target_url, target_policy_version, cv_sha256, answers_sha256, profile_revision, expires_at, nonce}. Gerado no servidor e imutável. SHA-256 dos bytes exatos do PDF; respostas e pacote usam JSON canônico RFC 8785, sem chaves duplicadas/NaN. Persistir os bytes aprovados, não regenerar PDF depois. Default de validade: 30 minutos. Cliente envia hash visto, servidor recalcula e valida identidade/versões.

Tabelas mínimas: users, profile_revisions, verified_facts, jobs, job_provenance, applications, packets, approvals, attempts, evidence, audit_events. FKs ligadas; unique(user_id,canonical_job_id) impede duplicata de candidatura no MVP. Aprovação: nonce único, revoked_at, consumed_at, expires_at. Estado com versão CAS. Claim da tentativa e consumo do nonce na mesma transação curta, sem manter transação aberta durante browser. Unique por autorização consumida. Crash após claim mantém tentativa incerta e não reabilita nonce. WAL não substitui locks/constraints. Migrações Alembic versionadas, upgrade de banco vazio/anterior, backup protegido antes de mudança destrutiva; sem reset automático.

## API proposta

Dados privados sempre autenticados e vinculados ao dono; health sem dados privados. Mutações exigem CSRF/Origin e sessão. Exemplos sintéticos; J abaixo significa objeto NormalizedJob completo, não string literal.

| Método/rota | Request | Response |
|---|---|---|
| GET /api/health | — | {"status":"ok"} |
| GET /api/jobs | ?limit=20&cursor=... | {"items":[{"id":"job-demo","job":J}],"next_cursor":null} |
| POST /api/jobs/discover | {"text":"Python","location":null,"remote_only":true,"limit":10} | {"items":[],"errors":[],"partial":false} |
| GET /api/jobs/{job_id} | — | {"id":"job-demo","job":J,"provenance":[]} |
| GET /api/profile | — | {"revision":1,"facts":[],"missing_fields":[]} |
| POST /api/profile/import | multipart file <=5 MiB | {"revision":2,"unverified_facts":[],"warnings":[]} |
| POST /api/applications/prepare | {"job_id":"job-demo","profile_revision":2,"answers":{}} | {"id":"app-demo","state":"awaiting_approval","packet_hash":"sha256","expires_at":"2026-11-11T02:00:00Z","missing_fields":[]} |
| POST /api/applications/{application_id}/approve | {"packet_hash":"hash exibido","expected_version":3} | {"id":"app-demo","state":"approved","version":4} |
| POST /api/applications/{application_id}/reject | {"expected_version":3,"reason":"Não desejo candidatar"} | {"id":"app-demo","state":"rejected"} |
| POST /api/applications/{application_id}/execute | {"expected_version":4}; header Idempotency-Key | {"id":"app-demo","state":"ready_for_manual_review","attempt_id":"attempt-demo","confirmed_submission":false} |
| GET /api/applications | ?limit=20&cursor=... | {"items":[],"next_cursor":null} |
| GET /api/settings | — | {"dry_run":true,"live_submission_enabled":false,"llm_enabled":false,"mode":"offline"} |

HTTP 200, ou 202 para tentativa assíncrona. Erro: {"error":{"code":"approval_expired","message":"Aprovação expirada","retryable":false,"request_id":"..."}}. 401 sessão ausente; 403 CSRF/política; 404 inexistente/outro dono; 409 versão/hash/estado/replay; 422 entrada; 429 limites; 503 indisponível.

Import extrai fatos NÃO verificados; bloqueia preparação até confirmação humana. Fixture de fatos já verificados é permitida só no teste. Antes de importação real, acordar rota auxiliar de confirmação de fatos e rota de visualização protegida do PDF/respostas com Gemini. Não promover extração automaticamente a verdade. Execute não aceita URL/caminho arbitrário nem altera settings. manual_only/unsupported retorna handoff sem automação; assisted_manual_submit só prepara em destino autorizado e não clica submit.

Adapter errors: timeout, rate_limited, unauthorized, policy_denied, malformed_source, network_blocked, captcha, missing_input, budget_exceeded, uncertain_submission. Falhas de fontes isoladas com resultados parciais. Cache proposto 15min conforme política; timeout 15s; até 2 retries apenas de leitura com jitter/Retry-After, teto total 60s. Submit nunca tem retry automático.

## Estados — whitelist

| Origem | Destinos | Autoridade/condição |
|---|---|---|
| discovered | evaluated, blocked, canceled | avaliador/política ou usuário |
| evaluated | prepared, needs_user_input, blocked, canceled | gerador validado/política ou usuário |
| prepared | awaiting_approval, needs_user_input, canceled | servidor persiste pacote ou usuário cancela |
| awaiting_approval | approved, rejected, expired, canceled | usuário autenticado ou relógio |
| approved | preparing_form, rejected, expired, blocked, canceled | executor com gate ou revogação/política |
| preparing_form | ready_for_manual_review, submitted_unverified, needs_user_input, blocked, failed, canceled, expired, rejected | executor; revogação somente antes do claim final |
| ready_for_manual_review | submitted_unverified, expired, canceled, rejected, needs_user_input | submit com gate repetido ou usuário |
| submitted_unverified | verified, blocked | reconciliador com evidência; sem retry |
| needs_user_input | evaluated, canceled | usuário resolve, novo pacote |
| failed, expired | evaluated, canceled | reinício só com prova de zero envio; nova aprovação |
| rejected, canceled, blocked, verified | nenhum | terminais MVP; conciliação administrativa não reenvia |

Mudança material em awaiting_approval/approved/preparing_form/ready_for_manual_review expira pacote. Recomeçar avaliação e pedir nova aprovação. Claim final grava submitted_unverified ANTES da chamada externa; se recibo chega no mesmo request, registrar transições internas até verified. Revogação vence se serializada antes do claim; após claim não prometer desfazer envio. Só verified tem confirmed_submission=true; fixture exige synthetic=true. Todas transições não listadas são negadas.
