#!/usr/bin/env bash
# Which Chromium browsers are installed, and what the installer says about them.
#
# Sourced by install.sh. Chrome, Brave and Chromium all run the same Manifest V3
# extension, so the only per-browser work is: name it correctly, open the right
# extensions page, and grant a Flatpak sandbox access to this folder.
#
# Only shell builtins are used here (printf, command, case, read), on purpose:
# tests/test_install_browsers.py runs these functions with PATH pointing at a
# throwaway directory of stub executables, so no browser on the host can leak
# into a result.

# Fixed order: Chrome first, then Brave, then Chromium. Detection output is
# sorted by it, and so is everything printed to the user.
BROWSER_FAMILIES="chrome brave chromium"

browser_label() {
    case "${1:-}" in
        chrome)   printf 'Google Chrome\n' ;;
        brave)    printf 'Brave\n' ;;
        chromium) printf 'Chromium\n' ;;
        *) return 1 ;;
    esac
}

browser_url() {
    case "${1:-}" in
        brave)           printf 'brave://extensions\n' ;;
        chrome|chromium) printf 'chrome://extensions\n' ;;
        *) return 1 ;;
    esac
}

flatpak_app() {
    case "${1:-}" in
        chrome)   printf 'com.google.Chrome\n' ;;
        brave)    printf 'com.brave.Browser\n' ;;
        chromium) printf 'org.chromium.Chromium\n' ;;
        *) return 1 ;;
    esac
}

# The executable names each family ships under, the way it lands in PATH: a
# native package, a snap symlink or a Flatpak export all resolve through
# `command -v`. New names belong here and nowhere else.
browser_names() {
    case "${1:-}" in
        chrome)   printf 'google-chrome google-chrome-stable\n' ;;
        brave)    printf 'brave-browser brave\n' ;;
        chromium) printf 'chromium chromium-browser\n' ;;
        *) return 1 ;;
    esac
}

# One line per installed browser, tab separated: family, kind, label.
# kind is native, snap or flatpak.
detect_browsers() {
    local family name path kind seen=" "
    for family in $BROWSER_FAMILIES; do
        for name in $(browser_names "$family"); do
            path="$(command -v "$name" 2>/dev/null || true)"
            [ -n "$path" ] || continue
            case "$path" in
                # Snap puts its launchers in /snap/bin (matched anywhere, so a
                # relocated or symlinked snap dir still counts).
                */snap/bin/*) kind=snap ;;
                *)            kind=native ;;
            esac
            case "$seen" in *" $family:$kind "*) continue ;; esac
            seen="$seen$family:$kind "
            printf '%s\t%s\t%s\n' "$family" "$kind" "$(browser_label "$family")"
        done
        if command -v flatpak >/dev/null 2>&1 \
           && flatpak info "$(flatpak_app "$family")" >/dev/null 2>&1; then
            case "$seen" in
                *" $family:flatpak "*) ;;
                *) seen="$seen$family:flatpak "
                   printf '%s\t%s\t%s\n' "$family" flatpak "$(browser_label "$family")" ;;
            esac
        fi
    done
}

# detect_browsers narrowed to what the user asked for: auto, chrome, brave,
# chromium, or a comma separated list of those. Unknown names are an error, not
# a silent no-op, because "installed nothing" reads like success.
filter_browsers() {
    local spec="${1:-auto}" want="" item rest family kind label keep
    case "$spec" in
        auto|all) want="$BROWSER_FAMILIES" ;;
        *)
            rest="$spec,"
            while [ -n "$rest" ]; do
                item="${rest%%,*}"
                rest="${rest#*,}"
                [ -n "$item" ] || continue
                if ! browser_label "$item" >/dev/null; then
                    printf 'unknown browser: %s (try chrome, brave, chromium or auto)\n' \
                        "$item" >&2
                    return 2
                fi
                want="$want $item"
            done
            ;;
    esac
    detect_browsers | while IFS=$'\t' read -r family kind label; do
        for keep in $want; do
            if [ "$keep" = "$family" ]; then
                printf '%s\t%s\t%s\n' "$family" "$kind" "$label"
                break
            fi
        done
    done
}

# True when at least one browser matches the request. install.sh uses this to
# say "no Chrome on PATH" out loud rather than installing silently and leaving
# the user to wonder why nothing changed.
has_browsers() {
    local found
    found="$(filter_browsers "${1:-auto}")" || return 2
    [ -n "$found" ]
}
