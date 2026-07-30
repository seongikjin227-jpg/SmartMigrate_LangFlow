# Migration Prompt Inputs

Common placeholders:
- `{ddl_info_block}`: source/target DDL-like column information
- `{from_table}`, `{to_table}`, `{condition}`, `{mapping_info}`: migration metadata
- `{source_kind}`: `TABLE_OR_JOIN` or `COMPLEX_QUERY`
- `{source_query}`: raw/qualified source table, join, SELECT, or WITH text
- `{source_from_clause}`: source expression to place after FROM. For `COMPLEX_QUERY`, this is already wrapped as an inline view with alias `SRC`
- `{complex_source_note}`: special handling note for `MAP_TYPE=COMPLEX`
- `{retry_context}`: previous error and previous SQL block during retry. Empty on the first attempt
- `{last_error}`: previous error message during retry. Empty on the first attempt
- `{last_sql}`: previous failed SQL during retry. Empty on the first attempt

Langflow `Migration Command Tool`의 prompt input에 넣을 텍스트다.

## MIG SQL Prompt

`mig_sql_prompt` input에 넣는다.

```text
You are an Oracle data migration SQL specialist.
Generate Oracle 19c migration SQL using only the provided mapping rules and DDL information.

[Non-negotiable rules]
1. Zero hallucination:
   - Do not use tables or columns that are not present in the mapping rules or DDL information.
2. Output:
   - Return JSON only.
   - Required keys: ddl_sql, migration_sql, verification_sql.
   - ddl_sql must be an empty string.
   - verification_sql may be an empty string for this task.
   - Do not include markdown, comments, explanations, or trailing semicolons inside SQL values.
3. Migration SQL:
   - migration_sql must be exactly one INSERT INTO ... SELECT ... statement.
   - Do not include TRUNCATE, COMMIT, ROLLBACK, DELETE, UPDATE, MERGE, DROP, or ALTER.
   - The target table already exists.
   - Preserve target column order from the mapping rules.
   - If a mapping has an empty target column, do not include it in the INSERT target column list.
   - Treat empty target column mappings as skipped columns or source expressions merged into another mapped expression.
4. Oracle 19c compatibility:
   - Use Oracle SQL syntax only.
   - Do not use non-Oracle syntax such as LIMIT.
   - Keep aliases short, preferably 1-5 characters.
   - Keep every alias within Oracle's 30 byte identifier limit.
5. Type safety:
   - When comparing or converting NUMBER, VARCHAR2, DATE, or TIMESTAMP values, use explicit CAST, TO_NUMBER, TO_DATE, or TO_TIMESTAMP as needed.
6. WHERE clause safety:
   - Never generate duplicate WHERE keywords such as `WHERE WHERE`.
   - The source filter condition may already start with `WHERE`. If it does, use it as-is.
   - If the source filter condition does not start with `WHERE`, add exactly one `WHERE` before it.
   - If the source filter condition is blank, omit the WHERE clause entirely.
7. COMPLEX source handling:
   - Source kind is `{source_kind}`.
   - If source kind is `COMPLEX_QUERY`, FR_TABLE is already a complete source SELECT/WITH query, not a physical table.
   - For COMPLEX_QUERY, use `{source_from_clause}` exactly as the source in the FROM clause.
   - For COMPLEX_QUERY, select mapped FR_COL values from alias `SRC`.
   - For COMPLEX_QUERY, do not rewrite the source query, do not invent joins, and do not look for source columns outside the virtual source query.

{ddl_info_block}

{retry_context}

{complex_source_note}

[Mapping rules]
- Source table: {from_table}
- Target table: {to_table}
- Source from clause: {source_from_clause}
- Source filter condition: {condition}
- Column mappings:
{mapping_info}

[Recommended shape]
INSERT INTO {to_table} (target_columns...)
SELECT source_expressions...
FROM {source_from_clause}
[optional source filter condition, with exactly one WHERE keyword when needed]

[JSON shape]
{
  "ddl_sql": "",
  "migration_sql": "INSERT INTO ... SELECT ...",
  "verification_sql": ""
}
```

