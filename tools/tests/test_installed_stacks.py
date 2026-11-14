"""Installed stack diagnostics must distinguish evidence from active backends."""
from pathlib import Path
from types import SimpleNamespace
import contextlib,importlib.util,io,json,os,shutil,subprocess,sys,tempfile,unittest
from unittest.mock import patch
TOOLS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOLS))
from optionkit import installed_stacks as stacks
from optionkit.stack_rules import library_matches,package_matches,path_matches

def package(name,depends=(),provides=(),optdepends=(),files=(),version='1'):
    return SimpleNamespace(name=name,version=version,base=name,depends=depends,provides=provides,
                           optdepends=optdepends,files=[(f,0,0) for f in files],reason=0)

class Classification(unittest.TestCase):
    def test_audio_bridges_are_both_protocols_but_libpulse_is_not_a_server(self):
        self.assertEqual({m['family'] for m in package_matches('pipewire-jack')},{'jack','pipewire'})
        self.assertEqual({m['family'] for m in package_matches('pipewire-pulse')},{'pulse','pipewire'})
        self.assertEqual({m['family'] for m in package_matches('pipewire-alsa')},{'alsa','pipewire'})
        self.assertEqual({m['role'] for m in package_matches('libpulse')},{'client'})
        self.assertEqual({m['role'] for m in package_matches('pulseaudio')},{'server'})
    def test_graphics_interfaces_and_32_bit_sonames_are_separate(self):
        for library,family in [('libGL.so.1','legacy-gl'),('libOpenGL.so.0','opengl'),('libGLX_mesa.so.0','glx'),('libEGL.so.1','egl'),('libGLESv2.so.2','gles'),('libGLdispatch.so.0','glvnd'),('libvulkan_radeon.so','vulkan')]:
            self.assertIn(family,{m['family'] for m in library_matches(library)},library)
        self.assertIn('legacy-gl',{m['family'] for m in library_matches('/usr/lib32/libGL.so.1')})
    def test_alsa_control_bridges_and_server_modules_are_classified(self):
        for library,families in [('libasound_module_ctl_pulse.so',{'alsa','pulse'}),('libasound_module_ctl_pipewire.so',{'alsa','pipewire'}),('libpulsecommon-17.0.so',{'pulse'}),('libglx.so',{'glx','x11-glx'})]:
            self.assertTrue(families.issubset({m['family'] for m in library_matches(library)}),library)
    def test_alsa_profile_data_and_mesa_dispatch_are_inventoried(self):
        self.assertTrue(any(m['family']=='alsa' and m['role']=='configuration' for m in package_matches('alsa-card-profiles')))
        self.assertTrue(any(m['family']=='opengl' for m in library_matches('libglapi.so.0')))
    def test_x_names_do_not_capture_non_x_libraries(self):
        for name in ('libxml2','libxslt','libxcrypt','libxkbcommon','libxkbcommon-waypure'):
            self.assertFalse([m for m in package_matches(name) if m['domain']=='x11'],name)
        for name in ('libX11.so.6','libxcb.so.1','libxkbcommon-x11.so.0','libqxcb.so'):
            self.assertTrue([m for m in library_matches(name) if m['domain']=='x11'],name)
        self.assertTrue(package_matches('lib32-libx11'))

class ManifestBoundaries(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('elftools'),'optional python-pyelftools required')
    def test_debug_symbol_files_and_debug_primary_links_are_not_runtime_support(self):
        for path in ('usr/lib/debug/usr/lib/libGL.so.1.debug','usr/lib/debug/usr/lib/libjack.so.0.debug','usr/lib/debug/.build-id/aa/symbols','usr/lib/libX11.so.6.debug'):
            self.assertFalse(path_matches(path),path)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'usr/lib/debug/.build-id/aa';folder.mkdir(parents=True)
            (folder/'primary').symlink_to('/usr/lib/libjack.so.0')
            _,errors,_,found=stacks.scan_files({'symbols':package('symbols',files=('usr/lib/debug/.build-id/aa/primary',))},root)
            self.assertFalse(errors);self.assertFalse(found.get('symbols'))
    def test_only_json_files_are_vulkan_manifest_evidence(self):
        for path in ('usr/share/vulkan/icd.d/','usr/share/vulkan/explicit_layer.d/','usr/share/vulkan/icd.d/README.txt'):
            self.assertFalse(path_matches(path),path)
        self.assertEqual(path_matches('usr/share/vulkan/icd.d/gpu.json')[0]['role'],'ICD-manifest')
        self.assertEqual(path_matches('usr/share/vulkan/explicit_layer.d/layer.json')[0]['role'],'layer-manifest')
    def test_empty_manifest_directory_does_not_propagate_graphics_support(self):
        result=stacks.analyze({'dirs':package('dirs',files=('usr/share/vulkan/icd.d/',))})
        self.assertFalse(result['packages']['dirs']['families'])

