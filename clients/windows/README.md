# Windows client
`hermes.ps1` (setup, ping, send, torrent, magnet, collect) and `install-sendto.ps1` (the right-click Send To entries, with the Hermes medallion from `hermes.png`): Windows PowerShell 5 only, no Python on the PC. How to set it up, what each command does, and the manual test checklist: `docs/windows-client.md`. The spec is `docs/design.md` §3 step 1 (send) and step 3 (collect).

```
powershell -ExecutionPolicy Bypass -File clients\windows\hermes.ps1 setup -Server {{SERVER}} -User <user>
powershell -ExecutionPolicy Bypass -File clients\windows\install-sendto.ps1
```
