"""Shared sample MasterProfile fixture factories for Phase 1-3 automated tests."""

from __future__ import annotations

from backend.app.schemas.profile import (
    ContactInfo,
    ContactLinks,
    EducationEntry,
    EvidenceRef,
    ExperienceBullet,
    ExperienceEntry,
    LanguageEntry,
    MasterProfileDocument,
    PreferencesInfo,
    ProjectEntry,
    SkillEntry,
    StandardAnswerEntry,
)


def build_sample_master_profile() -> MasterProfileDocument:
    """Create a representative local-candidate MasterProfileDocument with Portuguese accents and portfolio."""
    return MasterProfileDocument(
        contact=ContactInfo(
            name="Clara Nogueira Sales",
            email="clara.nogueira@example.org",
            phone="+55 81 99999-0123",
            city="Recife",
            state="PE",
            country="Brasil",
            links=ContactLinks(
                linkedin="https://linkedin.com/in/clara-nogueira-exemplo",
                github="https://github.com/clara-nogueira-exemplo",
                portfolio="https://clara.dev.example.org",
            ),
        ),
        experience=[
            ExperienceEntry(
                id="exp-1",
                company="Porto Digital Analytics",
                role="Engenheira de Dados Júnior",
                period="Jan 2024 – Atualmente",
                location="Recife, PE",
                details="Construção de pipelines ETL e APIs analíticas para dados públicos.",
                bullets=[
                    ExperienceBullet(
                        id="bul-1",
                        text="Implementou pipelines ETL em Python e SQL com testes em pytest e validação de esquema.",
                    ),
                    ExperienceBullet(
                        id="bul-2",
                        text="Desenvolveu serviços FastAPI com SQLAlchemy e PostgreSQL para consulta operacional.",
                    ),
                ],
                source="manual",
                evidence=[
                    EvidenceRef(
                        document_name="curriculo_clara.txt",
                        section_or_page="Experiência",
                        snippet="Implementou pipelines ETL em Python e SQL com testes em pytest.",
                    )
                ],
            )
        ],
        education=[
            EducationEntry(
                id="edu-1",
                institution="Universidade Federal de Pernambuco",
                degree="Bacharelado em Ciência da Computação",
                period="2020 – 2024",
                details="Ênfase em Engenharia de Dados e Bancos de Dados.",
                source="manual",
            )
        ],
        projects=[
            ProjectEntry(
                id="prj-1",
                name="Monitor Transporte Recife",
                url="https://github.com/clara-nogueira-exemplo/monitor-recife",
                details="Pipeline analítico em Python, SQL e FastAPI.",
                bullets=[
                    ExperienceBullet(
                        id="bul-p1",
                        text="Ingestão incremental com deduplicação determinística.",
                    )
                ],
                source="manual",
            )
        ],
        skills=[
            SkillEntry(id="skl-python", name="Python", level="Avançado", source="manual"),
            SkillEntry(id="skl-sql", name="SQL", level="Avançado", source="manual"),
            SkillEntry(id="skl-fastapi", name="FastAPI", level="Intermediário", source="manual"),
            SkillEntry(id="skl-etl", name="ETL", level="Intermediário", source="manual"),
            SkillEntry(id="skl-pytest", name="pytest", level="Intermediário", source="manual"),
        ],
        languages=[
            LanguageEntry(id="lng-pt", name="Português", proficiency="Nativo", source="manual"),
            LanguageEntry(id="lng-en", name="Inglês", proficiency="Avançado", source="manual"),
        ],
        preferences=PreferencesInfo(
            target_roles=["Engenheira de Dados Júnior", "Backend Python"],
            location="Recife, PE",
            work_model=["remote", "hybrid"],
            seniority=["junior", "pleno"],
            contract_types=["clt", "pj"],
        ),
        standard_answers=[
            StandardAnswerEntry(
                id="ans-1",
                question_signature="work_authorization",
                label="Possui autorização para trabalhar no Brasil?",
                answer="Sim",
                kind="boolean",
                source="manual",
            )
        ],
    )
