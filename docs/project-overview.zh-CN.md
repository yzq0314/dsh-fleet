# dsh-fleet 架构与功能

本文说明各组件的职责、授权范围，以及从手机派单到桌面续聊的流程。

## 1. 用户用它做什么

用户在手机上选一台电脑，把任务交给那台电脑自己的 AI Agent。代码、工作目录、桌面账号和 NAS 映射都在目标电脑上。用户在手机上看结果、批操作、预览产物，回到电脑后能在原生 DSH Desktop 继续聊。还可以从另一地的电脑、飞书通知和手机看到同一个控制中枢的状态。

部署时可按需组合 Hub、执行节点、命名卷和只读监控目标。三节点 Mock 演示可在一台电脑上运行，无需模型账号。

## 2. 系统怎样连起来

~~~text
手机 PWA ── 同源 HTTP 请求 / SSE 事件 ── Fleet Hub ── SQLite WAL
飞书 ← Hermes 私聊通知                     │
       Hermes Skill / fleetctl ── 授权 API ─┤
                                          ├─ 节点 Sidecar → 本机 DSH SDK → 工作区
                                          │               └→ 命名卷 / NAS
                                          ├─ DSH Desktop 原生插件 ↔ 已共享的桌面会话
                                          └─ 读取硬件快照 ← VM 定时器 → SSH 只读采集
~~~

| 位置 | 实现与职责 |
| --- | --- |
| packages/protocol | Python 共享模型、事件、策略与 AgentAdapter 接口。新 Agent 实现会话启动、事件转换、审批响应和取消，接入适配器注册表。已有 DSH 和 Mock。 |
| apps/fleet-hub | Python 3.11+、FastAPI、Uvicorn、httpx、aiosqlite。提供登录、设备登记、路由、事件汇聚、任务与审批查询、文件中转和 PWA 静态托管。Hub 单活，SQLite 持久化；支持服务重启恢复，多 Hub 自动故障切换尚未实现。 |
| apps/node-sidecar | 目标主机的执行代理。检查策略，启动和中止进程树，报告硬件，按卷授权提供文件。Windows 使用当前登录用户的 AtLogOn 计划任务，因此可以访问该用户的 DSH 配置和映射盘。Linux/macOS 也有 Sidecar 启动入口。 |
| integrations/dsh-app-plugin | 注册到 DSH desktop profile 的原生插件。提供节点范围凭据、桌面心跳、本机模型桥、主动共享、任务交接与后续消息投递。前端皮肤属于 DSH 自身，Fleet 手机界面独立实现。 |
| apps/web-pwa | React 18、TypeScript、Vite、Tailwind CSS、Lucide 图标和自定义 CSS。构建后由 Hub 同端口提供。manifest、Service Worker 和图标支持安装与离线外壳；手机安装需要安全上下文，执行操作需要联网。 |
| integrations/hermes、deploy、tests | Hermes Skill 和 fleetctl；配对、部署、诊断、隐私扫描、只读 SSH 采集器；pytest、桌面宿主集成测试、Playwright 移动端测试。依赖由 uv 和前端锁文件管理。 |

## 3. 一次真实派单和两种续聊入口

1. 手机选设备、工作区、Agent，可套用带参数的 Macro 或选择卷里的文件作上下文。单机模式定向派发；Auto 按标签筛在线节点，再比较活跃任务数和 CPU；Fanout 给每个目标生成独立任务，共用一个组 ID。
2. Hub 在事务中先写任务，再携稳定任务 ID 投递。Sidecar 去重并在目标机执行。模型选择依次考虑显式配置、可用桌面模型桥、受支持的本地配置和环境变量。用户已在桌面登录时，插件可以复用其模型；Hub 和浏览器无需持有模型 Key。
3. 执行前的工具调用可以触发审批。Sidecar 持有执行侧状态，Hub 转发决定并汇总结果。Hub 拉取带序号的节点增量事件，手机通过 SSE 订阅，以 Last-Event-ID 在断线后续读。
4. **手机发起，回电脑接着聊**：派 DSH 任务时勾选「在 DSH App 中续聊」。采用支持的桌面模型桥且交接成功后，任务详情显示「已加入 DSH App 对话列表」。到对应主机与工作区打开同名 Fleet 会话，或在手机任务详情的「继续此 DSH 会话」发送下一句。插件按节点和原生会话 ID 投递，沿用工作目录、可用历史与桌面原生权限。
5. **电脑上已有对话，带到手机**：在目标电脑打开那条 DSH 对话，输入 `/fleet on`，手机即可查看随后同步的事件并在插件支持时续聊。`/fleet off` 停止共享。过往桌面历史仍在原生会话中；手机没有任意遍历所有主机私有会话的总目录。
6. 一次任务完成、写入桌面对话、桌面插件在线、手机续聊成功是不同状态。手机续聊需要运行中的兼容插件；过大或不能完整交接的历史会有单独提示。验收续聊时，让第二轮复述只在第一轮出现的随机串，并检查返回事件。只收到 HTTP 200 表示消息进入流程。

## 4. 手机 UI 与实际交互

