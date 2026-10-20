"""hermes receive: the receiving side of the door (docs/design.md §3 step 1, issue #4). Each test builds its own HOME,
feeds a fake stdin and a fake tailscale (tests/fixtures/tailscale); nothing on this machine is read or written.
The client address is from the documentation range."""
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes import receive

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
CLIENT = "192.0.2.10 50000 192.0.2.1 22"


def b64(name):
    return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")


def put_cmd(route, name, data, digest=None, size=None):
    return "put %s %s %s %s" % (route, b64(name), len(data) if size is None else size,
                                digest or hashlib.sha256(data).hexdigest())


class Broken(io.BytesIO):
    """A stdin that breaks after the first read."""
    def read(self, n=-1):
        if self.tell():
            raise OSError("connection reset")
        return super().read(min(n, 5) if n and n > 0 else 5)


class ReceiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.stage = self.home / ".local/share/hermes/stage"
        self.inbox = self.home / "inbox"
        for d in (self.stage, self.inbox, self.home / "zeus", self.home / ".config/hermes"):
            d.mkdir(parents=True)
        self.routes = self.home / ".config/hermes/routes.json"
        self.routes.write_text(json.dumps({"version": 1, "stages": {"home": "~/.local/share/hermes/stage"}, "routes": {
            "lesson": {"to": "~/inbox/", "stage": "home", "conflict": "replace"},
            "pcmail": {"to": "taildrop:{{PC}}", "stage": "home"},
            "deploy-request": {"to": "~/zeus/", "stage": "home", "enabled": False}}}))
        self.env = {"HOME": str(self.home), "HERMES_CONFIG": str(self.routes), "JANUS_CONFIG": str(self.home / "no-janus"),
                    "LEAFY_REALM": str(self.home / "no-realm"), "SSH_CONNECTION": CLIENT,
                    "HERMES_TAILSCALE": str(FIXTURES / "tailscale")}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def run_receive(self, command, data=b"", key="desktop", **env):
        """-> (exit status, stdout, stderr), with SSH_ORIGINAL_COMMAND=command and data on stdin."""
        e = dict(os.environ, **env)
        if command is None:
            e.pop("SSH_ORIGINAL_COMMAND", None)
        else:
            e["SSH_ORIGINAL_COMMAND"] = command
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            stdin = data if isinstance(data, io.IOBase) else io.BytesIO(data)
            code = receive.receive(key, env=e, stdin=stdin, out=out)
        return code, out.getvalue(), err.getvalue()

    def arrivals(self, folder):
        return sorted(p.name for p in (self.stage / folder).iterdir()) if (self.stage / folder).is_dir() else []

    def assertNothingWritten(self):
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(self.arrivals("incoming"), [])
        self.assertEqual(self.arrivals("failed"), [])

    # --- the happy path ---
    def test_ping(self):
        code, out, err = self.run_receive("ping")
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"^hermes ok \d+\.\d+\.\d+\n$")
        self.assertNothingWritten()

    def test_a_good_file_lands_complete_in_in(self):
        data = b"lesson JSON " * 1000
        code, out, err = self.run_receive(put_cmd("lesson", "git basics é.json", data), data)
        self.assertEqual((code, err), (0, ""), err)
        arrival_id = out.split()[1]
        self.assertRegex(arrival_id, r"^\d{8}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(self.arrivals("in"), [arrival_id])
        self.assertEqual(self.arrivals("incoming"), [])
        folder = self.stage / "in" / arrival_id
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["data", "meta.json"])
        self.assertEqual((folder / "data").read_bytes(), data)
        self.assertEqual((folder / "data").stat().st_mode & 0o777, 0o600)     # no exec bits, ever
        meta = json.loads((folder / "meta.json").read_text())
        self.assertEqual(meta["id"], arrival_id)
        self.assertEqual(meta["route"], "lesson")
        self.assertEqual(meta["direction"], "in")
        self.assertEqual(meta["name"], "git basics é.json")
        self.assertEqual(meta["bytes"], len(data))
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(meta["sender"], {"via": "ssh", "key": "desktop", "node": "desktop", "user": "you@example.com",
                                          "ip": "192.0.2.10"})
        self.assertRegex(meta["received"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(meta["hermes"], (ROOT / "VERSION").read_text().strip())

    def test_an_empty_file_and_a_sha_in_upper_case(self):
        code, out, _ = self.run_receive(put_cmd("lesson", "empty.txt", b"", hashlib.sha256(b"").hexdigest().upper()), b"")
        self.assertEqual(code, 0)
        self.assertEqual((self.stage / "in" / out.split()[1] / "data").read_bytes(), b"")

    # --- transfer failures: set aside, never in in/ ---
    def test_short_stream(self):
        data = b"x" * 100
        code, out, err = self.run_receive(put_cmd("lesson", "a.txt", data), data[:40])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("ended after 40 of 100 bytes", err)
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(self.arrivals("incoming"), [])
        aside = self.stage / "failed" / self.arrivals("failed")[0]
        self.assertEqual((aside / "data").read_bytes(), data[:40])
        self.assertIn("ended after 40 of 100 bytes", (aside / "error.txt").read_text())
        self.assertFalse((aside / "meta.json").exists())

    def test_long_stream(self):
        data = b"x" * 100
        code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data), data + b"extra")
        self.assertEqual(code, 1)
        self.assertIn("more bytes arrived than the 100 declared", err)
        self.assertEqual(self.arrivals("in"), [])
        aside = self.stage / "failed" / self.arrivals("failed")[0]
        self.assertEqual((aside / "data").read_bytes(), data)      # nothing past the declared size is kept

    def test_wrong_hash(self):
        data = b"x" * 100
        code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data, "0" * 64), data)
        self.assertEqual(code, 1)
        self.assertIn("SHA-256", err)
        self.assertIn("not the declared " + "0" * 64, err)
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(len(self.arrivals("failed")), 1)

    def test_a_broken_connection_is_set_aside_too(self):
        data = b"x" * 100
        code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data), Broken(data))
        self.assertEqual(code, 1)
        self.assertIn("connection reset", err)
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(self.arrivals("incoming"), [])
        self.assertEqual(len(self.arrivals("failed")), 1)

    # --- refused before anything is written ---
    def assertRefused(self, command, fragment, data=b"", **env):
        code, out, err = self.run_receive(command, data, **env)
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn(fragment, err)
        self.assertNothingWritten()

    def test_bad_route(self):
        data = b"x"
        self.assertRefused(put_cmd("Lesson", "a.txt", data), "'Lesson' is not a route id", data)
        self.assertRefused(put_cmd("lesson/x", "a.txt", data), "is not a route id", data)
        self.assertRefused(put_cmd("nope", "a.txt", data), "there is no route 'nope' on this server", data)
        self.assertRefused(put_cmd("pcmail", "a.txt", data), "route pcmail goes out to a device", data)
        self.assertRefused(put_cmd("deploy-request", "a.txt", data), "route deploy-request is closed", data)

    def test_bad_names(self):
        data = b"x"
        for name, why in [("../up.txt", "starts with a dot"), ("/etc/hosts", "slash"), ("dir/a.txt", "slash"), (".hidden", "starts with a dot"),
                          ("..", "starts with a dot"), ("a\\b.txt", "backslash"), ("a\0b", "control"), ("a\nb", "control"),
                          ("x" * 201, "longer than 200"), ("   ", "empty")]:
            with self.subTest(name):
                self.assertRefused(put_cmd("lesson", name, data), why, data)
        self.assertRefused("put lesson !!! 1 " + "0" * 64, "not base64url", data)
        self.assertRefused("put lesson %s 1 %s" % (base64.urlsafe_b64encode(b"\xff\xfe").decode(), "0" * 64), "not base64url", data)

    def test_names_that_disguise_or_break(self):
        data = b"x"
        for name, why in [("photo\u202egpj.exe", "control, invisible or direction-changing character ('photo\\u202egpj.exe', U+202E)"),
                          ("a\u200bb.txt", "U+200B"), ("a\u0085b.txt", "U+0085"), ("a\u00adb.txt", "U+00AD"),
                          ("a\ue000b.txt", "U+E000"), ("a\u2028b.txt", "U+2028"), ("a\u2029b.txt", "U+2029"),
                          ("a.txt ", "ends with a space or a dot"), ("a.txt.", "ends with a space or a dot"),
                          ("a.txt. ", "ends with a space or a dot")]:
            with self.subTest(name):
                self.assertRefused(put_cmd("lesson", name, data), why, data)

    def test_names_are_nfc_normalized(self):
        data = b"x"
        decomposed = "cafe\u0301.txt"                   # e + combining acute
        self.assertEqual(len(decomposed), 9)
        code, out, _ = self.run_receive(put_cmd("lesson", decomposed, data), data)
        self.assertEqual(code, 0)
        meta = json.loads((self.stage / "in" / out.split()[1] / "meta.json").read_text())
        self.assertEqual(meta["name"], "caf\u00e9.txt")
        self.assertEqual(len(meta["name"]), 8)
        long_after_nfc = "e" + "\u0301" * 150 + ".txt"    # 155 code points before, 2 + 149 after: still refused by category? no: combining marks are Mn
        code, out, _ = self.run_receive(put_cmd("lesson", long_after_nfc, data), data)
        self.assertEqual(code, 0)

    def test_sizes_must_be_plain_digits(self):
        data = b"x"
        for size in ("\u00b2", "\u0661", "1e3", "-1", "+1", "", "1" * 16):
            with self.subTest(size):
                code, out, err = self.run_receive("put lesson %s %s %s" % (b64("a.txt"), size, "0" * 64), data)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("exactly four arguments" if size == "" else "must be a number of plain digits", err)
                self.assertNotIn("Traceback", err)
                self.assertNothingWritten()
        code, _, _ = self.run_receive(put_cmd("lesson", "a.txt", data, size="0001"), data)
        self.assertEqual(code, 0)                        # leading zeros are plain digits: 0001 declares 1 byte

    def test_idle_timeout_while_streaming(self):
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        data = b"x" * 100
        os.write(w, data[:30])                           # the sender stalls after 30 bytes and never closes
        with os.fdopen(r, "rb", buffering=0) as stdin:
            code, out, err = self.run_receive(put_cmd("lesson", "a.txt", data), stdin, HERMES_IDLE_TIMEOUT="0.2")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no data for 0.2 s", err)
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(self.arrivals("incoming"), [])
        aside = self.stage / "failed" / self.arrivals("failed")[0]
        self.assertEqual((aside / "data").read_bytes(), data[:30])
        self.assertIn("no data for 0.2 s", (aside / "error.txt").read_text())

    def test_idle_timeout_waiting_for_the_end(self):
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        data = b"x" * 100
        os.write(w, data)                                # every byte arrived, but the sender never closes its end
        with os.fdopen(r, "rb", buffering=0) as stdin:
            code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data), stdin, HERMES_IDLE_TIMEOUT="0.2")
        self.assertEqual(code, 1)
        self.assertIn("no data for 0.2 s", err)
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(len(self.arrivals("failed")), 1)

    def test_a_pipe_that_closes_in_time_is_fine(self):
        r, w = os.pipe()
        data = b"y" * 3000
        os.write(w, data)
        os.close(w)
        with os.fdopen(r, "rb", buffering=0) as stdin:
            code, out, err = self.run_receive(put_cmd("lesson", "a.txt", data), stdin, HERMES_IDLE_TIMEOUT="0.2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((self.stage / "in" / out.split()[1] / "data").read_bytes(), data)

    def test_an_unexpected_error_shows_the_sender_one_line(self):
        data = b"x"
        with mock.patch.object(receive, "put", side_effect=RuntimeError("stat failed on /srv/somewhere/private")):
            code, out, err = self.run_receive(put_cmd("lesson", "a.txt", data), data)
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(err, "hermes: the server hit an unexpected error; the owner can see it in " + receive.ERROR_LOG + "\n")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("private", err)
        log = (self.home / ".local/share/hermes/log/receive-errors.log").read_text()
        self.assertIn("Traceback (most recent call last)", log)
        self.assertIn("RuntimeError: stat failed on /srv/somewhere/private", log)
        self.assertRegex(log, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d hermes receive\n")

    def test_bad_commands(self):
        self.assertRefused("ls -la", "unknown command 'ls -la'")
        self.assertRefused("", "unknown command")
        self.assertRefused("put lesson " + b64("a"), "exactly four arguments")
        self.assertRefused("put lesson %s ten %s" % (b64("a"), "0" * 64), "<bytes> must be a number of plain digits")
        self.assertRefused("put lesson %s 1 zz" % b64("a"), "<sha256> must be 64 hex")
        self.assertRefused("ping extra", "unknown command")
        self.assertRefused(None, "no command. hermes receive runs only as an ssh forced command")

    # --- whois: recorded, never fatal ---
    def test_whois_failure_is_recorded_not_fatal(self):
        data = b"x"
        for env, error in [({"HERMES_FAKE_WHOIS": "fail"}, "no match for IP"), ({"HERMES_FAKE_WHOIS": "garbage"}, "whois failed"),
                           ({"HERMES_TAILSCALE": str(self.home / "no-tailscale")}, "whois failed"),
                           ({"SSH_CONNECTION": ""}, "no SSH_CONNECTION")]:
            with self.subTest(error):
                code, out, err = self.run_receive(put_cmd("lesson", "a.txt", data), data, **env)
                self.assertEqual((code, err), (0, ""))
                meta = json.loads((self.stage / "in" / out.split()[1] / "meta.json").read_text())
                self.assertIsNone(meta["sender"]["node"])
                self.assertIsNone(meta["sender"]["user"])
                self.assertIn(error, meta["sender"]["whois_error"])
                self.assertEqual(meta["sender"]["key"], "desktop")

    # --- the server's own problems ---
    def test_disk_margin_before_the_transfer(self):
        data = b"x" * 100
        with mock.patch.object(receive, "free_bytes", return_value=receive.MARGIN + 99):
            code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data), data)
        self.assertEqual(code, 3)
        self.assertIn("nearly full", err)
        self.assertNothingWritten()

    def test_disk_margin_while_streaming(self):
        data = b"x" * 100
        with mock.patch.object(receive, "free_bytes", side_effect=[receive.MARGIN * 2, receive.MARGIN - 1]):
            code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data), data)
        self.assertEqual(code, 3)
        self.assertIn("nearly full", err)
        self.assertEqual(self.arrivals("in"), [])
        self.assertEqual(len(self.arrivals("failed")), 1)
        self.assertGreater(receive.free_bytes(self.stage), 0)      # the real check works on this machine

    def test_broken_config_is_the_servers_problem(self):
        self.routes.write_text("{ not json")
        data = b"x"
        code, _, err = self.run_receive(put_cmd("lesson", "a.txt", data), data)
        self.assertEqual(code, 3)
        self.assertIn("hermes check-config", err)
        self.assertEqual((self.run_receive("ping")[0]), 0)       # ping needs no config

    def test_bad_key_name_from_authorized_keys(self):
        code, _, err = self.run_receive("ping", key="bad name")
        self.assertEqual(code, 3)
        self.assertIn("authorized_keys line is wrong", err)

    # --- the real command ---
    def test_command_line(self):
        data = b"hello " * 10
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=put_cmd("lesson", "hello.txt", data))
        r = subprocess.run([sys.executable, "-m", "hermes", "receive", "--key", "desktop"], input=data, capture_output=True, env=env, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.decode(), r"^ok \d{8}-\d{6}-[0-9a-f]{6}\n$")
        self.assertEqual(len(self.arrivals("in")), 1)
        env["SSH_ORIGINAL_COMMAND"] = "bash"
        r = subprocess.run([sys.executable, "-m", "hermes", "receive", "--key", "desktop"], input=b"", capture_output=True, env=env, cwd=ROOT)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"unknown command", r.stderr)
        r = subprocess.run([sys.executable, "-m", "hermes", "receive"], capture_output=True, env=env, cwd=ROOT)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
