from __future__ import annotations

import json
import re
from hashlib import sha1
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse

RECIPE_GENERIC = "generic_research"
RECIPE_HIGHSCHOOL = "highschool_district_compare"

DISTRICT_KEYWORDS = [
    "student-teacher ratio",
    "math",
    "reading",
    "graduation",
    "average sat",
    "average act",
    "college prep",
    "clubs & activities",
    "reviews",
    "ap offered",
]

DISTRICT_FIELDS = [
    "grades_served",
    "overall_niche_grade",
    "academics_grade",
    "college_prep_grade",
    "clubs_activities_grade",
    "student_teacher_ratio",
    "math_proficiency_pct",
    "reading_proficiency_pct",
    "graduation_rate_pct",
    "average_sat",
    "average_act",
    "rating_out_of_5",
    "review_count",
    "ap_offered",
]

DISTRICT_RUBRIC = [
    {
        "field": "academic_performance_score",
        "weight": 0.35,
        "description": "Math and reading proficiency plus academics indicators",
    },
    {
        "field": "student_teacher_ratio_score",
        "weight": 0.20,
        "description": "Lower student-teacher ratio scores better",
    },
    {
        "field": "college_readiness_score",
        "weight": 0.20,
        "description": "Graduation, SAT/ACT, and college prep indicators",
    },
    {
        "field": "extracurricular_breadth_score",
        "weight": 0.15,
        "description": "Programs, clubs, and AP availability",
    },
    {
        "field": "parent_sentiment_score",
        "weight": 0.10,
        "description": "Rating and review sentiment proxy",
    },
]

DISTRICT_RANKING_POLICY = {
    "required_fields": [
        "grades_served",
        "student_teacher_ratio",
        "math_proficiency_pct",
        "reading_proficiency_pct",
        "graduation_rate_pct",
    ],
    "minimum_coverage_weight": 0.85,
    "final_score_formula": "exploratory_score * coverage_weight",
    "notes": [
        "Exploratory scores may be shown on sparse records.",
        "Final rankable scores require a comparable high-school-only district plus the required fields.",
    ],
}

DISTRICT_SOURCE_REQUIREMENTS = [
    {
        "source_type": "district_profile",
        "required": True,
        "scope": "entity",
        "purpose": "broad profile coverage and sentiment",
    },
    {
        "source_type": "official_site",
        "required": True,
        "scope": "entity",
        "purpose": "official grade-band, program, and district facts",
    },
    {
        "source_type": "state_report_card",
        "required": True,
        "scope": "entity",
        "purpose": "official quantitative accountability metrics",
    },
    {
        "source_type": "ranking_article",
        "required": False,
        "scope": "global",
        "purpose": "candidate discovery and public context",
    },
]

STOPWORDS = {
    "a",
    "an",
    "and",
    "area",
    "best",
    "build",
    "compare",
    "comparison",
    "demo",
    "find",
    "for",
    "from",
    "high",
    "in",
    "is",
    "large",
    "metro",
    "metropolitan",
    "mock",
    "of",
    "on",
    "public",
    "school",
    "schools",
    "task",
    "the",
    "through",
    "top",
    "with",
}

SOURCE_TYPE_PRIORITY = {
    "state_report_card": 0,
    "official_site": 1,
    "district_profile": 2,
    "ranking_page": 3,
    "ranking_article": 4,
    "news_article": 5,
    "unknown": 6,
}

SOURCE_QUALITY_SCORES = {
    "state_report_card": 100,
    "official_site": 90,
    "district_profile": 70,
    "ranking_page": 45,
    "ranking_article": 40,
    "news_article": 30,
    "unknown": 10,
}

NEWS_DOMAINS = (
    "nbcchicago.com",
    "chicagotribune.com",
    "patch.com",
    "fox32chicago.com",
    "abc7chicago.com",
)

REGION_GUIDES = {
    "illinois": {
        "label": "Illinois",
        "state_report_card_domain": "illinoisreportcard.com",
        "state_agency_domain": "isbe.net",
    }
}

CAPSULES_ROOT = Path(__file__).resolve().parent.parent / "capsules"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def infer_recipe(task: str, manifest: dict[str, Any], explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit

    task_lower = task.lower()
    if "district" in task_lower and ("high school" in task_lower or "school district" in task_lower):
        return RECIPE_HIGHSCHOOL

    titles = " ".join(str(page.get("title", "")) for page in manifest.get("pages", []))
    if "school district" in titles.lower():
        return RECIPE_HIGHSCHOOL

    return RECIPE_GENERIC


def _extract_min_rows(task: str, default: int) -> int:
    match = re.search(r"\b(\d+)\s*[- ]?(?:row|rows)\b", task, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(?:sample|top|get me|collect)\s+(\d+)\b", task, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(?:compare|find|analyze|rank|evaluate)\s+(\d+)\b", task, re.I)
    if match:
        return int(match.group(1))
    return default


def _merge_plan_notes(existing: Any, *extra: str) -> list[str]:
    notes: list[str] = []
    if isinstance(existing, list):
        for item in existing:
            text = str(item).strip()
            if text and text not in notes:
                notes.append(text)
    for item in extra:
        text = str(item).strip()
        if text and text not in notes:
            notes.append(text)
    return notes


def _zip_fragment(prompt: str) -> str:
    match = re.search(r"\b(\d{5})(?:-\d{4})?\b", prompt)
    if match:
        return match.group(1)
    return "target-area"


def _clean_focus_phrase(value: str, *, fallback: str) -> str:
    text = str(value).strip().lower()
    if not text:
        return fallback
    text = re.sub(r"\b(?:the|best|reviewed|best reviewed|top)\b", " ", text)
    text = re.sub(r"\b(?:around the country|across the country|across the us|in the us|in us)\b", " ", text)
    text = re.sub(r"\b(?:near|in)\b.*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -,.")
    return text or fallback


def _extract_restaurant_focus(prompt: str, *, is_chain: bool) -> str:
    prompt_lower = prompt.lower()
    patterns = (
        (
            r"(?:what is the |find out what is the |find the |compare |rank |analyze )?"
            r"(?:best reviewed )?(.+?)\s+(?:franchise|chain)\b",
        )
        if is_chain
        else (
            r"(?:compare |rank |analyze |find )?(.+?)\s+restaurants?\b",
            r"(.+?)\s+restaurants?\b",
        )
    )
    for pattern in patterns:
        match = re.search(pattern, prompt_lower)
        if match:
            return _clean_focus_phrase(match.group(1), fallback="restaurant")
    return "restaurant"


def _looks_like_used_vehicle_prompt(prompt_lower: str) -> bool:
    vehicle_terms = (
        "vehicle",
        "vehicles",
        "car",
        "cars",
        "van",
        "cargo van",
        "minivan",
        "mini van",
        "mini-van",
        "suv",
        "truck",
        "pickup",
        "pickup truck",
        "hatchback",
        "sedan",
        "wagon",
        "miata",
        "mx-5",
        "mazda",
        "toyota",
        "honda",
        "ford",
        "chevrolet",
        "chevy",
        "gmc",
        "ram",
        "jeep",
        "subaru",
        "nissan",
        "hyundai",
        "kia",
        "lexus",
        "bmw",
        "audi",
        "mercedes",
        "tesla",
        "volkswagen",
        "vw",
    )
    has_vehicle_term = any(term in prompt_lower for term in vehicle_terms)
    if not has_vehicle_term:
        return False
    if "used" in prompt_lower:
        return True
    valuation_terms = (
        "fair price",
        "market price",
        "market value",
        "fair market value",
        "comps",
        "comparable sales",
        "price of",
    )
    return any(term in prompt_lower for term in valuation_terms)


def _extract_vehicle_focus(prompt: str) -> str:
    prompt_lower = prompt.lower()
    match = re.search(r"used\s+(.+?)(?:\s+near\b|\s+in\b|$)", prompt_lower)
    if match:
        return _clean_focus_phrase(match.group(1), fallback="vehicle")
    match = re.search(
        r"(?:fair price of|market price of|market value of|price of)\s+(.+?)(?:\s+near\b|\s+in\b|$)",
        prompt_lower,
    )
    if match:
        return _clean_focus_phrase(match.group(1), fallback="vehicle")
    return "vehicle"


def _looks_like_rental_prompt(prompt_lower: str) -> bool:
    housing_terms = (
        "apartment",
        "apartments",
        "rental",
        "rentals",
        "for rent",
        "lease",
        "leasing",
        "rent.com",
        "apartments.com",
        "studio apartment",
        "1 bedroom",
        "2 bedroom",
        "bedroom apartment",
    )
    return any(term in prompt_lower for term in housing_terms)


def _looks_like_home_sale_prompt(prompt_lower: str) -> bool:
    if _looks_like_rental_prompt(prompt_lower) or _looks_like_land_prompt(prompt_lower):
        return False
    housing_terms = (
        "home",
        "house",
        "condo",
        "condos",
        "property",
        "properties",
        "townhome",
        "townhouse",
        "zillow",
    )
    if not any(term in prompt_lower for term in housing_terms):
        return False
    sale_terms = (
        "sell",
        "sale",
        "selling",
        "market value",
        "home value",
        "housing market",
        "home prices",
        "price to sell",
        "correct price",
        "fair price",
        "valuation",
        "comp",
        "comps",
        "dropping",
        "falling",
        "down",
        "fastest",
        "market trends",
    )
    return any(term in prompt_lower for term in sale_terms)


def _looks_like_neighborhood_price_prompt(prompt_lower: str) -> bool:
    if _looks_like_rental_prompt(prompt_lower) or _looks_like_land_prompt(prompt_lower):
        return False
    if "neighborhood" not in prompt_lower and "neighborhoods" not in prompt_lower:
        return False
    if any(token in prompt_lower for token in ("zip code", "zip codes", "zipcode", "zipcodes")):
        return False
    expensive_terms = (
        "most expensive",
        "expensive",
        "richest",
        "wealthiest",
        "priciest",
        "highest home value",
        "highest home values",
        "highest property value",
        "highest property values",
        "upscale",
        "luxury",
        "luxurious",
    )
    housing_terms = (
        "home",
        "homes",
        "house",
        "houses",
        "property",
        "real estate",
        "housing",
        "zillow",
        "redfin",
        "price",
        "prices",
    )
    if not any(term in prompt_lower for term in expensive_terms):
        return False
    if any(term in prompt_lower for term in housing_terms):
        return True
    return True


def _looks_like_stock_prompt(prompt_lower: str) -> bool:
    stock_terms = (
        "stock",
        "stocks",
        "equity",
        "equities",
        "ticker",
        "tickers",
        "share",
        "shares",
    )
    if not any(term in prompt_lower for term in stock_terms):
        return False
    if any(term in prompt_lower for term in ("stock photo", "stock photos", "stock image", "stock images")):
        return False
    intent_terms = (
        "invest",
        "investing",
        "buy",
        "outlook",
        "forecast",
        "market condition",
        "market conditions",
        "pick",
        "picks",
        "2026",
        "best",
        "top",
        "cheap",
        "cheapest",
        "lowest",
        "undervalued",
        "worth",
    )
    return any(term in prompt_lower for term in intent_terms)


def _looks_like_prediction_market_prompt(prompt_lower: str) -> bool:
    if "polymarket" in prompt_lower:
        return True
    market_terms = (
        "prediction market",
        "prediction markets",
        "market contract",
        "market contracts",
        "active events",
        "active markets",
        "bet",
        "bets",
        "odds",
    )
    if not any(term in prompt_lower for term in market_terms):
        return False
    signal_terms = (
        "event",
        "events",
        "active",
        "available",
        "current",
        "volume",
        "liquidity",
        "yes",
        "no",
        "probability",
    )
    return any(term in prompt_lower for term in signal_terms)


def _looks_like_land_prompt(prompt_lower: str) -> bool:
    land_terms = (
        "land",
        "lot",
        "lots",
        "parcel",
        "parcels",
        "acre",
        "acres",
        "acreage",
        "vacant lot",
        "vacant land",
    )
    intent_terms = (
        "price",
        "prices",
        "for sale",
        "value",
        "worth",
        "market",
        "listing",
        "listings",
        "near ",
        " in ",
    )
    return any(term in prompt_lower for term in land_terms) and any(term in prompt_lower for term in intent_terms)


def _extract_local_area(prompt: str) -> str:
    prompt_lower = prompt.lower()
    zip_code = _zip_fragment(prompt)
    if zip_code != "target-area":
        return zip_code
    def _trim_area_fragment(value: str) -> str:
        text = _normalize_space(value)
        if not text:
            return "target area"
        text = re.split(
            r"\b(?:have|has|with|using|by|where|that|which|show|showing|compare|and|for)\b",
            text,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" ,.-")
        return _clean_focus_phrase(text, fallback="target area")
    for pattern in (
        r"\bnear\s+(.+?)(?:\s+area\b|[.,]|$)",
        r"\bin\s+(.+?)(?:\s+area\b|[.,]|$)",
    ):
        match = re.search(pattern, prompt_lower)
        if match:
            return _trim_area_fragment(match.group(1))
    return "target area"


def _looks_like_restaurant_chain_prompt(prompt_lower: str) -> bool:
    if not any(token in prompt_lower for token in ("franchise", "chain")):
        return False
    food_terms = (
        "restaurant",
        "restaurants",
        "food",
        "burger",
        "burgers",
        "pizza",
        "taco",
        "tacos",
        "ramen",
        "coffee",
        "fried chicken",
        "chicken",
    )
    return any(term in prompt_lower for term in food_terms)


def _looks_like_coworking_prompt(prompt_lower: str) -> bool:
    coworking_terms = (
        "coworking",
        "co-working",
        "shared office",
        "shared workspace",
        "desk space",
        "desk price",
        "private office",
    )
    return any(term in prompt_lower for term in coworking_terms)


def _coworking_mission_template(prompt: str) -> dict[str, Any]:
    return {
        "objective": "Build a comparable table of coworking spaces using directly observable desk price, ratings, reviews, and location context.",
        "questions": [
            "Which coworking spaces currently show the strongest combination of desk price and review support?",
            "How do monthly desk price, rating, and review count vary across the discovered spaces?",
            "Which spaces still need follow-up because pricing or review evidence is incomplete?",
        ],
        "name": "coworking_spaces",
        "description": "One row per coworking space",
        "grain": "one coworking space",
        "primary_key": ["space_name", "source_url"],
        "measures": ["monthly_price_usd", "rating_value", "review_count"],
        "dimensions": ["city", "state", "neighborhood", "source_domain"],
        "required_columns": ["space_name", "source_url", "monthly_price_usd", "rating_value", "review_count"],
        "min_rows": str(_extract_min_rows(prompt, 20)),
        "source_preferences": [
            {
                "source_type": "review_directory_search",
                "query_hint": "coworking reviews monthly desk price",
                "route_role": "scout",
                "rationale": "Find review-rich directory pages that expose coworking ratings and desk pricing.",
            },
            {
                "source_type": "map_directory_search",
                "query_hint": "coworking near me reviews pricing",
                "route_role": "scout",
                "rationale": "Use map-style discovery to widen local coworking coverage.",
            },
            {
                "source_type": "official_source_search",
                "query_hint": "coworking pricing official",
                "route_role": "scout",
                "rationale": "Pull direct operator pages when directory pricing is incomplete.",
            },
        ],
        "notes": ["local-service-comparison", "coworking"],
    }


def _restaurant_mission_template(prompt: str) -> dict[str, Any]:
    prompt_lower = prompt.lower()
    is_chain = any(
        token in prompt_lower
        for token in ("franchise", "chain", "nationwide", "country", "across the us", "around the country")
    )
    focus = _extract_restaurant_focus(prompt, is_chain=is_chain)
    if "fried chicken" in prompt_lower or is_chain:
        return {
            "objective": "Build a chain-level review table for comparing {focus} chains by rating strength, review volume, and national footprint.".format(
                focus=focus
            ),
            "questions": [
                "Which {focus} chains have the strongest review performance across public platforms?".format(
                    focus=focus
                ),
                "How do rating and review volume compare across the main national {focus} chains?".format(
                    focus=focus
                ),
                "Which {focus} chains have enough national presence to make the comparison meaningful?".format(
                    focus=focus
                ),
            ],
            "name": "restaurant_chains",
            "description": "One row per restaurant chain or franchise brand",
            "grain": "one restaurant chain",
            "primary_key": ["brand_name", "source_platform"],
            "measures": ["rating_value", "review_count", "location_count"],
            "dimensions": ["source_platform", "price_tier", "region_scope"],
            "required_columns": ["brand_name", "rating_value", "review_count", "source_platform", "source_url"],
            "min_rows": "12",
            "source_preferences": [
                {
                    "source_type": "review_directory_search",
                    "query_hint": "brand reviews and ratings",
                    "route_role": "scout",
                    "rationale": "Find public review pages for chain-level rating and review count signals.",
                },
                {
                    "source_type": "map_directory_search",
                    "query_hint": "locations reviews nationwide",
                    "route_role": "scout",
                    "rationale": "Find map and location surfaces that expose national chain coverage.",
                },
                {
                    "source_type": "franchise_directory_search",
                    "query_hint": "locations united states franchise footprint",
                    "route_role": "scout",
                    "rationale": "Find footprint and chain-location evidence for national comparisons.",
                },
            ],
        }
    return {
        "objective": "Build a comparable restaurant table for analyzing {focus} restaurants by price tier, rating, and review volume.".format(
            focus=focus
        ),
        "questions": [
            "Which {focus} restaurants cluster into each price tier?".format(focus=focus),
            "How do rating and review count differ across price tiers?",
            "Which {focus} restaurants stand out as strong review performers within their tier?".format(
                focus=focus
            ),
        ],
        "name": "restaurants",
        "description": "One row per restaurant or restaurant location",
        "grain": "one restaurant",
        "primary_key": ["restaurant_name", "source_url"],
        "measures": ["rating_value", "review_count"],
        "dimensions": ["price_tier", "city", "state"],
        "required_columns": ["restaurant_name", "price_tier", "rating_value", "review_count", "source_url"],
        "min_rows": "25",
        "source_preferences": [
            {
                "source_type": "review_directory_search",
                "query_hint": "reviews ratings price tier",
                "route_role": "scout",
                "rationale": "Find directory pages that expose rating, review count, and price tier.",
            },
            {
                "source_type": "map_directory_search",
                "query_hint": "locations reviews",
                "route_role": "scout",
                "rationale": "Use map-style results to widen candidate coverage across locations.",
            },
            {
                "source_type": "editorial_search",
                "query_hint": "best list shortlist",
                "route_role": "scout",
                "rationale": "Use shortlist or roundup pages for candidate discovery before row gathering.",
            },
        ],
    }


def _vehicle_mission_template(prompt: str) -> dict[str, Any]:
    zip_code = _zip_fragment(prompt)
    vehicle_focus = _extract_vehicle_focus(prompt)
    return {
        "objective": "Build a comparable local inventory of used {vehicle_focus} listings near ZIP {zip_code} using directly observable listing fields.".format(
            vehicle_focus=vehicle_focus,
            zip_code=zip_code,
        ),
        "questions": [
            "Which used {vehicle_focus} listings near ZIP {zip_code} look strongest on price, age, and mileage?".format(
                vehicle_focus=vehicle_focus,
                zip_code=zip_code,
            ),
            "How do price and mileage vary across the main {vehicle_focus} make and model clusters?".format(
                vehicle_focus=vehicle_focus
            ),
            "Which {vehicle_focus} listings need follow-up because key details are missing or inconsistent?".format(
                vehicle_focus=vehicle_focus
            ),
        ],
        "name": "vehicle_listings",
        "description": "One row per used vehicle listing",
        "grain": "one vehicle listing",
        "primary_key": ["source", "listing_id"],
        "measures": ["price_usd", "model_year", "odometer_miles"],
        "dimensions": ["make", "model", "seller_type", "city", "zip_code", "transmission", "fuel_type"],
        "required_columns": ["source", "listing_id", "listing_url", "title", "make", "model", "model_year", "price_usd", "odometer_miles", "city", "zip_code"],
        "min_rows": "25",
        "source_preferences": [
            {
                "source_type": "auto_marketplace_search",
                "query_hint": "for sale used inventory",
                "route_role": "scout",
                "rationale": "Find marketplace inventory pages that expose year, mileage, and price.",
            },
            {
                "source_type": "dealer_inventory_search",
                "query_hint": "dealer inventory used",
                "route_role": "scout",
                "rationale": "Add dealer inventory surfaces for structured listing pages.",
            },
            {
                "source_type": "classified_listing_search",
                "query_hint": "classified listings used",
                "route_role": "scout",
                "rationale": "Broaden the inventory pool with classified listing surfaces.",
            },
        ],
    }


def _mattress_mission_template(prompt: str) -> dict[str, Any]:
    zip_code = _zip_fragment(prompt)
    return {
        "objective": "Compare currently available used mattress listings near ZIP {zip_code} using listing fields that can be observed directly from marketplace pages.".format(zip_code=zip_code),
        "questions": [
            "What used mattresses are currently available near ZIP {zip_code}?".format(zip_code=zip_code),
            "How do price, size, and condition vary across the current local listings?",
            "Which listings need follow-up because condition or pickup details are unclear?",
        ],
        "name": "mattress_listings",
        "description": "One row per mattress listing",
        "grain": "one mattress listing",
        "primary_key": ["source", "listing_id"],
        "measures": ["price_usd"],
        "dimensions": ["condition", "mattress_size", "city", "zip_code", "pickup_available", "delivery_available"],
        "required_columns": ["source", "listing_id", "listing_url", "title", "price_usd", "condition", "mattress_size", "city", "zip_code"],
        "min_rows": "30",
        "source_preferences": [
            {
                "source_type": "local_marketplace_search",
                "query_hint": "for sale used local pickup",
                "route_role": "scout",
                "rationale": "Find local resale surfaces where used mattresses are actually listed.",
            },
            {
                "source_type": "classified_listing_search",
                "query_hint": "classified listings local",
                "route_role": "scout",
                "rationale": "Add second-source local classifieds for better current inventory coverage.",
            },
            {
                "source_type": "community_marketplace_search",
                "query_hint": "community resale used local",
                "route_role": "scout",
                "rationale": "Add a third local-discovery family so the first wave is not underplanned.",
            },
        ],
    }


def _rental_mission_template(prompt: str) -> dict[str, Any]:
    area = _extract_local_area(prompt)
    return {
        "objective": "Build a comparable inventory of current rental apartment listings in {area} using directly observable listing fields.".format(
            area=area
        ),
        "questions": [
            "What apartments for rent are currently available in {area}?".format(area=area),
            "How do price, bedrooms, bathrooms, and square footage vary across the current listings?",
            "Which listings need follow-up because rent, fees, or location details are missing?",
        ],
        "name": "rental_listings",
        "description": "One row per rental apartment listing",
        "grain": "one rental listing",
        "primary_key": ["source", "listing_id"],
        "measures": ["rent_usd", "bedrooms", "bathrooms", "square_feet"],
        "dimensions": ["neighborhood", "city", "zip_code", "property_type"],
        "required_columns": [
            "source",
            "listing_id",
            "listing_url",
            "title",
            "rent_usd",
            "bedrooms",
            "bathrooms",
            "neighborhood",
        ],
        "min_rows": str(_extract_min_rows(prompt, 25)),
        "source_preferences": [
            {
                "source_type": "rental_marketplace_search",
                "query_hint": "apartments for rent current listings",
                "route_role": "scout",
                "rationale": "Find rental listing surfaces that expose rent, beds, baths, and neighborhood.",
            },
            {
                "source_type": "apartment_directory_search",
                "query_hint": "apartment listings current rent",
                "route_role": "scout",
                "rationale": "Add apartment-directory results for broader neighborhood coverage.",
            },
            {
                "source_type": "property_management_search",
                "query_hint": "property management leasing availability",
                "route_role": "scout",
                "rationale": "Add direct leasing and property-management pages for primary listing detail.",
            },
        ],
    }


def _land_mission_template(prompt: str) -> dict[str, Any]:
    area = _extract_local_area(prompt)
    return {
        "objective": "Build a comparable inventory of current land listings in {area} using directly observable listing fields.".format(
            area=area
        ),
        "questions": [
            "What land listings are currently available in {area}?".format(area=area),
            "How do asking price and lot size vary across the current listings?",
            "Which listings need follow-up because acreage, zoning, or location details are missing?",
        ],
        "name": "land_listings",
        "description": "One row per land or lot listing",
        "grain": "one land listing",
        "primary_key": ["source", "listing_id"],
        "measures": ["price_usd", "lot_size_acres"],
        "dimensions": ["city", "state", "zip_code", "property_type", "listing_source"],
        "required_columns": [
            "source",
            "listing_id",
            "listing_url",
            "title",
            "price_usd",
            "city",
            "state",
            "listing_source",
        ],
        "min_rows": str(_extract_min_rows(prompt, 15)),
        "source_preferences": [
            {
                "source_type": "land_marketplace_search",
                "query_hint": "land for sale current listings",
                "route_role": "scout",
                "rationale": "Find land-listing surfaces with asking price, acreage, and location.",
            },
            {
                "source_type": "property_listing_search",
                "query_hint": "lots and land current listings",
                "route_role": "scout",
                "rationale": "Add broader property-listing pages that expose current lot inventory.",
            },
            {
                "source_type": "county_property_search",
                "query_hint": "parcel land listing and assessor context",
                "route_role": "scout",
                "rationale": "Add county or property-record style sources for parcel context and corroboration.",
            },
        ],
    }


def _home_sale_mission_template(prompt: str) -> dict[str, Any]:
    area = _extract_local_area(prompt)
    return {
        "objective": "Build a comparable table of metro-level home-price signals in {area} using directly observable market-trend, valuation, and research pages.".format(
            area=area
        ),
        "questions": [
            "Which metros or areas show the strongest home-price declines or weakest growth in {area}?".format(area=area),
            "Which sources provide market-trend or valuation evidence that can explain those moves?",
            "Which metro-level signals still need follow-up because the price-change evidence is weak or incomplete?",
        ],
        "name": "home_sale_signals",
        "description": "One row per metro-level home-price or market signal from one source page",
        "grain": "one metro market signal",
        "primary_key": ["metro", "source_url", "signal_type"],
        "measures": ["price_signal_usd", "mom_change_pct", "yoy_change_pct", "price_cut_share_pct", "days_on_market"],
        "dimensions": ["city", "state", "signal_type", "market_temperature", "source_domain"],
        "required_columns": [
            "metro",
            "source_url",
            "source_title",
            "signal_type",
            "source_domain",
        ],
        "min_rows": str(_extract_min_rows(prompt, 12)),
        "source_preferences": [
            {
                "source_type": "home_valuation_search",
                "query_hint": "home value estimate market value metro",
                "route_role": "scout",
                "rationale": "Find valuation-style pages that expose metro-level price signals.",
            },
            {
                "source_type": "housing_market_search",
                "query_hint": "housing market home prices trends metros",
                "route_role": "scout",
                "rationale": "Add market-trend pages with recent price-move evidence across metros.",
            },
            {
                "source_type": "real_estate_research_search",
                "query_hint": "real estate research home values metros",
                "route_role": "scout",
                "rationale": "Add research pages that summarize the metros gaining or losing value fastest.",
            },
        ],
    }


def _neighborhood_price_mission_template(prompt: str) -> dict[str, Any]:
    area = _extract_local_area(prompt)
    return {
        "objective": "Build a comparable table of neighborhood-level housing price rankings in {area} using directly observable price fields from roundup, valuation, and research pages.".format(
            area=area
        ),
        "questions": [
            "Which neighborhoods in {area} show the highest directly reported home-price signals?".format(area=area),
            "How do reported price levels differ across the gathered neighborhood ranking sources?",
            "Which neighborhoods still need follow-up because the price metric or source support is incomplete?",
        ],
        "name": "neighborhood_price_rankings",
        "description": "One row per neighborhood price ranking from one source page",
        "grain": "one neighborhood ranking row",
        "primary_key": ["neighborhood_name", "source_url"],
        "measures": ["rank", "reported_price_value"],
        "dimensions": ["city", "state", "metric_label", "source_domain"],
        "required_columns": ["neighborhood_name", "source_url", "reported_price_value"],
        "min_rows": str(_extract_min_rows(prompt, 8)),
        "source_preferences": [
            {
                "source_type": "official_source_search",
                "query_hint": "{area} neighborhoods home values".format(area=area),
                "site_hint": "zillow.com",
                "route_role": "scout",
                "rationale": "Use Zillow neighborhood- and valuation-style pages when they expose direct price signals.",
            },
            {
                "source_type": "official_source_search",
                "query_hint": "{area} neighborhoods median sale price".format(area=area),
                "site_hint": "redfin.com",
                "route_role": "scout",
                "rationale": "Use Redfin market or neighborhood pages when they expose direct sale-price signals.",
            },
            {
                "source_type": "web_search_query",
                "query_hint": "{area} most expensive neighborhoods median home price".format(area=area),
                "route_role": "scout",
                "rationale": "Find roundup and market pages that explicitly name neighborhoods and report the price metric used.",
            },
        ],
        "notes": ["housing-neighborhood-pricing"],
    }


def _product_mission_template(prompt: str) -> dict[str, Any]:
    prompt_lower = prompt.lower()
    infant = "infant" in prompt_lower or "baby" in prompt_lower
    age_range = "6-12 months" if "6month" in prompt_lower or "6 month" in prompt_lower else "infant"
    objective = "Build a comparable product table for {target} using retailer-visible price, review, and age-fit fields.".format(
        target="baby toys" if infant else "consumer products"
    )
    dimensions = ["brand", "retailer", "category"]
    required_columns = ["product_name", "brand", "retailer", "product_url", "price_value", "rating_value", "review_count"]
    notes = []
    if infant:
        dimensions.append("age_range_text")
        required_columns.append("age_range_text")
        notes.append("target-age:{age_range}".format(age_range=age_range))
    return {
        "objective": objective,
        "questions": [
            "Which products best fit the target age range and use case?",
            "How do price, rating, and review volume compare across the strongest options?",
            "Which brands or categories dominate the best-supported choices?",
        ],
        "name": "products",
        "description": "One row per product",
        "grain": "one product",
        "primary_key": ["product_name", "retailer", "product_url"],
        "measures": ["price_value", "rating_value", "review_count"],
        "dimensions": dimensions,
        "required_columns": required_columns,
        "min_rows": str(_extract_min_rows(prompt, 100)),
        "source_preferences": [
            {
                "source_type": "retailer_search",
                "query_hint": "shopping results product listings",
                "route_role": "scout",
                "rationale": "Find retailer result pages with price, review count, and age-fit information.",
            },
            {
                "source_type": "review_comparison_search",
                "query_hint": "product reviews comparison",
                "route_role": "scout",
                "rationale": "Find review-heavy comparisons to widen product discovery before gather.",
            },
            {
                "source_type": "brand_catalog_search",
                "query_hint": "brand catalog official product range",
                "route_role": "scout",
                "rationale": "Find brand or catalog pages that can reveal missing product variants.",
            },
        ],
        "notes": notes,
    }


def _prediction_market_mission_template(prompt: str) -> dict[str, Any]:
    prompt_lower = prompt.lower()
    active_only = any(token in prompt_lower for token in ("active", "currently active", "available events", "open markets"))
    seed_urls = [
        "https://kalshi.com/market-data",
        "https://polymarket.com/predictions/all",
    ]
    if any(token in prompt_lower for token in ("nfl", "super bowl", "nba", "mlb", "sports", "quarterback", "draft", "free agency")):
        seed_urls.append("https://polymarket.com/sports/live")
    else:
        seed_urls.append("https://polymarket.com/predictions/current-events")
    objective = (
        "Build a comparable table of active prediction-market contracts so the current market set can be analyzed before selecting a bet."
        if active_only
        else "Build a comparable table of prediction-market contracts and event markets from currently available public market pages."
    )
    return {
        "objective": objective,
        "questions": [
            "Which active market contracts are currently available across the gathered prediction-market pages?",
            "How do yes/no prices, liquidity, and volume differ across the active markets?",
            "Which markets need follow-up because pricing, liquidity, or status evidence is incomplete?",
        ],
        "name": "market_contracts",
        "description": "One row per active prediction-market contract or event market",
        "grain": "one market contract",
        "primary_key": ["platform", "market_url"],
        "measures": ["yes_price", "no_price", "volume_usd", "liquidity_usd"],
        "dimensions": ["platform", "event_category", "market_status", "resolution_date"],
        "required_columns": ["platform", "market_title", "market_url", "market_status", "yes_price", "volume_usd"],
        "min_rows": str(_extract_min_rows(prompt, 20)),
        "seed_urls": seed_urls,
        "source_preferences": [
            {
                "source_type": "prediction_market_search",
                "query_hint": "active markets current events",
                "route_role": "scout",
                "rationale": "Find live market pages that expose active contracts, pricing, and market status.",
            },
            {
                "source_type": "market_analytics_search",
                "query_hint": "prediction market analytics liquidity volume",
                "route_role": "scout",
                "rationale": "Add analytics pages that expose liquidity, volume, and market snapshots.",
            },
            {
                "source_type": "official_source_search",
                "query_hint": "active markets official source",
                "route_role": "scout",
                "rationale": "Prefer direct market pages before shaping event-level rows.",
            },
        ],
        "notes": ["prediction-markets", "active-market-scan"],
    }


GENERIC_MEASURE_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("price", "cost", "value", "usd", "dollar", "dollars"), "price_value"),
    (("rent", "rents"), "rent_usd"),
    (("rating", "ratings", "score", "scores"), "rating_value"),
    (("review", "reviews"), "review_count"),
    (("star", "stars"), "star_count"),
    (("volume",), "volume_usd"),
    (("liquidity",), "liquidity_usd"),
    (("odds", "probability", "probabilities", "chance", "chances"), "implied_probability"),
    (("yes", "yes price"), "yes_price"),
    (("no", "no price"), "no_price"),
]


GENERIC_DIMENSION_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("active", "open", "available", "latest", "current"), "status"),
    (("category", "categories", "sector", "sectors"), "category"),
    (("platform",), "platform"),
    (("license", "licensing"), "license_type"),
]


GENERIC_PROMPT_STOPWORDS = STOPWORDS | {
    "active",
    "available",
    "current",
    "currently",
    "evaluate",
    "first",
    "firstly",
    "latest",
    "look",
    "looks",
    "closely",
    "place",
    "set",
    "should",
    "want",
}


def _singularize_token(token: str) -> str:
    text = str(token).strip().lower()
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("ses") and len(text) > 4:
        return text[:-2]
    if text.endswith("s") and not text.endswith("ss") and len(text) > 3:
        return text[:-1]
    return text


def _generic_subject_tokens(prompt: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", prompt.lower())
    filtered = [
        token
        for token in tokens
        if token not in GENERIC_PROMPT_STOPWORDS
        and not token.isdigit()
        and len(token) > 2
    ]
    if not filtered:
        return ["entities"]
    return filtered[:3]


def _generic_object_name_from_prompt(prompt: str) -> str:
    prompt_lower = prompt.lower()
    if _looks_like_prediction_market_prompt(prompt_lower):
        return "market_contracts"
    subject_tokens = _generic_subject_tokens(prompt)
    if not subject_tokens:
        return "entities"
    tail = subject_tokens[-1]
    if tail.endswith("s"):
        return "_".join(subject_tokens)
    return "_".join([*subject_tokens[:-1], tail + "s"])


def _generic_name_field(prompt: str, object_name: str) -> str:
    prompt_lower = prompt.lower()
    if object_name == "market_contracts":
        return "market_title"
    if "event" in prompt_lower:
        return "event_name"
    if any(token in prompt_lower for token in ("company", "companies", "brand", "brands")):
        return "entity_name"
    return "entity_name"


def _generic_required_and_optional_fields(prompt: str, *, name_field: str) -> tuple[list[str], list[str], list[str]]:
    prompt_lower = prompt.lower()
    measures = [
        field
        for keywords, field in GENERIC_MEASURE_HINTS
        if any(keyword in prompt_lower for keyword in keywords)
    ]
    dimensions = [
        field
        for keywords, field in GENERIC_DIMENSION_HINTS
        if any(keyword in prompt_lower for keyword in keywords)
    ]
    if not dimensions:
        dimensions = ["source_domain"]
    required_columns = [name_field, "source_url"]
    if any(field in measures for field in ("yes_price", "no_price", "volume_usd", "liquidity_usd", "implied_probability")):
        required_columns.insert(0, "platform")
        dimensions = _ordered_unique(["platform", *dimensions])
    for field in measures[:2]:
        if field not in required_columns:
            required_columns.append(field)
    for field in dimensions[:1]:
        if field not in required_columns and field != "source_domain":
            required_columns.append(field)
    return _ordered_unique(measures), _ordered_unique(dimensions), _ordered_unique(required_columns)


def _generic_prompt_mission_template(prompt: str) -> dict[str, Any]:
    object_name = _generic_object_name_from_prompt(prompt)
    if object_name == "market_contracts":
        return _prediction_market_mission_template(prompt)
    name_field = _generic_name_field(prompt, object_name)
    measures, dimensions, required_columns = _generic_required_and_optional_fields(prompt, name_field=name_field)
    object_label = object_name.replace("_", " ")
    singular_label = _singularize_token(object_label.split()[-1])
    questions = [
        "What {label} can be shaped from the currently gathered sources?".format(label=object_label),
        "Which source pages expose the strongest repeated fields for these {label}?".format(label=object_label),
        "What additional fields or follow-ups are needed before stronger analysis can begin?",
    ]
    if measures:
        questions[1] = "How do {fields} vary across the current {label}?".format(
            fields=", ".join(measures[:2]).replace("_", " "),
            label=object_label,
        )
    return {
        "objective": "Build a first-pass structured table of {label} from the prompt using directly observable fields before deeper analysis.".format(
            label=object_label
        ),
        "questions": questions,
        "name": object_name,
        "description": "One row per {label} from one source".format(label=singular_label),
        "grain": "one {label}".format(label=singular_label),
        "primary_key": [name_field, "source_url"],
        "measures": measures,
        "dimensions": dimensions,
        "required_columns": required_columns,
        "min_rows": str(_extract_min_rows(prompt, 12)),
        "seed_urls": [],
        "source_preferences": [
            {
                "source_type": "web_search_query",
                "query_hint": object_label,
                "route_role": "scout",
                "rationale": "Start broad and discover sources that expose repeated {label} rows.".format(label=object_label),
            },
            {
                "source_type": "official_source_search",
                "query_hint": "{label} official source".format(label=object_label),
                "route_role": "scout",
                "rationale": "Prefer direct or primary pages once the first candidate sources are known.",
            },
        ],
        "notes": ["generic-prompt-schema", "minimal-observable-schema"],
    }


def _stock_mission_template(prompt: str) -> dict[str, Any]:
    year_match = re.search(r"\b(20\d{2})\b", prompt)
    outlook_year = year_match.group(1) if year_match else "the current outlook"
    return {
        "objective": "Build a comparable stock-candidate table from public investment research pages using directly observable security names, tickers, and source evidence for {year}.".format(
            year=outlook_year
        ),
        "questions": [
            "Which stock candidates recur across the gathered investment sources?",
            "Which tickers are mentioned most credibly across independent sources?",
            "Which candidates still need follow-up because the ticker or security name evidence is weak?",
        ],
        "name": "stock_candidates",
        "description": "One row per stock candidate mention from one source",
        "grain": "one stock candidate mention",
        "primary_key": ["ticker", "source_url"],
        "measures": [],
        "dimensions": ["source_domain", "asset_type"],
        "required_columns": ["security_name", "ticker", "source_url", "source_domain"],
        "min_rows": str(_extract_min_rows(prompt, 12)),
        "source_preferences": [
            {
                "source_type": "financial_publisher_search",
                "query_hint": "best stocks to buy outlook",
                "route_role": "scout",
                "rationale": "Find public investing roundups and publisher analysis pages that name specific stocks.",
            },
            {
                "source_type": "equity_research_search",
                "query_hint": "stock picks outlook analyst picks",
                "route_role": "scout",
                "rationale": "Find research-style pages that expose named stock candidates and tickers.",
            },
            {
                "source_type": "market_commentary_search",
                "query_hint": "top stocks outlook commentary",
                "route_role": "scout",
                "rationale": "Broaden the first pass with commentary pages that still expose named stock mentions.",
            },
        ],
        "notes": ["security-type:stocks"],
    }


def _looks_like_general_product_prompt(prompt_lower: str) -> bool:
    comparison_cues = (
        "compare",
        "comparison",
        "best",
        "top",
        "shortlist",
    )
    shopping_cues = (
        "under ",
        "dollar",
        "$",
        "price",
        "prices",
        "review",
        "reviews",
        "rating",
        "ratings",
        "brand",
        "brands",
        "buy",
        "buying",
    )
    return any(token in prompt_lower for token in comparison_cues) and any(
        token in prompt_lower for token in shopping_cues
    )


def _clean_source_preferences(values: Any) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    if not isinstance(values, list):
        return cleaned
    for item in values:
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type", "")).strip()
        if not source_type:
            continue
        entry = {
            "source_type": source_type,
            "query_hint": str(item.get("query_hint", "")).strip(),
            "site_hint": str(item.get("site_hint", "")).strip(),
            "route_role": str(item.get("route_role", "")).strip() or "scout",
            "rationale": str(item.get("rationale", "")).strip(),
        }
        cleaned.append(entry)
    return cleaned


def _default_source_preferences_for_target(
    target_name: str,
    *,
    prompt: str = "",
) -> list[dict[str, str]]:
    clean_target = str(target_name).strip()
    if clean_target == "districts":
        return [
            {
                "source_type": "district_profile_search",
                "query_hint": "district profile",
                "route_role": "scout",
                "rationale": "Scout profile-style sources that summarize the district clearly.",
            },
            {
                "source_type": "official_site_search",
                "query_hint": "official district site",
                "route_role": "scout",
                "rationale": "Find primary district sources before shaping a final district row.",
            },
            {
                "source_type": "state_report_card_search",
                "query_hint": "state report card accountability",
                "route_role": "scout",
                "rationale": "Find state accountability pages for comparable district metrics.",
            },
        ]
    if clean_target == "restaurants":
        return [
            {
                "source_type": "review_directory_search",
                "query_hint": "reviews ratings price tier",
                "route_role": "scout",
                "rationale": "Find review-rich restaurant directory pages.",
            },
            {
                "source_type": "map_directory_search",
                "query_hint": "locations reviews",
                "route_role": "scout",
                "rationale": "Use map-style discovery to widen location coverage.",
            },
            {
                "source_type": "editorial_search",
                "query_hint": "best list shortlist",
                "route_role": "scout",
                "rationale": "Use shortlist pages for candidate expansion before gather.",
            },
        ]
    if clean_target == "coworking_spaces":
        return [
            {
                "source_type": "review_directory_search",
                "query_hint": "coworking reviews monthly desk price",
                "route_role": "scout",
                "rationale": "Find review-rich directory pages that expose coworking ratings and desk pricing.",
            },
            {
                "source_type": "map_directory_search",
                "query_hint": "coworking near me reviews pricing",
                "route_role": "scout",
                "rationale": "Use map-style discovery to widen local coworking coverage.",
            },
            {
                "source_type": "official_source_search",
                "query_hint": "coworking pricing official",
                "route_role": "scout",
                "rationale": "Pull direct operator pages when directory pricing is incomplete.",
            },
        ]
    if clean_target == "restaurant_chains":
        return [
            {
                "source_type": "review_directory_search",
                "query_hint": "brand reviews and ratings",
                "route_role": "scout",
                "rationale": "Find public review pages for chain-level rating signals.",
            },
            {
                "source_type": "map_directory_search",
                "query_hint": "locations reviews nationwide",
                "route_role": "scout",
                "rationale": "Find location surfaces that expose national chain coverage.",
            },
            {
                "source_type": "franchise_directory_search",
                "query_hint": "locations united states franchise footprint",
                "route_role": "scout",
                "rationale": "Find footprint evidence to support national chain comparisons.",
            },
        ]
    if clean_target == "vehicle_listings":
        return [
            {
                "source_type": "auto_marketplace_search",
                "query_hint": "for sale used inventory",
                "route_role": "scout",
                "rationale": "Find marketplace inventory pages with year, mileage, and price.",
            },
            {
                "source_type": "dealer_inventory_search",
                "query_hint": "dealer inventory used",
                "route_role": "scout",
                "rationale": "Add dealer inventory surfaces for more structured listing pages.",
            },
            {
                "source_type": "classified_listing_search",
                "query_hint": "classified listings used",
                "route_role": "scout",
                "rationale": "Broaden the inventory pool with classified listing surfaces.",
            },
        ]
    if clean_target == "mattress_listings":
        return [
            {
                "source_type": "local_marketplace_search",
                "query_hint": "for sale used local pickup",
                "route_role": "scout",
                "rationale": "Find local resale surfaces where used mattresses are listed.",
            },
            {
                "source_type": "classified_listing_search",
                "query_hint": "classified listings local",
                "route_role": "scout",
                "rationale": "Add second-source local classifieds for better current inventory coverage.",
            },
            {
                "source_type": "community_marketplace_search",
                "query_hint": "community resale used local",
                "route_role": "scout",
                "rationale": "Add a third local-discovery family so the first wave is not underplanned.",
            },
        ]
    if clean_target == "rental_listings":
        return [
            {
                "source_type": "rental_marketplace_search",
                "query_hint": "apartments for rent current listings",
                "route_role": "scout",
                "rationale": "Find rental listing surfaces with rent, beds, baths, and neighborhood signals.",
            },
            {
                "source_type": "apartment_directory_search",
                "query_hint": "apartment listings current rent",
                "route_role": "scout",
                "rationale": "Add apartment directories for broader neighborhood coverage.",
            },
            {
                "source_type": "property_management_search",
                "query_hint": "property management leasing availability",
                "route_role": "scout",
                "rationale": "Add direct leasing pages for primary listing detail.",
            },
        ]
    if clean_target == "land_listings":
        return [
            {
                "source_type": "land_marketplace_search",
                "query_hint": "land for sale current listings",
                "route_role": "scout",
                "rationale": "Find land-listing surfaces with asking price, acreage, and location.",
            },
            {
                "source_type": "property_listing_search",
                "query_hint": "lots and land current listings",
                "route_role": "scout",
                "rationale": "Add broader property-listing pages that expose current lot inventory.",
            },
            {
                "source_type": "county_property_search",
                "query_hint": "parcel land listing and assessor context",
                "route_role": "scout",
                "rationale": "Add county or property-record style sources for parcel context and corroboration.",
            },
        ]
    if clean_target == "home_sale_signals":
        return [
            {
                "source_type": "home_valuation_search",
                "query_hint": "home value estimate market value metro",
                "route_role": "scout",
                "rationale": "Find valuation-style pages that expose metro-level price signals.",
            },
            {
                "source_type": "housing_market_search",
                "query_hint": "housing market home prices trends metros",
                "route_role": "scout",
                "rationale": "Add market-trend pages with recent price-move evidence across metros.",
            },
            {
                "source_type": "real_estate_research_search",
                "query_hint": "real estate research home values metros",
                "route_role": "scout",
                "rationale": "Add research pages that summarize which metros are gaining or losing value fastest.",
            },
        ]
    if clean_target == "neighborhood_price_rankings":
        area = _extract_local_area(prompt)
        return [
            {
                "source_type": "official_source_search",
                "query_hint": "{area} neighborhoods home values".format(area=area),
                "site_hint": "zillow.com",
                "route_role": "scout",
                "rationale": "Use Zillow neighborhood- and valuation-style pages when they expose direct price signals.",
            },
            {
                "source_type": "official_source_search",
                "query_hint": "{area} neighborhoods median sale price".format(area=area),
                "site_hint": "redfin.com",
                "route_role": "scout",
                "rationale": "Use Redfin neighborhood or market pages when they expose direct sale-price signals.",
            },
            {
                "source_type": "web_search_query",
                "query_hint": "{area} most expensive neighborhoods median home price".format(area=area),
                "route_role": "scout",
                "rationale": "Find roundup pages that explicitly name neighborhoods and report the pricing metric used.",
            },
        ]
    if clean_target == "products":
        return [
            {
                "source_type": "retailer_search",
                "query_hint": "shopping results product listings",
                "route_role": "scout",
                "rationale": "Find retailer result pages with price, review count, and mission-fit signals.",
            },
            {
                "source_type": "review_comparison_search",
                "query_hint": "product reviews comparison",
                "route_role": "scout",
                "rationale": "Find review-heavy comparisons to widen product discovery before gather.",
            },
            {
                "source_type": "brand_catalog_search",
                "query_hint": "brand catalog official product range",
                "route_role": "scout",
                "rationale": "Find brand or catalog pages that can reveal missing variants.",
            },
        ]
    if clean_target == "stock_candidates":
        return [
            {
                "source_type": "financial_publisher_search",
                "query_hint": "best stocks to buy outlook",
                "route_role": "scout",
                "rationale": "Find investing publisher pages that name specific stocks and tickers.",
            },
            {
                "source_type": "equity_research_search",
                "query_hint": "stock picks outlook analyst picks",
                "route_role": "scout",
                "rationale": "Add research-style pages that expose named stock candidates.",
            },
            {
                "source_type": "market_commentary_search",
                "query_hint": "top stocks outlook commentary",
                "route_role": "scout",
                "rationale": "Broaden first-pass stock discovery beyond one publisher style.",
            },
        ]
    if clean_target == "market_contracts":
        return [
            {
                "source_type": "prediction_market_search",
                "query_hint": "active markets current events",
                "route_role": "scout",
                "rationale": "Find live market boards that expose active events and prices directly.",
            },
            {
                "source_type": "market_analytics_search",
                "query_hint": "prediction market analytics liquidity volume",
                "route_role": "scout",
                "rationale": "Add analytics pages with liquidity, volume, and event snapshots.",
            },
            {
                "source_type": "official_source_search",
                "query_hint": "active markets official source",
                "route_role": "scout",
                "rationale": "Prefer direct market pages before shaping active contract rows.",
            },
        ]
    if clean_target == "listings":
        query_hint = "current marketplace listings"
        if _contains_keyword(prompt, "buy-it-now") or _contains_keyword(prompt, "buy it now"):
            query_hint = "buy it now marketplace listings"
        return [
            {
                "source_type": "marketplace_search_results",
                "query_hint": query_hint,
                "route_role": "scout",
                "rationale": "Find current marketplace result pages that expose listing rows directly.",
            },
            {
                "source_type": "marketplace_search_results",
                "query_hint": "current used marketplace inventory",
                "route_role": "scout",
                "rationale": "Broaden discovery across a second listing-style query surface.",
            },
            {
                "source_type": "marketplace_search_results",
                "query_hint": "current sale listings with filters",
                "route_role": "scout",
                "rationale": "Add another listing-style route to increase expected row yield.",
            },
        ]
    return [
        {
            "source_type": "web_search_query",
            "query_hint": "",
            "route_role": "scout",
            "rationale": "Start broad and let Scout discover the first viable sources.",
        },
        {
            "source_type": "official_source_search",
            "query_hint": "official source",
            "route_role": "scout",
            "rationale": "Look for primary sources before shaping rows.",
        },
    ]


def normalize_mission_plan(user_prompt: str, mission_plan: dict[str, Any]) -> dict[str, Any]:
    prompt = re.sub(r"\s+", " ", str(user_prompt or "")).strip()
    prompt_lower = prompt.lower()
    existing = dict(mission_plan or {})

    template: Optional[dict[str, Any]] = None
    if any(token in prompt_lower for token in ("restaurant", "restaurants", "burger", "burgers", "fried chicken", "eatery", "food")) or _looks_like_restaurant_chain_prompt(prompt_lower):
        template = _restaurant_mission_template(prompt)
    elif _looks_like_prediction_market_prompt(prompt_lower):
        template = _prediction_market_mission_template(prompt)
    elif _looks_like_stock_prompt(prompt_lower):
        template = _stock_mission_template(prompt)
    elif _looks_like_used_vehicle_prompt(prompt_lower):
        template = _vehicle_mission_template(prompt)
    elif _looks_like_coworking_prompt(prompt_lower):
        template = _coworking_mission_template(prompt)
    elif "mattress" in prompt_lower and "used" in prompt_lower:
        template = _mattress_mission_template(prompt)
    elif _looks_like_land_prompt(prompt_lower):
        template = _land_mission_template(prompt)
    elif _looks_like_neighborhood_price_prompt(prompt_lower):
        template = _neighborhood_price_mission_template(prompt)
    elif _looks_like_home_sale_prompt(prompt_lower):
        template = _home_sale_mission_template(prompt)
    elif _looks_like_rental_prompt(prompt_lower):
        template = _rental_mission_template(prompt)
    elif any(token in prompt_lower for token in ("toy", "toys", "baby", "infant")):
        template = _product_mission_template(prompt)
    elif _looks_like_general_product_prompt(prompt_lower):
        template = _product_mission_template(prompt)

    if template is None and mission_plan_is_low_information(existing):
        template = _generic_prompt_mission_template(prompt)

    if template is None:
        normalized = dict(existing)
        normalized["notes"] = _merge_plan_notes(existing.get("notes"), "mission-plan-normalized")
        normalized["source_preferences"] = _clean_source_preferences(existing.get("source_preferences"))
        return normalized

    sanitized = dict(template)
    template_name = str(template.get("name", "")).strip()
    existing_name = str(existing.get("name", "")).strip()
    preserve_existing_strategy = (
        bool(existing_name)
        and existing_name == template_name
        and existing_name != "records"
    )
    seed_urls = [str(item).strip() for item in existing.get("seed_urls", []) if str(item).strip()]
    if seed_urls:
        sanitized["seed_urls"] = seed_urls
    else:
        sanitized["seed_urls"] = list(template.get("seed_urls", []))
    sanitized["notes"] = _merge_plan_notes(
        existing.get("notes") if preserve_existing_strategy else [],
        *template.get("notes", []),
        "mission-plan-normalized",
        "minimal-observable-schema",
    )
    existing_preferences = (
        _clean_source_preferences(existing.get("source_preferences"))
        if preserve_existing_strategy
        else []
    )
    if existing_preferences:
        sanitized["source_preferences"] = existing_preferences
    else:
        sanitized["source_preferences"] = _clean_source_preferences(template.get("source_preferences"))
    return sanitized


def _generic_target_object() -> dict[str, Any]:
    return {
        "name": "records",
        "description": "One row per structured record derived from captured sources.",
        "grain": "one structured record",
        "primary_key": [],
        "measures": [],
        "dimensions": [],
        "required_columns": [],
        "sample_target": {"min_rows": 1},
    }


def mission_plan_is_low_information(payload: dict[str, Any]) -> bool:
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip().lower()
    grain = str(payload.get("grain", "")).strip().lower()
    primary_key = _clean_string_list(payload.get("primary_key"))
    measures = _clean_string_list(payload.get("measures"))
    dimensions = _clean_string_list(payload.get("dimensions"))
    required_columns = _clean_string_list(payload.get("required_columns"))
    source_preferences = _clean_source_preferences(payload.get("source_preferences"))
    sample_target = dict(payload.get("sample_target") or {})
    sample_min_rows = 0
    try:
        sample_min_rows = int(sample_target.get("min_rows", 0) or 0)
    except (TypeError, ValueError):
        sample_min_rows = 0
    concrete_names = {
        "coworking_spaces",
        "districts",
        "land_listings",
        "listings",
        "market_contracts",
        "mattress_listings",
        "neighborhood_price_rankings",
        "products",
        "rental_listings",
        "restaurant_chains",
        "restaurants",
        "stock_candidates",
        "vehicle_listings",
        "home_sale_signals",
    }
    if name in {"", "records"}:
        return True
    if required_columns:
        return False
    if primary_key or measures or dimensions:
        return False
    if name in concrete_names and sample_min_rows > 1:
        return False
    if name not in concrete_names:
        return True
    generic_grain = grain in {"", "one structured record"}
    generic_description = (
        not description
        or "structured record derived from captured sources" in description
        or description.startswith("one row per")
    )
    generic_sources = {
        str(item.get("source_type", "")).strip()
        for item in source_preferences
    }
    if name in concrete_names and generic_sources and not generic_sources.issubset({"", "web_search_query", "official_source_search"}):
        return False
    return generic_grain and generic_description and generic_sources.issubset({"", "web_search_query", "official_source_search"})


ROW_SCHEMA_DEFAULTS: dict[str, dict[str, Any]] = {
    "records": {
        "row_signature": ["structured record", "observable source fields", "repeatable row extraction"],
        "positive_page_signals": ["page exposes repeated structured facts", "page contains row-like entities"],
        "negative_page_signals": ["search engine page", "login wall", "empty page"],
        "optional_fields": [],
        "quality_rules": {"min_required_field_coverage": 0.85},
        "page_classes_to_pursue": ["structured_result_page", "detail_page"],
        "page_classes_to_avoid": ["search_engine_page", "blocked_page", "login_wall"],
        "schema_confidence": "low",
    },
    "products": {
        "row_signature": ["named product", "observable price", "retailer-backed listing or product detail page"],
        "positive_page_signals": ["retailer result grid", "product detail page", "brand catalog page"],
        "negative_page_signals": ["search engine page", "editorial roundup", "generic category hub", "blocked page"],
        "optional_fields": ["brand", "category", "age_range_text", "rating_value", "review_count"],
        "quality_rules": {"min_required_field_coverage": 0.9, "prefer_direct_detail_pages": True},
        "page_classes_to_pursue": ["retailer_result_grid", "product_detail_page", "brand_catalog_page"],
        "page_classes_to_avoid": ["search_engine_page", "editorial_roundup", "generic_category_hub", "blocked_page"],
        "schema_confidence": "medium",
    },
    "listings": {
        "row_signature": ["one marketplace listing", "observable title", "observable price or listing URL"],
        "positive_page_signals": ["marketplace search results", "listing detail page"],
        "negative_page_signals": ["search engine page", "generic article", "blocked page"],
        "optional_fields": ["condition", "shipping_text", "seller_name"],
        "quality_rules": {"min_required_field_coverage": 0.9},
        "page_classes_to_pursue": ["marketplace_results_page", "listing_detail_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_article", "blocked_page"],
        "schema_confidence": "high",
    },
    "vehicle_listings": {
        "row_signature": ["one vehicle listing", "observable price", "observable year or mileage"],
        "positive_page_signals": ["dealer inventory page", "classified listing page", "marketplace results page"],
        "negative_page_signals": ["search engine page", "dealer homepage", "generic buying guide", "blocked page"],
        "optional_fields": ["make", "model", "trim", "odometer_miles", "city", "state"],
        "quality_rules": {"min_required_field_coverage": 0.9, "prefer_listing_pages": True},
        "page_classes_to_pursue": ["vehicle_results_page", "vehicle_detail_page", "dealer_inventory_page"],
        "page_classes_to_avoid": ["search_engine_page", "dealer_homepage", "generic_buying_guide", "blocked_page"],
        "schema_confidence": "medium",
    },
    "mattress_listings": {
        "row_signature": ["one mattress listing", "observable price", "observable size or condition"],
        "positive_page_signals": ["classified listing page", "marketplace result grid", "local resale detail page"],
        "negative_page_signals": ["search engine page", "generic furniture category page", "blocked page"],
        "optional_fields": ["mattress_size", "condition", "city", "state"],
        "quality_rules": {"min_required_field_coverage": 0.9},
        "page_classes_to_pursue": ["listing_results_page", "listing_detail_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_category_hub", "blocked_page"],
        "schema_confidence": "medium",
    },
    "rental_listings": {
        "row_signature": ["one rental listing", "observable rent", "observable location"],
        "positive_page_signals": ["apartment results page", "rental detail page", "property management listing page"],
        "negative_page_signals": ["search engine page", "neighborhood guide", "generic market article", "blocked page"],
        "optional_fields": ["bedrooms", "bathrooms", "sqft", "listing_source"],
        "quality_rules": {"min_required_field_coverage": 0.9},
        "page_classes_to_pursue": ["rental_results_page", "rental_detail_page", "property_management_listing_page"],
        "page_classes_to_avoid": ["search_engine_page", "neighborhood_guide", "generic_market_article", "blocked_page"],
        "schema_confidence": "medium",
    },
    "land_listings": {
        "row_signature": ["one land listing", "observable asking price", "observable parcel or lot context"],
        "positive_page_signals": ["land listing page", "property listing results", "parcel listing detail"],
        "negative_page_signals": ["search engine page", "generic local guide", "assessor homepage", "blocked page"],
        "optional_fields": ["lot_size_acres", "city", "state", "zip_code", "listing_source"],
        "quality_rules": {"min_required_field_coverage": 0.9},
        "page_classes_to_pursue": ["land_results_page", "land_detail_page", "parcel_listing_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_local_guide", "assessor_homepage", "blocked_page"],
        "schema_confidence": "medium",
    },
    "home_sale_signals": {
        "row_signature": ["one metro market signal", "observable metro", "observable home-price or market-trend metric"],
        "positive_page_signals": ["market trend page", "valuation research page", "home value article with metro rows"],
        "negative_page_signals": ["search engine page", "generic homebuying guide", "listing detail page", "blocked page"],
        "optional_fields": ["price_signal_usd", "mom_change_pct", "yoy_change_pct", "price_cut_share_pct", "market_temperature"],
        "quality_rules": {"min_required_field_coverage": 0.85},
        "page_classes_to_pursue": ["market_trend_page", "valuation_research_page", "metro_price_article"],
        "page_classes_to_avoid": ["search_engine_page", "generic_homebuying_guide", "listing_detail_page", "blocked_page"],
        "schema_confidence": "medium",
    },
    "neighborhood_price_rankings": {
        "row_signature": ["one neighborhood ranking row", "observable neighborhood name", "observable home-price metric"],
        "positive_page_signals": ["neighborhood ranking page", "home value roundup", "market article with neighborhood prices"],
        "negative_page_signals": ["search engine page", "rental article", "social media shell", "blocked page"],
        "optional_fields": ["rank", "city", "state", "metric_label", "source_domain"],
        "quality_rules": {"min_required_field_coverage": 0.9, "prefer_direct_price_pages": True},
        "page_classes_to_pursue": ["neighborhood_ranking_page", "valuation_research_page", "metro_price_article"],
        "page_classes_to_avoid": ["search_engine_page", "rental_article", "social_media_shell", "blocked_page"],
        "schema_confidence": "medium",
    },
    "restaurants": {
        "row_signature": ["one restaurant", "observable rating", "observable price tier or review volume"],
        "positive_page_signals": ["review directory page", "map directory page", "restaurant profile page"],
        "negative_page_signals": ["search engine page", "generic city guide", "homepage without listings", "blocked page"],
        "optional_fields": ["price_tier", "city", "state", "review_count"],
        "quality_rules": {"min_required_field_coverage": 0.9},
        "page_classes_to_pursue": ["review_directory_page", "restaurant_profile_page", "map_results_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_city_guide", "homepage_without_listings", "blocked_page"],
        "schema_confidence": "medium",
    },
    "restaurant_chains": {
        "row_signature": ["one restaurant chain mention", "observable brand name", "observable review or footprint evidence"],
        "positive_page_signals": ["brand profile page", "review directory page", "chain locations page"],
        "negative_page_signals": ["search engine page", "generic fast food roundup", "blocked page"],
        "optional_fields": ["rating_value", "review_count", "locations_count", "source_platform"],
        "quality_rules": {"min_required_field_coverage": 0.85},
        "page_classes_to_pursue": ["brand_profile_page", "review_directory_page", "chain_locations_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_roundup", "blocked_page"],
        "schema_confidence": "medium",
    },
    "coworking_spaces": {
        "row_signature": ["one coworking location", "observable desk price or membership price", "observable source URL"],
        "positive_page_signals": ["coworking directory page", "operator location page", "workspace pricing page"],
        "negative_page_signals": ["search engine page", "generic blog roundup", "resources page", "blocked page"],
        "optional_fields": ["rating_value", "review_count", "city", "state", "neighborhood"],
        "quality_rules": {"min_required_field_coverage": 0.85},
        "page_classes_to_pursue": ["coworking_directory_page", "workspace_profile_page", "operator_pricing_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_blog_roundup", "resources_page", "blocked_page"],
        "schema_confidence": "medium",
    },
    "stock_candidates": {
        "row_signature": ["named security", "explicit ticker", "source-specific recommendation mention"],
        "positive_page_signals": ["article with named picks", "ticker list", "stock recommendation roundup"],
        "negative_page_signals": ["ETF tracker", "index landing page", "generic market hub", "screener home", "search engine page"],
        "optional_fields": ["thesis_excerpt", "recommendation_label", "published_at", "price_target", "asset_type"],
        "quality_rules": {"min_required_field_coverage": 0.95, "equities_only": True},
        "page_classes_to_pursue": ["publisher_stock_roundup", "analyst_pick_article", "research_pick_list"],
        "page_classes_to_avoid": ["etf_tracker", "index_landing_page", "market_hub", "screener_home", "search_engine_page"],
        "schema_confidence": "medium",
    },
    "market_contracts": {
        "row_signature": ["one active prediction market", "observable market title", "observable price or liquidity fields"],
        "positive_page_signals": ["active market board", "market contract page", "market analytics page"],
        "negative_page_signals": ["search engine page", "commentary article", "generic betting guide", "blocked page"],
        "optional_fields": ["event_category", "no_price", "liquidity_usd", "resolution_date"],
        "quality_rules": {"min_required_field_coverage": 0.9, "prefer_direct_market_pages": True},
        "page_classes_to_pursue": ["prediction_market_board", "market_contract_page", "market_analytics_page"],
        "page_classes_to_avoid": ["search_engine_page", "commentary_article", "generic_betting_guide", "blocked_page"],
        "schema_confidence": "medium",
    },
    "districts": {
        "row_signature": ["one comparable district", "observable district identity", "observable official or profile metrics"],
        "positive_page_signals": ["district profile page", "official district site", "state report card page"],
        "negative_page_signals": ["search engine page", "generic school article", "blocked page"],
        "optional_fields": ["student_teacher_ratio", "math_proficiency_pct", "reading_proficiency_pct"],
        "quality_rules": {"min_required_field_coverage": 0.85},
        "page_classes_to_pursue": ["district_profile_page", "official_district_page", "state_report_card_page"],
        "page_classes_to_avoid": ["search_engine_page", "generic_school_article", "blocked_page"],
        "schema_confidence": "high",
    },
}


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _contains_keyword(text: str, keyword: str) -> bool:
    escaped = re.escape(keyword.lower())
    return bool(re.search(r"(?<!\w){keyword}(?!\w)".format(keyword=escaped), text.lower()))


def _marketplace_focus(task: str) -> dict[str, Any]:
    task_lower = task.lower()
    focus_rules = [
        {
            "name": "price",
            "keywords": (
                "price",
                "pricing",
                "cost",
                "cheapest",
                "expensive",
                "median",
                "mean",
                "distribution",
                "histogram",
            ),
            "measures": ["price_value"],
            "dimensions": [],
            "required_columns": ["price_value"],
            "questions": [
                "What is the current price distribution?",
                "Which listings dominate the high end?",
                "Which listings are unusually cheap relative to the rest of the market?",
            ],
            "coverage_column": "price_value",
        },
        {
            "name": "condition",
            "keywords": (
                "condition",
                "used",
                "pre-owned",
                "pre owned",
                "new",
                "refurbished",
            ),
            "measures": [],
            "dimensions": ["condition"],
            "required_columns": ["condition"],
            "questions": [
                "How are listings distributed by condition?",
                "Which conditions dominate the sample?",
                "Are the strongest listings concentrated in one condition tier?",
            ],
            "coverage_column": "condition",
        },
        {
            "name": "shipping",
            "keywords": (
                "shipping",
                "delivery",
                "ship cost",
                "postage",
                "free ship",
                "free delivery",
            ),
            "measures": [],
            "dimensions": ["shipping_text"],
            "required_columns": ["shipping_text"],
            "questions": [
                "How much listing variation is coming from shipping terms?",
                "Which listings have free shipping versus added delivery cost?",
                "Are the best-looking listings still competitive after shipping is considered?",
            ],
            "coverage_column": "shipping_text",
        },
        {
            "name": "completeness",
            "keywords": (
                "full set",
                "complete",
                "incomplete",
                "missing",
                "suspicious",
                "fake",
                "counterfeit",
                "set makeup",
            ),
            "measures": [],
            "dimensions": ["title_clean"],
            "required_columns": ["title_clean"],
            "questions": [
                "Which listings look incomplete or suspicious from the title text?",
                "How consistent is set makeup across the sample?",
                "Which outliers need follow-up verification before analysis continues?",
            ],
            "coverage_column": "title_clean",
        },
        {
            "name": "brand",
            "keywords": (
                "brand",
                "taylormade",
                "callaway",
                "ping",
                "mizuno",
                "titleist",
                "cobra",
                "srixon",
                "pxg",
            ),
            "measures": [],
            "dimensions": ["title_clean"],
            "required_columns": ["title_clean"],
            "questions": [
                "Which brands appear most often in the sample?",
                "How are listings distributed across the main brands?",
                "Which brands dominate the strongest listings?",
            ],
            "coverage_column": "title_clean",
        },
    ]

    matched: list[dict[str, Any]] = []
    for rule in focus_rules:
        if any(_contains_keyword(task_lower, keyword) for keyword in rule["keywords"]):
            matched.append(rule)

    if not matched:
        return {
            "focus_names": ["overview"],
            "measures": [],
            "dimensions": [],
            "required_columns": [],
            "questions": [
                "What does the captured listing sample look like?",
                "Which listing clusters or patterns stand out first?",
                "What extra fields or follow-ups are needed before stronger analysis?",
            ],
            "coverage_columns": [],
        }

    return {
        "focus_names": [rule["name"] for rule in matched],
        "measures": _ordered_unique(
            [field for rule in matched for field in rule["measures"]]
        ),
        "dimensions": _ordered_unique(
            [field for rule in matched for field in rule["dimensions"]]
        ),
        "required_columns": _ordered_unique(
            [field for rule in matched for field in rule["required_columns"]]
        ),
        "questions": _ordered_unique(
            [question for rule in matched for question in rule["questions"]]
        ),
        "coverage_columns": _ordered_unique(
            [str(rule["coverage_column"]) for rule in matched if rule.get("coverage_column")]
        ),
    }


def _marketplace_object_bundle(task: str) -> dict[str, Any]:
    min_rows = _extract_min_rows(task, 100)
    focus = _marketplace_focus(task)
    required_columns = _ordered_unique(
        ["item_id", "title_clean", "listing_url", *focus["required_columns"]]
    )
    objective_parts = {
        "price": "pricing",
        "condition": "condition mix",
        "shipping": "shipping terms",
        "completeness": "listing completeness",
        "brand": "brand mix",
        "overview": "listing structure",
    }
    objective_focus = ", ".join(
        objective_parts[name] for name in focus["focus_names"] if name in objective_parts
    )
    if not objective_focus:
        objective_focus = "listing structure"
    target_object = {
        "name": "listings",
        "description": "One row per marketplace listing",
        "grain": "one listing",
        "primary_key": ["item_id", "listing_url"],
        "measures": list(focus["measures"]),
        "dimensions": list(focus["dimensions"]),
        "required_columns": required_columns,
        "sample_target": {"min_rows": min_rows},
    }
    stop_conditions: list[dict[str, Any]] = [
        {"type": "min_rows", "object": "listings", "value": min_rows},
    ]
    for column in focus["coverage_columns"]:
        stop_conditions.append(
            {
                "type": "required_column_coverage",
                "object": "listings",
                "column": column,
                "min_fraction": 0.95,
            }
        )
    return {
        "objective": "Capture and structure marketplace listings for interactive local analysis of {focus}.".format(
            focus=objective_focus
        ),
        "target_object": target_object,
        "questions": list(focus["questions"]),
        "source_preferences": _default_source_preferences_for_target("listings", prompt=task),
        "stop_conditions": stop_conditions,
    }


def _highschool_target_object() -> dict[str, Any]:
    return {
        "name": "districts",
        "description": "One row per comparable high-school district.",
        "grain": "one district",
        "primary_key": ["entity_name"],
        "measures": [
            "student_teacher_ratio",
            "math_proficiency_pct",
            "reading_proficiency_pct",
            "graduation_rate_pct",
            "average_sat",
            "average_act",
        ],
        "dimensions": [
            "grades_served",
            "comparability_flag",
            "source_types",
        ],
        "required_columns": [
            "entity_name",
            "comparability_flag",
            *DISTRICT_RANKING_POLICY["required_fields"],
        ],
        "sample_target": {"min_rows": 3},
    }


def _looks_like_marketplace_listing_task(task: str, seeded_urls: Optional[list[str]] = None) -> bool:
    haystack = " ".join([task, *(seeded_urls or [])]).lower()
    return any(token in haystack for token in ("ebay", "marketplace", "listing", "buy-it-now", "/sch/", "price"))


def build_task_spec(
    task: str,
    manifest: dict[str, Any],
    recipe: str,
    *,
    seeded_urls: Optional[list[str]] = None,
    mission_overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_text = str(task or manifest.get("task", "")).strip()
    if recipe == RECIPE_HIGHSCHOOL:
        objective = (
            "Produce a structured district table suitable for high-school-only comparison, "
            "ranking, and interactive analysis."
        )
        target_objects = [_highschool_target_object()]
        questions = [
            "Which districts are comparable high-school-only districts?",
            "Which districts currently look strongest academically?",
            "What evidence is still missing before a final ranking is defensible?",
        ]
        stop_conditions = [
            {"type": "min_rows", "object": "districts", "value": 3},
            {
                "type": "required_columns_present",
                "object": "districts",
                "columns": list(DISTRICT_RANKING_POLICY["required_fields"]),
            },
            {
                "type": "required_entity_source_types",
                "object": "districts",
                "value": _required_entity_source_types(RECIPE_HIGHSCHOOL),
            },
        ]
    else:
        objective = "Capture and structure evidence for interactive local analysis in pyreplab."
        source_preferences: list[dict[str, str]] = []
        if _looks_like_marketplace_listing_task(task_text, seeded_urls=seeded_urls):
            listing_bundle = _marketplace_object_bundle(task_text)
            objective = str(listing_bundle["objective"])
            target_objects = [dict(listing_bundle["target_object"])]
            questions = list(listing_bundle["questions"])
            source_preferences = _clean_source_preferences(listing_bundle.get("source_preferences", []))
            stop_conditions = list(listing_bundle["stop_conditions"])
        else:
            target_objects = [_generic_target_object()]
            questions = [
                "What sources were captured?",
                "What structured objects are available for analysis?",
                "What is missing before a stronger answer can be given?",
            ]
            source_preferences = _default_source_preferences_for_target("records", prompt=task_text)
            stop_conditions = [
                {
                    "type": "min_captured_pages",
                    "object": "sources",
                    "value": max(1, len(manifest.get("pages", [])) or 1),
                }
            ]

    task_spec = {
        "version": 1,
        "task_id": "task-{name}".format(name=str(manifest.get("name", "capsule"))),
        "created_at": str(manifest.get("created_at") or now_iso()),
        "user_prompt": task_text,
        "objective": objective,
        "task_type": recipe,
        "target_objects": target_objects,
        "questions": questions,
        "constraints": {
            "data_residency": "local_or_user_owned",
            "browser_runtime": "mcp",
            "analysis_runtime": "pyreplab",
            "allow_exploratory_answers": True,
        },
        "source_preferences": source_preferences if recipe != RECIPE_HIGHSCHOOL else [],
        "stop_conditions": stop_conditions,
    }
    task_spec = apply_task_spec_overrides(task_spec, mission_overrides or {})
    task_spec["mission_overrides"] = dict(mission_overrides or {})
    return task_spec


def _clean_string_list(values: Any) -> list[str]:
    cleaned: list[str] = []
    if not isinstance(values, list):
        return cleaned
    for value in values:
        text = str(value).strip()
        if text:
            cleaned.append(text)
    return cleaned


def apply_task_spec_overrides(
    task_spec: dict[str, Any],
    mission_overrides: dict[str, Any],
) -> dict[str, Any]:
    if not mission_overrides:
        return task_spec

    updated = json.loads(json.dumps(task_spec))

    objective = str(mission_overrides.get("objective", "")).strip()
    if objective:
        updated["objective"] = objective

    questions = _clean_string_list(mission_overrides.get("questions"))
    if questions:
        updated["questions"] = questions

    if "source_preferences" in mission_overrides:
        updated["source_preferences"] = _clean_source_preferences(mission_overrides.get("source_preferences"))

    targets = list(updated.get("target_objects") or [])
    if targets:
        target = dict(targets[0])
        old_name = str(target.get("name", "")).strip()

        scalar_fields = (
            "name",
            "description",
            "grain",
        )
        for field in scalar_fields:
            if field in mission_overrides:
                value = str(mission_overrides.get(field, "")).strip()
                if value:
                    target[field] = value

        list_fields = (
            "primary_key",
            "measures",
            "dimensions",
            "required_columns",
        )
        for field in list_fields:
            if field in mission_overrides:
                target[field] = _clean_string_list(mission_overrides.get(field))

        min_rows_raw = str(mission_overrides.get("min_rows", "")).strip()
        min_rows = 0
        if min_rows_raw.isdigit():
            min_rows = max(1, int(min_rows_raw))
            sample_target = dict(target.get("sample_target") or {})
            sample_target["min_rows"] = min_rows
            target["sample_target"] = sample_target

        targets[0] = target
        updated["target_objects"] = targets

        new_name = str(target.get("name", "")).strip() or old_name
        if "source_preferences" not in mission_overrides:
            if new_name != old_name or not _clean_source_preferences(updated.get("source_preferences")):
                updated["source_preferences"] = _default_source_preferences_for_target(
                    new_name,
                    prompt=str(updated.get("user_prompt", "")),
                )
        stop_conditions = list(updated.get("stop_conditions") or [])
        min_rows_found = False
        rewritten_conditions: list[dict[str, Any]] = []
        for condition in stop_conditions:
            if not isinstance(condition, dict):
                rewritten_conditions.append(condition)
                continue
            if str(condition.get("object", "")).strip() == old_name and new_name:
                condition["object"] = new_name
            condition_type = str(condition.get("type", "")).strip()
            if condition_type == "min_rows":
                min_rows_found = True
                if new_name:
                    condition["object"] = new_name
                if min_rows:
                    condition["value"] = min_rows
                rewritten_conditions.append(condition)
                continue
            if condition_type == "required_column_coverage":
                continue
            rewritten_conditions.append(condition)
        if min_rows and not min_rows_found and new_name:
            rewritten_conditions.append(
                {
                    "type": "min_rows",
                    "object": new_name,
                    "value": min_rows,
                }
            )
        coverage_columns = [str(field).strip() for field in target.get("measures", []) if str(field).strip()]
        if not coverage_columns:
            primary_key = {str(field).strip() for field in target.get("primary_key", []) if str(field).strip()}
            coverage_columns = [
                str(field).strip()
                for field in target.get("required_columns", [])
                if str(field).strip()
                and str(field).strip() not in primary_key
                and not str(field).strip().endswith("_url")
            ]
        for column in _ordered_unique(coverage_columns):
            rewritten_conditions.append(
                {
                    "type": "required_column_coverage",
                    "object": new_name,
                    "column": column,
                    "min_fraction": 0.95,
                }
            )
        updated["stop_conditions"] = rewritten_conditions

    return updated


def _primary_target_object(task_spec: dict[str, Any]) -> dict[str, Any]:
    targets = task_spec.get("target_objects") or []
    if isinstance(targets, list):
        for target in targets:
            if isinstance(target, dict):
                return target
    return _generic_target_object()


def _row_schema_defaults_for_object(object_name: str) -> dict[str, Any]:
    defaults = ROW_SCHEMA_DEFAULTS.get(object_name, ROW_SCHEMA_DEFAULTS["records"])
    return json.loads(json.dumps(defaults))


def _row_schema_quality_rules(object_name: str, task_spec: dict[str, Any]) -> dict[str, Any]:
    rules = dict(_row_schema_defaults_for_object(object_name).get("quality_rules", {}))
    prompt_lower = str(task_spec.get("user_prompt", "")).lower()
    if object_name == "stock_candidates":
        rules["equities_only"] = "etf" not in prompt_lower and "fund" not in prompt_lower
    return rules


def build_row_schema(task_spec: dict[str, Any]) -> dict[str, Any]:
    target = _primary_target_object(task_spec)
    object_name = str(target.get("name", "records")).strip() or "records"
    defaults = _row_schema_defaults_for_object(object_name)
    required_fields = _ordered_unique(
        [str(field).strip() for field in target.get("required_columns", []) if str(field).strip()]
    )
    optional_fields = _ordered_unique(
        [
            *[str(field).strip() for field in defaults.get("optional_fields", []) if str(field).strip()],
            *[
                str(field).strip()
                for field in (
                    list(target.get("primary_key", []))
                    + list(target.get("measures", []))
                    + list(target.get("dimensions", []))
                )
                if str(field).strip() and str(field).strip() not in required_fields
            ],
        ]
    )
    sample_target = dict(target.get("sample_target") or {})
    if "min_rows" not in sample_target:
        sample_target["min_rows"] = 1
    return {
        "version": 1,
        "generated_at": now_iso(),
        "task_id": str(task_spec.get("task_id", "")),
        "task_type": str(task_spec.get("task_type", "")),
        "object_name": object_name,
        "object_description": str(target.get("description", "")).strip(),
        "grain": str(target.get("grain", "")).strip() or "one structured record",
        "required_fields": required_fields,
        "optional_fields": optional_fields,
        "row_signature": list(defaults.get("row_signature", [])),
        "positive_page_signals": list(defaults.get("positive_page_signals", [])),
        "negative_page_signals": list(defaults.get("negative_page_signals", [])),
        "dedupe_keys": _ordered_unique(
            [str(field).strip() for field in target.get("primary_key", []) if str(field).strip()]
        ),
        "quality_rules": _row_schema_quality_rules(object_name, task_spec),
        "sample_target": sample_target,
        "page_classes_to_pursue": list(defaults.get("page_classes_to_pursue", [])),
        "page_classes_to_avoid": list(defaults.get("page_classes_to_avoid", [])),
        "schema_confidence": str(defaults.get("schema_confidence", "medium")),
        "schema_basis": "mission",
    }


def _mission_override_payload_from_template(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "objective": str(template.get("objective", "")).strip(),
        "questions": [str(item).strip() for item in template.get("questions", []) if str(item).strip()],
        "name": str(template.get("name", "")).strip(),
        "description": str(template.get("description", "")).strip(),
        "grain": str(template.get("grain", "")).strip(),
        "primary_key": [str(item).strip() for item in template.get("primary_key", []) if str(item).strip()],
        "measures": [str(item).strip() for item in template.get("measures", []) if str(item).strip()],
        "dimensions": [str(item).strip() for item in template.get("dimensions", []) if str(item).strip()],
        "required_columns": [str(item).strip() for item in template.get("required_columns", []) if str(item).strip()],
        "min_rows": str(template.get("min_rows", "")).strip(),
        "source_preferences": _clean_source_preferences(template.get("source_preferences")),
        "seed_urls": [str(item).strip() for item in template.get("seed_urls", []) if str(item).strip()],
    }


def _planner_clarification_payload(prompt: str) -> dict[str, Any]:
    prompt_lower = str(prompt).lower()
    if _looks_like_prediction_market_prompt(prompt_lower):
        template = _prediction_market_mission_template(prompt)
        return {
            "title": "Planner needs one more detail.",
            "copy": "The Mission is still too generic for active-market analysis. Clarify what one row should represent before Scout starts.",
            "question": "For this prediction-market mission, should one row be one active market contract?",
            "examples": [
                "One row per active market contract",
                "Fields: market title, yes price, no price, liquidity, volume, end date",
            ],
            "suggestions": [
                {
                    "suggestion_id": "market-contracts",
                    "label": "Use active market contracts",
                    "description": "Track one active contract per row with price and liquidity fields.",
                    "mission_overrides": _mission_override_payload_from_template(template),
                }
            ],
        }
    if _looks_like_home_sale_prompt(prompt_lower):
        sale_template = _home_sale_mission_template(prompt)
        rental_template = _rental_mission_template(prompt)
        return {
            "title": "Planner needs one more detail.",
            "copy": "This housing mission can branch into market signals or listing rows. Pick the row type before Scout starts.",
            "question": "Do you want one row per metro market signal or one row per rental/home listing?",
            "examples": [
                "One row per metro market signal",
                "Fields: metro, state, home value, month-over-month change, year-over-year change",
            ],
            "suggestions": [
                {
                    "suggestion_id": "metro-signals",
                    "label": "Use metro market signals",
                    "description": "Track one metro-level price or trend signal per row.",
                    "mission_overrides": _mission_override_payload_from_template(sale_template),
                },
                {
                    "suggestion_id": "rental-listings",
                    "label": "Use listing rows instead",
                    "description": "Track one listing per row with rent and beds/baths fields.",
                    "mission_overrides": _mission_override_payload_from_template(rental_template),
                },
            ],
        }
    if _looks_like_stock_prompt(prompt_lower):
        template = _stock_mission_template(prompt)
        return {
            "title": "Planner needs one more detail.",
            "copy": "The Mission needs a concrete stock row object before Scout can separate picks from generic market commentary.",
            "question": "Should one row be one stock candidate mention with a ticker?",
            "examples": [
                "One row per stock candidate mention",
                "Fields: security name, ticker, source URL, source domain",
            ],
            "suggestions": [
                {
                    "suggestion_id": "stock-candidates",
                    "label": "Use stock candidate mentions",
                    "description": "Track one named stock mention per source.",
                    "mission_overrides": _mission_override_payload_from_template(template),
                }
            ],
        }
    if _looks_like_used_vehicle_prompt(prompt_lower):
        template = _vehicle_mission_template(prompt)
        return {
            "title": "Planner needs one more detail.",
            "copy": "This vehicle mission needs listing-style rows before Gather can pursue marketplaces and dealer pages.",
            "question": "Should one row be one vehicle listing?",
            "examples": [
                "One row per vehicle listing",
                "Fields: year, make, model, price, mileage, city, seller type",
            ],
            "suggestions": [
                {
                    "suggestion_id": "vehicle-listings",
                    "label": "Use vehicle listings",
                    "description": "Track one used vehicle listing per row.",
                    "mission_overrides": _mission_override_payload_from_template(template),
                }
            ],
        }
    if _looks_like_coworking_prompt(prompt_lower):
        template = _coworking_mission_template(prompt)
        return {
            "title": "Planner needs one more detail.",
            "copy": "This local-service mission needs a concrete row for coworking locations before Scout can prioritize directories and operator pages.",
            "question": "Should one row be one coworking location?",
            "examples": [
                "One row per coworking location",
                "Fields: space name, monthly desk price, rating, review count, source URL",
            ],
            "suggestions": [
                {
                    "suggestion_id": "coworking-spaces",
                    "label": "Use coworking locations",
                    "description": "Track one coworking space per row with pricing and reviews.",
                    "mission_overrides": _mission_override_payload_from_template(template),
                }
            ],
        }
    template = _generic_prompt_mission_template(prompt)
    return {
        "title": "Planner needs one more detail.",
        "copy": "The Mission is still too generic for Scout and Gather. Clarify the row object before the pipeline keeps moving.",
        "question": "What should one row represent in this mission?",
        "examples": [
            "One row per entity or item",
            "The 3-5 fields that matter most",
        ],
        "suggestions": [
            {
                "suggestion_id": "generic-row-object",
                "label": "Use the suggested row object",
                "description": "Adopt the planner's best first-pass object and observable fields.",
                "mission_overrides": _mission_override_payload_from_template(template),
            }
        ],
    }


def build_object_decision_review(
    task_spec: dict[str, Any],
    row_schema: dict[str, Any],
    schema_refinement: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target = _primary_target_object(task_spec)
    prompt = str(task_spec.get("user_prompt", "")).strip()
    target_name = str(target.get("name", "")).strip() or "records"
    low_information = mission_plan_is_low_information(target)
    required_fields = [str(item).strip() for item in row_schema.get("required_fields", []) if str(item).strip()]
    clarification = _planner_clarification_payload(prompt) if low_information else {}
    status = "accepted"
    reason_code = "concrete-row-object"
    summary = "The Mission has a concrete row object and Scout can continue."
    blocking_stage = ""

    if low_information or not required_fields:
        status = "needs_clarification"
        reason_code = "low_information_row_object"
        summary = "The Mission still needs a clearer row object before Scout and Gather can use it reliably."
        blocking_stage = "scout"

    refinement_replan = str((schema_refinement or {}).get("replan_reason", "")).strip()
    if refinement_replan:
        status = "replan_recommended"
        reason_code = "evidence_contradicts_row_object"
        summary = refinement_replan
        blocking_stage = "gather"
        if not clarification:
            clarification = _planner_clarification_payload(prompt)
        clarification = {
            **clarification,
            "title": str(clarification.get("title", "")).strip() or "Planner wants to rework the Mission.",
            "copy": (
                str(clarification.get("copy", "")).strip()
                or "Scout evidence does not match the current row object. Re-plan the Mission before Scout or Gather continue."
            ),
            "question": (
                str(clarification.get("question", "")).strip()
                or "What should one row represent in this mission?"
            ),
        }

    return {
        "version": 1,
        "generated_at": now_iso(),
        "task_id": str(task_spec.get("task_id", "")).strip(),
        "status": status,
        "reason_code": reason_code,
        "target_name": target_name,
        "summary": summary,
        "blocking_stage": blocking_stage,
        "title": str(clarification.get("title", "")).strip(),
        "copy": str(clarification.get("copy", "")).strip(),
        "question": str(clarification.get("question", "")).strip(),
        "examples": list(clarification.get("examples", [])),
        "suggestions": list(clarification.get("suggestions", [])),
        "required_field_count": len(required_fields),
        "schema_confidence": str(row_schema.get("schema_confidence", "")).strip(),
    }


def _stock_schema_refinement(rows: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str], list[str], str]:
    pursue = {"publisher_stock_roundup", "analyst_pick_article", "research_pick_list"}
    avoid = {"search_engine_page"}
    schema_changes: list[str] = []
    negative_patterns: list[str] = []
    confidence = "medium"
    for row in rows:
        title_lower = str(row.get("title", "")).lower()
        if any(token in title_lower for token in (" etf", "etf ", "index", "tracker", "market outlook")):
            avoid.update({"etf_tracker", "index_landing_page", "market_hub"})
            negative_patterns.append("etf-or-index-page")
        if any(
            token in title_lower
            for token in (
                "stocks to buy",
                "best stocks",
                "top stocks",
                "companies to invest",
                "stock picks",
                "analyst picks",
            )
        ):
            pursue.update({"publisher_stock_roundup", "analyst_pick_article"})
    if negative_patterns:
        schema_changes.append("demote-etf-index-market-hub-pages")
    if rows and len(pursue) >= 3:
        confidence = "high"
    return sorted(pursue), sorted(avoid), _ordered_unique(schema_changes), _ordered_unique(negative_patterns), confidence


def _generic_schema_refinement(object_name: str, rows: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str], list[str], str]:
    defaults = _row_schema_defaults_for_object(object_name)
    pursue = {str(item).strip() for item in defaults.get("page_classes_to_pursue", []) if str(item).strip()}
    avoid = {str(item).strip() for item in defaults.get("page_classes_to_avoid", []) if str(item).strip()}
    schema_changes: list[str] = []
    negative_patterns: list[str] = []
    confidence = str(defaults.get("schema_confidence", "medium"))
    for row in rows:
        title_lower = str(row.get("title", "")).lower()
        if "google" in str(row.get("domain_hint", "")).lower():
            avoid.add("search_engine_page")
        if object_name == "products":
            if any(token in title_lower for token in ("best ", "top ", "reviews", "roundup")):
                avoid.add("editorial_roundup")
                negative_patterns.append("editorial-roundup")
            if any(token in title_lower for token in ("target", "walmart", "amazon", "buy", "shop")):
                pursue.update({"retailer_result_grid", "product_detail_page"})
        if object_name in {"vehicle_listings", "rental_listings", "land_listings", "mattress_listings"}:
            if any(token in title_lower for token in ("guide", "tips", "how to", "market trends")):
                avoid.add("generic_market_article")
                negative_patterns.append("generic-market-article")
        if object_name == "home_sale_signals":
            if any(token in title_lower for token in ("market trends", "home values", "metros", "zillow research")):
                pursue.update({"market_trend_page", "valuation_research_page", "metro_price_article"})
            if any(token in title_lower for token in ("guide", "tips", "how to buy", "how to sell")):
                avoid.add("generic_homebuying_guide")
                negative_patterns.append("generic-homebuying-guide")
        if object_name == "neighborhood_price_rankings":
            if any(token in title_lower for token in ("most expensive neighborhoods", "wealthiest neighborhoods", "richest neighborhoods", "median home price")):
                pursue.update({"neighborhood_ranking_page", "valuation_research_page"})
            if any(token in title_lower for token in ("for rent", "zip code", "suburb", "suburbs", "facebook")):
                avoid.update({"rental_article", "social_media_shell"})
                negative_patterns.append("wrong-geography-or-social-page")
        if object_name in {"restaurants", "restaurant_chains"}:
            if any(token in title_lower for token in ("near me", "locations", "reviews", "menu")):
                pursue.add("review_directory_page")
    if negative_patterns:
        schema_changes.append("reinforce-negative-page-class-filters")
    if rows:
        confidence = "high" if len(rows) >= 8 else "medium"
    return sorted(pursue), sorted(avoid), _ordered_unique(schema_changes), _ordered_unique(negative_patterns), confidence


def _schema_alignment_for_row(
    object_name: str,
    row: dict[str, Any],
    row_schema: dict[str, Any],
) -> tuple[str, list[str]]:
    title = _normalize_space(str(row.get("title", "")))
    candidate_name = _normalize_space(str(row.get("candidate_name", "")))
    snippet = _normalize_space(str(row.get("snippet", "")))
    domain_hint = _normalize_domain(str(row.get("domain_hint", "")).strip())
    combined = _normalize_space(" ".join(bit for bit in (title, candidate_name, snippet, domain_hint) if bit)).lower()
    reasons: list[str] = []
    positive_score = 0
    negative_score = 0

    positive_terms = _schema_signal_terms(
        [
            *[str(item) for item in row_schema.get("positive_page_signals", []) if str(item).strip()],
            *[str(item) for item in row_schema.get("page_classes_to_pursue", []) if str(item).strip()],
        ]
    )
    negative_terms = _schema_signal_terms(
        [
            *[str(item) for item in row_schema.get("negative_page_signals", []) if str(item).strip()],
            *[str(item) for item in row_schema.get("page_classes_to_avoid", []) if str(item).strip()],
        ]
    )
    positive_hits = sum(1 for term in positive_terms if term and term in combined)
    negative_hits = sum(1 for term in negative_terms if term and term in combined)
    if positive_hits:
        positive_score += min(positive_hits, 3)
        reasons.append("schema-positive")
    if negative_hits:
        negative_score += min(negative_hits, 3)
        reasons.append("schema-negative")

    if object_name == "stock_candidates":
        if any(token in combined for token in ("stocks to buy", "stock picks", "analyst picks", "best stocks", "top stocks")):
            positive_score += 2
            reasons.append("stock-picks-page")
        if re.search(r"\([A-Z]{1,5}\)", title):
            positive_score += 1
            reasons.append("ticker-mention")
        if any(token in combined for token in (" etf", "etf ", "index", "tracker", "screener", "market hub")):
            negative_score += 3
            reasons.append("etf-index-page")
    elif object_name == "market_contracts":
        has_direct_signals = any(re.search(pattern, combined) for pattern in MARKET_CONTRACT_DIRECT_SIGNAL_PATTERNS)
        has_guide_signals = any(re.search(pattern, combined) for pattern in MARKET_CONTRACT_GUIDE_PATTERNS)
        has_commentary_signals = any(re.search(pattern, combined) for pattern in MARKET_CONTRACT_COMMENTARY_PATTERNS)
        has_metric_signals = any(token in combined for token in ("yes", "no", "volume", "liquidity", "market data"))
        if has_direct_signals and has_metric_signals:
            positive_score += 3
            reasons.append("market-board-signal")
        elif has_direct_signals and not has_metric_signals:
            negative_score += 2
            reasons.append("missing-contract-rows")
        if has_guide_signals:
            negative_score += 3
            reasons.append("betting-guide")
        if has_commentary_signals and not has_direct_signals:
            negative_score += 3
            reasons.append("commentary-page")
        if not has_metric_signals:
            negative_score += 2
            reasons.append("missing-contract-rows")
        if any(token in combined for token in ("polymarket", "kalshi", "manifold")) and has_direct_signals:
            positive_score += 1
            reasons.append("market-platform")
    elif object_name == "home_sale_signals":
        if any(
            token in combined
            for token in (
                "home prices",
                "home values",
                "price drop",
                "price cuts",
                "days on market",
                "housing market",
                "median sale",
                "zillow research",
                "redfin research",
                "market trends",
                "neighborhood",
                "metro",
            )
        ):
            positive_score += 3
            reasons.append("home-market-signal")
        if any(
            token in combined
            for token in (
                "for rent",
                "apartments",
                "beds",
                "baths",
                "studio",
                "rental",
                "sq ft",
            )
        ):
            negative_score += 3
            reasons.append("rental-or-listing-page")
    elif object_name == "neighborhood_price_rankings":
        if any(
            token in combined
            for token in (
                "most expensive neighborhoods",
                "wealthiest neighborhoods",
                "richest neighborhoods",
                "median home price",
                "median sale price",
                "home values",
                "recent median home price",
                "luxury neighborhood",
            )
        ):
            positive_score += 2
            reasons.append("neighborhood-price-signal")
        if any(token in combined for token in ("for rent", "apartment rentals", "zip code", "zip codes", "suburb", "suburbs")):
            negative_score += 7
            reasons.append("wrong-geography-or-rental-page")
        if any(token in domain_hint for token in ("facebook.com", "instagram.com", "x.com", "twitter.com")):
            negative_score += 4
            reasons.append("social-shell")
    elif object_name == "rental_listings":
        if any(
            token in combined
            for token in ("for rent", "apartments", "beds", "baths", "studio", "available now", "pet friendly")
        ):
            positive_score += 3
            reasons.append("rental-listing-signal")
        if any(
            token in combined
            for token in ("home prices", "home values", "market trends", "price cuts", "neighborhood guide")
        ):
            negative_score += 3
            reasons.append("market-or-guide-page")
    elif object_name == "coworking_spaces":
        if any(
            token in combined
            for token in ("coworking", "dedicated desk", "hot desk", "private office", "per month", "/month")
        ):
            positive_score += 3
            reasons.append("workspace-pricing-signal")
        if any(token in combined for token in ("blog", "latest posts", "resources", "best coworking spaces", "roundup")):
            negative_score += 2
            reasons.append("coworking-roundup")

    if any(token in combined for token in ("page not found", "404", "access denied", "login", "sign in")):
        negative_score += 3
        reasons.append("blocked-or-missing-page")

    if positive_score <= 0 and negative_score <= 0:
        return "neutral", []
    if negative_score >= positive_score + 1:
        return "negative", _ordered_unique(reasons)
    if positive_score >= negative_score + 1:
        return "positive", _ordered_unique(reasons)
    return "neutral", _ordered_unique(reasons)


def _schema_replan_reason(
    object_name: str,
    *,
    reviewed_candidate_count: int,
    positive_candidate_count: int,
    negative_candidate_count: int,
    negative_reasons: list[str],
) -> str:
    if reviewed_candidate_count <= 0:
        return ""
    strong_contradiction = (
        (positive_candidate_count == 0 and negative_candidate_count >= 2)
        or (negative_candidate_count >= 3 and negative_candidate_count >= (positive_candidate_count * 2 + 1))
    )
    if not strong_contradiction:
        return ""

    if object_name == "stock_candidates":
        return "Scout evidence looks more like ETF, index, or market-hub pages than pages that name comparable stock candidates. Re-plan the row object or tighten the stock source routes before Gather continues."
    if object_name == "market_contracts":
        return "Scout evidence looks more like commentary or betting-guide pages than active market boards or contract analytics. Re-plan the Mission or tighten the source routes before Scout or Gather continue."
    if object_name == "home_sale_signals":
        return "Scout evidence looks more like rental, listing, or generic guide pages than metro- or neighborhood-level home price signal pages. Re-plan the Mission before Scout or Gather continue."
    if object_name == "neighborhood_price_rankings":
        return "Scout evidence looks more like rentals, zip-code lists, suburbs, or social shells than neighborhood-level home price pages. Re-plan the Mission before Scout or Gather continue."
    if object_name == "rental_listings":
        return "Scout evidence looks more like market-trend or guide pages than rental listing/result pages. Re-plan the Mission before Scout or Gather continue."
    if object_name == "coworking_spaces":
        return "Scout evidence looks more like roundup or resource pages than coworking listings with pricing and review signals. Re-plan the Mission before Scout or Gather continue."
    reason_suffix = ": {value}".format(value=", ".join(negative_reasons[:2])) if negative_reasons else ""
    return "Scout evidence does not match the current row object closely enough to continue Gather{suffix}.".format(
        suffix=reason_suffix
    )


def _schema_alignment_for_page(
    object_name: str,
    *,
    page_title: str,
    page_text: str,
    actual_domain: str,
    row_schema: dict[str, Any],
) -> tuple[str, list[str]]:
    combined = _normalize_space(" ".join(bit for bit in (page_title, page_text, actual_domain) if bit)).lower()
    reasons: list[str] = []
    positive_score = 0
    negative_score = 0

    positive_terms = _schema_signal_terms(
        [
            *[str(item) for item in row_schema.get("positive_page_signals", []) if str(item).strip()],
            *[str(item) for item in row_schema.get("page_classes_to_pursue", []) if str(item).strip()],
        ]
    )
    negative_terms = _schema_signal_terms(
        [
            *[str(item) for item in row_schema.get("negative_page_signals", []) if str(item).strip()],
            *[str(item) for item in row_schema.get("page_classes_to_avoid", []) if str(item).strip()],
        ]
    )
    positive_hits = sum(1 for term in positive_terms if term and term in combined)
    negative_hits = sum(1 for term in negative_terms if term and term in combined)
    if positive_hits:
        positive_score += min(positive_hits, 4)
        reasons.append("schema-positive")
    if negative_hits:
        negative_score += min(negative_hits, 4)
        reasons.append("schema-negative")

    if object_name == "market_contracts":
        has_direct_signals = any(re.search(pattern, combined) for pattern in MARKET_CONTRACT_DIRECT_SIGNAL_PATTERNS)
        has_guide_signals = any(re.search(pattern, combined) for pattern in MARKET_CONTRACT_GUIDE_PATTERNS)
        has_commentary_signals = any(re.search(pattern, combined) for pattern in MARKET_CONTRACT_COMMENTARY_PATTERNS)
        contract_candidate_count = len(_iter_market_contract_candidates(page_text[:6000]))
        metric_signal_count = 0
        if re.search(r"\b(?:yes|no)\b.{0,24}(?:¢|%|\$0?\.\d+)", page_text, re.I | re.S):
            metric_signal_count += 1
        if re.search(r"\bvol(?:ume)?\.?\b", page_text, re.I):
            metric_signal_count += 1
        if re.search(r"\bliq(?:uidity)?\.?\b", page_text, re.I):
            metric_signal_count += 1
        if has_direct_signals and metric_signal_count >= 1:
            positive_score += 3
            reasons.append("market-board-signal")
        elif has_direct_signals and metric_signal_count == 0:
            negative_score += 2
            reasons.append("missing-contract-rows")
        if contract_candidate_count >= 1 and metric_signal_count >= 1:
            positive_score += 4
            reasons.append("contract-row-signal")
        if has_guide_signals:
            negative_score += 3
            reasons.append("betting-guide")
            if contract_candidate_count == 0:
                negative_score += 4
                reasons.append("missing-contract-rows")
        if has_commentary_signals and not has_direct_signals:
            negative_score += 3
            reasons.append("commentary-page")
            if contract_candidate_count == 0 or metric_signal_count == 0:
                negative_score += 3
                reasons.append("missing-contract-rows")
    elif object_name == "home_sale_signals":
        if any(
            token in combined
            for token in (
                "home price",
                "home prices",
                "home value",
                "home values",
                "price cut",
                "price cuts",
                "days on market",
                "housing market",
                "median sale",
                "market trends",
                "neighborhood",
                "metro",
            )
        ):
            positive_score += 3
            reasons.append("home-market-signal")
        if any(
            token in combined
            for token in ("for rent", "apartments", "rental", "beds", "baths", "studio", "sq ft")
        ):
            negative_score += 3
            reasons.append("rental-or-listing-page")
    elif object_name == "neighborhood_price_rankings":
        if any(
            token in combined
            for token in (
                "most expensive neighborhoods",
                "wealthiest neighborhoods",
                "richest neighborhoods",
                "median home price",
                "median sale price",
                "home values",
                "recent median home price",
                "luxury neighborhood",
            )
        ):
            positive_score += 2
            reasons.append("neighborhood-price-signal")
        if any(token in combined for token in ("for rent", "apartment rentals", "zip code", "zip codes", "suburb", "suburbs")):
            negative_score += 7
            reasons.append("wrong-geography-or-rental-page")
        if any(token in actual_domain for token in ("facebook.com", "instagram.com", "x.com", "twitter.com")):
            negative_score += 4
            reasons.append("social-shell")
    elif object_name == "rental_listings":
        if any(
            token in combined
            for token in ("for rent", "apartments", "beds", "baths", "studio", "available now", "pet friendly")
        ):
            positive_score += 3
            reasons.append("rental-listing-signal")
        if any(
            token in combined
            for token in ("home prices", "home values", "market trends", "price cuts", "neighborhood guide")
        ):
            negative_score += 3
            reasons.append("market-or-guide-page")
    elif object_name == "stock_candidates":
        if any(token in combined for token in ("stocks to buy", "stock picks", "analyst picks", "best stocks", "top stocks")):
            positive_score += 3
            reasons.append("stock-picks-page")
        if re.search(r"\([A-Z]{1,5}\)", page_title):
            positive_score += 1
            reasons.append("ticker-mention")
        if any(token in combined for token in (" etf", "etf ", "index", "tracker", "screener", "market hub")):
            negative_score += 3
            reasons.append("etf-index-page")
    elif object_name == "coworking_spaces":
        if any(
            token in combined
            for token in ("coworking", "dedicated desk", "hot desk", "private office", "per month", "/month")
        ):
            positive_score += 3
            reasons.append("workspace-pricing-signal")
        if any(token in combined for token in ("blog", "latest posts", "resources", "best coworking spaces", "roundup")):
            negative_score += 2
            reasons.append("coworking-roundup")

    if positive_score <= 0 and negative_score <= 0:
        return "neutral", []
    if negative_score >= positive_score + 1:
        return "negative", _ordered_unique(reasons)
    if positive_score >= negative_score + 1:
        return "positive", _ordered_unique(reasons)
    return "neutral", _ordered_unique(reasons)


def build_schema_refinement(
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
    scout_summary: dict[str, Any],
) -> dict[str, Any]:
    row_schema = build_row_schema(task_spec)
    object_name = str(row_schema.get("object_name", "records")).strip() or "records"
    rows = [row for row in scout_index.get("rows", []) if isinstance(row, dict)]
    if object_name == "stock_candidates":
        pursue, avoid, schema_changes, negative_patterns, confidence = _stock_schema_refinement(rows)
    else:
        pursue, avoid, schema_changes, negative_patterns, confidence = _generic_schema_refinement(object_name, rows)
    positive_candidate_count = 0
    negative_candidate_count = 0
    neutral_candidate_count = 0
    observed_alignment_reasons: list[str] = []
    for row in rows:
        alignment, reasons = _schema_alignment_for_row(object_name, row, row_schema)
        if alignment == "positive":
            positive_candidate_count += 1
        elif alignment == "negative":
            negative_candidate_count += 1
            observed_alignment_reasons.extend(reasons)
        else:
            neutral_candidate_count += 1
    observed_alignment_reasons = _ordered_unique(observed_alignment_reasons)
    known_market_platform_candidate_count = 0
    if object_name == "market_contracts":
        for row in rows:
            combined_text = _normalize_space(
                " ".join(
                    bit
                    for bit in (
                        str(row.get("title", "")),
                        str(row.get("candidate_name", "")),
                        str(row.get("snippet", "")),
                    )
                    if bit
                )
            )
            platform_names = _extract_market_platform_names(combined_text)
            if any(_market_platform_domain_hint(name) for name in platform_names):
                known_market_platform_candidate_count += 1
    replan_reason = _schema_replan_reason(
        object_name,
        reviewed_candidate_count=len(rows),
        positive_candidate_count=positive_candidate_count,
        negative_candidate_count=negative_candidate_count,
        negative_reasons=observed_alignment_reasons,
    )
    if object_name == "market_contracts" and replan_reason and known_market_platform_candidate_count > 0:
        replan_reason = ""
    return {
        "version": 1,
        "generated_at": now_iso(),
        "task_id": str(task_spec.get("task_id", "")),
        "object_name": object_name,
        "reviewed_candidate_count": len(rows),
        "positive_candidate_count": positive_candidate_count,
        "negative_candidate_count": negative_candidate_count,
        "neutral_candidate_count": neutral_candidate_count,
        "known_market_platform_candidate_count": known_market_platform_candidate_count,
        "page_classes_to_pursue": pursue,
        "page_classes_to_avoid": avoid,
        "observed_domain_hints": sorted(
            str(key).strip()
            for key in dict(scout_summary.get("domain_counts", {})).keys()
            if str(key).strip()
        )[:12],
        "observed_negative_patterns": _ordered_unique([*negative_patterns, *observed_alignment_reasons]),
        "schema_changes": schema_changes,
        "refined_quality_rules": dict(row_schema.get("quality_rules", {})),
        "schema_confidence": confidence if rows else str(row_schema.get("schema_confidence", "medium")),
        "schema_basis": "scout",
        "replan_reason": replan_reason,
    }


def _default_capsule_progress(manifest: dict[str, Any]) -> tuple[str, str]:
    if manifest.get("pages"):
        return "analysis", "exploratory_ready"
    return "planning", "planned"


def build_capsule_state(
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    *,
    previous: Optional[dict[str, Any]] = None,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    latest_capture_batch_id: Optional[str] = None,
    latest_turn_id: Optional[str] = None,
    pending_followup_count: Optional[int] = None,
) -> dict[str, Any]:
    previous_state = dict(previous or {})
    default_stage, default_status = _default_capsule_progress(manifest)
    return {
        "version": 1,
        "task_id": str(task_spec.get("task_id", "")),
        "updated_at": now_iso(),
        "stage": stage or default_stage,
        "status": status or default_status,
        "task_spec_path": "task_spec.json",
        "row_schema_path": "row_schema.json",
        "source_plan_path": "source_plan.json",
        "schema_refinement_path": "schema_refinement.json",
        "gather_qa_path": "gather_qa.json",
        "gather_qa_review_path": "gather_qa_review.json",
        "object_manifest_path": "object_manifest.json",
        "readiness_path": "readiness.json",
        "latest_capture_batch_id": latest_capture_batch_id
        or str(previous_state.get("latest_capture_batch_id", "")),
        "latest_turn_id": latest_turn_id or str(previous_state.get("latest_turn_id", "")),
        "object_versions": dict(previous_state.get("object_versions") or {}),
        "pending_followup_count": int(
            pending_followup_count
            if pending_followup_count is not None
            else previous_state.get("pending_followup_count", 0)
        ),
    }


def _task_object_names(task_spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in task_spec.get("target_objects", []):
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item.get("name")))
    return names


def _first_target_object(task_spec: dict[str, Any]) -> dict[str, Any]:
    for item in task_spec.get("target_objects", []):
        if isinstance(item, dict):
            return dict(item)
    return {}


def _source_type_from_url(url: str, recipe: str) -> str:
    domain = _domain_from_url(url)
    path = urlparse(url).path.lower()
    if recipe == RECIPE_HIGHSCHOOL:
        if "illinoisreportcard.com" in domain:
            return "state_report_card"
        if "niche.com" in domain and "/k12/d/" in path:
            return "district_profile"
        if "niche.com" in domain and "rankings" in path:
            return "ranking_page"
        if domain and "school" in domain:
            return "official_site"
        return "seed_url"
    if "ebay.com" in domain and "/sch/" in path:
        return "marketplace_search_results"
    if "ebay.com" in domain and "/itm/" in path:
        return "marketplace_listing"
    return "seed_url"


def _source_extraction_hints(recipe: str, source_type: str, task_spec: dict[str, Any]) -> dict[str, Any]:
    target = _first_target_object(task_spec)
    if recipe == RECIPE_HIGHSCHOOL:
        return {
            "grain": "district_source_page",
            "expected_fields": [
                "entity_name",
                "grades_served",
                *DISTRICT_FIELDS,
            ],
        }
    if source_type == "marketplace_search_results":
        return {
            "grain": "listing_card",
            "expected_fields": [
                "item_id",
                "title_clean",
                "price_text",
                "price_value",
                "shipping_text",
                "condition",
                "listing_url",
            ],
        }
    target_fields = _ordered_unique(
        [
            *[str(field) for field in target.get("required_columns", []) if str(field).strip()],
            *[str(field) for field in target.get("measures", []) if str(field).strip()],
            *[str(field) for field in target.get("dimensions", []) if str(field).strip()],
        ]
    )
    target_names = _task_object_names(task_spec)
    return {
        "grain": str(target.get("grain", "page")).strip() or "page",
        "expected_fields": target_fields or target_names,
    }


def _source_dedupe_keys(recipe: str, task_spec: dict[str, Any]) -> dict[str, Any]:
    if recipe == RECIPE_HIGHSCHOOL:
        return {"districts": ["entity_name"]}
    object_names = _task_object_names(task_spec)
    if object_names == ["records"]:
        return {"records": []}
    return {name: [] for name in object_names}


def _query_phrase(text: str, fallback: str) -> str:
    query = _normalize_space(text)
    if query:
        return query
    return _normalize_space(fallback) or fallback


def _planner_focus_phrase(prompt: str) -> str:
    focus = _normalize_space(prompt)
    replacements = (
        r"^i want to\s+",
        r"^i need to\s+",
        r"^please\s+",
        r"^can you\s+",
        r"^(?:help me\s+)?(?:compare|find out what is the|find out what is|find out|find the|find|analyze|rank|evaluate|review|build)\s+",
        r"^the best\s+",
    )
    for pattern in replacements:
        focus = re.sub(pattern, "", focus, flags=re.I)
    return _normalize_space(focus)


def _compact_search_prompt(prompt: str) -> str:
    clean = _normalize_space(prompt)
    if not clean:
        return ""
    clean = re.sub(
        r"\bwith one row per\b.*$",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(
        r"\bincluding\b.*$",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(
        r"\b(?:autoplay|mcp|smoke|isolated|rerun|demo)\b",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"\b(?:jan|feb|mar|march|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b\s+\d{1,2}\s+\d{4}\b", "", clean, flags=re.I)
    clean = re.sub(r"\b\d{4}\b", "", clean)
    clean = _normalize_space(clean)
    tokens = clean.split()
    if len(tokens) > 16:
        clean = " ".join(tokens[:16])
    return _normalize_space(clean)


def _source_preferences(task_spec: dict[str, Any]) -> list[dict[str, str]]:
    preferences = _clean_source_preferences(task_spec.get("source_preferences"))
    if preferences:
        return preferences
    target_name = str(_first_target_object(task_spec).get("name", "")).strip()
    return _default_source_preferences_for_target(
        target_name,
        prompt=str(task_spec.get("user_prompt", "")),
    )


SOURCE_QUERY_SUFFIXES = {
    "district_profile_search": "district profile",
    "state_report_card_search": "state report card accountability",
    "official_site_search": "official district site",
    "review_directory_search": "reviews ratings",
    "map_directory_search": "locations reviews",
    "editorial_search": "best list shortlist",
    "franchise_directory_search": "locations united states franchise footprint",
    "auto_marketplace_search": "for sale used inventory",
    "dealer_inventory_search": "dealer inventory used",
    "classified_listing_search": "classified listings",
    "rental_marketplace_search": "apartments for rent listings",
    "apartment_directory_search": "apartment directory current rent",
    "property_management_search": "leasing availability property management",
    "local_marketplace_search": "for sale local pickup",
    "community_marketplace_search": "community resale local",
    "retailer_search": "shopping results product listings",
    "review_comparison_search": "reviews comparison",
    "brand_catalog_search": "brand catalog official products",
    "marketplace_search_results": "buy it now listings",
    "official_source_search": "official source",
    "web_search_query": "",
}


def _compose_source_query(prompt: str, preference: dict[str, str]) -> str:
    query_hint = _normalize_space(str(preference.get("query_hint", "")))
    source_type = str(preference.get("source_type", "")).strip()
    suffix = _normalize_space(query_hint or SOURCE_QUERY_SUFFIXES.get(source_type, ""))
    base = _normalize_space(prompt)
    if suffix:
        combined = _normalize_space("{base} {suffix}".format(base=base, suffix=suffix))
    else:
        combined = base
    site_hint = _domain_like_site_hint(preference.get("site_hint", ""))
    if site_hint:
        return 'site:{site} {query}'.format(site=site_hint, query=combined)
    return combined


def _source_query_variants(task_spec: dict[str, Any], preference: dict[str, str]) -> list[str]:
    prompt = _query_phrase(
        str(task_spec.get("user_prompt", "")),
        str(task_spec.get("objective", "")),
    )
    compact_prompt = _compact_search_prompt(prompt) or _normalize_space(prompt)
    focus_prompt = _compact_search_prompt(_planner_focus_phrase(prompt))
    candidates = [
        _compose_source_query(compact_prompt, preference),
    ]
    if focus_prompt and focus_prompt.lower() != compact_prompt.lower():
        candidates.append(_compose_source_query(focus_prompt, preference))
    queries: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        clean = _normalize_space(candidate)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(clean)
    return queries


def _discovery_source_blueprints(
    task_spec: dict[str, Any],
    recipe: str,
) -> list[dict[str, str]]:
    preferences = _source_preferences(task_spec)
    if preferences:
        return [
            blueprint
            for preference in preferences
            if str(preference.get("source_type", "")).strip()
            for blueprint in [
                {
                    "source_type": str(preference.get("source_type", "")),
                    "query": query,
                    "site_hint": str(preference.get("site_hint", "")).strip(),
                    "route_role": str(preference.get("route_role", "")).strip() or "scout",
                    "rationale": str(preference.get("rationale", "")).strip(),
                }
                for query in _source_query_variants(task_spec, preference)
            ]
        ]

    prompt = _query_phrase(
        str(task_spec.get("user_prompt", "")),
        str(task_spec.get("objective", "")),
    )
    return [
        {
            "source_type": "web_search_query",
            "query": prompt,
            "site_hint": "",
            "route_role": "scout",
            "rationale": "Start with a broad query to discover likely sources for the mission.",
        },
        {
            "source_type": "official_source_search",
            "query": _compose_source_query(
                prompt,
                {
                    "source_type": "official_source_search",
                    "query_hint": "official source",
                    "route_role": "scout",
                },
            ),
            "site_hint": "",
            "route_role": "scout",
            "rationale": "Look for primary sources before trusting downstream summaries.",
        },
    ]


SOURCE_YIELD_PROFILES: dict[str, dict[str, tuple[int, int, int, int, str]]] = {
    "districts": {
        "district_profile_search": (1, 3, 3, 8, "medium"),
        "state_report_card_search": (1, 3, 2, 6, "medium"),
        "official_site_search": (1, 3, 2, 5, "medium"),
        "district_profile": (1, 1, 1, 1, "high"),
        "state_report_card": (1, 1, 1, 1, "high"),
        "official_site": (1, 1, 1, 1, "high"),
    },
    "products": {
        "retailer_search": (8, 16, 10, 24, "medium"),
        "seed_url": (6, 14, 8, 20, "medium"),
    },
    "stock_candidates": {
        "financial_publisher_search": (4, 10, 6, 14, "medium"),
        "equity_research_search": (4, 10, 6, 14, "medium"),
        "market_commentary_search": (3, 8, 5, 12, "low"),
        "seed_url": (3, 8, 4, 10, "medium"),
    },
    "market_contracts": {
        "prediction_market_search": (6, 14, 8, 18, "medium"),
        "market_analytics_search": (5, 12, 7, 16, "medium"),
        "official_source_search": (4, 10, 6, 14, "medium"),
        "seed_url": (6, 14, 8, 18, "medium"),
    },
    "listings": {
        "marketplace_search_results": (35, 90, 35, 90, "medium"),
        "marketplace_listing": (1, 1, 1, 1, "high"),
        "seed_url": (20, 60, 20, 60, "medium"),
    },
    "vehicle_listings": {
        "auto_marketplace_search": (12, 24, 15, 30, "medium"),
        "seed_url": (8, 16, 10, 20, "medium"),
    },
    "mattress_listings": {
        "local_marketplace_search": (8, 18, 10, 24, "medium"),
        "community_marketplace_search": (6, 14, 8, 18, "medium"),
        "seed_url": (6, 14, 8, 18, "medium"),
    },
    "rental_listings": {
        "rental_marketplace_search": (8, 18, 10, 24, "medium"),
        "apartment_directory_search": (8, 16, 10, 22, "medium"),
        "property_management_search": (4, 10, 6, 14, "low"),
        "seed_url": (6, 14, 8, 18, "medium"),
    },
    "land_listings": {
        "land_marketplace_search": (8, 18, 10, 24, "medium"),
        "property_listing_search": (8, 16, 10, 22, "medium"),
        "county_property_search": (3, 8, 4, 10, "low"),
        "seed_url": (6, 14, 8, 18, "medium"),
    },
    "neighborhood_price_rankings": {
        "official_source_search": (2, 6, 4, 10, "medium"),
        "web_search_query": (2, 6, 4, 10, "medium"),
        "seed_url": (3, 8, 4, 10, "medium"),
    },
    "restaurants": {
        "review_directory_search": (6, 12, 8, 18, "medium"),
        "map_directory_search": (5, 10, 8, 18, "medium"),
        "editorial_search": (8, 16, 10, 20, "low"),
        "seed_url": (4, 10, 6, 16, "medium"),
    },
    "restaurant_chains": {
        "review_directory_search": (4, 8, 8, 16, "medium"),
        "map_directory_search": (4, 8, 7, 14, "medium"),
        "directory_search": (4, 8, 7, 14, "medium"),
        "franchise_directory_search": (2, 5, 4, 10, "low"),
        "seed_url": (2, 6, 4, 10, "low"),
    },
}


def _target_min_rows(task_spec: dict[str, Any]) -> int:
    target = _first_target_object(task_spec)
    sample_target = dict(target.get("sample_target") or {})
    raw_min_rows = sample_target.get("min_rows")
    try:
        min_rows = int(raw_min_rows)
    except (TypeError, ValueError):
        min_rows = 0
    if min_rows > 0:
        return min_rows
    for item in task_spec.get("stop_conditions", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).strip() != "min_rows":
            continue
        try:
            return max(0, int(item.get("value", 0)))
        except (TypeError, ValueError):
            continue
    return 0


def _domain_like_site_hint(text: str) -> str:
    clean = str(text or "").strip().lower()
    if not clean:
        return ""
    if "/" in clean:
        clean = clean.split("/", 1)[0]
    if "." not in clean or " " in clean:
        return ""
    return clean


def _should_carry_forward_source_url(url: str, task_spec: Optional[dict[str, Any]] = None) -> bool:
    clean = str(url or "").strip()
    if not clean:
        return False
    if _is_search_engine_url(clean):
        return False
    target_name = str(_first_target_object(task_spec or {}).get("name", "")).strip()
    if target_name == "products":
        lower_url = clean.lower()
        if "/zgbs/" in lower_url or "/gp/bestsellers/" in lower_url:
            return False
        query = _extract_search_query_from_url(clean)
        query_lower = query.lower()
        if _looks_like_generic_price_bucket(query):
            return False
        task_prompt = str((task_spec or {}).get("user_prompt", "")).lower()
        if "baby" in task_prompt and "toy" in task_prompt and query_lower:
            if (
                "baby" not in query_lower
                and "toy" not in query_lower
                and "infant" not in query_lower
                and "montessori" not in query_lower
            ):
                return False
    return True


def _source_family_key_from_source(source: dict[str, Any]) -> str:
    entrypoint = dict(source.get("entrypoint") or {})
    source_type = str(source.get("source_type", "")).strip() or "seed_url"
    site_hint = _domain_like_site_hint(entrypoint.get("site_hint", ""))
    if site_hint:
        return _normalize_domain(site_hint)
    mode = str(entrypoint.get("mode", "")).strip().lower()
    value = str(entrypoint.get("value", "")).strip()
    if mode == "url":
        domain = _normalize_domain(_domain_from_url(value))
        if domain:
            return domain
    if mode == "query":
        hinted = _query_site_hint(value)
        if hinted:
            return _normalize_domain(hinted)
    return source_type


def _source_route_role(source: dict[str, Any]) -> str:
    entrypoint = dict(source.get("entrypoint") or {})
    mode = str(entrypoint.get("mode", "")).strip().lower()
    value = str(entrypoint.get("value", "")).strip()
    if mode == "query":
        return "scout"
    if mode == "url" and _is_search_engine_url(value):
        return "scout"
    return "gather"


def _recommended_scout_action_budget(task_spec: dict[str, Any]) -> int:
    target_name = str(_first_target_object(task_spec).get("name", "")).strip()
    target_min_rows = _target_min_rows(task_spec)
    if target_name in {"products", "listings", "vehicle_listings", "mattress_listings"}:
        return min(160, max(80, int(target_min_rows * 1.2) if target_min_rows else 80))
    if target_name in {"restaurants", "restaurant_chains"}:
        return min(140, max(60, int(target_min_rows * 3) if target_min_rows else 60))
    if target_name == "districts":
        return min(80, max(24, int(target_min_rows * 8) if target_min_rows else 24))
    return min(120, max(40, int(target_min_rows * 2) if target_min_rows else 40))


def _source_family_key_from_row(
    row: dict[str, Any],
    pages_by_id: dict[str, dict[str, Any]],
) -> str:
    for key in ("source_url", "listing_url", "product_url", "primary_source_url", "final_url"):
        value = str(row.get(key, "")).strip()
        domain = _normalize_domain(_domain_from_url(value))
        if domain:
            return domain
    platform = str(row.get("source_platform", "")).strip().lower()
    platform_map = {
        "yelp": "yelp.com",
        "tripadvisor": "tripadvisor.com",
        "google": "google.com",
        "google maps": "google.com",
    }
    if platform in platform_map:
        return platform_map[platform]
    page_ids: list[str] = []
    if isinstance(row.get("source_page_ids"), list):
        page_ids.extend(str(item).strip() for item in row.get("source_page_ids", []) if str(item).strip())
    page_id = str(row.get("source_page_id", "") or row.get("page_id", "")).strip()
    if page_id:
        page_ids.append(page_id)
    for candidate_page_id in page_ids:
        page = pages_by_id.get(candidate_page_id, {})
        domain = _normalize_domain(
            _domain_from_url(
                str(page.get("final_url", "") or page.get("requested_url", "")).strip()
            )
        )
        if domain:
            return domain
    return ""


def _row_page_ids(row: dict[str, Any]) -> list[str]:
    page_ids: list[str] = []
    if isinstance(row.get("source_page_ids"), list):
        for item in row.get("source_page_ids", []):
            value = str(item).strip()
            if value and value not in page_ids:
                page_ids.append(value)
    for key in ("source_page_id", "page_id"):
        value = str(row.get(key, "")).strip()
        if value and value not in page_ids:
            page_ids.append(value)
    return page_ids


def _read_target_rows_from_capsule(capsule_dir: Path, object_manifest: dict[str, Any], target_name: str) -> list[dict[str, Any]]:
    for item in object_manifest.get("objects", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() != target_name:
            continue
        table_path = str(item.get("table_path", "")).strip()
        if table_path:
            return _read_jsonl_rows(capsule_dir / table_path)
    fallback = capsule_dir / "tables" / "{name}.jsonl".format(name=target_name)
    return _read_jsonl_rows(fallback)


def _collect_local_source_family_calibration(task_spec: dict[str, Any]) -> dict[str, Any]:
    target_name = str(_first_target_object(task_spec).get("name", "")).strip()
    if not target_name or not CAPSULES_ROOT.exists():
        return {"families": {}, "matched_capsules": []}

    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    matched_capsules: list[str] = []
    for task_spec_path in sorted(CAPSULES_ROOT.glob("*/task_spec.json")):
        capsule_dir = task_spec_path.parent
        source_plan_path = capsule_dir / "source_plan.json"
        object_manifest_path = capsule_dir / "object_manifest.json"
        manifest_path = capsule_dir / "manifest.json"
        if not source_plan_path.exists() or not object_manifest_path.exists():
            continue
        capsule_task_spec = _read_json(task_spec_path, {})
        capsule_target_name = str(_first_target_object(capsule_task_spec).get("name", "")).strip()
        if capsule_target_name != target_name:
            continue
        object_manifest = _read_json(object_manifest_path, {})
        target_object = _find_manifest_object(object_manifest, target_name)
        if not isinstance(target_object, dict) or int(target_object.get("row_count", 0) or 0) <= 0:
            continue
        source_plan = _read_json(source_plan_path, {})
        manifest = _read_json(manifest_path, {})
        source_family_by_source_id: dict[str, str] = {}
        source_type_by_source_id: dict[str, str] = {}
        for source in source_plan.get("sources", []):
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id", "")).strip()
            if not source_id:
                continue
            source_family = _source_family_key_from_source(source)
            source_type = str(source.get("source_type", "")).strip()
            if source_family:
                source_family_by_source_id[source_id] = source_family
            if source_type:
                source_type_by_source_id[source_id] = source_type
        pages_by_id = {
            str(page.get("page_id", "")).strip(): page
            for page in manifest.get("pages", [])
            if isinstance(page, dict) and str(page.get("page_id", "")).strip()
        }
        source_family_by_page_id: dict[str, str] = {}
        source_type_by_page_id: dict[str, str] = {}
        for page_id, page in pages_by_id.items():
            source_id = str(page.get("source_id", "")).strip()
            source_family = source_family_by_source_id.get(source_id, "")
            source_type = source_type_by_source_id.get(source_id, "")
            if source_family:
                source_family_by_page_id[page_id] = source_family
            if source_type:
                source_type_by_page_id[page_id] = source_type
        for source in source_plan.get("sources", []):
            if not isinstance(source, dict):
                continue
            page_id = str(source.get("page_id", "")).strip()
            if not page_id:
                continue
            source_family = _source_family_key_from_source(source)
            source_type = str(source.get("source_type", "")).strip()
            if source_family:
                source_family_by_page_id.setdefault(page_id, source_family)
            if source_type:
                source_type_by_page_id.setdefault(page_id, source_type)
        rows = _read_target_rows_from_capsule(capsule_dir, object_manifest, target_name)
        row_counts = Counter()
        for row in rows:
            if not isinstance(row, dict):
                continue
            attributed = False
            for page_id in _row_page_ids(row):
                source_family = source_family_by_page_id.get(page_id, "")
                source_type = source_type_by_page_id.get(page_id, "")
                if source_family:
                    row_counts[source_family] += 1
                    attributed = True
                if source_type and source_type != source_family:
                    row_counts[source_type] += 1
                    attributed = True
            if attributed:
                continue
            family = _source_family_key_from_row(row, pages_by_id)
            if family:
                row_counts[family] += 1
        source_counts = Counter()
        for source in source_plan.get("sources", []):
            if not isinstance(source, dict):
                continue
            if target_name not in [str(item) for item in source.get("target_objects", [])]:
                continue
            capture_status = str(source.get("capture_status", "")).strip()
            if capture_status != "captured" and not source.get("page_id") and not source.get("captured_page_count"):
                continue
            family = _source_family_key_from_source(source)
            if family:
                source_counts[family] += 1
        if not source_counts:
            continue
        matched_capsules.append(capsule_dir.name)
        for family, source_count in source_counts.items():
            rows_for_family = int(row_counts.get(family, 0))
            source_type = ""
            for source in source_plan.get("sources", []):
                if not isinstance(source, dict):
                    continue
                if _source_family_key_from_source(source) != family:
                    continue
                source_type = str(source.get("source_type", "")).strip()
                if source_type:
                    break
            sample = {
                "capsule_name": capsule_dir.name,
                "rows": rows_for_family,
                "source_count": source_count,
                "rows_per_source": round(rows_for_family / max(source_count, 1), 3),
            }
            families[family].append(
                sample
            )
            if source_type and source_type != family:
                families[source_type].append(dict(sample))
    return {
        "families": dict(families),
        "matched_capsules": sorted(set(matched_capsules)),
    }


def _calibrated_row_bounds(
    *,
    base_low: int,
    base_high: int,
    samples: list[dict[str, Any]],
) -> tuple[int, int, str, dict[str, Any]]:
    if not samples:
        return base_low, base_high, "heuristic", {}
    values = sorted(float(item.get("rows_per_source", 0.0) or 0.0) for item in samples)
    sample_count = len(values)
    median_value = values[sample_count // 2] if sample_count % 2 == 1 else (values[sample_count // 2 - 1] + values[sample_count // 2]) / 2.0
    if median_value <= 0.25:
        low = 0
        high = max(1, int(round(base_low * 0.25)))
    else:
        low = max(1, int(median_value * 0.8))
        high = max(low, int(median_value * 1.25 + 0.999))
    # Sparse local history should inform heuristics, not overwhelm them.
    if sample_count < 3:
        low = max(low, max(1, int(round(base_low * 0.6))))
        high = max(high, max(low, int(round(base_high * 0.75))))
    confidence = "high" if sample_count >= 3 else "medium"
    meta = {
        "observed_rows_per_source": round(median_value, 2),
        "observed_capsule_count": len({str(item.get("capsule_name", "")) for item in samples if str(item.get("capsule_name", ""))}),
    }
    return low, high, "local_empirical", meta


def _source_yield_estimate(
    task_spec: dict[str, Any],
    source: dict[str, Any],
    *,
    calibration: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target_name = str(_first_target_object(task_spec).get("name", "")).strip() or "records"
    source_type = str(source.get("source_type", "")).strip() or "seed_url"
    profile = SOURCE_YIELD_PROFILES.get(target_name, {}).get(source_type)
    if profile is None:
        entrypoint = dict(source.get("entrypoint") or {})
        mode = str(entrypoint.get("mode", "")).strip()
        if mode == "url":
            profile = (1, 4, 2, 6, "low")
        else:
            profile = (2, 6, 4, 10, "low")
    object_rows_low, object_rows_high, scout_low, scout_high, confidence = profile
    source_family = _source_family_key_from_source(source)
    family_samples = list((calibration or {}).get("families", {}).get(source_family, []))
    object_rows_low, object_rows_high, calibration_source, calibration_meta = _calibrated_row_bounds(
        base_low=object_rows_low,
        base_high=object_rows_high,
        samples=family_samples,
    )
    if calibration_source == "local_empirical":
        confidence = "high" if calibration_meta.get("observed_capsule_count", 0) >= 2 else "medium"
    return {
        "row_object": target_name,
        "source_family": source_family,
        "expected_rows_low": object_rows_low,
        "expected_rows_high": object_rows_high,
        "expected_scout_candidates_low": scout_low,
        "expected_scout_candidates_high": scout_high,
        "confidence": confidence,
        "calibration_source": calibration_source,
        **calibration_meta,
    }


def _source_plan_budget(task_spec: dict[str, Any], source_entries: list[dict[str, Any]]) -> dict[str, Any]:
    target_min_rows = _target_min_rows(task_spec)
    calibration = _collect_local_source_family_calibration(task_spec)
    scout_source_count = sum(1 for source in source_entries if _source_route_role(source) == "scout")
    gather_sources = [source for source in source_entries if _source_route_role(source) == "gather"]
    budget_sources = gather_sources or source_entries
    planned_sources = len(budget_sources)
    rows_low = 0
    rows_high = 0
    scout_low = 0
    scout_high = 0
    empirically_calibrated_sources = 0
    calibration_families: set[str] = set()
    for source in source_entries:
        source["route_role"] = _source_route_role(source)
        estimate = _source_yield_estimate(task_spec, source, calibration=calibration)
        source["yield_estimate"] = estimate
        if source["route_role"] == "gather" or not gather_sources:
            rows_low += int(estimate.get("expected_rows_low", 0) or 0)
            rows_high += int(estimate.get("expected_rows_high", 0) or 0)
        scout_low += int(estimate.get("expected_scout_candidates_low", 0) or 0)
        scout_high += int(estimate.get("expected_scout_candidates_high", 0) or 0)
        if estimate.get("calibration_source") == "local_empirical":
            empirically_calibrated_sources += 1
            family = str(estimate.get("source_family", "")).strip()
            if family:
                calibration_families.add(family)
    midpoint_per_source = (
        ((rows_low + rows_high) / 2.0) / planned_sources
        if planned_sources
        else 0.0
    )
    recommended_source_count = (
        max(1, int((target_min_rows / max(midpoint_per_source, 1.0)) + 0.999))
        if target_min_rows
        else planned_sources
    )
    source_gap = max(recommended_source_count - planned_sources, 0)
    if not target_min_rows:
        planning_status = "no_row_target"
    elif rows_low >= target_min_rows:
        planning_status = "covered"
    elif rows_high >= target_min_rows:
        planning_status = "tight"
    else:
        planning_status = "underplanned"
    scout_action_budget = _recommended_scout_action_budget(task_spec) if scout_source_count else 0
    scout_candidate_goal = (
        max(
            scout_source_count * 8,
            recommended_source_count * 3 if recommended_source_count else 0,
        )
        if scout_source_count
        else 0
    )
    return {
        "target_min_rows": target_min_rows,
        "planned_source_count": planned_sources,
        "scout_source_count": scout_source_count,
        "gather_source_count": len(gather_sources),
        "scout_action_budget": scout_action_budget,
        "scout_candidate_goal": scout_candidate_goal,
        "estimated_rows_low": rows_low,
        "estimated_rows_high": rows_high,
        "estimated_scout_candidates_low": scout_low,
        "estimated_scout_candidates_high": scout_high,
        "recommended_source_count": recommended_source_count,
        "source_gap": source_gap,
        "planning_status": planning_status,
        "budget_basis": "local_empirical" if empirically_calibrated_sources else "heuristic",
        "empirically_calibrated_source_count": empirically_calibrated_sources,
        "calibration_family_count": len(calibration_families),
        "calibration_families": sorted(calibration_families),
        "matched_capsule_count": len(list(calibration.get("matched_capsules", []))),
    }


def build_source_plan(
    task_spec: dict[str, Any],
    manifest: dict[str, Any],
    recipe: str,
    *,
    seeded_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    source_entries: list[dict[str, Any]] = []
    ordered_urls: list[str] = []
    seen_urls: set[str] = set()
    captured_pages_by_source_id: dict[str, list[dict[str, Any]]] = {}

    for page in manifest.get("pages", []):
        source_id = str(page.get("source_id", "")).strip()
        if source_id:
            captured_pages_by_source_id.setdefault(source_id, []).append(page)

    for url in seeded_urls or []:
        clean = str(url).strip()
        if _should_carry_forward_source_url(clean, task_spec=task_spec) and clean not in seen_urls:
            seen_urls.add(clean)
            ordered_urls.append(clean)

    for page in manifest.get("pages", []):
        url = str(page.get("requested_url") or page.get("final_url") or "").strip()
        if _should_carry_forward_source_url(url, task_spec=task_spec) and url not in seen_urls:
            seen_urls.add(url)
            ordered_urls.append(url)

    target_names = _task_object_names(task_spec)
    for index, url in enumerate(ordered_urls, start=1):
        source_id = "src-{index:03d}".format(index=index)
        source_type = _source_type_from_url(url, recipe)
        capture_status = "pending"
        matched_page = None
        for page in manifest.get("pages", []):
            if source_id and str(page.get("source_id", "")).strip() == source_id:
                matched_page = page
                break
            if url in {str(page.get("requested_url", "")), str(page.get("final_url", ""))}:
                matched_page = page
                break
        if matched_page is not None:
            capture_status = "captured"
        source_entries.append(
            {
                "source_id": source_id,
                "source_type": source_type,
                "target_objects": target_names,
                "entrypoint": {"mode": "url", "value": url},
                "capture_hints": {
                    "pagination": source_type == "marketplace_search_results",
                    "settle_seconds": 2.0,
                },
                "extraction_hints": _source_extraction_hints(recipe, source_type, task_spec),
                "priority": index,
                "capture_status": capture_status,
                "route_role": "gather",
            }
        )
        if matched_page is not None:
            source_entries[-1]["page_id"] = matched_page.get("page_id", "")

    append_discovery_sources = (not source_entries) or (
        "market_contracts" in target_names and not any(_source_route_role(source) == "scout" for source in source_entries)
    )
    if append_discovery_sources:
        seen_queries: set[tuple[str, str]] = set()
        existing_url_values = {
            str(source.get("entrypoint", {}).get("value", "")).strip().lower()
            for source in source_entries
            if isinstance(source, dict)
        }
        for blueprint in _discovery_source_blueprints(task_spec, recipe):
            source_type = str(blueprint.get("source_type", "web_search_query")).strip() or "web_search_query"
            query = str(blueprint.get("query", "")).strip()
            site_hint = str(blueprint.get("site_hint", "")).strip()
            if not query:
                continue
            dedupe_key = (source_type, query)
            if dedupe_key in seen_queries or query.lower() in existing_url_values:
                continue
            seen_queries.add(dedupe_key)
            source_id = "src-{index:03d}".format(index=len(source_entries) + 1)
            matched_pages = captured_pages_by_source_id.get(source_id, [])
            capture_status = "captured" if matched_pages else "planned"
            source_entry = {
                "source_id": source_id,
                "source_type": source_type,
                "target_objects": target_names,
                "entrypoint": {
                    "mode": "query",
                    "value": query,
                    "site_hint": site_hint,
                },
                "capture_hints": {
                    "pagination": False,
                    "settle_seconds": 2.0,
                    "search_engine": True,
                    "site_hint": site_hint,
                },
                "extraction_hints": _source_extraction_hints(recipe, source_type, task_spec),
                "priority": len(source_entries) + 1,
                "capture_status": capture_status,
                "rationale": str(blueprint.get("rationale", "")).strip(),
                "route_role": "scout",
            }
            if matched_pages:
                source_entry["page_id"] = matched_pages[0].get("page_id", "")
                source_entry["captured_page_count"] = len(matched_pages)
            source_entries.append(source_entry)

    budget = _source_plan_budget(task_spec, source_entries)

    return {
        "version": 1,
        "task_id": str(task_spec.get("task_id", "")),
        "plan_id": "plan-001",
        "created_at": now_iso(),
        "sources": source_entries,
        "source_budget": budget,
        "dedupe_keys": _source_dedupe_keys(recipe, task_spec),
        "exit_conditions": list(task_spec.get("stop_conditions", [])),
    }


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str, limit: int = 280) -> str:
    clean = _normalize_space(text)
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _read_page_text(capsule_dir: Path, page: dict[str, Any]) -> str:
    rel = page.get("artifacts", {}).get("page_text")
    if not isinstance(rel, str) or not rel:
        return ""
    try:
        return (capsule_dir / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def _nonempty_lines(raw_text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("=== Page Text ===") or line.startswith("Length:"):
            continue
        lines.append(line)
    return lines


def _google_result_title(lines: list[str], url_index: int) -> str:
    for offset in (2, 1):
        candidate_index = url_index - offset
        if candidate_index < 0:
            continue
        candidate = lines[candidate_index].strip()
        if not candidate:
            continue
        if candidate.lower() in {"search results", "ai overview", "accessibility links"}:
            continue
        if candidate.startswith("https://") or candidate.startswith("http://"):
            continue
        return candidate
    return ""


def _google_result_domain_hint(raw_url_text: str, fallback_label: str) -> str:
    match = re.search(r"https?://([^/\s›]+)", raw_url_text)
    if match:
        return match.group(1).lower()
    label = fallback_label.strip().lower()
    if not label:
        return ""
    if "." in label:
        return label
    label_map = {
        "yelp": "yelp.com",
        "tripadvisor": "tripadvisor.com",
        "facebook": "facebook.com",
        "youtube": "youtube.com",
        "food republic": "foodrepublic.com",
        "all kfc locations": "locations.kfc.com",
    }
    return label_map.get(label, "")


def _google_result_direct_url(raw_url_text: str) -> str:
    clean = raw_url_text.strip()
    if not clean.startswith(("https://", "http://")):
        return ""
    if "›" not in clean and " " not in clean:
        return clean
    host_match = re.match(r"(https?://[^/\s›]+)", clean)
    if not host_match:
        return ""
    host = host_match.group(1)
    path_part = clean[len(host) :].strip()
    if not path_part:
        return host
    if "›" not in path_part:
        return ""
    crumbs = []
    for chunk in path_part.split("›"):
        segment = chunk.strip().strip("/")
        if not segment:
            continue
        if "..." in segment or "…" in segment or " " in segment:
            return ""
        crumbs.append(segment)
    if not crumbs:
        return ""
    return "{host}/{path}".format(host=host.rstrip("/"), path="/".join(crumbs))


def _looks_like_google_results_page(page: dict[str, Any]) -> bool:
    requested_url = str(page.get("requested_url", "")).lower()
    final_url = str(page.get("final_url", "")).lower()
    return "google.com/search" in requested_url or "google.com/search" in final_url


def _candidate_name_from_title(title: str) -> str:
    clean = _normalize_space(title)
    if not clean:
        return ""
    pieces = re.split(r"\s+[|\-:]\s+", clean)
    candidate = pieces[0].strip() if pieces else clean
    generic_prefixes = (
        "best ",
        "most ",
        "top ",
        "locations ",
        "all ",
        "major ",
        "fried chicken chains",
        "12 fried chicken chains ranked",
        "21 best chicken chains",
    )
    lower = candidate.lower()
    if any(lower.startswith(prefix) for prefix in generic_prefixes):
        return ""
    return candidate


def _scout_followup_entrypoint(
    *,
    raw_url_text: str,
    title: str,
    domain_hint: str,
    candidate_name: str = "",
) -> dict[str, str]:
    direct_url = _google_result_direct_url(raw_url_text)
    if direct_url:
        return {"mode": "url", "value": direct_url}
    site_query = _normalize_space(candidate_name or title)
    if domain_hint and site_query:
        return {"mode": "query", "value": site_query, "site_hint": domain_hint}
    if domain_hint and title:
        return {"mode": "query", "value": title, "site_hint": domain_hint}
    if title:
        return {"mode": "query", "value": title}
    return {"mode": "query", "value": raw_url_text.strip()}


def _scout_score(
    title: str,
    domain_hint: str,
    followup_entrypoint: dict[str, str],
    *,
    route_source_type: str = "",
) -> int:
    score = 10
    title_lower = title.lower()
    domain_lower = domain_hint.lower()
    source_type_lower = route_source_type.lower()
    if followup_entrypoint.get("mode") == "url":
        score += 40
    if any(token in title_lower for token in ("locations", "location", "reviews", "franchise", "chain")):
        score += 15
    if domain_lower and not any(token in domain_lower for token in ("google.com", "bing.com", "yahoo.com", "duckduckgo.com")):
        score += 10
    if any(token in source_type_lower for token in ("review", "directory", "map", "franchise", "marketplace", "dealer")):
        score += 10
    if any(token in title_lower for token in ("kfc", "popeyes", "jollibee", "raising cane", "chick-fil-a", "church", "gus")):
        score += 15
    return score


def build_scout_index(
    capsule_dir: Path,
    manifest: dict[str, Any],
    source_plan: dict[str, Any],
) -> dict[str, Any]:
    source_lookup = {
        str(source.get("source_id", "")): source
        for source in source_plan.get("sources", [])
        if isinstance(source, dict) and str(source.get("source_id", "")).strip()
    }
    rows: list[dict[str, Any]] = []
    for page in manifest.get("pages", []):
        if not _looks_like_google_results_page(page):
            continue
        raw_text = _read_page_text(capsule_dir, page)
        lines = _nonempty_lines(raw_text)
        source_id = str(page.get("source_id", "")).strip()
        source = source_lookup.get(source_id, {})
        source_type = str(source.get("source_type", page.get("source_type", ""))).strip()
        for index, line in enumerate(lines):
            if not line.startswith("https://") and not line.startswith("http://"):
                continue
            title = _google_result_title(lines, index)
            if not title:
                continue
            candidate_name = _candidate_name_from_title(title)
            label = lines[index - 1] if index >= 1 else ""
            domain_hint = _google_result_domain_hint(line, label)
            snippet = ""
            for next_line in lines[index + 1 : index + 4]:
                if next_line.startswith("https://") or next_line.startswith("http://"):
                    break
                if next_line in {"Search Results", "AI Overview", "Footer Links", "Videos"}:
                    break
                snippet = _compact("{snippet} {line}".format(snippet=snippet, line=next_line).strip(), 260)
                if len(snippet) >= 220:
                    break
            followup_entrypoint = _scout_followup_entrypoint(
                raw_url_text=line,
                title=title,
                domain_hint=domain_hint,
                candidate_name=candidate_name,
            )
            rows.append(
                {
                    "candidate_id": "cand-{index:03d}".format(index=len(rows) + 1),
                    "page_id": str(page.get("page_id", "")),
                    "source_id": source_id,
                    "route_source_type": source_type,
                    "title": title,
                    "candidate_name": candidate_name,
                    "domain_hint": domain_hint,
                    "raw_url_text": line,
                    "snippet": snippet,
                    "followup_entrypoint": followup_entrypoint,
                    "scout_score": _scout_score(
                        title,
                        domain_hint,
                        followup_entrypoint,
                        route_source_type=source_type,
                    ),
                }
            )
    return {
        "generated_at": now_iso(),
        "rows": rows,
    }


def build_scout_summary(scout_index: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in scout_index.get("rows", []) if isinstance(row, dict)]
    domains = Counter(str(row.get("domain_hint", "")).strip() for row in rows if str(row.get("domain_hint", "")).strip())
    mode_counts = Counter(
        str((row.get("followup_entrypoint") or {}).get("mode", "")).strip()
        for row in rows
        if isinstance(row.get("followup_entrypoint"), dict)
    )
    return {
        "generated_at": now_iso(),
        "candidate_count": len(rows),
        "domain_counts": dict(sorted(domains.items())),
        "entrypoint_mode_counts": dict(sorted(mode_counts.items())),
        "top_candidates": sorted(
            [
                {
                    "title": str(row.get("title", "")),
                    "domain_hint": str(row.get("domain_hint", "")),
                    "scout_score": int(row.get("scout_score", 0)),
                }
                for row in rows
            ],
            key=lambda item: int(item.get("scout_score", 0)),
            reverse=True,
        )[:10],
    }


ROW_LEVEL_TARGET_OBJECTS = {
    "products",
    "stock_candidates",
    "listings",
    "vehicle_listings",
    "mattress_listings",
    "rental_listings",
    "land_listings",
    "home_sale_signals",
    "market_contracts",
}

CATEGORY_LIKE_SEGMENT_WORDS = (
    "toy",
    "toys",
    "baby",
    "babies",
    "shop",
    "category",
    "categories",
    "deals",
    "sale",
    "sales",
    "clearance",
    "reviews",
    "ratings",
    "listings",
    "inventory",
    "product",
    "products",
    "bestseller",
    "bestsellers",
    "mattress",
    "mattresses",
    "cars",
    "trucks",
    "vans",
    "months",
    "month",
    "stem",
    "gifts",
    "learning",
    "education",
)

CATEGORY_LIKE_TITLE_PATTERNS = (
    r"^best\b",
    r"^top\b",
    r"^most\b",
    r"^\$?\d+\s*(?:to|-)\s*\$?\d+\b",
    r"\bbest sellers?\b",
    r"\bshop\b",
    r"\bcategory\b",
    r"\bthe .* shop\b",
    r"\bproducts?\b",
    r"\breviews?\b",
    r"\bratings?\b",
)

TARGET_QUERY_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "months",
    "month",
    "years",
    "year",
    "best",
    "top",
    "most",
    "shop",
    "baby",
    "toys",
    "toy",
    "products",
    "product",
    "age",
    "new",
    "deal",
    "deals",
    "exclusive",
    "exclusives",
    "up",
    "light",
}

WEAK_TARGET_QUERY_PATTERNS = (
    r"\bdeals?\b",
    r"\bexclusives?\b",
    r"\bnew products?\b",
    r"\bshop by\b",
    r"\bbaby shop\b",
)

SITE_PREFIX_PATTERN = re.compile(r"^[a-z0-9.-]+\.(?:com|org|net|edu|gov):\s*", re.I)
EDITORIAL_PRODUCT_TITLE_PATTERNS = (
    r"^the\s+\d+\s+best\b",
    r"^\d+\s+best\b",
    r"\bhighly-rated\b",
    r"\bwe'?ve tested\b",
    r"\btried and tested\b",
    r"\bour editors?\b",
    r"\bexpert(?:s)?\b",
    r"\btested by\b",
    r"\bof 20\d{2}\b",
)


def _target_object_name(task_spec: dict[str, Any]) -> str:
    return str(_first_target_object(task_spec).get("name", "")).strip()


def _is_row_level_target(task_spec: dict[str, Any]) -> bool:
    return _target_object_name(task_spec) in ROW_LEVEL_TARGET_OBJECTS


def _looks_like_broad_category_url(url: str) -> bool:
    clean = str(url).strip()
    if not clean:
        return False
    lower_url = clean.lower()
    if "/zgbs/" in lower_url or "/gp/bestsellers/" in lower_url:
        return True
    parsed = urlparse(clean)
    if parsed.query:
        return False
    if re.search(r"\b\d+\s*(?:-|to)\s*\d+\b", parsed.path.replace("_", "-").lower()):
        return True
    segments = [segment.strip().lower() for segment in parsed.path.split("/") if segment.strip()]
    if not segments:
        return True
    storefront_markers = {"store", "stores", "brand", "brands", "page", "shop", "home", "homepage"}
    if len(segments) <= 4 and any(segment in storefront_markers for segment in segments):
        return True
    if len(segments) > 2:
        return False
    generic_count = 0
    for segment in segments:
        normalized = segment.replace("-", " ").replace("_", " ")
        if not re.fullmatch(r"[a-z0-9 ]{2,48}", normalized):
            return False
        if any(word in normalized for word in CATEGORY_LIKE_SEGMENT_WORDS):
            generic_count += 1
            continue
        if normalized.endswith("s") and " " not in normalized:
            generic_count += 1
            continue
        return False
    return generic_count == len(segments)


def _looks_like_broad_category_title(title: str) -> bool:
    clean = _normalize_space(title).lower()
    if not clean:
        return False
    if any(re.search(pattern, clean) for pattern in CATEGORY_LIKE_TITLE_PATTERNS):
        return True
    if len(clean.split()) <= 5 and any(word in clean for word in CATEGORY_LIKE_SEGMENT_WORDS):
        return True
    return False


def _normalize_gather_entrypoint(
    *,
    row: dict[str, Any],
    task_spec: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    entrypoint = dict(row.get("followup_entrypoint") or {})
    mode = str(entrypoint.get("mode", "")).strip()
    value = str(entrypoint.get("value", "")).strip()
    site_hint = str(entrypoint.get("site_hint", "")).strip() or str(row.get("domain_hint", "")).strip()
    title = str(row.get("title", "")).strip()
    candidate_name = str(row.get("candidate_name", "")).strip()
    quality = {
        "decision": "keep",
        "reason": "",
    }
    if mode == "query" and value:
        normalized_value = _normalize_target_query_value(value)
        if normalized_value:
            entrypoint["value"] = normalized_value
        if site_hint and not str(entrypoint.get("site_hint", "")).strip():
            entrypoint["site_hint"] = site_hint
    if not _is_row_level_target(task_spec):
        return entrypoint, quality
    if mode != "url" or not value:
        return entrypoint, quality
    if _looks_like_broad_category_url(value) or _looks_like_broad_category_title(title):
        query_value = _normalize_target_query_value(candidate_name or title)
        if query_value:
            return (
                {"mode": "query", "value": query_value, "site_hint": site_hint},
                {
                    "decision": "reroute_to_query",
                    "reason": "broad_category_target",
                },
            )
    return entrypoint, quality


def _meaningful_target_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", _normalize_space(text).lower())
    return [
        token
        for token in tokens
        if len(token) > 2 and token not in TARGET_QUERY_STOPWORDS
    ]


def _target_entity_key(text: str) -> str:
    clean = _normalize_space(text)
    if not clean:
        return ""
    if ":" in clean:
        head = _normalize_space(clean.split(":", 1)[0]).lower()
        if head and not any(re.search(pattern, head) for pattern in WEAK_TARGET_QUERY_PATTERNS):
            return head
    tokens = _meaningful_target_tokens(clean)
    if len(tokens) == 1:
        return tokens[0]
    if len(tokens) >= 2 and all(token not in CATEGORY_LIKE_SEGMENT_WORDS for token in tokens[:2]):
        return " ".join(tokens[:2])
    return ""


def _is_weak_target_phrase(text: str) -> bool:
    clean = _normalize_space(text).lower()
    if not clean:
        return True
    if any(re.search(pattern, clean) for pattern in WEAK_TARGET_QUERY_PATTERNS):
        return True
    return len(_meaningful_target_tokens(clean)) == 0


def _normalize_target_query_value(text: str) -> str:
    clean = _normalize_space(text).replace("…", "...").strip()
    if not clean:
        return ""
    clean = SITE_PREFIX_PATTERN.sub("", clean)
    clean = re.sub(r"\.\.\.+$", "", clean).strip(" -,:;")
    if ":" in clean:
        left, right = [part.strip(" -,:;") for part in clean.split(":", 1)]
        if left and right:
            if _is_weak_target_phrase(right):
                clean = left
            elif _is_weak_target_phrase(left):
                clean = right
            else:
                clean = "{left} {right}".format(left=left, right=right)
    clean = re.sub(r"\s+", " ", clean).strip(" -,:;")
    return clean


def _looks_like_editorial_product_title(text: str) -> bool:
    clean = _normalize_space(text).lower()
    if not clean:
        return False
    return any(re.search(pattern, clean) for pattern in EDITORIAL_PRODUCT_TITLE_PATTERNS)


SCHEMA_SIGNAL_STOPWORDS = {
    "page",
    "pages",
    "source",
    "sources",
    "generic",
    "blocked",
    "search",
    "result",
    "results",
    "detail",
    "official",
    "listing",
    "listings",
    "article",
    "articles",
}


def _schema_signal_terms(values: list[str]) -> set[str]:
    terms: set[str] = set()
    for value in values:
        clean = _normalize_space(str(value).replace("_", " ")).lower()
        if not clean:
            continue
        if len(clean) >= 5:
            terms.add(clean)
        for token in re.findall(r"[a-z0-9]{4,}", clean):
            if token in STOPWORDS or token in SCHEMA_SIGNAL_STOPWORDS:
                continue
            terms.add(token)
    return terms


def _schema_priority_context(
    row_schema: Optional[dict[str, Any]],
    schema_refinement: Optional[dict[str, Any]],
) -> dict[str, set[str]]:
    row_schema = dict(row_schema or {})
    schema_refinement = dict(schema_refinement or {})
    positive_values = [
        *[str(item) for item in row_schema.get("positive_page_signals", []) if str(item).strip()],
        *[str(item) for item in row_schema.get("page_classes_to_pursue", []) if str(item).strip()],
        *[str(item) for item in schema_refinement.get("page_classes_to_pursue", []) if str(item).strip()],
    ]
    negative_values = [
        *[str(item) for item in row_schema.get("negative_page_signals", []) if str(item).strip()],
        *[str(item) for item in row_schema.get("page_classes_to_avoid", []) if str(item).strip()],
        *[str(item) for item in schema_refinement.get("page_classes_to_avoid", []) if str(item).strip()],
        *[str(item) for item in schema_refinement.get("observed_negative_patterns", []) if str(item).strip()],
    ]
    return {
        "positive_terms": _schema_signal_terms(positive_values),
        "negative_terms": _schema_signal_terms(negative_values),
    }


MARKET_CONTRACT_GUIDE_PATTERNS = (
    r"\bexplainer\b",
    r"\bexplained\b",
    r"\bhow (?:prediction markets?|event contracts?) work\b",
    r"\bguide\b",
    r"\bkey questions\b",
    r"\bfaq\b",
    r"\bcalculator\b",
    r"\brisks?\b",
    r"\bwhat (?:is|are)\b",
    r"\bfuture of\b",
    r"\bintroduction\b",
    r"\btop \d+\b",
    r"\branked list\b",
)

MARKET_CONTRACT_COMMENTARY_PATTERNS = (
    r"\bnews\b",
    r"\barticle\b",
    r"\bvaluation\b",
    r"\braises?\b",
    r"\brolls? out\b",
    r"\blaunche?s?\b",
    r"\bgo to war\b",
    r"\bwar over\b",
    r"\bperspective\b",
    r"\bframework\b",
)

MARKET_CONTRACT_DIRECT_SIGNAL_PATTERNS = (
    r"\bactive markets?\b",
    r"\bcurrent (?:markets?|events?)\b",
    r"\blive markets?\b",
    r"\btrade prediction markets\b",
    r"\bodds\b",
    r"\byes\b",
    r"\bno\b",
    r"\bliquidity\b",
    r"\bvolume\b",
    r"\bprice(?:s|d)?\b",
)

MARKET_CONTRACT_ARTIFACT_PATTERNS = (
    r"\b(?:dataset|vocab|clusters|notebook|repo|repository|model card|weights?)\b",
    r"\bresolve/main\b",
    r"\bmlm_vocab\b",
    r"\bgooglelist\.counts\b",
    r"\bcharacter-bert\b",
    r"\bhugging ?face\b",
    r"\bgithub\b",
)

MARKET_CONTRACT_NOISE_DOMAINS = {
    "github.com",
    "huggingface.co",
    "raw.githubusercontent.com",
    "gist.github.com",
    "mit.edu",
}

MARKET_PLATFORM_NAME_STOPWORDS = {
    "Accounting",
    "As",
    "Active",
    "Current",
    "Feature",
    "Features",
    "Google",
    "Home",
    "How",
    "Here",
    "Inflation",
    "Latest",
    "Prediction",
    "Predictions",
    "Markets",
    "Market",
    "Sites",
    "Site",
    "News",
    "Resources",
    "Available",
    "Regulation",
    "Availability",
    "Fees",
    "Funding",
    "Contract",
    "Contracts",
    "Platform",
    "Platforms",
    "Current",
    "Active",
    "Global",
    "US",
    "DeFi",
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "SHIBA",
    "Volume",
    "What",
    "Why",
    "ZEC",
}


def _target_priority(
    *,
    row: dict[str, Any],
    entrypoint: dict[str, str],
    target_quality: dict[str, Any],
    task_spec: Optional[dict[str, Any]] = None,
    row_schema: Optional[dict[str, Any]] = None,
    schema_refinement: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    title = str(row.get("title", "")).strip()
    candidate_name = str(row.get("candidate_name", "")).strip()
    route_source_type = str(row.get("route_source_type", "")).strip().lower()
    target_name = _target_object_name(task_spec or {})
    query_text = _normalize_target_query_value(str(entrypoint.get("value", "")).strip())
    base_text = query_text or candidate_name or title
    meaningful_tokens = _meaningful_target_tokens(base_text)
    entity_key = _target_entity_key(candidate_name or title or query_text)
    score = int(row.get("scout_score", 0) or 0)
    lower_query = query_text.lower()
    if str(entrypoint.get("mode", "")) == "url":
        score += 15
    score += min(len(meaningful_tokens) * 4, 16)
    if re.search(r"\b\d+\s*(?:-|to)\s*\d+\b", base_text.lower()):
        score += 5
    if entity_key:
        score += 6
    if any(re.search(pattern, base_text.lower()) for pattern in WEAK_TARGET_QUERY_PATTERNS):
        score -= 35
    if _looks_like_broad_category_title(base_text):
        score -= 20
        if target_name == "products" and route_source_type in {"retailer_search", "brand_catalog_search"}:
            score += 10
    if str(target_quality.get("reason", "")) == "broad_category_target" and not entity_key:
        score -= 15
    if re.fullmatch(r"\d+(?:\s+\d+)*", entity_key):
        score -= 35
    if re.match(r"^\$?\d+\s*(?:-|to)\s*\$?\d+\b", lower_query):
        score -= 35
    if len(meaningful_tokens) <= 1:
        score -= 15
    if str(target_quality.get("decision", "")) == "reroute_to_query":
        score -= 5
    schema_context = _schema_priority_context(row_schema, schema_refinement)
    combined_schema_text = _normalize_space(
        " ".join(
            bit
            for bit in (
                title,
                candidate_name,
                str(row.get("snippet", "")),
                query_text,
                str(entrypoint.get("site_hint", "")),
            )
            if bit
        )
    ).lower()
    positive_terms = schema_context.get("positive_terms", set())
    negative_terms = schema_context.get("negative_terms", set())
    positive_hits = sum(1 for term in positive_terms if term and term in combined_schema_text)
    negative_hits = sum(1 for term in negative_terms if term and term in combined_schema_text)
    if positive_hits:
        score += min(positive_hits * 8, 20)
    if negative_hits:
        score -= min(negative_hits * 10, 30)
    if target_name == "products":
        lower_value = str(entrypoint.get("value", "")).strip().lower()
        site_hint = str(entrypoint.get("site_hint", "")).strip()
        if route_source_type == "retailer_search":
            score += 18
        elif route_source_type == "brand_catalog_search":
            score += 12
        elif route_source_type == "review_comparison_search":
            score -= 18
        elif route_source_type == "detail_followup":
            score += 42
        if str(entrypoint.get("mode", "")) == "query" and site_hint and route_source_type in {"retailer_search", "brand_catalog_search"}:
            score += 18
        if _looks_like_editorial_product_title(title or base_text):
            score -= 45
        if "/zgbs/" in lower_value or "/gp/bestsellers/" in lower_value:
            score -= 45
        if re.search(r"\b\d+\s*(?:-|to)\s*\d+\b", lower_value):
            score -= 30
    if target_name == "neighborhood_price_rankings":
        snippet = _normalize_space(str(row.get("snippet", "")))
        combined_text = _normalize_space(" ".join(bit for bit in (title, candidate_name, snippet, query_text) if bit)).lower()
        lower_value = str(entrypoint.get("value", "")).strip().lower()
        site_hint = str(entrypoint.get("site_hint", "")).strip()
        hinted_domain = _normalize_domain(site_hint)
        entrypoint_domain = _normalize_domain(_domain_from_url(str(entrypoint.get("value", "")).strip()))
        if route_source_type == "official_source_search":
            score += 18
        elif route_source_type == "web_search_query":
            score += 6
        if any(
            term in combined_text
            for term in (
                "most expensive neighborhoods",
                "wealthiest neighborhoods",
                "richest neighborhoods",
                "luxury homes for sale",
                "real estate & homes for sale",
                "neighborhood",
                "home values",
                "median sale price",
                "median home price",
            )
        ):
            score += 18
        if any(
            term in combined_text
            for term in (
                "housing market",
                "research data",
                "market data",
                "market trends",
                "home prices & trends",
                "housing data",
                "market explorer",
            )
        ):
            score -= 42
        if any(term in combined_text for term in ("zip code", "zip codes", "apartment rentals", "for rent", "suburbs")):
            score -= 55
        if str(entrypoint.get("mode", "")) == "url" and hinted_domain and entrypoint_domain and hinted_domain != entrypoint_domain:
            score -= 25
        if any(fragment in lower_value for fragment in ("/research/", "/research/data", "/market-data", "/data", "/housing-market")):
            score -= 48
        if any(fragment in lower_value for fragment in ("/homes/", "/real-estate/", "/luxury-homes-for-sale", "/neighborhood", "/community")):
            score += 22
    if target_name == "market_contracts":
        snippet = _normalize_space(str(row.get("snippet", "")))
        combined_text = _normalize_space(" ".join(bit for bit in (title, candidate_name, snippet, query_text) if bit)).lower()
        lower_value = str(entrypoint.get("value", "")).strip().lower()
        site_hint = str(entrypoint.get("site_hint", "")).strip()
        source_domain = _normalize_domain(_domain_from_url(str(entrypoint.get("value", "")).strip()))
        hinted_domain = _normalize_domain(site_hint or str(row.get("domain_hint", "")).strip())
        candidate_platform_domain = _normalize_domain(_market_platform_domain_hint(candidate_name))
        has_platform_backing = any(
            _is_known_market_platform_domain(domain)
            for domain in (source_domain, hinted_domain, candidate_platform_domain)
            if domain
        )
        has_direct_signals = any(re.search(pattern, combined_text) for pattern in MARKET_CONTRACT_DIRECT_SIGNAL_PATTERNS)
        has_guide_signals = any(re.search(pattern, combined_text) for pattern in MARKET_CONTRACT_GUIDE_PATTERNS)
        has_commentary_signals = any(re.search(pattern, combined_text) for pattern in MARKET_CONTRACT_COMMENTARY_PATTERNS)
        has_metric_signals = any(token in combined_text for token in ("yes", "no", "volume", "liquidity", "market data", "open interest"))
        has_artifact_signals = _looks_like_market_contract_artifact(
            " ".join(bit for bit in (title, candidate_name, snippet, query_text, lower_value, site_hint) if bit)
        )
        has_noise_domain = any(
            _is_market_contract_noise_domain(domain)
            for domain in (source_domain, hinted_domain)
            if domain
        )
        if route_source_type == "detail_followup":
            score += 36
            if not has_platform_backing:
                score -= 130
        if route_source_type == "official_source_search":
            score += 20
        elif route_source_type == "market_analytics_search":
            score += 12
        elif route_source_type == "prediction_market_search":
            score += 6
        if has_direct_signals:
            score += 24
        if has_guide_signals:
            score -= 60
        if has_commentary_signals and not has_direct_signals:
            score -= 35
        if route_source_type == "seed_url" and not has_platform_backing:
            score -= 95
        if has_artifact_signals and not has_platform_backing:
            score -= 180
        if has_noise_domain and not has_platform_backing:
            score -= 140
        if not has_platform_backing and not has_metric_signals:
            score -= 90
        if str(entrypoint.get("mode", "")) == "url" and not has_platform_backing:
            score -= 150
            if has_direct_signals and has_metric_signals:
                score += 70
        if str(entrypoint.get("mode", "")) == "query" and has_noise_domain and not has_platform_backing:
            score -= 120
        if has_commentary_signals and not has_platform_backing:
            score -= 55
        if any(fragment in lower_value for fragment in ("/learn/", "/guide", "/faq", "/blog/", "/news/", "/article")):
            score -= 25
        if any(fragment in lower_value for fragment in ("/resolve/main", "/blob/", "/tree/", ".txt", ".csv", ".json", ".parquet", ".tsv", ".zip")):
            score -= 160
        if "prediction-markets" in lower_value and not has_guide_signals:
            score += 10
        if "market data" in combined_text or "market analytics" in combined_text:
            score += 10
        if not has_direct_signals and not any(token in combined_text for token in ("polymarket", "kalshi", "market", "contract")):
            score -= 30
        if has_guide_signals and not has_direct_signals:
            score -= 55
    if target_name == "rental_listings":
        lower_value = str(entrypoint.get("value", "")).strip().lower()
        combined_text = _normalize_space(" ".join(bit for bit in (title, candidate_name, str(row.get("snippet", "")), query_text) if bit)).lower()
        if route_source_type in {"rental_marketplace_search", "apartment_directory_search"}:
            score += 14
        elif route_source_type == "property_management_search":
            score += 8
        if any(term in combined_text for term in ("apartments for rent", "under $", "studio", "1 bed", "2 beds", "beds", "baths")):
            score += 16
        if any(term in combined_text for term in ("404", "page not found", "oops", "error")):
            score -= 80
        if any(term in combined_text for term in ("arizona apartments and homes", "state guide", "moving center")):
            score -= 28
        if "/arizona" in lower_value and "/phoenix" not in lower_value and "phoenix" not in combined_text:
            score -= 22
    if target_name == "coworking_spaces":
        lower_value = str(entrypoint.get("value", "")).strip().lower()
        combined_text = _normalize_space(" ".join(bit for bit in (title, candidate_name, str(row.get("snippet", "")), query_text) if bit)).lower()
        if route_source_type == "official_source_search":
            score += 18
        elif route_source_type == "review_directory_search":
            score += 12
        elif route_source_type == "map_directory_search":
            score += 6
        if any(term in combined_text for term in ("per month", "/month", "dedicated desk", "hot desk", "monthly desk")):
            score += 16
        if any(term in combined_text for term in ("blog", "resources", "latest posts", "news", "page not found", "no results found")):
            score -= 48
        if re.search(r"\b(top \d+|best coworking spaces|average price)\b", combined_text):
            score -= 24
        if ":" in title and not re.search(r"\b(top \d+|best)\b", title.lower()):
            score += 10
    return {
        "priority_score": score,
        "entity_key": entity_key,
        "meaningful_token_count": len(meaningful_tokens),
        "skip": score < 20,
    }


def _looks_like_direct_detail_url(url: str) -> bool:
    clean = str(url or "").strip()
    if not clean:
        return False
    if _is_search_engine_url(clean):
        return False
    if "#product=" in clean:
        return False
    parsed = urlparse(clean)
    if parsed.query and any(key in parse_qs(parsed.query) for key in ("s", "k", "search", "q")):
        return False
    return not _looks_like_broad_category_url(clean)


def _build_product_detail_followup_targets(
    capsule_dir: Optional[Path],
    task_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    if capsule_dir is None:
        return []
    rows = _read_jsonl_rows(capsule_dir / "tables" / "products.jsonl")
    if not rows:
        return []

    targets: list[dict[str, Any]] = []
    prompt_lower = str(task_spec.get("user_prompt", "")).strip().lower()
    allow_filter_accessories = "filter" in prompt_lower or "replacement" in prompt_lower
    for row in rows:
        product_name = _normalize_space(str(row.get("product_name", "")))
        if not product_name:
            continue
        if _looks_like_junk_product_name(product_name):
            continue
        if not _product_row_matches_mission(row, task_spec):
            continue
        row_category = str(row.get("category", "")).strip().lower()
        if row_category in {"air_purifier_filter", "replacement_filter"} and not allow_filter_accessories:
            continue
        if row_category == "book" and "book" not in prompt_lower:
            continue
        if row_category == "baby_toy" and not any(token in prompt_lower for token in ("baby", "infant", "toddler", "toy", "toys")):
            continue
        if row_category == "generic_product" and product_name.lower() in {"out of stock", "add to cart"}:
            continue
        product_url = str(row.get("product_url", "")).strip()
        source_url = str(row.get("source_url", "")).strip()
        missing_metrics = any(
            _is_missing(row.get(field))
            for field in ("rating_value", "review_count")
        )
        if not missing_metrics and _looks_like_direct_detail_url(product_url):
            continue
        retailer_domain = _normalize_domain(_domain_from_url(source_url))
        if not retailer_domain:
            continue
        entrypoint: dict[str, str]
        if _looks_like_direct_detail_url(product_url):
            entrypoint = {"mode": "url", "value": product_url, "site_hint": retailer_domain}
        else:
            normalized_query = _normalize_target_query_value(product_name)
            if not normalized_query:
                continue
            entrypoint = {"mode": "query", "value": normalized_query, "site_hint": retailer_domain}
        targets.append(
            {
                "candidate_id": "detail-{digest}".format(
                    digest=sha1("{domain}||{name}".format(domain=retailer_domain, name=product_name).encode("utf-8")).hexdigest()[:10]
                ),
                "page_id": "",
                "title": product_name,
                "candidate_name": product_name,
                "domain_hint": retailer_domain,
                "entrypoint": entrypoint,
                "scout_score": 80,
                "route_source_type": "detail_followup",
                "target_quality": {
                    "decision": "detail_followup",
                    "reason": "missing_product_metrics" if missing_metrics else "direct_detail_capture",
                },
            }
        )
        if len(targets) >= 8:
            break
    return targets


def _split_market_platform_candidates(raw: str) -> list[str]:
    text = _normalize_space(raw.replace(" and ", ", "))
    values = [item.strip(" ,.;:") for item in text.split(",")]
    names: list[str] = []
    for value in values:
        clean = _normalize_space(value)
        if not clean or len(clean) > 40:
            continue
        if not all(part[:1].isupper() or part.isupper() for part in clean.split()):
            continue
        if clean in MARKET_PLATFORM_NAME_STOPWORDS:
            continue
        if any(part in MARKET_PLATFORM_NAME_STOPWORDS for part in clean.split()):
            continue
        names.append(clean)
    return _ordered_unique(names)


def _extract_market_platform_names(page_text: str) -> list[str]:
    names: list[str] = []
    normalized = _normalize_space(page_text)
    for match in re.finditer(
        r"platforms? like ([A-Z][A-Za-z0-9-]+(?:\s*,\s*[A-Z][A-Za-z0-9-]+)*(?:\s*,?\s+and\s+[A-Z][A-Za-z0-9-]+)?)",
        normalized,
    ):
        names.extend(_split_market_platform_candidates(match.group(1)))
    for match in re.finditer(
        r"(?:Feature|Platform)\s+([A-Z][A-Za-z0-9-]+(?:\s+[A-Z][A-Za-z0-9-]+){0,5})",
        page_text,
    ):
        names.extend(_split_market_platform_candidates(match.group(1)))
    for raw_line in page_text.splitlines():
        clean = _normalize_space(raw_line)
        match = re.match(r"^([A-Z][A-Za-z0-9-]+(?:\s+[A-Z][A-Za-z0-9-]+){0,2})\s+crypto markets?$", clean)
        if match:
            names.extend(_split_market_platform_candidates(match.group(1)))
        possessive_match = re.match(
            r"^([A-Z][A-Za-z0-9-]+)'s\b.{0,60}\bprediction markets?\b",
            clean,
            re.I,
        )
        if possessive_match:
            names.extend(_split_market_platform_candidates(possessive_match.group(1)))
        leading_match = re.match(
            r"^([A-Z][A-Za-z0-9-]+)\b.{0,40}\bactive prediction markets?\b",
            clean,
            re.I,
        )
        if leading_match:
            names.extend(_split_market_platform_candidates(leading_match.group(1)))
        rise_market_match = re.match(
            r"^([A-Z][A-Za-z0-9-]+)\s+and\s+the\s+rise\s+of\b.*\bmarkets?\b",
            clean,
            re.I,
        )
        if rise_market_match:
            names.extend(_split_market_platform_candidates(rise_market_match.group(1)))
        macro_market_match = re.match(
            r"^([A-Z][A-Za-z0-9-]+)\b.{0,60}\bmacro markets?\b",
            clean,
            re.I,
        )
        if macro_market_match:
            names.extend(_split_market_platform_candidates(macro_market_match.group(1)))
    return _ordered_unique(names)[:6]


def _market_contract_followup_query(prompt: str, platform_name: str) -> str:
    prompt_lower = prompt.lower()
    focus_terms: list[str] = []
    for token in (
        "bitcoin",
        "ethereum",
        "stablecoin",
        "solana",
        "xrp",
        "dogecoin",
        "nfl",
        "free agency",
        "draft",
        "quarterback",
        "interest rates",
        "fed",
        "inflation",
        "election",
    ):
        if token in prompt_lower:
            focus_terms.append(token)
    focus = " ".join(focus_terms[:3]).strip()
    return _normalize_target_query_value(
        "{platform} active prediction markets odds volume {focus}".format(
            platform=platform_name,
            focus=focus,
        )
    )


def _market_platform_domain_hint(platform_name: str) -> str:
    clean = _normalize_space(platform_name).lower()
    platform_map = {
        "polymarket": "polymarket.com",
        "kalshi": "kalshi.com",
        "predictit": "predictit.org",
        "manifold": "manifold.markets",
        "metaculus": "metaculus.com",
    }
    return platform_map.get(clean, "")


def _is_known_market_platform_domain(domain: str) -> bool:
    clean = _normalize_domain(domain)
    if not clean:
        return False
    return clean in {
        _normalize_domain("polymarket.com"),
        _normalize_domain("kalshi.com"),
        _normalize_domain("predictit.org"),
        _normalize_domain("manifold.markets"),
        _normalize_domain("metaculus.com"),
    }


def _looks_like_market_contract_artifact(text: str) -> bool:
    clean = _normalize_space(text).lower()
    if not clean:
        return False
    return any(re.search(pattern, clean) for pattern in MARKET_CONTRACT_ARTIFACT_PATTERNS)


def _is_market_contract_noise_domain(domain: str) -> bool:
    clean = _normalize_domain(domain)
    if not clean:
        return False
    return clean in MARKET_CONTRACT_NOISE_DOMAINS or clean.endswith(".edu")


def _build_market_contract_detail_followup_targets(
    capsule_dir: Optional[Path],
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    if capsule_dir is None:
        return []

    prompt = str(task_spec.get("user_prompt", "")).strip()
    targets: list[dict[str, Any]] = []
    seen_queries: set[str] = set()

    def _append_platform_followups(
        *,
        platform_names: list[str],
        page_id: str,
        scout_score: int,
    ) -> None:
        for platform_name in _ordered_unique(platform_names):
            domain_hint = _market_platform_domain_hint(platform_name)
            if not domain_hint:
                continue
            query_value = _market_contract_followup_query(prompt, platform_name)
            if not query_value or query_value in seen_queries:
                continue
            seen_queries.add(query_value)
            targets.append(
                {
                    "candidate_id": "detail-{digest}".format(
                        digest=sha1(query_value.encode("utf-8", "replace")).hexdigest()[:10]
                    ),
                    "page_id": page_id,
                    "title": "{platform} active prediction markets".format(platform=platform_name),
                    "candidate_name": platform_name,
                    "domain_hint": domain_hint,
                    "entrypoint": {
                        "mode": "query",
                        "value": query_value,
                        "site_hint": domain_hint,
                    },
                    "scout_score": scout_score,
                    "route_source_type": "detail_followup",
                    "target_quality": {
                        "decision": "detail_followup",
                        "reason": "platform_market_followup",
                    },
                }
            )
            if len(targets) >= 8:
                return

    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        page_text = _read_page_text(capsule_dir, page)
        if not page_text.strip():
            continue
        _append_platform_followups(
            platform_names=_extract_market_platform_names(page_text),
            page_id=str(page.get("page_id", "")),
            scout_score=82,
        )
        if len(targets) >= 8:
            return targets

    scout_index = _read_json(capsule_dir / "scout_index.json", {})
    for row in scout_index.get("rows", []):
        if not isinstance(row, dict):
            continue
        combined_text = "\n".join(
            [
                str(row.get("title", "")),
                str(row.get("candidate_name", "")),
                str(row.get("snippet", "")),
            ]
        )
        _append_platform_followups(
            platform_names=_extract_market_platform_names(combined_text),
            page_id=str(row.get("page_id", "")),
            scout_score=max(78, int(row.get("scout_score", 0) or 0)),
        )
        if len(targets) >= 8:
            return targets
    return targets


def _apply_schema_retry_priority(
    capsule_dir: Optional[Path],
    provisional_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if capsule_dir is None or not provisional_targets:
        return provisional_targets

    gather_qa = _read_json(capsule_dir / "gather_qa.json", {})
    previous_targets = _read_json(capsule_dir / "gather_targets.json", {})
    qa_rows = [row for row in gather_qa.get("rows", []) if isinstance(row, dict)]
    previous_by_id = {
        str(item.get("target_id", "")).strip(): item
        for item in previous_targets.get("targets", [])
        if isinstance(item, dict) and str(item.get("target_id", "")).strip()
    }

    retry_signals: list[dict[str, str]] = []
    for row in qa_rows:
        status = str(row.get("qa_status", "")).strip()
        reasons = {str(item).strip() for item in row.get("reasons", []) if str(item).strip()}
        if status not in {"redirect", "retry", "blocked"}:
            continue
        if not reasons.intersection({"schema_page_mismatch", "domain_mismatch", "search_engine_page", "weak_target_match"}):
            continue
        target = previous_by_id.get(str(row.get("gather_target_id", "")).strip(), {})
        entrypoint = dict(target.get("entrypoint") or {})
        preferred_site = (
            _normalize_domain(str(row.get("expected_domain", "")).strip())
            or _normalize_domain(_domain_from_url(str((row.get("suggested_entrypoint") or {}).get("value", "")).strip()))
            or _normalize_domain(_query_site_hint(str((row.get("suggested_entrypoint") or {}).get("value", "")).strip()))
            or _normalize_domain(str(entrypoint.get("site_hint", "")).strip())
        )
        retry_signals.append(
            {
                "site": preferred_site,
                "route_source_type": str(target.get("route_source_type", "")).strip().lower(),
                "bad_page_id": str(row.get("page_id", "")).strip(),
            }
        )

    if not retry_signals:
        return provisional_targets

    boosted: list[dict[str, Any]] = []
    for item in provisional_targets:
        target_quality = dict(item.get("target_quality") or {})
        entrypoint = dict(item.get("entrypoint") or {})
        route_source_type = str(item.get("route_source_type", "")).strip().lower()
        site_hint = _normalize_domain(
            str(entrypoint.get("site_hint", "")).strip() or str(item.get("domain_hint", "")).strip()
        )
        boost = 0
        for signal in retry_signals:
            if signal["site"] and site_hint and signal["site"] == site_hint:
                boost += 24
            if signal["route_source_type"] and route_source_type and signal["route_source_type"] == route_source_type:
                boost += 12
        if boost > 0:
            target_quality["priority_score"] = int(target_quality.get("priority_score", 0) or 0) + boost
            target_quality["recovery_boost"] = boost
            target_quality["recovery_reason"] = "schema_retry_promotion"
        boosted.append({**item, "target_quality": target_quality})
    return boosted


def build_gather_targets(
    scout_index: dict[str, Any],
    manifest: dict[str, Any],
    task_spec: Optional[dict[str, Any]] = None,
    capsule_dir: Optional[Path] = None,
    row_schema: Optional[dict[str, Any]] = None,
    schema_refinement: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = [row for row in scout_index.get("rows", []) if isinstance(row, dict)]
    captured_by_target: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for page in manifest.get("pages", []):
        target_id = str(page.get("gather_target_id", "")).strip()
        if not target_id:
            continue
        entrypoint = page.get("source_entrypoint") or {}
        mode = str(entrypoint.get("mode", "")).strip()
        value = str(entrypoint.get("value", "")).strip()
        if mode and value:
            captured_by_target[target_id].add((mode, value))
            continue
        requested_url = str(page.get("requested_url", "")).strip()
        final_url = str(page.get("final_url", "")).strip()
        for candidate_url in (requested_url, final_url):
            if candidate_url:
                captured_by_target[target_id].add(("url", candidate_url))
    seen_entrypoints: set[tuple[str, str]] = set()
    provisional_targets: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item.get("scout_score", 0)), reverse=True):
        entrypoint, target_quality = _normalize_gather_entrypoint(
            row=row,
            task_spec=task_spec or {},
        )
        mode = str(entrypoint.get("mode", "")).strip()
        value = str(entrypoint.get("value", "")).strip()
        site_hint = str(entrypoint.get("site_hint", "")).strip()
        if not mode or not value:
            continue
        dedupe_key = (mode, value)
        if dedupe_key in seen_entrypoints:
            continue
        seen_entrypoints.add(dedupe_key)
        priority_meta = _target_priority(
            row=row,
            entrypoint={"mode": mode, "value": value, "site_hint": site_hint},
            target_quality=target_quality,
            task_spec=task_spec or {},
            row_schema=row_schema,
            schema_refinement=schema_refinement,
        )
        provisional_targets.append(
            {
                "candidate_id": str(row.get("candidate_id", "")),
                "page_id": str(row.get("page_id", "")),
                "title": str(row.get("title", "")),
                "candidate_name": str(row.get("candidate_name", "")),
                "domain_hint": str(row.get("domain_hint", "")),
                "route_source_type": str(row.get("route_source_type", "")),
                "entrypoint": {"mode": mode, "value": value, "site_hint": site_hint},
                "scout_score": int(row.get("scout_score", 0)),
                "target_quality": {
                    **target_quality,
                    **priority_meta,
                },
            }
        )

    provisional_targets.sort(
        key=lambda item: (
            int((item.get("target_quality") or {}).get("priority_score", 0)),
            int(item.get("scout_score", 0)),
        ),
        reverse=True,
    )

    target_object_name = _target_object_name(task_spec or {})
    if target_object_name == "products":
        seen_provisional = {
            (
                str((item.get("entrypoint") or {}).get("mode", "")).strip(),
                str((item.get("entrypoint") or {}).get("value", "")).strip(),
            )
            for item in provisional_targets
        }
        for item in _build_product_detail_followup_targets(capsule_dir, task_spec or {}):
            entrypoint = dict(item.get("entrypoint") or {})
            dedupe_key = (
                str(entrypoint.get("mode", "")).strip(),
                str(entrypoint.get("value", "")).strip(),
            )
            if dedupe_key in seen_provisional:
                continue
            priority_meta = _target_priority(
                row=item,
                entrypoint={
                    "mode": str(entrypoint.get("mode", "")),
                    "value": str(entrypoint.get("value", "")),
                    "site_hint": str(entrypoint.get("site_hint", "")),
                },
                target_quality=dict(item.get("target_quality") or {}),
                task_spec=task_spec or {},
                row_schema=row_schema,
                schema_refinement=schema_refinement,
            )
            provisional_targets.append(
                {
                    **item,
                    "target_quality": {
                        **dict(item.get("target_quality") or {}),
                        **priority_meta,
                    },
                }
            )
            seen_provisional.add(dedupe_key)

        provisional_targets.sort(
            key=lambda item: (
                int((item.get("target_quality") or {}).get("priority_score", 0)),
                int(item.get("scout_score", 0)),
            ),
            reverse=True,
        )
    elif target_object_name == "market_contracts":
        seen_provisional = {
            (
                str((item.get("entrypoint") or {}).get("mode", "")).strip(),
                str((item.get("entrypoint") or {}).get("value", "")).strip(),
            )
            for item in provisional_targets
        }
        for item in _build_market_contract_detail_followup_targets(capsule_dir, manifest, task_spec or {}):
            entrypoint = dict(item.get("entrypoint") or {})
            dedupe_key = (
                str(entrypoint.get("mode", "")).strip(),
                str(entrypoint.get("value", "")).strip(),
            )
            if dedupe_key in seen_provisional:
                continue
            priority_meta = _target_priority(
                row=item,
                entrypoint={
                    "mode": str(entrypoint.get("mode", "")),
                    "value": str(entrypoint.get("value", "")),
                    "site_hint": str(entrypoint.get("site_hint", "")),
                },
                target_quality=dict(item.get("target_quality") or {}),
                task_spec=task_spec or {},
                row_schema=row_schema,
                schema_refinement=schema_refinement,
            )
            provisional_targets.append(
                {
                    **item,
                    "target_quality": {
                        **dict(item.get("target_quality") or {}),
                        **priority_meta,
                    },
                }
            )
            seen_provisional.add(dedupe_key)

        provisional_targets.sort(
            key=lambda item: (
                int((item.get("target_quality") or {}).get("priority_score", 0)),
                int(item.get("scout_score", 0)),
            ),
            reverse=True,
        )

    provisional_targets = _apply_schema_retry_priority(capsule_dir, provisional_targets)
    provisional_targets.sort(
        key=lambda item: (
            int((item.get("target_quality") or {}).get("priority_score", 0)),
            int(item.get("scout_score", 0)),
        ),
        reverse=True,
    )
    targets: list[dict[str, Any]] = []
    entity_counts: Counter[tuple[str, str]] = Counter()
    generic_site_counts: Counter[str] = Counter()
    row_level_target = _is_row_level_target(task_spec or {})
    for item in provisional_targets:
        quality = dict(item.get("target_quality") or {})
        if row_level_target and bool(quality.get("skip")):
            continue
        entrypoint = dict(item.get("entrypoint") or {})
        site_hint = str(entrypoint.get("site_hint", "")).strip() or str(item.get("domain_hint", "")).strip()
        entity_key = str(quality.get("entity_key", "")).strip()
        if row_level_target and entity_key:
            count_key = (site_hint.lower(), entity_key.lower())
            if entity_counts[count_key] >= 2:
                continue
            entity_counts[count_key] += 1
        elif row_level_target:
            site_key = site_hint.lower()
            if site_key and generic_site_counts[site_key] >= 3:
                continue
            if site_key:
                generic_site_counts[site_key] += 1
        target_id = "tgt-{index:03d}".format(index=len(targets) + 1)
        targets.append(
            {
                "target_id": target_id,
                **item,
                "gather_status": "captured"
                if (
                    str(entrypoint.get("mode", "")).strip(),
                    str(entrypoint.get("value", "")).strip(),
                )
                in captured_by_target.get(target_id, set())
                else "planned",
            }
        )
        if len(targets) >= 12:
            break
    return {
        "generated_at": now_iso(),
        "targets": targets,
    }


SEARCH_ENGINE_DOMAINS = (
    "google.com",
    "bing.com",
    "search.yahoo.com",
    "duckduckgo.com",
)

GATHER_QA_STATUSES = {"accepted", "retry", "redirect", "duplicate", "blocked"}

BLOCKED_CAPTURE_PATTERNS = (
    r"verify you are human",
    r"captcha",
    r"access denied",
    r"unusual traffic",
    r"sign in to continue",
    r"please sign in",
    r"robot check",
    r"temporarily unavailable",
    r"enable cookies",
)


def _normalize_domain(domain: str) -> str:
    clean = str(domain or "").strip().lower()
    if clean.startswith("www."):
        clean = clean[4:]
    return clean


def _domain_matches(expected_domain: str, actual_domain: str) -> bool:
    expected = _normalize_domain(expected_domain)
    actual = _normalize_domain(actual_domain)
    if not expected or not actual:
        return False
    return actual == expected or actual.endswith("." + expected)


def _query_site_hint(query: str) -> str:
    match = re.search(r"\bsite:([a-z0-9.-]+\.[a-z]{2,})\b", str(query).lower())
    if match:
        return match.group(1)
    return ""


def _expected_domain_from_entrypoint(entrypoint: dict[str, Any]) -> str:
    mode = str(entrypoint.get("mode", "")).strip().lower()
    value = str(entrypoint.get("value", "")).strip()
    site_hint = str(entrypoint.get("site_hint", "")).strip()
    if site_hint and "." in site_hint and " " not in site_hint:
        return _normalize_domain(site_hint)
    if mode == "url":
        return _normalize_domain(_domain_from_url(value))
    if mode == "query":
        hinted = _query_site_hint(value)
        if hinted:
            return _normalize_domain(hinted)
    return ""


def _is_search_engine_url(url: str) -> bool:
    domain = _normalize_domain(_domain_from_url(url))
    if not domain:
        return False
    return any(domain == root or domain.endswith("." + root) for root in SEARCH_ENGINE_DOMAINS)


def _text_hash(text: str) -> str:
    compact = _normalize_space(text)
    if not compact:
        return ""
    return sha1(compact.encode("utf-8")).hexdigest()


def _page_title_text(page: dict[str, Any], page_text: str) -> str:
    title = str(page.get("title", "")).strip()
    if title:
        return title
    lines = _nonempty_lines(page_text)
    return lines[0] if lines else ""


def _capture_looks_blocked(page_text: str, page_title: str, final_url: str) -> bool:
    lower_url = str(final_url or "").lower()
    if "/blocked" in lower_url or "access denied" in lower_url:
        return True
    haystack = _normalize_space("{title} {url} {text}".format(title=page_title, url=final_url, text=page_text[:1800]))
    haystack_lower = haystack.lower()
    if any(re.search(pattern, haystack_lower, re.I) for pattern in BLOCKED_CAPTURE_PATTERNS):
        return True
    if "404" in haystack_lower and any(token in haystack_lower for token in ("not found", "oops", "lost that one")):
        return True
    if "oops" in haystack_lower and any(token in haystack_lower for token in ("page you are looking for", "lost that one", "let's get you home")):
        return True
    return False


def _tokenize_for_overlap(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]{3,}", str(text).lower())
        if token not in STOPWORDS
    }
    return tokens


def _target_match_score(target: dict[str, Any], page: dict[str, Any], page_text: str) -> float:
    candidate_bits = [
        str(target.get("title", "")),
        str(target.get("candidate_name", "")),
    ]
    page_bits = [
        str(page.get("title", "")),
        page_text[:1600],
    ]
    target_tokens = _tokenize_for_overlap(" ".join(candidate_bits))
    if not target_tokens:
        return 1.0
    page_tokens = _tokenize_for_overlap(" ".join(page_bits))
    if not page_tokens:
        return 0.0
    overlap = target_tokens & page_tokens
    return round(len(overlap) / max(len(target_tokens), 1), 3)


def build_gather_qa(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    gather_targets: dict[str, Any],
) -> dict[str, Any]:
    row_schema = build_row_schema(task_spec)
    object_name = str(row_schema.get("object_name", "")).strip() or "records"
    targets = [
        target
        for target in gather_targets.get("targets", [])
        if isinstance(target, dict)
    ]
    targets_by_id = {
        str(target.get("target_id", "")).strip(): target
        for target in targets
        if str(target.get("target_id", "")).strip()
    }
    targets_by_entrypoint = {
        (
            str((target.get("entrypoint") or {}).get("mode", "")).strip(),
            str((target.get("entrypoint") or {}).get("value", "")).strip(),
        ): target
        for target in targets
        if str((target.get("entrypoint") or {}).get("mode", "")).strip()
        and str((target.get("entrypoint") or {}).get("value", "")).strip()
    }
    gathered_pages = [
        page
        for page in manifest.get("pages", [])
        if str(page.get("gather_target_id", "")).strip()
    ]
    seen_final_urls: dict[str, str] = {}
    seen_text_hashes: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    object_names = _task_object_names(task_spec)

    for page in gathered_pages:
        target_id = str(page.get("gather_target_id", "")).strip()
        entrypoint = dict(page.get("source_entrypoint") or {})
        entrypoint_key = (
            str(entrypoint.get("mode", "")).strip(),
            str(entrypoint.get("value", "")).strip(),
        )
        target = targets_by_id.get(target_id) or targets_by_entrypoint.get(entrypoint_key, {})
        target_entrypoint = dict(target.get("entrypoint") or entrypoint)
        expected_domain = _expected_domain_from_entrypoint(target_entrypoint)
        requested_url = str(page.get("requested_url", "")).strip()
        final_url = str(page.get("final_url", "")).strip() or requested_url
        actual_domain = _normalize_domain(_domain_from_url(final_url or requested_url))
        page_text = _read_page_text(capsule_dir, page)
        page_title = _page_title_text(page, page_text)
        text_length = len(_normalize_space(page_text))
        text_digest = _text_hash(page_text)
        match_score = _target_match_score(target, page, page_text)
        schema_alignment, schema_alignment_reasons = _schema_alignment_for_page(
            object_name,
            page_title=page_title,
            page_text=page_text,
            actual_domain=actual_domain,
            row_schema=row_schema,
        )
        reasons: list[str] = []
        suggested_next_action = "shape_rows"
        status = "accepted"
        qa_score = 100
        suggested_entrypoint: dict[str, str] = {}

        if _capture_looks_blocked(page_text, page_title, final_url):
            status = "blocked"
            reasons.append("blocked_page")
            qa_score -= 85
            suggested_next_action = "retry_with_fresh_session"
            if target_entrypoint:
                suggested_entrypoint = {
                    "mode": str(target_entrypoint.get("mode", "")),
                    "value": str(target_entrypoint.get("value", "")),
                }
        elif _is_search_engine_url(final_url) and expected_domain:
            status = "redirect"
            reasons.append("search_engine_page")
            qa_score -= 60
            suggested_next_action = "gather_direct_target"
            if target_entrypoint:
                suggested_entrypoint = {
                    "mode": str(target_entrypoint.get("mode", "")),
                    "value": str(target_entrypoint.get("value", "")),
                }
            elif expected_domain:
                suggested_entrypoint = {"mode": "url", "value": "https://{domain}".format(domain=expected_domain)}
        elif expected_domain and actual_domain and not _domain_matches(expected_domain, actual_domain):
            status = "redirect"
            reasons.append("domain_mismatch")
            qa_score -= 45
            suggested_next_action = "gather_expected_domain"
            if target_entrypoint:
                suggested_entrypoint = {
                    "mode": str(target_entrypoint.get("mode", "")),
                    "value": str(target_entrypoint.get("value", "")),
                }
            else:
                suggested_entrypoint = {"mode": "url", "value": "https://{domain}".format(domain=expected_domain)}
        elif text_length < 180:
            status = "retry"
            reasons.append("text_too_short")
            qa_score -= 50
            suggested_next_action = "retry_capture"
            if target_entrypoint:
                suggested_entrypoint = {
                    "mode": str(target_entrypoint.get("mode", "")),
                    "value": str(target_entrypoint.get("value", "")),
                }

        final_url_key = final_url or requested_url
        if status == "accepted" and final_url_key and final_url_key in seen_final_urls:
            status = "duplicate"
            reasons.append("duplicate_final_url")
            qa_score -= 35
            suggested_next_action = "skip_duplicate"
        elif status == "accepted" and text_digest and text_digest in seen_text_hashes:
            status = "duplicate"
            reasons.append("duplicate_page_text")
            qa_score -= 35
            suggested_next_action = "skip_duplicate"

        if status == "accepted" and match_score < 0.12 and expected_domain:
            reasons.append("weak_target_match")
            qa_score -= 15
        if status == "accepted" and not expected_domain:
            reasons.append("no_expected_domain")
            qa_score -= 5
        if status in {"accepted", "retry"} and object_name == "market_contracts" and schema_alignment != "positive":
            status = "redirect"
            reasons.append("schema_page_mismatch")
            if schema_alignment_reasons:
                reasons.extend(schema_alignment_reasons)
            qa_score -= 45
            suggested_next_action = "gather_better_matching_page"
            if target_entrypoint:
                suggested_entrypoint = {
                    "mode": str(target_entrypoint.get("mode", "")),
                    "value": str(target_entrypoint.get("value", "")),
                }
        elif status in {"accepted", "retry"} and schema_alignment == "negative":
            status = "redirect"
            reasons.append("schema_page_mismatch")
            reasons.extend(schema_alignment_reasons)
            qa_score -= 45
            suggested_next_action = "gather_better_matching_page"
            if target_entrypoint:
                suggested_entrypoint = {
                    "mode": str(target_entrypoint.get("mode", "")),
                    "value": str(target_entrypoint.get("value", "")),
                }
        elif status == "accepted" and schema_alignment == "positive":
            reasons.extend(schema_alignment_reasons)
            qa_score += 5

        if final_url_key and status != "duplicate":
            seen_final_urls[final_url_key] = str(page.get("page_id", ""))
        if text_digest and status != "duplicate":
            seen_text_hashes[text_digest] = str(page.get("page_id", ""))

        rows.append(
            {
                "page_id": str(page.get("page_id", "")),
                "gather_target_id": target_id,
                "target_title": str(target.get("title", page.get("target_title", ""))),
                "target_object_names": object_names,
                "qa_status": status,
                "qa_score": max(0, qa_score),
                "reasons": reasons,
                "expected_domain": expected_domain,
                "actual_domain": actual_domain,
                "requested_url": requested_url,
                "final_url": final_url,
                "text_length": text_length,
                "target_match_score": match_score,
                "page_excerpt": _compact(page_text, 420),
                "page_schema_alignment": schema_alignment,
                "suggested_next_action": suggested_next_action,
                "suggested_entrypoint": suggested_entrypoint,
            }
        )

    status_counts = Counter(str(row.get("qa_status", "")) for row in rows)
    accepted_like = status_counts.get("accepted", 0) + status_counts.get("duplicate", 0)
    reviewed_count = len(rows)
    top_reasons = Counter(
        reason
        for row in rows
        for reason in row.get("reasons", [])
        if isinstance(reason, str) and reason
    )
    return {
        "generated_at": now_iso(),
        "reviewed_page_count": reviewed_count,
        "accepted_page_count": status_counts.get("accepted", 0),
        "accepted_like_page_count": accepted_like,
        "accepted_fraction": round(status_counts.get("accepted", 0) / reviewed_count, 3) if reviewed_count else 0.0,
        "accepted_like_fraction": round(accepted_like / reviewed_count, 3) if reviewed_count else 0.0,
        "status_counts": dict(sorted(status_counts.items())),
        "top_reasons": dict(top_reasons.most_common(8)),
        "rows": rows,
    }


def summarize_gather_qa(
    gather_qa: dict[str, Any],
    gather_qa_review: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    base_rows = [
        dict(row)
        for row in gather_qa.get("rows", [])
        if isinstance(row, dict)
    ]
    if not base_rows:
        base_status_counts = {
            str(key): int(value)
            for key, value in dict(gather_qa.get("status_counts") or {}).items()
            if str(key)
        }
        reviewed_count = int(gather_qa.get("reviewed_page_count", 0) or 0)
        accepted_like_fraction = float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0)
        accepted_like_count = int(round(accepted_like_fraction * reviewed_count)) if reviewed_count else 0
        agent_reviewed_page_count = int((gather_qa_review or {}).get("reviewed_page_count", 0) or 0)
        return {
            "reviewed_page_count": reviewed_count,
            "agent_reviewed_page_count": agent_reviewed_page_count,
            "effective_status_counts": base_status_counts,
            "accepted_like_page_count": accepted_like_count,
            "accepted_like_fraction": accepted_like_fraction,
            "rows": [],
        }
    review_rows = [
        row
        for row in (gather_qa_review or {}).get("reviews", [])
        if isinstance(row, dict)
    ]
    review_by_page = {
        str(row.get("page_id", "")).strip(): row
        for row in review_rows
        if str(row.get("page_id", "")).strip()
    }

    merged_rows: list[dict[str, Any]] = []
    for row in base_rows:
        page_id = str(row.get("page_id", "")).strip()
        effective_status = str(row.get("qa_status", "")).strip() or "retry"
        review = review_by_page.get(page_id)
        if review:
            review_status = str(review.get("review_status", "")).strip()
            review_confidence = float(review.get("confidence", 0.0) or 0.0)
            if review_status in GATHER_QA_STATUSES and review_confidence >= 0.5:
                effective_status = review_status
            row["agent_review"] = {
                "review_status": review_status,
                "confidence": review_confidence,
                "rationale": str(review.get("rationale", "")),
                "suggested_next_action": str(review.get("suggested_next_action", "")),
            }
        row["effective_status"] = effective_status
        merged_rows.append(row)

    effective_status_counts = Counter(str(row.get("effective_status", "")) for row in merged_rows)
    accepted_like_count = effective_status_counts.get("accepted", 0) + effective_status_counts.get("duplicate", 0)
    reviewed_count = len(merged_rows)
    agent_reviewed_page_count = sum(1 for row in merged_rows if isinstance(row.get("agent_review"), dict))
    return {
        "reviewed_page_count": reviewed_count,
        "agent_reviewed_page_count": agent_reviewed_page_count,
        "effective_status_counts": dict(sorted(effective_status_counts.items())),
        "accepted_like_page_count": accepted_like_count,
        "accepted_like_fraction": round(accepted_like_count / reviewed_count, 3) if reviewed_count else 0.0,
        "rows": merged_rows,
    }


CHAIN_NAME_PATTERNS = [
    "Chick-fil-A",
    "Raising Cane's",
    "Lee's Famous Recipe Chicken",
    "Church's Texas Chicken",
    "Church's Chicken",
    "Dave's Hot Chicken",
    "Gus's World Famous Fried Chicken",
    "Gus's Fried Chicken",
    "Harold's Chicken",
    "Maryland Fried Chicken",
    "Pioneer Chicken",
    "Fry the Coop",
    "Slim Chickens",
    "Buffalo Wild Wings",
    "Wingstop",
    "Zaxby's",
    "Bojangles",
    "Jollibee",
    "Popeyes",
    "KFC",
]


def _parse_rating_review_price(text: str) -> tuple[Optional[float], Optional[int], str]:
    clean = _normalize_space(text)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    price_tier = ""

    match = re.search(r"\b([0-9](?:\.[0-9])?)\s*out of 5 stars?\s+with\s+([\d,]+)\s+reviews?\b", clean, re.I)
    if match:
        rating = float(match.group(1))
        review_count = _to_int(match.group(2))
    else:
        match = re.search(r"\b([0-9](?:\.[0-9])?)\s*\(([\d,]+)\)", clean)
    if match:
        rating = float(match.group(1))
        review_count = _to_int(match.group(2))
    else:
        match = re.search(r"\b([0-9](?:\.[0-9])?)\s*★?\s*[\u00b7|]\s*([\d,]+)\s+reviews", clean, re.I)
        if match:
            rating = float(match.group(1))
            review_count = _to_int(match.group(2))

    price_match = re.search(r"Price range:\s*(\${1,4})", clean, re.I)
    if price_match:
        price_tier = price_match.group(1)
    return rating, review_count, price_tier


def _canonical_chain_name(title: str, snippet: str = "") -> str:
    combined = _normalize_space("{title} {snippet}".format(title=title, snippet=snippet))
    combined_lower = combined.lower()
    title_clean = _normalize_space(title)
    title_lower = title_clean.lower()
    if "?" in title_clean:
        return ""
    if title_lower.startswith(("is ", "are ", "can ", "what ", "why ", "how ", "i was ")):
        return ""
    if " near " in title_lower and title_lower.startswith("best "):
        return ""
    for pattern in CHAIN_NAME_PATTERNS:
        if pattern.lower() in combined_lower:
            return pattern

    review_match = re.search(r"reviews from\s+(.+)$", title, re.I)
    if review_match:
        return _normalize_space(review_match.group(1))

    location_match = re.search(r"Locations?\s+\|\s+Find\s+(.+?)\s+Near You", title, re.I)
    if location_match:
        return _normalize_space(location_match.group(1))

    all_locations_match = re.search(r"All\s+(.+?)\s+Locations", title, re.I)
    if all_locations_match:
        return _normalize_space(all_locations_match.group(1))

    candidate = title_clean
    candidate = re.sub(r"\s+[|\-:]\s+.*$", "", candidate)
    generic_prefixes = (
        "All ",
        "Locations | ",
        "Best ",
        "Most ",
        "Top ",
        "12 Fried Chicken Chains",
        "21 Best Chicken Chains",
    )
    if any(candidate.startswith(prefix) for prefix in generic_prefixes):
        return ""
    return candidate


def _source_platform_label(domain_hint: str, source_url: str) -> str:
    domain = _domain_from_url(source_url) or str(domain_hint).lower()
    if "yelp" in domain:
        return "Yelp"
    if "tripadvisor" in domain:
        return "TripAdvisor"
    if "google" in domain:
        return "Google"
    if domain:
        return "Official" if any(token in domain for token in ("kfc.com", "gusfriedchicken.com", "locations.")) else domain
    return "Unknown"


def _extract_location_count(text: str) -> Optional[int]:
    patterns = [
        r"(\d[\d,]*)\s+[A-Za-z'&\-\s]+Locations in the United States",
        r"(\d[\d,]*)\s+Locations in the United States",
        r"over\s+(\d[\d,]*)\s+North American locations",
        r"(\d[\d,]*)\s+locations across\s+\d+\s+states",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _to_int(match.group(1))
    return None


STOCK_TICKER_PATTERN = re.compile(r"^\(([A-Z][A-Z0-9.\-]{0,7})\)$")
TRAILING_TICKER_PATTERN = re.compile(r"^([A-Z][A-Za-z0-9&'’.,/()\- ]{2,90}?)\s+([A-Z][A-Z0-9.\-]{0,7})$")
TICKER_LIST_PATTERN = re.compile(r"\b([A-Z][A-Z0-9.\-]{0,7})\b")
STOCK_NAME_STOPWORDS = {
    "summary",
    "share",
    "save",
    "comments",
    "securities in this article",
    "10 best stocks to buy now",
    "10 of the best undervalued international stocks to buy",
}
NON_TICKER_SUFFIXES = {"ADR", "PLC", "NV", "INC", "CORP", "CORPORATION", "HOLDINGS", "GROUP", "SA"}


def _clean_stock_security_name(text: str) -> str:
    clean = _normalize_space(text).strip(" -,:;")
    clean = re.sub(r"\s+\+\d+\s+More$", "", clean, flags=re.I)
    clean = re.sub(r"\s+\(.*?\)$", "", clean)
    clean = re.sub(r"^\d+\s+Best\s+", "", clean, flags=re.I)
    return _normalize_space(clean)


def _looks_like_stock_security_name(text: str) -> bool:
    clean = _clean_stock_security_name(text)
    if not clean:
        return False
    lower = clean.lower()
    if lower in STOCK_NAME_STOPWORDS:
        return False
    if clean.startswith(("Home", "Stocks", "Funds", "ETFs", "Markets")):
        return False
    if clean.endswith(":"):
        return False
    if len(clean.split()) > 12:
        return False
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.?\b", lower):
        return False
    return any(character.isalpha() for character in clean)


def _stock_asset_type(security_name: str) -> str:
    lower = security_name.lower()
    if " etf" in lower or lower.endswith(" etf") or "fund" in lower:
        return "etf"
    return "stock"


def _stock_rows_want_equities_only(task_spec: dict[str, Any]) -> bool:
    prompt_lower = str(task_spec.get("user_prompt", "")).lower()
    return ("stock" in prompt_lower or "stocks" in prompt_lower) and "etf" not in prompt_lower


def _excerpt_from_lines(lines: list[str], index: int) -> str:
    start = max(0, index - 1)
    end = min(len(lines), index + 3)
    return _compact(" ".join(lines[start:end]), limit=220)


def _is_valid_ticker_symbol(value: str) -> bool:
    clean = str(value or "").strip().upper()
    if not clean or clean in NON_TICKER_SUFFIXES:
        return False
    if len(clean) > 7:
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", clean))


def _extract_stock_mentions_from_lines(lines: list[str]) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    list_heading_pattern = re.compile(
        r"\b(best\s+stocks?\s+to\s+buy|best\s+companies?\s+to\s+invest|undervalued\s+international\s+stocks?)\b",
        re.I,
    )

    for index, line in enumerate(lines):
        lower = line.lower()
        if lower == "securities in this article":
            for candidate_index in range(index + 1, min(len(lines) - 1, index + 22)):
                ticker_match = STOCK_TICKER_PATTERN.match(lines[candidate_index + 1].strip())
                if not ticker_match:
                    continue
                security_name = _clean_stock_security_name(lines[candidate_index])
                if not _looks_like_stock_security_name(security_name):
                    continue
                if not _is_valid_ticker_symbol(ticker_match.group(1)):
                    continue
                if security_name.upper() == ticker_match.group(1):
                    continue
                mentions.append(
                    {
                        "security_name": security_name,
                        "ticker": ticker_match.group(1),
                        "thesis_excerpt": _excerpt_from_lines(lines, candidate_index),
                    }
                )
            continue

        if not list_heading_pattern.search(line):
            continue
        section_mentions: list[dict[str, Any]] = []
        for candidate_index in range(index + 1, min(len(lines), index + 24)):
            clean_line = _normalize_space(lines[candidate_index])
            if not clean_line:
                continue
            if len(clean_line) > 110 and re.search(r"[.!?]", clean_line):
                break
            match = TRAILING_TICKER_PATTERN.match(clean_line)
            if not match:
                continue
            security_name = _clean_stock_security_name(match.group(1))
            ticker = match.group(2)
            if not _is_valid_ticker_symbol(ticker):
                continue
            if not _looks_like_stock_security_name(security_name):
                continue
            if security_name.upper() == ticker:
                continue
            section_mentions.append(
                {
                    "security_name": security_name,
                    "ticker": ticker,
                    "thesis_excerpt": _excerpt_from_lines(lines, candidate_index),
                }
            )
        if len(section_mentions) >= 4:
            mentions.extend(section_mentions)
    return mentions


def build_stock_candidate_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    equities_only = _stock_rows_want_equities_only(task_spec)

    for page in manifest.get("pages", []):
        source_url = str(page.get("final_url", "") or page.get("requested_url", "")).strip()
        if not source_url:
            continue
        if _looks_like_google_results_page(page):
            continue
        raw_text = _read_page_text(capsule_dir, page)
        if not raw_text:
            continue
        lines = _nonempty_lines(raw_text)
        if not lines:
            continue
        source_domain = _normalize_domain(_domain_from_url(source_url))
        source_title = str(page.get("title", "")).strip()
        page_id = str(page.get("page_id", "")).strip()

        for mention in _extract_stock_mentions_from_lines(lines):
            security_name = str(mention.get("security_name", "")).strip()
            ticker = str(mention.get("ticker", "")).strip().upper()
            if not security_name or not ticker:
                continue
            asset_type = _stock_asset_type(security_name)
            if equities_only and asset_type != "stock":
                continue
            row_key = (source_url, ticker)
            if row_key in rows_by_key:
                existing = rows_by_key[row_key]
                if len(str(mention.get("thesis_excerpt", ""))) > len(str(existing.get("thesis_excerpt", ""))):
                    existing["thesis_excerpt"] = str(mention.get("thesis_excerpt", "")).strip()
                if security_name != ticker and str(existing.get("security_name", "")).strip() == ticker:
                    existing["security_name"] = security_name
                continue
            rows_by_key[row_key] = {
                "security_name": security_name,
                "ticker": ticker,
                "source_title": source_title or _title_root(lines[0] if lines else ""),
                "source_url": source_url,
                "source_domain": source_domain,
                "asset_type": asset_type,
                "thesis_excerpt": str(mention.get("thesis_excerpt", "")).strip(),
                "source_page_id": page_id,
                "source_page_ids": [page_id] if page_id else [],
            }

    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            str(item.get("ticker", "")),
            str(item.get("source_domain", "")),
            str(item.get("security_name", "")),
        )
    )
    return rows


def _iter_stock_mentions_from_text(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    compact = _normalize_space(text)
    if not compact:
        return mentions
    fragments = [compact]
    fragments.extend(_normalize_space(part) for part in re.split(r"[;•·|]", compact))
    for fragment in fragments:
        if not fragment:
            continue
        explicit_pairs = re.finditer(
            r"([A-Z][A-Za-z0-9&'’.,/()\- ]{2,90}?)\s*\(([A-Z][A-Z0-9.\-]{0,7})\)",
            fragment,
        )
        for match in explicit_pairs:
            security_name = _clean_stock_security_name(match.group(1))
            ticker = match.group(2).strip().upper()
            if not (_looks_like_stock_security_name(security_name) and _is_valid_ticker_symbol(ticker)):
                continue
            mentions.append(
                {
                    "security_name": security_name,
                    "ticker": ticker,
                    "thesis_excerpt": _compact(compact, 220),
                }
            )
    ticker_symbol_matches = re.finditer(
        r"([A-Z][A-Za-z0-9&'’.,/()\- ]{2,90}?)\.\s+Ticker symbol\s+\(([A-Z][A-Z0-9.\-]{0,7})\)",
        compact,
        re.I,
    )
    for match in ticker_symbol_matches:
        security_name = _clean_stock_security_name(match.group(1))
        ticker = match.group(2).strip().upper()
        if not (_looks_like_stock_security_name(security_name) and _is_valid_ticker_symbol(ticker)):
            continue
        mentions.append(
            {
                "security_name": security_name,
                "ticker": ticker,
                "thesis_excerpt": _compact(compact, 220),
            }
        )
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for mention in mentions:
        key = (mention["security_name"], mention["ticker"])
        unique.setdefault(key, mention)
    return list(unique.values())


def _build_stock_candidate_rows_from_scout(
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    equities_only = _stock_rows_want_equities_only(task_spec)
    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = _normalize_space(str(scout_row.get("title", "")))
        snippet = _normalize_space(str(scout_row.get("snippet", "")))
        source_url = _scout_row_source_url(scout_row)
        source_domain = _normalize_domain(_domain_from_url(source_url) or str(scout_row.get("domain_hint", "")))
        if not (title or snippet):
            continue
        for mention in _iter_stock_mentions_from_text("{title} {snippet}".format(title=title, snippet=snippet)):
            security_name = str(mention.get("security_name", "")).strip()
            ticker = str(mention.get("ticker", "")).strip().upper()
            if not security_name or not ticker:
                continue
            asset_type = _stock_asset_type(security_name)
            if equities_only and asset_type != "stock":
                continue
            row_key = (source_url or source_domain, ticker)
            if row_key in rows_by_key:
                continue
            page_id = str(scout_row.get("page_id", "")).strip()
            rows_by_key[row_key] = {
                "security_name": security_name,
                "ticker": ticker,
                "source_title": title,
                "source_url": source_url or "https://{domain}".format(domain=source_domain) if source_domain else "",
                "source_domain": source_domain,
                "asset_type": asset_type,
                "thesis_excerpt": str(mention.get("thesis_excerpt", "")).strip(),
                "source_page_id": page_id,
                "source_page_ids": [page_id] if page_id else [],
            }
    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            str(item.get("ticker", "")),
            str(item.get("source_domain", "")),
            str(item.get("security_name", "")),
        )
    )
    return rows


MARKET_CONTRACT_TITLE_SKIP_PATTERNS = (
    r"^what (?:is|are)\b",
    r"^how\b",
    r"^why\b",
    r"^where\b",
    r"^who\b",
    r"^when\b",
    r"\bfrequently asked questions\b",
    r"\bkey questions\b",
)

def _looks_like_market_contract_title(text: str) -> bool:
    clean = _normalize_space(text)
    if not clean or len(clean) < 12 or len(clean) > 180:
        return False
    lower = clean.lower()
    if any(re.search(pattern, lower) for pattern in MARKET_CONTRACT_TITLE_SKIP_PATTERNS):
        return False
    if "?" in clean:
        return True
    return bool(
        re.search(r"\b(?:will|can|is|does|should|who wins|which party)\b", lower)
        and any(term in lower for term in ("rate", "bitcoin", "stock", "election", "team", "super bowl", "fed", "crypto"))
    )


def _market_contract_price(value: str, *, percent_like: bool = False) -> Optional[float]:
    clean = str(value).strip().replace(",", "").rstrip("¢%")
    if not clean:
        return None
    try:
        numeric = float(clean)
    except ValueError:
        return None
    if percent_like or numeric > 1:
        numeric = numeric / 100.0
    if numeric < 0 or numeric > 1:
        return None
    return round(numeric, 4)


def _extract_market_side_price(text: str, side: str) -> Optional[float]:
    patterns = (
        r"\b{side}\b\s*(?:price\s*)?(?:at\s*)?\$?(0?\.\d+)\b",
        r"\b{side}\b\s*(\d{{1,2}}(?:\.\d+)?)¢\b",
        r"\b{side}\b\s*(\d{{1,2}}(?:\.\d+)?)%\b",
        r"\b{side}\b\s*(\d{{1,2}}(?:\.\d+)?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern.format(side=re.escape(side)), text, re.I)
        if not match:
            continue
        raw = match.group(1)
        percent_like = "%" in match.group(0) or "¢" in match.group(0)
        parsed = _market_contract_price(raw, percent_like=percent_like)
        if parsed is not None:
            return parsed
    return None


def _parse_money_amount(raw: str, suffix: str, multiplier_word: str) -> Optional[float]:
    clean = str(raw).replace(",", "").strip()
    if not clean:
        return None
    try:
        value = float(clean)
    except ValueError:
        return None
    suffix_clean = str(suffix or "").strip().lower()
    word_clean = str(multiplier_word or "").strip().lower()
    if suffix_clean == "k":
        value *= 1_000
    elif suffix_clean == "m" or "million" in word_clean:
        value *= 1_000_000
    elif suffix_clean == "b" or "billion" in word_clean:
        value *= 1_000_000_000
    return round(value, 2)


def _extract_market_money_metric(text: str, label: str) -> Optional[float]:
    label_aliases = {
        "volume": ("volume", "vol", "vol."),
        "liquidity": ("liquidity", "liq", "liq."),
    }
    aliases = label_aliases.get(label.lower(), (label,))
    label_pattern = "(?:{aliases})".format(
        aliases="|".join(re.escape(str(alias)) for alias in aliases if str(alias).strip())
    )
    patterns = (
        r"\b{label}\b[:\s]*\$?(\d[\d,]*(?:\.\d+)?)\s*([KMB]?)\b(?:\s*(million|billion))?".format(label=label_pattern),
        r"\$?(\d[\d,]*(?:\.\d+)?)\s*([KMB]?)\b(?:\s*(million|billion))?\s*\b{label}\b".format(label=label_pattern),
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        value = _parse_money_amount(match.group(1), match.group(2), match.group(3))
        if value is not None:
            return value
    return None


def _extract_first_money_amount(text: str) -> Optional[float]:
    match = re.search(r"\$?(\d[\d,]*(?:\.\d+)?)\s*([KMB]?)\s*(million|billion)?\b", text, re.I)
    if not match:
        return None
    return _parse_money_amount(match.group(1), match.group(2), match.group(3))


def _extract_market_probability(text: str) -> Optional[float]:
    matches = re.findall(r"(\d{1,2}(?:\.\d+)?)%", text)
    for raw in matches:
        parsed = _market_contract_price(raw, percent_like=True)
        if parsed is not None:
            return parsed
    return None


def _extract_market_status(text: str) -> str:
    lower = _normalize_space(text).lower()
    if any(token in lower for token in ("active", "open", "live")):
        return "active"
    if any(token in lower for token in ("closed", "resolved", "settled")):
        return "closed"
    return ""


def _extract_market_resolution_date(text: str) -> str:
    match = re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s+\d{4})?\b",
        text,
        re.I,
    )
    if match:
        return _normalize_space(match.group(0))
    match = re.search(r"\b(?:by|before|on)\s+((?:19|20)\d{2}|year-end)\b", text, re.I)
    if match:
        return _normalize_space(match.group(1))
    return ""


def _infer_market_event_category(text: str) -> str:
    lower = _normalize_space(text).lower()
    generic_hints = {
        "politic": "politics",
        "election": "politics",
        "sports": "sports",
        "super bowl": "sports",
        "crypto": "crypto",
        "bitcoin": "crypto",
        "rates": "macro",
        "inflation": "macro",
        "economy": "macro",
        "tech": "tech",
    }
    for needle, label in generic_hints.items():
        if needle in lower:
            return label
    return ""


def _market_platform_label(source_url: str, page_title: str) -> str:
    domain = _normalize_domain(_domain_from_url(source_url))
    if domain:
        return domain.removeprefix("www.")
    if page_title.strip():
        return _normalize_space(page_title.split("|", 1)[0]).strip()
    return "Unknown"


def _iter_market_contract_candidates(page_text: str) -> list[str]:
    candidates: list[str] = []
    normalized = page_text.replace("?", "?\n")
    for raw_line in normalized.splitlines():
        clean = _normalize_space(raw_line)
        if _looks_like_market_contract_title(clean):
            candidates.append(clean)
    return _ordered_unique(candidates)


def _iter_market_contract_rows_from_google_page(capsule_dir: Path, page: dict[str, Any]) -> list[dict[str, Any]]:
    if not _looks_like_google_results_page(page):
        return []
    raw_text = _read_page_text(capsule_dir, page)
    lines = _nonempty_lines(raw_text)
    if not lines:
        return []
    if "AI Overview" not in raw_text and "prediction market" not in raw_text.lower():
        return []

    source_url = str(page.get("final_url", "") or page.get("requested_url", "")).strip()
    page_title = _page_title_text(page, raw_text)
    page_id = str(page.get("page_id", "")).strip()
    platform = "polymarket" if "polymarket" in raw_text.lower() else ("kalshi" if "kalshi" in raw_text.lower() else "google_search")
    rows: list[dict[str, Any]] = []
    previous_row: Optional[dict[str, Any]] = None

    for line in lines[:220]:
        clean = _normalize_space(line)
        lower = clean.lower()
        if lower.startswith("trading volume:") and previous_row is not None:
            previous_row["volume_usd"] = _extract_market_money_metric(clean, "volume") or _extract_first_money_amount(clean)
            continue
        match = re.match(r"(.{6,140}?):\s*(\d{1,3}(?:\.\d+)?)%(?:[–-](\d{1,3}(?:\.\d+)?)%)?", clean)
        if not match:
            continue
        market_title = _normalize_space(match.group(1))
        if market_title.lower() in (
            "active prediction market events & odds (march 2026)",
            "top monthly desk pricing (approximate)",
            "popular locations by review volume",
            "context",
            "trading volume",
        ):
            continue
        yes_price = _market_contract_price(match.group(2), percent_like=True)
        if yes_price is None:
            continue
        row = {
            "platform": platform,
            "market_title": market_title,
            "market_url": "{url}#market={slug}".format(
                url=source_url,
                slug=sha1(market_title.encode("utf-8", "replace")).hexdigest()[:10],
            ),
            "market_status": "active",
            "yes_price": yes_price,
            "no_price": round(1.0 - yes_price, 4),
            "volume_usd": None,
            "liquidity_usd": None,
            "event_category": _infer_market_event_category(market_title),
            "resolution_date": _extract_market_resolution_date(clean or page_title),
            "source_url": source_url,
            "source_domain": _domain_from_url(source_url),
            "source_title": page_title,
            "source_page_ids": [page_id] if page_id else [],
        }
        rows.append(row)
        previous_row = row
    return rows


def build_market_contract_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        source_url = str(page.get("final_url", "") or page.get("requested_url", "")).strip()
        if not source_url:
            continue
        if _looks_like_google_results_page(page):
            for incoming in _iter_market_contract_rows_from_google_page(capsule_dir, page):
                row_key = (str(incoming.get("platform", "")), str(incoming.get("market_title", "")))
                existing = rows_by_key.get(row_key)
                if existing is None:
                    rows_by_key[row_key] = incoming
                    continue
                for field in ("yes_price", "no_price", "volume_usd", "liquidity_usd", "event_category", "resolution_date"):
                    if existing.get(field) in (None, "", []) and incoming.get(field) not in (None, "", []):
                        existing[field] = incoming[field]
                existing_ids = list(existing.get("source_page_ids", []))
                for source_page_id in incoming.get("source_page_ids", []):
                    if source_page_id not in existing_ids:
                        existing_ids.append(source_page_id)
                existing["source_page_ids"] = existing_ids
            continue
        raw_text = _read_page_text(capsule_dir, page)
        if not raw_text.strip():
            continue
        page_title = _page_title_text(page, raw_text)
        if _capture_looks_blocked(raw_text, page_title, source_url):
            continue
        lower_haystack = _normalize_space("{title} {text}".format(title=page_title, text=raw_text[:2000])).lower()
        if any(re.search(pattern, lower_haystack) for pattern in MARKET_CONTRACT_GUIDE_PATTERNS) and not any(
            re.search(pattern, lower_haystack) for pattern in MARKET_CONTRACT_DIRECT_SIGNAL_PATTERNS
        ):
            continue
        platform = _market_platform_label(source_url, page_title)
        page_id = str(page.get("page_id", "")).strip()
        for market_title in _iter_market_contract_candidates(raw_text):
            title_index = raw_text.find(market_title)
            if title_index < 0:
                continue
            context = raw_text[title_index : title_index + 520]
            yes_price = _extract_market_side_price(context, "yes")
            no_price = _extract_market_side_price(context, "no")
            if yes_price is None:
                yes_price = _extract_market_probability(context)
            volume_usd = _extract_market_money_metric(context, "volume")
            liquidity_usd = _extract_market_money_metric(context, "liquidity")
            market_status = _extract_market_status(context or raw_text) or "active"
            if all(metric is None for metric in (yes_price, no_price, volume_usd, liquidity_usd)):
                continue
            market_url = "{url}#market={slug}".format(
                url=source_url,
                slug=sha1(market_title.encode("utf-8", "replace")).hexdigest()[:10],
            )
            row_key = (platform, market_title)
            incoming = {
                "platform": platform,
                "market_title": market_title,
                "market_url": market_url,
                "market_status": market_status,
                "yes_price": yes_price,
                "no_price": no_price,
                "volume_usd": volume_usd,
                "liquidity_usd": liquidity_usd,
                "event_category": _infer_market_event_category(context or market_title),
                "resolution_date": _extract_market_resolution_date(context or market_title),
                "source_url": source_url,
                "source_domain": _domain_from_url(source_url),
                "source_title": page_title,
                "source_page_ids": [page_id] if page_id else [],
            }
            existing = rows_by_key.get(row_key)
            if existing is None:
                rows_by_key[row_key] = incoming
                continue
            for field in ("yes_price", "no_price", "volume_usd", "liquidity_usd", "event_category", "resolution_date"):
                if existing.get(field) in (None, "", []) and incoming.get(field) not in (None, "", []):
                    existing[field] = incoming[field]
            existing_ids = list(existing.get("source_page_ids", []))
            for source_page_id in incoming.get("source_page_ids", []):
                if source_page_id not in existing_ids:
                    existing_ids.append(source_page_id)
            existing["source_page_ids"] = existing_ids

    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            item.get("yes_price") is None,
            item.get("volume_usd") is None,
            float(item.get("volume_usd") or 0.0),
            str(item.get("market_title", "")),
        ),
        reverse=True,
    )
    return rows


def _looks_like_restaurant_chain_target(task_spec: dict[str, Any]) -> bool:
    target = _first_target_object(task_spec)
    return str(target.get("name", "")).strip() == "restaurant_chains"


def build_restaurant_chain_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = str(scout_row.get("title", ""))
        snippet = str(scout_row.get("snippet", ""))
        brand_name = _canonical_chain_name(str(scout_row.get("candidate_name", "")) or title, snippet)
        if not brand_name:
            continue
        raw_url_text = str(scout_row.get("raw_url_text", ""))
        followup_entrypoint = scout_row.get("followup_entrypoint") or {}
        source_url = ""
        if raw_url_text.startswith("https://") and "›" not in raw_url_text and " " not in raw_url_text:
            source_url = raw_url_text
        elif str(followup_entrypoint.get("mode", "")) == "url":
            source_url = str(followup_entrypoint.get("value", ""))
        platform = _source_platform_label(str(scout_row.get("domain_hint", "")), source_url)
        key = (brand_name, platform)
        row = rows_by_key.setdefault(
            key,
            {
                "brand_name": brand_name,
                "source_platform": platform,
                "source_url": source_url,
                "rating_value": None,
                "review_count": None,
                "location_count": None,
                "price_tier": "",
                "region_scope": "",
                "source_page_ids": [],
            },
        )
        rating_value, review_count, price_tier = _parse_rating_review_price("{title} {snippet}".format(title=title, snippet=snippet))
        if rating_value is not None and row.get("rating_value") in (None, ""):
            row["rating_value"] = rating_value
        if review_count is not None and int(review_count) > int(row.get("review_count") or 0):
            row["review_count"] = review_count
        if price_tier and not row.get("price_tier"):
            row["price_tier"] = price_tier
        if source_url and not row.get("source_url"):
            row["source_url"] = source_url
        if "united states" in snippet.lower():
            row["region_scope"] = "national"
        page_id = str(scout_row.get("page_id", "")).strip()
        if page_id and page_id not in row["source_page_ids"]:
            row["source_page_ids"].append(page_id)

    for page in manifest.get("pages", []):
        if not str(page.get("gather_target_id", "")).strip():
            continue
        title = str(page.get("title", "") or page.get("target_title", ""))
        raw_text = _clean_page_text(_read_page_text(capsule_dir, page))
        brand_name = _canonical_chain_name(title, raw_text[:280])
        if not brand_name:
            continue
        source_url = str(page.get("final_url", "") or page.get("requested_url", ""))
        platform = _source_platform_label(_domain_from_url(source_url), source_url)
        key = (brand_name, platform)
        row = rows_by_key.setdefault(
            key,
            {
                "brand_name": brand_name,
                "source_platform": platform,
                "source_url": source_url,
                "rating_value": None,
                "review_count": None,
                "location_count": None,
                "price_tier": "",
                "region_scope": "",
                "source_page_ids": [],
            },
        )
        location_count = _extract_location_count(raw_text)
        if location_count is not None and int(location_count) > int(row.get("location_count") or 0):
            row["location_count"] = location_count
        if "united states" in raw_text.lower():
            row["region_scope"] = "national"
        if source_url and not row.get("source_url"):
            row["source_url"] = source_url
        page_id = str(page.get("page_id", "")).strip()
        if page_id and page_id not in row["source_page_ids"]:
            row["source_page_ids"].append(page_id)

    rows = list(rows_by_key.values())
    for row in rows:
        row["source_page_ids"] = sorted(row.get("source_page_ids", []))
    rows.sort(
        key=lambda item: (
            int(item.get("review_count") or 0),
            int(item.get("location_count") or 0),
            str(item.get("brand_name", "")),
        ),
        reverse=True,
    )
    return rows


MATTRESS_SIZE_PATTERNS = (
    ("california king", "california_king"),
    ("twin xl", "twin_xl"),
    ("king", "king"),
    ("queen", "queen"),
    ("full", "full"),
    ("double", "full"),
    ("twin", "twin"),
)

MATTRESS_CONDITION_PATTERNS = (
    ("like new", "like_new"),
    ("new", "new"),
    ("excellent", "excellent"),
    ("good", "good"),
    ("fair", "fair"),
    ("used", "used"),
)


def _scout_row_source_url(scout_row: dict[str, Any]) -> str:
    raw_url_text = str(scout_row.get("raw_url_text", "")).strip()
    direct_url = _google_result_direct_url(raw_url_text)
    if direct_url:
        return direct_url
    followup_entrypoint = dict(scout_row.get("followup_entrypoint") or {})
    if str(followup_entrypoint.get("mode", "")).strip().lower() == "url":
        return str(followup_entrypoint.get("value", "")).strip()
    return ""


def _marketplace_source_label(source_url: str, domain_hint: str, route_source_type: str) -> str:
    domain = _normalize_domain(_domain_from_url(source_url) or str(domain_hint).strip())
    if domain:
        return domain
    return _normalize_space(route_source_type) or "unknown"


def _synthetic_listing_id(source: str, source_url: str, title: str) -> str:
    if source_url:
        parsed = urlparse(source_url)
        path_bits = [bit for bit in parsed.path.split("/") if bit]
        query_ids = parse_qs(parsed.query)
        for key in ("id", "item", "listing", "vehicle", "post"):
            values = query_ids.get(key, [])
            if values and str(values[0]).strip():
                return str(values[0]).strip()
        if path_bits:
            candidate = path_bits[-1].strip()
            if len(candidate) >= 4 and re.search(r"[A-Za-z0-9]", candidate):
                return candidate
    digest = sha1("{source}||{url}||{title}".format(source=source, url=source_url, title=title).encode("utf-8", "replace")).hexdigest()[:12]
    return digest


def _extract_price_from_text(text: str) -> Optional[float]:
    match = re.search(r"\$(\d[\d,]*(?:\.\d{2})?)", text)
    if not match:
        return None
    return _to_float(match.group(1))


def _extract_odometer_miles(text: str) -> Optional[int]:
    patterns = (
        r"(\d[\d,]{2,})\s*(?:miles|mile|mi)\b",
        r"(\d[\d,]{2,})\s*odometer",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _to_int(match.group(1))
    return None


def _extract_city_state(text: str) -> tuple[str, str]:
    match = re.search(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*),\s*([A-Z]{2})\b", text)
    if not match:
        return "", ""
    return _normalize_space(match.group(1)), match.group(2).upper()


def _extract_acres_from_text(text: str) -> Optional[float]:
    patterns = (
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:acre|acres)\b",
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:acre lot|ac lot)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _to_float(match.group(1))
    return None


def _infer_land_property_type(text: str) -> str:
    lower = _normalize_space(text).lower()
    if "industrial" in lower:
        return "industrial_land"
    if "commercial" in lower:
        return "commercial_land"
    if "farm" in lower or "ranch" in lower:
        return "agricultural_land"
    if "residential" in lower or "subdivision" in lower or "home site" in lower:
        return "residential_lot"
    return "land"


def _extract_model_year(title: str) -> Optional[int]:
    match = re.search(r"\b((?:19|20)\d{2})\b", title)
    if not match:
        return None
    return _to_int(match.group(1))


def _infer_vehicle_make_model(title: str) -> tuple[str, str]:
    clean = _normalize_space(title)
    if not clean:
        return "", ""
    year_match = re.search(r"\b(?:19|20)\d{2}\b", clean)
    remainder = clean[year_match.end() :].strip(" -,:;") if year_match else clean
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.\-]*", remainder)
    if not tokens:
        return "", ""
    make = tokens[0]
    model_tokens = [token for token in tokens[1:3] if not re.fullmatch(r"[A-Z]{2,3}", token)]
    return make, _normalize_space(" ".join(model_tokens))


def _infer_mattress_size(text: str) -> str:
    lower = _normalize_space(text).lower()
    for needle, label in MATTRESS_SIZE_PATTERNS:
        if needle in lower:
            return label
    return ""


def _infer_condition_label(text: str) -> str:
    lower = _normalize_space(text).lower()
    for needle, label in MATTRESS_CONDITION_PATTERNS:
        if needle in lower:
            return label
    return ""


def _build_vehicle_listing_rows(
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    zip_code = _zip_fragment(str(task_spec.get("user_prompt", "")))
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = _normalize_space(str(scout_row.get("candidate_name", "")) or str(scout_row.get("title", "")))
        if not title:
            continue
        combined_text = _normalize_space("{title} {snippet}".format(title=title, snippet=str(scout_row.get("snippet", ""))))
        source_url = _scout_row_source_url(scout_row)
        source = _marketplace_source_label(source_url, str(scout_row.get("domain_hint", "")), str(scout_row.get("route_source_type", "")))
        listing_id = _synthetic_listing_id(source, source_url, title)
        make, model = _infer_vehicle_make_model(title)
        model_year = _extract_model_year(title)
        price_usd = _extract_price_from_text(combined_text)
        odometer_miles = _extract_odometer_miles(combined_text)
        city, state = _extract_city_state(combined_text)
        key = (source, listing_id)
        row = rows_by_key.setdefault(
            key,
            {
                "source": source,
                "listing_source": source,
                "listing_id": listing_id,
                "listing_url": source_url,
                "vehicle_url": source_url,
                "title": title,
                "vehicle_title": title,
                "make": make,
                "model": model,
                "model_year": model_year,
                "price_usd": price_usd,
                "asking_price_usd": price_usd,
                "odometer_miles": odometer_miles,
                "mileage_miles": odometer_miles,
                "city": city,
                "location_city": city,
                "location_state": state,
                "zip_code": zip_code,
                "currency": "USD",
                "seller_type": "dealer" if "dealer" in str(scout_row.get("route_source_type", "")).lower() else "marketplace",
                "source_page_ids": [str(scout_row.get("page_id", ""))] if str(scout_row.get("page_id", "")).strip() else [],
                "source_url": source_url,
            },
        )
        if source_url and not row.get("listing_url"):
            row["listing_url"] = source_url
        if not row.get("make") and make:
            row["make"] = make
        if not row.get("model") and model:
            row["model"] = model
        if row.get("model_year") in (None, "") and model_year is not None:
            row["model_year"] = model_year
        if row.get("price_usd") in (None, "") and price_usd is not None:
            row["price_usd"] = price_usd
        if row.get("asking_price_usd") in (None, "") and price_usd is not None:
            row["asking_price_usd"] = price_usd
        if row.get("odometer_miles") in (None, "") and odometer_miles is not None:
            row["odometer_miles"] = odometer_miles
        if row.get("mileage_miles") in (None, "") and odometer_miles is not None:
            row["mileage_miles"] = odometer_miles
        if not row.get("city") and city:
            row["city"] = city
        if not row.get("location_city") and city:
            row["location_city"] = city
        if not row.get("location_state") and state:
            row["location_state"] = state
        page_id = str(scout_row.get("page_id", "")).strip()
        if page_id and page_id not in row["source_page_ids"]:
            row["source_page_ids"].append(page_id)
    rows = list(rows_by_key.values())
    for row in rows:
        row["source_page_ids"] = sorted(row.get("source_page_ids", []))
    rows.sort(
        key=lambda item: (
            item.get("price_usd") is None,
            float(item.get("price_usd") or 0.0),
            str(item.get("title", "")),
        )
    )
    return rows


def _build_mattress_listing_rows(
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    zip_code = _zip_fragment(str(task_spec.get("user_prompt", "")))
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = _normalize_space(str(scout_row.get("candidate_name", "")) or str(scout_row.get("title", "")))
        if not title:
            continue
        combined_text = _normalize_space("{title} {snippet}".format(title=title, snippet=str(scout_row.get("snippet", ""))))
        source_url = _scout_row_source_url(scout_row)
        source = _marketplace_source_label(source_url, str(scout_row.get("domain_hint", "")), str(scout_row.get("route_source_type", "")))
        listing_id = _synthetic_listing_id(source, source_url, title)
        price_usd = _extract_price_from_text(combined_text)
        mattress_size = _infer_mattress_size(combined_text)
        condition = _infer_condition_label(combined_text)
        city, _state = _extract_city_state(combined_text)
        key = (source, listing_id)
        row = rows_by_key.setdefault(
            key,
            {
                "source": source,
                "listing_id": listing_id,
                "listing_url": source_url,
                "title": title,
                "price_usd": price_usd,
                "condition": condition,
                "mattress_size": mattress_size,
                "city": city,
                "zip_code": zip_code,
                "pickup_available": "pickup" in combined_text.lower(),
                "delivery_available": "delivery" in combined_text.lower(),
                "source_page_ids": [str(scout_row.get("page_id", ""))] if str(scout_row.get("page_id", "")).strip() else [],
                "source_url": source_url,
            },
        )
        if source_url and not row.get("listing_url"):
            row["listing_url"] = source_url
        if row.get("price_usd") in (None, "") and price_usd is not None:
            row["price_usd"] = price_usd
        if not row.get("condition") and condition:
            row["condition"] = condition
        if not row.get("mattress_size") and mattress_size:
            row["mattress_size"] = mattress_size
        if not row.get("city") and city:
            row["city"] = city
        page_id = str(scout_row.get("page_id", "")).strip()
        if page_id and page_id not in row["source_page_ids"]:
            row["source_page_ids"].append(page_id)
    rows = list(rows_by_key.values())
    for row in rows:
        row["source_page_ids"] = sorted(row.get("source_page_ids", []))
    rows.sort(
        key=lambda item: (
            item.get("price_usd") is None,
            float(item.get("price_usd") or 0.0),
            str(item.get("title", "")),
        )
    )
    return rows


def _build_land_listing_rows(
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    zip_code = _zip_fragment(str(task_spec.get("user_prompt", "")))
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = _normalize_space(str(scout_row.get("candidate_name", "")) or str(scout_row.get("title", "")))
        if not title:
            continue
        combined_text = _normalize_space("{title} {snippet}".format(title=title, snippet=str(scout_row.get("snippet", ""))))
        source_url = _scout_row_source_url(scout_row)
        source = _marketplace_source_label(source_url, str(scout_row.get("domain_hint", "")), str(scout_row.get("route_source_type", "")))
        listing_id = _synthetic_listing_id(source, source_url, title)
        price_usd = _extract_price_from_text(combined_text)
        lot_size_acres = _extract_acres_from_text(combined_text)
        city, state = _extract_city_state(combined_text)
        property_type = _infer_land_property_type(combined_text)
        key = (source, listing_id)
        row = rows_by_key.setdefault(
            key,
            {
                "source": source,
                "listing_source": source,
                "listing_id": listing_id,
                "listing_url": source_url,
                "title": title,
                "price_usd": price_usd,
                "lot_size_acres": lot_size_acres,
                "city": city,
                "state": state,
                "zip_code": zip_code,
                "property_type": property_type,
                "source_page_ids": [str(scout_row.get("page_id", ""))] if str(scout_row.get("page_id", "")).strip() else [],
                "source_url": source_url,
                "currency": "USD" if price_usd is not None else "",
            },
        )
        if source_url and not row.get("listing_url"):
            row["listing_url"] = source_url
        if row.get("price_usd") in (None, "") and price_usd is not None:
            row["price_usd"] = price_usd
        if row.get("lot_size_acres") in (None, "") and lot_size_acres is not None:
            row["lot_size_acres"] = lot_size_acres
        if not row.get("city") and city:
            row["city"] = city
        if not row.get("state") and state:
            row["state"] = state
        if not row.get("property_type") and property_type:
            row["property_type"] = property_type
        page_id = str(scout_row.get("page_id", "")).strip()
        if page_id and page_id not in row["source_page_ids"]:
            row["source_page_ids"].append(page_id)
    rows = list(rows_by_key.values())
    for row in rows:
        row["source_page_ids"] = sorted(row.get("source_page_ids", []))
    rows.sort(
        key=lambda item: (
            item.get("price_usd") is None,
            float(item.get("price_usd") or 0.0),
            str(item.get("title", "")),
        )
    )
    return rows


RENTAL_RESULT_NOISE = {
    "videos",
    "video",
    "virtual tours",
    "virtual tour",
    "specials",
    "pets allowed fitness center pool dishwasher grill hardwood floors individual locking bedrooms",
    "email",
    "new",
    "sort",
    "save search",
    "all filters",
    "total monthly price",
}


def _looks_like_rental_property_title(line: str) -> bool:
    clean = _normalize_space(line)
    if not clean or len(clean) < 3 or len(clean) > 120:
        return False
    lower = clean.lower()
    if lower in RENTAL_RESULT_NOISE:
        return False
    if any(token in lower for token in ("apartments for rent", "rentals available", "searching for student")):
        return False
    if re.search(r"\$\d", clean):
        return False
    if re.search(r"\b(?:studio|\d+\s*bed|\d+\s*bath|\d+\s*sq\.?\s*ft)\b", lower):
        return False
    if re.search(r"\b\d{3,5}\b", clean) and "," in clean:
        return False
    return bool(re.search(r"[A-Za-z]", clean))


def _extract_bedroom_value(text: str) -> Optional[float]:
    clean = _normalize_space(text)
    if re.search(r"\bstudio\b", clean, re.I):
        return 0.0
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:bed|beds|br)\b", clean, re.I)
    if not match:
        return None
    return _to_float(match.group(1))


def _extract_bathroom_value(text: str) -> Optional[float]:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:bath|baths|ba)\b", _normalize_space(text), re.I)
    if not match:
        return None
    return _to_float(match.group(1))


def _extract_square_feet_value(text: str) -> Optional[int]:
    patterns = (
        r"\b(\d[\d,]{2,})\s*(?:sq\.?\s*ft|square feet)\b",
        r"\b(\d[\d,]{2,})\s*sf\b",
    )
    for pattern in patterns:
        match = re.search(pattern, _normalize_space(text), re.I)
        if match:
            return _to_int(match.group(1))
    return None


def _iter_rental_rows_from_results_page(page: dict[str, Any], raw_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lines = _nonempty_lines(raw_text)
    page_url = str(page.get("final_url") or page.get("requested_url") or "")
    page_domain = _domain_from_url(page_url)
    page_title = str(page.get("title", ""))
    page_id = str(page.get("page_id", "")).strip()
    index = 0
    while index < len(lines) - 1:
        title = _normalize_space(lines[index])
        address_line = _normalize_space(lines[index + 1])
        if not (_looks_like_rental_property_title(title) and re.search(r",\s*[A-Z]{2}\s+\d{5}\b", address_line)):
            index += 1
            continue
        block_lines: list[str] = []
        lookahead = index + 2
        while lookahead < len(lines):
            if (
                lookahead + 1 < len(lines)
                and _looks_like_rental_property_title(lines[lookahead])
                and re.search(r",\s*[A-Z]{2}\s+\d{5}\b", _normalize_space(lines[lookahead + 1]))
            ):
                break
            block_lines.append(_normalize_space(lines[lookahead]))
            lookahead += 1
        block_text = " ".join(block_lines)
        price_values = [_extract_price_from_text(line) for line in block_lines]
        prices = [value for value in price_values if value is not None]
        if not prices:
            index += 1
            continue
        city, state = _extract_city_state(address_line)
        zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", address_line)
        listing_identity = "{title}|{address}".format(title=title, address=address_line)
        listing_id = _synthetic_listing_id(page_domain or "rental", "", listing_identity)
        row = {
            "source": _normalize_domain(page_domain or "rental"),
            "listing_id": listing_id,
            "listing_url": "{url}#listing={slug}".format(
                url=page_url,
                slug=sha1(listing_identity.encode("utf-8", "replace")).hexdigest()[:10],
            ),
            "title": title,
            "rent_usd": min(prices),
            "bedrooms": next((value for value in (_extract_bedroom_value(line) for line in block_lines) if value is not None), None),
            "bathrooms": next((value for value in (_extract_bathroom_value(line) for line in block_lines) if value is not None), None),
            "square_feet": next((value for value in (_extract_square_feet_value(line) for line in block_lines) if value is not None), None),
            "neighborhood": "",
            "city": city,
            "state": state,
            "zip_code": zip_match.group(1) if zip_match else "",
            "property_type": "apartment",
            "source_page_ids": [page_id] if page_id else [],
            "source_url": page_url,
            "source_title": page_title,
        }
        if any(row.get(key) not in (None, "", []) for key in ("rent_usd", "bedrooms", "bathrooms", "square_feet")):
            rows.append(row)
        index = lookahead
    return rows


def _build_rental_listing_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        raw_text = _read_page_text(capsule_dir, page)
        if not raw_text.strip():
            continue
        final_url = str(page.get("final_url") or page.get("requested_url") or "")
        if _is_search_engine_url(final_url):
            continue
        page_title = str(page.get("title", "")).lower()
        text_lower = raw_text.lower()
        if _capture_looks_blocked(raw_text, str(page.get("title", "")), str(page.get("final_url") or page.get("requested_url") or "")):
            continue
        if not any(token in text_lower for token in ("apartments for rent", "rentals available", "1 bed", "2 beds", "studio")):
            continue
        if "phoenix, az" not in text_lower and "for rent" not in page_title and "apartments.com" not in str(page.get("final_url", "")):
            continue
        for row in _iter_rental_rows_from_results_page(page, raw_text):
            key = (str(row.get("source", "")), str(row.get("listing_id", "")))
            existing = rows_by_key.get(key)
            if existing is None:
                rows_by_key[key] = row
                continue
            for field in ("rent_usd", "bedrooms", "bathrooms", "square_feet", "city", "state", "zip_code"):
                if existing.get(field) in (None, "", []) and row.get(field) not in (None, "", []):
                    existing[field] = row[field]
            existing_ids = list(existing.get("source_page_ids", []))
            for source_page_id in row.get("source_page_ids", []):
                if source_page_id not in existing_ids:
                    existing_ids.append(source_page_id)
            existing["source_page_ids"] = existing_ids
    rows = list(rows_by_key.values())
    for row in rows:
        row["source_page_ids"] = sorted(row.get("source_page_ids", []))
    rows.sort(
        key=lambda item: (
            item.get("rent_usd") is None,
            float(item.get("rent_usd") or 0.0),
            str(item.get("title", "")),
        )
    )
    return rows


def _coworking_space_name_from_title(title: str, source_url: str) -> str:
    clean = _normalize_space(title)
    if not clean:
        return ""
    lower = clean.lower()
    generic_location_pattern = re.compile(
        r"\b(find a coworking space|coworking space(?:s)?(?: near| in)?|shared offices?|office space in|monthly desks|memberships)\b",
        re.I,
    )
    if re.search(
        r"\b(top \d+|best\b|average price|choose from|find a coworking space|coworking spaces? in|within \d+ miles|monthly coworking costs?)\b",
        lower,
    ):
        return ""
    for separator in (":", "|", "-", "—"):
        if separator in clean:
            candidate = _normalize_space(clean.split(separator, 1)[0])
            candidate_lower = candidate.lower()
            if candidate and not re.search(
                r"\b(top \d+|best\b|average price|choose from|coworking spaces? for)\b",
                candidate_lower,
            ):
                return candidate
    if " " in clean and not re.search(
        r"\b(top \d+|best\b|average price|choose from|coworking spaces? for|coworking spaces? in)\b",
        lower,
    ) and not generic_location_pattern.search(lower):
        return clean
    domain = _normalize_domain(_domain_from_url(source_url)).removeprefix("www.")
    if not domain:
        return ""
    domain_core = domain.split(".", 1)[0].replace("-", " ")
    for token in ("coworking", "desk", "space", "spaces", "office", "factory", "works", "work"):
        domain_core = re.sub(r"(?<!\s)({token})".format(token=token), r" \1", domain_core, flags=re.I)
    candidate = _normalize_space(domain_core)
    if any(token in candidate.lower() for token in ("deskpass", "liquidspace", "coworkingcafe", "coworkingmag", "drop desk", "hubble")):
        return ""
    return candidate.title()


def _extract_monthly_price_usd(text: str) -> Optional[float]:
    clean = _normalize_space(text)
    match = re.search(r"\$([\d,]+(?:\.\d+)?)\s*(?:/|per)?\s*(?:month|mo\.?)\b", clean, re.I)
    if match:
        return _to_float(match.group(1))
    match = re.search(r"\b(?:from|starting(?: cost)?|starts at)\s+\$([\d,]+(?:\.\d+)?)\b", clean, re.I)
    if match and any(token in clean.lower() for token in ("desk", "cowork", "workspace", "membership", "office", "space")):
        return _to_float(match.group(1))
    if "month" in clean.lower():
        value = _extract_price_from_text(clean)
        if value is not None:
            return value
    return None


def _looks_like_generic_coworking_title(text: str) -> bool:
    lower = _normalize_space(text).lower()
    if not lower:
        return True
    if re.search(
        r"\b(top \d+|best coworking spaces?|coworking spaces? in|find a coworking space|monthly desks|serviced offices & coworking spaces|shared offices|office space in|downtown .* coworking space|austin coworking space)\b",
        lower,
    ):
        return True
    return False


def _looks_like_coworking_plan_label(name: str) -> bool:
    lower = _normalize_space(name).lower()
    if not lower:
        return True
    return bool(
        re.search(
            r"\b(network membership|memberships?|labs? desk|hot desk|flex[- ]desk|open desk|private office|select plan|best value|monthly desks?)\b",
            lower,
        )
        or lower.startswith(("day.", "monthly "))
    )


def _coworking_row_key(space_name: str, source_url: str) -> tuple[str, str]:
    canonical = re.sub(r"[^a-z0-9]+", "", space_name.lower())
    canonical = canonical.removeprefix("the")
    return canonical or space_name.lower(), source_url


def _synthetic_coworking_locator(source_url: str, space_name: str) -> str:
    if source_url:
        return "{url}#space={slug}".format(
            url=source_url,
            slug=sha1(space_name.encode("utf-8", "replace")).hexdigest()[:10],
        )
    return "coworking://{slug}".format(slug=sha1(space_name.encode("utf-8", "replace")).hexdigest()[:10])


def _coworking_result_blocks_from_google_page(lines: list[str]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if "https://" not in line:
            continue
        source_url = _google_result_direct_url(line)
        if not source_url:
            continue
        provider = _normalize_space(lines[index - 1]) if index >= 1 else ""
        title = _normalize_space(lines[index - 2]) if index >= 2 else provider
        snippet_lines: list[str] = []
        for candidate in lines[index + 1 :]:
            if "https://" in candidate:
                break
            clean = _normalize_space(candidate)
            if not clean:
                continue
            snippet_lines.append(clean)
            if len(snippet_lines) >= 3:
                break
        blocks.append(
            {
                "title": title,
                "provider": provider,
                "source_url": source_url,
                "snippet": _normalize_space(" ".join(snippet_lines)),
            }
        )
    return blocks


def _iter_coworking_rows_from_google_page(capsule_dir: Path, page: dict[str, Any]) -> list[dict[str, Any]]:
    if not _looks_like_google_results_page(page):
        return []
    raw_text = _read_page_text(capsule_dir, page)
    lines = _nonempty_lines(raw_text)
    if not lines:
        return []
    rows: list[dict[str, Any]] = []
    page_id = str(page.get("page_id", "")).strip()
    search_url = str(page.get("final_url", "") or page.get("requested_url", "")).strip()

    for block in _coworking_result_blocks_from_google_page(lines):
        source_url = str(block.get("source_url", "")).strip()
        title = str(block.get("title", "")).strip()
        provider = str(block.get("provider", "")).strip()
        snippet = str(block.get("snippet", "")).strip()
        if not source_url or not snippet:
            continue
        if any(token in snippet.lower() for token in ("latest posts", "resources page", "page not found", "no results found")):
            continue
        title_source = provider if _looks_like_generic_coworking_title(title) and provider else title
        space_name = _coworking_space_name_from_title(title_source, source_url)
        if not space_name or _looks_like_coworking_plan_label(space_name):
            continue
        monthly_price = _extract_monthly_price_usd("{title} {snippet}".format(title=title, snippet=snippet))
        rating_value, review_count, _price_tier = _parse_rating_review_price("{title} {snippet}".format(title=title, snippet=snippet))
        city, state = _extract_city_state("{title} {snippet}".format(title=title, snippet=snippet))
        if all(value in (None, "", []) for value in (monthly_price, rating_value, review_count)):
            continue
        rows.append(
            {
                "space_name": space_name,
                "source_url": source_url or _synthetic_coworking_locator(search_url, space_name),
                "source_domain": _domain_from_url(source_url),
                "monthly_price_usd": monthly_price,
                "rating_value": rating_value,
                "review_count": review_count,
                "city": city,
                "state": state,
                "neighborhood": "",
                "source_page_ids": [page_id] if page_id else [],
            }
        )

    summary_text = _normalize_space(" ".join(lines[:180]))
    summary_patterns = (
        re.compile(
            r"([A-Z][A-Za-z0-9&'’.\- ]{2,70}?)\s*:\s*(?:from|starts?(?: around)?|starting(?: at| cost)?|dedicated desks? from)?\s*\$([\d,]+(?:\.\d+)?)\s*(?:/|per)?\s*(?:month|mo\.?)\b",
            re.I,
        ),
        re.compile(
            r"([A-Z][A-Za-z0-9&'’.\- ]{2,70}?)\s*\(from\s*\$([\d,]+(?:\.\d+)?)\s*(?:/|per)?\s*(?:month|mo\.?)\b",
            re.I,
        ),
    )
    for pattern in summary_patterns:
        for match in pattern.finditer(summary_text):
            space_name = _coworking_space_name_from_title(match.group(1), search_url)
            if not space_name or _looks_like_coworking_plan_label(space_name):
                continue
            rows.append(
                {
                    "space_name": space_name,
                    "source_url": _synthetic_coworking_locator(search_url, space_name),
                    "source_domain": _domain_from_url(search_url),
                    "monthly_price_usd": _to_float(match.group(2)),
                    "rating_value": None,
                    "review_count": None,
                    "city": "",
                    "state": "",
                    "neighborhood": "",
                    "source_page_ids": [page_id] if page_id else [],
                }
            )
    return rows


def _build_coworking_space_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    area = _extract_local_area(str(task_spec.get("user_prompt", ""))).lower()
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = _normalize_space(str(scout_row.get("candidate_name", "")) or str(scout_row.get("title", "")))
        snippet = _normalize_space(str(scout_row.get("snippet", "")))
        source_url = _scout_row_source_url(scout_row)
        domain_hint = _normalize_domain(str(scout_row.get("domain_hint", "")).strip())
        lower_text = _normalize_space("{title} {snippet}".format(title=title, snippet=snippet)).lower()
        if any(token in lower_text for token in ("page not found", "no results found", "resources", "latest posts", "all posts", "blog")):
            continue
        if re.search(r"\b(top \d+|best coworking spaces?|average price|choose from \d+|within \d+ miles|coworking spaces? in)\b", lower_text):
            continue
        if area and area not in lower_text and "denver" not in lower_text and "austin" not in lower_text:
            continue
        space_name = _coworking_space_name_from_title(title, source_url)
        if not space_name or _looks_like_coworking_plan_label(space_name):
            continue
        monthly_price = _extract_monthly_price_usd(snippet) or _extract_monthly_price_usd(title)
        rating_value, review_count, _price_tier = _parse_rating_review_price("{title} {snippet}".format(title=title, snippet=snippet))
        city, state = _extract_city_state("{title} {snippet}".format(title=title, snippet=snippet))
        if not source_url and not domain_hint:
            continue
        if not source_url and rating_value is None and review_count is None:
            continue
        key = _coworking_row_key(space_name, source_url or str(scout_row.get("domain_hint", "")))
        row = rows_by_key.setdefault(
            key,
            {
                "space_name": space_name,
                "source_url": source_url,
                "source_domain": _domain_from_url(source_url) or domain_hint,
                "monthly_price_usd": monthly_price,
                "rating_value": rating_value,
                "review_count": review_count,
                "city": city,
                "state": state,
                "neighborhood": "",
                "source_page_ids": [str(scout_row.get("page_id", ""))] if str(scout_row.get("page_id", "")).strip() else [],
            },
        )
        for field, value in (
            ("monthly_price_usd", monthly_price),
            ("rating_value", rating_value),
            ("review_count", review_count),
            ("city", city),
            ("state", state),
        ):
            if row.get(field) in (None, "", []) and value not in (None, "", []):
                row[field] = value
        page_id = str(scout_row.get("page_id", "")).strip()
        if page_id and page_id not in row["source_page_ids"]:
            row["source_page_ids"].append(page_id)
    for page in manifest.get("pages", []):
        for page_row in _iter_coworking_rows_from_google_page(capsule_dir, page):
            key = _coworking_row_key(str(page_row.get("space_name", "")), str(page_row.get("source_url", "")))
            row = rows_by_key.setdefault(key, dict(page_row))
            for field in ("monthly_price_usd", "rating_value", "review_count", "city", "state"):
                if row.get(field) in (None, "", []) and page_row.get(field) not in (None, "", []):
                    row[field] = page_row[field]
            existing_ids = list(row.get("source_page_ids", []))
            for page_id in page_row.get("source_page_ids", []):
                if page_id not in existing_ids:
                    existing_ids.append(page_id)
            row["source_page_ids"] = existing_ids
    rows = list(rows_by_key.values())
    for row in rows:
        row["source_page_ids"] = sorted(row.get("source_page_ids", []))
    rows = [
        row
        for row in rows
        if any(row.get(field) not in (None, "", []) for field in ("monthly_price_usd", "rating_value", "review_count"))
    ]
    rows.sort(
        key=lambda item: (
            item.get("monthly_price_usd") is None,
            float(item.get("monthly_price_usd") or 0.0),
            str(item.get("space_name", "")),
        )
    )
    return rows


def _to_percent(value: str) -> Optional[float]:
    clean = str(value).replace("%", "").strip()
    if not clean:
        return None
    try:
        return _to_float(clean)
    except ValueError:
        return None


def _iter_home_sale_rows_from_market_trends(
    page: dict[str, Any],
    lines: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        city, state = _extract_city_state(line)
        if not city or not state:
            continue
        price_value: Optional[float] = None
        for candidate in lines[index + 1 : index + 4]:
            price_value = _extract_price_from_text(candidate)
            if price_value is not None:
                break
        if price_value is None:
            continue
        metro = "{city}, {state}".format(city=city, state=state)
        rows.append(
            {
                "metro": metro,
                "city": city,
                "state": state,
                "signal_type": "median_market_price",
                "price_signal_usd": price_value,
                "market_temperature": "",
                "source_url": str(page.get("final_url") or page.get("requested_url") or ""),
                "source_domain": _domain_from_url(str(page.get("final_url") or page.get("requested_url") or "")),
                "source_title": str(page.get("title", "")),
                "source_page_ids": [str(page.get("page_id", ""))] if str(page.get("page_id", "")).strip() else [],
            }
        )
    return rows


def _iter_home_sale_rows_from_market_article(
    page: dict[str, Any],
    lines: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_url = str(page.get("final_url") or page.get("requested_url") or "")
    page_domain = _domain_from_url(page_url)
    page_title = str(page.get("title", ""))
    page_id = str(page.get("page_id", "")).strip()
    source_page_ids = [page_id] if page_id else []

    mode = ""
    for line in lines:
        clean = _normalize_space(line)
        if not clean:
            continue
        lower = clean.lower()
        if "strong sellers markets are" in lower:
            mode = "seller"
            continue
        if "buyers are calling the shots in" in lower:
            mode = "buyer"
            continue
        if mode in {"seller", "buyer"}:
            city, state = _extract_city_state(clean)
            if city and state:
                metro = "{city}, {state}".format(city=city, state=state)
                rows.append(
                    {
                        "metro": metro,
                        "city": city,
                        "state": state,
                        "signal_type": "market_heat",
                        "market_temperature": "seller_market" if mode == "seller" else "buyer_market",
                        "source_url": page_url,
                        "source_domain": page_domain,
                        "source_title": page_title,
                        "source_page_ids": list(source_page_ids),
                    }
                )
                continue
            if clean.endswith(":") or clean.startswith("As for home values"):
                mode = ""
        row_match = re.search(
            r"^([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\s+\$([\d,]+)\s+(-?\d+(?:\.\d+)?)%$",
            clean,
        )
        if row_match:
            metro = _normalize_space(row_match.group(1))
            city, state = _extract_city_state(metro)
            rows.append(
                {
                    "metro": metro,
                    "city": city,
                    "state": state,
                    "signal_type": "home_value_mom_change",
                    "price_signal_usd": _to_float(row_match.group(2)),
                    "mom_change_pct": _to_percent(row_match.group(3)),
                    "market_temperature": "",
                    "source_url": page_url,
                    "source_domain": page_domain,
                    "source_title": page_title,
                    "source_page_ids": list(source_page_ids),
                }
            )
    return rows


def build_home_sale_signal_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        raw_text = _read_page_text(capsule_dir, page)
        if not raw_text.strip():
            continue
        lines = _nonempty_lines(raw_text)
        page_rows: list[dict[str, Any]] = []
        page_title = str(page.get("title", "")).lower()
        text_lower = raw_text.lower()
        if "rental market trends" in page_title or "median rent in select u.s. markets" in text_lower:
            page_rows.extend(_iter_home_sale_rows_from_market_trends(page, lines))
        if any(
            token in text_lower
            for token in (
                "the strong sellers markets are",
                "buyers are calling the shots in",
                "top 10 metros* with the largest month-over-month increase",
            )
        ):
            page_rows.extend(_iter_home_sale_rows_from_market_article(page, lines))
        for row in page_rows:
            metro = str(row.get("metro", "")).strip()
            source_url = str(row.get("source_url", "")).strip()
            signal_type = str(row.get("signal_type", "")).strip()
            if not metro or not source_url or not signal_type:
                continue
            key = (metro, source_url, signal_type)
            existing = rows_by_key.get(key)
            if existing is None:
                rows_by_key[key] = row
                continue
            for field in ("price_signal_usd", "mom_change_pct", "yoy_change_pct", "price_cut_share_pct", "days_on_market", "market_temperature"):
                if existing.get(field) in (None, "", []) and row.get(field) not in (None, "", []):
                    existing[field] = row[field]
            existing_ids = list(existing.get("source_page_ids", []))
            for page_id in row.get("source_page_ids", []):
                if page_id not in existing_ids:
                    existing_ids.append(page_id)
            existing["source_page_ids"] = existing_ids
    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            item.get("mom_change_pct") is None,
            float(item.get("mom_change_pct") or 0.0),
            str(item.get("metro", "")),
        )
    )
    return rows


def _looks_like_neighborhood_ranking_heading(text: str) -> bool:
    clean = _normalize_space(text)
    if not clean:
        return False
    if re.search(r"\b(?:median|average|recent|price|home|value|sale|rent)\b", clean, re.I):
        return False
    if re.search(r"\b\d{5}\b", clean):
        return False
    return bool(re.fullmatch(r"[A-Z][A-Za-z0-9&'’.\- ]{2,60}", clean))


def _extract_neighborhood_price_rows_from_page(page: dict[str, Any], raw_text: str) -> list[dict[str, Any]]:
    final_url = str(page.get("final_url") or page.get("requested_url") or "").strip()
    if _is_search_engine_url(final_url):
        return []
    title = str(page.get("title", "")).strip()
    if _capture_looks_blocked(raw_text, title, final_url):
        return []
    domain = _normalize_domain(_domain_from_url(final_url))
    if any(token in domain for token in ("facebook.com", "instagram.com", "x.com", "twitter.com")):
        return []
    combined = _normalize_space("{title} {text}".format(title=title, text=raw_text[:5000])).lower()
    if not any(
        token in combined
        for token in (
            "most expensive neighborhoods",
            "wealthiest neighborhoods",
            "richest neighborhoods",
            "median home price",
            "median sale price",
            "home values",
        )
    ):
        return []
    if any(token in combined for token in ("apartment rentals", "for rent", "zip code", "zip codes")):
        return []

    lines = _nonempty_lines(raw_text)
    page_id = str(page.get("page_id", "")).strip()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines):
        heading_match = re.match(r"^(\d{1,2})\.\s+(.+)$", _normalize_space(line))
        if not heading_match:
            continue
        rank = _to_int(heading_match.group(1))
        neighborhood_name = _normalize_space(heading_match.group(2))
        if not _looks_like_neighborhood_ranking_heading(neighborhood_name):
            continue
        metric_label = ""
        reported_price_value: Optional[float] = None
        for candidate in lines[index + 1 : index + 7]:
            clean_candidate = _normalize_space(candidate)
            if not clean_candidate:
                continue
            if re.match(r"^\d{1,2}\.\s+", clean_candidate):
                break
            if re.search(r"\$\s*\d[\d,]*(?:\.\d+)?\s*(?:[KMB]\b|million\b|billion\b)", clean_candidate, re.I):
                value = _extract_first_money_amount(clean_candidate)
            else:
                value = _extract_price_from_text(clean_candidate)
            if value is None:
                continue
            reported_price_value = value
            metric_label = clean_candidate.split(":", 1)[0].strip() if ":" in clean_candidate else "reported home price"
            break
        if reported_price_value is None:
            for candidate in lines[index + 1 : index + 7]:
                clean_candidate = _normalize_space(candidate)
                if not clean_candidate:
                    continue
                if re.match(r"^\d{1,2}\.\s+", clean_candidate):
                    break
                lower_candidate = clean_candidate.lower()
                if not any(
                    token in lower_candidate
                    for token in (
                        "home price",
                        "home prices",
                        "property price",
                        "property prices",
                        "listing price",
                        "listing prices",
                        "real estate",
                    )
                ):
                    continue
                value = _extract_first_money_amount(clean_candidate)
                if value is None:
                    continue
                reported_price_value = value
                metric_label = "reported luxury price signal"
                break
        if reported_price_value is None:
            continue
        key = (neighborhood_name.lower(), final_url)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "neighborhood_name": neighborhood_name,
                "rank": rank,
                "reported_price_value": reported_price_value,
                "metric_label": metric_label or "reported home price",
                "city": "Chicago" if "chicago" in combined else "",
                "state": "IL" if "chicago" in combined else "",
                "source_url": final_url,
                "source_domain": domain,
                "source_title": title,
                "source_page_ids": [page_id] if page_id else [],
            }
        )
    return rows


def build_neighborhood_price_ranking_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        raw_text = _read_page_text(capsule_dir, page)
        if not raw_text.strip():
            continue
        for row in _extract_neighborhood_price_rows_from_page(page, raw_text):
            key = (str(row.get("neighborhood_name", "")).strip().lower(), str(row.get("source_url", "")).strip())
            if not key[0] or not key[1]:
                continue
            rows_by_key[key] = row
    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            item.get("rank") is None,
            int(item.get("rank") or 9999),
            -float(item.get("reported_price_value") or 0.0),
            str(item.get("neighborhood_name", "")),
        )
    )
    return rows


def _infer_home_sale_signal_type(text: str) -> str:
    lower = _normalize_space(text).lower()
    if "year-over-year" in lower or "yoy" in lower:
        return "home_value_yoy_change"
    if "month-over-month" in lower or "mom" in lower:
        return "home_value_mom_change"
    if "price" in lower or "home value" in lower or "market" in lower:
        return "home_value_signal"
    return "market_heat"


def _infer_home_sale_metro(text: str, task_spec: dict[str, Any]) -> str:
    city, state = _extract_city_state(text)
    if city:
        return "{city}, {state}".format(city=city, state=state) if state else city
    prompt = str(task_spec.get("user_prompt", ""))
    for token in ("phoenix", "atlanta", "miami", "dallas", "austin", "boise"):
        if token in text.lower() or token in prompt.lower():
            return token.title()
    area = _extract_local_area(prompt)
    area_clean = _normalize_space(area).title()
    if area_clean and area_clean.lower() != "target area":
        return area_clean
    return ""


def _build_home_sale_signal_rows_from_scout(
    task_spec: dict[str, Any],
    scout_index: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for scout_row in scout_index.get("rows", []):
        if not isinstance(scout_row, dict):
            continue
        title = _normalize_space(str(scout_row.get("title", "")))
        snippet = _normalize_space(str(scout_row.get("snippet", "")))
        combined = _normalize_space("{title} {snippet}".format(title=title, snippet=snippet))
        if not combined:
            continue
        lower = combined.lower()
        if any(token in lower for token in ("page not found", "facebook", "reddit", "youtube", "scribd", "hottest housing")):
            continue
        price_signal_usd = _extract_price_from_text(combined)
        pct_matches = re.findall(r"(-?\d+(?:\.\d+)?)%", combined)
        if not pct_matches and price_signal_usd is None:
            continue
        metro = _infer_home_sale_metro(combined, task_spec)
        if not metro:
            continue
        signal_type = _infer_home_sale_signal_type(combined)
        source_url = _scout_row_source_url(scout_row)
        source_domain = _normalize_domain(_domain_from_url(source_url) or str(scout_row.get("domain_hint", "")))
        if not source_domain:
            continue
        pct_value = None
        if pct_matches:
            pct_value = _to_percent(pct_matches[0])
            if any(token in lower for token in ("drop", "decline", "cooled", "falling", "fell", "cooling")) and pct_value is not None:
                pct_value = -abs(float(pct_value))
        page_id = str(scout_row.get("page_id", "")).strip()
        row = {
            "metro": metro,
            "city": _extract_city_state(metro)[0] or metro,
            "state": _extract_city_state(metro)[1],
            "signal_type": signal_type,
            "price_signal_usd": price_signal_usd,
            "mom_change_pct": pct_value if "mom" in signal_type else None,
            "yoy_change_pct": pct_value if "yoy" in signal_type or ("year-over-year" in lower) else None,
            "price_cut_share_pct": pct_value if "price cut" in lower else None,
            "market_temperature": "cooling" if pct_value is not None and pct_value < 0 else "",
            "source_url": source_url or "https://{domain}".format(domain=source_domain),
            "source_domain": source_domain,
            "source_title": title,
            "source_page_ids": [page_id] if page_id else [],
        }
        key = (row["metro"], row["source_url"], row["signal_type"])
        rows_by_key.setdefault(key, row)
    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            item.get("yoy_change_pct") is None and item.get("mom_change_pct") is None,
            float(item.get("yoy_change_pct") or item.get("mom_change_pct") or 0.0),
            str(item.get("metro", "")),
        )
    )
    return rows


PRODUCT_TITLE_EXCLUDE_PREFIXES = {
    "sponsored",
    "results",
    "price, product page",
    "free delivery",
    "or fastest delivery",
    "see options",
    "add to cart",
    "limited time deal",
    "overall pick",
    "best seller",
    "recently bought and rated",
    "list price:",
    "list:",
    "exclusive prime price",
    "lowest price in 30 days",
    "more buying choices",
    "sort by:",
    "sort by",
    "check each product page",
    "skip to",
    "main content",
    "top reviewed for",
    "see all",
    "rebates available",
    "buy more, save more",
    "bought in last month",
}

PRODUCT_LISTING_META_PREFIXES = {
    "add to cart",
    "sponsored",
    "highly rated",
    "bestseller",
    "best seller",
    "carb",
    "sale",
    "see all",
    "rebates available",
    "buy more, save more",
    "bought in last month",
}

PRODUCT_FEATURE_LINE_PREFIXES = (
    "covers ",
    "ideal for ",
    "excellent for ",
    "top quality",
    "patented ",
    "battery operated",
    "ozone free",
    "lightweight ",
    "removes ",
    "reduces ",
    "totally ",
    "completely ",
    "1 year warranty",
)

PRODUCT_BRAND_GENERIC_PREFIXES = {
    "baby",
    "babies",
    "by",
    "infant",
    "infants",
    "part",
    "premium",
    "positively",
    "tip",
    "newborn",
    "newborns",
    "toddler",
    "toddlers",
    "montessori",
    "educational",
    "learning",
    "sensory",
    "musical",
}

PRODUCT_JUNK_NAME_PATTERNS = (
    r"^by\s+.+\|\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"^part of:",
    r"^see all$",
    r"^rebates available$",
    r"^buy more,\s*save more$",
    r"^\d+(?:\.\d+)?k?\+\s+bought in last month$",
    r"^save\s+\d+%$",
    r"^save\s+\$\d[\d.,]*$",
    r"positively reviewed for",
    r"top reviewed for",
    r"\btip card\b",
    r"\bwallet\b",
    r"\bvoltage transformer\b",
    r"\bpublishing\b",
)

SUPPORT_OBJECT_NAMES = {
    "capture_targets",
    "entities",
    "gather_qa",
    "gather_qa_review",
    "gather_targets",
    "pages",
    "scout_index",
    "source_index",
}


def _retailer_label_from_url(url: str) -> str:
    domain = _domain_from_url(url)
    if "amazon." in domain:
        return "Amazon"
    if "target." in domain:
        return "Target"
    if "walmart." in domain:
        return "Walmart"
    if domain:
        return domain
    return "Unknown"


def _parse_quantity_text(text: str) -> Optional[int]:
    clean = text.strip().replace(",", "").rstrip("+")
    if not clean:
        return None
    multiplier = 1
    if clean.lower().endswith("k"):
        multiplier = 1000
        clean = clean[:-1]
    elif clean.lower().endswith("m"):
        multiplier = 1_000_000
        clean = clean[:-1]
    try:
        return int(float(clean) * multiplier)
    except ValueError:
        return None


def _infer_brand_from_product_name(name: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.\-]*", name)
    if not tokens:
        return ""
    first = tokens[0].strip(".,:;")
    if not first:
        return ""
    if first.lower() in PRODUCT_BRAND_GENERIC_PREFIXES:
        return ""
    if len(first) == 1:
        return ""
    return first


def _infer_product_age_range(text: str) -> str:
    direct = re.search(r"\bAges?:\s*([^\n]+)", text, re.I)
    if direct:
        return _compact(direct.group(1), 80)
    patterns = [
        r"(\d+\s*-\s*\d+\s*months?)",
        r"(\d+\s*to\s*\d+\s*months?)",
        r"(\d+\+\s*months?)",
        r"(\d+\s*months?\s*(?:and up|old))",
        r"(\d+\s*-\s*\d+\s*years?)",
        r"(\d+\s*to\s*\d+\s*years?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _normalize_space(match.group(1))
    return ""


def _infer_product_category(title: str) -> str:
    lower = title.lower()
    if "dehumidifier" in lower:
        return "dehumidifier"
    if "humidifier" in lower:
        return "humidifier"
    if "air purifier" in lower and "filter" in lower:
        return "air_purifier_filter"
    if "air purifier" in lower:
        return "air_purifier"
    if "bookshelf" in lower or "bookcase" in lower:
        return "bookshelf"
    if "desk lamp" in lower:
        return "desk_lamp"
    if "lamp" in lower:
        return "lamp"
    if "fan" in lower:
        return "fan"
    if "mattress" in lower:
        return "mattress"
    if "vacuum" in lower or "carpet cleaner" in lower:
        return "vacuum_cleaner"
    if "cleaner" in lower or "vinegar" in lower:
        return "cleaner"
    if "filter" in lower or "replacement" in lower:
        return "replacement_filter"
    if "book" in lower:
        return "book"
    if any(token in lower for token in ("baby", "infant", "toddler")):
        return "baby_toy"
    if "rattle" in lower or "teether" in lower:
        return "teether_rattle"
    if "stacking" in lower or "blocks" in lower or "sorting" in lower:
        return "stacking_sorting"
    if "walker" in lower:
        return "walker_activity"
    if "projector" in lower or "musical" in lower or "light up" in lower:
        return "musical_light_up"
    if "montessori" in lower:
        return "montessori"
    if "sensory" in lower or "tummy time" in lower:
        return "sensory_development"
    return "generic_product"


def _looks_like_junk_product_name(name: str) -> bool:
    lower = _normalize_space(name).lower()
    if not lower:
        return True
    return any(re.search(pattern, lower, re.I) for pattern in PRODUCT_JUNK_NAME_PATTERNS)


def _expected_product_focus(task_spec: Optional[dict[str, Any]]) -> dict[str, set[str]]:
    prompt_lower = str((task_spec or {}).get("user_prompt", "")).strip().lower()
    categories: set[str] = set()
    terms: set[str] = set()
    if not prompt_lower:
        return {"categories": categories, "terms": terms}
    has_dehumidifier = "dehumidifier" in prompt_lower
    has_humidifier = "humidifier" in prompt_lower
    if has_dehumidifier:
        categories.add("dehumidifier")
        terms.add("dehumidifier")
    if has_humidifier and not has_dehumidifier:
        categories.add("humidifier")
        terms.add("humidifier")
    if "air purifier" in prompt_lower:
        categories.add("air_purifier")
        terms.update({"air purifier", "purifier"})
    if "desk lamp" in prompt_lower:
        categories.update({"desk_lamp", "lamp"})
        terms.update({"desk lamp", "lamp"})
    elif "lamp" in prompt_lower:
        categories.update({"desk_lamp", "lamp"})
        terms.add("lamp")
    if "mattress" in prompt_lower:
        categories.add("mattress")
        terms.add("mattress")
    if "fan" in prompt_lower:
        categories.add("fan")
        terms.add("fan")
    if "bookshelf" in prompt_lower or "bookcase" in prompt_lower:
        categories.add("bookshelf")
        terms.update({"bookshelf", "bookcase"})
    if any(token in prompt_lower for token in ("baby", "infant", "toddler", "toy", "toys")):
        categories.update({"baby_toy", "teether_rattle", "stacking_sorting", "walker_activity", "musical_light_up", "montessori", "sensory_development"})
        terms.update({"baby", "infant", "toddler", "toy", "toys"})
    return {"categories": categories, "terms": terms}


def _product_row_matches_mission(row: dict[str, Any], task_spec: Optional[dict[str, Any]]) -> bool:
    focus = _expected_product_focus(task_spec)
    categories = set(focus.get("categories", set()))
    terms = set(focus.get("terms", set()))
    if not categories and not terms:
        return True
    row_category = str(row.get("category", "")).strip().lower()
    if row_category and row_category in categories:
        return True
    product_text = " ".join(
        value
        for value in (
            str(row.get("product_name", "")).strip().lower(),
            str(row.get("brand", "")).strip().lower(),
            str(row.get("product_url", "")).strip().lower(),
            str(row.get("source_url", "")).strip().lower(),
        )
        if value
    )
    return any(term in product_text for term in terms)


def _extract_search_query_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    query = parse_qs(parsed.query).get("k", [])
    if not query:
        return ""
    return _normalize_space(unquote_plus(str(query[0])))


def _looks_like_generic_price_bucket(query: str) -> bool:
    clean = _normalize_space(query).lower()
    if not clean:
        return False
    if re.fullmatch(r"\$?\d+\s*(to|-)\s*\$?\d+", clean):
        return True
    if re.fullmatch(r"\$?\d+\s*(to|-)\s*\$?\d+\s+.*", clean) and "baby" not in clean and "toy" not in clean:
        return True
    return False


def _page_is_relevant_product_source(page: dict[str, Any], task_spec: Optional[dict[str, Any]] = None) -> bool:
    final_url = str(page.get("final_url", "") or page.get("requested_url", ""))
    lower_url = final_url.lower()
    if _is_search_engine_url(final_url):
        return False
    if "/zgbs/" in lower_url or "/gp/bestsellers/" in lower_url:
        return False
    query = _extract_search_query_from_url(final_url)
    query_lower = query.lower()
    if _looks_like_generic_price_bucket(query):
        return False
    target_title = _normalize_space(str(page.get("target_title", ""))).lower()
    if re.match(r"^\$\d+\s*(to|-)\s*\$\d+", target_title):
        return False
    task_prompt = str((task_spec or {}).get("user_prompt", "")).lower()
    if "baby" in task_prompt and "toy" in task_prompt and query:
        if (
            "baby" not in query_lower
            and "toy" not in query_lower
            and "infant" not in query_lower
            and "montessori" not in query_lower
        ):
            return False
    return True


def _synthetic_product_locator(page_url: str, product_name: str) -> str:
    clean_page_url = page_url.strip()
    if not clean_page_url:
        return ""
    return "{page_url}#product={slug}".format(
        page_url=clean_page_url,
        slug=quote_plus(_normalize_space(product_name)[:120]),
    )


def _looks_like_product_title_line(line: str) -> bool:
    clean = _normalize_space(line)
    if not clean:
        return False
    if _looks_like_junk_product_name(clean):
        return False
    lower = clean.lower()
    if lower in PRODUCT_TITLE_EXCLUDE_PREFIXES:
        return False
    if any(lower.startswith(prefix) for prefix in PRODUCT_TITLE_EXCLUDE_PREFIXES):
        return False
    if re.fullmatch(r"\(?[\d.,]+k?\+?\)?", lower):
        return False
    if re.fullmatch(r"\d(?:\.\d+)?", lower):
        return False
    if re.fullmatch(r"\d+\s+count\s+\(pack of \d+\)", lower):
        return False
    if re.fullmatch(r"\d+\s+count", lower):
        return False
    if "pack of" in lower and "count" in lower:
        return False
    if clean.startswith("$"):
        return False
    if "out of 5 stars" in lower:
        return False
    if "bought in past month" in lower:
        return False
    if "bought in last month" in lower:
        return False
    if lower.startswith("ages:"):
        return False
    if any(lower.startswith(prefix) for prefix in PRODUCT_FEATURE_LINE_PREFIXES):
        return False
    if len(clean) < 18:
        token_count = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.\-]*", clean))
        if token_count < 2:
            return False
    if not re.search(r"[A-Za-z]", clean):
        return False
    return True


def _looks_like_listing_meta_line(line: str) -> bool:
    clean = _normalize_space(line)
    if not clean:
        return True
    lower = clean.lower()
    if lower in PRODUCT_LISTING_META_PREFIXES:
        return True
    if lower.startswith("reg $"):
        return True
    if lower.startswith("sale"):
        return True
    if re.fullmatch(r"save\s+(?:\d+%|\$\d[\d.,]*)", lower):
        return True
    if "bought in last month" in lower:
        return True
    if lower.startswith("see price in cart"):
        return True
    if re.fullmatch(r"\d+\s+results", lower):
        return True
    return False


def _find_title_before_price_marker(lines: list[str], price_index: int) -> Optional[int]:
    lower_bound = max(0, price_index - 12)
    for index in range(price_index - 1, lower_bound - 1, -1):
        if _looks_like_listing_meta_line(lines[index]):
            continue
        if _looks_like_product_title_line(lines[index]):
            return index
    return None


def _find_title_after_price_marker(lines: list[str], price_index: int) -> Optional[int]:
    upper_bound = min(len(lines), price_index + 6)
    for index in range(price_index + 1, upper_bound):
        line = lines[index]
        if _looks_like_listing_meta_line(line):
            continue
        if _looks_like_product_title_line(line):
            return index
    return None


def _extract_price_from_block(lines: list[str], start_index: int, end_index: int) -> Optional[float]:
    for index in range(start_index, min(end_index, len(lines))):
        line = lines[index].strip()
        if not line.startswith("$"):
            continue
        if re.fullmatch(r"\$\d[\d,]*\.\d{2}", line):
            return _to_float(line[1:])
        if re.fullmatch(r"\$\d[\d,]*", line):
            if index + 2 < end_index and lines[index + 1].strip() == "." and re.fullmatch(r"\d{2}", lines[index + 2].strip()):
                return _to_float("{whole}.{fraction}".format(whole=line[1:].replace(",", ""), fraction=lines[index + 2].strip()))
            return _to_float(line[1:])
        match = re.match(r"\$(\d[\d,]*\.\d{2})\b", line)
        if match:
            return _to_float(match.group(1))
    return None


def _generic_listing_price_indexes(lines: list[str]) -> list[int]:
    indexes: list[int] = []
    for index, line in enumerate(lines):
        clean = _normalize_space(line)
        if not clean:
            continue
        if clean.startswith("$") and _extract_price_from_text(clean) is not None:
            indexes.append(index)
    return indexes


def _extract_generic_listing_product_rows(capsule_dir: Path, page: dict[str, Any]) -> list[dict[str, Any]]:
    final_url = str(page.get("final_url", "") or page.get("requested_url", ""))
    raw_text = _read_page_text(capsule_dir, page)
    lines = _nonempty_lines(raw_text)
    if not lines:
        return []

    price_indexes = _generic_listing_price_indexes(lines)
    rows: list[dict[str, Any]] = []
    retailer = _retailer_label_from_url(final_url)
    for position, price_index in enumerate(price_indexes):
        before_index = _find_title_before_price_marker(lines, price_index)
        after_index = _find_title_after_price_marker(lines, price_index)
        title_index = None
        if before_index is not None and after_index is not None:
            before_distance = price_index - before_index
            after_distance = after_index - price_index
            title_index = before_index if before_distance < after_distance else after_index
        elif before_index is not None:
            title_index = before_index
        elif after_index is not None:
            title_index = after_index
        if title_index is None:
            continue
        next_price_index = price_indexes[position + 1] if position + 1 < len(price_indexes) else len(lines)
        block_start = max(0, min(title_index, price_index) - 1)
        block_end = min(len(lines), max(next_price_index, title_index + 6, price_index + 6))
        block_lines = lines[block_start:block_end]
        block_text = "\n".join(block_lines)
        product_name = _normalize_space(lines[title_index])
        if not product_name or _looks_like_junk_product_name(product_name):
            continue
        price_value = _extract_price_from_text(lines[price_index])
        if price_value is None:
            price_value = _extract_price_from_block(lines, price_index, block_end)
        rating_value, review_count, _price_tier = _parse_rating_review_price(block_text)
        row = {
            "product_name": product_name,
            "brand": _infer_brand_from_product_name(product_name),
            "retailer": retailer,
            "product_url": _synthetic_product_locator(final_url, product_name),
            "price_value": price_value,
            "rating_value": rating_value,
            "review_count": review_count,
            "age_range_text": _infer_product_age_range(block_text or product_name),
            "category": _infer_product_category(product_name),
            "source_page_ids": [str(page.get("page_id", ""))],
            "source_url": final_url,
        }
        if any(row.get(key) not in (None, "", []) for key in ("price_value", "rating_value", "review_count")):
            rows.append(row)
    return rows


def _clean_detail_page_title(title: str, retailer: str) -> str:
    clean = _normalize_space(title)
    if not clean:
        return ""
    for separator in (" : ", " - ", " | "):
        suffix = "{separator}{retailer}".format(separator=separator, retailer=retailer)
        if clean.endswith(suffix):
            return _normalize_space(clean[: -len(suffix)])
    return clean


DETAIL_PAGE_SECTION_MARKERS = (
    "about this item",
    "description",
    "specifications",
    "shipping & returns",
    "q&a",
    "guest ratings & reviews",
    "highlights",
)


def _detail_page_marker_count(lines: list[str]) -> int:
    count = 0
    seen: set[str] = set()
    for line in lines[:160]:
        lower = _normalize_space(line).lower()
        for marker in DETAIL_PAGE_SECTION_MARKERS:
            if marker in lower and marker not in seen:
                seen.add(marker)
                count += 1
    return count


def _extract_direct_detail_title(lines: list[str], page_title: str, retailer: str) -> str:
    title_from_page = _clean_detail_page_title(page_title, retailer)
    if title_from_page and not _looks_like_junk_product_name(title_from_page) and not _looks_like_broad_category_title(title_from_page):
        return title_from_page
    for index, line in enumerate(lines[:40]):
        lower = _normalize_space(line).lower()
        if lower.startswith("shop all ") and index + 1 < len(lines):
            candidate = _normalize_space(lines[index + 1])
            if candidate and not _looks_like_junk_product_name(candidate) and not _looks_like_broad_category_title(candidate):
                return candidate
        if "out of 5 stars" in lower and index >= 1:
            candidate = _normalize_space(lines[index - 1])
            if candidate and not _looks_like_junk_product_name(candidate) and not _looks_like_broad_category_title(candidate):
                return candidate
    return ""


def _extract_direct_detail_product_rows(capsule_dir: Path, page: dict[str, Any]) -> list[dict[str, Any]]:
    final_url = str(page.get("final_url", "") or page.get("requested_url", "")).strip()
    if not _looks_like_direct_detail_url(final_url):
        return []
    raw_text = _read_page_text(capsule_dir, page)
    lines = _nonempty_lines(raw_text)
    if not lines:
        return []
    if _detail_page_marker_count(lines) < 2:
        return []
    retailer = _retailer_label_from_url(final_url)
    product_name = _extract_direct_detail_title(lines, str(page.get("title", "")), retailer)
    if not product_name:
        return []
    header_lines = lines[:80]
    header_text = "\n".join(header_lines)
    price_value = _extract_price_from_block(header_lines, 0, len(header_lines))
    if price_value is None:
        price_value = _extract_price_from_text(header_text)
    rating_value, review_count, _price_tier = _parse_rating_review_price(header_text)
    row = {
        "product_name": product_name,
        "brand": _infer_brand_from_product_name(product_name),
        "retailer": retailer,
        "product_url": final_url,
        "price_value": price_value,
        "rating_value": rating_value,
        "review_count": review_count,
        "age_range_text": _infer_product_age_range(header_text or product_name),
        "category": _infer_product_category(product_name),
        "source_page_ids": [str(page.get("page_id", ""))],
        "source_url": final_url,
    }
    if (
        row["product_name"]
        and not _looks_like_junk_product_name(row["product_name"])
        and any(row.get(key) not in (None, "", []) for key in ("price_value", "rating_value", "review_count"))
    ):
        return [row]
    return []


def _extract_amazon_search_product_rows(capsule_dir: Path, page: dict[str, Any]) -> list[dict[str, Any]]:
    final_url = str(page.get("final_url", "") or page.get("requested_url", ""))
    domain = _domain_from_url(final_url)
    if "amazon." not in domain:
        return []
    raw_text = _read_page_text(capsule_dir, page)
    lines = _nonempty_lines(raw_text)
    if "Price, product page" not in lines:
        return []

    price_indexes = [index for index, line in enumerate(lines) if line.lower() == "price, product page"]
    rows: list[dict[str, Any]] = []
    for position, price_index in enumerate(price_indexes):
        title_index = _find_title_before_price_marker(lines, price_index)
        if title_index is None:
            continue
        next_price_index = price_indexes[position + 1] if position + 1 < len(price_indexes) else len(lines)
        block_end = min(len(lines), max(price_index + 12, next_price_index))
        block_lines = lines[title_index:block_end]
        block_text = "\n".join(block_lines)
        product_name = _normalize_space(lines[title_index])
        rating_match = re.search(r"(\d(?:\.\d+)?)\s+out of 5 stars", block_text, re.I)
        review_match = re.search(r"\(([\d.,]+[KM]?)\)", block_text, re.I)
        price_value = _extract_price_from_block(lines, price_index + 1, block_end)
        rating_value = float(rating_match.group(1)) if rating_match else None
        review_count = _parse_quantity_text(review_match.group(1)) if review_match else None
        age_range_text = _infer_product_age_range(block_text or product_name)
        row = {
            "product_name": product_name,
            "brand": _infer_brand_from_product_name(product_name),
            "retailer": "Amazon",
            "product_url": _synthetic_product_locator(final_url, product_name),
            "price_value": price_value,
            "rating_value": rating_value,
            "review_count": review_count,
            "age_range_text": age_range_text,
            "category": _infer_product_category(product_name),
            "source_page_ids": [str(page.get("page_id", ""))],
            "source_url": final_url,
        }
        if (
            row["product_name"]
            and not _looks_like_junk_product_name(row["product_name"])
            and any(row.get(key) not in (None, "", []) for key in ("price_value", "rating_value", "review_count"))
        ):
            rows.append(row)
    return rows


def _merge_product_rows(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ("brand", "age_range_text", "category"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    incoming_product_url = str(incoming.get("product_url", "")).strip()
    merged_product_url = str(merged.get("product_url", "")).strip()
    if incoming_product_url and (
        not merged_product_url
        or (_looks_like_direct_detail_url(incoming_product_url) and not _looks_like_direct_detail_url(merged_product_url))
    ):
        merged["product_url"] = incoming_product_url
    incoming_source_url = str(incoming.get("source_url", "")).strip()
    merged_source_url = str(merged.get("source_url", "")).strip()
    if incoming_source_url and (
        not merged_source_url
        or (_looks_like_direct_detail_url(incoming_source_url) and not _looks_like_direct_detail_url(merged_source_url))
    ):
        merged["source_url"] = incoming_source_url
    if incoming.get("price_value") is not None and merged.get("price_value") is None:
        merged["price_value"] = incoming["price_value"]
    if incoming.get("rating_value") is not None and (merged.get("rating_value") is None or float(incoming["rating_value"]) > float(merged["rating_value"])):
        merged["rating_value"] = incoming["rating_value"]
    if incoming.get("review_count") is not None and int(incoming["review_count"]) > int(merged.get("review_count") or 0):
        merged["review_count"] = incoming["review_count"]
    source_page_ids = set(str(page_id) for page_id in merged.get("source_page_ids", []) if str(page_id).strip())
    source_page_ids.update(str(page_id) for page_id in incoming.get("source_page_ids", []) if str(page_id).strip())
    merged["source_page_ids"] = sorted(source_page_ids)
    return merged


def build_product_rows(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for page in manifest.get("pages", []):
        if not _page_is_relevant_product_source(page, task_spec):
            continue
        product_rows = _extract_direct_detail_product_rows(capsule_dir, page)
        if not product_rows:
            product_rows = _extract_amazon_search_product_rows(capsule_dir, page)
        if not product_rows:
            product_rows = _extract_generic_listing_product_rows(capsule_dir, page)
        for row in product_rows:
            if not _product_row_matches_mission(row, task_spec):
                continue
            key = (
                str(row.get("retailer", "")).strip().lower(),
                _normalize_space(str(row.get("product_name", "")).lower()),
            )
            if not key[0] or not key[1]:
                continue
            existing = rows_by_key.get(key)
            if existing is None:
                rows_by_key[key] = row
            else:
                rows_by_key[key] = _merge_product_rows(existing, row)

    rows = list(rows_by_key.values())
    rows.sort(
        key=lambda item: (
            int(item.get("review_count") or 0),
            float(item.get("rating_value") or 0.0),
            str(item.get("product_name", "")),
        ),
        reverse=True,
    )
    return rows


def _is_missing_shape_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False


def _row_id_for_object(object_name: str, row: dict[str, Any], primary_key: list[str]) -> str:
    parts = [object_name]
    for key in primary_key:
        value = row.get(key)
        if _is_missing_shape_value(value):
            continue
        parts.append(_normalize_space(str(value)))
    if len(parts) == 1:
        parts.append(json.dumps(row, sort_keys=True, ensure_ascii=True))
    digest = sha1("||".join(parts).encode("utf-8", "replace")).hexdigest()[:12]
    return "{name}-{digest}".format(name=object_name, digest=digest)


def _row_confidence_metrics(row: dict[str, Any], required_columns: list[str]) -> tuple[float, str]:
    if not required_columns:
        return 1.0, "high"
    present_count = sum(0 if _is_missing_shape_value(row.get(column)) else 1 for column in required_columns)
    fraction = present_count / len(required_columns)
    if fraction >= 0.85:
        label = "high"
    elif fraction >= 0.6:
        label = "medium"
    else:
        label = "low"
    return round(fraction, 3), label


def _row_display_label(row: dict[str, Any]) -> str:
    for key in (
        "product_name",
        "brand_name",
        "restaurant_name",
        "title_clean",
        "title",
        "listing_title",
        "candidate_name",
    ):
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _row_source_url(row: dict[str, Any]) -> str:
    for key in ("source_url", "product_url", "listing_url", "primary_source_url"):
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _build_provenance_rows(
    *,
    object_name: str,
    row_id: str,
    row: dict[str, Any],
    extractor_name: str,
    extractor_version: int,
) -> list[dict[str, Any]]:
    page_ids = [
        str(page_id).strip()
        for page_id in row.get("source_page_ids", [])
        if str(page_id).strip()
    ]
    if not page_ids:
        page_ids = [str(row.get("source_page_id", "")).strip()] if str(row.get("source_page_id", "")).strip() else []
    evidence_excerpt = _compact(_row_display_label(row), 160)
    source_url = _row_source_url(row)
    provenance_rows: list[dict[str, Any]] = []
    if not page_ids:
        provenance_rows.append(
            {
                "row_id": row_id,
                "object_name": object_name,
                "source_page_id": "",
                "source_url": source_url,
                "evidence_type": "row_reference",
                "evidence_excerpt": evidence_excerpt,
                "extractor_name": extractor_name,
                "extractor_version": extractor_version,
            }
        )
        return provenance_rows
    for page_id in sorted(set(page_ids)):
        provenance_rows.append(
            {
                "row_id": row_id,
                "object_name": object_name,
                "source_page_id": page_id,
                "source_url": source_url,
                "evidence_type": "page_reference",
                "evidence_excerpt": evidence_excerpt,
                "extractor_name": extractor_name,
                "extractor_version": extractor_version,
            }
        )
    return provenance_rows


def _finalize_shape_artifact(
    *,
    object_name: str,
    rows: list[dict[str, Any]],
    task_spec: dict[str, Any],
    extractor_name: str,
    extractor_version: int = 1,
) -> dict[str, Any]:
    target = _first_target_object(task_spec)
    primary_key = [
        str(field).strip()
        for field in target.get("primary_key", [])
        if str(field).strip()
    ]
    required_columns = [
        str(field).strip()
        for field in target.get("required_columns", [])
        if str(field).strip()
    ]
    finalized_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enriched = dict(row)
        row_id = _row_id_for_object(object_name, enriched, primary_key)
        confidence_score, confidence_label = _row_confidence_metrics(enriched, required_columns)
        enriched["row_id"] = row_id
        enriched["row_confidence"] = confidence_label
        enriched["row_confidence_score"] = confidence_score
        finalized_rows.append(enriched)
        provenance_rows.extend(
            _build_provenance_rows(
                object_name=object_name,
                row_id=row_id,
                row=enriched,
                extractor_name=extractor_name,
                extractor_version=extractor_version,
            )
        )
    return {
        "object_name": object_name,
        "rows": finalized_rows,
        "provenance_rows": provenance_rows,
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "row_count": len(finalized_rows),
        "provenance_count": len(provenance_rows),
    }


def _build_passthrough_shape_artifact(
    *,
    capsule_dir: Path,
    object_name: str,
    task_spec: dict[str, Any],
    extractor_name: str = "primary_table_passthrough_v1",
) -> Optional[dict[str, Any]]:
    table_path = capsule_dir / "tables" / f"{object_name}.jsonl"
    if not table_path.exists():
        return None
    rows = _read_jsonl_rows(table_path)
    return _finalize_shape_artifact(
        object_name=object_name,
        rows=rows,
        task_spec=task_spec,
        extractor_name=extractor_name,
    )


def _shape_products(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = build_product_rows(capsule_dir, manifest, task_spec)
    return _finalize_shape_artifact(
        object_name="products",
        rows=rows,
        task_spec=task_spec,
        extractor_name="product_search_rows_v1",
    )


def _shape_restaurant_chains(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = build_restaurant_chain_rows(capsule_dir, manifest, scout_index or {})
    return _finalize_shape_artifact(
        object_name="restaurant_chains",
        rows=rows,
        task_spec=task_spec,
        extractor_name="restaurant_chain_rows_v1",
    )


def _shape_stock_candidates(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = build_stock_candidate_rows(capsule_dir, manifest, task_spec)
    if not rows and scout_index:
        rows = _build_stock_candidate_rows_from_scout(task_spec, scout_index)
    return _finalize_shape_artifact(
        object_name="stock_candidates",
        rows=rows,
        task_spec=task_spec,
        extractor_name="stock_candidate_rows_v1",
    )


def _shape_market_contracts(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = build_market_contract_rows(capsule_dir, manifest, task_spec)
    if not rows:
        passthrough = _build_passthrough_shape_artifact(
            capsule_dir=capsule_dir,
            object_name="market_contracts",
            task_spec=task_spec,
        )
        if passthrough and passthrough.get("row_count", 0):
            return passthrough
    return _finalize_shape_artifact(
        object_name="market_contracts",
        rows=rows,
        task_spec=task_spec,
        extractor_name="market_contract_rows_v1",
    )


def _shape_home_sale_signals(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = build_home_sale_signal_rows(capsule_dir, manifest)
    if not rows and scout_index:
        rows = _build_home_sale_signal_rows_from_scout(task_spec, scout_index)
    if not rows:
        passthrough = _build_passthrough_shape_artifact(
            capsule_dir=capsule_dir,
            object_name="home_sale_signals",
            task_spec=task_spec,
        )
        if passthrough and passthrough.get("row_count", 0):
            return passthrough
    return _finalize_shape_artifact(
        object_name="home_sale_signals",
        rows=rows,
        task_spec=task_spec,
        extractor_name="home_sale_signal_rows_v1",
    )


def _shape_neighborhood_price_rankings(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = build_neighborhood_price_ranking_rows(capsule_dir, manifest)
    if not rows:
        passthrough = _build_passthrough_shape_artifact(
            capsule_dir=capsule_dir,
            object_name="neighborhood_price_rankings",
            task_spec=task_spec,
        )
        if passthrough and passthrough.get("row_count", 0):
            return passthrough
    return _finalize_shape_artifact(
        object_name="neighborhood_price_rankings",
        rows=rows,
        task_spec=task_spec,
        extractor_name="neighborhood_price_ranking_rows_v1",
    )


def _shape_vehicle_listings(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    passthrough = _build_passthrough_shape_artifact(
        capsule_dir=capsule_dir,
        object_name="vehicle_listings",
        task_spec=task_spec,
    )
    if passthrough and passthrough.get("row_count", 0):
        return passthrough
    rows = _build_vehicle_listing_rows(task_spec, scout_index or {})
    return _finalize_shape_artifact(
        object_name="vehicle_listings",
        rows=rows,
        task_spec=task_spec,
        extractor_name="vehicle_listing_candidates_v1",
    )


def _shape_mattress_listings(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    passthrough = _build_passthrough_shape_artifact(
        capsule_dir=capsule_dir,
        object_name="mattress_listings",
        task_spec=task_spec,
    )
    if passthrough and passthrough.get("row_count", 0):
        return passthrough
    rows = _build_mattress_listing_rows(task_spec, scout_index or {})
    return _finalize_shape_artifact(
        object_name="mattress_listings",
        rows=rows,
        task_spec=task_spec,
        extractor_name="mattress_listing_candidates_v1",
    )


def _shape_land_listings(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    passthrough = _build_passthrough_shape_artifact(
        capsule_dir=capsule_dir,
        object_name="land_listings",
        task_spec=task_spec,
    )
    if passthrough and passthrough.get("row_count", 0):
        return passthrough
    rows = _build_land_listing_rows(task_spec, scout_index or {})
    return _finalize_shape_artifact(
        object_name="land_listings",
        rows=rows,
        task_spec=task_spec,
        extractor_name="land_listing_candidates_v1",
    )


def _shape_rental_listings(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = _build_rental_listing_rows(capsule_dir, manifest)
    if not rows:
        passthrough = _build_passthrough_shape_artifact(
            capsule_dir=capsule_dir,
            object_name="rental_listings",
            task_spec=task_spec,
        )
        if passthrough and passthrough.get("row_count", 0):
            return passthrough
    return _finalize_shape_artifact(
        object_name="rental_listings",
        rows=rows,
        task_spec=task_spec,
        extractor_name="rental_listing_results_v1",
    )


def _shape_coworking_spaces(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rows = _build_coworking_space_rows(capsule_dir, manifest, task_spec, scout_index or {})
    if not rows:
        passthrough = _build_passthrough_shape_artifact(
            capsule_dir=capsule_dir,
            object_name="coworking_spaces",
            task_spec=task_spec,
        )
        if passthrough and passthrough.get("row_count", 0):
            return passthrough
    return _finalize_shape_artifact(
        object_name="coworking_spaces",
        rows=rows,
        task_spec=task_spec,
        extractor_name="coworking_space_rows_v1",
    )


def _target_matches_vehicle_shape(task_spec: dict[str, Any]) -> bool:
    target = _first_target_object(task_spec)
    measures = {str(item).strip() for item in target.get("measures", []) if str(item).strip()}
    required_columns = {str(item).strip() for item in target.get("required_columns", []) if str(item).strip()}
    dimensions = {str(item).strip() for item in target.get("dimensions", []) if str(item).strip()}
    return (
        bool({"odometer_miles", "mileage_miles"} & (measures | required_columns))
        and ("model_year" in required_columns or "model_year" in dimensions)
        and ("make" in required_columns or "make" in dimensions)
        and ("model" in required_columns or "model" in dimensions)
        and bool({"price_usd", "asking_price_usd", "observed_price_usd"} & (measures | required_columns))
    )


def _shape_vehicle_like_target(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target_name = str(_first_target_object(task_spec).get("name", "")).strip() or "vehicle_listings"
    table_path = capsule_dir / "tables" / "{name}.jsonl".format(name=target_name)
    if table_path.exists():
        rows = _read_jsonl_rows(table_path)
        return _finalize_shape_artifact(
            object_name=target_name,
            rows=rows,
            task_spec=task_spec,
            extractor_name="primary_table_passthrough_v1",
        )
    rows = _build_vehicle_listing_rows(task_spec, scout_index or {})
    return _finalize_shape_artifact(
        object_name=target_name,
        rows=rows,
        task_spec=task_spec,
        extractor_name="vehicle_listing_candidates_v1",
    )


PRIMARY_OBJECT_SHAPE_BUILDERS = {
    "products": _shape_products,
    "stock_candidates": _shape_stock_candidates,
    "market_contracts": _shape_market_contracts,
    "restaurant_chains": _shape_restaurant_chains,
    "vehicle_listings": _shape_vehicle_listings,
    "mattress_listings": _shape_mattress_listings,
    "rental_listings": _shape_rental_listings,
    "land_listings": _shape_land_listings,
    "home_sale_signals": _shape_home_sale_signals,
    "neighborhood_price_rankings": _shape_neighborhood_price_rankings,
    "coworking_spaces": _shape_coworking_spaces,
}


def build_primary_object_shape_artifact(
    capsule_dir: Path,
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    scout_index: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    target_name = str(_first_target_object(task_spec).get("name", "")).strip()
    builder = PRIMARY_OBJECT_SHAPE_BUILDERS.get(target_name)
    if builder:
        return builder(
            capsule_dir,
            manifest,
            task_spec,
            scout_index=scout_index,
        )
    if _target_matches_vehicle_shape(task_spec):
        return _shape_vehicle_like_target(
            capsule_dir,
            manifest,
            task_spec,
            scout_index=scout_index,
        )
    if target_name:
        return _build_passthrough_shape_artifact(
            capsule_dir=capsule_dir,
            object_name=target_name,
            task_spec=task_spec,
        )
    return None


def _clean_page_text(raw_text: str) -> str:
    text = raw_text
    text = re.sub(r"^=== Page Text ===\s*Length:\s*\d+\s*chars\s*", "", text, flags=re.I)
    text = _normalize_space(text)

    noise_patterns = [
        r"We use cookies to improve your experience on our site and to show you personalized advertising\..*?cookie policy\.\s*OK",
        r"The following text input provides auto-suggestions as you type\..*?close the suggestions\.",
        r"Find a school or district \.\.\.",
        r"Search in a district or a location \.\.\.",
        r"Skip to Main Content",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, " ", text, flags=re.I)
    return _normalize_space(text)


def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    return urlparse(url).netloc.lower()


def _title_root(title: str) -> str:
    if " - " in title:
        return title.split(" - ", 1)[0].strip()
    if " – " in title:
        return title.split(" – ", 1)[0].strip()
    return title.strip()


def _capture_regex(
    text: str,
    pattern: str,
    *,
    cast: Any = None,
    flags: int = re.I,
) -> tuple[Any, Optional[str]]:
    match = re.search(pattern, text, flags)
    if not match:
        return None, None
    value = match.group(1) if match.lastindex else match.group(0)
    if cast is not None:
        value = cast(value)
    excerpt = _compact(text[max(0, match.start() - 100): match.end() + 120], 260)
    return value, excerpt


def _to_int(value: str) -> int:
    return int(value.replace(",", "").strip())


def _to_float(value: str) -> float:
    return float(value.replace(",", "").strip())


def _extract_grade_after_label(text: str, label: str) -> tuple[Optional[str], Optional[str]]:
    direct_match = re.search(
        r"{label}\s+grade\s+([ABCDF][+-]?)".format(label=re.escape(label)),
        text,
        re.I,
    )
    if direct_match:
        excerpt = _compact(text[max(0, direct_match.start() - 60): direct_match.end() + 80], 220)
        return direct_match.group(1), excerpt

    idx = text.lower().find(label.lower())
    if idx == -1:
        return None, None
    window = text[idx: idx + 120]
    match = re.search(r"\b([ABCDF][+-]?)\b", window)
    if not match:
        return None, None
    return match.group(1), _compact(window, 220)


def _infer_entity_name(title: str, text: str) -> str:
    patterns = [
        r"([A-Z][A-Za-z0-9 .&'\-]+High School District(?: No\.? \d+| \d+)?)",
        r"([A-Z][A-Za-z0-9 .&'\-]+School District(?: No\.? \d+| \d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            return _normalize_space(match.group(1))

    root = _title_root(title)
    if "school district" in root.lower():
        return root

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _normalize_space(match.group(1))
    return root or title


def _classify_source(url: str, title: str, text: str) -> str:
    domain = _domain_from_url(url)
    title_lower = title.lower()
    text_lower = text.lower()

    if "illinoisreportcard.com" in domain:
        return "state_report_card"
    if "niche.com" in domain and "/k12/d/" in url:
        return "district_profile"
    if "niche.com" in domain and ("rankings" in url or "best school districts" in title_lower):
        return "ranking_page"
    if any(news_domain in domain for news_domain in NEWS_DOMAINS):
        if "best" in title_lower or "ranked" in title_lower:
            return "ranking_article"
        return "news_article"
    if "school district" in title_lower and domain and "niche.com" not in domain:
        return "official_site"
    if "best school districts" in title_lower or "#1 in best school districts" in text_lower:
        return "ranking_page"
    return "unknown"


def _infer_region(task: str, sources: list[dict[str, Any]]) -> Optional[str]:
    haystack_parts = [task]
    for source in sources:
        haystack_parts.extend(
            [
                str(source.get("title", "")),
                str(source.get("final_url", "")),
                str(source.get("entity_name", "")),
            ]
        )
    haystack = " ".join(haystack_parts).lower()
    if "chicago" in haystack or "illinois" in haystack or "-il/" in haystack:
        return "illinois"
    return None


def _required_entity_source_types(recipe: str) -> list[str]:
    if recipe != RECIPE_HIGHSCHOOL:
        return []
    return [
        item["source_type"]
        for item in DISTRICT_SOURCE_REQUIREMENTS
        if item.get("scope") == "entity" and item.get("required")
    ]


def _recommended_queries(
    entity_name: str,
    *,
    missing_source_types: list[str],
    missing_fields: list[str],
    region: Optional[str],
) -> list[dict[str, str]]:
    region_guide = REGION_GUIDES.get(region or "")
    region_label = region_guide["label"] if region_guide else ""
    queries: list[dict[str, str]] = []

    if "district_profile" in missing_source_types:
        queries.append(
            {
                "source_type": "district_profile",
                "query": 'site:niche.com "{entity}"'.format(entity=entity_name),
                "why": "Find a broad profile page with basic metrics and sentiment.",
            }
        )

    if "official_site" in missing_source_types:
        official_query = '"{entity}" official site'.format(entity=entity_name)
        if region_label:
            official_query = "{query} {region}".format(query=official_query, region=region_label)
        queries.append(
            {
                "source_type": "official_site",
                "query": official_query,
                "why": "Find the district's official site for grade-band and program corroboration.",
            }
        )
        queries.append(
            {
                "source_type": "official_site",
                "query": 'site:.org "{entity}"'.format(entity=entity_name),
                "why": "Bias toward district-controlled domains instead of aggregator pages.",
            }
        )

    if "state_report_card" in missing_source_types:
        if region_guide:
            queries.append(
                {
                    "source_type": "state_report_card",
                    "query": 'site:{domain} "{entity}"'.format(
                        domain=region_guide["state_report_card_domain"],
                        entity=entity_name,
                    ),
                    "why": "Find the state's report-card page for official quantitative metrics.",
                }
            )
            queries.append(
                {
                    "source_type": "state_report_card",
                    "query": 'site:{domain} "{entity}"'.format(
                        domain=region_guide["state_agency_domain"],
                        entity=entity_name,
                    ),
                    "why": "Fall back to the state education agency if the report-card site is sparse.",
                }
            )
        else:
            queries.append(
                {
                    "source_type": "state_report_card",
                    "query": '"{entity}" state report card'.format(entity=entity_name),
                    "why": "Find a state accountability or report-card source for official metrics.",
                }
            )

    missing_metric_priority = [
        field
        for field in ("student_teacher_ratio", "math_proficiency_pct", "reading_proficiency_pct", "graduation_rate_pct")
        if field in missing_fields
    ]
    if missing_metric_priority:
        metric_terms = {
            "student_teacher_ratio": "student-teacher ratio",
            "math_proficiency_pct": "math proficiency",
            "reading_proficiency_pct": "reading proficiency",
            "graduation_rate_pct": "graduation rate",
        }
        query_terms = [metric_terms[field] for field in missing_metric_priority[:2]]
        metric_query = '"{entity}" "{terms}"'.format(
            entity=entity_name,
            terms='" "'.join(query_terms),
        )
        if region_guide:
            metric_query = "site:{domain} {query}".format(
                domain=region_guide["state_report_card_domain"],
                query=metric_query,
            )
        queries.append(
            {
                "source_type": "metric_followup",
                "query": metric_query,
                "why": "Target the highest-value missing metrics for ranking.",
            }
        )
    return queries


def _infer_comparability(entity_name: str, grades_served: Optional[str], text: str) -> str:
    if grades_served:
        grades_lower = grades_served.lower()
        if "9-12" in grades_lower or "9 - 12" in grades_lower:
            return "high_school_only"
        if "k-12" in grades_lower or "pk-12" in grades_lower or "pre-k" in grades_lower:
            return "mixed_k12"
        if any(token in grades_lower for token in ("k-", "pk-", "0-8", "1-8", "6-12")):
            return "mixed_k12"

    haystack = " ".join([entity_name, text[:1200]]).lower()
    if "high school district" in haystack:
        return "high_school_only"
    return "unknown"


def _extract_metrics(text: str) -> tuple[dict[str, Any], dict[str, str]]:
    metrics: dict[str, Any] = {}
    evidence: dict[str, str] = {}

    rating, rating_excerpt = _capture_regex(text, r"Rating\s+([0-9]+(?:\.[0-9]+)?)\s+out of 5", cast=_to_float)
    if rating is not None:
        metrics["rating_out_of_5"] = rating
        evidence["rating_out_of_5"] = rating_excerpt or ""

    reviews, review_excerpt = _capture_regex(
        text,
        r"Rating\s+[0-9]+(?:\.[0-9]+)?\s+out of 5\s+(\d[\d,]*)\s+reviews",
        cast=_to_int,
    )
    if reviews is None:
        reviews, review_excerpt = _capture_regex(text, r"(\d[\d,]*)\s+reviews", cast=_to_int)
    if reviews is not None:
        metrics["review_count"] = reviews
        evidence["review_count"] = review_excerpt or ""

    students, students_excerpt = _capture_regex(text, r"It has\s+(\d[\d,]*)\s+students\b", cast=_to_int)
    if students is not None:
        metrics["student_count"] = students
        evidence["student_count"] = students_excerpt or ""

    grades_served, grades_excerpt = _capture_regex(text, r"students in grades\s+([A-Za-z0-9\-]+)")
    if grades_served is None:
        grades_served, grades_excerpt = _capture_regex(text, r"\b(9-12|K-12|PK-12|6-12)\b")
    if grades_served is not None:
        metrics["grades_served"] = str(grades_served)
        evidence["grades_served"] = grades_excerpt or ""

    ratio, ratio_excerpt = _capture_regex(
        text,
        r"student-teacher ratio of\s+([0-9]+(?:\.[0-9]+)?)\s+to\s+1",
        cast=_to_float,
    )
    if ratio is not None:
        metrics["student_teacher_ratio"] = ratio
        evidence["student_teacher_ratio"] = ratio_excerpt or ""

    match = re.search(
        r"(\d{1,3})%\s+of students are at least proficient in math and\s+(\d{1,3})%\s+in reading",
        text,
        re.I,
    )
    if match:
        metrics["math_proficiency_pct"] = int(match.group(1))
        metrics["reading_proficiency_pct"] = int(match.group(2))
        excerpt = _compact(text[max(0, match.start() - 80): match.end() + 120], 260)
        evidence["math_proficiency_pct"] = excerpt
        evidence["reading_proficiency_pct"] = excerpt

    grad_rate, grad_excerpt = _capture_regex(text, r"Average Graduation Rate\s+(\d{1,3})%", cast=_to_int)
    if grad_rate is not None:
        metrics["graduation_rate_pct"] = grad_rate
        evidence["graduation_rate_pct"] = grad_excerpt or ""

    sat, sat_excerpt = _capture_regex(text, r"Average SAT\s+(\d{3,4})", cast=_to_int)
    if sat is not None:
        metrics["average_sat"] = sat
        evidence["average_sat"] = sat_excerpt or ""

    act, act_excerpt = _capture_regex(text, r"Average ACT\s+(\d{1,2})", cast=_to_int)
    if act is not None:
        metrics["average_act"] = act
        evidence["average_act"] = act_excerpt or ""

    for label, key in [
        ("Overall Niche Grade", "overall_niche_grade"),
        ("Academics", "academics_grade"),
        ("Diversity", "diversity_grade"),
        ("Teachers", "teachers_grade"),
        ("College Prep", "college_prep_grade"),
        ("Clubs & Activities", "clubs_activities_grade"),
        ("Administration", "administration_grade"),
    ]:
        grade, grade_excerpt = _extract_grade_after_label(text, label)
        if grade is not None:
            metrics[key] = grade
            evidence[key] = grade_excerpt or ""

    if "AP Offered" in text:
        metrics["ap_offered"] = True
        evidence["ap_offered"] = _compact(text[max(0, text.find("AP Offered") - 80): text.find("AP Offered") + 120], 200)

    rank_america, rank_excerpt = _capture_regex(text, r"#(\d+)\s+in Best School Districts in America", cast=_to_int)
    if rank_america is not None:
        metrics["best_district_rank_america"] = rank_america
        evidence["best_district_rank_america"] = rank_excerpt or ""

    rank_state, rank_state_excerpt = _capture_regex(text, r"#(\d+)\s+of\s+\d+\s+Best School Districts in Illinois", cast=_to_int)
    if rank_state is not None:
        metrics["best_district_rank_illinois"] = rank_state
        evidence["best_district_rank_illinois"] = rank_state_excerpt or ""

    return metrics, evidence


def build_source_index(capsule_dir: Path, manifest: dict[str, Any], recipe: str) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []

    for page in manifest.get("pages", []):
        title = str(page.get("title", ""))
        final_url = str(page.get("final_url", ""))
        raw_text = _read_page_text(capsule_dir, page)
        clean_text = _clean_page_text(raw_text)
        entity_name = _infer_entity_name(title, clean_text)
        source_type = _classify_source(final_url, title, clean_text)
        extracted_metrics, metric_evidence = _extract_metrics(clean_text)
        if source_type not in ("district_profile", "official_site", "state_report_card"):
            allowed = {"best_district_rank_america", "best_district_rank_illinois"}
            extracted_metrics = {
                key: value for key, value in extracted_metrics.items() if key in allowed
            }
            metric_evidence = {
                key: value for key, value in metric_evidence.items() if key in allowed
            }
        comparability_flag = _infer_comparability(
            entity_name,
            extracted_metrics.get("grades_served"),
            clean_text if source_type in ("district_profile", "official_site", "state_report_card") else entity_name,
        )

        if source_type in ("ranking_article", "ranking_page") and "best school districts" in clean_text.lower():
            source_role = "context_source"
        elif source_type in ("district_profile", "official_site", "state_report_card"):
            source_role = "entity_source"
        else:
            source_role = "supporting_source"

        sources.append(
            {
                "page_id": page.get("page_id", ""),
                "final_url": final_url,
                "requested_url": page.get("requested_url", ""),
                "title": title,
                "domain": _domain_from_url(final_url),
                "source_type": source_type,
                "source_role": source_role,
                "source_quality_score": SOURCE_QUALITY_SCORES.get(source_type, 0),
                "entity_name": entity_name,
                "comparability_flag": comparability_flag,
                "grades_served": extracted_metrics.get("grades_served"),
                "excerpt": _compact(clean_text, 420),
                "present_metric_fields": sorted(
                    key for key, value in extracted_metrics.items() if value not in (None, "", [])
                ),
                "extracted_metrics": extracted_metrics,
                "metric_evidence": metric_evidence,
                "captured_at": page.get("captured_at", ""),
            }
        )

    return {
        "generated_at": now_iso(),
        "recipe": recipe,
        "sources": sources,
    }


def _primary_source(items: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: SOURCE_TYPE_PRIORITY.get(item.get("source_type", "unknown"), 99))
    return ordered[0] if ordered else {}


def _entity_comparability(items: list[dict[str, Any]]) -> str:
    flags = {item.get("comparability_flag", "unknown") for item in items}
    if "mixed_k12" in flags:
        return "mixed_k12"
    if "high_school_only" in flags:
        return "high_school_only"
    return "unknown"


def build_schema_summary(recipe: str, source_index: dict[str, Any]) -> dict[str, Any]:
    sources = list(source_index.get("sources", []))
    source_type_counts = Counter(source.get("source_type", "unknown") for source in sources)
    required_source_types = _required_entity_source_types(recipe)
    entities_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        entity_name = str(source.get("entity_name", "")).strip()
        if entity_name:
            entities_map[entity_name].append(source)

    entities: list[dict[str, Any]] = []
    for entity_name, items in sorted(entities_map.items()):
        primary = _primary_source(items)
        fields_present = sorted(
            {
                field
                for item in items
                for field in item.get("present_metric_fields", [])
            }
        )
        missing_fields = [field for field in DISTRICT_FIELDS if field not in fields_present]
        source_types = sorted({item.get("source_type", "unknown") for item in items})
        missing_source_types = [source_type for source_type in required_source_types if source_type not in source_types]
        entities.append(
            {
                "entity_name": entity_name,
                "comparability_flag": _entity_comparability(items),
                "source_count": len(items),
                "source_page_ids": [item.get("page_id", "") for item in items],
                "source_types": source_types,
                "missing_source_types": missing_source_types,
                "source_mix_complete": not missing_source_types,
                "fields_present": fields_present,
                "missing_fields": missing_fields,
                "primary_source": {
                    "page_id": primary.get("page_id", ""),
                    "final_url": primary.get("final_url", ""),
                    "source_type": primary.get("source_type", "unknown"),
                },
            }
        )

    return {
        "generated_at": now_iso(),
        "recipe": recipe,
        "source_count": len(sources),
        "source_type_counts": dict(sorted(source_type_counts.items())),
        "entity_count": len(entities),
        "entities": entities,
        "fields_observed": sorted(
            {
                field
                for entity in entities
                for field in entity.get("fields_present", [])
            }
        ),
    }


def _generic_keywords(task: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-]+", task.lower())
    unique: list[str] = []
    for word in words:
        if word in STOPWORDS or len(word) < 4:
            continue
        if word not in unique:
            unique.append(word)
    return unique[:8]


def build_analysis_plan(
    recipe: str,
    manifest: dict[str, Any],
    source_index: dict[str, Any],
    schema_summary: dict[str, Any],
) -> dict[str, Any]:
    if recipe == RECIPE_HIGHSCHOOL:
        return {
            "version": 1,
            "generated_at": now_iso(),
            "task_type": RECIPE_HIGHSCHOOL,
            "task": manifest.get("task", ""),
            "questions": [
                "Which district scores highest under the current rubric?",
                "Which district looks strongest academically?",
                "Which district has the best student-teacher ratio?",
                "Which districts have missing or conflicting metrics?",
            ],
            "keywords": DISTRICT_KEYWORDS,
            "entities": schema_summary.get("entities", []),
            "fields_to_extract": DISTRICT_FIELDS,
            "rubric": DISTRICT_RUBRIC,
            "ranking_policy": DISTRICT_RANKING_POLICY,
            "source_requirements": DISTRICT_SOURCE_REQUIREMENTS,
            "cells": [
                {"template": "load_capsule", "title": "Load Capsule And Plan"},
                {"template": "inspect_sources", "title": "Inspect Captured Sources"},
                {"template": "validate_comparability", "title": "Validate Comparability"},
                {"template": "search_terms", "title": "Search District Keywords"},
                {"template": "extract_district_metrics", "title": "Extract District Metrics"},
                {"template": "compare_entities", "title": "Compare Districts"},
                {"template": "queue_followups", "title": "Suggest Followups"},
            ],
        }

    return {
        "version": 1,
        "generated_at": now_iso(),
        "task_type": RECIPE_GENERIC,
        "task": manifest.get("task", ""),
        "questions": [
            "What sources were captured?",
            "What keywords from the task are present in the captured text?",
            "What follow-up browsing tasks are still needed?",
        ],
        "keywords": _generic_keywords(str(manifest.get("task", ""))),
        "entities": schema_summary.get("entities", []),
        "fields_to_extract": [],
        "rubric": [],
        "cells": [
            {"template": "load_capsule", "title": "Load Capsule And Plan"},
            {"template": "inspect_sources", "title": "Inspect Captured Sources"},
            {"template": "search_terms", "title": "Search Task Keywords"},
            {"template": "queue_followups", "title": "Suggest Followups"},
        ],
    }


def build_capture_brief(
    recipe: str,
    manifest: dict[str, Any],
    source_index: dict[str, Any],
    schema_summary: dict[str, Any],
) -> dict[str, Any]:
    sources = list(source_index.get("sources", []))
    region = _infer_region(str(manifest.get("task", "")), sources)
    entities = list(schema_summary.get("entities", []))

    if recipe != RECIPE_HIGHSCHOOL:
        return {
            "generated_at": now_iso(),
            "recipe": recipe,
            "task": manifest.get("task", ""),
            "region": region,
            "source_type_counts": schema_summary.get("source_type_counts", {}),
            "summary": {
                "source_count": schema_summary.get("source_count", 0),
                "entity_count": len(entities),
            },
            "entities": [],
            "notes": [
                "Capture brief generation is currently specialized for the high-school district recipe.",
            ],
        }

    required_entity_source_types = _required_entity_source_types(recipe)
    entity_rows: list[dict[str, Any]] = []
    missing_source_type_counts: Counter[str] = Counter()
    missing_field_counts: Counter[str] = Counter()
    comparable_entity_count = 0
    source_mix_ready_count = 0
    rank_critical_gap_entities: list[str] = []
    required_fields = list(DISTRICT_RANKING_POLICY.get("required_fields", []))
    for entity in entities:
        entity_name = str(entity.get("entity_name", ""))
        current_source_types = list(entity.get("source_types", []))
        missing_source_types = [
            source_type
            for source_type in required_entity_source_types
            if source_type not in current_source_types
        ]
        missing_fields = list(entity.get("missing_fields", []))
        missing_rank_critical_fields = [field for field in required_fields if field in missing_fields]
        if entity.get("comparability_flag") == "high_school_only":
            comparable_entity_count += 1
        if not missing_source_types:
            source_mix_ready_count += 1
        else:
            missing_source_type_counts.update(missing_source_types)
        if missing_rank_critical_fields:
            rank_critical_gap_entities.append(entity_name)
            missing_field_counts.update(missing_rank_critical_fields)
        entity_rows.append(
            {
                "entity_name": entity_name,
                "comparability_flag": entity.get("comparability_flag", "unknown"),
                "current_source_types": current_source_types,
                "missing_required_source_types": missing_source_types,
                "source_mix_ready": not missing_source_types,
                "missing_fields": missing_fields,
                "missing_rank_critical_fields": missing_rank_critical_fields,
                "summary": (
                    "current={current}; missing_sources={missing_sources}; missing_rank_critical={missing_fields}".format(
                        current=",".join(current_source_types) or "none",
                        missing_sources=",".join(missing_source_types) or "none",
                        missing_fields=",".join(missing_rank_critical_fields) or "none",
                    )
                ),
                "recommended_queries": _recommended_queries(
                    entity_name,
                    missing_source_types=missing_source_types,
                    missing_fields=missing_fields,
                    region=region,
                ),
            }
        )

    global_queries = [
        {
            "query": '"best public high school districts" chicago',
            "why": "Find candidate-discovery or context pages for the Chicago metro.",
        }
    ]
    if region == "illinois":
        global_queries.append(
            {
                "query": 'site:illinoisreportcard.com "high school district"',
                "why": "Find Illinois report-card pages for target districts.",
            }
        )

    priority_actions: list[str] = []
    missing_official_count = missing_source_type_counts.get("official_site", 0)
    if missing_official_count:
        priority_actions.append(
            "Capture official district sites for {count} district(s).".format(count=missing_official_count)
        )
    missing_state_count = missing_source_type_counts.get("state_report_card", 0)
    if missing_state_count:
        priority_actions.append(
            "Capture state report-card sources for {count} district(s).".format(count=missing_state_count)
        )
    if rank_critical_gap_entities:
        priority_actions.append(
            "Fill rank-critical metric gaps for: {entities}.".format(
                entities=", ".join(rank_critical_gap_entities)
            )
        )

    summary = {
        "source_count": schema_summary.get("source_count", 0),
        "entity_count": len(entities),
        "comparable_entity_count": comparable_entity_count,
        "source_mix_ready_count": source_mix_ready_count,
        "source_mix_gap_count": len(entities) - source_mix_ready_count,
        "entities_missing_official_site_count": missing_official_count,
        "entities_missing_state_report_card_count": missing_state_count,
        "entities_with_rank_critical_metric_gaps_count": len(rank_critical_gap_entities),
        "top_missing_source_types": [
            {"source_type": source_type, "count": count}
            for source_type, count in sorted(
                missing_source_type_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "top_missing_rank_critical_fields": [
            {"field": field, "count": count}
            for field, count in sorted(
                missing_field_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
    }

    return {
        "generated_at": now_iso(),
        "recipe": recipe,
        "task": manifest.get("task", ""),
        "region": region,
        "source_type_counts": schema_summary.get("source_type_counts", {}),
        "summary": summary,
        "priority_actions": priority_actions,
        "required_entity_source_types": required_entity_source_types,
        "source_requirements": DISTRICT_SOURCE_REQUIREMENTS,
        "entities": entity_rows,
        "global_queries": global_queries,
        "notes": [
            "Use this brief to improve browser-side source selection before trusting the ranking.",
            "Required entity source types should be captured before presenting a final district rank.",
        ],
    }


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
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
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _is_missing(value: Any) -> bool:
    return value in (None, "", []) or value != value


def _object_target_spec(task_spec: dict[str, Any], object_name: str) -> dict[str, Any]:
    for item in task_spec.get("target_objects", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")) == object_name:
            return item
    return {}


def _object_primary_key(object_name: str, rows: list[dict[str, Any]], task_spec: dict[str, Any]) -> list[str]:
    target = _object_target_spec(task_spec, object_name)
    keys = [str(key) for key in target.get("primary_key", []) if isinstance(key, str) and key]
    if keys:
        return keys
    columns = {key for row in rows[:20] for key in row.keys()}
    for candidate in (["item_id", "listing_url"], ["entity_name"], ["page_id"]):
        if all(key in columns for key in candidate):
            return candidate
    return []


def _object_grain(object_name: str, task_spec: dict[str, Any]) -> str:
    target = _object_target_spec(task_spec, object_name)
    if target.get("grain"):
        return str(target["grain"])
    if object_name in {"pages", "source_index"}:
        return "one captured page"
    if object_name in {"entities", "districts", "ranked_districts"}:
        return "one district"
    if object_name.startswith("listings"):
        return "one listing"
    if object_name == "capture_targets":
        return "one follow-up capture target"
    return "one structured row"


def _object_required_columns(object_name: str, task_spec: dict[str, Any]) -> list[str]:
    target = _object_target_spec(task_spec, object_name)
    columns = [str(column) for column in target.get("required_columns", []) if isinstance(column, str) and column]
    if columns:
        return columns
    if object_name == "ranked_districts":
        target = _object_target_spec(task_spec, "districts")
        return [str(column) for column in target.get("required_columns", []) if isinstance(column, str) and column]
    if object_name == "listings_top100":
        target = _object_target_spec(task_spec, "listings")
        return [str(column) for column in target.get("required_columns", []) if isinstance(column, str) and column]
    return []


def _object_role(object_name: str, task_spec: dict[str, Any]) -> str:
    target_names = set(_task_object_names(task_spec))
    if object_name in target_names:
        return "primary"
    if object_name in SUPPORT_OBJECT_NAMES:
        return "support"
    derived_aliases = {
        "districts": {"ranked_districts"},
        "listings": {"listings_top100"},
        "products": set(),
        "restaurant_chains": set(),
    }
    for target_name in target_names:
        if object_name in derived_aliases.get(target_name, set()):
            return "derived"
    return "support"


def _column_dtype(values: list[Any]) -> str:
    clean = [value for value in values if not _is_missing(value)]
    if not clean:
        return "unknown"
    if all(isinstance(value, bool) for value in clean):
        return "boolean"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in clean):
        return "integer"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in clean):
        return "number"
    if all(isinstance(value, list) for value in clean):
        return "array"
    if all(isinstance(value, dict) for value in clean):
        return "object"
    return "string"


def _object_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names: list[str] = []
    for row in rows[:50]:
        for key in row.keys():
            if key not in names:
                names.append(key)
    columns: list[dict[str, Any]] = []
    for name in names:
        values = [row.get(name) for row in rows[:200]]
        columns.append(
            {
                "name": name,
                "dtype": _column_dtype(values),
                "nullable": any(_is_missing(value) for value in values) or not values,
                "description": "",
            }
        )
    return columns


def _required_column_coverage(rows: list[dict[str, Any]], required_columns: list[str]) -> dict[str, float]:
    if not rows:
        return {column: 0.0 for column in required_columns}
    coverage: dict[str, float] = {}
    for column in required_columns:
        present = sum(1 for row in rows if not _is_missing(row.get(column)))
        coverage[column] = round(present / len(rows), 3)
    return coverage


def _duplicate_rate(rows: list[dict[str, Any]], primary_key: list[str]) -> float:
    if not rows or not primary_key:
        return 0.0
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    for row in rows:
        key = tuple(row.get(column) for column in primary_key)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
    return round(duplicates / len(rows), 3)


def _source_coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    source_page_ids: set[str] = set()
    for row in rows:
        page_id = row.get("source_page_id") or row.get("page_id")
        if isinstance(page_id, str) and page_id:
            source_page_ids.add(page_id)
        page_ids = row.get("source_page_ids")
        if isinstance(page_ids, list):
            for item in page_ids:
                if isinstance(item, str) and item:
                    source_page_ids.add(item)
    return {
        "page_count": len(source_page_ids),
    }


def _row_confidence_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("row_confidence", "")).strip()
        if label:
            counts[label] += 1
    return dict(sorted(counts.items()))


def _infer_target_age_months(task_spec: dict[str, Any]) -> Optional[int]:
    prompt = str(task_spec.get("user_prompt", ""))
    match = re.search(r"\b(\d+)\s*months?\s*old\b", prompt, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\s*month\b", prompt, re.I)
    if match:
        return int(match.group(1))
    for note in (task_spec.get("mission_overrides", {}) or {}).get("notes", []):
        text = str(note)
        match = re.search(r"target-age:(\d+)", text)
        if match:
            return int(match.group(1))
    return None


def _age_range_bounds_months(age_range_text: str) -> Optional[tuple[int, int]]:
    text = _normalize_space(age_range_text).lower()
    if not text:
        return None
    range_match = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)\s*(months?|years?)", text)
    if range_match:
        low = int(range_match.group(1))
        high = int(range_match.group(2))
        unit = range_match.group(3)
        if "year" in unit:
            low *= 12
            high *= 12
        return (low, high)
    plus_match = re.search(r"(\d+)\+?\s*(months?|years?)", text)
    if plus_match and "and up" in text:
        low = int(plus_match.group(1))
        if "year" in plus_match.group(2):
            low *= 12
        return (low, 999)
    old_match = re.search(r"(\d+)\s*(months?|years?)\s*old", text)
    if old_match:
        value = int(old_match.group(1))
        if "year" in old_match.group(2):
            value *= 12
        return (value, value)
    return None


def _product_quality_metrics(rows: list[dict[str, Any]], task_spec: dict[str, Any]) -> dict[str, Any]:
    if not rows:
        return {
            "retailer_count": 0,
            "direct_detail_url_fraction": 0.0,
            "mission_age_fit_fraction": 0.0,
            "suspicious_row_fraction": 0.0,
        }
    retailers = {
        str(row.get("retailer", "")).strip()
        for row in rows
        if str(row.get("retailer", "")).strip()
    }
    direct_detail_count = 0
    suspicious_count = 0
    target_age_months = _infer_target_age_months(task_spec)
    mission_age_fit_count = 0
    for row in rows:
        product_url = str(row.get("product_url", "")).strip()
        if product_url and "#product=" not in product_url and "/s?k=" not in product_url and "/search?" not in product_url:
            direct_detail_count += 1
        product_name = str(row.get("product_name", "")).strip()
        if _looks_like_junk_product_name(product_name):
            suspicious_count += 1
        if target_age_months is not None:
            bounds = _age_range_bounds_months(str(row.get("age_range_text", "")))
            if bounds and bounds[0] <= target_age_months <= bounds[1]:
                mission_age_fit_count += 1
    return {
        "retailer_count": len(retailers),
        "retailer_names": sorted(retailers),
        "direct_detail_url_fraction": round(direct_detail_count / len(rows), 3),
        "mission_age_fit_fraction": round(mission_age_fit_count / len(rows), 3) if target_age_months is not None else None,
        "suspicious_row_fraction": round(suspicious_count / len(rows), 3),
    }


def build_object_manifest(capsule_dir: Path, task_spec: dict[str, Any]) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    tables_dir = capsule_dir / "tables"
    provenance_dir = capsule_dir / "provenance"
    shape_dir = capsule_dir / "shape"
    if tables_dir.exists():
        for path in sorted(tables_dir.glob("*.jsonl")):
            rows = _read_jsonl_rows(path)
            object_name = path.stem
            primary_key = _object_primary_key(object_name, rows, task_spec)
            required_columns = _object_required_columns(object_name, task_spec)
            object_role = _object_role(object_name, task_spec)
            source_mix_ready_count = sum(1 for row in rows if bool(row.get("source_mix_complete")))
            provenance_path = provenance_dir / f"{object_name}.jsonl"
            shape_metadata = _read_json(shape_dir / f"{object_name}.json", {})
            quality = {
                "duplicate_rate": _duplicate_rate(rows, primary_key),
                "required_column_coverage": _required_column_coverage(rows, required_columns),
                "source_mix_ready_count": source_mix_ready_count,
                "source_mix_ready_fraction": round(source_mix_ready_count / len(rows), 3)
                if rows
                else 0.0,
                "row_confidence_counts": _row_confidence_counts(rows),
            }
            if object_name == "products":
                quality.update(_product_quality_metrics(rows, task_spec))
            objects.append(
                {
                    "name": object_name,
                    "object_version": 1,
                    "object_role": object_role,
                    "grain": _object_grain(object_name, task_spec),
                    "primary_key": primary_key,
                    "table_path": "tables/{name}".format(name=path.name),
                    "provenance_path": "provenance/{name}.jsonl".format(name=object_name)
                    if provenance_path.exists()
                    else "",
                    "row_count": len(rows),
                    "required_columns": required_columns,
                    "columns": _object_columns(rows),
                    "quality": quality,
                    "extractor": {
                        "name": str(shape_metadata.get("extractor_name", "")),
                        "version": int(shape_metadata.get("extractor_version", 0) or 0),
                    },
                    "source_coverage": _source_coverage(rows),
                }
            )
    objects.sort(
        key=lambda item: (
            {"primary": 0, "derived": 1, "support": 2}.get(str(item.get("object_role", "support")), 9),
            str(item.get("name", "")),
        )
    )
    return {
        "version": 1,
        "task_id": str(task_spec.get("task_id", "")),
        "generated_at": now_iso(),
        "objects": objects,
    }


def _find_manifest_object(object_manifest: dict[str, Any], object_name: str) -> Optional[dict[str, Any]]:
    objects = [item for item in object_manifest.get("objects", []) if isinstance(item, dict)]
    for item in objects:
        if str(item.get("name", "")) == object_name and str(item.get("object_role", "")) != "support":
            return item
    alias_map = {
        "districts": ["districts", "entities"],
        "listings": ["listings", "listings_top100"],
        "sources": ["source_index", "pages"],
    }
    for alias in alias_map.get(object_name, []):
        for item in objects:
            if str(item.get("name", "")) == alias and str(item.get("object_role", "")) != "support":
                return item
    return None


def _append_product_quality_rules(
    *,
    manifest_object: Optional[dict[str, Any]],
    rules: list[dict[str, Any]],
    blocked_actions: list[str],
    recommended_followups: list[str],
) -> bool:
    if not manifest_object:
        return False
    quality = dict(manifest_object.get("quality") or {})
    coverage = dict(quality.get("required_column_coverage") or {})
    all_passed = True

    dynamic_rules = [
        (
            "brand_coverage",
            float(coverage.get("brand", 0.0)),
            0.85,
            "Improve brand extraction coverage on `products`.",
        ),
        (
            "age_range_coverage",
            float(coverage.get("age_range_text", 0.0)),
            0.9,
            "Capture or extract clearer age guidance on `products`.",
        ),
        (
            "retailer_diversity_count",
            float(quality.get("retailer_count", 0.0) or 0.0),
            2.0,
            "Gather at least one additional retailer before calling the product set final-ready.",
        ),
        (
            "direct_detail_url_fraction",
            float(quality.get("direct_detail_url_fraction", 0.0) or 0.0),
            0.8,
            "Gather direct product detail pages instead of relying mostly on search-result pages.",
        ),
    ]
    mission_age_fit_fraction = quality.get("mission_age_fit_fraction")
    if mission_age_fit_fraction is not None:
        dynamic_rules.append(
            (
                "mission_age_fit_fraction",
                float(mission_age_fit_fraction),
                0.7,
                "Increase the share of products that explicitly fit the mission age range.",
            )
        )
    suspicious_row_fraction = float(quality.get("suspicious_row_fraction", 0.0) or 0.0)
    for rule_id, actual, expected, followup in dynamic_rules:
        passed = actual >= expected
        all_passed = all_passed and passed
        if not passed:
            recommended_followups.append(followup)
        rules.append(
            {
                "rule_id": rule_id,
                "passed": passed,
                "expected": expected,
                "actual": round(actual, 3),
            }
        )
    suspicious_passed = suspicious_row_fraction <= 0.03
    all_passed = all_passed and suspicious_passed
    if not suspicious_passed:
        blocked_actions.append("Reduce suspicious or off-mission product rows before calling the set final-ready.")
    rules.append(
        {
            "rule_id": "suspicious_row_fraction",
            "passed": suspicious_passed,
            "expected": 0.03,
            "actual": round(suspicious_row_fraction, 3),
        }
    )
    return all_passed


def build_readiness(
    task_spec: dict[str, Any],
    object_manifest: dict[str, Any],
    gather_qa: Optional[dict[str, Any]] = None,
    gather_qa_review: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target_specs = [
        item
        for item in task_spec.get("target_objects", [])
        if isinstance(item, dict) and item.get("name")
    ]
    object_rows: list[dict[str, Any]] = []
    blocked_actions: list[str] = []
    recommended_followups: list[str] = []
    any_rows = False
    all_rules_passed = True
    has_product_target = False

    for target in target_specs:
        object_name = str(target.get("name", ""))
        if object_name == "products":
            has_product_target = True
        manifest_object = _find_manifest_object(object_manifest, object_name)
        rules: list[dict[str, Any]] = []
        row_count = int(manifest_object.get("row_count", 0)) if manifest_object else 0
        any_rows = any_rows or row_count > 0
        column_names = {
            str(item.get("name", ""))
            for item in (manifest_object.get("columns", []) if manifest_object else [])
            if isinstance(item, dict)
        }
        coverage = (
            manifest_object.get("quality", {}).get("required_column_coverage", {})
            if manifest_object
            else {}
        )
        for rule in task_spec.get("stop_conditions", []):
            if not isinstance(rule, dict):
                continue
            if str(rule.get("object", "")) != object_name:
                continue
            rule_type = str(rule.get("type", ""))
            passed = False
            actual: Any = None
            if rule_type == "min_rows":
                actual = row_count
                passed = row_count >= int(rule.get("value", 0))
            elif rule_type == "min_captured_pages":
                source_object = _find_manifest_object(object_manifest, "pages")
                actual = int(source_object.get("row_count", 0)) if source_object else 0
                passed = actual >= int(rule.get("value", 0))
            elif rule_type == "required_columns_present":
                required = [str(column) for column in rule.get("columns", []) if isinstance(column, str)]
                actual = sorted(column_names)
                passed = all(column in column_names for column in required)
            elif rule_type == "required_column_coverage":
                column = str(rule.get("column", ""))
                actual = float(coverage.get(column, 0.0))
                passed = actual >= float(rule.get("min_fraction", 0.0))
            elif rule_type == "required_entity_source_types":
                actual = float(manifest_object.get("quality", {}).get("source_mix_ready_fraction", 0.0)) if manifest_object else 0.0
                passed = actual >= 1.0 and row_count > 0
            if not passed:
                all_rules_passed = False
                if rule_type == "min_rows":
                    blocked_actions.append(
                        "Capture more {name} rows to reach {value}.".format(
                            name=object_name,
                            value=rule.get("value", 0),
                        )
                    )
                elif rule_type == "required_column_coverage":
                    recommended_followups.append(
                        "Improve coverage for `{column}` on `{name}`.".format(
                            column=rule.get("column", ""),
                            name=object_name,
                        )
                    )
                elif rule_type == "required_entity_source_types":
                    recommended_followups.append(
                        "Capture missing required source types for `{name}`.".format(name=object_name)
                    )
            rules.append(
                {
                    "rule_id": rule_type,
                    "passed": passed,
                    "expected": rule.get("value", rule.get("min_fraction", rule.get("columns", []))),
                    "actual": actual,
                }
            )
        if object_name == "products":
            product_rules_passed = _append_product_quality_rules(
                manifest_object=manifest_object,
                rules=rules,
                blocked_actions=blocked_actions,
                recommended_followups=recommended_followups,
            )
            if not product_rules_passed:
                all_rules_passed = False
        object_rows.append(
            {
                "name": object_name,
                "status": (
                    "final_ready"
                    if rules and all(item.get("passed") for item in rules)
                    else "exploratory_ready" if row_count > 0 else "blocked"
                ),
                "row_count": row_count,
                "rules": rules,
            }
        )

    capture_qa_summary = summarize_gather_qa(gather_qa or {}, gather_qa_review or {})
    reviewed_page_count = int(capture_qa_summary.get("reviewed_page_count", 0) or 0)
    accepted_like_fraction = float(capture_qa_summary.get("accepted_like_fraction", 0.0) or 0.0)
    capture_status_counts = {
        str(key): int(value)
        for key, value in dict(capture_qa_summary.get("effective_status_counts") or {}).items()
        if str(key)
    }
    if reviewed_page_count:
        if capture_status_counts.get("blocked", 0):
            recommended_followups.append("Re-gather pages that hit access or login blocks.")
        if capture_status_counts.get("redirect", 0):
            recommended_followups.append("Redirect gather targets onto direct detail pages instead of search results.")
        if capture_status_counts.get("retry", 0):
            recommended_followups.append("Retry short or incomplete gather pages before trusting final conclusions.")
        required_capture_qa_fraction = 0.85 if has_product_target else 0.75
        if accepted_like_fraction < required_capture_qa_fraction:
            all_rules_passed = False
            blocked_actions.append(
                "Improve Gather QA acceptance before treating the object set as final-ready."
            )

    if all_rules_passed and object_rows and all(item.get("rules") for item in object_rows):
        overall_status = "final_ready"
    elif any_rows:
        overall_status = "exploratory_ready"
    else:
        overall_status = "blocked"

    return {
        "version": 1,
        "task_id": str(task_spec.get("task_id", "")),
        "generated_at": now_iso(),
        "overall_status": overall_status,
        "objects": object_rows,
        "capture_qa": {
            "reviewed_page_count": reviewed_page_count,
            "agent_reviewed_page_count": int(capture_qa_summary.get("agent_reviewed_page_count", 0) or 0),
            "accepted_like_fraction": accepted_like_fraction,
            "status_counts": capture_status_counts,
        },
        "blocked_actions": blocked_actions,
        "recommended_followups": recommended_followups,
    }


def render_analysis(capsule_dir: Path, analysis_plan: dict[str, Any]) -> str:
    task_type = analysis_plan.get("task_type", RECIPE_GENERIC)
    template_renderers = {
        "load_capsule": _render_load_capsule,
        "inspect_sources": _render_inspect_sources,
        "validate_comparability": _render_validate_comparability,
        "search_terms": _render_search_terms,
        "extract_district_metrics": _render_extract_district_metrics,
        "compare_entities": _render_compare_entities,
        "queue_followups": _render_queue_followups,
    }

    blocks: list[str] = []
    for cell in analysis_plan.get("cells", []):
        template = cell.get("template", "")
        renderer = template_renderers.get(template)
        if renderer is None:
            continue
        if task_type != RECIPE_HIGHSCHOOL and template in (
            "validate_comparability",
            "extract_district_metrics",
            "compare_entities",
        ):
            continue
        blocks.append(renderer(capsule_dir, cell.get("title", template)))
    return "\n\n".join(blocks).rstrip() + "\n"


def _render_load_capsule(capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import load_analysis_context",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "capsule = context['capsule']",
        "analysis_plan = context['analysis_plan']",
        "SOURCE_DF = context['source_df']",
        "ENTITY_DF = context['entity_df']",
        "SOURCE_ROWS = context['source_rows']",
        "ENTITY_ROWS = context['entity_rows']",
        "RANKING_POLICY = context['ranking_policy']",
        "summary = context['summary']",
        "",
        "print(summary)",
        "print({",
        "    'task_type': analysis_plan.get('task_type'),",
        "    'question_count': len(analysis_plan.get('questions', [])),",
        "    'source_count': len(SOURCE_DF),",
        "    'entity_count': len(ENTITY_DF),",
        "    'source_columns': list(SOURCE_DF.columns),",
        "    'entity_columns': list(ENTITY_DF.columns),",
        "    'ranking_policy': RANKING_POLICY,",
        "})",
    ]
    return "\n".join(lines)


def _render_inspect_sources(_capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import load_analysis_context",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=_capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "SOURCE_DF = context['source_df']",
        "INSPECT_COLUMNS = ['page_id', 'entity_name', 'source_type', 'comparability_flag', 'grades_served', 'present_metric_fields', 'title']",
        "print(SOURCE_DF.loc[:, [column for column in INSPECT_COLUMNS if column in SOURCE_DF.columns]])",
    ]
    return "\n".join(lines)


def _render_validate_comparability(_capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import load_analysis_context",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=_capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "ENTITY_DF = context['entity_df']",
        "COMPARABILITY_COLUMNS = ['entity_name', 'comparability_flag', 'source_types', 'fields_present']",
        "COMPARABILITY_DF = ENTITY_DF.loc[:, [column for column in COMPARABILITY_COLUMNS if column in ENTITY_DF.columns]]",
        "COMPARABILITY_ISSUES_DF = ENTITY_DF.loc[ENTITY_DF['comparability_flag'] != 'high_school_only', ['entity_name']].copy()",
        "if not COMPARABILITY_ISSUES_DF.empty:",
        "    COMPARABILITY_ISSUES_DF['issue'] = 'Entity is not confirmed as a comparable high-school-only district.'",
        "print(COMPARABILITY_DF)",
        "print(COMPARABILITY_ISSUES_DF)",
    ]
    return "\n".join(lines)


def _render_search_terms(_capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import load_analysis_context",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=_capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "analysis_plan = context['analysis_plan']",
        "SOURCE_DF = context['source_df'].copy()",
        "keywords = list(analysis_plan.get('keywords', []))",
        "SOURCE_DF['hits'] = SOURCE_DF['excerpt'].fillna('').astype(str).str.lower().map(",
        "    lambda excerpt: [keyword for keyword in keywords if keyword.lower() in ' '.join(excerpt.split())]",
        ")",
        "KEYWORD_COLUMNS = ['page_id', 'entity_name', 'source_type', 'hits']",
        "print(SOURCE_DF.loc[:, KEYWORD_COLUMNS])",
    ]
    return "\n".join(lines)


def _render_extract_district_metrics(_capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import build_district_metrics, load_analysis_context, to_dataframe",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=_capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "DISTRICT_METRICS_DF = to_dataframe(build_district_metrics(context['entity_rows'], context['source_rows']))",
        "print(DISTRICT_METRICS_DF)",
    ]
    return "\n".join(lines)


def _render_compare_entities(_capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import build_district_metrics, load_analysis_context, to_dataframe",
        "from unchained_pyreplab.scoring import score_highschool_districts",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=_capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "analysis_plan = context['analysis_plan']",
        "DISTRICT_METRICS = build_district_metrics(context['entity_rows'], context['source_rows'])",
        "RANKED_DISTRICTS_DF = to_dataframe(score_highschool_districts(DISTRICT_METRICS, analysis_plan))",
        "print(RANKED_DISTRICTS_DF)",
    ]
    return "\n".join(lines)


def _render_queue_followups(_capsule_dir: Path, title: str) -> str:
    lines = [
        "# %% {title}".format(title=title),
        "from pathlib import Path",
        "",
        "from unchained_pyreplab.analysis_runtime import build_district_metrics, load_analysis_context",
        "from unchained_pyreplab.scoring import score_highschool_districts",
        "",
        'CAPSULE_DIR = Path(r"{path}")'.format(path=_capsule_dir.as_posix()),
        "context = load_analysis_context(CAPSULE_DIR)",
        "analysis_plan = context['analysis_plan']",
        "ENTITY_ROWS = context['entity_rows']",
        "RANKED_DISTRICTS = score_highschool_districts(",
        "    build_district_metrics(context['entity_rows'], context['source_rows']),",
        "    analysis_plan,",
        ")",
        "FOLLOWUP_SUGGESTIONS = []",
        "followup_keys = set()",
        "important_fields = [",
        "    ('grades_served', 'confirm the served grade band'),",
        "    ('student_teacher_ratio', 'capture the exact student-teacher ratio'),",
        "    ('math_proficiency_pct', 'capture the math proficiency percentage'),",
        "    ('reading_proficiency_pct', 'capture the reading proficiency percentage'),",
        "    ('graduation_rate_pct', 'capture the graduation rate'),",
        "    ('average_sat', 'capture the average SAT score'),",
        "    ('average_act', 'capture the average ACT score'),",
        "]",
        "important_field_prompts = {field: prompt for field, prompt in important_fields}",
        "ranked_by_entity = {row.get('entity_name', ''): row for row in RANKED_DISTRICTS}",
        "",
        "def queue_followup(entity_name, page_id, url, instruction):",
        "    key = (entity_name, instruction)",
        "    if key in followup_keys:",
        "        return",
        "    followup_keys.add(key)",
        "    FOLLOWUP_SUGGESTIONS.append({",
        "        'entity_name': entity_name,",
        "        'page_id': page_id,",
        "        'url': url,",
        "        'instruction': instruction,",
        "    })",
        "",
        "for entity in ENTITY_ROWS:",
        "    primary_source = entity.get('primary_source', {})",
        "    ranked = ranked_by_entity.get(entity.get('entity_name', ''), {})",
        "    if entity.get('comparability_flag') != 'high_school_only':",
        "        queue_followup(",
        "            entity.get('entity_name', ''),",
        "            primary_source.get('page_id', ''),",
        "            primary_source.get('final_url', ''),",
        "            'Confirm whether this district is truly high-school-only and capture the exact grade-band text.',",
        "        )",
        "    missing_critical_fields = list(ranked.get('missing_critical_fields', []))",
        "    if missing_critical_fields:",
        "        prompts = [important_field_prompts.get(field, 'capture the missing field') for field in missing_critical_fields]",
        "        queue_followup(",
        "            entity.get('entity_name', ''),",
        "            primary_source.get('page_id', ''),",
        "            primary_source.get('final_url', ''),",
        "            '{entity}: capture the missing rank-critical metrics: {prompts}.'.format(",
        "                entity=entity.get('entity_name', ''),",
        "                prompts='; '.join(prompts),",
        "            ),",
        "        )",
        "    for field, prompt in important_fields:",
        "        if field not in entity.get('fields_present', []) and field not in missing_critical_fields:",
        "            queue_followup(",
        "                entity.get('entity_name', ''),",
        "                primary_source.get('page_id', ''),",
        "                primary_source.get('final_url', ''),",
        "                '{entity}: {prompt}.'.format(entity=entity.get('entity_name', ''), prompt=prompt),",
        "            )",
        "    if 'official_site' not in entity.get('source_types', []):",
        "        queue_followup(",
        "            entity.get('entity_name', ''),",
        "            primary_source.get('page_id', ''),",
        "            primary_source.get('final_url', ''),",
        "            '{entity}: find an official district source to corroborate the key metrics.'.format(entity=entity.get('entity_name', '')),",
        "        )",
        "    if 'state_report_card' not in entity.get('source_types', []):",
        "        queue_followup(",
        "            entity.get('entity_name', ''),",
        "            primary_source.get('page_id', ''),",
        "            primary_source.get('final_url', ''),",
        "            '{entity}: find a state report-card or state accountability source for the quantitative metrics.'.format(entity=entity.get('entity_name', '')),",
        "        )",
        "print(FOLLOWUP_SUGGESTIONS[:12])",
        "# Example queue command:",
        "# if FOLLOWUP_SUGGESTIONS:",
        "#     first = FOLLOWUP_SUGGESTIONS[0]",
        "#     capsule.request_followup(url=first['url'], page_id=first['page_id'], instruction=first['instruction'])",
    ]
    return "\n".join(lines)
