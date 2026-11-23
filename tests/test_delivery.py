"""Falsification of privacy, source-history and packaging gates."""
import importlib
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from privacy_gate import ALLOWED,verify,identities_from_private_baseline
from package_candidate import MANIFEST,prepare,validate,extract_source


class DeliveryContract(unittest.TestCase):
    def test_source_allowlist_positive_and_forbidden_file_negative(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'server.py').write_bytes((ROOT/'server.py').read_bytes())
            assert verify(p)['working']=='PASS'
            (p/'operational.xlsx').write_bytes(b'FABRICATED_NOT_REAL')
            with self.assertRaisesRegex(ValueError,'DENIED'):
                verify(p)

    def test_package_inventory_checksum_and_malformed_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            good=p/'candidate.zip'
            prepare(ROOT,good)
            self.assertTrue(validate(good))
            self.assertTrue(ZipFile(good).testzip() is None)
            with self.assertRaisesRegex(ValueError,'External artifact SHA-256 mismatch'):
                validate(good,'0'*64)
            tampered=p/'tampered.zip'
            with ZipFile(good) as original, ZipFile(tampered,'w',ZIP_DEFLATED) as out:
                for filename in original.namelist():
                    data=original.read(filename)
                    if filename=='server.py':data+=b'\n# synthetic corruption\n'
                    out.writestr(filename,data)
            with self.assertRaisesRegex(ValueError,'digest mismatch'):
                validate(tampered)
            inserted=p/'inserted.zip'
            with ZipFile(good) as original,ZipFile(inserted,'w',ZIP_DEFLATED) as out:
                for n in original.namelist():out.writestr(n,original.read(n))
                out.writestr('dashboard.sqlite3',b'FAKE_SYNTHETIC')
            with self.assertRaisesRegex(ValueError,'forbidden entry'):
                validate(inserted)
            malformed=p/'malformed.zip';malformed.write_bytes(b'not a zip')
            with self.assertRaises(Exception):validate(malformed)

    def test_new_history_rejects_forbidden_even_if_deleted_later(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            def git(*cmd):
                return subprocess.check_output(['git','-C',str(p),*cmd],stderr=subprocess.PIPE).decode().strip()
            git('init','-q')
            git('config','user.name','Synthetic Tester');git('config','user.email','test@example.invalid')
            (p/'README.md').write_text('synthetic baseline\n',encoding='utf8')
            git('add','README.md');git('commit','-qm','base')
            base=git('rev-parse','HEAD')
            (p/'server.py').write_bytes((ROOT/'server.py').read_bytes())
            git('add','server.py');git('commit','-qm','safe code')
            self.assertEqual(verify(p,base,private_names={'مورد آزمایشی ساختگی'})['new_history'],'PASS')
            (p/'private.sqlite3').write_bytes(b'FAKE_ONLY')
            git('add','private.sqlite3');git('commit','-qm','bad staged')
            (p/'private.sqlite3').unlink()
            git('add','-u');git('commit','-qm','attempt to hide history')
            with self.assertRaisesRegex(ValueError,'DENIED'):
                verify(p,base,private_names={'مورد آزمایشی ساختگی'})

    def test_private_name_in_worktree_stage_and_new_history_is_denied(self):
        synthetic_identity='نام کاملاً ساختگی تست'
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            def git(*args):
                return subprocess.check_output(['git','-C',str(p),*args],stderr=subprocess.PIPE).decode().strip()
            git('init','-q')
            git('config','user.name','Synthetic Tester')
            git('config','user.email','test@example.invalid')
            (p/'README.md').write_text('Synthetic base\n',encoding='utf8')
            git('add','README.md');git('commit','-qm','base')
            base=git('rev-parse','HEAD')
            with self.assertRaisesRegex(ValueError,'private identities'):
                verify(p,base)
            self.assertEqual(verify(p,base,private_names={synthetic_identity})['new_history'],'PASS')
            (p/'README.md').write_text('Source '+synthetic_identity,encoding='utf8')
            with self.assertRaisesRegex(ValueError,'private identity'):
                verify(p,base,private_names={synthetic_identity})
            git('add','README.md')
            with self.assertRaisesRegex(ValueError,'private identity'):
                verify(p,base,private_names={synthetic_identity})
            git('commit','-qm','unapproved synthetic test identity')
            (p/'README.md').write_text('Synthetic clean\n',encoding='utf8')
            git('add','README.md');git('commit','-qm','hide sensitive earlier commit')
            with self.assertRaisesRegex(ValueError,'private identity'):
                verify(p,base,private_names={synthetic_identity})

    def test_known_private_name_is_rejected_in_packaging_and_artifact_qualification(self):
        synthetic_name='هویت کاملاً ' + 'ساختگی بسته'
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'source';source.mkdir()
            for name in ALLOWED:
                destination=source/name
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes((ROOT/name).read_bytes())
            (source/'README.md').write_text('No private identities',encoding='utf8')
            good=p/'valid.zip'
            prepare(source,good,private_names={synthetic_name})
            self.assertTrue(validate(good,private_names={synthetic_name}))
            (source/'README.md').write_text('Synthetic payload '+synthetic_name,encoding='utf8')
            denied=p/'denied.zip'
            with self.assertRaisesRegex(ValueError,'private identity'):
                prepare(source,denied,private_names={synthetic_name})
            self.assertFalse(denied.exists())
            # A package built without access to the private registry must still be
            # rejected when later qualified against that trusted registry.
            unknown=p/'unqualified.zip'
            prepare(source,unknown)
            with self.assertRaisesRegex(ValueError,'private identity'):
                validate(unknown,private_names={synthetic_name})

    def test_required_19_file_contract_survives_forged_consistent_manifest(self):
        # A complete archive passes, but a manifest faithfully regenerated from
        # only the remaining 18 files must NOT make an incomplete archive valid.
        self.assertEqual(len(ALLOWED),19)
        self.assertIn('server.py',ALLOWED)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            intact=p/'intact.zip'
            built=prepare(ROOT,intact)
            self.assertEqual(built['source_files'],19)
            self.assertTrue(validate(intact))
            extracted=p/'complete-source'
            result=extract_source(intact,extracted)
            self.assertEqual(result['source_files'],19)
            self.assertEqual({n for n in ALLOWED if (extracted/n).is_file()},ALLOWED)
            self.assertFalse((extracted/MANIFEST).exists())

            incomplete=p/'incomplete-with-accurate-manifest.zip'
            with ZipFile(intact) as original,ZipFile(incomplete,'w',ZIP_DEFLATED) as forged:
                source_files={n:original.read(n) for n in original.namelist()
                              if n not in ('server.py',MANIFEST)}
                metadata=json.loads(original.read(MANIFEST))
                metadata['source']=[
                    {'path':name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
                    for name,payload in sorted(source_files.items())
                ]
                for name,payload in source_files.items():
                    forged.writestr(name,payload)
                forged.writestr(MANIFEST,json.dumps(metadata,sort_keys=True).encode('utf8'))
            # Independently establish that the forged manifest is accurate.
            with ZipFile(incomplete) as archive:
                doc=json.loads(archive.read(MANIFEST))
                self.assertEqual(len(doc['source']),18)
                self.assertEqual({row['path'] for row in doc['source']},set(archive.namelist())-{MANIFEST})
                for row in doc['source']:
                    payload=archive.read(row['path'])
                    self.assertEqual(row['bytes'],len(payload))
                    self.assertEqual(row['sha256'],hashlib.sha256(payload).hexdigest())
            with self.assertRaisesRegex(ValueError,r'MANDATORY_SOURCE_FILES_MISSING: server\.py'):
                validate(incomplete)
            with self.assertRaisesRegex(ValueError,r'MANDATORY_SOURCE_FILES_MISSING: server\.py'):
                extract_source(incomplete,p/'must-not-create')
            self.assertFalse((p/'must-not-create').exists())

            # Build/prepare must also reject an incomplete source rather than
            # creating a self-consistent but invalid 18-file candidate.
            (extracted/'server.py').unlink()
            self.assertEqual(verify(extracted)['working'],'PASS')  # privacy-only, not completeness
            output=p/'must-not-build.zip'
            with self.assertRaisesRegex(ValueError,r'MANDATORY_SOURCE_FILES_MISSING: server\.py'):
                prepare(extracted,output)
            self.assertFalse(output.exists())
            command=subprocess.run([sys.executable,str(ROOT/'tools/package_candidate.py'),
                                   '--source',str(extracted),'--output',str(output)],
                                   capture_output=True,text=True)
            self.assertNotEqual(command.returncode,0)
            self.assertIn('MANDATORY_SOURCE_FILES_MISSING: server.py',command.stderr)
            self.assertFalse(output.exists())

    def test_archive_only_metadata_and_extraction_rejecting_contract(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            good=p/'good.zip'
            prepare(ROOT,good)
            tree=p/'extracted'
            result=extract_source(good,tree,hashlib.sha256(good.read_bytes()).hexdigest())
            self.assertEqual(result['source_files'],len(ALLOWED))
            self.assertFalse((tree/MANIFEST).exists())
            self.assertEqual(verify(tree)['working'],'PASS')
            with self.assertRaisesRegex(ValueError,'Refusing to replace'):
                extract_source(good,tree)
            # Reintroducing package-only metadata into source must be rejected.
            (tree/'META').mkdir()
            (tree/MANIFEST).write_text('{}',encoding='utf8')
            with self.assertRaisesRegex(ValueError,'DENIED'):
                verify(tree)
            (tree/MANIFEST).unlink(); (tree/'META').rmdir()
            self.assertEqual(verify(tree)['working'],'PASS')

            broken=p/'bad-manifest.zip'
            with ZipFile(good) as existing,ZipFile(broken,'w',ZIP_DEFLATED) as out:
                for n in existing.namelist():
                    content=existing.read(n)
                    if n==MANIFEST:
                        doc=json.loads(content)
                        doc['source'][0]['sha256']='0'*64
                        content=json.dumps(doc).encode('utf8')
                    out.writestr(n,content)
            with self.assertRaisesRegex(ValueError,'digest mismatch'):
                extract_source(broken,p/'must-not-exist')
            self.assertFalse((p/'must-not-exist').exists())
            bad_contract=p/'bad-contract.zip'
            with ZipFile(good) as existing,ZipFile(bad_contract,'w',ZIP_DEFLATED) as out:
                for n in existing.namelist():
                    content=existing.read(n)
                    if n==MANIFEST:
                        doc=json.loads(content)
                        doc['contract_version']=999
                        content=json.dumps(doc).encode('utf8')
                    out.writestr(n,content)
            with self.assertRaisesRegex(ValueError,'Manifest contract mismatch'):
                validate(bad_contract)

    def test_extraction_never_overwrites_external_operational_files(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            archive=p/'candidate.zip';prepare(ROOT,archive)
            operational=p/'operational';operational.mkdir()
            excel=operational/'keep.xlsx';database=operational/'keep.sqlite3'
            excel.write_bytes(b'SYNTHETIC EXCEL OWNED BY USER')
            database.write_bytes(b'SYNTHETIC SQLITE OWNED BY USER')
            originals={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [excel,database]}
            extracted=operational/'new-install'
            extract_source(archive,extracted)
            self.assertTrue((extracted/'server.py').is_file())
            with self.assertRaisesRegex(ValueError,'Refusing to replace'):
                extract_source(archive,operational)
            with self.assertRaisesRegex(ValueError,'Refusing to replace'):
                extract_source(archive,extracted)
            self.assertEqual(originals,{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [excel,database]})
            self.assertEqual(verify(extracted)['working'],'PASS')



if __name__=='__main__':unittest.main()

# Security regression tests use only synthetic config; the baseline ZIP is never
# required by CI, and no contents of private config are printed.
import os
import multiprocessing
from unittest import mock
from contextlib import contextmanager

sys.path.insert(0, str(ROOT/'tools'))
import private_config_from_baseline as migration


def _synthetic_private_config(value='synthetic-A'):
    return {'top_book_exclusion': {'manager': value, 'name': 'fixture'},
            'historical_supporters': [], 'supporters': {}, 'intelligence_eligible': []}


def _racing_config_worker(path, value, event, queue):
    event.wait()
    try:
        migration.create_private_config(path, _synthetic_private_config(value))
        queue.put('CREATED')
    except FileExistsError:
        queue.put('EXISTS')
    except BaseException:
        queue.put('UNEXPECTED_FAILURE')


def _run_as_unauthorized_posix(code, path):
    script = [sys.executable, '-c', code, str(path)]
    if os.geteuid() == 0:
        def drop():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
        proc = subprocess.run(script, preexec_fn=drop, capture_output=True)
    else:
        # Standard public Ubuntu hosted runners allow non-interactive sudo.
        proc = subprocess.run(['sudo', '-n', '-u', 'nobody', *script], capture_output=True)
    return proc.returncode


@contextmanager
def _windows_unauthorized_user():
    """Actual Windows access checks under a deny-only user SID (no admin needed).

    CreateRestrictedToken removes this process user's Allow SID and privileges.
    OS checks use the impersonation token; no fake path/access mock is used.
    """
    import ctypes
    from ctypes import wintypes

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('Sid', ctypes.c_void_p), ('Attributes', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    HANDLE = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = HANDLE
    kernel.CloseHandle.argtypes = [HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [HANDLE, wintypes.DWORD, ctypes.POINTER(HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.CreateRestrictedToken.argtypes = [HANDLE, wintypes.DWORD, wintypes.DWORD,
                                             ctypes.POINTER(SID_AND_ATTRIBUTES), wintypes.DWORD,
                                             ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                             ctypes.POINTER(HANDLE)]
    advapi.CreateRestrictedToken.restype = wintypes.BOOL
    advapi.ImpersonateLoggedOnUser.argtypes = [HANDLE]
    advapi.ImpersonateLoggedOnUser.restype = wintypes.BOOL
    advapi.RevertToSelf.restype = wintypes.BOOL

    token = HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x000E, ctypes.byref(token)):
        raise AssertionError('WINDOWS_TOKEN_SETUP_FAILED')
    restricted = HANDLE()
    try:
        count = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(count))
        if ctypes.get_last_error() != 122 or not count.value:
            raise AssertionError('WINDOWS_TOKEN_USER_QUERY_FAILED')
        buf = ctypes.create_string_buffer(count.value)
        if not advapi.GetTokenInformation(token, 1, buf, count, ctypes.byref(count)):
            raise AssertionError('WINDOWS_TOKEN_USER_READ_FAILED')
        sid = ctypes.cast(buf, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid
        disabled_sid = SID_AND_ATTRIBUTES(sid, 0)
        if not advapi.CreateRestrictedToken(token, 0x0001, 1,
                                            ctypes.byref(disabled_sid), 0, None, 0, None,
                                            ctypes.byref(restricted)):
            raise AssertionError('WINDOWS_RESTRICTED_TOKEN_CREATE_FAILED')
        try:
            def attempt(action):
                if not advapi.ImpersonateLoggedOnUser(restricted):
                    raise AssertionError('WINDOWS_IMPERSONATION_FAILED')
                try:
                    return action()
                finally:
                    if not advapi.RevertToSelf():
                        raise AssertionError('WINDOWS_REVERT_FAILED')
            yield attempt
        finally:
            kernel.CloseHandle(restricted)
    finally:
        kernel.CloseHandle(token)

def _windows_file_acl_info(path):
    """Read actual NTFS security descriptor and inherited ACE flags via Win32."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [('AceType', wintypes.BYTE), ('AceFlags', wintypes.BYTE),
                    ('AceSize', wintypes.WORD)]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [('AceCount', wintypes.DWORD), ('AclBytesInUse', wintypes.DWORD),
                    ('AclBytesFree', wintypes.DWORD)]

    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                              wintypes.DWORD, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_void_p,
                                              wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    fs_name = ctypes.create_unicode_buffer(64)
    volume = str(Path(path).anchor)
    if not kernel.GetVolumeInformationW(volume, None, 0, None, None, None,
                                        fs_name, len(fs_name)):
        raise AssertionError('WINDOWS_FILESYSTEM_CHECK_FAILED')
    if fs_name.value != 'NTFS':
        raise AssertionError('WINDOWS_NOT_NTFS')

    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD,
                                             wintypes.DWORD, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(wintypes.WORD),
                                                    ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    name_buffer = ctypes.create_unicode_buffer(str(path))
    code = advapi.GetNamedSecurityInfoW(name_buffer, 1, 0x4, None, None,
                                        ctypes.byref(dacl), None,
                                        ctypes.byref(descriptor))
    if code:
        raise AssertionError(f'WINDOWS_ACL_READ_FAILED_{code}')
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor,
                                                  ctypes.byref(control),
                                                  ctypes.byref(revision)):
            raise AssertionError('WINDOWS_ACL_CONTROL_READ_FAILED')
        if not dacl.value:
            raise AssertionError('WINDOWS_NULL_DACL')
        info = ACL_SIZE_INFORMATION()
        if not advapi.GetAclInformation(dacl, ctypes.byref(info),
                                        ctypes.sizeof(info), 2):
            raise AssertionError('WINDOWS_DACL_ACE_COUNT_FAILED')
        inherited = 0
        for idx in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, idx, ctypes.byref(ace)):
                raise AssertionError('WINDOWS_DACL_ACE_READ_FAILED')
            inherited += int(bool(ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents.AceFlags & 0x10))
        return {'protected': bool(control.value & 0x1000),
                'inherited_count': inherited, 'ace_count': info.AceCount}
    finally:
        kernel.LocalFree(descriptor)


class PrivateConfigurationSecurityContract(unittest.TestCase):
    def test_t1_create_then_initialize_application(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)/'private'
            target = directory/'config.private.json'
            cfg = _synthetic_private_config()
            migration.create_private_config(target, cfg)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), cfg)
            self.assertFalse(list(directory.glob('.config.private.*.tmp')))
            if os.name == 'nt':
                acl = _windows_file_acl_info(target)
                self.assertTrue(acl['protected'], 'WINDOWS_DACL_NOT_PROTECTED')
                self.assertEqual(acl['inherited_count'], 0, 'WINDOWS_INHERITED_ACES')
                self.assertGreater(acl['ace_count'], 0, 'WINDOWS_EMPTY_DACL')
            else:
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            env = {**os.environ, 'LOCAL_OPS_DATA_DIR':str(directory)}
            proc = subprocess.run([sys.executable, '-c',
                                   'import server; assert server.PRIVATE_CONFIG["top_book_exclusion"]["name"] == "fixture"'],
                                  cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(proc.returncode, 0, 'APPLICATION_CONFIG_INITIALIZATION_FAILED')

    def test_t2_interrupted_write_leaves_no_final_and_retry_works(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'private'/'config.private.json'
            cfg=_synthetic_private_config()
            def interrupt(fd, content):
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content[:8]); stream.flush()
                temporary = next(target.parent.glob('.config.private.*.tmp'))
                if os.name == 'nt':
                    acl = _windows_file_acl_info(temporary)
                    self.assertTrue(acl['protected'], 'INTERMEDIATE_WINDOWS_DACL_NOT_PROTECTED')
                    self.assertEqual(acl['inherited_count'], 0, 'INTERMEDIATE_WINDOWS_INHERITED_ACES')
                    self.assertGreater(acl['ace_count'], 0, 'INTERMEDIATE_WINDOWS_EMPTY_DACL')
                else:
                    self.assertEqual(temporary.stat().st_mode & 0o777, 0o600,
                                     'INTERMEDIATE_FILE_NOT_PRIVATE')
                raise InterruptedError('injected interruption (no private data)')
            with mock.patch.object(migration, '_write_and_check', side_effect=interrupt):
                with self.assertRaises(InterruptedError):
                    migration.create_private_config(target, cfg)
            self.assertFalse(target.exists(), 'INCOMPLETE_FINAL_CONFIG_PRESENT')
            self.assertEqual(list(target.parent.glob('.config.private.*.tmp')), [])
            migration.create_private_config(target, cfg)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), cfg)

    def test_t3_existing_valid_configuration_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'config.private.json'
            migration.create_private_config(target, _synthetic_private_config('first'))
            before=target.read_bytes()
            with self.assertRaises(FileExistsError):
                migration.create_private_config(target, _synthetic_private_config('different'))
            self.assertEqual(target.read_bytes(), before)
            self.assertFalse(list(target.parent.glob('.config.private.*.tmp')))

    def test_t4_real_unauthorized_identity_cannot_read(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'config.private.json'
            migration.create_private_config(target, _synthetic_private_config())
            if os.name == 'nt':
                with _windows_unauthorized_user() as attempt:
                    # Use actual OS ACL enforcement, not an access-check mock.
                    with self.assertRaises(PermissionError):
                        attempt(lambda: target.read_bytes())
            else:
                Path(d).chmod(0o711)  # Permit traversal; isolate file's 0600 mode.
                code=('from pathlib import Path;import sys;'
                      'p=Path(sys.argv[1]);'
                      '\ntry:p.read_bytes()\nexcept PermissionError:sys.exit(42)'
                      '\nexcept Exception:sys.exit(43)\nelse:sys.exit(0)')
                self.assertEqual(_run_as_unauthorized_posix(code,target), 42,
                                 'UNAUTHORIZED_READ_NOT_DENIED_AT_FILE')

    def test_t5_real_permission_failure_cannot_publish(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)/'private'
            directory.mkdir(mode=0o700)
            target=directory/'config.private.json'
            if os.name == 'nt':
                with _windows_unauthorized_user() as attempt:
                    with self.assertRaises(PermissionError):
                        attempt(lambda: migration.create_private_config(target, _synthetic_private_config()))
            else:
                code=('import sys;from pathlib import Path;'
                      'sys.path.insert(0,sys.argv[2]);'
                      'from private_config_from_baseline import create_private_config;'
                      '\ntry:create_private_config(Path(sys.argv[1]),{"x":"synthetic"})'
                      '\nexcept PermissionError:sys.exit(42)'
                      '\nexcept Exception:sys.exit(43)\nelse:sys.exit(0)')
                # The other identity has no write access to the parent directory.
                script=[sys.executable,'-c',code,str(target),str(ROOT/'tools')]
                if os.geteuid()==0:
                    def drop():os.setgroups([]);os.setgid(65534);os.setuid(65534)
                    proc=subprocess.run(script,preexec_fn=drop,capture_output=True)
                else:
                    proc=subprocess.run(['sudo','-n','-u','nobody',*script],capture_output=True)
                self.assertEqual(proc.returncode, 42, 'PERMISSION_FAILURE_NOT_REJECTED')
            self.assertFalse(target.exists(), 'FALSE_SUCCESSFUL_FINAL_CONFIG')

    def test_t6_two_competing_processes_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'config.private.json'
            ctx=multiprocessing.get_context('spawn')
            event=ctx.Event(); queue=ctx.Queue()
            processes=[ctx.Process(target=_racing_config_worker,
                                   args=(str(target),v,event,queue)) for v in ('A','B')]
            for p in processes:p.start()
            event.set()
            for p in processes:
                p.join(timeout=15)
                if p.is_alive():p.terminate();p.join();self.fail('CREATION_PROCESS_HUNG')
                self.assertEqual(p.exitcode, 0, 'CREATION_PROCESS_CRASHED')
            self.assertEqual(sorted([queue.get(timeout=3) for _ in processes]), ['CREATED','EXISTS'])
            self.assertIn(json.loads(target.read_text('utf-8'))['top_book_exclusion']['manager'], ('A','B'))
            self.assertFalse(list(target.parent.glob('.config.private.*.tmp')))
