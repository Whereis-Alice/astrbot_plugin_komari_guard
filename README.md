# Komari 监控推送插件

英文名称：**Komari Watch**（插件标识：`astrbot_plugin_komari_watch`）  
创建者：xiaowan

这是一个独立实现的 AstrBot 插件，借鉴了社区 `astrbot_plugin_komari_status` 的查询思路，但没有复用其代码。除节点查询外，本插件增加了适合长期运行的离线确认、高负载确认、告警冷却和恢复通知。

## 功能

- `/komari_status`（别名 `/kstatus`、`/komari`）：查询节点在线状态及 CPU/内存/磁盘占用。
- `/komari_realtime`、`/komari_public`、`/komari_version`：查询实时数据、公开站点信息和服务端版本。
- `/komari_bind`：把当前 OneBot 私聊或群聊绑定为告警接收目标。
- `/komari_unbind`：解除当前会话绑定。
- `/komari_check`：立即执行一次检查。
- 后台轮询 `/api/nodes`，并尽力从 `/api/clients` WebSocket 补充实时指标。
- 节点连续多个周期无心跳才告警；高负载连续多个周期超过阈值才告警；同类告警支持冷却和恢复通知。

## 安装配置

在 AstrBot 插件配置页面填写 Komari 地址，私有站点再填写 Token。按需调整轮询间隔、离线确认周期、CPU/内存/磁盘阈值等。启动后在目标 OneBot 群里发送 `/komari_bind` 即可接收推送；绑定信息保存于 AstrBot 的 `data/plugin_data/astrbot_plugin_komari_watch/state.json`。

建议先用 `/komari_check` 验证 API 与权限，再开启较短的轮询周期。Token 只保存在 AstrBot 配置中，不会写入日志。

## 开源说明

本项目遵循 MIT License，欢迎提交 Issue 和 Pull Request。发布到 GitHub 时请保留 `metadata.yaml`、`requirements.txt` 与本说明，并在发布前补充实际仓库地址。
