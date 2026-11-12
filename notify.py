#!/usr/bin/env python3
"""SetRaporu Radar -> Telegram. Sadece tarama (AI yok). "Taslak yaz" butonu Cloudflare Worker ile (worker/worker.js).

Özellikler: takip listesi (eşiksiz bildirim), sabah özeti, kaynak sağlığı uyarısı, gişe penceresi (düşük eşik),
bildirimsiz durum mesajı, "Taslak yaz" butonu.

Ortam değişkenleri:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    zorunlu (gönderim için)
  USE_REDDIT=1          Reddit'i de tara (varsayılan 0: bulut IP'lerini Reddit çoğu zaman engeller)
  MIN_SCORE=8           bu puan ve üstü gönderilir
  MIN_SOURCES=2         ya da en az bu kadar haber sitesi aynı haberi yazmışsa
  MAX_PER_RUN=0         bir turda en fazla kaç haber (0 = sınırsız)
  WINDOW_HOURS=12       son kaç saatin haberlerine bakılır
  QUIET_START=0 QUIET_END=9      Türkiye saatiyle sessiz saatler (bu aralıkta haber atmaz, sabah özetine kalır)
  DIGEST_HOUR=9 DIGEST_SIZE=5    sabah özeti saati / kaç haber (DIGEST_HOUR=-1: kapalı)
  BOXOFFICE_DAYS=4,6 BOXOFFICE_FROM=17 BOXOFFICE_MIN_SCORE=5   gişe haberleri: Cum/Paz 17:00'dan sonra düşük eşik
  FAIL_LIMIT=3          bir kaynak üst üste kaç tur düşerse uyarı
  WATCHLIST_FILE=watchlist.txt   WATCHLIST="ek,kelimeler"
  DRAFT_BUTTON=1        mesajlara "Taslak yaz" butonu ekle (Cloudflare Worker kurulunca)
  KEEP_HEARTBEATS=1     "haber yok" mesajları silinmesin
  STATE_FILE=state.json

Kullanım:
  python notify.py --dry-run     # ne göndereceğini yazdır, gönderme/kaydetme
  python notify.py --init        # mevcut haberleri 'görüldü' say (ilk kurulum)
  python notify.py               # normal tur
  python notify.py --force       # sessiz saati yok say
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

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
MAX_PER_RUN = env("MAX_PER_RUN", 0, int)  # 0 = sınırsız
QUIET_START = env("QUIET_START", 0, int)
QUIET_END = env("QUIET_END", 9, int)
DIGEST_HOUR = env("DIGEST_HOUR", 9, int)
DIGEST_SIZE = env("DIGEST_SIZE", 5, int)
BO_DAYS = {int(x) for x in env("BOXOFFICE_DAYS", "4,6").split(",") if x.strip()}  # 0=Pzt ... 4=Cum, 6=Paz
BO_FROM = env("BOXOFFICE_FROM", 17, int)
BO_MIN_SCORE = env("BOXOFFICE_MIN_SCORE", 5.0, float)
FAIL_LIMIT = env("FAIL_LIMIT", 3, int)
DRAFT_BUTTON = env("DRAFT_BUTTON", "0") == "1"
WATCHLIST_FILE = Path(env("WATCHLIST_FILE", "watchlist.txt"))
STATE = Path(env("STATE_FILE", "state.json"))

BO_RE = re.compile(r"box office|box-office|opening weekend|weekend gross|opening day|biggest opening|"
                   r"weekend recap|weekend estimates", re.I)


class Hit(NamedTuple):
    cluster: radar.Cluster
    watch: list
    bo: bool


# ---------------------------------------------------------------- takip listesi
def load_watchlist():
    terms = []
    try:
        for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line.lower())
    except FileNotFoundError:
        pass
    terms += [t.strip().lower() for t in os.environ.get("WATCHLIST", "").split(",") if t.strip()]
    if not terms:
        return None
    pat = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{pat})(?!\w)", re.I)


WATCH = load_watchlist()


def watch_hits(c: radar.Cluster) -> list:
    if not WATCH:
        return []
    return sorted({m.group(0).lower() for i in c.items for m in WATCH.finditer(i.title)})


# ---------------------------------------------------------------- durum (görülenler)
def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("seen", {})
    return st


def save_state(st: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def mark(st: dict, cluster: radar.Cluster) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for it in cluster.items:
        st["seen"].setdefault(radar.item_key(it), now)


# ---------------------------------------------------------------- seçim + biçim
def news_sources(c: radar.Cluster) -> set:
    return {i.source for i in c.items if not i.source.startswith("r/")}


def pick(ranked: list, st: dict, now_tr: datetime) -> list:
    """Gönderilmeye değer, daha önce görülmemiş kümeler: takip listesi / puan / çok kaynak / gişe penceresi."""
    in_bo = now_tr.weekday() in BO_DAYS and now_tr.hour >= BO_FROM
    out = []
    for c in ranked:
        if any(radar.item_key(i) in st["seen"] for i in c.items):
            mark(st, c)  # haber zaten gönderildi; sonradan eklenen kaynaklar tekrar mesaj üretmesin
            continue
        watch = watch_hits(c)
        bo = in_bo and any(BO_RE.search(i.title) for i in c.items)
        if watch or c.score >= MIN_SCORE or len(news_sources(c)) >= MIN_SOURCES or (bo and c.score >= BO_MIN_SCORE):
            out.append(Hit(c, watch, bo))
    return out


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def first_link(c: radar.Cluster) -> str:
    return sorted(c.items, key=lambda i: i.source.startswith("r/"))[0].link


def format_message(h: Hit) -> str:
    c = h.cluster
    icon = "🚨" if len(news_sources(c)) >= 3 else "📰"
    lines = [f"{icon} <b>{esc(c.lead.title)}</b>"]
    tags = []
    if h.watch:
        tags.append("⭐ " + ", ".join(h.watch[:4]))
    if h.bo:
        tags.append("🎟 gişe")
    if tags:
        lines.append("🔖 " + " · ".join(tags))
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


def format_digest(top: list) -> str:
    lines = ["☀️ <b>Sabah özeti</b>"]
    for n, h in enumerate(top, 1):
        star = " ⭐" if h.watch else ""
        lines.append(f'{n}. <a href="{esc(first_link(h.cluster))}">{esc(h.cluster.lead.title)}</a>{star} '
                     f"({len(h.cluster.items)} haber)")
    return "\n\n".join(lines)


# ---------------------------------------------------------------- Telegram
API = "https://api.telegram.org/bot{}/{}"
DRAFT_MARKUP = {"inline_keyboard": [[{"text": "✍️ Taslak yaz", "callback_data": "draft"}]]}


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


def send(token, chat_id, text, silent=False, button=False, preview=True):
    """silent=True: mesaj gelir ama telefon bildirimi/ses çıkarmaz."""
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_notification": silent}
    if button:
        body["reply_markup"] = DRAFT_MARKUP
    if not preview:
        body["link_preview_options"] = {"is_disabled": True}
    r = tg(token, "sendMessage", body)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"Telegram {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:200]}")
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def heartbeat(token, chat_id, st: dict, text: str, dry: bool = False) -> None:
    """Bildirimsiz durum mesajı. Önceki durum mesajını siler, sohbet dolmasın (KEEP_HEARTBEATS=1: silme)."""
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


# ---------------------------------------------------------------- kaynak sağlığı
def update_health(st: dict, token, chat_id, dry: bool) -> None:
    sources = list(radar.FEEDS) + [f"r/{s}" for s in radar.REDDIT_SUBS]
    fails, alerted = st.setdefault("fails", {}), st.setdefault("alerted", [])
    msgs = []
    for s in sources:
        if s in radar.FAILED:
            fails[s] = fails.get(s, 0) + 1
            if fails[s] >= FAIL_LIMIT and s not in alerted:
                alerted.append(s)
                msgs.append(f"⚠️ <b>Kaynak sorunu:</b> {esc(s)} üst üste {fails[s]} turdur alınamıyor.")
        else:
            if s in alerted:
                alerted.remove(s)
                msgs.append(f"✅ {esc(s)} tekrar çalışıyor.")
            fails[s] = 0
    for m in msgs:
        print(re.sub(r"<[^>]+>", "", m))
        if not dry:
            send(token, chat_id, m)


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
    stamp, today = now_tr.strftime("%H:%M"), now_tr.strftime("%Y-%m-%d")

    if not args.init and QUIET_START <= now_tr.hour < QUIET_END and not (args.force or args.dry_run):
        print(f"Sessiz saat (TR {stamp}). Haber gönderimi yok; haberler sabah özetine kalır.")
        heartbeat(token, chat, st, f"🌙 Sessiz saat · {stamp}")
        return 0

    items = radar.collect(WINDOW_HOURS)
    if not args.init:
        update_health(st, token, chat, args.dry_run)
        if not args.dry_run:
            save_state(st)
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

    qual = pick(ranked, st, now_tr)

    # Sabah özeti: gece birikenlerin en iyi N tanesi tek mesajda, geri kalanı sessizce görüldü sayılır
    if DIGEST_HOUR >= 0 and DIGEST_HOUR <= now_tr.hour < DIGEST_HOUR + 3 and st.get("digest") != today:
        top = sorted(qual, key=lambda h: -(h.cluster.score + (5 if h.watch else 0)))[:DIGEST_SIZE]
        if top:
            text = format_digest(top)
            if args.dry_run:
                print("[sabah özeti]\n" + text)
            else:
                send(token, chat, text, preview=False)
                for h in qual:
                    mark(st, h.cluster)
        if not args.dry_run:
            st["digest"] = today
            save_state(st)
        if top:
            return 0

    batch = qual if MAX_PER_RUN <= 0 else qual[:MAX_PER_RUN]
    print(f"{len(ranked)} küme, {len(qual)} gönderilecek, bu turda {len(batch)}.")

    if not batch:  # yeni haber yok: sessiz durum mesajı
        heartbeat(token, chat, st, f"📭 Yeni haber yok · {stamp} · {len(ranked)} haber tarandı", dry=args.dry_run)
        if not args.dry_run:
            save_state(st)
        return 0

    for h in batch:  # haber var: bildirimli mesaj
        text = format_message(h)
        if args.dry_run:
            print("-" * 40 + "\n" + text)
            continue
        send(token, chat, text, button=DRAFT_BUTTON)
        mark(st, h.cluster)
        save_state(st)  # her gönderimden sonra yaz: tur yarıda kesilse de tekrar atmaz
        time.sleep(1.1)

    if not args.dry_run:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
