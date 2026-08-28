import io
import json

import pytest

import database
import query_generator


class UploadedFile:
    """Small stand-in for FastAPI's UploadFile used by import tests."""

    def __init__(self, filename, content):
        self.filename = filename
        self.file = io.BytesIO(content.encode("utf-8"))


class FakeOllamaResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(
            {
                "model": "test-model",
                "done_reason": "stop",
                "message": {"content": json.dumps({"sql": "SELECT customer_name FROM customers LIMIT 500"})},
            }
        ).encode("utf-8")


@pytest.fixture(autouse=True)
def isolated_dataset_state(tmp_path, monkeypatch):
    """Keep test uploads and process-global active-dataset state isolated."""
    upload_dir = tmp_path / "uploads"
    database_dir = upload_dir / "databases"
    upload_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(database, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(database, "DATABASE_DIR", database_dir)
    monkeypatch.setattr(database, "_active_database_path", None)
    monkeypatch.setattr(database, "_active_engine", None)
    monkeypatch.setattr(database, "_active_schema", None)
    monkeypatch.setattr(database, "_active_relationships", None)
    monkeypatch.setattr(database, "_active_confirmed_relationships", None)
    query_generator._generate_sql_query_cached.cache_clear()


def upload_related_customer_data():
    return database.save_uploaded_files(
        [
            UploadedFile(
                "customers.csv",
                "customer_id,customer_name\n1,Ada\n2,Grace\n3,Linus\n",
            ),
            UploadedFile(
                "orders.csv",
                "order_id,customer_id,total\n101,1,40\n102,2,50\n103,3,60\n104,1,70\n",
            ),
        ]
    )


def test_multiple_csv_files_become_one_dataset_and_relationship_is_detected():
    database_path = upload_related_customer_data()

    assert database_path.is_file()
    assert set(database.get_uploaded_schema()) == {"customers", "orders"}
    assert any(
        relationship["source_table"] == "orders"
        and relationship["source_column"] == "customer_id"
        and relationship["target_table"] == "customers"
        and relationship["target_column"] == "customer_id"
        for relationship in database.get_detected_relationships()
    )


def test_confirmed_relationships_are_validated_and_stored():
    upload_related_customer_data()
    saved = database.set_confirmed_relationships(
        [
            {
                "source_table": "orders",
                "source_column": "customer_id",
                "target_table": "customers",
                "target_column": "customer_id",
            }
        ]
    )

    assert saved == database.get_confirmed_relationships()
    with pytest.raises(database.DatasetError, match="column"):
        database.set_confirmed_relationships(
            [
                {
                    "source_table": "orders",
                    "source_column": "missing_column",
                    "target_table": "customers",
                    "target_column": "customer_id",
                }
            ]
        )


def test_confirmed_relationships_are_sent_to_ollama(monkeypatch):
    upload_related_customer_data()
    database.set_confirmed_relationships(
        [
            {
                "source_table": "orders",
                "source_column": "customer_id",
                "target_table": "customers",
                "target_column": "customer_id",
            }
        ]
    )
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = json.loads(request.data.decode("utf-8"))
        return FakeOllamaResponse(captured["request"])

    monkeypatch.setattr(query_generator, "urlopen", fake_urlopen)

    sql = query_generator.generate_sql_query("List customer names that have orders")

    prompt = captured["request"]["messages"][0]["content"]
    assert "orders.customer_id REFERENCES customers.customer_id" in prompt
    assert "join tables only through the confirmed relationships" in prompt
    assert sql == "SELECT customer_name FROM customers LIMIT 500"
