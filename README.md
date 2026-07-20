# Codex 本地会话恢复工具

这是一个面向 Windows 的本地恢复工具，用来查找和修复 Codex 桌面版、CLI 留在本机的会话。

工具只使用 Python 标准库，不访问网络，不发送聊天消息，也不会读取、复制或修改 `auth.json` 中的登录令牌。

## 最简单的使用方法

双击：

```text
恢复Codex会话.cmd
```

菜单中有三个操作：

1. **扫描本地会话**：只读，不修改任何文件。
2. **恢复到 ChatGPT 官方服务**：先备份，再修复会话文件、数据库、索引和 `config.toml`。
3. **验证恢复结果**：只读，检查是否仍有未恢复项。

推荐顺序：

1. 先执行“扫描本地会话”。
2. 完全退出 Codex，包括托盘和后台的 `Codex.exe`。
3. 再双击工具，执行“恢复到 ChatGPT 官方服务”。
4. 看到“恢复与验证全部通过”后重新启动 Codex。
5. 最后执行一次“验证恢复结果”。

运行要求是 Python 3.11 或更高版本。启动器优先使用 Codex 自带的 Python，其次使用系统 `py -3`；它会跳过 Windows 应用商店的 `WindowsApps\python.exe` 占位符。

`恢复Codex会话.cmd` 必须保持 Windows CRLF 换行。LF-only 批处理在部分 Windows 区域设置下会被 `cmd.exe` 错误拆词，表现为双击后无输出或一直挂起。

## 命令行用法

```powershell
python recover_codex_sessions.py scan
python recover_codex_sessions.py repair
python recover_codex_sessions.py verify
```

指定其他 Codex 数据目录：

```powershell
python recover_codex_sessions.py --codex-home .\saved-codex-home scan
```

指定备份位置：

```powershell
python recover_codex_sessions.py repair --backup-root .\CodexBackups
```

不保留旧 `custom` 名称的官方兼容入口：

```powershell
python recover_codex_sessions.py repair --no-compat-alias
```

这个选项只适合已经完全退出 Codex、并且确认所有 rollout 和数据库元数据都能稳定迁移到 `openai` 的情况。通常建议保留默认兼容入口。

## 工具会找哪些内容

扫描范围：

- `%USERPROFILE%\.codex\sessions\**\*.jsonl`
- `%USERPROFILE%\.codex\archived_sessions\**\*.jsonl`
- `%USERPROFILE%\.codex\state_5.sqlite`
- `%USERPROFILE%\.codex\session_index.jsonl`
- `%USERPROFILE%\.codex\config.toml`

识别顺序：

1. 优先使用 rollout 文件名里的 UUID 作为会话 ID。
2. 文件名没有 UUID 时，才使用 `session_meta` 中的 ID。
3. 标题优先取 `session_index.jsonl` 的最新名称。
4. 索引没有标题时，使用数据库标题。
5. 两者都没有时，从第一条用户消息生成短标题。

文件名 UUID 必须优先。派生子任务的首个 `session_meta` 有时会暂时携带父会话 ID，如果只相信首行元数据，就会把两个不同会话错误合并。

## 恢复时会修改什么

### 1. 完整备份

恢复前会在下面的位置创建带时间戳的备份：

```text
%USERPROFILE%\.codex\session-recovery-backups\YYYYMMDD-HHMMSS
```

备份包含：

- 所有找到的活动和归档 rollout 文件
- `config.toml`
- `session_index.jsonl`
- `state_5.sqlite` 及其 WAL、SHM 文件
- `manifest.json`，记录每个备份文件的大小和 SHA-256

工具不会备份 `auth.json`，因为恢复过程不需要修改认证信息，额外复制令牌反而会增加泄露风险。

### 2. 会话 JSONL

工具只修改这种结构化元数据：

```json
{"type":"session_meta","payload":{"model_provider":"custom"}}
```

其中的 provider 会迁移为：

```json
{"type":"session_meta","payload":{"model_provider":"openai"}}
```

注意事项：

- 只处理 `type=session_meta` 的直接字段。
- 聊天正文、命令输出或文档里即使出现文字 `model_provider: custom`，也不会被替换。
- 修改通过同目录临时文件和原子替换完成。
- 如果识别数量与实际替换数量不一致，工具会停止，不会继续更新数据库。

### 3. 归档会话

如果 rollout 只存在于 `archived_sessions`：

- 工具会把它复制回按年月日组织的 `sessions` 目录。
- 原归档副本不会删除。
- 如果活动目录已有同名同内容文件，会直接复用。
- 如果同名文件内容不同，工具会停止，防止覆盖。

### 4. SQLite 数据库

`state_5.sqlite` 中的 `threads` 表会进行以下修复：

- 已有会话的 `model_provider` 改为 `openai`。
- `archived` 改为 `0`，并清空 `archived_at`。
- `rollout_path` 指向实际活动文件。
- 缺少数据库记录但存在完整 JSONL 的会话会补建记录。
- 标题为空时补回标题。

