"""Explicitly reviewed reusable Answer Arsenal: CRUD, seeding, legacy migration, and matching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.browser_automation.driver import (
    StepSnapshot,
    build_question_signature,
    classify_field_category,
    normalize_label_text,
)
from backend.app.core.llm import LlmGateway, LlmProviderError
from backend.app.core.time import utc_rfc3339, utf16_len
from backend.app.models.tables import ArsenalRespostaRow, AuditEvent, MasterProfile
from backend.app.schemas.profile import (
    Archetype,
    ArsenalCategory,
    ArsenalPayload,
    ArsenalSlot,
    LengthVariants,
    Locale,
    MasterProfileDocumentV2,
    NonEmptyId,
    RoleTag,
    RouteClause,
    Routing,
    StrictModel,
    StrictRevision,
    _validate_slot_placeholders,
    new_item_id,
)
from backend.app.security.prompt_guard import sanitize_untrusted_text
from backend.app.security.vault import (
    SENSITIVE_FACT_CATEGORIES,
    SensitivityLevel,
    classify_field_sensitivity,
)
from backend.app.services.applications.rules import FieldIntent, match_field_rule
from backend.app.services.applications.validator import ALLOWED_SLOTS, interpolate_template
from backend.app.services.discovery.matching import includes
from backend.app.services.profile.routing import JobContext

FACTUAL_ARSENAL_CATEGORIES: frozenset[str] = frozenset(
    {
        "professional_summary",
        "technical_challenge",
        "finops_cloud_impact",
        "availability",
        "work_authorization",
        "english_proficiency",
    }
)

_BASELINE_SEED_TS = "2026-01-01T00:00:00Z"
_DISALLOWED_TOPIC_RE = re.compile(
    r"\b(cpf|rg|cnh|passaporte|passport|senha|password|otp|2fa|token|"
    r"consentimento|privacy|rgpd|lgpd|pretensao salarial|salario|salary)\b",
    re.I,
)


class ArsenalNotFoundError(LookupError):
    """Raised when a requested Arsenal entry or revision does not exist."""


class ArsenalConflictError(RuntimeError):
    """Raised on CAS revision conflict or invalid Arsenal lifecycle transition."""

    def __init__(self, code: str, message: str, *, current: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current


class ArsenalValidationError(ValueError):
    """Raised when Arsenal entry creation/update/approval fails domain validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ArsenalEntryRequest(Routing):
    model_config = ConfigDict(extra="forbid")

    question: str
    control: Literal["text", "textarea"] = "textarea"
    language: Locale = "pt-BR"
    variants: LengthVariants
    role_archetype: Archetype = "all"
    category: ArsenalCategory = "custom"
    slots: list[ArsenalSlot] = Field(default_factory=list, max_length=3)
    trigger_keywords: list[str] = Field(default_factory=list, max_length=32)
    question_signatures: list[str] = Field(default_factory=list, max_length=32)
    source_claim_ids: list[NonEmptyId] = Field(default_factory=list, max_length=32)

    def payload(self, *, canonical_claim_ids: set[str] | None = None) -> ArsenalPayload:
        question = self.question.strip()
        if not question or len(question) > 140:
            raise ArsenalValidationError(
                "invalid_arsenal_entry",
                "Informe a pergunta exata (1 a 140 caracteres) para o Arsenal.",
            )
        cat = classify_field_category(question, "textarea")
        is_allowed_cat_question = (
            cat in {"custom_question", "contact_postal_code"}
            or (cat == "availability" and self.category == "availability")
            or (cat == "work_auth" and self.category == "work_authorization")
        )
        # Salary, sensitive IDs, credentials, legal consents never become fillable Arsenal answers
        if (
            self.category != "salary_note"
            and (
                not is_allowed_cat_question
                or classify_field_sensitivity(question) != SensitivityLevel.PUBLIC
                or _DISALLOWED_TOPIC_RE.search(question)
            )
        ):
            raise ArsenalValidationError(
                "disallowed_arsenal_topic",
                "Esta pergunta envolve dados sensíveis, consentimento ou remuneração e requer tratamento na fila manual.",
            )
        if canonical_claim_ids is not None and self.source_claim_ids:
            missing = [cid for cid in self.source_claim_ids if cid not in canonical_claim_ids]
            if missing:
                raise ArsenalValidationError(
                    "unresolved_claim_ref",
                    f"Referência source_claim_ids inexistente no Perfil mestre: {', '.join(missing[:3])}",
                )

        auto_sigs = [build_question_signature(question, self.control, [])]
        extra_triggers: list[str] = []
        if cat == "contact_postal_code":
            auto_sigs.extend(["text:cep", "text:zip code", "text:codigo postal", "text:postal code"])
            extra_triggers.extend(["CEP", "ZIP CODE", "Código Postal", "Postal Code"])
        merged_sigs = list(dict.fromkeys([*self.question_signatures, *auto_sigs]))[:32]
        merged_triggers = [
            kw.strip()
            for kw in dict.fromkeys([question, *self.trigger_keywords, *extra_triggers])
            if kw and kw.strip()
        ][:32]
        return ArsenalPayload(
            length_variants=self.variants,
            trigger_keywords=merged_triggers,
            question_signatures=merged_sigs,
            slots=self.slots,
            role_tags=self.role_tags,
            when_to_use=self.when_to_use,
            when_not_to_use=self.when_not_to_use,
            source_claim_ids=self.source_claim_ids,
            provenance="user",
        )


class ArsenalUpdateRequest(StrictModel):
    expected_revision: StrictRevision
    entry: ArsenalEntryRequest


class ArsenalVersionRequest(StrictModel):
    revision: StrictRevision


class SemanticMatchOutput(StrictModel):
    intent: FieldIntent
    candidate_ids: list[NonEmptyId] = Field(default_factory=list, max_length=3)


class AnswerSelection(StrictModel):
    candidate_id: str | None


