#!/usr/bin/env python3
"""SetRaporu Radar: global film/dizi haberlerini tarar, puanlar, Wiro LLM ile Türkçe post taslağı üretir.

Kurulum : pip install -r requirements.txt
Anahtar : .env dosyasına  WIRO_API_KEY=...   (signature projeyse ayrıca WIRO_API_SECRET=...)
          İstersen varsayılan model:  WIRO_MODEL=owner/model

Kullanım:
  python radar.py models --search claude      # kullanılabilir modelleri listele
  python radar.py run --model owner/model     # tara + taslak üret
  python radar.py run --no-ai                 # sadece tara ve puanla (AI yok)
  python radar.py run --hours 72 --top 15     # aralığı / adet sayısını değiştir
  python radar.py run --include-seen          # daha önce önerilenleri de göster
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx

BASE_URL = "https://llm.wiro.ai/v1"
UA = "Mozilla/5.0 (compatible; SetRaporuRadar/1.0)"
DB_PATH = Path("radar.db")
OUT_DIR = Path("out")

# ---- Kaynaklar: (rss, ağırlık). Çalışmayan feed atlanır, sadece uyarı verilir. ----
FEEDS = {
    "Deadline": ("https://deadline.com/feed/", 3.0),
    "Variety": ("https://variety.com/feed/", 3.0),
    "THR": ("https://www.hollywoodreporter.com/feed/", 3.0),
    "IndieWire": ("https://www.indiewire.com/feed/", 2.0),
    "Collider": ("https://collider.com/feed/", 1.0),
    "ScreenRant": ("https://screenrant.com/feed/", 1.0),
}
REDDIT_SUBS = {"boxoffice": 2.0, "movies": 1.0, "television": 1.0}
REDDIT_MIN_SCORE = 300

# ---- Puanlama: anahtar kelime -> puan ----
KEYWORDS = {
    "box office": 3, "opening weekend": 3, "highest-grossing": 3, "record": 2,
    "flop": 3, "bomb": 3, "budget": 2, "loss": 1, "behind the scenes": 2,
    "cancel": 2, "renewed": 1, "viewership": 2, "most-watched": 2, "lawsuit": 2,
    "fired": 2, "exits": 1, "reboot": 1, "sequel": 1, "cast": 1, "trailer": 1,
    "streaming": 1, "netflix": 1, "oscar": 1, "franchise": 1, "debut": 1,
}

# Sayfaya uymayan konular: eşleşirse puan düşer (kelime sınırlı regex)
NEGATIVE = [
    (r"\bbroadway\b|\boff-broadway\b|\bwest end\b|\bold vic\b|\bstage play\b|\bthe musical\b", -3),
    (r"\breviews?\b", -2),
    (r"\bsexual\b|\babuse\b|\brape\b|\bassault\b|\ballegations?\b|\binjunction\b|\bcoercive\b", -4),
    (r"\bdies\b|\bdied\b|\bobituary\b|\bpasses away\b", -3),
    (r"\bpodcast\b|\bnames? .{0,40}\bhead of\b|\bappoints?\b", -2),
]

STOP = set("the a an of in on to for and with is are as at by from this that its new first "
           "after over into about has have will his her their than more most "
           # haber dilinde her yerde geçen genel kelimeler (ortak sayılmasın)
           "box office weekend opens opening film films movie movies series season debut debuts "
           "debuted release released official trailer streaming".split())


@dataclass
class Item:
    title: str
    link: str
    source: str
    weight: float
    published: datetime
    summary: str = ""
    extra: float = 0.0  # reddit puanı vb.


@dataclass
class Cluster:
    lead: Item
    items: list[Item] = field(default_factory=list)
    score: float = 0.0


# ------------------------------------------------------------------ yardımcılar
def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def clean(text: str, limit: int = 400) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def tokens(title: str) -> set[str]:
    words = (re.sub(r"'s$", "", w.strip("'")) for w in re.findall(r"[a-z0-9']+", title.lower()))
    return {w for w in words if w not in STOP and len(w) > 2}


def same_story(a: set[str], b: set[str]) -> bool:
    """Aynı haber mi? Ya benzerlik yüksek, ya da uzun başlıklarda ortak kelime çok (≥3 ve küçük kümenin ≥%30'u)."""
    inter = len(a & b)
    if not a or not b:
        return False
    return jaccard(a, b) >= 0.45 or (inter >= 3 and inter / min(len(a), len(b)) >= 0.30)


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def norm_link(url: str) -> str:
    return re.sub(r"[?#].*$", "", url.strip().lower()).rstrip("/")


def item_key(item: Item) -> str:
    return hashlib.sha1(norm_link(item.link).encode()).hexdigest()


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


# ------------------------------------------------------------------ toplama
def fetch_rss(client: httpx.Client, name: str, url: str, weight: float, since: datetime) -> list[Item]:
    try:
        r = client.get(url)
        r.raise_for_status()
    except Exception as e:
        warn(f"{name}: alınamadı ({e.__class__.__name__})")
        return []
    out = []
    for e in feedparser.parse(r.content).entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc) if ts else datetime.now(timezone.utc)
        if pub < since or not e.get("title") or not e.get("link"):
            continue
        body = e.content[0].get("value", "") if e.get("content") else ""
        text = max(clean(body, 1500), clean(e.get("summary", ""), 1500), key=len)
        out.append(Item(clean(e.title, 200), e.link, name, weight, pub, text))
    return out


