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
    upload_listing_image_from_bytes,
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


def _cleanup_old_workspace_files(max_age_hours: int = 24) -> None:
    """Delete workspace design files and generated mockup folders older than max_age_hours."""
    _log = logging.getLogger("uvicorn.error")
    cutoff = datetime.now().timestamp() - max_age_hours * 3600
    deleted_files = 0
    deleted_dirs = 0

    # Clean old design files in _workspace_designs/
    if WORKSPACE_DESIGNS_ROOT.is_dir():
        for f in WORKSPACE_DESIGNS_ROOT.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    if f.is_file():
                        f.unlink()
                        deleted_files += 1
                    elif f.is_dir():
                        import shutil
                        shutil.rmtree(f, ignore_errors=True)
                        deleted_dirs += 1
            except Exception:
                pass

    # Clean old batch folders in mockups_generated/_workspace/
    if WORKSPACE_MOCKUPS_ROOT.is_dir():
        for d in WORKSPACE_MOCKUPS_ROOT.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                    deleted_dirs += 1
            except Exception:
                pass

    if deleted_files or deleted_dirs:
        _log.info(
            "[cleanup] %d eski dosya ve %d eski klasör silindi (>%dh).",
            deleted_files, deleted_dirs, max_age_hours,
        )


@app.on_event("startup")
def _on_startup() -> None:
    _cleanup_old_workspace_files(max_age_hours=24)


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
        s = s[:_ETSY_TAG_MAX_LEN]
        # Always trim back to the last complete word boundary to avoid partial words.
        last_space = s.rfind(" ")
        if last_space > 0:
            s = s[:last_space]
        s = s.rstrip(" -&'")
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
        "temperature": 0.3,
        "max_completion_tokens": 350,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an Etsy SEO specialist for physical t-shirts. "
                    "Generate exactly 13 Etsy search tags for the given listing. "
                    "Rules: each tag lowercase, max 20 chars, unique, multi-word phrases preferred. "
                    "Mix product type (graphic tee, tshirt), subject (e.g. skull shirt), "
                    "audience (gift for him, womens tee), style (vintage tshirt, retro tee), "
                    "and use-case (birthday gift, funny gift) tags. "
                    "Never use: digital, printable, wall art, poster. "
                    "Return strict JSON: {\"tags\": [...]}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "listing_title": title or "",
                        "keywords": keywords,
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
    fallback = _fallback_etsy_tags(keywords, title)
    return fallback[:_ETSY_TAG_MAX_COUNT], "fallback"


# Product-type tags — one is picked at random and appended after GPT generation.
_PRODUCT_TYPE_TAGS: dict[str, list[str]] = {
    "t-shirt":    ["unisex tshirt", "everyday tshirt"],
    "cc t-shirt": ["comfort colors tshirt", "comfort colors gift"],
    "cc long":    ["comfort colors long sleeve", "long sleeve gift"],
    "sweatshirt": ["crewneck sweatshirt", "sweatshirt gift"],
    "kids":       ["kids tshirt", "youth shirt", "gift for kids"],
}

# Title product-type word per mockup category (used in GPT prompt).
_PRODUCT_TYPE_TITLE_WORD: dict[str, str] = {
    "t-shirt":    "T-Shirt",
    "cc t-shirt": "CC Shirt",
    "cc long":    "CC Long Sleeve Shirt",
    "sweatshirt": "Sweatshirt",
    "kids":       "Kids Shirt",
}

_ETSY_TAG_TOTAL = 13


def _get_product_type_tags(mockup_type: str) -> list[str]:
    """Return 1 randomly selected product-type tag for the given mockup category."""
    import random
    key = (mockup_type or "").strip().lower()
    tags = _PRODUCT_TYPE_TAGS.get(key)
    return [random.choice(tags)] if tags else []


def _get_product_type_title_word(mockup_type: str) -> str:
    """Return the title product-type word for the given mockup category."""
    key = (mockup_type or "").strip().lower()
    return _PRODUCT_TYPE_TITLE_WORD.get(key, "")


def _detect_mockup_type_from_urls(urls: list[str]) -> str:
    """Infer mockup category from the folder name in selected template URLs.
    E.g. '/media/mockups/CC%20Long/1.png' → 'CC Long'
    """
    from urllib.parse import unquote
    known_keys = set(_PRODUCT_TYPE_TAGS.keys())  # lowercased
    for url in urls:
        parts = unquote(url).replace("\\", "/").split("/")
        # Walk path parts to find a known category folder
        for part in parts:
            if part.strip().lower() in known_keys:
                # Return with original casing from _PRODUCT_TYPE_TAGS
                for key in _PRODUCT_TYPE_TAGS:
                    if key == part.strip().lower():
                        # Return the display name (capitalize properly)
                        return part.strip()
    return ""


_FORBIDDEN_TAG_TERMS: set[str] = {
    "dtf", "dtf shirt", "dtf print", "dtf transfer", "dtf tee",
    "unisex", "unisex tee", "unisex shirt", "unisex t shirt",
    "print on demand", "printable", "digital", "wall art", "poster",
    "sublimation", "heat transfer",
    "graphic t shirt", "printed tee", "printed shirt",
    "cute tee", "cute shirt",
    "everyday wear", "funny gift", "fun gift", "cool gift", "awesome tee",
    "pastel aesthetic", "novelty tee", "novelty shirt", "bear graphic tee",
    "humor aesthetic", "statement tshirt", "party joke shirt",
    "bold graphic shirt", "bold graphic tee", "bold graphic",
    "bold graphic top", "bold top",
    "racing top", "graphic top", "fan top", "sport top", "sports top",
    "athletic top", "active top", "gym top", "training top", "workout top",
    "printed top", "cool top", "funny top",
    # Single-word filler tags that add no search value
    "everyday", "daily", "casual", "lifestyle", "vibes", "outdoorsy",
}

# Regex-based forbidden patterns (for rules not expressible as simple terms).
_FORBIDDEN_TAG_REGEXES: list[re.Pattern[str]] = [
    # Any tag ending with "aesthetic" when preceded by at least one word — "aesthetic" alone is allowed.
    re.compile(r"[a-z].+\baesthetic$"),
    # Any tag ending with " top" when preceded by a generic descriptor word.
    re.compile(r"\b(graphic|fan|racing|sport|sports|athletic|active|gym|training|workout|printed|cool|funny|bold|novelty|cute|casual|street|urban|retro|vintage|trendy|summer|festival)\s+top$"),
]


# Pre-compiled patterns for faster repeated matching.
_FORBIDDEN_TAG_PATTERNS: list[re.Pattern[str]] = []


def _build_forbidden_patterns() -> None:
    global _FORBIDDEN_TAG_PATTERNS
    _FORBIDDEN_TAG_PATTERNS = [
        re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
        for term in _FORBIDDEN_TAG_TERMS
    ]


