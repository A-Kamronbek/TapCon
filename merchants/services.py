"""Seller-facing business logic.

Views stay thin so the future DRF API (v2) can reuse these functions unchanged.
"""
from django.db import transaction

from .models import SellerProfile


def get_profile(user) -> SellerProfile | None:
    return SellerProfile.objects.filter(user=user).first()


@transaction.atomic
def update_profile(user, *, full_name: str):
    user.full_name = full_name
    user.save(update_fields=["full_name"])
    return user


@transaction.atomic
def update_business(profile: SellerProfile, *, business_name, contact_phone="", address=""):
    """Business details a seller may edit freely.

    Approval status is deliberately not touched here — only an admin moves a
    seller between pending / approved / suspended.
    """
    profile.business_name = business_name
    profile.contact_phone = contact_phone
    profile.address = address
    profile.save(update_fields=["business_name", "contact_phone", "address", "updated_at"])
    return profile
