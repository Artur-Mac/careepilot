/* CareerPilot pt-BR Dashboard Client — Phase 2 */
(() => {
  const state = {
    csrfToken: null,
    jobs: [],
    jobsCursor: null,
    jobsHasMore: false,
    applications: [],
    activeApplicationId: null,
    profile: { document: null, revision: 0, updated_at: null },
    settings: null,
    activeDiscoveryRunId: null,
    discoveryPollTimer: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  function showToast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    setTimeout(() => el.classList.add("hidden"), 6000);
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, "&quot;");
  }

  function safeExternalUrl(raw) {
    if (!raw) return "#";
    try {
      const parsed = new URL(String(raw));
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.toString() : "#";
    } catch (_) {
      return "#";
    }
  }

  function newClientId(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `${prefix}-${window.crypto.randomUUID().slice(0, 12)}`;
    }
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }

  async function apiFetch(url, opts = {}) {
    const headers = new Headers(opts.headers || {});
    if (opts.method && opts.method !== "GET" && state.csrfToken) {
      headers.set("X-CSRF-Token", state.csrfToken);
    }
    const resp = await fetch(url, { ...opts, headers, credentials: "same-origin" });
    if (!resp.ok) {
      let detail = `Erro ${resp.status}`;
      let rawBody = null;
      try {
        rawBody = await resp.json();
        if (typeof rawBody.detail === "string") {
          detail = rawBody.detail;
        } else if (rawBody.detail && typeof rawBody.detail.message === "string") {
          detail = rawBody.detail.message;
        }
      } catch (_) {}
      const err = new Error(detail);
      err.status = resp.status;
      err.body = rawBody;
      throw err;
    }
    const ct = resp.headers.get("content-type") || "";
    return ct.includes("application/json") ? resp.json() : resp.text();
  }

  function switchTab(tabName) {
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
    $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tabName}`));
    if (tabName === "perfil" && !state.profile.document) loadProfile();
    if (tabName === "candidatura") loadApplications();
    if (tabName === "configuracoes") loadSettings();
  }

  $$(".tab-btn").forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );

  function syncTabFromHash() {
    if (window.location.hash === "#perfil") {
      switchTab("perfil");
    } else if (window.location.hash === "#candidatura") {
      switchTab("candidatura");
    }
  }
  window.addEventListener("hashchange", syncTabFromHash);

  async function checkSession() {
    const res = await apiFetch("/api/auth/session");
    if (res.authenticated) {
      state.csrfToken = res.csrf_token;
      await Promise.all([
        loadBrowserStatus(),
        loadJobs(),
        loadApplications(),
        loadSettings(),
        loadProfile(),
      ]);
      syncTabFromHash();
      $("#unlock-panel").classList.add("hidden");
      $("#app-workspace").classList.remove("hidden");
      $("#btn-logout").classList.remove("hidden");
      $("#session-indicator").textContent = "Sessão local autenticada";
    } else {
      if (state.discoveryPollTimer) {
        clearTimeout(state.discoveryPollTimer);
        state.discoveryPollTimer = null;
      }
      $("#unlock-panel").classList.remove("hidden");
      $("#app-workspace").classList.add("hidden");
      $("#btn-logout").classList.add("hidden");
      $("#session-indicator").textContent = "Sessão bloqueada";
    }
  }

  $("#unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#unlock-error").classList.add("hidden");
    try {
      const res = await apiFetch("/api/auth/unlock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: $("#passcode-input").value }),
      });
      state.csrfToken = res.csrf_token;
      await checkSession();
      showToast("Painel desbloqueado em modo local.");
    } catch (err) {
      $("#unlock-error").textContent = err.message;
      $("#unlock-error").classList.remove("hidden");
    }
  });

  $("#btn-logout").addEventListener("click", async () => {
    await apiFetch("/api/auth/logout", { method: "POST" });
    state.csrfToken = null;
    await checkSession();
  });

  /* === VAGAS === */
  function eligibilityBadge(elig) {
    const scope = (elig && elig.remote_scope) || "unspecified";
    const status = (elig && elig.brazil_eligible) || "unspecified";
    const labels = {
      recife_pe: ["Recife, PE", "badge-ok"],
      recife_hybrid: ["Recife Híbrido", "badge-ok"],
      brazil: ["Remoto Brasil", "badge-ok"],
      latam: ["Remoto LATAM", "badge-ok"],
      global: ["Remoto Global", "badge-ok"],
      other_city: ["Outra cidade (restrita)", "badge-danger"],
      restricted_region: ["Região restrita", "badge-danger"],
      restricted_country: ["País restrito", "badge-danger"],
      unspecified: ["Confirmar elegibilidade", "badge-warn"],
    };
    const fallback = status === "restricted" ? ["Restrita", "badge-danger"] : ["Confirmar elegibilidade", "badge-warn"];
    const [text, cls] = labels[scope] || fallback;
    return `<span class="badge ${cls}" title="${escapeAttr((elig && elig.reason) || "")}">${escapeHtml(text)}</span>`;
  }

  async function loadBrowserStatus() {
    const badge = $("#browser-status-badge");
    const detail = $("#browser-status-detail");
    if (!badge || !detail) return;
    try {
      const data = await apiFetch("/api/browser/status");
      if (data.connected) {
        badge.className = "badge badge-ok";
        badge.textContent = "Conectado";
        detail.textContent = `${data.open_tabs ?? 0} aba(s) aberta(s) · CDP ${data.cdp_endpoint || ""}`;
      } else {
        badge.className = "badge badge-warn";
        badge.textContent = "Desconectado";
        detail.textContent = `Execute ${data.launch_command || "./scripts/start_browser.sh"} para abrir o Chrome local.`;
      }
    } catch (err) {
      badge.className = "badge badge-warn";
      badge.textContent = "Indisponível";
      detail.textContent = err.message || "";
    }
  }

  $("#btn-refresh-browser-status")?.addEventListener("click", () => {
    loadBrowserStatus();
  });

  function renderDiscoveryRunBanner(run) {
    const box = $("#discovery-run-status");
    const statusEl = $("#live-search-status");
    if (!box) return;
    box.classList.remove("hidden");
    const errors = run.source_errors || {};
    const errorLines = Object.entries(errors)
      .map(([src, info]) => {
        const msg = typeof info === "string" ? info : info.message || info.code || "falha";
        return `<li><strong>${escapeHtml(src)}</strong>: ${escapeHtml(msg)}</li>`;
      })
      .join("");
    const queries = (run.queries || []).join(" · ");
    if (run.status === "queued" || run.status === "running") {
      if (statusEl) statusEl.textContent = "Busca em andamento no Chrome…";
      box.innerHTML = `
        <strong>Busca em andamento</strong> (<code>${escapeHtml(run.id)}</code>) · Fontes: ${escapeHtml((run.sources || []).join(", "))}
        ${queries ? `<div class="muted">Consultas: ${escapeHtml(queries)}</div>` : ""}
      `;
      return;
    }
    if (statusEl) {
      statusEl.textContent =
        run.status === "completed"
          ? `${run.inserted_count ?? 0} novas, ${run.deduped_count ?? 0} já conhecidas.`
          : `Busca encerrada com erro.`;
    }
    box.innerHTML = `
      <div>
        <strong>${run.status === "completed" ? "Busca concluída" : "Falha na busca"}</strong>:
        ${run.inserted_count ?? 0} nova(s), ${run.deduped_count ?? 0} repetida(s), ${run.rejected_restricted_count ?? 0} restrita(s) ignorada(s).
      </div>
      ${run.error ? `<div class="alert-error">${escapeHtml(run.error)}</div>` : ""}
      ${errorLines ? `<ul style="margin:6px 0 0 18px;">${errorLines}</ul>` : ""}
    `;
  }

  function scheduleDiscoveryPoll(runId) {
    if (state.discoveryPollTimer) {
      clearTimeout(state.discoveryPollTimer);
      state.discoveryPollTimer = null;
    }
    state.activeDiscoveryRunId = runId;
    const poll = async () => {
      if (state.activeDiscoveryRunId !== runId) return;
      try {
        const run = await apiFetch(`/api/jobs/discovery-runs/${encodeURIComponent(runId)}`);
        renderDiscoveryRunBanner(run);
        if (run.status === "completed" || run.status === "failed") {
          state.activeDiscoveryRunId = null;
          const btn = $("#btn-discover-jobs");
          if (btn) btn.disabled = false;
          await loadJobs(false);
          await loadSettings();
          return;
        }
        state.discoveryPollTimer = setTimeout(poll, 1200);
      } catch (err) {
        const box = $("#discovery-run-status");
        if (box) {
          box.classList.remove("hidden");
          box.textContent = `Erro ao consultar status da busca: ${err.message}`;
        }
        const btn = $("#btn-discover-jobs");
        if (btn) btn.disabled = false;
      }
    };
    state.discoveryPollTimer = setTimeout(poll, 400);
  }

  $("#discover-jobs-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#btn-discover-jobs") || event.submitter;
    const statusEl = $("#live-search-status");
    const box = $("#discovery-run-status");
    const roleOverride = ($("#discover-role-override")?.value || "").trim() || null;
    const location = ($("#discover-location")?.value || "").trim() || null;
    const remoteOnly = Boolean($("#discover-remote-only")?.checked);
    const includeRemote = Boolean($("#discover-include-remote")?.checked);
    if (button) button.disabled = true;
    statusEl.textContent = "Iniciando busca no navegador Chrome…";
    if (box) {
      box.classList.remove("hidden");
      box.textContent = "Preparando consultas com base no Perfil mestre…";
    }
    try {
      const result = await apiFetch("/api/jobs/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          role_override: roleOverride,
          location,
          remote_only: remoteOnly,
          include_remote: includeRemote,
        }),
      });
      renderDiscoveryRunBanner(result);
      scheduleDiscoveryPoll(result.run_id || result.id);
    } catch (error) {
      statusEl.textContent = error.message;
      if (box) {
        box.classList.remove("hidden");
        if (error.status === 422) {
          box.innerHTML = `${escapeHtml(error.message)} <a href="#perfil">Editar Perfil mestre</a>`;
        } else {
          box.textContent = error.message;
        }
      }
      if (error.status === 503) {
        loadBrowserStatus();
      }
      if (button) button.disabled = false;
    }
  });

  $("#btn-apply-filters").addEventListener("click", () => {
    loadJobs(false);
  });

  $("#filter-include-restricted")?.addEventListener("change", () => {
    loadJobs(false);
  });

  $("#btn-load-more-jobs")?.addEventListener("click", () => {
    loadJobs(true);
  });

  async function loadJobs(append = false) {
    const params = new URLSearchParams();
    params.set("limit", "25");
    const q = $("#filter-q")?.value.trim() || "";
    const wp = $("#filter-workplace")?.value || "";
    const includeRestricted = Boolean($("#filter-include-restricted")?.checked);
    if (q) params.set("q", q);
    if (wp) params.set("workplace", wp);
    if (includeRestricted) params.set("include_restricted", "true");
    if (append && state.jobsCursor) {
      params.set("cursor", state.jobsCursor);
    }
    const data = await apiFetch(`/api/jobs?${params.toString()}`);
    const pageItems = data.jobs || data.items || [];
    state.jobs = append ? [...state.jobs, ...pageItems] : pageItems;
    state.jobsCursor = data.next_cursor || null;
    state.jobsHasMore = Boolean(data.has_more);
    $("#vagas-count").textContent = `${state.jobs.length} vaga(s)${state.jobsHasMore ? "+" : ""}`;
    $("#btn-load-more-jobs")?.classList.toggle("hidden", !state.jobsHasMore);
    renderJobsGrid();
  }

  function renderJobsGrid() {
    const grid = $("#jobs-grid");
    if (!state.jobs.length) {
      grid.innerHTML = `<p class="muted">Nenhuma vaga armazenada. Ajuste os filtros ou realize uma busca no Chrome.</p>`;
      return;
    }
    grid.innerHTML = state.jobs
      .map((j) => {
        const salary =
          j.salary_min || j.salary_max
            ? `${j.salary_currency || ""} ${j.salary_min || "?"}–${j.salary_max || "?"}`
            : "Salário não informado";
        const inj = (j.injection_signals || []).length
          ? `<span class="badge badge-danger">Prompt injection sanitizado</span>`
          : "";
        const elig = j.eligibility || {};
        const snippetSource = j.summary_text || j.description || "";
        return `
          <article class="job-card" data-job-id="${escapeAttr(j.id)}">
            <div>
              <div style="display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;">
                <span class="badge badge-info">${escapeHtml(j.source)}</span>
                <span class="badge badge-neutral">${escapeHtml(j.workplace_type)}</span>
                ${eligibilityBadge(elig)}
                ${inj}
              </div>
              <h3>${escapeHtml(j.title)}</h3>
              <p class="muted"><strong>${escapeHtml(j.company)}</strong> · ${escapeHtml(j.location || "Local não informado")}</p>
              <p class="muted">${escapeHtml(salary)}</p>
              <p style="font-size:0.88rem;">${escapeHtml(snippetSource.slice(0, 220))}${snippetSource.length > 220 ? "…" : ""}</p>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button type="button" class="btn-primary btn-prepare-job" data-id="${escapeAttr(j.id)}" data-testid="btn-prepare-job-${escapeAttr(j.id)}">
                Preparar &amp; Aplicar
              </button>
              <button type="button" class="btn-secondary btn-detail-job" data-id="${escapeAttr(j.id)}" data-testid="btn-detail-job-${escapeAttr(j.id)}">Detalhes</button>
              <button type="button" class="btn-secondary btn-dismiss-job" data-id="${escapeAttr(j.id)}" data-testid="btn-dismiss-job-${escapeAttr(j.id)}">Dispensar</button>
            </div>
          </article>
        `;
      })
      .join("");

    $$(".btn-prepare-job").forEach((b) =>
      b.addEventListener("click", () => handlePrepareJob(b.dataset.id, b))
    );
    $$(".btn-detail-job").forEach((b) =>
      b.addEventListener("click", () => openJobDetail(b.dataset.id))
    );
    $$(".btn-dismiss-job").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await apiFetch(`/api/jobs/${encodeURIComponent(b.dataset.id)}/dismiss`, {
            method: "POST",
          });
          showToast("Vaga dispensada.");
          await loadJobs(false);
        } catch (err) {
          showToast(err.message);
          b.disabled = false;
        }
      })
    );
  }

  async function openJobDetail(jobId) {
    const j = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    const box = $("#job-detail-container");
    const safeUrl = safeExternalUrl(j.apply_url || j.job_url);
    const elig = j.eligibility || {};
    const initialSummary = j.summary_text
      ? `Resumo (cache local): ${j.summary_text}`
      : "";
    box.classList.remove("hidden");
    box.innerHTML = `
      <div class="section-head">
        <h2>${escapeHtml(j.title)} — ${escapeHtml(j.company)}</h2>
        <button type="button" id="btn-close-detail" class="btn-secondary">Fechar detalhes</button>
      </div>
      <p class="muted">
        ${escapeHtml(j.location || "Local N/I")} · ${escapeHtml(j.workplace_type)} · Fonte: <em>${escapeHtml(j.source_attribution || j.source)}</em>
        · ${eligibilityBadge(elig)}
      </p>
      ${elig.reason ? `<p class="muted">Elegibilidade: ${escapeHtml(elig.reason)}</p>` : ""}
      <p>
        Link original: <a href="${escapeAttr(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(safeUrl)}</a>
      </p>
      <div style="margin:10px 0;display:flex;gap:8px;flex-wrap:wrap;">
        <button type="button" id="btn-detail-prepare" class="btn-primary">Preparar &amp; Aplicar</button>
        <button type="button" id="btn-job-summary" class="btn-secondary">Resumir vaga</button>
      </div>
      <p id="job-summary" class="muted" aria-live="polite">${escapeHtml(initialSummary)}</p>
      <h3>Descrição sanitizada</h3>
      <p style="white-space:pre-line;">${escapeHtml(j.description)}</p>
    `;
    $("#btn-close-detail").addEventListener("click", () => box.classList.add("hidden"));
    $("#btn-detail-prepare").addEventListener("click", (ev) => handlePrepareJob(j.id, ev.currentTarget));
    $("#btn-job-summary").addEventListener("click", async (ev) => {
      const button = ev.currentTarget;
      const out = $("#job-summary");
      button.disabled = true;
      out.textContent = "Gerando resumo…";
      try {
        const res = await apiFetch(`/api/jobs/${encodeURIComponent(j.id)}/summary`, { method: "POST" });
        const prefix = res.cached
          ? "Resumo (cache local)"
          : res.method === "llm"
            ? "Resumo por IA"
            : "Trecho da descrição";
        out.textContent = `${prefix}: ${res.summary}`;
      } catch (err) {
        out.textContent = err.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  async function handlePrepareJob(jobId, buttonEl) {
    if (buttonEl) buttonEl.disabled = true;
    try {
      const appPayload = await apiFetch("/api/applications/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId }),
      });
      const appId = appPayload.application?.id || appPayload.id;
      state.activeApplicationId = appId;
      await loadApplications();
      switchTab("candidatura");

      let openRes = null;
      try {
        openRes = await apiFetch(`/api/applications/${encodeURIComponent(appId)}/open`, {
          method: "POST",
        });
      } catch (openErr) {
        if (openErr.status === 503) {
          loadBrowserStatus();
          showToast("PDF preparado. Conecte o Chrome (./scripts/start_browser.sh) e clique em Abrir no navegador.");
        } else {
          showToast(openErr.message);
        }
        await loadApplications();
        return;
      }

      if (openRes && openRes.opened) {
        try {
          const fillRes = await apiFetch(
            `/api/applications/${encodeURIComponent(appId)}/fill-current-page`,
            { method: "POST" }
          );
          if (fillRes.status === "review") {
            showToast("Etapa preenchida. Revise no navegador e avance manualmente.");
          } else if (fillRes.status === "needs_user") {
            showToast("Etapa inspecionada: ação humana necessária.");
          }
        } catch (fillErr) {
          showToast(fillErr.message);
        }
      }
      await Promise.all([loadApplications(), loadBrowserStatus()]);
    } catch (err) {
      if (err.status === 422 && err.body?.detail?.editor_url) {
        showToast(`${err.message} Abra o Perfil mestre para preencher ou importar seus dados.`);
        switchTab("perfil");
      } else {
        showToast(err.message);
      }
    } finally {
      if (buttonEl) buttonEl.disabled = false;
    }
  }

  /* === CANDIDATURA === */
  const STATUS_LABEL = {
    ready: ["PDF pronto", "badge-info"],
    review: ["Em revisão no navegador", "badge-warn"],
    needs_user: ["AÇÃO HUMANA NECESSÁRIA", "badge-danger"],
    done: ["Concluída", "badge-ok"],
  };

  async function loadApplications() {
    const data = await apiFetch("/api/applications");
    state.applications = data.applications || [];
    if (
      state.activeApplicationId &&
      !state.applications.some((a) => a.id === state.activeApplicationId)
    ) {
      state.activeApplicationId = null;
    }
    if (!state.activeApplicationId && state.applications.length) {
      state.activeApplicationId = state.applications[0].id;
    }
    await renderActiveCandidatura();
    renderApplicationsList();
  }

  function renderStepBlockerFieldInput(blocker) {
    const fid = escapeAttr(blocker.field_id || "");
    const ftype = blocker.field_type || "text";
    const opts = Array.isArray(blocker.options) ? blocker.options : [];
    let controlHtml = "";
    if (opts.length) {
      const optionsHtml = opts
        .map((o) => `<option value="${escapeAttr(o.label || o.option_id || "")}">${escapeHtml(o.label || o.option_id || "")}</option>`)
        .join("");
      controlHtml = `
        <select class="step-answer-input" data-field-id="${fid}" style="width:100%;margin-top:4px;">
          <option value="">-- Selecione uma opção --</option>
          ${optionsHtml}
        </select>
      `;
    } else if (ftype === "boolean") {
      controlHtml = `
        <select class="step-answer-input" data-field-id="${fid}" style="width:100%;margin-top:4px;">
          <option value="">-- Selecione --</option>
          <option value="Sim">Sim</option>
          <option value="Não">Não</option>
        </select>
      `;
    } else if (ftype === "number") {
      controlHtml = `<input type="number" class="step-answer-input" data-field-id="${fid}" style="width:100%;margin-top:4px;" />`;
    } else {
      controlHtml = `<textarea rows="2" class="step-answer-input" data-field-id="${fid}" style="width:100%;margin-top:4px;" placeholder="Digite sua resposta verídica…"></textarea>`;
    }

    return `
      <div class="step-answer-row" data-blocker-field-id="${fid}">
        <label style="display:block;font-weight:600;font-size:0.92rem;">
          ${escapeHtml(blocker.label || blocker.message || "Pergunta obrigatória")}
          ${controlHtml}
        </label>
        <label style="display:inline-flex;align-items:center;gap:6px;margin-top:6px;font-size:0.86rem;">
          <input type="checkbox" class="step-save-profile-cb" data-field-id="${fid}" />
          Salvar no perfil mestre
        </label>
      </div>
    `;
  }

  function renderStepPanel(app) {
    const step = app.last_step || null;
    if (!step && app.status === "ready") {
      return `
        <div class="notice-box" style="margin:12px 0;">
          <strong>Próximo passo:</strong> clique em <em>Abrir no navegador</em> (ou <em>Anexar aba aberta</em>) para vincular a página da vaga e preencher a etapa atual.
        </div>
      `;
    }
    if (!step) return "";

    const blockers = Array.isArray(step.blockers) ? step.blockers : [];
    const filled = Array.isArray(step.filled) ? step.filled : [];
    const skipped = Array.isArray(step.skipped) ? step.skipped : [];
    const uploaded = Array.isArray(step.uploaded_files) ? step.uploaded_files : [];
    const notices = Array.isArray(step.notices) ? step.notices : [];
    const answerable = blockers.filter((b) => b.can_answer && b.field_id);
    const manualOnly = blockers.filter((b) => !(b.can_answer && b.field_id));

    const parts = [];

    if (step.pending_refill && app.status !== "done") {
      parts.push(`
        <div class="notice-box" style="margin:12px 0;border-color:#93c5fd;background:#eff6ff;">
          <strong>Respostas salvas localmente.</strong>
          Clique em <em>Preencher etapa atual</em> para aplicar as novas respostas na guia do Chrome.
        </div>
      `);
    }

    if (app.status === "needs_user" || blockers.length > 0) {
      parts.push(`
        <div class="human-action-box" data-testid="human-action-panel">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
            <strong>AÇÃO HUMANA NECESSÁRIA</strong>
            ${step.portal ? `<span class="badge badge-neutral">Portal: ${escapeHtml(step.portal)}</span>` : ""}
          </div>
          ${
            step.open_form_required
              ? `<p style="margin:6px 0 0 0;"><strong>Abra o formulário da vaga no navegador e clique em Preencher etapa atual.</strong></p>`
              : ""
          }
          ${
            manualOnly.length
              ? `<ul style="margin:8px 0 0 18px;">${manualOnly
                  .map(
                    (b) =>
                      `<li>${b.label ? `<strong>${escapeHtml(b.label)}</strong>: ` : ""}${escapeHtml(b.message)}</li>`
                  )
                  .join("")}</ul>`
              : ""
          }
          ${
            answerable.length && step.step_id
              ? `<form id="step-answers-form" data-step-id="${escapeAttr(step.step_id)}" style="margin-top:10px;">
                   ${answerable.map(renderStepBlockerFieldInput).join("")}
                   <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
                     <button type="submit" class="btn-primary" data-testid="btn-save-answers">Salvar respostas</button>
                     <span class="muted">Depois de salvar, clique em <em>Preencher etapa atual</em>.</span>
                   </div>
                 </form>`
              : ""
          }
        </div>
      `);
    }

    if (app.status === "review" && app.status !== "done") {
      parts.push(`
        <div class="review-box" data-testid="review-banner">
          <strong>Revise no navegador e avance para a próxima etapa</strong>
          <div style="margin-top:4px;font-size:0.92rem;">
            O CareerPilot preencheu apenas esta etapa e <strong>nunca</strong> clica em Avançar, Continuar ou Enviar.
            ${
              step.final_submit_detected
                ? `<div style="margin-top:6px;"><strong>Etapa final detectada:</strong> revise tudo no Chrome e clique você mesmo no botão de envio final; depois volte aqui e clique em <em>Marcar como concluída</em>.</div>`
                : ""
            }
          </div>
        </div>
      `);
    } else if (step.final_submit_detected && app.status !== "done") {
      parts.push(`
        <div class="notice-box" style="margin:10px 0;">
          <strong>Botão de envio final detectado nesta etapa.</strong> Resolva os itens acima, confira no Chrome e faça o envio final manualmente.
        </div>
      `);
    }

    if (filled.length || uploaded.length || notices.length || skipped.length) {
      const filledHtml = filled
        .map(
          (f) =>
            `<li>${escapeHtml(f.label || f.field_id)} <span class="muted">(${escapeHtml(f.kind || "filled")})</span> ${
              f.suggested ? `<span class="badge badge-warn">Resposta sugerida</span>` : ""
            }</li>`
        )
        .join("");
      const skippedHtml = skipped.length
        ? `<details style="margin-top:6px;"><summary class="muted">Campos ignorados ou já preenchidos (${skipped.length})</summary><ul style="margin:6px 0 0 18px;">${skipped
            .map((s) => `<li>${escapeHtml(s.label || s.field_id)} — <code>${escapeHtml(s.reason || "")}</code></li>`)
            .join("")}</ul></details>`
        : "";

      parts.push(`
        <div class="notice-box" style="margin:12px 0;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
            <strong>Resumo da etapa inspecionada</strong>
            ${step.portal ? `<span class="muted">Portal: <code>${escapeHtml(step.portal)}</code></span>` : ""}
          </div>
          ${uploaded.length ? `<div style="margin-top:6px;"><strong>Currículo anexado:</strong> ${uploaded.map((u) => `<code>${escapeHtml(u)}</code>`).join(", ")}</div>` : ""}
          ${filled.length ? `<ul style="margin:6px 0 0 18px;">${filledHtml}</ul>` : ""}
          ${
            notices.length
              ? `<ul style="margin:6px 0 0 18px;color:#475569;">${notices
                  .map((n) => `<li>${escapeHtml(n)}</li>`)
                  .join("")}</ul>`
              : ""
          }
          ${skippedHtml}
        </div>
      `);
    }

    return parts.join("");
  }

  async function renderActiveCandidatura() {
    const box = $("#candidatura-active");
    const app = state.applications.find((a) => a.id === state.activeApplicationId);
    if (!app) {
      box.innerHTML = `
        <h2>Candidatura Atual</h2>
        <p class="muted">Nenhuma candidatura ativa. Escolha <strong>Preparar &amp; Aplicar</strong> em uma vaga na aba Vagas.</p>
      `;
      return;
    }
    const [statusLabel, statusBadge] = STATUS_LABEL[app.status] || [app.status, "badge-neutral"];
    const safeUrl = safeExternalUrl(app.job_url);
    const modeLabel =
      app.tailoring_mode === "llm_structured"
        ? "Seleção por LLM + PDF determinístico"
        : "Modo offline determinístico";
    const isHistoricalUnbound =
      !app.bound_to_browser && (app.status === "review" || app.status === "needs_user");
    const openBtnLabel = app.bound_to_browser
      ? "Focar no navegador"
      : isHistoricalUnbound
      ? "Reabrir no navegador"
      : "Abrir no navegador";

    box.innerHTML = `
      <div class="section-head">
        <div>
          <h2 style="margin:0;">${escapeHtml(app.job_title || "Vaga")} — ${escapeHtml(app.company || "")}</h2>
          <p class="muted" style="margin:4px 0 0 0;">
            ${escapeHtml(app.location || "Local N/I")} · ${escapeHtml(app.workplace_type || "")} · Método: <code>${escapeHtml(modeLabel)}</code>
          </p>
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <span class="badge ${app.bound_to_browser ? "badge-ok" : "badge-neutral"}">
            ${app.bound_to_browser ? "Aba vinculada no Chrome" : "Sem aba vinculada"}
          </span>
          <span class="badge ${statusBadge}">${escapeHtml(statusLabel)}</span>
        </div>
      </div>

      ${
        app.profile_revision_stale && app.status !== "done"
          ? `<div class="notice-box" style="margin:10px 0;">
               <strong>Perfil mestre atualizado:</strong> este PDF foi gerado com a revisão anterior (${app.resume_profile_revision}).
               <button type="button" id="btn-rebuild-pdf" class="btn-secondary" style="margin-left:8px;">Regerar PDF com perfil atual</button>
             </div>`
          : ""
      }
      ${
        isHistoricalUnbound
          ? `<div class="notice-box" style="margin:10px 0;">
               Esta candidatura está sem guia vinculada no Chrome atual (por exemplo, após reiniciar o navegador). Clique em <strong>Reabrir no navegador</strong> ou <strong>Anexar aba aberta</strong> para retomar.
             </div>`
          : ""
      }
      ${app.last_error ? `<div class="card alert-box" style="margin:10px 0;">${escapeHtml(app.last_error)}</div>` : ""}

      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;">
        ${
          app.status !== "done"
            ? `<button type="button" id="btn-fill-step" class="btn-primary" data-testid="btn-fill-step">Preencher etapa atual</button>
               <button type="button" id="btn-open-browser" class="btn-secondary" data-testid="btn-open-browser">${escapeHtml(openBtnLabel)}</button>
               <button type="button" id="btn-list-pages" class="btn-secondary" data-testid="btn-list-pages">Anexar aba aberta</button>`
            : ""
        }
        ${
          app.resume_id
            ? `<a class="btn-secondary" href="/api/resumes/${encodeURIComponent(app.resume_id)}/download" data-testid="btn-download-pdf">Baixar PDF</a>
               <button type="button" id="btn-toggle-pdf-preview" class="btn-secondary">Pré-visualizar texto do PDF</button>`
            : ""
        }
        ${
          safeUrl !== "#"
            ? `<a class="btn-secondary" href="${escapeAttr(safeUrl)}" target="_blank" rel="noopener noreferrer" data-testid="btn-open-job-link">Abrir link externo</a>`
            : `<span class="muted">URL da vaga indisponível</span>`
        }
        ${
          app.status !== "done"
            ? `<button type="button" id="btn-complete-app" class="btn-ok" data-testid="btn-complete-app">Marcar como concluída</button>`
            : ""
        }
      </div>

      <div id="attach-pages-panel" class="tabs-attach-panel hidden"></div>
      ${renderStepPanel(app)}

      <p class="muted">
        <strong>Revise e finalize manualmente no portal da vaga</strong>, depois clique em <em>Marcar como concluída</em> para registrar nesta lista.
      </p>
      <div id="candidatura-pdf-preview" class="preview-box hidden"></div>
    `;

    $("#btn-rebuild-pdf")?.addEventListener("click", (ev) =>
      handlePrepareJob(app.job_id, ev.currentTarget)
    );

    $("#btn-open-browser")?.addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        const res = await apiFetch(`/api/applications/${encodeURIComponent(app.id)}/open`, {
          method: "POST",
        });
        showToast(
          res.open_form_required
            ? "Guia aberta. Abra o formulário da vaga no Chrome e clique em Preencher etapa atual."
            : "Guia aberta e pronta. Clique em Preencher etapa atual para preencher."
        );
        await Promise.all([loadApplications(), loadBrowserStatus()]);
      } catch (err) {
        if (err.status === 503) loadBrowserStatus();
        showToast(err.message);
      } finally {
        btn.disabled = false;
      }
    });

    $("#btn-fill-step")?.addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        const res = await apiFetch(
          `/api/applications/${encodeURIComponent(app.id)}/fill-current-page`,
          { method: "POST" }
        );
        if (res.status === "review") {
          showToast("Etapa preenchida. Revise no navegador e avance para a próxima etapa.");
        } else if (res.status === "needs_user") {
          showToast("AÇÃO HUMANA NECESSÁRIA nesta etapa.");
        }
        await Promise.all([loadApplications(), loadBrowserStatus()]);
      } catch (err) {
        if (err.status === 503) loadBrowserStatus();
        showToast(err.message);
        await loadApplications();
      } finally {
        btn.disabled = false;
      }
    });

    $("#btn-list-pages")?.addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      const panel = $("#attach-pages-panel");
      if (!panel) return;
      if (!panel.classList.contains("hidden")) {
        panel.classList.add("hidden");
        return;
      }
      btn.disabled = true;
      try {
        const res = await apiFetch(`/api/applications/${encodeURIComponent(app.id)}/pages`);
        const pages = res.pages || [];
        if (!pages.length) {
          panel.innerHTML = `<p class="muted" style="margin:0;">Nenhuma aba segura encontrada no Chrome conectado.</p>`;
        } else {
          panel.innerHTML = `
            <div style="margin-bottom:6px;"><strong>Escolha uma aba aberta do Chrome para vincular:</strong></div>
            ${pages
              .map(
                (p) => `
                <div class="tab-choice-row">
                  <div>
                    <strong>${escapeHtml(p.title || "(sem título)")}</strong>
                    <div class="muted">Origem: <code>${escapeHtml(p.origin || "")}</code> · ${escapeHtml(p.url || "")}</div>
                    ${p.is_bound_here ? `<span class="badge badge-ok">Vinculada aqui</span>` : ""}
                    ${p.matches_vacancy ? `<span class="badge badge-info">Corresponde à vaga</span>` : ""}
                    ${p.bound_application_id && !p.is_bound_here ? `<span class="badge badge-warn">Em outra candidatura</span>` : ""}
                  </div>
                  <button type="button" class="btn-secondary btn-attach-tab" data-tab-id="${escapeAttr(p.tab_id)}" data-testid="btn-attach-tab">Vincular</button>
                </div>
              `
              )
              .join("")}
          `;
          panel.querySelectorAll(".btn-attach-tab").forEach((attachBtn) =>
            attachBtn.addEventListener("click", async () => {
              attachBtn.disabled = true;
              try {
                await apiFetch(`/api/applications/${encodeURIComponent(app.id)}/attach-page`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ tab_id: attachBtn.dataset.tabId }),
                });
                showToast("Aba vinculada! Clique em Preencher etapa atual para inspecionar e preencher.");
                panel.classList.add("hidden");
                await Promise.all([loadApplications(), loadBrowserStatus()]);
              } catch (attachErr) {
                showToast(attachErr.message);
                attachBtn.disabled = false;
              }
            })
          );
        }
        panel.classList.remove("hidden");
      } catch (err) {
        if (err.status === 503) loadBrowserStatus();
        showToast(err.message);
      } finally {
        btn.disabled = false;
      }
    });

    $("#step-answers-form")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const form = ev.currentTarget;
      const stepId = form.dataset.stepId;
      const items = [];
      form.querySelectorAll(".step-answer-input").forEach((input) => {
        const fid = input.dataset.fieldId;
        const val = String(input.value || "").trim();
        if (!fid || !val) return;
        const saveCb = form.querySelector(`.step-save-profile-cb[data-field-id="${CSS.escape(fid)}"]`);
        items.push({
          field_id: fid,
          value: val,
          save_to_profile: Boolean(saveCb && saveCb.checked),
        });
      });
      if (!items.length) {
        showToast("Preencha ao menos uma resposta antes de salvar.");
        return;
      }
      const submitBtn = form.querySelector('[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const res = await apiFetch(
          `/api/applications/${encodeURIComponent(app.id)}/answer-questions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              step_id: stepId,
              answers: items,
              profile_revision: state.profile?.revision ?? null,
            }),
          }
        );
        if (res.saved_to_profile_count > 0) {
          await loadProfile();
        }
        showToast("Respostas salvas! Clique em Preencher etapa atual para aplicar no navegador.");
        await loadApplications();
      } catch (err) {
        if (err.status === 409 && err.body?.detail?.code === "profile_conflict") {
          await loadProfile();
        }
        showToast(err.message);
        await loadApplications();
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });

    $("#btn-complete-app")?.addEventListener("click", async (ev) => {
      ev.currentTarget.disabled = true;
      try {
        await apiFetch(`/api/applications/${encodeURIComponent(app.id)}/complete`, {
          method: "POST",
        });
        showToast("Candidatura marcada como concluída.");
        await loadApplications();
      } catch (err) {
        showToast(err.message);
      }
    });

    $("#btn-toggle-pdf-preview")?.addEventListener("click", async () => {
      const pvBox = $("#candidatura-pdf-preview");
      if (!pvBox.classList.contains("hidden")) {
        pvBox.classList.add("hidden");
        return;
      }
      const pv = await apiFetch(`/api/resumes/${encodeURIComponent(app.resume_id)}/preview`);
      pvBox.textContent =
        `=== TEXTO EXTRAÍDO DO PDF (verificação pypdf) · rev ${pv.profile_revision ?? "?"} ===\n` +
        `${pv.extracted_text}\n\n` +
        `=== PROVENIÊNCIA ===\n${JSON.stringify(pv.diff_summary, null, 2)}`;
      pvBox.classList.remove("hidden");
    });
  }

  function renderApplicationsList() {
    const el = $("#applications-list");
    if (!state.applications.length) {
      el.innerHTML = `<p class="muted">Nenhuma candidatura preparada ainda.</p>`;
      return;
    }
    el.innerHTML = state.applications
      .map((a) => {
        const [label, badge] = STATUS_LABEL[a.status] || [a.status, "badge-neutral"];
        const isCurrent = a.id === state.activeApplicationId;
        const needsReopen =
          !a.bound_to_browser && (a.status === "review" || a.status === "needs_user");
        return `
          <div class="timeline-card" style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
            <div>
              <strong>${escapeHtml(a.job_title || "Vaga")} — ${escapeHtml(a.company || "")}</strong>
              <span class="badge ${badge}" style="margin-left:6px;">${escapeHtml(label)}</span>
              ${needsReopen ? `<span class="badge badge-neutral">Reabrir no navegador</span>` : ""}
              ${a.profile_revision_stale && a.status !== "done" ? `<span class="badge badge-warn">Perfil atualizado</span>` : ""}
              <div class="muted">Atualizada em ${escapeHtml((a.updated_at || "").slice(0, 19).replace("T", " "))}</div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button type="button" class="btn-secondary btn-focus-app" data-id="${escapeAttr(a.id)}">
                ${isCurrent ? "Selecionada" : "Abrir em Candidatura"}
              </button>
              ${
                a.status !== "done"
                  ? `<button type="button" class="btn-ok btn-complete-inline" data-id="${escapeAttr(a.id)}">Marcar concluída</button>`
                  : ""
              }
            </div>
          </div>
        `;
      })
      .join("");

    $$(".btn-focus-app").forEach((b) =>
      b.addEventListener("click", async () => {
        state.activeApplicationId = b.dataset.id;
        await renderActiveCandidatura();
        renderApplicationsList();
      })
    );
    $$(".btn-complete-inline").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/applications/${encodeURIComponent(b.dataset.id)}/complete`, {
          method: "POST",
        });
        showToast("Candidatura marcada como concluída.");
        await loadApplications();
      })
    );
  }

  /* === PERFIL MESTRE === */
  async function loadProfile() {
    const data = await apiFetch("/api/profile");
    state.profile = data;
    $("#profile-conflict-banner").classList.add("hidden");
    renderProfileEditor();
  }

  function splitCommaList(value) {
    return String(value || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function renderDynamicArraySection(containerId, items, renderItemHtml) {
    const el = $(containerId);
    if (!items.length) {
      el.innerHTML = `<p class="muted">Nenhum item cadastrado.</p>`;
      return;
    }
    el.innerHTML = items.map((item, idx) => renderItemHtml(item, idx)).join("");
    el.querySelectorAll(".btn-remove-item").forEach((btn) =>
      btn.addEventListener("click", () => {
        syncArraysFromDom();
        items.splice(Number(btn.dataset.idx), 1);
        renderDynamicLists();
      })
    );
  }

  function renderDynamicLists() {
    const doc = state.profile.document;
    renderDynamicArraySection("#profile-exp-list", doc.experience, (item, idx) => `
      <div class="profile-entry" data-kind="exp" data-idx="${idx}">
        <div class="section-head">
          <strong>Experiência #${idx + 1} · <code>${escapeHtml(item.id)}</code></strong>
          <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
        </div>
        <div class="settings-grid">
          <label>Empresa <input type="text" data-f="company" value="${escapeAttr(item.company || "")}" /></label>
          <label>Cargo <input type="text" data-f="role" value="${escapeAttr(item.role || "")}" /></label>
          <label>Período <input type="text" data-f="period" value="${escapeAttr(item.period || "")}" /></label>
          <label>Local <input type="text" data-f="location" value="${escapeAttr(item.location || "")}" /></label>
        </div>
        <label style="display:block;font-weight:600;font-size:0.88rem;">Resumo / Detalhes
          <textarea data-f="details" rows="2" style="width:100%;">${escapeHtml(item.details || "")}</textarea>
        </label>
        <label style="display:block;font-weight:600;font-size:0.88rem;margin-top:6px;">Destaques (1 tópico por linha)
          <textarea data-f="bullets" rows="3" style="width:100%;">${escapeHtml((item.bullets || []).map((b) => b.text).join("\n"))}</textarea>
        </label>
      </div>
    `);

    renderDynamicArraySection("#profile-edu-list", doc.education, (item, idx) => `
      <div class="profile-entry" data-kind="edu" data-idx="${idx}">
        <div class="section-head">
          <strong>Formação #${idx + 1} · <code>${escapeHtml(item.id)}</code></strong>
          <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
        </div>
        <div class="settings-grid">
          <label>Instituição <input type="text" data-f="institution" value="${escapeAttr(item.institution || "")}" /></label>
          <label>Curso / Grau <input type="text" data-f="degree" value="${escapeAttr(item.degree || "")}" /></label>
          <label>Período <input type="text" data-f="period" value="${escapeAttr(item.period || "")}" /></label>
        </div>
        <label style="display:block;font-weight:600;font-size:0.88rem;">Detalhes
          <textarea data-f="details" rows="2" style="width:100%;">${escapeHtml(item.details || "")}</textarea>
        </label>
      </div>
    `);

    renderDynamicArraySection("#profile-proj-list", doc.projects, (item, idx) => `
      <div class="profile-entry" data-kind="proj" data-idx="${idx}">
        <div class="section-head">
          <strong>Projeto #${idx + 1} · <code>${escapeHtml(item.id)}</code></strong>
          <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
        </div>
        <div class="settings-grid">
          <label>Nome do projeto <input type="text" data-f="name" value="${escapeAttr(item.name || "")}" /></label>
          <label>URL <input type="text" data-f="url" value="${escapeAttr(item.url || "")}" /></label>
        </div>
        <label style="display:block;font-weight:600;font-size:0.88rem;">Detalhes
          <textarea data-f="details" rows="2" style="width:100%;">${escapeHtml(item.details || "")}</textarea>
        </label>
        <label style="display:block;font-weight:600;font-size:0.88rem;margin-top:6px;">Destaques (1 tópico por linha)
          <textarea data-f="bullets" rows="2" style="width:100%;">${escapeHtml((item.bullets || []).map((b) => b.text).join("\n"))}</textarea>
        </label>
      </div>
    `);

    renderDynamicArraySection("#profile-skills-list", doc.skills, (item, idx) => `
      <div class="profile-entry inline-form" data-kind="skill" data-idx="${idx}">
        <input type="text" data-f="name" placeholder="Skill" value="${escapeAttr(item.name || "")}" style="flex:1;" />
        <input type="text" data-f="category" placeholder="Categoria (opcional)" value="${escapeAttr(item.category || "")}" />
        <input type="text" data-f="level" placeholder="Nível (opcional)" value="${escapeAttr(item.level || "")}" />
        <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
      </div>
    `);

    renderDynamicArraySection("#profile-langs-list", doc.languages, (item, idx) => `
      <div class="profile-entry inline-form" data-kind="lang" data-idx="${idx}">
        <input type="text" data-f="name" placeholder="Idioma" value="${escapeAttr(item.name || "")}" style="flex:1;" />
        <input type="text" data-f="proficiency" placeholder="Proficiência (ex.: Fluente)" value="${escapeAttr(item.proficiency || "")}" />
        <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
      </div>
    `);

    renderDynamicArraySection("#profile-std-list", doc.standard_answers, (item, idx) => `
      <div class="profile-entry" data-kind="std" data-idx="${idx}">
        <div class="section-head">
          <strong>Resposta #${idx + 1}</strong>
          <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
        </div>
        <div class="settings-grid">
          <label>Pergunta / Rótulo <input type="text" data-f="label" value="${escapeAttr(item.label || "")}" /></label>
          <label>Assinatura opcional <input type="text" data-f="question_signature" value="${escapeAttr(item.question_signature || "")}" /></label>
          <label>Tipo
            <select data-f="kind">
              <option value="text" ${item.kind === "text" ? "selected" : ""}>Texto</option>
              <option value="option" ${item.kind === "option" ? "selected" : ""}>Opção</option>
              <option value="number" ${item.kind === "number" ? "selected" : ""}>Número</option>
              <option value="boolean" ${item.kind === "boolean" ? "selected" : ""}>Sim/Não</option>
            </select>
          </label>
        </div>
        <label style="display:block;font-weight:600;font-size:0.88rem;">Resposta
          <textarea data-f="answer" rows="2" style="width:100%;">${escapeHtml(item.answer || "")}</textarea>
        </label>
      </div>
    `);

    renderDynamicArraySection("#profile-imports-list", doc.imports, (item, idx) => `
      <div class="profile-entry" data-kind="imp" data-idx="${idx}">
        <div class="section-head">
          <strong>${escapeHtml(item.filename)} · <code>${escapeHtml(item.kind)}</code></strong>
          <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
        </div>
        <div class="muted">SHA-256: <code>${escapeHtml(item.sha256.slice(0, 16))}…</code> · ${item.extracted_text.length} caracteres</div>
        <p style="font-size:0.85rem;white-space:pre-line;margin-bottom:0;">${escapeHtml(item.extracted_text.slice(0, 400))}${item.extracted_text.length > 400 ? "…" : ""}</p>
      </div>
    `);

    $("#profile-raw-json").value = JSON.stringify(doc, null, 2);
  }

  function syncBullets(existingBullets, rawText) {
    const lines = String(rawText || "")
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
    return lines.map((line, i) => ({
      id: (existingBullets && existingBullets[i] && existingBullets[i].id) || newClientId("bul"),
      text: line,
    }));
  }

  function syncArraysFromDom() {
    const doc = state.profile.document;
    if (!doc) return;

    $$("#profile-exp-list .profile-entry").forEach((el, idx) => {
      const target = doc.experience[idx];
      if (!target) return;
      target.company = el.querySelector('[data-f="company"]').value.trim() || null;
      target.role = el.querySelector('[data-f="role"]').value.trim() || null;
      target.period = el.querySelector('[data-f="period"]').value.trim() || null;
      target.location = el.querySelector('[data-f="location"]').value.trim() || null;
      target.details = el.querySelector('[data-f="details"]').value.trim();
      target.bullets = syncBullets(target.bullets, el.querySelector('[data-f="bullets"]').value);
    });

    $$("#profile-edu-list .profile-entry").forEach((el, idx) => {
      const target = doc.education[idx];
      if (!target) return;
      target.institution = el.querySelector('[data-f="institution"]').value.trim() || null;
      target.degree = el.querySelector('[data-f="degree"]').value.trim() || null;
      target.period = el.querySelector('[data-f="period"]').value.trim() || null;
      target.details = el.querySelector('[data-f="details"]').value.trim();
    });

    $$("#profile-proj-list .profile-entry").forEach((el, idx) => {
      const target = doc.projects[idx];
      if (!target) return;
      target.name = el.querySelector('[data-f="name"]').value.trim() || target.name;
      target.url = el.querySelector('[data-f="url"]').value.trim() || null;
      target.details = el.querySelector('[data-f="details"]').value.trim();
      target.bullets = syncBullets(target.bullets, el.querySelector('[data-f="bullets"]').value);
    });

    const skills = [];
    $$("#profile-skills-list .profile-entry").forEach((el, idx) => {
      const prev = doc.skills[idx];
      const name = el.querySelector('[data-f="name"]').value.trim();
      if (!name) return;
      skills.push({
        id: prev ? prev.id : newClientId("skl"),
        name,
        category: el.querySelector('[data-f="category"]').value.trim() || null,
        level: el.querySelector('[data-f="level"]').value.trim() || null,
        source: prev ? prev.source : "manual",
      });
    });
    doc.skills = skills;

    const langs = [];
    $$("#profile-langs-list .profile-entry").forEach((el, idx) => {
      const prev = doc.languages[idx];
      const name = el.querySelector('[data-f="name"]').value.trim();
      if (!name) return;
      langs.push({
        id: prev ? prev.id : newClientId("lng"),
        name,
        proficiency: el.querySelector('[data-f="proficiency"]').value.trim() || null,
        details: prev ? prev.details : null,
        source: prev ? prev.source : "manual",
      });
    });
    doc.languages = langs;

    const stds = [];
    $$("#profile-std-list .profile-entry").forEach((el, idx) => {
      const prev = doc.standard_answers[idx];
      const label = el.querySelector('[data-f="label"]').value.trim();
      if (!label) return;
      stds.push({
        id: prev ? prev.id : newClientId("ans"),
        label,
        question_signature: el.querySelector('[data-f="question_signature"]').value.trim() || null,
        kind: el.querySelector('[data-f="kind"]').value,
        answer: el.querySelector('[data-f="answer"]').value,
        source: prev ? prev.source : "manual",
        updated_at: prev ? prev.updated_at : null,
      });
    });
    doc.standard_answers = stds;
  }

  function renderProfileEditor() {
    const doc = state.profile.document;
    if (!doc) return;
    $("#profile-revision-badge").textContent = `Revisão ${state.profile.revision}`;

    $("#prof-name").value = doc.contact.name || "";
    $("#prof-email").value = doc.contact.email || "";
    $("#prof-phone").value = doc.contact.phone || "";
    $("#prof-city").value = doc.contact.city || "";
    $("#prof-state").value = doc.contact.state || "";
    $("#prof-country").value = doc.contact.country || "";
    $("#prof-linkedin").value = doc.contact.links?.linkedin || "";
    $("#prof-github").value = doc.contact.links?.github || "";
    $("#prof-portfolio").value = doc.contact.links?.portfolio || "";
    $("#prof-other-links").value = (doc.contact.links?.other || []).join(", ");

    $("#pref-target-roles").value = (doc.preferences.target_roles || []).join(", ");
    $("#pref-location").value = doc.preferences.location || "";
    $("#pref-work-model").value = (doc.preferences.work_model || []).join(", ");
    $("#pref-seniority").value = (doc.preferences.seniority || []).join(", ");
    $("#pref-contracts").value = (doc.preferences.contract_types || []).join(", ");
    $("#pref-salary-amount").value =
      doc.preferences.salary_amount != null ? doc.preferences.salary_amount : "";
    $("#pref-salary-currency").value = doc.preferences.salary_currency || "";
    $("#pref-salary-period").value = doc.preferences.salary_period || "";
    $("#pref-salary-text").value = doc.preferences.salary_text || "";
    $("#pref-availability").value = doc.preferences.availability || "";
    $("#pref-work-auth").value = doc.preferences.work_authorization || "";
    $("#pref-excluded").value = (doc.preferences.excluded_companies || []).join(", ");

    renderDynamicLists();
  }

  function collectProfileFromDom() {
    syncArraysFromDom();
    const doc = state.profile.document;
    doc.contact = {
      name: $("#prof-name").value.trim() || null,
      email: $("#prof-email").value.trim() || null,
      phone: $("#prof-phone").value.trim() || null,
      city: $("#prof-city").value.trim() || null,
      state: $("#prof-state").value.trim() || null,
      country: $("#prof-country").value.trim() || null,
      links: {
        linkedin: $("#prof-linkedin").value.trim() || null,
        github: $("#prof-github").value.trim() || null,
        portfolio: $("#prof-portfolio").value.trim() || null,
        other: splitCommaList($("#prof-other-links").value),
      },
    };
    const rawSalary = $("#pref-salary-amount").value.trim();
    doc.preferences = {
      target_roles: splitCommaList($("#pref-target-roles").value),
      location: $("#pref-location").value.trim() || null,
      work_model: splitCommaList($("#pref-work-model").value),
      seniority: splitCommaList($("#pref-seniority").value),
      contract_types: splitCommaList($("#pref-contracts").value),
      salary_amount: rawSalary ? Number(rawSalary) : null,
      salary_currency: $("#pref-salary-currency").value.trim() || null,
      salary_period: $("#pref-salary-period").value || null,
      salary_text: $("#pref-salary-text").value.trim() || null,
      availability: $("#pref-availability").value.trim() || null,
      work_authorization: $("#pref-work-auth").value.trim() || null,
      excluded_companies: splitCommaList($("#pref-excluded").value),
    };
    return doc;
  }

  $("#btn-add-exp").addEventListener("click", () => {
    syncArraysFromDom();
    state.profile.document.experience.push({
      id: newClientId("exp"),
      company: "",
      role: "",
      period: "",
      location: null,
      details: "",
      bullets: [],
      source: "manual",
      evidence: [],
    });
    renderDynamicLists();
  });
  $("#btn-add-edu").addEventListener("click", () => {
    syncArraysFromDom();
    state.profile.document.education.push({
      id: newClientId("edu"),
      institution: "",
      degree: "",
      period: "",
      details: "",
      source: "manual",
      evidence: [],
    });
    renderDynamicLists();
  });
  $("#btn-add-proj").addEventListener("click", () => {
    syncArraysFromDom();
    state.profile.document.projects.push({
      id: newClientId("prj"),
      name: "Novo projeto",
      url: null,
      details: "",
      bullets: [],
      source: "manual",
      evidence: [],
    });
    renderDynamicLists();
  });
  $("#btn-add-skill").addEventListener("click", () => {
    syncArraysFromDom();
    state.profile.document.skills.push({
      id: newClientId("skl"),
      name: "",
      category: null,
      level: null,
      source: "manual",
    });
    renderDynamicLists();
  });
  $("#btn-add-lang").addEventListener("click", () => {
    syncArraysFromDom();
    state.profile.document.languages.push({
      id: newClientId("lng"),
      name: "",
      proficiency: null,
      details: null,
      source: "manual",
    });
    renderDynamicLists();
  });
  $("#btn-add-std").addEventListener("click", () => {
    syncArraysFromDom();
    state.profile.document.standard_answers.push({
      id: newClientId("ans"),
      label: "",
      question_signature: null,
      kind: "text",
      answer: "",
      source: "manual",
      updated_at: null,
    });
    renderDynamicLists();
  });

  $("#btn-apply-raw-json").addEventListener("click", () => {
    try {
      const parsed = JSON.parse($("#profile-raw-json").value);
      state.profile.document = parsed;
      renderProfileEditor();
      showToast("JSON aplicado ao editor. Clique em Salvar Perfil Mestre para persistir.");
    } catch (err) {
      showToast(`JSON inválido: ${err.message}`);
    }
  });

  $("#profile-editor-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const doc = collectProfileFromDom();
    $("#profile-conflict-banner").classList.add("hidden");
    try {
      const updated = await apiFetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          document: doc,
          revision: state.profile.revision,
        }),
      });
      state.profile = updated;
      renderProfileEditor();
      showToast(`Perfil mestre salvo (revisão ${updated.revision}).`);
    } catch (err) {
      if (err.status === 409 && err.body?.detail?.current) {
        const banner = $("#profile-conflict-banner");
        banner.textContent =
          `${err.message} O formulário foi atualizado para a revisão ${err.body.detail.current.revision}. Revise antes de salvar novamente.`;
        banner.classList.remove("hidden");
        state.profile = err.body.detail.current;
        renderProfileEditor();
      } else {
        showToast(err.message);
      }
    }
  });

  $("#import-resume-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("#resume-file-input").files[0];
    const msgEl = $("#import-status-msg");
    if (!file) return;
    msgEl.textContent = "Extraindo texto do documento…";
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await apiFetch("/api/profile/import", { method: "POST", body: fd });
      state.profile = {
        document: res.document,
        revision: res.revision,
        updated_at: res.updated_at,
      };
      renderProfileEditor();
      const summary = res.import_summary || {};
      const fname = summary.filename || file.name;
      const shaShort = (summary.sha256 || "").slice(0, 12);
      msgEl.textContent = summary.deduplicated
        ? `Documento "${fname}" já importado (SHA-256 ${shaShort}).`
        : `Documento "${fname}" importado na revisão ${res.revision}.`;
      showToast(msgEl.textContent);
    } catch (err) {
      msgEl.textContent = err.message;
      showToast(err.message);
    }
  });

  /* === CONFIGURAÇÕES === */
  async function loadSettings() {
    const data = await apiFetch("/api/settings");
    state.settings = data;
    const p = data.preferences || {};
    const llm = data.llm || {};

    const llmBadge = $("#badge-llm-status");
    const llmActive = llm.provider && !["offline", "none"].includes(String(llm.provider).toLowerCase());
    if (!llmActive) {
      llmBadge.textContent = "LLM: offline determinístico";
      llmBadge.className = "badge badge-neutral";
    } else if (llm.is_local) {
      llmBadge.textContent = `LLM: ${llm.provider} (local ${llm.destination_host})`;
      llmBadge.className = "badge badge-ok";
    } else if (llm.allow_remote_profile) {
      llmBadge.textContent = `LLM remoto permitido (${llm.destination_host})`;
      llmBadge.className = "badge badge-warn";
    } else {
      llmBadge.textContent = `LLM remoto bloqueado (${llm.destination_host})`;
      llmBadge.className = "badge badge-danger";
    }

    const privacySummary = !llmActive
      ? "Modo offline determinístico ativo: nenhum dado do perfil é enviado a provedores de IA."
      : llm.is_local
      ? `Provedor LLM local ativo (${llm.provider} em ${llm.destination_host}): dados permanecem em loopback.`
      : llm.allow_remote_profile
      ? `Atenção: LLM remoto configurado (${llm.provider} em ${llm.destination_host}) com LLM_ALLOW_REMOTE_PROFILE=true. Contatos diretos e documentos de identidade nunca são enviados.`
      : `Destino LLM remoto (${llm.destination_host}) bloqueado para o Perfil mestre. Configure um endpoint local ou defina LLM_ALLOW_REMOTE_PROFILE=true para autorizar.`;

    $("#profile-llm-banner").textContent = privacySummary;
    $("#llm-diagnostics-box").innerHTML = `
      <div><strong>Provedor LLM:</strong> <code>${escapeHtml(llm.provider || "offline")}</code> · Modelo: <code>${escapeHtml(llm.model || "n/d")}</code> · Destino: <code>${escapeHtml(llm.destination_host || "local")}</code></div>
      <div class="muted" style="margin-top:4px;">${escapeHtml(privacySummary)}</div>
      <div class="muted" style="margin-top:4px;">Gasto remoto hoje: $${Number(llm.today_spend_usd || 0).toFixed(4)} / $${Number(llm.daily_budget_usd || 0).toFixed(2)}</div>
    `;

    $("#settings-form").innerHTML = `
      <label>Cargos alvo padrão (vírgula)
        <input type="text" id="set-roles" value="${escapeAttr((p.target_roles || []).join(", "))}" />
      </label>
      <label>Localizações preferidas (vírgula)
        <input type="text" id="set-locs" value="${escapeAttr((p.preferred_locations || []).join(", "))}" />
      </label>
      <label>Piso salarial opcional
        <input type="number" id="set-salary" value="${p.salary_floor ?? ""}" />
      </label>
      <label>Empresas excluídas (vírgula)
        <input type="text" id="set-excl" value="${escapeAttr((p.excluded_companies || []).join(", "))}" />
      </label>
      <div style="grid-column: 1 / -1;">
        <button type="submit" class="btn-primary">Salvar Preferências</button>
      </div>
    `;

    $("#settings-form").onsubmit = async (e) => {
      e.preventDefault();
      await apiFetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_roles: splitCommaList($("#set-roles").value),
          preferred_locations: splitCommaList($("#set-locs").value),
          salary_floor: $("#set-salary").value ? Number($("#set-salary").value) : null,
          excluded_companies: splitCommaList($("#set-excl").value),
        }),
      });
      showToast("Preferências atualizadas.");
      await loadSettings();
    };

    $("#connectors-list").innerHTML = (data.connectors || [])
      .map(
        (c) => `
        <div class="fact-row" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
          <div>
            <strong>${escapeHtml(c.name)}</strong> (${escapeHtml(c.kind || "browser")}${c.mode ? ` · modo: <code>${escapeHtml(c.mode)}</code>` : ""})
            <div class="muted">Status: ${escapeHtml(c.last_status)} · Coletadas: ${c.total_fetched ?? 0}${c.last_error ? ` · Erro: ${escapeHtml(c.last_error)}` : ""}</div>
          </div>
          <button type="button" class="btn-secondary btn-toggle-conn" data-name="${escapeAttr(c.name)}" data-next="${!c.enabled}">
            ${c.enabled ? "Desativar" : "Ativar"}
          </button>
        </div>
      `
      )
      .join("");

    $$(".btn-toggle-conn").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/connectors/${encodeURIComponent(b.dataset.name)}/toggle?enabled=${b.dataset.next}`, {
          method: "POST",
        });
        await loadSettings();
      })
    );
  }

  $("#btn-export-data").addEventListener("click", async () => {
    const data = await apiFetch("/api/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "careerpilot-export.json";
    a.click();
  });

  $("#btn-delete-data").addEventListener("click", async () => {
    if (!confirm("Remover todos os dados e currículos gerados localmente?")) return;
    await apiFetch("/api/data", { method: "DELETE" });
    state.activeApplicationId = null;
    showToast("Dados locais apagados.");
    await Promise.all([loadJobs(), loadApplications(), loadProfile()]);
  });

  checkSession();
})();
