# Changelog

> 最低 Home Assistant 版本：**2024.12.0**（声明于 `hacs.json`）

## [4.0.0] - 2026-09-13

> ⚠️ 破坏性升级：domain 从 `hotata_airer` 改为 `hotata`，仓库更名 `ha-hotata`，无法自动迁移，需删除后重装。

- **传输层**：改用官方 App 的阿里云 IoT 网关通道（authCode → OpenAccount → IoT token）
- **动态实体**：按 TSL 物模型 + 上报属性创建，支持晾衣机/窗帘机/毛巾架/门锁/摄像头/音乐盒子/传感器/插座/墙壁开关/广播
- 修复 TSL 解析：`schema` 实为 URL 字符串，此前被当内嵌 JSON 解析而失败
- 修复机型门控：机型码不可用时不再误判为"支持"，消除永久 unavailable 实体
- 403 限频不再当认证错误重试，交由主备账号切换处理
- 替换已弃用的 `CONCENTRATION_*` 密度常量（HA 2027.8 移除）
- 许可变更：CC BY-NC 4.0 → **GPL-3.0**
- 代码清理：重写 `models.py`/`tsl.py`，移除 `util.py`，删除全部外部项目署名

## [3.0.3] - 2026-09-06

- 动态轮询：电机运行/控制后 70s 内 5s 快轮询，平时 30s，规避 403 限频
- 在线状态检查最多每 120s 一次
- 诊断信息脱敏：密码 redact、token 截断
- 传感器暴露机器可读的故障键（rate_limited/connection_error）及原始服务端错误属性

## [3.0.2] - 2026-08-05

- 所有登录错误追加原始服务端消息（如"密码错误，剩余尝试次数 2 次"），不再只显示泛化文案

## [3.0.1] - 2026-08-05

- 登录错误码精确映射：1032 → 手机号未注册、1035 → 密码格式错误、1073 → 认证过期
- 关键词检测验证码/锁定/限频，完整中英文翻译

## [3.0.0] - 2026-08-04

> BREAKING: refreshToken 认证替换为用户名/密码直登（逆向自好太太 App 3.5.8）

- 登录：AES-CBC 密码加密 + RSA-SHA256 请求签名
- Token 每 6h 自动刷新；refreshToken 过期（1073）自动回退用户名/密码重登
- Config flow 改为用户名/密码，v2 → v3 配置自动迁移
- manifest 声明依赖 `cryptography>=42.0.0`

## [2.x] - 早期版本

- 2.3.2（2026-07-16）：修复卸载 bug（`async_forward_entry_unloads` → `async_forward_entry_unload`）
- 旧架构：refreshToken 认证、domain=`hotata_airer`、仓库名 `hotata-airer`
