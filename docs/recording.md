# 局内临时帧录制（Temporary Frame Recording）

- 日期：2026-10-17
- 模块：`runtime/recording.py`（唯一实现，不 import `perception.capture`）
- 测试：`tests/test_recording.py`
- 默认：**关闭**；未启用且无有效帧时不创建任何录制目录。
- 非目标：不训练 YOLO、不生成 source video、不做 OCR、不向宿主 push、不改变 dry-run 语义。

本功能只做一件事：用户显式开启后，把**当前局**的采集帧以原始分辨率、JPEG 质量 85（可配）
写入插件自有临时目录，达到容量上限时滚动删除本局最早帧，并在局末/停止/关机时清理本局。
所有文件都是本功能刚生成的临时帧，删除范围仅限 `root` 下由本模块创建并通过
manifest 证明归属的 session 目录。

---

## 1. 冻结 API

```python
from pathlib import Path
from .runtime.recording import RecordingConfig, TemporaryFrameRecorder

config = RecordingConfig(
    enabled=False,
    max_bytes=2048 * 1024**2,
    jpeg_quality=85,
    min_free_bytes=256 * 1024**2,
    queue_size=4,
)
recorder = TemporaryFrameRecorder(
    root: Path,
    config=config,          # None -> RecordingConfig()
    clock=time.time,
    logger=None,
)
```

| 方法 | 语义 |
| --- | --- |
| `update_config(config)` | 热更新配置。关闭录制会立即作废队列与在途编码；已写入文件保留到本局 `end_match`/`stop`。支持 `RecordingConfig`、dict 或 `None`。 |
| `start_match(session_id, match_id)` | 开新局。会先清理上一局临时文件并重试所有 pending 清理；若仍有未清理目录则拒绝开新局（不覆盖、不建目录）。只记录元数据，不启动 worker。空 session_id 拒绝。 |
| `set_active(active, reason="")` | 暂停/恢复写入。置 false 会清空待写队列并提升 activity epoch，在途编码不会落盘。 |
| `submit(frame) -> bool` | 非阻塞入队。`False` = 未接受（disabled/inactive/重复/队列满/无效帧）。图像在提交线程内同步 `copy()`，之后调用方可复用/修改原图。 |
| `end_match(reason="match_end")` | 作废本局 generation，清空待写队列，删除本局 JPEG 与 manifest。worker 保留，下一局复用。 |
| `stop(reason="stopped")` | 作废 generation，停止 worker（最多等 `WORKER_JOIN_SECONDS=2s`），清理本局。join 超时不会报告 clean。 |
| `snapshot() -> dict` | 只含计数/短字符串，不含绝对路径、图片或帧内容。 |
| `cleanup_orphans() -> dict` | 清理上次异常退出遗留的、已证明归属的 session；不创建 root。 |

`frame` 为 duck type，只读取：`.image`、`.frame_id`、`.captured_at`、`.width`、
`.height`、`.backend`。因此本模块与 `CaptureResult` 没有 import 依赖。

### `submit` 的接受条件

1. `config.enabled is True`；
2. `active is True` 且存在本局 session；
3. `frame.image` 非空且可复制；
4. 本局内未出现过相同的 `captured_at`（正数时以时间戳为身份；否则退化到 `frame_id`）；
5. 有界队列未满（满了丢**新**帧并计数，不阻塞采集）。

`set_active(False)` 只在调用方确实判定画面不可用时使用；`unknown`/最小化/无有效帧时应由
集成层调用。录制器本身不猜测场景，也不把“上一次有效帧”重复写入。

---

## 2. 磁盘布局与归属

```text
<root>/
  .kards-frame-recorder.json        # root 归属标记（magic/version/owner/plugin_owned）
  <session_token>/                  # uuid4().hex，32 位小写十六进制
                                    # 清理也接受标准 36 位带连字符 UUID 目录
    manifest.json                   # 插件归属 + session + match + config
    frame-00000001.jpg              # 原始分辨率 JPEG
    frame-00000002.jpg
```

