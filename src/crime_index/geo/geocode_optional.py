from __future__ import annotations


def geocode_address_optional(address: str) -> None:
    """Placeholder-free explicit no-op for paid/API geocoding.

    The local pipeline deliberately avoids external geocoding services. Add a
    source-specific implementation here only when a vetted local or free geocoder
    is available and the downstream use case permits it.
    """

    return None
