.PHONY: check check-structure no-symlinks check-workspace test-sync check-system-docs \
        validate-workspace tool-refs vendor-shared pyyaml sync-tools install-sync help

.DEFAULT_GOAL := check

# Sibling checkouts this repo is checked against. Override on the command line
# or in the environment when they live elsewhere.
#   ENGINEERING_STANDARDS  engineering-standards checkout; workspaces/shared/
#                          vendors exactly the docs of its shared/ that the
#                          templates cite ({{system-doc:shared/...}}), unedited
#   AGENCY_TOOLS           agency-tools the tool refs must resolve in; by
#                          default the release the daemons run (sync-tools)
#   AGENCY_BIN             an `agency` binary built from Agency development
ENGINEERING_STANDARDS ?= ../agency-standards
AGENCY_TOOLS ?= $(AGENCY_RUNTIME_DIR)/current/agency-tools
AGENCY_BIN ?= $(shell command -v agency 2>/dev/null)
AGENTS := patch scarlett
# Where sync-tools installs the box's agency-tools + contracts, and the launchd
# job that refreshes it. Fixed (`override`, so neither the command line nor the
# environment changes it): both agent.env files and check_workspace.py name it.
override AGENCY_RUNTIME_DIR := /Volumes/SSD/agency-runtime
SYNC_LABEL := com.sc0red.agency-tools-sync
SYNC_PLIST := $(HOME)/Library/LaunchAgents/$(SYNC_LABEL).plist

check: check-structure check-system-docs validate-workspace tool-refs ## Every check (bare make)
	@echo "sc0red-engineering: every check passed"

# The checks that need no private sibling checkout: the subset Actions runs,
# since this repo has no credentials for engineering-standards, agency-tools
# or Agency.
check-structure: no-symlinks check-workspace test-sync ## Checks that need no sibling checkout (Actions)

# The Agency runtime rejects a symlink that escapes the workspace root, and the
# vendored shared docs are a real sibling directory, never a link.
no-symlinks:
	@found=$$(find workspaces -type l); \
	if [ -n "$$found" ]; then echo "symlink(s) under workspaces/, which the runtime refuses:"; echo "$$found"; exit 1; fi; \
	echo "no-symlinks: OK"

# The design's promises: stage rules, pinned allowlists, timeouts, the board
# check that never runs an LLM, concurrency. Self-test first.
check-workspace: pyyaml
	@python3 scripts/check_workspace.py --self-test >/dev/null || (python3 scripts/check_workspace.py --self-test; exit 1)
	@python3 scripts/check_workspace.py

# scripts/sync_agency_tools.sh and scripts/run_on_release.sh against a fake gh,
# a fake uv and a local agency-tools origin, all in temporary directories under
# the repo (gitignored .validate.*/).
test-sync:
	@stage=$$(mktemp -d "$(CURDIR)/.validate.XXXXXX") || { echo "test-sync: could not make a staging directory"; exit 1; }; \
	trap 'rm -rf "$$stage"' EXIT; \
	TMPDIR="$$stage" python3 scripts/test_sync_agency_tools.py >"$$stage/out" 2>&1 \
		&& echo "test-sync: OK" || { cat "$$stage/out"; exit 1; }

# Run the launchd sync job now. Only launchd runs scripts/sync_agency_tools.sh,
# and it never starts the job while a run is alive, so syncs never overlap;
# kickstart without -k leaves a running sync alone. Output: the sync log.
sync-tools: ## Run the agency-tools sync job now (after install-sync)
	@launchctl print "gui/$$(id -u)/$(SYNC_LABEL)" >/dev/null 2>&1 || { echo "sync-tools: $(SYNC_LABEL) is not loaded; run make install-sync"; exit 1; }
	@launchctl kickstart -p "gui/$$(id -u)/$(SYNC_LABEL)" && echo "sync-tools: started; see $(AGENCY_RUNTIME_DIR)/sync.log"

