"""Guarded Pending Decisions Queue, CAS state machine, and Salary Golden Rule."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.browser_automation.driver import (
    FieldDescriptor,
    StepBlocker,
    StepSnapshot,
    normalize_label_text,
    resolve_option_for_answer,
)
from backend.app.core.ssrf import extract_origin
from backend.app.core.state_machine import RESUMABLE_STATUSES
from backend.app.core.time import utc_rfc3339
from backend.app.models.tables import (
    Application,
    AuditEvent,
    JobPosting,
    PendingManualActionRow,
    UserSettings,
)
from backend.app.schemas.contracts import SaveToArsenalInput
from backend.app.schemas.profile import (
    ApprovalValue,
    Archetype,
    Contract,
    Currency,
    FieldApproval,
    FieldControlType,
    FieldOption,
    FieldSnapshot,
    HexSha256,
    Locale,
    MasterProfileDocument,
    Period,
    SalaryApproval,
    SalaryBand,
    SalaryRangeValue,
    SalarySuggestion,
    Seniority,
    StrictRevision,
)
from backend.app.security.vault import (
    SENSITIVE_FACT_CATEGORIES,
    classify_field_sensitivity,
    derive_local_fernet,
)
from backend.app.services.applications.rules import match_field_rule
from backend.app.services.applications.validator import (
    ControlConstraints,
    extract_control_constraints,
    validate_candidate_value,
)

ACTIVE_APPLICATION_STATUSES = tuple(sorted(RESUMABLE_STATUSES))

PendingKind = Literal["salary", "sensitive", "ambiguous", "low_confidence", "invalid", "manual_step"]

_SALARY_BLOCKER_CODES = frozenset({"salary_approval_required", "salary_review_required"})
_MANUAL_STEP_CODES = frozenset(
    {
        "consent_required",
        "upload_manual_required",
        "upload_failed",
        "captcha",
        "captcha_required",
        "otp_required",
        "login_required",
        "open_form_required",
        "unknown_origin",
        "multiple_containers",
        "vacancy_mismatch",
        "external_assessment",
        "video_required",
        "unsupported_widget",
    }
)


class PendingActionConflictError(RuntimeError):
    """Raised when a pending action CAS check fails or its decision already changed."""

    def __init__(self, code: str, message: str, *, current: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current


class PendingActionValidationError(ValueError):
    """Raised when an approval, skip, or Arsenal save violates field or policy rules."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ApproveSalaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictRevision
    field_fingerprint: HexSha256
    approval: SalaryApproval


def salary_context(field: FieldDescriptor) -> tuple[Currency | None, Period | None]:
    label = normalize_label_text(field.label)
    currency: Currency | None = None
    brl = "R$" in field.label or bool(re.search(r"\b(brl|reais)\b", label))
    usd = "US$" in field.label or bool(re.search(r"\busd|dollars?\b", label)) or bool(
        re.search(r"(?<![Rr])\$", field.label)
    )
    if brl != usd:
        currency = "BRL" if brl else "USD"
    elif not brl and not usd:
        currency = "BRL"
    periods: list[Period] = []
    if re.search(r"\b(anual|annual|yearly|por ano|per year)\b", label):
        periods.append("annual")
    if re.search(r"\b(mensal|monthly|por mes|per month)\b", label):
        periods.append("monthly")
    if re.search(r"\b(por hora|hourly|per hour|taxa horaria|valor hora)\b", label):
        periods.append("hourly")
    period: Period | None = periods[0] if len(periods) == 1 else ("monthly" if not periods else None)
    return currency, period


def _derive_job_context(job: JobPosting | None) -> tuple[Contract, Seniority, Archetype]:
    contract: Contract = "unknown"
    seniority: Seniority = "unknown"
    archetype: Archetype = "all"
    if job is not None:
        contract_hint = normalize_label_text(job.employment_type or "")
        if contract_hint == "clt":
            contract = "CLT"
        elif contract_hint == "pj":
            contract = "PJ"
        elif contract_hint in {"estagio", "internship", "intern"}:
            contract = "Estágio"
        title = normalize_label_text(job.title)
        level = normalize_label_text(job.seniority or "")
        if level in {"junior", "mid", "senior", "staff", "lead", "intern"}:
            seniority = cast(Seniority, level)
        elif level == "pleno":
            seniority = "mid"
        elif contract == "Estágio":
            seniority = "intern"
        if seniority == "intern":
            archetype = "intern_junior"
        elif re.search(
            r"\b(data engineer|engenheir[oa] de dados|engenharia de dados|analytics engineer)\b",
            title,
        ):
            archetype = "data_engineer"
        elif re.search(r"\b(backend|back end)\b", title) and "python" in title:
            archetype = "backend_python"
        elif re.search(r"\b(machine learning|data scientist|cientista de dados)\b", title):
            archetype = "ml_engineer"
    return contract, seniority, archetype


