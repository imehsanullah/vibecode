#!/usr/bin/env bash
# One-time GitHub Copilot Chat OAuth for LiteLLM.
#   github_copilot/access-token   — GitHub OAuth access token
#   github_copilot/api-key.json   — JSON from GET /copilot_internal/v2/token

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_DIR="${SCRIPT_DIR}/github_copilot"
mkdir -p "$TOKEN_DIR"

CLIENT_ID="Iv1.b507a08c87ecfe98"

echo "Requesting GitHub device code..."
RESP=$(curl -sS -X POST "https://github.com/login/device/code" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d "{\"client_id\":\"${CLIENT_ID}\",\"scope\":\"read:user\"}")

DEVICE_CODE=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['device_code'])")
USER_CODE=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['user_code'])")
VERIFY_URI=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['verification_uri'])")

echo ""
echo "  Open: ${VERIFY_URI}"
echo "  Code: ${USER_CODE}"
echo ""
echo "Waiting for you to authorize (up to 5 minutes)..."
echo ""

ACCESS_TOKEN=""
for _ in $(seq 1 60); do
  TOKEN_RESP=$(curl -sS -X POST "https://github.com/login/oauth/access_token" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d "{\"client_id\":\"${CLIENT_ID}\",\"device_code\":\"${DEVICE_CODE}\",\"grant_type\":\"urn:ietf:params:oauth:grant-type:device_code\"}")

  if echo "$TOKEN_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'access_token' in d else 1)" 2>/dev/null; then
    ACCESS_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
    break
  fi

  ERROR=$(echo "$TOKEN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null || echo "")
  if [ -n "$ERROR" ] && [ "$ERROR" != "authorization_pending" ] && [ "$ERROR" != "slow_down" ]; then
    echo "OAuth error: $TOKEN_RESP" >&2
    exit 1
  fi
  sleep 5
done

if [ -z "${ACCESS_TOKEN}" ]; then
  echo "Timed out waiting for authorization." >&2
  exit 1
fi

printf '%s' "$ACCESS_TOKEN" > "${TOKEN_DIR}/access-token"

# Same headers as LiteLLM litellm/llms/github_copilot/authenticator.py _get_github_headers.
COPILOT_JSON=$(curl -sS --compressed "https://api.github.com/copilot_internal/v2/token" \
  -H "accept: application/json" \
  -H "content-type: application/json" \
  -H "editor-version: vscode/1.85.1" \
  -H "editor-plugin-version: copilot/1.155.0" \
  -H "user-agent: GithubCopilot/1.155.0" \
  -H "authorization: token ${ACCESS_TOKEN}")

echo "$COPILOT_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); \
  assert 'token' in d, ('Unexpected response (need active GitHub Copilot subscription?):', d)"

printf '%s\n' "$COPILOT_JSON" > "${TOKEN_DIR}/api-key.json"

echo ""
echo "OK — wrote:"
echo "  ${TOKEN_DIR}/access-token"
echo "  ${TOKEN_DIR}/api-key.json"
echo ""
echo "Start or restart the proxy (e.g. bash ./restart.sh) so the volume picks up these files."