(() => {
  const MASCOTS = {
    fox: {
      name: "Raposa",
      svg: `<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="36" height="36">
        <path d="M8 24V10L14 15H22L28 10V24C28 27.5 24 29.5 18 29.5C12 29.5 8 27.5 8 24Z" />
        <path d="M13 20.5C13.6 19.8 14.6 19.8 15.2 20.5" />
        <path d="M20.8 20.5C21.4 19.8 22.4 19.8 23 20.5" />
        <path d="M16.8 23.2H19.2L18 24.5L16.8 23.2Z" fill="currentColor" />
        <path d="M5 21.5L9.5 22.5M5.5 24.5L9.5 24.2" />
        <path d="M31 21.5L26.5 22.5M30.5 24.5L26.5 24.2" />
      </svg>`,
    },
    owl: {
      name: "Coruja",
      svg: `<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="36" height="36">
        <path d="M8 12L12 8L16 11H20L24 8L28 12V24C28 27.5 23.5 30 18 30C12.5 30 8 27.5 8 24V12Z" />
        <circle cx="13.5" cy="18" r="2.5" />
        <circle cx="22.5" cy="18" r="2.5" />
        <path d="M18 20.5L16.5 23H19.5L18 20.5Z" />
      </svg>`,
    },
    wolf: {
      name: "Lobo",
      svg: `<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="36" height="36">
        <path d="M7 10L10 23L18 29L26 23L29 10L22 15H14L7 10Z" />
        <path d="M13 19.5H15" />
        <path d="M21 19.5H23" />
        <path d="M16.5 24H19.5L18 25.5L16.5 24Z" />
      </svg>`,
    },
    stag: {
      name: "Cervo",
      svg: `<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="36" height="36">
        <path d="M12 15L9 8M12 12L7 11M24 15L27 8M24 12L29 11" />
        <path d="M11 15H25L23 26L18 30L13 26L11 15Z" />
        <circle cx="15" cy="20" r="1" fill="currentColor" />
        <circle cx="21" cy="20" r="1" fill="currentColor" />
      </svg>`,
    },
  };

  const TAB_HEADERS = {
    vagas: {
      title: "Radar de Vagas",
      subtitle: "Seu assistente local para encontrar vagas, preparar candidaturas e preenchê-las nos portais.",
    },
    candidatura: {
      title: "Cockpit de Candidaturas",
      subtitle: "Acompanhe o preenchimento assistido, revise o PDF gerado e confirme o envio na aba do Chrome.",
    },
    perfil: {
      title: "Perfil Mestre",
      subtitle: "Fonte única local de fatos para gerar currículos verídicos e preencher formulários.",
    },
    preflight: {
      title: "Portais e Sessões",
      subtitle: "Verifique sessões autenticadas no Chrome local via extensão browse do OpenCode.",
    },
    configuracoes: {
      title: "Configurações Locais",
      subtitle: "Motor LLM local, conectores de descoberta e privacidade dos seus dados.",
    },
  };

  const SPECIES_ORDER = ["fox", "owl", "wolf", "stag"];
  const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

  let currentSpecies = "fox";
  try {
    const saved = localStorage.getItem("careerpilot_mascot");
    if (saved && MASCOTS[saved]) currentSpecies = saved;
  } catch (_) {}

  const svgSlot = document.getElementById("mascot-svg-slot");
  const speciesLabel = document.getElementById("mascot-species-label");
  const cycleBtn = document.getElementById("btn-cycle-mascot");

  function renderMascot(animate = false) {
    const item = MASCOTS[currentSpecies] || MASCOTS.fox;
    if (svgSlot) {
      svgSlot.innerHTML = item.svg;
      if (animate) {
        svgSlot.classList.remove("mascot-pop");
        void svgSlot.offsetWidth;
        svgSlot.classList.add("mascot-pop");
      }
    }
    if (speciesLabel) {
      speciesLabel.textContent = item.name;
    }
  }

  if (cycleBtn) {
    cycleBtn.addEventListener("click", () => {
      const idx = SPECIES_ORDER.indexOf(currentSpecies);
      currentSpecies = SPECIES_ORDER[(idx + 1) % SPECIES_ORDER.length];
      try {
        localStorage.setItem("careerpilot_mascot", currentSpecies);
      } catch (_) {}
      renderMascot(true);
    });
  }

  renderMascot(false);

  // Sidebar quick actions
  const sidebarSyncBtn = document.getElementById("btn-sidebar-sync");
  if (sidebarSyncBtn) {
    sidebarSyncBtn.addEventListener("click", () => {
      const icon = sidebarSyncBtn.querySelector(".ui-icon");
      if (icon) {
        icon.classList.add("is-spinning");
        setTimeout(() => icon.classList.remove("is-spinning"), 750);
      }
      document.getElementById("btn-refresh-browser-status")?.click();
    });
  }

  const sidebarProfileBtn = document.getElementById("btn-sidebar-profile");
  if (sidebarProfileBtn) {
    sidebarProfileBtn.addEventListener("click", () => {
      document.querySelector('.tab-btn[data-tab="perfil"]')?.click();
    });
  }

  // Sync profile name into sidebar footer card
  const profNameInput = document.getElementById("prof-name");
  const sidebarProfName = document.getElementById("sidebar-profile-name");
  const sidebarAvatar = document.querySelector(".profile-avatar-mono");

  function syncSidebarProfileName() {
    const val = (profNameInput?.value || "").trim();
    if (val && sidebarProfName) {
      const parts = val.split(/\s+/);
      sidebarProfName.textContent = parts.slice(0, 2).join(" ");
      if (sidebarAvatar) {
        const initials = parts.slice(0, 2).map((p) => p[0]?.toUpperCase() || "").join("");
        if (initials) sidebarAvatar.textContent = initials;
      }
    }
  }

  if (profNameInput) {
    profNameInput.addEventListener("input", syncSidebarProfileName);
    setInterval(syncSidebarProfileName, 1800);
  }

  // Sync sidebar job queue from #jobs-grid
  const jobsGrid = document.getElementById("jobs-grid");
  const sidebarJobList = document.getElementById("sidebar-job-list");
  const sidebarSearchInput = document.getElementById("sidebar-job-search");
  const sidebarJobsCount = document.getElementById("sidebar-jobs-count");

  function syncSidebarJobs() {
    if (!jobsGrid || !sidebarJobList) return;
    const cards = [...jobsGrid.querySelectorAll("article.job-card")];
    const query = (sidebarSearchInput?.value || "").trim().toLowerCase();

    if (sidebarJobsCount) {
      sidebarJobsCount.textContent = cards.length > 0 ? String(cards.length) : "";
    }

    if (cards.length === 0) {
      sidebarJobList.innerHTML = `<div class="sidebar-empty-hint">Nenhuma vaga na fila</div>`;
      return;
    }

    const items = [];
    for (const card of cards) {
      const titleEl = card.querySelector("h3");
      const title = (titleEl?.textContent || "Vaga").trim();
      const mutedLines = [...card.querySelectorAll(".muted")].map((m) => m.textContent.trim()).filter(Boolean);
      const metaLine = mutedLines[0] || "";
      const matchBadge = [...card.querySelectorAll(".badge")].find((b) => /%/.test(b.textContent));
      const matchText = matchBadge ? matchBadge.textContent.trim() : "";
      const detailBtn = card.querySelector('[data-testid^="btn-detail-job-"]');
      const jobId = detailBtn ? detailBtn.getAttribute("data-testid").replace("btn-detail-job-", "") : "";

      if (query && !title.toLowerCase().includes(query) && !metaLine.toLowerCase().includes(query)) {
        continue;
      }

      const isWarn = !!card.querySelector(".badge-warn");
      const isErr = !!card.querySelector(".badge-danger");
      const dotClass = isErr ? "dot-err" : isWarn ? "dot-warn" : "dot-ok";

      items.push({ title, metaLine, matchText, jobId, dotClass });
    }

    if (items.length === 0) {
      sidebarJobList.innerHTML = `<div class="sidebar-empty-hint">Nenhum resultado para "${query.replace(/</g, "&lt;")}"</div>`;
      return;
    }

    sidebarJobList.innerHTML = items
      .slice(0, 12)
      .map(
        (it) => `
        <button type="button" class="sidebar-job-item" data-job-id="${it.jobId}">
          <div class="sidebar-job-top">
            <span class="sidebar-job-title-wrap">
              <span class="semantic-dot ${it.dotClass}"></span>
              <span class="sidebar-job-title">${it.title.replace(/</g, "&lt;")}</span>
            </span>
            ${it.matchText ? `<span class="sidebar-job-match">${it.matchText.replace(/</g, "&lt;")}</span>` : ""}
          </div>
          ${it.metaLine ? `<div class="sidebar-job-meta">${it.metaLine.replace(/</g, "&lt;")}</div>` : ""}
        </button>`
      )
      .join("");

    sidebarJobList.querySelectorAll(".sidebar-job-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const jid = btn.getAttribute("data-job-id");
        document.querySelector('.tab-btn[data-tab="vagas"]')?.click();
        if (jid) {
          const targetBtn = document.querySelector(`[data-testid="btn-detail-job-${jid}"]`);
          if (targetBtn) {
            targetBtn.click();
            document.getElementById("job-detail-container")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
        }
      });
    });
  }

  if (sidebarSearchInput) {
    sidebarSearchInput.addEventListener("input", syncSidebarJobs);
  }

  // Live status & pipeline sync
  const statusPill = document.getElementById("companion-status-pill");
  const statusText = document.getElementById("companion-status-text");
  const stageHint = document.getElementById("pipeline-stage-hint");
  const stepEls = [...document.querySelectorAll("#pipeline-steps .pipeline-step")];
  const connectorEls = [...document.querySelectorAll("#pipeline-steps .pipeline-connector")];
  const pageTitle = document.getElementById("workspace-page-title");
  const pageSubtitle = document.getElementById("workspace-page-subtitle");

  let spinnerTimer = null;
  let spinnerIdx = 0;

  function stopSpinner() {
    if (spinnerTimer) {
      clearInterval(spinnerTimer);
      spinnerTimer = null;
    }
  }

  function startSpinner(baseText) {
    if (spinnerTimer) return;
    spinnerTimer = setInterval(() => {
      spinnerIdx = (spinnerIdx + 1) % SPINNER_FRAMES.length;
      if (statusText) {
        statusText.textContent = `${SPINNER_FRAMES[spinnerIdx]} ${baseText}`;
      }
    }, 90);
  }

  function setPipelineStep(activeIdx, hint) {
    stepEls.forEach((el, idx) => {
      const node = el.querySelector(".step-node");
      el.classList.remove("is-done", "is-active");
      if (idx < activeIdx) {
        el.classList.add("is-done");
        if (node) node.textContent = "✓";
      } else if (idx === activeIdx) {
        el.classList.add("is-active");
        if (node) node.textContent = String(idx + 1);
      } else {
        if (node) node.textContent = String(idx + 1);
      }
    });
    connectorEls.forEach((conn, idx) => {
      conn.classList.toggle("is-done", idx < activeIdx);
    });
    if (stageHint && hint) {
      stageHint.textContent = hint;
    }
  }

  function syncCompanionState() {
    const ocStatus = (document.getElementById("opencode-run-status")?.textContent || "").trim();
    const searchStatus = (document.getElementById("live-search-status")?.textContent || "").trim();
    const browserBadge = document.getElementById("browser-status-badge");
    const pendingBadge = document.getElementById("pending-count");
    const activeTabBtn = document.querySelector(".tab-btn.active");
    const activeTab = activeTabBtn ? activeTabBtn.dataset.tab : "vagas";

    const headerInfo = TAB_HEADERS[activeTab] || TAB_HEADERS.vagas;
    if (pageTitle) pageTitle.textContent = headerInfo.title;
    if (pageSubtitle) pageSubtitle.textContent = headerInfo.subtitle;

    const isWorking =
      ocStatus.length > 0 && /execut|enviand|process|rodand|aguard/i.test(ocStatus);
    const isSearching =
      searchStatus.length > 0 && /buscand|varrend|sincron/i.test(searchStatus);

    if (statusPill && statusText) {
      statusPill.classList.remove(
        "companion-pill-ok",
        "companion-pill-working",
        "companion-pill-warn",
        "companion-pill-err"
      );

      if (isWorking || isSearching) {
        statusPill.classList.add("companion-pill-working");
        const label = isSearching ? "Varrendo vagas locais..." : "Executando OpenCode...";
        startSpinner(label);
      } else {
        stopSpinner();
        const pendingCount = parseInt((pendingBadge?.textContent || "0").trim(), 10) || 0;
        const browserText = (browserBadge?.textContent || "").toLowerCase();

        if (pendingCount > 0) {
          statusPill.classList.add("companion-pill-err");
          statusText.textContent = `${pendingCount} pendência(s) aguardando revisão`;
        } else if (browserText.includes("desconect") || browserText.includes("erro") || browserText.includes("offline")) {
          statusPill.classList.add("companion-pill-warn");
          statusText.textContent = "Extensão browse desconectada";
        } else {
          statusPill.classList.add("companion-pill-ok");
          statusText.textContent = "Pronto para operar";
        }
      }
    }

    const hasHumanAction = !!document.querySelector("#candidatura-active .human-action-box");
    const hasActiveApp = !!document.querySelector("#candidatura-active .timeline-card, #candidatura-active .fact-row");

    if (hasHumanAction) {
      setPipelineStep(3, "Pausado antes do envio — revise no Chrome");
    } else if (activeTab === "candidatura" && hasActiveApp) {
      setPipelineStep(2, "Candidatura ativa em preparação / auto-fill");
    } else if (activeTab === "perfil") {
      setPipelineStep(1, "Calibrando Perfil Mestre & Arsenal de respostas");
    } else if (activeTab === "preflight") {
      setPipelineStep(2, "Verificando sessões autenticadas nos portais");
    } else {
      setPipelineStep(0, "Descubra vagas e prepare o currículo PDF");
    }
  }

  const observer = new MutationObserver(() => {
    syncCompanionState();
    syncSidebarJobs();
  });

  [
    "opencode-run-status",
    "live-search-status",
    "browser-status-badge",
    "pending-count",
    "candidatura-active",
    "jobs-grid",
  ].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      observer.observe(el, { childList: true, subtree: true, characterData: true });
    }
  });

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      setTimeout(syncCompanionState, 20);
    });
  });

  syncCompanionState();
  syncSidebarJobs();
})();
