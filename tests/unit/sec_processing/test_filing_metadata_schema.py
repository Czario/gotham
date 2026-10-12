"""Mongo schema migration coverage for foreign annual SEC filing records."""

from database.config.mongodb_config import (
    COLLECTIONS_CONFIG,
    ensure_filing_metadata_collection,
)
from database.config.mongodb_dimensional_config import ENHANCED_COLLECTIONS_CONFIG


class _FakeCollection:
    def __init__(self):
        self.indexes = []

    def create_index(self, keys, **options):
        self.indexes.append((keys, options))


class _FakeDatabase:
    def __init__(self, existing=()):
        self.names = set(existing)
        self.collections = {}
        self.commands = []
        self.created = []

    def list_collection_names(self):
        return sorted(self.names)

    def create_collection(self, name, **options):
        self.created.append((name, options))
        self.names.add(name)
        self.collections.setdefault(name, _FakeCollection())

    def command(self, command):
        self.commands.append(command)

    def __getitem__(self, name):
        self.names.add(name)
        return self.collections.setdefault(name, _FakeCollection())


def _form_enum(config):
    return config["filings"]["validator"]["$jsonSchema"]["properties"]["form_type"]["enum"]


def test_base_and_dimensional_filing_schemas_allow_20f_and_amendments():
    for config in (COLLECTIONS_CONFIG, ENHANCED_COLLECTIONS_CONFIG):
        forms = _form_enum(config)
        assert "20-F" in forms
        assert "20-F/A" in forms
        props = config["filings"]["validator"]["$jsonSchema"]["properties"]
        assert props["report_date"]["bsonType"] == ["date", "null"]


def test_ensure_filing_metadata_schema_creates_collection_and_unique_accession_index():
    db = _FakeDatabase()

    collection = ensure_filing_metadata_collection(db)

    assert db.created[0][0] == "filings"
    assert collection.indexes
    assert any(options.get("unique") for _, options in collection.indexes)


def test_ensure_filing_metadata_schema_collmods_existing_collection():
    db = _FakeDatabase(existing={"filings"})

    ensure_filing_metadata_collection(db)

    assert db.created == []
    assert db.commands[0]["collMod"] == "filings"
