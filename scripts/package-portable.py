"""Assemble target-native runtimes/wheels in Docker without running target code."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile


def fetch(url, path=None):
    request=urllib.request.Request(url,headers={"User-Agent":"AirFrame-build/0.1"})
    with urllib.request.urlopen(request,timeout=90) as response:
        if path:
            with path.open('wb') as target:shutil.copyfileobj(response,target)
            return path
        return response.read()


parser=argparse.ArgumentParser()
parser.add_argument('--target',choices=['macos-arm64','macos-x64','windows-x64'],required=True)
args=parser.parse_args()
work=Path('/portable-work');work.mkdir(exist_ok=True)
out=Path('/portable-out');out.mkdir(exist_ok=True)
wheel=next(Path('/out').glob('airframe-*.whl'))
manifest={'target':args.target,'application':hashlib.sha256(wheel.read_bytes()).hexdigest(),'wheels':{}}

if args.target.startswith('macos'):
    arch='aarch64' if args.target.endswith('arm64') else 'x86_64'
    release=json.loads(fetch('https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'))
    asset=next(a for a in release['assets'] if re.match(r'cpython-3\.14\.\d+\+',a['name'])
               and a['name'].endswith(f'-{arch}-apple-darwin-install_only.tar.gz'))
    archive=fetch(asset['browser_download_url'],work/'runtime.tar.gz')
    manifest['runtime']={'url':asset['browser_download_url'],'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    digest=asset.get('digest','')
    if digest.startswith('sha256:') and manifest['runtime']['sha256']!=digest[7:]:raise RuntimeError('Runtime digest mismatch')
    root=work/'AirFrame.app'/'Contents'
    resources=root/'Resources';resources.mkdir(parents=True)
    with tarfile.open(archive) as tar:tar.extractall(resources,filter='data')
    runtime=resources/'python'
    site=runtime/'lib'/'python3.14'/'site-packages'
    platform='macosx_14_0_arm64' if arch=='aarch64' else 'macosx_14_0_x86_64'
    (root/'MacOS').mkdir()
    launcher=root/'MacOS'/'AirFrame'
    launcher.write_text('#!/bin/sh\nset -eu\nAF_RES="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)"\nexec "$AF_RES/python/bin/python3.14" -I "$AF_RES/app-entry.py" "$@"\n')
    launcher.chmod(0o755)
    (resources/'app-entry.py').write_text('from airframe.desktop import main\nmain()\n')
    with (root/'Info.plist').open('wb') as output:
        plistlib.dump({'CFBundleName':'AirFrame','CFBundleDisplayName':'AirFrame','CFBundleIdentifier':'video.airframe.desktop',
                      'CFBundleVersion':'1','CFBundleShortVersionString':'0.3.0','CFBundleExecutable':'AirFrame',
                      'CFBundlePackageType':'APPL','NSHighResolutionCapable':True,'LSMinimumSystemVersion':'14.0',
                      'NSLocalNetworkUsageDescription':'AirFrame принимает экран вашего Android по локальной сети.'},output)
else:
    listing=fetch('https://www.python.org/ftp/python/').decode()
    versions=sorted(set(re.findall(r'href="(3\.14\.\d+)/"',listing)),key=lambda s:tuple(map(int,s.split('.'))),reverse=True)
    archive=work/'runtime.zip'
    for version in versions:
        url=f'https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip'
        try:fetch(url,archive);break
        except urllib.error.HTTPError as exc:
            if exc.code!=404:raise
    else:raise RuntimeError('No official Windows embedded Python runtime found')
    manifest['runtime']={'url':url,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    runtime=work/'AirFrame';runtime.mkdir()
    with zipfile.ZipFile(archive) as package:package.extractall(runtime)
    site=runtime/'Lib'/'site-packages'
    (runtime/'python314._pth').write_text('python314.zip\n.\nLib/site-packages\nimport site\n')
    (runtime/'app-entry.py').write_text('from airframe.desktop import main\nmain()\n')
    subprocess.run(['x86_64-w64-mingw32-gcc','-municode','-mwindows','-O2','-s',
                    '/scripts/windows-launcher.c','-o',str(runtime/'AirFrame.exe')],check=True)
    platform='win_amd64'

downloads=work/'wheels'
subprocess.run([sys.executable,'-m','pip','download','--only-binary=:all:',
                '--platform',platform,'--python-version','3.14','--implementation','cp','--abi','cp314',
                '--dest',str(downloads),str(wheel)],check=True)
site.mkdir(parents=True,exist_ok=True)
for dependency in downloads.glob('*.whl'):
    manifest['wheels'][dependency.name]=hashlib.sha256(dependency.read_bytes()).hexdigest()
    with zipfile.ZipFile(dependency) as package:package.extractall(site)
shutil.copy('/README.md',runtime/'AIRFRAME-README.md')
(runtime/'build-manifest.json').write_text(json.dumps(manifest,indent=2))
if args.target.startswith('macos'):
    with tarfile.open(out/f'AirFrame-{args.target}.tar.gz','w:gz') as archive:
        archive.add(work/'AirFrame.app',arcname='AirFrame.app')
else:
    shutil.make_archive(str(out/f'AirFrame-{args.target}'),'zip',work,'AirFrame')
print('Portable native distribution assembled:',args.target,flush=True)
