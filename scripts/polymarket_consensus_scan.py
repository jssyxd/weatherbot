#!/usr/bin/env python3
"""Polymarket temperature-market consensus scanner (Gamma events based).

Every 30 minutes: for every contract city's current-day high/low temperature
event, pull live prices for ALL buckets from the Gamma events endpoint (one
request per event), derive the market consensus, and detect consensus
BREAKOUTS:
  - high direction: the MODE bucket (most-consensus high value) moves UP by at
    least one bucket;
  - low direction:  the MODE bucket (most-consensus low value) moves DOWN by at
    least one bucket.

Breakouts are reported IMMEDIATELY on any same-day direction move (no
stability-wait): a single METAR/SPECI observation that crosses a bucket
boundary makes the crossed buckets factually dead, so the alert fires at once.
The previous mode's stable duration is still recorded for information, but it
is never a gate.

Secondary metric: the dead zone.
  - high: contiguous run of excluded buckets (YES prob < 0.10) from the COLD
    end; boundary = hi of the last dead bucket -> market consensus H >= X.
  - low:  contiguous run of excluded buckets from the WARM end; boundary = lo
    of the last dead bucket -> observed low is locked below X (L < X).
  (Excluded buckets on the opposite end are forecast-driven, not observation-
  locked, so they are intentionally not part of the boundary.)

Also cross-checks against weatherbot's own candidate-signal records so the
user can see whether the market broke out BEFORE our METAR-based signal fired
(a missed BUY_NO opportunity) or after.
"""
from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE = "/home/da/weatherbot"
STATE = f"{BASE}/data/state.json"
SIGNAL_DIR = f"{BASE}/data/signals"
CONSENSUS_STATE = f"{BASE}/data/consensus_state.json"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
EXCLUDED_P_YES_THRESHOLD = 0.10
# Breakout gate: none. Any same-day direction move of the consensus mode is a
# breakout (a fresh METAR/SPECI observation makes crossed buckets factually
# dead immediately). stable_minutes is informational only.
FALLBACK_PROXY = "http://192.168.1.5:7890"


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def bucket_anchor(lo, hi) -> float | None:
    """Representative temperature of a bucket: lo, or hi-1 for bottom tails."""
    if lo is not None:
        return float(lo)
    if hi is not None:
        return float(hi) - 1.0
    return None


def load_rules() -> list[dict]:
    state = json.load(open(STATE))
    rules = state.get("market_rules", [])
    best: dict[str, dict] = {}
    for r in rules:
        if not r.get("enabled", True) or not r.get("buckets"):
            continue
        key = f"{r.get('city_id')}|{r.get('direction')}"
        cur = best.get(key)
        if cur is None or str(r.get("market_local_date", "")) > str(cur.get("market_local_date", "")):
            best[key] = r
    return list(best.values())