_build_forbidden_patterns()


def _tag_contains_forbidden(tag: str) -> bool:
    tl = tag.strip().lower()
    return (
        any(p.search(tl) for p in _FORBIDDEN_TAG_PATTERNS)
        or any(p.search(tl) for p in _FORBIDDEN_TAG_REGEXES)
    )


def _filter_forbidden_tags(tags: list[str]) -> list[str]:
    return [t for t in tags if not _tag_contains_forbidden(t)]


def _filter_dangling_tags(tags: list[str]) -> list[str]:
    """Remove tags that end with a dangling/preposition word — they were cut off mid-phrase."""
    result = []
    for tag in tags:
        words = re.findall(r"[a-z']+", tag.lower())
        last = words[-1] if words else ""
        if last in _DANGLING_TAG_ENDINGS:
            continue  # drop — e.g. "roaring bear with", "black crow holding"
        result.append(tag)
    return result


def _default_seo_fields() -> dict[str, Any]:
    return {
        "seo_title": "",
        "seo_tags": [],
        "primary_color": "",
        "secondary_color": "",
        "occasion": "",
        "holiday": "",
        "graphic": "",
        "style_detected": "",
        "reasoning": "",
        "attempt_count": 1,
        "selfcheck_issues": [],
        "text_detected": "none",
        "text_content": "",
        "needs_text_input": False,
    }


def _fallback_seo_fields() -> dict[str, Any]:
    return {
        "seo_title": "tshirt design",
        "seo_tags": ["tshirt", "graphic tee", "shirt design"],
        "primary_color": "unknown",
        "secondary_color": "unknown",
        "occasion": "unknown",
        "holiday": "unknown",
        "graphic": "unknown",
        "style_detected": "unknown",
        "reasoning": "",
        "attempt_count": 1,
        "selfcheck_issues": [],
        "text_detected": "none",
        "text_content": "",
        "needs_text_input": False,
    }


