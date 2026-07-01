"""UK Companies House scraper. Plain HTTP REST API, no browser needed."""

from __future__ import annotations
import httpx
from .base import BaseScraper, RegistryResult


class UKCompaniesHouseScraper(BaseScraper):
    registry_name = "UK Companies House"
    jurisdiction = "GB"
    needs_browser = False

    API_BASE = "https://api.company-information.service.gov.uk"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, entity_name: str) -> list[RegistryResult]:
        headers = {"Authorization": self.api_key}

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.API_BASE}/search/companies",
                params={"q": entity_name, "items_per_page": 20},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("items", []):
            status = item.get("company_status", "").replace("_", " ").title()
            address_parts = []
            addr = item.get("address", {})
            for key in ["premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country"]:
                val = addr.get(key)
                if val:
                    address_parts.append(val)

            results.append(RegistryResult(
                entity_name=item.get("title", ""),
                registry_id=item.get("company_number"),
                status=status or None,
                jurisdiction="GB",
                formation_date=item.get("date_of_creation"),
                entity_type=item.get("company_type"),
                address=", ".join(address_parts) if address_parts else None,
                raw_data={
                    "company_status": item.get("company_status"),
                    "company_type": item.get("company_type"),
                    "snippet": item.get("snippet"),
                },
            ))

        return results

    async def get_details(self, entity_id: str) -> RegistryResult | None:
        """Get full company details by company number."""
        headers = {"Authorization": self.api_key}

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.API_BASE}/company/{entity_id}",
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

        status = data.get("company_status", "").replace("_", " ").title()
        address_parts = []
        addr = data.get("registered_office_address", {})
        for key in ["premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country"]:
            val = addr.get(key)
            if val:
                address_parts.append(val)

        other_names = []
        for prev in data.get("previous_company_names", []):
            other_names.append(prev.get("name", ""))

        return RegistryResult(
            entity_name=data.get("company_name", ""),
            registry_id=data.get("company_number"),
            status=status or None,
            jurisdiction="GB",
            formation_date=data.get("date_of_creation"),
            entity_type=data.get("type"),
            address=", ".join(address_parts) if address_parts else None,
            additional_names=other_names,
            raw_data=data,
        )
