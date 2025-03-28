import ccxt

exchange = ccxt.binance({
    'options': {
        'defaultType': 'swap',  # Perpetual Futures
        'adjustForTimeDifference': True
    }
})

exchange.load_markets()

print("\n🔍 Binance Perpetual Futures Sembolleri:")
for symbol, market in exchange.markets.items():
    if market['swap']:  # Sadece Perpetual kontratları göster
        print(f"- {symbol}")