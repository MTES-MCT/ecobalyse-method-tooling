"""
ecobalyse_data/detect/predict.py
================================

Predicts ALL metadata for a new ingredient from:
- Its name (French or English)
- Its LCA process name (activityName)

Uses classifiers trained on existing ingredients.

Usage:
    from ecobalyse_data.detect import predict

    # Training (once)
    predictor = predict.Predictor()
    predictor.fit(existing_ingredients)  # list of dicts
    predictor.save("models/ingredient_predictor.pkl")

    # Prediction
    predictor = predict.Predictor.load("models/ingredient_predictor.pkl")
    new_ingredient = {
        "name": "Tomate cerise bio",
        "activityName": "Cherry tomato, organic {FR} U"
    }
    predictions = predictor.predict(new_ingredient)
    # -> {"foodType": "vegetable", "density": 1.0, "densityMatch": {"file": "...", "name": "...", "confidence": 0.95}, ...}

CLI:
    # Train on existing ingredients
    python -m ecobalyse_data.detect.predict train ingredients.json --output model.pkl

    # Predict for a new ingredient
    python -m ecobalyse_data.detect.predict infer model.pkl --name "Tomate cerise" --activity "Cherry tomato {FR} U"

    # Evaluate with cross-validation
    python -m ecobalyse_data.detect.predict evaluate ingredients.json
"""

import json
import math
import pickle
import re
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import torch
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import LabelEncoder
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

warnings.filterwarnings("ignore")

# =============================================================================
# CONFIGURATION
# =============================================================================


# Farm-level activity patterns (shared by _detect_ratio_from_keywords and _classify_nova)
FARM_PATTERNS = [
    r"\bat\s+(farm\s+)?gate\b",
    r"\bat\s+farm\b",
    r"\bat\s+orchard\b",
    r"\bat\s+landing\b",
    r"\bat\s+greenhouse\b",
    r"\bmarket\s+for\b",
    r"\|\s*[\w\s]*production\b",
    r"\bproduction\s*\|",
    r"//\[[^\]]+\]\s*[\w\s]*production\b",
    r"\b\w+\s+production[,\s]",
]

# Noise words to remove from ingredient names before embedding
# Case-insensitive words
NAME_NOISE_WORDS_CI = [
    "par défaut",
    "par defaut",
    "élec",
]
# Case-sensitive words (uppercase country codes)
NAME_NOISE_WORDS_CS = [
    "FR",
    "IT",
    "DE",
    "ES",
    "BE",
    "UE",
    "EU",
]


def cleanup_name(name: str) -> str:
    """Remove noise words (country codes, 'par défaut', etc.) from ingredient name."""
    s = name
    # Case-insensitive removal
    pattern_ci = r"\b(" + "|".join(NAME_NOISE_WORDS_CI) + r")\b"
    s = re.sub(pattern_ci, " ", s, flags=re.IGNORECASE)
    # Case-sensitive removal (uppercase country codes only)
    pattern_cs = r"\b(" + "|".join(NAME_NOISE_WORDS_CS) + r")\b"
    s = re.sub(pattern_cs, " ", s)
    # Clean up punctuation and multiple spaces
    s = re.sub(r"[|]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ,;-").strip()


# Translation cache file (persisted to disk for faster subsequent runs)
TRANSLATION_CACHE_PATH = Path(__file__).parent / ".translation_cache.pkl"
MT_MODEL = "Helsinki-NLP/opus-mt-fr-en"  # FR → EN Machine Translation
# Embedding model (used for evaluation cross-validation only)
MODEL = "all-MiniLM-L6-v2"

# English terms that should NOT be translated (pass-through)
# These are English words that the FR→EN model mistranslates
TRANSLATION_PASSTHROUGH = {
    # Grains
    "millet",
    "barley",
    "rye",
    "oats",
    "wheat",
    "corn",
    "maize",
    "sorghum",
    "quinoa",
    "rice",
    "basmati",
    # Fish
    "char",
    "arctic char",
    "trout",
    "salmon",
    "cod",
    "haddock",
    "halibut",
    "bass",
    "perch",
    # Other foods
    "starch",
    "gluten",
    "syrup",
}

# Base categories (legacy - kept for backward compatibility during training)
BASE_CATEGORIES = [
    "misc",
    "dairy_product",
    "vegetable_processed",
    "vegetable_fresh",
    "grain_processed",
    "spice_condiment_additive",
    "animal_product",
    "nut_oilseed_raw",
    "grain_raw",
    "nut_oilseed_processed",
]

# New dimensional approach: split categories into foodType + processingState
FOOD_TYPES = [
    "vegetable",
    "fruit",
    "grain",
    "nut_oilseed",
    "dairy",
    "meat",
    "fish_seafood",
    "spice_condiment",
    "beverage",
    "misc",
]

PROCESSING_STATES = ["raw", "processed"]

# Mapping from old categories to new dimensions (foodType, processingState)
CATEGORY_TO_DIMENSIONS = {
    "vegetable_fresh": ("vegetable", "raw"),
    "vegetable_processed": ("vegetable", "processed"),
    "grain_raw": ("grain", "raw"),
    "grain_processed": ("grain", "processed"),
    "nut_oilseed_raw": ("nut_oilseed", "raw"),
    "nut_oilseed_processed": ("nut_oilseed", "processed"),
    "dairy_product": ("dairy", "processed"),
    "animal_product": ("meat", "raw"),
    "spice_condiment_additive": ("spice_condiment", "processed"),
    "misc": ("misc", "processed"),
}

# Reverse mapping: (foodType, processingState) -> base category
DIMENSIONS_TO_CATEGORY = {
    ("vegetable", "raw"): "vegetable_fresh",
    ("vegetable", "processed"): "vegetable_processed",
    ("fruit", "raw"): "vegetable_fresh",  # Fruits use vegetable_fresh in Ecobalyse
    ("fruit", "processed"): "vegetable_processed",
    ("grain", "raw"): "grain_raw",
    ("grain", "processed"): "grain_processed",
    ("nut_oilseed", "raw"): "nut_oilseed_raw",
    ("nut_oilseed", "processed"): "nut_oilseed_processed",
    ("dairy", "raw"): "dairy_product",
    ("dairy", "processed"): "dairy_product",
    ("meat", "raw"): "animal_product",
    ("meat", "processed"): "animal_product",
    ("fish_seafood", "raw"): "animal_product",
    ("fish_seafood", "processed"): "animal_product",
    ("spice_condiment", "raw"): "spice_condiment_additive",
    ("spice_condiment", "processed"): "spice_condiment_additive",
    ("beverage", "raw"): "misc",
    ("beverage", "processed"): "misc",
    ("misc", "raw"): "misc",
    ("misc", "processed"): "misc",
}

# Packaging types with their keywords and transportCooling values
PACKAGING_PATTERNS = {
    "canned": (r"\b(conserve|canned|appertis[ée]|bo[iî]te|tin)\b", "none"),
    "dried": (r"\b(s[ée]ch[ée]|d[ée]shydrat[ée]|dried|dehydrated|sec)\b", "none"),
    "frozen": (r"\b(surgel[ée]|congel[ée]|frozen)\b", "always"),
    "jar": (r"\b(bocal|jar|pot)\b", "none"),
    "vacuum": (r"\b(sous.?vide|vacuum)\b", "once_transformed"),
    "ambient": (r"\b(ambiant|ambient|shelf.?stable)\b", "none"),
    "fresh": (r"\b(frais|fra[iî]che|fresh)(?!\s+grade)\b", None),  # None = depends on foodType
}

# Additive labels (can combine with a base category)
ADDITIVE_LABELS = ["organic", "bleublanccoeur"]

TRANSPORT_COOLING_VALUES = ["none", "always", "once_transformed"]

# FoodType to CropGroup default mapping
# Used as fallback when specific patterns don't match
FOODTYPE_TO_CROPGROUP = {
    "fruit": "VERGERS",
    "vegetable": "LEGUMES-FLEURS",
    "grain": "AUTRES CEREALES",  # Override for wheat, rice, corn, barley
    "nut_oilseed": "FRUITS A COQUES",  # Override for specific oilseeds
    "spice_condiment": "DIVERS",
    "beverage": "DIVERS",
    "legume": "LEGUMINEUSES A GRAIN",
}


# =============================================================================
# REFERENCE DATA FOR VALUE CLASSIFIERS
# =============================================================================

# Paths to reference data files (relative to predict module)
REFERENCE_DIR = Path(__file__).parent / "reference"


def _load_csv_data(
    path: Path,
    name_col: str = "name",
    value_col: str = None,
    sep: str = ",",
) -> tuple[list, list, list]:
    """
    Generic CSV loader returning (names, values, sources).

    Args:
        path: Path to CSV file
        name_col: Column name for names
        value_col: Column name for values (if None, uses names as values)
        sep: CSV separator
    """
    if not path.exists():
        return [], [], []
    df = pd.read_csv(path, sep=sep)
    names = df[name_col].tolist()
    values = df[value_col].tolist() if value_col else names
    sources = [path.name] * len(df)
    return names, values, sources


def _load_density_data() -> tuple[list, list, list]:
    """Load fao_density.csv and density.csv, return combined (names, values, sources)."""
    names, values, sources = [], [], []
    # FAO density (primary reference)
    n, v, s = _load_csv_data(
        REFERENCE_DIR / "fao_density.csv", "name", "density", sep=";"
    )
    names.extend(n)
    values.extend(v)
    sources.extend(s)
    # Generic density (additional reference)
    n, v, s = _load_csv_data(REFERENCE_DIR / "density.csv", "name", "density", sep=";")
    names.extend(n)
    values.extend(v)
    sources.extend(s)
    return names, values, sources


def _load_inedible_data() -> tuple[list, list, list]:
    """Load agb_inedible.csv and inedible_part.csv, return combined (names, values, sources)."""
    names, values, sources = [], [], []
    # AGB inedible (primary reference)
    n, v, s = _load_csv_data(
        REFERENCE_DIR / "agb_inedible.csv", "name", "inedible_part", sep=";"
    )
    names.extend(n)
    values.extend(v)
    sources.extend(s)
    # Generic inedible (additional reference)
    n, v, s = _load_csv_data(
        REFERENCE_DIR / "inedible_part.csv", "name", "inedible_part", sep=";"
    )
    names.extend(n)
    values.extend(v)
    sources.extend(s)
    return names, values, sources


def _load_ratio_data() -> tuple[list, list, list]:
    """Load cooked_to_raw.csv, return (names, values, sources)."""
    return _load_csv_data(REFERENCE_DIR / "cooked_to_raw.csv", "food", "value", sep=";")


def _load_food_type_data() -> tuple[list, list, list]:
    """Load food_type.csv, return (names, food_types, sources)."""
    return _load_csv_data(REFERENCE_DIR / "food_type.csv", "name", "foodType")


