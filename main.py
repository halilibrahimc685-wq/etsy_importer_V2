#!/usr/bin/env python3
"""Amazon -> yerel draft -> (opsiyonel) Etsy draft upload CLI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from etsy_client import (
    create_draft_listing,
    delete_listing_image,
    get_listing_images,
    normalize_listing_who_when_supply,
    update_existing_listing,
    update_listing_inventory,
    upload_listing_image_from_url,
)
from bs4 import BeautifulSoup

from scraper import (
    collect_listing_image_urls,
    parse_rendered_html,
    scrape_with_playwright,
    to_draft_dict,
)


def fetch_html_simple(url: str) -> str:
    """Amazon bazen python-httpx istemcisini kısıtlı HTML ile yanıtlıyor; urllib daha stabil."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=60.0) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _validate_amazon_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise RuntimeError("URL http/https olmalı.")
    host = (p.netloc or "").lower()
    if "amazon." not in host:
        raise RuntimeError("Bu sürüm yalnızca Amazon ürün URL'leri için yapılandırıldı.")


def _augment_images_with_color_variants(draft: dict[str, object]) -> None:
    debug = draft.get("debug")
    if not isinstance(debug, dict):
        return
    color_asin_map = debug.get("color_asin_map")
    if not isinstance(color_asin_map, dict) or not color_asin_map:
        return

    images = draft.get("images")
    if not isinstance(images, list):
        images = []
        draft["images"] = images

    seen: set[str] = {str(x).strip() for x in images if isinstance(x, str)}
    base_asin = ""
    src = draft.get("source")
    if isinstance(src, dict) and src.get("item_id"):
        base_asin = str(src["item_id"]).strip().upper()

    for _, asin in color_asin_map.items():
        asin_s = str(asin).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin_s):
            continue
        if base_asin and asin_s == base_asin:
            continue
        try:
            html = fetch_html_simple(f"https://www.amazon.com/dp/{asin_s}")
            soup = BeautifulSoup(html, "lxml")
            for u in collect_listing_image_urls(soup, html):
                if u and u not in seen:
                    images.append(u)
                    seen.add(u)
        except Exception:
            continue


def _upload_images_best_effort(listing_id: int, images: list[str]) -> None:
    uploaded = 0
    failed = 0
    seen: set[str] = set()
    rank = 1
    for img_url in images:
        if not isinstance(img_url, str) or not img_url.strip():
            continue
        url = img_url.strip()
        if url in seen:
            continue
        seen.add(url)
        try:
            upload_listing_image_from_url(listing_id, url, rank=rank, overwrite=True)
            uploaded += 1
            rank += 1
            print(f"Görsel yüklendi ({uploaded}): {url}")
        except Exception as ex:
            failed += 1
            msg = str(ex)
            print(f"Görsel atlandı ({failed}): {msg}", file=sys.stderr)
            low = msg.lower()
    if uploaded > 0:
        print(f"Toplam görsel yüklendi: {uploaded}")
    elif images:
        print("Hiç görsel yüklenemedi.", file=sys.stderr)


