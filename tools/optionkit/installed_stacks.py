"""Read-only installed package and ELF stack diagnostics; no loader execution."""
from collections import defaultdict,deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from functools import lru_cache
from fnmatch import fnmatchcase
from pathlib import Path,PurePosixPath
import argparse,json,os,stat,sys,time
from .alpm import BackendError,BackendUnavailable,NativeBackend,get_backend
from .dependencies import split_dependency
from .installed_policy import InstalledGraph
from .stack_rules import FAMILIES,is_debug_path,library_matches,package_matches,path_matches


def evidence(rule,kind,value,**extra):
    return {**rule,'kind':kind,'value':value,**extra}


def analyze(packages,*,objects=(),file_evidence=None):
    """Resolve installed providers once, then reverse BFS once per category."""
    graph=InstalledGraph(packages)
    reverse=defaultdict(list);unresolved=[];ambiguous=[];optional=[]
    @lru_cache(maxsize=None)
    def providers(dependency):
        name,op,version=split_dependency(dependency)
        concrete=packages.get(name)
        if concrete and graph.matches(concrete.version,op,version):return (name,)
        return tuple(sorted({n for n,v in graph.providers.get(name,()) if graph.matches(v,op,version)}))
    rows={}
    for name,pkg in sorted(packages.items()):
        found=[evidence(m,'name',name) for m in package_matches(name)]
        for value in pkg.provides:
            capability=split_dependency(value)[0]
            found.extend(evidence(m,'provides',value) for m in package_matches(capability))
            found.extend(evidence(m,'provides',value) for m in library_matches(capability.removeprefix('lib:')))
        for entry in pkg.files:
            path=entry[0]
            if PurePosixPath(path).name.startswith('lib') or '/vulkan/' in path or path.endswith('/pw-jack'):
                found.extend(evidence(m,'file-list',path) for m in path_matches(path))
        found.extend((file_evidence or {}).get(name,()))
        rows[name]={'version':pkg.version,'reason':'explicit' if getattr(pkg,'reason',None)==0 else 'dependency',
                    'evidence':found,'families':{}}
        for dependency in pkg.depends:
            targets=providers(dependency)
            item={'package':name,'dependency':dependency,'providers':list(targets)}
            if not targets:unresolved.append(item)
            elif len(targets)>1:ambiguous.append(item)
            for target in targets:reverse[target].append((name,len(targets)>1))
        for value in getattr(pkg,'optdepends',()):
            dependency=value.split(': ',1)[0]
            targets=providers(dependency)
            matches=package_matches(split_dependency(dependency)[0])
            if targets or matches:optional.append({'package':name,'dependency':value,'providers':list(targets)})
    for obj in objects:
        for name in obj['owners']:
            for field,kind in (('needed','elf-needed'),('sonames','soname')):
                for value in obj[field]:
                    rows[name]['evidence'].extend(evidence(m,kind,value,path=obj['path']) for m in library_matches(value))
    # Prefer a definite path over a shorter conditional provider choice. States
    # are bounded to two per package, even when the graph has cycles.
    seeds=defaultdict(dict)
    for name,row in rows.items():
        for item in row['evidence']:
            basis='elf' if item['kind'] in {'elf-needed','soname'} else 'metadata'
            seeds[item['family']].setdefault(name,basis)
        row['evidence']=sorted({json.dumps(v,sort_keys=True):v for v in row['evidence']}.values(),key=lambda v:(v['family'],v['kind'],v['value'],v.get('path','')))
    for family,origins in sorted(seeds.items()):
        paths={(name,False):([name],basis) for name,basis in sorted(origins.items())}
        queue=deque(paths)
        while queue:
            current,conditional=queue.popleft();path,basis=paths[current,conditional]
            for parent,maybe in sorted(reverse[current]):
                state=(parent,conditional or maybe)
                if state not in paths:
                    paths[state]=([parent,*path],basis);queue.append(state)
        for name in rows:
            state=(name,False) if (name,False) in paths else (name,True)
            if state in paths:
                path,basis=paths[state]
                rows[name]['families'][family]={'package_path':path,'conditional':state[1],'basis':basis}
    return {'packages':rows,'unresolved_dependencies':unresolved,'ambiguous_dependencies':ambiguous,
            'optional_relations':optional,'definitions':{f:{'domain':d,'description':label} for f,(d,label) in FAMILIES.items()}}


