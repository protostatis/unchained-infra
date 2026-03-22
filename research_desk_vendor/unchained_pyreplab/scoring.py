from __future__ import annotations

from typing import Any

GRADE_POINTS = {
    "A+": 4.3,
    "A": 4.0,
    "A-": 3.7,
    "B+": 3.3,
    "B": 3.0,
    "B-": 2.7,
    "C+": 2.3,
    "C": 2.0,
    "C-": 1.7,
    "D+": 1.3,
    "D": 1.0,
    "D-": 0.7,
    "F": 0.0,
}

SCORE_FIELDS = [
    "academic_performance_score",
    "student_teacher_ratio_score",
    "college_readiness_score",
    "extracurricular_breadth_score",
    "parent_sentiment_score",
]


def grade_to_pct(letter: Any) -> float | None:
    if not isinstance(letter, str) or not letter:
        return None
    points = GRADE_POINTS.get(letter)
    if points is None:
        return None
    return round((points / 4.3) * 100.0, 1)


def mean_or_none(values: list[Any]) -> float | None:
    clean = [float(value) for value in values if isinstance(value, (int, float))]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 1)


def normalize_metric(
    records: list[dict[str, Any]],
    key: str,
    *,
    lower_is_better: bool = False,
) -> dict[str, float | None]:
    numeric = [float(record[key]) for record in records if isinstance(record.get(key), (int, float))]
    if not numeric:
        return {}
    low = min(numeric)
    high = max(numeric)
    result: dict[str, float | None] = {}
    for record in records:
        value = record.get(key)
        entity_name = str(record.get("entity_name", ""))
        if not isinstance(value, (int, float)):
            result[entity_name] = None
            continue
        if high == low:
            score = 100.0
        else:
            fraction = (
                (high - float(value)) / (high - low)
                if lower_is_better
                else (float(value) - low) / (high - low)
            )
            score = round(fraction * 100.0, 1)
        result[entity_name] = score
    return result


def _category_weights(analysis_plan: dict[str, Any]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for item in analysis_plan.get("rubric", []):
        field = item.get("field")
        if not isinstance(field, str) or not field:
            continue
        weights[field] = float(item.get("weight", 0.0))
    return weights


def _score_academic(record: dict[str, Any]) -> float | None:
    return mean_or_none(
        [
            record.get("math_proficiency_pct"),
            record.get("reading_proficiency_pct"),
            grade_to_pct(record.get("academics_grade")),
            grade_to_pct(record.get("overall_niche_grade")),
        ]
    )


def _score_college(record: dict[str, Any]) -> float | None:
    sat = record.get("average_sat")
    act = record.get("average_act")
    return mean_or_none(
        [
            record.get("graduation_rate_pct"),
            round((sat / 1600.0) * 100.0, 1) if isinstance(sat, (int, float)) else None,
            round((act / 36.0) * 100.0, 1) if isinstance(act, (int, float)) else None,
            grade_to_pct(record.get("college_prep_grade")),
        ]
    )


def _score_extracurricular(record: dict[str, Any]) -> float | None:
    return mean_or_none(
        [
            grade_to_pct(record.get("clubs_activities_grade")),
            100.0 if record.get("ap_offered") else None,
        ]
    )


def _score_sentiment(record: dict[str, Any]) -> float | None:
    rating = record.get("rating_out_of_5")
    if not isinstance(rating, (int, float)):
        return None
    return round((rating / 5.0) * 100.0, 1)


def score_highschool_districts(
    district_metrics: list[dict[str, Any]],
    analysis_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    category_weights = _category_weights(analysis_plan)
    ranking_policy = analysis_plan.get("ranking_policy") or {}
    required_fields = [
        str(field)
        for field in ranking_policy.get("required_fields", [])
        if isinstance(field, str) and field
    ]
    minimum_coverage_weight = float(ranking_policy.get("minimum_coverage_weight", 0.85))

    ratio_scores = normalize_metric(district_metrics, "student_teacher_ratio", lower_is_better=True)

    ranked: list[dict[str, Any]] = []
    for record in district_metrics:
        row = dict(record)
        row["academic_performance_score"] = _score_academic(record)
        row["student_teacher_ratio_score"] = ratio_scores.get(str(record.get("entity_name", "")))
        row["college_readiness_score"] = _score_college(record)
        row["extracurricular_breadth_score"] = _score_extracurricular(record)
        row["parent_sentiment_score"] = _score_sentiment(record)

        present_weight = sum(
            category_weights.get(field, 0.0)
            for field in SCORE_FIELDS
            if isinstance(row.get(field), (int, float))
        )
        row["coverage_weight"] = round(present_weight, 2)

        if present_weight:
            exploratory_score = sum(
                category_weights.get(field, 0.0) * float(row.get(field))
                for field in SCORE_FIELDS
                if isinstance(row.get(field), (int, float))
            ) / present_weight
            row["exploratory_score"] = round(exploratory_score, 1)
            row["coverage_penalized_score"] = round(exploratory_score * present_weight, 1)
        else:
            row["exploratory_score"] = None
            row["coverage_penalized_score"] = None

        row["missing_critical_fields"] = [
            field for field in required_fields if row.get(field) in (None, "", [])
        ]
        row["is_comparable"] = row.get("comparability_flag") == "high_school_only"
        row["is_rankable"] = bool(
            row["is_comparable"]
            and not row["missing_critical_fields"]
            and present_weight >= minimum_coverage_weight
        )

        if not row["is_comparable"]:
            ranking_status = "not_comparable"
        elif row["missing_critical_fields"]:
            ranking_status = "needs_followup_missing_critical_metrics"
        elif present_weight < minimum_coverage_weight:
            ranking_status = "needs_followup_low_coverage"
        else:
            ranking_status = "rankable"

        row["ranking_status"] = ranking_status
        row["final_rankable_score"] = (
            row["coverage_penalized_score"] if row["is_rankable"] else None
        )
        ranked.append(row)

    ranked.sort(
        key=lambda row: (
            row.get("final_rankable_score") is None,
            -(row.get("final_rankable_score") or 0.0),
            -(row.get("coverage_penalized_score") or 0.0),
            -(row.get("exploratory_score") or 0.0),
            row.get("entity_name", ""),
        )
    )

    exploratory_sorted = sorted(
        ranked,
        key=lambda row: (
            row.get("exploratory_score") is None,
            -(row.get("exploratory_score") or 0.0),
            row.get("entity_name", ""),
        ),
    )
    for index, row in enumerate(exploratory_sorted, start=1):
        row["exploratory_rank"] = index

    final_rank = 1
    for row in ranked:
        if row["is_rankable"]:
            row["final_rank"] = final_rank
            row["rank"] = final_rank
            final_rank += 1
        else:
            row["final_rank"] = None
            row["rank"] = None

    return ranked
