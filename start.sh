#!/bin/bash

# This script sets up the webhook and starts the bot and web server.

# Set the Telegram Bot Token from environment variables
TOKEN=${TELEGRAM_TOKEN}
WEBHOOK_URL=${RENDER_EXTERNAL_URL} # Render provides this URL

echo "Starting bot and web server..."

# Set the webhook for the bot
# This command tells Telegram where to send updates
curl -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
     -H "Content-Type: application/json" \
     -d "{\"url\": \"${WEBHOOK_URL}/${TOKEN}\"}"

# Start the Gunicorn web server to run the Flask app
# The bot.py will run the webhook listener in the same process
# We run web.py here to serve the mini-app's HTML and API
gunicorn web:app --bind 0.0.0.0:${PORT} --workers 4 &
python bot.py
