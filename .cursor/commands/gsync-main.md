# Gsync Main

## Metadados

| Campo | Valor |
|-------|-------|
| Tipo | Claude skill |
| Invocação | `gsync_main` |
| Escopo | Usuário |
| Origem | `~/.claude/skills/gsync_main/SKILL.md` |

## Instruções

Use this command when the user asks for gsync_main. Commit and push directly to the `main` branch.

1. **Atomic Commits**

   * Break changes into the smallest possible logical units.
   * Each commit must contain *exactly one intention*.
   * Never group unrelated changes together.
   * If the user provides multiple modifications, separate them into multiple commits.

2. **Conventional Commits (in English only)**

   * Commit messages must follow:
     * `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `style:`, `perf:`, `test:`
   * The message must start with a lowercase type, a colon, and a short summary in English.
   * Use an optional body only when needed to clarify context.
   * Never write commit messages in Portuguese.
   * Do not add any trailer (e.g. `Co-authored-by`, `Signed-off-by`, etc.) or any extra line to the commit message. The message must contain *only* the conventional commit: type, optional scope, summary, and optional body when strictly needed—nothing else.
   * **Never add Cursor, Claude, or any AI tool as co-author** on commits.
   * Stage and commit with **Git CLI only** from the repository root:
     ```
     git add <paths>
     git commit -m "type(scope): short summary in English"
     ```
     Do not use wrapper scripts (such as `git_sync`) — use Git CLI only.

3. **Branch: Always Use `main`**

   * Before committing, ensure you are on `main`.
   * If the current branch is **not** `main`, switch to it.
   * Commit directly to `main`. Do **not** create a new branch.

4. **Push Rules**

   * Before pushing, always synchronize with remote: `git fetch --all` then `git rebase`.
   * After rebasing, push to `main`.
   * Never use `--force` or `--force-with-lease`.

5. **General Behavior**

   * Before committing, review the diff and ensure the commit contains only the changes relevant to its message.
   * If multiple commits are needed, list them first before executing them.

6. **Optional: Discord summary after push (explicit channel only)**

   * **Never** pick a Discord channel automatically.
   * After a successful `git push origin main`, only post to Discord if the user explicitly asked for a Discord notification **and** supplied a **channel ID** for that message. Use: `scripts/discord/send-message.sh CHANNEL_ID "Short summary (under 2000 chars)."`
   * If the user asked for Discord but did not give a `CHANNEL_ID`, run `scripts/discord/list-channels.sh`, show the list, and **wait for the user to choose** an ID before sending.
   * If `.env`, token, or guild ID is missing, skip or say Discord is not configured.
