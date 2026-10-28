"""Salesforce auth for Site__c query and update."""

from __future__ import annotations

import logging
import os

from simple_salesforce import Salesforce

logger = logging.getLogger(__name__)


class SalesforceClient:
    """Authenticate against Salesforce. Query/update live in enrichment.sf_ops."""

    def __init__(self) -> None:
        self.sf = Salesforce(
            username=os.environ["SF_USERNAME"],
            password=os.environ["SF_PASSWORD"],
            security_token=os.environ["SF_SECURITY_TOKEN"],
            domain=os.environ.get("SF_DOMAIN", "login"),
        )
        instance = getattr(self.sf, "sf_instance", None) or getattr(
            self.sf, "base_url", ""
        )
        logger.info("Salesforce authenticated — API instance: %s", instance)
