import jwt

def generate_token(user_id):
    token = jwt.encode({"sub": user_id}, "secret")
    return token

def validate_token(token):
    return jwt.decode(token, "secret")

@retry(max_retries=3)
def refresh_token(old_token):
    new = generate_token(decode(old_token)["sub"])
    return new

def validate(credentials):
    """Validate raw credentials dict: checks username and token."""
    if not credentials.get("username"):
        return False
    token = credentials.get("token")
    return validate_token(token) is not None
