"""Read / write / merge the per-activity `lci_catalog/<source-slug>/<alias>.json`
layout that replaced the monolithic `activities.json`.

Pure stdlib module: no `bw2data`, no `pandas`, no `dotenv`, no `Predictor`.
Importable from any tool that needs to load or merge the catalog without
pulling in `export.py`'s heavy LCA stack.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Namespace UUID for deterministic UUID generation (matches export.py).
ECOBALYSE_NAMESPACE = uuid.UUID("a4e1d123-5c67-4b89-9def-1234567890ab")

# Legacy ingredient-only namespace markers used to age out pre-existing
# Ecobalyse identities while a fresh export takes over the canonical alias.
OLD_DISPLAY_SUFFIX = " (2025)"
OLD_ALIAS_SUFFIX = "-2025"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def extract_geo(activity_name: str) -> str:
    """Return the lowercase code inside the first `{…}` of an activityName, or ''."""
    m = re.search(r"\{([^}]+)\}", activity_name)
    return m.group(1).lower() if m else ""


def normalize_display_name(name: str, old_suffix: str) -> str:
    if not old_suffix:
        return name
    return name[: -len(old_suffix)] if name.endswith(old_suffix) else name


def normalize_alias(alias: str | None, old_suffix: str) -> str | None:
    if alias is None:
        return None
    if not old_suffix:
        return alias
    return alias[: -len(old_suffix)] if alias.endswith(old_suffix) else alias


# Adapted from https://github.com/django/django/blob/main/django/utils/text.py
def slugify(value: str) -> str:
    """Slugify a database source name into a directory-safe identifier.

    Mirrors `ECOBALYSE_DATA/migrations/2026-06-14-explode-activities-json.py`
    so the lci_catalog subdirectory names match exactly.
    """
    value = str(value)
    value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    value = re.sub(r"[^\w^\.\s-]", "", value.lower())
    return re.sub(r"[-.\s]+", "-", value).strip("-_")


# ---------------------------------------------------------------------------
# Catalog read / write
# ---------------------------------------------------------------------------


def load_lci_catalog(catalog_dir: Path) -> list[dict]:
    """Load every activity from `catalog_dir/<source-slug>/<alias>.json`.

    The migration script strips `alias` before writing each file, so we
    re-inject it from the filename stem to rebuild a list compatible with
    the legacy activities.json format.

    Files whose stem ends with `-2025` are loaded last so that non-suffixed
    activities claim their canonical alias first; the suffixed legacy
    survivors then geo-disambiguate against them in
    `extract_activities_and_ingredients`.
    """
    activities: list[dict] = []
    paths = sorted(
        catalog_dir.glob("*/*.json"),
        key=lambda p: (p.stem.endswith(OLD_ALIAS_SUFFIX), str(p)),
    )
    for path in paths:
        with open(path, encoding="utf-8") as f:
            activity = json.load(f)
        activity["alias"] = path.stem
        activities.append(activity)
    return activities


def write_lci_catalog(activities: list[dict], catalog_dir: Path) -> None:
    """Write activities into `catalog_dir/<source-slug>/<alias>.json`.

    File format and naming convention follow the migration script:
      - subdirectory = slugify(activity["source"])
      - filename = activity["alias"] + ".json"
      - the `alias` field is removed from the on-disk payload
      - JSON is dumped with indent=2 and ensure_ascii=False

    Files no longer present in `activities` are deleted, so the catalog
    matches the merged result (same semantics as the previous full
    rewrite of activities.json).
    """
    catalog_dir.mkdir(exist_ok=True)

    desired: set[tuple[str, str]] = set()
    for activity in activities:
        source_slug = slugify(activity["source"])
        alias = activity["alias"]
        desired.add((source_slug, alias))

        source_dir = catalog_dir / source_slug
        source_dir.mkdir(exist_ok=True)

        payload = {k: v for k, v in activity.items() if k != "alias"}
        with open(source_dir / f"{alias}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")

    for path in catalog_dir.glob("*/*.json"):
        if (path.parent.name, path.stem) not in desired:
            path.unlink()


# ---------------------------------------------------------------------------
# Merge primitives
# ---------------------------------------------------------------------------


def extract_activities_and_ingredients(
    activities_list: list[dict],
    old_display_suffix: str,
    old_alias_suffix: str,
) -> tuple[dict[tuple[str, str], dict], dict[str, dict], list[dict]]:
    """Extract and normalize flat dicts from nested activities.json.

    Activities are keyed by **(source_slug, alias)** — the same tuple that
    determines the on-disk file path under `lci_catalog/`. Two activities
    sharing an alias across different sources (e.g. raw `banana-organic`
    in Ginko vs. transformed `banana-organic` in Ecobalyse) stay separate
    instead of silently clobbering each other on dict merge.

    Ingredients stay keyed by `displayName`; the link from ingredient to
    its hosting activity is the `activity_key` field (the tuple above).

    The legacy `-2025` / ` (2025)` markers are an ingredient-only namespace.
    Activity-level aliases and displayNames are always stripped of those
    markers on load (independently of `add_old_suffix`), and any
    within-source alias collision created by the strip is resolved by
    appending the geo code from the activityName.
    """
    activities: dict[tuple[str, str], dict] = {}
    ingredients: dict[str, dict] = {}
    other: list[dict] = []
    by_activity_name: dict[str, tuple[str, str]] = {}
    seen_display_names: set[str] = set()

    marker_strip_re = re.compile(
        r"\{\{([^}]+?)" + re.escape(OLD_ALIAS_SUFFIX) + r"\}\}"
    )

    for a in activities_list:
        if "displayName" not in a:
            other.append(a)
            continue
        act_name = a.get("activityName") or ""
        if act_name:
            act_name = marker_strip_re.sub(r"{{\1}}", act_name)
            a = {**a, "activityName": act_name}
        act_display = normalize_display_name(a["displayName"], OLD_DISPLAY_SUFFIX)
        act_alias = normalize_alias(a.get("alias", ""), OLD_ALIAS_SUFFIX) or ""
        src_slug = slugify(a.get("source", "")) or ""

        if act_name and act_name in by_activity_name:
            act_key = by_activity_name[act_name]
        else:
            act_key = (src_slug, act_alias)
            if act_alias and act_key in activities:
                geo = extract_geo(act_name or "").lower()
                if geo:
                    candidate_alias = f"{act_alias}-{geo}"
                    candidate_key = (src_slug, candidate_alias)
                    if candidate_key not in activities:
                        act_alias = candidate_alias
                        act_key = candidate_key
            if act_display and act_display in seen_display_names:
                geo_disp = extract_geo(act_name or "").upper()
                if geo_disp:
                    candidate_disp = f"{act_display} ({geo_disp})"
                    if candidate_disp not in seen_display_names:
                        act_display = candidate_disp
            seen_display_names.add(act_display)
            if act_name:
                by_activity_name[act_name] = act_key
            meta_list = a.get("metadata", [])
            non_food_meta = [m for m in meta_list if "food" not in m.get("scopes", [])]
            activities[act_key] = {k: v for k, v in a.items() if k != "metadata"}
            if non_food_meta:
                activities[act_key]["_non_food_metadata"] = non_food_meta
            activities[act_key]["displayName"] = act_display
            activities[act_key]["alias"] = act_alias

        for ing in (m for m in a.get("metadata", []) if "food" in m.get("scopes", [])):
            ing = {**ing, "activity_key": act_key}
            ing["displayName"] = normalize_display_name(ing["displayName"], old_display_suffix)
            ing["alias"] = normalize_alias(ing.get("alias", ""), old_alias_suffix)
            ingredients[ing["displayName"]] = ing

    return activities, ingredients, other


def apply_suffixes(
    activities: dict[str, dict],
    ingredients: dict[str, dict],
    new_ing_names: set[str],
    keep_set: set[str],
    old_display_suffix: str,
    old_alias_suffix: str,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """Add legacy suffixes to pre-existing **ingredients** only.

    The `-2025` / ` (2025)` suffix is an ingredient-only namespace: it tags
    Ecobalyse-side identities being phased out. Activities (= LCI process
    handles) are never suffixed — they live by their `alias` derived from the
    activityName.

    Returns (activities, ingredients, alias_renames) where alias_renames maps
    old_alias -> new_alias for every ingredient alias that was suffixed.
    """
    new_ings = {}
    ing_alias_renames = {}
    for dn, ing in ingredients.items():
        ing = {**ing}
        if dn not in new_ing_names and dn not in keep_set:
            if not ing["displayName"].endswith(old_display_suffix):
                ing["displayName"] += old_display_suffix
            if ing.get("alias") and not ing["alias"].endswith(old_alias_suffix):
                old_ing_alias = ing["alias"]
                ing["alias"] = old_ing_alias + old_alias_suffix
                ing_alias_renames[old_ing_alias] = ing["alias"]
        new_ings[dn] = ing
    return activities, new_ings, ing_alias_renames


def reassemble(
    activities: dict[tuple[str, str], dict],
    ingredients: dict[str, dict],
    other: list[dict],
) -> list[dict]:
    """Reassemble flat dicts back into nested activities.json format.

    Activities are dict-keyed by (source_slug, alias); ingredients link
    back via the `activity_key` tuple field.
    """
    by_activity: dict[tuple[str, str], list[dict]] = {}
    for ing in ingredients.values():
        ak = ing["activity_key"]
        by_activity.setdefault(ak, []).append(
            {k: v for k, v in ing.items() if k != "activity_key"}
        )

    result = []
    for act_key, act in activities.items():
        entry = {**act}
        non_food_meta = entry.pop("_non_food_metadata", [])
        ings = by_activity.get(act_key, [])
        food_ings = [{**ing, "scopes": ing.get("scopes", ["food"])} for ing in ings]
        all_meta = non_food_meta + food_ings
        if all_meta:
            entry["metadata"] = all_meta
        elif "ingredient" in entry.get("categories", []):
            continue  # Skip orphaned ingredient activities with no metadata
        result.append(entry)
    return result + other


def dedupe_suffixed_ids(entries: dict[str, dict], kind: str) -> None:
    """Regenerate UUIDs for entries with duplicate ids.

    Convention: UUIDs are derived from displayName. After a merge, an old
    entry that was renamed with OLD_DISPLAY_SUFFIX still carries the UUID
    derived from its pre-suffix displayName; a freshly generated new entry
    with that same pre-suffix displayName then ends up with the same UUID.

    We keep the un-suffixed entry's UUID as the canonical one (it may be
    referenced elsewhere) and regenerate the UUID of the suffixed entry from
    its current (suffixed) displayName. Suffixed entries are removed later,
    so a changed UUID has no downstream impact.

    Mutates `entries` in place.
    """
    id_counts = Counter(e["id"] for e in entries.values() if "id" in e)
    dupes = {uid for uid, n in id_counts.items() if n > 1}
    if not dupes:
        return
    for dn, e in entries.items():
        if e.get("id") in dupes and dn.endswith(OLD_DISPLAY_SUFFIX):
            e["id"] = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"{kind}:{dn}"))


# ---------------------------------------------------------------------------
# Top-level merge
# ---------------------------------------------------------------------------


def merge_activities(
    new_activities_path: Path,
    target_catalog_dir: Path,
    add_old_suffix: bool = False,
):
    """Merge new_activities.json into the target lci_catalog directory.

    Uses flat dicts keyed by UUID for activities and ingredients.
    Normalizes on load (strips previous merge artifacts), then merges
    with new overriding existing, and optionally applies suffixes.

    Options:
    - add_old_suffix: Add " (2025)" suffix to pre-existing ingredient displayNames
      and "-2025" suffix to their aliases
    """
    with open(new_activities_path) as f:
        new_list = json.load(f)
    existing_list = load_lci_catalog(target_catalog_dir)

    keep_csv_path = Path(__file__).parent / "source/keep.csv"
    keep_set = set()
    if keep_csv_path.exists():
        with open(keep_csv_path, encoding="utf-8") as f:
            keep_set = {line.strip() for line in f if line.strip()}

    # Only strip old suffixes when we intend to re-apply them
    strip_display = OLD_DISPLAY_SUFFIX if add_old_suffix else ""
    strip_alias = OLD_ALIAS_SUFFIX if add_old_suffix else ""

    existing_acts, existing_ings, other = extract_activities_and_ingredients(
        existing_list, strip_display, strip_alias
    )
    new_acts, new_ings, _ = extract_activities_and_ingredients(
        new_list, strip_display, strip_alias
    )

    existing_act_by_name: dict[str, tuple[str, str]] = {
        act["activityName"]: act_key
        for act_key, act in existing_acts.items()
        if "activityName" in act
    }

    # For new activities: only add if activityName is genuinely new.
    # For new ingredients hosted on a reused activityName: remap activity_key.
    added_acts: dict[tuple[str, str], dict] = {}
    for new_key, act in new_acts.items():
        act_name = act.get("activityName")
        if act_name not in existing_act_by_name:
            added_acts[new_key] = act
            existing_act_by_name[act_name] = new_key
        else:
            existing_key = existing_act_by_name[act_name]
            for key in ("location", "source"):
                if key in act:
                    existing_acts[existing_key][key] = act[key]

    for ing_dn, ing in new_ings.items():
        ing_act_key = ing["activity_key"]
        source_act = new_acts.get(ing_act_key)
        if source_act:
            act_name = source_act["activityName"]
            existing_key = existing_act_by_name.get(act_name)
            if existing_key and existing_key != ing_act_key:
                ing["activity_key"] = existing_key

    merged_acts = {**existing_acts, **added_acts}
    merged_ings = {**existing_ings}
    pre_alias_renames: dict[str, str] = {}
    for dn, ing in new_ings.items():
        existing = merged_ings.get(dn)
        if existing is not None:
            existing_aa = existing.get("activity_key")
            new_aa = ing.get("activity_key")
            if existing_aa != new_aa:
                old_dn = existing["displayName"] + OLD_DISPLAY_SUFFIX
                renamed = {**existing, "displayName": old_dn}
                old_alias_val = existing.get("alias") or ""
                if old_alias_val and not old_alias_val.endswith(OLD_ALIAS_SUFFIX):
                    renamed["alias"] = old_alias_val + OLD_ALIAS_SUFFIX
                    pre_alias_renames[old_alias_val] = renamed["alias"]
                merged_ings[old_dn] = renamed
                merged_ings[dn] = ing
                continue
            if "id" in existing:
                ing = {**ing, "id": existing["id"]}
        merged_ings[dn] = ing

    # Detect ingredient alias collisions that the displayName-keyed merge
    # cannot see: a legacy ingredient (e.g. `apricot-non-eu` / "Abricot par
    # défaut") sharing its alias with a freshly exported ingredient under a
    # different displayName (e.g. "Abricot HORS UE"). Rename the legacy one
    # with the `-2025` / ` (2025)` markers so the new export keeps the
    # canonical alias.
    alias_collision_renames: dict[str, str] = {}
    new_ing_aliases = {ing["alias"] for ing in new_ings.values() if ing.get("alias")}
    for dn in list(merged_ings.keys()):
        if dn in new_ings:
            continue
        ing = merged_ings[dn]
        al = ing.get("alias")
        if not al or al.endswith(OLD_ALIAS_SUFFIX):
            continue
        if al not in new_ing_aliases:
            continue
        new_alias = al + OLD_ALIAS_SUFFIX
        new_dn = ing["displayName"] + OLD_DISPLAY_SUFFIX
        renamed = {**ing, "alias": new_alias, "displayName": new_dn}
        alias_collision_renames[al] = new_alias
        del merged_ings[dn]
        merged_ings[new_dn] = renamed

    alias_renames: dict[str, str] = dict(pre_alias_renames)
    alias_renames.update(alias_collision_renames)
    if add_old_suffix:
        merged_acts, merged_ings, apply_renames = apply_suffixes(
            merged_acts,
            merged_ings,
            set(new_ings),
            keep_set,
            OLD_DISPLAY_SUFFIX,
            OLD_ALIAS_SUFFIX,
        )
        alias_renames.update(apply_renames)

    # Update feed.json keys to match renamed ingredient aliases
    feed_path = target_catalog_dir.parent / "food/ecosystemic_services/feed.json"
    if feed_path.exists():
        with open(feed_path, encoding="utf-8") as f:
            feed_data = json.load(f)

        if alias_renames:
            updated_feed = {}
            renamed_count = 0
            for key, value in feed_data.items():
                new_key = alias_renames.get(key, key)
                new_value = {alias_renames.get(k, k): v for k, v in value.items()}
                updated_feed[new_key] = new_value
                if new_key != key:
                    renamed_count += 1
            feed_data = updated_feed
            print(f"feed.json: renamed {renamed_count} top-level keys")

        with open(feed_path, "w", encoding="utf-8") as f:
            json.dump(feed_data, f, indent=2, ensure_ascii=False)

    dedupe_suffixed_ids(merged_ings, "ingredient")

    result = reassemble(merged_acts, merged_ings, other)

    write_lci_catalog(result, target_catalog_dir)
    print(f"Merged {len(added_acts)} new activities into {len(merged_acts)} total activities")
