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

    def test_private_config_migration_is_nonoverwriting(self):
        tool=ROOT/'tools/private_config_from_baseline.py'
        text=tool.read_text(encoding='utf8')
        self.assertIn("destination.open('x'",text)
        self.assertIn("EXPECTED_SHA256",text)


if __name__=='__main__':unittest.main()