def _load_processing_state_data() -> tuple[list, list, list]:
    """Load processing_state.csv, return (names, processing_states, sources)."""
    return _load_csv_data(
        REFERENCE_DIR / "processing_state.csv", "name", "processingState"
    )


def _load_cropgroup_data() -> tuple[list, list, list]:
    """Load cropgroup.csv, return (names, cropgroups, sources)."""
    return _load_csv_data(
        REFERENCE_DIR / "cropgroup.csv", "name", "cropGroup"
    )


def _load_transport_data() -> tuple[list, list, list]:
    """Load transport_cooling.csv, return (names, transport_cooling, sources)."""
    return _load_csv_data(
        REFERENCE_DIR / "transport_cooling.csv", "name", "transportCooling"
    )


def _load_nova_data() -> tuple[list, list, list]:
    """Load nova_classification.csv, return (names, nova_groups, sources)."""
    return _load_csv_data(
        REFERENCE_DIR / "nova_classification.csv", "name", "novaGroup"
    )


def _load_food_type_density() -> dict[str, tuple[float, str]]:
    """Load food_type_density.csv, return {food_type: (density, source)}."""
    path = REFERENCE_DIR / "food_type_density.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, sep=";")
    return {
        row["food_type"]: (row["density"], row["source"]) for _, row in df.iterrows()
    }


def _load_food_type_inedible() -> dict[tuple[str, int], tuple[float, str]]:
    """Load food_type_inedible_part.csv, return {(food_type, nova): (value, source)}."""
    path = REFERENCE_DIR / "food_type_inedible_part.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, sep=";")
    return {
        (row["food_type"], row["nova_group"]): (row["inedible_part"], row["source"])
        for _, row in df.iterrows()
    }


def _load_food_type_ratio() -> dict[str, tuple[float, str]]:
    """Load food_type_cooked_to_raw.csv, return {food_type: (ratio, source)}."""
    path = REFERENCE_DIR / "food_type_cooked_to_raw.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, sep=";")
    return {
        row["food_type"]: (row["ratio"], row["source"]) for _, row in df.iterrows()
    }


def _match(rule: str, conf: float) -> dict:
    """Build a Match dict with rule explanation and confidence."""
    return {"rule": rule, "confidence": round(conf, 3)}


def _build_cropgroup_data(ingredients: list) -> tuple[list, list, list]:
    """Build (names, cropGroups, sources) from training ingredients + cropGroup labels themselves."""
    names = []
    cropgroups = []
    sources = []

    # Add ingredient names as training points
    for ing in ingredients:
        if ing.get("cropGroup"):
            names.append(ing.get("name", ""))
            cropgroups.append(ing["cropGroup"])
            sources.append("ingredients.json")

    # Add cropGroup labels themselves as training points
    # e.g., "LEGUMES-FLEURS" → LEGUMES-FLEURS
    unique_cropgroups = set(cropgroups)
    for cg in unique_cropgroups:
        names.append(cg)  # The label itself
        cropgroups.append(cg)
        sources.append("cropgroup_labels")

    return names, cropgroups, sources


def _extract_ingredient_values(
    ingredients: list, field: str, allow_zero: bool = False
) -> tuple[list, list, list]:
    """
    Extract (names, values, sources) from ingredients with a given field.

    Only extracts ground truth values - skips ingredients that have a *Match
    attribute for this field (indicating the value was predicted, not curated).

    Args:
        ingredients: List of ingredient dicts
        field: Field name to extract (e.g., "density", "inediblePart")
        allow_zero: If True, include zero values (use "is not None" check).
                   If False, exclude zero/falsy values (use truthiness check).
    """
    match_field = f"{field}Match"
    names, values = [], []

    for ing in ingredients:
        # Skip predicted values (has Match attribute = not ground truth)
        if ing.get(match_field) is not None:
            continue

        val = ing.get(field)
        if allow_zero:
            # Use "is not None" check to include zero values
            if val is None:
                continue
        else:
            # Use truthiness check to exclude zero/falsy values
            if not val:
                continue

        names.append(ing["name"])
        values.append(val)

    sources = ["ingredients.json"] * len(names)
    return names, values, sources


class NearestNeighborMatcher:
    """Find nearest neighbor by cosine similarity using FoodOn + regex features."""

    def __init__(
        self,
        names: list,
        values: list,
        sources: list = None,
        translate_fn=None,
        foodon_extractor=None,
    ):
        """
        Build a nearest neighbor matcher on FoodOn + regex features.

        Args:
            names: List of food names from reference data
            values: List of corresponding values (numeric or string)
            sources: List of source file names (e.g., "fao_density.csv", "ingredients.json")
            translate_fn: Optional function to translate names before encoding
            foodon_extractor: Optional FoodOnFeatureExtractor for ontology features
        """
        self.names = list(names)
        self.values = list(values)  # Keep as list to support both numeric and string
        self.sources = list(sources) if sources else ["unknown"] * len(names)
        self.foodon_extractor = foodon_extractor
        self.translate_fn = translate_fn

        # Translate names if translation function provided (cached)
        translated_names = list(names)
        if translate_fn:
            print(f"  Translating {len(names)} names (cached)...")
            translated_names = [translate_fn(n) for n in names]

        # Store both original and translated names (lowercase) for text matching
        self.names_lower = [n.lower() for n in names]
        self.translated_lower = [n.lower() for n in translated_names]

        # Compute FoodOn + regex features for all reference names
        print(f"  Computing FoodOn+regex features for {len(names)} reference items...")
        features_list = []
        for i, name in enumerate(names):
            feat = extract_features(
                translated_names[i],
                "",
                translate_fn=None,  # Already translated
                foodon_extractor=foodon_extractor,
            )
            features_list.append(feat)
        self.features = np.array(features_list)
        print(f"  Nearest neighbor matcher ready ({len(names)} items)")

    def predict(self, query: str, translate_fn=None, foodon_extractor=None):
        """
        Find nearest neighbor and return its value.

        Priority:
        1. Exact text match (case-insensitive)
        2. Substring match (query contains reference or vice versa)
        3. FoodOn + regex feature similarity

        Args:
            query: Query string (ingredient name)
            translate_fn: Optional translation function
            foodon_extractor: Optional FoodOn extractor (uses stored one if not provided)

        Returns:
            (value, confidence, best_match_name, source) - value can be numeric or string
        """
        # Use stored values if not provided
        extractor = (
            foodon_extractor if foodon_extractor is not None else self.foodon_extractor
        )
        translator = translate_fn if translate_fn is not None else self.translate_fn

        # Normalize query for text matching
        query_lower = query.lower()
        query_translated = translator(query).lower() if translator else query_lower

        # 1. Try exact text match first (original or translated)
        for i, (name_low, trans_low) in enumerate(
            zip(self.names_lower, self.translated_lower)
        ):
            if query_lower == name_low or query_translated == trans_low:
                value = self.values[i]
                if isinstance(value, (int, float, np.number)):
                    value = float(value)
                return value, 1.0, self.names[i], self.sources[i]

        # 1.5 Try semantic near-exact match (handles plurals like "Avocado" ≈ "Avocados")
        # This uses sentence_transformers to find semantically very similar names
        # Reference data is loaded first, so return FIRST match above threshold
        try:
            from sentence_transformers import SentenceTransformer

            if not hasattr(self, "_embedding_model"):
                self._embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
            # Encode query and all reference names
            query_emb = self._embedding_model.encode([query_lower])[0]
            ref_embs = self._embedding_model.encode(self.names_lower)
            # Compute cosine similarities
            similarities = np.dot(ref_embs, query_emb) / (
                np.linalg.norm(ref_embs, axis=1) * np.linalg.norm(query_emb) + 1e-8
            )
            # Return FIRST match above threshold (reference data comes first in the list)
            # This ensures reference data (agb_inedible.csv) is preferred over ingredients.json
            for idx in range(len(similarities)):
                if similarities[idx] > 0.9:  # High threshold for near-exact
                    value = self.values[idx]
                    if isinstance(value, (int, float, np.number)):
                        value = float(value)
                    return value, 0.98, self.names[idx], self.sources[idx]
        except ImportError:
            pass  # sentence_transformers not available, skip semantic matching

        # 2. Try word match (reference word appears as complete word in query, or vice versa)
        # Uses word boundaries to avoid false positives like "bread" matching "breadfruit"
        def is_word_match(word: str, text: str) -> bool:
            """Check if word appears as a complete word in text."""
            if len(word) < 3:  # Skip very short words
                return False
            return bool(re.search(r'\b' + re.escape(word) + r'\b', text))

        def get_plural_forms(word: str) -> list:
            """Generate common plural/singular variations of a word."""
            forms = [word]
            # Singular to plural
            if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
                forms.append(word[:-1] + "ies")  # berry → berries
            elif word.endswith("s") or word.endswith("x") or word.endswith("ch"):
                forms.append(word + "es")
            else:
                forms.append(word + "s")
            # Plural to singular
            if word.endswith("ies") and len(word) > 3:
                forms.append(word[:-3] + "y")  # berries → berry
            elif word.endswith("es") and len(word) > 2:
                forms.append(word[:-2])  # tomatoes → tomato
            elif word.endswith("s") and len(word) > 1:
                forms.append(word[:-1])  # apples → apple
            return forms

        word_matches = []
        for i, (name_low, trans_low) in enumerate(
            zip(self.names_lower, self.translated_lower)
        ):
            # Get plural/singular variations for matching
            query_forms = get_plural_forms(query_lower)
            name_forms = get_plural_forms(name_low)
            trans_forms = get_plural_forms(trans_low)
            matched = False
            # Check if any reference name form appears as word in any query form
            for qf in query_forms:
                for nf in name_forms:
                    if is_word_match(nf, qf) or is_word_match(qf, nf):
                        word_matches.append((i, len(nf)))
                        matched = True
                        break
                if matched:
                    break
                for tf in trans_forms:
                    if is_word_match(tf, qf) or is_word_match(qf, tf):
                        word_matches.append((i, len(tf)))
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                # Original matching logic as fallback
                # Check if reference name appears as word in query
                if is_word_match(name_low, query_lower):
                    word_matches.append((i, len(name_low)))
                elif is_word_match(trans_low, query_translated):
                    word_matches.append((i, len(trans_low)))
                # Check if query appears as word in reference
                elif is_word_match(query_lower, name_low):
                    word_matches.append((i, len(query_lower)))
                elif is_word_match(query_translated, trans_low):
                    word_matches.append((i, len(query_translated)))

        if word_matches:
            # Return the longest word match
            best_i, _ = max(word_matches, key=lambda x: x[1])
            value = self.values[best_i]
            if isinstance(value, (int, float, np.number)):
                value = float(value)
            return value, 0.95, self.names[best_i], self.sources[best_i]

        # 3. Fall back to FoodOn + regex feature similarity
        query_features = extract_features(
            query, "", translate_fn=translator, foodon_extractor=extractor
        ).reshape(1, -1)

        # Compute cosine similarities to all reference features
        similarities = np.dot(self.features, query_features.T).flatten()
        norms_ref = np.linalg.norm(self.features, axis=1)
        norm_query = np.linalg.norm(query_features)
        # Avoid division by zero
        valid_norms = (norms_ref > 0) & (norm_query > 0)
        similarities[valid_norms] = similarities[valid_norms] / (
            norms_ref[valid_norms] * norm_query
        )
        similarities[~valid_norms] = 0

        # Return value of closest match
        best_idx = int(np.argmax(similarities))
        value = self.values[best_idx]
        # Convert to float if numeric, otherwise keep as string
        if isinstance(value, (int, float, np.number)):
            value = float(value)
        return (
            value,
            float(similarities[best_idx]),
            self.names[best_idx],
            self.sources[best_idx],
        )


