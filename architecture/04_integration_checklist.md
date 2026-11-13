# Checklist de Integração e Pivô (branch `integration-review`)

Estado: implementação das Fases 1, 2 e 3 concluída sobre a arquitetura copiloto CDP por etapa (`architecture/05_pivot_blueprint.md`). Relatórios históricos em `architecture/reviews/**` preservados apenas como registro histórico da revisão inicial pré-pivô.

## Gate de Verificação do Pivô

- [x] **Fase 1 (Master Profile, estado e PDF fiel)**: remoção da máquina de aprovação, recibos/provas de submissão, `profile_facts` e `fake_ats`; criação do documento JSON `MasterProfile` com revisão otimista CAS, migração Alembic a partir de `adf094dd1fee` e geração de currículo PDF aterrada exclusivamente ao perfil real.
- [x] **Fase 2 (Descoberta CDP, filtros e resumo)**: substituição dos conectores sintéticos/APIs antigas por coletores registrados sobre Chrome CDP (`BrowserSession`), tabela `discovery_runs`, recuperação de runs interrompidos e feed paginado em `/api/jobs` com **Dispensar** e resumo sob demanda.
- [x] **Fase 3 (Copiloto CDP por etapa e UI Candidatura)**: inspeção sanitizada de etapa única, preenchimento restrito com re-passada condicional intra-etapa, recusa de `legal_consent`/`sensitive_id`/`captcha`/`unknown_origin`/uploads não-currículo, preservação de edições humanas, aterramento de `FieldPlan` no `MasterProfile`, vínculo seguro por `tab_id` opaco, reuso cross-portal via `question_signature`, aba **Candidatura** com Q&A inline e recuperação de aba perdida.
- [ ] **Portão Final de Validação e Aceitação**: suíte automatizada (`uv sync --locked --extra dev`, `ruff`, `mypy`, `pytest`, verificações Alembic em banco vazio e `adf094dd1fee -> head`, `./scripts/smoke.sh`) + sondas vivas isoladas em `CAREERPILOT_DATA_DIR` temporário registradas com vereditos exatos em `architecture/03_acceptance.md`.
