"""Drives server.py over stdio against a fake codex. Run: python3.12 -m unittest test_server"""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "codex_mcp", "server.py")

# The fake writes what it was asked to (FAKE_EDITS, via the filesystem like real Codex's
# shell edits), reports FAKE_CMDS as command_execution events, and answers FAKE_MESSAGE.
FAKE = textwrap.dedent("""\
    #!%s
    import json, os, sys, time
    argv = sys.argv[1:]
    schema = None
    if "--output-schema" in argv:
        schema = json.load(open(argv[argv.index("--output-schema") + 1]))
    open(os.environ["FAKE_LOG"], "w").write(json.dumps(
        {"argv": argv, "cwd": os.getcwd(), "schema": schema}))
    mode = os.environ.get("FAKE_MODE", "ok")
    if mode == "sleep":
        time.sleep(30)
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    if os.environ.get("FAKE_NOISE"):
        print("this is not a JSON event")
    for path, content in json.loads(os.environ.get("FAKE_EDITS", "[]")):
        if content is None:
            os.remove(path)
        else:
            open(path, "w").write(content)
    for cmd, code in json.loads(os.environ.get("FAKE_CMDS", "[]")):
        print(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
              "command": cmd, "aggregated_output": "", "exit_code": code}}))
    if mode in ("ok", "msgfail"):
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
              "text": os.environ.get("FAKE_MESSAGE", "PONG")}}))
        print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}))
    if mode in ("fail", "msgfail"):
        sys.stderr.write("boom\\n"); sys.exit(3)
    """) % sys.executable

