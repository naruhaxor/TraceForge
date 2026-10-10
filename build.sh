#!/usr/bin/env bash
set -uo pipefail
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_python="${HANTEK_BUILD_PYTHON:-python3}"
run_builder() {
    if ! command -v "$build_python" >/dev/null 2>&1 || ! "$build_python" -c 'import json,ssl,ctypes,tarfile,urllib.request' >/dev/null 2>&1; then
        if [[ -n "${HANTEK_BUILD_PYTHON:-}" ]]; then
            printf 'Selected Python is missing or has an incomplete standard library: %s\n' "$build_python" >&2
            return 1
        fi
        if command -v apt-get >/dev/null 2>&1; then
            install_python=(sudo apt-get install -y python3 ca-certificates)
        elif command -v dnf >/dev/null 2>&1; then
            install_python=(sudo dnf install -y python3 ca-certificates)
        elif command -v pacman >/dev/null 2>&1; then
            install_python=(sudo pacman -S --needed --noconfirm python ca-certificates)
        elif command -v zypper >/dev/null 2>&1; then
            install_python=(sudo zypper --non-interactive install python3 ca-certificates)
        else
            printf 'Install Python 3.8 or newer to start the dependency downloader.\n' >&2
            return 1
        fi
        printf 'Python or its standard library is missing. Install it to start the dependency downloader?\n$ '
        printf '%q ' "${install_python[@]}"
        printf '\n'
        local answer
        read -r -p 'Install required packages? [y/N] ' answer || return 1
        [[ "$answer" == [yY] ]] || return 2
        if command -v apt-get >/dev/null 2>&1; then
            printf '$ sudo apt-get update\n'
            sudo apt-get update || return $?
        fi
        "${install_python[@]}" || return $?
    fi
    "$build_python" "$source_root/build.py" "$@"
}
if [[ "${1:-}" == --build-terminal ]]; then
    shift
    run_builder "$@"
    result=$?
    printf '\nPress any key to close the build terminal...'
    IFS= read -r -s -n 1
    exit "$result"
fi
if [[ -t 0 || " $* " == *" --no-gui "* ]]; then
    run_builder "$@"
    exit $?
fi
if command -v konsole >/dev/null 2>&1; then
    exec konsole --separate -p 'tabtitle=TraceForge AppImage build' -e /bin/bash "$source_root/build.sh" --build-terminal "$@"
elif command -v ptyxis >/dev/null 2>&1; then
    exec ptyxis --standalone --title='TraceForge AppImage build' -- /bin/bash "$source_root/build.sh" --build-terminal "$@"
elif command -v gnome-terminal >/dev/null 2>&1; then
    exec gnome-terminal --wait --title='TraceForge AppImage build' -- /bin/bash "$source_root/build.sh" --build-terminal "$@"
elif command -v xfce4-terminal >/dev/null 2>&1; then
    exec xfce4-terminal --disable-server --title='TraceForge AppImage build' --execute /bin/bash "$source_root/build.sh" --build-terminal "$@"
elif command -v xterm >/dev/null 2>&1; then
    exec xterm -T 'TraceForge AppImage build' -e /bin/bash "$source_root/build.sh" --build-terminal "$@"
else
    run_builder "$@"
fi
