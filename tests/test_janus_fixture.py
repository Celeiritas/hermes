"""The fake Janus in tests/fixtures/janus keeps to the contract the door relies on (docs/design.md §3)."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

JANUS = Path(__file__).resolve().parent / "fixtures" / "janus"


def arrival(root, name, data=b"hello door\n", **meta_over):
    d = Path(root) / name.replace(".", "_")
    d.mkdir()
    (d / "data").write_bytes(data)
    meta = {"id": "20260101-000000-000000", "route": "servermail", "direction": "in", "name": name,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "sender": {"via": "ssh", "key": "{{PC}}", "node": "{{PC}}"}, "hermes": "0.1.0"}
    meta.update(meta_over)
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def check(d, **env):
    r = subprocess.run([str(JANUS), "check", "--json", str(d)], capture_output=True, text=True,
                       env={**os.environ, **env})
    out = json.loads(r.stdout) if r.stdout else None
    return r.returncode, out, r.stderr


class FakeJanusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop("HERMES_FAKE_JANUS", None)

    def test_a_good_file_passes(self):
        d = arrival(self.tmp.name, "notes.txt")
        code, out, _ = check(d)
        self.assertEqual(code, 0)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["reasons"], [])
        self.assertEqual(out["sha256"], json.loads((d / "meta.json").read_text())["sha256"])
        self.assertEqual(out["checks"], {"route": "ok", "kind": "ok", "shape": "ok", "integrity": "ok"})
        self.assertIn("janus", out)

    def test_an_executable_is_quarantined_with_a_plain_reason(self):
        code, out, _ = check(arrival(self.tmp.name, "setup.exe"))
        self.assertEqual(code, 10)
        self.assertEqual(out["verdict"], "quarantine")
        self.assertEqual(out["checks"]["kind"], "fail")
        self.assertEqual(out["reasons"], [".exe files are never allowed on any route."])

    def test_a_wrong_hash_is_quarantined(self):
        code, out, _ = check(arrival(self.tmp.name, "notes.txt", sha256="0" * 64))
        self.assertEqual(code, 10)
        self.assertEqual(out["checks"]["integrity"], "fail")

    def test_a_duplicate(self):
        code, out, _ = check(arrival(self.tmp.name, "duplicate.txt"))
        self.assertEqual(code, 11)
        self.assertEqual(out["verdict"], "duplicate")

    def test_the_verdict_can_be_forced(self):
        d = arrival(self.tmp.name, "notes.txt")
        self.assertEqual(check(d, HERMES_FAKE_JANUS="quarantine")[0], 10)
        self.assertEqual(check(d, HERMES_FAKE_JANUS="duplicate")[0], 11)
        self.assertEqual(check(d, HERMES_FAKE_JANUS="fail")[0], 1)
        self.assertEqual(check(d, HERMES_FAKE_JANUS="pass")[0], 0)

    def test_a_missing_arrival_is_a_failure_not_a_verdict(self):
        code, out, err = check(Path(self.tmp.name) / "nothing-here")
        self.assertNotIn(code, (0, 10, 11))
        self.assertIn("cannot read", err)

    def test_bad_arguments(self):
        r = subprocess.run([str(JANUS), "release", "x"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)

    def test_never_writes_into_the_arrival(self):
        d = arrival(self.tmp.name, "notes.txt")
        before = sorted(p.name for p in d.iterdir())
        check(d)
        check(arrival(self.tmp.name, "tool.sh"))
        self.assertEqual(sorted(p.name for p in d.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
