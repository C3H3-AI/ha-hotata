# 交接文档 — ha-hotata v4.0.0

> 更新时间：2026-09-13。本文件供后续会话/接手人快速上手。

## 1. 项目现状

**v4.0.0 已完成开发、离线回归与测试 HA 实测验证。**

> ### ⚠️ v4.0.0 破坏性变更：domain 更名
> - **domain**：`hotata_airer` → `hotata`
> - **目录**：`custom_components/hotata_airer/` → `custom_components/hotata/`
> - **仓库**：`ha-hotata-airer` → `ha-hotata`（GitHub 已改，旧地址自动 301 重定向）
> - **许可**：CC BY-NC 4.0 → **GPL-3.0**
>
> **不做自动迁移**，用户须删除旧集成后重新安装。原因：HA 将 config entry 与 domain
> 强绑定且不支持原地改名，而 unique_id 中含 domain，旧实体必然变孤儿。
> 曾尝试写迁移逻辑（重建 entry + 改写 unique_id），但因 config-entry-only 集成
> 的 `async_setup` 不会被主动调用、且重建有竞态，**已按用户决定全部移除**。
> 详见 README 的升级说明章节。

- 传输层采用官方 App（好太太智联 3.5.8）的**阿里云 IoT 网关通道**：账号服务换 authCode → 阿里云 OpenAccount → IoT token，后续设备/属性/服务/TSL 全部走网关。支持**全部产品线**：晾衣机（含 A/B 双杆高级机型）、窗帘机 V1/V2、毛巾架、智能门锁、人体/门磁/烟感/燃气/水浸传感器、空气质量、摄像头、音乐盒子、插座、墙壁开关、广播、植物灯。
- 实体按 **TSL 物模型 + 云端上报属性动态创建**，账号下有什么设备就出什么实体。
- 保留本项目独有能力：**403 限频退避 + 主备账号自动切换状态机**、动态轮询（活动 5s/空闲 30s）、晾衣杆时间模拟位置 + 自动停止、下降时长配置、DeviceModelType 机型门控。
- 403 限频**不当认证错误重试**（重试会延长云端处罚）；`api.py` 用 `_is_rate_limited()` 识别后抛 `HotataRateLimited`，交由账户层静默/切换。

### 1.1 本项目 v3（自研逆向）→ v4（阿里云网关）对比

> 2026-09-13 代码级核对。基线：`git archive 5b6390a`（v3.0.3，最后提交版）。
> 原则：**既然已经逆向好了就不重来** —— v4 不是重写，是双通道叠加。

**v3 的逆向成果（`util.py` + `const.py` + `hub.py`）**：
- 主机 `saas.keyoo.com/app-api/v2.0`（`login/password`、`spLogin/refreshToken`、
  `getSpDeviceList`、`property/get`、`property/set2`、`service/invoke2`、`synOnlineStatus`）
- 密码 **AES-CBC** 加密（固定 AES_KEY/AES_IV）+ base64
- 登录 body **RSA-PKCS1v15-SHA256** 签名（内置 DER 私钥）
- 业务请求 **MD5** 签名（`generate_sign`）

**v4 未废弃任何一项，而是把它们作为登录链的第一环**：

```
[keyoo 通道·v3 逆向成果]
  POST saas.keyoo.com/app-api/v2.0/login/password
  (AES 加密密码 + RSA 签名)  →  authCode
                                   ↓
[阿里云 OpenAccount]  sdk.openaccount.aliyun.com → sessionId
                                   ↓
[阿里云 IoT]  api.link.aliyun.com → iotToken / refreshToken / identityId
                                   ↓
  后续设备/属性/服务/TSL 全部走阿里云网关（HmacSHA1/256 + content-md5 签名）
```

即 **keyoo 只负责换 authCode，之后的设备与控制全交给阿里云**。const.py 同时保留
`ACCOUNT_HOST = saas.keyoo.com`、`OPEN_ACCOUNT_HOST`、`API_HOST = api.link.aliyun.com`。

**逆向常量全部保留**（仅 `util.py` 拆散后内联进 `api.py`，值不变）：

