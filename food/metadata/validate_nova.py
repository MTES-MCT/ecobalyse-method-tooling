#!/usr/bin/env python3
"""
Validate NOVA classification generalization using held-out test sets.

This script measures how well the NOVA classifier generalizes by:
1. Splitting reference data into train/test sets
2. Training only on the training set
3. Evaluating predictions on the held-out test set
4. Reporting accuracy and confusion matrix

Usage:
    python validate_nova.py                    # Run with default 80/20 split
    python validate_nova.py --test-ratio 0.3   # Use 70/30 split
    python validate_nova.py --folds 5          # 5-fold cross-validation
"""

import argparse
import json
import os
import random
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import bw2data

bw2data.projects.set_current("ecobalyse")

from predict import Predictor

REFERENCE_DIR = Path(__file__).parent / "reference"


def load_nova_reference() -> list[dict]:
    """Load NOVA reference data with comments stripped."""
    items = []
    with open(REFERENCE_DIR / "nova_classification.csv") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("name,"):  # header
                continue
            parts = line.split(",")
            if len(parts) >= 2:
                items.append({"name": parts[0], "nova": int(parts[1])})
    return items


def evaluate_predictions(
    predictor: Predictor, test_items: list[dict], verbose: bool = False
) -> dict:
    """
    Evaluate NOVA predictions on test items.

    Returns dict with accuracy, per-class metrics, and confusion matrix.
    """
    correct = 0
    total = 0
    confusion = defaultdict(lambda: defaultdict(int))
    errors = []

    for item in test_items:
        name = item["name"]
        expected_nova = item["nova"]

        # Create a dummy ingredient for prediction
        ingredient = {"name": name, "activityName": ""}
        predictions = predictor.predict(ingredient)
        predicted_nova = predictions.get("novaGroup", 1)
        reason = predictions.get("novaGroupReason", "unknown")

        confusion[expected_nova][predicted_nova] += 1
        total += 1

        if predicted_nova == expected_nova:
            correct += 1
        else:
            errors.append(
                {
                    "name": name,
                    "expected": expected_nova,
                    "predicted": predicted_nova,
                    "reason": reason,
                }
            )

    accuracy = correct / total if total > 0 else 0

    # Per-class metrics
    per_class = {}
    for nova in [1, 2, 3, 4]:
        tp = confusion[nova][nova]
        fp = sum(confusion[other][nova] for other in [1, 2, 3, 4] if other != nova)
        fn = sum(confusion[nova][other] for other in [1, 2, 3, 4] if other != nova)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0
        )

        per_class[nova] = {"precision": precision, "recall": recall, "f1": f1}

    return {
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "confusion": dict(confusion),
        "per_class": per_class,
        "errors": errors,
    }


def print_results(results: dict, fold_name: str = ""):
    """Print evaluation results."""
    prefix = f"[{fold_name}] " if fold_name else ""

    print(f"\n{prefix}=== Results ===")
    print(f"{prefix}Accuracy: {results['accuracy']:.1%} ({results['correct']}/{results['total']})")

    print(f"\n{prefix}Confusion Matrix (rows=expected, cols=predicted):")
    print(f"{prefix}        NOVA1  NOVA2  NOVA3  NOVA4")
    for expected in [1, 2, 3, 4]:
        row = results["confusion"].get(expected, {})
        vals = [row.get(p, 0) for p in [1, 2, 3, 4]]
        print(f"{prefix}NOVA{expected}:  {vals[0]:5d}  {vals[1]:5d}  {vals[2]:5d}  {vals[3]:5d}")

    print(f"\n{prefix}Per-class metrics:")
    print(f"{prefix}       Precision  Recall    F1")
    for nova in [1, 2, 3, 4]:
        m = results["per_class"][nova]
        print(f"{prefix}NOVA{nova}:   {m['precision']:.3f}     {m['recall']:.3f}   {m['f1']:.3f}")

    if results["errors"]:
        print(f"\n{prefix}Errors ({len(results['errors'])}):")
        for err in results["errors"][:10]:  # Show first 10
            print(
                f"{prefix}  {err['name']}: expected NOVA {err['expected']}, "
                f"got NOVA {err['predicted']} ({err['reason']})"
            )
        if len(results["errors"]) > 10:
            print(f"{prefix}  ... and {len(results['errors']) - 10} more")


def run_single_split(
    reference_items: list[dict], test_ratio: float, seed: int, verbose: bool
) -> dict:
    """Run a single train/test split evaluation."""
    random.seed(seed)
    shuffled = reference_items.copy()
    random.shuffle(shuffled)

    split_idx = int(len(shuffled) * (1 - test_ratio))
    train_items = shuffled[:split_idx]
    test_items = shuffled[split_idx:]

    print(f"Split: {len(train_items)} train, {len(test_items)} test")

    # Load training data (existing ingredients from activities.json)
    training_data_path = Path(__file__).parent / os.environ["TRAINING_DATA"]
    with open(training_data_path) as f:
        training_data = json.load(f)

    # Train predictor
    predictor = Predictor()
    predictor.fit(training_data)

    # Temporarily replace nova_matcher with one trained only on train_items
    from predict import NearestNeighborMatcher

    train_names = [item["name"] for item in train_items]
    train_novas = [item["nova"] for item in train_items]
    train_sources = ["reference"] * len(train_items)

    predictor.nova_matcher = NearestNeighborMatcher(
        train_names,
        train_novas,
        sources=train_sources,
        translate_fn=predictor._translate,
        foodon_extractor=predictor.foodon_extractor,
    )

    # Evaluate on test set
    results = evaluate_predictions(predictor, test_items, verbose)
    return results


