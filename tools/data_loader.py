"""Data-loading tools (Concept 1). Thin, cached pandas readers over the CSVs.

Supports runtime path overrides so the audit can run against user-uploaded
datasets instead of the built-in demo data.
"""
from __future__ import annotations

import functools
import os

import pandas as pd

import config

# ---- Runtime path overrides (None = use defaults from config) ----
_overrides: dict[str, str | None] = {
    "contracts": None,
    "invoices": None,
    "usage": None,
}


def override_paths(contracts_path: str, invoices_path: str, usage_path: str) -> None:
    """Point the data loaders at user-uploaded CSV files."""
    _overrides["contracts"] = contracts_path
    _overrides["invoices"] = invoices_path
    _overrides["usage"] = usage_path
    clear_cache()


def reset_overrides() -> None:
    """Revert to the built-in demo dataset."""
    _overrides["contracts"] = None
    _overrides["invoices"] = None
    _overrides["usage"] = None
    clear_cache()


def is_custom_dataset() -> bool:
    """Return True if a user-uploaded dataset is active."""
    return _overrides["contracts"] is not None


def _contracts_path() -> str:
    return _overrides["contracts"] or config.CONTRACTS_CSV


def _invoices_path() -> str:
    return _overrides["invoices"] or config.INVOICES_CSV


def _usage_path() -> str:
    return _overrides["usage"] or config.USAGE_CSV


@functools.lru_cache(maxsize=1)
def _contracts() -> pd.DataFrame:
    df = pd.read_csv(_contracts_path(), dtype={"customer_id": str, "contract_id": str})
    df["discount_expiry_date"] = df["discount_expiry_date"].fillna("")
    df["notes"] = df["notes"].fillna("") if "notes" in df.columns else ""
    return df


@functools.lru_cache(maxsize=1)
def _invoices() -> pd.DataFrame:
    return pd.read_csv(_invoices_path(), dtype={"customer_id": str, "contract_id": str})


@functools.lru_cache(maxsize=1)
def _usage() -> pd.DataFrame:
    return pd.read_csv(_usage_path(), dtype={"customer_id": str})


def clear_cache() -> None:
    _contracts.cache_clear()
    _invoices.cache_clear()
    _usage.cache_clear()


def load_contracts(customer_id: str | None = None) -> pd.DataFrame:
    df = _contracts()
    return df[df["customer_id"] == customer_id].copy() if customer_id else df.copy()


def load_invoices(customer_id: str | None = None) -> pd.DataFrame:
    df = _invoices()
    return df[df["customer_id"] == customer_id].copy() if customer_id else df.copy()


def load_usage(customer_id: str | None = None) -> pd.DataFrame:
    df = _usage()
    return df[df["customer_id"] == customer_id].copy() if customer_id else df.copy()


def all_customer_ids() -> list[str]:
    return sorted(_contracts()["customer_id"].unique().tolist())
