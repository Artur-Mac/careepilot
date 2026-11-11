"""Background discovery scheduler with start/stop/pause, jitter, quotas, and restart recovery."""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import Settings
from backend.app.models.tables import SchedulerRun, UserSettings
from backend.app.schemas.contracts import SearchQuery
from backend.app.services.discovery.service import DiscoveryService
from backend.app.services.matching.service import MatchingService, ensure_user_settings


class DiscoveryScheduler:
    """Managed background discovery worker with safe lifecycle and restart recovery."""

    def __init__(
        self, settings: Settings, session_factory: sessionmaker[Any]
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.paused = False
        self.running = False

    def recover_stale_runs(self) -> int:
        """Mark any leftover 'running' SchedulerRun rows from an unclean restart as 'failed_recovered'."""
        with self.session_factory() as db:
            stale = db.scalars(
                select(SchedulerRun).where(SchedulerRun.status == "running")
            ).all()
            for r in stale:
                r.status = "failed_recovered"
                r.notes = "Recuperado após reinício do processo."
                r.finished_at = datetime.now(UTC)
            db.commit()
            return len(stale)

    async def start(self) -> None:
        self.recover_stale_runs()
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self.running = True
        self.paused = False
        with self.session_factory() as db:
            us = ensure_user_settings(db)
            us.scheduler_enabled = True
            db.commit()
        self._task = asyncio.create_task(self._loop(), name="careerpilot-discovery-scheduler")

    async def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with self.session_factory() as db:
            us = ensure_user_settings(db)
            us.scheduler_enabled = False
            db.commit()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    async def run_once(self) -> dict[str, Any]:
        """Execute one bounded discovery + matching cycle with DB run tracking."""
        with self.session_factory() as db:
            us = ensure_user_settings(db)
            run = SchedulerRun(status="running", notes="Ciclo de descoberta em andamento")
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id
            query_text = " ".join((us.target_roles or ["Data Engineer", "Backend Python"])[:2])

        try:
            with self.session_factory() as db:
                discovery = DiscoveryService(self.settings)
                summary = await discovery.run_discovery(
                    db,
                    SearchQuery(text=query_text, limit=50),
                    use_live_network=False,
                )
                MatchingService().evaluate_all(db)
                run_row = db.scalar(select(SchedulerRun).where(SchedulerRun.id == run_id))
                if run_row is not None:
                    raw_inserted = summary.get("inserted")
                    raw_sources = summary.get("sources_run")
                    sources = [str(s) for s in raw_sources] if isinstance(raw_sources, list) else []
                    run_row.status = "completed"
                    run_row.jobs_discovered = int(raw_inserted) if isinstance(raw_inserted, (int, float)) else 0
                    run_row.notes = f"Fontes: {', '.join(sources)}"
                    run_row.finished_at = datetime.now(UTC)
                    db.commit()
                return summary
        except Exception as exc:
            with self.session_factory() as db:
                run_row = db.scalar(select(SchedulerRun).where(SchedulerRun.id == run_id))
                if run_row is not None:
                    run_row.status = "failed"
                    run_row.notes = str(exc)[:400]
                    run_row.finished_at = datetime.now(UTC)
                    db.commit()
            return {"error": str(exc)}

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.paused:
                await self.run_once()
            with self.session_factory() as db:
                us = db.scalar(select(UserSettings).where(UserSettings.user_id == "local-applicant"))
                hours = us.scheduler_interval_hours if us else self.settings.scheduler_interval_hours
            # 6-12h default interval plus bounded jitter to avoid bursty polling
            interval_sec = max(60.0, float(hours) * 3600.0 + random.uniform(5.0, 90.0))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_sec)
            except TimeoutError:
                continue

    def status(self) -> dict[str, Any]:
        with self.session_factory() as db:
            last_runs = db.scalars(
                select(SchedulerRun).order_by(SchedulerRun.started_at.desc()).limit(5)
            ).all()
            us = ensure_user_settings(db)
            return {
                "running": self.running,
                "paused": self.paused,
                "enabled_in_settings": us.scheduler_enabled,
                "interval_hours": us.scheduler_interval_hours,
                "recent_runs": [
                    {
                        "id": r.id,
                        "status": r.status,
                        "jobs_discovered": r.jobs_discovered,
                        "notes": r.notes,
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    }
                    for r in last_runs
                ],
            }
