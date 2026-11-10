#!/bin/bash

# Stage the rhythm runtime files into the Docker build context, and record the
# identity of what was staged.
#
# Rhythm lives in its own repository. Its staged files are not individually
# diffed against the host (the local copy is nested, the uploaded copy is
# flattened, so a path-based diff of the tree always looks dirty). Instead
# write_rhythm_revision stages one identity file, rhythm/REVISION, whose
# content covers every staged file. That single path is in the deployed-path
# list, so a rhythm content change is visible to the classifier (private-core
# is rebuilt) and an unchanged rhythm is not (nothing is rebuilt), while the
# same file makes the running revision observable inside the image.
#
# Usage: stage_rhythm_source <rhythm_src> <rhythm_dst> <require:0|1>
#        write_rhythm_revision <rhythm_src> <rhythm_dst>
#
# stage_rhythm_source returns:
#   0  complete source staged into <rhythm_dst>
#   2  source is incomplete and optional; <rhythm_dst> holds a .empty marker
#   1  source is incomplete and required; nothing is staged
#   3  the source was complete but staging failed (filesystem); nothing usable
#
# write_rhythm_revision returns 0 after writing <rhythm_dst>/REVISION, or 1 if
# the staged tree could not be hashed.

rhythm_required_files() {
    printf '%s\n' \
        rhythm/__init__.py \
        rhythm/tools.py \
        rhythm_binding.py \
        schema_registry.py \
        schema_db.py \
        state_machine.py \
        catcher_agent.js \
        prime_interceptor.js \
        executor_binding.js
}

stage_rhythm_source() {
    local rhythm_src="$1"
    local rhythm_dst="$2"
    local require="${3:-0}"
    local f
    local missing=()

    while IFS= read -r f; do
        [[ -f "$rhythm_src/$f" ]] || missing+=("$f")
    done < <(rhythm_required_files)

    if [[ "${#missing[@]}" -eq 0 ]]; then
        rm -rf "$rhythm_dst"
        # Copy from the same list the verification and the identity digest use.
        # A separate hardcoded cp list could stage a tenth file that no digest
        # covers, so a change to it would ship without triggering a rebuild.
        while IFS= read -r f; do
            mkdir -p "$rhythm_dst/$(dirname "$f")" || return 3
            cp "$rhythm_src/$f" "$rhythm_dst/$f" || return 3
        done < <(rhythm_required_files)
        # A silent partial copy is the failure this guard exists to prevent, so
        # verify the staged tree rather than trusting the copy commands. `-s`
        # also rejects a zero-byte file, which is what a truncated copy leaves
        # behind. The required paths are the exact staged layout: the two
        # package files under rhythm/, the rest at the top level.
        while IFS= read -r f; do
            [[ -s "$rhythm_dst/$f" ]] || return 3
        done < <(rhythm_required_files)
        return 0
    fi

    if [[ "$require" == "1" ]]; then
        printf 'missing rhythm file(s): %s\n' "${missing[*]}" >&2
        return 1
    fi

    mkdir -p "$rhythm_dst"
    touch "$rhythm_dst/.empty"
    return 2
}

# Write <rhythm_dst>/REVISION: "<git revision>[-dirty] sha256:<content digest>".
#
# The git revision identifies the checkout the source came from; the content
# digest covers every staged file, so an uncommitted edit or a non-git source
# still produces a different identity. Both halves matter: a revision alone
# would hide local edits, and a digest alone would not tell an operator which
# commit is running.
write_rhythm_revision() {
    local rhythm_src="$1" rhythm_dst="$2"
    local hash_cmd="" revision="unknown" content f

    if command -v sha256sum >/dev/null 2>&1; then
        hash_cmd="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        hash_cmd="shasum -a 256"
    else
        echo "write_rhythm_revision: need sha256sum or shasum" >&2
        return 1
    fi

    if command -v git >/dev/null 2>&1 \
        && revision="$(git -C "$rhythm_src" rev-parse HEAD 2>/dev/null)" \
        && [[ -n "$revision" ]]; then
        # An uncommitted edit must not masquerade as the checked-out revision.
        if [[ -n "$(git -C "$rhythm_src" status --porcelain -- $(rhythm_required_files) 2>/dev/null)" ]]; then
            revision="$revision-dirty"
        fi
    else
        revision="unknown"
    fi

    content=""
    while IFS= read -r f; do
        content+="$(cd "$rhythm_dst" && $hash_cmd "$f" 2>/dev/null)"$'\n' || return 1
    done < <(rhythm_required_files)

    # Truncated to 128 bits on purpose: this identifies a build for change
    # detection, not for integrity, and the short form stays readable in a log
    # line and a health response. Do not reuse it as a security digest.
    content="$(printf '%s' "$content" | $hash_cmd | cut -c1-32)" || return 1
    [[ -n "$content" ]] || return 1

    printf '%s sha256:%s\n' "$revision" "$content" > "$rhythm_dst/REVISION" || return 1
    [[ -s "$rhythm_dst/REVISION" ]] || return 1
    return 0
}
