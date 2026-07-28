#!/usr/bin/env python3
"""
RED tests for issue #241: generate-aws-exports script (CDK outputs -> frontend config).

`frontend/src/aws-exports.ts`'s own docstring (lines 4-17, pre-#241) and
`frontend/src/main.tsx`'s Amplify-configuration comment (lines 8-14, pre-#241)
both claim a `scripts/generate-aws-exports.ts` / `.js` script already exists
that turns `cdk deploy` stack outputs into a real `aws-exports.ts`. No such
script exists anywhere in the repo before this issue lands -- these tests
drive `scripts/generate_aws_exports.py` (the actual, underscored filename
this issue introduces; see PR #295/#299 for the same drafting-convention note
on `backend/tests/` vs `tests/`) into existence.

Checks:
  1. Given a sample CDK "outputs" JSON (the shape `cdk deploy --outputs-file`
     writes: `{"<StackName>": {"<OutputKey>": "<value>", ...}}`), running the
     script emits an `aws-exports.ts` containing the real Cognito user-pool
     ID, app-client ID, and hosted-UI domain -- and a real API base URL --
     in place of the committed stub's placeholder values.
  2. The emitted file is syntactically consistent with the stub it replaces
     (same `awsExports` default-export shape) so `main.tsx`'s
     `Amplify.configure(awsExports)` call keeps working unmodified.
  3. A missing --outputs file is a clean, non-zero-exit error (no traceback
     swallowing a bad path into a broken generated file).
  4. An outputs JSON with more than one stack requires an explicit --stack
     (ambiguous otherwise) and errors clearly without one.

Failure mode expected now: ImportError / ModuleNotFoundError -- no
scripts/generate_aws_exports.py exists yet.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_aws_exports.py"

# Import the implementation under test directly (for the unit-level checks);
# this WILL FAIL (ImportError) until scripts/generate_aws_exports.py exists --
# that's the RED failure.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from generate_aws_exports import (  # noqa: E402
    build_aws_exports_source,
    extract_config,
)

SAMPLE_OUTPUTS = {
    "ContractToasterStack-dev": {
        "UserPoolId": "us-east-1_SampLe123",
        "UserPoolClientId": "sample1client2id3",
        "UserPoolDomainPrefix": "contract-toaster-dev",
        "ApiUrl": "https://abc123xyz.us-east-1.awsapprunner.com",
    }
}


def _write_sample_outputs(dir_path: Path) -> Path:
    outputs_path = dir_path / "cdk-outputs.json"
    outputs_path.write_text(json.dumps(SAMPLE_OUTPUTS, indent=2), encoding="utf-8")
    return outputs_path


# ---------------------------------------------------------------------------
# Test 1 + 2: end-to-end script run emits real values in the expected shape
# ---------------------------------------------------------------------------

def test_script_emits_real_cognito_and_api_values():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        outputs_path = _write_sample_outputs(tmp_path)
        emitted_path = tmp_path / "aws-exports.ts"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--outputs",
                str(outputs_path),
                "--output",
                str(emitted_path),
                "--region",
                "us-east-1",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"script exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert emitted_path.exists(), "script did not write the output file"

        emitted = emitted_path.read_text(encoding="utf-8")

        # Real values are present.
        assert "us-east-1_SampLe123" in emitted
        assert "sample1client2id3" in emitted
        assert "contract-toaster-dev.auth.us-east-1.amazoncognito.com" in emitted
        assert "https://abc123xyz.us-east-1.awsapprunner.com" in emitted

        # Placeholder stub values are gone.
        assert "PLACEHOLDER" not in emitted

        # Shape compatible with main.tsx's `Amplify.configure(awsExports)`.
        assert "const awsExports" in emitted
        assert "export default awsExports" in emitted
        assert "export const apiBaseUrl" in emitted

    print("PASS: test_script_emits_real_cognito_and_api_values")


# ---------------------------------------------------------------------------
# Test: extract_config / build_aws_exports_source unit-level behavior
# ---------------------------------------------------------------------------

def test_extract_config_reads_expected_output_keys():
    config = extract_config(SAMPLE_OUTPUTS["ContractToasterStack-dev"], region="us-east-1")
    assert config.user_pool_id == "us-east-1_SampLe123"
    assert config.user_pool_client_id == "sample1client2id3"
    assert config.cognito_domain == "contract-toaster-dev.auth.us-east-1.amazoncognito.com"
    assert config.api_base_url == "https://abc123xyz.us-east-1.awsapprunner.com"

    print("PASS: test_extract_config_reads_expected_output_keys")


def test_build_aws_exports_source_is_valid_looking_ts():
    config = extract_config(SAMPLE_OUTPUTS["ContractToasterStack-dev"], region="us-east-1")
    source = build_aws_exports_source(config)
    assert source.count("{") == source.count("}")
    assert "Auth" in source and "Cognito" in source
    assert "userPoolId" in source and "userPoolClientId" in source

    print("PASS: test_build_aws_exports_source_is_valid_looking_ts")


# ---------------------------------------------------------------------------
# Test 3: missing --outputs file is a clean error, not a traceback / bad write
# ---------------------------------------------------------------------------

def test_missing_outputs_file_errors_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        missing_path = tmp_path / "does-not-exist.json"
        emitted_path = tmp_path / "aws-exports.ts"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--outputs",
                str(missing_path),
                "--output",
                str(emitted_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not emitted_path.exists(), "must not write a file on error"
        assert "does-not-exist.json" in result.stderr or "does-not-exist.json" in result.stdout

    print("PASS: test_missing_outputs_file_errors_cleanly")


# ---------------------------------------------------------------------------
# Test 4: ambiguous multi-stack outputs file requires --stack
# ---------------------------------------------------------------------------

def test_ambiguous_multi_stack_outputs_requires_stack_flag():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        multi = dict(SAMPLE_OUTPUTS)
        multi["OtherStack-dev"] = {"SomeOutput": "value"}
        outputs_path = tmp_path / "cdk-outputs.json"
        outputs_path.write_text(json.dumps(multi, indent=2), encoding="utf-8")
        emitted_path = tmp_path / "aws-exports.ts"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--outputs",
                str(outputs_path),
                "--output",
                str(emitted_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not emitted_path.exists()
        assert "--stack" in result.stderr or "--stack" in result.stdout

        # Passing --stack disambiguates and succeeds.
        result2 = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--outputs",
                str(outputs_path),
                "--output",
                str(emitted_path),
                "--stack",
                "ContractToasterStack-dev",
            ],
            capture_output=True,
            text=True,
        )
        assert result2.returncode == 0, f"stderr={result2.stderr}"
        assert emitted_path.exists()

    print("PASS: test_ambiguous_multi_stack_outputs_requires_stack_flag")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_script_emits_real_cognito_and_api_values,
        test_extract_config_reads_expected_output_keys,
        test_build_aws_exports_source_is_valid_looking_ts,
        test_missing_outputs_file_errors_cleanly,
        test_ambiguous_multi_stack_outputs_requires_stack_flag,
    ]

    failures = []
    for test in tests:
        try:
            test()
        except Exception as exc:
            failures.append((test.__name__, exc))
            print(f"FAIL: {test.__name__}: {exc}")

    if failures:
        print(f"\n{len(failures)}/{len(tests)} test(s) failed.")
        return 1
    print(f"\nPASS: all {len(tests)} generate_aws_exports tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
