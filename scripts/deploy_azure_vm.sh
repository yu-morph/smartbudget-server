#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    printf 'Usage: %s APP_DIR COMMIT_SHA SOURCE_REPOSITORY\n' "$0" >&2
    exit 2
fi

app_dir=$1
commit_sha=$2
source_repository=$3

if [[ ! $commit_sha =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Invalid commit SHA: expected 40 lowercase hexadecimal characters.\n' >&2
    exit 2
fi

if [[ ! $source_repository =~ ^[[:alnum:]][[:alnum:]_.-]*/[[:alnum:]][[:alnum:]_.-]*$ ]]; then
    printf 'Invalid GitHub repository: expected OWNER/REPOSITORY.\n' >&2
    exit 2
fi

if [[ $app_dir != /* ]]; then
    printf 'Application directory must be an absolute path.\n' >&2
    exit 2
fi

if [[ ! -d $app_dir/.git || ! -f $app_dir/docker-compose.yml ]]; then
    printf 'Application directory must be a Git checkout with docker-compose.yml.\n' >&2
    exit 2
fi

git_cmd=(git -c "safe.directory=$app_dir" -C "$app_dir")
status_output=$("${git_cmd[@]}" status --porcelain --untracked-files=no)
if [[ -n $status_output ]]; then
    printf 'Application checkout has tracked changes; refusing to deploy.\n' >&2
    exit 2
fi

"${git_cmd[@]}" fetch --no-tags "https://github.com/${source_repository}.git" main
main_sha=$("${git_cmd[@]}" rev-parse --verify 'FETCH_HEAD^{commit}')
"${git_cmd[@]}" cat-file -e "$commit_sha^{commit}"
if ! "${git_cmd[@]}" merge-base --is-ancestor "$commit_sha" "$main_sha"; then
    printf 'Commit SHA is not reachable from origin/main.\n' >&2
    exit 2
fi

"${git_cmd[@]}" checkout --detach "$commit_sha"
build_timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sudo env BUILD_VERSION="$commit_sha" BUILD_TIMESTAMP="$build_timestamp" \
    docker compose --project-directory "$app_dir" \
    up -d --build --pull never --no-deps --wait --wait-timeout 120 smartbudget-server

printf 'DEPLOYED_SHA=%s\n' "$commit_sha"
