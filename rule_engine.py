#!/usr/bin/env python3
"""
家庭菜单管家 - 规则引擎 + 评分引擎 (Rule Engine + Scoring Engine) V3

核心设计原则（V3 更新）：
  1. Rule Engine = 确定性规则（Python，可测试、可重复、可解释）
  2. Scoring Engine = 软偏好评分（库存优先、历史去重、口味多样性等）
  3. 满足规则立即 STOP，不强制补菜
  4. LOCKED 菜品只计算一次营养贡献
  5. Final Review 只生成 Warning，不阻断 Confirm (V3 变更)
  6. VV 是唯一最终确认人 (V3 新增)
  7. 早餐新增 tofu slot + egg slot (V3 新增)
  8. 午餐新增 quick_soup slot (V3 新增)
  9. 晚餐新增 slow_soup slot (V3 新增)
  10. 组合菜可同时满足多个槽位 (V3 新增)

LLM 不负责决定规则是否合格，只负责食材语义理解、文案生成等。
"""

import json
import random
from datetime import date, datetime, timedelta
from collections import Counter
from db import get_db

# ============================================================
# 常量
# ============================================================

# 非蔬菜类食材（主食/粗粮/薯类，不能算蔬菜）
NOT_VEGETABLES = {
    "红薯", "番薯", "地瓜", "粗粮", "玉米", "淮山", "山药", "土豆",
    "南瓜", "芋头",
    "火腿",  # 是蛋白质不是蔬菜
}

# 装饰性配菜/调味料（不计算为一份蔬菜）
GARNISH = {
    "葱花", "葱", "香菜", "姜", "蒜", "蒜蓉", "辣椒", "枸杞",
    "少量辣椒",
}

# 蔬菜同义词归一化（后者 → 前者）
VEGETABLE_SYNONYMS = {
    "西红柿": "番茄",
    "小番茄": "番茄",
    "小白菜": "白菜",
    "苋菜": "红苋菜",
}

# 烹饪方法归一化
COOKING_METHOD_NORMALIZE = {
    "stir_fried": "stir_fry",
    "pan_fried": "pan_fry",
    "cold_mixed": "cold_mix",
    "roasted": "roast",
    "warm_tossed": "warm_toss",
}

# 弱主食类型（不应作为唯一主食）
WEAK_CARB_TYPES = {"other", "dim_sum"}

# 早餐搭配主食四选一
BREAKFAST_COMPANION_STAPLES = {"mantou", "jiaozi", "bao", "sourdough", "huajuan"}

MEAT_PROTEINS = {"fish", "shrimp", "other_seafood", "beef", "pork", "猪肉", "chicken"}
MANUAL_SOURCES = {"manual", "owner"}
TANG_JIAO_NAME = "汤饺"
NO_CANDIDATE_MESSAGE = "暂无符合条件菜品，请手动选择或补录"

# REQUIREMENTS_V2 §4.6: a four-day window needs at least daily demand × 4.
# These thresholds are used only to decide whether the four-day lock may degrade.
AUTO_POOL_MINIMUMS = {
    "porridge": 4,
    "companion_staple": 4,
    "egg": 4,
    "tofu": 4,
    "vegetable": 24,
    "vegetable_dish": 24,
    "coarse_grain": 4,
    "protein_main": 20,
    "meat_main": 20,
    "staple": 8,
    "quick_soup": 4,
    "slow_soup": 4,
}

MEAL_SLOT_ORDER = {
    "breakfast": [
        "porridge", "companion_staple", "tofu", "egg",
        "vegetable", "coarse_grain",
    ],
    "lunch": ["meat_main", "protein_main", "vegetable_dish", "staple", "quick_soup"],
    "dinner": ["meat_main", "protein_main", "vegetable_dish", "staple", "slow_soup"],
}

# 原 6 项 pending_review 是冻结保护对象；旧库尚无独立 pending_review 列时
# 仍按稳定 dish id 排除自动池，迁移脚本不得改其业务字段或 required。
PROTECTED_PENDING_DISH_IDS = {
    "dish_0097", "dish_0122", "dish_0183",
    "dish_0188", "dish_0189", "dish_0202",
}


def is_manual_source(source):
    return (source or "").lower() in MANUAL_SOURCES


def is_auto_candidate(analysis, meal_type):
    """Frozen automatic-pool filter, including the breakfast 汤饺 exception."""
    if analysis.get("banquet"):
        return False
    if analysis.get("id") in PROTECTED_PENDING_DISH_IDS:
        return False
    if analysis.get("pending_review"):
        return False
    if analysis.get("drink"):
        return False
    if analysis.get("ingredients_pending"):
        return False
    if meal_type == "breakfast" and analysis.get("manual_only_breakfast"):
        return False
    if "one_pot_meal" in analysis.get("meal_roles", []):
        return meal_type == "breakfast" and analysis.get("name_cn") == TANG_JIAO_NAME
    return True


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
        """过滤掉非蔬菜和装饰性配菜，归一化同义词"""
        real = []
        for v in (veg_list or []):
            v = v.strip()
            if not v:
                continue
            if v in NOT_VEGETABLES:
                continue
            if v in GARNISH:
                continue
            # 同义词归一化
            v = VEGETABLE_SYNONYMS.get(v, v)
            if v not in real:
                real.append(v)
        return real

    @staticmethod
    def filter_proteins(protein_list):
        return [p for p in (protein_list or []) if p and p != "none"]

    @staticmethod
    def analyze(dish):
        """分析一道菜的营养贡献"""
        cat = dish.get("category_id", "")
        proteins = NutritionAnalyzer.filter_proteins(dish.get("protein_types", []))
        vegetables = NutritionAnalyzer.filter_real_vegetables(dish.get("vegetables", []))
        carb_type = dish.get("carb_type")
        breakfast_staple_type = dish.get("breakfast_staple_type")
        name_cn = dish.get("name_cn", "")
        is_soup = (cat == "soup")
        is_fruit = (cat == "fruit_snack")
        cooking_methods = NutritionAnalyzer.normalize_cooking_methods(
            dish.get("cooking_methods", [])
        )

        # V3: 检测豆腐/鸡蛋/快手汤/煲汤
        has_tofu = (
            "tofu" in proteins
            or "豆腐" in name_cn
            or "豆干" in name_cn
        )
        has_egg = (
            "egg" in proteins
            or "蛋" in name_cn
        )
        # quick_soup / slow_soup 从 dish 字段读取（SQLite 或 JSON）
        is_quick_soup = bool(dish.get("quick_soup", 0) if isinstance(dish.get("quick_soup", 0), int) else dish.get("quick_soup", False))
        is_slow_soup = bool(dish.get("slow_soup", 0) if isinstance(dish.get("slow_soup", 0), int) else dish.get("slow_soup", False))
        # manual_only_for_breakfast
        manual_only_breakfast = bool(dish.get("manual_only_for_breakfast", 0) if isinstance(dish.get("manual_only_for_breakfast", 0), int) else dish.get("manual_only_for_breakfast", False))

        # 业务池只读取显式 meal_roles；不再按分类或中文名猜槽位。
        meal_roles = dish.get("meal_roles", [])
        if isinstance(meal_roles, str):
            try:
                meal_roles = json.loads(meal_roles)
            except (json.JSONDecodeError, TypeError):
                meal_roles = []
        is_quick_soup = "quick_soup" in meal_roles
        is_slow_soup = "slow_soup" in meal_roles
        return {
            "proteins": proteins,
            "vegetables": vegetables,
            "carb_type": carb_type,
            "breakfast_staple_type": breakfast_staple_type,
            "is_soup": is_soup,
            "is_fruit": is_fruit,
            "cooking_methods": cooking_methods,
            "taste": dish.get("taste", "normal"),
            "banquet": dish.get("banquet", False),
            "drink": dish.get("drink"),
            "ingredients_pending": bool(dish.get("ingredients_pending", False)),
            "pending_review": dish.get("pending_review"),
            "custom_tags": dish.get("custom_tags", []),
            "name_cn": name_cn,
            "name_en": dish.get("name_en", ""),
            "id": dish.get("id", ""),
            "category_id": cat,
            "meal_tags": dish.get("meal_tags", []),
            # V3 新增字段
            "has_tofu": has_tofu,
            "has_egg": has_egg,
            "is_quick_soup": is_quick_soup,
            "is_slow_soup": is_slow_soup,
            "manual_only_breakfast": manual_only_breakfast,
            "meal_roles": meal_roles,
        }

