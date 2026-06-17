"""Base interface for permit source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from source.record import SourceRecord


class BaseSourceAdapter(ABC):
    """Pull candidate sites from an open-data or gov permit source."""

    name: str
    description: str

    @abstractmethod
    def fetch(self, **kwargs: Any) -> list[SourceRecord]:
        """Return candidate site records from the upstream data source."""
