#!/usr/bin/env python3
"""Final-menu auditor for the frozen family-menu generation rules.

The auditor reads final menu rows plus generation evidence. It never generates,
repairs, confirms, pushes, or mutates a menu.
"""

from collections import Counter
from datetime import date

from inventory import PANTRY_EXEMPT_CANONICAL_IDS, normalize_ingredient_id
from rule_engine import (
    AUTO_POOL_MINIMUMS,
    MEAT_PROTEINS,
    LEAFY_VEGETABLE_CANONICAL_IDS,
    counted_primary_protein_source,
    is_leafy_vegetable,
    primary_vegetable_canonical_id,
    primary_vegetable_subject,
    assign_breakfast_slots,
    has_egg_ingredient,
    has_tofu_ingredient,
    is_pantry_exempt_dish,
)


AUDIT_RULES = (
    {
        "id": "A01",
        "clause": "§2（2026-08-19 19:43 锁死版）",
        "rule": "早餐八槽各自独立：粥、主食伴侣、蛋、早餐标签凉拌豆腐、蔬菜×2、独立肉类蛋白菜、粗粮；任何菜不得一顶二。",
    },
    {
        "id": "A02",
        "clause": "§4 / §11.1 / §11.3 / §13.3",
        "rule": "同餐 egg_dish 不超过 1，全天自动 egg_dish 不超过 2。",
    },
    {
        "id": "A03",
        "clause": "§3 / §11.4 / §11.5 / §11.6 / §13.3",
        "rule": "午晚餐按 1–2、3、4+ 人矩阵精确满足蛋白、蔬菜、主食和对应汤槽。",
    },
    {
        "id": "A04",
        "clause": "§3 / §5 / §11.7 / §13.3",
        "rule": "午晚每餐至少 1 道非 tofu_dish 的肉/鱼/虾/牛/猪/鸡主菜。",
    },
    {
        "id": "A05",
        "clause": "§6 / §13.3",
        "rule": "同餐 dish_id 不得重复。",
    },
    {
        "id": "A06",
        "clause": "§6 / §11.9 / §13.3",
        "rule": "汤内肉、菜、蛋不占 protein_main / vegetable_dish / egg_dish 主菜槽。",
    },
    {
        "id": "A07",
        "clause": "§7 / §11.10 / §13.3",
        "rule": "同厨房 4 天窗口默认不重复 dish_id；常备豁免菜及合法低池降级事件可破锁。",
    },
    {
        "id": "A08",
        "clause": "§8 / §11.12 / §13.3",
        "rule": "低池降级必有指定提示；健康池过滤空必硬警告且不得破锁。",
    },
    {
        "id": "A09",
        "clause": "§11.15 / §13.3",
        "rule": "深圳/香港轮换历史与库存上下文按厨房隔离。",
    },
    {
        "id": "A10",
        "clause": "§4 REQUIREMENTS_V2 / UI SoT DEFAULT_PANTRY",
        "rule": "家庭常备 20 项加小米按 canonical id 视为可用，不得进入缺食材结果；非豁免缺口保持精确。",
    },
    {
        "id": "A11",
        "clause": "§14",
        "rule": "全天同一主蔬菜不超过 1 道；仅午晚 protein_main 主蛋白来源全天不重复，早餐不占来源，蛋和豆腐豁免。",
    },
    {
        "id": "A12",
        "clause": "§15",
        "rule": "每餐绿叶菜不超过 1 道；第二蔬菜位必须非绿叶，清单外默认非绿叶。",
    },
)

_RULE_BY_ID = {item["id"]: item for item in AUDIT_RULES}
_MEALS = ("breakfast", "lunch", "dinner")
_LOW_POOL_TEXT = "该分类菜品不足，建议补录"


def _roles(item):
    return set(item.get("meal_roles") or [])


def _proteins(item):
    return set(item.get("protein_types") or item.get("proteins") or [])


def _is_soup(item):
    roles = _roles(item)
    return item.get("category_id") == "soup" or bool(
        roles & {"quick_soup", "slow_soup"}
    )


def _is_auto(item):
    return (item.get("source") or "ai").lower() not in {"manual", "owner"}


