import re


def validate(value, rules):
    """Validate a value against a list of rule names.

    This is deliberately named 'validate' to test symbol disambiguation
    with auth.validate.
    """
    errors = []
    for rule in rules:
        checker = RULES.get(rule)
        if checker and not checker(value):
            errors.append(f"failed: {rule}")
    return errors


def _is_non_empty(value):
    return bool(value)


def _is_email(value):
    return bool(re.match(r"^[^@]+@[^@]+\.[^@]+$", str(value)))


def _is_positive(value):
    return isinstance(value, (int, float)) and value > 0


RULES = {
    "non_empty": _is_non_empty,
    "email": _is_email,
    "positive": _is_positive,
}
