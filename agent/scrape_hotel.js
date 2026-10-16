// HOTEL WEBSITE SCRAPER for the Hippo deal room builder (read-only; runs in your own browser)
// How to use:
//   1. Open the hotel's OFFICIAL page (e.g. its ihg.com / marriott.com / hilton.com hotel page). Accept the cookie banner.
//   2. Press F12 -> Console, paste this whole file, press Enter. Wait ~30-60 s (it scrolls the page to load all photos).
//   3. A file "hotel-scrape-<hotel>.json" downloads. Send it to Claude (or feed it to scrape_to_config.py).
(async () => {
  const T0 = Date.now(), log = (...a) => console.log("[hotel-scraper]", ...a);
  log("▶ started on", location.href);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  // undo image proxies (Next.js /_next/image?url=..., Nuxt /_ipx/...) so every photo keeps its own identity
  const unproxy = u => { try { const x = new URL(u, location.href); if (/\/_next\/image\/?$/.test(x.pathname) && x.searchParams.get("url")) return new URL(x.searchParams.get("url"), location.href).href;
    const ipx = x.pathname.match(/\/_ipx\/[^/]*\/(https?:\/\/.+|\/.+)$/); if (ipx) return new URL(decodeURIComponent(ipx[1]), location.href).href; return x.href; } catch (e) { return u; } };
  // hotel base path: e.g. /en-in/hotels/jai-mahal-palace-jaipur -> only these pages describe THIS hotel
  const baseM = location.pathname.match(/(.*\/(?:hotels?|resorts?|properties)\/[^/]+)/i);
  // no /hotels/ segment (e.g. Hyatt: /unbound-collection/en-US/bnaub-holston-house-nashville): a hyphenated slug that isn't a section name is the hotel itself
  // (keep SECTION in sync with SECTION_WORDS in scrape_to_config.py)
  const SECTION = new Set(("room rooms suite suites accommodation accommodations dining restaurant restaurants bar bars meeting meetings event events " +
    "venue venues wedding weddings gallery photos spa wellness amenities offer offers package packages deal deals special specials promotions " +
    "location directions overview contact about faq policies careers news blog press terms privacy reservations booking book rate rates home index").split(" "));
  const last = location.pathname.replace(/\/$/, "").split("/").pop().toLowerCase();
  const slugIsHotel = /^[a-z0-9]+(-[a-z0-9]+)+$/.test(last) && !/^[a-z]{2}-[a-z]{2,4}$/.test(last) && !last.split("-").some(w => SECTION.has(w));
  const BASE = baseM ? baseM[1] : slugIsHotel ? location.pathname.replace(/\/$/, "") : location.pathname.replace(/\/[^/]*$/, "");
  if (baseM && location.pathname.replace(/\/$/, "") !== BASE) log("ℹ tip: for best results run this on the hotel's main page: " + location.origin + BASE);
  const vis = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" && +cs.opacity > 0; };

  // ---------- 1. scroll the whole page so lazy images/sections load ----------
  const H = () => document.documentElement.scrollHeight;
  for (let y = 0, i = 0; y < H() && i < 80; y += Math.round(innerHeight * 0.8), i++) { scrollTo(0, y); await sleep(350); }
  scrollTo(0, H()); await sleep(1200); scrollTo(0, 0); await sleep(600);
  // open carousels / galleries a little: click "next" buttons a few times (non-navigating buttons only)
  for (const b of [...document.querySelectorAll('button[aria-label*="next" i], button[class*="next" i]')].slice(0, 6)) {
    for (let k = 0; k < 8; k++) { try { b.click(); } catch (e) {} await sleep(150); }
  }

  // ---------- 2. colours ----------
  const toHex = c => { const m = c && c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/); if (!m) return null; if (m[4] !== undefined && +m[4] < 0.5) return null;
    return "#" + [m[1], m[2], m[3]].map(x => (+x).toString(16).padStart(2, "0")).join("").toUpperCase(); };
  const bg = {}, text = {}, border = {};
  const roleColours = { buttons: {}, links: {}, headings: {}, header: {}, footer: {} };
  const add = (o, k, w) => { if (k) o[k] = (o[k] || 0) + w; };
  const all = [...document.querySelectorAll("body *")].filter(vis);
  for (const el of all) {
    const cs = getComputedStyle(el), r = el.getBoundingClientRect(), area = Math.min(r.width * r.height, innerWidth * innerHeight);
    add(bg, toHex(cs.backgroundColor), area);
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(" ");
    if (own) add(text, toHex(cs.color), own.length);
    if (parseFloat(cs.borderTopWidth) > 0) add(border, toHex(cs.borderTopColor), r.width);
  }
  const roles = { buttons: 'button, a[class*="btn" i], a[class*="button" i], [role="button"], input[type="submit"]', links: "a", headings: "h1,h2,h3", header: "header, header *, [class*='header' i]", footer: "footer, footer *" };
  for (const [role, sel] of Object.entries(roles)) for (const el of [...document.querySelectorAll(sel)].filter(vis).slice(0, 400)) {
    const cs = getComputedStyle(el); add(roleColours[role], "bg:" + toHex(cs.backgroundColor), 1); add(roleColours[role], "fg:" + toHex(cs.color), 1);
  }
  const cssVars = {};
  const rootCS = getComputedStyle(document.documentElement);
  for (let i = 0; i < rootCS.length; i++) { const p = rootCS[i]; if (p.startsWith("--")) { const v = rootCS.getPropertyValue(p).trim(); if (/^(#|rgb|hsl)/i.test(v) || /font|family/i.test(p)) cssVars[p] = v; } }
  const top = (o, n = 25) => Object.entries(o).filter(([k]) => k && !k.endsWith("null")).sort((a, b) => b[1] - a[1]).slice(0, n);

  // ---------- 3. fonts ----------
  const fontOf = sel => { const c = {}; [...document.querySelectorAll(sel)].filter(vis).slice(0, 300).forEach(el => { const cs = getComputedStyle(el);
    add(c, [cs.fontFamily, cs.fontWeight, cs.fontSize, cs.textTransform, cs.letterSpacing].join(" | "), (el.innerText || "").length || 1); }); return top(c, 6); };
  const fonts = {
    h1: fontOf("h1"), h2: fontOf("h2"), h3: fontOf("h3, h4"), body: fontOf("p, li"), buttons: fontOf(roles.buttons), nav: fontOf("nav a, header a"),
    loaded: [...document.fonts].filter(f => f.status === "loaded").map(f => ({ family: f.family.replace(/["']/g, ""), weight: f.weight, style: f.style })),
    files: performance.getEntriesByType("resource").map(e => e.name).filter(u => /\.(woff2?|ttf|otf|eot)(\?|$)/i.test(u) || /fonts\.(googleapis|gstatic)\.com|use\.typekit|fonts\.com/i.test(u)),
    font_face_rules: [], stylesheets: []
  };
  for (const ss of [...document.styleSheets]) {
    if (ss.href) fonts.stylesheets.push(ss.href);
    let rules; try { rules = ss.cssRules; } catch (e) { continue; }
    for (const r of rules || []) if (r.type === 5) fonts.font_face_rules.push({ family: r.style.getPropertyValue("font-family").replace(/["']/g, "").trim(), src: r.style.getPropertyValue("src").slice(0, 400), weight: r.style.getPropertyValue("font-weight"), style: r.style.getPropertyValue("font-style") });
  }

  // ---------- 4. logos ----------
  const logos = [], seenL = new Set();
  const pushLogo = (o) => { const k = o.src || o.svg?.slice(0, 200); if (!k || seenL.has(k)) return; seenL.add(k); logos.push(o); };
  document.querySelectorAll("img, svg, [style*='background-image']").forEach(el => {
    const hay = [el.alt, el.id, typeof el.className === "string" ? el.className : el.className?.baseVal, el.getAttribute("aria-label"), el.closest("a")?.getAttribute("aria-label"), el.getAttribute("src"), el.closest("[class*='logo' i]") ? "logo" : ""].join(" ").toLowerCase();
    if (!/logo|brand/.test(hay)) return;
    const r = el.getBoundingClientRect(), where = el.closest("header, nav") ? "header" : el.closest("footer") ? "footer" : "body";
    if (el.tagName.toLowerCase() === "svg") pushLogo({ where, kind: "inline-svg", w: Math.round(r.width), h: Math.round(r.height), svg: el.outerHTML.slice(0, 60000), label: el.getAttribute("aria-label") || "" });
    else if (el.tagName === "IMG") pushLogo({ where, kind: "img", src: abs(el.currentSrc || el.src), alt: el.alt, w: el.naturalWidth, h: el.naturalHeight });
    else { const m = getComputedStyle(el).backgroundImage.match(/url\("?(.*?)"?\)/); if (m) pushLogo({ where, kind: "bg", src: abs(m[1]), w: Math.round(r.width), h: Math.round(r.height) }); }
  });
  document.querySelectorAll("link[rel*='icon'], link[rel='apple-touch-icon'], link[rel='mask-icon']").forEach(l => pushLogo({ where: "favicon", kind: l.rel, src: abs(l.href), sizes: l.sizes?.value || "" }));
  const meta = n => document.querySelector(`meta[property="${n}"], meta[name="${n}"]`)?.content || "";

  // ---------- 5. images ----------
  const imgs = new Map();
  const ctx = el => { const h = el.closest("section, article, li, figure, div")?.querySelector("h1,h2,h3,h4,figcaption"); return (h?.innerText || "").trim().slice(0, 120); };
  const addImg = (url, o) => { url = abs(url); if (!url) return; url = unproxy(url); if (url.startsWith("data:") || /\.svg(\?|$)/i.test(url)) return; const key = url.split("?")[0];
    const prev = imgs.get(key); if (!prev || (o.w || 0) > (prev.w || 0)) imgs.set(key, { url, ...o }); };
  const bestSrcset = ss => { if (!ss) return null; let best = null, bw = 0; ss.split(",").forEach(p => { const [u, d] = p.trim().split(/\s+/); const w = parseFloat(d) || 1; if (w >= bw) { bw = w; best = u; } }); return best; };
  document.querySelectorAll("img").forEach(el => {
    const src = bestSrcset(el.getAttribute("srcset") || el.dataset.srcset) || el.currentSrc || el.dataset.src || el.dataset.lazySrc || el.src;
    addImg(src, { alt: el.alt || "", w: el.naturalWidth, h: el.naturalHeight, context: ctx(el), source: "img" });
  });
  document.querySelectorAll("picture source").forEach(s => addImg(bestSrcset(s.srcset), { alt: "", w: 0, h: 0, context: ctx(s), source: "picture" }));
  all.forEach(el => { const b = getComputedStyle(el).backgroundImage; if (b && b !== "none") [...b.matchAll(/url\("?(.*?)"?\)/g)].forEach(m => { const r = el.getBoundingClientRect(); addImg(m[1], { alt: el.getAttribute("aria-label") || "", w: Math.round(r.width), h: Math.round(r.height), context: ctx(el), source: "background" }); }); });
  if (meta("og:image")) addImg(meta("og:image"), { alt: "og:image", w: 0, h: 0, context: "og", source: "og" });

  // ---------- 6. facts & text ----------
  const jsonld = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => { try { return JSON.parse(s.textContent); } catch (e) { return null; } }).filter(Boolean);
  const headings = [...document.querySelectorAll("h1,h2,h3")].filter(vis).map(h => ({ tag: h.tagName, text: h.innerText.trim().slice(0, 200) })).filter(h => h.text);
  const pageText = (document.querySelector("main") || document.body).innerText.replace(/\n{3,}/g, "\n\n").slice(0, 60000);
  const links = [...document.querySelectorAll("a[href]")].map(a => ({ text: a.innerText.trim().slice(0, 80), href: abs(a.getAttribute("href")) }))
    .filter(l => l.href && l.href.startsWith(location.origin) && /meeting|event|wedding|dining|restaurant|bar|room|suite|accommodation|amenit|spa|pool|fitness|wellness|gallery|photo|location|direction|concierge|overview/i.test(l.text + " " + l.href))
    .filter(l => new URL(l.href).pathname.startsWith(BASE));   // never follow chain-wide pages (they describe other hotels)
  if (baseM && !links.some(l => new URL(l.href).pathname.replace(/\/$/, "") === BASE)) links.unshift({ text: "OVERVIEW", href: location.origin + BASE });

  // ---------- 6b. page outline: the page's own sections in order, with their look (the deal room mirrors this) ----------
  const styleOf = el => { const cs = getComputedStyle(el); return { family: cs.fontFamily, size: cs.fontSize, weight: cs.fontWeight, transform: cs.textTransform,
    spacing: cs.letterSpacing, color: toHex(cs.color), align: cs.textAlign, line: cs.lineHeight }; };
  const bgOf = el => { for (let e = el; e && e.nodeType === 1; e = e.parentElement) { const c = toHex(getComputedStyle(e).backgroundColor); if (c) return c; } return "#FFFFFF"; };
  const chrome = el => el.closest("header, nav, footer, [role='banner'], [role='contentinfo'], [role='dialog'], [aria-modal='true'], [class*='cookie' i], [id*='onetrust' i]");
  const imgUrl = el => unproxy(abs(bestSrcset(el.getAttribute("srcset") || el.dataset.srcset) || el.currentSrc || el.dataset.src || el.getAttribute("src") || "") || "");
  const bigImg = x => x.naturalWidth >= 300 || x.getBoundingClientRect().width >= 300;
  const heads = [...document.querySelectorAll("h1, h2")].filter(h => vis(h) && !chrome(h) && h.innerText.trim());
  const outline = heads.map(hd => {
    let box = hd;                      // the section = the biggest ancestor holding this heading and no other h1/h2
    for (let p = hd.parentElement; p && p !== document.body; p = p.parentElement) {
      if (heads.some(o => o !== hd && p.contains(o))) break;
      box = p;
    }
    const ps = [...box.querySelectorAll("p, li")].filter(p => vis(p) && !p.closest("button, nav, [class*='btn' i]"));
    const paras = [...new Set(ps.map(p => p.innerText.replace(/\s+/g, " ").trim()).filter(t => t.length > 25))].slice(0, 6);
    const subs = [...box.querySelectorAll("h3, h4")].filter(vis).slice(0, 8).map(h3 => {
      let card = h3;
      for (let p = h3.parentElement; p && p !== box; p = p.parentElement) { if ([...p.querySelectorAll("h3, h4")].filter(vis).length > 1) break; card = p; }
      const im = [...card.querySelectorAll("img")].find(bigImg);
      return { title: h3.innerText.trim().slice(0, 120), image: im ? imgUrl(im) : null,
               text: [...card.querySelectorAll("p")].map(p => p.innerText.replace(/\s+/g, " ").trim()).filter(t => t.length > 20).slice(0, 2).join(" ").slice(0, 400) };
    });
    const picEls = [...box.querySelectorAll("img")].filter(bigImg);
    const pics = picEls.map(x => ({ url: imgUrl(x), w: x.naturalWidth, h: x.naturalHeight, alt: x.alt || "" }));
    const bgImgs = [box, ...box.querySelectorAll("div, section, figure, picture")].slice(0, 300).filter(el => el.getBoundingClientRect().width >= 300)
      .map(el => (getComputedStyle(el).backgroundImage.match(/url\("?(.*?)"?\)/) || [])[1]).filter(Boolean).map(u => ({ url: unproxy(abs(u)), w: 0, h: 0, alt: "" }));
    const btn = [...box.querySelectorAll(roles.buttons)].filter(vis)[0], bs = btn ? getComputedStyle(btn) : null;
    const prev = hd.previousElementSibling;
    let layout = subs.length >= 3 ? "cards" : "stacked";
    if (layout === "stacked" && picEls.length) {
      const ir = picEls[0].getBoundingClientRect(), hr = hd.getBoundingClientRect();
      if (ir.width > 0 && ir.top < hr.bottom + 200 && ir.bottom > hr.top && (ir.right <= hr.left + 10 || ir.left >= hr.right - 10)) layout = ir.left < hr.left ? "image-left" : "image-right";
    }
    const firstP = ps[0], r = box.getBoundingClientRect();
    return { tag: hd.tagName, heading: hd.innerText.trim().slice(0, 200), eyebrow: prev && vis(prev) && prev.innerText && prev.innerText.trim().length < 60 && !/^h[1-6]$/i.test(prev.tagName) ? prev.innerText.trim() : "",
      text: paras, subs, images: [...new Map([...pics, ...bgImgs].filter(p => p.url && !p.url.startsWith("data:")).map(p => [p.url.split("?")[0], p])).values()].slice(0, 12),
      bg: bgOf(box), heading_style: styleOf(hd), text_style: firstP ? styleOf(firstP) : null, layout, top: Math.round(r.top + scrollY), height: Math.round(r.height),
      button: btn ? { text: btn.innerText.trim().slice(0, 40), bg: toHex(bs.backgroundColor), color: toHex(bs.color), border_width: bs.borderTopWidth, border_color: toHex(bs.borderTopColor),
                      radius: bs.borderTopLeftRadius, transform: bs.textTransform, family: bs.fontFamily, size: bs.fontSize, weight: bs.fontWeight, spacing: bs.letterSpacing } : null };
  });
  // hero: the biggest picture in the first screen (img or background), and the largest text over it
  const firstScreen = [...document.querySelectorAll("img, div, section, picture, figure")].filter(el => { const r = el.getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0 && r.width >= innerWidth * 0.5 && !chrome(el); });
  const heroPic = firstScreen.map(el => ({ el, r: el.getBoundingClientRect(), url: el.tagName === "IMG" ? imgUrl(el) : ((getComputedStyle(el).backgroundImage.match(/url\("?(.*?)"?\)/) || [])[1] || "") }))
    .filter(o => o.url && !o.url.startsWith("data:")).sort((a, b) => b.r.width * b.r.height - a.r.width * a.r.height)[0];
  const heroText = [...document.querySelectorAll("h1, h2, p, span, div")].filter(el => { const r = el.getBoundingClientRect(); return vis(el) && !chrome(el) && r.top < innerHeight && r.height > 0 && el.children.length === 0 && el.innerText.trim().length > 3; })
    .sort((a, b) => parseFloat(getComputedStyle(b).fontSize) - parseFloat(getComputedStyle(a).fontSize))[0];
  const hero = { image: heroPic ? unproxy(abs(heroPic.url)) : null, height: heroPic ? Math.round(heroPic.r.height) : 0,
                 title: heroText ? heroText.innerText.trim().slice(0, 160) : "", title_style: heroText ? styleOf(heroText) : null };
  const nav = [...new Set([...document.querySelectorAll("header nav a, nav a, header a")].filter(vis).map(a => a.innerText.replace(/\s+/g, " ").trim()).filter(t => t && t.length < 30))].slice(0, 20);

  // ---------- 7. same-site subpages (meetings, dining, rooms, gallery...) ----------
  const sub = [], seenP = new Set([location.href.split("#")[0]]);
  for (const l of links) {
    const u = l.href.split("#")[0]; if (seenP.has(u) || sub.length >= 15) continue; seenP.add(u);
    try {
      const r = await fetch(u, { credentials: "same-origin" }); if (!r.ok) continue;
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const dataImgs = new Set();
      doc.querySelectorAll('script#__NEXT_DATA__, script[type="application/json"]').forEach(sc => {
        (sc.textContent.match(/https?:\/\/[^"'\s\\]+?\.(?:jpe?g|png|webp|avif)(?:\?[^"'\s\\]*)?/gi) || []).forEach(u => dataImgs.add(u)); });
      const pageLd = [...doc.querySelectorAll('script[type="application/ld+json"]')].map(x => { try { return JSON.parse(x.textContent); } catch (e) { return null; } }).filter(Boolean);
      doc.querySelectorAll("style, script, noscript, template").forEach(n => n.remove());
      const pimgs = [...doc.querySelectorAll("img")].map(i => ({ url: abs(bestSrcset(i.getAttribute("srcset") || i.getAttribute("data-srcset")) || i.getAttribute("data-src") || i.getAttribute("src")), alt: i.getAttribute("alt") || "" })).filter(i => i.url && !i.url.startsWith("data:"));
      pimgs.forEach(i => addImg(i.url, { alt: i.alt, w: 0, h: 0, context: "page: " + l.text, page_url: u, source: "subpage" }));
      [...dataImgs].slice(0, 300).forEach(x => addImg(x, { alt: "", w: 0, h: 0, context: "page: " + l.text, page_url: u, source: "page-data" }));
      sub.push({ url: u, link_text: l.text, title: doc.title, text: [...(doc.querySelector("main") || doc.body || doc).querySelectorAll("h1,h2,h3,h4,p,li,td,th,dt,dd,figcaption")].map(e => e.textContent.replace(/\s+/g, " ").trim()).filter(Boolean).join("\n").slice(0, 15000),
                 jsonld: pageLd, images: pimgs.length });
      log("  subpage", sub.length, u);
    } catch (e) { log("  skip", u, e.message); }
  }

  const out = {
    scraper_version: "1.2", hotel_base_path: BASE, scraped_at: new Date().toISOString(), url: location.href, title: document.title,
    outline, hero, nav, page_bg: bgOf(document.body),
    meta: { description: meta("description") || meta("og:description"), og_title: meta("og:title"), og_image: meta("og:image"), theme_color: meta("theme-color"), site_name: meta("og:site_name") },
    colours: { background_by_area: top(bg), text_by_length: top(text), borders: top(border, 10), by_role: Object.fromEntries(Object.entries(roleColours).map(([k, v]) => [k, top(v, 10)])), css_variables: cssVars },
    fonts, logos, images: [...imgs.values()].sort((a, b) => (b.w * b.h || 0) - (a.w * a.h || 0)),
    jsonld, headings, page_text: pageText, subpages: sub, viewport: [innerWidth, innerHeight], took_seconds: Math.round((Date.now() - T0) / 1000)
  };
  const name = "hotel-scrape-" + (document.title || location.hostname).toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 50) + ".json";
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" })); a.download = name; a.click();
  log(`✔ Done in ${out.took_seconds}s: ${out.images.length} images, ${logos.length} logos, ${fonts.loaded.length} fonts loaded, ${fonts.files.length} font files, ${sub.length} subpages. Downloaded ${name}`);
})();
