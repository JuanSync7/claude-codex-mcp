"""Codex as an MCP tool for Claude Code.

Codex 0.166 dropped its built-in `codex mcp-server`, so this is a small stdio MCP
server that wraps `codex exec --json`. One tool, `codex`: give it a prompt and a
working directory, it runs one Codex turn there and returns a JSON envelope with
Codex's answer next to what was observed (commands run, files changed), so the
caller checks Codex's claims instead of trusting them.

Why it is built this way:
- Runs are `--ephemeral`. The shared ~/.codex thread store is locked by the Codex
  app-server daemon and a non-ephemeral `codex exec` times out on that lock, so
  there is no multi-turn reply, and Codex's own `--worktree` (which needs a stored
  thread) is unusable. `worktree: true` makes a git worktree here instead.
- Files changed come from a git snapshot before and after the run, not from Codex
  events: Codex edits through shell commands, which emit no file-change event.
  Outside a git repo the list is null (unknown), never empty.
- `report` makes Codex answer in a fixed JSON shape (reports.json, via
  --output-schema); for a task report the claimed files are diffed against the
  observed ones.
- Codex's actions bypass Claude Code's hooks, so every run is logged to a 0600
  JSONL file (spend is ChatGPT quota, i.e. notional, not billed).
- No MCP SDK: the stdio protocol is a few newline-delimited JSON-RPC methods.

Config (environment, set in the MCP entry):
    CODEX_MCP_BIN              codex executable (its dir also goes first on PATH, for node)
    CODEX_MCP_HOST             if set, refuse to start on any other host (home is NFS-shared)
    CODEX_MCP_DEFAULT_SANDBOX  read-only | workspace-write  (default read-only)
    CODEX_MCP_TIMEOUT          seconds per call (default 1800)
    CODEX_MCP_MAX_PARALLEL     concurrent Codex runs (default 2); further calls wait
    CODEX_MCP_REPORTS          report schemas (default reports.json beside this file)
    CODEX_MCP_LOG              run log (default ~/.local/state/codex-mcp/runs.jsonl;
                               its directory must exist - `make install` makes it)

Exit codes: 0 OK (stdin closed), 1 FAILED (internal error), 3 CONFIG (a setting is wrong;
every problem is listed). The last line on stderr is always `exit: <code> <name> - <reason>`.
"""

import datetime
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOXES = ("read-only", "workspace-write")  # danger-full-access is never offered

DEFAULTS = {
    "CODEX_MCP_BIN": "codex",
    "CODEX_MCP_DEFAULT_SANDBOX": "read-only",
    "CODEX_MCP_TIMEOUT": "1800",
    "CODEX_MCP_MAX_PARALLEL": "2",
    "CODEX_MCP_REPORTS": os.path.join(HERE, "reports.json"),
    "CODEX_MCP_LOG": os.path.expanduser("~/.local/state/codex-mcp/runs.jsonl"),
}

def setting(key):
    return os.environ.get(key) or DEFAULTS[key]

EXIT_NAMES = {0: "OK", 1: "FAILED", 3: "CONFIG"}

CODEX_BIN = setting("CODEX_MCP_BIN")
DEFAULT_SANDBOX = setting("CODEX_MCP_DEFAULT_SANDBOX")
REPORTS_PATH = setting("CODEX_MCP_REPORTS")
LOG_PATH = setting("CODEX_MCP_LOG")
TIMEOUT = None  # seconds; set and checked in configure()
SLOTS = None    # semaphore of CODEX_MCP_MAX_PARALLEL; set in configure()

REPORTS = {}  # kind -> JSON schema, loaded in main()
_out_lock = threading.Lock()
_log_lock = threading.Lock()


