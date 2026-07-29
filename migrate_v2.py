#!/usr/bin/env python3
"""
dish_pool.json 数据结构迁移脚本 v1 -> v2
将旧的嵌套分类结构迁移为扁平的 dishes 数组 + 结构化字段。
保留所有菜品、图片映射、轮换池不变。
"""

import json
import os
import re
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISH_POOL_FILE = os.path.join(BASE_DIR, "dish_pool.json")

# ========== 新分类定义 ==========
NEW_CATEGORIES = [
    {"id": "protein_main", "label_cn": "蛋白质 / 主菜", "label_en": "Protein / Main", "order": 1, "active": True},
    {"id": "egg_tofu", "label_cn": "蛋类 / 豆制品", "label_en": "Egg / Tofu", "order": 2, "active": True},
    {"id": "vegetable_mushroom", "label_cn": "蔬菜 / 菌菇", "label_en": "Vegetable / Mushroom", "order": 3, "active": True},
    {"id": "soup", "label_cn": "汤 / 羹", "label_en": "Soup", "order": 4, "active": True},
    {"id": "staple_carb", "label_cn": "主食 / 碳水", "label_en": "Staple / Carb", "order": 5, "active": True},
    {"id": "cold_dish", "label_cn": "冷菜 / 凉拌", "label_en": "Cold Dish", "order": 6, "active": True},
    {"id": "one_pot_meal", "label_cn": "一餐型料理", "label_en": "One-Pot Meal", "order": 7, "active": True},
    {"id": "fruit_snack", "label_cn": "水果 / 加餐 / 下午茶", "label_en": "Fruit / Snack", "order": 8, "active": True},
]

# ========== 旧分类 -> 新分类映射 ==========
CATEGORY_MAP = {
    "protein_breakfast":      {"new_cat": "protein_main", "meal_tags": ["breakfast"]},
    "protein_lunch_dinner":   {"new_cat": "protein_main", "meal_tags": ["lunch", "dinner"]},
    "vegetable":              {"new_cat": None, "meal_tags": ["breakfast", "lunch", "dinner"]},  # 需要逐菜判断
    "soup":                   {"new_cat": "soup", "meal_tags": ["lunch", "dinner"]},
    "staple":                 {"new_cat": "staple_carb", "meal_tags": ["breakfast", "lunch", "dinner"]},
    "fruit_snack":            {"new_cat": "fruit_snack", "meal_tags": ["breakfast", "afternoon_snack"]},
    "cold_dish_reform":       {"new_cat": "cold_dish", "meal_tags": ["lunch", "dinner"]},
}

# ========== 蛋白质类型提取规则 ==========
PROTEIN_KEYWORDS = {
    "beef":        ["牛肉", "和牛", "牛排", "牛腩", "牛柳"],
    "pork":        ["猪肉", "排骨", "五花肉", "里脊", "瘦肉"],
    "chicken":     ["鸡肉", "鸡丝", "鸡腿", "鸡翅", "鸡胸", "去皮鸡"],
    "fish":        ["鱼", "鳕鱼", "银鳕鱼", "鲈鱼", "东星斑", "鱼片", "三文鱼"],
    "shrimp":      ["虾", "虾仁", "大虾"],
    "seafood":     ["贝", "瑶柱", "鲜贝", "蟹", "龙虾", "青口"],
    "egg":         ["鸡蛋", "蛋", "溏心蛋", "水煮蛋", "蒸蛋", "煎蛋"],
    "tofu":        ["豆腐", "豆制品", "嫩豆腐"],
    "caviar":      ["鱼子酱", "三文鱼籽", "黑鱼子酱"],
}

# ========== 蔬菜提取规则（从 ingredients 中提取）==========
VEGETABLE_KEYWORDS = [
    "豆苗", "莴笋", "西兰花", "芦笋", "白萝卜", "空心菜", "红苋菜", "云南小瓜",
    "沙拉菜", "小白菜", "枸杞芽", "口蘑", "蟹味菇", "芥蓝", "冬瓜", "百合",
    "莲藕", "茄子", "舞茸", "秋葵", "番茄", "茴香", "蚕豆", "芹菜", "洋葱",
    "丝瓜", "红薯", "土豆", "玉米", "苦瓜", "苋菜", "白菜", "菠菜", "生菜",
    "荷兰豆", "四季豆", "豆角", "木耳", "香菇", "金针菇", "杏鲍菇", "白玉菇",
    "胡萝卜", "青笋", "茭白", "芋头", "山药", "淮山", "南瓜",
]

