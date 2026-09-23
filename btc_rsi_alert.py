"""
BTCUSDT 무기한 선물 4시간봉 RSI 알림 (시세: Hyperliquid/OKX/Coinbase/Kraken)
- RSI(14) ≤ 25 로 들어가면 🟢 알림
- RSI(14) ≥ 85 로 들어가면 🔴 알림
- 구간에 머무는 동안은 다시 알리지 않고, 벗어났다가 다시 들어오면 알림
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "4h")
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_LOW = float(os.getenv("RSI_LOW", "25"))
RSI_HIGH = float(os.getenv("RSI_HIGH", "85"))
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
KST = timezone(timedelta(hours=9))
SEC = {"1h": 3600, "4h": 14400, "1d": 86400}[INTERVAL]
COIN = SYMBOL.replace("USDT", "").replace("USD", "")


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "rsi-alert"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def candles_hyperliquid():
    end = int(time.time() * 1000)
    body = json.dumps({"type": "candleSnapshot", "req": {
        "coin": COIN, "interval": INTERVAL, "startTime": end - SEC * 1000 * 300, "endTime": end}}).encode()
    data = http("https://api.hyperliquid.xyz/info", body,
                {"Content-Type": "application/json", "User-Agent": "rsi-alert"})
    return sorted((int(c["t"]), float(c["c"])) for c in data)


def candles_okx():
    bar = {"1h": "1H", "4h": "4H", "1d": "1D"}[INTERVAL]
    url = f"https://www.okx.com/api/v5/market/candles?instId={COIN}-USDT-SWAP&bar={bar}&limit=300"
    return sorted((int(x[0]), float(x[4])) for x in http(url)["data"])


def candles_coinbase():
    url = f"https://api.exchange.coinbase.com/products/{COIN}-USD/candles?granularity={SEC}"
    return sorted((int(x[0]) * 1000, float(x[4])) for x in http(url))


def candles_kraken():
    pair = "XBTUSD" if COIN == "BTC" else f"{COIN}USD"
    data = http(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={SEC // 60}")
    rows = next(v for k, v in data["result"].items() if k != "last")
    return sorted((int(x[0]) * 1000, float(x[4])) for x in rows)


def get_candles():
    for fn in (candles_hyperliquid, candles_okx, candles_coinbase, candles_kraken):
        try:
            c = fn()
            if len(c) > RSI_LEN + 5:
                return c, fn.__name__.replace("candles_", "")
        except Exception as e:
            print(f"{fn.__name__} 실패: {e}")
    raise RuntimeError("시세를 가져오지 못했습니다")


def rsi(closes, n):
    g, l = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g.append(max(d, 0.0)); l.append(max(-d, 0.0))
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    v = lambda a, b: 100.0 if b == 0 else 100.0 - 100.0 / (1.0 + a / b)
    out = [None] * n + [v(ag, al)]
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n; al = (al * (n - 1) + l[i]) / n
        out.append(v(ag, al))
    return out


def send(text):
    http(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
         json.dumps({"chat_id": CHAT_ID, "text": text}).encode(), {"Content-Type": "application/json"})


def main():
    candles, source = get_candles()
    closed = candles[:-1]
    closes = [c[1] for c in closed]
    r = rsi(closes, RSI_LEN)
    prev, cur = r[-2], r[-1]
    price = closes[-1]
    t = datetime.fromtimestamp(closed[-1][0] / 1000, KST).strftime("%m/%d %H:%M")
    head = f"{SYMBOL} {INTERVAL} 봉 마감 {t} KST\n현재가 ${price:,.0f} / RSI {cur:.1f} (직전 {prev:.1f})"

    low_in = cur <= RSI_LOW and prev > RSI_LOW
    high_in = cur >= RSI_HIGH and prev < RSI_HIGH

    if low_in:
        send("🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢\n"
             f"🟢 매수 대기 — RSI {RSI_LOW:.0f} 이하 (과매도)\n"
             f"{head}\n"
             "🟢 롱 진입 타점 확인\n"
             "🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢")
    elif high_in:
        send("🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
             f"🔴 숏 대기 — RSI {RSI_HIGH:.0f} 이상 (과매수)\n"
             f"{head}\n"
             "🔴 숏 진입 타점 확인\n"
             "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴")
    elif TEST_MODE:
        send(f"✅ 테스트 — 봇 정상 작동 (시세: {source})\n{head}\n신호 없음")
    print(f"[{source}] {head} | low={low_in} high={high_in}")


if __name__ == "__main__":
    main()
