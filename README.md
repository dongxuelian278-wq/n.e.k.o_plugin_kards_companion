# KARDS 猫娘陪玩（kards_companion）

被动读取 KARDS 游戏画面 → 结构化对局状态与单位履历 → 猫娘按事件优先级做陪伴解说。
只读屏：不读内存、不抓包、不代操作。

当前清单版本：**6.0.0**。

## 当前能力边界（避免把待实机项写成完成）
- 已实现并在本地测试覆盖：事件契约、场景解析、tracker 履历、仲裁/安全、单槽投递、gate、生命周期与持久化。
- HUD 读屏链路已接线，但**尚未在真实对局稳定验证**；无有效帧/分类不可信时安全暂停。
- 单位/前线事件依赖可选的 `models/kards_det.onnx`；**本包未附带该模型**，缺失时只跑 HUD OCR、单位事件静默。
- 卡名匹配链未接通；**导入卡组代码只保存卡组，不等于画面能识别卡名**。
- 指令牌环形缓冲代码存在，但**稳定识别/放大卡面未完成**，不要按已完成能力宣传。
- 平稳期主动点评的节流与投递链已实现；实际观感仍以真实运行验证为准。

## 安全边界（硬门）
- **consent**：`[kards].llm_data_consent=false` 时，所有宿主 push（`read/respond/context/expire/test`）为
  **0 次真实推送**，实时状态查询 tool 也不返回对局数据。
- **dry-run**：完整执行场景/仲裁/安全链，但最终 gate 为 no-op；只写 `events.jsonl`，不触碰真实投递账本。
- **场景上下文**：无有效帧、`unknown`/`draft` 分类或场景上下文失效时，不推进 tracker diff，不补报
  单位/HQ/回合事件；确认离场（终局或连续明确菜单帧）才清当前局记忆，保留累计局号。
- **回执语义**：SDK `push_message` 回执只表示**本地提交**，不是宿主 LLM 已生成或 TTS 已播放。
  `legacy_unconfirmed`（旧 SDK 无回执）和 `unknown` 永远不当作已接受。

