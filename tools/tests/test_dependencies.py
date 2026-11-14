"""Dependency syntax and ALPM-backed version predicates for local recipes."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DependencyCase(unittest.TestCase):
    def test_dependency_syntax_preserves_epochs_and_soname_names(self):
        from optionkit.dependencies import split_dependency
        for text, expected in (
            ('cairo', ('cairo', None, None)),
            (' cairo>=1:1.18.6-1 ', ('cairo', '>=', '1:1.18.6-1')),
            ('lib:libfoo.so=2-64', ('lib:libfoo.so', '=', '2-64')),
            ('foo<2', ('foo', '<', '2')),
        ):
            with self.subTest(text=text):
                self.assertEqual(split_dependency(text), expected)

    def test_invalid_dependencies_cannot_silently_lose_their_constraint(self):
        from optionkit.dependencies import split_dependency
        for text in ('', 'foo>=', 'foo=>1', 'foo==1', 'foo>1<2', 'foo bar', '=1'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                split_dependency(text)

    def test_version_predicates_use_the_alpm_comparison_result(self):
        from optionkit.dependencies import version_satisfies
        expected = {
            -1: {'=': False, '>': False, '>=': False, '<': True, '<=': True},
            0: {'=': True, '>': False, '>=': True, '<': False, '<=': True},
            1: {'=': False, '>': True, '>=': True, '<': False, '<=': False},
        }
        for comparison, operators in expected.items():
            with patch('optionkit.dependencies.compare_versions', return_value=comparison):
                for operator, wanted in operators.items():
                    with self.subTest(comparison=comparison, operator=operator):
                        self.assertEqual(version_satisfies('available', operator, 'requested'), wanted)

    def test_unversioned_provision_cannot_satisfy_a_versioned_dependency(self):
        from optionkit.dependencies import version_satisfies
        with patch('optionkit.dependencies.compare_versions', side_effect=AssertionError('unexpected comparison')):
            self.assertTrue(version_satisfies(None, None, None))
            self.assertFalse(version_satisfies(None, '>=', '1.0'))
            self.assertTrue(version_satisfies('1.0', '=', '1.0'))


if __name__ == '__main__':
    unittest.main()
