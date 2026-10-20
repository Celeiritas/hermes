"""hermes door and its companions: retry, release, discard, log, status, ack (docs/design.md §3 step 2, issue #5).
Each test builds its own HOME with a stage, routes and the fake Janus in tests/fixtures/janus; nothing on this
machine is read or written."""
import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes import door, log

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


class DoorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.stage = self.home / ".local/share/hermes/stage"
        self.inbox, self.watch, self.mail, self.shows = (self.home / d for d in ("inbox", "watch", "mail", "shows"))
        for d in (self.stage / "in", self.inbox, self.watch, self.mail, self.shows, self.home / ".config/hermes"):
            d.mkdir(parents=True)
        self.routes = self.home / ".config/hermes/routes.json"
        self.routes.write_text(json.dumps({"version": 1, "stages": {"home": "~/.local/share/hermes/stage"}, "routes": {
            "lesson": {"to": "~/inbox/", "stage": "home", "conflict": "replace"},
            "torrent": {"to": "~/watch/", "stage": "home", "conflict": "skip"},
            "servermail": {"to": "~/mail/", "stage": "home", "conflict": "rename"},
            "jellyfin-theme": {"to": "~/shows/", "stage": "home", "subpath": True, "conflict": "skip", "after": "jellyfin-refresh"},
            "pcmail": {"to": "taildrop:{{PC}}", "stage": "home"}},
            "fetch": {"jellyfin-themes": {"job": "hermes-jellyfin", "route": "jellyfin-theme", "allow": ["example.com"],
                                          "jellyfin": "http://localhost:8096", "key_file": "~/jellyfin.key"}}}))
        (self.home / "jellyfin.key").write_text("k3y\n")
        self.env = {"HOME": str(self.home), "HERMES_CONFIG": str(self.routes), "JANUS_CONFIG": str(self.home / "no-janus"),
                    "LEAFY_REALM": str(self.home / "no-realm"), "HERMES_JANUS": str(FIXTURES / "janus"),
                    "HERMES_TAILSCALE": str(FIXTURES / "tailscale"), "USER": "owner", "SSH_CONNECTION": "192.0.2.10 5 192.0.2.1 22"}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        for var in ("HERMES_FAKE_JANUS", "HERMES_JANUS_TIMEOUT"):
            os.environ.pop(var, None)
        self.n = 0

    # --- helpers ---
    def arrival(self, route, name, data=b"hello door\n", folder="in", **meta_over):
        self.n += 1
        aid = "20260101-000000-%06d" % self.n
        d = self.stage / folder / aid
        d.mkdir(parents=True)
        (d / "data").write_bytes(data)
        os.chmod(d / "data", 0o600)
        meta = {"id": aid, "route": route, "direction": "in", "name": name, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "sender": {"via": "ssh", "key": "desktop", "node": "desktop"},
                "received": "2026-01-01T00:00:00+00:00", "hermes": "0.1.4"}
        meta.update(meta_over)
        (d / "meta.json").write_text(json.dumps(meta))
        return aid

    def run_door(self, fn=door.run, *args, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            code = fn(*args, out=out) if fn is not door.run else fn(out=out)
        return code, out.getvalue(), err.getvalue()

    def listing(self, folder):
        d = self.stage / folder
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def everything(self):
        """Every file under the temporary HOME except logs and state: the proof that nothing is deleted."""
        skip = (self.home / ".local/share/hermes/log", self.home / ".local/state")
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")
                      if p.is_file() and not any(s in p.parents for s in skip))

    def log_entries(self):
        return log.entries()

    def alert(self):
        return log.read_json(self.home / ".local/state/argus-ui/alerts/hermes.json")

    # --- verdicts ---
    def test_pass_is_delivered(self):
        aid = self.arrival("lesson", "git basics.json")
        code, out, err = self.run_door()
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("1 delivered", out)
        self.assertEqual((self.inbox / "git basics.json").read_bytes(), b"hello door\n")
        self.assertEqual(stat.S_IMODE((self.inbox / "git basics.json").stat().st_mode) & 0o111, 0)
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("delivered"), [aid])
        self.assertEqual(sorted(p.name for p in (self.stage / "delivered" / aid).iterdir()), ["meta.json", "verdict.json"])
        self.assertEqual(json.loads((self.stage / "delivered" / aid / "verdict.json").read_text())["verdict"], "pass")
        e = self.log_entries()
        self.assertEqual(len(e), 1)
        self.assertEqual({k: e[0][k] for k in ("id", "route", "dir", "from", "to", "name", "bytes", "verdict", "outcome", "reasons")},
                         {"id": aid, "route": "lesson", "dir": "in", "from": "desktop", "to": "~/inbox/git basics.json",
                          "name": "git basics.json", "bytes": 11, "verdict": "pass", "outcome": "delivered", "reasons": []})
        self.assertRegex(e[0]["t"], r"^\d{4}-\d\d-\d\dT")
        self.assertIsNone(self.alert())
        self.assertEqual(self.run_door()[1].strip(), "hermes door: nothing waiting")

    def test_quarantine(self):
        aid = self.arrival("lesson", "setup.exe")
        code, out, err = self.run_door()
        self.assertEqual((code, err), (0, ""))                 # a quarantine is Janus doing his job, not a failure
        self.assertIn("1 quarantined", out)
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("quarantine"), [aid])
        q = self.stage / "quarantine" / aid
        self.assertEqual(sorted(p.name for p in q.iterdir()), ["data", "meta.json", "verdict.json"])
        self.assertEqual(stat.S_IMODE((q / "data").stat().st_mode), 0o400)
        self.assertEqual(json.loads((q / "verdict.json").read_text())["reasons"], [".exe files are never allowed on any route."])
        self.assertEqual(list(self.inbox.iterdir()), [])
        e = self.log_entries()[-1]
        self.assertEqual((e["outcome"], e["verdict"], e["reasons"]), ("quarantined", "quarantine", [".exe files are never allowed on any route."]))
        alert = self.alert()
        self.assertEqual({k: alert[k] for k in ("job", "level", "link")}, {"job": "hermes-door", "level": "alert", "link": "delivery"})
        self.assertEqual(alert["message"], "1 delivery quarantined: .exe files are never allowed on any route.")
        self.assertEqual(alert["since"], "2026-01-01T00:00:00+00:00")

    def test_duplicate(self):
        aid = self.arrival("lesson", "duplicate.json")
        code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        self.assertIn("1 skipped", out)
        self.assertEqual(self.listing("skipped"), [aid])
        self.assertEqual(self.log_entries()[-1]["outcome"], "skipped")
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertIsNone(self.alert())

    def assertHeld(self, aid, fragment, **env):
        code, out, err = self.run_door(**env)
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("1 held", out)
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("held"), [aid])
        self.assertIn(fragment, (self.stage / "held" / aid / "held.txt").read_text())
        self.assertEqual(list(self.inbox.iterdir()), [])          # never delivered without a pass
        e = self.log_entries()[-1]
        self.assertEqual(e["outcome"], "held")
        self.assertIn(fragment, e["reasons"][0])
        self.assertIn("held", self.alert()["message"])

    def test_janus_error_holds(self):
        self.assertHeld(self.arrival("lesson", "a.json"), "janus check failed (exit 1", HERMES_FAKE_JANUS="fail")

    def test_janus_timeout_holds(self):
        self.assertHeld(self.arrival("lesson", "a.json"), "gave no answer in 0.3 s", HERMES_FAKE_JANUS="hang", HERMES_JANUS_TIMEOUT="0.3")

    def test_janus_missing_holds(self):
        self.assertHeld(self.arrival("lesson", "a.json"), "Janus is not installed", HERMES_JANUS=str(self.home / "no-janus"))

    def test_janus_answer_that_does_not_match_its_exit_holds(self):
        self.assertHeld(self.arrival("lesson", "a.json"), "not a matching verdict", HERMES_FAKE_JANUS="garbage")

    def test_held_is_retried_on_the_next_run_and_by_hermes_retry(self):
        aid = self.arrival("lesson", "a.json")
        self.run_door(HERMES_FAKE_JANUS="fail")
        self.assertEqual(self.listing("held"), [aid])
        code, out, _ = self.run_door()                           # Janus is back
        self.assertEqual(code, 0)
        self.assertIn("1 delivered", out)
        self.assertEqual(self.listing("held"), [])
        self.assertTrue((self.inbox / "a.json").exists())
        self.assertIsNone(self.alert())
        aid2 = self.arrival("lesson", "b.json")
        self.run_door(HERMES_FAKE_JANUS="fail")
        code, out, _ = self.run_door(door.retry, aid2)
        self.assertEqual(code, 0)
        self.assertIn("1 delivered", out)
        self.assertTrue((self.inbox / "b.json").exists())
        code, _, err = self.run_door(door.retry, "20260101-000000-999999")
        self.assertEqual(code, 1)
        self.assertIn("no arrival", err)

    def test_janus_timeout_scales_with_size(self):
        self.assertEqual(door.janus_timeout(0, {}), 30)
        self.assertEqual(door.janus_timeout(200 * 1024 * 1024, {}), 40)
        self.assertEqual(door.janus_timeout(10 ** 9, {"HERMES_JANUS_TIMEOUT": "2"}), 2)

    # --- conflicts ---
    def test_conflict_rename(self):
        (self.mail / "note.txt").write_bytes(b"old")
        self.arrival("servermail", "note.txt", b"new")
        self.arrival("servermail", "note.txt", b"newer")
        self.run_door()
        self.assertEqual((self.mail / "note.txt").read_bytes(), b"old")
        self.assertEqual((self.mail / "note (2).txt").read_bytes(), b"new")
        self.assertEqual((self.mail / "note (3).txt").read_bytes(), b"newer")
        self.assertEqual(self.log_entries()[-1]["to"], "~/mail/note (3).txt")

    def test_conflict_skip(self):
        (self.watch / "a.torrent").write_bytes(b"old")
        aid = self.arrival("torrent", "a.torrent", b"new")
        code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        self.assertIn("1 skipped", out)
        self.assertEqual((self.watch / "a.torrent").read_bytes(), b"old")
        self.assertEqual((self.stage / "skipped" / aid / "data").read_bytes(), b"new")
        e = self.log_entries()[-1]
        self.assertEqual((e["outcome"], e["verdict"]), ("skipped", "pass"))
        self.assertIn("already there", e["reasons"][0])

    def test_conflict_replace(self):
        (self.inbox / "lesson.json").write_bytes(b"old")
        aid = self.arrival("lesson", "lesson.json", b"new")
        self.run_door()
        self.assertEqual((self.inbox / "lesson.json").read_bytes(), b"new")
        self.assertEqual((self.stage / "replaced" / aid / "lesson.json").read_bytes(), b"old")

    # --- nothing lost in a crash ---
    def test_a_run_interrupted_after_the_verdict_finishes_without_asking_janus_again(self):
        aid = self.arrival("lesson", "a.json")
        (self.stage / "in" / aid / "verdict.json").write_text(json.dumps({"verdict": "pass", "reasons": [], "janus": "x"}))
        code, out, _ = self.run_door(HERMES_JANUS=str(self.home / "no-janus"))   # no Janus at all, yet the saved pass counts
        self.assertEqual(code, 0)
        self.assertIn("1 delivered", out)
        self.assertTrue((self.inbox / "a.json").exists())
        aid = self.arrival("lesson", "b.json")
        (self.stage / "in" / aid / "verdict.json").write_text(json.dumps({"verdict": "quarantine", "reasons": ["saved"], "janus": "x"}))
        self.run_door(HERMES_JANUS=str(self.home / "no-janus"))
        self.assertEqual(self.listing("quarantine"), [aid])
        self.assertFalse((self.inbox / "b.json").exists())

    def test_a_run_interrupted_after_the_data_moved_neither_doubles_the_file_nor_the_log_line(self):
        aid = self.arrival("lesson", "a.json", b"moved already")
        (self.stage / "in" / aid / "verdict.json").write_text(json.dumps({"verdict": "pass", "reasons": []}))
        os.rename(self.stage / "in" / aid / "data", self.inbox / "a.json")            # the crash came after the rename...
        log.write({"id": aid, "route": "lesson", "outcome": "delivered", "name": "a.json"})   # ...and after the log line
        before = self.everything()
        code, out, _ = self.run_door(HERMES_JANUS=str(self.home / "no-janus"))
        self.assertEqual(code, 0)
        self.assertIn("1 delivered", out)
        self.assertEqual(self.listing("delivered"), [aid])
        self.assertEqual(len([e for e in self.log_entries() if e["id"] == aid]), 1)
        self.assertEqual((self.inbox / "a.json").read_bytes(), b"moved already")
        self.assertEqual(len(list(self.inbox.iterdir())), 1)
        self.assertEqual(len(self.everything()), len(before))

    def test_a_replace_interrupted_after_the_old_copy_was_set_aside(self):
        aid = self.arrival("lesson", "a.json", b"new")
        (self.stage / "in" / aid / "verdict.json").write_text(json.dumps({"verdict": "pass", "reasons": []}))
        (self.stage / "replaced" / aid).mkdir(parents=True)
        (self.stage / "replaced" / aid / "a.json").write_bytes(b"old")
        self.run_door(HERMES_JANUS=str(self.home / "no-janus"))
        self.assertEqual((self.inbox / "a.json").read_bytes(), b"new")
        self.assertEqual((self.stage / "replaced" / aid / "a.json").read_bytes(), b"old")

    def test_a_held_file_interrupted_halfway_is_not_doubled(self):
        aid = self.arrival("lesson", "a.json")
        (self.stage / "held" / aid).mkdir(parents=True)        # the same id in held/ and in/: in/ wins, held/ stays put
        (self.stage / "held" / aid / "held.txt").write_text("x\n")
        code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        self.assertEqual(self.listing("held"), [aid])
        self.assertTrue((self.inbox / "a.json").exists())

    # --- the door is the boundary ---
    def test_names_are_checked_again_at_the_door(self):
        for name in ("../up.json", ".hidden", "a/b.json", "photo\u202egpj.exe", "x" * 201):
            with self.subTest(name):
                aid = self.arrival("lesson", name)
                code, out, _ = self.run_door(HERMES_JANUS=str(self.home / "no-janus"))   # Janus never needed
                self.assertEqual(code, 0)
                self.assertEqual(self.listing("in"), [])
                self.assertIn(aid, self.listing("quarantine"))
                v = json.loads((self.stage / "quarantine" / aid / "verdict.json").read_text())
                self.assertEqual((v["verdict"], v["janus"]), ("quarantine", None))
                self.assertTrue(v["reasons"][0].startswith("Hermes refused the file's name"), v)
        self.assertEqual(list(self.inbox.iterdir()), [])
        aid = self.arrival("lesson", 7)
        self.run_door()
        self.assertIn(aid, self.listing("quarantine"))

    def test_subpath_stays_inside_the_destination(self):
        aid = self.arrival("jellyfin-theme", "theme.mp3", subpath="Some Show/Season 1")
        with mock.patch.object(door.urllib.request, "urlopen") as bell:
            self.run_door()
        self.assertEqual((self.shows / "Some Show/Season 1/theme.mp3").read_bytes(), b"hello door\n")
        self.assertEqual(bell.call_count, 1)
        for sub in ("../elsewhere", "a/../../b", "/abs", ".hidden/x", "Show\u202e"):
            with self.subTest(sub):
                aid = self.arrival("jellyfin-theme", "theme.mp3", subpath=sub)
                self.run_door()
                self.assertIn(aid, self.listing("quarantine"))
        aid = self.arrival("lesson", "a.json", subpath="sub")     # lesson is not a subpath route
        self.run_door()
        self.assertIn(aid, self.listing("quarantine"))
        self.assertIn("does not allow", json.loads((self.stage / "quarantine" / aid / "verdict.json").read_text())["reasons"][0])

    def test_unknown_route_and_outbound_route(self):
        aid = self.arrival("gone", "a.json")
        self.run_door()
        self.assertIn(aid, self.listing("quarantine"))
        self.assertIn("no route 'gone'", json.loads((self.stage / "quarantine" / aid / "verdict.json").read_text())["reasons"][0])
        aid = self.arrival("pcmail", "a.json")
        self.run_door()
        self.assertIn(aid, self.listing("held"))

    def test_junk_in_in_leaves_in(self):
        (self.stage / "in" / "stray.txt").write_text("x")
        (self.stage / "in" / "20260101-000000-000099").mkdir()
        (self.stage / "in" / "20260101-000000-000099" / "data").write_text("no meta")
        code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("failed"), ["20260101-000000-000099", "stray.txt"])
        self.assertIn("no readable meta.json", (self.stage / "failed" / "20260101-000000-000099" / "error.txt").read_text())
        self.assertEqual(self.log_entries()[-1]["outcome"], "failed")

    # --- the door's own failures ---
    def test_broken_config_is_the_doors_failure(self):
        self.arrival("lesson", "a.json")
        self.routes.write_text("{ broken")
        code, _, err = self.run_door()
        self.assertEqual(code, 1)
        self.assertIn("hermes check-config", err)
        self.assertEqual(len(self.listing("in")), 1)

    def test_an_unexpected_error_holds_the_arrival_and_fails_the_run(self):
        aid = self.arrival("lesson", "a.json")
        with mock.patch.object(door, "place", side_effect=OSError("disk full")):
            code, _, err = self.run_door()
        self.assertEqual(code, 1)
        self.assertIn("unexpected error", err)
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("held"), [aid])
        self.assertIn("disk full", (self.stage / "held" / aid / "held.txt").read_text())
        self.assertIn("OSError: disk full", (self.home / ".local/share/hermes/log/door-errors.log").read_text())
        code, out, _ = self.run_door()                           # the next run finishes it: the verdict was saved
        self.assertEqual(code, 0)
        self.assertTrue((self.inbox / "a.json").exists())

    # --- across drives, SELinux, doorbells ---
    def test_across_drives_copy_fsync_rename(self):
        aid = self.arrival("lesson", "a.json", b"x" * 5000)
        with mock.patch("hermes.config.device_of", side_effect=lambda p: 1 if str(p).endswith("stage") else 2), \
                mock.patch.object(door.os, "rename", wraps=os.rename) as rename:
            code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        self.assertEqual((self.inbox / "a.json").read_bytes(), b"x" * 5000)
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["a.json"])  # the .part file was renamed into place
        self.assertIn(mock.call(self.inbox / (".hermes-%s.part" % aid), self.inbox / "a.json"), rename.call_args_list)
        self.assertFalse((self.stage / "delivered" / aid / "data").exists())

    def test_selinux_label_is_copied_from_the_folder(self):
        self.arrival("lesson", "a.json")
        real, calls = subprocess.run, []
        with mock.patch.object(door, "selinux_on", return_value=True), \
                mock.patch.object(door.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd) if cmd[0] == "chcon" else real(cmd, **kw)):
            self.run_door()
        self.assertEqual(calls, [["chcon", "--reference", str(self.inbox), str(self.inbox / "a.json")]])

    def test_doorbell_rings_after_a_delivery_and_never_fails_the_door(self):
        self.arrival("jellyfin-theme", "theme.mp3", subpath="Show")
        with mock.patch.object(door.urllib.request, "urlopen") as urlopen:
            code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        req = urlopen.call_args.args[0]
        self.assertEqual((req.full_url, req.method, req.get_header("X-emby-token")), ("http://localhost:8096/Library/Refresh", "POST", "k3y"))
        self.arrival("jellyfin-theme", "theme.mp3", subpath="Other")
        with mock.patch.object(door.urllib.request, "urlopen", side_effect=OSError("refused")):
            code, out, _ = self.run_door()
        self.assertEqual(code, 0)
        self.assertIn("doorbell jellyfin-refresh failed", out)
        self.assertTrue((self.shows / "Other/theme.mp3").exists())
        self.assertEqual(door.DOORBELLS.keys(), {"jellyfin-refresh"})

    # --- release, discard, ack, log, status ---
    def test_release_delivers_anyway_and_logs_who(self):
        aid = self.arrival("lesson", "tool.exe")
        self.run_door()
        self.assertIsNotNone(self.alert())
        code, _, err = self.run_door(door.release, aid, "bad word")
        self.assertEqual(code, 1)
        self.assertIn("one word", err)
        code, out, err = self.run_door(door.release, aid, "checked-by-hand")
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("released", out)
        self.assertEqual((self.inbox / "tool.exe").read_bytes(), b"hello door\n")
        self.assertEqual(stat.S_IMODE((self.inbox / "tool.exe").stat().st_mode) & 0o111, 0)
        self.assertEqual(self.listing("quarantine"), [])
        self.assertEqual(self.listing("delivered"), [aid])
        e = self.log_entries()[-1]
        self.assertEqual((e["outcome"], e["verdict"]), ("released", "quarantine"))
        self.assertEqual({k: e["released_by"][k] for k in ("user", "reason", "node", "ip")}, {"user": "owner", "reason": "checked-by-hand", "node": "desktop", "ip": "192.0.2.10"})
        v = json.loads((self.stage / "delivered" / aid / "verdict.json").read_text())
        self.assertEqual(v["released"]["reason"], "checked-by-hand")
        self.assertIsNone(self.alert())
        code, _, err = self.run_door(door.release, aid, "again")
        self.assertEqual(code, 1)
        self.assertIn("no arrival", err)

    def test_release_still_refuses_a_bad_name(self):
        aid = self.arrival("lesson", "../up.json")
        self.run_door()
        code, _, err = self.run_door(door.release, aid, "please")
        self.assertEqual(code, 1)
        self.assertIn("cannot be released", err)
        self.assertEqual(self.listing("quarantine"), [aid])
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_discard_moves_never_deletes(self):
        aid = self.arrival("lesson", "tool.exe")
        self.run_door()
        before = self.everything()
        code, out, _ = self.run_door(door.discard, aid)
        self.assertEqual(code, 0)
        self.assertIn(".set-aside-", out)
        aside = [p for p in (self.home / ".local/share/hermes").iterdir() if p.name.startswith(".set-aside-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / aid / "data").read_bytes(), b"hello door\n")
        self.assertEqual(len(self.everything()), len(before))
        self.assertEqual(self.log_entries()[-1]["outcome"], "discarded")
        self.assertIsNone(self.alert())

    def test_nothing_is_ever_deleted_across_a_mixed_run(self):
        (self.inbox / "l.json").write_bytes(b"old")
        (self.watch / "t.torrent").write_bytes(b"old")
        self.arrival("lesson", "l.json", b"new")
        self.arrival("torrent", "t.torrent", b"new")
        self.arrival("lesson", "bad.exe")
        self.arrival("lesson", "duplicate.json")
        self.arrival("gone", "x")
        before = self.everything()
        self.run_door()
        after = self.everything()
        self.assertEqual(len(after), len(before) + 5)          # one verdict.json per arrival, and nothing gone
        self.assertEqual(self.listing("in"), [])

    def test_ack_clears_the_alert_until_something_new(self):
        self.arrival("lesson", "a.exe")
        self.run_door()
        self.assertIsNotNone(self.alert())
        from hermes import config
        self.assertEqual(log.ack(config.load()), 1)
        self.assertIsNone(self.alert())
        self.run_door()                                          # a run with nothing new keeps it clear
        self.assertIsNone(self.alert())
        self.arrival("lesson", "b.exe")
        self.run_door()
        self.assertEqual(self.alert()["message"][:22], "1 delivery quarantined")
        self.run_door(door.discard, self.listing("quarantine")[0])
        self.assertIsNotNone(self.alert())

    def test_status_and_log(self):
        self.arrival("lesson", "a.json")
        self.arrival("lesson", "b.exe")
        self.arrival("lesson", "c.json")
        self.run_door(HERMES_FAKE_JANUS="fail")
        (self.stage / "failed" / "20260101-000000-000077").mkdir(parents=True)
        (self.stage / "failed" / "20260101-000000-000077" / "error.txt").write_text("the stream ended after 1 of 2 bytes\n")
        self.run_door()
        from hermes import config
        st = log.status(config.load())
        self.assertEqual(st["today"], {"held": 3, "delivered": 2, "quarantined": 1})
        self.assertEqual([i["name"] for i in st["quarantine"]], ["b.exe"])
        self.assertEqual(st["quarantine"][0]["reasons"], [".exe files are never allowed on any route."])
        self.assertEqual(st["held"], [])
        self.assertEqual(st["failed"][0]["id"], "20260101-000000-000077")
        self.assertEqual(st["failed"][0]["reasons"], ["the stream ended after 1 of 2 bytes"])
        self.assertEqual(st["alert"]["message"][:22], "1 delivery quarantined")
        self.assertEqual(log.summary(st), "2 delivered today · 3 held today · 1 quarantined today · 1 quarantined · 1 failed transfers")
        lines = [log.line(e) for e in log.entries()]
        self.assertEqual(len(lines), 6)
        self.assertRegex(lines[-1], r"^\d{4}-\d\d-\d\d \d\d:\d\d  lesson         desktop → ~/inbox/c.json  c.json  11 B  delivered$")
        self.assertIn("quarantined  (.exe files are never allowed on any route.)", "\n".join(lines))
        self.assertEqual(len(log.entries(limit=2)), 2)

    def test_command_line(self):
        self.arrival("lesson", "a.json")
        self.arrival("lesson", "b.exe")

        def run(*args):
            return subprocess.run([sys.executable, "-m", "hermes", *args], capture_output=True, text=True, cwd=ROOT)
        r = run("door")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hermes door: 1 delivered, 1 quarantined")
        r = run("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stdout)
        self.assertEqual(len(st["quarantine"]), 1)
        self.assertEqual(st["today"], {"delivered": 1, "quarantined": 1})
        r = run("status")
        self.assertIn("1 delivered today · 1 quarantined today · 1 quarantined", r.stdout)
        self.assertIn("quarantine  ", r.stdout)
        r = run("log", "--json", "-n", "1")
        self.assertEqual(json.loads(r.stdout)["outcome"], "quarantined")
        r = run("log")
        self.assertEqual(len(r.stdout.splitlines()), 2)
        r = run("ack")
        self.assertEqual(r.returncode, 0)
        self.assertIn("1 item seen", r.stdout)
        self.assertIsNone(self.alert())
        aid = st["quarantine"][0]["id"]
        r = run("release", aid, "--reason", "fine")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.inbox / "b.exe").exists())
        r = run("release", aid)
        self.assertEqual(r.returncode, 2)
        r = run("discard", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no arrival", r.stderr)
        r = run("retry", "nope")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
