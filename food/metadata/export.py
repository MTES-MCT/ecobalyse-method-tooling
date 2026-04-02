#!/usr/bin/env python3
"""
Export predicted ingredients to CSV and activities.json format.

Usage:
    python export.py metadata --variant FR     # Export FR variant
    python export.py metadata --variant ORG    # Export organic variant
    python export.py final_data                # Generate final CSV with impacts
    python export.py metadata --variant FR --add-old-suffix     # Add (2025) suffix to existing
    python export.py remove-old                                # Remove suffixed entries entirely

Variants: FR, ORG, UE, OI, NUE

Outputs:
    - generated/predictions.csv: CSV with all predictions and confidence scores
    - generated/new_activities.json: Activities format for Ecobalyse
"""

import argparse
import csv
import json
import os
import re
import shutil
import uuid
from enum import Enum
from pathlib import Path

import inflect

# Namespace UUID for deterministic UUID generation (generated once, never changes)
ECOBALYSE_NAMESPACE = uuid.UUID("a4e1d123-5c67-4b89-9def-1234567890ab")

_inflect_engine = inflect.engine()

PROCESSING_QUALIFIERS = {
    "dried", "fresh", "raw", "frozen", "smoked", "canned", "cooked", "uncooked",
    "roasted", "ground", "blanched", "peeled", "shelled", "dehusked", "pickled",
    "fermented", "salted", "dehydrated", "pasteurized", "refined", "whole",
    "concentrated",
}

PLURAL_EXCEPTIONS = {"oats", "french-fries"}


class Variant(Enum):
    FR = "FR"
    BIO = "BIO"
    UE = "UE"
    OI = "OI"
    NUE = "NUE"


VARIANT_SUFFIX = {
    Variant.FR: " FR",
    Variant.BIO: " Bio",
    Variant.UE: " UE",
    Variant.OI: " Origine Inconnue",
    Variant.NUE: " HORS UE",
}

VARIANT_ALIAS_SUFFIX = {
    Variant.FR: "-fr",
    Variant.BIO: "-organic",
    Variant.UE: "-eu",
    Variant.OI: "-default",
    Variant.NUE: "-non-eu",
}

VARIANT_SCENARIO = {
    Variant.FR: "reference",
    Variant.BIO: "organic",
    Variant.UE: "import",
    Variant.OI: "import",
    Variant.NUE: "import",
}

VARIANT_ORIGIN = {
    Variant.FR: "France",
    Variant.BIO: "France",
    Variant.UE: "EuropeAndMaghreb",
    Variant.OI: "OutOfEuropeAndMaghreb",
    Variant.NUE: "OutOfEuropeAndMaghreb",
}

from dotenv import load_dotenv

load_dotenv()

import bw2data
import pandas as pd
from rich.progress import track

from predict import Predictor

bw2data.projects.set_current("ecobalyse")

# =============================================================================
# ANIMAL DETECTION (from reference/animal.csv)
# =============================================================================

REFERENCE_DIR = Path(__file__).parent / "reference"


def _load_animal_data() -> list[dict]:
    """Load reference/animal.csv into a list of dicts with a compiled regex."""
    rows = []
    with open(REFERENCE_DIR / "animal.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "pattern": re.compile(r"\b" + re.escape(row["name"]) + r"\b", re.IGNORECASE),
                "animalGroup1": row["animalGroup1"],
                "animalGroup2": row["animalGroup2"],
                "animalProduct": row["animalProduct"],
            })
    return rows


ANIMAL_ENTRIES = _load_animal_data()


def detect_animal_fields(name: str, activity_name: str) -> dict:
    """Detect animalGroup1, animalGroup2, animalProduct from reference CSV."""
    text = f"{name} {activity_name}"
    for entry in ANIMAL_ENTRIES:
        if entry["pattern"].search(text):
            return {
                "animalGroup1": entry["animalGroup1"],
                "animalGroup2": entry["animalGroup2"],
                "animalProduct": entry["animalProduct"],
            }
    return {}


# =============================================================================
# HELPERS
# =============================================================================


def generate_alias(name: str) -> str:
    """Generate alias from English name.

    Singularizes base words, moves processing qualifiers to the end.
    """
    alias = name.lower()
    alias = re.sub(r"[\s_]+", "-", alias)
    alias = re.sub(r"[^a-z0-9-]", "", alias)
    alias = re.sub(r"-+", "-", alias)
    alias = alias.strip("-")

    if alias in PLURAL_EXCEPTIONS:
        return alias

    words = alias.split("-")

    # Separate processing qualifiers from base words (order-preserving)
    base_words = []
    qualifier_words = []
    for w in words:
        if w in PROCESSING_QUALIFIERS:
            qualifier_words.append(w)
        else:
            base_words.append(w)

    # Singularize base words
    singularized = []
    for w in base_words:
        singular = _inflect_engine.singular_noun(w)
        singularized.append(singular if singular else w)

    return "-".join(singularized + qualifier_words)


