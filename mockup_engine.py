from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageStat

_DESIGN_OPACITY_MULTIPLIER = 0.85
# Cok ince-uzun tasarimlar width'e gore fazla buyuyebiliyor; ekstra daraltma uygula.
_SUPER_TALL_RATIO_THRESHOLD = 0.45
_SUPER_TALL_MIN_EXTRA_SCALE = 0.55
# Genis-kisa tasarimlar optik olarak biraz asagida gorunebilir; hafif yukari kaydir.
_WIDE_SHORT_RATIO_THRESHOLD = 2.2
_WIDE_SHORT_Y_LIFT_FROM_FREE_SPACE = 0.08
# print_area kutusu varken: onceki (mh - th) * y% formulu kisa tasarimlarda kutunun altina yakinlastiriyordu.
# Dikey konum: kutunun UST kenarina yasla (0), ortala (0.5), alta yaklastir (1).
_PRINT_BOX_VERTICAL_ALIGN_FRAC = 0.0

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# İsteğe bağlı yerleşim: Mockups/placement.json, alt klasör/placement.json,
# veya görsel adıyla yan yana: "Dosya adı.placement.json" (ör. front.png → front.placement.json)
_PLACEMENT_KEYS = frozenset(
    {
        "design_width_percent",
        "design_y_offset_percent",
        "design_x_offset_percent",
        "luminance_threshold",
    }
)


@dataclass
class MockupProcessingConfig:
    mockups_root: Path
    dark_design_path: Path
    light_design_path: Path
    output_root: Path
    luminance_threshold: float = 145.0
    design_width_percent: float = 45.0
    design_y_offset_percent: float = 40.0
    design_x_offset_percent: float = 50.0
    auto_fit_enabled: bool = True
    ratio_tall_threshold: float = 0.80
    ratio_wide_threshold: float = 1.60
    ratio_scale_tall: float = 0.85
    ratio_scale_normal: float = 1.00
    ratio_scale_wide: float = 1.10


@dataclass(frozen=True)
class ResolvedPlacement:
    design_width_percent: float
    design_y_offset_percent: float
    design_x_offset_percent: float
    luminance_threshold: float
    print_area_left_px: Optional[float] = None
    print_area_right_px: Optional[float] = None
    print_area_top_px: Optional[float] = None
    print_area_bottom_px: Optional[float] = None
    force_design: Optional[str] = None


def calculate_luminance(image: Image.Image) -> float:
    rgb = image.convert("RGB")
    stat = ImageStat.Stat(rgb)
    r, g, b = stat.mean
    return (0.2126 * r) + (0.7152 * g) + (0.0722 * b)


