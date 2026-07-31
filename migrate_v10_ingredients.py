#!/usr/bin/env python3
"""
V10 Migration: Populate dish_ingredients for 97 dishes without entries.

Root cause of "No egg available": egg dishes have NO dish_ingredients records,
so check_dish_availability() returns "incomplete" (empty required list).
AI Fill can't find any "available" egg dishes.

This script populates dish_ingredients based on protein_types, vegetables,
carb_type, and dish name.
"""

import json
import sqlite3

def migrate():
    conn = sqlite3.connect("family_menu.db")
    conn.row_factory = sqlite3.Row

    # Get all ingredient_ids for validation
    valid_ingredients = set()
    for r in conn.execute("SELECT ingredient_id FROM ingredients").fetchall():
        valid_ingredients.add(r["ingredient_id"])

    # Vegetable name -> ingredient_id mapping
    VEG_MAP = {
        "口蘑": "button_mushroom",
        "豆芽": "bean_sprout",
        "四季豆": "green_bean",
        "青瓜": "cucumber",
        "紫苏": "perilla",
        "茴香": "茴香",
        "蘑菇": "mushroom_generic",
        "莲藕": "莲藕",
        "娃娃菜": "white_cabbage",
        "青椒": "green_pepper",
        "蟹味菇": "buna_mushroom",
        "牛油果": "牛油果",
        "西兰花": "西兰花",
        "番茄": "番茄",
        "西红柿": "番茄",
        "小番茄": "番茄",
        "白萝卜": "白萝卜",
        "胡萝卜": "carrot",
        "菠菜": "spinach",
        "芹菜": "芹菜",
        "小白菜": "小白菜",
        "白菜": "white_cabbage",
        "淮山": "淮山",
        "山药": "yam",
        "南瓜": "南瓜",
        "生菜": "mixed_greens",
        "杂蔬": "mixed_veg",
        "青菜": "green_veg",
        "豆皮": "tofu_skin",
        "西葫芦": "西葫芦",
        "云南小瓜": "云南小瓜",
        "茄子": "茄子",
        "红椒": "红椒",
        "秋葵": "秋葵",
        "空心菜": "空心菜",
        "苋菜": "苋菜",
        "红苋菜": "红苋菜",
        "枸杞叶": "枸杞叶",
        "枸杞芽": "枸杞芽",
        "百合": "百合",
        "芦笋": "芦笋",
        "时蔬": "时蔬",
        "蔬菜": "蔬菜",
        "彩椒": "彩椒",
        "丝瓜": "丝瓜",
        "芥蓝": "芥蓝",
        "莴笋": "莴笋",
        "冬瓜": "冬瓜",
        "蚕豆": "蚕豆",
        "芝麻菜": "arugula",
        "混合生菜": "mixed_greens",
        "金针菇": "enoki_mushroom",
        "菌菇": "mushroom",
        "火腿": "火腿",
        "水果": None,  # skip - not a pantry ingredient
    }

    # Protein type -> ingredient_id mapping
    PROTEIN_MAP = {
        "egg": "鸡蛋",
        "tofu": "tofu",
        "chicken": "chicken",
        "beef": "beef",
        "shrimp": "shrimp",
        "fish": "fish",
        "pork": "猪肉",
        "other_seafood": None,  # too generic, skip
        "other": None,
        "none": None,
    }

    # Get all dishes without dish_ingredients
    dishes = conn.execute("""
        SELECT d.id, d.name_cn, d.name_en, d.category_id, 
               d.protein_types, d.vegetables, d.carb_type,
               d.meal_roles, d.meal_tags
        FROM dishes d 
        WHERE (d.is_active = 1 OR d.is_active IS NULL)
        AND NOT EXISTS (SELECT 1 FROM dish_ingredients di WHERE di.dish_id = d.id)
        ORDER BY d.id
    """).fetchall()

    added = 0
    skipped = 0

    for d in dishes:
        dish_id = d["id"]
        name_cn = d["name_cn"]
        name_en = d["name_en"] or ""
        proteins = json.loads(d["protein_types"]) if d["protein_types"] else []
        vegetables = json.loads(d["vegetables"]) if d["vegetables"] else []
        carb_type = d["carb_type"]
        cat = d["category_id"]

        required_ings = []
        optional_ings = []

        # --- Map proteins to ingredient_ids ---
        for prot in proteins:
            ing_id = PROTEIN_MAP.get(prot)
            if ing_id and ing_id in valid_ingredients:
                if ing_id not in required_ings:
                    required_ings.append(ing_id)

        # --- Special name-based overrides ---
        # Cod/silver cod
        if "鳕鱼" in name_cn or "cod" in name_en.lower():
            if "cod" in valid_ingredients:
                required_ings = [x for x in required_ings if x != "fish"]
                if "cod" not in required_ings:
                    required_ings.append("cod")

        # Silken tofu (if name contains 嫩)
        if "嫩豆腐" in name_cn:
            if "silken_tofu" in valid_ingredients:
                required_ings = [x for x in required_ings if x != "tofu"]
                if "silken_tofu" not in required_ings:
                    required_ings.append("silken_tofu")

        # Beef steak
        if "牛排" in name_cn or "steak" in name_en.lower():
            if "beef_steak" in valid_ingredients:
                required_ings = [x for x in required_ings if x != "beef"]
                if "beef_steak" not in required_ings:
                    required_ings.append("beef_steak")

        # Pork ribs
        if "排骨" in name_cn:
            if "排骨" in valid_ingredients:
                required_ings = [x for x in required_ings if x != "猪肉"]
                if "排骨" not in required_ings:
                    required_ings.append("排骨")

        # Century egg
        if "皮蛋" in name_cn:
            if "皮蛋" in valid_ingredients:
                if "皮蛋" not in required_ings:
                    required_ings.append("皮蛋")

        # Caviar
        if "鱼子酱" in name_cn and "黑鱼子酱" not in name_cn:
            if "鱼子酱" in valid_ingredients:
                if "鱼子酱" not in required_ings:
                    required_ings.append("鱼子酱")
        if "黑鱼子酱" in name_cn:
            if "黑鱼子酱" in valid_ingredients:
                if "黑鱼子酱" not in required_ings:
                    required_ings.append("黑鱼子酱")
        if "三文鱼籽" in name_cn:
            if "三文鱼籽" in valid_ingredients:
                if "三文鱼籽" not in required_ings:
                    required_ings.append("三文鱼籽")

        # Miso
        if "味噌" in name_cn:
            if "味噌" in valid_ingredients:
                if "味噌" not in required_ings:
                    required_ings.append("味噌")

        # Ham
        if "火腿" in name_cn:
            if "火腿" in valid_ingredients:
                if "火腿" not in required_ings:
                    required_ings.append("火腿")

        # Seaweed
        if "紫菜" in name_cn:
            if "紫菜" in valid_ingredients:
                if "紫菜" not in required_ings:
                    required_ings.append("紫菜")

        # XO sauce
        if "XO酱" in name_cn or "xo酱" in name_cn.lower():
            if "xo酱" in valid_ingredients:
                if "xo酱" not in required_ings:
                    required_ings.append("xo酱")

        # Butter
        if "黄油" in name_cn:
            if "黄油" in valid_ingredients:
                if "黄油" not in required_ings:
                    required_ings.append("黄油")

        # --- Map vegetables ---
        for veg in vegetables:
            if veg == "水果":
                continue
            ing_id = VEG_MAP.get(veg, veg)
            if ing_id and ing_id in valid_ingredients:
                if ing_id not in required_ings:
                    required_ings.append(ing_id)

        # --- Map carb_type ---
        if carb_type == "rice":
            if "rice" in valid_ingredients and "rice" not in required_ings:
                required_ings.append("rice")
        elif carb_type == "noodle":
            if "面条" in valid_ingredients and "面条" not in required_ings:
                required_ings.append("面条")
        elif carb_type == "porridge":
            # Porridge needs rice or millet
            if "rice" in valid_ingredients and "rice" not in required_ings:
                required_ings.append("rice")
        elif carb_type == "coarse_grain":
            if "玉米" in name_cn:
                if "corn" in valid_ingredients and "corn" not in required_ings:
                    required_ings.append("corn")
            elif "淮山" in name_cn or "山药" in name_cn:
                if "淮山" in valid_ingredients and "淮山" not in required_ings:
                    required_ings.append("淮山")
            elif "红薯" in name_cn or "番薯" in name_cn:
                if "红薯" in valid_ingredients and "红薯" not in required_ings:
                    required_ings.append("红薯")
            elif "南瓜" in name_cn:
                if "南瓜" in valid_ingredients and "南瓜" not in required_ings:
                    required_ings.append("南瓜")
            elif "藜麦" in name_cn:
                if "藜麦" in valid_ingredients and "藜麦" not in required_ings:
                    required_ings.append("藜麦")
            elif "芋头" in name_cn:
                pass  # no taro ingredient in DB

        # --- Special handling for fruit_snack ---
        if cat == "fruit_snack":
            # Fruits don't need pantry matching - skip required ingredients
            # But still record for completeness
            if not required_ings:
                # Try to match fruit names
                fruit_map = {
                    "哈密瓜": None,
                    "青提": None,
                    "草莓": None,
                    "鲜橙桃胶冻": "orange" if "orange" in valid_ingredients else None,
                    "雪媚娘水果酸奶杯": "酸奶" if "酸奶" in valid_ingredients else None,
                }
                for fname, fid in fruit_map.items():
                    if fname in name_cn and fid:
                        required_ings.append(fid)

        # --- Special handling for dim_sum (包子/花卷/饺子) ---
        if cat == "staple_carb" and carb_type == "dim_sum":
            # These are pantry staples - don't need specific ingredients
            if not required_ings:
                # Add rice as optional (pantry staple)
                pass

        # --- Insert into dish_ingredients ---
        if not required_ings:
            # For dishes we still can't map, at least add something
            # For fruit_snack with no match - skip
            if cat == "fruit_snack":
                skipped += 1
                print(f"  SKIP (fruit): {dish_id} {name_cn}")
                continue
            # For dim_sum with no match - skip
            if cat == "staple_carb" and carb_type == "dim_sum":
                skipped += 1
                print(f"  SKIP (dim_sum): {dish_id} {name_cn}")
                continue
            # For seafood without specific ingredient - skip
            if not required_ings and cat == "protein_main":
                # Try fish/shrimp generic
                if "fish" in valid_ingredients:
                    required_ings.append("fish")
                elif "shrimp" in valid_ingredients:
                    required_ings.append("shrimp")

            if not required_ings:
                skipped += 1
                print(f"  SKIP (no match): {dish_id} {name_cn}")
                continue

        for ing_id in required_ings:
            conn.execute(
                "INSERT OR IGNORE INTO dish_ingredients (dish_id, ingredient_id, required) "
                "VALUES (?, ?, 1)",
                (dish_id, ing_id)
            )
            added += 1

        ings_str = ", ".join(f"{i}(req)" for i in required_ings)
        if optional_ings:
            ings_str += ", " + ", ".join(f"{i}(opt)" for i in optional_ings)
        print(f"  {dish_id} {name_cn}: {ings_str}")

    conn.commit()

    # Verify
    total_with = conn.execute(
        "SELECT COUNT(DISTINCT dish_id) as cnt FROM dish_ingredients"
    ).fetchone()
    total_ings = conn.execute("SELECT COUNT(*) as cnt FROM dish_ingredients").fetchone()
    total_dishes = conn.execute(
        "SELECT COUNT(*) as cnt FROM dishes WHERE is_active = 1 OR is_active IS NULL"
    ).fetchone()

    without = conn.execute("""
        SELECT COUNT(*) as cnt FROM dishes d 
        WHERE (d.is_active = 1 OR d.is_active IS NULL)
        AND NOT EXISTS (SELECT 1 FROM dish_ingredients di WHERE di.dish_id = d.id)
    """).fetchone()

    print(f"\n=== Migration Summary ===")
    print(f"Entries added: {added}")
    print(f"Skipped: {skipped}")
    print(f"Dishes with ingredients: {total_with['cnt']}/{total_dishes['cnt']}")
    print(f"Total dish_ingredients entries: {total_ings['cnt']}")
    print(f"Dishes still without ingredients: {without['cnt']}")

    # Show remaining without ingredients
    if without["cnt"] > 0:
        print("\n=== Remaining dishes without ingredients ===")
        rows = conn.execute("""
            SELECT d.id, d.name_cn, d.category_id, d.carb_type
            FROM dishes d 
            WHERE (d.is_active = 1 OR d.is_active IS NULL)
            AND NOT EXISTS (SELECT 1 FROM dish_ingredients di WHERE di.dish_id = d.id)
            ORDER BY d.id
        """).fetchall()
        for r in rows:
            print(f"  {r['id']} {r['name_cn']} (cat={r['category_id']}, carb={r['carb_type']})")

    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    migrate()
