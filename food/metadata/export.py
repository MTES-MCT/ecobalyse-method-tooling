#!/usr/bin/env python3
"""
Export predicted ingredients to CSV and activities.json format.

Usage:
    python export.py metadata --variant FR     # Export FR variant
    python export.py metadata --variant ORG    # Export organic variant
    python export.py final_data                # Generate final CSV with impacts
    python export.py metadata --variant FR --add-old-prefix     # Add (old) prefix to existing
    python export.py metadata --variant FR --remove-old-prefix  # Remove (old) prefix

Variants: FR, ORG, UE, DEF, NUE

Outputs:
    - generated/predictions.csv: CSV with all predictions and confidence scores
    - generated/new_activities.json: Activities format for Ecobalyse
"""

import argparse
import csv
import json
import os
import re
import uuid
from enum import Enum
from pathlib import Path

# Namespace UUID for deterministic UUID generation (generated once, never changes)
ECOBALYSE_NAMESPACE = uuid.UUID("a4e1d123-5c67-4b89-9def-1234567890ab")


class Variant(Enum):
    FR = "FR"
    ORG = "ORG"
    UE = "UE"
    DEF = "DEF"
    NUE = "NUE"


VARIANT_SUFFIX = {
    Variant.FR: " FR",
    Variant.ORG: " Bio",
    Variant.UE: " UE",
    Variant.DEF: " par défaut",
    Variant.NUE: " HORS UE",
}

VARIANT_SCENARIO = {
    Variant.FR: "reference",
    Variant.ORG: "organic",
    Variant.UE: "import",
    Variant.DEF: "import",
    Variant.NUE: "import",
}

from dotenv import load_dotenv

load_dotenv()

import bw2data
import pandas as pd
from rich.progress import track

from predict import Predictor

bw2data.projects.set_current("ecobalyse")

# =============================================================================
# ANIMAL DETECTION
# =============================================================================

ANIMAL_PATTERNS = {
    "cattle": {
        "patterns": [r"\b(beef|boeuf|veau|veal|cattle|bovine|cow)\b"],
        "group2": "cow",
        "product_default": "meat",
    },
    "pig": {
        "patterns": [r"\b(pork|porc|pig|swine|ham|jambon|bacon|saucisse|sausage)\b"],
        "group2": "pig",
        "product_default": "meat",
    },
    "poultry": {
        "patterns": [
            r"\b(chicken|poulet|turkey|dinde|duck|canard|poultry|volaille|hen|poule)\b"
        ],
        "group2": "chicken",
        "product_default": "meat",
    },
    "sheep": {
        "patterns": [r"\b(lamb|agneau|sheep|mouton|mutton)\b"],
        "group2": "sheep",
        "product_default": "meat",
    },
}

ANIMAL_PRODUCT_PATTERNS = {
    "egg": r"\b(egg|oeuf|œuf)\b",
    "milk": r"\b(milk|lait|dairy|cheese|fromage|yogurt|yaourt|cream|crème|butter|beurre)\b",
    "meat": r"\b(meat|viande|flesh|chair)\b",
}


def detect_animal_fields(name: str, activity_name: str) -> dict:
    """Detect animalGroup1, animalGroup2, animalProduct from ingredient name."""
    text = f"{name} {activity_name}".lower()

    animal_group1 = None
    animal_group2 = None
    product_default = "meat"

    for group1, config in ANIMAL_PATTERNS.items():
        for pattern in config["patterns"]:
            if re.search(pattern, text, re.IGNORECASE):
                animal_group1 = group1
                animal_group2 = config["group2"]
                product_default = config["product_default"]
                break
        if animal_group1:
            break

    if not animal_group1:
        return {}

    animal_product = product_default
    for product, pattern in ANIMAL_PRODUCT_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            animal_product = product
            break

    return {
        "animalGroup1": animal_group1,
        "animalGroup2": animal_group2,
        "animalProduct": animal_product,
    }


# =============================================================================
# HELPERS
# =============================================================================


