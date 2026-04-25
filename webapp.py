from __future__ import annotations

import copy
import io
import json
import logging
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, unquote, urlparse

import httpx
import boto3
from botocore.config import Config
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

load_dotenv()

# Vercel serverless: proje dizini salt okunur; state ve üretilen mockuplar /tmp altında tutulur.
def _drafts_base_dir() -> Path:
    if (os.environ.get("VERCEL") or "").strip():
        return Path("/tmp/drafts")
    return Path("drafts")


_DRAFTS_BASE = _drafts_base_dir()

app = FastAPI(title="Mockup -> Etsy Importer")
_templates_dir = (Path(__file__).resolve().parent / "templates").resolve()
templates = Jinja2Templates(directory=str(_templates_dir))


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
# Şablon / üretilmiş mockup görselleri: tarayıcı ve CDN kenarında önbellek (tekrar ziyaret hızlanır).
_MOCKUP_IMAGE_CACHE_CONTROL = "public, max-age=604800, stale-while-revalidate=86400"


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


def _r2_endpoint_url() -> str:
    """Cloudflare dokümantasyonundan <account_id> köşeli parantezleri kopyalanmissa temizle."""
    raw = (os.environ.get("S3_ENDPOINT") or "").strip()
    return raw.replace("<", "").replace(">", "").strip()


def _r2_client():
    endpoint = _r2_endpoint_url()
    region = (os.environ.get("S3_REGION") or "auto").strip() or "auto"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=(os.environ.get("S3_ACCESS_KEY_ID") or "").strip(),
        aws_secret_access_key=(os.environ.get("S3_SECRET_ACCESS_KEY") or "").strip(),
        config=Config(signature_version="s3v4"),
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
        ws_staging = _r2_workspace_output_prefix().strip("/")
        for rel in keys:
            rnorm = str(rel or "").replace("\\", "/").lstrip("/")
            if ws_staging and (rnorm == ws_staging or rnorm.startswith(f"{ws_staging}/")):
                continue
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


def _s3_prefix_label() -> str:
    raw = (os.environ.get("S3_PREFIX") or "").strip()
    return raw if raw else "(boş — nesneler bucket kökünde, örn. CC Long/1.png)"


def _mockup_catalog_image_count(categories: list[dict[str, Any]]) -> int:
    n = 0
    for cat in categories:
        imgs = cat.get("images")
        if isinstance(imgs, list):
            n += len(imgs)
    return n


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
            "Yerel Mockups klasörünü bucketa doğru yapıda yüklemek için repodaki "
            "scripts/sync-mockups-to-r2.sh scriptini kullanın (.env içinde aynı S3_* değişkenleri). "
            "Yerel bilgisayarda çalıştırırken tam Mockups klasörü kullanılabilir."
        )
    if _r2_enabled():
        pfx = _s3_prefix_label()
        return (
            "R2 (S3_*) tanımlı ama katalog boş. En sık neden: Vercel’deki S3_PREFIX ile bucketa yüklediğiniz "
            "yol uyuşmuyor. Şu an uygulama şu prefix ile listeliyor: "
            f"{pfx}. "
            "Dosyaları bucket köküne (T-shirt/1.png gibi) koyduysanız S3_PREFIX’i tamamen silin/boş bırakın. "
            "Sync scriptinde prefix kullandıysanız Vercel’de aynı değeri verin. "
            "Kökte hâlâ 'CC Long:1.png' gibi iki noktalı isimler varsa silin; uygulama 'CC Long/1.png' arar. "
            "Listeleme hatası olmuşsa Vercel Runtime log’larında boto3/403/404 mesajına bakın."
        )
    root = _mockups_root()
    if not root.is_dir():
        return f"Mockups klasörü yok: {root} — yolu kontrol edin."
    return (
        f"Katalog boş. {root} altında kategori adlarında alt klasörlere .png / .jpg / .webp template "
        "görselleri koyun."
    )


def _is_mockup_media_url(u: str) -> bool:
    s = str(u or "").strip()
    return s.startswith("/media/mockups/") or s.startswith(
        "/media/workspace-mockups/"
    ) or s.startswith("/media/workspace-r2/")


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


def _r2_workspace_output_prefix() -> str:
    raw = (os.environ.get("S3_WORKSPACE_OUTPUT_PREFIX") or "workspace-generated").strip().strip(
        "/"
    )
    return (raw + "/") if raw else "workspace-generated/"


def _r2_workspace_batch_s3_prefix(batch_id: str) -> str:
    bid = (batch_id or "").strip()
    return f"{_r2_workspace_output_prefix()}{bid}/"


def _r2_workspace_design_s3_key(batch_id: str, role: str, suffix: str) -> str:
    bid = (batch_id or "").strip()
    rr = (role or "").strip().lower() or "design"
    suf = (suffix or "").strip().lower()
    if not suf.startswith("."):
        suf = ".webp"
    return f"{_r2_workspace_output_prefix()}{bid}/_designs/{rr}{suf}"


