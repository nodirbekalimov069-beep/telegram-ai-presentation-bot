import base64
import hashlib
import json
from urllib.parse import urlencode

import pytest

from core.config import Settings
from modules.payments.factory import build_payment_provider
from modules.payments.providers.click import ClickProvider, InvalidClickRequest
from modules.payments.providers.payme import InvalidPaymeRequest, PaymeProvider


def signed_click_body(*, secret: str, action: str = "0", amount: str = "3000") -> bytes:
    fields = {
        "click_trans_id": "12345",
        "service_id": "77",
        "click_paydoc_id": "999",
        "merchant_trans_id": "order-public-id",
        "amount": amount,
        "action": action,
        "error": "0",
        "error_note": "Success",
        "sign_time": "2026-08-04 15:00:00",
    }
    preimage = (
        fields["click_trans_id"]
        + fields["service_id"]
        + secret
        + fields["merchant_trans_id"]
        + fields["amount"]
        + fields["action"]
        + fields["sign_time"]
    )
    fields["sign_string"] = hashlib.md5(preimage.encode()).hexdigest()  # noqa: S324
    return urlencode(fields).encode()


def test_disabled_payment_factory_fails_closed_without_building_provider() -> None:
    with pytest.raises(RuntimeError, match="payment provider is disabled"):
        build_payment_provider(Settings(app_env="test", payment_provider="disabled"))


def test_click_prepare_signature_and_integer_uzs_are_verified() -> None:
    provider = ClickProvider(
        merchant_id="merchant",
        service_id="77",
        secret_key="click-secret",
    )
    command = provider.authenticate_and_decode(signed_click_body(secret="click-secret"))
    assert command.operation == "PREPARE"
    assert command.order_public_id == "order-public-id"
    assert command.amount_uzs == 3000


@pytest.mark.parametrize("amount", ["3000.50", "-1", "NaN"])
def test_click_rejects_non_integer_or_invalid_amount(amount: str) -> None:
    provider = ClickProvider(merchant_id="merchant", service_id="77", secret_key="click-secret")
    with pytest.raises(InvalidClickRequest):
        provider.authenticate_and_decode(signed_click_body(secret="click-secret", amount=amount))


def test_click_rejects_tampering_and_duplicate_fields() -> None:
    provider = ClickProvider(merchant_id="merchant", service_id="77", secret_key="click-secret")
    tampered = signed_click_body(secret="click-secret").replace(b"amount=3000", b"amount=5000")
    duplicate = signed_click_body(secret="click-secret") + b"&amount=3000"
    with pytest.raises(InvalidClickRequest):
        provider.authenticate_and_decode(tampered)
    with pytest.raises(InvalidClickRequest):
        provider.authenticate_and_decode(duplicate)


def test_payme_basic_auth_and_tiyin_contract_are_verified() -> None:
    provider = PaymeProvider(merchant_id="merchant", login="Paycom", secret_key="payme-secret")
    authorization = "Basic " + base64.b64encode(b"Paycom:payme-secret").decode()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "CheckPerformTransaction",
            "params": {"amount": 300000, "account": {"order_id": "order-public-id"}},
        }
    ).encode()
    command = provider.authenticate_and_decode(body, {"authorization": authorization})
    assert command.method == "CheckPerformTransaction"
    assert command.amount_tiyin == 300000
    assert command.order_public_id == "order-public-id"


def test_payme_fails_closed_on_auth_and_duplicate_json_keys() -> None:
    provider = PaymeProvider(merchant_id="merchant", login="Paycom", secret_key="payme-secret")
    valid_auth = "Basic " + base64.b64encode(b"Paycom:payme-secret").decode()
    body = b'{"id":1,"method":"CheckPerformTransaction","params":{"amount":100,"amount":200,"account":{"order_id":"x"}}}'
    with pytest.raises(InvalidPaymeRequest):
        provider.authenticate_and_decode(body, {"authorization": valid_auth})
    with pytest.raises(InvalidPaymeRequest):
        provider.authenticate_and_decode(b"{}", {"authorization": "Basic invalid"})
