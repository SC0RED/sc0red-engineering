.PHONY: check check-structure no-symlinks check-workspace check-system-docs \
        validate-workspace tool-refs help

.DEFAULT_GOAL := check

# Sibling checkouts this repo is checked against. Override on the command line
# or in the environment when they live elsewhere.
#   ENGINEERING_STANDARDS  engineering-standards checkout; its shared/ is the
#                          sibling every template's {{system-doc:...}} resolves
#                          against (the box gets it from its deploy-shared)
#   AGENCY_TOOLS           agency-tools checkout the tool refs must resolve in
#   AGENCY_BIN             an `agency` binary built from Agency development
ENGINEERING_STANDARDS ?= ../agency-standards
AGENCY_TOOLS ?= ../agency-tools
AGENCY_BIN ?= $(shell command -v agency 2>/dev/null)
AGENTS := patch scarlett

check: check-structure check-system-docs validate-workspace tool-refs ## Every check (bare make)
	@echo "sc0red-engineering: every check passed"

# The checks that need no private sibling checkout: the subset Actions runs,
# since this repo has no credentials for engineering-standards, agency-tools
# or Agency.
check-structure: no-symlinks check-workspace ## Checks that need no sibling checkout (Actions)

# The Agency runtime rejects a symlink that escapes the workspace root, and the
# shared docs are a real sibling directory on the box, never a link.
no-symlinks:
	@found=$$(find workspaces -type l); \
	if [ -n "$$found" ]; then echo "symlink(s) under workspaces/, which the runtime refuses:"; echo "$$found"; exit 1; fi; \
	echo "no-symlinks: OK"

# The design's promises: stage rules, pinned allowlists, timeouts, the board
# check that never runs an LLM, concurrency. Self-test first.
check-workspace:
	@python3 -c "import yaml" 2>/dev/null || pip3 install --quiet pyyaml
	@python3 scripts/check_workspace.py --self-test >/dev/null || (python3 scripts/check_workspace.py --self-test; exit 1)
	@python3 scripts/check_workspace.py

check-system-docs:
	@test -d "$(ENGINEERING_STANDARDS)/shared" || { echo "ENGINEERING_STANDARDS=$(ENGINEERING_STANDARDS) has no shared/"; exit 1; }
	@python3 scripts/check_workspace.py --shared "$(ENGINEERING_STANDARDS)/shared"

# The daemon's own loader, on the layout the box has: each workspace beside a
# shared/ stamped from engineering-standards.
validate-workspace:
	@test -x "$(AGENCY_BIN)" || { echo "AGENCY_BIN must name an agency binary built from Agency development (got '$(AGENCY_BIN)')"; exit 1; }
	@test -d "$(ENGINEERING_STANDARDS)/shared" || { echo "ENGINEERING_STANDARDS=$(ENGINEERING_STANDARDS) has no shared/"; exit 1; }
	@stage=$$(mktemp -d -t sc0red-engineering-validate.XXXXXX); trap 'rm -rf "$$stage"' EXIT; \
	cp -R workspaces/patch workspaces/scarlett "$$stage/"; \
	cp -R "$(ENGINEERING_STANDARDS)/shared" "$$stage/shared"; \
	for agent in $(AGENTS); do \
		"$(AGENCY_BIN)" validate-workspace "$$stage/$$agent" || exit 1; \
	done

# Every module.python reference resolves in agency-tools.
tool-refs:
	@test -f "$(AGENCY_TOOLS)/scripts/validate_workspace.py" || { echo "AGENCY_TOOLS=$(AGENCY_TOOLS) is not an agency-tools checkout"; exit 1; }
	@for agent in $(AGENTS); do \
		python3 "$(AGENCY_TOOLS)/scripts/validate_workspace.py" --workspace workspaces/$$agent --tools-root "$(AGENCY_TOOLS)" || exit 1; \
	done
	@echo "tool-refs: OK"

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n",$$1,$$2}'