`manifest.json` 至少包含：

```json
{
  "plugin_owned": true,
  "magic": "kards_companion.temporary_frame_recorder",
  "version": 1,
  "owner": "kards_companion",
  "session_id": "调用方 session（截断，仅元数据）",
  "session": "同 session_id（兼容别名）",
  "session_token": "32位目录名",
  "match_id": "调用方局号（字符串）",
  "created_at": 0.0,
  "config": {"enabled": true, "max_bytes": 2147483648,
             "jpeg_quality": 85, "min_free_bytes": 268435456,
             "queue_size": 4}
}
```

- 不额外写每帧 JSONL：逐帧元数据写放大会拖慢采集。帧顺序由 `frame-XXXXXXXX.jpg`
  序号表达，manifest 是唯一的 session 元数据文件，其字节数计入容量预算。
- `session_id`/`match_id` 只写入 manifest，不参与路径拼接；目录名永远来自随机 UUID。
- root 必须是真实目录，不能是 symlink/junction/reparse point；session 目录在每次写入和
  清理前都会重新检查非 reparse。未知文件/目录一律不删除，只在日志中警告。
- root 归属标记是额外的声明；**清理的归属证明以每个 session 的 manifest 为准**。
  标记缺失仍可清理合法 manifest 的 session；标记存在但损坏时拒绝操作。

---

## 3. 容量、磁盘与编码

- 编码：PIL `image.save(BytesIO, format="JPEG", quality=config.jpeg_quality)`；不缩放、
  不改变分辨率。非 `RGB/L` 模式先 `convert("RGB")`（尺寸不变）。
- 预算：`max_bytes` 同时覆盖 JPEG 与 `manifest.json`，是**用户可自定义**的预算。
  默认 2 GiB 只是默认值，不是硬上限；例如用户可选 64 GiB（65536 MiB）或更大。
- 滚动：写入新帧前，如果 `当前字节 + 新帧` 超过 `max_bytes`，按写入顺序删除**本 session
  已写入**的最早帧，直到能放下；不会删除未知文件或其它 session。若删完所有已写帧仍放不下
  新帧，则直接丢弃新帧，不先删旧帧。
- 单帧大于 `max_bytes`：直接丢弃，不自动降 quality、不 resize。
- 磁盘余量：写任何文件前检查 `free - incoming >= min_free_bytes`。检查失败/探针异常时
  丢弃该帧并记录 `disk_margin`/`disk_unavailable`；不会先删旧帧再失败。
- 原子写：先写同目录 `.tmp-<uuid>.<suffix>`，再 `os.replace` 到最终文件。临时文件只在
  session 目录内。
- 文件创建只发生在 `root` 及其 UUID session 子目录内。

---

## 4. 并发与延迟写入

- generation：每次 `start_match`/`end_match`/`stop` 递增；activity epoch：每次
  `set_active`/禁用递增。每个 job 携带两者与 session token。
- worker 在编码前、编码后、rename 前都校验 generation/epoch/session。即使编码正在
  进行中，`end_match`/`stop` 之后也不会把迟到的帧写进下一局。
- `end_match` 会清空队列（计入 `cancelled`），等待当前 rename 的 I/O 锁后删除本局目录。
  在途编码完成后校验失败，只计数为 `late_dropped`。
- `stop` 先作废 generation，再发停止哨兵并 join；join 超时置 `clean=false`、
  `error=worker_stop_timeout`，但 generation 已失效，不会 late-write。
- 清理失败的 session 会记入 pending；`start_match` 在创建新局前先重试所有 pending，
  若仍失败则拒绝开新局（`reason=start_blocked_pending_cleanup`，保留 pending，不覆盖）；
  后续 `stop` 也会重试。只要仍有 pending 或上次 stop 未证明干净，重复 `stop` 不会把
  `clean` 改成 true。
