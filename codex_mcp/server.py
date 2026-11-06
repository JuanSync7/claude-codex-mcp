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
    CODEX_MCP_MAX_PARALLEL     concurrent Codex runs, or none for no limit (default none);
                               past a limit, further calls wait
    CODEX_MCP_REPORTS          report schemas (default reports.json beside this file)
    CODEX_MCP_LIMITS           how Codex words its usage limit and reset time
                               (default limits.json beside this file)
    CODEX_MCP_LOG              run log (default ~/.local/state/codex-mcp/runs.jsonl;
                               its directory must exist - `make install` makes it)

Exit codes: 0 OK (stdin closed, no call running), 1 FAILED (internal error, or stdin closed
while calls ran: they are cancelled, logged and counted), 3 CONFIG (a setting is wrong;
every problem is listed). The last line on stderr is always `exit: <code> <name> - <reason>`.
"""

import datetime
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NO_LIMIT = "none"  # the CODEX_MCP_MAX_PARALLEL value that sets no limit
SANDBOXES = ("read-only", "workspace-write")  # danger-full-access is never offered

DEFAULTS = {
    "CODEX_MCP_BIN": "codex",
    "CODEX_MCP_DEFAULT_SANDBOX": "read-only",
    "CODEX_MCP_TIMEOUT": "1800",
    "CODEX_MCP_MAX_PARALLEL": NO_LIMIT,
    "CODEX_MCP_REPORTS": os.path.join(HERE, "reports.json"),
    "CODEX_MCP_LIMITS": os.path.join(HERE, "limits.json"),
    "CODEX_MCP_LOG": os.path.expanduser("~/.local/state/codex-mcp/runs.jsonl"),
}

def setting(key):
    return os.environ.get(key) or DEFAULTS[key]

EXIT_NAMES = {0: "OK", 1: "FAILED", 3: "CONFIG"}

CODEX_BIN = setting("CODEX_MCP_BIN")
DEFAULT_SANDBOX = setting("CODEX_MCP_DEFAULT_SANDBOX")
REPORTS_PATH = setting("CODEX_MCP_REPORTS")
LIMITS_PATH = setting("CODEX_MCP_LIMITS")
LOG_PATH = setting("CODEX_MCP_LOG")
TIMEOUT = None  # seconds; set and checked in configure()
SLOTS = None    # CODEX_MCP_MAX_PARALLEL: a semaphore, or a no-op for none; set in configure()

REPORTS = {}  # kind -> JSON schema, loaded in main()
LIMITS = {}   # match, reset_at (compiled) and reset_format, loaded in configure()
# After Codex says the usage limit resets at a known time, calls before then are refused
# without starting it: LIMITED is what that run reported, LIMITED_UNTIL its reset.
LIMITED, LIMITED_UNTIL = None, None
_limit_lock = threading.Lock()
_out_lock = threading.Lock()
_log_lock = threading.Lock()

# When stdin closes, calls still in flight are cancelled, not dropped: each running codex
# is killed, logged as cancelled, and counted in the exit line.
CANCEL_GRACE = 10       # seconds to wait for cancelled calls to log themselves
CANCEL = threading.Event()
_calls = set()          # tools/call threads not yet finished
_running = {}           # pid -> Popen of codex runs in progress
_killed = set()         # pids killed by a cancel
_calls_lock = threading.Lock()


def kill_running():
    """Kill every codex run in progress (called once stdin has closed)."""
    with _calls_lock:
        for pid in list(_running):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:  # already exited
                continue
            _killed.add(pid)


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
            "report=task|review makes it answer in a fixed JSON shape. A cwd the sandbox "
            "cannot see is refused before Codex runs, and a review that changed files (or "
            "could have, unseen) is void. Each call is "
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


class NotStarted(Exception):
    """cmd could not be started: no process exists, so nothing it could have touched."""


def run_tracked(cmd, cwd, env):
    """Run cmd in its own process group where a cancel can kill it.

    Returns (returncode, stdout, stderr, why): why is "timeout", "cancelled" or None.
    Raises NotStarted when cmd cannot start; any other error means it did start.
    """
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, start_new_session=True)
    except Exception as e:
        raise NotStarted(e)
    with _calls_lock:
        _running[proc.pid] = proc
    if CANCEL.is_set():  # the cancel ran before the process was registered
        kill_running()
    why = None
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        why = "timeout"
    finally:
        with _calls_lock:
            _running.pop(proc.pid, None)
            if proc.pid in _killed:
                why = "cancelled"
    return proc.returncode, out, err, why


def remove_worktree(cwd, worktree):
    """Undo make_worktree for a run that never started; returns an error string, or None."""
    top = (git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    for args in (["worktree", "remove", "--force", worktree["path"]],
                 ["branch", "-D", worktree["branch"]]):
        p = subprocess.run(["git", "-C", top] + args, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        if p.returncode != 0:
            return "git %s failed: %s" % (" ".join(args[:2]), p.stderr.strip())
    return None


def sandbox_start(sandbox, run_dir, env):
    """(folder, error): where a shell inside the sandbox starts when Codex runs in run_dir.

    A sandbox that cannot see run_dir (read-only mounts a private, empty /dev/shm) starts
    Codex in the home folder instead, with no error, so it would judge the wrong tree.
    """
    cmd = [CODEX_BIN, "sandbox", "-c", 'sandbox_mode="%s"' % sandbox, "--",
           "/bin/sh", "-c", "pwd"]
    try:
        rc, out, err, why = run_tracked(cmd, run_dir, env)
    except NotStarted as e:
        return None, "could not check the sandbox (%s): %s" % (CODEX_BIN, e)
    if why == "cancelled":
        return None, "cancelled before codex started: the client closed the connection"
    if why == "timeout":
        return None, "could not check the sandbox: codex sandbox timed out after %gs" % TIMEOUT
    if rc != 0:
        return None, "could not check the sandbox: codex sandbox exited %d: %s" % (
            rc, err.strip() or "(no stderr)")
    # pwd prints the folder and one newline; strip() would also eat spaces in its name
    folder = out[:-1] if out.endswith("\n") else None
    if not folder or "\n" in folder:
        return None, "could not check the sandbox: expected one folder, got %r" % out
    return folder, None


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


def usage_limit(said):
    """Codex's usage-limit message among the messages of its error events, as the pair
    ({"until": ISO local time or None if no reset time could be read, "said": the message},
    the reset as a datetime or None), or (None, None) when none is a usage limit. Codex
    gives only a clock time, so one already past today means tomorrow."""
    hit = [m for m in said if LIMITS["match"].search(m)]
    if not hit:
        return None, None
    found = LIMITS["reset_at"].search(hit[-1])
    until = None
    if found:
        try:
            clock = datetime.datetime.strptime(found.group(1), LIMITS["reset_format"])
        except ValueError as e:  # the pattern matched text the format cannot read
            sys.stderr.write("warning: cannot read the reset time %r: %s\n" % (found.group(1), e))
        else:
            now = datetime.datetime.now().astimezone()
            until = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            if until <= now:
                until += datetime.timedelta(days=1)
    return {"until": until and until.isoformat(), "said": hit[-1]}, until


def run_codex(args):
    global LIMITED, LIMITED_UNTIL
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
    with _limit_lock:
        limited, until = LIMITED, LIMITED_UNTIL
    if until is not None and datetime.datetime.now().astimezone() < until:
        return tool_result(json.dumps({
            "ok": False, "limited": limited,
            "error": "usage limit: not started; Codex said it resets at %s" % limited["until"],
        }, indent=1), True)

    worktree, run_dir = None, cwd
    if want_wt:
        try:
            worktree, run_dir = make_worktree(cwd)
        except (ValueError, RuntimeError) as e:
            return tool_result(str(e), True)

    env = dict(os.environ)
    bindir = os.path.dirname(CODEX_BIN)
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")

    def refuse(error):  # codex never ran: undo the worktree made for it
        if worktree:
            cleanup = remove_worktree(cwd, worktree)
            if cleanup:
                error += "; the new worktree %s is left behind: %s" % (worktree["path"], cleanup)
        return tool_result(error, True)

    started = False  # set as codex itself starts: a worktree it may have touched is kept
    try:
        start, error = sandbox_start(sandbox, run_dir, env)
        if error is None and os.path.realpath(start) != os.path.realpath(run_dir):
            error = ("cwd is not visible inside the %s sandbox: a shell there starts in %s, "
                     "not %s, so Codex would work on the wrong folder. Use a folder the sandbox "
                     "shows, or workspace-write, which mounts cwd."
                     % (sandbox, os.path.realpath(start), run_dir))
        if error is not None:
            return refuse(error)

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

        t0 = time.monotonic()
        error, out, err, rc = None, "", "", None
        try:
            with SLOTS:
                if CANCEL.is_set():  # queued behind another call when the client left
                    return refuse("cancelled before codex started: the client closed "
                                  "the connection")
                started = True
                try:
                    rc, out, err, why = run_tracked(cmd, run_dir, env)
                except NotStarted as e:
                    return refuse("could not start codex (%s): %s" % (CODEX_BIN, e))
                if why == "cancelled":
                    error = "cancelled mid-run: the client closed the connection"
                elif why == "timeout":
                    error = "codex timed out after %gs" % TIMEOUT
        finally:
            if schema_file:
                try:
                    os.remove(schema_file)
                except OSError as e:  # harmless to the call, but say so in the server log
                    sys.stderr.write("warning: cannot remove %s: %s\n" % (schema_file, e))
    except BaseException:  # an error before codex started: nothing in the worktree to keep
        if worktree and not started:
            cleanup = remove_worktree(cwd, worktree)
            if cleanup:
                sys.stderr.write("warning: the new worktree %s is left behind: %s\n"
                                 % (worktree["path"], cleanup))
        raise
    duration = round(time.monotonic() - t0, 1)

    messages, events_err, commands, usage, thread, unparsed = [], [], [], None, None, []
    said = []  # the messages of error events, read for a usage limit
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
            text = ev.get("message") or (ev.get("error") or {}).get("message")
            if isinstance(text, str):
                said.append(text)

    limited, until = usage_limit(said)
    if until is not None:
        with _limit_lock:
            if LIMITED_UNTIL is None or until >= LIMITED_UNTIL:
                LIMITED, LIMITED_UNTIL = limited, until

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

    # A reviewer that edits what it judges, or could have without being seen, gives no verdict.
    if error is None and report_kind == "review":
        if files:
            error = ("review void: %d file(s) changed during the review, listed in "
                     "observed.files_changed (%s)" % (len(files), basis))
        elif files is None and sandbox != "read-only":
            error = ("review void: the %s sandbox could write and the files changed are "
                     "unknown (%s)" % (sandbox, basis))

    body = {
        "ok": error is None,
        "error": error,
        "message": message,
        "report": report,
        "observed": {"commands": commands, "files_changed": files, "files_basis": basis},
        "mismatch": mismatch,
        "limited": limited,
        "worktree": worktree,
        "run": {"exit": rc, "sandbox": sandbox, "model": model, "thread": thread,
                "usage": usage, "duration_s": duration, "unparsed_lines": unparsed},
        "log_error": None,
    }
    body["log_error"] = log_run({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(), "cwd": cwd, "worktree": worktree, "sandbox": sandbox,
        "model": model, "report": report_kind, "ok": body["ok"], "exit": rc,
        "error": error and error[:300], "limited_until": limited and limited["until"],
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
    try:
        send({"jsonrpc": "2.0", "id": req_id, "result": result})
    except (OSError, ValueError) as e:  # client gone; the run is already in the log
        sys.stderr.write("cannot send result of call %r: %s\n" % (req_id, e))
    finally:
        with _calls_lock:
            _calls.discard(threading.current_thread())


class NoLimit:
    """The slot for CODEX_MCP_MAX_PARALLEL=none: entering it never waits.
    (contextlib.nullcontext would do, but needs Python 3.7.)"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
        if setting("CODEX_MCP_MAX_PARALLEL") == NO_LIMIT:
            SLOTS = NoLimit()
        else:
            slots = int(setting("CODEX_MCP_MAX_PARALLEL"))
            if slots < 1:
                raise ValueError
            SLOTS = threading.Semaphore(slots)
    except ValueError:
        problems.append("CODEX_MCP_MAX_PARALLEL must be an integer >= 1 or %s: %r"
                        % (NO_LIMIT, setting("CODEX_MCP_MAX_PARALLEL")))
    try:
        with open(REPORTS_PATH) as f:
            REPORTS.update(json.load(f))
        for kind, schema in REPORTS.items():
            if not isinstance(schema.get("required"), list):
                raise ValueError("schema %r has no required list" % kind)
    except Exception as e:
        problems.append("cannot load report schemas %s: %r" % (REPORTS_PATH, e))
    try:
        with open(LIMITS_PATH) as f:
            limits = json.load(f)
        LIMITS.update(match=re.compile(limits["match"]), reset_at=re.compile(limits["reset_at"]),
                      reset_format=limits["reset_format"])
        if LIMITS["reset_at"].groups != 1:
            raise ValueError("reset_at must have exactly one group, the reset time")
    except Exception as e:
        problems.append("cannot load usage-limit patterns %s: %r" % (LIMITS_PATH, e))
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
            t = threading.Thread(target=handle_call, args=(req_id, req.get("params") or {}),
                                 daemon=True)
            with _calls_lock:
                _calls.add(t)
            t.start()
        else:
            send({"jsonrpc": "2.0", "id": req_id,
                  "error": {"code": -32601, "message": "method not found: %s" % method}})
    return finish_calls()


def finish_calls():
    """stdin has closed: cancel calls still in flight; returns (exit code, reason)."""
    with _calls_lock:
        pending = list(_calls)
    if not pending:
        return 0, "stdin closed"
    CANCEL.set()
    kill_running()
    deadline = time.monotonic() + CANCEL_GRACE
    for t in pending:
        t.join(max(0, deadline - time.monotonic()))
    stuck = sum(t.is_alive() for t in pending)
    why = "stdin closed with %d call(s) running; cancelled them" % len(pending)
    if stuck:
        why += ", %d did not finish within %ds and may be unlogged" % (stuck, CANCEL_GRACE)
    return 1, why


def main():
    try:
        code, why = serve()
    except Exception as e:  # still end with an exit line, never a bare traceback
        code, why = 1, "internal error: %r" % (e,)
    sys.stderr.write("exit: %d %s - %s\n" % (code, EXIT_NAMES[code], why))
    return code


if __name__ == "__main__":
    sys.exit(main())
