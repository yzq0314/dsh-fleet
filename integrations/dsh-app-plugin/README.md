# DSH Desktop 的 Fleet 插件

这个 Cordis bundle 可把用户选中的桌面会话接入 Fleet，也可在同一台电脑上为受 Sidecar 策略约束的任务提供模型推理。已用 DSH Desktop `0.2.2-rc.3` 的实际 Electron 分发包和 DSH CLI SDK `0.2.1-rc.2` 验证。

## 安装和卸载

启用桌面交接后，任务页的「对话」可以继续使用同一台机器上的原生会话。打开任务，等待「已加入 DSH App 对话列表」，在「继续此 DSH 会话」输入下一句话。首次派单使用 Sidecar 的执行策略，后续消息由 DSH Desktop 按该会话的原生权限处理。手机和电脑共享工作目录、历史和审批结果。插件每次心跳检查待发消息，桌面端按请求编号去重。超过两分钟仍未领取的消息会过期。在桌面输入 `/fleet off` 可停止共享当前会话。

Hub 和桌面插件都支持续聊协议时，任务页会显示发送框。旧插件仍可使用原有派单和桌面交接功能。队列按设备或项目分组，已结束的任务可移入「最近删除」并恢复，桌面原会话和工作区文件保留。

1. 用运行 Desktop 的操作系统账号完成 Sidecar 配对，并启动过一次 Desktop，生成 `profiles/desktop/package.json`。
2. 退出 Desktop，防止应用与安装器同时写入 profile。在仓库根目录运行以下命令，将示例路径替换为配对生成的本机配置文件。

   ```sh
   uv run python integrations/dsh-app-plugin/manage.py install --node-config local/node.local.yaml
   ```

3. 启动 Desktop，在希望共享的会话内输入 `/fleet on`。手机任务页随后能看到该会话的新事件。输入 `/fleet off` 可停止同步。应用退出会中断该账号的模型桥，重新启动后恢复。

安装器默认使用 `DSH_HOME` 或当前用户主目录下的 `.dsh`。自定义目录可给安装和卸载命令均传入 `--dsh-home <目录>`。重复安装可更新本地插件。要卸载，请退出 Desktop 后运行：

```sh
uv run python integrations/dsh-app-plugin/manage.py uninstall
```

安装器管理 profile 的 bundle 列表和 `node_modules/@dsh-fleet/desktop`。卸载仅移除本安装器管理的插件、连接配置和短期模型桥描述符。私有的 `fleet-desktop/sync-state-*.json` 包含选中会话、等待同步的内容及被 Hub 拒收事件的编号。每份状态绑定 Hub 地址、节点 ID 和配对凭据，切换站点会创建独立状态。需要清除旧记录时，请在退出 Desktop 后核对并自行删除相应状态文件。

## 在桌面对话中使用

### 从手机派单后，回到这台电脑继续

在 Fleet 派单页选择目标节点、DSH 和工作目录，并开启桌面会话交接。任务由 Sidecar 按节点策略执行。使用活动的桌面模型桥且任务成功时，Sidecar 会通过本机回环将本轮对话提交给配对插件。插件用宿主的原生会话存储写入历史，再将会话加入同一目录的桌面工作区。第一次使用某个目录时自动创建对应的工作区。任务页显示交接成功后，在这台电脑的 DSH Desktop 工作区点开以 **Fleet ·** 开头的对话即可续聊。

交接保留用户输入、助手可见回复、工具名称与参数、工具返回内容及错误状态。原任务产生的文件留在原目录。导入历史不会重新执行工具或发起模型请求。首次从桌面发送后续消息时，Desktop 读取这段历史并按当前桌面设置继续。SDK 的临时权限、模型内部推理和运行环境提示仅在原任务中使用。

交接需要同时更新节点 Sidecar 与桌面插件，并正常退出、重新启动 DSH Desktop。插件不在线、使用其他模型路由、出现当前导入器不能保留的附件或历史替换、可见内容超过 2 MiB 或 1024 条时，任务页会单独报告交接状态，已完成的任务和产物仍可在 Fleet 查看。旧任务没有保存交接所需的完整工具结果。从桌面发起并希望同步给 Fleet 的会话仍使用下方的 `/fleet on`。

| 输入 | 结果 |
| --- | --- |
| `/fleet on`、`/fleet off` | 开始或停止当前会话的新事件同步 |
| `/fleet nodes` | 查看 Hub 授权的节点和负载 |
| `/fleet files <节点ID> <卷名> [目录]` | 浏览已授权的命名卷 |
| `/fleet preview <节点ID> <卷名> <文件>` | 查看文本预览 |
| `/fleet dispatch <节点ID> <任务>` | 将用户明确输入的任务派给目标节点 |

模型也可在已共享会话内调用 `fleet_nodes`、`fleet_files` 和 `fleet_dispatch`。模型发起的派单需获得一次原生审批。跨节点权限默认是空集。下面的私有 Hub YAML 允许来源节点 `editor` 将任务派给 `worker` 并读取其 `shared` 卷。请使用现场的节点 ID 和卷名，保存后重启 Hub。

