import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DATABASE_DIR = UPLOAD_DIR / "databases"
UPLOAD_DIR.mkdir(exist_ok=True)
DATABASE_DIR.mkdir(parents=True, exist_ok=True)

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")

_active_database_path = None
_active_engine = None
_active_schema = None
_active_relationships = None
_active_confirmed_relationships = None


class DatasetError(ValueError):
    """Raised when an uploaded dataset cannot be processed."""


def detect_file_type(filename):
    """Return the supported dataset type based on its file extension."""
    extension = Path(filename or "").suffix.lower()
    file_types = {".csv": "csv", ".xlsx": "excel", ".db": "sqlite"}
    if extension not in file_types:
        raise DatasetError("Unsupported file type. Upload a CSV, XLSX, or SQLite DB file.")
    return file_types[extension]


def _safe_table_name(name, existing_names=None):
    table_name = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_").lower()
    if not table_name:
        table_name = "dataset"
    if table_name[0].isdigit():
        table_name = f"table_{table_name}"

    existing_names = existing_names or set()
    base_name = table_name
    index = 1
    while table_name in existing_names:
        index += 1
        table_name = f"{base_name}_{index}"
    return table_name


def _new_database_path(source_path):
    return DATABASE_DIR / f"{source_path.stem}_{uuid.uuid4().hex}.db"


def _quote_identifier(identifier):
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def csv_to_sqlite(csv_path, table_name=None, database_path=None, existing_names=None):
    """Convert a CSV file in chunks to avoid loading the whole file into memory."""
    owns_database = database_path is None
    database_path = Path(database_path) if database_path else _new_database_path(Path(csv_path))
    existing_names = existing_names if existing_names is not None else set()
    table_name = _safe_table_name(table_name or Path(csv_path).stem, existing_names)
    try:
        with sqlite3.connect(database_path) as connection:
            chunks = pd.read_csv(csv_path, on_bad_lines="error", chunksize=50_000)
            wrote_rows = False
            for dataframe in chunks:
                if dataframe.columns.empty or dataframe.empty:
                    continue
                dataframe.to_sql(
                    table_name,
                    connection,
                    index=False,
                    if_exists="append" if wrote_rows else "replace",
                    chunksize=1_000,
                    method="multi",
                )
                wrote_rows = True
            if not wrote_rows:
                raise DatasetError("The CSV file does not contain any usable rows.")
        existing_names.add(table_name)
    except DatasetError:
        if owns_database:
            database_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if owns_database:
            database_path.unlink(missing_ok=True)
        raise DatasetError(f"Could not convert CSV to SQLite: {exc}") from exc
    return database_path


def excel_to_sqlite(excel_path, database_path=None, existing_names=None):
    """Convert every Excel sheet to a SQLite table and return its path."""
    owns_database = database_path is None
    database_path = Path(database_path) if database_path else _new_database_path(Path(excel_path))
    existing_names = existing_names if existing_names is not None else set()
    try:
        workbook = pd.ExcelFile(excel_path)
        if not workbook.sheet_names:
            raise DatasetError("The Excel file does not contain any sheets.")

        with sqlite3.connect(database_path) as connection:
            for sheet_name in workbook.sheet_names:
                dataframe = pd.read_excel(workbook, sheet_name=sheet_name)
                if dataframe.columns.empty or dataframe.empty:
                    continue
                table_name = _safe_table_name(sheet_name, existing_names)
                dataframe.to_sql(table_name, connection, index=False, if_exists="replace")
                existing_names.add(table_name)
    except DatasetError:
        if owns_database:
            database_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if owns_database:
            database_path.unlink(missing_ok=True)
        raise DatasetError(f"Invalid Excel file: {exc}") from exc

    if not get_sqlite_table_names(database_path):
        if owns_database:
            database_path.unlink(missing_ok=True)
        raise DatasetError("The Excel file does not contain any usable tables.")
    return database_path


def get_sqlite_table_names(database_path):
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return [row[0] for row in rows]


def _validate_sqlite_database(database_path):
    try:
        with sqlite3.connect(database_path) as connection:
            integrity_result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity_result.lower() != "ok":
            raise DatasetError("The SQLite database failed its integrity check.")
        if not get_sqlite_table_names(database_path):
            raise DatasetError("The SQLite database does not contain any tables.")
    except sqlite3.DatabaseError as exc:
        raise DatasetError(f"Corrupted or invalid SQLite database: {exc}") from exc


