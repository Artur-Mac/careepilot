# CareerPilot MVP

Assistente local-first em pt-BR para descoberta de vagas, ranqueamento explicável, geração de currículos PDF rastreáveis, aprovação humana explícita por candidatura e preenchimento assistido em ATS permitidos com dupla trava de segurança (`DRY_RUN=true` por padrão).

> **Aviso de Privacidade e Demonstração**: Todos os dados incluídos nos seeds e fixtures (`Marina Costa Duarte` em `backend/tests/fixtures/fictional_profile.py`) são **100% fictícios**. Nunca versione `.env`, chaves de API, tokens OAuth ou documentos pessoais (CPF/RG/endereço).

---

## Como Iniciar Rapidamente (Painel + ATS Fictício Local)

### Pré-requisitos
- Python `>= 3.12` e [`uv`](https://docs.astral.sh/uv/)
- Chromium do Playwright instalado (`uv run playwright install chromium`, ou build em `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`)
- Fontes TTF Unicode (`fonts-noto-core` ou `fonts-dejavu-core` no Linux) para geração/extração limpa de acentos pt-BR via `reportlab` e `pypdf`

### 1. Instalar dependências e configurar ambiente (opcional)
```bash
uv sync
cp .env.example .env
```

### 2. Iniciar aplicação + ATS Fictício Local
```bash
./scripts/dev.sh
```
O script executa automaticamente:
1. Migrações Alembic em SQLite WAL (`uv run alembic upgrade head`)
2. Servidor **ATS Fictício Local** `[APENAS DADOS DE TESTE]` em `http://127.0.0.1:8010` (`scripts/run_fake_ats.py`)
3. Painel Web e API **CareerPilot** em `http://127.0.0.1:8000`

- Acesse **`http://127.0.0.1:8000`** no navegador
- Código de desbloqueio padrão da demonstração: **`demo-local-2026`** (configurável em `CAREERPILOT_PASSCODE`)
- Seed opcional via linha de comando (executa descoberta offline de 40+ vagas e avaliação inicial):
  ```bash
  export PYTHONPATH=.
  uv run python scripts/seed_demo.py
  ```

### Acompanhar o navegador do robô

Em uma sessão gráfica, inicie com `PLAYWRIGHT_HEADLESS=false ./scripts/dev.sh`.
No painel, busque vagas, abra a vaga fictícia de etapa única e gere o PDF.
Marque o consentimento do formulário de teste, aprove em **Aprovações** e clique em
**Executar (Dry-Run / Preencher)**. O Chromium abre enquanto preenche o formulário
e fecha ao terminar. O modo dry-run mantém o envio desativado.

### Buscar vagas reais

Preencha cargo ou palavras-chave em **Buscar vagas reais**. O painel começa com
**Recife** e **Incluir vagas remotas**, aceitando vagas de Recife ou remotas.
**Buscar em fontes públicas** consulta Arbeitnow, Himalayas e Remotive. Configure slugs separados por vírgula em
`GREENHOUSE_BOARDS`, `ASHBY_BOARDS` e `LEVER_BOARDS` para incluir páginas de carreira
específicas. `BRAVE_SEARCH_API_KEY` habilita a busca web ampla e a busca focada em ATS.
Os atalhos Google e LinkedIn abrem a consulta pronta para uso manual.

Use **Resumir vaga** na página da vaga. Com o provedor padrão offline, o painel exibe
um trecho da fonte. Um provedor compatível configurado via `LLM_PROVIDER`,
`LLM_BASE_URL`, `LLM_MODEL` e, se necessário, `LLM_API_KEY` gera um resumo a partir da
descrição pública. Importe seu currículo em **Currículos** e confirme os fatos antes
de gerar um PDF para uma vaga real. O PDF aprovado e o link original ficam no painel
para encaminhamento manual ao portal.

Com `FEATURE_VISUAL_FALLBACK=true` e um modelo configurado, o teste local de
preenchimento pode usar o modelo para mapear rótulos de contato desconhecidos.
A chamada recebe rótulos e tipos de campo, sem os valores do candidato. O driver
continua responsável pelo preenchimento e pelo controle de envio.

### 3. Rodar verificação completa (Lint + Mypy + Unit + Integration + Security + E2E Browser)
```bash
./scripts/smoke.sh
```

---

## Principais Módulos e Documentação

- [`docs/runbook.md`](docs/runbook.md): variáveis de `.env`, operação segura (`DRY_RUN`, `LIVE_MODE_ENABLED`, `manual_only`), OAuth/LLM opcionais e solução de problemas.
- [`docs/integration-notes.md`](docs/integration-notes.md): contratos congelados implementados, matriz completa de conectores, rotas da API/ATS fictício, checklist de segurança para revisão e limitações conhecidas.
- [`docs/research.md`](docs/research.md): pesquisa de licenças de projetos open-source e regras oficiais das APIs públicas avaliadas.
- [`docs/whatsapp-future.md`](docs/whatsapp-future.md): desenho futuro e pré-requisitos oficiais para adaptador opcional WhatsApp Cloud API (não ativo no MVP).
- [`alembic/versions/adf094dd1fee_initial_schema.py`](alembic/versions/adf094dd1fee_initial_schema.py): schema inicial das 22 tabelas relacionais em SQLite WAL / PostgreSQL.

---

## Controles de Segurança Embutidos

- **Localhost exclusivo**: recusa inicialização fora de `127.0.0.1`, `localhost` ou `::1`, exigindo autenticação por sessão `HttpOnly` + header `X-CSRF-Token` + validação estrita de `Origin`.
- **Aprovação unitária e anti-replay**: cada candidatura gera um `ApprovalRequest` vinculado ao `packet_hash` (vaga + respostas + SHA-256 do PDF) consumido atomicamente uma única vez no banco.
- **Anti-SSRF e confinamento de domínio no Playwright**: URLs privadas/metadados bloqueadas; automação bloqueia redirecionamentos fora do domínio permitido (`domain_blocked`) e para imediatamente diante de CAPTCHA (`blocked`) sem tentativas de bypass.
- **Zero fabricação de fatos**: currículos PDF gerados somente com evidências registradas em `ProfileFact`/`FactEvidence` e verificados via extração `pypdf`.
