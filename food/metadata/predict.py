"""
ecobalyse_data/detect/predict.py
================================

Prédit TOUTES les métadonnées d'un nouvel ingrédient à partir de :
- Son nom (français)
- Son procédé ACV (activityName)

Utilise un ensemble de classifieurs entraînés sur les ingrédients existants.

Usage:
    from ecobalyse_data.detect import predict

    # Entraînement (une seule fois)
    predictor = predict.Predictor()
    predictor.fit(existing_ingredients)  # liste de dicts
    predictor.save("models/ingredient_predictor.pkl")

    # Prédiction
    predictor = predict.Predictor.load("models/ingredient_predictor.pkl")
    new_ingredient = {
        "name": "Tomate cerise bio",
        "activityName": "Cherry tomato, organic {FR} U"
    }
    predictions = predictor.predict(new_ingredient)
    # → {"categories": ["vegetable_fresh", "organic"], "cropGroup": "LEGUMES-FLEURS", ...}

CLI:
    # Entraîner sur les ingrédients existants
    python -m ecobalyse_data.detect.predict train ingredients.json --output model.pkl

    # Prédire pour un nouvel ingrédient
    python -m ecobalyse_data.detect.predict infer model.pkl --name "Tomate cerise" --activity "Cherry tomato {FR} U"

    # Évaluer en cross-validation
    python -m ecobalyse_data.detect.predict evaluate ingredients.json
"""

import json
import pickle
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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

MODEL = "intfloat/e5-base-v2"  # 192M params, 768 dims (was jonny9f/food_embeddings2)

# Translation cache file (persisted to disk for faster subsequent runs)
TRANSLATION_CACHE_PATH = Path(__file__).parent / ".translation_cache.pkl"
MT_MODEL = "Helsinki-NLP/opus-mt-fr-en"  # FR → EN Machine Translation

# Catégories de base (legacy - kept for backward compatibility during training)
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
    "fresh": (r"\b(frais|fra[iî]che|fresh)\b", None),  # None = depends on foodType
}

# Labels additifs (peuvent se combiner avec une catégorie de base)
ADDITIVE_LABELS = ["organic", "bleublanccoeur"]

TRANSPORT_COOLING_VALUES = ["none", "always", "once_transformed"]

# Mapping localisation → origine
ORIGIN_MAPPING = {
    "FR": "France",
    "IT": "EuropeAndMaghreb",
    "ES": "EuropeAndMaghreb",
    "DE": "EuropeAndMaghreb",
    "BE": "EuropeAndMaghreb",
    "NL": "EuropeAndMaghreb",
    "PT": "EuropeAndMaghreb",
    "GR": "EuropeAndMaghreb",
    "PL": "EuropeAndMaghreb",
    "AT": "EuropeAndMaghreb",
    "DZ": "EuropeAndMaghreb",  # Algérie
    "MA": "EuropeAndMaghreb",  # Maroc
    "TN": "EuropeAndMaghreb",  # Tunisie
    "GLO": "OutOfEuropeAndMaghreb",
    "RoW": "OutOfEuropeAndMaghreb",
    "WI": "OutOfEuropeAndMaghreb",  # West Indies
    "BR": "OutOfEuropeAndMaghreb",
    "CN": "OutOfEuropeAndMaghreb",
    "IN": "OutOfEuropeAndMaghreb",
    "US": "OutOfEuropeAndMaghreb",
}


# =============================================================================
# REFERENCE DATA FOR VALUE CLASSIFIERS
# =============================================================================

# Paths to reference data files (relative to ecobalyse_data package)
DENSITY_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "density.csv"
INEDIBLE_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "inedible_part.csv"
RATIO_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "cooked_to_raw.csv"


def _load_density_data() -> tuple[list, list]:
    """Load density.csv, return (names, values)."""
    df = pd.read_csv(DENSITY_DATA_PATH)
    # Columns: name, density, source
    names = df["name"].tolist()
    values = df["density"].tolist()
    return names, values


def _load_inedible_data() -> tuple[list, list]:
    """Load inedible_part.csv, return (names, inedible_values)."""
    df = pd.read_csv(INEDIBLE_DATA_PATH, sep=";", decimal=",")
    # Columns: category;name;inedible_part (value is already inedible part)
    names = []
    values = []
    for _, row in df.iterrows():
        name = str(row.iloc[1]).strip() if pd.notnull(row.iloc[1]) else ""
        if not name:
            continue
        inedible_part = row.iloc[2]
        if pd.notnull(inedible_part):
            names.append(name)
            values.append(float(inedible_part))

    return names, values


def _load_ratio_data() -> tuple[list, list]:
    """Load cooked_to_raw.csv, return (names, values)."""
    df = pd.read_csv(RATIO_DATA_PATH, sep=";")
    # Columns: food;value
    names = []
    values = []
    for _, row in df.iterrows():
        name = str(row["food"]).strip() if pd.notnull(row.get("food")) else ""
        if not name:
            continue
        value = row.get("value")
        if pd.notnull(value):
            names.append(name)
            values.append(float(value))

    return names, values


def _build_cropgroup_data(ingredients: list) -> tuple[list, list]:
    """Build (names, cropGroups) from training ingredients + cropGroup labels themselves."""
    names = []
    cropgroups = []

    # Add ingredient names as training points
    for ing in ingredients:
        if ing.get("cropGroup"):
            names.append(ing.get("name", ""))
            cropgroups.append(ing["cropGroup"])

    # Add cropGroup labels themselves as training points
    # e.g., "LEGUMES-FLEURS" → LEGUMES-FLEURS
    unique_cropgroups = set(cropgroups)
    for cg in unique_cropgroups:
        names.append(cg)  # The label itself
        cropgroups.append(cg)

    return names, cropgroups


