This is a separated experiment to compute metadata from a list of new ingredients

# Ingredient Metadata Prediction System

## Directory Structure

```
metadata/
├── source/
│   └── new_ingredient_FR.csv      # Input: new ingredients to predict
├── generated/
│   ├── predictions.csv            # Output: CSV with predictions + confidence
│   └── new_activities.json        # Output: activities.json format
├── data/
│   └── foodon.owl                 # FoodOn ontology (auto-downloaded)
├── reference/
│   ├── food_type.csv              # custom food type mappings
│   ├── processing_state.csv       # custom processing state mappings
│   ├── nova_classification.csv    # NOVA 1-4 reference data
│   ├── cropgroup.csv              # custom crop group mappings
│   ├── density.csv                # custom density values
│   ├── inedible_part.csv          # custom inedible part percentages
│   ├── fao_density.csv            # FAO density reference (do not modify)
│   ├── agb_inedible.csv           # AGB inedible reference (do not modify)
│   ├── cooked_to_raw.csv          # custom cooked/raw ratios
│   └── transport_cooling.csv      # custom transport cooling
├── export.py                      # Main export script
├── predict.py                     # Predictor class
├── validate_nova.py               # NOVA classification validation
├── foodon_loader.py               # FoodOn ontology loader
└── .env                           # Environment configuration
```

## Setup

Create a `.env` file with required environment variables:

```bash
BRIGHTWAY2_DIR=/path/to/brightway-dirs/main
TRAINING_DATA=../data/activities.json
```

## Usage

```bash
uv run export.py                   # Export predictions to CSV + JSON
uv run export.py --clear-cache     # Clear translation cache first
uv run validate_nova.py --folds 5  # Validate NOVA classification (5-fold CV)
```

## Architecture

### Training Phase

```
ingredients.json ──────┐
(existing              │
 ingredients)          │
                       ▼
            ┌──────────────────────┐
            │  French→English      │
            │  Translation         │
            │  (Helsinki-NLP)      │
            └──────────┬───────────┘
                       │
                       ▼
            ┌──────────────────────┐      ┌─────────────────────────────┐
            │  Feature Extraction  │      │   Reference CSV Data        │
            │  per ingredient      │      │                             │
            │                      │      │  • food_type.csv            │
            │  ┌────────────────┐  │      │  • processing_state.csv     │
            │  │ FoodOn Ontology│  │      │  • cropgroup.csv            │
            │  │ (52K terms)    │  │      │  • density.csv              │
            │  │ → 20 dims      │  │      │  • inedible_part.csv        │
            │  └────────────────┘  │      │  • cooked_to_raw.csv        │
            │  ┌────────────────┐  │      │  • transport_cooling.csv    │
            │  │ Regex Patterns │  │      └─────────────┬───────────────┘
            │  │ (28 binary)    │  │                    │
            │  └────────────────┘  │                    │
            └──────────┬───────────┘                    │
                       │                                │
                       ▼                                ▼
            ┌─────────────────────────────────────────────────────────┐
            │           NearestNeighborMatcher (one per field)        │
            │                                                         │
            │   foodType_matcher ←── food_type.csv only               │
            │   nova_matcher ←── nova_classification.csv              │
            │   cropGroup_matcher ←── ingredients + CSV               │
            │   transportCooling_matcher ←── ingredients + CSV        │
            │   density_matcher ←── ingredients + CSV                 │
            │   inediblePart_matcher ←── ingredients + CSV            │
            │   rawToCookedRatio_matcher ←── ingredients + CSV        │
            └─────────────────────────────────────────────────────────┘
```

### Prediction Phase

```
New Ingredient
┌─────────────────────────┐
│ name: "Watermelon"      │
│ activityName: ".." (opt)│
└───────────┬─────────────┘
            │
            ▼
┌───────────────────────────────────────────────────────────────────┐
│                    NearestNeighborMatcher.predict()               │
│                                                                   │
│   PRIORITY 1: Exact Text Match (confidence = 1.0)                 │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ "watermelon" == "watermelon" in reference?              │     │
│   └─────────────────────────────────────────────────────────┘     │
│                          │                                        │
│                          ▼ (if no exact match)                    │
│   PRIORITY 2: Word Boundary Match (confidence = 0.95)             │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ Uses regex \b word boundaries to match complete words   │     │
│   │ Prevents false positives like "bread" in "breadfruit"   │     │
│   └─────────────────────────────────────────────────────────┘     │
│                          │                                        │
│                          ▼ (if no word match)                     │
│   PRIORITY 3: FoodOn + Regex Similarity (confidence = cosine)     │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ 48-dim feature vector: 20 FoodOn + 28 regex             │     │
│   │ Cosine similarity with all reference items              │     │
│   └─────────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────────────────────────────────┐
│                      Predicted Metadata                           │
│                                                                   │
│   foodType: "fruit"                                               │
│   processingState: "raw"                                          │
│   cropGroup: "LEGUMES-FLEURS"                                     │
│   transportCooling: "always"                                      │
│   density: 1.0                                                    │
│   inediblePart: 0.35                                              │
│   rawToCookedRatio: 1.0                                           │
└───────────────────────────────────────────────────────────────────┘
```

## NOVA Classification

The system classifies ingredients into NOVA 4-group categories:

| NOVA Group | Description | Examples |
|------------|-------------|----------|
| **NOVA 1** | Unprocessed/minimally processed | Fresh fruits, vegetables, meat, fish, milk, eggs, dried/frozen foods |
| **NOVA 2** | Culinary ingredients | Oils, butter, sugar, salt, flour, starch, vinegar |
| **NOVA 3** | Processed foods | Canned vegetables, cheese, ham, bacon, smoked fish, jam |
| **NOVA 4** | Ultra-processed | Textured proteins, corn syrup, distilled spirits, ready meals |

