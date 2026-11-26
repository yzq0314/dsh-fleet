# DSH Pocket 同屏与运维提醒

Fleet 与独立安装的 DSH Pocket 通过 HTTP 和 WebSocket 通信。上游软件由节点所有者单独安装。桌面端选择 compatibility 模式，在 Pocket 的 settings.json 中设置 proxyPort 为 13081，重启 Desktop。在新节点的 Fleet 工作台里核对本机 HTTP 探测结果。端口优先使用节点配置，其次使用 Pocket 配置，最后检查 13081、3081。

## 同屏访问地址

浏览器从 Fleet 的 `/pocket/{node_id}/` 进入。每台节点使用一个独立的内网 DNS 名，全部解析到 Hub，使用与控制台相同的协议和端口。建议把控制台与这些地址放在同一站点下，便于手机浏览器在内嵌页面中使用 Cookie。节点地址与真实内网 DNS 均由部署者在私有配置中维护。

Hub 的 `pocket_origins` 映射节点 ID 到完整访问来源，例如 `http://screen-a.fleet.example:8765`。Sidecar 的 `pocket_public_origin` 填写同一值。`pocket_port` 可覆盖自动发现。修改配置后重启对应服务。保留已有限制来源的 Hub 和 Sidecar 监听。DSH Pocket 的安装和局域网监听设置由用户在 Desktop 管理。

手机点「进入 DSH 实时同屏」。Hub 用 Fleet 会话签发有效期 20 秒的单次入口。在专用地址内，根路径资源、RPC 和 WebSocket 均属于对应节点，Pocket 执行其访问密码验证。Fleet 管理 Cookie、节点密钥及本机密码文件均独立于上游请求。退出 Fleet 后，后续请求需重新登录，存量 WebSocket 会定期重新校验。

Pocket 可能要求启用相应的远程访问模式或拒绝嵌入。请按该版本的官方认证流程登录。上游禁用嵌入或手机阻止第三方 Cookie 时可点顶栏「新窗口打开」。关闭自动公网隧道可避免额外公网入口。[DSH Pocket 使用说明](https://github.com/shaobeichen/dsh-pocket)

## 硬件通知与快捷指令

在「审批与设置 → 安全与审批 → 通知策略」选择通知类型和磁盘阈值。磁盘持续高占用按冷却时间提醒，恢复后重新计时。采集器私有配置可选 `monitored_mounts` 字符串数组以添加其他分区。卡片上的铃铛订阅特定服务器的 GPU 编号，需连续三份新采样满足显存低于 1024 MiB 且利用率低于 10%。刷新页面或重复轮询相同采样仅计一次。只读服务器继续通过采集器提供硬件数据。

运维通知通过 Hub 的本地 file_hook 交给已指定收件人的 Hermes 转发器。`integrations/hermes/monitor_notice.py` 格式化 `monitor_alert` 事件。收件目标由可信的私有部署配置固定；审批仍在控制台操作。GPU 订阅在告警写入持久化发送队列后结束。运营者可通过 Hermes 回执确认实际投递。

快捷指令编辑器支持设备标签。选择设备或按标签筛选时，命中标签更多的模板优先展示。管理模式保留手工排序。模板参数 `{{target}}` 预填当前候选目标，可在应用模板前改写。

## 升级已有 Windows 节点

在已提交的仓库根目录运行 `python deploy/build-windows-upgrade.py ALIAS upgrade.ps1`。在目标设备当前桌面用户会话运行脚本，可传 `-CheckOnly` 检查归属、`-Index` 指定 PyPI 镜像、`-DshCli` 指定已安装的 DSH 启动器。脚本分目录准备版本与依赖，检查节点空闲，保留现有私有配置、交互登录计划任务和防火墙规则。若新 Sidecar 健康检查失败，启动器恢复到前一版本。原生插件更新后可重开 Desktop 检查心跳。此前已停用且移除的桌面插件会保留停用状态。
