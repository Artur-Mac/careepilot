# Integração e handoff

Estado: arquitetura commitada. Na verificação final surgiram arquivos não rastreados no worktree Gemini (.env.example, .gitignore, pyproject.toml e uv.lock), indicando possível atividade externa. Não foram editados ou revisados; implementação e testes ainda não avaliados. Nenhuma sessão Gemini foi iniciada por esta sessão. Segundo prompt não fornecido aqui.

Sol: ../careerpilot-sol, sol-architecture, exclusivamente architecture/**. Gemini: ../careerpilot-gemini, gemini-implementation, app/config/fixtures/tests/docs/README. Mesmo baseline Initial project. Branches são independentes; objetos e histórico-base são compartilhados pelo Git.

## Passos exatos após Gemini terminar

1. Confirmar conclusão e commits; inspecionar status/log dos dois worktrees. Branch existente não comprova trabalho concluído. Não mesclar enquanto outro agente edita.
2. A partir de TxtBooks, com os dois trabalhos commitados e limpos:

```bash
git -C careerpilot worktree add -b integration-review ../careerpilot-integration sol-architecture
git -C careerpilot-integration merge --no-ff gemini-implementation
```

Se integration-review já existir, inspecionar antes de reutilizar; não forçar. Resolver conflitos apenas no novo worktree, sem reset/force-push/edição do Gemini.

3. Comparar schema completo, nulls, enums, adapters, rotas fixas/OpenAPI, estados, erros e identidade canônica. Acordar payloads propostos e rotas auxiliares de confirmação de fatos/visualização protegida antes de aceitar import real.
4. Revisar auth/CSRF/Host, dono, dry-run/live, pacote imutável, hash dos bytes, prazo/nonce/revogação, gate antes de upload e antes de envio, claim/idempotência e falha ambígua. Prioridade bloqueadora.
5. Verificar autorização de cada fonte/driver, rede/redirects/DNS, isolamento browser, parsing, factualidade, redação, retenção e licenças/versões do lockfile. Não inferir autorização por disponibilidade de API.
6. Instalar conforme lockfile em ambiente isolado sem credenciais reais; rodar comandos documentados e testes API/segurança/concorrência. Demonstrar E01 com PDF/recibo e E02 com contador zero no navegador real.
7. Fazer patches mínimos somente na integration-review. Reexecutar suite exigida e testes afetados. Registrar relatório sob architecture/reviews/** depois que existir código concreto.
8. Relatar commits, comandos, exit codes, evidências e riscos abertos. Não declarar execução a partir de código escrito. Não realizar envio real, publicação ou merge na principal automaticamente.

## Gate

- [ ] Contratos comparados com implementação e divergências resolvidas.
- [ ] P0/P1 executados; falhas bloqueiam entrega correspondente.
- [ ] Fluxo completo com upload e recibo fictício demonstrado.
- [ ] Negação, expiração, adulteração e concorrência impedem envio indevido.
- [ ] Sem retry cego/confirmacão falsa após resultado incerto.
- [ ] Relatório distingue proposta, implementação e verificação.
