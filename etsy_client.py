"""
Etsy Open API v3: taslak (draft) listing oluşturur ve ilk görseli yükler.

Not: Etsy API payload şeması zamanla değişebilir; hatada response body'yi
görmek için exception mesajını aynen kullan.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


ETSY_API = "https://api.etsy.com/v3"


def _x_api_key_value() -> str:
    api_key = os.environ.get("ETSY_API_KEY")
    keystring = os.environ.get("ETSY_KEYSTRING")
    shared_secret = os.environ.get("ETSY_SHARED_SECRET")
    if api_key and ":" in api_key:
        return api_key
    if keystring and shared_secret:
        return f"{keystring}:{shared_secret}"
    raise RuntimeError(
        "Etsy x-api-key için ETSY_API_KEY (keystring:shared_secret) veya "
        "ETSY_KEYSTRING + ETSY_SHARED_SECRET ayarlayın."
    )


def _refresh_oauth_tokens() -> None:
    """
    access_token süresi dolduğunda (≈1 saat) refresh_token ile yenisini alır.
    os.environ güncellenir; süreç çalışırken sonraki istekler yeni token kullanır.
    Kalıcı olsun diye .env'i elle veya script ile güncelleyebilirsiniz.
    """
    refresh = (os.environ.get("ETSY_REFRESH_TOKEN") or "").strip()
    keystring = (os.environ.get("ETSY_KEYSTRING") or "").strip()
    if not refresh:
        raise RuntimeError(
            "ETSY_ACCESS_TOKEN süresi doldu; ETSY_REFRESH_TOKEN .env içinde yok veya boş. "
            "Etsy geliştirici OAuth akışıyla yeniden yetkilendirin."
        )
    if not keystring:
        raise RuntimeError("ETSY_KEYSTRING eksik (token yenilemede client_id olarak kullanılır).")
    x_api_key = _x_api_key_value()
    url = f"{ETSY_API}/public/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": keystring,
        "refresh_token": refresh,
    }
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "x-api-key": x_api_key,
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(
            f"Etsy token yenilenemedi ({r.status_code}): {r.text}. "
            "Refresh token süresi dolmuş veya iptal olmuş olabilir (yaklaşık 90 gün); "
            "Etsy uygulamanızdan satıcıyla yeniden OAuth bağlantısı kurun."
        )
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Etsy token yanıtı beklenmedik: {body!r}")
    access = body.get("access_token")
    if not access:
        raise RuntimeError(f"Etsy yanıtında access_token yok: {body}")
    os.environ["ETSY_ACCESS_TOKEN"] = str(access)
    new_refresh = body.get("refresh_token")
    if new_refresh:
        os.environ["ETSY_REFRESH_TOKEN"] = str(new_refresh)


def _client_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """401 invalid_token ise bir kez token yenileyip isteği tekrarlar."""
    r = client.request(method, url, **kwargs)
    if r.status_code != 401:
        return r
    err = (r.text or "").lower()
    if "invalid_token" not in err and "expired" not in err:
        return r
    _refresh_oauth_tokens()
    headers = kwargs.get("headers")
    if isinstance(headers, dict):
        tok = os.environ.get("ETSY_ACCESS_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    return client.request(method, url, **kwargs)


def _env_int(name: str, *, default: Optional[int] = None, required: bool = True) -> int:
    """
    Ortam değişkenini int'e çevirir.
    `YOUR_*` placeholder ise daha anlaşılır hata verir.
    """
    val = os.environ.get(name)
    if val is None:
        if default is not None:
            return default
        if required:
            raise RuntimeError(f"{name} ortam değişkeni eksik.")
        return default  # type: ignore[return-value]

    if not isinstance(val, str):
        raise RuntimeError(f"{name} değeri string olmalı.")

    trimmed = val.strip()
    if trimmed == "" or trimmed.startswith("YOUR_") or trimmed.startswith("YOUR-") or trimmed.startswith("YOUR "):
        raise RuntimeError(
            f"{name} sayısal olmalı. Şu an: {val!r}. Etsy panelinden numeric ID alıp gir."
        )
    if "YOUR" in trimmed:
        raise RuntimeError(f"{name} sayısal olmalı. Şu an: {val!r}.")

    try:
        return int(trimmed)
    except ValueError:
        raise RuntimeError(f"{name} sayısal olmalı. Şu an: {val!r}.")


def _headers() -> dict[str, str]:
    """
    Etsy v3 için x-api-key: `keystring:shared_secret` biçimini oluşturur.
    - Eğer ETSY_API_KEY zaten `keystring:shared_secret` içeriyorsa onu kullanır.
    - Yoksa ETSY_KEYSTRING + ETSY_SHARED_SECRET birleştirir.
    """
    token = os.environ.get("ETSY_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("ETSY_ACCESS_TOKEN ortam değişkenini ayarlayın.")
    return {
        "x-api-key": _x_api_key_value(),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

def _headers_json() -> dict[str, str]:
    h = dict(_headers())
    h["Content-Type"] = "application/json"
    return h


def normalize_listing_who_when_supply(
    *,
    who_made: str,
    when_made: str,
    is_supply: bool,
) -> tuple[str, str, bool, str]:
    """
    Etsy 'invalid_marketplace' hatasını tetikleyen kombinasyonları düzeltir.
    Örn. başka şirket + made to order + bitmiş ürün yasaktır; someone_else + finished
    ürün için üretim ortağı şarttır — API'de en güvenlisi POD için i_did kullanmaktır.
    Dönüş: (who_made, when_made, is_supply, kullanıcıya kısa uyarı veya "").
    """
    who = (who_made or "i_did").strip()
    when = (when_made or "made_to_order").strip()
    note = ""

    if not is_supply and who == "someone_else":
        who = "i_did"
        note = (
            "Etsy: 'Another company/person' + bitmiş ürün API'de genelde reddedilir; "
            "taslak 'I did' ile açıldı. POD/print partner gerekiyorsa Etsy panelinde production partner ekleyin."
        )

    return who, when, is_supply, note


def create_draft_listing(
    *,
    shop_id: Optional[int] = None,
    title: str,
    description: str,
    price: str,
    quantity: int = 1,
    taxonomy_id: Optional[int] = None,
    who_made: str = "i_did",
    when_made: str = "made_to_order",
    is_supply: bool = False,
    shipping_profile_id: Optional[int] = None,
    readiness_state_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Etsy listing.state = draft oluşturur.
    Bitmiş ürün için who_made=i_did veya collective genelde güvenlidir;
    someone_else + finished ürün API'de sıkça invalid_marketplace verir.
    """
    sid = shop_id if shop_id is not None else _env_int("ETSY_SHOP_ID", required=True)
    tax = taxonomy_id if taxonomy_id is not None else _env_int(
        "ETSY_TAXONOMY_ID", default=1218, required=False
    )
    ship = (
        shipping_profile_id
        if shipping_profile_id is not None
        else _env_int("ETSY_SHIPPING_PROFILE_ID", required=True)
    )
    ready = (
        readiness_state_id
        if readiness_state_id is not None
        else _env_int("ETSY_READINESS_STATE_ID", required=False, default=None)  # type: ignore[arg-type]
    )

    payload = {
        "title": title[:140],
        "description": description,
        "price": price,  # "29.99"
        "quantity": str(quantity),
        "who_made": who_made,
        "when_made": when_made,
        "is_supply": "true" if is_supply else "false",
        "taxonomy_id": str(tax),
        "type": "physical",
        "state": "draft",
        "shipping_profile_id": str(ship),
    }
    # Fiziksel listing'ler için readiness_state_id çoğu durumda gereklidir.
    if ready is not None:
        payload["readiness_state_id"] = str(ready)

    url = f"{ETSY_API}/application/shops/{sid}/listings"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "POST", url, data=payload, headers=_headers())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy API {r.status_code}: {r.text}")
    data = r.json()
    return data if isinstance(data, dict) else {"result": data}


