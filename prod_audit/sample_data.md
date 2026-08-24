# Production Database Inventory

查询方式：对 `/opt/family-menu/data/family_menu.db` 使用 SQLite `mode=ro`；未复制数据库，未执行写事务。样本只保留业务结构所需字段，成员姓名、饮食健康信息、备注、事件详情、配置值、推送错误和 message ID 均已删除或替换。

## 关键关系

```text
categories 1 ── N dishes
dishes N ── N ingredients          via dish_ingredients(required)
menus 1 ── N menu_items            每道菜一行，但 status 在 menus 整单级
menus 1 ── N menu_meal_settings    每餐 diners/note/is_skipped
menus 1 ── N push_logs             按 confirmed revision 幂等
menus 1 ── N menu_item_replace_history

current_pantry N ── 1 ingredients  逻辑关联，按 location 隔离
inventory 1 ── N inventory_items   legacy 每日快照
inventory_snapshots                current pantry JSON 审计快照
purchase_requests N ── 1 ingredients
diners 1 ── N dietary_alerts       该功能已 deprecated
```

正式库没有 `meal_plans` 表。现有等价结构是 `menus + menu_items + menu_meal_settings`，但 `menus.status` 是整单状态。

## 行数统计

| 表 | 行数 |
|---|---:|
| categories | 8 |
| config | 20 |
| consumed_history | 41 |
| current_pantry | 78 |
| custom_tags_def | 3 |
| dietary_alerts | 2 |
| diners | 5 |
| dish_ingredients | 410 |
| dish_preference_stats | 45 |
| dishes | 223 |
| dishes_legacy | 203 |
| events | 1,906 |
| ingredients | 157 |
| inventory | 3 |
| inventory_items | 23 |
| inventory_snapshots | 58 |
| menu_item_replace_history | 236 |
| menu_items | 202 |
| menu_meal_settings | 3 |
| menus | 20 |
| pantry_usage_stats | 78 |
| purchase_requests | 0 |
| push_logs | 5 |
| selections | 0 |

补充统计：

```text
dishes active / inactive                         213 / 10
active dishes 无 required ingredients             7
dishes 至少有一条 required ingredient             210（含 inactive）
current pantry active: Shenzhen / Hong Kong       47 / 4
menus: Shenzhen / Hong Kong                       20 / 0
menu_items 可关联真实 dish / legacy 整餐文本       153 / 49
menus status: confirmed / draft / pushed          1 / 17 / 2
```

## 枚举实况

### categories

```text
protein_main, egg_tofu, vegetable_mushroom, soup,
staple_carb, cold_dish, one_pot_meal, fruit_snack
```

### menu item meal_type

```text
breakfast 74, lunch 51, afternoon_snack 18, dinner 59
```

### pantry status

```text
available 76, expiring 2
```

`current_pantry` 的 78 行包括 inactive 历史条目；active 共 51 行。

## 脱敏样本

### categories

```json
{"id":"protein_main","label_cn":"蛋白质 / 主菜","label_en":"Protein / Main","sort_order":1,"active":1}
{"id":"egg_tofu","label_cn":"蛋类 / 豆制品","label_en":"Egg / Tofu","sort_order":2,"active":1}
```

### dishes

```json
{"id":"dish_0001","name_cn":"黑鱼子酱配豆腐","name_en":"Black Caviar with Silken Tofu","category_id":"egg_tofu","meal_tags":["breakfast"],"banquet":0,"quick_soup":0,"slow_soup":0,"manual_only_for_breakfast":0,"is_active":1,"image":"black_caviar_with_silken_tofu.jpg"}
{"id":"dish_0002","name_cn":"三文鱼籽配嫩豆腐","name_en":"Salmon Roe with Silken Tofu","category_id":"protein_main","meal_tags":["breakfast"],"is_active":1,"image":"salmon_roe_with_silken_tofu.jpg"}
```

### ingredients

```json
{"ingredient_id":"16谷米","name_cn":"16谷米","name_en":"16-Grain Rice","category":"grain","ingredient_group":"staple_coarse","is_common":0,"translation_pending":0}
{"ingredient_id":"arugula","name_cn":"芝麻菜","name_en":"Arugula","category":"vegetable_mushroom","ingredient_group":"vegetable_mushroom","is_common":0,"translation_pending":0}
```

### dish_ingredients

```json
{"id":3,"dish_id":"dish_0002","ingredient_id":"三文鱼籽","required":1}
{"id":4,"dish_id":"dish_0002","ingredient_id":"silken_tofu","required":1}
```

### custom_tags_def

```json
{"id":1,"label":"[自定义标签 1]"}
{"id":2,"label":"[自定义标签 2]"}
{"id":3,"label":"[自定义标签 3]"}
```

### inventory

```json
{"id":4,"location":"shenzhen","date":"2026-07-30","status":"submitted"}
{"id":6,"location":"hongkong","date":"2026-07-30","status":"submitted"}
```

### inventory_items

```json
{"id":40,"inventory_id":6,"ingredient_id":"酸奶","status":"available","quantity_level":"enough"}
{"id":41,"inventory_id":6,"ingredient_id":"shrimp","status":"available","quantity_level":"enough"}
```

### menus

