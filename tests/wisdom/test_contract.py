import base64
import hashlib
import json
from pathlib import Path

from hermes_wisdom.contract import (
    CONTRACT_PIN,
    ContentFile,
    author_description_hash,
    derive_content_hash,
    sanitize_author_description,
    sha256_address,
)


VECTORS = Path("hermes_wisdom/contracts/canonical-hash-vectors.v1.json")
OPENAPI = Path("hermes_wisdom/contracts/gateway-openapi.json")


def test_checked_in_gateway_openapi_matches_the_pinned_digest():
    assert hashlib.sha256(OPENAPI.read_bytes()).hexdigest() == (
        CONTRACT_PIN.openapi_sha256
    )
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))
    draft = contract["components"]["schemas"]["WisdomDraftRecord"]
    assert "changes_requested" in draft["properties"]["state"]["enum"]
    assert "/v1/sync/org/proposals/{n}/return" in contract["paths"]


def test_gateway_canonical_vectors_match_exactly():
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    files = []
    for item in vectors["files"]:
        body = base64.b64decode(item["content_base64"], validate=True)
        assert sha256_address(body) == item["hash"]
        files.append(
            ContentFile(path=item["path"], mode=item["mode"], hash=item["hash"])
        )
    assert derive_content_hash(files) == vectors["content_hash"]
    canonical = sanitize_author_description(vectors["author_description_input"])
    assert canonical == vectors["canonical_author_description"]
    assert author_description_hash(canonical) == vectors["author_description_hash"]


def test_content_hash_commits_to_mode_and_path():
    blob = sha256_address(b"same")
    plain = derive_content_hash([ContentFile(path="SKILL.md", mode="file", hash=blob)])
    executable = derive_content_hash([
        ContentFile(path="SKILL.md", mode="exec", hash=blob)
    ])
    moved = derive_content_hash([
        ContentFile(path="refs/SKILL.md", mode="file", hash=blob)
    ])
    assert len({plain, executable, moved}) == 3
