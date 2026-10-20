#!/usr/bin/env bash
set -euo pipefail
provider=${1:?Usage: scripts/cli_login.sh codex|claude}
case "$provider" in
  codex) login_args=(flock --exclusive /auth/.codex-refresh.lock codex login --device-auth); config_name=CODEX_HOME;;
  claude) login_args=(claude auth login); config_name=CLAUDE_CONFIG_DIR;;
  *) echo 'Choose codex or claude.' >&2; exit 1;;
esac
runner_image=${CLI_RUNNER_IMAGE:-auto-reviewer-cli:local}
auth_volume="auto-reviewer_${provider}-auth"
docker volume create "$auth_volume" >/dev/null
docker run --rm --network=none --user=0:0 --read-only --mount "type=volume,source=$auth_volume,target=/auth" --entrypoint=node "$runner_image" -e 'const fs=require("fs");fs.chownSync("/auth",1000,1000);fs.chmodSync("/auth",0o700);'
exec docker run --rm -it --user=1000:1000 --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 --mount "type=volume,source=$auth_volume,target=/auth" --env HOME=/tmp --env "$config_name=/auth" --entrypoint /usr/bin/env "$runner_image" "${login_args[@]}"
