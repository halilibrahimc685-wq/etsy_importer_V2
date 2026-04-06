from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from etsy_client import (
    create_draft_listing,
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

    offering = {
        "price": float(price),
        "quantity": 1,
        "is_enabled": True,
        "readiness_state_id": readiness_state_id,
    }
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
    return (
        {"who_made": who, "when_made": when, "is_supply": is_supply},
        note,
    )


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
) -> HTMLResponse:
    if not etsy_shop_name:
        etsy_shop_name = _etsy_shop_display_name()
    if workspace_draft is not None:
        workspace_draft_json = json.dumps(workspace_draft, ensure_ascii=False, indent=2)
    elif not workspace_draft_json.strip():
        workspace_draft_json = "{}"
    ws = _workspace_ui(workspace_draft)
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
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _render_index(request)


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
    apply_variations: bool = Form(False),
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
        else:
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

        raw_imgs = draft.get("images") or []
        images = [x for x in raw_imgs if isinstance(x, str) and x.strip()][:20]
        if images:
            status += " | " + _upload_images_best_effort(listing_id, images)

        if apply_variations:
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
