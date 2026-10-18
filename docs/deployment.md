# 部署与安装同步（kards_companion）

本文件说明 `scripts/sync_installations.py` 的用途、边界和正式发布门禁。脚本默认
`--check` 只读；任何写入都必须显式 `--apply`。脚本不启动宿主、不访问外网，也不
读取或修改目标 `config`、运行时状态、身份文件。

## 目标目录

源目录固定为脚本父目录的父目录（即本仓库根目录）：

| 通道 | 安装副本 |
| --- | --- |
| dev | `%LOCALAPPDATA%\N.E.K.O\plugins\kards_companion_dev` |
| dev | `%LOCALAPPDATA%\N.E.K.O\.neko-plugin-installations\plugins\kards_companion_dev` |
| formal | `%LOCALAPPDATA%\N.E.K.O\plugins\kards_companion` |
| formal | `%LOCALAPPDATA%\N.E.K.O\.neko-plugin-installations\plugins\kards_companion` |

`LOCALAPPDATA` 缺失或不是绝对路径时直接拒绝，绝不退化成相对路径。

## 复制范围

只复制以下明确 allowlist 中的项目文件/包：

```text
__init__.py  plugin.toml  README.md  requirements.txt(如存在)
adapters/ delivery/ policy/ runtime/ state/ knowledge/ perception/
ui/ tests/ docs/ scripts/
```

递归复制时跳过 `__pycache__`、`.pytest_cache`、`.pyc`、`env`、`data`、`config`、
`model/models`、`dataset`、`outputs`、`records`。因此：

- `models/` 模型权重不复制、不覆盖；
- 空 `templates/` 不复制；
- 目标侧 `config/`、运行时状态、身份文件、未知文件一律保留；
- 目标侧存在但源侧没有的文件只记录为 `stale`，不删除、不替换；
- 目标目录本身及其父链只要出现 symlink / junction / reparse point 就拒绝；
- 目标已有 `plugin.toml` 时，`[plugin].id` 必须与当前通道一致。

源树中出现 symlink / junction / reparse point 同样拒绝，避免把链接内容当作源码复制。

### dev 清单派生

dev 的 `plugin.toml` 从源清单派生，只替换三个字段：

```text
[plugin].id    = kards_companion_dev
[plugin].name  = KARDS 猫娘陪玩（dev）
[plugin].entry = plugin.plugins.kards_companion_dev:KardsCompanionPlugin
```

不会把源清单里的正式 id 复制到 dev。`version` 当前保持源值；`6.0.0` 属于后续
最终集成时的版本写入，本脚本现在不改版本。

### 内置卡牌库

复制前先解析 `knowledge/cards_builtin.json` 的 JSON shape，再校验：

- `cards` 为列表且长度为 `1083`；
- 文件 SHA-256 必须为
  `dc06efb1180646e15953b73ccb9dc0683b83676bd0062d1b8ff5831771ec4661`。

任一条件不满足即拒绝，不写入目标。未来若上游卡库更新，必须显式修改脚本常量并
重新验收，不能静默漂移。

## 只读检查与写入

```bat
py -3.11 scripts\sync_installations.py --help
py -3.11 scripts\sync_installations.py --check
py -3.11 scripts\sync_installations.py --check --channel formal
```

`--check` 输出 JSON 到 stdout，包含每个目标的 `changes`（含 source/target hash）、
`stale`、`issues`、`config_hashes`、源/目标差异数量；不写任何备份或报告文件。

dev 写入：

```bat
py -3.11 scripts\sync_installations.py --apply --channel dev
```

正式写入只在真实宿主验收通过后执行：

```bat
py -3.11 scripts\sync_installations.py --apply --channel formal ^
  --host-verified --acceptance-report <验收报告.json>
```

写入前请关闭 N.E.K.O。脚本本身不会启动宿主，也不会停止宿主。

每次 `--apply`：

