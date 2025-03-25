import ccxt
import pandas as pd
import schedule
import time
import sqlite3
from datetime import datetime, timedelta
from telegram import Bot
from telegram.error import TelegramError
from config import *

# Veritabanı Bağlantısı
conn = sqlite3.connect('fvg_tracker.db')
c = conn.cursor()

# Tabloları Oluştur
c.execute('''CREATE TABLE IF NOT EXISTS fvgs
             (symbol TEXT, fvg_type TEXT, price_level REAL, 
              created_at TEXT, expiry TEXT, trade_id INTEGER PRIMARY KEY)''')

c.execute('''CREATE TABLE IF NOT EXISTS trades
             (trade_id INTEGER PRIMARY KEY,
              symbol TEXT,
              direction TEXT,
              entry_price REAL,
              tp_price REAL,
              sl_price REAL,
              status TEXT DEFAULT 'open',
              result TEXT,
              opened_at TEXT,
              closed_at TEXT)''')
conn.commit()

# Telegram Bot
bot = Bot(token=TELEGRAM_TOKEN)
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

#-------------------------- ORTAK FONKSİYONLAR --------------------------#
def send_telegram(message):
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except TelegramError as e:
        print(f"Telegram Hatası: {e}")

def detect_fvg(df):
    fvgs = []
    for i in range(2, len(df)):
        prev = df.iloc[i-2]
        middle = df.iloc[i-1]
        next = df.iloc[i]

        # Body boyutu kontrolü (en az %70)
        body_size = abs(middle['close'] - middle['open'])
        if body_size < (middle['high'] - middle['low']) * 0.7:
            continue

        # Mum gövdelerinin örtüşmemesi
        prev_body = sorted([prev['open'], prev['close']])
        next_body = sorted([next['open'], next['close']])
        middle_body = sorted([middle['open'], middle['close']])

        if (prev_body[1] > middle_body[0]) or (next_body[0] < middle_body[1]):
            continue

        # FVG Hesaplama
        trend = 'bullish' if middle['close'] > middle['open'] else 'bearish'
        if trend == 'bullish':
            gap_low = prev['high']
            gap_high = next['low']
        else:
            gap_low = prev['low']
            gap_high = next['high']

        if gap_low >= gap_high:
            fvg_price = (gap_low + gap_high) / 2
            fvgs.append({'price': fvg_price, 'trend': trend})
    return fvgs

#-------------------------- 4H FVG TESPİTİ --------------------------#
def check_4h_fvg():
    for symbol in SYMBOLS:
        try:
            # 4H Mum Verileri
            ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # FVG Tespiti
            fvgs = detect_fvg(df)
            for fvg in fvgs:
                expiry_time = (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S')
                
                # Veritabanına Kaydet
                c.execute('INSERT INTO fvgs (symbol, fvg_type, price_level, created_at, expiry) VALUES (?,?,?,?,?)',
                         (symbol, fvg['trend'], fvg['price'], datetime.now().strftime('%Y-%m-%d %H:%M:%S'), expiry_time))
                conn.commit()
                
                # Telegram Bildirimi
                send_telegram(
                    f"🚨 4H FVG Tespit Edildi\n"
                    f"🔷 {symbol} {fvg['trend'].upper()}\n"
                    f"🎯 Fiyat: {fvg['price']:.2f}\n"
                    f"🆔 ID: {c.lastrowid}"
                )
        except Exception as e:
            print(f"Hata ({symbol}): {str(e)}")

#-------------------------- 15M FVG ve İŞLEM TETİKLEME --------------------------#
def check_15m_fvg():
    c.execute("SELECT * FROM fvgs WHERE expiry > datetime('now')")
    active_fvgs = c.fetchall()
    
    for fvg in active_fvgs:
        trade_id, symbol, trend, price_level, created_at, expiry = fvg
        try:
            # 15M Mum Verileri
            ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=50)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            current_price = df.iloc[-1]['close']
            
            # Fiyat FVG Seviyesinde mi? (±%0.5)
            if not (price_level * 0.995 <= current_price <= price_level * 1.005):
                continue

            # 15M'de Aynı Trendde FVG Ara
            fvgs_15m = detect_fvg(df)
            for f in fvgs_15m:
                if f['trend'] == trend:
                    # İşlem Parametreleri
                    entry_price = current_price
                    sl = entry_price * 0.987 if trend == 'bullish' else entry_price * 1.013
                    tp = entry_price + 3*(entry_price - sl) if trend == 'bullish' else entry_price - 3*(sl - entry_price)
                    
                    # İşlemi Veritabanına Kaydet
                    c.execute('''INSERT INTO trades 
                              (trade_id, symbol, direction, entry_price, tp_price, sl_price, opened_at)
                              VALUES (?,?,?,?,?,?,?)''',
                              (trade_id, symbol, trend, entry_price, tp, sl, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                    conn.commit()
                    
                    # Telegram Bildirimi
                    send_telegram(
                        f"🎯 İŞLEM AÇILDI #{trade_id}\n"
                        f"🔷 {symbol} {trend.upper()}\n"
                        f"🔹 Giriş: {entry_price:.2f}\n"
                        f"🎯 TP: {tp:.2f}\n"
                        f"🛑 SL: {sl:.2f}"
                    )
                    
                    # FVG'yi Arşivle
                    c.execute("DELETE FROM fvgs WHERE trade_id=?", (trade_id,))
                    conn.commit()
                    break
        except Exception as e:
            print(f"Hata ({symbol}): {str(e)}")

#-------------------------- TP/SL TAKİP ve WİNRATE --------------------------#
def check_trade_results():
    # Açık İşlemleri Al
    c.execute("SELECT * FROM trades WHERE status='open'")
    open_trades = c.fetchall()
    
    for trade in open_trades:
        trade_id, symbol, direction, entry, tp, sl, _, _, opened_at, _ = trade
        
        # Anlık Fiyat
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
        except:
            continue
        
        # Sonuç Kontrolü
        result = None
        if direction == 'bullish':
            if current_price >= tp:
                result = 'TP'
            elif current_price <= sl:
                result = 'SL'
        else:
            if current_price <= tp:
                result = 'TP'
            elif current_price >= sl:
                result = 'SL'
        
        if result:
            # İşlemi Kapat
            c.execute('''UPDATE trades 
                      SET status='closed', result=?, closed_at=?
                      WHERE trade_id=?''',
                      (result, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), trade_id))
            conn.commit()
            
            # Winrate Hesapla
            c.execute("SELECT COUNT(*) FROM trades WHERE result='TP'")
            tp_count = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM trades")
            total_trades = c.fetchone()[0]
            winrate = (tp_count / total_trades * 100) if total_trades > 0 else 0
            
            # Telegram Bildirimi
            send_telegram(
                f"🔔 İŞLEM SONUÇLANDI #{trade_id}\n"
                f"🔷 {symbol} {direction.upper()}\n"
                f"🔹 Sonuç: {result}\n"
                f"📈 Winrate: {winrate:.1f}%"
            )

#-------------------------- ZAMANLAYICI --------------------------#
schedule.every(15).minutes.do(check_4h_fvg)
schedule.every(5).minutes.do(check_15m_fvg)
schedule.every(1).minutes.do(check_trade_results)

if __name__ == "__main__":
    print("🤖 Bot aktif! CTRL+C ile durdur.")
    while True:
        schedule.run_pending()
        time.sleep(1)