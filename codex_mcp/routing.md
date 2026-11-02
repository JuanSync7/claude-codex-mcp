## Model routing: Claude tiers and Codex

A starting split, not a measured one; revise it from what the Codex run log and reviews show.

- **Opus (main session):** plan, judge, verify, decide merges.
- **Sonnet subagents:** bulk implementation inside Claude Code (it runs under Claude's hooks and permissions).
- **Haiku:** mechanical lookups and classification.
- **Codex** (`mcp__codex__codex`; registered for one host, `CODEX_MCP_HOST`; on other hosts it shows as failed by design):
  - **Reviewer of work Claude wrote:** `report: "review"`, `sandbox: "read-only"`. The reviewer is never the author's model family: Codex reviews Claude's work, Claude reviews Codex's.
  - **Doer for well-scoped tasks:** `worktree: true`, `report: "task"`. Treat the report as a claim: check `observed` and `mismatch`, run the tests yourself, review the worktree diff, then merge or discard and remove the worktree (`git worktree remove <path>`, `git branch -D <branch>`).
  - **Not for:** anything touching secrets, credentials or confidential data with write access (Codex's actions bypass Claude's hooks, so keep it read-only there), or work that needs back-and-forth (each call is independent; put all context in the prompt).
  - **Spend:** the owner's ChatGPT quota (notional, not billed). Every run is logged to the run log (`CODEX_MCP_LOG`, default `~/.local/state/codex-mcp/runs.jsonl`); report Codex runs' tokens alongside other spend.