def generate_alias(name: str) -> str:
    """Generate alias from English name."""
    alias = name.lower()
    alias = re.sub(r"[\s_]+", "-", alias)
    alias = re.sub(r"[^a-z0-9-]", "", alias)
    alias = re.sub(r"-+", "-", alias)
    return alias.strip("-")


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


def get_db_unit(activity_name):
    dbs = ("Agribalyse 3.2", "Ecoinvent 3.9.1", "Ecoinvent 3.11", "WFLDB")
    for db in dbs:
        if (
            len(
                activities := [
                    a for a in bw2data.Database(db) if a["name"] == activity_name
                ]
            )
            >= 1
        ):
            return activities[0]["unit"], db
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
            str(row["Liste 4.2 Trad"]).strip()
            if pd.notna(row.get("Liste 4.2 Trad"))
            else ""
        )
        activity_name = (
            str(row["icv final"]).strip() if pd.notna(row["icv final"]) else ""
        )
        unit, source = get_db_unit(activity_name)

        if not name or not activity_name:
            continue

        # Extract production location for FR variant handling
        production_fr = str(row.get("Production_FR", "")).strip()

        ingredient = {"name": name, "activityName": activity_name}
        predictions = predictor.predict(ingredient)

        results.append({
            "name": name,
            "french_name": french_name,
            "activity_name": activity_name,
            "source": source,
            "unit": fix_unit(unit),
            "predictions": predictions,
            "variant": variant,
            "production_fr": production_fr,
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
        "defaultOrigin",
        "defaultOriginMatch",
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
                "defaultOrigin": pred.get("defaultOrigin", ""),
                "defaultOriginMatch": _format_match(pred.get("defaultOriginMatch")),
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
) -> dict:
    """Build an activity entry in the activities.json format."""
    # Determine suffix based on variant and production location
    if variant == Variant.FR and production_fr == "DOM":
        variant_suffix = " FR Outre-Mer"
        alias_suffix = "-fr-overseas"
    else:
        variant_suffix = VARIANT_SUFFIX[variant]
        alias_suffix = "-" + variant.value.lower()

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
        "defaultOrigin": predictions.get("defaultOrigin", "OutOfEuropeAndMaghreb"),
        "displayName": display_name,
        "id": ingredient_id,
        "inediblePart": predictions.get("inediblePart", 0),
        "inediblePartMatch": predictions.get("inediblePartMatch"),
        "ingredientCategories": predictions.get("categories", ["misc"]),
        "ingredientDensity": predictions.get("density", 1.0),
        "ingredientDensityMatch": predictions.get("densityMatch"),
        "rawToCookedRatio": predictions.get("rawToCookedRatio", 1.0),
        "rawToCookedRatioMatch": predictions.get("rawToCookedRatioMatch"),
        "scenario": scenario,
        "transportCooling": predictions.get("transportCooling", "none"),
        "transportCoolingMatch": predictions.get("transportCoolingMatch"),
        "visible": True,
    }

    if predictions.get("cropGroup"):
        ingredient["cropGroup"] = predictions["cropGroup"]
        ingredient["cropGroupMatch"] = predictions.get("cropGroupMatch")

    animal_fields = detect_animal_fields(name, activity_name)
    if animal_fields:
        ingredient["animalGroup1"] = animal_fields["animalGroup1"]
        ingredient["animalGroup2"] = animal_fields["animalGroup2"]
        ingredient["animalProduct"] = animal_fields["animalProduct"]

    return {
        "activityName": activity_name,
        "alias": alias,
        "categories": ["ingredient"],
        "displayName": display_name,
        "id": activity_id,
        "metadata": {"food": [ingredient]},
        "scopes": ["food"],
        "source": source,
        "unit": unit,
    }


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
        )
        activities.append(activity)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)

    print(f"JSON written to {output_path}")


# =============================================================================
# CLI
# =============================================================================

