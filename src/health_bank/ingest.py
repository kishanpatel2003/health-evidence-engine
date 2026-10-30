"""Import downloaded CDA/XML/ZIP files without contacting a medical portal."""
import argparse
import json
from pathlib import Path
from .cda import import_cda, import_package
from .store import Store


def import_jsonl(root,path,source_system='local-import'):
    """Land new, already structured assertions through the same evidence store."""
    path=Path(path)
    store=Store(root)
    receipt=store.add_artifact(path,source_system,kind='structured-jsonl')
    captured_path=store.root/receipt['local_path']
    records=[(number,json.loads(line)) for number,line in enumerate(captured_path.read_text(encoding='utf-8').splitlines(),1) if line.strip()]
    count=0
    for number,record in records:
        record=dict(record)
        source=dict(record.get('source',{}))
        source.setdefault('system',source_system)
        source.setdefault('artifact_sha256',receipt['artifact_sha256'])
        source.setdefault('locator',f'line:{number}')
        record['source']=source
        store.add_record(record)
        count+=1
    return {'records_processed':count,'artifact_sha256':receipt['artifact_sha256']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default=".")
    parser.add_argument("--patient-id",default="patient-001")
    sub=parser.add_subparsers(dest="command",required=True)
    cda=sub.add_parser("cda")
    cda.add_argument("path")
    cda.add_argument("--source-url")
    structured=sub.add_parser('jsonl')
    structured.add_argument('path')
    structured.add_argument('--source-system',default='local-import')
    args=parser.parse_args()
    path=Path(args.path)
    if args.command=='jsonl':
        result=import_jsonl(args.root,path,args.source_system)
    else:
        importer=import_package if path.suffix.lower()==".zip" else import_cda
        result=importer(args.root,path,args.patient_id,args.source_url)
    print(json.dumps(result,sort_keys=True))


if __name__=="__main__":
    main()
