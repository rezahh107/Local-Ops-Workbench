"""One local/agent/CI entry point. Exit nonzero on any required failure."""
import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]

def run(*args):
    print('RUN', ' '.join(args),flush=True)
    subprocess.run([sys.executable,*args],cwd=ROOT,check=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--browser',action='store_true',help='Require real Chromium browser checks (no silent SKIP)')
    args=ap.parse_args()
    run('tools/privacy_gate.py')
    run('-m','unittest','discover','-s','tests','-p','test_*.py','-v')
    with TemporaryDirectory(prefix='localops-candidate-') as temp:
        path=str(Path(temp)/'candidate.zip')
        run('tools/package_candidate.py','--output',path)
        run('tools/smoke_candidate.py',path)
    if args.browser:
        run('tests/browser_contract.py')
    print('LOCAL_CONTRACT_VERIFIED; browser=', 'executed' if args.browser else 'NOT_RUN')

if __name__=='__main__':main()
