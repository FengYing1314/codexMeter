"""用离屏 QML 和临时模块验证控件兼容，不连接桌面或读取账号数据。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QML = next((str(path) for path in (
    Path("/usr/lib/qt6/bin/qml"), Path("/usr/lib64/qt6/bin/qml")
) if path.is_file()), None) or shutil.which("qml6") or shutil.which("qml")


@unittest.skipUnless(QML, "未安装 Qt 6 qml 工具，跳过离屏控件兼容测试")
class QmlCompatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="meter qml ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.modules = self.directory / "modules"
        compat = self.directory / "compat"
        compat.mkdir()
        for name in ("CompatIcon.qml", "CompatActionButton.qml",
                     "CompatDropdown.qml", "CompatFactory.js"):
            shutil.copy2(ROOT / "compat" / name, compat / name)
        self.write_module("qs.Common", {
            "Theme": """pragma Singleton
import QtQuick
QtObject {
    readonly property color surfaceText: "#eeeeee"
    readonly property color surfaceVariantText: "#cccccc"
}
"""
        }, singletons={"Theme"})

    def write_module(self, uri, components, singletons=()):
        directory = self.modules.joinpath(*uri.split("."))
        directory.mkdir(parents=True, exist_ok=True)
        entries = ["module " + uri]
        for name, source in components.items():
            (directory / (name + ".qml")).write_text(source)
            prefix = "singleton " if name in singletons else ""
            entries.append(f"{prefix}{name} 1.0 {name}.qml")
        (directory / "qmldir").write_text("\n".join(entries) + "\n")

    def widget_module(self, modern, broken=False):
        backend = "modern" if modern else "legacy"
        prefix = "D" if modern else "Dank"
        header = 'import QtQuick\n'
        if broken:
            header += 'import MissingCompatDependency\n'
        marker = f'readonly property string fixtureBackend: "{backend}"\n'
        self.write_module("qs.DCommon.Widgets" if modern else "qs.Widgets", {
            prefix + "Icon": header + "Item {\n" + marker + """
    property string name: ""
    property real size: 16
    property color color: "white"
    implicitWidth: Math.round(size)
    implicitHeight: Math.round(size)
}
""",
            prefix + "ActionButton": header + "Item {\n" + marker + """
    property string iconName: ""
    property var tooltipText: null
    property color iconColor: "white"
    BUTTON_WIDTH: 37
    BUTTON_HEIGHT: 29
    signal clicked
}
""".replace("BUTTON_WIDTH", "implicitWidth" if modern else "width")
    .replace("BUTTON_HEIGHT", "implicitHeight" if modern else "height"),
            prefix + "Dropdown": header + "Item {\n" + marker + """
    property string text: ""
    property string description: ""
    property var options: []
    property string currentValue: ""
    width: parent.width
    implicitHeight: 56
    signal valueChanged(string value)
}
""",
        })

    def run_qml(self, source):
        harness = self.directory / "Harness.qml"
        harness.write_text(source)
        runtime = self.directory / "runtime"
        runtime.mkdir(mode=0o700)
        env = dict(os.environ)
        for key in ("QML_IMPORT_PATH", "QML2_IMPORT_PATH", "DMS_SHELL_DIR"):
            env.pop(key, None)
        env.update(QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                   QT_QPA_PLATFORMTHEME="", QML_DISABLE_DISK_CACHE="1",
                   QT_LOGGING_RULES="*.debug=false;qml.debug=true",
                   QT_LOGGING_TO_CONSOLE="1", QT_MESSAGE_PATTERN="%{message}",
                   XDG_RUNTIME_DIR=str(runtime),
                   XDG_CONFIG_HOME=str(self.directory / "config"),
                   XDG_CACHE_HOME=str(self.directory / "cache"),
                   XDG_DATA_HOME=str(self.directory / "data"))
        result = subprocess.run([QML, "-I", str(self.modules),
                                 str(harness)], cwd=self.directory, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def check_bindings_and_signals(self, backend):
        self.widget_module(modern=False)
        if backend == "modern":
            self.widget_module(modern=True)
        output = self.run_qml("""