新增记录只使用保守默认值，例如只读沙箱和 `on-request` 审批策略。会话重新打开后，Codex 会根据当前版本继续更新其运行元数据。

### 5. 标题索引

如果 `session_index.jsonl` 缺少会话或标题不同，工具会追加一条最新索引记录，不会重写旧索引历史。

### 6. config.toml

默认恢复模式会移除已有的 `[model_providers.custom]` 块，然后添加下面的兼容定义：

```toml
[model_providers.custom]
name = "ChatGPT Official (legacy compatibility)"
base_url = "https://chatgpt.com/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
```

这里的 `custom` 只是旧会话保存下来的历史标识，不代表第三方供应商。实际请求地址是 ChatGPT 官方域名，认证使用当前 Codex/ChatGPT 登录。

保留这个兼容入口的原因是：运行中的 Codex 可能缓存旧 provider，并把数据库里的 `openai` 再写回 `custom`。兼容入口能避免这种缓存导致旧会话再次出现：

```text
Model provider `custom` not found
```

## 为什么恢复时必须退出 Codex

这是最重要的注意事项。

Codex 运行时会同时占用或缓存：

- rollout JSONL
- `state_5.sqlite`
- provider 配置
- 会话标题与归档状态

在 Codex 运行时直接修改可能产生以下问题：

- Windows 文件锁阻止原子替换。
- SQLite WAL 中的旧数据重新覆盖主数据库。
- 内存里的 `custom` provider 被再次写回数据库。
- 页面保留之前的加载失败状态，看起来像修复没有生效。

因此恢复模式默认检测到 `Codex.exe` 就停止。`--allow-running` 只用于有完整备份、清楚风险的手工排障，不建议日常使用。

## 验证标准

`verify` 会检查：

- 所有找到的会话都有数据库记录。
- 活动会话没有仍被标记为归档。
- 结构化 `session_meta` 中没有 `custom` provider。
- 数据库中没有 `model_provider=custom`。
- 数据库记录的 rollout 文件真实存在。
- `config.toml` 可以被 TOML 解析。
- 如果保留 `custom` 兼容入口，它必须指向 `https://chatgpt.com/backend-api/codex`。

只有全部检查通过，工具才返回退出码 `0`。

退出码：

- `0`：成功。
- `1`：没有发现可恢复会话。
- `2`：参数、目录、运行状态或前置条件错误。
- `3`：完成操作但验证仍发现问题。

## 无法恢复的情况

以下情况不能凭空恢复正文：

- `session_index.jsonl` 里只有标题和 ID，但没有对应 JSONL。
- 数据库里只有线程记录，但 `rollout_path` 文件已经删除。
- JSONL 被截断，并且需要的消息行已经不存在。
- 磁盘清理工具已经覆盖原文件，且没有其他备份。

工具会把“只有索引、没有文件”的条目列出来，但不会创建一个空白假会话冒充原内容。

如果在其他磁盘、旧用户目录或系统备份中找到对应 rollout，可以把它放回 `sessions` 或 `archived_sessions`，再重新扫描和恢复。

## 如何回滚

1. 完全退出 Codex。
2. 打开最近的 `session-recovery-backups\时间戳`。
3. 根据 `manifest.json` 确认备份完整。
4. 把备份中的相对路径复制回 `%USERPROFILE%\.codex`。
5. 重新启动 Codex。

回滚时优先恢复：

```text
config.toml
state_5.sqlite
state_5.sqlite-wal
state_5.sqlite-shm
session_index.jsonl
sessions\...
archived_sessions\...
```

不要把不同时间点的 SQLite 主文件、WAL 和 SHM 混用。

## 版本兼容说明

Codex 的 `state_5.sqlite`、rollout JSONL 和本地索引属于应用内部存储格式，不应视为长期不变的公开 API。本工具已按当前机器上的 Codex 数据结构和真实恢复行为验证，但未来版本可能调整表结构、字段名称或归档方式。

为避免在未知版本上静默破坏数据，工具采取以下策略：

- 永远先备份再修改。
- 遇到未知的数据库必填列时停止新增记录。
- TOML 无法解析时停止替换配置。
- JSONL 元数据识别数和替换数不一致时停止。
- 同名 rollout 内容冲突时停止，不覆盖任一副本。
- 建议每次 Codex 大版本升级后先运行 `scan` 和隔离测试，再决定是否执行 `repair`。

当前环境无法通过代理获取官方 Codex 手册，因此本工具没有把内部存储细节描述成官方稳定承诺；实现依据是本机当前 schema、已有 rollout 和实际成功加载结果。

## 开发与测试

运行测试：

```powershell
python -m unittest discover -s tests -v
```

测试全部使用 `tests\fixtures` 下的隔离数据，不读取或修改真实 `%USERPROFILE%\.codex`。覆盖场景包括：

- 活动与归档会话发现
- 只有标题的残留索引
- 派生子任务继承父 ID
- `custom` 到 `openai` 的精确迁移
- 正文中的 `custom` 字样保持不变
- 缺失数据库记录重建
- 第三方 provider 配置替换为 ChatGPT 官方兼容入口
- 最终验证失败报告
