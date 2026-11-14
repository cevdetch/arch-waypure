"""Read-only ALPM integration against temporary native databases and CLI tools."""

import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOLS))
NATIVE = importlib.util.find_spec('pyalpm') is not None


def database_record(name,version,base=None):
    return ('%NAME%\n'+name+'\n\n%VERSION%\n'+version+'\n\n%BASE%\n'+(base or name)
            +'\n\n%ARCH%\nx86_64\n\n%DESC%\nSynthetic fixture\n\n').encode()


class AlpmCase(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def module(self):
        self.assertTrue((TOOLS/'optionkit/alpm.py').is_file(),'missing shared ALPM backend')
        module=importlib.import_module('optionkit.alpm')
        module.reset_backend()
        self.addCleanup(module.reset_backend)
        return module

    def native_config(self, *, default_database=False, glob_include=False):
        root=self.root/'installation root'
        root.mkdir()
        database=root/'var/lib/pacman' if default_database else self.root/'package database'
        (database/'local').mkdir(parents=True)
        (database/'sync').mkdir()
        (database/'local/ALPM_DB_VERSION').write_text('9\n')
        installed=database/'local/demo-waypure-1:1.0-2'
        installed.mkdir()
        (installed/'desc').write_bytes(database_record('demo-waypure','1:1.0-2','demo'))
        for repo,version in [('custom','9.0-1'),('extra','2:1.0-1')]:
            with tarfile.open(database/'sync'/(repo+'.db'),'w:gz') as archive:
                data=database_record('demo',version)
                entry=tarfile.TarInfo('demo-'+version+'/desc')
                entry.size=len(data)
                archive.addfile(entry,io.BytesIO(data))
        if glob_include:
            includes=self.root/'repositories'
            includes.mkdir()
            (includes/'20-extra.conf').write_text('[extra]\nServer = file:///unused/extra\n')
            (includes/'10-custom.conf').write_text('[custom]\nServer = file:///unused/custom\n')
            include=includes/'*.conf'
        else:
            include=self.root/'repositories.conf'
            include.write_text('[custom]\nServer = file:///unused/custom\n[extra]\nServer = file:///unused/extra\n')
        config=self.root/'pacman.conf'
        config.write_text('[options]\nRootDir = '+str(root)+('' if default_database else '\nDBPath = '+str(database))
                          +'\nArchitecture = x86_64\nSigLevel = Never\nInclude = '+str(include)+'\n')
        return config,database

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_native_reads_configured_databases_and_preserves_repo_priority(self):
        module=self.module()
        config,database=self.native_config()
        backend=module.get_backend(mode='native',config=config)
        self.assertEqual(backend.name,'native')
        self.assertEqual({name:package.version for name,package in backend.installed().items()},{'demo-waypure':'1:1.0-2'})
        self.assertEqual(backend.sync_versions()['demo'],'9.0-1')
        self.assertEqual(backend.sync_versions(repositories=('extra',))['demo'],'2:1.0-1')
        self.assertFalse((database/'db.lck').exists())

    @unittest.skipUnless(NATIVE and shutil.which('pacman'),'pyalpm and pacman needed for configuration parity')
    def test_rootdir_only_uses_the_root_database_in_both_backends(self):
        module=self.module()
        config,database=self.native_config(default_database=True)
        for mode in ('native','cli'):
            with self.subTest(mode=mode):
                backend=module.get_backend(mode=mode,config=config)
                self.assertEqual({name:package.version for name,package in backend.installed().items()},
                                 {'demo-waypure':'1:1.0-2'})
        self.assertFalse((database/'db.lck').exists())

    @unittest.skipUnless(NATIVE and shutil.which('pacman'),'pyalpm and pacman needed for configuration parity')
    def test_include_globs_preserve_pacman_repository_priority(self):
        module=self.module()
        config,database=self.native_config(glob_include=True)
        for mode in ('native','cli'):
            with self.subTest(mode=mode):
                self.assertEqual(module.get_backend(mode=mode,config=config).sync_versions()['demo'],'9.0-1')

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_native_refresh_discards_stale_installed_snapshot(self):
        module=self.module()
        config,database=self.native_config()
        first=module.get_backend(mode='native',config=config)
        self.assertNotIn('new-package',first.installed())
        added=database/'local/new-package-1.0-1'
        added.mkdir()
        (added/'desc').write_bytes(database_record('new-package','1.0-1'))
        self.assertNotIn('new-package',first.installed())
        module.reset_backend()
        self.assertIn('new-package',module.get_backend(mode='native',config=config).installed())

    @unittest.skipUnless(NATIVE, 'system pyalpm needed')
    def test_native_runtime_graph_is_cached_and_includes_real_dependencies_and_provides(self):
        module = self.module()
        config, database = self.native_config()
        desc = database / 'local/demo-waypure-1:1.0-2/desc'
        desc.write_bytes(desc.read_bytes() + b'%DEPENDS%\nwayland>=1\n\n%PROVIDES%\ndemo=1:1.0\n\n')
        backend = module.get_backend(mode='native', config=config)
        with self.configuration_queries_only():
            first = backend.installed_runtime()
            self.assertEqual(first['demo-waypure'].depends, ('wayland>=1',))
            self.assertEqual(first['demo-waypure'].provides, ('demo=1:1.0',))
            self.assertIs(backend.installed_runtime(), first)
        self.assertFalse((database / 'db.lck').exists())

    def test_cli_runtime_graph_reports_native_requirement_without_per_package_queries(self):
        module, backend, trace = self.cli()
        with self.assertRaisesRegex(module.BackendUnavailable, 'pyalpm'):
            backend.installed_runtime()
        self.assertFalse(trace.exists())

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_native_version_comparison_does_not_need_a_database(self):
        module=self.module()
        backend=module.get_backend(mode='native',config=self.root/'missing.conf')
        self.assertGreater(backend.compare('1:1.0-1','9.0-9'),0)
        self.assertLess(backend.compare('1.0rc1-1','1.0-1'),0)

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_native_reads_archive_metadata_without_installing(self):
        module=self.module()
        config,database=self.native_config()
        path=self.root/'demo-waypure-1.0-1-any.pkg.tar'
        data=b'pkgname = demo-waypure\npkgbase = demo\npkgver = 1:1.0-2\npkgdesc = Synthetic archive\nurl = https://example.invalid\narch = any\nsize = 0\nbuilddate = 1700000000\nlicense = MIT\n'
        with tarfile.open(path,'w') as archive:
            entry=tarfile.TarInfo('.PKGINFO')
            entry.size=len(data)
            archive.addfile(entry,io.BytesIO(data))
        package=module.get_backend(mode='native',config=config).archive(path)
        self.assertEqual((package.name,package.version,package.base),('demo-waypure','1:1.0-2','demo'))
        self.assertFalse((database/'local/demo-waypure-1.0-1').exists())

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_broken_native_configuration_does_not_fall_back_to_host_cli(self):
        module=self.module()
        with self.assertRaises(module.BackendError):
            module.get_backend(mode='auto',config=self.root/'missing.conf').installed()

    def cli(self):
        module=self.module()
        trace=self.root/'calls.json.log'
        for command,body in {
            'pacman': "args=sys.argv[1:]\nif os.environ.get('FAIL_QUERY'): sys.exit(3)\nif args==['-Q']: print('demo-waypure 1:1.0-2')\nelif args==['-Sl']: print('custom demo 9.0-1\\nextra demo 2:1.0-1')\nelif args[:1]==['-Qp']: print('demo-waypure 1:1.0-2')\nelse: sys.exit(99)\n",
            'vercmp': "print(-1 if sys.argv[1]=='older' else 1)\n",
        }.items():
            path=self.root/command
            path.write_text('#!'+sys.executable+'\nimport json,os,sys\nwith open('+repr(str(trace))+",'a') as output: output.write(json.dumps(["+repr(command)+"]+sys.argv[1:])+'\\n')\n"+body)
            path.chmod(0o755)
        self.enterContext(patch.dict(os.environ,{'PATH':str(self.root),'WAYPURE_ALPM_BACKEND':'cli'}))
        return module,module.get_backend(),trace

    def test_cli_installed_and_sync_queries_are_batched_and_cached(self):
        module,backend,trace=self.cli()
        for _ in range(3):
            self.assertEqual(backend.installed()['demo-waypure'].version,'1:1.0-2')
            self.assertEqual(backend.sync_versions()['demo'],'9.0-1')
            self.assertEqual(backend.sync_versions(repositories=('extra',))['demo'],'2:1.0-1')
        self.assertEqual([json.loads(line) for line in trace.read_text().splitlines()],[['pacman','-Q'],['pacman','-Sl']])

    def test_cli_version_comparison_is_cached(self):
        module,backend,trace=self.cli()
        self.assertLess(backend.compare('older','newer'),0)
        self.assertLess(backend.compare('older','newer'),0)
        self.assertEqual(len(trace.read_text().splitlines()),1)

    def test_cli_query_failure_cannot_become_an_empty_snapshot(self):
        module,backend,trace=self.cli()
        with patch.dict(os.environ,{'FAIL_QUERY':'1'}):
            with self.assertRaises(module.BackendError):
                backend.installed()

    def test_explicit_native_mode_reports_a_missing_binding(self):
        module=self.module()
        with patch.dict(sys.modules,{'pyalpm':None}):
            with self.assertRaises(module.BackendError):
                module.get_backend(mode='native')

    def test_explicit_native_mode_cannot_use_natural_version_fallback(self):
        module=self.module()
        with patch.dict(sys.modules,{'pyalpm':None}), patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native'}):
            with self.assertRaises(module.BackendError):
                module.compare_versions('1.0','2.0',natural=True)

    def test_auto_without_version_tools_retains_natural_fallback(self):
        module=self.module()
        with patch.dict(sys.modules,{'pyalpm':None}), patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'auto'}):
            with patch.object(shutil,'which',return_value=None):
                self.assertLess(module.compare_versions('1.0','2.0',natural=True),0)

    def test_public_audit_reports_explicit_native_missing_binding(self):
        self.module()
        from optionkit import repository
        recipe=self.root/'demo'
        recipe.mkdir()
        (recipe/'PKGBUILD').write_text('pkgname=demo-waypure\npkgver=1.0\npkgrel=1\nprovides=(demo)\nconflicts=(demo)\n')
        (recipe/'.SRCINFO').write_text('pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\npkgname = demo-waypure\n\tprovides = demo\n\tconflicts = demo\n')
        error=io.StringIO()
        with patch.dict(sys.modules,{'pyalpm':None}), patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native'}):
            with patch.object(sys,'argv',['waypure-audit','--repo',str(self.root),'--metadata-only']), patch.object(sys,'stderr',error):
                with self.assertRaises(SystemExit) as failure:
                    repository.main()
        self.assertEqual(failure.exception.code,2)
        self.assertIn('pyalpm is unavailable',error.getvalue())

    def test_auto_selects_cli_only_when_native_binding_is_unavailable(self):
        module=self.module()
        with patch.dict(sys.modules,{'pyalpm':None}):
            self.assertEqual(module.get_backend(mode='auto').name,'cli')

    def test_invalid_backend_choice_is_reported(self):
        module=self.module()
        with self.assertRaises(module.BackendError):
            module.get_backend(mode='typo')

    def test_native_filtered_query_does_not_read_unselected_databases(self):
        module=self.module()
        class UnrelatedDatabase:
            name='custom'
            @property
            def pkgcache(self):
                raise AssertionError('unrelated repository was read')
        selected=SimpleNamespace(name='extra',pkgcache=[SimpleNamespace(name='demo',version='2:1.0-1')])
        backend=module.NativeBackend(SimpleNamespace(error=RuntimeError),None)
        backend.handle=SimpleNamespace(get_syncdbs=lambda:[UnrelatedDatabase(),selected])
        self.assertEqual(backend.sync_versions(repositories=('extra',)),{'demo':'2:1.0-1'})

    @unittest.skipUnless(NATIVE and shutil.which('pacman'), 'native and CLI query parity needs pyalpm and pacman')
    def test_named_queries_preserve_priority_and_omit_missing_packages(self):
        module=self.module()
        config,database=self.native_config()
        for mode in ('native','cli'):
            with self.subTest(mode=mode):
                backend=module.get_backend(mode=mode,config=config)
                installed=backend.installed(names=iter(('missing','demo-waypure','demo-waypure')))
                self.assertEqual({name:package.version for name,package in installed.items()},
                                 {'demo-waypure':'1:1.0-2'})
                self.assertEqual(backend.sync_versions(names=iter(('demo','missing','demo'))),
                                 {'demo':'9.0-1'})
                self.assertEqual(backend.sync_versions(repositories=('extra',),names=('demo','missing')),
                                 {'demo':'2:1.0-1'})
                self.assertEqual(backend.sync_versions(repositories=('absent',),names=('demo',)),{})
        self.assertFalse((database/'db.lck').exists())

    def test_empty_name_selection_does_not_query_any_packages(self):
        module=self.module()
        native=module.NativeBackend(SimpleNamespace(error=RuntimeError),self.root/'missing.conf')
        cli=module.CliBackend(self.root/'missing.conf')
        with patch.object(subprocess,'run',side_effect=AssertionError('empty selection queried a database')):
            for backend in (native,cli):
                with self.subTest(mode=backend.name):
                    self.assertEqual(backend.installed(names=iter(())),{})
                    self.assertEqual(backend.sync_versions(names=iter(())),{})

    def test_filtered_cli_queries_reuse_the_same_bulk_snapshot(self):
        module,backend,trace=self.cli()
        for _ in range(3):
            self.assertEqual(set(backend.installed(names=('demo-waypure','missing'))),{'demo-waypure'})
            self.assertEqual(backend.sync_versions(names=('missing','demo')),{'demo':'9.0-1'})
            self.assertEqual(backend.sync_versions(repositories=('extra',),names=('demo',)),{'demo':'2:1.0-1'})
        self.assertEqual([json.loads(line) for line in trace.read_text().splitlines()],
                         [['pacman','-Q'],['pacman','-Sl']])

    def test_installed_names_does_not_read_unused_native_metadata(self):
        module=self.module()
        class NameOnlyPackage:
            name='demo-waypure'
            @property
            def version(self):
                raise AssertionError('name-only query read unused version metadata')
            @property
            def base(self):
                raise AssertionError('name-only query read unused base metadata')
        backend=module.NativeBackend(SimpleNamespace(error=RuntimeError),None)
        backend.handle=SimpleNamespace(get_localdb=lambda:SimpleNamespace(pkgcache=[NameOnlyPackage()]))
        self.assertEqual(set(backend.installed_names()),{'demo-waypure'})
        output=io.StringIO()
        with patch.object(module,'get_backend',return_value=backend), patch.object(sys,'stdout',output):
            with patch.object(sys,'argv',['waypure-alpm','installed-names']):
                module.main()
        self.assertEqual(output.getvalue(),'demo-waypure\n')

    def test_cli_installed_names_reuses_the_installed_snapshot(self):
        module,backend,trace=self.cli()
        for _ in range(3):
            self.assertEqual(set(backend.installed_names()),{'demo-waypure'})
            self.assertEqual(backend.installed(names=('demo-waypure',))['demo-waypure'].version,'1:1.0-2')
        self.assertEqual([json.loads(line) for line in trace.read_text().splitlines()],[['pacman','-Q']])

    @unittest.skipUnless(NATIVE, 'indexed query coverage needs pyalpm')
    def test_named_native_queries_avoid_enumeration_and_resolved_later_databases(self):
        module=self.module()
        config,database=self.native_config()
        backend=module.get_backend(mode='native',config=config)
        handle=backend.handle
        class IndexedOnly:
            def __init__(self, database):
                self.database=database
                self.name=database.name
            @property
            def pkgcache(self):
                raise AssertionError('named query enumerated the database')
            def get_pkg(self, name):
                return self.database.get_pkg(name)
        class UnneededDatabase:
            name='later'
            def get_pkg(self, name):
                raise AssertionError('query read a later repository after resolving every name')
        backend.handle=SimpleNamespace(
            get_localdb=lambda:IndexedOnly(handle.get_localdb()),
            get_syncdbs=lambda:[*(IndexedOnly(db) for db in handle.get_syncdbs()),UnneededDatabase()])
        self.assertEqual(backend.installed(names=('demo-waypure',))['demo-waypure'].version,'1:1.0-2')
        self.assertEqual(backend.sync_versions(names=('demo',)),{'demo':'9.0-1'})

    @unittest.skipUnless(NATIVE, 'indexed snapshot coverage needs pyalpm')
    def test_reset_discards_named_native_installed_snapshot(self):
        module=self.module()
        config,database=self.native_config()
        backend=module.get_backend(mode='native',config=config)
        self.assertEqual(backend.installed(names=('new-package',)),{})
        added=database/'local/new-package-1.0-1'
        added.mkdir()
        (added/'desc').write_bytes(database_record('new-package','1.0-1'))
        self.assertEqual(backend.installed(names=('new-package',)),{})
        module.reset_backend()
        self.assertEqual(module.get_backend(mode='native',config=config)
                         .installed(names=('new-package',))['new-package'].version,'1.0-1')

    @unittest.skipUnless(shutil.which('pacman'),'pacman needed for CLI configuration parity')
    def test_cli_reads_the_same_alternate_configuration_with_spaces(self):
        module=self.module()
        config,database=self.native_config()
        backend=module.get_backend(mode='cli',config=config)
        self.assertEqual({name:package.version for name,package in backend.installed().items()},{'demo-waypure':'1:1.0-2'})
        self.assertEqual(backend.sync_versions(repositories=('extra',)),{'demo':'2:1.0-1'})
        self.assertFalse((database/'db.lck').exists())

    def configuration_queries_only(self):
        run=subprocess.run
        def guarded(command,**kwargs):
            self.assertEqual(command[0],'pacman-conf','unexpected package query process')
            return run(command,**kwargs)
        return patch.object(subprocess,'run',side_effect=guarded)

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_builder_freshness_uses_native_snapshot_without_package_processes(self):
        module=self.module()
        config,database=self.native_config()
        from builder_test_support import builder as wb
        original=module.NativeBackend.installed
        def named_only(backend, names=None):
            self.assertEqual(set(names or ()),{'demo-waypure'},'freshness requested unrelated installed packages')
            return original(backend,names=names)
        with patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native','WAYPURE_PACMAN_CONFIG':str(config)}):
            with self.configuration_queries_only(), patch.object(module.NativeBackend,'installed',named_only):
                info=wb.PackageInfo(dir=self.root,pkgnames=['demo-waypure'],version='1:1.0-2')
                self.assertIn('no built artifact',wb.dirty_reason(info))

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_audit_sync_reads_native_official_repositories(self):
        module=self.module()
        config,database=self.native_config()
        from optionkit.repository import sync_versions
        with patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native','WAYPURE_PACMAN_CONFIG':str(config)}):
            with self.configuration_queries_only():
                self.assertEqual(sync_versions(),{'demo':'2:1.0-1'})

    @unittest.skipUnless(NATIVE, 'system pyalpm needed')
    def test_audit_installed_traversal_is_opt_in_and_reports_actual_provider_evidence(self):
        module = self.module()
        config, database = self.native_config()
        recipe = self.recipe('demo')
        metadata = recipe / '.SRCINFO'
        metadata.write_text(metadata.read_text() + '\tdepends = neutral-api>=2\n')
        installed = database / 'local/wrapper-2-1'
        installed.mkdir()
        (installed / 'desc').write_bytes(database_record('wrapper', '2-1')
                                        + b'%PROVIDES%\nneutral-api=2\njack=1.9\n\n')
        from optionkit import repository
        for enabled in (False, True):
            output = io.StringIO()
            args = ['waypure-audit', '--repo', str(recipe.parent), '--metadata-only', '--json']
            if enabled:
                args.append('--installed-deps')
            with patch.dict(os.environ, {'WAYPURE_ALPM_BACKEND': 'native', 'WAYPURE_PACMAN_CONFIG': str(config)}):
                with self.configuration_queries_only(), patch.object(sys, 'argv', args), patch.object(sys, 'stdout', output):
                    result = repository.main()
            row = json.loads(output.getvalue())['packages'][0]
            self.assertEqual(row['checks']['recipe'], 'PASS', row)
            findings = [item for item in row['findings'] if item['code'] == 'installed.path']
            self.assertEqual(bool(findings), enabled, row)
            self.assertEqual(result, 1 if enabled else 0)
            if enabled:
                self.assertTrue(any('wrapper@2-1' in item['message'] and 'jack=1.9' in item['message']
                                    for item in findings), findings)

    def recipe(self, name):
        directory=self.root/'recipes'/name
        directory.mkdir(parents=True)
        (directory/'PKGBUILD').write_text('pkgname=demo-waypure\npkgver=1.0\npkgrel=1\nepoch=2\nprovides=(demo)\nconflicts=(demo)\n')
        (directory/'.SRCINFO').write_text('pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n\tepoch = 2\npkgname = demo-waypure\n\tprovides = demo\n\tconflicts = demo\n')
        return directory

    @unittest.skipUnless(NATIVE, 'indexed audit coverage needs pyalpm')
    def test_audit_only_queries_selected_configured_official_names(self):
        module=self.module()
        config,database=self.native_config()
        selected=self.recipe('renamed')
        (selected/'build-options.toml').write_text('[package]\narch = "demo"\n')
        self.recipe('unselected')
        from optionkit import repository
        original=module.NativeBackend.sync_versions
        def named_only(backend, repositories=None, names=None):
            self.assertEqual(set(names or ()),{'demo'},'audit requested unrelated official packages')
            return original(backend,repositories=repositories,names=names)
        output=io.StringIO()
        with patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native','WAYPURE_PACMAN_CONFIG':str(config)}):
            with patch.object(module.NativeBackend,'sync_versions',named_only), patch.object(sys,'stdout',output):
                with patch.object(sys,'argv',['waypure-audit','--repo',str(selected.parent),'--metadata-only','--json','renamed']):
                    self.assertEqual(repository.main(),0)
        rows=json.loads(output.getvalue())['packages']
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['package'],rows[0]['arch_version'],rows[0]['checks']['arch']),
                         ('renamed','2:1.0-1','CACHED'))

    @unittest.skipUnless(NATIVE, 'indexed audit error coverage needs pyalpm')
    def test_invalid_audit_config_remains_a_package_finding(self):
        self.module()
        config,database=self.native_config()
        recipe=self.recipe('demo')
        (recipe/'build-options.toml').write_text('[package]\narch = 123\n')
        from optionkit import repository
        output=io.StringIO()
        with patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native','WAYPURE_PACMAN_CONFIG':str(config)}):
            with patch.object(sys,'stdout',output):
                with patch.object(sys,'argv',['waypure-audit','--repo',str(recipe.parent),'--metadata-only','--json']):
                    self.assertEqual(repository.main(),1)
        row=json.loads(output.getvalue())['packages'][0]
        self.assertTrue(any(item['code']=='review.unavailable' and 'package.arch' in item['message']
                            for item in row['findings']))

    @unittest.skipUnless(NATIVE, 'indexed versions coverage needs pyalpm')
    def test_versions_only_queries_names_from_installed_and_archived_packages(self):
        module=self.module()
        config,database=self.native_config()
        recipe=self.recipe('demo')
        self.package_archive(recipe,'2:0.5-1')
        self.package_archive(recipe,'1.0-1',name='archive-only-waypure')
        unrelated=database/'local/unrelated-1.0-1'
        unrelated.mkdir()
        (unrelated/'desc').write_bytes(database_record('unrelated','1.0-1'))
        installed_only=database/'local/installed-only-waypure-1.0-1'
        installed_only.mkdir()
        (installed_only/'desc').write_bytes(database_record('installed-only-waypure','1.0-1'))
        from optionkit import versions
        original=module.NativeBackend.sync_versions
        installed=module.NativeBackend.installed
        def named_only(backend, repositories=None, names=None):
            self.assertEqual(set(names or ()),{'demo','archive-only','installed-only'},
                             'version check did not select the union of installed and archived Waypure packages')
            return original(backend,repositories=repositories,names=names)
        def installed_waypure_only(backend, names=None):
            names=None if names is None else tuple(names)
            self.assertEqual(set(names or ()),{'demo-waypure','installed-only-waypure'},
                             'version check read unrelated installed metadata')
            return installed(backend,names=names)
        output=io.StringIO()
        with patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native','WAYPURE_PACMAN_CONFIG':str(config)}):
            with patch.object(module.NativeBackend,'sync_versions',named_only), patch.object(sys,'stdout',output), \
                    patch.object(module.NativeBackend,'installed',installed_waypure_only):
                with patch.object(sys,'argv',['waypure-versions','--repo',str(recipe.parent),'--color=never']):
                    self.assertEqual(versions.main(),0)
        row=next(line.split() for line in output.getvalue().splitlines() if line.startswith('demo '))
        self.assertEqual(row,['demo','2:0.5','2:1.0','both','DIFF'])

    def native_command(self,name,*arguments,config):
        binaries=self.root/'guarded bin'
        binaries.mkdir(exist_ok=True)
        pacman=binaries/'pacman'
        pacman.write_text('#!'+sys.executable+'\nimport sys\nprint("unexpected pacman query",file=sys.stderr)\nsys.exit(97)\n')
        pacman.chmod(0o755)
        environment={**os.environ,'WAYPURE_ALPM_BACKEND':'native','WAYPURE_PACMAN_CONFIG':str(config),
                     'WAYPURE_DIR':str(self.root/'recipes'),'PATH':str(binaries)+os.pathsep+os.environ['PATH'],
                     'PYTHONDONTWRITEBYTECODE':'1'}
        return subprocess.run([str(TOOLS/name),*arguments],env=environment,text=True,capture_output=True,timeout=10)

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_versions_command_uses_configured_native_state_and_official_scope(self):
        config,database=self.native_config()
        result=self.native_command('waypure-versions',config=config)
        self.assertEqual(result.returncode,0,result.stderr)
        row=next((line.split() for line in result.stdout.splitlines() if line.startswith('demo ')),None)
        self.assertEqual(row,['demo','1:1.0','2:1.0','installed','DIFF'])

    def package_archive(self,directory,version,name='demo-waypure',provides=()):
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(name+'-'+version+'-any.pkg.tar')
        data=('pkgname = '+name+'\npkgbase = demo\npkgver = '+version+'\npkgdesc = Synthetic archive\nurl = https://example.invalid\narch = any\nsize = 0\nbuilddate = 1700000000\n'
              +''.join('provides = '+value+'\n' for value in provides)).encode()
        with tarfile.open(path,'w') as archive:
            entry=tarfile.TarInfo('.PKGINFO')
            entry.size=len(data)
            archive.addfile(entry,io.BytesIO(data))
        return path

    @unittest.skipUnless(NATIVE,'dependency conformance needs pyalpm')
    def test_dependency_predicates_agree_with_native_satisfier(self):
        module=self.module()
        config,database=self.native_config()
        backend=module.get_backend(mode='native',config=config)
        archive=self.package_archive(self.root/'archives','1:1.10-2',provides=('virtual=2:0.5-1','plain'))
        package=backend.handle.load_pkg(str(archive),0)
        from optionkit.dependencies import version_satisfies
        cases=(
            ('demo-waypure>=1:1.9','1:1.10-2','>=','1:1.9',True),
            ('demo-waypure=1:1.10','1:1.10-2','=','1:1.10',True),
            ('demo-waypure>1:1.10','1:1.10-2','>','1:1.10',False),
            ('demo-waypure<1:2.0','1:1.10-2','<','1:2.0',True),
            ('demo-waypure<=1:1.9','1:1.10-2','<=','1:1.9',False),
            ('virtual>=2:0.4','2:0.5-1','>=','2:0.4',True),
            ('virtual=1:1.10-2','2:0.5-1','=','1:1.10-2',False),
            ('plain',None,None,None,True),
            ('plain>=1',None,'>=','1',False),
        )
        with patch.dict(os.environ,{'WAYPURE_ALPM_BACKEND':'native'}):
            for dependency,available,operator,required,expected in cases:
                with self.subTest(dependency=dependency):
                    self.assertEqual(version_satisfies(available,operator,required),expected)
                    self.assertEqual(backend.module.find_satisfier([package],dependency) is not None,expected)

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_versions_loads_archives_with_alpm_and_only_recurses_when_requested(self):
        config,database=self.native_config()
        recipe=self.root/'recipes/demo'
        self.package_archive(recipe,'1:1.0-2')
        self.package_archive(recipe,'2:0.5-1')
        self.package_archive(recipe/'src/nested','4:0.1-1')
        sentinel=self.root/'must-not-source'
        (recipe/'PKGBUILD').write_text('touch '+str(sentinel)+'\n')
        result=self.native_command('waypure-versions',config=config)
        self.assertEqual(result.returncode,0,result.stderr)
        row=next(line.split() for line in result.stdout.splitlines() if line.startswith('demo '))
        self.assertEqual(row[1:4],['2:0.5','2:1.0','both'])
        recursive=self.native_command('waypure-versions','--recursive',config=config)
        self.assertEqual(recursive.returncode,0,recursive.stderr)
        row=next(line.split() for line in recursive.stdout.splitlines() if line.startswith('demo '))
        self.assertEqual(row[1],'4:0.1')
        self.assertFalse(sentinel.exists())

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_cleanup_native_preview_does_not_query_or_modify_via_pacman(self):
        config,database=self.native_config()
        package=database/'local/libx11-1.0-1'
        package.mkdir()
        (package/'desc').write_bytes(database_record('libx11','1.0-1'))
        result=self.native_command('waypure-remove-x11',config=config)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('  libx11\n',result.stdout)
        self.assertNotIn('demo-waypure',result.stdout)
        self.assertFalse((database/'db.lck').exists())

    @unittest.skipUnless(NATIVE,'system pyalpm needed')
    def test_installed_stack_audit_uses_configured_root_and_replacement_provisions(self):
        config,database=self.native_config()
        for name,fields in [('app','%DEPENDS%\njack>=1\n\n'),('pipewire-jack','%PROVIDES%\njack=1\n\n')]:
            folder=database/'local'/(name+'-1-1');folder.mkdir()
            (folder/'desc').write_bytes(database_record(name,'1-1')+fields.encode())
        before={str(p):p.read_bytes() for p in (database/'local').glob('*/desc')}
        result=self.native_command('waypure-audit-audio','--metadata-only','--family','jack','--package','app','--json',config=config)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['package_count'],3)
        self.assertEqual(report['packages']['app']['families']['jack']['package_path'],['app','pipewire-jack'])
        self.assertEqual(report['root'],str(self.root/'installation root')+'/')
        self.assertEqual(before,{str(p):p.read_bytes() for p in (database/'local').glob('*/desc')})
        self.assertFalse((database/'db.lck').exists())
