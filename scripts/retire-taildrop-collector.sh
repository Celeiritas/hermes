#!/bin/bash
# scripts/retire-taildrop-collector.sh [--go] - find whatever collected the phones' Taildrops on this server before
# Hermes (a user unit or timer, or a crontab line, that runs `tailscale file get` or writes to Hermes-Mail) and set it
# aside, never delete it. Without --go it only says what it found. With --go: each unit is disabled and its file moved
# to <name>.set-aside-<date>; each crontab line is commented out with a dated note. Hermes's own hermes-* units are
# left alone. The scripts those units ran stay where they are.
set -e
U=${HERMES_UNIT_DIR:-$HOME/.config/systemd/user}; D=$(date +%Y%m%d-%H%M); go=0
[ "$1" = --go ] && go=1
units=()
for f in "$U"/*.service "$U"/*.timer; do
  [ -f "$f" ] || continue
  case $(basename "$f") in hermes-*) continue ;; esac
  grep -Eq 'tailscale file get|Hermes-Mail' "$f" && units+=("$f")
done
cron=
if command -v crontab >/dev/null 2>&1; then cron=$(crontab -l 2>/dev/null | grep -En 'tailscale file get|Hermes-Mail' | grep -v '^[0-9]*:#' || true); fi
if [ ${#units[@]} = 0 ] && [ -z "$cron" ]; then echo "retire-taildrop-collector: nothing collects Taildrops here besides Hermes"; exit 0; fi
for f in "${units[@]}"; do
  n=$(basename "$f")
  if [ $go = 1 ]; then
    command -v systemctl >/dev/null 2>&1 && systemctl --user disable --now "$n" 2>/dev/null || true
    mv "$f" "$f.set-aside-$D"; echo "set aside $f -> $f.set-aside-$D"
  else echo "would set aside $f (unit $n)"; fi
done
if [ -n "$cron" ]; then
  if [ $go = 1 ]; then
    crontab -l | sed -E "/tailscale file get|Hermes-Mail/{/^#/!s|^|# set aside by hermes $D: |}" | crontab -
    echo "commented out in crontab:"; echo "$cron"
  else echo "would comment out in crontab:"; echo "$cron"; fi
fi
[ $go = 1 ] && command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null || true
[ $go = 1 ] || echo "(preview; run with --go to do it)"
