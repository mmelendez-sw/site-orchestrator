"""Registered permit source adapters."""

from __future__ import annotations

from source.adapters.file import FileSource
from source.adapters.milwaukee import MilwaukeeSource
from source.base import BaseSourceAdapter
from source.scope import SourceScope

ADAPTERS: dict[str, type[BaseSourceAdapter]] = {
    MilwaukeeSource.name: MilwaukeeSource,
    FileSource.name: FileSource,
}


def get_adapter(name: str) -> BaseSourceAdapter:
    try:
        adapter_cls = ADAPTERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"Unknown source '{name}'. Available: {known}") from exc
    return adapter_cls()


def adapters_for_scope(scope: SourceScope | None) -> list[str]:
    """Return adapter names compatible with a geographic scope."""
    return sorted(
        name for name, adapter_cls in ADAPTERS.items()
        if adapter_cls().supports_scope(scope)
    )