def fetch_event(event_id: str) -> dict | None:
    url = f"{GAMMA_EVENTS}?id={event_id}"
    for use_proxy in (False, True):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": FALLBACK_PROXY, "https": FALLBACK_PROXY})) if use_proxy else urllib.request.build_opener()
            with opener.open(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ev = data[0] if isinstance(data, list) and data else data
            return ev or None
        except Exception:
            continue
    return None


def scan_consensus(rules: list[dict]) -> tuple[list[dict], int]:
    events: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_event, str(r["event_id"])): r for r in rules}
        for fut in as_completed(futs):
            events[str(futs[fut]["event_id"])] = fut.result()
    failures = 0
    out = []
    for rule in rules:
        city, direction, unit = rule["city_id"], rule["direction"], rule["market_unit"]
        ev = events.get(str(rule["event_id"]))
        if not ev:
            failures += 1
            out.append({"city": city, "direction": direction, "unit": unit, "date": rule.get("market_local_date"),
                        "mode_anchor": None, "mode_label": None, "dead_zone": None,
                        "excluded_count": 0, "error": "event_fetch_failed"})
            continue
        by_market = {m.get("id"): m for m in (ev.get("markets") or [])}
        prob_rows = []
        for b in rule.get("buckets", []):
            gm = by_market.get(str(b.get("market_id")))
            lo, hi = b.get("lo"), b.get("hi")
            p_yes = None
            if gm:
                op = gm.get("outcomePrices")
                try:
                    p_yes = float(json.loads(op)[0]) if op else None
                except Exception:
                    bb, ba = gm.get("bestBid"), gm.get("bestAsk")
                    p_yes = round(1.0 - (float(bb) + float(ba)) / 2.0, 4) if isinstance(bb, (int, float)) and isinstance(ba, (int, float)) else None
            prob_rows.append({"label": b.get("label"), "lo": lo, "hi": hi, "anchor": bucket_anchor(lo, hi), "p_yes": p_yes})
        live = [b for b in prob_rows if b["p_yes"] is not None]
        excluded = [b for b in live if b["p_yes"] < EXCLUDED_P_YES_THRESHOLD and b["anchor"] is not None]

        # dead-zone boundary (direction-correct semantics): the contiguous run
        # MUST start from the extreme end (coldest for high, warmest for low);
        # otherwise there is no observation-locked dead zone.
        dead_zone = None
        if direction == "high":
            ordered = sorted(prob_rows, key=lambda x: (x["lo"] if x["lo"] is not None else -1e9))
            first = ordered[0] if ordered else None
            if first and first["p_yes"] is not None and first["p_yes"] < EXCLUDED_P_YES_THRESHOLD and first["hi"] is not None:
                run = []
                for b in ordered:
                    if b["p_yes"] is not None and b["p_yes"] < EXCLUDED_P_YES_THRESHOLD and b["hi"] is not None:
                        run.append(b)
                    else:
                        break
                if run:
                    dead_zone = run[-1]["hi"]
        else:  # low: warm-end locked boundary
            ordered = sorted(prob_rows, key=lambda x: (x["lo"] if x["lo"] is not None else -1e9), reverse=True)
            first = ordered[0] if ordered else None
            if first and first["p_yes"] is not None and first["p_yes"] < EXCLUDED_P_YES_THRESHOLD and first["lo"] is not None:
                run = []
                for b in ordered:
                    if b["p_yes"] is not None and b["p_yes"] < EXCLUDED_P_YES_THRESHOLD and b["lo"] is not None:
                        run.append(b)
                    else:
                        break
                if run:
                    dead_zone = run[-1]["lo"]

        # mode: most-consensus bucket
        mode_anchor, mode_label = None, None
        if live:
            m = max(live, key=lambda x: x["p_yes"])
            mode_anchor, mode_label = m["anchor"], m["label"]
        out.append({"city": city, "direction": direction, "unit": unit, "date": rule.get("market_local_date"),
                    "mode_anchor": mode_anchor, "mode_label": mode_label, "dead_zone": dead_zone,
                    "excluded_count": len(excluded), "error": None})
    return out, failures


def cross_check(city: str, direction: str) -> str:
    import glob
    hits = []
    for sf in sorted(glob.glob(f"{SIGNAL_DIR}/*.jsonl"))[-3:]:
        try:
            with open(sf, errors="replace") as f:
                for line in f:
                    rec = json.loads(line)
                    if rec.get("signal_type") != "candidate_no_signal":
                        continue
                    if rec.get("city_id") != city or rec.get("direction") != direction:
                        continue
                    ex = rec.get("execution", {})
                    hits.append({
                        "event_time": (rec.get("event_id") or "").split("|")[2] if len((rec.get("event_id") or "").split("|")) >= 3 else "?",
                        "bucket": (rec.get("bucket") or {}).get("label", "?"),
                        "exec": ex.get("status", "?"),
                    })
        except Exception:
            continue
    if not hits:
        return "  (我们无该方向候选信号记录)"
    lines = ["  (我们最近的记录:"]
    for h in hits[-3:]:
        lines.append(f"    {h['event_time']} {h['bucket']} -> {h['exec']}")
    lines.append("  )")
    return "\n".join(lines)


