"""Refresh documentation in assembled archives; run in Docker with /packages mounted."""
from pathlib import Path
import io
import tarfile
import zipfile

content=Path('/README.md').read_bytes()
for path in Path('/packages').glob('AirFrame-*'):
    temporary=path.with_name(path.name+'.tmp')
    if path.name.endswith('.tar.gz'):
        with tarfile.open(path,'r:gz') as source,tarfile.open(temporary,'w:gz') as target:
            for item in source:
                data=source.extractfile(item) if item.isfile() else None
                if item.name.endswith('/AIRFRAME-README.md'):
                    item.size=len(content);data=io.BytesIO(content)
                target.addfile(item,data)
    elif path.suffix=='.zip':
        with zipfile.ZipFile(path) as source,zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data=content if item.filename.endswith('/AIRFRAME-README.md') else source.read(item)
                target.writestr(item,data)
    else:
        continue
    temporary.replace(path)
    print('Updated package documentation:',path.name)
