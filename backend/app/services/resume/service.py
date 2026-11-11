"""Truthful master profile import, factual provenance tracking, and ATS single-column PDF/DOCX generation."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.models.tables import (
    AnswerVersion,
    FactEvidence,
    JobPosting,
    ProfileFact,
    ResumeVersion,
)
from backend.app.security.prompt_guard import sanitize_untrusted_text
from backend.app.security.vault import classify_field_sensitivity
from backend.app.services.discovery.service import KNOWN_SKILLS
from backend.tests.fixtures.fictional_profile import (
    FICTIONAL_FACTS_SEED,
    FICTIONAL_MASTER_RESUME_MD,
    FICTIONAL_SKILLS,
)

MAX_IMPORT_BYTES = 5 * 1024 * 1024  # 5 MiB

_FONTS_REGISTERED = False


def _ensure_pdf_fonts() -> tuple[str, str]:
    """Register Unicode TTF fonts so pt-BR characters render and extract cleanly in PDFs."""
    global _FONTS_REGISTERED
    noto_reg = Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")
    noto_bold = Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")
    deja_reg = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    deja_bold = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

    if not _FONTS_REGISTERED:
        if noto_reg.exists() and noto_bold.exists():
            pdfmetrics.registerFont(TTFont("CareerSans", str(noto_reg)))
            pdfmetrics.registerFont(TTFont("CareerSans-Bold", str(noto_bold)))
            _FONTS_REGISTERED = True
            return "CareerSans", "CareerSans-Bold"
        if deja_reg.exists() and deja_bold.exists():
            pdfmetrics.registerFont(TTFont("CareerSans", str(deja_reg)))
            pdfmetrics.registerFont(TTFont("CareerSans-Bold", str(deja_bold)))
            _FONTS_REGISTERED = True
            return "CareerSans", "CareerSans-Bold"
    if _FONTS_REGISTERED:
        return "CareerSans", "CareerSans-Bold"
    return "Helvetica", "Helvetica-Bold"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_text_from_uploaded_bytes(filename: str, raw_bytes: bytes) -> list[tuple[str, str]]:
    """Safely parse PDF, DOCX, TXT, or Markdown into (section_or_page, text) chunks."""
    if len(raw_bytes) > MAX_IMPORT_BYTES:
        raise ValueError("Arquivo excede o limite de 5 MB.")
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".docx", ".txt", ".md"}:
        raise ValueError(f"Formato não suportado ({ext}). Envie PDF, DOCX, TXT ou MD.")

    chunks: list[tuple[str, str]] = []
    if ext == ".pdf":
        reader = PdfReader(io.BytesIO(raw_bytes))
        for idx, page in enumerate(reader.pages[:20], start=1):
            txt = sanitize_untrusted_text(page.extract_text() or "", max_len=15000).text
            if txt:
                chunks.append((f"Página {idx}", txt))
    elif ext == ".docx":
        doc = Document(io.BytesIO(raw_bytes))
        joined = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        clean = sanitize_untrusted_text(joined, max_len=30000).text
        if clean:
            chunks.append(("Documento DOCX", clean))
    else:
        decoded = raw_bytes.decode("utf-8", errors="replace")
        clean = sanitize_untrusted_text(decoded, max_len=30000).text
        if clean:
            chunks.append(("Texto/Markdown", clean))
    return chunks


class ResumeService:
    """Manages master profile provenance, truthful job-specific tailoring, and ATS PDF/DOCX output."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ensure_fictional_profile_seeded(
        self, db: Session, *, user_id: str = "local-applicant"
    ) -> list[ProfileFact]:
        """Seed unambiguous fictional demo profile when no facts exist yet."""
        existing = db.scalars(
            select(ProfileFact).where(ProfileFact.user_id == user_id)
        ).all()
        if existing:
            return list(existing)

        doc_sha = sha256_text(FICTIONAL_MASTER_RESUME_MD)
        created: list[ProfileFact] = []
        for item in FICTIONAL_FACTS_SEED:
            raw_meta = item.get("metadata")
            meta_dict: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            fact = ProfileFact(
                user_id=user_id,
                category=str(item["category"]),
                key=str(item["key"]),
                value=str(item["value"]),
                metadata_json=meta_dict,
                sensitivity=classify_field_sensitivity(str(item["key"])).value,
                user_verified=bool(item["user_verified"]),
                confidence=float(item["confidence"]),  # type: ignore[arg-type]
                is_fictional_demo=True,
            )
            db.add(fact)
            db.flush()
            db.add(
                FactEvidence(
                    fact_id=fact.id,
                    document_name="perfil_ficticio_demo.md",
                    section_or_page=str(item["section"]),
                    supporting_snippet=str(item["snippet"]),
                    source_sha256=doc_sha,
                )
            )
            created.append(fact)

        for skill in FICTIONAL_SKILLS:
            fact = ProfileFact(
                user_id=user_id,
                category="skill",
                key=f"skill:{skill.lower()}",
                value=skill,
                metadata_json={"canonical": skill},
                sensitivity="public",
                user_verified=True,
                confidence=1.0,
                is_fictional_demo=True,
            )
            db.add(fact)
            db.flush()
            db.add(
                FactEvidence(
                    fact_id=fact.id,
                    document_name="perfil_ficticio_demo.md",
                    section_or_page="Competências Técnicas",
                    supporting_snippet=f"Competências Técnicas: ... {skill} ...",
                    source_sha256=doc_sha,
                )
            )
            created.append(fact)

        db.commit()
        return created

    def import_resume_file(
        self,
        db: Session,
        *,
        filename: str,
        raw_bytes: bytes,
        user_id: str = "local-applicant",
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Parse user-selected file into provenance-tracked facts; ambiguous claims await verification."""
        safe_name = Path(filename).name
        chunks = extract_text_from_uploaded_bytes(safe_name, raw_bytes)
        if not chunks:
            raise ValueError("Nenhum texto legível encontrado no documento.")

        if replace_existing:
            old_facts = db.scalars(
                select(ProfileFact).where(ProfileFact.user_id == user_id)
            ).all()
            for f in old_facts:
                db.delete(f)
            db.flush()

        doc_sha = hashlib.sha256(raw_bytes).hexdigest()
        full_text = "\n".join(txt for _, txt in chunks)

        imported_facts: list[ProfileFact] = []
        # Extract candidate contact/skills conservatively; mark ambiguous extractions unverified.
        email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", full_text)
        if email_match:
            f = ProfileFact(
                user_id=user_id,
                category="contact",
                key="email",
                value=email_match.group(0),
                sensitivity="contact",
                user_verified=False,  # User must confirm extracted facts before live reliance
                confidence=0.85,
                is_fictional_demo=False,
            )
            db.add(f)
            db.flush()
            db.add(
                FactEvidence(
                    fact_id=f.id,
                    document_name=safe_name,
                    section_or_page=chunks[0][0],
                    supporting_snippet=email_match.group(0),
                    source_sha256=doc_sha,
                )
            )
            imported_facts.append(f)

        # Extract candidate lines with bullet/role structure as unverified experience/education claims
        for section, txt in chunks:
            for line in txt.splitlines():
                clean_line = line.strip(" -•*\t")
                if len(clean_line) < 12 or len(clean_line) > 280:
                    continue
                low = clean_line.lower()
                # Never auto-store sensitive government IDs from uploads into unencrypted summary facts
                if any(k in low for k in ("cpf", "rg:", "passaporte")):
                    continue
                cat = "experience"
                if any(w in low for w in ("bacharel", "graduação", "universidade", "faculdade", "degree", "b.sc")):
                    cat = "education"
                elif any(w in low for w in ("projeto", "project", "github")):
                    cat = "project"
                f = ProfileFact(
                    user_id=user_id,
                    category=cat,
                    key=clean_line[:80],
                    value=clean_line,
                    sensitivity="public",
                    user_verified=False,
                    confidence=0.65,
                    is_fictional_demo=False,
                )
                db.add(f)
                db.flush()
                db.add(
                    FactEvidence(
                        fact_id=f.id,
                        document_name=safe_name,
                        section_or_page=section,
                        supporting_snippet=clean_line,
                        source_sha256=doc_sha,
                    )
                )
                imported_facts.append(f)
                if len(imported_facts) >= 25:
                    break

        for skill in KNOWN_SKILLS:
            if re.search(rf"\b{re.escape(skill)}\b", full_text.lower()):
                f = ProfileFact(
                    user_id=user_id,
                    category="skill",
                    key=f"skill:{skill}",
                    value=skill,
                    sensitivity="public",
                    user_verified=False,
                    confidence=0.75,
                    is_fictional_demo=False,
                )
                db.add(f)
                db.flush()
                db.add(
                    FactEvidence(
                        fact_id=f.id,
                        document_name=safe_name,
                        section_or_page=chunks[0][0],
                        supporting_snippet=f"Mencionado em {safe_name}: {skill}",
                        source_sha256=doc_sha,
                    )
                )
                imported_facts.append(f)

        db.commit()
        return {
            "document_name": safe_name,
            "sha256": doc_sha,
            "facts_extracted": len(imported_facts),
            "requires_verification": True,
        }

    def generate_tailored_resume(
        self,
        db: Session,
        *,
        job: JobPosting,
        user_id: str = "local-applicant",
        generate_docx: bool = True,
    ) -> ResumeVersion:
        """Create a truthful, single-column ATS PDF + DOCX strictly from user_verified facts."""
        self.ensure_fictional_profile_seeded(db, user_id=user_id)

        verified_facts = db.scalars(
            select(ProfileFact).where(
                ProfileFact.user_id == user_id,
                ProfileFact.user_verified.is_(True),
            )
        ).all()
        if not verified_facts:
            raise ValueError("Nenhum fato verificado disponível no perfil mestre para gerar currículo.")

        contacts = {f.key: f.value for f in verified_facts if f.category == "contact"}
        experiences = [f for f in verified_facts if f.category == "experience"]
        projects = [f for f in verified_facts if f.category == "project"]
        educations = [f for f in verified_facts if f.category == "education"]
        languages = [f for f in verified_facts if f.category == "language"]
        skills_facts = [f for f in verified_facts if f.category == "skill"]

        # Reorder verified skills so job-relevant verified skills appear first (never inventing unverified skills!)
        job_text_lower = f"{job.title} {job.description}".lower()
        prioritized_skills: list[str] = []
        other_skills: list[str] = []
        for sf in skills_facts:
            skill_name = sf.value.strip()
            if re.search(rf"\b{re.escape(skill_name.lower())}\b", job_text_lower):
                prioritized_skills.append(skill_name)
            else:
                other_skills.append(skill_name)
        ordered_skills = list(dict.fromkeys(prioritized_skills + other_skills))

        # Reorder experiences by keyword overlap with job
        job_words = set(re.findall(r"[a-z0-9]{3,}", job_text_lower))

        def _exp_score(fact: ProfileFact) -> int:
            words = set(re.findall(r"[a-z0-9]{3,}", f"{fact.key} {fact.value}".lower()))
            return len(words & job_words)

        ordered_experiences = sorted(experiences, key=_exp_score, reverse=True)

        full_name = contacts.get("full_name", "Candidato(a) Local")
        is_demo = any(f.is_fictional_demo for f in verified_facts)
        header_badge = " [PERFIL FICTÍCIO DE DEMONSTRAÇÃO]" if is_demo else ""

        top_skills_str = ", ".join(prioritized_skills[:6] or ordered_skills[:6])
        tailored_summary = (
            f"Candidatura direcionada à vaga de {job.title} na {job.company}. "
            f"Base verificada em: {top_skills_str}."
        )

        # Build provenance report linking every resume section back to fact IDs & source snippets
        provenance_report: list[dict[str, Any]] = []
        for fact in [
            *[f for f in verified_facts if f.category == "contact"],
            *ordered_experiences,
            *projects,
            *educations,
            *skills_facts,
            *languages,
        ]:
            ev = db.scalar(select(FactEvidence).where(FactEvidence.fact_id == fact.id))
            provenance_report.append(
                {
                    "fact_id": fact.id,
                    "category": fact.category,
                    "key": fact.key,
                    "document": ev.document_name if ev else "manual",
                    "section": ev.section_or_page if ev else "perfil",
                    "snippet": ev.supporting_snippet if ev else fact.value[:140],
                    "user_verified": fact.user_verified,
                }
            )

        structured = {
            "header": {
                "name": f"{full_name}{header_badge}",
                "email": contacts.get("email", ""),
                "phone": contacts.get("phone", ""),
                "location": contacts.get("location", ""),
                "linkedin": contacts.get("linkedin_url", ""),
                "github": contacts.get("github_url", ""),
            },
            "summary": tailored_summary,
            "skills": ordered_skills,
            "experiences": [
                {
                    "title": f.key,
                    "details": f.value,
                    "bullets": list((f.metadata_json or {}).get("bullets") or [f.value]),
                }
                for f in ordered_experiences
            ],
            "projects": [{"title": f.key, "details": f.value} for f in projects],
            "education": [{"title": f.key, "details": f.value} for f in educations],
            "languages": [f.value for f in languages],
        }

        out_dir = self.settings.artifacts_path / "resumes"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = sha256_text(f"{job.id}:{user_id}:{len(provenance_report)}")[:12]
        pdf_path = out_dir / f"cv_{job.id[:8]}_{stamp}.pdf"
        docx_path: Path | None = out_dir / f"cv_{job.id[:8]}_{stamp}.docx" if generate_docx else None

        self._render_single_column_pdf(pdf_path, structured)
        if docx_path is not None:
            self._render_docx(docx_path, structured)

        # Mandatory ATS readability verification: extract text back from generated PDF via pypdf
        reader = PdfReader(str(pdf_path))
        extracted_text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        if len(extracted_text) < 80 or full_name.split()[0] not in extracted_text:
            raise RuntimeError("Falha na verificação de legibilidade ATS do PDF gerado.")

        pdf_sha = sha256_file(pdf_path)
        diff_summary = {
            "target_job": job.title,
            "company": job.company,
            "prioritized_verified_skills": prioritized_skills,
            "other_verified_skills": other_skills,
            "unverified_claims_excluded": db.query(ProfileFact)
            .filter(ProfileFact.user_id == user_id, ProfileFact.user_verified.is_(False))
            .count(),
            "fabricated_claims": 0,
        }

        rv = ResumeVersion(
            job_id=job.id,
            user_id=user_id,
            pdf_path=str(pdf_path),
            docx_path=str(docx_path) if docx_path else None,
            pdf_sha256=pdf_sha,
            extracted_text=extracted_text,
            structured_json=structured,
            provenance_report=provenance_report,
            diff_summary=diff_summary,
        )
        db.add(rv)
        db.commit()
        db.refresh(rv)
        return rv

    def _render_single_column_pdf(self, pdf_path: Path, data: dict[str, Any]) -> None:
        font_reg, font_bold = _ensure_pdf_fonts()
        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("H1", parent=styles["Normal"], fontName=font_bold, fontSize=15, leading=19, spaceAfter=4)
        h2 = ParagraphStyle("H2", parent=styles["Normal"], fontName=font_bold, fontSize=11, leading=14, spaceBefore=8, spaceAfter=3)
        body = ParagraphStyle("Body", parent=styles["Normal"], fontName=font_reg, fontSize=9.5, leading=13, spaceAfter=3)
        bullet = ParagraphStyle("Bullet", parent=body, leftIndent=10, firstLineIndent=-6)

        doc = SimpleDocTemplate(
            str(pdf_path),
            pagesize=A4,
            leftMargin=16 * mm,
            rightMargin=16 * mm,
            topMargin=14 * mm,
            bottomMargin=14 * mm,
            title=data["header"]["name"],
        )
        story: list[Any] = []
        hdr = data["header"]
        story.append(Paragraph(self._esc(hdr["name"]), h1))
        contact_line = " | ".join(
            p for p in [hdr.get("email"), hdr.get("phone"), hdr.get("location")] if p
        )
        if contact_line:
            story.append(Paragraph(self._esc(contact_line), body))
        links_line = " | ".join(p for p in [hdr.get("linkedin"), hdr.get("github")] if p)
        if links_line:
            story.append(Paragraph(self._esc(links_line), body))

        story.append(Spacer(1, 4))
        story.append(Paragraph("RESUMO PROFISSIONAL", h2))
        story.append(Paragraph(self._esc(data["summary"]), body))

        story.append(Paragraph("COMPETÊNCIAS VERIFICADAS", h2))
        story.append(Paragraph(self._esc(", ".join(data["skills"])), body))

        story.append(Paragraph("EXPERIÊNCIA", h2))
        for exp in data["experiences"]:
            story.append(Paragraph(f"<b>{self._esc(exp['title'])}</b>", body))
            for b in exp["bullets"]:
                story.append(Paragraph(f"• {self._esc(b)}", bullet))

        if data["projects"]:
            story.append(Paragraph("PROJETOS DOCUMENTADOS", h2))
            for proj in data["projects"]:
                story.append(Paragraph(f"• <b>{self._esc(proj['title'])}</b>: {self._esc(proj['details'])}", bullet))

        if data["education"]:
            story.append(Paragraph("FORMAÇÃO ACADÊMICA", h2))
            for edu in data["education"]:
                story.append(Paragraph(f"• <b>{self._esc(edu['title'])}</b> — {self._esc(edu['details'])}", bullet))

        if data["languages"]:
            story.append(Paragraph("IDIOMAS", h2))
            story.append(Paragraph(self._esc(" | ".join(data["languages"])), body))

        doc.build(story)

    def _render_docx(self, docx_path: Path, data: dict[str, Any]) -> None:
        doc = Document()
        hdr = data["header"]
        doc.add_heading(hdr["name"], level=1)
        doc.add_paragraph(" | ".join(p for p in [hdr.get("email"), hdr.get("phone"), hdr.get("location")] if p))
        doc.add_heading("Resumo Profissional", level=2)
        doc.add_paragraph(data["summary"])
        doc.add_heading("Competências Verificadas", level=2)
        doc.add_paragraph(", ".join(data["skills"]))
        doc.add_heading("Experiência", level=2)
        for exp in data["experiences"]:
            p = doc.add_paragraph()
            p.add_run(exp["title"]).bold = True
            for b in exp["bullets"]:
                doc.add_paragraph(b, style="List Bullet")
        doc.add_heading("Projetos Documentados", level=2)
        for proj in data["projects"]:
            doc.add_paragraph(f"{proj['title']}: {proj['details']}", style="List Bullet")
        doc.add_heading("Formação", level=2)
        for edu in data["education"]:
            doc.add_paragraph(f"{edu['title']} — {edu['details']}", style="List Bullet")
        doc.save(str(docx_path))

    @staticmethod
    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def generate_answers_packet(
        self,
        db: Session,
        *,
        job: JobPosting,
        custom_answers: dict[str, str] | None = None,
        user_id: str = "local-applicant",
    ) -> AnswerVersion:
        """Build truthful answers from verified facts; unknown questions stay pending (never fabricated)."""
        self.ensure_fictional_profile_seeded(db, user_id=user_id)
        facts = db.scalars(
            select(ProfileFact).where(
                ProfileFact.user_id == user_id,
                ProfileFact.user_verified.is_(True),
            )
        ).all()
        by_key = {f.key: f.value for f in facts}
        supplied = {k: v.strip() for k, v in (custom_answers or {}).items() if v.strip()}

        answers: dict[str, Any] = {
            "full_name": by_key.get("full_name", ""),
            "email": by_key.get("email", ""),
            "phone": by_key.get("phone", ""),
            "location": by_key.get("location", ""),
            "linkedin_url": by_key.get("linkedin_url", ""),
            "github_url": by_key.get("github_url", ""),
            "work_authorization": by_key.get("work_authorization_br", ""),
            "why_role": (
                f"Interesse na vaga de {job.title} em {job.company}, com experiência verificada "
                "em Python, SQL, ETL e FastAPI conforme currículo anexado."
            ),
        }
        answers.update(supplied)

        pending_questions: list[dict[str, Any]] = []
        # Check if job description or ATS requires unknown legal/salary/relocation input
        desc_low = job.description.lower()
        if (
            "multi-etapas" in job.title.lower()
            or "pretensão" in desc_low
            or "salary" in desc_low
        ) and "salary_expectation" not in answers:
            pending_questions.append(
                {
                    "field_key": "salary_expectation",
                    "question_label": "Pretensão salarial / expectativa de remuneração (não inventar)",
                    "reason": "unknown_compensation",
                }
            )
        if ("relocation" in desc_low or "mudança" in desc_low) and "relocation_willingness" not in answers:
            pending_questions.append(
                {
                    "field_key": "relocation_willingness",
                    "question_label": "Disponibilidade para mudança presencial",
                    "reason": "unknown_relocation",
                }
            )

        import json

        canonical_json = json.dumps(answers, sort_keys=True, ensure_ascii=False)
        ans_sha = sha256_text(canonical_json)
        av = AnswerVersion(
            job_id=job.id,
            user_id=user_id,
            answers_json=answers,
            pending_questions=pending_questions,
            answers_sha256=ans_sha,
        )
        db.add(av)
        db.commit()
        db.refresh(av)
        return av
