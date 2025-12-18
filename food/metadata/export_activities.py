#!/usr/bin/env python3
"""
Export predicted ingredients to activities.json format.

Usage:
    python export_activities.py data/new_ingredient_FR.csv -o new_ingredients.json
"""

import argparse
import json
import re
import uuid
from pathlib import Path

import pandas as pd
from predict import Predictor
from rich.progress import track


# Animal detection patterns and mappings
ANIMAL_PATTERNS = {
    # animalGroup1 -> animalGroup2 mappings
    "cattle": {
        "patterns": [
            r"\b(beef|boeuf|veau|veal|cattle|bovine|cow)\b",
        ],
        "group2": "cow",
        "product_default": "meat",
    },
    "pig": {
        "patterns": [
            r"\b(pork|porc|pig|swine|ham|jambon|bacon|saucisse|sausage)\b",
        ],
        "group2": "pig",
        "product_default": "meat",
    },
    "poultry": {
        "patterns": [
            r"\b(chicken|poulet|turkey|dinde|duck|canard|poultry|volaille|hen|poule)\b",
        ],
        "group2": "chicken",
        "product_default": "meat",
    },
    "sheep": {
        "patterns": [
            r"\b(lamb|agneau|sheep|mouton|mutton)\b",
        ],
        "group2": "sheep",
        "product_default": "meat",
    },
}

# Product type patterns (overrides default)
ANIMAL_PRODUCT_PATTERNS = {
    "egg": r"\b(egg|oeuf|œuf)\b",
    "milk": r"\b(milk|lait|dairy|cheese|fromage|yogurt|yaourt|cream|crème|butter|beurre)\b",
    "meat": r"\b(meat|viande|flesh|chair)\b",
}


def detect_animal_fields(name: str, activity_name: str) -> dict:
    """
    Detect animalGroup1, animalGroup2, animalProduct from ingredient name.

    Returns empty dict if not an animal product.
    """
    text = f"{name} {activity_name}".lower()

    # First detect animal group
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

    # Detect product type (egg, milk, meat)
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


def generate_alias(name: str) -> str:
    """
    Generate alias from English name.

    - Lowercase
    - Replace spaces with dashes
    - Remove special characters
    - Collapse multiple dashes
    """
    # Lowercase
    alias = name.lower()

    # Replace spaces and underscores with dashes
    alias = re.sub(r"[\s_]+", "-", alias)

    # Remove special characters (keep letters, numbers, dashes)
    alias = re.sub(r"[^a-z0-9-]", "", alias)

    # Collapse multiple dashes
    alias = re.sub(r"-+", "-", alias)

    # Strip leading/trailing dashes
    alias = alias.strip("-")

    return alias


def load_existing_uuids(output_path: str) -> dict:
    """
    Load existing UUIDs from output file to preserve them on re-export.

    Returns dict mapping (alias, activityName) -> (activity_id, ingredient_id)
    """
    existing = {}
    path = Path(output_path)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                activities = json.load(f)
            for activity in activities:
                alias = activity.get("alias", "")
                activity_name = activity.get("activityName", "")
                activity_id = activity.get("id")
                # Get ingredient ID from metadata
                ingredient_id = None
                food_list = activity.get("metadata", {}).get("food", [])
                if food_list:
                    ingredient_id = food_list[0].get("id")
                if alias and activity_id:
                    existing[(alias, activity_name)] = (activity_id, ingredient_id)
        except (json.JSONDecodeError, KeyError):
            pass  # File corrupt or wrong format, start fresh
    return existing


