# ruff: noqa: E501
"""The company cardholder form comes filled from what the company already gave: the director and registered address of
its Airwallex registration, the Finance Director's email; without a registration, the Super admin's name."""
from venthora import airwallex_onboarding as ob
from venthora.api import _cardholder_default


class Org:
    def __init__(self, profile, users, units):
        self.profile, self.users, self.units = profile, users, units

    def get(self, kind, _id):
        return self.profile if (kind, _id) == ob.PROFILE else None

    def list(self, kind):
        return {"user": self.users, "unit": self.units}.get(kind, [])


FD = {"name": "Hadi Halim", "email": "hadi.halim@muwahid.org", "role": "super_admin", "active": True}


def test_the_registration_fills_the_cardholder():
    org = Org({"director_first_name": "Hadi", "director_last_name": "Halim", "director_date_of_birth": "1984-05-12",
               "address_line1": "10 Anson Road #20-01", "suburb": "Singapore", "postcode": "079903", "country_code": "SG"},
              [FD], [{"unit_id": "hq", "country": "SG"}])
    d = _cardholder_default(org)
    assert (d["first_name"], d["last_name"], d["email"], d["date_of_birth"]) == ("Hadi", "Halim", "hadi.halim@muwahid.org", "1984-05-12")
    assert d["address"] == {"line1": "10 Anson Road #20-01", "city": "Singapore", "postcode": "079903", "country": "SG"}


def test_without_a_registration_the_super_admin_and_head_office_are_used():
    d = _cardholder_default(Org(None, [FD], [{"unit_id": "hq", "country": "ID"}]))
    assert (d["first_name"], d["last_name"], d["address"]["country"], d["date_of_birth"]) == ("Hadi", "Halim", "ID", "")
