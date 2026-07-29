#!/usr/bin/env python3
"""
家庭菜单管家 - AI 缺口补充法配餐引擎 (Gap-Filler Engine)

核心原则：
  1. 不按分类机械抽菜，而是先选核心菜 → 分析营养组成 → 计算缺口 → 只补缺失
  2. 每道菜的贡献基于结构化字段(protein_types/vegetables/carb_type/meal_components)计算
  3. 三餐丰富度不同：早餐HIGH / 午餐LOW-MEDIUM / 晚餐HIGH
  4. 生成后执行 BALANCE CHECK + FINAL REVIEW

用法：
  python gap_filler.py                    # 生成今天菜单
  python gap_filler.py --day 4            # 生成 Day 4
  python gap_filler.py --locked-breakfast "鱼子酱溏心蛋,香港红薯"
  python gap_filler.py --preview           # 生成HTML预览
  python gap_filler.py --update-menu       # 更新 menu_data.json
"""

import json
import random
import argparse
from datetime import date, datetime, timedelta
from collections import Counter

# ============================================================
# 常量定义
# ============================================================

DISH_POOL_FILE = "dish_pool.json"
PHOTO_MANIFEST_FILE = "photo_manifest.json"
MENU_DATA_FILE = "menu_data.json"
CONFIG_FILE = "config.json"

# ---- 非蔬菜类食材（主食/粗粮/薯类，不能算蔬菜） ----
NOT_VEGETABLES = {
    "红薯", "番薯", "地瓜", "粗粮", "玉米", "淮山", "山药", "土豆",
    "南瓜", "芋头",
}

# ---- 装饰性配菜/调味料（不计算为一份蔬菜） ----
GARNISH = {
    "葱花", "葱", "香菜", "姜", "蒜", "蒜蓉", "辣椒", "枸杞",
}

# ---- 烹饪方法归一化 ----
COOKING_METHOD_NORMALIZE = {
    "stir_fried": "stir_fry",
    "pan_fried": "pan_fry",
    "cold_mixed": "cold_mix",
    "roasted": "roast",
    "warm_tossed": "warm_toss",
}

# ---- 有效主食类型（rice/coarse_grain/porridge/noodle/dim_sum/other） ----
# 其中 other/dim_sum 不应该作为唯一主食来源（晚餐尤其需要米饭/粗粮饭）
WEAK_CARB_TYPES = {"other", "dim_sum"}


# ============================================================
# 数据加载
# ============================================================

