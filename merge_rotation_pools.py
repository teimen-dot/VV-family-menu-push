#!/usr/bin/env python3
"""
merge_rotation_pools.py
将 rotation_pools 中的 27 项合并进主 dishes 数组，统一数据结构。
同时将 carb_type='tuber' 合并到 'coarse_grain'。
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DISH_POOL = BASE_DIR / "dish_pool.json"

def main():
    # 备份
    bak = DISH_POOL.with_suffix(f".json.bak.v2.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(DISH_POOL, bak)
    print(f"备份: {bak.name}")

    with open(DISH_POOL, "r", encoding="utf-8") as f:
        pool = json.load(f)

    dishes = pool["dishes"]
    existing_names = {d["name_cn"] for d in dishes}

    # 找到最大 ID 数字
    max_id = 0
    for d in dishes:
        try:
            num = int(d["id"].replace("dish_", ""))
            if num > max_id:
                max_id = num
        except (ValueError, KeyError):
            pass
    print(f"当前最大 ID: dish_{max_id:04d} ({len(dishes)} 道菜)")

    # 合并 carb_type=tuber -> coarse_grain
    tuber_count = 0
    for d in dishes:
        if d.get("carb_type") == "tuber":
            d["carb_type"] = "coarse_grain"
            tuber_count += 1
    print(f"carb_type tuber->coarse_grain: {tuber_count} 道")

    # 合并轮换池
    rotation_pools = pool.get("rotation_pools", {})
    new_count = 0

    for pool_key, pool_data in rotation_pools.items():
        items = pool_data.get("items", [])

        if pool_key == "porridge":
            category_id = "staple_carb"
            carb_type = "porridge"
            meal_tags = ["breakfast"]
            custom_tags = ["粥底轮换"]
            default_protein = []
            default_cooking = ["boil"]
        elif pool_key == "egg_styles":
            category_id = "egg_tofu"
            carb_type = None
            meal_tags = ["breakfast", "lunch", "dinner"]
            custom_tags = ["鸡蛋做法轮换"]
            default_protein = ["egg"]
            default_cooking = []
        else:
            print(f"  未知轮换池: {pool_key}, 跳过")
            continue

        for item in items:
            zh = item.get("zh", "").strip()
            en = item.get("en", "").strip()
            if not zh or zh in existing_names:
                print(f"  跳过(重复或空名): {zh}")
                continue

            max_id += 1
            new_id = f"dish_{max_id:04d}"

            new_dish = {
                "id": new_id,
                "name_cn": zh,
                "name_en": en,
                "category_id": category_id,
                "meal_tags": meal_tags.copy(),
                "banquet": False,
                "protein_types": default_protein.copy(),
                "vegetables": [],
                "vegetable_count": 0,
                "carb_type": carb_type,
                "meal_components": [],
                "taste": "light" if pool_key == "porridge" else "normal",
                "cooking_methods": default_cooking.copy(),
                "can_serve_warm": True,
                "custom_tags": custom_tags.copy(),
                "needs_review": True,
            }
            dishes.append(new_dish)
            existing_names.add(zh)
            new_count += 1
            print(f"  新增: {new_id} | {zh} | {category_id}")

    # 删除 rotation_pools 字段
    if "rotation_pools" in pool:
        del pool["rotation_pools"]
        print("已删除 rotation_pools 字段")

    # 保存
    with open(DISH_POOL, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

    print(f"\n=== 迁移完成 ===")
    print(f"原有菜品: {len(dishes) - new_count}")
    print(f"新增菜品: {new_count}")
    print(f"菜品总数: {len(dishes)}")
    print(f"rotation_pools: 已删除")
    print(f"carb_type tuber->coarse_grain: {tuber_count}")


if __name__ == "__main__":
    main()
