"""Unit tests for Phase 1 Pydantic v2 strict contracts, structural extraction, and deterministic parser."""

from __future__ import annotations

import hashlib
import io
from datetime import date

import docx
import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from backend.app.core.time import utc_rfc3339
from backend.app.schemas.profile import (
    ArsenalPayload,
    BaseAnswerArsenal,
    FeedbackEvidence,
    FieldApproval,
    FieldSnapshot,
    ImportRepairPreviewResponse,
    ImportResult,
    JobDiscoveryFeedback,
    LengthVariants,
    MasterProfileDocumentV2,
    PendingManualAction,
    PortalSessionStatus,
    RouteClause,
    RoutedBullet,
    RoutedExperienceEntry,
    SalaryApproval,
    SalaryBand,
    SalaryRangeValue,
    SalarySuggestion,
    TuningRuleSet,
    VettedText,
)
from backend.app.services.profile.parser import parse_extracted_blocks
from backend.app.services.resume.service import (
    MAX_IMPORT_BYTES,
    extract_structured_blocks_from_uploaded_bytes,
)


def _pdf_bytes_from_pages(pages_lines: list[list[str]], *, encrypted_password: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for idx, lines in enumerate(pages_lines):
        if idx > 0:
            c.showPage()
        y = 780
        if not lines:
            c.rect(60, 660, 120, 80, fill=1)
        for line in lines:
            c.drawString(50, y, line)
            y -= 18
    c.save()
    raw = buf.getvalue()
    if encrypted_password is None:
        return raw

    writer = PdfWriter()
    writer.append(io.BytesIO(raw))
    writer.encrypt(encrypted_password)
    enc_buf = io.BytesIO()
    writer.write(enc_buf)
    return enc_buf.getvalue()


def test_phase1_all_contracts_export_json_schema_and_enforce_strict_invariants() -> None:
    models = (
        MasterProfileDocumentV2,
        BaseAnswerArsenal,
        ArsenalPayload,
        PendingManualAction,
        PortalSessionStatus,
        SalaryBand,
        SalarySuggestion,
        SalaryRangeValue,
        SalaryApproval,
        FieldApproval,
        FieldSnapshot,
        JobDiscoveryFeedback,
        TuningRuleSet,
        ImportResult,
        ImportRepairPreviewResponse,
    )
    for model in models:
        schema = model.model_json_schema()
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"

    # Reject unknown fields recursively and empty RouteClause
    with pytest.raises(ValidationError):
        RouteClause.model_validate({})
    with pytest.raises(ValidationError):
        RouteClause.model_validate({"archetype_in": ["data_engineer"], "unknown_field": 1})

    # StrictInt/StrictBool reject coercive strings or bools-as-ints
    with pytest.raises(ValidationError):
        SalaryRangeValue.model_validate({"min_minor": "1000", "max_minor": 2000})
    with pytest.raises(ValidationError):
        SalaryRangeValue.model_validate({"min_minor": True, "max_minor": 2000})
    with pytest.raises(ValidationError):
        SalaryRangeValue.model_validate({"min_minor": 5000, "max_minor": 4000})
    with pytest.raises(ValidationError):
        FieldSnapshot.model_validate({"label": "Salario", "control": "text", "required": "yes"})
    with pytest.raises(ValidationError):
        FieldSnapshot.model_validate(
            {"label": "Bio", "control": "textarea", "required": False, "min_chars": 50, "max_chars": 20}
        )

    # LengthVariants + slot declarations
    with pytest.raises(ValidationError):
        LengthVariants.model_validate({})
    with pytest.raises(ValidationError):
        ArsenalPayload.model_validate(
            {
                "length_variants": {"short": "Gosto da {{company_name}}"},
                "trigger_keywords": ["why"],
                "slots": [],
                "provenance": "user",
            }
        )

    # MasterProfileDocumentV2 UTF-16 summary cap (🚀 uses 2 UTF-16 code units -> 251 * 2 = 502 > 500)
    with pytest.raises(ValidationError):
        MasterProfileDocumentV2.model_validate(
            {
                "experience": [
                    {
                        "id": "exp-1",
                        "company": "Visagio",
                        "role": "Engenheiro de Dados",
                        "bullets": [{"id": "eb-1", "text": "Pipeline FinOps em Azure."}],
                    }
                ],
                "summaries": [
                    {
                        "id": "sum-over",
                        "language": "pt-BR",
                        "text": "🚀" * 251,
                        "source_claim_ids": ["eb-1"],
                    }
                ],
            }
        )

    # Non-canonical variant id in source_claim_ids must fail
    with pytest.raises(ValidationError):
        MasterProfileDocumentV2.model_validate(
            {
                "experience": [
                    {
                        "id": "exp-1",
                        "company": "Visagio",
                        "bullets": [
                            {
                                "id": "eb-1",
                                "text": "Pipeline Azure",
                                "variants": [
                                    {
                                        "id": "var-1",
                                        "language": "en",
                                        "text": "Azure pipeline",
                                        "source_claim_ids": ["eb-1"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "summaries": [
                    {
                        "id": "sum-1",
                        "language": "pt-BR",
                        "text": "Resumo válido",
                        "source_claim_ids": ["var-1"],
                    }
                ],
            }
        )

    # PendingManualAction / PortalSessionStatus / SalaryApproval invariants
    now = utc_rfc3339()
    with pytest.raises(ValidationError):
        PortalSessionStatus.model_validate(
            {
                "portal_key": "linkedin",
                "scope_key": "default",
                "status": "authenticated",
                "checked_at": now,
                "expires_at": now,
                "revision": 1,
            }
        )
    with pytest.raises(ValidationError):
        PendingManualAction.model_validate(
            {
                "id": "act-1",
                "application_id": "app-1",
                "step_key": "step-1",
                "field_key": "f-1",
                "field_fingerprint": "a" * 64,
                "kind": "sensitive",
                "status": "approved",
                "field": {"label": "CPF", "control": "text", "required": True},
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "approved_at": None,
            }
        )
    SalaryBand.model_validate(
        {
            "id": "band-1",
            "role_archetype": "data_engineer",
            "seniority": "junior",
            "contract": "CLT",
            "currency": "BRL",
            "period": "monthly",
            "min_minor": 600_000,
            "max_minor": 850_000,
            "basis": "gross",
            "source_note": "Pesquisa local",
            "as_of": date(2026, 11, 1),
        }
    )
    SalarySuggestion.model_validate(
        {"contract": "CLT", "seniority": "junior", "reason_code": "missing_band"}
    )
    SalaryApproval.model_validate(
        {"kind": "salary", "shape": "amount", "value": 750_000, "currency": "BRL", "period": "monthly"}
    )
    FieldApproval.model_validate({"kind": "boolean", "value": True})
    JobDiscoveryFeedback.model_validate(
        {
            "id": "fb-1",
            "job_id": "job-1",
            "action": "high_fit",
            "operation": "apply",
            "evidence": FeedbackEvidence(
                archetype="data_engineer",
                seniority="junior",
                skill_keys=["python"],
                company_key="visagio",
                workplace="remote",
                normalized_city="recife",
            ),
            "created_at": now,
        }
    )
    TuningRuleSet.model_validate({"keyword_weights": {"python": 10}})
    assert isinstance(RoutedBullet(id="b1", text="Bullet"), RoutedBullet)
    assert isinstance(RoutedExperienceEntry(id="e1", company="Visagio"), RoutedExperienceEntry)
    assert isinstance(VettedText(id="v1", language="pt-BR", text="Resumo", source_claim_ids=[]), VettedText)


def test_deterministic_parser_21_fragment_regression_orphan_warning_skills_and_languages() -> None:
    # Includes leading orphan bullet before any section, 3 experiences with (-, *, •),
    # 2 projects with bullets, C++/C#/.NET skills + blank punctuation skill noise, and languages.
    resume_text = (
        "• Bullet orfao antes de qualquer experiencia.\n"
        "Artur Albuquerque Machado\n"
        "arturmac10@gmail.com | +55 (81) 99781-1014\n"
        "https://www.linkedin.com/in/artur-albuquerque-machado/\n"
        "https://github.com/ArturAlbuquerqueMachado\n\n"
        "## Experiência Profissional\n"
        "Visagio - Engenheiro de Dados (Estagiário) | Out 2025 - Presente\n"
        "- Desenvolvimento de pipelines em Azure Databricks e Synapse Analytics.\n"
        "* Otimizacao FinOps reduzindo 40% dos custos de clusters em producao.\n"
        "• Observabilidade e integracoes SAP/SQL via Azure Monitor.\n"
        "Grupo Iquine - Estagiário de Engenharia de Dados | Ago 2025 - Set 2025\n"
        "- Extracao IDP com PaddleOCR e fine-tuned spaCy NER.\n"
        "• Automacao operacional via Python, SQL e APIs Fluig.\n"
        "Núcleo de Apoio à Tecnologia da Informação (NATI - CCM / UFPE) - Assistente de Suporte Técnico | Abr 2025 - Ago 2025\n"
        "- Administracao Linux/Windows e chamados N1/N2 no OTRS.\n\n"
        "## Formação Acadêmica\n"
        "Universidade Federal de Pernambuco (UFPE) - Bacharelado em Ciência da Computação | 2022 - 2026\n\n"
        "## Projetos\n"
        "CareerPilot - Copiloto local-first\n"
        "- Orquestracao FastAPI + SQLite WAL + CDP com aprovacao humana.\n"
        "• Geracao deterministica de curriculos PDF com verificacao de proveniencia.\n"
        "SentinelFinOps - Monitoramento Azure\n"
        "* Alertas de orcamento e custo ocioso em Databricks.\n\n"
        "## Habilidades\n"
        "Python, SQL, Azure Databricks, C++, C#, .NET, ---, •,   \n\n"
        "## Idiomas\n"
        "Português (Nativo), Inglês - Fluente, Espanhol: Intermediário\n"
    ).encode()

    extracted = extract_structured_blocks_from_uploaded_bytes("curriculo_artur.md", resume_text)
    parsed = parse_extracted_blocks(
        extracted.blocks,
        filename=extracted.filename,
        doc_sha=extracted.source_sha256,
        full_text=extracted.full_text,
        initial_warnings=extracted.warnings,
    )

    # Zero fragmented ghost entries!
    assert len(parsed.experience) == 3
    assert parsed.experience[0].company == "Visagio"
    assert len(parsed.experience[0].bullets) == 3
    assert parsed.experience[1].company == "Grupo Iquine"
    assert len(parsed.experience[1].bullets) == 2
    assert "NATI - CCM / UFPE" in (parsed.experience[2].company or "")
    assert len(parsed.experience[2].bullets) == 1
    assert all(
        (exp.company or "").strip() or (exp.role or "").strip() or (exp.period or "").strip()
        for exp in parsed.experience
    )

    assert len(parsed.education) == 1
    assert "UFPE" in (parsed.education[0].institution or "")
    assert len(parsed.projects) == 2
    assert len(parsed.projects[0].bullets) == 2
    assert len(parsed.projects[1].bullets) == 1

    # Orphan bullet captured as localized warning with coordinates rather than ghost experience
    orphan_warnings = [w for w in parsed.warnings if w.code == "orphan_bullet"]
    assert len(orphan_warnings) == 1
    assert orphan_warnings[0].section == "general"
    assert orphan_warnings[0].line == 1

    skill_names = [s.name for s in parsed.skills]
    assert {"Python", "SQL", "Azure Databricks", "C++", "C#", ".NET"}.issubset(skill_names)
    assert all(any(ch.isalnum() for ch in name) for name in skill_names)

    lang_map = {lang.name: lang.proficiency for lang in parsed.languages}
    assert lang_map["Português"] == "Nativo"
    assert lang_map["Inglês"] == "Fluente"
    assert lang_map["Espanhol"] == "Intermediário"


def test_docx_order_merged_cell_dedup_and_pdf_multipage_wrapped_bullets() -> None:
    # 1. DOCX with heading, paragraph, list bullet, and merged-cell table in document order
    d = docx.Document()
    d.add_paragraph("Marina Costa — marina@example.org")
    d.add_heading("Experiência Profissional", level=2)
    d.add_paragraph("TechCo — Engenheira Backend | Jan 2024 - Atual")
    d.add_paragraph("Construção de APIs FastAPI com SQLite WAL.", style="List Bullet")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Habilidades"
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(1, 0).text = "Python, SQL, C++"
    table.cell(1, 1).text = "Docker, Git"

    docx_buf = io.BytesIO()
    d.save(docx_buf)
    ext_docx = extract_structured_blocks_from_uploaded_bytes("marina.docx", docx_buf.getvalue())
    parsed_docx = parse_extracted_blocks(
        ext_docx.blocks,
        filename=ext_docx.filename,
        doc_sha=ext_docx.source_sha256,
        full_text=ext_docx.full_text,
        initial_warnings=ext_docx.warnings,
    )
    assert len(parsed_docx.experience) == 1
    assert len(parsed_docx.experience[0].bullets) == 1
    # Merged cell text should only appear once
    assert sum(1 for b in ext_docx.blocks if b.text == "Habilidades") == 1
    assert {"Python", "SQL", "C++", "Docker", "Git"}.issubset({s.name for s in parsed_docx.skills})

    # 2. Multi-page PDF with wrapped bullet across page boundary + Page 3 image-only + Layout check
    multipage_pdf = _pdf_bytes_from_pages(
        [
            [
                "Artur Machado",
                "Experiência Profissional",
                "Visagio - Engenheiro de Dados | 2025 - Atual",
                "- Pipeline de ingestao distribuida utilizando Azure Databricks,",
            ],
            [
                "PySpark e Delta Lake sob arquitetura Medallion.",
                "- Otimizacao de consultas analiticas em Synapse.",
            ],
            [],  # image-only / empty page -> warning without crashing extraction
        ]
    )
    ext_pdf = extract_structured_blocks_from_uploaded_bytes("curriculo.pdf", multipage_pdf)
    parsed_pdf = parse_extracted_blocks(
        ext_pdf.blocks,
        filename=ext_pdf.filename,
        doc_sha=ext_pdf.source_sha256,
        full_text=ext_pdf.full_text,
        initial_warnings=ext_pdf.warnings,
    )
    assert len(parsed_pdf.experience) == 1
    assert len(parsed_pdf.experience[0].bullets) == 2
    assert "arquitetura Medallion" in parsed_pdf.experience[0].bullets[0].text
    assert any(w.code == "image_only_page" and w.page == 3 for w in parsed_pdf.warnings)

    # 3. Encrypted PDF, all-image PDF, and oversized upload failures
    enc_pdf = _pdf_bytes_from_pages([["Confidencial"]], encrypted_password="secret")
    with pytest.raises(ValueError, match="criptografado"):
        extract_structured_blocks_from_uploaded_bytes("enc.pdf", enc_pdf)

    img_only_pdf = _pdf_bytes_from_pages([[]])
    with pytest.raises(ValueError, match="OCR"):
        extract_structured_blocks_from_uploaded_bytes("scan.pdf", img_only_pdf)

    with pytest.raises(ValueError, match="5 MB"):
        extract_structured_blocks_from_uploaded_bytes("huge.txt", b"x" * (MAX_IMPORT_BYTES + 1))

    # 4. PDF > 20 pages triggers warning while still parsing pages 1..20
    big_pdf = _pdf_bytes_from_pages(
        [["Clara Nunes", "Habilidades", "Python, SQL"]] + [[f"Nota pagina {n}"] for n in range(2, 23)]
    )
    ext_big = extract_structured_blocks_from_uploaded_bytes("big.pdf", big_pdf)
    assert any(w.code == "pdf_page_limit" and w.page == 20 for w in ext_big.warnings)
    assert hashlib.sha256(big_pdf).hexdigest() == ext_big.source_sha256
