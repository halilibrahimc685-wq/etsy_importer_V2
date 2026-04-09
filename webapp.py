from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from etsy_client import (
    create_draft_listing,
    delete_listing,
    list_shop_shipping_profiles,
    normalize_listing_who_when_supply,
    update_existing_listing,
    update_listing_inventory,
    upload_listing_image_from_url,
)
from main import fetch_html_simple
from scraper import parse_rendered_html, scrape_with_playwright, to_draft_dict

load_dotenv()

app = FastAPI(title="Amazon -> Etsy Importer")
templates = Jinja2Templates(directory="templates")

# Etsy shipping listesi her tam sayfa render'da API'yi vurmaması için kısa önbellek.
_SHIP_PROF_CACHE_TTL_SEC = 120.0
_ship_prof_cache_mono: float = 0.0
_ship_prof_cache_value: Optional[tuple[list[dict[str, Any]], Optional[str]]] = None
APP_DRAFTS_FILE = Path("drafts") / "app_draft_listings.json"
PRESETS_FILE = Path("drafts") / "variation_presets.json"
CATEGORY_TAXONOMY_MAP = {
    "tshirts": 482,       # Clothing > Gender-Neutral Adult Clothing > Tops & Tees > T-shirts
    "sweatshirts": 2202,  # Clothing > Gender-Neutral Adult Clothing > Hoodies & Sweatshirts > Sweatshirts
}

DEFAULT_VARIATION_PRESETS: dict[str, Any] = {
    "shirt": {
        "label": "Shirt",
        "type1": {
            "name": "Size",
            "options": [
                "Unisex Adult T-Shirt - S",
                "Unisex Adult T-Shirt - M",
                "Unisex Adult T-Shirt - L",
                "Unisex Adult T-Shirt - XL",
                "Unisex Adult T-Shirt - 2XL",
                "Unisex Adult T-Shirt - 3XL",
                "Youth - S",
                "Youth - M",
                "Youth - L",
                "Youth - XL",
                "Toddler - 2T",
                "Toddler - 3T",
                "Toddler - 4T",
                "Toddler - 5T",
                "Baby Onesie 3 - 6 Mos.",
                "Baby Onesie 6 - 12 Mos.",
                "Baby Onesie 12 - 18 Mos.",
                "Baby Onesie 18 - 24 Mos.",
            ],
            "prices": [],
        },
        "type2": {
            "name": "Color",
            "options": [
                "Athletic Heather",
                "Black",
                "Dark Grey Heather",
                "Heather Columbia Blue",
                "Heather Maroon",
                "Heather Mauve",
                "Heather Navy",
                "Heather Peach",
                "Kelly Green",
                "Orange",
                "Pink",
                "Red",
                "Soft Cream",
                "White",
            ],
        },
    },
    "sweatshirt": {
        "label": "Sweatshirt & Hoodie",
        "type1": {
            "name": "Size",
            "options": [
                "Unisex Crewneck - S",
                "Unisex Crewneck - M",
                "Unisex Crewneck - L",
                "Unisex Crewneck - XL",
                "Unisex Crewneck - 2XL",
                "Unisex Crewneck - 3XL",
                "Unisex Hoodie - S",
                "Unisex Hoodie - M",
                "Unisex Hoodie - L",
                "Unisex Hoodie - XL",
                "Unisex Hoodie - 2XL",
                "Unisex Hoodie - 3XL",
                "Youth Crewneck - S",
                "Youth Crewneck - M",
                "Youth Crewneck - L",
                "Youth Crewneck - XL",
                "Youth Hoodie - S",
                "Youth Hoodie - M",
                "Youth Hoodie - L",
                "Youth Hoodie - XL",
            ],
            "prices": [],
        },
        "type2": {
            "name": "Color",
            "options": [
                "White",
                "Black",
                "Forest",
                "Irish Green",
                "Light Blue",
                "Light Pink",
                "Maroon",
                "Military Green",
                "Navy",
                "Orange",
                "Red",
                "Sand",
                "Sport Grey",
            ],
        },
    },
    "comfort_colors": {
        "label": "Comfort Colors",
        "type1": {
            "name": "Size",
            "options": [
                "CC Unisex T-shirt - S",
                "CC Unisex T-shirt - M",
                "CC Unisex T-shirt - L",
                "CC Unisex T-shirt - XL",
                "CC Unisex T-shirt - 2XL",
                "CC Unisex T-shirt - 3XL",
                "CC Unisex T-shirt - 4XL",
                "CC Unisex Long Sleeve Shirt - S",
                "CC Unisex Long Sleeve Shirt - M",
                "CC Unisex Long Sleeve Shirt - L",
                "CC Unisex Long Sleeve Shirt - XL",
                "CC Unisex Long Sleeve Shirt - 2XL",
                "CC Unisex Long Sleeve Shirt - 3XL",
                "CC Youth T-shirt - S",
                "CC Youth T-shirt - M",
                "CC Youth T-shirt - L",
                "CC Youth T-shirt - XL",
                "CC Unisex Sweatshirt - S",
                "CC Unisex Sweatshirt - M",
                "CC Unisex Sweatshirt - L",
                "CC Unisex Sweatshirt - XL",
                "CC Unisex Sweatshirt - 2XL",
                "CC Unisex Sweatshirt - 3XL",
            ],
            "prices": [],
        },
        "type2": {
            "name": "Color",
            "options": [
                "Light Green",
                "Moss",
                "Black",
                "Pepper",
                "Gray",
                "Blue Jean",
                "Chalky Mint",
                "Violet",
                "Orchid",
                "White",
                "Ivory",
                "Mustard",
                "Red",
                "Crimson",
                "Yam",
                "Berry",
                "Blossom",
            ],
        },
    },
}


