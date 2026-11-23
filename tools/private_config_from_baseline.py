"""Extract private configuration only on the owner's device; never upload its output.

The final path is published via a same-directory, no-replace hard link only after
an exclusively created, owner-restricted temporary file is complete and checked.
"""
import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from zipfile import ZipFile

EXPECTED_SHA256 = '3f092528ee612827eb83734e32964f2a025cf99b0179e6db08b0df27b6fc8e51'


def from_baseline(archive):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise ValueError('Original baseline ZIP identity mismatch: refusing private extraction')
    with ZipFile(archive) as z:
        source = z.read('server.py').decode('utf-8')
        html = z.read('index.template.html').decode('utf-8')
    constants = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in {
                    'INTELLIGENCE_ELIGIBLE', 'SUPPORTERS_SHA256',
                    'SUPPORTER_MANAGER_ALIAS', 'SUPPORTER_EXCLUDED', 'SUPPORTER_SHEETS',
                }:
                    constants[target.id] = ast.literal_eval(statement.value)
    required = {'INTELLIGENCE_ELIGIBLE', 'SUPPORTERS_SHA256',
                'SUPPORTER_MANAGER_ALIAS', 'SUPPORTER_EXCLUDED', 'SUPPORTER_SHEETS'}
    if set(constants) != required:
        raise ValueError('Baseline layout differs: private config extraction was not qualified')
    block = re.search(r'const TOP_HISTORICAL_RECORDS=Object\.freeze\(\[(.*?)\]\.map', html, re.S)
    if not block:
        raise ValueError('Historical data block missing')
    entries = re.findall(r"\{name:'((?:[^'\\]|\\.)*)',score:([0-9]+(?:\.[0-9]+)?)\}",block.group(1))
    if len(entries) != 7:
        raise ValueError('Historical record count differs from approved baseline')
    if any('\\' in name for name, _ in entries):
        raise ValueError('Escaped JS names require manual review')
    exclusion = re.search(r"function topBookExcluded\(metric,record\)\{return metric==='کتاب'&&record\.manager==='([^']+)'&&record\.name==='([^']+)';\}",html)
    if not exclusion:
        raise ValueError('Book exclusion not identified')
    return {
        'intelligence_eligible': list(constants['INTELLIGENCE_ELIGIBLE']),
        'supporters': {
            'sha256': constants['SUPPORTERS_SHA256'],
            'manager_alias': constants['SUPPORTER_MANAGER_ALIAS'],
            'excluded': sorted(constants['SUPPORTER_EXCLUDED']),
            'sheets': list(constants['SUPPORTER_SHEETS']),
        },
        'top_book_exclusion': {'manager':exclusion.group(1),'name':exclusion.group(2)},
        'historical_supporters': [{'name':n,'score':float(score)} for n,score in entries],
    }


