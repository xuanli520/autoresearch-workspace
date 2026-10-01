"""One bounded control request; never keep a research worker attached to SSH."""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from controller import dispatch
from core.longrun import check_storage


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(2*1024*1024))
        args = argparse.Namespace(**payload['args'])
        if args.action not in ('init', 'start', 'status', 'doctor', 'stop', 'recover', 'context', 'logs'):
            raise ValueError('unsupported remote action')
        check_storage(payload['data_mount'], Path(args.state_dir))
        if args.action == 'start' and (not args.background or args.no_guard):
            raise ValueError('remote start must be detached and guarded')
        result, code = dispatch(args, remote_payload=payload)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
