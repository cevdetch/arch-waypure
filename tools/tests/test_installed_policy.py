"""Installed evidence must follow actual providers, never sync alternatives."""

from pathlib import Path
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from optionkit.alpm import Package
from optionkit.runtime_policy import load_policy


class InstalledPolicyCase(unittest.TestCase):
    def paths(self, packages, dependencies):
        from optionkit.installed_policy import InstalledGraph
        graph = InstalledGraph({package.name: package for package in packages})
        return graph.paths('demo', dependencies, load_policy(TOOLS / 'waypure-policy.toml'))

    def test_installed_replacement_uses_its_runtime_dependencies_and_versioned_provides(self):
        packages = [Package('gtk-waypure', '1:4.0-1', 'gtk', ('wayland',), ('gtk4=1:4.0',)),
                    Package('wayland', '1.0-1')]
        paths, uncertain = self.paths(packages, ['gtk4>=1:4.0'])
        self.assertEqual(paths, [])
        self.assertEqual(uncertain, [])

    def test_installed_transitive_jack_provision_is_identified(self):
        packages = [Package('middle-waypure', '2-1', '', ('audio-api>=2',), ('middle=2',)),
                    Package('wrapper', '3-1', '', (), ('audio-api=2', 'jack=1.9'))]
        paths, uncertain = self.paths(packages, ['middle>=2'])
        self.assertEqual(uncertain, [])
        self.assertTrue(any(severity == 'error' and 'PIPEWIRE' in path
                            and 'middle-waypure@2-1 -> wrapper@3-1' in path and 'jack=1.9' in path
                            for path, severity in paths), paths)

    def test_installed_concrete_provider_wins_over_virtual_alternative(self):
        packages = [Package('display', '2'), Package('other', '2', '', ('libx11',), ('display=2',))]
        self.assertEqual(self.paths(packages, ['display>=2']), ([], []))

    def test_ambiguous_installed_providers_are_conditional_and_missing_are_reported(self):
        packages = [Package('safe', '2', '', (), ('display=2',)),
                    Package('unsafe', '2', '', ('libx11',), ('display=2',)),
                    Package('libx11', '1')]
        paths, uncertain = self.paths(packages, ['display>=2', 'absent'])
        self.assertTrue(paths, uncertain)
        self.assertTrue(all(severity == 'warning' for _, severity in paths), paths)
        self.assertTrue(any('Multiple' in value and 'display>=2' in value for value in uncertain), uncertain)
        self.assertTrue(any('absent' in value for value in uncertain), uncertain)

    def test_unversioned_and_incompatible_provides_do_not_satisfy_versioned_requests(self):
        packages = [Package('wrapper', '9', '', ('libx11',), ('display', 'audio-api=1'))]
        paths, uncertain = self.paths(packages, ['display>=2', 'audio-api>=2'])
        self.assertEqual(paths, [])
        self.assertEqual(len(uncertain), 2)

    def test_cycles_terminate_and_source_base_is_not_a_capability(self):
        packages = [Package('libpulse', '1', 'pulseaudio', ('other',)),
                    Package('other', '1', '', ('libpulse',))]
        self.assertEqual(self.paths(packages, ['libpulse']), ([], []))
