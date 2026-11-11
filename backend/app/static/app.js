/* CareerPilot pt-BR Dashboard Client */
(() => {
  const state = {
    csrfToken: null,
    jobs: [],
    selectedJobId: null,
    applications: [],
    settings: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  function showToast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    setTimeout(() => el.classList.add("hidden"), 5000);
  }

  async function apiFetch(url, opts = {}) {
    const headers = new Headers(opts.headers || {});
    if (opts.method && opts.method !== "GET" && state.csrfToken) {
      headers.set("X-CSRF-Token", state.csrfToken);
    }
    const resp = await fetch(url, { ...opts, headers, credentials: "same-origin" });
    if (!resp.ok) {
      let detail = `Erro ${resp.status}`;
      try {
        const j = await resp.json();
        detail = j.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const ct = resp.headers.get("content-type") || "";
    return ct.includes("application/json") ? resp.json() : resp.text();
  }

  function switchTab(tabName) {
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
    $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tabName}`));
    if (tabName === "curriculos") loadProfile();
    if (tabName === "aprovacoes" || tabName === "candidaturas") loadApplications();
    if (tabName === "configuracoes") loadSettings();
  }

  $$(".tab-btn").forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );

  async function checkSession() {
    const res = await apiFetch("/api/auth/session");
    if (res.authenticated) {
      state.csrfToken = res.csrf_token;
      $("#unlock-panel").classList.add("hidden");
      $("#app-workspace").classList.remove("hidden");
      $("#btn-logout").classList.remove("hidden");
      $("#session-indicator").textContent = "Sessão local autenticada";
      await Promise.all([loadJobs(), loadApplications(), loadSettings()]);
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

  let lastLiveSearch = null;

  async function runDiscovery() {
    lastLiveSearch = null;
    showToast("Buscando vagas em conectores locais/mockados...");
    const res = await apiFetch("/api/jobs/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "data engineer python sql", limit: 60 }),
    });
    showToast(`Descoberta concluída: ${res.inserted} novas vagas (${res.deduped} duplicadas ignoradas).`);
    await loadJobs();
  }

  $("#btn-quick-discover").addEventListener("click", runDiscovery);
  function updateManualSearchLinks() {
    const text = $("#live-search-text").value.trim();
    const location = $("#live-search-location").value.trim();
    const remote = $("#live-search-remote").checked;
    const region = location && remote ? `("${location}" OR remoto OR remote)` : location || (remote ? "remoto" : "");
    const googleTerms = [text, region, "vagas empregos"].filter(Boolean);
    $("#live-search-google").href = "https://www.google.com/search?" + new URLSearchParams({ q: googleTerms.join(" ") });
    const linkedInQuery = new URLSearchParams({ keywords: text });
    if (location) linkedInQuery.set("location", location);
    $("#live-search-linkedin").href = "https://www.linkedin.com/jobs/search/?" + linkedInQuery;
    const remoteQuery = new URLSearchParams({ keywords: text, f_WT: "2" });
    $("#live-search-linkedin-remote").href = "https://www.linkedin.com/jobs/search/?" + remoteQuery;
    $("#live-search-linkedin-remote").hidden = !remote;
  }

  updateManualSearchLinks();
  $("#live-search-form").addEventListener("input", updateManualSearchLinks);
  $("#live-search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("#live-search-text").value.trim();
    if (!text) return;
    const button = event.submitter;
    const status = $("#live-search-status");
    button.disabled = true;
    status.textContent = "Consultando fontes públicas…";
    try {
      const location = $("#live-search-location").value.trim();
      const remote = $("#live-search-remote").checked;
      const result = await apiFetch("/api/jobs/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text, location: location || null, include_remote: remote && Boolean(location),
          remote_only: remote && !location, use_live_network: true, limit: 50,
        }),
      });
      lastLiveSearch = result.job_ids || [];
      $("#filter-q").value = "";
      $("#filter-mode").value = "";
      $("#filter-workplace").value = "";
      const failures = Object.keys(result.errors || {});
      status.textContent = `${result.inserted} vagas novas e ${result.deduped} já conhecidas. Fontes consultadas: ${result.sources_run.join(", ")}.`
        + (failures.length ? ` Indisponíveis nesta busca: ${failures.join(", ")}.` : "");
      await loadJobs();
      switchTab("radar");
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  $("#btn-apply-filters").addEventListener("click", loadJobs);

  async function loadJobs() {
    const params = new URLSearchParams();
    if (lastLiveSearch !== null) {
      params.set("live_only", "true");
      // An empty batch must not fall back to a previous search.
      for (const id of lastLiveSearch.length ? lastLiveSearch : ["none"]) params.append("job_ids", id);
    }
    if ($("#filter-q").value.trim()) params.set("q", $("#filter-q").value.trim());
    if ($("#filter-workplace").value) params.set("workplace", $("#filter-workplace").value);
    if ($("#filter-mode").value) params.set("apply_mode", $("#filter-mode").value);
    if (lastLiveSearch !== null || $("#filter-include-filtered").checked) params.set("include_filtered", "true");
    const data = await apiFetch(`/api/jobs?${params.toString()}`);
    state.jobs = data.jobs || [];
    $("#radar-count").textContent = `Exibindo ${data.total} vagas ranqueadas com evidências verificadas.`;
    renderJobsGrid();
  }

  function renderJobsGrid() {
    const grid = $("#jobs-grid");
    if (!state.jobs.length) {
      grid.innerHTML = `<p class="muted">Nenhuma vaga encontrada ainda. Busque por outras palavras-chave ou abra a demonstração.</p>`;
      return;
    }
    grid.innerHTML = state.jobs
      .map((j) => {
        const m = j.match || {};
        const score = m.relevance_score != null ? `${m.relevance_score}%` : "N/A";
        const salary =
          j.salary_min || j.salary_max
            ? `${j.salary_currency || ""} ${j.salary_min || "?"}–${j.salary_max || "?"}`
            : "Não informado";
        const inj = (j.injection_signals || []).length
          ? `<span class="badge badge-danger">Prompt injection sanitizado</span>`
          : "";
        return `
          <article class="job-card" data-job-id="${j.id}">
            <div>
              <div style="display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;">
                <span class="badge badge-info">${j.source}</span>
                <span class="badge ${j.apply_mode === "automated_allowed" ? "badge-ok" : "badge-warn"}">${j.apply_mode}</span>
                ${inj}
              </div>
              <h3>${escapeHtml(j.title)}</h3>
              <p class="muted"><strong>${escapeHtml(j.company)}</strong> · ${escapeHtml(j.location || "Não informado")} (${j.workplace_type})</p>
              <p class="muted">Aderência: <strong>${score}</strong> · Faixa: ${escapeHtml(salary)}</p>
              <p style="font-size:0.86rem;">${escapeHtml(m.explanation || "")}</p>
              ${lastLiveSearch !== null ? `<p>${escapeHtml(j.description.slice(0, 220))}${j.description.length > 220 ? "…" : ""}</p>` : ""}
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button type="button" class="btn-primary btn-open-job" data-id="${j.id}">Inspecionar Vaga</button>
              <a href="${escapeAttr(j.job_url)}" target="_blank" rel="noopener noreferrer" class="btn-secondary" style="text-decoration:none;">Link Original</a>
            </div>
          </article>
        `;
      })
      .join("");

    $$(".btn-open-job").forEach((b) =>
      b.addEventListener("click", () => openJobDetail(b.dataset.id))
    );
  }

  async function openJobDetail(jobId) {
    state.selectedJobId = jobId;
    const j = await apiFetch(`/api/jobs/${jobId}`);
    const m = j.match || {};
    const reqList = (j.requirements || [])
      .map((r) => `<li><strong>[${r.kind}]</strong> ${escapeHtml(r.term)} — <span class="muted">${escapeHtml(r.snippet)}</span></li>`)
      .join("");

    $("#job-detail-container").innerHTML = `
      <h2>${escapeHtml(j.title)}</h2>
      <p><strong>${escapeHtml(j.company)}</strong> · ${escapeHtml(j.location || "N/I")} · Fonte: <em>${escapeHtml(j.source_attribution)}</em></p>
      <p>Modo de candidatura: <span class="badge badge-info">${j.apply_mode}</span> · Link oficial: <a href="${escapeAttr(j.apply_url || j.job_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(j.apply_url || j.job_url)}</a></p>
      <p><strong>Análise de aderência:</strong> ${escapeHtml(m.explanation || "")}</p>
      <h3>Requisitos Identificados</h3>
      <ul>${reqList || "<li class='muted'>Sem requisitos estruturados extraídos.</li>"}</ul>
      <button type="button" id="btn-job-summary" class="btn-secondary">Resumir vaga</button>
      <p id="job-summary" aria-live="polite"></p>
      <h3>Descrição Sanitizada</h3>
      <p>${escapeHtml(j.description)}</p>
      <hr />
      <h3>Preparar Pacote de Candidatura</h3>
      <label class="checkbox-inline" style="display:block;margin-bottom:8px;">
        <input type="checkbox" id="prep-consent-check" />
        Autorizo incluir consentimento explícito de tratamento de dados neste pacote (<code>consent_privacy=sim</code>)
      </label>
      <label for="prep-salary-input" style="display:block;margin-bottom:6px;">Pretensão salarial opcional (deixe vazio se não quiser informar):</label>
      <input id="prep-salary-input" type="text" placeholder="Ex: R$ 7.500 CLT" style="width:100%;max-width:340px;margin-bottom:10px;" />
      <div>
        <button type="button" id="btn-prepare-app" class="btn-primary" data-testid="btn-prepare-app">Preparar Candidatura + Gerar Currículo PDF</button>
      </div>
    `;

    $("#btn-job-summary").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const output = $("#job-summary");
      button.disabled = true;
      output.textContent = "Preparando resumo…";
      try {
        const result = await apiFetch(`/api/jobs/${jobId}/summary`, { method: "POST" });
        output.textContent = `${result.method === "llm" ? "Resumo por IA" : "Trecho da descrição"}: ${result.summary}`;
      } catch (error) {
        output.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    $("#btn-prepare-app").addEventListener("click", async () => {
      const custom = {};
      if ($("#prep-consent-check").checked) custom.consent_privacy = "sim";
      const sal = $("#prep-salary-input").value.trim();
      if (sal) custom.salary_expectation = sal;
      try {
        const res = await apiFetch("/api/applications/prepare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: j.id, custom_answers: custom }),
        });
        showToast(`Pacote preparado (hash ${res.approval_request.packet_hash.slice(0, 10)}). Revise em Aprovações.`);
        await loadApplications();
        switchTab("aprovacoes");
      } catch (error) {
        showToast(error.message);
      }
    });

    switchTab("vaga");
  }

  async function loadProfile() {
    const data = await apiFetch("/api/profile");
    $("#badge-fictional").textContent = data.is_fictional_demo
      ? "Perfil Fictício Demo"
      : "Perfil Importado";

    $("#profile-facts-list").innerHTML = data.facts
      .map((f) => {
        const ev = (f.evidences || [])[0];
        return `
          <div class="fact-row">
            <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;">
              <strong>[${escapeHtml(f.category)}] ${escapeHtml(f.key)}</strong>
              <span class="badge ${f.user_verified ? "badge-ok" : "badge-warn"}">
                ${f.user_verified ? "Verificado" : "Aguardando confirmação"}
              </span>
            </div>
            <div>${escapeHtml(f.value)}</div>
            <div class="muted">Origem: ${escapeHtml(ev ? `${ev.document_name} (${ev.section_or_page})` : "manual")}</div>
            ${
              !f.user_verified
                ? `<button type="button" class="btn-secondary btn-verify-fact" data-id="${f.id}">Confirmar Fato</button>`
                : ""
            }
          </div>
        `;
      })
      .join("");

    $$(".btn-verify-fact").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/profile/facts/${b.dataset.id}/verify?verified=true`, { method: "POST" });
        showToast("Fato confirmado pelo usuário.");
        await loadProfile();
      })
    );

    $("#resumes-list").innerHTML = data.resumes.length
      ? data.resumes
          .map(
            (r) => `
            <div class="fact-row">
              <strong>Currículo #${r.id.slice(0, 8)}</strong> · SHA-256: <code>${r.pdf_sha256.slice(0, 16)}...</code>
              <div style="margin-top:6px;display:flex;gap:8px;">
                <button type="button" class="btn-secondary btn-preview-cv" data-id="${r.id}">Pré-visualizar Texto ATS & Diff</button>
                <a class="btn-primary" style="text-decoration:none;" href="/api/resumes/${r.id}/download">Baixar PDF</a>
              </div>
            </div>
          `
          )
          .join("")
      : `<p class="muted">Nenhum currículo específico gerado ainda. Prepare uma candidatura na aba Vaga.</p>`;

    $$(".btn-preview-cv").forEach((b) =>
      b.addEventListener("click", async () => {
        const pv = await apiFetch(`/api/resumes/${b.dataset.id}/preview`);
        const box = $("#resume-preview-box");
        box.textContent =
          `=== TEXTO EXTRAÍDO DO PDF (verificação pypdf) ===\n${pv.extracted_text}\n\n` +
          `=== DIFF & PROVENIÊNCIA (0 afirmações fabricadas) ===\n${JSON.stringify(pv.diff_summary, null, 2)}`;
        box.classList.remove("hidden");
      })
    );
  }

  $("#import-resume-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("#resume-file-input").files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const res = await apiFetch("/api/profile/import", { method: "POST", body: fd });
    showToast(`Importado ${res.document_name}: ${res.facts_extracted} fatos aguardando confirmação.`);
    await loadProfile();
  });

  async function loadApplications() {
    const data = await apiFetch("/api/applications");
    state.applications = data.applications || [];
    renderApprovals();
    renderTimeline();
  }

  function renderApprovals() {
    const el = $("#approvals-list");
    const relevant = state.applications.filter((a) =>
      ["awaiting_approval", "approved", "needs_user_input", "ready_for_manual_review"].includes(a.status)
    );
    if (!relevant.length) {
      el.innerHTML = `<p class="muted">Nenhuma aprovação pendente no momento.</p>`;
      return;
    }
    el.innerHTML = relevant
      .map((a) => {
        const ap = a.approval || {};
        const pendingQs = (a.pending_questions || [])
          .map(
            (q) =>
              `<div class="row"><label>${escapeHtml(q.label)} (${q.field_key})</label><input type="text" class="q-answer-input" data-app="${a.id}" data-key="${q.field_key}" /></div>`
          )
          .join("");
        return `
          <div class="approval-card" data-app-id="${a.id}">
            <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
              <strong>${escapeHtml(a.job_title)} — ${escapeHtml(a.company)}</strong>
              <span class="badge badge-info">${a.status}</span>
            </div>
            <p class="muted">Modo: <code>${a.apply_mode}</code> · Pacote: <code>${(ap.packet_hash || "").slice(0, 14)}</code> · PDF SHA-256: <code>${(a.resume_sha256 || "").slice(0, 12)}</code></p>
            <p class="muted">Motivo: ${escapeHtml((ap.fit_reasons || []).join("; "))}</p>
            ${
              pendingQs
                ? `<div style="margin:8px 0;"><strong>Perguntas pendentes:</strong>${pendingQs}<button type="button" class="btn-secondary btn-save-answers" data-id="${a.id}">Salvar Respostas e Gerar Novo Pacote</button></div>`
                : ""
            }
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
              ${a.resume_id ? `<a class="btn-secondary" style="text-decoration:none;" href="/api/resumes/${a.resume_id}/download">Ver PDF</a>` : ""}
              <a class="btn-secondary" style="text-decoration:none;" href="${escapeAttr(a.apply_url)}" target="_blank" rel="noopener noreferrer">Abrir Vaga</a>
              ${
                a.status === "awaiting_approval"
                  ? `<button type="button" class="btn-ok btn-approve" data-id="${a.id}" data-testid="btn-approve-${a.id}">Aprovar</button>
                     <button type="button" class="btn-danger btn-reject" data-id="${a.id}">Recusar</button>
                     <button type="button" class="btn-secondary btn-changes" data-id="${a.id}">Pedir alteração</button>`
                  : ""
              }
              ${
                a.status === "approved"
                  ? `<button type="button" class="btn-primary btn-exec-prepare" data-id="${a.id}">Executar (Dry-Run / Preencher)</button>
                     <button type="button" class="btn-ok btn-exec-submit" data-id="${a.id}">Executar e Enviar (se Live + Permitido)</button>`
                  : ""
              }
              ${
                ["approved", "ready_for_manual_review"].includes(a.status)
                  ? `<button type="button" class="btn-secondary btn-mark-manual" data-id="${a.id}">Marcar como enviada manualmente</button>`
                  : ""
              }
            </div>
          </div>
        `;
      })
      .join("");

    $$(".btn-approve").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/applications/${b.dataset.id}/approve`, { method: "POST" });
        showToast("Candidatura aprovada (uso único registrado).");
        await loadApplications();
      })
    );
    $$(".btn-reject").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/applications/${b.dataset.id}/reject`, { method: "POST" });
        showToast("Candidatura recusada.");
        await loadApplications();
      })
    );
    $$(".btn-changes").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/applications/${b.dataset.id}/request-changes`, { method: "POST" });
        showToast("Alterações solicitadas.");
        await loadApplications();
      })
    );
    $$(".btn-save-answers").forEach((b) =>
      b.addEventListener("click", async () => {
        const answers = {};
        $$(`.q-answer-input[data-app='${b.dataset.id}']`).forEach((inp) => {
          if (inp.value.trim()) answers[inp.dataset.key] = inp.value.trim();
        });
        await apiFetch(`/api/applications/${b.dataset.id}/answer-questions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answers }),
        });
        showToast("Respostas salvas e novo pacote gerado para aprovação.");
        await loadApplications();
      })
    );
    $$(".btn-exec-prepare").forEach((b) =>
      b.addEventListener("click", async () => {
        const res = await apiFetch(`/api/applications/${b.dataset.id}/execute`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ submit: false, allow_visual_fallback: true }),
        });
        showToast(res.browser_result?.evidence?.reason || `Execução concluída: status ${res.status}`);
        await loadApplications();
        switchTab("candidaturas");
      })
    );
    $$(".btn-exec-submit").forEach((b) =>
      b.addEventListener("click", async () => {
        const res = await apiFetch(`/api/applications/${b.dataset.id}/execute`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ submit: true }),
        });
        showToast(`Execução concluída: status ${res.status}`);
        await loadApplications();
        switchTab("candidaturas");
      })
    );
    $$(".btn-mark-manual").forEach((b) =>
      b.addEventListener("click", async () => {
        const ref = prompt("Informe o número de protocolo ou confirmação obtida manualmente:");
        if (!ref) return;
        await apiFetch(`/api/applications/${b.dataset.id}/mark-manual`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirmation_reference: ref }),
        });
        showToast("Envio manual confirmado com comprovante.");
        await loadApplications();
      })
    );
  }

  function renderTimeline() {
    const el = $("#applications-timeline");
    if (!state.applications.length) {
      el.innerHTML = `<p class="muted">Nenhuma candidatura registrada ainda.</p>`;
      return;
    }
    el.innerHTML = state.applications
      .map((a) => {
        const badgeCls =
          a.status === "verified"
            ? "badge-ok"
            : ["rejected", "blocked", "failed", "expired"].includes(a.status)
            ? "badge-danger"
            : "badge-info";
        const evidence = a.confirmation_receipt_id
          ? `Comprovante confirmado: <code>${escapeHtml(a.confirmation_receipt_id)}</code>`
          : "Ainda não enviada/verificada";
        return `
          <div class="timeline-card">
            <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
              <strong>${escapeHtml(a.job_title)} — ${escapeHtml(a.company)}</strong>
              <span class="badge ${badgeCls}">${a.status}</span>
            </div>
            <p class="muted">${evidence} · Modo: ${a.apply_mode}</p>
            ${a.last_error ? `<p class="alert-error">${escapeHtml(a.last_error)}</p>` : ""}
          </div>
        `;
      })
      .join("");
  }

  async function loadSettings() {
    const data = await apiFetch("/api/settings");
    state.settings = data;
    const p = data.preferences;
    const dryBadge = $("#badge-dry-run");
    if (p.dry_run || !p.live_mode_enabled) {
      dryBadge.textContent = "DRY_RUN ativo (seguro)";
      dryBadge.className = "badge badge-warn";
    } else {
      dryBadge.textContent = "LIVE TEST habilitado (requer aprovação 1-a-1)";
      dryBadge.className = "badge badge-ok";
    }

    $("#settings-form").innerHTML = `
      <label>Cargos alvo (separados por vírgula)
        <input type="text" id="set-roles" value="${escapeAttr((p.target_roles || []).join(", "))}" />
      </label>
      <label>Localizações preferidas
        <input type="text" id="set-locs" value="${escapeAttr((p.preferred_locations || []).join(", "))}" />
      </label>
      <label>Piso salarial opcional
        <input type="number" id="set-salary" value="${p.salary_floor || ""}" />
      </label>
      <label>Empresas excluídas
        <input type="text" id="set-excl" value="${escapeAttr((p.excluded_companies || []).join(", "))}" />
      </label>
      <label class="checkbox-inline">
        <input type="checkbox" id="set-dry-run" ${p.dry_run ? "checked" : ""} />
        DRY_RUN (impede clique final de Submit)
      </label>
      <label class="checkbox-inline">
        <input type="checkbox" id="set-live-mode" ${p.live_mode_enabled ? "checked" : ""} />
        Habilitar modo Live (nunca dispensa aprovação unitária)
      </label>
      <div style="grid-column: 1 / -1;">
        <button type="submit" class="btn-primary">Salvar Configurações</button>
      </div>
    `;

    $("#settings-form").onsubmit = async (e) => {
      e.preventDefault();
      await apiFetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_roles: $("#set-roles").value.split(",").map((s) => s.trim()).filter(Boolean),
          preferred_locations: $("#set-locs").value.split(",").map((s) => s.trim()).filter(Boolean),
          salary_floor: $("#set-salary").value ? Number($("#set-salary").value) : null,
          excluded_companies: $("#set-excl").value.split(",").map((s) => s.trim()).filter(Boolean),
          dry_run: $("#set-dry-run").checked,
          live_mode_enabled: $("#set-live-mode").checked,
        }),
      });
      showToast("Configurações atualizadas.");
      await loadSettings();
    };

    $("#connectors-list").innerHTML = data.connectors
      .map(
        (c) => `
        <div class="fact-row" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
          <div>
            <strong>${escapeHtml(c.name)}</strong> (${c.kind} · modo: <code>${c.mode}</code>)
            <div class="muted">Status: ${c.last_status} · Coletadas: ${c.total_fetched}${c.last_error ? ` · Erro: ${escapeHtml(c.last_error)}` : ""}</div>
          </div>
          <button type="button" class="btn-secondary btn-toggle-conn" data-name="${c.name}" data-next="${!c.enabled}">
            ${c.enabled ? "Desativar" : "Ativar"}
          </button>
        </div>
      `
      )
      .join("");

    $$(".btn-toggle-conn").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/connectors/${b.dataset.name}/toggle?enabled=${b.dataset.next}`, {
          method: "POST",
        });
        await loadSettings();
      })
    );

    const notifs = await apiFetch("/api/notifications");
    $("#notifications-box").innerHTML = `
      <p class="muted">Gmail modo: <code>${data.gmail.mode}</code> · Gasto LLM hoje: $${data.llm.today_spend_usd.toFixed(4)} / $${data.llm.daily_budget_usd.toFixed(2)}</p>
      ${(notifs.notifications || []).slice(0, 5).map((n) => `<div class="fact-row"><strong>${escapeHtml(n.subject)}</strong><div class="muted">${escapeHtml(n.body_preview)}</div></div>`).join("") || "<p class='muted'>Sem notificações locais ainda.</p>"}
    `;

    const sch = data.scheduler;
    $("#scheduler-box").innerHTML = `
      <p class="muted">Agendador: <strong>${sch.running ? (sch.paused ? "Pausado" : "Ativo") : "Parado"}</strong> · Intervalo: ${sch.interval_hours || 8}h</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button type="button" class="btn-secondary btn-sch" data-act="start">Iniciar</button>
        <button type="button" class="btn-secondary btn-sch" data-act="pause">Pausar</button>
        <button type="button" class="btn-secondary btn-sch" data-act="stop">Parar</button>
        <button type="button" class="btn-primary btn-sch" data-act="run_now">Executar Agora</button>
      </div>
    `;
    $$(".btn-sch").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiFetch(`/api/scheduler/${b.dataset.act}`, { method: "POST" });
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
    showToast("Dados locais apagados.");
    await Promise.all([loadJobs(), loadApplications()]);
  });

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, "&quot;");
  }

  checkSession();
})();
