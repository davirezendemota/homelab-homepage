---
name: dev-workspace
description: Dev Workspace — planejamento, pendências, tasks, checkpoints, milestones, plans, importar prompts, checklist agregado e resumo IA via API. Use quando pedir status do projeto, importar prompt do DW, checkpoints, milestones, planos, roadmap, resumo, ou perguntas sobre o workspace. Specs no repo continuam em .specs/ (ler/editar direto ou /spec-checklist).
disable-model-invocation: true
argument-hint: [checkpoints | milestones | plans | features | tasks | projects | pendências | importar prompt | resumo]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# Dev Workspace

Conexão: `.dev-workspace/.env`

| Variável | Uso |
|----------|-----|
| `DEV_WORKSPACE_ROOT` | Modo local (`workspace_data/`) |
| `DEV_WORKSPACE_URL` + `DEV_WORKSPACE_API_TOKEN` | Modo API — use o **token do consumidor** do projeto (não o admin global) |

**Tokens:** cada repo com `local_path` no DW recebe um token **scoped** (só os projetos desse path). Token admin global (logs do container) acessa tudo — **não** coloque no consumidor.

**Consumo via agent-cli:** com URL + token no `.env`, use **somente a API** (`curl` no shell). **Não** leia `workspace_data/` com Read/Grep — o humano testa sempre pela IA, não por curl manual.

Modos: só ROOT → local (dev sem container) · URL+token → api · ambos → **sempre API primeiro** (local só se API falhar).

## Escrita vs leitura (agentes no consumidor)

| Recurso | Ler (`GET`) | Escrever (`PUT`/`POST`) pelo agente |
|---------|-------------|--------------------------------------|
| spec-checklist / features (repo) | ✅ API + `.specs/` | ✅ só no repo (`.specs/`, `/spec-checklist`) |
| checkpoints | ✅ | ❌ UI do DW |
| milestones | ✅ | ❌ UI do DW |
| **plans** | ✅ | ✅ **após aprovação do usuário** (ver § Plans) |
| **tasks** | ✅ | ❌ **somente leitura** — nunca `PUT` tasks |
| prompts DW | ✅ | ❌ UI do DW (importar = cópia local) |

## vs `.specs/`

| | `.specs/` | Esta skill (DW API) |
|---|-----------|---------------------|
| Editar specs / ACs no arquivo | ✅ `/new-spec`, `/spec-checklist` | — |
| Ler specs no repo | ✅ direto | — |
| Pendências agregadas, tasks DW (**leitura**), checkpoints, milestones, plans (**escrita após aprovação**), prompts DW, resumo IA, ask multi-projeto | — | ✅ |

## Setup

```bash
set -a && source .dev-workspace/.env && set +a
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

Header em todas as chamadas API: `Authorization: Bearer $DEV_WORKSPACE_API_TOKEN`

## Resolver projetos deste repo

```bash
curl -s -H "Authorization: Bearer $DEV_WORKSPACE_API_TOKEN" \
  "$DEV_WORKSPACE_URL/api/projects"
