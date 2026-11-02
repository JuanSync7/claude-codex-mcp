# Host-specific values live in config.env (gitignored; copy config.example.env).
# Make only needs the interpreter from it; tools/install.py reads and checks the rest.
-include config.env
PYTHON := $(or $(CODEX_MCP_PYTHON),python3)

.PHONY: help test leakcheck verify install routing uninstall status

help:
	@echo "make verify     tests + leak check (the gate; run before every commit)"
	@echo "make install    check everything, then register the 'codex' MCP server from config.env"
	@echo "make routing    add or update the rule in CLAUDE.md that tells Claude when to use Codex"
	@echo "make status     does the registered entry match config.env?"
	@echo "make uninstall  remove the entry"

# Tests write only to throwaway dirs in RAM; they never touch the real Claude config or codex.
test:
	TMPDIR=/dev/shm $(PYTHON) -B -m unittest discover -s tests -v

leakcheck:
	$(PYTHON) tools/install.py leakcheck

verify: test leakcheck

install routing uninstall status:
	$(PYTHON) tools/install.py $@
