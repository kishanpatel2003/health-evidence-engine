"""Loss-preserving CDA import, with explicit clinical dates and source locators.

This is a document extractor, not a clinical reconciliation engine. The original
XML is always retained. Unsupported structures survive as source_xml and section
narratives rather than being silently dropped.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import re
from pathlib import Path, PurePosixPath
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from .store import Store

NS = {"c": "urn:hl7-org:v3"}
SYSTEM = "atlantic-health-mychart"
PATIENT = "patient-001"


class RejectDoctype(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("DTD declarations are not accepted")


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def text(element):
    if element is None:
        return ""
    return " ".join(" ".join(element.itertext()).split())


def xml_data(element):
    return {"tag": element.tag, "attributes": dict(element.attrib),
            "text": element.text, "tail": element.tail,
            "children": [xml_data(child) for child in element]}


def time_value(raw):
    """Keep partial dates partial; retain absent timezone rather than inventing it."""
    if not raw:
        return None
    match = re.fullmatch(r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\.\d+)?([+-]\d{4}|Z)?", raw)
    if not match:
        return {"start": None, "precision": "unparsed", "original": raw}
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    value = year
    precision = "year"
    for part, separator, name in [(month,"-","month"),(day,"-","day"),(hour,"T","hour"),(minute,":","minute"),(second,":","second")]:
        if part is not None:
            value += separator + part
            precision = name
    if fraction:
        value += fraction
        precision = "fractional_second"
    if zone:
        value += zone if zone == "Z" else zone[:3] + ":" + zone[3:]
    try:
        if day is not None:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif month is not None and not 1 <= int(month) <= 12:
            raise ValueError("Invalid month")
    except ValueError:
        return {"start": None, "precision": "invalid", "original": raw}
    return {"start": value, "precision": precision, "original": raw}


def effective(element):
    node = element.find("c:effectiveTime", NS)
    if node is None:
        return None
    if node.get("value"):
        return time_value(node.get("value"))
    low, high = node.find("c:low", NS), node.find("c:high", NS)
    low_raw = low.get("value") if low is not None else None
    high_raw = high.get("value") if high is not None else None
    if not low_raw and not high_raw:
        return {"original": dict(node.attrib), "precision": "unknown"}
    low_time, high_time = time_value(low_raw), time_value(high_raw)
    return {"start": low_time.get("start") if low_time else None,
            "end": high_time.get("start") if high_time else None,
            "precision": (low_time or high_time)["precision"],
            "original": {"low": low_raw, "high": high_raw}}


def lane_for(title, code=""):
    title = title.lower()
    rules = [("allerg", "allergies"),("medication", "medications"),("prescription", "medications"),
             ("note", "notes"),("instruction", "notes"),("referral", "referrals"),
             ("immun", "immunizations"),("vital", "observations"),
             ("result", "results"),("problem", "conditions"),
             ("diagnos", "conditions"),("family", "family_history"),
             ("social", "social_history"),("procedure", "procedures"),
             ("encounter", "encounters"),("visit", "encounters"),
             ("care team", "care_team"),("goal", "goals"),
             ("advance", "advance_care"),("insurance", "insurance"),
             ("payer", "insurance"),("payment", "insurance"),
             ("plan of treatment", "orders"),("care plan", "orders"),
             ("history", "history"),("functional", "observations"),
             ("cognitive", "observations"),("mental status", "observations"),("assessment", "observations")]
    return next((lane for term,lane in rules if term in title), "documents")


def code_data(element):
    return {**element.attrib, "original_text": text(element.find("c:originalText", NS)),
            "translations": [dict(x.attrib) for x in element.findall("c:translation", NS)]}


def summarize(element, references):
    codes = [code_data(x) for x in element.findall("c:code", NS)]
    values = []
    for value in element.findall("c:value", NS):
        attrs = dict(value.attrib)
        values.append({"type": attrs.pop("{http://www.w3.org/2001/XMLSchema-instance}type", None),
                       **attrs, "text": text(value), "source_xml": xml_data(value)})
    narrative = text(element.find("c:text", NS))
    linked = []
    for ref in element.findall(".//c:reference", NS):
        target = ref.get("value", "")
        if target.startswith("#") and target[1:] in references:
            linked.append(references[target[1:]])
    return {"label": next((x.get("displayName") or x.get("original_text") for x in codes if x.get("displayName") or x.get("original_text")), None),
            "text": narrative, "referenced_text": list(dict.fromkeys(linked)),
            "codes": codes, "values": values,
            "identifiers": [dict(x.attrib) for x in element.findall("c:id", NS)],
            "interpretations": [code_data(x) for x in element.findall("c:interpretationCode", NS)],
            "reference_ranges": [xml_data(x) for x in element.findall("c:referenceRange", NS)],
            "source_xml": xml_data(element)}


def identifier_key(identifiers):
    """Null-flavored/empty identifiers do not identify a source item."""
    substantive = []
    for identifier in identifiers:
        root, extension = identifier.get("root", ""), identifier.get("extension", "")
        if not identifier.get("nullFlavor") and (root.strip() or extension.strip()):
            substantive.append(f"{root}:{extension}")
    return "|".join(substantive) or None


def import_cda(root, path, patient_id=PATIENT, source_url=None, _store=None):
    store = _store or Store(root)
    path = Path(path)
    receipt = store.add_artifact(path, SYSTEM, source_url, "cda-xml")
    payload = (store.root / receipt["local_path"]).read_bytes()
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("DTD and entity declarations are not accepted")
    document = ET.fromstring(payload, parser=ET.XMLParser(target=RejectDoctype()))
    if local_name(document.tag) != "ClinicalDocument":
        raise ValueError("Expected a CDA ClinicalDocument")
    artifact = receipt["artifact_sha256"]
    document_id_node = document.find("c:id", NS)
    document_id = dict(document_id_node.attrib) if document_id_node is not None else {}
    document_key = "|".join(document_id.get(x, "") for x in ("root","extension")) if document_id_node is not None and identifier_key([document_id_node]) else artifact
    stamp = document.find("c:effectiveTime", NS)
    document_created = stamp.get("value") if stamp is not None else None
    document_code_node=document.find("c:code",NS)
    services=document.findall("c:documentationOf/c:serviceEvent",NS)
    context = {"document_id": document_id, "document_title": text(document.find("c:title",NS)),
               "document_created": document_created,
               "document_code":code_data(document_code_node) if document_code_node is not None else None,
               "document_coverage":[effective(service) for service in services]}
    references = {node.get("ID"): text(node) for node in document.iter() if node.get("ID")}
    locators = {}
    def locate(node, path):
        locators[id(node)] = path
        siblings = Counter()
        for child in node:
            name = local_name(child.tag)
            siblings[name] += 1
            locate(child, path+f"/c:{name}[{siblings[name]}]")
    locate(document, "/c:ClinicalDocument")
    counts = Counter()

    def emit(lane, kind, node, locator, key, data, source_status=None):
        status_node = node.find("c:statusCode", NS)
        status = source_status or (status_node.get("code") if status_node is not None else None) or "unknown"
        record = {"patient_id": patient_id,"lane": lane,"kind": kind,
                  "source": {"system":SYSTEM,"artifact_sha256":artifact,"locator":locator,"record_key":key},
                  "effective_time": effective(node),"status":status,"data":{**context,**data}}
        store.add_record(record)
        counts[lane] += 1

    header = {"tag": document.tag,"attributes":dict(document.attrib),
              "children":[xml_data(x) for x in document if local_name(x.tag)!="component"]}
    emit("documents","cda_document",document,"/ClinicalDocument",document_key,{"label":context["document_title"],"header_xml":header})
    for number, role in enumerate(document.findall("c:recordTarget/c:patientRole",NS),1):
        patient = role.find("c:patient",NS)
        data = {"label":"Patient demographics","source_xml":xml_data(role),
                "names":[text(x) for x in patient.findall("c:name",NS)] if patient is not None else [],
                "identifiers":[dict(x.attrib) for x in role.findall("c:id",NS)]}
        emit("demographics","patient_demographics",role,locators[id(role)],document_key+f":patient:{number}",data)

    for section_number,section in enumerate(document.findall(".//c:structuredBody//c:section",NS),1):
        title = text(section.find("c:title",NS))
        section_code = section.find("c:code",NS)
        code = section_code.get("code", "") if section_code is not None else ""
        lane = lane_for(title,code)
        section_context = {"section_title":title,"section_code":code}
        location = locators[id(section)]
        narrative = section.find("c:text",NS)
        emit(lane,"cda_section",section,location,document_key+":section:"+str(section_number),
             {**section_context,"label":title,"text":text(narrative),
              "narrative_xml":xml_data(narrative) if narrative is not None else None,
              "section_null_flavor":section.get("nullFlavor")})
        for entry_number, entry in enumerate(section.findall("c:entry",NS),1):
            clinical = next(iter(entry),entry)
            entry_ids = clinical.findall("c:id",NS)
            key = identifier_key(entry_ids)
            if not key:
                key = document_key + ":" + locators[id(clinical)]
            entry_location = locators[id(clinical)]
            emit(lane,"cda_entry",clinical,entry_location,key,{**section_context,**summarize(clinical,references)})
            # Component observations become independently queryable, while the
            # parent entry retains their complete relationship structure.
            for obs_number, observation in enumerate(clinical.findall(".//c:observation",NS),1):
                observation_ids = observation.findall("c:id",NS)
                obs_key = identifier_key(observation_ids)
                obs_key = obs_key or document_key+":"+locators[id(observation)]
                emit(lane,"clinical_observation",observation,locators[id(observation)],obs_key,
                     {**section_context,**summarize(observation,references),"parent_entry_key":key})
    return {"artifact_sha256":artifact,"records_processed":sum(counts.values()),"by_lane":dict(counts)}


def import_package(root, path, patient_id=PATIENT, source_url=None):
    """Preserve the original ZIP and every member; parse all CDA documents."""
    store=Store(root)
    path=Path(path)
    package_receipt=store.add_artifact(path,SYSTEM,source_url,"mychart-export-package")
    package_path=store.root/package_receipt['local_path']
    results=[]
    members=0
    total=0
    temporary_root=store.local/"tmp"
    temporary_root.mkdir(mode=0o700,exist_ok=True)
    with zipfile.ZipFile(package_path) as archive, tempfile.TemporaryDirectory(prefix="import-",dir=temporary_root) as temporary:
        for info in archive.infolist():
            name=PurePosixPath(info.filename)
            if info.is_dir():
                continue
            mode=info.external_attr >> 16
            if name.is_absolute() or ".." in name.parts or "\\" in info.filename or stat.S_ISLNK(mode):
                raise ValueError("Unsafe ZIP member")
            total+=info.file_size
            members+=1
            if total>1024*1024*1024 or members>20000:
                raise ValueError("Export exceeds extraction limits")
            content=archive.read(info)
            target=Path(temporary).joinpath(*name.parts)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
            member_url=(source_url+"#"+info.filename) if source_url else None
            receipt=store.add_artifact(target,SYSTEM,member_url,"export-member")
            if name.suffix.lower()==".xml" and b"ClinicalDocument" in content[:10000]:
                results.append(import_cda(root,store.root/receipt['local_path'],patient_id,member_url,_store=store))
            else:
                store.add_record({"patient_id":patient_id,"lane":"documents","kind":"export_member",
                    "source":{"system":SYSTEM,"artifact_sha256":receipt["artifact_sha256"],"locator":info.filename,"record_key":info.filename},
                    "data":{"label":info.filename,"media_extension":name.suffix.lower(),"package_member":info.filename}})
    return {"members":members,"cda_documents":len(results),"records_processed":sum(r["records_processed"] for r in results)}
