from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse

try:
    import pandas as _pd
except Exception:  # pragma: no cover - optional dependency
    _pd = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


_VALID_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _ensure_within_root(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError(f"Path escapes capsule root: {path}")
    return resolved_path


def _validated_capsule_relpath(root: Path, relative: str, *, expected_prefix: str) -> Path:
    rel = Path(str(relative or "").strip())
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Invalid capsule relative path: {relative}")
    if rel.parts[0] != expected_prefix or rel.suffix != ".jsonl":
        raise ValueError(f"Unexpected capsule table path: {relative}")
    return _ensure_within_root(root / rel, root)


def _validate_table_name(name: str) -> str:
    clean = str(name or "").strip()
    if not clean or not _VALID_TABLE_NAME_RE.fullmatch(clean):
        raise ValueError(f"Invalid table name: {name}")
    return clean


def _validate_followup_url(url: str) -> str:
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"Invalid followup URL: {url}")
    return clean


def _append_jsonl(path: Path, payload: dict[str, Any], *, root: Optional[Path] = None) -> None:
    if root is not None:
        _ensure_within_root(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _to_table(rows: list[dict[str, Any]]) -> Any:
    if _pd is not None:
        return _pd.DataFrame(rows)
    return rows


@dataclass
class Capsule:
    path: Path

    @property
    def manifest(self) -> dict[str, Any]:
        return _read_json(self.path / "manifest.json", {})

    def task_spec(self) -> dict[str, Any]:
        return _read_json(self.path / "task_spec.json", {})

    def source_plan(self) -> dict[str, Any]:
        return _read_json(self.path / "source_plan.json", {})

    def row_schema(self) -> dict[str, Any]:
        return _read_json(self.path / "row_schema.json", {})

    def schema_refinement(self) -> dict[str, Any]:
        return _read_json(self.path / "schema_refinement.json", {})

    def object_manifest(self) -> dict[str, Any]:
        return _read_json(self.path / "object_manifest.json", {})

    def readiness(self) -> dict[str, Any]:
        return _read_json(self.path / "readiness.json", {})

    def gather_qa(self) -> dict[str, Any]:
        return _read_json(self.path / "gather_qa.json", {})

    def gather_qa_review(self) -> dict[str, Any]:
        return _read_json(self.path / "gather_qa_review.json", {})

    def capsule_state(self) -> dict[str, Any]:
        return _read_json(self.path / "capsule_state.json", {})

    def object_names(self, *, include_support: bool = False) -> list[str]:
        names: list[str] = []
        for item in self.object_manifest().get("objects", []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            if not include_support and item.get("object_role") == "support":
                continue
            names.append(str(item.get("name")))
        return names

    def summary(self) -> dict[str, Any]:
        manifest = self.manifest
        task_spec = self.task_spec()
        source_plan = self.source_plan()
        row_schema = self.row_schema()
        schema_refinement = self.schema_refinement()
        readiness = self.readiness()
        gather_qa = self.gather_qa()
        gather_qa_review = self.gather_qa_review()
        capsule_state = self.capsule_state()
        return {
            "name": manifest.get("name", self.path.name),
            "task": manifest.get("task", ""),
            "created_at": manifest.get("created_at", ""),
            "task_id": task_spec.get("task_id", ""),
            "task_type": task_spec.get("task_type", manifest.get("task_type", "")),
            "row_object": row_schema.get("object_name", ""),
            "row_schema_confidence": row_schema.get("schema_confidence", ""),
            "schema_refinement_confidence": schema_refinement.get("schema_confidence", ""),
            "stage": capsule_state.get("stage", ""),
            "status": capsule_state.get("status", ""),
            "readiness": readiness.get("overall_status", ""),
            "analysis_object_count": len(self.object_names()),
            "support_object_count": len(self.object_names(include_support=True)) - len(self.object_names()),
            "gather_qa_reviewed_page_count": int(gather_qa.get("reviewed_page_count", 0) or 0),
            "gather_qa_agent_reviewed_page_count": int(gather_qa_review.get("reviewed_page_count", 0) or 0),
            "planned_source_count": len(source_plan.get("sources", [])),
            "page_count": len(manifest.get("pages", [])),
            "pending_followups": len(self.pending_followups()),
            "completed_followups": len(self.followup_results()),
        }

    def sources(self) -> list[dict[str, Any]]:
        manifest = self.manifest
        return list(manifest.get("pages", []))

    def source_index(self) -> dict[str, Any]:
        return _read_json(self.path / "source_index.json", {})

    def schema_summary(self) -> dict[str, Any]:
        return _read_json(self.path / "schema_summary.json", {})

    def analysis_plan(self) -> dict[str, Any]:
        return _read_json(self.path / "analysis_plan.json", {})

    def capture_brief(self) -> dict[str, Any]:
        return _read_json(self.path / "capture_brief.json", {})

    def table(self, name: str) -> Any:
        name = _validate_table_name(name)
        manifest = self.object_manifest()
        for item in manifest.get("objects", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("name", "")) != name:
                continue
            rel = item.get("table_path")
            if isinstance(rel, str) and rel:
                return _to_table(
                    _read_jsonl(
                        _validated_capsule_relpath(self.path, rel, expected_prefix="tables")
                    )
                )
        if name == "pages":
            return _to_table(_read_jsonl(self.path / "tables" / "pages.jsonl"))
        if name == "source_index":
            return _to_table(_read_jsonl(self.path / "tables" / "source_index.jsonl"))
        if name == "entities":
            return _to_table(_read_jsonl(self.path / "tables" / "entities.jsonl"))
        if name == "capture_targets":
            return _to_table(_read_jsonl(self.path / "tables" / "capture_targets.jsonl"))
        if name == "followups":
            return _to_table(self.pending_followups())
        if name == "followup_results":
            return _to_table(self.followup_results())
        generic_table_path = _validated_capsule_relpath(
            self.path,
            f"tables/{name}.jsonl",
            expected_prefix="tables",
        )
        if generic_table_path.exists():
            return _to_table(_read_jsonl(generic_table_path))
        raise KeyError(f"Unknown table: {name}")

    def pending_followups(self) -> list[dict[str, Any]]:
        rows = _read_jsonl(self.path / "followups" / "pending_followups.jsonl")
        return [row for row in rows if row.get("status", "pending") == "pending"]

    def followup_results(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.path / "followups" / "results.jsonl")

    def page_text(self, page_id: str) -> str:
        for page in self.sources():
            if page.get("page_id") == page_id:
                rel = page.get("artifacts", {}).get("page_text")
                if isinstance(rel, str):
                    try:
                        return (self.path / rel).read_text(encoding="utf-8")
                    except OSError:
                        return ""
        return ""

    def search(self, needle: str, *, case_sensitive: bool = False) -> Any:
        term = needle if case_sensitive else needle.lower()
        matches: list[dict[str, Any]] = []
        for page in self.sources():
            text = self.page_text(str(page.get("page_id", "")))
            source = text if case_sensitive else text.lower()
            if term not in source:
                continue
            idx = source.find(term)
            start = max(0, idx - 120)
            end = min(len(text), idx + len(needle) + 120)
            matches.append(
                {
                    "page_id": page.get("page_id", ""),
                    "title": page.get("title", ""),
                    "final_url": page.get("final_url", ""),
                    "snippet": text[start:end].replace("\n", " "),
                }
            )
        return _to_table(matches)

    def request_followup(
        self,
        *,
        url: str,
        instruction: str,
        page_id: Optional[str] = None,
        kind: str = "revisit",
    ) -> dict[str, Any]:
        payload = {
            "followup_id": f"fu-{uuid.uuid4().hex[:10]}",
            "created_at": _now_iso(),
            "status": "pending",
            "kind": kind,
            "url": _validate_followup_url(url),
            "page_id": page_id or "",
            "instruction": instruction,
        }
        _append_jsonl(
            self.path / "followups" / "pending_followups.jsonl",
            payload,
            root=self.path,
        )
        return payload


def load_capsule(path: Union[str, Path]) -> Capsule:
    return Capsule(Path(path).resolve())