# ============================================================
# 营养状态（修复 LOCKED 双重计数）
# ============================================================

class MealState:
    """跟踪一餐中已选菜品的累计营养状态。
    LOCKED 菜品通过 add_dish() 加入后，只从 state 读取，不重复计算。
    """

    def __init__(self):
        self.dishes = []
        self.proteins = set()
        self.vegetables = set()
        self.carb_types = set()
        self.breakfast_staple_types = set()
        self.has_soup = False
        self.has_fruit = False
        self.cooking_methods = []
        self.tastes = []
        self.protein_count = 0
        self.vegetable_dish_count = 0
        self.carb_count = 0
        self.locked_count = 0  # LOCKED 菜品数量
        # V3 新增追踪
        self.has_tofu = False
        self.has_egg = False
        self.has_quick_soup = False
        self.has_slow_soup = False
        # V9: 基于 meal_roles 的精确槽位计数（不依赖 ingredients）
        self.egg_dish_count = 0
        self.auto_egg_dish_count = 0
        self.tofu_dish_count = 0
        self.meat_main_count = 0
        # 只有手动加入的 onepot 才覆盖整餐；自动早餐汤饺不得触发。
        self.has_manual_one_pot_meal = False

    def add_dish(self, analysis, is_locked=False, source="ai"):
        """添加一道菜到状态中。无论 locked 还是 AI 选的，只计算一次。"""
        self.dishes.append(analysis)
        self.proteins.update(analysis["proteins"])
        self.vegetables.update(analysis["vegetables"])
        if analysis["carb_type"]:
            self.carb_types.add(analysis["carb_type"])
        if analysis.get("breakfast_staple_type"):
            self.breakfast_staple_types.add(analysis["breakfast_staple_type"])
        if analysis["is_soup"]:
            self.has_soup = True
        if analysis["is_fruit"]:
            self.has_fruit = True
        for cm in analysis["cooking_methods"]:
            if cm not in self.cooking_methods:
                self.cooking_methods.append(cm)
        self.tastes.append(analysis["taste"])

        # V3: 追踪豆腐/鸡蛋/快手汤/煲汤
        if analysis.get("has_tofu"):
            self.has_tofu = True
        if analysis.get("has_egg"):
            self.has_egg = True
        if analysis.get("is_quick_soup"):
            self.has_quick_soup = True
        if analysis.get("is_slow_soup"):
            self.has_slow_soup = True

        cat = analysis["category_id"]
        roles = analysis.get("meal_roles", [])

        # 所有业务槽只读取显式 role。
        if "vegetable_dish" in roles:
            self.vegetable_dish_count += 1
        if "protein_main" in roles:
            self.protein_count += 1
        if "staple" in roles:
            self.carb_count += 1
        # V9: egg_dish / tofu_dish 精确槽位（基于 meal_roles，不依赖 ingredients）
        if "egg_dish" in roles:
            self.egg_dish_count += 1
            if not is_manual_source(source):
                self.auto_egg_dish_count += 1
        if "tofu_dish" in roles:
            self.tofu_dish_count += 1
        if "tofu_dish" not in roles and set(analysis["proteins"]) & MEAT_PROTEINS:
            self.meat_main_count += 1
        if "one_pot_meal" in roles and is_manual_source(source):
            self.has_manual_one_pot_meal = True

        if is_locked:
            self.locked_count += 1

    @property
    def vegetable_count(self):
        return len(self.vegetables)

    @property
    def dish_count(self):
        return len(self.dishes)

    @property
    def has_strong_carb(self):
        return bool(self.carb_types - WEAK_CARB_TYPES)

    @property
    def has_porridge(self):
        return "porridge" in self.carb_types

    @property
    def has_coarse_grain(self):
        return "coarse_grain" in self.carb_types

    @property
    def has_companion_staple(self):
        return bool(self.breakfast_staple_types & BREAKFAST_COMPANION_STAPLES)

    @property
    def porridge_slot(self):
        return 1 if self.has_porridge else 0

    @property
    def companion_staple_slot(self):
        return 1 if self.has_companion_staple else 0

    @property
    def coarse_grain_slot(self):
        return 1 if self.has_coarse_grain else 0

    # V3 新增槽位属性
    # V9: egg_slot / tofu_slot 改为基于 meal_roles 的精确计数
    @property
    def tofu_slot(self):
        return self.tofu_dish_count

    @property
    def egg_slot(self):
        return self.egg_dish_count

    @property
    def quick_soup_slot(self):
        return 1 if self.has_quick_soup else 0

    @property
    def slow_soup_slot(self):
        return 1 if self.has_slow_soup else 0


# ============================================================
# RULE ENGINE — 确定性硬规则
# ============================================================

