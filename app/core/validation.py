import re
from typing import Any, Optional, Union
from uuid import UUID

from fastapi import HTTPException, status


def sanitize_string(value: str, max_length: int = 1000) -> str:
    """Sanitize string input by removing potentially dangerous characters."""
    if not value:
        return ""

    value = value.strip()
    value = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]", "", value)

    if len(value) > max_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"String exceeds maximum length of {max_length}",
        )

    return value


def validate_email(email: str) -> str:
    """Validate and sanitize email address."""
    email = sanitize_string(email, max_length=255)

    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_pattern, email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email format",
        )

    return email.lower()


def validate_phone_number(phone: str) -> str:
    """Validate and sanitize phone number."""
    phone = sanitize_string(phone, max_length=20)

    phone = re.sub(r"[^\d+]", "", phone)

    if len(phone) < 10 or len(phone) > 15:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid phone number format",
        )

    return phone


def validate_canadian_postal_code(postal_code: str) -> str:
    """Validate Canadian postal code format."""
    postal_code = sanitize_string(postal_code, max_length=10).upper()

    postal_code = re.sub(r"[^A-Z0-9]", "", postal_code)

    if len(postal_code) != 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Canadian postal code format",
        )

    postal_code = f"{postal_code[:3]} {postal_code[3:]}"

    return postal_code


def validate_ssn(ssn: str) -> str:
    """Validate SSN format."""
    ssn = sanitize_string(ssn, max_length=11)

    ssn = re.sub(r"[^\d]", "", ssn)

    if len(ssn) != 9:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid SSN format",
        )

    return f"***-**-{ssn[-4:]}"


def validate_sin(sin: str) -> str:
    """Validate Canadian Social Insurance Number format.

    Returns a MASKED form (``***-***-NNN``) — safe to surface. Use
    :func:`normalize_sin` when the bare digits are needed for encryption.
    """
    sin = sanitize_string(sin, max_length=11)

    sin = re.sub(r"[^\d]", "", sin)

    if len(sin) != 9:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid SIN format",
        )

    return f"***-***-{sin[-3:]}"


def _sin_luhn_valid(digits: str) -> bool:
    """Canadian SIN Luhn (mod-10) check on a 9-digit string."""
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == 1:  # every second digit (0-indexed positions 1,3,5,7) is doubled
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def normalize_sin(sin: str) -> str:
    """Return the bare 9-digit SIN after format + Luhn validation.

    Raises HTTP 422 on any invalid input. This is the value passed to
    ``encrypt_sin`` — it is the raw SIN and MUST NOT be logged or returned.
    The returned value carries no masking; the caller is responsible for only
    ever persisting it encrypted and exposing ``sin_last3`` instead.
    """
    cleaned = sanitize_string(sin, max_length=11)
    cleaned = re.sub(r"[^\d]", "", cleaned)

    if len(cleaned) != 9 or not _sin_luhn_valid(cleaned):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid SIN",
        )
    return cleaned


def validate_amount(amount: Union[float, int, str], min_amount: float = 0, max_amount: float = 1000000) -> float:
    """Validate monetary amount."""
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid amount format",
        )

    if amount < min_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amount must be at least {min_amount}",
        )

    if amount > max_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amount cannot exceed {max_amount}",
        )

    return round(amount, 2)


def validate_percentage(percentage: Union[float, int, str], min_pct: float = 0, max_pct: float = 100) -> float:
    """Validate percentage value."""
    try:
        percentage = float(percentage)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid percentage format",
        )

    if percentage < min_pct or percentage > max_pct:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Percentage must be between {min_pct} and {max_pct}",
        )

    return round(percentage, 2)


def sanitize_json_field(data: dict, field_name: str, max_length: int = 1000) -> str:
    """Safely sanitize a JSON field from request data."""
    if field_name not in data or data[field_name] is None:
        return ""

    value = str(data[field_name])
    return sanitize_string(value, max_length)


def validate_uuid(uuid_str: str) -> UUID:
    """Validate UUID string."""
    try:
        return UUID(uuid_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid UUID format",
        )


def sanitize_html(text: str) -> str:
    """Remove HTML tags and dangerous content from text."""
    if not text:
        return ""

    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<iframe[^>]*>.*?</iframe>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)

    dangerous_patterns = [
        r"javascript:",
        r"vbscript:",
        r"onload\s*=",
        r"onerror\s*=",
        r"onclick\s*=",
        r"onmouseover\s*=",
    ]

    for pattern in dangerous_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    return text.strip()


def validate_url(url: str, allowed_schemes: Optional[list[str]] = None) -> str:
    """Validate and sanitize URL."""
    if allowed_schemes is None:
        allowed_schemes = ["https", "http"]

    url = sanitize_string(url, max_length=2048)

    from urllib.parse import urlparse

    parsed = urlparse(url)

    if not parsed.scheme or parsed.scheme.lower() not in allowed_schemes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL must use one of the following schemes: {', '.join(allowed_schemes)}",
        )

    if not parsed.netloc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid URL format",
        )

    return url


def check_sql_injection(input_str: str) -> None:
    """Check for common SQL injection patterns."""
    if not input_str:
        return

    sql_patterns = [
        r"'\s*or\s*'",
        r'"\s*or\s*"',
        r"'\s*and\s*'",
        r'"\s*and\s*"',
        r";\s*drop",
        r";\s*delete",
        r";\s*insert",
        r";\s*update",
        r";\s*exec",
        r"union\s+select",
        r"--",
        r"/\*",
        r"\*/",
        r"xp_cmdshell",
        r"sp_executesql",
    ]

    input_lower = input_str.lower()

    for pattern in sql_patterns:
        if re.search(pattern, input_lower, re.IGNORECASE):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid input detected",
            )


def check_xss(input_str: str) -> None:
    """Check for common XSS attack patterns."""
    if not input_str:
        return

    xss_patterns = [
        r"<script[^>]*>",
        r"<iframe[^>]*>",
        r"<embed[^>]*>",
        r"<object[^>]*>",
        r"javascript:",
        r"vbscript:",
        r"data:",
        r"onload\s*=",
        r"onerror\s*=",
        r"onclick\s*=",
        r"onmouseover\s*=",
        r"onfocus\s*=",
        r"onblur\s*=",
        r"eval\s*\(",
        r"expression\s*\(",
    ]

    input_lower = input_str.lower()

    for pattern in xss_patterns:
        if re.search(pattern, input_lower, re.IGNORECASE):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid input detected",
            )


def validate_input(input_str: str) -> str:
    """Comprehensive input validation."""
    if not input_str:
        return ""

    sanitized = sanitize_string(input_str)
    check_sql_injection(sanitized)
    check_xss(sanitized)

    return sanitized


class QueryValidator:
    """Validator for database queries to prevent injection."""

    @staticmethod
    def validate_filter(value: Any) -> Any:
        """Validate filter value for queries."""
        if isinstance(value, str):
            return validate_input(value)
        elif isinstance(value, list):
            return [QueryValidator.validate_filter(v) for v in value]
        elif isinstance(value, dict):
            return {k: QueryValidator.validate_filter(v) for k, v in value.items()}
        return value

    @staticmethod
    def sanitize_order_by(order_by: str, allowed_fields: list[str]) -> str:
        """Sanitize order by clause."""
        order_by = sanitize_string(order_by, max_length=100)

        field = order_by.replace(" ", "").replace("-", "").replace("+", "")

        if field not in allowed_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid order by field. Allowed fields: {', '.join(allowed_fields)}",
            )

        direction = "DESC" if order_by.startswith("-") else "ASC"

        return f"{field} {direction}"