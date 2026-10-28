import QtQuick
import "CompatFactory.js" as Compat

Loader {
    id: root
    property string text: ""
    property string description: ""
    property var options: []
    property string currentValue: ""
    signal valueChanged(string value)

    width: parent ? parent.width : 0
    sourceComponent: Compat.component(root, "DDropdown", "DankDropdown")
    onLoaded: {
        item.text = Qt.binding(function() { return root.text; });
        item.description = Qt.binding(function() { return root.description; });
        item.options = Qt.binding(function() { return root.options; });
        item.currentValue = Qt.binding(function() { return root.currentValue; });
        item.valueChanged.connect(function(value) { root.valueChanged(value); });
    }
}
