#Requires -Version 5
<#
hermes.ps1 - Hermes on a Windows PC: send files to the server's door, collect what the server sent, set the key up.
Windows PowerShell 5 only, no Python on the PC. docs/windows-client.md explains it; docs/design.md section 3 is the spec.

  hermes.ps1 setup -Server <name> -User <user> [-Alias <ssh alias>] [-Port <n>] [-Device <name>] [-Mail <folder>]
  hermes.ps1 ping                        does the server answer the Hermes key?
  hermes.ps1 send <route> <file>...      e.g. send servermail report.pdf
  hermes.ps1 torrent <file>...           asks the type, then sends on torrent or torrent-<type>
  hermes.ps1 magnet [<file>...]          magnet links on the clipboard, or inside .txt .md .html .csv .docx files
  hermes.ps1 collect                     hermes__* files from Downloads (and waiting Taildrops) into the mail folder

Switches: -Window (the script runs in its own window, as from Send To: it pauses before closing),
          -Server, -Device, -Mail (override the setup for one run),
          -Alias, -Port, -User (setup only: they go into the ssh alias hermes-<server>, where ssh reads them),
          -Type 1-6 and -Pick all|1,3 (answer the torrent and magnet questions ahead of time).

Where the server's OpenSSH listens is never assumed: it may be any port, on the server's Tailscale address only.
setup copies HostName and Port from the ssh alias you already use for the server (-Alias <name>, or the Host block
in ~/.ssh/config that is called like the server: that name or HostName exactly, or else the one block whose HostName is
the server plus a dot and a domain; with two or more such blocks setup picks none and asks for -Alias). -Port wins over
it, and without either setup asks. With no port at all setup stops (exit 1): it never falls back to 22. Only -Port 0
(or the answer 0) means "ssh's own default" on purpose, and then there is no Port line.

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
    [string]$Server, [string]$User, [string]$Device, [int]$Port, [string]$Mail, [string]$Alias,
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
$AliasRe = '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'      # an ssh alias setup copies from: a plain name, never a pattern
$PortGiven = $PSBoundParameters.ContainsKey('Port')  # -Port 0 given on purpose is not the same as no -Port
$HostNameRe = '^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$' # a HostName setup copies: a name or an address, nothing else
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
    # The setup from %APPDATA%\Hermes\config.json, with this run's -Server/-User/-Port/-Alias/-Device/-Mail on top.
    # port 0 means "ssh's own default" (asked for with -Port 0): the alias hermes-<server> then has no Port line. A send never reads port, user
    # or alias from here; ssh takes them from the alias. They are kept as a record of what setup wrote.
    $cfg = @{ server = ''; user = ''; port = 0; alias = ''; device = ''; mail = (Join-Path $HOME 'Hermes-Mail') }
    if (Test-Path -LiteralPath $ConfigFile) {
        $j = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in 'server', 'user', 'port', 'alias', 'device', 'mail') {
            if ($j.PSObject.Properties[$k] -and $j.$k) { $cfg[$k] = $j.$k }
        }
    }
    if ($Server) { $cfg.server = $Server }
    if ($User) { $cfg.user = $User }
    if ($Port) { $cfg.port = $Port }
    if ($Alias) { $cfg.alias = $Alias }
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

function Read-SshHosts([string]$text) {
    # The Host blocks of an ssh config, in order, each as @{ names = <the words of its Host line>; opts = @{ keyword = value } }.
    # Read as text, the way ssh_config(5) writes it: keywords in any case, "Key Value" or "Key=Value", # comments, and
    # the first value of a keyword counts. A Match line ends the block before it. Include files are not opened.
    $blocks = New-Object System.Collections.Generic.List[object]
    $cur = $null
    foreach ($raw in ($text -split "`r?`n")) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z][A-Za-z0-9]*)(?:\s*=\s*|\s+)(.*)$') { continue }
        $key = $Matches[1].ToLower()
        $value = $Matches[2].Trim()
        if ($key -eq 'host') {
            $cur = @{ names = @($value -split '\s+' | ForEach-Object { $_.Trim('"') } | Where-Object { $_ }); opts = @{} }
            $blocks.Add($cur)
        }
        elseif ($key -eq 'match') { $cur = $null }
        elseif ($cur -and -not $cur.opts.ContainsKey($key)) { $cur.opts[$key] = $value.Trim('"') }
    }
    return , $blocks
}

