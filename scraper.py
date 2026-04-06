"""Amazon ürün sayfasından Etsy taslağı için veri çıkarır."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

DEFAULT_ETSY_DESCRIPTION = """✯✯✯✯✯ ORDER INSTRUCTIONS ✯✯✯✯✯

➤ Check and review all listing photos.

➤ Pick up your item’s size and color from drop down menus.

➤ Choose the quantity.

➤ Click "Add to Cart" button.

➤ Fill in the personalization box as recommended if provided.

➤ You can go back to add more item or you can complete the checkout process.

➤ Click “Proceed to Check Out”.

✯✯✯✯✯ WHICH SIZE FITS ME BEST ✯✯✯✯✯

➤ In each listing, you can find pictures showing the variety of colors and body sizes.

➤ Measurements are in inches.

➤ With arms down at side, measure around the upper body, under arms and around the fullest part of the chest.

✯✯✯✯✯ IMPORTANT ✯✯✯✯✯

➤ Due to the nature of the fabric as well as your monitor or mobile screen colors may differ slightly.

➤ We use high-quality DTF Printing.

➤ Our processing time is 2-4 business days.

✯✯✯✯✯ CANCELLATION, REFUND AND EXCHANGE ✯✯✯✯✯

➤ Refunds and Exchanges are not accepted unless the item defected or wrongly sent.

➤ Please contact us if you have problems with your order. We would be very happy to solve it.

➤ For cancellation request, please contact us 6 hours after ordering.


✯✯✯✯✯ CARE INSTRUCTIONS ✯✯✯✯✯

➤ Iron on low heat with shirt inside-out.

➤ Never iron directly over Heat Transfer Graphic.

➤ DO NOT dry clean.

➤ Machine wash COLD with mild detergent.

➤ Turn inside out when washing.

➤ Dry on low setting or hang to dry.

➤ Wait 24 hours before first wash.

