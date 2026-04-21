from __future__ import annotations

import copy
import io
import json
import logging
import os
import re
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, unquote, urlparse

import httpx
import boto3
from PIL import Image
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from etsy_client import (
    create_draft_listing,
    normalize_listing_who_when_supply,
    update_existing_listing,
    upload_listing_image_from_url,
)
from main import fetch_html_simple
from amazon_scraper import canonical_amazon_dp_url, is_amazon_cdn_product_image_url
from mockup_engine import (
    MockupProcessingConfig,
    SUPPORTED_EXTENSIONS,
    auto_fit_width_percent,
    calculate_luminance,
    collect_mockup_images,
    compose_mockup,
    pick_design_for_mockup,
    process_all,
    resolve_placement,
)
from scraper import parse_rendered_html, scrape_with_playwright, to_draft_dict

load_dotenv()

# Vercel serverless: proje dizini salt okunur; state ve üretilen mockuplar /tmp altında tutulur.
def _drafts_base_dir() -> Path:
    if (os.environ.get("VERCEL") or "").strip():
        return Path("/tmp/drafts")
    return Path("drafts")


_DRAFTS_BASE = _drafts_base_dir()

app = FastAPI(title="Amazon -> Etsy Importer")
_templates_dir = (Path(__file__).resolve().parent / "templates").resolve()
templates = Jinja2Templates(directory=str(_templates_dir))


def amazon_image_display_url(raw: Optional[str]) -> str:
    """Tarayıcı önizlemesi: Amazon CDN bazen doğrudan <img> ile engellenir; kendi sunucumuzdan servis."""
    try:
        u = (raw if isinstance(raw, str) else str(raw or "")).strip()
        if not u or not is_amazon_cdn_product_image_url(u):
            return u
        return "/media/amazon-image?u=" + quote(u, safe="")
    except Exception:
        return (raw or "").strip() if isinstance(raw, str) else ""


def _jinja_filter_amazon_display(value: Any) -> str:
    return amazon_image_display_url(None if value is None else str(value))


templates.env.filters["amazon_display"] = _jinja_filter_amazon_display


@app.middleware("http")
async def _log_unhandled_exceptions(
    request: Request, call_next: Callable[[Request], Awaitable[Any]]
) -> Any:
    try:
        return await call_next(request)
    except Exception:
        logging.getLogger("uvicorn.error").exception(
            "İstek: %s %s", request.method, request.url.path
        )
        raise


def _workspace_draft_stripped_for_storage(draft: Any) -> Any:
    """workspace_states.json — debug çok büyük / bazen JSON dışı tipler; kayıtta atılır."""
    if not isinstance(draft, dict):
        return draft
    out = copy.deepcopy(draft)
    out.pop("debug", None)
    return out

APP_DRAFTS_FILE = _DRAFTS_BASE / "app_draft_listings.json"
WORKSPACE_STATES_FILE = _DRAFTS_BASE / "workspace_states.json"
WORKSPACE_MOCKUPS_ROOT = _DRAFTS_BASE / "mockups_generated" / "_workspace"
WORKSPACE_DESIGNS_ROOT = _DRAFTS_BASE / "_workspace_designs"
_ETSY_TAG_MAX_LEN = 20
_ETSY_TAG_MAX_COUNT = 13


def _r2_enabled() -> bool:
    return all(
        (os.environ.get(k) or "").strip()
        for k in ("S3_BUCKET", "S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
    )


def _r2_prefix() -> str:
    raw = str(os.environ.get("S3_PREFIX") or "").strip().strip("/")
    return (raw + "/") if raw else ""


def _r2_key_for_rel(rel: str) -> str:
    rel_norm = str(rel or "").strip().replace("\\", "/").lstrip("/")
    return f"{_r2_prefix()}{rel_norm}"


def _r2_rel_from_key(key: str) -> str:
    prefix = _r2_prefix()
    if prefix and key.startswith(prefix):
        return key[len(prefix) :].lstrip("/")
    return key.lstrip("/")


def _r2_client():
    endpoint = (os.environ.get("S3_ENDPOINT") or "").strip()
    region = (os.environ.get("S3_REGION") or "auto").strip() or "auto"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=(os.environ.get("S3_ACCESS_KEY_ID") or "").strip(),
        aws_secret_access_key=(os.environ.get("S3_SECRET_ACCESS_KEY") or "").strip(),
    )


def _r2_list_keys() -> list[str]:
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return []
    client = _r2_client()
    prefix = _r2_prefix()
    keys: list[str] = []
    token: Optional[str] = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in (resp.get("Contents") or []):
            k = str(obj.get("Key") or "")
            if not k:
                continue
            rel = _r2_rel_from_key(k)
            if not rel or rel.endswith("/"):
                continue
            ext = Path(rel).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            keys.append(rel)
        if not resp.get("IsTruncated"):
            break
        token = str(resp.get("NextContinuationToken") or "")
        if not token:
            break
    return sorted(keys, key=lambda s: s.lower())


