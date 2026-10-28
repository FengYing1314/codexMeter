"""在临时配置目录验证安装，不更改桌面服务。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="meter install ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = self.directory / "config with spaces"
        self.home = self.directory / "home"
        self.home.mkdir()
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.calls = self.directory / "dms-calls"
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": str(self.bin),
            "DMS_TEST_CALLS": str(self.calls),
        }
        for name in ("sh", "dirname", "readlink", "mkdir", "ln", "sed"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, name)
            (self.bin / name).symlink_to(executable)
        (self.bin / "python3").symlink_to(sys.executable)

    def fake_dms(self, output, returncode=0):
        script = self.bin / "dms"
        script.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$*" >> "$DMS_TEST_CALLS"\n'
            'printf "%s\\n" "$DMS_TEST_OUTPUT"\n'
            'exit "$DMS_TEST_EXIT"\n'
        )
        script.chmod(0o755)
        self.env.update(DMS_TEST_OUTPUT=output, DMS_TEST_EXIT=str(returncode))

    def run_install(self, *args, config=None):
        env = dict(self.env, XDG_CONFIG_HOME=str(config or self.config))
        return subprocess.run([str(self.bin / "sh"), str(ROOT / "install.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=10)

    def target(self, major, config=None):
        shell = "DankMaterialShell" if major == 1 else "dms"
        return (config or self.config) / shell / "plugins/codexMeter"

    def assert_installed(self, major, config=None):
        target = self.target(major, config)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), ROOT)
        self.assertFalse(self.target(3 - major, config).exists())

    def assert_not_installed(self):
        self.assertFalse(self.config.exists())
        self.assertFalse((self.home / ".config").exists())

    def test_detects_dms_major_and_repeat_install(self):
        for major, version in ((1, "dms v1.6.3"), (2, "dms v2.0.0")):
            with self.subTest(major=major):
                config = self.directory / f"config for DMS {major}"
                self.fake_dms(version)
                for _ in range(2):
                    result = self.run_install(config=config)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_installed(major, config)
        self.assertEqual(self.calls.read_text().splitlines(), ["version"] * 4)

    def test_without_dms_defaults_to_major_two(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed(2)
        self.assertFalse(self.calls.exists())

    def test_explicit_major_does_not_call_dms(self):
        self.fake_dms("unrecognized output", returncode=42)
        for major in (1, 2):
            with self.subTest(major=major):
                config = self.directory / f"explicit DMS {major}"
                result = self.run_install("--dms-major", str(major), config=config)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_installed(major, config)
        self.assertFalse(self.calls.exists())

    def test_invalid_detection_does_not_create_config(self):
        for output, returncode in (
            ("", 0),
            ("unknown version", 0),
            ("dms v3.0.0", 0),
            ("dms v2.0.0", 1),
        ):
            with self.subTest(output=output, returncode=returncode):
                self.fake_dms(output, returncode)
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assert_not_installed()
        self.assertEqual(self.calls.read_text().splitlines(), ["version"] * 4)

    def test_invalid_arguments_do_not_call_dms_or_create_config(self):
        self.fake_dms("dms v2.0.0")
        for args in (("--dms-major",), ("--dms-major", "3"),
                     ("--dms-major", "unknown"), ("--unknown",),
                     ("--dms-major", "2", "extra")):
            with self.subTest(args=args):
                result = self.run_install(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assert_not_installed()
        self.assertFalse(self.calls.exists())

    def test_existing_directory_is_preserved(self):
        for major in (1, 2):
            with self.subTest(major=major):
                target = self.target(major)
                target.mkdir(parents=True)
                (target / "user-file").write_text("keep")
                result = self.run_install("--dms-major", str(major))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(target.is_symlink())
                self.assertEqual((target / "user-file").read_text(), "keep")

    def test_broken_link_is_preserved(self):
        for major in (1, 2):
            with self.subTest(major=major):
                target = self.target(major)
                target.parent.mkdir(parents=True)
                missing = self.config / "missing"
                target.symlink_to(missing)
                result = self.run_install("--dms-major", str(major))
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(target.is_symlink())
                self.assertEqual(os.readlink(target), str(missing))
