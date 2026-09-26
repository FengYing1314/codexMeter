import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: meter
    property var windows: []
    property string problem: ""
    property string updated: ""
    property double nowMs: Date.now()
    property var resetCredits: null
    property var resetSchedule: ({status: "unavailable", stale: false, schedule: null, meta: {}, message: ""})
    property bool forceResetSchedule: false
    readonly property bool busy: collector.running
    readonly property bool hasResetSchedule: resetSchedule.status === "pending" || resetSchedule.status === "grace"
    readonly property var selected: windows.length ? windows[0] : null
    readonly property color accent: selected && selected.used >= 80 ? Theme.error : Theme.primary
    readonly property string caption: selected ? "Codex " + Math.round(selected.used) + "%" : busy ? "查询中" : "暂不可用"
    readonly property var extraGroups: {
        var groups = [];
        windows.slice(1).forEach(function(entry) {
            var found = groups.find(function(group) { return group.name === entry.group; });
            if (!found) {
                found = {name: entry.group, windows: []};
                groups.push(found);
            }
            found.windows.push(entry);
        });
        return groups;
    }

    function refresh(forceSchedule) {
        if (!collector.running) {
            meter.forceResetSchedule = !!forceSchedule;
            collector.running = true;
        }
    }

    function resetText(value) {
        var date = new Date(value);
        if (!value || isNaN(date.getTime())) return "重置时间未提供";
        return "重置：" + Qt.formatDateTime(date, "MM-dd hh:mm");
    }

    function period(entry) {
        if (!entry) return "";
        if (entry.seconds === 604800) return "本周";
        if (entry.seconds === 18000) return "5 小时";
        return entry.seconds && entry.seconds < 86400 ? "短周期" : "当前周期";
    }

    function resetCountdown(value) {
        var time = new Date(value).getTime();
        if (!value || isNaN(time)) return "重置时间待更新";
        var minutes = Math.ceil((time - nowMs) / 60000);
        if (minutes <= 0) return "等待额度更新";
        if (minutes < 60) return minutes + " 分钟后重置";
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + " 小时" + (minutes % 60 ? " " + minutes % 60 + " 分钟" : "") + "后重置";
        return Math.floor(hours / 24) + " 天" + (hours % 24 ? " " + hours % 24 + " 小时" : "") + "后重置";
    }

    function groupTitle(name) {
        return name === "Codex" ? "其他主额度" : name.endsWith("-Spark") ? "Codex Spark" : name;
    }

    function scheduleStatusText() {
        if (resetSchedule.status === "pending") return "已排期，等待重置";
        if (resetSchedule.status === "grace") return "计划时间已过，重置结果待确认";
        return resetSchedule.status === "none" ? "暂无重置排期" : "暂时无法获取排期";
    }

    function scheduleScopeSummary(schedule) {
        var scope = schedule && schedule.scope || {};
        var plans = scope.plans || [];
        var windows = scope.windows || [];
        if (plans.indexOf("all") < 0 || plans.indexOf("unknown") >= 0
                || !windows.length || windows.indexOf("unknown") >= 0)
            return "适用范围待确认";
        return scheduleScopeText(schedule);
    }

    function scheduleScopeText(schedule) {
        var scope = schedule && schedule.scope;
        if (!scope) return "适用范围待确认";
        var plans = scope.plans || [];
        var windows = scope.windows || [];
        var planText = !plans.length || plans.indexOf("unknown") >= 0
            ? "套餐范围待确认"
            : plans.indexOf("all") >= 0
                ? "所有套餐"
                : "套餐：" + plans.join("、") + "（当前账号适用性待确认）";
        var windowLabels = {five_hour: "5 小时额度", weekly: "周额度", unknown: "额度窗口待确认"};
        var windowText = windows.length
            ? windows.map(function(value) { return windowLabels[value] || value; }).join("、")
            : "额度窗口待确认";
        return planText + " · " + windowText;
    }

    function scheduleTimeText(schedule, compact) {
        if (!schedule) return "时间未公布";
        var timeWindow = schedule.window || {};
        if (schedule.precision === "date") {
            var start = timeWindow.startAt || schedule.effectiveAt || timeWindow.endAt || "";
            var end = timeWindow.endAt || "";
            if (!start) return "日期未公布";
            var startValue = new Date(start);
            if (isNaN(startValue.getTime())) return "日期待确认";
            var endValue = end ? new Date(end) : null;
            if (endValue && !isNaN(endValue.getTime()))
                endValue = new Date(endValue.getTime() - 60000);
            var currentYear = new Date(nowMs).getFullYear();
            var format = compact && startValue.getFullYear() === currentYear
                && (!endValue || isNaN(endValue.getTime()) || endValue.getFullYear() === currentYear)
                ? "MM-dd" : "yyyy-MM-dd";
            var startDate = Qt.formatDate(startValue, format);
            var endDate = endValue && !isNaN(endValue.getTime())
                ? Qt.formatDate(endValue, format) : "";
            return endDate && endDate !== startDate
                ? startDate + " – " + endDate + "（本地日期范围）"
                : startDate + "（本地日期）";
        }
        if (schedule.precision !== "datetime") return "时间精度待确认";
        var exactTime = schedule.effectiveAt || timeWindow.startAt || timeWindow.endAt;
        var date = new Date(exactTime || "");
        var dateFormat = compact && date.getFullYear() === new Date(nowMs).getFullYear()
            ? "MM-dd hh:mm" : "yyyy-MM-dd hh:mm";
        return exactTime && String(exactTime).indexOf("T") >= 0 && !isNaN(date.getTime())
            ? Qt.formatDateTime(date, dateFormat) + "（本地时间）"
            : "具体时间未公布";
    }

    function scheduleConfidenceText(schedule) {
        return schedule && typeof schedule.confidence === "number"
            ? "可信度 " + Math.round(schedule.confidence * 100) + "%" : "";
    }

    function scheduleBasisText(schedule) {
        if (!schedule) return "";
        return schedule.basis === "explicit" ? "明确排期"
            : schedule.basis === "contextual_inference" ? "上下文推断" : "依据待确认";
    }

    function scheduleUpdatedText() {
        var value = resetSchedule.meta && resetSchedule.meta.generatedAt;
        var date = new Date(value || "");
        return value && !isNaN(date.getTime())
            ? "数据更新：" + Qt.formatDateTime(date, "MM-dd hh:mm") + "（本地时间）" : "";
    }

    Timer {
        interval: 60000
        running: true
        repeat: true
        onTriggered: meter.nowMs = Date.now()
    }

    Component.onCompleted: Qt.callLater(refresh)

    Process {
        id: collector
        command: meter.forceResetSchedule
            ? ["python3", "-B", decodeURIComponent(Qt.resolvedUrl("collect.py").toString().replace(/^file:\/\//, "")), "--force-reset-schedule"]
            : ["python3", "-B", decodeURIComponent(Qt.resolvedUrl("collect.py").toString().replace(/^file:\/\//, ""))]
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    var result = JSON.parse(text);
                    if (result.resetSchedule) meter.resetSchedule = result.resetSchedule;
                    if (!result.ok) {
                        meter.problem = result.message || "查询失败";
                        return;
                    }
                    meter.windows = result.windows;
                    meter.nowMs = Date.now();
                    meter.updated = Qt.formatDateTime(new Date(result.updatedAt), "hh:mm:ss");
                    meter.resetCredits = result.resetCredits;
                    meter.problem = "";
                } catch (error) {
                    meter.problem = "采集结果无法解析";
                }
            }
        }
        onExited: code => {
            if (code !== 0) meter.problem = "采集进程异常退出";
            meter.forceResetSchedule = false;
        }
    }

    Timer {
        interval: Math.max(60000, Number(meter.pluginData.refreshMs) || 120000)
        running: true
        repeat: true
        onTriggered: meter.refresh()
    }

    horizontalBarPill: Component {
        Row {
            spacing: 7
            DankIcon {
                name: meter.problem ? "cloud_off" : "data_usage"
                size: 16
                color: meter.problem ? Theme.error : meter.accent
                anchors.verticalCenter: parent.verticalCenter
            }
            StyledText {
                text: meter.caption
                textFormat: Text.PlainText
                color: Theme.surfaceText
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.Medium
                anchors.verticalCenter: parent.verticalCenter
            }
            Rectangle {
                visible: !!meter.selected
                width: periodText.implicitWidth + 10
                height: 20
                radius: 6
                color: Qt.rgba(meter.accent.r, meter.accent.g, meter.accent.b, 0.12)
                anchors.verticalCenter: parent.verticalCenter
                StyledText {
                    id: periodText
                    anchors.centerIn: parent
                    text: meter.problem ? "旧数据" : meter.period(meter.selected)
                    font.pixelSize: 10
                    font.weight: Font.Medium
                    color: meter.accent
                }
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: 3
            DankIcon {
                name: meter.problem ? "cloud_off" : "data_usage"
                size: 16
                color: meter.accent
                anchors.horizontalCenter: parent.horizontalCenter
            }
            StyledText {
                text: meter.selected ? Math.round(meter.selected.used) + "%" : "—"
                color: Theme.surfaceText
                font.pixelSize: Theme.fontSizeSmall
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    component QuotaRow: Column {
        required property var quota
        spacing: 7
        Item {
            width: parent.width
            height: 20
            StyledText {
                text: meter.period(quota)
                color: Theme.surfaceText
                font.pixelSize: Theme.fontSizeSmall
            }
            StyledText {
                anchors.right: parent.right
                text: Math.round(quota.used) + "% 已用"
                color: quota.used >= 80 ? Theme.error : Theme.surfaceText
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.Medium
            }
        }
        Rectangle {
            width: parent.width
            height: 5
            radius: 3
            color: Theme.surfaceContainerHighest
            Rectangle {
                width: parent.width * quota.used / 100
                height: parent.height
                radius: parent.radius
                color: quota.used >= 80 ? Theme.error : Theme.primary
                Behavior on width { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }
            }
        }
        StyledText {
            width: parent.width
            wrapMode: Text.Wrap
            text: meter.resetCountdown(quota.reset)
            color: Theme.surfaceVariantText
            font.pixelSize: 11
        }
    }

    popoutWidth: 400
    popoutContent: Component {
        PopoutComponent {
            id: panel
            headerText: "Codex 用量"
            detailsText: ""
            showCloseButton: true
            spacing: 12
            headerActions: Component {
                DankActionButton {
                    iconName: "refresh"
                    tooltipText: meter.busy ? "正在刷新" : "刷新用量"
                    enabled: !meter.busy
                    iconColor: Theme.primary
                    onClicked: meter.refresh(true)
                }
            }

            Rectangle {
                visible: meter.problem.length > 0
                width: parent.width
                height: warningText.implicitHeight + 20
                radius: 10
                color: Qt.rgba(Theme.error.r, Theme.error.g, Theme.error.b, 0.09)
                StyledText {
                    id: warningText
                    x: 12; y: 10
                    width: parent.width - 24
                    wrapMode: Text.Wrap
                    text: meter.problem + (meter.windows.length ? "，当前显示上次数据。" : "")
                    color: Theme.error
                    font.pixelSize: Theme.fontSizeSmall
                }
            }

            Flickable {
                width: parent.width
                height: Math.min(cards.implicitHeight, Math.max(250, Math.min(480, (meter.parentScreen ? meter.parentScreen.height : 1080) - 240)))
                contentHeight: cards.implicitHeight
                contentWidth: width
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }

                Column {
                    id: cards
                    width: parent.width
                    spacing: 10

                    Rectangle {
                        visible: !!meter.selected
                        width: parent.width
                        height: hero.implicitHeight + 32
                        radius: 16
                        color: Qt.rgba(meter.accent.r, meter.accent.g, meter.accent.b, 0.08)
                        border.width: 1
                        border.color: Qt.rgba(meter.accent.r, meter.accent.g, meter.accent.b, 0.16)

                        Column {
                            id: hero
                            x: 16; y: 16
                            width: parent.width - 32
                            spacing: 12

                            Item {
                                width: parent.width
                                height: 20
                                StyledText {
                                    text: meter.period(meter.selected) + "额度"
                                    font.pixelSize: Theme.fontSizeMedium
                                    font.weight: Font.Medium
                                    color: Theme.surfaceText
                                }
                                StyledText {
                                    anchors.right: parent.right
                                    text: meter.problem ? "旧数据" : meter.busy ? "正在同步…" : "主额度"
                                    color: meter.problem ? Theme.error : Theme.surfaceVariantText
                                    font.pixelSize: Theme.fontSizeSmall
                                }
                            }
                            Row {
                                spacing: 10
                                StyledText {
                                    text: meter.selected ? Math.round(meter.selected.used) + "%" : "—"
                                    font.pixelSize: 42
                                    font.weight: Font.DemiBold
                                    color: meter.accent
                                }
                                StyledText {
                                    anchors.bottom: parent.bottom
                                    anchors.bottomMargin: 9
                                    text: "已使用"
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                }
                                Item { width: 4; height: 1 }
                                StyledText {
                                    anchors.bottom: parent.bottom
                                    anchors.bottomMargin: 9
                                    text: meter.selected ? "剩余 " + Math.round(100 - meter.selected.used) + "%" : ""
                                    color: Theme.surfaceText
                                    font.pixelSize: Theme.fontSizeSmall
                                }
                            }
                            Rectangle {
                                width: parent.width
                                height: 7
                                radius: 4
                                color: Qt.rgba(meter.accent.r, meter.accent.g, meter.accent.b, 0.12)
                                Rectangle {
                                    width: parent.width * (meter.selected ? meter.selected.used : 0) / 100
                                    height: parent.height
                                    radius: parent.radius
                                    color: meter.accent
                                    Behavior on width { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                                }
                            }
                            Column {
                                width: parent.width
                                spacing: 4
                                StyledText {
                                    text: meter.resetCountdown(meter.selected ? meter.selected.reset : "")
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceText
                                }
                                StyledText {
                                    text: meter.resetText(meter.selected ? meter.selected.reset : "")
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                            }
                        }
                    }

                    Repeater {
                        model: meter.extraGroups
                        delegate: Rectangle {
                            required property var modelData
                            width: cards.width
                            height: groupBody.implicitHeight + 28
                            radius: 14
                            color: Theme.surfaceContainerHigh
                            Column {
                                id: groupBody
                                x: 14; y: 14
                                width: parent.width - 28
                                spacing: 14
                                StyledText {
                                    width: parent.width
                                    text: meter.groupTitle(modelData.name)
                                    textFormat: Text.PlainText
                                    elide: Text.ElideRight
                                    font.pixelSize: Theme.fontSizeMedium
                                    font.weight: Font.Medium
                                    color: Theme.surfaceText
                                }
                                Repeater {
                                    model: modelData.windows
                                    delegate: QuotaRow {
                                        required property var modelData
                                        quota: modelData
                                        width: groupBody.width
                                    }
                                }
                            }
                        }
                    }

                    Column {
                        visible: !meter.selected
                        width: parent.width
                        topPadding: 20
                        bottomPadding: 24
                        spacing: 12
                        DankIcon {
                            anchors.horizontalCenter: parent.horizontalCenter
                            name: meter.busy ? "hourglass_top" : "cloud_off"
                            size: 32
                            color: Theme.surfaceVariantText
                        }
                        StyledText {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: meter.busy ? "正在读取额度…" : "暂时没有额度数据"
                            font.pixelSize: Theme.fontSizeMedium
                            color: Theme.surfaceText
                        }
                    }

                    Rectangle {
                        width: cards.width
                        height: scheduleBody.implicitHeight + 28
                        radius: 12
                        color: Theme.surfaceContainerHigh
                        Column {
                            id: scheduleBody
                            x: 14; y: 14
                            width: parent.width - 28
                            spacing: 8
                            Item {
                                width: parent.width
                                height: scheduleToggle.height
                                StyledText {
                                    anchors.left: parent.left
                                    anchors.right: scheduleToggle.left
                                    anchors.rightMargin: 8
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: "额外重置预告" + (meter.resetSchedule.stale ? " · 旧数据" : "")
                                    textFormat: Text.PlainText
                                    elide: Text.ElideRight
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: meter.resetSchedule.stale ? Theme.error : Theme.surfaceVariantText
                                }
                                Controls.AbstractButton {
                                    id: scheduleToggle
                                    anchors.right: parent.right
                                    width: 64
                                    height: 28
                                    checkable: true
                                    focusPolicy: Qt.StrongFocus
                                    Accessible.name: checked ? "收起排期详情" : "展开排期详情"
                                    contentItem: Item {
                                        StyledText {
                                            x: 6
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: scheduleToggle.checked ? "收起" : "详情"
                                            font.pixelSize: Theme.fontSizeSmall
                                            color: Theme.surfaceVariantText
                                        }
                                        DankIcon {
                                            anchors.right: parent.right
                                            anchors.rightMargin: 4
                                            anchors.verticalCenter: parent.verticalCenter
                                            name: scheduleToggle.checked ? "expand_less" : "expand_more"
                                            size: 16
                                            color: Theme.surfaceVariantText
                                        }
                                    }
                                    background: Rectangle {
                                        radius: 6
                                        color: scheduleToggle.hovered || scheduleToggle.down
                                            ? Theme.surfaceContainerHighest : "transparent"
                                        border.width: scheduleToggle.visualFocus ? 1 : 0
                                        border.color: Theme.primary
                                    }
                                }
                            }
                            Item {
                                width: parent.width
                                height: Math.max(scheduleStatus.implicitHeight, scheduleConfidence.implicitHeight)
                                StyledText {
                                    id: scheduleStatus
                                    width: parent.width - (scheduleConfidence.visible ? scheduleConfidence.width + 12 : 0)
                                    wrapMode: Text.Wrap
                                    text: meter.scheduleStatusText()
                                    textFormat: Text.PlainText
                                    font.pixelSize: Theme.fontSizeMedium
                                    font.weight: Font.Medium
                                    color: meter.hasResetSchedule ? Theme.primary : Theme.surfaceVariantText
                                }
                                StyledText {
                                    id: scheduleConfidence
                                    visible: meter.hasResetSchedule && text.length > 0
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: meter.scheduleConfidenceText(meter.resetSchedule.schedule)
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                            }
                            StyledText {
                                visible: meter.hasResetSchedule
                                width: parent.width
                                wrapMode: Text.Wrap
                                text: meter.scheduleTimeText(meter.resetSchedule.schedule, true)
                                textFormat: Text.PlainText
                                font.pixelSize: Theme.fontSizeSmall
                                color: Theme.surfaceText
                            }
                            StyledText {
                                visible: meter.hasResetSchedule
                                width: parent.width
                                wrapMode: Text.Wrap
                                text: meter.scheduleScopeSummary(meter.resetSchedule.schedule)
                                textFormat: Text.PlainText
                                font.pixelSize: 11
                                color: Theme.surfaceVariantText
                            }
                            Column {
                                visible: scheduleToggle.checked
                                width: parent.width
                                spacing: 6
                                Rectangle {
                                    width: parent.width
                                    height: 1
                                    color: Theme.surfaceContainerHighest
                                }
                                StyledText {
                                    visible: meter.hasResetSchedule
                                    width: parent.width
                                    wrapMode: Text.Wrap
                                    text: "排期：" + meter.scheduleTimeText(meter.resetSchedule.schedule)
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                                StyledText {
                                    visible: meter.hasResetSchedule
                                    width: parent.width
                                    wrapMode: Text.Wrap
                                    text: meter.scheduleScopeText(meter.resetSchedule.schedule)
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                                StyledText {
                                    visible: meter.hasResetSchedule
                                    width: parent.width
                                    wrapMode: Text.Wrap
                                    text: "依据：" + meter.scheduleBasisText(meter.resetSchedule.schedule)
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                                StyledText {
                                    visible: meter.resetSchedule.status === "unavailable" || meter.resetSchedule.stale
                                    width: parent.width
                                    wrapMode: Text.Wrap
                                    text: meter.resetSchedule.message || "暂未获取到最新排期，请稍后刷新。"
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                                StyledText {
                                    visible: text.length > 0
                                    width: parent.width
                                    wrapMode: Text.Wrap
                                    text: meter.scheduleUpdatedText()
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                                StyledText {
                                    width: parent.width
                                    wrapMode: Text.Wrap
                                    text: "来源：Did Codex Reset"
                                    textFormat: Text.PlainText
                                    font.pixelSize: 11
                                    color: Theme.surfaceVariantText
                                }
                            }
                        }
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: 42
                radius: 10
                color: Theme.surfaceContainerHigh
                Row {
                    x: 12
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 8
                    DankIcon { name: "confirmation_number"; size: 17; color: Theme.primary }
                    StyledText { text: "可用重置券"; font.pixelSize: Theme.fontSizeSmall; color: Theme.surfaceText }
                }
                StyledText {
                    anchors.right: parent.right
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    text: meter.resetCredits === null ? "待查询" : meter.resetCredits + " 张"
                    font.pixelSize: Theme.fontSizeSmall
                    font.weight: Font.Medium
                    color: Theme.primary
                }
            }

            Item {
                width: parent.width
                height: 24
                StyledText {
                    anchors.verticalCenter: parent.verticalCenter
                    text: meter.busy ? "正在刷新…" : meter.updated ? "更新于 " + meter.updated : "尚未更新"
                    font.pixelSize: 11
                    color: Theme.surfaceVariantText
                }
                StyledText {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text: "每 " + Math.round(Math.max(60000, Number(meter.pluginData.refreshMs) || 120000) / 60000) + " 分钟自动刷新"
                    font.pixelSize: 11
                    color: Theme.surfaceVariantText
                }
            }
        }
    }
}