def merge_sqlite_database(source_path, database_path, existing_names):
    """Copy each table from an uploaded SQLite database into the active dataset."""
    _validate_sqlite_database(source_path)
    source_tables = get_sqlite_table_names(source_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("ATTACH DATABASE ? AS uploaded_source", (str(source_path),))
        try:
            for source_table in source_tables:
                table_name = _safe_table_name(source_table, existing_names)
                connection.execute(
                    f"CREATE TABLE {_quote_identifier(table_name)} AS "
                    f"SELECT * FROM uploaded_source.{_quote_identifier(source_table)}"
                )
                existing_names.add(table_name)
        finally:
            connection.execute("DETACH DATABASE uploaded_source")


def _persist_upload(upload_file):
    if not upload_file or not upload_file.filename:
        raise DatasetError("One of the uploaded files has no filename.")

    file_type = detect_file_type(upload_file.filename)
    suffix = Path(upload_file.filename).suffix.lower()
    upload_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with upload_path.open("wb") as destination:
        shutil.copyfileobj(upload_file.file, destination)

    if upload_path.stat().st_size == 0:
        upload_path.unlink(missing_ok=True)
        raise DatasetError(f"'{upload_file.filename}' is empty.")
    return file_type, upload_path


def load_uploaded_database(database_path):
    """Make an uploaded SQLite database the active database for this process."""
    global _active_database_path, _active_engine, _active_schema, _active_relationships, _active_confirmed_relationships

    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise DatasetError("Uploaded database file was not found.")
    _validate_sqlite_database(database_path)

    _active_database_path = database_path
    _active_engine = create_engine(f"sqlite:///{database_path.as_posix()}", pool_pre_ping=True)
    _active_schema = _extract_schema(_active_engine)
    _active_relationships = detect_relationships(_active_engine)
    _active_confirmed_relationships = []
    return _active_engine


def save_uploaded_file(upload_file):
    """Backward-compatible wrapper for code that uploads one file."""
    return save_uploaded_files([upload_file])


def save_uploaded_files(upload_files):
    """Import one or more files into a new SQLite database and activate it."""
    if not upload_files:
        raise DatasetError("Upload at least one CSV, XLSX, or SQLite database file.")

    database_path = _new_database_path(Path("combined_dataset.db"))
    existing_names = set()
    try:
        for upload_file in upload_files:
            file_type, upload_path = _persist_upload(upload_file)
            if file_type == "csv":
                csv_to_sqlite(upload_path, Path(upload_file.filename).stem, database_path, existing_names)
            elif file_type == "excel":
                excel_to_sqlite(upload_path, database_path, existing_names)
            else:
                merge_sqlite_database(upload_path, database_path, existing_names)

        load_uploaded_database(database_path)
        return database_path
    except Exception:
        database_path.unlink(missing_ok=True)
        raise


def _get_mysql_engine():
    missing_vars = [
        name
        for name, value in {
            "MYSQL_HOST": MYSQL_HOST,
            "MYSQL_USER": MYSQL_USER,
            "MYSQL_DATABASE": MYSQL_DATABASE,
        }.items()
        if not value
    ]
    if missing_vars:
        raise EnvironmentError(
            f"Missing required database environment variables: {', '.join(missing_vars)}"
        )

    database_url = (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@"
        f"{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
    )
    return create_engine(database_url, echo=True, pool_pre_ping=True)


def get_engine():
    """Return the active uploaded SQLite engine, or the configured MySQL engine."""
    global _active_engine
    if _active_engine is None:
        _active_engine = _get_mysql_engine()
    return _active_engine


def get_sql_dialect():
    """Return the SQL dialect used by the active database."""
    return get_engine().dialect.name


def _extract_schema(engine):
    """Extract table, column, and datatype metadata from an SQLAlchemy engine."""
    inspector = inspect(engine)
    schema = {}
    for table_name in inspector.get_table_names():
        schema[table_name] = [
            {"name": column["name"], "type": str(column["type"])}
            for column in inspector.get_columns(table_name)
        ]
    if not schema:
        raise DatasetError("The active database does not contain any tables.")
    return schema


def get_uploaded_schema():
    """Return schema metadata, cached for the active uploaded database."""
    if _active_database_path is not None and _active_schema is not None:
        return _active_schema
    return _extract_schema(get_engine())


def get_schema():
    """Return schema in the format expected by the SQL generator."""
    return {
        table_name: [f"{column['name']} ({column['type']})" for column in columns]
        for table_name, columns in get_uploaded_schema().items()
    }


def _normalise_name(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _singular_name(value):
    value = _normalise_name(value)
    if value.endswith("ies"):
        return f"{value[:-3]}y"
    return value[:-1] if value.endswith("s") else value


def _key_values(dataframe, column_name):
    values = set()
    for value in dataframe[column_name].dropna():
        value = str(value).strip()
        if value:
            values.add(value)
    return values


def _table_sample(engine, table_name, limit=2_000):
    with engine.connect() as connection:
        result = connection.execute(
            text(f"SELECT * FROM {_quote_identifier(table_name)} LIMIT :limit"),
            {"limit": limit},
        )
        return pd.DataFrame(result.fetchall(), columns=result.keys())


def detect_relationships(engine=None, sample_size=2_000):
    """Infer likely many-to-one table relationships from schema and sample data.

    These are suggestions, not enforced foreign keys. The next UI step will let
    users confirm or correct them before they guide SQL generation.
    """
    engine = engine or get_engine()
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    schema = {
        table_name: [column["name"] for column in inspector.get_columns(table_name)]
        for table_name in table_names
    }
    samples = {table_name: _table_sample(engine, table_name, sample_size) for table_name in table_names}
    relationships = []
    covered_source_columns = set()

    for source_table in table_names:
        for foreign_key in inspector.get_foreign_keys(source_table):
            constrained = foreign_key.get("constrained_columns", [])
            referred = foreign_key.get("referred_columns", [])
            target_table = foreign_key.get("referred_table")
            if len(constrained) == 1 and len(referred) == 1 and target_table:
                source_column, target_column = constrained[0], referred[0]
                relationships.append(
                    {
                        "source_table": source_table,
                        "source_column": source_column,
                        "target_table": target_table,
                        "target_column": target_column,
                        "relationship": "many-to-one",
                        "confidence": 100,
                        "reason": "Declared foreign key in the uploaded SQLite database.",
                    }
                )
                covered_source_columns.add((source_table, source_column))

    for source_table, source_columns in schema.items():
        source_sample = samples[source_table]
        for source_column in source_columns:
            if (source_table, source_column) in covered_source_columns:
                continue
            source_values = _key_values(source_sample, source_column)
            if len(source_values) < 3:
                continue

            source_name = _normalise_name(source_column)
            source_base = source_name.removesuffix("id")
            candidates = []
            for target_table, target_columns in schema.items():
                if target_table == source_table:
                    continue
                target_sample = samples[target_table]
                for target_column in target_columns:
                    target_values = _key_values(target_sample, target_column)
                    if len(target_values) < 3 or len(target_values) != len(target_sample[target_column].dropna()):
                        continue

                    overlap = len(source_values & target_values) / len(source_values)
                    if overlap < 0.80:
                        continue

                    target_name = _normalise_name(target_column)
                    score = int(overlap * 45)
                    reasons = [f"{overlap:.0%} of sampled values match"]
                    if source_name == target_name:
                        score += 30
                        reasons.append("matching column names")
                    if source_base and source_base == _singular_name(target_table):
                        score += 20
                        reasons.append("column name matches the target table")
                    if target_name == "id":
                        score += 10
                        reasons.append("target column is a unique id")

                    if score >= 70:
                        candidates.append(
                            (
                                score,
                                target_table,
                                target_column,
                                "; ".join(reasons),
                            )
                        )

            if candidates:
                score, target_table, target_column, reason = max(candidates, key=lambda item: item[0])
                relationships.append(
                    {
                        "source_table": source_table,
                        "source_column": source_column,
                        "target_table": target_table,
                        "target_column": target_column,
                        "relationship": "many-to-one",
                        "confidence": min(score, 99),
                        "reason": f"Inferred from {reason}.",
                    }
                )

    return sorted(
        relationships,
        key=lambda relationship: (-relationship["confidence"], relationship["source_table"], relationship["source_column"]),
    )


def get_detected_relationships():
    """Return cached relationship candidates for the active uploaded dataset."""
    if _active_database_path is not None and _active_relationships is not None:
        return _active_relationships
    return detect_relationships()


def set_confirmed_relationships(relationships):
    """Validate and retain the relationships the user has approved for SQL generation."""
    global _active_confirmed_relationships
    schema = get_uploaded_schema()
    confirmed = []
    seen = set()

    for relationship in relationships:
        source_table = relationship.get("source_table")
        source_column = relationship.get("source_column")
        target_table = relationship.get("target_table")
        target_column = relationship.get("target_column")
        key = (source_table, source_column, target_table, target_column)

        if not all(key):
            raise DatasetError("Every relationship must include a source and target table and column.")
        if source_table not in schema or target_table not in schema:
            raise DatasetError("A relationship references a table that is not in the active dataset.")

        source_columns = {column["name"] for column in get_uploaded_schema()[source_table]}
        target_columns = {column["name"] for column in get_uploaded_schema()[target_table]}
        if source_column not in source_columns or target_column not in target_columns:
            raise DatasetError("A relationship references a column that is not in the active dataset.")
        if source_table == target_table and source_column == target_column:
            raise DatasetError("A relationship cannot join a column to itself.")
        if key in seen:
            continue

        confirmed.append(
            {
                "source_table": source_table,
                "source_column": source_column,
                "target_table": target_table,
                "target_column": target_column,
                "relationship": relationship.get("relationship", "many-to-one"),
                "confidence": relationship.get("confidence", 100),
                "reason": relationship.get("reason", "Confirmed by the user."),
            }
        )
        seen.add(key)

    _active_confirmed_relationships = confirmed
    return confirmed


def get_confirmed_relationships():
    """Return the relationships approved by the user for the active dataset."""
    return _active_confirmed_relationships or []


def get_dataset_preview(limit=5):
    """Return a small preview of each active table for the upload UI."""
    preview = {}
    engine = get_engine()
    for table_name in get_uploaded_schema():
        with engine.connect() as connection:
            safe_table_name = table_name.replace('"', '""')
            result = connection.execute(
                text(f'SELECT * FROM "{safe_table_name}" LIMIT :limit'),
                {"limit": limit},
            )
            preview[table_name] = [dict(row._mapping) for row in result.fetchall()]
    return preview


def test_connection():
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            print("Database connection successful")
    except Exception as exc:
        print("Error connecting to the database:", exc)


if __name__ == "__main__":
    test_connection()
