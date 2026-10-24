"""The Delivery plugin's Python side (plugin/plugin.py, docs/design.md §6, issue #11): reads go through Hermes's own code and
change nothing; every write runs a fixed command (hermes message / hermes ack / systemctl --user start for an allowlisted
job), never a shell; server paths never reach the page. The page itself is tested in a browser (tests/delivery/)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SHA = "ab" * 32


def load_plugin():
    spec = importlib.util.spec_from_file_location("delivery_plugin", ROOT / "plugin" / "plugin.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Ctx:
    def __init__(self, headers=None):
        self.headers = headers or {}


class Done:
    def __init__(self, code=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


class DeliveryPluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.stage = self.home / ".local/share/hermes/stage"
        watch, mail = Path(self.tmp.name) / "watch", Path(self.tmp.name) / "mail"
        for d in (self.stage, watch, mail, self.home / ".config/hermes", self.home / ".local/share/hermes/log"):
            d.mkdir(parents=True)
        routes = self.home / ".config/hermes/routes.json"
        routes.write_text(json.dumps({"version": 1, "stages": {"home": "~/.local/share/hermes/stage"}, "routes": {
            "torrent": {"to": str(watch), "stage": "home", "conflict": "skip"},
            "pcmail": {"to": "taildrop:{{PC}}", "stage": "home", "retry_hours": 1},
            "servermail": {"to": str(mail), "stage": "home"},
            "closed": {"to": str(mail), "stage": "home", "enabled": False}},
            "fetch": {"decks": {"job": "hermes-decks", "route": "servermail", "repo": "git@example.com:you/realm.git", "paths": ["decks/*.json"]},
                      "odd": {"job": "rm-rf", "route": "servermail"}}}))
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "HERMES_CONFIG": str(routes), "JANUS_CONFIG": str(self.home / "no-janus"),
               "LEAFY_REALM": str(self.home / "no-realm"), "HERMES_LIB": str(ROOT), "HERMES_BIN": "/opt/test/hermes"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.p = load_plugin()

    def log(self, *entries):
        month = entries[0]["t"][:7]
        with open(self.home / ".local/share/hermes/log" / ("deliveries-%s.jsonl" % month), "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def quarantine(self, aid="20261023-101500-abcd", reasons=("an executable (.exe) is never delivered",)):
        d = self.stage / "quarantine" / aid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": aid, "route": "servermail", "name": "setup.exe", "bytes": 2048, "sha256": SHA,
                                                 "sender": {"key": "{{PHONE}}", "via": "ssh"}, "received": "2026-10-23T10:15:00+00:00"}))
        (d / "verdict.json").write_text(json.dumps({"verdict": "quarantine", "reasons": list(reasons)}))
        return d

    # --- reads ---
    def test_the_snapshot_reads_the_log_newest_first_and_never_shows_server_paths(self):
        from hermes import log
        self.log({"t": log.now(), "id": "a1", "route": "torrent", "dir": "in", "from": "{{PHONE}}", "to": "~/watch/x.torrent", "name": "x.torrent", "bytes": 900, "outcome": "delivered", "reasons": []},
                 {"t": log.now(), "id": "a2", "route": "servermail", "dir": "in", "from": "{{PHONE}}", "to": None, "name": "setup.exe", "bytes": 2048, "outcome": "quarantined", "reasons": ["an executable"]})
        q = self.quarantine()
        code, s = self.p.get("", {}, Ctx())
        self.assertEqual(code, 200)
        self.assertTrue(s["ok"])
        self.assertEqual([e["id"] for e in s["recent"]], ["a2", "a1"])
        self.assertEqual(s["quarantine"][0]["sha256"], SHA)
        self.assertEqual(s["quarantine"][0]["reasons"], ["an executable (.exe) is never delivered"])
        self.assertNotIn(str(q), json.dumps(s), "the arrival's path on the server stays on the server")
        self.assertIn("1 delivered today", s["line"])
        self.assertIn("1 quarantined", s["line"])

    def test_the_send_box_offers_only_open_routes(self):
        routes = {r["id"]: r["to"] for r in self.p.get("", {}, Ctx())[1]["routes"]}
        self.assertEqual(sorted(routes), ["pcmail", "servermail", "torrent"])
        self.assertIn("by Taildrop", routes["pcmail"])

    def test_fetchers_are_phone_mail_plus_allowlisted_jobs_only(self):
        jobs = [f["job"] for f in self.p.get("", {}, Ctx())[1]["fetchers"]]
        self.assertEqual(jobs, ["hermes-taildrop", "hermes-decks"])

    def test_reads_change_nothing(self):
        self.quarantine()
        before = sorted(str(p) for p in self.home.rglob("*"))
        self.p.get("", {}, Ctx())
        self.p.get("status", {}, Ctx())
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")), before)

    def test_home_status_says_needs_you_while_an_alert_is_up(self):
        self.assertEqual(self.p.get("status", {}, Ctx())[1]["needs"], False)
        self.quarantine()
        from hermes import config, log
        log.update_alert(config.load())
        st = self.p.get("status", {}, Ctx())[1]
        self.assertTrue(st["needs"])
        self.assertEqual(st["action"], "Look at the set-aside one")

    def test_a_broken_config_is_said_not_raised(self):
        (self.home / ".config/hermes/routes.json").write_text("{ nope")
        code, s = self.p.get("", {}, Ctx())
        self.assertEqual((code, s["ok"]), (200, False))
        self.assertIn("routes.json", s["problem"])

    def test_hermes_missing_is_said_not_raised(self):
        with mock.patch.dict(os.environ, {"HERMES_LIB": str(self.home / "nothing")}):
            p = load_plugin()
            s = p.get("", {}, Ctx())[1]
        self.assertFalse(s["ok"])
        self.assertIn("not installed", s["problem"])

    # --- writes ---
    def test_send_runs_hermes_message_with_the_text_on_stdin_and_who_asked(self):
        with mock.patch.object(self.p.subprocess, "run", return_value=Done(0, b"note-1.txt: delivered\n")) as run:
            code, r = self.p.post("send", {"route": "servermail", "text": "  buy milk  "}, Ctx({"Tailscale-User-Login": "owner@example.com"}))
        self.assertEqual((code, r["ok"]), (200, True))
        args, kw = run.call_args
        self.assertEqual(args[0], ["/opt/test/hermes", "message", "servermail", "--user", "owner@example.com"])
        self.assertEqual(kw["input"], b"buy milk")
        self.assertNotIn("shell", kw)
        self.assertIn("delivered", r["msg"])

    def test_send_refuses_bad_routes_empty_and_long_text(self):
        with mock.patch.object(self.p.subprocess, "run") as run:
            for body in ({"route": "--help", "text": "x"}, {"route": "a b", "text": "x"}, {"route": "servermail", "text": "   "},
                         {"route": "servermail", "text": "x" * 4097}, {"route": "servermail", "text": 5}):
                self.assertEqual(self.p.post("send", body, Ctx())[0], 400, body)
        run.assert_not_called()

    def test_run_starts_only_allowlisted_jobs(self):
        with mock.patch.object(self.p.subprocess, "run", return_value=Done()) as run:
            self.assertEqual(self.p.post("run", {"job": "hermes-decks"}, Ctx())[1]["ok"], True)
            self.assertEqual(run.call_args[0][0], ["systemctl", "--user", "start", "--no-block", "hermes-decks.service"])
            for job in ("argus-ui", "hermes-decks; reboot", "rm-rf", ""):
                self.assertEqual(self.p.post("run", {"job": job}, Ctx())[0], 400, job)
        self.assertEqual(run.call_count, 1)

    def test_seen_runs_hermes_ack_and_unknown_posts_are_404(self):
        with mock.patch.object(self.p.subprocess, "run", return_value=Done()) as run:
            self.assertTrue(self.p.post("seen", {}, Ctx())[1]["ok"])
            self.assertEqual(run.call_args[0][0], ["/opt/test/hermes", "ack"])
            self.assertEqual(self.p.post("release", {"id": "x"}, Ctx())[0], 404, "no Release button in v1 (issue #27)")
        self.assertEqual(run.call_count, 1)

    def test_run_reports_failures_and_missing_commands(self):
        self.assertEqual(self.p.run([sys.executable, "-c", "import sys; print('no'); sys.exit(3)"]), (3, "no"))
        self.assertEqual(self.p.run([str(self.home / "missing")])[0], 127)
        self.assertEqual(self.p.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], stdin=b"hi"), (0, "hi"))


if __name__ == "__main__":
    unittest.main()
