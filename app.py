from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from database import DatasetError, get_dataset_preview, get_uploaded_schema, save_uploaded_file
from query_generator import SQLGenerationError, execute_sql_query, generate_sql_query

app = FastAPI()


class QueryRequest(BaseModel):
    query: str


@app.post("/upload_dataset/")
async def upload_dataset(file: UploadFile = File(...)):
    """Upload a CSV, XLSX, or SQLite dataset and make it active."""
    try:
        database_path = save_uploaded_file(file)
        schema = get_uploaded_schema()
        return {
            "success": True,
            "tables": list(schema.keys()),
            "database_path": str(database_path),
            "schema": schema,
            "preview": get_dataset_preview(),
        }
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not process dataset: {exc}") from exc
    finally:
        await file.close()


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
    """Execute a generated SQL query."""
    try:
        results = execute_sql_query(request.query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if results is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid SQL query or database execution error.",
        )

    return {
        "results": results["results"],
        "optimization_tips": results.get(
            "optimization_tips",
            "No optimization tips available.",
        ),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
