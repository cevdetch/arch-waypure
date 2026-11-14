"""Builder graph coverage migrated from ~/.local/share/waypure-build/tests."""

import shutil
import unittest
from builder_test_support import BuilderCase
from pathlib import Path
from unittest.mock import patch

def _info(wb, dirname, Deps=(), provides=(), names=None, version='1-1'):
    d = Path('/repo') / dirname
    return wb.PackageInfo(dir=d, pkgnames=list(names or [dirname]), version=version, depends=list(Deps), makedepends=[], checkdepends=[], provides=list(provides))

class BuilderGraphCase(BuilderCase):

    def setUp(self):
        super().setUp()
        # Graph selection uses a controlled ALPM boundary; native version
        # semantics are checked against pyalpm in test_alpm.py.
        order={'1-1':0,'1.0':1,'1.0-1':1,'1.18':2,'2.0':3,'2.0-1':3,'3.0':4}
        self.enterContext(patch('optionkit.dependencies.compare_versions',
                               side_effect=lambda a,b:(order[a]>order[b])-(order[a]<order[b])))

    def test_bare_name_strips_constraints(self):
        wb = self.wb
        self.assertEqual(wb.bare_name('foo>=1.2'), 'foo')
        self.assertEqual(wb.bare_name('libbar=1.0-1'), 'libbar')
        self.assertEqual(wb.bare_name('baz'), 'baz')

    def test_versioned_dep_and_provides_still_map(self):
        wb = self.wb
        cairo = _info(wb, 'cairo', provides=['cairo=1.18'])
        pango = _info(wb, 'pango', Deps=['cairo>=1.0'])
        mapping = wb.build_provides_map([pango, cairo])
        self.assertEqual(wb.local_dep_dirs(pango, mapping), {Path('/repo/cairo')})

    def test_incompatible_local_version_cannot_silently_become_external(self):
        library=_info(self.wb,'library',version='1.0-1')
        app=_info(self.wb,'app',Deps=['library>=2.0'])
        with self.assertRaisesRegex(self.wb.DependencyError,'app.*library>=2.0'):
            self.wb.topo_tiers([app,library])

    def test_unversioned_alias_does_not_inherit_the_recipe_version(self):
        library=_info(self.wb,'library',provides=['virtual'],version='2.0-1')
        app=_info(self.wb,'app',Deps=['virtual>=2.0'])
        with self.assertRaisesRegex(self.wb.DependencyError,'virtual>=2.0'):
            self.wb.topo_tiers([app,library])

    def test_only_compatible_virtual_provider_creates_an_edge(self):
        older=_info(self.wb,'a-old',provides=['virtual=1.0'])
        newer=_info(self.wb,'b-new',provides=['virtual=2.0'])
        app=_info(self.wb,'app',Deps=['virtual>=2.0'])
        graph=self.wb.dependency_graph([app,older,newer])
        self.assertEqual(graph[app.dir],{newer.dir})

    def test_ambiguous_virtual_providers_require_explicit_selection(self):
        first=_info(self.wb,'a',provides=['virtual=2.0'])
        second=_info(self.wb,'b',provides=['virtual=2.0'])
        app=_info(self.wb,'app',Deps=['virtual'])
        with self.assertRaisesRegex(self.wb.DependencyError,'app.*virtual.*a.*b.*--ignore'):
            self.wb.topo_tiers([app,first,second])

    def test_compatible_concrete_output_wins_over_virtual_providers(self):
        concrete=_info(self.wb,'concrete',names=['virtual'],version='2.0-1')
        alias=_info(self.wb,'alias',provides=['virtual=2.0'])
        app=_info(self.wb,'app',Deps=['virtual>=2.0'])
        graph=self.wb.dependency_graph([app,alias,concrete])
        self.assertEqual(graph[app.dir],{concrete.dir})

    def test_same_recipe_provisions_coalesce_and_split_self_dependencies_have_no_edge(self):
        library=_info(self.wb,'library',names=['lib','lib-utils'],provides=['virtual=1.0','virtual=2.0'],
                      Deps=['lib=2.0-1'],version='2.0-1')
        app=_info(self.wb,'app',Deps=['virtual>=1.0'])
        graph=self.wb.dependency_graph([app,library])
        self.assertEqual(graph,{app.dir:{library.dir},library.dir:set()})

    def test_duplicate_concrete_outputs_fail_even_without_consumers(self):
        first=_info(self.wb,'a',names=['same'],version='1.0-1')
        second=_info(self.wb,'b',names=['same'],version='2.0-1')
        with self.assertRaisesRegex(self.wb.DependencyError,'same.*a.*b.*--ignore'):
            self.wb.topo_tiers([first,second])

    def test_invalid_provider_constraint_is_not_treated_as_an_alias(self):
        library=_info(self.wb,'library',provides=['virtual>=2.0'])
        with self.assertRaisesRegex(self.wb.DependencyError,'library.*virtual>=2.0'):
            self.wb.topo_tiers([library])

    def test_tiers_leaf_first(self):
        wb = self.wb
        glvnd = _info(wb, 'libglvnd')
        epoxy = _info(wb, 'libepoxy', Deps=['libglvnd'])
        gtk = _info(wb, 'gtk', Deps=['libepoxy'])
        tiers = wb.topo_tiers([gtk, epoxy, glvnd])
        self.assertEqual([[i.dir.name for i in t] for t in tiers], [['libglvnd'], ['libepoxy'], ['gtk']])

    def test_optdepends_ignored_and_externals_dropped(self):
        wb = self.wb
        a = _info(wb, 'a', Deps=['glibc'])
        tiers = wb.topo_tiers([a])
        self.assertEqual([[i.dir.name for i in t] for t in tiers], [['a']])

    def test_cycle_members_share_one_tier(self):
        wb = self.wb
        a = _info(wb, 'a', Deps=['b'])
        b = _info(wb, 'b', Deps=['a'])
        c = _info(wb, 'c', Deps=['a'])
        tiers = wb.topo_tiers([c, b, a])
        self.assertEqual([[i.dir.name for i in t] for t in tiers], [['a', 'b'], ['c']])

    def test_cycle_members_warn_on_stderr(self):
        wb = self.wb
        capsys = self.capsys
        a = _info(wb, 'a', Deps=['b'])
        b = _info(wb, 'b', Deps=['a'])
        tiers = wb.topo_tiers([a, b])
        self.assertEqual([[i.dir.name for i in t] for t in tiers], [['a', 'b']])
        err = capsys.readouterr().err
        self.assertTrue('a' in err and 'b' in err)

    def test_makedeps_and_checkdeps_create_edges(self):
        wb = self.wb
        base = _info(wb, 'base')
        mid = _info(wb, 'mid', Deps=[])
        mid.makedepends = ['base']
        top = _info(wb, 'top', Deps=[])
        top.checkdepends = ['mid']
        tiers = wb.topo_tiers([top, mid, base])
        self.assertEqual([[i.dir.name for i in t] for t in tiers], [['base'], ['mid'], ['top']])
