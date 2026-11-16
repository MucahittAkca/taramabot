#!/usr/bin/env python3
"""SetRaporu Radar -> Telegram.

Akış:
  - Her çalıştırmada (saat başı) SESSİZ tarama: önemli haberler state.json içindeki havuza biriktirilir. Telegram'a mesaj gitmez.
  - Günde 2 kez (12:30 ve 17:30, TR saati) havuz AI'ya gider: AI paylaşılacak haberi seçer, taslağı yazar, tek mesaj olarak gelir.
    Taslaktan sonra havuz silinir.
  - Kendi haberin için taslak: haber linkini bota yaz (Cloudflare Worker, worker/worker.js).

Ortam değişkenleri:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    zorunlu (gönderim için)
  WIRO_API_KEY (+ WIRO_API_SECRET), WIRO_MODEL (varsayılan openai/gpt-6-1-terra)
  SLOTS=12:30,17:30     taslak saatleri (TR)      SLOT_GRACE_HOURS=4   saat kaçırılırsa kaç saat içinde yetiş
  WINDOW_HOURS=6        her taramada son kaç saatin haberine bakılır
  MIN_SCORE=8 MIN_SOURCES=2   havuza girme eşiği (ya da takip listesi / gişe penceresi)
  POOL_MAX=40 POOL_TTL_HOURS=36
  BOXOFFICE_DAYS=4,6 BOXOFFICE_FROM=17 BOXOFFICE_MIN_SCORE=5   Cum/Paz akşamı gişe haberleri için düşük eşik
  FAIL_LIMIT=3          bir kaynak üst üste kaç tur düşerse taslak mesajına uyarı eklenir
  USE_REDDIT=1          Reddit'i de tara (bulutta varsayılan kapalı)
  WATCHLIST_FILE=watchlist.txt   WATCHLIST="ek,kelimeler"   STATE_FILE=state.json

Kullanım:
  python notify.py                 # normal tur (tara + saati geldiyse taslak)
  python notify.py --dry-run       # gönderme/kaydetme; mesajları yazdır
  python notify.py --now           # taslak adımını hemen çalıştır (test)
  python notify.py --init          # mevcut haberleri 'görüldü' say, havuzu boşalt (ilk kurulum)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import ai
import radar

TR = timezone(timedelta(hours=3))  # Türkiye UTC+3, yaz/kış saati yok
KEEP_DAYS = 10


def env(name: str, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


WINDOW_HOURS = env("WINDOW_HOURS", 6, int)
MIN_SCORE = env("MIN_SCORE", 8.0, float)
MIN_SOURCES = env("MIN_SOURCES", 2, int)
POOL_MAX = env("POOL_MAX", 40, int)
POOL_TTL_HOURS = env("POOL_TTL_HOURS", 36, int)
SLOTS = [s.strip() for s in env("SLOTS", "12:30,17:30").split(",") if s.strip()]
SLOT_GRACE_HOURS = env("SLOT_GRACE_HOURS", 4, int)
BO_DAYS = {int(x) for x in env("BOXOFFICE_DAYS", "4,6").split(",") if x.strip()}  # 0=Pzt ... 4=Cum, 6=Paz
BO_FROM = env("BOXOFFICE_FROM", 17, int)
BO_MIN_SCORE = env("BOXOFFICE_MIN_SCORE", 5.0, float)
FAIL_LIMIT = env("FAIL_LIMIT", 3, int)
MODEL = env("WIRO_MODEL", "openai/gpt-6-1-terra")
WATCHLIST_FILE = Path(env("WATCHLIST_FILE", "watchlist.txt"))
STATE = Path(env("STATE_FILE", "state.json"))

BO_RE = re.compile(r"box office|box-office|opening weekend|weekend gross|opening day|biggest opening|"
                   r"weekend recap|weekend estimates", re.I)


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


# ---------------------------------------------------------------- durum
def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("seen", {})
    st.setdefault("pool", [])
    st.setdefault("slots", {})
    st.setdefault("fails", {})
    return st


def save_state(st: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    st["slots"] = dict(sorted(st["slots"].items())[-14:])
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def mark(st: dict, cluster: radar.Cluster) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for it in cluster.items:
        st["seen"].setdefault(radar.item_key(it), now)


# ---------------------------------------------------------------- havuz
def news_sources(c: radar.Cluster) -> set:
    return {i.source for i in c.items if not i.source.startswith("r/")}


def sources_of(c: radar.Cluster) -> list:
    out, names = [], set()
    for it in sorted(c.items, key=lambda i: i.source.startswith("r/")):  # haber siteleri önce
        if it.source not in names:
            names.add(it.source)
            out.append({"name": it.source, "url": it.link, "title": it.title, "summary": it.summary[:700]})
    return out[:6]


def make_entry(c: radar.Cluster, keys: list, watch: list, bo: bool) -> dict:
    return {"keys": keys, "title": c.lead.title, "score": round(c.score, 1), "watch": watch, "bo": bo,
            "added": datetime.now(timezone.utc).isoformat(), "sources": sources_of(c)}


def merge_entry(e: dict, c: radar.Cluster, keys: list, watch: list, bo: bool) -> bool:
    """Havuzdaki habere sonradan gelen kaynak/puanı ekle. Bir şey değiştiyse True."""
    changed = False
    new_keys = [k for k in keys if k not in e["keys"]]
    if new_keys:
        e["keys"] += new_keys
        changed = True
    have = {s["name"] for s in e["sources"]}
    for s in sources_of(c):
        if s["name"] not in have and len(e["sources"]) < 6:
            e["sources"].append(s)
            changed = True
    e["score"] = max(e["score"], round(c.score, 1))
    e["watch"] = sorted(set(e["watch"]) | set(watch))
    e["bo"] = e["bo"] or bo
    return changed


def update_pool(st: dict, ranked: list, now_tr: datetime) -> tuple:
    pool = st["pool"]
    in_bo = now_tr.weekday() in BO_DAYS and now_tr.hour >= BO_FROM
    added = updated = 0
    for c in ranked:
        keys = [radar.item_key(i) for i in c.items]
        watch = watch_hits(c)
        bo = in_bo and any(BO_RE.search(i.title) for i in c.items)
        entry = next((e for e in pool if set(keys) & set(e["keys"])), None)
        if entry:
            updated += merge_entry(entry, c, keys, watch, bo)
            mark(st, c)
            continue
        if any(k in st["seen"] for k in keys):  # daha önce havuza girmiş/taslağı yazılmış haber
            mark(st, c)
            continue
        if watch or c.score >= MIN_SCORE or len(news_sources(c)) >= MIN_SOURCES or (bo and c.score >= BO_MIN_SCORE):
            pool.append(make_entry(c, keys, watch, bo))
            mark(st, c)
            added += 1
    return added, updated


def prune_pool(st: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=POOL_TTL_HOURS)).isoformat()
    pool = [e for e in st["pool"] if e["added"] >= cutoff]
    pool.sort(key=lambda e: -(e["score"] + (5 if e["watch"] else 0)))
    st["pool"] = pool[:POOL_MAX]


# ---------------------------------------------------------------- kaynak sağlığı (mesaj atmaz, taslak mesajına eklenir)
def update_health(st: dict) -> None:
    fails = st["fails"]
    for s in list(radar.FEEDS) + [f"r/{x}" for x in radar.REDDIT_SUBS]:
        fails[s] = fails.get(s, 0) + 1 if s in radar.FAILED else 0


def health_line(st: dict) -> str:
    bad = [f"{s} ({n} tur)" for s, n in st["fails"].items() if n >= FAIL_LIMIT]
    return ("⚠️ Kaynak sorunu: " + ", ".join(bad)) if bad else ""


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


def send(token: str, chat_id: str, text: str, silent: bool = False) -> None:
    body = {"chat_id": chat_id, "text": text, "disable_notification": silent,
            "link_preview_options": {"is_disabled": True}}
    r = tg(token, "sendMessage", body)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"Telegram {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:200]}")


def split_message(text: str, limit: int = 4000) -> list:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + [text]


def post(token, chat_id, text: str, silent: bool, dry: bool) -> None:
    if dry:
        print(("[sessiz] " if silent else "") + text + "\n" + "=" * 40)
        return
    for part in split_message(text):
        send(token, chat_id, part, silent=silent)
        time.sleep(1.1)


# ---------------------------------------------------------------- taslak zamanı
def due_slot(st: dict, now_tr: datetime):
    for idx, s in enumerate(SLOTS):
        h, m = (int(x) for x in s.split(":"))
        start = now_tr.replace(hour=h, minute=m, second=0, microsecond=0)
        key = f"{now_tr:%Y-%m-%d} {s}"
        if start <= now_tr < start + timedelta(hours=SLOT_GRACE_HOURS) and key not in st["slots"]:
            return key, idx, s
    return None


def entry_links(e: dict, n: int = 3) -> str:
    return " · ".join(s["url"] for s in e["sources"][:n])


def compose(idx: int, label: str, pool: list, sel: dict, draft_text: str, st: dict) -> str:
    entry = pool[sel["pick"]]
    icon = ["☀️", "🌆"][idx] if idx < 2 else "📰"
    lines = [f"{icon} {label} · havuzda {len(pool)} haber", "",
             f"📌 {entry['title']}"]
    if sel.get("reason"):
        lines.append(f"Neden bu haber: {sel['reason']}")
    lines += [f"Kaynak: {entry_links(entry)}", "", draft_text]
    if sel["alternates"]:
        lines += ["", "— Diğer güçlü adaylar —"]
        for i in sel["alternates"]:
            lines += [f"• {pool[i]['title']}", f"  {pool[i]['sources'][0]['url']}"]
        lines.append("(Bunlardan biri için taslak istersen linkini bana yapıştır.)")
    hl = health_line(st)
    if hl:
        lines += ["", hl]
    return "\n".join(lines)


def run_slot(st: dict, token, chat, key: str, idx: int, label: str, dry: bool) -> None:
    pool = st["pool"]
    if not pool:
        post(token, chat, f"📭 {label} · havuzda uygun haber yok", True, dry)
        st["slots"][key] = "empty"
        return
    sel, u1 = ai.select(MODEL, pool)
    entry = pool[sel["pick"]]
    try:
        draft_text, u2 = ai.draft(MODEL, entry)
    except Exception as e:
        msg = (f"⚠️ {label} · taslak üretilemedi: {str(e)[:200]}\n\nSeçilen: {entry['title']}\n{entry_links(entry)}\n\n"
               "Havuz silinmedi, bir sonraki taslak zamanına kalır.")
        post(token, chat, msg, False, dry)
        st["slots"][key] = "fail"
        return
    cost = sum(float((u or {}).get("cost") or 0) for u in (u1, u2))
    if cost:
        print(f"  AI maliyeti: ~{cost:.4f} USD")
    post(token, chat, compose(idx, label, pool, sel, draft_text, st), False, dry)
    st["pool"] = []  # taslaktan sonra biriken haberler silinir
    st["slots"][key] = "ok"


# ---------------------------------------------------------------- ana akış
def main() -> int:
    ap = argparse.ArgumentParser(description="SetRaporu Radar -> Telegram")
    ap.add_argument("--dry-run", action="store_true", help="gönderme/kaydetme, sadece yazdır")
    ap.add_argument("--init", action="store_true", help="mevcut haberleri görüldü say, havuzu boşalt")
    ap.add_argument("--now", action="store_true", help="taslak adımını saatini beklemeden çalıştır")
    args = ap.parse_args()

    if os.environ.get("USE_REDDIT", "0") != "1":
        radar.REDDIT_SUBS = {}

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (args.dry_run or args.init) and not (token and chat):
        sys.exit("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID gerekli.")

    st = load_state()
    now_tr = datetime.now(TR)

    items = radar.collect(WINDOW_HOURS)
    if not args.init:
        update_health(st)
    ranked = radar.rank(items) if items else []

    if args.init:
        for c in ranked:
            mark(st, c)
        st["pool"] = []
        save_state(st)
        print(f"Başlangıç: {len(ranked)} haber 'görüldü' sayıldı, havuz boşaltıldı.")
        return 0

    if not items:
        print("Hiç haber alınamadı (kaynaklar erişilemiyor olabilir).")
    added, updated = update_pool(st, ranked, now_tr)
    prune_pool(st)
    print(f"Havuz: {len(st['pool'])} haber (+{added} yeni, {updated} güncellendi), {len(ranked)} küme tarandı.")

    slot = ("now", 0, now_tr.strftime("%H:%M")) if args.now else due_slot(st, now_tr)
    if slot:
        key, idx, label = slot
        print(f"Taslak zamanı: {label}")
        run_slot(st, token, chat, key, idx, label, args.dry_run)

    if not args.dry_run:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
