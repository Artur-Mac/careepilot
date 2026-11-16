"""OpenCode CLI + browser extension (`browse`) integration and persistent chat service."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.core.time import utc_rfc3339
from backend.app.models.tables import Application, ArsenalRespostaRow, JobPosting, ResumeVersion
from backend.app.services.profile.service import ProfileService
from backend.app.services.resume.service import resolve_safe_resume_path


class OpenCodeUnavailableError(RuntimeError):
    """Raised when the local OpenCode CLI binary cannot be located."""


@dataclass
class _ChatRunState:
    run_id: str
    user_id: str
    thread_id: str
    status: str = "running"  # running | completed | failed | cancelled
    partial_text: str = ""
    tool_events: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    opencode_session_id: str | None = None
    created_at: str = field(default_factory=utc_rfc3339)
    updated_at: str = field(default_factory=utc_rfc3339)
    task: asyncio.Task[None] | None = None
    proc: asyncio.subprocess.Process | None = None


_RUNS: dict[str, _ChatRunState] = {}
_THREAD_LOCKS: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    lock = _THREAD_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _THREAD_LOCKS[key] = lock
    return lock


def resolve_opencode_cli() -> str | None:
    """Locate the local opencode-cli binary across standard installation paths."""
    env_bin = os.environ.get("OPENCODE_CLI_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    for name in ("opencode-cli", "opencode"):
        found = shutil.which(name)
        if found:
            return found

    desktop_cli_glob = sorted(
        (Path.home() / ".config" / "ai.opencode.desktop" / "cli").glob("*/opencode-cli"),
        reverse=True,
    )
    for cand in desktop_cli_glob:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)

    opt_cli = Path("/opt/OpenCode/resources/opencode-cli")
    if opt_cli.is_file() and os.access(opt_cli, os.X_OK):
        return str(opt_cli)
    return None


def resolve_browse_bin() -> str | None:
    """Locate the local `browse` OpenCode browser extension relay binary."""
    env_bin = os.environ.get("BROWSE_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    found = shutil.which("browse")
    if found:
        return found

    home_bin = Path.home() / ".local" / "bin" / "browse"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    return None


def _chat_root_dir(settings: Settings) -> Path:
    root = (settings.artifacts_path / "opencode_chat").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_user_slug(user_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(user_id or "local-applicant")).strip("._")
    return cleaned or "local-applicant"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    root = path.parent.resolve()
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".chat-", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            with suppress(OSError):
                os.unlink(tmp_name)


async def _run_short_cmd(cmd: list[str], timeout: float = 3.5) -> tuple[int, str]:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return int(proc.returncode or 0), (stdout or b"").decode("utf-8", errors="replace")
    except TimeoutError:
        if proc is not None:
            with suppress(ProcessLookupError):
                proc.kill()
        return 124, ""
    except Exception as exc:
        return 1, str(exc)


class OpenCodeService:
    """Manages OpenCode CLI sessions, browse extension status, and CareerPilot chat threads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profile_service = ProfileService()

    def configured_model(self) -> str:
        """Resolve the OpenCode provider/model keeping the configured local Qwen model."""
        override = os.environ.get("OPENCODE_MODEL", "").strip()
        if override:
            return override

        raw_model = (self.settings.llm_model or "").strip()
        if "/" in raw_model:
            return raw_model

        config_path = Path.home() / ".config" / "opencode" / "opencode.json"
        if config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                providers = cfg.get("providers") or {}
                for prov_id, prov_cfg in providers.items():
                    models = (prov_cfg or {}).get("models") or {}
                    if raw_model and raw_model in models:
                        return f"{prov_id}/{raw_model}"
                    if "qwen-local" in models:
                        return f"{prov_id}/qwen-local"
            except Exception:
                pass

        return f"llamacpp/{raw_model or 'qwen-local'}"

    async def get_status(self) -> dict[str, Any]:
        """Inspect OpenCode CLI, browse MCP extension pairing, and configured model."""
        cli_path = resolve_opencode_cli()
        browse_path = resolve_browse_bin()

        extension_paired = False
        paired_browser: str | None = None
        relay_state = "unavailable"
        mcp_configured = False

        if browse_path:
            rc_status, out_status = await _run_short_cmd([browse_path, "status"])
            if rc_status == 0 and out_status:
                mcp_configured = '"browse" configured' in out_status or "Setup:     installed" in out_status
                if "Relay: running" in out_status:
                    relay_state = "running"
                elif "Relay: stopped" in out_status:
                    relay_state = "ready"

            rc_pair, out_pair = await _run_short_cmd([browse_path, "pair", "--list"])
            if rc_pair == 0 and out_pair.strip():
                lines = [ln.strip() for ln in out_pair.splitlines() if ln.strip()]
                if lines and not lines[0].lower().startswith("no paired"):
                    extension_paired = True
                    paired_browser = lines[0]

        config_path = Path.home() / ".config" / "opencode" / "opencode.json"
        if not mcp_configured and config_path.is_file():
            with suppress(Exception):
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                mcp_configured = "browse" in ((cfg.get("mcp") or {}).get("servers") or {})

        model = self.configured_model()
        ready = bool(cli_path and browse_path and extension_paired)
        return {
            "cli_available": bool(cli_path),
            "cli_path": cli_path,
            "browse_available": bool(browse_path),
            "browse_path": browse_path,
            "mcp_configured": mcp_configured,
            "extension_paired": extension_paired,
            "paired_browser": paired_browser,
            "relay_state": relay_state,
            "model": model,
            "model_base_url": self.settings.llm_base_url or "http://127.0.0.1:9931/v1",
            "ready": ready,
        }

    def _store_path(self, user_id: str) -> Path:
        root = _chat_root_dir(self.settings)
        target = (root / f"{_safe_user_slug(user_id)}.json").resolve()
        if not target.is_relative_to(root):
            raise ValueError("Caminho de histórico inválido.")
        return target

    def _load_store(self, user_id: str) -> dict[str, Any]:
        path = self._store_path(user_id)
        if not path.is_file():
            return {"active_thread_id": None, "threads": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("threads"), list):
                return data
        except Exception:
            pass
        return {"active_thread_id": None, "threads": []}

    def _save_store(self, user_id: str, store: dict[str, Any]) -> None:
        store["threads"] = list(store.get("threads") or [])[:40]
        _atomic_write_json(self._store_path(user_id), store)

    def list_threads(self, user_id: str) -> dict[str, Any]:
        store = self._load_store(user_id)
        threads = store.get("threads") or []
        active_id = store.get("active_thread_id")
        if active_id and not any(t.get("thread_id") == active_id for t in threads):
            active_id = threads[0]["thread_id"] if threads else None
        return {
            "active_thread_id": active_id,
            "threads": threads,
        }

    def get_thread(self, user_id: str, thread_id: str) -> dict[str, Any] | None:
        store = self._load_store(user_id)
        for thread in store.get("threads") or []:
            if thread.get("thread_id") == thread_id:
                return dict(thread)
        return None

    def create_or_select_thread(
        self,
        db: Session,
        *,
        user_id: str,
        title: str | None = None,
        job_id: str | None = None,
        application_id: str | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        store = self._load_store(user_id)
        threads: list[dict[str, Any]] = list(store.get("threads") or [])

        if not force_new and (application_id or job_id):
            for existing in threads:
                if (application_id and existing.get("application_id") == application_id) or (
                    job_id and not application_id and existing.get("job_id") == job_id
                ):
                    store["active_thread_id"] = existing["thread_id"]
                    self._save_store(user_id, store)
                    return dict(existing)

        resolved_title = (title or "").strip()
        if not resolved_title and job_id:
            job = db.scalar(select(JobPosting).where(JobPosting.id == job_id))
            if job is not None:
                resolved_title = f"{job.title} — {job.company}"
        if not resolved_title and application_id:
            app = db.scalar(
                select(Application).where(
                    Application.id == application_id,
                    Application.user_id == user_id,
                )
            )
            if app is not None:
                job_id = job_id or app.job_id
                job = db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
                if job is not None:
                    resolved_title = f"{job.title} — {job.company}"

        now = utc_rfc3339()
        thread: dict[str, Any] = {
            "thread_id": f"oc-{uuid.uuid4().hex[:12]}",
            "title": (resolved_title or "Chat OpenCode")[:120],
            "opencode_session_id": None,
            "job_id": job_id,
            "application_id": application_id,
            "model": self.configured_model(),
            "context_signature": "",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        threads.insert(0, thread)
        store["threads"] = threads[:40]
        store["active_thread_id"] = thread["thread_id"]
        self._save_store(user_id, store)
        return dict(thread)

    def build_careerpilot_context(
        self,
        db: Session,
        *,
        user_id: str,
        job_id: str | None = None,
        application_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Assemble strictly factual Perfil Mestre, Arsenal, Skills, and selected Job/PDF info."""
        doc, rev = self.profile_service.get_document(db, user_id=user_id)

        arsenal_rows = list(
            db.scalars(
                select(ArsenalRespostaRow)
                .where(
                    ArsenalRespostaRow.user_id == user_id,
                    ArsenalRespostaRow.status.in_(["approved", "draft"]),
                )
                .order_by(ArsenalRespostaRow.updated_at.desc())
                .limit(40)
            ).all()
        )

        app: Application | None = None
        if application_id:
            app = db.scalar(
                select(Application).where(
                    Application.id == application_id,
                    Application.user_id == user_id,
                )
            )
            if app is not None and not job_id:
                job_id = app.job_id

        job: JobPosting | None = None
        if job_id:
            job = db.scalar(select(JobPosting).where(JobPosting.id == job_id))
            if app is None:
                app = db.scalar(
                    select(Application)
                    .where(
                        Application.job_id == job_id,
                        Application.user_id == user_id,
                    )
                    .order_by(Application.updated_at.desc())
                )

        resume_pdf_path: str | None = None
        if app is not None and app.resume_version_id:
            rv = db.scalar(select(ResumeVersion).where(ResumeVersion.id == app.resume_version_id))
            if rv is not None:
                with suppress(Exception):
                    resume_pdf_path = str(resolve_safe_resume_path(self.settings, rv, user_id=user_id))

        langs_text = ", ".join(f"{lg.name} ({lg.proficiency or 'N/I'})" for lg in doc.languages) or "N/I"
        c = doc.contact
        p = doc.preferences
        lines: list[str] = [
            "CONTEXTO_DO_CANDIDATO (CareerPilot — use apenas fatos reais abaixo, nunca invente dados):",
            f"Perfil Mestre Revisão: {rev}",
            f"- Nome completo: {c.name or 'N/I'}",
            f"- E-mail: {c.email or 'N/I'} | Telefone: {c.phone or 'N/I'}",
            f"- Localização: {', '.join(x for x in (c.city, c.state, c.country) if x) or p.location or 'N/I'}",
            f"- Links: LinkedIn={c.links.linkedin or '-'} | GitHub={c.links.github or '-'} | Portfólio={c.links.portfolio or '-'}",
            f"- Cargos alvo: {', '.join(p.target_roles) or 'N/I'} | Modelo: {', '.join(p.work_model) or 'N/I'}",
            f"- Pretensão salarial padrão: amount={p.salary_amount or '-'} currency={p.salary_currency or 'BRL'} period={p.salary_period or 'monthly'} text={p.salary_text or '-'}",
            f"- Disponibilidade: {p.availability or 'N/I'} | Autorização de trabalho: {p.work_authorization or 'N/I'}",
            f"- Skills ({len(doc.skills)}): {', '.join(s.name for s in doc.skills)}",
            f"- Idiomas: {langs_text}",
        ]

        if doc.experience:
            lines.append("- Experiências:")
            for exp in doc.experience[:6]:
                lines.append(f"  * {exp.role} em {exp.company} ({exp.period})")
                for b in exp.bullets[:3]:
                    lines.append(f"    - {b.text}")

        if doc.education:
            lines.append("- Formação:")
            for edu in doc.education[:4]:
                lines.append(f"  * {edu.degree} — {edu.institution} ({edu.period})")

        if doc.projects:
            lines.append("- Projetos:")
            for prj in doc.projects[:4]:
                lines.append(f"  * {prj.name}: {prj.details}")

        if doc.standard_answers:
            lines.append("- Respostas padrão do Perfil:")
            for sa in doc.standard_answers[:20]:
                if sa.answer:
                    lines.append(f"  * {sa.label}: {sa.answer}")

        if arsenal_rows:
            lines.append("- Arsenal de respostas / textos salvos:")
            for row in arsenal_rows[:25]:
                payload = row.payload_json if isinstance(row.payload_json, dict) else {}
                raw_variants = payload.get("length_variants")
                variants: dict[str, Any] = raw_variants if isinstance(raw_variants, dict) else {}
                ans = str(variants.get("short") or variants.get("medium") or variants.get("long") or "").strip()
                triggers = ", ".join(str(t) for t in (payload.get("trigger_keywords") or [])[:4])
                if ans:
                    lines.append(
                        f"  * [{row.category}/{row.language}] {triggers or row.id}: {ans[:280]}"
                    )

        meta: dict[str, Any] = {
            "profile_revision": rev,
            "job_id": job.id if job else None,
            "application_id": app.id if app else None,
            "resume_pdf_path": resume_pdf_path,
        }

        if job is not None:
            desc_excerpt = " ".join((job.description or "").split())[:2600]
            lines.extend(
                [
                    "",
                    "VAGA_ALVO:",
                    f"- Cargo: {job.title}",
                    f"- Empresa: {job.company}",
                    f"- Local / Modalidade: {job.location or 'N/I'} / {job.workplace_type}",
                    f"- URL da vaga/candidatura: {job.apply_url or job.job_url}",
                    f"- Caminho local do Currículo PDF gerado para esta vaga: {resume_pdf_path or '(ainda não gerado — avise o usuário se precisar anexar)'}",
                    f"- Descrição resumida: {desc_excerpt}",
                ]
            )

        lines.extend(
            [
                "",
                "REGRAS_DE_EXECUCAO:",
                "1. Para abrir páginas, ver abas abertas e preencher formulários no Chrome do usuário, utilize o servidor MCP `browse` (extensão do navegador já conectada às abas e logins do usuário).",
                "2. Sempre faça snapshot/leitura da página com `browse` antes e depois de agir; se a vaga pedir upload de currículo, envie o arquivo PDF indicado acima.",
                "3. Nunca invente fatos fora do CONTEXTO_DO_CANDIDATO. Use os textos, skills e CEP/respostas do Arsenal quando aplicável.",
                "4. NUNCA clique no botão final de envio/submissão (`Enviar candidatura` / `Submit application`) nem tente burlar CAPTCHA/2FA/login. Quando tudo estiver preenchido ou houver CAPTCHA/login/dúvida salarial não informada, pare e informe exatamente o que falta no chat para o usuário confirmar.",
            ]
        )

        context_text = "\n".join(lines)
        sig = f"{rev}:{job.id if job else ''}:{resume_pdf_path or ''}:{len(arsenal_rows)}"
        meta["signature"] = sig
        return context_text, meta

    async def start_chat_run(
        self,
        db: Session,
        *,
        user_id: str,
        message: str,
        thread_id: str | None = None,
        job_id: str | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        """Append user message and start an async background OpenCode CLI execution."""
        cli_bin = resolve_opencode_cli()
        if not cli_bin:
            raise OpenCodeUnavailableError(
                "Executável `opencode-cli` não encontrado na máquina."
            )

        clean_msg = (message or "").strip()
        if not clean_msg:
            raise ValueError("Digite uma mensagem para enviar ao OpenCode.")

        async with _get_lock(f"store:{user_id}"):
            store = self._load_store(user_id)
            threads: list[dict[str, Any]] = list(store.get("threads") or [])
            thread: dict[str, Any] | None = None
            if thread_id:
                thread = next((t for t in threads if t.get("thread_id") == thread_id), None)
            if thread is None:
                thread = self.create_or_select_thread(
                    db,
                    user_id=user_id,
                    job_id=job_id,
                    application_id=application_id,
                    force_new=False,
                )
                store = self._load_store(user_id)
                threads = list(store.get("threads") or [])
                thread = next(t for t in threads if t["thread_id"] == thread["thread_id"])

            if job_id and thread.get("job_id") != job_id:
                thread["job_id"] = job_id
            if application_id and thread.get("application_id") != application_id:
                thread["application_id"] = application_id

            eff_job_id = thread.get("job_id") or job_id
            eff_app_id = thread.get("application_id") or application_id
            context_block, ctx_meta = self.build_careerpilot_context(
                db,
                user_id=user_id,
                job_id=eff_job_id,
                application_id=eff_app_id,
            )

            need_context = (
                not thread.get("opencode_session_id")
                or thread.get("context_signature") != ctx_meta["signature"]
            )
            full_prompt = (
                f"{context_block}\n\nMENSAGEM_DO_USUARIO:\n{clean_msg}"
                if need_context
                else clean_msg
            )

            now = utc_rfc3339()
            thread.setdefault("messages", []).append(
                {
                    "id": f"msg-{uuid.uuid4().hex[:10]}",
                    "role": "user",
                    "content": clean_msg,
                    "tools": [],
                    "created_at": now,
                }
            )
            thread["updated_at"] = now
            thread["context_signature"] = ctx_meta["signature"]
            thread["job_id"] = ctx_meta["job_id"]
            thread["application_id"] = ctx_meta["application_id"]
            store["active_thread_id"] = thread["thread_id"]
            self._save_store(user_id, store)

            model = self.configured_model()
            existing_ses = thread.get("opencode_session_id")
            resolved_thread_id = str(thread["thread_id"])

        run_id = f"run-{uuid.uuid4().hex[:12]}"
        run_state = _ChatRunState(
            run_id=run_id,
            user_id=user_id,
            thread_id=resolved_thread_id,
            opencode_session_id=existing_ses,
        )
        _RUNS[run_id] = run_state

        task = asyncio.create_task(
            self._execute_opencode_run(
                run_state=run_state,
                cli_bin=cli_bin,
                model=model,
                existing_session_id=existing_ses,
                prompt=full_prompt,
            )
        )
        run_state.task = task
        return {
            "run_id": run_id,
            "thread_id": resolved_thread_id,
            "status": run_state.status,
            "model": model,
            "context_meta": ctx_meta,
        }

    async def _execute_opencode_run(
        self,
        *,
        run_state: _ChatRunState,
        cli_bin: str,
        model: str,
        existing_session_id: str | None,
        prompt: str,
    ) -> None:
        cmd: list[str] = [
            cli_bin,
            "run",
            "--model",
            model,
            "--format",
            "json",
            "--auto",
        ]
        if existing_session_id:
            cmd.extend(["--session", existing_session_id])
        else:
            cmd.extend(["--title", f"CareerPilot {run_state.thread_id}"])
        cmd.append(prompt)

        text_by_part: dict[str, str] = {}
        ordered_parts: list[str] = []
        stderr_chunks: list[str] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(Path(__file__).resolve().parents[3]),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            run_state.proc = proc

            async def _drain_stderr() -> None:
                assert proc.stderr is not None
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    stderr_chunks.append(line.decode("utf-8", errors="replace"))

            stderr_task = asyncio.create_task(_drain_stderr())

            assert proc.stdout is not None
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue

                sid = ev.get("sessionID")
                if isinstance(sid, str) and sid.strip():
                    run_state.opencode_session_id = sid.strip()

                ev_type = str(ev.get("type") or "")
                raw_part = ev.get("part")
                part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}
                part_id = str(part.get("id") or f"p-{len(ordered_parts)}")
                part_type = str(part.get("type") or ev_type)

                if ev_type == "text" or part_type == "text":
                    chunk_txt = part.get("text") if isinstance(part.get("text"), str) else ev.get("text", "")
                    if isinstance(chunk_txt, str) and chunk_txt:
                        if part_id not in text_by_part:
                            ordered_parts.append(part_id)
                        text_by_part[part_id] = chunk_txt
                        run_state.partial_text = "\n\n".join(
                            text_by_part[pid] for pid in ordered_parts if text_by_part.get(pid)
                        ).strip()
                        run_state.updated_at = utc_rfc3339()
                elif ev_type == "tool_use" or part_type in {"tool", "tool-invocation", "tool_use"} or part.get("tool"):
                    tool_name = str(part.get("tool") or part.get("name") or ev.get("tool") or "browse")
                    raw_state = part.get("state")
                    state_obj: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
                    tool_status = str(state_obj.get("status") or part.get("status") or "running")
                    tool_title = str(state_obj.get("title") or "")
                    entry = {"tool": tool_name, "status": tool_status, "title": tool_title[:120]}
                    # Update matching tool call or append
                    if run_state.tool_events and run_state.tool_events[-1].get("tool") == tool_name and run_state.tool_events[-1].get("status") == "pending":
                        run_state.tool_events[-1] = entry
                    else:
                        run_state.tool_events.append(entry)
                    run_state.updated_at = utc_rfc3339()
                elif ev_type == "error":
                    err_obj = ev.get("error")
                    if isinstance(err_obj, dict):
                        run_state.error = str(err_obj.get("message") or err_obj.get("name") or err_obj)
                    elif err_obj:
                        run_state.error = str(err_obj)

            await stderr_task
            rc = await proc.wait()

            if run_state.status == "cancelled":
                return

            final_reply = run_state.partial_text.strip()
            if rc != 0 and not final_reply:
                err_txt = "".join(stderr_chunks).strip() or run_state.error or f"OpenCode retornou código {rc}"
                run_state.status = "failed"
                run_state.error = err_txt[:800]
                reply_to_store = f"Erro ao executar OpenCode: {run_state.error}"
            else:
                run_state.status = "completed"
                reply_to_store = final_reply or "(Execução concluída sem texto de resposta.)"

            async with _get_lock(f"store:{run_state.user_id}"):
                store = self._load_store(run_state.user_id)
                for thread in store.get("threads") or []:
                    if thread.get("thread_id") == run_state.thread_id:
                        if run_state.opencode_session_id:
                            thread["opencode_session_id"] = run_state.opencode_session_id
                        thread.setdefault("messages", []).append(
                            {
                                "id": f"msg-{uuid.uuid4().hex[:10]}",
                                "role": "assistant",
                                "content": reply_to_store,
                                "tools": list(run_state.tool_events),
                                "created_at": utc_rfc3339(),
                            }
                        )
                        thread["updated_at"] = utc_rfc3339()
                        break
                self._save_store(run_state.user_id, store)
        except asyncio.CancelledError:
            run_state.status = "cancelled"
            if run_state.proc and run_state.proc.returncode is None:
                with suppress(ProcessLookupError):
                    run_state.proc.terminate()
        except Exception as exc:
            run_state.status = "failed"
            run_state.error = str(exc)
        finally:
            run_state.updated_at = utc_rfc3339()
            run_state.proc = None

    def get_run(self, *, user_id: str, run_id: str) -> dict[str, Any] | None:
        run_state = _RUNS.get(run_id)
        if run_state is None or run_state.user_id != user_id:
            return None
        thread = self.get_thread(user_id, run_state.thread_id)
        return {
            "run_id": run_state.run_id,
            "thread_id": run_state.thread_id,
            "status": run_state.status,
            "partial_text": run_state.partial_text,
            "tool_events": list(run_state.tool_events),
            "error": run_state.error,
            "opencode_session_id": run_state.opencode_session_id,
            "thread": thread,
        }

    async def cancel_run(self, *, user_id: str, run_id: str) -> dict[str, Any] | None:
        run_state = _RUNS.get(run_id)
        if run_state is None or run_state.user_id != user_id:
            return None
        run_state.status = "cancelled"
        if run_state.proc and run_state.proc.returncode is None:
            with suppress(ProcessLookupError):
                run_state.proc.terminate()
        if run_state.task and not run_state.task.done():
            run_state.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.sleep(0.05)
        return self.get_run(user_id=user_id, run_id=run_id)