| 常量 | v3 | v4 | 状态 |
|------|----|----|------|
| AES_KEY / AES_IV | const.py | api.py 内联 | ✅ 值不变 |
| ACCOUNT_PRIVATE_KEY (RSA) | const.py | api.py 内联 | ✅ 值不变 |
| APP_SECRET (MD5/Hmac) | const.py | const.py | ✅ 值不变 |
| APP_VERSION | `APP_VERSION_APP` | 改名 `APP_VERSION` | ✅ 仍为 `"3.5.8"` |

**架构变化**：`hub.py` 1186 → **546** 行——传输/签名逻辑抽到 `api.py`(635)，
`hub.py` 只剩账户状态机；新增 `coordinator/entity/models/tsl/exceptions/event/media_player/select`，
`util.py` 删除（职责并入 api.py，并消除 hub↔config_flow 循环导入）。

**v3 独有能力已全部继承**：下降时长配置（v3 `hub._descent_time` → v4 `DeviceRuntime`）、
时间模拟位置 + 自动停止、机型门控、诊断脱敏、主备切换。**无丢失项。**

**⚠️ 一处继承的 bug（已修）**：TSL 解析失败并非 v4 引入，
**本项目 §4b 已自行修复。**

### 1.2 代码溯源与清理（2026-09-13）

> **用户决定：彻底重写，不留外部参考痕迹。** 本节记录处理过程与结果，供接手人了解现状。

**已完成的清理**：

| 项 | 处理 |
|----|------|
| 源码中的外部项目署名 | 7 处注释全部移除（api / const / coordinator / cover / __init__） |
| `models.py` | **重写**（原与外部实现逐字符相同） |
| `tsl.py` | **重写**（原与外部实现逐字符相同；另加 `has_property` / `data_specs` / `value_range`） |
| LICENSE | **CC BY-NC 4.0 → GPL-3.0**（原许可不适用于软件，且与派生代码的 copyleft 要求冲突） |
| README | 移除外部项目链接与"同源"表述；License 徽章与章节同步改为 GPL-3.0 |
| HANDOFF | 移除外部仓库地址与 clone 指引 |

**重写原则**：仅保留不受版权保护的**客观事实**（协议端点、产品 key、加解密常量、
TSL 数据形状），表达方式、代码结构、注释全部重写。

**当前代码状态**：`custom_components/` 下已无任何外部项目名或仓库链接残留。

> **维护方针（2026-09-13 用户确认）**：本项目独立维护，不向任何外部仓库提 issue/PR。
> 发现外部实现缺陷时在本项目内自行修复即可，不对外同步。

## 2. 环境与访问

| 目标 | 信息 |
|------|------|
| 生产 HA | 192.168.3.3（HAOS），SSH root，密码见 `../home-assistant-nas/ha-standby/sync_config.sh`（本文件不重复明文）。SSH/Samba 匿名不可写。 |
| 生产集成状态 | 代码已是 v4.0.0，但 config entry 为 `disabled_by: user`（用户此前禁用）。**UI 中启用即可**，启用时自动执行 v3→v4 迁移（免手动操作）。 |
| 测试 HA | 本机 docker `homeassistant-test`，端口 **8125**，配置目录 `../home-assistant-nas/ha-test/config`。前端账号 `duola` / 与 SSH 相同密码。 |
| 测试 HA 现状 | 已迁移 v4 并实测：24 个实体（其中 4 个 unavailable，见 §4b）、真实开/关往返成功、零错误。v3 孤儿实体已清理。 |
| 备份 | 生产旧代码备份在 192.168.3.3:`/tmp/hotata_airer_backup_20260912_071042.tar.gz`（/tmp 重启即失，建议尽早移到 /backup）。 |
| 账号 | 生产 entry 里主账号 13736776363 + 备用账号 19285871820（密码与 SSH 相同，用户复用）。 |

## 3. 代码结构（v4）

