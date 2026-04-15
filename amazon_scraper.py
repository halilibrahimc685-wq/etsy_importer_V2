"""
Amazon ürün sayfası — PDP görselleri ve Color/twister altındaki child ASIN sayfalarından
görsel birleştirme (tek modül).
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup


def is_amazon_cdn_product_image_url(u: str) -> bool:
    """m.media-amazon.com ve bölgesel ssl-images-amazon.com ürün görseli yolları."""
    u = (u or "").strip()
    if not u.startswith("https://"):
        return False
    try:
        p = urlparse(u)
    except ValueError:
        return False
    path_l = (p.path or "").lower()
    if "/images/i/" not in path_l:
        return False
    host = p.netloc.lower()
    if host == "m.media-amazon.com":
        return True
    if host.endswith(".ssl-images-amazon.com") and host.startswith("images-"):
        return True
    return False


# --- colorImages (tırnak varyantları) ---
_RE_COLOR_IMAGES_OBJ_START = re.compile(
    r'(?i)(?:"colorImages"|\'colorImages\'|colorImages)\s*:\s*\{'
)
_RE_COLOR_IMAGES_INITIAL_ARRAY = re.compile(
    r'(?i)(?:"colorImages"|\'colorImages\'|colorImages)\s*:\s*\{\s*["\']initial["\']\s*:\s*\['
)

_MEDIA_WINDOW_START_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"ImageBlockATF",
        r"imageBlockRenderingStart",
        r"enableImageBlock",
        r"imageBlockRenderingStartData",
        r"id=[\"']imageBlock_feature_div[\"']",
        r"id=[\"']imageBlock[\"']",
        r"imgTagWrapperId",
        r"landingImageUrl",
        r"data-old-hires\s*=",
        r"booksImageBlock",
        r"booksImageBlock_feature_div",
        r"imgBlkFront",
        r"ebooksImgBlkFront",
        r"main-image-container",
        r"mobile-image-container",
        r"ivImagesFeature",
        r"twister_feature_div",
        r"variationTwister",
    )
)
_PPD_FALLBACK = re.compile(r'id=[\"\'](?:ppd|centerCol|leftCol|desktop_hero)[\"\']', re.I)

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)
_MAX_VARIANT_ASINS = 72
_MAX_FETCH_WORKERS = 8
# Her renk/varyant ASIN sayfasından en fazla bu kadar görsel (sıra: Amazon galeri sırası).
_MAX_IMAGES_PER_VARIANT_ASIN = 4
_KW_MIN_LEN = 3
_KW_MAX_COUNT = 20
_KW_STOPWORDS = {
    "and", "the", "for", "with", "from", "this", "that", "your", "you", "are",
    "our", "its", "into", "onto", "about", "than", "then", "these", "those",
    "they", "them", "their", "will", "would", "have", "has", "had", "not",
    "new", "all", "any", "can", "use", "used", "using", "out", "off", "via",
    "size", "sizes", "color", "colors", "style", "styles", "pack",
}


def fetch_amazon_html(url: str, *, timeout: float = 60.0) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def asin_from_url(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/dp/([A-Z0-9]{10})", path, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"/gp/product/([A-Z0-9]{10})", path, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"([A-Z0-9]{10})", path, re.I)
    return m.group(1).upper() if m else "UNKNOWN_ASIN"


def canonical_amazon_dp_url(url: str) -> str:
    """
    Takip parametreleri (pd_rd_, ref_, th=, psc=) bazen bot/kısa sayfa döndürür.
    Şemayı ve hostu koruyarak yalnızca /dp/ASIN adresine indirger.
    """
    u = (url or "").strip()
    if not u:
        return u
    p = urlparse(u)
    path = p.path or ""
    m = re.search(r"/dp/([A-Z0-9]{10})", path, re.I)
    if not m:
        m = re.search(r"/gp/product/([A-Z0-9]{10})", path, re.I)
    if not m:
        return u.split("#")[0]
    asin = m.group(1).upper()
    scheme = p.scheme if p.scheme in ("http", "https") else "https"
    netloc = (p.netloc or "").strip().lower()
    if not netloc:
        netloc = "www.amazon.com"
    return f"{scheme}://{netloc}/dp/{asin}"


def dp_url_for_asin(asin: str, *, scheme: str, netloc: str) -> str:
    return f"{scheme}://{netloc}/dp/{asin}"


def _slice_balanced_braces(block: str, open_brace_idx: int) -> Optional[str]:
    if open_brace_idx < 0 or open_brace_idx >= len(block) or block[open_brace_idx] != "{":
        return None
    depth = 0
    in_string = False
    quote = ""
    escape = False
    i = open_brace_idx
    while i < len(block):
        c = block[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == quote:
                in_string = False
            i += 1
            continue
        if c in "\"'":
            in_string = True
            quote = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return block[open_brace_idx : i + 1]
        i += 1
    return None


def _js_array_inner_after_open_bracket(block: str, open_bracket_idx: int) -> Optional[str]:
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


def _canonical_image_key(url: str) -> str:
    m = re.search(r"/images/I/([^/?#]+)", url, re.I)
    if not m:
        return url
    filename = m.group(1).split("?")[0]
    stem = re.sub(r"\.(jpg|jpeg|png|webp)$", "", filename, flags=re.I)
    if "._" in stem:
        stem = stem.split("._", 1)[0]
    return stem


def _is_low_res_or_overlay_image(url: str) -> bool:
    u = url.lower()
    low_tokens = (
        "_ac_sr38,50_",
        "_sr38,50_",
        "_ss64_",
        "_ss40_",
        "pkmb-play-button-overlay-thumb",
        "play-button-overlay-thumb",
        ".ss125_",
        "_ac_ql10_sx1960_sy110_",
    )
    return any(t in u for t in low_tokens)


def _image_priority(url: str) -> tuple[int, int]:
    u = url.lower()
    score = 0
    if "_ac_sl" in u or "_sl1500" in u:
        score += 4
    if "_sx" in u and "_sy" in u:
        score += 2
    if "_sr38,50_" in u or "ss125" in u:
        score -= 3
    return (score, len(url))


def _ordered_urls_with_best_per_key(urls: list[str]) -> list[str]:
    key_order: list[str] = []
    key_to_url: dict[str, str] = {}
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        u = u.strip()
        if not is_amazon_cdn_product_image_url(u):
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


def _best_url_from_img_tag(img: Any) -> Optional[str]:
    if not img:
        return None
    hires = (img.get("data-old-hires") or "").strip()
    if is_amazon_cdn_product_image_url(hires):
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
                    if not is_amazon_cdn_product_image_url(u):
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
        if is_amazon_cdn_product_image_url(v):
            return v
    return None


def _best_media_url_from_amazon_image_variant(d: dict[str, Any]) -> Optional[str]:
    candidates: list[tuple[int, str]] = []
    for k, v in d.items():
        if not isinstance(k, str):
            continue
        ku = k.strip()
        if not is_amazon_cdn_product_image_url(ku):
            continue
        area = 0
        if isinstance(v, list) and len(v) >= 2:
            try:
                area = int(v[0]) * int(v[1])
            except (TypeError, ValueError):
                area = 0
        candidates.append((area, ku))
    chosen: Optional[str] = None
    if candidates:
        chosen = sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]
    if not chosen:
        lg = d.get("large")
        if isinstance(lg, str) and is_amazon_cdn_product_image_url(lg.strip()):
            chosen = lg.strip()
    if not chosen:
        hr = d.get("hiRes")
        if isinstance(hr, str) and is_amazon_cdn_product_image_url(hr.strip()):
            chosen = hr.strip()
    if chosen and not _is_low_res_or_overlay_image(chosen):
        return chosen
    return None


def _hires_from_amazon_color_image_item(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    hit = _best_media_url_from_amazon_image_variant(item)
    if hit:
        return hit
    main = item.get("main")
    if isinstance(main, dict):
        return _best_media_url_from_amazon_image_variant(main)
    return None


def _top_level_curly_object_strings(inner: str) -> list[str]:
    objects: list[str] = []
    depth = 0
    in_string = False
    quote = ""
    escape = False
    start = -1
    for i, ch in enumerate(inner):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(inner[start : i + 1])
                start = -1
    return objects


def _hires_url_from_color_image_object_literal(obj: str) -> Optional[str]:
    if '"hiRes"' not in obj and '"large"' not in obj and "media-amazon.com/images/I/" not in obj:
        return None
    candidates: list[tuple[int, str]] = []
    for m2 in re.finditer(
        r'"(https://[a-z0-9.-]*amazon\.com/images/I/[^"]+)"\s*:\s*\[(\d+)\s*,\s*(\d+)\]',
        obj,
    ):
        u = m2.group(1).strip()
        if _is_low_res_or_overlay_image(u):
            continue
        try:
            area = int(m2.group(2)) * int(m2.group(3))
        except Exception:
            area = 0
        candidates.append((area, u))
    chosen: Optional[str] = None
    if candidates:
        chosen = sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]
    if not chosen:
        m3 = re.search(r'"large"\s*:\s*"(https://[a-z0-9.-]*amazon\.com/images/I/[^"]+)"', obj)
        if m3:
            chosen = m3.group(1).strip()
    if not chosen:
        m4 = re.search(r'"hiRes"\s*:\s*"(https://[a-z0-9.-]*amazon\.com/images/I/[^"]+)"', obj)
        if m4:
            chosen = m4.group(1).strip()
    if chosen and not _is_low_res_or_overlay_image(chosen):
        return chosen
    return None


def _urls_from_color_images_object_literal(obj_str: str) -> list[str]:
    out: list[str] = []
    seen_keys: set[str] = set()
    for pat in (
        re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*\['),
        re.compile(r"'((?:[^'\\]|\\.)*)'\s*:\s*\["),
    ):
        for m in pat.finditer(obj_str):
            open_b = m.end() - 1
            if open_b >= len(obj_str) or obj_str[open_b] != "[":
                continue
            inner = _js_array_inner_after_open_bracket(obj_str, open_b)
            if not inner:
                continue
            for sub in _top_level_curly_object_strings(inner):
                u = _hires_url_from_color_image_object_literal(sub)
                if not u:
                    continue
                ck = _canonical_image_key(u)
                if ck in seen_keys:
                    continue
                seen_keys.add(ck)
                out.append(u)
    return out


def _urls_from_single_color_images_object(obj_str: str) -> list[str]:
    try:
        data = json.loads(obj_str)
    except json.JSONDecodeError:
        return _urls_from_color_images_object_literal(obj_str)
    if not isinstance(data, dict):
        return _urls_from_color_images_object_literal(obj_str)

    def _sk(k: Any) -> tuple[int, str]:
        ks = str(k)
        return (0, ks) if ks == "initial" else (1, ks)

    urls: list[str] = []
    for key in sorted(data.keys(), key=_sk):
        val = data[key]
        if not isinstance(val, list):
            continue
        for item in val:
            u = _hires_from_amazon_color_image_item(item)
            if u:
                urls.append(u)
    if urls:
        return urls
    return _urls_from_color_images_object_literal(obj_str)


def _extract_all_color_images_hires(html: str) -> list[str]:
    slices: list[str] = []
    for m in _RE_COLOR_IMAGES_OBJ_START.finditer(html):
        obj_str = _slice_balanced_braces(html, m.end() - 1)
        if obj_str:
            slices.append(obj_str)
    if not slices:
        return []

    def _prio(s: str) -> tuple[int, int]:
        hi = "'initial'" in s or '"initial"' in s
        return (0 if hi else 1, -len(s))

    slices.sort(key=_prio)
    out: list[str] = []
    seen: set[str] = set()
    for obj_str in slices:
        for u in _urls_from_single_color_images_object(obj_str):
            ck = _canonical_image_key(u)
            if ck in seen:
                continue
            seen.add(ck)
            out.append(u)
    return out


def _extract_color_images_initial_hires(html: str) -> list[str]:
    ib = re.search(r"ImageBlockATF", html, re.I)
    window = html[ib.start() : ib.start() + 220_000] if ib else html
    m = _RE_COLOR_IMAGES_INITIAL_ARRAY.search(window)
    ctx = window
    if not m:
        m = _RE_COLOR_IMAGES_INITIAL_ARRAY.search(html)
        ctx = html
    if not m:
        return []
    inner = _js_array_inner_after_open_bracket(ctx, m.end() - 1)
    if not inner:
        return []
    urls: list[str] = []
    for obj in _top_level_curly_object_strings(inner):
        u = _hires_url_from_color_image_object_literal(obj)
        if u:
            urls.append(u)
    return urls


def _product_media_window_start(html: str) -> Optional[int]:
    positions: list[int] = []
    for pat in _MEDIA_WINDOW_START_PATTERNS:
        m = pat.search(html)
        if m:
            positions.append(m.start())
    m_ci = _RE_COLOR_IMAGES_OBJ_START.search(html)
    if m_ci:
        positions.append(max(0, m_ci.start() - 8_000))
    if not positions:
        m2 = _PPD_FALLBACK.search(html)
        if m2:
            positions.append(m2.start())
    return min(positions) if positions else None


def _extract_urls_from_media_window(html: str, *, max_chars: int = 900_000) -> list[str]:
    start = _product_media_window_start(html)
    if start is None:
        return []
    chunk = html[start : start + max_chars]
    pat = re.compile(
        r"https://[a-z0-9.-]*amazon\.com/images/I/[A-Za-z0-9.%_+\-]+\.(?:jpg|jpeg|png|webp)",
        re.I,
    )
    out: list[str] = []
    seen: set[str] = set()
    for u in pat.findall(chunk):
        u = u.strip()
        if _is_low_res_or_overlay_image(u) or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _dom_gallery_urls(soup: BeautifulSoup) -> list[str]:
    selectors = (
        "#imageBlock_feature_div",
        "#imageBlock",
        "#imageBlockNew",
        "#booksImageBlock_feature_div",
        "#booksImageBlock",
        "#imgBlkFront",
        "#ebooksImgBlkFront",
        "#ivImages_feature_div",
        "#leftCol",
    )
    seen: set[str] = set()
    out: list[str] = []
    for sel in selectors:
        root = soup.select_one(sel)
        if not root:
            continue
        for img in root.select("img"):
            u = _best_url_from_img_tag(img)
            if not u or u in seen or _is_low_res_or_overlay_image(u):
                continue
            seen.add(u)
            out.append(u)
    return out


def _extract_images_from_product_json(html: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    lowered = html.lower()
    chunks: list[str] = []

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
        "imgtagwrapperid",
        "altimages",
        "a-dynamic-image",
        "booksimageblock",
        "imgblkfront",
        "imagegallery",
        "ivimage",
        "twister_feature_div",
    ):
        _slice_at(needle)

    if not chunks:
        return out
    pat = re.compile(
        r"https://[a-z0-9.-]*amazon\.com/images/I/[A-Za-z0-9.%_-]+\.(?:jpg|jpeg|png|webp)",
        re.I,
    )
    for chunk in chunks:
        for m in pat.finditer(chunk):
            u = m.group(0)
            if _is_low_res_or_overlay_image(u) or u in seen:
                continue
            seen.add(u)
            out.append(u)
    return out


def extract_pdp_image_urls(html: str) -> list[str]:
    """Tek bir ürün detay HTML'inden mümkün olan tüm galeri URL'leri."""
    soup = BeautifulSoup(html, "lxml")
    structured = _extract_all_color_images_hires(html)
    if not structured:
        structured = _extract_color_images_initial_hires(html)
    embedded = _extract_urls_from_media_window(html)
    dom_urls = _dom_gallery_urls(soup)
    merged = structured + embedded + dom_urls
    if len(merged) < 8:
        merged = merged + _extract_images_from_product_json(html)
    if not merged:
        block = soup.select_one("#imageBlock_feature_div, #imageBlock, #leftCol")
        rail: list[str] = []
        if block:
            alt = block.select_one("#altImages")
            if alt:
                for img in alt.select("img"):
                    u = _best_url_from_img_tag(img)
                    if u and not _is_low_res_or_overlay_image(u):
                        rail.append(u)
        hero = None
        if block:
            for sel in ("#landingImage", "#imgTagWrapperId img"):
                im = block.select_one(sel)
                if im:
                    hero = _best_url_from_img_tag(im)
                    if hero:
                        break
        _, _, meta_imgs = _extract_from_meta(soup)
        pool = rail + ([hero] if hero else []) + meta_imgs
        merged = [u for u in pool if isinstance(u, str) and u.strip()]
    return _ordered_urls_with_best_per_key(merged)


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
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for key, values in raw.items():
        if not isinstance(values, list):
            continue
        clean_values = [str(v).strip() for v in values if str(v).strip()]
        if not clean_values:
            continue
        display = str(key).replace("_name", "").replace("_", " ").strip().title()
        out.append({"name": display or "Variation", "values": clean_values})
    return out


def _extract_keywords(title: str, bullets: list[str], description: str) -> list[str]:
    text = " ".join([title] + bullets + [description]).lower()
    if not text:
        return []
    words = re.findall(r"[a-z0-9][a-z0-9'\-]{1,}", text)
    freq: dict[str, int] = {}
    first_pos: dict[str, int] = {}
    for idx, w in enumerate(words):
        if len(w) < _KW_MIN_LEN:
            continue
        if w.isdigit():
            continue
        if w in _KW_STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1
        first_pos.setdefault(w, idx)
    if not freq:
        return []
    ordered = sorted(freq.keys(), key=lambda x: (-freq[x], first_pos.get(x, 10_000_000), x))
    return ordered[:_KW_MAX_COUNT]


def _extract_color_asin_from_color_to_asin(html: str) -> dict[str, str]:
    m = re.search(r'"colorToAsin"\s*:\s*\{', html)
    if not m:
        return {}
    obj_str = _slice_balanced_braces(html, m.end() - 1)
    if not obj_str:
        return {}
    try:
        raw = json.loads(obj_str)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for color_key, payload in raw.items():
        ck = str(color_key).strip()
        if ck.lower() == "initial":
            continue
        if not isinstance(payload, dict):
            continue
        asin_v = payload.get("asin")
        if isinstance(asin_v, str) and _ASIN_RE.fullmatch(asin_v.strip().upper()):
            out.setdefault(ck, asin_v.strip().upper())
    return out


def _parse_dimension_values_display_data(html: str) -> dict[str, Any]:
    key_match = re.search(r'"dimensionValuesDisplayData"\s*:\s*\{', html)
    if not key_match:
        return {}
    raw_block = _slice_balanced_braces(html, key_match.end() - 1)
    if not raw_block:
        return {}
    try:
        raw = json.loads(raw_block)
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _color_asin_map_from_dimension_raw(raw: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for asin, vals in raw.items():
        if not isinstance(asin, str) or not isinstance(vals, list) or not vals:
            continue
        color = str(vals[-1]).strip()
        a = asin.strip().upper()
        if color and _ASIN_RE.fullmatch(a):
            out.setdefault(color, a)
    return out


def _child_variant_asins_from_dimension_raw(raw: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for asin in raw:
        if isinstance(asin, str):
            a = asin.strip().upper()
            if _ASIN_RE.fullmatch(a):
                found.append(a)
    return sorted(set(found))


def _merge_color_asin_maps(dvd: dict[str, str], cta: dict[str, str]) -> dict[str, str]:
    merged = dict(cta)
    for k, v in dvd.items():
        merged.setdefault(k, v)
    return merged


def _child_asins_from_nested_json(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        nested = obj.get("dimensionValuesDisplayData")
        if isinstance(nested, dict):
            for k in nested:
                if isinstance(k, str):
                    a = k.strip().upper()
                    if _ASIN_RE.fullmatch(a):
                        out.add(a)
        for v in obj.values():
            out |= _child_asins_from_nested_json(v)
    elif isinstance(obj, list):
        for x in obj:
            out |= _child_asins_from_nested_json(x)
    return out


def _extract_asins_from_twister_markup(html: str) -> set[str]:
    """JSON kaçarsa twister DOM'daki data-asin yedekleri (yalnızca varyasyon kutusu içi)."""
    if not html or len(html) < 8_000:
        return set()
    soup = BeautifulSoup(html, "lxml")
    out: set[str] = set()
    roots = soup.select(
        "#twister_feature_div, #variation_twister, #twister, "
        "[id*='variation'][id*='twister']"
    )
    if not roots:
        return set()
    seen_ids: set[int] = set()
    for root in roots:
        rid = id(root)
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        for tag in root.select("[data-asin]"):
            a = (tag.get("data-asin") or "").strip().upper()
            if _ASIN_RE.fullmatch(a):
                out.add(a)
    return out


