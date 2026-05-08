#!/usr/bin/env bash
# install-udev-rules.sh — install repo-local ST-Link udev rule.
#
# Run with: sudo bash stlink-toolkit/scripts/install-udev-rules.sh
#
# Idempotent: copies the rule, reloads udev, and triggers change events for
# any already-attached ST-Link probes so perms are corrected without replug.

set -euo pipefail

VERIFY_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --verify-only) VERIFY_ONLY=1 ;;
        -h|--help)
            echo "Usage: $0 [--verify-only]"
            echo "  (no args)      install rule + reload udev (requires sudo)"
            echo "  --verify-only  print current rule + probe perms, no changes (no sudo)"
            exit 0
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/../udev/60-awto-stlink.rules"
DST="/etc/udev/rules.d/60-awto-stlink.rules"

verify_perms() {
    echo "[install-udev] current ST-Link perms:"
    shopt -s nullglob
    local found=0
    for d in /sys/bus/usb/devices/*; do
        local vid_file="${d}/idVendor"
        local pid_file="${d}/idProduct"
        [[ -f "${vid_file}" && -f "${pid_file}" ]] || continue
        local vid; vid=$(<"${vid_file}")
        [[ "${vid}" == "0483" ]] || continue
        local pid; pid=$(<"${pid_file}")
        local busnum; busnum=$(<"${d}/busnum" 2>/dev/null || echo "")
        local devnum; devnum=$(<"${d}/devnum" 2>/dev/null || echo "")
        [[ -n "${busnum}" && -n "${devnum}" ]] || continue
        local serial; serial=$(<"${d}/serial" 2>/dev/null || echo "")
        local node; node=$(printf "/dev/bus/usb/%03d/%03d" "${busnum}" "${devnum}")
        if [[ -e "${node}" ]]; then
            local perms; perms=$(stat -c '%a %U:%G' "${node}")
            echo "  ${vid}:${pid} sn=${serial:0:8}.. ${node} -> ${perms}"
            found=1
        fi
    done
    [[ ${found} -eq 1 ]] || echo "  (no ST-Link probes attached)"
    echo "[install-udev] rule status:"
    if [[ -f "${DST}" ]]; then
        echo "  installed: ${DST} ($(stat -c '%y' "${DST}"))"
    else
        echo "  NOT installed (expected at ${DST})"
        return 1
    fi
    if ! getent group plugdev >/dev/null; then
        echo "  WARNING: group 'plugdev' missing"
        return 1
    fi
    return 0
}

if [[ ${VERIFY_ONLY} -eq 1 ]]; then
    verify_perms
    exit $?
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo "error: must run as root (use: sudo bash $0)" >&2
    exit 1
fi

if [[ ! -f "${SRC}" ]]; then
    echo "error: rule source not found: ${SRC}" >&2
    exit 1
fi

# Ensure the `plugdev` system group exists (it does not on stock Fedora).
if ! getent group plugdev >/dev/null; then
    echo "[install-udev] creating system group: plugdev"
    groupadd -r plugdev
else
    echo "[install-udev] group plugdev already exists"
fi

# Add the invoking user (the one who ran sudo) to plugdev so they can flash
# without root. Group membership only takes effect on next login.
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    if id -nG "${SUDO_USER}" | tr ' ' '\n' | grep -qx plugdev; then
        echo "[install-udev] user ${SUDO_USER} already in plugdev"
    else
        echo "[install-udev] adding ${SUDO_USER} to plugdev (log out + back in to take effect)"
        usermod -a -G plugdev "${SUDO_USER}"
    fi
fi

echo "[install-udev] installing ${SRC} -> ${DST}"
install -m 0644 -o root -g root "${SRC}" "${DST}"

# Disable upstream stlink rules — they reference `plugdev` (which we now
# create) but assign weaker perms and conflict with this rule's GROUP/MODE.
# Renaming makes the override visible and reversible.
for f in /etc/udev/rules.d/49-stlinkv1.rules \
         /etc/udev/rules.d/49-stlinkv2.rules \
         /etc/udev/rules.d/49-stlinkv2-1.rules \
         /etc/udev/rules.d/49-stlinkv3.rules; do
    if [[ -f "${f}" && ! -L "${f}" ]]; then
        echo "[install-udev] disabling ${f} -> ${f}.disabled-by-awto"
        mv -n "${f}" "${f}.disabled-by-awto"
    fi
done

echo "[install-udev] reloading udev rules"
udevadm control --reload

echo "[install-udev] triggering change events for attached ST-Link probes"
# Fire change events only for the ST-Link VID so we don't disturb other USB.
udevadm trigger --action=change --subsystem-match=usb \
    --attr-match=idVendor=0483 || true
udevadm settle --timeout=5 || true

echo "[install-udev] verification:"
shopt -s nullglob
for d in /sys/bus/usb/devices/*; do
    vid_file="${d}/idVendor"
    pid_file="${d}/idProduct"
    [[ -f "${vid_file}" && -f "${pid_file}" ]] || continue
    vid=$(<"${vid_file}")
    [[ "${vid}" == "0483" ]] || continue
    pid=$(<"${pid_file}")
    busnum=$(<"${d}/busnum" 2>/dev/null || echo "")
    devnum=$(<"${d}/devnum" 2>/dev/null || echo "")
    [[ -n "${busnum}" && -n "${devnum}" ]] || continue
    serial=$(<"${d}/serial" 2>/dev/null || echo "")
    node=$(printf "/dev/bus/usb/%03d/%03d" "${busnum}" "${devnum}")
    if [[ -e "${node}" ]]; then
        perms=$(stat -c '%a %U:%G' "${node}")
        echo "  ${vid}:${pid} sn=${serial:0:8}.. ${node} -> ${perms}"
    fi
done

echo "[install-udev] done. If a probe still fails, unplug + replug it."
