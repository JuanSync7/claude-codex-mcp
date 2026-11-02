"""Drives tools/install.py against a fake `claude` CLI and a throwaway Claude config."""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
INSTALL = os.path.join(REPO, "tools", "install.py")
SERVER = os.path.join(REPO, "codex_mcp", "server.py")

# Implements just `claude mcp add-json -s user <name> <json>` and `claude mcp remove -s user
# <name>` on the JSON file in FAKE_CLAUDE_CONFIG, and logs each call. FAKE_CLAUDE_FAIL=1 fails;
# FAKE_CLAUDE_NOOP=1 exits 0 without changing anything.
FAKE_CLAUDE = textwrap.dedent("""\
    #!%s
    import json, os, sys
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        sys.stderr.write("refused\\n"); sys.exit(1)
    if os.environ.get("FAKE_CLAUDE_NOOP"):
        sys.exit(0)
    path = os.environ["FAKE_CLAUDE_CONFIG"]
    d = json.load(open(path)) if os.path.exists(path) else {}
    servers = d.setdefault("mcpServers", {})
    a = sys.argv[1:]
    if a[:2] == ["mcp", "add-json"]:
        servers[a[4]] = json.loads(a[5])
    elif a[:2] == ["mcp", "remove"]:
        if a[4] not in servers:
            sys.exit(1)
        del servers[a[4]]
    elif a[:2] == ["mcp", "get"]:
        print(json.dumps(servers.get(a[2]))); sys.exit(0)
    json.dump(d, open(path, "w"))
    """) % sys.executable


# `codex login status` succeeds unless FAKE_CODEX_LOGGED_OUT is set.
FAKE_CODEX = """#!/bin/sh
if [ "$1 $2" = "login status" ] && [ -n "$FAKE_CODEX_LOGGED_OUT" ]; then
    echo "Not logged in"; exit 1
fi
"""