REDDIT_UA = "python:setraporu-radar:1.0 (personal news aggregator)"


def parse_reddit_json(data: dict, sub: str, weight: float, since: datetime) -> list[Item]:
    out = []
    for p in data["data"]["children"]:
        d = p["data"]
        if d.get("score", 0) < REDDIT_MIN_SCORE or d.get("stickied"):
            continue
        pub = datetime.fromtimestamp(d["created_utc"], timezone.utc)
        if pub < since:
            continue
        link = d["url"] if not d.get("is_self") else "https://www.reddit.com" + d["permalink"]
        out.append(Item(clean(d["title"], 300), link, f"r/{sub}", weight, pub, "", float(d["score"])))
    return out


def parse_reddit_rss(content: bytes, sub: str, weight: float, since: datetime) -> list[Item]:
    # RSS'te oy sayısı yok: günün en iyileri sırayla geliyor, sıraya göre yapay puan veriyoruz.
    out = []
    for idx, e in enumerate(feedparser.parse(content).entries):
        ts = e.get("updated_parsed") or e.get("published_parsed")
        pub = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc) if ts else datetime.now(timezone.utc)
        if pub < since or not e.get("title") or not e.get("link"):
            continue
        out.append(Item(clean(e.title, 300), e.link, f"r/{sub}", weight, pub, "", max(300.0, 5000.0 / (1 + idx))))
    return out


def fetch_reddit(client: httpx.Client, sub: str, weight: float, since: datetime) -> list[Item]:
    """Sırayla dener: old.reddit JSON -> www.reddit JSON -> RSS. Hepsi düşerse atlar."""
    attempts = [
        ("json", f"https://old.reddit.com/r/{sub}/top/.json"),
        ("json", f"https://www.reddit.com/r/{sub}/top/.json"),
        ("rss", f"https://www.reddit.com/r/{sub}/top/.rss"),
    ]
    params = {"t": "day", "limit": 40}
    errors = []
    for kind, url in attempts:
        try:
            r = client.get(url, params=params, headers={"User-Agent": REDDIT_UA})
            if r.status_code == 429:
                time.sleep(3)
                r = client.get(url, params=params, headers={"User-Agent": REDDIT_UA})
            r.raise_for_status()
            if kind == "json":
                return parse_reddit_json(r.json(), sub, weight, since)
            return parse_reddit_rss(r.content, sub, weight, since)
        except httpx.HTTPStatusError as e:
            errors.append(f"{kind} {e.response.status_code}")
        except Exception as e:
            errors.append(f"{kind} {e.__class__.__name__}")
    warn(f"r/{sub}: alınamadı ({', '.join(errors)})")
    return []


