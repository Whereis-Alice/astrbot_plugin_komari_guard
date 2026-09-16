<p align="center">
  <img src="./logo.png" width="160" alt="Komari Guard Logo">
</p>

# Komari Guard

Komari Guard 是一个面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 Komari 节点监控插件。它可查询节点状态和历史趋势，也可将离线、高负载、恢复、重启等通知按节点分发到指定私聊或群聊。

本项目使用独立的插件 ID `astrbot_plugin_komari_guard`、数据目录和 `/kg` 命令组，可与 `astrbot_plugin_komari_watch` 同时安装，不会冲突。

## 主要能力

- 查询全部或单个节点的在线状态、CPU、内存、磁盘、网络、负载与运行时长。
- 查询 1-24 小时的资源与流量趋势，以及在线节点资源排行。
- 监控节点离线、CPU/内存/磁盘超阈值、恢复、重启、长期离线和 Komari 面板不可达。
- 通知路由可分别指定目标会话、节点、是否接收告警、每日日报时刻。
- 同一目标的重叠路由会自动去重；发送失败或静默期间的告警进入持久待发队列。
- WebSocket 不可用时自动使用历史记录；两条遥测通道都失败时标记为“未知”，不会批量误报离线。

## 兼容性

- AstrBot `>=4.16,<5`
- Python `>=3.10`
- Komari 1.4/1.5 常见 API 结构
- 声明支持 `aiocqhttp`；其他平台必须支持 AstrBot 主动消息

## 快速开始

1. 在 AstrBot 插件管理页安装本仓库。
2. 填写 `komari_url`；私有 Komari 站点再填写 `komari_token`。
3. 在目标私聊或群聊中发送 `/kg ck` 验证连接。
4. 发送 `/kg b` 绑定当前会话的全部节点告警。
5. 发送 `/kg r` 检查已生效的路由。

`/kg h` 可随时查看命令速查。除帮助外，命令默认需要 AstrBot 管理员权限，防止群成员查看隐藏节点或修改推送配置。如需对成员开放查询，可在 AstrBot Dashboard 中单独调整命令权限。

## 命令

| 命令 | 作用 | 示例 |
| --- | --- | --- |
| `/kg s [节点]` | 状态报告 | `/kg s web-01` |
| `/kg rt [节点]` | WebSocket 实时状态 | `/kg rt` |
| `/kg his [小时] [节点]` | 历史趋势 | `/kg his 6 web-01` |
| `/kg ls` | 节点列表 | `/kg ls` |
| `/kg top [cpu\|mem\|disk] [数量]` | 在线节点排行 | `/kg top mem 10` |
| `/kg b [节点] [HH:MM] [模式]` | 绑定当前会话 | `/kg b web-01 09:00 both` |
| `/kg ub [节点]` | 删除当前会话的命令路由 | `/kg ub web-01` |
| `/kg r` | 列出所有路由 | `/kg r` |
| `/kg m [分钟] [all]` | 暂停当前或所有目标 | `/kg m 60` |
| `/kg um [all]` | 解除暂停并补发待发告警 | `/kg um` |
| `/kg a` | 最近 10 条告警（`alert` 也可用） | `/kg a` |
| `/kg ck` | 立即检查一次 | `/kg ck` |
| `/kg i` / `/kg v` | 站点信息 / Komari 版本（`info` / `ver` 也可用） | `/kg v` |

## 通知路由

路由是本插件的核心：

```text
目标 UMO + 节点选择 + 告警开关 + 日报时刻
```

### 在当前会话快速绑定

```text
/kg b                         # 全部节点告警
/kg b web-01                  # 仅 web-01 告警
/kg b web-01 09:00            # web-01 告警 + 每日 09:00 日报
/kg b web-01 09:00 daily      # 仅每日 09:00 日报
/kg b web-01 09:00 both       # 告警 + 每日 09:00 日报
```

模式支持 `alert`、`daily`、`both`。未写模式时，有时刻就默认 `both`，无时刻就默认 `alert`。

绑定成功后，插件会立即把路由保存到 AstrBot 插件配置的 `notification_routes`。刷新 Dashboard 配置页即可看到并编辑该条目；插件升级前已经由 `/kg b` 创建的状态文件路由也会在首次启动时自动迁移。`/kg ub` 只删除由命令创建的路由，不会误删手动添加的配置路由。

### 节点选择规则

- `*`：全部节点。
- `web-01`：精确匹配节点名、主机名、ID 或 UUID，避免误匹配 `web-010`。
- `node-a,node-b`：一条路由选择多个节点。
- `~web`：显式使用子串匹配。

节点可改名时，建议在配置页使用 UUID。

### 发往指定私聊或群聊

在 `_conf_schema.json` 对应的“通知路由”配置中添加路由，并填入完整 `target_umo`。UMO 的实际格式是：

