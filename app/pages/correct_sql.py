import pandas as pd
import streamlit as st

from utils.db import get_sql_job_full, get_sql_jobs, update_sql_user_edited_sql


ALL = "All"

_SQL_VIEW_OPTIONS = {
    "FR_SQL": "FR_SQL",
    "EDIT_FR_SQL": "EDIT_FR_SQL",
    "TO_SQL": "TO_SQL",
    "BIND SQL": "BIND_SQL",
    "BIND SET": "BIND_SET",
    "TEST SQL": "TEST_SQL",
    "TUNED_TO_SQL": "TUNED_TO_SQL",
    "TUNED RESULT": "TUNED_RESULT",
    "FORMATTED SQL": "FORMATTED_SQL",
    "USER_EDITED": "USER_EDITED",
    "LOG": "LOG",
}

_USER_SQL_OPTIONS = {
    "TO_SQL": ("TOBE", "TO_SQL"),
    "BIND SQL": ("BIND", "BIND_SQL"),
    "TEST SQL": ("TEST", "TEST_SQL"),
}


def _prepare_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in (
        "ROW_ID",
        "SQL_ID",
        "SPACE_NM",
        "STATUS_CONVERSION",
        "STATUS_TUNING",
        "PRIORITY",
        "MAP_TYPE",
        "TARGET_TABLE",
        "USER_EDITED",
        "RETRY_COUNT",
        "LOG",
    ):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    return df


def _contains(series: pd.Series, keyword: str) -> pd.Series:
    keyword = keyword.strip()
    if not keyword:
        return pd.Series(True, index=series.index)
    return series.fillna("").astype(str).str.contains(keyword, case=False, na=False, regex=False)


def _options(df: pd.DataFrame, column: str) -> list[str]:
    values = [v for v in df[column].dropna().astype(str).str.strip().unique().tolist() if v]
    return [ALL] + sorted(values)


def _job_label(row: pd.Series) -> str:
    return (
        f"{row.get('SPACE_NM') or '-'} / {row.get('SQL_ID') or '-'} "
        f"| STATUS_CONVERSION={row.get('STATUS_CONVERSION') or 'NULL'} "
        f"| STATUS_TUNING={row.get('STATUS_TUNING') or 'NULL'} "
        f"| USER_EDITED={row.get('USER_EDITED') or 'N'} "
        f"| RETRY={row.get('RETRY_COUNT') or '0'} "
        f"| PRIORITY={row.get('PRIORITY') or '-'}"
    )


def render():
    st.title("User Edited SQL Manager")

    if st.button("Refresh"):
        st.rerun()

    try:
        jobs = get_sql_jobs()
    except Exception as exc:
        st.error(f"DB connection failed: {exc}")
        return

    if not jobs:
        st.info("No SQL jobs found.")
        return

    df_all = _prepare_df(jobs)

    with st.expander("Search / Filter", expanded=True):
        c1, c2, c3, c4 = st.columns([1.4, 1.4, 1, 1])
        with c1:
            sql_id_query = st.text_input("SQL_ID LIKE")
        with c2:
            namespace_query = st.text_input("SPACE_NM LIKE")
        with c3:
            sel_status = st.selectbox("STATUS_CONVERSION", _options(df_all, "STATUS_CONVERSION"))
        with c4:
            sel_user_edited = st.selectbox("USER_EDITED", _options(df_all, "USER_EDITED"))

    df = df_all.copy()
    df = df[_contains(df["SQL_ID"], sql_id_query)]
    df = df[_contains(df["SPACE_NM"], namespace_query)]
    if sel_status != ALL:
        df = df[df["STATUS_CONVERSION"] == sel_status]
    if sel_user_edited != ALL:
        df = df[df["USER_EDITED"] == sel_user_edited]

    if df.empty:
        st.warning("No SQL jobs match the current filters.")
        return

    st.caption(f"Rows: {len(df)} / {len(df_all)}")
    records = df.to_dict("records")
    selected_idx = st.selectbox(
        "SQL Job",
        range(len(records)),
        format_func=lambda i: _job_label(pd.Series(records[i])),
    )
    row_id = str(records[selected_idx]["ROW_ID"])
    detail = get_sql_job_full(row_id) or records[selected_idx]

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.write(f"**SQL_ID:** {detail.get('SQL_ID') or '-'}")
    with m2:
        st.write(f"**SPACE_NM:** {detail.get('SPACE_NM') or '-'}")
    with m3:
        st.metric("STATUS_CONVERSION", detail.get("STATUS_CONVERSION") or "-")
    with m4:
        st.metric("USER_EDITED", detail.get("USER_EDITED") or "N")

    st.divider()

    left, right = st.columns(2)
    view_labels = list(_SQL_VIEW_OPTIONS.keys())
    with left:
        st.subheader("Current Columns")
        view_label = st.selectbox("View column", view_labels, index=0)
        st.code(detail.get(_SQL_VIEW_OPTIONS[view_label]) or "(empty)", language="sql")

    with right:
        st.subheader("Save User Edited SQL")
        edit_label = st.selectbox("Target SQL", list(_USER_SQL_OPTIONS.keys()))
        sql_kind, sql_column = _USER_SQL_OPTIONS[edit_label]
        current_value = detail.get(sql_column) or ""
        sql_text = st.text_area(
            "SQL",
            value=current_value,
            height=420,
            placeholder="Enter the SQL to preserve as user-edited output.",
        )
        if st.button("Save and Mark USER_EDITED", type="primary", width="stretch"):
            ok, message = update_sql_user_edited_sql(row_id, sql_kind, sql_text)
            if ok:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
