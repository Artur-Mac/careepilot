# Runbook Operacional — CareerPilot MVP (`runbook.md`)

> Guia prático em pt-BR para instalar, configurar, executar com segurança (`DRY_RUN=true`) e depurar o CareerPilot MVP e o servidor ATS Fictício Local.

## 1. Requisitos do Sistema

- **Python**: `>= 3.12` (testado no projeto com Python 3.12.3 e `uv`)
- **Playwright / Chromium**: binário headless do Chromium instalado via `uv run playwright install chromium` (ou em `~/.cache/ms-playwright/chromium-*/`)
- **Fontes Unicode TTF (Linux)**: pacotes `fonts-noto-core` / `fonts-dejavu-core` em `/usr/share/fonts/truetype/` para renderização e extração limpa de acentuação em pt-BR via `pypdf`

## 2. Configuração de Ambiente (`.env`)

Copie `.env.example` para `.env` na raiz de `careerpilot-gemini`:

```bash
cp .env.example .env
```

Principais variáveis e defaults seguros:

| Variável | Padrão | Segurança / Finalidade |
|---|---|---|
| `CAREERPILOT_HOST` | `127.0.0.1` | Validação estrita em código: recusa iniciar fora de loopback (`127.0.0.1`, `localhost`, `::1`). |
| `CAREERPILOT_PORT` | `8000` | Porta HTTP local do painel e API FastAPI. |
| `DRY_RUN` | `true` | Quando `true`, nunca clica no botão final de envio (`submit`). |
| `LIVE_MODE_ENABLED` | `false` | Segundo portão obrigatório: envio real exige `DRY_RUN=false`, `LIVE_MODE_ENABLED=true`, vaga `automated_allowed` e aprovação explícita de uso único. |
| `TEST_ATS_ENABLED` / `TEST_ATS_URL` | `true` / `http://127.0.0.1:8010` | Permite automação apenas contra o servidor ATS Fictício Local isolado em loopback. |
| `CAREERPILOT_PASSCODE` | `demo-local-2026` | Código de desbloqueio da interface e API local (troque em uso pessoal). |
| `LLM_PROVIDER` | `offline` | Modo determinístico padrão (`$0.00`). Aceita `openai_compat` ou `ollama` com orçamento em `LLM_DAILY_BUDGET_USD`. |
| `DATABASE_URL` | `sqlite:///./data/careerpilot.db` | SQLite WAL com chaves estrangeiras ativadas; compatível com PostgreSQL via Alembic. |

> **Aviso de Privacidade**: O banco inicial de demonstração usa exclusivamente a candidata fictícia **Marina Costa Duarte** (`backend/tests/fixtures/fictional_profile.py`). Não envie CPF, RG ou endereço residencial real para testes ou modelos externos.

## 3. Inicialização e Migrações

### 3.1 Aplicar migrações Alembic

```bash
export PYTHONPATH=.
uv run alembic upgrade head
```

### 3.2 Iniciar Painel CareerPilot + ATS Fictício Local (modo recomendado)

```bash
./scripts/dev.sh
```

O script `scripts/dev.sh`:
1. Executa `uv run alembic upgrade head`
2. Sobe o ATS Fictício Local em `http://127.0.0.1:8010` (`scripts/run_fake_ats.py`)
3. Sobe o CareerPilot em `http://127.0.0.1:8000`

Para iniciar em terminais separados:

```bash
# Terminal 1 — ATS Fictício Local [APENAS DADOS DE TESTE]
export PYTHONPATH=.
uv run python scripts/run_fake_ats.py

# Terminal 2 — CareerPilot Backend + Painel UI
export PYTHONPATH=.
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

### 3.3 Popular banco local com fixtures (opcional via CLI)

```bash
export PYTHONPATH=.
uv run python scripts/seed_demo.py
```

### 3.4 Acessar no Navegador

- Painel CareerPilot: `http://127.0.0.1:8000`
- Saúde da API: `http://127.0.0.1:8000/api/health`
- Saúde do ATS Fictício: `http://127.0.0.1:8010/health`

