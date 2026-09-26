# 工作方式

## 数据流

`Meter.qml` 定时启动 `collect.py`，接收过滤后的 JSON 并更新面板。设置保存在 DMS 的插件配置中。

当前额度窗口的 `reset_at` 是账号自己的重置时间，面板以它判断本周期是否预计刷新。采集器另会匿名请求 `https://didcodexreset.com/openapi/v1/records/latest?kind=all` 获取额外公开排期。该请求不携带 Codex 令牌、Cookie 或账号标识，超时为 5 秒，响应上限为 512 KiB。`kind=reset_scheduled` 且 `scheduleState=pending` 的记录作为待执行排期；若状态为 `elapsed`，且排期窗口结束未满 3 小时，则标为“尚未确认”的重置信号，3 小时后不再显示。`schedulePrecision`、`scope`、`confidence` 和 `scheduleBasis` 规范化后交给面板。

采集器读取 `CODEX_HOME/auth.json`，默认目录为 `~/.codex`。
令牌有效时直接查询 `https://chatgpt.com/backend-api/wham/usage`，超时为 30 秒。请求保留 TLS 验证，不跟随重定向，也不使用 Cookie。

当 JWT 距到期不超过 5 分钟时，采集器调用官方 Codex 程序的 `app-server`，经初始化后请求 `account/rateLimits/read`。无法读取 JWT 到期时间时，改按 `last_refresh` 缺失或超过 8 天判断。JWT 声明只用于本地判断，服务端负责验证认证信息。

插件不自行兑换共享刷新令牌，也不写登录文件。官方程序可能在查询过程中更新自身凭据。普通 HTTP 错误不会触发刷新循环。

## 额度与缓存

窗口按实际时长排序，主额度的短周期优先，周额度其次；额外模型额度单独展示。缺失比例不会被当作零。

重置券数量来自 `rate-limit-reset-credits`，超时 4 秒，查询失败不影响主额度。插件不提供兑换操作。

多屏实例通过文件锁共享 10 秒缓存。缓存只保存额度字段与不可逆的认证状态摘要，权限为 `0600`。账号或认证状态变化会使缓存失效。界面遇到查询失败时保留上次成功的数据，并显示旧数据提示。

公开排期使用同一文件锁，但保存在独立的 `reset-schedule.json` 中，成功结果自动缓存 10 分钟；额度刷新设置不会提高该 API 的请求频率。手动刷新绕过 10 分钟缓存，但保留 10 秒并发去重，并遵守 `Retry-After`、`X-RateLimit-Remaining` 和 `X-RateLimit-Reset`。服务端限流为每个出口 IP 每小时 20 次。查询失败时保留上次成功的排期并标旧；没有成功缓存时显示暂不可用。排期错误不改变额度查询结果。

没有仍有效或待确认的排期信号只表示当前没有额外的公开排期，不会覆盖主额度卡片中的账号周期重置时间。“额外重置预告”卡片仅根据公开排期显示状态，默认展示状态、可信度、本地日期和适用范围；依据、完整范围、数据更新时间与来源折叠在详情中，旧数据标记保持可见。若公开排期的套餐或额度窗口范围未知，或无法确认当前套餐是否匹配，摘要显示“适用范围待确认”；日期级排期只显示本地日期范围，不推导精确时刻。当前年份可省略年份，跨年或其他年份的日期保留年份。

## 程序定位

1. `CODEX_METER_CLI` 指定的可执行文件绝对路径。
2. `PATH` 中的 `codex`。
3. Linux 桌面应用安装布局 `/usr/lib/chatgpt/resources/codex`。

QML 按自身文件位置定位采集器，不依赖固定用户名、工作目录或 DMS 配置目录。

协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
