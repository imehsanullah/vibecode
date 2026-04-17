# Start everything

```
docker compose up -d
```

# Get your public tunnel URL

```
docker compose logs cloudflared | grep -o 'https://.*\.trycloudflare\.com'
```

- Using this URL and LITELLM_MASTER_KEY from .env in claude-code and cursor

# CLIProxyAPI-backed Codex models


`client -> cursor-shim (Docker :4000) -> LiteLLM (Docker :4001) -> CLIProxyAPI service (Docker :8317)`

The compose includes:

- `cliproxyapi` for Codex OAuth-backed upstream access
- `litellm` as the internal gateway on port `4001`
- `cursor-shim` as the public entrypoint on port `4000`

For `cpa-*` models, the shim detects Cursor's buggy case where a Responses-style body is posted to `/chat/completions`, and reroutes that request to LiteLLM's `/v1/responses` upstream without flattening the tool protocol. All other requests pass through unchanged.

LiteLLM reaches CLIProxyAPI over the Docker network at:

`http://cliproxyapi:8317/v1`

## Start CLIProxyAPI

Start the whole stack:

```bash
docker compose up -d
```

Then authenticate Codex OAuth against the running `cliproxyapi` container:

```bash
docker compose exec cliproxyapi /CLIProxyAPI/CLIProxyAPI --codex-login
```

If you need a URL printed in the terminal instead of opening a browser:

```bash
docker compose exec cliproxyapi /CLIProxyAPI/CLIProxyAPI --codex-login --no-browser
```

The compose file publishes:

- `8317:8317` for the CLIProxyAPI server
- `1455:1455` for the Codex OAuth callback

CLIProxyAPI persists its config and OAuth state under:

- `./cliproxyapi/config.yaml`
- `./cliproxyapi/auth/`


## Restart services after config changes

```bash
docker compose up -d --build --force-recreate cliproxyapi litellm cursor-shim
```

## Test through LiteLLM

Chat Completions:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cpa-openai-gpt-5.4(medium)",
    "messages": [
      {"role": "user", "content": "Say hello in one sentence."}
    ]
  }'
```

Responses API:

```bash
curl http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cpa-openai-gpt-5.4(medium)",
    "input": "Return the word ready."
  }'
```

CLIProxyAPI documents Codex as an OpenAI-compatible `responses` provider, and LiteLLM supports both `/v1/chat/completions` and `/v1/responses`, so either endpoint can be exposed via LiteLLM depending on the client.

## Troubleshooting

If you see this LiteLLM error:

```text
Router.acompletion() missing 1 required positional argument: 'messages'
```

the request shape does not match the endpoint you called.

For Cursor BYOK with `Override OpenAI Base URL`, this repository now includes a shim that detects Responses-style requests sent to `/chat/completions` for routed model families and forwards them to LiteLLM's `/v1/responses` upstream instead of trying to flatten them into Chat Completions.

The default routed families are:

- `cpa-openai-*`
- `cliproxyapi-*`
- `copilot-gpt-5*`

This preserves:

- `input`
- `previous_response_id`
- Responses-style tool definitions
- Responses-style output items / tool-loop state
- reasoning and structured-output fields

For Cursor compatibility, the shim then translates the upstream Responses result back into Chat Completions format before returning it on the original `/chat/completions` route.

For CLIProxyAPI specifically, the shim also strips optional fields that its `/v1/responses` implementation currently rejects. At the moment this includes:

- `metadata`

Use these pairings when you test outside Cursor:

- `/v1/chat/completions` with a `messages` array
- `/v1/responses` with an `input` field

Examples:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cliproxyapi-gpt-5.4",
    "messages": [
      {"role": "user", "content": "Say hello in one sentence."}
    ]
  }'
```

```bash
curl http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cliproxyapi-gpt-5.4",
    "input": "Say hello in one sentence."
  }'
```

If you are using Codex itself against LiteLLM, configure Codex to use the Responses API surface, not Chat Completions:

```toml
model_provider = "litellm"
model = "cliproxyapi-gpt-5.4"

[model_providers.litellm]
name = "litellm"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
```

I use the following functions in my bashrc

