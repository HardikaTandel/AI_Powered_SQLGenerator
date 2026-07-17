import pandas as pd
import requests
import streamlit as st

API_BASE_URL = "http://127.0.0.1:8000"

st.title("AI-Powered SQL Query Generator")

with st.sidebar:
    st.header("Upload Dataset")
    st.caption("Supported: CSV, Excel (.xlsx), SQLite (.db)")
    uploaded_file = st.file_uploader(
        "Choose a dataset",
        type=["csv", "xlsx", "db"],
        label_visibility="collapsed",
    )

    if uploaded_file is not None:
        file_signature = f"{uploaded_file.name}:{uploaded_file.size}"
        if st.session_state.get("uploaded_file_signature") != file_signature:
            try:
                response = requests.post(
                    f"{API_BASE_URL}/upload_dataset/",
                    files={
                        "file": (
                            uploaded_file.name,
                            uploaded_file.getvalue(),
                            uploaded_file.type,
                        )
                    },
                    timeout=120,
                )
                response_data = response.json()
                if not response.ok:
                    st.error(response_data.get("detail", "Dataset upload failed."))
                else:
                    st.session_state["uploaded_file_signature"] = file_signature
                    st.session_state["dataset_metadata"] = response_data
            except requests.RequestException as exc:
                st.error(f"Could not reach FastAPI: {exc}")

    dataset_metadata = st.session_state.get("dataset_metadata")
    if dataset_metadata:
        st.success("Upload successful")
        st.subheader("Detected Tables")
        st.write(dataset_metadata["tables"])

        st.subheader("Detected Columns")
        for table_name, columns in dataset_metadata["schema"].items():
            st.write(f"**{table_name}**")
            st.write([f"{column['name']} ({column['type']})" for column in columns])

        st.subheader("Dataset Preview")
        for table_name, rows in dataset_metadata.get("preview", {}).items():
            st.write(f"**{table_name}**")
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

query_input = st.text_area("Enter your natural language query:")

if st.button("Generate SQL Query"):
    try:
        response = requests.post(
            f"{API_BASE_URL}/generate_sql/",
            json={"query": query_input},
            timeout=620,
        )
        response_data = response.json()
        if not response.ok:
            st.error(response_data.get("detail", "Error generating SQL query."))
        else:
            sql_query = response_data["sql_query"]
            st.session_state["generated_sql"] = sql_query
            st.session_state["editable_sql"] = sql_query
            st.session_state.pop("query_results", None)
            st.session_state.pop("optimization_tips", None)
    except requests.RequestException as exc:
        st.error(f"Could not reach FastAPI: {exc}")

if "generated_sql" in st.session_state:
    st.subheader("Generated SQL")
    edited_sql = st.text_area(
        "Edit SQL before execution",
        key="editable_sql",
        height=180,
        help="You can correct table or column names before executing the query.",
    )

    if st.button("Execute SQL"):
        try:
            response = requests.post(
                f"{API_BASE_URL}/execute_sql/",
                json={"query": edited_sql},
                timeout=120,
            )
            response_data = response.json()
            if not response.ok:
                st.error(response_data.get("detail", "Error executing SQL query."))
            else:
                st.session_state["query_results"] = response_data.get("results", [])
                st.session_state["optimization_tips"] = response_data.get(
                    "optimization_tips",
                    "No optimization tips available.",
                )
        except requests.RequestException as exc:
            st.error(f"Could not reach FastAPI: {exc}")

if "query_results" in st.session_state:
    st.subheader("Query Results")
    results = st.session_state["query_results"]
    if results:
        st.dataframe(pd.DataFrame(results), use_container_width=True)
    else:
        st.info("The query executed successfully but did not return any rows.")

    st.subheader("Optimization Tips")
    st.write(st.session_state.get("optimization_tips", "No optimization tips available."))
