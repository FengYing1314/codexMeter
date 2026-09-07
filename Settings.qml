import QtQuick
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: settings
    pluginId: "codexMeter"
    readonly property var intervals: [60000, 120000, 300000, 900000]
    readonly property var intervalLabels: ["1 分钟", "2 分钟", "5 分钟", "15 分钟"]
    StyledText {
        width: parent.width
        text: "Codex 用量"
        color: Theme.surfaceText
        font.pixelSize: Theme.fontSizeLarge
    }
    DankDropdown {
        text: "自动刷新间隔"
        description: "选择用量面板自动更新的频率"
        options: settings.intervalLabels
        currentValue: {
            var index = settings.intervals.indexOf(Number(settings.loadValue("refreshMs", 120000)));
            return settings.intervalLabels[index < 0 ? 1 : index];
        }
        onValueChanged: value => {
            var index = settings.intervalLabels.indexOf(value);
            if (index >= 0) settings.saveValue("refreshMs", settings.intervals[index]);
        }
    }
    StyledText {
        width: parent.width
        wrapMode: Text.Wrap
        text: "显示当前 Codex 账号的已用额度。点击顶栏可查看剩余比例、重置时间和额外模型额度，也可以随时手动刷新。"
        color: Theme.surfaceVariantText
    }
}