def _regex_asins_near_color_to_asin(html: str) -> set[str]:
    """Bozuk JSON'da colorToAsin bloğundan ASIN yakalama."""
    m = re.search(r'(?i)"colorToAsin"\s*:\s*\{', html)
    if not m:
        return set()
    chunk = html[m.start() : m.start() + 120_000]
    found: set[str] = set()
    for m2 in re.finditer(
        r'(?i)"asin"\s*:\s*"([A-Z0-9]{10})"',
        chunk,
    ):
        a = m2.group(1).upper()
        if _ASIN_RE.fullmatch(a):
            found.add(a)
    return found


def is_usable_amazon_pdp_html(html: str) -> bool:
    """Kısıtlı bot / doğrulama sayfasını PDP HTML'inden ayır (Playwright önbelleği için)."""
    if not html or not isinstance(html, str):
        return False
    if len(html) < 25_000:
        return False
    h = html[:120_000].lower()
    if "continue shopping" in h and "producttitle" not in h.replace(" ", ""):
        return False
    return (
        "producttitle" in h.replace(" ", "")
        or "imageblockatf" in h
        or "colorimages" in h.replace(" ", "")
        or "twister_feature_div" in h
    )


def compact_asins_for_color_galleries(
    ordered_asins: list[str],
    color_asin_map: dict[str, str],
    base_asin: str,
) -> tuple[list[str], bool]:
    """
    Beden×renk matrisinde her beden için aynı renk galerisi tekrarlanır; tüm ASIN'leri
    taramak hem yavaş hem de ardışık isteklerde Amazon'un kısıtlamasına yol açabilir.
    color_asin_map doluysa ve gerçekten küçültme kazancı varsa yalnızca
    (taban ASIN + her renk için bir temsilci) döndürülür.
    """
    if not ordered_asins or not color_asin_map:
        return ordered_asins, False
    b0 = base_asin.strip().upper()
    if not _ASIN_RE.fullmatch(b0):
        b0 = ordered_asins[0].strip().upper()
    seen: set[str] = set()
    out: list[str] = []
    if _ASIN_RE.fullmatch(b0):
        seen.add(b0)
        out.append(b0)
    for _c, asin in sorted(color_asin_map.items(), key=lambda x: str(x[0]).lower()):
        a = str(asin).strip().upper()
        if not _ASIN_RE.fullmatch(a) or a in seen:
            continue
        seen.add(a)
        out.append(a)
    if len(out) >= len(ordered_asins):
        return ordered_asins, False
    return out, True


