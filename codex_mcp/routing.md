## Model routing: Claude tiers and Codex

A starting split, not a measured one; revise it from what the Codex run log and reviews show. The main session's tokens go to judgment, not to reading, polling or landing by hand: lanes carry the work, and Codex takes its share of the reviewing and the doing.

- **Opus (main session):** plan, rule, decide merges. Stay thin: hand each slice to a lane (a script or a workflow that runs to the end and writes a verdict file), wake on the verdict, then redirect or land. Do not poll, gate or land by hand where a script can. In a workflow stage, name Opus only with a written reason.
- **Sonnet subagents:** implementation that needs Claude Code's tools, hooks or permissions, and the reviewer of work Codex wrote.
- **Haiku:** mechanical lookups and classification.
- **Codex** (`mcp__codex__codex`; registered for one host, `CODEX_MCP_HOST`; on other hosts it shows as failed by design):
  - **Default reviewer of work Claude wrote:** `report: "review"`, with `cwd` the lane's own folder. Use `sandbox: "read-only"` where the sandbox shows that folder and `workspace-write` where it does not (the read-only sandbox hides a private `/dev/shm`). The wrapper refuses a folder the sandbox cannot see, and voids a review that changed files or could have without being seen. A refused or void review is not a verdict: fix the cause and run it again. The reviewer is never the author's model family: Codex reviews Claude's work, Claude reviews Codex's.
  - **Default doer for well-scoped tasks** (a named failing test, the files it owns, no secrets): `worktree: true`, `report: "task"`. Treat the report as a claim: check `observed` and `mismatch`, run the tests yourself, review the worktree diff, then merge or discard and remove the worktree (`git worktree remove <path>`, `git branch -D <branch>`).
  - **Not for:** anything touching secrets, credentials or confidential data with write access (Codex's actions bypass Claude's hooks, so keep it read-only there), or work that needs back-and-forth (each call is independent; put all context in the prompt).
  - **Spend:** the owner's ChatGPT quota (notional, not billed). Every run is logged to the run log (`CODEX_MCP_LOG`, default `~/.local/state/codex-mcp/runs.jsonl`); report Codex runs' tokens alongside other spend.
