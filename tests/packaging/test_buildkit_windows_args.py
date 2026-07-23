"""Guard: the Windows build CLI maps flags to BuildContext correctly, and the
mutual-exclusion checks in `assert_signing_options` actually fire.

`python -m scripts.buildkit windows` is the build entrypoint, so the flag ->
context contract and the signing-option guards must hold. Loaded by file path
to stay independent of scripts/ being importable (mirrors
test_buildkit_macos_args.py's loading approach). This module only exercises
pure-Python argument parsing/validation — no signtool.exe or cert calls, so it
runs green on macOS/Linux CI as well as Windows.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BUILDKIT = _REPO / "scripts" / "buildkit"


def _load_windows() -> ModuleType:
    # Register the package so windows.py's relative imports (`from . import ...`) resolve.
    pkg_spec = importlib.util.spec_from_file_location(
        "scripts_buildkit_pkg3",
        _BUILDKIT / "__init__.py",
        submodule_search_locations=[str(_BUILDKIT)],
    )
    assert pkg_spec is not None and pkg_spec.loader is not None
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["scripts_buildkit_pkg3"] = pkg
    pkg_spec.loader.exec_module(pkg)
    for sub in ("version", "_proc", "context", "phases", "windows"):
        spec = importlib.util.spec_from_file_location(
            f"scripts_buildkit_pkg3.{sub}", _BUILDKIT / f"{sub}.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"scripts_buildkit_pkg3.{sub}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["scripts_buildkit_pkg3.windows"]


def test_default_flags_full_signed_release_build() -> None:
    windows = _load_windows()
    ctx, args = windows.parse_args([])
    assert ctx.build_mode == "release"
    assert ctx.no_sign is False
    assert ctx.skip_dashboard is False
    assert args.self_sign is False
    assert args.certificate_thumbprint == ""
    windows.assert_signing_options(args)  # no exception


def test_all_flags_map_through() -> None:
    windows = _load_windows()
    ctx, args = windows.parse_args(
        ["--debug", "--no-sign", "--skip-dashboard", "--install", "--iscc", "C:\\iscc.exe"]
    )
    assert ctx.build_mode == "debug"
    assert ctx.no_sign is True
    assert ctx.skip_dashboard is True
    assert args.install is True
    assert args.iscc == "C:\\iscc.exe"


def test_no_sign_and_self_sign_is_rejected() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--no-sign", "--self-sign"])
    with pytest.raises(windows.BuildError, match="either --no-sign or --self-sign"):
        windows.assert_signing_options(args)


def test_trust_self_signed_without_self_sign_is_rejected() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--trust-self-signed-certificate"])
    with pytest.raises(windows.BuildError, match="requires --self-sign"):
        windows.assert_signing_options(args)


def test_setup_self_signed_only_without_self_sign_is_rejected() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--setup-self-signed-certificate-only"])
    with pytest.raises(windows.BuildError, match="requires --self-sign"):
        windows.assert_signing_options(args)


def test_self_sign_and_certificate_thumbprint_is_rejected() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--self-sign", "--certificate-thumbprint", "ABCDEF"])
    with pytest.raises(windows.BuildError, match="either --self-sign or --certificate-thumbprint"):
        windows.assert_signing_options(args)


def test_self_sign_alone_is_accepted() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--self-sign"])
    windows.assert_signing_options(args)  # no exception


def test_self_sign_with_trust_self_signed_is_accepted() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--self-sign", "--trust-self-signed-certificate"])
    windows.assert_signing_options(args)  # no exception


def test_self_sign_with_setup_self_signed_only_is_accepted() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--self-sign", "--setup-self-signed-certificate-only"])
    windows.assert_signing_options(args)  # no exception


def test_certificate_thumbprint_alone_is_accepted() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(["--certificate-thumbprint", "ABCDEF"])
    windows.assert_signing_options(args)  # no exception


def test_signing_params_reflects_flags() -> None:
    windows = _load_windows()
    _, args = windows.parse_args(
        [
            "--self-sign",
            "--trust-self-signed-certificate",
            "--self-signed-subject",
            "CN=Test",
            "--timestamp-url",
            "http://ts.example",
        ]
    )
    params = windows._signing_params(args)
    assert "-SelfSign" in params
    assert "-TrustSelfSignedCertificate" in params
    assert params[params.index("-SelfSignedCertificateSubject") + 1] == "CN=Test"
    assert params[params.index("-TimestampUrl") + 1] == "http://ts.example"


def test_parse_args_returns_namespace_with_defaults() -> None:
    windows = _load_windows()
    _, args = windows.parse_args([])
    assert isinstance(args, argparse.Namespace)
    assert args.no_sign is False
    assert args.self_sign is False
    assert args.trust_self_signed is False
    assert args.setup_self_signed_only is False
    assert args.certificate_thumbprint == ""
    assert args.timestamp_url == "http://timestamp.digicert.com"