function Find-SshAlias($blocks, [string]$name, [string]$server) {
    # One ssh alias out of Read-SshHosts' blocks: @{ name = <as the config spells it>; opts = <its settings> }, or $null.
    # Only plain names count. A pattern (*, test*, !x) is ssh's own business: ssh applies it to hermes-<server> by
    # itself or not at all, and copying from it would freeze somebody's default into the alias.
    # With a name: every block that lists it, the first value of each keyword, as ssh reads them.
    # Without: among the blocks that are not Hermes' own, an exact match wins: first a block called like the server,
    # then one whose HostName is the server. Only when there is none does "the server's name plus a dot and a domain"
    # count, and then only when exactly one block matches that way; with two or more the answer is
    # @{ ambiguous = <the candidates> } and nothing is picked, because another machine's block must never be taken.
    if (-not $name) {
        $byName = $null
        $byHost = $null
        $prefix = @()
        foreach ($b in $blocks) {
            $plain = @($b.names | Where-Object { $_ -notmatch '[*?!]' })
            if ($plain.Count -eq 0 -or @($plain | Where-Object { $_ -like 'hermes-*' }).Count -gt 0) { continue }
            $same = @($plain | Where-Object { $_ -eq $server })
            if ($same.Count -gt 0) { if (-not $byName) { $byName = $same[0] }; continue }
            $hn = [string]$b.opts['hostname']
            if (-not $hn) { continue }
            if ($hn -eq $server) { if (-not $byHost) { $byHost = $plain[0] } }
            elseif ($hn.StartsWith($server + '.', [StringComparison]::OrdinalIgnoreCase)) {
                $prefix += , @{ name = $plain[0]; hostname = $hn; port = [string]$b.opts['port'] }
            }
        }
        if ($byName) { $name = $byName }
        elseif ($byHost) { $name = $byHost }
        elseif ($prefix.Count -eq 1) { $name = $prefix[0].name }
        elseif ($prefix.Count -gt 1) { return @{ ambiguous = $prefix } }
        else { return $null }
    }
    $opts = @{}
    $spelled = $null
    foreach ($b in $blocks) {
        $hit = @($b.names | Where-Object { $_ -notmatch '[*?!]' -and $_ -eq $name })
        if ($hit.Count -eq 0) { continue }
        if (-not $spelled) { $spelled = $hit[0] }
        foreach ($k in @($b.opts.Keys)) { if (-not $opts.ContainsKey($k)) { $opts[$k] = $b.opts[$k] } }
    }
    if (-not $spelled) { return $null }
    return @{ name = $spelled; opts = $opts }
}

function Get-PortOf($opts) {
    # The Port of an ssh config block as a number; 0 when it has none, or one that is not a port number.
    $p = [string]$opts['port']
    if ($p -match '^\d{1,5}$' -and [int]$p -ge 1 -and [int]$p -le 65535) { return [int]$p }
    return 0
}

function Read-Port {
    # Asks which port the server's OpenSSH listens on. -1 = no answer (setup then stops); 0 = typed on purpose: no Port
    # line is written and ssh uses its own default.
    Say "Which port does the server's OpenSSH listen on? There is no ssh alias for the server to copy it from."
    Say "  (A number, or 0 for ssh's own default. Enter alone is no answer. -Port <n> or -Alias <name> answers this ahead of time.)"
    $a = ''
    try { $a = [string](Read-Host 'Port') } catch { $a = '' }      # nobody to ask (a script, -NonInteractive)
    $a = $a.Trim()
    if ($a -match '^\d{1,5}$' -and [int]$a -ge 0 -and [int]$a -le 65535) { return [int]$a }
    if ($a) { Bad ("'{0}' is not a port (1-65535, or 0 for ssh's own default): no port set." -f $a) }
    return -1
}

