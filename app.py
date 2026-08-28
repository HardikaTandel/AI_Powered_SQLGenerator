from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from database import (
    DatasetError,
    get_dataset_preview,
    get_detected_relationships,
    get_uploaded_schema,
    save_uploaded_files,
    set_confirmed_relationships,
)
from query_generator import SQLGenerationError, execute_sql_query, generate_sql_query

app = FastAPI()


class QueryRequest(BaseModel):
    query: str
    analyze: bool = False


class Relationship(BaseModel):
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    relationship: str = "many-to-one"
    confidence: int = 100
    reason: str = "Confirmed by the user."


class RelationshipUpdateRequest(BaseModel):
    relationships: list[Relationship]


@app.post("/upload_dataset/")
async def upload_dataset(files: list[UploadFile] = File(...)):
    """Upload one or more datasets and make their combined database active."""
    try:
        database_path = save_uploaded_files(files)
        schema = get_uploaded_schema()
        return {
            "success": True,
            "tables": list(schema.keys()),
            "database_path": str(database_path),
            "schema": schema,
            "preview": get_dataset_preview(),
            "relationships": get_detected_relationships(),
        }
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not process dataset: {exc}") from exc
    finally:
        for file in files:
            await file.close()


@app.post("/relationships/")
async def save_relationships(request: RelationshipUpdateRequest):
    """Save user-confirmed relationships for the active uploaded dataset."""
    try:
        relationships = set_confirmed_relationships(
            [
                relationship.model_dump() if hasattr(relationship, "model_dump") else relationship.dict()
                for relationship in request.relationships
            ]
        )
        return {"success": True, "relationships": relationships}
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/generate_sql/")
async def generate_sql(request: QueryRequest):
    """Generate SQL query from natural language input."""
    try:
        sql_query = generate_sql_query(request.query)
    except SQLGenerationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return {"sql_query": sql_query}


@app.post("/execute_sql/")
async def execute_sql(request: QueryRequest):
    """Execute a safe, read-only SQL query."""
    try:
        results = execute_sql_query(request.query, analyze=request.analyze)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if results is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid SQL query or database execution error.",
        )

    return {
        "results": results["results"],
        "row_limit": results["row_limit"],
        "execution_plan": results.get("execution_plan"),
        "optimization_tips": results.get(
            "optimization_tips",
            "No optimization tips available.",
        ),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
