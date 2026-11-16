/* CareerPilot pt-BR Dashboard Client — Phase 2 */
(() => {
  const state = {
    csrfToken: null,
    activeTab: "vagas",
    jobs: [],
    jobsCursor: null,
    jobsHasMore: false,
    applications: [],
    activeApplicationId: null,
    profile: { document: null, revision: 0, updated_at: null },
    settings: null,
    activeDiscoveryRunId: null,
    discoveryPollTimer: null,
    statusTimer: null,
    statusRequest: null,
    lastStatusSignature: null,
    preflightPollTimer: null,
    preflightPollCount: 0,
    pendingActionsCursor: null,
    pendingActionItems: [],
    focusAfterPendingDecision: null,
    opencodeStatus: null,
    opencodeThreads: [],
    opencodeActiveThreadId: null,
    opencodeActiveRunId: null,
    opencodeRunPollTimer: null,
    opencodeFocusJobId: null,
    opencodeFocusApplicationId: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  function applyTheme(theme) {
    const resolved = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = resolved;
    try {
      localStorage.setItem("careerpilot_theme", resolved);
    } catch (_) {}
    const btn = $("#btn-theme-toggle");
    if (btn) {
      btn.textContent = resolved === "dark" ? "Tema: Escuro" : "Tema: Claro";
      btn.setAttribute("aria-pressed", resolved === "light" ? "true" : "false");
    }
  }

  (() => {
    let initial = "dark";
    try {
      if (localStorage.getItem("careerpilot_theme") === "light") initial = "light";
    } catch (_) {}
    applyTheme(initial);
  })();

  $("#btn-theme-toggle")?.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    applyTheme(current === "dark" ? "light" : "dark");
  });

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

  function clearPreflightPoll() {
    if (state.preflightPollTimer) {
      clearTimeout(state.preflightPollTimer);
      state.preflightPollTimer = null;
    }
    state.preflightPollCount = 0;
  }

  function switchTab(tabName) {
    state.activeTab = tabName;
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
    $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tabName}`));
    if (tabName !== "preflight") clearPreflightPoll();
    if (tabName === "perfil") {
      if (!state.profile.document) loadProfile();
      loadArsenal();
    }
    if (tabName === "candidatura") loadApplications();
    if (tabName === "configuracoes") loadSettings();
    if (tabName === "preflight") {
      loadPortalSessions();
      loadPendingActions();
    }
  }

  $$(".tab-btn").forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );

  function syncTabFromHash() {
    if (window.location.hash === "#perfil") {
      switchTab("perfil");
    } else if (window.location.hash === "#candidatura") {
      switchTab("candidatura");
    } else if (window.location.hash === "#preflight") {
      switchTab("preflight");
    }
  }
  window.addEventListener("hashchange", syncTabFromHash);

  async function checkSession() {
    const res = await apiFetch("/api/auth/session");
    if (res.authenticated) {
      state.csrfToken = res.csrf_token;
      await Promise.all([
        loadJobs(),
        loadApplications(),
        loadSettings(),
        loadProfile(),
        loadOpenCodeThreads(),
      ]);
      syncTabFromHash();
      $("#unlock-panel").classList.add("hidden");
      $("#app-workspace").classList.remove("hidden");
      $("#btn-logout").classList.remove("hidden");
      $("#session-indicator").textContent = "Sessão local autenticada";
      $("#system-status-bar").classList.remove("hidden");
      loadBrowserStatus();
    } else {
      state.csrfToken = null;
      state.lastStatusSignature = null;
      clearTimeout(state.statusTimer);
      clearTimeout(state.opencodeRunPollTimer);
      state.opencodeRunPollTimer = null;
      state.opencodeActiveRunId = null;
      clearPreflightPoll();
      $("#system-status-bar").classList.add("hidden");
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

  function setStatusBadge(selector, text, style) {
    const badge = $(selector);
    if (badge.textContent !== text) badge.textContent = text;
    badge.className = `badge badge-${style}`;
  }

  function announceStatusIfChanged(sig, text) {
    if (state.lastStatusSignature === sig) return;
    state.lastStatusSignature = sig;
    const announcer = $("#status-live-announcer");
    if (announcer) announcer.textContent = text;
  }

  function loadBrowserStatus() {
    if (!state.csrfToken) return;
    if (state.statusRequest) return state.statusRequest;

    clearTimeout(state.statusTimer);
    const sessionToken = state.csrfToken;
    const detail = $("#browser-status-detail");
    let delay = 5000;
    state.statusRequest = apiFetch("/api/system/status", { signal: AbortSignal.timeout(8000) })
      .then((data) => {
        if (state.csrfToken !== sessionToken) return;
        const oc = data.opencode || {};
        state.opencodeStatus = oc;
        const extReady = Boolean(oc.ready);
        const badgeLabel = extReady
          ? `Conectada (${oc.paired_browser ? oc.paired_browser.split(" ")[0] : "browse"})`
          : oc.cli_available
            ? "Desconectada (parear browse)"
            : "Desconectada";
        setStatusBadge("#browser-status-badge", badgeLabel, extReady ? "ok" : "warn");

        const modelBadge = $("#opencode-model-badge");
        if (modelBadge && oc.model) {
          modelBadge.textContent = oc.model;
        }

        const engineNames = {
          offline_deterministic: "Offline determinístico",
          local_llm: "IA local",
          cloud_assisted: "IA mestre na nuvem",
        };
        const engine = data.engine || {};
        const engineLabel = engineNames[engine.mode] || "Indisponível";
        const engineDegraded = engine.available === false || Boolean(engine.fallback_reason);
        const engineStyle = engineDegraded
          ? "warn"
          : engine.mode === "local_llm" || engine.mode === "cloud_assisted"
          ? "ok"
          : "neutral";
        setStatusBadge(
          "#badge-llm-status",
          engineDegraded ? `${engineLabel} (fallback)` : engineLabel,
          engineStyle,
        );

        const pre = data.preflight || {};
        const loginReq = Number(pre.login_required || 0);
        const blocked = Number(pre.blocked_portals || 0);
        const count = Number(pre.pending_actions || 0);

        if (blocked > 0) {
          setStatusBadge("#preflight-summary-badge", `${blocked} bloqueado(s)`, "danger");
        } else if (loginReq > 0) {
          setStatusBadge("#preflight-summary-badge", `${loginReq} login(s)`, "warn");
        } else {
          setStatusBadge("#preflight-summary-badge", "Pronto", "ok");
        }

        setStatusBadge("#pending-count", String(count), count > 0 ? "warn" : "neutral");
        const message = extReady
          ? `Modelo: ${oc.model || "llamacpp/qwen-local"}`
          : oc.cli_available
            ? "Execute `browse pair` caso a extensão do navegador ainda não esteja pareada."
            : "Instale ou exponha o opencode-cli e a extensão browse no sistema.";
        if (detail && detail.textContent !== message) detail.textContent = message;

        const sig = `${extReady}|${oc.model || ""}|${engine.mode || ""}|${engine.available}|${engine.fallback_reason || ""}|${loginReq}|${blocked}|${count}`;
        announceStatusIfChanged(
          sig,
          `Extensão OpenCode ${extReady ? "conectada" : "desconectado"}. Motor ${engineLabel}. ${blocked} portais bloqueados, ${loginReq} logins necessários, ${count} pendências.`,
        );
      })
      .catch((error) => {
        if (state.csrfToken !== sessionToken) return;
        if (error.status === 401) {
          checkSession().catch(() => {});
          return;
        }
        setStatusBadge("#browser-status-badge", "Indisponível", "warn");
        setStatusBadge("#badge-llm-status", "Indisponível", "neutral");
        setStatusBadge("#preflight-summary-badge", "Indisponível", "warn");
        setStatusBadge("#pending-count", "—", "neutral");
        delay = 30000;
      })
      .finally(() => {
        state.statusRequest = null;
        if (state.csrfToken && !document.hidden) {
          state.statusTimer = setTimeout(loadBrowserStatus, state.csrfToken === sessionToken ? delay : 0);
        }
      });
    return state.statusRequest;
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(state.statusTimer);
    else loadBrowserStatus();
  });

  window.addEventListener("focus", () => {
    if (state.csrfToken) loadBrowserStatus();
  });

  $("#btn-refresh-browser-status").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await apiFetch("/api/browser/reconnect", {
        method: "POST",
        signal: AbortSignal.timeout(8000),
      });
      await state.statusRequest;
      await loadBrowserStatus();
      if (state.activeTab === "preflight") loadPortalSessions();
    } catch (_) {
      showToast("Não foi possível reconectar ao Chrome. Tente novamente.");
    } finally {
      button.disabled = false;
    }
  });

  function formatLocalTime(isoStr) {
    if (!isoStr) return "";
    try {
      const dt = new Date(isoStr);
      if (Number.isNaN(dt.getTime())) return "";
      return dt.toLocaleString("pt-BR");
    } catch (_) {
      return "";
    }
  }

  function renderPortalSessions(items) {
    const list = $("#preflight-portals");
    const summary = $("#preflight-summary-banner");
    const labels = {
      authenticated: ["Autenticado", "ok"],
      public_access: ["Acesso público", "info"],
      login_required: ["Login necessário", "danger"],
      challenge: ["Verificação no Chrome", "warn"],
      unavailable: ["Indisponível", "warn"],
      disconnected: ["Desconectado", "warn"],
      checking: ["Verificando…", "info"],
      unknown: ["Não verificado", "neutral"],
    };
    const reasonText = {
      browser_disconnected: "Chrome conectado via CDP necessário.",
      tenant_required: "Salve ou abra uma vaga da empresa para descobrir a URL do tenant.",
      no_open_tab: "Nenhuma aba deste portal está aberta no Chrome agora.",
      check_expired: "Verificação anterior expirada ou interrompida.",
      check_cancelled: "Verificação cancelada.",
      timeout: "Tempo limite excedido ao inspecionar a aba.",
    };

    const blockedCount = items.filter((i) => i.status === "challenge" || i.status === "unavailable").length;
    const loginCount = items.filter((i) => i.status === "login_required").length;
    const readyCount = items.filter((i) => i.status === "authenticated" || i.status === "public_access").length;
    const checkingCount = items.filter((i) => i.status === "checking").length;
    if (summary) {
      summary.textContent = `Resumo do pré-voo: ${readyCount} pronto(s) · ${loginCount} login(s) necessário(s) · ${blockedCount} bloqueado(s)${checkingCount ? ` · ${checkingCount} em verificação…` : ""}`;
    }

    list.innerHTML = items.map((item) => {
      const [label, style] = labels[item.status] || labels.unknown;
      const checkedLocal = formatLocalTime(item.checked_at);
      const expiredBadge = item.expired && item.status !== "checking" && item.status !== "disconnected"
        ? ` <span class="badge badge-warn">Expirado</span>`
        : "";
      const note = reasonText[item.reason_code] || "";
      const tenantMissing = item.auth_model === "tenant_dependent" && item.scope_key === "default";
      const loginDisabled = item.status === "disconnected" || tenantMissing;
      return `<div class="fact-row section-head" data-portal-row="${escapeAttr(item.portal_key)}" data-scope-row="${escapeAttr(item.scope_key)}">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          ${item.scope_key ? `<span class="muted">(${escapeHtml(item.scope_key)})</span>` : ""}
          <span class="badge badge-${style}">${label}</span>${expiredBadge}
          ${checkedLocal ? `<div class="muted" style="font-size:0.82rem;">Verificado em ${escapeHtml(checkedLocal)}</div>` : ""}
          ${note ? `<div class="muted" style="font-size:0.82rem;">${escapeHtml(note)}</div>` : ""}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" class="btn-secondary portal-verify-one" data-portal="${escapeAttr(item.portal_key)}" data-scope="${escapeAttr(item.scope_key)}"
            ${item.status === "disconnected" ? "disabled" : ""}>Reverificar</button>
          <button type="button" class="btn-secondary portal-login" data-portal="${escapeAttr(item.portal_key)}" data-scope="${escapeAttr(item.scope_key)}"
            ${loginDisabled ? "disabled" : ""}>Abrir login no navegador</button>
        </div>
      </div>`;
    }).join("");
  }

  function schedulePreflightPoll() {
    clearPreflightPoll();
    const tick = async () => {
      if (!state.csrfToken || state.activeTab !== "preflight") return;
      state.preflightPollCount += 1;
      try {
        const data = await apiFetch("/api/preflight/sessions", { signal: AbortSignal.timeout(8000) });
        renderPortalSessions(data.items || []);
        const stillChecking = (data.items || []).some((item) => item.status === "checking");
        if (stillChecking && state.preflightPollCount < 30) {
          state.preflightPollTimer = setTimeout(tick, 700);
        } else {
          loadBrowserStatus();
        }
      } catch (_) {}
    };
    state.preflightPollTimer = setTimeout(tick, 600);
  }

  async function loadPortalSessions(refresh = false, targets = null) {
    const list = $("#preflight-portals");
    const button = $("#btn-check-portals");
    if (refresh && button.disabled) return;
    if (refresh) {
      button.disabled = true;
      clearPreflightPoll();
      list.textContent = "Iniciando verificação das sessões…";
    }
    try {
      const opts = refresh
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(targets ? { targets } : {}),
            signal: AbortSignal.timeout(12000),
          }
        : { method: "GET", signal: AbortSignal.timeout(12000) };
      const data = await apiFetch(
        refresh ? "/api/preflight/sessions/verify" : "/api/preflight/sessions",
        opts,
      );
      const items = data.items || [];
      renderPortalSessions(items);
      if (items.some((item) => item.status === "checking")) {
        schedulePreflightPoll();
      } else {
        loadBrowserStatus();
      }
    } catch (error) {
      list.textContent = error.message || "Não foi possível verificar as sessões dos portais.";
    } finally {
      button.disabled = false;
    }
  }

  function renderPendingActionsList() {
    const list = $("#preflight-actions");
    const moreWrap = $("#preflight-actions-more");
    const kindLabels = {
      salary: ["Pretensão salarial", "warn"],
      sensitive: ["Dado sensível", "danger"],
      ambiguous: ["Pergunta ambígua", "warn"],
      low_confidence: ["Baixa confiança", "warn"],
      invalid: ["Validação necessária", "danger"],
      manual_step: ["Ação no Chrome", "danger"],
    };
    list.innerHTML = state.pendingActionItems.length
      ? state.pendingActionItems.map((item) => {
          const [kLabel, kStyle] = kindLabels[item.kind] || ["Revisão pendente", "neutral"];
          return `<div class="fact-row section-head">
            <div>
              <strong>${escapeHtml(item.job_title || "Candidatura")}</strong>${item.company ? ` <span class="muted">· ${escapeHtml(item.company)}</span>` : ""}
              <div>${escapeHtml(item.label || "Revisão pendente")} <span class="badge badge-${kStyle}">${kLabel}</span></div>
            </div>
            <button type="button" class="btn-secondary pending-application"
              data-id="${escapeAttr(item.application_id)}">Abrir candidatura</button>
          </div>`;
        }).join("")
      : "<p class='muted'>Nenhuma pendência registrada.</p>";
    if (moreWrap) {
      moreWrap.classList.toggle("hidden", !state.pendingActionsCursor);
    }
  }

  async function loadPendingActions(append = false) {
    const list = $("#preflight-actions");
    try {
      const qs = new URLSearchParams({ status: "pending", limit: "25" });
      if (append && state.pendingActionsCursor) qs.set("cursor", state.pendingActionsCursor);
      const data = await apiFetch(`/api/preflight/pending-actions?${qs.toString()}`);
      const nextItems = Array.isArray(data.items) ? data.items : [];
      state.pendingActionItems = append ? [...state.pendingActionItems, ...nextItems] : nextItems;
      state.pendingActionsCursor = data.next_cursor || null;
      renderPendingActionsList();
    } catch (_) {
      list.textContent = "Não foi possível carregar as pendências.";
    }
  }

  $("#preflight-actions").addEventListener("click", (event) => {
    const target = event.target.closest(".pending-application");
    if (!target) return;
    state.activeApplicationId = target.dataset.id;
    switchTab("candidatura");
  });

  $("#btn-refresh-pending-actions")?.addEventListener("click", () => {
    loadPendingActions(false);
    loadBrowserStatus();
  });

  $("#btn-load-more-pending")?.addEventListener("click", () => {
    if (state.pendingActionsCursor) loadPendingActions(true);
  });

  $("#btn-check-portals").addEventListener("click", () => {
    loadPortalSessions(true);
    loadPendingActions(false);
  });

  $("#preflight-portals").addEventListener("click", async (event) => {
    const verifyBtn = event.target.closest(".portal-verify-one");
    if (verifyBtn && !verifyBtn.disabled) {
      loadPortalSessions(true, [{ portal_key: verifyBtn.dataset.portal, scope_key: verifyBtn.dataset.scope }]);
      return;
    }
    const button = event.target.closest(".portal-login");
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const pKey = encodeURIComponent(button.dataset.portal);
      const sKey = encodeURIComponent(button.dataset.scope || "default");
      await apiFetch(`/api/preflight/sessions/${pKey}/${sKey}/open-login`, { method: "POST" });
      showToast("Portal aberto no Chrome. Após entrar, clique em Reverificar todas as sessões.");
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
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
        <button type="button" id="btn-cancel-discovery" class="btn-secondary">Cancelar busca</button>
      `;
      return;
    }
    if (run.status === "cancelled") {
      if (statusEl) statusEl.textContent = "Busca cancelada.";
      box.textContent = "Busca cancelada.";
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
        if (state.activeDiscoveryRunId !== runId) return;
        renderDiscoveryRunBanner(run);
        if (["completed", "failed", "cancelled"].includes(run.status)) {
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

  $("#discovery-run-status").addEventListener("click", async (event) => {
    const button = event.target.closest("#btn-cancel-discovery");
    const runId = state.activeDiscoveryRunId;
    if (!button || !runId) return;
    button.disabled = true;
    try {
      const run = await apiFetch(`/api/jobs/discovery-runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
      state.activeDiscoveryRunId = null;
      clearTimeout(state.discoveryPollTimer);
      renderDiscoveryRunBanner(run);
      $("#btn-discover-jobs").disabled = false;
      await loadJobs();
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });

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
    const arch = $("#filter-archetype")?.value || "";
    const sen = $("#filter-seniority")?.value || "";
    const contract = $("#filter-contract")?.value || "";
    const eligFilter = $("#filter-eligibility")?.value || "";
    const includeRestricted = Boolean($("#filter-include-restricted")?.checked);
    if (q) params.set("q", q);
    if (wp) params.set("workplace", wp);
    if (arch) params.set("archetype", arch);
    if (sen) params.set("seniority", sen);
    if (contract) params.set("contract", contract);
    if (eligFilter) params.set("eligibility", eligFilter);
    if ($("#filter-sort")?.value === "match") params.set("sort", "match");
    const minimumMatch = Number($("#filter-min-match")?.value || 0);
    if (Number.isInteger(minimumMatch) && minimumMatch > 0 && minimumMatch <= 100) {
      params.set("min_match", String(minimumMatch));
    }
    if (includeRestricted) params.set("include_restricted", "true");
    if (append && state.jobsCursor) {
      params.set("cursor", state.jobsCursor);
    }
    let data;
    try {
      data = await apiFetch(`/api/jobs?${params.toString()}`);
    } catch (error) {
      if (append && error.status === 409) return loadJobs();
      throw error;
    }
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
              ${j.match ? `<details><summary>Compatibilidade · ${j.match.score}%</summary>
                <p class="muted">Competências em comum: ${escapeHtml((j.match.skills || []).join(", ") || "Nenhuma identificada")}.</p>
                ${j.match.missing_requirements?.length ? `<p class="muted">Competências ausentes: ${escapeHtml(j.match.missing_requirements.join(", "))}.</p>` : ""}
                <p class="muted">Cargo compatível: ${escapeHtml((j.match.roles || []).join(", ") || "Sem correspondência exata")}.</p>
              </details>` : ""}
              <details>
                <summary>Avaliar vaga</summary>
                <div class="inline-form">
                  ${Object.entries({
                    high_fit: "Ótima aderência", too_senior: "Senioridade alta demais",
                    wrong_tech_stack: "Stack incompatível", onsite_outside_recife: "Presencial fora do Recife",
                    blacklist_company: "Bloquear empresa",
                  }).map(([action, label]) => {
                    const active = (j.feedback || []).includes(action);
                    return `<button type="button" class="btn-secondary job-feedback"
                      data-id="${escapeAttr(j.id)}" data-action="${action}" data-operation="${active ? "undo" : "apply"}">${active ? "Desfazer: " : ""}${label}</button>`;
                  }).join("")}
                </div>
              </details>
              <p style="font-size:0.88rem;">${escapeHtml(snippetSource.slice(0, 220))}${snippetSource.length > 220 ? "…" : ""}</p>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button type="button" class="btn-primary btn-prepare-job" data-id="${escapeAttr(j.id)}" data-testid="btn-prepare-job-${escapeAttr(j.id)}">
                Preparar PDF &amp; Chat OpenCode
              </button>
              <button type="button" class="btn-secondary btn-detail-job" data-id="${escapeAttr(j.id)}" data-testid="btn-detail-job-${escapeAttr(j.id)}">Detalhes</button>
              <button type="button" class="btn-secondary btn-dismiss-job" data-id="${escapeAttr(j.id)}" data-testid="btn-dismiss-job-${escapeAttr(j.id)}">Dispensar</button>
            </div>
          </article>
        `;
      })
      .join("");

    $$(".job-feedback").forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await apiFetch(`/api/jobs/${encodeURIComponent(button.dataset.id)}/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: button.dataset.action, operation: button.dataset.operation,
            request_id: newClientId("feedback"),
          }),
        });
        await loadJobs();
      } catch (error) {
        showToast(error.message);
      } finally {
        button.disabled = false;
      }
    }));
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
        <button type="button" id="btn-detail-prepare" class="btn-primary">Preparar PDF &amp; Chat OpenCode</button>
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
        body: JSON.stringify({ job_id: jobId, locale: $("#resume-locale")?.value || null }),
      });
      const appId = appPayload.application?.id || appPayload.id;
      state.activeApplicationId = appId;
      await loadApplications();
      switchTab("candidatura");
      await focusOpenCodeOnApplication(jobId, appId, false);
      showToast("Currículo PDF preparado e contexto enviado ao Chat OpenCode.");
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
    const labelStr = String(blocker.label || blocker.message || "");
    const isSensitiveOrSalary =
      blocker.code === "invalid_cpf" ||
      blocker.code === "salary_approval_required" ||
      /(cpf|rg|passaporte|passport|salario|remuneracao|salary|senha|password)/i.test(labelStr);
    const canSaveArsenal = !opts.length && !isSensitiveOrSalary && (ftype === "text" || ftype === "textarea");

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
    } else if (ftype === "date") {
      controlHtml = `<input type="date" class="step-answer-input" data-field-id="${fid}" style="width:100%;margin-top:4px;" />`;
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
        ${canSaveArsenal ? `
          <label class="checkbox-inline">
            <input type="checkbox" class="step-save-arsenal-cb" data-field-id="${fid}" />
            Salvar no arsenal com regras de uso
          </label>
          <div class="step-arsenal-scope-box hidden notice-box" data-scope-field-id="${fid}" style="margin-top:6px;padding:8px;">
            <div class="settings-grid">
              <label>Categoria
                <select class="step-arsenal-cat">
                  <option value="custom">Personalizada</option>
                  <option value="why_company">Por que esta empresa</option>
                  <option value="professional_summary">Resumo profissional</option>
                  <option value="technical_challenge">Desafio técnico</option>
                  <option value="finops_cloud_impact">FinOps / Impacto Cloud</option>
                  <option value="availability">Disponibilidade</option>
                  <option value="work_authorization">Autorização de trabalho</option>
                </select>
              </label>
              <label>Cargo
                <select class="step-arsenal-arch">
                  <option value="all">Qualquer cargo</option>
                  <option value="data_engineer">Engenharia de dados</option>
                  <option value="backend_python">Backend Python</option>
                  <option value="ml_engineer">Machine Learning</option>
                  <option value="intern_junior">Estágio ou júnior</option>
                </select>
              </label>
              <label>Tags de cargo (vírgula)
                <input type="text" class="step-arsenal-tags" placeholder="backend_python, data_engineering" maxlength="160" />
              </label>
              <label>Quando usar — palavras-chave/empresa (vírgula)
                <input type="text" class="step-arsenal-when" placeholder="Use {{company_name}} ou cite empresa/cargo" maxlength="200" />
              </label>
            </div>
          </div>` : ""}
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

    const pendingActions = Array.isArray(step.pending_actions) && step.pending_actions.length
      ? step.pending_actions
      : (Array.isArray(step.salary_actions) ? step.salary_actions : []);

    if (pendingActions.length && app.status !== "done") {
      const decisionCards = [];
      for (const action of pendingActions) {
        if (action.kind === "salary") {
          if (action.status === "approved") {
            decisionCards.push(`
              <div class="salary-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
                <div class="decision-meta">
                  <strong>Pretensão salarial · ${escapeHtml(action.label)}</strong>
                  <span class="badge badge-ok">Valor confirmado</span>
                </div>
                <div class="muted">Aprovação registrada nesta sessão. Clique em <em>Preencher etapa atual</em> para aplicar no Chrome.</div>
              </div>
            `);
            continue;
          }
          if (action.status === "applied") {
            decisionCards.push(`
              <div class="salary-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
                <div class="decision-meta">
                  <strong>Pretensão salarial · ${escapeHtml(action.label)}</strong>
                  <span class="badge badge-ok">Aplicado nesta etapa</span>
                </div>
              </div>
            `);
            continue;
          }
          if (action.status === "skipped") {
            decisionCards.push(`
              <div class="salary-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
                <div class="decision-meta">
                  <strong>${escapeHtml(action.label)}</strong>
                  <span class="badge badge-neutral">Campo opcional ignorado</span>
                </div>
              </div>
            `);
            continue;
          }
          if (action.status !== "pending") continue;

          const period = { monthly: "mensal", hourly: "por hora", annual: "anual" }[action.period] || action.period || "";
          const suggestion = action.suggestion;
          const meta = action.suggestion_meta || {};
          let suggestionHtml = `<p class="muted"><span class="badge badge-warn">Faixa indisponível</span> ${
            meta.conflicting_bands
              ? "Faixas salariais conflitantes encontradas nas configurações; informe explicitamente."
              : "Informe explicitamente o valor com moeda e período confirmados."
          }</p>`;
          let defaultAmount = "";
          let defaultMax = "";
          if (suggestion?.reason_code === "configured_band" && suggestion.min_minor != null && suggestion.max_minor != null) {
            const format = new Intl.NumberFormat("pt-BR", { style: "currency", currency: action.currency || "BRL" });
            suggestionHtml = `<p class="muted">Faixa sugerida ${escapeHtml(format.format(suggestion.min_minor / 100))} a ${escapeHtml(format.format(suggestion.max_minor / 100))} · ${escapeHtml(suggestion.basis)} · ${escapeHtml(suggestion.source_note)} (${escapeHtml(suggestion.as_of)}).</p>`;
            if (!meta.floor_exceeds_band) {
              defaultAmount = (Number(suggestion.min_minor) / 100).toFixed(2);
              defaultMax = (Number(suggestion.max_minor) / 100).toFixed(2);
            }
          }
          const floorWarningHtml = meta.floor_exceeds_band
            ? `<div class="review-box" style="margin:6px 0;">Piso salarial do perfil supera o teto da faixa sugerida para esta vaga. Confirme ou edite o valor desejado.</div>`
            : "";

          if (action.can_approve) {
            decisionCards.push(`
              <div class="salary-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
                <div class="decision-meta">
                  <strong>Pretensão salarial</strong> · <span>${escapeHtml(action.label)}</span>
                  <span class="badge badge-warn">${escapeHtml(action.currency || "")} · ${escapeHtml(period)}</span>
                </div>
                ${suggestionHtml}
                ${floorWarningHtml}
                <form class="salary-approval-form" data-action-id="${escapeAttr(action.id)}"
                  data-revision="${action.revision}" data-fingerprint="${escapeAttr(action.field_fingerprint)}"
                  data-currency="${escapeAttr(action.currency)}" data-period="${escapeAttr(action.period)}" data-shape="${escapeAttr(action.shape)}">
                  ${action.shape === "option" ? `
                    <label>Opção ${escapeHtml(period)} em ${escapeHtml(action.currency)}
                      <select name="option" required>
                        <option value="">Selecione</option>
                        ${(action.options || []).map((option) => `<option value="${escapeAttr(option.key)}">${escapeHtml(option.label)}</option>`).join("")}
                      </select>
                    </label>` : `
                    <label>${action.shape === "range" ? "Valor mínimo" : "Valor"} ${escapeHtml(period)} em ${escapeHtml(action.currency)}
                      <input name="amount" type="number" min="0" max="9999999999.99" step="0.01" value="${escapeAttr(defaultAmount)}" required>
                    </label>
                    ${action.shape === "range" ? `<label>Valor máximo
                      <input name="maximum" type="number" min="0" max="9999999999.99" step="0.01" value="${escapeAttr(defaultMax)}" required>
                    </label>` : ""}`}
                  <button type="submit" class="btn-primary">Confirmar valor</button>
                </form>
                <div class="decision-actions">
                  <button type="button" class="btn-secondary btn-edit-decision" data-edit-card="${escapeAttr(action.id)}">Editar valor</button>
                  ${action.can_skip ? `<button type="button" class="btn-secondary btn-skip-pending" data-action-id="${escapeAttr(action.id)}" data-revision="${action.revision}" data-fingerprint="${escapeAttr(action.field_fingerprint)}">Pular campo opcional</button>` : ""}
                </div>
              </div>
            `);
          } else {
            decisionCards.push(`
              <div class="salary-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
                <div class="decision-meta"><strong>Pretensão salarial · ${escapeHtml(action.label)}</strong></div>
                <p class="muted">Confira o formato, a moeda e o período e preencha este campo manualmente no Chrome.</p>
                ${action.can_skip ? `<div class="decision-actions"><button type="button" class="btn-secondary btn-skip-pending" data-action-id="${escapeAttr(action.id)}" data-revision="${action.revision}" data-fingerprint="${escapeAttr(action.field_fingerprint)}">Pular campo opcional</button></div>` : ""}
              </div>
            `);
          }
          continue;
        }

        const field = action.field || {};
        const kindBadgeMap = {
          sensitive: ["Dado sensível", "danger"],
          ambiguous: ["Pergunta ambígua", "warn"],
          low_confidence: ["Baixa confiança", "warn"],
          invalid: ["Validação necessária", "danger"],
          manual_step: ["Ação no Chrome", "danger"],
        };
        const [kLabel, kStyle] = kindBadgeMap[action.kind] || ["Revisão pendente", "neutral"];
        if (action.status === "approved") {
          decisionCards.push(`
            <div class="pending-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
              <div class="decision-meta">
                <strong>${escapeHtml(action.label)}</strong>
                <span class="badge badge-ok">Decisão aprovada</span>
              </div>
              <div class="muted">Aprovação protegida salva. Clique em <em>Preencher etapa atual</em> para aplicar.</div>
            </div>
          `);
          continue;
        }
        if (action.status === "applied" || action.status === "skipped") {
          decisionCards.push(`
            <div class="pending-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
              <div class="decision-meta">
                <strong>${escapeHtml(action.label)}</strong>
                <span class="badge ${action.status === "applied" ? "badge-ok" : "badge-neutral"}">${action.status === "applied" ? "Aplicado nesta etapa" : "Campo opcional ignorado"}</span>
              </div>
            </div>
          `);
          continue;
        }
        if (action.status !== "pending") continue;

        if (action.can_approve) {
          const ctrl = String(field.control || "text");
          const opts = Array.isArray(action.options) ? action.options : [];
          let fieldKind = "text";
          let inputHtml = `<label>Valor aprovado <input name="value" type="text" required autocomplete="off" /></label>`;
          if (ctrl === "checkbox") {
            fieldKind = "boolean";
            inputHtml = `<label>Escolha <select name="value" required><option value="">Selecione</option><option value="true">Sim</option><option value="false">Não</option></select></label>`;
          } else if ((ctrl === "select" || ctrl === "radio") && opts.length) {
            fieldKind = "option";
            inputHtml = `<label>Opção <select name="value" required><option value="">Selecione</option>${opts.map((o) => `<option value="${escapeAttr(o.key || o.label)}">${escapeHtml(o.label)}</option>`).join("")}</select></label>`;
          } else if (ctrl === "number") {
            fieldKind = "number";
            inputHtml = `<label>Valor numérico <input name="value" type="number" step="any" required autocomplete="off" /></label>`;
          } else if (ctrl === "date") {
            fieldKind = "text";
            inputHtml = `<label>Data <input name="value" type="date" required /></label>`;
          } else if (action.kind === "sensitive") {
            fieldKind = "text";
            inputHtml = `<label>Dado protegido <div style="display:flex;gap:8px;flex-wrap:wrap;"><input name="value" type="password" required autocomplete="off" /><button type="button" class="btn-secondary btn-toggle-secret">Mostrar</button></div></label>`;
          }
          const canSaveArsenalAction = action.kind !== "sensitive" && action.kind !== "salary" && action.kind !== "manual_step" && (ctrl === "text" || ctrl === "textarea");
          const arsenalToggleHtml = canSaveArsenalAction
            ? `<label class="checkbox-inline" style="margin-top:6px;"><input type="checkbox" name="save_to_arsenal" class="pending-save-arsenal-cb" /> Salvar no arsenal com regras de uso</label>
               <div class="pending-arsenal-scope-box hidden notice-box" style="margin-top:6px;padding:8px;">
                 <div class="settings-grid">
                   <label>Categoria
                     <select name="arsenal_category">
                       <option value="custom">Personalizada</option>
                       <option value="why_company">Por que esta empresa</option>
                       <option value="professional_summary">Resumo profissional</option>
                       <option value="technical_challenge">Desafio técnico</option>
                       <option value="finops_cloud_impact">FinOps / Impacto Cloud</option>
                       <option value="availability">Disponibilidade</option>
                       <option value="work_authorization">Autorização de trabalho</option>
                     </select>
                   </label>
                   <label>Tags (vírgula) <input name="arsenal_role_tags" type="text" placeholder="backend_python, data_engineering" maxlength="160" /></label>
                   <label>Quando usar — empresa/palavras-chave (vírgula) <input name="arsenal_when_to_use" type="text" placeholder="Ex.: Acme Corp, cloud" maxlength="200" /></label>
                 </div>
               </div>`
            : `<p class="muted" style="font-size:0.82rem;margin:4px 0;">${
                action.kind === "sensitive"
                  ? "Dados sensíveis nunca são salvos no Arsenal."
                  : "Apenas respostas textuais reutilizáveis podem ser salvas no Arsenal."
              }</p>`;

          decisionCards.push(`
            <div class="pending-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
              <div class="decision-meta">
                <strong>${escapeHtml(action.label)}</strong>
                <span class="badge badge-${kStyle}">${kLabel}</span>
              </div>
              <form class="pending-field-approval-form" data-action-id="${escapeAttr(action.id)}"
                data-revision="${action.revision}" data-fingerprint="${escapeAttr(action.field_fingerprint)}"
                data-field-kind="${escapeAttr(fieldKind)}" data-pending-kind="${escapeAttr(action.kind)}">
                ${inputHtml}
                ${arsenalToggleHtml}
                <div style="margin-top:8px;">
                  <button type="submit" class="btn-primary">Confirmar decisão</button>
                </div>
              </form>
              ${action.can_skip ? `<div class="decision-actions"><button type="button" class="btn-secondary btn-skip-pending" data-action-id="${escapeAttr(action.id)}" data-revision="${action.revision}" data-fingerprint="${escapeAttr(action.field_fingerprint)}">Pular campo opcional</button></div>` : ""}
            </div>
          `);
        } else if (action.kind !== "manual_step" || action.can_skip) {
          decisionCards.push(`
            <div class="pending-action-card notice-box" data-pending-card="${escapeAttr(action.id)}" tabindex="-1">
              <div class="decision-meta">
                <strong>${escapeHtml(action.label)}</strong>
                <span class="badge badge-${kStyle}">${kLabel}</span>
              </div>
              <p class="muted">${escapeHtml(field.reason || "Complete manualmente diretamente no navegador.")}</p>
              ${action.can_skip ? `<div class="decision-actions"><button type="button" class="btn-secondary btn-skip-pending" data-action-id="${escapeAttr(action.id)}" data-revision="${action.revision}" data-fingerprint="${escapeAttr(action.field_fingerprint)}">Pular campo opcional</button></div>` : ""}
            </div>
          `);
        }
      }
      if (decisionCards.length) {
        parts.push(`<div id="pending-decisions-region">${decisionCards.join("")}</div>`);
      }
    }

    if (step.pending_refill && app.status !== "done") {
      parts.push(`
        <div class="notice-box" style="margin:12px 0;">
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
              ? `<ul class="muted" style="margin:6px 0 0 18px;">${notices
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

    const prevPvBox = box.querySelector("#candidatura-pdf-preview");
    const keepPreviewOpen =
      prevPvBox &&
      !prevPvBox.classList.contains("hidden") &&
      prevPvBox.dataset.resumeId === String(app.resume_id || "");
    const prevPreviewText = keepPreviewOpen ? prevPvBox.textContent : "";

    box.innerHTML = `
      <div class="section-head">
        <div>
          <h2 style="margin:0;">${escapeHtml(app.job_title || "Vaga")} — ${escapeHtml(app.company || "")}</h2>
          <p class="muted" style="margin:4px 0 0 0;">
            ${escapeHtml(app.location || "Local N/I")} · ${escapeHtml(app.workplace_type || "")} · Método: <code>${escapeHtml(modeLabel)}</code>
          </p>
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <span class="badge badge-info">OpenCode + browse</span>
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
      ${app.last_error ? `<div class="card alert-box" style="margin:10px 0;">${escapeHtml(app.last_error)}</div>` : ""}

      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;">
        ${
          app.status !== "done"
            ? `<button type="button" id="btn-send-to-opencode" class="btn-primary" data-testid="btn-send-to-opencode">Aplicar via OpenCode (browse)</button>
               <button type="button" id="btn-focus-opencode-chat" class="btn-secondary" data-testid="btn-focus-opencode-chat">Abrir no Chat OpenCode</button>
               ${!app.profile_revision_stale ? `<button type="button" id="btn-rebuild-pdf" class="btn-secondary">Regerar PDF com IA</button>` : ""}`
            : ""
        }
        ${
          app.resume_id
            ? `<a class="btn-secondary" href="/api/resumes/${encodeURIComponent(app.resume_id)}/download" data-testid="btn-download-pdf">Baixar PDF</a>
                 <a class="btn-secondary" href="/api/resumes/${encodeURIComponent(app.resume_id)}/download?format=markdown">Baixar Markdown</a>
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

      ${renderStepPanel(app)}

      <p class="muted">
        <strong>Use o Chat OpenCode ao lado</strong> para navegar/preencher na sua aba do Chrome via extensão <code>browse</code>, revise antes do envio final e clique em <em>Marcar como concluída</em> ao terminar.
      </p>
      <div id="candidatura-pdf-preview" class="preview-box hidden" data-resume-id="${escapeAttr(app.resume_id || "")}"></div>
    `;
    if (keepPreviewOpen && prevPreviewText) {
      const nextPv = box.querySelector("#candidatura-pdf-preview");
      if (nextPv) {
        nextPv.textContent = prevPreviewText;
        nextPv.classList.remove("hidden");
      }
    }

    const restoreDecisionFocus = () => {
      if (!state.focusAfterPendingDecision) return;
      const target =
        $(`[data-pending-card="${CSS.escape(state.focusAfterPendingDecision)}"]`) ||
        $(".salary-action-card, .pending-action-card") ||
        $("#candidatura-active h2");
      if (target) {
        if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: true });
      }
      state.focusAfterPendingDecision = null;
    };

    $$(".btn-edit-decision").forEach((btn) => {
      btn.addEventListener("click", () => {
        const card = $(`[data-pending-card="${CSS.escape(btn.dataset.editCard)}"]`);
        const input = card?.querySelector("input:not([type=checkbox]), select, textarea");
        if (input) {
          input.focus();
          if (typeof input.select === "function") input.select();
        }
      });
    });

    $$(".btn-toggle-secret").forEach((btn) => {
      btn.addEventListener("click", () => {
        const inp = btn.parentElement?.querySelector("input");
        if (!inp) return;
        const show = inp.type === "password";
        inp.type = show ? "text" : "password";
        btn.textContent = show ? "Ocultar" : "Mostrar";
      });
    });

    $$(".salary-approval-form").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button");
        const { shape } = form.dataset;
        const toMinor = (name) => Math.round(form.elements.namedItem(name).valueAsNumber * 100);
        let value;
        if (shape === "option") {
          value = form.elements.namedItem("option").value;
        } else if (shape === "range") {
          value = { min_minor: toMinor("amount"), max_minor: toMinor("maximum") };
          if (value.min_minor > value.max_minor) {
            showToast("O valor máximo deve ser igual ou superior ao mínimo.");
            return;
          }
        } else {
          value = toMinor("amount");
        }
        button.disabled = true;
        try {
          await apiFetch(`/api/preflight/pending-actions/${encodeURIComponent(form.dataset.actionId)}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              revision: Number(form.dataset.revision),
              field_fingerprint: form.dataset.fingerprint,
              approval: {
                kind: "salary",
                shape,
                value,
                currency: form.dataset.currency,
                period: form.dataset.period,
              },
            }),
          });
          const card = form.closest(".salary-action-card");
          if (card) {
            card.innerHTML = `<div class="decision-meta"><strong>Valor confirmado.</strong> <span class="badge badge-ok">Aprovado</span></div><div class="muted">Clique em <em>Preencher etapa atual</em> para aplicar no navegador.</div>`;
            card.setAttribute("tabindex", "-1");
            card.focus({ preventScroll: true });
          } else {
            form.textContent = "Valor confirmado. Clique em Preencher etapa atual para aplicar.";
          }
          loadBrowserStatus();
        } catch (error) {
          showToast(error.message);
          if (error.status === 409) {
            state.focusAfterPendingDecision = form.dataset.actionId;
            await loadApplications();
          }
        } finally {
          button.disabled = false;
        }
      });
    });

    $$(".pending-save-arsenal-cb").forEach((cb) => {
      cb.addEventListener("change", () => {
        const box = cb.closest("form")?.querySelector(".pending-arsenal-scope-box");
        if (box) box.classList.toggle("hidden", !cb.checked);
      });
    });

    $$(".pending-field-approval-form").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        const rawVal = form.elements.namedItem("value").value;
        const fieldKind = form.dataset.fieldKind || "text";
        const val = fieldKind === "boolean" ? rawVal === "true" : String(rawVal);
        const saveCb = form.elements.namedItem("save_to_arsenal");
        const payload = {
          revision: Number(form.dataset.revision),
          field_fingerprint: form.dataset.fingerprint,
          approval: { kind: fieldKind, value: val },
        };
        if (saveCb && saveCb.checked && form.dataset.pendingKind !== "sensitive") {
          const catEl = form.elements.namedItem("arsenal_category");
          const tagsEl = form.elements.namedItem("arsenal_role_tags");
          const whenEl = form.elements.namedItem("arsenal_when_to_use");
          const whenKws = splitCommaList(whenEl ? whenEl.value : "");
          payload.save_to_arsenal = {
            category: catEl ? catEl.value : "custom",
            language: "pt-BR",
            role_archetype: "all",
            trigger_keywords: [],
            when_to_use: whenKws.length ? [{ all_keywords: whenKws }] : [],
            when_not_to_use: [],
            role_tags: splitCommaList(tagsEl ? tagsEl.value : ""),
          };
        }
        button.disabled = true;
        try {
          await apiFetch(`/api/preflight/pending-actions/${encodeURIComponent(form.dataset.actionId)}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const card = form.closest(".pending-action-card");
          if (card) {
            card.innerHTML = `<div class="decision-meta"><strong>Decisão aprovada.</strong> <span class="badge badge-ok">Aprovado</span></div><div class="muted">Clique em <em>Preencher etapa atual</em> para aplicar nesta guia.</div>`;
            card.setAttribute("tabindex", "-1");
            card.focus({ preventScroll: true });
          }
          loadBrowserStatus();
        } catch (error) {
          showToast(error.message);
          if (error.status === 409) {
            state.focusAfterPendingDecision = form.dataset.actionId;
            await loadApplications();
          }
        } finally {
          button.disabled = false;
        }
      });
    });

    $$(".btn-skip-pending").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await apiFetch(`/api/preflight/pending-actions/${encodeURIComponent(btn.dataset.actionId)}/skip`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              revision: Number(btn.dataset.revision),
              field_fingerprint: btn.dataset.fingerprint,
            }),
          });
          const card = btn.closest(".salary-action-card, .pending-action-card");
          if (card) {
            card.innerHTML = `<div class="decision-meta"><strong>Campo opcional ignorado.</strong> <span class="badge badge-neutral">Ignorado</span></div>`;
            card.setAttribute("tabindex", "-1");
            card.focus({ preventScroll: true });
          }
          loadBrowserStatus();
        } catch (error) {
          showToast(error.message);
          if (error.status === 409) {
            state.focusAfterPendingDecision = btn.dataset.actionId;
            await loadApplications();
          }
        } finally {
          btn.disabled = false;
        }
      });
    });

    restoreDecisionFocus();

    $("#btn-rebuild-pdf")?.addEventListener("click", (ev) =>
      handlePrepareJob(app.job_id, ev.currentTarget)
    );

    $("#btn-send-to-opencode")?.addEventListener("click", async () => {
      await focusOpenCodeOnApplication(app.job_id, app.id, true);
    });

    $("#btn-focus-opencode-chat")?.addEventListener("click", async () => {
      await focusOpenCodeOnApplication(app.job_id, app.id, false);
    });

    $$("#step-answers-form .step-save-arsenal-cb").forEach((cb) => {
      cb.addEventListener("change", () => {
        const fid = cb.dataset.fieldId;
        const scopeBox = fid
          ? cb.closest("form")?.querySelector(`.step-arsenal-scope-box[data-scope-field-id="${CSS.escape(fid)}"]`)
          : null;
        if (scopeBox) scopeBox.classList.toggle("hidden", !cb.checked);
      });
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
        const arsCb = form.querySelector(`.step-save-arsenal-cb[data-field-id="${CSS.escape(fid)}"]`);
        const saveToArsenal = Boolean(arsCb && arsCb.checked);
        const entryItem = {
          field_id: fid,
          value: val,
          save_to_profile: Boolean(saveCb && saveCb.checked),
          save_to_arsenal: saveToArsenal,
        };
        if (saveToArsenal) {
          const scopeBox = form.querySelector(`.step-arsenal-scope-box[data-scope-field-id="${CSS.escape(fid)}"]`);
          if (scopeBox) {
            const cat = scopeBox.querySelector(".step-arsenal-cat")?.value || "custom";
            const arch = scopeBox.querySelector(".step-arsenal-arch")?.value || "all";
            const tags = splitCommaList(scopeBox.querySelector(".step-arsenal-tags")?.value || "");
            const whenKws = splitCommaList(scopeBox.querySelector(".step-arsenal-when")?.value || "");
            if (cat !== "custom" || arch !== "all" || tags.length || whenKws.length) {
              entryItem.arsenal_meta = {
                category: cat,
                role_archetype: arch,
                role_tags: tags,
                when_to_use: whenKws.length ? [{ all_keywords: whenKws }] : [],
                when_not_to_use: [],
                trigger_keywords: [],
              };
            }
          }
        }
        items.push(entryItem);
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
          <div>
            <button type="button" class="btn-secondary btn-repair-import" data-id="${escapeAttr(item.id)}" style="margin-right:6px;">Prévia de reparo</button>
            <button type="button" class="btn-danger btn-remove-item" data-idx="${idx}">Remover</button>
          </div>
        </div>
        <div class="muted">SHA-256: <code>${escapeHtml(item.sha256.slice(0, 16))}…</code> · ${item.extracted_text.length} caracteres</div>
        <p style="font-size:0.85rem;white-space:pre-line;margin-bottom:0;">${escapeHtml(item.extracted_text.slice(0, 400))}${item.extracted_text.length > 400 ? "…" : ""}</p>
      </div>
    `);

    $$("#profile-imports-list .btn-repair-import").forEach((btn) =>
      btn.addEventListener("click", () => requestImportRepairPreview(btn.dataset.id))
    );

    $("#profile-raw-json").value = JSON.stringify(doc, null, 2);
  }

  function renderImportWarnings(warnings) {
    let box = $("#import-warnings-box");
    const anchor = $("#import-status-msg");
    if (!anchor || !anchor.parentElement) return;
    if (!box) {
      box = document.createElement("div");
      box.id = "import-warnings-box";
      box.className = "notice-box hidden";
      box.style.marginTop = "8px";
      anchor.parentElement.appendChild(box);
    }
    const items = Array.isArray(warnings) ? warnings : [];
    if (!items.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.classList.remove("hidden");
    box.innerHTML = `
      <strong>Avisos de importação (${items.length}):</strong>
      <ul style="margin:6px 0 0 18px;padding:0;">
        ${items
          .map((w) => {
            const loc = [
              w.section ? `seção ${escapeHtml(w.section)}` : null,
              w.page != null ? `página ${w.page}` : null,
              w.line != null ? `linha ${w.line}` : null,
            ]
              .filter(Boolean)
              .join(", ");
            return `<li>${loc ? `<code>[${loc}]</code> ` : ""}${escapeHtml(w.message || w.code)}</li>`;
          })
          .join("")}
      </ul>
    `;
  }

  async function requestImportRepairPreview(documentId) {
    const previewEl = $("#profile-repair-preview");
    if (!documentId || !previewEl) return;
    previewEl.classList.remove("hidden");
    previewEl.innerHTML = "Gerando prévia determinística de reparo…";
    try {
      const res = await apiFetch(
        `/api/profile/imports/${encodeURIComponent(documentId)}/repair-preview`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ base_revision: state.profile.revision }),
        }
      );
      const diff = res.diff || {};
      const counts = diff.counts || {};
      const replacedCount = (diff.grouped_replacements || []).reduce(
        (acc, g) => acc + ((g.replaced_ids || []).length || 0),
        0
      );
      const eduCount = (diff.recovered_education || []).length;
      const rmSkillCount = (diff.removed_empty_skills || []).length;
      const preservedCount = (diff.preserved_user_edited_ids || []).length;
      const orphanCount = (diff.orphaned_content || []).length;
      const warns = res.warnings || [];
      previewEl.innerHTML = `
        <div class="section-head">
          <strong>Prévia de reparo · documento <code>${escapeHtml(documentId)}</code> (revisão base ${res.base_revision})</strong>
          <button type="button" id="btn-close-repair-preview" class="btn-secondary">Fechar prévia</button>
        </div>
        <p style="margin:6px 0;">
          Fragmentos reagrupados: <strong>${replacedCount}</strong> ·
          Experiências consolidadas: <strong>${counts.experience ?? 0}</strong> ·
          Formações recuperadas: <strong>${eduCount}</strong> ·
          Skills vazias removidas: <strong>${rmSkillCount}</strong> ·
          Edições manuais preservadas: <strong>${preservedCount}</strong> ·
          Linhas órfãs: <strong>${orphanCount}</strong>
        </p>
        ${
          warns.length
            ? `<ul style="margin:6px 0 8px 18px;padding:0;">${warns
                .map((w) => `<li><code>${escapeHtml(w.code)}</code>: ${escapeHtml(w.message)}</li>`)
                .join("")}</ul>`
            : ""
        }
        <div style="margin-top:8px;">
          <button type="button" id="btn-apply-repair-preview" class="btn-primary">Aplicar reparo e salvar</button>
        </div>
      `;
      $("#btn-close-repair-preview").addEventListener("click", () => {
        previewEl.classList.add("hidden");
        previewEl.innerHTML = "";
      });
      $("#btn-apply-repair-preview").addEventListener("click", async () => {
        try {
          const updated = await apiFetch("/api/profile", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              document: res.proposed_document,
              revision: state.profile.revision,
            }),
          });
          state.profile = updated;
          previewEl.classList.add("hidden");
          previewEl.innerHTML = "";
          renderProfileEditor();
          showToast(`Reparo aplicado e salvo (revisão ${updated.revision}).`);
        } catch (err) {
          if (err.status === 409 && err.body?.detail?.current) {
            state.profile = err.body.detail.current;
            renderProfileEditor();
            const banner = $("#profile-conflict-banner");
            banner.textContent =
              `${err.message} O formulário foi atualizado para a revisão ${err.body.detail.current.revision}. Gere a prévia novamente.`;
            banner.classList.remove("hidden");
          } else {
            showToast(err.message);
          }
        }
      });
    } catch (err) {
      if (err.status === 409 && err.body?.detail?.current) {
        state.profile = err.body.detail.current;
        renderProfileEditor();
      }
      previewEl.textContent = err.message;
      showToast(err.message);
    }
  }

  function syncBullets(existingBullets, rawText) {
    const lines = String(rawText || "")
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
    return lines.map((line, i) => {
      const prev = existingBullets && existingBullets[i] ? existingBullets[i] : {};
      return {
        ...prev,
        id: prev.id || newClientId("bul"),
        text: line,
      };
    });
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
        ...(prev || {}),
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
    doc.schema_version = doc.schema_version || 2;
    doc.summaries = Array.isArray(doc.summaries) ? doc.summaries : [];
  }

  function renderProfileEditor() {
    const doc = state.profile.document;
    if (!doc) return;
    $("#profile-revision-badge").textContent = `Revisão ${state.profile.revision}`;
    $("#career-fact-form select[name='experience_id']").innerHTML = [
      `<option value="">Geral no Perfil mestre</option>`,
      ...(doc.experience || []).map(
        (entry) => `<option value="${escapeAttr(entry.id)}">${escapeHtml([entry.role, entry.company].filter(Boolean).join(" · ") || "Experiência")}</option>`
      ),
    ].join("");

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
    doc.schema_version = doc.schema_version || 2;
    doc.summaries = Array.isArray(doc.summaries) ? doc.summaries : [];
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
    msgEl.textContent = "Extraindo texto estruturado do documento…";
    renderImportWarnings([]);
    const fd = new FormData();
    fd.append("file", file);
    if (state.profile && typeof state.profile.revision === "number") {
      fd.append("base_revision", String(state.profile.revision));
    }
    try {
      const res = await apiFetch("/api/profile/import", { method: "POST", body: fd });
      state.profile = {
        document: res.document,
        revision: res.revision,
        updated_at: res.updated_at,
      };
      renderProfileEditor();
      const summary = res.import_summary || {};
      const result = res.import_result || {};
      const counts = result.counts || {};
      const fname = summary.filename || file.name;
      const shaShort = (summary.sha256 || "").slice(0, 12);
      if (result.duplicate || summary.deduplicated) {
        msgEl.textContent = `Documento "${fname}" já importado (SHA-256 ${shaShort}).`;
      } else {
        msgEl.textContent =
          `Documento "${fname}" importado na revisão ${res.revision}: ` +
          `${counts.experience ?? summary.added_experience ?? 0} exp., ` +
          `${counts.education ?? summary.added_education ?? 0} form., ` +
          `${counts.projects ?? summary.added_projects ?? 0} proj., ` +
          `${counts.skills ?? summary.added_skills ?? 0} skills, ` +
          `${counts.languages ?? summary.added_languages ?? 0} idiomas.`;
      }
      renderImportWarnings(result.warnings || []);
      showToast(msgEl.textContent);
    } catch (err) {
      if (err.status === 409 && err.body?.detail?.current) {
        const banner = $("#profile-conflict-banner");
        banner.textContent =
          `${err.message} O formulário foi atualizado para a revisão ${err.body.detail.current.revision}. Revise antes de importar novamente.`;
        banner.classList.remove("hidden");
        state.profile = err.body.detail.current;
        renderProfileEditor();
      }
      msgEl.textContent = err.message;
      showToast(err.message);
    }
  });

  $("#career-fact-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const review = $("#career-fact-review");
    const button = form.querySelector("button[type='submit']");
    const instruction = String(data.get("instruction") || "").trim();
    const expId = String(data.get("experience_id") || "").trim() || null;
    review.replaceChildren();
    button.disabled = true;
    try {
      const proposal = await apiFetch("/api/profile/change-proposals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_revision: state.profile.revision,
          instruction,
          target_experience_id: expId,
          language: data.get("language"),
          allow_remote_profile: data.has("allow_remote_profile"),
        }),
      });
      const dataClasses = Array.isArray(proposal.data_classes_sent) ? proposal.data_classes_sent : [];
      const diffList = Array.isArray(proposal.diff) ? proposal.diff : [];
      const uncertainties = Array.isArray(proposal.uncertainties) ? proposal.uncertainties : [];
      const diffHtml = diffList.length
        ? diffList.map((item) => {
            const deps = Array.isArray(item.depends_on) ? item.depends_on : [];
            const afterVal = typeof item.after === "string" ? item.after : JSON.stringify(item.after, null, 2);
            return `
              <div class="fact-row" style="margin:6px 0;padding:8px;border:1px solid var(--border);">
                <label class="checkbox-inline" style="font-weight:600;">
                  <input type="checkbox" class="proposal-op-cb" value="${escapeAttr(item.change_id)}" data-deps="${escapeAttr(deps.join(","))}" checked />
                  ${escapeHtml(item.label || item.op)}
                  <span class="badge badge-neutral">${escapeHtml(item.op || "")}</span>
                </label>
                ${deps.length ? `<div class="muted" style="font-size:0.82rem;">Depende de: <code>${escapeHtml(deps.join(", "))}</code></div>` : ""}
                ${item.before ? `<div class="muted" style="font-size:0.86rem;"><strong>Antes:</strong> ${escapeHtml(String(item.before))}</div>` : ""}
                <div style="font-size:0.9rem;"><strong>Depois:</strong> ${escapeHtml(afterVal)}</div>
                ${item.uncertainty ? `<div class="badge badge-warn" style="margin-top:4px;">${escapeHtml(item.uncertainty)}</div>` : ""}
              </div>`;
          }).join("")
        : `<p><strong>Rascunho:</strong> ${escapeHtml(proposal.draft?.text || "")}</p>`;
      const uncertHtml = uncertainties.length
        ? `<div class="review-box" style="margin:8px 0;"><strong>Pontos que exigem atenção:</strong><ul style="margin:4px 0 0 18px;">${uncertainties.map((u) => `<li>${escapeHtml(u.message || "")}</li>`).join("")}</ul></div>`
        : "";
      review.innerHTML = `<div class="notice-box" data-testid="proposal-diff-box">
        <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center;">
          <strong>Proposta pronta para revisão (expira em 24h)</strong>
          ${dataClasses.length ? `<span class="muted" style="font-size:0.82rem;">Enviado: ${dataClasses.map((d) => `<code>${escapeHtml(d)}</code>`).join(" ")}</span>` : ""}
        </div>
        ${uncertHtml}
        <div style="margin-top:8px;">${diffHtml}</div>
        <div class="inline-form" style="margin-top:10px;">
          <button type="button" class="btn-primary" data-decision="apply">Confirmar e aplicar selecionados</button>
          <button type="button" class="btn-secondary" data-decision="reject">Descartar proposta</button>
        </div>
      </div>`;

      const syncProposalDependencies = (changedCb) => {
        const allCbs = Array.from(review.querySelectorAll(".proposal-op-cb"));
        const byId = new Map(allCbs.map((cb) => [cb.value, cb]));
        if (changedCb.checked) {
          const queue = [changedCb.value];
          while (queue.length) {
            const cur = queue.shift();
            const cb = byId.get(cur);
            if (!cb) continue;
            for (const dep of (cb.dataset.deps || "").split(",").filter(Boolean)) {
              const depCb = byId.get(dep);
              if (depCb && !depCb.checked) {
                depCb.checked = true;
                queue.push(dep);
              }
            }
          }
        } else {
          const removed = new Set([changedCb.value]);
          let grew = true;
          while (grew) {
            grew = false;
            for (const cb of allCbs) {
              if (!cb.checked) continue;
              const deps = (cb.dataset.deps || "").split(",").filter(Boolean);
              if (deps.some((d) => removed.has(d))) {
                cb.checked = false;
                removed.add(cb.value);
                grew = true;
              }
            }
          }
        }
      };

      review.querySelectorAll(".proposal-op-cb").forEach((cb) => {
        cb.addEventListener("change", () => syncProposalDependencies(cb));
      });

      review.querySelectorAll("[data-decision]").forEach((decisionButton) => {
        decisionButton.addEventListener("click", async () => {
          const decision = decisionButton.dataset.decision;
          const bodyObj = { decision, base_revision: proposal.base_revision };
          if (decision === "apply" && diffList.length) {
            const selectedIds = Array.from(review.querySelectorAll(".proposal-op-cb:checked")).map((cb) => cb.value);
            if (!selectedIds.length) {
              showToast("Selecione ao menos uma alteração para aplicar.");
              return;
            }
            bodyObj.selected_change_ids = selectedIds;
          }
          const buttons = review.querySelectorAll("button");
          buttons.forEach((control) => { control.disabled = true; });
          try {
            const resolved = await apiFetch(`/api/profile/change-proposals/${encodeURIComponent(proposal.id)}/resolve`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(bodyObj),
            });
            if (resolved.status === "applied") {
              state.profile = resolved.profile;
              renderProfileEditor();
              await loadArsenal();
              review.textContent = "Alterações selecionadas aplicadas ao Perfil mestre.";
              form.reset();
            } else {
              review.textContent = resolved.status === "stale" ? "Proposta expirada." : "Proposta descartada.";
            }
          } catch (error) {
            showToast(error.message);
            buttons.forEach((control) => { control.disabled = false; });
          }
        });
      });
    } catch (error) {
      review.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  const ARSENAL_CATEGORY_LABELS = {
    professional_summary: "Resumo profissional",
    why_company: "Por que esta empresa",
    technical_challenge: "Desafio técnico",
    finops_cloud_impact: "FinOps / Impacto Cloud",
    availability: "Disponibilidade",
    work_authorization: "Autorização de trabalho",
    english_proficiency: "Proficiência em inglês",
    salary_note: "Nota salarial (visual)",
    custom: "Personalizada",
  };
  const ARSENAL_ARCHETYPE_LABELS = {
    all: "Qualquer cargo",
    data_engineer: "Engenharia de dados",
    backend_python: "Backend Python",
    ml_engineer: "Machine Learning",
    intern_junior: "Estágio ou júnior",
  };

  function formatArsenalRoutePreview(entry) {
    const bits = [];
    bits.push(`Cargo: ${ARSENAL_ARCHETYPE_LABELS[entry.role_archetype] || entry.role_archetype || "all"}`);
    if (Array.isArray(entry.role_tags) && entry.role_tags.length) {
      bits.push(`Tags: ${entry.role_tags.join(", ")}`);
    }
    if (Array.isArray(entry.when_to_use) && entry.when_to_use.length) {
      const kws = entry.when_to_use.flatMap((c) => [...(c.all_keywords || []), ...(c.any_skills || []), ...(c.archetype_in || [])]);
      if (kws.length) bits.push(`Usar quando: ${kws.join(", ")}`);
    }
    if (Array.isArray(entry.when_not_to_use) && entry.when_not_to_use.length) {
      const kws = entry.when_not_to_use.flatMap((c) => [...(c.all_keywords || []), ...(c.archetype_in || [])]);
      if (kws.length) bits.push(`Evitar em: ${kws.join(", ")}`);
    }
    if (Array.isArray(entry.trigger_keywords) && entry.trigger_keywords.length) {
      bits.push(`Gatilhos: ${entry.trigger_keywords.join(", ")}`);
    }
    return bits.join(" · ");
  }

  async function loadArsenal() {
    const list = $("#arsenal-list");
    try {
      const params = new URLSearchParams();
      const st = $("#arsenal-filter-status")?.value || "";
      const lang = $("#arsenal-filter-language")?.value || "";
      const cat = $("#arsenal-filter-category")?.value || "";
      if (st) params.set("status", st);
      if (lang) params.set("language", lang);
      if (cat) params.set("category", cat);
      const qs = params.toString() ? `?${params.toString()}` : "";
      const { items, examples } = await apiFetch(`/api/arsenal${qs}`);
      state.arsenalItems = items || [];
      $("#arsenal-examples").innerHTML = (examples || []).map((example) =>
        `<p><strong>${escapeHtml(example.question)}</strong><br>${escapeHtml(example.text)}</p>`
      ).join("");
      const statusBadgeMap = {
        approved: ["Aprovada", "badge-ok"],
        draft: ["Rascunho", "badge-neutral"],
        retired: ["Arquivada", "badge-neutral"],
      };
      list.innerHTML = state.arsenalItems.map((entry) => {
        const [stLabel, stClass] = statusBadgeMap[entry.status] || [entry.status, "badge-neutral"];
        const catLabel = ARSENAL_CATEGORY_LABELS[entry.category] || entry.category || "custom";
        const variantsObj = entry.length_variants || entry.variants || {};
        const variantsHtml = ["short", "medium", "long"]
          .filter((tier) => variantsObj[tier])
          .map((tier) => {
            const label = { short: "Curta", medium: "Média", long: "Longa" }[tier];
            return `<p style="margin:4px 0;"><strong>${label}:</strong> ${escapeHtml(variantsObj[tier])}</p>`;
          })
          .join("");
        const whenKeywords = (entry.when_to_use || []).flatMap((c) => c.all_keywords || []).join(", ");
        const canApprove = entry.status === "draft" && !entry.display_only && !entry.is_instructional_draft;
        return `
        <article class="fact-row" data-arsenal-id="${escapeAttr(entry.id)}">
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
            <strong>${escapeHtml(entry.question)}</strong>
            <span class="badge badge-neutral">rev ${entry.revision}</span>
            <span class="badge badge-neutral">${entry.language === "en" ? "Inglês" : "Português"}</span>
            <span class="badge badge-neutral">${escapeHtml(catLabel)}</span>
            <span class="badge ${stClass}">${escapeHtml(stLabel)}</span>
            ${entry.display_only ? `<span class="badge badge-warn">Somente exibição visual (salary_note)</span>` : ""}
            ${entry.is_instructional_draft ? `<span class="badge badge-warn">Instrução — substitua antes de aprovar</span>` : ""}
          </div>
          <div class="muted" style="font-size:0.84rem;margin:4px 0;">${escapeHtml(formatArsenalRoutePreview(entry))}</div>
          ${variantsHtml}
          <div class="inline-form" style="margin-top:6px;">
            <button type="button" class="btn-secondary" data-arsenal-action="edit" data-id="${escapeAttr(entry.id)}" data-revision="${entry.revision}">Editar</button>
            ${canApprove ? `<button type="button" class="btn-primary" data-arsenal-action="approve" data-id="${escapeAttr(entry.id)}" data-revision="${entry.revision}">Aprovar reutilização</button>` : ""}
            ${entry.status !== "retired" ? `<button type="button" class="btn-secondary" data-arsenal-action="retire" data-id="${escapeAttr(entry.id)}" data-revision="${entry.revision}">Arquivar</button>` : ""}
          </div>
          <form class="arsenal-edit-form hidden settings-grid" data-id="${escapeAttr(entry.id)}" data-revision="${entry.revision}" style="margin-top:8px;padding:8px;border:1px dashed var(--border);">
            <label>Pergunta exata <input name="question" maxlength="140" value="${escapeAttr(entry.question || "")}" required /></label>
            <label>Categoria
              <select name="category">
                ${Object.entries(ARSENAL_CATEGORY_LABELS).map(([k, lbl]) => `<option value="${k}" ${entry.category === k ? "selected" : ""}>${escapeHtml(lbl)}</option>`).join("")}
              </select>
            </label>
            <label>Idioma
              <select name="language">
                <option value="pt-BR" ${entry.language === "pt-BR" ? "selected" : ""}>Português</option>
                <option value="en" ${entry.language === "en" ? "selected" : ""}>Inglês</option>
              </select>
            </label>
            <label>Cargo
              <select name="role_archetype">
                ${Object.entries(ARSENAL_ARCHETYPE_LABELS).map(([k, lbl]) => `<option value="${k}" ${entry.role_archetype === k ? "selected" : ""}>${escapeHtml(lbl)}</option>`).join("")}
              </select>
            </label>
            <label>Termos alternativos (vírgula) <input name="triggers" maxlength="400" value="${escapeAttr((entry.trigger_keywords || []).join(", "))}" /></label>
            <label>Tags de cargo (vírgula) <input name="role_tags" maxlength="240" value="${escapeAttr((entry.role_tags || []).join(", "))}" /></label>
            <label>Quando usar — palavras-chave/empresa (vírgula) <input name="when_to_use" maxlength="300" value="${escapeAttr(whenKeywords)}" /></label>
            <label>Resposta curta <input name="short" maxlength="150" value="${escapeAttr(variantsObj.short || "")}" /></label>
            <label>Resposta média <textarea name="medium" maxlength="500">${escapeHtml(variantsObj.medium || "")}</textarea></label>
            <label>Resposta longa <textarea name="long" maxlength="1500">${escapeHtml(variantsObj.long || "")}</textarea></label>
            <div class="inline-form">
              <button type="submit" class="btn-primary">Salvar nova revisão</button>
              <button type="button" class="btn-secondary btn-cancel-arsenal-edit">Cancelar</button>
            </div>
          </form>
        </article>`;
      }).join("");
    } catch (error) {
      list.textContent = error.message;
    }
  }

  ["#arsenal-filter-status", "#arsenal-filter-language", "#arsenal-filter-category"].forEach((sel) => {
    $(sel)?.addEventListener("change", () => loadArsenal());
  });

  $("#arsenal-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const variants = Object.fromEntries(["short", "medium", "long"]
      .filter((key) => String(data.get(key) || "").trim()).map((key) => [key, String(data.get(key)).trim()]));
    if (!Object.keys(variants).length) {
      showToast("Informe ao menos uma variante de resposta.");
      return;
    }
    const whenKws = splitCommaList(data.get("when_to_use"));
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      await apiFetch("/api/arsenal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: data.get("question"),
          category: data.get("category") || "custom",
          language: data.get("language"),
          control: data.get("control"),
          role_archetype: data.get("role_archetype"),
          trigger_keywords: splitCommaList(data.get("triggers")),
          role_tags: splitCommaList(data.get("role_tags")),
          when_to_use: whenKws.length ? [{ all_keywords: whenKws }] : [],
          variants,
        }),
      });
      form.reset();
      await loadArsenal();
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#arsenal-list").addEventListener("click", async (event) => {
    const cancelBtn = event.target.closest(".btn-cancel-arsenal-edit");
    if (cancelBtn) {
      cancelBtn.closest(".arsenal-edit-form")?.classList.add("hidden");
      return;
    }
    const button = event.target.closest("button[data-arsenal-action]");
    if (!button) return;
    const { id, revision, arsenalAction } = button.dataset;
    if (arsenalAction === "edit") {
      const form = button.closest("article")?.querySelector(".arsenal-edit-form");
      if (form) form.classList.toggle("hidden");
      return;
    }
    button.disabled = true;
    try {
      await apiFetch(`/api/arsenal/${encodeURIComponent(id)}/${arsenalAction}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: Number(revision) }),
      });
      await loadArsenal();
    } catch (error) {
      showToast(error.message);
      if (error.status === 409) await loadArsenal();
    } finally {
      button.disabled = false;
    }
  });

  $("#arsenal-list").addEventListener("submit", async (event) => {
    const form = event.target.closest(".arsenal-edit-form");
    if (!form) return;
    event.preventDefault();
    const id = form.dataset.id;
    const expectedRevision = Number(form.dataset.revision);
    const data = new FormData(form);
    const variants = Object.fromEntries(["short", "medium", "long"]
      .filter((key) => String(data.get(key) || "").trim()).map((key) => [key, String(data.get(key)).trim()]));
    if (!Object.keys(variants).length) {
      showToast("Informe ao menos uma variante de resposta.");
      return;
    }
    const whenKws = splitCommaList(data.get("when_to_use"));
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    try {
      await apiFetch(`/api/arsenal/${encodeURIComponent(id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: expectedRevision,
          entry: {
            question: data.get("question"),
            category: data.get("category") || "custom",
            language: data.get("language"),
            control: "textarea",
            role_archetype: data.get("role_archetype"),
            trigger_keywords: splitCommaList(data.get("triggers")),
            role_tags: splitCommaList(data.get("role_tags")),
            when_to_use: whenKws.length ? [{ all_keywords: whenKws }] : [],
            variants,
          },
        }),
      });
      await loadArsenal();
    } catch (error) {
      showToast(error.message);
      if (error.status === 409) await loadArsenal();
    } finally {
      if (button) button.disabled = false;
    }
  });

  /* === CONFIGURAÇÕES === */
  async function loadSettings() {
    const data = await apiFetch("/api/settings");
    state.settings = data;
    const p = data.preferences || {};
    const llm = data.llm || {};

    const llmActive = llm.provider && !["offline", "none"].includes(String(llm.provider).toLowerCase());

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
    state.opencodeThreads = [];
    state.opencodeActiveThreadId = null;
    state.opencodeFocusJobId = null;
    state.opencodeFocusApplicationId = null;
    showToast("Dados locais apagados.");
    await Promise.all([loadJobs(), loadApplications(), loadProfile(), loadOpenCodeThreads()]);
  });

  /* === CHAT OPENCODE (EXTENSÃO BROWSE + MODELO LOCAL) === */
  function setChatVisible(visible) {
    const panel = $("#opencode-chat-panel");
    if (!panel) return;
    panel.classList.toggle("hidden", !visible);
  }

  function currentOpenCodeThread() {
    return (
      state.opencodeThreads.find((t) => t.thread_id === state.opencodeActiveThreadId) ||
      state.opencodeThreads[0] ||
      null
    );
  }

  function renderOpenCodeContextBar() {
    const bar = $("#opencode-context-bar");
    if (!bar) return;
    const thread = currentOpenCodeThread();
    const appId = state.opencodeFocusApplicationId || thread?.application_id || null;
    const jobId = state.opencodeFocusJobId || thread?.job_id || null;
    const app = appId ? state.applications.find((a) => a.id === appId) : null;
    const job = jobId ? state.jobs.find((j) => j.id === jobId) : null;
    const title = app?.job_title || job?.title || "";
    const company = app?.company || job?.company || "";

    if (!title && !appId && !jobId) {
      bar.classList.add("hidden");
      bar.innerHTML = "";
      return;
    }
    bar.classList.remove("hidden");
    const pdfBadge = app?.resume_id
      ? `<span class="badge badge-ok">PDF pronto</span>`
      : `<span class="badge badge-neutral">Sem PDF</span>`;
    bar.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;">
        <span><strong>Foco:</strong> ${escapeHtml(title || "Vaga selecionada")}${company ? ` — ${escapeHtml(company)}` : ""} ${pdfBadge}</span>
        <button type="button" id="btn-clear-opencode-focus" class="btn-secondary" style="padding:2px 6px;font-size:0.75rem;">Limpar</button>
      </div>
    `;
    $("#btn-clear-opencode-focus")?.addEventListener("click", () => {
      state.opencodeFocusJobId = null;
      state.opencodeFocusApplicationId = null;
      renderOpenCodeContextBar();
    });
  }

  function renderOpenCodeChat(liveRun = null) {
    const selectEl = $("#opencode-thread-select");
    if (selectEl) {
      if (!state.opencodeThreads.length) {
        selectEl.innerHTML = `<option value="">Nova conversa OpenCode</option>`;
      } else {
        selectEl.innerHTML = state.opencodeThreads
          .map(
            (t) =>
              `<option value="${escapeAttr(t.thread_id)}"${t.thread_id === state.opencodeActiveThreadId ? " selected" : ""}>${escapeHtml(t.title || "Chat OpenCode")}</option>`
          )
          .join("");
      }
    }

    renderOpenCodeContextBar();

    const box = $("#opencode-chat-messages");
    if (!box) return;
    const thread = currentOpenCodeThread();
    const msgs = Array.isArray(thread?.messages) ? thread.messages : [];
    const chunks = [];
    if (!msgs.length && !liveRun) {
      chunks.push(`
        <div class="oc-msg oc-msg-assistant muted">
          Chat conectado ao <strong>OpenCode</strong> com seu modelo local (<code>${escapeHtml(state.opencodeStatus?.model || "llamacpp/qwen-local")}</code>) e extensão <code>browse</code>. Seu Perfil Mestre, textos do Arsenal, skills e PDFs ficam disponíveis automaticamente aqui.
        </div>
      `);
    } else {
      for (const m of msgs) {
        const isUser = m.role === "user";
        const tools = Array.isArray(m.tools) ? m.tools : [];
        const toolsHtml = tools.length
          ? `<div class="oc-tools-row">${tools
              .map(
                (tl) =>
                  `<span class="badge badge-neutral">${escapeHtml(tl.tool || "browse")}${tl.status ? `: ${escapeHtml(tl.status)}` : ""}</span>`
              )
              .join("")}</div>`
          : "";
        chunks.push(`
          <div class="oc-msg ${isUser ? "oc-msg-user" : "oc-msg-assistant"}">
            ${toolsHtml}
            <div>${escapeHtml(m.content || "")}</div>
          </div>
        `);
      }
    }

    if (liveRun && liveRun.status === "running") {
      const liveTools = Array.isArray(liveRun.tool_events) ? liveRun.tool_events : [];
      const liveToolsHtml = liveTools.length
        ? `<div class="oc-tools-row">${liveTools
            .map(
              (tl) =>
                `<span class="badge badge-info">${escapeHtml(tl.tool || "browse")} (${escapeHtml(tl.status || "running")})</span>`
            )
            .join("")}</div>`
        : "";
      chunks.push(`
        <div class="oc-msg oc-msg-assistant">
          ${liveToolsHtml}
          <div>${escapeHtml(liveRun.partial_text || "Pensando / acionando OpenCode e extensão browse…")}</div>
        </div>
      `);
    }

    box.innerHTML = chunks.join("");
    box.scrollTop = box.scrollHeight;
  }

  async function loadOpenCodeThreads() {
    if (!state.csrfToken) return;
    try {
      const data = await apiFetch("/api/opencode/threads");
      state.opencodeThreads = data.threads || [];
      if (!state.opencodeActiveThreadId || !state.opencodeThreads.some((t) => t.thread_id === state.opencodeActiveThreadId)) {
        state.opencodeActiveThreadId = data.active_thread_id || (state.opencodeThreads[0]?.thread_id ?? null);
      }
      renderOpenCodeChat();
    } catch (_) {
      // ignore initial thread fetch errors before login
    }
  }

  function setChatRunningUI(running, statusText = "") {
    const sendBtn = $("#btn-send-opencode-chat");
    const cancelBtn = $("#btn-cancel-opencode-run");
    const statusEl = $("#opencode-run-status");
    if (sendBtn) sendBtn.disabled = running;
    if (cancelBtn) cancelBtn.classList.toggle("hidden", !running);
    if (statusEl) statusEl.textContent = statusText;
  }

  async function pollOpenCodeRun(runId) {
    if (!state.csrfToken || state.opencodeActiveRunId !== runId) return;
    try {
      const run = await apiFetch(`/api/opencode/runs/${encodeURIComponent(runId)}`);
      if (run.thread) {
        const idx = state.opencodeThreads.findIndex((t) => t.thread_id === run.thread.thread_id);
        if (idx >= 0) state.opencodeThreads[idx] = run.thread;
        else state.opencodeThreads.unshift(run.thread);
        state.opencodeActiveThreadId = run.thread.thread_id;
      }

      if (run.status === "running") {
        setChatRunningUI(true, "OpenCode executando (extensão browse + modelo local)…");
        renderOpenCodeChat(run);
        state.opencodeRunPollTimer = setTimeout(() => pollOpenCodeRun(runId), 900);
        return;
      }

      state.opencodeActiveRunId = null;
      state.opencodeRunPollTimer = null;
      setChatRunningUI(false, run.status === "failed" ? "Erro na execução." : "");
      renderOpenCodeChat();
    } catch (err) {
      state.opencodeActiveRunId = null;
      state.opencodeRunPollTimer = null;
      setChatRunningUI(false, err.message || "Falha ao acompanhar execução.");
      renderOpenCodeChat();
    }
  }

  async function sendOpenCodeMessage(message) {
    const clean = String(message || "").trim();
    if (!clean) return;
    if (state.opencodeActiveRunId) {
      showToast("Aguarde a resposta atual do OpenCode ou clique em Parar.");
      return;
    }
    setChatVisible(true);
    setChatRunningUI(true, "Iniciando OpenCode…");
    try {
      const res = await apiFetch("/api/opencode/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: clean,
          thread_id: state.opencodeActiveThreadId,
          job_id: state.opencodeFocusJobId,
          application_id: state.opencodeFocusApplicationId,
        }),
      });
      state.opencodeActiveThreadId = res.thread_id;
      state.opencodeActiveRunId = res.run_id;
      await loadOpenCodeThreads();
      pollOpenCodeRun(res.run_id);
    } catch (err) {
      setChatRunningUI(false, err.message);
      showToast(err.message);
    }
  }

  async function focusOpenCodeOnApplication(jobId, applicationId, autoSend = false) {
    state.opencodeFocusJobId = jobId || null;
    state.opencodeFocusApplicationId = applicationId || null;
    setChatVisible(true);
    try {
      const res = await apiFetch("/api/opencode/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: jobId || null,
          application_id: applicationId || null,
          force_new: false,
        }),
      });
      if (res.thread) {
        const idx = state.opencodeThreads.findIndex((t) => t.thread_id === res.thread.thread_id);
        if (idx >= 0) state.opencodeThreads[idx] = res.thread;
        else state.opencodeThreads.unshift(res.thread);
        state.opencodeActiveThreadId = res.thread.thread_id;
      }
      renderOpenCodeChat();
      if (autoSend) {
        await sendOpenCodeMessage(
          "Use a extensão browse para abrir esta vaga no meu Chrome (ou verificar se já está aberta), preencha o formulário usando exclusivamente meu Perfil Mestre, textos do Arsenal, skills e o currículo PDF gerado em disco, e pare antes do envio final para eu revisar."
        );
      } else {
        $("#opencode-chat-input")?.focus();
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  $("#btn-toggle-opencode-chat")?.addEventListener("click", () => {
    const panel = $("#opencode-chat-panel");
    if (panel) setChatVisible(panel.classList.contains("hidden"));
  });

  $("#btn-close-opencode-chat")?.addEventListener("click", () => setChatVisible(false));

  $("#opencode-thread-select")?.addEventListener("change", (ev) => {
    state.opencodeActiveThreadId = ev.currentTarget.value || null;
    const thread = currentOpenCodeThread();
    if (thread) {
      state.opencodeFocusJobId = thread.job_id || null;
      state.opencodeFocusApplicationId = thread.application_id || null;
    }
    renderOpenCodeChat();
  });

  $("#btn-new-opencode-thread")?.addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/opencode/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: state.opencodeFocusJobId,
          application_id: state.opencodeFocusApplicationId,
          force_new: true,
        }),
      });
      if (res.thread) {
        state.opencodeThreads.unshift(res.thread);
        state.opencodeActiveThreadId = res.thread.thread_id;
      }
      renderOpenCodeChat();
      $("#opencode-chat-input")?.focus();
    } catch (err) {
      showToast(err.message);
    }
  });

  $$(".oc-quick-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      const prompt = btn.dataset.prompt || "";
      if (prompt) sendOpenCodeMessage(prompt);
    })
  );

  $("#btn-cancel-opencode-run")?.addEventListener("click", async () => {
    if (!state.opencodeActiveRunId) return;
    const runId = state.opencodeActiveRunId;
    clearTimeout(state.opencodeRunPollTimer);
    state.opencodeRunPollTimer = null;
    state.opencodeActiveRunId = null;
    try {
      await apiFetch(`/api/opencode/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
    } catch (_) {}
    setChatRunningUI(false, "Execução interrompida.");
    await loadOpenCodeThreads();
  });

  $("#opencode-chat-input")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      $("#opencode-chat-form")?.requestSubmit();
    }
  });

  $("#opencode-chat-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("#opencode-chat-input");
    if (!input) return;
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    await sendOpenCodeMessage(msg);
  });

  checkSession();
})();
