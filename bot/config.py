from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="dev", alias="APP_ENV")
    trading_mode: str = Field(default="paper", alias="TRADING_MODE")  # paper|live (live not implemented)

    mexc_base_url: str = Field(default="https://api.mexc.com", alias="MEXC_BASE_URL")
    quote_asset: str = Field(default="USDT", alias="QUOTE_ASSET")
    market_type: str = Field(default="futures", alias="MARKET_TYPE")  # futures|spot

    top_symbols_limit: int = Field(default=150, alias="TOP_SYMBOLS_LIMIT")
    state_path: str = Field(default="data/state.json", alias="STATE_PATH")
    top_symbols_cache_path: str = Field(default="data/top_symbols.json", alias="TOP_SYMBOLS_CACHE_PATH")
    state_backup_enabled: bool = Field(default=True, alias="STATE_BACKUP_ENABLED")
    state_backup_interval_sec: int = Field(default=900, alias="STATE_BACKUP_INTERVAL_SEC")
    state_backup_dir: str = Field(default="data/backups", alias="STATE_BACKUP_DIR")
    state_backup_keep: int = Field(default=96, alias="STATE_BACKUP_KEEP")
    github_state_sync_enabled: bool = Field(default=False, alias="GITHUB_STATE_SYNC_ENABLED")
    github_state_repo: str | None = Field(default=None, alias="GITHUB_STATE_REPO")  # owner/repo
    github_state_file_path: str = Field(default="data/render_state.json", alias="GITHUB_STATE_FILE_PATH")
    github_state_branch: str = Field(default="main", alias="GITHUB_STATE_BRANCH")
    github_state_token: str | None = Field(default=None, alias="GITHUB_STATE_TOKEN")
    github_state_timeout_sec: int = Field(default=15, alias="GITHUB_STATE_TIMEOUT_SEC")
    github_state_sync_interval_sec: int = Field(default=300, alias="GITHUB_STATE_SYNC_INTERVAL_SEC")
    github_state_commit_message: str = Field(default="chore(state): sync bot runtime state", alias="GITHUB_STATE_COMMIT_MESSAGE")

    timeframe: str = Field(default="1h", alias="TIMEFRAME")
    candles_limit: int = Field(default=300, alias="CANDLES_LIMIT")

    risk_percent: float = Field(default=0.01, alias="RISK_PERCENT")
    risk_percent_strong: float = Field(default=0.015, alias="RISK_PERCENT_STRONG")
    risk_percent_neutral: float = Field(default=0.01, alias="RISK_PERCENT_NEUTRAL")
    risk_percent_weak: float = Field(default=0.005, alias="RISK_PERCENT_WEAK")
    max_drawdown: float = Field(default=0.08, alias="MAX_DRAWDOWN")
    leverage: int = Field(default=1, alias="LEVERAGE")

    adx_period: int = Field(default=14, alias="ADX_PERIOD")
    adx_threshold: float = Field(default=25.0, alias="ADX_THRESHOLD")
    trend_min_adx: float = Field(default=34.0, alias="TREND_MIN_ADX")
    trend_min_move_24h: float = Field(default=0.08, alias="TREND_MIN_MOVE_24H")
    min_breadth_count: int = Field(default=6, alias="MIN_BREADTH_COUNT")
    neutral_min_breadth_count: int = Field(default=3, alias="NEUTRAL_MIN_BREADTH_COUNT")
    breadth_probe_symbols: int = Field(default=30, alias="BREADTH_PROBE_SYMBOLS")
    atr_period: int = Field(default=14, alias="ATR_PERIOD")
    atr_min_pct: float = Field(default=0.005, alias="ATR_MIN_PCT")
    boll_period: int = Field(default=20, alias="BOLL_PERIOD")
    boll_std: float = Field(default=2.0, alias="BOLL_STD")
    strategy_entry_mode: str = Field(default="pump", alias="STRATEGY_ENTRY_MODE")
    strategy_entry_mode_strong: str = Field(default="pump", alias="STRATEGY_ENTRY_MODE_STRONG")
    strategy_entry_mode_neutral: str = Field(default="hybrid", alias="STRATEGY_ENTRY_MODE_NEUTRAL")
    strategy_entry_mode_weak: str = Field(default="hybrid", alias="STRATEGY_ENTRY_MODE_WEAK")
    weak_disable_impulse_entries: bool = Field(default=True, alias="WEAK_DISABLE_IMPULSE_ENTRIES")
    pump_lookback: int = Field(default=6, alias="PUMP_LOOKBACK")
    pump_min_ret_1h: float = Field(default=0.02, alias="PUMP_MIN_RET_1H")
    pump_volume_mult: float = Field(default=1.8, alias="PUMP_VOLUME_MULT")
    pump_close_pos_min: float = Field(default=0.65, alias="PUMP_CLOSE_POS_MIN")
    pump_max_overext_atr: float = Field(default=1.0, alias="PUMP_MAX_OVEREXT_ATR")
    pump_max_opp_wick_ratio: float = Field(default=1.2, alias="PUMP_MAX_OPP_WICK_RATIO")
    pump_short_min_ret_1h: float = Field(default=0.018, alias="PUMP_SHORT_MIN_RET_1H")
    pump_short_volume_mult: float = Field(default=1.8, alias="PUMP_SHORT_VOLUME_MULT")
    pump_short_close_pos_min: float = Field(default=0.65, alias="PUMP_SHORT_CLOSE_POS_MIN")
    pump_short_max_overext_atr: float = Field(default=1.4, alias="PUMP_SHORT_MAX_OVEREXT_ATR")
    pump_short_max_opp_wick_ratio: float = Field(default=1.0, alias="PUMP_SHORT_MAX_OPP_WICK_RATIO")

    cooldown_hours: int = Field(default=48, alias="COOLDOWN_HOURS")
    loss_streak_block_threshold: int = Field(default=2, alias="LOSS_STREAK_BLOCK_THRESHOLD")
    loss_streak_block_hours: int = Field(default=72, alias="LOSS_STREAK_BLOCK_HOURS")

    # Backtest / execution cost model (futures)
    futures_taker_fee_rate: float = Field(default=0.0004, alias="FUTURES_TAKER_FEE_RATE")
    slippage_bps: float = Field(default=2.0, alias="SLIPPAGE_BPS")  # 2 bps = 0.02%

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    # Render sets PORT for web services; keep API_PORT for local runs.
    port: int | None = Field(default=None, alias="PORT")


def get_settings() -> Settings:
    return Settings()

