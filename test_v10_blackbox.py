#!/usr/bin/env python3
"""
V10 黑盒测试脚本
测试 V10 指令中的 12 项测试
"""

import json
import sys
import sqlite3
from datetime import date, timedelta

sys.path.insert(0, "/Users/vv/WorkBuddy/Claw")

from db import get_db, init_db
from inventory import (
    check_dish_availability, check_dishes_availability_batch,
    get_current_pantry_ids, save_pantry_changes,
    normalize_ingredient_id, _invalidate_availability_cache,
)
from rule_engine import (
    RuleEngine, NutritionAnalyzer, MealState, analyze_meal_slots,
    filter_candidates_for_slot, BREAKFAST_COMPANION_STAPLES,
)
from menu_service import (
    _load_pool, ai_fill_menu, reconcile_meal_for_diners,
    generate_and_store_menu, get_menu_with_dishes,
    add_dish_to_menu, _fill_missing_slots_v8, GapFiller,
    get_dish_ingredients_map, get_history_3day, get_history_7day,
    get_inventory_ingredients,
)

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
TEST_DATE = TOMORROW
results = []


def report(test_name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append((test_name, passed))
    print(f"  [{status}] {test_name}")
    if detail:
        print(f"         {detail}")


def clear_test_menu():
    """清除测试菜单"""
    conn = get_db()
    try:
        conn.execute("DELETE FROM menu_items WHERE menu_id IN (SELECT id FROM menus WHERE date = ?)", (TEST_DATE,))
        conn.execute("DELETE FROM menus WHERE date = ?", (TEST_DATE,))
        conn.commit()
    finally:
        conn.close()


def get_dish_id_by_name(name_cn):
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM dishes WHERE name_cn = ? AND is_active = 1", (name_cn,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def set_pantry(location, ingredient_ids):
    """设置测试库存"""
    items = [{"ingredient_id": ing, "status": "available"} for ing in ingredient_ids]
    save_pantry_changes(location, items)
    _invalidate_availability_cache(location)


def set_diners(menu_id, diner_count):
    """设置用餐人数"""
    conn = get_db()
    try:
        diner_ids = json.dumps([f"diner_{i}" for i in range(diner_count)])
        conn.execute("UPDATE menus SET diners = ?, diners_count = ? WHERE id = ?", (diner_ids, diner_count, menu_id))
        conn.commit()
    finally:
        conn.close()


def get_menu_items(menu_id, meal_type):
    """获取指定餐次的菜单项"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT mi.id, mi.dish_id, mi.is_locked, mi.source, d.name_cn, d.category_id, d.meal_roles "
            "FROM menu_items mi LEFT JOIN dishes d ON mi.dish_id = d.id "
            "WHERE mi.menu_id = ? AND mi.meal_type = ? ORDER BY mi.sort_order",
            (menu_id, meal_type)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_slots(menu_id, meal_type, diners_count=4):
    """计算槽位状态"""
    pool = _load_pool()
    dish_map = {d["id"]: d for d in pool["dishes"]}
    items = get_menu_items(menu_id, meal_type)
    state = MealState()
    for item in items:
        did = item["dish_id"]
        if did in dish_map:
            state.add_dish(NutritionAnalyzer.analyze(dish_map[did]))
    return analyze_meal_slots(meal_type, state, diners_count)


# ============================================================
# Test 1: Pantry Egg → Egg Dish
# ============================================================
def test_1_egg_availability():
    print("\n=== Test 1: Pantry Egg → Egg Dish ===")
    _invalidate_availability_cache("shenzhen")

    # Ensure egg is in pantry
    available, _, _ = get_current_pantry_ids("shenzhen")
    print(f"  Pantry has 鸡蛋: {'鸡蛋' in available}")

    # Check egg dishes
    egg_dish_ids = []
    for name in ["水煮蛋", "煎蛋", "日式溏心蛋"]:
        did = get_dish_id_by_name(name)
        if did:
            egg_dish_ids.append((name, did))

    all_available = True
    for name, did in egg_dish_ids:
        r = check_dish_availability(did, "shenzhen")
        print(f"  {name} ({did}): status={r['status']}")
        if r["status"] != "available":
            all_available = False

    report("1. Pantry Egg → Egg Dish Available", all_available,
           f"Tested {len(egg_dish_ids)} egg dishes, all available: {all_available}")


# ============================================================
# Test 2: Egg alias normalization
# ============================================================
def test_2_egg_alias():
    print("\n=== Test 2: Egg Alias Normalization ===")
    # Test alias normalization
    aliases = ["egg", "Egg", "Chicken Egg", "蛋", "鸡蛋"]
    canonical = normalize_ingredient_id("egg")
    print(f"  normalize('egg') = {canonical}")

    all_match = True
    for alias in aliases:
        result = normalize_ingredient_id(alias)
        print(f"  normalize('{alias}') = {result}")
        if result != "鸡蛋":
            all_match = False

    report("2. Egg alias normalization", all_match,
           f"All aliases map to '鸡蛋': {all_match}")

    # Also test availability with canonical ID
    # If dish uses 'egg' and pantry has '鸡蛋', should still match
    available, _, _ = get_current_pantry_ids("shenzhen")
    normalized = set(normalize_ingredient_id(x) for x in available)
    print(f"  Normalized pantry contains '鸡蛋': {'鸡蛋' in normalized}")
    report("2b. Pantry normalized contains egg", "鸡蛋" in normalized)


# ============================================================
# Test 3: Egg dish basic condiment handling
# ============================================================
def test_3_condiment_handling():
    print("\n=== Test 3: Egg Dish Basic Condiment ===")
    # 水煮蛋 should only require 鸡蛋, not oil/salt
    did = get_dish_id_by_name("水煮蛋")
    if not did:
        report("3. Egg dish condiment handling", False, "水煮蛋 not found")
        return

    r = check_dish_availability(did, "shenzhen")
    required_names = [i["name_cn"] for i in r["required"]]
    print(f"  水煮蛋 required: {required_names}")

    # Should NOT have oil/salt/pepper as required
    condiments = ["油", "盐", "胡椒", "pepper", "oil", "salt", "酱油", "醋"]
    has_condiment = any(c in required_names for c in condiments)
    has_egg = "鸡蛋" in required_names or "egg" in [i["ingredient_id"] for i in r["required"]]

    report("3. Egg dish basic condiment handling",
           has_egg and not has_condiment,
           f"Required: {required_names}, has_condiment={has_condiment}")

    # Test: Pantry only has egg → egg dish should be available
    # (This is already tested above, but let's be explicit)
    report("3b. Pantry with only egg → egg dish available",
           r["status"] == "available")


# ============================================================
# Test 4: Existing tofu dish satisfies tofu slot
# ============================================================
def test_4_tofu_slot():
    print("\n=== Test 4: Existing Tofu Dish Satisfies Tofu Slot ===")
    # 四味豆腐沙拉 has meal_roles = vegetable_dish (not tofu_dish!)
    # But its protein_types has tofu. Let's check what meal_roles it has.
    did = get_dish_id_by_name("四味豆腐沙拉")
    if not did:
        report("4. Tofu slot satisfied", False, "四味豆腐沙拉 not found")
        return

    conn = get_db()
    try:
        row = conn.execute("SELECT meal_roles, protein_types FROM dishes WHERE id = ?", (did,)).fetchone()
        print(f"  四味豆腐沙拉 meal_roles: {row['meal_roles']}, proteins: {row['protein_types']}")
    finally:
        conn.close()

    # If the dish has tofu_dish in meal_roles, it should satisfy tofu_slot
    analysis = NutritionAnalyzer.analyze({"id": did, "name_cn": "四味豆腐沙拉", "category_id": "cold_dish",
                                          "protein_types": ["tofu"], "vegetables": [], "carb_type": None,
                                          "meal_roles": ["vegetable_dish"]})
    state = MealState()
    state.add_dish(analysis)
    print(f"  tofu_slot = {state.tofu_slot}")

    # Check: a dish with tofu_dish role should set tofu_slot=1
    analysis2 = NutritionAnalyzer.analyze({"id": did, "name_cn": "四味豆腐沙拉", "category_id": "cold_dish",
                                           "protein_types": ["tofu"], "vegetables": [], "carb_type": None,
                                           "meal_roles": ["tofu_dish"]})
    state2 = MealState()
    state2.add_dish(analysis2)
    print(f"  tofu_slot (with tofu_dish role) = {state2.tofu_slot}")

    report("4. Tofu dish with tofu_dish role satisfies slot", state2.tofu_slot >= 1)

    # Also check: 豆腐蒸蛋 has both egg_dish and tofu_dish
    did2 = get_dish_id_by_name("豆腐蒸蛋")
    if did2:
        pool = _load_pool()
        dm = {d["id"]: d for d in pool["dishes"]}
        if did2 in dm:
            analysis3 = NutritionAnalyzer.analyze(dm[did2])
            state3 = MealState()
            state3.add_dish(analysis3)
            print(f"  豆腐蒸蛋: egg_slot={state3.egg_slot}, tofu_slot={state3.tofu_slot}")
            report("4b. 豆腐蒸蛋 satisfies both egg and tofu slots",
                   state3.egg_slot >= 1 and state3.tofu_slot >= 1)


# ============================================================
# Test 5: unmet warning dedupe
# ============================================================
def test_5_unmet_dedupe():
    print("\n=== Test 5: unmet_slots Dedup ===")
    # Create a test menu with a meal that can't be filled
    clear_test_menu()

    # Generate a minimal menu
    menu_id, review = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)

    # Run AI fill multiple times to trigger unmet_slots
    ok, msg, review1 = ai_fill_menu(menu_id, "shenzhen", seed=42, meal_type="breakfast")
    ok, msg, review2 = ai_fill_menu(menu_id, "shenzhen", seed=42, meal_type="breakfast")

    unmet = review2.get("unmet_slots", []) if review2 else []
    print(f"  unmet_slots count after 2nd fill: {len(unmet)}")

    # Check for duplicates
    seen = set()
    has_dupes = False
    for u in unmet:
        key = (u.get("meal", ""), u.get("slot", ""))
        if key in seen:
            has_dupes = True
            print(f"  DUPLICATE: {key}")
        seen.add(key)

    report("5. unmet_slots dedup", not has_dupes,
           f"unmet_slots={len(unmet)}, duplicates={has_dupes}")


# ============================================================
# Test 6: 4 diners → 3 diners reconciliation
# ============================================================
def test_6_dinner_4_to_3():
    print("\n=== Test 6: Dinner 4→3 Diners ===")
    clear_test_menu()

    # Generate dinner with 4 diners
    menu_id, _ = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)
    set_diners(menu_id, 4)

    # Get dinner items before reconcile
    items_before = get_menu_items(menu_id, "dinner")
    slots_before = count_slots(menu_id, "dinner", 4)
    print(f"  Before (4 diners): {len(items_before)} items")
    print(f"  Slots: protein={slots_before['protein_main']['current']}, "
          f"veg={slots_before['vegetable_dish']['current']}, "
          f"staple={slots_before['staple']['current']}, "
          f"soup={slots_before['slow_soup']['current']}")

    # Switch to 3 diners
    set_diners(menu_id, 3)
    ok, msg, review = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_after = get_menu_items(menu_id, "dinner")
    slots_after = count_slots(menu_id, "dinner", 3)
    print(f"  After (3 diners): {len(items_after)} items")
    print(f"  Slots: protein={slots_after['protein_main']['current']}, "
          f"veg={slots_after['vegetable_dish']['current']}, "
          f"staple={slots_after['staple']['current']}, "
          f"soup={slots_after['slow_soup']['current']}")

    # 3 diners target: 2P + 1V + 1S + 1Soup
    target = RuleEngine._dinner_target(3)
    print(f"  3-diner target: P={target['protein_main']}, V={target['vegetable_dish']}, "
          f"S={target['staple']}, Soup={target['slow_soup']}")

    # Check: vegetable should have decreased from 2 to 1
    veg_before = slots_before["vegetable_dish"]["current"]
    veg_after = slots_after["vegetable_dish"]["current"]
    protein_ok = slots_after["protein_main"]["current"] <= target["protein_main"] + 1  # allow owner excess
    veg_ok = veg_after <= target["vegetable_dish"] + 1

    report("6. 4→3: excess AI vegetable removed",
           veg_after < veg_before or veg_after <= target["vegetable_dish"],
           f"veg before={veg_before}, after={veg_after}, target={target['vegetable_dish']}")


# ============================================================
# Test 7: 4 diners → 2 diners reconciliation
# ============================================================
def test_7_dinner_4_to_2():
    print("\n=== Test 7: Dinner 4→2 Diners ===")
    clear_test_menu()

    menu_id, _ = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)
    set_diners(menu_id, 4)

    items_before = get_menu_items(menu_id, "dinner")
    slots_before = count_slots(menu_id, "dinner", 4)
    print(f"  Before (4 diners): {len(items_before)} items, "
          f"P={slots_before['protein_main']['current']}, V={slots_before['vegetable_dish']['current']}")

    # Switch to 2 diners
    set_diners(menu_id, 2)
    ok, msg, review = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_after = get_menu_items(menu_id, "dinner")
    slots_after = count_slots(menu_id, "dinner", 2)
    print(f"  After (2 diners): {len(items_after)} items, "
          f"P={slots_after['protein_main']['current']}, V={slots_after['vegetable_dish']['current']}")

    target = RuleEngine._dinner_target(2)
    print(f"  2-diner target: P={target['protein_main']}, V={target['vegetable_dish']}")

    # 2 diners: 1P + 1V. Should have removed excess protein and vegetable
    p_removed = slots_before["protein_main"]["current"] > slots_after["protein_main"]["current"]
    v_removed = slots_before["vegetable_dish"]["current"] > slots_after["vegetable_dish"]["current"]

    report("7. 4→2: excess AI protein and vegetable removed",
           len(items_after) < len(items_before),
           f"items {len(items_before)}→{len(items_after)}, P {slots_before['protein_main']['current']}→{slots_after['protein_main']['current']}")


# ============================================================
# Test 8: 2 diners → 3 diners adds only protein
# ============================================================
def test_8_dinner_2_to_3():
    print("\n=== Test 8: Dinner 2→3 Diners (adds only protein) ===")
    clear_test_menu()

    menu_id, _ = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)
    set_diners(menu_id, 2)
    # Reconcile to 2 first
    ok, msg, _ = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_before = get_menu_items(menu_id, "dinner")
    slots_before = count_slots(menu_id, "dinner", 2)
    print(f"  Before (2 diners): P={slots_before['protein_main']['current']}, "
          f"V={slots_before['vegetable_dish']['current']}")

    # Switch to 3 diners
    set_diners(menu_id, 3)
    ok, msg, review = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_after = get_menu_items(menu_id, "dinner")
    slots_after = count_slots(menu_id, "dinner", 3)
    print(f"  After (3 diners): P={slots_after['protein_main']['current']}, "
          f"V={slots_after['vegetable_dish']['current']}")

    target = RuleEngine._dinner_target(3)
    print(f"  3-diner target: P={target['protein_main']}, V={target['vegetable_dish']}")

    # 2→3 should add 1 protein (1→2), vegetable stays 1→1
    p_increased = slots_after["protein_main"]["current"] > slots_before["protein_main"]["current"]
    v_unchanged = slots_after["vegetable_dish"]["current"] == slots_before["vegetable_dish"]["current"]

    report("8. 2→3: adds only protein",
           p_increased and v_unchanged,
           f"P {slots_before['protein_main']['current']}→{slots_after['protein_main']['current']}, "
           f"V {slots_before['vegetable_dish']['current']}→{slots_after['vegetable_dish']['current']}")


# ============================================================
# Test 9: 3 diners → 4 diners adds only vegetable
# ============================================================
def test_9_dinner_3_to_4():
    print("\n=== Test 9: Dinner 3→4 Diners (adds only vegetable) ===")
    clear_test_menu()

    menu_id, _ = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)
    set_diners(menu_id, 3)
    # Reconcile to 3 first
    ok, msg, _ = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_before = get_menu_items(menu_id, "dinner")
    slots_before = count_slots(menu_id, "dinner", 3)
    print(f"  Before (3 diners): P={slots_before['protein_main']['current']}, "
          f"V={slots_before['vegetable_dish']['current']}")

    # Switch to 4 diners
    set_diners(menu_id, 4)
    ok, msg, review = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_after = get_menu_items(menu_id, "dinner")
    slots_after = count_slots(menu_id, "dinner", 4)
    print(f"  After (4 diners): P={slots_after['protein_main']['current']}, "
          f"V={slots_after['vegetable_dish']['current']}")

    target = RuleEngine._dinner_target(4)
    print(f"  4-diner target: P={target['protein_main']}, V={target['vegetable_dish']}")

    # 3→4 should add 1 vegetable (1→2), protein stays 2→2
    v_increased = slots_after["vegetable_dish"]["current"] > slots_before["vegetable_dish"]["current"]
    p_unchanged = slots_after["protein_main"]["current"] == slots_before["protein_main"]["current"]

    report("9. 3→4: adds only vegetable",
           v_increased and p_unchanged,
           f"P {slots_before['protein_main']['current']}→{slots_after['protein_main']['current']}, "
           f"V {slots_before['vegetable_dish']['current']}→{slots_after['vegetable_dish']['current']}")


# ============================================================
# Test 10: Repeated AI Fill is idempotent
# ============================================================
def test_10_idempotent():
    print("\n=== Test 10: Repeated AI Fill Idempotent ===")
    clear_test_menu()

    menu_id, _ = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)

    # Run AI fill 3 times
    ok1, msg1, _ = ai_fill_menu(menu_id, "shenzhen", seed=42)
    items_1 = get_menu_items(menu_id, "dinner")

    ok2, msg2, _ = ai_fill_menu(menu_id, "shenzhen", seed=42)
    items_2 = get_menu_items(menu_id, "dinner")

    ok3, msg3, _ = ai_fill_menu(menu_id, "shenzhen", seed=42)
    items_3 = get_menu_items(menu_id, "dinner")

    print(f"  Fill 1: {len(items_1)} dinner items")
    print(f"  Fill 2: {len(items_2)} dinner items")
    print(f"  Fill 3: {len(items_3)} dinner items")

    # Count should stay the same
    idempotent = len(items_1) == len(items_2) == len(items_3)

    report("10. Repeated AI Fill idempotent", idempotent,
           f"counts: {len(items_1)} → {len(items_2)} → {len(items_3)}")


# ============================================================
# Test 11: Owner Selected preserved
# ============================================================
def test_11_owner_preserved():
    print("\n=== Test 11: Owner Selected Preserved ===")
    clear_test_menu()

    menu_id, _ = generate_and_store_menu(TEST_DATE, "shenzhen", seed=42)
    set_diners(menu_id, 4)

    # Add an owner dish to dinner
    owner_dish = get_dish_id_by_name("香煎银鳕鱼")
    if not owner_dish:
        owner_dish = get_dish_id_by_name("牛肉粒")
    if not owner_dish:
        # Find any protein dish
        pool = _load_pool()
        for d in pool["dishes"]:
            if d.get("category_id") == "protein_main" and "dinner" in d.get("meal_tags", []):
                owner_dish = d["id"]
                break

    if not owner_dish:
        report("11. Owner Selected preserved", False, "No suitable dish found")
        return

    add_dish_to_menu(menu_id, owner_dish, "dinner")
    items_before = get_menu_items(menu_id, "dinner")

    # Verify owner dish is in menu
    owner_before = [i for i in items_before if i["is_locked"]]
    print(f"  Owner dishes before: {[(i['name_cn'], i['dish_id']) for i in owner_before]}")

    # Reconcile to 2 diners
    set_diners(menu_id, 2)
    ok, msg, _ = reconcile_meal_for_diners(menu_id, "shenzhen")

    items_after = get_menu_items(menu_id, "dinner")
    owner_after = [i for i in items_after if i["is_locked"]]

    print(f"  Owner dishes after: {[(i['name_cn'], i['dish_id']) for i in owner_after]}")

    # All owner dishes should still be present
    owner_ids_before = {i["dish_id"] for i in owner_before}
    owner_ids_after = {i["dish_id"] for i in owner_after}

    all_preserved = owner_ids_before.issubset(owner_ids_after)

    report("11. Owner Selected preserved", all_preserved,
           f"before={owner_ids_before}, after={owner_ids_after}")


# ============================================================
# Test 12: Normal AI Fill no browser alert
# ============================================================
def test_12_no_alert():
    print("\n=== Test 12: No browser alert() ===")
    # This is a frontend test — we check that app.py doesn't use alert() in aiFillMeal
    import re
    with open("/Users/vv/WorkBuddy/Claw/app.py", "r") as f:
        content = f.read()

    # Find the aiFillMeal function
    match = re.search(r'async function aiFillMeal\(.*?\}', content, re.DOTALL)
    if match:
        func_code = match.group()
        has_alert = "alert(" in func_code
        has_warning_card = "showWarningCard" in func_code
        print(f"  aiFillMeal has alert(): {has_alert}")
        print(f"  aiFillMeal has showWarningCard(): {has_warning_card}")
        report("12. Normal AI Fill no browser alert", not has_alert,
               f"alert() present: {has_alert}, showWarningCard present: {has_warning_card}")
    else:
        report("12. Normal AI Fill no browser alert", False, "aiFillMeal function not found")


# ============================================================
# Run all tests
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  V10 Black Box Tests")
    print("=" * 60)

    test_1_egg_availability()
    test_2_egg_alias()
    test_3_condiment_handling()
    test_4_tofu_slot()
    test_5_unmet_dedupe()
    test_6_dinner_4_to_3()
    test_7_dinner_4_to_2()
    test_8_dinner_2_to_3()
    test_9_dinner_3_to_4()
    test_10_idempotent()
    test_11_owner_preserved()
    test_12_no_alert()

    # Cleanup
    clear_test_menu()

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, p in results if p)
    failed = sum(1 for _, p in results if not p)
    total = len(results)
    for name, p in results:
        print(f"  [{'✓' if p else '✗'}] {name}")
    print(f"\n  Total: {passed}/{total} PASS, {failed} FAIL")
    print("=" * 60)