# =============================================================================
# FEATURE EXTRACTION
# =============================================================================

# Detection patterns (French + English)
DETECTION_PATTERNS = {
    # Processing attributes
    "is_organic": r"\b(bio|organic|organique)\b",
    "is_fresh": r"\b(frais|fraîche|fraiche|fresh)\b",
    "is_frozen": r"\b(surgelé|surgelee|congelé|congelee|frozen)\b",
    "is_cooked": r"\b(cuit|cuite|cuire|cooked|roasted|grillé|grillee|rôti|rotie|bouilli|poché|pochee|frit|frite)\b",
    "is_raw": r"\b(cru|crue|raw|brut|brute)\b",
    "is_dried": r"\b(séché|sechee|sec|sèche|seche|dried|déshydraté|deshydratee)\b",
    "is_processed": r"\b(transformé|transformee|processed|préparé|preparee|industriel|conserve)\b",
    "is_canned": r"\b(conserve|appertisé|appertisee|canned)\b",
    "is_smoked": r"\b(fumé|fumee|smoked)\b",
    # Food types - Animals
    "is_meat": r"\b(viande|meat|boeuf|beef|porc|pork|veau|veal|agneau|lamb|mouton|mutton|poulet|chicken|dinde|turkey|canard|duck|lapin|rabbit|gibier|game)\b",
    "is_fish": r"\b(poisson|pêche|fish|cabillaud|cod|saumon|salmon|thon|tuna|sardine|maquereau|mackerel|truite|trout|bar|bass|dorade|bream|merlu|hake|sole|anchois|anchovy)\b",
    "is_seafood": r"\b(fruit.{0,3}mer|seafood|crevette|shrimp|prawn|crabe|crab|homard|lobster|moule|mussel|huître|huitre|oyster|coquillage|shellfish|calmar|squid|poulpe|octopus)\b",
    "is_egg": r"\b(oeuf|œuf|egg)\b",
    "is_dairy": r"\b(lait|milk|fromage|cheese|yaourt|yogurt|yoghurt|crème|cream|beurre|butter|lactose|dairy)\b",
    # Food types - Vegetables
    "is_vegetable": r"\b(légume|legume|vegetable|carotte|carrot|tomate|tomato|courgette|zucchini|aubergine|eggplant|poivron|pepper|oignon|onion|ail|garlic|pomme.{0,3}terre|potato|haricot|bean|petit.{0,3}pois|pea|épinard|spinach|salade|salad|laitue|lettuce|chou|cabbage|brocoli|broccoli|céleri|celery|concombre|cucumber|radis|radish|navet|turnip|betterave|beet|artichaut|artichoke|asperge|asparagus|fenouil|fennel|poireau|leek)\b",
    "is_fruit": r"\b(fruit|pomme|apple|poire|pear|orange|citron|lemon|banane|banana|fraise|strawberry|framboise|raspberry|cerise|cherry|pêche|peche|peach|abricot|apricot|prune|plum|raisin|grape|melon|pastèque|watermelon|mangue|mango|ananas|pineapple|kiwi|figue|fig|datte|date|grenade|pomegranate|papaye|papaya|litchi|lychee|avocat|avocado)\b",
    "is_grain": r"\b(céréale|cereale|cereal|grain|blé|ble|wheat|riz|rice|maïs|mais|corn|orge|barley|avoine|oat|seigle|rye|épeautre|epeautre|spelt|sarrasin|buckwheat|quinoa|millet|sorgho|sorghum|farine|flour|semoule|semolina|pâte|pate|pasta)\b",
    "is_legume": r"\b(légumineuse|legumineuse|legume|légume.{0,3}sec|lentille|lentil|pois|pea|bean|haricot|fève|feve|fava|pois.{0,3}chiche|chickpea|soja|soy|lupin)\b",
    "is_nut_seed": r"\b(noix|nut|walnut|amande|almond|noisette|hazelnut|pistache|pistachio|cacahuète|cacahuete|peanut|cajou|cashew|pécan|pecan|macadamia|graine|seed|tournesol|sunflower|sésame|sesame|lin|flax|chia|courge|pumpkin|chanvre|hemp|pignon|pine.{0,3}nut)\b",
    "is_oil_fat": r"\b(huile|oil|graisse|fat|margarine|olive|colza|rapeseed|tournesol|sunflower|arachide|peanut|palme|palm|coco|coconut|noix|walnut|sésame|sesame)\b",
    "is_spice": r"\b(épice|epice|spice|herbe|herb|aromate|poivre|pepper|sel|salt|sucre|sugar|cannelle|cinnamon|curcuma|turmeric|gingembre|ginger|paprika|curry|cumin|coriandre|coriander|basilic|basil|thym|thyme|romarin|rosemary|persil|parsley|menthe|mint|aneth|dill|origan|oregano|laurier|bay|muscade|nutmeg|clou.{0,3}girofle|clove|safran|saffron|vanille|vanilla)\b",
    "is_beverage": r"\b(boisson|beverage|drink|jus|juice|café|cafe|coffee|thé|the|tea|vin|wine|bière|biere|beer|alcool|alcohol|eau|water|soda|limonade|lemonade)\b",
    "is_sugar_sweet": r"\b(sucre|sugar|miel|honey|sirop|syrup|confiture|jam|chocolat|chocolate|bonbon|candy|gâteau|gateau|cake|biscuit|cookie|dessert|pâtisserie|patisserie|pastry)\b",
    # LCA process info
    "at_farm_gate": r"\bat\s+(farm\s+)?gate\b",
    "at_plant": r"\bat\s+plant\b",
    "at_processing": r"\bat\s+processing\b",
    "is_greenhouse": r"\b(greenhouse|serre)\b",
    "is_heated_greenhouse": r"\b(heated\s+greenhouse|serre\s+chauffée|serre\s+chauffee)\b",
}

# Index of binary features in the vector
BINARY_FEATURE_NAMES = list(DETECTION_PATTERNS.keys())

# FoodOn feature dimension (loaded from foodon_loader)
FOODON_DIM = 21

# Scale factors to balance FoodOn and regex features

# FoodOn features are already normalized, so scale = 1.0
FOODON_SCALE = 1.0
# Regex features are scaled to have similar magnitude
REGEX_SCALE = math.sqrt(FOODON_DIM / len(DETECTION_PATTERNS))  # ~0.9


def extract_features(
    name: str, activity_name: str, translate_fn=None, foodon_extractor=None
) -> np.ndarray:
    """
    Extract feature vector combining FoodOn ontology + regex pattern features.

    Features vector structure:
    - [0:21] FoodOn ontology features (scaled)
    - [21:46] Regex binary features (scaled)

    Args:
        name: Ingredient name (potentially French)
        activity_name: Activity/process name
        translate_fn: Optional function to translate name before encoding
        foodon_extractor: Optional FoodOnFeatureExtractor for ontology features

    Returns:
        np.ndarray of dimension (21 + nb_patterns) = 46 dims
    """
    # Combine name + activity for regex matching
    full_text = f"{name} {activity_name}".lower()

    # 1. FoodOn features (21 dims) - uses translated name for English ontology
    if foodon_extractor is not None:
        # Translate name for FoodOn (English-based ontology)
        name_for_foodon = translate_fn(name) if translate_fn else name
        foodon_features = foodon_extractor.extract_features(name_for_foodon)
    else:
        foodon_features = np.zeros(FOODON_DIM, dtype=np.float32)
    foodon_scaled = foodon_features * FOODON_SCALE

    # 2. Regex binary features (25 dims) - scaled for equal weight
    binary_features = []
    for pattern_name, pattern in DETECTION_PATTERNS.items():
        match = 1.0 if re.search(pattern, full_text, re.IGNORECASE) else 0.0
        binary_features.append(match)
    regex_features = np.array(binary_features, dtype=np.float32) * REGEX_SCALE

    # Concatenate features
    return np.concatenate([foodon_scaled, regex_features])


# =============================================================================
# PREDICTOR CLASS
# =============================================================================