def collect_variant_asins_to_fetch(
    product_url: str,
    html: str,
    *,
    extra_jsons: Optional[list[Any]] = None,
) -> tuple[list[str], dict[str, Any]]:
    """
    Tüm Color / twister child ASIN'leri — tekrarsız sıra: önce URL'deki ASIN, sonra colorToAsin, sonra matris.
    """
    p = urlparse(product_url)
    scheme = p.scheme or "https"
    netloc = p.netloc or "www.amazon.com"
    base = asin_from_url(product_url)

    cta = _extract_color_asin_from_color_to_asin(html)
    dvd_raw = _parse_dimension_values_display_data(html)
    dvd = _color_asin_map_from_dimension_raw(dvd_raw)
    color_merged = _merge_color_asin_maps(dvd, cta)

    child: set[str] = set(_child_variant_asins_from_dimension_raw(dvd_raw))
    for ej in extra_jsons or []:
        child |= _child_asins_from_nested_json(ej)
    child |= _extract_asins_from_twister_markup(html)
    child |= _regex_asins_near_color_to_asin(html)

    ordered: list[str] = []
    seen: set[str] = set()

    def add(a: str) -> None:
        a = a.strip().upper()
        if not _ASIN_RE.fullmatch(a) or a in seen:
            return
        seen.add(a)
        ordered.append(a)

    add(base)
    for _name, asin in sorted(color_merged.items(), key=lambda x: x[0].lower()):
        add(asin)
    for a in sorted(child):
        add(a)

    if len(ordered) > _MAX_VARIANT_ASINS:
        keep = set(ordered[:_MAX_VARIANT_ASINS])
        if base not in keep:
            keep.add(base)
        ordered = [x for x in ordered if x in keep][: _MAX_VARIANT_ASINS]

    debug = {
        "color_asin_map": color_merged,
        "child_variant_asins": sorted(child),
        "asins_fetched_for_images": list(ordered),
        "fetch_scheme": scheme,
        "fetch_netloc": netloc,
        "all_variant_images_merged": True,
    }
    return ordered, debug


