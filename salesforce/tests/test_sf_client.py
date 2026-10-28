"""Placeholder tests for Salesforce client."""

from salesforce.field_map import OBJECT_NAME
from salesforce.sf_client import SalesforceClient


def test_object_name():
    assert OBJECT_NAME == "Site__c"


def test_client_class_exists():
    assert SalesforceClient is not None