def suggest_salary_with_meta(
    bands: list[SalaryBand],
    job: JobPosting | None,
    currency: Currency | None,
    period: Period | None,
    today: date,
    *,
    preferred_floor_minor: int | None = None,
    preferred_currency: str | None = None,
    preferred_period: str | None = None,
) -> tuple[SalarySuggestion, dict[str, Any]]:
    """Compute a strictly contextual salary suggestion plus safe floor presentation metadata."""
    contract, seniority, archetype = _derive_job_context(job)
    meta: dict[str, Any] = {
        "role_archetype": archetype,
        "conflicting_bands": False,
        "floor_exceeds_band": False,
        "preferred_floor_minor": None,
        "band_min_minor": None,
        "band_max_minor": None,
    }
    if contract == "unknown" or seniority == "unknown" or currency is None or period is None:
        return (
            SalarySuggestion(contract=contract, seniority=seniority, reason_code="unknown_context"),
            meta,
        )

    matches = [
        band
        for band in bands
        if band.contract == contract
        and band.currency == currency
        and band.period == period
        and band.seniority in {seniority, "unknown"}
        and band.role_archetype in {archetype, "all"}
    ]
    fresh = [band for band in matches if today - timedelta(days=180) <= band.as_of <= today]
    if not fresh:
        return (
            SalarySuggestion(
                contract=contract,
                seniority=seniority,
                reason_code="stale_band" if matches else "missing_band",
            ),
            meta,
        )
    ranking = {
        band.id: (band.role_archetype == archetype, band.seniority == seniority)
        for band in fresh
    }
    max_rank = max(ranking.values())
    best = [band for band in fresh if ranking[band.id] == max_rank]
    if len(best) != 1:
        meta["conflicting_bands"] = True
        return (
            SalarySuggestion(contract=contract, seniority=seniority, reason_code="missing_band"),
            meta,
        )
    band = best[0]
    suggested_min = band.min_minor
    suggested_max = band.max_minor
    meta["band_min_minor"] = band.min_minor
    meta["band_max_minor"] = band.max_minor

    # Profile salary floor raises lower bound if compatible & inside band, or warns separately if higher.
    floor_curr = (preferred_currency or "").strip().upper()
    floor_per = (preferred_period or "").strip().lower()
    if (
        preferred_floor_minor is not None
        and preferred_floor_minor >= 0
        and floor_curr == currency
        and floor_per == period
    ):
        meta["preferred_floor_minor"] = preferred_floor_minor
        if band.min_minor < preferred_floor_minor <= band.max_minor:
            suggested_min = preferred_floor_minor
        elif preferred_floor_minor > band.max_minor:
            meta["floor_exceeds_band"] = True

    suggestion = SalarySuggestion(
        contract=contract,
        seniority=seniority,
        currency=currency,
        period=period,
        min_minor=suggested_min,
        max_minor=suggested_max,
        band_id=band.id,
        basis=band.basis,
        source_note=band.source_note,
        as_of=band.as_of,
        reason_code="configured_band",
    )
    return suggestion, meta


def suggest_salary(
    bands: list[SalaryBand],
    job: JobPosting | None,
    currency: Currency | None,
    period: Period | None,
    today: date,
) -> SalarySuggestion:
    suggestion, _ = suggest_salary_with_meta(bands, job, currency, period, today)
    return suggestion


def _amount_text(value: int) -> str:
    if value > 999_999_999_999:
        raise PendingActionValidationError("invalid_amount", "Valor salarial acima do limite.")
    whole, fraction = divmod(value, 100)
    return f"{whole}.{fraction:02d}" if fraction else str(whole)


def _field_shape(field: FieldDescriptor) -> Literal["amount", "range", "option", "ambiguous"]:
    if field.control_kind == "select" and field.locator_hint.get("tag") == "select":
        if not field.options or len(field.options) > 200:
            return "ambiguous"
        seen_currencies: set[str] = set()
        seen_periods: set[str] = set()
        for opt in field.options:
            lbl = normalize_label_text(opt.label)
            if "r$" in opt.label.lower() or re.search(r"\b(brl|reais)\b", lbl):
                seen_currencies.add("BRL")
            if "us$" in opt.label.lower() or re.search(r"\b(usd|dollars?)\b", lbl):
                seen_currencies.add("USD")
            if re.search(r"\b(mensal|monthly|por mes)\b", lbl):
                seen_periods.add("monthly")
            if re.search(r"\b(anual|annual|yearly|por ano)\b", lbl):
                seen_periods.add("annual")
            if re.search(r"\b(por hora|hourly|per hour)\b", lbl):
                seen_periods.add("hourly")
        if len(seen_currencies) > 1 or len(seen_periods) > 1:
            return "ambiguous"
        return "option"
    if field.control_kind not in {"number", "text", "textarea"}:
        return "ambiguous"
    if not re.search(r"\b(faixa|range|intervalo)\b", normalize_label_text(field.label)):
        return "amount"
    example = str(field.locator_hint.get("placeholder") or "")
    if field.control_kind != "number" and re.fullmatch(
        r"\s*\d+(?:\.\d{1,2})? - \d+(?:\.\d{1,2})?\s*", example
    ):
        return "range"
    return "ambiguous"


def _map_control_type(control_kind: str) -> FieldControlType:
    allowed: set[str] = {
        "text",
        "textarea",
        "tel",
        "email",
        "url",
        "number",
        "date",
        "select",
        "radio",
        "checkbox",
        "file",
        "unsupported",
    }
    if control_kind in allowed:
        return cast(FieldControlType, control_kind)
    return "unsupported"


def _parse_optional_nonneg_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if text.isdigit():
        val = int(text)
        return val if val >= 0 else None
    return None


