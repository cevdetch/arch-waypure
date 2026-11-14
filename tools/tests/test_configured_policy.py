"""Configured dependency evidence is stronger than option-name guesses."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from optionkit.runtime_policy import load_policy


class ConfiguredPolicyCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.build = Path(temporary.name)
        self.data = {'backend': 'meson', 'project': 'Demo', 'version': '1.0'}
        self.info = self.build / 'meson-info'
        self.info.mkdir()
        self.write('intro-projectinfo.json', {'version': '1.0', 'descriptive_name': 'Demo'})
        self.write('intro-buildoptions.json', [{'name': 'x11', 'value': False}, {'name': 'audio', 'value': 'disabled'}])
        self.write('intro-dependencies.json', [])

    def write(self, name, data):
        (self.info / name).write_text(json.dumps(data))

    def audit(self, selected=None, desired=None):
        from optionkit.configured_policy import audit_build
        return audit_build(self.build, self.data, selected or {}, desired or {},
                          load_policy(TOOLS / 'waypure-policy.toml'))

    def test_configured_dependencies_and_link_arguments_catch_forbidden_implementations(self):
        self.write('intro-dependencies.json', [
            {'name': 'display', 'link_args': ['/usr/lib/libX11.so.6']},
            {'name': 'audio-api', 'link_args': ['-ljack']},
            {'name': 'jack', 'link_args': []},
            {'name': 'helper', 'link_args': ['-l:libjackserver.so.0']},
            {'name': 'libpulse', 'link_args': ['-lpulse']},
            {'name': 'allowed-static', 'link_args': ['/usr/lib/libxkbcommon.a']},
        ])
        result = self.audit()
        findings = result['findings']
        for code, value in (('build.x11', '/usr/lib/libX11.so.6'), ('build.pipewire', '-ljack'),
                            ('build.pipewire', 'jack'), ('build.pipewire', '-l:libjackserver.so.0')):
            self.assertTrue(any(item['code'] == code and value in item['message'] for item in findings), findings)
        self.assertFalse(any('libpulse' in item['message'] or 'libxkbcommon.a' in item['message'] for item in findings))

    def test_observed_drift_warns_and_profile_conflicts_fail_without_guessing_option_names(self):
        result = self.audit({'audio': {'value': 'enabled'}}, {'audio': {'value': 'enabled', 'groups': ['PERSONAL']}})
        self.assertTrue(any(item['code'] == 'build.drift' and item['severity'] == 'warning'
                            for item in result['findings']), result)
        self.assertTrue(any(item['code'] == 'build.policy' and item['severity'] == 'error'
                            for item in result['findings']), result)
        self.assertEqual(result['observed']['audio'], 'disabled')

    def test_missing_dependency_evidence_and_unobserved_profile_values_are_visible(self):
        (self.info / 'intro-dependencies.json').unlink()
        result = self.audit(desired={'absent': {'value': 'disabled', 'groups': ['WAYLAND']}})
        codes = {item['code'] for item in result['findings']}
        self.assertIn('build.coverage', codes)
        self.assertIn('build.unobserved', codes)

    def test_build_identity_is_checked_by_the_existing_observation_reader(self):
        self.write('intro-projectinfo.json', {'version': '2.0', 'descriptive_name': 'Demo'})
        with self.assertRaisesRegex(ValueError, 'version'):
            self.audit()

    def test_meson_probe_only_warns_but_actual_target_link_fails(self):
        self.write('intro-dependencies.json', [{'name': 'jack', 'link_args': ['-ljack']}])
        self.write('intro-targets.json', [{'name': 'allowed', 'target_sources': [{'linker': ['cc'], 'parameters': ['-lpulse']}]}])
        result = self.audit()
        policy = [item for item in result['findings'] if item['code'] == 'build.pipewire']
        self.assertTrue(policy, result)
        self.assertTrue(all(item['severity'] == 'warning' for item in policy), policy)
        self.write('intro-targets.json', [{'name': 'linked', 'target_sources': [{'linker': ['cc'], 'parameters': ['-ljack']}]}])
        result = self.audit()
        self.assertTrue(any(item['code'] == 'build.pipewire' and item['severity'] == 'error'
                            and 'target linked' in item['message'] for item in result['findings']), result)

    def test_invalid_native_evidence_shapes_are_reported_as_unusable(self):
        for name, value in (('intro-dependencies.json', {}), ('intro-targets.json', {}),
                            ('intro-buildoptions.json', [42])):
            with self.subTest(name=name):
                self.write('intro-dependencies.json', [])
                self.write('intro-targets.json', [])
                self.write('intro-buildoptions.json', [])
                self.write(name, value)
                with self.assertRaises(ValueError):
                    self.audit()

    def test_cmake_reply_older_than_cache_is_reported_as_stale(self):
        self.data = {'backend': 'cmake', 'project': 'Demo', 'version': '1.0'}
        cache = self.build / 'CMakeCache.txt'
        cache.write_text('CMAKE_PROJECT_NAME:STATIC=Demo\nCMAKE_PROJECT_VERSION:STATIC=1.0\n')
        reply = self.build / '.cmake/api/v1/reply'
        reply.mkdir(parents=True)
        index = reply / 'index-test.json'
        index.write_text(json.dumps({'objects': [{'kind': 'codemodel', 'version': {'major': 2}, 'jsonFile': 'model.json'}]}))
        (reply / 'model.json').write_text(json.dumps({'paths': {'build': str(self.build)}, 'configurations': []}))
        os.utime(index, ns=(1, 1))
        result = self.audit()
        self.assertTrue(any(item['code'] == 'build.stale' and item['severity'] == 'warning'
                            for item in result['findings']), result)

    def test_configured_identity_missing_from_cmake_cache_is_visible(self):
        self.data = {'backend': 'cmake', 'project': 'Demo', 'version': '1.0'}
        (self.build / 'CMakeCache.txt').write_text('WITH_X11:BOOL=OFF\n')
        result = self.audit()
        self.assertTrue(any(item['code'] == 'build.identity' for item in result['findings']), result)

    def test_forwarded_and_separate_library_arguments_are_checked_without_confusing_rpaths(self):
        for arguments in (['-Wl,-lX11'], ['-Xlinker', '-ljack'], ['-Wl,-l,X11'],
                          ['-l', 'jack'], ['-Xlinker', '-l', '-Xlinker', 'X11']):
            with self.subTest(arguments=arguments):
                self.write('intro-targets.json', [{'name': 'linked', 'target_sources': [
                    {'linker': ['cc'], 'parameters': arguments}]}])
                result = self.audit()
                self.assertTrue(any(item['severity'] == 'error' and item['code'] in {'build.x11', 'build.pipewire'}
                                    for item in result['findings']), result)
        self.write('intro-targets.json', [{'name': 'safe', 'target_sources': [{'linker': ['cc'], 'parameters': [
            '-Wl,-rpath,/libX11.so.6', '-Xlinker', '-rpath', '-Xlinker', '/libjack.so.0', '-lpulse']}]}])
        self.assertFalse(any(item['code'] in {'build.x11', 'build.pipewire'} for item in self.audit()['findings']))

    def test_malformed_nested_target_records_do_not_pass(self):
        for sources in ({}, [42], [{'linker': ['cc'], 'parameters': 'not a list'}]):
            with self.subTest(sources=sources):
                self.write('intro-targets.json', [{'name': 'invalid', 'target_sources': sources}])
                with self.assertRaises(ValueError):
                    self.audit()
