"""Amazon ürün → ScrapedListing. Görseller amazon_scraper ile (tüm renk ASIN'leri dahil)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import amazon_scraper

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
    keywords: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)


def collect_listing_image_urls(soup: Any, html: str) -> list[str]:
    """Tek sayfa görselleri (Eski API). Çoklu ASIN birleştirme için parse_rendered_html kullanın."""
    return amazon_scraper.extract_pdp_image_urls(html)


def parse_rendered_html(
    html: str,
    source_url: str,
    extra_jsons: Optional[list[Any]] = None,
    html_by_asin: Optional[dict[str, str]] = None,
    max_images_per_variant_asin: Optional[int] = None,
) -> ScrapedListing:
    meta = amazon_scraper.extract_listing_metadata(html, source_url)
    images, vdebug = amazon_scraper.gather_all_listing_images(
        source_url,
        html,
        extra_jsons=extra_jsons,
        html_by_asin=html_by_asin,
        max_images_per_variant_asin=max_images_per_variant_asin,
    )
    debug: dict[str, Any] = {
        "platform": "amazon",
        "extra_json_count": len(extra_jsons or []),
        "variation_source": "twister_state" if meta["variations"] else None,
        **vdebug,
    }
    return ScrapedListing(
        source_url=source_url,
        item_id=meta["asin"],
        title=meta["title"],
        description_text=meta["description_text"],
        currency=meta["currency"],
        price_display=meta["price_display"],
        price_min=meta["price_min"],
        images=images,
        variations=meta["variations"],
        keywords=meta.get("keywords") if isinstance(meta.get("keywords"), list) else [],
        debug=debug,
    )


def scrape_with_playwright(
    url: str,
    headless: bool = True,
    wait_ms: int = 2200,
    variant_wait_ms: int = 1600,
    goto_timeout_ms: int = 35000,
    max_images_per_variant_asin: Optional[int] = None,
) -> ScrapedListing:
    """
    İlk sayfa + colorToAsin/twister ile bulunan child ASIN sayfaları aynı tarayıcı oturumunda
    yüklenir. Ek ASIN'ler için urllib kullanılamaz — Amazon bot sayfası döndürür; görseller
    birleşmezdi.
    """
    from playwright.sync_api import sync_playwright

    captured_jsons: list[Any] = []
    html_by_asin: dict[str, str] = {}
    variant_errors: list[str] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless)
        except Exception as exc:
            msg = str(exc).lower()
            if "executable doesn't exist" not in msg:
                raise
            try:
                browser = p.chromium.launch(channel="chrome", headless=headless)
            except Exception:
                raise exc
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
        page.goto(url, wait_until="load", timeout=goto_timeout_ms)
        try:
            page.wait_for_selector("#productTitle, #title", timeout=8000)
        except Exception:
            pass
        try:
            tw = page.locator("#twister_feature_div").first
            if tw.count() > 0:
                tw.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)
        html = page.content()

        extras = captured_jsons[:50]
        asins_full, vdbg = amazon_scraper.collect_variant_asins_to_fetch(
            url, html, extra_jsons=extras
        )
        cmap = vdbg.get("color_asin_map")
        color_map = cmap if isinstance(cmap, dict) else {}
        base_a = amazon_scraper.asin_from_url(url)
        asins, _ = amazon_scraper.compact_asins_for_color_galleries(
            asins_full, color_map, base_a
        )
        pu = urlparse(url)
        scheme = pu.scheme if pu.scheme in ("http", "https") else "https"
        netloc = (pu.netloc or "").strip().lower() or "www.amazon.com"

        if len(asins) > 1:
            for asin in asins[1:]:
                dp = amazon_scraper.dp_url_for_asin(asin, scheme=scheme, netloc=netloc)
                try:
                    page.goto(dp, wait_until="load", timeout=goto_timeout_ms)
                    page.wait_for_timeout(variant_wait_ms)
                    html_by_asin[asin] = page.content()
                except Exception as exc:
                    variant_errors.append(f"{asin}: {exc}")

        browser.close()

    listing = parse_rendered_html(
        html,
        url,
        extra_jsons=extras,
        html_by_asin=html_by_asin or None,
        max_images_per_variant_asin=max_images_per_variant_asin,
    )
    listing.debug["playwright_json_count"] = len(captured_jsons)
    listing.debug["playwright_variant_pages_fetched"] = len(html_by_asin)
    if variant_errors:
        listing.debug["playwright_variant_errors"] = variant_errors
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
        "keywords": listing.keywords,
        "tags": listing.keywords[:13],
        "debug": listing.debug,
        "notes": "Taslak - Etsy SEO ve fiyat/stok değerlerini gözden geçirin.",
    }