def tool_def():
    return {
        "name": "codex",
        "description": (
            "Run one OpenAI Codex agent turn (codex exec) in a directory. Returns JSON: "
            "Codex's message (or report), plus what was observed - commands it ran with exit "
            "codes and files changed per a git snapshot (null = unknown, outside git). "
            "Treat the message/report as Codex's claim and check it against `observed` and "
            "`mismatch`. sandbox=workspace-write lets it edit files under cwd; worktree=true "
            "runs it in a fresh git worktree off HEAD (uncommitted main-tree changes are not "
            "in it) and returns its path and branch for you to review and merge. "
            "report=task|review makes it answer in a fixed JSON shape. Each call is "
            "independent (no memory), so put all needed context in the prompt. Uses the "
            "owner's Codex quota."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Full instructions for Codex."},
                "cwd": {"type": "string", "description": "Absolute working directory."},
                "sandbox": {"type": "string", "enum": list(SANDBOXES),
                            "description": "Default: " + DEFAULT_SANDBOX},
                "report": {"type": "string", "enum": sorted(REPORTS),
                           "description": "task: summary/files/tests/questions. "
                                          "review: verdict and findings with evidence."},
                "worktree": {"type": "boolean",
                             "description": "Run in a new git worktree (needs a git repo)."},
                "model": {"type": "string", "description": "Codex model override."},
            },
            "required": ["prompt", "cwd"],
            "additionalProperties": False,
        },
    }