def _r2_upload_workspace_design(batch_id: str, role: str, design_path: Path) -> str:
    if not _r2_enabled():
        return ""
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket or not design_path.is_file():
        return ""
    key = _r2_workspace_design_s3_key(batch_id, role, design_path.suffix)
    try:
        _r2_client().upload_file(str(design_path), bucket, key)
        return key
    except Exception:
        logging.getLogger("uvicorn.error").exception(
            "R2 workspace design upload basarisiz bucket=%s key=%s", bucket, key
        )
        return ""


def _r2_download_workspace_design_to_tmp(
    key: str, *, batch_id: str, role: str, fallback_suffix: str = ".webp"
) -> Optional[Path]:
    if not _r2_enabled():
        return None
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    k = (key or "").strip()
    if not bucket or not k:
        return None
    ext = Path(k).suffix.lower() or fallback_suffix
    out = (WORKSPACE_DESIGNS_ROOT / f"{batch_id}_{role}_r2{ext}").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        _r2_client().download_file(bucket, k, str(out))
        if out.is_file():
            return out
    except Exception:
        logging.getLogger("uvicorn.error").exception(
            "R2 workspace design download basarisiz bucket=%s key=%s", bucket, k
        )
    return None


def _workspace_r2_mockup_url(batch_id: str, rel: str) -> str:
    bid = (batch_id or "").strip()
    rel_norm = (rel or "").strip().replace("\\", "/").lstrip("/")
    parts: list[str] = [bid] + [p for p in rel_norm.split("/") if p and p != "."]
    return "/media/workspace-r2/" + "/".join(quote(p, safe="") for p in parts)


def _r2_mirror_workspace_batch(batch_id: str, batch_dir: Path) -> None:
    """Vercel: /tmp batch farkli lambda'da olmayabilir; R2'ye kopyala."""
    if not _r2_enabled():
        return
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket or not batch_dir.is_dir():
        return
    prefix = _r2_workspace_batch_s3_prefix(batch_id)
    batch_resolved = batch_dir.resolve()
    tasks: list[tuple[str, str]] = []
    for p in sorted(batch_resolved.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            rel = p.resolve().relative_to(batch_resolved).as_posix()
        except ValueError:
            continue
        key = f"{prefix}{rel}".replace("\\", "/")
        tasks.append((str(p), key))

    def _one(t: tuple[str, str]) -> None:
        lp, ky = t
        try:
            _r2_client().upload_file(lp, bucket, ky)
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "R2 workspace mirror upload basarisiz bucket=%s key=%s", bucket, ky
            )

    if not tasks:
        return
    workers = min(10, max(1, len(tasks)))
    if workers == 1:
        for t in tasks:
            _one(t)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, t) for t in tasks]
        for f in as_completed(futures):
            f.result()


def _r2_remote_workspace_batch_has_objects(batch_id: str) -> bool:
    if not _r2_enabled():
        return False
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return False
    prefix = _r2_workspace_batch_s3_prefix(batch_id)
    try:
        resp = _r2_client().list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        return bool(resp.get("Contents"))
    except Exception:
        return False


def _r2_list_workspace_batch_urls(batch_id: str) -> list[str]:
    if not _r2_enabled():
        return []
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return []
    prefix = _r2_workspace_batch_s3_prefix(batch_id)
    client = _r2_client()
    urls: list[str] = []
    token: Optional[str] = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in (resp.get("Contents") or []):
            k = str(obj.get("Key") or "")
            if not k or k.endswith("/"):
                continue
            ext = Path(k).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            if not k.startswith(prefix):
                continue
            rel = k[len(prefix) :].lstrip("/")
            if not rel or ".." in rel.split("/"):
                continue
            urls.append(_workspace_r2_mockup_url(batch_id, rel))
        if not resp.get("IsTruncated"):
            break
        token = str(resp.get("NextContinuationToken") or "")
        if not token:
            break
    return sorted(urls, key=lambda s: s.lower())


def _workspace_urls_for_batch(batch: Path) -> list[str]:
    batch_id = batch.name
    if _r2_remote_workspace_batch_has_objects(batch_id):
        return _r2_list_workspace_batch_urls(batch_id)
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


def _workspace_r2_key_from_media_url(url: str) -> Optional[str]:
    u = str(url or "").strip()
    api = "/media/workspace-r2/"
    if not u.startswith(api):
        return None
    tail = u[len(api) :]
    raw_parts = [p for p in tail.split("/") if p]
    if len(raw_parts) < 2:
        return None
    bid = unquote(raw_parts[0])
    rel_parts = [unquote(p) for p in raw_parts[1:]]
    if ".." in bid or "/" in bid:
        return None
    if any(".." in x for x in rel_parts):
        return None
    rel = "/".join(rel_parts)
    if not rel:
        return None
    return f"{_r2_workspace_batch_s3_prefix(bid)}{rel}"


def _workspace_r2_zip_arcname(url: str) -> str:
    u = str(url or "").strip()
    api = "/media/workspace-r2/"
    if not u.startswith(api):
        return "image.png"
    tail = u[len(api) :]
    parts = [unquote(p) for p in tail.split("/") if p]
    if len(parts) < 2:
        return "image.png"
    return "/".join(parts)


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


