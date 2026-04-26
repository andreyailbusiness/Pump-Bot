import asyncio
import pandas as pd
from bot.config import get_settings
from bot.exchange import MexcClient
from bot.symbols import get_top_symbols_by_quote_volume, filter_hot_symbols

async def main():
    s = get_settings()
    client = MexcClient(base_url=s.mexc_base_url)
    print("--- Momentum Scanner Test ---")
    print(f"1. Fetching Top {s.top_symbols_limit} symbols by volume...")
    
    from bot.symbols import TopSymbolsCache
    cache = TopSymbolsCache(s.top_symbols_cache_path)
    
    raw_symbols = get_top_symbols_by_quote_volume(
        client, 
        quote_asset=s.quote_asset, 
        limit=s.top_symbols_limit, 
        cache=cache,
        refresh_every=pd.Timedelta(seconds=1) # Force refresh for test
    )
    print(f"Total raw symbols: {len(raw_symbols)}")
    
    print("2. Filtering for HOT symbols (ADX > 20, ATR% > 0.5%)...")
    hot_symbols = await filter_hot_symbols(
        client,
        raw_symbols,
        interval=s.timeframe,
        limit=20 # Show top 20 for test
    )
    
    print("\nTOP 20 HOT SYMBOLS RIGHT NOW:")
    for i, sym in enumerate(hot_symbols, 1):
        print(f"{i}. {sym}")
        
    if "RAVE_USDT" in hot_symbols:
        print("\nAlert: RAVE is still in the hot list.")
    else:
        print("\nSuccess: RAVE is filtered out (No momentum/volatility).")

if __name__ == "__main__":
    asyncio.run(main())
