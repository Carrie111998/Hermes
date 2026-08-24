"""Runtime dashboard-plugin route remount regression tests for issue #3."""

from tui_gateway import server


def test_plugins_manage_reload_dashboard_routes_requires_confirmation(monkeypatch):
    called = []

    import hermes_cli.web_server as web_server

    monkeypatch.setattr(
        web_server,
        "remount_dashboard_plugin_api_routes",
        lambda: called.append(True) or {"ok": True, "mounted": ["fleet-graph"]},
    )

    refused = server.handle_request(
        {
            "id": "reload-no-confirm",
            "method": "plugins.manage",
            "params": {"action": "reload_dashboard_routes"},
        }
    )
    assert "error" in refused, refused
    assert not called


def test_plugins_manage_reload_dashboard_routes_returns_mount_receipt(monkeypatch):
    called = []

    import hermes_cli.web_server as web_server

    monkeypatch.setattr(
        web_server,
        "remount_dashboard_plugin_api_routes",
        lambda: called.append(True) or {
            "ok": True,
            "mounted": ["fleet-graph", "kanban"],
        },
    )

    response = server.handle_request(
        {
            "id": "reload-confirmed",
            "method": "plugins.manage",
            "params": {"action": "reload_dashboard_routes", "confirm": True},
        }
    )
    assert response.get("result") == {
        "ok": True,
        "mounted": ["fleet-graph", "kanban"],
        "protocol": "fleet-graph.dashboard-routes",
        "protocol_version": 1,
    }, response
    assert called == [True]


def test_route_remount_is_scoped_and_idempotent(monkeypatch):
    import sys
    import types

    from hermes_cli import web_server

    class Route:
        def __init__(self, path):
            self.path = path

    original_routes = web_server.app.router.routes
    base_route = Route("/api/dashboard/plugins/hub")
    old_plugin_route = Route("/api/plugins/fleet-graph/overview")
    setattr(old_plugin_route, "_hermes_dashboard_plugin", "fleet-graph")
    web_server.app.router.routes = [base_route, old_plugin_route]
    web_server._dashboard_plugin_api_route_ids.clear()
    web_server._dashboard_plugin_api_route_ids.add(id(old_plugin_route))
    module_name = "hermes_dashboard_plugin_fleet_graph"
    sys.modules[module_name] = types.ModuleType(module_name)
    web_server._dashboard_plugin_api_module_names.clear()
    web_server._dashboard_plugin_api_module_names.add(module_name)

    def fake_mount():
        new_route = Route("/api/plugins/fleet-graph/overview")
        setattr(new_route, "_hermes_dashboard_plugin", "fleet-graph")
        web_server.app.router.routes.append(new_route)
        web_server._dashboard_plugin_api_route_ids.add(id(new_route))

    monkeypatch.setattr(web_server, "_mount_plugin_api_routes", fake_mount)
    try:
        first = web_server.remount_dashboard_plugin_api_routes()
        second = web_server.remount_dashboard_plugin_api_routes()
        plugin_routes = [
            route
            for route in web_server.app.router.routes
            if getattr(route, "_hermes_dashboard_plugin", "")
        ]
        assert first == {"ok": True, "mounted": ["fleet-graph"], "count": 1}
        assert second == first
        assert len(plugin_routes) == 1
        assert web_server.app.router.routes[0] is base_route
        assert module_name not in sys.modules
    finally:
        web_server.app.router.routes = original_routes
        web_server._dashboard_plugin_api_route_ids.clear()
        web_server._dashboard_plugin_api_module_names.clear()


def test_real_plugin_router_mount_and_remount(tmp_path, monkeypatch):
    from hermes_cli import plugins_cmd, web_server

    plugin_dir = tmp_path / "dashboard"
    plugin_dir.mkdir()
    (plugin_dir / "plugin_api.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/health')\n"
        "def health():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    plugin = {
        "name": "fleet-graph-test",
        "source": "user",
        "_dir": str(plugin_dir),
        "_api_file": "plugin_api.py",
    }
    enabled = {"fleet-graph-test"}
    monkeypatch.setattr(web_server, "_get_dashboard_plugins", lambda: [plugin])
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: enabled)
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set())
    original_routes = web_server.app.router.routes
    baseline_mounted = sorted(
        {
            str(getattr(route, "_hermes_dashboard_plugin", ""))
            for route in original_routes
            if getattr(route, "_hermes_dashboard_plugin", "")
        }
    )
    web_server._dashboard_plugin_api_route_ids.clear()
    web_server._dashboard_plugin_api_module_names.clear()
    try:
        web_server._mount_plugin_api_routes()
        mounted = [
            route for route in web_server.app.router.routes
            if getattr(route, "_hermes_dashboard_plugin", "") == "fleet-graph-test"
        ]
        assert len(mounted) == 1
        assert mounted[0].path == "/api/plugins/fleet-graph-test/health"

        enabled.clear()
        receipt = web_server.remount_dashboard_plugin_api_routes()
        assert receipt == {"ok": True, "mounted": baseline_mounted, "count": len(baseline_mounted)}
        assert not any(
            getattr(route, "_hermes_dashboard_plugin", "") == "fleet-graph-test"
            for route in web_server.app.router.routes
        )
    finally:
        web_server.app.router.routes = original_routes
        web_server._dashboard_plugin_api_route_ids.clear()
        web_server._dashboard_plugin_api_module_names.clear()