def _extract_field_snapshot(
    field: FieldDescriptor,
    *,
    fallback_label: str = "Decisão pendente",
    currency: Currency | None = None,
    period: Period | None = None,
    shape: Literal["amount", "range", "option", "ambiguous"] | None = None,
    contract: Contract | None = None,
    locale: Locale | None = None,
) -> FieldSnapshot:
    constraints = extract_control_constraints(
        control_kind=field.control_kind,
        label=field.label,
        locale=locale,
        required=field.required,
        locator_hint=field.locator_hint,
    )

    options: list[FieldOption] = []
    if field.control_kind in {"select", "radio"} and field.options:
        options = [
            FieldOption(key=opt.option_id, label=(opt.label or opt.option_id)[:120])
            for opt in field.options
            if opt.label.strip()
            and (field.control_kind == "radio" or (opt.value and opt.available))
            and (
                field.control_kind != "select"
                or sum(other.value == opt.value for other in field.options) == 1
            )
        ][:200]

    min_chars = constraints.minlength
    max_chars = constraints.effective_max_chars
    if min_chars is not None and max_chars is not None and min_chars > max_chars:
        min_chars = None

    return FieldSnapshot(
        label=((field.label or "").strip()[:120] or fallback_label),
        control=_map_control_type(field.control_kind),
        locale=locale,
        required=field.required,
        min_chars=min_chars,
        max_chars=max_chars,
        max_words=constraints.counter_max_words,
        pattern=constraints.pattern[:240] if constraints.pattern else None,
        minimum=constraints.minimum[:120] if constraints.minimum else None,
        maximum=constraints.maximum[:120] if constraints.maximum else None,
        step=constraints.step[:120] if constraints.step else None,
        options=options,
        salary_contract=contract,
        salary_currency=currency,
        salary_period=period,
        salary_value_shape=shape,
    )


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _extract_profile_floor_minor(doc: MasterProfileDocument | None) -> tuple[int | None, str | None, str | None]:
    if doc is None or doc.preferences.salary_amount is None:
        return None, None, None
    try:
        dec = Decimal(str(doc.preferences.salary_amount))
        if dec < 0:
            return None, None, None
        minor = int((dec * 100).to_integral_value())
        return minor, doc.preferences.salary_currency, doc.preferences.salary_period
    except (InvalidOperation, ValueError):
        return None, None, None


