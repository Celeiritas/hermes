"""The Windows client (issue #6, docs/windows-client.md). CI has no PowerShell and no server, so this tests the protocol
on Linux with exactly the arguments hermes.ps1 composes (the name as base64url, the byte count, the lower-case SHA-256)
and checks the scripts for the things that must be true of them. The manual checklist in the doc covers the rest."""
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from hermes import receive

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "clients" / "windows"
PS1 = (CLIENT / "hermes.ps1").read_text(encoding="utf-8")
SENDTO = (CLIENT / "install-sendto.ps1").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "windows-client.md").read_text(encoding="utf-8")
ALL256 = bytes(range(256))
NAMES = ["Report (final) 'draft' [v2] é ü ß 日本語.txt", "a  two  spaces.txt", "comma, semi; amp & eq = pct %.txt",
         "café.txt", "naïve–dash—long.pdf", "x" * 200]


def b64url_like_the_client(name):
    """[Convert]::ToBase64String(UTF8 bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')"""
    return base64.b64encode(name.encode("utf-8")).decode().rstrip("=").replace("+", "-").replace("/", "_")


def put_like_the_client(route, name, data):
    """The SSH_ORIGINAL_COMMAND hermes.ps1 produces: put <route> <name-b64url> <bytes> <sha256 lower-case>."""
    return "put %s %s %d %s" % (route, b64url_like_the_client(name), len(data), hashlib.sha256(data).hexdigest().lower())