```

Com token de consumidor, a API já retorna **apenas** o(s) projeto(s) deste repo (`local_path`). Não filtrar manualmente nem chamar outros ids.

Obter token do consumidor (admin / UI DW): `GET /api/projects/{id}/connection` → `consumer_api_token`.

Fallback local (só se API indisponível e **sem** URL+token): scan `$DEV_WORKSPACE_ROOT/workspace_data/projects/*.json`.

## API

| Necessidade | Chamada |
|-------------|---------|
| Pendências / checklist | `GET /api/projects/{id}/spec-checklist` |
| Tasks (**somente leitura**) | `GET /api/projects/{id}/tasks` — **não** usar `PUT` |
| Checkpoints | `GET /api/projects/{id}/checkpoints` |
| Milestones (planejamento futuro) | `GET /api/projects/{id}/milestones` |
| Salvar milestones | `PUT /api/projects/{id}/milestones` — **UI do DW**; agentes não gravam |
| Planos de ação (por milestone) | `GET /api/projects/{id}/plans` |
| **Cadastrar plano aprovado** | `PUT /api/projects/{id}/plans` `{"version":1,"items":[...]}` — ver § Plans |
| Gerar plano (IA no DW) | `POST /api/projects/{id}/plans/generate` `{"milestoneId":"..."}` |
| Feature (spec markdown) | `GET /api/projects/{id}/features/{specId}` |
| Resumo IA | `GET /api/projects/{id}` → `json_data.ai` |
| Regenerar resumo | `POST /api/projects/{id}/ai-summary` |
| Pergunta aberta | `POST /api/projects/ask` `{"prompt":"..."}` |
| Listar prompts (aba Prompts, **somente leitura**) | `GET /api/prompts` (`?q=` busca por nome/id/texto) |
| Um prompt | `GET /api/prompts/{id}` → `content` (Markdown) |

## Formatação ASCII (terminal)

Para **checkpoints**, **milestones**, **plans**, **features**, **tasks** e **projects**: responder com **um diagrama ASCII em bloco de código** (` ```text `). **Não** usar mermaid, tabela markdown ou lista duplicada fora do bloco.

Regras comuns:

- Título do bloco: `{TIPO} · {contexto}` (nome do projeto ou `este repo`).
- Caixas com `┌─`, `│`, `└─`; conectar caixas com `│` entre elas quando houver sequência.
- Quebrar linhas longas (~60 chars) mantendo indentação `│   `.
- Omitir linhas vazias (resumo, notas, specs) quando o campo estiver vazio.
- Após o título do marco (`●` checkpoint ou `◆` milestone), inserir **linha vazia** (`│` sozinha) antes de resumo, notas ou specs (só quando houver conteúdo abaixo do título). **Não** usar linha horizontal (`───`).
- Em milestones: linha vazia `│` entre descrição e linha `specs:` (quando ambas existem).
- Símbolos de status:
  - Checkpoints (histórico): `●` marco · ordem **mais recente primeiro** (`▼ mais recente` no topo).
  - Milestones (futuro): `◆` marco · ordem **data mais próxima primeiro** (`▲ mais próximo` no topo).
  - Plans: passos `○` todo · `◐` in-progress · `✓` done · `⊘` blocked · ordem por `order`.
  - Features: `▣` spec · ordem por `specId`.
  - Tasks: `✓` concluída · `○` pendente · ordem da API.
  - Projects: `▸` projeto · ordem da API.

## Pendências

Após spec-checklist: listar ACs com `status` ≠ `done` por spec. Usar campo `stats` se existir.

Quando o usuário pedir **pendências** ou **checklist**:

1. `GET /api/projects/{id}/spec-checklist`.
2. Responder em ASCII (não lista markdown solta).

```text
PENDÊNCIAS · {nome do projeto}

  {done}/{total} ACs · {todo} pendente · {in_progress} em progresso · {blocked} bloqueado

┌─ 003 · Título da spec ─────────────────────
│ ○ AC2 — descrição curta do AC
│ ◐ AC4 — em progresso
│ ⊘ AC5 — bloqueado
└─────────────────────────────────────────────
         │
┌─ 011 · Outra spec ─────────────────────────
│ ○ AC1 — descrição
└─────────────────────────────────────────────
```

Símbolos AC: `✓` done · `◐` in-progress · `⊘` blocked · `○` todo.

## Checkpoints

**Checkpoints** = reuniões com stakeholders e entregas **já realizadas** (histórico).

Quando o usuário pedir **checkpoints**, **timeline de entregas** ou **histórico**:

1. Buscar **via API** `GET /api/projects/{id}/checkpoints` (nunca ler JSON local se URL+token no `.env`).
2. Ordenação: **mais recente primeiro** (como na UI do DW).
3. Formato ASCII (ver § Formatação ASCII).
4. Agrupar por dia; vários marcos no mesmo dia ficam sob o mesmo cabeçalho de data.
5. Omitir linha de resumo se vazio.

Formato (um marco):

```text
CHECKPOINTS · {nome do projeto}

  ▼ mais recente

┌─ 29/07 ─────────────────────────────────────
│ ● Concepção do MVP
│
│   Primeiro checkpoint — concepção do MVP.
└─────────────────────────────────────────────
```

Formato (vários dias — conectar com `│` entre caixas):

```text
CHECKPOINTS · {nome do projeto}

  ▼ mais recente

┌─ 29/07 ─────────────────────────────────────
│ ● Concepção do MVP
│
│   Resumo do marco.
└─────────────────────────────────────────────
         │
┌─ 20/07 ─────────────────────────────────────
│ ● Kickoff
│
│   Resumo do marco.
└─────────────────────────────────────────────
         ▼
  mais antigo
```

Título do marco em linha `● {título}`; linha vazia `│` entre título e resumo; resumo indentado com `│   ` (quebrar linhas longas ~60 chars).

## Milestones

**Milestones** = planejamento **futuro** — specs que serão implementadas. Vinculados a `specIds` do checklist, **não** a checkpoints.

Quando o usuário pedir **milestones**, **planejamento**, **roadmap** ou **specs futuras**:

1. `GET /api/projects/{id}/milestones` e, se precisar de títulos de specs, `GET /api/projects/{id}/spec-checklist`.
2. Ordenação: **data alvo mais próxima primeiro**; sem data no final.
3. Formato ASCII (ver § Formatação ASCII). Agrupar por data alvo quando vários milestones compartilham o dia.
4. Incluir specs vinculadas como `specs: {specId}` (título da spec se disponível no checklist). Linha vazia `│` entre descrição e `specs:` quando ambas existem.

Formato (um marco):

```text
MILESTONES · {nome do projeto}

  ▲ mais próximo

┌─ 15/08 ─────────────────────────────────────
│ ◆ Sprint 2
│
│   Entregar auth e dashboard.
│
│   specs: 011-skill-dev-workspace, 012-milestones
└─────────────────────────────────────────────
```

Formato (vários marcos — conectar com `│` entre caixas):

```text
MILESTONES · {nome do projeto}

  ▲ mais próximo

┌─ 15/08 ─────────────────────────────────────
│ ◆ Sprint 2
│
│   Entregar auth e dashboard.
│
│   specs: 011, 012
└─────────────────────────────────────────────
         │
┌─ 30/09 ─────────────────────────────────────
│ ◆ Release v2
│
│   specs: 013
└─────────────────────────────────────────────
         ▼
  mais distante
```

## Plans

**Plans** = planos de ação vinculados a uma milestone. Uma milestone pode ter **vários** planos (`milestoneId`). Cada plano tem um **`id` único e estável** no JSON do projeto — use esse `id` no agent-cli para referenciar o plano (`implementar plano plan01`, `/dev-workspace plan plan01`, etc.). Passos ordenados com referência opcional a `specId`/`ac` e campo `content` (texto).

### Cadastrar plano após aprovação (obrigatório)

Quando o agente **elaborar** um plano (rascunho, milestone, implementação) e o usuário **aprovar** o conteúdo:

1. Resolver o **project id** deste repo: `GET /api/projects` (token consumidor).
2. Obter `milestoneId`: `GET /api/projects/{id}/milestones` (título citado pelo usuário ou milestone do contexto).
3. `GET /api/projects/{id}/plans` → lista atual (anotar `id`s existentes para não colidir).
4. **Cadastrar** o plano aprovado no Dev Workspace do projeto respectivo:
   - `PUT /api/projects/{id}/plans` com `{"version":1,"items":[...lista atual + novo plano]}`.
   - Novo item mínimo: **`id`** (string única no projeto), `milestoneId`, **`title`** no padrão **`PXXX - nome`** (ex.: `P001 - Auth refresh` — `P` + 3 dígitos + ` - ` + nome; código `PXXX` único no projeto), `source` (`"manual"` ou `"ai"`), `generatedAt` (ISO-8601), `content` (texto completo aprovado), `items[]` (passos estruturados, se houver).
   - Para o próximo `PXXX`: `GET plans`, extrair códigos existentes (`P001`, `P002`…) e usar o próximo disponível (ex.: se já existem `P001` e `P002`, cadastrar como `P003 - …`).
5. Confirmar ao usuário: **`id`** do plano, **`title`** (`PXXX - nome`), milestone e que foi salvo no DW.

**Não** encerrar a tarefa de planejamento só com o rascunho no chat — plano aprovado **deve** persistir no DW com `id` registrado.

Atalho (sem rascunho manual): `POST /api/projects/{id}/plans/generate` com `milestoneId` — a IA do DW gera, atribui `id` e **já cadastra** na lista de planos. Use o `id` retornado em `GET plans` ou na resposta do `PUT`.

### Consultar e implementar

Quando o usuário pedir **planos**, **plano**, **plan {id}**, **plan-milestone**, **implementar milestone** ou **roadmap detalhado**:

1. `GET /api/projects/{id}/plans` e, para contexto, `GET /api/projects/{id}/milestones`.
2. Filtrar por `milestoneId`, por **`PXXX`** no título ou por **`id`** se o usuário citar (`P001`, `plan01`, etc.).
3. Ordenar planos da milestone: **mais recente primeiro** (`generatedAt`).
4. Para **implementar**: usar o plano pelo **`id` citado**, o mais recente da milestone, ou o que o usuário indicar; seguir `content` ou `items[]` em ordem.
5. Formato ASCII. Exibir **`title`** (`PXXX - nome`) e `id` de cada plano — referência no agent-cli: `P001` ou `id`.

Listagem (vários planos na mesma milestone):

```text
PLANS · {título da milestone}

  2 planos · mais recente primeiro

┌─ P002 - Sprint 2 (30/07/2026) ──────────────
│ id: plan02 · P002
└─────────────────────────────────────────────
         │
┌─ P001 - Rascunho inicial (15/07/2026) ─────
│ id: plan01 · P001
└─────────────────────────────────────────────
```

Detalhe de um plano:

```text
PLAN · P001 - {nome do plano}

  id: plan01 · P001 · milestone: {título} · {data}

┌─ conteúdo ──────────────────────────────────
│ 1. Discovery: swipe e curtida
│    spec: 004/AC1
│    Feed e ações like/pass
│
│ 2. Match por reciprocidade
│    spec: 004/AC2
└─────────────────────────────────────────────
```

## Features

**Features** = specs do checklist (`.specs/features/*.md`) — requisitos e ACs do projeto.

Quando o usuário pedir **features**, **specs** ou **listar specs**:

1. `GET /api/projects/{id}/spec-checklist`.
2. Para **uma** feature específica (`feature 003`, `spec 011`): `GET /api/projects/{id}/features/{specId}` (markdown completo) + ACs do checklist para essa spec.
3. Formato ASCII. Ordenar specs por `specId`.

Listagem:

```text
FEATURES · {nome do projeto}

  {done}/{total} ACs no projeto

┌─ 003 · Projects modal ─────────────────────
│ features/003-projects.md
│ 5/8 ACs · ○ AC3 · ○ AC6
└─────────────────────────────────────────────
         │
┌─ 011 · Skill dev-workspace ────────────────
│ features/011-skill-dev-workspace.md
│ 4/6 ACs · todos concluídos ✓
└─────────────────────────────────────────────
```

Detalhe de uma feature (pedido explícito de spec/AC):

```text
FEATURE · {specId} — {título}

  {specFile} · {done}/{total} ACs

┌─ ACs ───────────────────────────────────────
│ ✓ AC1 — descrição
│ ✓ AC2 — descrição
│ ◐ AC3 — em progresso
│ ○ AC4 — pendente
└─────────────────────────────────────────────

  (resumo do markdown só se o usuário pediu conteúdo — truncar ~400 chars)
```

Não repetir o markdown inteiro da feature sem pedido explícito.

## Tasks

**Tasks** = tarefas operacionais do projeto (JSON do DW), distintas dos ACs de specs.

### Somente leitura (agentes)

**Nunca** chamar `PUT /api/projects/{id}/tasks` a partir do repo consumidor ou desta skill.

- Tasks são criadas, editadas e marcadas concluídas **apenas na UI** do Dev Workspace (aba Tasks).
- O agente **pode** `GET /api/projects/{id}/tasks` para contexto e exibição em ASCII.
- **Não** sincronizar progresso de implementação em tasks via API — use ACs do checklist (`.specs/`) ou o plano cadastrado em `plans`.

Quando o usuário pedir **tasks** ou **tarefas**:

1. `GET /api/projects/{id}/tasks` (**somente leitura**).
2. Formato ASCII. Ordem da API (`items`).
3. Mostrar contagem `{done}/{total}` no cabeçalho.

```text
TASKS · {nome do projeto}

  3/5 concluídas

┌─ lista ─────────────────────────────────────
│ ✓ Implementar endpoint de milestones
│ ✓ Atualizar skill dev-workspace
│ ○ Escrever testes E2E
│ ○ Revisar PR
│ ○ Deploy em staging
└─────────────────────────────────────────────
```

Se vazio:

```text
TASKS · {nome do projeto}

  0/0 concluídas

┌─ lista ─────────────────────────────────────
│   (vazio)
└─────────────────────────────────────────────
```

## Projects

Quando o usuário pedir **projects**, **projetos** ou visão do **repo** no DW:

1. `GET /api/projects` (com token consumidor → só projeto(s) deste repo).
2. Para cada projeto, opcionalmente `GET /api/projects/{id}/spec-checklist` → `stats` e `GET /api/projects/{id}/tasks` → contagem.
3. Formato ASCII. Um caixa por projeto.

```text
PROJECTS · este repo

  1 projeto

┌─ ▸ dev-workspace ───────────────────────────
│ id: dev-workspace
│ path: /home/davi/workspace/dev-workspace
│ specs: 18/24 ACs · tasks: 3/5 · checkpoints: 6
│ AI: Resumo curto do projeto truncado se longo…
└─────────────────────────────────────────────
```

Vários projetos (token admin — conectar caixas com `│`):

```text
PROJECTS · workspace

  3 projetos

┌─ ▸ dev-workspace ───────────────────────────
│ specs: 18/24 · tasks: 3/5 · checkpoints: 6
└─────────────────────────────────────────────
         │
┌─ ▸ outro-repo ──────────────────────────────
│ specs: 2/10 · tasks: 0/0 · checkpoints: 1
└─────────────────────────────────────────────
```

Não invente URL, token ou project id.

## Prompts (aba Prompts do DW)

**Somente leitura** via `/api/prompts` — criar/editar prompts é na UI do DW (`/api/agents`). No consumidor, **importar** grava cópia local em `.cursor/commands/`.

Prompts globais do workspace (arquivos `*.md` em `agents_folder`) — **não** são por projeto; qualquer repo consumidor pode buscar pelo `id` (slug do arquivo).

Quando o usuário pedir **prompt do DW**, **usar prompt X**, ou citar um slug (`dev-fullstack-nextjs`, `specs-new-spec`, …):

1. Listar: `GET /api/prompts` ou buscar `GET /api/prompts?q=nextjs`.
2. Conteúdo completo: `GET /api/prompts/{id}` → usar campo `content` (Markdown com `# título`).
3. **Seguir o prompt** no trabalho atual do repo (contexto local + pedido do usuário).
4. Resumir qual prompt foi usado (`id` + `name`) — não precisa repetir o Markdown inteiro se o usuário só pediu listagem.

`id` = nome do arquivo sem `.md` (ex.: `dev-fullstack-nextjs` para `dev-fullstack-nextjs.md`).

## Importar prompt no repo

Quando o usuário pedir **importar prompt**, **importar o prompt X** ou `/dev-workspace importar …`:

1. Resolver o prompt via API (nunca ler `workspace_data/agents/`):
   - `GET /api/prompts?q={termos}` (ex.: `gsync main` → `gsync-main`).
   - Se vários resultados, escolher o mais próximo do nome ou pedir confirmação.
   - Fallback: slugificar termos (`gsync main` → `gsync-main`) e tentar `GET /api/prompts/{id}`.
2. `GET /api/prompts/{id}` → campo `content`.
3. Gravar **no repo consumidor** (não no DW):
   - Arquivo: `.cursor/commands/{id}.md`
   - Conteúdo: `content` da API **sem alterar** o corpo (reimportação substitui o arquivo).
4. Registrar em `.dev-workspace/imported-prompts.json`:

```json
{
  "gsync-main": {
    "path": ".cursor/commands/gsync-main.md",
    "name": "Gsync Main",
    "imported_at": "ISO-8601",
    "dw_updated_at": "do campo updated_at da API"
  }
}
```

5. Confirmar ao usuário: `id`, caminho local e se foi criado ou atualizado.

**Não** commitar automaticamente. O usuário invoca depois via slash do metadata (`/bootstrap-specs`, etc.) ou pelo nome do arquivo.

Importar ≠ só ler: **sempre** escrever o arquivo no consumidor quando o pedido é importar.
