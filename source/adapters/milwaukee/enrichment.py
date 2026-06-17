"""Enrich Milwaukee permits with MPROP property owner data."""

from __future__ import annotations

import re
import time

import pandas as pd
import requests

from source.adapters.milwaukee.constants import MPROP_API_URL, MPROP_RESOURCE_ID

MPROP_FIELDS = (
    "TAXKEY,HOUSE_NR_LO,HOUSE_NR_HI,SDIR,STREET,STTYPE,OWNER_NAME_1,OWNER_NAME_2,"
    "OWNER_MAIL_ADDR,OWNER_CITY_STATE,OWNER_ZIP,C_A_TOTAL,C_A_LAND,C_A_IMPRV,ZONING,"
    "LAND_USE,LAND_USE_GP,BLDG_TYPE,NR_STORIES,NR_UNITS,YR_BUILT,GEO_ZIP_CODE,"
    "GEO_ALDER,NEIGHBORHOOD"
)


def fetch_mprop_index(timeout: int = 30) -> dict[str, pd.Series]:
    """Download MPROP records and build an address lookup index."""
    records: list[dict] = []
    offset = 0
    while True:
        response = requests.get(
            MPROP_API_URL,
            params={
                "resource_id": MPROP_RESOURCE_ID,
                "limit": 5000,
                "offset": offset,
                "fields": MPROP_FIELDS,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        batch = response.json().get("result", {}).get("records") or []
        if not batch:
            break
        records.extend(batch)
        offset += len(batch)
        if len(batch) < 5000:
            break
        time.sleep(0.2)

    mprop_df = pd.DataFrame(records)
    mprop_df["MATCH_ADDR"] = mprop_df.apply(
        lambda row: _normalize_mprop_address(
            row.get("HOUSE_NR_LO"), row.get("SDIR"), row.get("STREET"), row.get("STTYPE")
        ),
        axis=1,
    )
    return {
        row["MATCH_ADDR"]: row
        for _, row in mprop_df.iterrows()
        if row["MATCH_ADDR"]
    }


def enrich_permits(df: pd.DataFrame, mprop_index: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    """Match permit addresses to MPROP and attach owner/property fields."""
    index = mprop_index if mprop_index is not None else fetch_mprop_index()
    enriched = df.copy()
    matches = []

    for _, row in enriched.iterrows():
        norm = normalize_permit_address(row.get("Address", ""))
        mprop_row = index.get(norm)
        if mprop_row is None:
            matches.append({})
            continue
        owner = " / ".join(filter(None, [
            str(mprop_row.get("OWNER_NAME_1") or "").strip(),
            str(mprop_row.get("OWNER_NAME_2") or "").strip(),
        ])).strip(" /")
        matches.append({
            "TAXKEY": str(mprop_row.get("TAXKEY", "")),
            "Owner Name": owner,
            "Assessed Value": mprop_row.get("C_A_TOTAL", ""),
            "Zoning": mprop_row.get("ZONING", ""),
            "Land Use": mprop_row.get("LAND_USE", ""),
            "Building Type": mprop_row.get("BLDG_TYPE", ""),
            "Year Built": mprop_row.get("YR_BUILT", ""),
        })

    match_df = pd.DataFrame(matches)
    for col in match_df.columns:
        enriched[col] = match_df[col]

    front_cols = [
        "City", "State", "Address", "TAXKEY", "Owner Name", "Assessed Value",
        "Zoning", "Land Use", "Building Type", "Year Built",
    ]
    other_cols = [c for c in enriched.columns if c not in front_cols]
    return enriched[front_cols + other_cols].sort_values(
        ["Address", "Date Issued"], ascending=[True, False]
    ).reset_index(drop=True)


def dedupe_by_address(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the most recent permit per unique address."""
    return df.drop_duplicates(subset=["Address"], keep="first").reset_index(drop=True)


def normalize_permit_address(addr: str) -> str:
    """Normalize a permit address to match MPROP format."""
    if not addr or str(addr) == "nan":
        return ""
    cleaned = re.sub(r",?\s*MILWAUKEE.*$", "", str(addr), flags=re.I).strip().upper()
    replacements = {
        r"\bAVENUE\b": "AVE", r"\bBOULEVARD\b": "BLVD", r"\bSTREET\b": "ST",
        r"\bROAD\b": "RD", r"\bDRIVE\b": "DR", r"\bLANE\b": "LN",
        r"\bCOURT\b": "CT", r"\bPLACE\b": "PL", r"\bCIRCLE\b": "CIR",
        r"\bHIGHWAY\b": "HWY",
    }
    for pattern, repl in replacements.items():
        cleaned = re.sub(pattern, repl, cleaned)
    return re.sub(
        r",?\s*#?\s*(STE|SUITE|UNIT|APT|FL|FLOOR)\.?\s*\w*",
        "",
        cleaned,
        flags=re.I,
    ).strip()


def _normalize_mprop_address(house_nr, sdir, street, sttype) -> str:
    parts = [
        str(int(float(house_nr))) if house_nr and str(house_nr) not in ("", "None", "0") else "",
        str(sdir or "").strip().upper(),
        str(street or "").strip().upper(),
        str(sttype or "").strip().upper(),
    ]
    return " ".join(part for part in parts if part).strip()
