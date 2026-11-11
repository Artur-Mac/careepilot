# Segurança e aprovação

Fluxo: fonte/CV/email não confiável -> parser limitado -> vaga/fatos verificados -> PDF/respostas imutáveis -> revisão autenticada -> aprovação -> driver restrito -> recibo/conciliação. Conteúdo externo nunca tem autoridade sobre políticas ou ferramentas.

| Fronteira | Ameaça | Controle |
|---|---|---|
| Fonte/parser | SSRF, HTML ativo, arquivo hostil | limites, timeout, parser isolado sem macros/exec, escape de HTML |
| Conteúdo/LLM | injeção e alegações falsas | saída estruturada com fact_ids verificados; rejeitar alegação sem fonte; sem shell/tools no LLM |
| UI/servidor | CSRF, rebinding, acesso indevido | loopback, Host/Origin estritos, sessão, CSRF, sem CORS wildcard |
| Pacote/browser | replay, adulteração, TOCTOU, duplicatas | hashes dos bytes, nonce, prazo, revisão e claim transacional |
| Browser/rede | exfiltração/redirecionamento | egress deny-by-default por adapter; contexto efêmero |
| Dados/logs | vazamento | minimização, permissões, redação e retenção |

## Gate

Exibir vaga, empregador/destino, PDF exato, respostas, modo e validade antes de aprovar. Servidor verifica user_id, canonical_job_id, employer_id, snapshot, URL/política, revisão, hashes dos bytes, estado, prazo e nonce não revogado/consumido. Não confiar no cliente para declarar hashes corretos. Aprovação vincula pacote específico e não autoriza outros uploads.

Validar ANTES de preencher/anexar: preparação já pode divulgar dados ao site. Repetir gate antes do submit; claim atômico consome nonce e registra submitted_unverified. Dois executores resultam em um claim. Crash/timeout após claim bloqueia retry até conciliação com recibo oficial. Exactly-once externo não é garantido. Revogação/rejeição antes do claim impede envio; após claim mostrar incerteza. Campos/perguntas/destino alterados exigem nova revisão se materialmente diferentes.

## Browser e SSRF

Produção exige HTTPS, esquema/host/porta permitidos e sem userinfo. Validar IDNA e todos A/AAAA; negar IPv4/IPv6 privados, loopback, link-local, metadata, multicast/reservados. Aplicar em cada redirect, iframe, subrecurso, fetch/websocket. Impedir DNS rebinding na conexão com proxy de egress/isolamento de rede, não apenas uma checagem anterior de DNS. Desativar service workers e downloads não solicitados. Política sem wildcard de domínios, incluindo destinos de upload.

Fixture: allowlist literal de host/porta do ATS fictício e egress externo negado; nunca localhost genérico/dashboard. Modo fixture não pode habilitar live externo. Contexto descartável, sem perfil pessoal/cookies/extensões/credenciais. Worker sem shell nem pastas do usuário, com somente o PDF exato; upload resolve artifact_id no servidor e revalida bytes/hash. CAPTCHA, login inesperado ou pergunta legal desconhecida pausam; sem evasão antibot.

## Sessão e dados

Bootstrap: segredo aleatório de uso único exibido no terminal e trocado por sessão, nunca URL/log. Cookies HttpOnly SameSite=Strict, expiração, rotação/logout; Secure em HTTPS. HTTP somente loopback explicitamente. Todas mutações com CSRF e Origin/Host; leituras privadas autenticadas e owner-scoped. Sem túnel/publicação automática; settings nunca retorna segredo. PDF/evidência não são diretório público.

MVP apenas dados sintéticos. Diretórios 0700, arquivos 0600; SQLite WAL não é criptografia. Antes de dados reais, definir criptografia de dados/backups e exclusão. Vault opcional futuro: AEAD de biblioteca mantida, chave em keyring do SO, nunca junto do ciphertext, liberação de campo mínimo por consentimento. Não coletar CPF/RG/endereço agora.

Logs: IDs/códigos/custos, nunca corpos/CV/respostas/cookies/tokens/URLs secretas. Traces/screenshots desligados fora de teste por padrão; evidência redigida e protegida. Retenção proposta: logs/evidência 7 dias, documentos até exclusão solicitada; remover temporários ao finalizar. Exclusão inclui política de backups e não promete apagamento físico instantâneo.

## Email e OAuth futuros

Aprovação por reply adiada. Notificação futura sem CV/dados sensíveis/token de aprovação; link para painel privado autenticado. Usuário fora do PC aguarda ou configura acesso privado autenticado, sem expor localhost publicamente.

OAuth futuro: Authorization Code + PKCE, state, escopos mínimos, refresh token no keyring, revogação; conferir documentação atual antes de implementar. Reply exigiria desafio autenticado por pacote/usuário, nonce curto de uso único e prazo. From, DKIM, Message-ID ou texto SIM isolados não bastam. Preferir confirmação final no painel. Nenhuma credencial exigida no MVP.
