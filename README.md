# claude-codex-mcp

Lets Claude Code call OpenAI Codex as a tool: a reviewer for work Claude wrote, or a doer
for a well-scoped task while Claude coordinates. It is a small stdio MCP server
(`codex_mcp/server.py`, Python standard library only) that wraps `codex exec --json`, plus
an installer that checks everything and then registers it with Claude Code from a
gitignored config file.

## Quick start

You need:

- **Claude Code** (the `claude` CLI).
- **Codex CLI**, logged in: run `codex login` once. `make install` refuses to register
  the server while `codex login status` fails.
- **Python 3.** The server and installer use only the standard library.
- **git**, if you want `observed.files_changed` and `worktree: true` (both need a repo).
- No administrator-managed `managed-mcp.json` in the system Claude Code directory. When
  that file exists, it gives the administrator exclusive control of MCP servers:
  `claude mcp add` is refused, and user entries do not load.

Then, in a clone of this repo:

```sh
cp config.example.env config.env   # 1. set the three required values (see Config)
make verify                        # 2. tests + leak check; exit 0 expected
make routing                       # 3. add the rule that tells Claude when to use Codex
make install                       # 4. check everything, then register the "codex" server
make status                        # 5. exit 0: the entry and the rule match config.env
```

The three required values are the Python that runs the server (`CODEX_MCP_PYTHON`), the
absolute path of `codex` (`CODEX_MCP_BIN`, from `command -v codex`) and this host's name
(`CODEX_MCP_HOST`, from `hostname`).

Then restart Claude Code: it reads MCP servers at start-up. In a new session, `/mcp`
lists `codex` as connected. The first call asks for permission like any other tool; you
can allow `mcp__codex__codex` there, or in the `permissions` of your Claude Code settings.

If a step fails, it says why on its last line (see [Exit codes](#exit-codes)), and
`make install` changes nothing until every check passes.

## Why a wrapper

Current Codex CLI builds have no built-in `codex mcp-server`, so this server runs one
`codex exec` turn per call and returns a JSON envelope. It holds Codex's answer next to
what was **observed**, so the caller checks Codex's claims instead of trusting them.

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

The result is one JSON object:

| Field | Meaning |
|---|---|
| `ok`, `error` | `ok` is false when Codex failed, timed out, gave no final message, or a requested report did not match its schema; `error` says which. |
| `message`, `report` | Codex's final message, and the parsed report when one was asked for. |
| `observed.commands` | Every shell command Codex ran, with its exit code. |
| `observed.files_changed` | Files that differ in a git snapshot taken before and after the run. Codex edits through shell commands, which emit no file-change event, so the snapshot is the evidence. Outside a git repo this is `null` (unknown), never `[]`. |
| `mismatch` | With `report: "task"`: files Codex claimed but did not change, and files it changed but did not claim. |
| `worktree` | The worktree's path, branch and base commit, when one was made. |
| `run` | Exit code, sandbox, model, token usage and duration. `unparsed_lines` holds any Codex output line that was not a JSON event, so nothing is dropped unseen. |
| `log_error` | Why the run could not be logged, or `null`. |

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

## The routing rule

Registering the tool does not tell Claude when to use it. `codex_mcp/routing.md` holds
that rule: which work goes to the Claude tiers, when Codex reviews or does, what it must
not touch, and how its spend is reported. `make routing` copies it into your Claude
instructions file (`CLAUDE_MD`) between two marker comments, adding it once or replacing
an older copy in place, and leaves the rest of the file alone. Edit `routing.md`, not the
copy: `make install` and `make status` fail while the copy is missing or differs, and
show the rule. `make uninstall` removes the copy.

## What `make install` does

Before it changes anything, it checks, and lists every problem in one run:

- `config.env` is complete and valid, and `CODEX_MCP_HOST` is this host.
- `codex login status` succeeds.
- The routing rule is present and current.
- The server starts with exactly the command and environment that would be registered,
  and lists its `codex` tool.

Only then does it create the log directory (mode 0700) and register the user-scope entry
through `claude mcp add-json`, so Claude Code stays the only writer of its own config. If
the entry already matches, it changes nothing. If it differs, it replaces it and reads it
back to confirm. `make uninstall` removes the entry and the routing rule.

The user-scope entry lives in `~/.claude.json`. When that file is shared across hosts
(for example, a networked home directory), `CODEX_MCP_HOST` makes the server refuse to
start anywhere else, so on other hosts `/mcp` shows it as failed, by design.

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
| `CLAUDE_MD` | no | `~/.claude/CLAUDE.md` | installer | The instructions file that `make routing` adds the routing rule to. |
| `LEAKCHECK_WORDS` | no | | leak check | Comma-separated words that must never be committed. |

The server also reads `CODEX_MCP_REPORTS` (the report schemas; by default `reports.json`
beside it). The installer does not set it.

## Leak check

`make leakcheck` (part of `make verify`) fails if any file git would commit contains a
`config.env` path or host, the home directory, this host's name or domain, or a
`LEAKCHECK_WORDS` entry. Gitignored files are skipped. A check over zero files fails,
because it proves nothing. A value it cannot search for safely (a bare name such as
`codex`, which would match ordinary text) is named on a `not searching for` line rather
than skipped silently. Without a `config.env` (as in CI), it checks the home directory and
host name only.

## Exit codes

Both programs end with one line, `exit: <code> <name> - <reason>`: the installer on
stdout, the server on stderr (stdout carries the MCP protocol). An unexpected error ends
with that line too, not a traceback.

| Code | Name | Installer (`tools/install.py`) | Server (`codex_mcp/server.py`) |
|---|---|---|---|
| 0 | OK | Done, or nothing needed doing. | Claude Code closed its stdin. |
| 1 | FAILED | The `claude` CLI failed, a read-back did not match, the entry or routing rule is missing or has drifted (`status`), a leak was found, or an unexpected error. | An unexpected error. |
| 2 | USAGE | Unknown command or option. | |
| 3 | CONFIG | `config.env` is missing, unreadable or invalid, or a preflight check failed; nothing was changed. | A setting is invalid, or this is not `CODEX_MCP_HOST`. |

A failed Codex run is not a server failure: the call returns `ok: false` with the reason,
and the server keeps serving.

## Tests

`make test` runs the suites against a fake `codex` and a fake `claude` in throwaway
directories under `/dev/shm`. They never touch the real Claude config, Codex or its quota.
`tests/test_docs.py` checks this README against the code: the config table, the exit
codes and the make targets.

## License

MIT. See `LICENSE`.