INPUT_CSV = Path(__file__).parent / "source/new_ingredient_FR.csv"
OUTPUT_CSV = Path(__file__).parent / "generated/predictions.csv"
OUTPUT_JSON = Path(__file__).parent / "generated/new_activities.json"
FINAL_OUTPUT_CSV = Path(__file__).parent / "generated/new_ingredients.csv"

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


def merge_activities(
    new_activities_path: Path,
    target_activities_path: Path,
    add_old_prefix: bool = False,
    remove_old_prefix: bool = False,
):
    """Merge new_activities.json into target activities.json.

    Two-level merge with global ingredient uniqueness:
    1. Process level: merge by displayName (preserve existing UUIDs)
    2. Ingredient level: globally unique by displayName
       - If ingredient exists anywhere: preserve UUID, move to new activity
       - If ingredient is new: add with generated UUID

    Ingredients are globally unique: if an ingredient moves from one activity
    to another, it is removed from the old activity and added to the new one.

    Options:
    - add_old_prefix: Add " (old)" suffix to pre-existing ingredients
    - remove_old_prefix: Remove " (old)" suffix from all ingredients
    """
    with open(new_activities_path) as f:
        new_activities = json.load(f)

    with open(target_activities_path) as f:
        existing_activities = json.load(f)

    # Load keep list (ingredients that should not get "(old)" suffix)
    keep_csv_path = Path(__file__).parent / "source/keep.csv"
    keep_set = set()
    if keep_csv_path.exists():
        with open(keep_csv_path, encoding="utf-8") as f:
            keep_set = {line.strip() for line in f if line.strip()}

    # Separate activities with/without displayName (e.g. textile materials)
    existing_by_display = {a["displayName"]: a for a in existing_activities if "displayName" in a}
    other_activities = [a for a in existing_activities if "displayName" not in a]

    # Apply old suffix modifications to existing activities
    if add_old_prefix:
        count = 0
        skipped = 0
        for activity in existing_by_display.values():
            for ing in activity.get("metadata", {}).get("food", []):
                if ing["displayName"] in keep_set:
                    skipped += 1
                    continue
                if not ing["displayName"].endswith(" (old)"):
                    ing["displayName"] = ing["displayName"] + " (old)"
                    count += 1
        print(f"Added '(old)' suffix to {count} ingredients (skipped {skipped} from keep.csv)")

    if remove_old_prefix:
        for activity in existing_by_display.values():
            # Remove "new-" prefix from activity alias
            if activity.get("alias", "").startswith("new-"):
                activity["alias"] = activity["alias"][4:]
            for ing in activity.get("metadata", {}).get("food", []):
                if ing["displayName"].endswith(" (old)"):
                    ing["displayName"] = ing["displayName"][:-6]  # Remove " (old)"
                # Remove "new-" prefix from ingredient alias
                if ing.get("alias", "").startswith("new-"):
                    ing["alias"] = ing["alias"][4:]

    # Build global ingredient index: displayName -> {ingredient, activity_display_name}
    global_ingredients = {}
    for activity in existing_by_display.values():
        for ing in activity.get("metadata", {}).get("food", []):
            global_ingredients[ing["displayName"]] = {
                "ingredient": ing,
                "activity_display_name": activity["displayName"],
            }

    # Track which ingredients to remove from old activities
    ingredients_to_remove = {}  # activity_display_name -> set of displayNames to remove

    # Process new activities
    for new_activity in new_activities:
        display_name = new_activity["displayName"]

        # Preserve activity UUID if it exists
        if display_name in existing_by_display:
            new_activity["id"] = existing_by_display[display_name]["id"]

        # Process ingredients
        new_ingredients = new_activity.get("metadata", {}).get("food", [])

        # Add "new-" prefix to aliases when add_old_prefix is True
        if add_old_prefix:
            # Add "new-" prefix to activity alias
            if not new_activity["alias"].startswith("new-"):
                new_activity["alias"] = "new-" + new_activity["alias"]
            # Add "new-" prefix to ingredient aliases
            for new_ing in new_ingredients:
                if not new_ing["alias"].startswith("new-"):
                    new_ing["alias"] = "new-" + new_ing["alias"]

        for new_ing in new_ingredients:
            ing_display_name = new_ing["displayName"]

            if ing_display_name in global_ingredients:
                existing_entry = global_ingredients[ing_display_name]
                # Preserve existing ingredient UUID
                new_ing["id"] = existing_entry["ingredient"]["id"]

                # Mark for removal from old activity (if different)
                old_activity_display_name = existing_entry["activity_display_name"]
                if old_activity_display_name != display_name:
                    if old_activity_display_name not in ingredients_to_remove:
                        ingredients_to_remove[old_activity_display_name] = set()
                    ingredients_to_remove[old_activity_display_name].add(ing_display_name)

            # Update global index to point to new location
            global_ingredients[ing_display_name] = {
                "ingredient": new_ing,
                "activity_display_name": display_name,
            }

        # Update activity in index
        if display_name in existing_by_display:
            # Merge ingredients: keep existing ones not in new, add all new ones
            existing_activity = existing_by_display[display_name]
            existing_ings = existing_activity.get("metadata", {}).get("food", [])
            new_ing_names = {ing["displayName"] for ing in new_ingredients}
            # Keep existing ingredients that aren't being replaced
            kept_ings = [ing for ing in existing_ings if ing["displayName"] not in new_ing_names]
            # Combine: kept existing + new
            merged_ings = kept_ings + new_ingredients
            new_activity["metadata"]["food"] = merged_ings
            if kept_ings:
                print(f"Merged activity '{display_name}': kept {len(kept_ings)} existing + {len(new_ingredients)} new ingredients")
        existing_by_display[display_name] = new_activity

    # Remove moved ingredients from old activities
    for activity_display_name, ing_display_names in ingredients_to_remove.items():
        activity = existing_by_display.get(activity_display_name)
        if activity and "metadata" in activity and "food" in activity["metadata"]:
            activity["metadata"]["food"] = [
                ing for ing in activity["metadata"]["food"]
                if ing["displayName"] not in ing_display_names
            ]

    merged = list(existing_by_display.values()) + other_activities

    # Count "(old)" ingredients in final output
    old_count = sum(
        1 for a in merged
        for ing in a.get("metadata", {}).get("food", [])
        if ing.get("displayName", "").endswith(" (old)")
    )
    print(f"Final output has {old_count} '(old)' ingredients")

    with open(target_activities_path, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"Merged {len(new_activities)} activities into {target_activities_path}")
    print(f"Total activities: {len(merged)}")


