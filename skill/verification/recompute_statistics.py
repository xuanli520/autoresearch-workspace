#!/usr/bin/env python3
"""只读重放官方统计接口；只读审查JSON，不导入题目包或覆盖正式结果。"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', required=True, type=Path)
    args = parser.parse_args()
    doc = json.loads(args.review.read_text(encoding='utf-8'))
    method = Path(doc['method']['path']).resolve()
    expected = Path(__file__).resolve().parents[2] / 'materials/autoresearch-qa-skills/autoresearch-task-qa/scripts/content_gates.py'
    if method != expected.resolve():
        raise ValueError('只允许官方 content_gates.py')
    actual = hashlib.sha256(method.read_bytes()).hexdigest()
    if actual != doc['method']['sha256']:
        raise ValueError('官方方法版本漂移；须重新审查')
    spec = importlib.util.spec_from_file_location('official_content_gates', method)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inputs = doc['input']
    result = module.assess_improvement(inputs['comparison'], inputs['assessment'])
    print(json.dumps({'official_method_sha256': actual, 'result': result,
                      'matches_saved_result': result == doc['result']},
                     ensure_ascii=False, indent=2, allow_nan=False))
    return int(result != doc['result'])


if __name__ == '__main__':
    raise SystemExit(main())