def main() -> None:
    rules = load_rules()
    scan, failures = scan_consensus(rules)
    prev = {}
    if os.path.exists(CONSENSUS_STATE):
        try:
            prev = json.load(open(CONSENSUS_STATE))
        except Exception:
            prev = {}
    now_utc = utc_now_str()
    now_dt = datetime.now(timezone.utc)
    new_state: dict = {}
    breakouts = []
    lines = []
    lines.append(f"consensus_scan_time_utc: {now_utc}")
    lines.append(f"markets_scanned: {len(scan)} (city x direction), event_failures: {failures}")
    for s in sorted(scan, key=lambda x: (x["city"], x["direction"])):
        key = f"{s['city']}|{s['direction']}"
        val = s["mode_anchor"]
        p = prev.get(key)
        if val is not None:
            if p and p.get("value") == val and p.get("date") == s["date"]:
                first = p.get("first_seen_utc", now_utc)
                stable_min = round((now_dt - _parse_time(first)).total_seconds() / 60.0, 1)
                new_state[key] = {"value": val, "mode": s["mode_label"], "dead_zone": s["dead_zone"],
                                  "date": s["date"], "first_seen_utc": first, "last_seen_utc": now_utc}
            else:
                new_state[key] = {"value": val, "mode": s["mode_label"], "dead_zone": s["dead_zone"],
                                  "date": s["date"], "first_seen_utc": now_utc, "last_seen_utc": now_utc}
                stable_min = 0.0
            if p and p.get("value") is not None and p["value"] != val and p.get("date") == s["date"]:
                # same contract day: evaluate breakout only within the same date
                try:
                    stable_before = round((_parse_time(p["last_seen_utc"]) - _parse_time(p["first_seen_utc"])).total_seconds() / 60.0, 1)
                except Exception:
                    stable_before = 0.0
                direction = s["direction"]
                unit = s["unit"]
                moved = (val > p["value"] and direction == "high") or (val < p["value"] and direction == "low")
                if moved:
                    breakouts.append({
                        "time": now_utc, "city": s["city"], "direction": direction, "unit": unit,
                        "old": p["value"], "new": val, "stable_minutes": stable_before,
                        "mode_label": s["mode_label"], "dead_zone": s["dead_zone"],
                        "old_dead_zone": p.get("dead_zone"),
                        "excluded_count": s["excluded_count"],
                    })
        else:
            new_state[key] = {"value": None, "mode": s["mode_label"], "dead_zone": s["dead_zone"],
                              "date": s["date"], "first_seen_utc": now_utc, "last_seen_utc": now_utc}
        dz_txt = f"<{s['dead_zone']}{s['unit']}" if s["dead_zone"] is not None else "-"
        mode_txt = f"{s['mode_label'] or '-'}"
        lines.append(f"consensus {s['city']:14s} {s['direction']:4s} date={s['date']} mode={mode_txt:16s} dead_zone{dz_txt:8s} excl={s['excluded_count']}"
                     + (" ERROR" if s.get("error") else ""))
    json.dump(new_state, open(CONSENSUS_STATE, "w"), ensure_ascii=False, indent=1)
    lines.append(f"breakout_count: {len(breakouts)}")
    if breakouts:
        lines.append("BREAKOUTS:")
        for b in breakouts:
            dir_txt = "高温共识向上突破" if b["direction"] == "high" else "低温共识向下突破"
            lines.append(
                f"  {b['time']} {b['city']} {dir_txt}: mode {b['old']}{b['unit']} -> {b['new']}{b['unit']} "
                f"(old stable {b['stable_minutes']:.0f} min, dead_zone {b.get('old_dead_zone')}->{b['dead_zone']}, excl={b['excluded_count']})")
            lines.append(cross_check(b["city"], b["direction"]))
    else:
        lines.append("(no consensus breakout in this scan)")
    lines.append(f"event_fetch_failures: {failures}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
