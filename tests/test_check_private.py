"""scripts/check-private.sh stops personal data: it fails on a planted home path, *.ts.net name, IP address and email,
honours the denylist and private-ok, and passes on this repository's own tree."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-private.sh"
# Built from pieces so that this file itself passes the check it tests.
PLANTED = {
    "home path": "/home/" + "alice" + "/.ssh/id_ed25519",
    "tailnet name": "alice-laptop." + "ts" + ".net",
    "IPv4 address": "100.101" + ".102.103",
    "IPv6 address": "fd7a:115c:" + "a1e0:ab12:4843:cd96:6258:b9c1",
    "email": "alice@" + "home-mail" + ".org",
}
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@" + "example.com"]


class CheckPrivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# A clean file\nNothing personal: {{SERVER}}, example.com, 127.0.0.1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "clean")

    def git(self, *args):
        return subprocess.run(["git", *GIT_ID, *args], cwd=self.repo, check=True, capture_output=True, text=True)

    def check(self, *args, denylist=None):
        env = {**os.environ, "HOME": self.tmp.name, "LEAFY_DENYLIST": denylist or ""}
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=self.repo, capture_output=True, text=True, env=env)

    def stage(self, text, name="notes.md"):
        (self.repo / name).write_text(text)
        self.git("add", "-A")

    def test_each_planted_pattern_stops_the_staged_changes(self):
        for what, sample in PLANTED.items():
            with self.subTest(what):
                self.stage("see " + sample + " for details\n")
                r = self.check("--generic")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("notes.md:1: " + what, r.stdout)

    def test_clean_changes_pass(self):
        self.stage("Hosts are written as {{SERVER}} and {{PC}}; see docs at example.org\n")
        r = self.check("--generic")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing personal found", r.stdout)

    def test_private_ok_skips_a_deliberate_example(self):
        self.stage("from=\"100.64.0.0/10,fd7a:115c:a1e0::/48\"   (private-ok: Tailscale's fixed ranges)\n")
        self.assertEqual(self.check("--generic").returncode, 0)

    def test_commit_range_includes_messages(self):
        self.stage("fine\n")
        self.git("commit", "-q", "-m", "mention " + PLANTED["tailnet name"])
        r = self.check("--generic", "main~1..main")
        self.assertEqual(r.returncode, 1)
        self.assertIn("message:1: tailnet name", r.stdout)

    def test_denylist_entries_match_ignoring_case(self):
        deny = Path(self.tmp.name) / "denylist.txt"
        deny.write_text("# my devices\nzeta-box\n")
        self.stage("backups go to Zeta-Box nightly\n")
        r = self.check(denylist=str(deny))
        self.assertEqual(r.returncode, 1)
        self.assertIn("denylist entry 1", r.stdout)
        self.assertEqual(self.check("--generic", denylist=str(deny)).returncode, 0)

    def test_this_repository_is_clean(self):
        r = subprocess.run(["bash", str(SCRIPT), "--generic", "--tree"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