def load_json(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def load_dish_pool():
    return load_json(DISH_POOL_FILE)


def load_photo_manifest():
    try:
        return load_json(PHOTO_MANIFEST_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ============================================================
# 营养分析器
# ============================================================

class NutritionAnalyzer:
    """分析每道菜的实际营养贡献"""

    @staticmethod
    def normalize_cooking_methods(methods):
        result = []
        for m in (methods or []):
            normalized = COOKING_METHOD_NORMALIZE.get(m, m)
            if normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def filter_real_vegetables(veg_list):
        """过滤掉非蔬菜（红薯/玉米/淮山/装饰性配菜等）"""
        real = []
        for v in (veg_list or []):
            v = v.strip()
            if not v:
                continue
            if v in NOT_VEGETABLES:
                continue
            if v in GARNISH:
                continue
            if v not in real:
                real.append(v)
        return real

    @staticmethod
    def filter_proteins(protein_list):
        """过滤掉 none 等无效蛋白质"""
        return [p for p in (protein_list or []) if p and p != "none"]

    @staticmethod
    def analyze(dish):
        """分析一道菜的营养贡献"""
        cat = dish.get("category_id", "")
        proteins = NutritionAnalyzer.filter_proteins(dish.get("protein_types", []))
        vegetables = NutritionAnalyzer.filter_real_vegetables(dish.get("vegetables", []))
        carb_type = dish.get("carb_type")
        is_soup = (cat == "soup")
        is_fruit = (cat == "fruit_snack")
        cooking_methods = NutritionAnalyzer.normalize_cooking_methods(
            dish.get("cooking_methods", [])
        )
        return {
            "proteins": proteins,
            "vegetables": vegetables,
            "carb_type": carb_type,
            "is_soup": is_soup,
            "is_fruit": is_fruit,
            "cooking_methods": cooking_methods,
            "taste": dish.get("taste", "normal"),
            "banquet": dish.get("banquet", False),
            "custom_tags": dish.get("custom_tags", []),
            "name_cn": dish.get("name_cn", ""),
            "name_en": dish.get("name_en", ""),
            "id": dish.get("id", ""),
            "category_id": cat,
            "meal_tags": dish.get("meal_tags", []),
        }


# ============================================================
# 营养状态
# ============================================================

class MealState:
    """跟踪一餐中已选菜品的累计营养状态"""

    def __init__(self):
        self.dishes = []
        self.proteins = set()
        self.vegetables = set()
        self.carb_types = set()
        self.has_soup = False
        self.has_fruit = False
        self.cooking_methods = []
        self.tastes = []
        self.protein_count = 0        # 蛋白质主菜道数
        self.vegetable_dish_count = 0 # 纯蔬菜菜品道数
        self.carb_count = 0           # 主食道数

    def add_dish(self, analysis):
        self.dishes.append(analysis)
        self.proteins.update(analysis["proteins"])
        self.vegetables.update(analysis["vegetables"])
        if analysis["carb_type"]:
            self.carb_types.add(analysis["carb_type"])
        if analysis["is_soup"]:
            self.has_soup = True
        if analysis["is_fruit"]:
            self.has_fruit = True
        for cm in analysis["cooking_methods"]:
            if cm not in self.cooking_methods:
                self.cooking_methods.append(cm)
        self.tastes.append(analysis["taste"])

        cat = analysis["category_id"]
        if cat in ("protein_main", "egg_tofu"):
            self.protein_count += 1
        elif cat == "vegetable_mushroom":
            self.vegetable_dish_count += 1
        elif cat == "staple_carb":
            self.carb_count += 1
        elif cat == "one_pot_meal":
            self.protein_count += 1
            self.carb_count += 1

    @property
    def vegetable_count(self):
        return len(self.vegetables)

    @property
    def dish_count(self):
        return len(self.dishes)

    @property
    def has_strong_carb(self):
        """是否有强主食（米饭/粗粮饭/粥/面条），弱主食(other/dim_sum)不算"""
        return bool(self.carb_types - WEAK_CARB_TYPES)


# ============================================================
# 三餐目标定义
# ============================================================

class MealTarget:
    """每餐的营养目标"""

    def __init__(self, meal_type):
        self.meal_type = meal_type
        if meal_type == "breakfast":
            self.richness = "HIGH"
            self.protein_min = 1
            self.protein_max = 2
            self.vegetable_min = 2
            self.vegetable_max = 3
            self.carb_min = 1
            self.carb_max = 2
            self.soup_default = False
            self.fruit_optional = True
            self.min_dishes = 3   # 至少3道：蛋白质+蔬菜+主食
            self.max_dishes = 5
            self.prefer_coarse_grain = True
        elif meal_type == "lunch":
            self.richness = "LOW"
            self.protein_min = 1
            self.protein_max = 1
            self.vegetable_min = 1
            self.vegetable_max = 2
            self.carb_min = 1
            self.carb_max = 1
            self.soup_default = False
            self.fruit_optional = False
            self.min_dishes = 3   # 蛋白质+蔬菜+主食
            self.max_dishes = 4
            self.prefer_coarse_grain = True
        elif meal_type == "dinner":
            self.richness = "HIGH"
            self.protein_min = 1
            self.protein_max = 2
            self.vegetable_min = 2
            self.vegetable_max = 3
            self.carb_min = 1
            self.carb_max = 1
            self.soup_default = True
            self.fruit_optional = False
            self.min_dishes = 4   # 至少4道：蛋白质+蔬菜+汤+主食
            self.max_dishes = 7
            self.prefer_coarse_grain = False
        else:
            raise ValueError(f"Unknown meal type: {meal_type}")


# ============================================================
# 缺口补充法引擎
# ============================================================

class GapFiller:
    """AI 缺口补充法配餐引擎"""

    def __init__(self, dish_pool, seed=None, inventory=None):
        self.dishes = dish_pool["dishes"]
        self.categories = {c["id"]: c for c in dish_pool.get("categories", [])}
        self.rng = random.Random(seed)
        self.inventory = inventory
        self.analyzed = {}
        for d in self.dishes:
            self.analyzed[d["id"]] = NutritionAnalyzer.analyze(d)

    def get_candidates(self, meal_type, exclude_ids=None):
        exclude = exclude_ids or set()
        return [
            a for a in self.analyzed.values()
            if a["id"] not in exclude and meal_type in a["meal_tags"]
        ]

    def score_dish(self, analysis, state, target, day_proteins=None):
        """
        给一道菜打分，衡量它填补当前缺口的能力。
        day_proteins: 当天其他餐已用的蛋白质类型集合（跨餐去重用）
        """
        score = 0
        day_proteins = day_proteins or set()

        # === 一餐型料理降分 ===
        # 一餐型料理是单菜组合，适合个人午餐，不适合家庭晚餐
        if analysis["category_id"] == "one_pot_meal":
            if target.meal_type == "dinner":
                score -= 200  # 家庭晚餐几乎排除
            elif target.meal_type == "breakfast":
                score -= 120  # 家庭早餐也不适合一锅料理
            else:
                score -= 25  # 午餐可接受，但也不优先

        # === 无营养数据菜品降分 ===
        # 如果一道菜没有蛋白质、没有蔬菜、没有主食、不是汤、不是水果
        # 说明数据不完整，不应该被引擎选中
        has_any_nutrition = (
            analysis["proteins"]
            or analysis["vegetables"]
            or analysis["carb_type"]
            or analysis["is_soup"]
            or analysis["is_fruit"]
        )
        if not has_any_nutrition:
            score -= 30

        # === 冷菜/凉拌在午餐降分 ===
        if analysis["category_id"] == "cold_dish" and target.meal_type == "lunch":
            score -= 25

        # === 蛋白质缺口 ===
        # 使用实际蛋白质类型（包括汤里的）而非仅类别
        current_protein_types = set(state.proteins)
        new_proteins = set(analysis["proteins"]) - current_protein_types

        # 计算实际蛋白质主菜道数（protein_main/egg_tofu 类别）
        is_protein_dish = analysis["category_id"] in ("protein_main", "egg_tofu")

        if not current_protein_types and analysis["proteins"]:
            # 还没有任何蛋白质，急需
            score += 40
        elif len(current_protein_types) < target.protein_max and new_proteins:
            # 蛋白质种类不足上限，新种类加分
            score += 25
        elif is_protein_dish and state.protein_count >= target.protein_max:
            # 蛋白质主菜已达上限，再选蛋白质主菜大幅降分
            score -= 35
        elif is_protein_dish and state.protein_count >= target.protein_min and not new_proteins:
            # 已满足最低蛋白质要求，同类型蛋白质主菜降分
            score -= 20

        # 同餐内蛋白质类型重复：大幅降分（不限类别）
        same_protein = len(set(analysis["proteins"]) & current_protein_types)
        if same_protein > 0:
            if is_protein_dish:
                score -= same_protein * 20
            else:
                score -= same_protein * 12  # 非蛋白质类别菜也有同种蛋白质时降分

        # 跨餐蛋白质去重：同一天其他餐已用过的蛋白质降分
        if analysis["proteins"]:
            cross_overlap = len(set(analysis["proteins"]) & day_proteins)
            score -= cross_overlap * 18

        # === 蔬菜缺口 ===
        new_vegs = set(analysis["vegetables"]) - state.vegetables
        veg_needed = max(0, target.vegetable_min - state.vegetable_count)
        if veg_needed > 0 and new_vegs:
            score += 25 * min(len(new_vegs), veg_needed)
        elif state.vegetable_count >= target.vegetable_max and analysis["vegetables"]:
            score -= 15

        # === 主食缺口 ===
        if state.carb_count < target.carb_min and analysis["carb_type"]:
            score += 25
            if analysis["carb_type"] in WEAK_CARB_TYPES and not state.has_strong_carb:
                score -= 10
        elif state.carb_count >= target.carb_max and analysis["carb_type"]:
            score -= 30

        if target.prefer_coarse_grain and analysis["carb_type"] == "coarse_grain":
            score += 8

        # === 汤的处理 ===
        if analysis["is_soup"]:
            if target.soup_default and not state.has_soup:
                score += 20
                if analysis["proteins"]:
                    score += 8
                if analysis["vegetables"]:
                    score += 8
            elif not target.soup_default:
                score -= 20
            else:
                score -= 10

        # === 水果 ===
        if analysis["is_fruit"]:
            if target.fruit_optional and not state.has_fruit and state.dish_count >= target.min_dishes - 1:
                score += 5
            else:
                score -= 20

        # === 烹饪方式多样性 ===
        new_methods = [m for m in analysis["cooking_methods"] if m not in state.cooking_methods]
        score += len(new_methods) * 4

        # === 口味平衡 ===
        if analysis["taste"] == "spicy":
            spicy_count = state.tastes.count("spicy")
            score -= spicy_count * 15
        elif analysis["taste"] == "light" and "spicy" in state.tastes:
            score += 5

        # === 午餐偏好明确蛋白质主菜（protein_main/egg_tofu类别）===
        if target.meal_type == "lunch" and analysis["category_id"] in ("protein_main", "egg_tofu"):
            score += 25
        # 午餐不偏好蛋白质埋在蔬菜里（如肉末空心菜）
        # 大幅降分：午餐应该有一道独立的蛋白质主菜
        if target.meal_type == "lunch" and analysis["category_id"] == "vegetable_mushroom" and analysis["proteins"]:
            score -= 25

        # === 早餐第二主食奖励 ===
        # 当已有主食但缺第二主食时，粥/粗粮/饭类主食加分
        if target.meal_type == "breakfast" and state.carb_count == 1 and analysis["carb_type"] in ("porridge", "rice", "coarse_grain"):
            if analysis["carb_type"] not in state.carb_types:
                score += 20

        # === 食材重复检查（同餐内） ===
        all_ingredients = set()
        for d in state.dishes:
            all_ingredients |= set(d["vegetables"])
            all_ingredients |= set(d["proteins"])
        overlap = len(set(analysis["vegetables"]) & all_ingredients)
        score -= overlap * 10

        # === 家宴菜降分 ===
        if analysis["banquet"]:
            score -= 5

        # === 当缺口已填满时，优先非蛋白质非主食菜品 ===
        # 避免为了凑min_dishes而加多余蛋白质
        gaps_filled = (
            len(current_protein_types) >= target.protein_min
            and state.vegetable_count >= target.vegetable_min
            and state.carb_count >= target.carb_min
        )
        if gaps_filled:
            if is_protein_dish:
                score -= 15  # 缺口已满，不需要更多蛋白质
            if analysis["carb_type"] and state.carb_count >= target.carb_min:
                score -= 15  # 不需要更多主食
            # 蔬菜/菌菇类菜品轻微加分（丰富度）
            if analysis["category_id"] == "vegetable_mushroom" and state.vegetable_count < target.vegetable_max:
                score += 5

        # === 同类主食重复惩罚 ===
        # 例如已有rice类型主食，再选rice类型减分
        if analysis["carb_type"] and analysis["carb_type"] in state.carb_types:
            score -= 15

        # === 随机扰动 ===
        score += self.rng.uniform(0, 6)

        return score

    def generate_meal(self, meal_type, locked_dish_ids=None, day_history=None, day_proteins=None):
        """
        生成一餐菜单（缺口补充法）。
        返回: (dishes, state, log)
        """
        target = MealTarget(meal_type)
        locked_ids = locked_dish_ids or []
        day_history = day_history or set()
        day_proteins = day_proteins or set()

        # 加载锁定菜品
        locked_analyses = [self.analyzed[did] for did in locked_ids if did in self.analyzed]

        state = MealState()
        log = []

        # 先加入锁定菜品
        for la in locked_analyses:
            state.add_dish(la)
            log.append(f"  [LOCKED] {la['name_cn']} | protein={la['proteins']} veg={la['vegetables']} carb={la['carb_type']}")

        # 排除当天已用的菜
        exclude = set(day_history) | set(locked_ids)

        # ---- 缺口补充循环 ----
        max_iterations = 10
        for iteration in range(max_iterations):
            gaps = self._check_gaps(state, target, locked_analyses)

            # 如果缺口已填满但还没达到最低菜品数，继续加菜（选加分最高的）
            if not gaps:
                if state.dish_count < target.min_dishes:
                    log.append(f"  [CONTINUE] Gaps filled but only {state.dish_count} dishes (min={target.min_dishes})")
                else:
                    log.append(f"  [COMPLETE] All gaps filled, {state.dish_count} dishes")
                    break

            if state.dish_count >= target.max_dishes:
                log.append(f"  [STOP] Max dishes ({target.max_dishes}) reached")
                break

            candidates = self.get_candidates(meal_type, exclude_ids=exclude)
            if not candidates:
                log.append(f"  [WARN] No more candidates")
                break

            # 关键缺口过滤：午餐明确蛋白质缺口时，只选 protein_main/egg_tofu 类别
            # 同时排除"肉末"类菜品（蛋白质仅作调味，不是主菜）
            if "clear_protein" in gaps:
                filtered = [
                    c for c in candidates
                    if c["category_id"] in ("protein_main", "egg_tofu")
                    and "肉末" not in c["name_cn"]
                ]
                if filtered:
                    candidates = filtered
                    log.append(f"  [FILTER] clear_protein gap: {len(candidates)} clear protein candidates")

            # 强主食缺口过滤：只选 staple_carb 类别且非弱主食类型
            if "strong_carb" in gaps:
                strong_carb_types = {"rice", "coarse_grain", "porridge", "noodle"}
                filtered = [
                    c for c in candidates
                    if c["category_id"] == "staple_carb"
                    and c["carb_type"] in strong_carb_types
                ]
                if filtered:
                    candidates = filtered
                    log.append(f"  [FILTER] strong_carb gap: {len(candidates)} strong carb candidates")

            # 打分
            scored = []
            for c in candidates:
                s = self.score_dish(c, state, target, day_proteins)
                scored.append((s, c))
            scored.sort(key=lambda x: x[0], reverse=True)

            # 如果缺口已填满且最佳分数为负，判断是否还需要加菜
            if not gaps and scored[0][0] < 0:
                # 如果还没达到最低菜品数，仍然加菜（选分数最高的）
                if state.dish_count < target.min_dishes:
                    log.append(f"  [FORCE ADD] Below min_dishes ({state.dish_count}<{target.min_dishes}), adding best candidate")
                else:
                    log.append(f"  [STOP] No beneficial dish to add (best={scored[0][0]:.1f})")
                    break

            # 关键缺口时只选最高分（不随机），非关键缺口从前3名随机选
            critical_gaps = {"protein", "clear_protein", "carb", "strong_carb"}
            has_critical = any(g in critical_gaps for g in gaps)
            if has_critical:
                top_n = 1  # 关键缺口：选最高分
            else:
                top_n = min(3, len(scored))  # 非关键缺口：前3随机
            chosen_idx = self.rng.randint(0, top_n - 1) if top_n > 1 else 0
            chosen = scored[chosen_idx][1]
            chosen_score = scored[chosen_idx][0]

            state.add_dish(chosen)
            exclude.add(chosen["id"])
            log.append(
                f"  [ADD] {chosen['name_cn']} (score={chosen_score:.1f}) | "
                f"protein={chosen['proteins']} veg={chosen['vegetables']} "
                f"carb={chosen['carb_type']} soup={chosen['is_soup']}"
            )

        log.append(
            f"  [FINAL] dishes={state.dish_count} | "
            f"proteins={list(state.proteins)} | "
            f"veg={list(state.vegetables)} ({state.vegetable_count}种) | "
            f"carb={list(state.carb_types)} | "
            f"soup={state.has_soup} | fruit={state.has_fruit}"
        )
        return state.dishes, state, log

    def _check_gaps(self, state, target, locked_analyses):
        """
        检查当前营养缺口。
        使用实际蛋白质类型（包括汤里的）而非仅类别计数。
        """
        gaps = []

        # 合并锁定菜的营养
        all_proteins = set(state.proteins)
        all_vegs = set(state.vegetables)
        all_carbs = set(state.carb_types)
        protein_dish_count = state.protein_count  # 类别计数（protein_main/egg_tofu/one_pot_meal）
        carb_dish_count = state.carb_count
        clear_protein_dish_count = 0  # 明确的蛋白质主菜（非蔬菜/菌菇类）
        for la in locked_analyses:
            all_proteins |= set(la["proteins"])
            all_vegs |= set(la["vegetables"])
            if la["carb_type"]:
                all_carbs.add(la["carb_type"])
            if la["category_id"] in ("protein_main", "egg_tofu", "one_pot_meal"):
                protein_dish_count += 1
            # 午餐偏好明确蛋白质主菜（非蔬菜/菌菇类）
            if la["category_id"] in ("protein_main", "egg_tofu"):
                clear_protein_dish_count += 1
            if la["category_id"] in ("staple_carb", "one_pot_meal"):
                carb_dish_count += 1

        # 蛋白质缺口
        if len(all_proteins) < target.protein_min and protein_dish_count < target.protein_min:
            gaps.append("protein")
        if protein_dish_count < target.protein_min and len(all_proteins) == 0:
            gaps.append("protein")

        # 午餐明确蛋白质主菜缺口（不要蛋白质埋在蔬菜里）
        if target.meal_type == "lunch" and clear_protein_dish_count < 1:
            if clear_protein_dish_count + state.protein_count < 1:
                gaps.append("clear_protein")

        # 蔬菜缺口
        if len(all_vegs) < target.vegetable_min:
            gaps.append("vegetable")

        # 主食缺口
        if carb_dish_count < target.carb_min:
            gaps.append("carb")

        # 强主食缺口：已有主食但全是弱主食（dim_sum/other），晚餐需要米饭/粗粮饭
        has_strong = bool(all_carbs - WEAK_CARB_TYPES)
        if carb_dish_count >= target.carb_min and not has_strong and state.dish_count < target.max_dishes:
            gaps.append("strong_carb")

        # 早餐第二主食偏好（HIGH丰富度需要1-2种主食）
        if target.meal_type == "breakfast" and carb_dish_count == 1 and state.dish_count < target.max_dishes:
            # 已有1种主食，仍有空间，加"second_staple"缺口
            # 优先粥/米饭/粗粮饭作为第二主食
            gaps.append("second_staple")

        # 汤缺口（仅晚餐默认需要）
        if target.soup_default and not state.has_soup and state.dish_count < target.max_dishes:
            gaps.append("soup")

        return gaps

    def generate_day(self, locked=None, seed=None):
        """生成一天三餐"""
        if seed is not None:
            self.rng = random.Random(seed)

        locked = locked or {}
        all_logs = {}
        day_history = set()
        day_proteins = set()  # 跨餐蛋白质跟踪

        result = {}
        for meal_type in ["breakfast", "lunch", "dinner"]:
            locked_ids = locked.get(meal_type, [])
            for lid in locked_ids:
                day_history.add(lid)

            dishes, state, log = self.generate_meal(
                meal_type,
                locked_dish_ids=locked_ids,
                day_history=day_history,
                day_proteins=set(day_proteins),  # 传副本
            )

            for d in dishes:
                day_history.add(d["id"])
                day_proteins.update(d["proteins"])

            result[meal_type] = {"dishes": dishes, "state": state}
            all_logs[meal_type] = log

        review = self.final_review(result)
        all_logs["review"] = review
        return result, all_logs

    def final_review(self, day_result):
        """最终审查"""
        issues = []

        b_state = day_result.get("breakfast", {}).get("state")
        l_state = day_result.get("lunch", {}).get("state")
        d_state = day_result.get("dinner", {}).get("state")

        # BREAKFAST CHECK
        if b_state:
            if b_state.vegetable_count < 2:
                issues.append(f"早餐蔬菜不足: {b_state.vegetable_count}种")
            if b_state.carb_count > 2:
                issues.append(f"早餐主食过多: {b_state.carb_count}道")
            if len(b_state.proteins) < 1:
                issues.append("早餐蛋白质不足")

        # LUNCH CHECK
        if l_state:
            if l_state.dish_count > 4:
                issues.append(f"午餐菜品过多: {l_state.dish_count}道")
            # 用实际蛋白质类型而非类别计数
            if len(l_state.proteins) < 1:
                issues.append("午餐缺蛋白质")
            if l_state.vegetable_count < 1:
                issues.append("午餐缺蔬菜")
            if l_state.carb_count < 1 and not l_state.carb_types:
                issues.append("午餐缺主食")

        # DINNER CHECK
        if d_state:
            if l_state and d_state.dish_count <= l_state.dish_count:
                issues.append("晚餐菜品数不多于午餐")
            if len(d_state.cooking_methods) < 2 and d_state.dish_count >= 3:
                issues.append(f"晚餐烹饪方式单一: {d_state.cooking_methods}")
            spicy_count = d_state.tastes.count("spicy")
            if spicy_count > 1:
                issues.append(f"晚餐辣味过多: {spicy_count}道")
            # 晚餐弱主食检查：有主食但没有强主食（米饭/粗粮/粥/面）
            if d_state.carb_types and not d_state.has_strong_carb:
                issues.append(f"晚餐主食过弱: {list(d_state.carb_types)}（需米饭/粗粮饭）")

        # 跨餐检查
        all_vegs = []
        all_proteins = []
        for mk in ["breakfast", "lunch", "dinner"]:
            ms = day_result.get(mk, {}).get("state")
            if ms:
                all_vegs.extend(ms.vegetables)
                all_proteins.extend(ms.proteins)
        for veg, cnt in Counter(all_vegs).items():
            if cnt >= 3:
                issues.append(f"食材 '{veg}' 一天出现 {cnt} 次")
        for prot, cnt in Counter(all_proteins).items():
            if cnt >= 3:
                issues.append(f"蛋白质 '{prot}' 一天出现 {cnt} 次")

        return {"passed": len(issues) == 0, "issues": issues}


# ============================================================
# 格式化输出
# ============================================================

def format_meal_zh(dishes):
    return " ＋ ".join(d["name_cn"] for d in dishes)

def format_meal_en(dishes):
    return " + ".join(d["name_en"] for d in dishes)

def find_dish_id_by_name(dishes, name):
    name = name.strip()
    for d in dishes:
        if d["name_cn"] == name:
            return d["id"]
    for d in dishes:
        if name in d["name_cn"] or d["name_cn"] in name:
            return d["id"]
    return None

def parse_locked_items(dishes, locked_str):
    if not locked_str:
        return []
    names = [n.strip() for n in locked_str.split(",") if n.strip()]
    ids = []
    for name in names:
        did = find_dish_id_by_name(dishes, name)
        if did:
            ids.append(did)
        else:
            print(f"  [WARN] 未找到菜品: {name}")
    return ids

def get_dish_name(dishes, dish_id):
    for d in dishes:
        if d["id"] == dish_id:
            return d["name_cn"]
    return dish_id

def generate_menu_entry(day_number, gap_filler, locked=None, seed=None):
    """生成一天的菜单条目"""
    result, logs = gap_filler.generate_day(locked=locked, seed=seed)

    entry = {
        "day": day_number,
        "breakfast": {
            "zh": format_meal_zh(result["breakfast"]["dishes"]),
            "en": format_meal_en(result["breakfast"]["dishes"]),
        },
        "lunch": {
            "zh": format_meal_zh(result["lunch"]["dishes"]),
            "en": format_meal_en(result["lunch"]["dishes"]),
        },
        "afternoon_snack": {
            "zh": "无糖酸奶（生冷） ＋ 麦冬大麦茶",
            "en": "Sugar-free Yogurt (Cold) + Ophiopogon & Barley Tea",
        },
        "dinner": {
            "zh": format_meal_zh(result["dinner"]["dishes"]),
            "en": format_meal_en(result["dinner"]["dishes"]),
        },
        "late_night": {"zh": "无需安排", "en": "No arrangement needed"},
        "notes": {"zh": "", "en": ""},
    }

    # 自动备注
    notes = []
    # 凉拌菜检查：按烹饪方法 cold_mix 判断（不限类别）
    for mk in ["breakfast", "lunch", "dinner"]:
        for d in result[mk]["dishes"]:
            cms = d.get("cooking_methods", [])
            if "cold_mix" in cms and "warm_toss" not in cms:
                notes.append("凉拌菜属生冷")
                break
    # 生冷食物检查
    for d in result["breakfast"]["dishes"] + result["lunch"]["dishes"]:
        if d.get("is_fruit") or "生冷" in d.get("name_cn", ""):
            notes.append("含生冷食物")
            break
    # 温拌菜检查
    for mk in ["breakfast", "lunch", "dinner"]:
        for d in result[mk]["dishes"]:
            if d.get("cooking_methods") and "warm_toss" in d.get("cooking_methods", []):
                notes.append("温拌菜必须温热")
                break
        else:
            continue
        break

    if notes:
        unique_notes = list(dict.fromkeys(notes))  # 去重保序
        entry["notes"]["zh"] = "；".join(unique_notes)
        entry["notes"]["en"] = entry["notes"]["zh"]

    return entry, result, logs


# ============================================================
# 预览 HTML
# ============================================================

def generate_preview_html(entry, day_number, day_date, manifest):
    def get_photos(zh_str):
        photos = []
        seen = set()
        for name in zh_str.split(" ＋ "):
            name = name.strip()
            if " / " in name:
                name = name.split(" / ")[0].strip()
            if name in manifest:
                fname = manifest[name].get("file", "")
                if fname and fname not in seen:
                    photos.append(f"photos/{fname}")
                    seen.add(fname)
        return photos

    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_cn[day_date.weekday()]

    # 罗马数字转换
    roman_map = {1:"I",2:"II",3:"III",4:"IV",5:"V",6:"VI",7:"VII",8:"VIII",9:"IX",10:"X",
                 11:"XI",12:"XII",13:"XIII",14:"XIV",15:"XV",16:"XVI",17:"XVII",18:"XVIII",19:"XIX",20:"XX"}
    day_roman = roman_map.get(day_number, str(day_number))

    date_str = day_date.strftime("%Y.%m.%d")

    meals = [
        ("breakfast", "早餐", "Breakfast", "#E8A87C"),
        ("lunch", "午餐", "Lunch", "#C38D9E"),
        ("afternoon_snack", "下午茶", "Afternoon Snack", "#85B79D"),
        ("dinner", "晚餐", "Dinner", "#7C9EB2"),
    ]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>家庭菜单 Day {day_number} | {day_date.strftime('%m月%d日')}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #f0ede8; padding: 24px 16px; color: #2c2c2c; line-height: 1.7;
}}
.container {{
    max-width: 480px; margin: 0 auto; background: #fffdf9;
    border-radius: 6px; overflow: hidden;
    box-shadow: 0 4px 24px rgba(0,0,0,0.08);
}}