class Predictor:
    """
    Metadata predictor for food ingredients.

    Uses FoodOn ontology + regex pattern features for nearest neighbor matching.
    Combines:
    - Nearest neighbor matching for foodType, processingState, cropGroup, transportCooling
    - Nearest neighbor matching for density, inediblePart, rawToCookedRatio
    """

    def __init__(self):
        """Initialize predictor."""
        # SentenceTransformer for evaluation cross-validation only
        self.model = None
        self._model_loaded = False

        # Translation model (FR → EN)
        self.mt_tokenizer = None
        self.mt_model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._translation_cache = (
            self._load_translation_cache()
        )  # Load from disk if exists

        # FoodOn feature extractor (lazy loaded)
        self.foodon_extractor = None
        self._foodon_loaded = False

        # Matchers for categorical metadata (nearest neighbor approach)
        self.food_type_matcher = None
        self.transport_matcher = None
        self.nova_matcher = None

        # CropGroup matcher (nearest neighbor)
        self.cropgroup_matcher = None

        # Value matchers for continuous values (nearest neighbor on reference data)
        self.density_matcher = None
        self.inedible_matcher = None
        self.ratio_matcher = None

        # FoodType default tables (loaded from CSV)
        self.food_type_density = _load_food_type_density()
        self.food_type_inedible = _load_food_type_inedible()
        self.food_type_ratio = _load_food_type_ratio()

        # Training data (for evaluation)
        self.training_features = None
        self.training_ingredients = None

        # Metadata
        self.is_fitted = False
        self.feature_dim = None

    @staticmethod
    def _load_translation_cache() -> dict:
        """Load translation cache from disk if exists."""
        if TRANSLATION_CACHE_PATH.exists():
            try:
                with open(TRANSLATION_CACHE_PATH, "rb") as f:
                    cache = pickle.load(f)
                print(f"Loaded {len(cache)} cached translations from disk")
                return cache
            except Exception:
                return {}
        return {}

    def _save_translation_cache(self):
        """Save translation cache to disk."""
        with open(TRANSLATION_CACHE_PATH, "wb") as f:
            pickle.dump(self._translation_cache, f)
        print(f"Saved {len(self._translation_cache)} translations to cache")

    @staticmethod
    def clear_translation_cache():
        """Clear the translation cache file."""
        if TRANSLATION_CACHE_PATH.exists():
            TRANSLATION_CACHE_PATH.unlink()
            print("Translation cache cleared")

    def _load_translation_model(self):
        """Load translation model (lazy loading)."""
        if self.mt_model is None:
            print(f"Loading translation model: {MT_MODEL}")
            self.mt_tokenizer = AutoTokenizer.from_pretrained(MT_MODEL)
            self.mt_model = AutoModelForSeq2SeqLM.from_pretrained(MT_MODEL).to(
                self.device
            )

    def _load_embedding_model(self):
        """Load embedding model for evaluation only (lazy loading)."""
        if not self._model_loaded:
            print("Importing sentence_transformers...")
            from sentence_transformers import SentenceTransformer

            print(f"Loading embedding model: {MODEL}")
            self.model = SentenceTransformer(MODEL)
            self._model_loaded = True

    def _load_foodon(self):
        """Load FoodOn feature extractor (lazy loading)."""
        if not self._foodon_loaded:
            from foodon_loader import FoodOnFeatureExtractor

            self.foodon_extractor = FoodOnFeatureExtractor()
            self._foodon_loaded = True

    def _translate(self, text: str) -> str:
        """Translate French text to English (with caching)."""
        # Clean up noise words (country codes, "par défaut", etc.) before translation
        text = cleanup_name(text)
        if not text:
            return ""

        # Check cache first
        if text in self._translation_cache:
            return self._translation_cache[text]

        # Check if text is an English term that should not be translated
        text_lower = text.lower()
        if text_lower in TRANSLATION_PASSTHROUGH:
            self._translation_cache[text] = text
            return text

        # Check if all significant words are English passthrough terms
        words = text_lower.split()
        if all(w in TRANSLATION_PASSTHROUGH for w in words if len(w) > 2):
            self._translation_cache[text] = text
            return text

        inputs = self.mt_tokenizer(text, return_tensors="pt").to(self.device)
        with torch.no_grad():
            outputs = self.mt_model.generate(**inputs, max_length=40)
        result = self.mt_tokenizer.batch_decode(outputs, skip_special_tokens=True)[
            0
        ].strip()

        # Cache the result
        self._translation_cache[text] = result
        return result

    def _get_base_category(self, categories: list) -> str:
        """Extract base category (not organic/bleublanccoeur)."""
        for cat in categories:
            if cat in BASE_CATEGORIES:
                return cat
        return "misc"

    def _get_additive_labels(self, categories: list) -> list:
        """Extract additive labels."""
        return [cat for cat in categories if cat in ADDITIVE_LABELS]

    def _is_vegetal(self, categories: list) -> bool:
        """Determine if ingredient is vegetal (requires cropGroup)."""
        vegetal_categories = {
            "vegetable_fresh",
            "vegetable_processed",
            "grain_raw",
            "grain_processed",
            "nut_oilseed_raw",
            "nut_oilseed_processed",
            "spice_condiment_additive",
        }
        return any(cat in vegetal_categories for cat in categories)

    def _extract_binary_from_features(self, features: np.ndarray) -> dict:
        """Extract binary regex features from the feature vector."""
        # Features: [0:21] FoodOn, [21:46] regex
        regex_start = FOODON_DIM  # 21
        binary_values = features[0, regex_start:]  # Skip FoodOn to get regex features
        # Unscale to get original binary values
        binary_values = binary_values / REGEX_SCALE
        return {
            name: bool(binary_values[i] > 0.5)
            for i, name in enumerate(BINARY_FEATURE_NAMES)
        }

    def _predict_category_by_rules(self, binary_features: dict) -> str | None:
        """Apply deterministic rules for category. Returns None if no rule matches."""
        if binary_features.get("is_fish") or binary_features.get("is_seafood"):
            return "animal_product"
        if binary_features.get("is_meat"):
            return "animal_product"
        if binary_features.get("is_egg"):
            return "animal_product"
        if binary_features.get("is_dairy"):
            return "dairy_product"
        return None

    def _infer_cropgroup_from_foodtype(
        self, name: str, activity: str, food_type: str
    ) -> tuple[str, str] | None:
        """Infer cropGroup from foodType and keyword patterns.

        Returns (cropGroup, match_description) or None to fall back to matcher.
        """
        text = f"{name} {activity}".lower()

        # Check cropgroup reference data FIRST (before pattern inference)
        # This gives CSV data priority over hardcoded patterns
        if self.cropgroup_matcher is not None:
            cropgroup_val, conf, match_name, source = self.cropgroup_matcher.predict(
                name, translate_fn=self._translate
            )
            # Only use matches from reference CSV, not ingredients.json
            if conf >= 0.95 and source == "cropgroup.csv":
                return cropgroup_val, f"matched '{match_name}' in cropgroup.csv"

        # Edge cases: items often misclassified by foodType prediction
        # These patterns take priority over foodType-based logic
        if any(w in text for w in ["cocoa", "cacao", "coffee", "café"]):
            return "DIVERS", "cocoa/coffee pattern"
        if "prickly pear" in text or "figue de barbarie" in text:
            return "VERGERS", "prickly pear pattern"
        if any(w in text for w in ["grape", "raisin", "wine", "vin"]):
            return "VIGNES", "grape/wine pattern"

        # Specific grains override default
        if food_type == "grain":
            if any(w in text for w in ["wheat", "flour", "bread", "pasta", "biscuit", "cake", "semolina", "couscous"]):
                return "BLE TENDRE", "wheat/flour pattern"
            if any(w in text for w in ["rice", "basmati", "riz"]):
                return "RIZ", "rice pattern"
            if any(w in text for w in ["corn", "maize", "maïs", "polenta", "popcorn"]):
                return "MAIS GRAIN ET ENSILAGE", "corn pattern"
            if any(w in text for w in ["barley", "orge", "malt", "beer", "bière"]):
                return "ORGE", "barley pattern"
            return "AUTRES CEREALES", "grain default"

        # Specific oilseeds/nuts override default
        if food_type == "nut_oilseed":
            if any(w in text for w in ["sunflower", "tournesol"]):
                return "TOURNESOL", "sunflower pattern"
            if any(w in text for w in ["rapeseed", "canola", "colza"]):
                return "COLZA", "rapeseed pattern"
            if any(w in text for w in ["olive"]):
                return "OLIVIERS", "olive pattern"
            if any(w in text for w in ["grape", "wine", "vin", "vinegar", "vinaigre", "raisin"]):
                return "VIGNES", "grape/wine pattern"
            if any(w in text for w in ["soy", "soja", "sesame", "sésame", "flax", "lin", "palm", "palme"]):
                return "AUTRES OLEAGINEUX", "oilseed pattern"
            # Default: nuts (almond, walnut, hazelnut, etc.)
            return "FRUITS A COQUES", "nut default"

        # Legumes (can have foodType=legume or foodType=vegetable)
        if food_type == "legume":
            return "LEGUMINEUSES A GRAIN", "legume default"

        # Legume patterns (for items classified as vegetable but are actually legumes)
        # Use word boundary matching to avoid false positives (peaches, coffee beans)
        legume_patterns = [
            r"\blentil", r"\blentille", r"\bchickpea", r"\bpois chiche",
            r"\b(red|white|lima|mung|broad|french|flageolet|fava|kidney)\s*(bean|haricot)",
            r"\bharicot", r"\bfève\b", r"\bflageolet",
            r"\b(split|spring|winter|snow|garden)\s*pea",
            r"\bpeas\b",  # "peas" but not "peaches"
            r"\blupin",
        ]
        if any(re.search(p, text) for p in legume_patterns):
            return "LEGUMINEUSES A GRAIN", "legume pattern"

        # Use foodType default for fruit, vegetable, spice, beverage
        default = FOODTYPE_TO_CROPGROUP.get(food_type)
        if default:
            return default, f"{food_type} default"

        return None  # Fall back to matcher

    def _predict_transport_by_rules(
        self, binary_features: dict, food_type: str, nova_group: int
    ) -> str | None:
        """Apply deterministic rules for transportCooling based on features, foodType and NOVA.

        Returns None if no rule matches (falls back to nearest neighbor matcher).
        """
        # Packaging-based rules (highest priority)
        if binary_features.get("is_frozen"):
            return "always"
        if binary_features.get("is_dried") or binary_features.get("is_canned"):
            return "none"

        # NOVA 1 (raw/fresh) + perishable foodType → needs cooling
        perishable_types = {"vegetable", "fruit", "legume", "meat", "fish_seafood", "dairy"}
        if nova_group == 1 and food_type in perishable_types:
            return "always"

        # Non-perishable types → no cooling needed
        non_perishable_types = {"grain", "nut_oilseed", "spice_condiment"}
        if food_type in non_perishable_types:
            return "none"

        return None  # fallback to matcher for edge cases

    def _get_default_density(self, food_type: str) -> tuple[float, str]:
        """Return (density, source_file) for food type from CSV.

        Used as fallback when matcher confidence is too low.
        """
        if food_type in self.food_type_density:
            density, _ = self.food_type_density[food_type]
            return density, "food_type_density.csv"
        return 0.90, "predict.py"

    def _is_related_match(self, query: str, match: str) -> bool:
        """Check if query and match are semantically related.

        This catches false positives from sparse feature vectors where
        unrelated items (e.g., 'Amaranth' and 'Lard') get identical vectors.

        First tries explicit plural matching (handles berry/berries which
        embedding models handle poorly), then falls back to semantic similarity.
        """

        def get_plural_forms(word: str) -> set:
            """Generate common plural/singular variations of a word."""
            word = word.lower()
            forms = {word}
            # Singular to plural
            if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
                forms.add(word[:-1] + "ies")  # berry → berries
            elif word.endswith("s") or word.endswith("x") or word.endswith("ch"):
                forms.add(word + "es")
            else:
                forms.add(word + "s")
            # Plural to singular
            if word.endswith("ies") and len(word) > 3:
                forms.add(word[:-3] + "y")  # berries → berry
            elif word.endswith("es") and len(word) > 2:
                forms.add(word[:-2])  # tomatoes → tomato
            elif word.endswith("s") and len(word) > 1:
                forms.add(word[:-1])  # apples → apple
            return forms

        # Check if plural forms overlap (handles berry/berries, tomato/tomatoes)
        query_forms = get_plural_forms(query)
        match_forms = get_plural_forms(match)
        if query_forms & match_forms:
            return True

        # Fall back to semantic similarity for other cases
        self._load_embedding_model()
        embeddings = self.model.encode([query, match])
        similarity = np.dot(embeddings[0], embeddings[1]) / (
            np.linalg.norm(embeddings[0]) * np.linalg.norm(embeddings[1])
        )
        return similarity > 0.7

    def _detect_inedible_from_keywords(
        self, name: str, activity: str
    ) -> tuple[float, str] | None:
        """Detect inedible part from processing keywords.

        Returns (value, keyword_description) if keywords indicate processing state,
        None otherwise to fall back to matcher.
        """
        text = f"{name} {activity}".lower()

        # Fillet/boneless = 0 (bones removed by definition)
        if re.search(r"\b(fillet|filet|filé|boneless|désossé)\b", text):
            return 0.0, "fillet/boneless"

        # Shelled/peeled = 0 (shell/peel removed)
        if re.search(r"\b(shelled|peeled|décortiqué|pelé)\b", text):
            return 0.0, "shelled/peeled"

        # Canned/frozen processed = 0 (pre-processed)
        if re.search(r"\b(canned|conserve|frozen|surgelé)\b", text):
            return 0.0, "canned/frozen"

        # With shell = high (shell is inedible)
        if re.search(r"\b(with\s+shell|avec\s+coquille|in\s+shell)\b", text):
            return 0.50, "with shell"

        # With bone = medium (bone is inedible)
        if re.search(r"\b(with\s+bone|avec\s+os)\b", text):
            return 0.20, "with bone"

        return None  # No keyword found, fall back to matcher

    def _get_default_inedible_part(
        self, food_type: str, nova_group: int
    ) -> tuple[float, str]:
        """Return (inedible_part, source_file) for foodType + novaGroup from CSV.

        Uses novaGroup to adjust: processed items (NOVA 2-4) typically have
        lower inedible parts since processing removes waste.
        """
        # NOVA 2-4 all use nova_group=2 in the CSV
        lookup_nova = 1 if nova_group == 1 else 2
        key = (food_type, lookup_nova)
        if key in self.food_type_inedible:
            inedible, _ = self.food_type_inedible[key]
            return inedible, "food_type_inedible_part.csv"
        return 0.10, "predict.py"

    def _detect_ratio_from_keywords(
        self, name: str, activity: str, food_type: str
    ) -> tuple[float, str] | None:
        """Detect cooking ratio from processing keywords.

        Returns (value, keyword_description) for special cases (dried, poultry,
        offal), None otherwise to fall back to foodType default.
        """
        text = f"{name} {activity}".lower()

        # Dried items absorb water when cooked — ratio depends on food type
        is_at_farm = any(re.search(p, activity, re.IGNORECASE) for p in FARM_PATTERNS)
        if not is_at_farm:
            print(f"WARNING: '{name}' activity is not at farm level: '{activity}'")
            if re.search(r"\b(dried|séchée?s?|dehydrated|déshydratée?s?)\b", text):
                dried_ratios = {"legume": 2.33, "grain": 2.259, "fruit": 1.0, "vegetable": 3.33}
                ratio = dried_ratios.get(food_type, 4.0)
                return ratio, "dried/dehydrated"

        # Poultry detection (more specific than generic meat)
        if re.search(
            r"\b(chicken|poulet|turkey|dinde|duck|canard|poultry|volaille|"
            r"goose|oie|pigeon|rabbit|lapin|broiler)\b",
            text,
        ):
            return 0.755, "poultry"

        # Offal detection
        if re.search(r"\b(liver|foie|kidney(?!\s*bean)|rein|offal|abat)\b", text):
            return 0.730, "offal"

        return None  # Fall back to foodType default

    def _get_default_ratio(self, food_type: str) -> tuple[float, str]:
        """Return (ratio, source_file) for food type from CSV.

        Values represent cooked weight / raw weight:
        - < 1: weight loss (water evaporation during cooking)
        - > 1: weight gain (water absorption, e.g., cereals/legumes)
        """
        if food_type in self.food_type_ratio:
            ratio, _ = self.food_type_ratio[food_type]
            return ratio, "food_type_cooked_to_raw.csv"
        return 1.0, "predict.py"

    def _detect_packaging(self, text: str) -> tuple[str | None, str | None]:
        """
        Detect packaging type from text and return (packaging, transportCooling).

        Returns:
            (packaging_type, transport_cooling) or (None, None) if not detected
        """
        text_lower = text.lower()
        for pkg_type, (pattern, transport) in PACKAGING_PATTERNS.items():
            if re.search(pattern, text_lower, re.IGNORECASE):
                return pkg_type, transport
        return None, None

    def _get_transport_from_packaging(
        self, packaging: str | None, food_type: str
    ) -> str:
        """
        Determine transportCooling from packaging and foodType.

        If packaging is 'fresh' or None, uses foodType to decide:
        - meat, fish_seafood, dairy, vegetable, fruit → always
        - grain, nut_oilseed, spice_condiment, misc → none
        """
        if packaging and packaging != "fresh":
            # Packaging has a direct mapping
            _, transport = PACKAGING_PATTERNS.get(packaging, (None, None))
            if transport:
                return transport

        # Fresh or unknown packaging: use foodType
        perishable_types = {"meat", "fish_seafood", "dairy", "vegetable", "fruit", "legume"}
        if food_type in perishable_types:
            return "always"
        return "none"

    def _compute_category(self, food_type: str, nova_group: int) -> str:
        """Compute category directly from foodType and novaGroup.

        This replaces the indirect lookup via DIMENSIONS_TO_CATEGORY.
        NOVA 1 = raw/fresh, NOVA 2-4 = processed.
        """
        is_raw = nova_group == 1

        if food_type in {"vegetable", "fruit", "legume"}:
            return "vegetable_fresh" if is_raw else "vegetable_processed"
        elif food_type == "grain":
            return "grain_raw" if is_raw else "grain_processed"
        elif food_type == "nut_oilseed":
            return "nut_oilseed_raw" if is_raw else "nut_oilseed_processed"
        elif food_type in {"meat", "fish_seafood"}:
            return "animal_product"
        elif food_type == "dairy":
            return "dairy_product"
        elif food_type == "spice_condiment":
            return "spice_condiment_additive"
        else:
            return "misc"

    def _classify_nova(
        self, name: str, activity_name: str, food_type: str, binary_features: dict
    ) -> tuple[int, str, float]:
        """
        Classify ingredient into NOVA 1-4 group.

        Decision hierarchy:
        1. Activity name location keywords (at farm, at orchard, at landing → NOVA 1)
        2. NOVA 2 culinary ingredient patterns (oil, butter, sugar, salt, flour)
        3. NOVA 4 ultra-processed indicators (textured, rehydrated, instant, isolate)
        4. "at plant" processing (minimal→NOVA 1, extracted→NOVA 2, other→NOVA 3)
        5. Packaging/preservation (canned, smoked → NOVA 3)
        6. Nearest neighbor matching on reference data
        7. FoodType-based defaults
        8. Default: NOVA 1 (unprocessed)

        Returns:
            (nova_group, reason, confidence)
        """
        text = f"{name} {activity_name}".lower()

        # Priority 0: Distilled spirits (highest priority - definitely NOVA 4)
        distilled_spirits = [
            r"\b(brandy|cognac|armagnac|calvados|whiskey|whisky|vodka|gin|rum|rhum|pastis|sake|spiritueux|spirits)\b",
            r"\b(distill|alcool\s+pur|pure\s+alcohol)\b",
        ]
        if any(re.search(p, text, re.IGNORECASE) for p in distilled_spirits):
            return 4, "distilled_spirits", 0.95

        # Priority 0.5: NOVA 2 culinary ingredients (before farm patterns)
        # These are extracted/refined products that should not be treated as raw farm output
        name_lower = name.lower()

        # Sugar check: sugar in name but not "sugar beet" or "sugar cane" (raw crops)
        if re.search(r"\bsugar\b", name_lower, re.IGNORECASE):
            if not re.search(r"\bsugar\s*(beet|cane)\b", name_lower, re.IGNORECASE):
                return 2, "nova2_sugar", 0.95

        nova2_patterns = [
            (r"\b(huile|oil)\b(?!.*seed)", "oil"),
            (r"\b(beurre|butter|margarine)\b(?!.*nut)", "butter"),  # exclude butternut
            (r"\b(lard|saindoux)\b", "fat"),  # rendered animal fat
            (r"\b(salt|sel)\b(?!.*fish)", "salt"),  # exclude salted fish
            (r"\b(farine|flour)\b(?!.*seed)", "flour"),
            (r"\b(f[ée]cule|starch)\b", "starch"),
            (r"\bwheat\s+gluten\b", "gluten"),  # refined gluten extract
            (r"\b(maple\s+syrup|sirop\s+d[''e]\s*[ée]rable)\b", "syrup"),  # only maple syrup is NOVA 2
            (r"\b(vinegar|vinaigre)\b", "vinegar"),
            (r"\b(molasses|m[ée]lasse)\b", "molasses"),
        ]
        for pattern, ingredient_type in nova2_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return 2, f"nova2_{ingredient_type}", 0.95

        # Priority 0.6: FoodOn refined food product detection
        # Items classified as "refined food product" in FoodOn are typically NOVA 2
        if self.foodon_extractor:
            term_id = self.foodon_extractor.lookup_foodon_term(name)
            if term_id:
                from foodon_loader import FOODON_REFINED_CATEGORIES

                ancestors = self.foodon_extractor.get_ancestors(term_id)
                for refined_id, nova in FOODON_REFINED_CATEGORIES.items():
                    if refined_id in ancestors:
                        return nova, f"FoodOn refined: {refined_id}", 0.90

        # Priority 1: Activity name location keywords (highest priority - explicit source)
        # "at farm gate", "at farm", "at orchard", "at landing", "at greenhouse", "production" → NOVA 1
        for pattern in FARM_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return 1, "at_farm_source", 0.95

        # "at processing" for raw products (nuts, etc.) - still NOVA 1
        if re.search(r"\bat\s+processing\b", text, re.IGNORECASE):
            if re.search(r"\b(unshelled|raw|whole|fresh)\b", text, re.IGNORECASE):
                return 1, "raw_at_processing", 0.9

        # Priority 2.5: NOVA 3 processed food indicators (from name)
        nova3_patterns = [
            (r"\b(jam|marmalade|confiture)\b", "preserve"),
            (r"\b(pickled|pickle)\b", "pickled"),
            (r"\b(cured|salaison)\b", "cured"),
            (r"\b(smoked|fum[ée])\b", "smoked"),
            (r"\b(canned|conserve|appertis[ée])\b", "canned"),
            (r"\b(ham|jambon)\b", "cured_meat"),
            (r"\bbacon\b", "cured_meat"),  # lard is NOVA 2, not here
            (r"\b(sausage|saucisse)\b", "processed_meat"),
        ]
        for pattern, reason in nova3_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return 3, f"nova3_{reason}", 0.9

        # Priority 3: NOVA 4 ultra-processed indicators
        nova4_patterns = [
            (r"\btextured\b", "textured"),
            (r"\brehydrated\b", "rehydrated"),
            (r"\binstant\b", "instant"),
            (r"\bprotein\s+isolate\b", "isolate"),
            (r"\bhydrolyzed\s+protein\b", "hydrolyzed"),
            (r"\bgluten\s+meal\b", "isolate"),
            (r"\bdistill", "distilled"),
            (r"\bhigh\s+fructose\b", "industrial_sugar"),
            (r"\binvert\s+sugar\b", "industrial_sugar"),
            (r"\bcorn\s+syrup\b", "industrial_sugar"),
            (r"\bglucose\s+syrup\b", "industrial_sugar"),
            (r"\bmaltodextrin\b", "industrial_additive"),
        ]
        for pattern, reason in nova4_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return 4, f"nova4_{reason}", 0.9

        # Check for "at plant" processing
        if re.search(r"\bat\s+plant\b", text, re.IGNORECASE):
            # Recipe at plant → NOVA 4
            if re.search(r"\brecipe\b", text, re.IGNORECASE):
                return 4, "recipe_at_plant", 0.85
            # Minimal processing at plant (gutted, filleted, cleaned) → still NOVA 1
            minimal_at_plant = ["gutted", "fillet", "cleaned", "washed", "peeled", "shelled"]
            if any(w in text for w in minimal_at_plant):
                return 1, "minimal_at_plant", 0.85
            # Fresh fruits, vegetables, spices at plant (packing/sorting/drying) → NOVA 1
            if food_type in {"fruit", "vegetable", "fish_seafood", "spice_condiment"}:
                # Unless there's explicit heavy processing indicators
                processing_indicators = ["juice", "puree", "paste", "canned", "fermented"]
                if not any(w in text for w in processing_indicators):
                    return 1, "fresh_at_plant", 0.85
            # NOVA 2 product types (oil, sugar, flour, starch at plant)
            if food_type in {"nut_oilseed", "grain"}:
                if any(w in text for w in ["oil", "butter", "flour", "starch", "sugar", "semolina"]):
                    return 2, "extracted_at_plant", 0.85
            # Default: at plant without specific indicator → NOVA 3
            return 3, "at_plant_processed", 0.8

        # Priority 5: Packaging/preservation indicators
        if binary_features.get("is_canned"):
            return 3, "canned_preservation", 0.85
        # Only match "smoked" if it's the actual product, not "for smoked" (ingredient for)
        if binary_features.get("is_smoked") and not re.search(
            r"\bfor\s+smoked\b", text, re.IGNORECASE
        ):
            return 3, "smoked_preservation", 0.85

        # NOVA 1: Minimal processing indicators
        if binary_features.get("is_frozen") or binary_features.get("is_dried"):
            if not binary_features.get("is_processed"):
                return 1, "minimal_processing", 0.75

        # Priority 6: FoodType-based defaults (before nearest neighbor for reliable types)
        raw_default_types = {"vegetable", "fruit", "meat", "fish_seafood"}
        if food_type in raw_default_types:
            return 1, f"default_{food_type}", 0.7

        if food_type == "spice_condiment":
            # Dried spices are minimal processing (NOVA 1)
            return 1, "spice_condiment", 0.7

        if food_type == "dairy":
            # Milk and fermented milk products are NOVA 1
            if re.search(r"\b(milk|lait|yogurt|yaourt|yoghurt)\b", text, re.IGNORECASE):
                return 1, "dairy_minimal", 0.7
            # Cheese is NOVA 3
            return 3, "default_dairy_processed", 0.6

        if food_type == "grain":
            if re.search(r"\b(grain|seed|graine)\b", text, re.IGNORECASE):
                return 1, "raw_grain", 0.7
            return 3, "grain_processed", 0.6

        if food_type == "beverage":
            if re.search(r"\b(water|eau)\b", text, re.IGNORECASE):
                return 1, "water", 0.9
            if re.search(r"\b(juice|jus)\b", text, re.IGNORECASE):
                return 1, "fruit_juice", 0.7
            # Roasted coffee/tea are minimal processing (NOVA 1)
            if re.search(r"\b(coffee|caf[ée]|tea|th[ée])\b", text, re.IGNORECASE):
                return 1, "beverage_minimal", 0.7
            if re.search(r"\b(wine|vin|beer|bi[eè]re|cider|cidre)\b", text, re.IGNORECASE):
                return 3, "alcoholic_beverage", 0.8
            return 4, "industrial_beverage", 0.6

        # Priority 7: Nearest neighbor matching (fallback)
        if self.nova_matcher:
            matched_nova, match_conf, match_name, match_source = (
                self.nova_matcher.predict(name, translate_fn=self._translate)
            )
            if match_conf >= 0.8:  # Higher threshold for fallback
                return int(matched_nova), f"matched_{match_name}", match_conf

        # Default: NOVA 1 (unprocessed)
        return 1, "default_raw", 0.5

    def _build_matcher(
        self, names: list, values: list, sources: list
    ) -> NearestNeighborMatcher:
        """Build a NearestNeighborMatcher with common configuration."""
        return NearestNeighborMatcher(
            names,
            values,
            sources=sources,
            translate_fn=self._translate,
            foodon_extractor=self.foodon_extractor,
        )

    def fit(self, ingredients: list[dict], verbose: bool = True):
        """
        Train the predictor on a list of ingredients.

        Args:
            ingredients: List of dicts with at least "name" and "activityName"
        """

        def timed_print(msg, start_time=[None]):
            if start_time[0] is not None:
                elapsed = time.time() - start_time[0]
                print(f"  [{elapsed:.1f}s]")
            print(msg, end="", flush=True)
            start_time[0] = time.time()

        self._load_translation_model()
        self._load_foodon()

        if verbose:
            timed_print(f"Training on {len(ingredients)} ingredients...\n")

        # 1. Pre-translate all ingredient names (batch for performance)
        cache_size_before = len(self._translation_cache)
        if verbose:
            timed_print(f"Translating ingredient names ({cache_size_before} cached)...")
        translated_names = [self._translate(ing.get("name", "")) for ing in ingredients]
        cache_hits = cache_size_before
        cache_misses = len(self._translation_cache) - cache_size_before
        if verbose and cache_misses > 0:
            print(f" ({cache_hits} hits, {cache_misses} new)", end="")

        # 2. Extract features for all ingredients
        if verbose:
            timed_print("Extracting features...")

        features_list = []
        for i, ing in enumerate(ingredients):
            activity = ing.get("activityName", "")
            feat = extract_features(
                translated_names[i],
                activity,
                translate_fn=None,  # Already translated
                foodon_extractor=self.foodon_extractor,
            )
            features_list.append(feat)

        self.training_features = np.array(features_list)
        self.training_ingredients = ingredients
        self.feature_dim = self.training_features.shape[1]

        # 3. Build foodType matcher (nearest neighbor)
        # Use ONLY reference data from food_type.csv - NOT ingredients.json
        if verbose:
            timed_print("Building foodType matcher...")

        ref_food_names, ref_food_types, ref_food_sources = _load_food_type_data()

        self.food_type_matcher = self._build_matcher(
            ref_food_names, ref_food_types, ref_food_sources
        )

        # 3b. Build NOVA matcher (nearest neighbor on reference data only)
        if verbose:
            timed_print("Building NOVA matcher...")

        nova_names, nova_groups, nova_sources = _load_nova_data()
        if nova_names:
            # Convert string novaGroup to int
            nova_groups = [int(g) for g in nova_groups]
            self.nova_matcher = self._build_matcher(nova_names, nova_groups, nova_sources)

        # 4. Build cropGroup matcher (nearest neighbor)
        if verbose:
            timed_print("Building cropGroup matcher...")

        # Start with reference data from cropgroup.csv
        cropgroup_names, cropgroup_vals, cropgroup_sources = _load_cropgroup_data()

        # Add training data from ingredients with cropGroup
        ing_cg_names, ing_cg_vals, ing_cg_sources = _build_cropgroup_data(ingredients)
        cropgroup_names.extend(ing_cg_names)
        cropgroup_vals.extend(ing_cg_vals)
        cropgroup_sources.extend(ing_cg_sources)

        if cropgroup_names:
            self.cropgroup_matcher = self._build_matcher(
                cropgroup_names, cropgroup_vals, cropgroup_sources
            )

        # 5. Build transportCooling matcher (combines ingredients.json + reference data)
        if verbose:
            timed_print("Building transportCooling matcher...")

        transport_names = [ing["name"] for ing in ingredients]
        y_transport = [ing.get("transportCooling", "none") for ing in ingredients]
        transport_sources = ["ingredients.json"] * len(ingredients)

        ref_transport_names, ref_transport, ref_transport_sources = (
            _load_transport_data()
        )
        transport_names.extend(ref_transport_names)
        y_transport.extend(ref_transport)
        transport_sources.extend(ref_transport_sources)

        self.transport_matcher = self._build_matcher(
            transport_names, y_transport, transport_sources
        )

        # 6. Build nearest neighbor matchers for continuous values
        # Each matcher combines ingredients.json + reference CSV data

        def build_value_matcher(
            field, ref_loader, allow_zero=False, name=None, skip_ingredients=False
        ):
            """Helper to build a matcher combining reference + ingredients data.

            Reference data comes FIRST so it has priority in word matches.

            Args:
                skip_ingredients: If True, don't include ingredients.json data.
                    Use this for fields where ingredients.json values are generated
                    (not manually curated) and shouldn't be used for training.
            """
            if verbose:
                timed_print(f"Building {name or field} matcher...")
            # Reference data first (has priority in matches)
            names, vals, sources = ref_loader()
            # Then training ingredients (unless skipped)
            if not skip_ingredients:
                ing_names, ing_vals, ing_sources = _extract_ingredient_values(
                    ingredients, field, allow_zero=allow_zero
                )
                names.extend(ing_names)
                vals.extend(ing_vals)
                sources.extend(ing_sources)
            return self._build_matcher(names, vals, sources)

        self.density_matcher = build_value_matcher(
            "density", _load_density_data, name="density"
        )
        # Skip ingredients.json for inediblePart - values are generated, not curated
        self.inedible_matcher = build_value_matcher(
            "inediblePart",
            _load_inedible_data,
            allow_zero=True,
            name="inedible part",
            skip_ingredients=True,
        )
        # Skip ingredients.json for rawToCookedRatio - values are generated, not curated
        self.ratio_matcher = build_value_matcher(
            "rawToCookedRatio",
            _load_ratio_data,
            name="raw-to-cooked ratio",
            skip_ingredients=True,
        )

        self.is_fitted = True

        # Save translation cache to disk for faster subsequent runs
        self._save_translation_cache()

        if verbose:
            timed_print("✓ Training complete!\n")

    def _get_foodtype_from_foodon_features(self, features: np.ndarray) -> str | None:
        """
        Get food type directly from FoodOn features (indices 0-9).
        Returns None if no clear FoodOn signal.

        Features layout (from foodon_loader.py):
        - [0]=vegetable, [1]=fruit, [2]=grain, [3]=meat,
        - [4]=fish, [5]=dairy, [6]=nut_oilseed, [7]=spice, [8]=beverage, [9]=legume
        - [20]=match_confidence
        """
        # Check confidence threshold (FoodOn match confidence is at features[20])
        foodon_confidence = features[20] if len(features) > 20 else 0
        if foodon_confidence < 0.7:  # Require decent FoodOn match
            return None

        # Check in priority order: more specific types first
        # (fruit/legume before vegetable, since they are subtypes of plant food in FoodOn)
        priority_order = [
            (1, "fruit"),         # Check fruit BEFORE vegetable
            (9, "legume"),        # Check legume BEFORE vegetable
            (2, "grain"),
            (3, "meat"),          # Meat (vertebrate material, excluding fish)
            (4, "fish_seafood"),
            (5, "dairy"),
            (6, "nut_oilseed"),
            (7, "spice_condiment"),
            (8, "beverage"),
            (0, "vegetable"),     # Check vegetable LAST (it's the most generic plant category)
        ]

        for idx, food_type in priority_order:
            if features[idx] > 0.5:  # Feature is active
                return food_type

        return None

    def predict(self, ingredient: dict) -> dict:
        """
        Predict metadata for a new ingredient.

        Args:
            ingredient: Dict with "name" and optionally "activityName"

        Returns:
            Dict with predicted values and match info (including confidence)
        """
        if not self.is_fitted:
            raise RuntimeError(
                "Predictor must be fitted before prediction. Call fit() first."
            )

        self._load_translation_model()
        self._load_foodon()

        name = ingredient.get("name", "")
        activity = ingredient.get("activityName", "")
        full_text = f"{name} {activity}"

        # Extract features (with translation and FoodOn)
        features = extract_features(
            name,
            activity,
            translate_fn=self._translate,
            foodon_extractor=self.foodon_extractor,
        ).reshape(1, -1)

        predictions = {}

        # Extract binary features for rules
        binary_features = self._extract_binary_from_features(features)

        # 1. FoodType - Check food_type.csv exact match FIRST, then FoodOn, then fallback
        food_type, conf, match_name, source = self.food_type_matcher.predict(
            name, translate_fn=self._translate
        )
        # Validate high-confidence match (sparse vectors can give 1.0 for unrelated items)
        is_exact = conf == 1.0 and self._is_related_match(name, match_name)
        is_text_match = conf >= 0.95 and self._is_related_match(name, match_name)

        if is_exact:
            predictions["foodTypeMatch"] = _match(
                f"{match_name} found in {source}", conf
            )
        elif is_text_match:
            predictions["foodTypeMatch"] = _match(
                f"Matched with {match_name} in {source}", conf
            )
        else:
            # Try FoodOn features
            foodon_type = self._get_foodtype_from_foodon_features(features.flatten())
            if foodon_type:
                food_type = foodon_type
                predictions["foodTypeMatch"] = _match(
                    f"FoodOn ontology: {food_type} features detected", 1.0
                )
            else:
                # Default fallback (no trusted match)
                food_type = "vegetable"
                predictions["foodTypeMatch"] = _match(
                    f"Default: {food_type} (no trusted match for {match_name})", 0.5
                )
        predictions["foodType"] = food_type

        # 2. NOVA Classification (determines processingState)
        nova_group, nova_reason, nova_confidence = self._classify_nova(
            name, activity, food_type, binary_features
        )
        predictions["novaGroup"] = nova_group
        predictions["novaGroupMatch"] = _match(f"{nova_reason} → NOVA {nova_group}", nova_confidence)

        # Derive processingState from NOVA
        # NOVA 1 = raw (unprocessed/minimally processed)
        # NOVA 2, 3, 4 = processed
        processing_state = "raw" if nova_group == 1 else "processed"
        if processing_state == "processed":
            print(f"WARNING: '{name}' classified as processed (NOVA {nova_group}): '{activity}'")
        predictions["processingState"] = processing_state
        predictions["processingStateMatch"] = _match(
            f"Derived from NOVA {nova_group} → {processing_state}", 1.0
        )

        # Packaging detection
        packaging, _ = self._detect_packaging(full_text)
        predictions["packaging"] = packaging
        if packaging:
            predictions["packagingMatch"] = _match(
                f"{packaging} keyword found in {name}", 1.0
            )
        else:
            predictions["packagingMatch"] = _match("No packaging keyword detected", 1.0)

        # 3. Additive labels (by rules)
        labels = []
        if re.search(r"\b(bleu.?blanc.?c[oœ]eur)\b", name, re.IGNORECASE):
            labels.append("bleublanccoeur")
        predictions["labels"] = labels

        # 4. Build categories from foodType + novaGroup + labels
        # Note: "organic" is added at the variant level (ORG), not here
        base_category = self._compute_category(food_type, nova_group)
        predictions["categories"] = [base_category] + labels

        # 5. cropGroup (for vegetal types) - pattern-based inference with matcher fallback
        vegetal_types = {
            "vegetable",
            "fruit",
            "grain",
            "nut_oilseed",
            "spice_condiment",
            "legume",
            "beverage",
        }
        # Try pattern-based inference first (catches misclassified items like snow pea)
        pattern_result = self._infer_cropgroup_from_foodtype(name, activity, food_type)
        if pattern_result:
            cropgroup_val, pattern_desc = pattern_result
            predictions["cropGroup"] = cropgroup_val
            predictions["cropGroupMatch"] = _match(f"{pattern_desc}", 1.0)
        elif food_type in vegetal_types:
            if self.cropgroup_matcher is not None:
                # Fall back to nearest neighbor matcher
                cropgroup_val, conf, match_name, source = self.cropgroup_matcher.predict(
                    name, translate_fn=self._translate
                )
                predictions["cropGroup"] = cropgroup_val
                if conf == 1.0:
                    predictions["cropGroupMatch"] = _match(
                        f"{match_name} found in {source}", conf
                    )
                else:
                    predictions["cropGroupMatch"] = _match(
                        f"Matched with {match_name} in {source}", conf
                    )
            else:
                # No matcher available, use foodType default
                default = FOODTYPE_TO_CROPGROUP.get(food_type, "DIVERS")
                predictions["cropGroup"] = default
                predictions["cropGroupMatch"] = _match(f"{food_type} fallback", 0.5)
        else:
            predictions["cropGroup"] = None
            predictions["cropGroupMatch"] = _match("Not applicable (animal product)", 1.0)

        # 6. transportCooling (packaging first, then rules, then nearest neighbor)
        if packaging and packaging != "fresh":
            _, transport_cooling = PACKAGING_PATTERNS.get(packaging, (None, None))
            transport_cooling = transport_cooling or "none"
            predictions["transportCoolingMatch"] = _match(
                f"{packaging} packaging → {transport_cooling}", 1.0
            )
        else:
            transport_cooling = self._predict_transport_by_rules(
                binary_features, food_type, nova_group
            )
            if transport_cooling:
                # Rule-based prediction
                perishable = food_type in {"meat", "fish_seafood", "dairy", "vegetable", "fruit", "legume"}
                if nova_group == 1 and perishable:
                    predictions["transportCoolingMatch"] = _match(
                        f"{food_type} NOVA 1 → {transport_cooling} (perishable)", 1.0
                    )
                else:
                    predictions["transportCoolingMatch"] = _match(
                        f"{food_type} → {transport_cooling} (non-perishable)", 1.0
                    )
            else:
                transport_cooling, conf, match_name, source = self.transport_matcher.predict(
                    name, translate_fn=self._translate
                )
                predictions["transportCoolingMatch"] = _match(
                    f"Matched with {match_name} in {source}", conf
                )
        predictions["transportCooling"] = transport_cooling

        # 7. Continuous values (nearest neighbor with foodType fallback)
        density_val, conf, match_name, source = self.density_matcher.predict(
            name, translate_fn=self._translate
        )
        # Use foodType default unless it's a real text match (exact or word boundary)
        # Feature similarity can give 1.0 for unrelated items with sparse vectors
        is_exact = conf == 1.0 and self._is_related_match(name, match_name)
        is_text_match = conf >= 0.95 and self._is_related_match(name, match_name)
        if is_exact:
            predictions["densityMatch"] = _match(f"{match_name} found in {source}", conf)
        elif is_text_match:
            predictions["densityMatch"] = _match(
                f"Matched with {match_name} in {source}", conf
            )
        else:
            density_val, source_file = self._get_default_density(food_type)
            predictions["densityMatch"] = _match(
                f"{food_type} default from {source_file}", 1.0
            )
        predictions["density"] = round(density_val, 3)

        # 9. InediblePart (keywords first, then matcher with validation, then default)
        keyword_result = self._detect_inedible_from_keywords(name, activity)
        if keyword_result is not None:
            # Keyword-based (fillet, shelled, etc.) - high confidence
            inedible_val, keyword = keyword_result
            predictions["inediblePart"] = round(inedible_val, 2)
            predictions["inediblePartMatch"] = _match(
                f"{keyword} keyword found in {name}", 1.0
            )
        else:
            inedible_val, conf, match_name, source = self.inedible_matcher.predict(
                name, translate_fn=self._translate
            )
            # Validate matcher result (must share words to avoid sparse vector issues)
            is_valid = conf >= 0.95 and self._is_related_match(name, match_name)

            # If no valid match with name, try with activity name (extract food item)
            # e.g., "Blackberry" from "Blackberry, at farm {RS} - Adapted..."
            if not is_valid and activity:
                activity_food = activity.split(",")[0].split("{")[0].strip()
                if activity_food and activity_food.lower() != name.lower():
                    act_val, act_conf, act_match, act_source = (
                        self.inedible_matcher.predict(
                            activity_food, translate_fn=self._translate
                        )
                    )
                    act_valid = act_conf >= 0.95 and self._is_related_match(
                        activity_food, act_match
                    )
                    if act_valid:
                        inedible_val, conf, match_name, source = (
                            act_val,
                            act_conf,
                            act_match,
                            act_source,
                        )
                        is_valid = True

            if is_valid:
                predictions["inediblePart"] = round(inedible_val, 2)
                predictions["inediblePartMatch"] = _match(
                    f"Matched with {match_name} in {source}", conf
                )
            else:
                # Fall back to foodType/novaGroup default
                default_inedible, source_file = self._get_default_inedible_part(
                    food_type, nova_group
                )
                nova_desc = "NOVA 1" if nova_group == 1 else "NOVA 2-4"
                predictions["inediblePart"] = round(default_inedible, 2)
                predictions["inediblePartMatch"] = _match(
                    f"{food_type} {nova_desc} default from {source_file}", 1.0
                )

        # 10. rawToCookedRatio (keywords first, then matcher with validation, then default)
        keyword_result = self._detect_ratio_from_keywords(name, activity, food_type)
        if keyword_result is not None:
            # Keyword-based (poultry, offal, dried) - high confidence
            ratio_val, keyword = keyword_result
            predictions["rawToCookedRatio"] = round(ratio_val, 3)
            predictions["rawToCookedRatioMatch"] = _match(
                f"{keyword} keyword found in {name}", 1.0
            )
        else:
            ratio_val, conf, match_name, source = self.ratio_matcher.predict(
                name, translate_fn=self._translate
            )
            # Validate matcher result (must share words to avoid sparse vector issues)
            is_valid = conf >= 0.95 and self._is_related_match(name, match_name)
            if is_valid:
                predictions["rawToCookedRatio"] = round(ratio_val, 3)
                predictions["rawToCookedRatioMatch"] = _match(
                    f"Matched with {match_name} in {source}", conf
                )
            else:
                # Fall back to foodType default (Agribalyse values)
                default_ratio, source_file = self._get_default_ratio(food_type)
                predictions["rawToCookedRatio"] = round(default_ratio, 3)
                predictions["rawToCookedRatioMatch"] = _match(
                    f"{food_type} default from {source_file}", 1.0
                )

        return predictions

    def evaluate(self, verbose: bool = True) -> dict:
        """
        Evaluate predictor with cross-validation on training data.

        Returns:
            Dict with scores per metadata field
        """
        if not self.is_fitted:
            raise RuntimeError("Predictor must be fitted before evaluation.")

        def _cv_score(y_values: list, field_name: str, X=None, cv=5) -> dict:
            """Run cross-validation and return {mean, std}."""
            features = X if X is not None else self.training_features
            encoder = LabelEncoder()
            y_encoded = encoder.fit_transform(y_values)
            cv_scores = cross_val_score(
                RandomForestClassifier(
                    n_estimators=100, class_weight="balanced", random_state=42
                ),
                features,
                y_encoded,
                cv=cv,
                scoring="accuracy",
            )
            if verbose:
                print(
                    f"{field_name} accuracy: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}"
                )
            return {"mean": cv_scores.mean(), "std": cv_scores.std()}

        scores = {}

        # Extract foodType and processingState from categories
        y_food = []
        y_proc = []
        for ing in self.training_ingredients:
            base_cat = self._get_base_category(ing.get("categories", ["misc"]))
            food_type, proc_state = CATEGORY_TO_DIMENSIONS.get(
                base_cat, ("misc", "processed")
            )
            y_food.append(food_type)
            y_proc.append(proc_state)

        scores["foodType"] = _cv_score(y_food, "FoodType")
        scores["processingState"] = _cv_score(y_proc, "ProcessingState")
        scores["transportCooling"] = _cv_score(
            [ing.get("transportCooling", "none") for ing in self.training_ingredients],
            "TransportCooling",
        )

        # Evaluate cropGroup (vegetables only, using RandomForest on embeddings)
        cropgroup_names, cropgroup_vals, _ = _build_cropgroup_data(
            self.training_ingredients
        )
        if len(cropgroup_names) > 10:
            self._load_embedding_model()
            X_crop = self.model.encode(cropgroup_names)
            scores["cropGroup"] = _cv_score(
                cropgroup_vals,
                "CropGroup",
                X=X_crop,
                cv=min(5, len(set(cropgroup_vals))),
            )

        return scores

    def save(self, path: str):
        """Save the trained predictor."""
        if not self.is_fitted:
            raise RuntimeError("Cannot save unfitted predictor.")

        # Clear unpicklable references from matchers
        matchers = [
            self.food_type_matcher,
            self.transport_matcher,
            self.cropgroup_matcher,
            self.density_matcher,
            self.inedible_matcher,
            self.ratio_matcher,
        ]
        for matcher in matchers:
            if matcher is not None:
                matcher.foodon_extractor = None
                matcher.translate_fn = None  # Can't pickle bound methods

        # Don't save embedding model or FoodOn (reloaded lazily)
        state = {
            # Categorical matchers (nearest neighbor approach)
            "food_type_matcher": self.food_type_matcher,
            "transport_matcher": self.transport_matcher,
            # CropGroup matcher (nearest neighbor)
            "cropgroup_matcher": self.cropgroup_matcher,
            # Value matchers (nearest neighbor on reference data)
            "density_matcher": self.density_matcher,
            "inedible_matcher": self.inedible_matcher,
            "ratio_matcher": self.ratio_matcher,
            # Translation cache (avoid re-translating on reload)
            "_translation_cache": self._translation_cache,
            # Training data (for evaluation)
            "training_features": self.training_features,
            "training_ingredients": self.training_ingredients,
            "feature_dim": self.feature_dim,
            "is_fitted": True,
            # FoodOn state (extractor is reloaded lazily)
            "_foodon_loaded": False,
        }

        with open(path, "wb") as f:
            pickle.dump(state, f)

        print(f"✓ Predictor saved to {path}")

    @classmethod
    def load(cls, path: str) -> "Predictor":
        """Load a saved predictor."""
        predictor = cls()

        with open(path, "rb") as f:
            state = pickle.load(f)

        for key, value in state.items():
            setattr(predictor, key, value)

        print(f"✓ Predictor loaded from {path}")
        return predictor