```
custom_components/hotata/
├── api.py          # 阿里云网关客户端（登录链 authCode→OpenAccount→iotToken、HmacSHA1/256 签名、
│                   #   属性/服务/设备列表/TSL/事件；403→HotataRateLimited，认证失败自动刷新重试一次）
├── coordinator.py  # DataUpdateCoordinator：动态轮询 + 403 接入 + 网关子设备递归发现 + DeviceRuntime（descent_time Store）
├── hub.py          # HotataAccount：主备身份切换状态机（per-identity 处罚期限、探测先行切回、Store 持久化、通知）
├── entity.py       # HotataEntity(CoordinatorEntity) + 动态实体工厂 + property_value/has_property
├── models.py / tsl.py / exceptions.py   # 设备模型、TSL 解释器、异常层次
├── const.py        # 产品线 product keys、READ_ONLY_QUERIES 白名单、门控/退避常量
├── config_flow.py  # v4（VERSION=4）：登录 + 备用账号；错误分类渲染
├── __init__.py     # setup/unload + v3→v4 迁移（保留 descent_time 到 Store）+ set_property/invoke_service/query 服务
└── 10 个平台文件    # cover/switch/light/sensor/binary_sensor/button/number/select/event/media_player
tests/
├── ha_stub.py          # HA/aiohttp stub（Store 按 key 共享内存）
├── test_v4_account.py  # 8 个故障切换场景回归（python3 tests/test_v4_account.py，无需 HA）
└── key_reference.txt   # 协议 RSA 私钥备份（api.py 中为唯一权威）
```

**关键设计点**（改动前必读）：
- 主身份 token 存 `entry.data`（coordinator 刷新后经 `maybe_persist_primary_tokens` 回写，**没有注册 update listener**，所以不会触发集成 reload）；备用身份 token 与处罚状态存 Store（`hotata.account.<entry_id>`）。
- 403 罚则按身份独立计时；切回主账号是**探测先行**（用 `async_list_devices` 试探，成功才切换），失败按指数退避（10min→最长 1h）；杂散成功请求不会提前解除处罚（防 403 风暴）。
- `dataclass(slots=True)` 的 `HotataDevice` 每次轮询重建，实体一律经 `current_device` 读最新值，不缓存引用。
- `HotataEntity` 必须继承 `CoordinatorEntity`（曾因漏 super 初始化导致全平台实体 restored，已在测试 HA 踩坑修复）。

## 4. 验证记录

1. **离线回归**（`tests/test_v4_account.py`）：主备切换/双 403/探测切回/探测失败留任/备用登录被限/重启恢复/杂散成功——8/8 通过。
2. **测试 HA 实测**（本机 8125，真实云端）：
   - entry v3→v4 自动迁移 ✓（keyoo 旧字段清除，iot_token 写入）
   - 阿里云登录链路 ✓（设备发现、属性、在线状态）
   - 机型门控（ModelType=2 → 无烘干/风干）✓
   - query 服务直接查云端属性/状态 ✓（`?return_response`）
3. 生产已同步 v4.0.0 代码文件，entry 保持禁用待用户启用。

## 4b. 2026-09-13 复验（本轮）

在交接基础上重新验证，并修复了两处遗留/隐患：

| 项 | 结果 |
|----|------|
| 离线回归 | 8/8 PASS |
| 动态轮询 | 正常：控制后 5s 快轮询，活动窗口 70s 后回落 30s（日志 08:01:52 观测到 `Poll interval -> 30.0s`） |
| 真实控制往返 | 照明 / 消毒 / 晾衣杆 三项全部 PASS（云端 `/thing/properties/set` 链路） |
| query 服务 | `device_properties` 返回 200 + 全量属性 |
| hotata 相关 ERROR | 0 |

**本轮修复 2 处**：

1. **TSL 解析失败（遗留项 #2，已修复）**——根因：`api.async_get_thing_model` 把 `data["schema"]`
   （实为 TSL JSON-schema 的 **URL 字符串**）当成内嵌 JSON 交给 `_decode_embedded_json`，
   `json.loads` 必然抛 `Unexpected JSON-encoded cloud response`。
   修复：仅当 `schema` 为 `dict/list` 时才递归解析。
   效果：日志 `Thing-model update failed` 归零；TSL 驱动新增
   功能列表 / PM2.5 / 湿度 / 温度 四个传感器，因本机机型未上报故为 `unknown`，属预期。
   select 实体不出现是**设计如此**：`select.py` 的枚举属性按 product_key 白名单
   （护理机 WorkMode / 摄像头 StreamVideoQuality / 广播 ArmMode），晾衣机不匹配。
   > **实体计数更正**：早期记录的"16 → 20"统计不准（漏计了 4 个 `unavailable` 的
   > 烘干/风干实体，它们创建于 09-13 00:40，早于该次统计）。**实测总数为 24**：
   > 20 个正常 + 4 个 unavailable（烘干/风干开关及其剩余时间传感器）。
   > 这 4 个是 `switch.py` 门控对"机型不支持"的处理结果——实体仍创建但置为
   > unavailable（sensor 侧则是直接跳过，见日志 `model type 2 lacks it`）。
   > 二者行为不一致，属**已知待优化项**，非本轮引入。
