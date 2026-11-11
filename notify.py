#!/usr/bin/env python3
"""SetRaporu Radar -> Telegram. Sadece tarama (AI yok). Cron / GitHub Actions ile 30-60 dk'da bir çalışır.

Ortam değişkenleri:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   zorunlu (gönderim için)
  USE_REDDIT=1        Reddit'i de tara (varsayılan 0: bulut IP'lerini Reddit çoğu zaman engeller)
  MIN_SCORE=8         bu puan ve üstü gönderilir
  MIN_SOURCES=2       ya da en az bu kadar farklı haber sitesi aynı haberi yazmışsa
  MAX_PER_RUN=6       bir turda en fazla kaç mesaj
  WINDOW_HOURS=12     son kaç saatin haberlerine bakılır
  QUIET_START=0 QUIET_END=8   Türkiye saatiyle sessiz saatler (bu aralıkta mesaj atmaz)
  STATE_FILE=state.json

Kullanım:
  python notify.py --dry-run     # ne göndereceğini yazdır, gönderme, kaydetme
  python notify.py --init        # mevcut haberleri 'görüldü' say (ilk kurulumda mesaj yağmasın)
  python notify.py               # normal tur
  python notify.py --force       # sessiz saati yok say
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import radar

TR = timezone(timedelta(hours=3))  # Türkiye UTC+3, yaz/kış saati yok
KEEP_DAYS = 10


def env(name: str, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


WINDOW_HOURS = env("WINDOW_HOURS", 12, int)
MIN_SCORE = env("MIN_SCORE", 8.0, float)
MIN_SOURCES = env("MIN_SOURCES", 2, int)
MAX_PER_RUN = env("MAX_PER_RUN", 6, int)
QUIET_START = env("QUIET_START", 0, int)
QUIET_END = env("QUIET_END", 8, int)
STATE = Path(env("STATE_FILE", "state.json"))


# ---------------------------------------------------------------- durum (görülenler)
def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        st.setdefault("seen", {})
        return st
    except Exception:
        return {"seen": {}}


def save_state(st: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def mark(st: dict, cluster: radar.Cluster) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for it in cluster.items:
        st["seen"].setdefault(radar.item_key(it), now)


# ---------------------------------------------------------------- seçim + biçim
def news_sources(c: radar.Cluster) -> set[str]:
    return {i.source for i in c.items if not i.source.startswith("r/")}


def pick(ranked: list[radar.Cluster], st: dict) -> list[radar.Cluster]:
    out = []
    for c in ranked:
        keys = {radar.item_key(i) for i in c.items}
        if keys & set(st["seen"]):
            mark(st, c)  # haber zaten gönderildi; sonradan eklenen kaynaklar tekrar mesaj üretmesin
            continue
        if c.score >= MIN_SCORE or len(news_sources(c)) >= MIN_SOURCES:
            out.append(c)
    return out


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def format_message(c: radar.Cluster) -> str:
    n_news = len(news_sources(c))
    icon = "🚨" if n_news >= 3 else "📰"
    lines = [f"{icon} <b>{esc(c.lead.title)}</b>"]
    summary = max((i.summary for i in c.items), key=len, default="")
    if summary:
        lines.append(esc(summary[:220].rstrip()) + ("…" if len(summary) > 220 else ""))
    links, seen_src = [], set()
    for it in sorted(c.items, key=lambda i: i.source.startswith("r/")):  # haber siteleri önce
        if it.source not in seen_src and len(links) < 5:
            seen_src.add(it.source)
            links.append(f'<a href="{esc(it.link)}">{esc(it.source)}</a>')
    lines.append(f"Puan {c.score:.0f} · {len(c.items)} haber · " + " · ".join(links))
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram
API = "https://api.telegram.org/bot{}/{}"


def tg(token: str, method: str, body: dict):
    r = None
    for _ in range(2):
        r = httpx.post(API.format(token, method), json=body, timeout=30)
        if r.status_code == 429:
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            time.sleep(wait + 1)
            continue
        break
    return r


def send(token: str, chat_id: str, text: str, silent: bool = False) -> int | None:
    """silent=True: mesaj gelir ama telefon bildirimi/ses çıkarmaz."""
    r = tg(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "disable_notification": silent})
    if r is None or r.status_code != 200:
        raise RuntimeError(f"Telegram {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:200]}")
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def heartbeat(token, chat_id, st: dict, text: str, dry: bool = False) -> None:
    """Bildirimsiz durum mesajı ("bot çalışıyor"). Önceki durum mesajını siler, sohbet dolmasın.
    Silinmesin istersen KEEP_HEARTBEATS=1."""
    if dry:
        print("[sessiz] " + text)
        return
    if st.get("hb") and os.environ.get("KEEP_HEARTBEATS") != "1":
        try:
            tg(token, "deleteMessage", {"chat_id": chat_id, "message_id": st["hb"]})
        except Exception:
            pass
    st["hb"] = send(token, chat_id, text, silent=True)
    save_state(st)


# ---------------------------------------------------------------- ana akış
def main() -> int:
    ap = argparse.ArgumentParser(description="SetRaporu Radar -> Telegram")
    ap.add_argument("--dry-run", action="store_true", help="gönderme/kaydetme, sadece yazdır")
    ap.add_argument("--init", action="store_true", help="mevcut haberleri görüldü say, mesaj atma")
    ap.add_argument("--force", action="store_true", help="sessiz saati yok say")
    args = ap.parse_args()

    if os.environ.get("USE_REDDIT", "0") != "1":
        radar.REDDIT_SUBS = {}

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (args.dry_run or args.init) and not (token and chat):
        sys.exit("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID gerekli.")

    st = load_state()
    now_tr = datetime.now(TR)
    stamp = now_tr.strftime("%H:%M")

    if not args.init and QUIET_START <= now_tr.hour < QUIET_END and not (args.force or args.dry_run):
        print(f"Sessiz saat (TR {stamp}). Haber gönderimi yok.")
        heartbeat(token, chat, st, f"🌙 Sessiz saat · {stamp}")
        return 0

    items = radar.collect(WINDOW_HOURS)
    if not items:
        print("Hiç haber alınamadı (kaynaklar erişilemiyor olabilir).")
        if not args.init:
            heartbeat(token, chat, st, f"⚠️ Kaynaklardan haber alınamadı · {stamp}", dry=args.dry_run)
        return 0
    ranked = radar.rank(items)

    if args.init:
        for c in ranked:
            mark(st, c)
        save_state(st)
        print(f"Başlangıç: {len(ranked)} haber 'görüldü' olarak işaretlendi.")
        return 0

    fresh = pick(ranked, st)
    batch = fresh[:MAX_PER_RUN]
    print(f"{len(ranked)} küme, {len(fresh)} gönderilecek, bu turda {len(batch)}.")

    if not batch:  # yeni haber yok: sessiz durum mesajı
        heartbeat(token, chat, st, f"📭 Yeni haber yok · {stamp} · {len(ranked)} haber tarandı", dry=args.dry_run)
        if not args.dry_run:
            save_state(st)
        return 0

    for c in batch:  # haber var: bildirimli mesaj
        text = format_message(c)
        if args.dry_run:
            print("-" * 40 + "\n" + text)
            continue
        send(token, chat, text)
        mark(st, c)
        save_state(st)  # her gönderimden sonra yaz: tur yarıda kesilse de tekrar atmaz
        time.sleep(1.1)

    if not args.dry_run:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