```json
{"id":1,"date":"2026-07-28","location":"shenzhen","status":"pushed","diners_count":4,"meal_mode":"daily","banquet_total_diners":null,"push_status":"not_sent"}
{"id":2,"date":"2026-07-29","location":"shenzhen","status":"pushed","diners_count":4,"meal_mode":"daily","banquet_total_diners":null,"push_status":"not_sent"}
```

### menu_items

```json
{"id":1,"menu_id":1,"dish_id":"[legacy 整餐文本，已截断]","meal_type":"breakfast","is_locked":0,"sort_order":0,"source":"ai"}
{"id":2,"menu_id":1,"dish_id":"[legacy 整餐文本，已截断]","meal_type":"lunch","is_locked":0,"sort_order":1,"source":"ai"}
```

### menu_meal_settings

```json
{"menu_id":10,"meal_type":"dinner","diner_count":3,"note":"[REDACTED]","is_skipped":0,"updated_at":"2026-08-06 08:46:11"}
{"menu_id":11,"meal_type":"breakfast","diner_count":null,"note":"","is_skipped":0,"updated_at":"2026-08-06 08:58:50"}
```

### menu_item_replace_history

```json
{"id":1,"menu_id":10,"menu_item_id":2958,"dish_id":"dish_0228","replaced_at":"2026-08-06 08:58:55"}
{"id":2,"menu_id":10,"menu_item_id":2958,"dish_id":"dish_0041","replaced_at":"2026-08-06 08:58:57"}
```

### selections

```text
0 行；无样本。
```

### purchase_requests

```text
0 行；无样本。
```

### diners

```json
{"id":"diner_1","role":"owner","default_attends":1,"sort_order":0}
{"id":"diner_2","role":"owner","default_attends":1,"sort_order":1}
{"id":"diner_3","role":"family","default_attends":1,"sort_order":2}
```

### dietary_alerts

过敏原、备注和真实 diner ID 属于健康/个人信息，全部删除：

```json
{"id":1,"diner_id":"diner_1","severity":"avoid"}
{"id":2,"diner_id":"diner_2","severity":"avoid"}
```

### events

`entity_id` 与 `details` 已删除：

```json
{"id":1906,"event_type":"pantry_item_removed","entity_type":"current_pantry","created_at":"2026-08-09 22:04:42"}
{"id":1905,"event_type":"dish_replaced","entity_type":"menu_item","created_at":"2026-08-09 12:55:59"}
```

### config

所有值均统一遮蔽，不判断其敏感程度：

```json
{"key":"afternoon_tea_in_push","value":"[REDACTED]"}
{"key":"auto_fallback_time","value":"[REDACTED]"}
{"key":"catalog_version","value":"[REDACTED]"}
```

### current_pantry

```json
{"id":1,"location":"hongkong","ingredient_id":"酸奶","status":"available","is_active":1}
{"id":2,"location":"hongkong","ingredient_id":"shrimp","status":"available","is_active":1}
```

### inventory_snapshots

`items_json` 未导出，只保留数组长度：

```json
{"id":1,"location":"shenzhen","item_count":2,"created_at":"2026-07-30T18:45:53.488791"}
{"id":2,"location":"shenzhen","item_count":3,"created_at":"2026-07-30T18:50:48.276843"}
```

### dishes_legacy

```json
{"id":"dish_0001","name_cn":"黑鱼子酱配嫩豆腐","name_en":"Black Caviar with Silken Tofu","category_id":"protein_main","meal_tags":["breakfast","lunch","dinner"],"is_active":1}
{"id":"dish_0002","name_cn":"三文鱼籽配嫩豆腐","name_en":"Salmon Roe with Silken Tofu","category_id":"protein_main","meal_tags":["breakfast","lunch","dinner"],"is_active":1}
```

### dish_preference_stats

```json
{"dish_id":"dish_0001","vv_confirm_count":23,"vv_confirm_count_30d":10,"last_confirmed_at":"2026-08-07T17:17:20.811592","last_selected_at":"2026-08-07T17:17:20.811592"}
{"dish_id":"dish_0002","vv_confirm_count":2,"vv_confirm_count_30d":1,"last_confirmed_at":"2026-07-31 10:34:34","last_selected_at":null}
```

### push_logs

错误文本、触发人、message ID 与返回内容均未导出：

```json
{"id":1,"menu_id":10,"date":"2026-08-06","location":"shenzhen","channel":"pushplus","status":"success","attempt_count":1}
{"id":2,"menu_id":10,"date":"2026-08-06","location":"shenzhen","channel":"pushplus","status":"success","attempt_count":1}
```

### consumed_history

`consumed_by` 已删除：

```json
{"id":1,"location":"shenzhen","ingredient_id":"沙拉菜","consumed_at":"2026-08-03 14:58:01","source":"legacy_soft_delete"}
{"id":2,"location":"shenzhen","ingredient_id":"小米","consumed_at":"2026-07-31 03:32:20","source":"legacy_soft_delete"}
```

### pantry_usage_stats

```json
{"location":"hongkong","ingredient_id":"black_tiger_shrimp","add_count":1,"last_action_at":"2026-07-30 10:44:25"}
{"location":"hongkong","ingredient_id":"fish","add_count":1,"last_action_at":"2026-08-05T12:08:35.792017"}
```
