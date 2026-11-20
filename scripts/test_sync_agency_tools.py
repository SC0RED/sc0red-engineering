"""scripts/sync_agency_tools.sh and scripts/run_on_release.sh against a fake
gh, a fake uv and a local agency-tools origin, all in temporary directories.

Run: python3 scripts/test_sync_agency_tools.py   (make test-sync)
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "sync_agency_tools.sh")
LAUNCHER = os.path.join(HERE, "run_on_release.sh")
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
DAY = 24 * 3600

FAKE_GH = """#!/usr/bin/env python3
import json, os, shutil, sys
state = json.load(open(os.environ["FAKE_GH_STATE"]))
open(os.environ["FAKE_GH_STATE"] + ".log", "a").write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    assert "--status" in args and args[args.index("--status") + 1] == "success", args
    print(state["run_id"] if state["run_id"] else "")
elif args[:2] == ["run", "download"]:
    run = args[2]
    dest = args[args.index("-D") + 1]
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(state["artifacts"][run]):
        shutil.copy(os.path.join(state["artifacts"][run], name), dest)
elif args[:2] == ["auth", "git-credential"]:
    pass
else:
    sys.exit("fake gh: unexpected " + " ".join(args))
"""

# Logs each sync's directory: a release is always built off to the side.
FAKE_UV = """#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_UV_FAIL"):
    sys.exit("fake uv: lock is stale")
assert sys.argv[1:] == ["sync", "--quiet", "--frozen", "--no-dev", "--no-editable"], sys.argv
open(os.environ["FAKE_GH_STATE"] + ".uv", "a").write(os.getcwd() + "\\n")
os.makedirs(".venv/bin", exist_ok=True)
if not os.path.exists(".venv/bin/python"):
    os.symlink(os.environ["FAKE_UV_PYTHON"], ".venv/bin/python")
