# Licensed under the Apache License, Version 2.0

import fcntl
import os
from pathlib import Path
import subprocess
import tempfile

from colcon_cargo.task.cargo import CARGO_EXECUTABLE
from colcon_cargo.task.cargo.build import CargoBuildTask
from colcon_core.logging import colcon_logger
from colcon_core.plugin_system import satisfies_version
from colcon_core.shell import create_environment_hook
from colcon_core.task import TaskExtensionPoint
import toml


logger = colcon_logger.getChild(__name__)

# Some logic needs to be executed once per run.
# There are no colcon hooks for this, so it is shoehorned into the build step
# with a global.
package_paths = None


class AmentCargoBuildTask(CargoBuildTask):
    """A build task for packages with Cargo.toml + package.xml.

    The primary problem that needs to be solved is that dependencies on other
    packages in the same workspace are expressed by just a name in colcon, but
    by a full path in Cargo.

    That means, when building a Cargo package, all the packages it depends on
    need to be resolved to full paths, and those need to be written into the
    Cargo.toml, or alternatively into a [patch] section of a .cargo/config.toml
    file. Here the latter approach is used.
    """

    def __init__(self):  # noqa: D107
        super().__init__()
        satisfies_version(TaskExtensionPoint.EXTENSION_POINT_VERSION, '^1.0')

    def add_arguments(self, *, parser):  # noqa: D102
        parser.add_argument(
            '--lookup-in-workspace',
            action='store_true',
            help='Look up dependencies in the workspace directory. '
            'By default, dependencies are looked up only in the installation '
            'prefixes. This option is useful for setting up a '
            '.cargo/config.toml for subsequent builds with cargo.')
        parser.add_argument(
            '--config-path',
            type=Path,
            help='The path to store the .cargo/config.toml '
            'By default, the configuration will be stored at the '
            'colcon workspace top level directory. Use this option to '
            'indicate a path above the resolved path of the package sources '
            '(e.g the pointed path if the source is under a symbolic link)')

    def _prepare(self, env, additional_hooks):
        ament_build = 'cargo ament-build --help'.split()
        if subprocess.run(ament_build, capture_output=True).returncode != 0:
            logger.error(
                '\n\nament_cargo package found but cargo ament-build was '
                'not detected.'
                '\n\nPlease install it by running:'
                '\n $ cargo install cargo-ament-build\n')
            return 1

        args = self.context.args

        global package_paths
        if package_paths is None:
            if args.lookup_in_workspace:
                package_paths = find_workspace_cargo_packages(args.build_base, args.install_base)  # noqa: E501
            else:
                package_paths = {}

        # Scan the install dirs, aka prefixes. Note that only those prefixes
        # will be scanned that are a dependency of the current package.
        new_package_paths = find_installed_cargo_packages(env)
        # The new_package_paths cover only the dependencies of the
        # current package, but .cargo/config.toml should contain all Rust
        # packages seen during the build process (so that you can afterwards
        # use cargo for every package in the workspace).
        # Hence, the installed package paths need to be accumulated.
        new_package_paths.update(package_paths)
        package_paths = new_package_paths
        self.write_cargo_config_toml(package_paths, args.config_path)

        additional_hooks += create_environment_hook(
            'ament_prefix_path',
            Path(self.context.args.install_base),
            self.context.pkg.name,
            'AMENT_PREFIX_PATH',
            self.context.args.install_base,
            mode='prepend')

    def _build_cmd(self, cargo_args):
        args = self.context.args
        src_dir = Path(self.context.pkg.path).resolve()
        manifest_path = str(src_dir / 'Cargo.toml')
        return [
            CARGO_EXECUTABLE, 'ament-build',
            '--install-base', args.install_base,
            '--',
            '--manifest-path', manifest_path,
            '--target-dir', args.build_base,
            '--quiet'
        ] + cargo_args

    # Installation is done by cargo ament-build
    def _install_cmd(self, cargo_args):  # noqa: D102
        pass

    def write_cargo_config_toml(self, package_paths, config_path=None):
        """Write the resolved package paths to config.toml.

        Uses file locking to serialise concurrent colcon workers and atomic
        rename so that cargo never reads a partially-written file.  Merges
        with any existing content written by other workers and stubs paths
        that do not yet contain a valid Cargo manifest.

        :param package_paths: A mapping of package names to paths
        :param config_path: Optional path for .cargo/config.toml
        """
        src_dir = Path(self.context.pkg.path)
        if config_path:
            config_dir = config_path.resolve() / '.cargo'
        else:
            config_dir = Path.cwd() / '.cargo'
            # The package path might be a link to another directory.
            # Cargo only looks for configuration in the package directory
            # and in all its parent directories.
            # Hence, if the package directory is link, ./cargo/config.toml
            # should be installed above the directory pointed to
            # in order cargo to necessarily hit it.
            if src_dir.absolute() != src_dir.resolve():
                logger.warn(
                    'The package source path may be under a symbolic link. '
                    'Use --config-path option to store .cargo/config.toml '
                    'in a way it will be hit by cargo')
        config_dir.mkdir(exist_ok=True)
        cargo_config_toml_out = config_dir / 'config.toml'
        lock_path = config_dir / 'config.toml.lock'

        with open(lock_path, 'w') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                # Merge with paths already written by other workers.
                merged = dict(package_paths)
                if cargo_config_toml_out.exists():
                    try:
                        existing = toml.load(str(cargo_config_toml_out))
                    except (OSError, toml.TomlDecodeError):
                        pass
                    else:
                        patches = existing.get('patch', {}).get(
                            'crates-io', {})
                        if isinstance(patches, dict):
                            for name, info in patches.items():
                                if isinstance(info, dict) and 'path' in info:
                                    merged.setdefault(name, info['path'])

                patches = {
                    pkg: {'path': str(_resolve_cargo_package_path(
                        pkg, path, config_dir / 'stubs'))}
                    for pkg, path in merged.items()
                }
                content = {'patch': {'crates-io': patches}}

                # Atomic write via temp-file + rename.
                fd, tmp = tempfile.mkstemp(
                    dir=str(config_dir), suffix='.tmp', prefix='config_')
                try:
                    with os.fdopen(fd, 'w') as f:
                        toml.dump(content, f)
                    os.replace(tmp, str(cargo_config_toml_out))
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _resolve_cargo_package_path(pkg, path, stubs_dir, _visited=None):
    """Return a valid package path, using a stable stub when necessary."""
    if _visited is None:
        _visited = set()
    try:
        pkg_dir = Path(path).resolve()
    except OSError:
        return _create_cargo_stub(pkg, stubs_dir)
    if pkg_dir in _visited:
        return pkg_dir
    _visited.add(pkg_dir)

    try:
        manifest = pkg_dir / 'Cargo.toml'
        cargo_toml = toml.load(str(manifest))
        package = cargo_toml.get('package', {})
        if not isinstance(package, dict) or package.get('name') != pkg:
            return _use_cargo_stub(pkg, path, stubs_dir)
        if _is_generated_cargo_stub(pkg_dir, cargo_toml):
            stable_stub = (Path(stubs_dir) / pkg).resolve()
            if pkg_dir == stable_stub:
                return pkg_dir
            return _use_cargo_stub(pkg, path, stubs_dir)

        for section in ('dependencies', 'dev-dependencies',
                        'build-dependencies'):
            for dep_name, spec in cargo_toml.get(section, {}).items():
                if isinstance(spec, dict) and 'path' in spec:
                    dep_package = spec.get('package', dep_name)
                    dep_path = pkg_dir / spec['path']
                    if _resolve_cargo_package_path(
                            dep_package, dep_path, stubs_dir,
                            _visited) != dep_path.resolve():
                        return _use_cargo_stub(pkg, path, stubs_dir)
        return pkg_dir
    except (AttributeError, OSError, TypeError, toml.TomlDecodeError,
            RuntimeError):
        return _use_cargo_stub(pkg, path, stubs_dir)