2. **HA 弃用常量告警（交接文档未记录，已修复）**——`sensor.py` 直接用
   `CONCENTRATION_MICROGRAMS_PER_CUBIC_METER` / `CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER`，
   HA Core 2027.8 将移除。改为优先导入 `UnitOfDensity`，失败回退旧名，
   兼容 manifest 声明的 `homeassistant: 2024.1.0`。部署后两条 WARNING 消失。

已同步到测试 HA 并重启验证；**生产尚未同步这两处修复**（见遗留事项 6）。

## 5. 遗留事项

1. **git 未提交**：v3.1.0→v4.0.0 的全部改动在工作区（含主机上原本未提交的 failover 基线）。建议整理为 release commit（历史惯例 `release v4.0.0: ...`）。
2. ~~**TSL 获取失败**~~ —— **已于 2026-09-13 修复**，见 §4b。
3. **生产启用**：用户在 UI 启用 entry 即可；启用后旧 v3 实体会变 unavailable 孤儿（unique_id 格式变了），需手动删除。
4. **测试 HA 的 entity_id 带 `_2` 后缀**：v3/v4 entity_id 冲突的历史痕迹，新装环境不会出现。
5. 外部参考副本 `/tmp/ref-ha-hotata`、`/tmp/refL` 等临时克隆均已删除；如需重新对照，可自行获取。
6. **生产待同步本轮两处修复**：生产 `/homeassistant/custom_components/hotata/` 目前仍是
   9/13 00:06 版（含 v4.0.0 全部功能，但**不含** §4b 的 TSL 解析修复、弃用常量修复，
   以及本轮的代码重写）。生产 entry 仍为 `version: 3` + `disabled_by: user`，
   待用户 UI 启用时自动迁移。建议：用户在 UI 启用**之前**先 rsync 一次最新代码。
   备份仍在 `/tmp/hotata_airer_backup_20260912_071042.tar.gz`
   （在 /tmp，重启即失，建议移到 /backup）。

## 6. 常用操作

```bash
# 离线回归
python3 tests/test_v4_account.py

# 部署到测试 HA 并重启（必须用绝对路径，~ 展开会指向别的目录）
rsync -a --exclude='__pycache__' --delete custom_components/hotata/ \
  /media/duola/devdata/AI-workspace/home-assistant-nas/ha-test/config/custom_components/hotata/
docker restart homeassistant-test

# 部署到生产（不影响禁用状态）
sshpass -p <密码> rsync -avz --delete --exclude='__pycache__' \
  -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
  custom_components/hotata/ root@192.168.3.3:/homeassistant/custom_components/hotata/

# 查看集成日志（测试 HA）
tail -f ../home-assistant-nas/ha-test/config/home-assistant.log | grep hotata
# 注意：生产/测试的 `docker logs` 不可靠（日志曾停在 9/4），要用 /config/home-assistant.log

# 用 query 服务查云端原始数据（调试利器）
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"device_properties","iot_id":"<iotId>"}' \
  "http://localhost:8125/api/services/hotata/query?return_response"
```

### 获取 HA API token（两步，直接用 login_flow 的 result 会 401）

`login_flow` 返回的是 **authorization code**，不是 token；必须再 POST `/auth/token` 换一次：

```python
import requests
BASE='http://localhost:8125'; CID=BASE+'/'
s=requests.Session()
fid=s.post(BASE+'/auth/login_flow',json={'client_id':CID,'handler':['homeassistant',None],'redirect_uri':CID}).json()['flow_id']
code=s.post(BASE+'/auth/login_flow/'+fid,json={'username':'duola','password':'<SSH密码>','client_id':CID}).json()['result']
token=s.post(BASE+'/auth/token',data={'grant_type':'authorization_code','code':code,'client_id':CID}).json()['access_token']
```

注意：每次 `login_flow` 步骤都要带 `client_id`，否则报 `required key not provided`。