def gather_all_listing_images(
    product_url: str,
    initial_html: str,
    *,
    extra_jsons: Optional[list[Any]] = None,
    html_by_asin: Optional[dict[str, str]] = None,
    max_images_per_variant_asin: Optional[int] = None,
) -> tuple[list[str], dict[str, Any]]:
    """
    Verilen listing + tüm renk/varyant ASIN sayfalarından görselleri birleştirir.
    html_by_asin: {asin: html} önbellek (test); yoksa HTTP ile çekilir.
    max_images_per_variant_asin: Her ASIN için en fazla bu kadar görsel (varsayılan 4).
        0 veya negatif = sınır yok (tüm galeri).
    """
    asins, vdebug = collect_variant_asins_to_fetch(product_url, initial_html, extra_jsons=extra_jsons)
    full_asins = list(asins)
    cmap = vdebug.get("color_asin_map")
    color_map: dict[str, str] = cmap if isinstance(cmap, dict) else {}
    base_a = asin_from_url(product_url)
    asins, compacted = compact_asins_for_color_galleries(asins, color_map, base_a)
    vdebug["asins_all_discovered"] = full_asins
    vdebug["asins_fetched_for_images"] = list(asins)
    vdebug["compacted_color_gallery_asins"] = compacted

    scheme = str(vdebug.get("fetch_scheme") or "https")
    netloc = str(vdebug.get("fetch_netloc") or "www.amazon.com")

    cap = (
        max_images_per_variant_asin
        if max_images_per_variant_asin is not None
        else _MAX_IMAGES_PER_VARIANT_ASIN
    )
    vdebug["max_images_per_variant_asin"] = cap

    cache: dict[str, str] = dict(html_by_asin or {})
    if asins:
        cache.setdefault(asins[0], initial_html)

    raw_pool: list[str] = []

    def html_for(asin: str) -> str:
        if asin in cache:
            cached = cache[asin]
            if is_usable_amazon_pdp_html(cached):
                return cached
        return fetch_amazon_html(dp_url_for_asin(asin, scheme=scheme, netloc=netloc))

    def urls_for_page(html: str) -> list[str]:
        found = extract_pdp_image_urls(html)
        if cap > 0:
            return found[:cap]
        return found

    if len(asins) <= 1:
        raw_pool.extend(urls_for_page(initial_html))
    else:
        with ThreadPoolExecutor(max_workers=min(_MAX_FETCH_WORKERS, len(asins))) as pool:
            futures = {pool.submit(html_for, a): a for a in asins}
            asin_html: dict[str, str] = {}
            for fut in as_completed(futures):
                a = futures[fut]
                try:
                    asin_html[a] = fut.result()
                except Exception:
                    asin_html[a] = ""
            for a in asins:
                h = asin_html.get(a) or ""
                if not h:
                    continue
                raw_pool.extend(urls_for_page(h))

    images = _ordered_urls_with_best_per_key(raw_pool)
    return images, vdebug