设计基准为 **390 × 844** 的竖屏，桌面用多列卡片利用宽屏。默认底色 `#121716`、面板 `#1B2220`、正文 `#EEF2EC`、薄荷绿主色 `#A5ECC4`。细边框区分层级，状态同时有文字与颜色；常规信息用系统无衬线字，事件终端采用等宽排版。底部五个导航让常用操作靠近拇指。移动验收检查核心触控目标至少 44px，页面无横向溢出。另有浅色、午夜蓝主题和可选强调色。

| 页面 | 用户能看到和做什么 |
| --- | --- |
| 设备 Fleet | 在线数、负载卡、标签、延迟、设备管理、配对、WOL、服务书签。动态拓扑从实际节点生成，能切换全景、下发、回传；连线按方向流动，近期派单或回传另有事件提示。新增或移除节点时分支自动调整。支持减少动态效果。只读服务器有单独的硬件卡。 |
| 每台设备的 DSH 工作台 | 从设备卡进入，显示该主机的桌面共享会话与 Fleet 任务，可定向派单。若节点另行启动并配置 DSH Web，提供独立入口，遵循 DSH Web 自己的登录机制。Fleet 当前提供任务、会话和文件操作；尚未复刻 DSH Desktop 全部设置或实现整机远程桌面。 |
| 派单 Dispatch | 按设备、标签或多机选择目标；选工作区和 Agent，用可编辑、排序的参数化快捷指令填写常用任务。 |
| 任务 Tasks | 按设备筛选、搜索、按设备或项目分组、整理到文件夹、移入最近删除和恢复。任务详情默认以聊天气泡呈现对话；切到「终端」可按行看事件并跟随输出。终端是任务日志视图。可中止、复制摘要、查看交接和产物。 |
| 文件 / NAS | 按节点和命名卷浏览、搜索、上传、下载、预览，把文件或目录作为 DSH 上下文。任务流中可内联阅读 Markdown、查看 CSV、放大图像；已登记的产物可转存到授权的可写卷。同一 NAS 还可有独立的网页管理书签。 |
| 审批与设置 | 全站提示待审批，卡片展示节点、目录、操作与触发原因。支持允许一次、本会话信任目录、拒绝、超时拒绝。可编辑策略、切换主题和默认路由，查看节点插件状态。 |

参见[带图使用教程](../README.zh-CN.md)，其中节点、服务和文件均来自本地 Mock 演示。

## 5. 安全、只读服务器与通知

- **网络和身份**：绑定支持固定 IP、网卡、CIDR 匹配，默认拒绝通配监听。Hub/Sidecar 校验来源网段。手机用管理员密码与 HttpOnly 会话 Cookie，CLI 用 API Key，节点通过限时单次配对获得独立密钥。节点地址与标签可热更新。工作区路径、卷权限、节点启停和插件跨机授权分别控制。
- **执行策略**：默认 balanced 允许可信工作区内的常规操作，越界及高危行为触发审批；Windows 支持受限的原生 Python、PowerShell、Node 执行，Docker 是可选隔离方式。strict_hash 校验管理员登记脚本及 SHA-256。autonomous 需要二次确认，当前实现仍保留宿主路径和高危规则约束。首次 Sidecar 派单和续聊后的桌面工具调用分别遵循各自执行端的策略。
- **只读 Linux 服务器**：VM 的 systemd 用户定时器约每 15 秒经已验证 SSH 主机密钥运行固定采集脚本，原子更新私有 JSON 快照。Hub 只读取白名单字段，超过 60 秒标记过期。设备卡显示 CPU、内存、根分区和各 NVIDIA GPU 的利用率及显存。这类对象不进入调度节点表，手机没有命令、文件或唤醒入口。
- **Hermes 与飞书**：通用 fleetctl 可查节点、派单、查结果、浏览卷、处理审批和唤醒设备。通知支持本地事件文件、Webhook 和飞书适配。部署者可指定私聊作为通知目标，并仅在 Fleet 控制台做审批决定。

## 6. 验证方法

用回环集群验证接入、派单、审批、产物和 NAS 转存。桌面插件、模型桥与共享会话还需要匹配版本的本机 DSH。分项验收方法见[验收记录](acceptance.md)。

## 7. 接手时从哪里开始

- 先跑演示：仓库根目录执行 `uv run --no-dev python deploy/demo.py`，使用终端显示的临时地址和密码，可体验 Hub、三个 Mock Sidecar、审批与产物。真实设施不参与演示。
- 看一次派单：从 `apps/web-pwa/src/Dispatch.tsx` 到 `apps/fleet-hub/fleet_hub/app.py`、`engine.py`，再看 `apps/node-sidecar/fleet_node/`。
- 看桌面接力：从 `integrations/dsh-app-plugin/README.md`、`index.mjs`、`handoff.mjs` 到 Hub 的 `desktop.py`、`conversations.py`，再看 `TaskConversation.tsx` 如何渲染与发送。
- 看 UI：入口 `apps/web-pwa/src/main.tsx`，全局主题 `style.css`，设备拓扑 `FleetTopology.tsx`，主机工作台 `NodeWorkbench.tsx`，产物渲染 `Artifacts.tsx`，硬件卡 `Fleet.tsx`。
- 看测试：`docs/acceptance.md`、`tests/ui/`、`tests/test_task_conversations.py`、`tests/test_hardware_monitors.py`。使用通用示例与 Mock 数据复现。私有配置、探测记录、凭据、生产日志和用户会话内容应留在部署环境内。