"""hermes keys add|list|remove: the Hermes-only lines in ~/.ssh/authorized_keys (issue #4). Each test has its own HOME."""
import base64
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes import keys

ROOT = Path(__file__).resolve().parent.parent


def pubkey(seed, keytype="ssh-ed25519", comment="hermes@desktop"):
    """A well-formed public key line (SSH wire format: the type, then 32 key bytes)."""
    raw = struct.pack(">I", len(keytype)) + keytype.encode() + struct.pack(">I", 32) + (seed.encode() * 32)[:32]
    return "%s %s %s\n" % (keytype, base64.b64encode(raw).decode(), comment)


FOREIGN = "ssh-ed25519 " + pubkey("f").split()[1] + " someone-else"
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.ak = self.home / ".ssh" / "authorized_keys"

    def lines(self):
        return self.ak.read_text().splitlines()

    def backups(self):
        return sorted(p.name for p in self.ak.parent.iterdir() if ".set-aside-" in p.name)

    def test_add_writes_the_forced_command_line(self):
        msg = keys.add("desktop", pubkey("a"))
        self.assertTrue(msg.startswith("added hermes:desktop (ssh-ed25519 SHA256:"), msg)
        blob = pubkey("a").split()[1]
        self.assertEqual(self.lines(), ['restrict,from="%s",command="%s/.local/bin/hermes receive --key desktop" ssh-ed25519 %s hermes:desktop'
                                        % (keys.TAILNET_RANGES, self.home, blob)])
        self.assertEqual(stat.S_IMODE(self.ak.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.ak.parent.stat().st_mode), 0o700)
        self.assertEqual(self.backups(), [])                       # there was no file to back up

    def test_other_lines_are_never_touched(self):
        self.ak.parent.mkdir(mode=0o700)
        self.ak.write_text("# my keys\n" + FOREIGN + "\n\n")
        keys.add("desktop", pubkey("a"))
        keys.add("laptop", pubkey("b"))
        self.assertEqual(self.lines()[:3], ["# my keys", FOREIGN, ""])
        self.assertEqual([keys.tagged(l) for l in self.lines()[3:]], ["desktop", "laptop"])
        keys.remove("desktop")
        self.assertEqual(self.lines()[:3], ["# my keys", FOREIGN, ""])
        self.assertEqual([keys.tagged(l) for l in self.lines()[3:]], ["laptop"])

    def test_every_change_saves_a_dated_backup_first(self):
        keys.add("desktop", pubkey("a"))
        first = self.ak.read_text()
        msg = keys.add("laptop", pubkey("b"))
        self.assertRegex(self.backups()[0], r"^authorized_keys\.set-aside-\d{8}-\d{4}$")
        self.assertIn("the old file is " + self.backups()[0], msg)
        self.assertEqual((self.ak.parent / self.backups()[0]).read_text(), first)
        second = self.ak.read_text()
        keys.remove("laptop")
        self.assertEqual(len(self.backups()), 2)                   # same minute: a second name, nothing overwritten
        self.assertEqual((self.ak.parent / self.backups()[1]).read_text(), second)

    def test_add_again_replaces_the_devices_line(self):
        keys.add("desktop", pubkey("a"))
        msg = keys.add("desktop", pubkey("c"))
        self.assertTrue(msg.startswith("replaced hermes:desktop"))
        self.assertEqual(len(self.lines()), 1)
        self.assertIn(pubkey("c").split()[1], self.lines()[0])

    def test_list(self):
        self.assertEqual(keys.listing(), [])
        keys.add("desktop", pubkey("a"))
        keys.add("laptop", pubkey("b", "ssh-rsa"))
        self.ak.write_text(self.ak.read_text() + FOREIGN + "\n" + 'command="/bin/true" ' + pubkey("d").strip() + " hermes:edited\n")
        out = keys.listing()
        self.assertEqual(len(out), 3)
        self.assertRegex(out[0], r"^desktop  ssh-ed25519  SHA256:[A-Za-z0-9+/]{43}  ok$")
        self.assertRegex(out[1], r"^laptop  ssh-rsa  SHA256:\S+  ok$")
        self.assertRegex(out[2], r"^edited  ssh-ed25519  SHA256:\S+  options differ")

    def test_remove_a_device_that_is_not_there(self):
        keys.add("desktop", pubkey("a"))
        with self.assertRaises(keys.KeyError_) as cm:
            keys.remove("laptop")
        self.assertIn("no line tagged hermes:laptop", str(cm.exception))
        self.assertEqual(len(self.lines()), 1)

    def test_bad_keys_are_refused(self):
        for text, why in [(PRIVATE, "that is a private key"), ("", "expected one public key line"),
                          (pubkey("a") + pubkey("b"), "found 2"),
                          ("ssh-dss AAAA x", "key type Hermes knows"), ("ssh-ed25519 AAAA x", "not a valid ssh-ed25519"),
                          ("ssh-ed25519 %s x" % pubkey("a", "ssh-rsa").split()[1], "not a valid ssh-ed25519"),
                          ("ssh-ed25519", "key type Hermes knows")]:
            with self.subTest(why):
                with self.assertRaises(keys.KeyError_) as cm:
                    keys.add("desktop", text)
                self.assertIn(why, str(cm.exception))
        self.assertFalse(self.ak.exists())

    def test_a_key_that_is_already_someone_elses_is_refused(self):
        self.ak.parent.mkdir(mode=0o700)
        self.ak.write_text(FOREIGN + "\n")
        with self.assertRaises(keys.KeyError_) as cm:
            keys.add("desktop", pubkey("f"))
        self.assertIn("on a line that is not Hermes's", str(cm.exception))
        self.assertIn("ssh-keygen -t ed25519 -f hermes_desktop", str(cm.exception))
        self.assertEqual(self.lines(), [FOREIGN])

    def test_bad_device_names(self):
        for device in ("", "my pc", "pc;rm", 'pc"', "-pc", "x" * 64):
            with self.subTest(device):
                with self.assertRaises(keys.KeyError_):
                    keys.add(device, pubkey("a"))
                with self.assertRaises(keys.KeyError_):
                    keys.remove(device)
        self.assertFalse(self.ak.exists())

    def test_command_line(self):
        pub = Path(self.tmp.name) / "hermes_desktop.pub"
        pub.write_text(pubkey("a"))

        def run(*args, stdin=""):
            return subprocess.run([sys.executable, "-m", "hermes", "keys", *args], capture_output=True, text=True, cwd=ROOT, input=stdin)
        r = run("add", "desktop", str(pub))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added hermes:desktop", r.stdout)
        r = run("add", "laptop", stdin=pubkey("b"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("list")
        self.assertEqual(r.returncode, 0)
        self.assertEqual([l.split()[0] for l in r.stdout.splitlines()], ["desktop", "laptop"])
        r = run("remove", "laptop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.lines()), 1)
        r = run("add", stdin=pubkey("b"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("which device", r.stderr)
        r = run("add", "laptop", str(pub.with_name("missing.pub")))
        self.assertEqual(r.returncode, 1)
        other = Path(self.tmp.name) / "other_keys"
        r = run("add", "laptop", str(pub), "--file", str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(keys.tagged(other.read_text()), "laptop")
        self.assertEqual(len(self.lines()), 1)


if __name__ == "__main__":
    unittest.main()
