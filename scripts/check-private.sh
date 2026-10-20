#!/bin/bash
# scripts/check-private.sh - stop personal data reaching this public repository. Run it before every push (the pre-push
# hook from scripts/install-hooks.sh does); CI runs the generic checks on every pull request.
# It looks for home paths (C:\Users\<name>, /home/<name>, /Users/<name>), *.ts.net host names, IPv4 and IPv6 addresses,
# email addresses, and every line of ~/.config/leafy/denylist.txt (yours alone, never committed: one name per line,
# # starts a comment; matched ignoring case).
#   bash scripts/check-private.sh                   the staged changes (git diff --cached)
#   bash scripts/check-private.sh main..HEAD        every commit in a range, one at a time, messages included
#   bash scripts/check-private.sh --tree            every tracked file as it is now
#   bash scripts/check-private.sh --history [rev]   every commit reachable from rev (default main)
#   --generic                                       skip the denylist (what CI runs)
# Prints file:line (and the commit) for each hit and exits 1 if there are any. Placeholder names (you, user, example.com,
# 127.0.0.1, the documentation address ranges ...) pass. A line containing "private-ok" is skipped: use it only for a
# deliberate example that has to show a pattern, never to wave through a real name.
set -e
cd "$(git rev-parse --show-toplevel)"
DENY="${LEAFY_DENYLIST:-$HOME/.config/leafy/denylist.txt}"; mode=staged; revs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --generic) DENY= ;;
    --tree) mode=tree ;;
    --history) mode=log; revs=("${2:-main}"); [ $# -gt 1 ] && shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) mode=log; revs+=("$1") ;;
  esac
  shift
done
[ -n "$DENY" ] && [ ! -f "$DENY" ] && DENY=

# every source becomes lines of "where<TAB>text"; for diffs, only added lines
diff_lines() {   # unified diff (-U0) on stdin -> "[commit X ]file:line<TAB>added text"; commit messages from git log too
  awk 'BEGIN { msg = 0 }
    /^\001commit / { c = "commit " substr($0, 9) " "; msg = 1; n = 0; next }
    /^\001end$/ { msg = 0; next }
    msg { n++; if ($0 != "") print c "message:" n "\t" $0; next }
    /^--- / { hdr = 1; next }
    /^\+\+\+ / && hdr { f = substr($0, 5); sub(/^b\//, "", f); hdr = 0; next }
    { hdr = 0 }
    /^@@ / { match($0, /\+[0-9]+/); n = substr($0, RSTART + 1, RLENGTH - 1) + 0; next }
    /^\+/ { print c f ":" n "\t" substr($0, 2); n++ }'
}
case $mode in
  staged) src() { git diff --cached -U0 --no-color --no-ext-diff | diff_lines; } ;;
  log)    src() { git log -p -U0 --no-color --no-ext-diff --format='%x01commit %h%n%B%x01end' "${revs[@]}" -- | diff_lines; } ;;
  tree)   src() { git grep -z -I -l -e '' | xargs -0 awk '{ print FILENAME ":" FNR "\t" $0 }'; } ;;
esac