def _format_match(match_info: dict | None) -> str:
    """Format match rule for CSV output."""
    if match_info is None:
        return ""
    return match_info.get("rule", "")


def _format_conf(match_info: dict | None) -> str:
    """Format confidence from match info for CSV output."""
    if match_info is None:
        return ""
    conf = match_info.get("confidence")
    return f"{conf:.3f}" if conf else ""


def get_db_unit(activity_name, location=""):
    """Return (unit, db_name, location_or_empty).

    The location is only returned when it was needed to disambiguate
    multiple activities with the same name (e.g. WFLDB).
    """
    dbs = ("Agribalyse 3.2", "Ecoinvent 3.9.1", "Ecoinvent 3.11", "WFLDB", "Ecobalyse", "Ginko 2025")
    for db in dbs:
        activities = [a for a in bw2data.Database(db) if a["name"] == activity_name]
        if len(activities) == 1:
            return activities[0]["unit"], db, ""
        if len(activities) > 1 and location:
            filtered = [a for a in activities if a.get("location") == location]
            if len(filtered) >= 1:
                return filtered[0]["unit"], db, location
    raise Exception(f"Not found in {str(dbs)}: {activity_name}")


def fix_unit(unit):
    return {"kilogram": "kg", "unit": "item", "litre": "L"}[unit]


# =============================================================================
# PREDICTION
# =============================================================================


def predict_all(predictor: Predictor, input_df: pd.DataFrame, variant: Variant) -> list:
    """
    Predict metadata for all ingredients in the DataFrame.

    Returns list of dicts with: name, french_name, activity_name, source, predictions, variant
    """
    results = []

    for _, row in track(
        input_df.iterrows(), total=len(input_df), description="Predicting..."
    ):
        name = str(row["item"]).strip()
        french_name = (
            str(row["item trad"]).strip()
            if pd.notna(row.get("item trad"))
            else ""
        )
        activity_name = (
            str(row["icv final"]).strip() if pd.notna(row["icv final"]) else ""
        )
        csv_location = str(row.get("location", "")).strip() if pd.notna(row.get("location")) else ""
        unit, source, location = get_db_unit(activity_name, csv_location)

        if not name or not activity_name:
            continue

        # Extract production location for FR variant handling
        production_fr = str(row.get("Production_FR", "")).strip()

        ingredient = {"name": name, "activityName": activity_name}
        predictions = predictor.predict(ingredient)

        visible = str(row.get("visible", "TRUE")).strip().upper() == "TRUE"

        results.append({
            "name": name,
            "french_name": french_name,
            "activity_name": activity_name,
            "source": source,
            "unit": fix_unit(unit),
            "predictions": predictions,
            "variant": variant,
            "production_fr": production_fr,
            "location": location,
            "visible": visible,
        })

    return results


# =============================================================================
# CSV OUTPUT
# =============================================================================


