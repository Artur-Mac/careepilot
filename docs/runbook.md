# Runbook Operacional — CareerPilot

## 1. Pré-requisitos

- **Python 3.12+** gerenciado via `uv`
- **Google Chrome / Chromium** instalado localmente (ou build do Playwright em `~/.cache/ms-playwright/chromium-*`)
- Opcional: **Ollama** local (`http://127.0.0.1:11434/v1`) ou API compatível com OpenAI

Instalação reprodutível a partir do lockfile:

```bash
uv sync --locked --extra dev
```

## 2. Banco de Dados e Migrações (Alembic)

O startup de produção valida que o SQLite está exatamente na revisão `head` do Alembic (`verify_database_at_head`) e não recria tabelas via `create_all`.

Para aplicar migrações manualmente em um diretório de dados:

```bash
CAREERPILOT_DATA_DIR=data/real \
  DATABASE_URL="sqlite:///data/real/careerpilot.db" \
  uv run --locked python -m backend.app.db.migrations upgrade
```

Para inspecionar revisão e consistência de schema em banco isolado:

```bash
DATABASE_URL="sqlite:///tmp_check.db" uv run --locked alembic -c alembic.ini upgrade head
DATABASE_URL="sqlite:///tmp_check.db" uv run --locked alembic -c alembic.ini current
DATABASE_URL="sqlite:///tmp_check.db" uv run --locked alembic -c alembic.ini check
```

> **Atenção:** bancos legados sem `alembic_version` exigem backup via API de backup do SQLite e conferência explícita de schema antes de qualquer migração. Rebaixamentos destrutivos são bloqueados por padrão.

## 3. Fluxo diário com Chrome real (CDP persistente)

1. **Terminal 1 — Abrir Chrome conectado por CDP (`127.0.0.1:9222`)**:

```bash
./scripts/start_browser.sh
```

O script cria/reutiliza o perfil persistente em `${CAREERPILOT_CHROME_PROFILE_DIR:-${CAREERPILOT_DATA_DIR:-data/real}/chrome-profile}`. Na primeira execução, autentique manualmente suas contas de trabalho (LinkedIn, Gupy, InfoJobs) nessa janela. Reiniciar o servidor FastAPI não fecha nem desconecta suas abas abertas no Chrome.

2. **Terminal 2 — Subir o servidor CareerPilot**:

```bash
./scripts/run_real.sh
```

O script executa `python -m backend.app.db.migrations upgrade`, gera/reutiliza credenciais em `${CAREERPILOT_DATA_DIR:-data/real}/access.txt` (passcode de 8 dígitos na linha 1), e inicia o Uvicorn em `http://127.0.0.1:8000` (ou em `CAREERPILOT_HOST`/`CAREERPILOT_PORT`).

Para testes locais rápidos em `data/dev` (passcode padrão `demo-local-2026`):

```bash
./scripts/dev.sh
```

## 4. Fluxo de Uso no Painel

1. **Desbloqueio**: informe o passcode em `http://127.0.0.1:8000`.
2. **Perfil mestre**: preencha/importe contatos, experiências, educação, projetos, habilidades, idiomas, preferências e respostas padrão (`standard_answers`). Apenas fatos presentes nesse documento serão usados no PDF e nos formulários.
3. **Vagas**:
   - Clique **Buscar vagas** (com título opcional e toggles de localização/remoto) para disparar descoberta em background no Chrome conectado.
   - Use **Detalhes** + resumo sob demanda ou **Dispensar**.
   - Clique **Preparar & Aplicar** para gerar o currículo PDF específico da vaga, abrir a página no Chrome e executar a primeira passada de preenchimento.
4. **Candidatura**:
   - Revise os dados preenchidos diretamente no Chrome. O CareerPilot nunca clica em *Avançar*, *Continuar* ou *Enviar candidatura*, nunca marca caixas de consentimento/LGPD e nunca preenche CPF/RG/OTP/CAPTCHA.
   - Quando avançar para uma nova etapa no portal, volte ao painel e clique **Preencher etapa atual**.
   - Para perguntas bloqueantes com resposta faltante, responda no formulário de bloqueios (com opção **Salvar no perfil mestre**) e clique **Preencher etapa atual**.
   - Se a aba mudar ou for fechada, use **Anexar aba aberta** para vincular novamente uma aba compatível via token opaco.
   - Após concluir manualmente no portal, clique **Marcar como concluída**.

## 5. Configuração de LLM (Opcional — Ollama / OpenAI-compatible)

Por padrão `LLM_PROVIDER=offline` (somente extração determinística sem chamadas externas ou custo).

- **Ollama local**:

```bash
export LLM_PROVIDER=ollama
export LLM_BASE_URL=http://127.0.0.1:11434/v1
export LLM_MODEL=qwen2.5:7b-instruct
```

- **API compatível com OpenAI**: configure `LLM_PROVIDER=openai_compat`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` e `LLM_DAILY_BUDGET_USD`. Por padrão `LLM_ALLOW_REMOTE_PROFILE=false` bloqueia envio de texto completo do perfil mestre para endpoints não-loopback.

## 6. Validação Automatizada (`smoke.sh`)

```bash
./scripts/smoke.sh
```

Executa `ruff check backend scripts`, `mypy backend/app` e `pytest`.

## 7. Solução de Problemas Comuns

- **CDP indisponível (`503 browser_unavailable`)**: verifique se `./scripts/start_browser.sh` está em execução no endereço configurado em `BROWSER_CDP_URL` (ex.: `http://127.0.0.1:9222`) e se `GET http://127.0.0.1:9222/json/version` responde.
- **Aba perdida ou navegação fora da origem (`409 tab_lost` / `origin_mismatch`)**: clique em **Reabrir no navegador** ou abra manualmente a página correta no Chrome e vincule via **Anexar aba aberta**.
- **Conflito de etapa (`409 step_changed`)**: o DOM do formulário mudou durante o preenchimento; clique **Preencher etapa atual** novamente para reinspecionar a etapa atual.
- **Limpeza local**: use **Apagar dados locais** no painel (ou `DELETE /api/data`) para remover registros de vagas/candidaturas e artefatos mantendo apenas o schema migrado.