class NearestNeighborMatcher:
    """Find nearest neighbor by cosine similarity using combined features (E5 + FoodOn + regex)."""

    def __init__(self, names: list, values: list, model, translate_fn=None, foodon_extractor=None):
        """
        Build a nearest neighbor matcher on combined features.

        Args:
            names: List of food names from reference data
            values: List of corresponding values (numeric or string)
            model: SentenceTransformer model for encoding
            translate_fn: Optional function to translate names before encoding
            foodon_extractor: Optional FoodOnFeatureExtractor for ontology features
        """
        self.names = list(names)
        self.values = list(values)  # Keep as list to support both numeric and string
        self.foodon_extractor = foodon_extractor

        # Translate names if translation function provided (cached)
        translated_names = list(names)
        if translate_fn:
            print(f"  Translating {len(names)} names (cached)...")
            translated_names = [translate_fn(n) for n in names]

        # Compute combined features for all reference names
        print(f"  Computing embeddings for {len(names)} reference items...")
        features_list = []
        for i, name in enumerate(names):
            # Use extract_features with FoodOn for combined features
            # Pass translated name for both embedding AND FoodOn (FoodOn is English-based)
            feat = extract_features(
                translated_names[i], "", model,
                foodon_extractor=foodon_extractor
            )
            # FoodOn is already extracted from translated name in extract_features
            # (FoodOn ontology uses English terms, so translation helps matching)
            features_list.append(feat)
        self.embeddings = np.array(features_list)
        print(f"  Nearest neighbor matcher ready ({len(names)} items)")

    def predict(self, query: str, model, translate_fn=None, foodon_extractor=None):
        """
        Find nearest neighbor and return its value.

        Args:
            query: Query string (ingredient name)
            model: Embedding model
            translate_fn: Optional translation function
            foodon_extractor: Optional FoodOn extractor (uses stored one if not provided)

        Returns:
            (value, confidence, best_match_name) - value can be numeric or string
        """
        # Use stored foodon_extractor if not provided
        extractor = foodon_extractor if foodon_extractor is not None else self.foodon_extractor

        # Extract combined features for query
        query_features = extract_features(
            query, "", model, translate_fn=translate_fn,
            foodon_extractor=extractor
        ).reshape(1, -1)

        # Compute cosine similarities to all reference embeddings
        similarities = np.dot(self.embeddings, query_features.T).flatten()
        norms_ref = np.linalg.norm(self.embeddings, axis=1)
        norm_query = np.linalg.norm(query_features)
        # Avoid division by zero
        valid_norms = (norms_ref > 0) & (norm_query > 0)
        similarities[valid_norms] = similarities[valid_norms] / (norms_ref[valid_norms] * norm_query)
        similarities[~valid_norms] = 0

        # Return value of closest match
        best_idx = int(np.argmax(similarities))
        value = self.values[best_idx]
        # Convert to float if numeric, otherwise keep as string
        if isinstance(value, (int, float, np.number)):
            value = float(value)
        return value, float(similarities[best_idx]), self.names[best_idx]


# =============================================================================
# FEATURE EXTRACTION
# =============================================================================

