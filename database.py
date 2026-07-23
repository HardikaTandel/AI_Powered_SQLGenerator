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


def csv_to_sqlite(csv_path, table_name=None):
    """Convert a CSV file to a new SQLite database and return its path."""
    try:
        dataframe = pd.read_csv(csv_path, on_bad_lines="error")
    except Exception as exc:
        raise DatasetError(f"Invalid CSV file: {exc}") from exc

    if dataframe.columns.empty:
        raise DatasetError("The CSV file does not contain any columns.")

    database_path = _new_database_path(Path(csv_path))
    table_name = _safe_table_name(table_name or Path(csv_path).stem)
    try:
        with sqlite3.connect(database_path) as connection:
            dataframe.to_sql(table_name, connection, index=False, if_exists="replace")
    except Exception as exc:
        database_path.unlink(missing_ok=True)
        raise DatasetError(f"Could not convert CSV to SQLite: {exc}") from exc
    return database_path


def excel_to_sqlite(excel_path):
    """Convert every Excel sheet to a SQLite table and return its path."""
    database_path = _new_database_path(Path(excel_path))
    try:
        workbook = pd.ExcelFile(excel_path)
        if not workbook.sheet_names:
            raise DatasetError("The Excel file does not contain any sheets.")

        table_names = set()
        with sqlite3.connect(database_path) as connection:
            for sheet_name in workbook.sheet_names:
                dataframe = pd.read_excel(workbook, sheet_name=sheet_name)
                if dataframe.columns.empty:
                    continue
                table_name = _safe_table_name(sheet_name, table_names)
                dataframe.to_sql(table_name, connection, index=False, if_exists="replace")
                table_names.add(table_name)
    except DatasetError:
        database_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        database_path.unlink(missing_ok=True)
        raise DatasetError(f"Invalid Excel file: {exc}") from exc

    if not get_sqlite_table_names(database_path):
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


def load_uploaded_database(database_path):
    """Make an uploaded SQLite database the active database for this process."""
    global _active_database_path, _active_engine, _active_schema

    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise DatasetError("Uploaded database file was not found.")
    _validate_sqlite_database(database_path)

    _active_database_path = database_path
    _active_engine = create_engine(f"sqlite:///{database_path.as_posix()}", pool_pre_ping=True)
    _active_schema = _extract_schema(_active_engine)
    return _active_engine


def save_uploaded_file(upload_file):
    """Persist an uploaded file and activate its resulting SQLite database."""
    if not upload_file or not upload_file.filename:
        raise DatasetError("No file was uploaded.")

    file_type = detect_file_type(upload_file.filename)
    suffix = Path(upload_file.filename).suffix.lower()
    upload_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"

    with upload_path.open("wb") as destination:
        shutil.copyfileobj(upload_file.file, destination)

    if upload_path.stat().st_size == 0:
        upload_path.unlink(missing_ok=True)
        raise DatasetError("The uploaded file is empty.")

    database_path = None
    try:
        if file_type == "csv":
            database_path = csv_to_sqlite(upload_path, Path(upload_file.filename).stem)
        elif file_type == "excel":
            database_path = excel_to_sqlite(upload_path)
        else:
            database_path = DATABASE_DIR / f"{upload_path.stem}_{uuid.uuid4().hex}.db"
            shutil.copy2(upload_path, database_path)

        load_uploaded_database(database_path)
        return database_path
    except Exception:
        if database_path is not None:
            Path(database_path).unlink(missing_ok=True)
        if file_type != "sqlite":
            upload_path.unlink(missing_ok=True)
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
