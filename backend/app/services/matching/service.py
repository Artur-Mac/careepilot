"""Transparent, deterministic-first job matching with hard filters, BM25/TF-IDF, and diversity."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.tables import (
    JobPosting,
    JobRequirement,
    JobSourceRecord,
    MatchEvaluation,
    ProfileFact,
    UserSettings,
)
from backend.app.services.discovery.service import KNOWN_SKILLS


def ensure_user_settings(db: Session, user_id: str = "local-applicant") -> UserSettings:
    """Return existing UserSettings or initialize clean default preferences."""
    row = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if row is None:
        row = UserSettings(
            user_id=user_id,
            target_roles=[
                "Data Engineer",
                "Analytics Engineer",
                "Data Analyst",
                "Backend Python",
                "Software Engineer",
                "Data Scientist",
            ],
            preferred_locations=["Brasil", "Remoto", "Belo Horizonte", "São Paulo"],
            workplace_preferences=["remote", "hybrid"],
            seniority_preferences=["junior", "entry_level", "trainee", "intern"],
            contract_types=["CLT", "PJ", "full_time", "FullTime", "Full Time", "Estágio/CLT"],
            languages=["pt-BR", "en"],
            excluded_companies=[],
            salary_floor=None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9áéíóúâêôãõç]{2,}", text.lower())


def _tfidf_cosine(doc_tokens: list[str], profile_tokens: list[str]) -> float:
    if not doc_tokens or not profile_tokens:
        return 0.0
    c_doc = Counter(doc_tokens)
    c_prof = Counter(profile_tokens)
    vocab = set(c_doc) | set(c_prof)
    dot = sum(c_doc[t] * c_prof[t] for t in vocab)
    norm_a = math.sqrt(sum(v * v for v in c_doc.values()))
    norm_b = math.sqrt(sum(v * v for v in c_prof.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _job_family(title: str) -> str:
    low = title.lower()
    if "analytics engineer" in low:
        return "analytics_engineer"
    if "data engineer" in low or "engenheiro de dados" in low or "engenheira de dados" in low or "etl" in low:
        return "data_engineer"
    if "data analyst" in low or "analista de dados" in low or "bi" in low or "power bi" in low:
        return "data_analyst"
    if "data scientist" in low or "cientista de dados" in low or "machine learning" in low:
        return "data_scientist"
    if "backend" in low or "fastapi" in low or "software engineer" in low or "desenvolvedor" in low:
        return "backend_python"
    return "other"


class MatchingService:
    """Evaluates jobs deterministically with hard filters, verified skills, and transparent reasons."""

    def evaluate_all(
        self,
        db: Session,
        *,
        user_id: str = "local-applicant",
    ) -> list[MatchEvaluation]:
        settings = ensure_user_settings(db, user_id=user_id)
        facts = db.scalars(
            select(ProfileFact).where(
                ProfileFact.user_id == user_id,
                ProfileFact.user_verified.is_(True),
            )
        ).all()
        real_facts = [fact for fact in facts if not fact.is_fictional_demo]

        def profile_signals(profile_facts: list[ProfileFact]) -> tuple[list[str], set[str]]:
            corpus = " ".join(f"{fact.key} {fact.value}" for fact in profile_facts)
            skills = {
                skill for skill in KNOWN_SKILLS if re.search(rf"\b{re.escape(skill)}\b", corpus.lower())
            }
            return _tokenize(corpus), skills

        all_signals = profile_signals(list(facts))
        real_signals = profile_signals(real_facts)
        live_ids = set(db.scalars(select(JobSourceRecord.job_id).where(JobSourceRecord.is_mocked.is_(False))))

        jobs = db.scalars(select(JobPosting).order_by(JobPosting.discovered_at.desc())).all()
        results: list[MatchEvaluation] = []
        now = datetime.now(UTC)

        for job in jobs:
            is_live = job.id in live_ids
            verified_tokens, verified_skills = real_signals if is_live else all_signals
            reqs = db.scalars(select(JobRequirement).where(JobRequirement.job_id == job.id)).all()
            req_terms = [r.term for r in reqs if r.kind == "required"]
            pref_terms = [r.term for r in reqs if r.kind == "preferred"]
            if not req_terms:
                req_terms = [
                    s for s in KNOWN_SKILLS if re.search(rf"\b{re.escape(s)}\b", f"{job.title} {job.description}".lower())
                ]

            hard_ok, hard_reasons, uncertain = self._run_hard_filters(job, settings)

            matched = sorted({t for t in (req_terms + pref_terms) if t in verified_skills})
            missing_req = sorted({t for t in req_terms if t not in verified_skills})
            missing_pref = sorted({t for t in pref_terms if t not in verified_skills})

            req_cov = (len([t for t in req_terms if t in verified_skills]) / len(set(req_terms))) if req_terms else 0.5
            pref_cov = (len([t for t in pref_terms if t in verified_skills]) / len(set(pref_terms))) if pref_terms else 0.5
            lexical = _tfidf_cosine(_tokenize(f"{job.title} {job.description}"), verified_tokens)

            freshness_bonus = 0.1
            if job.posted_at:
                posted = job.posted_at.replace(tzinfo=UTC) if job.posted_at.tzinfo is None else job.posted_at
                age_days = max(0.0, (now - posted).total_seconds() / 86400.0)
                freshness_bonus = max(0.0, 0.15 * (1.0 - min(age_days, 45.0) / 45.0))

            role_bonus = 0.12 if any(r.lower() in job.title.lower() for r in (settings.target_roles or [])) else 0.0
            raw_score = (0.48 * req_cov) + (0.15 * pref_cov) + (0.15 * lexical) + freshness_bonus + role_bonus
            if not hard_ok:
                raw_score *= 0.25

            existing = db.scalar(
                select(MatchEvaluation).where(
                    MatchEvaluation.job_id == job.id,
                    MatchEvaluation.user_id == user_id,
                )
            )
            fb_delta = existing.feedback_delta if existing else 0.0
            final_score = round(max(0.0, min(100.0, (raw_score + fb_delta) * 100.0)), 1)

            explanation = (
                f"Compatibilidade de competências verificadas ({len(matched)}/{len(set(req_terms + pref_terms)) or 1}). "
                f"Requisitos atendidos: {', '.join(matched) if matched else 'nenhum explícito'}. "
                f"Lacunas obrigatórias: {', '.join(missing_req) if missing_req else 'nenhuma'}."
            )
            if is_live and not real_facts:
                explanation = "Importe e confirme seu currículo para calcular a aderência ao seu perfil."
            if not hard_ok:
                explanation = f"Filtrada por critérios objetivos: {'; '.join(hard_reasons)}. " + explanation

            if existing is None:
                existing = MatchEvaluation(
                    job_id=job.id,
                    user_id=user_id,
                )
                db.add(existing)

            existing.passed_hard_filters = hard_ok
            existing.hard_filter_reasons = hard_reasons
            existing.relevance_score = final_score
            existing.matched_skills = matched
            existing.missing_skills = missing_req + [f"{m} (desejável)" for m in missing_pref]
            existing.uncertain_items = uncertain
            existing.explanation = explanation
            existing.evaluated_at = now
            results.append(existing)

        db.commit()
        return results

    def _run_hard_filters(
        self, job: JobPosting, settings: UserSettings
    ) -> tuple[bool, list[str], list[str]]:
        reasons: list[str] = []
        uncertain: list[str] = []

        if job.is_expired:
            reasons.append("Vaga expirada (>60 dias)")

        excluded = {c.strip().lower() for c in (settings.excluded_companies or []) if c.strip()}
        if job.company.lower() in excluded:
            reasons.append("Empresa na lista de exclusão")

        # Seniority check: reject clearly senior/staff roles when user prefers junior/trainee/intern
        pref_sen = {s.lower() for s in (settings.seniority_preferences or [])}
        if (
            pref_sen
            and pref_sen.issubset({"junior", "entry_level", "trainee", "intern"})
            and (
                job.seniority == "senior"
                or any(
                    w in job.title.lower()
                    for w in ("staff", "principal", "sênior", "senior", "lead")
                )
            )
        ):
            reasons.append("Senioridade acima da preferência configurada")

        if job.workplace_type == "unknown":
            uncertain.append("Modalidade (remoto/híbrido/presencial) não informada")
        elif (
            settings.workplace_preferences
            and job.workplace_type not in settings.workplace_preferences
            and job.workplace_type == "onsite"
            and "onsite" not in settings.workplace_preferences
        ):
            reasons.append(f"Modalidade presencial fora das preferências ({settings.workplace_preferences})")

        if settings.salary_floor is not None:
            if job.salary_max is None and job.salary_min is None:
                uncertain.append("Faixa salarial não divulgada (não desclassifica automaticamente)")
            else:
                top = job.salary_max if job.salary_max is not None else job.salary_min
                if top is not None and top < settings.salary_floor:
                    reasons.append(f"Salário abaixo do piso ({top} < {settings.salary_floor})")

        return len(reasons) == 0, reasons, uncertain

    def apply_feedback(
        self, db: Session, job_id: str, delta: float, user_id: str = "local-applicant"
    ) -> MatchEvaluation | None:
        ev = db.scalar(
            select(MatchEvaluation).where(
                MatchEvaluation.job_id == job_id,
                MatchEvaluation.user_id == user_id,
            )
        )
        if ev is None:
            return None
        ev.feedback_delta = max(-0.3, min(0.3, ev.feedback_delta + delta))
        ev.relevance_score = round(max(0.0, min(100.0, ev.relevance_score + (delta * 100.0))), 1)
        db.commit()
        db.refresh(ev)
        return ev

    @staticmethod
    def diversify_ranked_jobs(
        items: list[tuple[JobPosting, MatchEvaluation | None]],
        *,
        max_per_family: int = 8,
    ) -> list[tuple[JobPosting, MatchEvaluation | None]]:
        """Promote diversity among job families while keeping highest relevance first."""
        buckets: dict[str, int] = {}
        primary: list[tuple[JobPosting, MatchEvaluation | None]] = []
        overflow: list[tuple[JobPosting, MatchEvaluation | None]] = []
        for job, ev in items:
            fam = _job_family(job.title)
            count = buckets.get(fam, 0)
            if count < max_per_family:
                primary.append((job, ev))
                buckets[fam] = count + 1
            else:
                overflow.append((job, ev))
        return primary + overflow