class Graph(unittest.TestCase):
    def report(self,packages,**kwargs):
        return stacks.analyze({p.name:p for p in packages},**kwargs)
    def test_versioned_replacement_and_transitive_reverse_path(self):
        result=self.report([package('app',('middle>=2',)),package('middle-waypure',('jack',),('middle=2',),version='2'),package('pipewire-jack',provides=('jack=1',))])
        row=result['packages']['app']['families']['jack']
        self.assertEqual(row['package_path'],['app','middle-waypure','pipewire-jack'])
        self.assertFalse(row['conditional'])
    def test_optional_dependencies_do_not_become_required_paths_and_cycles_terminate(self):
        result=self.report([package('app',('middle',),optdepends=('jack: optional output',)),package('middle',('app',)),package('jack')])
        self.assertNotIn('jack',result['packages']['app']['families'])
        self.assertEqual(result['optional_relations'][0]['providers'],['jack'])
    def test_ambiguous_providers_are_conditional_and_missing_are_explicit(self):
        result=self.report([package('app',('audio>=2','missing')),package('safe',provides=('audio=2',)),package('jack-wrapper',provides=('audio=2','jack=1'))])
        self.assertTrue(result['packages']['app']['families']['jack']['conditional'])
        self.assertEqual(len(result['unresolved_dependencies']),1)
        self.assertEqual(len(result['ambiguous_dependencies']),1)
    def test_concrete_provider_wins_and_source_base_does_not_classify_server(self):
        p=package('libpulse');p.base='pulseaudio'
        r=self.report([package('app',('libpulse',)),p,package('alternative',('jack',),('libpulse',)),package('jack')])
        self.assertNotIn('jack',r['packages']['app']['families'])
        self.assertEqual(r['packages']['libpulse']['evidence'][0]['role'],'client')
    def test_undeclared_bundled_plugin_is_evidence_not_a_required_jack_package(self):
        objects=[{'owners':['mpg123'],'path':'/usr/lib/mpg123/output_jack.so','needed':['libjack.so.0'],'sonames':[]}]
        r=self.report([package('player',('mpg123',)),package('mpg123')],objects=objects)
        self.assertEqual(r['packages']['player']['families']['jack']['package_path'],['player','mpg123'])
        e=r['packages']['mpg123']['evidence'];self.assertTrue(any(v['kind']=='elf-needed' for v in e))
        self.assertEqual(r['packages']['player']['families']['jack']['basis'],'elf')
    def test_display_filter_retains_full_graph(self):
        r=self.report([package('app',('middle',)),package('middle',('jack',)),package('jack')])
        selected=stacks.select_report(r,domain='audio',families=['jack'],patterns=['app'])
        self.assertEqual(list(selected['packages']),['app'])
        self.assertEqual(selected['packages']['app']['families']['jack']['package_path'],['app','middle','jack'])

@unittest.skipUnless(importlib.util.find_spec('elftools'),'optional python-pyelftools required')
class Files(unittest.TestCase):
    def test_symlinks_and_special_files_are_not_opened_as_elf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'plain').write_text('not ELF');(root/'libjack.so.0').symlink_to('missing');os.mkfifo(root/'fifo')
            objects,errors,stats,evidence=stacks.scan_files({'p':package('p',files=('plain','libjack.so.0','fifo'))},root)
            self.assertFalse(objects);self.assertFalse(errors)
            self.assertTrue(any(e['family']=='jack' and e['kind']=='symlink' for e in evidence['p']))
    def test_missing_file_does_not_report_clean_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,errors,_,_=stacks.scan_files({'p':package('p',files=('missing',))},Path(tmp))
            self.assertEqual(errors[0]['owners'],['p'])
    @unittest.skipUnless(shutil.which('cc'),'C compiler required for real ELF fixture')
    def test_dynamic_tags_and_library_ownership_without_executing_elf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'a.c').write_text('int jack_fixture(void) {return 1;}\n')
            subprocess.run(['cc','-shared','-fPIC',str(root/'a.c'),'-Wl,-soname,libjack.so.0','-o',str(root/'renamed.so')],check=True,capture_output=True)
            objects,errors,stats,_=stacks.scan_files({'bundler':package('bundler',files=('renamed.so',))},root)
            self.assertFalse(errors);self.assertEqual(stats['elf_files'],1)
            self.assertEqual(objects[0]['sonames'],['libjack.so.0'])
            r=stacks.analyze({'bundler':package('bundler')},objects=objects)
            self.assertIn('jack',r['packages']['bundler']['families'])
    def test_alternate_root_does_not_scan_through_parent_symlink_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'root';outside=base/'outside';root.mkdir();outside.mkdir()
            (outside/'library').write_bytes(b'not ELF')
            (root/'escape').symlink_to(outside,target_is_directory=True)
            _,errors,_,_=stacks.scan_files({'p':package('p',files=('escape/library',))},root)
            self.assertTrue(errors)
    def test_malformed_elf_is_a_coverage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'broken').write_bytes(b'\x7fELFbad')
            _,errors,_,_=stacks.scan_files({'p':package('p',files=('broken',))},root)
            self.assertTrue(errors)

class Commands(unittest.TestCase):
    def test_all_three_entrypoints_help(self):
        for name in ('audio','graphics','x11'):
            p=subprocess.run([sys.executable,str(TOOLS/('waypure-audit-'+name)),'--help'],text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stderr);self.assertIn('--metadata-only',p.stdout)
    def test_json_is_structured_and_coverage_failures_exit_two(self):
        r=stacks.analyze({'app':package('app',('jack',)),'jack':package('jack')})
        r.update({'inspection_errors':[{'path':'/unreadable','owners':['app'],'error':'denied'}],'database_stable':True,'scan':{'elf_files':0},'mode':'elf'})
        with patch.object(stacks,'collect',return_value=r),contextlib.redirect_stdout(io.StringIO()) as output:
            code=stacks.main('audio',['--json','--family','jack'])
        self.assertEqual(code,2);self.assertEqual(json.loads(output.getvalue())['status'],'incomplete')