```yaml
desktop_clients:
  editor:
    targets: [worker]
    volumes:
      worker: [shared]
```

插件订阅原生会话事件、工具调用与审批。手机显示实际工作目录和完整工具参数，同一审批可在手机或桌面处理。桌面发起的原生审批支持允许一次或拒绝。缺少完整参数、需要脱敏或超出 UTF-8 事件预算时，请在桌面查看原始操作并决定。Hub 断线不会产生默认同意。普通长文本会标明截断。插件只同步启用后的新事件，推理内容和本机历史记录不会导入 Hub。

## Sidecar 选择哪条模型路由

每次启动 DSH 任务时，Sidecar 依次检查以下来源。

1. 节点私有配置中显式指定的 `dsh_provider`。使用 `type: desktop` 可要求当前桌面模型桥在线。
2. 活跃的桌面模型桥。插件通过 `agentDefaultModel.currentSelection()` 取得宿主当前 provider 和模型，每次新任务重新读取。插件只用宿主的 `llm.stream` 执行推理。工具执行和文件审批均在 Sidecar 内完成。任务中途更换模型后，进行中的请求会报告路由变化，可重新派单。
3. `DSH_HOME/profiles/<profile>/cordis.patch.yml` 以及主目录覆盖层 `DSH_HOME/cordis.patch.yml` 的可解析静态配置。默认 profile 为 `desktop`，可设置节点字段 `dsh_profile`。支持官方 `agent-default-model`、`llm-deepseek` 和使用 `openai-completions` 的具名 `llm-pi-ai` provider。正式 v1 凭据库存储的具名引用可由已安装的官方 resolver 读取。旧版凭据格式、动态配置、桌面账号登录以及专有 provider 请使用桌面模型桥。
4. `DSH_HOME/.env` 中含模型 API Key 的一组字段，其次为 Sidecar 进程环境。DeepSeek 可使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DSH_FLEET_MODEL`。OpenAI-compatible 可使用 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。凭据所属层提供自己的端点设置。

显式使用兼容接口的节点配置示例。请在本机设置凭据变量，并确认这一凭据确实属于所填写的服务。

```yaml
dsh_provider:
  type: openai-compatible
  model: example-model
  base_url: https://model.example.org/v1
  api_key_env: TEAM_MODEL_API_KEY
  credential_origin: https://model.example.org
```

已有 DSH profile 将接入点和私有凭据库中的名称配对时，可以直接复用这条路由。DSH 主目录的 `.env` 同时给出 Base URL 和 Key 时也可直接复用。若自定义端点需要借用单独的进程环境凭据，使用节点字段 `dsh_credential_origins` 指定该凭据名的来源，纯进程环境模式使用 `DSH_FLEET_CREDENTIAL_ORIGIN`。显式来源绑定会校验协议、域名和端口。回环测试服务支持 HTTP，其他模型端点需要 HTTPS。

桌面账号登录由宿主已有 provider 处理。Sidecar 通过本机回环地址和临时能力令牌请求纯推理，插件不会读取浏览器 Cookie、复制账号令牌或把模型凭据写入 Hub。模型仍会看到任务中发送给它的提示、上下文与工具定义，请按该 provider 的数据处理约定派单。

## 排查模型请求失败

在 Fleet 的任务详情展开日志。每次调用桌面模型都有独立的 `trace`，同一任务中的多轮模型调用可以分别查询。桌面插件也会在 `DSH_HOME/fleet-desktop/model-diagnostics.json` 保存本次进程最近 128 条诊断事件。两端使用同一个 `trace`。新增诊断只记录阶段、固定错误类别、状态码和关联编号，桌面副本还记录时间。

| 任务日志字段 | 能确认的事实 |
| --- | --- |
| `bridge_received`、`adapter_start` | 本机桌面桥已接收请求，并开始调用桌面选中的模型适配器。 |
| `upstream_send` | 已观测到出站推理请求发送。 |
| `upstream_http status=…` | 已从配置的推理端点收到该请求的 HTTP 响应。使用第三方代理时，要确认最终服务商是否收到请求，还需核对代理或服务商记录。 |
| `bridge_error category=…` | 桌面桥已将失败归为鉴权、限流、超时等类别。`client_error` 附带的状态码来自本机桥。 |
| `sdk_turn_end reason=error` | SDK 结束了本轮。展开查看 `sdk_code` 和 SDK 提供的可选 `sdk_status`。 |

自定义传输或非标准端点可能只产生适配器事件。缺少 `upstream_http` 时，端点响应仍待核实。更新桌面插件后，请从托盘正常退出并重新打开 Desktop，使新日志在后续请求中生效。

## 验证

```sh
uv run pytest tests/test_desktop_provider.py tests/test_desktop_native.py tests/test_sidecar_dsh.py
```

原生集成测试使用真实安装包、临时用户配置、合成会话事件与本机假模型服务。缺少相应 Desktop 或 SDK 安装时，依赖该软件的测试会跳过。真实账号额度、生产会话和真实模型不参与此测试。
