"""Tests for Symbol Role Classification in find_symbol."""

from pathlib import Path

import pytest

from app.code_tools.tools import (
    _ROLE_PRIORITY,
    _classify_symbol_role,
    find_symbol,
)


class TestClassifySymbolRole:
    """Test the role classification logic."""

    def test_route_entry_by_file_path(self):
        role = _classify_symbol_role(
            name="get_users",
            kind="function",
            file_path="app/routes/users.py",
            signature="def get_users():",
            workspace="/tmp",
        )
        assert role == "route_entry"

    def test_route_entry_by_controller_path(self):
        role = _classify_symbol_role(
            name="create",
            kind="method",
            file_path="src/controllers/UserController.ts",
            signature="create(req, res)",
            workspace="/tmp",
        )
        assert role == "route_entry"

    def test_route_entry_by_handler_path(self):
        role = _classify_symbol_role(
            name="handle_login",
            kind="function",
            file_path="handlers/auth.go",
            signature="func handle_login(w, r)",
            workspace="/tmp",
        )
        assert role == "route_entry"

    def test_business_logic_by_service_path(self):
        role = _classify_symbol_role(
            name="process_payment",
            kind="function",
            file_path="app/services/payment.py",
            signature="def process_payment():",
            workspace="/tmp",
        )
        assert role == "business_logic"

    def test_business_logic_by_name(self):
        role = _classify_symbol_role(
            name="PaymentService",
            kind="class",
            file_path="app/core.py",
            signature="class PaymentService:",
            workspace="/tmp",
        )
        assert role == "business_logic"

    def test_domain_model_by_path(self):
        role = _classify_symbol_role(
            name="User",
            kind="class",
            file_path="app/models/user.py",
            signature="class User(Base):",
            workspace="/tmp",
        )
        assert role == "domain_model"

    def test_domain_model_by_signature(self):
        role = _classify_symbol_role(
            name="Order",
            kind="class",
            file_path="app/core.py",
            signature="class Order(DeclarativeBase):",
            workspace="/tmp",
        )
        assert role == "domain_model"

    def test_domain_model_by_name(self):
        role = _classify_symbol_role(
            name="UserSchema",
            kind="class",
            file_path="app/types.py",
            signature="class UserSchema:",
            workspace="/tmp",
        )
        assert role == "domain_model"

    def test_infrastructure_by_path(self):
        role = _classify_symbol_role(
            name="get_user",
            kind="function",
            file_path="app/repository/user_repo.py",
            signature="def get_user(id):",
            workspace="/tmp",
        )
        assert role == "infrastructure"

    def test_infrastructure_by_name(self):
        role = _classify_symbol_role(
            name="UserRepository",
            kind="class",
            file_path="app/core.py",
            signature="class UserRepository:",
            workspace="/tmp",
        )
        assert role == "infrastructure"

    def test_test_by_path(self):
        role = _classify_symbol_role(
            name="authenticate",
            kind="function",
            file_path="tests/test_auth.py",
            signature="def authenticate():",
            workspace="/tmp",
        )
        assert role == "test"

    def test_test_by_name(self):
        role = _classify_symbol_role(
            name="test_login_success",
            kind="function",
            file_path="app/auth.py",  # even in non-test file
            signature="def test_login_success():",
            workspace="/tmp",
        )
        assert role == "test"

    def test_test_spec_file(self):
        role = _classify_symbol_role(
            name="LoginFlow",
            kind="class",
            file_path="src/auth.spec.ts",
            signature="class LoginFlow",
            workspace="/tmp",
        )
        assert role == "test"

    def test_utility_by_path(self):
        role = _classify_symbol_role(
            name="format_date",
            kind="function",
            file_path="app/utils/dates.py",
            signature="def format_date():",
            workspace="/tmp",
        )
        assert role == "utility"

    def test_utility_by_name(self):
        role = _classify_symbol_role(
            name="StringHelper",
            kind="class",
            file_path="app/core.py",
            signature="class StringHelper:",
            workspace="/tmp",
        )
        assert role == "utility"

    def test_unknown_fallback(self):
        role = _classify_symbol_role(
            name="Foo",
            kind="class",
            file_path="app/core.py",
            signature="class Foo:",
            workspace="/tmp",
        )
        assert role == "unknown"

    def test_decorator_detection(self, tmp_path: Path):
        """Decorator above the function should influence role classification."""
        py_file = tmp_path / "router.py"
        py_file.write_text(
            "from fastapi import APIRouter\n"
            "\n"
            "router = APIRouter()\n"
            "\n"
            "@router.get('/users')\n"
            "def list_users():\n"
            "    return []\n"
        )
        role = _classify_symbol_role(
            name="list_users",
            kind="function",
            file_path="router.py",
            signature="def list_users():",
            workspace=str(tmp_path),
            start_line=6,
        )
        assert role == "route_entry"

    def test_java_annotation_detection(self, tmp_path: Path):
        """Java @Service annotation should classify as business_logic."""
        java_file = tmp_path / "PaymentService.java"
        java_file.write_text(
            "package com.example;\n\n@Service\npublic class PaymentService {\n    public void process() {}\n}\n"
        )
        role = _classify_symbol_role(
            name="PaymentService",
            kind="class",
            file_path="PaymentService.java",
            signature="public class PaymentService",
            workspace=str(tmp_path),
            start_line=4,
        )
        assert role == "business_logic"

    def test_spring_entity_detection(self, tmp_path: Path):
        java_file = tmp_path / "User.java"
        java_file.write_text(
            'package com.example;\n\n@Entity\n@Table(name="users")\npublic class User {\n    private Long id;\n}\n'
        )
        role = _classify_symbol_role(
            name="User",
            kind="class",
            file_path="User.java",
            signature="public class User",
            workspace=str(tmp_path),
            start_line=5,
        )
        assert role == "domain_model"


