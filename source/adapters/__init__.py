"""Registered permit source adapters."""

from __future__ import annotations

from source.adapters.milwaukee import MilwaukeeSource

from source.base import BaseSourceAdapter

ADAPTERS: dict[str, type[BaseSourceAdapter]] = {
    MilwaukeeSource.name: MilwaukeeSource,
}


def get_adapter(name: str) -> BaseSourceAdapter:
    try:
        adapter_cls = ADAPTERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"Unknown source '{name}'. Available: {known}") from exc
    return adapter_cls()
