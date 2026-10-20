#!/bin/bash
# scripts/install-hooks.sh - install the pre-push hook that runs scripts/check-private.sh on every commit a push adds
# (with your ~/.config/leafy/denylist.txt, if you have one). Run once per clone:  bash scripts/install-hooks.sh
# An older pre-push hook that isn't this one is moved aside to pre-push.set-aside-<date>, never deleted.
set -e
cd "$(git rev-parse --show-toplevel)"
H=$(git rev-parse --git-path hooks)/pre-push
mkdir -p "$(dirname "$H")"
if [ -f "$H" ] && ! grep -q 'check-private.sh' "$H"; then mv "$H" "$H.set-aside-$(date +%F)"; echo "moved your old hook to $H.set-aside-$(date +%F)"; fi
cat > "$H" <<'EOF'
#!/bin/bash
# pre-push (from scripts/install-hooks.sh): no personal data in what this push adds. Git passes one line per ref.
R=$(git rev-parse --show-toplevel); C="$R/scripts/check-private.sh"
[ -f "$C" ] || { echo "pre-push: no scripts/check-private.sh on this branch; not checked"; exit 0; }
while read -r lref lsha rref rsha; do
  case $lsha in *[!0]*) ;; *) continue ;; esac                  # deleting a branch: nothing to check
  case $rsha in *[!0]*) range=("$rsha..$lsha") ;; *) range=("$lsha" --not --remotes) ;; esac   # new branch: commits no remote has
  bash "$C" "${range[@]}" || { echo "pre-push: push stopped. Fix the lines above (git commit --amend or a new commit), then push again."; exit 1; }
done
EOF
chmod +x "$H"
echo "installed $H: check-private.sh now runs before every push"