def extract_listing_metadata(html: str, source_url: str) -> dict[str, Any]:
    """Başlık, açıklama, fiyat, varyasyonlar (tek sayfa)."""
    asin = asin_from_url(source_url)
    soup = BeautifulSoup(html, "lxml")
    title_meta, desc_meta, _ = _extract_from_meta(soup)
    title = title_meta.strip()
    if not title:
        t = soup.select_one("#productTitle")
        if t:
            title = t.get_text(" ", strip=True)
    if not title:
        title = f"Amazon Product {asin}"

    bullets = [
        x.get_text(" ", strip=True)
        for x in soup.select("#feature-bullets li span.a-list-item")
    ]
    bullets = [b for b in bullets if b and "translate to english" not in b.lower()]
    description = "\n".join(bullets[:12]) if bullets else desc_meta
    if not description:
        description = title
    keywords = _extract_keywords(title, bullets[:20], description)

    variations = _extract_variations_from_twister_state(html)
    if not variations:
        variations = _extract_variations(soup)

    price_display = None
    currency = None
    price_node = soup.select_one(
        ".a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen"
    )
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

    return {
        "asin": asin,
        "title": title,
        "description_text": description,
        "currency": currency,
        "price_display": price_display,
        "price_min": price_min,
        "variations": variations[:2],
        "keywords": keywords,
    }