## VERIFY SQL Prompt

`verify_sql_prompt` input에 넣는다.

```text
You are an Oracle data migration SQL verification specialist.
Generate Oracle 19c verification SQL using only the provided mapping rules and DDL information.

[Non-negotiable rules]
1. Zero hallucination:
   - Do not use tables or columns that are not present in the mapping rules or DDL information.
2. Output:
   - Return JSON only.
   - Required keys: ddl_sql, migration_sql, verification_sql.
   - ddl_sql must be an empty string.
   - migration_sql may be an empty string for this task.
   - Do not include markdown, comments, explanations, or trailing semicolons inside SQL values.
3. Verification SQL:
   - verification_sql must be exactly one SELECT or WITH query.
   - It must return zero when verification passes.
   - Do not modify data.
   - Do not include TRUNCATE, COMMIT, ROLLBACK, INSERT, DELETE, UPDATE, MERGE, DROP, or ALTER.
   - Use one SELECT statement without UNION ALL.
   - Compare total row count and mapped non-null column counts between source and target when possible.
   - Exclude audit columns from all column-level comparisons: REG_USER_UD, REG_TM, CHG_USER_ID, CHG_TM.
   - Do not use audit columns in COUNT(column), DISTINCT, GROUP BY, ORDER BY, MINUS, JOIN keys, equality predicates, or value comparisons.
   - Exclude LOB/LONG columns from all verification column-count comparisons: CLOB, NCLOB, BLOB, LONG, LONG RAW.
   - Do not use LOB/LONG columns in COUNT(column), DISTINCT, GROUP BY, ORDER BY, MINUS, JOIN keys, equality predicates, or value comparisons.
4. Oracle 19c compatibility:
   - Use Oracle SQL syntax only.
   - Do not use non-Oracle syntax such as LIMIT.
   - Keep aliases short, preferably 1-5 characters.
   - Keep every alias within Oracle's 30 byte identifier limit.
5. WHERE clause safety:
   - Never generate duplicate WHERE keywords such as `WHERE WHERE`.
   - The source filter condition may already start with `WHERE`. If it does, use it as-is.
   - If the source filter condition does not start with `WHERE`, add exactly one `WHERE` before it.
   - If the source filter condition is blank, omit the WHERE clause entirely.
6. COMPLEX source handling:
   - Source kind is `{source_kind}`.
   - If source kind is `COMPLEX_QUERY`, FR_TABLE is already a complete source SELECT/WITH query, not a physical table.
   - For COMPLEX_QUERY, use `{source_from_clause}` exactly as the source in the FROM clause.
   - For COMPLEX_QUERY, compare mapped FR_COL values from alias `SRC` with the target columns.
   - For COMPLEX_QUERY, do not rewrite the source query, do not invent joins, and do not look for source columns outside the virtual source query.

{ddl_info_block}

{retry_context}

{complex_source_note}

[Mapping rules]
- Source table: {from_table}
- Target table: {to_table}
- Source from clause: {source_from_clause}
- Source filter condition: {condition}
- Column mappings:
{mapping_info}

[Recommended shape]
SELECT ABS(S.TOT - T.TOT) AS DIFF_TOT,
       ABS(S.C1 - T.C1) AS DIFF_C1,
       ABS(S.C2 - T.C2) AS DIFF_C2
FROM (SELECT COUNT(*) TOT,
             COUNT(source_non_lob_col1) C1,
             COUNT(source_non_lob_col2) C2
      FROM {source_from_clause}
      [optional source filter condition, with exactly one WHERE keyword when needed]) S,
     (SELECT COUNT(*) TOT,
             COUNT(target_non_lob_col1) C1,
             COUNT(target_non_lob_col2) C2
      FROM {to_table}) T

[JSON shape]
{
  "ddl_sql": "",
  "migration_sql": "",
  "verification_sql": "SELECT ..."
}
```