### NOVA Detection Priority

```
1. Distilled spirits (brandy, vodka, etc.) → NOVA 4
2. NOVA 2 culinary ingredients (oil, butter, sugar, salt, flour)
3. NOVA 3 processed indicators (jam, pickled, cured, smoked, ham, bacon)
4. NOVA 4 ultra-processed (textured, hydrolyzed, corn syrup, maltodextrin)
5. Activity name patterns ("at farm", "at plant", "production")
6. FoodType-based defaults (fruit/vegetable/fish → NOVA 1)
7. Nearest neighbor matching on reference data
8. Default: NOVA 1
```

### Validation

The NOVA classifier can be validated using cross-validation:

```bash
uv run validate_nova.py --folds 5   # 5-fold cross-validation
uv run validate_nova.py --test-ratio 0.3  # 70/30 train/test split
```

Current performance (5-fold CV on 93 reference items):
- **Accuracy: 89.2%**
- NOVA 1: F1=0.893, NOVA 2: F1=0.960, NOVA 3: F1=0.829, NOVA 4: F1=0.872

### processingState Derivation

`processingState` is derived from NOVA:
- NOVA 1 → `raw`
- NOVA 2, 3, 4 → `processed`

## Output Format

### predictions.csv

| Column | Description |
|--------|-------------|
| name | Ingredient name |
| categories | Predicted categories (comma-separated) |
| foodType | Food type + match name + confidence |
| **novaGroup** | NOVA 1-4 classification |
| **novaGroupReason** | Detection method used |
| **novaGroupConf** | Confidence score |
| processingState | Derived from NOVA (raw/processed) |
| transportCooling | Transport cooling + match |
| cropGroup | Crop group + match + confidence |
| density | Density value + match + confidence |
| inediblePart | Inedible part + match + confidence |
| rawToCookedRatio | Raw-to-cooked ratio + match + confidence |

### new_activities.json

Match info includes source file and confidence:

```json
{
  "ingredientDensity": 0.9,
  "ingredientDensityMatch": {
    "file": "density.csv",
    "name": "bell pepper",
    "confidence": 0.95
  }
}
```

## Feature Extraction

Each ingredient is represented as a 48-dimensional feature vector (20 FoodOn + 28 regex).

### FoodOn Ontology Features (20 dimensions)

The FoodOn ontology (~50K food terms) provides structured semantic features:

```
Query: "Watermelon"
         ↓
   FoodOn Ontology lookup (exact/fuzzy match)
         ↓
   Extract ancestor categories from ontology graph
         ↓
   20-dim feature vector:
     - dims 0-8:  Type flags (vegetable, fruit, grain, meat, fish, dairy, nut, spice, beverage)
     - dims 9-13: Processing flags (raw, cooked, preserved, fermented, processed)
     - dims 14-17: Source flags (plant, animal, fungus, mineral)
     - dims 18-19: Numeric (hierarchy_depth, match_confidence)
```

### Regex Binary Features (28 dimensions)

Pattern-based detection for French/English ingredient names:

```python
DETECTION_PATTERNS = {
    # Processing (9 patterns)
    "is_organic", "is_fresh", "is_frozen", "is_cooked", "is_raw",
    "is_dried", "is_processed", "is_canned", "is_smoked",

    # Food types - Animals (5 patterns)
    "is_meat", "is_fish", "is_seafood", "is_egg", "is_dairy",

    # Food types - Plants (9 patterns)
    "is_vegetable", "is_fruit", "is_grain", "is_legume", "is_nut_seed",
    "is_oil_fat", "is_spice", "is_beverage", "is_sugar_sweet",

    # LCA process info (5 patterns)
    "at_farm_gate", "at_plant", "at_processing", "is_greenhouse", "is_heated_greenhouse"
}
```

## Confidence Scores

| Match Type | Confidence |
|------------|------------|
| Exact match | 1.0 |
| Word boundary match | 0.95 |
| FoodOn + regex similarity | 0.0 - 1.0 (cosine) |

## Category Computation

Categories are computed directly from `foodType + novaGroup`:

```python
def _compute_category(food_type, nova_group):
    is_raw = nova_group == 1

    if food_type in {"vegetable", "fruit"}:
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
```

## transportCooling Rules

transportCooling is determined by rules based on `foodType + novaGroup`:

```
1. Packaging detection (frozen → always, dried/canned → none)
2. NOVA 1 (raw) + perishable type (vegetable, fruit, meat, fish, dairy) → always
3. Non-perishable types (grain, nut_oilseed, spice_condiment) → none
4. Fallback: nearest neighbor matching
```

This rule-based approach handles ~97% of items (220/228), with only edge cases falling back to the matcher.

## Example

Input: `{"name": "Salmon fillet", "activityName": "Salmon, fillet, at plant {NO}"}`

```
Step 1: Translate → "Salmon fillet" (already English)

Step 2: For each field, find best match:
  ├─ foodType:         fish_seafood (rule: is_fish pattern)
  ├─ novaGroup:        1 (fresh_at_plant: fish at plant = minimal processing)
  ├─ processingState:  raw (derived from NOVA 1)
  ├─ cropGroup:        N/A (animal product)
  ├─ transportCooling: always (rule: fresh fish)
  ├─ density:          1.05 (nearest neighbor)
  ├─ inediblePart:     0.0 (nearest neighbor)
  └─ rawToCookedRatio: 0.75 (nearest neighbor)

Output: {
  "foodType": "fish_seafood",
  "novaGroup": 1,
  "novaGroupReason": "fresh_at_plant",
  "processingState": "raw",
  "transportCooling": "always",
  "density": 1.05,
  "inediblePart": 0.0,
  "rawToCookedRatio": 0.75
}
```