Código inicial de desbloqueio da demonstração: `demo-local-2026`.

## 4. Fluxo Operacional Seguro no Painel

1. **Desbloqueio**: informe o código local para obter sessão cookie `HttpOnly` + token CSRF.
2. **Radar**: clique em *Buscar Vagas (30+ fixtures)* para coletar, normalizar, sanitizar HTML, deduplicar e ranquear vagas por competências verificadas.
3. **Vaga**: inspecione requisitos obrigatórios/desejáveis e explicação do score. Marque explicitamente o consentimento de privacidade caso deseje incluí-lo no pacote e clique em *Preparar Candidatura + Gerar Currículo PDF*.
4. **Currículos**: confira a extração via `pypdf` (*Pré-visualizar Texto ATS & Diff*) garantindo 0 afirmações inventadas e proveniência (`FactEvidence`) para cada item.
5. **Aprovações**: revise o `packet_hash` e SHA-256 do PDF. Somente após clicar em **Aprovar** (registro único no banco) a automação pode executar.
6. **Execução**:
   - Com `DRY_RUN=true`: preenche o formulário, anexa o PDF e para antes de enviar (`ready_for_manual_review`), salvando screenshot em `./data/artifacts/evidence/`.
   - Em vaga `automated_allowed` no ATS Fictício Local com `DRY_RUN=false` e `LIVE_MODE_ENABLED=true`: envia, valida protocolo (`REC-...`) e marca `verified` na aba *Candidaturas*.
   - Em vaga `manual_only`: nunca clica em Enviar; permite *Marcar como enviada manualmente* informando protocolo obtido pelo usuário.

## 5. Passos Manuais para APIs Externas e Gmail OAuth (Opcional)

1. **Brave Search / Adzuna / Tavily / Himalayas**: crie sua conta desenvolvedor no provedor desejado e preencha `BRAVE_SEARCH_API_KEY`, `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, `TAVILY_API_KEY` ou `HIMALAYAS_API_KEY` no `.env`. Ative o conector em *Configurações* apenas quando quiser tráfego real (`use_live_network=true`).
2. **Gmail Desktop OAuth (`gmail_oauth.py`)**:
   - Crie credenciais OAuth Desktop no Google Cloud Console (sem publicar app).
   - Salve `client_secret.json` fora do repositório e aponte `GMAIL_CLIENT_SECRETS_PATH` e `GMAIL_TOKEN_PATH` no `.env`.
   - Comece exclusivamente com escopo mínimo `gmail.readonly` para ingerir alertas oficiais (`manual_only`) e classificar respostas de recrutadores sem envio automático.
3. **LLM Externo / Ollama Local**:
   - Para **Ollama local**: `LLM_PROVIDER=ollama`, `LLM_BASE_URL=http://127.0.0.1:11434/v1`, `LLM_MODEL=qwen2.5:7b-instruct`.
   - Para API compatível com OpenAI: preencha `LLM_PROVIDER=openai_compat`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` e ajuste `LLM_DAILY_BUDGET_USD`.

## 6. Validação Automatizada (`smoke.sh`)

```bash
./scripts/smoke.sh
```

Executa em sequência: `ruff check`, `mypy backend/app` e `pytest` (unit + integration + security + E2E Playwright).

## 7. Solução de Problemas Comuns

- **Erro no Playwright buscando `chromium_headless_shell`**: o `BrowserDriver` localiza automaticamente builds em `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`. Caso não haja nenhum binário instalado, rode `uv run playwright install chromium` ou configure `PLAYWRIGHT_CHROMIUM_PATH`.
- **Aplicação bloqueada com erro 409 (`Hash divergiu` ou `replay`)**: o pacote aprovado é imutável e de uso único; gere um novo pacote na aba *Vaga* após alterar respostas, descrição ou currículo.
- **Limpeza local**: utilize o botão *Apagar Dados Locais* em *Configurações* (ou `DELETE /api/data`) para remover banco/artefatos gerados em testes locais.