```
use_litellm_cursor() {
  local DB="$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
  local JSON_KEY="src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser"
  local LITTELM_KEY="..."
  local LITELLM_DIR="$HOME/vibecode/litellm"

  pkill -x Cursor || true
  sleep 1

  if [ "$1" = "reset" ]; then
    sqlite3 "$DB" "UPDATE ItemTable SET value = json_remove(value, '$.openAIBaseUrl') WHERE key = '$JSON_KEY';"
    sqlite3 "$DB" "UPDATE ItemTable SET value = '' WHERE key = 'cursorAuth/openAIKey';"
    echo "✅ Reset to native Cursor models"
  else
    :  

    local LITTELM_URL=""
    local attempt=0
    while [ "$attempt" -lt 90 ] && [ -z "$LITTELM_URL" ]; do
      sleep 1
      LITTELM_URL=$(cd "$LITELLM_DIR" && docker compose logs cloudflared 2>/dev/null | grep -o 'https://.*\.trycloudflare\.com' | tail -1)
      attempt=$((attempt + 1))
    done

    if [ -z "$LITTELM_URL" ]; then
      echo "❌ Could not read trycloudflare URL from cloudflared logs (timed out after ${attempt}s)"
      return 1
    fi

    echo "🌐 Litellm proxy URL: $LITTELM_URL"

    sqlite3 "$DB" "UPDATE ItemTable SET value = json_set(value, '$.openAIBaseUrl', '$LITTELM_URL') WHERE key = '$JSON_KEY';"
    sqlite3 "$DB" "UPDATE ItemTable SET value = '$LITTELM_KEY' WHERE key = 'cursorAuth/openAIKey';"
    echo "✅ Switched to Litellm Proxy (base URL set to printed URL above)"
  fi
  open "cursor://command/workbench.action.reloadWindow"

}

use_litellm_claude() {
  if [ "${1:-}" = "reset" ] || [ "${1:-}" = "unset" ]; then
    unset LITELLM_API_KEY
    unset OPENROUTER_API_KEY
    unset ANTHROPIC_API_KEY
    unset ANTHROPIC_AUTH_TOKEN
    unset ANTHROPIC_BASE_URL
    unset ANTHROPIC_API_URL
    unset ANTHROPIC_MODEL
    unset ANTHROPIC_CUSTOM_MODEL_OPTION
    unset ANTHROPIC_CUSTOM_MODEL_OPTION_NAME
    unset ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION
    unset ANTHROPIC_DEFAULT_OPUS_MODEL
    unset ANTHROPIC_DEFAULT_SONNET_MODEL
    unset ANTHROPIC_DEFAULT_HAIKU_MODEL
    unset CLAUDE_CODE_SUBAGENT_MODEL
    unset NO_PROXY
    unset DISABLE_TELEMETRY
    unset DISABLE_COST_WARNINGS
    unset API_TIMEOUT_MS

    echo "CCR/LiteLLM environment cleared for this shell."
    echo "Now run: claude"
    echo "Then inside Claude Code run: /model default"
    return 0
  fi

  local LITELLM_KEY="..."
  local LITELLM_DIR="$HOME/vibecode/litellm"

  local LITTELM_URL=""
  local attempt=0
  while [ "$attempt" -lt 90 ] && [ -z "$LITTELM_URL" ]; do
    sleep 1
    LITTELM_URL=$(cd "$LITELLM_DIR" && docker compose logs cloudflared 2>/dev/null | grep -o 'https://.*\.trycloudflare\.com' | tail -1)
    attempt=$((attempt + 1))
  done

  if [ -z "$LITTELM_URL" ]; then
    echo "❌ Could not read trycloudflare URL from cloudflared logs (timed out after ${attempt}s)"
    return 1
  fi

  echo "🌐 LiteLLM proxy URL: $LITTELM_URL"

  local models=(
    ali-qwen3.5-plus
    ali-qwen3-max-2026-01-23
    ali-qwen3-coder-next
    ali-qwen3-coder-plus
    ali-glm-5
    ali-glm-4.7
    ali-kimi-k2.5
    ali-MiniMax-M2.5
    or-minimax-m2.7
    or-minimax-m2.5
    or-kimi-k2.5
    or-glm-5
    or-glm-4.7
    or-nemotron-120b-free
    or-step-3.5-flash-free
    or-mimo-v2-pro
    or-qwen3.6-plus
    or-qwen3.5-plus
    or-qwen3.5-397b
    or-qwen3-coder-next
    or-qwen3.5-flash-free
    or-z-ai/glm-5.1
    glm-5.1:cloud
    minimax-m2.7:cloud
    gemma4:31b-cloud
    qwen3.5:397b-cloud
    kimi-k2.5:cloud
    cliproxyapi-gpt-5.4
    cliproxyapi-gpt-5.2-codex
    cliproxyapi-gpt-5.1-codex-max
    cliproxyapi-gpt-5.4-mini
    cliproxyapi-gpt-5.3-codex
    cliproxyapi-gpt-5.2
    cliproxyapi-gpt-5.1-codex-mini
    cliproxyapi-codex-gpt-5
    cliproxyapi-codex-gpt-5-codex
  )

  local model=""
  if [ -n "${1:-}" ]; then
    model="$1"
  else
    echo "Select a LiteLLM model_name (or Ctrl-C to cancel):"
    select model in "${models[@]}"; do
      if [ -n "$model" ]; then
        break
      fi
      echo "Invalid selection."
    done
  fi

  if [ -z "$model" ]; then
    echo "No model selected."
    return 1
  fi

  export LITELLM_API_KEY="$LITELLM_KEY"

  mkdir -p "$HOME/.claude-code-router"
  cat > "$HOME/.claude-code-router/config.json" <<EOF
{
  "LOG": true,
  "LITELLM_API_KEY": "\${LITELLM_API_KEY}",
  "Providers": [
    {
      "name": "litellm",
      "api_base_url": "${LITTELM_URL}/v1/chat/completions",
      "api_key": "\${LITELLM_API_KEY}",
      "models": ["$model"],
      "transformer": { "use": ["openai"] }
    }
  ],
  "Router": {
    "default": "litellm,$model"
  }
}
EOF

  ccr restart >/dev/null 2>&1 || ccr start
  eval "$(ccr activate)"

  export ANTHROPIC_MODEL="litellm,$model"

  echo "Claude Code Router is configured for LiteLLM (trycloudflare)."
  echo "Base: ${LITTELM_URL}/v1"
  echo "Model: $model"
}



```