```text
<平台实例 ID>:GroupMessage|FriendMessage|OtherMessage:<会话 ID>
```

它不是单独的群号或 QQ 号。最稳妥的获取方式是先到目标会话执行 `/kg b`，再刷新配置页或用 `/kg r` 查看 AstrBot 原样生成的 UMO。配置页手动创建和命令创建的路由会同时生效。

### 日报时间

- `report_time` 使用 AstrBot 宿主的本地时间，格式为 `HH:MM`。
- 每条路由单独记录上次发送，不会互相抑制。
- 日报在轮询时触发，最多会比设定时刻晚一个 `poll_interval`。
- 路由没写 `report_time` 时，使用全局 `status_report_time` 或 `status_report_interval`。

## 主要配置

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `notification_routes` | `[]` | 指定目标 UMO、节点、告警和日报 |
| `poll_interval` | `60` | 轮询秒数 |
| `offline_grace_cycles` | `2` | 离线连续确认次数 |
| `cpu_threshold` | `90` | CPU 告警阈值（%） |
| `memory_threshold` | `90` | 内存告警阈值（%） |
| `disk_threshold` | `90` | 磁盘告警阈值（%） |
| `high_load_cycles` | `2` | 高负载连续确认次数 |
| `alert_cooldown` | `1800` | 同类告警冷却秒数 |
| `filter_mode` | `none` | `none` / `allow` / `deny` 全局节点过滤 |
| `status_report_time` | 空 | 全局每日日报默认时刻 |
| `status_report_interval` | `0` | 全局日报间隔（小时），`0` 关闭 |
| `panel_fail_cycles` | `3` | 面板连续失败告警，`0` 关闭 |
| `long_offline_remind_hours` | `0` | 长期离线每日提醒，`0` 关闭 |
| `image_output` | `false` | 使用 HTML 渲染的图片卡片 |

完整配置和提示可直接在 AstrBot Dashboard 的插件配置页查看。运行时还会用 Pydantic 再次校验数值边界，避免错误配置进入监控循环。

## 告警与可靠性

- Komari 的 CPU 字段按 `0..100` 百分数处理；`0.8` 表示 `0.8%`，不会被放大为 `80%`。
- 支持 Komari 1.4 的扁平 WS 字段和 1.5 的嵌套字段。
- 指标缺失不会触发“负载恢复”；必须重新观测到之前超标的指标已回落。
- 静默是“暂停发送”，不是丢弃事件。待发告警在解除静默或平台恢复后补发，每个目标最多保留 50 条去重消息。
- 状态以原子替换方式写入新插件专用数据目录。
- 监控循环有异常边界和指数退避，单次未预期异常不会让后台监控永久停止。

## 隐私与安全

- `komari_token` 只用于请求 Komari，不会写入插件日志或状态文件。
- `image_output` 默认关闭。启用后，AstrBot `html_render` 可能通过你配置的 T2I 服务渲染卡片，节点名和指标会出现在渲染内容中。对外部服务有隐私顾虑时请保持关闭。
- 路由 UMO 应从 AstrBot 原样复用，不要把不可信用户输入直接写入配置。

## 常见问题

### `/kg rt` 提示 WebSocket 不可用

反向代理可能没有转发 WebSocket，或拦截了 `Origin`。插件会自动使用 `/api/records/load` 作为后台监控的历史兜底；手动查询可用 `/kg s`。

### 绑定成功但收不到消息

1. 用 `/kg r` 确认路由中的 UMO 完整。
2. 确认平台适配器支持主动消息。
3. 检查 `/kg m` 静默是否仍生效。
4. 查看 AstrBot 日志中的“未找到平台实例”或适配器发送错误。

### 节点显示“未知”

这表示插件能读取节点列表，但本轮 WebSocket 和该节点历史遥测都不可用。“未知”不会累计离线周期，避免数据源故障变成节点离线误报。

## 开发与验证

```bash
python -m pip install -r requirements.txt
python test_smoke.py
```

回归套件覆盖指标解析、遥测三态、路由隔离与去重、待发重试、静默补发、独立日报进度、命令签名和 AstrBot `chain_result` 契约。

## 来源与致谢

本项目基于 [xiaowan138/astrbot_plugin_komari_watch](https://github.com/xiaowan138/astrbot_plugin_komari_watch) 的 MIT 许可代码衍生，保留原项目提交历史和许可声明。本版合入并扩展了上游 [PR #1](https://github.com/xiaowan138/astrbot_plugin_komari_watch/pull/1)、[Issue #2](https://github.com/xiaowan138/astrbot_plugin_komari_watch/issues/2) 和 [Issue #3](https://github.com/xiaowan138/astrbot_plugin_komari_watch/issues/3) 中报告的问题。

## 许可证

[MIT License](./LICENSE)