/* ---- 杂志风格页头 ---- */
.header {{
    text-align: center; padding: 40px 24px 32px;
    border-bottom: 1px solid #e8e2d8;
}}
.header .main-title {{
    font-family: 'Georgia', 'Songti SC', 'STSong', serif;
    font-size: 32px; font-weight: 700; color: #1a1a1a;
    letter-spacing: 8px; margin-bottom: 8px;
}}
.header .sub-title {{
    font-family: 'Georgia', 'Times New Roman', serif;
    font-size: 15px; font-style: italic; color: #999;
    letter-spacing: 2px; margin-bottom: 16px;
}}
.header .date-line {{
    font-size: 13px; color: #888; letter-spacing: 1px;
    margin-bottom: 20px;
}}
.header .divider {{
    display: flex; align-items: center; justify-content: center; gap: 6px;
}}
.header .divider .line {{
    width: 60px; height: 1px; background: #d4cec3;
}}
.header .divider .dot {{
    width: 5px; height: 5px; border-radius: 50%; display: inline-block;
}}
.header .divider .dot:nth-child(2) {{ background: #E8A87C; }}
.header .divider .dot:nth-child(4) {{ background: #C38D9E; }}
.header .divider .dot:nth-child(6) {{ background: #85B79D; }}

/* ---- 餐次区块 ---- */
.meal {{
    padding: 24px 24px; border-bottom: 1px solid #f0ebe3;
}}
.meal:last-of-type {{ border-bottom: none; }}
.meal-header {{
    display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
}}
.meal-header .bar {{
    width: 4px; height: 20px; border-radius: 2px;
}}
.meal-header .meal-zh {{
    font-size: 17px; font-weight: 600; color: #333;
}}
.meal-header .meal-en {{
    font-size: 12px; color: #bbb; font-style: italic; margin-left: auto;
}}
.photos {{
    display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px;
}}
.photos img {{
    width: 96px; height: 96px; border-radius: 6px; object-fit: cover;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}}
.dish-zh {{
    font-size: 15px; font-weight: 500; color: #2c2c2c; margin-bottom: 3px;
    line-height: 1.5;
}}
.dish-en {{
    font-size: 12px; color: #aaa; font-style: italic; line-height: 1.4;
}}

/* ---- 备注与页脚 ---- */
.notes {{
    background: #faf6ee; padding: 14px 24px; font-size: 12px; color: #9c8b6f;
    border-top: 1px solid #f0ebe3;
}}
.footer {{
    text-align: center; padding: 20px; font-size: 11px; color: #ccc;
    background: #fffdf9;
}}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div class="main-title">家庭菜单</div>
        <div class="sub-title">Family Table &middot; Day {day_roman}</div>
        <div class="date-line">{date_str} {weekday}</div>
        <div class="divider">
            <span class="line"></span>
            <span class="dot"></span>
            <span class="line"></span>
            <span class="dot"></span>
            <span class="line"></span>
            <span class="dot"></span>
            <span class="line"></span>
        </div>
    </div>
"""
    for meal_key, zh_label, en_label, bar_color in meals:
        meal = entry.get(meal_key, {})
        zh = meal.get("zh", "")
        en = meal.get("en", "")
        html += f'    <div class="meal">\n'
        html += f'        <div class="meal-header">\n'
        html += f'            <span class="bar" style="background:{bar_color}"></span>\n'
        html += f'            <span class="meal-zh">{zh_label}</span>\n'
        html += f'            <span class="meal-en">{en_label}</span>\n'
        html += f'        </div>\n'
        if not zh or zh == "无需安排":
            html += '        <div class="dish-zh">无需安排</div>\n'
        else:
            photos = get_photos(zh)
            if photos:
                html += '        <div class="photos">'
                for p in photos:
                    html += f'<img src="{p}" loading="lazy">'
                html += '</div>\n'
            html += f'        <div class="dish-zh">{zh}</div>\n'
            if en:
                html += f'        <div class="dish-en">{en}</div>\n'
        html += '    </div>\n'

    notes_zh = entry.get("notes", {}).get("zh", "")
    if notes_zh and notes_zh != "无":
        html += f'    <div class="notes">📌 {notes_zh}</div>\n'
    html += '    <div class="footer">由家庭菜单管家 AI 缺口补充法生成 · Generated by Gap-Filler Engine</div>\n'
    html += '</div>\n</body>\n</html>'
    return html


# ============================================================
# 主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="家庭菜单 AI 缺口补充法配餐引擎")
    parser.add_argument("--day", type=int, default=None, help="生成第几天的菜单")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    parser.add_argument("--locked-breakfast", type=str, default="", help="锁定早餐菜品（逗号分隔菜名）")
    parser.add_argument("--locked-lunch", type=str, default="", help="锁定午餐菜品")
    parser.add_argument("--locked-dinner", type=str, default="", help="锁定晚餐菜品")
    parser.add_argument("--preview", action="store_true", help="生成 HTML 预览")
    parser.add_argument("--update-menu", action="store_true", help="更新 menu_data.json")
    parser.add_argument("--verbose", action="store_true", help="显示详细配餐日志")
    args = parser.parse_args()

    dish_pool = load_dish_pool()
    manifest = load_photo_manifest()

    # 确定日期
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    elif args.day:
        try:
            menu_data = load_json(MENU_DATA_FILE)
            cycle_start = datetime.strptime(menu_data.get("cycle_start", "2026-07-28"), "%Y-%m-%d").date()
            target_date = cycle_start + timedelta(days=args.day - 1)
        except Exception:
            target_date = date.today()
    else:
        target_date = date.today()

    # 计算天数
    try:
        menu_data = load_json(MENU_DATA_FILE)
        cycle_start = datetime.strptime(menu_data.get("cycle_start", "2026-07-28"), "%Y-%m-%d").date()
        total_days = menu_data.get("total_days", 20)
        day_number = args.day or ((target_date - cycle_start).days % total_days) + 1
    except Exception:
        day_number = args.day or 1

    print("=" * 60)
    print(f"家庭菜单 AI 配餐引擎 — 缺口补充法")
    print(f"目标: Day {day_number} | {target_date}")
    print(f"数据库: {len(dish_pool['dishes'])} 道菜")
    print("=" * 60)

    # 解析锁定菜品
    locked = {}
    if args.locked_breakfast:
        locked["breakfast"] = parse_locked_items(dish_pool["dishes"], args.locked_breakfast)
        names = [get_dish_name(dish_pool["dishes"], i) for i in locked["breakfast"]]
        print(f"锁定早餐: {names}")
    if args.locked_lunch:
        locked["lunch"] = parse_locked_items(dish_pool["dishes"], args.locked_lunch)
        names = [get_dish_name(dish_pool["dishes"], i) for i in locked["lunch"]]
        print(f"锁定午餐: {names}")
    if args.locked_dinner:
        locked["dinner"] = parse_locked_items(dish_pool["dishes"], args.locked_dinner)
        names = [get_dish_name(dish_pool["dishes"], i) for i in locked["dinner"]]
        print(f"锁定晚餐: {names}")

    # 生成菜单
    gap_filler = GapFiller(dish_pool, seed=args.seed)
    entry, result, logs = generate_menu_entry(
        day_number, gap_filler,
        locked=locked if locked else None,
        seed=args.seed,
    )

    # 打印结果
    print(f"\n{'='*60}")
    print(f"Day {day_number} 菜单")
    print(f"{'='*60}")

    for meal_key, meal_label in [("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐")]:
        print(f"\n{'─'*40}")
        print(f"  {meal_label}")
        print(f"{'─'*40}")
        if args.verbose and meal_key in logs:
            for line in logs[meal_key]:
                print(line)

        dishes = result[meal_key]["dishes"]
        state = result[meal_key]["state"]
        for d in dishes:
            p = ",".join(d["proteins"]) if d["proteins"] else "—"
            v = ",".join(d["vegetables"]) if d["vegetables"] else "—"
            c = d["carb_type"] or "—"
            cm = ",".join(d["cooking_methods"]) if d["cooking_methods"] else "—"
            print(f"  • {d['name_cn']}")
            print(f"    蛋白质:{p} | 蔬菜:{v} | 主食:{c} | 烹饪:{cm}")

        print(f"\n  📊 营养统计:")
        print(f"    蛋白质: {list(state.proteins)} ({state.protein_count}道)")
        print(f"    蔬菜: {list(state.vegetables)} ({state.vegetable_count}种)")
        print(f"    主食: {list(state.carb_types)} ({state.carb_count}道)")
        print(f"    汤:{'✓' if state.has_soup else '✗'} 水果:{'✓' if state.has_fruit else '✗'}")
        print(f"    烹饪: {state.cooking_methods}")
        print(f"    口味: {state.tastes}")
        print(f"    总数: {state.dish_count}道")

    # Final Review
    print(f"\n{'='*60}")
    print("FINAL REVIEW")
    print(f"{'='*60}")
    review = logs.get("review", {})
    if review.get("passed"):
        print("✅ 审查通过！菜单符合所有规则。")
    else:
        print("⚠️ 审查发现问题:")
        for issue in review.get("issues", []):
            print(f"  • {issue}")

    # JSON 输出
    print(f"\n{'='*60}")
    print("菜单输出 (menu_data.json 格式)")
    print(f"{'='*60}")
    print(json.dumps(entry, ensure_ascii=False, indent=2))

    # 预览
    if args.preview:
        html = generate_preview_html(entry, day_number, target_date, manifest)
        preview_file = f"preview_day{day_number}.html"
        with open(preview_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n预览文件已生成: {preview_file}")

    # 更新 menu_data.json
    if args.update_menu:
        menu_data = load_json(MENU_DATA_FILE)
        if day_number <= len(menu_data["menu"]):
            menu_data["menu"][day_number - 1] = entry
            with open(MENU_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(menu_data, f, ensure_ascii=False, indent=2)
            print(f"\n✅ menu_data.json Day {day_number} 已更新")
        else:
            print(f"\n⚠️ Day {day_number} 超出菜单范围 ({len(menu_data['menu'])} 天)")


if __name__ == "__main__":
    main()