# Patterns de détection (français + anglais)
DETECTION_PATTERNS = {
    # Attributs de transformation
    "is_organic": r"\b(bio|organic|organique)\b",
    "is_fresh": r"\b(frais|fraîche|fraiche|fresh)\b",
    "is_frozen": r"\b(surgelé|surgelee|congelé|congelee|frozen)\b",
    "is_cooked": r"\b(cuit|cuite|cuire|cooked|roasted|grillé|grillee|rôti|rotie|bouilli|poché|pochee|frit|frite)\b",
    "is_raw": r"\b(cru|crue|raw|brut|brute)\b",
    "is_dried": r"\b(séché|sechee|sec|sèche|seche|dried|déshydraté|deshydratee)\b",
    "is_processed": r"\b(transformé|transformee|processed|préparé|preparee|industriel|conserve)\b",
    "is_canned": r"\b(conserve|appertisé|appertisee|canned)\b",
    "is_smoked": r"\b(fumé|fumee|smoked)\b",
    # Types d'aliments - Animaux
    "is_meat": r"\b(viande|meat|boeuf|beef|porc|pork|veau|veal|agneau|lamb|mouton|mutton|poulet|chicken|dinde|turkey|canard|duck|lapin|rabbit|gibier|game)\b",
    "is_fish": r"\b(poisson|pêche|fish|cabillaud|cod|saumon|salmon|thon|tuna|sardine|maquereau|mackerel|truite|trout|bar|bass|dorade|bream|merlu|hake|sole|anchois|anchovy)\b",
    "is_seafood": r"\b(fruit.{0,3}mer|seafood|crevette|shrimp|prawn|crabe|crab|homard|lobster|moule|mussel|huître|huitre|oyster|coquillage|shellfish|calmar|squid|poulpe|octopus)\b",
    "is_egg": r"\b(oeuf|œuf|egg)\b",
    "is_dairy": r"\b(lait|milk|fromage|cheese|yaourt|yogurt|yoghurt|crème|cream|beurre|butter|lactose|dairy)\b",
    # Types d'aliments - Végétaux
    "is_vegetable": r"\b(légume|legume|vegetable|carotte|carrot|tomate|tomato|courgette|zucchini|aubergine|eggplant|poivron|pepper|oignon|onion|ail|garlic|pomme.{0,3}terre|potato|haricot|bean|petit.{0,3}pois|pea|épinard|spinach|salade|salad|laitue|lettuce|chou|cabbage|brocoli|broccoli|céleri|celery|concombre|cucumber|radis|radish|navet|turnip|betterave|beet|artichaut|artichoke|asperge|asparagus|fenouil|fennel|poireau|leek)\b",
    "is_fruit": r"\b(fruit|pomme|apple|poire|pear|orange|citron|lemon|banane|banana|fraise|strawberry|framboise|raspberry|cerise|cherry|pêche|peche|peach|abricot|apricot|prune|plum|raisin|grape|melon|pastèque|watermelon|mangue|mango|ananas|pineapple|kiwi|figue|fig|datte|date|grenade|pomegranate|papaye|papaya|litchi|lychee|avocat|avocado)\b",
    "is_grain": r"\b(céréale|cereale|cereal|grain|blé|ble|wheat|riz|rice|maïs|mais|corn|orge|barley|avoine|oat|seigle|rye|épeautre|epeautre|spelt|sarrasin|buckwheat|quinoa|millet|sorgho|sorghum|farine|flour|semoule|semolina|pâte|pate|pasta)\b",
    "is_legume": r"\b(légumineuse|legumineuse|legume|légume.{0,3}sec|lentille|lentil|pois|pea|haricot.{0,3}sec|dried.{0,3}bean|fève|feve|fava|pois.{0,3}chiche|chickpea|soja|soy|lupin)\b",
    "is_nut_seed": r"\b(noix|nut|walnut|amande|almond|noisette|hazelnut|pistache|pistachio|cacahuète|cacahuete|peanut|cajou|cashew|pécan|pecan|macadamia|graine|seed|tournesol|sunflower|sésame|sesame|lin|flax|chia|courge|pumpkin|chanvre|hemp|pignon|pine.{0,3}nut)\b",
    "is_oil_fat": r"\b(huile|oil|graisse|fat|margarine|olive|colza|rapeseed|tournesol|sunflower|arachide|peanut|palme|palm|coco|coconut|noix|walnut|sésame|sesame)\b",
    "is_spice": r"\b(épice|epice|spice|herbe|herb|aromate|poivre|pepper|sel|salt|sucre|sugar|cannelle|cinnamon|curcuma|turmeric|gingembre|ginger|paprika|curry|cumin|coriandre|coriander|basilic|basil|thym|thyme|romarin|rosemary|persil|parsley|menthe|mint|aneth|dill|origan|oregano|laurier|bay|muscade|nutmeg|clou.{0,3}girofle|clove|safran|saffron|vanille|vanilla)\b",
    "is_beverage": r"\b(boisson|beverage|drink|jus|juice|café|cafe|coffee|thé|the|tea|vin|wine|bière|biere|beer|alcool|alcohol|eau|water|soda|limonade|lemonade)\b",
    "is_sugar_sweet": r"\b(sucre|sugar|miel|honey|sirop|syrup|confiture|jam|chocolat|chocolate|bonbon|candy|gâteau|gateau|cake|biscuit|cookie|dessert|pâtisserie|patisserie|pastry)\b",
    # Infos procédé ACV
    "at_farm_gate": r"\bat\s+(farm\s+)?gate\b",
    "at_plant": r"\bat\s+plant\b",
    "at_processing": r"\bat\s+processing\b",
    "is_greenhouse": r"\b(greenhouse|serre)\b",
    "is_heated_greenhouse": r"\b(heated\s+greenhouse|serre\s+chauffée|serre\s+chauffee)\b",
}

# Index des features binaires dans le vecteur (après les 768 dims d'embedding)
BINARY_FEATURE_NAMES = list(DETECTION_PATTERNS.keys())
EMBEDDING_DIM = 768  # E5-base-v2 output dimension

# FoodOn feature dimension (loaded from foodon_loader)
FOODON_DIM = 20

# Scale factors for equal contribution in cosine similarity
# E5 has 768 dims, we scale smaller feature vectors to have similar weight
# sqrt(768 / N) gives each feature set equal total contribution
import math
FOODON_SCALE = math.sqrt(EMBEDDING_DIM / FOODON_DIM)  # ~6.2
REGEX_SCALE = math.sqrt(EMBEDDING_DIM / len(DETECTION_PATTERNS))  # ~5.5


def _extract_location(activity_name: str) -> Optional[str]:
    """Extrait le code de localisation du nom du procédé ACV."""
    # Pattern: {FR}, {RoW}, {GLO}, etc.
    match = re.search(r"\{([A-Z]{2,3})\}", activity_name)
    if match:
        return match.group(1)

    # Pattern alternatif: /FR U, /IT U, etc.
    match = re.search(r"/([A-Z]{2})\s*U\b", activity_name)
    if match:
        return match.group(1)

    return None


def _extract_origin(activity_name: str) -> str:
    """Détermine l'origine à partir du nom du procédé."""
    location = _extract_location(activity_name)
    if location:
        return ORIGIN_MAPPING.get(location, "OutOfEuropeAndMaghreb")

    # Patterns textuels
    activity_lower = activity_name.lower()
    if "by plane" in activity_lower or "by air" in activity_lower:
        return "OutOfEuropeAndMaghrebByPlane"
    if any(x in activity_lower for x in ["france", "french"]):
        return "France"
    if any(
        x in activity_lower for x in ["europe", "eu ", "italian", "spanish", "german"]
    ):
        return "EuropeAndMaghreb"

    return "OutOfEuropeAndMaghreb"


