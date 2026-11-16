"""Bounded step inspection, deterministic rule/validator resolution, and single-step Playwright fill."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Frame, Locator, Page
from sqlalchemy.orm import Session

from backend.app.browser_automation.adapters import (
    detect_portal,
    get_adapter,
    matches_profile_surface_warning,
)
from backend.app.browser_automation.session import BrowserSession, _sanitize_tab_summary_url
from backend.app.core.config import Settings
from backend.app.core.llm import LlmGateway
from backend.app.core.ssrf import extract_origin
from backend.app.schemas.profile import LengthVariants, Locale, MasterProfileDocument
from backend.app.services.applications.rules import (
    FieldRule,
    build_verified_slot_values,
    extract_canonical_literals,
    match_field_rule,
)
from backend.app.services.applications.validator import (
    ControlConstraints,
    extract_control_constraints,
    fit_arsenal_variants,
    validate_candidate_value,
    validate_text_limits,
)

ControlKind = Literal[
    "text",
    "textarea",
    "email",
    "tel",
    "url",
    "number",
    "date",
    "select",
    "radio",
    "checkbox",
    "file",
    "custom_combobox",
    "unsupported",
]


class StepChangedError(RuntimeError):
    """Raised when the browser DOM or navigation generation changes mid-fill."""

    def __init__(self, *, written_count: int, message: str) -> None:
        super().__init__(message)
        self.written_count = written_count


@dataclass(frozen=True, slots=True)
class FieldOption:
    option_id: str
    label: str
    value: str = ""
    selected: bool = False
    available: bool = True


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    field_id: str
    question_signature: str
    label: str
    control_kind: ControlKind
    required: bool
    editable: bool
    has_value: bool
    has_file: bool
    checked: bool | None
    accept: str
    options: tuple[FieldOption, ...]
    category: str
    locator_hint: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StepBlocker:
    code: str
    message: str
    can_answer: bool = False
    field_id: str | None = None
    label: str | None = None
    question_signature: str | None = None
    field_type: str | None = None
    options: tuple[dict[str, str], ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "can_answer": self.can_answer,
        }
        if self.field_id:
            payload["field_id"] = self.field_id
        if self.label:
            payload["label"] = self.label
        if self.question_signature:
            payload["question_signature"] = self.question_signature
        if self.field_type:
            payload["field_type"] = self.field_type
        if self.options:
            payload["options"] = list(self.options)
        return payload


@dataclass(frozen=True, slots=True)
class StepSnapshot:
    step_id: str
    navigation_gen: int
    portal: str
    page_url: str
    page_origin: str
    heading: str
    vacancy_context_title: str
    vacancy_mismatch: bool
    profile_surface_warning: str | None
    container_selector: str | None
    frame_url: str | None
    multiple_containers: bool
    cross_origin_iframes: tuple[str, ...]
    open_form_required: bool
    login_required: bool
    has_captcha: bool
    has_otp: bool
    has_video: bool
    has_external_assessment: bool
    final_submit_detected: bool
    has_resume_upload: bool
    structural_fingerprint: str
    container_index: int = 0
    fields: tuple[FieldDescriptor, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PlannedMutation:
    field: FieldDescriptor
    kind: Literal["profile_value", "choose_option", "draft", "resume_upload"]
    value: str
    option_id: str | None = None
    suggested: bool = False


@dataclass(frozen=True, slots=True)
class PlannedStepResolution:
    mutations: tuple[PlannedMutation, ...]
    skipped: tuple[dict[str, str], ...]
    blockers: tuple[StepBlocker, ...]
    notices: tuple[str, ...] = ()
    llm_fallback_note: str | None = None
    llm_usage_events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class StepRunResult:
    snapshot: StepSnapshot
    filled: list[dict[str, Any]]
    skipped: list[dict[str, str]]
    uploaded_files: list[str]
    blockers: list[StepBlocker]
    notices: list[str]
    llm_usage_events: tuple[dict[str, Any], ...] = ()
    write_outcomes: dict[str, str] = field(default_factory=dict)


def normalize_label_text(raw: str) -> str:
    """Fold accents and punctuation into lowercase single-space tokens for signatures."""
    folded = unicodedata.normalize("NFKD", raw or "")
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()
    return cleaned


def build_question_signature(
    label: str, control_kind: str, option_labels: list[str] | tuple[str, ...] = ()
) -> str:
    """Compute a portal-ID-free semantic question signature from label, kind, and options."""
    norm_label = normalize_label_text(label)[:140] or "unnamed"
    norm_kind = (control_kind or "text").lower()
    norm_opts = sorted({normalize_label_text(opt)[:60] for opt in option_labels if normalize_label_text(opt)})
    opt_hash = ""
    if norm_opts:
        digest = hashlib.sha256("|".join(norm_opts).encode("utf-8")).hexdigest()[:8]
        opt_hash = f"|{digest}"
    return f"{norm_kind}:{norm_label}{opt_hash}"[:230]


_SENSITIVE_ID_RE = re.compile(
    r"\b(cpf|rg|cnh|passaporte|passport|ssn|pis|pasep|titulo de eleitor|codigo de verificacao|2fa|otp)\b",
    re.I,
)
_LEGAL_CONSENT_RE = re.compile(
    r"(concordo|aceito|declaro|li e aceito|politica de privacidade|privacy policy|"
    r"termos de uso|terms of use|terms and conditions|\blgpd\b|\bgdpr\b|"
    r"marketing|receber comunicacoes|novidades|autorizo o tratamento|consentimento|"
    r"\battest\b|\bcertify\b|\backnowledg|confirm that all information|declaration)",
    re.I,
)
_NON_RESUME_FILE_RE = re.compile(
    r"(carta de apresentacao|cover letter|portfolio|foto|photo|avatar|video|"
    r"diploma|certificado|historico escolar|identidade|documento)",
    re.I,
)
_RESUME_FILE_RE = re.compile(
    r"(curriculo|curriculum|\bcv\b|resume|anexar|anexo|arquivo|upload|assets attributes|\bfile\b)",
    re.I,
)

_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contact_email", re.compile(r"\b(e ?mail|correo)\b", re.I)),
    ("contact_phone", re.compile(r"\b(telefone|celular|whatsapp|phone|mobile|fone)\b", re.I)),
    ("contact_linkedin", re.compile(r"\blinkedin\b", re.I)),
    ("contact_github", re.compile(r"\bgithub\b", re.I)),
    ("contact_portfolio", re.compile(r"\b(portfolio|site pessoal|website|personal site|homepage)\b", re.I)),
    ("contact_first_name", re.compile(r"\b(primeiro nome|first name|given name)\b", re.I)),
    ("contact_last_name", re.compile(r"\b(sobrenome|ultimo nome|last name|surname|family name)\b", re.I)),
    ("contact_name", re.compile(r"\b(nome completo|full name|seu nome)\b|^nome$|^name$", re.I)),
    ("contact_city", re.compile(r"\b(cidade|city)\b", re.I)),
    ("contact_state", re.compile(r"\b(estado|uf|state|province)\b", re.I)),
    ("contact_country", re.compile(r"\b(pais|country)\b", re.I)),
    ("contact_postal_code", re.compile(r"\b(cep|zip ?code|postal ?code|codigo postal)\b", re.I)),
    ("contact_location", re.compile(r"\b(localizacao|endereco|location|onde mora|residencia)\b", re.I)),
    ("availability", re.compile(r"\b(disponibilidade|aviso previo|notice period|availability|start date)\b", re.I)),
    ("work_auth", re.compile(r"\b(autorizacao de trabalho|work authorization|visa sponsorship|autorizado a trabalhar|visto)\b", re.I)),
)


def classify_field_category(label: str, control_kind: ControlKind, accept: str = "") -> str:
    """Classify a control purely from normalized label, control kind, and accept rules."""
    folded = normalize_label_text(label)
    if control_kind == "file":
        if _NON_RESUME_FILE_RE.search(folded):
            return "non_resume_file"
        acc = (accept or "").lower()
        if (
            acc
            and not any(tok in acc for tok in (".pdf", "application/pdf", "*/*"))
            and ("image" in acc or "video" in acc or "audio" in acc)
        ):
            return "non_resume_file"
        if _RESUME_FILE_RE.search(folded) or not folded or ".pdf" in acc or "application/pdf" in acc:
            return "resume_file"
        return "non_resume_file"

    if re.search(
        r"\b(salarial|salario|remuneracao|compensacao|salary|compensation|expected pay|pay expectation|"
        r"hourly rate|taxa horaria|valor mensal esperado|expectativa salarial|"
        r"pretensao.*valor|quanto (?:voce )?(?:espera|pretende) (?:receber|ganhar)|"
        r"qual valor (?:mensal|anual|hora).*espera)\b",
        folded,
    ):
        return "salary"

    if _SENSITIVE_ID_RE.search(folded):
        return "sensitive_id"
    if control_kind in {"checkbox", "radio"} and _LEGAL_CONSENT_RE.search(folded):
        return "legal_consent"
    if control_kind == "email":
        return "contact_email"
    if control_kind == "tel":
        return "contact_phone"

    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(folded):
            return category
    return "custom_question"


_DOM_INSPECT_SCRIPT = r"""
(args) => {
  const containerSelectors = args.containerSelectors || [];
  const applyPatterns = (args.applyPatterns || []).map((p) => new RegExp(p, 'i'));

  const isVisible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };

  const normText = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const isLoginOrSearchForm = (form) => {
    if (!form) return true;
    const tag = (form.tagName || '').toLowerCase();
    if (tag === 'form') {
      if (form.closest('header, nav, [role="navigation"], [role="banner"]')) return true;
      const fRole = (form.getAttribute('role') || '').toLowerCase();
      const fAction = (form.getAttribute('action') || '').toLowerCase();
      const fIdCls = ((form.id || '') + ' ' + (form.className || '')).toLowerCase();
      if (fRole === 'search' || /\b(search|busca|pesquisa|newsletter)\b/.test(fIdCls) || /\/(search|busca|pesquisa)\b/.test(fAction)) {
        return true;
      }
    }
    const txt = normText(form.innerText).toLowerCase();
    const hasPassword = !!form.querySelector('input[type="password"]');
    const controls = Array.from(form.querySelectorAll('input, textarea, select')).filter((el) => {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'reset';
    });
    if (hasPassword && controls.length <= 3) return true;
    if (controls.length === 0) return true;
    if (controls.length === 1) {
      const only = controls[0];
      const t = (only.getAttribute('type') || 'text').toLowerCase();
      const role = (only.getAttribute('role') || '').toLowerCase();
      const name = ((only.getAttribute('name') || '') + ' ' + (only.getAttribute('aria-label') || '') + ' ' + (only.getAttribute('placeholder') || '')).toLowerCase();
      if (t === 'search' || role === 'searchbox' || /\b(buscar|pesquisar|search|newsletter)\b/.test(name)) {
        return true;
      }
      if (/entrar|sign in|login/.test(txt) && hasPassword) return true;
    }
    return false;
  };

  const countInteractiveControls = (root) => {
    if (!root) return 0;
    return Array.from(
      root.querySelectorAll('input, textarea, select, [role="combobox"], [role="radiogroup"]')
    ).filter((el) => {
      if (!isVisible(el)) return false;
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'reset';
    }).length;
  };

  const resolveContainer = () => {
    const matched = [];
    const seen = new Set();
    for (const sel of containerSelectors) {
      let nodes = [];
      try {
        nodes = Array.from(document.querySelectorAll(sel));
      } catch (e) {
        continue;
      }
      for (const node of nodes) {
        if (!isVisible(node)) continue;
        // If another outer/inner already included same form, avoid self-nesting duplicates
        let covered = false;
        for (const prev of seen) {
          if (prev === node || prev.contains(node) || node.contains(prev)) {
            covered = true;
            break;
          }
        }
        if (covered) continue;
        if (isLoginOrSearchForm(node)) continue;
        if (countInteractiveControls(node) === 0) continue;
        seen.add(node);
        matched.push({ el: node, selector: sel, index: nodes.indexOf(node) });
      }
      if (matched.length === 1) {
        return { container: matched[0].el, selector: matched[0].selector, index: matched[0].index, multiple: false };
      }
      if (matched.length > 1) {
        return { container: null, selector: null, index: 0, multiple: true };
      }
    }
    return { container: null, selector: null, index: 0, multiple: false };
  };

  const bodyText = normText(document.body ? document.body.innerText : '').slice(0, 6000);
  const passwordInputs = Array.from(document.querySelectorAll('input[type="password"]')).filter(isVisible);
  const otpInputs = Array.from(
    document.querySelectorAll('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i]')
  ).filter(isVisible);

  const isCssVisible = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    let cur = el;
    while (cur && cur.nodeType === 1) {
      const st = window.getComputedStyle(cur);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
      cur = cur.parentElement;
    }
    return true;
  };

  const hasCaptcha = !!(
    Array.from(document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]')).some((el) => {
      if ((el.getAttribute('data-size') || '').toLowerCase() === 'invisible') return false;
      if (el.closest('.grecaptcha-badge')) return false;
      if (!isCssVisible(el)) return false;
      if (isVisible(el)) return true;
      return el.matches('.g-recaptcha, .h-captcha, .cf-turnstile') && !!el.closest('form');
    }) ||
    Array.from(document.querySelectorAll('iframe')).some((f) => {
      if (!isVisible(f) || f.closest('.grecaptcha-badge')) return false;
      const src = (f.getAttribute('src') || '').toLowerCase();
      if (src.includes('size=invisible')) return false;
      return src.includes('recaptcha') || src.includes('hcaptcha') || src.includes('turnstile') || src.includes('arkose');
    })
  );

  const loginRequired =
    passwordInputs.length > 0 ||
    (/\b(fa[çc]a login|entrar na conta|sign in to continue|log in to apply|entrar ou cadastrar)\b/i.test(bodyText) &&
      countInteractiveControls(document.body) <= 3);

  const hasOtp =
    otpInputs.length > 0 ||
    /\b(c[óo]digo de verifica[çc][ãa]o|verifica[çc][ãa]o em duas etapas|two[- ]factor|2fa|token enviado)\b/i.test(bodyText);

  const hasExternalAssessment = /\b(hackerrank|codility|testgorillas|karat|pymetrics|teste t[ée]cnico externo|avalia[çc][ãa]o externa)\b/i.test(bodyText);

  const crossOriginIframes = [];
  const pageOrigin = window.location.origin;
  for (const ifr of Array.from(document.querySelectorAll('iframe'))) {
    if (!isVisible(ifr)) continue;
    const rawSrc = ifr.getAttribute('src') || '';
    if (!rawSrc || rawSrc.startsWith('about:') || rawSrc.startsWith('javascript:')) continue;
    try {
      const u = new URL(rawSrc, window.location.href);
      if (u.origin && u.origin !== pageOrigin) {
        const lower = rawSrc.toLowerCase();
        if (lower.includes('recaptcha') || lower.includes('hcaptcha') || lower.includes('turnstile')) continue;
        const rect = ifr.getBoundingClientRect();
        if (rect.width >= 120 && rect.height >= 80) {
          crossOriginIframes.push(u.origin);
        }
      }
    } catch (e) {}
  }

  const res = resolveContainer();
  let openFormRequired = false;
  if (!res.container && !res.multiple) {
    const ctas = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
    openFormRequired = ctas.some((b) => {
      const t = normText(b.innerText || b.getAttribute('aria-label') || '');
      return t && applyPatterns.some((re) => re.test(t));
    });
  }

  const hNode = res.container
    ? (res.container.querySelector('h1, h2, h3, legend, [role="heading"]') || document.querySelector('h1, h2'))
    : document.querySelector('h1, h2');
  const heading = normText(hNode ? hNode.innerText : (document.title || '')).slice(0, 160);
  const titleNode = document.querySelector('h1, [data-testid*="job-title"], .job-details-jobs-unified-top-card__job-title');
  const vacancyContextTitle = normText(titleNode ? titleNode.innerText : '').slice(0, 160);

  if (!res.container) {
    return {
      heading,
      vacancyContextTitle,
      containerSelector: null,
      containerIndex: 0,
      multipleContainers: res.multiple,
      crossOriginIframes,
      openFormRequired,
      loginRequired,
      hasCaptcha,
      hasOtp,
      hasVideo: false,
      hasExternalAssessment,
      finalSubmitDetected: false,
      fields: [],
    };
  }

  const container = res.container;
  const containerText = normText(container.innerText).slice(0, 4000);
  const hasVideo =
    /\b(grava[çc][ãa]o de v[íi]deo|v[íi]deo de apresenta[çc][ãa]o|video interview|record a video)\b/i.test(containerText) ||
    Array.from(container.querySelectorAll('input[type="file"]')).some((f) =>
      (f.getAttribute('accept') || '').toLowerCase().includes('video/')
    );

  const finalSubmitDetected = Array.from(
    container.querySelectorAll('button, input[type="submit"], [role="button"]')
  )
    .filter(isVisible)
    .some((btn) => {
      const lbl = normText(btn.innerText || btn.value || btn.getAttribute('aria-label') || '').toLowerCase();
      return /(enviar candidatura|finalizar candidatura|concluir candidatura|submit application|^submit$|^enviar$)/i.test(lbl);
    });

  const labelForElement = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return normText(aria);
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => normText(n.innerText));
      if (parts.join(' ').trim()) return parts.join(' ').trim();
    }
    const id = el.getAttribute('id');
    if (id) {
      const lbl = container.querySelector(`label[for="${CSS.escape(id)}"]`) || document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lbl) return normText(lbl.innerText);
    }
    const parentLabel = el.closest('label');
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll('input, textarea, select').forEach((c) => c.remove());
      const t = normText(clone.textContent);
      if (t) return t;
    }
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg) return normText(lg.innerText);
    }
    const prev = el.previousElementSibling;
    if (prev && ['LABEL', 'SPAN', 'P', 'DIV', 'STRONG'].includes(prev.tagName)) {
      const pt = normText(prev.innerText);
      if (pt && pt.length <= 160) return pt;
    }
    const fieldWrap = el.closest('.field, .form-group, .form-field, [data-field]');
    if (fieldWrap) {
      const wlbl = fieldWrap.querySelector('label');
      if (wlbl && !wlbl.getAttribute('for')) {
        const wt = normText(wlbl.innerText);
        if (wt && wt.length <= 160) return wt;
      }
    }
    const fallbackAttr = normText(el.getAttribute('placeholder') || el.getAttribute('name') || '');
    if (el.tagName.toLowerCase() === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'file') {
      if (!fallbackAttr || /\[|assets_attributes|_file\b/i.test(fallbackAttr)) {
        return 'Currículo';
      }
    }
    return fallbackAttr;
  };

  const inspectVisibleCounter = (el) => {
    const candidates = [];
    const seen = new Set();
    const pushNode = (node) => {
      if (!node || seen.has(node) || !isVisible(node)) return;
      if (['LABEL', 'INPUT', 'TEXTAREA', 'SELECT', 'SCRIPT', 'STYLE'].includes(node.tagName)) return;
      if (node.querySelector && node.querySelector('input, textarea, select')) return;
      seen.add(node);
      candidates.push(node);
    };
    const descBy = el.getAttribute('aria-describedby') || '';
    for (const refId of descBy.split(/\s+/).filter(Boolean)) {
      pushNode(document.getElementById(refId));
    }
    const wrapper = el.closest('.field, .form-group, .form-field, [data-field]') || el.parentElement;
    if (wrapper) {
      const visibleControls = Array.from(
        wrapper.querySelectorAll('input, textarea, select, [role="combobox"]')
      ).filter(isVisible);
      if (visibleControls.length === 1) {
        wrapper
          .querySelectorAll('[data-counter], .counter, .char-count, .word-count, small, span, div, p')
          .forEach(pushNode);
      }
    }
    if (el.nextElementSibling) pushNode(el.nextElementSibling);

    const charCaps = new Set();
    const wordCaps = new Set();
    let conflict = false;
    let unsupported = false;
    const unsupportedUnitRe = /\b(\d+)\s*(linhas?|lines?|bytes?|tokens?|kb|mb|segundos?|seconds?|minutos?|minutes?)\b/i;
    const slashRe = /\b\d+\s*\/\s*(\d+)\b(?:\s*(caracteres?|chars?|characters?|palavras?|words?|linhas?|lines?|bytes?|tokens?))?/i;
    const remRe = /\b(\d+)\s*(caracteres?|chars?|characters?|palavras?|words?|linhas?|lines?|bytes?|tokens?)\s*(?:restantes?|remaining|max(?:imo|imum)?|limite)\b/i;
    const maxRe = /(?:max(?:imo|imum)?|limite)\s*(?:de)?\s*[:\-]?\s*(\d+)\s*(caracteres?|chars?|characters?|palavras?|words?|linhas?|lines?|bytes?|tokens?)?/i;

    for (const node of candidates) {
      const txt = normText(node.innerText || node.textContent || '');
      if (!txt || txt.length > 90) continue;
      const isCounterNode =
        node.hasAttribute('data-counter') ||
        node.classList.contains('counter') ||
        node.classList.contains('char-count') ||
        node.classList.contains('word-count') ||
        descBy.includes(node.id || '___');
      const mUnsupported = txt.match(unsupportedUnitRe);
      if (mUnsupported && (isCounterNode || /restante|remaining|max|limite|\//i.test(txt))) {
        unsupported = true;
        continue;
      }
      const mSlash = txt.match(slashRe);
      if (mSlash) {
        const cap = parseInt(mSlash[1], 10);
        const unit = (mSlash[2] || '').toLowerCase();
        if (/(linhas?|lines?|bytes?|tokens?)/.test(unit)) {
          unsupported = true;
        } else if (/(palavras?|words?)/.test(unit)) {
          wordCaps.add(cap);
        } else {
          charCaps.add(cap);
        }
        continue;
      }
      const mRem = txt.match(remRe);
      if (mRem) {
        const cap = parseInt(mRem[1], 10);
        const unit = (mRem[2] || '').toLowerCase();
        if (/(linhas?|lines?|bytes?|tokens?)/.test(unit)) {
          unsupported = true;
        } else if (/(palavras?|words?)/.test(unit)) {
          wordCaps.add(cap);
        } else {
          charCaps.add(cap);
        }
        continue;
      }
      const mMax = txt.match(maxRe);
      if (mMax && (isCounterNode || mMax[2])) {
        const cap = parseInt(mMax[1], 10);
        const unit = (mMax[2] || '').toLowerCase();
        if (/(linhas?|lines?|bytes?|tokens?)/.test(unit)) {
          unsupported = true;
        } else if (/(palavras?|words?)/.test(unit)) {
          wordCaps.add(cap);
        } else {
          charCaps.add(cap);
        }
      }
    }
    if (charCaps.size > 1 || wordCaps.size > 1) {
      conflict = true;
    }
    return {
      max_chars: charCaps.size === 1 ? Array.from(charCaps)[0] : null,
      max_words: wordCaps.size === 1 ? Array.from(wordCaps)[0] : null,
      conflict,
      unsupported,
    };
  };

  const fields = [];
  const handledRadioGroups = new Set();
  const candidates = Array.from(
    container.querySelectorAll('input, textarea, select, [role="combobox"], [role="radiogroup"]')
  );

  for (const el of candidates) {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const inputType = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (el.classList.contains('select2-search__field') || el.classList.contains('select2-selection') || el.closest('.select2-container')) {
      continue;
    }
    const isStyledFileInput =
      tag === 'input' &&
      inputType === 'file' &&
      !el.closest('[hidden], .hidden') &&
      isVisible(el.closest('.field, .form-group, .form-field, label') || el.parentElement || container);
    const isStyledSelect =
      tag === 'select' &&
      el.classList.contains('select2-hidden-accessible') &&
      !el.closest('[hidden], .hidden') &&
      isVisible(el.parentElement || container);
    if (!isVisible(el) && !isStyledFileInput && !isStyledSelect) continue;

    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(inputType)) continue;
    if (inputType === 'password') continue;

    // Avoid duplicating native input nested directly inside a radiogroup
    if (inputType === 'radio') {
      const rname = el.getAttribute('name') || '';
      const groupKey = rname ? `name:${rname}` : `el:${candidates.indexOf(el)}`;
      if (handledRadioGroups.has(groupKey)) continue;
      handledRadioGroups.add(groupKey);

      const radios = rname
        ? Array.from(container.querySelectorAll(`input[type="radio"][name="${CSS.escape(rname)}"]`)).filter(isVisible)
        : [el];
      const fs = el.closest('fieldset');
      const groupLabel =
        (fs && fs.querySelector('legend') && normText(fs.querySelector('legend').innerText)) ||
        normText(el.getAttribute('aria-label') || '') ||
        rname ||
        labelForElement(el);

      const options = radios.map((r, oIdx) => {
        const optLabel = labelForElement(r) || r.value || `Opcao ${oIdx + 1}`;
        return {
          option_id: `opt-${oIdx}`,
          label: optLabel.slice(0, 160),
          value: (r.value || optLabel).slice(0, 160),
          selected: !!r.checked,
        };
      });
      const anySelected = radios.some((r) => !!r.checked);
      const anyReq = radios.some((r) => r.required || r.getAttribute('aria-required') === 'true');
      const anyEditable = radios.some((r) => !r.disabled);

      fields.push({
        label: groupLabel.slice(0, 200),
        control_kind: 'radio',
        required: anyReq,
        editable: anyEditable,
        has_value: anySelected,
        has_file: false,
        checked: null,
        accept: '',
        options,
        locator_hint: {
          tag: 'radio_group',
          radio_name: rname,
          name_attr: rname,
          dom_index: fields.length,
        constraints: ['min', 'max', 'step', 'minlength', 'maxlength', 'pattern']
          .map(attr => el.getAttribute(attr)),
        },
      });
      continue;
    }

    if (role === 'radiogroup') {
      if (el.querySelector('input[type="radio"]')) continue;
      const groupLabel = labelForElement(el);
      const optsNodes = Array.from(el.querySelectorAll('[role="radio"]')).filter(isVisible);
      const options = optsNodes.map((o, oIdx) => ({
        option_id: `opt-${oIdx}`,
        label: normText(o.innerText || o.getAttribute('aria-label') || '').slice(0, 160),
        value: normText(o.getAttribute('data-value') || o.innerText || '').slice(0, 160),
        selected: o.getAttribute('aria-checked') === 'true',
      }));
      fields.push({
        label: groupLabel.slice(0, 200),
        control_kind: options.length ? 'radio' : 'unsupported',
        required: el.getAttribute('aria-required') === 'true',
        editable: el.getAttribute('aria-disabled') !== 'true',
        has_value: options.some((o) => o.selected),
        has_file: false,
        checked: null,
        accept: '',
        options,
        locator_hint: {
          tag: 'aria_radiogroup',
          aria_label: (el.getAttribute('aria-label') || '').slice(0, 160),
          dom_index: fields.length,
        constraints: ['min', 'max', 'step', 'minlength', 'maxlength', 'pattern']
          .map(attr => el.getAttribute(attr)),
        },
      });
      continue;
    }

    let kind = 'unsupported';
    let options = [];
    let hasValue = false;
    let hasFile = false;
    let checked = null;

    if (tag === 'textarea') {
      kind = 'textarea';
      hasValue = !!(el.value || '').trim();
    } else if (tag === 'select') {
      kind = 'select';
      options = Array.from(el.options || []).map((o, oIdx) => ({
        option_id: `opt-${oIdx}`,
        label: normText(o.text || o.label || '').slice(0, 160),
        value: (o.value || '').slice(0, 160),
        selected: !!o.selected,
        available: !(o.disabled || o.closest('optgroup')?.disabled) && o.value.length <= 160,
      })).filter((o) => {
        const l = o.label.toLowerCase();
        return o.value !== '' || (l !== '' && !/^(selecione|select|escolha|--)/.test(l));
      });
      const selOpt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      hasValue = !!(el.value && selOpt && !(selOpt.value === '' && /^(selecione|select|escolha|--)/i.test(normText(selOpt.text))));
    } else if (role === 'combobox') {
      kind = 'custom_combobox';
      const txt = normText(el.value || el.innerText || '').trim();
      hasValue = !!txt && !/^(selecione|select|escolha)/i.test(txt);
    } else if (tag === 'input') {
      if (['text', 'search'].includes(inputType)) kind = 'text';
      else if (inputType === 'email') kind = 'email';
      else if (inputType === 'tel') kind = 'tel';
      else if (inputType === 'url') kind = 'url';
      else if (inputType === 'number') kind = 'number';
      else if (inputType === 'date') kind = 'date';
      else if (inputType === 'checkbox') kind = 'checkbox';
      else if (inputType === 'file') kind = 'file';
      else kind = 'unsupported';

      if (kind === 'checkbox') {
        checked = !!el.checked;
        hasValue = !!el.checked;
      } else if (kind === 'file') {
        hasFile = !!(el.files && el.files.length > 0);
        hasValue = hasFile;
      } else {
        const rawVal = (el.value || '').trim();
        hasValue = kind === 'tel' ? (!!rawVal && !/^\+\d{1,3}$/.test(rawVal)) : !!rawVal;
      }
    }

    const nameAttr = el.getAttribute('name') || '';
    const idAttr = el.getAttribute('id') || '';
    const ariaAttr = el.getAttribute('aria-label') || '';
    const placeholderAttr = el.getAttribute('placeholder') || '';
    const acceptAttr = el.getAttribute('accept') || '';

    fields.push({
      label: labelForElement(el).slice(0, 200),
      control_kind: kind,
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      editable: !(el.disabled || el.readOnly || el.getAttribute('aria-disabled') === 'true'),
      has_value: hasValue,
      has_file: hasFile,
      checked,
      accept: acceptAttr.slice(0, 120),
      options,
      locator_hint: {
        tag,
        role,
        input_type: inputType,
        id_attr: idAttr.slice(0, 120),
        name_attr: nameAttr.slice(0, 120),
        aria_label: ariaAttr.slice(0, 160),
        placeholder: placeholderAttr.slice(0, 160),
        dom_index: fields.length,
        constraints: ['min', 'max', 'step', 'minlength', 'maxlength', 'pattern']
          .map(attr => el.getAttribute(attr)),
        counters: ['text', 'textarea', 'email', 'tel', 'url', 'number'].includes(kind)
          ? inspectVisibleCounter(el)
          : null,
        invalid: el.getAttribute('aria-invalid') === 'true',
      },
    });
  }

  return {
    heading,
    vacancyContextTitle,
    containerSelector: res.selector,
    containerIndex: res.index || 0,
    multipleContainers: false,
    crossOriginIframes,
    openFormRequired: false,
    loginRequired,
    hasCaptcha,
    hasOtp,
    hasVideo,
    hasExternalAssessment,
    finalSubmitDetected,
    fields,
  };
}
"""


def _vacancy_context_mismatched(
    detected_title: str,
    *,
    expected_title: str | None,
    expected_company: str | None,
) -> bool:
    """Return True only when an explicit job title heading clearly contradicts the target vacancy."""
    det = normalize_label_text(detected_title)
    if not det or len(det) < 6:
        return False
    # Ignore generic step headings like "etapa 1", "dados pessoais", "candidatura"
    generic_headings = {
        "candidatura",
        "dados pessoais",
        "informacoes de contato",
        "experiencias",
        "curriculo",
        "perguntas adicionais",
        "revisao",
        "application",
        "contact info",
        "apply",
    }
    if det in generic_headings or det.startswith(("etapa ", "step ", "pagina ")):
        return False
    expected_tokens: set[str] = set()
    for source in (expected_title, expected_company):
        for tok in normalize_label_text(source or "").split():
            if len(tok) >= 3:
                expected_tokens.add(tok)
    if not expected_tokens:
        return False
    det_tokens = {tok for tok in det.split() if len(tok) >= 3}
    if not det_tokens:
        return False
    # Require explicit title-like phrase with zero token overlap against title/company
    return len(det_tokens & expected_tokens) == 0


def _compute_structural_fingerprint(
    *,
    portal: str,
    frame_url: str | None,
    heading: str,
    fields: list[FieldDescriptor],
) -> str:
    tokens = [
        portal,
        frame_url or "main",
        normalize_label_text(heading)[:80],
    ]
    for f in fields:
        tokens.append(
            f"{f.question_signature}:{f.control_kind}:{'1' if f.required else '0'}:{len(f.options)}"
        )
        if f.category == "salary":
            tokens.append(repr((f.locator_hint, [(o.option_id, o.label, o.value, o.available) for o in f.options])))
    return hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()[:24]


async def inspect_step(
    page: Page,
    *,
    navigation_gen: int = 1,
    expected_job_title: str | None = None,
    expected_company: str | None = None,
) -> StepSnapshot:
    """Inspect active portal container/same-origin frame and return a bounded StepSnapshot."""
    raw_url = page.url or ""
    portal = detect_portal(raw_url)
    adapter = get_adapter(portal)
    safe_url = _sanitize_tab_summary_url(raw_url)
    page_origin = extract_origin(raw_url)
    eval_args = {
        "containerSelectors": list(adapter.container_selectors),
        "applyPatterns": list(adapter.apply_cta_patterns),
    }

    main_data: dict[str, Any] = await page.main_frame.evaluate(_DOM_INSPECT_SCRIPT, eval_args)
    chosen_data = main_data
    chosen_frame_url: str | None = None
    cross_origins: list[str] = list(main_data.get("crossOriginIframes") or [])
    multiple_containers = bool(main_data.get("multipleContainers"))

    if not main_data.get("containerSelector") and not multiple_containers:
        same_origin_matches: list[tuple[str, dict[str, Any]]] = []
        for frame in page.frames:
            if frame == page.main_frame or frame.is_detached():
                continue
            f_url = frame.url or ""
            f_origin = extract_origin(f_url) if f_url.startswith(("http://", "https://")) else page_origin
            if f_origin != page_origin:
                continue
            try:
                f_data: dict[str, Any] = await frame.evaluate(_DOM_INSPECT_SCRIPT, eval_args)
            except Exception:
                continue
            if f_data.get("containerSelector"):
                same_origin_matches.append((_sanitize_tab_summary_url(f_url) or f_url, f_data))
            elif f_data.get("multipleContainers"):
                multiple_containers = True
        if len(same_origin_matches) == 1:
            chosen_frame_url, chosen_data = same_origin_matches[0]
        elif len(same_origin_matches) > 1:
            multiple_containers = True

    heading = str(chosen_data.get("heading") or main_data.get("heading") or "")[:160]
    vacancy_ctx_title = str(
        chosen_data.get("vacancyContextTitle") or main_data.get("vacancyContextTitle") or ""
    )[:160]

    raw_fields: list[dict[str, Any]] = list(chosen_data.get("fields") or [])
    descriptors: list[FieldDescriptor] = []
    for idx, rf in enumerate(raw_fields):
        label = str(rf.get("label") or "").strip() or f"Campo {idx + 1}"
        kind_raw = str(rf.get("control_kind") or "unsupported")
        control_kind: ControlKind = (
            kind_raw  # type: ignore[assignment]
            if kind_raw
            in {
                "text",
                "textarea",
                "email",
                "tel",
                "url",
                "number",
                "date",
                "select",
                "radio",
                "checkbox",
                "file",
                "custom_combobox",
                "unsupported",
            }
            else "unsupported"
        )
        raw_opts = list(rf.get("options") or [])
        opts = tuple(
            FieldOption(
                option_id=str(o.get("option_id") or f"opt-{oidx}"),
                label=str(o.get("label") or "")[:160],
                value=str(o.get("value") or "")[:160],
                selected=bool(o.get("selected")),
                available=bool(o.get("available", True)),
            )
            for oidx, o in enumerate(raw_opts)
        )
        sig = build_question_signature(label, control_kind, [o.label for o in opts])
        sig_digest = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:8]
        field_id = f"f-{idx}-{sig_digest}"
        accept = str(rf.get("accept") or "")
        category = classify_field_category(label, control_kind, accept)
        hint = dict(rf.get("locator_hint") or {})
        hint_tokens = f"{hint.get('name_attr', '')} {hint.get('id_attr', '')}"
        if category in {"contact_name", "custom_question"}:
            if re.search(r"(first_?name|given_?name)", hint_tokens, re.I):
                category = "contact_first_name"
            elif re.search(r"(last_?name|surname|family_?name)", hint_tokens, re.I):
                category = "contact_last_name"
        hint["container_selector"] = chosen_data.get("containerSelector")
        hint["frame_url"] = chosen_frame_url
        descriptors.append(
            FieldDescriptor(
                field_id=field_id,
                question_signature=sig,
                label=label,
                control_kind=control_kind,
                required=bool(rf.get("required")),
                editable=bool(rf.get("editable", True)),
                has_value=bool(rf.get("has_value")),
                has_file=bool(rf.get("has_file")),
                checked=rf.get("checked"),
                accept=accept,
                options=opts,
                category=category,
                locator_hint=hint,
            )
        )

    if any(d.category == "contact_last_name" for d in descriptors):
        descriptors = [
            FieldDescriptor(
                field_id=d.field_id,
                question_signature=d.question_signature,
                label=d.label,
                control_kind=d.control_kind,
                required=d.required,
                editable=d.editable,
                has_value=d.has_value,
                has_file=d.has_file,
                checked=d.checked,
                accept=d.accept,
                options=d.options,
                category="contact_first_name",
                locator_hint=d.locator_hint,
            )
            if d.category == "contact_name" and normalize_label_text(d.label) in {"nome", "name"}
            else d
            for d in descriptors
        ]

    fingerprint = _compute_structural_fingerprint(
        portal=portal,
        frame_url=chosen_frame_url,
        heading=heading,
        fields=descriptors,
    )
    has_resume_upload = any(f.category == "resume_file" for f in descriptors)
    profile_warning = matches_profile_surface_warning(portal, raw_url, heading)

    return StepSnapshot(
        step_id=f"step-{secrets.token_urlsafe(12)}",
        navigation_gen=navigation_gen,
        portal=portal,
        page_url=safe_url,
        page_origin=page_origin,
        heading=heading,
        vacancy_context_title=vacancy_ctx_title,
        vacancy_mismatch=_vacancy_context_mismatched(
            vacancy_ctx_title,
            expected_title=expected_job_title,
            expected_company=expected_company,
        ),
        profile_surface_warning=profile_warning,
        container_selector=chosen_data.get("containerSelector"),
        frame_url=chosen_frame_url,
        multiple_containers=multiple_containers,
        cross_origin_iframes=tuple(dict.fromkeys(cross_origins)),
        open_form_required=bool(chosen_data.get("openFormRequired") and not descriptors),
        login_required=bool(chosen_data.get("loginRequired") or main_data.get("loginRequired")),
        has_captcha=bool(chosen_data.get("hasCaptcha") or main_data.get("hasCaptcha")),
        has_otp=bool(chosen_data.get("hasOtp") or main_data.get("hasOtp")),
        has_video=bool(chosen_data.get("hasVideo") or main_data.get("hasVideo")),
        has_external_assessment=bool(
            chosen_data.get("hasExternalAssessment") or main_data.get("hasExternalAssessment")
        ),
        final_submit_detected=bool(chosen_data.get("finalSubmitDetected")),
        has_resume_upload=has_resume_upload,
        structural_fingerprint=fingerprint,
        container_index=int(chosen_data.get("containerIndex") or 0),
        fields=tuple(descriptors),
    )


def _profile_literal_map(doc: MasterProfileDocument) -> dict[str, str]:
    """Return literal non-fabricated candidate scalars keyed cleanly by canonical profile key."""
    out: dict[str, str] = {}
    c = doc.contact
    p = doc.preferences

    if c.name and c.name.strip():
        full_name = c.name.strip()
        parts = full_name.split()
        out["contact.name"] = full_name
        if parts:
            out["contact.first_name"] = parts[0]
        if len(parts) >= 2:
            out["contact.last_name"] = " ".join(parts[1:])
    if c.email and c.email.strip():
        out["contact.email"] = c.email.strip()
    if c.phone and c.phone.strip():
        out["contact.phone"] = c.phone.strip()
    if c.city and c.city.strip():
        out["contact.city"] = c.city.strip()
    if c.state and c.state.strip():
        out["contact.state"] = c.state.strip()
    if c.country and c.country.strip():
        out["contact.country"] = c.country.strip()
    loc_parts = [x.strip() for x in (c.city, c.state, c.country) if x and x.strip()]
    if loc_parts:
        out["contact.location"] = ", ".join(loc_parts)
    elif p.location and p.location.strip():
        out["contact.location"] = p.location.strip()
    if c.links.linkedin and c.links.linkedin.strip():
        out["contact.links.linkedin"] = c.links.linkedin.strip()
    if c.links.github and c.links.github.strip():
        out["contact.links.github"] = c.links.github.strip()
    if c.links.portfolio and c.links.portfolio.strip():
        out["contact.links.portfolio"] = c.links.portfolio.strip()

    if p.availability and p.availability.strip():
        out["preferences.availability"] = p.availability.strip()
    if p.work_authorization and p.work_authorization.strip():
        out["preferences.work_authorization"] = p.work_authorization.strip()

    return out


_ALLOWED_PROFILE_KEY_BY_CONTROL: dict[str, set[str]] = {
    "text": {
        "contact.name",
        "contact.first_name",
        "contact.last_name",
        "contact.email",
        "contact.phone",
        "contact.city",
        "contact.state",
        "contact.country",
        "contact.postal_code",
        "contact.location",
        "contact.links.linkedin",
        "contact.links.github",
        "contact.links.portfolio",
        "preferences.availability",
        "preferences.work_authorization",
    },
    "textarea": {
        "contact.location",
        "preferences.availability",
        "preferences.work_authorization",
    },
    "email": {"contact.email"},
    "tel": {"contact.phone"},
    "url": {
        "contact.links.linkedin",
        "contact.links.github",
        "contact.links.portfolio",
    },
    "number": {"contact.postal_code"},
}

_CATEGORY_TO_PROFILE_KEY: dict[str, str] = {
    "contact_name": "contact.name",
    "contact_first_name": "contact.first_name",
    "contact_last_name": "contact.last_name",
    "contact_email": "contact.email",
    "contact_phone": "contact.phone",
    "contact_city": "contact.city",
    "contact_state": "contact.state",
    "contact_country": "contact.country",
    "contact_postal_code": "contact.postal_code",
    "contact_location": "contact.location",
    "contact_linkedin": "contact.links.linkedin",
    "contact_github": "contact.links.github",
    "contact_portfolio": "contact.links.portfolio",
    "availability": "preferences.availability",
    "work_auth": "preferences.work_authorization",
}

_BOOLEAN_ALIASES: tuple[tuple[set[str], set[str]], ...] = (
    ({"sim", "yes", "true", "1", "verdadeiro"}, {"sim", "yes", "true", "1"}),
    ({"nao", "no", "false", "0", "falso"}, {"nao", "no", "false", "0"}),
)


def resolve_option_for_answer(answer: str, options: tuple[FieldOption, ...]) -> FieldOption | None:
    """Semantically resolve a text/boolean saved answer against current portal option labels."""
    raw = (answer or "").strip()
    if not raw or not options:
        return None
    norm_ans = normalize_label_text(raw)
    # 1. Exact value or exact normalized label
    for opt in options:
        if opt.value == raw or normalize_label_text(opt.label) == norm_ans:
            return opt
    # 2. Boolean/Yes-No equivalence
    for group, targets in _BOOLEAN_ALIASES:
        if norm_ans in group:
            for opt in options:
                opt_norm = normalize_label_text(opt.label)
                opt_low = opt.label.strip().lower()
                if (
                    opt_norm in targets
                    or opt.value.lower() in targets
                    or any(
                        opt_norm.startswith(f"{t} ")
                        and (opt_low.startswith(f"{t},") or opt_low.startswith(f"{t} -"))
                        for t in targets
                    )
                ):
                    return opt
    # 3. Clear single token-aligned contains match for city/country/work-auth strings
    if len(norm_ans) >= 3:
        boolean_tokens = {"sim", "yes", "nao", "no", "true", "false"}
        matches: list[FieldOption] = []
        for opt in options:
            opt_norm = normalize_label_text(opt.label)
            if len(opt_norm) < 3 or opt_norm in boolean_tokens:
                continue
            if re.search(rf"\b{re.escape(norm_ans)}\b", opt_norm) or re.search(
                rf"\b{re.escape(opt_norm)}\b", norm_ans
            ):
                matches.append(opt)
        if len(matches) == 1:
            return matches[0]
    return None


def _field_blocker_options(field: FieldDescriptor) -> tuple[dict[str, str], ...]:
    return tuple({"option_id": o.option_id, "label": o.label} for o in field.options)


def _resolve_candidate_for_field(
    field: FieldDescriptor,
    raw_val: str,
    *,
    rule: FieldRule,
    constraints: ControlConstraints,
    expected_locale: Locale,
    allow_test_origins: set[str] | None,
    can_answer: bool,
) -> tuple[PlannedMutation | None, StepBlocker | None, dict[str, str] | None]:
    """Validate and convert a candidate literal/answer into a PlannedMutation, blocker, or skip."""
    if field.control_kind == "checkbox":
        if normalize_label_text(raw_val) in {"sim", "yes", "true", "1", "verdadeiro"}:
            return PlannedMutation(field=field, kind="profile_value", value="true"), None, None
        return None, None, {"field_id": field.field_id, "label": field.label, "reason": "explicit_false"}

    if field.control_kind in {"select", "radio"}:
        opt = resolve_option_for_answer(raw_val, field.options)
        if opt is None:
            return None, None, None
        return (
            PlannedMutation(field=field, kind="choose_option", value=opt.label, option_id=opt.option_id),
            None,
            None,
        )

    outcome = validate_candidate_value(
        constraints,
        raw_val,
        rule_id=rule.rule_id,
        intent=rule.formatter or rule.intent,
        source_locale=expected_locale,
        allow_test_origins=allow_test_origins,
    )
    if outcome.valid and outcome.value is not None:
        return PlannedMutation(field=field, kind="profile_value", value=outcome.value), None, None

    return (
        None,
        StepBlocker(
            code=outcome.blocker_code or "invalid_field_value",
            field_id=field.field_id,
            label=field.label,
            question_signature=field.question_signature,
            field_type=field.control_kind,
            message=outcome.blocker_message or f"Valor inválido para {field.label}.",
            can_answer=can_answer,
        ),
        None,
    )


def _fit_from_arsenal_tuple(
    variants: tuple[str, ...],
    constraints: ControlConstraints,
    expected_locale: Locale,
) -> tuple[str | None, str | None]:
    """Select/fit first valid variant from pre-rendered Arsenal variant tuple."""
    if constraints.has_conflict:
        return None, "unsupported_counter" if constraints.counter_unsupported else "conflicting_constraints"
    for item in variants:
        ok, _, _ = validate_text_limits(item, constraints)
        if ok:
            return item, None
    lv_kwargs: dict[str, str] = {}
    for item in variants:
        u16 = len(item.encode("utf-16-le")) // 2
        if u16 <= 150 and "\n" not in item and "\r" not in item and "short" not in lv_kwargs:
            lv_kwargs["short"] = item
        elif u16 <= 500 and "medium" not in lv_kwargs:
            lv_kwargs["medium"] = item
        elif u16 <= 1500 and "long" not in lv_kwargs:
            lv_kwargs["long"] = item
    if not lv_kwargs:
        return None, "length_mismatch"
    fitted, err, _, _ = fit_arsenal_variants(
        LengthVariants(**lv_kwargs),
        {},
        constraints,
        entry_locale=expected_locale,
        expected_locale=expected_locale,
    )
    return fitted, err


async def build_field_plan(
    snapshot: StepSnapshot,
    *,
    doc: MasterProfileDocument,
    answers_json: dict[str, str],
    resume_pdf_path: Path | None,
    job_title: str = "",
    job_company: str = "",
    job_description: str = "",
    llm_gateway: LlmGateway | None = None,
    db: Session | None = None,
    remaining_budget_usd: float | None = None,
    approved_salary: dict[str, str] | None = None,
    approved_actions: dict[str, str] | None = None,
    arsenal_answers: dict[str, tuple[str, ...]] | None = None,
    arsenal_candidates: Mapping[str, Any] | None = None,
    expected_locale: Locale = "pt-BR",
    allow_test_origins: set[str] | None = None,
    slot_values: Mapping[str, str] | None = None,
) -> PlannedStepResolution:
    """Resolve current step fields deterministically with typed rules, Arsenal fitting, and queue gates."""
    del llm_gateway, db, remaining_budget_usd  # Open-ended worker fills are disabled in Phase 3+
    blockers: list[StepBlocker] = []
    skipped: list[dict[str, str]] = []
    mutations: list[PlannedMutation] = []
    notices: list[str] = []

    if snapshot.profile_surface_warning:
        notices.append(snapshot.profile_surface_warning)

    # Hard gates that block form mutations immediately
    if snapshot.login_required or snapshot.has_otp:
        blockers.append(
            StepBlocker(
                code="otp_required" if snapshot.has_otp else "login_required",
                message=(
                    "Autenticação ou código OTP exigido no navegador. "
                    "Conclua o login/verificação no Chrome e clique em Preencher etapa atual."
                ),
            )
        )
    if snapshot.has_captcha:
        blockers.append(
            StepBlocker(
                code="captcha",
                message=(
                    "CAPTCHA detectado nesta etapa. Resolva o desafio manualmente no Chrome "
                    "e clique em Preencher etapa atual."
                ),
            )
        )
    if snapshot.has_external_assessment:
        blockers.append(
            StepBlocker(
                code="external_assessment",
                message="Avaliação externa detectada; conclua diretamente no navegador.",
            )
        )
    if snapshot.has_video:
        blockers.append(
            StepBlocker(
                code="video_required",
                message="Etapa exige gravação ou envio de vídeo; conclua diretamente no navegador.",
            )
        )
    if snapshot.cross_origin_iframes:
        blockers.append(
            StepBlocker(
                code="unknown_origin",
                message=(
                    f"Formulário em iframe de origem externa ({', '.join(snapshot.cross_origin_iframes)}). "
                    "Preencha manualmente no navegador ou anexe a aba externa aberta."
                ),
            )
        )
    if snapshot.multiple_containers:
        blockers.append(
            StepBlocker(
                code="multiple_containers",
                message=(
                    "Mais de um formulário de candidatura visível na mesma página. "
                    "Abra apenas o modal/etapa desejado no navegador e tente novamente."
                ),
            )
        )
    if snapshot.open_form_required:
        blockers.append(
            StepBlocker(
                code="open_form_required",
                message="Abra o formulário da vaga no navegador e clique em Preencher etapa atual.",
            )
        )
    if snapshot.vacancy_mismatch:
        blockers.append(
            StepBlocker(
                code="vacancy_mismatch",
                message=(
                    f"A aba exibe outra vaga ({snapshot.vacancy_context_title}). "
                    "Use Anexar aba aberta ou Reabrir no navegador para voltar à vaga correta."
                ),
            )
        )

    if blockers:
        return PlannedStepResolution(
            mutations=(),
            skipped=(),
            blockers=tuple(blockers),
            notices=tuple(notices),
        )

    if not snapshot.fields and not snapshot.container_selector:
        blockers.append(
            StepBlocker(
                code="open_form_required",
                message="Abra o formulário da vaga no navegador e clique em Preencher etapa atual.",
            )
        )
        return PlannedStepResolution(
            mutations=(),
            skipped=(),
            blockers=tuple(blockers),
            notices=tuple(notices),
        )

    if not snapshot.has_resume_upload and snapshot.portal in {"gupy", "infojobs"}:
        notices.append(
            f"O portal {snapshot.portal.capitalize()} não exibiu campo de anexo de currículo nesta etapa "
            "(ou utiliza o currículo cadastrado na conta)."
        )

    literals = {**_profile_literal_map(doc), **extract_canonical_literals(doc)}
    slot_map = (
        dict(slot_values)
        if slot_values is not None
        else build_verified_slot_values(
            doc,
            job_company=job_company,
            job_title=job_title,
            job_description=job_description,
        )
    )
    std_by_sig = {
        s.question_signature: s.answer
        for s in doc.standard_answers
        if s.question_signature and s.answer.strip()
    }
    std_by_label = {
        normalize_label_text(s.label): s.answer
        for s in doc.standard_answers
        if normalize_label_text(s.label) and s.answer.strip()
    }

    still_unresolved: list[FieldDescriptor] = []

    for f in snapshot.fields:
        if not f.editable:
            skipped.append({"field_id": f.field_id, "label": f.label, "reason": "readonly_or_disabled"})
            continue

        if f.has_value:
            skipped.append({"field_id": f.field_id, "label": f.label, "reason": "already_populated"})
            continue

        rule = match_field_rule(f.label, f.control_kind, category_hint=f.category)
        constraints = extract_control_constraints(
            control_kind=f.control_kind,
            label=f.label,
            locale=expected_locale,
            required=f.required,
            locator_hint=f.locator_hint,
        )

        # 1. Explicit counter/constraint conflicts halt auto-write on this field
        if constraints.counter_unsupported:
            blockers.append(
                StepBlocker(
                    code="unsupported_counter",
                    field_id=f.field_id,
                    label=f.label,
                    message=f"Contador visível com unidade não suportada em '{f.label}'. Preencha no Chrome.",
                    can_answer=False,
                )
            )
            continue
        if constraints.has_conflict:
            blockers.append(
                StepBlocker(
                    code="conflicting_constraints",
                    field_id=f.field_id,
                    label=f.label,
                    message=f"Restrições conflitantes detectadas em '{f.label}'. Revise no Chrome.",
                    can_answer=False,
                )
            )
            continue

        # 2. Protected / manual-only / salary controls
        if f.category == "salary" or rule.intent == "salary":
            value = (approved_salary or {}).get(f.field_id)
            if value is not None and f.control_kind in {"number", "text", "textarea"}:
                if f.control_kind == "number":
                    out = validate_candidate_value(
                        constraints,
                        value,
                        rule_id=rule.rule_id,
                        intent="salary",
                        source_locale=expected_locale,
                    )
                    if not out.valid or out.value is None:
                        blockers.append(
                            StepBlocker(
                                code="salary_approval_required",
                                field_id=f.field_id,
                                label=f.label,
                                message=out.blocker_message or "Valor salarial incompatível com o campo.",
                            )
                        )
                        continue
                    value = out.value
                else:
                    lim_ok, _, lim_msg = validate_text_limits(value, constraints)
                    if not lim_ok:
                        blockers.append(
                            StepBlocker(
                                code="salary_approval_required",
                                field_id=f.field_id,
                                label=f.label,
                                message=lim_msg or "Valor salarial incompatível com os limites do campo.",
                            )
                        )
                        continue
                mutations.append(PlannedMutation(field=f, kind="profile_value", value=value))
                continue
            if value is not None and f.control_kind == "select":
                option = next((o for o in f.options if o.option_id == value and o.value and o.available), None)
                if option is not None:
                    mutations.append(
                        PlannedMutation(field=f, kind="choose_option", value=option.value, option_id=option.option_id)
                    )
                    continue
            blockers.append(
                StepBlocker(
                    code="salary_approval_required",
                    field_id=f.field_id,
                    label=f.label,
                    message="Confirme a pretensão salarial antes de preencher este campo.",
                )
            )
            continue

        if f.category == "sensitive_id" or (
            rule.sensitivity == "sensitive_pii" and rule.source in {"pending_approval", "manual_only"}
        ):
            queued_val = (approved_actions or {}).get(f.field_id)
            if (
                rule.source != "manual_only"
                and queued_val is not None
                and f.control_kind in {"text", "textarea", "number", "date"}
            ):
                mut, blk, _ = _resolve_candidate_for_field(
                    f,
                    queued_val,
                    rule=rule,
                    constraints=constraints,
                    expected_locale=expected_locale,
                    allow_test_origins=allow_test_origins,
                    can_answer=False,
                )
                if mut is not None:
                    mutations.append(mut)
                    continue
                if blk is not None:
                    blockers.append(blk)
                    continue
            blockers.append(
                StepBlocker(
                    code="sensitive_manual",
                    field_id=f.field_id,
                    label=f.label,
                    message=f"Dado sensível ou documento ({f.label}) deve ser preenchido manualmente no Chrome.",
                    can_answer=False,
                )
            )
            continue

        if f.category == "legal_consent" or rule.intent == "legal_consent":
            blockers.append(
                StepBlocker(
                    code="consent_required",
                    field_id=f.field_id,
                    label=f.label,
                    message=f"Confirmação de termos/consentimento ({f.label}) exige marcação manual no Chrome.",
                    can_answer=False,
                )
            )
            continue

        if f.category == "non_resume_file" or rule.intent == "unsupported_upload":
            skipped.append({"field_id": f.field_id, "label": f.label, "reason": "non_resume_file_skipped"})
            if f.required:
                blockers.append(
                    StepBlocker(
                        code="upload_manual_required",
                        field_id=f.field_id,
                        label=f.label,
                        message=f"Anexo específico ({f.label}) não utiliza o PDF de currículo; envie manualmente no Chrome.",
                        can_answer=False,
                    )
                )
            continue

        if f.category == "resume_file" or rule.intent == "resume_file":
            if resume_pdf_path is not None:
                mutations.append(
                    PlannedMutation(
                        field=f,
                        kind="resume_upload",
                        value=str(resume_pdf_path),
                    )
                )
            elif f.required:
                blockers.append(
                    StepBlocker(
                        code="upload_failed",
                        field_id=f.field_id,
                        label=f.label,
                        message="Currículo PDF ausente para envio; clique em Preparar & Aplicar.",
                        can_answer=False,
                    )
                )
            continue

        if rule.source == "manual_only":
            blockers.append(
                StepBlocker(
                    code="sensitive_manual",
                    field_id=f.field_id,
                    label=f.label,
                    message=f"Campo protegido ({f.label}) exige preenchimento manual no Chrome.",
                    can_answer=False,
                )
            )
            continue

        if f.control_kind == "unsupported":
            if f.required:
                blockers.append(
                    StepBlocker(
                        code="unsupported_widget",
                        field_id=f.field_id,
                        label=f.label,
                        message=f"Controle personalizado ({f.label}) exige preenchimento manual no Chrome.",
                        can_answer=False,
                    )
                )
            else:
                skipped.append({"field_id": f.field_id, "label": f.label, "reason": "unsupported_widget"})
            continue

        # 3. Explicitly approved current-action value
        queued_action_val = (approved_actions or {}).get(f.field_id)
        if queued_action_val is not None:
            if f.control_kind in {"select", "radio"}:
                opt = next(
                    (o for o in f.options if o.option_id == queued_action_val and o.available),
                    None,
                ) or resolve_option_for_answer(queued_action_val, f.options)
                if opt is not None:
                    mutations.append(
                        PlannedMutation(field=f, kind="choose_option", value=opt.label, option_id=opt.option_id)
                    )
                    continue
            else:
                mut, blk, skp = _resolve_candidate_for_field(
                    f,
                    queued_action_val,
                    rule=rule,
                    constraints=constraints,
                    expected_locale=expected_locale,
                    allow_test_origins=allow_test_origins,
                    can_answer=True,
                )
                if mut is not None:
                    mutations.append(mut)
                    continue
                if blk is not None:
                    blockers.append(blk)
                    continue
                if skp is not None:
                    skipped.append(skp)
                    continue

        # 4. Application-scoped answers already saved for this step
        app_answer = answers_json.get(f.question_signature)
        if app_answer is not None and str(app_answer).strip():
            mut, blk, skp = _resolve_candidate_for_field(
                f,
                str(app_answer).strip(),
                rule=rule,
                constraints=constraints,
                expected_locale=expected_locale,
                allow_test_origins=allow_test_origins,
                can_answer=True,
            )
            if mut is not None:
                mutations.append(mut)
                continue
            if blk is not None:
                if f.required:
                    blockers.append(blk)
                else:
                    skipped.append({"field_id": f.field_id, "label": f.label, "reason": blk.code})
                continue
            if skp is not None:
                skipped.append(skp)
                continue

        # 5. Canonical MasterProfile field mapping (validated & formatted)
        profile_key = (
            _CATEGORY_TO_PROFILE_KEY.get(f.category)
            if f.category in {"contact_first_name", "contact_last_name"}
            else (rule.profile_key if not rule.requires_manual else None)
            or _CATEGORY_TO_PROFILE_KEY.get(f.category)
        )
        if profile_key and profile_key in literals:
            mut, blk, skp = _resolve_candidate_for_field(
                f,
                literals[profile_key],
                rule=rule,
                constraints=constraints,
                expected_locale=expected_locale,
                allow_test_origins=allow_test_origins,
                can_answer=True,
            )
            if mut is not None:
                mutations.append(mut)
                continue
            if blk is not None:
                blockers.append(blk)
                continue
            if skp is not None:
                skipped.append(skp)
                continue

        # 6. Approved reusable Arsenal answers (with slot interpolation + 95% sentence-boundary trim)
        if f.control_kind in {"text", "textarea"}:
            cand = (arsenal_candidates or {}).get(f.question_signature)
            if cand is not None:
                fitted, fit_err, _, _ = fit_arsenal_variants(
                    cand.payload.length_variants,
                    slot_map,
                    constraints,
                    entry_locale=cand.language,
                    expected_locale=expected_locale,
                )
                if fitted is not None:
                    mutations.append(PlannedMutation(field=f, kind="profile_value", value=fitted))
                    continue
                if f.required:
                    blockers.append(
                        StepBlocker(
                            code=fit_err or "length_mismatch",
                            field_id=f.field_id,
                            label=f.label,
                            question_signature=f.question_signature,
                            field_type=f.control_kind,
                            message=f"Nenhuma variação aprovada do Arsenal cabe em '{f.label}'.",
                            can_answer=True,
                        )
                    )
                else:
                    skipped.append({"field_id": f.field_id, "label": f.label, "reason": fit_err or "length_mismatch"})
                continue

            variants = (arsenal_answers or {}).get(f.question_signature)
            if variants:
                fitted, fit_err = _fit_from_arsenal_tuple(variants, constraints, expected_locale)
                if fitted is not None:
                    mutations.append(PlannedMutation(field=f, kind="profile_value", value=fitted))
                    continue
                if f.required:
                    blockers.append(
                        StepBlocker(
                            code=fit_err or "length_mismatch",
                            field_id=f.field_id,
                            label=f.label,
                            question_signature=f.question_signature,
                            field_type=f.control_kind,
                            message=f"Nenhuma variação aprovada do Arsenal cabe em '{f.label}'.",
                            can_answer=True,
                        )
                    )
                else:
                    skipped.append({"field_id": f.field_id, "label": f.label, "reason": fit_err or "length_mismatch"})
                continue

        # 7. Reviewed non-sensitive standard_answers compatibility lookup (sig -> label)
        std_answer = std_by_sig.get(f.question_signature) or std_by_label.get(normalize_label_text(f.label))
        if std_answer is not None and str(std_answer).strip():
            mut, blk, skp = _resolve_candidate_for_field(
                f,
                str(std_answer).strip(),
                rule=rule,
                constraints=constraints,
                expected_locale=expected_locale,
                allow_test_origins=allow_test_origins,
                can_answer=True,
            )
            if mut is not None:
                mutations.append(mut)
                continue
            if blk is not None:
                if f.required:
                    blockers.append(blk)
                else:
                    skipped.append({"field_id": f.field_id, "label": f.label, "reason": blk.code})
                continue
            if skp is not None:
                skipped.append(skp)
                continue

        if f.control_kind == "custom_combobox":
            if f.required:
                blockers.append(
                    StepBlocker(
                        code="unsupported_widget",
                        field_id=f.field_id,
                        label=f.label,
                        message=f"Campo combobox ({f.label}) exige seleção manual no navegador.",
                        can_answer=False,
                    )
                )
            else:
                skipped.append({"field_id": f.field_id, "label": f.label, "reason": "custom_combobox_optional"})
            continue

        still_unresolved.append(f)

    for f in still_unresolved:
        if f.required:
            opts = (
                ({"option_id": "true", "label": "Sim"}, {"option_id": "false", "label": "Não"})
                if f.control_kind == "checkbox"
                else _field_blocker_options(f)
            )
            blockers.append(
                StepBlocker(
                    code="missing_answer",
                    field_id=f.field_id,
                    label=f.label,
                    question_signature=f.question_signature,
                    field_type="boolean" if f.control_kind == "checkbox" else ("option" if f.options else f.control_kind),
                    options=opts,
                    message=f"Pergunta obrigatória sem resposta salva: {f.label}",
                    can_answer=True,
                )
            )
        else:
            skipped.append({"field_id": f.field_id, "label": f.label, "reason": "optional_unresolved"})

    return PlannedStepResolution(
        mutations=tuple(mutations),
        skipped=tuple(skipped),
        blockers=tuple(blockers),
        notices=tuple(notices),
        llm_fallback_note=None,
        llm_usage_events=(),
    )


def _resolve_target_frame(page: Page, snapshot: StepSnapshot) -> Frame:
    if not snapshot.frame_url:
        return page.main_frame
    for frame in page.frames:
        if frame.is_detached():
            continue
        sanitized = _sanitize_tab_summary_url(frame.url or "")
        if sanitized == snapshot.frame_url or (frame.url or "") == snapshot.frame_url:
            return frame
    raise StepChangedError(
        written_count=0,
        message="O iframe do formulário não está mais disponível; reinspicione a etapa atual.",
    )


async def _resolve_unique_control_locator(
    frame: Frame, snapshot: StepSnapshot, descriptor: FieldDescriptor
) -> Locator | None:
    """Resolve a fresh bounded Playwright locator inside the inspected container/frame."""
    hint = descriptor.locator_hint
    container_sel = str(hint.get("container_selector") or snapshot.container_selector or "body")
    container = frame.locator(container_sel).nth(snapshot.container_index)

    if descriptor.control_kind == "radio":
        if hint.get("tag") == "aria_radiogroup":
            aria_lbl = str(hint.get("aria_label") or descriptor.label or "")
            if aria_lbl:
                rg = container.get_by_role("radiogroup", name=aria_lbl, exact=True)
                if await rg.count() == 1:
                    return rg.first
            rg_all = container.locator('[role="radiogroup"]')
            if await rg_all.count() == 1:
                return rg_all.first
            return None
        rname = str(hint.get("radio_name") or "")
        if rname:
            loc = container.locator(f'input[type="radio"][name="{rname.replace(chr(34), "")}"]')
            if await loc.count() > 0:
                return loc
        return None

    # Exact safe accessible name or form attribute matching (prefer unambiguous single match)
    candidates: list[Locator] = []
    id_attr = str(hint.get("id_attr") or "")
    name_attr = str(hint.get("name_attr") or "")
    aria_attr = str(hint.get("aria_label") or "")
    tag = str(hint.get("tag") or "input")

    if id_attr and re.fullmatch(r"[A-Za-z0-9_-]+", id_attr):
        candidates.append(container.locator(f"#{id_attr}"))
    if name_attr and re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+", name_attr):
        candidates.append(container.locator(f'{tag}[name="{name_attr}"]'))
    if aria_attr:
        candidates.append(container.get_by_label(aria_attr, exact=True))
    if descriptor.label:
        candidates.append(container.get_by_label(descriptor.label, exact=True))

    for loc in candidates:
        try:
            if await loc.count() == 1:
                return loc.first
        except Exception:
            continue
    return None


async def _control_already_populated(locator: Locator, descriptor: FieldDescriptor) -> bool:
    """Verify right before writing that user/portal edits weren't populated after snapshot."""
    try:
        if descriptor.control_kind == "radio":
            if descriptor.locator_hint.get("tag") == "aria_radiogroup":
                radios = locator.locator('[role="radio"]')
                rc = await radios.count()
                for i in range(rc):
                    if (await radios.nth(i).get_attribute("aria-checked")) == "true":
                        return True
                return False
            count = await locator.count()
            for i in range(count):
                if await locator.nth(i).is_checked():
                    return True
            return False
        if descriptor.control_kind == "checkbox":
            return await locator.is_checked()
        if descriptor.control_kind == "file":
            return bool(await locator.evaluate("el => !!(el.files && el.files.length > 0)"))
        if descriptor.control_kind == "select":
            val = await locator.evaluate("el => el.value || ''")
            return bool(val.strip())
        val = (await locator.input_value() or "").strip()
        if descriptor.control_kind == "tel" and re.fullmatch(r"\+\d{1,3}", val):
            return False
        return bool(val)
    except Exception:
        return False


