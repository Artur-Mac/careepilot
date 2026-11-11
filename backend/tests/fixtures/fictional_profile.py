"""Fictional demo applicant profile and provenance fixtures (ZERO real user PII)."""

from __future__ import annotations

FICTIONAL_APPLICANT_NAME = "Marina Costa Duarte (FICTÍCIO — DADOS DE TESTE)"
FICTIONAL_APPLICANT_EMAIL = "marina.duarte.teste@example.invalid"
FICTIONAL_APPLICANT_PHONE = "+55 31 99999-0101"
FICTIONAL_APPLICANT_LOCATION = "Belo Horizonte, MG — Brasil (Remoto)"
FICTIONAL_APPLICANT_LINKEDIN = "https://example.invalid/in/marina-duarte-demo"
FICTIONAL_APPLICANT_GITHUB = "https://example.invalid/github/marina-duarte-demo"

FICTIONAL_MASTER_RESUME_MD = """# Marina Costa Duarte (Perfil Fictício de Demonstração)
Email: marina.duarte.teste@example.invalid | Telefone: +55 31 99999-0101
Localização: Belo Horizonte, MG — Brasil (Disponível para Remoto)
LinkedIn: https://example.invalid/in/marina-duarte-demo
GitHub: https://example.invalid/github/marina-duarte-demo

## Resumo
Profissional em início de carreira (Júnior / Trainee) com foco em Engenharia de Dados, Analytics Engineering e Desenvolvimento Backend Python. Experiência acadêmica e de estágio construindo pipelines ETL com Python, SQL, FastAPI, Databricks e dashboards em Power BI.

## Experiência
### Estagiária de Engenharia de Dados — DadosFictícios Ltda (Jan 2025 – Out 2026)
- Desenvolveu pipelines ETL em Python e SQL para ingestão diária de dados públicos em camada bronze/prata.
- Construiu APIs internas de consulta analítica utilizando FastAPI, Pydantic e SQLite/PostgreSQL.
- Criou modelos dimensionais e painéis operacionais no Power BI conectados ao Azure Data Lake.

### Monitora de Banco de Dados e Python — Universidade Fictícia de Minas (Mar 2024 – Dez 2024)
- Auxiliou estudantes em modelagem relacional, consultas SQL otimizadas e automação de scripts em Python.

## Projetos Documentados
- **Radar Público ETL**: Pipeline em Python + SQL + pytest para normalização e deduplicação de datasets abertos.
- **API Analítica FastAPI**: Serviço REST local com SQLAlchemy, testes automatizados e documentação OpenAPI.

## Formação
- Bacharelado em Sistemas de Informação — Universidade Fictícia de Minas (Conclusão prevista: Dez 2026).

## Competências Técnicas
Python, SQL, FastAPI, ETL, Pandas, Power BI, Databricks, Azure, Git, pytest, Docker básico, Machine Learning introdutório.

## Idiomas
- Português (Nativo)
- Inglês (Intermediário / Leitura técnica avançada)
"""