def collect(hours: int) -> list[Item]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[Item] = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
        for name, (url, w) in FEEDS.items():
            got = fetch_rss(client, name, url, w, since)
            print(f"  {name}: {len(got)}")
            items += got
        for sub, w in REDDIT_SUBS.items():
            got = fetch_reddit(client, sub, w, since)
            print(f"  r/{sub}: {len(got)}")
            time.sleep(1)
            items += got
    return items


# ------------------------------------------------------------------ kümeleme + puan
def cluster(items: list[Item]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for it in sorted(items, key=lambda i: -i.weight):
        t = tokens(it.title)
        for c in clusters:
            if same_story(t, tokens(c.lead.title)):
                c.items.append(it)
                break
        else:
            clusters.append(Cluster(lead=it, items=[it]))
    return clusters


def score(c: Cluster) -> float:
    now = datetime.now(timezone.utc)
    sources = {i.source for i in c.items}
    text = " ".join(i.title.lower() + " " + i.summary.lower() for i in c.items)
    kw = min(sum(p for k, p in KEYWORDS.items() if k in text), 6)
    reddit = min(sum(math.log10(max(i.extra, 1)) for i in c.items if i.extra), 4)
    if all(i.source.startswith("r/") for i in c.items):
        reddit = min(reddit, 2.5) - 1.0  # sadece Reddit'te konuşulan, haber sitesi yazmamış
    neg = max(sum(p for rx, p in NEGATIVE if re.search(rx, text)), -8)
    newest = max(i.published for i in c.items)
    age_h = (now - newest).total_seconds() / 3600
    recency = max(0.0, 3 - age_h / 12)
    return max(i.weight for i in c.items) + 1.5 * (len(sources) - 1) + kw + reddit + neg + recency


def rank(items: list[Item]) -> list[Cluster]:
    cs = cluster(items)
    for c in cs:
        c.score = score(c)
        # özeti en uzun olan öğeyi lead yap (AI'ye daha çok bağlam gider)
        c.lead = max(c.items, key=lambda i: (len(i.summary), i.weight))
    return sorted(cs, key=lambda c: -c.score)


# ------------------------------------------------------------------ veritabanı
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, title TEXT, first_seen TEXT)")
    return con


# ------------------------------------------------------------------ Wiro LLM
def api_key() -> str:
    key = os.environ.get("WIRO_API_KEY")
    if not key:
        sys.exit("WIRO_API_KEY yok. .env dosyasına ekle.")
    secret = os.environ.get("WIRO_API_SECRET")
    return f"{key}:{secret}" if secret else key


def llm(model: str, messages: list[dict]) -> tuple[str, dict]:
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    body = {"model": model, "messages": messages}
    last = None
    for attempt in range(3):
        r = httpx.post(f"{BASE_URL}/chat/completions", headers=headers, json=body, timeout=300)
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:300]}}
        err = data.get("error")
        if r.status_code in (503, 529) or (err and err.get("code") in ("provider_overloaded", "provider_unavailable")):
            last = err or r.status_code
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code >= 400 or err:
            raise RuntimeError(f"Wiro hatası ({r.status_code}): {err or data}")
        return data["choices"][0]["message"]["content"], data.get("usage") or {}
    raise RuntimeError(f"Wiro şu an yanıt vermiyor: {last}")


def parse_json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("JSON bulunamadı")
    return json.loads(text[a : b + 1])


