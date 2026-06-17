"""Download and filter Milwaukee telecom building permits."""

from __future__ import annotations

import io

import pandas as pd
import requests

from source.adapters.milwaukee.constants import PERMIT_CSV_URL, TELECOM_KEYWORDS


def fetch_telecom_permits(timeout: int = 60) -> pd.DataFrame:
    """Download Milwaukee permits and filter to telecom-related records."""
    response = requests.get(PERMIT_CSV_URL, timeout=timeout)
    response.raise_for_status()
    df_raw = pd.read_csv(io.StringIO(response.text), low_memory=False)

    mask = pd.Series([False] * len(df_raw))
    for keyword in TELECOM_KEYWORDS:
        for col in ("Permit Type", "Use of Building"):
            mask |= df_raw[col].astype(str).str.upper().str.contains(
                keyword.upper(), na=False
            )

    df = df_raw[mask].copy().reset_index(drop=True)
    df["Telecom Keywords"] = df.apply(_find_keywords, axis=1)
    df["Address Clean"] = df["Address"].astype(str).str.replace(
        r",?\s*MILWAUKEE,?\s*WI\s*\d+", "", regex=True
    ).str.strip()
    df.insert(0, "City", "Milwaukee")
    df.insert(1, "State", "WI")
    df = df.rename(columns={
        "Address": "Full Address",
        "Address Clean": "Address",
        "Record ID": "Permit #",
        "Use of Building": "Work Description",
        "Construction Total Cost": "Construction Cost",
        "Dwelling units impact": "Dwelling Units Impact",
    })
    df["Date Issued"] = pd.to_datetime(df["Date Issued"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["Date Opened"] = pd.to_datetime(df["Date Opened"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.sort_values(["Address", "Date Issued"], ascending=[True, False]).reset_index(drop=True)


def _find_keywords(row: pd.Series) -> str:
    text = " ".join(str(v) for v in row.values if v and str(v) != "nan").lower()
    matched = [kw for kw in TELECOM_KEYWORDS if kw.lower() in text]
    return ", ".join(dict.fromkeys(matched))