def generate_final_data():
    """Generate final CSV with all ingredient data and impacts.

    Combines:
    - source/new_ingredient_FR.csv (base data)
    - new_activities.json (predicted metadata)
    - processes_impacts.json (environmental impacts, matched by activityName)
    """
    # Load source CSV
    print(f"Loading {INPUT_CSV}...")
    source_df = pd.read_csv(INPUT_CSV)

    # Load processes_impacts.json - key by activityName for direct matching
    processes_path = Path(os.environ["PROCESSES"])
    print(f"Loading {processes_path}...")
    with open(processes_path) as f:
        processes_list = json.load(f)
    processes_by_name = {p["activityName"]: p for p in processes_list}

    # Load new_activities.json to get predicted metadata
    print(f"Loading {OUTPUT_JSON}...")
    with open(OUTPUT_JSON) as f:
        new_activities = json.load(f)
    # Map activityName to full activity (which contains metadata)
    activities_by_name = {a["activityName"]: a for a in new_activities}

    # Load ingredients.json to get ecosystemicServices
    ingredients_path = Path(os.environ["INGREDIENTS"])
    print(f"Loading {ingredients_path}...")
    with open(ingredients_path) as f:
        ingredients_list = json.load(f)
    ingredients_by_name = {i["activityName"]: i for i in ingredients_list}

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

        # Get predicted metadata from new_activities.json
        activity = activities_by_name.get(activity_name)
        if activity:
            food_meta = activity.get("metadata", {}).get("food", [{}])[0]
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
        ingredient = ingredients_by_name.get(activity_name)
        if ingredient:
            es = ingredient.get("ecosystemicServices", {}) or {}
            result["cropDiversity"] = es.get("cropDiversity") or 0
            result["hedges"] = es.get("hedges") or 0
            result["livestockDensity"] = es.get("livestockDensity") or 0
            result["permanentPasture"] = es.get("permanentPasture") or 0
            result["plotSize"] = es.get("plotSize") or 0
        else:
            result["cropDiversity"] = ""
            result["hedges"] = ""
            result["livestockDensity"] = ""
            result["permanentPasture"] = ""
            result["plotSize"] = ""

        results.append(result)

    # Write output
    output_df = pd.DataFrame(results)
    output_df.to_csv(FINAL_OUTPUT_CSV, index=False)
    print(f"\nMatched: {matched_processes}/{len(results)} processes with impacts")
    print(f"Final data written to {FINAL_OUTPUT_CSV}")


