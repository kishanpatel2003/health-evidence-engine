"""Ingest a browser capture manifest and persist explicit coverage observations."""
import argparse
import json
from pathlib import Path

from .store import Store, utc_now
from .portal import import_html

SYSTEM='atlantic-health-mychart'


def import_document_manifest(root,path,patient_id='patient-001'):
    store=Store(root)
    path=Path(path)
    receipt=store.add_artifact(path,SYSTEM,kind='document-capture-manifest')
    captured_path=store.root/receipt['local_path']
    entries=[(number,json.loads(line)) for number,line in enumerate(captured_path.read_text(encoding='utf-8').splitlines(),1) if line.strip()]
    captured=0
    for number,entry in entries:
        source_path=Path(entry['local_path'])
        if not source_path.is_absolute():
            source_path=store.root/source_path
        source_path=source_path.resolve()
        if not source_path.is_relative_to(store.local.resolve()):
            raise ValueError('Capture files must be within the local vault workspace')
        original=store.add_artifact(source_path,SYSTEM,entry.get('source_url'),'portal-document')
        expected=entry.get('sha256')
        if expected and original['artifact_sha256']!=expected:
            raise ValueError('Captured document hash differs from its manifest')
        source={'system':SYSTEM,'artifact_sha256':original['artifact_sha256'],
                'locator':entry.get('source_locator') or f'manifest-line:{number}',
                # Local inventory positions and filenames are capture labels,
                # not portal identities. Only an explicitly supplied native
                # source key may join revisions across collection runs.
                'record_key':entry.get('source_record_key')}
        store.add_record({'patient_id':patient_id,'lane':'documents','kind':'portal_document',
            'source':source,'captured_at':entry.get('captured_at') or utc_now(),
            'data':{'label':entry.get('portal_title'),'capture':entry,
                    'manifest_artifact_sha256':receipt['artifact_sha256'],
                    'text_extraction_status':'not_performed'}})
        if source_path.suffix.lower() in {'.html','.htm'}:
            # Signed original HTML can lack an application main landmark; keep
            # the verified original even when it needs a dedicated extractor.
            try:
                import_html(root,store.root/original['local_path'],'documents',patient_id,entry.get('source_url'),document_body=True)
            except ValueError as error:
                store.add_record({'patient_id':patient_id,'lane':'coverage','kind':'document_extraction_gap',
                    'source':source,'status':'captured_raw',
                    'data':{'label':'Original retained; text extraction needs a document adapter',
                            'document_id':entry.get('document_id'),'reason':str(error)}})
        captured+=1
    return {'manifest_entries':len(entries),'captured_documents':captured}


def import_capture_state(root,path,patient_id='patient-001'):
    store=Store(root)
    path=Path(path)
    receipt=store.add_artifact(path,SYSTEM,kind='collection-coverage')
    state=json.loads((store.root/receipt['local_path']).read_text(encoding='utf-8'))
    for number,surface in enumerate(state['surfaces'],1):
        store.add_record({'patient_id':patient_id,'lane':'coverage','kind':'capture_coverage',
            'source':{'system':SYSTEM,'artifact_sha256':receipt['artifact_sha256'],
                      'locator':f'/surfaces/{number-1}','record_key':surface['surface']},
            'captured_at':state['updated_at'],'status':surface['status'],'data':surface})
    return {'coverage_surfaces':len(state['surfaces'])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='.')
    parser.add_argument('--patient-id',default='patient-001')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['documents','coverage']:
        child=sub.add_parser(name)
        child.add_argument('path')
    args=parser.parse_args()
    importer=import_document_manifest if args.command=='documents' else import_capture_state
    print(json.dumps(importer(args.root,args.path,args.patient_id),sort_keys=True))


if __name__=='__main__':
    main()
