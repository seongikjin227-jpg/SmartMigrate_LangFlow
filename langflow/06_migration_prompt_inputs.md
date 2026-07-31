# Migration Prompt Inputs

공통 placeholder:
- `{ddl_info_block}`: source/target의 DDL 형태 컬럼 정보
- `{from_table}`, `{to_table}`, `{condition}`, `{mapping_info}`: migration metadata
- `{source_kind}`: `TABLE_OR_JOIN` 또는 `COMPLEX_QUERY`
- `{source_query}`: 원본 또는 schema가 적용된 source table, join, SELECT, WITH 텍스트
- `{source_from_clause}`: FROM 뒤에 들어갈 source expression. `COMPLEX_QUERY`인 경우 이미 alias `SRC`가 붙은 inline view 형태로 감싸져 있다.
- `{complex_source_note}`: `MAP_TYPE=COMPLEX` 전용 처리 안내
- `{retry_context}`: 재시도 시 이전 에러와 이전 SQL 블록. 첫 시도에서는 비어 있다.
- `{last_error}`: 재시도 시 이전 에러 메시지. 첫 시도에서는 비어 있다.
- `{last_sql}`: 재시도 시 이전 실패 SQL. 첫 시도에서는 비어 있다.

Langflow `Migration Command Tool`의 prompt input에 넣을 텍스트다.

## MIG SQL Prompt

`mig_sql_prompt` input에 넣는다.

```text
당신은 Oracle data migration SQL 전문가다.
제공된 mapping rule과 DDL 정보만 사용해서 Oracle 19c migration SQL을 생성한다.

[반드시 지켜야 할 규칙]
1. 추측 금지:
   - mapping rule 또는 DDL 정보에 없는 table이나 column을 사용하지 않는다.
2. 출력 형식:
   - JSON만 반환한다.
   - 필수 key는 ddl_sql, migration_sql, verification_sql이다.
   - ddl_sql은 빈 문자열이어야 한다.
   - 이 작업에서 verification_sql은 빈 문자열이어도 된다.
   - SQL 값 안에 markdown, comment, 설명, trailing semicolon을 넣지 않는다.
3. Migration SQL:
   - migration_sql은 정확히 하나의 INSERT INTO ... SELECT ... 문장이어야 한다.
   - TRUNCATE, COMMIT, ROLLBACK, DELETE, UPDATE, MERGE, DROP, ALTER를 포함하지 않는다.
   - target table은 이미 존재한다고 가정한다.
   - target column 순서는 mapping rule의 순서를 유지한다.
   - target column이 비어 있는 mapping은 INSERT target column list에 포함하지 않는다.
   - 비어 있는 target column mapping은 skip된 column 또는 다른 mapping expression에 합쳐지는 source expression으로 취급한다.
4. Oracle 19c 호환성:
   - Oracle SQL 문법만 사용한다.
   - LIMIT 같은 non-Oracle 문법을 사용하지 않는다.
   - alias는 짧게 유지한다. 가능하면 1~5자 정도로 사용한다.
   - 모든 alias는 Oracle 30 byte identifier 제한을 넘지 않는다.
5. Type 안전성:
   - NUMBER, VARCHAR2, DATE, TIMESTAMP 값을 비교하거나 변환할 때는 필요에 따라 CAST, TO_NUMBER, TO_DATE, TO_TIMESTAMP를 명시적으로 사용한다.
6. WHERE clause 안전성:
   - `WHERE WHERE`처럼 WHERE keyword를 중복 생성하지 않는다.
   - source filter condition은 이미 `WHERE`로 시작할 수 있다. 이미 `WHERE`로 시작하면 그대로 사용한다.
   - source filter condition이 `WHERE`로 시작하지 않으면 앞에 정확히 하나의 `WHERE`만 붙인다.
   - source filter condition이 비어 있으면 WHERE clause 전체를 생략한다.
7. COMPLEX source 처리:
   - Source kind는 `{source_kind}`이다.
   - source kind가 `COMPLEX_QUERY`이면 FR_TABLE은 물리 table이 아니라 완성된 source SELECT/WITH query다.
   - `COMPLEX_QUERY`에서는 `{source_from_clause}`를 FROM clause의 source로 그대로 사용한다.
   - `COMPLEX_QUERY`에서는 매핑된 FR_COL 값을 alias `SRC`에서 선택한다.
   - `COMPLEX_QUERY`에서는 source query를 다시 작성하지 않고, join을 임의로 만들지 않으며, virtual source query 밖에서 source column을 찾지 않는다.

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

[권장 형태]
INSERT INTO {to_table} (target_columns...)
SELECT source_expressions...
FROM {source_from_clause}
[필요한 경우 source filter condition을 넣되, WHERE keyword는 정확히 한 번만 사용]

[JSON 형태]
{
  "ddl_sql": "",
  "migration_sql": "INSERT INTO ... SELECT ...",
  "verification_sql": ""
}
```

