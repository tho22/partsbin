#!/usr/bin/env bash
# Build the committed HEAD on the homelab docker host and redeploy the Portainer stack.
#
# Usage: deploy/deploy.sh
# Settings come from the environment or deploy/deploy.env (not committed, see deploy/deploy.env.example):
#   DOCKER_HOST_SSH, PORTAINER_URL, PORTAINER_STACK_ID, PORTAINER_ENDPOINT_ID, PORTAINER_TOKEN_FILE
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
[[ -r deploy/deploy.env ]] && . deploy/deploy.env

: "${DOCKER_HOST_SSH:?set DOCKER_HOST_SSH, e.g. root@docker-host (see deploy/deploy.env.example)}"
: "${PORTAINER_URL:?set PORTAINER_URL, e.g. https://docker-host:9443}"
: "${PORTAINER_STACK_ID:?set PORTAINER_STACK_ID}"
PORTAINER_ENDPOINT_ID="${PORTAINER_ENDPOINT_ID:-1}"
PORTAINER_TOKEN_FILE="${PORTAINER_TOKEN_FILE:-$HOME/.portainer_token}"
REMOTE_DIR=/opt/partsbin
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree has uncommitted changes; only HEAD is deployed." >&2
fi
rev="$(git rev-parse --short HEAD)"

echo "==> Building $rev on $DOCKER_HOST_SSH"
git archive --format=tar HEAD | ssh "$DOCKER_HOST_SSH" \
  "rm -rf $REMOTE_DIR.new && mkdir -p $REMOTE_DIR.new && tar -x -C $REMOTE_DIR.new \
   && echo $rev > $REMOTE_DIR.new/REVISION \
   && rm -rf $REMOTE_DIR && mv $REMOTE_DIR.new $REMOTE_DIR \
   && docker build -q -t partsbin:latest -t partsbin:$rev $REMOTE_DIR"

echo "==> Redeploying Portainer stack $PORTAINER_STACK_ID"
PORTAINER_URL="$PORTAINER_URL" PORTAINER_STACK_ID="$PORTAINER_STACK_ID" \
PORTAINER_ENDPOINT_ID="$PORTAINER_ENDPOINT_ID" PORTAINER_TOKEN_FILE="$PORTAINER_TOKEN_FILE" \
python3 - <<'EOF'
import json, os, ssl, urllib.request

base, sid, eid = os.environ["PORTAINER_URL"], os.environ["PORTAINER_STACK_ID"], os.environ["PORTAINER_ENDPOINT_ID"]
token = open(os.path.expanduser(os.environ["PORTAINER_TOKEN_FILE"])).read().strip()
ctx = ssl._create_unverified_context()  # Portainer uses a self-signed certificate


def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, headers={"X-API-Key": token, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, context=ctx, timeout=300) as r:
        return json.load(r)


stack = call("GET", f"/api/stacks/{sid}")
content = open("deploy/portainer-stack.yml").read()
# Keep the environment configured in Portainer; only the compose file comes from the repo.
call("PUT", f"/api/stacks/{sid}?endpointId={eid}",
     {"stackFileContent": content, "env": stack.get("Env") or [], "prune": True, "pullImage": False})
print("stack updated")
EOF

echo "==> Waiting for health check"
ssh "$DOCKER_HOST_SSH" 'for i in $(seq 1 30); do
  s=$(docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{end}}" partsbin 2>/dev/null)
  [ "$s" = healthy ] && { echo "partsbin is healthy ($(cat /opt/partsbin/REVISION))"; exit 0; }
  sleep 3
done; echo "partsbin did not become healthy" >&2; docker logs --tail 30 partsbin; exit 1'
