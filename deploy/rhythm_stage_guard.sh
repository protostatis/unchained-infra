#!/bin/bash

# Stage the rhythm runtime files into the Docker build context.
#
# Rhythm lives in its own repository and is excluded from the deployed-path
# checksum list (the local copy is nested, the remote copy is flattened, so a
# path-based diff always looks dirty). That means a rhythm content change does
# not by itself trigger a rebuild: callers MUST force a full rebuild whenever
# this function reports that it staged a complete source, otherwise the image
# keeps the previously baked rhythm.
#
# Usage: stage_rhythm_source <rhythm_src> <rhythm_dst> <require:0|1>
#
# Returns:
#   0  complete source staged into <rhythm_dst>
#   2  source is incomplete and optional; <rhythm_dst> holds a .empty marker
#   1  source is incomplete and required; nothing is staged

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
        mkdir -p "$rhythm_dst/rhythm"
        cp "$rhythm_src/rhythm/__init__.py" "$rhythm_src/rhythm/tools.py" \
            "$rhythm_dst/rhythm/"
        cp "$rhythm_src/rhythm_binding.py" "$rhythm_src/schema_registry.py" \
            "$rhythm_src/schema_db.py" "$rhythm_src/state_machine.py" \
            "$rhythm_src/catcher_agent.js" "$rhythm_src/prime_interceptor.js" \
            "$rhythm_src/executor_binding.js" "$rhythm_dst/"
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
