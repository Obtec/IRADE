"""Platform's own spec tables (DOC.md §3.3) as a hydratable snapshot."""


def _req(i, dom, title, content, ac, st, ph, typ="FUNCTIONAL", pr="P0"):
    return {"req_id": i, "domain": dom, "req_type": typ, "title": title, "requirement_content": content,
            "acceptance_criteria": ac, "adoption_status": st, "target_phase": ph, "priority": pr}


def _arc(i, layer, name, role, tech, contract, st, ph):
    return {"arch_id": i, "layer": layer, "component_name": name, "role_content": role, "tech_stack": tech,
            "interface_contract": contract, "adoption_status": st, "target_phase": ph}


def _sch(i, eng, name, role, ddl, st, ph):
    return {"schema_id": i, "storage_engine": eng, "table_or_key_name": name, "role_content": role,
            "columns_and_ddl_json": ddl, "adoption_status": st, "target_phase": ph}


def _t(s, sid, t, tid, rel):
    return {"source_type": s, "source_id": sid, "target_type": t, "target_id": tid, "relation_type": rel}


PLATFORM_SNAPSHOT = {
    "srs": [
        _req("REQ-001", "GRAPH_ENGINE", "JSON 그래프 실행 및 노드 리셋",
             "노드·엣지 정의를 JSON으로 저장하고 노드 진입 시 이전 대화 이력을 초기화해 Handoff JSON만 전달한다.",
             "노드 전환 시 입력 컨텍스트가 직전 Handoff+고정 Prefix로만 구성됨", "ACCEPT_NOW", "MVP"),
        _req("REQ-002", "TOKEN_OPT", "심볼 색인 기반 부분 읽기",
             "파일 전체 Read를 금지하고 Tree-sitter SQLite 인덱스로 대상 함수 라인 범위만 부분 읽기한다.",
             "파일 읽기 토큰 80% 이상 절감, 150~350줄 모듈 기준 탐색 턴 ≤3회", "ACCEPT_NOW", "MVP",
             "NON_FUNCTIONAL"),
        _req("REQ-003", "CODE_VERIFY", "함수·메서드 단위 의미 검증",
             "ast_hash가 변경된 함수·메서드만 저렴한 모델로 선언형 명세로 변환해 계약과 대조한다.",
             "미변경 심볼 LLM 호출 0건, 계약 불일치 시 해당 심볼만 Dev 환류", "ACCEPT_NOW", "MVP"),
        _req("REQ-004", "UX", "인터랙티브 UX 직접 구성",
             "대시보드 라이브 프리뷰에서 사용자가 레이아웃·토큰·노출 조건을 직접 조절하고 잠금한다.",
             "locked_by_user=true 항목에 대한 에이전트 덮어쓰기 100% 차단", "DEFER", "PHASE_2", pr="P1"),
        _req("REQ-005", "RESEARCH", "교착 시 즉각 논문·문서 검색",
             "Watchdog 교착 판정 또는 사람 요청 시 논문·공식 문서를 검색해 1,000토큰 이내 Insight Brief를 생성한다.",
             "외부 쿼리에서 사내 코드·시크릿 유출 0건, 브리프 출처 링크 100% 포함", "DEFER", "PHASE_2", pr="P1"),
        _req("REQ-006", "ANALYTICS", "장기 토큰·품질 OLAP 분석",
             "수개월치 실행 이력을 Parquet/ClickHouse로 이관해 모델별 1차 통과율과 비용 추이를 집계한다.",
             "운영 Primary RDB I/O 간섭 0%, 90일 집계 쿼리 2초 이내 응답", "DEFER", "PHASE_3", pr="P2"),
    ],
    "arc": [
        _arc("ARC-001", "IN_MEMORY_SPEC", "In-Memory Spec Engine",
             "ACCEPT_NOW 명세만 메모리에 올려 SQL Narrowing 제공, 시작 전 SQL+저렴 모델 모순 검사 집행",
             "SQLite :memory: (FK+FTS5)", "SQL / JSON", "ACCEPT_NOW", "MVP"),
        _arc("ARC-002", "CONTROL_PLANE", "Graph Orchestrator",
             "SKIP LOCKED 작업 선점, 낙관적 락 상태 전이, Fast-Track 우회(≤30줄), HITL 게이트 제어",
             "Python + PostgreSQL", "JSONB", "ACCEPT_NOW", "MVP"),
        _arc("ARC-003", "WORKER", "Semantic Lifting Verifier",
             "변경 심볼만 추출해 저렴한 모델(thinking=none)로 선언형 계약 변환·대조",
             "AST/Tree-sitter + SQLite + 저가 모델", "JSON", "ACCEPT_NOW", "MVP"),
        _arc("ARC-004", "CLIENT", "Interactive UX Workbench",
             "라이브 컴포넌트 프리뷰 조작 결과를 선언형 JSON으로 저장하고 프론트엔드 코드에 바인딩",
             "React Canvas + ux_overrides", "JSON", "DEFER", "PHASE_2"),
        _arc("ARC-005", "CONTROL_PLANE", "On-Demand Research Engine",
             "문제 시그니처 익명화 후 arXiv·Semantic Scholar·공식 문서 병렬 검색, FTS5 발췌 합성",
             "Search APIs + SQLite FTS5", "Markdown", "DEFER", "PHASE_2"),
    ],
    "sch": [
        _sch("SCH-001", "POSTGRES_RDS", "workflow_runs, job_queue, hitl_decisions",
             "Hot State·작업 큐·승인 게이트", {"fillfactor": [80, 70], "partial_index": "status='PENDING'"},
             "ACCEPT_NOW", "MVP"),
        _sch("SCH-002", "POSTGRES_RDS", "graph_definitions, workspace_budgets, semantic_contracts",
             "그래프 JSONB 버전 관리, 예산 게이트, 심볼 계약", {"jsonb": True}, "ACCEPT_NOW", "MVP"),
        _sch("SCH-003", "SQLITE_WORKER", "tool_outputs_fts, code_symbols, semantic_projections, file_read_watermarks",
             "도구 출력 오프로딩, 심볼 색인, 의미 변환 캐시, 워터마크", {"fts5": True}, "ACCEPT_NOW", "MVP"),
        _sch("SCH-004", "POSTGRES_RDS", "ux_overrides, research_sessions",
             "UX 잠금 및 리서치 pgvector 저장소", {"pgvector": True}, "DEFER", "PHASE_2"),
    ],
    "trace": [
        _t("SRS", "REQ-001", "ARC", "ARC-002", "IMPLEMENTS"),
        _t("SRS", "REQ-001", "SCH", "SCH-002", "PERSISTS_IN"),
        _t("SRS", "REQ-002", "ARC", "ARC-003", "IMPLEMENTS"),
        _t("SRS", "REQ-002", "SCH", "SCH-003", "PERSISTS_IN"),
        _t("SRS", "REQ-003", "ARC", "ARC-003", "IMPLEMENTS"),
        _t("SRS", "REQ-003", "SCH", "SCH-002", "PERSISTS_IN"),
        _t("SRS", "REQ-004", "ARC", "ARC-004", "IMPLEMENTS"),
        _t("SRS", "REQ-004", "SCH", "SCH-004", "PERSISTS_IN"),
        _t("SRS", "REQ-005", "ARC", "ARC-005", "IMPLEMENTS"),
        _t("SRS", "REQ-005", "SCH", "SCH-004", "PERSISTS_IN"),
        _t("ARC", "ARC-001", "SCH", "SCH-003", "DEPENDS_ON"),
    ],
}
