#!/usr/bin/env bash
set -u
helper=$1
result_file=$2
printf '\033]0;Hantek USB setup - sudo password\007'
printf '\nTraceForge - one-time USB setup\n\n'
printf 'This installs a USB access rule for your Hantek 1008B.\n'
printf 'It allows your desktop user to use the scope without running the app as root.\n\n'
printf 'Command being run:\n  sudo /bin/bash -x %q\n\n' "$helper"
printf 'Enter your Linux password when sudo asks. Password characters will not be displayed.\n\n'
sudo -- /bin/bash -x "$helper"
setup_status=$?
if [[ "$setup_status" == 0 ]]; then
    printf '\nUSB setup finished successfully.\n'
    printf 'Press any key to close this terminal and open TraceForge...'
else
    printf '\nUSB setup did not complete (status %s).\n' "$setup_status"
    printf 'Press any key to close this terminal and return to setup...'
fi
if ! IFS= read -r -s -n 1 < /dev/tty; then
    exit 1
fi
printf '\n'
printf '%s\n' "$setup_status" > "$result_file"
exit "$setup_status"
