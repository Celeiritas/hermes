"""Both ways: hermes send, hermes collect (the outbox and the phones' Taildrops), hermes message, the units, the
Dolphin menu and the retire script (docs/design.md §3 steps 3–5, issue #7). Each test builds its own HOME and talks
to the fake tailscale and the fake Janus in tests/fixtures/; nothing on this machine is read or written."""
import contextlib
import fcntl
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from hermes import door, log, send

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


class BothWaysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.stage = self.home / ".local/share/hermes/stage"
        self.watch, self.mail, self.device, self.phone = (Path(self.tmp.name) / d for d in ("watch", "mail", "device", "phone-taildrops"))
        for d in (self.stage, self.watch, self.mail, self.device, self.phone, self.home / ".config/hermes", self.home / "docs"):
            d.mkdir(parents=True)
        self.routes = self.home / ".config/hermes/routes.json"
        self.routes.write_text(json.dumps({"version": 1, "stages": {"home": "~/.local/share/hermes/stage"}, "routes": {
            "pcmail": {"to": "taildrop:{{PC}}", "stage": "home", "prefix": "hermes__", "retry_hours": 1},
            "phone": {"to": "taildrop:{{PHONE}}", "stage": "home"},
            "torrent": {"to": str(self.watch), "stage": "home", "conflict": "skip"},
            "servermail": {"to": str(self.mail), "stage": "home", "conflict": "rename"}},
            "taildrop": {"by_kind": {".torrent": "torrent", ".magnet": "torrent"}, "default": "servermail"}}))
        self.env = {"HOME": str(self.home), "HERMES_CONFIG": str(self.routes), "JANUS_CONFIG": str(self.home / "no-janus"),
                    "LEAFY_REALM": str(self.home / "no-realm"), "HERMES_JANUS": str(FIXTURES / "janus"),
                    "HERMES_TAILSCALE": str(FIXTURES / "tailscale"), "HERMES_FAKE_DEVICE": str(self.device),
                    "HERMES_FAKE_TAILDROPS": str(self.phone), "USER": "owner"}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        for var in ("HERMES_FAKE_JANUS", "HERMES_FAKE_TAILDROP", "HERMES_JANUS_TIMEOUT", "SSH_CONNECTION"):
            os.environ.pop(var, None)
        self.doc = self.home / "docs" / "report.pdf"
        self.doc.write_bytes(b"%PDF report " * 100)
        os.chmod(self.doc, 0o644)

    def call(self, fn, *args, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            code = fn(*args, out=out)
        return code, out.getvalue(), err.getvalue()

    def listing(self, folder):
        d = self.stage / folder
        return sorted(p.name for p in d.iterdir() if p.name != ".door.lock") if d.is_dir() else []

    def everything(self):
        skip = (self.home / ".local/share/hermes/log", self.home / ".local/state")
        return sorted(str(p.relative_to(self.tmp.name)) for p in Path(self.tmp.name).rglob("*")
                      if p.is_file() and not any(s in p.parents for s in skip))

    def sent(self):
        return (self.device / "sent.log").read_text().splitlines() if (self.device / "sent.log").exists() else []

    # --- hermes send ---
    def test_send_goes_through_the_door_and_the_original_is_never_touched(self):
        before = self.doc.stat()
        code, out, err = self.call(send.send, "pcmail", [str(self.doc)])
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("report.pdf: sent to desktop", out)
        self.assertEqual((self.device / "hermes__report.pdf").read_bytes(), self.doc.read_bytes())
        self.assertEqual(self.sent(), ["cp %s desktop" % (self.stage / "in" / self.listing("delivered")[0] / "hermes__report.pdf")])
        after = self.doc.stat()
        self.assertEqual((after.st_mode, after.st_mtime, after.st_nlink, after.st_size), (before.st_mode, before.st_mtime, 1, before.st_size))
        aid = self.listing("delivered")[0]
        self.assertEqual(sorted(p.name for p in (self.stage / "delivered" / aid).iterdir()), ["meta.json", "verdict.json"])  # the link is gone
        meta = json.loads((self.stage / "delivered" / aid / "meta.json").read_text())
        self.assertEqual((meta["direction"], meta["route"], meta["name"], meta["sender"]["via"], meta["sender"]["user"], meta["source"]),
                         ("out", "pcmail", "report.pdf", "local", "owner", str(self.doc)))
        e = log.entries()[-1]
        self.assertEqual((e["outcome"], e["dir"], e["to"], e["name"], e["from"], e["verdict"]), ("sent", "out", "taildrop:desktop", "hermes__report.pdf", "local", "pass"))
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("outbox"), [])

    def test_send_stages_a_hardlink(self):
        with mock.patch.object(door, "taildrop_send", side_effect=lambda path, *a: (self.assertEqual(os.stat(path).st_nlink, 2), (True, ""))[1]):
            code, out, _ = self.call(send.send, "pcmail", [str(self.doc)])
        self.assertEqual(code, 0)
        self.assertEqual(self.doc.stat().st_nlink, 1)
        with mock.patch.object(send.os, "link", side_effect=OSError("cross-device")):     # another drive: a copy
            code, out, _ = self.call(send.send, "phone", [str(self.doc)])
        self.assertEqual(code, 0)
        self.assertIn("report.pdf: sent to phone", out)
        self.assertEqual((self.device / "report.pdf").read_bytes(), self.doc.read_bytes())

    def test_send_refusals(self):
        for args, code, fragment in [(("torrent", [str(self.doc)]), 2, "delivers on this server"), (("nope", [str(self.doc)]), 2, "no route 'nope'"),
                                     (("pcmail", []), 2, "nothing to send"), (("pcmail", [str(self.home / "missing.pdf")]), 1, "is not a file")]:
            with self.subTest(args):
                c, out, err = self.call(send.send, *args)
                self.assertEqual(c, code, err)
                self.assertIn(fragment, err)
        bad = self.home / "docs" / ".hidden.pdf"
        bad.write_bytes(b"x")
        c, out, err = self.call(send.send, "pcmail", [str(bad), str(self.doc)])
        self.assertEqual(c, 1)
        self.assertIn("refused: the name starts with a dot", err)
        self.assertIn("report.pdf: sent to desktop", out)
        self.assertEqual(self.listing("in"), [])

    def test_send_quarantine_leaves_the_original_alone(self):
        tool = self.home / "docs" / "tool.exe"
        tool.write_bytes(b"MZ")
        os.chmod(tool, 0o755)
        code, out, err = self.call(send.send, "pcmail", [str(tool)])
        self.assertEqual(code, 1)
        self.assertIn("tool.exe: quarantined: .exe files are never allowed on any route.", out)
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)       # no 0400 on a hardlinked original
        self.assertEqual(self.listing("quarantine"), self.listing("quarantine")[:1])
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.doc.stat().st_nlink, 1)

    # --- the outbox ---
    def test_offline_device_waits_retries_and_is_sent(self):
        code, out, err = self.call(send.send, "pcmail", [str(self.doc)], HERMES_FAKE_TAILDROP="offline")
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("report.pdf: waiting for desktop (asleep?); hermes collect keeps trying for 1 hours", out)
        aid = self.listing("outbox")[0]
        self.assertEqual(sorted(p.name for p in (self.stage / "outbox" / aid).iterdir()), ["hermes__report.pdf", "meta.json", "tries.txt", "verdict.json"])
        self.assertEqual(log.entries()[-1]["outcome"], "waiting")
        self.assertIn("peer is offline", log.entries()[-1]["reasons"][0])
        self.assertEqual(self.doc.stat().st_nlink, 2)
        code, out, _ = self.call(send.collect, HERMES_FAKE_TAILDROP="offline")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "hermes collect: 1 waiting")
        self.assertEqual(self.listing("outbox"), [aid])
        self.assertEqual(len((self.stage / "outbox" / aid / "tries.txt").read_text().splitlines()), 2)
        self.assertEqual(len(log.entries()), 1)                             # retries don't fill the log
        st = log.status(door.load(os.environ))
        self.assertEqual([i["id"] for i in st["outbox"]], [aid])
        self.assertIn("1 waiting to be sent", log.summary(st))
        self.assertIsNone(st["alert"])
        code, out, _ = self.call(send.collect)                              # the device is awake
        self.assertEqual(out.strip(), "hermes collect: 1 sent")
        self.assertEqual(self.listing("outbox"), [])
        self.assertEqual(self.listing("delivered"), [aid])
        self.assertEqual((self.device / "hermes__report.pdf").read_bytes(), self.doc.read_bytes())
        self.assertEqual(self.doc.stat().st_nlink, 1)
        self.assertEqual([e["outcome"] for e in log.entries()], ["waiting", "sent"])

    def test_offline_for_too_long_ends_undelivered_with_an_alert(self):
        self.call(send.send, "pcmail", [str(self.doc)], HERMES_FAKE_TAILDROP="offline")
        aid = self.listing("outbox")[0]
        tries = self.stage / "outbox" / aid / "tries.txt"
        old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        tries.write_text(old + " peer is offline\n")
        before = self.everything()
        code, out, _ = self.call(send.collect, HERMES_FAKE_TAILDROP="offline")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "hermes collect: 1 undelivered")
        self.assertEqual(self.listing("outbox"), [])
        self.assertEqual(self.listing("undelivered"), [aid])
        self.assertEqual(len(self.everything()), len(before))
        e = log.entries()[-1]
        self.assertEqual(e["outcome"], "undelivered")
        self.assertEqual(e["reasons"][-1], "desktop was unreachable for 1 hours.")
        alert = log.read_json(self.home / ".local/state/argus-ui/alerts/hermes.json")
        self.assertEqual(alert["message"], "1 delivery undelivered: desktop was unreachable for 1 hours.")
        st = log.status(door.load(os.environ))
        self.assertEqual(st["undelivered"][0]["name"], "report.pdf")
        self.assertIn("1 undelivered", log.summary(st))
        self.assertEqual(log.ack(door.load(os.environ)), 1)
        self.assertFalse((self.home / ".local/state/argus-ui/alerts/hermes.json").exists())
        code, out, _ = self.call(door.discard, aid)
        self.assertEqual(code, 0)
        self.assertEqual(self.listing("undelivered"), [])

    def test_release_of_an_outbound_quarantine_sends_it(self):
        tool = self.home / "docs" / "tool.exe"
        tool.write_bytes(b"MZ")
        self.call(send.send, "pcmail", [str(tool)])
        aid = self.listing("quarantine")[0]
        code, out, err = self.call(door.release, aid, "known-tool")
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual((self.device / "hermes__tool.exe").read_bytes(), b"MZ")
        e = log.entries()[-1]
        self.assertEqual((e["outcome"], e["released_by"]["reason"]), ("released", "known-tool"))
        self.assertEqual(self.listing("delivered"), [aid])

    # --- the phones' Taildrops ---
    def test_taildrops_land_by_kind_after_janus(self):
        (self.phone / "ubuntu.torrent").write_bytes(b"d8:announce")
        (self.phone / "photo.jpg").write_bytes(b"\xff\xd8 photo")
        (self.phone / "setup.exe").write_bytes(b"MZ")
        code, out, err = self.call(send.collect)
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(out.strip(), "hermes collect: 2 delivered, 1 quarantined")
        self.assertEqual((self.watch / "ubuntu.torrent").read_bytes(), b"d8:announce")
        self.assertEqual((self.mail / "photo.jpg").read_bytes(), b"\xff\xd8 photo")
        self.assertEqual(list(self.phone.iterdir()), [])
        self.assertEqual(list((self.stage / "incoming" / "taildrop").iterdir()), [])
        q = self.listing("quarantine")
        self.assertEqual(len(q), 1)
        meta = json.loads((self.stage / "quarantine" / q[0] / "meta.json").read_text())
        self.assertEqual((meta["name"], meta["route"], meta["direction"], meta["sender"]), ("setup.exe", "servermail", "in", {"via": "taildrop", "key": None, "node": None, "user": None}))
        self.assertEqual(sorted((e["from"], e["outcome"]) for e in log.entries()), [("taildrop", "delivered"), ("taildrop", "delivered"), ("taildrop", "quarantined")])

    def test_taildrop_without_a_route_is_set_aside(self):
        doc = json.loads(self.routes.read_text())
        del doc["taildrop"]["default"]
        self.routes.write_text(json.dumps(doc))
        (self.phone / "voice.m4a").write_bytes(b"audio")
        code, out, err = self.call(send.collect)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "hermes collect: nothing waiting; 1 failure")
        failed = self.listing("failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual((self.stage / "failed" / failed[0] / "data").read_bytes(), b"audio")
        self.assertIn("no taildrop route for '.m4a'", (self.stage / "failed" / failed[0] / "error.txt").read_text())
        self.assertEqual(log.entries()[-1]["outcome"], "failed")

    def test_a_quiet_collect_is_fast_and_writes_nothing(self):
        self.call(send.collect)                                              # the first run makes the pickup folder
        before = self.everything()
        t = time.monotonic()
        code, out, err = self.call(send.collect)
        took = time.monotonic() - t
        self.assertEqual((code, out.strip(), err), (0, "hermes collect: nothing waiting", ""))
        self.assertLess(took, 0.5, took)
        self.assertEqual(self.everything(), before)
        self.assertFalse((self.home / ".local/share/hermes/log").exists())
        self.assertFalse((self.home / ".local/state").exists())
        r = subprocess.run([sys.executable, "-m", "hermes", "collect"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.stdout.strip(), "hermes collect: nothing waiting")

    def test_collect_without_tailscale(self):
        code, out, err = self.call(send.collect, HERMES_TAILSCALE=str(self.home / "no-tailscale"))
        self.assertEqual(code, 1)
        self.assertIn("tailscale is not installed", err)

    def test_the_fake_tailscale_refuses_wait(self):
        r = subprocess.run([str(FIXTURES / "tailscale"), "file", "get", "--wait", str(self.home)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    # --- messages ---
    def test_magnet_message(self):
        magnet = b"magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=ubuntu"
        code, out, err = self.call(send.message, "torrent", magnet)
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("abcdef01.magnet: delivered", out)
        self.assertEqual((self.watch / "abcdef01.magnet").read_bytes(), magnet + b"\n")
        meta = json.loads((self.stage / "delivered" / self.listing("delivered")[0] / "meta.json").read_text())
        self.assertEqual((meta["message"], meta["sender"]["via"], meta["direction"]), ({"type": "magnet"}, "local", "in"))

    def test_note_and_link_to_the_pc(self):
        code, out, err = self.call(send.message, "pcmail", b"  buy milk\nand bread  ")
        self.assertEqual((code, err), (0, ""), err)
        self.assertRegex(out, r"^note-\d{8}-\d{6}\.txt: sent to desktop\n$")
        sent = [p for p in self.device.iterdir() if p.name.startswith("hermes__note-")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].read_bytes(), b"buy milk\nand bread\n")
        out = io.StringIO()
        code = send.message("pcmail", b"https://example.com/a?b=c\n", out=out, user="you@example.com")
        out = out.getvalue()
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^link-\d{8}-\d{6}\.txt: sent to desktop\n$")
        metas = [json.loads((self.stage / "delivered" / aid / "meta.json").read_text()) for aid in self.listing("delivered")]
        meta = [m for m in metas if m["name"].startswith("link-")][0]
        self.assertEqual((meta["message"]["type"], meta["sender"]["user"]), ("link", "you@example.com"))

    def test_message_refusals(self):
        for text, fragment in [(b"", "empty"), (b"   \n", "empty"), (b"x" * 4097, "4096 bytes at most"), (b"\xff\xfe", "UTF-8")]:
            with self.subTest(text[:10]):
                code, _, err = self.call(send.message, "pcmail", text)
                self.assertEqual(code, 2)
                self.assertIn(fragment, err)
        code, _, err = self.call(send.message, "nope", b"hi")
        self.assertEqual(code, 2)
        self.assertEqual(self.listing("in"), [])

    # --- the door ---
    def test_direction_must_match_the_route(self):
        aid = send.stage_arrival(door.load(os.environ).routes["torrent"], "a.torrent", data=b"x", sender={"via": "local"})
        (self.stage / "in" / aid / "meta.json").write_text(json.dumps(dict(json.loads((self.stage / "in" / aid / "meta.json").read_text()), direction="out")))
        self.call(door.run)
        self.assertEqual(self.listing("quarantine"), [aid])
        self.assertIn("goes out, but route torrent goes to a folder here", json.loads((self.stage / "quarantine" / aid / "verdict.json").read_text())["reasons"][0])

    def test_one_door_per_stage_at_a_time(self):
        send.stage_arrival(door.load(os.environ).routes["torrent"], "a.torrent", data=b"x", sender={"via": "local"})
        with open(self.stage / ".door.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            p = subprocess.Popen([sys.executable, "-m", "hermes", "door"], cwd=ROOT, stdout=subprocess.PIPE, text=True)
            time.sleep(0.7)
            self.assertIsNone(p.poll())                                      # blocked on the lock
            self.assertEqual(self.listing("in"), self.listing("in"))
            fcntl.flock(lock, fcntl.LOCK_UN)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(out.strip(), "hermes door: 1 delivered")

    # --- the command line, units, the menu, the retire script ---
    def test_command_line(self):
        def run(*args, stdin=None):
            return subprocess.run([sys.executable, "-m", "hermes", *args], capture_output=True, text=True, cwd=ROOT, input=stdin)
        r = run("send", "pcmail", str(self.doc))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "report.pdf: sent to desktop")
        r = run("message", "torrent", stdin="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.watch / "01234567.magnet").exists())
        r = run("message", "pcmail", stdin="x" * 5000)
        self.assertEqual(r.returncode, 2)
        r = run("collect")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hermes collect: nothing waiting"))
        r = run("send")
        self.assertEqual(r.returncode, 2)

    def test_units_and_menu(self):
        timer = (ROOT / "units/hermes-taildrop.timer").read_text()
        self.assertIn("OnUnitActiveSec=2min", timer)
        self.assertIn("Unit=hermes-taildrop.service", timer)
        service = (ROOT / "units/hermes-taildrop.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=%h/.local/bin/hermes collect", service)
        menu = (ROOT / "clients/dolphin/hermes-send-to-pc.desktop").read_text()
        self.assertIn("Name=Hermes: send to {{PC}}", menu)
        self.assertIn("send pcmail", menu)
        self.assertIn("%F", menu)

    def test_retire_script(self):
        units = Path(self.tmp.name) / "units"
        units.mkdir()
        (units / "collect-mail.service").write_text("[Service]\nExecStart=/bin/sh -c 'tailscale file get ~/Hermes-Mail'\n")
        (units / "collect-mail.timer").write_text("[Timer]\nOnUnitActiveSec=5min\n")
        (units / "hermes-taildrop.service").write_text("ExecStart=%h/.local/bin/hermes collect\n")   # ours: left alone
        (units / "other.service").write_text("ExecStart=/bin/true\n")
        env = dict(os.environ, HERMES_UNIT_DIR=str(units), PATH="/usr/bin:/bin")
        r = subprocess.run(["bash", str(ROOT / "scripts/retire-taildrop-collector.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would set aside %s/collect-mail.service" % units, r.stdout)
        self.assertIn("(preview", r.stdout)
        self.assertEqual(sorted(p.name for p in units.iterdir()), ["collect-mail.service", "collect-mail.timer", "hermes-taildrop.service", "other.service"])
        r = subprocess.run(["bash", str(ROOT / "scripts/retire-taildrop-collector.sh"), "--go"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        names = sorted(p.name for p in units.iterdir())
        self.assertEqual([n for n in names if not n.startswith("collect-mail.service.set-aside-")], ["collect-mail.timer", "hermes-taildrop.service", "other.service"])
        self.assertEqual(len(names), 4)
        r = subprocess.run(["bash", str(ROOT / "scripts/retire-taildrop-collector.sh")], capture_output=True, text=True, env=env)
        self.assertIn("nothing collects Taildrops here besides Hermes", r.stdout)


if __name__ == "__main__":
    unittest.main()
