"""scripts/stop_daemon.sh against a fake launchctl, and its place in
make install-daemon.

Run: python3 scripts/test_stop_daemon.py   (make test-stop-daemon)
"""

import json
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stop_daemon.sh")
MAKEFILE = os.path.join(HERE, os.pardir, "Makefile")
SERVICE = "gui/501/com.sc0red.agency-patch"

# `print` succeeds while the job is registered: "loaded" is how many more
# prints find it (-1: forever). bootout starts the drain; before it, the job
# stays registered however often it is printed.
FAKE_LAUNCHCTL = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_LAUNCHCTL_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
open(path + ".log", "a").write(" ".join(args) + "\\n")
if args[0] == "print":
    if state["loaded"] == 0:
        sys.exit(113)
    if state["booted_out"] and state["loaded"] > 0:
        state["loaded"] -= 1
elif args[0] == "bootout":
    if state.get("bootout_fails"):
        sys.exit("Boot-out failed: 5: Input/output error")
    state["booted_out"] = True
else:
    sys.exit("fake launchctl: unexpected " + " ".join(args))
json.dump(state, open(path, "w"))
"""


class StopDaemon(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        fake = os.path.join(bin_dir, "launchctl")
        with open(fake, "w") as handle:
            handle.write(FAKE_LAUNCHCTL)
        os.chmod(fake, 0o755)
        self.state = os.path.join(self.dir, "state.json")
        self.env = {**os.environ, "PATH": bin_dir + os.pathsep + os.environ["PATH"],
                    "FAKE_LAUNCHCTL_STATE": self.state, "STOP_DAEMON_POLL_SECS": "0.05"}

    def run_script(self, loaded, timeout="5", **state):
        with open(self.state, "w") as handle:
            json.dump({"loaded": loaded, "booted_out": False, **state}, handle)
        done = subprocess.run([SCRIPT, SERVICE], env={**self.env, "STOP_DAEMON_TIMEOUT_SECS": timeout},
                              capture_output=True, text=True, timeout=30)
        with open(self.state + ".log") as handle:
            return done, handle.read().splitlines()

    def test_not_loaded_is_a_no_op(self):
        done, log = self.run_script(loaded=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(log, [f"print {SERVICE}"])

    def test_waits_until_launchd_lets_the_job_go(self):
        # Still registered for four prints after bootout (a run draining); the fifth fails.
        done, log = self.run_script(loaded=4)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(log[:2], [f"print {SERVICE}", f"bootout {SERVICE}"])
        self.assertEqual(log[2:], [f"print {SERVICE}"] * 5, "returned before print failed")
        self.assertIn("stopped", done.stdout)

    def test_timeout_exits_non_zero_and_says_so(self):
        done, log = self.run_script(loaded=-1, timeout="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("still registered 1s after bootout", done.stderr)
        self.assertEqual(log.count(f"bootout {SERVICE}"), 1)

    def test_failed_bootout_exits_non_zero(self):
        done, log = self.run_script(loaded=-1, bootout_fails=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("bootout", done.stderr)


class InstallDaemonRecipe(unittest.TestCase):
    def test_stops_through_the_script_before_any_swap(self):
        with open(MAKEFILE) as handle:
            text = handle.read()
        recipe = re.search(r"^install-daemon:.*?\n((?:\t.*\n)+)", text, re.MULTILINE).group(1)
        self.assertNotIn("launchctl bootout", recipe)
        stop = recipe.index("scripts/stop_daemon.sh")
        self.assertIn("exit 1", recipe[stop:recipe.index("\n", stop)])
        for change in ('mv "$$stage/start_daemon.sh"', 'mv "$$new" "$$plist"', 'launchctl bootstrap'):
            self.assertLess(stop, recipe.index(change), change)


if __name__ == "__main__":
    unittest.main()
