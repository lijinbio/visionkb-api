from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOKENS = {
    "read": "test-read-token-0000000000",
    "pr": "test-proposals-read-000000",
    "pw": "test-proposals-write-00000",
    "all": "test-all-scopes-0000000000",
}


def load_fixture_module():
    spec = importlib.util.spec_from_file_location("make_fixture", HERE / "fixtures" / "make_fixture.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_fixture"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def fixture_mod():
    return load_fixture_module()


@pytest.fixture(scope="session")
def release_dir(tmp_path_factory, fixture_mod) -> Path:
    """The fixture release: $VISIONKB_TEST_RELEASE_DIR if set (integration), else generated in tmp."""
    given = os.environ.get("VISIONKB_TEST_RELEASE_DIR")
    if given:
        return Path(given)
    return fixture_mod.make(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture(scope="session")
def tokens_file(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("auth") / "tokens.tsv"
    p.write_text(
        "# test tokens\n"
        f"{TOKENS['read']}\tread\n"
        f"{TOKENS['pr']}\tproposals:read\n"
        f"{TOKENS['pw']}\tproposals:write\n"
        f"{TOKENS['all']}\tread,proposals:read,proposals:write\n"
    )
    return p


def auth(kind: str = "read") -> dict:
    return {"Authorization": f"Bearer {TOKENS[kind]}"}
