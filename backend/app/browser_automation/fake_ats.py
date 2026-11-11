"""Fictional local employer ATS test web app (loopback only, unambiguously marked TEST DATA)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

fake_ats_app = FastAPI(title="CareerPilot Fictional Local Employer ATS [TEST DATA]")

_RECEIPTS: dict[str, dict[str, Any]] = {}

_BASE_CSS = """
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 720px; margin: 24px auto; padding: 0 16px; background: #f8fafc; color: #0f172a; }
  .banner { background: #fef3c7; border: 2px solid #d97706; color: #92400e; padding: 10px 14px; border-radius: 6px; font-weight: 700; margin-bottom: 18px; }
  .card { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  label { display: block; font-weight: 600; margin-top: 12px; margin-bottom: 4px; }
  input[type=text], input[type=email], input[type=tel], input[type=url], select, textarea { width: 100%; padding: 8px 10px; border: 1px solid #94a3b8; border-radius: 5px; box-sizing: border-box; }
  .row { margin-top: 10px; }
  button { margin-top: 16px; padding: 10px 18px; background: #1d4ed8; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; }
  .receipt-box { background: #ecfdf5; border: 2px solid #059669; padding: 16px; border-radius: 8px; }
  .captcha-box { background: #fef2f2; border: 2px dashed #dc2626; padding: 16px; border-radius: 8px; margin-top: 14px; }
</style>
"""


@fake_ats_app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "TEST_DATA_FICTIONAL_ATS"}


@fake_ats_app.get("/jobs/single-step", response_class=HTMLResponse)
def single_step_form() -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>Candidatura ATS Fictício [DADOS DE TESTE]</title>{_BASE_CSS}</head>
<body>
  <div class="banner" data-testid="test-data-banner">DADOS DE TESTE — ATS FICTÍCIO LOCAL (APENAS DEMONSTRAÇÃO)</div>
  <div class="card">
    <h1>Empresa Fictícia Horizonte Dados — Engenheira(o) de Dados Júnior</h1>
    <form id="ats-form" action="/jobs/single-step/submit" method="post" enctype="multipart/form-data">
      <label for="full_name">Nome completo *</label>
      <input id="full_name" name="full_name" type="text" required />

      <label for="email">E-mail de contato *</label>
      <input id="email" name="email" type="email" required />

      <label for="phone">Telefone *</label>
      <input id="phone" name="phone" type="tel" required />

      <label for="location">Cidade / Estado *</label>
      <input id="location" name="location" type="text" required />

      <label for="linkedin_url">Perfil LinkedIn</label>
      <input id="linkedin_url" name="linkedin_url" type="url" />

      <label for="seniority_level">Nível pretendido *</label>
      <select id="seniority_level" name="seniority_level" required>
        <option value="">Selecione...</option>
        <option value="junior">Júnior / Entry-level</option>
        <option value="trainee">Trainee</option>
        <option value="intern">Estágio</option>
      </select>

      <div class="row" role="radiogroup" aria-label="Autorização de trabalho no Brasil">
        <span style="font-weight:600; display:block;">Possui autorização para trabalhar no Brasil? *</span>
        <label style="display:inline-block; font-weight:400; margin-right:16px;">
          <input type="radio" name="work_authorization" value="sim" required /> Sim
        </label>
        <label style="display:inline-block; font-weight:400;">
          <input type="radio" name="work_authorization" value="nao" /> Não
        </label>
      </div>

      <label for="dyn_why" id="dyn-label-why">Por que você se interessa por esta vaga?</label>
      <textarea id="dyn_why" name="why_role" rows="3" aria-labelledby="dyn-label-why"></textarea>

      <label for="optional_note">Pergunta opcional desconhecida: código de indicação (opcional)</label>
      <input id="optional_note" name="optional_referral_note" type="text" placeholder="Opcional — deixar vazio se não souber" />

      <label for="resume_pdf">Currículo em PDF (obrigatório) *</label>
      <input id="resume_pdf" name="resume_pdf" type="file" accept=".pdf,application/pdf" required />

      <label style="font-weight:500;">
        <input id="consent_privacy" name="consent_privacy" type="checkbox" value="sim" required />
        Concordo com o tratamento de dados deste processo seletivo fictício de teste *
      </label>

      <button type="submit" data-testid="submit-application">Enviar Candidatura de Teste</button>
    </form>
  </div>
  <script>
    // Dynamic label simulation to ensure Playwright locates reliably by semantic role/label.
    setTimeout(() => {{
      const el = document.getElementById("dyn-label-why");
      if (el) el.textContent = "Motivação e resumo técnico para a vaga (rótulo dinâmico)";
    }}, 40);
  </script>
</body>
</html>"""


@fake_ats_app.post("/jobs/single-step/submit", response_class=HTMLResponse)
async def single_step_submit(
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    location: str = Form(...),
    linkedin_url: str = Form(""),
    seniority_level: str = Form(...),
    work_authorization: str = Form(...),
    why_role: str = Form(""),
    optional_referral_note: str = Form(""),
    consent_privacy: str = Form(...),
    resume_pdf: UploadFile = File(...),
) -> HTMLResponse:
    pdf_bytes = await resume_pdf.read()
    if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) < 100:
        return HTMLResponse(
            f"<html><body><h1>Erro</h1><p>Arquivo enviado não é um PDF válido ({resume_pdf.filename}).</p></body></html>",
            status_code=400,
        )
    receipt_id = f"REC-{secrets.token_hex(6).upper()}"
    _RECEIPTS[receipt_id] = {
        "full_name": full_name,
        "email": email,
        "filename": resume_pdf.filename,
        "size_bytes": len(pdf_bytes),
        "seniority_level": seniority_level,
        "work_authorization": work_authorization,
        "consent_privacy": consent_privacy,
    }
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>Confirmação [DADOS DE TESTE]</title>{_BASE_CSS}</head>
<body>
  <div class="banner">DADOS DE TESTE — CONFIRMAÇÃO SIMULADA LOCAL</div>
  <div class="receipt-box" data-testid="submission-confirmation">
    <h1>Candidatura Recebida com Sucesso (Ambiente de Teste)</h1>
    <p>Candidato(a): <strong>{full_name}</strong> ({email})</p>
    <p>Arquivo PDF anexado: <strong>{resume_pdf.filename}</strong> ({len(pdf_bytes)} bytes)</p>
    <p>Protocolo / Comprovante: <strong id="receipt-id" data-testid="receipt-id">{receipt_id}</strong></p>
  </div>
</body>
</html>"""
    )


@fake_ats_app.get("/jobs/multi-step", response_class=HTMLResponse)
def multi_step_form() -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>ATS Multi-Etapas [DADOS DE TESTE]</title>{_BASE_CSS}</head>
<body>
  <div class="banner">DADOS DE TESTE — ATS FICTÍCIO MULTI-ETAPAS</div>
  <div class="card">
    <h1>Laboratório Fictício Atlântico — Analytics Engineer Júnior</h1>
    <form id="multi-form" action="/jobs/multi-step/submit" method="post" enctype="multipart/form-data">
      <div id="step-1" data-step="1">
        <h2>Etapa 1 de 2 — Identificação</h2>
        <label for="ms_name">Nome completo *</label>
        <input id="ms_name" name="full_name" type="text" required />

        <label for="ms_email">E-mail *</label>
        <input id="ms_email" name="email" type="email" required />

        <label for="ms_phone">Telefone *</label>
        <input id="ms_phone" name="phone" type="tel" required />

        <label for="ms_loc">Localização *</label>
        <input id="ms_loc" name="location" type="text" required />

        <button type="button" id="next-step-btn" data-testid="next-step" onclick="goStep2()">Próxima Etapa</button>
      </div>
      <div id="step-2" data-step="2" style="display:none;">
        <h2>Etapa 2 de 2 — Perguntas Obrigatórias e Currículo</h2>
        <label for="ms_salary">Pretensão salarial (R$) *</label>
        <input id="ms_salary" name="salary_expectation" type="text" required />

        <label for="ms_why">Motivação *</label>
        <textarea id="ms_why" name="why_role" rows="3" required></textarea>

        <label for="ms_pdf">Anexar Currículo PDF *</label>
        <input id="ms_pdf" name="resume_pdf" type="file" accept=".pdf,application/pdf" required />

        <label style="font-weight:500;">
          <input id="ms_consent" name="consent_privacy" type="checkbox" value="sim" required />
          Confirmo veracidade das informações e consentimento de teste *
        </label>

        <button type="submit" data-testid="submit-application">Confirmar Envio Final</button>
      </div>
    </form>
  </div>
  <script>
    function goStep2() {{
      document.getElementById('step-1').style.display = 'none';
      document.getElementById('step-2').style.display = 'block';
    }}
  </script>
</body>
</html>"""


@fake_ats_app.post("/jobs/multi-step/submit", response_class=HTMLResponse)
async def multi_step_submit(
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    location: str = Form(...),
    salary_expectation: str = Form(...),
    why_role: str = Form(""),
    consent_privacy: str = Form(...),
    resume_pdf: UploadFile = File(...),
) -> HTMLResponse:
    pdf_bytes = await resume_pdf.read()
    if not pdf_bytes.startswith(b"%PDF-"):
        return HTMLResponse("PDF inválido", status_code=400)
    receipt_id = f"REC-MS-{secrets.token_hex(5).upper()}"
    _RECEIPTS[receipt_id] = {
        "full_name": full_name,
        "salary_expectation": salary_expectation,
        "filename": resume_pdf.filename,
    }
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>Confirmação Multi-Etapas [TESTE]</title>{_BASE_CSS}</head>
<body>
  <div class="banner">DADOS DE TESTE — CONFIRMAÇÃO MULTI-ETAPAS</div>
  <div class="receipt-box" data-testid="submission-confirmation">
    <h1>Candidatura Multi-Etapas Confirmada</h1>
    <p>Protocolo: <strong id="receipt-id" data-testid="receipt-id">{receipt_id}</strong></p>
  </div>
</body>
</html>"""
    )


@fake_ats_app.get("/jobs/captcha-gate", response_class=HTMLResponse)
def captcha_gate_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>Verificação de Segurança [TESTE]</title>{_BASE_CSS}</head>
<body>
  <div class="banner">DADOS DE TESTE — SIMULAÇÃO DE BLOQUEIO CAPTCHA</div>
  <div class="card">
    <h1>Banco Fictício Seguro — Backend Python Júnior</h1>
    <div class="captcha-box" data-captcha-blocker="true">
      <strong>Verificação humana obrigatória (CAPTCHA simulado)</strong>
      <p>Verify you are human before continuing. Automação deve pausar e solicitar ação humana.</p>
    </div>
    <form action="/jobs/single-step/submit" method="post">
      <label for="full_name">Nome completo</label>
      <input id="full_name" name="full_name" type="text" />
      <button type="submit" data-testid="submit-application">Enviar</button>
    </form>
  </div>
</body>
</html>"""


@fake_ats_app.get("/jobs/manual-demo", response_class=HTMLResponse)
def manual_demo_page() -> str:
    return single_step_form()


@fake_ats_app.get("/jobs/redirect-offdomain")
def redirect_offdomain() -> RedirectResponse:
    return RedirectResponse(url="https://example.com/", status_code=302)


@fake_ats_app.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: str) -> dict[str, Any]:
    if receipt_id not in _RECEIPTS:
        return {"found": False}
    return {"found": True, "receipt_id": receipt_id, **_RECEIPTS[receipt_id]}
