"""Master Profile persistence with CAS concurrency control and file import."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.tables import AuditEvent, MasterProfile, UserSettings
from backend.app.schemas.profile import (
    MAX_IMPORT_TEXT_CHARS,
    EducationEntry,
    EvidenceRef,
    ExperienceBullet,
    ExperienceEntry,
    ImportedDocumentEntry,
    LanguageEntry,
    MasterProfileDocument,
    ProjectEntry,
    SkillEntry,
    StandardAnswerEntry,
    new_item_id,
)

KNOWN_SKILLS: tuple[str, ...] = (
    "python",
    "sql",
    "fastapi",
    "etl",
    "databricks",
    "azure",
    "power bi",
    "pandas",
    "pytest",
    "sqlalchemy",
    "git",
    "docker",
    "machine learning",
    "dbt",
    "spark",
    "airflow",
    "aws",
    "gcp",
    "kubernetes",
    "rust",
    "typescript",
    "javascript",
    "react",
    "postgresql",
)

_GOV_ID_LINE_RE = re.compile(r"\b(cpf|rg\s*:|cnh|passaporte|passport)\b", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)\d{4,5}[\s.-]?\d{4}")
_URL_RE = re.compile(r"https?://[^\s|,)>\]]+")


class ProfileConflictError(Exception):
    """Raised when a PUT /profile CAS check fails due to a stale revision."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("Conflito de revisão no perfil mestre. Recarregue os dados atuais.")
        self.current = current


def ensure_user_settings(db: Session, user_id: str = "local-applicant") -> UserSettings:
    """Get or create non-fictional user preference defaults."""
    existing = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if existing:
        return existing
    created = UserSettings(user_id=user_id)
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


