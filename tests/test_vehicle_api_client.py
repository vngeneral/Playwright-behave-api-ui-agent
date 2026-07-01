"""
Unit tests for utils/api/base_client.py and utils/api/vehicle_client.py.
All HTTP is mocked — no real network calls made.
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal mock for requests.Response
# ---------------------------------------------------------------------------

def _mock_response(status_code: int = 200, body: dict | None = None, text: str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text or json.dumps(body or {})
    resp.json = MagicMock(return_value=body or {})
    return resp


# ---------------------------------------------------------------------------
# generate_transaction_id() — core behaviour under test
# ---------------------------------------------------------------------------

class TestGenerateTransactionId(unittest.TestCase):
    """
    generate_transaction_id() is the single place where transactionIds
    are produced.  These tests pin its contract.
    """

    def _gen(self) -> str:
        from utils.api.vehicle_client import generate_transaction_id
        return generate_transaction_id()

    def test_returns_string(self):
        self.assertIsInstance(self._gen(), str)

    def test_is_valid_uuid4(self):
        tid = self._gen()
        parsed = uuid.UUID(tid, version=4)
        self.assertEqual(str(parsed), tid)

    def test_lowercase_hyphenated_format(self):
        tid = self._gen()
        # Must match xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx
        self.assertRegex(tid, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

    def test_each_call_returns_different_value(self):
        """The core guarantee: no two consecutive calls return the same ID."""
        from utils.api.vehicle_client import generate_transaction_id
        ids = {generate_transaction_id() for _ in range(100)}
        self.assertEqual(len(ids), 100, "generate_transaction_id() returned a duplicate within 100 calls")

    def test_consecutive_ids_differ_in_content(self):
        """Illustrate the requirement: old vs new transactionId are always different."""
        from utils.api.vehicle_client import generate_transaction_id
        old_id = generate_transaction_id()
        new_id = generate_transaction_id()
        self.assertNotEqual(
            old_id, new_id,
            f"Expected different IDs, got:\n  old: {old_id}\n  new: {new_id}"
        )

    def test_generate_transaction_id_is_the_only_id_source(self):
        """
        Verify that scattered uuid4() calls have been removed from vehicle_client.py
        and all generation flows through generate_transaction_id().
        """
        import inspect
        import utils.api.vehicle_client as module

        full_source = inspect.getsource(module)
        # Lines that belong to generate_transaction_id's own body are allowed
        fn_lines = set(
            line.strip()
            for line in inspect.getsource(module.generate_transaction_id).splitlines()
        )
        leaking_lines = [
            line.strip()
            for line in full_source.splitlines()
            if "uuid.uuid4()" in line and line.strip() not in fn_lines
        ]
        self.assertEqual(
            leaking_lines, [],
            "Raw uuid.uuid4() found outside generate_transaction_id():\n"
            + "\n".join(leaking_lines)
        )


# ---------------------------------------------------------------------------
# BaseAPIClient tests
# ---------------------------------------------------------------------------

class TestBaseAPIClientRequest(unittest.TestCase):
    def _make_client(self, base_url="https://api.example.com"):
        from utils.api.base_client import BaseAPIClient
        return BaseAPIClient(base_url=base_url)

    @patch("utils.api.base_client.requests.Session.request")
    def test_get_appends_path(self, mock_request):
        mock_request.return_value = _mock_response(200, {"ok": True})
        client = self._make_client("https://api.example.com")
        resp = client.get("v1/resource")
        call_url = mock_request.call_args[1]["url"]
        self.assertEqual(call_url, "https://api.example.com/v1/resource")

    @patch("utils.api.base_client.requests.Session.request")
    def test_strips_double_slash(self, mock_request):
        mock_request.return_value = _mock_response(200)
        client = self._make_client("https://api.example.com/")
        client.get("/v1/resource")
        url = mock_request.call_args[1]["url"]
        self.assertNotIn("//v1", url)

    @patch("utils.api.base_client.requests.Session.request")
    def test_post_sends_json_body(self, mock_request):
        mock_request.return_value = _mock_response(200)
        client = self._make_client()
        payload = {"transactionId": "abc", "partnerCode": "XYZ"}
        client.post("v1/register", json_body=payload)
        call_json = mock_request.call_args[1]["json"]
        self.assertEqual(call_json, payload)

    @patch("utils.api.base_client.requests.Session.request")
    def test_returns_api_response_with_status(self, mock_request):
        mock_request.return_value = _mock_response(201, {"id": "123"})
        from utils.api.base_client import APIResponse
        client = self._make_client()
        result = client.post("v1/thing", json_body={})
        self.assertIsInstance(result, APIResponse)
        self.assertEqual(result.status_code, 201)

    @patch("utils.api.base_client.requests.Session.request")
    def test_elapsed_ms_is_non_negative(self, mock_request):
        mock_request.return_value = _mock_response(200)
        client = self._make_client()
        result = client.get("v1/ping")
        self.assertGreaterEqual(result.elapsed_ms, 0)

    @patch("utils.api.base_client.requests.Session.request")
    def test_expected_status_passes_when_matching(self, mock_request):
        mock_request.return_value = _mock_response(200)
        client = self._make_client()
        result = client.get("v1/ok", expected_status=200)
        self.assertEqual(result.status_code, 200)

    @patch("utils.api.base_client.requests.Session.request")
    def test_expected_status_raises_when_mismatch(self, mock_request):
        mock_request.return_value = _mock_response(404)
        client = self._make_client()
        with self.assertRaises(AssertionError) as ctx:
            client.get("v1/missing", expected_status=200)
        self.assertIn("404", str(ctx.exception))
        self.assertIn("200", str(ctx.exception))

    @patch("utils.api.base_client.requests.Session.request")
    def test_ok_true_for_2xx(self, mock_request):
        for code in (200, 201, 204):
            mock_request.return_value = _mock_response(code)
            client = self._make_client()
            result = client.get("v1/ok")
            self.assertTrue(result.ok, f"Expected ok=True for status {code}")

    @patch("utils.api.base_client.requests.Session.request")
    def test_ok_false_for_4xx_5xx(self, mock_request):
        for code in (400, 401, 403, 404, 500):
            mock_request.return_value = _mock_response(code)
            client = self._make_client()
            result = client.get("v1/fail")
            self.assertFalse(result.ok, f"Expected ok=False for status {code}")

    @patch("utils.api.base_client.requests.Session.request")
    def test_extra_headers_merged(self, mock_request):
        mock_request.return_value = _mock_response(200)
        client = self._make_client()
        client.get("v1/secure", headers={"x-api-key": "test-key"})
        call_headers = mock_request.call_args[1]["headers"]
        self.assertEqual(call_headers["x-api-key"], "test-key")


# ---------------------------------------------------------------------------
# APIResponse tests
# ---------------------------------------------------------------------------

class TestAPIResponse(unittest.TestCase):
    def test_json_method(self):
        from utils.api.base_client import APIResponse
        body = {"status": "ok", "count": 3}
        raw = _mock_response(200, body)
        r = APIResponse(raw, 42.5)
        self.assertEqual(r.json(), body)

    def test_repr(self):
        from utils.api.base_client import APIResponse
        raw = _mock_response(200)
        r = APIResponse(raw, 123.4)
        self.assertIn("200", repr(r))
        self.assertIn("123", repr(r))


# ---------------------------------------------------------------------------
# Request model tests — transaction_id is now an explicit required field
# ---------------------------------------------------------------------------

class TestRequestModels(unittest.TestCase):
    def test_registration_to_dict_structure(self):
        from utils.api.vehicle_client import VehicleRegistrationRequest, generate_transaction_id
        txn = generate_transaction_id()
        req = VehicleRegistrationRequest(
            transaction_id=txn,
            partner_code="HBL4BP-006",
            vin_list=["KMUHCESC7RU179347"],
        )
        d = req.to_dict()
        self.assertEqual(d["transactionId"], txn)
        self.assertEqual(d["partnerCode"], "HBL4BP-006")
        self.assertEqual(d["vinList"], [{"vin": "KMUHCESC7RU179347"}])

    def test_registration_requires_explicit_transaction_id(self):
        """
        transaction_id has no default on the dataclass — it must come from
        generate_transaction_id(), not be auto-created silently.
        """
        from utils.api.vehicle_client import VehicleRegistrationRequest
        import inspect, dataclasses
        fields = {f.name: f for f in dataclasses.fields(VehicleRegistrationRequest)}
        tid_field = fields["transaction_id"]
        self.assertIs(
            tid_field.default, dataclasses.MISSING,
            "transaction_id should have no default — caller must pass generate_transaction_id()"
        )
        self.assertIs(
            tid_field.default_factory, dataclasses.MISSING,
            "transaction_id should have no default_factory — use generate_transaction_id() explicitly"
        )

    def test_deregistration_requires_explicit_transaction_id(self):
        from utils.api.vehicle_client import VehicleDeregistrationRequest
        import inspect, dataclasses
        fields = {f.name: f for f in dataclasses.fields(VehicleDeregistrationRequest)}
        tid_field = fields["transaction_id"]
        self.assertIs(tid_field.default, dataclasses.MISSING)
        self.assertIs(tid_field.default_factory, dataclasses.MISSING)

    def test_batch_vin_list_serialised_correctly(self):
        from utils.api.vehicle_client import VehicleRegistrationRequest, generate_transaction_id
        vins = ["VIN1", "VIN2", "VIN3"]
        req = VehicleRegistrationRequest(
            transaction_id=generate_transaction_id(),
            partner_code="P",
            vin_list=vins,
        )
        self.assertEqual(req.to_dict()["vinList"], [{"vin": v} for v in vins])

    def test_deregistration_to_dict_structure(self):
        from utils.api.vehicle_client import VehicleDeregistrationRequest, generate_transaction_id
        txn = generate_transaction_id()
        req = VehicleDeregistrationRequest(
            transaction_id=txn,
            partner_code="HBL4BP-006",
            vin_list=["KM8JFDA25PU103343"],
        )
        d = req.to_dict()
        self.assertEqual(d["transactionId"], txn)
        self.assertIn("partnerCode", d)
        self.assertEqual(d["vinList"], [{"vin": "KM8JFDA25PU103343"}])


# ---------------------------------------------------------------------------
# VehicleAPIClient — env var + config wiring
# ---------------------------------------------------------------------------

class TestVehicleAPIClientConfig(unittest.TestCase):
    def tearDown(self):
        for k in ("VEHICLE_API_KEY", "VEHICLE_API_BASE_URL", "ENV"):
            os.environ.pop(k, None)

    def test_raises_when_no_base_url(self):
        os.environ.pop("VEHICLE_API_BASE_URL", None)
        from utils.api.vehicle_client import VehicleAPIClient, VehicleClientConfigError
        config = {"environment": "dev", "environments": {"dev": {}}}
        with self.assertRaises(VehicleClientConfigError):
            VehicleAPIClient.from_config(config)

    def test_uses_base_url_from_config(self):
        config = {
            "environment": "dev",
            "environments": {"dev": {"vehicle_api_url": "https://api.example.com"}},
            "retry": {},
        }
        from utils.api.vehicle_client import VehicleAPIClient
        client = VehicleAPIClient.from_config(config)
        self.assertEqual(client._base_url, "https://api.example.com")

    def test_env_var_overrides_config_url(self):
        os.environ["VEHICLE_API_BASE_URL"] = "https://override.example.com"
        config = {
            "environment": "dev",
            "environments": {"dev": {"vehicle_api_url": "https://config.example.com"}},
            "retry": {},
        }
        from utils.api.vehicle_client import VehicleAPIClient
        client = VehicleAPIClient.from_config(config)
        self.assertEqual(client._base_url, "https://override.example.com")

    def test_api_key_read_from_env(self):
        os.environ["VEHICLE_API_KEY"] = "my-secret-key"
        from utils.api.vehicle_client import VehicleAPIClient
        client = VehicleAPIClient(base_url="https://api.example.com")
        headers = client._auth_headers()
        self.assertEqual(headers["x-api-key"], "my-secret-key")

    def test_api_key_not_hardcoded(self):
        """API key must come from env var — never from source code constants."""
        import inspect
        import utils.api.vehicle_client as module
        source = inspect.getsource(module)
        self.assertNotIn("ESD2PteobddbIr6qkuh0o1ixlVMStsm939sv9NtYX8I57UXI", source)


# ---------------------------------------------------------------------------
# VehicleAPIClient — HTTP calls + transactionId injection
# ---------------------------------------------------------------------------

class TestVehicleAPIClientHTTP(unittest.TestCase):
    BASE_URL = "https://stage.bl4b.api.sample.com"

    def setUp(self):
        os.environ["VEHICLE_API_KEY"] = "test-api-key"
        from utils.api.vehicle_client import VehicleAPIClient
        self.client = VehicleAPIClient(base_url=self.BASE_URL)

    def tearDown(self):
        os.environ.pop("VEHICLE_API_KEY", None)
        self.client.close()

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_calls_correct_endpoint(self, mock_request):
        mock_request.return_value = _mock_response(200, {"transactionId": "t1"})
        self.client.register_vehicles("HBL4BP-006", ["VIN1"])
        url = mock_request.call_args[1]["url"]
        self.assertIn("register", url)
        self.assertNotIn("deregister", url)

    @patch("utils.api.base_client.requests.Session.request")
    def test_deregister_calls_correct_endpoint(self, mock_request):
        mock_request.return_value = _mock_response(200, {"transactionId": "t1"})
        self.client.deregister_vehicles("HBL4BP-006", ["VIN1"])
        url = mock_request.call_args[1]["url"]
        self.assertIn("deregister", url)

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_sends_x_api_key_header(self, mock_request):
        mock_request.return_value = _mock_response(200)
        self.client.register_vehicles("HBL4BP-006", ["VIN1"])
        headers = mock_request.call_args[1]["headers"]
        self.assertEqual(headers["x-api-key"], "test-api-key")

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_sends_partner_code_in_body(self, mock_request):
        mock_request.return_value = _mock_response(200)
        self.client.register_vehicles("HBL4BP-006", ["KMUHCESC7RU179347"])
        body = mock_request.call_args[1]["json"]
        self.assertEqual(body["partnerCode"], "HBL4BP-006")

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_sends_vin_list_in_body(self, mock_request):
        mock_request.return_value = _mock_response(200)
        self.client.register_vehicles("HBL4BP-006", ["VIN_A", "VIN_B"])
        body = mock_request.call_args[1]["json"]
        self.assertEqual(body["vinList"], [{"vin": "VIN_A"}, {"vin": "VIN_B"}])

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_body_contains_valid_uuid4_transaction_id(self, mock_request):
        mock_request.return_value = _mock_response(200)
        self.client.register_vehicles("HBL4BP-006", ["VIN1"])
        body = mock_request.call_args[1]["json"]
        tid = body["transactionId"]
        parsed = uuid.UUID(tid, version=4)
        self.assertEqual(str(parsed), tid)

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_uses_provided_transaction_id(self, mock_request):
        mock_request.return_value = _mock_response(200)
        self.client.register_vehicles("HBL4BP-006", ["VIN1"], transaction_id="custom-txn-id")
        body = mock_request.call_args[1]["json"]
        self.assertEqual(body["transactionId"], "custom-txn-id")

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_transaction_id_generated_before_request(self, mock_request):
        """
        generate_transaction_id() is called before the HTTP request fires.
        Verify by patching generate_transaction_id and checking the body
        uses the patched value — not some value created at object construction.
        """
        mock_request.return_value = _mock_response(200)
        pinned_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        with patch("utils.api.vehicle_client.generate_transaction_id", return_value=pinned_id):
            self.client.register_vehicles("HBL4BP-006", ["VIN1"])
        body = mock_request.call_args[1]["json"]
        self.assertEqual(body["transactionId"], pinned_id)

    @patch("utils.api.base_client.requests.Session.request")
    def test_deregister_transaction_id_generated_before_request(self, mock_request):
        mock_request.return_value = _mock_response(200)
        pinned_id = "11111111-2222-4333-8444-555555555555"
        with patch("utils.api.vehicle_client.generate_transaction_id", return_value=pinned_id):
            self.client.deregister_vehicles("HBL4BP-006", ["VIN1"])
        body = mock_request.call_args[1]["json"]
        self.assertEqual(body["transactionId"], pinned_id)

    @patch("utils.api.base_client.requests.Session.request")
    def test_two_consecutive_requests_have_different_transaction_ids(self, mock_request):
        mock_request.return_value = _mock_response(200)
        self.client.register_vehicles("HBL4BP-006", ["VIN1"])
        first_tid = mock_request.call_args[1]["json"]["transactionId"]
        self.client.register_vehicles("HBL4BP-006", ["VIN2"])
        second_tid = mock_request.call_args[1]["json"]["transactionId"]
        self.assertNotEqual(
            first_tid, second_tid,
            f"Expected different transactionIds:\n  first : {first_tid}\n  second: {second_tid}"
        )

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_with_invalid_key_uses_bad_key(self, mock_request):
        mock_request.return_value = _mock_response(401, {"error": "Unauthorized"})
        resp = self.client.register_vehicles_with_invalid_key("HBL4BP-006", ["VIN1"])
        headers = mock_request.call_args[1]["headers"]
        self.assertEqual(headers["x-api-key"], "invalid-key-for-testing")
        self.assertEqual(resp.status_code, 401)

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_with_invalid_key_still_sends_valid_uuid_transaction_id(self, mock_request):
        """Even the invalid-key helper must generate a fresh transactionId."""
        mock_request.return_value = _mock_response(401)
        self.client.register_vehicles_with_invalid_key("HBL4BP-006", ["VIN1"])
        body = mock_request.call_args[1]["json"]
        tid = body["transactionId"]
        parsed = uuid.UUID(tid, version=4)
        self.assertEqual(str(parsed), tid)

    @patch("utils.api.base_client.requests.Session.request")
    def test_register_missing_field_omits_field(self, mock_request):
        mock_request.return_value = _mock_response(400, {"error": "Missing transactionId"})
        self.client.register_without_required_field(
            missing_field="transactionId",
            partner_code="HBL4BP-006",
            vin_list=["VIN1"],
        )
        body = mock_request.call_args[1]["json"]
        self.assertNotIn("transactionId", body)
        self.assertIn("partnerCode", body)

    @patch("utils.api.base_client.requests.Session.request")
    def test_empty_vin_list_sends_empty_array(self, mock_request):
        mock_request.return_value = _mock_response(400)
        self.client.register_vehicles("HBL4BP-006", [])
        body = mock_request.call_args[1]["json"]
        self.assertEqual(body["vinList"], [])

    @patch("utils.api.base_client.requests.Session.request")
    def test_network_error_propagates(self, mock_request):
        mock_request.side_effect = ConnectionError("Connection refused")
        with self.assertRaises(Exception):
            self.client.register_vehicles("HBL4BP-006", ["VIN1"])


# ---------------------------------------------------------------------------
# Test data file smoke test
# ---------------------------------------------------------------------------

class TestVehicleApiTestData(unittest.TestCase):
    def setUp(self):
        self.data_path = os.path.join(
            os.path.dirname(__file__),
            "..", "test_data", "vehicle_api.json"
        )

    def test_file_exists(self):
        self.assertTrue(os.path.exists(self.data_path), "test_data/vehicle_api.json not found")

    def test_has_required_sections(self):
        with open(self.data_path) as f:
            data = json.load(f)
        self.assertIn("partner", data)
        self.assertIn("vins", data)
        self.assertIn("scenarios", data)

    def test_register_vin_is_17_chars(self):
        with open(self.data_path) as f:
            data = json.load(f)
        single = data["vins"]["register"]["single"]
        self.assertEqual(len(single), 17, f"VIN '{single}' is not 17 characters")

    def test_deregister_vin_is_17_chars(self):
        with open(self.data_path) as f:
            data = json.load(f)
        single = data["vins"]["deregister"]["single"]
        self.assertEqual(len(single), 17, f"VIN '{single}' is not 17 characters")


if __name__ == "__main__":
    unittest.main(verbosity=2)