- 容量滚动只由单个 worker 在 I/O 锁内执行；`submit` 只做图像复制与 `put_nowait`，
  不会等待编码或磁盘。

---

## 5. `snapshot()` 字段（无路径、无图片）

```text
enabled active state reason error session
queued inflight pending bytes max_bytes
captured written files dropped
queue_dropped duplicate_dropped late_dropped error_dropped
oversize_dropped budget_dropped disk_dropped cancelled clean
```

- `state`：`disabled | idle | recording | paused | stopped`。
- `session`：当前 session token 前 8 位；无启用中的 session 时为空。
- `queued` = 有界队列中等待的帧数（不超过 `queue_size`）；
  `inflight` = 正在编码的帧数；`pending` = `queued + inflight`（等待落盘的总帧数，
  可用于等待队列排空）。
- `captured` 为本局接受的帧数；`written` 为本局成功 rename 的累计数；
  `files` 为滚动删除后当前保留的帧文件数；`dropped` 为丢弃总数。
- `bytes` 包含当前 session 的 manifest 和保留的 JPEG；`max_bytes` 为当前配置。
- `clean` 表示最后一次清理是否干净（stop 超时、目录中留下未知文件时为 false）。

---

## 6. 集成接口（给 main / UI）

以下调用点均已确认存在于当前 `__init__.py` 的感知/生命周期链；录制器不修改这些文件，
由 main ID 接线。

### 6.1 构造（`KardsCompanionPlugin.__init__`）

```python
from pathlib import Path
from .runtime.recording import RecordingConfig, TemporaryFrameRecorder

self.recorder = TemporaryFrameRecorder(
    Path(self.data_path("recording")),      # root；只需传路径，不要在构造时 mkdir
    config=RecordingConfig(
        enabled=bool(self._cfg.get("recording_enabled", False)),
        max_bytes=max(0, int(float(self._cfg.get("max_mb", 2048)) * 1024 * 1024)),
    ),
    logger=self.logger,
)
```

`data_path` 只用于得到插件数据目录下的路径；录制器在真正写入第一帧时才创建 root，
所以默认关闭时不会创建 `recording/`。

### 6.2 配置（面板字段）

在 `KardsSettings` / `_CONFIG_DEFAULTS` 中增加：

```python
recording_enabled: bool = SettingsField(
    False, hot=True, description="局内临时帧录制（默认关闭）")
max_mb: int = SettingsField(
    2048, hot=True, ge=16, le=65536,
    description="临时帧总容量 MiB（含 manifest；默认 2048，可自定义，2GB 不是硬上限）")
```

默认 `recording_enabled=false`、`max_mb=2048`。`jpeg_quality`/`min_free_bytes`/`queue_size`
保留 `RecordingConfig` 默认值（85 / 256MiB / 4），面板仅暴露上面两项。
建议面板文案：**“录制容量（默认 2048 MiB，可自定义；2 GB 只是默认值，不是硬上限）”。**
`le=65536` 只是建议的面板范围（64 GiB），不是录制器限制；`RecordingConfig.max_bytes`
没有 2 GiB 上限。

`_reload_config()` / `config_change` 应用配置后：

```python
self.recorder.update_config(RecordingConfig(
    enabled=_boolean(self._cfg.get("recording_enabled"), False),
    max_bytes=max(0, int(float(self._cfg.get("max_mb", 2048)) * 1024 * 1024)),
))
```

录制与 `dry_run` **完全独立**：dry-run 只控制 LLM 投递 gate；只要用户开启
`recording_enabled`，录制照常进行。不要用 `dry_run` 门控 `submit`。

### 6.3 startup

```python
@lifecycle(id="startup")
async def startup(self, **_):
    await self._reload_config()
    try:
        self.recorder.cleanup_orphans()     # 只读/删旧归属目录；不会创建 root
    except Exception:
        pass
    ...
```

