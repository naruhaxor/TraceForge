#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
if [[ "$(id -u)" != 0 ]]; then
    printf '%s\n' 'Administrator permission is required. Run this helper with sudo.' >&2
    exit 1
fi
rule_path=/etc/udev/rules.d/70-hantek1008-local.rules
rule_text='SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0783", ATTR{idProduct}=="5725", MODE="0660", TAG+="uaccess"'
if [[ -e "$rule_path" && "$(cat "$rule_path")" != "$rule_text" ]]; then
    printf 'An existing rule differs: %s\nIt has been left unchanged.\n' "$rule_path" >&2
    exit 1
fi
if [[ ! -e "$rule_path" ]]; then
    install -d -m 0755 /etc/udev/rules.d
    rule_tmp=$(mktemp /etc/udev/rules.d/.hantek-rule.XXXXXX)
    trap 'rm -f -- "$rule_tmp"' EXIT
    printf '%s\n' "$rule_text" > "$rule_tmp"
    chmod 0644 "$rule_tmp"
    mv -n -- "$rule_tmp" "$rule_path"
fi
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=usb --attr-match=idVendor=0783 --attr-match=idProduct=5725
udevadm settle --timeout=10
printf '%s\n' 'Hantek USB access is configured. Reconnect the scope if it is not detected.'
