# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The `predict/` module provides ML-based metadata prediction for food ingredients in the Ecobalyse environmental impact calculator. It predicts multiple metadata fields (`categories`, `cropGroup`, `transportCooling`, `defaultOrigin`, `density`, `inediblePart`, `rawToCookedRatio`) from an ingredient's name and LCA process name.

## CLI Commands

```bash
# Train predictor on existing ingredients
python -m ecobalyse_data.detect.predict train ingredients.json --output model.pkl

# Predict metadata for a new ingredient
python -m ecobalyse_data.detect.predict infer model.pkl --name "Tomate cerise" --activity "Cherry tomato {FR} U"

# Show similar ingredients during inference
python -m ecobalyse_data.detect.predict infer model.pkl --name "Tomate cerise" --activity "Cherry tomato {FR} U" --similar 5

# Evaluate with cross-validation
python -m ecobalyse_data.detect.predict evaluate ingredients.json

# Run demo
python demo_predict.py
```

## Architecture

### Predictor Class

The `Predictor` class combines multiple ML approaches:
- **RandomForest classifiers** for categorical fields (`categories`, `cropGroup`, `transportCooling`)
- **KNN with cosine distance** for continuous values (`density`, `inediblePart`, `rawToCookedRatio`)
- **Rule-based extraction** for `defaultOrigin` (parsed from activity name location codes like `{FR}`)

### Feature Extraction

Features are extracted via `extract_features(name, activity_name, embedding_model)`:
1. **Semantic embedding** (384 dims) from `all-MiniLM-L6-v2` sentence transformer
2. **Binary regex features** (25 dims) detecting food types, processing states, and LCA context

Binary features are defined in `DETECTION_PATTERNS` dict (e.g., `is_organic`, `is_meat`, `is_frozen`, `at_farm_gate`).

### Prediction Logic

1. **Rules first**: Deterministic rules in `_predict_category_by_rules()` and `_predict_transport_by_rules()` take priority
2. **ML fallback**: RandomForest classifiers used when rules don't match
3. **KNN interpolation**: Continuous values computed as distance-weighted average of 5 nearest neighbors

### Detector Interface

The `Detector` class provides compatibility with other `ecobalyse_data.detect` modules:
- `detect(ingredient)` returns `(predictions, score, best_match)`
- `update(input_json, threshold, debug)` for batch processing

## Key Constants

- `MODEL = "all-MiniLM-L6-v2"` - Embedding model
- `BASE_CATEGORIES` - 10 mutually exclusive food categories
- `ADDITIVE_LABELS = ["organic", "bleublanccoeur"]` - Labels that combine with base categories
- `ORIGIN_MAPPING` - Maps location codes to origin values
- `THRESHOLD = 0.6` - Default confidence threshold for batch updates

## Dependencies

- `sentence_transformers` - Semantic embeddings (lazy-loaded)
- `sklearn` - RandomForestClassifier, NearestNeighbors, LabelEncoder
- `numpy` - Feature vectors
- `rich` - Progress bars (for `update()`)
- don't mention Claude in the commits