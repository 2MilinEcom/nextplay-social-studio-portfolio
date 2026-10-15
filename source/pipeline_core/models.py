from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Product:
    source_url: str
    product_id: str
    title: str
    brand: str = ""
    description: str = ""
    image_urls: list[str] = field(default_factory=list)
    primary_image_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Product":
        return cls(**data)


@dataclass
class SearchPlan:
    queries: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    relevance_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchPlan":
        return cls(**data)


@dataclass
class VideoCandidate:
    url: str
    video_id: str
    creator: str = ""
    caption: str = ""
    thumbnail_url: str = ""
    discovered_by: list[str] = field(default_factory=list)
    text_score: float = 0.0
    visual_raw_score: float | None = None
    visual_score: float | None = None
    combined_score: float = 0.0
    download_status: str = "pending"
    local_path: str | None = None
    sha256: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VideoCandidate":
        return cls(**data)