class RuleEngine:
    """
    硬规则引擎：确定性判断，不交给 LLM。
    负责判断一餐是否合格、Final Review 是否通过。
    """

    @staticmethod
    def check_breakfast_rules(state):
        """
        V3 早餐规则（全部为 Warning，不阻断 Confirm）：
          1. porridge_slot == 1
          2. companion_staple_slot == 1 (馒头/饺子/酸种面包/花卷)
          3. coarse_grain_slot >= 1
          4. protein >= 1
          5. vegetable_types >= 2
          6. egg_slot >= 1 (V3 新增)
          7. tofu_slot >= 1 (V3 新增)
        返回: (passed, issues, warnings)
        V3: 所有问题都返回为 warnings，不再有 hard_errors
        """
        warnings = []

        if state.has_manual_one_pot_meal:
            return True, [], []
        if state.porridge_slot < 1:
            warnings.append("早餐缺粥 / No porridge")
        if state.companion_staple_slot < 1:
            warnings.append("早餐缺搭配主食 / No companion staple (mantou/jiaozi/bao/sourdough/huajuan)")
        if state.coarse_grain_slot < 1:
            warnings.append("早餐缺粗粮 / No coarse grain")
        if state.vegetable_dish_count < 2:
            warnings.append(f"早餐蔬菜不足: {state.vegetable_dish_count}/2 / Insufficient vegetable dishes ({state.vegetable_dish_count}/2)")
        # V3 新增
        if state.egg_slot < 1:
            warnings.append("早餐还没有鸡蛋 / No egg for breakfast")
        if state.tofu_slot < 1:
            warnings.append("早餐还没有豆腐 / No tofu for breakfast")
        if state.auto_egg_dish_count > 1:
            warnings.append(f"早餐自动蛋类过多: {state.auto_egg_dish_count}/1 / Too many automatic egg dishes")

        # 早餐不应有米饭/炒饭
        if "rice" in state.carb_types:
            warnings.append("早餐不应有米饭/炒饭 / Rice should not appear in breakfast")

        # V3: 全部为 warning，passed 始终为 True
        return True, [], warnings

    @staticmethod
    def check_lunch_rules(state, diners_count=4):
        """
        V3 午餐规则（全部为 Warning，不阻断 Confirm）：
          protein >= 1
          vegetable >= 1
          carb == 1
          quick_soup >= 1 (V3 新增)
        返回: (passed, issues, warnings)
        """
        warnings = []

        if state.has_manual_one_pot_meal:
            return True, [], []
        target = RuleEngine._meal_target("lunch", diners_count)
        if state.protein_count < target["protein_main"]:
            warnings.append(f"午餐蛋白质不足: {state.protein_count}/{target['protein_main']} / Insufficient protein")
        if state.vegetable_dish_count < target["vegetable_dish"]:
            warnings.append(f"午餐蔬菜不足: {state.vegetable_dish_count}/{target['vegetable_dish']} / Insufficient vegetables")
        if state.carb_count < 1:
            warnings.append("午餐缺主食 / No carb for lunch")
        if state.meat_main_count < 1:
            warnings.append("午餐缺独立肉类菜 / No separate meat or seafood dish for lunch")
        # V3 新增
        if state.quick_soup_slot < 1:
            warnings.append("午餐还没有快手汤 / No quick soup for lunch")

        # 午餐不应过于丰富
        if state.dish_count > target["total"]:
            warnings.append(f"午餐菜品过多: {state.dish_count}/{target['total']} / Too many dishes")

        return True, [], warnings

    @staticmethod
    def check_dinner_rules(state, diners_count=4):
        """
        V9 晚餐规则（全部为 Warning，不阻断 Confirm）：
          蛋白质主菜: 按人数 (2人=1, 3人=2, 4人=2)
          独立蔬菜菜品: 按人数 (2人=1, 3人=1, 4人=2)
          主食: 1
          slow_soup >= 1
        返回: (passed, issues, warnings)
        """
        warnings = []
        target = RuleEngine._meal_target("dinner", diners_count)

        if state.has_manual_one_pot_meal:
            return True, [], []
        if state.dish_count < 3:
            warnings.append(f"晚餐菜品不足: {state.dish_count}道 / Insufficient dishes ({state.dish_count}, need >=3)")
        if state.protein_count < target["protein_main"]:
            warnings.append(f"晚餐蛋白质不足: {state.protein_count}/{target['protein_main']} / Insufficient protein ({state.protein_count}/{target['protein_main']}, diners={diners_count})")
        if state.meat_main_count < 1:
            warnings.append("晚餐缺独立肉类菜 / No separate meat or seafood dish for dinner")
        if state.vegetable_dish_count < target["vegetable_dish"]:
            warnings.append(f"晚餐蔬菜不足: {state.vegetable_dish_count}/{target['vegetable_dish']} / Insufficient vegetable dishes ({state.vegetable_dish_count}/{target['vegetable_dish']}, diners={diners_count})")
        if state.carb_count < 1:
            warnings.append("晚餐缺主食 / No carb for dinner")
        if state.carb_count > 1:
            warnings.append(f"晚餐主食过多: {state.carb_count}道 / Too many carbs ({state.carb_count})")
        if state.carb_types and not state.has_strong_carb:
            warnings.append(f"晚餐主食偏弱 / Weak carb: {list(state.carb_types)}")

        # V3 新增
        if state.slow_soup_slot < 1:
            warnings.append("晚餐还没有煲汤 / No slow-cooked soup for dinner")
        if state.dish_count > target["total"]:
            warnings.append(f"晚餐菜品过多: {state.dish_count}/{target['total']} / Too many dishes")

        # 烹饪方式应多样
        if len(state.cooking_methods) < 2 and state.dish_count >= 3:
            warnings.append(f"晚餐烹饪方式较单一 / Limited cooking methods: {state.cooking_methods}")

        # 辣味较多
        spicy_count = state.tastes.count("spicy")
        if spicy_count > 1:
            warnings.append(f"晚餐辣味较多: {spicy_count}道 / Too many spicy dishes ({spicy_count})")

        return True, [], warnings

    @staticmethod
    def check_meal(meal_type, state, diners_count=4):
        """检查单餐是否合格。返回: (passed, hard_errors, warnings)"""
        if meal_type == "breakfast":
            return RuleEngine.check_breakfast_rules(state)
        elif meal_type == "lunch":
            return RuleEngine.check_lunch_rules(state, diners_count)
        elif meal_type == "dinner":
            return RuleEngine.check_dinner_rules(state, diners_count)
        return True, [], []

    @staticmethod
    def final_review(day_result, diners_count=4):
        """
        V3 最终审查：所有检查项均为 Warning，不阻断 Confirm。
        V9: diners_count 用于晚餐精确人数目标。
        返回:
          {
            passed: True (V3: 始终 True，不再阻断),
            hard_errors: [] (V3: 始终为空),
            warnings: [...],      # 所有提示
            issues: [...]          # = warnings (backward compat)
          }
        """
        warnings = []

        for meal_type in ["breakfast", "lunch", "dinner"]:
            state = day_result.get(meal_type, {}).get("state")
            if not state:
                warnings.append(f"{meal_type} 未生成 / {meal_type} not generated")
                continue

            passed, meal_hard, meal_warnings = RuleEngine.check_meal(meal_type, state, diners_count)
            # V3: meal_hard 始终为空，但保留兼容
            warnings.extend(meal_hard)
            warnings.extend(meal_warnings)

        # 跨餐检查（重复出现 = 警告，不阻止）
        all_vegs = []
        all_proteins = []
        for mk in ["breakfast", "lunch", "dinner"]:
            ms = day_result.get(mk, {}).get("state")
            if ms:
                all_vegs.extend(ms.vegetables)
                all_proteins.extend(ms.proteins)

        for veg, cnt in Counter(all_vegs).items():
            if cnt >= 3:
                warnings.append(f"食材 '{veg}' 一天出现 {cnt} 次 / '{veg}' appears {cnt} times today")
        for prot, cnt in Counter(all_proteins).items():
            if cnt >= 3:
                warnings.append(f"蛋白质 '{prot}' 一天出现 {cnt} 次 / Protein '{prot}' appears {cnt} times today")

        auto_egg_total = sum(
            day_result.get(meal, {}).get("state").auto_egg_dish_count
            for meal in ("breakfast", "lunch", "dinner")
            if day_result.get(meal, {}).get("state")
        )
        if auto_egg_total > 2:
            warnings.append(f"自动蛋类全天过多: {auto_egg_total}/2 / Too many automatic egg dishes today")

        return {
            "passed": True,  # V3: 始终 True
            "hard_errors": [],  # V3: 始终为空
            "warnings": warnings,
            "issues": warnings,  # backward compat
        }

    @staticmethod
    def is_satisfied(meal_type, state, diners_count=4):
        """
        判断硬规则是否已满足（用于决定是否 STOP）。
        V3: 包含 tofu/egg/quick_soup/slow_soup 新槽位。
        V9: 早餐 egg_slot/tofu_slot 基于 meal_roles（非 ingredients）；
             晚餐按 2/3/4 人精确定量。
        V11.1: 一餐型料理在场 → 蛋白质/蔬菜/主食视为已满足（汤仍需检查）。
        满足 → STOP，不再加菜。
        不满足 → 继续补缺口。
        """
        if meal_type == "breakfast":
            if state.has_manual_one_pot_meal:
                return True
            return (
                state.porridge_slot >= 1
                and state.companion_staple_slot >= 1
                and state.coarse_grain_slot >= 1
                and state.vegetable_dish_count >= 2
                and state.egg_slot >= 1
                and state.tofu_slot >= 1
            )
        elif meal_type == "lunch":
            if state.has_manual_one_pot_meal:
                return True
            target = RuleEngine._meal_target("lunch", diners_count)
            return (
                state.protein_count >= target["protein_main"]
                and state.meat_main_count >= 1
                and state.vegetable_dish_count >= target["vegetable_dish"]
                and state.carb_count >= 1
                and state.quick_soup_slot >= 1
            )
        elif meal_type == "dinner":
            # V9: 按人数精确定量
            target = RuleEngine._meal_target("dinner", diners_count)
            if state.has_manual_one_pot_meal:
                return True
            return (
                state.protein_count >= target["protein_main"]
                and state.meat_main_count >= 1
                and state.vegetable_dish_count >= target["vegetable_dish"]
                and state.carb_count >= target["staple"]
                and state.slow_soup_slot >= target["slow_soup"]
            )
        return True

    @staticmethod
    def _dinner_target(diners_count):
        """V9: 晚餐按人数确定精确目标。
        2人: 1蛋白+1蔬菜+1主食+1煲汤
        3人: 2蛋白+1蔬菜+1主食+1煲汤
        4人: 2蛋白+2蔬菜+1主食+1煲汤
        1人或5+人: 使用 fallback（同4人）
        """
        target = RuleEngine._meal_target("dinner", diners_count)
        return {key: value for key, value in target.items() if key != "total"}

    @staticmethod
    def _meal_target(meal_type, diners_count):
        """Frozen lunch/dinner matrix; 4+ diners stays at six dishes."""
        if diners_count <= 2:
            protein, vegetable = 1, 1
        elif diners_count == 3:
            protein, vegetable = 2, 1
        else:
            protein, vegetable = 2, 2
        soup_key = "quick_soup" if meal_type == "lunch" else "slow_soup"
        return {
            "protein_main": protein,
            "meat_main": 1,
            "vegetable_dish": vegetable,
            "staple": 1,
            soup_key: 1,
            "total": protein + vegetable + 2,
        }


