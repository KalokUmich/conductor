"""Tests for AWS SSO auth module."""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.auth.service import SSOService


class TestSSOServiceExtractEmail:
    """Tests for email extraction from SSO ARN."""

    def test_extract_email_from_typical_sso_arn(self):
        arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_AdminAccess_abc123/user@example.com"
        assert SSOService._extract_email_from_arn(arn) == "user@example.com"

    def test_extract_email_from_arn_with_plus(self):
        arn = "arn:aws:sts::123456789012:assumed-role/RoleName/user+tag@example.com"
        assert SSOService._extract_email_from_arn(arn) == "user+tag@example.com"

    def test_extract_non_email_session_name(self):
        arn = "arn:aws:sts::123456789012:assumed-role/RoleName/session-name"
        assert SSOService._extract_email_from_arn(arn) == "session-name"

    def test_extract_from_empty_arn(self):
        assert SSOService._extract_email_from_arn("") == ""

    def test_extract_from_arn_no_slash(self):
        # ARNs without slashes (e.g. root user) return empty since
        # SSO assumed-role ARNs always have slashes
        assert SSOService._extract_email_from_arn("arn:aws:iam::123:root") == ""


class TestSSOServiceRegisterAndStart:
    """Tests for the register_and_start flow."""

    @patch("app.auth.service.boto3")
    def test_register_and_start_success(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.register_client.return_value = {
            "clientId": "test-client-id",
            "clientSecret": "test-client-secret",
        }
        mock_client.start_device_authorization.return_value = {
            "verificationUriComplete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH",
            "userCode": "ABCD-EFGH",
            "deviceCode": "test-device-code",
            "expiresIn": 600,
            "interval": 5,
        }

        service = SSOService(start_url="https://d-test.awsapps.com/start", region="us-east-1")
        result = service.register_and_start()

        assert result["verification_uri_complete"] == "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH"
        assert result["user_code"] == "ABCD-EFGH"
        assert result["device_code"] == "test-device-code"
        assert result["client_id"] == "test-client-id"
        assert result["client_secret"] == "test-client-secret"
        assert result["expires_in"] == 600
        assert result["interval"] == 5

        mock_client.register_client.assert_called_once_with(
            clientName="conductor-vscode",
            clientType="public",
        )
        mock_client.start_device_authorization.assert_called_once_with(
            clientId="test-client-id",
            clientSecret="test-client-secret",
            startUrl="https://d-test.awsapps.com/start",
        )


class TestSSOServicePollForToken:
    """Tests for the poll_for_token method."""

    @patch("app.auth.service.boto3")
    def test_poll_returns_none_when_pending(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.create_token.side_effect = ClientError(
            {"Error": {"Code": "AuthorizationPendingException", "Message": "pending"}},
            "CreateToken",
        )

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        result = service.poll_for_token("cid", "csecret", "dcode")
        assert result is None

    @patch("app.auth.service.boto3")
    def test_poll_returns_none_on_slow_down(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.create_token.side_effect = ClientError(
            {"Error": {"Code": "SlowDownException", "Message": "slow down"}},
            "CreateToken",
        )

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        result = service.poll_for_token("cid", "csecret", "dcode")
        assert result is None

    @patch("app.auth.service.boto3")
    def test_poll_returns_token_on_success(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.create_token.return_value = {
            "accessToken": "test-access-token",
            "tokenType": "Bearer",
            "expiresIn": 28800,
        }

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        result = service.poll_for_token("cid", "csecret", "dcode")
        assert result == "test-access-token"

    @patch("app.auth.service.boto3")
    def test_poll_raises_on_unexpected_error(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.create_token.side_effect = ClientError(
            {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}},
            "CreateToken",
        )

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        with pytest.raises(ClientError):
            service.poll_for_token("cid", "csecret", "dcode")


class TestSSOServiceGetIdentity:
    """Tests for the get_identity flow."""

    @patch("app.auth.service.boto3")
    def test_get_identity_full_flow(self, mock_boto3):
        # Set up mock clients based on service name
        mock_sso_client = MagicMock()
        mock_sts_client = MagicMock()
        mock_oidc_client = MagicMock()

        def client_factory(service_name, **kwargs):
            if service_name == "sso":
                return mock_sso_client
            elif service_name == "sts":
                return mock_sts_client
            elif service_name == "sso-oidc":
                return mock_oidc_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory

        mock_sso_client.list_accounts.return_value = {
            "accountList": [
                {"accountId": "111111111111", "accountName": "Dev", "emailAddress": "dev@company.com"},
                {"accountId": "222222222222", "accountName": "Prod", "emailAddress": "prod@company.com"},
            ]
        }
        mock_sso_client.list_account_roles.return_value = {
            "roleList": [
                {"roleName": "AdminAccess", "accountId": "111111111111"},
                {"roleName": "ReadOnly", "accountId": "111111111111"},
            ]
        }
        mock_sso_client.get_role_credentials.return_value = {
            "roleCredentials": {
                "accessKeyId": "AKIA...",
                "secretAccessKey": "secret...",
                "sessionToken": "token...",
            }
        }
        mock_sts_client.get_caller_identity.return_value = {
            "Arn": "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_AdminAccess_abc/dev@company.com",
            "UserId": "AROATEST:dev@company.com",
            "Account": "111111111111",
        }

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        identity = service.get_identity("test-access-token")

        assert identity["email"] == "dev@company.com"
        assert identity["account_id"] == "111111111111"
        assert identity["account_name"] == "Dev"
        assert identity["role_name"] == "AdminAccess"
        assert len(identity["accounts"]) == 2
        assert len(identity["roles"]) == 2
        assert identity["accounts"][0]["account_id"] == "111111111111"
        assert identity["roles"][0]["role_name"] == "AdminAccess"

    @patch("app.auth.service.boto3")
    def test_get_identity_no_accounts(self, mock_boto3):
        mock_sso_client = MagicMock()
        mock_oidc_client = MagicMock()

        def client_factory(service_name, **kwargs):
            if service_name == "sso":
                return mock_sso_client
            elif service_name == "sso-oidc":
                return mock_oidc_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        mock_sso_client.list_accounts.return_value = {"accountList": []}

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        identity = service.get_identity("test-access-token")
        assert "error" in identity

    @patch("app.auth.service.boto3")
    def test_get_identity_no_roles(self, mock_boto3):
        mock_sso_client = MagicMock()
        mock_oidc_client = MagicMock()

        def client_factory(service_name, **kwargs):
            if service_name == "sso":
                return mock_sso_client
            elif service_name == "sso-oidc":
                return mock_oidc_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        mock_sso_client.list_accounts.return_value = {
            "accountList": [{"accountId": "111", "accountName": "Test"}]
        }
        mock_sso_client.list_account_roles.return_value = {"roleList": []}

        service = SSOService(start_url="https://d-test.awsapps.com/start")
        identity = service.get_identity("test-access-token")
        assert "error" in identity
        assert "accounts" in identity


class TestSSOEndpoints:
    """Tests for the auth API endpoints."""

    def test_sso_start_disabled(self):
        """SSO start returns 400 when SSO is not enabled."""
        from app.config import ConductorConfig, SSOConfig

        mock_config = ConductorConfig(sso=SSOConfig(enabled=False))

        with patch("app.auth.router.get_config", return_value=mock_config):
            from app.auth.router import router
            from fastapi import FastAPI

            test_app = FastAPI()
            test_app.include_router(router)
            client = TestClient(test_app)

            response = client.post("/auth/sso/start")
            assert response.status_code == 400
            assert "not enabled" in response.json()["detail"]

    def test_sso_start_no_url(self):
        """SSO start returns 400 when start_url is empty."""
        from app.config import ConductorConfig, SSOConfig

        mock_config = ConductorConfig(sso=SSOConfig(enabled=True, start_url=""))

        with patch("app.auth.router.get_config", return_value=mock_config):
            from app.auth.router import router
            from fastapi import FastAPI

            test_app = FastAPI()
            test_app.include_router(router)
            client = TestClient(test_app)

            response = client.post("/auth/sso/start")
            assert response.status_code == 400
            assert "start_url" in response.json()["detail"]

    @patch("app.auth.router.SSOService")
    def test_sso_start_success(self, mock_service_cls):
        """SSO start returns device authorization data."""
        from app.config import ConductorConfig, SSOConfig

        mock_config = ConductorConfig(
            sso=SSOConfig(enabled=True, start_url="https://d-test.awsapps.com/start", region="us-east-1")
        )
        mock_instance = MagicMock()
        mock_service_cls.return_value = mock_instance
        mock_instance.register_and_start.return_value = {
            "verification_uri_complete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD",
            "user_code": "ABCD",
            "device_code": "dcode",
            "client_id": "cid",
            "client_secret": "csecret",
            "expires_in": 600,
            "interval": 5,
        }

        with patch("app.auth.router.get_config", return_value=mock_config):
            from app.auth.router import router
            from fastapi import FastAPI

            test_app = FastAPI()
            test_app.include_router(router)
            client = TestClient(test_app)

            response = client.post("/auth/sso/start")
            assert response.status_code == 200
            data = response.json()
            assert data["user_code"] == "ABCD"
            assert data["device_code"] == "dcode"

    @patch("app.auth.router.SSOService")
    def test_sso_poll_pending(self, mock_service_cls):
        """SSO poll returns pending when token not ready."""
        from app.config import ConductorConfig, SSOConfig

        mock_config = ConductorConfig(
            sso=SSOConfig(enabled=True, start_url="https://d-test.awsapps.com/start")
        )
        mock_instance = MagicMock()
        mock_service_cls.return_value = mock_instance
        mock_instance.poll_for_token.return_value = None

        with patch("app.auth.router.get_config", return_value=mock_config):
            from app.auth.router import router
            from fastapi import FastAPI

            test_app = FastAPI()
            test_app.include_router(router)
            client = TestClient(test_app)

            response = client.post("/auth/sso/poll", json={
                "device_code": "dcode",
                "client_id": "cid",
                "client_secret": "csecret",
            })
            assert response.status_code == 200
            assert response.json()["status"] == "pending"

    @patch("app.auth.router.SSOService")
    def test_sso_poll_complete(self, mock_service_cls):
        """SSO poll returns identity when token is complete."""
        from app.config import ConductorConfig, SSOConfig

        mock_config = ConductorConfig(
            sso=SSOConfig(enabled=True, start_url="https://d-test.awsapps.com/start")
        )
        mock_instance = MagicMock()
        mock_service_cls.return_value = mock_instance
        mock_instance.poll_for_token.return_value = "test-access-token"
        mock_instance.get_identity.return_value = {
            "email": "user@company.com",
            "arn": "arn:aws:sts::123:assumed-role/Role/user@company.com",
            "account_id": "123",
            "account_name": "Dev",
            "role_name": "AdminAccess",
            "accounts": [],
            "roles": [],
        }

        with patch("app.auth.router.get_config", return_value=mock_config):
            from app.auth.router import router
            from fastapi import FastAPI

            test_app = FastAPI()
            test_app.include_router(router)
            client = TestClient(test_app)

            response = client.post("/auth/sso/poll", json={
                "device_code": "dcode",
                "client_id": "cid",
                "client_secret": "csecret",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "complete"
            assert data["identity"]["email"] == "user@company.com"

    def test_sso_poll_disabled(self):
        """SSO poll returns 400 when SSO not enabled."""
        from app.config import ConductorConfig, SSOConfig

        mock_config = ConductorConfig(sso=SSOConfig(enabled=False))

        with patch("app.auth.router.get_config", return_value=mock_config):
            from app.auth.router import router
            from fastapi import FastAPI

            test_app = FastAPI()
            test_app.include_router(router)
            client = TestClient(test_app)

            response = client.post("/auth/sso/poll", json={
                "device_code": "dcode",
                "client_id": "cid",
                "client_secret": "csecret",
            })
            assert response.status_code == 400