def scan_files(packages,root,*,jobs=4):
    """Read only regular packaged files, with bounded batches and no symlink roots."""
    try:
        from elftools.elf.elffile import ELFFile
        from elftools.common.exceptions import ELFError
    except ImportError as error:
        raise BackendUnavailable('ELF scans require python-pyelftools; use --metadata-only for package metadata') from error
    root=root.resolve()
    @lru_cache(maxsize=8192)
    def parent_inside(parent):
        return parent.resolve().is_relative_to(root)
    owners=defaultdict(set)
    for name,pkg in packages.items():
        for relative,_,_ in pkg.files:
            if not relative.endswith('/'):owners[relative].add(name)
    stats={'listed_files':len(owners),'regular_files':0,'elf_files':0,'symlinks':0,'special_files':0}
    def inspect(relative):
        names=sorted(owners[relative]);path=root/relative
        base={'owners':names,'path':str(path)}
        found=[]
        try:
            if PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts:
                raise ValueError('Unsafe path in installed file list')
            if root!=Path('/') and not parent_inside(path.parent):
                raise ValueError('Parent symlink escapes configured root')
            before=path.lstat()
            if stat.S_ISLNK(before.st_mode):
                target=os.readlink(path)
                for rule in ([] if is_debug_path(relative) else [*path_matches(relative),*path_matches(target)]):
                    found.append(evidence(rule,'symlink',relative,target=target))
                return None,None,'symlinks',found,names
            if not stat.S_ISREG(before.st_mode):return None,None,'special_files',found,names
            # O_NONBLOCK avoids blocking if a regular file is replaced by a FIFO.
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as stream:
                opened=os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):raise ValueError('File type changed while scanning')
                if stream.read(4)!=b'\x7fELF':return None,None,'regular_files',found,names
                stream.seek(0);elf=ELFFile(stream);needed=[];sonames=[]
                for segment in elf.iter_segments():
                    if segment['p_type']!='PT_DYNAMIC':continue
                    # Guile VM ELF images may have custom tags without DT_STRTAB.
                    # Request only native dependency string tags, never every tag.
                    needed.extend(t.needed for t in segment.iter_tags(type='DT_NEEDED'))
                    sonames.extend(t.soname for t in segment.iter_tags(type='DT_SONAME'))
                after=os.fstat(stream.fileno())
                if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise ValueError('ELF changed during inspection')
                return {**base,'needed':needed,'sonames':sonames,'class':elf.elfclass,'machine':elf.header['e_machine']},None,'elf_files',found,names
        except (OSError,ValueError,ELFError,AssertionError) as error:
            return None,{**base,'error':str(error),'type':type(error).__name__},None,found,names
    objects=[];errors=[];file_evidence=defaultdict(list)
    paths=iter(sorted(owners))
    # executor.map is eager on supported Python3.11. Batches cap outstanding
    # futures instead of allocating one future for every installed file.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        while batch:=[p for _,p in zip(range(256),paths)]:
            for obj,error,kind,found,names in pool.map(inspect,batch):
                if obj:objects.append(obj);stats['regular_files']+=1
                if error:errors.append(error)
                if kind:stats[kind]+=1
                for name in names:file_evidence[name].extend(found)
    return objects,errors,stats,dict(file_evidence)


def database_stamp(handle):
    """Detect package database writes without loading unrelated sync databases."""
    directory=Path(handle.dbpath)/'local'
    return [(str(p.relative_to(directory)),p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns)
            for p in sorted(directory.glob('*/*')) if p.name in {'desc','files'}]


def collect(*,metadata_only=False,jobs=4):
    start=time.monotonic();backend=get_backend()
    if not isinstance(backend,NativeBackend):
        raise BackendUnavailable('Installed stack audits require system Python with pyalpm (native backend)')
    handle=backend.handle;before=database_stamp(handle)
    packages={p.name:p for p in handle.get_localdb().pkgcache}
    objects,errors,stats,found=([],[],{'elf_files':0},{}) if metadata_only else scan_files(packages,Path(handle.root),jobs=jobs)
    result=analyze(packages,objects=objects,file_evidence=found)
    result.update({'timestamp':datetime.now(timezone.utc).isoformat(),'mode':'metadata-only' if metadata_only else 'elf',
                   'root':handle.root,'database':handle.dbpath,'database_stable':before==database_stamp(handle),
                   'package_count':len(packages),'scan':stats,'inspection_errors':errors,
                   'elapsed_seconds':round(time.monotonic()-start,3)})
    return result


