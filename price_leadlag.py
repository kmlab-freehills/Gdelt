"""
記事の需要シグナルと価格の先行・遅行を調べる（予備分析、DBは読み取りのみ）

品目ごとに、採用された記事（狭いクエリ）から日次のシグナルを作り、価格の日次リターンとの相関を
ずらし幅（lag）ごとに出す。lag > 0 は「記事が価格より先」（t日のシグナルと t+lag 日のリターン）、
lag < 0 は「価格が記事より先」。価格は Yahoo Finance の日次終値（yfinance）を使う。
記事の日付は GDELT の観測日（UTC）。週末・休日の記事は次の取引日に寄せる。

シグナル:
  - n_kept   : 採用記事の件数
  - net_tone : (強気 - 弱気) / 採用記事の件数（記事がない日は0）
  - net_star : ★で重み付けした (強気 - 弱気)

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor sh -c \
    "pip install -q yfinance && python price_leadlag.py --version demand-v2"
"""

import argparse
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf
from sqlalchemy import text

from database import SessionLocal

# 品目 → Yahoo Finance のティッカー（先物がないものはETFで代用）
TICKERS = {
    "copper": "HG=F",       # COMEX 銅先物
    "gold": "GC=F",         # COMEX 金先物
    "natural gas": "NG=F",  # NYMEX 天然ガス先物
    "uranium": "URA",       # ウラン関連株ETF（現物価格の代用）
    "rare earth": "REMX",   # レアアース・戦略金属ETF（代用）
}
LAGS = range(-5, 6)

SIGNAL_SQL = """
SELECT target, publish_date, llm_analysis->>'tone' AS tone, (llm_analysis->>'rating')::int AS rating
FROM articles
WHERE collection_mode = 'analyze' AND target = ANY(:targets) AND llm_analysis->>'prompt_version' = :version
  AND NOT (llm_analysis->>'excluded')::boolean AND publish_date >= :since
"""


def daily_signal(rows: pd.DataFrame, trading_days: pd.DatetimeIndex) -> pd.DataFrame:
    """記事を取引日に寄せて日次のシグナルにする（休日の記事は次の取引日へ）。"""
    days = pd.to_datetime(rows["publish_date"]).dt.normalize()
    pos = trading_days.searchsorted(days)
    rows = rows.assign(day=[trading_days[p] if p < len(trading_days) else pd.NaT for p in pos]).dropna(subset=["day"])
    sign = rows["tone"].map({"bullish": 1, "bearish": -1}).fillna(0)
    rows = rows.assign(sign=sign, star=sign * rows["rating"].fillna(1))
    g = rows.groupby("day")
    out = pd.DataFrame({"n_kept": g.size(), "bull_minus_bear": g["sign"].sum(), "net_star": g["star"].sum()})
    out = out.reindex(trading_days, fill_value=0)
    out["net_tone"] = np.where(out["n_kept"] > 0, out["bull_minus_bear"] / out["n_kept"].replace(0, 1), 0.0)
    return out


def lag_corr(signal: pd.Series, ret: pd.Series) -> dict:
    result = {}
    for lag in LAGS:
        pair = pd.concat([signal, ret.shift(-lag)], axis=1).dropna()
        if len(pair) >= 10 and pair.iloc[:, 0].std() > 0 and pair.iloc[:, 1].std() > 0:
            r = float(np.corrcoef(pair.iloc[:, 0], pair.iloc[:, 1])[0, 1])
            result[lag] = {"r": round(r, 3), "n": len(pair)}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="記事の需要シグナルと価格の先行・遅行を調べる")
    parser.add_argument("--version", default="demand-v2", help="使う判定のプロンプトの版")
    parser.add_argument("--since", default="2026-08-25", help="対象期間の開始日")
    parser.add_argument("--out", default="eval_results/price_leadlag", help="出力先ディレクトリの親")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        rows = pd.DataFrame(session.execute(text(SIGNAL_SQL), {"targets": list(TICKERS), "version": args.version,
                                                               "since": args.since}).mappings().all())
    finally:
        session.close()

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    summary = {"version": args.version, "since": args.since, "tickers": TICKERS, "targets": {}}
    for target, ticker in TICKERS.items():
        prices = yf.download(ticker, start=args.since, progress=False, auto_adjust=True)["Close"].squeeze().dropna()
        prices.index = pd.to_datetime(prices.index).tz_localize(None).normalize()
        ret = np.log(prices).diff().rename("ret")
        sig = daily_signal(rows[rows["target"] == target], prices.index)
        frame = sig.join(ret).join(prices.rename("close"))
        frame.to_csv(os.path.join(out_dir, f"{target.replace(' ', '_')}.csv"))
        summary["targets"][target] = {
            "trading_days": len(prices),
            "articles": int(sig["n_kept"].sum()),
            "price_change_pct": round(100 * (prices.iloc[-1] / prices.iloc[0] - 1), 1),
            "lag_corr": {name: lag_corr(sig[name], ret) for name in ("n_kept", "net_tone", "net_star")},
            # 参考: 記事の件数が価格の大きな動き（絶対値）とどうずれるか
            "lag_corr_abs_ret": lag_corr(sig["n_kept"], ret.abs()),
        }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
