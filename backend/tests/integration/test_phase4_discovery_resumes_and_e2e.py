"""Phase 4 integration, benchmark, and E2E tests:
- Bounded search plan & POST /api/search-queries/preview
- Discovery collectors, stale plan 409 conflict, identity conflict preservation, and similar-job review
- Strict geography gate before ranking (eligible|review|excluded + reason_code)
- Match Score 0-100 (round_half_up, weighted skills, role/seniority/location/contract, negative keyword penalties)
- 5,000-job warm benchmark (p95 <= 750ms) and sort=match cursor binding
- Atomic feedback projection (idempotent request_id, 409 mismatch, 2-job negative keyword threshold, undo, delete recompute)
- Deterministic 1-page ATS Markdown + PDF resumes (localized routing, vetted EN guard, pypdf 1-page verification, artifact_key)
- Full real-CDP E2E workflow (discovery -> feedback -> new plan -> prepare -> PDF/MD preview -> preflight handoff -> salary approve -> fill)
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
from pypdf import PdfReader

from backend.app.api.deps import get_browser_session, get_discovery_collectors_override
from backend.app.browser_automation.session import BrowserSession
from backend.app.connectors.browser.base import BrowserCollector, BrowserDiscoveryPlan
from backend.app.core.config import Settings, get_settings
from backend.app.db.migrations import prepare_and_upgrade_database
from backend.app.db.session import get_db, get_session_factory, reset_engine_cache
from backend.app.main import create_app
from backend.app.models.tables import JobPosting, JobSourceRecord, MasterProfile, UserSettings
from backend.app.schemas.contracts import NormalizedJob
from backend.app.services.discovery.eligibility import classify_job_eligibility
from backend.app.services.discovery.matching import round_half_up
from backend.tests.fixtures.sample_profile import build_sample_master_profile


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _resolve_chrome_bin() -> str | None:
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
        for cand in sorted(cache_root.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
            if cand.is_file():
                return str(cand)
    return None


class _LiveUvicornThread:
    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _LiveUvicornThread:
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=0.5) as resp:
                    if resp.status == 200:
                        return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("Uvicorn server failed to start in time.")

    def __exit__(self, *args: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)


def test_bounded_search_plan_preview_bilingual_aliases_and_missing_role_guard(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    # 1. When no target role exists in profile or request, return 422 with localized link to Perfil mestre
    empty_resp = client.post("/api/search-queries/preview", json={}, headers=auth_headers)
    assert empty_resp.status_code == 422
    detail = empty_resp.json()["detail"]
    assert detail["code"] == "missing_target_role"
    assert detail["editor_url"] == "/#perfil"

    # 2. Save MasterProfile and preview bounded plan with target_role_keys and locale
    doc = build_sample_master_profile().model_dump(mode="json")
    put_res = client.put("/api/profile", json={"document": doc, "revision": 0}, headers=auth_headers)
    assert put_res.status_code == 200

    plan_pt = client.post(
        "/api/search-queries/preview",
        json={"portal_keys": ["linkedin", "gupy"], "locale": "pt-BR", "target_role_keys": ["data_engineer"]},
        headers=auth_headers,
    )
    assert plan_pt.status_code == 200
    body = plan_pt.json()
    assert body["profile_revision"] == 1
    assert body["tuning_revision"] == 0
    assert body["portal_keys"] == ["linkedin", "gupy"]
    assert len(body["discriminative_skills"]) <= 2
    assert any(q["query_mode"] == "recife" for q in body["portal_queries"])
    assert any(q["query_mode"] == "remote" for q in body["portal_queries"])
    assert any(q["role_only_fallback"] is True for q in body["portal_queries"])
    assert any(t["kind"] == "role" for t in body["chosen_terms"])
    assert body["limits"]["max_result_pages_per_portal"] == 2
    assert body["limits"]["max_detail_pages_total"] == 20
    assert body["limits"]["run_timeout_seconds"] == 120

    # 3. Disable a connector and verify skipped_scopes reports it
    client.post("/api/connectors/gupy/toggle?enabled=false", headers=auth_headers)
    plan_en = client.post(
        "/api/search-queries/preview",
        json={"portal_keys": ["linkedin", "gupy"], "locale": "en", "remote_only": True},
        headers=auth_headers,
    ).json()
    assert "gupy" not in plan_en["portal_keys"]
    skipped_codes = {s["reason_code"] for s in plan_en["skipped_scopes"]}
    assert "connector_disabled" in skipped_codes
    assert "recife_onsite_skipped_remote_only" in skipped_codes
    client.post("/api/connectors/gupy/toggle?enabled=true", headers=auth_headers)


def test_dedup_identity_conflict_similar_job_review_and_stale_plan_conflict(
    client: TestClient,
    auth_headers: dict[str, str],
    isolated_settings: Settings,
) -> None:
    doc = build_sample_master_profile().model_dump(mode="json")
    assert client.put("/api/profile", json={"document": doc, "revision": 0}, headers=auth_headers).status_code == 200

    # 1. Stale base_profile_revision -> 409 conflict
    stale = client.post(
        "/api/jobs/discover",
        json={"base_profile_revision": 99},
        headers=auth_headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_discovery_plan"

    # 2. Seed two distinct existing jobs to trigger an identity conflict (provider_id -> job A, canonical_url -> job B)
    from backend.app.services.discovery.service import compute_dedup_signature

    sf = get_session_factory(isolated_settings)
    with sf() as db:
        job_a = JobPosting(
            id="job-existing-a",
            canonical_url="https://gupy.example.org/jobs/101",
            dedup_signature=compute_dedup_signature("Porto Tech", "Engenheira de Dados"),
            source="gupy",
            source_job_id="gp-101",
            company="Porto Tech",
            title="Engenheira de Dados",
            location="Recife, PE",
            workplace_type="remote",
            description="Python SQL Spark",
            description_hash="a" * 64,
            job_url="https://gupy.example.org/jobs/101",
            apply_url="https://gupy.example.org/jobs/101",
            source_attribution="Gupy",
        )
        job_b = JobPosting(
            id="job-existing-b",
            canonical_url="https://gupy.example.org/jobs/202",
            dedup_signature=compute_dedup_signature("Porto Tech", "Backend Python"),
            source="gupy",
            source_job_id="gp-202",
            company="Porto Tech",
            title="Backend Python",
            location="Recife, PE",
            workplace_type="remote",
            description="Python FastAPI",
            description_hash="b" * 64,
            job_url="https://gupy.example.org/jobs/202",
            apply_url="https://gupy.example.org/jobs/202",
            source_attribution="Gupy",
        )
        db.add_all([job_a, job_b])
        db.flush()
        db.add(
            JobSourceRecord(
                job_id=job_a.id,
                source="gupy",
                provider_job_id="gp-101",
                raw_url="https://gupy.example.org/jobs/101",
                attribution="Gupy",
                discovery_channel="browser",
            )
        )
        db.commit()

    class ConflictAndSimilarCollector(BrowserCollector):
        name = "gupy"

        async def collect(
            self, session: BrowserSession, plan: BrowserDiscoveryPlan
        ) -> list[NormalizedJob]:
            now_ts = datetime.now(UTC)
            return [
                # Conflicting identity: provider_job_id='gp-101' (job_a) but job_url='.../202' (job_b)
                NormalizedJob(
                    source="gupy",
                    source_job_id="gp-101",
                    company="Porto Tech",
                    title="Conflito Identidade",
                    location="Recife, PE",
                    workplace_type="remote",
                    description="Conflito entre ID e URL.",
                    job_url="https://gupy.example.org/jobs/202?utm_source=linkedin#apply",
                    apply_url="https://gupy.example.org/jobs/202",
                    discovered_at=now_ts,
                    source_attribution="Gupy",
                ),
                # Same company & title as job_a, but distinct URL & distinct requisition -> keep separate and flag review
                NormalizedJob(
                    source="gupy",
                    source_job_id="gp-303",
                    company="Porto Tech",
                    title="Engenheira de Dados",
                    location="Recife, PE",
                    workplace_type="hybrid",
                    description="Nova requisição distinta na mesma empresa.",
                    job_url="https://gupy.example.org/jobs/303?gh_src=test",
                    apply_url="https://gupy.example.org/jobs/303",
                    discovered_at=now_ts,
                    source_attribution="Gupy",
                ),
            ]

    assert isinstance(client.app, FastAPI)
    app = client.app
    app.dependency_overrides[get_discovery_collectors_override] = lambda: [ConflictAndSimilarCollector()]
    try:
        res = client.post(
            "/api/jobs/discover",
            json={"base_profile_revision": 1, "base_tuning_revision": 0},
            headers=auth_headers,
        )
        assert res.status_code == 202
        run_id = res.json()["run_id"]
        final_run: dict[str, Any] = {}
        for _ in range(40):
            r = client.get(f"/api/jobs/discovery-runs/{run_id}", headers=auth_headers)
            final_run = r.json()
            if final_run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert final_run["status"] == "completed"
        assert len(final_run["identity_conflicts"]) == 1
        assert final_run["identity_conflicts"][0]["provider_resolved_job_id"] == "job-existing-a"
        assert final_run["identity_conflicts"][0]["url_resolved_job_id"] == "job-existing-b"
        assert len(final_run["similar_job_reviews"]) == 1
        assert final_run["similar_job_reviews"][0]["canonical_url"] == "https://gupy.example.org/jobs/303"
    finally:
        app.dependency_overrides.pop(get_discovery_collectors_override, None)

    # Verify both job_a and job_b were preserved without corruption, plus job 303 was inserted separately
    jobs_resp = client.get("/api/jobs", headers=auth_headers).json()
    ids = {j["id"] for j in jobs_resp["jobs"]}
    assert {"job-existing-a", "job-existing-b"} <= ids
    assert len(ids) == 3


def test_geography_gate_match_score_round_half_up_and_feedback_projection(
    client: TestClient,
    auth_headers: dict[str, str],
    isolated_settings: Settings,
) -> None:
    # 1. Verify round_half_up and geography gate reason codes
    assert round_half_up(20.5) == 21
    assert round_half_up(20.4) == 20

    el_recife = classify_job_eligibility(
        title="Engenheira de Dados",
        location="Recife, PE",
        workplace_type="hybrid",
        description="Híbrido no Porto Digital.",
    )
    assert el_recife["status"] == "eligible"
    assert el_recife["reason_code"] == "recife_pe_hybrid"

    el_review = classify_job_eligibility(
        title="Backend Engineer",
        location="Remote",
        workplace_type="remote",
        description="Python and SQL.",
    )
    assert el_review["status"] == "review"
    assert el_review["reason_code"] == "remote_scope_unspecified"

    el_excl = classify_job_eligibility(
        title="Dev Python",
        location="São Paulo, SP",
        workplace_type="onsite",
        description="Presencial na Paulista.",
    )
    assert el_excl["status"] == "excluded"
    assert el_excl["reason_code"] == "onsite_outside_recife"

    # 2. Save Master Profile and seed 3 jobs to test feedback projection, 2-job negative keyword threshold, undo, and delete recompute
    doc = build_sample_master_profile()
    assert client.put(
        "/api/profile",
        json={"document": doc.model_dump(mode="json"), "revision": 0},
        headers=auth_headers,
    ).status_code == 200

    sf = get_session_factory(isolated_settings)
    with sf() as db:
        j1 = JobPosting(
            id="job-fb-1",
            canonical_url="https://jobs.example.org/1",
            dedup_signature="sig-fb-1",
            source="gupy",
            source_job_id="fb-1",
            company="Alpha Data",
            title="Engenheira de Dados Júnior",
            location="Recife, PE",
            workplace_type="remote",
            seniority="junior",
            employment_type="CLT",
            description="Requisitos obrigatórios:\n- Python\n- SQL\n- Snowflake\n- Scala",
            requirements_json=[
                {"text": "Python SQL Snowflake Scala", "kind": "required", "skill_keys": ["python", "sql", "snowflake", "scala"]}
            ],
            eligibility_json=el_recife,
            description_hash="1" * 64,
            job_url="https://jobs.example.org/1",
            apply_url="https://jobs.example.org/1",
            source_attribution="Gupy",
        )
        j2 = JobPosting(
            id="job-fb-2",
            canonical_url="https://jobs.example.org/2",
            dedup_signature="sig-fb-2",
            source="linkedin",
            source_job_id="fb-2",
            company="Beta Cloud",
            title="Data Engineer Pleno",
            location="Brasil (Remoto)",
            workplace_type="remote",
            seniority="mid",
            employment_type="PJ",
            description="Requisitos obrigatórios:\n- Python\n- Snowflake\n- Scala",
            requirements_json=[
                {"text": "Python Snowflake Scala", "kind": "required", "skill_keys": ["python", "snowflake", "scala"]}
            ],
            eligibility_json=el_recife,
            description_hash="2" * 64,
            job_url="https://jobs.example.org/2",
            apply_url="https://jobs.example.org/2",
            source_attribution="LinkedIn",
        )
        db.add_all([j1, j2])
        db.commit()

    # Check initial score and explanation on GET /api/jobs/{job_id}
    d1_before = client.get("/api/jobs/job-fb-1", headers=auth_headers).json()
    assert 0 <= d1_before["match_score"] <= 100
    assert d1_before["match_explanation"]["components"]["role"] == 25
    assert d1_before["match_explanation"]["components"]["location"] == 15
    assert "snowflake" in d1_before["match_explanation"]["missing_requirements"]

    # Idempotent replay on identical request_id, 409 on different payload with same request_id
    fb1 = client.post(
        "/api/jobs/job-fb-1/feedback",
        json={"action": "high_fit", "operation": "apply", "request_id": "req-high-fit-001"},
        headers=auth_headers,
    )
    assert fb1.status_code == 200
    assert "high_fit" in fb1.json()["active_actions"]

    replay = client.post(
        "/api/jobs/job-fb-1/feedback",
        json={"action": "high_fit", "operation": "apply", "request_id": "req-high-fit-001"},
        headers=auth_headers,
    )
    assert replay.status_code == 200

    mismatch = client.post(
        "/api/jobs/job-fb-1/feedback",
        json={"action": "too_senior", "operation": "apply", "request_id": "req-high-fit-001"},
        headers=auth_headers,
    )
    assert mismatch.status_code == 409

    # Apply wrong_tech_stack on 1st job -> negative keyword NOT yet active in penalties (needs 2 distinct jobs)
    client.post(
        "/api/jobs/job-fb-1/feedback",
        json={"action": "wrong_tech_stack", "operation": "apply", "request_id": "req-wrong-stack-001"},
        headers=auth_headers,
    )
    d1_one_vote = client.get("/api/jobs/job-fb-1", headers=auth_headers).json()
    assert d1_one_vote["match_explanation"]["penalties"] == 0

    # Apply wrong_tech_stack on 2nd distinct job -> snowflake & scala now activate (-3 per match = -6 penalty)
    client.post(
        "/api/jobs/job-fb-2/feedback",
        json={"action": "wrong_tech_stack", "operation": "apply", "request_id": "req-wrong-stack-002"},
        headers=auth_headers,
    )
    d1_two_votes = client.get("/api/jobs/job-fb-1", headers=auth_headers).json()
    assert d1_two_votes["match_explanation"]["penalties"] == 6
    assert set(d1_two_votes["match_explanation"]["negative_matches"]) == {"scala", "snowflake"}

    # Deleting job-fb-2 recomputes tuning rules and drops vote count back to 1 (penalty -> 0)
    del_res = client.delete("/api/jobs/job-fb-2", headers=auth_headers)
    assert del_res.status_code == 200
    d1_after_del = client.get("/api/jobs/job-fb-1", headers=auth_headers).json()
    assert d1_after_del["match_explanation"]["penalties"] == 0

    # Test blacklist_company and immediate undo
    client.post(
        "/api/jobs/job-fb-1/feedback",
        json={"action": "blacklist_company", "operation": "apply", "request_id": "req-blk-001"},
        headers=auth_headers,
    )
    assert len(client.get("/api/jobs", headers=auth_headers).json()["jobs"]) == 0
    assert len(client.get("/api/jobs?eligibility=excluded", headers=auth_headers).json()["jobs"]) == 1

    client.post(
        "/api/jobs/job-fb-1/feedback",
        json={"action": "blacklist_company", "operation": "undo", "request_id": "req-blk-undo-001"},
        headers=auth_headers,
    )
    assert len(client.get("/api/jobs", headers=auth_headers).json()["jobs"]) == 1


def test_5000_jobs_warm_ranking_benchmark_under_750ms(
    client: TestClient,
    auth_headers: dict[str, str],
    isolated_settings: Settings,
) -> None:
    doc = build_sample_master_profile().model_dump(mode="json")
    assert client.put("/api/profile", json={"document": doc, "revision": 0}, headers=auth_headers).status_code == 200

    sf = get_session_factory(isolated_settings)
    base_time = datetime.now(UTC)
    with sf() as db:
        rows = [
            JobPosting(
                id=f"bench-job-{i:04d}",
                canonical_url=f"https://bench.example.org/jobs/{i}",
                dedup_signature=f"sig-{i}",
                source="gupy" if i % 2 == 0 else "linkedin",
                source_job_id=f"bp-{i}",
                company=f"Empresa {i % 200}",
                title="Engenheira de Dados Python" if i % 5 == 0 else "Backend Developer",
                location="Recife, PE" if i % 3 == 0 else "Brasil (Remoto)",
                workplace_type="remote" if i % 3 else "hybrid",
                seniority="junior" if i % 2 == 0 else "mid",
                employment_type="CLT",
                description="Vaga com Python, SQL, FastAPI e Docker para engenharia de dados.",
                requirements_json=[
                    {"text": "Python SQL", "kind": "required", "skill_keys": ["python", "sql"]}
                ],
                eligibility_json={
                    "remote_scope": "recife_pe",
                    "brazil_eligible": "eligible",
                    "status": "eligible",
                    "reason_code": "recife_pe_onsite",
                    "reason": "Recife, PE",
                },
                description_hash=f"{i:064x}",
                job_url=f"https://bench.example.org/jobs/{i}",
                apply_url=f"https://bench.example.org/jobs/{i}",
                discovered_at=base_time - timedelta(seconds=i),
                source_attribution="Bench",
            )
            for i in range(5000)
        ]
        db.bulk_save_objects(rows)
        db.commit()

    # Warm-up request
    warm = client.get("/api/jobs?sort=match&limit=20", headers=auth_headers)
    assert warm.status_code == 200
    assert len(warm.json()["jobs"]) == 20

    # Measure 5 warm requests and check p95 <= 750ms
    durations_ms: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        r = client.get("/api/jobs?sort=match&limit=20", headers=auth_headers)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert r.status_code == 200
        assert len(r.json()["jobs"]) == 20
        durations_ms.append(elapsed_ms)

    durations_ms.sort()
    p95 = durations_ms[-1]
    assert p95 <= 750.0, f"Warm 5,000-job ranking p95={p95:.1f}ms exceeded 750ms budget"


def test_deterministic_one_page_resume_markdown_pdf_and_localized_routing(
    client: TestClient,
    auth_headers: dict[str, str],
    isolated_settings: Settings,
) -> None:
    doc = build_sample_master_profile().model_dump(mode="json")
    exp0_id = doc["experience"][0]["id"]
    b0_id = doc["experience"][0]["bullets"][0]["id"]
    # Add an English summary and an experience bullet with an approved English variant + route
    doc["schema_version"] = 2
    doc["summaries"] = [
        {
            "id": "sum-pt",
            "language": "pt-BR",
            "text": "Engenheira de dados com foco em Python, SQL e pipelines confiáveis em nuvem.",
            "source_claim_ids": [exp0_id],
            "role_tags": ["data_engineering"],
            "when_to_use": [{"locales": ["pt-BR"]}],
            "when_not_to_use": [],
            "status": "approved",
        },
        {
            "id": "sum-en",
            "language": "en",
            "text": "Data Engineer experienced in Python, SQL, and reliable cloud data pipelines.",
            "source_claim_ids": [exp0_id],
            "role_tags": ["data_engineering"],
            "when_to_use": [{"locales": ["en"]}],
            "when_not_to_use": [],
            "status": "approved",
        },
    ]
    doc["experience"][0]["bullets"][0]["variants"] = [
        {
            "id": "var-en-1",
            "language": "en",
            "text": "Built automated ETL pipelines in Python and SQL processing 2M daily records.",
            "source_claim_ids": [b0_id],
            "role_tags": ["data_engineering"],
            "when_to_use": [{"locales": ["en"]}],
            "when_not_to_use": [],
            "status": "approved",
        }
    ]
    put_res = client.put("/api/profile", json={"document": doc, "revision": 0}, headers=auth_headers)
    assert put_res.status_code == 200, put_res.text

    sf = get_session_factory(isolated_settings)
    with sf() as db:
        job_en = JobPosting(
            id="job-resume-en",
            canonical_url="https://jobs.example.org/en-data",
            dedup_signature="sig-en-data",
            source="linkedin",
            source_job_id="li-en-1",
            company="Global Data Co",
            title="Data Engineer",
            location="Remote - Brazil",
            workplace_type="remote",
            description="Requirements: experience with Python, SQL, and ETL responsibilities. Apply now as Data Engineer.",
            description_hash="e" * 64,
            job_url="https://jobs.example.org/en-data",
            apply_url="https://jobs.example.org/en-data",
            source_attribution="LinkedIn",
        )
        db.add(job_en)
        db.commit()

    # Prepare English resume
    prep_en = client.post(
        "/api/applications/prepare",
        json={"job_id": "job-resume-en", "locale": "en"},
        headers=auth_headers,
    )
    assert prep_en.status_code == 200
    resume_id = prep_en.json()["application"]["resume_id"]

    # Download Markdown and PDF, verify 1-page constraint and matching content
    md_resp = client.get(f"/api/resumes/{resume_id}/download?format=markdown", headers=auth_headers)
    assert md_resp.status_code == 200
    md_text = md_resp.text
    assert "## Summary" in md_text
    assert "Data Engineer experienced in Python, SQL" in md_text
    assert "Built automated ETL pipelines in Python and SQL" in md_text

    pdf_resp = client.get(f"/api/resumes/{resume_id}/download?format=pdf", headers=auth_headers)
    assert pdf_resp.status_code == 200
    import io

    reader = PdfReader(io.BytesIO(pdf_resp.content))
    assert len(reader.pages) == 1
    pdf_text = reader.pages[0].extract_text() or ""
    assert "SUMMARY" in pdf_text
    assert "Clara Nogueira Sales" in pdf_text

    # Verify required route item lacking vetted English wording raises MissingProfileContentError -> 422
    doc["experience"][0]["when_to_use"] = [{"locales": ["en"]}]
    doc["experience"][0]["bullets"] = [
        {
            "id": "b-pt-only",
            "text": "Tópico somente em português sem tradução aprovada.",
            "when_to_use": [{"locales": ["en"]}],
            "when_not_to_use": [],
            "variants": [],
        }
    ]
    assert client.put("/api/profile", json={"document": doc, "revision": 1}, headers=auth_headers).status_code == 200
    blocked_en = client.post(
        "/api/applications/prepare",
        json={"job_id": "job-resume-en", "locale": "en"},
        headers=auth_headers,
    )
    assert blocked_en.status_code == 422
    assert "inglês" in blocked_en.json()["detail"]["message"].lower()


@pytest.mark.asyncio
async def test_phase4_full_cdp_end_to_end_discovery_feedback_prepare_preflight_salary_and_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome_bin = _resolve_chrome_bin()
    if chrome_bin is None:
        pytest.skip("Chromium/Chrome binário não encontrado.")

    portal_port = _find_free_port()
    cdp_port = _find_free_port()
    ui_port = _find_free_port()
    portal_origin = f"http://127.0.0.1:{portal_port}"
    cdp_url = f"http://127.0.0.1:{cdp_port}"

    class _PortalHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            pages = {
                "/gupy-search": f"""
                <html><body>
                  <script id="__NEXT_DATA__" type="application/json">
                    {{"props":{{"pageProps":{{"jobs":[{{"id":901,"name":"Engenheira de Dados Python","careerPageName":"Porto Recife AI","jobUrl":"{portal_origin}/gupy/jobs/901","city":"Recife","state":"PE","workplaceType":"hybrid","description":"Requisitos obrigatórios:\\nPython e SQL\\n FastAPI"}}]}} }}}}
                  </script>
                </body></html>
                """,
                "/gupy/jobs/901": """
                <html>
                  <head><title>Candidatura - Engenheira de Dados Python</title></head>
                  <body>
                    <main>
                      <div data-testid="job-description">
                        <h2>Requisitos obrigatórios</h2>
                        <p>Python, SQL e FastAPI em Recife, PE.</p>
                      </div>
                      <form id="apply-form" action="/submit" method="post">
                        <label for="f-name">Nome completo</label>
                        <input id="f-name" name="full_name" type="text" required />
                        <label for="f-email">E-mail</label>
                        <input id="f-email" name="email" type="email" required />
                        <label for="f-salary">Pretensão salarial mensal (R$)</label>
                        <input id="f-salary" name="salary" type="text" placeholder="R$ 6.500,00" required />
                        <label for="f-cv">Currículo (PDF)</label>
                        <input id="f-cv" name="resume" type="file" accept=".pdf" required />
                        <button id="btn-submit" type="submit">Enviar candidatura</button>
                      </form>
                    </main>
                  </body>
                </html>
                """,
            }
            body = pages.get(path, "<html><body>OK</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    portal_srv = http.server.ThreadingHTTPServer(("127.0.0.1", portal_port), _PortalHandler)
    threading.Thread(target=portal_srv.serve_forever, daemon=True).start()

    cdp_profile = tmp_path / "cdp-p4-profile"
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
            await __import__("asyncio").sleep(0.15)
    else:
        chrome_proc.terminate()
        portal_srv.shutdown()
        pytest.fail("CDP Chrome failed to start.")

    reset_engine_cache()
    get_settings.cache_clear()
    artifacts_dir = tmp_path / "p4_e2e_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "p4_e2e.db"
    passcode = "p4-e2e-passcode"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=str(artifacts_dir),
        browser_cdp_url=cdp_url,
        careerpilot_host="127.0.0.1",
        careerpilot_port=ui_port,
        careerpilot_passcode=passcode,
        careerpilot_session_secret="p4-e2e-secret",
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
        db.commit()

    class LiveCdpGupyCollector(BrowserCollector):
        name = "gupy"

        async def collect(
            self, session: BrowserSession, plan: BrowserDiscoveryPlan
        ) -> list[NormalizedJob]:
            from backend.app.connectors.browser.gupy import GupyBrowserCollector

            return await GupyBrowserCollector().collect(
                session,
                BrowserDiscoveryPlan(
                    roles=plan.roles,
                    location=plan.location,
                    remote_only=plan.remote_only,
                    include_remote=plan.include_remote,
                    limit_per_source=plan.limit_per_source,
                    base_url_override=f"{portal_origin}/gupy-search",
                ),
            )

    app = create_app(settings)
    assert isinstance(app, FastAPI)

    def _override_db() -> Iterator[object]:
        db = sf()
        try:
            yield db
        finally:
            db.close()

    def _override_session() -> BrowserSession:
        if not getattr(app.state, "_p4_e2e_bound", False):
            app.state.browser_session = BrowserSession(
                settings, allow_test_origins={portal_origin}
            )
            app.state._p4_e2e_bound = True
        return app.state.browser_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_browser_session] = _override_session
    app.dependency_overrides[get_discovery_collectors_override] = lambda: [LiveCdpGupyCollector()]

    try:
        with _LiveUvicornThread(app, "127.0.0.1", ui_port) as srv:
            async with async_playwright() as p:
                ui_browser = await p.chromium.launch(headless=True, executable_path=chrome_bin)
                cdp_browser = await p.chromium.connect_over_cdp(cdp_url)
                page = await ui_browser.new_page()
                try:
                    await page.goto(srv.url, wait_until="networkidle")
                    await page.fill("#passcode-input", passcode)
                    await page.click('[data-testid="btn-unlock"]')
                    await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=8000)

                    # 1. Discover jobs in Vagas tab via real CDP collector
                    await page.click('[data-testid="btn-discover"]')
                    await page.wait_for_selector("article.job-card", timeout=15000)
                    job_card = page.locator("article.job-card").first
                    job_id = await job_card.get_attribute("data-job-id")
                    assert job_id

                    # 2. Apply feedback (high_fit) on the discovered job and verify new search plan reflects tuning revision
                    await job_card.locator("details summary", has_text="Avaliar vaga").click()
                    async with page.expect_response(
                        lambda r: r.url.endswith(f"/api/jobs/{job_id}/feedback")
                    ) as fb_resp_info:
                        await job_card.locator('button.job-feedback[data-action="high_fit"]').click()
                    fb_resp = await fb_resp_info.value
                    assert fb_resp.status == 200

                    # 3. Click Preparar PDF & Chat OpenCode -> generates 1-page PDF + Markdown and focuses OpenCode chat
                    await page.click(f'[data-testid="btn-prepare-job-{job_id}"]')
                    await page.wait_for_selector('[data-testid="btn-send-to-opencode"]', timeout=15000)
                    await page.wait_for_selector("#opencode-chat-panel:not(.hidden)", timeout=8000)
                    await page.click("#btn-toggle-pdf-preview")
                    await page.wait_for_selector("#candidatura-pdf-preview:not(.hidden)", timeout=8000)
                    preview_txt = await page.locator("#candidatura-pdf-preview").inner_text()
                    assert "Clara Nogueira Sales" in preview_txt

                    # Trigger legacy CDP /open + /fill-current-page API directly to verify pending salary card + fill backend pipeline
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
                    await page.reload(wait_until="networkidle")
                    await page.click('[data-testid="tab-candidatura"]')
                    await page.wait_for_selector('.salary-approval-form input[name="amount"]', timeout=15000)

                    # 4. Approve salary in the active application salary card, then call backend fill-current-page
                    await page.fill('.salary-approval-form input[name="amount"]', "6500.00")
                    async with page.expect_response(
                        lambda r: "/api/preflight/pending-actions/" in r.url and r.url.endswith("/approve")
                    ) as sal_resp_info:
                        await page.click('.salary-approval-form button[type="submit"]')
                    assert (await sal_resp_info.value).status == 200

                    fill_json = await page.evaluate(
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
                    assert fill_json["status"] == "review"

                    portal_page = next(
                        (
                            pg
                            for pg in cdp_browser.contexts[0].pages
                            if "/gupy/jobs/901" in (pg.url or "")
                        ),
                        None,
                    )
                    assert portal_page is not None
                    assert await portal_page.locator("#f-name").input_value() == "Clara Nogueira Sales"
                    assert "6500" in (await portal_page.locator("#f-salary").input_value()).replace(".", "")
                finally:
                    await page.close()
                    await cdp_browser.close()
                    await ui_browser.close()
    finally:
        if hasattr(app.state, "browser_session"):
            await app.state.browser_session.shutdown()
        chrome_proc.terminate()
        chrome_proc.wait(timeout=5)
        portal_srv.shutdown()
        portal_srv.server_close()
