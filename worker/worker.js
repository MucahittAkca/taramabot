// SetRaporu: Telegram botu için Cloudflare Worker
//  1) ÖZEL TASLAK: bota bir haber linki (ya da haber metni) yaz -> Türkçe taslak yanıt gelir.
//     Linkin yanına not da yazabilirsin: "https://... bütçe çelişkisini vurgula"
//  2) "Taslak yaz" butonu (eski mesajlarda kalan butonlar için)
//
// Gerekli ayarlar (Workers > Settings > Variables and Secrets):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, WIRO_API_KEY
//   isteğe bağlı: WIRO_API_SECRET (signature projeyse), WIRO_MODEL (varsayılan openai/gpt-6-1-terra)
// Webhook: allowed_updates içinde "message" ve "callback_query" olmalı.
// Not: aşağıdaki SYSTEM, ai.py içindeki DRAFT_SYSTEM ile aynı kuralları taşır; birini değiştirirsen ötekini de değiştir.

const SYSTEM = `Sen SetRaporu adlı Türkçe film/dizi Instagram sayfasının editörüsün.
Sayfa hikaye anlatımı formatında, TEK görsel + detaylı caption paylaşıyor.
Sana tek bir haber veriliyor: başlık, kısa özet, haber sitesinden alınmış makale metni (article_text) ve varsa editörün özel isteği (editor_note).

Kurallar:
- Sadece verilen bilgileri kullan. Rakam, tarih, isim UYDURMA. Metin yetersizse kısa tut, eksikleri DOĞRULA'ya yaz.
- Haberi çevirme, kendi cümlelerinle yeniden anlat. Kaynaktan cümle kopyalama.
- editor_note varsa (açı, vurgu, ton isteği) ona uy, ama kaynakta olmayan bilgi ekleme.
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

const HELP =
  "Bir haber linki gönder, taslağını yazayım.\n" +
  "Linkin yanına not da ekleyebilirsin, örn: https://... bütçe çelişkisini vurgula\n" +
  "Sayfa açılmazsa haberin metnini yapıştır (link olmadan da olur).";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("SetRaporu worker ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response("bad request", { status: 400 }); }

    const own = (chat) => chat && String(chat.id) === String(env.TELEGRAM_CHAT_ID); // yalnızca senin sohbetin
    if (update.callback_query) {
      const cb = update.callback_query;
      if (cb.data === "draft" && cb.message && own(cb.message.chat)) ctx.waitUntil(handleButton(env, cb, cb.message));
    } else if (update.message && own(update.message.chat)) {
      ctx.waitUntil(handleMessage(env, update.message)); // uzun işi arka planda yap, Telegram'a hemen "ok" dön
    }
    return new Response("ok");
  },
};

// ---------------------------------------------------------------- özel taslak (link / metin)
async function handleMessage(env, m) {
  try {
    const text = (m.text || "").trim();
    if (!text) return;
    if (text.startsWith("/")) { await reply(env, m, HELP); return; }

    const urls = extractUrls(m);
    const rest = text.replace(/https?:\/\/\S+/g, "").trim();
    let page = { title: "", text: "" }, note = "";

    if (urls.length) {
      await tg(env, "sendChatAction", { chat_id: m.chat.id, action: "typing" });
      page = await fetchPage(urls[0]);
      if (page.text.length >= 300) note = rest;                        // sayfa okundu: kalan yazı editör notu
      else if (rest.length >= 150) page.text = rest;                  // sayfa okunamadı ama metni sen yapıştırdın
      else {
        await reply(env, m, "Sayfayı okuyamadım (site botları engelliyor olabilir). Haberin metnini linkle birlikte yapıştırır mısın? Ona göre yazarım.");
        return;
      }
    } else if (rest.length >= 150) {
      page.text = rest;                                               // yapıştırılmış haber metni
    } else {
      await reply(env, m, HELP);
      return;
    }

    await tg(env, "sendChatAction", { chat_id: m.chat.id, action: "typing" });
    const user = JSON.stringify({ title: page.title, source_links: urls, editor_note: note, article_text: page.text });
    await sendLong(env, m.chat.id, await callLLM(env, user), m.message_id);
  } catch (e) {
    await reply(env, m, "❌ Taslak üretilemedi: " + String((e && e.message) || e).slice(0, 300));
  }
}

function extractUrls(m) {
  const out = [], text = m.text || "";
  for (const e of m.entities || []) {
    if (e.type === "url") out.push(text.slice(e.offset, e.offset + e.length));
    else if (e.type === "text_link" && e.url) out.push(e.url);
  }
  if (!out.length) out.push(...(text.match(/https?:\/\/\S+/g) || []));
  return [...new Set(out)];
}

// ---------------------------------------------------------------- eski "Taslak yaz" butonu
async function handleButton(env, cb, msg) {
  try {
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Taslak hazırlanıyor, ~20 sn…" });
    const lines = (msg.text || "").split("\n");
    const title = (lines[0] || "").replace(/^\S+\s+/, ""); // baştaki emojiyi at
    const summary = lines.slice(1).filter((l) => !l.startsWith("Puan") && !l.startsWith("🔖")).join(" ").trim();
    const links = (msg.entities || []).filter((e) => e.type === "text_link").map((e) => e.url);
    const articleUrl = links.find((u) => !/reddit\.com/.test(u));
    const page = articleUrl ? await fetchPage(articleUrl) : { title: "", text: "" };
    const user = JSON.stringify({ title, summary, source_links: links, article_text: page.text });
    await sendLong(env, msg.chat.id, await callLLM(env, user), msg.message_id);
  } catch (e) {
    await reply(env, msg, "❌ Taslak üretilemedi: " + String((e && e.message) || e).slice(0, 300));
  }
}

// ---------------------------------------------------------------- yardımcılar
async function fetchPage(url) {
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; SetRaporuBot/1.0)", Accept: "text/html" },
      signal: ctl.signal,
      redirect: "follow",
    });
    clearTimeout(timer);
    if (!r.ok) return { title: "", text: "" };
    const page = (await r.text()).slice(0, 600000);
    const paras = [...page.matchAll(/<p[^>]*>([\s\S]*?)<\/p>/gi)].map((m) => clean(m[1])).filter((p) => p.length > 60);
    const og =
      page.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i) ||
      page.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["']/i) ||
      page.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
    return { title: og ? clean(og[1]) : "", text: paras.join("\n").slice(0, 3500) };
  } catch {
    return { title: "", text: "" };
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

async function reply(env, m, text) {
  await tg(env, "sendMessage", { chat_id: m.chat.id, reply_to_message_id: m.message_id, text });
}

async function sendLong(env, chatId, text, replyTo) {
  const parts = text.match(/[\s\S]{1,3900}/g) || [text];
  for (let i = 0; i < parts.length; i++) {
    await tg(env, "sendMessage", { chat_id: chatId, text: parts[i], ...(i === 0 ? { reply_to_message_id: replyTo } : {}) });
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
