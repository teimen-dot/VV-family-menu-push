# Test Results

测试对象：从生产 `/opt/family-menu/app` 流式取得并与 `e9b64ec` 逐文件确认一致的代码快照。  
执行位置：本机临时目录。  
约束：只运行使用内存或临时数据库、不会访问/修改正式服务的现有测试；未新建测试框架或测试用例。

## 结果摘要

```text
总计             76
PASS             74
FAIL              1
ERROR             1
生产数据库写入     0
生产服务请求写入   0
```

| 模块 | 结果 |
|---|---:|
| `test_acceptance_round_two` | 0 pass / 1 error |
| `test_core_menu_integration` | 8 pass |
| `test_hotfix_production_regression` | 7 pass |
| `test_meal_plan_extension` | 4 pass |
| `test_requested_five_fixes` | 5 pass |
| `test_requested_three_fixes` | 3 pass |
| `test_requested_two_fixes` | 4 pass |
| `test_task_a` | 14 pass |
| `test_task_b` | 11 pass |
| `test_task_c221` | 9 pass / 1 fail |
| `test_task_current_real_page` | 5 pass |
| `test_tomorrow_search_mobile` | 4 pass |

## 失败/错误说明

### ERROR: `test_acceptance_round_two`

```text
FileNotFoundError: family_menu_test.db
```

该测试依赖 `family_menu_test.db` baseline fixture，但此文件没有部署在生产代码目录，也不在生产 commit 的可用文件中。测试在 setup 阶段即退出，未运行断言。`test_candidate_filtering.py` 依赖同一缺失 fixture，因此未重复运行。

结论：测试包不完整，不是已证实的业务失败。

### FAIL: `test_task_c221.test_nginx_overwrites_authenticated_user_header`

旧测试要求示例配置包含：

```nginx
proxy_set_header X-Authenticated-User $remote_user;
```

生产提交和服务器实际配置现在均为：

```nginx
proxy_set_header X-Authenticated-User "";
```

这是鉴权架构从 Family Nginx Basic Auth 切换到 `app.py` 登录/session 后的预期实现，仍能覆盖浏览器伪造 header。测试断言未随架构更新，属于过期测试，而非当前生产配置漂移。

## 未运行的现有脚本

| 脚本 | 原因 |
|---|---|
| `test_candidate_filtering.py` | 依赖未部署的 `family_menu_test.db` fixture。 |
| `test_v10_blackbox.py` | 写菜单、库存、偏好并含工作电脑绝对路径；不是隔离测试。 |
| `test_v11_blackbox.py` | 直接写配置数据库、菜单和偏好；未提供独立 DB fixture。 |
| `v5_blackbox_test.py` | 直接请求 `localhost:8090` 并包含写 API；与当前 session auth 也不匹配。 |
| `v6_blackbox_test.py` | 同上，属于旧版 live blackbox。 |

## 生产运行状态（只读核验）

```text
family-menu-app.service      active/running
family-menu-admin.service    active/running
127.0.0.1:8090               app.py PID 2415808
127.0.0.1:8080               photo_manager.py PID 2415809
PUSH_ENABLED                  false
PUSH_SCHEDULE_ENABLED         false
reminder 19/20 timers         not found / inactive
```

本次没有执行登录后的线上写操作、没有发送 PushPlus、没有重启服务。
