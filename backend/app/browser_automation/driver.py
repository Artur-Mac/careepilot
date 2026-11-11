"""Deterministic-first Playwright browser driver with domain allowlist, CAPTCHA pause, field verification, and separated submit."""

from __future__ import annotations

import glob
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, async_playwright

from backend.app.core.config import Settings
from backend.app.core.ssrf import UnsafeURLError, extract_origin, validate_external_url
from backend.app.schemas.contracts import BrowserAttemptResult
from backend.app.security.redaction import redact_secrets


def _resolve_chromium_executable(settings: Settings) -> str | None:
    """Find Playwright Chromium executable, falling back to cached ms-playwright revisions."""
    if settings.playwright_chromium_path and Path(settings.playwright_chromium_path).exists():
        return settings.playwright_chromium_path
    patterns = [
        str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
        str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]
    for pat in patterns:
        matches = sorted(glob.glob(pat), reverse=True)
        for m in matches:
            if Path(m).exists():
                return m
    return None


class BrowserDriver:
    """Controls Playwright deterministically, strictly separating prepare from submit."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _allowed_test_origins(self, extra_test_origins: set[str] | None = None) -> set[str]:
        origins: set[str] = set(extra_test_origins or set())
        if self.settings.test_ats_origin:
            origins.add(self.settings.test_ats_origin)
        return origins

    def _validate_resume_path(self, resume_file: Path) -> Path:
        resolved = resume_file.resolve()
        artifacts_root = self.settings.artifacts_path.resolve()
        if not resolved.is_relative_to(artifacts_root):
            raise ValueError(
                f"Arquivo de currículo fora do diretório permitido de artefatos: {resolved}"
            )
        if not resolved.exists() or resolved.suffix.lower() != ".pdf":
            raise ValueError(f"Currículo PDF inválido ou inexistente: {resolved}")
        return resolved

    def _validate_target(
        self,
        target_url: str,
        *,
        extra_test_origins: set[str] | None = None,
        allowed_domains: set[str] | None = None,
    ) -> str:
        allow_test = self._allowed_test_origins(extra_test_origins)
        # Never allow targeting CareerPilot's own control plane port!
        careerpilot_origins = {
            f"http://127.0.0.1:{self.settings.careerpilot_port}",
            f"http://localhost:{self.settings.careerpilot_port}",
        }
        if extract_origin(target_url) in careerpilot_origins:
            raise UnsafeURLError("BrowserDriver proibido de navegar para o painel/API do CareerPilot.")
        return validate_external_url(
            target_url,
            allow_test_origins=allow_test,
            allowed_domains=allowed_domains,
        )

    @staticmethod
    def _session_allowed_domains(safe_url: str, allow_origins: set[str]) -> set[str]:
        """Compute strict domain allowlist for a browser run (no external escape from test ATS)."""
        origin = extract_origin(safe_url).lower()
        if origin in {o.lower() for o in allow_origins}:
            # Loopback test ATS session may NEVER navigate to external domains!
            return set()
        host = (urlparse(safe_url).hostname or "").lower().rstrip(".")
        return {host} if host else set()

    async def prepare(
        self,
        approved_packet: dict[str, Any],
        resume_file: Path | str,
        target_url: str,
        dry_run: bool = True,
        *,
        submit_after_verify: bool = False,
        authorize_submit: Callable[[], None] | None = None,
        extra_test_origins: set[str] | None = None,
        allow_visual_fallback: bool = False,
    ) -> BrowserAttemptResult:
        """Open permitted ATS page, detect/fill fields, attach PDF, verify, and optionally submit if strictly authorized."""
        safe_url = self._validate_target(target_url, extra_test_origins=extra_test_origins)
        safe_pdf = self._validate_resume_path(Path(resume_file))
        allow_origins = self._allowed_test_origins(extra_test_origins)
        session_domains = self._session_allowed_domains(safe_url, allow_origins)

        answers: dict[str, Any] = dict(approved_packet.get("answers") or {})
        policy_mode = str(approved_packet.get("policy_mode") or "manual_only")
        application_id = str(approved_packet.get("application_id") or "app")

        evidence_dir = self.settings.artifacts_path / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = evidence_dir / f"prep_{application_id[:8]}.png"

        fields_detected: list[str] = []
        fields_filled: dict[str, str] = {}
        missing_fields: list[str] = []

        async with async_playwright() as p:
            exe = _resolve_chromium_executable(self.settings)
            launch_kwargs: dict[str, Any] = {"headless": self.settings.playwright_headless}
            if not self.settings.playwright_headless:
                launch_kwargs["slow_mo"] = 300
            if exe:
                launch_kwargs["executable_path"] = exe

            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(accept_downloads=False)
            page = await context.new_page()

            # Enforce strict domain allowlist even on redirects or sub-navigations
            blocked_nav: list[str] = []

            async def _route_guard(route: Any) -> None:
                req_url = route.request.url
                if route.request.is_navigation_request():
                    try:
                        self._validate_target(
                            req_url,
                            extra_test_origins=allow_origins,
                            allowed_domains=session_domains,
                        )
                    except UnsafeURLError as exc:
                        blocked_nav.append(f"{req_url}: {exc}")
                        await route.abort()
                        return
                await route.continue_()

            await page.route("**/*", _route_guard)

            try:
                try:
                    await page.goto(safe_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as exc:
                    if blocked_nav:
                        return BrowserAttemptResult(
                            status="domain_blocked",
                            final_url=page.url or safe_url,
                            confirmed_submission=False,
                            evidence={"blocked_navigation": blocked_nav, "error": str(exc)},
                        )
                    raise

                # Re-verify landing URL after redirects
                try:
                    self._validate_target(
                        page.url,
                        extra_test_origins=allow_origins,
                        allowed_domains=session_domains,
                    )
                except UnsafeURLError as exc:
                    return BrowserAttemptResult(
                        status="domain_blocked",
                        final_url=page.url,
                        confirmed_submission=False,
                        evidence={"error": str(exc)},
                    )

                # Wait briefly for any dynamic label script
                await page.wait_for_timeout(80)

                if await self._has_captcha_or_identity_gate(page):
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    return BrowserAttemptResult(
                        status="captcha_blocked",
                        final_url=page.url,
                        confirmed_submission=False,
                        evidence={
                            "reason": "CAPTCHA ou verificação de identidade detectada — intervenção humana obrigatória.",
                            "screenshot": str(screenshot_path),
                        },
                    )

                # Step 1 (or single-step) deterministic fill
                det, filled, miss = await self._fill_visible_fields(page, answers, safe_pdf)
                fields_detected.extend(det)
                fields_filled.update(filled)
                missing_fields.extend(miss)

                # Multi-step support: if there's a next-step button and step 1 had no missing required fields
                next_btn = page.locator("[data-testid='next-step'], button#next-step-btn")
                if await next_btn.count() > 0 and await next_btn.first.is_visible() and not miss:
                    await next_btn.first.click()
                    await page.wait_for_timeout(80)
                    det2, filled2, miss2 = await self._fill_visible_fields(page, answers, safe_pdf)
                    fields_detected.extend([d for d in det2 if d not in fields_detected])
                    fields_filled.update(filled2)
                    missing_fields.extend([m for m in miss2 if m not in missing_fields])

                await page.screenshot(path=str(screenshot_path), full_page=True)

                if missing_fields:
                    return BrowserAttemptResult(
                        status="needs_user_input",
                        fields_detected=fields_detected,
                        fields_filled=fields_filled,
                        missing_fields=missing_fields,
                        final_url=page.url,
                        confirmed_submission=False,
                        evidence={
                            "screenshot": str(screenshot_path),
                            "visual_fallback_used": False,
                            "reason": "Campos obrigatórios ou consentimento sem resposta verificada.",
                        },
                    )

                # NEVER submit if dry_run=True or policy_mode != automated_allowed or submit_after_verify=False
                if dry_run or not submit_after_verify or authorize_submit is None or policy_mode != "automated_allowed":
                    return BrowserAttemptResult(
                        status="ready_for_manual_review",
                        fields_detected=fields_detected,
                        fields_filled=fields_filled,
                        missing_fields=[],
                        final_url=page.url,
                        confirmed_submission=False,
                        evidence={
                            "screenshot": str(screenshot_path),
                            "dry_run": dry_run,
                            "policy_mode": policy_mode,
                            "visual_fallback_used": bool(allow_visual_fallback and self.settings.feature_visual_fallback),
                            "reason": "Formulário preenchido e verificado; aguardando envio manual ou modo live autorizado.",
                        },
                    )

                # Separated submit step — only reachable when dry_run=False, submit_after_verify=True, automated_allowed
                return await self._perform_submit_and_verify(
                    page,
                    application_id=application_id,
                    evidence_dir=evidence_dir,
                    fields_detected=fields_detected,
                    fields_filled=fields_filled,
                    allow_origins=allow_origins,
                    allowed_domains=session_domains,
                    authorize_submit=authorize_submit,
                )
            finally:
                await context.close()
                await browser.close()

    async def _has_captcha_or_identity_gate(self, page: Page) -> bool:
        if await page.locator("[data-captcha-blocker='true'], iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']").count() > 0:
            return True
        body_text = (await page.locator("body").inner_text()).lower()
        return any(
            phrase in body_text
            for phrase in (
                "verify you are human",
                "captcha simulado",
                "verificação humana obrigatória",
                "complete the captcha",
            )
        )

    async def _fill_visible_fields(
        self, page: Page, answers: dict[str, Any], pdf_path: Path
    ) -> tuple[list[str], dict[str, str], list[str]]:
        detected: list[str] = []
        filled: dict[str, str] = {}
        missing: list[str] = []

        # Text / email / tel / url / textarea controls
        text_controls = page.locator("input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([type='submit']), textarea, select")
        count = await text_controls.count()
        for idx in range(count):
            ctrl = text_controls.nth(idx)
            if not await ctrl.is_visible():
                continue
            name = (await ctrl.get_attribute("name")) or (await ctrl.get_attribute("id")) or f"field_{idx}"
            detected.append(name)
            is_req = (await ctrl.get_attribute("required")) is not None
            tag = await ctrl.evaluate("el => el.tagName.toLowerCase()")

            val = answers.get(name)
            if val is None and name == "seniority_level":
                val = answers.get("seniority_level") or "junior"
            if val is not None and str(val).strip():
                str_val = str(val).strip()
                if tag == "select":
                    await ctrl.select_option(str_val)
                    actual = await ctrl.input_value()
                else:
                    await ctrl.fill(str_val)
                    actual = await ctrl.input_value()
                if actual == str_val:
                    filled[name] = redact_secrets(actual, self.settings.secret_values())
            elif is_req:
                missing.append(name)

        # Radio groups
        radio_names = await page.evaluate(
            """() => [...new Set([...document.querySelectorAll('input[type="radio"]')].filter(e => e.offsetParent !== null).map(e => e.name).filter(Boolean))]"""
        )
        for rname in radio_names:
            detected.append(rname)
            ans_val = str(answers.get(rname) or "").strip().lower()
            if rname == "work_authorization" and ans_val:
                if any(tok in ans_val for tok in ("sim", "yes", "true", "autoriz", "cidad")):
                    ans_val = "sim"
                elif any(tok in ans_val for tok in ("nao", "não", "no", "false")):
                    ans_val = "nao"
            elif ans_val in {"sim", "yes", "true"}:
                ans_val = "sim"
            radio = page.locator(f"input[type='radio'][name='{rname}'][value='{ans_val}']")
            if ans_val and await radio.count() > 0:
                await radio.first.check()
                if await radio.first.is_checked():
                    filled[rname] = ans_val
            else:
                req = await page.locator(f"input[type='radio'][name='{rname}'][required]").count() > 0
                if req:
                    missing.append(rname)

        # Consent / checkboxes — NEVER auto-tick unknown consent without explicit confirmation!
        checkboxes = page.locator("input[type='checkbox']")
        cb_count = await checkboxes.count()
        for idx in range(cb_count):
            cb = checkboxes.nth(idx)
            if not await cb.is_visible():
                continue
            cname = (await cb.get_attribute("name")) or f"checkbox_{idx}"
            detected.append(cname)
            is_req = (await cb.get_attribute("required")) is not None
            explicit_val = str(answers.get(cname) or "").strip().lower()
            if explicit_val in {"sim", "yes", "true", "1", "agreed"}:
                await cb.check()
                if await cb.is_checked():
                    filled[cname] = "confirmed_by_user"
            elif is_req:
                missing.append(cname)

        # File upload (PDF)
        file_inputs = page.locator("input[type='file']")
        f_count = await file_inputs.count()
        for idx in range(f_count):
            fi = file_inputs.nth(idx)
            if not await fi.is_visible():
                continue
            fname = (await fi.get_attribute("name")) or f"file_{idx}"
            detected.append(fname)
            await fi.set_input_files(str(pdf_path))
            attached_name = await fi.evaluate("el => el.files && el.files[0] ? el.files[0].name : ''")
            if attached_name == pdf_path.name:
                filled[fname] = attached_name
            else:
                missing.append(fname)

        return detected, filled, missing

    async def _perform_submit_and_verify(
        self,
        page: Page,
        *,
        application_id: str,
        evidence_dir: Path,
        fields_detected: list[str],
        fields_filled: dict[str, str],
        allow_origins: set[str],
        authorize_submit: Callable[[], None],
        allowed_domains: set[str] | None = None,
    ) -> BrowserAttemptResult:
        submit_btn = page.locator("[data-testid='submit-application'], button[type='submit']")
        if await submit_btn.count() == 0:
            return BrowserAttemptResult(
                status="submitted_unverified",
                fields_detected=fields_detected,
                fields_filled=fields_filled,
                final_url=page.url,
                confirmed_submission=False,
                evidence={"reason": "Botão de envio não encontrado; nenhuma confirmação obtida."},
            )

        async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            authorize_submit()
            await submit_btn.first.click()

        self._validate_target(
            page.url,
            extra_test_origins=allow_origins,
            allowed_domains=allowed_domains,
        )
        confirm_shot = evidence_dir / f"confirm_{application_id[:8]}.png"
        await page.screenshot(path=str(confirm_shot), full_page=True)

        confirm_box = page.locator("[data-testid='submission-confirmation']")
        receipt_el = page.locator("[data-testid='receipt-id'], #receipt-id")
        body_text = await page.locator("body").inner_text()

        receipt_id = ""
        if await receipt_el.count() > 0:
            receipt_id = (await receipt_el.first.inner_text()).strip()
        elif match := re.search(r"\bREC-(?:MS-)?[A-F0-9]{8,}\b", body_text):
            receipt_id = match.group(0)

        confirmed = bool(await confirm_box.count() > 0 and receipt_id)
        return BrowserAttemptResult(
            status="confirmed" if confirmed else "submitted_unverified",
            fields_detected=fields_detected,
            fields_filled=fields_filled,
            missing_fields=[],
            final_url=page.url,
            confirmed_submission=confirmed,
            evidence={
                "receipt_id": receipt_id or None,
                "confirmation_snippet": redact_secrets(body_text[:400], self.settings.secret_values()),
                "screenshot": str(confirm_shot),
            },
        )
