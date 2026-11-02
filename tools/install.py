"""Register the Codex MCP server with Claude Code from config.env, and keep the repo clean.

    install    check everything first (config, this host, codex login, the routing rule in
               CLAUDE.md, that the server starts and lists its tool), then add the
               user-scope MCP entry "codex"; a second run with the same config changes
               nothing, a changed config replaces the entry
    routing    add the routing rule (codex_mcp/routing.md) to CLAUDE.md, or bring it up to
               date; the rest of the file is left as it is
    uninstall  remove the entry and the routing rule (nothing to do if they are not there)
    status     exit 0 if the entry and the routing rule match, 1 if missing or drifted
    leakcheck  fail if a file git would commit contains a value from config.env, the home
               directory, this host's name or domain, or a LEAKCHECK_WORDS entry

The host-specific values live only in the gitignored config.env and in the entry this
writes to the Claude config; the repo stays generic. The entry goes through the `claude mcp`
CLI rather than an edit of the JSON file, so Claude Code stays the only writer of its file.

Exit codes: 0 OK, 1 FAILED, 2 USAGE, 3 CONFIG. The last line printed is always `exit: ...`.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "codex_mcp", "server.py")
ROUTING = os.path.join(REPO, "codex_mcp", "routing.md")
BEGIN = "<!-- claude-codex-mcp routing: begin (managed by make routing; edit routing.md) -->"
END = "<!-- claude-codex-mcp routing: end -->"
SMOKE_TIMEOUT = 30  # seconds for the server to answer initialize + tools/list
NAME = "codex"  # the MCP server name; Claude exposes the tool as mcp__codex__codex
SANDBOXES = ("read-only", "workspace-write")
EXIT_NAMES = {0: "OK", 1: "FAILED", 2: "USAGE", 3: "CONFIG"}

REQUIRED = ("CODEX_MCP_PYTHON", "CODEX_MCP_BIN", "CODEX_MCP_HOST")
OPTIONAL = {
    "CODEX_MCP_DEFAULT_SANDBOX": "read-only",
    "CODEX_MCP_TIMEOUT": "1800",
    "CODEX_MCP_MAX_PARALLEL": "2",
    "CODEX_MCP_LOG": "~/.local/state/codex-mcp/runs.jsonl",
    "CLAUDE_BIN": "claude",
    "CLAUDE_CONFIG": "~/.claude.json",
    "CLAUDE_MD": "~/.claude/CLAUDE.md",
    "LEAKCHECK_WORDS": "",
}
# Keys passed to the server in the entry's env; the rest are for this script only.
SERVER_KEYS = ("CODEX_MCP_BIN", "CODEX_MCP_HOST", "CODEX_MCP_DEFAULT_SANDBOX",
               "CODEX_MCP_TIMEOUT", "CODEX_MCP_MAX_PARALLEL", "CODEX_MCP_LOG")
PATH_KEYS = ("CODEX_MCP_PYTHON", "CODEX_MCP_BIN", "CODEX_MCP_LOG", "CLAUDE_CONFIG",
             "CLAUDE_MD")


class Fail(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def read_env_file(path):
    """{key: raw value} from KEY=VALUE lines; blank lines and # comments are skipped."""
    values = {}
    try:
        f = open(path)
    except OSError as e:
        raise Fail(3, "cannot read config %s: %s" % (path, e))
    with f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, eq, value = line.partition("=")
            if not eq or not key.strip():
                raise Fail(3, "%s:%d: not KEY=VALUE: %r" % (path, n, line))
            values[key.strip()] = value.strip()
    return values


