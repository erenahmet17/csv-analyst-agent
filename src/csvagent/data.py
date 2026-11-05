"""Loading a CSV and describing its columns."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Integer columns with this few distinct values are treated as categories
# (e.g. a 0/1 target, a 1-4 chest pain type).
MAX_CATEGORY_LEVELS = 10


def load_csv(path: str | Path) -> pd.DataFrame:
    """Read a CSV, guessing the delimiter (',' ';' '\\t' '|') and falling back on encoding."""
    path = Path(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        sample = f.read(20_000)
    try:
        sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        sep = ","
    for enc in ("utf-8", "utf-8-sig", "cp1254", "latin-1"):  # cp1254 = Turkish Windows
        try:
            return pd.read_csv(path, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"could not decode {path}")


@dataclass
class Dataset:
    df: pd.DataFrame
    name: str = "data"

    @classmethod
    def from_csv(cls, path: str | Path) -> Dataset:
        return cls(load_csv(path), Path(path).stem)

    @property
    def columns(self) -> list[str]:
        return [str(c) for c in self.df.columns]

    def kind(self, column: str) -> str:
        """'numeric', 'categorical' or 'text' (high-cardinality strings, e.g. IDs)."""
        s = self.df[column]
        if pd.api.types.is_bool_dtype(s):
            return "categorical"
        if pd.api.types.is_numeric_dtype(s):
            return "categorical" if s.nunique(dropna=True) <= MAX_CATEGORY_LEVELS else "numeric"
        if s.nunique(dropna=True) <= max(30, int(0.05 * len(s))):
            return "categorical"
        return "text"

    def numeric_columns(self) -> list[str]:
        return [c for c in self.columns if pd.api.types.is_numeric_dtype(self.df[c])]

    def require(self, *columns: str | None) -> None:
        missing = [c for c in columns if c is not None and c not in self.df.columns]
        if missing:
            raise KeyError(f"unknown column(s) {missing}; available: {self.columns}")
