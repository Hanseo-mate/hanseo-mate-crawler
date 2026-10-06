#!/usr/bin/env bash

# Run with: sudo bash /opt/hanseo-mate/crawler/source/scripts/deploy.sh
# Keep deployment inside a function so git can update this file while it runs.
main() {
    set -Eeuo pipefail

    local source_dir="${CRAWLER_SOURCE_DIR:-/opt/hanseo-mate/crawler/source}"
    local service_name="${CRAWLER_SERVICE_NAME:-hanseo-crawler}"
    local branch="main"
    local health_url="${CRAWLER_HEALTH_URL:-http://127.0.0.1:8000/health}"
    local service_user working_dir current_branch changes previous_commit new_commit
    local python_bin response attempt

    if [[ "${1:-}" == "--help" ]]; then
        printf 'Usage: sudo bash scripts/deploy.sh\n'
        printf 'Defaults: %s, service=%s, branch=%s\n' "$source_dir" "$service_name" "$branch"
        return 0
    fi
    if (( $# != 0 )); then
        printf 'Unknown arguments. Use --help.\n' >&2
        return 1
    fi
    if (( EUID != 0 )); then
        printf 'Run this script with sudo.\n' >&2
        return 1
    fi

    for command in git systemctl runuser curl readlink; do
        if ! command -v "$command" >/dev/null 2>&1; then
            printf 'Required command is missing: %s\n' "$command" >&2
            return 1
        fi
    done

    # Prevent two deployments from installing packages or restarting together.
    if ! command -v flock >/dev/null 2>&1; then
        printf 'Required command is missing: flock (util-linux).\n' >&2
        return 1
    fi
    exec 9>/run/lock/hanseo-crawler-deploy.lock
    if ! flock -n 9; then
        printf 'Another crawler deployment is already running.\n' >&2
        return 1
    fi

    deployment_error() {
        local exit_code="$1"
        printf '\nDeployment failed. Check the error above.\n' >&2
        printf 'Inspect systemctl status and journalctl for the configured crawler service.\n' >&2
        exit "$exit_code"
    }
    trap 'deployment_error "$?"' ERR

    if [[ ! -d "$source_dir" ]]; then
        printf 'Source directory does not exist: %s\n' "$source_dir" >&2
        return 1
    fi
    source_dir="$(readlink -f "$source_dir")"
    if [[ "$(systemctl show "$service_name" --property=LoadState --value)" != "loaded" ]]; then
        printf 'The systemd service is not installed: %s\n' "$service_name" >&2
        return 1
    fi
    working_dir="$(systemctl show "$service_name" --property=WorkingDirectory --value)"
    service_user="$(systemctl show "$service_name" --property=User --value)"
    if [[ -z "$working_dir" || "$(readlink -f "$working_dir")" != "$source_dir" ]]; then
        printf 'Service WorkingDirectory does not match %s. Inspect systemctl cat %s.\n' "$source_dir" "$service_name" >&2
        return 1
    fi
    if [[ -z "$service_user" || "$service_user" == "root" ]]; then
        printf 'Set a non-root User in the systemd service before deploying.\n' >&2
        return 1
    fi

    as_service_user() {
        runuser -u "$service_user" -- "$@"
    }
    cd "$source_dir"
    if [[ "$(as_service_user git rev-parse --show-toplevel)" != "$source_dir" ]]; then
        printf 'Source directory is not the root of its Git repository.\n' >&2
        return 1
    fi
    current_branch="$(as_service_user git branch --show-current)"
    if [[ "$current_branch" != "$branch" ]]; then
        printf 'Expected branch %s, found %s. No branch was changed.\n' "$branch" "$current_branch" >&2
        return 1
    fi
    changes="$(as_service_user git status --porcelain --untracked-files=normal)"
    if [[ -n "$changes" ]]; then
        printf 'Server checkout has local changes. Preserve them before deploying:\n%s\n' "$changes" >&2
        return 1
    fi

    previous_commit="$(as_service_user git rev-parse HEAD)"
    printf '[1/5] Fetching origin/%s (current %s)\n' "$branch" "${previous_commit:0:12}"
    as_service_user git fetch origin "refs/heads/$branch:refs/remotes/origin/$branch"
    if ! as_service_user git merge-base --is-ancestor HEAD "origin/$branch"; then
        printf 'Server commits differ from origin/%s. No reset or overwrite was performed.\n' "$branch" >&2
        return 1
    fi
    as_service_user git merge --ff-only "origin/$branch"
    new_commit="$(as_service_user git rev-parse HEAD)"

    printf '[2/5] Installing dependencies and checking the application\n'
    python_bin="$source_dir/.venv/bin/python"
    if [[ ! -x "$python_bin" ]]; then
        as_service_user python3 -m venv "$source_dir/.venv"
    fi
    as_service_user "$python_bin" -m pip install -r requirements.txt
    as_service_user "$python_bin" -m pip check
    as_service_user "$python_bin" -c 'from api_main import app; print("Application import OK:", app.title)'

    printf '[3/5] Creating/verifying the crawl progress table (existing menus untouched)\n'
    as_service_user "$python_bin" -m crawler.cafeteria.migrate

    printf '[4/5] Restarting %s\n' "$service_name"
    systemctl restart "$service_name"

    printf '[5/5] Checking service and local health endpoint\n'
    for attempt in {1..15}; do
        if systemctl is-active --quiet "$service_name"; then
            if response="$(curl --fail --silent --show-error --connect-timeout 2 --max-time 3 "$health_url" 2>/dev/null)"; then
                if printf '%s' "$response" | "$python_bin" -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("status") == "ok" else 1)' 2>/dev/null; then
                    printf '\nDeployment complete: %s -> %s\n' "${previous_commit:0:12}" "${new_commit:0:12}"
                    printf 'Service: active; health: ok\n'
                    printf 'This confirms API startup. Scheduled crawling and DB writes require separate verification.\n'
                    return 0
                fi
            fi
        fi
        sleep 1
    done
    printf 'Service did not become healthy.\n' >&2
    systemctl status "$service_name" --no-pager --full || true
    journalctl -u "$service_name" -n 40 --no-pager || true
    return 1
}

# Both commands are parsed before main runs, including when git updates this script.
main "$@"; exit "$?"
