import QtQuick
import qs.Common
import "CompatFactory.js" as Compat

Loader {
    id: root
    property string iconName: ""
    property var tooltipText: null
    property color iconColor: Theme.surfaceVariantText
    signal clicked

    sourceComponent: Compat.component(root, "DActionButton", "DankActionButton")
    onLoaded: {
        item.iconName = Qt.binding(function() { return root.iconName; });
        item.tooltipText = Qt.binding(function() { return root.tooltipText; });
        item.iconColor = Qt.binding(function() { return root.iconColor; });
        item.clicked.connect(function() { root.clicked(); });
    }
}
