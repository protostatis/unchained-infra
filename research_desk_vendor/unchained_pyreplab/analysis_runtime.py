from __future__ import annotations
from pathlib import Path
from typing import Any

from .capsule_runtime import load_capsule

try:
    import pandas as pd
except Exception:  # pragma: no cover - optional during bootstrap
    pd = None

SOURCE_TYPE_PRIORITY = {
    "state_report_card": 0,
    "official_site": 1,
    "district_profile": 2,
    "ranking_page": 3,
    "ranking_article": 4,
    "news_article": 5,
    "unknown": 6,
}


def get_dataframe_module() -> Any:
    return pd


def to_dataframe(rows: list[dict[str, Any]]) -> Any:
    if pd is None:
        return rows
    return pd.DataFrame(rows)


def ensure_dataframe(table: Any) -> Any:
    if pd is None:
        return table
    if isinstance(table, pd.DataFrame):
        return table
    if isinstance(table, list):
        if not table:
            return pd.DataFrame()
        if all(isinstance(row, dict) for row in table):
            return pd.DataFrame(table)
        return pd.DataFrame({"value": table})
    if isinstance(table, dict):
        return pd.DataFrame([table])
    return pd.DataFrame({"value": [table]})


def table_row_count(table: Any) -> int:
    if pd is not None and isinstance(table, pd.DataFrame):
        return int(table.shape[0])
    if hasattr(table, "__len__"):
        return len(table)
    return 0


def table_columns(table: Any, limit: int = 12) -> list[str]:
    if pd is not None and isinstance(table, pd.DataFrame):
        return [str(column) for column in list(table.columns)[:limit]]
    seen: list[str] = []
    if isinstance(table, list):
        for row in table[:20]:
            if not isinstance(row, dict):
                continue
            for key in row.keys():
                if key not in seen:
                    seen.append(key)
    elif isinstance(table, dict):
        seen.extend(str(key) for key in list(table.keys())[:limit])
    return seen[:limit]


def table_records(table: Any) -> list[dict[str, Any]]:
    if pd is not None and isinstance(table, pd.DataFrame):
        if table.empty:
            return []
        return table.to_dict(orient="records")
    if isinstance(table, list):
        return [row for row in table if isinstance(row, dict)]
    if isinstance(table, dict):
        return [table]
    return []


def _format_markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    if pd is not None:
        try:
            if pd.isna(value):  # type: ignore[arg-type]
                return ""
        except Exception:
            pass
    if isinstance(value, float):
        return ("{value:.2f}".format(value=value)).rstrip("0").rstrip(".")
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text.replace("|", "\\|")


def _dataframe_to_markdown(frame: Any) -> str:
    if pd is None or not isinstance(frame, pd.DataFrame):
        return str(frame)
    columns = [str(column) for column in frame.columns]
    if not columns:
        return "(0 columns)"
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body_lines: list[str] = []
    for row in frame.to_dict(orient="records"):
        values = [_format_markdown_cell(row.get(column)) for column in columns]
        body_lines.append("| " + " | ".join(values) + " |")
    return "\n".join([header, divider, *body_lines])


def format_dataframe(
    table: Any,
    *,
    limit: int = 20,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    ascending: bool = False,
) -> str:
    frame = ensure_dataframe(table)
    if pd is None or not isinstance(frame, pd.DataFrame):
        return str(frame)
    if columns:
        selected = [column for column in columns if column in frame.columns]
        if selected:
            frame = frame.loc[:, selected]
    if sort_by and sort_by in frame.columns:
        frame = frame.sort_values(sort_by, ascending=ascending, na_position="last")
    preview = frame.head(limit)
    if preview.empty:
        return "(0 rows)"
    body = _dataframe_to_markdown(preview)
    more_rows = max(int(frame.shape[0]) - int(preview.shape[0]), 0)
    if more_rows:
        return "rows={rows} shown={shown}\n{body}\n... {more} more rows".format(
            rows=int(frame.shape[0]),
            shown=int(preview.shape[0]),
            body=body,
            more=more_rows,
        )
    return "rows={rows}\n{body}".format(rows=int(frame.shape[0]), body=body)


def load_analysis_context(capsule_dir: Path) -> dict[str, Any]:
    capsule_path = Path(capsule_dir)
    capsule = load_capsule(capsule_path)
    task_spec = capsule.task_spec()
    source_plan = capsule.source_plan()
    row_schema = capsule.row_schema()
    schema_refinement = capsule.schema_refinement()
    object_manifest = capsule.object_manifest()
    readiness = capsule.readiness()
    gather_qa = capsule.gather_qa()
    gather_qa_review = capsule.gather_qa_review()
    capsule_state = capsule.capsule_state()
    analysis_plan = capsule.analysis_plan()
    source_index = capsule.source_index()
    schema_summary = capsule.schema_summary()
    source_rows = list(source_index.get("sources", []))
    entity_rows = list(schema_summary.get("entities", []))
    return {
        "capsule": capsule,
        "task_spec": task_spec,
        "source_plan": source_plan,
        "row_schema": row_schema,
        "schema_refinement": schema_refinement,
        "object_manifest": object_manifest,
        "readiness": readiness,
        "gather_qa": gather_qa,
        "gather_qa_review": gather_qa_review,
        "capsule_state": capsule_state,
        "analysis_plan": analysis_plan,
        "source_index": source_index,
        "schema_summary": schema_summary,
        "source_rows": source_rows,
        "entity_rows": entity_rows,
        "source_df": to_dataframe(source_rows),
        "entity_df": to_dataframe(entity_rows),
        "ranking_policy": dict(analysis_plan.get("ranking_policy") or {}),
        "summary": capsule.summary(),
    }


def build_district_metrics(
    entity_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entity in entity_rows:
        relevant_sources = [
            source
            for source in source_rows
            if source.get("entity_name") == entity.get("entity_name")
        ]
        relevant_sources = sorted(
            relevant_sources,
            key=lambda source: SOURCE_TYPE_PRIORITY.get(source.get("source_type", "unknown"), 99),
        )
        row = {
            "entity_name": entity.get("entity_name", ""),
            "comparability_flag": entity.get("comparability_flag", "unknown"),
            "source_page_ids": entity.get("source_page_ids", []),
            "source_types": entity.get("source_types", []),
            "metric_provenance": {},
        }
        for source in relevant_sources:
            for key, value in source.get("extracted_metrics", {}).items():
                if value in (None, "", []):
                    continue
                if key not in row:
                    row[key] = value
                    row["metric_provenance"][key] = source.get("page_id", "")
        rows.append(row)
    return rows
