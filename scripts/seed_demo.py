"""Seed local database with fictional demo profile, 36+ fixture vacancies, and ranked evaluations."""

from __future__ import annotations

import asyncio

from backend.app.core.config import get_settings
from backend.app.db.base import Base
from backend.app.db.session import get_engine, get_session_factory
from backend.app.schemas.contracts import SearchQuery
from backend.app.services.discovery.service import DiscoveryService
from backend.app.services.matching.service import MatchingService
from backend.app.services.resume.service import ResumeService


async def _run() -> None:
    settings = get_settings()
    engine = get_engine(settings)
    Base.metadata.create_all(bind=engine)
    factory = get_session_factory(settings)
    with factory() as db:
        facts = ResumeService(settings).ensure_fictional_profile_seeded(db)
        summary = await DiscoveryService(settings).run_discovery(
            db, SearchQuery(text="data engineer python sql", limit=60), use_live_network=False
        )
        evals = MatchingService().evaluate_all(db)
        print(
            f"Seed concluído (APENAS DADOS FICTÍCIOS): {len(facts)} fatos, "
            f"{summary['inserted']} vagas inseridas ({summary['deduped']} duplicadas), "
            f"{len(evals)} avaliações."
        )


if __name__ == "__main__":
    asyncio.run(_run())