## 安装（自用）
1. 关闭 NEKO。
2. 把本目录整体复制到宿主插件安装根目录（实际优先级为
   `%LOCALAPPDATA%\N.E.K.O\.neko-plugin-installations\plugins\kards_companion\`；
   旧文档中的 `plugins\kards_companion\` 不是首选安装位置）。
   ⚠️ 与商店旧版同 id：先卸载/停用旧版再放新版，避免双实例。
3. 启动 NEKO → 插件列表出现「KARDS 猫娘陪玩」。

## 上手
1. 面板「开始监控」（动作 `monitor_start`），进 KARDS 打一局；
2. 建议先开 `dry_run`（动作 `dry_run_set`，`{ "enabled": true }`）：只往数据目录写
   `events.jsonl` 不发言，打一两局核对已接线的 HUD/场景/事件链（单位与指令牌识别仍待
   可选模型/实机验证，现阶段不要当作已完成能力）；
3. 关掉 dry_run，切「陪伴」模式（动作 `mode_set`，`{ "mode": "companion" }`）；
   `chatter_interval_seconds`（默认 60）控制闲聊节奏；
4. 可选：用 `deck_import` 粘贴卡组代码（`%%45|…`）。当前仅保存卡组信息，卡名识别链未接通；
5. 多角色宿主：`target_set` 设置真实目标会话；留空=跟随当前角色，插件绝不伪造角色名。

## 面板动作 ID
| 动作 ID | 参数 | 说明 |
| --- | --- | --- |
| `monitor_start` | 无 | 开始监控 |
| `monitor_stop` | 无 | 停止监控，expire 实时读上下文 |
| `monitor_pause` | 无 | 暂停识别/推送并 expire read；采集与监控保持运行 |
| `monitor_resume` | 无 | 恢复；首帧重建基线，不补报暂停期事件 |
| `monitor_status` | 无 | 状态（场景/上下文有效性/gate/投递） |
| `monitor_diagnostics` | 无 | 诊断时间线、gate 回执、模拟记录、最后快照 |
| `target_set` | `{ "target": "会话名" \| "" }` | 设置/清空固定目标 |
| `monitor_test_say` | `{ "target": "会话名", "authorized_real_test": true }` | 一次非对局连接测试；回执仅本地提交 |
| `mode_set` | `{ "mode": "companion" \| "coach" \| "silent" }` | 切换解说模式 |
| `dry_run_set` | `{ "enabled": true \| false }` | 切换完整模拟/最终不推送 |
| `deck_import` | `{ "code": "%%xx\|…" }` | 导入卡组代码 |

连接测试硬条件：显式 `authorized_real_test=true`、真实目标（无目标直接拒绝，不给假名）、
`dry_run=false`、`llm_data_consent=true`、gate running、输出安全门允许。测试载荷明确
`non_game=true` / `no_history=true`；不会读取或声称对局历史。

## 配置默认（`[kards]`）
- `auto_start_monitoring = true`（显式保留旧习惯）
- `plugin_owned_output_enabled = false`
- `user_chat_quiet_window_seconds = 60`
- `battle_output_quiet_window_seconds = 30`
- `battle_output_backpressure_seconds = 20`
- `critical_cooldown_seconds = 5`
- `battle_event_ttl_seconds = 12`
- `target_lanlan = ""`（跟随当前角色）
- `llm_data_consent = true`
- `dry_run = false`

已显式写入配置的值优先；manifest 只提供默认值。运行时身份（正式/开发安装）取自
`ctx.plugin_id`，不会把 dev 安装标成正式 id。

## 采集健康与无有效帧安全暂停
- 健康状态用于回答「这一帧能不能拿来识别」，而不是「监控按钮是否按下了」。约定值包括：
  `disabled`、`process_not_found`、`window_not_found`、`minimized`、`frame_unavailable`、
  `stale_frame`、`capturing`、`degraded`、`stopped`。
- 只有窗口存在、未最小化、画面可用且帧龄在阈值内时，才把画面交给识别。无有效帧时
  识别与解说**安全暂停**：不推进对局、不补造单位事件；恢复后从当前画面重建基线。
- 诊断时间线只保留阶段、结果、原因等元数据，不包含截图或原始 OCR 文本。

## 采集能力边界（当前源码）
- 采集链：dxcam（桌面复制）优先；失败后回退 `PIL.ImageGrab`（仅目标窗口前台时）。
- 两条后端对遮挡、最小化、独占全屏、虚拟桌面切换的行为不同，不能承诺「后台/被遮挡也能读」。
- WGC 是候选后端方向，不等于已实现或默认启用；以实际构建和运行状态为准。
- 新的采集与健康实现仍在联调中。验证请以 dry-run 的事件日志和面板诊断时间线为准。

## 隐私
- 画面只在内存中处理；截图不外发。
- 可选出网：卡牌目录更新，默认关闭；内置卡牌库离线可用。启用后失败保留本地数据；
  数据源可在 `[kards].catalog_url` 更换（SSRF 校验，禁私网/环回/元数据地址/重定向）。
- 可选 VLM 兜底默认关闭；密钥只从环境变量读取，绝不写入配置或本仓库。
- 用户聊天 hook 只记录**时间**与目标是否存在，不读取/保存原文或语音转写。

## 许可与第三方归属
- 本插件整体：AGPL-3.0（见 LICENSE）。
- 卡组代码格式实现参考 [PJ1059/KARDS-Deck-Tracker](https://github.com/PJ1059/KARDS-Deck-Tracker)
  （GPL-3.0, © 2026 PJ1059），独立重写；AGPL-3.0 与其兼容合并分发。
- KARDS 为 Playfully Distributed 商标，本插件与其无隶属、赞助或授权关系。

## 开发
`python -m pytest tests`（需 Python 3.11 + pytest/numpy/pillow；宿主冻结运行时即 3.11）。
