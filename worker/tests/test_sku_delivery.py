"""Synthetic SKU orders: exact pools, retries, drift and platform rechecks."""
import sqlite3
import time
import unittest

from delivery_store import DeliveryStore, DeliveryStoreError
from main import OrderVerificationRejected, XianyuLive
from tests import test_delivery_security as fixtures
from tests.test_delivery_security import API_ITEM_ID, BUYER_ID, ORDER_ID, SESSION_ID, paid_event, write_json


class SkuDeliveryTest(unittest.IsolatedAsyncioTestCase):
    automation_mode = "rules"
    asyncSetUp = fixtures.AgentTestCase.asyncSetUp
    asyncTearDown = fixtures.AgentTestCase.asyncTearDown
    bind = fixtures.AgentTestCase.bind

    def configure_skus(self):
        self.configs = [{"id": "template-" + str(i), "item_ids": [API_ITEM_ID],
                         "sku_id": str(6000 + i), "delivery": "redeem",
                         "card_pool_id": "pool-" + str(i)} for i in range(5)]
        write_json(self.products_path, {"types": self.configs})
        write_json(self.state_dir / "redeem_codes.json", [
            {"code": f"SYNTHETIC-{i}-{n}", "used": False, "pool_id": "pool-" + str(i)}
            for i in range(5) for n in range(3)
        ])
        self.agent._refresh_runtime_config(force=True)
        self.bind()

    async def pay(self):
        return await self.agent.handle_paid_order(paid_event(SESSION_ID, int(time.time() * 1000)))

    def order(self):
        return self.agent.delivery_store.get_order(self.agent._canonical_order_key(self.api.order_id))

    async def test_all_five_skus_send_only_their_own_pool_and_are_idempotent(self):
        self.configure_skus()
        for i in range(5):
            self.api.order_id = self.api.head_order_id = str(5005 + i)
            self.api.order_sku_id = str(6000 + i)
            await self.pay()
            order = self.order()
            self.assertEqual((order.status, order.sku_id, order.pool_id),
                             ("delivered", str(6000 + i), "pool-" + str(i)))
            self.assertEqual(order.resources, (f"SYNTHETIC-{i}-0",))
            await self.pay()
        self.assertEqual(self.agent.send_text_reliably.await_count, 5)

    async def test_missing_unknown_or_malformed_sku_never_consumes_inventory(self):
        self.configure_skus()
        for i, sku in enumerate([None, "9999", True, "bad", "6000 "]):
            self.api.order_id = self.api.head_order_id = str(5100 + i)
            self.api.order_sku_id = sku
            await self.pay()
            self.assertEqual(self.order().status, "manual_review")
            self.assertEqual(self.order().resources, ())
        self.agent.send_text_reliably.assert_not_awaited()

    async def test_multi_quantity_uses_one_pool_atomically(self):
        self.configure_skus()
        self.api.order_sku_id = "6001"
        self.api.order_quantity = 2
        await self.pay()
        self.assertEqual(self.order().resources, ("SYNTHETIC-1-0", "SYNTHETIC-1-1"))

    async def test_insufficient_sku_stock_does_not_borrow_from_other_pools(self):
        self.configure_skus()
        self.api.order_sku_id = "6001"
        self.api.order_quantity = 4
        await self.pay()
        self.assertEqual((self.order().status, self.order().reason), ("manual_review", "inventory_empty"))
        self.agent.send_text_reliably.assert_not_awaited()

    async def test_sku_changes_at_final_platform_check_quarantines_cards(self):
        self.configure_skus()
        self.api.order_sku_id = "6000"
        original = self.api.get_order_detail

        def changing_detail(order_id):
            if self.api.detail_calls:
                self.api.order_sku_id = "6001"
            return original(order_id)

        self.api.get_order_detail = changing_detail
        await self.pay()
        self.assertEqual((self.order().status, self.order().reason), ("manual_review", "order_sku_mismatch"))
        self.assertEqual(self.api.consign_calls, 0)
        self.assertFalse(self.agent.delivery_store.order_inventory_is_sendable(self.order().key))

    async def test_config_changes_while_platform_recheck_is_inflight_block_sending(self):
        self.configure_skus()
        self.api.order_sku_id = "6000"
        original = self.agent._reverify_order

        async def change_pool(reservation):
            await original(reservation)
            self.configs[0]["card_pool_id"] = "pool-1"
            write_json(self.products_path, {"types": self.configs})

        self.agent._reverify_order = change_pool
        await self.pay()
        self.assertEqual(self.order().reason, "card_pool_changed")
        self.assertEqual(self.api.consign_calls, 0)

    async def test_reconnect_resumes_original_sku_and_cards(self):
        self.configure_skus()
        self.api.order_sku_id = "6002"
        self.agent.send_text_reliably.side_effect = ConnectionError("synthetic disconnect")
        with self.assertRaises(ConnectionError):
            await self.pay()
        initial = self.order()
        self.assertEqual(initial.status, "retry")
        # Retry backoff is bypassed only in this synthetic test database.
        with sqlite3.connect(self.agent.delivery_db_file) as conn:
            conn.execute("UPDATE delivery_events SET updated_at = 0 WHERE order_key = ?", (initial.key,))
        async def acknowledge(*args, **kwargs):
            await kwargs["before_attempt"]()
        self.agent.send_text_reliably.side_effect = acknowledge
        await self.agent.retry_pending_deliveries()
        self.assertEqual(self.order().status, "delivered")
        self.assertEqual(self.order().resources, initial.resources)
        self.assertEqual(self.order().sku_id, "6002")

    async def test_sku_drift_on_repeated_paid_notice_is_manual(self):
        self.configure_skus()
        self.api.order_sku_id = "6000"
        self.agent.send_text_reliably.side_effect = ConnectionError("synthetic disconnect")
        with self.assertRaises(ConnectionError):
            await self.pay()
        self.api.order_sku_id = "6001"
        await self.pay()
        self.assertEqual(self.order().reason, "order_sku_mismatch")
        self.assertEqual(self.order().pool_id, "pool-0")

    async def test_pending_scan_routes_by_verified_detail_sku(self):
        self.configure_skus()
        self.api.order_sku_id = "6004"
        await self.agent._reconcile_pending_order({"order_id": ORDER_ID, "item_id": API_ITEM_ID,
            "buyer_id": BUYER_ID, "in_refund": False, "paid_amount": "5", "quantity": 1})
        self.assertEqual((self.order().status, self.order().pool_id), ("delivered", "pool-4"))

    async def test_conflicting_template_bindings_revoke_automatic_delivery(self):
        self.configure_skus()
        self.configs.append({"id": "whole", "item_ids": [API_ITEM_ID], "delivery": "redeem"})
        write_json(self.products_path, {"types": self.configs})
        self.agent._refresh_products()
        self.assertEqual(self.agent.products, {})
        self.assertEqual(self.agent.sku_products, {})

    async def test_real_sku_cannot_fall_back_to_whole_product_template(self):
        self.bind()
        self.api.order_sku_id = "6000"
        await self.pay()
        self.assertEqual(self.order().reason, "unsupported_sku")
        self.agent.send_text_reliably.assert_not_awaited()

    async def test_parser_rejects_conflicting_platform_sku_ids(self):
        self.api.order_sku_id = "6000"
        response = self.api.get_order_detail(ORDER_ID)
        response["data"]["skuId"] = "6001"
        with self.assertRaises(OrderVerificationRejected):
            XianyuLive._parse_order_detail(response)

    async def test_store_refuses_changing_verified_sku_and_quarantines_reservation_drift(self):
        self.configure_skus()
        store = self.agent.delivery_store
        key = self.agent._canonical_order_key(ORDER_ID)
        proof = dict(platform_order_id=ORDER_ID, platform_status="2", paid_amount="5")
        store.record_verified_payment_event(key, SESSION_ID, time.time(), 100, sku_id="6000", **proof)
        with self.assertRaises(DeliveryStoreError):
            store.record_verified_payment_event(key, SESSION_ID, time.time(), 100, sku_id="6001", **proof)
        store.prepare_order(key, SESSION_ID, BUYER_ID, API_ITEM_ID, "redeem", sku_id="6000", pool_id="pool-0")
        drift = store.prepare_order(key, SESSION_ID, BUYER_ID, API_ITEM_ID, "redeem", sku_id="6001", pool_id="pool-1")
        self.assertEqual((drift.status, drift.reason, drift.sku_id), ("manual_review", "order_sku_mismatch", "6000"))

    async def test_existing_database_adds_nullable_sku_without_altering_delivery_state(self):
        store = self.agent.delivery_store
        store.record_verified_payment_event("old", SESSION_ID, time.time(), 100,
            platform_order_id=ORDER_ID, platform_status="2", paid_amount="5")
        with sqlite3.connect(self.agent.delivery_db_file) as conn:
            conn.execute("ALTER TABLE delivery_events DROP COLUMN sku_id")
        reopened = DeliveryStore(self.agent.delivery_db_file)
        self.assertEqual(reopened.get_order("old").status, "verified")
        self.assertIsNone(reopened.get_order("old").sku_id)