def _validate_amazon_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise RuntimeError("URL http/https olmalı.")
    host = (p.netloc or "").lower()
    if "amazon." not in host:
        raise RuntimeError("Yalnızca Amazon ürün URL destekleniyor.")


def _apply_variations(listing_id: int, draft: dict[str, Any], price: str) -> Optional[str]:
    vars_ = draft.get("variations")
    if not vars_ or not isinstance(vars_, list):
        return None

    vars_ = vars_[:2]
    property_ids = [513, 514][: len(vars_)]

    value_lists: list[list[str]] = []
    names: list[str] = []
    for v in vars_:
        if not isinstance(v, dict):
            continue
        names.append(str(v.get("name") or "Variation"))
        vals = [str(x) for x in (v.get("values") or [])]
        value_lists.append(vals or ["Default"])

    combos: list[list[str]] = [[]]
    for vals in value_lists:
        combos = [c + [val] for c in combos for val in vals]

    readiness_state_id: Optional[int] = None
    try:
        raw_ready = (os.environ.get("ETSY_READINESS_STATE_ID") or "").strip()
        if raw_ready:
            readiness_state_id = int(raw_ready)
    except ValueError:
        readiness_state_id = None
    if readiness_state_id is None:
        raise RuntimeError(
            "Varyasyon (inventory) güncellemesi için her satış teklifinde readiness state gerekir. "
            ".env dosyasına ETSY_READINESS_STATE_ID ekleyin (draft listing oluştururken kullandığınız ID, "
            "Etsy Satıcı Paneli → Shop Manager’da readiness/shipping ayarlarınızdan doğrulanabilir)."
        )

    meta_v = draft.get("workspace_meta")
    preset_key = "custom"
    if isinstance(meta_v, dict):
        preset_key = str(meta_v.get("variation_preset") or "custom").strip()

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
        offering_price = _offering_price_for_combo(combo, names, preset_key, price)
        offering = {
            "price": offering_price,
            "quantity": 1,
            "is_enabled": True,
            "readiness_state_id": readiness_state_id,
        }
        products.append(
            {
                "sku": "",
                "property_values": prop_values,
                "offerings": [dict(offering)],
            }
        )

    update_listing_inventory(
        listing_id=int(listing_id),
        products=products,
        property_ids=property_ids,
    )
    return f"Varyasyonlar uygulandı ({len(products)} kombinasyon)."


def _upload_images_best_effort(listing_id: int, images: list[Any]) -> str:
    uploaded = 0
    failed = 0
    seen: set[str] = set()
    rank = 1
    for image in images:
        if not isinstance(image, str) or not image.strip():
            continue
        url = image.strip()
        if url in seen:
            continue
        seen.add(url)
        try:
            upload_listing_image_from_url(listing_id, url, rank=rank, overwrite=True)
            uploaded += 1
            rank += 1
        except Exception as ex:
            failed += 1
    if uploaded == 0 and failed > 0:
        return "Görsel yüklenemedi."
    return f"{uploaded} görsel yüklendi."


def _etsy_shop_display_name() -> str:
    return (os.environ.get("ETSY_SHOP_NAME") or "Your shop").strip()


