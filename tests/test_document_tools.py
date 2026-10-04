"""Observable selection, readiness and output-validation scenarios."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'plugins/confluence-docs/skills/confluence-doc-writer'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / f'scripts/{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


templates = module('templates')
validator = module('validate_body')


class SelectionAndIntakeTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'dist/test-work'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(parent.resolve()) and self.root != parent.resolve()
        self.addCleanup(self.temp.cleanup)
        self.catalog = templates.load_catalog()

    def completion(self):
        return templates.select('완료 보고', self.root, {}, self.catalog)

    def test_korean_selection_and_local_override(self):
        self.assertEqual(self.completion()['id'], 'completion-report')
        local = self.root / 'completion-report.md'
        local.write_text('Company-specific sections', encoding='utf-8')
        result = self.completion()
        self.assertEqual(Path(result['path']), local)
        self.assertIsNone(result['sections'])
        with self.assertRaises(ValueError):
            templates.readiness(result, {'sections': {}})

    def test_custom_alias_and_missing_explicit_template(self):
        local = self.root / '사내완료.md'
        local.write_text('Local', encoding='utf-8')
        alias = {'완료보고': local.name}
        self.assertEqual(Path(templates.select('완료보고', self.root, alias)['path']), local)
        local.unlink()
        with self.assertRaises(ValueError):
            templates.select('완료보고', self.root, alias)

    def test_unknown_name_does_not_fall_back(self):
        with self.assertRaises(ValueError):
            templates.select('없는양식', self.root, {})

    def test_issue_selection_and_removed_operations_local_override(self):
        for name in ('이슈 보고', '장애 보고서', 'issue report', 'issue-report.md'):
            with self.subTest(name=name):
                self.assertEqual(templates.select(name, self.root, {})['id'], 'issue-report')
        self.assertNotIn('operations', {entry['id'] for entry in self.catalog})
        with self.assertRaises(ValueError):
            templates.select('operations.md', self.root, {})
        local = self.root / 'operations.md'
        local.write_text('Company-specific operations template', encoding='utf-8')
        self.assertEqual(Path(templates.select('operations.md', self.root, {})['path']), local)
        issue_override = self.root / 'issue-report.md'
        issue_override.write_text('Company-specific issue template', encoding='utf-8')
        self.assertEqual(Path(templates.select('이슈 보고', self.root, {})['path']), issue_override)

    def test_issue_readiness_allows_pending_cause_and_reopens_conflicts(self):
        selected = templates.select('이슈 보고', self.root, {})
        state = {'sections': {
            '발생 개요': {'status': 'confirmed', 'content': '대상·환경·종료 현상·확인 시각·현재 상태와 확인 시점 확인'},
        }}
        self.assertFalse(templates.readiness(selected, state)['ready'])
        state['sections'].update({
            '영향 범위': {'status': 'agreed_pending', 'content': '사용자가 영향 범위 조사 중으로 작성 요청'},
            '대응 경과': {'status': 'confirmed', 'content': '09:15 재기동, 업무 복구는 확인 중으로 제공'},
            '원인 확인 및 근거': {'status': 'agreed_pending', 'content': '사용자가 원인 미확인으로 작성 요청, 제공된 OOM 로그 확인'},
            'Action Item': {'status': 'agreed_none', 'content': '추가 작업 없음을 사용자 확인'},
        })
        self.assertTrue(templates.readiness(selected, state)['ready'])
        state['sections']['대응 경과'] = {'status': 'unresolved', 'content': '복구 확인 결과가 이전 답변과 충돌'}
        self.assertEqual(templates.readiness(selected, state)['blockers'], ['대응 경과'])

    def test_focused_meeting_selection_and_readiness(self):
        for name in ('주요 안건 회의록', '핵심 안건 회의록', '안건별 상세 회의록', 'meeting-focused.md'):
            with self.subTest(name=name):
                self.assertEqual(templates.select(name, self.root, {})['id'], 'meeting-focused')
        self.assertEqual(templates.select('회의록', self.root, {})['id'], 'meeting')
        selected = templates.select('주요 안건 회의록', self.root, {})
        state = {'sections': {
            '회의 정보': {'status': 'confirmed', 'content': '일시와 참석자 확인'},
            '논의 요점': {'status': 'confirmed', 'content': '안건 A와 B의 논의 자료 확인'},
        }}
        self.assertEqual(templates.readiness(selected, state)['blockers'], ['결정 사항', '후속 작업'])
        state['sections'].update({
            '결정 사항': {'status': 'agreed_pending', 'content': 'A의 결정과 B의 결정 미정 표현을 사용자 확인'},
            '후속 작업': {'status': 'confirmed', 'content': 'A와 B의 모든 작업·담당·기한 확인'},
        })
        self.assertTrue(templates.readiness(selected, state)['ready'])
        state['sections']['결정 사항'] = {'status': 'unresolved', 'content': 'B 결정의 새 답변이 충돌'}
        self.assertEqual(templates.readiness(selected, state)['blockers'], ['결정 사항'])
        local = self.root / 'meeting-focused.md'
        local.write_text('Company-specific agenda template', encoding='utf-8')
        self.assertEqual(Path(templates.select('주요 안건 회의록', self.root, {})['path']), local)

    def test_config_resolves_relative_to_config_not_process_directory(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'template_root': 'company', 'template_aliases': {'완료보고': '완료.md'}}), encoding='utf-8')
        root, aliases, _ = templates.settings(path)
        self.assertEqual(root, self.root / 'company')
        self.assertEqual(aliases['완료보고'], '완료.md')
        path.write_text(json.dumps({'template_aliases': {'완료보고': '../private.md'}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            templates.settings(path)

    def test_rough_memo_then_answers_enable_generation(self):
        state = {'sections': {'추진내용': {'status': 'confirmed', 'content': 'A 시스템 배치 개선 완료, 변경 파일과 테스트 결과 제공'}}}
        first = templates.readiness(self.completion(), state)
        self.assertFalse(first['ready'])
        self.assertNotIn('Executive Summary', first['blockers'])
        self.assertIn('효과', first['blockers'])
        state['sections'].update({
            '배경': {'status': 'confirmed', 'content': '기존 배치 대기시간 개선 목적'},
            '효과': {'status': 'confirmed', 'content': '운영자 정성 평가로 대기 감소, 실측 데이터 없음'},
            '향후계획': {'status': 'agreed_pending', 'content': '사용자가 담당·일정을 미정으로 기재하도록 확인'},
        })
        self.assertTrue(templates.readiness(self.completion(), state)['ready'])
        # A correction reopens only the affected section; other answers survive.
        state['sections']['효과'] = {'status': 'unresolved', 'content': '기존 효과 표현과 새 답변이 충돌'}
        self.assertEqual(templates.readiness(self.completion(), state)['blockers'], ['효과'])

    def test_missing_optional_is_omitted_but_conflicting_optional_blocks(self):
        selected = templates.select('현황 보고', self.root, {})
        state = {'sections': {
            '현황 및 주요 지표': {'status': 'confirmed', 'content': 'A 작업 완료, B 작업 진행 중'},
            '후속 작업': {'status': 'agreed_none', 'content': '추가 작업 없음을 사용자 확인'},
        }}
        self.assertTrue(templates.readiness(selected, state)['ready'])
        state['sections']['의사결정 요청'] = {'status': 'unresolved'}
        self.assertFalse(templates.readiness(selected, state)['ready'])

    def test_settled_without_content_and_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            templates.readiness(self.completion(), {'sections': {'배경': {'status': 'confirmed'}}})
        with self.assertRaises(ValueError):
            templates.readiness(self.completion(), {'sections': {'배경': {'status': 'confirmed', 'content': []}}})
        with self.assertRaises(ValueError):
            templates.readiness(self.completion(), {'sections': {'효꽈': {'status': 'confirmed', 'content': 'typo'}}})


class BodyValidationTests(unittest.TestCase):
    def codes(self, html):
        return {issue['code'] for issue in validator.validate_html(html)}

    def test_valid_report_and_literal_code_examples(self):
        html = '<p>완료 요약 &amp; 결과</p><h2>효과</h2><table><thead><tr><th>값</th></tr></thead><tbody><tr><td>10</td></tr></tbody></table><pre><code>&lt;body&gt;{{code_example}}\n```python\n</code></pre>'
        self.assertEqual(validator.validate_html(html), [])

    def test_wrappers_comments_and_unfilled_fields_fail(self):
        codes = self.codes('<!doctype html><html><body><h1>제목</h1><!-- 작성 안내 --><p>{{담당}}</p></body></html>')
        self.assertTrue({'document_wrapper', 'comment', 'placeholder'} <= codes)

    def test_code_fence_and_broken_nesting_fail(self):
        self.assertIn('code_fence', self.codes('```html\n<p>내용</p>\n```'))
        self.assertIn('closing_tag', self.codes('<p><strong>내용</p>'))
        self.assertIn('nesting', self.codes('<p><h2>잘못된 위치</h2></p>'))

    def test_local_image_and_obfuscated_active_url_fail(self):
        self.assertIn('image_path', self.codes('<img src="diagrams/a.png" alt="그림">'))
        self.assertIn('image_path', self.codes('<img src="https:diagrams/a.png" alt="그림">'))
        self.assertIn('url', self.codes('<a href="java&#10;script:alert(1)">링크</a>'))
        self.assertIn('active_content', self.codes('<p onclick="run()">내용</p>'))
        self.assertEqual(self.codes('<img src="/download/attachments/123/a.png" alt="그림">'), set())

    def test_empty_and_missing_closure_fail(self):
        self.assertIn('empty', self.codes(' \n'))
        self.assertIn('closing_tag', self.codes('<h2>효과'))
        self.assertIn('escape', self.codes('<p'))
        self.assertIn('escape', self.codes('<p>본문</p><p'))
        self.assertEqual(self.codes('<p>&lt;p&gt; 문법</p>'), set())

    def test_cli_pass_failure_and_read_only(self):
        parent = ROOT / 'dist/test-work'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory).resolve()
            assert root.is_relative_to(parent.resolve()) and root != parent.resolve()
            path = root / 'report.body.html'
            script = SKILL / 'scripts/validate_body.py'
            for source, expected in [('<p>완료</p>', 0), ('<body>잘못된 출력</body>', 1)]:
                path.write_text(source, encoding='utf-8')
                result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(path.read_text(encoding='utf-8'), source)


if __name__ == '__main__':
    unittest.main()