# Copy the sync script and the daemon launcher into $(AGENCY_RUNTIME_DIR)/bin
# (each replaced by a rename, so a run never reads a half-copied file), then
# render the launchd job (every 10 minutes and at load) against that copy and
# (re)load it: the job never depends on this checkout. Rerun after changing
# either script. PATH is the directories of the gh, uv, git and python3 this
# shell resolves.
install-sync: ## Install the scripts + load the launchd job that runs the sync
	@for tool in gh uv git python3; do command -v $$tool >/dev/null || { echo "install-sync: no $$tool on PATH"; exit 1; }; done; \
	path=$$(for tool in gh uv git python3; do dirname "$$(command -v $$tool)"; done | awk '!seen[$$0]++' | paste -sd: -):/usr/bin:/bin; \
	mkdir -p "$(AGENCY_RUNTIME_DIR)/bin" "$(dir $(SYNC_PLIST))" || exit 1; \
	install -S -m 0755 scripts/sync_agency_tools.sh scripts/run_on_release.sh "$(AGENCY_RUNTIME_DIR)/bin/" || exit 1; \
	new="$(SYNC_PLIST).new"; trap 'rm -f "$$new"' EXIT; \
	sed -e "s|@SCRIPT@|$(AGENCY_RUNTIME_DIR)/bin/sync_agency_tools.sh|g" -e "s|@RUNTIME@|$(AGENCY_RUNTIME_DIR)|g" \
		-e "s|@PATH@|$$path|g" launchd/$(SYNC_LABEL).plist > "$$new" || exit 1; \
	plutil -lint -s "$$new" || { echo "install-sync: rendered plist is invalid; nothing loaded"; exit 1; }; \
	mv "$$new" "$(SYNC_PLIST)" || exit 1; \
	domain="gui/$$(id -u)"; \
	if launchctl print "$$domain/$(SYNC_LABEL)" >/dev/null 2>&1; then \
		launchctl bootout "$$domain/$(SYNC_LABEL)" || exit 1; \
		for i in 1 2 3 4 5 6 7 8 9 10; do launchctl print "$$domain/$(SYNC_LABEL)" >/dev/null 2>&1 || break; sleep 1; done; \
	fi; \
	launchctl bootstrap "$$domain" "$(SYNC_PLIST)" || exit 1; \
	echo "install-sync: $(SYNC_LABEL) loaded; log $(AGENCY_RUNTIME_DIR)/sync.log"

check-system-docs: pyyaml
	@test -d "$(ENGINEERING_STANDARDS)/shared" || { echo "ENGINEERING_STANDARDS=$(ENGINEERING_STANDARDS) has no shared/"; exit 1; }
	@python3 scripts/check_workspace.py --shared "$(ENGINEERING_STANDARDS)/shared"

# check_workspace.py reads YAML: every target that runs it depends on this one.
pyyaml:
	@python3 -c "import yaml" 2>/dev/null || pip3 install --quiet pyyaml

# The daemon's own loader, on the layout the daemon runs: each workspace beside
# the vendored shared/.
validate-workspace:
	@test -x "$(AGENCY_BIN)" || { echo "AGENCY_BIN must name an agency binary built from Agency development (got '$(AGENCY_BIN)')"; exit 1; }
	@stage=$$(mktemp -d "$(CURDIR)/.validate.XXXXXX") || { echo "validate-workspace: could not make a staging directory"; exit 1; }; \
	trap 'rm -rf "$$stage"' EXIT; \
	cp -R $(addprefix workspaces/,$(AGENTS)) workspaces/shared "$$stage/" || { echo "validate-workspace: could not stage the workspaces"; exit 1; }; \
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

# Re-vendor workspaces/shared/ from engineering-standards: exactly the shared/
# docs the templates cite, copied unedited. check-system-docs fails on drift.
vendor-shared: pyyaml ## Copy the cited shared/ docs from engineering-standards
	@test -d "$(ENGINEERING_STANDARDS)/shared" || { echo "ENGINEERING_STANDARDS=$(ENGINEERING_STANDARDS) has no shared/"; exit 1; }
	@docs=$$(python3 scripts/check_workspace.py --cited-shared) || exit 1; \
	test -n "$$docs" || { echo "vendor-shared: no template cites a shared/ doc; workspaces/shared/ left as it was"; exit 1; }; \
	stage=$$(mktemp -d "$(CURDIR)/.validate.XXXXXX") || { echo "vendor-shared: could not make a staging directory; workspaces/shared/ left as it was"; exit 1; }; \
	trap 'rm -rf "$$stage"' EXIT; \
	for doc in $$docs; do \
		test -f "$(ENGINEERING_STANDARDS)/shared/$$doc" || { echo "vendor-shared: $(ENGINEERING_STANDARDS)/shared/$$doc is missing; workspaces/shared/ left as it was"; exit 1; }; \
		mkdir -p "$$stage/shared/$$(dirname "$$doc")" && cp "$(ENGINEERING_STANDARDS)/shared/$$doc" "$$stage/shared/$$doc" || exit 1; \
	done; \
	if [ -e workspaces/shared ]; then mv workspaces/shared "$$stage/previous" || exit 1; fi; \
	mv "$$stage/shared" workspaces/shared || { \
		if [ ! -e "$$stage/previous" ] || mv "$$stage/previous" workspaces/shared; then \
			echo "vendor-shared: could not install the new copy; workspaces/shared/ left as it was"; exit 1; fi; \
		trap - EXIT; \
		echo "vendor-shared: could not install the new copy nor restore the old one; the old copy is at $$stage/previous"; exit 1; }; \
	echo "vendor-shared: $$(find workspaces/shared -type f | wc -l | tr -d ' ') doc(s) from $(ENGINEERING_STANDARDS)/shared"

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n",$$1,$$2}'
