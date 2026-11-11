# Arquitetura Futura: Adaptador Opcional WhatsApp Cloud API (`whatsapp-future.md`)

> **Status no MVP**: **Não implementado no MVP local** (apenas documentado como ponto de extensão segura). O canal ativo padrão do MVP é o `LocalOutboxAdapter` (registro local consultado pelo painel em `/api/notifications`), acompanhado do adaptador opcional `TelegramBotAdapter` em `backend/app/notifications/adapters.py`.

## 1. Objetivo de Negócio e Fluxo Human-in-the-Loop

Quando habilitado em versões futuras fora do ambiente 100% local, o adaptador de WhatsApp permitirá:
1. **Notificar** o usuário quando novas vagas com alta aderência forem descobertas ou quando um pacote imutável (`ApprovalRequest`) estiver pronto na fila `awaiting_approval`.
2. **Coletar aprovação explícita** com resumo seguro da vaga, *hash* SHA-256 resumido do currículo PDF gerado, respostas preparadas e link de revisão no painel local (ou resposta interativa assinada vinculada ao `nonce` do pacote).
3. **Responder a perguntas pontuais faltantes** (`QuestionRequest`, ex.: pretensão salarial ou consentimento específico) sem fabricar respostas automaticamente.

## 2. Requisitos Oficiais e Conformidade (WhatsApp Business Platform / Cloud API)

1. **Canal Oficial Exclusivo**: Utilizar apenas a **WhatsApp Cloud API** oficial da Meta (`graph.facebook.com`), **nunca** bibliotecas não oficiais de scraping do WhatsApp Web que violam termos de serviço ou expõem sessões locais.
2. **Regras de Janela de 24h e Templates**:
   - Mensagens iniciadas pela aplicação após mais de 24 horas sem interação do usuário exigem **Message Templates** previamente aprovados pela Meta (`utility` template em pt-BR, com variáveis restritas ao nome da empresa, título da vaga, código resumido do pacote e instrução de aprovação).
   - Dentro da janela de atendimento de 24h aberta pelo usuário, é possível enviar mensagens interativas com botões (`Aprovar`, `Recusar`, `Ver Detalhes no Painel`).
3. **Webhook HTTPS Público + Verificação de Assinatura**:
   - Exige um endpoint HTTPS dedicado com verificação de token de desafio (`GET /webhooks/whatsapp`) e validação obrigatória de assinatura HMAC-SHA256 (`X-Hub-Signature-256`) em todo payload `POST`.
   - O backend principal do CareerPilot permanece **estritamente em loopback (`127.0.0.1`)**. Caso o adaptador WhatsApp seja ativado no futuro, recomenda-se um *relay* mínimo isolado (ou túnel autenticado por mTLS/Cloudflare Tunnel dedicado apenas ao webhook assinado) que encaminhe eventos validados para o processo local.

## 3. Fronteiras de Segurança e Privacidade (Regras Obrigatórias)

| Controle | Política Obrigatória |
|---|---|
| **Credenciais & Tokens** | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET` e `WHATSAPP_PHONE_NUMBER_ID` armazenados no `.env` fora do Git / encriptados via Fernet; **nunca** exibidos em UI, `/api/settings` ou logs. |
| **Remetente Autorizado (Allowlist)** | Configurar `WHATSAPP_ALLOWED_PHONE_NUMBERS` (formato E.164). Mensagens de qualquer outro número são descartadas com auditoria mínima e **sem** efeito na máquina de estados. |
| **Vínculo Imutável de Aprovação** | O botão/comando "Aprovar" no WhatsApp DEVE ecoar e validar o identificador do `ApprovalRequest` + `packet_hash` (ou código curto derivado do `nonce`) e exigir confirmação explícita de uso único (`consumed = False`). Se qualquer arquivo (PDF, respostas ou descrição) mudar após a notificação, a execução é bloqueada (`blocked`). |
| **Proibição de Dados Sensíveis em Mensagens** | `vault.is_safe_for_notification()` impede inclusão de **CPF, RG, passaporte, endereço completo residencial ou segredos OAuth** em mensagens enviadas pela Cloud API. |
| **Sem Bypass de `DRY_RUN`** | Mesmo que um pacote seja aprovado via WhatsApp, a execução continua respeitando `DRY_RUN=true` / `live_mode_enabled` e `apply_mode` da vaga (`manual_only` nunca envia sozinho). |

## 4. Contrato Proposto (`NotificationAdapter`)

```python
class WhatsAppCloudAdapter(NotificationAdapter):
    channel = "whatsapp"

    def __init__(self, settings: Settings) -> None:
        # Exige WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ALLOWED_PHONES
        ...

    def send(self, db: Session, notification: OutgoingNotification) -> Notification:
        # 1. Sanitizar e aplicar redact_secrets(...) em subject e body
        # 2. Usar template aprovado se fora da janela de 24h
        # 3. Registrar apenas metadados seguros na tabela notifications
        ...
```

## 5. Passos Manuais Necessários Antes de Ativar

1. Criar conta Meta for Developers + WhatsApp Business App e obter `Phone Number ID` + Token de Sistema permanente.
2. Registrar e aprovar template `careerpilot_approval_alert` (categoria *Utility*, idioma `pt_BR`).
3. Provisionar endpoint HTTPS para webhooks e configurar `App Secret` para validar `X-Hub-Signature-256`.
4. Implementar rota `/api/webhooks/whatsapp` com proteção HMAC + allowlist de remetentes antes de habilitar envio real.
