#!/usr/bin/env python3
"""Contracts for local Xianyu login identity checks and safe replacement."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(tempfile.mkdtemp(prefix="xianyu-local-login-contract-"))
os.environ.update(
    {
        "SAAS_DB": str(RUN_DIR / "saas.db"),
        "SAAS_TENANTS_DIR": str(RUN_DIR / "tenants"),
        "SAAS_COOKIE_SECURE": "0",
        "SAAS_RESTORE_WORKERS": "0",
        "SAAS_TESTING": "1",
        "SAAS_ALLOW_REGISTRATION": "1",
        "SAAS_SELF_UPDATE_ENABLED": "1",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import shop_sync  # noqa: E402


def fake_shop_sync(cookie_header: str) -> dict:
    _, cookies = shop_sync.parse_cookie_header(cookie_header)
    seller = cookies["unb"]
    return {
        "version": 1,
        "account_ref": shop_sync.account_ref(cookies),
        "nickname": f"本地登录店铺-{seller}",
        "products": [],
        "product_count": 0,
        "synced_at": "2026-10-18T22:00:00+0800",
        "truncated": False,
    }


def login(client: TestClient) -> int:
    app.db.create_user(
        "local-login-owner",
        "password-123",
        role="owner",
        initializer=app._new_user_initializer({}),
    )
    response = client.post(
        "/api/auth/login",
        json={"username": "local-login-owner", "password": "password-123"},
    )
    assert response.status_code == 200, response.text
    client.cookies.set(
        app.SESSION_COOKIE,
        response.cookies.get(app.SESSION_COOKIE),
        path="/",
    )
    user_id = int(app.db.get_user("local-login-owner")["id"])
    with app.db._lock:
        app.db.con.execute(
            "UPDATE users SET expires_at = 4102444800 WHERE id = ?", (user_id,)
        )
        app.db.con.commit()
    return user_id


def create_account(client: TestClient, key: str):
    response = client.post(
        "/api/bot/accounts", json={"key": key, "name": f"店铺-{key}"}
    )
    assert response.status_code == 200, response.text


def load_local_login_script():
    path = ROOT / "scripts/sync-local-login.py"
    spec = importlib.util.spec_from_file_location("sync_local_login", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> None:
    app.sync_shop = fake_shop_sync
    app.reserve_sync = lambda *_args: None
    client = TestClient(app.app)
    user_id = login(client)

    running: dict[str, int] = {}
    starts: dict[str, int] = {}
    next_pid = 62000

    def fake_status(_user_id, account_key="default"):
        snapshot = shop_sync.load_verified_snapshot(user_id, account_key)
        return {"connected": snapshot is not None, "running": account_key in running}

    def fake_start(_user_id, mode, account_key="default"):
        nonlocal next_pid
        del mode
        if account_key in running:
            return True, "already_running"
        next_pid += 1
        running[account_key] = next_pid
        starts[account_key] = starts.get(account_key, 0) + 1
        return True, "started"

    def fake_stop(_user_id, account_key="default"):
        if account_key not in running:
            return False, "not_running"
        running.pop(account_key, None)
        return True, "stopped"

    with (
        patch.object(app, "maintenance_active", return_value=False),
        patch.object(app, "bot_status", side_effect=fake_status),
        patch.object(app, "bot_start", side_effect=fake_start),
        patch.object(app, "bot_stop", side_effect=fake_stop),
        patch.object(app, "bot_process_id", side_effect=lambda _uid, key="default": running.get(key)),
    ):
        create_account(client, "local-shop")
        headers = {"X-Shop-Account": "local-shop"}
        original_cookie = "unb=620001; _m_h5_tk=old-token_tail; sid=old"
        connected = client.put("/api/bot/cookies", headers=headers, json={"cookies": original_cookie})
        assert connected.status_code == 200, connected.text
        assert starts["local-shop"] == 1

        account = app.db.get_shop_account(user_id, account_key="local-shop")
        identity = shop_sync.account_ref(shop_sync.parse_cookie_header(original_cookie)[1])
        assert account["account_ref"] == identity

        expired_state = {
            "version": 2,
            "phase": "NEEDS_HUMAN",
            "code": "session_expired",
            "failure_class": "NEEDS_HUMAN",
            "needs_human": True,
            "reauthorization_required": True,
            "updated_at": 1.0,
        }
        app.write_secret(user_id, "auth_status.json", json.dumps(expired_state), "local-shop")
        status = client.get("/api/bot/status", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["account"]["identity_fingerprint"] == identity

        mismatch = "unb=620002; _m_h5_tk=wrong-token_tail; sid=wrong"
        rejected = client.put(
            "/api/bot/cookies",
            headers=headers,
            json={"cookies": mismatch, "expected_account_ref": identity, "resume": True},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "account_mismatch"
        assert app.read_secret(user_id, "cookies.txt", "local-shop") == original_cookie
        assert "local-shop" in running

        replacement = "unb=620001; _m_h5_tk=new-token_tail; sid=new"
        replaced = client.put(
            "/api/bot/cookies",
            headers=headers,
            json={"cookies": replacement, "expected_account_ref": identity, "resume": True},
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["connected"] is True
        assert replaced.json()["identity_fingerprint"] == identity
        assert app.read_secret(user_id, "cookies.txt", "local-shop") == replacement
        assert starts["local-shop"] == 2
        assert "local-shop" in running

        failing = "unb=620001; _m_h5_tk=failed-token_tail; sid=failed"

        def fail_sync(_cookie):
            raise shop_sync.ShopSyncError("cookie_expired", "expired")

        with patch.object(app, "sync_shop", side_effect=fail_sync):
            failed = client.put(
                "/api/bot/cookies",
                headers=headers,
                json={"cookies": failing, "expected_account_ref": identity, "resume": True},
            )
        assert failed.status_code == 422, failed.text
        assert app.read_secret(user_id, "cookies.txt", "local-shop") == replacement
        assert "local-shop" in running

        # The local login-only workbench must complete the verified sync while
        # leaving the local shop Worker stopped. The online deployment keeps
        # the normal autostart path because this flag is process-local.
        running.pop("local-shop", None)
        local_only_cookie = "unb=620001; _m_h5_tk=local-only-token_tail; sid=local-only"
        with patch.dict(os.environ, {"SAAS_LOCAL_LOGIN_ONLY": "1"}):
            local_only = client.put(
                "/api/bot/cookies",
                headers=headers,
                json={
                    "cookies": local_only_cookie,
                    "expected_account_ref": identity,
                    "resume": True,
                },
            )
        assert local_only.status_code == 200, local_only.text
        assert starts["local-shop"] == 2
        assert "local-shop" not in running
        assert local_only.json()["worker"]["code"] == "local_login_only"

        # Both local launcher modes omit the consumer. A refresh requested by
        # the browser must still fetch and persist fresh SKU data immediately.
        assert client.post("/api/bot/stop", headers=headers).status_code == 200
        sync_account = app.db.get_shop_account(user_id, account_key="local-shop")
        refreshed = fake_shop_sync(local_only_cookie)
        refreshed.update(synced_at="2026-10-25T01:00:00+0800", product_count=1)
        refreshed["products"] = [{
            "id": "100001", "title": "新多规格商品", "status": "在售",
            "quantity": 2,
            "skus": [{"sku_id": "600001", "name": "6S 16G WIFI",
                      "price": "128.00", "stock": 2, "properties": []}],
        }]
        for mode in ("SAAS_LOCAL_LOGIN_ONLY", "SAAS_LOCAL_OBSERVE_ONLY"):
            refresh_cookie = local_only_cookie + "; test_mode=" + mode
            app.write_secret(user_id, "cookies.txt", refresh_cookie, "local-shop")
            with (
                patch.dict(os.environ, {"SAAS_LOCAL_LOGIN_ONLY": "0",
                                        "SAAS_LOCAL_OBSERVE_ONLY": "0", mode: "1"}),
                patch.object(app, "sync_shop", return_value=refreshed) as fetch,
                patch.object(app, "_enqueue_shop_refresh", side_effect=AssertionError("no local consumer")),
            ):
                response = client.post("/api/bot/shop/sync", headers={**headers, "Prefer": "respond-async"})
            assert response.status_code == 200, response.text
            assert response.json()["synced_at"] == refreshed["synced_at"]
            assert response.json()["product_count"] == 1
            assert "job" not in response.json()
            fetch.assert_called_once_with(refresh_cookie)
            products = client.get("/api/bot/products", headers=headers).json()["products"]
            assert products[0]["skus"][0]["price"] == "128.00"
            assert products[0]["skus"][0]["stock"] == 2
            assert "local-shop" not in running
            assert starts["local-shop"] == 2
            assert app.db.get_worker_runtime(user_id, sync_account["id"])["desired_state"] == "stopped"
        app.write_secret(user_id, "cookies.txt", local_only_cookie, "local-shop")

        create_account(client, "unknown-shop")
        unknown_headers = {"X-Shop-Account": "unknown-shop"}
        candidate = "unb=620003; _m_h5_tk=candidate-token_tail; sid=candidate"
        initial_status = client.get("/api/bot/status", headers=unknown_headers)
        assert initial_status.status_code == 200, initial_status.text
        assert initial_status.json()["local_login_import"]["initial_bind_allowed"] is True
        unknown = client.put(
            "/api/bot/cookies",
            headers=unknown_headers,
            json={
                "cookies": candidate,
                "expected_account_ref": shop_sync.account_ref(shop_sync.parse_cookie_header(candidate)[1]),
                "resume": True,
            },
        )
        assert unknown.status_code == 409, unknown.text
        assert unknown.json()["detail"]["code"] == "account_identity_unknown"
        assert not app.read_secret(user_id, "cookies.txt", "unknown-shop")
        initial = client.put(
            "/api/bot/cookies",
            headers=unknown_headers,
            json={
                "cookies": candidate,
                "expected_account_ref": shop_sync.account_ref(shop_sync.parse_cookie_header(candidate)[1]),
                "resume": True,
                "allow_initial_bind": True,
            },
        )
        assert initial.status_code == 200, initial.text
        assert initial.json()["identity_fingerprint"] == shop_sync.account_ref(
            shop_sync.parse_cookie_header(candidate)[1]
        )
        assert app.read_secret(user_id, "cookies.txt", "unknown-shop") == candidate

        # Legacy installations may have the identity in cookies.txt but not in
        # the account row; status derives only the bounded fingerprint.
        legacy_account = app.db.get_shop_account(user_id, account_key="local-shop")
        with app.db._lock:
            app.db.con.execute(
                "UPDATE shop_accounts SET account_ref = '' WHERE id = ?", (legacy_account["id"],)
            )
            app.db.con.commit()
        fallback = client.get("/api/bot/status", headers=headers)
        assert fallback.status_code == 200, fallback.text
        assert fallback.json()["account"]["identity_fingerprint"] == identity

        local_login = load_local_login_script()
        assert "playwright" not in (ROOT / "scripts/sync-local-login.py").read_text()
        assert not (ROOT / "scripts/requirements-local-login.txt").exists()
        try:
            local_login.server_address("http://example.test/xianyu-saas")
        except local_login.SyncError:
            pass
        else:
            raise AssertionError("HTTP workbench address must be rejected")
        try:
            local_login.server_address("https://example.test/xianyu-saas?token=secret")
        except local_login.SyncError:
            pass
        else:
            raise AssertionError("query-bearing workbench address must be rejected")

    print("local-login-contract: identity binding, replacement rollback, resume and local tool guards passed")


if __name__ == "__main__":
    main()
