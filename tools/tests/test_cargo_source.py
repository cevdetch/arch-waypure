"""Cargo source and frozen native resolution evidence use distinct scopes."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from optionkit.runtime_policy import load_policy


class CargoSourceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = load_policy(TOOLS / 'waypure-policy.toml')

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def capture(self):
        from optionkit.cargo_source import capture
        return capture(self.root)

    def audit(self, **kwargs):
        from optionkit.cargo_source import audit_source
        return audit_source(self.root, self.policy, **kwargs)

    def test_capture_preserves_aliases_inherited_versions_and_features_without_generic_options(self):
        self.write('Cargo.toml', '''[workspace]
members = ["app"]
[workspace.package]
version = "2.1.0"
[workspace.dependencies]
x = { package = "x11", version = "2" }
''')
        manifest = self.write('app/Cargo.toml', '''[package]
name = "app"
version.workspace = true
[dependencies]
x = { workspace = true, optional = true }
[features]
default = []
x11 = ["dep:x"]
''')
        self.write('app/build.rs', 'fn main() { panic!("must not execute"); }')
        self.write('Cargo.lock', 'version = 4\n')
        result = self.capture()
        self.assertEqual((result['backend'], result['version'], result['options']), ('cargo', '2.1.0', {}))
        package = result['cargo']['packages'][0]
        self.assertEqual((package['name'], package['version']), ('app', '2.1.0'))
        self.assertEqual(package['features']['x11'], ['dep:x'])
        dependency = package['dependencies'][0]
        self.assertEqual((dependency['alias'], dependency['package'], dependency['optional']), ('x', 'x11', True))
        self.assertEqual(result['sources']['app/Cargo.toml'], hashlib.sha256(manifest.read_bytes()).hexdigest())
        self.assertIn('app/build.rs', result['cargo']['build_scripts'])
        self.assertIn('Cargo.lock', result['cargo']['lockfiles'])

    def test_declaration_audit_warns_optional_dev_and_other_target_without_guessing_feature_names(self):
        self.write('Cargo.toml', '''[package]
name = "app"
version = "1.0.0"
[features]
x11 = []
[dependencies]
renamed = { package = "x11", version = "2", optional = true }
libxunknown = "1"
libpulse-binding = "2"
[dev-dependencies]
xcb = "1"
[target.'cfg(windows)'.dependencies]
jack = "0.1"
''')
        result = self.audit()
        policy = [item for item in result['findings'] if item['code'] in {'source.x11', 'source.pipewire'}]
        self.assertEqual(len(policy), 3)
        self.assertTrue(all(item['severity'] == 'warning' for item in policy), policy)
        messages = ' '.join(item['message'] for item in policy)
        for word in ('renamed', 'optional', 'dev', 'cfg(windows)'):
            self.assertIn(word, messages)
        self.assertFalse(any('libxunknown' in item['message'] or 'libpulse-binding' in item['message'] for item in policy))
        self.assertTrue(any(item['code'] == 'source.coverage' for item in result['findings']))

    def test_malformed_toml_and_dependency_shapes_raise_actionable_value_errors(self):
        for content in ('[package', '[package]\nname="a"\nversion="1"\n[dependencies]\nx=42\n'):
            self.write('Cargo.toml', content)
            with self.assertRaisesRegex(ValueError, 'Cargo.toml'):
                self.capture()

    def test_cargo_config_hash_changes_preserve_resolution_provenance(self):
        self.write('Cargo.toml', '[package]\nname="a"\nversion="1"\n')
        config = self.write('.cargo/config.toml', '[net]\noffline=true\n')
        captured = self.capture()
        self.assertIn('.cargo/config.toml', captured['sources'])
        config.write_text('[net]\noffline=false\n')
        self.assertNotEqual(captured['sources']['.cargo/config.toml'], self.capture()['sources']['.cargo/config.toml'])

    def test_virtual_workspace_without_release_identity_is_explicitly_unknown(self):
        self.write('Cargo.toml', '[workspace]\nmembers=[]\n')
        result = self.capture()
        self.assertEqual(result['version'], 'unknown')
        self.assertEqual(result['version_origin'], 'unknown')
        self.assertTrue(any(item['code'] == 'source.identity' for item in self.audit()['findings']))

    def fixture(self):
        self.write('Cargo.toml', '''[workspace]
members = ["app", "other"]
exclude = ["deps/x11", "deps/jack", "deps/xcb", "deps/x11-dl"]
resolver = "2"
[workspace.package]
version = "1.0.0"
''')
        self.write('app/Cargo.toml', '''[package]
name = "app"
version = "1.0.0"
[dependencies]
renamed = { package = "x11", path = "../deps/x11", optional = true }
[features]
default = []
x = ["dep:renamed"]
[build-dependencies]
jack = { path = "../deps/jack" }
[dev-dependencies]
xcb = { path = "../deps/xcb" }
[target.'cfg(windows)'.dependencies]
x11-dl = { path = "../deps/x11-dl" }
''')
        self.write('app/src/lib.rs', '')
        self.write('app/build.rs', 'fn main() { panic!("metadata must not run build scripts"); }')
        self.write('other/Cargo.toml', '[package]\nname="other"\nversion="1.0.0"\n')
        self.write('other/src/lib.rs', '')
        for name in ('x11', 'jack', 'xcb', 'x11-dl'):
            self.write(f'deps/{name}/Cargo.toml', f'[package]\nname="{name}"\nversion="1.0.0"\n')
            self.write(f'deps/{name}/src/lib.rs', '')
        executable = shutil.which('cargo')
        if not executable:
            self.skipTest('Cargo unavailable')
        environment = dict(os.environ, RUSTUP_AUTO_INSTALL='0', CARGO_NET_OFFLINE='true')
        run = subprocess.run([executable, 'generate-lockfile', '--offline'], cwd=self.root,
                             env=environment, text=True, capture_output=True)
        if run.returncode:
            self.skipTest('installed Cargo toolchain unavailable: ' + run.stderr)

    def test_native_frozen_metadata_tracks_aliases_features_build_scope_and_selected_roots(self):
        self.fixture()
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = self.audit(resolve=True, packages=('app',), no_default_features=True, features=('app/x',))
        self.assertFalse(any(item['code'] == 'cargo.metadata' for item in result['findings']), result)
        evidence = result['evidence']['resolved']
        self.assertEqual(evidence['selected_packages'], ['app'])
        self.assertEqual({item['name'] for item in evidence['runtime']}, {'app', 'x11'})
        self.assertEqual({item['name'] for item in evidence['build']}, {'jack'})
        self.assertEqual(evidence['features'][evidence['selected_ids'][0]], ['x'])
        edge = next(edge for edge in evidence['edges'] if edge['alias'] == 'renamed')
        self.assertIn('x11', edge['package'])
        self.assertTrue(any(item['code'] == 'cargo.scope' and 'union' in item['message'] for item in result['findings']))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_resolution_without_optional_feature_and_unknown_selected_package_stays_honest(self):
        self.fixture()
        result = self.audit(resolve=True, packages=('app',), no_default_features=True)
        self.assertEqual({item['name'] for item in result['evidence']['resolved']['runtime']}, {'app'})
        missing = self.audit(resolve=True, packages=('nonexistent',))
        self.assertTrue(any(item['code'] == 'cargo.selection' and item['severity'] == 'error'
                            for item in missing['findings']), missing)
        self.assertNotIn('resolved', missing['evidence'])

    def test_missing_lockfile_fails_frozen_resolution_without_generating_lock(self):
        self.fixture()
        lock = self.root / 'Cargo.lock'
        lock.unlink()
        result = self.audit(resolve=True)
        self.assertTrue(any(item['code'] == 'cargo.metadata' and item['severity'] == 'error'
                            for item in result['findings']), result)
        self.assertFalse(lock.exists())

    def test_metadata_uses_installed_default_toolchain_despite_source_override(self):
        self.fixture()
        self.write('rust-toolchain.toml', '[toolchain]\nchannel="unavailable-optionkit-test-toolchain"\n')
        result = self.audit(resolve=True, packages=('app',), no_default_features=True)
        self.assertIn('resolved', result['evidence'], result)
        self.assertFalse(any(item['code'] == 'cargo.metadata' for item in result['findings']), result)

    def test_nested_invalid_toml_types_raise_value_error_instead_of_traceback(self):
        for section in ('workspace = []', '[workspace]\npackage = []',
                        '[package]\nname="a"\nversion="1"\n[dependencies]\nx={optional="yes"}',
                        '[package]\nname="a"\nversion="1"\n[dependencies]\nx={features="a"}'):
            with self.subTest(section=section):
                self.write('Cargo.toml', section + '\n')
                with self.assertRaisesRegex(ValueError, 'Cargo.toml'):
                    self.capture()

    def test_selected_root_does_not_claim_workspace_feature_union_is_an_actual_build(self):
        self.fixture()
        self.write('other/Cargo.toml', '[package]\nname="other"\nversion="1.0.0"\n[dependencies]\napp={path="../app",features=["x"]}\n')
        run = subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=self.root,
                             env=dict(os.environ, RUSTUP_AUTO_INSTALL='0'), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = self.audit(resolve=True, packages=('app',), no_default_features=True)
        self.assertIn('x11', {item['name'] for item in result['evidence']['resolved']['runtime']})
        findings = [item for item in result['findings'] if item['code'] == 'cargo.x11']
        self.assertTrue(findings, result)
        self.assertTrue(all(item['severity'] == 'warning' for item in findings))

    def test_explicit_cargo_home_preserves_rustup_home_and_requires_no_download(self):
        self.fixture()
        home = self.root / 'empty-cargo-home'
        result = self.audit(resolve=True, packages=('other',), cargo_home=home)
        self.assertIn('resolved', result['evidence'], result)
        self.assertEqual(result['evidence']['resolved']['selected_packages'], ['other'])

    def test_unexpected_lockfile_change_invalidates_resolution_evidence(self):
        self.write('Cargo.toml', '[package]\nname="a"\nversion="1"\n')
        self.write('Cargo.lock', 'version=4\n')
        def change_lock(*args, **kwargs):
            self.write('Cargo.lock', 'version=3\n')
            return subprocess.CompletedProcess(args, 0, '{}', '')
        with patch('optionkit.cargo_source.shutil.which', return_value='/usr/bin/cargo-direct-for-test'), \
                patch('optionkit.cargo_source.subprocess.run', side_effect=change_lock):
            result = self.audit(resolve=True)
        self.assertNotIn('resolved', result['evidence'])
        self.assertTrue(any(item['code'] == 'cargo.changed' and item['severity'] == 'error'
                            for item in result['findings']))

    def test_metadata_pins_compiler_and_disables_configured_wrappers_without_project_execution(self):
        self.fixture()
        marker = self.root / 'EXECUTED'
        compiler = self.write('project-compiler', '#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
        compiler.chmod(0o755)
        configs = [
            '[build]\nrustc="' + str(compiler) + '"\n',
            '[build]\nrustc-wrapper="' + str(compiler) + '"\n',
            '[build]\nrustc-workspace-wrapper="' + str(compiler) + '"\n',
            '[env]\nRUSTC={value="' + str(compiler) + '",force=true}\n',
            '[env]\nRUSTC_WRAPPER={value="' + str(compiler) + '",force=true}\n',
        ]
        for config in configs:
            with self.subTest(config=config):
                self.write('.cargo/config.toml', config)
                result = self.audit(resolve=True, packages=('other',))
                self.assertFalse(marker.exists(), result)
                if config.startswith('[env]'):
                    self.assertTrue(any(item['code'] == 'cargo.config' for item in result['findings']), result)
                else:
                    self.assertIn('resolved', result['evidence'], result)

    def test_nonvirtual_workspace_uses_cargo_default_members_instead_of_root_package(self):
        self.fixture()
        self.write('Cargo.toml', '[package]\nname="root-app"\nversion="1.0.0"\n[workspace]\nmembers=["app","other"]\ndefault-members=["other"]\nexclude=["deps/x11","deps/jack","deps/xcb","deps/x11-dl"]\nresolver="2"\n')
        self.write('src/lib.rs', '')
        run = subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=self.root,
                             env=dict(os.environ, RUSTUP_AUTO_INSTALL='0'), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = self.audit(resolve=True)
        self.assertEqual(result['evidence']['resolved']['selected_packages'], ['other'])
        member = self.audit(resolve=True, manifest='app/Cargo.toml')
        self.assertEqual(member['evidence']['resolved']['selected_packages'], ['app'])

    def test_nested_workspace_uses_its_own_inheritance(self):
        self.write('Cargo.toml', '[workspace]\nmembers=[]\n[workspace.package]\nversion="1.0.0"\n[workspace.dependencies]\napi={package="x11",version="1"}\n')
        self.write('nested/Cargo.toml', '[workspace]\nmembers=["inner"]\n[workspace.package]\nversion="9.0.0"\n[workspace.dependencies]\napi={package="jack",version="1"}\n')
        self.write('nested/inner/Cargo.toml', '[package]\nname="inner"\nversion.workspace=true\n[dependencies]\napi.workspace=true\n')
        package = self.capture()['cargo']['packages'][0]
        self.assertEqual(package['version'], '9.0.0')
        self.assertEqual(package['dependencies'][0]['package'], 'jack')

    def test_malformed_duplicate_native_identities_do_not_hide_forbidden_crates(self):
        from optionkit.cargo_source import _resolved
        package = {'id': 'one', 'name': 'x11', 'version': '1', 'manifest_path': str(self.root / 'Cargo.toml')}
        node = {'id': 'one', 'features': [], 'deps': []}
        metadata = {'packages': [package], 'workspace_members': ['one'],
                    'workspace_default_members': ['one'], 'workspace_root': str(self.root),
                    'resolve': {'nodes': [node]}}
        for key in ('packages', 'nodes'):
            with self.subTest(key=key):
                data = dict(metadata)
                if key == 'packages':
                    data['packages'] = [package, {**package, 'name': 'allowed'}]
                else:
                    data['resolve'] = {'nodes': [node, node]}
                with self.assertRaises(ValueError):
                    _resolved(data, (), self.root / 'Cargo.toml')

    def test_metadata_clears_inherited_compiler_and_wrapper_environment(self):
        self.fixture()
        marker = self.root / 'EXECUTED'
        compiler = self.write('project-compiler', '#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
        compiler.chmod(0o755)
        injected = {key: str(compiler) for key in ('RUSTC', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER',
                    'CARGO_BUILD_RUSTC', 'CARGO_BUILD_RUSTC_WRAPPER', 'CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER')}
        with patch.dict(os.environ, injected):
            result = self.audit(resolve=True, packages=('other',))
        self.assertFalse(marker.exists(), result)
        self.assertIn('resolved', result['evidence'], result)

    def test_ancestor_and_cargo_home_env_configuration_are_rejected_before_resolution(self):
        parent = self.root
        self.root = parent / 'source'
        self.root.mkdir()
        self.fixture()
        config = parent / '.cargo/config.toml'
        config.parent.mkdir()
        config.write_text('[env]\nLD_PRELOAD={value="/project/library.so",force=true}\n')
        result = self.audit(resolve=True)
        self.assertNotIn('resolved', result['evidence'])
        self.assertTrue(any(item['code'] == 'cargo.config' and str(config) in item['message'] for item in result['findings']))
        config.unlink()
        home = parent / 'cargo-home'
        home.mkdir()
        (home / 'config.toml').write_text('[env]\nLD_PRELOAD="/project/library.so"\n')
        result = self.audit(resolve=True, cargo_home=home)
        self.assertNotIn('resolved', result['evidence'])
        self.assertTrue(any(item['code'] == 'cargo.config' for item in result['findings']))

    def test_reviewed_macos_deployment_environment_allows_unchanged_frozen_resolution(self):
        self.fixture()
        values = ('"10.15.7"', '{value="11",force=true}',
                  '{value="12.3",force=false,relative=false}')
        for value in values:
            with self.subTest(value=value):
                config = self.write('.cargo/config.toml', '[env]\nMACOSX_DEPLOYMENT_TARGET=' + value + '\n')
                before = config.read_bytes()
                with patch.dict(os.environ, {'MACOSX_DEPLOYMENT_TARGET': '13.0'}):
                    result = self.audit(resolve=True, packages=('other',))
                self.assertIn('resolved', result['evidence'], result['findings'])
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual(result['evidence']['request']['configurations'][str(config)], hashlib.sha256(before).hexdigest())
                self.assertIn('MACOSX_DEPLOYMENT_TARGET', result['evidence']['request']['compiler_query']['reviewed_environment'])

    def test_reviewed_environment_partial_tables_keep_cargo_native_config_merging(self):
        self.fixture()
        home = self.root / 'cargo-home'
        home.mkdir()
        shared = home / 'config.toml'
        shared.write_text('[env]\nMACOSX_DEPLOYMENT_TARGET={value="10.15"}\n')
        config = self.write('.cargo/config.toml', '[env]\nMACOSX_DEPLOYMENT_TARGET={force=true}\n')
        result = self.audit(resolve=True, packages=('other',), cargo_home=home)
        self.assertIn('resolved', result['evidence'], result['findings'])
        self.assertEqual(set(result['evidence']['request']['configurations']), {str(shared), str(config)})
        shared.write_text('[env]\nMACOSX_DEPLOYMENT_TARGET={value="not-a-version"}\n')
        result = self.audit(resolve=True, packages=('other',), cargo_home=home)
        self.assertNotIn('resolved', result['evidence'])
        self.assertTrue(any(item['code'] == 'cargo.config' for item in result['findings']))

    def test_incomplete_reviewed_environment_table_is_validated_by_cargo(self):
        self.fixture()
        self.write('.cargo/config.toml', '[env]\nMACOSX_DEPLOYMENT_TARGET={force=true}\n')
        home = self.root / 'empty-cargo-home'
        home.mkdir()
        result = self.audit(resolve=True, packages=('other',), cargo_home=home)
        self.assertNotIn('resolved', result['evidence'])
        self.assertTrue(any(item['code'] == 'cargo.metadata' for item in result['findings']), result['findings'])
        self.assertFalse(any(item['code'] == 'cargo.config' for item in result['findings']), result['findings'])

    def test_malformed_unreviewed_or_relative_environment_stays_rejected(self):
        self.fixture()
        values = ('MACOSX_DEPLOYMENT_TARGET=true', 'MACOSX_DEPLOYMENT_TARGET="10.15; command"',
                  'MACOSX_DEPLOYMENT_TARGET={value="10.15",relative=true}',
                  'MACOSX_DEPLOYMENT_TARGET={value="10.15",force="yes"}',
                  'LD_PRELOAD="/tmp/library.so"',
                  'RUSTC_BOOTSTRAP="1"', 'UNREVIEWED="value"')
        for value in values:
            with self.subTest(value=value):
                self.write('.cargo/config.toml', '[env]\n' + value + '\n')
                result = self.audit(resolve=True, packages=('other',))
                self.assertNotIn('resolved', result['evidence'])
                self.assertTrue(any(item['code'] == 'cargo.config' and item['severity'] == 'error'
                                    for item in result['findings']), result['findings'])

    @unittest.skipUnless(shutil.which('cc', path=os.defpath), 'C compiler needed for a temporary constructor sentinel')
    def test_metadata_suppresses_custom_codegen_plugins_and_inherited_rustflags(self):
        self.fixture()
        marker = self.root / 'PLUGIN_EXECUTED'
        source = self.write('sentinel.c', '#include <stdio.h>\n__attribute__((constructor)) static void sentinel(void) { FILE *f=fopen(' + json.dumps(str(marker)) + ',"w"); if(f){fputs("executed",f);fclose(f);} }\n')
        plugin = self.root / 'sentinel.so'
        compiled = subprocess.run([shutil.which('cc', path=os.defpath), '-shared', '-fPIC', '-o', str(plugin), str(source)],
                                  text=True, capture_output=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        flag = '-Zcodegen-backend=' + str(plugin)
        variants = [('source', {}), ('RUSTFLAGS', {'RUSTFLAGS': flag}),
                    ('CARGO_ENCODED_RUSTFLAGS', {'CARGO_ENCODED_RUSTFLAGS': flag})]
        for variant, injected in variants:
            with self.subTest(variant=variant):
                marker.unlink(missing_ok=True)
                flags = [flag] if variant == 'source' else ['--cfg', 'custom_build_cfg']
                self.write('.cargo/config.toml', '[build]\nrustflags=' + json.dumps(flags) + '\n')
                with patch.dict(os.environ, {'RUSTC_BOOTSTRAP': '1', **injected}):
                    result = self.audit(resolve=True, packages=('app',), features=('app/x',), no_default_features=True)
                self.assertFalse(marker.exists(), 'Cargo metadata executed custom codegen backend from ' + variant)
                self.assertIn('resolved', result['evidence'], result['findings'])
                selected = result['evidence']['resolved']['selected_ids'][0]
                self.assertEqual(result['evidence']['resolved']['features'][selected], ['x'])
                self.assertEqual(result['evidence']['request']['compiler_query']['rustflags'], 'suppressed')
                self.assertTrue(any(item['code'] == 'cargo.scope' and 'custom cfg' in item['message']
                                    for item in result['findings']))

    def test_invalid_native_json_is_reported_without_claiming_resolved_coverage(self):
        self.write('Cargo.toml', '[package]\nname="a"\nversion="1"\n')
        with patch('optionkit.cargo_source.shutil.which', return_value='/usr/bin/cargo-direct-for-test'), \
                patch('optionkit.cargo_source.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{"packages": [42]}', '')):
            result = self.audit(resolve=True)
        self.assertNotIn('resolved', result['evidence'])
        self.assertTrue(any(item['code'] == 'cargo.metadata' for item in result['findings']))


if __name__ == '__main__':
    unittest.main()
