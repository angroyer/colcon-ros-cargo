# Copyright 2024 Open Source Robotics Foundation, Inc.
# Licensed under the Apache License, Version 2.0

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import xml.etree.ElementTree as eTree

from colcon_core.event_handler.console_direct import ConsoleDirectEventHandler
from colcon_core.package_descriptor import PackageDescriptor
from colcon_core.subprocess import new_event_loop
from colcon_core.task import TaskContext
from colcon_ros.package_identification.ros import RosPackageIdentification
from colcon_ros_cargo.task.ament_cargo.build import (
    _resolve_cargo_package_path,
    AmentCargoBuildTask,
)
from colcon_ros_cargo.task.ament_cargo.test import AmentCargoTestTask
import pytest
import toml

TEST_PACKAGE_NAME = 'rust-sample-package'

test_project_path = Path(__file__).parent / TEST_PACKAGE_NAME


@pytest.fixture(autouse=True)
def monkey_patch_put_event_into_queue(monkeypatch):
    event_handler = ConsoleDirectEventHandler()
    monkeypatch.setattr(
        TaskContext,
        'put_event_into_queue',
        lambda self, event: event_handler((event, 'cargo')),
    )


def test_package_identification():
    cpi = RosPackageIdentification()
    desc = PackageDescriptor(test_project_path)
    cpi.identify(desc)
    assert desc.type == 'ros.ament_cargo'
    assert desc.name == TEST_PACKAGE_NAME


@pytest.mark.skipif(
    not shutil.which('cargo'),
    reason='Rust must be installed to run this test')
def test_build_and_test_package():
    event_loop = new_event_loop()
    asyncio.set_event_loop(event_loop)

    try:
        cpi = RosPackageIdentification()
        package = PackageDescriptor(test_project_path)
        cpi.identify(package)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            # TODO(luca) Also test clean build and cargo args
            context = TaskContext(pkg=package,
                                  args=SimpleNamespace(
                                      path=str(test_project_path),
                                      build_base=str(tmpdir / 'build'),
                                      install_base=str(tmpdir / 'install'),
                                      clean_build=None,
                                      cargo_args=None,
                                      lookup_in_workspace=None,
                                      config_path=None,
                                  ),
                                  dependencies={}
                                  )

            task = AmentCargoBuildTask()
            task.set_context(context=context)

            src_base = test_project_path / 'src'

            source_files_before = set(src_base.rglob('*'))
            rc = event_loop.run_until_complete(task.build())
            assert not rc
            source_files_after = set(src_base.rglob('*'))
            assert source_files_before == source_files_after

            # Make sure the binary is compiled
            install_base = Path(task.context.args.install_base)
            app_name = TEST_PACKAGE_NAME
            executable = TEST_PACKAGE_NAME
            # Executable in windows have a .exe extension
            if os.name == 'nt':
                executable += '.exe'
            assert (install_base / 'lib' / app_name / executable).is_file()

            # Now compile tests
            task = AmentCargoTestTask()
            task.set_context(context=context)

            # Expect tests to have failed but return code will still be 0
            # since testing run succeeded
            rc = event_loop.run_until_complete(task.test())
            assert not rc
            build_base = Path(task.context.args.build_base)

            # Make sure the testing files are built
            assert (build_base / 'debug' / 'deps').is_dir()
            assert len(os.listdir(build_base / 'debug' / 'deps')) > 0
            result_file_path = build_base / 'cargo_test.xml'
            assert result_file_path.is_file()
            check_result_file(result_file_path)

    finally:
        event_loop.close()


# Check the testing result file, expect cargo test and doc test to fail
# but fmt to succeed
def check_result_file(path):
    tree = eTree.parse(path)
    root = tree.getroot()
    testsuite = root.find('testsuite')
    assert testsuite is not None
    unit_result = testsuite.find("testcase[@name='unit']")
    assert unit_result is not None
    assert unit_result.find('failure') is not None
    fmt_result = testsuite.find("testcase[@name='fmt']")
    assert fmt_result is not None
    assert fmt_result.find('failure') is None


