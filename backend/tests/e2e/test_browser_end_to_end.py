"""True end-to-end Playwright browser tests against live CareerPilot UI and Fictional Local ATS."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from playwright.async_api import async_playwright

from backend.app.browser_automation.driver import BrowserDriver, _resolve_chromium_executable
from backend.app.browser_automation.fake_ats import _RECEIPTS
from backend.app.core.config import Settings
from backend.app.db.session import get_session_factory
from backend.app.models.tables import ApprovalRequest


async def _api_unlock(client: httpx.AsyncClient, passcode: str) -> dict[str, str]:
    resp = await client.post("/api/auth/unlock", json={"passcode": passcode})
    assert resp.status_code == 200
    data = resp.json()
    return {
        "Authorization": f"Bearer {data['access_token']}",
        "X-CSRF-Token": data["csrf_token"],
    }


@pytest.mark.asyncio
async def test_e2e_ui_full_happy_flow_single_step(
    live_e2e_servers: tuple[str, str, Settings],
) -> None:
    """Real Playwright browser drives UI: unlock -> discover -> inspect -> prepare PDF -> approve -> enable live -> submit -> verify receipt."""
    t0 = time.perf_counter()
    app_url, ats_url, cfg = live_e2e_servers
    exe = _resolve_chromium_executable(cfg)

    async with async_playwright() as p:
        launch_kwargs: dict[str, object] = {"headless": True}
        if exe:
            launch_kwargs["executable_path"] = exe
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(app_url, wait_until="domcontentloaded")

            # 1. Unlock local dashboard
            await page.fill("#passcode-input", "demo-local-2026")
            await page.click('[data-testid="btn-unlock"]')
            await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=10000)

            # 2. Discover 30+ mocked/fictional jobs via UI button
            await page.click('[data-testid="btn-discover"]')
            await page.wait_for_function(
                "() => document.querySelectorAll('#jobs-grid .job-card').length >= 10",
                timeout=15000,
            )

            # 3. Filter for automated fictional ATS jobs and open the single-step job
            await page.select_option("#filter-mode", "automated_allowed")
            await page.click("#btn-apply-filters")
            await page.wait_for_function(
                "() => document.querySelectorAll('#jobs-grid .job-card').length >= 1",
                timeout=10000,
            )
            single_card = page.locator("#jobs-grid .job-card", has_text="(1 Etapa)").first
            await single_card.locator(".btn-open-job").click()
            await page.wait_for_selector('#tab-vaga [data-testid="btn-prepare-app"]', timeout=10000)

            # 4. Tick explicit consent and prepare tailored PDF + immutable approval packet
            await page.check("#prep-consent-check")
            await page.click('[data-testid="btn-prepare-app"]')
            await page.wait_for_selector("#tab-aprovacoes:not(.hidden) .approval-card", timeout=15000)

            approval_card = page.locator("#approvals-list .approval-card", has_text="(1 Etapa)").first
            assert "awaiting_approval" in await approval_card.inner_text()

            # 5. Explicitly approve 1-use packet in UI
            await approval_card.locator(".btn-approve").click()
            await page.wait_for_function(
                "() => [...document.querySelectorAll('#approvals-list .approval-card')].some(c => c.innerText.includes('(1 Etapa)') && c.innerText.includes('approved'))",
                timeout=10000,
            )

            # 6. Enable Live Mode explicitly in Configurações tab (never on by default)
            await page.click('.tab-btn[data-tab="configuracoes"]')
            await page.wait_for_selector("#set-dry-run", timeout=10000)
            await page.uncheck("#set-dry-run")
            await page.check("#set-live-mode")
            await page.click('#settings-form button[type="submit"]')
            await page.wait_for_function(
                "() => document.getElementById('badge-dry-run').innerText.includes('LIVE TEST')",
                timeout=10000,
            )

            # 7. Switch back to Aprovações and click Executar e Enviar (Playwright fills ATS + uploads PDF + submits!)
            await page.click('.tab-btn[data-tab="aprovacoes"]')
            approved_card = page.locator("#approvals-list .approval-card", has_text="(1 Etapa)").first
            await approved_card.locator(".btn-exec-submit").click()

            # 8. Wait for automatic redirect to Candidaturas timeline with verified receipt!
            await page.wait_for_selector("#tab-candidaturas:not(.hidden) .timeline-card", timeout=25000)
            await page.wait_for_function(
                "() => document.querySelector('#applications-timeline').innerText.includes('verified') && document.querySelector('#applications-timeline').innerText.includes('REC-')",
                timeout=25000,
            )
            timeline_text = await page.locator("#applications-timeline").inner_text()
            m = re.search(r"\bREC-[A-F0-9]{8,}\b", timeline_text)
            assert m is not None, f"Comprovante REC-... ausente na UI: {timeline_text}"
            receipt_id = m.group(0)
        finally:
            await context.close()
            await browser.close()

    # Verify Fictional ATS backend genuinely received form + PDF bytes, and screenshots exist on disk
    async with httpx.AsyncClient() as http:
        rec_resp = await http.get(f"{ats_url}/receipts/{receipt_id}")
        assert rec_resp.status_code == 200
        rec = rec_resp.json()
        assert rec["found"] is True
        assert rec["full_name"] == "Marina Costa Duarte"
        assert rec["filename"].endswith(".pdf")
        assert rec["size_bytes"] > 1000
        with get_session_factory(cfg)() as db:
            packet = db.query(ApprovalRequest).filter_by(consumed=True).one()
            assert rec["pdf_sha256"] == packet.resume_sha256
        assert rec["consent_privacy"] == "sim"
        assert rec["work_authorization"] == "sim"

    evidence_dir = cfg.artifacts_path / "evidence"
    screenshots = list(evidence_dir.glob("*.png"))
    assert any(p.name.startswith("prep_") for p in screenshots)
    assert any(p.name.startswith("confirm_") for p in screenshots)

    resumes_dir = cfg.artifacts_path / "resumes"
    pdfs = list(resumes_dir.glob("*.pdf"))
    assert len(pdfs) >= 1 and pdfs[0].read_bytes().startswith(b"%PDF-")

    elapsed = time.perf_counter() - t0
    assert elapsed < 30.0


@pytest.mark.asyncio
async def test_e2e_multi_step_ats_form_with_answers_and_pdf_upload(
    live_e2e_servers: tuple[str, str, Settings],
) -> None:
    """Multi-step ATS flow: pending salary question -> user answers -> new packet -> approve -> Playwright multi-step submit + receipt."""
    app_url, ats_url, _cfg = live_e2e_servers
    async with httpx.AsyncClient(base_url=app_url, timeout=30.0) as client:
        headers = await _api_unlock(client, "demo-local-2026")
        disc = await client.post("/api/jobs/discover", json={"text": "analytics engineer", "limit": 60}, headers=headers)
        assert disc.status_code == 200

        jobs = (await client.get("/api/jobs?apply_mode=automated_allowed&limit=20", headers=headers)).json()["jobs"]
        ms_job = next(j for j in jobs if "/jobs/multi-step" in j["apply_url"])

        # Prepare initially without salary_expectation -> pending question flagged
        prep = (
            await client.post(
                "/api/applications/prepare",
                json={"job_id": ms_job["id"], "custom_answers": {}},
                headers=headers,
            )
        ).json()
        app_id = prep["application_id"]
        old_hash = prep["approval_request"]["packet_hash"]
        assert any(q["field_key"] == "salary_expectation" for q in prep["pending_questions"])

        # User answers salary & consent -> issues fresh packet hash
        ans_resp = await client.post(
            f"/api/applications/{app_id}/answer-questions",
            json={"answers": {"salary_expectation": "R$ 8.000 CLT", "consent_privacy": "sim"}},
            headers=headers,
        )
        assert ans_resp.status_code == 200
        assert ans_resp.json()["new_packet_hash"] != old_hash
        assert ans_resp.json()["status"] == "awaiting_approval"

        # Enable live mode + approve + execute real multi-step Playwright submission
        assert (
            await client.put(
                "/api/settings",
                json={"dry_run": False, "live_mode_enabled": True},
                headers=headers,
            )
        ).status_code == 200
        assert (await client.post(f"/api/applications/{app_id}/approve", headers=headers)).status_code == 200

        exec_resp = await client.post(
            f"/api/applications/{app_id}/execute", json={"submit": True}, headers=headers
        )
        assert exec_resp.status_code == 200
        exec_data = exec_resp.json()
        assert exec_data["status"] == "verified"
        assert exec_data["dry_run"] is False
        receipt_id = exec_data["confirmation_receipt_id"]
        assert receipt_id.startswith("REC-MS-")

    async with httpx.AsyncClient() as ats_http:
        rec = (await ats_http.get(f"{ats_url}/receipts/{receipt_id}")).json()
        assert rec["found"] is True
        assert rec["salary_expectation"] == "R$ 8.000 CLT"
        assert rec["filename"].endswith(".pdf")


@pytest.mark.asyncio
async def test_e2e_dry_run_reject_replay_captcha_and_manual_only_safety_gates(
    live_e2e_servers: tuple[str, str, Settings],
) -> None:
    """Verify DRY_RUN never submits, replay/reject fail closed, CAPTCHA halts, missing consent asks user, and manual_only confirms only manually."""
    app_url, _ats_url, _cfg = live_e2e_servers
    async with httpx.AsyncClient(base_url=app_url, timeout=30.0) as client:
        headers = await _api_unlock(client, "demo-local-2026")
        await client.post("/api/jobs/discover", json={"text": "data engineer python", "limit": 60}, headers=headers)
        jobs = (await client.get("/api/jobs?include_filtered=true&limit=60", headers=headers)).json()["jobs"]

        single_job = next(j for j in jobs if "/jobs/single-step" in j["apply_url"])
        captcha_job = next(j for j in jobs if "/jobs/captcha-gate" in j["apply_url"])
        manual_job = next(j for j in jobs if "/jobs/manual-demo" in j["apply_url"])

        # A. DRY_RUN default MUST NOT submit even if submit=True is requested
        prep_dry = (
            await client.post(
                "/api/applications/prepare",
                json={"job_id": single_job["id"], "custom_answers": {"consent_privacy": "sim"}},
                headers=headers,
            )
        ).json()
        app_dry = prep_dry["application_id"]
        assert (await client.post(f"/api/applications/{app_dry}/approve", headers=headers)).status_code == 200
        res_dry = (
            await client.post(f"/api/applications/{app_dry}/execute", json={"submit": True}, headers=headers)
        ).json()
        assert res_dry["dry_run"] is True
        assert res_dry["status"] == "ready_for_manual_review"
        assert res_dry["browser_result"]["confirmed_submission"] is False

        # B. 1-use replay: second execution MUST fail closed with 409
        replay_resp = await client.post(
            f"/api/applications/{app_dry}/execute", json={"submit": True}, headers=headers
        )
        assert replay_resp.status_code == 409

        # C. Reject workflow transitions cleanly and blocks execution
        prep_rej = (
            await client.post(
                "/api/applications/prepare",
                json={"job_id": single_job["id"], "custom_answers": {}},
                headers=headers,
            )
        ).json()
        rej_id = prep_rej["application_id"]
        rej_resp = await client.post(
            f"/api/applications/{rej_id}/reject",
            json={"comment": "Não quero seguir agora"},
            headers=headers,
        )
        assert rej_resp.status_code == 200 and rej_resp.json()["status"] == "rejected"
        assert (
            await client.post(f"/api/applications/{rej_id}/execute", json={"submit": False}, headers=headers)
        ).status_code == 409

        # Enable live mode for remaining safety-gate tests (CAPTCHA, missing consent, manual_only)
        assert (
            await client.put(
                "/api/settings",
                json={"dry_run": False, "live_mode_enabled": True},
                headers=headers,
            )
        ).status_code == 200

        # D. Missing privacy consent MUST return needs_user_input (never auto-checks)
        prep_nc = (
            await client.post(
                "/api/applications/prepare",
                json={"job_id": single_job["id"], "custom_answers": {}},
                headers=headers,
            )
        ).json()
        nc_id = prep_nc["application_id"]
        assert (await client.post(f"/api/applications/{nc_id}/approve", headers=headers)).status_code == 200
        res_nc = (
            await client.post(f"/api/applications/{nc_id}/execute", json={"submit": True}, headers=headers)
        ).json()
        assert res_nc["status"] == "needs_user_input"
        assert "consent_privacy" in res_nc["browser_result"]["missing_fields"]
        assert res_nc["browser_result"]["confirmed_submission"] is False

        # E. Simulated CAPTCHA blocker halts automation and returns blocked cleanly
        prep_cap = (
            await client.post(
                "/api/applications/prepare",
                json={"job_id": captcha_job["id"], "custom_answers": {"consent_privacy": "sim"}},
                headers=headers,
            )
        ).json()
        cap_id = prep_cap["application_id"]
        assert (await client.post(f"/api/applications/{cap_id}/approve", headers=headers)).status_code == 200
        res_cap = (
            await client.post(f"/api/applications/{cap_id}/execute", json={"submit": True}, headers=headers)
        ).json()
        assert res_cap["status"] == "blocked"
        assert res_cap["browser_result"]["status"] == "captcha_blocked"
        assert res_cap["browser_result"]["confirmed_submission"] is False

        # F. manual_only policy NEVER clicks submit even with live_mode_enabled + submit=True; verifies ONLY on explicit user receipt
        prep_man = (
            await client.post(
                "/api/applications/prepare",
                json={"job_id": manual_job["id"], "custom_answers": {"consent_privacy": "sim"}},
                headers=headers,
            )
        ).json()
        man_id = prep_man["application_id"]
        assert (await client.post(f"/api/applications/{man_id}/approve", headers=headers)).status_code == 200
        res_man = (
            await client.post(f"/api/applications/{man_id}/execute", json={"submit": True}, headers=headers)
        ).json()
        assert res_man["status"] == "ready_for_manual_review"
        assert res_man["dry_run"] is True
        assert res_man["browser_result"]["confirmed_submission"] is False

        manual_confirm = await client.post(
            f"/api/applications/{man_id}/mark-manual",
            json={"confirmation_reference": "PROT-MANUAL-2026-01", "notes": "Enviado manualmente pelo usuário."},
            headers=headers,
        )
        assert manual_confirm.status_code == 200
        assert manual_confirm.json()["status"] == "verified"
        assert manual_confirm.json()["confirmation_receipt_id"] == "PROT-MANUAL-2026-01"


@pytest.mark.parametrize("scenario", ["expired", "lost_confirmation"])
async def test_e2e_final_approval_gate_and_ambiguous_send(
    live_e2e_servers: tuple[str, str, Settings], monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    app_url, _ats_url, cfg = live_e2e_servers
    before = set(_RECEIPTS)
    async with httpx.AsyncClient(base_url=app_url, timeout=30) as client:
        headers = await _api_unlock(client, "demo-local-2026")
        assert (await client.post("/api/jobs/discover", json={"text": "Python", "limit": 60}, headers=headers)).status_code == 200
        jobs = (await client.get("/api/jobs?apply_mode=automated_allowed&limit=20", headers=headers)).json()["jobs"]
        job = next(j for j in jobs if "/jobs/single-step" in j["apply_url"])
        prep = await client.post("/api/applications/prepare", json={
            "job_id": job["id"], "custom_answers": {"consent_privacy": "sim"}
        }, headers=headers)
        assert prep.status_code == 200
        app_id = prep.json()["application_id"]
        assert (await client.put("/api/settings", json={"dry_run": False, "live_mode_enabled": True}, headers=headers)).status_code == 200
        assert (await client.post(f"/api/applications/{app_id}/approve", headers=headers)).status_code == 200

        perform_submit = BrowserDriver._perform_submit_and_verify

        async def simulate_boundary(driver, page, **kwargs):
            if scenario == "expired":
                with get_session_factory(cfg)() as db:
                    req = db.query(ApprovalRequest).filter_by(application_id=app_id).one()
                    req.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    db.commit()
            result = await perform_submit(driver, page, **kwargs)
            if scenario == "lost_confirmation":
                assert result.confirmed_submission
                raise TimeoutError("Confirmação indisponível")
            return result

        monkeypatch.setattr(BrowserDriver, "_perform_submit_and_verify", simulate_boundary)
        url = f"/api/applications/{app_id}/execute"
        first = await client.post(url, json={"submit": True}, headers=headers)
        assert first.status_code == 200
        assert first.json()["status"] == ("blocked" if scenario == "expired" else "submitted_unverified")
        assert (await client.post(url, json={"submit": True}, headers=headers)).status_code == 409
    assert len(set(_RECEIPTS) - before) == (0 if scenario == "expired" else 1)