def _target(diners_count):
    if diners_count <= 2:
        protein, vegetable = 1, 1
    elif diners_count == 3:
        protein, vegetable = 2, 1
    else:
        protein, vegetable = 2, 2
    return {"protein_main": protein, "vegetable_dish": vegetable}


def _check(rule_id, status, details):
    rule = _RULE_BY_ID[rule_id]
    return {
        "id": rule_id,
        "clause": rule["clause"],
        "rule": rule["rule"],
        "status": status,
        "passed": status == "PASS",
        "details": details,
    }


def _hard_warning(evidence, meal, slot):
    prefix = f"{meal}.{slot} "
    return next(
        (message for message in evidence.get("hard_warnings", [])
         if message.startswith(prefix)),
        None,
    )


def _degradation_warning(evidence, meal, slot):
    prefix = f"{meal}.{slot} "
    return next(
        (message for message in evidence.get("degradation_warnings", [])
         if message.startswith(prefix)),
        None,
    )


def _breakfast_assignment(items):
    """Assign exactly one distinct dish to every locked §2 breakfast slot."""
    return assign_breakfast_slots(items)


def _slot_status(evidence, missing_slots):
    if missing_slots and all(
        _hard_warning(evidence, meal, slot)
        for meal, slot in missing_slots
    ):
        return "HARD_SHORTAGE"
    return "FAIL" if missing_slots else "PASS"


def _audit_breakfast(menu, evidence):
    items = menu.get("meals", {}).get("breakfast", [])
    assigned, candidates, assignment = _breakfast_assignment(items)
    targets = {
        "porridge": 1, "companion_staple": 1, "tofu": 1,
        "egg": 1, "vegetable": 2, "breakfast_meat": 1,
        "coarse_grain": 1,
    }
    current = {
        "porridge": len(candidates["porridge"]),
        "companion_staple": len(candidates["companion_staple"]),
        "tofu": len(candidates["tofu"]),
        "egg": len(candidates["egg"]),
        "vegetable": len({*candidates["vegetable_1"], *candidates["vegetable_2"]}),
        "breakfast_meat": len(candidates["breakfast_meat"]),
        "coarse_grain": len(candidates["coarse_grain"]),
    }
    missing = [
        ("breakfast", slot) for slot, target in targets.items()
        if current[slot] < target
    ]
    status = _slot_status(evidence, missing)
    dual_role_ingredient_offenders = []
    for item in items:
        if {"egg_dish", "tofu_dish"} <= _roles(item) and not (
            has_egg_ingredient(item) and has_tofu_ingredient(item)
        ):
            dual_role_ingredient_offenders.append(
                item.get("dish_id") or item.get("id")
            )
    if dual_role_ingredient_offenders:
        status = "FAIL"
    if not assigned and not missing:
        status = "FAIL"
    unused_automatic = [
        item.get("dish_id") or item.get("id")
        for index, item in enumerate(items)
        if _is_auto(item) and index not in set(assignment.values())
    ]
    if assigned and unused_automatic:
        status = "FAIL"
    details = {
        "current": current,
        "target": targets,
        "assignment_valid": assigned,
        "dish_count_informational": len(items),
        "missing_slots": [slot for _, slot in missing],
        "unused_automatic_dishes": unused_automatic,
        "dual_role_ingredient_offenders": dual_role_ingredient_offenders,
        "distinct_dish_per_slot": True,
    }
    return _check("A01", status, details)


def _audit_eggs(menu):
    meal_counts = {
        meal: sum("egg_dish" in _roles(item)
                  for item in menu.get("meals", {}).get(meal, []))
        for meal in _MEALS
    }
    automatic_day = sum(
        "egg_dish" in _roles(item) and _is_auto(item)
        for meal in _MEALS
        for item in menu.get("meals", {}).get(meal, [])
    )
    failures = [meal for meal, count in meal_counts.items() if count > 1]
    if automatic_day > 2:
        failures.append("automatic_day")
    return _check(
        "A02", "FAIL" if failures else "PASS",
        {"meal_counts": meal_counts, "automatic_day": automatic_day,
         "violations": failures},
    )


