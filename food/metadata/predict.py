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
from sklearn.svm import SVC
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

warnings.filterwarnings("ignore")

# =============================================================================
# CONFIGURATION
# =============================================================================

MODEL = "all-MiniLM-L6-v2"  # Même modèle que density.py et cooked_to_raw.py
MT_MODEL = "Helsinki-NLP/opus-mt-fr-en"  # FR → EN Machine Translation

# Catégories de base (pas des labels additifs)
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
DENSITY_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "density_DB.xlsx"
INEDIBLE_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "inedible_part.csv"
RATIO_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "cooked_to_raw.csv"


def _load_density_data() -> tuple[list, list]:
    """Load density_DB.xlsx, return (names, values)."""
    df = pd.read_excel(DENSITY_DATA_PATH, sheet_name="Density DB", engine="openpyxl")
    # Columns: A=food, B=density, C=specific_gravity
    df = df.iloc[:, :3]
    df.columns = ["food", "density", "specific_gravity"]
    df = df[pd.notnull(df["density"]) | pd.notnull(df["specific_gravity"])]
    df = df.dropna(subset=["food"])

    names = []
    values = []
    for _, row in df.iterrows():
        name = str(row["food"]).strip()
        if not name:
            continue
        # Use density, fallback to specific_gravity
        value = row["density"] if pd.notnull(row["density"]) else row["specific_gravity"]
        # Handle value ranges like "0.2-0.4" → 0.3
        if isinstance(value, str) and "-" in value:
            parts = [float(x) for x in value.split("-")]
            value = sum(parts) / len(parts)
        if isinstance(value, (int, float)):
            names.append(name)
            values.append(float(value))

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


class ValueClassifier:
    """Classifier that treats unique numeric values as categories."""

    def __init__(self, names: list, values: list, model, translate_fn=None):
        """
        Train an SVM classifier on embeddings where classes are unique values.

        Args:
            names: List of food names from reference data
            values: List of corresponding numeric values
            model: SentenceTransformer model for encoding
            translate_fn: Optional function to translate names before encoding
        """
        self.encoder = LabelEncoder()
        self.values = np.array(values)
        self.translate_fn = translate_fn

        # Encode unique values as classes
        self.encoded_labels = self.encoder.fit_transform(values)

        # Translate names if translation function provided (batch translation)
        if translate_fn:
            print(f"  Translating {len(names)} names (batch)...")
            names = translate_fn(names)  # Pass list for batch translation

        # Compute embeddings for all reference names
        print(f"  Computing embeddings for {len(names)} reference items...")
        self.embeddings = model.encode(names)

        # Train classifier (SVM with probability estimates)
        print(f"  Training SVM classifier ({len(self.encoder.classes_)} classes)...")
        self.classifier = SVC(kernel="rbf", probability=True, class_weight="balanced")
        self.classifier.fit(self.embeddings, self.encoded_labels)

    def predict(self, query: str, model, translate_fn=None) -> tuple[float, float]:
        """
        Predict value for a query string.

        Returns:
            (predicted_value, confidence_score)
        """
        # Translate query if translation function provided
        if translate_fn:
            query = translate_fn(query)

        query_emb = model.encode(query).reshape(1, -1)

        # Get prediction and probability
        pred_class = self.classifier.predict(query_emb)[0]
        proba = self.classifier.predict_proba(query_emb)[0]
        confidence = float(proba.max())

        # Decode back to original value
        pred_value = self.encoder.inverse_transform([pred_class])[0]
        return float(pred_value), confidence