src | awk -F '\t' -v deny="$DENY" '
  BEGIN {
    # placeholder names that are fine in public text
    split("you user username name me runner example your-name yourname public default all users shared <name> <user>", a, " ")
    for (i in a) okname[a[i]] = 1
    split("example tailnet your-tailnet tailnet-name", a, " "); for (i in a) oktail[a[i]] = 1
    split("example.com example.org example.net users.noreply.github.com", a, " "); for (i in a) okmail[a[i]] = 1
    split("git@github.com noreply@anthropic.com noreply@github.com", a, " "); for (i in a) okaddr[a[i]] = 1
    nd = 0
    if (deny != "") while ((getline l < deny) > 0) { sub(/\r$/, "", l); sub(/^[ \t]+/, "", l); sub(/[ \t]+$/, "", l)
      if (l != "" && l !~ /^#/) dl[++nd] = tolower(l) }
  }
  function before(s, p) { return p > 1 ? substr(s, p - 1, 1) : "" }
  function after(s, p, n) { return substr(s, p + n, 1) }
  function hit(what) { found = found (found == "" ? "" : "; ") what }
  function v4ok(ip,   o, i) {   # a real address that is not loopback, unspecified or a documentation range
    split(ip, o, "."); for (i = 1; i <= 4; i++) if (o[i] + 0 > 255 || length(o[i]) > 3) return 0
    if (o[1] == 127 || ip == "0.0.0.0" || ip == "255.255.255.255") return 0
    if (o[1] == 192 && o[2] == 0 && o[3] == 2 || o[1] == 198 && o[2] == 51 && o[3] == 100 || o[1] == 203 && o[2] == 0 && o[3] == 113) return 0
    return 1
  }
  function v6ok(t,   g, n, i, full, dbl) {
    if (t ~ /:::/) return 0
    dbl = index(t, "::") > 0; if (dbl && index(substr(t, index(t, "::") + 2), "::")) return 0
    n = split(t, g, ":"); full = 0
    for (i = 1; i <= n; i++) if (g[i] != "") { if (length(g[i]) > 4) return 0; full++ }
    if (tolower(t) == "::1" || tolower(t) ~ /^2001:0?db8:/) return 0   # loopback, documentation range
    if (!dbl) return n == 8
    if (full < 2 || full > 7) return 0
    for (i = 1; i <= n; i++) if (length(g[i]) >= 3 || g[i] ~ /[A-Fa-f]/) return 1   # not a slice like a[1::2]
    return 0
  }
  {
    where = $1; text = substr($0, length($1) + 2); found = ""
    if (index(text, "private-ok")) next
    s = text; while (match(s, /[A-Za-z]:[\\\/]+[Uu]sers[\\\/]+[A-Za-z0-9_][A-Za-z0-9._-]*/)) {
      m = substr(s, RSTART, RLENGTH); nm = m; sub(/^.*[\\\/]/, "", nm)
      if (!(tolower(nm) in okname)) hit("home path " m); s = substr(s, RSTART + RLENGTH) }
    s = text; while (match(s, /\/(home|Users)\/[A-Za-z0-9_][A-Za-z0-9._-]*/)) {
      m = substr(s, RSTART, RLENGTH); nm = m; sub(/^.*\//, "", nm); b = before(s, RSTART)
      if (b != ":" && !(tolower(nm) in okname)) hit("home path " m); s = substr(s, RSTART + RLENGTH) }   # C:/Users is caught above
    s = text; while (match(s, /[A-Za-z0-9-]+\.ts\.net/)) {
      m = substr(s, RSTART, RLENGTH); nm = tolower(m); sub(/\.ts\.net$/, "", nm)
      if (!(nm in oktail)) hit("tailnet name " m); s = substr(s, RSTART + RLENGTH) }
    s = text; while (match(s, /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/)) {
      m = substr(s, RSTART, RLENGTH)
      if (before(s, RSTART) !~ /[0-9.A-Za-z]/ && after(s, RSTART, RLENGTH) !~ /[0-9A-Za-z]/ && \
          !(after(s, RSTART, RLENGTH) == "." && substr(s, RSTART + RLENGTH + 1, 1) ~ /[0-9]/) && v4ok(m)) hit("IPv4 address " m)
      s = substr(s, RSTART + RLENGTH) }
    s = text; while (match(s, /[0-9A-Fa-f]*:[0-9A-Fa-f]*:[0-9A-Fa-f:]*/)) {
      m = substr(s, RSTART, RLENGTH)
      if (before(s, RSTART) !~ /[0-9A-Za-z_.]/ && after(s, RSTART, RLENGTH) !~ /[0-9A-Za-z_.]/ && v6ok(m)) hit("IPv6 address " m)
      s = substr(s, RSTART + RLENGTH) }
    s = text; while (match(s, /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z]+/)) {
      m = substr(s, RSTART, RLENGTH); dom = tolower(m); sub(/^.*@/, "", dom)
      if (!(tolower(m) in okaddr) && !(dom in okmail) && dom !~ /\.example$/) hit("email " m); s = substr(s, RSTART + RLENGTH) }
    if (nd) { lt = tolower(text); for (i = 1; i <= nd; i++) if (index(lt, dl[i])) hit("denylist entry " i) }
    if (found != "") { print where ": " found; bad++ }
  }
  END {
    if (bad) { printf "\ncheck-private: %d line%s with personal data. Replace each with a placeholder ({{SERVER}}, {{HOME_PC}} ... see docs/mentor.md) or remove it.\n", bad, bad == 1 ? "" : "s"; exit 1 }
    print "check-private: nothing personal found" (nd ? " (denylist: " nd " entries)" : " (generic checks only)")
  }'