def write_csv(results: list, output_path: str):
    """Write predictions to CSV file."""
    fieldnames = [
        "name",
        "categories",
        "foodType",
        "foodTypeMatch",
        "foodTypeConf",
        "novaGroup",
        "novaGroupMatch",
        "novaGroupConf",
        "processingState",
        "processingStateMatch",
        "processingStateConf",
        "packaging",
        "packagingMatch",
        "transportCooling",
        "transportCoolingMatch",
        "cropGroup",
        "cropGroupMatch",
        "cropGroupConf",
        "density",
        "densityMatch",
        "densityConf",
        "inediblePart",
        "inediblePartMatch",
        "inediblePartConf",
        "rawToCookedRatio",
        "rawToCookedRatioMatch",
        "rawToCookedRatioConf",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in results:
            pred = r["predictions"]
            categories = pred.get("categories", [])

            writer.writerow({
                "name": r["name"],
                "categories": ",".join(categories) if categories else "",
                "foodType": pred.get("foodType", ""),
                "foodTypeMatch": _format_match(pred.get("foodTypeMatch")),
                "foodTypeConf": _format_conf(pred.get("foodTypeMatch")),
                "novaGroup": pred.get("novaGroup", ""),
                "novaGroupMatch": _format_match(pred.get("novaGroupMatch")),
                "novaGroupConf": _format_conf(pred.get("novaGroupMatch")),
                "processingState": pred.get("processingState", ""),
                "processingStateMatch": _format_match(pred.get("processingStateMatch")),
                "processingStateConf": _format_conf(pred.get("processingStateMatch")),
                "packaging": pred.get("packaging") or "",
                "packagingMatch": _format_match(pred.get("packagingMatch")),
                "transportCooling": pred.get("transportCooling", ""),
                "transportCoolingMatch": _format_match(
                    pred.get("transportCoolingMatch")
                ),
                "cropGroup": pred.get("cropGroup") or "",
                "cropGroupMatch": _format_match(pred.get("cropGroupMatch")),
                "cropGroupConf": _format_conf(pred.get("cropGroupMatch")),
                "density": f"{pred.get('density', 0):.3f}",
                "densityMatch": _format_match(pred.get("densityMatch")),
                "densityConf": _format_conf(pred.get("densityMatch")),
                "inediblePart": f"{pred.get('inediblePart', 0):.2f}",
                "inediblePartMatch": _format_match(pred.get("inediblePartMatch")),
                "inediblePartConf": _format_conf(pred.get("inediblePartMatch")),
                "rawToCookedRatio": f"{pred.get('rawToCookedRatio', 0):.3f}",
                "rawToCookedRatioMatch": _format_match(
                    pred.get("rawToCookedRatioMatch")
                ),
                "rawToCookedRatioConf": _format_conf(pred.get("rawToCookedRatioMatch")),
            })

    print(f"CSV written to {output_path}")


# =============================================================================
# JSON OUTPUT (activities.json format)
# =============================================================================


def build_activity_entry(
    name: str,
    french_name: str,
    activity_name: str,
    source: str,
    unit: str,
    predictions: dict,
    variant: Variant,
    production_fr: str = "",
    location: str = "",
    visible: bool = True,
) -> dict:
    """Build an activity entry in the activities.json format."""
    # Determine suffix based on variant and production location
    if variant == Variant.FR and production_fr == "DOM":
        variant_suffix = " FR Outre-Mer"
        alias_suffix = "-fr-overseas"
    else:
        variant_suffix = VARIANT_SUFFIX[variant]
        alias_suffix = VARIANT_ALIAS_SUFFIX[variant]

    # Alias from English name + variant suffix (lowercase)
    alias = generate_alias(name) + alias_suffix

    # DisplayName from French name + variant suffix
    display_name = (french_name if french_name else name) + variant_suffix

    # Generate deterministic UUIDs based on displayName
    # This ensures each unique displayName gets a unique UUID
    activity_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"activity:{display_name}"))
    ingredient_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"ingredient:{display_name}"))

    # Scenario from variant
    scenario = VARIANT_SCENARIO[variant]

    ingredient = {
        "alias": alias,
        "defaultOrigin": VARIANT_ORIGIN[variant],
        "displayName": display_name,
        "id": ingredient_id,
        "inediblePart": predictions.get("inediblePart", 0),
        "inediblePartMatch": predictions.get("inediblePartMatch"),
        "ingredientCategories": predictions.get("categories", ["misc"])
            + (["organic"] if variant == Variant.BIO else []),
        "ingredientDensity": predictions.get("density", 1.0),
        "ingredientDensityMatch": predictions.get("densityMatch"),
        "rawToCookedRatio": predictions.get("rawToCookedRatio", 1.0),
        "rawToCookedRatioMatch": predictions.get("rawToCookedRatioMatch"),
        "scenario": scenario,
        "transportCooling": predictions.get("transportCooling", "none"),
        "transportCoolingMatch": predictions.get("transportCoolingMatch"),
        "visible": visible,
    }

    if predictions.get("cropGroup"):
        ingredient["cropGroup"] = predictions["cropGroup"]
        ingredient["cropGroupMatch"] = predictions.get("cropGroupMatch")

    animal_fields = detect_animal_fields(name, activity_name)
    if animal_fields:
        ingredient["animalGroup1"] = animal_fields["animalGroup1"]
        ingredient["animalGroup2"] = animal_fields["animalGroup2"]
        ingredient["animalProduct"] = animal_fields["animalProduct"]

    entry = {
        "activityName": activity_name,
        "alias": alias,
        "categories": ["ingredient"],
        "displayName": display_name,
        "id": activity_id,
        "metadata": [{**ingredient, "scopes": ["food"]}],
        "scopes": ["food"],
        "source": source,
        "unit": unit,
    }
    if location:
        entry["location"] = location
    return entry


def write_json(results: list, output_path: str):
    """Write activities to JSON file."""
    activities = []
    for r in results:
        activity = build_activity_entry(
            r["name"],
            r["french_name"],
            r["activity_name"],
            r["source"],
            r["unit"],
            r["predictions"],
            r["variant"],
            r.get("production_fr", ""),
            r.get("location", ""),
            r.get("visible", True),
        )
        activities.append(activity)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)

    print(f"JSON written to {output_path}")


