/* CareerPilot pt-BR Dashboard Client — Phase 1 */
(() => {
  const state = {
    csrfToken: null,
    jobs: [],
    applications: [],
    activeApplicationId: null,
    profile: { document: null, revision: 0, updated_at: null },
    settings: null,
    lastLiveSearch: null,
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
      await Promise.all([loadJobs(), loadApplications(), loadSettings(), loadProfile()]);
      syncTabFromHash();
      $("#unlock-panel").classList.add("hidden");
      $("#app-workspace").classList.remove("hidden");
      $("#btn-logout").classList.remove("hidden");
      $("#session-indicator").textContent = "Sessão local autenticada";
    } else {
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
  $("#live-search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("#live-search-text").value.trim();
    if (!text) return;
    const button = event.submitter;
    const statusEl = $("#live-search-status");
    button.disabled = true;
    statusEl.textContent = "Consultando fontes configuradas…";
    try {
      const location = $("#live-search-location").value.trim();
      const remote = $("#live-search-remote").checked;
      const result = await apiFetch("/api/jobs/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          location: location || null,
          include_remote: remote && Boolean(location),
          remote_only: remote && !location,
          limit: 50,
        }),
      });
      state.lastLiveSearch = result.job_ids || [];
      const failures = Object.keys(result.errors || {});
      statusEl.textContent =
        `${result.inserted} novas vagas e ${result.deduped} já conhecidas. Fontes: ${result.sources_run.join(", ")}.` +
        (failures.length ? ` Indisponíveis: ${failures.join(", ")}.` : "");
      await loadJobs();
    } catch (error) {
      statusEl.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  $("#btn-apply-filters").addEventListener("click", () => {
    state.lastLiveSearch = null;
    loadJobs();
  });

  async function loadJobs() {
    const params = new URLSearchParams();
    if (state.lastLiveSearch !== null) {
      params.set("live_only", "true");
      for (const id of state.lastLiveSearch.length ? state.lastLiveSearch : ["none"]) {
        params.append("job_ids", id);
      }
    }
    const q = $("#filter-q").value.trim();
    const wp = $("#filter-workplace").value;
    if (q) params.set("q", q);
    if (wp) params.set("workplace", wp);
    const data = await apiFetch(`/api/jobs?${params.toString()}`);
    state.jobs = data.jobs || [];
    $("#vagas-count").textContent = `${data.total} vaga(s)`;
    renderJobsGrid();
  }

  function renderJobsGrid() {
    const grid = $("#jobs-grid");
    if (!state.jobs.length) {
      grid.innerHTML = `<p class="muted">Nenhuma vaga armazenada. Ajuste os filtros ou realize uma busca.</p>`;
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
        return `
          <article class="job-card" data-job-id="${escapeAttr(j.id)}">
            <div>
              <div style="display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;">
                <span class="badge badge-info">${escapeHtml(j.source)}</span>
                <span class="badge badge-neutral">${escapeHtml(j.workplace_type)}</span>
                ${inj}
              </div>
              <h3>${escapeHtml(j.title)}</h3>
              <p class="muted"><strong>${escapeHtml(j.company)}</strong> · ${escapeHtml(j.location || "Local não informado")}</p>
              <p class="muted">${escapeHtml(salary)}</p>
              <p style="font-size:0.88rem;">${escapeHtml((j.description || "").slice(0, 220))}${(j.description || "").length > 220 ? "…" : ""}</p>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button type="button" class="btn-primary btn-prepare-job" data-id="${escapeAttr(j.id)}" data-testid="btn-prepare-job-${escapeAttr(j.id)}">
                Preparar &amp; Aplicar
              </button>
              <button type="button" class="btn-secondary btn-detail-job" data-id="${escapeAttr(j.id)}">Detalhes</button>
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
  }

  async function openJobDetail(jobId) {
    const j = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    const box = $("#job-detail-container");
    const safeUrl = safeExternalUrl(j.apply_url || j.job_url);
    const reqList = (j.requirements || [])
      .map((r) => `<li><strong>[${escapeHtml(r.kind)}]</strong> ${escapeHtml(r.term)} — <span class="muted">${escapeHtml(r.snippet)}</span></li>`)
      .join("");
    box.classList.remove("hidden");
    box.innerHTML = `
      <div class="section-head">
        <h2>${escapeHtml(j.title)} — ${escapeHtml(j.company)}</h2>
        <button type="button" id="btn-close-detail" class="btn-secondary">Fechar detalhes</button>
      </div>
      <p class="muted">
        ${escapeHtml(j.location || "Local N/I")} · ${escapeHtml(j.workplace_type)} · Fonte: <em>${escapeHtml(j.source_attribution || j.source)}</em>
      </p>
      <p>
        Link original: <a href="${escapeAttr(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(safeUrl)}</a>
      </p>
      <div style="margin:10px 0;display:flex;gap:8px;flex-wrap:wrap;">
        <button type="button" id="btn-detail-prepare" class="btn-primary">Preparar &amp; Aplicar</button>
        <button type="button" id="btn-job-summary" class="btn-secondary">Resumir vaga</button>
      </div>
      <p id="job-summary" class="muted" aria-live="polite"></p>
      ${reqList ? `<h3>Requisitos identificados</h3><ul>${reqList}</ul>` : ""}
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
        out.textContent = `${res.method === "llm" ? "Resumo por IA" : "Trecho da descrição"}: ${res.summary}`;
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
      state.activeApplicationId = appPayload.application?.id || appPayload.id;
      await loadApplications();
      switchTab("candidatura");
      showToast("Currículo PDF preparado para revisão e candidatura.");
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
    needs_user: ["Ação humana necessária", "badge-danger"],
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

    box.innerHTML = `
      <div class="section-head">
        <div>
          <h2 style="margin:0;">${escapeHtml(app.job_title || "Vaga")} — ${escapeHtml(app.company || "")}</h2>
          <p class="muted" style="margin:4px 0 0 0;">
            ${escapeHtml(app.location || "Local N/I")} · ${escapeHtml(app.workplace_type || "")} · Método: <code>${escapeHtml(modeLabel)}</code>
          </p>
        </div>
        <span class="badge ${statusBadge}">${escapeHtml(statusLabel)}</span>
      </div>

      ${
        app.profile_revision_stale && app.status !== "done"
          ? `<div class="notice-box" style="margin:10px 0;">
               <strong>Perfil mestre atualizado:</strong> este PDF foi gerado com a revisão anterior (${app.resume_profile_revision}).
               <button type="button" id="btn-rebuild-pdf" class="btn-secondary" style="margin-left:8px;">Regerar PDF com perfil atual</button>
             </div>`
          : ""
      }
      ${app.last_error ? `<div class="card alert-box">${escapeHtml(app.last_error)}</div>` : ""}

      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;">
        ${
          app.resume_id
            ? `<a class="btn-secondary" href="/api/resumes/${encodeURIComponent(app.resume_id)}/download" data-testid="btn-download-pdf">Baixar PDF</a>
               <button type="button" id="btn-toggle-pdf-preview" class="btn-secondary">Pré-visualizar texto do PDF</button>`
            : ""
        }
        ${
          safeUrl !== "#"
            ? `<a class="btn-primary" href="${escapeAttr(safeUrl)}" target="_blank" rel="noopener noreferrer" data-testid="btn-open-job-link">Abrir vaga no navegador</a>`
            : `<span class="muted">URL da vaga indisponível</span>`
        }
        ${
          app.status !== "done"
            ? `<button type="button" id="btn-complete-app" class="btn-ok" data-testid="btn-complete-app">Marcar como concluída</button>`
            : ""
        }
      </div>

      <p class="muted">
        <strong>Revise e finalize manualmente no portal da vaga</strong>, depois clique em <em>Marcar como concluída</em> para registrar nesta lista.
      </p>
      <div id="candidatura-pdf-preview" class="preview-box hidden"></div>
    `;

    $("#btn-rebuild-pdf")?.addEventListener("click", (ev) =>
      handlePrepareJob(app.job_id, ev.currentTarget)
    );
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
        return `
          <div class="timeline-card" style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
            <div>
              <strong>${escapeHtml(a.job_title || "Vaga")} — ${escapeHtml(a.company || "")}</strong>
              <span class="badge ${badge}" style="margin-left:6px;">${escapeHtml(label)}</span>
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
            <strong>${escapeHtml(c.name)}</strong> (${escapeHtml(c.kind)} · modo: <code>${escapeHtml(c.mode)}</code>)
            <div class="muted">Status: ${escapeHtml(c.last_status)} · Coletadas: ${c.total_fetched}${c.last_error ? ` · Erro: ${escapeHtml(c.last_error)}` : ""}</div>
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