def build_activity_entry(
    name: str,
    french_name: str,
    activity_name: str,
    source: str,
    predictions: dict,
    existing_uuids: dict = None,
) -> dict:
    """
    Build an activity entry in the activities.json format.
    """
    # Generate alias first (needed for UUID lookup)
    alias = generate_alias(name)

    # Reuse existing UUIDs if available, otherwise generate new ones
    if existing_uuids and (alias, activity_name) in existing_uuids:
        activity_id, ingredient_id = existing_uuids[(alias, activity_name)]
        if not ingredient_id:
            ingredient_id = str(uuid.uuid4())
    else:
        activity_id = str(uuid.uuid4())
        ingredient_id = str(uuid.uuid4())

    # Use French name from CSV for displayName (fallback to English name)
    display_name = french_name if french_name else name

    # Build ingredient metadata
    ingredient = {
        "alias": alias,
        "defaultOrigin": predictions.get("defaultOrigin", "OutOfEuropeAndMaghreb"),
        "displayName": display_name,
        "id": ingredient_id,
        "inediblePart": predictions.get("inediblePart", 0),
        "ingredientCategories": predictions.get("categories", ["misc"]),
        "ingredientDensity": predictions.get("density", 1.0),
        "rawToCookedRatio": predictions.get("rawToCookedRatio", 1.0),
        "scenario": "reference",
        "transportCooling": predictions.get("transportCooling", "none"),
        "visible": True,
    }

    # Add cropGroup for vegetables
    if predictions.get("cropGroup"):
        ingredient["cropGroup"] = predictions["cropGroup"]

    # Add animal fields if animal product
    animal_fields = detect_animal_fields(name, activity_name)
    if animal_fields:
        ingredient["animalGroup1"] = animal_fields["animalGroup1"]
        ingredient["animalGroup2"] = animal_fields["animalGroup2"]
        ingredient["animalProduct"] = animal_fields["animalProduct"]

    # Build activity entry
    activity = {
        "activityName": activity_name,
        "alias": alias,
        "categories": ["ingredient"],
        "displayName": display_name,
        "id": activity_id,
        "metadata": {"food": [ingredient]},
        "scopes": ["food"],
        "source": source,
    }

    return activity


def main():
    parser = argparse.ArgumentParser(
        description="Export predicted ingredients to activities.json format"
    )
    parser.add_argument(
        "input_csv",
        type=str,
        help="Input CSV file (new_ingredient_FR.csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="new_ingredients.json",
        help="Output JSON file",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Path to saved predictor model (optional, will train if not provided)",
    )
    args = parser.parse_args()

    # Load input CSV
    print(f"Loading {args.input_csv}...")
    df = pd.read_csv(args.input_csv)

    # Expected columns: item, icv final, Source
    if "item" not in df.columns or "icv final" not in df.columns:
        raise ValueError("CSV must have 'item' and 'icv final' columns")

    # Load or train predictor
    if args.model and Path(args.model).exists():
        print(f"Loading predictor from {args.model}...")
        predictor = Predictor.load(args.model)
    else:
        print("Training predictor on existing ingredients...")
        with open("../ingredients.json") as f:
            training_data = json.load(f)
        predictor = Predictor()
        predictor.fit(training_data)

    # Load existing UUIDs to preserve them on re-export
    existing_uuids = load_existing_uuids(args.output)
    if existing_uuids:
        print(f"Loaded {len(existing_uuids)} existing UUIDs from {args.output}")

    # Process each ingredient
    print(f"\nProcessing {len(df)} ingredients...")
    activities = []

    for _, row in track(df.iterrows(), total=len(df), description="Predicting..."):
        name = str(row["item"]).strip()
        french_name = (
            str(row["Liste 4.1 Trad"]).strip()
            if pd.notna(row.get("Liste 4.1 Trad"))
            else ""
        )
        activity_name = (
            str(row["icv final"]).strip() if pd.notna(row["icv final"]) else ""
        )
        source = (
            str(row.get("Source", "")).strip()
            if pd.notna(row.get("Source"))
            else "Unknown"
        )

        if not name or not activity_name:
            continue

        # Get predictions
        ingredient = {"name": name, "activityName": activity_name}
        predictions = predictor.predict(ingredient)

        # Build activity entry (using French name from CSV, preserving existing UUIDs)
        activity = build_activity_entry(
            name, french_name, activity_name, source, predictions, existing_uuids
        )
        activities.append(activity)

    # Write output
    print(f"\nWriting {len(activities)} activities to {args.output}...")
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)

    print(f"✓ Done! Output written to {args.output}")

    # Show sample
    if activities:
        print("\n--- Sample output ---")
        print(json.dumps(activities[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