def select_report(result,*,domain,families=(),patterns=()):
    chosen={f for f,(d,_) in FAMILIES.items() if (domain=='all' or d==domain) and (not families or f in families)}
    rows={}
    for name,row in result['packages'].items():
        if patterns and not any(fnmatchcase(name,p) for p in patterns):continue
        hits={f:v for f,v in row['families'].items() if f in chosen}
        if hits:rows[name]={**row,'families':hits,'evidence':[e for e in row['evidence'] if e['family'] in chosen]}
    optional=[r for r in result['optional_relations'] if (not patterns or any(fnmatchcase(r['package'],p) for p in patterns)) and
              (any(set(result['packages'][p]['families']) & chosen for p in r['providers']) or
               any(m['family'] in chosen for m in package_matches(split_dependency(r['dependency'].split(': ',1)[0])[0])))]
    return {**result,'domain':domain,'families':sorted(chosen),'packages':rows,'optional_relations':optional,
            'status':'incomplete' if result.get('inspection_errors') or result['unresolved_dependencies'] or not result.get('database_stable',True) else 'complete',
            'limits':[
                'Required package paths follow installed depends/provides. Multiple installed virtual providers are conditional; optional edges are listed separately.',
                'ELF evidence reads direct DT_NEEDED/DT_SONAME, including unused/optional plugins. A package path to an ELF user is not an ELF loader-resolution trace.',
                'Metadata/file lists are declarations. Detected libraries/plugins do not establish the active audio backend, display protocol or driver.',
                'No dlopen-only, statically linked protocol implementation, unowned file, container/Flatpak or loaded kernel-module inventory. No applications or dynamic loaders executed.',
                'Unreadable/missing/malformed files or changed package databases make coverage incomplete. Metadata-only mode does not inspect ELF files.',
                'libGL alone does not prove GLX; ALSA/Pulse clients can use PipeWire. This is descriptive inventory, not a package-removal recommendation.',
            ]}


def render_text(result,details=False):
    print(f"Installed {result['domain']} stacks: {result['status']} ({result.get('mode','metadata')})")
    print(f"Packages: {result.get('package_count',len(result['packages']))}; ELF files: {result.get('scan',{}).get('elf_files',0)}; matched packages: {len(result['packages'])}")
    print('Detected support is not proof that a backend or plugin is active. Optional dependencies are separate.')
    for family in result['families']:
        rows={n:r for n,r in result['packages'].items() if family in r['families']}
        if not rows:continue
        print(f"\n{family}: {FAMILIES[family][1]} ({len(rows)} packages)")
        for name,row in rows.items():
            path=row['families'][family];own=[e for e in row['evidence'] if e['family']==family]
            kinds=','.join(sorted({e['kind'] for e in own})) if own else 'dependency-path'
            suffix=' [conditional provider]' if path['conditional'] else ''
            print(f"  {name} {row['version']} [{row['reason']}; {kinds}]{suffix}")
            if details:
                print('    package path: '+' -> '.join(path['package_path'])+'; endpoint evidence: '+path['basis'])
                for e in own:print(f"    {e['kind']} ({e['role']}): {e.get('path','')} {e['value']}".rstrip())
    if result['optional_relations']:
        print('\nOptional relations (not required dependency paths):')
        for row in result['optional_relations']:
            print(f"  {row['package']}: {row['dependency']} [installed: {', '.join(row['providers']) or 'none'}]")
    for key,label in (('ambiguous_dependencies','Conditional providers'),('unresolved_dependencies','Unresolved required dependencies'),('inspection_errors','File inspection gaps')):
        values=result[key];print(f'\n{label}: {len(values)}')
        if details:
            for value in values:print('  '+json.dumps(value,sort_keys=True))
    if not result.get('database_stable',True):print('Package database changed during scan; rerun.')
    print('\nScope: installed package metadata and direct ELF tags; no active-backend or removal decision. Use --details or --json for evidence and coverage gaps.')


def main(domain,argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family',action='append',choices=sorted(FAMILIES),default=[],help='limit to a stack family; repeatable')
    parser.add_argument('--package',action='append',default=[],metavar='GLOB',help='filter displayed installed packages; graph stays complete')
    parser.add_argument('--all',action='store_true',help='report all three domains in one scan')
    parser.add_argument('--metadata-only',action='store_true',help='fast package/file-list query without reading ELF files')
    parser.add_argument('--details',action='store_true',help='show package paths, file evidence and coverage gaps')
    parser.add_argument('--json',action='store_true',help='machine-readable evidence and coverage')
    parser.add_argument('--jobs',type=int,default=4,help='file scan workers (1..32; default4)')
    args=parser.parse_args(argv);domain='all' if args.all else domain
    if not 1<=args.jobs<=32:parser.error('--jobs must be between1 and32')
    if any(FAMILIES[f][0]!=domain for f in args.family) and domain!='all':parser.error('--family belongs to another domain; use --all')
    try:
        report=select_report(collect(metadata_only=args.metadata_only,jobs=args.jobs),domain=domain,families=args.family,patterns=args.package)
        if args.json:print(json.dumps(report,indent=2))
        else:render_text(report,args.details)
        return 2 if report['status']=='incomplete' else 0
    except (BackendError,OSError,ValueError) as error:
        print(f'waypure-audit-{domain}: {error}',file=sys.stderr);return 1