def _seed_placement_json_into_run_root(run_root: Path) -> None:
    """R2 modunda gecici mockup_root'a placement.json koy (yerlesim anahtarlari)."""
    chosen: Optional[Path] = None
    local_cfg = _mockups_root() / "placement.json"
    if local_cfg.is_file():
        chosen = local_cfg
    else:
        bundle_cfg = Path(__file__).resolve().parent / "Mockups" / "placement.json"
        if bundle_cfg.is_file():
            chosen = bundle_cfg
    if chosen is None:
        return
    try:
        (run_root / "placement.json").write_bytes(chosen.read_bytes())
    except OSError:
        logging.getLogger("uvicorn.error").exception(
            "placement.json kopyalanamadi: %s -> %s", chosen, run_root
        )


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
        try:
            client.download_file(bucket, key, str(target))
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "R2 download_file basarisiz bucket=%s key=%s rel=%s", bucket, key, rel
            )
            raise
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


def _default_seo_fields() -> dict[str, Any]:
    return {
        "seo_title": "",
        "seo_tags": [],
        "primary_color": "",
        "secondary_color": "",
        "occasion": "",
        "holiday": "",
        "graphic": "",
    }


def _fallback_seo_fields() -> dict[str, Any]:
    return {
        "seo_title": "tshirt design",
        "seo_tags": ["tshirt", "graphic", "design"],
        "primary_color": "unknown",
        "secondary_color": "unknown",
        "occasion": "unknown",
        "holiday": "unknown",
        "graphic": "unknown",
    }