def _record_ambiguous(
    field: FieldDescriptor,
    skipped_report: list[dict[str, str]],
    runtime_blockers: list[StepBlocker],
) -> None:
    skipped_report.append({"field_id": field.field_id, "label": field.label, "reason": "ambiguous_locator"})
    if field.required:
        runtime_blockers.append(
            StepBlocker(
                code="unsupported_widget",
                field_id=field.field_id,
                label=field.label,
                message=(
                    f"Campo ambíguo ou duplicado ({field.label}) não pôde ser localizado com segurança; "
                    "preencha manualmente no navegador."
                ),
                can_answer=False,
            )
        )


async def fill_current_step(
    page: Page,
    session: BrowserSession,
    application_id: str,
    snapshot: StepSnapshot,
    resolution: PlannedStepResolution,
    *,
    job_url: str | None = None,
    resume_pdf_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str], list[StepBlocker], bool]:
    """Execute bounded mutations for a single step with pre-write stale-DOM & origin guards."""
    filled_report: list[dict[str, Any]] = []
    skipped_report: list[dict[str, str]] = list(resolution.skipped)
    uploaded_files: list[str] = []
    runtime_blockers: list[StepBlocker] = list(resolution.blockers)
    mutated_choice_control = False

    if not resolution.mutations:
        return filled_report, skipped_report, uploaded_files, runtime_blockers, False

    for mut in resolution.mutations:
        session.validate_bound_page(application_id, job_url=job_url)
        if session.get_navigation_generation(page) != snapshot.navigation_gen:
            raise StepChangedError(
                written_count=len(filled_report),
                message="A página navegou ou mudou de etapa durante o preenchimento.",
            )

        frame = _resolve_target_frame(page, snapshot)
        fresh = await inspect_step(page, navigation_gen=snapshot.navigation_gen)
        if fresh.structural_fingerprint != snapshot.structural_fingerprint:
            fresh_sigs = {f.question_signature for f in fresh.fields}
            expected_sigs = {f.question_signature for f in snapshot.fields}
            if (
                not mutated_choice_control
                or mut.field.question_signature not in fresh_sigs
                or not expected_sigs.issubset(fresh_sigs)
            ):
                raise StepChangedError(
                    written_count=len(filled_report),
                    message="O formulário da etapa foi alterado no navegador durante o preenchimento.",
                )

        if mut.kind == "resume_upload":
            if resume_pdf_path is None or not resume_pdf_path.is_file():
                runtime_blockers.append(
                    StepBlocker(
                        code="upload_failed",
                        field_id=mut.field.field_id,
                        label=mut.field.label,
                        message="Arquivo PDF do currículo indisponível para envio.",
                    )
                )
                continue
            locator = await _resolve_unique_control_locator(frame, snapshot, mut.field)
            if locator is None:
                _record_ambiguous(mut.field, skipped_report, runtime_blockers)
                continue
            if await _control_already_populated(locator, mut.field):
                skipped_report.append(
                    {"field_id": mut.field.field_id, "label": mut.field.label, "reason": "already_populated"}
                )
                continue
            await locator.set_input_files(str(resume_pdf_path))
            uploaded_name = await locator.evaluate(
                "el => (el.files && el.files.length > 0) ? el.files[0].name : ''"
            )
            if uploaded_name != resume_pdf_path.name:
                runtime_blockers.append(
                    StepBlocker(
                        code="upload_failed",
                        field_id=mut.field.field_id,
                        label=mut.field.label,
                        message=f"Falha ao confirmar o anexo do PDF ({resume_pdf_path.name}) no portal.",
                    )
                )
                continue
            uploaded_files.append(resume_pdf_path.name)
            filled_report.append(
                {
                    "field_id": mut.field.field_id,
                    "label": mut.field.label,
                    "kind": "resume_upload",
                    "suggested": False,
                }
            )
            continue

        locator = await _resolve_unique_control_locator(frame, snapshot, mut.field)
        if locator is None:
            _record_ambiguous(mut.field, skipped_report, runtime_blockers)
            continue
        if await _control_already_populated(locator, mut.field):
            skipped_report.append(
                {"field_id": mut.field.field_id, "label": mut.field.label, "reason": "already_populated"}
            )
            continue

        if mut.field.category == "salary" and (
            fresh.structural_fingerprint != snapshot.structural_fingerprint
            or session.get_navigation_generation(page) != snapshot.navigation_gen
        ):
            raise StepChangedError(
                written_count=len(filled_report),
                message="O formulário mudou antes do preenchimento salarial.",
            )

        if mut.field.control_kind == "select":
            opt = next((o for o in mut.field.options if o.option_id == mut.option_id), None)
            if opt is None:
                continue
            if opt.value:
                await locator.select_option(value=opt.value, force=True)
            else:
                await locator.select_option(label=opt.label, force=True)
            selected_val = await locator.evaluate(
                """el => {
                    if (window.jQuery && el.classList.contains('select2-hidden-accessible')) {
                        try { window.jQuery(el).trigger('change'); } catch (e) {}
                    }
                    return (el.options && el.selectedIndex >= 0) ? (el.options[el.selectedIndex].value || el.options[el.selectedIndex].text || '') : '';
                }"""
            )
            if mut.field.category == "salary" and selected_val != opt.value:
                runtime_blockers.append(StepBlocker(
                    code="salary_review_required", field_id=mut.field.field_id, label=mut.field.label,
                    message="Confira a opção salarial no Chrome antes de continuar.",
                ))
                continue
            if not selected_val.strip():
                skipped_report.append(
                    {"field_id": mut.field.field_id, "label": mut.field.label, "reason": "select_readback_empty"}
                )
                continue
            mutated_choice_control = True
        elif mut.field.control_kind == "radio":
            opt = next((o for o in mut.field.options if o.option_id == mut.option_id), None)
            if opt is None:
                continue
            matched = False
            if mut.field.locator_hint.get("tag") == "aria_radiogroup":
                opts_loc = locator.locator('[role="radio"]')
                opt_idx = int(opt.option_id.split("-")[-1])
                if 0 <= opt_idx < await opts_loc.count():
                    target_opt = opts_loc.nth(opt_idx)
                    await target_opt.click()
                    matched = (await target_opt.get_attribute("aria-checked")) == "true"
            else:
                target_radio = locator.filter(has_not=frame.locator(":disabled"))
                rcount = await target_radio.count()
                for idx in range(rcount):
                    candidate = target_radio.nth(idx)
                    cval = (await candidate.get_attribute("value")) or ""
                    if cval == opt.value or idx == int(opt.option_id.split("-")[-1]):
                        await candidate.check()
                        if await candidate.is_checked():
                            matched = True
                        break
            if not matched:
                skipped_report.append(
                    {"field_id": mut.field.field_id, "label": mut.field.label, "reason": "radio_readback_failed"}
                )
                continue
            mutated_choice_control = True
        elif mut.field.control_kind == "checkbox":
            if mut.field.category == "legal_consent" or mut.value != "true":
                continue
            await locator.check()
            if not await locator.is_checked():
                continue
            mutated_choice_control = True
        else:
            if not await locator.evaluate(
                    """(el, value) => {
                        const probe = el.cloneNode();
                        probe.value = value;
                        const max = probe.maxLength;
                        const min = probe.minLength;
                        return probe.value === value && probe.validity.valid
                            && (max === undefined || max < 0 || value.length <= max)
                            && (min === undefined || min < 0 || value.length >= min);
                    }""",
                    mut.value,
                ):
                runtime_blockers.append(StepBlocker(
                    code="salary_approval_required" if mut.field.category == "salary" else "invalid_field_value",
                    field_id=mut.field.field_id,
                    label=mut.field.label,
                    message="O valor não atende às exigências atuais do campo. Revise no Chrome.",
                ))
                continue
            if mut.field.category == "salary" and session.get_navigation_generation(page) != snapshot.navigation_gen:
                raise StepChangedError(
                    written_count=len(filled_report),
                    message="A página navegou antes do preenchimento salarial.",
                )
            await locator.fill(mut.value)
            readback = await locator.input_value()
            target_phone_digits = re.sub(r"\D", "", mut.value)
            is_phone_field = mut.field.category == "contact_phone" or mut.field.control_kind == "tel"
            phone_digits_match = (
                is_phone_field
                and re.sub(r"\D", "", readback or "") == target_phone_digits
                and bool(target_phone_digits)
            )
            if readback != mut.value and not phone_digits_match and is_phone_field and target_phone_digits:
                e164_val = f"+{target_phone_digits}" if mut.value.strip().startswith("+") or len(target_phone_digits) >= 12 else f"+55{target_phone_digits}"
                country_iso = "BR" if e164_val.startswith("+55") else ("US" if e164_val.startswith("+1") else "")
                with suppress(Exception):
                    await locator.evaluate(
                        """(el, payload) => {
                            const fk = Object.keys(el).find(k => k.startsWith("__reactFiber"));
                            let cur = fk ? el[fk] : null;
                            while (cur) {
                                const mp = cur.memoizedProps || {};
                                if (typeof mp.onCountryChange === "function" && typeof mp.onChange === "function") {
                                    if (payload.country) mp.onCountryChange(payload.country);
                                    mp.onChange(payload.e164);
                                    break;
                                }
                                cur = cur.return;
                            }
                        }""",
                        {"e164": e164_val, "country": country_iso},
                    )
                    readback = await locator.input_value()
                    phone_digits_match = re.sub(r"\D", "", readback or "") == re.sub(r"\D", "", e164_val)
            if readback != mut.value and not phone_digits_match:
                runtime_blockers.append(
                    StepBlocker(
                        code="salary_review_required" if mut.field.category == "salary" else "readback_mismatch",
                        field_id=mut.field.field_id,
                        label=mut.field.label,
                        message=(
                            "Confira o valor salarial no Chrome antes de continuar."
                            if mut.field.category == "salary"
                            else f"Valor preenchido em '{mut.field.label}' divergiu na releitura do navegador; revise no Chrome."
                        ),
                    )
                )
                continue
            if not bool(
                await locator.evaluate(
                    "el => typeof el.checkValidity === 'function' ? el.checkValidity() : true"
                )
            ):
                runtime_blockers.append(
                    StepBlocker(
                        code="salary_review_required" if mut.field.category == "salary" else "invalid_field_value",
                        field_id=mut.field.field_id,
                        label=mut.field.label,
                        message="O navegador recusou o valor na validação nativa (`checkValidity`). Revise no Chrome.",
                    )
                )
                continue
            if not (readback or "").strip():
                skipped_report.append(
                    {"field_id": mut.field.field_id, "label": mut.field.label, "reason": "fill_readback_empty"}
                )
                continue

        filled_report.append(
            {
                "field_id": mut.field.field_id,
                "label": mut.field.label,
                "kind": mut.kind,
                "suggested": mut.suggested,
            }
        )
        await asyncio.sleep(0.05)

    with suppress(Exception):
        await asyncio.sleep(0.12)

    return filled_report, skipped_report, uploaded_files, runtime_blockers, mutated_choice_control