def extract_features(
    name: str, activity_name: str, embedding_model, translate_fn=None,
    foodon_extractor=None
) -> np.ndarray:
    """
    Extrait un vecteur de features combinant E5 + FoodOn + regex.

    Features vector structure:
    - [0:768] E5 embedding (L2 normalized)
    - [768:788] FoodOn features (scaled by ~6.2)
    - [788:813] Regex binary features (scaled by ~5.5)

    Args:
        name: Ingredient name (potentially French)
        activity_name: Activity/process name
        embedding_model: SentenceTransformer model
        translate_fn: Optional function to translate name before encoding
        foodon_extractor: Optional FoodOnFeatureExtractor for ontology features

    Returns:
        np.ndarray de dimension (768 + 20 + nb_patterns)
    """
    # Combine nom + activité pour le texte complet (keep original for regex matching)
    full_text = f"{name} {activity_name}".lower()

    # 1. E5 Embedding (768 dims) - translate first if available, then L2 normalize
    name_for_embedding = translate_fn(name) if translate_fn else name
    name_embedding = embedding_model.encode(name_for_embedding, convert_to_tensor=False)
    # L2 normalize embedding
    norm = np.linalg.norm(name_embedding)
    if norm > 0:
        name_embedding = name_embedding / norm

    # 2. FoodOn features (20 dims) - scaled for equal weight
    if foodon_extractor is not None:
        foodon_features = foodon_extractor.extract_features(name)
    else:
        foodon_features = np.zeros(FOODON_DIM, dtype=np.float32)
    foodon_scaled = foodon_features * FOODON_SCALE

    # 3. Regex binary features (25 dims) - scaled for equal weight
    binary_features = []
    for pattern_name, pattern in DETECTION_PATTERNS.items():
        match = 1.0 if re.search(pattern, full_text, re.IGNORECASE) else 0.0
        binary_features.append(match)
    regex_features = np.array(binary_features, dtype=np.float32) * REGEX_SCALE

    # 4. Concatenate all features
    return np.concatenate([name_embedding, foodon_scaled, regex_features])


# =============================================================================
# PREDICTOR CLASS
# =============================================================================


