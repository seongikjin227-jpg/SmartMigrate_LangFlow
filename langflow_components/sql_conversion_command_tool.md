[1. 개발 및 로직 가이드 (절대 수정 X)]
1) SQL Conversion의 경우 따로 id가 없고 SQL_ID, SPACE_NM 컬럼의 조합으로 job을 구분한다.
2) STATUS_CONVERSION 컬럼이 NULL인 경우 SQL Conversion 작업 대상이다.
3) 작업 우선순위 정렬은 PRIORITY 컬럼이 낮은 순으로 한다.
4) TAG_KIND = "SELECT"인 경우에만 BIND_SQL , TEST_SQL 생성으로 이어진다. 아닌 경우에는 TO_SQL 생성 후 바로 PASS_CONVERSION
5) 