1. 先做完整 preflight；任何 issue 都拒绝，不写目标文件；
2. 备份“将变更且已存在于目标”的 allowlist 文件到
   `%TEMP%\neko-kards-sync-<channel>-<timestamp>\backup\<target-label>\`；
   新文件没有旧内容，不需要备份；
3. 备份前对目标侧将变更的 `.py` 做静态 secret 检测；任何命中或无法扫描都拒绝
   备份和写入。JSON/卡库/其他资产不按密钥扫描，避免把 `assets`/卡牌字段误判；
4. 每个文件先在目标同目录写临时文件，再 `os.replace` 原子替换；
5. 写后再次检查 allowlist 差异和 `config/` hash，必须全部通过；
6. 报告只写到上述 Temp 目录的 `report.json`；check 模式不写报告。

目标中未知文件始终不删除；它们会出现在 `stale` 列表里。目标 `config/` 内容不会被
打印，只记录 hash 并在写后比较。

## 正式门禁（formal）

`--host-verified` 只是执行者的声明，不是证明。脚本在 `--apply --channel formal` 时
还要求真实验收证据，并检查：

- `plugin_loaded` 为 true；
- `delivery.submitted` 为 true；
- `sdk.accepted` 为 true；
- `source_manifest` / `artifact` fingerprint 与当前源 artifact 完全一致；
- 如报告带 `plugin_id`，必须是 `kards_companion_dev`；
- 两个 dev 安装副本已与当前源同步，否则拒绝晋级 formal。

支持的证据格式（二者择一，不能同时给）：

1. `--acceptance-report`：schema 以 `/1` 结尾的真实报告，例如

```json
{
  "schema": "neko-plugin-acceptance/1",
  "plugin_loaded": true,
  "plugin_id": "kards_companion_dev",
  "delivery": {"submitted": true},
  "sdk": {"accepted": true},
  "source_manifest": {"fingerprint": "<artifact fingerprint>"},
  "artifact": {"sha256": "<artifact fingerprint>"}
}
```

2. `--verified-manifest`：简化扁平 JSON，至少包含

```json
{
  "plugin_loaded": true,
  "delivery_submitted": true,
  "sdk_accepted": true,
  "source_manifest_fingerprint": "<artifact fingerprint>",
  "plugin_id": "kards_companion_dev"
}
```

如果真实宿主验收报告没有上述 schema/字段，formal 会被拒绝并给出原因（例如
`acceptance_schema_not_v1`、`plugin_loaded_not_true`、
`source_manifest_fingerprint_missing`、`artifact_hash_mismatch`），不会写入任何
formal 文件。不得伪造验收报告；证据必须来自真实宿主运行。当前
`scripts/host_acceptance.py` 生成的 `neko-host-acceptance/1` 报告不包含
`plugin_loaded`、`delivery`、`sdk`、`source_manifest` 字段，因此不能直接作为
formal 证据；需要由真实验收流程补齐上述字段后再生效。

### artifact fingerprint

为保证 dev 验收结果可复用到 formal，fingerprint **不包含** 通道相关的
`plugin.toml`，算法为：

```text
sha256( 对每个 allowlist 文件按相对路径排序:
        utf8(relpath) + b"\0" + utf8(sha256(file)) + b"\n" )
```

代码、文档、测试、脚本等都在内；`plugin.toml` 因 id/name/entry 随通道变化而排除。
正式验收报告里的 `source_manifest` 与 `artifact.sha256` 都应使用该值。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | check 完成，或 apply 成功 |
| 2 | preflight/源校验/secret/卡库/目标安全检查失败，未写目标 |
| 3 | formal 门禁失败，未写目标 |
| 4 | 写入后校验失败（需人工检查报告） |

## 最终集成后的顺序

1. `--check` 确认 dev 差异只包含预期新增模块；
2. `--apply --channel dev`，确认两个 dev 副本一致；
3. 关闭/重启宿主，跑真实 dev 验收，取得带 `plugin_loaded`、
   `delivery.submitted`、`sdk.accepted` 和当前 fingerprint 的真实证据；
4. 证据通过后，才执行 formal apply；仍然只改 allowlist 文件，保留目标
   config/state/identity；
5. 版本 `6.0.0` 在最终集成时另行写入，本脚本不越权修改。