def _normalize_seo_tags(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = _dedupe_preserve_order([str(x) for x in values if isinstance(x, str)])
    return cleaned[:_ETSY_TAG_MAX_COUNT]


def _normalize_seo_title(value: Any) -> str:
    return str(value or "").strip()[:140]


def _expand_seo_title_if_too_short(
    title: str,
    *,
    tags: list[str],
    primary_color: str,
    secondary_color: str,
    occasion: str,
    holiday: str,
    graphic: str,
) -> str:
    base = _normalize_seo_title(title)
    if len(base) >= 65:
        return base
    parts: list[str] = [base] if base else []
    for extra in [graphic, occasion, holiday, primary_color, secondary_color]:
        x = str(extra or "").strip()
        if x and x.lower() not in " ".join(parts).lower():
            parts.append(x)
    for t in tags[:6]:
        tt = str(t or "").strip()
        if tt and tt.lower() not in " ".join(parts).lower():
            parts.append(tt)
        if len(" ".join(parts)) >= 80:
            break
    out = _normalize_seo_title(" ".join(parts))
    return out or base


def _prepare_design_image_for_ai(design_path: Path) -> tuple[bytes, str]:
    """
    GPT vision için resmi küçültüp WebP'e çevirir.
    Fallback: dönüştürme başarısızsa orijinal dosya byte'ları.
    """
    try:
        max_side_raw = (os.environ.get("OPENAI_DESIGN_MAX_SIDE") or "768").strip()
        max_side = max(512, min(768, int(max_side_raw)))
    except Exception:
        max_side = 768
    try:
        quality_raw = (os.environ.get("OPENAI_DESIGN_WEBP_QUALITY") or "70").strip()
        quality = max(40, min(90, int(quality_raw)))
    except Exception:
        quality = 70
    try:
        max_bytes_raw = (os.environ.get("OPENAI_DESIGN_MAX_BYTES") or "450000").strip()
        max_bytes = max(120_000, min(900_000, int(max_bytes_raw)))
    except Exception:
        max_bytes = 450_000
    try:
        with Image.open(design_path) as im:
            if im.mode not in {"RGB", "RGBA"}:
                im = im.convert("RGBA")
            cur = im
            while True:
                w, h = cur.size
                scale = min(1.0, float(max_side) / float(max(w, h, 1)))
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                if (nw, nh) != (w, h):
                    cur = cur.resize((nw, nh), Image.Resampling.LANCZOS)
                for q in [quality, max(40, quality - 10), 40]:
                    buf = io.BytesIO()
                    cur.save(buf, format="WEBP", quality=q, method=6)
                    data = buf.getvalue()
                    if data and len(data) <= max_bytes:
                        return data, "image/webp"
                # Hala büyükse bir kademe daha küçült
                if max(cur.size) <= 512:
                    break
                cur = cur.resize(
                    (max(1, int(cur.size[0] * 0.82)), max(1, int(cur.size[1] * 0.82))),
                    Image.Resampling.LANCZOS,
                )
    except Exception:
        pass
    fallback = design_path.read_bytes()
    if len(fallback) > max_bytes:
        raise RuntimeError(
            "Design resmi AI analiz için çok büyük. Daha küçük bir dosya yükleyin "
            "veya OPENAI_DESIGN_MAX_BYTES değerini artırın."
        )
    ext = design_path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return fallback, mime


def _remove_hallucinated_subject_terms(text: str, detected_subjects: list[str]) -> str:
    s = str(text or "")
    detected_tokens: set[str] = set()
    for item in detected_subjects:
        for t in re.findall(r"[a-z0-9]{3,}", str(item).lower()):
            detected_tokens.add(t)
    risky_terms = {
        "cat",
        "dog",
        "pet",
        "animal",
        "mickey",
        "disney",
        "frozen",
        "princess",
        "superhero",
        "mandalorian",
        "star wars",
    }
    for term in risky_terms:
        parts = [p for p in re.findall(r"[a-z0-9]{3,}", term.lower())]
        if parts and all(p not in detected_tokens for p in parts):
            s = re.sub(rf"\b{re.escape(term)}\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,;-")
    return s


def _analyze_design_for_seo(design_path: Path) -> dict[str, Any]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY bulunamadı.")
    if not design_path.is_file():
        raise RuntimeError(f"Design dosyası bulunamadı: {design_path}")
    model = (
        os.environ.get("OPENAI_VISION_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    ).strip()
    image_bytes, mime = _prepare_design_image_for_ai(design_path)
    image_url_override = (os.environ.get("OPENAI_IMAGE_URL") or "").strip()
    if image_url_override:
        image_ref = image_url_override
    else:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        image_ref = f"data:{mime};base64,{image_b64}"
    prompt_text = (
        "You are an Etsy SEO generator for DTF printed physical t-shirts.\n"
        "CORE CONTEXT:\n"
        "- Product is always a physical t-shirt\n"
        "- Printing method is DTF (Direct to Film)\n"
        "- Not a digital product, not wall art, not poster, not printable\n"
        "TWO-STAGE SYSTEM:\n"
        "STEP 1 (image analysis): detect main subject, style, and theme from visible elements only.\n"
        "Do not guess. If unclear, use \"unknown\".\n"
        "STEP 2 (SEO generation): generate Etsy SEO specifically for DTF printed t-shirts.\n"
        "TITLE RULES:\n"
        "- length target 100-130 chars (hard max 140)\n"
        "- must include detected subject and \"t-shirt\" or \"tee\"\n"
        "- should include style, audience, and use case when relevant\n"
        "- must not include: printable, poster, wall art, digital download\n"
        "TAG RULES:\n"
        "- exactly 13 tags, each <=20 chars, unique\n"
        "- include t-shirt-related tags such as tshirt, graphic tee, dtf shirt, printed shirt when relevant\n"
        "- must not include: printable, wall art, poster, digital\n"
        "- tags must be grounded in subject/style/audience from image\n"
        "SUBJECT CONSISTENCY:\n"
        "- title and tags must match detected subject; never change topic\n"
        "HARD RULES:\n"
        "- never return null\n"
        "- seo_tags must always be an array of exactly 13 items\n"
        "- if output includes unrelated concepts return {\"error\":\"invalid_output\"}\n"
        "OUTPUT JSON ONLY (no explanation, no markdown):\n"
        "{\"seo_title\":\"\",\"seo_tags\":[],\"primary_color\":\"\",\"secondary_color\":\"\",\"occasion\":\"\",\"holiday\":\"\",\"graphic\":\"\"}"
    )
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "Return only strict JSON output.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_ref,
                            "detail": "low",
                        },
                    },
                ],
            },
        ],
    }
    try:
        with httpx.Client(timeout=90.0) as client:
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
        txt = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(txt) if isinstance(txt, str) else {}
    except Exception as exc:
        raise RuntimeError(f"GPT analiz hatası: {exc}")

    out = _default_seo_fields()
    if not isinstance(parsed, dict):
        return _fallback_seo_fields()
    if str(parsed.get("error") or "").strip().lower() == "invalid_output":
        return _fallback_seo_fields()
    if parsed.get("seo_tags") is None:
        parsed["seo_tags"] = []
    raw_detected = parsed.get("detected_subjects")
    raw_detected_list = raw_detected if isinstance(raw_detected, list) else []
    detected_subjects = [
        str(x).strip()
        for x in raw_detected_list
        if isinstance(x, str) and str(x).strip()
    ]
    clean_title = _remove_hallucinated_subject_terms(
        str(parsed.get("seo_title") or ""),
        detected_subjects,
    )
    out["seo_title"] = _normalize_seo_title(clean_title)
    raw_tags = _normalize_seo_tags(parsed.get("seo_tags") or [])
    cleaned_tags = [
        _remove_hallucinated_subject_terms(tag, detected_subjects) for tag in raw_tags
    ]
    out["seo_tags"] = _normalize_seo_tags(cleaned_tags)
    if len(out["seo_tags"]) < _ETSY_TAG_MAX_COUNT:
        filler_seed = [
            out["seo_title"],
            str(parsed.get("graphic") or ""),
            str(parsed.get("occasion") or ""),
            str(parsed.get("holiday") or ""),
            str(parsed.get("primary_color") or ""),
            str(parsed.get("secondary_color") or ""),
            "shirt design",
            "gift idea",
            "trending design",
        ]
        fill_tags = _fallback_etsy_tags([x for x in filler_seed if x], out["seo_title"])
        for tag in fill_tags:
            if tag not in out["seo_tags"]:
                out["seo_tags"].append(tag)
            if len(out["seo_tags"]) >= _ETSY_TAG_MAX_COUNT:
                break
    out["primary_color"] = str(parsed.get("primary_color") or "").strip()
    out["secondary_color"] = str(parsed.get("secondary_color") or "").strip()
    out["occasion"] = str(parsed.get("occasion") or "").strip()
    out["holiday"] = str(parsed.get("holiday") or "").strip()
    out["graphic"] = str(parsed.get("graphic") or "").strip()
    out["seo_title"] = _expand_seo_title_if_too_short(
        out["seo_title"],
        tags=out["seo_tags"],
        primary_color=out["primary_color"],
        secondary_color=out["secondary_color"],
        occasion=out["occasion"],
        holiday=out["holiday"],
        graphic=out["graphic"],
    )
    out["seo_tags"] = _normalize_seo_tags(out.get("seo_tags") or [])
    return out