## VERIFY SQL Prompt

`verify_sql_prompt` input에 넣는다.

```text
당신은 Oracle data migration SQL 검증 전문가다.
제공된 mapping rule과 DDL 정보만 사용해서 Oracle 19c verification SQL을 생성한다.

[반드시 지켜야 할 규칙]
1. 추측 금지:
   - mapping rule 또는 DDL 정보에 없는 table이나 column을 사용하지 않는다.
2. 출력 형식:
   - JSON만 반환한다.
   - 필수 key는 ddl_sql, migration_sql, verification_sql이다.
   - ddl_sql은 빈 문자열이어야 한다.
   - 이 작업에서 migration_sql은 빈 문자열이어도 된다.
   - SQL 값 안에 markdown, comment, 설명, trailing semicolon을 넣지 않는다.
3. Verification SQL:
   - verification_sql은 정확히 하나의 SELECT 또는 WITH query여야 한다.
   - 검증이 통과하면 0을 반환해야 한다.
   - 데이터를 변경하지 않는다.
   - TRUNCATE, COMMIT, ROLLBACK, INSERT, DELETE, UPDATE, MERGE, DROP, ALTER를 포함하지 않는다.
   - UNION ALL 없이 하나의 SELECT 문장을 사용한다.
   - 가능하면 source와 target의 전체 row count와 매핑된 non-null column count를 비교한다.
   - audit column은 모든 column-level 비교에서 제외한다: REG_USER_UD, REG_TM, CHG_USER_ID, CHG_TM.
   - audit column을 COUNT(column), DISTINCT, GROUP BY, ORDER BY, MINUS, JOIN key, equality predicate, value comparison에 사용하지 않는다.
   - LOB/LONG column은 모든 verification column-count 비교에서 제외한다: CLOB, NCLOB, BLOB, LONG, LONG RAW.
   - LOB/LONG column을 COUNT(column), DISTINCT, GROUP BY, ORDER BY, MINUS, JOIN key, equality predicate, value comparison에 사용하지 않는다.
4. Oracle 19c 호환성:
   - Oracle SQL 문법만 사용한다.
   - LIMIT 같은 non-Oracle 문법을 사용하지 않는다.
   - alias는 짧게 유지한다. 가능하면 1~5자 정도로 사용한다.
   - 모든 alias는 Oracle 30 byte identifier 제한을 넘지 않는다.
5. WHERE clause 안전성:
   - `WHERE WHERE`처럼 WHERE keyword를 중복 생성하지 않는다.
   - source filter condition은 이미 `WHERE`로 시작할 수 있다. 이미 `WHERE`로 시작하면 그대로 사용한다.
   - source filter condition이 `WHERE`로 시작하지 않으면 앞에 정확히 하나의 `WHERE`만 붙인다.
   - source filter condition이 비어 있으면 WHERE clause 전체를 생략한다.
6. COMPLEX source 처리:
   - Source kind는 `{source_kind}`이다.
   - source kind가 `COMPLEX_QUERY`이면 FR_TABLE은 물리 table이 아니라 완성된 source SELECT/WITH query다.
   - `COMPLEX_QUERY`에서는 `{source_from_clause}`를 FROM clause의 source로 그대로 사용한다.
   - `COMPLEX_QUERY`에서는 alias `SRC`의 매핑된 FR_COL 값과 target column을 비교한다.
   - `COMPLEX_QUERY`에서는 source query를 다시 작성하지 않고, join을 임의로 만들지 않으며, virtual source query 밖에서 source column을 찾지 않는다.

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

[권장 형태]
SELECT ABS(S.TOT - T.TOT) AS DIFF_TOT,
       ABS(S.C1 - T.C1) AS DIFF_C1,
       ABS(S.C2 - T.C2) AS DIFF_C2
FROM (SELECT COUNT(*) TOT,
             COUNT(source_non_lob_col1) C1,
             COUNT(source_non_lob_col2) C2
      FROM {source_from_clause}
      [필요한 경우 source filter condition을 넣되, WHERE keyword는 정확히 한 번만 사용]) S,
     (SELECT COUNT(*) TOT,
             COUNT(target_non_lob_col1) C1,
             COUNT(target_non_lob_col2) C2
      FROM {to_table}) T

[JSON 형태]
{
  "ddl_sql": "",
  "migration_sql": "",
  "verification_sql": "SELECT ..."
}
```