class CategoryClassifier:
    """Classifier for string categories using semantic matching."""

    def __init__(self, names: list, categories: list, model, translate_fn=None):
        """
        Train an SVM classifier on embeddings where classes are string categories.

        Args:
            names: List of food names from reference data
            categories: List of corresponding category strings
            model: SentenceTransformer model for encoding
            translate_fn: Optional function to translate names before encoding
        """
        self.encoder = LabelEncoder()
        self.translate_fn = translate_fn

        # Encode categories as classes
        self.encoded_labels = self.encoder.fit_transform(categories)

        # Translate names if translation function provided (batch translation)
        if translate_fn:
            print(f"  Translating {len(names)} names (batch)...")
            names = translate_fn(names)  # Pass list for batch translation

        # Compute embeddings for all reference names
        print(f"  Computing embeddings for {len(names)} reference items...")
        self.embeddings = model.encode(names)

        # Train classifier (SVM with probability estimates)
        print(f"  Training SVM classifier ({len(self.encoder.classes_)} classes)...")
        self.classifier = SVC(kernel="rbf", probability=True, class_weight="balanced")
        self.classifier.fit(self.embeddings, self.encoded_labels)

    def predict(self, query: str, model, translate_fn=None) -> tuple[str, float]:
        """
        Predict category for a query string.

        Returns:
            (predicted_category, confidence_score)
        """
        # Translate query if translation function provided
        if translate_fn:
            query = translate_fn(query)

        query_emb = model.encode(query).reshape(1, -1)

        # Get prediction and probability
        pred_class = self.classifier.predict(query_emb)[0]
        proba = self.classifier.predict_proba(query_emb)[0]
        confidence = float(proba.max())

        # Decode back to original category string
        pred_category = self.encoder.inverse_transform([pred_class])[0]
        return pred_category, confidence


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
    "is_processed": r"\b(transformé|transformee|processed|préparé|preparee|industriel)\b",
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

