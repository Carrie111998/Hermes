"""Tests for the Agent Marketplace (v0.25.0).

Covers:
- Listing CRUD (publish, get, search, update version)
- Installation management (install, uninstall, enable, disable, configure)
- Reviews system
- RBAC gating on install
- Built-in seed listings
- Acceptance test: publish → install → resolve

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg():
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"mkt_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    conn.commit()
    from hermes_cli.postgres_authority import init_schema
    init_schema(conn)
    yield conn
    conn.close()
    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestMarketplaceListings:
    def test_seed_builtins_exist(self, pg):
        from hermes_cli.postgres_authority import get_listing

        listing = get_listing(pg, listing_id="hermes.web-search")
        assert listing is not None
        assert listing["name"] == "Web Search"
        assert listing["package_type"] == "builtin"

    def test_publish_listing(self, pg):
        from hermes_cli.postgres_authority import publish_listing, get_listing

        listing = publish_listing(
            pg, listing_id="acme.crm-sync", name="CRM Sync",
            description="Sync contacts with Salesforce",
            category="integration", author="acme-corp",
            version="2.1.0", package_type="pip",
            package_ref="acme-crm-sync==2.1.0",
            entry_point="acme_crm.tools.sync",
        )
        assert listing["listing_id"] == "acme.crm-sync"
        assert listing["version"] == "2.1.0"

        fetched = get_listing(pg, listing_id="acme.crm-sync")
        assert fetched["author"] == "acme-corp"

    def test_publish_upserts_version(self, pg):
        from hermes_cli.postgres_authority import publish_listing, get_listing

        publish_listing(
            pg, listing_id="tool.v1", name="Tool V1", version="1.0.0",
            package_type="pip", author="test",
        )
        publish_listing(
            pg, listing_id="tool.v1", name="Tool V1", version="2.0.0",
            package_type="pip", author="test",
        )
        listing = get_listing(pg, listing_id="tool.v1")
        assert listing["version"] == "2.0.0"

    def test_search_by_name(self, pg):
        from hermes_cli.postgres_authority import publish_listing, search_listings

        publish_listing(
            pg, listing_id="search.tool", name="Super Search Tool",
            version="1.0.0", package_type="pip", author="test",
        )
        results = search_listings(pg, query="Super Search")
        assert any(r["listing_id"] == "search.tool" for r in results)

    def test_search_by_category(self, pg):
        from hermes_cli.postgres_authority import search_listings

        results = search_listings(pg, category="search")
        assert any(r["listing_id"] == "hermes.web-search" for r in results)

    def test_update_listing_version(self, pg):
        from hermes_cli.postgres_authority import (
            publish_listing, update_listing_version, get_listing,
        )

        publish_listing(
            pg, listing_id="ver.tool", name="Versioned",
            version="1.0.0", package_type="git", author="test",
            package_ref="https://github.com/test/tool.git@v1.0.0",
        )
        result = update_listing_version(
            pg, listing_id="ver.tool", version="1.1.0",
            package_ref="https://github.com/test/tool.git@v1.1.0",
        )
        assert result is True
        listing = get_listing(pg, listing_id="ver.tool")
        assert listing["version"] == "1.1.0"


class TestMarketplaceInstallations:
    def test_install_tool(self, pg):
        from hermes_cli.postgres_authority import (
            install_tool, get_installed_tools, DEFAULT_TENANT_ID,
        )

        inst = install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.web-search", version="1.0.0",
            installed_by="admin@acme.com",
        )
        assert inst["listing_id"] == "hermes.web-search"
        assert inst["enabled"] is True

        tools = get_installed_tools(pg, tenant_id=DEFAULT_TENANT_ID)
        assert any(t["listing_id"] == "hermes.web-search" for t in tools)

    def test_install_upgrade_version(self, pg):
        from hermes_cli.postgres_authority import (
            install_tool, get_installed_tools, DEFAULT_TENANT_ID,
        )

        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.email", version="1.0.0",
        )
        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.email", version="2.0.0",
        )
        tools = get_installed_tools(pg, tenant_id=DEFAULT_TENANT_ID)
        email = next(t for t in tools if t["listing_id"] == "hermes.email")
        assert email["version"] == "2.0.0"

    def test_uninstall_tool(self, pg):
        from hermes_cli.postgres_authority import (
            install_tool, uninstall_tool, get_installed_tools, DEFAULT_TENANT_ID,
        )

        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.calendar", version="1.0.0",
        )
        result = uninstall_tool(pg, tenant_id=DEFAULT_TENANT_ID, listing_id="hermes.calendar")
        assert result is True

        tools = get_installed_tools(pg, tenant_id=DEFAULT_TENANT_ID)
        assert not any(t["listing_id"] == "hermes.calendar" for t in tools)

    def test_disable_enable_tool(self, pg):
        from hermes_cli.postgres_authority import (
            install_tool, disable_tool, enable_tool,
            get_installed_tools, DEFAULT_TENANT_ID,
        )

        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.code-exec", version="1.0.0",
        )

        disable_tool(pg, tenant_id=DEFAULT_TENANT_ID, listing_id="hermes.code-exec")
        tools = get_installed_tools(pg, tenant_id=DEFAULT_TENANT_ID)
        assert not any(t["listing_id"] == "hermes.code-exec" for t in tools)

        enable_tool(pg, tenant_id=DEFAULT_TENANT_ID, listing_id="hermes.code-exec")
        tools = get_installed_tools(pg, tenant_id=DEFAULT_TENANT_ID)
        assert any(t["listing_id"] == "hermes.code-exec" for t in tools)

    def test_tool_config(self, pg):
        from hermes_cli.postgres_authority import (
            install_tool, get_tool_config, update_tool_config, DEFAULT_TENANT_ID,
        )

        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.email", version="1.0.0",
            config={"smtp_host": "smtp.example.com", "smtp_port": 587},
        )

        config = get_tool_config(pg, tenant_id=DEFAULT_TENANT_ID, listing_id="hermes.email")
        assert config["smtp_host"] == "smtp.example.com"

        update_tool_config(
            pg, tenant_id=DEFAULT_TENANT_ID, listing_id="hermes.email",
            config={"smtp_host": "mail.new.com", "smtp_port": 465},
        )
        config = get_tool_config(pg, tenant_id=DEFAULT_TENANT_ID, listing_id="hermes.email")
        assert config["smtp_host"] == "mail.new.com"

    def test_tenant_isolation(self, pg):
        from hermes_cli.postgres_authority import (
            install_tool, get_installed_tools, DEFAULT_TENANT_ID,
        )

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        install_tool(pg, tenant_id=tenant_a, listing_id="hermes.web-search", version="1.0.0")
        install_tool(pg, tenant_id=tenant_b, listing_id="hermes.email", version="1.0.0")

        tools_a = get_installed_tools(pg, tenant_id=tenant_a)
        tools_b = get_installed_tools(pg, tenant_id=tenant_b)

        assert len(tools_a) == 1
        assert tools_a[0]["listing_id"] == "hermes.web-search"
        assert len(tools_b) == 1
        assert tools_b[0]["listing_id"] == "hermes.email"


class TestMarketplaceReviews:
    def test_submit_review(self, pg):
        from hermes_cli.postgres_authority import (
            submit_review, get_reviews, DEFAULT_TENANT_ID,
        )

        review = submit_review(
            pg, listing_id="hermes.web-search",
            tenant_id=DEFAULT_TENANT_ID, rating=5,
            review_text="Works great for research tasks",
        )
        assert review["rating"] == 5

        reviews = get_reviews(pg, listing_id="hermes.web-search")
        assert len(reviews) == 1
        assert reviews[0]["review_text"] == "Works great for research tasks"

    def test_one_review_per_tenant(self, pg):
        from hermes_cli.postgres_authority import (
            submit_review, get_reviews, DEFAULT_TENANT_ID,
        )

        submit_review(
            pg, listing_id="hermes.email", tenant_id=DEFAULT_TENANT_ID,
            rating=3, review_text="OK",
        )
        submit_review(
            pg, listing_id="hermes.email", tenant_id=DEFAULT_TENANT_ID,
            rating=5, review_text="Actually great after the update",
        )

        reviews = get_reviews(pg, listing_id="hermes.email")
        assert len(reviews) == 1
        assert reviews[0]["rating"] == 5  # Updated, not duplicated


class TestMarketplaceRBAC:
    def test_install_requires_capability(self, pg):
        """Install should work, but with RBAC the runtime would gate access."""
        from hermes_cli.postgres_authority import (
            install_tool, enforce_capability, grant_capability,
            DEFAULT_TENANT_ID,
        )

        # Without any grants, enforce_capability is fail-open (no exception)
        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="hermes.web-search", version="1.0.0",
        )

        # Grant marketplace:install to a principal
        grant_capability(
            pg, tenant_id=DEFAULT_TENANT_ID,
            principal_type="user", principal_id="admin@acme.com",
            resource="marketplace", action="install",
            granted_by="system",
        )

        # Now enforcement is active — granted principal passes (no exception)
        enforce_capability(
            pg, tenant_id=DEFAULT_TENANT_ID,
            principal_type="user", principal_id="admin@acme.com",
            resource="marketplace", action="install",
        )

        # Ungated principal is denied (raises PermissionError)
        with pytest.raises(PermissionError):
            enforce_capability(
                pg, tenant_id=DEFAULT_TENANT_ID,
                principal_type="user", principal_id="random@hacker.com",
                resource="marketplace", action="install",
            )


class TestMarketplaceAcceptance:
    """Full cycle: publish → install → resolve at runtime."""

    def test_publish_install_resolve(self, pg):
        from hermes_cli.postgres_authority import (
            publish_listing, install_tool, get_installed_tools,
            get_listing, DEFAULT_TENANT_ID,
        )

        # 1. Third party publishes a tool
        publish_listing(
            pg, listing_id="vendor.analytics",
            name="Analytics Dashboard",
            description="Real-time usage analytics for agents",
            category="monitoring", author="vendor-corp",
            version="3.2.1", package_type="pip",
            package_ref="vendor-analytics==3.2.1",
            entry_point="vendor_analytics.tools.dashboard",
            capabilities_required=["analytics:read", "analytics:write"],
            metadata={"min_runtime": "0.23.0"},
        )

        # 2. Tenant installs it
        install_tool(
            pg, tenant_id=DEFAULT_TENANT_ID,
            listing_id="vendor.analytics", version="3.2.1",
            installed_by="admin@tenant.io",
            config={"api_key": "ak_test_123", "dashboard_url": "https://dash.vendor.io"},
        )

        # 3. Runtime resolves installed tools for this tenant
        tools = get_installed_tools(pg, tenant_id=DEFAULT_TENANT_ID)
        analytics = next(
            (t for t in tools if t["listing_id"] == "vendor.analytics"), None
        )
        assert analytics is not None
        assert analytics["entry_point"] == "vendor_analytics.tools.dashboard"
        assert analytics["config"]["api_key"] == "ak_test_123"

        # 4. Runtime can look up capabilities required
        listing = get_listing(pg, listing_id="vendor.analytics")
        assert "analytics:read" in listing["capabilities_required"]