# =============================================================================
# INTEGRATION with ecobalyse_data/detect
# =============================================================================

# For compatibility with existing pattern
THRESHOLD = 0.6
SCORE_KEY = "predict_Score"
MATCH_KEY = "predict_BestMatch"
BAD, GOOD = 0.5, 0.8

_predictor_instance: Optional[Predictor] = None


def _name(obj):
    return obj.get("name", "")


def _get(obj):
    return obj.get("categories")


def _set(obj, predictions):
    """Apply predictions to the object."""
    for key, value in predictions.items():
        if value is not None:
            obj[key] = value


class Detector:
    """Interface compatible with other detectors."""

    def __init__(
        self, model_path: Optional[str] = None, training_data: Optional[list] = None
    ):
        """
        Args:
            model_path: Path to a saved model
            training_data: Training data (if no saved model provided)
        """
        self.predictor = Predictor()

        if model_path and Path(model_path).exists():
            self.predictor = Predictor.load(model_path)
        elif training_data:
            self.predictor.fit(training_data)
        else:
            raise ValueError("Either model_path or training_data must be provided")

    def detect(self, ingredient, debug=False):
        """
        Predict metadata for an ingredient.

        Returns:
            (predictions, score, best_match)
        """
        predictions = self.predictor.predict(ingredient)

        # Extract confidence from match dicts
        def _get_conf(match_key):
            match = predictions.get(match_key)
            return match.get("confidence", 0) if match else 0

        # Score global = moyenne des confiances
        score = np.mean(
            [
                _get_conf("densityMatch"),
                _get_conf("inediblePartMatch"),
                _get_conf("rawToCookedRatioMatch"),
            ]
        )

        best_match = f"density={predictions.get('density')}, inedible={predictions.get('inediblePart')}"

        return predictions, score, best_match


