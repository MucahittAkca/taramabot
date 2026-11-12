// SetRaporu: Telegram "Taslak yaz" butonu (Cloudflare Worker)
// Butona basılınca: mesajdaki habere ait makaleyi (varsa) çeker, Wiro LLM ile Türkçe post taslağı yazar,
// taslağı aynı sohbete haberin altına yanıt olarak gönderir.
//
// Gerekli ayarlar (Workers > Settings > Variables and Secrets):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, WIRO_API_KEY
//   isteğe bağlı: WIRO_API_SECRET (signature projeyse), WIRO_MODEL (varsayılan openai/gpt-6-1-terra)

const SYSTEM = `Sen SetRaporu adlı Türkçe film/dizi Instagram sayfasının editörüsün.
Sayfa hikaye anlatımı formatında, TEK görsel + detaylı caption paylaşıyor.
Sana tek bir haber veriliyor: başlık, kısa özet ve (varsa) haber sitesinden alınmış makale metni.

Kurallar:
- Sadece verilen bilgileri kullan. Rakam, tarih, isim UYDURMA.
- Haberi çevirme, kendi cümlelerinle yeniden anlat. Kaynaktan cümle kopyalama.
- Caption OKUYUCUYA yazılır. Caption içinde "başlık", "özet", "makale", "kaynakta yer almıyor" gibi kaynağa ya da senin işine gönderme YAPMA. Bilmediğin ya da belirsiz konuyu caption'da hiç açma; sadece DOĞRULA bölümüne yaz. Caption içinde [DOĞRULA] yazma.
- Hitap samimi ve tekil olsun ("sen", "sence"). Resmi "siz" kullanma.
- Kaynaklar ya da metin farklı rakam veriyorsa birini kesin gibi yazma: "yaklaşık" de, farkı DOĞRULA'ya yaz.
- Kaynakta olmayan dramatik yorum, sebep ya da sonuç ekleme.
- İngilizce "domestic" ABD-Kanada gişesi demektir: "yurt içi" diye çevirme, "ABD-Kanada (Kuzey Amerika) gişesi" yaz. Para birimini ve pazarı her zaman belirt.
- Haberde çarpıcı bir karşıtlık varsa (bütçe vs hasılat, beklenti vs sonuç) kancayı ondan kur.

Çıktı biçimi (düz metin, markdown ve yıldız kullanma):
GÖRSEL METNİ: (görsel üstüne yazılacak kanca, en fazla 12 kelime)

CAPTION:
(ilk satır kanca; sonra 3-5 kısa paragraf hikaye; sonda tek soru; en son satırda 5-8 hashtag, #SetRaporu dahil)

DOĞRULA:
(doğrulanması gereken noktalar, her biri ayrı satırda; rakam/sıralama/tarih varsa boş bırakma)`;

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("SetRaporu worker ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response("bad request", { status: 400 }); }

    const cb = update.callback_query;
    if (!cb || cb.data !== "draft") return new Response("ok");
    const msg = cb.message;
    // yalnızca senin sohbetin
    if (!msg || String(msg.chat.id) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ok");

    ctx.waitUntil(handle(env, cb, msg)); // uzun işi arka planda yap, Telegram'a hemen "ok" dön
    return new Response("ok");
  },
};

async function handle(env, cb, msg) {
  try {
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Taslak hazırlanıyor, ~20 sn…" });

    const lines = (msg.text || "").split("\n");
    const title = (lines[0] || "").replace(/^\S+\s+/, ""); // baştaki emojiyi at
    const summary = lines
      .slice(1)
      .filter((l) => !l.startsWith("Puan") && !l.startsWith("🔖"))
      .join(" ")
      .trim();
    const links = (msg.entities || []).filter((e) => e.type === "text_link").map((e) => e.url);
    const articleUrl = links.find((u) => !/reddit\.com/.test(u));
    const article = articleUrl ? await fetchArticle(articleUrl) : "";

    const user = JSON.stringify({ title, summary, source_links: links, article_text: article });
    const draft = await callLLM(env, user);
    await sendLong(env, msg.chat.id, draft, msg.message_id);
  } catch (e) {
    await tg(env, "sendMessage", {
      chat_id: msg.chat.id,
      reply_to_message_id: msg.message_id,
      text: "❌ Taslak üretilemedi: " + String((e && e.message) || e).slice(0, 300),
    });
  }
}

async function fetchArticle(url) {
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; SetRaporuBot/1.0)", Accept: "text/html" },
      signal: ctl.signal,
      redirect: "follow",
    });
    clearTimeout(timer);
    if (!r.ok) return "";
    const page = (await r.text()).slice(0, 600000);
    const paras = [...page.matchAll(/<p[^>]*>([\s\S]*?)<\/p>/gi)]
      .map((m) => clean(m[1]))
      .filter((p) => p.length > 60);
    return paras.join("\n").slice(0, 3500);
  } catch {
    return ""; // makale çekilemezse mesajdaki özetle devam
  }
}

function clean(s) {
  return s
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;|&rsquo;|&#8217;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

async function callLLM(env, userContent) {
  const key = env.WIRO_API_SECRET ? `${env.WIRO_API_KEY}:${env.WIRO_API_SECRET}` : env.WIRO_API_KEY;
  const r = await fetch("https://llm.wiro.ai/v1/chat/completions", {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: env.WIRO_MODEL || "openai/gpt-6-1-terra",
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: userContent },
      ],
    }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(JSON.stringify(data.error || data).slice(0, 300));
  return data.choices[0].message.content;
}

async function sendLong(env, chatId, text, replyTo) {
  const parts = text.match(/[\s\S]{1,3900}/g) || [text];
  for (let i = 0; i < parts.length; i++) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: parts[i],
      ...(i === 0 ? { reply_to_message_id: replyTo } : {}),
    });
  }
}

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json().catch(() => ({}));
}