# =============================================================================
# CLI
# =============================================================================

INPUT_CSV_DIR = Path(__file__).parent / "source"
GENERATED_DIR = Path(__file__).parent / "generated"


def get_input_csv(variant: Variant) -> Path:
    return INPUT_CSV_DIR / f"new_ingredient_{variant.value}.csv"


def get_output_paths(variant: Variant) -> tuple[Path, Path, Path]:
    """Generate output paths with variant suffix."""
    suffix = f"_{variant.value}"
    return (
        GENERATED_DIR / f"predictions{suffix}.csv",
        GENERATED_DIR / f"new_activities{suffix}.json",
        GENERATED_DIR / f"fichier_final{suffix}.csv",
    )

# Impact columns to extract from processes_impacts.json
IMPACT_COLUMNS = [
    "acd",
    "cch",
    "etf-c",
    "fru",
    "fwe",
    "htc-c",
    "htn-c",
    "ior",
    "ldu",
    "mru",
    "ozd",
    "pco",
    "pma",
    "swe",
    "tre",
    "wtu",
    "ecs",
]

OLD_DISPLAY_SUFFIX = " (2025)"
OLD_ALIAS_SUFFIX = "-2025"

ECOSYSTEMIC_SERVICES_MULTIPLIERS = {
    "cropDiversity": -1.5,
    "hedges": -3,
    "livestockDensity": 3000,
    "permanentPasture": -7,
    "plotSize": -4,
}


def normalize_display_name(name: str, old_suffix: str) -> str:
    if not old_suffix:
        return name
    return name[: -len(old_suffix)] if name.endswith(old_suffix) else name


def normalize_alias(alias: str | None, old_suffix: str) -> str | None:
    if alias is None:
        return None
    if not old_suffix:
        return alias
    return alias[: -len(old_suffix)] if alias.endswith(old_suffix) else alias


def extract_activities_and_ingredients(
    activities_list: list[dict],
    old_display_suffix: str,
    old_alias_suffix: str,
) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    """Extract and normalize flat dicts from nested activities.json.
    Keyed by displayName. Consolidates by activityName. Last wins.
    """
    activities, ingredients, other = {}, {}, []
    by_activity_name = {}  # activityName -> activity displayName

    for a in activities_list:
        if "displayName" not in a:
            other.append(a)
            continue
        act_name = a.get("activityName")
        act_display = normalize_display_name(a["displayName"], old_display_suffix)

        if act_name and act_name in by_activity_name:
            act_display = by_activity_name[act_name]
        else:
            if act_name:
                by_activity_name[act_name] = act_display
            # Preserve non-food metadata (textile, etc.) on the activity dict
            meta_list = a.get("metadata", [])
            non_food_meta = [m for m in meta_list if "food" not in m.get("scopes", [])]
            activities[act_display] = {k: v for k, v in a.items() if k != "metadata"}
            if non_food_meta:
                activities[act_display]["_non_food_metadata"] = non_food_meta
            activities[act_display]["displayName"] = act_display
            activities[act_display]["alias"] = normalize_alias(
                a.get("alias", ""), old_alias_suffix
            )

        for ing in (m for m in a.get("metadata", []) if "food" in m.get("scopes", [])):
            ing = {**ing, "activity_display": act_display}
            ing["displayName"] = normalize_display_name(ing["displayName"], old_display_suffix)
            ing["alias"] = normalize_alias(ing.get("alias", ""), old_alias_suffix)
            ingredients[ing["displayName"]] = ing

    return activities, ingredients, other