def update(input_json, threshold=THRESHOLD, debug=False, model_path=None):
    """
    Update metadata for all ingredients.

    Compatible with existing CLI.

    Args:
        input_json: List of ingredients
        threshold: Confidence threshold for predictions
        debug: Add debug info to output
        model_path: Path to saved model (optional)
    """
    from rich.progress import track

    # Filter ingredients that already have all metadata
    to_predict = [ing for ing in input_json if not _get(ing)]
    already_done = [ing for ing in input_json if _get(ing)]

    if not to_predict:
        print("All ingredients already have metadata.")
        return input_json

    # Train on existing ingredients if no model provided
    detector = Detector(model_path=model_path, training_data=already_done or input_json)

    output_json = list(already_done)

    print(f"Predicting metadata for {len(to_predict)} ingredients:")
    for ingredient in track(to_predict, description="Predicting"):
        predictions, score, best_match = detector.detect(ingredient, debug)

        if score >= threshold:
            _set(ingredient, predictions)

            if debug:
                ingredient[SCORE_KEY] = score
                ingredient[MATCH_KEY] = best_match

            output_json.append(ingredient)
        else:
            print(
                f"\n⚠️  Low confidence for '{_name(ingredient)}' "
                f"(score: {score:.2f}, best match: '{best_match}')"
            )
            output_json.append(ingredient)

    return output_json


