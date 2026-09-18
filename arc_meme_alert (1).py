#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arc 체인 밈코인 텔레그램 알림 봇
--------------------------------
- GeckoTerminal(무료 API)에서 Arc 체인의 신규 풀을 15분마다 조회
- DEX 상장 후 30분 이내인 풀만 대상으로, 필터(유동성, 거래량/유동성 비율, 구매자 수,
  고점 대비 가격, 허니팟 의심) 통과 시 텔레그램으로 1회 알림
- 가능한 경우에만 부가 정보 첨부: 홀더 수, 상위10 집중도, 텔레그램 방 인원,
  X 계정/언급 수(X API 키가 있을 때만), DexScreener 교차검증 및 유료 부스트 여부

실행:
    pip install requests
    export TG_BOT_TOKEN="123456:ABC..."     (윈도우: set TG_BOT_TOKEN=...)
    export TG_CHAT_ID="-1001234567890"
    python arc_meme_alert.py --test          # 텔레그램 연결 테스트
    python arc_meme_alert.py                 # 상시 실행 (PC/서버)
    python arc_meme_alert.py --once          # 1회만 조회 후 종료 (GitHub Actions 예약 실행용)
"""

import os
import sys
import json
import time
import html
import logging
from datetime import datetime, timezone

import requests

# ───────────────────────── 설정 ─────────────────────────
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")      # 선택: X API(유료) 키가 있으면 언급 수 조회

GT_NETWORK = os.getenv("GT_NETWORK", "arc")            # GeckoTerminal 네트워크 키
DS_CHAIN = os.getenv("DS_CHAIN", "arc")                # DexScreener 체인 ID

POLL_SECONDS = 900          # 조회 주기 (15분)
NEW_POOL_MAX_PAGES = 10     # 신규 풀 최대 페이지 (페이지당 20개, API 상한 10페이지)
MAX_ENRICH_PER_CYCLE = 10   # 사이클당 상세 조회할 후보 수 (무료 API 호출 한도 보호)
GT_CALL_GAP = 2.5           # GeckoTerminal 호출 간격(초) — 무료 한도 분당 약 30회

# 필터 — DEX 상장 후 30분 이내 초기 풀 전용 기준 (수치는 '상장 이후 누적' 의미)
MAX_AGE_MINUTES = 30        # 상장 후 이 시간 이내인 풀만 알림
AGE_GRACE_MINUTES = 3       # 15분 주기 오차 보정 (두 번째 조회 기회를 놓치지 않도록)
MIN_AGE_MINUTES = 0
MIN_LIQUIDITY_USD = 10_000
MIN_VOLUME_USD = 20_000     # 상장 이후 누적 거래량
MIN_VOL_LIQ_RATIO = 0.5     # 누적 거래량 / 유동성
MAX_VOL_LIQ_RATIO = 60      # 너무 높으면 워시트레이딩 의심
MIN_BUYERS = 50             # 상장 이후 고유 구매 지갑 수
ATH_PROXIMITY = 0.70        # 현재가가 상장 후 최고가의 70% 이상 (이미 덤핑된 코인 제외)
MIN_HOLDERS = 50            # 홀더 수 (데이터 없으면 통과시키고 '정보 없음' 표기)
TOP10_WARN_PCT = 40         # 상위10 지갑 비중 경고 기준 (LP/본딩커브 포함될 수 있어 탈락 아닌 경고)

SKIP_BASE_SYMBOLS = {"USDC", "USDT", "EURC", "DAI", "WETH", "ETH", "WBTC", "CBBTC", "WUSDC"}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arc_alert_state.json")
GT_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"accept": "application/json", "user-agent": "arc-meme-alert/1.0"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("arc")


# ───────────────────────── 유틸 ─────────────────────────
def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def money(x):
    x = fnum(x)
    if x >= 1e9:
        return f"${x/1e9:.2f}B"
    if x >= 1e6:
        return f"${x/1e6:.2f}M"
    if x >= 1e3:
        return f"${x/1e3:.1f}K"
    return f"${x:.0f}"


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"alerted": {}}


def save_state(state):
    # 30일 지난 기록 정리
    cutoff = time.time() - 30 * 86400
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


_last_gt_call = 0.0


def gt_get(path, params=None):
    """GeckoTerminal GET (호출 간격 유지 + 429 재시도)"""
    global _last_gt_call
    for attempt in range(3):
        wait = GT_CALL_GAP - (time.time() - _last_gt_call)
        if wait > 0:
            time.sleep(wait)
        _last_gt_call = time.time()
        try:
            r = requests.get(f"{GT_BASE}{path}", params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            log.warning("GT 요청 실패 %s: %s", path, e)
            time.sleep(5)
            continue
        if r.status_code == 429:
            log.warning("GT 호출 한도 초과 — 30초 대기")
            time.sleep(30)
            continue
        if r.status_code != 200:
            log.warning("GT %s → HTTP %s", path, r.status_code)
            return None
        return r.json()
    return None


# ───────────────────────── 후보 수집 ─────────────────────────
def parse_pool(item, tokens):
    a = item.get("attributes", {})
    rel = item.get("relationships", {})
    base_id = rel.get("base_token", {}).get("data", {}).get("id", "")
    base = tokens.get(base_id, {})
    tx24 = (a.get("transactions") or {}).get("h24") or {}
    tx1 = (a.get("transactions") or {}).get("h1") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    created = a.get("pool_created_at")
    age_h = None
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except ValueError:
            pass
    return {
        "pool": a.get("address", ""),
        "pool_name": a.get("name", ""),
        "dex": rel.get("dex", {}).get("data", {}).get("id", ""),
        "token": base.get("address") or base_id.split("_", 1)[-1],
        "symbol": base.get("symbol", ""),
        "name": base.get("name", ""),
        "price": fnum(a.get("base_token_price_usd")),
        "fdv": fnum(a.get("fdv_usd")),
        "mcap": fnum(a.get("market_cap_usd")),
        "liq": fnum(a.get("reserve_in_usd")),
        "vol24": fnum(vol.get("h24")),
        "vol1": fnum(vol.get("h1")),
        "chg1": fnum(chg.get("h1")),
        "chg24": fnum(chg.get("h24")),
        "buys24": int(fnum(tx24.get("buys"))),
        "sells24": int(fnum(tx24.get("sells"))),
        "buyers24": int(fnum(tx24.get("buyers"))),
        "buys1": int(fnum(tx1.get("buys"))),
        "age_h": age_h,
    }


def _collect(data, src, pools):
    """응답의 풀들을 pools 에 추가하고, 이 페이지에서 가장 오래된 풀의 나이(시간)를 반환."""
    tokens = {
        inc["id"]: inc.get("attributes", {})
        for inc in data.get("included", [])
        if inc.get("type") == "token"
    }
    oldest = 0.0
    for item in data.get("data", []):
        p = parse_pool(item, tokens)
        if p["age_h"] is not None:
            oldest = max(oldest, p["age_h"])
        if p["pool"] and p["pool"] not in pools:
            p["source"] = src
            pools[p["pool"]] = p
    return oldest


def fetch_candidates():
    pools = {}
    limit_h = (MAX_AGE_MINUTES + AGE_GRACE_MINUTES) / 60
    # 신규 풀: 최신순이므로 30분을 넘는 풀이 나오면 페이지 넘김 중단
    for page in range(1, NEW_POOL_MAX_PAGES + 1):
        data = gt_get(f"/networks/{GT_NETWORK}/new_pools", {"include": "base_token", "page": page})
        if not data or not data.get("data"):
            break
        if _collect(data, "new_pools", pools) > limit_h:
            break
    else:
        log.warning("신규 풀이 %d페이지를 넘음 — 일부 풀을 놓쳤을 수 있음", NEW_POOL_MAX_PAGES)
    # 트렌딩 1페이지: 신규 목록에서 밀려난 '이미 뜨거운' 초기 풀 보완
    data = gt_get(f"/networks/{GT_NETWORK}/trending_pools", {"include": "base_token", "page": 1})
    if data:
        _collect(data, "trending_pools", pools)
    return list(pools.values())


def basic_filter(p):
    """목록 데이터만으로 하는 1차 필터. (통과 여부, 탈락 사유)"""
    if p["symbol"].upper() in SKIP_BASE_SYMBOLS:
        return False, "스테이블/메이저"
    if p["age_h"] is None:
        return False, "생성시각 없음"
    age_m = p["age_h"] * 60
    if age_m < MIN_AGE_MINUTES:
        return False, "너무 신생"
    if age_m > MAX_AGE_MINUTES + AGE_GRACE_MINUTES:
        return False, "30분 경과"
    if p["liq"] < MIN_LIQUIDITY_USD:
        return False, "유동성 부족"
    if p["vol24"] < MIN_VOLUME_USD:
        return False, "거래량 부족"
    ratio = p["vol24"] / p["liq"] if p["liq"] else 0
    if ratio < MIN_VOL_LIQ_RATIO or ratio > MAX_VOL_LIQ_RATIO:
        return False, f"거래량/유동성 비율 {ratio:.1f}"
    if p["buyers24"] < MIN_BUYERS:
        return False, "구매자 수 부족"
    if p["buys24"] >= 30 and p["sells24"] < p["buys24"] * 0.05:
        return False, "매도 거의 없음(허니팟 의심)"
    return True, ""


# ───────────────────────── 상세 조회 ─────────────────────────
def get_token_info(token):
    data = gt_get(f"/networks/{GT_NETWORK}/tokens/{token}/info")
    return (data or {}).get("data", {}).get("attributes", {}) or {}


def get_ath_ratio(pool, price):
    data = gt_get(f"/networks/{GT_NETWORK}/pools/{pool}/ohlcv/minute", {"aggregate": 1, "limit": 120})
    rows = (data or {}).get("data", {}).get("attributes", {}).get("ohlcv_list") or []
    highs = [fnum(r[2]) for r in rows if len(r) >= 5]
    if not highs or not price:
        return None
    ath = max(highs)
    return price / ath if ath else None


def get_dexscreener(pool):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/pairs/{DS_CHAIN}/{pool}", headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            return {}
        pairs = r.json().get("pairs") or []
        return pairs[0] if pairs else {}
    except (requests.RequestException, ValueError):
        return {}


def get_tg_members(handle):
    """공개 텔레그램 방 인원 수. 실패하면 None."""
    if not handle or not TG_BOT_TOKEN:
        return None
    handle = handle.strip().lstrip("@").split("/")[-1]
    if not handle or handle.startswith("+"):
        return None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getChatMemberCount",
            params={"chat_id": f"@{handle}"},
            timeout=15,
        )
        j = r.json()
        return int(j["result"]) if j.get("ok") else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def get_x_mentions(token):
    """X API 키가 있을 때만: 최근 7일 컨트랙트 주소 언급 수(최대 100까지 집계)."""
    if not X_BEARER_TOKEN:
        return None
    try:
        r = requests.get(
            "https://api.x.com/2/tweets/search/recent",
            params={"query": f'"{token}" -is:retweet', "max_results": 100},
            headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return int(r.json().get("meta", {}).get("result_count", 0))
    except (requests.RequestException, ValueError):
        return None


def enrich(p):
    info = get_token_info(p["token"])
    holders = info.get("holders") or {}
    dist = holders.get("distribution_percentage") or {}
    p["holders"] = int(fnum(holders.get("count"))) or None
    p["top10"] = fnum(dist.get("top_10"), None) if dist.get("top_10") is not None else None
    p["gt_score"] = info.get("gt_score")
    p["twitter"] = info.get("twitter_handle")
    p["telegram"] = info.get("telegram_handle")
    p["websites"] = info.get("websites") or []
    p["honeypot"] = info.get("is_honeypot")
    p["ath_ratio"] = get_ath_ratio(p["pool"], p["price"])

    ds = get_dexscreener(p["pool"])
    p["ds_liq"] = fnum((ds.get("liquidity") or {}).get("usd")) or None
    p["ds_boosts"] = int(fnum((ds.get("boosts") or {}).get("active"))) if ds else 0
    if ds and not p["twitter"]:
        for s in (ds.get("info") or {}).get("socials") or []:
            url = s.get("url", "")
            if s.get("type") == "twitter" and url:
                p["twitter"] = url.rstrip("/").split("/")[-1]
            if s.get("type") == "telegram" and url and not p["telegram"]:
                p["telegram"] = url.rstrip("/").split("/")[-1]

    p["tg_members"] = get_tg_members(p["telegram"])
    p["x_mentions"] = get_x_mentions(p["token"])
    return p


def enriched_filter(p):
    if str(p.get("honeypot")).lower() in ("yes", "true"):
        return False, "허니팟 판정"
    if p.get("holders") is not None and p["holders"] < MIN_HOLDERS:
        return False, f"홀더 {p['holders']}명"
    if p.get("ath_ratio") is not None and p["ath_ratio"] < ATH_PROXIMITY:
        return False, f"고점 대비 {p['ath_ratio']*100:.0f}%"
    return True, ""


# ───────────────────────── 텔레그램 ─────────────────────────
def build_message(p):
    e = html.escape
    ratio = p["vol24"] / p["liq"] if p["liq"] else 0
    age = p["age_h"]
    age_txt = f"{age*60:.0f}분" if age < 1 else f"{age:.1f}시간"

    lines = [
        f"🟢 <b>Arc 신규 상장: {e(p['name'] or p['pool_name'])} (${e(p['symbol'])})</b>",
        f"<code>{e(p['token'])}</code>",
        "",
        f"• 시총/FDV: {money(p['mcap'] or p['fdv'])} | 누적 거래량: {money(p['vol24'])}",
        f"• 유동성: {money(p['liq'])} | 거래량/유동성: {ratio:.1f}x",
        f"• 상장 후 가격변동: {p['chg24']:+.1f}%",
        f"• 매수 {p['buys24']} / 매도 {p['sells24']} | 구매지갑 {p['buyers24']}",
        f"• 상장 후 경과: {age_txt} | DEX: {e(p['dex'])} | 포착: {e(p.get('source',''))}",
    ]
    if p.get("ath_ratio") is not None:
        lines.append(f"• 상장 후 최고가 대비: {p['ath_ratio']*100:.0f}%")

    # 커뮤니티 — 있는 정보만
    comm = []
    if p.get("holders"):
        comm.append(f"홀더 {p['holders']:,}명")
    if p.get("tg_members"):
        comm.append(f"텔레그램 {p['tg_members']:,}명")
    if p.get("x_mentions") is not None:
        n = p["x_mentions"]
        comm.append(f"X 7일 언급 {'100+' if n >= 100 else n}건")
    if p.get("gt_score"):
        comm.append(f"GT점수 {fnum(p['gt_score']):.0f}")
    if comm:
        lines.append("• 커뮤니티: " + " | ".join(comm))

    # 경고
    warns = []
    if p.get("top10") is not None and p["top10"] > TOP10_WARN_PCT:
        warns.append(f"상위10 지갑 {p['top10']:.0f}% (LP·런치패드 주소 포함 가능)")
    if p.get("ds_boosts"):
        warns.append(f"DexScreener 유료 부스트 {p['ds_boosts']}개 (노출 구매)")
    if p.get("ds_liq") and p["liq"]:
        hi, lo = max(p["ds_liq"], p["liq"]), min(p["ds_liq"], p["liq"])
        if lo and hi / lo > 2:
            warns.append(f"유동성 수치 불일치: GT {money(p['liq'])} vs DS {money(p['ds_liq'])}")
    if not p.get("twitter") and not p.get("telegram"):
        warns.append("등록된 소셜 없음")
    if str(p.get("honeypot")).lower() == "unknown":
        warns.append("허니팟 여부 미확인")
    if warns:
        lines.append("")
        lines.append("⚠️ " + "\n⚠️ ".join(e(w) for w in warns))

    # 링크
    links = [
        f'<a href="https://dexscreener.com/{DS_CHAIN}/{p["pool"]}">DexScreener</a>',
        f'<a href="https://www.geckoterminal.com/{GT_NETWORK}/pools/{p["pool"]}">GeckoTerminal</a>',
        f'<a href="https://x.com/search?q={p["token"]}&amp;f=live">X 검색(CA)</a>',
    ]
    if p.get("twitter"):
        links.append(f'<a href="https://x.com/{e(p["twitter"])}">X 계정</a>')
    if p.get("telegram"):
        links.append(f'<a href="https://t.me/{e(p["telegram"])}">텔레그램</a>')
    lines.append("")
    lines.append(" · ".join(links))
    lines.append("")
    lines.append("※ 상장 30분 이내 초기 풀 — 러그 위험 높음. LP 락/소각, 컨트랙트 검증 직접 확인 필요")
    return "\n".join(lines)


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.error("TG_BOT_TOKEN / TG_CHAT_ID 가 설정되지 않았습니다.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": False,   # 소리/푸시 알림 켬
            },
            timeout=20,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            log.error("텔레그램 전송 실패: %s", r.text[:300])
        return bool(ok)
    except requests.RequestException as e:
        log.error("텔레그램 전송 오류: %s", e)
        return False


# ───────────────────────── 메인 루프 ─────────────────────────
def run_cycle(state):
    candidates = fetch_candidates()
    log.info("조회된 풀 %d개", len(candidates))
    passed = []
    for p in candidates:
        if p["pool"] in state["alerted"]:
            continue
        ok, _ = basic_filter(p)
        if ok:
            passed.append(p)
    # 구매지갑 수가 많은 순으로 우선 상세조회
    passed.sort(key=lambda x: x["buyers24"], reverse=True)
    log.info("1차 통과 %d개", len(passed))

    sent = 0
    for p in passed[:MAX_ENRICH_PER_CYCLE]:
        enrich(p)
        ok, why = enriched_filter(p)
        if not ok:
            log.info("탈락 %s: %s", p["symbol"], why)
            continue
        if send_telegram(build_message(p)):
            state["alerted"][p["pool"]] = time.time()
            save_state(state)
            sent += 1
            log.info("알림 전송: %s (%s)", p["symbol"], p["token"])
    return sent


def main():
    if "--test" in sys.argv:
        ok = send_telegram("✅ Arc 밈코인 알림 봇 연결 테스트 — 이 메시지가 소리와 함께 왔다면 정상입니다.")
        sys.exit(0 if ok else 1)

    if "--once" in sys.argv:
        if not TG_BOT_TOKEN or not TG_CHAT_ID:
            log.error("TG_BOT_TOKEN / TG_CHAT_ID 가 설정되지 않았습니다.")
            sys.exit(1)
        state = load_state()
        try:
            run_cycle(state)
        except Exception:
            log.exception("사이클 오류")
        sys.exit(0)

    state = load_state()
    log.info("시작 — 네트워크=%s, 주기=%ds", GT_NETWORK, POLL_SECONDS)
    while True:
        started = time.time()
        try:
            run_cycle(state)
        except Exception:  # 루프가 죽지 않도록
            log.exception("사이클 오류")
        time.sleep(max(5, POLL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
