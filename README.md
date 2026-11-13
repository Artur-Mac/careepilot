# CareerPilot

Assistente local-first em **pt-BR** para busca de vagas em modo remoto ou em **Recife**, geração de currículo PDF fiel ao **Perfil mestre** e copiloto de preenchimento por etapa no **Chrome/Chromium real via CDP**.

## Fluxos principais no painel

1. **Vagas**: descobre vagas via sessões autenticadas no Chrome conectado por CDP (LinkedIn, Gupy, InfoJobs, páginas de carreira e buscas no Google) com filtros de busca, elegibilidade para o Brasil, resumo sob demanda e **Dispensar** / **Preparar & Aplicar**.
2. **Candidatura**: gera currículo PDF aterrado exclusivamente no **Perfil mestre**, abre ou vincula a aba da vaga no Chrome via CDP e preenche apenas a etapa visível atual com **Preencher etapa atual**.
   - **Sem envio automático**: o usuário revisa tudo no navegador e clica manualmente em *Avançar*, *Continuar* ou *Enviar candidatura*.
   - **Bloqueios humanos explicitados**: perguntas faltantes podem ser respondidas no painel (com opção de salvar em `standard_answers`), enquanto CAPTCHA, login/OTP, consentimento LGPD/termos e documentos sensíveis (CPF/RG) permanecem bloqueados para ação direta no Chrome.
   - **Recuperação de aba**: caso a aba mude ou feche, **Anexar aba aberta** lista apenas identificadores opacos de abas abertas para revincular a candidatura.
   - **Conclusão manual**: **Marcar como concluída** registra o estado `done` após o usuário concluir no portal.
3. **Perfil mestre**: editor estruturado do documento JSON único (`MasterProfile`) com controle de revisão (`expected_revision`) e importação assistida de CV/texto (`PDF`, `DOCX`, `TXT`, `JSON`).

## Execução rápida (modo local real)

```bash
uv sync --locked --extra dev
```

Em **dois terminais** dentro do diretório do repositório:

1. **Terminal 1 — Navegador CDP persistente** (faça login manualmente no LinkedIn, Gupy e InfoJobs):

```bash
./scripts/start_browser.sh
```

2. **Terminal 2 — Banco Alembic + servidor FastAPI (`127.0.0.1:8000`)**:

```bash
./scripts/run_real.sh
```

Para ambiente de desenvolvimento isolado em `data/dev` (passcode padrão `demo-local-2026`):

```bash
./scripts/dev.sh
```

Mais detalhes operacionais, variáveis de ambiente e configuração de LLM local (Ollama) ou compatível com OpenAI estão em [docs/runbook.md](docs/runbook.md).

## Validação automatizada

```bash
./scripts/smoke.sh
```

Executa `ruff check backend scripts`, `mypy backend/app` e `pytest` (unitários, integração, segurança/SSRF, migrações Alembic e E2E com Chromium/CDP).

## Limites e garantias de segurança

- Binding exclusivo em loopback (`127.0.0.1`, `localhost`, `::1`); `BROWSER_CDP_URL` restrito a loopback via `validate_cdp_url`.
- Autenticação local por passcode único, sessão em cookie `HttpOnly`/`SameSite=Strict`, CSRF + validação de `Origin`.
- Proteção SSRF na camada HTTP/redirecionamentos e classificação determinística/aterrada dos campos do formulário (sem invenção de alegações fora do `MasterProfile` nem evasão de antibot/CAPTCHA).