class TestRolePriority:
    """Test that all roles have a priority defined."""

    def test_all_roles_have_priority(self):
        expected = {"route_entry", "business_logic", "domain_model", "infrastructure", "utility", "test", "unknown"}
        assert set(_ROLE_PRIORITY.keys()) == expected

    def test_route_entry_highest_priority(self):
        assert _ROLE_PRIORITY["route_entry"] < _ROLE_PRIORITY["test"]
        assert _ROLE_PRIORITY["route_entry"] < _ROLE_PRIORITY["unknown"]

    def test_business_logic_before_test(self):
        assert _ROLE_PRIORITY["business_logic"] < _ROLE_PRIORITY["test"]


class TestFindSymbolWithRole:
    """Test that find_symbol returns role in results and sorts by priority."""

    @pytest.fixture
    def workspace(self, tmp_path: Path):
        """Create a workspace with symbols of different roles."""
        # A test file
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_auth.py").write_text("def test_authenticate():\n    pass\n")
        # A service file
        svc_dir = tmp_path / "services"
        svc_dir.mkdir()
        (svc_dir / "auth_service.py").write_text(
            "class AuthService:\n    def authenticate(self, user):\n        pass\n"
        )
        # A route file
        (tmp_path / "routes.py").write_text("def authenticate_user():\n    pass\n")
        return str(tmp_path)

    def test_results_have_role_field(self, workspace):
        result = find_symbol(workspace, "authenticate")
        assert result.success
        for item in result.data:
            assert "role" in item

    def test_test_symbols_sorted_last(self, workspace):
        result = find_symbol(workspace, "authenticate")
        assert result.success
        if len(result.data) >= 2:
            roles = [r.get("role", "unknown") for r in result.data]
            # test role should not appear before non-test roles
            for i, role in enumerate(roles):
                if role == "test":
                    # All subsequent roles should be test or unknown
                    for j in range(i + 1, len(roles)):
                        assert roles[j] in ("test", "unknown"), f"non-test role {roles[j]} after test at index {i}"
