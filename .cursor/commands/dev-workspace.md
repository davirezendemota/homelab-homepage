# /dev-workspace

Consumir o Dev Workspace **via API** usando a skill `.cursor/skills/dev-workspace/SKILL.md`.

## Regras

1. `source .dev-workspace/.env` — exige `DEV_WORKSPACE_URL` + `DEV_WORKSPACE_API_TOKEN`.
2. **Não** ler `workspace_data/` nem JSON de projetos com Read/Grep — só `curl` na API.
3. Resolver o project id deste repo quando o pedido for sobre **este** projeto (`GET /api/projects`, filtrar `local_path`).
4. Formatar respostas conforme a skill (checkpoints, milestones, plans, features, tasks, projects, pendências → diagrama ASCII; importar → gravar arquivo).
5. **Plano aprovado** pelo usuário → cadastrar no DW via `PUT /api/projects/{id}/plans` com **`id` único**; informar o `id` ao usuário (ver skill § Plans).
6. **Tasks:** somente `GET` — **nunca** `PUT /api/projects/{id}/tasks`.

Pedido: `$ARGUMENTS`

## Interpretação

| Pedido | Ação |
|--------|------|
| Vazio | Checkpoints deste repo (diagrama ASCII) |
| `milestones` / `roadmap` | Milestones (diagrama ASCII) |
| `plans` / `plano` / `P001` / `plan {id}` / `plan-milestone` | Planos de ação (diagrama ASCII; citar `PXXX` ou `id`) |
| `features` / `specs` | Features do checklist (diagrama ASCII) |
| `tasks` / `tarefas` | Tasks do projeto (diagrama ASCII) |
| `projects` / `projetos` | Projetos deste repo no DW (diagrama ASCII) |
| `pendências` / `checklist` | ACs pendentes (diagrama ASCII) |
| `importar …` / `import …` | Buscar prompt na API → gravar `.cursor/commands/{id}.md` + atualizar `.dev-workspace/imported-prompts.json` (ver skill § Importar prompt) |
| Outros | Seguir skill (`pendências`, `tasks`, `milestones`, `plans`, `resumo`, listar prompts, …) |

Exemplos:

- `/dev-workspace importar o prompt gsync main`
- `/dev-workspace importar gsync-main`
- `/dev-workspace checkpoints`
- `/dev-workspace milestones`
- `/dev-workspace plans`
- `/dev-workspace features`
- `/dev-workspace tasks`
- `/dev-workspace projects`
