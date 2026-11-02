"""The README config table must match the keys and defaults in the code."""

import importlib.util
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def readme_config_rows():
    """{key: (required, default)} from the README's config table."""
    with open(os.path.join(REPO, "README.md")) as f:
        text = f.read()
    section = text.split("## Config", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"\| `([A-Z_]+)` \| (yes|no) \|\s*`?([^|`]*?)`?\s*\|", line)
        if m:
            rows[m.group(1)] = (m.group(2) == "yes", m.group(3).strip())
    return rows


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.install = load("install", "tools/install.py")
        self.server = load("server", "codex_mcp/server.py")
        self.rows = readme_config_rows()

    def test_table_lists_exactly_the_installer_keys(self):
        self.assertTrue(self.rows)  # a table that failed to parse would pass the rest
        want = {k: (True, "") for k in self.install.REQUIRED}
        want.update({k: (False, v) for k, v in self.install.OPTIONAL.items()})
        self.assertEqual(self.rows, want)

    def test_server_defaults_match_installer_defaults(self):
        for key in self.install.SERVER_KEYS:
            if key in self.install.OPTIONAL:
                self.assertEqual(self.server.DEFAULTS[key],
                                 os.path.expanduser(self.install.OPTIONAL[key]), key)

    def test_every_server_key_is_documented(self):
        with open(os.path.join(REPO, "README.md")) as f:
            text = f.read()
        for key in self.server.DEFAULTS:
            self.assertIn("`%s`" % key, text)

    def test_exit_code_table_matches_both_programs(self):
        with open(os.path.join(REPO, "README.md")) as f:
            section = f.read().split("## Exit codes", 1)[1].split("\n## ", 1)[0]
        rows = {}
        for line in section.splitlines():
            m = re.match(r"\| (\d) \| ([A-Z]+) \|([^|]*)\|([^|]*)\|$", line)
            if m:
                rows[int(m.group(1))] = (m.group(2), m.group(3).strip(), m.group(4).strip())
        self.assertTrue(rows)
        self.assertEqual({c: n for c, (n, _, _) in rows.items()}, self.install.EXIT_NAMES)
        self.assertEqual({c for c, (_, _, srv) in rows.items() if srv}, set(self.server.EXIT_NAMES))
        for code, name in self.server.EXIT_NAMES.items():
            self.assertEqual(rows[code][0], name)

    def test_every_make_target_is_documented(self):
        with open(os.path.join(REPO, "Makefile")) as f:
            phony = re.search(r"^\.PHONY:(.*)$", f.read(), re.M).group(1).split()
        with open(os.path.join(REPO, "README.md")) as f:
            text = f.read()
        self.assertTrue(phony)
        for target in phony:
            if target != "help":
                self.assertIn("make %s" % target, text)

    def test_example_config_lists_every_key(self):
        with open(os.path.join(REPO, "config.example.env")) as f:
            keys = {l.split("=", 1)[0] for l in f if re.match(r"[A-Z_]+=", l)}
        self.assertEqual(keys, set(self.install.REQUIRED) | set(self.install.OPTIONAL))


if __name__ == "__main__":
    unittest.main()