def serialize_pending_action_row(
    row: PendingManualActionRow,
    *,
    job_title: str = "",
    company: str = "",
    suggestion_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize a PendingManualActionRow safely without leaking approval_sealed."""
    field_json = dict(row.field_json or {})
    shape = field_json.get("salary_value_shape")
    currency = field_json.get("salary_currency")
    period = field_json.get("salary_period")
    can_approve = False
    if row.kind == "salary":
        can_approve = bool(shape != "ambiguous" and currency and period)
    elif row.kind in {"ambiguous", "low_confidence", "invalid"}:
        can_approve = field_json.get("control") in {
            "text",
            "textarea",
            "tel",
            "email",
            "url",
            "number",
            "date",
            "select",
            "radio",
            "checkbox",
        }
    elif row.kind == "sensitive":
        can_approve = field_json.get("control") in {"text", "textarea", "date"}

    return {
        "id": row.id,
        "application_id": row.application_id,
        "step_key": row.step_key,
        "field_key": row.field_key,
        "field_fingerprint": row.field_fingerprint,
        "kind": row.kind,
        "status": row.status,
        "field": field_json,
        "suggestion": row.suggestion_json,
        "suggestion_meta": suggestion_meta,
        "revision": row.revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "approved_at": row.approved_at,
        "applied_at": row.applied_at,
        # Safe display helpers for Candidatura & Pré-voo panels:
        "job_title": job_title,
        "company": company,
        "label": field_json.get("label") or "Revisão pendente",
        "currency": currency,
        "period": period,
        "shape": shape,
        "options": field_json.get("options") or [],
        "required": bool(field_json.get("required", True)),
        "can_approve": can_approve,
        "can_skip": not bool(field_json.get("required", True)) and row.kind != "manual_step",
    }


def _encode_action_cursor(updated_at: str, action_id: str) -> str:
    raw = json.dumps({"u": updated_at, "id": action_id}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_action_cursor(token: str) -> tuple[str, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        u = obj.get("u")
        aid = obj.get("id")
        if not isinstance(u, str) or not isinstance(aid, str) or not u or not aid:
            raise ValueError("cursor inválido")
        return u, aid
    except Exception as exc:
        raise PendingActionValidationError("invalid_cursor", "Cursor de paginação inválido.") from exc


def count_active_pending_actions(db: Session, *, user_id: str) -> int:
    """Count each pending action on active applications once."""
    return len(
        db.scalars(
            select(PendingManualActionRow.id)
            .join(Application, Application.id == PendingManualActionRow.application_id)
            .where(
                PendingManualActionRow.user_id == user_id,
                PendingManualActionRow.status == "pending",
                Application.status.in_(ACTIVE_APPLICATION_STATUSES),
            )
        ).all()
    )


def list_pending_manual_actions(
    db: Session,
    *,
    user_id: str,
    application_id: str | None = None,
    status: str = "pending",
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return cursor-paginated pending manual actions sorted by updated_at DESC, id DESC."""
    allowed_statuses = {"pending", "approved", "applied", "skipped", "stale", "all"}
    if status not in allowed_statuses:
        raise PendingActionValidationError("invalid_status", "Filtro de status inválido para pendências.")

    stmt = (
        select(PendingManualActionRow, JobPosting.title, JobPosting.company)
        .join(Application, Application.id == PendingManualActionRow.application_id)
        .join(JobPosting, JobPosting.id == Application.job_id)
        .where(PendingManualActionRow.user_id == user_id)
    )
    if application_id:
        stmt = stmt.where(PendingManualActionRow.application_id == application_id)
    if status != "all":
        stmt = stmt.where(PendingManualActionRow.status == status)
    if cursor:
        cur_u, cur_id = _decode_action_cursor(cursor)
        stmt = stmt.where(
            (PendingManualActionRow.updated_at < cur_u)
            | (
                (PendingManualActionRow.updated_at == cur_u)
                & (PendingManualActionRow.id < cur_id)
            )
        )

    stmt = stmt.order_by(PendingManualActionRow.updated_at.desc(), PendingManualActionRow.id.desc()).limit(
        limit + 1
    )
    rows = list(db.execute(stmt).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        serialize_pending_action_row(row, job_title=title, company=company)
        for row, title, company in page_rows
    ]
    next_cursor = (
        _encode_action_cursor(page_rows[-1][0].updated_at, page_rows[-1][0].id)
        if has_more and page_rows
        else None
    )
    return items, next_cursor


def _validate_live_scalar_constraints(decimal_str: str, live_field: FieldDescriptor | None, field_json: dict[str, Any]) -> None:
    minimum = (
        live_field.locator_hint.get("constraints", [None] * 6)[0]
        if live_field is not None
        else field_json.get("minimum")
    )
    maximum = (
        live_field.locator_hint.get("constraints", [None] * 6)[1]
        if live_field is not None
        else field_json.get("maximum")
    )
    step = (
        live_field.locator_hint.get("constraints", [None] * 6)[2]
        if live_field is not None
        else field_json.get("step")
    )
    maxlen = (
        live_field.locator_hint.get("constraints", [None] * 6)[4]
        if live_field is not None
        else field_json.get("max_chars")
    )
    if maxlen is not None and str(maxlen).isdigit() and _utf16_len(decimal_str) > int(maxlen):
        raise PendingActionValidationError("value_too_long", "Valor excede o tamanho máximo aceito pelo campo.")
    try:
        dec = Decimal(decimal_str)
    except InvalidOperation as exc:
        raise PendingActionValidationError("invalid_number", "Valor numérico inválido.") from exc
    if minimum not in (None, ""):
        try:
            if dec < Decimal(str(minimum)):
                raise PendingActionValidationError(
                    "below_minimum", f"Valor abaixo do mínimo exigido pelo formulário ({minimum})."
                )
        except InvalidOperation:
            pass
    if maximum not in (None, ""):
        try:
            if dec > Decimal(str(maximum)):
                raise PendingActionValidationError(
                    "above_maximum", f"Valor acima do máximo permitido pelo formulário ({maximum})."
                )
        except InvalidOperation:
            pass
    if step not in (None, "", "any"):
        try:
            step_dec = Decimal(str(step))
            base = Decimal(str(minimum)) if minimum not in (None, "") else Decimal(0)
            if step_dec > 0 and ((dec - base) % step_dec) != 0:
                raise PendingActionValidationError(
                    "invalid_step", f"Valor incompatível com o incremento '{step}' do campo."
                )
        except InvalidOperation:
            pass


def _blocker_kind(code: str) -> PendingKind:
    if code in _SALARY_BLOCKER_CODES:
        return "salary"
    if code in {"sensitive_manual", "invalid_cpf"}:
        return "sensitive"
    if code in {
        "invalid_field_value",
        "invalid_cep",
        "invalid_phone",
        "invalid_email",
        "invalid_url",
        "invalid_date",
        "length_mismatch",
        "pattern_mismatch",
        "readback_mismatch",
    }:
        return "invalid"
    if code == "low_confidence":
        return "low_confidence"
    if code == "missing_answer":
        return "ambiguous"
    return "manual_step"


class SalaryQueue:
    """Unified queue manager for pending manual decisions including salary approvals."""

    def __init__(
        self,
        secret: str,
        user_id: str,
        application_id: str,
        binding: str,
        *,
        browser_generation: int = 1,
    ) -> None:
        self.secret = secret
        self.vault = derive_local_fernet(f"salary-approval:{secret}")
        self.user_id = user_id
        self.application_id = application_id
        self.binding = binding
        self.browser_generation = browser_generation
        # Map field_id -> (action_id, attempt_id, pre_write_revision, kind)
        self.claimed: dict[str, tuple[str, str, int, str]] = {}
        self.last_pending_action_ids: list[str] = []
        self.last_pending_actions: list[dict[str, Any]] = []

    def step_key(self, snapshot: StepSnapshot) -> str:
        origin = extract_origin(snapshot.page_url) if snapshot.page_url else ""
        path = urlparse(snapshot.page_url).path if snapshot.page_url else ""
        data = (
            self.binding,
            self.browser_generation,
            origin,
            snapshot.navigation_gen,
            path,
            snapshot.structural_fingerprint,
        )
        return hmac.new(self.secret.encode(), repr(data).encode(), hashlib.sha256).hexdigest()

    def field_fingerprint(
        self,
        key: str,
        field_key: str,
        field: FieldDescriptor | None,
        *,
        currency: Currency | None = None,
        period: Period | None = None,
        shape: Literal["amount", "range", "option", "ambiguous"] | None = None,
        kind: PendingKind = "manual_step",
        label: str = "",
    ) -> str:
        if field is not None:
            constraints = tuple(field.locator_hint.get("constraints") or [])
            opts = tuple((o.option_id, o.value, o.label, o.available) for o in field.options)
            payload: tuple[Any, ...] = (
                self.binding,
                key,
                field.field_id,
                field.question_signature,
                normalize_label_text(field.label),
                field.control_kind,
                field.required,
                constraints,
                opts,
                currency,
                period,
                shape,
                kind,
            )
        else:
            payload = (self.binding, key, field_key, normalize_label_text(label), kind)
        return hmac.new(self.secret.encode(), repr(payload).encode(), hashlib.sha256).hexdigest()

    def rows(self, db: Session) -> list[PendingManualActionRow]:
        return list(
            db.scalars(
                select(PendingManualActionRow).where(
                    PendingManualActionRow.application_id == self.application_id,
                    PendingManualActionRow.user_id == self.user_id,
                    PendingManualActionRow.status.in_(("pending", "approved", "applied", "skipped")),
                )
            )
        )

    def _has_open_write_attempt(self, db: Session, action_id: str) -> bool:
        events = list(
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "pending_manual_action",
                    AuditEvent.entity_id == action_id,
                    AuditEvent.event_type.in_(
                        (
                            "protected_write_attempt",
                            "protected_write_applied",
                            "protected_write_failed",
                            "protected_write_interrupted",
                        )
                    ),
                )
            )
        )
        started: set[str] = set()
        terminated: set[str] = set()
        for ev in events:
            att_id = str((ev.details or {}).get("attempt_id") or "")
            if not att_id:
                continue
            if ev.event_type == "protected_write_attempt":
                started.add(att_id)
            else:
                terminated.add(att_id)
        return bool(started - terminated)

    def claim_pending(self, db: Session, snapshot: StepSnapshot) -> tuple[dict[str, str], dict[str, str]]:
        """Decrypt and audit-lock approved rows right before browser writes outside transaction."""
        key = self.step_key(snapshot)
        fields_by_id = {f.field_id: f for f in snapshot.fields if f.editable and not f.has_value}
        salary_result: dict[str, str] = {}
        action_result: dict[str, str] = {}
        now = utc_rfc3339()

        for row in self.rows(db):
            if row.status != "approved" or row.step_key != key:
                continue
            field = fields_by_id.get(row.field_key)
            if field is None:
                continue
            if row.kind == "salary" and field.category != "salary":
                continue

            cur = salary_context(field) if row.kind == "salary" else (None, None)
            shp = _field_shape(field) if row.kind == "salary" else None
            expected_fp = self.field_fingerprint(
                key,
                field.field_id,
                field,
                currency=cur[0],
                period=cur[1],
                shape=shp,
                kind=cast(PendingKind, row.kind),
            )
            if row.field_fingerprint != expected_fp:
                row.status = "stale"
                row.approval_sealed = None
                row.revision += 1
                row.updated_at = now
                continue

            if self._has_open_write_attempt(db, row.id):
                row.status = "stale"
                row.approval_sealed = None
                row.revision += 1
                row.updated_at = now
                db.add(
                    AuditEvent(
                        actor=self.user_id,
                        event_type="protected_write_interrupted",
                        entity_type="pending_manual_action",
                        entity_id=row.id,
                        details={"reason": "unreconciled_prior_attempt"},
                    )
                )
                continue

            sealed = row.approval_sealed
            if sealed is None:
                row.status = "stale"
                row.revision += 1
                row.updated_at = now
                continue
            try:
                payload = json.loads(self.vault.decrypt(sealed))
            except (InvalidToken, ValueError):
                row.status = "stale"
                row.approval_sealed = None
                row.revision += 1
                row.updated_at = now
                continue
            if payload.get("id") != row.id or payload.get("fingerprint") != row.field_fingerprint:
                continue

            attempt_id = str(uuid4())
            self.claimed[row.field_key] = (row.id, attempt_id, row.revision, row.kind)
            if row.kind == "salary":
                salary_result[row.field_key] = str(payload["value"])
            else:
                action_result[row.field_key] = str(payload["value"])
            db.add(
                AuditEvent(
                    actor=self.user_id,
                    event_type="protected_write_attempt",
                    entity_type="pending_manual_action",
                    entity_id=row.id,
                    details={
                        "attempt_id": attempt_id,
                        "revision": row.revision,
                        "fingerprint": row.field_fingerprint,
                        "kind": row.kind,
                    },
                )
            )
        db.commit()
        return salary_result, action_result

    def claim(self, db: Session, snapshot: StepSnapshot) -> dict[str, str]:
        salary_result, _ = self.claim_pending(db, snapshot)
        return salary_result

    def synchronize(
        self,
        db: Session,
        snapshot: StepSnapshot,
        blocked_ids: set[str],
        filled_ids: set[str],
        job: JobPosting | None,
        *,
        doc: MasterProfileDocument | None = None,
        all_blockers: tuple[StepBlocker, ...] | list[StepBlocker] = (),
        skipped_items: tuple[dict[str, str], ...] | list[dict[str, str]] = (),
        write_outcomes: dict[str, str] | None = None,
        locale: Locale | None = None,
    ) -> list[dict[str, Any]]:
        """Reconcile claimed write attempts and sync step blockers into pending_manual_actions."""
        key = self.step_key(snapshot)
        now = utc_rfc3339()
        outcomes = write_outcomes or {}

        # 1. Reconcile claimed writes after browser fill attempt
        just_applied_ids: set[str] = set()
        for field_id, (action_id, attempt_id, expected_rev, kind) in self.claimed.items():
            if field_id in filled_ids and outcomes.get(field_id, "applied") == "applied":
                changed = db.execute(
                    update(PendingManualActionRow)
                    .where(
                        PendingManualActionRow.id == action_id,
                        PendingManualActionRow.user_id == self.user_id,
                        PendingManualActionRow.status == "approved",
                        PendingManualActionRow.revision == expected_rev,
                    )
                    .values(
                        status="applied",
                        approval_sealed=None,
                        applied_at=now,
                        revision=expected_rev + 1,
                        updated_at=now,
                    )
                    .returning(PendingManualActionRow.id)
                ).scalar_one_or_none()
                if changed is not None:
                    just_applied_ids.add(action_id)
                    db.add(
                        AuditEvent(
                            actor=self.user_id,
                            event_type="protected_write_applied",
                            entity_type="pending_manual_action",
                            entity_id=action_id,
                            details={"attempt_id": attempt_id, "kind": kind},
                        )
                    )
            else:
                reason = outcomes.get(field_id, "write_interrupted")
                changed = db.execute(
                    update(PendingManualActionRow)
                    .where(
                        PendingManualActionRow.id == action_id,
                        PendingManualActionRow.user_id == self.user_id,
                        PendingManualActionRow.status == "approved",
                    )
                    .values(
                        status="stale",
                        approval_sealed=None,
                        revision=PendingManualActionRow.revision + 1,
                        updated_at=now,
                    )
                    .returning(PendingManualActionRow.id)
                ).scalar_one_or_none()
                if changed is not None:
                    db.add(
                        AuditEvent(
                            actor=self.user_id,
                            event_type="protected_write_failed",
                            entity_type="pending_manual_action",
                            entity_id=action_id,
                            details={"attempt_id": attempt_id, "kind": kind, "reason": reason},
                        )
                    )

        self.claimed.clear()
        fields_by_id = {f.field_id: f for f in snapshot.fields}

        # Build desired active queue targets keyed by field_key
        desired: dict[str, tuple[PendingKind, FieldDescriptor | None, str]] = {}
        for fid in blocked_ids:
            if fid in fields_by_id:
                desired[fid] = ("salary", fields_by_id[fid], fields_by_id[fid].label or "Pretensão salarial")

        for blk in all_blockers:
            kind = _blocker_kind(blk.code)
            if blk.field_id:
                fid = blk.field_id
                fdesc = fields_by_id.get(fid)
                if fid not in desired:
                    desired[fid] = (kind, fdesc, blk.label or blk.message[:120])
            elif blk.code in _MANUAL_STEP_CODES:
                step_fid = f"step:{blk.code}"
                desired[step_fid] = ("manual_step", None, blk.label or blk.message[:120])

        for sk in skipped_items:
            if sk.get("reason") == "optional_unresolved" and sk.get("field_id"):
                fid = str(sk["field_id"])
                fdesc = fields_by_id.get(fid)
                if fdesc is not None and fid not in desired:
                    desired[fid] = ("ambiguous", fdesc, str(sk.get("label") or fdesc.label or "Pergunta opcional"))

        # Compute fingerprints upfront for every desired item
        desired_fp: dict[str, tuple[PendingKind, FieldDescriptor | None, str, Currency | None, Period | None, Literal["amount", "range", "option", "ambiguous"] | None, str]] = {}
        for fkey, (kind, fdesc, lbl) in desired.items():
            cur: tuple[Currency | None, Period | None] = (None, None)
            shp: Literal["amount", "range", "option", "ambiguous"] | None = None
            if kind == "salary" and fdesc is not None:
                cur = salary_context(fdesc)
                shp = _field_shape(fdesc)
            fp = self.field_fingerprint(
                key,
                fkey,
                fdesc,
                currency=cur[0],
                period=cur[1],
                shape=shp,
                kind=kind,
                label=lbl,
            )
            desired_fp[fkey] = (kind, fdesc, lbl, cur[0], cur[1], shp, fp)

        # 2. Stale previous step/changed/re-blocked rows; preserve deduplicated matching & newly applied rows
        active_by_field: dict[str, PendingManualActionRow] = {}
        for row in self.rows(db):
            if row.id in just_applied_ids:
                continue
            target = desired_fp.get(row.field_key)
            if row.status == "applied":
                if row.step_key != key or target is not None:
                    # Single-use applied approval: if step changed or field is unresolved/blocked again, invalidate it.
                    row.status = "stale"
                    row.approval_sealed = None
                    row.revision += 1
                    row.updated_at = now
                continue
            if row.step_key != key or target is None or target[6] != row.field_fingerprint:
                row.status = "stale"
                row.approval_sealed = None
                row.revision += 1
                row.updated_at = now
                continue
            active_by_field[row.field_key] = row
        db.flush()

        settings = (
            db.scalar(select(UserSettings).where(UserSettings.user_id == self.user_id))
            if any(k == "salary" for k, *_ in desired_fp.values())
            else None
        )
        bands = [SalaryBand.model_validate(item) for item in settings.salary_policy_json] if settings else []
        today = datetime.now(ZoneInfo("America/Recife")).date()
        pref_floor, pref_curr, pref_per = _extract_profile_floor_minor(doc)

        salary_actions: list[dict[str, Any]] = []
        pending_actions: list[dict[str, Any]] = []
        contract_ctx, _, _ = _derive_job_context(job)

        for fkey, (kind, fdesc, lbl, currency, period, shape, fp) in desired_fp.items():
            action = active_by_field.get(fkey)
            if fdesc is not None:
                field_snap = _extract_field_snapshot(
                    fdesc,
                    fallback_label=lbl,
                    currency=currency,
                    period=period,
                    shape=shape,
                    contract=contract_ctx if kind == "salary" else None,
                    locale=locale,
                )
            else:
                field_snap = FieldSnapshot(label=lbl[:120] or "Ação manual na página", control="unsupported", required=True)

            suggestion_meta: dict[str, Any] | None = None
            suggestion_dict: dict[str, Any] | None = None
            if kind == "salary":
                suggestion, suggestion_meta = suggest_salary_with_meta(
                    bands,
                    job,
                    currency,
                    period,
                    today,
                    preferred_floor_minor=pref_floor,
                    preferred_currency=pref_curr,
                    preferred_period=pref_per,
                )
                suggestion_dict = suggestion.model_dump(mode="json")

            if action is None:
                action = PendingManualActionRow(
                    user_id=self.user_id,
                    application_id=self.application_id,
                    step_key=key,
                    field_key=fkey,
                    field_fingerprint=fp,
                    kind=kind,
                    status="pending",
                    field_json=field_snap.model_dump(mode="json"),
                    suggestion_json=suggestion_dict,
                )
                db.add(action)
                db.flush()
            else:
                action.field_json = field_snap.model_dump(mode="json")
                if suggestion_dict is not None:
                    action.suggestion_json = suggestion_dict
                if action.status == "skipped":
                    # Preserve user's explicit skip on unchanged optional field.
                    continue

            serialized = serialize_pending_action_row(
                action,
                job_title=job.title if job else "",
                company=job.company if job else "",
                suggestion_meta=suggestion_meta,
            )
            pending_actions.append(serialized)
            if kind == "salary":
                salary_actions.append(serialized)

        self.last_pending_actions = pending_actions
        self.last_pending_action_ids = [
            item["id"] for item in pending_actions if item["status"] == "pending"
        ]
        return salary_actions

    def seal(self, row: PendingManualActionRow, approval: SalaryApproval) -> bytes:
        """Backwards-compatible salary sealing helper."""
        return self.validate_and_seal(row, approval)

    def validate_and_seal(
        self,
        row: PendingManualActionRow,
        approval: ApprovalValue,
        *,
        live_field: FieldDescriptor | None = None,
        save_to_arsenal: SaveToArsenalInput | None = None,
    ) -> bytes:
        """Strictly validate a user decision against field snapshot & live DOM control before sealing."""
        if row.kind == "manual_step":
            raise PendingActionValidationError(
                "manual_browser_task",
                "Esta etapa exige conclusão manual diretamente no Chrome.",
            )

        if save_to_arsenal is not None:
            if (
                row.kind in {"salary", "sensitive"}
                or save_to_arsenal.category == "salary_note"
                or (live_field is not None and live_field.category == "salary")
            ):
                raise PendingActionValidationError(
                    "sensitive_or_salary_not_reusable",
                    "Pretensão salarial e dados sensíveis não podem ser salvos no Arsenal reutilizável.",
                )
            label_text = str(row.field_json.get("label") or "")
            if (
                classify_field_sensitivity(label_text).value == "sensitive_pii"
                or any(k in label_text.lower() for k in SENSITIVE_FACT_CATEGORIES)
            ):
                raise PendingActionValidationError(
                    "sensitive_not_reusable",
                    "Campos com dados sensíveis não podem ser salvos no Arsenal reutilizável.",
                )

        if row.kind == "salary":
            if not isinstance(approval, SalaryApproval):
                raise PendingActionValidationError(
                    "invalid_salary_approval",
                    "Aprovação salarial exige o contrato tipado de salário.",
                )
            expected_shape = row.field_json.get("salary_value_shape")
            expected_curr = row.field_json.get("salary_currency")
            expected_per = row.field_json.get("salary_period")
            if (
                approval.shape != expected_shape
                or approval.currency != expected_curr
                or approval.period != expected_per
                or expected_shape == "ambiguous"
            ):
                raise PendingActionValidationError(
                    "salary_context_mismatch",
                    "Confirme o formato, a moeda e o período indicados pelo formulário.",
                )
            if approval.shape == "amount" and isinstance(approval.value, int) and not isinstance(approval.value, bool):
                canonical_value = _amount_text(approval.value)
                _validate_live_scalar_constraints(canonical_value, live_field, row.field_json)
            elif approval.shape == "range" and isinstance(approval.value, SalaryRangeValue):
                lo_txt = _amount_text(approval.value.min_minor)
                hi_txt = _amount_text(approval.value.max_minor)
                canonical_value = f"{lo_txt} - {hi_txt}"
                maxlen = row.field_json.get("max_chars")
                if maxlen is not None and _utf16_len(canonical_value) > int(maxlen):
                    raise PendingActionValidationError(
                        "value_too_long", "Faixa excede o tamanho máximo do campo."
                    )
            elif approval.shape == "option" and isinstance(approval.value, str):
                valid_opts = row.field_json.get("options", [])
                if not any(approval.value == opt.get("key") for opt in valid_opts):
                    raise PendingActionValidationError(
                        "invalid_option", "Opção inválida para o campo salarial."
                    )
                if live_field is not None and not any(
                    o.option_id == approval.value and o.value and o.available
                    for o in live_field.options
                ):
                    raise PendingActionValidationError(
                        "stale_option", "A opção salarial escolhida não está mais disponível no formulário."
                    )
                canonical_value = approval.value
            else:
                raise PendingActionValidationError(
                    "invalid_salary_value", "Valor ou opção inválida para o campo salarial."
                )
            payload = {"id": row.id, "fingerprint": row.field_fingerprint, "value": canonical_value}
            return self.vault.encrypt(json.dumps(payload).encode())

        # Non-salary governed field approval
        if not isinstance(approval, FieldApproval):
            raise PendingActionValidationError(
                "invalid_field_approval",
                "Use aprovação tipada de campo para pendências não salariais.",
            )
        control = str(row.field_json.get("control") or "text")
        if approval.kind == "boolean":
            if control not in {"checkbox", "radio", "select"} or not isinstance(approval.value, bool):
                raise PendingActionValidationError("invalid_boolean", "Valor booleano incompatível com o controle.")
            canonical_value = "true" if approval.value else "false"
        elif approval.kind == "option":
            if control not in {"select", "radio"} or not isinstance(approval.value, str):
                raise PendingActionValidationError("invalid_option_control", "Campo não aceita seleção de opção.")
            valid_opts = row.field_json.get("options", [])
            matched = next(
                (o for o in valid_opts if approval.value in {o.get("key"), o.get("label")}),
                None,
            )
            if matched is None and live_field is not None:
                opt = resolve_option_for_answer(approval.value, live_field.options)
                if opt is not None:
                    matched = {"key": opt.option_id, "label": opt.label}
            if matched is None:
                raise PendingActionValidationError("invalid_option", "Opção selecionada não existe no campo.")
            canonical_value = str(matched["key"])
        elif approval.kind == "number":
            if control not in {"number", "text"} or not isinstance(approval.value, str):
                raise PendingActionValidationError("invalid_number", "Valor numérico inválido para o campo.")
            canonical_value = approval.value.strip()
            _validate_live_scalar_constraints(canonical_value, live_field, row.field_json)
        else:
            if not isinstance(approval.value, str):
                raise PendingActionValidationError("invalid_text", "Valor textual inválido.")
            canonical_value = approval.value.strip()
            if not canonical_value and row.field_json.get("required", True):
                raise PendingActionValidationError("empty_required_value", "Campo obrigatório não pode ficar vazio.")
            if control != "textarea" and "\n" in canonical_value:
                canonical_value = " ".join(canonical_value.splitlines()).strip()

            label_text = str(row.field_json.get("label") or "")
            raw_loc = row.field_json.get("locale")
            src_locale: Locale = "en" if raw_loc == "en" else "pt-BR"
            if live_field is not None:
                constraints = extract_control_constraints(
                    control_kind=live_field.control_kind,
                    label=live_field.label,
                    locale=src_locale,
                    required=live_field.required,
                    locator_hint=live_field.locator_hint,
                )
            else:
                constraints = ControlConstraints(
                    control_kind=control,
                    label=label_text,
                    locale=src_locale,
                    required=bool(row.field_json.get("required", True)),
                    minlength=row.field_json.get("min_chars"),
                    maxlength=row.field_json.get("max_chars"),
                    counter_max_words=row.field_json.get("max_words"),
                    pattern=row.field_json.get("pattern"),
                    minimum=row.field_json.get("minimum"),
                    maximum=row.field_json.get("maximum"),
                    step=row.field_json.get("step"),
                )
            rule = match_field_rule(
                label_text,
                control,
                category_hint=live_field.category if live_field is not None else None,
            )
            outcome = validate_candidate_value(
                constraints,
                canonical_value,
                rule_id=rule.rule_id,
                intent=rule.formatter or rule.intent,
                source_locale=src_locale,
            )
            if not outcome.valid or outcome.value is None:
                raise PendingActionValidationError(
                    outcome.blocker_code or "invalid_field_value",
                    outcome.blocker_message or "Valor rejeitado pela validação do campo.",
                )
            canonical_value = outcome.value

        payload = {"id": row.id, "fingerprint": row.field_fingerprint, "value": canonical_value}
        return self.vault.encrypt(json.dumps(payload).encode())