def _clear_listing_images(listing_id: int) -> None:
    try:
        existing = get_listing_images(listing_id)
    except Exception as ex:
        print(f"Mevcut görseller alınamadı: {ex}", file=sys.stderr)
        return
    deleted = 0
    for item in existing:
        image_id = item.get("listing_image_id")
        if not isinstance(image_id, int):
            continue
        try:
            delete_listing_image(listing_id, image_id)
            deleted += 1
        except Exception as ex:
            print(f"Görsel silinemedi ({image_id}): {ex}", file=sys.stderr)
    if deleted:
        print(f"Eski görseller silindi: {deleted}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon -> yerel taslak (+ opsiyonel Etsy draft)")
    parser.add_argument("url", nargs="?", default=None, help="Amazon ürün URL'si (draft-json yoksa gerekli)")
    parser.add_argument("--no-playwright", action="store_true", help="Sadece düz HTTP ile HTML al")
    parser.add_argument("--out-dir", type=Path, default=Path("drafts"), help="JSON taslak klasörü")
    parser.add_argument("--etsy", action="store_true", help="Etsy'de draft listing oluştur")
    parser.add_argument(
        "--etsy-update-listing-id",
        type=int,
        default=None,
        help="Mevcut Etsy listing ID'sini update et (create yerine update).",
    )
    parser.add_argument(
        "--apply-variations",
        action="store_true",
        help="Draft JSON içindeki varyasyonları listing inventory'ye uygula (max 2 varyasyon).",
    )
    parser.add_argument("--draft-json", type=Path, default=None, help="Var olan taslak JSON dosyasını Etsy'ye gönder")
    parser.add_argument("--var1-name", type=str, default=None, help="Varyasyon 1 adı (örn Color)")
    parser.add_argument("--var1-values", type=str, default=None, help="Varyasyon 1 değerleri, virgülle (örn Silver,Gold)")
    parser.add_argument("--var2-name", type=str, default=None, help="Varyasyon 2 adı (örn Length)")
    parser.add_argument("--var2-values", type=str, default=None, help="Varyasyon 2 değerleri, virgülle (örn 16cm,18cm)")
    parser.add_argument(
        "--write-variations-to-draft",
        action="store_true",
        help="--var* ile verilen varyasyonları draft JSON dosyasına yazar (draft-json ile kullan).",
    )
    parser.add_argument(
        "--price",
        type=str,
        default=None,
        help="Etsy fiyatı (örn 19.99). Verilmez scraper'dan tahmin veya 0.01",
    )
    parser.add_argument("--headful", action="store_true", help="Playwright pencereli mod (debug)")
    args = parser.parse_args()

    try:
        if args.draft_json is not None:
            draft = json.loads(args.draft_json.read_text(encoding="utf-8"))
            listing = None
        else:
            if not args.url:
                raise RuntimeError("Amazon URL gerekli: url argumenti veya --draft-json kullanın.")
            _validate_amazon_url(args.url)
            if args.no_playwright:
                html = fetch_html_simple(args.url)
                listing = parse_rendered_html(html, args.url)
            else:
                listing = scrape_with_playwright(args.url, headless=not args.headful)
    except Exception as e:
        print("Scrape hatası:", e, file=sys.stderr)
        return 1

    if args.draft_json is not None:
        # --draft-json ile geldiyse draft doğrudan kullanılır.
        pass
    else:
        draft = to_draft_dict(listing)
        _augment_images_with_color_variants(draft)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.draft_json is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = args.out_dir / f"draft_{listing.item_id}_{stamp}.json"
        out_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Taslak kaydedildi:", out_path.resolve())

    # Optionally inject variations into draft JSON (so user doesn't edit JSON manually)
    if args.draft_json is not None and args.write_variations_to_draft:
        variations = []
        if args.var1_name and args.var1_values:
            vals = [v.strip() for v in args.var1_values.split(",") if v.strip()]
            if vals:
                variations.append({"name": args.var1_name.strip(), "values": vals})
        if args.var2_name and args.var2_values:
            vals = [v.strip() for v in args.var2_values.split(",") if v.strip()]
            if vals:
                variations.append({"name": args.var2_name.strip(), "values": vals})
        if variations:
            draft["variations"] = variations
            args.draft_json.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Varyasyonlar draft JSON'a yazıldı:", args.draft_json.resolve())
        else:
            print("Varyasyon yazılmadı: --var1/--var2 parametreleri eksik.", file=sys.stderr)

    if args.etsy:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        price = args.price
        if not price:
            price_min = None
            if isinstance(draft.get("price_hint"), dict):
                price_min = draft["price_hint"].get("min")
            if price_min is not None:
                price = f"{float(price_min):.2f}"
            else:
                price = "0.01"
        desc = draft["description_text"] or ""
        if draft.get("variations"):
            desc += "\n\n--- Varyasyonlar (taslak) ---\n"
            desc += json.dumps(draft["variations"], ensure_ascii=False, indent=2)
        if draft.get("source", {}).get("url"):
            desc += f"\n\nKaynak: {draft['source']['url']}"
        if args.etsy_update_listing_id is not None:
            result = update_existing_listing(
                listing_id=args.etsy_update_listing_id,
                title=draft["title"],
                description=desc[:49990],
                price=price,
                quantity=1,
            )
            listing_id = args.etsy_update_listing_id
            print("Etsy listing güncellendi, listing_id:", listing_id)
        else:
            meta = draft.get("workspace_meta")
            if not isinstance(meta, dict):
                meta = {}
            who = str(meta.get("who_made") or "i_did").strip()
            what = str(meta.get("what_is_it") or "a_finished_product").strip()
            when = str(meta.get("when_made") or "made_to_order").strip()
            is_sup = what == "a_supply_or_tool"
            who, when, is_sup, mp_note = normalize_listing_who_when_supply(
                who_made=who, when_made=when, is_supply=is_sup
            )
            if mp_note:
                print(mp_note, file=sys.stderr)
            result = create_draft_listing(
                title=draft["title"],
                description=desc[:49990],
                price=price,
                quantity=1,
                who_made=who,
                when_made=when,
                is_supply=is_sup,
            )
            listing_id = result.get("listing_id")
            print("Etsy draft listing_id:", listing_id)

        if listing_id and draft.get("images"):
            _upload_images_best_effort(int(listing_id), list(draft.get("images") or []))

        if args.apply_variations and listing_id and draft.get("variations"):
            vars_ = draft["variations"]
            if not isinstance(vars_, list) or len(vars_) == 0:
                print("Varyasyon bulunamadı.", file=sys.stderr)
            else:
                # Etsy custom variation property ids
                prop_ids = [513, 514]
                vars_ = vars_[:2]
                property_ids = prop_ids[: len(vars_)]

                # Build cartesian product of values
                value_lists = []
                names = []
                for v in vars_:
                    if not isinstance(v, dict):
                        continue
                    names.append(str(v.get("name") or "Variation"))
                    vals = v.get("values") or []
                    vals = [str(x) for x in vals][:50]
                    value_lists.append(vals or ["Default"])

                combos = [[]]
                for vals in value_lists:
                    combos = [c + [val] for c in combos for val in vals]

                readiness_state_id = None
                try:
                    import os

                    raw_r = (os.environ.get("ETSY_READINESS_STATE_ID") or "").strip()
                    if raw_r:
                        readiness_state_id = int(raw_r)
                except (ValueError, TypeError):
                    readiness_state_id = None

                if readiness_state_id is None:
                    print(
                        "ETSY_READINESS_STATE_ID .env içinde yok; varyasyon envanter güncellemesi atlandı.",
                        file=sys.stderr,
                    )
                else:
                    products = []
                    for combo in combos:
                        prop_values = []
                        for idx, val in enumerate(combo):
                            prop_values.append(
                                {
                                    "property_id": property_ids[idx],
                                    "property_name": names[idx],
                                    "values": [val],
                                }
                            )
                        products.append(
                            {
                                "sku": "",
                                "property_values": prop_values,
                                "offerings": [
                                    {
                                        "price": float(price),
                                        "quantity": 1,
                                        "is_enabled": True,
                                        "readiness_state_id": readiness_state_id,
                                    }
                                ],
                            }
                        )

                    try:
                        update_listing_inventory(
                            listing_id=int(listing_id),
                            products=products,
                            property_ids=property_ids,
                        )
                        print(f"Varyasyonlar uygulandı. Ürün kombinasyonu: {len(products)}")
                    except Exception as ex:
                        print("Varyasyonlar uygulanamadı:", ex, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
