import json
import io
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from health_bank.cda import import_cda, import_package, time_value
from health_bank.store import Store

SAMPLE='''<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<id root="doc" extension="one"/><title>Synthetic Summary</title><effectiveTime value="20261022123000-0400"/>
<recordTarget><patientRole><id root="synthetic" extension="example"/><patient><name>Example Patient</name></patient></patientRole></recordTarget>
<component><structuredBody><component><section><code code="results"/><title>Results</title>
<text><table><tbody><tr ID="test-row"><td>Example test</td><td>0 units</td></tr></tbody></table></text>
<entry><organizer><id root="panel"/><component><observation><id root="result-one"/>
<code code="synthetic-code" displayName="Synthetic measurement"/><text><reference value="#test-row"/></text>
<statusCode code="completed"/><effectiveTime value="202601"/><value xsi:type="PQ" value="0" unit="synthetic"/>
<referenceRange><observationRange><text>Synthetic range only</text></observationRange></referenceRange>
</observation></component><component><observation><id root="result-two"/><code code="unknown"/>
<value xsi:type="PQ" nullFlavor="UNK"/></observation></component></organizer></entry>
</section></component><component><section><title>Unrecognized clinical section</title><text>Keep this narrative.</text></section></component>
</structuredBody></component></ClinicalDocument>'''