def _load_app_draft_listings(limit: int = 50) -> list[dict[str, Any]]:
    try:
        if not APP_DRAFTS_FILE.exists():
            return []
        data = json.loads(APP_DRAFTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        rows = [x for x in data if isinstance(x, dict)]
        for row in rows:
            lid = row.get("listing_id")
            if row.get("etsy_listing_id") is None and lid is not None:
                row["etsy_listing_id"] = lid
            if not row.get("amazon_url") and row.get("source_url"):
                row["amazon_url"] = row.get("source_url")
            if row.get("sku") is None:
                row["sku"] = ""
            if row.get("variation_preset") is None:
                row["variation_preset"] = "custom"
            if row.get("section") is None:
                row["section"] = ""
        rows.sort(key=lambda x: str(x.get("saved_at") or ""), reverse=True)
        return rows[:limit]
    except Exception:
        return []


def _save_app_draft_listing(*, listing_id: int, draft: dict[str, Any], price: str, mode: str) -> None:
    APP_DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = _load_app_draft_listings(limit=500)
    src = draft.get("source") if isinstance(draft.get("source"), dict) else {}
    meta = draft.get("workspace_meta") if isinstance(draft.get("workspace_meta"), dict) else {}
    row = {
        "listing_id": int(listing_id),
        "etsy_listing_id": int(listing_id),
        "title": str(draft.get("title") or "")[:140],
        "price": str(price or ""),
        "sku": str(meta.get("sku") or ""),
        "variation_preset": str(meta.get("variation_preset") or "custom"),
        "section": str(meta.get("section") or ""),
        "image": (draft.get("images") or [None])[0] if isinstance(draft.get("images"), list) else None,
        "source_url": str(src.get("url") or ""),
        "amazon_url": str(src.get("url") or ""),
        "source_item_id": str(src.get("item_id") or ""),
        "etsy_url": f"https://www.etsy.com/your/shops/me/listing-editor/{int(listing_id)}",
        "mode": mode,  # create|update
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "draft_json": draft if isinstance(draft, dict) else {},
    }
    # aynı listing tekrar kaydedilirse en üste al ve güncelle
    target = int(listing_id)
    filtered: list[dict[str, Any]] = []
    for r in rows:
        try:
            rid1 = int(r.get("listing_id") or -1)
        except Exception:
            rid1 = -1
        try:
            rid2 = int(r.get("etsy_listing_id") or -1)
        except Exception:
            rid2 = -1
        if rid1 == target or rid2 == target:
            continue
        filtered.append(r)
    rows = filtered
    rows.insert(0, row)
    APP_DRAFTS_FILE.write_text(json.dumps(rows[:500], ensure_ascii=False, indent=2), encoding="utf-8")


def _delete_app_draft_listing(listing_id: int) -> bool:
    rows = _load_app_draft_listings(limit=500)
    target = int(listing_id)
    kept: list[dict[str, Any]] = []
    removed = False
    for r in rows:
        try:
            rid1 = int(r.get("listing_id") or -1)
        except Exception:
            rid1 = -1
        try:
            rid2 = int(r.get("etsy_listing_id") or -1)
        except Exception:
            rid2 = -1
        if rid1 == target or rid2 == target:
            removed = True
            continue
        kept.append(r)
    if removed:
        APP_DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_DRAFTS_FILE.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


def _load_variation_presets() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        if PRESETS_FILE.exists():
            raw = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    except Exception:
        data = {}

    merged = json.loads(json.dumps(DEFAULT_VARIATION_PRESETS, ensure_ascii=False))
    for key, val in data.items():
        if key not in merged or not isinstance(val, dict):
            continue
        for part in ("type1", "type2"):
            if isinstance(val.get(part), dict):
                if isinstance(val[part].get("name"), str):
                    merged[key][part]["name"] = val[part]["name"].strip() or merged[key][part]["name"]
                if isinstance(val[part].get("options"), list):
                    opts = [str(x).strip() for x in val[part]["options"] if str(x).strip()]
                    if opts:
                        merged[key][part]["options"] = opts
                if isinstance(val[part].get("prices"), list):
                    merged[key][part]["prices"] = [
                        str(x).strip() for x in val[part]["prices"]
                    ]
    for block in merged.values():
        if not isinstance(block, dict):
            continue
        for part in ("type1", "type2"):
            p = block.get(part)
            if isinstance(p, dict) and "prices" not in p:
                p["prices"] = []
    return merged


def _preset_type1_price_by_option(preset_key: str, base_price: str) -> dict[str, float]:
    """Birincil seçenek (genelde beden) metni -> Etsy offering fiyatı."""
    base_s = (base_price or "19.99").strip() or "19.99"
    try:
        base_f = float(base_s)
    except ValueError:
        base_f = 19.99
    raw = _load_variation_presets().get(preset_key)
    if not isinstance(raw, dict):
        return {}
    t1 = raw.get("type1")
    if not isinstance(t1, dict):
        return {}
    opts = t1.get("options")
    if not isinstance(opts, list):
        return {}
    plist = t1.get("prices")
    price_lines: list[str] = (
        [str(x).strip() for x in plist] if isinstance(plist, list) else []
    )
    out: dict[str, float] = {}
    for i, opt in enumerate(opts):
        o = str(opt).strip()
        if not o:
            continue
        cell = price_lines[i] if i < len(price_lines) else ""
        use_s = cell if cell else base_s
        try:
            out[o] = float(use_s)
        except ValueError:
            out[o] = base_f
    return out


def _offering_price_for_combo(
    combo: list[str],
    dim_names: list[str],
    preset_key: str,
    base_price: str,
) -> float:
    base_s = (base_price or "19.99").strip() or "19.99"
    try:
        base_f = float(base_s)
    except ValueError:
        base_f = 19.99
    if preset_key not in ("shirt", "sweatshirt", "comfort_colors"):
        return base_f
    presets_block = _load_variation_presets().get(preset_key)
    if not isinstance(presets_block, dict):
        return base_f
    t1 = presets_block.get("type1")
    if not isinstance(t1, dict):
        return base_f
    t1_name = str(t1.get("name") or "Size").strip().lower()
    price_by_opt = _preset_type1_price_by_option(preset_key, base_price)
    if not price_by_opt:
        return base_f
    idx = -1
    for i, n in enumerate(dim_names):
        if str(n).strip().lower() == t1_name:
            idx = i
            break
    if idx < 0:
        idx = 0
    if idx >= len(combo):
        return base_f
    val = combo[idx]
    return price_by_opt.get(val.strip(), base_f)


def _opts_from_multivalue(form: dict[str, Any], key: str) -> list[str]:
    """Formdan aynı isimli alanların listesi; boş ve yinelenenleri atlar (sıra korunur)."""
    raw = form.get(key) or []
    if not isinstance(raw, list):
        raw = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        v = str(x).strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _type1_opts_prices_from_form(
    form: dict[str, Any], opt_key: str, price_key: str
) -> tuple[list[str], list[str]]:
    olist = form.get(opt_key) or []
    plist = form.get(price_key) or []
    if not isinstance(olist, list):
        olist = [str(olist).strip()] if str(olist).strip() else []
    else:
        olist = [str(x).strip() for x in olist]
    if not isinstance(plist, list):
        plist = [str(plist).strip()] if str(plist).strip() else []
    else:
        plist = [str(x).strip() for x in plist]
    opts_out: list[str] = []
    prices_out: list[str] = []
    n = max(len(olist), len(plist))
    for i in range(n):
        o = olist[i] if i < len(olist) else ""
        p = plist[i] if i < len(plist) else ""
        if not o:
            continue
        opts_out.append(o)
        prices_out.append(p)
    return opts_out, prices_out


def _save_variation_presets_from_form(form: dict[str, Any]) -> None:
    presets = _load_variation_presets()

    presets["shirt"]["type1"]["name"] = str(form.get("shirt_type1_name") or "Size").strip() or "Size"
    presets["shirt"]["type2"]["name"] = str(form.get("shirt_type2_name") or "Color").strip() or "Color"
    so, sp = _type1_opts_prices_from_form(form, "shirt_type1_opts", "shirt_type1_opt_prices")
    if so:
        presets["shirt"]["type1"]["options"] = so
        while len(sp) < len(so):
            sp.append("")
        presets["shirt"]["type1"]["prices"] = sp[: len(so)]
    opts2 = _opts_from_multivalue(form, "shirt_type2_opts")
    if opts2:
        presets["shirt"]["type2"]["options"] = opts2

    presets["sweatshirt"]["type1"]["name"] = (
        str(form.get("sweat_type1_name") or "Size").strip() or "Size"
    )
    presets["sweatshirt"]["type2"]["name"] = (
        str(form.get("sweat_type2_name") or "Color").strip() or "Color"
    )
    wo, wp = _type1_opts_prices_from_form(form, "sweat_type1_opts", "sweat_type1_opt_prices")
    if wo:
        presets["sweatshirt"]["type1"]["options"] = wo
        while len(wp) < len(wo):
            wp.append("")
        presets["sweatshirt"]["type1"]["prices"] = wp[: len(wo)]
    w2 = _opts_from_multivalue(form, "sweat_type2_opts")
    if w2:
        presets["sweatshirt"]["type2"]["options"] = w2

    presets["comfort_colors"]["type1"]["name"] = (
        str(form.get("cc_type1_name") or "Size").strip() or "Size"
    )
    presets["comfort_colors"]["type2"]["name"] = (
        str(form.get("cc_type2_name") or "Color").strip() or "Color"
    )
    co, cp = _type1_opts_prices_from_form(form, "cc_type1_opts", "cc_type1_opt_prices")
    if co:
        presets["comfort_colors"]["type1"]["options"] = co
        while len(cp) < len(co):
            cp.append("")
        presets["comfort_colors"]["type1"]["prices"] = cp[: len(co)]
    c2 = _opts_from_multivalue(form, "cc_type2_opts")
    if c2:
        presets["comfort_colors"]["type2"]["options"] = c2

    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")


def _draft_listing_create_args(draft: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """create_draft_listing için who_made / when_made / is_supply ve opsiyonel uyarı."""
    meta = draft.get("workspace_meta")
    if not isinstance(meta, dict):
        meta = {}
    who = str(meta.get("who_made") or "i_did").strip()
    what = str(meta.get("what_is_it") or "a_finished_product").strip()
    when = str(meta.get("when_made") or "made_to_order").strip()
    is_supply = what == "a_supply_or_tool"
    who, when, is_supply, note = normalize_listing_who_when_supply(
        who_made=who, when_made=when, is_supply=is_supply
    )
    cat_key = str(meta.get("category_taxonomy") or "tshirts").strip().lower()
    taxonomy_id = CATEGORY_TAXONOMY_MAP.get(cat_key, CATEGORY_TAXONOMY_MAP["tshirts"])
    opts: dict[str, Any] = {
        "who_made": who,
        "when_made": when,
        "is_supply": is_supply,
        "taxonomy_id": taxonomy_id,
    }
    ship_s = str(meta.get("shipping_profile_id") or "").strip()
    if ship_s.isdigit():
        opts["shipping_profile_id"] = int(ship_s)
    return opts, note


def _normalize_shipping_profiles_for_ui(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in raw:
        pid = p.get("shipping_profile_id")
        if pid is None:
            pid = p.get("profile_id")
        if pid is None:
            continue
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            continue
        if p.get("is_deleted") is True:
            continue
        title = str(p.get("title") or p.get("name") or f"Profile {pid_i}")
        rows.append({"id": pid_i, "title": title})
    rows.sort(key=lambda x: (x["title"].lower(), x["id"]))
    return rows


def _etsy_shipping_profiles_display() -> tuple[list[dict[str, Any]], Optional[str]]:
    global _ship_prof_cache_mono, _ship_prof_cache_value
    now = time.monotonic()
    if (
        _ship_prof_cache_value is not None
        and now - _ship_prof_cache_mono < _SHIP_PROF_CACHE_TTL_SEC
    ):
        return _ship_prof_cache_value
    try:
        raw = list_shop_shipping_profiles()
        out: tuple[list[dict[str, Any]], Optional[str]] = (
            _normalize_shipping_profiles_for_ui(raw),
            None,
        )
    except Exception as exc:
        out = ([], str(exc))
    _ship_prof_cache_value = out
    _ship_prof_cache_mono = now
    return out


def _workspace_ui(draft: Optional[dict[str, Any]]) -> dict[str, Any]:
    defaults_meta = {
        "who_made": "i_did",
        "what_is_it": "a_finished_product",
        "when_made": "made_to_order",
        "renewal": "manual",
        "listing_type": "physical",
        "personalization": "off",
        "section": "",
        "sku": "",
        "quantity": "1",
        "variation_preset": "custom",
        "category_taxonomy": "tshirts",
        "shipping_profile_id": "",
    }
    if not draft:
        return {
            "has_draft": False,
            "title": "",
            "description": "",
            "images": [],
            "tags_str": "",
            "source_url": "",
            "item_id": "",
            "price_display": "",
            "variations_preview": "",
            "variations": [],
            **defaults_meta,
        }
    tags = draft.get("tags")
    if not isinstance(tags, list):
        tags = []
    tags_str = ", ".join(str(t).strip() for t in tags if str(t).strip())
    ph = draft.get("price_hint") or {}
    pd = ""
    if ph.get("display"):
        pd = str(ph["display"])
    elif ph.get("min") is not None:
        pd = str(ph["min"])
    vars_ = draft.get("variations")
    var_preview = ""
    variations_editable: list[dict[str, Any]] = []
    if isinstance(vars_, list):
        for item in vars_:
            if not isinstance(item, dict):
                continue
            vname = str(item.get("name") or "").strip()
            raw_vals = item.get("values")
            if not isinstance(raw_vals, list):
                raw_vals = []
            vvals = [str(x).strip() for x in raw_vals if str(x).strip()]
            variations_editable.append({"name": vname, "values": vvals})
        if vars_:
            try:
                var_preview = json.dumps(vars_, ensure_ascii=False, indent=2)
            except Exception:
                var_preview = str(vars_)[:2000]
    src = draft.get("source") or {}
    meta_in = draft.get("workspace_meta")
    meta: dict[str, Any] = dict(defaults_meta)
    if isinstance(meta_in, dict):
        meta.update({k: v for k, v in meta_in.items() if k in defaults_meta})
    return {
        "has_draft": bool(draft.get("title") or draft.get("images")),
        "title": draft.get("title") or "",
        "description": draft.get("description_text") or "",
        "images": draft.get("images") if isinstance(draft.get("images"), list) else [],
        "tags_str": tags_str,
        "source_url": str(src.get("url") or ""),
        "item_id": str(src.get("item_id") or ""),
        "price_display": pd,
        "variations_preview": var_preview[:4000],
        "variations": variations_editable,
        "who_made": str(meta.get("who_made") or defaults_meta["who_made"]),
        "what_is_it": str(meta.get("what_is_it") or defaults_meta["what_is_it"]),
        "when_made": str(meta.get("when_made") or defaults_meta["when_made"]),
        "renewal": str(meta.get("renewal") or defaults_meta["renewal"]),
        "listing_type": str(meta.get("listing_type") or defaults_meta["listing_type"]),
        "personalization": str(meta.get("personalization") or defaults_meta["personalization"]),
        "section": str(meta.get("section") or ""),
        "sku": str(meta.get("sku") or ""),
        "quantity": str(meta.get("quantity") or "1"),
        "variation_preset": str(meta.get("variation_preset") or "custom"),
        "category_taxonomy": str(meta.get("category_taxonomy") or "tshirts"),
        "shipping_profile_id": str(meta.get("shipping_profile_id") or "").strip(),
    }


def _build_draft(url: str, no_playwright: bool) -> dict[str, Any]:
    _validate_amazon_url(url)
    if no_playwright:
        html = fetch_html_simple(url)
        listing = parse_rendered_html(html, url)
    else:
        listing = scrape_with_playwright(url, headless=True)
    return to_draft_dict(listing)


def _render_index(
    request: Request,
    *,
    error: Optional[str] = None,
    status: Optional[str] = None,
    workspace_url: str = "",
    workspace_draft: Optional[dict[str, Any]] = None,
    workspace_draft_json: str = "",
    workspace_price: str = "19.99",
    workspace_listing_id: str = "",
    etsy_shop_name: str = "",
    active_tab: str = "workspace",
) -> HTMLResponse:
    if not etsy_shop_name:
        etsy_shop_name = _etsy_shop_display_name()
    if workspace_draft is not None:
        workspace_draft_json = json.dumps(workspace_draft, ensure_ascii=False, indent=2)
    elif not workspace_draft_json.strip():
        workspace_draft_json = "{}"
    ws = _workspace_ui(workspace_draft)
    variation_presets = _load_variation_presets()
    variation_presets_json = json.dumps(variation_presets, ensure_ascii=False)
    etsy_shipping_profiles, etsy_shipping_profiles_error = _etsy_shipping_profiles_display()
    ws_ship_sel = str(ws.get("shipping_profile_id") or "").strip()
    if ws_ship_sel.isdigit():
        sid_i = int(ws_ship_sel)
        if not any(p["id"] == sid_i for p in etsy_shipping_profiles):
            etsy_shipping_profiles = list(etsy_shipping_profiles) + [
                {"id": sid_i, "title": f"Taslak / .env ({sid_i})"}
            ]
            etsy_shipping_profiles.sort(key=lambda x: (str(x["title"]).lower(), x["id"]))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "error": error,
            "status": status,
            "workspace_url": workspace_url,
            "workspace_draft": workspace_draft,
            "workspace_draft_json": workspace_draft_json,
            "workspace_price": workspace_price,
            "workspace_listing_id": workspace_listing_id,
            "etsy_shop_name": etsy_shop_name,
            "ws": ws,
            "app_draft_listings": _load_app_draft_listings(),
            "variation_presets": variation_presets,
            "variation_presets_json": variation_presets_json,
            "etsy_shipping_profiles": etsy_shipping_profiles,
            "etsy_shipping_profiles_error": etsy_shipping_profiles_error,
            "active_tab": active_tab,
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _render_index(request)


@app.get("/presets", response_class=HTMLResponse)
def presets(request: Request) -> HTMLResponse:
    return _render_index(request, active_tab="presets")


@app.get("/drafts", response_class=HTMLResponse)
def drafts(request: Request) -> HTMLResponse:
    return _render_index(request, active_tab="drafts")


@app.post("/drafts/open", response_class=HTMLResponse)
def drafts_open(request: Request, listing_id: int = Form(...)) -> HTMLResponse:
    rows = _load_app_draft_listings(limit=500)
    selected = None
    for row in rows:
        if int(row.get("listing_id") or -1) == int(listing_id):
            selected = row
            break
    if not selected:
        return _render_index(request, active_tab="drafts", error=f"Draft bulunamadı: {listing_id}")

    draft_obj = selected.get("draft_json")
    if not isinstance(draft_obj, dict):
        draft_obj = {
            "title": str(selected.get("title") or ""),
            "images": [selected.get("image")] if selected.get("image") else [],
            "source": {
                "url": str(selected.get("source_url") or ""),
                "item_id": str(selected.get("source_item_id") or ""),
            },
            "tags": [],
            "variations": [],
            "workspace_meta": {
                "who_made": "i_did",
                "what_is_it": "a_finished_product",
                "when_made": "made_to_order",
                "renewal": "manual",
                "listing_type": "physical",
                "personalization": "off",
                "section": "",
                "sku": "",
                "quantity": "1",
                "variation_preset": "custom",
                "category_taxonomy": "tshirts",
                "shipping_profile_id": "",
            },
        }

    return _render_index(
        request,
        active_tab="workspace",
        status=f"Draft yüklendi: {listing_id}. Düzenleyip update edebilirsin.",
        workspace_draft=draft_obj,
        workspace_price=str(selected.get("price") or "19.99"),
        workspace_listing_id=str(listing_id),
    )


@app.post("/drafts/delete", response_class=HTMLResponse)
def drafts_delete(request: Request, listing_id: int = Form(...)) -> HTMLResponse:
    try:
        delete_listing(int(listing_id))
    except Exception as exc:
        return _render_index(
            request,
            active_tab="drafts",
            error=f"Etsy listing silinemedi ({listing_id}): {exc}",
        )
    removed = _delete_app_draft_listing(listing_id)
    if removed:
        return _render_index(
            request,
            active_tab="drafts",
            status=f"Draft Etsy ve uygulamadan silindi: {listing_id}",
        )
    return _render_index(
        request,
        active_tab="drafts",
        status=f"Etsy listing silindi, yerel kayıt zaten yoktu: {listing_id}",
    )


@app.post("/presets/save", response_class=HTMLResponse)
async def presets_save(request: Request) -> HTMLResponse:
    form_in = await request.form()

    def _gl(name: str) -> list[str]:
        return [str(v).strip() for v in form_in.getlist(name)]

    payload: dict[str, Any] = {
        "shirt_type1_name": str(form_in.get("shirt_type1_name") or "Size"),
        "shirt_type2_name": str(form_in.get("shirt_type2_name") or "Color"),
        "shirt_type2_opts": _gl("shirt_type2_opts"),
        "shirt_type1_opts": _gl("shirt_type1_opts"),
        "shirt_type1_opt_prices": _gl("shirt_type1_opt_prices"),
        "sweat_type1_name": str(form_in.get("sweat_type1_name") or "Size"),
        "sweat_type2_name": str(form_in.get("sweat_type2_name") or "Color"),
        "sweat_type2_opts": _gl("sweat_type2_opts"),
        "sweat_type1_opts": _gl("sweat_type1_opts"),
        "sweat_type1_opt_prices": _gl("sweat_type1_opt_prices"),
        "cc_type1_name": str(form_in.get("cc_type1_name") or "Size"),
        "cc_type2_name": str(form_in.get("cc_type2_name") or "Color"),
        "cc_type2_opts": _gl("cc_type2_opts"),
        "cc_type1_opts": _gl("cc_type1_opts"),
        "cc_type1_opt_prices": _gl("cc_type1_opt_prices"),
    }
    try:
        _save_variation_presets_from_form(payload)
        return _render_index(request, active_tab="presets", status="Presetler kaydedildi.")
    except Exception as exc:
        return _render_index(request, active_tab="presets", error=str(exc))


@app.post("/workspace/scrape", response_class=HTMLResponse)
def workspace_scrape(
    request: Request,
    url: str = Form(...),
    no_playwright: bool = Form(False),
) -> HTMLResponse:
    try:
        draft = _build_draft(url, no_playwright)
        if isinstance(draft, dict):
            try:
                from main import _augment_images_with_color_variants

                _augment_images_with_color_variants(draft)
            except Exception:
                pass
        if isinstance(draft, dict) and "tags" not in draft:
            draft["tags"] = []
        if isinstance(draft, dict) and "workspace_meta" not in draft:
            draft["workspace_meta"] = {
                "who_made": "i_did",
                "what_is_it": "a_finished_product",
                "when_made": "made_to_order",
                "renewal": "manual",
                "listing_type": "physical",
                "personalization": "off",
                "section": "",
                "sku": "",
                "quantity": "1",
                "variation_preset": "custom",
                "category_taxonomy": "tshirts",
                "shipping_profile_id": "",
            }
        return _render_index(
            request,
            status="Taslak hazır — Etsy listing formuna benzer alanları düzenleyip yayınlayabilirsin.",
            workspace_url=url,
            workspace_draft=draft,
        )
    except Exception as exc:
        return _render_index(
            request,
            error=str(exc),
            workspace_url=url,
        )


@app.post("/workspace/publish", response_class=HTMLResponse)
def workspace_publish(
    request: Request,
    draft_json: str = Form(...),
    price: str = Form("19.99"),
    etsy_update_listing_id: str = Form(""),
) -> HTMLResponse:
    try:
        draft = json.loads(draft_json)
        desc = str(draft.get("description_text") or "")
        if draft.get("source", {}).get("url"):
            desc += f"\n\nKaynak: {draft['source']['url']}"

        listing_id: int
        if etsy_update_listing_id.strip():
            listing_id = int(etsy_update_listing_id.strip())
            update_existing_listing(
                listing_id=listing_id,
                title=str(draft.get("title") or "")[:140],
                description=desc[:49990],
                price=price,
                quantity=1,
            )
            status = f"Etsy listing güncellendi: {listing_id}"
            _save_app_draft_listing(listing_id=listing_id, draft=draft, price=price, mode="update")
        else:
            wm_pub = draft.get("workspace_meta")
            if not isinstance(wm_pub, dict):
                wm_pub = {}
            ship_pub = str(wm_pub.get("shipping_profile_id") or "").strip()
            if not ship_pub.isdigit():
                raise RuntimeError(
                    "Yeni Etsy taslağı için Shipping & processing altında bir shipping profile seçin."
                )
            listing_opts, mp_note = _draft_listing_create_args(draft)
            result = create_draft_listing(
                title=str(draft.get("title") or "")[:140],
                description=desc[:49990],
                price=price,
                quantity=1,
                **listing_opts,
            )
            listing_id = int(result.get("listing_id"))
            status = f"Etsy draft oluşturuldu: {listing_id}"
            if mp_note:
                status += " | " + mp_note
            _save_app_draft_listing(listing_id=listing_id, draft=draft, price=price, mode="create")

        raw_imgs = draft.get("images") or []
        images = [x for x in raw_imgs if isinstance(x, str) and x.strip()][:20]
        if images:
            status += " | " + _upload_images_best_effort(listing_id, images)

        variation_status = _apply_variations(listing_id, draft, price)
        if variation_status:
            status += f" | {variation_status}"

        return _render_index(
            request,
            status=status,
            workspace_draft=draft if isinstance(draft, dict) else None,
            workspace_price=price,
            workspace_listing_id=etsy_update_listing_id,
        )
    except Exception as exc:
        try:
            draft_err = json.loads(draft_json)
        except Exception:
            draft_err = None
        return _render_index(
            request,
            error=str(exc),
            workspace_draft=draft_err if isinstance(draft_err, dict) else None,
            workspace_draft_json=draft_json,
            workspace_price=price,
            workspace_listing_id=etsy_update_listing_id,
        )
