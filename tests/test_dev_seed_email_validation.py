"""The dev clinic-seed request must validate email at the boundary.

An invalid address (e.g. a ``.test`` TLD) previously created a user whose email
the login RESPONSE model — keyed on Pydantic ``EmailStr`` — could not serialize,
producing a user that 403s on every login. ``SeedClinicRequest.email`` is now an
``Optional[EmailStr]``, so the bad value is rejected (422) before any DB write.

These assertions exercise only the Pydantic request model, so they are DB-free.
"""
import pytest
from pydantic import ValidationError

from app.api.clinic.v1.endpoints.dev_tools import SeedClinicRequest


@pytest.mark.parametrize(
    "bad_email",
    [
        "clinic@example.test",  # the original repro: a .test TLD
        "not-an-email",
        "clinic@",
        "@example.com",
        "clinic example@x.com",
    ],
)
def test_seed_clinic_rejects_invalid_email(bad_email):
    with pytest.raises(ValidationError):
        SeedClinicRequest(email=bad_email)


def test_seed_clinic_accepts_valid_or_omitted_email():
    # A real address validates...
    assert SeedClinicRequest(email="clinic@example.com").email == "clinic@example.com"
    # ...and omitting it (the random-default path) stays valid.
    assert SeedClinicRequest().email is None