def load_config(path):
    """Typed, checked config; every problem is reported in one run before failing."""
    if not os.path.isfile(path):
        raise Fail(3, "no config file %s - copy config.example.env to config.env and set "
                      "its values" % path)
    raw = read_env_file(path)
    problems = ["unknown key %s (known: %s)" % (k, ", ".join(REQUIRED + tuple(OPTIONAL)))
                for k in sorted(raw) if k not in REQUIRED and k not in OPTIONAL]
    cfg = {}
    for key in REQUIRED:
        if raw.get(key):
            cfg[key] = raw[key]
        else:
            problems.append("[not set] %s" % key)
    for key, default in OPTIONAL.items():
        if raw.get(key):
            cfg[key] = raw[key]
        else:
            cfg[key] = default
            say("[using default] %s = %s" % (key, default))
    for key in PATH_KEYS:
        if key in cfg:
            cfg[key] = os.path.expanduser(cfg[key])

    for key in ("CODEX_MCP_PYTHON", "CODEX_MCP_BIN"):
        if key in cfg and not (os.path.isabs(cfg[key]) and os.path.isfile(cfg[key])
                               and os.access(cfg[key], os.X_OK)):
            problems.append("%s must be an absolute path to an executable: %r"
                            % (key, cfg[key]))
    if cfg["CODEX_MCP_DEFAULT_SANDBOX"] not in SANDBOXES:
        problems.append("CODEX_MCP_DEFAULT_SANDBOX must be one of %s: %r"
                        % (", ".join(SANDBOXES), cfg["CODEX_MCP_DEFAULT_SANDBOX"]))
    try:
        if float(cfg["CODEX_MCP_TIMEOUT"]) <= 0:
            raise ValueError
    except ValueError:
        problems.append("CODEX_MCP_TIMEOUT must be a positive number of seconds: %r"
                        % cfg["CODEX_MCP_TIMEOUT"])
    try:
        if int(cfg["CODEX_MCP_MAX_PARALLEL"]) < 1:
            raise ValueError
    except ValueError:
        problems.append("CODEX_MCP_MAX_PARALLEL must be an integer >= 1: %r"
                        % cfg["CODEX_MCP_MAX_PARALLEL"])
    if not os.path.isabs(cfg["CODEX_MCP_LOG"]):
        problems.append("CODEX_MCP_LOG must be an absolute path: %r" % cfg["CODEX_MCP_LOG"])
    if "CODEX_MCP_HOST" in cfg and cfg["CODEX_MCP_HOST"] != socket.gethostname():
        problems.append("CODEX_MCP_HOST is %r but this host is %r - the server would refuse "
                        "to start; set it to the output of `hostname`"
                        % (cfg["CODEX_MCP_HOST"], socket.gethostname()))
    claude = shutil.which(cfg["CLAUDE_BIN"])
    if claude is None:
        problems.append("CLAUDE_BIN not found or not executable: %r" % cfg["CLAUDE_BIN"])
    cfg["CLAUDE_BIN"] = claude

    if problems:
        for p in problems:
            say(p)
        raise Fail(3, "%d problem(s) in %s" % (len(problems), path))
    return cfg


def routing_block():
    with open(ROUTING) as f:
        return "%s\n%s\n%s\n" % (BEGIN, f.read().strip("\n"), END)


def read_claude_md(cfg):
    """(text, (start, end) of the routing block or None); text is "" if there is no file."""
    path = cfg["CLAUDE_MD"]
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return "", None
    except OSError as e:
        raise Fail(1, "cannot read %s: %s" % (path, e))
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise Fail(1, "%s has %d routing begin and %d end markers; expected one of each - "
                      "fix it by hand" % (path, text.count(BEGIN), text.count(END)))
    if BEGIN not in text:
        return text, None
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    if end < start:
        raise Fail(1, "%s: routing end marker comes before the begin marker" % path)
    if text[end:end + 1] == "\n":
        end += 1
    return text, (start, end)


def routing_state(cfg):
    """"current", "missing" or "stale"."""
    text, span = read_claude_md(cfg)
    if span is None:
        return "missing"
    return "current" if text[span[0]:span[1]] == routing_block() else "stale"


