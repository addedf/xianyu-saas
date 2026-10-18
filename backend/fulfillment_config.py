"""Strict, account-local fulfillment configuration shared by manual APIs and tools.

Only products_config.json is written here. Resources and redeem inventory are
read-only inputs; template updates never mutate their source resources.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
from urllib.parse import urlparse

from account_storage import AccountStorageError, normalize_account_key
from automation import AutomationValidationError, _clean_text, _safe_item_id, MAX_MATERIAL_CHARS

PRODUCTS_FILE = "products_config.json"
CARD_POOLS_FILE = "card_pools.json"
RECEIPT_FIELD = "_ops_receipt"
READ_FILES = frozenset({PRODUCTS_FILE, CARD_POOLS_FILE, "shop_snapshot.json", "reply_rules.json", "pan_links.json", "redeem_codes.json", "card_pool.json", "delivery_state.db"})
MAX_FILE_BYTES = 32 * 1024 * 1024  # Explicit I/O protection, never a product-count quota.


class FulfillmentConfigError(ValueError):
    def __init__(self, code="invalid_arguments", message=None, status_code=400):
        self.code, self.status_code = code, status_code
        super().__init__(message or {"storage_unavailable": "配置文件不可安全读取，请在手动页面核对，未重置原文件", "revision_conflict": "配置已变化，请重新读取后操作", "clarification_required": "缺少可用资料或明确的覆盖范围，请补充说明", "invalid_arguments": "履约配置字段无效"}.get(code, "履约配置未保存"))


def card_pool_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise FulfillmentConfigError(message="请选择有效的卡密池")
    return value


def card_documents(catalog, codes, legacy_meta=None):
    """Validate the private catalog and each card's pool ownership."""
    try:
        if codes is not None and not isinstance(codes, list):
            raise ValueError("invalid inventory")
        manifest, seen = [], set()
        for row in codes or []:
            if (not isinstance(row, dict) or not isinstance(row.get("code"), str)
                    or not row["code"].strip() or len(row["code"].strip()) > 512
                    or type(row.get("used", False)) is not bool):
                raise ValueError("invalid inventory entry")
            secret = row["code"].strip()
            if secret in seen:
                raise ValueError("duplicate inventory entry")
            seen.add(secret)
            manifest.append({**row, "code": secret, "used": row.get("used", False),
                             "pool_id": card_pool_id(row.get("pool_id", "redeem"))})
        if catalog is None:
            pools = []
            if manifest or legacy_meta is not None:
                if legacy_meta is not None and not isinstance(legacy_meta, dict):
                    raise ValueError("invalid legacy pool")
                pools = [{"id": "redeem", "name": (legacy_meta or {}).get("name", "兑换码池"),
                          "note": (legacy_meta or {}).get("note", "")}]
        else:
            if not isinstance(catalog, dict) or catalog.get("version") != 1 or not isinstance(catalog.get("pools"), list):
                raise ValueError("invalid pool catalog")
            pools = copy.deepcopy(catalog["pools"])
        ids, names = set(), set()
        for pool in pools:
            if not isinstance(pool, dict):
                raise ValueError("invalid pool")
            identifier = card_pool_id(pool.get("id"))
            name, note = pool.get("name"), pool.get("note", "")
            if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 120
                    or not isinstance(note, str) or len(note.strip()) > 500
                    or "\x00" in name + note or identifier in ids or name.strip() in names):
                raise ValueError("invalid pool metadata")
            pool.update(id=identifier, name=name.strip(), note=note.strip())
            ids.add(identifier)
            names.add(name.strip())
        if any(row["pool_id"] not in ids for row in manifest):
            raise ValueError("inventory references missing pool")
        return pools, manifest
    except (ValueError, TypeError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def without_receipt(document):
    return {key: value for key, value in document.items() if key != RECEIPT_FIELD} if isinstance(document, dict) else document


def validate_receipt(document):
    if not isinstance(document, dict) or RECEIPT_FIELD not in document:
        return
    receipt = document[RECEIPT_FIELD]
    if (not isinstance(receipt, dict) or set(receipt) != {"plan_id", "item_id", "digest", "after_hash"}
            or any(not isinstance(receipt[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", receipt[key]) for key in ("plan_id", "item_id"))
            or any(not isinstance(receipt[key], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key]) for key in ("digest", "after_hash"))
            or digest(without_receipt(document)) != receipt["after_hash"]):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)


def strict_json(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("non-finite JSON number")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc


def account_path(storage, user_id, account_key, name, item_id=None):
    """Resolve only fixed business files, rejecting symlinks and Windows junctions."""
    try:
        key = normalize_account_key(account_key)
        if name == "ai_knowledge":
            path = storage.account_dir(user_id, key) / name / (_safe_item_id(item_id) + ".json")
        elif name in READ_FILES:
            path = storage.account_dir(user_id, key) / name
        else:
            raise ValueError("file is not allowed")
        current = storage.root
        for part in path.relative_to(storage.root).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or (current != path and not stat.S_ISDIR(info.st_mode))):
                raise ValueError("unsafe path")
        return path
    except (OSError, ValueError, AccountStorageError, AutomationValidationError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc


def read_json(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("unsafe file size or type")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file size exceeds safe I/O bound")
        result = strict_json(data.decode("utf-8"))
        if result is None:
            # Only FileNotFoundError means an absent document; JSON null is corrupt configuration.
            raise ValueError("null configuration document")
        validate_receipt(result)
        return result
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def item_ids(raw):
    values = raw.get("item_ids")
    if values is None:
        values = [raw["item_id"]] if raw.get("item_id") is not None else []
    if not isinstance(values, list):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)
    try:
        clean = [_safe_item_id(value) for value in values]
    except AutomationValidationError as exc:
        raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
    if len(set(clean)) != len(clean):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)
    return clean


def template_sku_id(raw):
    value = raw.get("sku_id")
    if value is None or value == "":
        return ""
    try:
        return _safe_item_id(value)
    except AutomationValidationError as exc:
        raise FulfillmentConfigError(message="SKU ID 格式无效") from exc


def products_document(value):
    if value is None:
        return {"version": 1, "types": []}
    if not isinstance(value, dict) or not isinstance(value.get("types"), list):
        raise FulfillmentConfigError("storage_unavailable", status_code=503)
    validate_receipt(value)
    for entry in value["types"]:
        if not isinstance(entry, dict) or entry.get("delivery") not in {"material", "pan", "redeem"} or type(entry.get("enabled", True)) is not bool:
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        try:
            sku_id = template_sku_id(entry)
        except FulfillmentConfigError as exc:
            raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
        if sku_id and (entry["delivery"] not in {"pan", "redeem"} or len(item_ids(entry)) != 1):
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        item_ids(entry)
        if entry["delivery"] == "redeem":
            try:
                card_pool_id(entry.get("card_pool_id", "redeem"))
            except FulfillmentConfigError as exc:
                raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
    return copy.deepcopy(value)


def has_enabled_delivery(document, snapshot):
    """Whether a verified shop has a configured, enabled delivery listener.

    Inventory availability is checked per order; temporary stock exhaustion
    must not disable the listener before the owner replenishes it.
    """
    products = products_document(document)
    available = {
        str(row.get("id"))
        for row in (snapshot or {}).get("products", [])
        if isinstance(row, dict)
    }
    for entry in products["types"]:
        if entry.get("enabled", True) is not True or not available.intersection(item_ids(entry)):
            continue
        if entry["delivery"] == "material":
            payload = entry.get("payload", entry.get("material", ""))
            if isinstance(payload, str) and payload.strip():
                return True
        elif entry["delivery"] == "pan":
            tags = entry.get("resource_match")
            if isinstance(tags, list) and tags and all(isinstance(tag, str) and tag.strip() for tag in tags):
                return True
        elif entry["delivery"] == "redeem":
            return True
    return False


def normalise_template_item_ids(raw, snapshot, preserved_item_ids=None):
    if not isinstance(raw, dict):
        raise FulfillmentConfigError()
    try:
        selected = item_ids(raw)
    except FulfillmentConfigError as exc:
        raise FulfillmentConfigError() from exc
    available = {str(row.get("id")) for row in (snapshot or {}).get("products", []) if isinstance(row, dict)}
    preserved = {str(value) for value in (preserved_item_ids or [])} if (snapshot or {}).get("truncated") else set()
    if set(selected) - available - preserved:
        raise FulfillmentConfigError(message="只能绑定当前店铺已识别的商品")
    return selected


def snapshot_contains_sku(snapshot, item_id, sku_id) -> bool:
    for product in (snapshot or {}).get("products", []):
        if not isinstance(product, dict) or str(product.get("id")) != item_id:
            continue
        skus = product.get("skus")
        return isinstance(skus, list) and any(
            isinstance(sku, dict) and str(sku.get("sku_id")) == sku_id
            for sku in skus
        )
    return False


def normalise_template_input(raw, snapshot, preserved_item_ids=None):
    if not isinstance(raw, dict):
        raise FulfillmentConfigError()
    try:
        name = _clean_text(raw.get("name"), limit=120, field="模板名称", allow_newlines=False)
    except AutomationValidationError as exc:
        raise FulfillmentConfigError(message=str(exc)) from exc
    delivery = raw.get("delivery")
    if delivery not in {"redeem", "pan"}:
        raise FulfillmentConfigError(message="delivery 只能是 redeem/pan")
    enabled = raw.get("enabled", True)
    enabled = True if enabled is None else enabled
    if type(enabled) is not bool:
        raise FulfillmentConfigError()
    result = {"name": name, "delivery": delivery, "enabled": enabled,
              "item_ids": normalise_template_item_ids(raw, snapshot, preserved_item_ids)}
    sku_id = template_sku_id(raw)
    if sku_id:
        if len(result["item_ids"]) != 1 or not snapshot_contains_sku(snapshot, result["item_ids"][0], sku_id):
            raise FulfillmentConfigError(message="一个 SKU 模板只能绑定当前商品的一个有效规格")
        result["sku_id"] = sku_id
    for key, limit in (("description", 500), ("price", 120)):
        if raw.get(key) is not None:
            value = raw[key]
            if not isinstance(value, str) or len(value.strip()) > limit or "\x00" in value:
                raise FulfillmentConfigError()
            result[key] = value.strip()
    if delivery == "pan":
        tags = raw.get("resource_match")
        if not isinstance(tags, list) or not tags or len(tags) > 32 or any(not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 120 for tag in tags):
            raise FulfillmentConfigError(message="网盘模板必须配置有效资源匹配标签")
        result["resource_match"] = list(dict.fromkeys(tag.strip() for tag in tags))
    else:
        result["card_pool_id"] = card_pool_id(raw.get("card_pool_id") if raw.get("card_pool_id") is not None else "redeem")
    return result


def template_public(item):
    result = {key: copy.deepcopy(item[key]) for key in ("id", "name", "description", "price", "delivery", "resource_match", "card_pool_id", "sku_id") if key in item}
    if item.get("delivery") == "redeem":
        result["card_pool_id"] = item.get("card_pool_id", "redeem")
    result.update(item_ids=item_ids(item), enabled=item.get("enabled", True))
    result["item_count"] = len(result["item_ids"])
    if item.get("payload") or item.get("material"):
        result["payload_set"] = True
    return result


def _bindings_conflict(first, second) -> bool:
    if not set(item_ids(first)).intersection(item_ids(second)):
        return False
    first_sku = template_sku_id(first)
    second_sku = template_sku_id(second)
    return not first_sku or not second_sku or first_sku == second_sku


def upsert_template(document, raw, snapshot, *, new_id):
    result = without_receipt(products_document(document))
    template_id = str(raw.get("id") or "").strip()
    matches = [pos for pos, row in enumerate(result["types"]) if row.get("delivery") in {"redeem", "pan"} and str(row.get("id") or "") == template_id]
    if len(matches) > 1:
        raise FulfillmentConfigError("clarification_required")
    position = matches[0] if matches else None
    previous = result["types"][position] if position is not None else {}
    template_input = dict(raw)
    if template_input.get("card_pool_id") is None and previous.get("delivery") == "redeem":
        template_input["card_pool_id"] = previous.get("card_pool_id", "redeem")
    clean = normalise_template_input(template_input, snapshot, item_ids(previous))
    saved = {**previous, **clean, "id": template_id if position is not None else new_id}
    # The manual endpoint treats omission/null of these known optional fields
    # as clearing them. Preserve only unknown extensions by default here.
    for field in ("description", "price"):
        if field not in clean:
            saved.pop(field, None)
    saved.pop("item_id", None)
    if "sku_id" not in clean:
        saved.pop("sku_id", None)
    if saved["delivery"] != "pan":
        saved.pop("resource_match", None)
    else:
        saved.pop("card_pool_id", None)
    if any(_bindings_conflict(saved, row) for index, row in enumerate(result["types"]) if index != position):
        raise FulfillmentConfigError("clarification_required", "商品或 SKU 已绑定其他模板，请明确处理绑定冲突")
    if position is None:
        result["types"].append(saved)
    else:
        result["types"][position] = saved
    return result, saved


def remove_template(document, template_id):
    result = without_receipt(products_document(document))
    retained = [row for row in result["types"] if not (row.get("delivery") in {"redeem", "pan"} and str(row.get("id") or "") == template_id)]
    if len(retained) == len(result["types"]):
        raise FulfillmentConfigError("not_found", "发货模板不存在", 404)
    result["types"] = retained
    return result


def target_config(document, item_id):
    matches = [row for row in products_document(document)["types"] if item_id in item_ids(row)]
    if len(matches) > 1:
        raise FulfillmentConfigError("clarification_required", "商品同时绑定多份配置，请先核对绑定冲突")
    return copy.deepcopy(matches[0]) if matches else None


def configure_target(document, item_id, title, delivery, *, enabled, stable_id, material=None, resource=None, replace_existing=False):
    """Copy-on-write one product binding; preserve non-target mappings and metadata."""
    result = without_receipt(products_document(document))
    old = target_config(result, item_id)
    if old and template_sku_id(old):
        raise FulfillmentConfigError("clarification_required", "多规格商品请按 SKU 使用发货模板配置")
    if type(enabled) is not bool or delivery not in {"material", "pan", "redeem"}:
        raise FulfillmentConfigError()
    if old and old["delivery"] != delivery and not replace_existing:
        raise FulfillmentConfigError("clarification_required", "商品已有不同类型的发货配置，请明确是否替换")
    saved = copy.deepcopy(old or {})
    identifier = old.get("id") if old and len(item_ids(old)) == 1 else stable_id
    saved.update(id=identifier or stable_id, name=saved.get("name") or title, item_ids=[item_id], delivery=delivery, enabled=enabled)
    saved.pop("item_id", None)
    # Replacing the type does not delete unknown extensions or price metadata.
    if delivery != "pan":
        saved.pop("resource_match", None)
    if delivery != "redeem":
        saved.pop("card_pool_id", None)
    if delivery != "material":
        saved.pop("payload", None)
        saved.pop("material", None)
    if delivery == "material":
        if material is None and resource and resource.get("delivery") == "material":
            material = resource.get("material")
        if material is None and old and old["delivery"] == delivery:
            material = old.get("payload", old.get("material"))
        try:
            saved["payload"] = _clean_text(material, limit=MAX_MATERIAL_CHARS, field="自动发送资料")
        except AutomationValidationError as exc:
            raise FulfillmentConfigError("clarification_required", "请提供明确的文字资料，或绑定已有文字资料资源") from exc
        saved.pop("material", None)
    elif delivery == "pan":
        if not resource or resource.get("delivery") != "pan" or not resource.get("available"):
            if not (not enabled and old and old["delivery"] == "pan"):
                raise FulfillmentConfigError("clarification_required", "未找到可绑定的网盘资料，请选择现有资源或通过手动页面补充")
        else:
            saved["resource_match"] = list(resource["tags"])
    elif not resource or resource.get("delivery") != "redeem" or not resource.get("available"):
        if not (not enabled and old and old["delivery"] == "redeem"):
            raise FulfillmentConfigError("clarification_required", "当前没有可用卡密库存，请在手动页面补充库存")
    else:
        saved["card_pool_id"] = card_pool_id(resource.get("card_pool_id", "redeem"))
    if old:
        comparable = {**saved, "id": old.get("id")}
        original = {**old, "id": old.get("id"), "item_ids": [item_id], "enabled": old.get("enabled", True)}
        original.pop("item_id", None)
        if comparable == original:
            # An unchanged setting does not need a new binding, even when the
            # source template is shared. Split only for an actual target edit.
            return result, old
    retained = []
    for row in result["types"]:
        ids = item_ids(row)
        if item_id not in ids:
            retained.append(row)
        elif len(ids) > 1:
            remainder = copy.deepcopy(row)
            remainder["item_ids"] = [value for value in ids if value != item_id]
            remainder.pop("item_id", None)
            retained.append(remainder)
    retained.append(saved)
    result["types"] = retained
    return result, saved


class FulfillmentConfig:
    def __init__(self, storage):
        self.storage = storage

    def read_products(self, user_id, account_key):
        return products_document(read_json(account_path(self.storage, user_id, account_key, PRODUCTS_FILE)))

    def read_cards(self, user_id, account_key):
        root_args = (self.storage, user_id, account_key)
        catalog = read_json(account_path(*root_args, CARD_POOLS_FILE))
        codes = read_json(account_path(*root_args, "redeem_codes.json"))
        legacy_meta = read_json(account_path(*root_args, "card_pool.json")) if catalog is None else None
        return card_documents(catalog, codes, legacy_meta)

    def cards_payload(self, user_id, account_key, *, pools=None, codes=None, selected_pool_id=None, inventory=None):
        if pools is None or codes is None:
            pools, codes = self.read_cards(user_id, account_key)
        inventory = self._inventory_records(user_id, account_key) if inventory is None else inventory
        public = [{"id": pool["id"], "name": pool["name"], "note": pool["note"], "enabled": True,
                   "total": 0, "available": 0, "reserved": 0, "used": 0, "quarantined": 0} for pool in pools]
        by_id = {pool["id"]: pool for pool in public}
        for entry in codes:
            pool = by_id[entry["pool_id"]]
            durable = inventory.get(entry["code"])
            status = durable["status"] if durable else "available"
            if durable and durable["pool_id"] != entry["pool_id"]:
                status = "quarantined"
            if entry["used"] and status == "available":
                status = "legacy_used"
            elif entry["used"] and status == "reserved":
                status = "quarantined"
            bucket = {"available": "available", "reserved": "reserved", "delivered": "used", "legacy_used": "used"}.get(status, "quarantined")
            pool["total"] += 1
            pool[bucket] += 1
        stats = {key: sum(pool[key] for pool in public) for key in ("total", "available", "reserved", "used", "quarantined")}
        stats["pools"] = len(public)
        selected = next((pool for pool in public if pool["id"] == selected_pool_id), None)
        if selected is None and len(public) == 1:
            selected = public[0]
        return {"pools": public, "pool": selected, "stats": stats}

    def write_products(self, user_id, account_key, document, *, expected_revision=None, ensure_current=None, receipt=None):
        clean = without_receipt(products_document(document))
        if receipt is not None:
            clean[RECEIPT_FIELD] = copy.deepcopy(receipt)
            validate_receipt(clean)
        if ensure_current is not None and ensure_current() is False:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        current = self.read_products(user_id, account_key)
        if expected_revision is not None and digest(current) != expected_revision:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        path = account_path(self.storage, user_id, account_key, PRODUCTS_FILE)
        self.storage.ensure_account_dir(user_id, account_key)
        if ensure_current is not None and ensure_current() is False:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        if expected_revision is not None and digest(self.read_products(user_id, account_key)) != expected_revision:
            raise FulfillmentConfigError("revision_conflict", status_code=409)
        self.storage.atomic_write_path(path, json_text(clean).encode("utf-8"))
        return clean

    def resources(self, user_id, account_key):
        """Private descriptors. Tools expose an explicit safe projection, not this list."""
        root_args = (self.storage, user_id, account_key)
        products = self.read_products(user_id, account_key)
        pan = read_json(account_path(*root_args, "pan_links.json"))
        if pan is not None and (not isinstance(pan, dict) or not isinstance(pan.get("links"), list)):
            raise FulfillmentConfigError("storage_unavailable", status_code=503)
        groups = {}
        for entry in (pan or {}).get("links", []):
            if not isinstance(entry, dict):
                raise FulfillmentConfigError("storage_unavailable", status_code=503)
            if entry.get("used") is True:
                continue
            url, code, remark, tags = (entry.get(key) for key in ("url", "code", "remark", "match"))
            try:
                parsed = urlparse(url)
                valid = (isinstance(url, str) and len(url) <= 2048 and parsed.scheme == "https" and parsed.hostname and not parsed.username
                         and isinstance(code, str) and 0 < len(code.strip()) <= 64 and isinstance(remark, str) and 0 < len(remark.strip()) <= 512
                         and isinstance(tags, list) and tags and all(isinstance(tag, str) and tag.strip() for tag in tags))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise FulfillmentConfigError("storage_unavailable", status_code=503)
            key = tuple(sorted(set(tag.strip() for tag in tags)))
            groups.setdefault(key, []).append(entry)
        resources = []
        for tags, entries in sorted(groups.items()):
            resources.append({"key": "pan:" + digest(tags), "kind": "pan", "delivery": "pan", "name": " / ".join(entry["remark"] for entry in entries),
                              "tags": list(tags), "available": True, "count": len(entries), "version": digest(entries)})
        pools, codes = self.read_cards(user_id, account_key)
        inventory = self._inventory_records(user_id, account_key)
        stock = self.cards_payload(user_id, account_key, pools=pools, codes=codes, inventory=inventory)
        for pool in stock["pools"]:
            manifest = [entry for entry in codes if entry["pool_id"] == pool["id"]]
            states = {entry["code"]: inventory.get(entry["code"]) for entry in manifest}
            resources.append({"key": "inventory:" + pool["id"], "kind": "inventory", "delivery": "redeem", "name": pool["name"],
                              "card_pool_id": pool["id"], "tags": [], "available": pool["available"] > 0,
                              "count": pool["available"], "version": digest([pool, manifest, states])})
        for position, row in enumerate(products["types"]):
            delivery = row["delivery"]
            resource = {"key": f"template:{position}", "kind": "template", "delivery": delivery, "name": row.get("name", "已有发货模板"),
                        "tags": row.get("resource_match", []) if delivery == "pan" else [], "version": digest(row), "available": True}
            if delivery == "material":
                resource["material"] = row.get("payload", row.get("material", ""))
                resource["available"] = isinstance(resource["material"], str) and bool(resource["material"].strip())
            elif delivery == "pan":
                group = groups.get(tuple(sorted(set(resource["tags"]))))
                resource["available"] = bool(group)
                resource["version"] = digest([row, group])
            else:
                resource["card_pool_id"] = row.get("card_pool_id", "redeem")
                pool = next((item for item in resources if item["kind"] == "inventory" and item["card_pool_id"] == resource["card_pool_id"]), None)
                resource["available"] = bool(pool and pool["available"])
                resource["version"] = digest([row, pool])
            resources.append(resource)
        return resources

    def _inventory_states(self, user_id, account_key):
        return {secret: entry["status"] for secret, entry in self._inventory_records(user_id, account_key).items()}

    def _inventory_records(self, user_id, account_key):
        path = account_path(self.storage, user_id, account_key, "delivery_state.db")
        if not path.exists():
            return {}
        con = None
        try:
            # Read-only SQLite; never create/initialize the Worker's private DB.
            # Keep WAL visibility: immutable reads could miss a used/reserved code.
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            con.execute("PRAGMA query_only = ON")
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory'").fetchone()
            if not exists:
                return {}
            columns = {row[1] for row in con.execute("PRAGMA table_info(inventory)")}
            pool_column = "pool_id" if "pool_id" in columns else "'redeem'"
            return {row[0]: {"status": row[1], "pool_id": row[2]}
                    for row in con.execute(f"SELECT secret,status,{pool_column} FROM inventory WHERE kind='redeem'")}
        except sqlite3.Error as exc:
            raise FulfillmentConfigError("storage_unavailable", status_code=503) from exc
        finally:
            if con is not None:
                con.close()
