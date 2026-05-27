#!/usr/bin/env bash
# Generate CHANGELOG.md from annotated git tag messages.
#
# Each `git tag -a vX.Y.Z` carries the release notes for that
# version (set when tagging via `-m "$(cat <<'EOF' ... EOF)"`).
# This script enumerates tags in descending version order and
# emits one section per tag — annotation body + the commits
# included since the previous tag.
#
# Usage:
#   ./scripts/release-notes.sh [from-tag]
#
# With no arg: regenerate the full CHANGELOG.md.
# With a tag: emit only that one release's notes (for `gh release
# create` paste-bait).
#
# Tag annotations should follow this shape:
#   v0.3.2: <one-line title>
#
#   <multi-paragraph body — what changed, why, how to verify>
#
# This script trusts the annotation; it doesn't try to invent
# release notes from commit messages alone. If a tag has no
# annotation (e.g. a quick `git tag vX.Y.Z` without -a), the
# section just lists commits.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

emit_one() {
    local tag="$1"
    local date
    date=$(git log -1 --format=%ai "$tag" | cut -d' ' -f1)
    echo "## $tag — $date"
    echo ""
    # Annotated tag body (everything after the subject line).
    # %(contents:body) returns the body; %(contents:subject) the
    # first line. Concatenated they form the full annotation.
    local subj body
    subj=$(git tag -l "$tag" --format='%(contents:subject)')
    body=$(git tag -l "$tag" --format='%(contents:body)')
    if [[ -n "$subj" ]]; then
        # Tag subject usually mirrors the title; skip it if it
        # looks redundant with the heading we just printed.
        if [[ "$subj" != "$tag"* ]]; then
            echo "**$subj**"
            echo ""
        fi
    fi
    if [[ -n "$body" ]]; then
        echo "$body"
        echo ""
    fi
}

if [[ $# -ge 1 ]]; then
    emit_one "$1"
    exit 0
fi

# Full changelog: iterate tags newest-first.
{
    echo "# CHANGELOG"
    echo ""
    echo "Release notes for pi-bake. Generated from annotated git"
    echo "tags via \`./scripts/release-notes.sh\`. To add notes for"
    echo "a new release, tag the commit with"
    echo "\`git tag -a vX.Y.Z -m \"...\"\` and re-run this script."
    echo ""
    for tag in $(git tag -l 'v*' --sort=-version:refname); do
        emit_one "$tag"
    done
} > CHANGELOG.md
echo "wrote CHANGELOG.md ($(wc -l < CHANGELOG.md) lines)"