class ProtocolTest(unittest.TestCase):
    """The same arguments against the real receive, in a temporary HOME."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.stage = self.home / ".local/share/hermes/stage"
        for d in (self.stage, self.home / "mail", self.home / ".config/hermes"):
            d.mkdir(parents=True)
        routes = self.home / ".config/hermes/routes.json"
        routes.write_text(json.dumps({"version": 1, "stages": {"home": "~/.local/share/hermes/stage"},
                                      "routes": {"servermail": {"to": "~/mail/", "stage": "home"}}}))
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "HERMES_CONFIG": str(routes),
                                             "JANUS_CONFIG": str(self.home / "no-janus"), "LEAFY_REALM": str(self.home / "no-realm"),
                                             "SSH_CONNECTION": "192.0.2.10 50000 192.0.2.1 22",
                                             "HERMES_TAILSCALE": str(ROOT / "tests" / "fixtures" / "tailscale")})
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, name, data, route="servermail"):
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=put_like_the_client(route, name, data))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = receive.receive("desktop", env=env, stdin=io.BytesIO(data), out=out)
        return code, out.getvalue(), err.getvalue()

    def arrival(self, out):
        folder = self.stage / "in" / out.split()[1]
        return (folder / "data").read_bytes(), json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    def test_all_256_byte_values_arrive_with_the_same_sha256(self):
        code, out, err = self.put("all256.bin", ALL256)
        self.assertEqual((code, err), (0, ""), err)
        data, meta = self.arrival(out)
        self.assertEqual(data, ALL256)
        self.assertEqual(meta["sha256"], hashlib.sha256(ALL256).hexdigest())
        self.assertEqual(meta["bytes"], 256)

    def test_a_bigger_file_streamed_in_chunks(self):
        data = os.urandom(3 * 1024 * 1024 + 7)      # not a multiple of the client's 1 MB chunk
        code, out, _ = self.put("big.bin", data)
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(self.arrival(out)[0]).hexdigest(), hashlib.sha256(data).hexdigest())

    def test_an_empty_file(self):
        code, out, _ = self.put("empty.txt", b"")
        self.assertEqual(code, 0)
        self.assertEqual(self.arrival(out)[0], b"")

    def test_names_with_spaces_parentheses_quotes_and_non_ascii_arrive_unchanged(self):
        for name in NAMES:
            with self.subTest(name):
                code, out, err = self.put(name, b"x")
                self.assertEqual((code, err), (0, ""), err)
                self.assertEqual(self.arrival(out)[1]["name"], unicodedata.normalize("NFC", name))

    def test_the_clients_base64url_is_what_the_server_decodes(self):
        for name in NAMES + ["??>>.bin", "???.bin"]:        # names whose base64 holds + and / (turned into - and _)
            self.assertEqual(receive.clean_name(b64url_like_the_client(name)), unicodedata.normalize("NFC", name))
        self.assertIn("+", base64.b64encode(b"??>>").decode())
        self.assertIn("/", base64.b64encode(b"???").decode())
        for name in ("??>>.bin", "???.bin"):
            self.assertRegex(b64url_like_the_client(name), r"^[A-Za-z0-9_-]+$")

    def test_a_refused_name_is_refused_and_nothing_is_written(self):
        code, out, err = self.put(".hidden.txt", b"x")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("starts with a dot", err)
        self.assertFalse((self.stage / "in").exists() and list((self.stage / "in").iterdir()))

    def test_the_arguments_match_the_clients_own_check(self):
        """hermes.ps1 refuses to compose a line unless it matches this pattern; the server must parse everything it allows."""
        pattern = re.search(r"notmatch '\^\((ping\|put [^']+)\)\$'", PS1)
        self.assertIsNotNone(pattern, "the token check in Get-SshArgs")
        client_re = re.compile("^(%s)$" % pattern.group(1))
        for name in NAMES:
            cmd = put_like_the_client("servermail", name, b"x")
            self.assertRegex(cmd, client_re)
            self.assertEqual(receive.parse(cmd)[0], "put")
        self.assertRegex("ping", client_re)
        self.assertNotRegex("put servermail abc 1 " + "0" * 63 + "g", client_re)
        self.assertNotRegex("put Servermail abc 1 " + "0" * 64, client_re)


class ScriptTest(unittest.TestCase):
    """What must be true of the scripts, read as text (and parsed, when a PowerShell is on this machine)."""
    def test_hashes_streams_and_never_pipes_into_ssh(self):
        self.assertIn("Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256", PS1)
        self.assertIn("StandardInput.BaseStream", PS1)
        self.assertIn("-T -o BatchMode=yes -o ConnectTimeout=15", PS1)
        code = "\n".join(l for l in PS1.splitlines() if "keys add" not in l)   # the printed keys-add line pipes text, on purpose
        self.assertNotRegex(code, r"\|\s*&?\s*\$?ssh\b", "a PowerShell pipeline into ssh would re-encode the bytes")
        self.assertNotRegex(code, r"\|\s*ssh(\.exe)?\b")
        self.assertIn("ConvertTo-Base64Url", PS1)
        self.assertIn("Replace('+', '-').Replace('/', '_')", PS1)
        # .NET Framework flushes the StandardInput writer's preamble at process start: a UTF-8 console with a BOM
        # put EF BB BF in front of every file (found in the first end-to-end run). The client must prevent it.
        self.assertIn("[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false", PS1)
        self.assertLess(PS1.index("UTF8Encoding $false"), PS1.index("[System.Diagnostics.Process]::Start($psi)"))

    def test_only_ok_id_counts_as_delivered(self):
        self.assertIn("$r.out -match '^ok (\\S+)$'", PS1)
        self.assertIn("not counted as delivered", PS1)

    def test_the_unreachable_message(self):
        self.assertIn("Is the server on? Is a VPN blocking Tailscale?", PS1)
        self.assertIn("refused the Hermes key", PS1)

    def test_literal_paths_everywhere(self):
        self.assertNotRegex(PS1, r"Get-Item -Path|Get-Content -Path|Move-Item -Path|Test-Path -Path",
                            "[brackets] in a name need -LiteralPath")

    def test_setup_writes_the_alias_and_the_keys_add_line(self):
        for s in ("IdentitiesOnly yes", "IdentityFile ~/.ssh/hermes_", "-t ed25519", "hermes keys add", "config.json"):
            self.assertIn(s, PS1)

    def test_collect_strips_the_prefix(self):
        self.assertIn("'hermes__*'", PS1)
        self.assertIn("$f.Name.Substring(8)", PS1)
        self.assertEqual(len("hermes__"), 8)
        self.assertIn("file get --conflict=rename", PS1)

    def test_the_torrent_types_and_routes(self):
        self.assertIn("@('movies', 'shows', 'audiobooks', 'ebooks', 'music', 'games')", PS1)
        self.assertIn("'torrent-' + $TorrentTypes", PS1)

    def test_magnets_are_named_like_hermes_message(self):
        self.assertIn("$hash.Substring(0, 8)", PS1)      # <hash8>.magnet, lower case, as send.py names them
        self.assertIn("xt=urn:btih:([A-Za-z0-9]{32,40})", PS1)

    def test_scripts_are_pure_ascii(self):
        """Windows PowerShell 5 reads a script without a BOM as ANSI: one non-ASCII character would come out as two wrong ones."""
        for script, text in (("hermes.ps1", PS1), ("install-sendto.ps1", SENDTO)):
            bad = sorted({c for c in text if ord(c) > 127})
            self.assertEqual(bad, [], "%s holds non-ASCII characters %r" % (script, bad))

    def test_sendto_entries(self):
        self.assertIn("$Dot = [string][char]0xB7", SENDTO)       # the middle dot, without a non-ASCII byte in the file
        for name, command in [("Mail", "send servermail"), ("Torrent", "torrent"), ("Magnet", "magnet"),
                              ("Lesson", "send lesson"), ("ROMs", "send roms"), ("Collect", "collect")]:
            self.assertIn('"Hermes $Dot %s"' % name, SENDTO)
            self.assertIn("'%s'" % command, SENDTO)
        self.assertIn('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Window {1}', SENDTO)
        self.assertIn("hermes.ico", SENDTO)
        self.assertTrue((CLIENT / "hermes.png").is_file())
        self.assertIn("clients/windows/hermes.ico", (ROOT / ".gitignore").read_text())
        self.assertIn(".set-aside-", SENDTO)              # -Remove sets aside, never deletes

    def test_no_real_names_in_the_client(self):
        for text in (PS1, SENDTO, DOC):
            self.assertNotRegex(text, r"[A-Za-z]:\\Users\\(?!you\b)[A-Za-z]")
            self.assertNotIn(".ts.net", text)

    def test_the_doc_has_the_checklist(self):
        for item in ("256 byte values", "2 GB", "Is the server on? Is a VPN blocking Tailscale?", "hermes keys add", "Send To",
                     "collect", "Tailscale SSH", "Last run"):
            self.assertIn(item, DOC)

    def test_scripts_parse(self):
        """When a PowerShell is on this machine (Windows, or pwsh on Linux), both scripts must parse without errors."""
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("no PowerShell here; CI checks the scripts as text only")
        check = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$e) | Out-Null; "
                 "$e | ForEach-Object { $_.Message }; exit $e.Count")
        for script in ("hermes.ps1", "install-sendto.ps1"):
            r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check % str(CLIENT / script).replace("'", "''")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, script + ": " + r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
