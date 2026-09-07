"""在临时配置目录验证安装，不更改桌面服务。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def run_install(self, config):
        return subprocess.run(["sh", str(ROOT / "install.sh")],
                              env=dict(os.environ, XDG_CONFIG_HOME=str(config)),
                              capture_output=True, text=True, timeout=10)

    def test_custom_config_and_repeat(self):
        with tempfile.TemporaryDirectory(prefix="meter config ") as directory:
            config = Path(directory)
            target = config / "DankMaterialShell/plugins/codexMeter"
            self.assertEqual(self.run_install(config).returncode, 0)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), ROOT)
            self.assertEqual(self.run_install(config).returncode, 0)

    def test_existing_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            target = config / "DankMaterialShell/plugins/codexMeter"
            target.mkdir(parents=True)
            (target / "user-file").write_text("keep")
            self.assertNotEqual(self.run_install(config).returncode, 0)
            self.assertEqual((target / "user-file").read_text(), "keep")

    def test_broken_link_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            target = config / "DankMaterialShell/plugins/codexMeter"
            target.parent.mkdir(parents=True)
            target.symlink_to(config / "missing")
            self.assertNotEqual(self.run_install(config).returncode, 0)
            self.assertEqual(os.readlink(target), str(config / "missing"))
