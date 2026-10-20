"""The hermes command line and its version (python3 -m hermes from the checkout)."""
import subprocess
import sys
import unittest
from pathlib import Path

import hermes

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()


def run(*args):
    return subprocess.run([sys.executable, "-m", "hermes", *args], cwd=ROOT, capture_output=True, text=True)


class CliTest(unittest.TestCase):
    def test_version_matches_the_version_file(self):
        self.assertEqual(hermes.version(), VERSION)
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_version_flag(self):
        r = run("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hermes " + VERSION)

    def test_no_command_prints_help(self):
        r = run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: hermes", r.stdout)
        self.assertIn("never runs, unpacks or installs", r.stdout)

    def test_unknown_command_is_refused(self):
        r = run("unpack")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: hermes", r.stderr)


if __name__ == "__main__":
    unittest.main()
