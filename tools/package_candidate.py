"""Canonical source ZIP: manifest in archive META only, never in Git source tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, BadZipFile
from privacy_gate import ALLOWED, ROOT, check_content, verify, verify_path, identities_from_private_baseline

BASELINE_SHA256='3f092528ee612827eb83734e32964f2a025cf99b0179e6db08b0df27b6fc8e51'
MANIFEST='META/manifest.json'
KIND='Local-Ops-Workbench-source-candidate'
# The existing privacy allowlist is also the authoritative mandatory source contract.
# Its 19 paths are fixed by the authenticated baseline, not by a generated manifest.
MANDATORY_SOURCE_COUNT=19


def require_complete_source(paths):
    if len(ALLOWED)!=MANDATORY_SOURCE_COUNT:
        raise ValueError('MANDATORY_SOURCE_CONTRACT_INVALID: expected 19 defined paths')
    missing=sorted(ALLOWED-set(paths))
    if missing:
        raise ValueError('MANDATORY_SOURCE_FILES_MISSING: '+', '.join(missing))



def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _zip_write(z, name, data):
    info=ZipInfo(name,(2026,1,1,0,0,0))
    info.compress_type=ZIP_DEFLATED
    info.external_attr=0o100644<<16
    z.writestr(info,data,compresslevel=9)


def prepare(root,out,private_names=()):
    root,out=Path(root).resolve(),Path(out).resolve()
    verify(root, private_names=private_names)
    filepaths=sorted((n,root/n) for n in ALLOWED if (root/n).is_file())
    require_complete_source(n for n,p in filepaths)
    manifest={
        'kind':KIND,
        'contract_version':1,
        'upstream_zip_sha256':BASELINE_SHA256,
        'source':[{'path':n, 'sha256':sha(p.read_bytes()), 'bytes':p.stat().st_size} for n,p in filepaths],
        'external_data':'required and deliberately excluded',
        'version_authority':'historical v1.6.0 candidate (no production release version assigned)',
    }
    out.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2).encode('utf8')+b'\n'
    if out.exists():raise ValueError('Refusing to overwrite existing candidate archive')
    with ZipFile(out,'x') as z:
        for n,p in filepaths:_zip_write(z,n,p.read_bytes())
        _zip_write(z,MANIFEST,payload)
    validate(out,private_names=private_names)
    return {'path':str(out),'sha256':sha(out.read_bytes()),'source_files':len(filepaths)}


def validate(archive,expected_sha256=None,private_names=()):
    archive=Path(archive)
    if expected_sha256 is not None and sha(archive.read_bytes())!=expected_sha256:
        raise ValueError('External artifact SHA-256 mismatch')
    with ZipFile(archive) as z:
        infos=z.infolist(); names=[i.filename for i in infos]
        if len(set(names))!=len(names):raise ValueError('Duplicate ZIP entry')
        if MANIFEST not in names or set(names)-{MANIFEST}-ALLOWED:
            raise ValueError('Artifact includes forbidden entry or lacks manifest')
        for info in infos:
            mode=(info.external_attr>>16)&0o170000
            if info.is_dir() or mode not in (0,0o100000) or info.file_size>1024*1024:
                raise ValueError('Artifact has unsafe entry type or size')
        if z.testzip() is not None:raise ValueError('Corrupted artifact')
        # Independent of source[]: a regenerated manifest cannot bless missing code.
        require_complete_source(set(names)-{MANIFEST})
        info=z.getinfo(MANIFEST)
        if info.file_size>131072:raise ValueError('Oversized package metadata')
        manifest=json.loads(z.read(MANIFEST))
        if not isinstance(manifest,dict) or manifest.get('kind')!=KIND or manifest.get('contract_version')!=1:
            raise ValueError('Manifest contract mismatch')
        if manifest.get('upstream_zip_sha256')!=BASELINE_SHA256:
            raise ValueError('Baseline binding mismatch')
        source=manifest.get('source')
        if not isinstance(source,list) or any(not isinstance(item,dict) for item in source):
            raise ValueError('Malformed source manifest')
        paths=[item.get('path') for item in source]
        if any(not isinstance(name,str) for name in paths) or len(set(paths))!=len(paths):
            raise ValueError('Duplicate/malformed manifest source path')
        if set(names)-{MANIFEST}!=set(paths):raise ValueError('Manifest/ZIP inventory mismatch')
        for item in source:
            path=item['path'];verify_path(path)
            content=z.read(path)
            check_content(path,content,private_names)
            if not isinstance(item.get('bytes'),int) or isinstance(item['bytes'],bool) or len(content)!=item['bytes'] or sha(content)!=item.get('sha256'):
                raise ValueError('Candidate source digest mismatch')
    return True


def extract_source(archive,destination,expected_sha256=None,private_names=()):
    """Validate first; create a NEW source directory, omitting ZIP-only metadata.

    The destination must not exist. Existing installations and adjacent operational
    Excel/SQLite/config data are never moved, removed or replaced.
    """
    validate(archive,expected_sha256,private_names)
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Refusing to replace an existing source or operational directory')
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.mkdir(exist_ok=False)
    try:
        with ZipFile(archive) as z:
            for name in sorted(set(z.namelist())-{MANIFEST}):
                verify_path(name)
                target=destination/name
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(z.read(name))
        verify(destination,private_names=private_names)
        require_complete_source(n for n in ALLOWED if (destination/n).is_file())
        if (destination/MANIFEST).exists() or (destination/'META').exists():
            raise ValueError('Manifest is archive-only metadata, not source')
        return {'source_files':len(set(z.namelist())-{MANIFEST}), 'source_dir':str(destination)}
    except BaseException:
        # Only the directory created by this invocation may be removed.
        shutil.rmtree(destination)
        raise


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,default=ROOT)
    p.add_argument('--output',type=Path)
    p.add_argument('--validate',type=Path)
    p.add_argument('--extract-source',type=Path,help='New destination; no overwrite; manifest stays in ZIP')
    p.add_argument('--expected-sha256',help='Externally trusted artifact digest')
    p.add_argument('--private-baseline',type=Path,help='Locally authenticated historical ZIP; checks known private names in source and ZIP')
    args=p.parse_args()
    private_names=identities_from_private_baseline(args.private_baseline) if args.private_baseline else ()
    if args.extract_source:
        if not args.validate:p.error('--extract-source requires --validate')
        print(json.dumps(extract_source(args.validate,args.extract_source,args.expected_sha256,private_names)))
    elif args.validate:
        validate(args.validate,args.expected_sha256,private_names)
        print('PACKAGE_VERIFIED')
    else:
        if args.output is None:p.error('--output required')
        print(json.dumps(prepare(args.source,args.output,private_names),ensure_ascii=False))

if __name__=='__main__':
    main()
