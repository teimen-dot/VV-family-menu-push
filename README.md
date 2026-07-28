# 🍽️ 家庭菜单管家 | Family Menu Manager

每日自动推送家庭菜单到微信，支持中英双语、时令建议、菜谱数据库管理。

## 功能
- ⏰ 每天定时推送当天菜单（默认北京时间10:30）
- 🌐 中英双语（中文给主人/保姆，英文给主人/菲佣）
- 🔄 菜单周期循环，支持任意天数
- 📚 菜谱数据库（dish_pool.json），随时更新扩充
- 🌿 按月时令饮食建议
- ☁️ GitHub Actions 云端运行，电脑关机也能推送

## 文件说明
| 文件 | 用途 |
|------|------|
| `menu_data.json` | 当前推送周期菜单（含 cycle_start 和 total_days） |
| `dish_pool.json` | 菜谱数据库（112道菜 + 10款粥底 + 18款鸡蛋做法） |
| `config.json` | 推送配置（时间/地点/语言） |
| `seasonal_tips.json` | 按月时令饮食建议 |
| `push_menu.py` | 推送脚本 |
| `.github/workflows/daily-push.yml` | GitHub Actions 定时任务 |

## 配置
1. 在仓库 Settings → Secrets → Actions 添加：
   - `PUSHPLUS_TOKEN`：PushPlus 个人 Token
   - `PUSHPLUS_TOPIC`：PushPlus 群组编码（默认 home-menu）

2. 修改推送时间：编辑 `.github/workflows/daily-push.yml` 中的 cron 表达式

3. 更新菜单：替换 `menu_data.json`，设置新的 `cycle_start`

## 技术栈
- Python 3（仅内置库）
- GitHub Actions
- PushPlus 群组推送
