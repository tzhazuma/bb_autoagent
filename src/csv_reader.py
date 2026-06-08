from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_NAME_ALIASES = {"姓名", "name", "student name", "student_name"}
_ID_ALIASES = {"学号", "studentid", "student_id", "id", "student id", "编号"}
_SCORE_ALIASES = {"分数", "score", "grade", "成绩", "得分", "grades"}


class ScoreRecord(BaseModel):
    name: str
    student_id: str
    score: float
    extra: Optional[dict[str, Any]] = None


class ScoreTable:

    def __init__(self, data: pd.DataFrame):
        self._data = data.copy()

    @classmethod
    def from_csv(
        cls,
        filepath: str,
        name_col: str = "姓名",
        id_col: str = "学号",
        score_col: str = "分数",
    ) -> ScoreTable:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        ext = path.suffix.lower()
        if ext == ".csv":
            df = cls._read_csv(path)
        elif ext in (".xlsx", ".xls"):
            df = cls._read_excel(path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        df = cls._clean_dataframe(df)
        mapping = cls._resolve_columns(df, name_col, id_col, score_col)

        df = df[list(mapping.values())].rename(
            columns={v: k for k, v in mapping.items()}
        )
        df = df.dropna(subset=["name", "student_id"], how="all")
        df = df.reset_index(drop=True)

        return cls(df)

    @staticmethod
    def _read_csv(path: Path) -> pd.DataFrame:
        encodings = ["utf-8-sig", "utf-8", "gb2312", "gbk", "utf-16"]
        for enc in encodings:
            try:
                return pd.read_csv(path, encoding=enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError(
            f"Unable to read CSV file. Tried encodings: {encodings}"
        )

    @staticmethod
    def _read_excel(path: Path) -> pd.DataFrame:
        try:
            return pd.read_excel(path)
        except ImportError as e:
            raise ImportError(
                "Reading Excel files requires openpyxl (for .xlsx) "
                "or xlrd (for .xls). Install with: pip install openpyxl xlrd"
            ) from e

    @staticmethod
    def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].apply(
                lambda x: x.strip() if isinstance(x, str) else x
            )
        df = df.dropna(how="all").dropna(axis=1, how="all")
        return df

    @staticmethod
    def _resolve_columns(
        df: pd.DataFrame, name_col: str, id_col: str, score_col: str
    ) -> dict[str, str]:

        def _find(aliases: set[str], preferred: str) -> str:
            lower_map = {str(c).strip().lower(): c for c in df.columns}
            pref_low = preferred.strip().lower()

            if pref_low in lower_map:
                return lower_map[pref_low]

            for alias in aliases:
                if alias in lower_map:
                    return lower_map[alias]

            for col_lower, col_orig in lower_map.items():
                for alias in aliases:
                    if alias in col_lower or col_lower in alias:
                        return col_orig

            raise ValueError(
                f"Could not find a column matching any of: {aliases | {preferred}}"
            )

        return {
            "name": _find(_NAME_ALIASES, name_col),
            "student_id": _find(_ID_ALIASES, id_col),
            "score": _find(_SCORE_ALIASES, score_col),
        }

    def to_records(self) -> list[ScoreRecord]:
        records: list[ScoreRecord] = []
        for _, row in self._data.iterrows():
            name = str(row.get("name", "") or "").strip()
            student_id = str(row.get("student_id", "") or "").strip()

            raw_score = row.get("score")
            try:
                score = float(raw_score) if pd.notna(raw_score) else 0.0
            except (ValueError, TypeError):
                score = 0.0

            if not name and not student_id:
                continue

            extra: dict[str, Any] = {}
            for k, v in row.to_dict().items():
                if k not in ("name", "student_id", "score") and pd.notna(v):
                    extra[k] = v

            records.append(
                ScoreRecord(
                    name=name,
                    student_id=student_id,
                    score=score,
                    extra=extra or None,
                )
            )
        return records

    def validate(self) -> list[str]:
        errors: list[str] = []
        records = self.to_records()
        if not records:
            errors.append("No valid records found in the table")
            return errors

        for i, rec in enumerate(records):
            if not rec.name:
                errors.append(f"Row {i + 1}: missing name")
            if not rec.student_id:
                errors.append(f"Row {i + 1}: missing student ID")
            if rec.score < 0 or rec.score > 100:
                errors.append(
                    f"Row {i + 1}: score {rec.score} out of range [0, 100]"
                )

        seen: set[str] = set()
        for rec in records:
            if rec.student_id:
                if rec.student_id in seen:
                    errors.append(f"Duplicate student ID: {rec.student_id}")
                seen.add(rec.student_id)

        return errors

    def summary(self) -> dict[str, Any]:
        records = self.to_records()
        if not records:
            return {"count": 0, "avg_score": 0.0, "max_score": 0.0, "min_score": 0.0}

        scores = [r.score for r in records]
        return {
            "count": len(records),
            "avg_score": round(sum(scores) / len(scores), 2),
            "max_score": max(scores),
            "min_score": min(scores),
        }

    @property
    def data(self) -> pd.DataFrame:
        return self._data.copy()

    def __repr__(self) -> str:
        n = len(self._data)
        return f"<ScoreTable with {n} row{'s' if n != 1 else ''}>"