def apply_suffixes(
    activities: dict[str, dict],
    ingredients: dict[str, dict],
    new_act_names: set[str],
    new_ing_names: set[str],
    kept_act_names: set[str],
    keep_set: set[str],
    old_display_suffix: str,
    old_alias_suffix: str,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """Add old_display_suffix and old_alias_suffix to old activity/ingredient names.

    - Old activities get old_display_suffix on their displayName and old_alias_suffix
      on their alias, unless they are in kept_act_names (existing activities reused
      by new ingredients).
    - Old ingredients get old_display_suffix on their displayName and old_alias_suffix
      on their alias, unless in keep_set.
    - New activities/ingredients keep their clean names (no suffix).

    Returns (activities, ingredients, alias_renames) where alias_renames maps
    old_alias -> new_alias for every activity alias that was suffixed.
    """
    new_acts = {}
    for dn, act in activities.items():
        act = {**act}
        if dn not in new_act_names:
            # OLD activity
            if dn not in kept_act_names and "ingredient" in act.get("categories", []):
                act["displayName"] = act["displayName"] + old_display_suffix
                if act.get("alias"):
                    act["alias"] = act["alias"] + old_alias_suffix
        new_acts[dn] = act

    new_ings = {}
    ing_alias_renames = {}
    for dn, ing in ingredients.items():
        ing = {**ing}
        if dn not in new_ing_names:
            # OLD ingredient
            if dn not in keep_set:
                ing["displayName"] += old_display_suffix
                if ing.get("alias"):
                    old_ing_alias = ing["alias"]
                    ing["alias"] = old_ing_alias + old_alias_suffix
                    ing_alias_renames[old_ing_alias] = ing["alias"]
        new_ings[dn] = ing
    return new_acts, new_ings, ing_alias_renames


def reassemble(
    activities: dict[str, dict],
    ingredients: dict[str, dict],
    other: list[dict],
) -> list[dict]:
    """Reassemble flat dicts back into nested activities.json format."""
    by_activity = {}
    for ing in ingredients.values():
        ad = ing["activity_display"]
        by_activity.setdefault(ad, []).append(
            {k: v for k, v in ing.items() if k != "activity_display"}
        )

    result = []
    for dn, act in activities.items():
        entry = {**act}
        non_food_meta = entry.pop("_non_food_metadata", [])
        ings = by_activity.get(dn, [])
        food_ings = [{**ing, "scopes": ing.get("scopes", ["food"])} for ing in ings]
        all_meta = non_food_meta + food_ings
        if all_meta:
            entry["metadata"] = all_meta
        elif entry.get("categories") == ["ingredient"]:
            continue  # Skip orphaned ingredient activities with no metadata
        result.append(entry)
    return result + other


def merge_activities(
    new_activities_path: Path,
    target_activities_path: Path,
    add_old_suffix: bool = False,
):
    """Merge new_activities.json into target activities.json.

    Uses flat dicts keyed by UUID for activities and ingredients.
    Normalizes on load (strips previous merge artifacts), then merges
    with new overriding existing, and optionally applies suffixes.

    Options:
    - add_old_suffix: Add " (2025)" suffix to pre-existing ingredient displayNames
      and "-2025" suffix to their aliases
    """
    with open(new_activities_path) as f:
        new_list = json.load(f)
    with open(target_activities_path) as f:
        existing_list = json.load(f)

    keep_csv_path = Path(__file__).parent / "source/keep.csv"
    keep_set = set()
    if keep_csv_path.exists():
        with open(keep_csv_path, encoding="utf-8") as f:
            keep_set = {line.strip() for line in f if line.strip()}

    # Only strip old suffixes when we intend to re-apply them
    strip_display = OLD_DISPLAY_SUFFIX if add_old_suffix else ""
    strip_alias = OLD_ALIAS_SUFFIX if add_old_suffix else ""

    # Extract into flat dicts (normalizing on load)
    existing_acts, existing_ings, other = extract_activities_and_ingredients(
        existing_list, strip_display, strip_alias
    )
    new_acts, new_ings, _ = extract_activities_and_ingredients(
        new_list, strip_display, strip_alias
    )

    # Build activityName -> displayName map from existing
    existing_act_by_name = {
        act["activityName"]: dn
        for dn, act in existing_acts.items()
        if "activityName" in act
    }

    # For new activities: only add if activityName is genuinely new.
    # For new ingredients mapping to existing activityNames: remap activity_display.
    added_acts = {}
    for dn, act in new_acts.items():
        act_name = act.get("activityName")
        if act_name not in existing_act_by_name:
            added_acts[dn] = act
            existing_act_by_name[act_name] = dn
        else:
            # Update existing activity with fields from new export (location, source, etc.)
            existing_dn = existing_act_by_name[act_name]
            for key in ("location", "source"):
                if key in act:
                    existing_acts[existing_dn][key] = act[key]
                elif key in existing_acts[existing_dn]:
                    del existing_acts[existing_dn][key]

    # Remap new ingredients to existing activity displayNames where applicable
    for ing_dn, ing in new_ings.items():
        act_display = ing["activity_display"]
        # Find the activityName for this ingredient's activity
        source_act = new_acts.get(act_display)
        if source_act:
            act_name = source_act["activityName"]
            existing_dn = existing_act_by_name.get(act_name)
            if existing_dn and existing_dn != act_display:
                ing["activity_display"] = existing_dn

    # Existing activities whose activityName is shared with a new activity
    # are "kept" (they hold new ingredients and will survive the future release).
    kept_act_names = set()
    for dn, act in new_acts.items():
        act_name = act.get("activityName")
        if act_name and act_name in existing_act_by_name:
            kept_act_names.add(existing_act_by_name[act_name])

    merged_acts = {**existing_acts, **added_acts}
    # New ingredients override existing on displayName collision (allows re-exporting updates)
    # but preserve existing UUIDs
    merged_ings = {**existing_ings}
    for dn, ing in new_ings.items():
        if dn in existing_ings and "id" in existing_ings[dn]:
            ing = {**ing, "id": existing_ings[dn]["id"]}
        merged_ings[dn] = ing

    # Apply suffix logic
    alias_renames = {}
    if add_old_suffix:
        merged_acts, merged_ings, alias_renames = apply_suffixes(
            merged_acts,
            merged_ings,
            set(added_acts),
            set(new_ings),
            kept_act_names,
            keep_set,
            OLD_DISPLAY_SUFFIX,
            OLD_ALIAS_SUFFIX,
        )

    # Update feed.json keys to match renamed ingredient aliases
    feed_path = target_activities_path.parent / "food/ecosystemic_services/feed.json"
    if feed_path.exists():
        with open(feed_path, encoding="utf-8") as f:
            feed_data = json.load(f)

        if add_old_suffix and alias_renames:
            updated_feed = {}
            renamed_count = 0
            for key, value in feed_data.items():
                new_key = alias_renames.get(key, key)
                new_value = {alias_renames.get(k, k): v for k, v in value.items()}
                updated_feed[new_key] = new_value
                if new_key != key:
                    renamed_count += 1
            feed_data = updated_feed
            print(f"feed.json: renamed {renamed_count} top-level keys")

        with open(feed_path, "w", encoding="utf-8") as f:
            json.dump(feed_data, f, indent=2, ensure_ascii=False)

    result = reassemble(merged_acts, merged_ings, other)

    with open(target_activities_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Merged {len(added_acts)} new activities into {len(merged_acts)} total activities")


def generate_final_data(variant: Variant):
    """Generate final CSV with all ingredient data and impacts.

    Combines:
    - source/new_ingredient_{variant}.csv (base data)
    - new_activities.json (predicted metadata)
    - processes_impacts.json (environmental impacts, matched by activityName)
    """
    output_csv, output_json, final_output_csv = get_output_paths(variant)

    # Load source CSV
    input_csv = get_input_csv(variant)
    print(f"Loading {input_csv}...")
    source_df = pd.read_csv(input_csv)

    ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
    ECOBALYSE = Path(os.environ["ECOBALYSE"])

    # Load processes_impacts.json - key by activityName for direct matching
    processes_path = ECOBALYSE_DATA / "public/data/processes_impacts.json"
    print(f"Loading {processes_path}...")
    with open(processes_path) as f:
        processes_list = json.load(f)
    processes_by_name = {p["activityName"]: p for p in processes_list}

    # Load new_activities.json to get predicted metadata
    print(f"Loading {output_json}...")
    with open(output_json) as f:
        new_activities = json.load(f)
    # Map alias to full activity (alias is unique; activityName is not)
    activities_by_alias = {a["alias"]: a for a in new_activities}

    # Load ingredients.json to get ecosystemicServices
    ingredients_path = ECOBALYSE / "public/data/food/ingredients.json"
    print(f"Loading {ingredients_path}...")
    with open(ingredients_path) as f:
        ingredients_list = json.load(f)
    ingredients_by_alias = {i["alias"]: i for i in ingredients_list}
    # For ingredients that have no ecosystemicServices, index by activityName as fallback
    # (a new ingredient may share an activityName with an existing one that has the data)
    ingredients_es_by_activity = {}
    for i in ingredients_list:
        es = i.get("ecosystemicServices")
        if es and any(v for v in es.values() if v is not None):
            ingredients_es_by_activity.setdefault(i.get("activityName"), i)

    print(
        f"\nLoaded: {len(new_activities)} activities, {len(processes_by_name)} processes, {len(ingredients_list)} ingredients"
    )

    # Process each row
    print(f"Processing {len(source_df)} ingredients...")
    results = []
    matched_processes = 0

    for _, row in source_df.iterrows():
        result = dict(row)  # Copy all source columns
        activity_name = row["icv final"]

        # Derive alias the same way build_activity_entry does (alias is unique, activityName is not)
        if variant == Variant.FR and row.get("Production_FR") == "DOM":
            alias_suffix = "-fr-overseas"
        else:
            alias_suffix = VARIANT_ALIAS_SUFFIX[variant]
        row_alias = generate_alias(row["item"]) + alias_suffix

        # Get predicted metadata from new_activities.json
        activity = activities_by_alias.get(row_alias)
        if activity:
            food_meta = next((m for m in activity.get("metadata", []) if "food" in m.get("scopes", [])), {})
            result["categories"] = ";".join(food_meta.get("ingredientCategories", []))
            result["transportCooling"] = food_meta.get("transportCooling", "")
            result["cropGroup"] = food_meta.get("cropGroup", "")
            result["defaultOrigin"] = food_meta.get("defaultOrigin", "")
            result["density"] = food_meta.get("ingredientDensity", "")
            result["inediblePart"] = food_meta.get("inediblePart", "")
            result["rawToCookedRatio"] = food_meta.get("rawToCookedRatio", "")
        else:
            result["categories"] = ""
            result["transportCooling"] = ""
            result["cropGroup"] = ""
            result["defaultOrigin"] = ""
            result["density"] = ""
            result["inediblePart"] = ""
            result["rawToCookedRatio"] = ""

        # Get impacts directly from processes_impacts.json by activityName
        process = processes_by_name.get(activity_name)
        if process:
            matched_processes += 1
            impacts = process.get("impacts", {})
            for col in IMPACT_COLUMNS:
                result[col] = impacts.get(col, "")
        else:
            for col in IMPACT_COLUMNS:
                result[col] = ""

        # Get ecosystemicServices from ingredients.json
        # Primary lookup by alias; fall back to activityName when the alias entry has no data
        ingredient = ingredients_by_alias.get(row_alias)
        es = (ingredient.get("ecosystemicServices") if ingredient else None) or None
        if es is None:
            fallback = ingredients_es_by_activity.get(activity_name)
            es = (fallback.get("ecosystemicServices") if fallback else None) or {}
        if ingredient or es:
            for field, multiplier in ECOSYSTEMIC_SERVICES_MULTIPLIERS.items():
                raw = es.get(field) or 0
                result[field] = raw * multiplier if raw else 0
        else:
            result["cropDiversity"] = ""
            result["hedges"] = ""
            result["livestockDensity"] = ""
            result["permanentPasture"] = ""
            result["plotSize"] = ""

        results.append(result)

    # Write output
    output_df = pd.DataFrame(results)
    output_df.to_csv(final_output_csv, index=False)
    print(f"\nMatched: {matched_processes}/{len(results)} processes with impacts")
    print(f"Final data written to {final_output_csv}")


def remove_old(target_activities_path: Path):
    """Remove activities/ingredients whose alias ends with '-2025' or displayName ends with ' (2025)'.

    Also removes feed.json entries whose top-level key ends with '-2025'.
    """
    old_alias_suffix = OLD_ALIAS_SUFFIX
    old_display_suffix = OLD_DISPLAY_SUFFIX

    # Load and filter activities.json
    with open(target_activities_path) as f:
        activities_list = json.load(f)

    filtered = []
    removed_activities = 0
    removed_ingredients = 0
    for activity in activities_list:
        alias = activity.get("alias", "")
        display = activity.get("displayName", "")
        if alias.endswith(old_alias_suffix) or display.endswith(old_display_suffix):
            removed_activities += 1
            continue

        # Filter food ingredients within the activity
        meta_list = activity.get("metadata", [])
        food_ings = [m for m in meta_list if "food" in m.get("scopes", [])]
        if food_ings:
            new_food = []
            for ing in food_ings:
                ing_alias = ing.get("alias", "")
                ing_display = ing.get("displayName", "")
                if ing_alias.endswith(old_alias_suffix) or ing_display.endswith(old_display_suffix):
                    removed_ingredients += 1
                else:
                    new_food.append(ing)
            non_food = [m for m in meta_list if "food" not in m.get("scopes", [])]
            if new_food:
                activity = {**activity, "metadata": non_food + new_food}
            else:
                if non_food:
                    activity = {**activity, "metadata": non_food}
                else:
                    activity = {k: v for k, v in activity.items() if k != "metadata"}

        filtered.append(activity)

    with open(target_activities_path, "w") as f:
        json.dump(filtered, f, indent=2, ensure_ascii=False)
    print(f"activities.json: removed {removed_activities} activities, {removed_ingredients} ingredients")

    # Load and filter feed.json
    feed_path = target_activities_path.parent / "food/ecosystemic_services/feed.json"
    if feed_path.exists():
        with open(feed_path, encoding="utf-8") as f:
            feed_data = json.load(f)

        removed_feed = 0
        filtered_feed = {}
        for key, value in feed_data.items():
            if key.endswith(old_alias_suffix):
                removed_feed += 1
            else:
                filtered_feed[key] = value
        feed_data = filtered_feed

        with open(feed_path, "w", encoding="utf-8") as f:
            json.dump(feed_data, f, indent=2, ensure_ascii=False)
        print(f"feed.json: removed {removed_feed} entries")

    print("Done!")


VARIANT_INDEPENDENT_FIELDS = [
    "foodType",
    "novaGroup",
    "processingState",
    "transportCooling",
    "cropGroup",
    "density",
    "inediblePart",
    "rawToCookedRatio",
    "packaging",
    "categories",
]


def compare_variants(generated_dir: Path):
    """Compare predictions across variant CSVs and warn on mismatches."""
    csv_files = sorted(generated_dir.glob("predictions_*.csv"))
    if len(csv_files) < 2:
        return

    variant_data = {}
    for csv_file in csv_files:
        variant = csv_file.stem.replace("predictions_", "")
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            variant_data[variant] = {row["name"]: row for row in reader}

    variants = list(variant_data.keys())
    all_names = set()
    for data in variant_data.values():
        all_names.update(data.keys())

    mismatches = 0
    for name in sorted(all_names):
        present = {v: variant_data[v][name] for v in variants if name in variant_data[v]}
        if len(present) < 2:
            continue
        variant_list = list(present.keys())
        for field in VARIANT_INDEPENDENT_FIELDS:
            values = {v: present[v].get(field, "") for v in variant_list}
            unique = set(values.values())
            if len(unique) > 1:
                parts = ", ".join(f"{v}={values[v]}" for v in variant_list)
                print(f"WARNING: '{name}' has different '{field}' across variants: {parts}")
                mismatches += 1

    if mismatches:
        print(f"\n{mismatches} cross-variant mismatches found.")
    else:
        print("\nNo cross-variant mismatches found.")


def main():
    parser = argparse.ArgumentParser(
        description="Export predicted ingredients to CSV and JSON"
    )
    parser.add_argument(
        "command",
        choices=["metadata", "final_data", "remove-old"],
        help="metadata: export predictions + merge activities. final_data: generate final CSV with impacts. remove-old: remove suffixed entries",
    )
    parser.add_argument(
        "--variant",
        type=lambda v: Variant[v.upper()],
        choices=list(Variant),
        metavar="{FR,ORG,UE,OI,NUE}",
        help="Variant: FR, ORG, UE, OI, NUE (required, used in output file names)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear translation cache before running",
    )
    parser.add_argument(
        "--add-old-suffix",
        action="store_true",
        help="Add '(2025)' suffix to pre-existing ingredient displayNames and '-2025' suffix to their aliases",
    )
    args = parser.parse_args()

    # Handle remove-old command (no variant needed)
    if args.command == "remove-old":
        ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
        activities_path = ECOBALYSE_DATA / "activities.json"
        remove_old(activities_path)
        return

    # Validate --variant is required for metadata and final_data commands
    if args.variant is None:
        parser.error("--variant is required for metadata and final_data commands")

    # Get output paths for this variant
    output_csv, output_json, final_output_csv = get_output_paths(args.variant)

    if args.command == "final_data":
        generate_final_data(args.variant)
        return

    if args.clear_cache:
        Predictor.clear_translation_cache()
        print("Translation cache cleared")

    ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
    ECOBALYSE = Path(os.environ["ECOBALYSE"])

    # Load training data
    ingredients_path = ECOBALYSE / "public/data/food/ingredients.json"
    print(f"Loading training data from {ingredients_path}...")
    with open(ingredients_path) as f:
        training_data = json.load(f)

    # Train predictor
    print(f"\nTraining on {len(training_data)} ingredients...")
    predictor = Predictor()
    predictor.fit(training_data)

    # Load input CSV
    input_csv = get_input_csv(args.variant)
    print(f"\nLoading {input_csv}...")
    df = pd.read_csv(input_csv)
    df.columns = df.columns.str.replace("_", " ")

    if "item" not in df.columns or "icv final" not in df.columns:
        raise ValueError("CSV must have 'item' and 'icv final' columns")

    # Predict for all ingredients
    print(f"\nProcessing {len(df)} ingredients...")
    results = predict_all(predictor, df, args.variant)

    # Write outputs
    print(f"\nWriting {len(results)} results...")
    write_csv(results, output_csv)
    write_json(results, output_json)

    # Compare predictions across variants
    compare_variants(GENERATED_DIR)

    # Merge into activities.json
    activities_path = ECOBALYSE_DATA / "activities.json"
    if activities_path.exists():
        merge_activities(
            output_json,
            activities_path,
            args.add_old_suffix,
        )

        # Copy reference CSVs to ecobalyse-data
        ref_src = Path(__file__).parent / "reference"
        ref_dst = ECOBALYSE_DATA / "food/metadata"
        ref_dst.mkdir(parents=True, exist_ok=True)
        for csv_file in sorted(ref_src.glob("*.csv")):
            shutil.copy2(csv_file, ref_dst / csv_file.name)
        print(f"Copied reference files to {ref_dst}")

        print(
            "\nNext step: run 'just export-all' in ecobalyse-data to regenerate ingredients.json"
        )
    else:
        print(f"\nWarning: ACTIVITIES path does not exist: {activities_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