def write_atomic(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".claude-md-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def preflight(cfg, want):
    """Every reason install would fail, checked before anything is changed."""
    problems = []
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(cfg["CODEX_MCP_BIN"]) + os.pathsep + env.get("PATH", "")
    p = subprocess.run([cfg["CODEX_MCP_BIN"], "login", "status"], env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    if p.returncode != 0:
        problems.append("codex is not logged in (`codex login status` exit %d: %s) - run "
                        "`codex login`" % (p.returncode, p.stdout.strip()[-300:]))
    state = routing_state(cfg)
    if state != "current":
        problems.append("the routing rule in %s is %s - run `make routing` to %s it. "
                        "It tells Claude when to use Codex:\n%s"
                        % (cfg["CLAUDE_MD"], "missing" if state == "missing" else
                           "out of date", "add" if state == "missing" else "update",
                           routing_block()))
    problems += smoke_test(want)
    return problems


def smoke_test(entry):
    """Start the server as Claude would and check it lists the codex tool; [] when it does."""
    env = dict(os.environ)
    env.update(entry["env"])
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    stdin = "".join(json.dumps(r) + "\n" for r in reqs)
    try:
        p = subprocess.run([entry["command"]] + entry["args"], input=stdin, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=SMOKE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return ["the server did not start: %s" % e]
    tools = None
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            tools = [t.get("name") for t in (msg.get("result") or {}).get("tools", [])]
    if p.returncode != 0 or tools != [NAME]:
        return ["the server does not start as configured (exit %s, tools %r):\n%s"
                % (p.returncode, tools, p.stderr.strip()[-2000:] or "(no stderr)")]
    return []


def desired_entry(cfg):
    return {"type": "stdio", "command": cfg["CODEX_MCP_PYTHON"], "args": [SERVER],
            "env": {k: cfg[k] for k in SERVER_KEYS}}


def current_entry(cfg):
    """The registered user-scope entry, or None if there is none."""
    path = cfg["CLAUDE_CONFIG"]
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise Fail(1, "cannot read Claude config %s: %s" % (path, e))
    return (data.get("mcpServers") or {}).get(NAME)


def claude(cfg, *args):
    cmd = [cfg["CLAUDE_BIN"], "mcp"] + list(args)
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    if p.returncode != 0:
        raise Fail(1, "`claude mcp %s` failed (exit %d): %s"
                      % (args[0], p.returncode, p.stdout.strip()))


def make_log_dir(cfg):
    d = os.path.dirname(cfg["CODEX_MCP_LOG"])
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)  # the log holds prompts; keep it private even if it existed
    except OSError as e:
        raise Fail(1, "cannot create log directory %s: %s" % (d, e))
    say("log directory %s (mode 0700)" % d)


def cmd_install(cfg):
    want = desired_entry(cfg)
    problems = preflight(cfg, want)
    if problems:
        for p in problems:
            say(p)
        raise Fail(3, "%d problem(s) found before install; nothing was changed"
                      % len(problems))
    say("preflight ok: host, codex login, routing rule, server starts and lists %r" % NAME)
    make_log_dir(cfg)
    have = current_entry(cfg)
    if have == want:
        say("entry %r already matches config - unchanged" % NAME)
        return
    if have is not None:
        say("entry %r differs from config - replacing it" % NAME)
        claude(cfg, "remove", "-s", "user", NAME)
    claude(cfg, "add-json", "-s", "user", NAME, json.dumps(want))
    if current_entry(cfg) != want:
        raise Fail(1, "after `claude mcp add-json`, the entry in %s does not match config"
                      % cfg["CLAUDE_CONFIG"])
    say("registered %r: %s %s" % (NAME, want["command"], SERVER))


def cmd_routing(cfg):
    text, span = read_claude_md(cfg)
    block = routing_block()
    if span is not None and text[span[0]:span[1]] == block:
        say("routing rule in %s is current - unchanged" % cfg["CLAUDE_MD"])
        return
    if span is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n")
                                                              else "\n\n")
        new = text + sep + block
        say("adding the routing rule to %s" % cfg["CLAUDE_MD"])
    else:
        new = text[:span[0]] + block + text[span[1]:]
        say("updating the routing rule in %s" % cfg["CLAUDE_MD"])
    write_atomic(cfg["CLAUDE_MD"], new)  # raises on any failure, so no read-back


def cmd_uninstall(cfg):
    if current_entry(cfg) is None:
        say("no entry %r - nothing to remove" % NAME)
    else:
        claude(cfg, "remove", "-s", "user", NAME)
        if current_entry(cfg) is not None:
            raise Fail(1, "entry %r is still in %s after `claude mcp remove`"
                          % (NAME, cfg["CLAUDE_CONFIG"]))
        say("removed %r" % NAME)
    text, span = read_claude_md(cfg)
    if span is None:
        say("no routing rule in %s - nothing to remove" % cfg["CLAUDE_MD"])
        return
    new = text[:span[0]] + text[span[1]:]
    if new.endswith("\n\n") and not text[span[1]:]:
        new = new.rstrip("\n") + "\n"  # drop the blank line `routing` put before the block
    write_atomic(cfg["CLAUDE_MD"], new)
    say("removed the routing rule from %s" % cfg["CLAUDE_MD"])


def cmd_status(cfg):
    have, want = current_entry(cfg), desired_entry(cfg)
    state = routing_state(cfg)
    if state != "current":
        say("routing rule in %s is %s (run make routing)" % (cfg["CLAUDE_MD"], state))
    if have is None:
        raise Fail(1, "entry %r is not installed (run make install)" % NAME)
    if have != want:
        for key in sorted(set(want["env"]) | set((have.get("env") or {}))):
            a, b = (have.get("env") or {}).get(key), want["env"].get(key)
            if a != b:
                say("  env %s: installed %r, config %r" % (key, a, b))
        for key in ("type", "command", "args"):
            if have.get(key) != want[key]:
                say("  %s: installed %r, config %r" % (key, have.get(key), want[key]))
        raise Fail(1, "entry %r differs from config (run make install)" % NAME)
    if state != "current":
        raise Fail(1, "routing rule is %s" % state)
    say("entry %r and the routing rule match config" % NAME)


def leak_needles(config_path):
    """[(label, text)] that must not appear in committed files."""
    needles = []
    if os.path.isfile(config_path):
        raw = read_env_file(config_path)
        for key in PATH_KEYS:
            value = os.path.expanduser(raw.get(key, ""))
            if value:
                needles.append((key, value))
        host = raw.get("CODEX_MCP_HOST", "")
        if host:
            needles.append(("CODEX_MCP_HOST", host))
        needles += [("LEAKCHECK_WORDS", w.strip())
                    for w in raw.get("LEAKCHECK_WORDS", "").split(",") if w.strip()]
    else:
        say("no %s - checking environment values only (home, host name)" % config_path)
        host = ""
    needles.append(("home directory", os.path.expanduser("~")))
    for label, name in (("configured host", host), ("this host", socket.getfqdn())):
        if "." in name:
            needles.append((label + " name", name))
            domain = name.split(".", 1)[1]
            if "." in domain:  # a bare TLD such as "local" would match ordinary words
                needles.append((label + " domain", domain))
    seen, out = set(), []
    for label, text in needles:
        if not os.path.isabs(text) and "." not in text and label != "LEAKCHECK_WORDS":
            say("not searching for %s %r: a bare name would match ordinary text" % (label, text))
            continue
        if text == "/":
            say("not searching for %s '/': it matches every path" % label)
            continue
        if text not in seen:
            seen.add(text)
            out.append((label, text))
    return out


def cmd_leakcheck(config_path, root):
    p = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others",
                        "--exclude-standard"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise Fail(1, "git ls-files failed in %s: %s"
                      % (root, p.stderr.decode(errors="replace").strip()))
    files = [f for f in p.stdout.decode("utf-8", "surrogateescape").split("\0") if f]
    needles = leak_needles(config_path)
    hits, scanned = 0, 0
    for rel in files:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue  # deleted from the work tree, still in the index
        with open(full, "rb") as f:
            text = f.read().decode("utf-8", "surrogateescape")
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            for label, needle in needles:
                if needle in line:
                    say("%s:%d: contains %s" % (rel, n, label))
                    hits += 1
    say("scanned %d file(s) for %d value(s)" % (scanned, len(needles)))
    if scanned == 0:
        raise Fail(1, "no files to scan in %s - a check over nothing proves nothing" % root)
    if hits:
        raise Fail(1, "%d leak(s) found; move these values to config.env" % hits)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        say(self.format_usage().strip())
        raise Fail(2, message)


def main(argv):
    parser = Parser(prog="install.py", description=__doc__.split("\n")[0])
    parser.add_argument("command",
                        choices=("install", "routing", "uninstall", "status", "leakcheck"))
    parser.add_argument("--config", default=os.path.join(REPO, "config.env"))
    parser.add_argument("--root", default=REPO, help="tree to leak-check (default: this repo)")
    try:
        args = parser.parse_args(argv)
        if args.command == "leakcheck":
            cmd_leakcheck(args.config, args.root)
        else:
            cfg = load_config(args.config)
            {"install": cmd_install, "routing": cmd_routing, "uninstall": cmd_uninstall,
             "status": cmd_status}[args.command](cfg)
        code, why = 0, args.command + " done"
    except Fail as e:
        code, why = e.code, str(e)
    except KeyboardInterrupt:
        code, why = 1, "interrupted"
    except Exception as e:  # still end with an exit line, never a bare traceback
        code, why = 1, "internal error: %r" % (e,)
    say("exit: %d %s - %s" % (code, EXIT_NAMES[code], why))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
