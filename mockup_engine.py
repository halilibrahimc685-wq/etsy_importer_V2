from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageStat

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


@dataclass(frozen=True)
class ResolvedPlacement:
    design_width_percent: float
    design_y_offset_percent: float
    design_x_offset_percent: float
    luminance_threshold: float


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


def _merge_placement(base: dict[str, float], overlay: dict[str, float]) -> dict[str, float]:
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
    merged: dict[str, float] = {
        "design_width_percent": float(config.design_width_percent),
        "design_y_offset_percent": float(config.design_y_offset_percent),
        "design_x_offset_percent": float(config.design_x_offset_percent),
        "luminance_threshold": float(config.luminance_threshold),
    }
    root_cfg = root / "placement.json"
    merged = _merge_placement(merged, _read_placement_file(root_cfg))
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


def compose_mockup(
    *,
    mockup_path: Path,
    selected_design_path: Path,
    output_path: Path,
    design_width_percent: float,
    design_y_offset_percent: float,
    design_x_offset_percent: float = 50.0,
) -> None:
    with Image.open(mockup_path).convert("RGBA") as mockup:
        with Image.open(selected_design_path).convert("RGBA") as design:
            target_width = int(mockup.width * (design_width_percent / 100.0))
            scale_ratio = target_width / design.width
            target_height = int(design.height * scale_ratio)
            resized = design.resize((target_width, target_height), Image.Resampling.LANCZOS)

            x = int((mockup.width - target_width) * (design_x_offset_percent / 100.0))
            x = max(0, min(x, mockup.width - target_width))
            y = int((mockup.height - target_height) * (design_y_offset_percent / 100.0))
            y = max(0, min(y, mockup.height - target_height))

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
            design_to_use = (
                config.light_design_path
                if lum < placement.luminance_threshold
                else config.dark_design_path
            )
            compose_mockup(
                mockup_path=mockup,
                selected_design_path=design_to_use,
                output_path=out_path,
                design_width_percent=placement.design_width_percent,
                design_y_offset_percent=placement.design_y_offset_percent,
                design_x_offset_percent=placement.design_x_offset_percent,
            )
            out_paths.append(out_path)
            if log_callback:
                log_callback(f"OK {rel}")
        except Exception as exc:
            failed += 1
            if log_callback:
                log_callback(f"FAIL {rel}: {exc}")
    return out_paths, failed