def _read_placement_file(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if k not in _PLACEMENT_KEYS:
            continue
        if v is None:
            continue
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _float_or_none(v: object) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _placement_from_corners(
    corners: dict[str, object], mockup_w: int, mockup_h: int
) -> dict[str, float]:
    tl = corners.get("top_left")
    tr = corners.get("top_right")
    bl = corners.get("bottom_left")
    if not isinstance(tl, dict) or not isinstance(tr, dict) or not isinstance(bl, dict):
        return {}
    left = _float_or_none(tl.get("x"))
    top = _float_or_none(tl.get("y"))
    right = _float_or_none(tr.get("x"))
    bottom = _float_or_none(bl.get("y"))
    if left is None or top is None or right is None or bottom is None:
        return {}
    box_w = max(1.0, right - left)
    box_h = max(1.0, bottom - top)
    width_percent = (box_w / float(max(1, mockup_w))) * 100.0
    # X/Y yüzde tanımları compose_mockup içindeki yerleşim matematiğiyle uyumludur.
    x_denom = max(1.0, float(mockup_w) - box_w)
    y_denom = max(1.0, float(mockup_h) - box_h)
    x_offset_percent = (left / x_denom) * 100.0
    y_offset_percent = (top / y_denom) * 100.0
    return {
        "design_width_percent": max(1.0, min(95.0, width_percent)),
        "design_x_offset_percent": max(0.0, min(100.0, x_offset_percent)),
        "design_y_offset_percent": max(0.0, min(100.0, y_offset_percent)),
        "_print_area_left_px": max(0.0, left),
        "_print_area_right_px": max(0.0, right),
        "_print_area_top_px": max(0.0, top),
        "_print_area_bottom_px": max(0.0, bottom),
    }


def _read_root_template_placement(root_cfg: Path, root: Path, mockup_path: Path) -> dict[str, Any]:
    if not root_cfg.is_file():
        return {}
    try:
        raw = json.loads(root_cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    templates = raw.get("templates")
    if not isinstance(templates, dict):
        return {}
    try:
        rel = mockup_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return {}
    node = templates.get(rel)
    if not isinstance(node, dict):
        return {}
    corners = node.get("print_area_corners_px")
    if not isinstance(corners, dict):
        return {}
    try:
        with Image.open(mockup_path) as im:
            mw, mh = im.size
    except Exception:
        return {}
    out: dict[str, Any] = _placement_from_corners(corners, mw, mh)
    design_raw = str(node.get("design") or "").strip().lower()
    if design_raw in {"white", "black"}:
        out["_force_design"] = design_raw
    return out


def _merge_placement(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update(overlay)
    return merged


def resolve_placement(config: MockupProcessingConfig, mockup_path: Path) -> ResolvedPlacement:
    """
    Öncelik (sonrakiler öncekilerin üzerine yazar):
    1) Web formu / MockupProcessingConfig varsayılanları
    2) mockups_root/placement.json
    3) mockups_root → dosyanın klasörüne kadar her alt klasörde placement.json
    4) Aynı klasörde <dosya_adı>.placement.json (örn. live-front.png → live-front.placement.json)
    """
    root = config.mockups_root.resolve()
    mp = mockup_path.resolve()
    merged: dict[str, Any] = {
        "design_width_percent": float(config.design_width_percent),
        "design_y_offset_percent": float(config.design_y_offset_percent),
        "design_x_offset_percent": float(config.design_x_offset_percent),
        "luminance_threshold": float(config.luminance_threshold),
    }
    root_cfg = root / "placement.json"
    merged = _merge_placement(merged, _read_placement_file(root_cfg))
    # Global placement.json içindeki templates[relpath].print_area_corners_px desteği.
    merged = _merge_placement(merged, _read_root_template_placement(root_cfg, root, mp))
    try:
        rel_parent = mp.parent.relative_to(root)
    except ValueError:
        rel_parent = Path()
    parts = [] if rel_parent == Path(".") else list(rel_parent.parts)
    acc = root
    for part in parts:
        acc = acc / part
        merged = _merge_placement(merged, _read_placement_file(acc / "placement.json"))
    side = mp.parent / f"{mp.stem}.placement.json"
    merged = _merge_placement(merged, _read_placement_file(side))
    return ResolvedPlacement(
        design_width_percent=float(merged["design_width_percent"]),
        design_y_offset_percent=float(merged["design_y_offset_percent"]),
        design_x_offset_percent=float(merged.get("design_x_offset_percent", 50.0)),
        luminance_threshold=float(merged["luminance_threshold"]),
        print_area_left_px=merged.get("_print_area_left_px"),
        print_area_right_px=merged.get("_print_area_right_px"),
        print_area_top_px=merged.get("_print_area_top_px"),
        print_area_bottom_px=merged.get("_print_area_bottom_px"),
        force_design=str(merged.get("_force_design") or "").strip().lower() or None,
    )


def pick_design_for_mockup(
    *,
    config: MockupProcessingConfig,
    placement: ResolvedPlacement,
    luminance: float,
) -> Path:
    forced = (placement.force_design or "").strip().lower()
    if forced == "white":
        return config.light_design_path
    if forced == "black":
        return config.dark_design_path
    return (
        config.light_design_path
        if luminance < placement.luminance_threshold
        else config.dark_design_path
    )


def collect_mockup_images(root: Path) -> list[Path]:
    files: list[Path] = []
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        files.append(file_path)
    return files


def design_ratio_scale(
    design_size: tuple[int, int],
    *,
    tall_threshold: float,
    wide_threshold: float,
    scale_tall: float,
    scale_normal: float,
    scale_wide: float,
) -> float:
    w, h = design_size
    if w <= 0 or h <= 0:
        return scale_normal
    ratio = float(w) / float(h)
    if ratio < tall_threshold:
        return scale_tall
    if ratio > wide_threshold:
        return scale_wide
    return scale_normal


def auto_fit_width_percent(
    base_width_percent: float,
    selected_design_path: Path,
    cfg: MockupProcessingConfig,
) -> float:
    if not cfg.auto_fit_enabled:
        return base_width_percent
    try:
        with Image.open(selected_design_path) as d:
            size = d.size
    except Exception:
        return base_width_percent
    scale = design_ratio_scale(
        size,
        tall_threshold=float(cfg.ratio_tall_threshold),
        wide_threshold=float(cfg.ratio_wide_threshold),
        scale_tall=float(cfg.ratio_scale_tall),
        scale_normal=float(cfg.ratio_scale_normal),
        scale_wide=float(cfg.ratio_scale_wide),
    )
    w, h = size
    if w > 0 and h > 0:
        ratio = float(w) / float(h)
        if ratio < _SUPER_TALL_RATIO_THRESHOLD:
            extra_scale = max(
                _SUPER_TALL_MIN_EXTRA_SCALE, ratio / _SUPER_TALL_RATIO_THRESHOLD
            )
            scale *= extra_scale
    out = float(base_width_percent) * float(scale)
    return max(5.0, min(95.0, out))


def compose_mockup(
    *,
    mockup_path: Path,
    selected_design_path: Path,
    output_path: Path,
    design_width_percent: float,
    design_y_offset_percent: float,
    design_x_offset_percent: float = 50.0,
    print_area_left_px: Optional[float] = None,
    print_area_right_px: Optional[float] = None,
    print_area_top_px: Optional[float] = None,
    print_area_bottom_px: Optional[float] = None,
) -> None:
    with Image.open(mockup_path).convert("RGBA") as mockup:
        with Image.open(selected_design_path).convert("RGBA") as design:
            design_ratio = (
                float(design.width) / float(design.height)
                if design.width > 0 and design.height > 0
                else 1.0
            )
            mw, mh = mockup.width, mockup.height
            dw, dh = design.width, design.height
            if dw <= 0 or dh <= 0:
                return

            scale_ratio = (mw * (design_width_percent / 100.0)) / float(dw)
            has_box = (
                print_area_left_px is not None
                and print_area_right_px is not None
                and print_area_top_px is not None
                and print_area_bottom_px is not None
            )
            if has_box:
                left = float(print_area_left_px or 0.0)
                right = float(print_area_right_px or 0.0)
                top = float(print_area_top_px or 0.0)
                bottom = float(print_area_bottom_px or 0.0)
                box_w = max(1.0, right - left)
                box_h = max(1.0, bottom - top)
                tw_f = dw * scale_ratio
                th_f = dh * scale_ratio
                if tw_f > 0 and th_f > 0:
                    fit = min(1.0, box_w / tw_f, box_h / th_f)
                    scale_ratio *= float(fit)

            target_width = max(1, int(round(dw * scale_ratio)))
            target_height = max(1, int(round(dh * scale_ratio)))
            if has_box:
                left = float(print_area_left_px or 0.0)
                right = float(print_area_right_px or 0.0)
                top = float(print_area_top_px or 0.0)
                bottom = float(print_area_bottom_px or 0.0)
                box_w_i = max(1, int(round(right - left)))
                box_h_i = max(1, int(round(bottom - top)))
                if target_width > box_w_i or target_height > box_h_i:
                    fit2 = min(box_w_i / float(target_width), box_h_i / float(target_height))
                    target_width = max(1, int(round(target_width * fit2)))
                    target_height = max(1, int(round(target_height * fit2)))
                for _ in range(6):
                    if target_width <= box_w_i and target_height <= box_h_i:
                        break
                    fit3 = min(
                        box_w_i / float(max(1, target_width)),
                        box_h_i / float(max(1, target_height)),
                    )
                    target_width = max(1, int(target_width * fit3))
                    target_height = max(1, int(target_height * fit3))

            resized = design.resize((target_width, target_height), Image.Resampling.LANCZOS)
            # Hafif transparan baski: mockup dokusunun daha dogal gorunmesini saglar.
            alpha = resized.getchannel("A")
            alpha = alpha.point(lambda p: int(p * _DESIGN_OPACITY_MULTIPLIER))
            resized.putalpha(alpha)

            x = int((mw - target_width) * (design_x_offset_percent / 100.0))
            y = int((mh - target_height) * (design_y_offset_percent / 100.0))
            if design_ratio > _WIDE_SHORT_RATIO_THRESHOLD:
                free_space_h = max(0, mh - target_height)
                y -= int(free_space_h * _WIDE_SHORT_Y_LIFT_FROM_FREE_SPACE)

            if has_box:
                left = int(max(0.0, float(print_area_left_px or 0.0)))
                right = int(max(0.0, float(print_area_right_px or 0.0)))
                top = int(max(0.0, float(print_area_top_px or 0.0)))
                bottom = int(max(0.0, float(print_area_bottom_px or 0.0)))
                max_x = right - target_width
                max_y = bottom - target_height
                if max_x >= left:
                    x = max(left, min(x, max_x))
                else:
                    x = left
                if max_y >= top:
                    slack_y = float(max_y - top)
                    y = top + int(
                        round(slack_y * float(_PRINT_BOX_VERTICAL_ALIGN_FRAC))
                    )
                    y = max(top, min(y, max_y))
                else:
                    y = top
            else:
                x = max(0, min(x, mw - target_width))
                if print_area_top_px is not None:
                    min_y = int(max(0.0, print_area_top_px))
                    if print_area_bottom_px is not None:
                        max_y_from_area = int(
                            max(0.0, float(print_area_bottom_px)) - target_height
                        )
                        if max_y_from_area >= min_y:
                            y = max(min_y, min(y, max_y_from_area))
                        else:
                            y = min_y
                    else:
                        y = max(y, min_y)
                y = max(0, min(y, mh - target_height))

            output = mockup.copy()
            output.alpha_composite(resized, (x, y))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output.save(output_path)


def process_all(
    config: MockupProcessingConfig,
    log_callback: Optional[Callable[[str], None]] = None,
) -> tuple[list[Path], int]:
    images = collect_mockup_images(config.mockups_root)
    if not images:
        raise ValueError("Mockups klasöründe desteklenen görsel bulunamadı.")

    out_paths: list[Path] = []
    failed = 0
    for mockup in images:
        rel = mockup.relative_to(config.mockups_root)
        out_path = config.output_root / rel.with_suffix(".png")
        try:
            placement = resolve_placement(config, mockup)
            with Image.open(mockup) as preview:
                lum = calculate_luminance(preview)
            design_to_use = pick_design_for_mockup(
                config=config, placement=placement, luminance=lum
            )
            width_percent = auto_fit_width_percent(
                placement.design_width_percent, design_to_use, config
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
                log_callback(f"OK {rel}")
        except Exception as exc:
            failed += 1
            if log_callback:
                log_callback(f"FAIL {rel}: {exc}")
    return out_paths, failed