# ========== 主食类型提取规则 ==========
def guess_carb_type(dish):
    zh = dish.get("zh", "")
    en = dish.get("en", "")
    ingredients = dish.get("ingredients", [])
    tags = dish.get("tags", [])
    combined = zh + en + "".join(ingredients) + "".join(tags)

    if any(k in combined for k in ["粥", "porridge", "米粥", "小米粥"]):
        return "porridge"
    if any(k in combined for k in ["面", "noodle", "面条", "打卤面"]):
        return "noodle"
    if any(k in combined for k in ["面包", "bread", "饺子", "包子", "馒头", "寿司", "饭团", "sushi", "roll"]):
        return "dim_sum"
    if any(k in combined for k in ["红薯", "番薯", "potato", "土豆", "薯", "tuber", "sweet potato"]):
        return "tuber"
    if any(k in combined for k in ["粗粮", "16谷", "藜麦", "quinoa", "玉米", "corn", "小米", "millet", "莲子"]):
        return "coarse_grain"
    if any(k in combined for k in ["米饭", "rice", "白米", "二米"]):
        return "rice"
    return "other"


def extract_protein_types(dish):
    """从菜品名称和食材中提取蛋白质类型"""
    zh = dish.get("zh", "")
    en = dish.get("en", "")
    ingredients = dish.get("ingredients", [])
    combined = zh + en + "".join(ingredients)

    types = []
    for ptype, keywords in PROTEIN_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                if ptype not in types:
                    types.append(ptype)
                break
    return types


def extract_vegetables(dish):
    """从食材中提取蔬菜"""
    ingredients = dish.get("ingredients", [])
    zh = dish.get("zh", "")
    combined = zh + "".join(ingredients)

    veggies = []
    for v in VEGETABLE_KEYWORDS:
        if v in combined and v not in veggies:
            veggies.append(v)
    return veggies


def classify_vegetable_category(dish):
    """
    对原 vegetable (蔬菜/蛋类) 分类的菜进行二次分类。
    返回 (new_category, needs_review)
    """
    zh = dish.get("zh", "")
    ingredients = dish.get("ingredients", [])
    combined = zh + "".join(ingredients)

    # 蛋类 / 豆制品
    if any(k in combined for k in ["鸡蛋", "蛋", "豆腐", "豆制品"]):
        # 但排除名字里有"蔬菜"但食材含蛋的情况——以食材为准
        if any(k in combined for k in ["鸡蛋", "蛋炒", "蒸蛋", "煎蛋", "豆腐"]):
            return "egg_tofu", False

    # 冷菜
    if "沙拉" in combined:
        return "cold_dish", True  # 需要人工确认

    # 默认蔬菜 / 菌菇
    return "vegetable_mushroom", False


def guess_cooking_methods(dish):
    """根据菜名猜测烹饪方式"""
    zh = dish.get("zh", "")
    en = dish.get("en", "")
    combined = zh + en

    methods = []
    if any(k in combined for k in ["蒸", "steamed", "蒸蛋"]):
        methods.append("steamed")
    if any(k in combined for k in ["煮", "boiled", "水煮", "煮蛋"]):
        methods.append("boiled")
    if any(k in combined for k in ["炒", "stir-fried", "stir_fried", "爆炒", "小炒"]):
        methods.append("stir_fried")
    if any(k in combined for k in ["炖", "stewed", "清炖", "焖", "braised"]):
        methods.append("stewed")
    if any(k in combined for k in ["煎", "pan-fried", "pan_fried", "香煎"]):
        methods.append("pan_fried")
    if any(k in combined for k in ["烤", "roasted", "grilled", "baked"]):
        methods.append("roasted")
    if any(k in combined for k in ["凉拌", "cold", "沙拉", "marinated"]):
        methods.append("cold_mixed")
    if any(k in combined for k in ["灼", "白灼", "blanched"]):
        methods.append("blanched")
    if any(k in combined for k in ["温拌", "warm tossed"]):
        methods.append("warm_tossed")

    return methods if methods else []


def guess_taste(dish):
    """根据菜名和标签猜测口味"""
    zh = dish.get("zh", "")
    tags = dish.get("tags", [])
    combined = zh + "".join(tags)

    if any(k in combined for k in ["辣", "麻辣", "水煮", "红油", "辣子", "spicy", "chili"]):
        return "spicy"
    if any(k in combined for k in ["浓", "红烧", "酱爆", "XO酱", "rich"]):
        return "rich"
    if any(k in combined for k in ["清淡", "清蒸", "清炒", "白灼", "light", "steamed"]):
        return "light"
    return "normal"