def _audit_matrix(menu, evidence):
    diners = int(menu.get("diners_count") or 4)
    target = _target(diners)
    failures = []
    shortages = []
    summary = {}
    for meal in ("lunch", "dinner"):
        items = menu.get("meals", {}).get(meal, [])
        soup_slot = "quick_soup" if meal == "lunch" else "slow_soup"
        current = {
            "protein_main": sum(
                "protein_main" in _roles(item) and not _is_soup(item)
                for item in items
            ),
            "vegetable_dish": sum(
                "vegetable_dish" in _roles(item) and not _is_soup(item)
                for item in items
            ),
            "staple": sum("staple" in _roles(item) for item in items),
            soup_slot: sum(soup_slot in _roles(item) for item in items),
        }
        expected = {
            **target, "staple": 1, soup_slot: 1,
        }
        summary[meal] = {"current": current, "target": expected}
        for slot, wanted in expected.items():
            actual = current[slot]
            if actual == wanted:
                continue
            if actual < wanted and _hard_warning(evidence, meal, slot):
                shortages.append(f"{meal}.{slot} {actual}/{wanted}")
            else:
                failures.append(f"{meal}.{slot} {actual}/{wanted}")
    status = "FAIL" if failures else ("HARD_SHORTAGE" if shortages else "PASS")
    summary["failures"] = failures
    summary["hard_shortages"] = shortages
    return _check("A03", status, summary)


def _audit_meat(menu, evidence):
    failures = []
    shortages = []
    counts = {}
    for meal in ("lunch", "dinner"):
        count = sum(
            "protein_main" in _roles(item)
            and "tofu_dish" not in _roles(item)
            and not _is_soup(item)
            and bool(_proteins(item) & MEAT_PROTEINS)
            for item in menu.get("meals", {}).get(meal, [])
        )
        counts[meal] = count
        if count < 1:
            if _hard_warning(evidence, meal, "meat_main"):
                shortages.append(meal)
            else:
                failures.append(meal)
    status = "FAIL" if failures else ("HARD_SHORTAGE" if shortages else "PASS")
    return _check(
        "A04", status,
        {"non_tofu_meat_counts": counts, "failures": failures,
         "hard_shortages": shortages},
    )


def _audit_duplicates(menu):
    duplicates = {}
    for meal in _MEALS:
        counts = Counter(
            item.get("dish_id") or item.get("id")
            for item in menu.get("meals", {}).get(meal, [])
        )
        duplicates[meal] = sorted(
            dish_id for dish_id, count in counts.items()
            if dish_id and count > 1
        )
    failed = any(duplicates.values())
    return _check("A05", "FAIL" if failed else "PASS", duplicates)


def _audit_soup_roles(menu):
    offenders = []
    forbidden = {"protein_main", "vegetable_dish", "egg_dish"}
    for meal in _MEALS:
        for item in menu.get("meals", {}).get(meal, []):
            overlap = _roles(item) & forbidden
            if _is_soup(item) and overlap:
                offenders.append({
                    "meal": meal,
                    "dish_id": item.get("dish_id") or item.get("id"),
                    "forbidden_roles": sorted(overlap),
                })
    return _check("A06", "FAIL" if offenders else "PASS", offenders)


def _valid_degradation_event(event, evidence):
    slot = event.get("slot")
    minimum = AUTO_POOL_MINIMUMS.get(slot)
    pool_size = event.get("pool_size")
    warning = event.get("warning") or ""
    listed = warning in evidence.get("degradation_warnings", [])
    return (
        minimum is not None
        and isinstance(pool_size, int)
        and pool_size < minimum
        and _LOW_POOL_TEXT in warning
        and listed
    )