SYSTEM = """Sen SetRaporu adlı Türkçe film/dizi Instagram sayfasının editörüsün.
Sayfa hikaye anlatımı formatında, TEK görsel + detaylı caption paylaşıyor. Kitle global haberlerle büyüyecek.
Sana İngilizce haber adayları veriliyor. Her aday aynı haberi veren kaynak raporlarını (reports: kaynak, başlık, metin) ve varsa Reddit başlıklarını (reddit_titles) içerir. Tüm raporlardaki bilgileri birleştirerek kullan.

Kurallar:
- Sadece verilen bilgileri kullan. Rakam, tarih, isim UYDURMA.
- Haberi çevirme, kendi cümlelerinle yeniden anlat. Kaynaktan cümle kopyalama.
- Kaynaklar farklı rakam veriyorsa (ör. 31,6 vs 30,5 milyon dolar) birini seçip kesin gibi yazma: caption'da "yaklaşık" de ya da aralık ver, farkı verify'a yaz.
- Kaynakta olmayan dramatik yorum, sebep ya da sonuç ekleme ("koltuklarını kaybettiler", "çığır açtı" gibi). Sadece kaynakların söylediğini anlat.
- İngilizce "domestic" ABD-Kanada gişesi demektir: "yurt içi" diye çevirme, "ABD-Kanada (Kuzey Amerika) gişesi" yaz. Para birimini ve pazarı her zaman belirt.
- Haberdeki ana çelişki ya da çarpıcı karşıtlık (bütçe vs hasılat, beklenti vs sonuç) varsa kancayı ondan kur.
- Caption OKUYUCUYA yazılır. Caption içinde "başlık", "özet", "verilen bilgi", "kaynakta yer almıyor" gibi kaynağa ya da senin işine gönderme YAPMA. Bilmediğin ya da belirsiz bir konuyu caption'da hiç açma; onu sadece verify listesine yaz. Caption içinde [DOĞRULA] yazma.
- Hitap samimi ve tekil olsun ("sen", "sence"). Resmi "siz" kullanma.
- Hikaye kurmaya yetecek bilgi yoksa (sadece başlıktan ibaretse, meme/yorum tarzı gönderiyse), film/dizi dışıysa ya da dedikodu ağırlıklıysa keep=false yap.
- Hikaye öncelikle: perde arkası, para/bütçe/gişe çelişkisi, kimsenin beklemediği gelişme.

Her aday için alanlar:
- id: verilen id
- keep: true/false
- tr_title: Türkçe kısa başlık
- angle: tek cümle, hikaye açısı
- image_text: görsel üstüne yazılacak kanca, en fazla 12 kelime
- caption: ilk satır kanca; sonra 3-5 kısa paragraf hikaye; sonda tek soru; en son satırda 5-8 hashtag (#SetRaporu dahil)
- verify: doğrulanması gereken noktaların listesi (rakam, sıralama, tarih içeren her aday için BOŞ OLAMAZ)

Çıktı SADECE geçerli JSON olsun: {"items":[{...}]}"""


def cluster_payload(idx: int, c: Cluster) -> dict:
    """Kümedeki her haber sitesinden metin + Reddit başlıkları: model tüm açıları ve rakam farklarını görsün."""
    news, reddit = [], []
    for it in sorted(c.items, key=lambda i: -len(i.summary)):
        if it.source.startswith("r/"):
            reddit.append(it.title)
        elif len(news) < 4:
            news.append({"source": it.source, "title": it.title, "text": it.summary[:700]})
    if not news:
        news = [{"source": c.lead.source, "title": c.lead.title, "text": c.lead.summary[:700]}]
    d = {"id": idx, "published": c.lead.published.strftime("%Y-%m-%d %H:%M UTC"), "reports": news}
    if reddit:
        d["reddit_titles"] = reddit[:3]
    return d


