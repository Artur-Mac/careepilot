"""Phase 1: master_profiles backfill, state simplification, and approval/scoring cleanup.

Revision ID: b1d2e3f4a501
Revises: adf094dd1fee
Create Date: 2026-11-12 22:30:00.000000
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "b1d2e3f4a501"
down_revision: str | Sequence[str] | None = "adf094dd1fee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_STATUS_MAP: dict[str, str] = {
    "verified": "done",
    "submitted_unverified": "done",
    "rejected": "closed",
    "expired": "closed",
    "canceled": "closed",
    "ready_for_manual_review": "review",
    "blocked": "needs_user",
    "needs_user_input": "needs_user",
}

_CONTACT_KEY_MAP: dict[str, str] = {
    "full_name": "name",
    "name": "name",
    "nome": "name",
    "nome_completo": "name",
    "candidate_name": "name",
    "email": "email",
    "e-mail": "email",
    "mail": "email",
    "phone": "phone",
    "telefone": "phone",
    "celular": "phone",
    "whatsapp": "phone",
    "mobile": "phone",
    "city": "city",
    "cidade": "city",
    "state": "state",
    "estado": "state",
    "uf": "state",
    "country": "country",
    "pais": "country",
    "país": "country",
    "linkedin": "linkedin",
    "linkedin_url": "linkedin",
    "github": "github",
    "github_url": "github",
    "portfolio": "portfolio",
    "portfolio_url": "portfolio",
    "website": "portfolio",
    "site": "portfolio",
}

_GOV_ID_KEY_RE = re.compile(r"\b(cpf|rg|cnh|passaporte|passport|ssn|pis)\b", re.I)


def _parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return default


def _ts_sort_key(created_at: Any, row_id: str) -> tuple[float, str]:
    if isinstance(created_at, datetime):
        dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        return (dt.timestamp(), row_id)
    if isinstance(created_at, str) and created_at.strip():
        raw = created_at.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (dt.timestamp(), row_id)
        except ValueError:
            pass
    return (0.0, row_id)


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_location_parts(raw: str) -> tuple[str | None, str | None, str | None]:
    cleaned = re.sub(r"\([^)]*\)", "", raw).strip()
    parts = [p.strip() for p in re.split(r"[,—–|/]+|\s+-\s+", cleaned) if p.strip()]
    if not parts:
        return None, None, None
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        second = parts[1]
        if second.lower() in {"brasil", "brazil", "eua", "usa", "portugal"}:
            return parts[0], None, second
        return parts[0], second, None
    return parts[0], parts[1], parts[2]


def _note_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_source(evidences: list[dict[str, Any]], fallback: str) -> str:
    if evidences:
        names = [str(ev.get("document_name") or "").strip() for ev in evidences if ev.get("document_name")]
        if names:
            return f"migrated:{names[0]}"
    return fallback


def _build_master_profile_document(
    facts: list[dict[str, Any]],
    evidences_by_fact: dict[str, list[dict[str, Any]]],
    user_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    singleton_candidates: dict[str, list[tuple[tuple[float, str], dict[str, Any]]]] = defaultdict(list)
    experience: list[dict[str, Any]] = []
    education: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    languages: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    other_links: list[str] = []

    for fact in facts:
        fid = str(fact["id"])
        cat = str(fact["category"] or "").strip().lower()
        key = str(fact["key"] or "").strip()
        key_low = key.lower()
        val = str(fact["value"] or "").strip()
        meta = _parse_json(fact.get("metadata_json"), {})
        if not isinstance(meta, dict):
            meta = {}
        evs = evidences_by_fact.get(fid, [])
        ev_refs = [
            {
                "document_name": ev.get("document_name"),
                "section_or_page": ev.get("section_or_page"),
                "snippet": ev.get("supporting_snippet"),
                "source_sha256": ev.get("source_sha256"),
            }
            for ev in evs
        ]
        source_label = _format_source(evs, f"migrated:{cat or 'fact'}")
        sort_key = _ts_sort_key(fact.get("created_at"), fid)

        if cat == "contact" or key_low in _CONTACT_KEY_MAP or key_low in {"location", "localizacao", "localização"}:
            if key_low in _CONTACT_KEY_MAP:
                slot = _CONTACT_KEY_MAP[key_low]
                singleton_candidates[slot].append((sort_key, {**fact, "_evs": evs, "_source": source_label}))
            elif key_low in {"location", "localizacao", "localização", "endereco", "endereço"}:
                singleton_candidates["location_raw"].append((sort_key, {**fact, "_evs": evs, "_source": source_label}))
            elif val.startswith(("http://", "https://")):
                if val not in other_links:
                    other_links.append(val)
            elif not _GOV_ID_KEY_RE.search(key_low):
                imports.append(
                    {
                        "id": fid,
                        "sha256": _note_sha(f"{key}:{val}"),
                        "filename": f"Nota migrada ({key[:80] or 'contato'})",
                        "extracted_text": f"{key}: {val}" if key else val,
                        "kind": "migrated_note",
                        "source": source_label,
                        "imported_at": _iso_or_none(fact.get("created_at")),
                    }
                )
            continue

        if cat == "experience":
            raw_bullets = meta.get("bullets") if isinstance(meta.get("bullets"), list) else []
            bullets = [
                {"id": f"{fid}-b{idx}", "text": str(b).strip()[:600]}
                for idx, b in enumerate(raw_bullets[:20])
                if str(b).strip()
            ]
            company = meta.get("company")
            role = meta.get("role")
            if not company and not role and key:
                role = key
            experience.append(
                {
                    "id": fid,
                    "company": str(company).strip() if company else None,
                    "role": str(role).strip() if role else None,
                    "period": str(meta.get("period")).strip() if meta.get("period") else None,
                    "location": str(meta.get("location")).strip() if meta.get("location") else None,
                    "details": val,
                    "bullets": bullets,
                    "source": source_label,
                    "evidence": ev_refs,
                }
            )
            continue

        if cat == "education":
            education.append(
                {
                    "id": fid,
                    "institution": str(meta.get("institution")).strip() if meta.get("institution") else None,
                    "degree": (str(meta.get("degree")).strip() if meta.get("degree") else None) or (key or None),
                    "period": str(meta.get("period")).strip() if meta.get("period") else None,
                    "details": val,
                    "source": source_label,
                    "evidence": ev_refs,
                }
            )
            continue

        if cat == "project":
            raw_bullets = meta.get("bullets") if isinstance(meta.get("bullets"), list) else []
            bullets = [
                {"id": f"{fid}-b{idx}", "text": str(b).strip()[:600]}
                for idx, b in enumerate(raw_bullets[:15])
                if str(b).strip()
            ]
            projects.append(
                {
                    "id": fid,
                    "name": (str(meta.get("name") or key or val).strip()[:240]) or "Projeto",
                    "url": str(meta.get("url")).strip() if meta.get("url") else None,
                    "details": val,
                    "bullets": bullets,
                    "source": source_label,
                    "evidence": ev_refs,
                }
            )
            continue

        if cat == "skill":
            skill_name = str(meta.get("canonical") or val or key).strip()[:120]
            if skill_name:
                skills.append(
                    {
                        "id": fid,
                        "name": skill_name,
                        "category": str(meta.get("skill_category")).strip() if meta.get("skill_category") else None,
                        "level": str(meta.get("level")).strip() if meta.get("level") else None,
                        "source": source_label,
                    }
                )
            continue

        if cat == "language":
            lang_name = val if len(val) <= 120 else (key[:120] or val[:120])
            languages.append(
                {
                    "id": fid,
                    "name": lang_name,
                    "proficiency": str(meta.get("proficiency")).strip() if meta.get("proficiency") else None,
                    "details": val if len(val) > 120 or (key and key.lower() not in {"idiomas", "languages", lang_name.lower()}) else None,
                    "source": source_label,
                }
            )
            continue

        if cat in {"work_auth", "work_authorization"} or key_low in {"work_authorization", "work_authorization_br"}:
            singleton_candidates["work_authorization"].append((sort_key, {**fact, "_evs": evs, "_source": source_label}))
            continue

        if cat == "availability" or key_low in {"availability", "disponibilidade"}:
            singleton_candidates["availability"].append((sort_key, {**fact, "_evs": evs, "_source": source_label}))
            continue

        if not _GOV_ID_KEY_RE.search(key_low):
            imports.append(
                {
                    "id": fid,
                    "sha256": _note_sha(f"{key}:{val}"),
                    "filename": f"Nota migrada ({cat}: {key[:60] or fid[:8]})",
                    "extracted_text": f"{key}: {val}" if key else val,
                    "kind": "migrated_note",
                    "source": source_label,
                    "imported_at": _iso_or_none(fact.get("created_at")),
                }
            )

    resolved_singletons: dict[str, str | None] = {}
    for slot, entries in singleton_candidates.items():
        if slot == "location_raw":
            continue
        entries_sorted = sorted(entries, key=lambda pair: pair[0], reverse=True)
        _, winner = entries_sorted[0]
        winner_val = str(winner["value"] or "").strip()
        resolved_singletons[slot] = winner_val or None
        for _, loser in entries_sorted[1:]:
            loser_val = str(loser["value"] or "").strip()
            if loser_val and loser_val != winner_val:
                imports.append(
                    {
                        "id": str(loser["id"]),
                        "sha256": _note_sha(f"{slot}:{loser_val}"),
                        "filename": f"Contato conflitante ({loser['key']})",
                        "extracted_text": f"{loser['key']}: {loser_val}",
                        "kind": "migrated_note",
                        "source": str(loser.get("_source") or f"migrated:contact:{loser['key']}"),
                        "imported_at": _iso_or_none(loser.get("created_at")),
                    }
                )

    loc_entries = sorted(singleton_candidates.get("location_raw", []), key=lambda pair: pair[0], reverse=True)
    if loc_entries:
        _, loc_winner = loc_entries[0]
        loc_val = str(loc_winner["value"] or "").strip()
        parsed_city, parsed_state, parsed_country = _parse_location_parts(loc_val)
        if parsed_city and not resolved_singletons.get("city"):
            resolved_singletons["city"] = parsed_city
        elif parsed_city and resolved_singletons.get("city") and parsed_city != resolved_singletons["city"]:
            imports.append(
                {
                    "id": str(loc_winner["id"]),
                    "sha256": _note_sha(f"location:{loc_val}"),
                    "filename": f"Contato conflitante ({loc_winner['key']})",
                    "extracted_text": f"{loc_winner['key']}: {loc_val}",
                    "kind": "migrated_note",
                    "source": str(loc_winner.get("_source") or "migrated:contact:location"),
                    "imported_at": _iso_or_none(loc_winner.get("created_at")),
                }
            )
        if parsed_state and not resolved_singletons.get("state"):
            resolved_singletons["state"] = parsed_state
        if parsed_country and not resolved_singletons.get("country"):
            resolved_singletons["country"] = parsed_country
        for _, loc_loser in loc_entries[1:]:
            loser_val = str(loc_loser["value"] or "").strip()
            if loser_val and loser_val != loc_val:
                imports.append(
                    {
                        "id": str(loc_loser["id"]),
                        "sha256": _note_sha(f"location:{loser_val}"),
                        "filename": f"Contato conflitante ({loc_loser['key']})",
                        "extracted_text": f"{loc_loser['key']}: {loser_val}",
                        "kind": "migrated_note",
                        "source": str(loc_loser.get("_source") or "migrated:contact:location"),
                        "imported_at": _iso_or_none(loc_loser.get("created_at")),
                    }
                )

    target_roles: list[str] = []
    pref_location: str | None = None
    work_model: list[str] = []
    seniority: list[str] = []
    contract_types: list[str] = []
    excluded_companies: list[str] = []
    salary_amount: float | None = None
    salary_currency: str | None = None
    availability: str | None = resolved_singletons.get("availability")
    work_authorization: str | None = resolved_singletons.get("work_authorization")

    if user_settings:
        raw_roles = _parse_json(user_settings.get("target_roles"), [])
        if isinstance(raw_roles, list):
            target_roles = [str(x).strip() for x in raw_roles if str(x).strip()]
        raw_locs = _parse_json(user_settings.get("preferred_locations"), [])
        if isinstance(raw_locs, list):
            locs = [str(x).strip() for x in raw_locs if str(x).strip()]
            if locs:
                pref_location = ", ".join(locs)
        raw_work = _parse_json(user_settings.get("workplace_preferences"), [])
        if isinstance(raw_work, list):
            work_model = [str(x).strip() for x in raw_work if str(x).strip()]
        raw_sen = _parse_json(user_settings.get("seniority_preferences"), [])
        if isinstance(raw_sen, list):
            seniority = [str(x).strip() for x in raw_sen if str(x).strip()]
        raw_ct = _parse_json(user_settings.get("contract_types"), [])
        if isinstance(raw_ct, list):
            contract_types = [str(x).strip() for x in raw_ct if str(x).strip()]
        raw_excl = _parse_json(user_settings.get("excluded_companies"), [])
        if isinstance(raw_excl, list):
            excluded_companies = [str(x).strip() for x in raw_excl if str(x).strip()]
        if user_settings.get("salary_floor") is not None:
            salary_amount = float(user_settings["salary_floor"])
            salary_currency = str(user_settings.get("salary_currency") or "BRL").strip() or "BRL"
        # Current factory defaults ("imediata" and "Brasil (autorizado)") lack user provenance.
        us_avail = str(user_settings.get("availability") or "").strip()
        if not availability and us_avail and us_avail.lower() != "imediata":
            availability = us_avail
        us_auth = str(user_settings.get("work_authorization") or "").strip()
        if not work_authorization and us_auth and us_auth != "Brasil (autorizado)":
            work_authorization = us_auth

    return {
        "contact": {
            "name": resolved_singletons.get("name"),
            "email": resolved_singletons.get("email"),
            "phone": resolved_singletons.get("phone"),
            "city": resolved_singletons.get("city"),
            "state": resolved_singletons.get("state"),
            "country": resolved_singletons.get("country"),
            "links": {
                "linkedin": resolved_singletons.get("linkedin"),
                "github": resolved_singletons.get("github"),
                "portfolio": resolved_singletons.get("portfolio"),
                "other": other_links,
            },
        },
        "experience": experience,
        "education": education,
        "projects": projects,
        "skills": skills,
        "languages": languages,
        "preferences": {
            "target_roles": target_roles,
            "location": pref_location,
            "work_model": work_model,
            "salary_amount": salary_amount,
            "salary_currency": salary_currency,
            "salary_period": "monthly" if salary_amount is not None else None,
            "salary_text": None,
            "availability": availability,
            "work_authorization": work_authorization,
            "seniority": seniority,
            "contract_types": contract_types,
            "excluded_companies": excluded_companies,
        },
        "standard_answers": [],
        "imports": imports,
    }


def _has_explicit_user_preferences(user_settings: dict[str, Any]) -> bool:
    if _parse_json(user_settings.get("target_roles"), []):
        return True
    if _parse_json(user_settings.get("preferred_locations"), []):
        return True
    if _parse_json(user_settings.get("excluded_companies"), []):
        return True
    if user_settings.get("salary_floor") is not None:
        return True
    us_avail = str(user_settings.get("availability") or "").strip()
    if us_avail and us_avail.lower() != "imediata":
        return True
    us_auth = str(user_settings.get("work_authorization") or "").strip()
    if us_auth and us_auth != "Brasil (autorizado)":
        return True
    return False


def upgrade() -> None:
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    if is_sqlite:
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=OFF"))

    # 1. Create master_profiles
    op.create_table(
        "master_profiles",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )

    # 2. Backfill real profile_facts + explicit user_settings into master_profiles
    facts_rows = [
        dict(row._mapping)
        for row in conn.execute(
            sa.text(
                "SELECT id, user_id, category, key, value, metadata_json, sensitivity, "
                "user_verified, confidence, is_fictional_demo, created_at "
                "FROM profile_facts WHERE is_fictional_demo = 0"
            )
        )
    ]
    evidence_rows = [
        dict(row._mapping)
        for row in conn.execute(
            sa.text(
                "SELECT id, fact_id, document_name, section_or_page, supporting_snippet, "
                "source_sha256, created_at FROM fact_evidence"
            )
        )
    ]
    settings_rows = [
        dict(row._mapping)
        for row in conn.execute(sa.text("SELECT * FROM user_settings"))
    ]

    evidences_by_fact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in evidence_rows:
        evidences_by_fact[str(ev["fact_id"])].append(ev)

    facts_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts_rows:
        facts_by_user[str(fact["user_id"])].append(fact)

    settings_by_user: dict[str, dict[str, Any]] = {
        str(row["user_id"]): row for row in settings_rows
    }

    now_utc = datetime.now(UTC)
    candidate_users = sorted(set(facts_by_user.keys()) | set(settings_by_user.keys()))
    master_profiles_table = sa.table(
        "master_profiles",
        sa.column("user_id", sa.String(length=64)),
        sa.column("document", sa.JSON()),
        sa.column("revision", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for uid in candidate_users:
        user_facts = facts_by_user.get(uid, [])
        user_cfg = settings_by_user.get(uid)
        if not user_facts and not (user_cfg and _has_explicit_user_preferences(user_cfg)):
            continue
        doc = _build_master_profile_document(user_facts, evidences_by_fact, user_cfg)
        conn.execute(
            master_profiles_table.insert().values(
                user_id=uid,
                document=doc,
                revision=1,
                updated_at=now_utc,
            )
        )

    # 3. Clean fictional demo jobs identified strictly by explicit fictional source/flag
    fictional_job_ids = {
        str(row[0])
        for row in conn.execute(
            sa.text(
                "SELECT DISTINCT jp.id FROM job_postings jp "
                "LEFT JOIN employers emp ON jp.employer_id = emp.id "
                "LEFT JOIN job_source_records jsr ON jsr.job_id = jp.id "
                "WHERE jp.source = 'fictional_ats' "
                "   OR COALESCE(emp.is_fictional_test, 0) = 1 "
                "   OR COALESCE(jsr.source, '') = 'fictional_ats'"
            )
        )
    }
    if fictional_job_ids:
        job_id_list = sorted(fictional_job_ids)
        conn.execute(
            sa.text("DELETE FROM browser_attempts WHERE application_attempt_id IN ("
                    "SELECT aa.id FROM application_attempts aa "
                    "JOIN applications a ON aa.application_id = a.id "
                    "WHERE a.job_id IN :jids)"
                    ).bindparams(sa.bindparam("jids", expanding=True)),
            {"jids": job_id_list},
        )
        for child_tbl, fk_col in (
            ("approval_decisions", "application_id"),
            ("application_attempts", "application_id"),
            ("approval_requests", "application_id"),
            ("question_requests", "application_id"),
        ):
            conn.execute(
                sa.text(
                    f"DELETE FROM {child_tbl} WHERE {fk_col} IN ("
                    "SELECT id FROM applications WHERE job_id IN :jids)"
                ).bindparams(sa.bindparam("jids", expanding=True)),
                {"jids": job_id_list},
            )
        for tbl in (
            "applications",
            "resume_versions",
            "answer_versions",
            "match_evaluations",
            "job_requirements",
            "job_source_records",
        ):
            conn.execute(
                sa.text(f"DELETE FROM {tbl} WHERE job_id IN :jids").bindparams(
                    sa.bindparam("jids", expanding=True)
                ),
                {"jids": job_id_list},
            )
        conn.execute(
            sa.text("DELETE FROM job_postings WHERE id IN :jids").bindparams(
                sa.bindparam("jids", expanding=True)
            ),
            {"jids": job_id_list},
        )

    conn.execute(
        sa.text(
            "DELETE FROM employers WHERE COALESCE(is_fictional_test, 0) = 1 "
            "AND id NOT IN (SELECT DISTINCT employer_id FROM job_postings WHERE employer_id IS NOT NULL)"
        )
    )
    conn.execute(sa.text("DELETE FROM source_connectors WHERE name = 'fictional_ats'"))

    # 4. Collect resolved QuestionRequest answers before dropping table
    resolved_questions = [
        dict(row._mapping)
        for row in conn.execute(
            sa.text(
                "SELECT application_id, field_key, user_answer "
                "FROM question_requests "
                "WHERE resolved = 1 AND user_answer IS NOT NULL "
                "ORDER BY COALESCE(resolved_at, created_at) ASC, id ASC"
            )
        )
    ]
    answers_by_app: dict[str, dict[str, str]] = defaultdict(dict)
    for qr in resolved_questions:
        ans = str(qr["user_answer"])
        if ans:
            answers_by_app[str(qr["application_id"])][str(qr["field_key"])] = ans

    # 5. Update resume_versions (add nullable profile_revision and job_description_hash)
    with op.batch_alter_table("resume_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("profile_revision", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("job_description_hash", sa.String(length=64), nullable=True))

    # 6. Migrate applications: add answers_json & last_step_json, copy answers, remap status, then drop old columns
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("answers_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("last_step_json", sa.JSON(), nullable=True))

    app_rows = [
        dict(row._mapping)
        for row in conn.execute(sa.text("SELECT id, status FROM applications"))
    ]
    apps_tbl = sa.table(
        "applications",
        sa.column("id", sa.String(length=36)),
        sa.column("status", sa.String(length=32)),
        sa.column("answers_json", sa.JSON()),
        sa.column("last_step_json", sa.JSON()),
    )
    for app_row in app_rows:
        app_id = str(app_row["id"])
        old_status = str(app_row["status"] or "")
        new_status = _APP_STATUS_MAP.get(old_status, "ready")
        app_answers = answers_by_app.get(app_id, {})
        conn.execute(
            apps_tbl.update()
            .where(apps_tbl.c.id == app_id)
            .values(status=new_status, answers_json=app_answers, last_step_json=None)
        )

    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.alter_column("answers_json", existing_type=sa.JSON(), nullable=False)
        batch_op.drop_column("version")
        batch_op.drop_column("answer_version_id")
        batch_op.drop_column("confirmation_receipt_id")
        batch_op.drop_column("confirmation_url")
        batch_op.drop_column("confirmed_at")

    # 7. Rebuild user_settings without dry_run, live_mode_enabled, and scheduler settings
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("dry_run")
        batch_op.drop_column("live_mode_enabled")
        batch_op.drop_column("scheduler_enabled")
        batch_op.drop_column("scheduler_interval_hours")

    # 8. Drop retired child tables before parents
    for table_name in (
        "browser_attempts",
        "approval_decisions",
        "application_attempts",
        "approval_requests",
        "question_requests",
        "answer_versions",
        "match_evaluations",
        "fact_evidence",
        "profile_facts",
    ):
        op.drop_table(table_name)

    if is_sqlite:
        fk_violations = conn.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
        if fk_violations:
            raise RuntimeError(f"SQLite foreign_key_check failed after migration: {fk_violations}")
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrade from Phase 1 migration b1d2e3f4a501 is unsupported. "
        "Stop CareerPilot and restore the SQLite backup created before upgrading "
        "(see <data_dir>/backups/)."
    )
