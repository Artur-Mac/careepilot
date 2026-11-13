# Segurança — CareerPilot (Copiloto CDP por Etapa)

Fluxo confiável: `MasterProfile` local -> geração de PDF aterrada a IDs de evidência -> inspeção sanitizada do DOM da etapa atual -> mutações restritas com proteção anti-stale/anti-injeção -> revisão e envio exclusivamente humanos no Chrome.

| Fronteira | Ameaça | Controle implementado |
| --- | --- | --- |
| Painel local | CSRF, DNS rebinding, acesso não autenticado | Binding obrigatório em loopback (`127.0.0.1`/`localhost`/`::1`), cookie `HttpOnly` + `SameSite=Strict`, CSRF + validação de `Origin`, zero CORS wildcard |
| Conexão CDP | Acesso a CDP remoto não confiável | `validate_cdp_url` restringe `BROWSER_CDP_URL` a loopback; `tab_id` opaco efêmero em memória no `/pages` e `/attach-page` (sem expor handles/tokens ou query/fragmento de URLs) |
| Conteúdo externo / LLM | Prompt injection em formulários ou vagas; invenção de dados | Texto não confiável passa por `sanitize_untrusted_text` (ocultos removidos no DOM); LLM restrito a JSON estruturado (`FieldPlan`) com verificação obrigatória de `evidence_id` no `MasterProfile` e bloqueio de troca de categoria factual; sem shell/ferramentas no LLM |
| Preenchimento no Chrome | Envio acidental, clique em termos, coleta de CPF/OTP, corrida em etapa alterada | Nunca clica em `Avançar`/`Continuar`/`Enviar`; recusa `legal_consent` (`consent_required`), `sensitive_id` (`sensitive_manual`), `captcha`, `login_required`, `otp_required`, `unknown_origin` (iframe cross-origin) e uploads não-currículo (`upload_manual_required`); bloqueia controles ambíguos; valida `navigation_gen`, origem compatível e `structural_fingerprint` antes de escrever (`409 step_changed`); lock assíncrono por candidatura (`409 operation_in_progress`) |
| LLM remoto opcional | Vazamento involuntário do perfil completo | `llm_allow_remote_profile=false` por padrão impede envio do documento completo do `MasterProfile` a endpoints não-loopback |
| Descoberta e URLs | SSRF, redirecionamentos internos/privados | `validate_external_url` nega IPs privados, loopback, link-local, metadata e multicast em URLs externas (HTTP de loopback permitido só em `allow_test_origins` de testes) |
| Artefatos e dados | Escrita fora do diretório ou retenção de segredos | Permissões restritas (`0700`/`0600`), download de PDF por `resume_id` autenticado, limpeza de temporários órfãos no startup |

## Invariantes não negociáveis

1. **Human-in-the-loop na submissão e nos termos**: a automação prepara o PDF e preenche a etapa atual visível; avançar etapas, marcar aceites legais/LGPD, responder documentos sensíveis (CPF/RG/2FA) e confirmar envio acontecem somente por ação humana no Chrome.
2. **Sem bypass antibot**: diante de CAPTCHA, autenticação ou desafio de verificação, a execução pausa imediatamente e reporta o bloqueio ao usuário.
3. **Respeito ao que já foi preenchido pelo usuário**: campos visíveis já preenchidos no formulário atual não são sobrescritos.
4. **Factualidade estrita**: respostas sugeridas pelo LLM sem `evidence_id` válido do `MasterProfile` são descartadas antes de tocar no navegador.
