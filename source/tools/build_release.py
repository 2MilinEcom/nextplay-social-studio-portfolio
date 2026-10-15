"""Build a code-only release with a positive file allowlist and verified hashes."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
ROOT=Path(__file__).resolve().parents[1]
TOP='nextplay-v4.0.2'
DOCS={'README.md','INSTALL.md','RELEASE_NOTES.md','TEST_STATUS.md','VERSION'}
FORBIDDEN={'.env','.venv','venv','node_modules','__pycache__','products','downloads','raw','rendered','ads','nextplay','browser_profile','chrome_profile','test-run'}

def files(root=ROOT):
    selected=[]
    for path in root.rglob('*'):
        rel=path.relative_to(root)
        if any(p in FORBIDDEN or p.startswith('.env') for p in rel.parts):continue
        if path.is_symlink():continue
        if not path.is_file():continue
        if len(rel.parts)==1:
            allowed=path.name in DOCS or path.suffix=='.py' or path.name.startswith('requirements') and path.suffix=='.txt' or path.name=='OPEN_NEXTPLAY_V4.command'
        else:
            allowed=(rel.parts[0] in {'pipeline_core','distutils','tests','tools'} and path.suffix in {'.py','.js','.css','.html'}) or rel.as_posix()=='testdata/elroel_shop_fixture.html'
        if allowed:
            body=path.read_bytes()
            if re.search(rb'sk-(?:proj-)?[A-Za-z0-9_-]{32,}',body):raise ValueError('Credential-like content: '+rel.as_posix())
            selected.append((rel.as_posix(),body,path.stat().st_mode))
    return sorted(selected)

def build(target):
    entries=files()
    manifest={name:hashlib.sha256(body).hexdigest() for name,body,_ in entries}
    with ZipFile(target,'x',ZIP_DEFLATED) as archive:
        from zipfile import ZipInfo
        for name,body,mode in entries:
            info=ZipInfo(TOP+'/'+name);info.external_attr=(mode&0xffff)<<16;info.compress_type=ZIP_DEFLATED
            archive.writestr(info,body)
        archive.writestr(TOP+'/PACKAGE_MANIFEST.json',json.dumps(manifest,indent=2))
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        for name,sha in manifest.items():assert hashlib.sha256(archive.read(TOP+'/'+name)).hexdigest()==sha
    sha=hashlib.sha256(Path(target).read_bytes()).hexdigest()
    Path(str(target)+'.sha256').write_text(sha+'  '+Path(target).name+'\n')
    print(json.dumps({'zip':str(target),'files':len(entries)+1,'sha256':sha}))
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args();build(args.output.resolve())
