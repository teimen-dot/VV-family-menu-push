# 项目固定规则

- 本地正式项目：`/Users/vv/WorkBuddy/Claw`。
- 生产服务器：腾讯云 Lighthouse 香港，部署目录 `/opt/family-menu`。
- 正式域名：`menu.ourmenu.site`（家庭端）、`admin.ourmenu.site`（管理端）。
- `vivian` 为 Owner，`kitchen` 为 Worker。
- `kitchen` 不得确认、AI Fill、Replace 或写菜单；写接口必须拒绝。
- 不得使用 mock 或 localStorage 保存业务数据；SQLite 是正式业务数据源。
- 不得整文件覆盖 `app.py` 或 `photo_manager.py`。
- 未明确要求时不得部署、替换数据库或开启 PushPlus。
- PUSH 定时任务默认关闭。
- 小任务只读取相关文件，不扫描无关目录。
- 完成后只回复：修改文件、测试结果、未完成问题。
