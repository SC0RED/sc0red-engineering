.PHONY: check check-structure no-symlinks check-workspace test-sync test-stop-daemon check-system-docs \
        validate-workspace tool-refs vendor-shared pyyaml sync-tools install-sync help \
        install-agency team-db install-daemon daemon-health test-survival

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
check-structure: no-symlinks check-workspace test-sync test-stop-daemon ## Checks that need no sibling checkout (Actions)

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

# scripts/stop_daemon.sh against a fake launchctl (install-daemon waits for
# launchd to let the old job go before it swaps anything), in a temporary
# directory under the repo.
test-stop-daemon:
	@stage=$$(mktemp -d "$(CURDIR)/.validate.XXXXXX") || { echo "test-stop-daemon: could not make a staging directory"; exit 1; }; \
	trap 'rm -rf "$$stage"' EXIT; \
	TMPDIR="$$stage" python3 scripts/test_stop_daemon.py >"$$stage/out" 2>&1 \
		&& echo "test-stop-daemon: OK" || { cat "$$stage/out"; exit 1; }

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

# Install the agency binary the daemons run, with the Agency scripts that go
# with it (the MCP server the runs use, database provisioning), from an Agency
# checkout whose target/release/agency is built from that checkout's HEAD. The
# new copy is staged beside the old and swapped in by renames. Restart the
# daemons after (make install-daemon), and rerun make team-db when the
# release has new migrations.
AGENCY_SRC ?=
AGENCY_SCRIPTS := agency_mcp_server.py provision_database.sh lifecycle_provision.sql
install-agency: ## Install agency from AGENCY_SRC=<Agency checkout with a release build>
	@test -x "$(AGENCY_SRC)/target/release/agency" || { echo "install-agency: AGENCY_SRC=$(AGENCY_SRC) has no target/release/agency"; exit 1; }
	@dest="$(AGENCY_RUNTIME_DIR)/agency"; stage="$$dest.new"; old="$$dest.previous"; \
	rm -rf "$$stage" "$$old" && mkdir -p "$$stage/scripts" || exit 1; \
	install -m 0755 "$(AGENCY_SRC)/target/release/agency" "$$stage/agency" || exit 1; \
	for script in $(AGENCY_SCRIPTS); do install -m 0644 "$(AGENCY_SRC)/scripts/$$script" "$$stage/scripts/" || exit 1; done; \
	chmod 0755 "$$stage/scripts/provision_database.sh"; \
	echo "sha=$$(git -C "$(AGENCY_SRC)" rev-parse HEAD)" > "$$stage/build.info" || exit 1; \
	if [ -e "$$dest" ]; then mv "$$dest" "$$old" || exit 1; fi; \
	mv "$$stage" "$$dest" || exit 1; rm -rf "$$old"; \
	echo "install-agency: $$dest at $$(cat "$$dest/build.info")"

team-db: ## Create/start the team Postgres, then migrate + provision it (idempotent)
	@scripts/team_db.sh

