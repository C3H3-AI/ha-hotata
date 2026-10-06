# Changelog

> 最低 Home Assistant 版本：**2024.12.0**（声明于 `hacs.json`）

## [4.0.11] - 2026-10-07

- **switch.py**：修复高级款晾衣机（[Issue #11](https://github.com/C3H3-AI/ha-hotata/issues/11)，D-3072S 缺除菌开关）。根因确认：晾衣机存在**两个产品族，能力描述体系完全不同**——
  - 普通款（`AIRER_PRODUCT_KEYS`）上报机型码 `DeviceModelType`（0-3），TSL 是产品线全量模板（会列出该系列所有可能功能），上报流是 TSL 的镜像，因此**机型表是硬件的唯一权威**；
  - 高级款（`ADVANCED_AIRER_PRODUCT_KEYS`，如 D-3072S 的 `a1abYBCSVlV`）**根本不上报机型码**，TSL 是按型号精确的（59 个属性 vs 普通款 29 个），且会上报 `ModelFunctionList`（32 位功能位图）作为逐设备真实功能清单。
  此前对所有晾衣机统一套用「机型白名单」门控，高级款因无机型码被判定为不支持 → 除菌/风干/烘干开关全部误伤。现按产品族分流：普通款沿用机型白名单（行为不变），高级款改为「TSL 声明 + 实际上报」判定
- **tests**：`test_capability_gating.py` 新增两个产品族的用例（25 项断言）。修复前高级款 3 项失败、修复后全绿；普通款（model=2）行为在修复前后完全一致，防止分流逻辑越界
- 依据：D-3072S 用户的 4.0.10 诊断（`device_name=044a6997f003`，`DeviceModelType=null`，`ModelFunctionList='00000000000000000011111100110000'`），与你自己的 model=2 设备诊断交叉比对

## [4.0.10] - 2026-10-06

- **新增 `hotata.export_capabilities` 诊断服务**：一键导出账号内所有设备的能力矩阵——每个 TSL 属性的声明状态、实际上报、当前值、accessMode 对照，另含"上报了但 TSL 未声明"的异常清单。排查"某机型缺开关/传感器"时，调用一次即得全部判定数据（响应可整段贴进 GitHub issue）
- **diagnostics**：`thing_model_properties` 从属性名列表升级为完整声明（identifier + name + accessMode + dataType 含枚举 specs）。用户"下载诊断"即可拿到机型 TSL 原文，无需再手动调用服务
- 背景：排查 [#11](https://github.com/C3H3-AI/ha-hotata/issues/11) 时发现我们的机型白名单与云端 TSL 存在冲突（model=2 设备声明并上报了白名单之外的能力），且不同机型的 TSL 声明集可能不同。本版本不改判定行为，只补齐验证所需的数据通道；待收集到 D-3072S 等机型的真实 TSL 后再决定白名单的最终形态

## [4.0.9] - 2026-10-06

- **switch.py**：**撤回 4.0.8 的错误修复**。4.0.8 在机型码缺失时按「设备是否上报该属性」放行——但真机实测（model=2 设备）证明**上报流是 TSL 的镜像而非硬件的反映**：该设备把语义表里它没有的 `DryingSwitch` / `AirDryingSwitch` / `IonsSwitch` 也全部上报。按上报放行会把烘干/风干/负离子开关错误地发给本不该有的机型。现改为：机型码缺失时不创建实体，并打 warning 日志（点名设备与缺失字段，引导用户上报，用真实数据修正白名单）；白名单外机型码仍然压制
- **App 逆向结论（本轮补充验证）**：`classes.dex` 为 360 加固壳（587 条字符串 0 命中机型/功能），业务逻辑不在任何可读层；`assets/` 里的 `air_*.json` 全是 Lottie 动画。App 本身不维护本地机型表，能力展示完全依赖云端 TSL 下发——我们此前的 0-3 机型表来自对其 `Constants.java` 的反推，是集成自己的知识，不是 App 的行为
- **tests**：`test_capability_gating.py` 更新为钉住正确语义（18 项断言），4.0.8 的放行行为在测试中作为回归被钉死

## [4.0.7] - 2026-09-27

- **manifest.json**：移除 `requirements` 里的 `cryptography>=42.0.0`。hassfest 新增规则：该库是 Home Assistant 自身的依赖（core 当前锁 `cryptography==50.0.1`），自定义集成不得在 manifest 中重复声明，否则 CI 报 `[REQUIREMENTS] ... must not be listed` 并失败。`api.py` 的 import 不受影响，HA 环境内始终可用
- **仓库归属回归**：原账号解禁，`C3H3-AI/ha-hotata` 恢复为主仓库（18 star / 6 fork / 全部 PR 与 issue 均在此），`c3h3-ci/ha-hotata` 保留为灾备镜像。此前封号期间写入的 owner 引用（manifest / FUNDING / README / CHANGELOG）已改回 `C3H3-AI`
- **release 工作流**：修掉 `release.yml` 的 UTF-8 BOM。该文件开头带 `EF BB BF`，导致 `on: push: tags: v*` 触发器从未注册 —— 这就是 `v4.0.1` 一直没有 release 的原因。另两个 workflow 无 BOM，故一直正常

## [4.0.6] - 2026-09-21

- **轮询优化**：设备离线时只查在线状态，不再拉取属性/事件/TSL
  - `_async_update_data` 改为**先读在线状态再读属性**。原先先拉属性、最后才查在线，离线设备必然白白浪费一次属性请求，且这些失败请求会助推云端 403（操作过于频繁）限频
  - 在线状态明确为 `False` 的设备跳过 `get_properties` / `get_latest_event` / `get_thing_model`，**保留上一次已知属性**不置空（实体通过 `HotataEntity.available` 判 `online is not False` 转为不可用）
  - 在线状态**读取失败**（异常）或返回 `None` 时**不当作离线**，仍照常拉属性 —— 拿不到状态不等于设备下线
  - 按设备区分：混合场景下在线成员照常轮询，不受离线成员影响
- **轮询间隔**：新增 `POLL_INTERVAL_OFFLINE = 30`。只要有任一已知设备离线，轮询固定 30 秒；**离线优先于快窗**，即刚下发了控制命令也不会对已离线设备跑 5 秒快轮询。下一轮发现设备回到在线即自动恢复正常的 5s/30s 动态区间
- **tests**：新增 `test_offline_polling.py`（27 项断言）。在改动前的代码上 8 项失败，本版本全绿

## [4.0.5] - 2026-09-20

- **cover.py**：修复晾衣架在 HomeKit 桥接后从「已关上」状态点击图标无法上升（[Issue #13](https://github.com/C3H3-AI/ha-hotata/issues/13)）。`HotataAirerCover` 用 `self._position or 100` 计算当前位置，把合法的关闭态 `0` 当成假值替换为 `100`，导致 HomeKit 下发的 `set_cover_position(position=100)` 命中 `target == current` 被静默丢弃（无电机命令、无日志）。`async_set_cover_position` / `async_close_cover` 改为显式 `is not None` 判断，让 `0` 作为合法位置参与比较
- **cover.py**：同根因的两个副作用一并修掉——首次从底部拖滑块会误发 `MOTOR_CLOSE`（方向相反）；`async_close_cover` 在底部触发自动停止时时长被算成完整 `descent_time` 而非最小 1 秒
- **cover.py**：`_async_auto_stop_cover` 在 `target_position` 已被 `_cancel_stop_timer` 清空时，不再用裸 `else 0` 把位置拍到底部，改为回落到 `runtime.simulated_position`
- **cover.py**：四个命令方法（open/close/stop/set_position）成功路径末尾补 `async_write_ha_state()`，让 HomeKit 的 `CurrentPosition` 命令后即时刷新，不必等一个协调器轮询周期；命令因 `target == current` 被跳过时补一条 debug 日志，避免故障在日志里完全不可见
- **tests**：新增 `test_airer_cover_position.py`（issue #13 回归，32 项断言）与 `test_airer_cover_preservation.py`（非 bug 输入保持性基线，36 项断言），配套 `cover_harness.py`。在 `main` 上前者 15 项失败、本版本全绿
- 窗帘机 V1/V2 与 A/B 杆导轨实体未改动
- 由 @Kyle0820 在 PR #14 定位并修复

## [4.0.4] - 2026-09-16

- **cover.py**：`HotataRailCover` 补声明 `_attr_is_closed = None`。HA 的 `CoverEntity` 对该属性只做类型标注、无默认值（相邻的 `_attr_is_closing` / `_attr_is_opening` 均有 `= None`），未声明会让 `is_closed` 在每次状态写入时抛 `AttributeError`；每次写状态会读两次且 `cached_property` 不缓存异常，故为持续报错而非偶发
- 由 @RainySat 在 PR #12 定位并修复

## [4.0.3] - 2026-09-13

- **entity.py**：`DeviceInfo.via_device` 已弃用（HA 每次启动告警，2027.8 起失效），改用 `via_device_id`。新参数需要网关的**设备注册表 ID**，而该 ID 在 `__init__` 构建 `DeviceInfo` 时尚未生成（实体先于注册表处理完成构造），因此改为在 `async_added_to_hass` 中建立关联：无 `parent_iot_id` 直接返回、网关未注册则保持顶层不设悬空 ID、已关联则空操作。独立设备（无网关）行为不变

## [4.0.2] - 2026-09-13

- **两轮 heavy probe**：主号 24h 到期后先发真实请求（账号级 list_devices + 设备级 get_properties 各一次）都通过才切回，防止好太太 403 风控只卡设备请求不卡账号请求时，切回后几分钟又被打回备用号的抖动循环
- **备用号字段补入首次配置表单**：backup_username / backup_password 之前只在重配置步骤才有，首次添加集成看不到，failover 功能等于没激活
- **传感器 friendly-name 改名**：DeviceModelType "设备型号" -> "机型功能配置"（原字段是硬件功能组合枚举，不是型号字符串）

## [4.0.1] - 2026-09-13

- **entity.py**：DeviceInfo 注入 MAC connections，HA 设备注册表能按 MAC 跨集成匹配同一台实体
- **sensor.py**：requires_report 硬件门控，TSL 声明但硬件不支持的传感器（温度/湿度/PM2.5/甲醛等）只有云 API 实际上报了才创建
- **switch.py**：IonsSwitch（负离子开关）恢复 MODEL_HOT_DRYING 机型门控

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