class Predictor:
    """
    Prédicteur de métadonnées pour ingrédients alimentaires.

    Combine :
    - Classification RandomForest pour categories, cropGroup, transportCooling
    - Classification SVM pour density, inediblePart, rawToCookedRatio (values as categories)
    - Règles déterministes pour defaultOrigin
    """

    def __init__(self):
        self.model = None  # SentenceTransformer, chargé lazily
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
        self.processing_matcher = None
        self.transport_matcher = None

        # CropGroup matcher (nearest neighbor)
        self.cropgroup_matcher = None

        # Value matchers for continuous values (nearest neighbor on reference data)
        self.density_matcher = None
        self.inedible_matcher = None
        self.ratio_matcher = None

        # Training data (for categorical classifiers)
        self.training_features = None
        self.training_ingredients = None

        # Métadonnées
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

    def _load_model(self):
        """Charge le modèle d'embedding et de traduction (lazy loading)."""
        if not self._model_loaded:
            print(f"Importing sentence_transformers...")
            from sentence_transformers import SentenceTransformer

            print(f"Loading embedding model: {MODEL}")
            self.model = SentenceTransformer(MODEL)

            print(f"Loading translation model: {MT_MODEL}")
            self.mt_tokenizer = AutoTokenizer.from_pretrained(MT_MODEL)
            self.mt_model = AutoModelForSeq2SeqLM.from_pretrained(MT_MODEL).to(
                self.device
            )

            self._model_loaded = True

    def _load_foodon(self):
        """Charge le FoodOn feature extractor (lazy loading)."""
        if not self._foodon_loaded:
            from foodon_loader import FoodOnFeatureExtractor
            self.foodon_extractor = FoodOnFeatureExtractor()
            self._foodon_loaded = True

    def _translate(self, text: str) -> str:
        """Translate French text to English (with caching)."""
        text = text.strip()
        if not text:
            return ""

        # Check cache first
        if text in self._translation_cache:
            return self._translation_cache[text]

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
        """Extrait la catégorie de base (pas organic/bleublanccoeur)."""
        for cat in categories:
            if cat in BASE_CATEGORIES:
                return cat
        return "misc"

    def _get_additive_labels(self, categories: list) -> list:
        """Extrait les labels additifs."""
        return [cat for cat in categories if cat in ADDITIVE_LABELS]

    def _is_vegetal(self, categories: list) -> bool:
        """Détermine si l'ingrédient est végétal (nécessite cropGroup)."""
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
        """Extrait les features binaires (regex) du vecteur de features."""
        # Skip embedding (768) and FoodOn (20) to get to regex features
        regex_start = EMBEDDING_DIM + FOODON_DIM
        binary_values = features[0, regex_start:]  # Skip embedding and FoodOn
        # Unscale to get original binary values
        binary_values = binary_values / REGEX_SCALE
        return {
            name: bool(binary_values[i] > 0.5)
            for i, name in enumerate(BINARY_FEATURE_NAMES)
        }

    def _predict_category_by_rules(self, binary_features: dict) -> str | None:
        """Applique les règles déterministes pour la catégorie. Retourne None si aucune règle ne matche."""
        if binary_features.get("is_fish") or binary_features.get("is_seafood"):
            return "animal_product"
        if binary_features.get("is_meat"):
            return "animal_product"
        if binary_features.get("is_egg"):
            return "animal_product"
        if binary_features.get("is_dairy"):
            return "dairy_product"
        return None

    def _predict_transport_by_rules(self, binary_features: dict) -> str | None:
        """Applique les règles déterministes pour transportCooling. Retourne None si aucune règle ne matche."""
        if binary_features.get("is_frozen"):
            return "always"
        if binary_features.get("is_fresh") and (
            binary_features.get("is_fish")
            or binary_features.get("is_seafood")
            or binary_features.get("is_meat")
            or binary_features.get("is_dairy")
        ):
            return "always"
        return None

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
        perishable_types = {"meat", "fish_seafood", "dairy", "vegetable", "fruit"}
        if food_type in perishable_types:
            return "always"
        return "none"

    def fit(self, ingredients: list[dict], verbose: bool = True):
        """
        Entraîne le prédicteur sur une liste d'ingrédients.

        Args:
            ingredients: Liste de dicts avec au minimum "name" et "activityName"
        """
        import time

        def timed_print(msg, start_time=[None]):
            if start_time[0] is not None:
                elapsed = time.time() - start_time[0]
                print(f"  [{elapsed:.1f}s]")
            print(msg, end="", flush=True)
            start_time[0] = time.time()

        self._load_model()
        self._load_foodon()

        # Load augmented training data if available
        augmented_path = Path(__file__).parent.parent / "ingredients_augmented.json"
        if augmented_path.exists():
            with open(augmented_path) as f:
                augmented = json.load(f)
            ingredients = list(ingredients) + augmented
            if verbose:
                print(f"Added {len(augmented)} augmented ingredients")

        if verbose:
            timed_print(f"Training on {len(ingredients)} ingredients...\n")

        # 1. Pre-translate all ingredient names (batch for performance)
        if verbose:
            timed_print("Translating ingredient names (cached)...")
        translated_names = [self._translate(ing.get("name", "")) for ing in ingredients]

        # 2. Extraction des features pour tous les ingrédients
        if verbose:
            timed_print("Extracting features...")

        features_list = []
        for i, ing in enumerate(ingredients):
            activity = ing.get("activityName", "")
            # Use translated name for both embedding AND FoodOn (FoodOn is English-based)
            feat = extract_features(
                translated_names[i], activity, self.model,
                foodon_extractor=self.foodon_extractor
            )
            features_list.append(feat)

        self.training_features = np.array(features_list)
        self.training_ingredients = ingredients
        self.feature_dim = self.training_features.shape[1]

        # 3. Build foodType and processingState matchers (nearest neighbor)
        if verbose:
            timed_print("Building foodType matcher...")

        # Extract foodType and processingState from old categories
        ing_names = [ing["name"] for ing in ingredients]
        y_food_types = []
        y_processing = []
        for ing in ingredients:
            base_cat = self._get_base_category(ing.get("categories", ["misc"]))
            food_type, proc_state = CATEGORY_TO_DIMENSIONS.get(
                base_cat, ("misc", "processed")
            )
            y_food_types.append(food_type)
            y_processing.append(proc_state)

        self.food_type_matcher = NearestNeighborMatcher(
            ing_names, y_food_types, self.model,
            translate_fn=self._translate, foodon_extractor=self.foodon_extractor
        )

        if verbose:
            timed_print("Building processingState matcher...")

        self.processing_matcher = NearestNeighborMatcher(
            ing_names, y_processing, self.model,
            translate_fn=self._translate, foodon_extractor=self.foodon_extractor
        )

        # 4. Build cropGroup matcher (nearest neighbor)
        if verbose:
            timed_print("Building cropGroup matcher...")

        # Build training data from ingredients with cropGroup
        cropgroup_names, cropgroup_vals = _build_cropgroup_data(ingredients)
        if cropgroup_names:
            self.cropgroup_matcher = NearestNeighborMatcher(
                cropgroup_names,
                cropgroup_vals,
                self.model,
                translate_fn=self._translate,
                foodon_extractor=self.foodon_extractor,
            )

        # 5. Build transportCooling matcher (nearest neighbor)
        if verbose:
            timed_print("Building transportCooling matcher...")

        y_transport = [ing.get("transportCooling", "none") for ing in ingredients]
        self.transport_matcher = NearestNeighborMatcher(
            ing_names, y_transport, self.model,
            translate_fn=self._translate, foodon_extractor=self.foodon_extractor
        )

        # 6. Build nearest neighbor matchers from reference data
        if verbose:
            timed_print("Building density matcher from reference data...")
        density_names, density_vals = _load_density_data()
        self.density_matcher = NearestNeighborMatcher(
            density_names, density_vals, self.model,
            translate_fn=self._translate, foodon_extractor=self.foodon_extractor
        )

        if verbose:
            timed_print("Building inedible part matcher from reference data...")
        inedible_names, inedible_vals = _load_inedible_data()
        self.inedible_matcher = NearestNeighborMatcher(
            inedible_names, inedible_vals, self.model,
            translate_fn=self._translate, foodon_extractor=self.foodon_extractor
        )

        if verbose:
            timed_print("Building raw-to-cooked ratio matcher from reference data...")
        ratio_names, ratio_vals = _load_ratio_data()
        self.ratio_matcher = NearestNeighborMatcher(
            ratio_names, ratio_vals, self.model,
            translate_fn=self._translate, foodon_extractor=self.foodon_extractor
        )

        self.is_fitted = True

        # Save translation cache to disk for faster subsequent runs
        self._save_translation_cache()

        if verbose:
            timed_print("✓ Training complete!\n")

    def predict(self, ingredient: dict) -> dict:
        """
        Prédit les métadonnées pour un nouvel ingrédient.

        Args:
            ingredient: Dict avec "name" et "activityName"

        Returns:
            Dict avec les métadonnées prédites
        """
        if not self.is_fitted:
            raise RuntimeError(
                "Predictor must be fitted before prediction. Call fit() first."
            )

        self._load_model()
        self._load_foodon()

        name = ingredient.get("name", "")
        activity = ingredient.get("activityName", "")

        # Extraction des features (with translation and FoodOn)
        features = extract_features(
            name, activity, self.model, translate_fn=self._translate,
            foodon_extractor=self.foodon_extractor
        ).reshape(1, -1)

        predictions = {}
        full_text = f"{name} {activity}"

        # Extraire les features binaires pour les règles
        binary_features = self._extract_binary_from_features(features)

        # 1. FoodType (règles prioritaires, puis nearest neighbor)
        # Check if we can determine foodType from rules (e.g., fish, meat, dairy)
        if binary_features.get("is_fish") or binary_features.get("is_seafood"):
            food_type = "fish_seafood"
            food_type_match = None
        elif binary_features.get("is_meat"):
            food_type = "meat"
            food_type_match = None
        elif binary_features.get("is_dairy"):
            food_type = "dairy"
            food_type_match = None
        else:
            # Fallback to nearest neighbor
            food_type, _, food_type_match = self.food_type_matcher.predict(
                name, self.model, translate_fn=self._translate
            )

        predictions["foodType"] = food_type
        predictions["foodTypeMatch"] = food_type_match

        # 2. ProcessingState (packaging detection prioritaire, puis nearest neighbor)
        packaging, _ = self._detect_packaging(full_text)
        if packaging and packaging != "fresh":
            # Non-fresh packaging implies processed
            processing_state = "processed"
            processing_match = None
        else:
            # Fallback to nearest neighbor
            processing_state, _, processing_match = self.processing_matcher.predict(
                name, self.model, translate_fn=self._translate
            )

        predictions["processingState"] = processing_state
        predictions["processingStateMatch"] = processing_match
        predictions["packaging"] = packaging

        # 3. Labels additifs (par règles)
        labels = []
        if re.search(r"\b(bio|organic)\b", full_text, re.IGNORECASE):
            labels.append("organic")
        if re.search(r"\b(bleu.?blanc.?c[oœ]eur)\b", full_text, re.IGNORECASE):
            labels.append("bleublanccoeur")
        predictions["labels"] = labels

        # 4. Build categories from foodType + processingState + labels
        base_category = DIMENSIONS_TO_CATEGORY.get(
            (food_type, processing_state), "misc"
        )
        categories = [base_category] + labels
        predictions["categories"] = categories

        # 4. cropGroup (si végétal) - nearest neighbor matching
        vegetal_types = {"vegetable", "fruit", "grain", "nut_oilseed", "spice_condiment"}
        if food_type in vegetal_types and self.cropgroup_matcher is not None:
            cropgroup_val, _, cropgroup_match = self.cropgroup_matcher.predict(
                name, self.model, translate_fn=self._translate
            )
            predictions["cropGroup"] = cropgroup_val
            predictions["cropGroupMatch"] = cropgroup_match
        else:
            predictions["cropGroup"] = None
            predictions["cropGroupMatch"] = None

        # 5. transportCooling (packaging-based, puis foodType-based)
        transport_cooling = self._get_transport_from_packaging(packaging, food_type)
        predictions["transportCooling"] = transport_cooling

        # 5. defaultOrigin (par règles)
        predictions["defaultOrigin"] = _extract_origin(activity)

        # 6. Continuous values by classification (query = name only)
        density_val, _, density_match = self.density_matcher.predict(
            name, self.model, translate_fn=self._translate
        )
        inedible_val, _, inedible_match = self.inedible_matcher.predict(
            name, self.model, translate_fn=self._translate
        )
        ratio_val, _, ratio_match = self.ratio_matcher.predict(
            name, self.model, translate_fn=self._translate
        )

        predictions["density"] = round(density_val, 3)
        predictions["densityMatch"] = density_match
        predictions["inediblePart"] = round(inedible_val, 2)
        predictions["inediblePartMatch"] = inedible_match
        predictions["rawToCookedRatio"] = round(ratio_val, 3)
        predictions["rawToCookedRatioMatch"] = ratio_match

        return predictions

    def predict_with_confidence(self, ingredient: dict) -> tuple[dict, dict]:
        """
        Prédit avec scores de confiance.

        Returns:
            (predictions, confidence_scores)
        """
        if not self.is_fitted:
            raise RuntimeError("Predictor must be fitted before prediction.")

        self._load_model()
        self._load_foodon()

        name = ingredient.get("name", "")
        activity = ingredient.get("activityName", "")
        full_text = f"{name} {activity}"
        features = extract_features(
            name, activity, self.model, translate_fn=self._translate,
            foodon_extractor=self.foodon_extractor
        ).reshape(1, -1)

        predictions = {}
        confidence = {}

        # Extraire les features binaires pour les règles
        binary_features = self._extract_binary_from_features(features)

        # 1. FoodType (règles prioritaires, puis nearest neighbor)
        food_type_conf = 1.0  # Default confidence for rule-based
        food_type_match = None
        if binary_features.get("is_fish") or binary_features.get("is_seafood"):
            food_type = "fish_seafood"
        elif binary_features.get("is_meat"):
            food_type = "meat"
        elif binary_features.get("is_dairy"):
            food_type = "dairy"
        else:
            # Fallback to nearest neighbor
            food_type, food_type_conf, food_type_match = self.food_type_matcher.predict(
                name, self.model, translate_fn=self._translate
            )

        predictions["foodType"] = food_type
        predictions["foodTypeMatch"] = food_type_match
        confidence["foodType"] = food_type_conf

        # 2. ProcessingState (packaging detection prioritaire, puis nearest neighbor)
        packaging, _ = self._detect_packaging(full_text)
        proc_conf = 1.0  # Default confidence for rule-based
        processing_match = None
        if packaging and packaging != "fresh":
            processing_state = "processed"
        else:
            processing_state, proc_conf, processing_match = self.processing_matcher.predict(
                name, self.model, translate_fn=self._translate
            )

        predictions["processingState"] = processing_state
        predictions["processingStateMatch"] = processing_match
        predictions["packaging"] = packaging
        confidence["processingState"] = proc_conf

        # 3. Labels additifs (par règles)
        labels = []
        if re.search(r"\b(bio|organic)\b", full_text, re.IGNORECASE):
            labels.append("organic")
        if re.search(r"\b(bleu.?blanc.?c[oœ]eur)\b", full_text, re.IGNORECASE):
            labels.append("bleublanccoeur")
        predictions["labels"] = labels

        # 4. Build categories from foodType + processingState + labels
        base_category = DIMENSIONS_TO_CATEGORY.get(
            (food_type, processing_state), "misc"
        )
        categories = [base_category] + labels
        predictions["categories"] = categories
        # Use foodType confidence as categories confidence
        confidence["categories"] = food_type_conf

        # 5. cropGroup - nearest neighbor matching
        cropgroup_conf = 0.0
        vegetal_types = {"vegetable", "fruit", "grain", "nut_oilseed", "spice_condiment"}
        if food_type in vegetal_types and self.cropgroup_matcher is not None:
            cropgroup_val, cropgroup_conf, cropgroup_match = self.cropgroup_matcher.predict(
                name, self.model, translate_fn=self._translate
            )
            predictions["cropGroup"] = cropgroup_val
            predictions["cropGroupMatch"] = cropgroup_match
        else:
            predictions["cropGroup"] = None
            predictions["cropGroupMatch"] = None

        if predictions["cropGroup"]:
            confidence["cropGroup"] = cropgroup_conf

        # 5. transportCooling (packaging-based, deterministic from packaging + foodType)
        transport_cooling = self._get_transport_from_packaging(packaging, food_type)
        predictions["transportCooling"] = transport_cooling
        # No confidence for rule-based transportCooling

        # 6. defaultOrigin
        predictions["defaultOrigin"] = _extract_origin(activity)

        # 7. Value predictions with confidence
        density_val, density_conf, density_match = self.density_matcher.predict(
            name, self.model, translate_fn=self._translate
        )
        inedible_val, inedible_conf, inedible_match = self.inedible_matcher.predict(
            name, self.model, translate_fn=self._translate
        )
        ratio_val, ratio_conf, ratio_match = self.ratio_matcher.predict(
            name, self.model, translate_fn=self._translate
        )

        predictions["density"] = round(density_val, 3)
        predictions["densityMatch"] = density_match
        predictions["inediblePart"] = round(inedible_val, 2)
        predictions["inediblePartMatch"] = inedible_match
        predictions["rawToCookedRatio"] = round(ratio_val, 3)
        predictions["rawToCookedRatioMatch"] = ratio_match

        confidence["density"] = density_conf
        confidence["inediblePart"] = inedible_conf
        confidence["rawToCookedRatio"] = ratio_conf

        return predictions, confidence

    def evaluate(self, verbose: bool = True) -> dict:
        """
        Évalue le prédicteur en cross-validation sur les données d'entraînement.

        Returns:
            Dict avec les scores par métadonnée
        """
        if not self.is_fitted:
            raise RuntimeError("Predictor must be fitted before evaluation.")

        scores = {}

        # Évaluation foodType
        y_food = []
        y_proc = []
        for ing in self.training_ingredients:
            base_cat = self._get_base_category(ing.get("categories", ["misc"]))
            food_type, proc_state = CATEGORY_TO_DIMENSIONS.get(
                base_cat, ("misc", "processed")
            )
            y_food.append(food_type)
            y_proc.append(proc_state)

        food_encoder = LabelEncoder()
        y_food_encoded = food_encoder.fit_transform(y_food)
        food_scores = cross_val_score(
            RandomForestClassifier(
                n_estimators=100, class_weight="balanced", random_state=42
            ),
            self.training_features,
            y_food_encoded,
            cv=5,
            scoring="accuracy",
        )
        scores["foodType"] = {"mean": food_scores.mean(), "std": food_scores.std()}

        if verbose:
            print(
                f"FoodType accuracy: {food_scores.mean():.3f} ± {food_scores.std():.3f}"
            )

        # Évaluation processingState
        proc_encoder = LabelEncoder()
        y_proc_encoded = proc_encoder.fit_transform(y_proc)
        proc_scores = cross_val_score(
            RandomForestClassifier(
                n_estimators=100, class_weight="balanced", random_state=42
            ),
            self.training_features,
            y_proc_encoded,
            cv=5,
            scoring="accuracy",
        )
        scores["processingState"] = {"mean": proc_scores.mean(), "std": proc_scores.std()}

        if verbose:
            print(
                f"ProcessingState accuracy: {proc_scores.mean():.3f} ± {proc_scores.std():.3f}"
            )

        # Évaluation transportCooling
        transport_encoder = LabelEncoder()
        y_transport = transport_encoder.fit_transform([
            ing.get("transportCooling", "none") for ing in self.training_ingredients
        ])
        transport_scores = cross_val_score(
            RandomForestClassifier(
                n_estimators=100, class_weight="balanced", random_state=42
            ),
            self.training_features,
            y_transport,
            cv=5,
            scoring="accuracy",
        )
        scores["transportCooling"] = {
            "mean": transport_scores.mean(),
            "std": transport_scores.std(),
        }

        if verbose:
            print(
                f"TransportCooling accuracy: {transport_scores.mean():.3f} ± {transport_scores.std():.3f}"
            )

        # Évaluation cropGroup (sur végétaux uniquement, using RandomForest on embeddings)
        cropgroup_names, cropgroup_vals = _build_cropgroup_data(
            self.training_ingredients
        )

        if len(cropgroup_names) > 10:
            # Compute embeddings for names
            X_crop = self.model.encode(cropgroup_names)
            le = LabelEncoder()
            y_crop_enc = le.fit_transform(cropgroup_vals)

            crop_scores = cross_val_score(
                RandomForestClassifier(
                    n_estimators=100, class_weight="balanced", random_state=42
                ),
                X_crop,
                y_crop_enc,
                cv=min(5, len(set(cropgroup_vals))),
                scoring="accuracy",
            )
            scores["cropGroup"] = {"mean": crop_scores.mean(), "std": crop_scores.std()}

            if verbose:
                print(
                    f"CropGroup accuracy: {crop_scores.mean():.3f} ± {crop_scores.std():.3f}"
                )

        return scores

    def save(self, path: str):
        """Sauvegarde le prédicteur entraîné."""
        if not self.is_fitted:
            raise RuntimeError("Cannot save unfitted predictor.")

        # Clear FoodOn extractor references from matchers (can't be pickled)
        # They will be restored on load when predict() is called
        matchers = [
            self.food_type_matcher,
            self.processing_matcher,
            self.transport_matcher,
            self.cropgroup_matcher,
            self.density_matcher,
            self.inedible_matcher,
            self.ratio_matcher,
        ]
        for matcher in matchers:
            if matcher is not None:
                matcher.foodon_extractor = None

        # On ne sauvegarde pas le modèle d'embedding ni FoodOn (rechargés au besoin)
        state = {
            # Categorical matchers (nearest neighbor approach)
            "food_type_matcher": self.food_type_matcher,
            "processing_matcher": self.processing_matcher,
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
            "_foodon_loaded": False,  # Will be reloaded on first predict
        }

        with open(path, "wb") as f:
            pickle.dump(state, f)

        print(f"✓ Predictor saved to {path}")

    @classmethod
    def load(cls, path: str) -> "Predictor":
        """Charge un prédicteur sauvegardé."""
        predictor = cls()

        with open(path, "rb") as f:
            state = pickle.load(f)

        for key, value in state.items():
            setattr(predictor, key, value)

        print(f"✓ Predictor loaded from {path}")
        return predictor