# ============================================================
# V8: MEAL SLOT ANALYZER — 槽位分析器
# ============================================================

def analyze_meal_slots(meal_type, state, diners_count=4):
    """
    V8: 分析一餐中各槽位的当前值和缺口。
    返回 {slot: {current, target_min, missing_min}}。
    missing_min > 0 的槽位需要 AI Fill 补齐。
    """
    if meal_type == "dinner":
        # V9: 按 2/3/4 人精确定量
        target = RuleEngine._meal_target("dinner", diners_count)
        target = {key: value for key, value in target.items() if key != "total"}
        current = {
            "protein_main": state.protein_count,
            "meat_main": state.meat_main_count,
            "vegetable_dish": state.vegetable_dish_count,
            "staple": state.carb_count,
            "slow_soup": state.slow_soup_slot,
        }
    elif meal_type == "lunch":
        target = RuleEngine._meal_target("lunch", diners_count)
        target = {key: value for key, value in target.items() if key != "total"}
        current = {
            "protein_main": state.protein_count,
            "meat_main": state.meat_main_count,
            "vegetable_dish": state.vegetable_dish_count,
            "staple": state.carb_count,
            "quick_soup": state.quick_soup_slot,
        }
    elif meal_type == "breakfast":
        target = {
            "porridge": 1, "companion_staple": 1, "coarse_grain": 1,
            "vegetable": 2, "egg": 1, "tofu": 1
        }
        current = {
            "porridge": state.porridge_slot,
            "companion_staple": state.companion_staple_slot,
            "coarse_grain": state.coarse_grain_slot,
            "vegetable": state.vegetable_dish_count,
            # V9: egg/tofu 基于 meal_roles 而非 ingredients
            "egg": state.egg_dish_count,
            "tofu": state.tofu_dish_count,
        }
    else:
        return {}

    result = {}
    for slot, tgt in target.items():
        cur = current.get(slot, 0)
        result[slot] = {
            "current": cur,
            "target_min": tgt,
            "missing_min": max(0, tgt - cur),
            "excess": max(0, cur - tgt),  # V11: for reconcile
        }

    # 手动 onepot 覆盖整餐；自动早餐汤饺不进入此分支。
    if state.has_manual_one_pot_meal:
        for slot in result:
            result[slot]["missing_min"] = 0
            result[slot]["current"] = max(result[slot]["current"], result[slot]["target_min"])

    return result


# 槽位 → 候选菜过滤条件映射
SLOT_ROLE_MAP = {
    "protein_main": {
        "roles": ["protein_main"],
        "exclude_names": ["肉末"],
    },
    "meat_main": {
        "roles": ["protein_main"],
        "require_meat": True,
        "exclude_roles": ["tofu_dish"],
    },
    "vegetable_dish": {
        "roles": ["vegetable_dish"],
    },
    "staple": {
        "roles": ["staple"],
        "categories": ["staple_carb"],
        "exclude_categories": ["one_pot_meal"],
    },
    "slow_soup": {
        "roles": ["slow_soup"],
    },
    "quick_soup": {
        "roles": ["quick_soup"],
    },
    "egg": {
        "roles": ["egg_dish"],
    },
    "tofu": {
        "roles": ["tofu_dish"],
    },
    "porridge": {
        "require_carb_type": "porridge",
    },
    "companion_staple": {
        "require_breakfast_staple": True,
    },
    "coarse_grain": {
        "require_carb_type": "coarse_grain",
    },
    # V9: 早餐蔬菜食材种类缺口（与 vegetable_dish 不同：这是食材种类数，不是菜品数）
    "vegetable": {
        "roles": ["vegetable_dish"],
        "require_vegetables": True,
    },
}


def filter_candidates_for_slot(candidates, slot_name):
    """V8: 根据槽位名称过滤候选菜。"""
    spec = SLOT_ROLE_MAP.get(slot_name)
    if not spec:
        return candidates

    filtered = []
    for c in candidates:
        roles = c.get("meal_roles", [])
        name = c.get("name_cn", "")

        # 角色匹配
        role_match = any(r in roles for r in spec.get("roles", []))
        # 排除分类
        if spec.get("exclude_categories") and c.get("category_id") in spec["exclude_categories"]:
            continue
        if any(role in roles for role in spec.get("exclude_roles", [])):
            continue
        # 排除菜名
        if any(ex in name for ex in spec.get("exclude_names", [])):
            continue
        if spec.get("require_meat") and not (set(c.get("proteins", [])) & MEAT_PROTEINS):
            continue
        # 要求 carb_type
        if spec.get("require_carb_type") and c.get("carb_type") != spec["require_carb_type"]:
            continue
        # 要求早餐搭配主食
        if spec.get("require_breakfast_staple"):
            from_rule = c.get("breakfast_staple_type") in BREAKFAST_COMPANION_STAPLES
            if not from_rule:
                continue
        # 要求有蔬菜
        if spec.get("require_vegetables") and not c.get("vegetables"):
            continue

        if role_match or spec.get("require_carb_type") or spec.get("require_breakfast_staple"):
            filtered.append(c)

    return filtered