def detect(ingredient, model_path=None, training_data=None):
    """Simple interface to detect a single ingredient."""
    detector = Detector(model_path=model_path, training_data=training_data)
    return detector.detect(ingredient)


# =============================================================================
# CLI
# =============================================================================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Predict ingredient metadata using ML")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Train command
    train_parser = subparsers.add_parser("train", help="Train predictor on ingredients")
    train_parser.add_argument(
        "input", type=str, help="Input JSON file with ingredients"
    )
    train_parser.add_argument(
        "--output", "-o", type=str, default="predictor.pkl", help="Output model file"
    )

    # Infer command
    infer_parser = subparsers.add_parser(
        "infer", help="Predict metadata for new ingredient"
    )
    infer_parser.add_argument("model", type=str, help="Model file (.pkl)")
    infer_parser.add_argument(
        "--name", "-n", type=str, required=True, help="Ingredient name"
    )
    infer_parser.add_argument(
        "--activity", "-a", type=str, required=True, help="Activity/process name"
    )

    # Evaluate command
    eval_parser = subparsers.add_parser(
        "evaluate", help="Evaluate predictor with cross-validation"
    )
    eval_parser.add_argument("input", type=str, help="Input JSON file with ingredients")

    args = parser.parse_args()

    if args.command == "train":
        with open(args.input) as f:
            ingredients = json.load(f)

        predictor = Predictor()
        predictor.fit(ingredients)
        predictor.save(args.output)

    elif args.command == "infer":
        predictor = Predictor.load(args.model)

        ingredient = {
            "name": args.name,
            "activityName": args.activity,
        }

        predictions = predictor.predict(ingredient)

        print("\n📊 Predictions:")
        for key, value in predictions.items():
            if key.endswith("Match"):
                continue  # Skip match info in summary
            match_key = f"{key}Match"
            match = predictions.get(match_key)
            if match and match.get("confidence"):
                print(
                    f"  {key}: {value} (conf: {match['confidence']:.2f}, match: {match['name']})"
                )
            else:
                print(f"  {key}: {value}")

    elif args.command == "evaluate":
        with open(args.input) as f:
            ingredients = json.load(f)

        predictor = Predictor()
        predictor.fit(ingredients)

        print("\n📈 Cross-validation results:")
        predictor.evaluate()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
