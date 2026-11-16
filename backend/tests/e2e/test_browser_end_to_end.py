"""Phase 1 Playwright E2E UI smoke test (expanded in Phase 3 with CDP portal copilot flow)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from backend.app.core.config import Settings
from backend.app.db.session import get_session_factory
from backend.app.models.tables import JobPosting


def _resolve_local_chromium() -> str | None:
    import os
    import shutil

    env_bin = os.environ.get("CHROME_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    cache_root = Path.home() / ".cache" / "ms-playwright"
    if cache_root.exists():
        for candidate in sorted(cache_root.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
            if candidate.is_file():
                return str(candidate)
    vivaldi = Path("/opt/vivaldi/vivaldi")
    if vivaldi.is_file():
        return str(vivaldi)
    return None


@pytest.mark.asyncio
async def test_phase1_ui_profile_prepare_pdf_handoff_and_complete(
    live_app_server: tuple[str, Settings],
) -> None:
    """Real browser drives UI: unlock -> Perfil mestre edit/save -> Vagas Prepare & Apply -> Candidatura preview & complete."""
    app_url, cfg = live_app_server
    with get_session_factory(cfg)() as db:
        job = JobPosting(
            canonical_url="https://example.org/jobs/e2e-p1-1",
            dedup_signature="e2e-p1-sig-1",
            source="remotive",
            source_job_id="e2e-p1-1",
            company="Recife Analytics Ltda",
            title="Engenheira de Dados Júnior",
            location="Recife, PE",
            workplace_type="remote",
            description="Requisitos: Python, SQL, FastAPI, ETL e testes.",
            description_hash="e" * 64,
            job_url="https://example.org/jobs/e2e-p1-1",
            apply_url="https://example.org/jobs/e2e-p1-1/apply",
            discovered_at=datetime.now(UTC),
            source_attribution="Remotive",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    exe = _resolve_local_chromium()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=exe)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(app_url, wait_until="domcontentloaded")

            # Retired tabs/badges must NOT exist
            assert await page.locator('.tab-btn[data-tab="aprovacoes"]').count() == 0
            assert await page.locator("#badge-dry-run").count() == 0
            assert await page.locator("#badge-fictional").count() == 0

            # 1. Unlock
            await page.fill("#passcode-input", "test-e2e-passcode")
            await page.click('[data-testid="btn-unlock"]')
            await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=10000)

            # 2. Fill Perfil mestre & save with CAS
            await page.click('[data-testid="tab-perfil"]')
            await page.wait_for_selector("#prof-name", timeout=10000)
            await page.fill("#prof-name", "Clara Nogueira Sales")
            await page.fill("#prof-email", "clara.ui@example.org")
            await page.fill("#prof-city", "Recife")
            await page.fill("#prof-state", "PE")
            await page.fill("#prof-portfolio", "https://clara.portfolio.example.org")
            await page.click("#btn-add-skill")
            await page.fill('#profile-skills-list .profile-entry input[data-f="name"]', "Python")
            await page.click("#btn-add-exp")
            await page.fill('#profile-exp-list .profile-entry input[data-f="company"]', "Porto Labs")
            await page.fill('#profile-exp-list .profile-entry input[data-f="role"]', "Engenheira de Dados")
            await page.click('[data-testid="btn-save-profile"]')
            await page.wait_for_function(
                "() => document.getElementById('profile-revision-badge').innerText.includes('Revisão 1')",
                timeout=10000,
            )

            # 3. Switch to Vagas and click Preparar & Aplicar
            await page.click('[data-testid="tab-vagas"]')
            await page.wait_for_selector(f'[data-testid="btn-prepare-job-{job_id}"]', timeout=10000)
            await page.click(f'[data-testid="btn-prepare-job-{job_id}"]')

            # 4. Candidatura renders PDF download, safe external job link, text preview, and completion action
            await page.wait_for_selector("#tab-candidatura:not(.hidden) [data-testid='btn-download-pdf']", timeout=15000)
            link_href = await page.get_attribute('[data-testid="btn-open-job-link"]', "href")
            assert link_href == "https://example.org/jobs/e2e-p1-1/apply"

            await page.click("#btn-toggle-pdf-preview")
            await page.wait_for_selector("#candidatura-pdf-preview:not(.hidden)", timeout=10000)
            pv_text = await page.locator("#candidatura-pdf-preview").inner_text()
            assert "Clara Nogueira Sales" in pv_text
            assert "clara.portfolio.example.org" in pv_text

            # 5. Mark application complete
            await page.click('[data-testid="btn-complete-app"]')
            await page.wait_for_function(
                "() => document.getElementById('candidatura-active').innerText.includes('Concluída')",
                timeout=10000,
            )
        finally:
            await context.close()
            await browser.close()

    resumes = list((Path(cfg.artifacts_path) / "resumes").glob("*.pdf"))
    assert len(resumes) == 1
    assert resumes[0].read_bytes().startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_phase2_ui_discovery_status_pagination_summary_and_dismiss(
    tmp_path: Path,
) -> None:
    """Real browser drives Phase 2 Vagas UI: status banner, async discover polling, Carregar mais, summary cache, and Dispensar."""
    from collections.abc import Iterator
    from datetime import timedelta

    from backend.app.api.deps import get_discovery_collectors_override
    from backend.app.browser_automation.session import BrowserSession
    from backend.app.connectors.browser.base import (
        BrowserCollector,
        BrowserDiscoveryPlan,
        CollectorBlockedError,
    )
    from backend.app.db.migrations import prepare_and_upgrade_database
    from backend.app.db.session import get_db, reset_engine_cache
    from backend.app.main import create_app
    from backend.app.schemas.contracts import NormalizedJob
    from backend.tests.conftest import _find_free_port, _LiveUvicornThread

    chromium_bin = _resolve_local_chromium()
    if chromium_bin is None:
        pytest.skip("Nenhum binário Chrome/Chromium instalado para executar E2E Playwright.")

    class BlockedLinkedIn(BrowserCollector):
        name = "linkedin"

        async def collect(
            self, session: BrowserSession, plan: BrowserDiscoveryPlan
        ) -> list[NormalizedJob]:
            raise CollectorBlockedError("LinkedIn authwall")

    class BulkGupy(BrowserCollector):
        name = "gupy"

        async def collect(
            self, session: BrowserSession, plan: BrowserDiscoveryPlan
        ) -> list[NormalizedJob]:
            now = datetime.now(UTC)
            return [
                NormalizedJob(
                    source="gupy",
                    source_job_id=f"ui-gp-{i}",
                    company=f"Recife Labs {i}",
                    title=f"Engenheira de Dados {i:02d}",
                    location="Recife, PE",
                    workplace_type="remote",
                    description=f"Vaga {i} com Python, SQL e FastAPI em Recife.",
                    job_url=f"https://gupy.example.org/jobs/ui-{i}",
                    apply_url=f"https://gupy.example.org/jobs/ui-{i}",
                    discovered_at=now - timedelta(minutes=i),
                    source_attribution="Gupy",
                    discovery_channel="browser",
                )
                for i in range(27)
            ]

    reset_engine_cache()
    port = _find_free_port()
    db_path = tmp_path / "p2_e2e.db"
    artifacts_dir = tmp_path / "p2_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    cfg = Settings(
        careerpilot_port=port,
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=str(artifacts_dir),
        careerpilot_passcode="test-e2e-passcode",
        careerpilot_session_secret="e2e-session-secret-xyz",
    )
    prepare_and_upgrade_database(cfg.database_url)
    app = create_app(cfg)
    sf = get_session_factory(cfg)

    def _override_db() -> Iterator[object]:
        db = sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_discovery_collectors_override] = lambda: [
        BlockedLinkedIn(),
        BulkGupy(),
    ]

    with _LiveUvicornThread(app, "127.0.0.1", port) as srv:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chromium_bin, headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(srv.url, wait_until="networkidle")
                await page.fill("#passcode-input", "test-e2e-passcode")
                await page.click('[data-testid="btn-unlock"]')
                await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=10000)

                # OpenCode extension status badge + chat panel render cleanly on unlock
                await page.wait_for_function(
                    "() => { const t = document.querySelector('#browser-status-badge')?.textContent || ''; return t.includes('Conectada') || t.includes('Desconectada'); }"
                )
                await page.wait_for_selector("#opencode-chat-panel:not(.hidden)", timeout=5000)

                assert await page.locator("html").evaluate(
                    "(el) => getComputedStyle(el).colorScheme"
                ) == "dark"
                await page.get_by_test_id("tab-configuracoes").click()
                assert await page.locator("#system-status-bar").is_visible()
                await page.get_by_test_id("tab-vagas").click()

                # Submit with empty profile role & blank override -> 422 link to #perfil
                await page.click('[data-testid="btn-discover"]')
                await page.wait_for_selector('#discovery-run-status a[href="#perfil"]', timeout=5000)

                # Fill role override -> 202 run completes showing partial linkedin alert & first page (25/27)
                await page.fill("#discover-role-override", "Engenheira de Dados")
                await page.click('[data-testid="btn-discover"]')
                await page.wait_for_function(
                    "() => document.querySelector('#discovery-run-status')?.textContent?.includes('linkedin')",
                    timeout=10000,
                )
                await page.wait_for_function(
                    "() => document.querySelector('#vagas-count')?.textContent?.includes('25 vaga(s)+')",
                    timeout=10000,
                )

                # Click Carregar mais -> loads remaining 2 jobs (27 total) and hides pagination button
                await page.click("#btn-load-more-jobs")
                await page.wait_for_function(
                    "() => document.querySelector('#vagas-count')?.textContent?.trim() === '27 vaga(s)'",
                    timeout=10000,
                )
                assert await page.locator("#btn-load-more-jobs.hidden").count() == 1

                # Open first job detail -> generate excerpt summary -> second click returns cache
                first_card = page.locator("article.job-card").first
                first_job_id = await first_card.get_attribute("data-job-id")
                assert first_job_id
                await page.click(f'[data-testid="btn-detail-job-{first_job_id}"]')
                await page.click("#btn-job-summary")
                await page.wait_for_function(
                    "() => document.querySelector('#job-summary')?.textContent?.includes('Trecho')"
                )

                # Dismiss first job -> disappears from active feed
                await page.click(f'[data-testid="btn-dismiss-job-{first_job_id}"]')
                await page.wait_for_function(
                    f"() => !document.querySelector('[data-job-id=\"{first_job_id}\"]')",
                    timeout=10000,
                )
            finally:
                await browser.close()

    reset_engine_cache()


@pytest.mark.asyncio
async def test_phase3_ui_multistep_cdp_copilot_recovery_and_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive full UI -> CDP Chrome multi-step copilot, conditional reveal, Q&A save, tab recovery, and completion."""
    chrome_bin = _resolve_local_chromium()
    if chrome_bin is None:
        pytest.skip("Chrome/Chromium local não encontrado.")

    import asyncio
    import http.server
    import socket
    import subprocess
    import threading
    import time
    import urllib.request
    from collections.abc import Iterator
    from datetime import UTC, datetime
    from typing import Any

    from fastapi import FastAPI
    from playwright.async_api import async_playwright

    from backend.app.api.deps import get_browser_session
    from backend.app.browser_automation.session import BrowserSession
    from backend.app.core.config import Settings, get_settings
    from backend.app.db.migrations import prepare_and_upgrade_database
    from backend.app.db.session import get_db, get_session_factory, reset_engine_cache
    from backend.app.main import create_app
    from backend.app.models.tables import JobPosting, MasterProfile, UserSettings
    from backend.tests.conftest import _LiveUvicornThread
    from backend.tests.fixtures.sample_profile import build_sample_master_profile

    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    portal_port = _free_port()
    cdp_port = _free_port()
    ui_port = _free_port()
    portal_origin = f"http://127.0.0.1:{portal_port}"
    cdp_url = f"http://127.0.0.1:{cdp_port}"

    portal_state = {"step2_gets": 0, "submit_posts": 0}

    class _MockPortalHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/apply-step2":
                portal_state["step2_gets"] += 1
            pages = {
                "/apply-start": """
                <html>
                  <head><title>Candidatura - Engenheira Backend Python | Empresa Recife</title></head>
                  <body>
                    <h1>Candidatura: Engenheira Backend Python</h1>
                    <form id="step1-form" action="/apply-step2" method="GET">
                      <label for="s1-name">Nome completo</label>
                      <input id="s1-name" type="text" required />
                      <label for="s1-email">E-mail</label>
                      <input id="s1-email" type="email" required />
                      <fieldset>
                        <legend>Possui autorização para trabalhar no Brasil?</legend>
                        <label><input id="s1-auth-yes" type="radio" name="work_auth" value="Sim"
                          onchange="document.getElementById('cond-portfolio').style.display='block';" /> Sim</label>
                        <label><input id="s1-auth-no" type="radio" name="work_auth" value="Não" /> Não</label>
                      </fieldset>
                      <div id="cond-portfolio" style="display:none;">
                        <label for="s1-portfolio">Portfólio pessoal</label>
                        <input id="s1-portfolio" type="url" />
                      </div>
                      <button type="button" id="btn-next"
                        onclick="window.__nextClicks=(window.__nextClicks||0)+1; window.location.href='/apply-step2';">
                        Avançar para Etapa 2
                      </button>
                    </form>
                  </body>
                </html>
                """,
                "/apply-step2": """
                <html>
                  <head><title>Etapa 2 - Engenheira Backend Python</title></head>
                  <body>
                    <h1>Etapa 2: Documentos e Perguntas</h1>
                    <form id="step2-form" action="/submit" method="POST">
                      <label for="s2-cv">Currículo (PDF)</label>
                      <input id="s2-cv" type="file" accept=".pdf" required />
                      <label for="s2-q">Qual sua experiência com arquiteturas Python e SQL?</label>
                      <textarea id="s2-q" required></textarea>
                      <label for="s2-salary">Pretensão salarial mensal (BRL)</label>
                      <input id="s2-salary" type="number" min="0" max="50000" step="0.01" />
                      <label for="s2-range">Faixa salarial mensal (BRL)</label>
                      <input id="s2-range" type="text" placeholder="1000 - 2000" />
                      <label for="s2-salary-option">Faixa salarial anual (USD)</label>
                      <select id="s2-salary-option">
                        <option value="">Selecione</option>
                        <option value="band-one">Até 60000</option>
                        <option value="band-two">60000 a 80000</option>
                        <option value="band-disabled" disabled>Faixa indisponível</option>
                      </select>
                      <label><input id="s2-lgpd" type="checkbox" required /> Li e concordo com os termos LGPD</label>
                      <button type="submit" id="btn-submit"
                        onclick="window.__submitClicks=(window.__submitClicks||0)+1;">
                        Enviar candidatura
                      </button>
                    </form>
                  </body>
                </html>
                """,
                "/captcha-step": """
                <html>
                  <head><title>Verificação - Engenheira Backend Python</title></head>
                  <body>
                    <form id="captcha-form">
                      <div class="g-recaptcha"></div>
                      <label for="s3-name">Nome completo</label>
                      <input id="s3-name" type="text" required />
                    </form>
                  </body>
                </html>
                """,
            }
            body = pages.get(path, "<html><body>OK</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] == "/submit":
                portal_state["submit_posts"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body>Recebido</body></html>")

        def log_message(self, format: str, *args: Any) -> None:
            return

    portal_srv = http.server.ThreadingHTTPServer(("127.0.0.1", portal_port), _MockPortalHandler)
    threading.Thread(target=portal_srv.serve_forever, daemon=True).start()

    cdp_profile = tmp_path / "cdp-profile"
    chrome_proc = subprocess.Popen(
        [
            chrome_bin,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={cdp_profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except Exception:
            await asyncio.sleep(0.15)
    else:
        chrome_proc.terminate()
        portal_srv.shutdown()
        pytest.fail("Chrome CDP de E2E não iniciou a tempo.")

    reset_engine_cache()
    get_settings.cache_clear()
    artifacts_dir = tmp_path / "e2e_p3_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "e2e_p3.db"
    passcode = "e2e-p3-passcode"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    monkeypatch.setenv("BROWSER_CDP_URL", cdp_url)
    monkeypatch.setenv("CAREERPILOT_HOST", "127.0.0.1")
    monkeypatch.setenv("CAREERPILOT_PORT", str(ui_port))
    monkeypatch.setenv("CAREERPILOT_PASSCODE", passcode)
    monkeypatch.setenv("CAREERPILOT_SESSION_SECRET", "e2e-p3-secret")
    get_settings.cache_clear()
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=str(artifacts_dir),
        browser_cdp_url=cdp_url,
        careerpilot_host="127.0.0.1",
        careerpilot_port=ui_port,
        careerpilot_passcode=passcode,
        careerpilot_session_secret="e2e-p3-secret",
    )
    prepare_and_upgrade_database(settings.database_url)
    sf = get_session_factory(settings)
    doc = build_sample_master_profile()
    with sf() as db:
        db.add(UserSettings(id="local-applicant"))
        db.add(
            MasterProfile(
                user_id="local-applicant",
                document=doc.model_dump(mode="json"),
                revision=1,
            )
        )
        job = JobPosting(
            canonical_url=f"{portal_origin}/apply-start",
            dedup_signature="e2e-p3-job-sig",
            source="gupy",
            source_job_id="e2e-p3-1",
            company="Empresa Recife",
            title="Engenheira Backend Python",
            location="Recife, PE",
            workplace_type="hybrid",
            description="Vaga backend Python e SQL em Recife com trabalho híbrido.",
            description_hash="c" * 64,
            job_url=f"{portal_origin}/apply-start",
            apply_url=f"{portal_origin}/apply-start",
            discovered_at=datetime.now(UTC),
            source_attribution="Gupy",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    app = create_app(settings)
    assert isinstance(app, FastAPI)

    def _override_db() -> Iterator[object]:
        db = sf()
        try:
            yield db
        finally:
            db.close()

    def _override_session() -> BrowserSession:
        # Replace lifespan BrowserSession on uvicorn's loop once so test origin is permitted and shut down cleanly.
        if not getattr(app.state, "_e2e_test_origins_bound", False):
            app.state.browser_session = BrowserSession(
                settings, allow_test_origins={portal_origin}
            )
            app.state._e2e_test_origins_bound = True
        return app.state.browser_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_browser_session] = _override_session

    saved_answer = "Experiência sólida de 5 anos em Python, FastAPI e PostgreSQL."

    fake_cli = tmp_path / "fake-opencode-cli"
    fake_cli.write_text(
        """#!/usr/bin/env python3
import json, sys
prompt = sys.argv[-1]
assert "Clara Nogueira Sales" in prompt, f"Missing profile name in prompt: {prompt[:200]}"
print(json.dumps({"type": "step_start", "sessionID": "ses_test_opencode_e2e"}), flush=True)
print(json.dumps({"type": "tool_use", "sessionID": "ses_test_opencode_e2e", "part": {"id": "p1", "type": "tool", "tool": "browse", "state": {"status": "completed", "title": "browse snapshot"}}}), flush=True)
print(json.dumps({"type": "text", "sessionID": "ses_test_opencode_e2e", "part": {"id": "p2", "type": "text", "text": "Perfil de Clara Nogueira Sales e PDF pronto carregados no OpenCode via browse."}}), flush=True)
print(json.dumps({"type": "step_finish", "sessionID": "ses_test_opencode_e2e", "part": {"reason": "stop"}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("OPENCODE_CLI_BIN", str(fake_cli))

    try:
        with _LiveUvicornThread(app, "127.0.0.1", ui_port) as srv:
            async with async_playwright() as p:
                ui_browser = await p.chromium.launch(headless=True, executable_path=chrome_bin)
                cdp_browser = await p.chromium.connect_over_cdp(cdp_url)
                page = await ui_browser.new_page()
                try:
                    await page.goto(srv.url, wait_until="networkidle")
                    await page.fill("#passcode-input", passcode)
                    async with page.expect_response(
                        lambda r: r.url.endswith("/api/auth/unlock")
                    ) as unlock_resp_info:
                        await page.click('[data-testid="btn-unlock"]')
                    unlock_resp = await unlock_resp_info.value
                    assert unlock_resp.status == 200, await unlock_resp.text()
                    await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=8000)

                    # 1. Trigger Preparar PDF & Chat OpenCode -> generates PDF and opens OpenCode chat without forcing CDP
                    await page.click('[data-testid="tab-vagas"]')
                    await page.wait_for_selector(f'[data-testid="btn-prepare-job-{job_id}"]')
                    await page.click(f'[data-testid="btn-prepare-job-{job_id}"]')
                    await page.wait_for_selector('[data-testid="btn-send-to-opencode"]', timeout=15000)
                    await page.wait_for_selector("#opencode-context-bar:not(.hidden)", timeout=8000)
                    assert "PDF pronto" in await page.locator("#opencode-context-bar").inner_text()

                    # Send a chat message through the UI chat panel and verify OpenCode + browse response
                    await page.fill("#opencode-chat-input", "Verifique a aba no navegador")
                    await page.click('[data-testid="btn-send-opencode-chat"]')
                    await page.wait_for_function(
                        "() => (document.querySelector('#opencode-chat-messages')?.textContent || '').includes('Clara Nogueira Sales')",
                        timeout=15000,
                    )
                    assert "browse" in (await page.locator("#opencode-chat-messages").inner_text()).lower()

                    # Verify legacy backend CDP /open + /fill-current-page APIs directly on Step 1
                    await page.evaluate(
                        """async () => {
                            const ses = await (await fetch('/api/auth/session', { credentials: 'same-origin' })).json();
                            const apps = await (await fetch('/api/applications', { credentials: 'same-origin' })).json();
                            const appId = apps.applications[0].id;
                            const headers = { 'X-CSRF-Token': ses.csrf_token };
                            await fetch(`/api/applications/${appId}/open`, { method: 'POST', headers, credentials: 'same-origin' });
                            await fetch(`/api/applications/${appId}/fill-current-page`, { method: 'POST', headers, credentials: 'same-origin' });
                        }"""
                    )

                    portal_page = next(
                        (
                            pg
                            for pg in cdp_browser.contexts[0].pages
                            if "/apply-start" in (pg.url or "")
                        ),
                        None,
                    )
                    assert portal_page is not None
                    assert await portal_page.locator("#s1-name").input_value() == "Clara Nogueira Sales"
                    assert await portal_page.locator("#s1-email").input_value() == "clara.nogueira@example.org"
                    assert await portal_page.locator("#s1-auth-yes").is_checked() is True
                    assert (
                        await portal_page.locator("#s1-portfolio").input_value()
                        == "https://clara.dev.example.org"
                    )
                    assert portal_state["step2_gets"] == 0
                    assert portal_state["submit_posts"] == 0

                    # 2. Simulate user clicking Avançar to Step 2 -> backend fill-current-page
                    await portal_page.click("#btn-next")
                    await portal_page.wait_for_url("**/apply-step2")
                    assert portal_state["step2_gets"] == 1

                    fill2_json = await page.evaluate(
                        """async () => {
                            const ses = await (await fetch('/api/auth/session', { credentials: 'same-origin' })).json();
                            const apps = await (await fetch('/api/applications', { credentials: 'same-origin' })).json();
                            const appId = apps.applications[0].id;
                            const r = await fetch(`/api/applications/${appId}/fill-current-page`, {
                                method: 'POST',
                                headers: { 'X-CSRF-Token': ses.csrf_token },
                                credentials: 'same-origin',
                            });
                            return await r.json();
                        }"""
                    )
                    assert fill2_json["final_submit_detected"] is True

                    await page.reload(wait_until="networkidle")
                    await page.click('[data-testid="tab-candidatura"]')
                    await page.wait_for_selector('[data-testid="human-action-panel"]')
                    uploaded_name = await portal_page.locator("#s2-cv").evaluate(
                        "el => el.files && el.files.length === 1 ? el.files[0].name : ''"
                    )
                    assert uploaded_name.endswith(".pdf")
                    assert uploaded_name in fill2_json["uploaded_files"]
                    assert await portal_page.locator("#s2-lgpd").is_checked() is False
                    assert await portal_page.locator("#s2-q").input_value() == ""
                    assert portal_state["submit_posts"] == 0

                    assert await portal_page.locator("#s2-salary").input_value() == ""
                    amount_form = page.locator('.salary-approval-form[data-shape="amount"]')
                    range_form = page.locator('.salary-approval-form[data-shape="range"]')
                    option_form = page.locator('.salary-approval-form[data-shape="option"]')
                    await amount_form.locator('input[name="amount"]').fill("8500.50")
                    await range_form.locator('input[name="amount"]').fill("8000")
                    await range_form.locator('input[name="maximum"]').fill("9500")
                    assert await option_form.get_by_text("Faixa indisponível", exact=True).count() == 0
                    await option_form.locator("select").select_option(label="60000 a 80000")
                    for approval_form in (amount_form, range_form, option_form):
                        async with page.expect_response(
                            lambda r: r.url.endswith("/approve") and r.request.method == "POST"
                        ) as salary_resp_info:
                            await approval_form.locator("button").click()
                        salary_resp = await salary_resp_info.value
                        assert salary_resp.status == 200, await salary_resp.text()
                    assert await portal_page.locator("#s2-salary").input_value() == ""
                    assert await portal_page.locator("#s2-range").input_value() == ""
                    assert await portal_page.locator("#s2-salary-option").input_value() == ""

                    # 3. Answer Step 2 question in dashboard + save to Master Profile + backend refill
                    await page.fill("#step-answers-form .step-answer-input", saved_answer)
                    await page.check("#step-answers-form .step-save-profile-cb")
                    await page.check("#step-answers-form .step-save-arsenal-cb")
                    async with page.expect_response(
                        lambda r: r.url.endswith("/answer-questions") and r.request.method == "POST"
                    ) as save_ans_resp_info:
                        await page.click('[data-testid="btn-save-answers"]')
                    save_response = await save_ans_resp_info.value
                    assert save_response.status == 200, await save_response.text()
                    assert (await save_response.json())["arsenal_draft_count"] == 1
                    await page.wait_for_function(
                        "() => (document.querySelector('#toast')?.textContent || '').includes('Respostas salvas')",
                        timeout=10000,
                    )

                    await page.evaluate(
                        """async () => {
                            const ses = await (await fetch('/api/auth/session', { credentials: 'same-origin' })).json();
                            const apps = await (await fetch('/api/applications', { credentials: 'same-origin' })).json();
                            const appId = apps.applications[0].id;
                            await fetch(`/api/applications/${appId}/fill-current-page`, {
                                method: 'POST',
                                headers: { 'X-CSRF-Token': ses.csrf_token },
                                credentials: 'same-origin',
                            });
                        }"""
                    )
                    assert await portal_page.locator("#s2-q").input_value() == saved_answer
                    assert await portal_page.locator("#s2-salary").input_value() == "8500.50"
                    assert await portal_page.locator("#s2-range").input_value() == "8000 - 9500"
                    assert await portal_page.locator("#s2-salary-option").input_value() == "band-two"
                    assert await portal_page.locator("#s2-lgpd").is_checked() is False
                    assert portal_state["submit_posts"] == 0
                    assert (await portal_page.evaluate("() => window.__submitClicks || 0")) == 0

                    # 4. Close Step 2 portal tab, open CAPTCHA page in new tab, test backend attach-page + CAPTCHA block
                    await portal_page.close()
                    captcha_page = await cdp_browser.contexts[0].new_page()
                    await captcha_page.goto(
                        f"{portal_origin}/captcha-step", wait_until="domcontentloaded"
                    )
                    fill_captcha = await page.evaluate(
                        """async () => {
                            const ses = await (await fetch('/api/auth/session', { credentials: 'same-origin' })).json();
                            const apps = await (await fetch('/api/applications', { credentials: 'same-origin' })).json();
                            const appId = apps.applications[0].id;
                            const pagesRes = await (await fetch(`/api/applications/${appId}/pages`, { credentials: 'same-origin' })).json();
                            const capTab = pagesRes.pages.find((pg) => (pg.url || '').includes('/captcha-step'));
                            const headers = { 'X-CSRF-Token': ses.csrf_token, 'Content-Type': 'application/json' };
                            await fetch(`/api/applications/${appId}/attach-page`, {
                                method: 'POST',
                                headers,
                                body: JSON.stringify({ tab_id: capTab.tab_id }),
                                credentials: 'same-origin',
                            });
                            const r = await fetch(`/api/applications/${appId}/fill-current-page`, {
                                method: 'POST',
                                headers: { 'X-CSRF-Token': ses.csrf_token },
                                credentials: 'same-origin',
                            });
                            return await r.json();
                        }"""
                    )
                    assert any(b.get("code") == "captcha" for b in fill_captcha.get("blockers", []))
                    assert await captcha_page.locator("#s3-name").input_value() == ""

                    # 5. Complete application -> Concluída & saved answer appears in Perfil mestre
                    await page.reload(wait_until="networkidle")
                    await page.click('[data-testid="tab-candidatura"]')
                    await page.click('[data-testid="btn-complete-app"]')
                    await page.wait_for_function(
                        "() => (document.querySelector('#candidatura-active')?.textContent || '').includes('Concluída')"
                    )
                    await page.click('[data-testid="tab-perfil"]')
                    await page.wait_for_selector("#profile-std-list")
                    std_values = await page.evaluate(
                        """() => Array.from(
                            document.querySelectorAll('#profile-std-list [data-f="answer"]')
                        ).map((el) => el.value)"""
                    )
                    assert saved_answer in std_values
                finally:
                    await cdp_browser.close()
                    await ui_browser.close()
    finally:
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_proc.kill()
        portal_srv.shutdown()
        reset_engine_cache()
        get_settings.cache_clear()
