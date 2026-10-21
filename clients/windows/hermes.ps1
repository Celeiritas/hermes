#Requires -Version 5
<#
hermes.ps1 - Hermes on a Windows PC: send files to the server's door, collect what the server sent, set the key up.
Windows PowerShell 5 only, no Python on the PC. docs/windows-client.md explains it; docs/design.md section 3 is the spec.

  hermes.ps1 setup -Server <name> -User <user> [-Device <name>] [-Port <n>] [-Mail <folder>]
  hermes.ps1 ping                        does the server answer the Hermes key?
  hermes.ps1 send <route> <file>...      e.g. send servermail report.pdf
  hermes.ps1 torrent <file>...           asks the type, then sends on torrent or torrent-<type>
  hermes.ps1 magnet [<file>...]          magnet links on the clipboard, or inside .txt .md .html .csv .docx files
  hermes.ps1 collect                     hermes__* files from Downloads (and waiting Taildrops) into the mail folder

Switches: -Window (the script runs in its own window, as from Send To: it pauses before closing),
          -Server, -Port, -User, -Device, -Mail (override the setup for one run),
          -Type 1-6 and -Pick all|1,3 (answer the torrent and magnet questions ahead of time).

How a send works: per file, Get-FileHash SHA256, the name as base64url, then
  ssh -T hermes-<server> put <route> <name-b64url> <bytes> <sha256>
with the file's bytes streamed to ssh's standard input through System.Diagnostics.Process (binary-safe; never a
PowerShell pipeline, which re-encodes bytes). Every ssh argument is a route id, base64url, digits or hex, so
PowerShell 5's quote stripping can't break it. Only "ok <id>" from the server counts as delivered to the door.
Exit code: 0 when everything went through, 1 otherwise.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = '',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @(),
    [string]$Server, [string]$User, [string]$Device, [int]$Port, [string]$Mail,
    [string]$Type, [string]$Pick, [switch]$Window
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$ConfigDir = Join-Path $env:APPDATA 'Hermes'
$ConfigFile = Join-Path $ConfigDir 'config.json'
$SshDir = Join-Path $HOME '.ssh'
$DeviceRe = '^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$'      # the same rule as hermes receive --key
$RouteRe = '^[a-z][a-z0-9-]{0,31}$'                 # the same rule as routes.json
$UserRe = '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$'
$TorrentTypes = @('movies', 'shows', 'audiobooks', 'ebooks', 'music', 'games')
$TorrentMenu = '1 Movie  2 Show  3 Audiobook  4 Ebook  5 Music  6 Game  (Enter = not sure, no type)'
$Chunk = 1048576
$AnswerTimeout = 600000                             # ms to wait for the server's answer after the last byte

function Say($text, $color) { if ($color) { Write-Host $text -ForegroundColor $color } else { Write-Host $text } }
function Bad($text) { Write-Host $text -ForegroundColor Red }

function Get-Version {
    $v = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'VERSION'
    if (Test-Path -LiteralPath $v) { return (Get-Content -LiteralPath $v -Raw).Trim() }
    return 'unknown'
}