def test_incomplete_patch_package_uses_stable_stub(tmp_path):
    installed_package = tmp_path / 'install' / 'share' / 'ros2_foo' / 'rust'
    installed_package.mkdir(parents=True)
    manifest = installed_package / 'Cargo.toml'
    manifest.write_text('[package]\nname = "another_crate"\n')
    original_manifest = manifest.read_text()

    stub_path = _resolve_cargo_package_path(
        'ros2_foo', installed_package, tmp_path / '.cargo' / 'stubs')

    assert stub_path == tmp_path / '.cargo' / 'stubs' / 'ros2_foo'
    assert 'name = "ros2_foo"' in (stub_path / 'Cargo.toml').read_text()
    assert manifest.read_text() == original_manifest


def test_valid_patch_package_keeps_installed_path(tmp_path):
    installed_package = tmp_path / 'install' / 'share' / 'ros2_foo' / 'rust'
    installed_package.mkdir(parents=True)
    (installed_package / 'Cargo.toml').write_text(
        '[package]\nname = "ros2_foo"\nversion = "1.0.0"\n')

    resolved_path = _resolve_cargo_package_path(
        'ros2_foo', installed_package, tmp_path / '.cargo' / 'stubs')

    assert resolved_path == installed_package
    assert not (tmp_path / '.cargo' / 'stubs').exists()


def test_legacy_install_stub_uses_stable_stub(tmp_path):
    installed_package = tmp_path / 'install' / 'share' / 'ros2_foo' / 'rust'
    src_dir = installed_package / 'src'
    src_dir.mkdir(parents=True)
    manifest = installed_package / 'Cargo.toml'
    manifest.write_text(
        '[package]\nname = "ros2_foo"\nversion = "0.0.0"\n'
        'edition = "2021"\n\n[lib]\npath = "src/lib.rs"\n')
    (src_dir / 'lib.rs').write_text('')

    stub_path = _resolve_cargo_package_path(
        'ros2_foo', installed_package, tmp_path / '.cargo' / 'stubs')

    assert stub_path == tmp_path / '.cargo' / 'stubs' / 'ros2_foo'
    assert manifest.exists()
    assert (installed_package / 'src' / 'lib.rs').read_text() == ''


def test_stable_stub_remains_unchanged(tmp_path):
    stable_stub = tmp_path / '.cargo' / 'stubs' / 'ros2_foo'
    stable_stub.mkdir(parents=True)
    (stable_stub / 'src').mkdir()
    (stable_stub / 'src' / 'lib.rs').write_text('')
    (stable_stub / 'Cargo.toml').write_text(
        '[package]\nname = "ros2_foo"\nversion = "0.0.0"\n'
        'edition = "2021"\n\n[lib]\npath = "src/lib.rs"\n\n'
        '[package.metadata.colcon-ros-cargo]\nstub = true\n')

    resolved_path = _resolve_cargo_package_path(
        'ros2_foo', stable_stub, tmp_path / '.cargo' / 'stubs')

    assert resolved_path == stable_stub


def test_config_writer_patches_legacy_stub_to_stable_path(tmp_path):
    installed_package = tmp_path / 'install' / 'share' / 'ros2_foo' / 'rust'
    src_dir = installed_package / 'src'
    src_dir.mkdir(parents=True)
    (installed_package / 'Cargo.toml').write_text(
        '[package]\nname = "ros2_foo"\nversion = "0.0.0"\n'
        'edition = "2021"\n\n[lib]\npath = "src/lib.rs"\n')
    (src_dir / 'lib.rs').write_text('')

    context = TaskContext(
        pkg=PackageDescriptor(test_project_path),
        args=SimpleNamespace(),
        dependencies={})
    task = AmentCargoBuildTask()
    task.set_context(context=context)
    task.write_cargo_config_toml(
        {'ros2_foo': str(installed_package)}, config_path=tmp_path)

    config = toml.load(str(tmp_path / '.cargo' / 'config.toml'))
    patched_path = config['patch']['crates-io']['ros2_foo']['path']
    assert Path(patched_path) == tmp_path / '.cargo' / 'stubs' / 'ros2_foo'
