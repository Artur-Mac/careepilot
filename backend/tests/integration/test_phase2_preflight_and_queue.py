"""Phase 2 integration, preflight portal probe, pending manual action queue CAS, and accessibility UI tests."""

from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright

from backend.app.browser_automation.driver import (
    FieldDescriptor,
    FieldOption,
    StepBlocker,
    inspect_step,
)
from backend.app.browser_automation.preflight import (
    PORTAL_REGISTRY,
    PortalScope,
    _invalidate_stuck_checking_rows,
    cancel_all_probe_tasks,
    count_fresh_preflight_issues,
    probe_session,
    start_portal_verification,
)
from backend.app.browser_automation.session import BrowserSession
from backend.app.core.config import Settings
from backend.app.core.llm import clear_engine_fallback, get_engine_telemetry, record_engine_fallback
from backend.app.core.security import issue_session_token
from backend.app.core.time import utc_rfc3339
from backend.app.db.migrations import prepare_and_upgrade_database
from backend.app.db.session import get_db, get_session_factory, reset_engine_cache
from backend.app.main import create_app
from backend.app.models.tables import (
    ArsenalRespostaRow,
    AuditEvent,
    JobPosting,
    MasterProfile,
    PendingManualActionRow,
    PortalSessionStatusRow,
    UserSettings,
)
from backend.app.schemas.contracts import (
    ApprovePendingActionRequest,
    SaveToArsenalInput,
    SkipPendingActionRequest,
)
from backend.app.schemas.profile import FieldApproval, SalaryApproval, SalaryBand
from backend.app.services.applications.salary import (
    PendingActionConflictError,
    PendingActionValidationError,
    SalaryQueue,
    _field_shape,
    suggest_salary_with_meta,
)
from backend.app.services.applications.service import ApplicationService
from backend.tests.conftest import _find_free_port, _LiveUvicornThread
from backend.tests.fixtures.sample_profile import build_sample_master_profile


def _make_field(
    *,
    field_id: str,
    label: str,
    control_kind: Any = "text",
    question_signature: str = "sig",
    category: str = "unknown_question",
    required: bool = True,
    options: tuple[FieldOption, ...] = (),
    locator_hint: dict[str, Any] | None = None,
) -> FieldDescriptor:
    return FieldDescriptor(
        field_id=field_id,
        question_signature=question_signature,
        label=label,
        control_kind=control_kind,
        required=required,
        editable=True,
        has_value=False,
        has_file=False,
        checked=None,
        accept="",
        options=options,
        category=category,
        locator_hint=locator_hint or {},
    )


def _resolve_local_chromium() -> str | None:
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
    return None


