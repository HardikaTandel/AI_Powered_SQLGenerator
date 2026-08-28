import pandas as pd
import requests
import streamlit as st

API_BASE_URL = "http://127.0.0.1:8000"

st.title("AI-Powered SQL Query Generator")

with st.sidebar:
    st.header("Upload Dataset")
    st.caption("Upload related CSV, Excel (.xlsx), or SQLite (.db) files together.")
    uploaded_files = st.file_uploader(
        "Choose datasets",
        type=["csv", "xlsx", "db"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )

    if uploaded_files:
        file_signature = "|".join(
            sorted(f"{uploaded_file.name}:{uploaded_file.size}" for uploaded_file in uploaded_files)
        )
        if st.session_state.get("uploaded_file_signature") != file_signature:
            try:
                response = requests.post(
                    f"{API_BASE_URL}/upload_dataset/",
                    files=[
                        (
                            "files",
                            (
                            uploaded_file.name,
                            uploaded_file.getvalue(),
                            uploaded_file.type,
                            ),
                        )
                        for uploaded_file in uploaded_files
                    ],
                    timeout=120,
                )
                response_data = response.json()
                if not response.ok:
                    st.error(response_data.get("detail", "Dataset upload failed."))
                else:
                    st.session_state["uploaded_file_signature"] = file_signature
                    st.session_state["dataset_metadata"] = response_data
                    st.session_state["relationship_candidates"] = response_data.get("relationships", [])
                    st.session_state["manual_relationships"] = []
                    st.session_state["relationship_saved"] = False
            except requests.RequestException as exc:
                st.error(f"Could not reach FastAPI: {exc}")

    dataset_metadata = st.session_state.get("dataset_metadata")
    if dataset_metadata:
        relationship_key_prefix = st.session_state.get("uploaded_file_signature", "active_dataset")
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

        relationships = dataset_metadata.get("relationships", [])
        st.subheader("Detected Relationships")
        if relationships:
            st.caption("Select the relationships you trust. Only saved relationships are used for generated JOIN queries.")
            selected_relationships = []
            for index, relationship in enumerate(relationships):
                source = f"{relationship['source_table']}.{relationship['source_column']}"
                target = f"{relationship['target_table']}.{relationship['target_column']}"
                label = f"{source} → {target} ({relationship['confidence']}% confidence)"
                if st.checkbox(label, value=True, key=f"relationship_{relationship_key_prefix}_{index}"):
                    selected_relationships.append(relationship)
        else:
            st.info("No high-confidence relationships were detected. Add them manually below.")
            selected_relationships = []

        st.subheader("Add Relationship Manually")
        table_columns = {
            table_name: [column["name"] for column in columns]
            for table_name, columns in dataset_metadata["schema"].items()
        }
        table_names = list(table_columns)
        source_table = st.selectbox("Source (child) table", table_names, key=f"source_table_{relationship_key_prefix}")
        source_column = st.selectbox(
            "Source column",
            table_columns[source_table],
            key=f"source_column_{relationship_key_prefix}",
        )
        target_table = st.selectbox("Target (parent) table", table_names, key=f"target_table_{relationship_key_prefix}")
        target_column = st.selectbox(
            "Target column",
            table_columns[target_table],
            key=f"target_column_{relationship_key_prefix}",
        )
        if st.button("Add manual relationship"):
            manual_relationship = {
                "source_table": source_table,
                "source_column": source_column,
                "target_table": target_table,
                "target_column": target_column,
                "relationship": "many-to-one",
                "confidence": 100,
                "reason": "Added manually by the user.",
            }
            if manual_relationship not in st.session_state["manual_relationships"]:
                st.session_state["manual_relationships"].append(manual_relationship)
            st.rerun()

        manual_relationships = st.session_state.get("manual_relationships", [])
        if manual_relationships:
            st.caption("Manual relationships")
            st.dataframe(pd.DataFrame(manual_relationships), use_container_width=True, hide_index=True)

        if st.button("Save confirmed relationships", type="primary"):
            confirmed_relationships = selected_relationships + manual_relationships
            try:
                response = requests.post(
                    f"{API_BASE_URL}/relationships/",
                    json={"relationships": confirmed_relationships},
                    timeout=30,
                )
                response_data = response.json()
                if not response.ok:
                    st.error(response_data.get("detail", "Could not save relationships."))
                else:
                    st.session_state["relationship_saved"] = True
                    st.success(f"Saved {len(response_data['relationships'])} relationship(s) for JOIN generation.")
            except requests.RequestException as exc:
                st.error(f"Could not save relationships: {exc}")

        if st.session_state.get("relationship_saved"):
            st.info("Generated multi-table SQL will now use the saved relationships.")

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
    except requests.RequestException as exc:
        st.error(f"Could not reach FastAPI: {exc}")

if "generated_sql" in st.session_state:
    st.subheader("Generated SQL")
    edited_sql = st.text_area(
        "SQL query",
        key="editable_sql",
        height=180,
        label_visibility="collapsed",
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
                st.session_state["row_limit"] = response_data.get("row_limit")
        except requests.RequestException as exc:
            st.error(f"Could not reach FastAPI: {exc}")

if "query_results" in st.session_state:
    st.subheader("Query Results")
    results = st.session_state["query_results"]
    if results:
        st.dataframe(pd.DataFrame(results), use_container_width=True)
        if st.session_state.get("row_limit") and len(results) >= st.session_state["row_limit"]:
            st.warning(f"Showing the first {st.session_state['row_limit']} rows.")
    else:
        st.info("The query executed successfully but did not return any rows.")