"""


def read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def git(*args, cwd):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sync-test.", dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = os.path.join(self.tmp, "runtime")
        self.releases = os.path.join(self.runtime, "releases")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        for name, body in (("gh", FAKE_GH), ("uv", FAKE_UV)):
            path = os.path.join(bin_dir, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        # The agency-tools origin: two commits, the second importable as agency_tools.v2.
        self.origin = os.path.join(self.tmp, "agency-tools")
        os.makedirs(os.path.join(self.origin, "agency_tools"))
        git("init", "-q", cwd=self.origin)
        self.commit("agency_tools/__init__.py", "")
        self.sha1 = git("rev-parse", "HEAD", cwd=self.origin)
        self.commit("agency_tools/v2.py", "")
        self.sha2 = git("rev-parse", "HEAD", cwd=self.origin)
        self.state = os.path.join(self.tmp, "gh-state.json")
        self.artifacts = {}
        self.env = {
            **os.environ, **GIT_ENV,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AGENCY_RUNTIME_DIR": self.runtime,
            "AGENCY_TOOLS_REMOTE": self.origin,
            "SYNC_VERIFY_MODULES": "agency_tools",
            "FAKE_GH_STATE": self.state,
            "FAKE_UV_PYTHON": sys.executable,
            "UV_CACHE_DIR": os.path.join(self.tmp, "uv-cache"),
        }

    def commit(self, path, text):
        with open(os.path.join(self.origin, path), "w") as f:
            f.write(text)
        git("add", path, cwd=self.origin)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", path, cwd=self.origin)

    def artifact(self, run_id, at_sha, agency_sha="a" * 40, contract="v1", tamper=False, info=None,
                 summed=("build.info", "contracts.tar")):
        d = os.path.join(self.tmp, f"artifact-{run_id}")
        os.makedirs(d)
        with open(os.path.join(d, "build.info"), "w") as f:
            f.write(info if info is not None else f"sha={agency_sha}\nref=development\nwa_sha={'b' * 40}\nat_sha={at_sha}\n")
        with tarfile.open(os.path.join(d, "contracts.tar"), "w") as tar:
            data = contract.encode()
            member = tarfile.TarInfo("contracts/claims-v1/claims.json")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        sums = []
        for name in summed:
            digest = hashlib.sha256(read(os.path.join(d, name), "rb")).hexdigest()
            sums.append(f"{digest}  {name}")
        if tamper:
            with open(os.path.join(d, "contracts.tar"), "ab") as f:
                f.write(b"x")
        with open(os.path.join(d, "SHA256SUMS"), "w") as f:
            f.write("\n".join(sums) + "\n")
        self.artifacts[str(run_id)] = d

    def sync(self, run_id, **extra):
        with open(self.state, "w") as f:
            json.dump({"run_id": run_id, "artifacts": self.artifacts}, f)
        return subprocess.run(["bash", SCRIPT], env={**self.env, **extra},
                              capture_output=True, text=True)

    def synced(self, run_id, **extra):
        done = self.sync(run_id, **extra)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def current(self):
        link = os.path.join(self.runtime, "current")
        return os.readlink(link) if os.path.islink(link) else None

    def head(self):
        return git("rev-parse", "HEAD", cwd=os.path.join(self.runtime, "current", "agency-tools"))

    def gh_calls(self):
        path = self.state + ".log"
        return read(path).splitlines() if os.path.exists(path) else []

    def uv_dirs(self):
        path = self.state + ".uv"
        return read(path).splitlines() if os.path.exists(path) else []

    def listing(self):
        return sorted(os.listdir(self.releases))

    def age(self, name, days):
        marker = os.path.join(self.releases, name, ".complete")
        past = time.time() - days * DAY
        os.utime(marker, (past, past))

    # --- install ------------------------------------------------------------

    def test_installs_the_runs_tools_and_contracts(self):
        self.artifact(101, self.sha1)
        self.synced(101)
        name = f"{self.sha1}-{'a' * 12}"
        self.assertEqual(self.current(), f"releases/{name}")
        self.assertEqual(self.head(), self.sha1)
        contracts = os.path.join(self.runtime, "current", "contracts", "claims-v1", "claims.json")
        self.assertEqual(read(contracts), "v1")
        self.assertTrue(os.access(os.path.join(self.runtime, "current", "agency-tools", ".venv", "bin", "python"), os.X_OK))
        self.assertIn(f"at_sha={self.sha1}", read(os.path.join(self.releases, name, ".complete")))
        # Built off to the side, then renamed in: nothing left behind.
        self.assertTrue(all("/releases/.build." in d for d in self.uv_dirs()), self.uv_dirs())
        self.assertEqual(self.listing(), [name])

    def test_the_same_run_is_not_downloaded_again(self):
        self.artifact(101, self.sha1)
        self.synced(101)
        done = self.synced(101)
        self.assertIn("already installed", done.stdout)
        self.assertEqual(sum("run download" in c for c in self.gh_calls()), 1)

    def test_a_new_run_of_the_live_release_does_not_rebuild_it(self):
        self.artifact(101, self.sha1)
        self.synced(101)
        self.artifact(102, self.sha1)
        done = self.synced(102, FAKE_UV_FAIL="1")
        self.assertIn("already current", done.stdout)
        self.assertEqual(self.current(), f"releases/{self.sha1}-{'a' * 12}")
        self.assertEqual(read(os.path.join(self.runtime, "state", "run-id")).strip(), "102")
        self.assertEqual(len(self.uv_dirs()), 1)

    def test_a_complete_release_is_reused_never_rebuilt(self):
        # Back to a matched set already on disk: no uv, no checkout, same files.
        self.artifact(101, self.sha1)
        self.artifact(102, self.sha2, agency_sha="c" * 40)
        self.artifact(103, self.sha1)
        self.synced(101)
        first = f"{self.sha1}-{'a' * 12}"
        marker = read(os.path.join(self.releases, first, ".complete"))
        self.synced(102)
        done = self.synced(103, FAKE_UV_FAIL="1")
        self.assertIn(f"reusing release {first}", done.stdout)
        self.assertEqual(self.current(), f"releases/{first}")
        self.assertEqual(self.head(), self.sha1)
        self.assertEqual(read(os.path.join(self.releases, first, ".complete")), marker)
        self.assertEqual(len(self.uv_dirs()), 2)

    def test_a_release_dir_without_complete_is_rebuilt(self):
        name = f"{self.sha1}-{'a' * 12}"
        os.makedirs(os.path.join(self.releases, name, "agency-tools"))
        with open(os.path.join(self.releases, name, "junk"), "w") as f:
            f.write("half a build")
        self.artifact(101, self.sha1)
        self.synced(101)
        self.assertFalse(os.path.exists(os.path.join(self.releases, name, "junk")))
        self.assertTrue(os.path.isfile(os.path.join(self.releases, name, ".complete")))
        self.assertEqual(self.head(), self.sha1)

    def test_an_abandoned_build_is_removed(self):
        stale = os.path.join(self.releases, ".build.dead01")
        os.makedirs(os.path.join(stale, "release"))
        self.artifact(101, self.sha1)
        done = self.synced(101)
        self.assertIn("removed abandoned .build.dead01", done.stdout)
        self.assertEqual(self.listing(), [f"{self.sha1}-{'a' * 12}"])

    # --- cleanup ------------------------------------------------------------

    def test_young_releases_are_kept_old_ones_deleted(self):
        self.artifact(101, self.sha1)
        self.artifact(102, self.sha2, agency_sha="c" * 40)
        self.artifact(103, self.sha1, agency_sha="d" * 40)
        self.artifact(104, self.sha2, agency_sha="e" * 40)
        r1, r2, r3, r4 = (f"{self.sha1}-{'a' * 12}", f"{self.sha2}-{'c' * 12}",
                          f"{self.sha1}-{'d' * 12}", f"{self.sha2}-{'e' * 12}")
        for run in (101, 102, 103):
            self.synced(run)
        # 101 is neither current nor previous, but built today: kept.
        self.assertEqual(self.listing(), sorted([r1, r2, r3]))
        self.assertEqual(self.current(), f"releases/{r3}")
        self.assertEqual(self.head(), self.sha1)
        # Built over 7 days ago: once 104 is current and 103 previous, 101 and 102 go.
        self.age(r1, 8)
        self.age(r2, 8)
        self.synced(104)
        self.assertEqual(self.listing(), sorted([r3, r4]))
        self.assertEqual(read(os.path.join(self.runtime, "state", "previous")).strip(), f"releases/{r3}")
        worktrees = git("worktree", "list", cwd=os.path.join(self.runtime, "agency-tools.git"))
        self.assertNotIn(r1, worktrees)
        self.assertNotIn(r2, worktrees)

    def test_old_current_and_previous_are_kept(self):
        self.artifact(101, self.sha1)
        self.artifact(102, self.sha2, agency_sha="c" * 40)
        self.artifact(103, self.sha2, agency_sha="c" * 40)
        r1, r2 = f"{self.sha1}-{'a' * 12}", f"{self.sha2}-{'c' * 12}"
        self.synced(101)
        self.synced(102)
        self.age(r1, 30)
        self.age(r2, 30)
        self.synced(103)
        self.assertEqual(self.listing(), sorted([r1, r2]))

    # --- refusals: current is never touched ----------------------------------

    def assert_refused_and_kept(self, done, want):
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(want, done.stdout + done.stderr)
        self.assertIn("current left as it was", done.stdout + done.stderr)
        self.assertEqual(self.current(), f"releases/{self.sha1}-{'a' * 12}")
        self.assertEqual(read(os.path.join(self.runtime, "state", "run-id")).strip(), "101")
        # No half-built release is ever under releases/.
        for name in self.listing():
            self.assertFalse(name.startswith(".build."), self.listing())
            self.assertTrue(os.path.isfile(os.path.join(self.releases, name, ".complete")), name)

    def refused(self, artifact_kwargs, want, **extra):
        self.artifact(101, self.sha1)
        self.synced(101)
        self.artifact(102, **artifact_kwargs)
        self.assert_refused_and_kept(self.sync(102, **extra), want)

    def test_a_tampered_artifact_is_refused(self):
        self.refused({"at_sha": self.sha2, "tamper": True}, "does not match its SHA256SUMS")

    def test_sums_that_skip_the_contracts_are_refused(self):
        self.refused({"at_sha": self.sha2, "summed": ("build.info",)}, "does not list contracts.tar exactly once")

    def test_sums_that_list_one_file_twice_are_refused(self):
        # Two build.info lines would pass a line count while contracts.tar goes unchecked.
        self.refused({"at_sha": self.sha2, "summed": ("build.info", "build.info")}, "does not list")

    def test_sums_that_list_a_file_twice_alongside_the_other_are_refused(self):
        self.refused({"at_sha": self.sha2, "summed": ("build.info", "contracts.tar", "contracts.tar")},
                     "does not list contracts.tar exactly once")

    def test_a_build_info_without_a_tools_sha_is_refused(self):
        self.refused({"at_sha": "main"}, "has no agency-tools sha")

    def test_an_unknown_tools_sha_is_refused(self):
        self.refused({"at_sha": "e" * 40}, "cannot fetch agency-tools")

    def test_a_failed_uv_sync_keeps_current(self):
        self.refused({"at_sha": self.sha2}, "uv sync --frozen failed", FAKE_UV_FAIL="1")

    def test_a_release_that_cannot_import_keeps_current(self):
        self.refused({"at_sha": self.sha2}, "cannot import one of: agency_tools.missing",
                     SYNC_VERIFY_MODULES="agency_tools.missing")
        # Never marked complete, so never under releases/.
        self.assertEqual(self.listing(), [f"{self.sha1}-{'a' * 12}"])

    def test_no_successful_run_installs_nothing(self):
        done = self.sync(None)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no successful", done.stdout)
        self.assertIsNone(self.current())

    # --- run_on_release: a daemon resolves `current` once, at spawn -----------

    def daemon_env(self, **overrides):
        current = os.path.join(self.runtime, "current")
        return {**self.env,
                "AGENCY_TOOLS_ROOT": f"{current}/agency-tools",
                "AGENCY_PYTHON_BINARY": f"{current}/agency-tools/.venv/bin/python",
                "AGENCY_CONTRACTS_DIR": f"{current}/contracts",
                **overrides}

    def spawn(self, env):
        """What a daemon started through run_on_release.sh sees."""
        probe = ("import json, os; print(json.dumps({k: os.environ[k] for k in "
                 "('AGENCY_TOOLS_ROOT', 'AGENCY_PYTHON_BINARY', 'AGENCY_CONTRACTS_DIR')}))")
        return subprocess.run(["bash", LAUNCHER, sys.executable, "-c", probe], env=env,
                              capture_output=True, text=True)

    def test_a_daemon_runs_on_the_real_release_path(self):
        self.artifact(101, self.sha1)
        self.synced(101)
        done = self.spawn(self.daemon_env())
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        release = os.path.realpath(os.path.join(self.releases, f"{self.sha1}-{'a' * 12}"))
        self.assertEqual(seen, {
            "AGENCY_TOOLS_ROOT": f"{release}/agency-tools",
            "AGENCY_PYTHON_BINARY": f"{release}/agency-tools/.venv/bin/python",
            "AGENCY_CONTRACTS_DIR": f"{release}/contracts",
        })
        # A later sync swaps `current`; what the daemon holds is still that release, whole.
        self.artifact(102, self.sha2, agency_sha="c" * 40)
        self.synced(102)
        self.assertEqual(self.current(), f"releases/{self.sha2}-{'c' * 12}")
        self.assertEqual(git("rev-parse", "HEAD", cwd=seen["AGENCY_TOOLS_ROOT"]), self.sha1)
        self.assertEqual(read(os.path.join(seen["AGENCY_CONTRACTS_DIR"], "claims-v1", "claims.json")), "v1")

    def test_a_daemon_env_off_current_is_refused(self):
        self.artifact(101, self.sha1)
        self.synced(101)
        done = self.spawn(self.daemon_env(AGENCY_TOOLS_ROOT="/Volumes/SSD/Code/agency-tools"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("AGENCY_TOOLS_ROOT is '/Volumes/SSD/Code/agency-tools', not a path under", done.stderr)

    def test_no_release_starts_no_daemon(self):
        done = self.spawn(self.daemon_env())
        self.assertEqual(done.returncode, 1)
        self.assertIn("is not a release link", done.stderr)


if __name__ == "__main__":
    unittest.main()