def _secure_new_file(path):
    """Return a binary write FD with access restricted from the creation syscall."""
    if os.name != 'nt':
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd

    # Python's chmod(0o600) does not establish a Windows DACL. CreateFileW
    # accepts a protected explicit DACL *at creation*, not after writing secrets.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    HANDLE = wintypes.HANDLE

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('Sid', ctypes.c_void_p), ('Attributes', wintypes.DWORD)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('nLength', wintypes.DWORD),
                    ('lpSecurityDescriptor', ctypes.c_void_p),
                    ('bInheritHandle', wintypes.BOOL)]

    kernel.GetCurrentProcess.restype = HANDLE
    advapi.OpenProcessToken.argtypes = [HANDLE, wintypes.DWORD, ctypes.POINTER(HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
                                   wintypes.DWORD, HANDLE]
    kernel.CreateFileW.restype = HANDLE
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(wintypes.BOOL),
                                                 ctypes.POINTER(ctypes.c_void_p),
                                                 ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetSecurityInfo.argtypes = [HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetSecurityInfo.restype = wintypes.DWORD

    token = HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        count = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(count))
        if ctypes.get_last_error() != 122 or not count.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(count.value)
        if not advapi.GetTokenInformation(token, 1, buffer, count,
                                          ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid
        sid_string = ctypes.c_void_p()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid_text = ctypes.wstring_at(sid_string)
        finally:
            kernel.LocalFree(sid_string)
    finally:
        kernel.CloseHandle(token)

    # D:P is protected against inheritable directory ACEs. SYSTEM and the
    # actual calling user SID are the only principals granted file access.
    security_descriptor = ctypes.c_void_p()
    sddl = f'D:P(A;;FA;;;SY)(A;;FA;;;{sid_text})'
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(security_descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(security_descriptor,
                                                ctypes.byref(present), ctypes.byref(dacl),
                                                ctypes.byref(defaulted)) or not present.value or not dacl.value:
            raise OSError('Explicit private DACL unavailable')
        attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES),
                                    security_descriptor, False)
        # CreateFileW applies the owner/SYSTEM-only DACL from the creation syscall.
        # WRITE_DAC permits protection against any later inherited ACEs while
        # the file is still EMPTY. No confidential bytes exist before this step.
        handle = kernel.CreateFileW(str(path), 0x40040000, 0,
                                    ctypes.byref(attrs), 1, 0x80, None)
        if handle == HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # NTFS can leave a newly assigned DACL unprotected even when the
            # supplied SDDL contains D:P. Reapply the SAME restrictive ACL and
            # prohibit inheritance before writing the first private byte.
            rc = advapi.SetSecurityInfo(handle, 1, 0x80000004,
                                        None, None, dacl, None)
            if rc:
                raise OSError(rc, 'Private Windows ACL protection failed')
            return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            # The file contains zero confidential bytes on every error here.
            path.unlink()
            raise
    finally:
        kernel.LocalFree(security_descriptor)


def _write_and_check(fd, content):
    """A separate hook permits deterministic interruption tests of actual flow."""
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def create_private_config(destination, config):
    """Create only a verified complete config; never replace an existing one.

    Raises on every failure, including a concurrent winner or cleanup failure.
    The caller may retry only if destination is still absent.
    """
    destination = Path(destination)
    if not isinstance(config, dict):
        raise ValueError('Private configuration must be a JSON object')
    # Validate data before any filesystem mutation. Do not log config contents.
    content = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if json.loads(content) != config:
        raise ValueError('Private configuration validation failed')
    if os.path.lexists(destination):
        raise FileExistsError('REFUSED: private configuration already exists')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name('.config.private.' + secrets.token_hex(16) + '.tmp')
    created = False
    try:
        fd = _secure_new_file(temporary)
        created = True
        _write_and_check(fd, content)
        # No final path can appear until data and descriptor exist and close.
        if temporary.read_bytes() != content or json.loads(temporary.read_text('utf-8')) != config:
            raise ValueError('Private configuration verification failed')
        if os.name != 'nt' and stat.S_IMODE(temporary.stat().st_mode) != 0o600:
            raise PermissionError('Private temporary file permissions were not enforced')
        # Same-directory hard link: all-or-nothing publication without replace.
        # Windows/NTFS and POSIX reject the operation when destination exists.
        os.link(temporary, destination)
    finally:
        if created:
            # A cleanup error is not success: the temp is private, but retained.
            temporary.unlink()


def main():
    ap = argparse.ArgumentParser(description='Prepare private config locally from exact original ZIP; never upload output')
    ap.add_argument('original_zip', type=Path)
    ap.add_argument('--data-dir', type=Path, required=True)
    ap.add_argument('--write', action='store_true', help='Explicitly create new private config; never overwrite')
    args = ap.parse_args()
    try:
        cfg = from_baseline(args.original_zip)
        destination = args.data_dir.expanduser().resolve() / 'config.private.json'
        if os.path.lexists(destination):
            raise FileExistsError('REFUSED: private configuration already exists')
        if not args.write:
            print('DRY RUN: exact source verified; private config can be created in the requested external data directory')
            return
        create_private_config(destination, cfg)
    except (OSError, ValueError, TypeError) as exc:
        # Paths/exception payloads can contain private information; do not print.
        raise SystemExit('FAILED: private config not safely created (' + type(exc).__name__ + ')') from None
    print('CREATED private config outside source tree (not in VCS). No Excel/SQLite files modified.')


if __name__ == '__main__':
    main()
