"""High-conf Gemini tower stamp so Salesforce apply can skip Claude."""

from __future__ import annotations

from typing import Any


def stamp_gemini_tower_solo(res: dict[str, Any]) -> dict[str, Any]:
    """Mark a high-conf Gemini tower so SF apply can write without Claude."""
    from classifier import asset_classifier as ac

    ac.normalize_model_result(res)
    if ac.should_skip_claude_for_gemini_tower(res):
        res["claude_cell_equipment"] = None
        res["cell_models_agree"] = True
        res["dual_model_resolution"] = "gemini_strong_solo"
        res["gemini_cell_equipment"] = res.get("cell_equipment")
    return res
