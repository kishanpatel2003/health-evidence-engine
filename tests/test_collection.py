import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from health_bank.collection import import_capture_state, import_document_manifest
from health_bank.store import Store


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.local=self.root/'.local'
        self.local.mkdir()

    def test_capture_state_is_parsed_from_saved_receipt(self):
        path=self.local/'state.json'
        state={'updated_at':'2026-10-22T00:00:00Z','surfaces':[{'surface':'synthetic','status':'not_captured','label':'original'}]}
        path.write_text(json.dumps(state))
        original_add=Store.add_artifact
        def change_then_capture(store,source,*arguments,**keywords):
            if Path(source)==path:
                state['surfaces'][0]['label']='changed before capture'
                path.write_text(json.dumps(state))
            return original_add(store,source,*arguments,**keywords)
        with patch.object(Store,'add_artifact',change_then_capture):
            import_capture_state(self.root,path)
        self.assertEqual(next(Store(self.root).iter_records())['data']['label'],'changed before capture')

    def test_document_manifest_uses_preserved_lines_and_native_source_key(self):
        source=self.local/'original.txt'
        source.write_text('synthetic original document')
        path=self.local/'manifest.jsonl'
        entry={'local_path':str(source),'document_id':'capture-label','source_record_key':'native-source-key'}
        path.write_text('\n'+json.dumps(entry)+'\n')
        original_add=Store.add_artifact
        def capture_then_change(store,original,*arguments,**keywords):
            receipt=original_add(store,original,*arguments,**keywords)
            if Path(original)==path:
                path.write_text('')
            return receipt
        with patch.object(Store,'add_artifact',capture_then_change):
            result=import_document_manifest(self.root,path)
        self.assertEqual(result['captured_documents'],1)
        record=next(Store(self.root).iter_records())
        self.assertEqual(record['source']['record_key'],'native-source-key')
        self.assertEqual(record['source']['locator'],'manifest-line:2')

    def test_html_document_extracts_verified_original_after_download_changes(self):
        source=self.local/'document.html'
        payload=b'<html><body><h1>Original synthetic document</h1></body></html>'
        source.write_bytes(payload)
        path=self.local/'manifest.jsonl'
        path.write_text(json.dumps({'local_path':str(source),'sha256':hashlib.sha256(payload).hexdigest()})+'\n')
        original_add=Store.add_artifact
        def capture_then_change(store,original,*arguments,**keywords):
            receipt=original_add(store,original,*arguments,**keywords)
            if Path(original)==source:
                source.write_bytes(b'<html><body>Changed synthetic document</body></html>')
            return receipt
        with patch.object(Store,'add_artifact',capture_then_change):
            import_document_manifest(self.root,path)
        surface=next(record for record in Store(self.root).iter_records() if record['kind']=='portal_surface')
        self.assertEqual(surface['data']['label'],'Original synthetic document')
        self.assertTrue(Store(self.root).verify()['verified'])


if __name__=='__main__':
    unittest.main()
