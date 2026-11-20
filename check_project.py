"""Run this project's unittest suite and the joint correction-chain verification."""
import sys
import tempfile
import unittest
from pathlib import Path


def run_tests():
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    if suite.countTestCases():
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    else:
        print('No tests discovered in tests/', file=sys.stderr)
        result = unittest.TestResult()
    for status, failed_cases in (
        ('FAIL', (test for test, _ in result.failures)),
        ('ERROR', (test for test, _ in result.errors)),
        ('UNEXPECTED_SUCCESS', result.unexpectedSuccesses),
    ):
        for test in failed_cases:
            print(f'{status} {test.id()}')
    successful = result.testsRun > 0 and result.wasSuccessful()
    print(f'TEST SUMMARY total={result.testsRun} failures={len(result.failures)} '
          f'errors={len(result.errors)} skipped={len(result.skipped)} '
          f'expected_failures={len(result.expectedFailures)} '
          f'unexpected_successes={len(result.unexpectedSuccesses)} '
          f'result={"PASS" if successful else "FAIL"}')
    return successful


def run_settlement_examples():
    from settlement_examples import CASES, run_correction, run_v1

    for case_id in CASES:
        with tempfile.TemporaryDirectory() as directory:
            run_v1(Path(directory), case_id)
        with tempfile.TemporaryDirectory() as directory:
            run_correction(Path(directory), case_id)
    print('SETTLEMENT EXAMPLES result=PASS cases=' + ','.join(CASES))
    return True


def main():
    tests_ok = run_tests()
    try:
        settlement_ok = run_settlement_examples()
    except Exception as exc:  # noqa: BLE001 - report as a verification failure
        print(f'SETTLEMENT EXAMPLES result=FAIL {type(exc).__name__}: {exc}', file=sys.stderr)
        settlement_ok = False
    return 0 if tests_ok and settlement_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