TASK_REPORT = {"summary": "did it", "files_changed": ["a.txt", "ghost.txt"],
               "tests_run": ["make test"], "test_result": "pass",
               "open_questions": [], "confidence": "high"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), check=True, stdout=subprocess.PIPE,
                          universal_newlines=True).stdout


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "codex")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp.name, "argv.json")
        self.runlog = os.path.join(self.tmp.name, "runs.jsonl")
        self.work = os.path.join(self.tmp.name, "work")
        os.mkdir(self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def make_repo(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.mkdir(repo)
        git(repo, "init", "-q")
        for name in ("a.txt", "dirty.txt", "untouched.txt"):
            with open(os.path.join(repo, name), "w") as f:
                f.write(name)
        git(repo, "add", ".")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        return repo

    def env(self, mode="ok", **extra):
        e = dict(os.environ, CODEX_MCP_BIN=self.fake, FAKE_LOG=self.log, FAKE_MODE=mode,
                 CODEX_MCP_DEFAULT_SANDBOX="workspace-write", CODEX_MCP_LOG=self.runlog)
        e.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in extra.items()
                  if v is not None})
        for k in [k for k, v in extra.items() if v is None]:  # None unsets the key
            e.pop(k, None)
        return e

    def session(self, requests, **env):
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        p = subprocess.run([sys.executable, SERVER], input=stdin, env=self.env(**env),
                           timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        return p, {m["id"]: m for m in map(json.loads, p.stdout.splitlines())}

    def call(self, args, **env):
        """One tools/call; returns (isError, parsed result envelope or raw text)."""
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "codex", "arguments": args}}
        p = subprocess.Popen([sys.executable, SERVER], env=self.env(**env),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True)
        p.stdin.write(json.dumps(req) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()  # stdin stays open until the worker thread has answered
        p.stdin.close()
        p.wait(timeout=10)
        p.stdout.close()
        res = json.loads(line)["result"]
        text = res["content"][0]["text"]
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return res["isError"], body

    def assert_exit_line(self, text, code, name, reason):
        last = text.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("exit: %d %s - " % (code, name)), text)
        self.assertIn(reason, last)

    def fake_saw(self):
        with open(self.log) as f:
            return json.load(f)

    def runs(self):
        with open(self.runlog) as f:
            return [json.loads(line) for line in f]

    # --- protocol ---------------------------------------------------------------

    def test_initialize_and_list(self):
        p, r = self.session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "bogus"},
        ])
        self.assertEqual(r[1]["result"]["protocolVersion"], "2025-06-18")
        tools = r[2]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["codex"])
        props = tools[0]["inputSchema"]["properties"]
        self.assertNotIn("danger-full-access", props["sandbox"]["enum"])
        self.assertEqual(sorted(props["report"]["enum"]), ["review", "task"])
        self.assertEqual(r[3]["error"]["code"], -32601)
        self.assertEqual(len(r), 3)  # the notification got no reply

    def test_non_object_request_is_answered_not_fatal(self):
        p, r = self.session([[1], {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual(r[None]["error"]["code"], -32600)
        self.assertEqual(r[2]["result"], {})  # the server kept going
        self.assertEqual(p.returncode, 0)

    def test_wrong_host_refuses_to_start(self):
        p, r = self.session([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                            CODEX_MCP_HOST="not-" + socket.gethostname())
        self.assertEqual(p.returncode, 3)
        self.assertEqual(r, {})
        self.assert_exit_line(p.stderr, 3, "CONFIG", "refusing")

    def test_right_host_starts(self):
        p, r = self.session([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                            CODEX_MCP_HOST=socket.gethostname())
        self.assertEqual(r[1]["result"], {})

    def test_defaults_are_announced(self):
        p, r = self.session([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                            CODEX_MCP_TIMEOUT=None, CODEX_MCP_LOG=None)
        self.assertEqual(r[1]["result"], {})
        self.assertIn("[using default] CODEX_MCP_TIMEOUT = 1800", p.stderr)
        self.assertIn("[using default] CODEX_MCP_LOG = "
                      + os.path.expanduser("~/.local/state/codex-mcp/runs.jsonl"), p.stderr)
        self.assertNotIn("[using default] CODEX_MCP_DEFAULT_SANDBOX", p.stderr)  # it was set

    def test_missing_report_schemas_refuses_to_start(self):
        p, r = self.session([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                            CODEX_MCP_REPORTS=os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(p.returncode, 3)
        self.assertEqual(r, {})
        self.assert_exit_line(p.stderr, 3, "CONFIG", "nope.json")

    def test_bad_settings_refuse_to_start_listing_every_problem(self):
        p, r = self.session([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                            CODEX_MCP_TIMEOUT="soon", CODEX_MCP_MAX_PARALLEL="0",
                            CODEX_MCP_DEFAULT_SANDBOX="danger-full-access")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(r, {})
        self.assertNotIn("Traceback", p.stderr)
        for key in ("CODEX_MCP_TIMEOUT", "CODEX_MCP_MAX_PARALLEL", "CODEX_MCP_DEFAULT_SANDBOX"):
            self.assertIn(key, p.stderr)
        self.assert_exit_line(p.stderr, 3, "CONFIG", "")

    def close_mid_call(self, calls, **env):
        """Start `calls` sleeping calls, close stdin once codex runs; returns (process, stderr)."""
        p = subprocess.Popen([sys.executable, SERVER], env=self.env(mode="sleep", **env),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True)
        for i in range(calls):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                                      "params": {"name": "codex", "arguments": {
                                          "prompt": "x", "cwd": self.work}}}) + "\n")
        p.stdin.flush()
        deadline = time.monotonic() + 10
        while not os.path.exists(self.log):  # the fake writes it as soon as it starts
            self.assertLess(time.monotonic(), deadline, "codex never started")
            time.sleep(0.05)
        p.stdin.close()
        p.stdin = None  # closed by hand; communicate() must not flush it again
        _, err = p.communicate(timeout=20)
        return p, err

    def test_closing_mid_call_cancels_and_logs_it(self):
        t0 = time.monotonic()
        p, err = self.close_mid_call(1)
        self.assertLess(time.monotonic() - t0, 15)  # codex was killed, not waited out
        self.assertEqual(p.returncode, 1, err)
        self.assert_exit_line(err, 1, "FAILED", "1 call(s) running; cancelled")
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0]["ok"])
        self.assertIn("cancelled", runs[0]["error"])

    def test_closing_with_a_call_queued_does_not_start_it(self):
        p, err = self.close_mid_call(2, CODEX_MCP_MAX_PARALLEL="1")
        self.assertEqual(p.returncode, 1, err)
        self.assert_exit_line(err, 1, "FAILED", "2 call(s) running; cancelled")
        self.assertEqual(len(self.runs()), 1)  # the queued call never ran codex

    def test_clean_shutdown_prints_exit_line(self):
        p, r = self.session([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(p.returncode, 0)
        self.assert_exit_line(p.stderr, 0, "OK", "stdin closed")

    # --- plain calls ------------------------------------------------------------

    def test_unparsed_codex_output_is_counted_not_dropped(self):
        err, body = self.call({"prompt": "x", "cwd": self.work}, FAKE_NOISE="1")
        self.assertFalse(err, body)
        self.assertEqual(body["run"]["unparsed_lines"], ["this is not a JSON event"])
        err, body = self.call({"prompt": "x", "cwd": self.work})
        self.assertEqual(body["run"]["unparsed_lines"], [])

    def test_ok_call_uses_default_sandbox_and_cwd(self):
        err, body = self.call({"prompt": "ping", "cwd": self.work})
        self.assertFalse(err, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "PONG")
        self.assertIsNone(body["report"])
        self.assertEqual(body["run"]["sandbox"], "workspace-write")
        self.assertEqual(body["run"]["usage"], {"output_tokens": 1})
        got = self.fake_saw()
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(self.work))
        self.assertIn('sandbox_mode="workspace-write"', got["argv"])
        self.assertIn("--ephemeral", got["argv"])
        self.assertNotIn("--output-schema", got["argv"])
        self.assertEqual(got["argv"][-2:], ["--", "ping"])

    def test_explicit_read_only(self):
        err, body = self.call({"prompt": "ping", "cwd": self.work, "sandbox": "read-only"})
        self.assertFalse(err, body)
        self.assertIn('sandbox_mode="read-only"', self.fake_saw()["argv"])

    def test_nonzero_exit_is_error(self):
        err, body = self.call({"prompt": "ping", "cwd": self.work}, mode="fail")
        self.assertTrue(err)
        self.assertFalse(body["ok"])
        self.assertIn("boom", body["error"])

    def test_nonzero_exit_with_message_is_error(self):
        err, body = self.call({"prompt": "ping", "cwd": self.work}, mode="msgfail")
        self.assertTrue(err)
        self.assertEqual(body["run"]["exit"], 3)

    def test_no_message_is_error(self):
        err, body = self.call({"prompt": "ping", "cwd": self.work}, mode="silent")
        self.assertTrue(err)
        self.assertIn("no final message", body["error"])

    def test_danger_sandbox_refused_without_running(self):
        err, _ = self.call({"prompt": "x", "cwd": self.work, "sandbox": "danger-full-access"})
        self.assertTrue(err)
        self.assertFalse(os.path.exists(self.log))

    def test_relative_cwd_refused(self):
        err, _ = self.call({"prompt": "x", "cwd": "relative/dir"})
        self.assertTrue(err)
        self.assertFalse(os.path.exists(self.log))

    def test_unknown_report_kind_refused_without_running(self):
        err, _ = self.call({"prompt": "x", "cwd": self.work, "report": "essay"})
        self.assertTrue(err)
        self.assertFalse(os.path.exists(self.log))

    def test_timeout_is_error(self):
        t0 = time.monotonic()
        err, body = self.call({"prompt": "x", "cwd": self.work}, mode="sleep",
                              CODEX_MCP_TIMEOUT="1")
        self.assertTrue(err)
        self.assertIn("timed out", body["error"])
        self.assertLess(time.monotonic() - t0, 15)  # the hung child was killed, not waited out

    # --- observed facts ---------------------------------------------------------

    def test_commands_are_observed_with_exit_codes(self):
        err, body = self.call({"prompt": "x", "cwd": self.work},
                              FAKE_CMDS=[["ls", 0], ["make test", 2]])
        self.assertFalse(err, body)
        self.assertEqual(body["observed"]["commands"],
                         [{"command": "ls", "exit_code": 0},
                          {"command": "make test", "exit_code": 2}])

    def test_files_changed_from_git_snapshot(self):
        repo = self.make_repo()
        with open(os.path.join(repo, "dirty.txt"), "w") as f:
            f.write("dirty before the run")  # already modified, then Codex edits it again
        with open(os.path.join(repo, "stale.txt"), "w") as f:
            f.write("untracked before, untouched by Codex")
        err, body = self.call({"prompt": "x", "cwd": repo}, FAKE_EDITS=[
            ["a.txt", "changed"], ["new.txt", "new"], ["dirty.txt", "dirtier"],
            ["untouched.txt", None]])
        self.assertFalse(err, body)
        self.assertEqual(body["observed"]["files_changed"],
                         ["a.txt", "dirty.txt", "new.txt", "untouched.txt"])
        self.assertTrue(body["observed"]["files_basis"].startswith("git-snapshot"))

    def test_files_changed_unknown_outside_git(self):
        err, body = self.call({"prompt": "x", "cwd": self.work}, FAKE_EDITS=[["f.txt", "x"]])
        self.assertFalse(err, body)
        self.assertIsNone(body["observed"]["files_changed"])  # unknown, not "none"
        self.assertIn("not a git repo", body["observed"]["files_basis"])

    # --- reports ----------------------------------------------------------------

    def test_task_report_parsed_and_cross_checked(self):
        repo = self.make_repo()
        err, body = self.call({"prompt": "x", "cwd": repo, "report": "task"},
                              FAKE_EDITS=[["a.txt", "changed"], ["sneaky.txt", "y"]],
                              FAKE_MESSAGE=json.dumps(TASK_REPORT))
        self.assertFalse(err, body)
        self.assertEqual(body["report"], TASK_REPORT)
        self.assertEqual(body["mismatch"], {"claimed_not_observed": ["ghost.txt"],
                                            "observed_not_claimed": ["sneaky.txt"]})
        schema = self.fake_saw()["schema"]
        self.assertIn("test_result", schema["required"])

    def test_review_report_uses_review_schema(self):
        review = {"verdict": "approve", "summary": "fine", "findings": [],
                  "checked": ["a.txt"], "confidence": "medium"}
        err, body = self.call({"prompt": "x", "cwd": self.work, "report": "review"},
                              FAKE_MESSAGE=json.dumps(review))
        self.assertFalse(err, body)
        self.assertEqual(body["report"], review)
        self.assertIn("verdict", self.fake_saw()["schema"]["required"])
        self.assertIsNone(body["mismatch"])  # nothing to cross-check without files_changed

    def test_report_that_is_not_json_is_error(self):
        err, body = self.call({"prompt": "x", "cwd": self.work, "report": "task"},
                              FAKE_MESSAGE="I did stuff")
        self.assertTrue(err)
        self.assertIn("report", body["error"])

    def test_report_missing_required_key_is_error(self):
        partial = dict(TASK_REPORT)
        del partial["test_result"]
        err, body = self.call({"prompt": "x", "cwd": self.work, "report": "task"},
                              FAKE_MESSAGE=json.dumps(partial))
        self.assertTrue(err)
        self.assertIn("test_result", body["error"])

    # --- worktree ---------------------------------------------------------------

    def test_worktree_isolates_codex_from_main_tree(self):
        repo = self.make_repo()
        err, body = self.call({"prompt": "x", "cwd": repo, "worktree": True},
                              FAKE_EDITS=[["a.txt", "changed in worktree"]])
        self.assertFalse(err, body)
        wt = body["worktree"]
        self.assertTrue(wt["branch"].startswith("codex/"))
        self.assertEqual(os.path.realpath(self.fake_saw()["cwd"]), os.path.realpath(wt["path"]))
        self.assertEqual(git(repo, "status", "--porcelain"), "")  # main tree untouched
        with open(os.path.join(wt["path"], "a.txt")) as f:
            self.assertEqual(f.read(), "changed in worktree")
        self.assertEqual(body["observed"]["files_changed"], ["a.txt"])
        self.assertIn("isolated worktree", body["observed"]["files_basis"])
        self.assertIn(os.path.realpath(wt["path"]),
                      [os.path.realpath(l.split()[1]) for l in
                       git(repo, "worktree", "list", "--porcelain").splitlines()
                       if l.startswith("worktree ")])

    def test_worktree_keeps_subdirectory(self):
        repo = self.make_repo()
        os.mkdir(os.path.join(repo, "sub"))
        with open(os.path.join(repo, "sub", "s.txt"), "w") as f:
            f.write("s")
        git(repo, "add", ".")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub")
        err, body = self.call({"prompt": "x", "cwd": os.path.join(repo, "sub"),
                               "worktree": True})
        self.assertFalse(err, body)
        self.assertEqual(os.path.realpath(self.fake_saw()["cwd"]),
                         os.path.realpath(os.path.join(body["worktree"]["path"], "sub")))

    def test_worktree_outside_git_refused_without_running(self):
        err, _ = self.call({"prompt": "x", "cwd": self.work, "worktree": True})
        self.assertTrue(err)
        self.assertFalse(os.path.exists(self.log))

    # --- run log ----------------------------------------------------------------

    def test_logged_failure_says_why(self):
        self.call({"prompt": "x", "cwd": self.work}, mode="fail")
        self.assertIn("boom", self.runs()[0]["error"])
        self.call({"prompt": "x", "cwd": self.work})
        self.assertIsNone(self.runs()[1]["error"])

    def test_every_run_is_logged_including_failures(self):
        self.call({"prompt": "first", "cwd": self.work})
        self.call({"prompt": "second", "cwd": self.work}, mode="fail")
        runs = self.runs()
        self.assertEqual([r["ok"] for r in runs], [True, False])
        self.assertEqual(runs[0]["usage"], {"output_tokens": 1})
        self.assertEqual(runs[0]["sandbox"], "workspace-write")
        self.assertEqual(runs[1]["exit"], 3)
        for key in ("ts", "host", "cwd", "duration_s", "report", "worktree", "spend"):
            self.assertIn(key, runs[0])
        self.assertEqual(oct(os.stat(self.runlog).st_mode & 0o777), "0o600")

    def test_refused_call_is_not_logged(self):
        self.call({"prompt": "x", "cwd": "relative"})
        self.assertFalse(os.path.exists(self.runlog))

    def test_log_failure_is_reported_not_swallowed(self):
        err, body = self.call({"prompt": "x", "cwd": self.work},
                              CODEX_MCP_LOG=os.path.join(self.tmp.name, "no", "such", "x.jsonl"))
        self.assertFalse(err, body)  # Codex already ran; its answer still comes back
        self.assertTrue(body["log_error"])


if __name__ == "__main__":
    unittest.main()