def _normalize_seo_tags(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = _dedupe_preserve_order([str(x) for x in values if isinstance(x, str)])
    return cleaned[:_ETSY_TAG_MAX_COUNT]


def _fix_title_caps_for_etsy(title: str) -> str:
    """Etsy rejects titles where >3 words start with 2+ sequential capital letters.

    Strategy: convert any fully-uppercase word with 3+ chars to Title Case.
    2-char abbreviations (CC, LA, NY) are kept as-is.
    If after that conversion we still have >3 caps-starting words, convert the
    2-char ones to Title Case as well.
    """
    def starts_with_two_caps(w: str) -> bool:
        stripped = re.sub(r"[^A-Za-z]", "", w)
        return len(stripped) >= 2 and stripped[:2].isupper()

    words = title.split(" ")

    # First pass: convert 3+ char ALL-CAPS words to Title Case
    fixed: list[str] = []
    for w in words:
        alpha = re.sub(r"[^A-Za-z]", "", w)
        if len(alpha) >= 3 and alpha.isupper():
            # Capitalise only the alpha part, preserve surrounding punctuation
            fixed.append(w[0].upper() + w[1:].lower() if w else w)
        else:
            fixed.append(w)

    # Second pass: if still >3 caps words, also title-case 2-char ones
    if sum(1 for w in fixed if starts_with_two_caps(w)) > 3:
        result: list[str] = []
        for w in fixed:
            alpha = re.sub(r"[^A-Za-z]", "", w)
            if len(alpha) == 2 and alpha.isupper():
                result.append(w[0].upper() + w[1:].lower() if w else w)
            else:
                result.append(w)
        return " ".join(result)

    return " ".join(fixed)


def _normalize_seo_title(value: Any) -> str:
    title = str(value or "").strip()[:140]
    return _fix_title_caps_for_etsy(title)


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
    segments: list[str] = [base] if base else []
    joined_lower = base.lower()
    for extra in [graphic, occasion, holiday]:
        x = str(extra or "").strip().title()
        if x and x.lower() not in joined_lower:
            segments.append(x)
            joined_lower = " ".join(segments).lower()
    multi_word_tags = [t for t in tags[:8] if " " in t]
    for t in multi_word_tags:
        tt = str(t or "").strip().title()
        if tt and tt.lower() not in joined_lower:
            segments.append(tt)
            joined_lower = " ".join(segments).lower()
        if len(" | ".join(segments)) >= 80:
            break
    out = _normalize_seo_title(" | ".join(segments))
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


def _build_seo_prompt(prior_issues: list[str], text_hint: str = "", mockup_type: str = "") -> str:
    forbidden_str = ", ".join(sorted(_FORBIDDEN_TAG_TERMS))
    text_hint_block = (
        f"USER-PROVIDED TEXT HINT: The user has manually identified the following text in the design: \"{text_hint.strip()}\". "
        "Use this text in your title and tags as appropriate (name, phrase, or keyword).\n\n"
        if text_hint.strip() else ""
    )
    base = (
        "You are an expert Etsy SEO copywriter for printed physical t-shirts.\n\n"
        "PRODUCT CONTEXT: Always a wearable physical t-shirt. Never digital, download, wall art, or poster.\n\n"
        + text_hint_block +

        "=== STEP 0: TEXT DETECTION ===\n"
        "Before anything else, carefully scan the entire design for any visible text, letters, numbers, words, or name-like elements.\n"
        "- CLEARLY READABLE: Extract the exact text as written.\n"
        "- PRESENT BUT HARD TO READ (rotated, mirrored, angled, stylized, overlapping): Attempt to decode it letter by letter. Provide your best guess and mark as 'partially_readable'.\n"
        "- NO TEXT VISIBLE: Mark as 'none'.\n"
        "This is critical — text often reveals a person's name, slogan, cultural reference, or sports number that must appear in the title and tags.\n"
        "Output in: \"text_detected\": \"none\"|\"readable\"|\"partially_readable\", \"text_content\": \"<extracted or best-guess text>\"\n\n"

        "=== STEP 1: IMAGE ANALYSIS ===\n"
        "Study the design carefully. Identify:\n"
        "- SUBJECT: the main graphic/character/object depicted (be specific: e.g. 'mushroom with stars' not just 'nature')\n"
        "- ART STYLE: choose the most accurate from this list or use your own if more fitting:\n"
        "  vintage, retro, cottagecore, storybook, engraving, woodcut, boho, minimalist, bold/graphic,\n"
        "  watercolor, whimsical, dark/gothic, cute/kawaii, funny/humor, geometric, floral, maximalist\n"
        "- AUDIENCE: who would wear this? (nurses, teachers, dog moms, hikers, etc.) — only if unmistakably clear from design elements\n"
        "- THEME/OCCASION: hobby, holiday, profession, lifestyle — only if clear\n"
        "- CULTURAL/ETHNIC IDENTITY: if the design clearly expresses a specific cultural or ethnic identity (e.g. Hispanic, Latino, Latina, Mexican, Irish, Italian, Black, African American, Asian, Korean, Japanese, Puerto Rican, etc.) through flags, symbols, text, or unmistakable cultural motifs — identify it explicitly. Do NOT infer ethnicity from skin color or ambiguous imagery.\n"
        "Never invent details not visible in the image. Use 'unknown' if unclear.\n\n"

        "=== STEP 2: SEO TITLE (100-140 chars, HARD MAX 140) ===\n"
        "Structure: [Subject/Niche] [Art Style] [T-Shirt/Tee] | [Long-tail secondary keyword] | [Use case] | [Gift phrase]\n\n"
        "TITLE RULES — follow strictly:\n"
        "1. FIRST WORD RULE: The title MUST open with the highest-search-volume keyword for the design's main subject or niche — never a style descriptor.\n"
        "   WRONG openers: Bold, Retro, Funny, Cute, Vintage, Aesthetic, Cool, Unique, Trendy, Quirky, Colorful, Stylish, Amazing\n"
        "   CORRECT openers: the actual subject or niche (e.g. 'Ratatouille', 'Mushroom', 'Skeleton', 'Nurse', 'Graduation', 'Stitch', 'Trump')\n"
        "   The art/style word may follow the subject: 'Mushroom Retro Tee' ✓ — 'Retro Mushroom Tee' ✗\n"
        "2. Prefer specific over generic: 'Mushroom Engraving Style Tee' beats 'Mushroom T-Shirt'\n"
        "3. Use ' | ' to separate 3-4 keyword phrases naturally\n"
        "4. NEVER add audience qualifiers ('for Women', 'for Men', 'for Him', 'for Her', 'for Girls', 'for Boys') unless the design EXPLICITLY depicts a gender-specific subject (e.g. clearly nurse uniform, mom/dad text, explicitly feminine character). A general animal, plant, skull, or abstract design does NOT justify audience language.\n"
        "5. End with a gift phrase when it fits ('Birthday Gift Idea', 'Nature Lover Gift', 'Mom Gift')\n"
        "6. Spell every word correctly — no missing letters, no abbreviations unless widely known\n"
        "7. Count characters precisely: target 110-135 chars, never exceed 140\n"
        "8. FORBIDDEN in title: DTF, unisex, printable, digital, download, wall art, poster, bold graphic, graphic tee, graphic shirt, novelty, everyday, casual, lifestyle\n"
        "9. CULTURAL IDENTITY RULE: If a cultural/ethnic identity was detected in Step 1, it MUST appear in the title. Example: 'Retro Skull Mexican Heritage Tee | Dia de los Muertos Shirt | Latino Pride Gift'\n"
        "10. POLITICAL/SARCASTIC TONE RULE: If the design has a political, satirical, or sarcastic tone (mocking a system, ironic commentary, protest humor, exaggerated social critique), the title MUST include 'Satire' or 'Sarcastic'. AVOID aggressive words like lunatic, crazy, stupid, idiot, moron — replace them with 'Satire' or 'Sarcastic' instead.\n"
        "10b. RECOGNIZABLE REAL PERSON RULE: If the design features any recognizable real person — politician, athlete, musician, actor, influencer, or public figure — identifiable by face, name, number, signature symbol, or unmistakable visual trait:\n"
        "    TITLE: Last name (or the most recognizable single name) is sufficient. Full name is not required in the title.\n"
        "      Examples: 'Tsunoda Fan Tee', 'Hamilton 44 Shirt', 'Swift Aesthetic Tee', 'Trump Sarcastic Gift'\n"
        "    TAGS: The person's FULL NAME ([First] [Last]) must appear in AT LEAST 1 tag, combined with a product or intent word.\n"
        "      Format: '[first last] shirt', '[first last] fan gift', '[first last] tee'\n"
        "      Examples by category:\n"
        "        Athlete: 'yuki tsunoda shirt', 'yuki tsunoda fan gift'\n"
        "        Musician: 'taylor swift shirt', 'taylor swift fan gift'\n"
        "        Politician: 'donald trump shirt', 'trump satire gift'\n"
        "        Actor: '[first last] fan shirt', '[first last] gift idea'\n"
        "    Additionally include at least 1 more tag with just the last name + product word (e.g. 'tsunoda tee', 'hamilton shirt').\n"
        "    Never replace the name with vague terms like 'political figure', 'famous athlete', or 'celebrity'.\n\n"
        "10c. TONE ACCURACY RULE: The tone word in the title must reflect the design's ACTUAL INTENT, not its surface imagery.\n"
        "    - Political figure + romantic symbol (heart, rose, kiss, love) = SARCASTIC or IRONIC intent, NOT romantic. Use: Sarcastic, Ironic, Satirical.\n"
        "    - 'Romantic', 'Sweet', 'Lovely', 'Cute' are ONLY allowed when the design is genuinely romantic with no political, cynical, or ironic context.\n"
        "    - When in doubt between romantic-looking and political/ironic, always choose the sarcastic framing.\n"
        "11. DISNEY/PIXAR RULE: If the design contains ANY Disney or Pixar universe visual element — including silhouettes, outlines, partial shapes — you MUST use the relevant character name AND film/franchise name in both the title and tags.\n"
        "    CRITICAL STUDIO DISTINCTION — Disney and Pixar are DIFFERENT studios. Use the correct tag:\n"
        "      'disney' ONLY (NOT 'disney pixar'): Mickey, Minnie, Goofy, Donald Duck, Pluto, Tinkerbell, Lilo, Stitch, Moana, Encanto/Mirabel, Frozen/Elsa/Anna/Olaf, Lion King/Simba, Tangled/Rapunzel, Little Mermaid/Ariel, Cinderella, Sleeping Beauty, Snow White.\n"
        "      'disney pixar' (Pixar films): Toy Story/Woody/Buzz, Finding Nemo/Dory, Ratatouille/Remy, Monsters Inc/Sully, The Incredibles, Up/Carl/Russell, Brave/Merida, Inside Out/Joy/Sadness, Coco/Miguel, Turning Red/Mei, Elemental/Ember, Cars/Lightning McQueen, WALL-E, A Bug's Life.\n"
        "    NEVER tag a Disney-only character (e.g. Stitch, Moana, Elsa) with 'disney pixar'. NEVER tag a Pixar character with just 'disney'.\n"
        "    Classic Disney triggers: Minnie bow → 'minnie mouse' + 'disney'; Mickey ears → 'mickey mouse' + 'disney'; castle silhouette → 'disney', 'magic kingdom'.\n"
        "    Lilo & Stitch → 'stitch' + 'lilo and stitch' + 'disney' (NOT disney pixar)\n"
        "    Do NOT avoid these terms when the design clearly warrants them, even if only a silhouette or outline.\n"
        "    REQUIRED in title: film/franchise name + character name (e.g. 'Stitch Lilo Disney Shirt', 'Remy Ratatouille Pixar Tee').\n"
        "    REQUIRED in tags: film name tag, character name tag, correct studio tag ('disney' or 'disney pixar').\n\n"
        "Title examples — subject/niche always FIRST, style word second:\n"
        "\"Mushroom Vintage Engraving T-Shirt | Cottagecore Forest Tee | Nature Lover Gift Idea\" (85 chars)\n"
        "\"Skeleton Retro Halloween Shirt | Spooky Dark Tee | Halloween Gift Idea\" (71 chars)\n"
        "\"Frog Wizard Storybook Tee | Whimsical Fantasy Shirt | Frog Lover Gift\" (70 chars)\n"
        "\"Butterfly Watercolor Floral Shirt | Boho Garden Tee | Cottagecore Gift Idea\" (76 chars)\n"
        "\"Graduation Class Dismissed Shirt | Senior 2025 Tee | Funny Grad Gift Idea\" (75 chars)\n"
        "\"Nurse Appreciation Retro Tee | RN Life Shirt | Nurse Gift Idea\" (62 chars)\n\n"

        + (
            f"PRODUCT TYPE: This design will be printed on a '{mockup_type}'. "
            f"Use '{_get_product_type_title_word(mockup_type)}' as the product type word in the title (e.g. 'Mushroom Retro {_get_product_type_title_word(mockup_type)} | ...'). "
            f"Do NOT use generic words like 'T-Shirt', 'Tee', 'Shirt', 'Top' unless the product type word above already contains them. "
            + ("In tags, NEVER use 'tee' or 'top' — use 'sweatshirt' instead whenever referencing the product. "
               if mockup_type.lower() == "sweatshirt" else
               "In tags, prefer 'long sleeve' over 'tee' when referencing the product type. "
               if mockup_type.lower() == "cc long" else "")
            + "Do NOT generate any product-type tag (e.g. unisex tshirt, long sleeve tshirt, comfort colors) — one will be added automatically. "
            + "Generate exactly 12 tags instead of 13.\n\n"
            if mockup_type and _get_product_type_tags(mockup_type) else ""
        )
        + f"=== STEP 3: EXACTLY {12 if mockup_type and _get_product_type_tags(mockup_type) else 13} ETSY TAGS ===\n"
        "Rules:\n"
        f"- Exactly {12 if mockup_type and _get_product_type_tags(mockup_type) else 13} tags, each ≤20 chars, all lowercase, no duplicates\n"
        "- Each tag must target a DIFFERENT search intent — do not repeat the same concept with minor wording changes\n"
        "- Prefer 2-3 word phrases over single words (higher search specificity)\n"
        "- Spell every tag correctly and completely — no truncated words\n"
        f"- FORBIDDEN tags (never use any of these): {forbidden_str}\n"
        "- Gender audience tags ('mens tshirt', 'womens tee', 'gift for him', 'gift for her') ONLY if the design unmistakably targets that gender. Otherwise use: 'birthday gift', '[subject] lover gift', '[subject] fan gift'.\n\n"
        "Cover these 7 intent categories across your 13 tags:\n"
        "1. SUBJECT+PRODUCT (2 tags): e.g. 'mushroom shirt', 'mushroom tee'\n"
        "2. STYLE+PRODUCT (2 tags): e.g. 'vintage graphic tee', 'cottagecore shirt'\n"
        "3. PURCHASE INTENT (2 tags): e.g. 'birthday gift', '[subject] lover gift'\n"
        "4. THEME/OCCASION (2 tags): e.g. 'halloween shirt', 'nature lover tee'\n"
        "5. PRODUCT TYPE (2 tags): e.g. 'graphic tee', 'novelty shirt'\n"
        "6. LONG-TAIL SPECIFIC (2 tags): the most specific possible phrase ≤20 chars, e.g. 'cottagecore mushroom'\n"
        "7. STYLE AESTHETIC (1 tag): e.g. 'cottagecore aesthetic', 'dark academia'\n"
        "CULTURAL IDENTITY RULE: If a cultural/ethnic identity was detected, include AT LEAST 2 tags that reference it directly. Examples: 'hispanic heritage', 'latino shirt', 'irish pride tee', 'mexican heritage'. These replace 2 slots from the categories above.\n"
        "PROFESSION RULE: If the design clearly depicts a profession or role (nurse, teacher, doctor, firefighter, paramedic, police, engineer, etc.), include AT LEAST 3 tags that directly target that profession using these formats: '[profession] appreciation', '[profession] gift idea', '[profession] shirt'. These replace 3 slots from the categories above.\n\n"

        "=== OUTPUT — strict JSON only, no markdown, no extra text ===\n"
        "{\"text_detected\":\"none\",\"text_content\":\"\","
        "\"detected_subjects\":[],\"style_detected\":\"\",\"title\":\"\",\"tags\":[],"
        "\"primary_color\":\"\",\"secondary_color\":\"\",\"occasion\":\"\",\"holiday\":\"\","
        "\"graphic\":\"\",\"reasoning\":\"\"}\n"
        "text_detected: 'none', 'readable', or 'partially_readable'.\n"
        "text_content: the extracted or best-guess text; empty string if none.\n"
        "style_detected: if text was partially_readable, append ' (text partially readable: <your guess>)' to the style string.\n"
        "reasoning: 2-3 sentences explaining why you chose these specific keywords, style label, and gift angle."
    )
    if prior_issues:
        issues_block = "\n".join(f"  - {i}" for i in prior_issues)
        base = (
            f"⚠️ PREVIOUS ATTEMPT HAD THESE ISSUES — fix all of them in your new output:\n{issues_block}\n\n"
            + base
        )
    return base





def _call_openai(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    _log = logging.getLogger("uvicorn.error")
    _log.info("[OpenAI] model=%s max_completion_tokens=%s", payload.get("model"), payload.get("max_completion_tokens"))
    with httpx.Client(timeout=90.0) as client:
        resp = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    _log.info("[OpenAI] status=%s", resp.status_code)
    _log.info("[OpenAI] full_response=%s", resp.text[:2000])
    resp.raise_for_status()
    return resp.json()


def _parse_seo_response(parsed: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw GPT JSON dict into a normalized seo fields dict."""
    out = _default_seo_fields()
    if not isinstance(parsed, dict):
        return _fallback_seo_fields()
    if str(parsed.get("error") or "").strip().lower() == "invalid_output":
        return _fallback_seo_fields()

    raw_title = str(parsed.get("title") or parsed.get("seo_title") or "").strip()
    raw_tags_list = parsed.get("tags") or parsed.get("seo_tags") or []

    raw_detected_list = parsed.get("detected_subjects") or []
    if not isinstance(raw_detected_list, list):
        raw_detected_list = []
    detected_subjects = [str(x).strip() for x in raw_detected_list if isinstance(x, str) and str(x).strip()]

    clean_title = _remove_hallucinated_subject_terms(raw_title, detected_subjects)
    out["seo_title"] = _normalize_seo_title(clean_title)

    raw_tags = _normalize_seo_tags(raw_tags_list)
    cleaned_tags = [_remove_hallucinated_subject_terms(t, detected_subjects) for t in raw_tags]
    # Apply char limit BEFORE forbidden check so truncated forms are also caught
    truncated_tags = [_normalize_tag_phrase(t) for t in cleaned_tags if t]
    filtered = _filter_forbidden_tags([t for t in truncated_tags if t])
    # Drop any tags that end with a dangling word (cut-off phrases) — runs after forbidden so
    # normalization already happened and short forms like "roaring bear with" are still caught.
    out["seo_tags"] = _filter_dangling_tags(filtered)[:_ETSY_TAG_MAX_COUNT]

    if len(out["seo_tags"]) < _ETSY_TAG_MAX_COUNT:
        filler_seed = [
            out["seo_title"],
            str(parsed.get("graphic") or ""),
            str(parsed.get("occasion") or ""),
            str(parsed.get("holiday") or ""),
            "shirt design", "graphic tee",
        ]
        for tag in _filter_forbidden_tags(_fallback_etsy_tags([x for x in filler_seed if x], out["seo_title"])):
            if tag not in out["seo_tags"]:
                out["seo_tags"].append(tag)
            if len(out["seo_tags"]) >= _ETSY_TAG_MAX_COUNT:
                break

    out["primary_color"] = str(parsed.get("primary_color") or "").strip()
    out["secondary_color"] = str(parsed.get("secondary_color") or "").strip()
    out["occasion"] = str(parsed.get("occasion") or "").strip()
    out["holiday"] = str(parsed.get("holiday") or "").strip()
    out["graphic"] = str(parsed.get("graphic") or "").strip()
    out["style_detected"] = str(parsed.get("style_detected") or "").strip()
    out["reasoning"] = str(parsed.get("reasoning") or "").strip()[:600]
    # Store GPT's actual detected subjects so self-check can use them (not derived from title)
    out["detected_subjects_raw"] = detected_subjects
    raw_text_detected = str(parsed.get("text_detected") or "none").strip().lower()
    out["text_detected"] = raw_text_detected if raw_text_detected in ("readable", "partially_readable") else "none"
    out["text_content"] = str(parsed.get("text_content") or "").strip()[:200]
    out["needs_text_input"] = out["text_detected"] == "partially_readable" and not out["text_content"]

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


def _selfcheck_seo_output(
    title: str,
    tags: list[str],
    style_detected: str,
    detected_subjects: list[str],
    api_key: str,
    model: str,
) -> list[str]:
    """Returns list of issues found; empty list = OK."""
    forbidden_str = ", ".join(sorted(_FORBIDDEN_TAG_TERMS))
    subjects_str = ", ".join(detected_subjects) if detected_subjects else "unknown"
    tags_str = ", ".join(tags)
    check_prompt = (
        "You are a strict quality checker for Etsy t-shirt SEO listings.\n\n"
        f"LISTING:\n"
        f"Title: {title}\n"
        f"Tags: {tags_str}\n"
        f"Detected art style: {style_detected}\n"
        f"Detected subjects: {subjects_str}\n\n"
        "CHECK THESE 4 ISSUES:\n\n"
        "1. AUDIENCE ASSUMPTION: Does the title contain phrases like 'for Women', 'for Men', 'for Girls', 'for Boys', 'for Him', 'for Her'?\n"
        "   These are ONLY valid if detected_subjects clearly includes a gender-specific element (nurse, mom, dad, explicitly feminine/masculine character).\n"
        "   If the design is gender-neutral (mushroom, skull, animal, coffee, abstract), flag it.\n\n"
        "2. FORBIDDEN TAGS: Check every tag against this list:\n"
        f"   FORBIDDEN TERMS: {forbidden_str}\n"
        "   A tag is forbidden if it CONTAINS any of the above terms as whole words — exact match, prefix, suffix, or embedded.\n"
        "   Examples: 'graphic t shirt' is forbidden → 'bold graphic t shirt' is ALSO forbidden (contains it).\n"
        "   'novelty tee' is forbidden → 'funny novelty tee' is ALSO forbidden.\n"
        "   'cute shirt' is forbidden → 'super cute shirt' is ALSO forbidden.\n"
        "   Check substring containment, not just exact match. Flag the full tag and which forbidden term it contains.\n\n"
        "3. SPELLING & TRUNCATION — check EVERY word in every tag and the title:\n"
        "   a) TRUNCATED WORDS: Is any word cut off mid-spelling? Examples of bad: 'tshir', 'tshi', 'vintagee', 'graphi', 'apprec', 'birtday', 'hallowee', 'aestheti', 'character te' (where 'te' is a truncated 'tee'). Flag the exact bad word and what it should be.\n"
        "   b) DOUBLED LETTERS: Does any word end with an unintended repeated letter? Examples: 'shirtt', 'vintagee', 'tshirtt'. Flag it.\n"
        "   c) RUN-TOGETHER WORDS: Are words incorrectly joined without a space? Examples: 'birthdaygift' → 'birthday gift', 'mushromshirt' → 'mushroom shirt'. Flag it.\n"
        "   d) REAL-WORD CHECK: Is every word a real English word or a known proper noun? Single letters alone (except 'a') or 2-letter fragments that aren't real words must be flagged.\n"
        "   e) TAG LENGTH: Every tag must be ≤20 characters. If a tag is 21+ characters, flag it — even if it looks like a real word.\n"
        "   f) TRUNCATED ENDING: If a tag ends with a 1-2 character fragment that appears to be a cut-off word (e.g. tag ending in '...m', '...si', '...ti', '...ae'), flag it as truncated.\n"
        "   Quote the EXACT problematic word in your issue description.\n\n"
        "4. TONE MISMATCH: Does the detected style conflict with the tags?\n"
        "   Examples of mismatches: vintage/retro style + 'funny gift'; dark/gothic style + 'cute shirt'; minimalist style + 'maximalist'.\n\n"
        "5. CULTURAL IDENTITY: If the detected subjects clearly include a cultural/ethnic identity (Hispanic, Latino, Irish, Asian, Mexican, etc.), verify that:\n"
        "   a) The title includes the cultural identity term.\n"
        "   b) At least 2 tags directly reference that cultural identity.\n"
        "   Flag if either condition is missing.\n\n"
        "6. PROFESSION RULE: If the detected subjects include a profession or role (nurse, teacher, doctor, firefighter, etc.), verify that at least 3 tags use profession-specific formats: '[profession] appreciation', '[profession] gift idea', '[profession] shirt'. Flag if fewer than 3 such tags are present.\n\n"
        "7. POLITICAL/SARCASTIC TONE: If the design has a political, satirical, or sarcastic tone, verify that the title contains 'Satire' or 'Sarcastic'. Also flag if the title or tags contain aggressive words like lunatic, crazy, stupid, idiot, moron — these must be replaced.\n"
        "   TONE ACCURACY: If the detected subjects include a political figure combined with romantic symbols (heart, rose, kiss, love), the title must use 'Sarcastic', 'Ironic', or 'Satirical' — NOT 'Romantic', 'Sweet', or 'Lovely'. Flag any title that uses romantic tone words on a clearly political/ironic design.\n"
        "   RECOGNIZABLE REAL PERSON: If the detected subjects include any recognizable real person (politician, athlete, musician, actor, etc.), verify:\n"
        "   a) The title contains at least the person's last name (or most recognizable name). Full name not required in title.\n"
        "   b) At least 1 tag contains the person's FULL NAME ([First Last]) combined with a product/intent word (e.g. 'yuki tsunoda shirt', 'taylor swift fan gift').\n"
        "   c) At least 1 additional tag uses just the last name + product word (e.g. 'tsunoda tee', 'hamilton shirt').\n"
        "   Flag if any of these conditions are missing.\n\n"
        "8. DISNEY/PIXAR KEYWORDS: Check whether ANY of the following appears in detected_subjects OR style_detected OR the title/tags themselves:\n"
        "   Disney-ONLY indicators (tag must be 'disney', NEVER 'disney pixar'): mickey, minnie, mouse ears, mickey ears, minnie bow, magic kingdom, disneyland, disney world, castle, cinderella, tinkerbell, stitch, lilo, goofy, donald duck, pluto, moana, encanto, mirabel, luisa, frozen, elsa, anna, olaf, simba, lion king, rapunzel, ariel, little mermaid\n"
        "   Pixar indicators (tag must be 'disney pixar'): remy, ratatouille, nemo, dory, finding nemo, woody, buzz, buzz lightyear, toy story, miguel, coco, carl, russell, up, brave, merida, inside out, joy, sadness, monsters inc, sully, mike wazowski, incredibles, violet, dash, turning red, mei, elemental, ember, wade, lightning mcqueen, cars, wall-e\n"
        "   CRITICAL STUDIO CHECK: If a Disney-only character (Stitch, Moana, Elsa, Simba, etc.) is detected, flag any tag containing 'disney pixar' — it must be just 'disney'. If a Pixar character (Remy, Woody, Nemo, etc.) is detected, flag if studio tag is only 'disney' without 'pixar'.\n"
        "   If ANY indicator is found, verify ALL of the following:\n"
        "   a) The title contains BOTH the character name AND the film/franchise name.\n"
        "   b) At least 1 tag contains the character's name.\n"
        "   c) At least 1 tag contains the film/franchise name.\n"
        "   d) Studio tag is correct: 'disney' for Disney-only, 'disney pixar' for Pixar films.\n"
        "   IMPORTANT: Silhouettes and outlines count. Any partial or stylized reference triggers this check.\n"
        "   Flag specifically which character/film was detected and which condition (a/b/c/d) is missing.\n\n"
        "Return strict JSON only:\n"
        "{\"ok\": true} — no issues\n"
        "{\"ok\": false, \"issues\": [\"specific issue 1\", \"specific issue 2\"]} — issues found\n"
        "Be specific in issue descriptions so they can be fixed."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "max_completion_tokens": 500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return only strict JSON. No markdown, no explanation."},
            {"role": "user", "content": check_prompt},
        ],
    }
    try:
        data = _call_openai(payload, api_key)
        txt = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        result = json.loads(txt) if isinstance(txt, str) else {}
        if not isinstance(result, dict):
            return []
        if result.get("ok") is True:
            return []
        issues = result.get("issues") or []
        return [str(i) for i in issues if i][:8]
    except Exception:
        return []


_KNOWN_TAG_CORRECTIONS: dict[str, str] = {
    "tshir": "tshirt", "tshi": "tshirt", "tsh": "tshirt",
    "vintagee": "vintage", "vintag": "vintage",
    "graphi": "graphic", "graph": "graphic",
    "apprec": "appreciation", "appreciat": "appreciation",
    "birtday": "birthday", "birthda": "birthday",
    "hallowee": "halloween", "hallowe": "halloween",
    "christma": "christmas", "christm": "christmas",
    "aestheti": "aesthetic", "aesthet": "aesthetic",
    "cottageor": "cottagecore", "cottagecor": "cottagecore",
    "minimalst": "minimalist", "minimalis": "minimalist",
}

_SHORT_WORD_WHITELIST: set[str] = {
    "tee", "tees", "top", "dad", "mom", "rn", "md", "pa", "er", "ed",
    "go", "do", "so", "no", "art", "fan", "fun", "new", "old", "big",
}


# Tags ending with these words are likely truncated (phrase expects more after them)
_DANGLING_TAG_ENDINGS: set[str] = {
    "holding", "wearing", "with", "and", "of", "in", "on", "by", "for",
    "the", "a", "an", "to", "from", "at", "into", "about", "carrying",
    "sitting", "standing", "flying", "running", "looking", "featuring",
}


def _detect_tag_typos(tags: list[str], title: str) -> list[str]:
    """Fast, API-free check for obvious truncations and typos in tags and title."""
    issues: list[str] = []
    all_texts = [("tag", t) for t in tags] + [("title", title)]
    for source, text in all_texts:
        words = re.findall(r"[a-z']+", text.lower())
        for word in words:
            # Truncation: matches a known bad prefix
            for bad, correction in _KNOWN_TAG_CORRECTIONS.items():
                if word == bad:
                    issues.append(
                        f"Truncated word in {source}: '{word}' — should be '{correction}'"
                    )
                    break
            # Suspiciously short standalone word
            if len(word) <= 2 and word not in _SHORT_WORD_WHITELIST:
                issues.append(
                    f"Suspiciously short/incomplete word in {source}: '{word}'"
                )
            # Trailing doubled letter (vintagee, shirtt, tshirtt)
            if len(word) >= 4 and word[-1] == word[-2] and word[:-1] in _KNOWN_TAG_CORRECTIONS.values():
                issues.append(
                    f"Double-letter typo in {source}: '{word}' — likely '{word[:-1]}'"
                )
        # Tag-level check: dangling word at end implies truncation
        if source == "tag":
            last_word = words[-1] if words else ""
            if last_word in _DANGLING_TAG_ENDINGS:
                issues.append(
                    f"Incomplete/truncated tag '{text}' — ends with '{last_word}' which suggests it was cut off"
                )
        # Title-level check: scan for forbidden terms in the title
        if source == "title":
            tl = text.lower()
            for term in _FORBIDDEN_TAG_TERMS:
                if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", tl):
                    issues.append(
                        f"Forbidden term '{term}' found in title — remove or replace it"
                    )
    return issues


def _run_seo_generation_once(
    image_ref: str,
    model: str,
    api_key: str,
    prior_issues: list[str],
    text_hint: str = "",
    mockup_type: str = "",
) -> dict[str, Any]:
    prompt_text = _build_seo_prompt(prior_issues, text_hint=text_hint, mockup_type=mockup_type)
    payload = {
        "model": model,
        "temperature": 0.3,
        "max_completion_tokens": 1000,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You are an Etsy SEO specialist. Return only strict JSON output, no markdown, no explanation.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": image_ref, "detail": "auto"}},
                ],
            },
        ],
    }
    try:
        data = _call_openai(payload, api_key)
        txt = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(txt) if isinstance(txt, str) else {}
    except Exception as exc:
        raise RuntimeError(f"GPT analiz hatası: {exc}")
    return _parse_seo_response(parsed)


def _analyze_design_for_seo(design_path: Path, text_hint: str = "", mockup_type: str = "") -> dict[str, Any]:
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

    prior_issues: list[str] = []
    out: dict[str, Any] = {}
    for attempt in range(1, 3):
        out = _run_seo_generation_once(image_ref, model, api_key, prior_issues, text_hint=text_hint, mockup_type=mockup_type)
        # Fast code-level typo check (no API call)
        typo_issues = _detect_tag_typos(out.get("seo_tags", []), out.get("seo_title", ""))
        if typo_issues:
            out["attempt_count"] = attempt
            out["selfcheck_issues"] = typo_issues
            prior_issues = typo_issues
            if attempt < 2:
                continue
            break
        # Use GPT's actual detected subjects, not title-derived words
        detected_subjects = out.get("detected_subjects_raw") or []
        issues = _selfcheck_seo_output(
            title=out.get("seo_title", ""),
            tags=out.get("seo_tags", []),
            style_detected=out.get("style_detected", ""),
            detected_subjects=detected_subjects,
            api_key=api_key,
            model=model,
        )
        out["attempt_count"] = attempt
        out["selfcheck_issues"] = issues
        if not issues:
            break
        prior_issues = issues

    # Append product-type tags after GPT generation (they don't count toward GPT's budget)
    product_tags = _get_product_type_tags(mockup_type)
    if product_tags:
        gpt_tags = out.get("seo_tags") or []
        # Remove any GPT-generated tags that duplicate product type tags
        product_tags_set = set(t.lower() for t in product_tags)
        gpt_tags = [t for t in gpt_tags if t.lower() not in product_tags_set]
        # Trim GPT tags to make guaranteed room for product tags
        gpt_tags = gpt_tags[:(_ETSY_TAG_TOTAL - len(product_tags))]
        out["seo_tags"] = gpt_tags + product_tags

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


_MOCKUP_SLUG_STOPWORDS: set[str] = {
    "t-shirt", "tshirt", "tee", "shirt", "top", "the", "a", "an", "and",
    "for", "to", "of", "in", "on", "at", "with", "by", "from", "is",
    "it", "my", "me", "i", "your", "our", "gift", "idea", "ideas",
}

# Words that signal style/aesthetic — used to enrich rank-1 slug
_MOCKUP_STYLE_WORDS: set[str] = {
    "retro", "vintage", "funny", "cute", "aesthetic", "bold", "minimalist",
    "classic", "cool", "trendy", "boho", "groovy", "whimsical", "kawaii",
    "silly", "sarcastic", "witty", "humorous", "quirky", "cottagecore",
    "colorful", "pastel", "dark", "floral", "abstract",
}

# Words that signal occasion / scenario — used for rank-3 slug
_MOCKUP_SCENARIO_WORDS: set[str] = {
    "birthday", "christmas", "halloween", "thanksgiving", "easter", "holiday",
    "teacher", "nurse", "doctor", "mom", "dad", "sister", "brother",
    "friend", "coworker", "colleague", "student", "graduation", "wedding",
    "summer", "fall", "autumn", "winter", "spring", "beach", "camping",
    "hiking", "travel", "coffee", "wine", "cat", "dog", "mama", "grandma",
    "grandpa", "valentines", "mothers", "fathers",
}

# Product type words to pull from tags for rank-2 slug
_MOCKUP_PRODUCT_WORDS: set[str] = {
    "sweatshirt", "hoodie", "crewneck", "pullover", "sleeve", "comfort",
}


def _mockup_slug_for_rank(seo_title: str, tags: list[str], rank: int) -> str:
    """Return a unique keyword-combination slug based on the image rank position.

    rank 1 → main topic + style words        (e.g. "mushroom-retro-forest")
    rank 2 → secondary segment + product     (e.g. "nature-lover-sweatshirt")
    rank 3 → occasion/scenario + niche       (e.g. "birthday-gift-mushroom")
    rank 4+ → gift prefix + main topic       (e.g. "gift-mushroom-lover")
    """
    stop = _MOCKUP_SLUG_STOPWORDS

    def clean(text: str) -> list[str]:
        # Strip apostrophes first so "don't" → "dont" (not "don" + "t")
        text = re.sub(r"[''`']", "", text.lower())
        return [
            w.strip("-")
            for w in re.sub(r"[^\w\s-]", " ", text).split()
            if len(w.strip("-")) > 1 and w.strip("-") not in stop
        ]

    segments = re.split(r"[|—–]", seo_title) if seo_title else [""]
    seg1 = clean(segments[0]) if segments else []
    seg2 = clean(segments[1]) if len(segments) > 1 else []

    # Flatten all tag words, deduplicated against seg1; exclude scenario/occasion words entirely
    seen_tw: set[str] = set(seg1)
    tag_words: list[str] = []
    for tag in (tags or []):
        for w in clean(tag):
            if w not in seen_tw and w not in _MOCKUP_SCENARIO_WORDS:
                seen_tw.add(w)
                tag_words.append(w)

    # Also exclude scenario words from seg1 (title may mention "birthday" etc.)
    style     = [w for w in seg1 + tag_words if w in _MOCKUP_STYLE_WORDS]
    product   = [w for w in tag_words if w in _MOCKUP_PRODUCT_WORDS]
    main      = [w for w in seg1
                 if w not in _MOCKUP_STYLE_WORDS
                 and w not in _MOCKUP_PRODUCT_WORDS
                 and w not in _MOCKUP_SCENARIO_WORDS]
    secondary = [
        w for w in tag_words
        if w not in _MOCKUP_STYLE_WORDS
        and w not in _MOCKUP_PRODUCT_WORDS
    ]

    if rank == 1:
        # main topic + style
        parts = main[:3] + style[:2]
        if not parts:
            parts = seg1[:5]
    elif rank == 2:
        # secondary segment or tag words + product type
        base = seg2[:3] if seg2 else secondary[:3]
        parts = base + product[:2]
        if not parts:
            parts = tag_words[:5]
    elif rank == 3:
        # design-secondary: non-title tag keywords (no scenario, no gift)
        non_gift_secondary = [w for w in secondary if w not in {"gift", "gifts", "present"}]
        parts = non_gift_secondary[:5]
        if not parts:
            parts = seg2[:3] + secondary[:2]
        if not parts:
            parts = seg1[1:5] or seg1[:5]
    else:
        # gift + main topic
        parts = ["gift"] + main[:4]
        if len(parts) <= 1:
            parts = ["gift"] + seg1[:4]

    parts = [p for p in parts if p][:5]
    return "-".join(parts) if parts else "design"


def _mockup_filename_for_upload(
    seo_title: str,
    image_url: str,
    tags: list[str] | None = None,
    rank: int = 1,
) -> str:
    """Build a rank-varied renamed mockup filename.

    Each image in a listing gets a different keyword combination so filenames
    are not repetitive and cover more SEO surface area.
    Example (rank 1): mushroom-retro-forest-1.png
    Example (rank 2): nature-lover-sweatshirt-2.png
    """
    slug = _mockup_slug_for_rank(seo_title, tags or [], rank)
    path = Path(image_url.split("?")[0])
    original_stem = path.stem  # e.g. "1", "12"
    ext = path.suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    return f"{slug}-{original_stem}{ext}"


def _upload_images_best_effort(
    listing_id: int,
    images: list[Any],
    seo_title: str = "",
    tags: list[str] | None = None,
) -> str:
    uploaded = 0
    failed = 0
    last_error = ""
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
            filename = (
                _mockup_filename_for_upload(seo_title, url, tags=tags, rank=rank)
                if seo_title
                else None
            )
            # R2-stored images: fetch directly via boto3 to avoid HTTP loopback issues
            r2_key = _workspace_r2_key_from_media_url(url)
            if r2_key:
                bucket = (os.environ.get("S3_BUCKET") or "").strip()
                raw = _r2_client().get_object(Bucket=bucket, Key=r2_key)["Body"].read()
                upload_listing_image_from_bytes(listing_id, raw, rank=rank, overwrite=True, filename=filename)
            else:
                fetch_url = _public_image_fetch_url(url)
                upload_listing_image_from_url(listing_id, fetch_url, rank=rank, overwrite=True, filename=filename)
            uploaded += 1
            rank += 1
        except Exception as exc:
            _log = logging.getLogger("uvicorn.error")
            _log.exception("[upload] Görsel yüklenemedi url=%s hata=%s", url, exc)
            last_error = str(exc)
            failed += 1
    if uploaded == 0 and failed > 0:
        return f"Görsel yüklenemedi: {last_error}"
    if failed > 0:
        return f"{uploaded} görsel yüklendi, {failed} başarısız: {last_error}"
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
def workspace_download_selected_mockups(
    selected_urls: str = Form("[]"),
    seo_title: str = Form(""),
    tags_json: str = Form("[]"),
) -> Response:
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
            r2_pairs.append((rk, item))  # store original url for naming
    if not files and not r2_pairs:
        raise HTTPException(status_code=400, detail="Indirilecek secili mockup bulunamadi.")

    title = seo_title.strip()
    try:
        dl_tags = json.loads(tags_json or "[]")
        if not isinstance(dl_tags, list):
            dl_tags = []
        dl_tags = [str(t).strip() for t in dl_tags if t]
    except Exception:
        dl_tags = []
    buf = io.BytesIO()
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    client = _r2_client() if r2_pairs else None
    img_rank = 1
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            arcname = _mockup_filename_for_upload(title, p.name, tags=dl_tags, rank=img_rank) if title else p.name
            zf.write(p, arcname=arcname)
            img_rank += 1
        if client and bucket:
            for s3_key, original_url in r2_pairs:
                arcname = (
                    _mockup_filename_for_upload(title, original_url, tags=dl_tags, rank=img_rank)
                    if title
                    else _workspace_r2_zip_arcname(original_url)
                )
                try:
                    body = client.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
                    zf.writestr(arcname, body)
                    img_rank += 1
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
        text_hint = str(draft.get("user_text_hint") or "").strip()
        # Detect mockup type from generated image URLs (most reliable) — fallback to draft field
        all_image_urls = [str(u) for u in (draft.get("images") or []) if u]
        mockup_type = _detect_mockup_type_from_urls(all_image_urls) or str(draft.get("mockup_type") or "").strip()
        draft["mockup_type"] = mockup_type  # persist for tab restore
        seo = _analyze_design_for_seo(black_path, text_hint=text_hint, mockup_type=mockup_type)
        draft["seo_title"] = str(seo.get("seo_title") or "").strip()[:140]
        draft["seo_tags"] = _normalize_seo_tags(seo.get("seo_tags"))
        draft["primary_color"] = str(seo.get("primary_color") or "").strip()
        draft["secondary_color"] = str(seo.get("secondary_color") or "").strip()
        draft["occasion"] = str(seo.get("occasion") or "").strip()
        draft["holiday"] = str(seo.get("holiday") or "").strip()
        draft["graphic"] = str(seo.get("graphic") or "").strip()
        draft["style_detected"] = str(seo.get("style_detected") or "").strip()
        draft["reasoning"] = str(seo.get("reasoning") or "").strip()
        draft["attempt_count"] = int(seo.get("attempt_count") or 1)
        draft["selfcheck_issues"] = seo.get("selfcheck_issues") or []
        draft["text_detected"] = str(seo.get("text_detected") or "none")
        draft["text_content"] = str(seo.get("text_content") or "")
        draft["needs_text_input"] = bool(seo.get("needs_text_input", False))
        if draft["seo_title"]:
            draft["title"] = draft["seo_title"]
        if draft["seo_tags"]:
            draft["tags"] = draft["seo_tags"][:_ETSY_TAG_MAX_COUNT]
        draft = _normalize_images_only_draft(draft)
        attempt_count = draft["attempt_count"]
        status_msg = f"SEO analizi tamamlandı ({attempt_count}. denemede)."
        if draft.get("style_detected"):
            status_msg += f" Stil: {draft['style_detected']}."
        return _redirect_workspace_state(
            {
                "status": status_msg,
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

        status += " | " + _upload_images_best_effort(listing_id, images, seo_title=seo_title, tags=tags)

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
