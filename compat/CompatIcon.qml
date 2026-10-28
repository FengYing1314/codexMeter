import QtQuick
import qs.Common
import "CompatFactory.js" as Compat

Loader {
    id: root
    property string name: ""
    property real size: 16
    property color color: Theme.surfaceText

    sourceComponent: Compat.component(root, "DIcon", "DankIcon")
    onLoaded: {
        item.name = Qt.binding(function() { return root.name; });
        item.size = Qt.binding(function() { return root.size; });
        item.color = Qt.binding(function() { return root.color; });
    }
}
