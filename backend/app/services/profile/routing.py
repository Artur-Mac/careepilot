"""Shared evaluation of typed routes against the current vacancy."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.app.schemas.profile import Archetype, Locale, RouteClause
from backend.app.services.discovery.matching import includes, match_key


@dataclass(frozen=True)
class JobContext:
    title: str
    text: str
    archetype: Archetype
    locale: Locale
    skills: frozenset[str]
    seniority: str
    contract: str

    @classmethod
    def from_job(
        cls, title: str, description: str, locale: Locale, skills: list[str],
        seniority: str = "", contract: str = "",
    ) -> JobContext:
        title_key = match_key(title)
        text = match_key(f"{title} {description}")
        archetype: Archetype = "all"
        if re.search(r"\b(data engineer|engenheir[oa] de dados|engenharia de dados)\b", title_key):
            archetype = "data_engineer"
        elif re.search(r"\b(backend|back end)\b", title_key) and includes(text, "python"):
            archetype = "backend_python"
        elif re.search(r"\b(machine learning|data scientist|cientista de dados)\b", title_key):
            archetype = "ml_engineer"
        elif re.search(r"\b(estagio|internship|intern|junior)\b", title_key):
            archetype = "intern_junior"
        level = match_key(seniority)
        level = {"pleno": "mid", "estagio": "intern"}.get(level, level)
        return cls(title, text, archetype, locale, frozenset(
            key for skill in skills if includes(text, key := match_key(skill))
        ), level, match_key(contract))

    def _matches(self, rule: RouteClause) -> bool | None:
        if (
            (rule.locales and self.locale not in rule.locales)
            or (rule.archetype_in and self.archetype != "all" and self.archetype not in rule.archetype_in and "all" not in rule.archetype_in)
            or (rule.seniority_in and self.seniority not in {"", "unknown"} and self.seniority not in rule.seniority_in)
            or (rule.contract_in and self.contract not in {"", "unknown"} and self.contract not in map(match_key, rule.contract_in))
            or any(match_key(skill) not in self.skills for skill in rule.all_skills)
            or (rule.any_skills and not any(match_key(skill) in self.skills for skill in rule.any_skills))
            or any(not includes(self.text, match_key(keyword)) for keyword in rule.all_keywords)
        ):
            return False
        if (
            (rule.seniority_in and self.seniority in {"", "unknown"})
            or (rule.contract_in and self.contract in {"", "unknown"})
            or (rule.archetype_in and self.archetype == "all" and "all" not in rule.archetype_in)
        ):
            return None
        return True

    def allows(self, include: list[RouteClause], exclude: list[RouteClause]) -> bool:
        return (not include or any(self._matches(rule) is True for rule in include)) and all(
            self._matches(rule) is False for rule in exclude
        )
