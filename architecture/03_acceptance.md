# Aceitação — matriz a implementar (não executada)

Fixture: Empresa Exemplo/Pessoa Teste, pessoa@example.test, fatos explicitamente verificados, vagas estáveis, PDF real e ATS isolado com upload, contador de POSTs e recibo próprio. Sem internet ou credenciais obrigatórias. Clique não é confirmação.

| ID | Cenário | Resultado exigido |
|---|---|---|
| E01 P0 | Descobrir -> avaliar -> PDF -> revisar -> aprovar -> Playwright preenche -> anexa -> envia em modo fixture | ATS recebe bytes/hash exatos, contador=1, recibo sintético e verified. |
| E02 P0 | Rejeitar e chamar execute/submit diretamente | Zero upload/envio, rejected. |
| E03 P0 | Ausência de aprovação, prazo vencido, nonce revogado ou outro usuário (cada variante) | Zero envio, erro server-side. |
| E04 P0 | Alterar separadamente CV, respostas, vaga, empregador, usuário, destino, revisão e política | Pacote invalidado, zero envio. |
| E05 P0 | Duas execuções concorrentes e retries com mesma/outra chave | Um claim, no máximo um POST, mesma tentativa retornada. |
| E06 P0 | ATS recebe POST mas resposta cai; reiniciar executor | submitted_unverified, sem reenvio; só recibo conciliado promove verified. |
| E07 P0 | Clique/mensagem genérica/LLM diz enviado, sem recibo | confirmed=false, nunca verified. |
| E08 P0 | Domínio proibido ou fluxo restrito | Handoff manual, sem automação indevida. |
| E09 P0 | Pergunta legal desconhecida ou CAPTCHA | Pausa, sem resposta inventada/submit. |
| E10 P0 | Vaga/form/email ordena ignorar regras/ler arquivos/enviar CPF | Nenhuma ferramenta/arquivo/segredo extra ou autorização alterada. |
| E11 P0 | PDF com alegação sem fact_id; import com fato novo | Bloqueio até verificação; alegações aceitas rastreiam fonte/revisão. |
| E12 P0 | Sentinelas sensíveis em dados e captura de logs/erros/emails | Ausentes de logs/notificações, evidência protegida. |
| E13 P0 | Sessão ausente, CSRF, Origin/Host falsos, IDs de outro dono | 401/403/404, zero mutação/leitura privada. |
| E14 P0 | SSRF IPv4/IPv6, redirect, DNS rebinding, iframe/fetch metadata/dashboard | Bloqueio efetivo no egress; fixture permite somente ATS exato. |
| E15 P0 | Defaults dry-run/live; cliente tenta habilitar no execute | Zero submit e confirmed=false. |
| E16 P0 | Rejeitar/expirar durante preparação antes do claim final | Zero envio; depois do claim, UI admite incerteza sem prometer cancelamento. |
| E17 P1 | Fonte falha/429 junto de fonte saudável | Resultado parcial e backoff limitado. |
| E18 P1 | Nulls, salário inválido, URLs semelhantes de vagas distintas | Null preservado, inválido rejeitado, dedup sem perda de proveniência. |
| E19 P1 | Banco novo/anterior e workers concorrentes | Migração preserva dados; FKs/uniques e CAS efetivos. |
| E20 P1 | LLM desligado/limite diário atingido | Demo offline funciona, chamada excedente bloqueada, custo honesto. |
| E21 P1 | Handoff manual sem recibo ou transição fora da whitelist | Sem verified; transição inválida negada. |

Gemini deve documentar comandos exatos conforme layout/lockfile: pytest para serviços/API/concorrência e pytest+Playwright/Chromium real para E01/E02. Registrar comando, ambiente, exit code e relatório. P0 bloqueia release; P1 integra gate do MVP. Matriz escrita não significa testes aprovados. Só UI funcional não atende definição de pronto.
