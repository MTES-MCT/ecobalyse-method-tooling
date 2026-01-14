"""
FoodOn Ontology Feature Extractor
=================================

Extracts structured features from FoodOn ontology for food ingredient classification.
Used to supplement E5 embeddings with explicit food category knowledge.

FoodOn: https://foodon.org/
"""

import warnings
from pathlib import Path
from typing import Optional

import numpy as np

# Suppress pronto warnings
warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=UnicodeWarning)

import re

FOODON_PATH = Path(__file__).parent / "data" / "foodon.owl"
FOODON_URL = "http://purl.obolibrary.org/obo/foodon.owl"

# Prefix words to skip in lookup (quality/state/seasonal terms that match generic FoodOn entries)
SKIP_PREFIX_WORDS = {"whole", "raw", "fresh", "dried", "frozen", "canned", "cooked", "spring"}

# Regional food synonyms (British/American variants not captured by embeddings)
# Maps US English → British/international English (FoodOn uses British)
FOOD_SYNONYMS = {
    "corn": "maize",
    "eggplant": "aubergine",
    "zucchini": "courgette",
    "cilantro": "coriander",
    "chickpea": "garbanzo",
    "arugula": "rocket",
    "scallion": "spring onion",
    "bell pepper": "capsicum",
    "faba": "broad bean",  # faba bean = fava bean = broad bean
}


def _download_foodon(destination: Path) -> None:
    """Download FoodOn ontology from OBO Foundry.

    Args:
        destination: Path to save the foodon.owl file
    """
    import urllib.request

    print(f"Downloading FoodOn ontology from {FOODON_URL}...")
    print("(This is a ~200MB file, may take a few minutes)")

    # Ensure data directory exists
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Download with progress indication
    urllib.request.urlretrieve(FOODON_URL, destination)
    print(f"FoodOn downloaded to {destination}")


# FoodOn term IDs for food categories (verified via pronto)
FOODON_CATEGORIES = {
    "vegetable": "FOODON:00001261",  # vegetable food product
    "fruit": "FOODON:00001057",  # plant fruit food product (apples, pears, berries, etc.)
    "grain": "FOODON:00001093",  # cereal grain food product
    "fish_seafood": "FOODON:00001248",  # fish food product
    "dairy": "FOODON:00001256",  # dairy food product
    "nut_oilseed": "FOODON:00001172",  # nut food product
    "spice": "FOODON:03303380",  # spice food product
    "beverage": "FOODON:03301977",  # beverage food product
    "plant": "FOODON:00001015",  # plant food product (parent of veg/fruit)
    "legume": "FOODON:00001264",  # legume food product (peas, beans, lentils)
    "egg": "FOODON:00001274",  # egg food product
}

# Additional grain IDs (FoodOn ontology doesn't classify maize under cereal grain)
FOODON_GRAIN_EXTRA = {
    "FOODON:00001142",  # maize (corn) food product
    "FOODON:00001089",  # milled corn food product
}

# Additional seafood IDs (shellfish, molluscs, etc. - not under "fish food product")
FOODON_SEAFOOD_EXTRA = {
    "FOODON:00001046",  # animal seafood product (parent of shellfish)
    "FOODON:00001293",  # shellfish food product
    "FOODON:00005702",  # mollusk material
    "FOODON:00005522",  # invertebrate material
}

# Material hierarchy IDs (for organism-derived terms like "swordfish", "chicken")
# These catch items that match organism terms rather than food product terms
FOODON_MATERIAL_CATEGORIES = {
    "fish_seafood": "FOODON:03000165",  # fish material
    "plant": "FOODON:00004331",  # plant material
    "vertebrate": "FOODON:00005502",  # vertebrate material (for meat detection)
}

# FoodOn refined food product categories for NOVA detection
# Items under "refined or partially-refined food product" are typically NOVA 2 (culinary ingredients)
FOODON_REFINED_CATEGORIES = {
    "FOODON:00001595": 2,  # refined or partially-refined food product → NOVA 2
    "FOODON:00002131": 2,  # plant-based refined food product
    "FOODON:00002196": 2,  # animal-based refined food product
    "FOODON:00001907": 2,  # gluten refined food product
    "FOODON:00002274": 2,  # starch refined food product
}

