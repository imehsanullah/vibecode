# Start everything
docker compose up -d

# Get your public tunnel URL
docker compose logs cloudflared | grep -o 'https://.*\.trycloudflare\.com'

- Using this URL and LITELLM_MASTER_KEY from .env in claude-code and cursor