function Find-Exe($name, $fallback) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    if ($fallback -and (Test-Path -LiteralPath $fallback)) { return $fallback }
    return $null
}
$OpenSsh = Join-Path $env:SystemRoot 'System32\OpenSSH'
$Ssh = Find-Exe 'ssh.exe' (Join-Path $OpenSsh 'ssh.exe')
$SshKeygen = Find-Exe 'ssh-keygen.exe' (Join-Path $OpenSsh 'ssh-keygen.exe')
$Tailscale = Find-Exe 'tailscale.exe' (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe')

function ConvertTo-Base64Url([string]$text) {
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Read-Config {
    # The setup from %APPDATA%\Hermes\config.json, with this run's -Server/-User/-Port/-Device/-Mail on top.
    $cfg = @{ server = ''; user = ''; port = 22; device = ''; mail = (Join-Path $HOME 'Hermes-Mail') }
    if (Test-Path -LiteralPath $ConfigFile) {
        $j = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in 'server', 'user', 'port', 'device', 'mail') {
            if ($j.PSObject.Properties[$k] -and $j.$k) { $cfg[$k] = $j.$k }
        }
    }
    if ($Server) { $cfg.server = $Server }
    if ($User) { $cfg.user = $User }
    if ($Port) { $cfg.port = $Port }
    if ($Device) { $cfg.device = $Device }
    if ($Mail) { $cfg.mail = $Mail }
    if (-not $cfg.device) { $cfg.device = $env:COMPUTERNAME.ToLower() }
    $cfg.host = 'hermes-' + $cfg.server
    return $cfg
}

function Test-Setup($cfg) {
    if (-not $Ssh) { Bad "Windows' OpenSSH client is missing (Settings > Apps > Optional features > OpenSSH Client)."; return $false }
    if (-not $cfg.server -or $cfg.server -notmatch $DeviceRe) {
        Bad "No server set up yet. Run:  hermes.ps1 setup -Server <name> -User <user>   (or pass -Server for this run)."
        return $false
    }
    return $true
}

function Invoke-Native($exe, $arguments, $inputFile, $label) {
    # Runs exe with the argument string exactly as given (no PowerShell re-quoting) and streams inputFile, byte for byte,
    # to its standard input. Returns @{ code; out; err; sent }. A progress bar shows for files over 8 MB.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    # .NET Framework builds the child's StandardInput writer from the console's input encoding and flushes it at once,
    # so a UTF-8 console with a byte-order mark would put 3 bytes (EF BB BF) in front of the file. A BOM-less UTF-8
    # while the process starts keeps the stream exactly the file's bytes; the writer itself is never used.
    $inputEncoding = $null
    try { $inputEncoding = [Console]::InputEncoding; [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
    try { $p = [System.Diagnostics.Process]::Start($psi) }
    finally { if ($inputEncoding) { try { [Console]::InputEncoding = $inputEncoding } catch {} } }
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    $stdin = $p.StandardInput.BaseStream
    $sent = [long]0
    try {
        if ($inputFile) {
            $total = [long](Get-Item -LiteralPath $inputFile).Length
            $show = $total -gt 8MB
            $fs = [System.IO.File]::Open($inputFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            try {
                $buf = New-Object byte[] $Chunk
                $watch = [Diagnostics.Stopwatch]::StartNew()
                $shown = -1000
                while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
                    try { $stdin.Write($buf, 0, $n) } catch { break }     # the far side closed the pipe; ssh says why
                    $sent += $n
                    if ($show -and ($watch.ElapsedMilliseconds - $shown) -ge 250) {
                        $shown = $watch.ElapsedMilliseconds
                        $secs = $watch.Elapsed.TotalSeconds
                        $rate = 0
                        if ($secs -gt 0) { $rate = $sent / 1MB / $secs }
                        Write-Progress -Activity ("Hermes: sending {0}" -f $label) -PercentComplete ([int](100 * $sent / $total)) `
                            -Status ("{0:N0} of {1:N0} MB, {2:N1} MB/s" -f ($sent / 1MB), ($total / 1MB), $rate)
                    }
                }
                if ($show) { Write-Progress -Activity ("Hermes: sending {0}" -f $label) -Completed }
            } finally { $fs.Dispose() }
        }
    } finally { try { $stdin.Close() } catch {} }
    if (-not $p.WaitForExit($AnswerTimeout)) {
        try { $p.Kill() } catch {}
        return @{ code = -1; out = ''; err = ("no answer from the server within {0} minutes" -f ($AnswerTimeout / 60000)); sent = $sent }
    }
    $p.WaitForExit()      # lets the async readers drain
    return @{ code = $p.ExitCode; out = $outTask.Result.Trim(); err = $errTask.Result.Trim(); sent = $sent }
}

function Get-SshArgs($cfg, $command) {
    # Every token is checked before it goes near ssh: the alias, then route id, base64url, digits and hex only.
    if ($cfg.host -notmatch '^hermes-[A-Za-z0-9][A-Za-z0-9.-]*$') { throw ("bad server name {0}" -f $cfg.server) }
    if ($command -notmatch '^(ping|put [a-z][a-z0-9-]{0,31} [A-Za-z0-9_-]+ [0-9]{1,15} [0-9a-f]{64})$') { throw ("bad command {0}" -f $command) }
    return ("-T -o BatchMode=yes -o ConnectTimeout=15 {0} {1}" -f $cfg.host, $command)
}

function Explain-Failure($cfg, $r, $what) {
    # One clear line about why a put or ping did not go through.
    $why = $r.err -replace '(?m)^hermes: ', ''
    switch ($r.code) {
        255 {
            if ($why -match 'Permission denied') {
                Bad ("  {0}: {1} refused the Hermes key. Run the 'hermes keys add' line that hermes.ps1 setup printed (run setup again to see it)." -f $what, $cfg.server)
            } elseif ($why -match 'Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED') {
                Bad ("  {0}: ssh does not trust {1}'s host key. Check with the owner, then fix ~/.ssh/known_hosts. ({2})" -f $what, $cfg.server, $why)
            } else {
                Bad ("  {0}: could not reach {1}. Is the server on? Is a VPN blocking Tailscale? (ssh: {2})" -f $what, $cfg.server, $why)
            }
        }
        1 { Bad ("  {0}: the transfer failed, nothing was delivered. {1}" -f $what, $why) }
        2 { Bad ("  {0}: refused by {1}. {2}" -f $what, $cfg.server, $why) }
        3 { Bad ("  {0}: {1} has a problem of its own; tell the owner. {2}" -f $what, $cfg.server, $why) }
        default { Bad ("  {0}: not delivered (ssh exit {1}). {2}" -f $what, $r.code, $why) }
    }
}

function Send-One($cfg, $route, $path) {
    # One file to the door. Returns $true only when the server answered "ok <id>".
    try { $item = Get-Item -LiteralPath $path -ErrorAction Stop } catch { Bad ("  {0}: not found" -f $path); return $false }
    if ($item.PSIsContainer) { Bad ("  {0}: is a folder; Hermes sends files" -f $item.Name); return $false }
    $name = $item.Name
    $size = [long]$item.Length
    try { $sha = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLower() }
    catch { Bad ("  {0}: cannot read it ({1})" -f $name, $_.Exception.Message); return $false }
    $args = Get-SshArgs $cfg ("put {0} {1} {2} {3}" -f $route, (ConvertTo-Base64Url $name), $size, $sha)
    $r = Invoke-Native $Ssh $args $item.FullName $name
    if ($r.code -eq 0 -and $r.out -match '^ok (\S+)$') {
        Say ("  {0}: at the door of {1} on route {2} ({3:N1} MB, arrival {4})" -f $name, $cfg.server, $route, ($size / 1MB), $Matches[1]) Green
        return $true
    }
    if ($r.code -eq 0) { Bad ("  {0}: the server answered '{1}' instead of ok <id>; not counted as delivered" -f $name, $r.out); return $false }
    Explain-Failure $cfg $r $name
    return $false
}

function Send-Files($cfg, $route, $files) {
    if ($route -notmatch $RouteRe) { Bad ("'{0}' is not a route id (lowercase letters, digits and dashes, e.g. servermail)." -f $route); return $false }
    if (-not $files -or $files.Count -eq 0) { Bad "Nothing to send. Give one or more files, or use Send To."; return $false }
    Say ("Hermes: {0} file(s) to {1}, route {2}" -f $files.Count, $cfg.server, $route)
    $ok = 0
    foreach ($f in $files) { if (Send-One $cfg $route $f) { $ok++ } }
    if ($ok -eq $files.Count) { Say ("Hermes handed {0} file(s) to {1}'s door. Janus checks them next." -f $ok, $cfg.server) Green }
    else { Bad ("{0} of {1} delivered to the door; the rest did not go." -f $ok, $files.Count) }
    return ($ok -eq $files.Count)
}

function Read-TorrentRoute {
    # The type question (or -Type): torrent, or torrent-<type>.
    $t = $Type
    if (-not $t) { Say $TorrentMenu; $t = Read-Host 'Type' }
    if ($t -match '^[1-6]$') { return 'torrent-' + $TorrentTypes[[int]$t - 1] }
    if ($t -and $t -notmatch '^\s*$') { Bad ("'{0}' is not 1-6; sending with no type." -f $t) }
    return 'torrent'
}

function Get-TextOf($path) {
    # The text inside a file, for finding magnet links; .docx is unzipped in memory (text and the hyperlink list).
    if ($path -match '\.docx$') {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($path)
        try {
            $x = ''
            foreach ($entry in 'word/document.xml', 'word/_rels/document.xml.rels') {
                $e = $zip.GetEntry($entry)
                if ($e) { $r = New-Object IO.StreamReader($e.Open()); $x += $r.ReadToEnd() + "`n"; $r.Close() }
            }
            return ($x -replace '</w:p>|<w:br/>|<w:tab/>|"', "`n" -replace '<[^>]+>', '' -replace '&amp;', '&')
        } finally { $zip.Dispose() }
    }
    return ((Get-Content -LiteralPath $path -Raw -Encoding UTF8) -replace '&amp;', '&')
}

function Find-Magnets($text) {
    # Every distinct magnet link (by info-hash) in the text: ordered hash -> @{ Link; Name; Hash8 }.
    $found = [ordered]@{}
    foreach ($m in [regex]::Matches([string]$text, 'magnet:\?(?:(?!magnet:)[^\s"''<>])+')) {
        $link = $m.Value.TrimEnd('.', ',', ';', ')', ']')
        if ($link -match 'xt=urn:btih:([A-Za-z0-9]{32,40})') {
            $hash = $Matches[1].ToLower()
            if (-not $found.Contains($hash)) {
                $name = '(no name in the link)'
                if ($link -match '[?&]dn=([^&]+)') { $name = [Uri]::UnescapeDataString(([string]$Matches[1]).Replace('+', ' ')) }
                $found[$hash] = @{ Link = $link; Name = $name; Hash8 = $hash.Substring(0, 8) }
            }
        }
    }
    return $found
}

function Send-Magnets($cfg, $files) {
    if ($files -and $files.Count -gt 0) {
        $text = ($files | ForEach-Object { Get-TextOf $_ }) -join "`n"
        $from = "{0} file(s)" -f $files.Count
    } else {
        $text = Get-Clipboard -Raw
        $from = 'the clipboard'
    }
    $found = Find-Magnets $text
    if ($found.Count -eq 0) { Bad ("No magnet links found in {0}." -f $from); return $false }
    Say ("`nFound {0} magnet link(s) in {1}:`n" -f $found.Count, $from)
    $keys = @($found.Keys)
    for ($i = 0; $i -lt $keys.Count; $i++) {
        Say ("  [{0}] {1}" -f ($i + 1), $found[$keys[$i]].Name)
        Say ("      {0}" -f $keys[$i]) Yellow
    }
    Say ''
    $ans = $Pick
    if (-not $ans) {
        if ($keys.Count -eq 1) { $ans = Read-Host 'Hash matches the site? Send it (y/n)' }
        else { $ans = Read-Host 'Send which? a = all, numbers like 1,3,5, n = cancel' }
    }
    $chosen = @()
    if ($ans -match '^(a|all|y|yes)$') { $chosen = $keys }
    elseif ($ans -match '^[0-9 ,]+$') {
        foreach ($n in ($ans -split '[ ,]+')) { if ($n -match '^[0-9]+$' -and [int]$n -ge 1 -and [int]$n -le $keys.Count) { $chosen += $keys[[int]$n - 1] } }
    }
    if ($chosen.Count -eq 0) { Say 'Nothing sent.'; return $false }
    $route = Read-TorrentRoute
    # Each magnet becomes a <hash8>.magnet file (the same name hermes message gives it on the server), sent like any file.
    $tmp = Join-Path $env:TEMP ('hermes-magnet-' + [IO.Path]::GetRandomFileName())
    $paths = @()
    try {
        foreach ($h in $chosen) {
            $dir = Join-Path $tmp $h
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            $file = Join-Path $dir ($found[$h].Hash8 + '.magnet')
            [IO.File]::WriteAllText($file, $found[$h].Link + "`n", (New-Object Text.UTF8Encoding $false))
            $paths += $file
        }
        return (Send-Files $cfg $route $paths)
    } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue } }
}

function Get-DownloadsFolder {
    try {
        $k = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
        $v = $k.'{374DE290-123F-4565-9164-39C4925E467B}'
        if ($v) { return [Environment]::ExpandEnvironmentVariables($v) }
    } catch {}
    return (Join-Path $HOME 'Downloads')
}

function Move-Free($src, $dir, $name) {
    # Move src into dir as name; a file already there keeps its place and the new one gets " (2)", " (3)" ...
    $base = [IO.Path]::GetFileNameWithoutExtension($name)
    $ext = [IO.Path]::GetExtension($name)
    $target = Join-Path $dir $name
    $n = 2
    while (Test-Path -LiteralPath $target) { $target = Join-Path $dir ("{0} ({1}){2}" -f $base, $n, $ext); $n++ }
    Move-Item -LiteralPath $src -Destination $target
    return $target
}

function Collect-Mail($cfg) {
    # hermes__* files from Downloads (the Tailscale app saves Taildrops there) and anything still waiting in
    # tailscale file get, into the mail folder without the prefix. Says how many arrived and opens the folder.
    $mail = $cfg.mail
    New-Item -ItemType Directory -Force -Path $mail | Out-Null
    if ($Tailscale) {
        $r = Invoke-Native $Tailscale ('file get --conflict=rename "{0}"' -f $mail) $null $null
        if ($r.code -ne 0 -and $r.err) { Say ("  (tailscale file get: {0})" -f $r.err) Yellow }
    }
    $n = 0
    foreach ($f in @(Get-ChildItem -LiteralPath (Get-DownloadsFolder) -File -Filter 'hermes__*' -ErrorAction SilentlyContinue)) {
        $to = Move-Free $f.FullName $mail $f.Name.Substring(8)
        Say ("  {0}" -f (Split-Path $to -Leaf)); $n++
    }
    foreach ($f in @(Get-ChildItem -LiteralPath $mail -File -Filter 'hermes__*' -ErrorAction SilentlyContinue)) {
        $to = Move-Free $f.FullName $mail $f.Name.Substring(8)
        Say ("  {0}" -f (Split-Path $to -Leaf)); $n++
    }
    if ($n -gt 0) {
        Say ("Hermes: {0} file(s) arrived from {1} in {2}" -f $n, $cfg.server, $mail) Green
        Start-Process explorer.exe ('"{0}"' -f $mail)
    } else { Say ("Hermes: no mail waiting from {0}." -f $cfg.server) }
    return $true
}

function Setup-Client($cfg) {
    # The Hermes-only key, the ssh alias hermes-<server>, the config file, and the one line that installs the key.
    if (-not $Ssh -or -not $SshKeygen) { Bad "Windows' OpenSSH client is missing (Settings > Apps > Optional features > OpenSSH Client)."; return $false }
    if (-not $cfg.server -or $cfg.server -notmatch $DeviceRe) { Bad "setup needs -Server <name>: the server's name as ssh reaches it over Tailscale."; return $false }
    if (-not $cfg.user -or $cfg.user -notmatch $UserRe) { Bad "setup needs -User <user>: your user name on the server."; return $false }
    if ($cfg.device -notmatch $DeviceRe) { Bad ("-Device '{0}': letters, digits, dots and dashes only (63 at most)." -f $cfg.device); return $false }
    if ($cfg.port -lt 1 -or $cfg.port -gt 65535) { Bad "-Port must be 1-65535."; return $false }
    New-Item -ItemType Directory -Force -Path $SshDir | Out-Null
    $key = Join-Path $SshDir ('hermes_' + $cfg.device)
    $pub = $key + '.pub'
    if (Test-Path -LiteralPath $key) { Say ("Key: {0} is already there (kept)." -f $key) }
    else {
        $r = Invoke-Native $SshKeygen ('-q -t ed25519 -f "{0}" -C hermes@{1} -N ""' -f $key, $cfg.device) $null $null
        if ($r.code -ne 0 -or -not (Test-Path -LiteralPath $pub)) { Bad ("ssh-keygen failed: {0}" -f $r.err); return $false }
        Say ("Key: made {0} (ed25519, Hermes only)." -f $key) Green
    }
    $sshConfig = Join-Path $SshDir 'config'
    $text = ''
    if (Test-Path -LiteralPath $sshConfig) { $text = [IO.File]::ReadAllText($sshConfig) }
    if ($text -match ('(?mi)^\s*Host\s+' + [regex]::Escape($cfg.host) + '\s*$')) { Say ("ssh config: Host {0} is already there (kept)." -f $cfg.host) }
    else {
        $nl = "`n"
        if ($text -match "`r`n") { $nl = "`r`n" }
        $block = New-Object System.Collections.Generic.List[string]
        $block.Add("Host " + $cfg.host)
        $block.Add("    HostName " + $cfg.server)
        if ($cfg.port -ne 22) { $block.Add("    Port " + $cfg.port) }
        $block.Add("    User " + $cfg.user)
        $block.Add("    IdentityFile ~/.ssh/hermes_" + $cfg.device)
        $block.Add("    IdentitiesOnly yes")
        $block.Add("    StrictHostKeyChecking accept-new")
        if ($text -and -not $text.EndsWith("`n")) { $text += $nl }
        if ($text) { $text += $nl }
        $text += ($block -join $nl) + $nl
        [IO.File]::WriteAllText($sshConfig, $text, (New-Object Text.UTF8Encoding $false))
        Say ("ssh config: added Host {0} -> {1}@{2}{3} with that key only." -f $cfg.host, $cfg.user, $cfg.server, $(if ($cfg.port -ne 22) { ' port ' + $cfg.port } else { '' })) Green
    }
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    $doc = @{ server = $cfg.server; user = $cfg.user; port = $cfg.port; device = $cfg.device; mail = $cfg.mail }
    [IO.File]::WriteAllText($ConfigFile, ($doc | ConvertTo-Json) + "`n", (New-Object Text.UTF8Encoding $false))
    New-Item -ItemType Directory -Force -Path $cfg.mail | Out-Null
    Say ("Saved {0}; mail from {1} is collected into {2}." -f $ConfigFile, $cfg.server, $cfg.mail)
    Say ''
    Say "Now install the public key on the server (one line, pasteable; it asks for your usual login there):" Cyan
    Say ("  Get-Content ""{0}"" | ssh {1}@{2} 'hermes keys add {3}'" -f $pub, $cfg.user, $cfg.server, $cfg.device) White
    Say "Then check:  hermes.ps1 ping" Cyan
    return $true
}

function Ping-Server($cfg) {
    $r = Invoke-Native $Ssh (Get-SshArgs $cfg 'ping') $null $null
    if ($r.code -eq 0 -and $r.out -match '^hermes ok (\S+)$') { Say ("{0} answers: hermes {1} is at the door." -f $cfg.server, $Matches[1]) Green; return $true }
    if ($r.code -eq 0) { Bad ("{0} answered '{1}', not 'hermes ok <version>'. Is port {2} the server's OpenSSH door?" -f $cfg.server, $r.out, $cfg.port); return $false }
    Explain-Failure $cfg $r 'ping'
    return $false
}

function Finish($ok) {
    if ($Window) { if ($ok) { Start-Sleep 3 } else { Read-Host 'Press Enter to close' | Out-Null } }
    if ($ok) { exit 0 } else { exit 1 }
}

$cfg = Read-Config
$ok = $false
try {
    switch ($Command.ToLower()) {
        'setup' { $ok = Setup-Client $cfg }
        'ping' { if (Test-Setup $cfg) { $ok = Ping-Server $cfg } }
        'send' {
            if (Test-Setup $cfg) {
                if ($Rest.Count -lt 1) { Bad "send needs a route and files:  hermes.ps1 send servermail <file>..." }
                else { $ok = Send-Files $cfg $Rest[0] @($Rest | Select-Object -Skip 1) }
            }
        }
        'torrent' {
            if (Test-Setup $cfg) {
                if ($Rest.Count -lt 1) { Bad "torrent needs .torrent files (use Send To on them)." }
                else {
                    foreach ($f in $Rest) { Say ("  {0}" -f (Split-Path $f -Leaf)) }
                    $ok = Send-Files $cfg (Read-TorrentRoute) $Rest
                }
            }
        }
        'magnet' { if (Test-Setup $cfg) { $ok = Send-Magnets $cfg $Rest } }
        'collect' { $ok = Collect-Mail $cfg }
        'version' { Say ("hermes.ps1 {0}" -f (Get-Version)); $ok = $true }
        default {
            Say ("hermes.ps1 {0}: setup | ping | send <route> <file>... | torrent <file>... | magnet [<file>...] | collect" -f (Get-Version))
            Say "See docs/windows-client.md. Send To entries: install-sendto.ps1"
            $ok = $Command -eq ''
        }
    }
} catch {
    Bad ("Hermes stopped: {0}" -f $_.Exception.Message)
    $ok = $false
}
Finish $ok