# FoodOn processing classes mapped to NOVA groups
# NOVA 2: Culinary ingredients (oils, butter, sugar, salt, flour, starch)
# NOVA 3: Processed foods (fermented, cured, canned)
# NOVA 4: Ultra-processed (distilled, isolates, industrial)
NOVA_PROCESSING_CLASSES = {
    # NOVA 2 - Extracted culinary ingredients
    "FOODON:03460263": 2,  # oil/fat processing
    "FOODON:03460136": 2,  # sugar/syrup processing
    "FOODON:00001158": 2,  # oil food product
    "FOODON:00002275": 2,  # butter food product
    "FOODON:03311436": 2,  # flour food product
    "FOODON:00001250": 2,  # starch food product
    "FOODON:00001185": 2,  # salt food product
    "FOODON:03301103": 2,  # sugar food product
    # NOVA 3 - Processed foods
    "FOODON:03460232": 3,  # alcoholic fermentation (wine, beer)
    "FOODON:03470104": 3,  # preservation by fermentation (pickles, yogurt)
    "FOODON:03460253": 3,  # curing/aging process (cured meats, cheese)
    "FOODON:03450005": 3,  # food baking process (bread)
    "FOODON:03460190": 3,  # pickling process
    "FOODON:03420086": 3,  # canned food product
    "FOODON:00001013": 3,  # cheese food product
    "FOODON:03307539": 3,  # bread food product
    # NOVA 4 - Ultra-processed
    "FOODON:03460270": 4,  # distillation (spirits)
    "FOODON:03420228": 4,  # extraction - isolates, concentrates (when industrial)
    "FOODON:00002626": 4,  # protein isolate
}

# Number of features extracted
FOODON_FEATURE_DIM = 20


