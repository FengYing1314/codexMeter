# 参与贡献

请先通过 Issue 描述问题或拟议改动，附上 DMS、Quickshell、Python 和合成器版本。界面问题可以提供裁剪后的面板截图。

## 本地开发

1. 克隆仓库并运行 `sh install.sh`。
2. 在 DMS 中启用插件并添加到 DankBar。
3. 修改后运行 `dms ipc call plugins reload codexMeter`。
4. 执行离线检查：

```sh
python3 -B -m unittest discover -s tests -v
sh -n install.sh
python3 -m json.tool plugin.json > /dev/null
```

测试只使用合成凭据、模拟 HTTP 响应和临时目录，不需要登录账号或访问网络。
界面变更还需在运行中的 DMS 验证：顶栏、弹出面板、空数据、错误提示、长列表滚动及设置保存。

## 提交改动

- 保持改动聚焦，说明行为变化和已运行的检查。
- 保持 DMS 主题适配和现有中文文案风格。
- 查询逻辑变更应覆盖缺失字段、认证失效和多屏并发等边界。
- 不提交 `auth.json`、令牌、账号标识或包含个人信息的原始响应和截图。

提交的贡献适用项目的 MIT 许可证。
