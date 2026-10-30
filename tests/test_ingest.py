import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from health_bank.ingest import import_jsonl
from health_bank.store import Store


class StructuredIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.path=self.root/'synthetic.jsonl'

    def record(self,value):
        return {'patient_id':'synthetic-1','lane':'documents','kind':'synthetic','data':{'value':value}}

    def test_blank_lines_do_not_shift_precise_source_locator(self):
        self.path.write_text(json.dumps(self.record('first'))+'\n\n'+json.dumps(self.record('second'))+'\n')
        import_jsonl(self.root,self.path)
        records=list(Store(self.root).iter_records())
        self.assertEqual([record['source']['locator'] for record in records],['line:1','line:3'])

    def test_parse_captured_jsonl_if_original_changes(self):
        self.path.write_text(json.dumps(self.record('original'))+'\n')
        original_add=Store.add_artifact
        def capture_then_change(store,path,*arguments,**keywords):
            receipt=original_add(store,path,*arguments,**keywords)
            if Path(path)==self.path:
                self.path.write_text(json.dumps(self.record('changed'))+'\n')
            return receipt
        with patch.object(Store,'add_artifact',capture_then_change):
            import_jsonl(self.root,self.path)
        self.assertEqual(next(Store(self.root).iter_records())['data']['value'],'original')


if __name__=='__main__':
    unittest.main()
