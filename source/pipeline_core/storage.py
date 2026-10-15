from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .models import Product, SearchPlan, VideoCandidate


def slugify(value: str, max_length: int = 72) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return (value or "product")[:max_length].rstrip("-")


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class ProductWorkspace:
    def __init__(self, root: Path, product: Product):
        suffix = product.product_id[-10:] if product.product_id else "unknown"
        self.root = Path(root) / f"{slugify(product.title)}-{suffix}"
        self.product_json = self.root / "product.json"
        self.search_plan_json = self.root / "search_plan.json"
        self.videos_json = self.root / "videos.json"
        self.discovery_dir = self.root / "discovery"
        self.raw_dir = self.root / "raw"
        self.thumbnails_dir = self.root / "thumbnails"
        self.analysis_dir = self.root / "analysis"
        self.clips_dir = self.root / "clips"
        self.ads_dir = self.root / "ads"
        self.quarantine_dir = self.root / "quarantine"
        self.debug_dir = self.root / "debug"

    def ensure(self) -> "ProductWorkspace":
        for path in (
            self.root,
            self.discovery_dir,
            self.raw_dir,
            self.thumbnails_dir,
            self.analysis_dir,
            self.clips_dir,
            self.ads_dir,
            self.quarantine_dir,
            self.debug_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def save_product(self, product: Product) -> None:
        atomic_write_json(self.product_json, product.to_dict())

    def save_search_plan(self, plan: SearchPlan) -> None:
        atomic_write_json(self.search_plan_json, plan.to_dict())

    def load_search_plan(self) -> SearchPlan:
        return SearchPlan.from_dict(read_json(self.search_plan_json, {}))

    def load_candidates(self) -> list[VideoCandidate]:
        rows = read_json(self.videos_json, []) or []
        return [VideoCandidate.from_dict(row) for row in rows]

    def save_candidates(self, candidates: Iterable[VideoCandidate]) -> None:
        ordered = sorted(candidates, key=lambda c: (-c.combined_score, c.url))
        atomic_write_json(self.videos_json, [c.to_dict() for c in ordered])


def load_product_workspace(path: str | Path) -> tuple[Product, ProductWorkspace]:
    root = Path(path).expanduser().resolve()
    data = read_json(root / "product.json")
    if not data:
        raise FileNotFoundError(f"No product.json found in {root}")
    product = Product.from_dict(data)
    workspace = ProductWorkspace(root.parent, product)
    # Respect an explicitly supplied directory even if naming rules later change.
    workspace.root = root
    workspace.product_json = root / "product.json"
    workspace.search_plan_json = root / "search_plan.json"
    workspace.videos_json = root / "videos.json"
    workspace.discovery_dir = root / "discovery"
    workspace.raw_dir = root / "raw"
    workspace.thumbnails_dir = root / "thumbnails"
    workspace.analysis_dir = root / "analysis"
    workspace.clips_dir = root / "clips"
    workspace.ads_dir = root / "ads"
    workspace.quarantine_dir = root / "quarantine"
    workspace.debug_dir = root / "debug"
    workspace.ensure()
    return product, workspace
