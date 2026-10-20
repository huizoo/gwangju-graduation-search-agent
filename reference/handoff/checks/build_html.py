"""Render the revised files into one offline HTML document."""

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

from markdown_it import MarkdownIt


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    ('Chapter 1. 개정 근거와 기획', ['00_revision_notes.md', '01_product_plan.md']),
    ('Chapter 2. 기술 설계와 게시 규칙', [
        '02_technical_design.md', '08_release_policy.md', 'contracts/README.md', '05_sources.md']),
    ('Chapter 3. 실제 검증과 구현 인계', [
        '07_execution_basis.md', '06_acceptance.md', '03_setup_guide.md', '04_codex_handoff.txt']),
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--zip-name', default='graduation-agent-handoff-integrated.zip')
    args = parser.parse_args()
    files = sorted(p for p in ROOT.rglob('*') if p.is_file()
                   and '__pycache__' not in p.parts and p.name != 'package-manifest.json')
    manifest = {'contract_version': '4.0', 'scope': 'integrated_specification_not_implemented_service',
                'files': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in files}}
    manifest_path = ROOT / 'checks/package-manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    manifest_hash = digest(manifest_path.read_bytes())
    renderer = MarkdownIt('commonmark', {'html': False}).enable('table')
    parts = ['<header><h1>광주 졸업식 일정 도우미<br>통합 기획·설계 4.0</h1>'
             '<p class="author">김희주</p></header>',
             '<p><strong>2026-10-20 · 계약 4.0</strong></p>',
             '<p><mark>개별 수집·읽기 경로는 확인했으며, 자동 게시의 정확성과 전체 기관 수집은 아직 검증하지 않았습니다.</mark> '
             '이 문서는 수정된 기획·설계와 계약을 통합한 사본입니다. 앱 구현 완료 보고가 아닙니다.</p>',
             '<p>LLM 후보 해석, 서버의 근거 검증, 확인 일정 게시를 분리합니다. '
             '일반 조회·판매 준비·관리 기록은 하나의 <code>Publication</code> 판정 결과를 사용합니다.</p>',
             '<nav aria-label="문서 목차"><p><strong>문서 이동</strong></p><ul>']
    all_documents = [name for _, names in DOCUMENTS for name in names]
    anchors = {name: 'doc-' + str(i) for i, name in enumerate(all_documents)}
    titles = {}
    for name in all_documents:
        text = (ROOT / name).read_text()
        titles[name] = text.splitlines()[0].lstrip('# ') if name.endswith('.md') else 'Codex 구현 인계문'
        parts.append(f'<li><a href="#{anchors[name]}">{html.escape(titles[name])}</a></li>')
    parts.append('<li><a href="#contracts">부록 · 기계 계약과 실행 증거</a></li></ul></nav>')
    for chapter, names in DOCUMENTS:
        parts.append('<section><h2>' + html.escape(chapter) + '</h2>')
        for name in names:
            text = (ROOT / name).read_text()
            parts.append(f'<article id="{anchors[name]}" data-source="{html.escape(name)}">')
            parts.append('<h3>' + html.escape(titles[name]) + '</h3>')
            parts.append('<p class="source">원본 파일: <code>' + html.escape(name) + '</code></p>')
            if name.endswith('.txt'):
                parts.append('<pre>' + html.escape(text) + '</pre>')
            else:
                rendered = renderer.render('\n'.join(text.splitlines()[1:]))
                rendered = re.sub(r'<(/?)h([1-6])>',
                                  lambda m: '<' + m[1] + 'h' + str(min(int(m[2]) + 2, 6)) + '>', rendered)
                rendered = rendered.replace('<table>', '<div class="table-wrap"><table>').replace('</table>', '</table></div>')
                parts.append(rendered)
            parts.append('</article>')
        parts.append('</section>')
    parts.append('<section id="contracts" class="contracts"><h2>부록. 기계 계약과 실행 증거</h2>'
                 '<p>아래 계약은 구현할 구조입니다. 예제의 <code>fixture.*</code>는 가상 규칙이며 live 확인 근거가 아닙니다. '
                 '실제 앱의 미완료 항목은 검증 문서에 남깁니다.</p>')
    appendix = [p for p in files if str(p.relative_to(ROOT)) not in all_documents]
    appendix.append(manifest_path)
    for path in appendix:
        name = str(path.relative_to(ROOT))
        parts.append('<details data-source="' + html.escape(name) + '"><summary>' + html.escape(name)
                     + '</summary><pre>' + html.escape(path.read_text()) + '</pre></details>')
    parts.append('</section><footer><p class="source">문서·계약은 같은 패키지에서 생성했습니다. '
                 '이 HTML은 서비스의 실제 일정 데이터가 아닙니다.</p></footer>')
    template = args.template.read_text()
    template = template.replace('<title>실습 보고서 템플릿</title>', '<title>광주 졸업식 일정 도우미 · 통합 기획·설계 4.0</title>')
    template = template.replace('</style>', '''
article { margin-top:22pt; }
.table-wrap { width:100%; }
details { margin:10pt 0; border-top:.5pt solid var(--grid); padding-top:7pt; }
summary { cursor:pointer; font-weight:700; overflow-wrap:anywhere; }
pre { font-size:9pt; word-break:break-all; }
nav ul { columns:2; }
@media screen and (max-width:600px) { nav ul { columns:1; } table { font-size:9pt; } th,td { padding:5pt; } }
@media print { details { break-inside:auto; } details:not([open])>pre { display:none; } }
</style>''')
    body = ('<body><div class="preview-tools"><button type="button" id="print-document">인쇄</button>'
            '<a href="' + html.escape(args.zip_name) + '">수정 인계 ZIP</a></div><main class="report">')
    body += ''.join(parts) + '</main><script>document.getElementById("print-document").addEventListener("click",()=>window.print());</script></body>'
    template = re.sub(r'<body>.*</body>', lambda _: body, template, flags=re.S)
    metadata = json.dumps({'manifest_sha256': manifest_hash, **manifest}, ensure_ascii=False).replace('<', '\\u003c')
    template = template.replace('</head>', '<meta name="package-manifest-sha256" content="' + manifest_hash + '">'
                                '<script id="source-manifest" type="application/json">' + metadata + '</script></head>')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(template)
    zip_path = args.output.parent / args.zip_name
    with ZipFile(zip_path, 'w', ZIP_DEFLATED) as archive:
        for path in [*files, manifest_path]:
            archive.write(path, ROOT.name + '/' + str(path.relative_to(ROOT)))
    print(json.dumps({'html': str(args.output), 'zip': str(zip_path), 'documents': len(all_documents), 'embedded_files': len(appendix),
                      'manifest_sha256': manifest_hash}, ensure_ascii=False))


if __name__ == '__main__':
    main()