function Setup-Client($cfg) {
    # The Hermes-only key, the ssh alias hermes-<server>, the config file, and the one line that installs the key.
    # The alias gets its address and its port from the ssh alias the user already has for the server, from -Port, or
    # from the user's answer: the server's OpenSSH may listen on any port, on its Tailscale address only, so setup
    # never writes a port that nobody named.
    if (-not $Ssh -or -not $SshKeygen) { Bad "Windows' OpenSSH client is missing (Settings > Apps > Optional features > OpenSSH Client)."; return $false }
    if (-not $cfg.server -or $cfg.server -notmatch $DeviceRe) { Bad "setup needs -Server <name>: the server's name as ssh reaches it over Tailscale."; return $false }
    if (-not $cfg.user -or $cfg.user -notmatch $UserRe) { Bad "setup needs -User <user>: your user name on the server."; return $false }
    if ($cfg.device -notmatch $DeviceRe) { Bad ("-Device '{0}': letters, digits, dots and dashes only (63 at most)." -f $cfg.device); return $false }
    if ($Port -lt 0 -or $Port -gt 65535) { Bad "-Port must be 1-65535."; return $false }
    $sshConfig = Join-Path $SshDir 'config'
    if ($Alias -and ($Alias -notmatch $AliasRe -or $Alias -eq $cfg.host)) {
        Bad ("-Alias '{0}': give the name of the ssh alias you already use for the server (a Host line in {1}), not a pattern and not Hermes' own {2}." -f $Alias, $sshConfig, $cfg.host)
        return $false
    }
    $text = ''
    if (Test-Path -LiteralPath $sshConfig) { $text = [IO.File]::ReadAllText($sshConfig) }
    $blocks = Read-SshHosts $text
    $mine = $null                                    # the user's own alias for the server, when there is one
    if ($Alias) {
        $mine = Find-SshAlias $blocks $Alias $cfg.server
        if (-not $mine) { Bad ("-Alias {0}: there is no 'Host {0}' in {1}. Nothing was changed." -f $Alias, $sshConfig); return $false }
    } else {
        if ($cfg.alias -and $cfg.alias -ne $cfg.host) { $mine = Find-SshAlias $blocks $cfg.alias $cfg.server }
        if (-not $mine) { $mine = Find-SshAlias $blocks '' $cfg.server }
        if ($mine -and $mine.ContainsKey('ambiguous')) {
            Bad ("ssh config: more than one Host block has a HostName that starts with '{0}.' and none is called {0}, so setup picks none (it could be another machine's):" -f $cfg.server)
            foreach ($c in $mine.ambiguous) {
                $cp = 'no Port line'
                if ($c.port) { $cp = 'Port ' + $c.port }
                Say ("  Host {0} (HostName {1}, {2})" -f $c.name, $c.hostname, $cp)
            }
            Say "Name the one that reaches the server with -Alias <name>. Nothing was changed."
            return $false
        }
    }
    $hostName = $cfg.server
    $hostFrom = '-Server'
    $doorPort = 0
    $portFrom = ''
    if ($mine) {
        $hostName = $mine.name
        $hostFrom = 'Host ' + $mine.name
        $hn = ([string]$mine.opts['hostname'] -split '\s+')[0]
        if ($hn -and $hn -match $HostNameRe) { $hostName = $hn }
        elseif ($hn) {
            $hostName = $cfg.server
            $hostFrom = '-Server'
            Say ("ssh config: Host {0} has HostName {1}, which setup does not copy (not a plain name or address); using {2}." -f $mine.name, $hn, $cfg.server) Yellow
        }
        $doorPort = Get-PortOf $mine.opts
        $shownHost = $hn
        if (-not $shownHost) { $shownHost = 'none' }
        $shownPort = 'no Port line'
        if ($doorPort) { $shownPort = 'Port ' + $doorPort }
        Say ("ssh config: using Host {0} (HostName {1}, {2}) as your alias for the server." -f $mine.name, $shownHost, $shownPort)
        if ($doorPort) { $portFrom = 'Host ' + $mine.name }
        elseif ($mine.opts['port']) { Say ("ssh config: Host {0} has Port {1}, which is not a port number (1-65535): not copied." -f $mine.name, $mine.opts['port']) Yellow }
    }
    $defaultOk = $false                              # "no Port line" was asked for on purpose: -Port 0, or the answer 0
    if ($Port) { $doorPort = $Port; $portFrom = '-Port' }
    elseif ($PortGiven) { $doorPort = 0; $portFrom = '-Port 0'; $defaultOk = $true }
    New-Item -ItemType Directory -Force -Path $SshDir | Out-Null
    $key = Join-Path $SshDir ('hermes_' + $cfg.device)
    $pub = $key + '.pub'
    if (Test-Path -LiteralPath $key) { Say ("Key: {0} is already there (kept)." -f $key) }
    else {
        $r = Invoke-Native $SshKeygen ('-q -t ed25519 -f "{0}" -C hermes@{1} -N ""' -f $key, $cfg.device) $null $null
        if ($r.code -ne 0 -or -not (Test-Path -LiteralPath $pub)) { Bad ("ssh-keygen failed: {0}" -f $r.err); return $false }
        Say ("Key: made {0} (ed25519, Hermes only)." -f $key) Green
    }
    $own = Find-SshAlias $blocks $cfg.host $cfg.server
    if ($own) {
        # A block that is already there is the user's to change, never setup's: say what it holds and where it disagrees.
        $ownPort = Get-PortOf $own.opts
        $ownHost = ([string]$own.opts['hostname'] -split '\s+')[0]
        $has = 'no Port line'
        if ($ownPort) { $has = 'Port ' + $ownPort }
        $holds = $has
        if ($ownHost) { $holds = 'HostName ' + $ownHost + ', ' + $has }
        Say ("ssh config: Host {0} is already there (kept): {1}." -f $cfg.host, $holds)
        if ($doorPort -and $doorPort -ne $ownPort) {
            Say ("  It has {0}, but {1} says port {2}. Setup changes nothing in a block that is already there: edit it, or remove it and run setup again." -f $has, $portFrom, $doorPort) Yellow
        }
        $doorPort = $ownPort
        if (-not $mine -and $ownHost -match $HostNameRe) { $hostName = $ownHost }
    }
    else {
        if (-not $doorPort -and -not $mine -and -not $defaultOk) {
            $asked = Read-Port
            if ($asked -gt 0) { $doorPort = $asked; $portFrom = 'your answer' }
            elseif ($asked -eq 0) { $defaultOk = $true; $portFrom = 'your answer' }
        }
        if (-not $doorPort -and -not $defaultOk) {
            $why = 'there is no ssh alias for the server to copy it from'
            if ($mine) { $why = 'Host ' + $mine.name + ' has no Port line' }
            Bad ("No port is known for the server's OpenSSH: {0}, and no -Port was given. Setup never assumes 22, so nothing was changed. Name the port with -Port <n>, or the ssh alias that has it with -Alias <name>. If ssh's own default is right, say so with -Port 0." -f $why)
            return $false
        }
        $nl = "`n"
        if ($text -match "`r`n") { $nl = "`r`n" }
        $block = New-Object System.Collections.Generic.List[string]
        $block.Add("Host " + $cfg.host)
        $block.Add("    HostName " + $hostName)
        if ($doorPort) { $block.Add("    Port " + $doorPort) }
        $block.Add("    User " + $cfg.user)
        $block.Add("    IdentityFile ~/.ssh/hermes_" + $cfg.device)
        $block.Add("    IdentitiesOnly yes")
        $block.Add("    StrictHostKeyChecking accept-new")
        if ($text -and -not $text.EndsWith("`n")) { $text += $nl }
        if ($text) { $text += $nl }
        $text += ($block -join $nl) + $nl
        [IO.File]::WriteAllText($sshConfig, $text, (New-Object Text.UTF8Encoding $false))
        Say ("ssh config: added Host {0} -> {1}@{2} with that key only." -f $cfg.host, $cfg.user, $hostName) Green
        if ($doorPort) { Say ("  The address comes from {0}, port {1} from {2}." -f $hostFrom, $doorPort, $portFrom) }
        else { Say ("  The address comes from {0}. {1} asked for ssh's own default, so there is no Port line. If the server's OpenSSH listens elsewhere, remove this block and run setup again with -Port <n>." -f $hostFrom, $portFrom) Yellow }
    }
    $aliasName = ''
    if ($mine) { $aliasName = $mine.name }
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    $doc = @{ server = $cfg.server; user = $cfg.user; port = $doorPort; alias = $aliasName; device = $cfg.device; mail = $cfg.mail }
    [IO.File]::WriteAllText($ConfigFile, ($doc | ConvertTo-Json) + "`n", (New-Object Text.UTF8Encoding $false))
    New-Item -ItemType Directory -Force -Path $cfg.mail | Out-Null
    Say ("Saved {0}; mail from {1} is collected into {2}." -f $ConfigFile, $cfg.server, $cfg.mail)
    Say ''
    Say "Now install the public key on the server (one line, pasteable; it asks for your usual login there):" Cyan
    # Through the user's own alias when there is one: it already knows the address and the port. Else the same two by hand.
    $via = '{0}@{1}' -f $cfg.user, $hostName
    if ($mine) { $via = $mine.name } elseif ($doorPort) { $via = '-p {0} {1}' -f $doorPort, $via }
    Say ("  Get-Content ""{0}"" | ssh {1} 'hermes keys add {2}'" -f $pub, $via, $cfg.device) White
    Say "Then check:  hermes.ps1 ping" Cyan
    return $true
}

function Ping-Server($cfg) {
    $r = Invoke-Native $Ssh (Get-SshArgs $cfg 'ping') $null $null
    if ($r.code -eq 0 -and $r.out -match '^hermes ok (\S+)$') { Say ("{0} answers: hermes {1} is at the door." -f $cfg.server, $Matches[1]) Green; return $true }
    if ($r.code -eq 0) { Bad ("{0} answered '{1}', not 'hermes ok <version>'. Do HostName and Port under Host {2} (in your ssh config) lead to the server's OpenSSH?" -f $cfg.server, $r.out, $cfg.host); return $false }
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