def executable(path, text="#!/bin/sh\n"):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.claude = executable(os.path.join(t, "claude"), FAKE_CLAUDE)
        self.codex = executable(os.path.join(t, "codex"), FAKE_CODEX)
        self.claude_md = os.path.join(t, "CLAUDE.md")
        self.claude_config = os.path.join(t, "claude.json")
        self.claude_log = os.path.join(t, "claude-calls.jsonl")
        self.config = os.path.join(t, "config.env")
        self.values = {
            "CODEX_MCP_PYTHON": sys.executable,
            "CODEX_MCP_BIN": self.codex,
            "CODEX_MCP_HOST": socket.gethostname(),
            "CODEX_MCP_DEFAULT_SANDBOX": "workspace-write",
            "CODEX_MCP_TIMEOUT": "1800",
            "CODEX_MCP_MAX_PARALLEL": "2",
            "CODEX_MCP_LOG": os.path.join(t, "state", "runs.jsonl"),
            "CLAUDE_BIN": self.claude,
            "CLAUDE_CONFIG": self.claude_config,
            "CLAUDE_MD": self.claude_md,
        }
        self.write_config()
        with open(self.claude_md, "w") as f:
            f.write("# My instructions\n\nBe terse.\n")
        code, out = self.run_install("routing")
        assert code == 0, out

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, **change):
        self.values.update(change)
        with open(self.config, "w") as f:
            f.write("# test config\n\n")
            for k, v in self.values.items():
                if v is not None:
                    f.write("%s=%s\n" % (k, v))

    def run_install(self, *args, **env):
        e = dict(os.environ, FAKE_CLAUDE_LOG=self.claude_log,
                 FAKE_CLAUDE_CONFIG=self.claude_config)
        e.update(env)
        p = subprocess.run([sys.executable, INSTALL] + list(args) + ["--config", self.config],
                           env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=60)
        return p.returncode, p.stdout

    def calls(self):
        if not os.path.exists(self.claude_log):
            return []
        with open(self.claude_log) as f:
            return [json.loads(line)[:2] for line in f]

    def entry(self):
        if not os.path.exists(self.claude_config):
            return None
        with open(self.claude_config) as f:
            return json.load(f).get("mcpServers", {}).get("codex")

    def assert_exit_line(self, out, code, name):
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("exit: %d %s" % (code, name)),
                        out)

    # --- install ----------------------------------------------------------------

    def test_install_registers_entry_from_config(self):
        code, out = self.run_install("install")
        self.assertEqual(code, 0, out)
        self.assert_exit_line(out, 0, "OK")
        self.assertEqual(self.calls(), [["mcp", "add-json"]])
        e = self.entry()
        self.assertEqual(e["type"], "stdio")
        self.assertEqual(e["command"], sys.executable)
        self.assertEqual(e["args"], [SERVER])
        self.assertEqual(e["env"]["CODEX_MCP_HOST"], socket.gethostname())
        self.assertEqual(e["env"]["CODEX_MCP_DEFAULT_SANDBOX"], "workspace-write")
        self.assertEqual(e["env"]["CODEX_MCP_LOG"], self.values["CODEX_MCP_LOG"])
        self.assertNotIn("CLAUDE_BIN", e["env"])  # installer keys stay out of the server env
        log_dir = os.path.dirname(self.values["CODEX_MCP_LOG"])
        self.assertEqual(oct(os.stat(log_dir).st_mode & 0o777), "0o700")

    def test_existing_log_dir_is_made_private(self):
        log_dir = os.path.dirname(self.values["CODEX_MCP_LOG"])
        os.makedirs(log_dir)
        os.chmod(log_dir, 0o755)
        code, out = self.run_install("install")
        self.assertEqual(code, 0, out)
        self.assertEqual(oct(os.stat(log_dir).st_mode & 0o777), "0o700")

    def test_second_install_changes_nothing(self):
        self.run_install("install")
        with open(self.claude_config) as f:
            before = f.read()
        code, out = self.run_install("install")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [["mcp", "add-json"]])  # no second call
        with open(self.claude_config) as f:
            self.assertEqual(f.read(), before)

    def test_changed_config_replaces_entry(self):
        self.run_install("install")
        self.write_config(CODEX_MCP_DEFAULT_SANDBOX="read-only")
        code, out = self.run_install("install")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [["mcp", "add-json"], ["mcp", "remove"],
                                        ["mcp", "add-json"]])
        self.assertEqual(self.entry()["env"]["CODEX_MCP_DEFAULT_SANDBOX"], "read-only")

    def test_claude_failure_is_reported(self):
        code, out = self.run_install("install", FAKE_CLAUDE_FAIL="1")
        self.assertEqual(code, 1, out)
        self.assert_exit_line(out, 1, "FAILED")
        self.assertIn("refused", out)

    def test_claude_success_without_effect_is_caught(self):
        code, out = self.run_install("install", FAKE_CLAUDE_NOOP="1")
        self.assertEqual(code, 1, out)
        self.assertIn("does not match", out)
        self.run_install("install")
        code, out = self.run_install("uninstall", FAKE_CLAUDE_NOOP="1")
        self.assertEqual(code, 1, out)
        self.assertIn("still in", out)

    # --- preflight: everything is checked before anything changes -------------

    def test_wrong_host_refused(self):
        self.write_config(CODEX_MCP_HOST="not-" + socket.gethostname())
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assertIn("set it to the output of `hostname`", out)  # caught by the installer itself
        self.assertEqual(self.calls(), [])

    def test_codex_logged_out_refused(self):
        code, out = self.run_install("install", FAKE_CODEX_LOGGED_OUT="1")
        self.assertEqual(code, 3, out)
        self.assertIn("codex login", out)
        self.assertEqual(self.calls(), [])

    def test_server_that_does_not_start_is_not_registered(self):
        broken = executable(os.path.join(self.tmp.name, "python-broken"),
                            "#!/bin/sh\necho boom >&2\nexit 1\n")
        self.write_config(CODEX_MCP_PYTHON=broken)
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)  # a preflight problem, like the others
        self.assertIn("boom", out)
        self.assertEqual(self.calls(), [])

    def test_missing_routing_rule_refused_and_shown(self):
        os.remove(self.claude_md)
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assertIn("make routing", out)
        self.assertIn("mcp__codex__codex", out)  # the rule itself is shown
        self.assertEqual(self.calls(), [])

    def test_stale_routing_rule_refused(self):
        with open(self.claude_md) as f:
            text = f.read()
        with open(self.claude_md, "w") as f:
            f.write(text.replace("Codex", "Kodex"))
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assertIn("out of date", out)

    def test_all_preflight_problems_in_one_run(self):
        os.remove(self.claude_md)
        code, out = self.run_install("install", FAKE_CODEX_LOGGED_OUT="1")
        self.assertEqual(code, 3, out)
        self.assertIn("codex login", out)
        self.assertIn("make routing", out)

    # --- routing rule -------------------------------------------------------------

    def test_routing_is_added_once_and_keeps_other_text(self):
        with open(self.claude_md) as f:
            after_first = f.read()
        self.assertTrue(after_first.startswith("# My instructions\n\nBe terse.\n"))
        self.assertIn("mcp__codex__codex", after_first)
        code, out = self.run_install("routing")
        self.assertEqual(code, 0, out)
        with open(self.claude_md) as f:
            self.assertEqual(f.read(), after_first)

    def test_routing_replaces_stale_block_in_place(self):
        with open(self.claude_md) as f:
            good = f.read()
        with open(self.claude_md, "w") as f:
            f.write(good.replace("Codex", "Kodex") + "\nAfter.\n")
        self.assertEqual(self.run_install("routing")[0], 0)
        with open(self.claude_md) as f:
            self.assertEqual(f.read(), good + "\nAfter.\n")

    def test_uninstall_removes_routing_block_only(self):
        self.run_install("install")
        self.assertEqual(self.run_install("uninstall")[0], 0)
        with open(self.claude_md) as f:
            self.assertEqual(f.read(), "# My instructions\n\nBe terse.\n")

    def test_status_reports_missing_routing(self):
        self.run_install("install")
        os.remove(self.claude_md)
        code, out = self.run_install("status")
        self.assertEqual(code, 1, out)
        self.assertIn("routing", out)

    # --- nothing ends without an exit line ---------------------------------------

    def test_unreadable_config_has_exit_line(self):
        os.chmod(self.config, 0)
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assert_exit_line(out, 3, "CONFIG")

    def test_unexpected_error_has_exit_line_not_traceback(self):
        with open(self.claude_config, "w") as f:
            f.write("[]")  # valid JSON, wrong shape
        code, out = self.run_install("install")
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        self.assert_exit_line(out, 1, "FAILED")

    # --- config -----------------------------------------------------------------

    def test_optional_keys_announce_their_default(self):
        self.write_config(CODEX_MCP_TIMEOUT=None)
        code, out = self.run_install("install")
        self.assertEqual(code, 0, out)
        self.assertIn("[using default] CODEX_MCP_TIMEOUT = 1800", out)
        self.assertNotIn("[using default] CODEX_MCP_MAX_PARALLEL", out)
        self.assertEqual(self.entry()["env"]["CODEX_MCP_TIMEOUT"], "1800")

    def test_all_missing_keys_listed_in_one_run(self):
        self.write_config(CODEX_MCP_BIN=None, CODEX_MCP_HOST=None)
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assert_exit_line(out, 3, "CONFIG")
        self.assertIn("[not set] CODEX_MCP_BIN", out)
        self.assertIn("[not set] CODEX_MCP_HOST", out)
        self.assertEqual(self.calls(), [])

    def test_empty_value_counts_as_not_set(self):
        self.write_config(CODEX_MCP_HOST="")
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assertIn("[not set] CODEX_MCP_HOST", out)

    def test_unknown_key_refused(self):
        with open(self.config, "a") as f:
            f.write("CODEX_MCP_TYPO=1\n")
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assertIn("CODEX_MCP_TYPO", out)
        self.assertEqual(self.calls(), [])

    def test_bad_values_refused(self):
        for key, value in [("CODEX_MCP_DEFAULT_SANDBOX", "danger-full-access"),
                           ("CODEX_MCP_TIMEOUT", "soon"), ("CODEX_MCP_MAX_PARALLEL", "0"),
                           ("CODEX_MCP_BIN", os.path.join(self.tmp.name, "missing")),
                           ("CLAUDE_BIN", os.path.join(self.tmp.name, "missing"))]:
            with self.subTest(key=key):
                good = self.values[key]
                self.write_config(**{key: value})
                code, out = self.run_install("install")
                self.assertEqual(code, 3, out)
                self.assertIn(key, out)
                self.write_config(**{key: good})
        self.assertEqual(self.calls(), [])

    def test_missing_config_file_is_config_error(self):
        os.remove(self.config)
        code, out = self.run_install("install")
        self.assertEqual(code, 3, out)
        self.assertIn("config.example.env", out)

    def test_bad_command_is_usage(self):
        code, out = self.run_install("instal")
        self.assertEqual(code, 2, out)
        self.assert_exit_line(out, 2, "USAGE")

    # --- uninstall and status ---------------------------------------------------

    def test_uninstall(self):
        code, out = self.run_install("uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])  # nothing installed, nothing to do
        self.run_install("routing")  # uninstall also removed the routing rule
        self.run_install("install")
        code, out = self.run_install("uninstall")
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.entry())
        self.assertEqual(self.calls()[-1], ["mcp", "remove"])

    def test_status(self):
        self.assertEqual(self.run_install("status")[0], 1)  # not installed
        self.run_install("install")
        self.assertEqual(self.run_install("status")[0], 0)  # installed as configured
        self.write_config(CODEX_MCP_TIMEOUT="60")
        self.assertEqual(self.run_install("status")[0], 1)  # drifted from config

    # --- leak check -------------------------------------------------------------

    def make_tree(self, files):
        root = os.path.join(self.tmp.name, "tree")
        os.mkdir(root)
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        for name, text in files.items():
            with open(os.path.join(root, name), "w") as f:
                f.write(text)
        return root

    def test_leakcheck_clean_tree(self):
        root = self.make_tree({"README.md": "set CODEX_MCP_HOST in config.env\n"})
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 0, out)

    def test_leakcheck_finds_configured_host_and_home(self):
        self.write_config(CODEX_MCP_HOST="build-host.example.internal")
        root = self.make_tree({
            "a.md": "runs on build-host.example.internal\n",
            "b.py": "LOG = '%s/x'\n" % os.path.expanduser("~"),
            "c.md": "the example.internal domain\n",
        })
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 1, out)
        for name in ("a.md", "b.py", "c.md"):
            self.assertIn(name, out)

    def test_leakcheck_words_from_config(self):
        root = self.make_tree({"a.md": "built at Widgetcorp\n"})
        self.assertEqual(self.run_install("leakcheck", "--root", root)[0], 0)
        self.write_config(LEAKCHECK_WORDS="Widgetcorp, other")
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 1, out)
        self.assertIn("a.md:1: contains LEAKCHECK_WORDS", out)

    def test_leakcheck_announces_values_it_cannot_search(self):
        self.write_config(CODEX_MCP_BIN="codex")  # a bare name, as CI might have
        root = self.make_tree({"a.md": "run codex\n"})
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 0, out)
        self.assertIn("not searching for CODEX_MCP_BIN 'codex'", out)

    def test_leakcheck_ignores_gitignored_files(self):
        self.write_config(CODEX_MCP_HOST="build-host.example.internal")
        root = self.make_tree({".gitignore": "config.env\n",
                               "config.env": "CODEX_MCP_HOST=build-host.example.internal\n"})
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 0, out)

    def test_leakcheck_over_no_files_fails(self):
        root = self.make_tree({})
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 1, out)

    def test_leakcheck_without_config_still_checks_home(self):
        root = self.make_tree({"b.py": "LOG = '%s/x'\n" % os.path.expanduser("~")})
        os.remove(self.config)
        code, out = self.run_install("leakcheck", "--root", root)
        self.assertEqual(code, 1, out)
        self.assertIn("b.py", out)


if __name__ == "__main__":
    unittest.main()
