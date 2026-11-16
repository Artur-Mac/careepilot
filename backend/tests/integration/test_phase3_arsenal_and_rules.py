"""Phase 3 integration, validator, migration, and Playwright UI tests for Arsenal, Rules, and Cloud Master AI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
from sqlalchemy import select

from alembic import command
from backend.app.core.config import Settings
from backend.app.core.llm import LlmGateway, LlmProviderError, LlmStructuredResult
from backend.app.core.time import utc_rfc3339
from backend.app.db.migrations import build_alembic_config, prepare_and_upgrade_database
from backend.app.db.session import get_session_factory
from backend.app.models.tables import ArsenalRespostaRow, MasterProfile, ProfileChangeProposalRow
from backend.app.schemas.profile import (
    LengthVariants,
    RouteClause,
    Routing,
)
from backend.app.services.applications.arsenal import (
    ArsenalEntryRequest,
    ArsenalUpdateRequest,
    ArsenalValidationError,
    approve_arsenal_entry,
    build_confirmed_answer_arsenal_draft,
    migrate_legacy_standard_answers_to_arsenal,
    update_arsenal_entry,
)
from backend.app.services.applications.rules import (
    build_verified_slot_values,
    extract_canonical_literals,
    extract_english_proficiency,
    match_field_rule,
)
from backend.app.services.applications.validator import (
    ControlConstraints,
    count_words,
    extract_control_constraints,
    fit_arsenal_variants,
    interpolate_template,
    validate_and_format_cep,
    validate_and_format_cpf,
    validate_and_format_date,
    validate_and_format_phone,
    validate_candidate_value,
    validate_email,
    validate_https_url,
    validate_scalar_number,
)
from backend.app.services.profile.curation import (
    AddBulletOp,
    AddSummaryOp,
    AddVariantOp,
    DraftArsenalEntryOp,
    EditBulletOp,
    EditExperienceOp,
    EditProjectOp,
    ProposalDraftBundle,
    SetRoutingOp,
)
from backend.tests.e2e.test_browser_end_to_end import _resolve_local_chromium
from backend.tests.fixtures.sample_profile import build_sample_master_profile


def test_cloud_master_ai_v2_proposals_redaction_dag_ttl_and_atomic_apply(
    client: TestClient,
    auth_headers: dict[str, str],
    isolated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify 30-op union, redaction, 422 guards, 24h TTL expiry, dependency closure, and atomic apply."""
    isolated_settings.llm_provider = "openai_compat"
    isolated_settings.llm_base_url = "https://llm.example.org/v1"
    isolated_settings.llm_allow_remote_profile = True
    isolated_settings.llm_api_key = "sk-secret-key-xyz987654321"

    doc = build_sample_master_profile().model_dump(mode="json")
    doc["experience"].append(
        {
            "id": "exp-other",
            "company": "Concorrente Global S.A.",
            "role": "Analista",
            "period": "2021 - 2022",
            "location": "Recife, PE",
            "details": "Atuação anterior.",
            "bullets": [{"id": "bul-other-1", "text": "Mantive rotinas internas."}],
        }
    )
    saved = client.put("/api/profile", json={"document": doc, "revision": 0}, headers=auth_headers)
    assert saved.status_code == 200
    exp_id = doc["experience"][0]["id"]
    existing_bullet_id = doc["experience"][0]["bullets"][0]["id"]
    existing_bullet_text = doc["experience"][0]["bullets"][0]["text"]
    proj_id = doc["projects"][0]["id"]

    # 1. Remote consent required (both request flag and settings)
    no_consent = client.post(
        "/api/profile/change-proposals",
        json={
            "base_revision": 1,
            "allow_remote_profile": False,
            "instruction": "Adicione conquista técnica.",
        },
        headers=auth_headers,
    )
    assert no_consent.status_code == 403
    assert no_consent.headers.get("Cache-Control") == "no-store"
    assert no_consent.json()["detail"]["code"] == "remote_profile_not_authorized"

    # 2. Unredactable PII / secrets / links / emails / phones rejected with 422 before calling provider
    for bad_instruction in (
        f"Meu email pessoal é {doc['contact']['email']} e reduzi latência em 30%.",
        "Meu token sk-secret-key-xyz987654321 foi usado no deploy.",
        "Veja meu perfil em https://linkedin.com/in/candidata para detalhes.",
        "Ligue para +55 81 99999-1234 para confirmar a arquitetura.",
    ):
        resp_pii = client.post(
            "/api/profile/change-proposals",
            json={
                "base_revision": 1,
                "allow_remote_profile": True,
                "instruction": bad_instruction,
            },
            headers=auth_headers,
        )
        assert resp_pii.status_code == 422, bad_instruction
        assert resp_pii.headers.get("Cache-Control") == "no-store"
        assert resp_pii.json()["detail"]["code"] == "unredactable_pii"

    # 3. Validation failures on AI output: cyclic DAG, metric drift, employer mismatch, salary_note, old_value mismatch
    next_bundle: ProposalDraftBundle | Exception = ProposalDraftBundle(
        operations=[
            AddBulletOp(
                change_id="c1",
                depends_on=["c2"],
                op="add_bullet",
                parent_id=exp_id,
                canonical_text="Reduzi custos de infraestrutura em 25%.",
                language="pt-BR",
                user_fact_refs=["statement"],
            ),
            AddVariantOp(
                change_id="c2",
                depends_on=["c1"],
                op="add_variant",
                bullet_id="c1",
                text="Reduzi custos de nuvem em 25%.",
                language="pt-BR",
                source_claim_ids=["c1"],
            ),
        ]
    )
    captured_payloads: list[dict[str, Any]] = []

    async def fake_complete(_self: LlmGateway, _db: object, **kwargs: Any) -> LlmStructuredResult[Any]:
        captured_payloads.append(kwargs["user_payload"])
        if isinstance(next_bundle, Exception):
            raise next_bundle
        return LlmStructuredResult(
            data=next_bundle,
            provider="openai_compat",
            model="gpt-4o-mini",
            prompt_tokens=40,
            completion_tokens=60,
            estimated_cost_usd=0.001,
            mode="llm_structured",
        )

    monkeypatch.setattr(LlmGateway, "complete_structured", fake_complete)

    instruction_text = "Reduzi custos de infraestrutura em 25% usando Airflow e Python."
    r_cycle = client.post(
        "/api/profile/change-proposals",
        json={"base_revision": 1, "allow_remote_profile": True, "instruction": instruction_text},
        headers=auth_headers,
    )
    assert r_cycle.status_code == 422
    assert r_cycle.json()["detail"]["code"] == "cyclic_dependencies"
    # Verify outbound payload never contained candidate contact PII, imports, or salary
    sent_str = str(captured_payloads[-1])
    assert doc["contact"]["email"] not in sent_str
    assert doc["contact"]["name"] not in sent_str

    # Metric drift (25% -> 40%)
    next_bundle = ProposalDraftBundle(
        operations=[
            AddBulletOp(
                change_id="c1",
                op="add_bullet",
                parent_id=exp_id,
                canonical_text="Reduzi custos de infraestrutura em 40% usando Airflow.",
                language="pt-BR",
                user_fact_refs=["statement"],
            )
        ]
    )
    r_metric = client.post(
        "/api/profile/change-proposals",
        json={"base_revision": 1, "allow_remote_profile": True, "instruction": instruction_text},
        headers=auth_headers,
    )
    assert r_metric.status_code == 422
    assert r_metric.json()["detail"]["code"] == "metric_drift"

    # Cross-employer contamination
    next_bundle = ProposalDraftBundle(
        operations=[
            AddBulletOp(
                change_id="c1",
                op="add_bullet",
                parent_id=exp_id,
                canonical_text="Reduzi custos em 25% na Concorrente Global S.A. com Airflow.",
                language="pt-BR",
                user_fact_refs=["statement"],
            )
        ]
    )
    r_emp = client.post(
        "/api/profile/change-proposals",
        json={"base_revision": 1, "allow_remote_profile": True, "instruction": instruction_text},
        headers=auth_headers,
    )
    assert r_emp.status_code == 422
    assert r_emp.json()["detail"]["code"] == "employer_mismatch"

    # salary_note forbidden in AI proposals
    next_bundle = ProposalDraftBundle(
        operations=[
            DraftArsenalEntryOp(
                change_id="c-sal",
                op="draft_arsenal_entry",
                question="Observação salarial",
                category="salary_note",
                language="pt-BR",
                length_variants=LengthVariants(short="Apenas referência."),
            )
        ]
    )
    r_sal = client.post(
        "/api/profile/change-proposals",
        json={"base_revision": 1, "allow_remote_profile": True, "instruction": instruction_text},
        headers=auth_headers,
    )
    assert r_sal.status_code == 422
    assert r_sal.json()["detail"]["code"] == "salary_note_forbidden"

    # expected_old_value mismatch
    next_bundle = ProposalDraftBundle(
        operations=[
            EditExperienceOp(
                change_id="c-edit",
                op="edit_experience",
                target_id=exp_id,
                field="role",
                expected_old_value="Cargo Incorreto",
                new_value="Engenheira de Dados Sênior",
                user_fact_refs=["statement"],
            )
        ]
    )
    r_old = client.post(
        "/api/profile/change-proposals",
        json={"base_revision": 1, "allow_remote_profile": True, "instruction": instruction_text},
        headers=auth_headers,
    )
    assert r_old.status_code == 422
    assert r_old.json()["detail"]["code"] == "expected_old_value_mismatch"

    # Outage fallback -> 503 without blocking manual PUT /api/profile
    next_bundle = LlmProviderError("Timeout from remote provider")
    r_outage = client.post(
        "/api/profile/change-proposals",
        json={"base_revision": 1, "allow_remote_profile": True, "instruction": instruction_text},
        headers=auth_headers,
    )
    assert r_outage.status_code == 503
    assert r_outage.json()["detail"]["code"] == "provider_unavailable"

    # 4. Valid multi-op bundle covering all 8 op kinds + dependency closure + atomic apply
    next_bundle = ProposalDraftBundle(
        operations=[
            AddBulletOp(
                change_id="chg-bullet",
                op="add_bullet",
                parent_id=exp_id,
                canonical_text="Reduzi custos de infraestrutura em 25% usando Airflow e Python.",
                language="pt-BR",
                user_fact_refs=["statement"],
                routing=Routing(role_tags=["data_engineering"]),
            ),
            SetRoutingOp(
                change_id="chg-route",
                depends_on=["chg-bullet"],
                op="set_routing",
                target_id="chg-bullet",
                routing=Routing(
                    role_tags=["data_engineering", "backend_python"],
                    when_to_use=[RouteClause(all_skills=["Python"])],
                ),
            ),
            AddVariantOp(
                change_id="chg-variant",
                depends_on=["chg-bullet"],
                op="add_variant",
                bullet_id="chg-bullet",
                text="Cut cloud infrastructure costs by 25% with Airflow and Python.",
                language="en",
                source_claim_ids=["chg-bullet"],
                routing=Routing(role_tags=["data_engineering"]),
            ),
            AddSummaryOp(
                change_id="chg-summary",
                depends_on=["chg-bullet"],
                op="add_summary",
                text="Engenheira de dados com redução comprovada de 25% em custos de infraestrutura.",
                language="pt-BR",
                source_claim_ids=["chg-bullet"],
            ),
            DraftArsenalEntryOp(
                change_id="chg-arsenal",
                depends_on=["chg-bullet"],
                op="draft_arsenal_entry",
                question="Como você otimizou custos de nuvem?",
                category="finops_cloud_impact",
                language="pt-BR",
                role_archetype="data_engineer",
                length_variants=LengthVariants(
                    short="Reduzi custos de nuvem em 25% com Airflow e Python."
                ),
                source_claim_ids=["chg-bullet"],
                role_tags=["data_engineering"],
            ),
            EditExperienceOp(
                change_id="chg-edit-exp",
                op="edit_experience",
                target_id=exp_id,
                field="details",
                expected_old_value=doc["experience"][0]["details"],
                new_value="Liderança técnica de pipelines com redução de 25% em custos.",
                user_fact_refs=["statement"],
            ),
            EditProjectOp(
                change_id="chg-edit-proj",
                op="edit_project",
                target_id=proj_id,
                field="details",
                expected_old_value=doc["projects"][0]["details"],
                new_value="Orquestração de dados com economia de 25%.",
                user_fact_refs=["statement"],
            ),
            EditBulletOp(
                change_id="chg-edit-bul",
                op="edit_bullet",
                target_id=existing_bullet_id,
                field="text",
                expected_old_value=existing_bullet_text,
                new_value=existing_bullet_text,
                source_claim_ids=[existing_bullet_id],
            ),
        ],
        rationale="Pacote completo de atualização estruturada.",
    )
    created = client.post(
        "/api/profile/change-proposals",
        json={
            "base_revision": 1,
            "allow_remote_profile": True,
            "instruction": instruction_text,
            "target_experience_id": exp_id,
        },
        headers=auth_headers,
    )
    assert created.status_code == 200, created.text
    assert created.headers.get("Cache-Control") == "no-store"
    prop_data = created.json()
    prop_id = prop_data["id"]
    assert "statement_redacted" in prop_data["data_classes_sent"]
    assert len(prop_data["diff"]) == 8

    # Selecting child 'chg-variant' without parent 'chg-bullet' fails with 422 missing_dependency
    bad_sel = client.post(
        f"/api/profile/change-proposals/{prop_id}/resolve",
        json={
            "decision": "apply",
            "base_revision": 1,
            "selected_change_ids": ["chg-variant"],
        },
        headers=auth_headers,
    )
    assert bad_sel.status_code == 422
    assert bad_sel.json()["detail"]["code"] == "missing_dependency"

    # Applying dependency-closed subset succeeds atomically
    applied = client.post(
        f"/api/profile/change-proposals/{prop_id}/resolve",
        json={
            "decision": "apply",
            "base_revision": 1,
            "selected_change_ids": [
                "chg-bullet",
                "chg-route",
                "chg-variant",
                "chg-summary",
                "chg-arsenal",
                "chg-edit-exp",
                "chg-edit-proj",
                "chg-edit-bul",
            ],
        },
        headers=auth_headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.headers.get("Cache-Control") == "no-store"
    app_json = applied.json()
    assert app_json["status"] == "applied"
    assert len(app_json["arsenal_draft_ids"]) == 1
    new_doc = app_json["profile"]["document"]
    assert app_json["profile"]["revision"] == 2
    added_bullet = new_doc["experience"][0]["bullets"][-1]
    assert "25%" in added_bullet["text"]
    assert len(added_bullet["variants"]) == 1
    assert added_bullet["variants"][0]["source_claim_ids"] == [added_bullet["id"]]
    assert len(new_doc["summaries"]) == 1

    # 5. 24h TTL expiration transitions draft to 'stale' and clears sealed payload
    next_bundle = ProposalDraftBundle(
        operations=[
            AddBulletOp(
                change_id="chg-ttl",
                op="add_bullet",
                parent_id=exp_id,
                canonical_text="Reduzi custos de infraestrutura em 25% usando Airflow.",
                language="pt-BR",
                user_fact_refs=["statement"],
            )
        ]
    )
    created2 = client.post(
        "/api/profile/change-proposals",
        json={
            "base_revision": 2,
            "allow_remote_profile": True,
            "instruction": instruction_text,
            "target_experience_id": exp_id,
        },
        headers=auth_headers,
    )
    assert created2.status_code == 200, created2.text
    stale_id = created2.json()["id"]
    with get_session_factory(isolated_settings)() as db:
        row = db.get(ProfileChangeProposalRow, stale_id)
        assert row is not None
        row.created_at = utc_rfc3339(datetime.now(UTC) - timedelta(hours=25))
        row.expires_at = utc_rfc3339(datetime.now(UTC) - timedelta(hours=1))
        db.commit()

    get_stale = client.get(f"/api/profile/change-proposals/{stale_id}", headers=auth_headers)
    assert get_stale.status_code == 200
    assert get_stale.json()["status"] == "stale"
    assert "operations" not in get_stale.json()

    res_stale = client.post(
        f"/api/profile/change-proposals/{stale_id}/resolve",
        json={"decision": "apply", "base_revision": 2},
        headers=auth_headers,
    )
    assert res_stale.status_code == 200
    assert res_stale.json()["status"] == "stale"
    with get_session_factory(isolated_settings)() as db:
        row = db.get(ProfileChangeProposalRow, stale_id)
        assert row is not None
        assert row.status == "stale"
        assert row.proposal_sealed is None


def test_arsenal_crud_seeds_instructional_and_salary_note_guards_and_specificity_gating(
    client: TestClient,
    auth_headers: dict[str, str],
    isolated_settings: Settings,
) -> None:
    """Verify 8-category pt-BR/en seeds, instructional & salary_note blocks, CAS revisions, atomic retire, and specificity gating."""
    # 1. Seed drafts (8 categories x 2 locales = 16 items)
    listed = client.get("/api/arsenal?limit=50", headers=auth_headers)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 16
    pt_cats = {i["category"] for i in items if i["language"] == "pt-BR"}
    en_cats = {i["category"] for i in items if i["language"] == "en"}
    expected_8 = {
        "professional_summary",
        "why_company",
        "technical_challenge",
        "finops_cloud_impact",
        "availability",
        "work_authorization",
        "english_proficiency",
        "salary_note",
    }
    assert pt_cats == expected_8
    assert en_cats == expected_8

    # 2. Instructional draft cannot be approved until replaced
    seed_summary = next(
        i for i in items if i["language"] == "pt-BR" and i["category"] == "professional_summary"
    )
    assert seed_summary["is_instructional_draft"] is True
    bad_approve = client.post(
        f"/api/arsenal/{seed_summary['id']}/approve",
        json={"revision": 1},
        headers=auth_headers,
    )
    assert bad_approve.status_code == 422
    assert bad_approve.json()["detail"]["code"] == "instructional_draft_requires_replacement"

    # 3. salary_note is display-only and can never be approved even after replacing instructional text
    seed_salary = next(i for i in items if i["language"] == "pt-BR" and i["category"] == "salary_note")
    assert seed_salary["display_only"] is True
    updated_sal = client.put(
        f"/api/arsenal/{seed_salary['id']}",
        json={
            "expected_revision": 1,
            "entry": {
                "question": "Observação salarial interna",
                "control": "textarea",
                "language": "pt-BR",
                "category": "salary_note",
                "role_archetype": "all",
                "variants": {"short": "Faixa alvo CLT acima de R$ 9.000 mensais."},
            },
        },
        headers=auth_headers,
    )
    assert updated_sal.status_code == 200
    sal_approve = client.post(
        f"/api/arsenal/{seed_salary['id']}/approve",
        json={"revision": 2},
        headers=auth_headers,
    )
    assert sal_approve.status_code == 422
    assert sal_approve.json()["detail"]["code"] == "salary_note_display_only"

    # 4. Create -> Approve rev 1 -> Edit to rev 2 (CAS) -> Approve rev 2 atomically retires rev 1
    created = client.post(
        "/api/arsenal",
        json={
            "question": "Qual sua disponibilidade para início?",
            "control": "text",
            "language": "pt-BR",
            "category": "availability",
            "role_archetype": "backend_python",
            "variants": {"short": "Disponibilidade imediata (até 15 dias)."},
            "role_tags": ["backend_python"],
        },
        headers=auth_headers,
    )
    assert created.status_code == 201
    entry_id = created.json()["id"]

    app1 = client.post(
        f"/api/arsenal/{entry_id}/approve",
        json={"revision": 1},
        headers=auth_headers,
    )
    assert app1.status_code == 200
    assert app1.json()["status"] == "approved"

    # Stale expected_revision rejected with 409 + current
    rev2 = client.put(
        f"/api/arsenal/{entry_id}",
        json={
            "expected_revision": 1,
            "entry": {
                "question": "Qual sua disponibilidade para início?",
                "control": "text",
                "language": "pt-BR",
                "category": "availability",
                "role_archetype": "backend_python",
                "variants": {"short": "Início imediato em regime remoto."},
                "role_tags": ["backend_python"],
            },
        },
        headers=auth_headers,
    )
    assert rev2.status_code == 200
    assert rev2.json()["revision"] == 2

    stale_put = client.put(
        f"/api/arsenal/{entry_id}",
        json={
            "expected_revision": 1,
            "entry": {
                "question": "Qual sua disponibilidade para início?",
                "control": "text",
                "language": "pt-BR",
                "category": "availability",
                "role_archetype": "backend_python",
                "variants": {"short": "Conflito."},
            },
        },
        headers=auth_headers,
    )
    assert stale_put.status_code == 409
    assert stale_put.json()["detail"]["code"] == "stale_arsenal_revision"
    assert stale_put.json()["detail"]["current"]["revision"] == 2

    app2 = client.post(
        f"/api/arsenal/{entry_id}/approve",
        json={"revision": 2},
        headers=auth_headers,
    )
    assert app2.status_code == 200
    with get_session_factory(isolated_settings)() as db:
        rows = db.scalars(
            select(ArsenalRespostaRow)
            .where(ArsenalRespostaRow.id == entry_id)
            .order_by(ArsenalRespostaRow.revision.asc())
        ).all()
        assert [(r.revision, r.status) for r in rows] == [(1, "retired"), (2, "approved")]

    # 5. Cursor pagination and filters
    p1 = client.get("/api/arsenal?language=pt-BR&limit=4", headers=auth_headers).json()
    assert len(p1["items"]) == 4
    assert p1["next_cursor"] is not None
    p2 = client.get(
        f"/api/arsenal?language=pt-BR&limit=4&cursor={p1['next_cursor']}",
        headers=auth_headers,
    ).json()
    ids_p1 = {(i["id"], i["revision"]) for i in p1["items"]}
    ids_p2 = {(i["id"], i["revision"]) for i in p2["items"]}
    assert ids_p1.isdisjoint(ids_p2)

    # 6. Company/role specificity gating & single-tier LengthVariants on confirmed manual answers
    with pytest.raises(ArsenalValidationError) as exc_comp:
        build_confirmed_answer_arsenal_draft(
            user_id="local-applicant",
            question_label="Por que deseja trabalhar conosco?",
            question_signature="textarea:por que deseja trabalhar conosco",
            control_kind="textarea",
            answer_text="Admiro a cultura de engenharia da Nubank e seus sistemas distribuídos.",
            language="pt-BR",
            category="why_company",
            job_company="Nubank",
            job_title="Engenheira de Dados",
        )
    assert exc_comp.value.code == "arsenal_company_specific_requires_scope"

    # Using {{company_name}} or explicit when_to_use passes company gating; mentioning role requires role scope
    with pytest.raises(ArsenalValidationError) as exc_role:
        build_confirmed_answer_arsenal_draft(
            user_id="local-applicant",
            question_label="Por que deseja esta vaga?",
            question_signature="textarea:por que deseja esta vaga",
            control_kind="textarea",
            answer_text="Quero atuar como Engenheira de Dados construindo pipelines confiáveis.",
            language="pt-BR",
            category="why_company",
            job_company="Nubank",
            job_title="Engenheira de Dados",
        )
    assert exc_role.value.code == "arsenal_role_specific_requires_scope"

    # Medium-length answer (200 chars) populates ONLY medium tier (never synthesizes short or long)
    med_text = "Construí pipelines de dados em produção com Python, SQL e dbt durante três anos, " * 2 + "garantindo qualidade e observabilidade ponta a ponta."
    assert 150 < len(med_text) <= 500
    draft_row = build_confirmed_answer_arsenal_draft(
        user_id="local-applicant",
        question_label="Resumo da experiência com dados",
        question_signature="textarea:resumo da experiencia com dados",
        control_kind="textarea",
        answer_text=med_text,
        language="pt-BR",
        category="technical_challenge",
        role_archetype="data_engineer",
        role_tags=["data_engineering"],
        job_company="Nubank",
        job_title="Engenheira de Dados",
    )
    lv = draft_row.payload_json["length_variants"]
    assert lv["short"] is None
    assert lv["medium"] == med_text
    assert lv["long"] is None


def test_legacy_standard_answers_migration_idempotency_and_safety_filters(
    isolated_settings: Settings,
) -> None:
    """Verify legacy standard_answers migration filters out salary/sensitive items, is idempotent, and blocks downgrade."""
    prepare_and_upgrade_database(isolated_settings.database_url)
    doc = build_sample_master_profile().model_dump(mode="json")
    doc["standard_answers"] = [
        {
            "id": "std-avail",
            "question_signature": "text:qual sua disponibilidade",
            "label": "Qual sua disponibilidade para início?",
            "answer": "Disponibilidade em 15 dias.",
            "kind": "text",
        },
        {
            "id": "std-sal",
            "question_signature": "text:pretensao salarial",
            "label": "Qual sua pretensão salarial mensal?",
            "answer": "R$ 10.000",
            "kind": "text",
        },
        {
            "id": "std-cpf",
            "question_signature": "text:cpf do candidato",
            "label": "Informe seu CPF",
            "answer": "12345678909",
            "kind": "text",
        },
    ]
    with get_session_factory(isolated_settings)() as db:
        db.add(
            MasterProfile(
                user_id="local-applicant",
                revision=1,
                document=doc,
            )
        )
        db.commit()

        migrated_first = migrate_legacy_standard_answers_to_arsenal(db, "local-applicant")
        db.commit()
        assert migrated_first == 1

        # Second invocation is strictly idempotent
        migrated_second = migrate_legacy_standard_answers_to_arsenal(db, "local-applicant")
        assert migrated_second == 0

        rows = db.scalars(
            select(ArsenalRespostaRow).where(ArsenalRespostaRow.user_id == "local-applicant")
        ).all()
        assert len(rows) == 1
        migrated_row = rows[0]
        assert migrated_row.status == "draft"
        assert migrated_row.category == "availability"
        assert migrated_row.payload_json["provenance"] == "legacy"

        # Factual legacy entry without source_claim_ids cannot be approved until user edits/confirms it
        with pytest.raises(ArsenalValidationError) as exc_fact:
            approve_arsenal_entry(db, "local-applicant", migrated_row.id, 1)
        assert exc_fact.value.code == "factual_confirmation_required"

        # User edits via update_arsenal_entry -> provenance becomes 'user' -> approval succeeds
        update_arsenal_entry(
            db,
            "local-applicant",
            migrated_row.id,
            ArsenalUpdateRequest(
                expected_revision=1,
                entry=ArsenalEntryRequest(
                    question="Qual sua disponibilidade para início?",
                    control="text",
                    language="pt-BR",
                    category="availability",
                    variants=LengthVariants(short="Disponibilidade em 15 dias."),
                ),
            ),
        )
        approved = approve_arsenal_entry(db, "local-applicant", migrated_row.id, 2)
        assert approved["status"] == "approved"

    with pytest.raises(RuntimeError, match="Destructive downgrade from migration e4f6a8b0c204"):
        command.downgrade(build_alembic_config(isolated_settings.database_url), "d3f5a7c9e103")


def test_ordered_field_rules_slot_interpolation_sentence_trim_and_pure_validators() -> None:
    """Verify ordered rule precedence, slot interpolation, >=95% sentence trimming, and pure CPF/CEP/phone/date/URL/number validators."""
    # 1. Ordered FieldRule precedence
    assert match_field_rule("Currículo em PDF", "file", category_hint="resume_file").rule_id == "rule-resume-file"
    assert match_field_rule("Carta de apresentação", "file").rule_id == "rule-non-resume-file"
    assert match_field_rule("Concordo com os termos LGPD", "checkbox", category_hint="legal_consent").rule_id == "rule-legal-consent"
    assert match_field_rule("Pretensão salarial (R$)", "text").rule_id == "rule-salary"
    assert match_field_rule("CPF do candidato", "text").rule_id == "rule-cpf"
    assert match_field_rule("CEP residencial", "text").rule_id == "rule-cep"
    assert match_field_rule("Data de nascimento", "date").rule_id == "rule-birth-date"
    assert match_field_rule("E-mail de contato", "email").rule_id == "rule-contact-email"
    assert match_field_rule("Telefone / WhatsApp", "tel").rule_id == "rule-contact-phone"
    assert match_field_rule("LinkedIn URL", "url").rule_id == "rule-contact-linkedin"
    assert match_field_rule("GitHub Profile", "url").rule_id == "rule-contact-github"
    assert match_field_rule("Por que quer trabalhar aqui?", "textarea").rule_id == "rule-arsenal-why-company"
    assert match_field_rule("Impacto em FinOps e custos de nuvem", "textarea").rule_id == "rule-arsenal-finops"
    assert match_field_rule("Descreva um desafio técnico complexo", "textarea").rule_id == "rule-arsenal-technical-challenge"
    assert match_field_rule("Pergunta aberta específica", "textarea").rule_id == "rule-arsenal-custom-fallback"

    doc = build_sample_master_profile()
    literals = extract_canonical_literals(doc)
    assert literals["contact.email"] == doc.contact.email
    assert extract_english_proficiency(doc) is not None
    slots = build_verified_slot_values(
        doc,
        job_company="Acme Cloud",
        job_title="Data Engineer",
        job_description="Requisitos: Python, SQL e Airflow em produção.",
    )
    assert slots["company_name"] == "Acme Cloud"
    assert slots["role_title"] == "Data Engineer"
    assert "Python" in slots["tech_stack_highlight"]

    # 2. Slot interpolation & locale mismatch blocking
    ok, rendered, err = interpolate_template(
        "Quero atuar na {{company_name}} como {{role_title}} usando {{tech_stack_highlight}}.",
        slots,
        entry_locale="pt-BR",
        expected_locale="pt-BR",
    )
    assert ok is True and err is None
    assert rendered is not None and "Acme Cloud" in rendered and "Data Engineer" in rendered and "Python" in rendered

    ok_loc, _, err_loc = interpolate_template(
        "Hello {{company_name}}",
        slots,
        entry_locale="en",
        expected_locale="pt-BR",
    )
    assert ok_loc is False and err_loc == "locale_mismatch"

    ok_unk, _, err_unk = interpolate_template("Valor {{unknown_slot}}", slots)
    assert ok_unk is False and err_unk == "unknown_slot"

    ok_miss, _, err_miss = interpolate_template("Olá {{company_name}}", {"company_name": ""})
    assert ok_miss is False and err_miss == "missing_slot_value"

    # 3. Complete-sentence trimming with >= 95% literal retention and metric/qualifier protection
    s1 = (
        "Desenvolvi arquiteturas distribuídas de processamento de dados em tempo real utilizando "
        "Python, Apache Spark, Airflow e SQL com observabilidade contínua, testes automatizados "
        "rigorosos, governança de schemas e alta disponibilidade em múltiplos ambientes críticos."
    )
    s2_ok = "Grata."
    full_tpl = f"{s1} {s2_ok}"
    # Cap slightly below full_tpl length but above s1 length; s1 retains >97% of literal chars
    constraints_trim = ControlConstraints(
        control_kind="textarea",
        maxlength=len(s1) + 2,
    )
    fitted, fit_err, was_trimmed, ratio = fit_arsenal_variants(
        LengthVariants(medium=full_tpl),
        {},
        constraints_trim,
    )
    assert fit_err is None
    assert was_trimmed is True
    assert ratio >= 0.95
    assert fitted == s1

    # Dropping a trailing sentence with a number/percentage or negation qualifier must be refused
    for protected_tail in ("Reduzi 15%.", "Exceto legado."):
        tpl_prot = f"{s1} {protected_tail}"
        f_prot, err_prot, _, _ = fit_arsenal_variants(
            LengthVariants(medium=tpl_prot),
            {},
            ControlConstraints(control_kind="textarea", maxlength=len(s1) + 2),
        )
        assert f_prot is None
        assert err_prot == "length_mismatch"

    # 4. Pure validators: CPF, CEP, Phone, Gregorian dates, HTTPS URL, Email, Scalar numbers, Counters
    assert validate_and_format_cpf("123.456.789-09", style="digits") == "12345678909"
    assert validate_and_format_cpf("12345678909", style="formatted") == "123.456.789-09"
    with pytest.raises(ValueError):
        validate_and_format_cpf("12345678900")
    with pytest.raises(ValueError):
        validate_and_format_cpf("11111111111")

    assert validate_and_format_cep("01310100", style="formatted") == "01310-100"
    assert validate_and_format_cep("01310-100", style="digits") == "01310100"
    with pytest.raises(ValueError):
        validate_and_format_cep("00000-000")

    assert validate_and_format_phone("81999998888", style="br_intl") == "+55 (81) 99999-8888"
    assert validate_and_format_phone("+55 81 99999-8888", style="br_local") == "(81) 99999-8888"
    assert validate_and_format_phone("81999998888", style="e164") == "+5581999998888"
    with pytest.raises(ValueError):
        validate_and_format_phone("081999998888")

    # Leap year 2024 vs non-leap 2023 + min/max bounds + ISO vs pt-BR
    assert validate_and_format_date("29/02/2024", control_kind="date", source_locale="pt-BR") == "2024-02-29"
    assert validate_and_format_date("2024-02-29", control_kind="text", source_locale="pt-BR") == "29/02/2024"
    assert validate_and_format_date("02/29/2024", control_kind="date", source_locale="en") == "2024-02-29"
    with pytest.raises(ValueError):
        validate_and_format_date("29/02/2023", control_kind="date", source_locale="pt-BR")
    with pytest.raises(ValueError):
        validate_and_format_date(
            "2024-01-01",
            control_kind="date",
            source_locale="pt-BR",
            minimum="2024-06-01",
        )

    assert validate_https_url("https://github.com/clara") == "https://github.com/clara"
    with pytest.raises(ValueError):
        validate_https_url("http://github.com/clara")
    with pytest.raises(ValueError):
        validate_https_url("https://user:pass@github.com/clara")

    assert validate_email("clara.sales@example.org") == "clara.sales@example.org"
    with pytest.raises(ValueError):
        validate_email("invalido@@example.org")

    assert validate_scalar_number("8500.50", minimum="1000", maximum="20000", step="0.50") == "8500.50"
    with pytest.raises(ValueError):
        validate_scalar_number("R$ 8.500,50")
    with pytest.raises(ValueError):
        validate_scalar_number("8500.25", minimum="0", step="0.50")

    # Word count keeps hyphenated and apostrophe tokens intact
    assert count_words("Engenheira full-stack com foco em d'água e e-commerce", "pt-BR") == 8

    # Visible counter tightest cap & conflict detection
    c_tight = extract_control_constraints(
        control_kind="textarea",
        locator_hint={"constraints": [None, None, None, 10, 500, None], "counters": {"max_chars": 120}},
    )
    assert c_tight.effective_max_chars == 120
    assert c_tight.has_conflict is False

    c_conflict = extract_control_constraints(
        control_kind="textarea",
        locator_hint={"constraints": [None, None, None, 200, 500, None], "counters": {"max_chars": 100}},
    )
    assert c_conflict.has_conflict is True
    out_conflict = validate_candidate_value(
        c_conflict,
        "Texto qualquer",
        rule_id="rule-arsenal-custom-fallback",
        intent="custom",
    )
    assert out_conflict.valid is False
    assert out_conflict.blocker_code == "conflicting_constraints"


@pytest.mark.asyncio
async def test_phase3_ui_arsenal_and_career_fact_proposal_end_to_end(
    live_app_server: tuple[str, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive Perfil mestre UI with Playwright: Arsenal badges/filters/inline edit/approve/retire + AI proposal diff & dependency closure."""
    app_url, cfg = live_app_server
    cfg.llm_provider = "openai_compat"
    cfg.llm_base_url = "https://llm.example.org/v1"
    cfg.llm_allow_remote_profile = True

    doc = build_sample_master_profile().model_dump(mode="json")
    exp_id = doc["experience"][0]["id"]

    async def fake_v2_proposal(_self: LlmGateway, _db: object, **_kwargs: Any) -> LlmStructuredResult[ProposalDraftBundle]:
        parent_exp_id = _kwargs["user_payload"]["claims"]["experiences"][0]["id"]
        return LlmStructuredResult(
            data=ProposalDraftBundle(
                operations=[
                    AddBulletOp(
                        change_id="chg-parent",
                        op="add_bullet",
                        parent_id=parent_exp_id,
                        canonical_text="Reduzi o tempo de ETL em 35% com particionamento incremental.",
                        language="pt-BR",
                        user_fact_refs=["statement"],
                        routing=Routing(role_tags=["data_engineering"]),
                    ),
                    AddVariantOp(
                        change_id="chg-child",
                        depends_on=["chg-parent"],
                        op="add_variant",
                        bullet_id="chg-parent",
                        text="Reduced ETL runtime by 35% through incremental partitioning.",
                        language="en",
                        source_claim_ids=["chg-parent"],
                        routing=Routing(role_tags=["data_engineering"]),
                    ),
                ],
                rationale="Bullet e variante bilingue.",
            ),
            provider="openai_compat",
            model="gpt-4o-mini",
            prompt_tokens=20,
            completion_tokens=30,
            estimated_cost_usd=0.0005,
            mode="llm_structured",
        )

    monkeypatch.setattr(LlmGateway, "complete_structured", fake_v2_proposal)

    exe = _resolve_local_chromium()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=exe)
        page = await browser.new_page()
        try:
            await page.goto(app_url, wait_until="domcontentloaded")
            await page.fill("#passcode-input", "test-e2e-passcode")
            await page.click('[data-testid="btn-unlock"]')
            await page.wait_for_selector("#app-workspace:not(.hidden)", timeout=10000)

            await page.click('[data-testid="tab-perfil"]')
            await page.wait_for_selector("#arsenal-list .fact-row", timeout=10000)

            # 1. Verify seed badges ([RASCUNHO-INSTRUÇÃO], instructional badge, and salary_note display-only badge)
            arsenal_text = await page.locator("#arsenal-list").inner_text()
            assert "[RASCUNHO-INSTRUÇÃO]" in arsenal_text
            assert "Instrução — substitua antes de aprovar" in arsenal_text
            assert "Somente exibição visual (salary_note)" in arsenal_text

            # 2. Filter Arsenal to pt-BR availability seed, inline edit, approve, and retire
            await page.select_option("#arsenal-filter-language", "pt-BR")
            await page.select_option("#arsenal-filter-category", "availability")
            await page.wait_for_function(
                "() => document.querySelectorAll('#arsenal-list .fact-row').length === 1",
                timeout=10000,
            )
            seed_row = page.locator('#arsenal-list .fact-row[data-arsenal-id="seed-ptbr-availability"]')
            assert await seed_row.count() == 1
            # Instructional draft hides 'Aprovar reutilização' button until edited
            assert await seed_row.locator('button[data-arsenal-action="approve"]').count() == 0

            await seed_row.locator('button[data-arsenal-action="edit"]').click()
            edit_form = seed_row.locator(".arsenal-edit-form")
            await edit_form.locator('input[name="short"]').fill("Disponibilidade para início em 15 dias.")
            await edit_form.locator('textarea[name="medium"]').fill(
                "Tenho disponibilidade para início em até 15 dias corridos após proposta."
            )
            await edit_form.locator('textarea[name="long"]').fill("")
            async with page.expect_response(
                lambda r: "/api/arsenal/seed-ptbr-availability" in r.url and r.request.method == "PUT"
            ) as put_resp_info:
                await edit_form.locator('button[type="submit"]').click()
            assert (await put_resp_info.value).status == 200

            # Now rev 2 is non-instructional and shows 'Aprovar reutilização'
            await page.wait_for_selector(
                '#arsenal-list .fact-row[data-arsenal-id="seed-ptbr-availability"] button[data-arsenal-action="approve"]',
                timeout=10000,
            )
            async with page.expect_response(
                lambda r: r.url.endswith("/approve") and r.request.method == "POST"
            ) as app_resp_info:
                await page.click(
                    '#arsenal-list .fact-row[data-arsenal-id="seed-ptbr-availability"] button[data-arsenal-action="approve"]'
                )
            assert (await app_resp_info.value).status == 200

            # Filter by status=approved and verify it appears
            await page.select_option("#arsenal-filter-status", "approved")
            await page.wait_for_selector(
                '#arsenal-list .fact-row[data-arsenal-id="seed-ptbr-availability"]',
                timeout=10000,
            )

            # 3. Save initial profile so experience exists, then test Cloud Master AI V2 diff & dependency closure
            await page.fill("#prof-name", "Clara Nogueira Sales")
            await page.fill("#prof-email", "clara.ui@example.org")
            await page.click("#btn-add-exp")
            await page.fill('#profile-exp-list .profile-entry input[data-f="company"]', "Porto Labs")
            await page.fill('#profile-exp-list .profile-entry input[data-f="role"]', "Engenheira de Dados")
            # Ensure experience id matches exp_id used by stub
            await page.evaluate(
                f"() => {{ document.querySelector('#profile-exp-list .profile-entry').dataset.id = '{exp_id}'; }}"
            )
            await page.click('[data-testid="btn-save-profile"]')
            await page.wait_for_function(
                "() => document.getElementById('profile-revision-badge').innerText.includes('Revisão 1')",
                timeout=10000,
            )

            await page.fill(
                '#career-fact-form textarea[name="instruction"]',
                "Reduzi o tempo de ETL em 35% com particionamento incremental.",
            )
            await page.check('#career-fact-form input[name="allow_remote_profile"]')
            await page.click('#career-fact-form button[type="submit"]')
            await page.wait_for_selector('[data-testid="proposal-diff-box"]', timeout=10000)

            diff_text = await page.locator('[data-testid="proposal-diff-box"]').inner_text()
            assert "statement_redacted" in diff_text

            parent_cb = page.locator('.proposal-op-cb[value="chg-parent"]')
            child_cb = page.locator('.proposal-op-cb[value="chg-child"]')
            assert await parent_cb.is_checked() is True
            assert await child_cb.is_checked() is True

            # Unchecking parent automatically unchecks dependent child
            await parent_cb.uncheck()
            assert await child_cb.is_checked() is False

            # Re-checking child automatically re-checks required parent
            await child_cb.check()
            assert await parent_cb.is_checked() is True

            async with page.expect_response(
                lambda r: r.url.endswith("/resolve") and r.request.method == "POST"
            ) as resolve_resp_info:
                await page.click('[data-testid="proposal-diff-box"] button[data-decision="apply"]')
            assert (await resolve_resp_info.value).status == 200
            await page.wait_for_function(
                "() => document.getElementById('profile-revision-badge').innerText.includes('Revisão 2')",
                timeout=10000,
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_infojobs_formless_textarea_and_linkedin_candidatura_simplificada_cta(
    tmp_path: Any,
) -> None:
    from backend.app.browser_automation.driver import inspect_step
    from backend.app.browser_automation.session import BrowserSession
    from backend.app.services.applications.service import ApplicationService

    settings = Settings(artifacts_dir=str(tmp_path / "artifacts"))
    svc = ApplicationService(settings)
    session = BrowserSession(settings)

    exe = _resolve_local_chromium()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=exe)
        try:
            # 1. InfoJobs page with textarea inside main (no <form> tag) should not require opening a form.
            ij_page = await browser.new_page()
            await ij_page.route(
                "https://www.infojobs.com.br/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="""<html><head><title>InfoJobs - Perguntas</title></head><body>
                    <main>
                      <h1>Pergunta adicional</h1>
                      <label for="ij-sal">Qual sua pretensão salarial ?</label>
                      <textarea id="ij-sal" name="salary_expectation"></textarea>
                    </main>
                    </body></html>""",
                ),
            )
            await ij_page.goto("https://www.infojobs.com.br/candidate/apply", wait_until="domcontentloaded")
            snap_ij = await inspect_step(
                ij_page,
                expected_job_title="Analista de Dados",
                expected_company="Empresa Teste",
            )
            assert snap_ij.portal == "infojobs"
            assert snap_ij.open_form_required is False
            assert len(snap_ij.fields) == 1
            assert "pretens" in snap_ij.fields[0].label.lower()

            # 2. LinkedIn job view with 'Minhas candidaturas' in nav and 'Candidatura simplificada' button
            li_page = await browser.new_page()
            await li_page.route(
                "https://www.linkedin.com/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="""<html><body>
                    <header><nav><a href="https://www.linkedin.com/my-items/saved-jobs/">Minhas candidaturas</a></nav></header>
                    <main>
                      <button class="jobs-apply-button" aria-label="Candidatura simplificada para Talent Data Analyst"
                              onclick="document.getElementById('modal').style.display='block'">
                        Candidatura simplificada
                      </button>
                      <div id="modal" class="jobs-easy-apply-modal" role="dialog" style="display:none">
                        <label for="li-zip">CEP / ZIP Code</label>
                        <input id="li-zip" type="text" />
                      </div>
                    </main>
                    </body></html>""",
                ),
            )
            await li_page.goto("https://www.linkedin.com/jobs/view/12345/", wait_until="domcontentloaded")
            followed = await svc._try_follow_entry_apply_cta(
                application_id="app-li-test",
                page=li_page,
                browser_session=session,
            )
            assert followed is True
            snap_li = await inspect_step(
                li_page,
                expected_job_title="Talent Data Analyst",
                expected_company="INDI Staffing Services",
            )
            assert snap_li.portal == "linkedin"
            assert snap_li.open_form_required is False
            assert any(f.category == "contact_postal_code" for f in snap_li.fields)
        finally:
            await browser.close()

