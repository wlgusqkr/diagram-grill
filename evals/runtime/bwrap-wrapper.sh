#!/bin/sh

set -eu

real_bwrap="$0.real"
if [ ! -x "$real_bwrap" ]; then
    echo "diagram-grill-eval: pinned bubblewrap binary is unavailable" >&2
    exit 125
fi

# Codex inspects these capabilities before choosing a PATH-discovered bwrap.
if [ "$#" -eq 1 ] && [ "$1" = "--help" ]; then
    exec "$real_bwrap" "$@"
fi

# Codex 0.149.1 probes a PATH-discovered system bubblewrap with this exact,
# non-payload command before using it. The real invocation is checked below.
if [ "$#" -eq 6 ] \
    && [ "$1" = "--unshare-user" ] \
    && [ "$2" = "--unshare-net" ] \
    && [ "$3" = "--ro-bind" ] \
    && [ "$4" = "/" ] \
    && [ "$5" = "/" ] \
    && [ "$6" = "/bin/true" ]; then
    exec "$real_bwrap" "$@"
fi

has_user=0
has_pid=0
has_net=0
has_proc=0
has_cap_drop=0
has_command=0
expect=""

for argument in "$@"; do
    if [ -n "$expect" ]; then
        case "$expect:$argument" in
            proc:/proc) has_proc=1 ;;
            cap:ALL) has_cap_drop=1 ;;
            *)
                echo "diagram-grill-eval: unsafe bubblewrap argument after --$expect" >&2
                exit 125
                ;;
        esac
        expect=""
        continue
    fi
    case "$argument" in
        --unshare-user) has_user=1 ;;
        --unshare-pid) has_pid=1 ;;
        --unshare-net) has_net=1 ;;
        --proc) expect="proc" ;;
        --cap-drop) expect="cap" ;;
        --share-net|--disable-userns|--userns-block-fd)
            echo "diagram-grill-eval: forbidden bubblewrap argument: $argument" >&2
            exit 125
            ;;
        --) has_command=1 ;;
    esac
done

if [ -n "$expect" ] \
    || [ "$has_user" -ne 1 ] \
    || [ "$has_pid" -ne 1 ] \
    || [ "$has_net" -ne 1 ] \
    || [ "$has_proc" -ne 1 ] \
    || [ "$has_cap_drop" -ne 1 ] \
    || [ "$has_command" -ne 1 ]; then
    echo "diagram-grill-eval: bubblewrap isolation contract is incomplete" >&2
    exit 125
fi

exec /usr/bin/python3 -I -S -c '
import os
import sys

real = sys.argv[1]
arguments = sys.argv[2:]
try:
    delimiter = arguments.index("--")
except ValueError:
    raise SystemExit(125)
arguments[delimiter:delimiter] = ["--tmpfs", "/tmp"]
os.execv(real, [real, *arguments])
' "$real_bwrap" "$@"