# Install one agent's daemon: its workspace beside the vendored shared/, the
# launcher, and its launchd job (launchd/com.sc0red.agency-daemon.plist,
# label com.sc0red.agency-<agent>), then (re)load the job. Everything is
# staged first; only then is a loaded job booted out (which waits for the
# daemon to drain its runs), the staged copies renamed into place and the job
# bootstrapped. PATH is the directories of the tools a run uses, as this shell
# resolves them. An agent's agent.env must carry the daemon settings (PORT and
# the rest).
AGENT ?=
DAEMON_TOOLS := claude op gh git python3 uv make docker cargo node
DAEMON_AGENT = $(if $(filter $(AGENT),$(AGENTS)),$(AGENT),$(error AGENT must be one of $(AGENTS)))
install-daemon: ## Install + (re)load an agent's daemon: AGENT=patch
	@grep -q '^PORT=' workspaces/$(DAEMON_AGENT)/agent.env || { echo "install-daemon: workspaces/$(AGENT)/agent.env has no daemon settings (PORT)"; exit 1; }
	@test -x "$(AGENCY_RUNTIME_DIR)/agency/agency" || { echo "install-daemon: no agency at $(AGENCY_RUNTIME_DIR)/agency; run make install-agency"; exit 1; }
	@for tool in $(DAEMON_TOOLS); do command -v $$tool >/dev/null || { echo "install-daemon: no $$tool on PATH"; exit 1; }; done; \
	path=$$(for tool in $(DAEMON_TOOLS); do dirname "$$(command -v $$tool)"; done | awk '!seen[$$0]++' | paste -sd: -):/usr/bin:/bin:/usr/sbin:/sbin; \
	label="com.sc0red.agency-$(AGENT)"; plist="$(HOME)/Library/LaunchAgents/$$label.plist"; domain="gui/$$(id -u)"; \
	ws="$(AGENCY_RUNTIME_DIR)/workspaces"; stage="$$ws/.staging-$(AGENT)"; new="$$plist.new"; \
	trap 'rm -rf "$$new" "$$stage"' EXIT; \
	mkdir -p "$(AGENCY_RUNTIME_DIR)/bin" "$$ws" "$(AGENCY_RUNTIME_DIR)/$(AGENT)" "$(dir $(SYNC_PLIST))" || exit 1; \
	sed -e "s|@LABEL@|$$label|g" -e "s|@LAUNCHER@|$(AGENCY_RUNTIME_DIR)/bin/start_daemon.sh|g" -e "s|@AGENT@|$(AGENT)|g" \
		-e "s|@LOG@|$(AGENCY_RUNTIME_DIR)/$(AGENT)/agency.log|g" -e "s|@RUNTIME@|$(AGENCY_RUNTIME_DIR)|g" \
		-e "s|@PATH@|$$path|g" launchd/com.sc0red.agency-daemon.plist > "$$new" || exit 1; \
	plutil -lint -s "$$new" || { echo "install-daemon: rendered plist is invalid; nothing changed"; exit 1; }; \
	rm -rf "$$stage" && mkdir -p "$$stage" || exit 1; \
	cp -R workspaces/$(AGENT) workspaces/shared "$$stage/" || { echo "install-daemon: could not stage the workspace; nothing changed"; exit 1; }; \
	install -m 0755 scripts/start_daemon.sh scripts/run_on_release.sh "$$stage/" || { echo "install-daemon: could not stage the launchers; nothing changed"; exit 1; }; \
	scripts/stop_daemon.sh "$$domain/$$label" || { echo "install-daemon: $$label did not stop; nothing was swapped or loaded"; exit 1; }; \
	mv "$$stage/start_daemon.sh" "$$stage/run_on_release.sh" "$(AGENCY_RUNTIME_DIR)/bin/" || exit 1; \
	for dir in $(AGENT) shared; do \
		rm -rf "$$stage/old-$$dir"; \
		if [ -e "$$ws/$$dir" ]; then mv "$$ws/$$dir" "$$stage/old-$$dir" || exit 1; fi; \
		mv "$$stage/$$dir" "$$ws/$$dir" || exit 1; \
	done; \
	mv "$$new" "$$plist" || exit 1; \
	launchctl bootstrap "$$domain" "$$plist" || exit 1; \
	echo "install-daemon: $$label loaded; log $(AGENCY_RUNTIME_DIR)/$(AGENT)/agency.log"

daemon-health: ## Show an agent's launchd state + /api/health: AGENT=patch
	@launchctl print "gui/$$(id -u)/com.sc0red.agency-$(DAEMON_AGENT)" | grep -E '^\s+(state|pid|last exit code|runs) ='
	@port=$$(sed -n 's/^PORT=//p' "$(AGENCY_RUNTIME_DIR)/workspaces/$(AGENT)/agent.env"); \
	health=$$(curl -fsS --max-time 5 "http://127.0.0.1:$$port/api/health") || { echo "daemon-health: no answer on $$port"; exit 1; }; echo "$$health"

# Layer 2 of the test plan on this Mac: the daemon survives a restart, a
# Postgres outage and a launchd reload. Reboot, sleep and a locked 1Password
# are the owner's manual steps (README).
test-survival: ## Layer-2 survival checks against the installed daemon: AGENT=patch
	@scripts/test_daemon_survival.sh "$(DAEMON_AGENT)"

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