class _LocalFixtureServer:
    """Thread-safe local HTTP fixture server for Playwright portal/form tests."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        port = _find_free_port()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                body = pages.get(path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _LocalFixtureServer:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def _launch_cdp_chrome(chrome_bin: str, user_data_dir: Path) -> tuple[subprocess.Popen[bytes], str]:
    cdp_port = _find_free_port()
    proc = subprocess.Popen(
        [
            chrome_bin,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    import httpx

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{cdp_url}/json/version", timeout=1.0)
            if r.status_code == 200:
                return proc, cdp_url
        except Exception:
            time.sleep(0.15)
    proc.terminate()
    raise RuntimeError("Chromium CDP não respondeu a tempo no teste Phase 2.")


def test_engine_telemetry_modes_and_sanitized_fallback(tmp_path: Path) -> None:
    """Engine telemetry distinguishes offline, local_llm, cloud_assisted, and sanitized fallback reasons."""
    db_url = f"sqlite:///{tmp_path / 'eng.db'}"

    s_offline = Settings(database_url=db_url, llm_provider="offline")
    clear_engine_fallback(s_offline)
    assert get_engine_telemetry(s_offline) == {
        "mode": "offline_deterministic",
        "available": True,
        "fallback_reason": None,
    }

    s_local = Settings(
        database_url=db_url,
        llm_provider="qwen_local",
        llm_base_url="http://127.0.0.1:8080/v1",
    )
    clear_engine_fallback(s_local)
    t_local = get_engine_telemetry(s_local)
    assert t_local["mode"] == "local_llm"
    assert t_local["available"] is True

    s_cloud_blocked = Settings(
        database_url=db_url,
        llm_provider="openai_compat",
        llm_base_url="https://api.example.com/v1",
        llm_allow_remote_profile=False,
    )
    t_blocked = get_engine_telemetry(s_cloud_blocked)
    assert t_blocked["mode"] == "offline_deterministic"
    assert t_blocked["fallback_reason"] == "remote_profile_disabled"

    s_cloud_ok = Settings(
        database_url=db_url,
        llm_provider="openai_compat",
        llm_base_url="https://api.example.com/v1",
        llm_allow_remote_profile=True,
        llm_api_key="sk-secret-key",
    )
    clear_engine_fallback(s_cloud_ok)
    assert get_engine_telemetry(s_cloud_ok)["mode"] == "cloud_assisted"

    record_engine_fallback(s_local, "local_llm_unavailable")
    t_degraded = get_engine_telemetry(s_local)
    assert t_degraded["available"] is False
    assert t_degraded["fallback_reason"] == "local_llm_unavailable"
    clear_engine_fallback(s_local)


def test_salary_golden_rule_bands_floors_and_conflicts() -> None:
    """Salary suggestions respect context, floor constraints, conflicting bands, and explicit options."""
    job_clt_jr = JobPosting(
        id="job-1",
        canonical_url="https://example.org/jobs/1",
        dedup_signature="sig-1",
        source="gupy",
        source_job_id="1",
        company="Acme",
        title="Engenheiro de Dados Júnior",
        location="Recife, PE",
        workplace_type="remote",
        employment_type="CLT",
        seniority="junior",
        description="Vaga CLT para Engenheiro de Dados Júnior com Python e Spark.",
        description_hash="a" * 64,
        job_url="https://example.org/jobs/1",
        source_attribution="Gupy",
    )
    field_brl = _make_field(
        field_id="f-sal",
        label="Pretensão salarial mensal (R$)",
        control_kind="text",
        question_signature="sig-sal",
        category="salary",
        required=True,
    )
    assert _field_shape(field_brl) == "amount"

    today = date(2026, 10, 10)
    band_ok = SalaryBand(
        id="b1",
        role_archetype="data_engineer",
        seniority="junior",
        contract="CLT",
        currency="BRL",
        period="monthly",
        min_minor=450000,
        max_minor=600000,
        basis="Bruto mensal CLT",
        source_note="Pesquisa local",
        as_of=date(2026, 10, 1),
    )

    # No bands -> missing_band
    sugg_empty, meta_empty = suggest_salary_with_meta([], job_clt_jr, "BRL", "monthly", today)
    assert sugg_empty.reason_code == "missing_band"
    assert meta_empty["conflicting_bands"] is False

    # Profile floor within band raises suggested minimum without exceeding max
    sugg_floor, meta_floor = suggest_salary_with_meta(
        [band_ok],
        job_clt_jr,
        "BRL",
        "monthly",
        today,
        preferred_floor_minor=500000,
        preferred_currency="BRL",
        preferred_period="monthly",
    )
    assert sugg_floor.reason_code == "configured_band"
    assert sugg_floor.min_minor == 500000
    assert sugg_floor.max_minor == 600000
    assert meta_floor["floor_exceeds_band"] is False

    # Profile floor above band ceiling sets floor_exceeds_band warning without exceeding band
    sugg_high, meta_high = suggest_salary_with_meta(
        [band_ok],
        job_clt_jr,
        "BRL",
        "monthly",
        today,
        preferred_floor_minor=750000,
        preferred_currency="BRL",
        preferred_period="monthly",
    )
    assert meta_high["floor_exceeds_band"] is True
    assert sugg_high.min_minor == 450000

    # Conflicting bands for same tuple reject automatic suggestion
    band_conflict = SalaryBand(
        id="b2",
        role_archetype="data_engineer",
        seniority="junior",
        contract="CLT",
        currency="BRL",
        period="monthly",
        min_minor=300000,
        max_minor=400000,
        basis="Outra tabela",
        source_note="Conflito",
        as_of=date(2026, 10, 2),
    )
    sugg_conf, meta_conf = suggest_salary_with_meta(
        [band_ok, band_conflict],
        job_clt_jr,
        "BRL",
        "monthly",
        today,
    )
    assert sugg_conf.reason_code == "missing_band"
    assert meta_conf["conflicting_bands"] is True

    # Option field recognizes select shape and validate_and_seal rejects mismatched option approval
    field_opt_miss = _make_field(
        field_id="f-sal-opt",
        label="Faixa salarial (R$/mês)",
        control_kind="select",
        question_signature="sig-opt",
        category="salary",
        required=True,
        options=(FieldOption(option_id="opt_1", value="opt_1", label="R$ 15.000 - R$ 20.000", available=True),),
        locator_hint={"tag": "select"},
    )
    assert _field_shape(field_opt_miss) == "option"
    sal_queue = SalaryQueue(
        secret="sec-sal-test",
        user_id="local-applicant",
        application_id="app-sal",
        binding="bind-sal-test",
    )
    dummy_row = PendingManualActionRow(
        id="act-sal",
        user_id="local-applicant",
        application_id="app-sal",
        step_key="s1",
        field_key="f-sal-opt",
        field_fingerprint="a" * 64,
        kind="salary",
        status="pending",
        field_json={
            "label": field_opt_miss.label,
            "control": "select",
            "required": True,
            "options": [{"key": "opt_1", "label": "R$ 15.000 - R$ 20.000"}],
            "salary_currency": "BRL",
            "salary_period": "monthly",
            "salary_value_shape": "option",
        },
    )
    with pytest.raises(PendingActionValidationError, match="Opção inválida"):
        sal_queue.validate_and_seal(
            dummy_row,
            SalaryApproval(kind="salary", currency="BRL", period="monthly", shape="option", value="opt_nonexistent"),
            live_field=field_opt_miss,
        )


def test_preflight_api_status_verify_and_tenant_errors(tmp_path: Path) -> None:
    """Preflight endpoints return structured pt-BR errors, 202 verify status, and startup checking recovery."""
    reset_engine_cache()
    db_path = tmp_path / "pf_api.db"
    cfg = Settings(
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=str(tmp_path / "artifacts"),
        careerpilot_passcode="pw-123",
        careerpilot_session_secret="sec-123",
    )
    prepare_and_upgrade_database(cfg.database_url)
    app = create_app(cfg)
    sf = get_session_factory(cfg)

    def _override_db() -> Iterator[Any]:
        db = sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    with sf() as db:
        db.add(
            PortalSessionStatusRow(
                user_id="local-applicant",
                portal_key="linkedin",
                scope_key="default",
                status="checking",
                checked_at="2020-01-01T00:00:00Z",
                revision=1,
            )
        )
        db.add(
            PortalSessionStatusRow(
                user_id="local-applicant",
                portal_key="gupy",
                scope_key="default",
                status="login_required",
                browser_generation="1",
                checked_at=utc_rfc3339(),
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                revision=1,
            )
        )
        db.add(
            PortalSessionStatusRow(
                user_id="local-applicant",
                portal_key="workday",
                scope_key="default",
                status="challenge",
                browser_generation="1",
                checked_at=utc_rfc3339(),
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                revision=1,
            )
        )
        db.commit()

    # Registry coverage check
    assert {
        "linkedin",
        "gupy",
        "inhire",
        "greenhouse",
        "lever",
        "workday",
        "vagas",
        "infojobs",
        "google",
    }.issubset(PORTAL_REGISTRY.keys())

    with get_session_factory(cfg)() as db:
        login_cnt, blocked_cnt = count_fresh_preflight_issues(db, user_id="local-applicant")
        assert login_cnt == 1
        assert blocked_cnt == 1

    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        unlock = tc.post("/api/auth/unlock", json={"passcode": "pw-123"})
        assert unlock.status_code == 200
        tok, csrf_tok = issue_session_token(cfg, "local-applicant")
        hdrs = {"Authorization": f"Bearer {tok}", "X-CSRF-Token": csrf_tok}

        # Lifespan startup recovered stale checking row to unknown
        sessions = tc.get("/api/preflight/sessions", headers=hdrs)
        assert sessions.status_code == 200
        by_portal = {(x["portal_key"], x["scope_key"]): x for x in sessions.json()["items"]}
        assert by_portal[("linkedin", "default")]["status"] == "disconnected"

        # System status reports no-store, fresh counts, browser & engine telemetry
        sys_st = tc.get("/api/system/status", headers=hdrs)
        assert sys_st.status_code == 200
        assert sys_st.headers["cache-control"] == "no-store"
        body = sys_st.json()
        assert body["browser"]["state"] == "disconnected"
        assert body["engine"]["mode"] == "offline_deterministic"
        assert body["preflight"] == {"login_required": 1, "blocked_portals": 1, "pending_actions": 0}

        # Invalid status filter on pending-actions -> 422
        bad_q = tc.get("/api/preflight/pending-actions?status=invalid_stat", headers=hdrs)
        assert bad_q.status_code == 422
        assert bad_q.json()["detail"]["code"] == "invalid_status"

        # Unknown portal verify -> 422
        bad_verify = tc.post(
            "/api/preflight/sessions/verify",
            json={"targets": [{"portal_key": "unknown_site"}]},
            headers=hdrs,
        )
        assert bad_verify.status_code == 422
        assert bad_verify.json()["detail"]["code"] == "unknown_portal"

        # Targeted verify -> 202
        ok_verify = tc.post(
            "/api/preflight/sessions/verify",
            json={"targets": [{"portal_key": "gupy"}]},
            headers=hdrs,
        )
        assert ok_verify.status_code == 202
        assert ok_verify.json()["items"][0]["portal_key"] == "gupy"

        # Unknown portal open-login -> 404
        miss_login = tc.post("/api/preflight/sessions/nope/open-login", headers=hdrs)
        assert miss_login.status_code == 404
        assert miss_login.json()["detail"]["code"] == "unknown_portal"

        # Tenant-dependent portal without saved tenant -> 422 tenant_required
        tenant_err = tc.post("/api/preflight/sessions/gupy/default/open-login", headers=hdrs)
        assert tenant_err.status_code == 422
        assert tenant_err.json()["detail"]["code"] == "tenant_required"


@pytest.mark.asyncio
async def test_preflight_live_probes_with_chromium_cdp(tmp_path: Path) -> None:
    """probe_session inspects open tabs read-only across authenticated, login_required, challenge, public, and generation changes."""
    chrome_bin = _resolve_local_chromium()
    if chrome_bin is None:
        pytest.skip("Chromium não disponível.")

    pages_html = {
        "/jobs/123": "<html><body><h1>Engenheiro de Dados</h1><input type='search' placeholder='Cargo'/></body></html>",
        "/login": "<html><body><h1>Sign in</h1><form><input type='password' name='pwd'/></form></body></html>",
        "/dashboard": "<html><body><nav><a href='/logout'>Sair</a> Minha conta</nav></body></html>",
        "/challenge": "<html><body><h1>Verify you are human</h1><div class='cf-turnstile'>Cloudflare Challenge</div></body></html>",
    }
    with _LocalFixtureServer(pages_html) as srv:
        user_dir = tmp_path / "cdp_pf_user"
        user_dir.mkdir(parents=True, exist_ok=True)
        proc, cdp_url = _launch_cdp_chrome(chrome_bin, user_dir)
        cfg = Settings(
            database_url=f"sqlite:///{tmp_path / 'pf_cdp.db'}",
            artifacts_dir=str(tmp_path / "arts"),
            browser_cdp_url=cdp_url,
        )
        prepare_and_upgrade_database(cfg.database_url)
        session = BrowserSession(cfg, allow_test_origins={srv.origin})
        try:
            ctx = await session.ensure_connected(timeout_ms=4000)
            p_login = await ctx.new_page()
            await p_login.goto(f"{srv.origin}/login", wait_until="domcontentloaded")
            p_auth = await ctx.new_page()
            await p_auth.goto(f"{srv.origin}/dashboard", wait_until="domcontentloaded")
            p_chal = await ctx.new_page()
            await p_chal.goto(f"{srv.origin}/challenge", wait_until="domcontentloaded")
            p_pub = await ctx.new_page()
            await p_pub.goto(f"{srv.origin}/jobs/123", wait_until="domcontentloaded")

            scope_gupy = PortalScope(portal_key="gupy", scope_key="tenant.local", entry_url=srv.origin)
            res_login = await probe_session(scope_gupy, session, page=p_login)
            assert res_login["status"] == "login_required"
            assert res_login["expires_at"] > res_login["checked_at"]

            res_auth = await probe_session(scope_gupy, session, page=p_auth)
            assert res_auth["status"] == "authenticated"

            res_chal = await probe_session(scope_gupy, session, page=p_chal)
            assert res_chal["status"] == "challenge"

            res_pub = await probe_session(
                PortalScope(portal_key="gupy", scope_key="tenant.local", entry_url=f"{srv.origin}/jobs/123"),
                session,
                page=p_pub,
            )
            assert res_pub["status"] == "public_access"

            # Reconnect with force=True bumps browser_generation and invalidates bindings
            old_gen = session.browser_generation
            reconn = await session.reconnect(force=True)
            assert reconn["generation"] > old_gen

            # Async verify via DB spawns tasks and persists cleanly
            sf = get_session_factory(cfg)
            with sf() as db:
                started = await start_portal_verification(
                    db,
                    sf,
                    user_id="local-applicant",
                    browser=session,
                    targets=[{"portal_key": "vagas", "scope_key": "default"}],
                )
                assert started[0]["status"] == "checking"

            await asyncio.sleep(0.5)
            await cancel_all_probe_tasks()
            with sf() as db:
                _invalidate_stuck_checking_rows(db, user_id="local-applicant")
                row = db.get(PortalSessionStatusRow, ("local-applicant", "vagas", "default"))
                assert row is not None
                assert row.status in {"public_access", "authenticated", "unknown"}
        finally:
            await cancel_all_probe_tasks()
            await session.shutdown()
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_pending_queue_cas_sealing_arsenal_skip_and_single_use(tmp_path: Path) -> None:
    """Verify full PendingManualAction lifecycle: CAS conflicts, Fernet sealing, Arsenal draft gating, skip rules, and single-use reload."""
    chrome_bin = _resolve_local_chromium()
    if chrome_bin is None:
        pytest.skip("Chromium não disponível.")

    reset_engine_cache()
    form_html = """<!doctype html><html lang="pt-BR"><body>
    <main>
      <h1>Engenheira de Dados CLT Pleno - Empresa Recife</h1>
      <form id="app-form">
        <label for="cpf">CPF do candidato</label>
        <input id="cpf" name="cpf" type="text" required />
        <label for="sal">Pretensão salarial mensal (R$)</label>
        <input id="sal" name="sal" type="text" required />
        <label for="why">Por que você quer trabalhar nesta equipe?</label>
        <textarea id="why" name="why"></textarea>
        <label for="turno">Preferência de turno opcional</label>
        <select id="turno" name="turno">
          <option value="">Selecione</option>
          <option value="o1">Manhã</option>
          <option value="o2">Flexível</option>
        </select>
      </form>
    </main>
    </body></html>"""

    with _LocalFixtureServer({"/job-q1": form_html}) as srv:
        user_dir = tmp_path / "cdp_q_user"
        user_dir.mkdir(parents=True, exist_ok=True)
        proc, cdp_url = _launch_cdp_chrome(chrome_bin, user_dir)
        cfg = Settings(
            database_url=f"sqlite:///{tmp_path / 'q.db'}",
            artifacts_dir=str(tmp_path / "arts"),
            careerpilot_passcode="pw-123",
            careerpilot_session_secret="queue-test-secret",
            browser_cdp_url=cdp_url,
        )
        prepare_and_upgrade_database(cfg.database_url)
        sf = get_session_factory(cfg)
        svc = ApplicationService(cfg)
        doc = build_sample_master_profile()

        with sf() as db:
            db.add(UserSettings(user_id="local-applicant", salary_policy_json=[]))
            db.add(
                MasterProfile(
                    user_id="local-applicant",
                    revision=1,
                    document=doc.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
            db.add(
                JobPosting(
                    id="job-q1",
                    canonical_url=f"{srv.origin}/job-q1",
                    dedup_signature="sig-q1",
                    source="generic",
                    source_job_id="q1",
                    company="Empresa Recife",
                    title="Engenheira de Dados CLT Pleno",
                    location="Recife, PE",
                    workplace_type="remote",
                    employment_type="CLT",
                    seniority="pleno",
                    description="Vaga CLT para Engenheira de Dados com Python e SQL em Recife.",
                    description_hash="b" * 64,
                    job_url=f"{srv.origin}/job-q1",
                    apply_url=f"{srv.origin}/job-q1",
                    source_attribution="Fixture",
                )
            )
            db.commit()

        browser = BrowserSession(cfg, allow_test_origins={srv.origin})
        try:
            prep = await svc.prepare_application(job_id="job-q1", user_id="local-applicant")
            app_id = str(prep["application"]["id"])
            await svc.open_application(application_id=app_id, browser_session=browser, user_id="local-applicant")
            fill_res = await svc.fill_current_page(
                application_id=app_id, browser_session=browser, user_id="local-applicant"
            )
            actions = list(fill_res["application"]["last_step"]["pending_actions"])
            by_label = {str(a["field"]["label"]): a for a in actions}
            cpf_act = next(v for k, v in by_label.items() if "CPF" in k)
            sal_act = next(v for k, v in by_label.items() if "Pretensão salarial" in k)
            why_act = next(v for k, v in by_label.items() if "Por que" in k)
            opt_act = next(v for k, v in by_label.items() if "turno" in k.lower())

            assert cpf_act["kind"] == "sensitive"
            assert sal_act["kind"] == "salary"
            assert opt_act["can_skip"] is True
            assert cpf_act["can_skip"] is False

            # Attempt save_to_arsenal on sensitive -> 422 rejected
            ars_meta = SaveToArsenalInput(category="custom", language="pt-BR")
            with pytest.raises(PendingActionValidationError, match="Arsenal"):
                await svc.approve_pending_action(
                    cpf_act["id"],
                    ApprovePendingActionRequest(
                        revision=cpf_act["revision"],
                        field_fingerprint=cpf_act["field_fingerprint"],
                        approval=FieldApproval(kind="text", value="12345678909"),
                        save_to_arsenal=ars_meta,
                    ),
                    user_id="local-applicant",
                    browser_session=browser,
                )

            # Approve CPF cleanly -> Fernet sealed ciphertext in DB, never plaintext
            cpf_res = await svc.approve_pending_action(
                cpf_act["id"],
                ApprovePendingActionRequest(
                    revision=cpf_act["revision"],
                    field_fingerprint=cpf_act["field_fingerprint"],
                    approval=FieldApproval(kind="text", value="12345678909"),
                ),
                user_id="local-applicant",
                browser_session=browser,
            )
            assert cpf_res["approved"] is True
            with sf() as db:
                cpf_row = db.get(PendingManualActionRow, cpf_act["id"])
                assert cpf_row is not None
                assert cpf_row.status == "approved"
                assert cpf_row.approval_sealed is not None
                assert b"12345678909" not in cpf_row.approval_sealed
                assert "12345678909" not in str(cpf_res)

            # Stale revision -> 409 conflict returning current without mutating
            with pytest.raises(PendingActionConflictError) as exc_conf:
                await svc.approve_pending_action(
                    cpf_act["id"],
                    ApprovePendingActionRequest(
                        revision=1,
                        field_fingerprint=cpf_act["field_fingerprint"],
                        approval=FieldApproval(kind="text", value="00000000000"),
                    ),
                    user_id="local-applicant",
                    browser_session=browser,
                )
            assert exc_conf.value.current is not None
            assert exc_conf.value.current["status"] == "approved"

            # Skip required salary field -> 422 rejected
            with pytest.raises(PendingActionValidationError):
                await svc.skip_pending_action(
                    sal_act["id"],
                    SkipPendingActionRequest(
                        revision=sal_act["revision"],
                        field_fingerprint=sal_act["field_fingerprint"],
                    ),
                    user_id="local-applicant",
                )

            # Skip optional field -> skipped
            opt_skip = await svc.skip_pending_action(
                opt_act["id"],
                SkipPendingActionRequest(
                    revision=opt_act["revision"],
                    field_fingerprint=opt_act["field_fingerprint"],
                ),
                user_id="local-applicant",
            )
            assert opt_skip["skipped"] is True

            # Approve optional prose field WITH Save to Arsenal -> atomic single-tier draft created in arsenal_respostas!
            why_text = "Admiro o foco técnico em pipelines distribuídos e escalabilidade."
            why_res = await svc.approve_pending_action(
                why_act["id"],
                ApprovePendingActionRequest(
                    revision=why_act["revision"],
                    field_fingerprint=why_act["field_fingerprint"],
                    approval=FieldApproval(kind="text", value=why_text),
                    save_to_arsenal=ars_meta,
                ),
                user_id="local-applicant",
                browser_session=browser,
            )
            assert why_res["arsenal_draft_id"] is not None
            with sf() as db:
                draft = db.get(ArsenalRespostaRow, ("local-applicant", why_res["arsenal_draft_id"], 1))
                assert draft is not None
                assert draft.status == "draft"
                assert draft.payload_json["length_variants"]["short"] == why_text
                assert draft.payload_json["length_variants"]["long"] is None

            # Re-fill applies approved fields in Chrome; why becomes applied & single-use
            fill_res2 = await svc.fill_current_page(
                application_id=app_id, browser_session=browser, user_id="local-applicant"
            )
            with sf() as db:
                applied_why = db.get(PendingManualActionRow, why_act["id"])
                assert applied_why is not None
                assert applied_why.status == "applied"
                assert applied_why.approval_sealed is None

            # Verify readback_mismatch reconciliation via SalaryQueue + single-use reload on real snapshot
            bound_page = browser.get_application_page(app_id)
            assert bound_page is not None
            await bound_page.reload(wait_until="domcontentloaded")
            snap = await inspect_step(
                bound_page,
                navigation_gen=browser.get_navigation_generation(bound_page),
                expected_job_title="Engenheira de Dados CLT Pleno",
                expected_company="Empresa Recife",
            )
            browser.store_step_snapshot(app_id, snap)
            assert snap is not None
            snap_blockers = [
                StepBlocker(
                    code="salary_approval_required",
                    field_id=sal_act["field_key"],
                    label=sal_act["field"]["label"],
                    message="Salário",
                ),
                StepBlocker(
                    code="sensitive_manual",
                    field_id=cpf_act["field_key"],
                    label=cpf_act["field"]["label"],
                    message="CPF",
                ),
                StepBlocker(
                    code="missing_answer",
                    field_id=why_act["field_key"],
                    label=why_act["field"]["label"],
                    message="Motivo",
                    can_answer=True,
                ),
            ]
            with sf() as db:
                queue_recheck = SalaryQueue(
                    cfg.careerpilot_session_secret,
                    "local-applicant",
                    app_id,
                    browser.application_binding_key(app_id),
                    browser_generation=browser.browser_generation,
                )
                job = db.get(JobPosting, "job-q1")
                assert job is not None
                # Next synchronize sees applied row on still-blocked why field -> invalidates single-use approval and creates fresh pending row
                queue_recheck.synchronize(
                    db,
                    snap,
                    {sal_act["field_key"]},
                    set(),
                    job,
                    doc=doc,
                    all_blockers=snap_blockers,
                )
                db.commit()
                why_reloaded = next(
                    a for a in queue_recheck.last_pending_actions if a["field_key"] == why_act["field_key"]
                )
                assert why_reloaded["status"] == "pending"
                assert why_reloaded["id"] != why_act["id"]
                assert fill_res2 is not None

                # Explicitly approve newly queued why_reloaded, claim it, and test readback_mismatch audit & stale transition
                await svc.approve_pending_action(
                    why_reloaded["id"],
                    ApprovePendingActionRequest(
                        revision=why_reloaded["revision"],
                        field_fingerprint=why_reloaded["field_fingerprint"],
                        approval=FieldApproval(kind="text", value=why_text),
                    ),
                    user_id="local-applicant",
                    browser_session=browser,
                )
            with sf() as db:
                queue3 = SalaryQueue(
                    cfg.careerpilot_session_secret,
                    "local-applicant",
                    app_id,
                    browser.application_binding_key(app_id),
                    browser_generation=browser.browser_generation,
                )
                _, claimed_act = queue3.claim_pending(db, snap)
                assert claimed_act.get(why_act["field_key"]) == why_text
                job = db.get(JobPosting, "job-q1")
                assert job is not None
                queue3.synchronize(
                    db,
                    snap,
                    {sal_act["field_key"]},
                    set(),
                    job,
                    doc=doc,
                    all_blockers=snap_blockers,
                    write_outcomes={why_act["field_key"]: "readback_mismatch"},
                )
                db.commit()
                mismatch_ev = db.query(AuditEvent).filter_by(event_type="protected_write_failed").first()
                assert mismatch_ev is not None
                assert (mismatch_ev.details or {}).get("reason") == "readback_mismatch"
                old_row = db.get(PendingManualActionRow, why_reloaded["id"])
                assert old_row is not None
                assert old_row.status == "stale"
        finally:
            await browser.shutdown()
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_phase2_ui_dark_theme_320px_live_announcer_and_preflight_flows(tmp_path: Path) -> None:
    """Real Playwright browser checks dark-first persistence, 320px zero horizontal overflow, non-repeating live region, and Pré-voo UI."""
    chrome_bin = _resolve_local_chromium()
    if chrome_bin is None:
        pytest.skip("Chromium não disponível.")

    reset_engine_cache()
    port = _find_free_port()
    cfg = Settings(
        database_url=f"sqlite:///{tmp_path / 'ui2.db'}",
        artifacts_dir=str(tmp_path / "arts"),
        careerpilot_host="127.0.0.1",
        careerpilot_port=port,
        careerpilot_passcode="ui-pass-2",
        careerpilot_session_secret="ui-sec-2",
    )
    prepare_and_upgrade_database(cfg.database_url)
    sf = get_session_factory(cfg)

    with sf() as db:
        db.add(UserSettings(user_id="local-applicant", salary_policy_json=[]))
        db.add(
            MasterProfile(
                user_id="local-applicant",
                revision=1,
                document=build_sample_master_profile().model_dump(mode="json"),
                updated_at=datetime.now(UTC),
            )
        )
        db.commit()

    app = create_app(cfg)

    def _override_db() -> Iterator[Any]:
        db = sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db

    with _LiveUvicornThread(app, "127.0.0.1", port) as srv:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, executable_path=chrome_bin)
            context = await browser.new_context(viewport={"width": 320, "height": 800})
            page = await context.new_page()
            try:
                await page.goto(srv.url, wait_until="networkidle")
                # Dark-first initial paint
                assert await page.locator("html").evaluate("el => el.dataset.theme") == "dark"
                assert await page.locator("html").evaluate("el => getComputedStyle(el).colorScheme") == "dark"

                # Theme toggle persists via localStorage across reload
                await page.click("#btn-theme-toggle")
                assert await page.locator("html").evaluate("el => el.dataset.theme") == "light"
                await page.reload(wait_until="networkidle")
                assert await page.locator("html").evaluate("el => el.dataset.theme") == "light"
                await page.click("#btn-theme-toggle")
                assert await page.locator("html").evaluate("el => el.dataset.theme") == "dark"

                # Unlock & check non-repeating live region across consecutive identical status responses
                await page.fill("#passcode-input", "ui-pass-2")
                await page.click('[data-testid="btn-unlock"]')
                await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=10000)
                await page.wait_for_function(
                    "() => { const t = (document.querySelector('#status-live-announcer')?.textContent || '').toLowerCase(); return t.includes('opencode') && (t.includes('conectada') || t.includes('desconectado')); }"
                )
                first_ann = await page.locator("#status-live-announcer").inner_text()

                # Trigger reconnect click (returns identical disconnected state) -> announcer must not gain new nodes/duplicate spam
                await page.evaluate(
                    "() => { window.__annMutations = 0; new MutationObserver(() => { window.__annMutations++; }).observe(document.querySelector('#status-live-announcer'), { childList: true, characterData: true, subtree: true }); }"
                )
                await page.click("#btn-refresh-browser-status")
                await page.wait_for_timeout(400)
                assert await page.evaluate("() => window.__annMutations") == 0
                assert await page.locator("#status-live-announcer").inner_text() == first_ann

                # 320px responsive width check across Vagas, Pré-voo, Candidatura, Configurações
                for tid in ("tab-vagas", "tab-preflight", "tab-candidatura", "tab-configuracoes"):
                    await page.get_by_test_id(tid).click()
                    await page.wait_for_timeout(150)
                    overflow = await page.evaluate(
                        "() => document.documentElement.scrollWidth - window.innerWidth"
                    )
                    assert overflow <= 0, f"Horizontal overflow at 320px on {tid}: {overflow}px"

                # On Pré-voo tab check registry cards render disconnected status
                await page.get_by_test_id("tab-preflight").click()
                await page.wait_for_selector('#preflight-portals [data-portal-row="linkedin"]', timeout=5000)
                assert "Desconectado" in await page.locator('#preflight-portals [data-portal-row="linkedin"]').inner_text()
            finally:
                await context.close()
                await browser.close()