def _etsy_placeholder_title() -> str:
    return (os.environ.get("ETSY_DRAFT_PLACEHOLDER_TITLE") or "Draft — add listing details in Etsy")[:140]


def _etsy_placeholder_description(source_url: str = "") -> str:
    base = (os.environ.get("ETSY_DRAFT_PLACEHOLDER_DESCRIPTION") or "").strip()
    if not base:
        base = (
            "Photos from selected mockups and uploaded designs. "
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
    if "seo_title" not in draft or not isinstance(draft.get("seo_title"), str):
        draft["seo_title"] = ""
    draft["seo_title"] = _normalize_seo_title(draft.get("seo_title"))
    draft["seo_tags"] = _normalize_seo_tags(draft.get("seo_tags"))
    for field in ("primary_color", "secondary_color", "occasion", "holiday", "graphic"):
        if field not in draft or not isinstance(draft.get(field), str):
            draft[field] = ""
        draft[field] = str(draft.get(field) or "").strip()
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
    black_design_path = str(wa.get("black_design_path") or "").strip()
    white_design_path = str(wa.get("white_design_path") or "").strip()
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
            "black_design_path": black_design_path,
            "white_design_path": white_design_path,
            "black_design_r2_key": str(wa.get("black_design_r2_key") or "").strip(),
            "white_design_r2_key": str(wa.get("white_design_r2_key") or "").strip(),
            "batch_id": str(wa.get("batch_id") or "").strip(),
        },
        "variations": draft.get("variations") if isinstance(draft.get("variations"), list) else [],
        "keywords": draft.get("keywords") if isinstance(draft.get("keywords"), list) else [],
        "tags": draft.get("tags") if isinstance(draft.get("tags"), list) else [],
        "seo_title": _normalize_seo_title(draft.get("seo_title")),
        "seo_tags": draft.get("seo_tags") if isinstance(draft.get("seo_tags"), list) else [],
        "primary_color": str(draft.get("primary_color") or "").strip(),
        "secondary_color": str(draft.get("secondary_color") or "").strip(),
        "occasion": str(draft.get("occasion") or "").strip(),
        "holiday": str(draft.get("holiday") or "").strip(),
        "graphic": str(draft.get("graphic") or "").strip(),
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
    generated_urls: list[str] = []
    current_batch = ""
    if isinstance(workspace_draft, dict):
        wa = workspace_draft.get("workspace_assets")
        if isinstance(wa, dict):
            raw_urls = wa.get("mockup_images")
            if isinstance(raw_urls, list):
                generated_urls = [
                    str(x).strip() for x in raw_urls if isinstance(x, str) and str(x).strip()
                ]
    if not generated_urls:
        latest = _latest_workspace_batch_dir()
        if latest:
            current_batch = latest.name
            generated_urls = _workspace_urls_for_batch(latest)
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
            "r2_active": _r2_enabled(),
            "vercel_runtime": _on_vercel(),
            "s3_prefix_label": _s3_prefix_label(),
            "mockup_catalog_image_count": _mockup_catalog_image_count(mockup_categories),
            "mockups_root_path": str(_mockups_root()),
            "mockups_root_exists": _mockups_root().is_dir(),
            "generated_urls": generated_urls,
            "current_batch": current_batch,
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
            warning="Bu oturum bağlantısı geçersiz veya süresi doldu.",
        )
    return _render_index(request)


@app.get("/media/mockups/{path:path}")
def mockup_media(path: str) -> FileResponse:
    if _r2_enabled():
        rel = unquote((path or "").strip().replace("\\", "/")).lstrip("/")
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
            logging.getLogger("uvicorn.error").exception(
                "R2 get_object basarisiz bucket=%s key=%s rel=%s", bucket, key, rel
            )
            raise HTTPException(status_code=404, detail="Mockup bulunamadı")
        ct = media.get(ext, "application/octet-stream")
        return Response(
            content=data,
            media_type=ct,
            headers={"Cache-Control": _MOCKUP_IMAGE_CACHE_CONTROL},
        )
    rel_local = unquote((path or "").strip().replace("\\", "/")).lstrip("/")
    p = _safe_mockup_file_path(rel_local)
    if not p:
        raise HTTPException(status_code=404, detail="Mockup bulunamadı")
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    ct = media.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(
        p,
        media_type=ct,
        filename=p.name,
        headers={"Cache-Control": _MOCKUP_IMAGE_CACHE_CONTROL},
    )


