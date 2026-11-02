# Host-specific values live in config.env (gitignored; copy config.example.env).
# Make only needs the interpreter from it; tools/install.py reads and checks the rest.
-include config.env
PYTHON := $(or $(CODEX_MCP_PYTHON),python3)

.PHONY: help test leakcheck verify install uninstall status

help:
	@echo "make verify     tests + leak check (the gate; run before every commit)"
	@echo "make install    register the 'codex' MCP server for Claude Code from config.env"
	@echo "make status     does the registered entry match config.env?"
	@echo "make uninstall  remove the entry"

# Tests write only to throwaway dirs in RAM; they never touch the real Claude config or codex.
test:
	TMPDIR=/dev/shm $(PYTHON) -B -m unittest discover -s tests -v

leakcheck:
	$(PYTHON) tools/install.py leakcheck

verify: test leakcheck

install uninstall status:
	$(PYTHON) tools/install.py $@