def _audit_rotation(menu, evidence, previous_records):
    location = menu.get("location")
    current_date = date.fromisoformat(menu["date"])
    prior_ids = set()
    for record in previous_records:
        prior = record["menu"]
        if prior.get("location") != location:
            continue
        delta = (current_date - date.fromisoformat(prior["date"])).days
        if not 1 <= delta <= 3:
            continue
        for meal in _MEALS:
            prior_ids.update(
                item.get("dish_id") or item.get("id")
                for item in prior.get("meals", {}).get(meal, [])
            )

    events = evidence.get("degradation_events", [])
    repeats = []
    seen_today = set()
    for meal in _MEALS:
        for item in menu.get("meals", {}).get(meal, []):
            dish_id = item.get("dish_id") or item.get("id")
            repeated = dish_id in prior_ids or dish_id in seen_today
            if dish_id:
                seen_today.add(dish_id)
            if not repeated:
                continue
            event = next(
                (value for value in events
                 if value.get("meal") == meal and value.get("dish_id") == dish_id),
                None,
            )
            pantry_exempt = is_pantry_exempt_dish(item)
            repeats.append({
                "meal": meal,
                "dish_id": dish_id,
                "legal_pantry_exemption": pantry_exempt,
                "legal_low_pool_degradation": bool(
                    event and _valid_degradation_event(event, evidence)
                ),
            })
    illegal = [
        item for item in repeats
        if not item["legal_pantry_exemption"]
        and not item["legal_low_pool_degradation"]
    ]
    return _check(
        "A07", "FAIL" if illegal else "PASS",
        {"repeats": repeats, "illegal_repeats": illegal},
    )


def _audit_degradation(evidence):
    failures = []
    events = evidence.get("degradation_events", [])
    for event in events:
        if not _valid_degradation_event(event, evidence):
            failures.append({"invalid_event": event})

    pool_sizes = evidence.get("slot_pool_sizes", {})
    for key, pool_size in pool_sizes.items():
        if "." not in key:
            continue
        meal, slot = key.split(".", 1)
        minimum = AUTO_POOL_MINIMUMS.get(slot)
        hard = _hard_warning(evidence, meal, slot)
        degradation = _degradation_warning(evidence, meal, slot)
        if not hard or minimum is None:
            continue
        if pool_size < minimum and not degradation:
            failures.append({"low_pool_hard_shortage_without_prompt": key})
        if pool_size >= minimum and degradation:
            failures.append({"healthy_pool_entered_degradation": key})

    return _check(
        "A08", "FAIL" if failures else "PASS",
        {"events": events, "failures": failures,
         "hard_warnings": evidence.get("hard_warnings", []),
         "degradation_warnings": evidence.get("degradation_warnings", [])},
    )


def _audit_kitchen_scope(menu, evidence):
    location = menu.get("location")
    scopes = {
        "menu": location,
        "rotation": evidence.get("rotation_context_location"),
        "inventory": evidence.get("inventory_context_location"),
    }
    failed = any(value != location for value in scopes.values())
    return _check("A09", "FAIL" if failed else "PASS", scopes)


def _audit_pantry_exemption(menu):
    offenders = []
    checked_missing = 0
    for dish_id, availability in (menu.get("availability") or {}).items():
        for ingredient in availability.get("missing_required", []):
            checked_missing += 1
            raw_id = ingredient.get("ingredient_id")
            canonical_id = normalize_ingredient_id(raw_id)
            if canonical_id in PANTRY_EXEMPT_CANONICAL_IDS:
                offenders.append({
                    "dish_id": dish_id,
                    "ingredient_id": raw_id,
                    "canonical_id": canonical_id,
                    "name_cn": ingredient.get("name_cn"),
                })
    return _check(
        "A10",
        "FAIL" if offenders else "PASS",
        {
            "canonical_ids": sorted(PANTRY_EXEMPT_CANONICAL_IDS),
            "checked_missing_ingredients": checked_missing,
            "exempt_items_reported_missing": offenders,
        },
    )


def _audit_all_day_primary_subjects(menu):
    vegetables = {}
    proteins = {}
    missing_primary_vegetable_data = []
    for meal in _MEALS:
        for item in (menu.get("meals") or {}).get(meal, []):
            occurrence = {
                "meal": meal,
                "dish_id": item.get("dish_id") or item.get("id"),
                "name_cn": item.get("name_cn"),
            }
            vegetable = primary_vegetable_subject(item)
            protein = counted_primary_protein_source(item, meal)
            raw_vegetables = item.get("vegetables") or []
            has_placeholder = any(
                value in {"any_available_vegetable", "任意可用蔬菜"}
                for value in raw_vegetables
            )
            if ("vegetable_dish" in _roles(item) and not vegetable
                    and not has_placeholder):
                missing_primary_vegetable_data.append(occurrence)
            if vegetable:
                vegetables.setdefault(vegetable, []).append(occurrence)
            if protein:
                proteins.setdefault(protein, []).append(occurrence)
    duplicate_vegetables = {
        key: value for key, value in vegetables.items() if len(value) > 1
    }
    duplicate_proteins = {
        key: value for key, value in proteins.items() if len(value) > 1
    }
    failed = bool(
        duplicate_vegetables or duplicate_proteins
        or missing_primary_vegetable_data
    )
    return _check(
        "A11", "FAIL" if failed else "PASS",
        {
            "primary_vegetable_occurrences": vegetables,
            "primary_protein_occurrences": proteins,
            "duplicate_primary_vegetables": duplicate_vegetables,
            "duplicate_primary_proteins": duplicate_proteins,
            "missing_primary_vegetable_data": missing_primary_vegetable_data,
            "protein_exemptions": ["egg", "tofu"],
            "breakfast_protein_sources_counted": False,
        },
    )


