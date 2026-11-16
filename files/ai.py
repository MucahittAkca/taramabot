"""Yapay zeka adımları: havuzdan haber seçimi + Türkçe taslak (Wiro LLM Gateway). notify.py kullanır."""
from __future__ import annotations

import html
import json
import re

import httpx

import radar

UA = "Mozilla/5.0 (compatible; SetRaporuBot/1.0)"

SELECT_SYSTEM = """Sen SetRaporu adlı Türkçe film/dizi Instagram sayfasının editörüsün.
Sayfa hikaye anlatımı formatında, TEK görsel + detaylı caption paylaşıyor; kitle global haberlerle büyüyecek.
Sana son saatlerde toplanmış haber adayları veriliyor (id, başlık, özet, haber sitesi sayısı, puan, etiketler).
Görevin: şu an paylaşılacak EN İYİ 1 haberi seç ve 2 alternatif ver.

Seçim ölçütleri:
- Hikaye kurmaya yetecek bilgi var, birden fazla güvenilir kaynakta çıkmış.
- Global ilgi: büyük franchise/yönetmen/oyuncu/platform, gişe ya da bütçe çelişkisi, beklenmedik gelişme, perde arkası.
- Kaçın: dedikodu, taciz/suç iddiaları, ölüm haberleri, tiyatro/Broadway, çok niş sektör ya da atama haberleri, sadece duyuru niteliğinde ince haberler.

Çıktı SADECE geçerli JSON: {"pick": <id>, "reason": "tek cümle Türkçe gerekçe", "alternates": [<id>, <id>]}"""

DRAFT_SYSTEM = """Sen SetRaporu adlı Türkçe film/dizi Instagram sayfasının editörüsün.
Sayfa hikaye anlatımı formatında, TEK görsel + detaylı caption paylaşıyor.
Sana tek bir haber veriliyor: başlık, kaynak raporları (reports), varsa Reddit başlıkları (reddit_titles) ve varsa haber sitesinden alınmış makale metni (article_text).

Kurallar:
- Sadece verilen bilgileri kullan. Rakam, tarih, isim UYDURMA. Metin yetersizse kısa tut, eksikleri DOĞRULA'ya yaz.
- Haberi çevirme, kendi cümlelerinle yeniden anlat. Kaynaktan cümle kopyalama.
- Caption OKUYUCUYA yazılır. Caption içinde "başlık", "özet", "makale", "kaynakta yer almıyor" gibi kaynağa ya da senin işine gönderme YAPMA. Bilmediğin ya da belirsiz konuyu caption'da hiç açma; sadece DOĞRULA bölümüne yaz. Caption içinde [DOĞRULA] yazma.
- Hitap samimi ve tekil olsun ("sen", "sence"). Resmi "siz" kullanma.
- Kaynaklar farklı rakam veriyorsa birini kesin gibi yazma: "yaklaşık" de, farkı DOĞRULA'ya yaz.
- Kaynakta olmayan dramatik yorum, sebep ya da sonuç ekleme.
- İngilizce "domestic" ABD-Kanada gişesi demektir: "yurt içi" diye çevirme, "ABD-Kanada (Kuzey Amerika) gişesi" yaz. Para birimini ve pazarı her zaman belirt.
- Haberde çarpıcı bir karşıtlık varsa (bütçe vs hasılat, beklenti vs sonuç) kancayı ondan kur.

Çıktı biçimi (düz metin, markdown ve yıldız kullanma):
GÖRSEL METNİ: (görsel üstüne yazılacak kanca, en fazla 12 kelime)

CAPTION:
(ilk satır kanca; sonra 3-5 kısa paragraf hikaye; sonda tek soru; en son satırda 5-8 hashtag, #SetRaporu dahil)

DOĞRULA:
(doğrulanması gereken noktalar, her biri ayrı satırda; rakam/sıralama/tarih varsa boş bırakma)"""


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def fetch_article(url: str) -> str:
    """Haber sayfasından paragraf metni (en iyi çaba; engellenirse boş döner)."""
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=10, follow_redirects=True)
        if r.status_code != 200:
            return ""
        page = r.text[:600000]
    except Exception:
        return ""
    paras = [clean(m) for m in re.findall(r"<p[^>]*>(.*?)</p>", page, flags=re.S | re.I)]
    return "\n".join(p for p in paras if len(p) > 60)[:3500]


def news_count(entry: dict) -> int:
    return sum(1 for s in entry["sources"] if not s["name"].startswith("r/"))


def best_summary(entry: dict) -> str:
    return max((s.get("summary", "") for s in entry["sources"]), key=len, default="")


def select(model: str, pool: list) -> tuple:
    """Havuzdan en iyi haberi seç. Döner: ({"pick": idx, "reason": str, "alternates": [idx,...]}, usage)."""
    order = sorted(range(len(pool)), key=lambda i: -(pool[i]["score"] + (5 if pool[i]["watch"] else 0)))[:30]
    idmap = {n + 1: i for n, i in enumerate(order)}
    payload = []
    for n, i in enumerate(order):
        e = pool[i]
        tags = (["takip: " + ", ".join(e["watch"][:3])] if e["watch"] else []) + (["gişe"] if e["bo"] else [])
        payload.append({"id": n + 1, "title": e["title"], "summary": best_summary(e)[:350],
                        "news_sites": news_count(e), "score": e["score"], "tags": tags})
    fallback = {"pick": order[0], "reason": "Puana göre en yüksek aday (AI seçimi yapılamadı).",
                "alternates": order[1:3]}
    try:
        text, usage = radar.llm(model, [{"role": "system", "content": SELECT_SYSTEM},
                                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        data = radar.parse_json(text)
        pick = idmap[int(data["pick"])]
        alts = []
        for x in data.get("alternates", []):
            if int(x) in idmap and idmap[int(x)] != pick and idmap[int(x)] not in alts:
                alts.append(idmap[int(x)])
        return {"pick": pick, "reason": str(data.get("reason", ""))[:300], "alternates": alts[:2]}, usage
    except Exception as e:
        print(f"  ! seçim adımı başarısız, puana göre seçildi: {e}")
        return fallback, {}


def draft(model: str, entry: dict) -> tuple:
    """Seçilen haber için Türkçe taslak. Döner: (metin, usage). Hata olursa istisna fırlatır."""
    article, tried = "", 0
    for s in entry["sources"]:
        if s["name"].startswith("r/") or tried >= 2:
            continue
        tried += 1
        article = fetch_article(s["url"])
        if len(article) >= 300:
            break
    user = {
        "title": entry["title"],
        "reports": [{"source": s["name"], "title": s["title"], "text": s.get("summary", "")}
                    for s in entry["sources"] if not s["name"].startswith("r/")][:4],
        "reddit_titles": [s["title"] for s in entry["sources"] if s["name"].startswith("r/")][:3],
        "article_text": article,
    }
    text, usage = radar.llm(model, [{"role": "system", "content": DRAFT_SYSTEM},
                                    {"role": "user", "content": json.dumps(user, ensure_ascii=False)}])
    return text.strip(), usage
