#!/usr/bin/env sh

set -eu

cd "$(dirname "$0")"

# Load local secrets for the API (for example OLLAMA_API_KEY). This file is git-ignored.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# Push DB schema so SQLite tables exist before the API starts.
pnpm db:push

exec pnpm dev