def _audit_leafy_vegetables(menu):
    per_meal = {}
    failures = {}
    for meal in _MEALS:
        occurrences = []
        for item in (menu.get("meals") or {}).get(meal, []):
            if not is_leafy_vegetable(item):
                continue
            occurrences.append({
                "dish_id": item.get("dish_id") or item.get("id"),
                "name_cn": item.get("name_cn"),
                "canonical_vegetable": primary_vegetable_canonical_id(item),
            })
        per_meal[meal] = occurrences
        if len(occurrences) > 1:
            failures[meal] = occurrences
    return _check(
        "A12", "FAIL" if failures else "PASS",
        {
            "leafy_canonical_ids": sorted(LEAFY_VEGETABLE_CANONICAL_IDS),
            "leafy_occurrences_by_meal": per_meal,
            "meals_over_limit": failures,
            "unknown_default": "non_leafy",
        },
    )


def audit_final_menu(menu, evidence=None, previous_records=None):
    """Audit one final menu. No generation helper state is accepted or trusted."""
    evidence = evidence or {}
    previous_records = previous_records or []
    checks = [
        _audit_breakfast(menu, evidence),
        _audit_eggs(menu),
        _audit_matrix(menu, evidence),
        _audit_meat(menu, evidence),
        _audit_duplicates(menu),
        _audit_soup_roles(menu),
        _audit_rotation(menu, evidence, previous_records),
        _audit_degradation(evidence),
        _audit_kitchen_scope(menu, evidence),
        _audit_pantry_exemption(menu),
        _audit_all_day_primary_subjects(menu),
        _audit_leafy_vegetables(menu),
    ]
    violations = [item for item in checks if item["status"] == "FAIL"]
    shortages = [item for item in checks if item["status"] == "HARD_SHORTAGE"]
    if violations:
        status = "VIOLATION"
    elif shortages:
        status = "HARD_SHORTAGE"
    else:
        status = "PASS"
    return {
        "date": menu.get("date"),
        "location": menu.get("location"),
        "diners_count": menu.get("diners_count"),
        "menu_status": menu.get("status"),
        "status": status,
        "passed": status == "PASS",
        "rule_compliant": not violations,
        "menu_complete": not shortages and not violations,
        "checks": checks,
        "violation_ids": [item["id"] for item in violations],
        "hard_shortage_ids": [item["id"] for item in shortages],
        "degradation_warnings": list(evidence.get("degradation_warnings", [])),
        "degradation_events": list(evidence.get("degradation_events", [])),
        "hard_warnings": list(evidence.get("hard_warnings", [])),
    }


def audit_menu_sequence(records):
    """Audit dated final menus; four-day history is scoped by kitchen."""
    ordered = sorted(
        records,
        key=lambda record: (record["menu"]["date"], record["menu"]["location"]),
    )
    audited_records = []
    days = []
    for record in ordered:
        days.append(audit_final_menu(
            record["menu"], record.get("evidence"), audited_records
        ))
        audited_records.append(record)
    counts = Counter(item["status"] for item in days)
    return {
        "rules": list(AUDIT_RULES),
        "days": days,
        "status_counts": dict(counts),
        "passed": bool(days) and all(item["passed"] for item in days),
        "rule_compliant": bool(days) and all(
            item["rule_compliant"] for item in days
        ),
    }