FICTIONAL_FACTS_SEED: list[dict[str, object]] = [
    {
        "category": "contact",
        "key": "full_name",
        "value": "Marina Costa Duarte",
        "snippet": "Marina Costa Duarte (Perfil Fictício de Demonstração)",
        "section": "Cabeçalho",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "contact",
        "key": "email",
        "value": FICTIONAL_APPLICANT_EMAIL,
        "snippet": f"Email: {FICTIONAL_APPLICANT_EMAIL}",
        "section": "Cabeçalho",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "contact",
        "key": "phone",
        "value": FICTIONAL_APPLICANT_PHONE,
        "snippet": f"Telefone: {FICTIONAL_APPLICANT_PHONE}",
        "section": "Cabeçalho",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "contact",
        "key": "location",
        "value": "Belo Horizonte, MG, Brasil",
        "snippet": "Localização: Belo Horizonte, MG — Brasil (Disponível para Remoto)",
        "section": "Cabeçalho",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "contact",
        "key": "linkedin_url",
        "value": FICTIONAL_APPLICANT_LINKEDIN,
        "snippet": f"LinkedIn: {FICTIONAL_APPLICANT_LINKEDIN}",
        "section": "Cabeçalho",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "contact",
        "key": "github_url",
        "value": FICTIONAL_APPLICANT_GITHUB,
        "snippet": f"GitHub: {FICTIONAL_APPLICANT_GITHUB}",
        "section": "Cabeçalho",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "work_auth",
        "key": "work_authorization_br",
        "value": "Autorizada a trabalhar no Brasil (Cidadã fictícia de teste)",
        "snippet": "Localização: Belo Horizonte, MG — Brasil (Disponível para Remoto)",
        "section": "Elegibilidade",
        "user_verified": True,
        "confidence": 0.95,
    },
    {
        "category": "experience",
        "key": "DadosFictícios Ltda — Estagiária de Engenharia de Dados (Jan 2025 – Out 2026)",
        "value": "Desenvolveu pipelines ETL em Python e SQL; construiu APIs analíticas com FastAPI e SQLAlchemy; criou modelos no Power BI com Azure Data Lake.",
        "snippet": "Estagiária de Engenharia de Dados — DadosFictícios Ltda (Jan 2025 – Out 2026)",
        "section": "Experiência",
        "user_verified": True,
        "confidence": 1.0,
        "metadata": {
            "company": "DadosFictícios Ltda",
            "role": "Estagiária de Engenharia de Dados",
            "period": "Jan 2025 – Out 2026",
            "bullets": [
                "Desenvolveu pipelines ETL em Python e SQL para ingestão diária de dados públicos em camada bronze/prata.",
                "Construiu APIs internas de consulta analítica utilizando FastAPI, Pydantic e SQLite/PostgreSQL.",
                "Criou modelos dimensionais e painéis operacionais no Power BI conectados ao Azure Data Lake.",
            ],
        },
    },
    {
        "category": "experience",
        "key": "Universidade Fictícia de Minas — Monitora de Banco de Dados e Python (Mar 2024 – Dez 2024)",
        "value": "Auxiliou estudantes em modelagem relacional, consultas SQL otimizadas e automação de scripts em Python.",
        "snippet": "Monitora de Banco de Dados e Python — Universidade Fictícia de Minas (Mar 2024 – Dez 2024)",
        "section": "Experiência",
        "user_verified": True,
        "confidence": 1.0,
        "metadata": {
            "company": "Universidade Fictícia de Minas",
            "role": "Monitora de Banco de Dados e Python",
            "period": "Mar 2024 – Dez 2024",
            "bullets": [
                "Auxiliou estudantes em modelagem relacional, consultas SQL otimizadas e automação de scripts em Python.",
            ],
        },
    },
    {
        "category": "project",
        "key": "Radar Público ETL",
        "value": "Pipeline em Python, SQL e pytest para normalização e deduplicação de datasets abertos.",
        "snippet": "Radar Público ETL: Pipeline em Python + SQL + pytest para normalização e deduplicação de datasets abertos.",
        "section": "Projetos Documentados",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "project",
        "key": "API Analítica FastAPI",
        "value": "Serviço REST local com FastAPI, SQLAlchemy, testes automatizados e documentação OpenAPI.",
        "snippet": "API Analítica FastAPI: Serviço REST local com SQLAlchemy, testes automatizados e documentação OpenAPI.",
        "section": "Projetos Documentados",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "education",
        "key": "Bacharelado em Sistemas de Informação — Universidade Fictícia de Minas",
        "value": "Conclusão prevista: Dez 2026 — Universidade Fictícia de Minas",
        "snippet": "Bacharelado em Sistemas de Informação — Universidade Fictícia de Minas (Conclusão prevista: Dez 2026).",
        "section": "Formação",
        "user_verified": True,
        "confidence": 1.0,
    },
    {
        "category": "language",
        "key": "Idiomas",
        "value": "Português (Nativo), Inglês (Intermediário / Leitura técnica)",
        "snippet": "Português (Nativo) | Inglês (Intermediário / Leitura técnica avançada)",
        "section": "Idiomas",
        "user_verified": True,
        "confidence": 1.0,
    },
]

FICTIONAL_SKILLS: list[str] = [
    "Python",
    "SQL",
    "FastAPI",
    "ETL",
    "Pandas",
    "Power BI",
    "Databricks",
    "Azure",
    "Git",
    "pytest",
    "SQLAlchemy",
    "Docker",
    "Machine Learning",
]
