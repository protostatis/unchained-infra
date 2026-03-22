from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .analysis_runtime import (
    build_district_metrics,
    format_dataframe,
    get_dataframe_module,
    load_analysis_context,
    table_columns,
    table_row_count,
    to_dataframe,
)
from .scoring import score_highschool_districts

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_REVISION = "2026-03-10-lab-bootstrap-v2"
SESSION_REVISION_FILES = (
    "manifest.json",
    "task_spec.json",
    "source_plan.json",
    "source_index.json",
    "schema_summary.json",
    "analysis_plan.json",
    "object_manifest.json",
    "readiness.json",
    "row_schema.json",
    "schema_refinement.json",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


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


def _capsule_revision(capsule_dir: Path) -> str:
    parts: list[str] = ["bootstrap:{revision}".format(revision=BOOTSTRAP_REVISION)]
    for name in SESSION_REVISION_FILES:
        path = capsule_dir / name
        try:
            stat = path.stat()
        except OSError:
            parts.append("{name}:missing".format(name=name))
            continue
        parts.append(
            "{name}:{mtime}:{size}".format(
                name=name,
                mtime=int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                size=int(stat.st_size),
            )
        )
    return "|".join(parts)


def _namespace_rows(namespace: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in sorted(namespace.items()):
        if key.startswith("__"):
            continue
        row: dict[str, Any] = {"name": key, "type": type(value).__name__}
        if hasattr(value, "shape") and hasattr(value, "columns"):
            row["size"] = table_row_count(value)
            row["columns"] = table_columns(value)
            if hasattr(value, "dtypes"):
                try:
                    row["dtypes"] = {
                        str(column): str(dtype)
                        for column, dtype in list(value.dtypes.items())[:10]
                    }
                except Exception:
                    row["dtypes"] = {}
        elif isinstance(value, list):
            row["size"] = len(value)
            if value and isinstance(value[0], dict):
                row["columns"] = list(value[0].keys())[:12]
        elif isinstance(value, dict):
            row["size"] = len(value)
            row["keys"] = list(value.keys())[:12]
        rows.append(row)
    return rows


def _singularize_name(name: str) -> str:
    clean = str(name).strip()
    if not clean:
        return clean
    if clean.endswith("ies") and len(clean) > 3:
        return clean[:-3] + "y"
    if clean.endswith("_rows") and len(clean) > 5:
        return clean[:-5]
    if clean.endswith("s") and not clean.endswith("ss") and len(clean) > 1:
        return clean[:-1]
    return clean


def _primary_object_name(object_manifest: dict[str, Any]) -> str:
    objects = object_manifest.get("objects", [])
    if not isinstance(objects, list):
        return ""
    for item in objects:
        if isinstance(item, dict) and str(item.get("object_role", "")).strip() == "primary":
            return str(item.get("name", "")).strip()
    for item in objects:
        if isinstance(item, dict) and str(item.get("object_role", "")).strip() != "support":
            return str(item.get("name", "")).strip()
    return ""


def discover_pyreplab_bin() -> Optional[str]:
    candidates = [
        os.environ.get("PYREPLAB_BIN"),
        shutil.which("pyreplab"),
        str(REPO_ROOT.parent / "pyrepl" / "pyreplab"),
        "/Users/zhiminzou/Projects/pyrepl/pyreplab",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def build_bootstrap_markdown(capsule_dir: Path) -> str:
    context = load_analysis_context(capsule_dir)
    capsule = context["capsule"]
    task_spec = context.get("task_spec", {})
    source_plan = context.get("source_plan", {})
    object_manifest = context.get("object_manifest", {})
    readiness = context.get("readiness", {})
    capsule_state = context.get("capsule_state", {})
    brief = capsule.capture_brief()
    summary = brief.get("summary", {})

    source_rows = list(context["source_rows"])
    entity_rows = list(context["entity_rows"])
    source_df = context["source_df"]
    entity_df = context["entity_df"]
    gather_qa = context.get("gather_qa", {})
    gather_qa_review = context.get("gather_qa_review", {})
    district_metric_rows = build_district_metrics(entity_rows, source_rows)
    district_metrics_df = to_dataframe(district_metric_rows)
    ranked_district_rows = score_highschool_districts(district_metric_rows, context["analysis_plan"])
    ranked_districts_df = to_dataframe(ranked_district_rows)
    gather_qa_df = to_dataframe(list(gather_qa.get("rows", [])))
    gather_qa_review_df = to_dataframe(list(gather_qa_review.get("reviews", [])))
    extra_table_lines: list[str] = []
    for item in object_manifest.get("objects", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if name in {"pages", "source_index", "entities", "districts", "ranked_districts", "capture_targets"}:
            continue
        try:
            table = capsule.table(name)
        except KeyError:
            continue
        extra_table_lines.append(
            "- `{name}_df`: {count} rows".format(name=name, count=table_row_count(table))
        )

    lines = [
        "# Lab Bootstrap",
        "",
        "## Task",
        "- `task_id`: {task_id}".format(task_id=task_spec.get("task_id", "unknown")),
        "- `task_type`: {task_type}".format(
            task_type=task_spec.get("task_type") or context["analysis_plan"].get("task_type", "generic")
        ),
        "- workflow: `{stage}` / `{status}`".format(
            stage=capsule_state.get("stage", "unknown"),
            status=capsule_state.get("status", "unknown"),
        ),
        "- readiness: `{status}`".format(status=readiness.get("overall_status", "unknown")),
        "- planned sources: {count}".format(count=len(source_plan.get("sources", []))),
        "- target objects: {targets}".format(
            targets=", ".join(
                str(item.get("name", ""))
                for item in task_spec.get("target_objects", [])
                if isinstance(item, dict) and item.get("name")
            )
            or "none",
        ),
        "",
        "## Objects",
        "- `pd`: pandas module for notebook analysis",
        "- `source_df`: {count} rows".format(count=table_row_count(source_df)),
        "- `entity_df`: {count} rows".format(count=table_row_count(entity_df)),
        "- `district_metrics_df`: {count} rows".format(count=table_row_count(district_metrics_df)),
        "- `ranked_districts_df`: {count} rows".format(count=table_row_count(ranked_districts_df)),
        "- `gather_qa_df`: {count} rows".format(count=table_row_count(gather_qa_df)),
        "- `gather_qa_review_df`: {count} rows".format(count=table_row_count(gather_qa_review_df)),
        "- raw records remain available as `source_records`, `entity_records`, `district_metric_records`, `ranked_district_records`",
    ]
    lines.extend(extra_table_lines)
    lines.extend(
        [
            "",
            "## Columns",
            "- `source_df`: {cols}".format(cols=", ".join(table_columns(source_df)) or "none"),
            "- `entity_df`: {cols}".format(cols=", ".join(table_columns(entity_df)) or "none"),
            "- `district_metrics_df`: {cols}".format(cols=", ".join(table_columns(district_metrics_df)) or "none"),
            "- `ranked_districts_df`: {cols}".format(cols=", ".join(table_columns(ranked_districts_df)) or "none"),
            "- `gather_qa_df`: {cols}".format(cols=", ".join(table_columns(gather_qa_df)) or "none"),
            "- `gather_qa_review_df`: {cols}".format(cols=", ".join(table_columns(gather_qa_review_df)) or "none"),
            "",
            "## Readiness",
            "- overall: {overall}".format(overall=readiness.get("overall_status", "unknown")),
            "- comparable entities: {current}/{total}".format(
                current=summary.get("comparable_entity_count", 0),
                total=summary.get("entity_count", 0),
            ),
            "- source mix ready: {current}/{total}".format(
                current=summary.get("source_mix_ready_count", 0),
                total=summary.get("entity_count", 0),
            ),
            "- missing official sites: {count}".format(
                count=summary.get("entities_missing_official_site_count", 0)
            ),
            "- missing state report-card sources: {count}".format(
                count=summary.get("entities_missing_state_report_card_count", 0)
            ),
            "- entities with rank-critical gaps: {count}".format(
                count=summary.get("entities_with_rank_critical_metric_gaps_count", 0)
            ),
        ]
    )

    priority_actions = list(brief.get("priority_actions", []))
    if priority_actions:
        lines.extend(["", "## Next Actions"])
        for action in priority_actions:
            lines.append("- {action}".format(action=action))

    return "\n".join(lines)


def _bootstrap_namespace(capsule_dir: Path) -> dict[str, Any]:
    context = load_analysis_context(capsule_dir)
    capsule = context["capsule"]
    task_spec = context["task_spec"]
    source_plan = context["source_plan"]
    object_manifest = context["object_manifest"]
    readiness = context["readiness"]
    gather_qa = context["gather_qa"]
    gather_qa_review = context["gather_qa_review"]
    capsule_state = context["capsule_state"]
    analysis_plan = context["analysis_plan"]
    source_rows = list(context["source_rows"])
    entity_rows = list(context["entity_rows"])
    source_df = context["source_df"]
    entity_df = context["entity_df"]
    district_metric_rows = build_district_metrics(entity_rows, source_rows)
    district_metrics_df = to_dataframe(district_metric_rows)
    ranked_district_rows: list[dict[str, Any]] = []
    ranked_districts_df = to_dataframe(ranked_district_rows)
    gather_qa_df = to_dataframe(list(gather_qa.get("rows", [])))
    gather_qa_review_df = to_dataframe(list(gather_qa_review.get("reviews", [])))
    if analysis_plan.get("task_type") == "highschool_district_compare":
        ranked_district_rows = score_highschool_districts(district_metric_rows, analysis_plan)
        ranked_districts_df = to_dataframe(ranked_district_rows)

    namespace = {
        "__builtins__": __builtins__,
        "pd": get_dataframe_module(),
        "show_df": lambda table, limit=20, columns=None, sort_by=None, ascending=False, **kwargs: print(
            format_dataframe(
                table,
                limit=limit,
                columns=list(columns) if columns is not None else None,
                sort_by=sort_by,
                ascending=ascending,
            )
        ),
        "capsule_dir": capsule_dir,
        "context": context,
        "capsule": capsule,
        "task_spec": task_spec,
        "source_plan": source_plan,
        "object_manifest": object_manifest,
        "readiness": readiness,
        "gather_qa": gather_qa,
        "gather_qa_review": gather_qa_review,
        "capsule_state": capsule_state,
        "analysis_plan": analysis_plan,
        "source_df": source_df,
        "entity_df": entity_df,
        "district_metrics_df": district_metrics_df,
        "gather_qa_df": gather_qa_df,
        "gather_qa_review_df": gather_qa_review_df,
        "source_rows": source_df,
        "entity_rows": entity_df,
        "district_metrics": district_metrics_df,
        "source_records": source_rows,
        "entity_records": entity_rows,
        "district_metric_records": district_metric_rows,
        "capture_brief": capsule.capture_brief(),
    }
    extra_tables: dict[str, Any] = {}
    for name in capsule.object_names():
        try:
            table = capsule.table(name)
        except KeyError:
            continue
        extra_tables[name] = table
        namespace[f"{name}_df"] = table
        singular_name = _singularize_name(name)
        if singular_name and singular_name != name:
            namespace.setdefault(f"{singular_name}_df", table)
    namespace["table_frames"] = extra_tables
    primary_object_name = _primary_object_name(object_manifest)
    if primary_object_name:
        namespace["recommended_dataframe"] = f"{primary_object_name}_df"
    if analysis_plan.get("task_type") == "highschool_district_compare":
        namespace["ranked_districts_df"] = ranked_districts_df
        namespace["ranked_districts"] = ranked_districts_df
        namespace["ranked_district_records"] = ranked_district_rows
    return namespace


def _bootstrap_code(capsule_dir: Path) -> str:
    capsule_literal = json.dumps(str(capsule_dir))
    return """
import importlib
from pathlib import Path

capsule_runtime = importlib.import_module("unchained_pyreplab.capsule_runtime")
capsule_runtime = importlib.reload(capsule_runtime)
analysis_runtime = importlib.import_module("unchained_pyreplab.analysis_runtime")
analysis_runtime = importlib.reload(analysis_runtime)
scoring = importlib.import_module("unchained_pyreplab.scoring")
scoring = importlib.reload(scoring)

build_district_metrics = analysis_runtime.build_district_metrics
get_dataframe_module = analysis_runtime.get_dataframe_module
load_analysis_context = analysis_runtime.load_analysis_context
to_dataframe = analysis_runtime.to_dataframe
score_highschool_districts = scoring.score_highschool_districts

capsule_dir = Path({capsule_literal})
context = load_analysis_context(capsule_dir)
capsule = context["capsule"]
task_spec = context["task_spec"]
source_plan = context["source_plan"]
object_manifest = context["object_manifest"]
readiness = context["readiness"]
gather_qa = context["gather_qa"]
gather_qa_review = context["gather_qa_review"]
capsule_state = context["capsule_state"]
analysis_plan = context["analysis_plan"]
source_rows = list(context["source_rows"])
entity_rows = list(context["entity_rows"])
source_df = context["source_df"]
entity_df = context["entity_df"]
district_metric_rows = build_district_metrics(entity_rows, source_rows)
district_metrics_df = to_dataframe(district_metric_rows)
ranked_district_rows = []
ranked_districts_df = to_dataframe(ranked_district_rows)
gather_qa_df = to_dataframe(list(gather_qa.get("rows", [])))
gather_qa_review_df = to_dataframe(list(gather_qa_review.get("reviews", [])))
if analysis_plan.get("task_type") == "highschool_district_compare":
    ranked_district_rows = score_highschool_districts(district_metric_rows, analysis_plan)
    ranked_districts_df = to_dataframe(ranked_district_rows)

def _format_markdown_cell(value):
    try:
        import pandas as _pd
        if _pd.isna(value):
            return ""
    except Exception:
        pass
    if value is None:
        return ""
    if isinstance(value, float):
        return ("{{value:.2f}}".format(value=value)).rstrip("0").rstrip(".")
    return str(value).replace("\\n", " ").replace("\\r", " ").replace("|", "\\\\|")

def _dataframe_to_markdown(frame):
    columns = [str(column) for column in list(frame.columns)]
    if not columns:
        return "(0 columns)"
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = []
    for row in frame.to_dict(orient="records"):
        body.append("| " + " | ".join(_format_markdown_cell(row.get(column)) for column in columns) + " |")
    return "\\n".join([header, divider, *body])

def show_df(table, limit=20, columns=None, sort_by=None, ascending=False, **kwargs):
    frame = table
    try:
        import pandas as _pd
    except Exception:
        _pd = None
    if _pd is not None and not isinstance(frame, _pd.DataFrame):
        if isinstance(frame, list):
            frame = _pd.DataFrame(frame)
        elif isinstance(frame, dict):
            frame = _pd.DataFrame([frame])
        else:
            frame = _pd.DataFrame({{"value": [frame]}})
    if _pd is None or not isinstance(frame, _pd.DataFrame):
        print(str(frame))
        return
    if columns:
        selected = [column for column in columns if column in frame.columns]
        if selected:
            frame = frame.loc[:, selected]
    if sort_by and sort_by in frame.columns:
        frame = frame.sort_values(sort_by, ascending=ascending, na_position="last")
    preview = frame.head(limit)
    if preview.empty:
        print("(0 rows)")
        return
    body = _dataframe_to_markdown(preview)
    more_rows = max(int(frame.shape[0]) - int(preview.shape[0]), 0)
    if more_rows:
        print("rows={{rows}} shown={{shown}}\\n{{body}}\\n... {{more}} more rows".format(
            rows=int(frame.shape[0]),
            shown=int(preview.shape[0]),
            body=body,
            more=more_rows,
        ))
        return
    print("rows={{rows}}\\n{{body}}".format(rows=int(frame.shape[0]), body=body))

globals().update(
    {{
        "pd": get_dataframe_module(),
        "capsule_dir": capsule_dir,
        "context": context,
        "capsule": capsule,
        "task_spec": task_spec,
        "source_plan": source_plan,
        "object_manifest": object_manifest,
        "readiness": readiness,
        "gather_qa": gather_qa,
        "gather_qa_review": gather_qa_review,
        "capsule_state": capsule_state,
        "analysis_plan": analysis_plan,
        "source_df": source_df,
        "entity_df": entity_df,
        "district_metrics_df": district_metrics_df,
        "gather_qa_df": gather_qa_df,
        "gather_qa_review_df": gather_qa_review_df,
        "source_rows": source_df,
        "entity_rows": entity_df,
        "district_metrics": district_metrics_df,
        "source_records": source_rows,
        "entity_records": entity_rows,
        "district_metric_records": district_metric_rows,
        "capture_brief": capsule.capture_brief(),
        "show_df": show_df,
    }}
)

extra_tables = {{}}
for stem in capsule.object_names():
    table = capsule.table(stem)
    extra_tables[stem] = table
    globals()[f"{{stem}}_df"] = table
    singular_stem = stem
    if stem.endswith("ies") and len(stem) > 3:
        singular_stem = stem[:-3] + "y"
    elif stem.endswith("_rows") and len(stem) > 5:
        singular_stem = stem[:-5]
    elif stem.endswith("s") and not stem.endswith("ss") and len(stem) > 1:
        singular_stem = stem[:-1]
    if singular_stem and singular_stem != stem:
        globals().setdefault(f"{{singular_stem}}_df", table)
globals()["table_frames"] = extra_tables
primary_object_name = ""
for item in object_manifest.get("objects", []):
    if isinstance(item, dict) and str(item.get("object_role", "")).strip() == "primary":
        primary_object_name = str(item.get("name", "")).strip()
        break
if not primary_object_name:
    for item in object_manifest.get("objects", []):
        if isinstance(item, dict) and str(item.get("object_role", "")).strip() != "support":
            primary_object_name = str(item.get("name", "")).strip()
            break
if primary_object_name:
    globals()["recommended_dataframe"] = f"{{primary_object_name}}_df"

if analysis_plan.get("task_type") == "highschool_district_compare":
    globals()["ranked_districts_df"] = ranked_districts_df
    globals()["ranked_districts"] = ranked_districts_df
    globals()["ranked_district_records"] = ranked_district_rows
""".format(capsule_literal=capsule_literal).strip()


NAMESPACE_SUMMARY_CODE = """
import json

rows = []
for key, value in sorted(globals().items()):
    if key.startswith("__"):
        continue
    row = {"name": key, "type": type(value).__name__}
    if hasattr(value, "shape") and hasattr(value, "columns"):
        try:
            row["size"] = int(value.shape[0])
            row["columns"] = [str(column) for column in list(value.columns)[:12]]
        except Exception:
            pass
    elif isinstance(value, list):
        row["size"] = len(value)
        if value and isinstance(value[0], dict):
            row["columns"] = list(value[0].keys())[:12]
    elif isinstance(value, dict):
        row["size"] = len(value)
        row["keys"] = [str(item) for item in list(value.keys())[:12]]
    rows.append(row)
print(json.dumps(rows))
""".strip()


@dataclass
class LabSession:
    capsule_dir: Path
    namespace: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    pyreplab_ready: bool = False
    namespace_rows_cache: list[dict[str, Any]] = field(default_factory=list)
    capsule_revision: str = ""

    @property
    def turns_path(self) -> Path:
        return self.capsule_dir / "lab" / "turns.jsonl"

    @property
    def pyreplab_dir(self) -> Path:
        return self.capsule_dir / "lab" / "pyreplab_session"

    def _pyreplab_env(self, *, timeout_seconds: Optional[float] = None) -> dict[str, str]:
        env = os.environ.copy()
        env["PYREPLAB_DIR"] = str(self.pyreplab_dir)
        if timeout_seconds is not None:
            env["PYREPLAB_TIMEOUT"] = str(int(timeout_seconds))
        return env

    def _run_pyreplab(
        self,
        *args: str,
        input_text: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> subprocess.CompletedProcess[str]:
        pyreplab_bin = discover_pyreplab_bin()
        if not pyreplab_bin:
            raise RuntimeError("Unable to find pyreplab. Set PYREPLAB_BIN or install it in PATH.")
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_handle:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_handle:
                completed = subprocess.run(
                    [pyreplab_bin, *args],
                    input=input_text,
                    text=True,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    check=False,
                    cwd=REPO_ROOT,
                    env=self._pyreplab_env(timeout_seconds=timeout_seconds),
                )
                stdout_handle.seek(0)
                stderr_handle.seek(0)
                stdout_text = stdout_handle.read()
                stderr_text = stderr_handle.read()
        return subprocess.CompletedProcess(
            args=[pyreplab_bin, *args],
            returncode=completed.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )

    def runtime_status(self) -> dict[str, str]:
        pyreplab_bin = discover_pyreplab_bin()
        if not pyreplab_bin:
            return {
                "backend": "pyreplab",
                "state": "missing",
                "detail": "Unable to find pyreplab. Set PYREPLAB_BIN or install it in PATH.",
            }
        completed = self._run_pyreplab("status", timeout_seconds=5)
        detail = (completed.stdout or completed.stderr or "").strip()
        state = "stopped"
        if completed.returncode == 0:
            state = "busy" if "executing command" in detail.lower() else "idle"
        return {"backend": "pyreplab", "state": state, "detail": detail}

    def _bootstrap_local_namespace(self) -> None:
        self.namespace = _bootstrap_namespace(self.capsule_dir)
        self.namespace_rows_cache = _namespace_rows(self.namespace)
        self.capsule_revision = _capsule_revision(self.capsule_dir)

    def _next_turn_id(self) -> str:
        return "turn-{value}".format(value=uuid.uuid4().hex)

    def _append_turn_unlocked(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload)
        row.setdefault("turn_id", self._next_turn_id())
        row.setdefault("created_at", _now_iso())
        _append_jsonl(self.turns_path, row)
        return row

    def _invalidate_if_capsule_changed(self) -> None:
        current_revision = _capsule_revision(self.capsule_dir)
        if not self.namespace:
            return
        if self.capsule_revision and current_revision == self.capsule_revision:
            return
        status = self.runtime_status()
        if status["state"] in {"idle", "busy"}:
            self._run_pyreplab("stop")
            self._run_pyreplab("clean")
        self.pyreplab_ready = False
        self.namespace_rows_cache = []
        self._bootstrap_local_namespace()

    def _ensure_pyreplab_runtime(self) -> None:
        if self.pyreplab_ready:
            return
        status = self.runtime_status()
        if status["state"] in {"idle", "busy"}:
            self.pyreplab_ready = True
            if status["state"] == "idle":
                bootstrap_timeout = float(os.environ.get("UNCHAINED_PYREPLAB_BOOTSTRAP_TIMEOUT_SECONDS", "45"))
                bootstrap = self._run_pyreplab(
                    "run",
                    input_text=_bootstrap_code(self.capsule_dir),
                    timeout_seconds=bootstrap_timeout,
                )
                if bootstrap.returncode != 0:
                    detail = (bootstrap.stderr or bootstrap.stdout or "unknown error").strip()
                    raise RuntimeError("pyreplab bootstrap failed: {detail}".format(detail=detail))
                self._refresh_namespace_rows()
            return
        start = self._run_pyreplab("start", "--workdir", str(REPO_ROOT), "--cwd", str(REPO_ROOT))
        if start.returncode != 0:
            detail = (start.stderr or start.stdout or "unknown error").strip()
            raise RuntimeError("pyreplab start failed: {detail}".format(detail=detail))
        start_deadline = time.time() + float(os.environ.get("UNCHAINED_PYREPLAB_START_WAIT_SECONDS", "5"))
        while time.time() < start_deadline:
            status = self.runtime_status()
            if status["state"] in {"idle", "busy"}:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("pyreplab did not become ready after start")
        bootstrap_timeout = float(os.environ.get("UNCHAINED_PYREPLAB_BOOTSTRAP_TIMEOUT_SECONDS", "45"))
        bootstrap = self._run_pyreplab(
            "run",
            input_text=_bootstrap_code(self.capsule_dir),
            timeout_seconds=bootstrap_timeout,
        )
        if bootstrap.returncode not in (0,):
            detail = (bootstrap.stderr or bootstrap.stdout or "unknown error").strip()
            raise RuntimeError("pyreplab bootstrap failed: {detail}".format(detail=detail))
        self.pyreplab_ready = True
        self._refresh_namespace_rows()

    def _refresh_namespace_rows(self) -> None:
        if not self.pyreplab_ready:
            self.namespace_rows_cache = _namespace_rows(self.namespace)
            return
        refresh_timeout = float(os.environ.get("UNCHAINED_PYREPLAB_REFRESH_TIMEOUT_SECONDS", "10"))
        result = self._run_pyreplab("run", input_text=NAMESPACE_SUMMARY_CODE, timeout_seconds=refresh_timeout)
        if result.returncode != 0:
            return
        payload = (result.stdout or "").strip()
        if not payload:
            return
        try:
            rows = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(rows, list):
            self.namespace_rows_cache = [row for row in rows if isinstance(row, dict)]

    def namespace_rows(self) -> list[dict[str, Any]]:
        if self.namespace_rows_cache:
            return list(self.namespace_rows_cache)
        return _namespace_rows(self.namespace)

    def ensure_initialized(self) -> None:
        with self.lock:
            self._invalidate_if_capsule_changed()
            if not self.namespace:
                self._bootstrap_local_namespace()
            if not self.turns_path.exists():
                self._append_turn_unlocked(
                    {
                        "cell_type": "markdown",
                        "role": "system",
                        "content": build_bootstrap_markdown(self.capsule_dir),
                    }
                )

    def reset(self) -> None:
        with self.lock:
            status = self.runtime_status()
            if status["state"] in {"idle", "busy"}:
                self._run_pyreplab("stop")
                self._run_pyreplab("clean")
            self.pyreplab_ready = False
            self.namespace_rows_cache = []
            self._bootstrap_local_namespace()
            self._append_turn_unlocked(
                {
                    "cell_type": "markdown",
                    "role": "system",
                    "content": "## Session Reset\n\nKernel state was reset and bootstrap context was reloaded.",
                }
            )

    def append_markdown(self, text: str, *, role: str = "user") -> dict[str, Any]:
        with self.lock:
            return self._append_turn_unlocked(
                {
                    "cell_type": "markdown",
                    "role": role,
                    "content": text.strip(),
                }
            )

    def append_query(self, text: str, *, role: str = "user") -> dict[str, Any]:
        with self.lock:
            return self._append_turn_unlocked(
                {
                    "cell_type": "query",
                    "role": role,
                    "content": text.strip(),
                }
            )

    def _output_payload_from_completed(self, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        stdout_text = (completed.stdout or "").rstrip()
        stderr_text = (completed.stderr or "").rstrip()
        output_text = stdout_text or "(no stdout)"
        error_text = ""
        status = "ok"

        if completed.returncode == 2:
            status = "running"
            output_text = stderr_text or stdout_text or "pyreplab: still running"
        elif completed.returncode != 0:
            status = "error"
            error_text = stderr_text or stdout_text or "pyreplab command failed"
        elif stderr_text:
            output_text = "{stdout}\n\n[stderr]\n{stderr}".format(
                stdout=output_text,
                stderr=stderr_text,
            )

        payload = {
            "turn_id": self._next_turn_id(),
            "created_at": _now_iso(),
            "cell_type": "output",
            "role": "system",
            "content": output_text,
            "status": status,
        }
        if error_text:
            payload["error"] = error_text.rstrip()
        return payload

    def execute_code(
        self,
        code: str,
        *,
        role: str = "user",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.ensure_initialized()
        with self.lock:
            self._ensure_pyreplab_runtime()
            code_cell = {
                "cell_type": "code",
                "role": role,
                "content": code.rstrip(),
            }
            if metadata:
                for key in ("title", "intent", "notes"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        code_cell[key] = value.strip()
            self._append_turn_unlocked(code_cell)

            run_timeout = float(os.environ.get("UNCHAINED_PYREPLAB_RUN_TIMEOUT_SECONDS", "30"))
            completed = self._run_pyreplab("run", input_text=code, timeout_seconds=run_timeout)
            result = self._output_payload_from_completed(completed)

            if result["status"] in {"ok", "error"}:
                self._refresh_namespace_rows()

            self._append_turn_unlocked(result)
            return result

    def wait_for_pending(self) -> dict[str, Any]:
        self.ensure_initialized()
        with self.lock:
            self._ensure_pyreplab_runtime()
            wait_timeout = float(os.environ.get("UNCHAINED_PYREPLAB_WAIT_TIMEOUT_SECONDS", "2"))
            completed = self._run_pyreplab("wait", timeout_seconds=wait_timeout)
            stdout_text = (completed.stdout or "").rstrip()
            stderr_text = (completed.stderr or "").rstrip()
            detail = stderr_text or stdout_text or "pyreplab wait returned no output"

            if completed.returncode == 2:
                return {"status": "running", "content": detail, "appended": False}
            if completed.returncode != 0 and "no command pending" in detail.lower():
                return {"status": "idle", "content": detail, "appended": False}

            result = self._output_payload_from_completed(completed)
            if result["status"] in {"ok", "error"}:
                self._refresh_namespace_rows()
            self._append_turn_unlocked(result)
            result["appended"] = True
            return result

    def read_turns(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.turns_path)


_SESSIONS: dict[str, LabSession] = {}
_SESSIONS_LOCK = threading.Lock()


def get_session(capsule_dir: Path) -> LabSession:
    key = str(capsule_dir.resolve())
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
        if session is None:
            session = LabSession(capsule_dir=Path(key))
            _SESSIONS[key] = session
    session.ensure_initialized()
    return session