def choose_rotation_candidate(candidates, scorer, state, meal_type, context=None):
    """Choose LRU-first; soft score is only a tie-break within the same last-used day."""
    ctx = context or {}
    last_used = ctx.get("historical_last_used", {})
    ranked = []
    for candidate in candidates:
        used = last_used.get(candidate["id"])
        soft_score = scorer.score_dish(candidate, state, meal_type, ctx)
        ranked.append((
            1 if used else 0,  # never used first
            used or "",
            -soft_score,
            candidate["id"],
            candidate,
        ))
    ranked.sort(key=lambda item: item[:-1])
    return ranked[0][-1] if ranked else None

class ScoringEngine:
    """
    软偏好评分引擎。
    规则引擎判断合格/不合格，评分引擎在合格候选中选最优。
    """

    # 评分权重（可调）
    W_INVENTORY_MATCH = 30      # 库存中已有食材
    W_PRIORITY_USE = 40         # priority_use 食材
    W_EXPIRING = 60             # 快过期食材
    W_SAME_MEAL_PROTEIN = -20   # 同餐相同蛋白质重复
    W_SAME_DAY_PROTEIN = -15    # 同一天蛋白质过度重复
    W_COOKING_REPEAT = -10      # 烹饪方式重复
    W_BOSS_FAVORITE = 20        # 老板常选
    W_ONE_POT_DINNER = -200     # 一餐型料理在晚餐
    W_ONE_POT_BREAKFAST = -120  # 一餐型料理在早餐
    W_ONE_POT_LUNCH = -25       # 一餐型料理在午餐
    W_BREAKFAST_RICE = -40      # 早餐米饭惩罚
    # V11: VV Preference (added via context.vv_preferences dict)
    W_VV_PREFERENCE = 40         # max bonus from VV confirm-based preference

    def __init__(self, rng=None):
        self.rng = rng or random.Random()

    def score_dish(self, analysis, state, meal_type, context=None):
        """
        给一道菜打分。
        context: dict with optional keys:
          - day_proteins: set of proteins used in other meals today
          - inventory_ingredients: set of ingredient_ids available
          - priority_ingredients: set of ingredient_ids priority_use
          - expiring_ingredients: set of ingredient_ids expiring
          - boss_favorites: set of dish_ids boss frequently picks
        """
        ctx = context or {}
        day_proteins = ctx.get("day_proteins", set())
        inv_ings = ctx.get("inventory_ingredients", set())
        pri_ings = ctx.get("priority_ingredients", set())
        exp_ings = ctx.get("expiring_ingredients", set())
        boss_fav = ctx.get("boss_favorites", set())
        dish_ings = ctx.get("dish_ingredients", {}).get(analysis["id"], set())

        score = 0

        # === 一餐型料理降分 ===
        if analysis["category_id"] == "one_pot_meal":
            if meal_type == "dinner":
                score += self.W_ONE_POT_DINNER
            elif meal_type == "breakfast":
                score += self.W_ONE_POT_BREAKFAST
            else:
                score += self.W_ONE_POT_LUNCH

        # === 库存评分 ===
        if dish_ings:
            matched = dish_ings & inv_ings
            if matched:
                score += self.W_INVENTORY_MATCH
            if matched & pri_ings:
                score += self.W_PRIORITY_USE
            if matched & exp_ings:
                score += self.W_EXPIRING

        dish_id = analysis["id"]

        # === 同餐蛋白质重复 ===
        same_protein = len(set(analysis["proteins"]) & state.proteins)
        if same_protein > 0:
            is_protein_dish = "protein_main" in analysis.get("meal_roles", [])
            if is_protein_dish:
                score += same_protein * self.W_SAME_MEAL_PROTEIN
            else:
                score += same_protein * (self.W_SAME_MEAL_PROTEIN // 2)

        # === 跨餐蛋白质去重 ===
        if analysis["proteins"]:
            cross_overlap = len(set(analysis["proteins"]) & day_proteins)
            score += cross_overlap * self.W_SAME_DAY_PROTEIN

        # === 蛋白质缺口加分 ===
        current_protein_types = set(state.proteins)
        new_proteins = set(analysis["proteins"]) - current_protein_types
        is_protein_dish = "protein_main" in analysis.get("meal_roles", [])

        if not current_protein_types and analysis["proteins"]:
            score += 40
        elif new_proteins and len(current_protein_types) < 2:
            score += 25
        elif is_protein_dish and state.protein_count >= 2:
            score -= 35

        # === 蔬菜缺口 ===
        new_vegs = set(analysis["vegetables"]) - state.vegetables
        if new_vegs:
            score += 25 * min(len(new_vegs), 3)

        # === 主食缺口 ===
        if state.carb_count < 1 and analysis["carb_type"]:
            score += 25
            if analysis["carb_type"] in WEAK_CARB_TYPES:
                score -= 10
        elif state.carb_count >= 1 and analysis["carb_type"]:
            score -= 50

        # 早餐米饭惩罚
        if meal_type == "breakfast" and analysis["carb_type"] == "rice":
            score += self.W_BREAKFAST_RICE
        # 早餐粗粮加分
        if meal_type == "breakfast" and analysis["carb_type"] == "coarse_grain":
            if not state.has_coarse_grain:
                score += 20
        # 早餐粥加分
        if meal_type == "breakfast" and analysis["carb_type"] == "porridge":
            if not state.has_porridge:
                score += 20
        # 早餐搭配主食加分
        if meal_type == "breakfast" and analysis.get("breakfast_staple_type"):
            if not state.has_companion_staple:
                score += 20

        # V9: 早餐鸡蛋缺口加分 — 基于 meal_roles (egg_dish)，不依赖 ingredients
        if meal_type == "breakfast" and "egg_dish" in analysis.get("meal_roles", []):
            if state.egg_dish_count < 1:
                score += 25

        # V9: 早餐豆腐缺口加分 — 基于 meal_roles (tofu_dish)
        if meal_type == "breakfast" and "tofu_dish" in analysis.get("meal_roles", []):
            if state.tofu_dish_count < 1:
                score += 25

        # V3: 早餐排除 manual_only_breakfast
        if meal_type == "breakfast" and analysis.get("manual_only_breakfast"):
            score -= 60

        # === 汤的处理 (V3: 区分 quick_soup / slow_soup) ===
        if analysis["is_soup"]:
            if meal_type == "dinner":
                # V3: 晚餐优先 slow_soup
                if analysis.get("is_slow_soup") and not state.has_slow_soup:
                    score += 25
                    if analysis["proteins"]:
                        score += 8
                    if analysis["vegetables"]:
                        score += 8
                elif not state.has_soup:
                    # 没有标记 slow_soup 的汤也加分，但不如 slow_soup
                    score += 10
                    if analysis["proteins"]:
                        score += 4
                else:
                    score -= 20
            elif meal_type == "lunch":
                # V3: 午餐优先 quick_soup
                if analysis.get("is_quick_soup") and not state.has_quick_soup:
                    score += 25
                    if analysis["proteins"]:
                        score += 5
                    if analysis["vegetables"]:
                        score += 5
                elif not state.has_soup:
                    score += 5
                else:
                    score -= 20
            else:
                score -= 10

        # === 烹饪方式多样性 ===
        new_methods = [m for m in analysis["cooking_methods"] if m not in state.cooking_methods]
        score += len(new_methods) * 4

        # === 口味平衡 ===
        if analysis["taste"] == "spicy":
            spicy_count = state.tastes.count("spicy")
            score -= spicy_count * 15

        # === 午餐偏好明确蛋白质主菜 ===
        if meal_type == "lunch" and is_protein_dish:
            score += 25
        if meal_type == "lunch" and analysis["category_id"] == "vegetable_mushroom" and analysis["proteins"]:
            score -= 25

        # === 老板常选 ===
        if dish_id in boss_fav:
            score += self.W_BOSS_FAVORITE

        # V11: VV Preference — confirm-based ranking (only affects soft score, never breaks hard filters)
        vv_prefs = ctx.get("vv_preferences", {})
        if dish_id in vv_prefs:
            pref_score = vv_prefs[dish_id]
            score += min(pref_score, self.W_VV_PREFERENCE)

        # === 随机扰动 ===
        score += self.rng.uniform(0, 6)

        return score


# ============================================================
# GAP FILLER — 缺口补充法引擎（重构版）
# ============================================================

class GapFiller:
    """
    AI 缺口补充法配餐引擎（v2.0 重构版）。
    使用 RuleEngine 判断硬规则，ScoringEngine 评分选菜。
    满足硬规则立即 STOP，不强制补菜。
    """

    def __init__(self, dish_pool, seed=None, dish_ingredients=None):
        self.dishes = dish_pool.get("dishes", [])
        self.rng = random.Random(seed)
        self.analyzed = {}
        for d in self.dishes:
            self.analyzed[d["id"]] = NutritionAnalyzer.analyze(d)
        self.scorer = ScoringEngine(rng=self.rng)
        self.degradation_warnings = []
        # dish_id → set of ingredient_ids
        self.dish_ingredients = dish_ingredients or {}

    def get_candidates(self, meal_type, exclude_ids=None, context=None):
        ctx = context or {}
        exclude = set(exclude_ids or set()) | set(ctx.get("hard_locked_dish_ids", set()))
        availability = ctx.get("dish_availability", {})
        return [
            a for a in self.analyzed.values()
            if a["id"] not in exclude and meal_type in a["meal_tags"]
            and is_auto_candidate(a, meal_type)
            and (not availability or availability.get(a["id"]) == "available")
        ]

    def _business_pool(self, meal_type, context=None):
        """Return the eligible automatic pool before rotation and availability."""
        rows = []
        for analysis in self.analyzed.values():
            if meal_type not in analysis["meal_tags"]:
                continue
            if not is_auto_candidate(analysis, meal_type):
                continue
            if (meal_type == "breakfast" and analysis["is_soup"]
                    and analysis["name_cn"] != TANG_JIAO_NAME):
                continue
            rows.append(analysis)
        return rows

    @staticmethod
    def _candidate_within_caps(candidate, state, day_auto_egg_count):
        roles = candidate.get("meal_roles", [])
        if candidate["id"] in {dish["id"] for dish in state.dishes}:
            return False
        if "egg_dish" in roles:
            if state.egg_dish_count >= 1 or day_auto_egg_count >= 2:
                return False
        if "tofu_dish" in roles and state.tofu_dish_count >= 1:
            return False
        return True

    def get_slot_candidates(self, meal_type, slot_name, state, context=None,
                            exclude_ids=None, day_auto_egg_count=0):
        """Apply business pool, inventory, four-day lock, and explicit degradation."""
        ctx = context or {}
        availability = ctx.get("dish_availability", {})
        business_slot = filter_candidates_for_slot(
            self._business_pool(meal_type, ctx), slot_name
        )
        business_slot = [
            candidate for candidate in business_slot
            if self._candidate_within_caps(
                candidate, state, day_auto_egg_count + state.auto_egg_dish_count
            )
        ]
        available_slot = [
            candidate for candidate in business_slot
            if not availability or availability.get(candidate["id"]) == "available"
        ]
        blocked = set(exclude_ids or set()) | set(ctx.get("hard_locked_dish_ids", set()))
        unlocked = [candidate for candidate in available_slot if candidate["id"] not in blocked]
        if unlocked:
            return unlocked, None

        minimum = AUTO_POOL_MINIMUMS.get(slot_name)
        if minimum is not None and len(business_slot) < minimum and available_slot:
            message = (
                f"{meal_type}.{slot_name} 分类菜品不足 "
                f"({len(business_slot)}/{minimum})，允许窗口内重复；建议补录"
            )
            if message not in self.degradation_warnings:
                self.degradation_warnings.append(message)
            return available_slot, message
        return [], None

    def generate_meal(self, meal_type, locked_dish_ids=None, context=None, diners_count=4):
        """
        生成一餐菜单。
        V9: diners_count 用于晚餐 is_satisfied 精确判断。
        返回: (dishes, state, log)
        """
        locked_ids = locked_dish_ids or []
        ctx = context or {}
        state = MealState()
        log = []

        for did in locked_ids:
            if did in self.analyzed:
                analysis = self.analyzed[did]
                state.add_dish(analysis, is_locked=True, source="owner")
                log.append(f"  [LOCKED] {analysis['name_cn']}")

        # A manually selected onepot owns the whole meal. The automatic breakfast
        # 汤饺 exception never sets this state flag.
        if state.has_manual_one_pot_meal:
            return state.dishes, state, [*log, "  [STOP] manual onepot covers meal"]

        day_history = set(ctx.get("day_history", set()))
        exclude = set(locked_ids) | day_history
        day_auto_egg_count = int(ctx.get("day_auto_egg_count", 0) or 0)

        for slot_name in MEAL_SLOT_ORDER[meal_type]:
            while analyze_meal_slots(meal_type, state, diners_count).get(
                slot_name, {}
            ).get("missing_min", 0) > 0:
                candidates, degraded = self.get_slot_candidates(
                    meal_type, slot_name, state, ctx,
                    exclude_ids=exclude,
                    day_auto_egg_count=day_auto_egg_count,
                )
                if not candidates:
                    log.append(f"  [WARN] no candidate for {slot_name}")
                    break
                chosen = choose_rotation_candidate(
                    candidates, self.scorer, state, meal_type, ctx
                )
                if not chosen:
                    break
                state.add_dish(chosen, source="ai")
                exclude.add(chosen["id"])
                log.append(
                    f"  [ADD:{slot_name}] {chosen['name_cn']}"
                    + (" [DEGRADED]" if degraded else "")
                )

        log.append(
            f"  [FINAL] dishes={state.dish_count} | "
            f"proteins={list(state.proteins)} | "
            f"veg={list(state.vegetables)} ({state.vegetable_count}种) | "
            f"carb={list(state.carb_types)} | "
            f"soup={state.has_soup}"
        )
        return state.dishes, state, log

    def _filter_by_gaps(self, meal_type, state, candidates, log, diners_count=4):
        """根据缺口过滤候选菜 (V3: 含 tofu/egg/quick_soup/slow_soup)
        V10.1: diners_count 用于晚餐蛋白质精确定量"""

        # 早餐粥缺口
        if meal_type == "breakfast" and not state.has_porridge:
            filtered = [c for c in candidates if c["carb_type"] == "porridge"]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] porridge gap: {len(candidates)} candidates")

        # 早餐粗粮缺口
        if meal_type == "breakfast" and not state.has_coarse_grain:
            filtered = [c for c in candidates if c["carb_type"] == "coarse_grain"]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] coarse_grain gap: {len(candidates)} candidates")

        # 早餐搭配主食缺口
        if meal_type == "breakfast" and not state.has_companion_staple:
            filtered = [c for c in candidates
                        if c.get("breakfast_staple_type") in BREAKFAST_COMPANION_STAPLES]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] companion_staple gap: {len(candidates)} candidates")

        # V9: 早餐鸡蛋缺口 — 基于 meal_roles (egg_dish)，不依赖 ingredients
        if meal_type == "breakfast" and state.egg_dish_count < 1:
            filtered = [c for c in candidates if "egg_dish" in c.get("meal_roles", [])]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] egg_dish gap: {len(candidates)} candidates")

        # V9: 早餐豆腐缺口 — 基于 meal_roles (tofu_dish)
        if meal_type == "breakfast" and state.tofu_dish_count < 1:
            filtered = [c for c in candidates if "tofu_dish" in c.get("meal_roles", [])]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] tofu_dish gap: {len(candidates)} candidates")

        # V3: 早餐排除 manual_only_breakfast 菜品（豆浆等不由 AI 自动推荐）
        if meal_type == "breakfast":
            filtered = [c for c in candidates if not c.get("manual_only_breakfast")]
            if filtered:
                candidates = filtered

        # 午餐明确蛋白质缺口
        if meal_type == "lunch" and state.protein_count < 1:
            filtered = [
                c for c in candidates
                if "protein_main" in c.get("meal_roles", [])
                and "肉末" not in c["name_cn"]
            ]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] clear_protein gap: {len(candidates)} candidates")

        # V10.1: 晚餐明确蛋白质缺口 — 按人数精确定量
        # 只有当蔬菜已满足且蛋白质未满足时，才强制过滤为蛋白质菜品
        if meal_type == "dinner":
            dinner_target = RuleEngine._dinner_target(diners_count)
            if state.protein_count < dinner_target["protein_main"]:
                filtered = [
                    c for c in candidates
                    if "protein_main" in c.get("meal_roles", [])
                    and "肉末" not in c["name_cn"]
                ]
                if filtered:
                    candidates = filtered
                    log.append(f"  [FILTER] dinner protein gap ({state.protein_count}/{dinner_target['protein_main']}): {len(candidates)} candidates")

        # 午晚餐必须另有一道非豆腐的肉/鱼虾菜。
        if meal_type in ("lunch", "dinner") and state.meat_main_count < 1:
            filtered = [
                c for c in candidates
                if "protein_main" in c.get("meal_roles", [])
                and "tofu_dish" not in c.get("meal_roles", [])
                and set(c.get("proteins", [])) & MEAT_PROTEINS
            ]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] separate meat gap: {len(candidates)} candidates")

        # V3: 午餐快手汤缺口
        if meal_type == "lunch" and not state.has_quick_soup:
            filtered = [c for c in candidates if "quick_soup" in c.get("meal_roles", [])]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] quick_soup gap: {len(candidates)} candidates")

        # 晚餐强主食缺口（排除一餐型料理）
        if meal_type == "dinner" and state.carb_count < 1:
            strong_types = {"rice", "coarse_grain", "porridge", "noodle"}
            filtered = [
                c for c in candidates
                if c["carb_type"] in strong_types
                and c["category_id"] != "one_pot_meal"
            ]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] dinner carb gap: {len(candidates)} candidates")

        # V3: 晚餐煲汤缺口
        if meal_type == "dinner" and not state.has_slow_soup:
            filtered = [c for c in candidates if "slow_soup" in c.get("meal_roles", [])]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] slow_soup gap: {len(candidates)} candidates")

        # V8: 晚餐/午餐蔬菜菜缺口（独立蔬菜菜品，不是食材种类数）
        if meal_type in ("dinner", "lunch") and state.vegetable_dish_count < 1:
            filtered = [
                c for c in candidates
                if "vegetable_dish" in c.get("meal_roles", [])
            ]
            if filtered:
                candidates = filtered
                log.append(f"  [FILTER] vegetable_dish gap: {len(candidates)} candidates")

        # 蛋白质缺口（通用）
        if len(state.proteins) < 1 and state.protein_count < 1:
            filtered = [c for c in candidates if c["proteins"]]
            if filtered:
                candidates = filtered

        # 蔬菜食材种类缺口（仅早餐用，午晚餐用 vegetable_dish_count）
        if meal_type == "breakfast" and state.vegetable_count < 2:
            filtered = [c for c in candidates if c["vegetables"]]
            if filtered:
                candidates = filtered

        return candidates

    def generate_day(self, locked=None, context=None, diners_count=4):
        """生成一天三餐
        V10: diners_count 传递给 generate_meal，确保晚餐按人数精确生成。
        """
        locked = locked or {}
        ctx = context or {}
        all_logs = {}
        day_history = set()
        day_proteins = set()

        result = {}
        self.degradation_warnings = []
        day_auto_egg_count = 0
        for meal_type in ["breakfast", "lunch", "dinner"]:
            locked_ids = locked.get(meal_type, [])
            for lid in locked_ids:
                day_history.add(lid)

            meal_ctx = dict(ctx)
            meal_ctx["day_proteins"] = set(day_proteins)
            meal_ctx["day_history"] = set(day_history)  # 跨餐排除
            meal_ctx["day_auto_egg_count"] = day_auto_egg_count

            dishes, state, log = self.generate_meal(
                meal_type,
                locked_dish_ids=locked_ids,
                context=meal_ctx,
                diners_count=diners_count,
            )

            for d in dishes:
                day_history.add(d["id"])
                day_proteins.update(d["proteins"])
            day_auto_egg_count += state.auto_egg_dish_count

            result[meal_type] = {"dishes": dishes, "state": state}
            all_logs[meal_type] = log

        review = RuleEngine.final_review(result, diners_count)
        review["degradation_warnings"] = list(self.degradation_warnings)
        review["warnings"].extend(self.degradation_warnings)
        review["issues"] = list(review["warnings"])
        all_logs["review"] = review
        return result, all_logs