# Exact instructional prompts preserved cleanly for comparison before allowing activation.
_INSTRUCTIONAL_VARIANTS_PTBR: dict[ArsenalCategory, tuple[str, str, str, list[str]]] = {
    "professional_summary": (
        "Resuma sua atuação e cite competências comprovadas relevantes para a vaga.",
        "[RASCUNHO-INSTRUÇÃO] Escreva de 2 a 4 frases com sua atuação principal, tecnologias verificadas no perfil e objetivos alinhados à vaga.",
        "[RASCUNHO-INSTRUÇÃO] Redija um resumo profissional completo baseado estritamente nas suas experiências, métricas comprovadas e competências do Perfil mestre.",
        ["resumo profissional", "fale sobre você", "sobre você"],
    ),
    "why_company": (
        "Tenho interesse na vaga de {{role_title}} na {{company_name}}.",
        "Tenho interesse na vaga de {{role_title}} na {{company_name}}, especialmente pela oportunidade de aplicar {{tech_stack_highlight}}.",
        "Tenho interesse na oportunidade de {{role_title}} na {{company_name}} e acredito que minha experiência prática com {{tech_stack_highlight}} pode contribuir diretamente para os resultados da equipe.",
        ["por que quer trabalhar nesta empresa", "por que esta empresa", "motivação"],
    ),
    "technical_challenge": (
        "Descreva o desafio, sua contribuição e o resultado comprovado.",
        "[RASCUNHO-INSTRUÇÃO] Detalhe um desafio técnico real (contexto, ação sua e métrica comprovada no Perfil mestre).",
        "[RASCUNHO-INSTRUÇÃO] Estruture em Situação, Tarefa, Ação e Resultado com tecnologias e métricas verificáveis do seu Perfil mestre.",
        ["maior desafio técnico", "desafio técnico", "contribuição técnica"],
    ),
    "finops_cloud_impact": (
        "Descreva um resultado comprovado de FinOps e como ele foi medido.",
        "[RASCUNHO-INSTRUÇÃO] Informe uma entrega comprovada de eficiência em nuvem ou redução de custos e como a melhoria foi mensurada.",
        "[RASCUNHO-INSTRUÇÃO] Detalhe arquitetura cloud, otimização aplicada, ferramenta de observabilidade/custo e impacto percentual comprovado.",
        ["resultado com finops", "finops", "otimização cloud", "redução de custos"],
    ),
    "availability": (
        "Informe sua disponibilidade atual e a data de início, se confirmada.",
        "[RASCUNHO-INSTRUÇÃO] Confirme seu prazo de aviso prévio ou disponibilidade imediata de acordo com o Perfil mestre.",
        "[RASCUNHO-INSTRUÇÃO] Detalhe sua disponibilidade para início, formato de contratação aceito e eventuais restrições de agenda.",
        ["qual é sua disponibilidade", "disponibilidade de início", "aviso prévio"],
    ),
    "work_authorization": (
        "Informe apenas a autorização de trabalho que você confirmou para este país.",
        "[RASCUNHO-INSTRUÇÃO] Declare somente sua cidadania ou permissão de trabalho confirmada para a jurisdição da vaga.",
        "[RASCUNHO-INSTRUÇÃO] Confirme sua elegibilidade trabalhista sem suposições sobre patrocínio ou vistos não verificados.",
        ["autorização para trabalhar", "autorização de trabalho", "visto"],
    ),
    "english_proficiency": (
        "Informe seu nível de inglês conforme o perfil revisado.",
        "[RASCUNHO-INSTRUÇÃO] Descreva seu nível de leitura, escrita e conversação em inglês conforme revisado no Perfil mestre.",
        "[RASCUNHO-INSTRUÇÃO] Informe sua proficiência em inglês e experiências práticas de uso no trabalho, quando comprovadas.",
        ["nível de inglês", "fluência em inglês", "english level"],
    ),
    "salary_note": (
        "Revise a faixa, a moeda e o regime antes de confirmar a pretensão salarial.",
        "[SOMENTE REFERÊNCIA VISUAL] Nunca ative como resposta automática; confira contrato (CLT/PJ/Estágio), moeda e periodicidade antes de aprovar.",
        "[SOMENTE REFERÊNCIA VISUAL] Uso exclusivo como guia na fila de revisão salarial manual. Valores dependem de aprovação humana por vaga.",
        ["nota salarial", "pretensão salarial"],
    ),
}

_INSTRUCTIONAL_VARIANTS_EN: dict[ArsenalCategory, tuple[str, str, str, list[str]]] = {
    "professional_summary": (
        "Summarize your background and cite verified skills relevant to this role.",
        "[DRAFT-INSTRUCTION] Write 2 to 4 sentences covering your core experience, verified stack, and goals for this role.",
        "[DRAFT-INSTRUCTION] Draft a complete professional summary strictly grounded in verified roles and outcomes from your Master Profile.",
        ["professional summary", "about yourself", "summary"],
    ),
    "why_company": (
        "I am interested in the {{role_title}} role at {{company_name}}.",
        "I am interested in the {{role_title}} position at {{company_name}}, particularly to apply my verified background in {{tech_stack_highlight}}.",
        "I am applying for the {{role_title}} opportunity at {{company_name}} and look forward to contributing with my practical experience in {{tech_stack_highlight}}.",
        ["why do you want to work here", "why this company", "motivation"],
    ),
    "technical_challenge": (
        "Describe the technical challenge, your contribution, and the verified outcome.",
        "[DRAFT-INSTRUCTION] Detail a real engineering challenge, your ownership, and the measurable result.",
        "[DRAFT-INSTRUCTION] Structure context, technical trade-offs, implementation, and verified impact from your profile.",
        ["biggest technical challenge", "technical challenge", "technical contribution"],
    ),
    "finops_cloud_impact": (
        "Describe a verified FinOps outcome and how it was measured.",
        "[DRAFT-INSTRUCTION] Share a verified cloud cost optimization initiative and the metric used to measure savings.",
        "[DRAFT-INSTRUCTION] Detail the cloud workload, optimization mechanism, monitoring approach, and proven percentage or cost impact.",
        ["finops impact", "cloud optimization", "cost reduction"],
    ),
    "availability": (
        "State your current availability and start date once confirmed.",
        "[DRAFT-INSTRUCTION] Confirm notice period or immediate availability strictly per your reviewed preferences.",
        "[DRAFT-INSTRUCTION] Provide verified notice period and start-date readiness for the target schedule.",
        ["availability", "notice period", "start date"],
    ),
    "work_authorization": (
        "State only the work authorization you have confirmed for this country.",
        "[DRAFT-INSTRUCTION] Confirm citizenship or legal work permit status accurately without assuming sponsorship.",
        "[DRAFT-INSTRUCTION] State verified jurisdiction eligibility matching the role location and contract requirements.",
        ["work authorization", "visa sponsorship", "authorized to work"],
    ),
    "english_proficiency": (
        "State your English proficiency according to your reviewed profile.",
        "[DRAFT-INSTRUCTION] State your verified English reading, writing, and speaking level from your profile.",
        "[DRAFT-INSTRUCTION] Provide verified English proficiency and practical workplace communication context if applicable.",
        ["english proficiency", "english level"],
    ),
    "salary_note": (
        "Review the salary band, currency, and contract type before confirming expectations.",
        "[DISPLAY-ONLY REFERENCE] Never activate for auto-fill; verify CLT/PJ/Intern terms, currency, and pay period manually.",
        "[DISPLAY-ONLY REFERENCE] Guidance note for the manual salary approval queue only. Never automated.",
        ["salary note", "expected compensation"],
    ),
}

