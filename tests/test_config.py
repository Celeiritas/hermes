"""routes.json: load, expand, validate and explain (docs/design.md §2, issue #3). Every test builds its own temporary
HOME, so nothing on this machine is read. /srv/media is the one path in the example that is not under HOME; the test
puts it in the temporary folder too."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes import config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "routes.example.json"


class Home:
    """A temporary HOME with routes.json (the example by default) and every folder it names."""
    def __init__(self, tmp):
        self.home = Path(tmp) / "home"
        self.media = Path(tmp) / "srv-media"
        self.routes = self.home / ".config" / "hermes" / "routes.json"
        self.janus = self.home / ".config" / "janus" / "routes.json"
        self.realm = self.home / ".config" / "leafy" / "realm.json"
        self.routes.parent.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "HERMES_CONFIG": str(self.routes),
                                                "JANUS_CONFIG": str(self.janus), "LEAFY_REALM": str(self.realm)})
        self.env.start()
        self.doc = json.loads(EXAMPLE.read_text().replace("/srv/media", str(self.media)))
        self.make_folders()

    def make_folders(self):
        for folder in list(self.doc["stages"].values()) + [r["to"] for r in self.doc["routes"].values()]:
            if not folder.startswith("taildrop:"):
                Path(os.path.expanduser(folder)).mkdir(parents=True, exist_ok=True)

    def write(self, doc=None, text=None):
        self.routes.write_text(text if text is not None else json.dumps(doc or self.doc))
        return self

    def check(self, **kw):
        return config.check(**kw)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Home(self.tmp.name)
        self.addCleanup(self.h.env.stop)

    def route(self, rid, **changes):
        """The example with one route changed; None removes a setting."""
        doc = json.loads(json.dumps(self.h.doc))
        for k, v in changes.items():
            if v is None:
                doc["routes"][rid].pop(k, None)
            else:
                doc["routes"][rid][k] = v
        return doc

    # --- the example, placeholders, paths ---
    def test_the_example_passes_and_explains_every_route(self):
        cfg = self.h.write().check()
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.warnings, [])
        self.assertTrue(cfg.ok)
        self.assertEqual(len(cfg.routes), 9)
        lines = cfg.explain()
        self.assertIn("lesson: files go to ~/.local/share/argus-ui/inbox (staged in home on the same drive; "
                      "same name: the older copy is set aside).", lines)
        self.assertIn("torrent: files go to ~/Downloads/torrents/watch (staged in media on the same drive; "
                      "same name: the new file is set aside).", lines)
        self.assertIn("jellyfin-theme: files go to %s/Media/Shows/<subpath> (staged in media on the same drive; "
                      "same name: the new file is set aside), then rings jellyfin-refresh." % self.h.media, lines)
        self.assertIn("pcmail: files go to desktop by Taildrop as hermes__<name>, retried for 24 hours while the "
                      "device sleeps.", lines)
        self.assertIn("phone: files go to phone by Taildrop, retried for 24 hours while the device sleeps.", lines)
        self.assertIn("deploy-request: closed; files would go to ~/.local/share/zeus/requests (staged in home on the "
                      "same drive; same name: the new file gets (2)).", lines)
        self.assertFalse(cfg.routes["deploy-request"].enabled)
        self.assertTrue(cfg.routes["pcmail"].outbound)
        self.assertEqual(cfg.taildrop["default"], "servermail")
        self.assertEqual(cfg.fetch["decks"]["route"], "lesson")

    def test_placeholders_come_from_realm_json_with_public_defaults(self):
        self.assertEqual(config.realm()["PC"], "desktop")        # no realm.json: the public defaults
        self.h.realm.parent.mkdir(parents=True)
        self.h.realm.write_text(json.dumps({"pc": "study-pc", "PHONE": "", "SERVER": 7}))
        self.assertEqual(config.realm()["PC"], "study-pc")       # key case doesn't matter
        self.assertEqual(config.realm()["PHONE"], "phone")       # blank keeps the default
        self.assertEqual(config.realm()["SERVER"], "7")
        cfg = self.h.write().check()
        self.assertEqual(cfg.routes["pcmail"].device, "study-pc")
        self.h.realm.write_text("{ not json")
        self.assertEqual(config.realm(), config.REALM_DEFAULTS)  # a broken file means the defaults

    def test_fill_escapes_values_inside_json(self):
        vals = dict(config.REALM_DEFAULTS, HOME_PC="C:\\Users\\you")
        self.assertEqual(json.loads(config.fill('{"p": "{{ HOME_PC }}"}', vals, as_json=True))["p"], "C:\\Users\\you")
        self.assertEqual(config.fill("to {{PC}} and {{UNKNOWN}}", vals), "to desktop and {{UNKNOWN}}")

    def test_tilde_and_symlinks_are_resolved(self):
        real = self.h.home / "real-inbox"
        real.mkdir()
        (self.h.home / "link-inbox").symlink_to(real)
        cfg = self.h.write(self.route("lesson", to="~/link-inbox/")).check()
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.routes["lesson"].to, real.resolve())
        self.assertEqual(cfg.stages["home"], (self.h.home / ".local/share/hermes/stage").resolve())

    def test_hermes_config_env_and_explicit_path(self):
        other = Path(self.tmp.name) / "elsewhere.json"
        other.write_text(json.dumps(self.h.doc))
        self.assertTrue(config.check(path=other).ok)
        with mock.patch.dict(os.environ, {"HERMES_CONFIG": str(other)}):
            self.assertEqual(config.config_path(), other)
        self.assertEqual(config.check().errors[0][:33], "~/.config/hermes/routes.json: can")

    # --- errors: one test per rule ---
    def assertError(self, doc, fragment, text=None):
        cfg = self.h.write(doc, text).check()
        self.assertFalse(cfg.ok)
        hits = [e for e in cfg.errors if fragment in e]
        self.assertTrue(hits, "expected an error with %r, got %r" % (fragment, cfg.errors))
        return hits[0]

    def test_error_bad_route_id(self):
        doc = self.h.doc
        doc["routes"]["Lesson-2"] = doc["routes"]["lesson"]
        e = self.assertError(doc, "route 'Lesson-2': route ids are lowercase letters, digits and dashes")
        self.assertIn("32 characters at most", e)

    def test_error_to_is_not_a_folder(self):
        e = self.assertError(self.route("lesson", to="~/no-such-inbox/"), 'route lesson: "to": ~/no-such-inbox/ is not a folder')
        self.assertIn("mkdir -p ~/no-such-inbox/", e)

    def test_error_to_missing(self):
        self.assertError(self.route("lesson", to=None), 'route lesson: "to" is missing. Give the destination folder')

    def test_error_taildrop_without_a_device(self):
        self.assertError(self.route("pcmail", to="taildrop:"), 'route pcmail: "to": \'taildrop:\' must name a device')
        self.assertError(self.route("pcmail", to="taildrop:a b"), "must name a device")

    def test_error_stage_not_defined(self):
        e = self.assertError(self.route("lesson", stage="fast"), 'route lesson: "stage": \'fast\' names no stage')
        self.assertIn("stages defined: home, media", e)

    def test_error_stage_folder_missing(self):
        doc = self.h.doc
        doc["stages"]["home"] = "~/.local/share/hermes/elsewhere"
        e = self.assertError(doc, "stages.home: ~/.local/share/hermes/elsewhere is not a folder")
        self.assertIn("mkdir -p", e)
        self.assertFalse([x for x in self.h.check().errors if "names no stage" in x])   # reported once, not per route

    def test_error_conflict(self):
        self.assertError(self.route("lesson", conflict="overwrite"), 'route lesson: "conflict" must be rename, skip or replace, not \'overwrite\'')

    def test_error_after_is_not_a_known_doorbell(self):
        e = self.assertError(self.route("lesson", after="rm -rf /"), 'route lesson: "after" must be a doorbell Hermes knows (jellyfin-refresh)')
        self.assertIn("never runs a command", e)

    def test_error_to_inside_a_stage(self):
        inside = self.h.home / ".local/share/hermes/stage/in"
        inside.mkdir(parents=True)
        e = self.assertError(self.route("lesson", to=str(inside)), 'route lesson: "to" ~/.local/share/hermes/stage/in is inside stage home')
        self.assertIn("outside every stage", e)
        stage = self.h.home / ".local/share/hermes/stage"
        self.assertError(self.route("lesson", to=str(stage)), "is inside stage home")

    def test_error_version(self):
        doc = self.h.doc
        doc["version"] = 2
        self.assertError(doc, "version: must be 1")
        del doc["version"]
        self.assertError(doc, "version: must be 1")

    def test_error_missing_stages_or_routes(self):
        doc = self.h.doc
        doc["stages"] = {}
        self.assertError(doc, "stages: must map at least one stage name to a folder")
        doc = self.h.doc
        doc["routes"] = []
        self.assertError(doc, "routes: must map at least one route id")

    def test_error_taildrop_and_fetch_name_defined_routes(self):
        doc = self.h.doc
        doc["taildrop"]["by_kind"][".nzb"] = "usenet"
        self.assertError(doc, "taildrop.by_kind: .nzb names route 'usenet', which is not defined")
        doc = self.h.doc
        doc["taildrop"]["default"] = "mail"
        self.assertError(doc, "taildrop.default: route 'mail' is not defined")
        doc = self.h.doc
        doc["fetch"]["decks"]["route"] = "lessons"
        self.assertError(doc, 'fetch.decks: "route" must name a route defined under routes')

    def test_error_settings_of_the_wrong_kind(self):
        self.assertError(self.route("pcmail", retry_hours=0), 'route pcmail: "retry_hours" must be a number of hours above 0')
        self.assertError(self.route("pcmail", retry_hours=True), '"retry_hours" must be a number')
        self.assertError(self.route("pcmail", prefix="a/b"), 'route pcmail: "prefix" must be plain text with no slashes')
        self.assertError(self.route("lesson", subpath="yes"), 'route lesson: "subpath" must be true or false')
        self.assertError(self.route("lesson", enabled=0), 'route lesson: "enabled" must be true or false')

    def test_error_unreadable_or_broken_file(self):
        self.h.routes.unlink(missing_ok=True)
        e = config.check().errors
        self.assertEqual(len(e), 1)
        self.assertIn("~/.config/hermes/routes.json: cannot read it", e[0])
        self.assertIn("Copy examples/routes.example.json there", e[0])
        self.assertError(None, "not valid JSON", text='{ "version": 1, }')
        self.assertError(None, "must hold one JSON object", text="[]")

    def test_load_raises_with_the_errors(self):
        self.h.write(self.route("lesson", conflict="overwrite"))
        with self.assertRaises(config.ConfigError) as cm:
            config.load()
        self.assertEqual(len(cm.exception.config.errors), 1)
        self.assertIn("1 error; run `hermes check-config`", str(cm.exception))
        self.h.write()
        self.assertEqual(len(config.load().routes), 9)

    # --- warnings ---
    def test_warning_different_drives(self):
        stage = (self.h.home / ".local/share/hermes/stage").resolve()
        with mock.patch.object(config, "device_of", lambda p: 1 if Path(p) == stage else 2):
            cfg = self.h.write().check()
        self.assertTrue(cfg.ok)
        self.assertEqual(len(cfg.warnings), 3)   # lesson, servermail, deploy-request; pcmail and phone are outbound
        self.assertIn("route lesson: ~/.local/share/argus-ui/inbox and stage home (~/.local/share/hermes/stage) are on "
                      "different drives, so a passing file is copied instead of renamed", cfg.warnings[0])
        self.assertIn("Add a stage on the destination's drive", cfg.warnings[0])
        self.assertIn("(staged in home on another drive;", cfg.routes["lesson"].sentence())
        self.assertFalse([w for w in cfg.warnings if "torrent" in w])

    def test_warning_routes_janus_does_not_know(self):
        self.h.janus.parent.mkdir(parents=True)
        self.h.janus.write_text(json.dumps({"routes": {r: {} for r in self.h.doc["routes"] if r not in ("roms", "phone")}}))
        cfg = self.h.write().check()
        self.assertTrue(cfg.ok)
        self.assertEqual(cfg.warnings, ["Janus's ~/.config/janus/routes.json doesn't mention routes roms, phone: every "
                                        "arrival on them would be quarantined. Add them to Janus's file."])
        self.h.janus.write_text(json.dumps({"routes": {r: {} for r in self.h.doc["routes"]}}))
        self.assertEqual(self.h.check().warnings, [])
        self.h.janus.write_text("{ broken")
        self.assertIn("cannot read Janus's routes", self.h.check().warnings[0])

    def test_warning_unknown_setting(self):
        cfg = self.h.write(self.route("lesson", conflct="skip")).check()
        self.assertTrue(cfg.ok)
        self.assertEqual(cfg.warnings, ["route lesson: unknown setting 'conflct' is ignored (the settings are to, stage, "
                                        "conflict, subpath, after, prefix, retry_hours, enabled)."])

    # --- the command ---
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "hermes", "check-config", *args], cwd=ROOT, capture_output=True, text=True)

    def test_check_config_command(self):
        self.h.write()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 10)
        self.assertIn("lesson: files go to ~/.local/share/argus-ui/inbox", r.stdout)
        self.assertIn("~/.config/hermes/routes.json: 9 routes ok\n", r.stdout)
        self.assertEqual(r.stderr, "")

    def test_check_config_command_fails_on_errors_and_shows_warnings(self):
        self.h.write(self.route("lesson", conflict="overwrite", colour="red"))
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn('error: route lesson: "conflict" must be rename, skip or replace', r.stderr)
        self.assertIn("warning: route lesson: unknown setting 'colour'", r.stderr)
        self.assertIn("1 error. Fix them; Hermes delivers nothing", r.stderr)
        other = Path(self.tmp.name) / "fixed.json"
        other.write_text(json.dumps(self.route("lesson", colour="red")))
        r = self.run_cli("--config", str(other))
        self.assertEqual(r.returncode, 0)
        self.assertIn("9 routes ok, 1 warning", r.stdout)


if __name__ == "__main__":
    unittest.main()
