from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RegistryResult:
    """A single result from a registry search."""
    entity_name: str
    registry_id: str | None = None
    status: str | None = None
    jurisdiction: str | None = None
    formation_date: str | None = None
    entity_type: str | None = None
    address: str | None = None
    additional_names: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)


class BaseScraper(ABC):
    """Base class for all registry scrapers."""

    registry_name: str = ""
    jurisdiction: str = ""  # e.g. "US-DE", "CA-ON", "GB"
    needs_browser: bool = False

    @abstractmethod
    async def search(self, entity_name: str) -> list[RegistryResult]:
        """Search the registry for entities matching the given name."""
        ...

    async def get_details(self, entity_id: str) -> RegistryResult | None:
        """Get full details for a specific entity. Optional override."""
        return None
