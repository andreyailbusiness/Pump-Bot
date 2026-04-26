# Instarding Trading Bot

This repository contains a premium‑grade cryptocurrency trading bot for the influencer **Инстардинг**. The bot runs 24/7 on MEXC, trades the top‑100 symbols by volume, uses a low‑frequency high‑win‑rate strategy, and provides a web dashboard and Telegram notifications.

## Features
- Daily refresh of top‑100 symbols
- EMA‑200 + ADX + Bollinger Bands entry logic
- 1 % risk per trade, max drawdown 8 %
- Optional cross‑margin leverage up to 20×
- Docker + Render free‑tier deployment (always‑on)
- Backtester for historical performance analysis
- Real‑time dashboard (`ui/index.html`)

## Quick start
```bash
# Clone repo, create .env, then:
docker build -t instarding-bot .
docker run -d --restart unless-stopped \
  -e MEXC_API_KEY=... -e MEXC_SECRET=... \
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=... \
  instarding-bot
```

See `README.md` in the repository for full setup instructions.
