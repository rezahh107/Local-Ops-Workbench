"""Fail-closed allowlisted source, staging, new-history, and private-identity checks.

A local exact-baseline archive supplies known private identities in memory for the
pre-push gate; none are copied to the repository, CI, manifests, or diagnostics.
This is not a universal PII detector; review remains necessary.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    '.python-version', '.gitattributes', '.gitignore',
    'server.py', 'index.template.html', 'start_with_uv.bat', 'start_uv_offline.bat',
    'README.md', 'CHANGELOG.md',
    'tools/privacy_gate.py', 'tools/package_candidate.py', 'tools/smoke_candidate.py',
    'tools/private_config_from_baseline.py', 'tools/verify.py',
    'tests/synthetic_fixtures.py', 'tests/test_application.py', 'tests/test_delivery.py',
    'tests/browser_contract.py', '.github/workflows/ci.yml',
}
IGNORED = {'.git', '__pycache__', '.venv', 'dist', 'build', '.pytest_cache'}


def tracked_files(root):
    return {p.relative_to(root).as_posix(): p for p in root.rglob('*')
            if p.is_file() and not (set(p.relative_to(root).parts) & IGNORED)}


def verify_path(path):
    if path not in ALLOWED or path.startswith('/') or '..' in Path(path).parts:
        raise ValueError('DENIED: unapproved source path (includes private/generated data)')


def normalized_identity(text):
    text = unicodedata.normalize('NFKC', text).translate(str.maketrans({'ي':'ی','ك':'ک','‌':' '}))
    return re.sub(r'\s+', ' ', text).strip().casefold()


def identities_from_private_baseline(path):
    # The original archive is read-only; the exact SHA is verified by extractor.
    from private_config_from_baseline import from_baseline
    cfg = from_baseline(path)
    supporters = cfg['supporters']
    candidates = [*cfg['intelligence_eligible'], *supporters['manager_alias'].keys(),
                  *supporters['manager_alias'].values(), *supporters['excluded'],
                  *supporters['sheets'], *cfg['top_book_exclusion'].values(),
                  *(r['name'] for r in cfg['historical_supporters'])]
    return {value for value in candidates if isinstance(value, str) and len(normalized_identity(value)) >= 4}


def check_content(path, content, private_names=()):
    if b'\0' in content or len(content) > 1024 * 1024:
        raise ValueError('DENIED: unexpected binary/oversized source')
    try:
        decoded = content.decode('utf-8')
    except UnicodeError as exc:
        raise ValueError('DENIED: non-UTF8 source') from exc
    if path == 'index.template.html' and (
        b'__HISTORICAL_RECORDS_JSON__' not in content or
        b'__BOOK_EXCLUSION_JSON__' not in content or
        b'__SUPPORTER_EXCLUSION_JSON__' not in content or
        b'function topBookConfigured()' not in content
    ):
        raise ValueError('DENIED: missing private presentation configuration boundary')
    if path == 'server.py' and b'PRIVATE_CONFIG = load_private_config()' not in content:
        raise ValueError('DENIED: server private-data boundary missing')
    normalized = normalized_identity(decoded)
    for name in private_names:
        if normalized_identity(name) in normalized:
            raise ValueError('DENIED: private identity appears in committable content')


def run_git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True).stdout


def verify(root=ROOT, base=None, private_names=None):
    root = Path(root).resolve()
    # Every pre-push check requires a private-name source, not an empty best-effort scan.
    if base and private_names is None:
        raise ValueError('DENIED: pre-push qualification requires verified private identities')
    names = tuple(private_names or ())
    for path, source in tracked_files(root).items():
        verify_path(path)
        if source.is_symlink():
            raise ValueError('DENIED: source symlink')
        check_content(path, source.read_bytes(), names)
    if not (root/'.git').exists():
        if base:
            raise ValueError('DENIED: new-history verification needs authentic Git checkout')
        return {'working':'PASS', 'staging':'NOT_PROVEN (no git)', 'new_history':'NOT_PROVEN (no base supplied)'}
    # Inspect the entire index, not only its diff against HEAD, including file modes.
    entries = run_git(root, 'ls-files', '--stage', '-z').split(b'\0')
    for entry in filter(None, entries):
        metadata, path_raw = entry.split(b'\t', 1)
        mode, blob_sha, stage = metadata.split()
        if mode not in (b'100644', b'100755') or stage != b'0':
            raise ValueError('DENIED: staged symlink/submodule/unmerged file')
        path = path_raw.decode('utf-8')
        verify_path(path)
        check_content(path, run_git(root, 'show', ':'+path), names)
    if base:
        # Fail on missing/ambiguous base, including non-ancestor or unrelated history.
        run_git(root, 'rev-parse', '--verify', f'{base}^{{commit}}')
        if subprocess.run(['git','-C',str(root),'merge-base','--is-ancestor',base,'HEAD']).returncode:
            raise ValueError('DENIED: base is not an ancestor of HEAD')
        commits = run_git(root, 'rev-list', f'{base}..HEAD').decode().splitlines()
        for commit in commits:
            check_content('commit-message', run_git(root, 'show', '-s', '--format=%B', commit), names)
            for raw in filter(None, run_git(root,'ls-tree','-r','-z',commit).split(b'\0')):
                meta,path_raw=raw.split(b'\t',1)
                mode,kind,blob_sha=meta.split()
                if kind!=b'blob' or mode not in (b'100644',b'100755'):
                    raise ValueError('DENIED: unsafe Git tree entry')
                path=path_raw.decode('utf8')
                verify_path(path)
                check_content(path,run_git(root,'show',f'{commit}:{path}'),names)
        return {'working':'PASS', 'staging':'PASS', 'new_history_commits':len(commits), 'new_history':'PASS'}
    return {'working':'PASS', 'staging':'PASS', 'new_history':'NOT_PROVEN (no base supplied)'}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source', type=Path, default=ROOT)
    p.add_argument('--base', help='Authenticated base commit for pre-push qualification')
    p.add_argument('--private-baseline', type=Path, help='Exact original ZIP, local only; never copy to Git')
    args=p.parse_args()
    try:
        names = identities_from_private_baseline(args.private_baseline) if args.private_baseline else None
        print('Privacy source gate:',verify(args.source,args.base,names))
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        # Never render private literal content or file-derived personally identifying values.
        print('PRIVACY_GATE_FAIL:',str(exc),file=sys.stderr)
        raise SystemExit(1)

if __name__=='__main__':
    main()
