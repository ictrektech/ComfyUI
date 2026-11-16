import pathlib
import tempfile
import unittest

import yaml

import package
from vos_version import next_version


class PackageTest(unittest.TestCase):
    def test_profiles_routes_and_archives(self):
        package.DIST.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=package.DIST.parent) as workspace:
            dir_ = pathlib.Path(workspace)
            stage = dir_ / "stage"
            images = {key: f"{package.REPOSITORY}:{sheet[0]}_test" for key, sheet in package.PROFILES.items()}
            version = "0.45.0-vos.01"
            package.render(version, images, stage, ">=0.0.108")
            manifest = yaml.safe_load((stage / "manifest.yml").read_text())
            self.assertEqual(manifest["dependencies"]["apps"][0]["version"], ">=0.0.108")
            saved, package.DIST = package.DIST, dir_
            try:
                self.assertTrue(package.package(version, stage).is_file())
            finally:
                package.DIST = saved

    def test_upstream_revision_reset(self):
        self.assertEqual(next_version("0.45.0", "0.45.0"), "0.45.0-vos.01")
        self.assertEqual(next_version("0.45.0-vos.01", "0.45.0"), "0.45.0-vos.02")
        self.assertEqual(next_version("0.45.0-vos.99", "0.46.0"), "0.46.0-vos.01")