# ============================================================
# 轮换状态（只由历史最终 MealPlan + 当前有效菜单推导）
# ============================================================

def get_rotation_context(target_date, location, exclude_menu_id=None):
    """Return historical LRU data and the global four-day hard lock for one kitchen."""
    target = date.fromisoformat(target_date)
    conn = get_db()
    try:
        historical_rows = conn.execute(
            "SELECT mi.dish_id, MAX(m.date) AS last_used "
            "FROM menu_items mi JOIN menus m ON mi.menu_id=m.id "
            "WHERE m.location=? AND m.date<? AND m.status IN ('confirmed','pushed') "
            "GROUP BY mi.dish_id",
            (location, target.isoformat()),
        ).fetchall()
        historical_last_used = {
            row["dish_id"]: row["last_used"] for row in historical_rows if row["dish_id"]
        }

        params = [
            location,
            (target - timedelta(days=3)).isoformat(),
            (target + timedelta(days=3)).isoformat(),
        ]
        exclude_sql = ""
        if exclude_menu_id is not None:
            exclude_sql = " AND m.id<>?"
            params.append(exclude_menu_id)
        reservation_rows = conn.execute(
            "SELECT m.id AS menu_id, m.date, mi.dish_id "
            "FROM menu_items mi JOIN menus m ON mi.menu_id=m.id "
            "LEFT JOIN menu_meal_settings mms "
            "ON mms.menu_id=mi.menu_id AND mms.meal_type=mi.meal_type "
            "WHERE m.location=? AND m.date BETWEEN ? AND ? "
            "AND COALESCE(m.status,'draft')<>'cancelled' "
            "AND COALESCE(mms.is_skipped,0)=0" + exclude_sql,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    hard_locked = set()
    for dish_id, used_text in historical_last_used.items():
        delta = (target - date.fromisoformat(used_text)).days
        if 1 <= delta <= 3:
            hard_locked.add(dish_id)
    for row in reservation_rows:
        if not row["dish_id"]:
            continue
        delta = abs((target - date.fromisoformat(row["date"])).days)
        if delta <= 3:
            hard_locked.add(row["dish_id"])

    return {
        "historical_last_used": historical_last_used,
        "hard_locked_dish_ids": hard_locked,
    }


def get_history_3day(location="shenzhen"):
    """Compatibility helper for the frozen four-day hard-lock window."""
    return get_rotation_context(date.today().isoformat(), location)["hard_locked_dish_ids"]


# ============================================================
# 库存查询
# ============================================================

def get_inventory_ingredients(location, target_date=None):
    """获取指定地点最新库存的食材集合"""
    conn = get_db()
    if target_date:
        row = conn.execute(
            "SELECT id FROM inventory WHERE location = ? AND date <= ? AND status = 'submitted' "
            "ORDER BY date DESC LIMIT 1",
            (location, target_date)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM inventory WHERE location = ? AND status = 'submitted' "
            "ORDER BY date DESC LIMIT 1",
            (location,)
        ).fetchone()

    if not row:
        conn.close()
        return set(), set(), set()

    items = conn.execute(
        "SELECT ingredient_id, status FROM inventory_items WHERE inventory_id = ?",
        (row["id"],)
    ).fetchall()
    conn.close()

    available = set()
    priority = set()
    expiring = set()

    for item in items:
        if item["status"] == "available":
            available.add(item["ingredient_id"])
        elif item["status"] == "priority_use":
            priority.add(item["ingredient_id"])
        elif item["status"] == "expiring":
            expiring.add(item["ingredient_id"])

    available |= priority | expiring  # priority/expiring 也是 available
    return available, priority, expiring


def get_dish_ingredients_map():
    """获取所有菜品的食材映射 dish_id → set(ingredient_id)"""
    conn = get_db()
    rows = conn.execute(
        "SELECT dish_id, ingredient_id FROM dish_ingredients"
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        if r["dish_id"] not in result:
            result[r["dish_id"]] = set()
        result[r["dish_id"]].add(r["ingredient_id"])
    return result


# ============================================================
# 格式化输出
# ============================================================

def format_meal_zh(dishes):
    return " ＋ ".join(d["name_cn"] for d in dishes)

def format_meal_en(dishes):
    return " + ".join(d["name_en"] for d in dishes)


# ============================================================
# 下午茶生成
# ============================================================

def generate_afternoon_snack(pool, rng=None, context=None, exclude_ids=None):
    """Select up to two available snacks with the same hard lock and LRU policy."""
    ctx = context or {}
    excluded = set(exclude_ids or set()) | set(ctx.get("hard_locked_dish_ids", set()))
    availability = ctx.get("dish_availability", {})
    snacks = []
    for dish in pool["dishes"]:
        analysis = NutritionAnalyzer.analyze(dish)
        if (dish["category_id"] == "fruit_snack"
                and dish["id"] not in excluded
                and is_auto_candidate(analysis, "afternoon_snack")
                and (not availability or availability.get(dish["id"]) == "available")):
            snacks.append(dish)
    if not snacks:
        return []
    last_used = ctx.get("historical_last_used", {})
    snacks.sort(key=lambda dish: (
        1 if last_used.get(dish["id"]) else 0,
        last_used.get(dish["id"]) or "",
        dish["id"],
    ))
    return snacks[:2]


# ============================================================
# CLI 接口
# ============================================================

def generate_day_menu(pool, seed=None, locked=None, context=None, with_snack=True):
    """生成一天完整菜单（含下午茶）"""
    dish_ings = context.get("dish_ingredients") if context else None
    gf = GapFiller(pool, seed=seed, dish_ingredients=dish_ings)
    result, logs = gf.generate_day(locked=locked, context=context)

    if with_snack:
        rng = random.Random(seed + 1000 if seed else None)
        used_ids = {
            dish["id"] for meal in result.values()
            for dish in meal.get("dishes", [])
        }
        snacks = generate_afternoon_snack(
            pool, rng=rng, context=context, exclude_ids=used_ids
        )
        result["afternoon_snack"] = {"dishes": snacks, "state": None}

    return result, logs


def format_day_output(result, logs, day_num=1):
    """格式化输出文本"""
    lines = []
    lines.append(f"{'='*50}")
    lines.append(f"  DAY {day_num}")
    lines.append(f"{'='*50}")

    meal_names = {
        "breakfast": "早餐 BREAKFAST",
        "lunch": "午餐 LUNCH",
        "afternoon_snack": "下午茶 SNACK",
        "dinner": "晚餐 DINNER",
    }

    for mt in ["breakfast", "lunch", "afternoon_snack", "dinner"]:
        dishes = result.get(mt, {}).get("dishes", [])
        if not dishes:
            continue
        lines.append(f"\n  ▎{meal_names.get(mt, mt)}")
        lines.append(f"    ZH: {format_meal_zh(dishes)}")
        lines.append(f"    EN: {format_meal_en(dishes)}")

    review = logs.get("review", {})
    lines.append(f"\n{'='*50}")
    if review.get("passed"):
        if review.get("warnings"):
            lines.append("  ✅ FINAL REVIEW PASSED (with warnings)")
            for w in review.get("warnings", []):
                lines.append(f"    ⚠️ {w}")
        else:
            lines.append("  ✅ FINAL REVIEW PASSED")
    else:
        lines.append("  ❌ FINAL REVIEW FAILED:")
        for issue in review.get("issues", []):
            lines.append(f"    • {issue}")
    lines.append(f"{'='*50}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="家庭菜单管家 - 规则引擎")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--day", type=int, default=1, help="第几天")
    parser.add_argument("--locked-breakfast", type=str, default="", help="锁定早餐菜(逗号分隔)")
    parser.add_argument("--locked-lunch", type=str, default="", help="锁定午餐菜(逗号分隔)")
    parser.add_argument("--locked-dinner", type=str, default="", help="锁定晚餐菜(逗号分隔)")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    parser.add_argument("--no-snack", action="store_true", help="不生成下午茶")
    args = parser.parse_args()

    with open("dish_pool.json", "r", encoding="utf-8") as f:
        pool = json.load(f)

    dish_ings = get_dish_ingredients_map()
    target_date = (date.today() + timedelta(days=max(args.day - 1, 0))).isoformat()
    rotation = get_rotation_context(target_date, "shenzhen")
    inv_avail, inv_pri, inv_exp = get_inventory_ingredients("shenzhen")

    context = {
        **rotation,
        "inventory_ingredients": inv_avail,
        "priority_ingredients": inv_pri,
        "expiring_ingredients": inv_exp,
        "dish_ingredients": dish_ings,
    }

    # 解析 locked（按菜名查找 ID）
    locked = {}
    name_to_id = {d["name_cn"]: d["id"] for d in pool["dishes"]}
    for meal, arg_key in [("breakfast", "locked_breakfast"),
                           ("lunch", "locked_lunch"),
                           ("dinner", "locked_dinner")]:
        names_str = getattr(args, arg_key)
        if names_str:
            ids = []
            for name in names_str.split(","):
                name = name.strip()
                if name in name_to_id:
                    ids.append(name_to_id[name])
                else:
                    print(f"⚠️ 未找到菜品: {name}")
            if ids:
                locked[meal] = ids

    result, logs = generate_day_menu(
        pool, seed=args.seed, locked=locked,
        context=context, with_snack=not args.no_snack
    )

    if args.verbose:
        for mt in ["breakfast", "lunch", "dinner"]:
            print(f"\n{'='*40}")
            print(f"  {mt.upper()}")
            print(f"{'='*40}")
            for line in logs.get(mt, []):
                print(line)

    print()
    print(format_day_output(result, logs, day_num=args.day))
