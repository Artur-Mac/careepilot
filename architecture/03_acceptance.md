# Aceitação e Validação — CareerPilot (Copiloto CDP por Etapa)

Data da verificação: `2026-11-13` · Branch: `integration-review` (sobre commits `006cc8ce` baseline, `98b53a9` Fase 1, `f8ae7df` Fase 2)
Ambiente observado: `Python 3.12.3`, `Chrome/156.0.8078.4` (Playwright Chromium, CDP `1.3`), SQLite + Alembic `c2e4a6b8d002 (head)`.

## 1. Critérios Automatizados (Unidade, Segurança, Migração e E2E)

| Área | Cenários cobertos | Veredito |
| --- | --- | --- |
| **Fase 1 — Master Profile e PDF fiel** | Migração `adf094dd1fee -> b1d2e3f4a501 -> c2e4a6b8d002` preservando `ProfileFact` reais e descartando demodata; CAS `expected_revision` (`409` em conflito); geração de PDF aterrada a IDs do `MasterProfile`; conclusão manual (`done`) | **pass** |
| **Fase 2 — Descoberta CDP e feed** | Migração `c2e4a6b8d002` (`discovery_runs`, `job_dismissals`, `eligibility_json`, `summary_text`); recuperação de runs interrompidos; `POST /api/jobs/discover` (HTTP 202) isolando falhas por conector; resumo sob demanda em cache; **Dispensar** e paginação por cursor | **pass** |
| **Fase 3 — Copiloto CDP por etapa** | Classificação de categorias/assinaturas semântica; recusa estrita de CPF/OTP (`sensitive_manual`), LGPD/termos (`consent_required`), CAPTCHA (`captcha`), iframe cross-origin (`unknown_origin`), upload não-currículo e controles ambíguos; preservação de campos editados pelo usuário; revelação condicional intra-etapa via radio button; `FieldPlan` LLM rejeitando `evidence_id` forjado/cross-category com degradação segura; reuso de resposta entre portais via `question_signature`; rollback atômico em conflito CAS `profile_revision`; recuperação por **Anexar aba aberta** com `tab_id` opaco e zero clique em *Avançar* / *Enviar* (`POST /submit == 0`) | **pass** |
| **Segurança e fronteiras** | Loopback obrigatório, sessão `HttpOnly`/`SameSite=Strict`, CSRF + `Origin`, SSRF em redirecionamentos/IPs privados/CDP URL, sanitização anti-injection e remoção de textos ocultos no DOM | **pass** |
| **Alembic e integridade de schema** | Banco novo (`upgrade head`, `current`, `check` sem diferenças) e upgrade sintético (`adf094dd1fee -> head`, `current`, `check` sem diferenças) em diretório temporário isolado | **pass** |

Comandos executados (com `exit code 0`):

- `./scripts/smoke.sh` (`uv run --locked --extra dev ruff check backend scripts`, `uv run --locked --extra dev mypy backend/app`, `uv run --locked --extra dev pytest`)
- `uv sync --locked --extra dev && uv run --locked --extra dev ruff check backend scripts && uv run --locked --extra dev mypy backend && uv run --locked --extra dev pytest -v` (`52 passed`)
- `DATABASE_URL="sqlite:///<tmp>/fresh.db" uv run --locked alembic -c alembic.ini upgrade head && current && check`
- `DATABASE_URL="sqlite:///<tmp>/upgrade.db" uv run --locked alembic -c alembic.ini upgrade adf094dd1fee && upgrade head && current && check`

## 2. Sondas Vivas Isoladas (`CAREERPILOT_DATA_DIR` temporário)

Executadas em diretório temporário (`mktemp -d`) sem alterar `data/real/` (`access.txt`, `careerpilot.db`, `-shm`, `-wal` preservados intactos).

| Sonda viva | Comportamento observado | Veredito |
| --- | --- | --- |
| `./scripts/start_browser.sh --headless=new --no-sandbox --disable-gpu` + `./scripts/run_real.sh` | `/json/version` respondeu `Chrome/156.0.8078.4` (`Protocol-Version 1.3`); `/api/health` retornou HTTP 200 com `database_revision: "c2e4a6b8d002"`; `/api/auth/unlock` aceitou passcode gerado em `access.txt`; `/api/browser/status` retornou `connected: true`, `mode: "cdp_connected"` | **pass** |
| **Persistência CDP no reinício do FastAPI** | Aba aberta no Chrome CDP (`Tab Persistente`) permaneceu viva em `/json/list` após encerrar `run_real.sh`; ao reiniciar `run_real.sh`, `/api/browser/status` reconectou com sucesso (`connected: true`, `open_tabs: 2`) | **pass** |
| **Janela GUI visível do Chrome (`./scripts/start_browser.sh` sem `--headless=new`)** | Em modo gráfico Wayland nesta sessão CLI automatizada, `/json/version` não respondeu dentro da janela de 15s (processo iniciou com aviso Wayland sem expor porta CDP no prazo). Fluxo interativo visível requer validação manual no desktop do operador | **blocked** |
| **Ollama local (`http://127.0.0.1:11434/api/tags`)** | Binário `ollama` ausente e conexão local recusada (`ConnectionRefusedError`). Operação determinística offline (`LLM_PROVIDER=offline`) validada nos testes automatizados | **blocked** |
| **LinkedIn (`LinkedInBrowserCollector`, perfil limpo sem login)** | Coletou 5 cards públicos (`Senior Software Engineer (Python) - Remote Work | REF#308246` — `BairesDev`), porém acesso direto à busca exibe prompt de login (`sign in`); fluxo de candidatura autenticado não testado sem sessão real do usuário | **pass (descoberta pública) / blocked (fluxo autenticado)** |
| **Gupy (`GupyBrowserCollector`, perfil limpo sem login)** | Página carregou HTTP 200 sem CAPTCHA e extraiu 5 vagas reais; candidatura em portal autenticado não executada sem credenciais reais do usuário | **pass (descoberta) / blocked (candidatura autenticada)** |
| **InfoJobs (`InfoJobsBrowserCollector`, perfil limpo sem login)** | Página carregou HTTP 200 sem CAPTCHA e extraiu 5 vagas reais (`Desenvolvedor Python`, observada captura de nota `"4,3"` no campo de empresa quando ausente); candidatura autenticada não testada | **pass (descoberta) / blocked (candidatura autenticada)** |
| **Google (`GoogleCareersBrowserCollector`, perfil limpo sem login)** | Navegação redirecionou para `/sorry/index` com verificação anti-bot/CAPTCHA; coletor levantou `CollectorBlockedError` sem tentar contorno | **blocked** |