def draft(model: str, clusters: list[Cluster], chunk: int = 5) -> dict[int, dict]:
    results: dict[int, dict] = {}
    total_cost = 0.0
    for start in range(0, len(clusters), chunk):
        part = clusters[start : start + chunk]
        payload = [cluster_payload(start + i + 1, c) for i, c in enumerate(part)]
        print(f"  AI: {start + 1}-{start + len(part)} / {len(clusters)}")
        try:
            text, usage = llm(model, [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            for row in parse_json(text).get("items", []):
                results[int(row["id"])] = row
            total_cost += float(usage.get("cost") or 0)
        except Exception as e:
            warn(f"AI bu parça için başarısız: {e}")
    if total_cost:
        print(f"  AI maliyeti: ~{total_cost:.4f} USD")
    return results


# ------------------------------------------------------------------ çıktı
def render(clusters: list[Cluster], ai: dict[int, dict], model: str | None) -> str:
    lines = [f"# SetRaporu Radar, {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    if model:
        lines.append(f"_Model: {model}_")
    n = 0
    for idx, c in enumerate(clusters, 1):
        row = ai.get(idx)
        if ai and row and not row.get("keep", True):
            continue
        n += 1
        title = row.get("tr_title") if row else None
        lines += ["", f"## {n}. {title or c.lead.title}", f"**Puan:** {c.score:.1f}  |  **Orijinal:** {c.lead.title}"]
        lines.append("**Kaynaklar:** " + ", ".join(f"[{i.source}]({i.link})" for i in c.items[:5]))
        if row:
            lines += [f"**Açı:** {row.get('angle', '')}", f"**Görsel metni:** {row.get('image_text', '')}",
                      "", "**Caption:**", "", row.get("caption", "")]
            if row.get("verify"):
                lines += ["", "**Doğrula:** " + "; ".join(map(str, row["verify"]))]
        elif c.lead.summary:
            lines += ["", c.lead.summary[:400]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ komutlar
def cmd_models(args) -> None:
    r = httpx.get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {api_key()}"},
                  params={"search": args.search or "", "limit": args.limit}, timeout=60)
    r.raise_for_status()
    for m in r.json().get("data", []):
        pr = m.get("pricing") or {}
        try:
            price = f"in ${float(pr['prompt']) * 1e6:.2f} / out ${float(pr['completion']) * 1e6:.2f} (1M token)"
        except (KeyError, TypeError, ValueError):
            price = "fiyat: işlem süresine göre / belirtilmemiş"
        print(f"{m.get('id', '?'):45} {price}")


def cmd_run(args) -> None:
    model = args.model or os.environ.get("WIRO_MODEL")
    if not model and not args.no_ai:
        sys.exit("Model seçilmedi. --model owner/model ver (liste için: python radar.py models).")

    print("Kaynaklar taranıyor...")
    items = collect(args.hours)
    if not items:
        sys.exit("Hiç haber alınamadı. İnternet bağlantısını ve feed'leri kontrol et.")
    ranked = rank(items)

    con = db()
    seen = {r[0] for r in con.execute("SELECT key FROM seen")}
    if not args.include_seen:
        ranked = [c for c in ranked if item_key(c.lead) not in seen]
    top = ranked[: args.top]
    if not top:
        sys.exit("Yeni aday yok (hepsi daha önce önerilmiş). --include-seen ile tekrar dene.")
    print(f"{len(items)} haber -> {len(ranked)} küme -> ilk {len(top)}")

    ai = {} if args.no_ai else draft(model, top)
    md = render(top, ai, None if args.no_ai else model)

    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"radar_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"Kaydedildi: {path}")

    if not args.no_save:
        now = datetime.now(timezone.utc).isoformat()
        con.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?)",
                        [(item_key(c.lead), c.lead.title, now) for c in top])
        con.commit()


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="SetRaporu haber radarı")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("models", help="Wiro LLM modellerini listele")
    m.add_argument("--search", help="isim filtresi, ör. claude")
    m.add_argument("--limit", type=int, default=50)
    m.set_defaults(fn=cmd_models)

    r = sub.add_parser("run", help="tara ve taslak üret")
    r.add_argument("--model", help="owner/model (WIRO_MODEL ile de verilebilir)")
    r.add_argument("--hours", type=int, default=48, help="son kaç saat (varsayılan 48)")
    r.add_argument("--top", type=int, default=10, help="kaç aday (varsayılan 10)")
    r.add_argument("--no-ai", action="store_true", help="AI adımını atla")
    r.add_argument("--include-seen", action="store_true", help="önceden önerilenleri de dahil et")
    r.add_argument("--no-save", action="store_true", help="önerilenleri 'görüldü' diye kaydetme")
    r.set_defaults(fn=cmd_run)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