# Index des features binaires dans le vecteur (après les 384 dims d'embedding)
BINARY_FEATURE_NAMES = list(DETECTION_PATTERNS.keys())
EMBEDDING_DIM = 384


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
    name: str, activity_name: str, embedding_model, translate_fn=None
) -> np.ndarray:
    """
    Extrait un vecteur de features à partir du nom et du procédé.

    Args:
        name: Ingredient name (potentially French)
        activity_name: Activity/process name
        embedding_model: SentenceTransformer model
        translate_fn: Optional function to translate name before encoding

    Returns:
        np.ndarray de dimension (384 + nb_patterns,)
    """
    # Combine nom + activité pour le texte complet (keep original for regex matching)
    full_text = f"{name} {activity_name}".lower()

    # 1. Embedding du nom (384 dims) - translate first if available
    name_for_embedding = translate_fn(name) if translate_fn else name
    name_embedding = embedding_model.encode(name_for_embedding, convert_to_tensor=False)

    # 2. Features binaires par regex (on original French text)
    binary_features = []
    for pattern_name, pattern in DETECTION_PATTERNS.items():
        match = 1.0 if re.search(pattern, full_text, re.IGNORECASE) else 0.0
        binary_features.append(match)

    # 3. Concaténation
    return np.concatenate([name_embedding, np.array(binary_features)])


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
        self._translation_cache = {}  # Cache for translated strings

        # Classifieurs pour categorical metadata
        self.category_classifier = None
        self.transport_classifier = None

        # Encoders for categorical metadata
        self.category_encoder = LabelEncoder()
        self.transport_encoder = LabelEncoder()

        # CropGroup classifier (SVM-based, uses semantic matching)
        self.cropgroup_classifier = None

        # Value classifiers for continuous values (trained on reference data)
        self.density_classifier = None
        self.inedible_classifier = None
        self.ratio_classifier = None

        # Training data (for categorical classifiers)
        self.training_features = None
        self.training_ingredients = None

        # Métadonnées
        self.is_fitted = False
        self.feature_dim = None

    def _load_model(self):
        """Charge le modèle d'embedding et de traduction (lazy loading)."""
        if not self._model_loaded:
            print(f"Importing sentence_transformers...")
            from sentence_transformers import SentenceTransformer

            print(f"Loading embedding model: {MODEL}")
            self.model = SentenceTransformer(MODEL)

            print(f"Loading translation model: {MT_MODEL}")
            self.mt_tokenizer = AutoTokenizer.from_pretrained(MT_MODEL)
            self.mt_model = AutoModelForSeq2SeqLM.from_pretrained(MT_MODEL).to(self.device)

            self._model_loaded = True

    def _translate(self, text: str | list[str]) -> str | list[str]:
        """Translate French text(s) to English. Handles both single string and list."""
        if isinstance(text, list):
            return self._translate_batch(text)

        # Single string case
        text = text.strip()
        if not text:
            return ""

        # Check cache first
        if text in self._translation_cache:
            return self._translation_cache[text]

        inputs = self.mt_tokenizer(text, return_tensors="pt").to(self.device)
        with torch.no_grad():
            outputs = self.mt_model.generate(**inputs, max_length=40)
        result = self.mt_tokenizer.batch_decode(outputs, skip_special_tokens=True)[0].strip()

        # Cache the result
        self._translation_cache[text] = result
        return result

    def _translate_batch(self, texts: list[str]) -> list[str]:
        """Translate a batch of French texts to English (with caching)."""
        results = [""] * len(texts)
        to_translate = []  # (original_index, text) pairs for texts not in cache

        # Check cache and collect texts that need translation
        for i, t in enumerate(texts):
            t = t.strip()
            if not t:
                results[i] = ""
            elif t in self._translation_cache:
                results[i] = self._translation_cache[t]
            else:
                to_translate.append((i, t))

        if not to_translate:
            return results

        # Batch translate only the uncached texts
        indices, valid_texts = zip(*to_translate)
        inputs = self.mt_tokenizer(list(valid_texts), return_tensors="pt", padding=True).to(self.device)
        with torch.no_grad():
            outputs = self.mt_model.generate(**inputs, max_length=40)
        translations = self.mt_tokenizer.batch_decode(outputs, skip_special_tokens=True)

        # Store results and update cache
        for idx, original_text, trans in zip(indices, valid_texts, translations):
            trans = trans.strip()
            results[idx] = trans
            self._translation_cache[original_text] = trans

        return results

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
        """Extrait les features binaires du vecteur de features."""
        binary_values = features[0, EMBEDDING_DIM:]  # Skip embedding
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

    def fit(self, ingredients: list[dict], verbose: bool = True):
        """
        Entraîne le prédicteur sur une liste d'ingrédients.

        Args:
            ingredients: Liste de dicts avec au minimum "name" et "activityName"
        """
        self._load_model()

        if verbose:
            print(f"Training on {len(ingredients)} ingredients...")

        # 1. Pre-translate all ingredient names (batch for performance)
        if verbose:
            print("Pre-translating ingredient names (batch)...")
        all_names = [ing.get("name", "") for ing in ingredients]
        translated_names = self._translate(all_names)

        # 2. Extraction des features pour tous les ingrédients
        if verbose:
            print("Extracting features...")

        features_list = []
        for i, ing in enumerate(ingredients):
            activity = ing.get("activityName", "")
            # Use pre-translated name, no translate_fn needed
            feat = extract_features(translated_names[i], activity, self.model)
            features_list.append(feat)

        self.training_features = np.array(features_list)
        self.training_ingredients = ingredients
        self.feature_dim = self.training_features.shape[1]

        # 3. Entraînement du classifieur de catégories
        if verbose:
            print("Training category classifier...")

        y_categories = [
            self._get_base_category(ing.get("categories", ["misc"]))
            for ing in ingredients
        ]
        self.category_encoder.fit(BASE_CATEGORIES)
        y_cat_encoded = self.category_encoder.transform(y_categories)

        self.category_classifier = RandomForestClassifier(
            n_estimators=100,
            max_depth=15,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
        self.category_classifier.fit(self.training_features, y_cat_encoded)

        # 4. Entraînement du classifieur cropGroup (SVM-based avec semantic matching)
        if verbose:
            print("Training cropGroup classifier (semantic matching)...")

        # Build training data from ingredients + cropGroup labels themselves
        cropgroup_names, cropgroup_vals = _build_cropgroup_data(ingredients)
        if cropgroup_names:
            self.cropgroup_classifier = CategoryClassifier(
                cropgroup_names, cropgroup_vals, self.model, translate_fn=self._translate
            )

        # 5. Entraînement du classifieur transportCooling
        if verbose:
            print("Training transportCooling classifier...")

        y_transport = [ing.get("transportCooling", "none") for ing in ingredients]
        self.transport_encoder.fit(TRANSPORT_COOLING_VALUES)
        y_transport_encoded = self.transport_encoder.transform(y_transport)

        self.transport_classifier = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
        self.transport_classifier.fit(self.training_features, y_transport_encoded)

        # 6. Train value classifiers from reference data
        if verbose:
            print("Training density classifier from reference data...")
        density_names, density_vals = _load_density_data()
        self.density_classifier = ValueClassifier(
            density_names, density_vals, self.model, translate_fn=self._translate
        )

        if verbose:
            print("Training inedible part classifier from reference data...")
        inedible_names, inedible_vals = _load_inedible_data()
        self.inedible_classifier = ValueClassifier(
            inedible_names, inedible_vals, self.model, translate_fn=self._translate
        )

        if verbose:
            print("Training raw-to-cooked ratio classifier from reference data...")
        ratio_names, ratio_vals = _load_ratio_data()
        self.ratio_classifier = ValueClassifier(
            ratio_names, ratio_vals, self.model, translate_fn=self._translate
        )

        self.is_fitted = True

        if verbose:
            print("✓ Training complete!")

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

        name = ingredient.get("name", "")
        activity = ingredient.get("activityName", "")

        # Extraction des features (with translation)
        features = extract_features(
            name, activity, self.model, translate_fn=self._translate
        ).reshape(1, -1)

        predictions = {}

        # Extraire les features binaires pour les règles
        binary_features = self._extract_binary_from_features(features)

        # 1. Catégorie de base (règles prioritaires, puis ML)
        base_category = self._predict_category_by_rules(binary_features)
        if base_category is None:
            # Fallback sur ML
            cat_pred = self.category_classifier.predict(features)[0]
            base_category = self.category_encoder.inverse_transform([cat_pred])[0]

        # 2. Labels additifs (par règles)
        categories = [base_category]
        full_text = f"{name} {activity}".lower()
        if re.search(r"\b(bio|organic)\b", full_text, re.IGNORECASE):
            categories.append("organic")
        if re.search(r"\b(bleu.?blanc.?c[oœ]eur)\b", full_text, re.IGNORECASE):
            categories.append("bleublanccoeur")

        predictions["categories"] = categories

        # 3. cropGroup (si végétal) - uses semantic matching
        if self._is_vegetal(categories) and self.cropgroup_classifier is not None:
            cropgroup_val, _ = self.cropgroup_classifier.predict(
                name, self.model, translate_fn=self._translate
            )
            predictions["cropGroup"] = cropgroup_val
        else:
            predictions["cropGroup"] = None

        # 4. transportCooling (règles prioritaires, puis ML)
        transport_cooling = self._predict_transport_by_rules(binary_features)
        if transport_cooling is None:
            # Fallback sur ML
            transport_pred = self.transport_classifier.predict(features)[0]
            transport_cooling = self.transport_encoder.inverse_transform([
                transport_pred
            ])[0]
        predictions["transportCooling"] = transport_cooling

        # 5. defaultOrigin (par règles)
        predictions["defaultOrigin"] = _extract_origin(activity)

        # 6. Continuous values by classification (query = name only)
        density_val, _ = self.density_classifier.predict(
            name, self.model, translate_fn=self._translate
        )
        inedible_val, _ = self.inedible_classifier.predict(
            name, self.model, translate_fn=self._translate
        )
        ratio_val, _ = self.ratio_classifier.predict(
            name, self.model, translate_fn=self._translate
        )

        predictions["density"] = round(density_val, 3)
        predictions["inediblePart"] = round(inedible_val, 2)
        predictions["rawToCookedRatio"] = round(ratio_val, 3)

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

        name = ingredient.get("name", "")
        activity = ingredient.get("activityName", "")
        features = extract_features(
            name, activity, self.model, translate_fn=self._translate
        ).reshape(1, -1)

        # Get base predictions first
        predictions = {}

        # Extraire les features binaires pour les règles
        binary_features = self._extract_binary_from_features(features)

        # 1. Catégorie de base (règles prioritaires, puis ML)
        base_category = self._predict_category_by_rules(binary_features)
        if base_category is None:
            cat_pred = self.category_classifier.predict(features)[0]
            base_category = self.category_encoder.inverse_transform([cat_pred])[0]

        categories = [base_category]
        full_text = f"{name} {activity}".lower()
        if re.search(r"\b(bio|organic)\b", full_text, re.IGNORECASE):
            categories.append("organic")
        if re.search(r"\b(bleu.?blanc.?c[oœ]eur)\b", full_text, re.IGNORECASE):
            categories.append("bleublanccoeur")
        predictions["categories"] = categories

        # 2. cropGroup - uses semantic matching
        cropgroup_conf = 0.0
        if self._is_vegetal(categories) and self.cropgroup_classifier is not None:
            cropgroup_val, cropgroup_conf = self.cropgroup_classifier.predict(
                name, self.model, translate_fn=self._translate
            )
            predictions["cropGroup"] = cropgroup_val
        else:
            predictions["cropGroup"] = None

        # 3. transportCooling
        transport_cooling = self._predict_transport_by_rules(binary_features)
        if transport_cooling is None:
            transport_pred = self.transport_classifier.predict(features)[0]
            transport_cooling = self.transport_encoder.inverse_transform([transport_pred])[0]
        predictions["transportCooling"] = transport_cooling

        # 4. defaultOrigin
        predictions["defaultOrigin"] = _extract_origin(activity)

        # 5. Value predictions with confidence
        density_val, density_conf = self.density_classifier.predict(
            name, self.model, translate_fn=self._translate
        )
        inedible_val, inedible_conf = self.inedible_classifier.predict(
            name, self.model, translate_fn=self._translate
        )
        ratio_val, ratio_conf = self.ratio_classifier.predict(
            name, self.model, translate_fn=self._translate
        )

        predictions["density"] = round(density_val, 3)
        predictions["inediblePart"] = round(inedible_val, 2)
        predictions["rawToCookedRatio"] = round(ratio_val, 3)

        # Build confidence dict
        confidence = {}

        # Confiance pour les classifications catégorielles (max probability)
        cat_proba = self.category_classifier.predict_proba(features)[0]
        confidence["categories"] = float(cat_proba.max())

        transport_proba = self.transport_classifier.predict_proba(features)[0]
        confidence["transportCooling"] = float(transport_proba.max())

        if predictions["cropGroup"]:
            confidence["cropGroup"] = cropgroup_conf

        # Confiance pour les value classifiers
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

        # Évaluation catégories
        y_cat = self.category_encoder.transform([
            self._get_base_category(ing.get("categories", ["misc"]))
            for ing in self.training_ingredients
        ])
        cat_scores = cross_val_score(
            RandomForestClassifier(n_estimators=100, class_weight="balanced", random_state=42),
            self.training_features,
            y_cat,
            cv=5,
            scoring="accuracy",
        )
        scores["categories"] = {"mean": cat_scores.mean(), "std": cat_scores.std()}

        if verbose:
            print(
                f"Categories accuracy: {cat_scores.mean():.3f} ± {cat_scores.std():.3f}"
            )

        # Évaluation transportCooling
        y_transport = self.transport_encoder.transform([
            ing.get("transportCooling", "none") for ing in self.training_ingredients
        ])
        transport_scores = cross_val_score(
            RandomForestClassifier(n_estimators=100, class_weight="balanced", random_state=42),
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

        # Évaluation cropGroup (sur végétaux uniquement, using SVM on name embeddings)
        cropgroup_names, cropgroup_vals = _build_cropgroup_data(self.training_ingredients)

        if len(cropgroup_names) > 10:
            # Compute embeddings for names
            X_crop = self.model.encode(cropgroup_names)
            le = LabelEncoder()
            y_crop_enc = le.fit_transform(cropgroup_vals)

            crop_scores = cross_val_score(
                SVC(kernel="rbf", class_weight="balanced"),
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

        # On ne sauvegarde pas le modèle d'embedding (trop gros, rechargé au besoin)
        state = {
            # Categorical classifiers
            "category_classifier": self.category_classifier,
            "transport_classifier": self.transport_classifier,
            "category_encoder": self.category_encoder,
            "transport_encoder": self.transport_encoder,
            # CropGroup classifier (SVM-based, semantic matching)
            "cropgroup_classifier": self.cropgroup_classifier,
            # Value classifiers (SVM-based)
            "density_classifier": self.density_classifier,
            "inedible_classifier": self.inedible_classifier,
            "ratio_classifier": self.ratio_classifier,
            # Training data (for categorical classifiers and evaluation)
            "training_features": self.training_features,
            "training_ingredients": self.training_ingredients,
            "feature_dim": self.feature_dim,
            "is_fitted": True,
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
