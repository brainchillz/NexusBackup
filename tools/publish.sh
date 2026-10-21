#!/bin/bash
# Push to the public repository only after the publication gate passes.
#
#   tools/publish.sh                 gate HEAD's history and the working tree, then push main and tags to "public"
#   tools/publish.sh --remote NAME   another remote
#   tools/publish.sh --install-hook  install a pre-push hook that runs the gate for pushes to github.com
#
# The gate's lists live outside the repository: $GATE_TERMS_FILE (default ../gate-terms.txt)
# and $GATE_AUTHORS_FILE (default ../gate-authors.txt).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE=public
TERMS="${GATE_TERMS_FILE:-$HERE/../gate-terms.txt}"
AUTHORS="${GATE_AUTHORS_FILE:-$HERE/../gate-authors.txt}"
if [ "${1:-}" = --install-hook ]; then
    cat > "$HERE/.git/hooks/pre-push" <<HOOK
#!/bin/bash
# installed by tools/publish.sh: gate pushes that go to github.com
url="\$2"
case "\$url" in *github.com*) ;; *) exit 0 ;; esac
exec python3 "$HERE/tools/gate.py" --repo "$HERE" --terms-file "$TERMS" --authors-file "$AUTHORS" --all
HOOK
    chmod +x "$HERE/.git/hooks/pre-push"; echo "pre-push hook installed"; exit 0
fi
[ "${1:-}" = --remote ] && REMOTE="$2"
[ -z "$(git -C "$HERE" status --porcelain)" ] || { echo "working tree is not clean; commit first"; exit 1; }
python3 "$HERE/tools/gate.py" --repo "$HERE" --terms-file "$TERMS" --authors-file "$AUTHORS" --all --worktree
git -C "$HERE" push "$REMOTE" main --tags