def migrate():
    with open(DISH_POOL_FILE, "r", encoding="utf-8") as f:
        old_pool = json.load(f)

    old_categories = old_pool.get("categories", {})
    rotation_pools = old_pool.get("rotation_pools", {})

    new_dishes = []
    dish_counter = 0

    for old_cat_key, cat_data in old_categories.items():
        mapping = CATEGORY_MAP.get(old_cat_key, {"new_cat": "vegetable_mushroom", "meal_tags": ["lunch", "dinner"]})
        dishes = cat_data.get("dishes", [])
        old_label = cat_data.get("label", old_cat_key)

        for dish in dishes:
            dish_counter += 1
            zh = dish.get("zh", "")
            en = dish.get("en", "")
            ingredients = dish.get("ingredients", [])
            tags = dish.get("tags", [])

            # 确定新分类
            new_cat = mapping["new_cat"]
            meal_tags = list(mapping["meal_tags"])
            needs_review = False

            if new_cat is None:
                # vegetable 分类需要逐菜判断
                new_cat, needs_review = classify_vegetable_category(dish)

            # 特殊处理：冷菜改造类
            can_serve_warm = False
            if old_cat_key == "cold_dish_reform":
                can_serve_warm = True
                if "reform" in dish:
                    # 把改造说明放到 custom_tags 里保留
                    tags = tags + ["需改造"]

            # 提取结构化字段
            protein_types = extract_protein_types(dish)
            vegetables = extract_vegetables(dish)
            carb_type = guess_carb_type(dish) if new_cat == "staple_carb" else None
            cooking_methods = guess_cooking_methods(dish)
            taste = guess_taste(dish)

            # 如果是一餐型料理（如饭菜一锅蒸、牛肉面等），设置 meal_components
            meal_components = []
            if new_cat == "one_pot_meal" or any(k in zh for k in ["一锅", "面", "饭面"]):
                if protein_types:
                    meal_components.append("protein")
                if vegetables:
                    meal_components.append("vegetable")
                meal_components.append("carb")

            # 判断是否为家宴菜（根据标签）
            banquet = "高端" in tags or "家宴" in tags

            new_dish = {
                "id": f"dish_{dish_counter:04d}",
                "name_cn": zh,
                "name_en": en,
                "category_id": new_cat,
                "meal_tags": meal_tags,
                "banquet": banquet,
                "protein_types": protein_types,
                "vegetables": vegetables,
                "vegetable_count": len(vegetables),
                "carb_type": carb_type,
                "meal_components": meal_components,
                "taste": taste,
                "cooking_methods": cooking_methods,
                "can_serve_warm": can_serve_warm,
                "custom_tags": [],
                "needs_review": needs_review,
                "ingredients": ingredients,
                "old_tags": tags,
                "old_category": old_cat_key,
            }
            new_dishes.append(new_dish)

    # 构建新的 pool
    new_pool = {
        "meta": {
            "version": "2.0",
            "last_updated": date.today().isoformat(),
            "description": "家庭菜单管家 - 菜谱数据库 v2.0。结构化字段支持 AI 配餐。",
        },
        "categories": NEW_CATEGORIES,
        "dishes": new_dishes,
        "custom_tags_def": [],
        "rotation_pools": rotation_pools,
    }

    # 写入
    with open(DISH_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(new_pool, f, ensure_ascii=False, indent=2)

    # 统计
    print(f"迁移完成！")
    print(f"  菜品总数: {len(new_dishes)}")
    print(f"  分类数: {len(NEW_CATEGORIES)}")
    print(f"  轮换池: {len(rotation_pools)} 个")

    # 按新分类统计
    cat_counts = {}
    for d in new_dishes:
        cat_counts[d["category_id"]] = cat_counts.get(d["category_id"], 0) + 1

    print(f"\n  各分类菜品数:")
    for cat in NEW_CATEGORIES:
        count = cat_counts.get(cat["id"], 0)
        print(f"    {cat['label_cn']}: {count}")

    # needs_review 统计
    review_count = sum(1 for d in new_dishes if d["needs_review"])
    print(f"\n  需人工审核: {review_count} 道")

    # 有蛋白质信息的
    has_protein = sum(1 for d in new_dishes if d["protein_types"])
    print(f"  已提取蛋白质类型: {has_protein} 道")

    # 有蔬菜信息的
    has_veg = sum(1 for d in new_dishes if d["vegetables"])
    print(f"  已提取蔬菜: {has_veg} 道")

    # 家宴标记
    banquet_count = sum(1 for d in new_dishes if d["banquet"])
    print(f"  家宴推荐: {banquet_count} 道")


if __name__ == "__main__":
    migrate()
