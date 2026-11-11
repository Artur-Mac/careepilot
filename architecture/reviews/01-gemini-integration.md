# Revisão da integração Gemini

Branch integration-review, merge adc7d6d de b46f0bc (Gemini) e c022ea3 (Sol). Worktrees originais preservados. Escopo: demo local com dados sintéticos; sem submissão real ou validação operacional de provedores externos.

- Conferidos no código: rotas do MVP, estados principais, schema de vaga, uso único da aprovação e defaults dry-run/live. Os formatos HTTP e status do driver divergem das propostas em 01_contracts.md; não houve renomeação de API nesta revisão. A implementação oferece verificação de fatos e visualização de PDF por rotas auxiliares.
- Corrigido: destino, empresa, política, vaga e usuário do registro atual precisam corresponder ao pacote. Antes do clique, o serviço relê e revalida aprovação/prazo/hash e flags; persiste o início do envio com atualização condicionada à versão da candidatura. Se houver erro após esse ponto, mantém estado de envio não verificado e impede repetir a execução.
- Verificado: fluxo completo Chromium com descoberta, PDF, aprovação, preenchimento, upload e recibo do ATS local. Rejeição e expiração durante preenchimento impedem envio. Perda simulada de confirmação após recebimento deixa uma única submissão fictícia e bloqueia reexecução.
- Baseline: 35 testes passaram. Smoke da integração passou em lint, tipos e 37 testes, incluindo 5 testes de navegador; aviso de depreciação do TestClient da Starlette não bloqueou execução. Após adicionar a comparação do SHA-256 do PDF recebido com o aprovado, os 5 testes de navegador passaram novamente. O smoke agora instala explicitamente o extra de ferramentas de desenvolvimento do lockfile.

## Bloqueadores para dados reais ou automação externa

1. **Isolamento de rede:** interceptação atual do navegador valida navegações, mas não implementa confinamento efetivo de subrecursos, workers, redirects e DNS na conexão. Exigir egress independente, deny-by-default e validar ausência de tráfego fora da política.
2. **Prova e privacidade:** confirmação manual aceita referência informada pelo usuário como verificada; separar declaração de evidência verificada. Traces/screenshots e valores de formulário precisam de minimização, acesso e retenção antes de uso com dados reais.
3. **Ambiente e autorização:** demo usa credenciais locais conhecidas e liga fixture por configuração; separar fixture do modo live externo, substituir credenciais demo e verificar políticas de cada fonte. Nenhum canal externo autorizado por esta revisão.
4. **Contrato de aprovação:** a checagem final foi reforçada; faltam o vínculo explícito com revisão do perfil e ID do empregador, hash apresentado pelo cliente na confirmação, política versionada, revogação durante a preparação e dedup de candidatura por vaga/usuário além do uso único de cada aprovação. Os testes não cobrem toda a matriz de aceitação.
