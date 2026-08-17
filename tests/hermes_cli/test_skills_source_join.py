"""Same-source joins in _resolve_source_meta_and_bundle (#88020).

Metadata and the SKILL.md payload must come from the SAME source. The old
loop let a fully-qualified skills.sh identifier keep its correct repo
metadata while the bundle was fetched from a later source that indexed the
bare skill name — serving a third party's SKILL.md behind a legitimate
repo URL.
"""

from hermes_cli.skills_hub import _resolve_source_meta_and_bundle


class _Meta:
    def __init__(self, name, identifier):
        self.name = name
        self.description = f"{name} from {identifier}"
        self.source = "test"
        self.identifier = identifier
        self.tags = []


class _Bundle:
    def __init__(self, author):
        self.files = {"SKILL.md": f"---\nauthor: {author}\n---\ncontent"}
        self.metadata = {"author": author}


class _Source:
    """Fake source: inspect/fetch hit only when the identifier is listed."""

    def __init__(self, name, inspect_ids=(), fetch_ids=(), author="nobody"):
        self.name = name
        self._inspect_ids = set(inspect_ids)
        self._fetch_ids = set(fetch_ids)
        self._author = author

    def inspect(self, identifier):
        if identifier in self._inspect_ids:
            return _Meta(identifier.rsplit("/", 1)[-1], identifier)
        return None

    def fetch(self, identifier):
        if identifier in self._fetch_ids:
            return _Bundle(self._author)
        return None


QUALIFIED = "skills-sh/mvanhorn/last30days-skill/last30days"


def test_bundle_never_joined_across_sources():
    """Anchoring source fails to fetch -> bundle is None, not the later
    source's bare-name payload (the #88020 third-party substitution)."""
    skills_sh = _Source(
        "skills_sh",
        inspect_ids={QUALIFIED},
        fetch_ids=set(),  # fetch fails for the qualified id
        author="mvanhorn",
    )
    clawhub = _Source(
        "clawhub",
        inspect_ids=set(),
        fetch_ids={QUALIFIED},  # would serve a bare-name match
        author="AIsa",
    )
    meta, bundle, matched = _resolve_source_meta_and_bundle(
        QUALIFIED, [skills_sh, clawhub]
    )
    assert meta is not None and meta.identifier == QUALIFIED
    assert matched is skills_sh
    assert bundle is None, (
        "bundle was joined across sources — metadata from "
        f"{skills_sh.name} with payload from {clawhub.name}"
    )


def test_same_source_meta_and_bundle():
    src = _Source(
        "skills_sh",
        inspect_ids={QUALIFIED},
        fetch_ids={QUALIFIED},
        author="mvanhorn",
    )
    other = _Source("clawhub", fetch_ids={QUALIFIED}, author="AIsa")
    meta, bundle, matched = _resolve_source_meta_and_bundle(QUALIFIED, [src, other])
    assert meta is not None and bundle is not None
    assert matched is src
    assert bundle.metadata["author"] == "mvanhorn"


def test_no_source_matches():
    src = _Source("skills_sh")
    meta, bundle, matched = _resolve_source_meta_and_bundle(QUALIFIED, [src])
    assert meta is None and bundle is None and matched is None
