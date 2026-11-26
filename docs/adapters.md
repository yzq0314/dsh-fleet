# 接入 Agent

`packages/protocol/adapters.py` 定义 `AgentAdapter`、`TaskRequest`、`Event` 和三种审批决策。Sidecar 启动时加载内置适配器，每次任务从 `REGISTRY` 建立独立实例。自定义适配器由部署者安装到节点，并在启动模块中显式导入注册模块。PWA 和 Hub 传递同一个 agent 名称。

## 适配器契约

1. 在 `async run(task, emit, authorize)` 中启动任务。使用 argv 数组和明确工作目录，将提示词经 stdin 或受支持的结构化协议传入。
2. 把工具名、实际参数及解析后的目标路径送进执行前钩子。调用 `await authorize(command, paths, high_risk)`，只有返回 true 才能执行该操作。解析不出的工具需要安全的默认处理。来自进程 stdout 的已完成事件只用于展示。
3. 用 `emit("tool_call", data)`、`emit("log", {"text": ...})`、`emit("result", {"text": ...})` 输出结构化记录。控制生命周期的状态、审批 ID 和事件序号由 Sidecar 生成。
4. 实现可重复调用的 `async cancel()`，等待子进程树退出并释放管道、句柄与会话。实现特定 CLI 的 `parse` 可映射外部事件。默认 `approval_response` 只解析对应审批 Future。
5. 调用 `register("name", YourAdapter)`。在可信目录、越界访问、双重审批、超时、取消和 CLI 退出时都运行测试。

## 内置 DSH

Sidecar 通过 `sdk-minimal` 的逐行 JSON-RPC 驱动真实 DSH。每个任务使用独立的 DSH_HOME、配置 patch 和随机认证的本机审批通道。启动时确认 SDK 身份和执行前插件握手，再提交 prompt。模型优先采用明确指定的路由，其次复用桌面或 CLI 现有配置，最后读取进程环境。[桌面插件与模型路由](../integrations/dsh-app-plugin/README.md)

适配器仅开放它能核对工具实际目标的操作。当前文件读、写、编辑走路径检查和高危策略；未经建模的工具在执行前关闭。通用 shell 命令可以解析任意程序和脚本，单靠正则难以确定最终访问的资源。需要新工具时，在适配器中提供结构化参数验证与受限执行环境，并为拒绝分支加入测试。受控 DSH 环境可读取专用的模型环境变量。

在默认 `balanced` 档，Agent 用 `write` 创建脚本，再用 `fleet_run` 提交以下结构。节点在授权后读取脚本和输入的快照，将相同字节送入隔离来宾，输出写入任务事件。这里的快照摘要用于识别本次执行内容，普通脚本可以随写随跑。

```json
{"language":"pytest","script":"test_summary.py","inputs":["summary.py","data.csv"],"args":[]}
```

- 用 `uv sync --no-dev --extra sandbox` 安装 WASI Python 和 pytest。来宾只读显式输入，无宿主环境、网络和常规子进程。pytest 支持纯 Python 测试，使用固定启动参数。普通输出文件可交给经过执行前审批的 DSH 文件工具写回。
- Windows 的 `balanced` 默认使用 `windows_runner: native`。Python、Node.js 和 Windows PowerShell 脚本在本机受限进程中读取已审批的输入快照，标准输出作为工具结果返回。PowerShell 使用系统 Windows PowerShell，Node.js 使用节点账户 PATH 中的解释器。脚本需要的工作区文件须列入 `inputs`。取消任务会结束受管进程。
- Windows 可用 `windows_runner: isolated` 切回 WASI 或镜像执行。在 `sandbox_images` 下为语言填写本地 Linux 镜像的完整 sha256 ID，即可选择 Docker。镜像可通过 `docker image inspect IMAGE --format '{{.Id}}'` 核对，需包含相应解释器、支持非 root 用户且无隐式 VOLUME。其他平台继续使用 WASI 或配置的镜像。
- `autonomous` 放宽隔离来宾的大批量输入人工确认，路径授权、可配置高危规则和执行器上限继续生效。手机可中止任务与进程树。
- 在 `strict_hash` 档，用 `checks` 登记 `name`、`script` 和管理员审核版本的 `sha256`，Agent 通过 `fleet_check` 选择名称。节点复核摘要后以节点用户权限执行审核脚本，通过 `FLEET_WORKSPACE` 指示工作目录。

Mock 的 `[approval]`、`[wait]` 用于模拟审批和启动可取消的固定等待进程。`[artifact]` 可演练命名卷内的产物链接。Mock 不把提示词送进系统 shell。
