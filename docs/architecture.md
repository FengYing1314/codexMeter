# 工作方式

## 数据流

`Meter.qml` 定时启动 `collect.py`，接收过滤后的 JSON 并更新面板。设置保存在 DMS 的插件配置中。

采集器读取 `CODEX_HOME/auth.json`，默认目录为 `~/.codex`。
令牌有效时直接查询 `https://chatgpt.com/backend-api/wham/usage`，超时为 30 秒。请求保留 TLS 验证，不跟随重定向，也不使用 Cookie。

当 JWT 距到期不超过 5 分钟时，采集器调用官方 Codex 程序的 `app-server`，经初始化后请求 `account/rateLimits/read`。无法读取 JWT 到期时间时，改按 `last_refresh` 缺失或超过 8 天判断。JWT 声明只用于本地判断，服务端负责验证认证信息。

插件不自行兑换共享刷新令牌，也不写登录文件。官方程序可能在查询过程中更新自身凭据。普通 HTTP 错误不会触发刷新循环。

## 额度与缓存

窗口按实际时长排序，主额度的短周期优先，周额度其次；额外模型额度单独展示。缺失比例不会被当作零。

重置券数量来自 `rate-limit-reset-credits`，超时 4 秒，查询失败不影响主额度。插件不提供兑换操作。

多屏实例通过文件锁共享 10 秒缓存。缓存只保存额度字段与不可逆的认证状态摘要，权限为 `0600`。账号或认证状态变化会使缓存失效。界面遇到查询失败时保留上次成功的数据，并显示旧数据提示。

## 程序定位

1. `CODEX_METER_CLI` 指定的可执行文件绝对路径。
2. `PATH` 中的 `codex`。
3. Linux 桌面应用安装布局 `/usr/lib/chatgpt/resources/codex`。

QML 按自身文件位置定位采集器，不依赖固定用户名、工作目录或 DMS 配置目录。

协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