class FoodOnFeatureExtractor:
    """Extract structured features from FoodOn ontology."""

    def __init__(self, foodon_path: Path = FOODON_PATH):
        """
        Load FoodOn ontology and build lookup indices.

        Downloads foodon.owl automatically if it doesn't exist.

        Args:
            foodon_path: Path to foodon.owl file
        """
        import pronto

        # Download FoodOn if not present
        if not foodon_path.exists():
            _download_foodon(foodon_path)

        print("Loading FoodOn ontology...")
        self.ontology = pronto.Ontology(str(foodon_path))
        self._build_indices()
        print(f"FoodOn loaded: {len(self.label_to_term)} terms indexed")

    def _build_indices(self):
        """Build lookup indices for fast matching.

        Prioritizes FOODON terms over NCBITaxon (organism) terms.
        Also parses parenthetical synonyms from term names (e.g., "maize (corn)").
        """
        self.label_to_term = {}
        self.food_product_terms = {}  # Separate index for food product terms

        for term in self.ontology.terms():
            if not term.name:
                continue

            label = term.name.lower()
            is_foodon_term = term.id.startswith("FOODON:")
            # True food products have "food product" in name or specific patterns
            is_food_product = is_foodon_term and (
                "food product" in label
                or label.endswith("(raw)")
                or label.endswith("(cooked)")
                or label.endswith("(dried)")
                or label.endswith("(frozen)")
                or label.endswith("(canned)")
            )

            # Always index FOODON terms in label_to_term
            if is_foodon_term:
                self.label_to_term[label] = term
            elif label not in self.label_to_term:
                self.label_to_term[label] = term

            # Only put true food products in food_product_terms
            if is_food_product:
                self.food_product_terms[label] = term

                # Parse parenthetical synonyms like "maize (corn) food product"
                paren_match = re.search(r"^(\w+)\s*\((\w+)\)", label)
                if paren_match:
                    main_word = paren_match.group(1)
                    synonym = paren_match.group(2)
                    # Index the synonym (e.g., "corn" from "maize (corn)")
                    if synonym not in self.food_product_terms:
                        self.food_product_terms[synonym] = term
                    # Index "corn food product" variant
                    suffix = label[paren_match.end() :].strip()
                    if suffix:
                        synonym_variant = f"{synonym} {suffix}"
                        if synonym_variant not in self.food_product_terms:
                            self.food_product_terms[synonym_variant] = term

            elif label not in self.label_to_term:
                self.label_to_term[label] = term

            # Index by synonyms (prefer food products)
            for syn in getattr(term, "synonyms", []):
                if hasattr(syn, "description") and syn.description:
                    syn_label = syn.description.lower()
                    if is_foodon_term:
                        self.label_to_term[syn_label] = term
                        if is_food_product:
                            self.food_product_terms[syn_label] = term
                    elif syn_label not in self.label_to_term:
                        self.label_to_term[syn_label] = term

    def lookup(self, name: str, threshold: float = 0.6):
        """
        Match ingredient name to FoodOn food product term.

        Prioritizes FOODON: terms (food products) over NCBITaxon (organisms).
        Uses fast dictionary lookups instead of slow fuzzy matching.
        Handles prefix words (whole, raw, etc.) and regional synonyms.

        Args:
            name: Ingredient name to look up
            threshold: Minimum similarity score (not used, kept for compatibility)

        Returns:
            (term, confidence) - term is None if no match found
        """
        name_lower = name.lower().strip()

        # Skip very short names (noise)
        if len(name_lower) < 3:
            return None, 0.0

        # Skip prefix words like "whole", "raw", "fresh" FIRST to avoid matching
        # quality terms (e.g., "whole barley" matching "whole" instead of "barley")
        words = name_lower.split()
        if len(words) > 1 and words[0] in SKIP_PREFIX_WORDS:
            stripped_name = " ".join(words[1:])
            result = self._lookup_core(stripped_name)
            if result[0] is not None:
                return result

        # Try lookup with the original name
        result = self._lookup_core(name_lower)
        if result[0] is not None:
            return result

        # Try regional synonyms (corn → maize, eggplant → aubergine, etc.)
        for us_term, intl_term in FOOD_SYNONYMS.items():
            if us_term in name_lower:
                synonym_name = name_lower.replace(us_term, intl_term)
                result = self._lookup_core(synonym_name)
                if result[0] is not None:
                    return result

        return None, 0.0

    def _lookup_core(self, name_lower: str):
        """Core lookup logic without prefix/synonym handling."""
        # 1. Exact match in food product terms first (highest priority)
        if name_lower in self.food_product_terms:
            return self.food_product_terms[name_lower], 1.0

        # 2. Try adding "food product" suffix
        food_product_key = f"{name_lower} food product"
        if food_product_key in self.food_product_terms:
            return self.food_product_terms[food_product_key], 0.95

        # 3. Try adding "vegetable food product" suffix
        veg_key = f"{name_lower} vegetable food product"
        if veg_key in self.food_product_terms:
            return self.food_product_terms[veg_key], 0.95

        # 4. Try adding "fruit food product" suffix
        fruit_key = f"{name_lower} fruit food product"
        if fruit_key in self.food_product_terms:
            return self.food_product_terms[fruit_key], 0.95

        # Extract main words from name (skip common words)
        skip_words = {
            "de",
            "le",
            "la",
            "les",
            "du",
            "des",
            "en",
            "au",
            "aux",
            "et",
            "ou",
            "bio",
            "fr",
            "organic",
            "in",
            "shell",
            "with",
            "without",
        }
        words = [
            w
            for w in name_lower.replace(",", " ").split()
            if w not in skip_words and len(w) >= 3
        ]

        # 5. Try each significant word with various suffixes
        for word in words:
            # Try singular form (remove trailing 's')
            singular = (
                word.rstrip("s") if word.endswith("s") and len(word) > 4 else word
            )

            # Try "(raw)" suffix first (common in FoodOn)
            for base in [singular, word]:
                raw_key = f"{base} (raw)"
                if raw_key in self.food_product_terms:
                    return self.food_product_terms[raw_key], 0.95

            # Word as nut food product
            for base in [singular, word]:
                word_nut = f"{base} nut food product"
                if word_nut in self.food_product_terms:
                    return self.food_product_terms[word_nut], 0.9

            # Word as vegetable food product
            for base in [singular, word]:
                word_veg = f"{base} vegetable food product"
                if word_veg in self.food_product_terms:
                    return self.food_product_terms[word_veg], 0.9

            # Word as food product
            for base in [singular, word]:
                word_food = f"{base} food product"
                if word_food in self.food_product_terms:
                    return self.food_product_terms[word_food], 0.85

            # Exact word match in food_product_terms
            for base in [singular, word]:
                if base in self.food_product_terms:
                    term = self.food_product_terms[base]
                    # Skip obsolete terms
                    if "obsolete" not in (term.name or "").lower():
                        return term, 0.85

            # Word in food product label (substring match) - be more selective
            for base in [singular, word]:
                for label, term in self.food_product_terms.items():
                    # Skip obsolete terms and require the word to be at the start
                    if "obsolete" in label:
                        continue
                    if label.startswith(base) and "food product" in label:
                        return term, 0.8

        # 6. Fallback to general label_to_term (may include non-food terms)
        if name_lower in self.label_to_term:
            term = self.label_to_term[name_lower]
            # Only use if it's a FOODON term
            if term.id.startswith("FOODON:"):
                return term, 0.7

        # No match found
        return None, 0.0

    def lookup_foodon_term(self, name: str) -> Optional[str]:
        """Look up FoodOn term ID for ingredient name.

        Returns:
            FoodOn term ID (e.g., "FOODON:00001142") or None if not found
        """
        term, confidence = self.lookup(name)
        return term.id if term else None

    def get_ancestors(self, term_id: str) -> set:
        """Get all ancestor IDs for a FoodOn term.

        Args:
            term_id: FoodOn term ID (e.g., "FOODON:00001142")

        Returns:
            Set of ancestor term IDs
        """
        try:
            term = self.ontology.get(term_id)
            if term:
                return set(a.id for a in term.superclasses())
        except Exception:
            pass
        return set()

    def extract_features(self, name: str) -> np.ndarray:
        """
        Extract FoodOn feature vector for ingredient name.

        Features (20 dimensions):
        - 0-8: Binary type features (vegetable, fruit, grain, meat, fish, dairy, nut, spice, beverage)
        - 9-13: Processing features (raw, cooked, preserved, fermented, processed)
        - 14-17: Source features (plant, animal, fungus, mineral)
        - 18-19: Numeric features (hierarchy_depth, match_confidence)

        Args:
            name: Ingredient name

        Returns:
            Feature vector of shape (20,)
        """
        term, confidence = self.lookup(name)

        # Initialize feature vector (20 dims)
        features = np.zeros(FOODON_FEATURE_DIM, dtype=np.float32)

        if term is None:
            # No FoodOn match - return zeros (will rely on E5/regex)
            return features

        # Get all ancestor IDs
        ancestors = set(a.id for a in term.superclasses())

        # Binary type features (0-8)
        # Check BOTH food product hierarchy AND material hierarchy
        is_fish_material = FOODON_MATERIAL_CATEGORIES["fish_seafood"] in ancestors
        is_fish_product = FOODON_CATEGORIES["fish_seafood"] in ancestors
        is_other_seafood = bool(ancestors & FOODON_SEAFOOD_EXTRA)  # shellfish, molluscs
        is_fish = is_fish_material or is_fish_product or is_other_seafood
        is_vertebrate = FOODON_MATERIAL_CATEGORIES["vertebrate"] in ancestors
        is_plant_material = FOODON_MATERIAL_CATEGORIES["plant"] in ancestors
        is_legume = FOODON_CATEGORIES["legume"] in ancestors
        is_egg = FOODON_CATEGORIES["egg"] in ancestors

        # Compute specific plant categories first (grain, fruit, nut) before vegetable fallback
        is_grain = (
            FOODON_CATEGORIES["grain"] in ancestors
            or bool(ancestors & FOODON_GRAIN_EXTRA)
            or term.id in FOODON_GRAIN_EXTRA
        )
        is_fruit = FOODON_CATEGORIES["fruit"] in ancestors
        is_nut_oilseed = FOODON_CATEGORIES["nut_oilseed"] in ancestors

        # Vegetable: plant material that's NOT grain/fruit/nut, OR explicit vegetable, OR legume
        features[0] = (
            1.0
            if (
                FOODON_CATEGORIES["vegetable"] in ancestors
                or is_legume  # legumes → vegetable category
                # plant material fallback only if not a more specific category
                or (is_plant_material and not is_grain and not is_fruit and not is_nut_oilseed)
            )
            else 0.0
        )
        features[1] = 1.0 if is_fruit else 0.0
        features[2] = 1.0 if is_grain else 0.0
        # Meat: vertebrate material (NOT fish), OR egg (eggs → meat in Ecobalyse)
        features[3] = (
            1.0 if ((is_vertebrate and not is_fish) or is_egg) else 0.0
        )  # is_meat
        features[4] = 1.0 if is_fish else 0.0  # is_fish
        features[5] = 1.0 if FOODON_CATEGORIES["dairy"] in ancestors else 0.0
        features[6] = 1.0 if is_nut_oilseed else 0.0
        features[7] = 1.0 if FOODON_CATEGORIES["spice"] in ancestors else 0.0
        features[8] = 1.0 if FOODON_CATEGORIES["beverage"] in ancestors else 0.0

        # Processing features (9-13) - detect from term name
        term_name_lower = (term.name or "").lower()
        features[9] = 1.0 if "raw" in term_name_lower else 0.0  # is_raw
        features[10] = (
            1.0
            if any(
                w in term_name_lower for w in ["cooked", "roasted", "fried", "boiled"]
            )
            else 0.0
        )  # is_cooked
        features[11] = (
            1.0
            if any(
                w in term_name_lower for w in ["canned", "frozen", "dried", "preserved"]
            )
            else 0.0
        )  # is_preserved
        features[12] = (
            1.0 if any(w in term_name_lower for w in ["fermented", "pickled"]) else 0.0
        )  # is_fermented
        features[13] = (
            1.0 if any(w in term_name_lower for w in ["processed", "prepared"]) else 0.0
        )  # is_processed

        # Source features (14-17) - check ancestors
        features[14] = (
            1.0
            if (FOODON_CATEGORIES["plant"] in ancestors or is_plant_material)
            else 0.0
        )  # source_plant
        # Animal source: check for animal-related ancestors
        animal_keywords = ["animal", "meat", "poultry", "beef", "pork", "chicken"]
        features[15] = (
            1.0 if any(kw in str(ancestors).lower() for kw in animal_keywords) else 0.0
        )  # source_animal
        features[16] = (
            1.0 if "fungus" in str(ancestors).lower() else 0.0
        )  # source_fungus
        features[17] = 0.0  # source_mineral (salt, etc.) - rare

        # Numeric features (18-19)
        features[18] = min(len(ancestors) / 10.0, 1.0)  # hierarchy depth normalized
        features[19] = confidence  # match confidence

        return features

    def get_nova_group(self, name: str) -> tuple[int | None, str, float]:
        """
        Determine NOVA group from FoodOn ontology classes.

        Checks if term or its ancestors match NOVA processing classes.
        Returns highest NOVA group found (4 > 3 > 2).

        Args:
            name: Ingredient name

        Returns:
            (nova_group, reason, confidence) - nova_group is None if no match
        """
        term, confidence = self.lookup(name)

        if term is None:
            return None, "no_foodon_match", 0.0

        # Get all ancestor IDs
        ancestors = set(a.id for a in term.superclasses())
        ancestors.add(term.id)  # Include the term itself

        # Check for NOVA processing classes in ancestors
        # Priority: NOVA 4 > NOVA 3 > NOVA 2
        found_nova = None
        found_reason = None

        for ancestor_id in ancestors:
            if ancestor_id in NOVA_PROCESSING_CLASSES:
                nova = NOVA_PROCESSING_CLASSES[ancestor_id]
                if found_nova is None or nova > found_nova:
                    found_nova = nova
                    found_reason = f"foodon_{ancestor_id}"

        if found_nova:
            return found_nova, found_reason, confidence

        # Check term name for processing keywords (fallback)
        term_name_lower = (term.name or "").lower()

        # NOVA 4 keywords
        if any(w in term_name_lower for w in ["isolate", "hydrolyzed", "textured"]):
            return 4, "foodon_keyword_ultraprocessed", confidence * 0.8

        # NOVA 3 keywords
        if any(w in term_name_lower for w in ["canned", "pickled", "cured", "smoked"]):
            return 3, "foodon_keyword_processed", confidence * 0.8

        # NOVA 2 keywords
        if any(w in term_name_lower for w in ["oil", "butter", "sugar", "salt", "flour", "starch"]):
            return 2, "foodon_keyword_culinary", confidence * 0.8

        # NOVA 1: raw indicator or no processing found
        if "raw" in term_name_lower or "(raw)" in term_name_lower:
            return 1, "foodon_raw", confidence

        # No specific NOVA indicator found
        return None, "foodon_unclassified", confidence


# Singleton instance for reuse
_instance = None


def get_extractor() -> FoodOnFeatureExtractor:
    """Get or create singleton FoodOnFeatureExtractor instance."""
    global _instance
    if _instance is None:
        _instance = FoodOnFeatureExtractor()
    return _instance