class ProfileService:
    """Manages JSON MasterProfile documents with compare-and-swap revisions."""

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "document": MasterProfileDocument().model_dump(mode="json"),
            "revision": 0,
            "updated_at": None,
        }

    def get_profile(self, db: Session, user_id: str = "local-applicant") -> dict[str, Any]:
        """Return `{document, revision, updated_at}`; missing row has revision 0."""
        row = db.scalar(select(MasterProfile).where(MasterProfile.user_id == user_id))
        if not row:
            return self._empty_state()
        doc = MasterProfileDocument.model_validate(row.document or {})
        updated_at = row.updated_at.isoformat() if row.updated_at else None
        return {
            "document": doc.model_dump(mode="json"),
            "revision": int(row.revision),
            "updated_at": updated_at,
        }

    def get_document(self, db: Session, user_id: str = "local-applicant") -> tuple[MasterProfileDocument, int]:
        """Return validated `MasterProfileDocument` and current revision."""
        row = db.scalar(select(MasterProfile).where(MasterProfile.user_id == user_id))
        if not row:
            return MasterProfileDocument(), 0
        return MasterProfileDocument.model_validate(row.document or {}), int(row.revision)

    def put_profile(
        self,
        db: Session,
        *,
        document: MasterProfileDocument,
        revision: int,
        user_id: str = "local-applicant",
        audit_event: str = "profile.updated",
        commit: bool = True,
    ) -> dict[str, Any]:
        """Atomically update or initialize the master profile using CAS on `revision`."""
        serialized = document.model_dump(mode="json")
        now_utc = datetime.now(UTC)

        if revision == 0:
            row = MasterProfile(
                user_id=user_id,
                document=serialized,
                revision=1,
                updated_at=now_utc,
            )
            db.add(row)
            try:
                db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type=audit_event,
                        entity_type="master_profile",
                        entity_id=user_id,
                        details={"revision": 1},
                    )
                )
                if commit:
                    db.commit()
                    db.refresh(row)
                else:
                    db.flush()
            except IntegrityError as exc:
                db.rollback()
                raise ProfileConflictError(self.get_profile(db, user_id=user_id)) from exc
            return {
                "document": serialized,
                "revision": 1 if not commit else int(row.revision),
                "updated_at": now_utc.isoformat()
                if not commit
                else (row.updated_at.isoformat() if row.updated_at else now_utc.isoformat()),
            }

        next_rev = revision + 1
        stmt = (
            update(MasterProfile)
            .where(MasterProfile.user_id == user_id, MasterProfile.revision == revision)
            .values(document=serialized, revision=next_rev, updated_at=now_utc)
        )
        from typing import cast

        from sqlalchemy.engine import CursorResult

        result = cast(CursorResult[Any], db.execute(stmt))
        if result.rowcount != 1:
            db.rollback()
            raise ProfileConflictError(self.get_profile(db, user_id=user_id))

        db.add(
            AuditEvent(
                actor=user_id,
                event_type=audit_event,
                entity_type="master_profile",
                entity_id=user_id,
                details={"revision": next_rev},
            )
        )
        if commit:
            db.commit()
        else:
            db.flush()
        return {
            "document": serialized,
            "revision": next_rev,
            "updated_at": now_utc.isoformat(),
        }

    def upsert_standard_answers(
        self,
        db: Session,
        *,
        entries: list[StandardAnswerEntry],
        expected_revision: int | None = None,
        user_id: str = "local-applicant",
        commit: bool = True,
    ) -> dict[str, Any]:
        """Upsert reusable standard answers by question_signature with CAS revision check."""
        doc, current_rev = self.get_document(db, user_id=user_id)
        if expected_revision is not None and current_rev != expected_revision:
            raise ProfileConflictError(self.get_profile(db, user_id=user_id))

        updated_list = list(doc.standard_answers)
        now_iso = datetime.now(UTC).isoformat()

        for entry in entries:
            matched_idx: int | None = None
            for idx, existing in enumerate(updated_list):
                if entry.question_signature and existing.question_signature == entry.question_signature:
                    matched_idx = idx
                    break
                if existing.label.strip().lower() == entry.label.strip().lower():
                    matched_idx = idx
                    break
            normalized_entry = StandardAnswerEntry(
                id=updated_list[matched_idx].id if matched_idx is not None else entry.id,
                question_signature=entry.question_signature,
                label=entry.label[:400],
                answer=entry.answer[:4000],
                kind=entry.kind,
                source=entry.source or "application_copilot",
                updated_at=now_iso,
            )
            if matched_idx is not None:
                updated_list[matched_idx] = normalized_entry
            else:
                updated_list.append(normalized_entry)

        updated_doc = doc.model_copy(update={"standard_answers": updated_list})
        validated_doc = MasterProfileDocument.model_validate(updated_doc.model_dump(mode="python"))
        return self.put_profile(
            db,
            document=validated_doc,
            revision=expected_revision if expected_revision is not None else current_rev,
            user_id=user_id,
            audit_event="profile.standard_answers_upserted",
            commit=commit,
        )

    def import_file(
        self,
        db: Session,
        *,
        filename: str,
        raw_bytes: bytes,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Extract lossless text within size bounds, deduplicate by SHA-256, and merge conservatively."""
        from backend.app.services.resume.service import extract_text_from_uploaded_bytes

        safe_name = Path(filename or "curriculo.txt").name
        chunks = extract_text_from_uploaded_bytes(safe_name, raw_bytes)
        if not chunks:
            raise ValueError("Nenhum texto legível encontrado no documento.")

        full_text = "\n\n".join(txt for _, txt in chunks).strip()
        if not full_text:
            raise ValueError("Nenhum texto legível encontrado no documento.")
        if len(full_text) > MAX_IMPORT_TEXT_CHARS:
            raise ValueError(
                f"Texto extraído ({len(full_text)} caracteres) excede o limite de {MAX_IMPORT_TEXT_CHARS}."
            )

        doc_sha = hashlib.sha256(raw_bytes).hexdigest()

        for attempt in range(2):
            current_doc, current_rev = self.get_document(db, user_id=user_id)
            if any(imp.sha256 == doc_sha for imp in current_doc.imports):
                state = self.get_profile(db, user_id=user_id)
                return {
                    **state,
                    "import_summary": {
                        "filename": safe_name,
                        "sha256": doc_sha,
                        "deduplicated": True,
                        "added_skills": 0,
                        "added_experience": 0,
                        "added_education": 0,
                        "added_projects": 0,
                        "added_languages": 0,
                    },
                }

            import_entry = ImportedDocumentEntry(
                id=new_item_id("imp"),
                sha256=doc_sha,
                filename=safe_name,
                extracted_text=full_text,
                kind="resume_import",
                source=f"upload:{safe_name}",
                imported_at=datetime.now(UTC).isoformat(),
            )
            merged_doc, stats = self._merge_parsed_import(
                current_doc,
                import_entry=import_entry,
                chunks=chunks,
                filename=safe_name,
                doc_sha=doc_sha,
            )
            try:
                saved = self.put_profile(
                    db,
                    document=merged_doc,
                    revision=current_rev,
                    user_id=user_id,
                    audit_event="profile.imported",
                )
                return {**saved, "import_summary": {"filename": safe_name, "sha256": doc_sha, "deduplicated": False, **stats}}
            except ProfileConflictError:
                if attempt == 1:
                    raise

        raise RuntimeError("Falha inesperada ao salvar importação no perfil.")

    def _merge_parsed_import(
        self,
        doc: MasterProfileDocument,
        *,
        import_entry: ImportedDocumentEntry,
        chunks: list[tuple[str, str]],
        filename: str,
        doc_sha: str,
    ) -> tuple[MasterProfileDocument, dict[str, int]]:
        updated = doc.model_copy(deep=True)
        updated.imports.append(import_entry)
        full_text = import_entry.extracted_text

        # Populate blank singleton contact fields without overwriting existing user values
        lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
        first_section = chunks[0][0] if chunks else "Documento"

        if not updated.contact.name and lines:
            candidate_name = lines[0].lstrip("#").strip()
            if (
                2 <= len(candidate_name) <= 90
                and "@" not in candidate_name
                and "http" not in candidate_name.lower()
                and not _GOV_ID_LINE_RE.search(candidate_name)
            ):
                updated.contact.name = candidate_name

        if not updated.contact.email:
            email_match = _EMAIL_RE.search(full_text)
            if email_match:
                updated.contact.email = email_match.group(0)

        if not updated.contact.phone:
            for ln in lines[:12]:
                if _GOV_ID_LINE_RE.search(ln):
                    continue
                phone_match = _PHONE_RE.search(ln)
                if phone_match:
                    updated.contact.phone = phone_match.group(0).strip()
                    break

        for url in _URL_RE.findall(full_text):
            low_u = url.lower()
            if "linkedin.com" in low_u and not updated.contact.links.linkedin:
                updated.contact.links.linkedin = url.rstrip(".,;")
            elif "github.com" in low_u and not updated.contact.links.github:
                updated.contact.links.github = url.rstrip(".,;")
            elif (
                any(k in low_u for k in ("portfolio", "vercel.app", "netlify.app", "github.io"))
                and not updated.contact.links.portfolio
            ):
                updated.contact.links.portfolio = url.rstrip(".,;")

        # Parse structured sections conservatively
        added_exp = 0
        added_edu = 0
        added_proj = 0
        added_lang = 0
        added_skills = 0

        existing_exp_texts = {e.details.strip().lower() for e in updated.experience if e.details}
        existing_edu_texts = {e.details.strip().lower() for e in updated.education if e.details}
        existing_proj_texts = {p.details.strip().lower() for p in updated.projects if p.details}
        existing_skill_names = {s.name.strip().lower() for s in updated.skills}
        existing_lang_names = {lang.name.strip().lower() for lang in updated.languages}

        section_mode = "general"
        for section_name, chunk_text in chunks:
            for raw_line in chunk_text.splitlines():
                stripped = raw_line.strip()
                if not stripped or _GOV_ID_LINE_RE.search(stripped):
                    continue
                low = stripped.lower()
                clean_hdr = stripped.lstrip("#").strip().rstrip(":").lower()
                if len(clean_hdr) <= 40 and any(
                    k in clean_hdr for k in ("experiência", "experiencia", "experience")
                ):
                    section_mode = "experience"
                    continue
                if len(clean_hdr) <= 40 and any(
                    k in clean_hdr for k in ("formação", "formacao", "educação", "educacao", "education")
                ):
                    section_mode = "education"
                    continue
                if len(clean_hdr) <= 40 and any(k in clean_hdr for k in ("projeto", "projects")):
                    section_mode = "project"
                    continue
                if len(clean_hdr) <= 40 and any(k in clean_hdr for k in ("idioma", "language")):
                    section_mode = "language"
                    continue
                if len(clean_hdr) <= 40 and any(k in clean_hdr for k in ("competência", "competencia", "habilidade", "skills")):
                    section_mode = "skills"
                    continue
                if len(clean_hdr) <= 40 and any(k in clean_hdr for k in ("resumo", "summary", "sobre")):
                    section_mode = "general"
                    continue

                if section_mode == "skills":
                    for token in re.split(r"[,;|/]+", stripped.lstrip("-•* ")):
                        t = token.strip()
                        if 1 < len(t) <= 60 and t.lower() not in existing_skill_names:
                            updated.skills.append(
                                SkillEntry(id=new_item_id("sk"), name=t, source=f"import:{filename}")
                            )
                            existing_skill_names.add(t.lower())
                            added_skills += 1
                    continue

                if section_mode == "language":
                    item_txt = stripped.lstrip("-•* ").strip()
                    if 3 <= len(item_txt) <= 120 and item_txt.lower() not in existing_lang_names:
                        updated.languages.append(
                            LanguageEntry(
                                id=new_item_id("lang"),
                                name=item_txt,
                                source=f"import:{filename}",
                            )
                        )
                        existing_lang_names.add(item_txt.lower())
                        added_lang += 1
                    continue

                clean_line = stripped.lstrip("-•*# ").strip()
                if len(clean_line) < 12 or len(clean_line) > 500:
                    continue
                if "@" in clean_line and "http" not in clean_line and len(clean_line) < 100:
                    continue

                ev = EvidenceRef(
                    document_name=filename,
                    section_or_page=section_name or first_section,
                    snippet=clean_line[:240],
                    source_sha256=doc_sha,
                )
                norm_low = clean_line.lower()
                if section_mode == "education" or any(
                    w in low for w in ("bacharel", "graduação", "mestrado", "universidade", "faculdade", "b.sc", "degree")
                ):
                    if norm_low not in existing_edu_texts and len(updated.education) < 25:
                        updated.education.append(
                            EducationEntry(
                                id=new_item_id("edu"),
                                degree=clean_line[:160],
                                details=clean_line,
                                source=f"import:{filename}",
                                evidence=[ev],
                            )
                        )
                        existing_edu_texts.add(norm_low)
                        added_edu += 1
                elif section_mode == "project" or any(w in low for w in ("projeto", "project", "repositório")):
                    if norm_low not in existing_proj_texts and len(updated.projects) < 30:
                        proj_name = clean_line.split(":", 1)[0].strip()[:120] or "Projeto"
                        updated.projects.append(
                            ProjectEntry(
                                id=new_item_id("proj"),
                                name=proj_name,
                                details=clean_line,
                                bullets=[ExperienceBullet(id=new_item_id("pb"), text=clean_line[:580])],
                                source=f"import:{filename}",
                                evidence=[ev],
                            )
                        )
                        existing_proj_texts.add(norm_low)
                        added_proj += 1
                elif (
                    (section_mode == "experience" or len(clean_line) >= 24)
                    and norm_low not in existing_exp_texts
                    and len(updated.experience) < 40
                ):
                    role_title = clean_line.split("—", 1)[0].strip()[:120]
                    updated.experience.append(
                        ExperienceEntry(
                            id=new_item_id("exp"),
                            role=role_title if len(role_title) < 90 else None,
                            details=clean_line,
                            bullets=[ExperienceBullet(id=new_item_id("eb"), text=clean_line[:580])],
                            source=f"import:{filename}",
                            evidence=[ev],
                        )
                    )
                    existing_exp_texts.add(norm_low)
                    added_exp += 1

        text_lower = full_text.lower()
        for skill in KNOWN_SKILLS:
            if re.search(rf"\b{re.escape(skill)}\b", text_lower) and skill not in existing_skill_names:
                canonical = skill.upper() if skill in {"sql", "etl", "aws", "gcp"} else skill.title()
                if skill == "fastapi":
                    canonical = "FastAPI"
                elif skill == "pytest":
                    canonical = "pytest"
                elif skill == "sqlalchemy":
                    canonical = "SQLAlchemy"
                elif skill == "power bi":
                    canonical = "Power BI"
                elif skill == "typescript":
                    canonical = "TypeScript"
                elif skill == "javascript":
                    canonical = "JavaScript"
                elif skill == "postgresql":
                    canonical = "PostgreSQL"
                updated.skills.append(
                    SkillEntry(id=new_item_id("sk"), name=canonical, source=f"import:{filename}")
                )
                existing_skill_names.add(skill)
                added_skills += 1

        return updated, {
            "added_skills": added_skills,
            "added_experience": added_exp,
            "added_education": added_edu,
            "added_projects": added_proj,
            "added_languages": added_lang,
        }