def main():
    parser = argparse.ArgumentParser(
        description="Export predicted ingredients to CSV and JSON"
    )
    parser.add_argument(
        "command",
        choices=["metadata", "final_data"],
        help="metadata: export predictions + merge activities. final_data: generate final CSV with impacts",
    )
    parser.add_argument(
        "--variant",
        type=lambda v: Variant[v.upper()],
        choices=list(Variant),
        metavar="{FR,ORG,UE,DEF,NUE}",
        help="Variant: FR, ORG, UE, DEF, NUE (required for metadata command)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear translation cache before running",
    )
    parser.add_argument(
        "--add-old-prefix",
        action="store_true",
        help="Add '(old) ' prefix to pre-existing ingredients",
    )
    parser.add_argument(
        "--remove-old-prefix",
        action="store_true",
        help="Remove '(old) ' prefix from all ingredients",
    )
    args = parser.parse_args()

    # Validate mutually exclusive options
    if args.add_old_prefix and args.remove_old_prefix:
        parser.error("--add-old-prefix and --remove-old-prefix are mutually exclusive")

    # Validate --variant is required for metadata command
    if args.command == "metadata" and args.variant is None:
        parser.error("--variant is required for the metadata command")

    if args.command == "final_data":
        generate_final_data()
        return

    if args.clear_cache:
        Predictor.clear_translation_cache()
        print("Translation cache cleared")

    # Load training data
    ingredients_path = Path(os.environ["INGREDIENTS"])
    print(f"Loading training data from {ingredients_path}...")
    with open(ingredients_path) as f:
        training_data = json.load(f)

    # Train predictor
    print(f"\nTraining on {len(training_data)} ingredients...")
    predictor = Predictor()
    predictor.fit(training_data)

    # Load input CSV
    print(f"\nLoading {INPUT_CSV}...")
    df = pd.read_csv(INPUT_CSV)

    if "item" not in df.columns or "icv final" not in df.columns:
        raise ValueError("CSV must have 'item' and 'icv final' columns")

    # Predict for all ingredients
    print(f"\nProcessing {len(df)} ingredients...")
    results = predict_all(predictor, df, args.variant)

    # Write outputs
    print(f"\nWriting {len(results)} results...")
    write_csv(results, OUTPUT_CSV)
    write_json(results, OUTPUT_JSON)

    # Merge into activities.json if configured
    activities_path = os.environ.get("ACTIVITIES")
    if activities_path:
        activities_path = Path(activities_path)
        if activities_path.exists():
            merge_activities(
                OUTPUT_JSON,
                activities_path,
                args.add_old_prefix,
                args.remove_old_prefix,
            )
            print(
                "\nNext step: run 'just export-all' in ecobalyse-data to regenerate ingredients.json"
            )
        else:
            print(f"\nWarning: ACTIVITIES path does not exist: {activities_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
