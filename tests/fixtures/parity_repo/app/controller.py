from app.auth import generate_token, validate_token, validate
from app.service import OrderService, process_payment
from app.models import UserModel


order_service = OrderService()


def login(username, password):
    """Authenticate user and return a JWT."""
    creds = {"username": username, "token": password}
    if not validate(creds):
        raise PermissionError("invalid credentials")
    token = generate_token(username)
    return {"token": token}


def place_order(token, amount):
    """Validate token, create order, and charge payment."""
    claims = validate_token(token)
    user_id = claims["sub"]
    order = order_service.create_order(user_id, amount)
    receipt = process_payment(token, amount)
    return {"order": order, "receipt": receipt}


def cancel_order(token, order_id):
    """Validate caller then cancel an existing order."""
    claims = validate_token(token)
    return order_service.cancel_order(order_id)