def _is_generated_cargo_stub(pkg_dir, cargo_toml):
    """Detect stable stubs and the legacy stubs written into install trees."""
    package = cargo_toml.get('package', {})
    metadata = package.get('metadata', {})
    if isinstance(metadata, dict):
        stub_metadata = metadata.get('colcon-ros-cargo', {})
        if (isinstance(stub_metadata, dict)
                and stub_metadata.get('stub') is True):
            return True

    lib = cargo_toml.get('lib', {})
    if not isinstance(lib, dict):
        return False
    if package.get('version') != '0.0.0' or lib.get('path') != 'src/lib.rs':
        return False
    try:
        return (pkg_dir / 'src' / 'lib.rs').stat().st_size == 0
    except OSError:
        return False


def _use_cargo_stub(pkg, invalid_path, stubs_dir):
    stub_path = _create_cargo_stub(pkg, stubs_dir)
    logger.warning(
        'Cargo package %s at %s is missing or incomplete; using stub at %s',
        pkg, invalid_path, stub_path)
    return stub_path


def _create_cargo_stub(pkg, stubs_dir):
    """Create a minimal package outside install prefixes for Cargo patches."""
    pkg_dir = Path(stubs_dir) / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest = pkg_dir / 'Cargo.toml'
    if not manifest.exists():
        src_dir = pkg_dir / 'src'
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / 'lib.rs').write_text('')
        manifest.write_text(toml.dumps({
            'package': {
                'name': pkg,
                'version': '0.0.0',
                'edition': '2021',
                'metadata': {'colcon-ros-cargo': {'stub': True}},
            },
            'lib': {'path': 'src/lib.rs'},
        }))
    return pkg_dir


