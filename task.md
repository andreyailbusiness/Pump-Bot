## Instarding bot - текущий статус

### Готово
- `bot/` базовая реализация paper‑trading
- Публичные данные MEXC: топ‑символы по 24h объёму + 1h свечи
- Индикаторы: EMA, ADX, ATR, Bollinger
- Стратегия: EMA200 тренд + ADX/ATR фильтры + breakout Bollinger + pullback к SMA20
- SL/TP (1:3) + трейлинг‑стоп (1.5 ATR активация / 0.5 ATR дистанция)
- Cooldown по символу: 48h после закрытия
- FastAPI: `/api/state`, `/api/positions`, `/api/trades` + `ui/index.html`
- `render.yaml`, `Dockerfile`, `requirements.txt`, `.env.example`

### Дальше
- Backtester (`bot/backtester.py`) по 1h данным за 12 месяцев и KPI (ROI, winrate, MDD, Sharpe)
- Live‑trading (подписанные ордера) после paper‑прогона
- Уточнить точные эндпоинты и режим торговли (spot vs futures) под ваши ключи/плечо