`cleanup_orphans` 返回 `{"status", "removed", "skipped", "bytes", "error"}`；
`status` 为 `ok | missing | refused | busy | error`。可以只记录日志。
root 归属标记缺失时，仍以每个 session 的 manifest 作为归属证明；root 归属标记存在但
损坏/不匹配时返回 `refused`，不做任何删除。

### 6.4 每帧：`_perceive_once`

在 `_perceive_once` 中：

1. **无有效帧分支**（`result is None or not health.reading`）：在返回前
   `self.recorder.set_active(False, reason)`，`reason` 用
   `health.reason or "frame_unavailable"`。这样最小化/unknown/无帧会立即停止写，
   并丢弃尚未编码的旧帧。
2. **有效帧分支**：只对局内帧写入。分类得到 `scene`、`pre["allow_diff"]` 后：

```python
# pre = self.scenario.observe_scene(...) 的结果；不要在 _lifecycle_lock 内做录制 I/O
in_match_frame = scene == SCENE_MATCH and bool(pre.get("allow_diff"))

if in_match_frame:
    if pre.get("new_match") or not self._recording_match_live:
        # start_match 不建目录；即使录制关闭也可安全调用，便于中途开启
        self.recorder.start_match(
            self._session_id,
            self.tracker.game_number,
        )
        self._recording_match_live = True
    self.recorder.set_active(True, "in_match")
    self.recorder.submit(result)          # 同步 copy，不会阻塞采集
else:
    self.recorder.set_active(
        False, "scene_menu" if scene == SCENE_MENU else f"scene_{scene}"
    )
```

- `start_match`/`end_match`/`stop` 会做少量文件 I/O；必须在
  `self._lifecycle_lock` 之外调用（与当前“host-facing work outside lock”原则一致）。
- `submit` 内部复制图像，因此可以在分类/HUD/detector 之后提交，但必须在原图被复用前。
- 只有 `SCENE_MATCH` 且 `allow_diff` 的帧才提交；menu/draft/unknown 调到 false，
  不提交，绝不会把上一张有效帧重复写入。
- 插件从局中启动、没有观察到 `new_match` 时，用 `_recording_match_live` 的首帧兜底，
  保证“本局中途开始监控”也能从当前可信帧开始录。

### 6.5 局末（victory/defeat、menu 3 帧）

`_perceive_once` 已经在锁外计算：

```python
terminal = any(event.kind == E_MATCH_END for event in events)
confirmed_exit = bool(scene_snapshot.get("confirmed_exit"))
```

在这两个条件首次成立时（与现有 purge/expire 同一段锁外逻辑）：

```python
if terminal or confirmed_exit:
    self.recorder.end_match("match_ended" if terminal else "confirmed_exit")
    self._recording_match_live = False
```

`invalid_context`（unknown/draft）只暂停，不 `end_match`；必须等确认离场/终局。

### 6.6 手动暂停/恢复

- `_pause_pipeline_serialized` / `monitor_pause`：
  `self.recorder.set_active(False, "user_pause")`。
- `monitor_resume`：不需要恢复写入；下一次可信局内帧执行 `set_active(True, "in_match")`。
- 暂停期间保持 session 与已写文件，只是不再提交；`end_match` 时才清理本局。

### 6.7 monitor stop / shutdown

`_stop_pipeline_serialized` 与 `shutdown` 中，在锁外调用：

```python
self.recorder.stop("user_stop" if reason != "shutdown" else "shutdown")
self._recording_match_live = False
```

`stop` 会清除本局并 join worker；worker 可在下一次 `start_match` + `submit` 时惰性重启。
如果 stop 返回时 `snapshot()["clean"] is False`，应把 `error`（例如
`worker_stop_timeout`）写入诊断，不要把停止报告成 clean。

### 6.8 面板状态（可选）

`monitor_status` / `monitor_diagnostics` 可以直接附带
`self.recorder.snapshot()`；字段已经不含路径/图片，可原样返回。若要展示容量，
使用 `bytes / max_bytes`、`written`、`dropped`、`state`、`clean`。