@app.get("/media/workspace-mockups/{path:path}")
def workspace_mockup_media(path: str) -> FileResponse:
    rel = unquote((path or "").strip().replace("\\", "/")).lstrip("/")
    p = _safe_workspace_mockup_file_path(rel)
    if not p:
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    ct = media.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(
        p,
        media_type=ct,
        filename=p.name,
        headers={"Cache-Control": _MOCKUP_IMAGE_CACHE_CONTROL},
    )


@app.get("/media/workspace-r2/{batch_id}/{path:path}")
def workspace_r2_mockup_media(batch_id: str, path: str) -> Response:
    """Vercel: üretilen batch R2'de; önizleme bu rota üzerinden."""
    if not _r2_enabled():
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    bid = (batch_id or "").strip()
    if not bid or ".." in bid or "/" in bid:
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    rel = unquote((path or "").strip().replace("\\", "/")).lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    key = f"{_r2_workspace_batch_s3_prefix(bid)}{rel}"
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    ext = Path(rel).suffix.lower()
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    try:
        resp = _r2_client().get_object(Bucket=bucket, Key=key)
        data = resp["Body"].read()
    except Exception:
        logging.getLogger("uvicorn.error").exception(
            "R2 workspace-r2 get_object bucket=%s key=%s", bucket, key
        )
        raise HTTPException(status_code=404, detail="Workspace mockup bulunamadı")
    ct = media.get(ext, "application/octet-stream")
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": _MOCKUP_IMAGE_CACHE_CONTROL},
    )


@app.post("/workspace/set-mockups-dir", response_class=HTMLResponse)
def workspace_set_mockups_dir(mockups_dir: str = Form("")) -> HTMLResponse:
    cleaned = (mockups_dir or "").strip()
    if not cleaned:
        os.environ.pop("MOCKUPS_DIR", None)
        return _redirect_workspace_state({"status": "Mockups klasörü varsayılana alındı."})
    resolved = Path(cleaned).expanduser().resolve()
    os.environ["MOCKUPS_DIR"] = str(resolved)
    if not resolved.is_dir():
        msg = quote(f"Mockups klasoru bulunamadi: {resolved}")
        return _redirect_workspace_state({"warning": f"Mockups klasörü bulunamadı: {resolved}"})
    status_msg = quote(f"Mockups klasoru guncellendi: {resolved}")
    if _on_vercel() and not _r2_enabled():
        w = quote(
            "Vercel: disk üzerinde büyük mockup görselleri yok; kütüphane için S3_*(R2) gerekir. "
            "Bu sadece placement.json yoludur, şablon PNG’lerini bekleme."
        )
        return _redirect_workspace_state(
            {
                "status": f"Mockups klasörü güncellendi: {resolved}",
                "warning": (
                    "Vercel: disk üzerinde büyük mockup görselleri yok; kütüphane için S3_*(R2) gerekir. "
                    "Bu sadece placement.json yoludur, şablon PNG’lerini bekleme."
                ),
            }
        )
    return _redirect_workspace_state({"status": f"Mockups klasörü güncellendi: {resolved}"})


@app.post("/workspace/download-selected-mockups")
def workspace_download_selected_mockups(selected_urls: str = Form("[]")) -> Response:
    try:
        raw = json.loads(selected_urls or "[]")
    except Exception:
        raw = []
    if not isinstance(raw, list):
        raw = []
    files: list[Path] = []
    r2_pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        p = _workspace_path_from_media_url(item)
        if p:
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            files.append(p)
            continue
        rk = _workspace_r2_key_from_media_url(item)
        if rk and rk not in seen:
            seen.add(rk)
            r2_pairs.append((rk, _workspace_r2_zip_arcname(item)))
    if not files and not r2_pairs:
        raise HTTPException(status_code=400, detail="Indirilecek secili mockup bulunamadi.")
    buf = io.BytesIO()
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    client = _r2_client() if r2_pairs else None
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            try:
                rel = p.resolve().relative_to(WORKSPACE_MOCKUPS_ROOT.resolve()).as_posix()
            except ValueError:
                rel = p.name
            zf.write(p, arcname=rel)
        if client and bucket:
            for s3_key, arc in r2_pairs:
                try:
                    body = client.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
                    zf.writestr(arc, body)
                except Exception:
                    logging.getLogger("uvicorn.error").exception(
                        "Zip R2 okuma basarisiz key=%s", s3_key
                    )
    headers = {
        "Content-Disposition": 'attachment; filename="selected_mockups.zip"',
        "Cache-Control": "no-store",
    }
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