class CDATests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source=self.root/'synthetic.xml'
        self.source.write_text(SAMPLE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lossless_values_references_nulls_and_dates(self):
        report=import_cda(self.root,self.source)
        records=list(Store(self.root).iter_records())
        observations=[r for r in records if r['kind']=='clinical_observation']
        self.assertEqual(len(observations),2)
        zero=next(r for r in observations if r['data']['values'][0].get('value')=='0')
        self.assertEqual(zero['data']['values'][0]['unit'],'synthetic')
        self.assertEqual(zero['effective_time']['start'],'2026-01')
        self.assertEqual(zero['effective_time']['precision'],'month')
        self.assertIn('Example test 0 units',zero['data']['referenced_text'])
        self.assertTrue(zero['data']['reference_ranges'])
        missing=next(r for r in observations if r['data']['values'][0].get('nullFlavor'))
        self.assertNotIn('value',missing['data']['values'][0])
        self.assertNotEqual(zero['source']['locator'],missing['source']['locator'])
        self.assertTrue(any(r['data'].get('text')=='Keep this narrative.' for r in records))
        self.assertEqual(report['records_processed'],len(records))
        self.assertEqual(Store(self.root).verify()['verified'],True)

    def test_repeat_import_is_idempotent(self):
        import_cda(self.root,self.source)
        before=list(Store(self.root).iter_records())
        import_cda(self.root,self.source)
        self.assertEqual(before,list(Store(self.root).iter_records()))

    def test_partial_time_and_unknown_timezone_remain_explicit(self):
        self.assertEqual(time_value('2026')['start'],'2026')
        self.assertEqual(time_value('20260102')['start'],'2026-01-02')
        self.assertEqual(time_value('20260102123000-0400')['start'],'2026-01-02T12:30:00-04:00')
        self.assertEqual(time_value('20261399')['precision'],'invalid')
        self.assertIsNone(time_value('unknown')['start'])

    def test_reject_entities_and_zip_traversal(self):
        self.source.write_text('<!DOCTYPE x [<!ENTITY x "value">]><ClinicalDocument/>')
        with self.assertRaises(ValueError):
            import_cda(self.root,self.source)
        zipped=self.root/'unsafe.zip'
        with zipfile.ZipFile(zipped,'w') as archive:
            archive.writestr('../escaped.txt','bad')
        with self.assertRaises(ValueError):
            import_package(self.root,zipped)
        self.assertFalse((self.root.parent/'escaped.txt').exists())

    def test_package_preserves_binary_and_all_cda_members(self):
        zipped=self.root/'synthetic.zip'
        with zipfile.ZipFile(zipped,'w') as archive:
            archive.writestr('MachineReadable_XDMFormat/document.xml',SAMPLE)
            archive.writestr('HumanReadable/example.pdf',b'%PDF-synthetic')
        result=import_package(self.root,zipped)
        self.assertEqual(result['cda_documents'],1)
        self.assertEqual(result['members'],2)
        self.assertTrue(any(r['kind']=='export_member' for r in Store(self.root).iter_records()))

    def test_utf16_dtd_rejected_but_original_preserved(self):
        payload='<!DOCTYPE ClinicalDocument [<!ENTITY x "value">]><ClinicalDocument xmlns="urn:hl7-org:v3"/>'.encode('utf-16')
        self.source.write_bytes(payload)
        with self.assertRaises(ValueError):
            import_cda(self.root,self.source)
        originals=list((self.root/'.local'/'raw').glob('*/*'))
        self.assertEqual(len(originals),1)
        self.assertEqual(originals[0].read_bytes(),payload)

    def test_identical_unidentified_events_keep_separate_locations(self):
        sample=SAMPLE.replace('<id root="doc" extension="one"/>','').replace('<id root="result-one"/>','').replace('<id root="result-two"/>','')
        self.source.write_text(sample)
        import_cda(self.root,self.source)
        records=[r for r in Store(self.root).iter_records() if r['kind']=='clinical_observation']
        self.assertEqual(len({r['source']['record_key'] for r in records}),2)
        self.assertTrue(all(r['source']['record_key'].startswith(tuple('0123456789abcdef')) for r in records))

    def test_null_flavored_identifiers_do_not_collapse_identical_assertions(self):
        observation='<observation><id nullFlavor="NI"/><code code="synthetic"/><value value="1"/></observation>'
        sample='<ClinicalDocument xmlns="urn:hl7-org:v3"><id nullFlavor="NI"/><component><structuredBody><component><section><title>Results</title><entry>'+observation+'</entry><entry>'+observation+'</entry></section></component></structuredBody></component></ClinicalDocument>'
        self.source.write_text(sample)
        result=import_cda(self.root,self.source)
        records=list(Store(self.root).iter_records())
        entries=[record for record in records if record['kind']=='cda_entry']
        self.assertEqual(len(entries),2)
        self.assertEqual(len({record['source']['record_key'] for record in entries}),2)
        document=next(record for record in records if record['kind']=='cda_document')
        self.assertEqual(document['source']['record_key'],result['artifact_sha256'])

    def test_parser_uses_exact_captured_xml_when_input_changes(self):
        original_add=Store.add_artifact
        changed=SAMPLE.replace('Synthetic Summary','Changed synthetic summary')
        def change_then_capture(store,path,*arguments,**keywords):
            if Path(path)==self.source:
                self.source.write_text(changed)
            return original_add(store,path,*arguments,**keywords)
        with patch.object(Store,'add_artifact',change_then_capture):
            result=import_cda(self.root,self.source)
        document=next(record for record in Store(self.root).iter_records() if record['kind']=='cda_document')
        self.assertEqual(document['data']['document_title'],'Changed synthetic summary')
        self.assertEqual(document['source']['artifact_sha256'],result['artifact_sha256'])

    def test_package_reads_preserved_zip_if_download_path_changes(self):
        zipped=self.root/'changing.zip'
        with zipfile.ZipFile(zipped,'w') as archive:
            archive.writestr('original.txt',b'synthetic-original')
        alternate=io.BytesIO()
        with zipfile.ZipFile(alternate,'w') as archive:
            archive.writestr('changed-one.txt',b'synthetic-changed')
            archive.writestr('changed-two.txt',b'synthetic-changed')
        original_add=Store.add_artifact
        def capture_then_change(store,path,*arguments,**keywords):
            receipt=original_add(store,path,*arguments,**keywords)
            if Path(path)==zipped:
                zipped.write_bytes(alternate.getvalue())
            return receipt
        with patch.object(Store,'add_artifact',capture_then_change):
            result=import_package(self.root,zipped)
        self.assertEqual(result['members'],1)
        self.assertEqual(next(Store(self.root).iter_records())['data']['package_member'],'original.txt')


if __name__=='__main__':
    unittest.main()
