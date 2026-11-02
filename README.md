# claude-codex-mcp

Lets Claude Code call OpenAI Codex as a tool: a reviewer for work Claude wrote, or a doer
for a well-scoped task while Claude coordinates. It is a small stdio MCP server
(`codex_mcp/server.py`, Python standard library only) that wraps `codex exec --json`, plus
an installer that registers it with Claude Code from a gitignored config file.

## Why a wrapper

Current Codex CLI builds have no built-in `codex mcp-server`, so this server runs one
`codex exec` turn per call and returns a JSON envelope. It holds Codex's answer next to
what was **observed**, so the caller checks Codex's claims instead of trusting them:

- `observed.commands`: every shell command Codex ran, with its exit code.
- `observed.files_changed`: the files that differ in a git snapshot taken before and after
  the run. Codex edits through shell commands, which emit no file-change event, so the
  snapshot is the evidence. Outside a git repo this is `null` (unknown), never `[]`.
- `mismatch` (with `report: "task"`): files Codex claimed but did not change, and files it
  changed but did not claim.

Runs are `--ephemeral`, because a stored Codex thread can time out on the shared
`~/.codex` thread lock. So each call is independent: put all the context in the prompt.
Codex's own `--worktree` needs a stored thread, so `worktree: true` makes a git worktree
here instead.

## The tool

`codex(prompt, cwd, sandbox?, report?, worktree?, model?)`

| Argument | Meaning |
|---|---|
| `prompt` | Full instructions for Codex. |
| `cwd` | Absolute directory to run in. |
| `sandbox` | `read-only` or `workspace-write` (edits under `cwd`). `danger-full-access` is never offered. |
| `report` | `task` or `review`: Codex answers in the fixed JSON shape in `codex_mcp/reports.json`. |
| `worktree` | Run in a new worktree off `HEAD`, under `<git dir>/codex-worktrees/`, on branch `codex/<name>`. Uncommitted changes in the main tree are not in it. |
| `model` | Codex model override. |

The usual patterns:

- **Review:** `report: "review"`, `sandbox: "read-only"`. You get a verdict and findings,
  each with the evidence Codex says it used.
- **Task:** `worktree: true`, `report: "task"`. Check `observed` and `mismatch`, run the
  tests yourself, and review the diff in the worktree. Then merge or discard, and remove
  it (`git worktree remove <path>`, `git branch -D <branch>`). Worktrees are not removed
  for you.

Codex's actions bypass Claude Code's hooks and permissions. Every run is therefore
appended to a JSONL log (mode 0600) with its host, directory, sandbox, outcome, token
usage and the start of its prompt. Spend is the owner's ChatGPT quota (notional, not
billed).

## Install

```sh
cp config.example.env config.env   # then set this host's values
make verify                        # tests + leak check
make install                       # register the "codex" MCP server for Claude Code
make status                        # 0 if the registered entry matches config.env
```

`make install` builds the user-scope MCP entry from `config.env` and registers it through
`claude mcp add-json`, so Claude Code stays the only writer of its own config. If it runs
again with the same config, it changes nothing. If the config has changed, it replaces the
entry and then reads it back to confirm. It also creates the log directory with mode 0700.
`make uninstall` removes the entry.

The user-scope entry lives in `~/.claude.json`. When that file is shared across hosts
(for example, a networked home directory), `CODEX_MCP_HOST` makes the server refuse to
start anywhere else. On other hosts it then shows as failed, by design.

A managed `managed-mcp.json` in the system Claude Code directory gives the administrator
exclusive control of MCP servers: `claude mcp add` is refused, and user entries do not
load.

## Config

`config.env` holds one `KEY=VALUE` per line. `~` is expanded in paths. Unknown keys, empty
required keys and bad values are all reported in one run, with exit code 3, before
anything is changed. Each optional key that is left out prints a `[using default]` line.

| Key | Required | Default | Used by | Meaning |
|---|---|---|---|---|
| `CODEX_MCP_PYTHON` | yes | | make, installer | Python 3 that runs the server and the tools. |
| `CODEX_MCP_BIN` | yes | | server | The `codex` executable. Its directory also goes first on `PATH`, so Codex finds its `node`. |
| `CODEX_MCP_HOST` | yes | | server | The only host name the server starts on. |
| `CODEX_MCP_DEFAULT_SANDBOX` | no | `read-only` | server | The sandbox used when a call does not choose one. |
| `CODEX_MCP_TIMEOUT` | no | `1800` | server | Seconds per call. After that, Codex's process group is killed. |
| `CODEX_MCP_MAX_PARALLEL` | no | `2` | server | Codex runs at once. Further calls wait. |
| `CODEX_MCP_LOG` | no | `~/.local/state/codex-mcp/runs.jsonl` | server | The run log. |
| `CLAUDE_BIN` | no | `claude` | installer | The Claude Code CLI. |
| `CLAUDE_CONFIG` | no | `~/.claude.json` | installer | Where Claude Code keeps user-scope MCP entries. It is read to compare against, and never written. |
| `LEAKCHECK_WORDS` | no | | leak check | Comma-separated words that must never be committed. |

The server also reads `CODEX_MCP_REPORTS` (the report schemas; by default `reports.json`
beside it). The installer does not set it.

## Leak check

`make leakcheck` (part of `make verify`) fails if any file git would commit contains a
`config.env` path or host, the home directory, this host's name or domain, or a
`LEAKCHECK_WORDS` entry. Gitignored files are skipped. A check over zero files fails,
because it proves nothing. Without a `config.env` (as in CI), it checks the home directory
and host name only.

## Exit codes (`tools/install.py`)

| Code | Name | When |
|---|---|---|
| 0 | OK | Done, or nothing needed doing. |
| 1 | FAILED | The `claude` CLI failed, the read-back did not match, the entry is missing or has drifted (`status`), or a leak was found. |
| 2 | USAGE | Unknown command or option. |
| 3 | CONFIG | `config.env` is missing, or a key is missing, unknown or invalid. |

The last line printed is always `exit: <code> <name> - <reason>`.

## Tests

`make test` runs both suites against a fake `codex` and a fake `claude` in throwaway
directories under `/dev/shm`. They never touch the real Claude config, Codex or its quota.
`tests/test_docs.py` checks that the config table above matches the keys in the code.

## License

MIT. See `LICENSE`.