➤ Do not use bleach."""


@dataclass
class ScrapedListing:
    source_url: str
    item_id: str
    title: str
    description_text: str
    currency: Optional[str]
    price_display: Optional[str]
    price_min: Optional[float]
    images: list[str] = field(default_factory=list)
    variations: list[dict[str, Any]] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)


def _asin_from_url(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/dp/([A-Z0-9]{10})", path, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"/gp/product/([A-Z0-9]{10})", path, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"([A-Z0-9]{10})", path, re.I)
    return m.group(1).upper() if m else "UNKNOWN_ASIN"


def _digits_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"[\d.,]+", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _extract_from_meta(soup: BeautifulSoup) -> tuple[str, str, list[str]]:
    title = ""
    desc = ""
    imgs: list[str] = []
    og_t = soup.find("meta", attrs={"property": "og:title"})
    if og_t and og_t.get("content"):
        title = og_t["content"].strip()
    og_d = soup.find("meta", attrs={"property": "og:description"})
    if og_d and og_d.get("content"):
        desc = og_d["content"].strip()
    og_i = soup.find("meta", attrs={"property": "og:image"})
    if og_i and og_i.get("content"):
        imgs.append(og_i["content"].strip())
    return title, desc, imgs


def _best_url_from_img_tag(img: Any) -> Optional[str]:
    """img: data-old-hires > data-a-dynamic-image (en büyük alan) > data-src > src."""
    if not img:
        return None
    hires = (img.get("data-old-hires") or "").strip()
    if hires.startswith("https://m.media-amazon.com/images/I/"):
        return hires
    dyn = (img.get("data-a-dynamic-image") or "").strip()
    if dyn:
        try:
            parsed = json.loads(dyn)
            if isinstance(parsed, dict) and parsed:
                best_u: Optional[str] = None
                best_area = -1
                for url_key, dim in parsed.items():
                    u = str(url_key).strip()
                    if not u.startswith("https://m.media-amazon.com/images/I/"):
                        continue
                    area = 1
                    if isinstance(dim, list) and len(dim) >= 2:
                        try:
                            area = int(dim[0]) * int(dim[1])
                        except (ValueError, TypeError):
                            area = 1
                    if area > best_area:
                        best_area = area
                        best_u = u
                if best_u:
                    return best_u
        except Exception:
            pass
    for attr in ("data-src", "src"):
        v = (img.get(attr) or "").strip()
        if v.startswith("https://m.media-amazon.com/images/I/"):
            return v
    return None


def _image_block_root(soup: BeautifulSoup) -> Optional[Any]:
    """Yalnızca ürün galerisi; sponsor/related ürün bloklarının dışında kalır."""
    for sel in (
        "#imageBlock_feature_div",
        "#imageBlock",
        "#leftCol",
        "#ppd",
    ):
        el = soup.select_one(sel)
        if el is not None and el.select_one("#altImages, #landingImage, #imgTagWrapperId"):
            return el
    return None


def _extract_alt_images_rail_ordered(soup: BeautifulSoup, search_root: Optional[Any] = None) -> list[str]:
    """
    Sol dikey thumbnail (#altImages) — sıra korunur.
    search_root: #imageBlock_feature_div vb.; yoksa yalnızca sayfada bir kez #altImages.
    """
    out: list[str] = []
    seen_url: set[str] = set()
    alt_root = None
    if search_root is not None:
        alt_root = search_root.select_one("#altImages")
    if alt_root is None:
        alt_root = soup.select_one("#altImages")

    if not alt_root:
        return out

    candidates: list[Any] = []
    for sel in (
        "ul.a-unordered-list.a-nostyle > li",
        "ul.a-unordered-list > li",
        "ul > li",
        "li.item",
    ):
        found = alt_root.select(sel)
        if found:
            candidates = [li for li in found if li.find("img")]
            if candidates:
                break

    if not candidates:
        for img in alt_root.select("img"):
            u = _best_url_from_img_tag(img)
            if not u or u in seen_url or _is_low_res_or_overlay_image(u):
                continue
            out.append(u)
            seen_url.add(u)
        return out

    for li in candidates:
        imgs = li.find_all("img")
        best_u: Optional[str] = None
        best_score = -10_000
        for img in imgs:
            u = _best_url_from_img_tag(img)
            if not u or _is_low_res_or_overlay_image(u):
                continue
            sc = _image_priority(u)[0]
            if sc > best_score:
                best_score = sc
                best_u = u
        if best_u and best_u not in seen_url:
            out.append(best_u)
            seen_url.add(best_u)
    return out


def _extract_landing_image_url(search_root: Any) -> Optional[str]:
    for sel in ("#landingImage", "#imgTagWrapperId img#landingImage", "#imgTagWrapperId img"):
        img = search_root.select_one(sel)
        if img:
            u = _best_url_from_img_tag(img)
            if u and not _is_low_res_or_overlay_image(u):
                return u
    return None


def _js_array_inner_after_open_bracket(block: str, open_bracket_idx: int) -> Optional[str]:
    """'...initial': [ ile başlayan dizinin içeriği (köşeli parantez içi), string kaçışlarına saygılı."""
    if open_bracket_idx < 0 or open_bracket_idx >= len(block) or block[open_bracket_idx] != "[":
        return None
    depth = 1
    in_string = False
    quote = ""
    escape = False
    j = open_bracket_idx + 1
    while j < len(block) and depth > 0:
        c = block[j]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_string = False
            j += 1
            continue
        if c in "\"'":
            in_string = True
            quote = c
            j += 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return block[open_bracket_idx + 1 : j]
        j += 1
    return None


def _extract_color_images_initial_hires(html: str) -> list[str]:
    """
    ImageBlockATF içindeki colorImages.initial dizisi: her kare için canonical hiRes URL'leri
    (Amazon sol şeritte renk swatch'ları gösterir; asıl galeri sırası burada).
    """
    ib = re.search(r"ImageBlockATF", html, re.I)
    window = html[ib.start() : ib.start() + 220_000] if ib else html
    m = re.search(
        r"colorImages['\"]\s*:\s*\{\s*['\"]initial['\"]\s*:\s*\[",
        window,
        re.I,
    )
    if not m:
        return []
    open_idx = m.end() - 1
    inner = _js_array_inner_after_open_bracket(window, open_idx)
    if not inner:
        return []
    urls: list[str] = []
    for mm in re.finditer(
        r'\{"hiRes"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"',
        inner,
    ):
        u = mm.group(1).strip()
        if u and not _is_low_res_or_overlay_image(u):
            urls.append(u)
    return urls


def _extract_images_from_product_json(html: str) -> list[str]:
    """
    Sadece ürün galerisiyle ilişkili script alanlarından URL çeker.
    Tüm HTML üzerinde regex ÇALIŞTIRILMAZ (önerilen ürün görselleri karışmasın diye).
    """
    out: list[str] = []
    seen: set[str] = set()
    chunks: list[str] = []
    lowered = html.lower()

    def _slice_at(needle: str) -> None:
        pos = lowered.find(needle.lower())
        if pos != -1:
            chunks.append(html[pos : pos + 450_000])

    for needle in (
        "colorimages",
        "imageblockatf",
        "colorimagestoswatch",
        "imageblockrenderingstart",
        "enableimageblock",
        "landingimageurl",
    ):
        _slice_at(needle)

    if not chunks:
        return out

    pattern = re.compile(
        r"https://m\.media-amazon\.com/images/I/[A-Za-z0-9.%_-]+\.(?:jpg|jpeg|png|webp)",
        re.I,
    )
    for chunk in chunks:
        for m in pattern.finditer(chunk):
            u = m.group(0)
            if _is_low_res_or_overlay_image(u):
                continue
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _ordered_urls_with_best_per_key(urls: list[str]) -> list[str]:
    """Sırayı koru; aynı görsel kimliğinde daha yüksek çözünürlük kazanır."""
    key_order: list[str] = []
    key_to_url: dict[str, str] = {}
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        u = u.strip()
        if not u.startswith("https://m.media-amazon.com/images/I/"):
            continue
        if _is_low_res_or_overlay_image(u):
            continue
        k = _canonical_image_key(u)
        if k not in key_to_url:
            key_order.append(k)
            key_to_url[k] = u
        else:
            prev = key_to_url[k]
            if _image_priority(u) > _image_priority(prev):
                key_to_url[k] = u
    return [key_to_url[k] for k in key_order]


def _best_url_per_key_map(urls: list[str]) -> dict[str, str]:
    """Tüm adaylardan her görsel kimliği için en yüksek öncelikli URL."""
    key_to_url: dict[str, str] = {}
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        u = u.strip()
        if not u.startswith("https://m.media-amazon.com/images/I/"):
            continue
        if _is_low_res_or_overlay_image(u):
            continue
        k = _canonical_image_key(u)
        if k not in key_to_url or _image_priority(u) > _image_priority(key_to_url[k]):
            key_to_url[k] = u
    return key_to_url


def collect_listing_image_urls(soup: BeautifulSoup, html: str) -> list[str]:
    """
    Öncelik: ImageBlockATF colorImages.initial içindeki hiRes dizisi (gerçek galeri sırası).
    Bunun yok olduğu eski düzenlerde: #altImages ana görselleri + hero/meta ile çözünürlük birleştirme.
    """
    initial_hires = _extract_color_images_initial_hires(html)
    if initial_hires:
        return _ordered_urls_with_best_per_key(initial_hires)

    block = _image_block_root(soup)
    rail: list[str] = []
    hero: Optional[str] = None
    if block is not None:
        rail = _extract_alt_images_rail_ordered(soup, search_root=block)
        hero = _extract_landing_image_url(block)
    else:
        rail = _extract_alt_images_rail_ordered(soup, search_root=None)

    _, _, meta_imgs = _extract_from_meta(soup)
    json_urls = _extract_images_from_product_json(html)

    pool: list[str] = list(rail)
    if hero:
        pool.append(hero)
    pool.extend(meta_imgs)
    pool.extend(json_urls)

    key_to_best = _best_url_per_key_map(pool)

    if rail:
        out: list[str] = []
        seen_keys: set[str] = set()
        for u in rail:
            k = _canonical_image_key(u)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            best = key_to_best.get(k, u)
            if _is_low_res_or_overlay_image(best):
                continue
            out.append(best)
        return out

    fallback: list[str] = []
    if hero:
        fallback.append(hero)
    fallback.extend(meta_imgs)
    fallback.extend(json_urls)
    return _ordered_urls_with_best_per_key(fallback)


def _canonical_image_key(url: str) -> str:
    m = re.search(r"/images/I/([^/]+)$", url)
    if not m:
        return url
    filename = m.group(1)
    filename = filename.split("?")[0]
    stem = re.sub(r"\.(jpg|jpeg|png|webp)$", "", filename, flags=re.I)
    if "._" in stem:
        stem = stem.split("._", 1)[0]
    return stem


def _is_low_res_or_overlay_image(url: str) -> bool:
    u = url.lower()
    low_tokens = [
        "_ac_sr38,50_",
        "_sr38,50_",
        "_ss64_",
        "_ss40_",
        "pkmb-play-button-overlay-thumb",
        "play-button-overlay-thumb",
        ".ss125_",
        "_ac_ql10_sx1960_sy110_",
    ]
    return any(t in u for t in low_tokens)


def _image_priority(url: str) -> tuple[int, int]:
    # Etsy için küçük thumbnail'lerden kaçınmak adına büyük görselleri öne al.
    u = url.lower()
    score = 0
    if "_ac_sl" in u or "_sl1500" in u:
        score += 4
    if "_sx" in u and "_sy" in u:
        score += 2
    if "_sr38,50_" in u or "ss125" in u:
        score -= 3
    return (score, len(url))


def _unique_images_prefer_largest(urls: list[str]) -> list[str]:
    unique_by_key: dict[str, str] = {}
    for u in sorted(urls, key=_image_priority, reverse=True):
        if _is_low_res_or_overlay_image(u):
            continue
        key = _canonical_image_key(u)
        if key not in unique_by_key:
            unique_by_key[key] = u
    return list(unique_by_key.values())


def _extract_variations(soup: BeautifulSoup) -> list[dict[str, Any]]:
    variations: list[dict[str, Any]] = []
    selectors = [
        ("Color", "#variation_color_name li, #variation_color_name select option"),
        ("Size", "#variation_size_name li, #variation_size_name select option"),
        ("Style", "#variation_style_name li, #variation_style_name select option"),
    ]
    for name, css in selectors:
        values: list[str] = []
        for node in soup.select(css):
            txt = (node.get_text(" ", strip=True) or "").strip()
            if not txt or txt.lower() in {"select", "choose"}:
                continue
            if txt not in values:
                values.append(txt)
        if values:
            variations.append({"name": name, "values": values})
    return variations


def _extract_variations_from_twister_state(html: str) -> list[dict[str, Any]]:
    # Amazon'ın twister state'inde variationValues alanı en güvenilir kaynaktır.
    m = re.search(
        r'"variationValues"\s*:\s*(\{.*?\})\s*,\s*"selectedVariationValues"',
        html,
        flags=re.DOTALL,
    )
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return out
    for key, values in raw.items():
        if not isinstance(values, list):
            continue
        clean_values = [str(v).strip() for v in values if str(v).strip()]
        if not clean_values:
            continue
        display = str(key).replace("_name", "").replace("_", " ").strip().title()
        if not display:
            display = "Variation"
        out.append({"name": display, "values": clean_values})
    return out


def _extract_color_asin_from_color_to_asin(html: str) -> dict[str, str]:
    """Twister obj.colorToAsin — her renk için tek (ebeveyn) ASIN."""
    out: dict[str, str] = {}
    for m in re.finditer(
        r'"([A-Z][a-zA-Z0-9]*)"\s*:\s*\{\s*"asin"\s*:\s*"([A-Z0-9]{10})"',
        html,
    ):
        color, asin = m.group(1), m.group(2).strip().upper()
        if color.lower() == "initial":
            continue
        out.setdefault(color, asin)
    return out


def _merge_color_asin_maps(
    dimension_fallback: dict[str, str], color_to_asin: dict[str, str]
) -> dict[str, str]:
    """Önce colorToAsin (twister); eksik renkler için dimensionValuesDisplayData."""
    merged = dict(color_to_asin)
    for k, v in dimension_fallback.items():
        merged.setdefault(k, v)
    return merged


def _extract_color_asin_map_from_twister(html: str) -> dict[str, str]:
    key_match = re.search(r'"dimensionValuesDisplayData"\s*:\s*\{', html)
    if not key_match:
        return {}
    start = key_match.end() - 1
    brace = 0
    end = start
    while end < len(html):
        ch = html[end]
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                break
        end += 1
    if end >= len(html):
        return {}
    raw_block = html[start : end + 1]
    try:
        raw = json.loads(raw_block)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, str] = {}
    for asin, vals in raw.items():
        if not isinstance(asin, str) or not isinstance(vals, list) or len(vals) < 2:
            continue
        color = str(vals[1]).strip()
        asin_norm = asin.strip().upper()
        if color and re.fullmatch(r"[A-Z0-9]{10}", asin_norm):
            out.setdefault(color, asin_norm)
    return out


def parse_rendered_html(html: str, source_url: str, extra_jsons: Optional[list[Any]] = None) -> ScrapedListing:
    asin = _asin_from_url(source_url)
    soup = BeautifulSoup(html, "lxml")

    title_meta, desc_meta, imgs_meta = _extract_from_meta(soup)
    title = title_meta.strip()
    if not title:
        t = soup.select_one("#productTitle")
        if t:
            title = t.get_text(" ", strip=True)
    if not title:
        title = f"Amazon Product {asin}"

    bullets = [x.get_text(" ", strip=True) for x in soup.select("#feature-bullets li span.a-list-item")]
    bullets = [b for b in bullets if b and "translate to english" not in b.lower()]
    description = "\n".join(bullets[:12]) if bullets else desc_meta
    if not description:
        description = title

    images = collect_listing_image_urls(soup, html)
    variations = _extract_variations_from_twister_state(html)
    if not variations:
        variations = _extract_variations(soup)

    price_display = None
    currency = None
    price_node = soup.select_one(".a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen")
    if price_node:
        price_display = price_node.get_text(strip=True)
    if not price_display:
        m = re.search(r'\"priceAmount\"\s*:\s*([\d.]+)', html)
        if m:
            price_display = f"${m.group(1)}"
    if price_display:
        upper = price_display.upper()
        if "€" in price_display or "EUR" in upper:
            currency = "EUR"
        elif "£" in price_display or "GBP" in upper:
            currency = "GBP"
        elif "TRY" in upper or "₺" in price_display:
            currency = "TRY"
        else:
            currency = "USD"
    price_min = _digits_price(price_display)

    dvd = _extract_color_asin_map_from_twister(html)
    cta = _extract_color_asin_from_color_to_asin(html)
    debug: dict[str, Any] = {
        "platform": "amazon",
        "extra_json_count": len(extra_jsons or []),
        "variation_source": "twister_state" if variations else None,
        "color_asin_map": _merge_color_asin_maps(dvd, cta) if cta else dvd,
    }
    return ScrapedListing(
        source_url=source_url,
        item_id=asin,
        title=title,
        description_text=description,
        currency=currency,
        price_display=price_display,
        price_min=price_min,
        images=images,
        variations=variations[:2],
        debug=debug,
    )


def scrape_with_playwright(url: str, headless: bool = True, wait_ms: int = 5000) -> ScrapedListing:
    from playwright.sync_api import sync_playwright

    captured_jsons: list[Any] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        def on_response(resp: Any) -> None:
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                if "application/json" in ct:
                    captured_jsons.append(resp.json())
            except Exception:
                return

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()

    listing = parse_rendered_html(html, url, extra_jsons=captured_jsons[:50])
    listing.debug["playwright_json_count"] = len(captured_jsons)
    return listing


def to_draft_dict(listing: ScrapedListing) -> dict[str, Any]:
    return {
        "source": {"platform": "amazon", "url": listing.source_url, "item_id": listing.item_id},
        "title": listing.title,
        "description_text": DEFAULT_ETSY_DESCRIPTION,
        "price_hint": {
            "display": listing.price_display,
            "min": listing.price_min,
            "currency": listing.currency,
        },
        "images": listing.images,
        "variations": listing.variations,
        "debug": listing.debug,
        "notes": "Taslak - Etsy SEO ve fiyat/stok değerlerini gözden geçirin.",
    }

