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

    launch_kwargs: dict[str, object] = {"headless": True}
    exe = _resolve_local_chromium()
    if exe:
        launch_kwargs["executable_path"] = exe

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
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

                # Browser status banner renders disconnected instruction safely
                await page.wait_for_function(
                    "() => document.querySelector('#browser-status-badge')?.textContent?.includes('Desconectado')"
                )

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