import QtQuick
import "./compat"
Item {
    id: root
    width: 480
    height: 320
    property string title: "first"
    property string description: "initial description"
    property string tooltip: "initial tooltip"
    property real glyphSize: 19
    property color tint: "#123456"
    property var choices: ["first", "second"]
    property string selected: "first"
    property int clicks: 0
    property string emittedValue: ""

    CompatIcon {
        id: icon
        name: root.title
        size: root.glyphSize
        color: root.tint
    }
    CompatActionButton {
        id: button
        iconName: root.title
        tooltipText: root.tooltip
        iconColor: root.tint
        onClicked: root.clicks++
    }
    CompatDropdown {
        id: dropdown
        text: root.title
        description: root.description
        options: root.choices
        currentValue: root.selected
        onValueChanged: value => root.emittedValue = value
    }
    function equal(actual, expected, label) {
        if (actual !== expected)
            throw new Error(label + ": " + actual + " != " + expected);
    }
    function run(check) {
        try { check(); }
        catch (error) {
            console.error("COMPAT_TEST_FAILED " + error);
            Qt.exit(1);
        }
    }
    function verifyProperties() {
        equal(icon.item.name, title, "icon name");
        equal(icon.item.size, glyphSize, "icon size");
        equal(String(icon.item.color), String(tint), "icon color");
        equal(icon.width, glyphSize, "icon loader width");
        equal(icon.height, glyphSize, "icon loader height");
        equal(icon.item.width, glyphSize, "actual icon width");
        equal(icon.item.height, glyphSize, "actual icon height");
        equal(button.item.iconName, title, "button icon");
        equal(button.item.tooltipText, tooltip, "button tooltip");
        equal(String(button.item.iconColor), String(tint), "button color");
        equal(button.width, 37, "button loader width");
        equal(button.height, 29, "button loader height");
        equal(dropdown.item.text, title, "dropdown text");
        equal(dropdown.item.description, description, "dropdown description");
        equal(dropdown.item.options.join("|"), choices.join("|"), "dropdown options");
        equal(dropdown.item.currentValue, selected, "dropdown current value");
        equal(dropdown.width, root.width, "dropdown loader width");
        equal(dropdown.item.width, root.width, "actual dropdown width");
    }
    function initial() {
        for (const control of [icon, button, dropdown])
            equal(control.item.fixtureBackend, "BACKEND", "selected backend");
        verifyProperties();
        title = "updated";
        description = "updated description";
        tooltip = "updated tooltip";
        glyphSize = 31;
        tint = "#abcdef";
        choices = ["updated", "third"];
        selected = "updated";
        root.width = 560;
        Qt.callLater(function() { root.run(root.updated); });
    }
    function updated() {
        verifyProperties();
        button.item.clicked();
        equal(clicks, 1, "button clicked forwarding");
        dropdown.item.valueChanged("third");
        equal(emittedValue, "third", "dropdown valueChanged forwarding");
        console.log("COMPAT_TEST_PASSED");
        Qt.exit(0);
    }
    Timer {
        interval: 10
        running: true
        repeat: true
        onTriggered: {
            if (icon.item && button.item && dropdown.item) {
                stop();
                root.run(root.initial);
            }
        }
    }
    Timer {
        interval: 2000
        running: true
        onTriggered: {
            console.error("COMPAT_TEST_FAILED controls did not load");
            Qt.exit(1);
        }
    }
}
""".replace("BACKEND", backend))
        self.assertIn("COMPAT_TEST_PASSED", output)
        self.assertNotIn("Binding loop", output)

    def test_modern_bindings_sizes_and_signals(self):
        self.check_bindings_and_signals("modern")

    def test_legacy_bindings_sizes_and_signals(self):
        self.check_bindings_and_signals("legacy")

    def test_broken_modern_module_does_not_fall_back(self):
        self.widget_module(modern=False)
        self.widget_module(modern=True, broken=True)
        output = self.run_qml("""
import QtQuick
import "./compat"
Item {
    CompatIcon { id: icon }
    Timer {
        interval: 100
        running: true
        onTriggered: {
            if (icon.item) {
                console.error("COMPAT_TEST_FAILED silently loaded a fallback");
                Qt.exit(1);
                return;
            }
            console.log("NO_SILENT_FALLBACK");
            Qt.exit(0);
        }
    }
}
""")
        self.assertIn("NO_SILENT_FALLBACK", output)
        self.assertIn("MissingCompatDependency", output)