def send(msg):
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def git(cwd, *args):
    """stdout of a git command, or None if it fails (e.g. not a repo)."""
    p = subprocess.run(["git", "-C", cwd] + list(args), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.stdout.decode("utf-8", "surrogateescape") if p.returncode == 0 else None


def snapshot(top):
    """{path: (status, content hash)} for every non-clean path in the tree at `top`."""
    out = git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if out is None:
        raise RuntimeError("git status failed in %s" % top)
    snap, parts, i = {}, out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            i += 1  # the rename/copy source follows; the destination is what changed
        full = os.path.join(top, path)
        digest = None
        if os.path.isfile(full):
            h = hashlib.sha256()
            with open(full, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            digest = h.hexdigest()
        snap[path] = (status, digest)
    return snap


def make_worktree(cwd):
    top = git(cwd, "rev-parse", "--show-toplevel")
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if top is None or common is None:
        raise ValueError("worktree needs cwd inside a git repo: %s" % cwd)
    top, common = top.strip(), common.strip()
    name = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))
    path = os.path.join(common, "codex-worktrees", name)
    branch = "codex/" + name
    p = subprocess.run(["git", "-C", top, "worktree", "add", "-q", "-b", branch, path, "HEAD"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode != 0:
        raise RuntimeError("git worktree add failed: %s" % p.stderr.strip())
    base = (git(top, "rev-parse", "HEAD") or "").strip()
    sub = os.path.relpath(os.path.realpath(cwd), os.path.realpath(top))
    run_dir = path if sub == "." else os.path.join(path, sub)
    return {"path": path, "branch": branch, "base": base}, run_dir


def check_report(kind, text):
    """(report, error) - the parsed report, or why it is not one."""
    try:
        report = json.loads(text)
    except ValueError:
        return None, "report is not JSON: %r" % text[:500]
    if not isinstance(report, dict):
        return None, "report is not a JSON object"
    missing = [k for k in REPORTS[kind]["required"] if k not in report]
    if missing:
        return None, "report missing required keys: %s" % ", ".join(missing)
    return report, None


def log_run(entry):
    """Append one run to the log; returns an error string, or None."""
    try:
        line = json.dumps(entry) + "\n"
        with _log_lock:
            fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line)
        return None
    except Exception as e:
        return "run log %s: %r" % (LOG_PATH, e)


def run_codex(args):
    prompt = args.get("prompt")
    cwd = args.get("cwd")
    sandbox = args.get("sandbox") or DEFAULT_SANDBOX
    report_kind = args.get("report")
    want_wt = bool(args.get("worktree"))
    model = args.get("model")
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_result("prompt is required", True)
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return tool_result("cwd must be an existing absolute directory: %r" % (cwd,), True)
    if sandbox not in SANDBOXES:
        return tool_result("sandbox must be one of %s, got %r" % (SANDBOXES, sandbox), True)
    if report_kind is not None and report_kind not in REPORTS:
        return tool_result("report must be one of %s, got %r" % (sorted(REPORTS), report_kind),
                           True)

    worktree, run_dir = None, cwd
    if want_wt:
        try:
            worktree, run_dir = make_worktree(cwd)
        except (ValueError, RuntimeError) as e:
            return tool_result(str(e), True)

    top = git(run_dir, "rev-parse", "--show-toplevel")
    top = top.strip() if top else None
    if top is None:
        basis = "unknown: not a git repo"
    elif worktree:
        basis = "git-snapshot (isolated worktree)"
    else:
        basis = "git-snapshot (shared tree: includes any other writer during the run)"
    before = snapshot(top) if top else None

    cmd = [CODEX_BIN, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "-c", 'sandbox_mode="%s"' % sandbox, "-C", run_dir]
    if model:
        cmd += ["-m", model]
    schema_file = None
    if report_kind:
        fd, schema_file = tempfile.mkstemp(prefix="codex-report-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(REPORTS[report_kind], f)
        cmd += ["--output-schema", schema_file]
    cmd += ["--", prompt]

    env = dict(os.environ)
    bindir = os.path.dirname(CODEX_BIN)
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")

    t0 = time.monotonic()
    error, out, err, rc = None, "", "", None
    try:
        with SLOTS:
            try:
                proc = subprocess.Popen(cmd, cwd=run_dir, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        universal_newlines=True, start_new_session=True)
            except OSError as e:
                return tool_result("could not start codex (%s): %s" % (CODEX_BIN, e), True)
            try:
                out, err = proc.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                error = "codex timed out after %gs" % TIMEOUT
            rc = proc.returncode
    finally:
        if schema_file:
            try:
                os.remove(schema_file)
            except OSError as e:  # harmless to the call, but say so in the server log
                sys.stderr.write("warning: cannot remove %s: %s\n" % (schema_file, e))
    duration = round(time.monotonic() - t0, 1)

    messages, events_err, commands, usage, thread, unparsed = [], [], [], None, None, []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                unparsed.append(line[:500])  # returned, so a format change shows up
            continue
        if not isinstance(ev, dict):
            unparsed.append(line[:500])
            continue
        kind = ev.get("type")
        item = ev.get("item") or {}
        if kind == "thread.started":
            thread = ev.get("thread_id")
        elif kind == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
        elif kind == "item.completed" and item.get("type") == "command_execution":
            commands.append({"command": item.get("command"), "exit_code": item.get("exit_code")})
        elif kind == "turn.completed":
            usage = ev.get("usage")
        elif kind in ("error", "turn.failed"):
            events_err.append(json.dumps(ev))

    files = None
    if top:
        after = snapshot(top)
        files = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))

    message = messages[-1] if messages else None
    report = None
    if error is None and (rc != 0 or events_err):
        error = "codex failed: " + ("\n".join(events_err) or err.strip()[-4000:]
                                    or "(no stderr)")
    elif error is None and message is None:
        error = "no final message"
    elif error is None and report_kind:
        report, error = check_report(report_kind, message)

    mismatch = None
    if report_kind == "task" and report is not None and files is not None:
        claimed = set(report["files_changed"])
        mismatch = {"claimed_not_observed": sorted(claimed - set(files)),
                    "observed_not_claimed": sorted(set(files) - claimed)}

    body = {
        "ok": error is None,
        "error": error,
        "message": message,
        "report": report,
        "observed": {"commands": commands, "files_changed": files, "files_basis": basis},
        "mismatch": mismatch,
        "worktree": worktree,
        "run": {"exit": rc, "sandbox": sandbox, "model": model, "thread": thread,
                "usage": usage, "duration_s": duration, "unparsed_lines": unparsed},
        "log_error": None,
    }
    body["log_error"] = log_run({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(), "cwd": cwd, "worktree": worktree, "sandbox": sandbox,
        "model": model, "report": report_kind, "ok": body["ok"], "exit": rc,
        "duration_s": duration, "usage": usage, "spend": "chatgpt-quota (notional)",
        "commands": len(commands), "files_changed": None if files is None else len(files),
        "prompt_head": prompt[:200],
    })
    return tool_result(json.dumps(body, indent=1), error is not None)