---

## 7. 清理规则与安全边界

### `end_match` / `stop`

只处理**当前 session**：

1. 提升 generation，active=false，清空队列；
2. 校验 root/session 真实目录、非 reparse、目录名为 32 位 UUID、manifest 归属有效；
3. 只删除本 session 下的 `manifest.json`、`frame-XXXXXXXX.jpg`、`.tmp-<uuid>.jpg/json`；
4. 目录为空才 `rmdir`；有未知文件/子目录则保留并告警，`clean=false`。

不删除 root 本身、root 归属标记、其它 session、原始视频、训练数据、`raw/` 等目录。

### `cleanup_orphans`

- root 不存在：`status="missing"`，不创建；
- root 是文件/reparse：`status="refused"`；
- root 归属标记缺失：允许继续，以每个 session 的 manifest 作为归属证明；
- root 归属标记存在但 magic/version/owner 不匹配：`status="refused"`，不删除；
- 只扫描 root 下一层：目录名必须是 32 位小写 UUID 或标准 36 位带连字符 UUID、
  非 reparse、含有效 manifest；
  manifest 的 `session_token` 必须等于目录名，`magic/version/owner/plugin_owned` 必须匹配；
- 跳过当前活动 session；
- 未知目录/文件、无 manifest 的目录、manifest token 不匹配的目录一律保留并
  `skipped` 计数；
- 只删除上述已知文件名，目录为空才 `rmdir`；不使用 `rmtree` 删除任意用户目录。

### 路径穿越

- `session_id`、`match_id` 只作为 manifest 文本，截断后写入；
- session 目录名始终是 `uuid.uuid4().hex`；
- 不存在接收“删除路径”的公开 API；
- 所有删除目标都在 `root/<32hex>` 下，并经过名称、父目录、manifest 与非 reparse 校验。

---

## 8. 测试覆盖（`tests/test_recording.py`）

```bash
python -m pytest tests/test_recording.py -q
```

覆盖：

- 禁用时零输出、不创建 root；
- 原始分辨率 + 指定 JPEG quality（与同参数 PIL 输出逐字节一致）；
- 重复 timestamp 只写一次；
- 容量滚动并删除最早帧、总字节含 manifest 且不超预算；
- 64 GiB 用户自定义预算可用，2 GiB 仅为默认值；
- 预算缩小且新帧无法放下时不误删旧帧；
- 单帧超容量直接丢弃（不降 quality/resize）；
- `_free_disk_bytes` 为零/异常时的磁盘余量拒绝；
- 手动暂停取消在途/排队帧；
- 有界队列满时丢新帧且 `submit` 不阻塞；
- `end_match` 后迟到 worker 不写入新 session；
- `start_match` 先重试 pending；仍失败时拒绝开新局，清理完成后才用新 token 开局；
- 新局清理旧局；
- `stop` join 并清理；join 超时报告 `clean=false`；
- `cleanup_orphans` 只删归属 UUID session，未知目录保留；root 标记缺失但 session
  manifest 有归属时仍可清理；
- symlink/junction session 与 symlink root 拒绝；
- session_id 路径穿越不产生 root 外文件；
- 配置启停、图像 copy 安全、snapshot 无路径/图片。

---

## 9. 已知边界

- 本模块不判断“是否在局内”，只依赖集成层按上述时机调用 `start_match`/`set_active`/
  `submit`/`end_match`；如果集成层在非局内调用 `submit`，录制器不会自行识别场景。
- 孤儿清理的归属依赖每个 session 的 manifest 自证（magic/version/owner/token）+ UUID
  目录名 + 非 reparse；没有跨进程文件锁，同一 root 只应由一个插件实例使用，属低风险
  已知边界。
- 不生成逐帧 JSONL；如需帧级审计，应在后续版本以稀疏索引方式单独设计，避免每帧
  额外 write 拖慢采集。