# =============================================================================
# INTEGRATION avec ecobalyse_data/detect
# =============================================================================

# Pour compatibilité avec le pattern existant
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
    """Applique les prédictions à l'objet."""
    for key, value in predictions.items():
        if value is not None:
            obj[key] = value


class Detector:
    """Interface compatible avec les autres détecteurs."""

    def __init__(
        self, model_path: Optional[str] = None, training_data: Optional[list] = None
    ):
        """
        Args:
            model_path: Chemin vers un modèle sauvegardé
            training_data: Données d'entraînement (si pas de modèle sauvegardé)
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
        Prédit les métadonnées pour un ingrédient.

        Returns:
            (predictions, score, best_match)
        """
        predictions, confidence = self.predictor.predict_with_confidence(ingredient)

        # Score global = moyenne des confiances (categorical + value classifiers)
        score = np.mean([
            confidence.get("categories", 0),
            confidence.get("transportCooling", 0),
            confidence.get("density", 0),
            confidence.get("inediblePart", 0),
            confidence.get("rawToCookedRatio", 0),
        ])

        # Best match info is no longer available without KNN
        best_match = f"density={predictions.get('density')}, inedible={predictions.get('inediblePart')}"

        return predictions, score, best_match


def update(input_json, threshold=THRESHOLD, debug=False, model_path=None):
    """
    Met à jour les métadonnées pour tous les ingrédients.

    Compatible avec le CLI existant.
    """
    from rich.progress import track

    # Filtrer les ingrédients qui ont déjà toutes les métadonnées
    to_predict = [ing for ing in input_json if not _get(ing)]
    already_done = [ing for ing in input_json if _get(ing)]

    if not to_predict:
        print("All ingredients already have metadata.")
        return input_json

    # Entraîner sur les ingrédients existants si pas de modèle fourni
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
    """Interface simple pour détecter un seul ingrédient."""
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

        predictions, confidence = predictor.predict_with_confidence(ingredient)

        print("\n📊 Predictions:")
        for key, value in predictions.items():
            conf = confidence.get(key, 0)
            if conf > 0:
                print(f"  {key}: {value} (confidence: {conf:.2f})")
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