def handle_call(req_id, params):
    if params.get("name") != "codex":
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32602, "message": "unknown tool: %r" % params.get("name")}})
        return
    try:
        result = run_codex(params.get("arguments") or {})
    except Exception as e:  # report, never leave the call hanging
        result = tool_result("internal error: %r" % (e,), True)
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def configure():
    """Check the settings; returns a list of problems (empty when all is well)."""
    global TIMEOUT, SLOTS
    problems = []
    want = os.environ.get("CODEX_MCP_HOST")
    if want and want != socket.gethostname():
        problems.append("CODEX_MCP_HOST is %s, refusing to start on %s"
                        % (want, socket.gethostname()))
    if DEFAULT_SANDBOX not in SANDBOXES:
        problems.append("CODEX_MCP_DEFAULT_SANDBOX must be one of %s: %r"
                        % (", ".join(SANDBOXES), DEFAULT_SANDBOX))
    try:
        TIMEOUT = float(setting("CODEX_MCP_TIMEOUT"))
        if TIMEOUT <= 0:
            raise ValueError
    except ValueError:
        problems.append("CODEX_MCP_TIMEOUT must be a positive number of seconds: %r"
                        % setting("CODEX_MCP_TIMEOUT"))
    try:
        slots = int(setting("CODEX_MCP_MAX_PARALLEL"))
        if slots < 1:
            raise ValueError
        SLOTS = threading.Semaphore(slots)
    except ValueError:
        problems.append("CODEX_MCP_MAX_PARALLEL must be an integer >= 1: %r"
                        % setting("CODEX_MCP_MAX_PARALLEL"))
    try:
        with open(REPORTS_PATH) as f:
            REPORTS.update(json.load(f))
        for kind, schema in REPORTS.items():
            if not isinstance(schema.get("required"), list):
                raise ValueError("schema %r has no required list" % kind)
    except Exception as e:
        problems.append("cannot load report schemas %s: %r" % (REPORTS_PATH, e))
    return problems


def serve():
    """Answer requests until stdin closes; returns (exit code, reason)."""
    for key in sorted(DEFAULTS):  # stderr: stdout is the protocol channel
        if not os.environ.get(key):
            sys.stderr.write("[using default] %s = %s\n" % (key, DEFAULTS[key]))
    problems = configure()
    if problems:
        for p in problems:
            sys.stderr.write(p + "\n")
        return 3, "; ".join(problems)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(req, dict):
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32600, "message": "request is not a JSON object"}})
            continue
        method, req_id = req.get("method"), req.get("id")
        if req_id is None:  # notification
            continue
        if method == "initialize":
            client = (req.get("params") or {}).get("protocolVersion") or "2025-06-18"
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": client,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codex", "version": "2"},
            }})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [tool_def()]}})
        elif method == "tools/call":
            threading.Thread(target=handle_call, args=(req_id, req.get("params") or {}),
                             daemon=True).start()
        else:
            send({"jsonrpc": "2.0", "id": req_id,
                  "error": {"code": -32601, "message": "method not found: %s" % method}})
    return 0, "stdin closed"


def main():
    try:
        code, why = serve()
    except Exception as e:  # still end with an exit line, never a bare traceback
        code, why = 1, "internal error: %r" % (e,)
    sys.stderr.write("exit: %d %s - %s\n" % (code, EXIT_NAMES[code], why))
    return code


if __name__ == "__main__":
    sys.exit(main())
