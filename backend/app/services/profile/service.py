"""Master Profile persistence with CAS concurrency control, file import, and repair preview."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.time import utc_rfc3339
from backend.app.models.tables import AuditEvent, MasterProfile, UserSettings
from backend.app.schemas.profile import (
    MAX_EDUCATION_ITEMS,
    MAX_EXPERIENCE_ITEMS,
    MAX_IMPORT_ITEMS,
    MAX_LANGUAGE_ITEMS,
    MAX_PROJECT_ITEMS,
    MAX_SKILL_ITEMS,
    MAX_STANDARD_ANSWER_ITEMS,
    ImportedDocumentEntry,
    ImportRepairPreviewResponse,
    ImportResult,
    ImportSectionCounts,
    MasterProfileDocument,
    MasterProfileDocumentV2,
    RoutedStandardAnswerEntry,
    StandardAnswerEntry,
    new_item_id,
    upgrade_profile_document_v1_to_v2,
)
from backend.app.services.profile.parser import (
    ParsedResumeResult,
    build_repair_preview,
    normalize_match_key,
    parse_extracted_blocks,
)
from backend.app.services.resume.service import (
    ExtractedBlock,
    ExtractedDocument,
    extract_blocks_from_preserved_text,
    extract_structured_blocks_from_uploaded_bytes,
)


class ProfileConflictError(Exception):
    """Raised when optimistic CAS revision on MasterProfile fails."""

    def __init__(
        self,
        current: dict[str, Any],
        message: str = "Conflito de revisão no Perfil Mestre.",
    ) -> None:
        self.current = current
        super().__init__(message)


class ImportNotFoundError(LookupError):
    """Raised when a referenced imported document does not exist in MasterProfile."""


def ensure_user_settings(db: Session, user_id: str = "local-applicant") -> UserSettings:
    """Return or initialize UserSettings row for the local user."""
    existing = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if existing:
        return existing
    created = UserSettings(user_id=user_id)
    db.add(created)
    db.flush()
    return created


def _collect_existing_ids(doc: MasterProfileDocumentV2) -> set[str]:
    """Collect every entry, bullet, variant, answer, import, and summary id in `doc`."""
    ids: set[str] = set()
    for group in (
        doc.experience,
        doc.education,
        doc.projects,
        doc.skills,
        doc.languages,
        doc.standard_answers,
        doc.imports,
        doc.summaries,
    ):
        for item in group:
            ids.add(item.id)
    for exp in doc.experience:
        for b in exp.bullets:
            ids.add(b.id)
            for v in b.variants:
                ids.add(v.id)
    for proj in doc.projects:
        for pb in proj.bullets:
            ids.add(pb.id)
            for v in pb.variants:
                ids.add(v.id)
    return ids


def _allocate_non_colliding_id(candidate_id: str, prefix: str, used_ids: set[str]) -> str:
    """Return `candidate_id` if free, else generate a non-colliding ID and reserve it."""
    chosen = candidate_id
    while chosen in used_ids:
        chosen = new_item_id(prefix)
    used_ids.add(chosen)
    return chosen


def _parse_chunk_page_number(label: str) -> int | None:
    m = re.match(r"^(?:página|pagina|page)\s+(\d+)\b", label.strip(), re.I)
    return int(m.group(1)) if m else None


class ProfileService:
    """Manages JSON MasterProfile v2 documents with compare-and-swap revisions."""

    @staticmethod
    def _coerce_v2(raw: MasterProfileDocument | dict[str, Any] | None) -> MasterProfileDocumentV2:
        if isinstance(raw, MasterProfileDocumentV2):
            payload: dict[str, Any] = raw.model_dump(mode="python")
        elif isinstance(raw, MasterProfileDocument):
            payload = raw.model_dump(mode="python")
        else:
            payload = dict(raw or {})
        upgraded, _ = upgrade_profile_document_v1_to_v2(payload)
        return MasterProfileDocumentV2.model_validate(upgraded)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "document": MasterProfileDocumentV2().model_dump(mode="json"),
            "revision": 0,
            "updated_at": None,
        }

    def get_profile(self, db: Session, user_id: str = "local-applicant") -> dict[str, Any]:
        """Return `{document, revision, updated_at}`; missing row has revision 0."""
        row = db.scalar(select(MasterProfile).where(MasterProfile.user_id == user_id))
        if not row:
            return self._empty_state()
        doc = self._coerce_v2(row.document or {})
        updated_at = row.updated_at.isoformat() if row.updated_at else None
        return {
            "document": doc.model_dump(mode="json"),
            "revision": int(row.revision),
            "updated_at": updated_at,
        }

    def get_document(self, db: Session, user_id: str = "local-applicant") -> tuple[MasterProfileDocumentV2, int]:
        """Return validated `MasterProfileDocumentV2` and current revision."""
        row = db.scalar(select(MasterProfile).where(MasterProfile.user_id == user_id))
        if not row:
            return MasterProfileDocumentV2(), 0
        return self._coerce_v2(row.document or {}), int(row.revision)

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
        v2_doc = self._coerce_v2(document)
        serialized = v2_doc.model_dump(mode="json")
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
        now_iso = utc_rfc3339()

        for entry in entries:
            matched_idx: int | None = None
            for idx, existing in enumerate(updated_list):
                if entry.question_signature and existing.question_signature == entry.question_signature:
                    matched_idx = idx
                    break
                if existing.label.strip().lower() == entry.label.strip().lower():
                    matched_idx = idx
                    break
            existing_entry = updated_list[matched_idx] if matched_idx is not None else None
            routed_source = entry if isinstance(entry, RoutedStandardAnswerEntry) else None
            normalized_entry = RoutedStandardAnswerEntry(
                id=existing_entry.id if existing_entry is not None else entry.id,
                question_signature=entry.question_signature,
                label=entry.label[:400],
                answer=entry.answer[:4000],
                kind=entry.kind,
                source=entry.source or (existing_entry.source if existing_entry else None) or "application_copilot",
                updated_at=now_iso,
                role_tags=(
                    routed_source.role_tags
                    if routed_source is not None and routed_source.role_tags
                    else (existing_entry.role_tags if existing_entry else [])
                ),
                when_to_use=(
                    routed_source.when_to_use
                    if routed_source is not None and routed_source.when_to_use
                    else (existing_entry.when_to_use if existing_entry else [])
                ),
                when_not_to_use=(
                    routed_source.when_not_to_use
                    if routed_source is not None and routed_source.when_not_to_use
                    else (existing_entry.when_not_to_use if existing_entry else [])
                ),
                language=(
                    routed_source.language
                    if routed_source is not None and routed_source.language is not None
                    else (existing_entry.language if existing_entry else None)
                ),
                source_claim_ids=(
                    routed_source.source_claim_ids
                    if routed_source is not None and routed_source.source_claim_ids
                    else (existing_entry.source_claim_ids if existing_entry else [])
                ),
            )
            if matched_idx is not None:
                updated_list[matched_idx] = normalized_entry
            else:
                updated_list.append(normalized_entry)

        updated_doc = doc.model_copy(update={"standard_answers": updated_list[-MAX_STANDARD_ANSWER_ITEMS:]})
        validated_doc = self._coerce_v2(updated_doc)
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
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Extract structured blocks within bounds, deduplicate by SHA-256, and merge conservatively."""
        safe_name = Path(filename or "curriculo.txt").name
        extracted = extract_structured_blocks_from_uploaded_bytes(safe_name, raw_bytes)
        doc_sha = extracted.source_sha256

        for attempt in range(2):
            current_doc, current_rev = self.get_document(db, user_id=user_id)
            if base_revision is not None and current_rev != base_revision:
                raise ProfileConflictError(self.get_profile(db, user_id=user_id))

            existing_imp = next((imp for imp in current_doc.imports if imp.sha256 == doc_sha), None)
            if existing_imp is not None:
                state = self.get_profile(db, user_id=user_id)
                dup_result = ImportResult(
                    document_id=existing_imp.id,
                    duplicate=True,
                    counts=ImportSectionCounts(),
                    warnings=list(extracted.warnings),
                ).model_dump(mode="json")
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
                    "import_result": dup_result,
                }

            import_entry = ImportedDocumentEntry(
                id=new_item_id("imp"),
                sha256=doc_sha,
                filename=safe_name,
                extracted_text=extracted.full_text,
                kind="resume_import",
                source=f"upload:{safe_name}",
                imported_at=utc_rfc3339(),
            )
            merged_doc, stats, parsed_res = self._merge_extracted_document(
                current_doc,
                import_entry=import_entry,
                extracted=extracted,
                filename=safe_name,
                doc_sha=doc_sha,
            )
            import_result = ImportResult(
                document_id=import_entry.id,
                duplicate=False,
                counts=ImportSectionCounts(
                    experience=stats["added_experience"],
                    education=stats["added_education"],
                    projects=stats["added_projects"],
                    skills=stats["added_skills"],
                    languages=stats["added_languages"],
                    bullets=parsed_res.total_bullets,
                    excluded_blocks=parsed_res.excluded_blocks,
                ),
                warnings=parsed_res.warnings,
            ).model_dump(mode="json")
            try:
                saved = self.put_profile(
                    db,
                    document=merged_doc,
                    revision=current_rev,
                    user_id=user_id,
                    audit_event="profile.imported",
                )
                return {
                    **saved,
                    "import_summary": {
                        "filename": safe_name,
                        "sha256": doc_sha,
                        "deduplicated": False,
                        **stats,
                    },
                    "import_result": import_result,
                }
            except ProfileConflictError:
                if base_revision is not None or attempt == 1:
                    raise

        raise RuntimeError("Falha inesperada ao salvar importação no perfil.")

    def repair_preview(
        self,
        db: Session,
        *,
        document_id: str,
        base_revision: int,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Re-parse a preserved import deterministically without mutating SQLite or user edits."""
        current_doc, current_rev = self.get_document(db, user_id=user_id)
        if current_rev != base_revision:
            raise ProfileConflictError(self.get_profile(db, user_id=user_id))

        imp = next((item for item in current_doc.imports if item.id == document_id), None)
        if imp is None:
            raise ImportNotFoundError(f"Documento importado '{document_id}' não encontrado no perfil.")

        preserved_doc = extract_blocks_from_preserved_text(
            imp.extracted_text,
            filename=imp.filename,
            source_sha256=imp.sha256,
        )
        parsed = parse_extracted_blocks(
            preserved_doc.blocks,
            filename=imp.filename,
            doc_sha=imp.sha256,
            full_text=imp.extracted_text,
            initial_warnings=preserved_doc.warnings,
        )
        proposed_doc, diff = build_repair_preview(
            current_doc,
            document_id=document_id,
            base_revision=base_revision,
            reparsed=parsed,
            filename=imp.filename,
            doc_sha=imp.sha256,
        )
        return ImportRepairPreviewResponse(
            document_id=document_id,
            source_sha256=imp.sha256,
            base_revision=base_revision,
            proposed_document=proposed_doc,
            diff=diff,
            warnings=parsed.warnings,
        ).model_dump(mode="json")

    def _merge_parsed_import(
        self,
        doc: MasterProfileDocument,
        *,
        import_entry: ImportedDocumentEntry,
        chunks: list[tuple[str, str]],
        filename: str,
        doc_sha: str,
    ) -> tuple[MasterProfileDocumentV2, dict[str, int]]:
        """Backward-compatible wrapper around structured block extraction and v2 merge."""
        blocks: list[ExtractedBlock] = []
        ordinal_offset = 0
        for label, chunk_text in chunks:
            page_num = _parse_chunk_page_number(label)
            chunk_doc = extract_blocks_from_preserved_text(
                chunk_text,
                filename=filename,
                source_sha256=doc_sha,
            )
            for blk in chunk_doc.blocks:
                ordinal_offset += 1
                blocks.append(
                    ExtractedBlock(
                        ordinal=ordinal_offset,
                        text=blk.text,
                        page=page_num,
                        section_label=label,
                        heading_level=blk.heading_level,
                        list_marker=blk.list_marker,
                        indent=blk.indent,
                        source_kind=blk.source_kind,
                        source_sha256=doc_sha,
                        blank_before=blk.blank_before,
                    )
                )
        extracted = ExtractedDocument(
            filename=filename,
            source_sha256=doc_sha,
            chunks=chunks,
            blocks=blocks,
        )
        merged, stats, _ = self._merge_extracted_document(
            doc,
            import_entry=import_entry,
            extracted=extracted,
            filename=filename,
            doc_sha=doc_sha,
        )
        return merged, stats

    def _merge_extracted_document(
        self,
        doc: MasterProfileDocument,
        *,
        import_entry: ImportedDocumentEntry,
        extracted: ExtractedDocument,
        filename: str,
        doc_sha: str,
    ) -> tuple[MasterProfileDocumentV2, dict[str, int], ParsedResumeResult]:
        updated = self._coerce_v2(doc).model_copy(deep=True)
        parsed = parse_extracted_blocks(
            extracted.blocks,
            filename=filename,
            doc_sha=doc_sha,
            full_text=extracted.full_text,
            initial_warnings=extracted.warnings,
        )
        used_ids = _collect_existing_ids(updated)
        used_ids.add(import_entry.id)

        # 1. Conservative contact fill (never overwrite existing user values)
        if not (updated.contact.name or "").strip() and parsed.contact_name:
            updated.contact.name = parsed.contact_name
        if not (updated.contact.email or "").strip() and parsed.contact_email:
            updated.contact.email = parsed.contact_email
        if not (updated.contact.phone or "").strip() and parsed.contact_phone:
            updated.contact.phone = parsed.contact_phone
        if not (updated.contact.links.linkedin or "").strip() and parsed.linkedin:
            updated.contact.links.linkedin = parsed.linkedin
        if not (updated.contact.links.github or "").strip() and parsed.github:
            updated.contact.links.github = parsed.github
        if not (updated.contact.links.portfolio or "").strip() and parsed.portfolio:
            updated.contact.links.portfolio = parsed.portfolio

        # 2. Experience merge (dedup by normalized company|role|period)
        added_exp = 0
        existing_exp_sigs = {
            normalize_match_key(f"{e.company or ''}|{e.role or ''}|{e.period or ''}")
            for e in updated.experience
        }
        for exp in parsed.experience:
            if len(updated.experience) >= MAX_EXPERIENCE_ITEMS:
                break
            sig = normalize_match_key(f"{exp.company or ''}|{exp.role or ''}|{exp.period or ''}")
            if sig and sig in existing_exp_sigs:
                continue
            exp_copy = exp.model_copy(deep=True)
            exp_copy.id = _allocate_non_colliding_id(exp_copy.id, "exp", used_ids)
            for b in exp_copy.bullets:
                b.id = _allocate_non_colliding_id(b.id, "eb", used_ids)
            updated.experience.append(exp_copy)
            if sig:
                existing_exp_sigs.add(sig)
            added_exp += 1

        # 3. Education merge
        added_edu = 0
        existing_edu_sigs = {
            normalize_match_key(f"{e.institution or ''}|{e.degree or ''}|{e.period or ''}")
            for e in updated.education
        }
        for edu in parsed.education:
            if len(updated.education) >= MAX_EDUCATION_ITEMS:
                break
            sig = normalize_match_key(f"{edu.institution or ''}|{edu.degree or ''}|{edu.period or ''}")
            if sig and sig in existing_edu_sigs:
                continue
            edu_copy = edu.model_copy(deep=True)
            edu_copy.id = _allocate_non_colliding_id(edu_copy.id, "edu", used_ids)
            updated.education.append(edu_copy)
            if sig:
                existing_edu_sigs.add(sig)
            added_edu += 1

        # 4. Projects merge
        added_proj = 0
        existing_proj_sigs = {normalize_match_key(p.name) for p in updated.projects}
        for proj in parsed.projects:
            if len(updated.projects) >= MAX_PROJECT_ITEMS:
                break
            sig = normalize_match_key(proj.name)
            if sig and sig in existing_proj_sigs:
                continue
            proj_copy = proj.model_copy(deep=True)
            proj_copy.id = _allocate_non_colliding_id(proj_copy.id, "prj", used_ids)
            for pb in proj_copy.bullets:
                pb.id = _allocate_non_colliding_id(pb.id, "pb", used_ids)
            updated.projects.append(proj_copy)
            if sig:
                existing_proj_sigs.add(sig)
            added_proj += 1

        # 5. Skills merge (canonical normalized dedup)
        added_skills = 0
        existing_skills = {normalize_match_key(s.name) for s in updated.skills if s.name.strip()}
        for sk in parsed.skills:
            if len(updated.skills) >= MAX_SKILL_ITEMS:
                break
            key = normalize_match_key(sk.name)
            if not key or key in existing_skills:
                continue
            sk_copy = sk.model_copy(deep=True)
            sk_copy.id = _allocate_non_colliding_id(sk_copy.id, "sk", used_ids)
            updated.skills.append(sk_copy)
            existing_skills.add(key)
            added_skills += 1

        # 6. Languages merge
        added_langs = 0
        existing_langs = {normalize_match_key(lang.name) for lang in updated.languages if lang.name.strip()}
        for lang in parsed.languages:
            if len(updated.languages) >= MAX_LANGUAGE_ITEMS:
                break
            key = normalize_match_key(lang.name)
            if not key or key in existing_langs:
                continue
            lang_copy = lang.model_copy(deep=True)
            lang_copy.id = _allocate_non_colliding_id(lang_copy.id, "lng", used_ids)
            updated.languages.append(lang_copy)
            existing_langs.add(key)
            added_langs += 1

        updated.imports = [*updated.imports, import_entry][-MAX_IMPORT_ITEMS:]
        validated = MasterProfileDocumentV2.model_validate(updated.model_dump(mode="python"))
        stats = {
            "added_skills": added_skills,
            "added_experience": added_exp,
            "added_education": added_edu,
            "added_projects": added_proj,
            "added_languages": added_langs,
        }
        return validated, stats, parsed