@app.post("/workspace/analyze-design", response_class=HTMLResponse)
async def workspace_analyze_design(
    request: Request,
    draft_json: str = Form("{}"),
    design_black_file: Optional[UploadFile] = File(None),
) -> HTMLResponse:
    temp_uploaded_path: Optional[Path] = None
    try:
        draft = json.loads(draft_json or "{}")
        if not isinstance(draft, dict):
            draft = {}
        draft = _normalize_images_only_draft(draft)
        wa = draft.get("workspace_assets")
        assets = wa if isinstance(wa, dict) else {}
        black_path: Optional[Path] = None

        # Ephemeral analiz: kullanıcı black dosyayı yeni yüklediyse direkt onu kullan.
        if design_black_file is not None and (design_black_file.filename or "").strip():
            tmp_batch = f"analyze_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            temp_uploaded_path = await _save_uploaded_design_file(
                design_black_file, batch_id=tmp_batch, role="black"
            )
            black_path = temp_uploaded_path

        # Yükleme yoksa mevcut draft yolunu dene (geriye uyumluluk).
        if black_path is None:
            black_path_raw = str(assets.get("black_design_path") or "").strip()
            if black_path_raw:
                candidate = Path(black_path_raw).expanduser().resolve()
                if candidate.is_file():
                    black_path = candidate

        # Vercel fallback: eski akıştan gelen R2 key varsa indirip kullan.
        if black_path is None and _on_vercel() and _r2_enabled():
            black_path_raw = str(assets.get("black_design_path") or "").strip()
            batch_id = str(assets.get("batch_id") or "").strip()
            if not batch_id and black_path_raw:
                stem = Path(black_path_raw).stem
                if stem.endswith("_black"):
                    batch_id = stem[: -len("_black")]
            r2_key = str(assets.get("black_design_r2_key") or "").strip()
            if r2_key and batch_id:
                dl = _r2_download_workspace_design_to_tmp(
                    r2_key, batch_id=batch_id, role="black", fallback_suffix=".webp"
                )
                if dl and dl.is_file():
                    black_path = dl
                    assets["black_design_path"] = str(dl)
                    draft["workspace_assets"] = assets

        if black_path is None or not black_path.is_file():
            raise RuntimeError("Black design dosyası bulunamadı. Dosyayı yükleyip tekrar deneyin.")
        seo = _analyze_design_for_seo(black_path)
        draft["seo_title"] = str(seo.get("seo_title") or "").strip()[:140]
        draft["seo_tags"] = _normalize_seo_tags(seo.get("seo_tags"))
        draft["primary_color"] = str(seo.get("primary_color") or "").strip()
        draft["secondary_color"] = str(seo.get("secondary_color") or "").strip()
        draft["occasion"] = str(seo.get("occasion") or "").strip()
        draft["holiday"] = str(seo.get("holiday") or "").strip()
        draft["graphic"] = str(seo.get("graphic") or "").strip()
        if draft["seo_title"]:
            draft["title"] = draft["seo_title"]
        if draft["seo_tags"]:
            draft["tags"] = draft["seo_tags"][:_ETSY_TAG_MAX_COUNT]
        draft = _normalize_images_only_draft(draft)
        return _redirect_workspace_state(
            {
                "status": "SEO analizi tamamlandı.",
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
    finally:
        # Analiz için o anda yüklenen dosyayı kalıcı tutma.
        if temp_uploaded_path is not None:
            try:
                if temp_uploaded_path.exists():
                    temp_uploaded_path.unlink()
            except Exception:
                pass


@app.post("/workspace/publish", response_class=HTMLResponse)
def workspace_publish(
    request: Request,
    draft_json: str = Form(...),
    etsy_update_listing_id: str = Form(""),
    selected_mockup_urls: str = Form("[]"),
) -> HTMLResponse:
    try:
        draft = json.loads(draft_json)
        if not isinstance(draft, dict):
            raise RuntimeError("Geçersiz taslak verisi.")
        draft = _normalize_images_only_draft(draft)
        src = draft.get("source") if isinstance(draft.get("source"), dict) else {}
        source_url = str(src.get("url") or "")
        seo_title = str(draft.get("seo_title") or "").strip()
        draft_title = seo_title or str(draft.get("title") or "").strip()
        title = draft_title[:140] if draft_title else _etsy_placeholder_title()
        desc_parts = [_etsy_placeholder_description(source_url)]
        for label, key in (
            ("Primary Color", "primary_color"),
            ("Secondary Color", "secondary_color"),
            ("Occasion", "occasion"),
            ("Holiday", "holiday"),
            ("Graphic", "graphic"),
        ):
            val = str(draft.get(key) or "").strip()
            if val:
                desc_parts.append(f"{label}: {val}")
        desc = "\n".join([x for x in desc_parts if x]).strip()[:49990]
        price = _etsy_placeholder_price()

        selected_images: list[str] = []
        try:
            parsed_selected = json.loads(selected_mockup_urls or "[]")
            if isinstance(parsed_selected, list):
                selected_images = [
                    str(x).strip()
                    for x in parsed_selected
                    if isinstance(x, str) and str(x).strip() and _is_mockup_media_url(str(x).strip())
                ]
        except Exception:
            selected_images = []
        if selected_images:
            images = []
            seen_img: set[str] = set()
            for u in selected_images:
                if u in seen_img:
                    continue
                seen_img.add(u)
                images.append(u)
                if len(images) >= 20:
                    break
        else:
            raw_imgs = draft.get("images") or []
            images = [x for x in raw_imgs if isinstance(x, str) and x.strip()][:20]
        if not images:
            raise RuntimeError("Etsy’ye göndermek için en az bir görsel URL’si gerekir.")
        raw_tags = draft.get("seo_tags")
        if not isinstance(raw_tags, list):
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
    design_white_file: Optional[UploadFile] = File(None),
    design_black_file: Optional[UploadFile] = File(None),
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
        d_assets = draft.get("workspace_assets")
        assets = d_assets if isinstance(d_assets, dict) else {}

        def _has_upload(u: Optional[UploadFile]) -> bool:
            return bool(u is not None and (u.filename or "").strip())

        # Etsy/Mockup mantığı:
        # - Açık (light) template'lere siyah design basılır -> dark_design_path
        # - Koyu (dark) template'lere beyaz design basılır -> light_design_path
        white_uploaded = _has_upload(design_white_file)
        black_uploaded = _has_upload(design_black_file)

        if white_uploaded:
            white_design_path = await _save_uploaded_design_file(
                design_white_file, batch_id=batch_id, role="white"
            )
        else:
            white_design_path = None
            prev_white = str(assets.get("white_design_path") or "").strip()
            if prev_white:
                cand = Path(prev_white).expanduser().resolve()
                if cand.is_file():
                    white_design_path = cand
            if white_design_path is None and _on_vercel() and _r2_enabled():
                prev_white_key = str(assets.get("white_design_r2_key") or "").strip()
                if prev_white_key:
                    white_design_path = _r2_download_workspace_design_to_tmp(
                        prev_white_key, batch_id=batch_id, role="white", fallback_suffix=".webp"
                    )
            if white_design_path is None or not white_design_path.is_file():
                raise RuntimeError("White design dosyası bulunamadı. Yükleyip tekrar deneyin.")

        if black_uploaded:
            black_design_path = await _save_uploaded_design_file(
                design_black_file, batch_id=batch_id, role="black"
            )
        else:
            black_design_path = None
            prev_black = str(assets.get("black_design_path") or "").strip()
            if prev_black:
                cand = Path(prev_black).expanduser().resolve()
                if cand.is_file():
                    black_design_path = cand
            if black_design_path is None and _on_vercel() and _r2_enabled():
                prev_black_key = str(assets.get("black_design_r2_key") or "").strip()
                if prev_black_key:
                    black_design_path = _r2_download_workspace_design_to_tmp(
                        prev_black_key, batch_id=batch_id, role="black", fallback_suffix=".webp"
                    )
            if black_design_path is None or not black_design_path.is_file():
                raise RuntimeError("Black design dosyası bulunamadı. Yükleyip tekrar deneyin.")

        out_root = (WORKSPACE_MOCKUPS_ROOT / batch_id).resolve()
        run_root = root
        if r2_mode:
            run_root = (WORKSPACE_DESIGNS_ROOT / f"{batch_id}_mockups_src").resolve()
            run_root.mkdir(parents=True, exist_ok=True)
            _seed_placement_json_into_run_root(run_root)
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
        if _on_vercel() and _r2_enabled():
            try:
                _r2_mirror_workspace_batch(batch_id, out_root)
            except Exception:
                logging.getLogger("uvicorn.error").exception(
                    "R2 workspace batch mirror basarisiz batch_id=%s", batch_id
                )
        urls: list[str] = []
        if _on_vercel() and _r2_enabled() and _r2_remote_workspace_batch_has_objects(batch_id):
            urls = _r2_list_workspace_batch_urls(batch_id)
        if not urls:
            for p in out_paths:
                try:
                    rel = p.resolve().relative_to(WORKSPACE_MOCKUPS_ROOT.resolve()).as_posix()
                except ValueError:
                    continue
                urls.append(_workspace_mockup_rel_url(rel))
        if not urls:
            raise RuntimeError("Mockup görselleri URL'e dönüştürülemedi.")

        assets["mockup_images"] = urls
        assets["active_source"] = "mockups"
        assets["batch_id"] = batch_id
        assets["black_design_path"] = str(black_design_path)
        assets["white_design_path"] = str(white_design_path)
        if _on_vercel() and _r2_enabled():
            bkey = (
                _r2_upload_workspace_design(batch_id, "black", black_design_path)
                if black_uploaded
                else str(assets.get("black_design_r2_key") or "").strip()
            )
            wkey = (
                _r2_upload_workspace_design(batch_id, "white", white_design_path)
                if white_uploaded
                else str(assets.get("white_design_r2_key") or "").strip()
            )
            if bkey:
                assets["black_design_r2_key"] = bkey
            if wkey:
                assets["white_design_r2_key"] = wkey
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
