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
├── reference/
│   ├── food_type.csv              # custom food type mappings
│   ├── processing_state.csv       # custom processing state mappings
│   ├── nova_classification.csv    # NOVA 1-4 reference data
│   ├── cropgroup.csv              # custom crop group mappings
│   ├── density.csv                # custom density values
│   ├── inedible_part.csv          # custom inedible part percentages
│   ├── fao_density.csv            # FAO density reference
│   ├── agb_inedible.csv           # AGB inedible reference
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
uv run export.py                # Export predictions to CSV + JSON
uv run export.py --clear-cache  # Clear translation cache first
uv run validate_nova.py --folds 5  # Validate NOVA classification
```

The `fao_density.csv` and `agb_inedible.csv` should not be changed, they are original AGB and FAO values.
All other reference files can be adapted at will.

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
            │  │ (25 binary)    │  │                    │
            │  └────────────────┘  │                    │
            └──────────┬───────────┘                    │
                       │                                │
                       ▼                                ▼
            ┌─────────────────────────────────────────────────────────┐
            │           NearestNeighborMatcher (one per field)        │
            │                                                         │
            │   foodType_matcher ←── food_type.csv only               │
            │   nova_matcher ←── nova_classification.csv              │
            │   processingState_matcher ←── ingredients + CSV         │
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
│   PRIORITY 2: Substring Match (confidence = 0.95)                 │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ Minimum 5 characters required (to avoid false matches)  │     │
│   │ Longest match wins                                       │     │
│   └─────────────────────────────────────────────────────────┘     │
│                          │                                        │
│                          ▼ (if no substring match)                │
│   PRIORITY 3: FoodOn + Regex Similarity (confidence = cosine)     │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ 45-dim feature vector: 20 FoodOn + 25 regex             │     │
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

### FoodOn Ontology Features (20 dimensions)

```
Query: "Watermelon"
         ↓
   FoodOn Ontology (52,628 food terms)
         ↓
   Find top-20 most similar terms by word overlap
         ↓
   [0.8, 0.6, 0.4, 0.3, ...] (20 similarity scores)
```

### Regex Binary Features (25 dimensions)

```python
DETECTION_PATTERNS = {
    "is_meat": r"\b(viande|meat|boeuf|porc|poulet|...)\b",
    "is_fish": r"\b(poisson|fish|saumon|thon|...)\b",
    "is_dairy": r"\b(lait|milk|fromage|cheese|...)\b",
    "is_vegetable": r"\b(légume|vegetable|carotte|...)\b",
    "is_fruit": r"\b(fruit|pomme|orange|...)\b",
    "is_frozen": r"\b(surgelé|frozen|congelé)\b",
    "is_canned": r"\b(conserve|canned|boîte)\b",
    # ... 25 total patterns
}
```

## Confidence Scores

| Match Type | Confidence |
|------------|------------|
| Exact match | 1.0 |
| Substring match (min 5 chars) | 0.95 |
| FoodOn + regex similarity | 0.0 - 1.0 (cosine) |

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