ARSENAL_EXAMPLES: tuple[tuple[str, str, str], ...] = tuple(
    (cat, meta[3][0].capitalize() + ("?" if cat != "professional_summary" else ""), meta[0])
    for cat, meta in _INSTRUCTIONAL_VARIANTS_PTBR.items()
)

_ALL_INSTRUCTIONAL_LITERALS: frozenset[str] = frozenset(
    txt
    for table in (_INSTRUCTIONAL_VARIANTS_PTBR, _INSTRUCTIONAL_VARIANTS_EN)
    for cat, (short_t, med_t, long_t, _) in table.items()
    if cat != "why_company"
    for txt in (short_t, med_t, long_t)
)


def is_instructional_variant_text(text: str) -> bool:
    """Return True if a variant is still an unedited editorial instruction or display-only marker."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if cleaned.startswith(("[RASCUNHO-INSTRUÇÃO]", "[DRAFT-INSTRUCTION]", "[SOMENTE REFERÊNCIA", "[DISPLAY-ONLY")):
        return True
    return cleaned in _ALL_INSTRUCTIONAL_LITERALS


def is_instructional_draft_payload(payload: ArsenalPayload) -> bool:
    """Return True if any filled variant in an Arsenal payload contains unedited instructional copy."""
    return any(
        is_instructional_variant_text(v)
        for v in (
            payload.length_variants.short,
            payload.length_variants.medium,
            payload.length_variants.long,
        )
        if v
    )


def _canonical_claim_ids_from_db(db: Session, user_id: str) -> set[str]:
    row = db.scalar(select(MasterProfile).where(MasterProfile.user_id == user_id))
    if not row or not isinstance(row.document, dict):
        return set()
    try:
        v2 = MasterProfileDocumentV2.model_validate(row.document)
    except Exception:
        return set()
    ids: set[str] = set()
    for group in (v2.experience, v2.education, v2.projects, v2.skills, v2.languages, v2.imports):
        for item in group:
            ids.add(item.id)
    for exp in v2.experience:
        for b in exp.bullets:
            ids.add(b.id)
    for proj in v2.projects:
        for pb in proj.bullets:
            ids.add(pb.id)
    return ids


def serialize_arsenal_row(row: ArsenalRespostaRow) -> dict[str, Any]:
    """Convert ORM row into typed API response dict preserving compatibility fields."""
    payload = row.payload_json or {}
    triggers = list(payload.get("trigger_keywords") or [])
    variants = dict(payload.get("length_variants") or {})
    parsed_payload: ArsenalPayload | None = None
    instructional = False
    try:
        parsed_payload = ArsenalPayload.model_validate(payload)
        instructional = is_instructional_draft_payload(parsed_payload)
    except Exception:
        instructional = False
    return {
        "id": row.id,
        "revision": int(row.revision),
        "status": row.status,
        "language": row.language,
        "role_archetype": row.role_archetype,
        "category": row.category,
        "question": payload.get("question") or (triggers[0] if triggers else row.category),
        "variants": variants,
        "length_variants": variants,
        "trigger_keywords": triggers,
        "question_signatures": list(payload.get("question_signatures") or []),
        "slots": list(payload.get("slots") or []),
        "role_tags": list(payload.get("role_tags") or []),
        "when_to_use": list(payload.get("when_to_use") or []),
        "when_not_to_use": list(payload.get("when_not_to_use") or []),
        "source_claim_ids": list(payload.get("source_claim_ids") or []),
        "provenance": str(payload.get("provenance") or "user"),
        "display_only": row.category == "salary_note",
        "is_instructional_draft": instructional,
        "approved_at": row.approved_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _seed_specs() -> list[tuple[str, Locale, ArsenalCategory, str, str, str, list[str], list[ArsenalSlot]]]:
    specs: list[tuple[str, Locale, ArsenalCategory, str, str, str, list[str], list[ArsenalSlot]]] = []
    seed_sources: tuple[tuple[Locale, dict[ArsenalCategory, tuple[str, str, str, list[str]]]], ...] = (
        ("pt-BR", _INSTRUCTIONAL_VARIANTS_PTBR),
        ("en", _INSTRUCTIONAL_VARIANTS_EN),
    )
    for locale, table in seed_sources:
        tag = "ptbr" if locale == "pt-BR" else "en"
        for category, (short_t, med_t, long_t, triggers) in table.items():
            slots: list[ArsenalSlot] = (
                ["company_name", "role_title", "tech_stack_highlight"]
                if category == "why_company"
                else []
            )
            stable_id = f"seed-{tag}-{category.replace('_', '-')}"
            specs.append((stable_id, locale, category, short_t, med_t, long_t, triggers, slots))
    return specs


def ensure_arsenal_seeded(db: Session, user_id: str) -> int:
    """Idempotently ensure localized seed Arsenal drafts exist for all 8 categories in pt-BR and en."""
    existing_ids = set(
        db.scalars(
            select(ArsenalRespostaRow.id).where(ArsenalRespostaRow.user_id == user_id)
        ).all()
    )
    inserted = 0
    for stable_id, locale, category, short_t, med_t, long_t, triggers, slots in _seed_specs():
        if stable_id in existing_ids:
            continue
        used_in_short = re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", short_t)
        assert all(s in ALLOWED_SLOTS for s in used_in_short)
        payload = ArsenalPayload(
            length_variants=LengthVariants(short=short_t, medium=med_t, long=long_t),
            trigger_keywords=triggers,
            question_signatures=[],
            slots=slots,
            role_tags=[],
            when_to_use=[],
            when_not_to_use=[],
            source_claim_ids=[],
            provenance="seed",
        )
        db.add(
            ArsenalRespostaRow(
                user_id=user_id,
                id=stable_id,
                revision=1,
                status="draft",
                language=locale,
                role_archetype="all",
                category=category,
                payload_json=payload.model_dump(mode="json"),
                approved_at=None,
                created_at=_BASELINE_SEED_TS,
                updated_at=_BASELINE_SEED_TS,
            )
        )
        inserted += 1
    if inserted:
        db.flush()
    return inserted


def migrate_legacy_standard_answers_to_arsenal(db: Session, user_id: str) -> int:
    """Migrate eligible non-salary, non-sensitive legacy standard_answers into draft Arsenal rows."""
    row = db.scalar(select(MasterProfile).where(MasterProfile.user_id == user_id))
    if not row or not isinstance(row.document, dict):
        return 0
    raw_answers = list(row.document.get("standard_answers") or [])
    if not raw_answers:
        return 0
    canonical_ids = _canonical_claim_ids_from_db(db, user_id)
    existing_ids = set(
        db.scalars(
            select(ArsenalRespostaRow.id).where(ArsenalRespostaRow.user_id == user_id)
        ).all()
    )
    migrated = 0
    now = utc_rfc3339()
    seen_sigs: set[str] = set()
    for item in raw_answers:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        answer = str(item.get("answer") or "").strip()
        kind = str(item.get("kind") or "text").strip().lower()
        if not label or not answer or kind == "salary":
            continue
        cat = classify_field_category(label, "textarea")
        if cat in {"salary", "sensitive_id", "legal_consent", "resume_file", "non_resume_file"}:
            continue
        if classify_field_sensitivity(label) != SensitivityLevel.PUBLIC or _DISALLOWED_TOPIC_RE.search(label):
            continue
        sig = str(item.get("question_signature") or build_question_signature(label, "textarea", []))[:230]
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        stable_id = "legacy-std-" + hashlib.sha256(f"{user_id}:{sig}".encode()).hexdigest()[:12]
        if stable_id in existing_ids:
            continue
        ans_u16 = len(answer.encode("utf-16-le")) // 2
        if ans_u16 <= 150 and "\n" not in answer and "\r" not in answer:
            lv = LengthVariants(short=answer)
        elif ans_u16 <= 500:
            lv = LengthVariants(medium=answer)
        elif ans_u16 <= 1500:
            lv = LengthVariants(long=answer)
        else:
            continue
        raw_claims = [str(c) for c in (item.get("source_claim_ids") or []) if str(c) in canonical_ids][:32]
        lang: Locale = item.get("language") if item.get("language") in {"pt-BR", "en"} else "pt-BR"  # type: ignore[assignment]
        mapped_category: ArsenalCategory = (
            "availability"
            if cat == "availability"
            else ("work_authorization" if cat == "work_auth" else "custom")
        )
        try:
            payload = ArsenalPayload(
                length_variants=lv,
                trigger_keywords=[label[:120]],
                question_signatures=[sig],
                slots=[],
                role_tags=list(item.get("role_tags") or [])[:16],
                when_to_use=list(item.get("when_to_use") or [])[:16],
                when_not_to_use=list(item.get("when_not_to_use") or [])[:16],
                source_claim_ids=raw_claims,
                provenance="legacy",
            )
        except Exception:
            continue
        db.add(
            ArsenalRespostaRow(
                user_id=user_id,
                id=stable_id,
                revision=1,
                status="draft",
                language=lang,
                role_archetype="all",
                category=mapped_category,
                payload_json=payload.model_dump(mode="json"),
                approved_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        existing_ids.add(stable_id)
        migrated += 1
    if migrated:
        db.flush()
    return migrated


def _encode_arsenal_cursor(updated_at: str, entry_id: str, revision: int) -> str:
    return f"{updated_at}|{entry_id}|{revision}"


def _decode_arsenal_cursor(cursor: str | None) -> tuple[str, str, int] | None:
    if not cursor:
        return None
    parts = cursor.split("|", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        raise ArsenalValidationError("invalid_cursor", "Cursor de paginação inválido.")
    return parts[0], parts[1], int(parts[2])


def list_arsenal_entries(
    db: Session,
    user_id: str,
    *,
    language: Locale | None = None,
    category: ArsenalCategory | None = None,
    status: Literal["draft", "approved", "retired", "all"] | None = None,
    cursor: str | None = None,
    limit: int = 50,
    auto_seed: bool = True,
) -> dict[str, Any]:
    """List Arsenal entries ordered stably (updated_at DESC, id DESC, revision DESC) with cursor pagination."""
    if auto_seed:
        ensure_arsenal_seeded(db, user_id)
        db.commit()

    stmt = select(ArsenalRespostaRow).where(ArsenalRespostaRow.user_id == user_id)
    if status is None:
        stmt = stmt.where(ArsenalRespostaRow.status != "retired")
    elif status != "all":
        stmt = stmt.where(ArsenalRespostaRow.status == status)
    if language is not None:
        stmt = stmt.where(ArsenalRespostaRow.language == language)
    if category is not None:
        stmt = stmt.where(ArsenalRespostaRow.category == category)

    decoded = _decode_arsenal_cursor(cursor)
    if decoded is not None:
        c_upd, c_id, c_rev = decoded
        from sqlalchemy import and_, or_

        stmt = stmt.where(
            or_(
                ArsenalRespostaRow.updated_at < c_upd,
                and_(ArsenalRespostaRow.updated_at == c_upd, ArsenalRespostaRow.id < c_id),
                and_(
                    ArsenalRespostaRow.updated_at == c_upd,
                    ArsenalRespostaRow.id == c_id,
                    ArsenalRespostaRow.revision < c_rev,
                ),
            )
        )

    rows = db.scalars(
        stmt.order_by(
            ArsenalRespostaRow.updated_at.desc(),
            ArsenalRespostaRow.id.desc(),
            ArsenalRespostaRow.revision.desc(),
        ).limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        _encode_arsenal_cursor(page_rows[-1].updated_at, page_rows[-1].id, page_rows[-1].revision)
        if has_more and page_rows
        else None
    )
    return {
        "items": [serialize_arsenal_row(r) for r in page_rows],
        "next_cursor": next_cursor,
        "examples": [
            {"category": cat, "question": question, "text": answer}
            for cat, question, answer in ARSENAL_EXAMPLES
        ],
    }


def create_arsenal_entry(
    db: Session,
    user_id: str,
    body: ArsenalEntryRequest,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Validate and insert a new revision-1 draft entry into arsenal_respostas."""
    canonical_ids = _canonical_claim_ids_from_db(db, user_id)
    payload = body.payload(canonical_claim_ids=canonical_ids)
    now = utc_rfc3339()
    entry = ArsenalRespostaRow(
        id=new_item_id("ars"),
        user_id=user_id,
        revision=1,
        status="draft",
        language=body.language,
        category=body.category,
        role_archetype=body.role_archetype,
        payload_json=payload.model_dump(mode="json"),
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(entry)
    db.add(
        AuditEvent(
            actor=user_id,
            event_type="arsenal.created",
            entity_type="arsenal_respostas",
            entity_id=entry.id,
            details={"revision": 1, "category": body.category, "language": body.language},
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return {"id": entry.id, "revision": 1, "status": "draft", "item": serialize_arsenal_row(entry)}


def update_arsenal_entry(
    db: Session,
    user_id: str,
    entry_id: str,
    req: ArsenalUpdateRequest,
) -> dict[str, Any]:
    """Create a new draft revision (expected_revision + 1) using strict CAS on latest revision."""
    rows = db.scalars(
        select(ArsenalRespostaRow)
        .where(ArsenalRespostaRow.user_id == user_id, ArsenalRespostaRow.id == entry_id)
        .order_by(ArsenalRespostaRow.revision.desc())
    ).all()
    if not rows:
        raise ArsenalNotFoundError("Entrada do Arsenal não encontrada.")
    latest = rows[0]
    if int(latest.revision) != req.expected_revision:
        raise ArsenalConflictError(
            "stale_arsenal_revision",
            "A resposta do Arsenal foi alterada. Atualize a lista antes de editar.",
            current=serialize_arsenal_row(latest),
        )

    canonical_ids = _canonical_claim_ids_from_db(db, user_id)
    payload = req.entry.payload(canonical_claim_ids=canonical_ids)
    now = utc_rfc3339()
    next_rev = req.expected_revision + 1
    new_row = ArsenalRespostaRow(
        user_id=user_id,
        id=entry_id,
        revision=next_rev,
        status="draft",
        language=req.entry.language,
        role_archetype=req.entry.role_archetype,
        category=req.entry.category,
        payload_json=payload.model_dump(mode="json"),
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(new_row)
    db.add(
        AuditEvent(
            actor=user_id,
            event_type="arsenal.revised",
            entity_type="arsenal_respostas",
            entity_id=entry_id,
            details={"revision": next_rev, "category": req.entry.category},
        )
    )
    db.commit()
    return {"id": entry_id, "revision": next_rev, "status": "draft", "item": serialize_arsenal_row(new_row)}


def approve_arsenal_entry(
    db: Session,
    user_id: str,
    entry_id: str,
    revision: int,
) -> dict[str, Any]:
    """Validate safety/claim/non-instructional rules and atomically promote draft revision to approved."""
    target = db.get(ArsenalRespostaRow, (user_id, entry_id, revision))
    if target is None:
        raise ArsenalNotFoundError("Entrada do Arsenal não encontrada.")
    if target.status != "draft":
        raise ArsenalConflictError(
            "stale_arsenal_revision",
            "A resposta mudou e não está mais em rascunho. Atualize o Arsenal.",
            current=serialize_arsenal_row(target),
        )
    if target.category == "salary_note":
        raise ArsenalValidationError(
            "salary_note_display_only",
            "Notas salariais são apenas de referência visual e não podem ser aprovadas para envio automático.",
        )

    payload = ArsenalPayload.model_validate(target.payload_json)
    if is_instructional_draft_payload(payload):
        raise ArsenalValidationError(
            "instructional_draft_requires_replacement",
            "Substitua o texto de instrução por sua resposta real e revisada antes de aprovar.",
        )

    canonical_ids = _canonical_claim_ids_from_db(db, user_id)
    if payload.source_claim_ids:
        missing = [cid for cid in payload.source_claim_ids if cid not in canonical_ids]
        if missing:
            raise ArsenalValidationError(
                "unresolved_claim_ref",
                f"Referências não encontradas no Perfil mestre atual: {', '.join(missing[:3])}",
            )
    elif payload.provenance in {"legacy", "cloud"} and target.category in FACTUAL_ARSENAL_CATEGORIES:
        raise ArsenalValidationError(
            "factual_confirmation_required",
            "Entradas factuais importadas/sugeridas exigem source_claim_ids válidos ou edição confirmada pelo usuário.",
        )

    now = utc_rfc3339()
    # Atomically retire prior approved revision(s) for same entry ID in the same transaction
    db.execute(
        update(ArsenalRespostaRow)
        .where(
            ArsenalRespostaRow.user_id == user_id,
            ArsenalRespostaRow.id == entry_id,
            ArsenalRespostaRow.status == "approved",
        )
        .values(status="retired", approved_at=None, updated_at=now)
    )
    changed = db.execute(
        update(ArsenalRespostaRow)
        .where(
            ArsenalRespostaRow.user_id == user_id,
            ArsenalRespostaRow.id == entry_id,
            ArsenalRespostaRow.revision == revision,
            ArsenalRespostaRow.status == "draft",
        )
        .values(status="approved", approved_at=now, updated_at=now)
        .returning(ArsenalRespostaRow.revision)
    ).scalar_one_or_none()
    if changed is None:
        db.rollback()
        raise ArsenalConflictError(
            "stale_arsenal_revision",
            "A resposta mudou durante a aprovação. Atualize o Arsenal.",
        )
    db.add(
        AuditEvent(
            actor=user_id,
            event_type="arsenal.approved",
            entity_type="arsenal_respostas",
            entity_id=entry_id,
            details={"revision": revision, "category": target.category},
        )
    )
    db.commit()
    refreshed = db.get(ArsenalRespostaRow, (user_id, entry_id, revision))
    assert refreshed is not None
    return {"status": "approved", "id": entry_id, "revision": revision, "item": serialize_arsenal_row(refreshed)}


def retire_arsenal_entry(
    db: Session,
    user_id: str,
    entry_id: str,
    revision: int,
) -> dict[str, Any]:
    """Atomically retire a draft or active approved revision from future matching."""
    existing = db.get(ArsenalRespostaRow, (user_id, entry_id, revision))
    if existing is None:
        raise ArsenalNotFoundError("Entrada do Arsenal não encontrada.")
    now = utc_rfc3339()
    changed = db.execute(
        update(ArsenalRespostaRow)
        .where(
            ArsenalRespostaRow.user_id == user_id,
            ArsenalRespostaRow.id == entry_id,
            ArsenalRespostaRow.revision == revision,
            ArsenalRespostaRow.status.in_(("draft", "approved")),
        )
        .values(status="retired", approved_at=None, updated_at=now)
        .returning(ArsenalRespostaRow.revision)
    ).scalar_one_or_none()
    if changed is None:
        raise ArsenalConflictError(
            "stale_arsenal_revision",
            "A resposta já mudou de estado. Atualize o Arsenal.",
            current=serialize_arsenal_row(existing),
        )
    db.add(
        AuditEvent(
            actor=user_id,
            event_type="arsenal.retired",
            entity_type="arsenal_respostas",
            entity_id=entry_id,
            details={"revision": revision},
        )
    )
    db.commit()
    return {"status": "retired", "id": entry_id, "revision": revision}


@dataclass(frozen=True, slots=True)
class ArsenalMatchCandidate:
    """Authorized approved Arsenal candidate pinned at its reviewed revision."""

    entry_id: str
    revision: int
    category: ArsenalCategory
    language: Locale
    role_archetype: Archetype
    payload: ArsenalPayload
    score: float
    trigger_coverage: float
    category_match: float
    role_match: float
    rule_id: str
    intent: FieldIntent
    exact_signature: bool
    source_claim_ids: tuple[str, ...]
    diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _EligibleEntry:
    row: ArsenalRespostaRow
    payload: ArsenalPayload
    triggers: list[str]
    rendered_variants: LengthVariants


def _render_variants_preserving_slots(
    variants: LengthVariants,
    slot_values: Mapping[str, str],
    *,
    entry_locale: Locale,
    expected_locale: Locale,
) -> LengthVariants | None:
    out_vals: dict[str, str] = {}
    for tier in ("long", "medium", "short"):
        raw = getattr(variants, tier)
        if not raw:
            continue
        ok, rendered, _ = interpolate_template(
            raw,
            slot_values,
            entry_locale=entry_locale,
            expected_locale=expected_locale,
        )
        if ok and rendered:
            out_vals[tier] = rendered
    if not out_vals:
        return None
    return LengthVariants(**out_vals)


async def match_arsenal_for_step(
    db: Session,
    user_id: str,
    snapshot: StepSnapshot,
    context: JobContext,
    values: Mapping[str, str],
    gateway: LlmGateway | None = None,
) -> tuple[dict[str, ArsenalMatchCandidate], list[dict[str, Any]]]:
    """Match active approved Arsenal entries against editable form controls with 0.60/0.25/0.15 rubric."""
    rows = db.scalars(
        select(ArsenalRespostaRow).where(
            ArsenalRespostaRow.user_id == user_id,
            ArsenalRespostaRow.status == "approved",
            ArsenalRespostaRow.category != "salary_note",
            ArsenalRespostaRow.language == context.locale,
            ArsenalRespostaRow.role_archetype.in_(("all", context.archetype)),
        )
    ).all()

    eligible: list[_EligibleEntry] = []
    exact_by_sig: dict[str, list[_EligibleEntry]] = {}
    custom_sigs = {
        f.question_signature
        for f in snapshot.fields
        if f.editable and not f.has_value and f.control_kind in {"text", "textarea"}
    }

    for row in rows:
        try:
            payload = ArsenalPayload.model_validate(row.payload_json)
        except Exception:
            continue
        if is_instructional_draft_payload(payload):
            continue
        if not context.allows(payload.when_to_use, payload.when_not_to_use):
            continue
        rendered_lv = _render_variants_preserving_slots(
            payload.length_variants,
            values,
            entry_locale=row.language,  # type: ignore[arg-type]
            expected_locale=context.locale,
        )
        if rendered_lv is None:
            continue
        triggers = sorted(
            {
                normalize_label_text(word)
                for word in payload.trigger_keywords
                if normalize_label_text(word)
            }
        )
        if len(payload.trigger_keywords) > 1 and payload.provenance == "user":
            primary = normalize_label_text(payload.trigger_keywords[0])
            filtered = [t for t in triggers if t != primary]
            if filtered:
                triggers = filtered
        item = _EligibleEntry(row=row, payload=payload, triggers=triggers, rendered_variants=rendered_lv)
        eligible.append(item)
        for sig in custom_sigs.intersection(payload.question_signatures):
            exact_by_sig.setdefault(sig, []).append(item)

    db.rollback()

    matched: dict[str, ArsenalMatchCandidate] = {}
    diagnostics: list[dict[str, Any]] = []

    # 1. Exact vetted signature matches (score = 1.00); ambiguous duplicates excluded cleanly
    ambiguous_sigs: set[str] = set()
    for sig, entries in exact_by_sig.items():
        if len(entries) != 1:
            ambiguous_sigs.add(sig)
            diagnostics.append(
                {
                    "question_signature": sig,
                    "decision": "manual_ambiguous_exact_signature",
                    "candidate_ids": [e.row.id for e in entries],
                }
            )
            continue
        ent = entries[0]
        matching_field = next((f for f in snapshot.fields if f.question_signature == sig), None)
        rule = match_field_rule(
            matching_field.label if matching_field else sig,
            matching_field.control_kind if matching_field else "textarea",
            category_hint=matching_field.category if matching_field else None,
        )
        if rule.requires_manual:
            continue
        role_match_val: float = 1.0 if ent.row.role_archetype == context.archetype else 0.5
        diag: dict[str, Any] = {
            "entry_id": ent.row.id,
            "revision": int(ent.row.revision),
            "rule_id": rule.rule_id,
            "intent": rule.semantic_intent,
            "score": 1.0,
            "trigger_coverage": 1.0,
            "category_match": 1.0,
            "role_match": role_match_val,
            "exact_signature": True,
            "mode": "exact_signature",
        }
        matched[sig] = ArsenalMatchCandidate(
            entry_id=ent.row.id,
            revision=int(ent.row.revision),
            category=ent.row.category,  # type: ignore[arg-type]
            language=ent.row.language,  # type: ignore[arg-type]
            role_archetype=ent.row.role_archetype,  # type: ignore[arg-type]
            payload=ent.payload,
            score=1.0,
            trigger_coverage=1.0,
            category_match=1.0,
            role_match=role_match_val,
            rule_id=rule.rule_id,
            intent=rule.semantic_intent,
            exact_signature=True,
            source_claim_ids=tuple(ent.payload.source_claim_ids),
            diagnostics=diag,
        )
        diagnostics.append(diag)

    if not eligible:
        return matched, diagnostics

    # 2. Deterministic lexical + optional bounded local Ollama semantic matching for unresolved fields
    unresolved_fields = [
        f
        for f in snapshot.fields
        if f.editable
        and not f.has_value
        and f.control_kind in {"text", "textarea"}
        and f.question_signature not in matched
        and f.question_signature not in ambiguous_sigs
    ][:8]

    for field in unresolved_fields:
        rule = match_field_rule(field.label, field.control_kind, category_hint=field.category)
        if rule.requires_manual:
            continue
        question = sanitize_untrusted_text(field.label, max_len=140)
        if question.injection_signals:
            diagnostics.append({"question_signature": field.question_signature, "decision": "blocked_injection"})
            continue
        label_norm = normalize_label_text(question.text)
        if re.search(r"\b(cpf|senha|password|consentimento|lgpd|privacy|bank|bancario|conta bancaria|salario|salarial|salary)\b", label_norm):
            continue

        expected_category = rule.arsenal_category if rule.arsenal_category != "custom" else None
        scored: list[tuple[float, float, float, float, _EligibleEntry]] = []
        for ent in eligible:
            hits = sum(1 for trigger in ent.triggers if includes(label_norm, trigger))
            trigger_cov = min(1.0, hits / min(3, len(ent.triggers))) if ent.triggers and hits > 0 else 0.0
            cat_match = 1.0 if (expected_category is not None and ent.row.category == expected_category) else 0.0
            role_match = 1.0 if ent.row.role_archetype == context.archetype else 0.5
            score = round(0.60 * trigger_cov + 0.25 * cat_match + 0.15 * role_match, 6)
            if hits > 0 or cat_match > 0.0:
                scored.append((score, trigger_cov, cat_match, role_match, ent))

        if not scored:
            continue

        scored.sort(key=lambda item: (-item[0], item[4].row.id))
        best_score, best_cov, best_cat, best_role, best_ent = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = round(best_score - second_score, 6) if len(scored) > 1 else 1.0

        offered = scored[:5]
        offered_ids = {item[4].row.id for item in offered}
        ollama_ids: list[str] = []
        ollama_intent: FieldIntent = rule.semantic_intent
        used_model = "deterministic_local"

        if gateway is not None:
            def _validate_candidate_ids(out: SemanticMatchOutput, allowed: set[str] = offered_ids) -> None:
                for cid in out.candidate_ids:
                    if cid not in allowed:
                        raise ValueError(f"ID candidato fora da lista fornecida: {cid}")

            def _offline_semantic(
                intent_val: FieldIntent = rule.semantic_intent,
                top_id: str = best_ent.row.id,
            ) -> SemanticMatchOutput:
                return SemanticMatchOutput(intent=intent_val, candidate_ids=[top_id])

            try:
                async with asyncio.timeout(4):
                    sel = await gateway.complete_structured(
                        None,
                        operation="arsenal_selection",
                        system_prompt=(
                            "Classifique a intenção do campo e ordene até 3 IDs de candidatos elegíveis. "
                            "Nunca gere texto de resposta nem invente IDs fora da lista."
                        ),
                        user_payload={
                            "label": question.text,
                            "heading": sanitize_untrusted_text(snapshot.heading, max_len=120).text,
                            "locale": context.locale,
                            "control": field.control_kind,
                            "candidates": [
                                {
                                    "id": item[4].row.id,
                                    "category": item[4].row.category,
                                    "triggers": item[4].triggers[:8],
                                }
                                for item in offered
                            ],
                        },
                        output_model=SemanticMatchOutput,
                        validate_ids=_validate_candidate_ids,
                        offline_factory=_offline_semantic,
                        includes_candidate_profile=False,
                        max_tokens=96,
                    )
                    used_model = sel.mode
                    ollama_ids = [cid for cid in sel.data.candidate_ids if cid in offered_ids]
                    ollama_intent = sel.data.intent
            except (TimeoutError, LlmProviderError, ValueError):
                # Deterministic fallback: rules & server-computed rubric stay in control
                used_model = "deterministic_fallback"

        # Near-ties and sub-threshold scores ALWAYS route to manual, even if Ollama ranked them!
        if best_score < 0.75:
            diagnostics.append(
                {
                    "question_signature": field.question_signature,
                    "rule_id": rule.rule_id,
                    "decision": "manual_below_threshold",
                    "best_entry_id": best_ent.row.id,
                    "score": best_score,
                    "trigger_coverage": best_cov,
                    "category_match": best_cat,
                    "role_match": best_role,
                    "ollama_candidate_ids": ollama_ids,
                }
            )
            continue
        if len(scored) > 1 and margin < 0.10:
            diagnostics.append(
                {
                    "question_signature": field.question_signature,
                    "rule_id": rule.rule_id,
                    "decision": "manual_near_tie",
                    "best_entry_id": best_ent.row.id,
                    "score": best_score,
                    "margin": margin,
                    "ollama_candidate_ids": ollama_ids,
                }
            )
            continue
        # If live local LLM returned empty list or disagreed with deterministic top candidate,
        # keep deterministic rules/scores authoritative by never letting LLM promote a runner-up.
        if used_model == "llm_structured" and (not ollama_ids or ollama_ids[0] != best_ent.row.id):
            diagnostics.append(
                {
                    "question_signature": field.question_signature,
                    "rule_id": rule.rule_id,
                    "decision": "manual_llm_disagreement",
                    "best_entry_id": best_ent.row.id,
                    "score": best_score,
                    "ollama_candidate_ids": ollama_ids,
                }
            )
            continue

        resolved_intent: FieldIntent = ollama_intent if used_model == "llm_structured" else rule.semantic_intent
        diag = {
            "question_signature": field.question_signature,
            "entry_id": best_ent.row.id,
            "revision": int(best_ent.row.revision),
            "rule_id": rule.rule_id,
            "intent": resolved_intent,
            "score": best_score,
            "trigger_coverage": best_cov,
            "category_match": best_cat,
            "role_match": best_role,
            "margin": margin,
            "exact_signature": False,
            "mode": used_model,
            "ollama_candidate_ids": ollama_ids,
        }
        matched[field.question_signature] = ArsenalMatchCandidate(
            entry_id=best_ent.row.id,
            revision=int(best_ent.row.revision),
            category=best_ent.row.category,  # type: ignore[arg-type]
            language=best_ent.row.language,  # type: ignore[arg-type]
            role_archetype=best_ent.row.role_archetype,  # type: ignore[arg-type]
            payload=best_ent.payload,
            score=best_score,
            trigger_coverage=best_cov,
            category_match=best_cat,
            role_match=best_role,
            rule_id=rule.rule_id,
            intent=resolved_intent,
            exact_signature=False,
            source_claim_ids=tuple(best_ent.payload.source_claim_ids),
            diagnostics=diag,
        )
        diagnostics.append(diag)

    return matched, diagnostics


async def approved_answers(
    db: Session,
    user_id: str,
    snapshot: StepSnapshot,
    context: JobContext,
    values: Mapping[str, str],
    gateway: LlmGateway | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return rendered fitting variant tuples keyed by question_signature for approved Arsenal matches."""
    matched, _ = await match_arsenal_for_step(
        db,
        user_id,
        snapshot,
        context,
        values,
        gateway=gateway,
    )
    out: dict[str, tuple[str, ...]] = {}
    for sig, cand in matched.items():
        rendered_lv = _render_variants_preserving_slots(
            cand.payload.length_variants,
            values,
            entry_locale=cand.language,
            expected_locale=context.locale,
        )
        if rendered_lv is None:
            continue
        ordered = tuple(
            v for v in (rendered_lv.long, rendered_lv.medium, rendered_lv.short) if v
        )
        if ordered:
            out[sig] = ordered
    return out


def dump_arsenal_debug_safe(cand: ArsenalMatchCandidate) -> str:
    """Serialize anonymized rule/answer IDs and scores without any answer text."""
    return json.dumps(cand.diagnostics, ensure_ascii=False)


def _contains_whole_phrase(norm_text: str, norm_phrase: str) -> bool:
    if not norm_text or not norm_phrase:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(norm_phrase)}(?![a-z0-9])", norm_text))


def build_confirmed_answer_arsenal_draft(
    *,
    user_id: str,
    question_label: str,
    question_signature: str,
    control_kind: str,
    answer_text: str,
    language: Locale,
    category: ArsenalCategory,
    role_archetype: Archetype = "all",
    trigger_keywords: list[str] | tuple[str, ...] = (),
    when_to_use: list[RouteClause] | tuple[RouteClause, ...] = (),
    when_not_to_use: list[RouteClause] | tuple[RouteClause, ...] = (),
    role_tags: list[RoleTag] | tuple[RoleTag, ...] = (),
    job_company: str = "",
    job_title: str = "",
) -> ArsenalRespostaRow:
    """Build a draft ArsenalRespostaRow from confirmed manual input without synthesizing longer tiers."""
    if control_kind not in {"text", "textarea"}:
        raise ArsenalValidationError(
            "arsenal_requires_text",
            "Apenas respostas textuais reutilizáveis podem virar rascunho no Arsenal.",
        )

    clean_label = question_label.strip()
    if not clean_label:
        raise ArsenalValidationError("invalid_arsenal_entry", "Rótulo de pergunta ausente para o Arsenal.")

    if category == "salary_note":
        raise ArsenalValidationError(
            "salary_note_display_only",
            "Notas salariais são apenas de referência visual e não podem ser aprendidas de etapas.",
        )

    cat = classify_field_category(clean_label, "textarea")
    if (
        cat in {"salary", "sensitive_id", "legal_consent", "resume_file", "non_resume_file"}
        or classify_field_sensitivity(clean_label) != SensitivityLevel.PUBLIC
        or _DISALLOWED_TOPIC_RE.search(clean_label)
        or any(k in clean_label.lower() for k in SENSITIVE_FACT_CATEGORIES)
    ):
        raise ArsenalValidationError(
            "sensitive_or_salary_not_reusable",
            "Pretensão salarial e dados sensíveis ou de consentimento não podem ser salvos no Arsenal reutilizável.",
        )

    clean_text = answer_text.strip()
    if not clean_text:
        raise ArsenalValidationError("arsenal_requires_text", "Informe texto válido antes de salvar no Arsenal.")
    u16 = utf16_len(clean_text)
    if u16 > 1500:
        raise ArsenalValidationError(
            "arsenal_text_too_long",
            "O texto excede o limite de 1500 caracteres para salvar no Arsenal.",
        )

    try:
        used_slots = _validate_slot_placeholders(clean_text)
    except ValueError as exc:
        raise ArsenalValidationError("invalid_arsenal_slots", str(exc)) from exc

    norm_ans = normalize_label_text(clean_text)
    norm_company = normalize_label_text(job_company)
    if (
        norm_company
        and len(norm_company) >= 3
        and "company_name" not in used_slots
        and _contains_whole_phrase(norm_ans, norm_company)
    ):
        has_company_scope = any(
            _contains_whole_phrase(normalize_label_text(kw), norm_company)
            or _contains_whole_phrase(norm_company, normalize_label_text(kw))
            for clause in when_to_use
            for kw in clause.all_keywords
            if normalize_label_text(kw)
        )
        if not has_company_scope:
            raise ArsenalValidationError(
                "arsenal_company_specific_requires_scope",
                "A resposta menciona a empresa atual; defina critério explícito em 'when_to_use' "
                "ou use {{company_name}} antes de salvar no Arsenal.",
            )

    norm_title = normalize_label_text(job_title)
    if (
        norm_title
        and len(norm_title) >= 5
        and "role_title" not in used_slots
        and _contains_whole_phrase(norm_ans, norm_title)
    ):
        has_role_scope = bool(
            role_tags
            or role_archetype != "all"
            or any(c.archetype_in or c.seniority_in or c.all_keywords for c in when_to_use)
        )
        if not has_role_scope:
            raise ArsenalValidationError(
                "arsenal_role_specific_requires_scope",
                "A resposta menciona o cargo atual; defina role_archetype/role_tags/when_to_use "
                "ou use {{role_title}} antes de salvar no Arsenal.",
            )

    # Populate ONLY the single tier matching confirmed text length; never synthesize longer variants.
    if u16 <= 150 and "\n" not in clean_text and "\r" not in clean_text:
        variants = LengthVariants(short=clean_text)
    elif u16 <= 500:
        variants = LengthVariants(medium=clean_text)
    else:
        variants = LengthVariants(long=clean_text)

    extra_triggers = ["CEP", "ZIP CODE", "Código Postal", "Postal Code"] if cat == "contact_postal_code" else []
    extra_sigs = (
        ["text:cep", "text:zip code", "text:codigo postal", "text:postal code"]
        if cat == "contact_postal_code"
        else []
    )
    merged_triggers = [
        kw.strip()[:120]
        for kw in dict.fromkeys([clean_label[:120], *trigger_keywords, *extra_triggers])
        if kw and kw.strip()
    ][:32]
    merged_sigs = list(dict.fromkeys(([question_signature] if question_signature else []) + extra_sigs))[:32]

    payload = ArsenalPayload(
        length_variants=variants,
        trigger_keywords=merged_triggers,
        question_signatures=merged_sigs,
        slots=sorted(used_slots),
        role_tags=list(role_tags)[:16],
        when_to_use=list(when_to_use)[:16],
        when_not_to_use=list(when_not_to_use)[:16],
        source_claim_ids=[],
        provenance="user",
    )
    if is_instructional_draft_payload(payload):
        raise ArsenalValidationError(
            "instructional_draft_requires_replacement",
            "Substitua textos instrucionais por uma resposta real antes de salvar no Arsenal.",
        )

    now = utc_rfc3339()
    return ArsenalRespostaRow(
        id=new_item_id("ars"),
        user_id=user_id,
        revision=1,
        status="draft",
        language=language,
        role_archetype=role_archetype,
        category=category,
        payload_json=payload.model_dump(mode="json"),
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
