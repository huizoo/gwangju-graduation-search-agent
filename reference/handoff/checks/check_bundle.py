"""Verify that the packaged files and rendered HTML share the same sources."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.anchors, self.source_paths = [], [], []
        self.in_manifest = False
        self.manifest = ''
        self.remote_assets = []

    def handle_starttag(self, tag, attributes):
        a = dict(attributes)
        if 'id' in a:
            self.ids.append(a['id'])
        if a.get('href', '').startswith('#'):
            self.anchors.append(a['href'][1:])
        if 'data-source' in a:
            self.source_paths.append(a['data-source'])
        if tag == 'script' and a.get('id') == 'source-manifest':
            self.in_manifest = True
        if tag in {'script', 'img', 'iframe', 'link'}:
            url = a.get('src') or a.get('href', '')
            if url.startswith(('http:', 'https:', '//')):
                self.remote_assets.append(url)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_manifest = False

    def handle_data(self, data):
        if self.in_manifest:
            self.manifest += data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--html', type=Path, required=True)
    parser.add_argument('--zip', type=Path, required=True)
    args = parser.parse_args()
    manifest_path = ROOT / 'checks/package-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for filename, digest in manifest['files'].items():
        assert sha((ROOT / filename).read_bytes()) == digest, filename
    dom = Document()
    dom.feed(args.html.read_text())
    assert len(set(dom.ids)) == len(dom.ids), 'duplicate HTML IDs'
    assert set(dom.anchors) <= set(dom.ids), 'broken HTML anchors'
    assert not dom.remote_assets, 'remote assets in offline document'
    assert len(set(dom.source_paths)) == len(dom.source_paths), 'duplicated source sections'
    assert set(dom.source_paths) == set(manifest['files']) | {'checks/package-manifest.json'}
    embedded = json.loads(dom.manifest)
    assert embedded['manifest_sha256'] == sha(manifest_path.read_bytes())
    assert embedded['files'] == manifest['files'], 'HTML source versions differ'
    with ZipFile(args.zip) as archive:
        assert archive.testzip() is None
        expected = {ROOT.name + '/' + name for name in manifest['files']}
        expected.add(ROOT.name + '/checks/package-manifest.json')
        assert set(archive.namelist()) == expected, 'ZIP contents differ'
        for name, digest in manifest['files'].items():
            assert sha(archive.read(ROOT.name + '/' + name)) == digest, name
        assert archive.read(ROOT.name + '/checks/package-manifest.json') == manifest_path.read_bytes()
    defaults = json.loads((ROOT / 'config/defaults.json').read_text())
    api = json.loads((ROOT / 'contracts/openapi.json').read_text())
    assert defaults['schema_version'] == api['info']['version'] == manifest['contract_version'] == '4.0'
    assert "('schema_version','4.0')" in (ROOT / 'contracts/schema.sql').read_text()
    assert defaults['external_transfer']['document_text_allowed'] is False
    assert defaults['llm_profiles']['openai'] == {'document_text_allowed': True, 'image_allowed': False}
    assert defaults['llm_profiles']['ollama'] == defaults['llm_profiles']['fake'] == defaults['external_transfer']
    sql = (ROOT / 'contracts/schema.sql').read_text()
    domain = json.loads((ROOT / 'contracts/domain.schema.json').read_text())['$defs']
    for table, column, values in [
        ('documents', 'status', domain['DocumentRead']['properties']['status']['enum']),
        ('institutions', 'type', domain['InstitutionType']['enum']),
        ('institutions', 'district', domain['District']['enum']),
        ('jobs', 'status', domain['JobStatus']['enum']),
        ('job_items', 'status', domain['ItemStatus']['enum']),
    ]:
        statement = sql.split('CREATE TABLE ' + table + '(')[1].split('\n);')[0]
        matched = re.search(column + r" TEXT[^\n]*?CHECK\(" + column + r" IN \(([^)]+)\)\)", statement)
        assert matched is not None, (table, column)
        assert set(re.findall(r"'([^']+)'", matched[1])) == set(values), (table, column)
    print(json.dumps({'contract_version': '4.0', 'files_hashed': len(manifest['files']),
                      'html_zip_same_sources': True, 'sql_enums': 'passed',
                      'anchors': len(dom.anchors), 'remote_assets': len(dom.remote_assets)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