async def run_step_fill_pass(
    page: Page,
    session: BrowserSession,
    application_id: str,
    *,
    doc: MasterProfileDocument,
    answers_json: dict[str, str],
    resume_pdf_path: Path | None,
    job_url: str | None = None,
    job_title: str = "",
    job_company: str = "",
    job_description: str = "",
    llm_gateway: LlmGateway | None = None,
    db: Session | None = None,
    remaining_budget_usd: float | None = None,
    max_dependent_passes: int = 1,
    claim_salary: Callable[[StepSnapshot], dict[str, str]] | None = None,
    claim_pending: Callable[[StepSnapshot], tuple[dict[str, str], dict[str, str]]] | None = None,
    load_arsenal: Callable[[StepSnapshot], Awaitable[dict[str, tuple[str, ...]]]] | None = None,
    load_arsenal_candidates: Callable[[StepSnapshot], Awaitable[Mapping[str, Any]]] | None = None,
    expected_locale: Locale = "pt-BR",
    allow_test_origins: set[str] | None = None,
    slot_values: Mapping[str, str] | None = None,
) -> StepRunResult:
    """Inspect current step, resolve deterministically, execute fill, and allow 1 same-step reveal re-check."""
    nav_gen = session.get_navigation_generation(page)
    snapshot = await inspect_step(
        page,
        navigation_gen=nav_gen,
        expected_job_title=job_title,
        expected_company=job_company,
    )
    approved_salary: dict[str, str] | None = None
    approved_actions: dict[str, str] | None = None
    if claim_pending is not None:
        approved_salary, approved_actions = claim_pending(snapshot)
    elif claim_salary is not None:
        approved_salary = claim_salary(snapshot)

    arsenal_candidates_map = (
        await load_arsenal_candidates(snapshot) if load_arsenal_candidates is not None else None
    )
    arsenal_answers_map = (
        await load_arsenal(snapshot)
        if load_arsenal is not None and arsenal_candidates_map is None
        else None
    )

    resolution = await build_field_plan(
        snapshot,
        approved_salary=approved_salary,
        approved_actions=approved_actions,
        arsenal_answers=arsenal_answers_map,
        arsenal_candidates=arsenal_candidates_map,
        doc=doc,
        answers_json=answers_json,
        resume_pdf_path=resume_pdf_path,
        job_title=job_title,
        job_company=job_company,
        job_description=job_description,
        llm_gateway=llm_gateway,
        db=db,
        remaining_budget_usd=remaining_budget_usd,
        expected_locale=expected_locale,
        allow_test_origins=allow_test_origins,
        slot_values=slot_values,
    )
    usage_events: list[dict[str, Any]] = list(resolution.llm_usage_events)
    filled, skipped, uploaded, blockers, mutated_choice = await fill_current_step(
        page,
        session,
        application_id,
        snapshot,
        resolution,
        job_url=job_url,
        resume_pdf_path=resume_pdf_path,
    )
    notices = list(resolution.notices)
    if resolution.llm_fallback_note:
        notices.append(f"Aviso do modelo (preenchimento determinístico mantido): {resolution.llm_fallback_note}")

    final_snapshot = snapshot
    # Allow at most one same-step dependent field pass without navigating/advancing step
    if (
        max_dependent_passes > 0
        and mutated_choice
        and not page.is_closed()
        and session.get_navigation_generation(page) == nav_gen
    ):
        fresh = await inspect_step(
            page,
            navigation_gen=nav_gen,
            expected_job_title=job_title,
            expected_company=job_company,
        )
        old_sigs = {f.question_signature for f in snapshot.fields}
        new_fields = [f for f in fresh.fields if f.question_signature not in old_sigs]
        if (
            fresh.structural_fingerprint != snapshot.structural_fingerprint
            and fresh.page_url == snapshot.page_url
            and old_sigs.issubset({f.question_signature for f in fresh.fields})
            and new_fields
        ):
            arsenal_cands2 = (
                await load_arsenal_candidates(fresh) if load_arsenal_candidates is not None else None
            )
            arsenal_ans2 = (
                await load_arsenal(fresh)
                if load_arsenal is not None and arsenal_cands2 is None
                else None
            )
            res2 = await build_field_plan(
                fresh,
                approved_salary=approved_salary,
                approved_actions=approved_actions,
                arsenal_answers=arsenal_ans2,
                arsenal_candidates=arsenal_cands2,
                doc=doc,
                answers_json=answers_json,
                resume_pdf_path=resume_pdf_path,
                job_title=job_title,
                job_company=job_company,
                job_description=job_description,
                llm_gateway=llm_gateway,
                db=db,
                remaining_budget_usd=remaining_budget_usd,
                expected_locale=expected_locale,
                allow_test_origins=allow_test_origins,
                slot_values=slot_values,
            )
            usage_events.extend(res2.llm_usage_events)
            f2, s2, u2, b2, _ = await fill_current_step(
                page,
                session,
                application_id,
                fresh,
                res2,
                job_url=job_url,
                resume_pdf_path=resume_pdf_path,
            )
            seen_ids = {item["field_id"] for item in filled}
            for item in f2:
                if item["field_id"] not in seen_ids:
                    filled.append(item)
            for up in u2:
                if up not in uploaded:
                    uploaded.append(up)
            blockers = b2
            skipped = s2
            final_snapshot = fresh

    write_outcomes: dict[str, str] = {}
    for item in skipped:
        fid = str(item.get("field_id") or "")
        if fid:
            write_outcomes[fid] = str(item.get("reason") or "skipped")
    for blk in blockers:
        if blk.field_id:
            write_outcomes[blk.field_id] = blk.code
    for item in filled:
        fid = str(item.get("field_id") or "")
        if fid:
            write_outcomes[fid] = "applied"

    session.store_step_snapshot(application_id, final_snapshot)
    return StepRunResult(
        snapshot=final_snapshot,
        filled=filled,
        skipped=skipped,
        uploaded_files=uploaded,
        blockers=blockers,
        notices=notices,
        llm_usage_events=tuple(usage_events),
        write_outcomes=write_outcomes,
    )


class BrowserDriver:
    """Facade wrapping BrowserSession step inspection, resolution, and single-step bounded fill."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