def update_existing_listing(
    *,
    listing_id: int,
    shop_id: Optional[int] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    price: Optional[str] = None,
    quantity: Optional[int] = None,
) -> dict[str, Any]:
    """
    Mevcut bir Etsy listing'i (özellikle draft) günceller.
    """
    sid = shop_id if shop_id is not None else _env_int("ETSY_SHOP_ID", required=True)
    payload: dict[str, str] = {}
    if title is not None:
        payload["title"] = title[:140]
    if description is not None:
        payload["description"] = description[:49990]
    if price is not None:
        payload["price"] = str(price)
    if quantity is not None:
        payload["quantity"] = str(quantity)

    if not payload:
        return {"warning": "Güncellenecek alan verilmedi."}

    url = f"{ETSY_API}/application/shops/{sid}/listings/{listing_id}"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "PATCH", url, data=payload, headers=_headers())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy update listing {r.status_code}: {r.text}")
    data = r.json()
    return data if isinstance(data, dict) else {"result": data}


def delete_listing(listing_id: int, shop_id: Optional[int] = None) -> None:
    """Etsy listing'i siler (draft dahil)."""
    sid = shop_id if shop_id is not None else _env_int("ETSY_SHOP_ID", required=True)
    url = f"{ETSY_API}/application/shops/{sid}/listings/{listing_id}"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "DELETE", url, headers=_headers_json())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy delete listing {r.status_code}: {r.text}")