def run_cross_validation(
    reference_items: list[dict], n_folds: int, seed: int, verbose: bool
) -> dict:
    """Run k-fold cross-validation."""
    random.seed(seed)
    shuffled = reference_items.copy()
    random.shuffle(shuffled)

    fold_size = len(shuffled) // n_folds
    all_results = []

    # Load training data once
    training_data_path = Path(__file__).parent / os.environ["TRAINING_DATA"]
    with open(training_data_path) as f:
        training_data = json.load(f)

    for fold in range(n_folds):
        start_idx = fold * fold_size
        end_idx = start_idx + fold_size if fold < n_folds - 1 else len(shuffled)

        test_items = shuffled[start_idx:end_idx]
        train_items = shuffled[:start_idx] + shuffled[end_idx:]

        print(f"\nFold {fold + 1}/{n_folds}: {len(train_items)} train, {len(test_items)} test")

        # Train predictor
        predictor = Predictor()
        predictor.fit(training_data)

        # Replace nova_matcher with fold-specific one
        from predict import NearestNeighborMatcher

        train_names = [item["name"] for item in train_items]
        train_novas = [item["nova"] for item in train_items]
        train_sources = ["reference"] * len(train_items)

        predictor.nova_matcher = NearestNeighborMatcher(
            train_names,
            train_novas,
            sources=train_sources,
            translate_fn=predictor._translate,
            foodon_extractor=predictor.foodon_extractor,
        )

        # Evaluate
        results = evaluate_predictions(predictor, test_items, verbose)
        all_results.append(results)

        if verbose:
            print_results(results, f"Fold {fold + 1}")

    # Aggregate results
    total_correct = sum(r["correct"] for r in all_results)
    total_items = sum(r["total"] for r in all_results)
    avg_accuracy = total_correct / total_items if total_items > 0 else 0

    # Aggregate confusion matrix
    agg_confusion = defaultdict(lambda: defaultdict(int))
    for r in all_results:
        for expected, preds in r["confusion"].items():
            for predicted, count in preds.items():
                agg_confusion[expected][predicted] += count

    # Aggregate per-class
    agg_per_class = {}
    for nova in [1, 2, 3, 4]:
        tp = agg_confusion[nova][nova]
        fp = sum(agg_confusion[other][nova] for other in [1, 2, 3, 4] if other != nova)
        fn = sum(agg_confusion[nova][other] for other in [1, 2, 3, 4] if other != nova)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0
        )
        agg_per_class[nova] = {"precision": precision, "recall": recall, "f1": f1}

    # Collect all errors
    all_errors = []
    for r in all_results:
        all_errors.extend(r["errors"])

    return {
        "accuracy": avg_accuracy,
        "correct": total_correct,
        "total": total_items,
        "confusion": dict(agg_confusion),
        "per_class": agg_per_class,
        "errors": all_errors,
        "fold_accuracies": [r["accuracy"] for r in all_results],
    }


def main():
    parser = argparse.ArgumentParser(description="Validate NOVA classification")
    parser.add_argument(
        "--test-ratio",
        type=float,
        default=0.2,
        help="Ratio of data to use for testing (default: 0.2)",
    )
    parser.add_argument(
        "--folds",
        type=int,
        default=0,
        help="Number of folds for cross-validation (0 = single split)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    print("Loading NOVA reference data...")
    reference_items = load_nova_reference()
    print(f"Loaded {len(reference_items)} reference items")

    # Show distribution
    nova_counts = Counter(item["nova"] for item in reference_items)
    print(f"Distribution: {dict(sorted(nova_counts.items()))}")

    if args.folds > 0:
        print(f"\nRunning {args.folds}-fold cross-validation...")
        results = run_cross_validation(
            reference_items, args.folds, args.seed, args.verbose
        )
        print("\n" + "=" * 50)
        print("CROSS-VALIDATION RESULTS")
        print("=" * 50)
        print(f"Fold accuracies: {[f'{a:.1%}' for a in results['fold_accuracies']]}")
    else:
        print(f"\nRunning single {1-args.test_ratio:.0%}/{args.test_ratio:.0%} split...")
        results = run_single_split(
            reference_items, args.test_ratio, args.seed, args.verbose
        )

    print_results(results)

    # Summary assessment
    print("\n" + "=" * 50)
    print("GENERALIZATION ASSESSMENT")
    print("=" * 50)
    if results["accuracy"] >= 0.8:
        print("✓ Good generalization (≥80% accuracy on held-out data)")
    elif results["accuracy"] >= 0.6:
        print("⚠ Moderate generalization (60-80% accuracy)")
    else:
        print("✗ Poor generalization (<60% accuracy) - likely overfitting")

    # Identify problematic patterns
    error_reasons = Counter(err["reason"] for err in results["errors"])
    if error_reasons:
        print("\nMost common error reasons:")
        for reason, count in error_reasons.most_common(5):
            print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()
