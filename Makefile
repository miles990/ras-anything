# Thin wrappers around ./install.sh (POSIX). Idempotent; safe to re-run.
#   make install     # CLI + ~/.claude/skills + ~/.codex/skills
#   make uninstall   # remove only what install.sh wrote

.PHONY: install uninstall help

help:
	@echo "Targets:"
	@echo "  make install     install the ras-anything CLI and agent skills (idempotent)"
	@echo "  make uninstall   remove the CLI shim/venv and skill copies this repo installed"

install:
	./install.sh

uninstall:
	./install.sh --uninstall