def upload_listing_image_from_url(
    listing_id: int,
    image_url: str,
    shop_id: Optional[int] = None,
    rank: Optional[int] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    URL'den görsel indirip listing'e yükler.
    """
    sid = shop_id if shop_id is not None else _env_int("ETSY_SHOP_ID", required=True)
    token = os.environ.get("ETSY_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("ETSY_ACCESS_TOKEN gerekli.")

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        img = client.get(image_url)
        img.raise_for_status()
        content_type = img.headers.get("content-type") or "image/jpeg"

        files = {"image": ("image.jpg", img.content, content_type)}
        data: dict[str, str] = {}
        if rank is not None:
            data["rank"] = str(rank)
        if overwrite:
            data["overwrite"] = "1"
        headers = _headers()
        # multipart upload için Content-Type'ı httpx kendisi belirlemeli.
        headers.pop("Content-Type", None)
        r = _client_request(
            client,
            "POST",
            f"{ETSY_API}/application/shops/{sid}/listings/{listing_id}/images",
            headers=headers,
            files=files,
            data=data,
        )

    if r.status_code >= 400:
        raise RuntimeError(f"Etsy görsel yükleme {r.status_code}: {r.text}")
    return r.json()


def get_listing_images(listing_id: int, shop_id: Optional[int] = None) -> list[dict[str, Any]]:
    url = f"{ETSY_API}/application/listings/{listing_id}/images"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "GET", url, headers=_headers_json())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy get listing images {r.status_code}: {r.text}")
    data = r.json()
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return [x for x in data["results"] if isinstance(x, dict)]
    return []


def delete_listing_image(listing_id: int, listing_image_id: int, shop_id: Optional[int] = None) -> None:
    url = f"{ETSY_API}/application/listings/{listing_id}/images/{listing_image_id}"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "DELETE", url, headers=_headers_json())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy delete listing image {r.status_code}: {r.text}")


def get_listing_inventory(listing_id: int) -> dict[str, Any]:
    url = f"{ETSY_API}/application/listings/{listing_id}/inventory"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "GET", url, headers=_headers_json())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy get inventory {r.status_code}: {r.text}")
    data = r.json()
    return data if isinstance(data, dict) else {"result": data}


def update_listing_inventory(
    *,
    listing_id: int,
    products: list[dict[str, Any]],
    property_ids: list[int],
) -> dict[str, Any]:
    """
    Listing inventory'yi komple günceller (PUT).
    Custom variation property_id: 513 ve 514.
    """
    payload = {
        "products": products,
        "price_on_property": property_ids,
        "quantity_on_property": property_ids,
        "sku_on_property": property_ids,
    }
    url = f"{ETSY_API}/application/listings/{listing_id}/inventory"
    with httpx.Client(timeout=60.0) as client:
        r = _client_request(client, "PUT", url, json=payload, headers=_headers_json())
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy update inventory {r.status_code}: {r.text}")
    data = r.json()
    return data if isinstance(data, dict) else {"result": data}