def write_cargo_config_toml(package_paths):
    """Write the resolved package paths to config.toml.

    :param package_paths: A mapping of package names to paths
    """
    patches = {pkg: {'path': str(path)} for pkg, path in package_paths.items()}
    content = {'patch': {'crates-io': patches}}
    config_dir = Path.cwd() / '.cargo'
    config_dir.mkdir(exist_ok=True)
    cargo_config_toml_out = config_dir / 'config.toml'
    with cargo_config_toml_out.open('w') as toml_file:
        toml.dump(content, toml_file)


def find_installed_cargo_packages(env):
    """Find out which prefix contains each of the dependencies.

    :param env: Environment dict for this package
    :returns: A mapping of package names to paths
    :rtype dict(str, Path)
    """
    prefix_for_package = {}
    ament_prefix_path_var = env.get('AMENT_PREFIX_PATH')
    if ament_prefix_path_var is None:
        logger.warning('AMENT_PREFIX_PATH is empty. '
                       'You probably intended to source a ROS installation.')
        prefixes = []
    else:
        prefixes = ament_prefix_path_var.split(os.pathsep)
    for prefix in prefixes:
        prefix = Path(prefix)
        packages_dir = prefix / 'share' / 'ament_index' / 'resource_index' \
            / 'rust_packages'
        if packages_dir.exists():
            packages = {path.name for path in packages_dir.iterdir()}
        else:
            packages = set()
        for pkg in packages:
            prefix_for_package[pkg] = prefix
    return {pkg: str(prefix / 'share' / pkg / 'rust')
            for pkg, prefix in prefix_for_package.items()}


def find_workspace_cargo_packages(build_base, install_base):
    """Find Cargo packages in the workspace/current working directory.

    :param install_base: The install base of the current build
    :returns: A mapping of package names to paths
    :rtype dict(str, Path)
    """
    path_for_package = {}
    for (dirpath, dirnames, filenames) in os.walk(Path.cwd(), topdown=True):
        # Users will often build the workspace several times into differently
        # named install directories, and we don't know their names. So if we
        # just scan through the current working directory, we'll probably find
        # Rust packages in those install directories. That's not what we want,
        # so install directories (identified by a setup.sh file) should be
        # skipped.
        if dirpath == install_base or (Path(dirpath) / 'setup.sh').exists():
            # Do not descend into this directory
            dirnames[:] = []
        elif dirpath == build_base or (Path(dirpath) / 'COLCON_IGNORE').exists():  # noqa: E501
            # In particular, build dirs have a COLCON_IGNORE
            # Do not descend into this directory
            dirnames[:] = []
        elif 'Cargo.toml' in filenames:
            try:
                cargo_toml = toml.load(Path(dirpath) / 'Cargo.toml')
                name = cargo_toml['package']['name']
                path_for_package[name] = dirpath
            except toml.decoder.TomlDecodeError:
                pass
    return path_for_package