def _mockups_root() -> Path:
    raw = (os.environ.get("MOCKUPS_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path(__file__).resolve().parent / "Mockups").resolve()


def _mockup_rel_url(rel: str) -> str:
    """URL path: /media/mockups/... (segment başına quote)."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    return "/media/mockups/" + "/".join(quote(p, safe="") for p in parts)


def _safe_mockup_file_path(rel: str) -> Optional[Path]:
    root = _mockups_root()
    if not root.is_dir():
        return None
    rel_norm = (rel or "").strip().replace("\\", "/")
    if not rel_norm or ".." in rel_norm.split("/"):
        return None
    candidate = (root / rel_norm).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    return candidate


def _list_mockup_catalog() -> list[dict[str, Any]]:
    if _r2_enabled():
        try:
            keys = _r2_list_keys()
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "R2 mockup katalogu listelenemedi (S3_* ortam degiskenlerini kontrol edin)"
            )
            return []
        groups: dict[str, list[str]] = {}
        for rel in keys:
            parts = [p for p in rel.split("/") if p]
            if not parts:
                continue
            if len(parts) == 1:
                title = "Files"
                filename = parts[0]
            else:
                title = parts[0]
                filename = parts[-1]
            groups.setdefault(title, []).append(rel)
        categories: list[dict[str, Any]] = []
        for title in sorted(groups.keys(), key=lambda s: s.lower()):
            rels_sorted = sorted(groups[title], key=lambda s: s.lower())
            images = [
                {"rel": rel, "filename": Path(rel).name, "url": _mockup_rel_url(rel)}
                for rel in rels_sorted
            ]
            if not images:
                continue
            categories.append(
                {
                    "id": title,
                    "title": title,
                    "count": len(images),
                    "cover_rel": images[0]["rel"],
                    "images": images,
                }
            )
        return categories
    root = _mockups_root()
    if not root.is_dir():
        return []
    categories: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            imgs = collect_mockup_images(child)
            if not imgs:
                continue
            imgs_sorted = sorted(imgs, key=lambda p: str(p).lower())
            rels: list[dict[str, str]] = []
            for img in imgs_sorted:
                try:
                    rel = img.relative_to(root).as_posix()
                except ValueError:
                    continue
                rels.append(
                    {
                        "rel": rel,
                        "filename": img.name,
                        "url": _mockup_rel_url(rel),
                    }
                )
            if rels:
                categories.append(
                    {
                        "id": child.name,
                        "title": child.name,
                        "count": len(rels),
                        "cover_rel": rels[0]["rel"],
                        "images": rels,
                    }
                )
        elif child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
            rel = child.name
            url = _mockup_rel_url(rel)
            categories.append(
                {
                    "id": f"file:{child.name}",
                    "title": child.stem,
                    "count": 1,
                    "cover_rel": rel,
                    "images": [{"rel": rel, "filename": child.name, "url": url}],
                }
            )
    return categories


def _on_vercel() -> bool:
    return bool((os.environ.get("VERCEL") or "").strip())


def _mockup_library_empty_note(categories: list[dict[str, Any]]) -> Optional[str]:
    """Katalog boşken kullanıcıya nedenini anlat (özellikle Vercel + .vercelignore)."""
    if categories:
        return None
    if _on_vercel() and not _r2_enabled():
        return (
            "Vercel: büyük mockup görselleri bu deploy paketine alınmaz "
            "(.vercelignore — sadece Mockups/placement.json gelir). Bu yüzden klasör “bulunur” "
            "görünse de şablon PNG’leri yoktur. Kütüphane ve üretim için Vercel ortam değişkenlerinde "
            "S3_* (Cloudflare R2) tanımlayıp görselleri bucket’a yükleyin; uygulama o zaman R2’den listeler. "
            "Yerel bilgisayarda çalıştırırken tam Mockups klasörü kullanılabilir."
        )
    if _r2_enabled():
        return (
            "R2 (S3_*) tanımlı ama katalog boş. Bucket’ta dosya var mı, S3_PREFIX doğru mu kontrol edin. "
            "Bir ağ/kimlik hatası Vercel Runtime log’larına düşmüş olabilir."
        )
    root = _mockups_root()
    if not root.is_dir():
        return f"Mockups klasörü yok: {root} — yolu kontrol edin."
    return (
        f"Katalog boş. {root} altında kategori adlarında alt klasörlere .png / .jpg / .webp template "
        "görselleri koyun."
    )


def _is_mockup_media_url(u: str) -> bool:
    return u.startswith("/media/mockups/") or u.startswith("/media/workspace-mockups/")


def _workspace_mockup_rel_url(rel: str) -> str:
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    return "/media/workspace-mockups/" + "/".join(quote(p, safe="") for p in parts)


def _safe_workspace_mockup_file_path(rel: str) -> Optional[Path]:
    root = WORKSPACE_MOCKUPS_ROOT.resolve()
    if not root.is_dir():
        return None
    rel_norm = (rel or "").strip().replace("\\", "/")
    if not rel_norm or ".." in rel_norm.split("/"):
        return None
    candidate = (root / rel_norm).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    return candidate


def _latest_workspace_batch_dir() -> Optional[Path]:
    root = WORKSPACE_MOCKUPS_ROOT.resolve()
    if not root.is_dir():
        return None
    dirs = [p for p in root.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _workspace_urls_for_batch(batch: Path) -> list[str]:
    urls: list[str] = []
    for p in sorted(batch.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            rel = p.resolve().relative_to(WORKSPACE_MOCKUPS_ROOT.resolve()).as_posix()
        except ValueError:
            continue
        urls.append(_workspace_mockup_rel_url(rel))
    return urls


def _workspace_path_from_media_url(url: str) -> Optional[Path]:
    u = str(url or "").strip()
    if not u.startswith("/media/workspace-mockups/"):
        return None
    rel = unquote(u[len("/media/workspace-mockups/") :]).strip().replace("\\", "/")
    if not rel or ".." in rel.split("/"):
        return None
    return _safe_workspace_mockup_file_path(rel)


def _template_paths_from_urls(urls: list[str], root: Path) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for raw in urls:
        u = str(raw or "").strip()
        if not u.startswith("/media/mockups/"):
            continue
        rel = unquote(u[len("/media/mockups/") :]).strip().replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            continue
        p = (root / rel).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            continue
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _template_rels_from_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        u = str(raw or "").strip()
        if not u.startswith("/media/mockups/"):
            continue
        rel = unquote(u[len("/media/mockups/") :]).strip().replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _download_r2_templates(temp_root: Path, rels: list[str]) -> list[Path]:
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return []
    client = _r2_client()
    out: list[Path] = []
    for rel in rels:
        ext = Path(rel).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        target = (temp_root / rel).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        key = _r2_key_for_rel(rel)
        client.download_file(bucket, key, str(target))
        out.append(target)
    return out


def _process_selected_mockups(
    cfg: MockupProcessingConfig,
    selected_paths: list[Path],
    log_callback: Optional[Callable[[str], None]] = None,
) -> tuple[list[Path], int]:
    out_paths: list[Path] = []
    failed = 0
    root = cfg.mockups_root.resolve()
    for mockup in selected_paths:
        try:
            rel = mockup.resolve().relative_to(root)
            out_path = cfg.output_root / rel.with_suffix(".png")
            placement = resolve_placement(cfg, mockup)
            with Image.open(mockup) as preview:
                lum = calculate_luminance(preview)
            design_to_use = pick_design_for_mockup(
                config=cfg, placement=placement, luminance=lum
            )
            width_percent = auto_fit_width_percent(
                placement.design_width_percent, design_to_use, cfg
            )
            compose_mockup(
                mockup_path=mockup,
                selected_design_path=design_to_use,
                output_path=out_path,
                design_width_percent=width_percent,
                design_y_offset_percent=placement.design_y_offset_percent,
                design_x_offset_percent=placement.design_x_offset_percent,
                print_area_left_px=placement.print_area_left_px,
                print_area_right_px=placement.print_area_right_px,
                print_area_top_px=placement.print_area_top_px,
                print_area_bottom_px=placement.print_area_bottom_px,
            )
            out_paths.append(out_path)
            if log_callback:
                log_callback(f"OK {rel.as_posix()}")
        except Exception as exc:
            failed += 1
            if log_callback:
                log_callback(f"FAIL {mockup.name}: {exc}")
    return out_paths, failed


async def _save_uploaded_design_file(upload: UploadFile, *, batch_id: str, role: str) -> Path:
    filename = (upload.filename or "").strip()
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"{role} design dosyası png/jpg/jpeg/webp olmalı.")
    content = await upload.read()
    if not content:
        raise RuntimeError(f"{role} design dosyası boş.")
    if len(content) > 20 * 1024 * 1024:
        raise RuntimeError(f"{role} design dosyası çok büyük (max 20MB).")
    WORKSPACE_DESIGNS_ROOT.mkdir(parents=True, exist_ok=True)
    out = WORKSPACE_DESIGNS_ROOT / f"{batch_id}_{role}{ext}"
    out.write_bytes(content)
    return out


def _normalize_tag_phrase(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip().lower())
    s = re.sub(r"[^a-z0-9 '&-]", "", s)
    s = s.strip(" -&'")
    if not s:
        return ""
    if len(s) > _ETSY_TAG_MAX_LEN:
        s = s[:_ETSY_TAG_MAX_LEN].rstrip(" -&'")
    return s


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        n = _normalize_tag_phrase(v)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _fallback_etsy_tags(keywords: list[str], title: str) -> list[str]:
    kws = _dedupe_preserve_order([str(x) for x in keywords if isinstance(x, str)])
    title_words = [
        w
        for w in re.findall(r"[a-z0-9][a-z0-9'-]{1,}", (title or "").lower())
        if len(w) >= 3
    ]
    title_kws = _dedupe_preserve_order(title_words)
    merged = kws + [w for w in title_kws if w not in kws]

    tags: list[str] = []
    for kw in merged:
        tags.append(kw)
        if len(tags) >= _ETSY_TAG_MAX_COUNT:
            break
        if " " not in kw:
            # Try producing short 2-word phrases from neighboring terms.
            for nxt in merged:
                if nxt == kw or " " in nxt:
                    continue
                phrase = _normalize_tag_phrase(f"{kw} {nxt}")
                if phrase and phrase not in tags and len(phrase) <= _ETSY_TAG_MAX_LEN:
                    tags.append(phrase)
                    break
        if len(tags) >= _ETSY_TAG_MAX_COUNT:
            break
    return _dedupe_preserve_order(tags)[:_ETSY_TAG_MAX_COUNT]


def _ai_rewrite_etsy_tags(keywords: list[str], title: str) -> list[str]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return []
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You generate Etsy SEO tags. Return strict JSON object with key "
                    "'tags' as array of up to 13 lowercase strings, each <=20 chars."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": title or "",
                        "keywords": keywords,
                        "constraints": {
                            "max_tags": _ETSY_TAG_MAX_COUNT,
                            "max_chars_per_tag": _ETSY_TAG_MAX_LEN,
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        resp.raise_for_status()
        data = resp.json()
        txt = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        parsed = json.loads(txt) if isinstance(txt, str) else {}
        raw_tags = parsed.get("tags") if isinstance(parsed, dict) else []
        if not isinstance(raw_tags, list):
            return []
        cleaned = _dedupe_preserve_order([str(x) for x in raw_tags])
        return cleaned[:_ETSY_TAG_MAX_COUNT]
    except Exception:
        return []


def _generate_etsy_tags(keywords: list[str], title: str) -> tuple[list[str], str]:
    ai_tags = _ai_rewrite_etsy_tags(keywords, title)
    if ai_tags:
        return ai_tags[:_ETSY_TAG_MAX_COUNT], "AI"
    return [], "AI"


def _etsy_placeholder_title() -> str:
    return (os.environ.get("ETSY_DRAFT_PLACEHOLDER_TITLE") or "Draft — add listing details in Etsy")[:140]


def _etsy_placeholder_description(source_url: str = "") -> str:
    base = (os.environ.get("ETSY_DRAFT_PLACEHOLDER_DESCRIPTION") or "").strip()
    if not base:
        base = (
            "Photos from Amazon listing (color variants). "
            "Edit title, description, price, and inventory in Etsy."
        )
    if source_url.strip():
        return f"{base}\n\nSource: {source_url.strip()}"[:49990]
    return base[:49990]


def _etsy_placeholder_price() -> str:
    s = (os.environ.get("ETSY_DRAFT_PLACEHOLDER_PRICE") or "19.99").strip()
    return s or "19.99"


def _minimal_create_listing_kwargs() -> tuple[dict[str, Any], str]:
    who, when, is_supply, note = normalize_listing_who_when_supply(
        who_made="i_did", when_made="made_to_order", is_supply=False
    )
    return (
        {
            "who_made": who,
            "when_made": when,
            "is_supply": is_supply,
            "taxonomy_id": None,
        },
        note,
    )


def _validate_amazon_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise RuntimeError("URL http/https olmalı.")
    host = (p.netloc or "").lower()
    if "amazon." not in host:
        raise RuntimeError("Yalnızca Amazon ürün URL destekleniyor.")


def _public_image_fetch_url(url: str) -> str:
    """Etsy yüklemesi için httpx'in GET atabileceği mutlak URL (yerel /media/... için)."""
    u = (url or "").strip()
    if not u:
        return u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/"):
        base = (os.environ.get("APP_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/")
        return base + u
    return u


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
            fetch_url = _public_image_fetch_url(url)
            upload_listing_image_from_url(listing_id, fetch_url, rank=rank, overwrite=True)
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
    row = {
        "listing_id": int(listing_id),
        "etsy_listing_id": int(listing_id),
        "title": str(draft.get("title") or _etsy_placeholder_title())[:140],
        "price": str(price or _etsy_placeholder_price()),
        "sku": "",
        "variation_preset": "",
        "section": "",
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


def _normalize_amazon_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except Exception:
        return raw.lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    return f"{host}{path}".lower()


def _extract_asin_from_text(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    m = re.search(r"\b([A-Z0-9]{10})\b", raw.upper())
    return m.group(1) if m else ""


def _find_existing_draft_match(url: str, asin: str = "") -> Optional[dict[str, Any]]:
    want_url = _normalize_amazon_url(url)
    want_asin = _extract_asin_from_text(asin)
    if not want_url and not want_asin:
        return None
    for row in _load_app_draft_listings(limit=500):
        row_url = _normalize_amazon_url(str(row.get("amazon_url") or row.get("source_url") or ""))
        row_asin = _extract_asin_from_text(str(row.get("source_item_id") or ""))
        if want_url and row_url and want_url == row_url:
            return row
        if want_asin and row_asin and want_asin == row_asin:
            return row
    return None


def _load_workspace_states() -> dict[str, Any]:
    try:
        if not WORKSPACE_STATES_FILE.exists():
            return {}
        raw = json.loads(WORKSPACE_STATES_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_workspace_state(payload: dict[str, Any]) -> str:
    states = _load_workspace_states()
    # lightweight cleanup of stale entries
    now = time.time()
    keep: dict[str, Any] = {}
    for k, v in states.items():
        if not isinstance(v, dict):
            continue
        ts = v.get("_ts")
        if isinstance(ts, (int, float)) and now - float(ts) < 2 * 24 * 3600:
            keep[k] = v
    state_id = uuid4().hex
    rec = dict(payload)
    rec["_ts"] = now
    if isinstance(rec.get("workspace_draft"), dict):
        rec["workspace_draft"] = _workspace_draft_stripped_for_storage(rec["workspace_draft"])
    keep[state_id] = rec
    WORKSPACE_STATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = json.dumps(keep, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        for v in keep.values():
            if isinstance(v, dict) and isinstance(v.get("workspace_draft"), dict):
                v["workspace_draft"] = _workspace_draft_stripped_for_storage(v["workspace_draft"])
                if isinstance(v["workspace_draft"], dict):
                    v["workspace_draft"].pop("notes", None)
        text = json.dumps(keep, ensure_ascii=False, indent=2)
    WORKSPACE_STATES_FILE.write_text(text, encoding="utf-8")
    return state_id


def _redirect_workspace_state(payload: dict[str, Any]) -> RedirectResponse:
    sid = _save_workspace_state(payload)
    return RedirectResponse(url=f"/?state={sid}", status_code=303)


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


def _normalize_images_only_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Workspace için görsel kaynaklarını normalize eder (amazon/mockup)."""
    assets = draft.get("workspace_assets")
    if not isinstance(assets, dict):
        assets = {}
    imgs = [str(x).strip() for x in (draft.get("images") or []) if isinstance(x, str) and str(x).strip()]
    prev_a = assets.get("amazon_images")
    prev_m = assets.get("mockup_images")
    prev_a_list = [str(x).strip() for x in prev_a] if isinstance(prev_a, list) else []
    prev_m_list = [str(x).strip() for x in prev_m] if isinstance(prev_m, list) else []
    from_amazon = [u for u in imgs if not _is_mockup_media_url(u)]
    from_mockups = [u for u in imgs if _is_mockup_media_url(u)]
    seen: set[str] = set()
    amazon: list[str] = []
    for u in prev_a_list + from_amazon:
        if not u or _is_mockup_media_url(u) or u in seen:
            continue
        seen.add(u)
        amazon.append(u)
    seen_m: set[str] = set()
    mockups: list[str] = []
    for u in prev_m_list + from_mockups:
        if not u or not _is_mockup_media_url(u) or u in seen_m:
            continue
        seen_m.add(u)
        mockups.append(u)
    assets["amazon_images"] = amazon
    assets["mockup_images"] = mockups
    active_raw = str(assets.get("active_source") or "").strip().lower()
    active = active_raw if active_raw in {"amazon", "mockups"} else "amazon"
    if active == "mockups" and not mockups and amazon:
        active = "amazon"
    assets["active_source"] = active
    draft["workspace_assets"] = assets
    draft["images"] = mockups if active == "mockups" else amazon
    if "title" not in draft or not isinstance(draft.get("title"), str):
        draft["title"] = ""
    if "keywords" not in draft or not isinstance(draft.get("keywords"), list):
        draft["keywords"] = []
    if "tags" not in draft or not isinstance(draft.get("tags"), list):
        draft["tags"] = []
    draft["tags"] = _dedupe_preserve_order([str(x) for x in draft["tags"]])[:_ETSY_TAG_MAX_COUNT]
    if "variations" not in draft or not isinstance(draft.get("variations"), list):
        draft["variations"] = []
    if "workspace_meta" in draft:
        del draft["workspace_meta"]
    return draft


def _workspace_ui(draft: Optional[dict[str, Any]]) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "has_draft": False,
        "images": [],
        "amazon_images": [],
        "mockup_images": [],
        "active_source": "amazon",
        "source_url": "",
        "item_id": "",
        "image_count": 0,
        "variations": [],
        "title": "",
        "keywords": [],
        "tags": [],
    }
    if not draft:
        return empty
    assets = draft.get("workspace_assets")
    if not isinstance(assets, dict):
        assets = {}
    amazon_images = assets.get("amazon_images")
    if not isinstance(amazon_images, list):
        amazon_images = [
            x
            for x in (draft.get("images") or [])
            if isinstance(x, str)
            and str(x).strip()
            and not _is_mockup_media_url(str(x).strip())
        ]
    amazon_images = [
        str(x).strip()
        for x in amazon_images
        if isinstance(x, str) and str(x).strip() and not _is_mockup_media_url(str(x).strip())
    ]
    mockup_raw = assets.get("mockup_images")
    mockup_images = [str(x).strip() for x in mockup_raw] if isinstance(mockup_raw, list) else []
    mockup_images = [x for x in mockup_images if _is_mockup_media_url(x)]
    active = str(assets.get("active_source") or "amazon").strip().lower()
    if active not in {"amazon", "mockups"}:
        active = "amazon"
    if active == "mockups" and not mockup_images and amazon_images:
        active = "amazon"
    current_images = mockup_images if active == "mockups" else amazon_images
    src = draft.get("source") or {}
    if not isinstance(src, dict):
        src = {}
    n = len(current_images)
    var_raw = draft.get("variations")
    variations_out: list[dict[str, Any]] = []
    if isinstance(var_raw, list):
        for row in var_raw:
            if isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("values"), list):
                vals = [str(v).strip() for v in row["values"] if str(v).strip()]
                if vals:
                    variations_out.append({"name": row["name"].strip(), "values": vals})
    kws = draft.get("keywords")
    keywords = [str(x).strip() for x in kws] if isinstance(kws, list) else []
    keywords = [x for x in keywords if x]
    tags_raw = draft.get("tags")
    tags = [str(x).strip() for x in tags_raw] if isinstance(tags_raw, list) else []
    tags = [x for x in tags if x]
    return {
        # Workspace paneli, görsel olmasa da draft varsa açık kalsın.
        "has_draft": True,
        "images": current_images,
        "amazon_images": amazon_images,
        "mockup_images": mockup_images,
        "active_source": active,
        "source_url": str(src.get("url") or ""),
        "item_id": str(src.get("item_id") or ""),
        "image_count": n,
        "variations": variations_out,
        "title": str(draft.get("title") or "").strip(),
        "keywords": keywords[:20],
        "tags": tags[:_ETSY_TAG_MAX_COUNT],
    }


def _draft_image_count(d: dict[str, Any]) -> int:
    imgs = d.get("images")
    if not isinstance(imgs, list):
        return 0
    return len([x for x in imgs if isinstance(x, str) and str(x).strip()])


def _build_draft(url: str, no_playwright: bool) -> tuple[dict[str, Any], str]:
    _validate_amazon_url(url)
    clean_url = canonical_amazon_dp_url(url)
    last_html = ""
    scrape_note = ""

    def _fast_http_draft() -> dict[str, Any]:
        nonlocal last_html
        last_html = fetch_html_simple(clean_url)
        listing_fast = parse_rendered_html(last_html, clean_url)
        d = to_draft_dict(listing_fast)
        d["source"] = dict(d.get("source") or {})
        d["source"]["url"] = clean_url
        return d

    if no_playwright:
        draft = _fast_http_draft()
    else:
        # Önce urllib çoğu zaman "Continue shopping" bot sayfası döndürür; doğrudan Playwright.
        try:
            listing = scrape_with_playwright(
                clean_url,
                headless=True,
                wait_ms=4500,
                variant_wait_ms=1800,
                goto_timeout_ms=45000,
            )
            draft = to_draft_dict(listing)
            draft["source"] = dict(draft.get("source") or {})
            draft["source"]["url"] = clean_url
        except Exception as exc:
            scrape_note = f"Playwright çalışmadı — HTTP sonucuna düşüldü: {exc}"
            draft = _fast_http_draft()
        else:
            if _draft_image_count(draft) < 1:
                scrape_note = (
                    "Tarayıcı oturumunda görsel çıkmadı (Amazon doğrulama veya bot sayfası olabilir). "
                    "HTTP ile tekrar denendi."
                )
                draft_http = _fast_http_draft()
                if _draft_image_count(draft_http) > 0:
                    draft = draft_http
                    scrape_note += f" HTTP: {_draft_image_count(draft)} görsel."
                else:
                    scrape_note += (
                        " HTTP de boş. Terminalde: playwright install chromium — "
                        "veya kısa linki normal tarayıcıda açıp ürün sayfası geldiğini doğrulayın: "
                        f"{clean_url}"
                    )
    imgs = draft.get("images") if isinstance(draft.get("images"), list) else []
    draft["workspace_assets"] = {
        "amazon_images": [str(x).strip() for x in imgs if isinstance(x, str) and str(x).strip()],
        "active_source": "amazon",
    }
    if scrape_note:
        dbg = draft.get("debug")
        if not isinstance(dbg, dict):
            dbg = {}
        dbg["scrape_note"] = scrape_note
        draft["debug"] = dbg
    out = _normalize_images_only_draft(draft)
    return out, scrape_note


def _workspace_draft_json_for_page(draft: dict[str, Any]) -> str:
    """
    Sayfaya gömülecek taslak: devasa debug alanını çıkarır (parse/performans),
    tek satır JSON (</script> kaçış riski azalır).
    """
    wa = draft.get("workspace_assets")
    if not isinstance(wa, dict):
        wa = {}
    ai = wa.get("amazon_images")
    ai_list = ai if isinstance(ai, list) else []
    ai_f = [
        str(x).strip()
        for x in ai_list
        if isinstance(x, str) and str(x).strip() and not _is_mockup_media_url(str(x).strip())
    ]
    mi = wa.get("mockup_images")
    mi_list = mi if isinstance(mi, list) else []
    mi_f = [
        str(x).strip()
        for x in mi_list
        if isinstance(x, str) and _is_mockup_media_url(str(x).strip())
    ]
    active = str(wa.get("active_source") or "amazon").strip().lower()
    if active not in {"amazon", "mockups"}:
        active = "amazon"
    current = mi_f if active == "mockups" else ai_f
    slim: dict[str, Any] = {
        "source": draft.get("source") if isinstance(draft.get("source"), dict) else {},
        "title": str(draft.get("title") or "").strip(),
        "images": current,
        "workspace_assets": {
            "amazon_images": ai_f,
            "mockup_images": mi_f,
            "active_source": active,
        },
        "variations": draft.get("variations") if isinstance(draft.get("variations"), list) else [],
        "keywords": draft.get("keywords") if isinstance(draft.get("keywords"), list) else [],
        "tags": draft.get("tags") if isinstance(draft.get("tags"), list) else [],
    }
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def _json_for_html_script_embed(json_text: str) -> str:
    """<script type=application/json> içinde </script> veya <…> HTML ayrıştırıcıyı kırmasın."""
    return json_text.replace("<", "\\u003c").replace(">", "\\u003e")


def _render_index(
    request: Request,
    *,
    error: Optional[str] = None,
    status: Optional[str] = None,
    warning: Optional[str] = None,
    workspace_url: str = "",
    workspace_draft: Optional[dict[str, Any]] = None,
    workspace_draft_json: str = "",
    workspace_listing_id: str = "",
    etsy_shop_name: str = "",
) -> HTMLResponse:
    if not etsy_shop_name:
        etsy_shop_name = _etsy_shop_display_name()
    if workspace_draft is not None:
        workspace_draft_json = _json_for_html_script_embed(
            _workspace_draft_json_for_page(workspace_draft)
        )
    elif not workspace_draft_json.strip():
        workspace_draft_json = "{}"
    else:
        workspace_draft_json = _json_for_html_script_embed(workspace_draft_json)
    ws = _workspace_ui(workspace_draft)
    mockup_categories = _list_mockup_catalog()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "active_nav": "workspace",
            "error": error,
            "status": status,
            "warning": warning,
            "workspace_url": workspace_url,
            "workspace_draft": workspace_draft,
            "workspace_draft_json": workspace_draft_json,
            "etsy_placeholder_price": _etsy_placeholder_price(),
            "workspace_listing_id": workspace_listing_id,
            "etsy_shop_name": etsy_shop_name,
            "ws": ws,
            "mockup_categories": mockup_categories,
            "mockup_library_note": _mockup_library_empty_note(mockup_categories),
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    sid = str(request.query_params.get("state") or "").strip()
    if sid:
        states = _load_workspace_states()
        rec = states.get(sid)
        if isinstance(rec, dict):
            wd_raw = rec.get("workspace_draft")
            wd_out: Optional[dict[str, Any]] = None
            if isinstance(wd_raw, dict):
                wd_out = _normalize_images_only_draft(copy.deepcopy(wd_raw))
            wli = rec.get("workspace_listing_id")
            wli_s = str(wli).strip() if wli is not None and str(wli).strip() else ""
            return _render_index(
                request,
                error=rec.get("error") if isinstance(rec.get("error"), str) else None,
                status=rec.get("status") if isinstance(rec.get("status"), str) else None,
                warning=rec.get("warning") if isinstance(rec.get("warning"), str) else None,
                workspace_url=rec.get("workspace_url") if isinstance(rec.get("workspace_url"), str) else "",
                workspace_draft=wd_out,
                workspace_listing_id=wli_s,
            )
        return _render_index(
            request,
            warning=(
                "Bu oturum bağlantısı geçersiz veya süresi doldu (sayfayı yeniden yükleyip "
                "Amazon URL’sini tekrar gönderin)."
            ),
        )
    return _render_index(request)


@app.get("/studio", response_class=HTMLResponse)
def studio_page(
    request: Request,
    status: str = Query(""),
    warning: str = Query(""),
    error: str = Query(""),
    batch: str = Query(""),
) -> HTMLResponse:
    batch_dir: Optional[Path] = None
    if batch.strip():
        cand = (WORKSPACE_MOCKUPS_ROOT / batch.strip()).resolve()
        try:
            cand.relative_to(WORKSPACE_MOCKUPS_ROOT.resolve())
            if cand.is_dir():
                batch_dir = cand
        except ValueError:
            batch_dir = None
    if batch_dir is None:
        batch_dir = _latest_workspace_batch_dir()
    generated_urls = _workspace_urls_for_batch(batch_dir) if batch_dir else []
    mockups_root = _mockups_root()
    mockup_categories = _list_mockup_catalog()
    return templates.TemplateResponse(
        request,
        "studio.html",
        {
            "request": request,
            "active_nav": "studio",
            "status": status.strip() or None,
            "warning": warning.strip() or None,
            "error": error.strip() or None,
            "mockup_categories": mockup_categories,
            "mockup_library_note": _mockup_library_empty_note(mockup_categories),
            "mockups_root_path": str(mockups_root),
            "mockups_root_exists": mockups_root.is_dir(),
            "generated_urls": generated_urls,
            "current_batch": batch_dir.name if batch_dir else "",
            "etsy_shop_name": _etsy_shop_display_name(),
        },
    )


@app.get("/media/mockups/{path:path}")
def mockup_media(path: str) -> FileResponse:
    if _r2_enabled():
        rel = (path or "").strip().replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            raise HTTPException(status_code=404, detail="Mockup bulunamadı")
        ext = Path(rel).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise HTTPException(status_code=404, detail="Mockup bulunamadı")
        key = _r2_key_for_rel(rel)
        bucket = (os.environ.get("S3_BUCKET") or "").strip()
        media = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }
        try:
            resp = _r2_client().get_object(Bucket=bucket, Key=key)
            data = resp["Body"].read()
        except Exception:
            raise HTTPException(status_code=404, detail="Mockup bulunamadı")
        ct = media.get(ext, "application/octet-stream")
        return Response(content=data, media_type=ct)
    p = _safe_mockup_file_path(path)
    if not p:
        raise HTTPException(status_code=404, detail="Mockup bulunamadı")
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    ct = media.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=ct, filename=p.name)


@app.get("/media/workspace-mockups/{path:path}")
def workspace_mockup_media(path: str) -> FileResponse:
    p = _safe_workspace_mockup_file_path(path)
    if not p:
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    ct = media.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=ct, filename=p.name)


@app.post("/studio/set-mockups-dir", response_class=HTMLResponse)
def studio_set_mockups_dir(mockups_dir: str = Form("")) -> HTMLResponse:
    cleaned = (mockups_dir or "").strip()
    if not cleaned:
        os.environ.pop("MOCKUPS_DIR", None)
        return RedirectResponse(url="/studio?status=Mockups%20klasoru%20varsayilana%20alindi.", status_code=303)
    resolved = Path(cleaned).expanduser().resolve()
    os.environ["MOCKUPS_DIR"] = str(resolved)
    if not resolved.is_dir():
        msg = quote(f"Mockups klasoru bulunamadi: {resolved}")
        return RedirectResponse(url=f"/studio?warning={msg}", status_code=303)
    status_msg = quote(f"Mockups klasoru guncellendi: {resolved}")
    if _on_vercel() and not _r2_enabled():
        w = quote(
            "Vercel: disk üzerinde büyük mockup görselleri yok; kütüphane için S3_*(R2) gerekir. "
            "Bu sadece placement.json yoludur, şablon PNG’lerini bekleme."
        )
        return RedirectResponse(
            url=f"/studio?status={status_msg}&warning={w}", status_code=303
        )
    return RedirectResponse(url=f"/studio?status={status_msg}", status_code=303)


@app.post("/studio/generate-mockups", response_class=HTMLResponse)
async def studio_generate_mockups(
    design_white_file: UploadFile = File(...),
    design_black_file: UploadFile = File(...),
    selected_template_urls: str = Form("[]"),
) -> HTMLResponse:
    try:
        r2_mode = _r2_enabled()
        root = _mockups_root()
        if not r2_mode and not root.is_dir():
            raise RuntimeError(f"Mockups klasoru bulunamadi: {root}")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = f"studio_{stamp}_{uuid4().hex[:8]}"
        white_design_path = await _save_uploaded_design_file(
            design_white_file, batch_id=batch_id, role="white"
        )
        black_design_path = await _save_uploaded_design_file(
            design_black_file, batch_id=batch_id, role="black"
        )
        out_root = (WORKSPACE_MOCKUPS_ROOT / batch_id).resolve()
        run_root = root
        if r2_mode:
            run_root = (WORKSPACE_DESIGNS_ROOT / f"{batch_id}_mockups_src").resolve()
            run_root.mkdir(parents=True, exist_ok=True)
        cfg = MockupProcessingConfig(
            mockups_root=run_root,
            dark_design_path=black_design_path,
            light_design_path=white_design_path,
            output_root=out_root,
        )
        selected_raw: list[str] = []
        try:
            p = json.loads(selected_template_urls or "[]")
            if isinstance(p, list):
                selected_raw = [str(x).strip() for x in p if isinstance(x, str) and str(x).strip()]
        except Exception:
            selected_raw = []
        if r2_mode:
            selected_rels = _template_rels_from_urls(selected_raw)
            if not selected_rels:
                raise RuntimeError("R2 modunda en az bir template secilmelidir.")
            selected_paths = _download_r2_templates(run_root, selected_rels)
        else:
            selected_paths = _template_paths_from_urls(selected_raw, root)
        if selected_raw and not selected_paths:
            raise RuntimeError("Secilen template'ler gecersiz veya bulunamadi.")
        if selected_paths:
            out_paths, failed = _process_selected_mockups(cfg, selected_paths)
        else:
            out_paths, failed = process_all(cfg)
        if not out_paths:
            raise RuntimeError("Mockup uretilemedi.")
        status = quote(f"{len(out_paths)} mockup uretildi." + (f" {failed} dosya basarisiz." if failed else ""))
        return RedirectResponse(url=f"/studio?status={status}&batch={quote(batch_id)}", status_code=303)
    except Exception as exc:
        return RedirectResponse(url=f"/studio?warning={quote(str(exc))}", status_code=303)


@app.get("/studio/download-latest-mockups")
def studio_download_latest_mockups() -> Response:
    batch = _latest_workspace_batch_dir()
    if not batch:
        raise HTTPException(status_code=404, detail="Indirilecek mockup batch bulunamadi.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(batch.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(batch).as_posix()
            zf.write(p, arcname=rel)
    headers = {
        "Content-Disposition": f'attachment; filename="{batch.name}.zip"',
        "Cache-Control": "no-store",
    }
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


@app.post("/studio/download-selected-mockups")
def studio_download_selected_mockups(selected_urls: str = Form("[]")) -> Response:
    try:
        raw = json.loads(selected_urls or "[]")
    except Exception:
        raw = []
    if not isinstance(raw, list):
        raw = []
    files: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        p = _workspace_path_from_media_url(item)
        if not p:
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        files.append(p)
    if not files:
        raise HTTPException(status_code=400, detail="Indirilecek secili mockup bulunamadi.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            try:
                rel = p.resolve().relative_to(WORKSPACE_MOCKUPS_ROOT.resolve()).as_posix()
            except ValueError:
                rel = p.name
            zf.write(p, arcname=rel)
    headers = {
        "Content-Disposition": 'attachment; filename="selected_mockups.zip"',
        "Cache-Control": "no-store",
    }
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


@app.get("/media/amazon-image")
def amazon_image_proxy(u: str = Query("", max_length=4500)) -> Response:
    """Amazon ürün görselini sunucu üzerinden ilet (tarayıcıda doğrudan CDN sık engellenir)."""
    raw = unquote(u).strip()
    if not raw or not is_amazon_cdn_product_image_url(raw):
        raise HTTPException(status_code=400, detail="Geçersiz görsel URL")
    req = urllib.request.Request(
        raw,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            data = resp.read()
            if len(data) > 30 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Görsel çok büyük")
            ct = resp.headers.get("content-type") or "image/jpeg"
            if not ct.startswith("image/"):
                ct = "image/jpeg"
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Görsel indirilemedi")
    return Response(content=data, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})


@app.post("/workspace/scrape", response_class=HTMLResponse)
def workspace_scrape(
    request: Request,
    url: str = Form(...),
    no_playwright: bool = Form(False),
) -> HTMLResponse:
    try:
        clean_url = canonical_amazon_dp_url(url)
        pre_match = _find_existing_draft_match(clean_url) or _find_existing_draft_match(url)
        draft, scrape_note = _build_draft(url, no_playwright)
        if isinstance(draft, dict):
            _normalize_images_only_draft(draft)
        asin = ""
        if isinstance(draft, dict):
            src = draft.get("source")
            if isinstance(src, dict):
                asin = str(src.get("item_id") or "")
        match = _find_existing_draft_match(clean_url, asin) or _find_existing_draft_match(url, asin) or pre_match
        warn = None
        if match:
            mid = str(match.get("listing_id") or match.get("etsy_listing_id") or "").strip()
            asin_s = str(match.get("source_item_id") or "").strip()
            warn = (
                f"Bu link/ASIN için daha önce Etsy draft oluşturulmuş görünüyor"
                f"{' (listing: ' + mid + ')' if mid else ''}"
                f"{' | ASIN: ' + asin_s if asin_s else ''}. "
                "Yeni draft açmadan önce mevcut listing’i güncellemek isteyebilirsin."
            )
        img_n = 0
        if isinstance(draft, dict):
            imgs = draft.get("images")
            if isinstance(imgs, list):
                img_n = len([x for x in imgs if isinstance(x, str) and x.strip()])
        if scrape_note:
            warn = f"{scrape_note} {warn}" if warn else scrape_note
        if img_n == 0:
            extra = (
                "Hiç görsel alınamadı. Uzun takip linkleri yerine kısa adres kullanıldı: "
                f"{clean_url} — 'HTTP only' kutusunu kapatıp tekrar deneyin (Chromium/Playwright gerekir). "
                "Sunucu Amazon’a bot gibi görünüyorsa tarayıcı modu şarttır."
            )
            warn = f"{extra} {warn}" if warn else extra
            status = "Yükleme tamamlandı ancak görsel bulunamadı (yukarıdaki uyarıya bakın)."
        else:
            status = (
                f"{img_n} görsel hazır (her renk/varyant ASIN için en fazla 4 fotoğraf). "
                "Etsy’ye yalnız fotoğraflı taslak için aşağıdaki düğmeyi kullanın."
            )
        return _redirect_workspace_state(
            {
                "status": status,
                "warning": warn,
                "workspace_url": clean_url,
                "workspace_draft": draft,
            }
        )
    except Exception as exc:
        return _redirect_workspace_state(
            {
                "error": str(exc),
                "workspace_url": url,
            }
        )


@app.post("/workspace/publish", response_class=HTMLResponse)
def workspace_publish(
    request: Request,
    draft_json: str = Form(...),
    etsy_update_listing_id: str = Form(""),
) -> HTMLResponse:
    try:
        draft = json.loads(draft_json)
        if not isinstance(draft, dict):
            raise RuntimeError("Geçersiz taslak verisi.")
        draft = _normalize_images_only_draft(draft)
        src = draft.get("source") if isinstance(draft.get("source"), dict) else {}
        source_url = str(src.get("url") or "")
        draft_title = str(draft.get("title") or "").strip()
        title = draft_title[:140] if draft_title else _etsy_placeholder_title()
        desc = _etsy_placeholder_description(source_url)
        price = _etsy_placeholder_price()

        raw_imgs = draft.get("images") or []
        images = [x for x in raw_imgs if isinstance(x, str) and x.strip()][:20]
        if not images:
            raise RuntimeError("Etsy’ye göndermek için en az bir görsel URL’si gerekir.")
        raw_tags = draft.get("tags")
        tags = [str(x).strip() for x in raw_tags] if isinstance(raw_tags, list) else []
        tags = [x for x in tags if x][:13]

        listing_id: int
        if etsy_update_listing_id.strip():
            listing_id = int(etsy_update_listing_id.strip())
            update_existing_listing(
                listing_id=listing_id,
                title=title,
                tags=tags if tags else None,
            )
            status = f"Mevcut Etsy taslağına görseller yüklendi: {listing_id}"
            if draft_title:
                status += " | title güncellendi"
            if tags:
                status += f" | {len(tags)} tag güncellendi"
            _save_app_draft_listing(listing_id=listing_id, draft=draft, price=price, mode="update")
        else:
            listing_opts, mp_note = _minimal_create_listing_kwargs()
            result = create_draft_listing(
                title=title,
                description=desc,
                price=price,
                quantity=1,
                tags=tags if tags else None,
                **listing_opts,
            )
            listing_id = int(result.get("listing_id"))
            status = f"Etsy draft oluşturuldu (yalnızca yer tutucu metin + görseller): {listing_id}"
            if mp_note:
                status += " | " + mp_note
            if tags:
                status += f" | {len(tags)} tag eklendi"
            _save_app_draft_listing(listing_id=listing_id, draft=draft, price=price, mode="create")

        status += " | " + _upload_images_best_effort(listing_id, images)

        return _redirect_workspace_state(
            {
                "status": status,
                "workspace_draft": draft if isinstance(draft, dict) else None,
                "workspace_listing_id": etsy_update_listing_id,
            }
        )
    except Exception as exc:
        try:
            draft_err = json.loads(draft_json)
        except Exception:
            draft_err = None
        return _redirect_workspace_state(
            {
                "error": str(exc),
                "workspace_draft": draft_err if isinstance(draft_err, dict) else None,
                "workspace_listing_id": etsy_update_listing_id,
            }
        )


@app.post("/workspace/generate-mockups", response_class=HTMLResponse)
async def workspace_generate_mockups(
    request: Request,
    design_white_file: UploadFile = File(...),
    design_black_file: UploadFile = File(...),
    draft_json: str = Form("{}"),
    workspace_url: str = Form(""),
    selected_template_urls: str = Form("[]"),
) -> HTMLResponse:
    draft: dict[str, Any] = {}
    try:
        parsed = json.loads(draft_json or "{}")
        if isinstance(parsed, dict):
            draft = _normalize_images_only_draft(parsed)
    except Exception:
        draft = {}
    try:
        r2_mode = _r2_enabled()
        root = _mockups_root()
        if not r2_mode and not root.is_dir():
            raise RuntimeError(f"Mockups klasörü bulunamadı: {root}")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = f"web_{stamp}_{uuid4().hex[:8]}"
        # Etsy/Mockup mantığı:
        # - Açık (light) template'lere siyah design basılır -> dark_design_path
        # - Koyu (dark) template'lere beyaz design basılır -> light_design_path
        white_design_path = await _save_uploaded_design_file(
            design_white_file, batch_id=batch_id, role="white"
        )
        black_design_path = await _save_uploaded_design_file(
            design_black_file, batch_id=batch_id, role="black"
        )

        out_root = (WORKSPACE_MOCKUPS_ROOT / batch_id).resolve()
        run_root = root
        if r2_mode:
            run_root = (WORKSPACE_DESIGNS_ROOT / f"{batch_id}_mockups_src").resolve()
            run_root.mkdir(parents=True, exist_ok=True)
        cfg = MockupProcessingConfig(
            mockups_root=run_root,
            dark_design_path=black_design_path,
            light_design_path=white_design_path,
            output_root=out_root,
        )
        selected_raw: list[str] = []
        try:
            p = json.loads(selected_template_urls or "[]")
            if isinstance(p, list):
                selected_raw = [str(x).strip() for x in p if isinstance(x, str) and str(x).strip()]
        except Exception:
            selected_raw = []
        if r2_mode:
            selected_rels = _template_rels_from_urls(selected_raw)
            if not selected_rels:
                raise RuntimeError("R2 modunda en az bir template secilmelidir.")
            selected_paths = _download_r2_templates(run_root, selected_rels)
        else:
            selected_paths = _template_paths_from_urls(selected_raw, root)
        if selected_raw and not selected_paths:
            raise RuntimeError("Secilen template'ler gecersiz veya bulunamadi.")
        out_paths, failed = _process_selected_mockups(cfg, selected_paths) if selected_paths else process_all(cfg)
        if not out_paths:
            raise RuntimeError("Mockup üretilemedi. Mockups klasörünü ve design dosyasını kontrol edin.")
        urls: list[str] = []
        for p in out_paths:
            try:
                rel = p.resolve().relative_to(WORKSPACE_MOCKUPS_ROOT.resolve()).as_posix()
            except ValueError:
                continue
            urls.append(_workspace_mockup_rel_url(rel))
        if not urls:
            raise RuntimeError("Mockup görselleri URL'e dönüştürülemedi.")

        d_assets = draft.get("workspace_assets")
        assets = d_assets if isinstance(d_assets, dict) else {}
        assets["mockup_images"] = urls
        assets["active_source"] = "mockups"
        draft["workspace_assets"] = assets
        draft["images"] = urls
        draft = _normalize_images_only_draft(draft)
        msg = f"{len(urls)} mockup üretildi."
        if failed:
            msg += f" {failed} dosya üretilemedi."
        return _redirect_workspace_state(
            {
                "status": msg,
                "workspace_draft": draft,
                "workspace_url": workspace_url,
            }
        )
    except Exception as exc:
        return _redirect_workspace_state(
            {
                "warning": str(exc),
                "workspace_draft": draft if isinstance(draft, dict) else None,
                "workspace_url": workspace_url,
            }
        )


@app.post("/workspace/generate-tags", response_class=HTMLResponse)
def workspace_generate_tags(
    request: Request,
    draft_json: str = Form(...),
) -> HTMLResponse:
    try:
        draft = json.loads(draft_json)
        if not isinstance(draft, dict):
            raise RuntimeError("Geçersiz taslak verisi.")
        draft = _normalize_images_only_draft(draft)
        kws = draft.get("keywords")
        keywords = [str(x).strip() for x in kws] if isinstance(kws, list) else []
        keywords = [x for x in keywords if x]
        title = str(draft.get("title") or "").strip()
        tags, mode = _generate_etsy_tags(keywords, title)
        if not tags:
            raise RuntimeError("Tag üretilemedi. Önce scrape ile keyword alın.")
        draft["tags"] = tags
        return _redirect_workspace_state(
            {
                "status": f"{len(tags)} Etsy tag üretildi ({mode}).",
                "workspace_draft": draft,
            }
        )
    except Exception as exc:
        try:
            draft_err = json.loads(draft_json)
        except Exception:
            draft_err = None
        return _redirect_workspace_state(
            {
                "error": str(exc),
                "workspace_draft": draft_err if isinstance(draft_err, dict) else None,
            }
        )


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("webapp:app", host="127.0.0.1", port=_port, reload=False)
