"""
Entity Lookup v3b (Python) — composed LookupTools.

Faithful equivalent of php/tools.php class LookupTools: the shared fetch/whois/log base
(ToolBase) combined with every registry tool cluster (ported as mixins).
"""
from toolbase import ToolBase
from tools_google import GoogleMixin
from tools_ch import CompaniesHouseMixin
from tools_sec import SecMixin
from tools_northdata import NorthDataMixin
from tools_delaware import DelawareMixin
from tools_bizapedia import BizapediaMixin
from tools_opencorporates import OpenCorporatesMixin


class LookupTools(ToolBase, GoogleMixin, CompaniesHouseMixin, SecMixin,
                  NorthDataMixin, DelawareMixin, BizapediaMixin, OpenCorporatesMixin):
    """All Entity-Lookup registry + fetch tools on one object (ToolBase provides __init__)."""
    pass
