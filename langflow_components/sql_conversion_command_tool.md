[1. 개발 및 로직 가이드 (절대 수정 X)]
1) SQL Conversion의 경우 따로 id가 없고 SQL_ID, SPACE_NM 컬럼의 조합으로 job을 구분한다.
2) STATUS_CONVERSION 컬럼이 NULL인 경우 SQL Conversion 작업 대상이다.
3) 작업 우선순위 정렬은 PRIORITY 컬럼이 낮은 순으로 한다.
4) TAG_KIND = "SELECT"인 경우에만 BIND_SQL , TEST_SQL 생성으로 이어진다. 아닌 경우에는 TO_SQL 생성 후 바로 PASS_CONVERSION
5) USER_EDITED = Y 인 경우는 TO_SQL 컬럼에 등록된 SQL을 그대로 사용하고 STATUS = "SUCCESS-TOBE"
6) TO_SQL 생성 : TO_SQL_PROMPT 를 랜더링해서 받아온 다음, 성공 STATUS = "SUCCESS-TOBE" , 실패 STATUS = "FAIL-TOBE"
7) BIND_SQL 생성 : BIND_SQL_PROMPT를 받아와서, STATUS = "SUCCESS-TOBE" 이면 이제 BIND_SQL (SELECT ...) 생성 및 실행 : 우선 FR_SQL 기준으로 BIND_SQL 을 생성하는데, FR_SQL은 FR_TABLE에 SOUCRE SCHEMA가 붙어야 SELECT 문이 실행이 될거임.성공 STATUS = "SUCCESS-BIND", 실패 STATUS = "FAIL-BIND", 성공하면 BIND_SET 컬럼에 BIND_SQL 실행 결과 테이블 저장, [{"param1" : "data1", "param2" : "data2" },{"param1" : "data3", "param2" : "data4" }] 형태로 저장됨.
8) BIND_SET 은 FR_TABLE 기준이기 때문에 DATA도 매핑룰에 의해 변환돼서 MIGRATION 됐을수도 있음. 그래서 TEST_SQL을 생성할 때 BIND_SET 에 있는 FR_TABLE, FR_COL 기준 DATA를 TO_TABLE 구조에 맞게 변환해서 파라미터 자리에 대입해야 함. FR_SQL (또는 EDIT_FR_SQL), TO_SQL, 매핑룰, BIND_SET 을 가지고 하나의 TEST_SQL을 만드는 것.
9) TEST_SQL을 실행했을 때, 결과 테이블은 CASE_NO , FROM_COUNT , TO_COUNT 세 개의 컬럼이 나올거고, 모든 CASE_NO의 FROM_COUNT = TO_COUNT 데이터가 같으면 TEST PASS야. 그럼 최종적으로 STATUS_CONVERSION = "PASS-CONVERSION" , STATUS_TUNING = "READY" 을 저장하면 돼. 생성한 SQL들도 마지막에 self._save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._update_job_status(sql_id, space_nm, final_status, elapsed, last_retry_count)
            self._write_log( 로 SQL이랑 STATUS랑 로그 다 저장하면 됨. 

(참고)
원래 기존 소스 코드에서는 SQL_LENGTH와 MAP_TYPE 을 기록하고 SQL_LENGTH = "LONG" 이면 TUNED_FR_SQL을 입력해서 FR_SQL 대신 TUNED_FR_SQL로 TO_SQL을 생성하는 분기가 있었는데, 우선 이번 RUN CONVERSION JOB 에서는 제외하고 필요시 코드 추가
그리고 원래는 RAG 기능으로 FR_SQL 기준으로 참고할만한 TO_SQL을 가져와서 뭐 참고 자료로 넣어주는게 있었는데 우선 RAG 관련 기능도 전부 제외, 기존에 있던 LAST RETRY 전용 프롬포트도 우선 제외.

(메모)
TO_SQL_PROMPT 에 파라미터도 분리해야 하면 분리하도록,, 가이드를 줘야 할듯.

=============================================================================
[RUN CONVERSION JOB 구현 메모]
1) run_sql_conversion_job은 SQL_ID + SPACE_NM으로 job을 조회한다.
2) STATUS_CONVERSION이 NULL인 row만 실행한다. READY는 작업 대상으로 보지 않는다.
3) TO_SQL은 USER_EDITED=Y이면 DB의 TO_SQL을 그대로 사용하고, 아니면 TO_SQL_PROMPT로 생성한다.
4) TAG_KIND가 SELECT가 아니면 TO_SQL 확정 후 바로 PASS-CONVERSION, STATUS_TUNING=READY로 저장한다.
5) TAG_KIND가 SELECT이면 TO_SQL 이후 BIND_SQL_PROMPT로 BIND_SQL을 만들고 실행 결과를 BIND_SET JSON으로 저장 후보에 둔다.
6) TEST_SQL_PROMPT는 FR_SQL 또는 EDIT_FR_SQL, TO_SQL, 매핑룰, BIND_SET을 받아 TEST_SQL을 생성한다.
7) TEST_SQL 실행 결과는 CASE_NO, FROM_COUNT, TO_COUNT 컬럼을 기준으로 검증하고, 모든 row의 FROM_COUNT와 TO_COUNT가 같으면 PASS-CONVERSION이다.
8) 최종 성공/실패 시점에만 NEXT_SQL_INFO의 TO_SQL, BIND_SQL, BIND_SET, TEST_SQL, STATUS_CONVERSION, STATUS_TUNING, RETRY_COUNT, BATCH_CNT를 저장한다.
9) elapsed_seconds는 NEXT_SQL_INFO에 저장하지 않고 NEXT_SQL_LOG.ELAPSED_SECONDS에 기록한다.
10) 이번 구현에서는 SQL_LENGTH=LONG 분기, TUNED_FR_SQL 기반 생성, RAG 참고 SQL, LAST RETRY 전용 프롬포트는 제외한다.
11) generate_to_sql, generate_bind_sql, generate_test_sql는 채팅 반환 전용 action이다. 이 action들은 NEXT_SQL_INFO를 update하지 않는다.
12) preview_to_sql_prompt, preview_bind_sql_prompt, preview_test_sql_prompt는 LLM을 호출하지 않고 prompt 렌더링 결과만 반환한다.
13) run_sql_conversion_job만 최종 성공/실패 시점에 TO_SQL, BIND_SQL, BIND_SET, TEST_SQL과 STATUS_CONVERSION/STATUS_TUNING을 저장한다.
