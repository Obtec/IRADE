export type ProverEngine = 'LEAN4' | 'ROCQ';

export interface TheoremRow {
  theoremId: number;
  workspaceId: number | null;
  workspaceKey: string;
  proverEngine: ProverEngine | string;
  theoremName: string;
  statementKind: string;
  statementText: string;
  proofCode: string;
  verificationStatus: string;
  verifierOutput: string;
  sourceCellId: string | null;
  verifiedAt: string;
  updatedAt: string;
}

export interface DefinitionRow {
  definitionId: number;
  workspaceId: number | null;
  workspaceKey: string;
  proverEngine: ProverEngine | string;
  symbolName: string;
  definitionKind: string;
  signatureText: string;
  sourceCode: string;
  sourceCellId: string | null;
  updatedAt: string;
}

export interface LemmaRelationRow {
  relationId: number;
  theoremId: number;
  theoremName: string;
  theoremKind: string;
  lemmaTheoremId: number | null;
  lemmaName: string;
  relationType: string;
  referenceContext: string | null;
  lemmaVerifiedInDb: boolean;
  createdAt: string;
}

export interface DefinitionRelationRow {
  relationId: number;
  theoremId: number;
  theoremName: string;
  definitionId: number;
  definitionName: string;
  definitionKind: string;
  usageRole: string;
  referenceContext: string | null;
  createdAt: string;
}

export interface ProofKnowledgeSnapshot {
  workspaceKey: string;
  theorems: TheoremRow[];
  definitions: DefinitionRow[];
  lemmaRelations: LemmaRelationRow[];
  definitionRelations: DefinitionRelationRow[];
}

export interface LemmaRelationInput {
  theoremName: string;
  lemmaName: string;
  relationType: 'USES_LEMMA' | 'DEPENDS_ON' | 'REWRITES_WITH' | 'APPLIES';
  referenceContext?: string;
}

export interface DefinitionUsageInput {
  theoremName: string;
  symbolName: string;
  usageRole: 'IN_STATEMENT' | 'IN_PROOF' | 'BOTH' | 'REFERENCED';
  referenceContext?: string;
}

export interface VerifiedProofSyncPayload {
  workspaceId?: number;
  workspaceKey: string;
  proverEngine: ProverEngine;
  verificationStatus: 'VERIFIED';
  cellId?: string;
  sourceCode: string;
  verifierOutput: string;
  lemmaRelations: LemmaRelationInput[];
  definitionUsages: DefinitionUsageInput[];
}

export interface ClientParsedProofRelations {
  definitions: { name: string; kind: string; signature: string }[];
  theorems: { name: string; kind: string; statement: string; body: string }[];
  lemmaRelations: LemmaRelationInput[];
  definitionUsages: DefinitionUsageInput[];
}

const escapeHtml = (value: string): string =>
  value.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]!);

const LEAN_DECL_RE = /^([ \t]*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*)(def|abbrev|inductive|structure|class|axiom|opaque|theorem|lemma|corollary|proposition)\s+([A-Za-z_][A-Za-z0-9_.']*)/gm;
const ROCQ_DECL_RE = /^[ \t]*(?:Local\s+|Global\s+|Program\s+)?(Definition|Fixpoint|CoFixpoint|Inductive|CoInductive|Record|Class|Axiom|Parameter|Theorem|Lemma|Corollary|Proposition|Remark|Fact)\s+([A-Za-z_][A-Za-z0-9_']*)/gm;
const DEF_KEYWORDS = new Set([
  'def', 'abbrev', 'inductive', 'structure', 'class', 'axiom', 'opaque',
  'Definition', 'Fixpoint', 'CoFixpoint', 'Inductive', 'CoInductive', 'Record', 'Class', 'Axiom', 'Parameter',
]);

export function normalizeLeanCodeForExecution(code: string): string {
  return code.replace(
    /^(\s*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*)lemma(?=\s)/gm,
    '$1theorem',
  );
}

function stripLeanComments(source: string): string {
  let out = '';
  let blockDepth = 0;
  let lineComment = false;
  for (let i = 0; i < source.length; i += 1) {
    const c = source[i];
    const next = source[i + 1] ?? '';
    if (lineComment) {
      if (c === '\n') {
        lineComment = false;
        out += '\n';
      } else {
        out += ' ';
      }
      continue;
    }
    if (blockDepth > 0) {
      if (c === '/' && next === '-') {
        blockDepth += 1;
        out += '  ';
        i += 1;
      } else if (c === '-' && next === '/') {
        blockDepth -= 1;
        out += '  ';
        i += 1;
      } else {
        out += c === '\n' ? '\n' : ' ';
      }
      continue;
    }
    if (c === '-' && next === '-') {
      lineComment = true;
      out += '  ';
      i += 1;
    } else if (c === '/' && next === '-') {
      blockDepth = 1;
      out += '  ';
      i += 1;
    } else {
      out += c;
    }
  }
  return out;
}

function stripRocqComments(source: string): string {
  let out = '';
  let depth = 0;
  for (let i = 0; i < source.length; i += 1) {
    const c = source[i];
    const next = source[i + 1] ?? '';
    if (depth > 0) {
      if (c === '(' && next === '*') {
        depth += 1;
        out += '  ';
        i += 1;
      } else if (c === '*' && next === ')') {
        depth -= 1;
        out += '  ';
        i += 1;
      } else {
        out += c === '\n' ? '\n' : ' ';
      }
      continue;
    }
    if (c === '(' && next === '*') {
      depth = 1;
      out += '  ';
      i += 1;
    } else {
      out += c;
    }
  }
  return out;
}

function escapeRegExp(literal: string): string {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function hasIdentifier(text: string, ident: string): boolean {
  if (!text || !ident) return false;
  const re = new RegExp(`(?<![A-Za-z0-9_'])${escapeRegExp(ident)}(?![A-Za-z0-9_'])`);
  return re.test(text);
}

function findReferenceLine(text: string, ident: string): string | undefined {
  for (const line of text.split(/\r?\n/)) {
    if (hasIdentifier(line, ident)) {
      return line.trim().slice(0, 240);
    }
  }
  return undefined;
}

function findLeanAssignIndex(block: string): number {
  let paren = 0;
  let brace = 0;
  let bracket = 0;
  let inString = false;
  for (let i = 0; i < block.length - 1; i += 1) {
    const c = block[i];
    if (c === '"' && (i === 0 || block[i - 1] !== '\\')) {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (c === '(') paren += 1;
    else if (c === ')' && paren > 0) paren -= 1;
    else if (c === '{') brace += 1;
    else if (c === '}' && brace > 0) brace -= 1;
    else if (c === '[') bracket += 1;
    else if (c === ']' && bracket > 0) bracket -= 1;
    else if (c === ':' && block[i + 1] === '=' && paren === 0 && brace === 0 && bracket === 0) {
      return i;
    }
  }
  return -1;
}

function findRocqSentenceEnd(block: string): number {
  let inString = false;
  for (let i = 0; i < block.length; i += 1) {
    const c = block[i];
    if (c === '"' && (i === 0 || block[i - 1] !== '\\')) {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (c === '.' && (i + 1 === block.length || /\s/.test(block[i + 1]))) {
      return i;
    }
  }
  return -1;
}

function classifyLemmaRelationType(text: string, lemmaName: string): LemmaRelationInput['relationType'] {
  const q = escapeRegExp(lemmaName);
  if (new RegExp(`\\b(?:rw|simp|simpa|rewrite|autorewrite)\\b[^.\\n;]{0,80}(?<![A-Za-z0-9_'])${q}(?![A-Za-z0-9_'])`).test(text)) {
    return 'REWRITES_WITH';
  }
  if (new RegExp(`\\b(?:apply|exact|eapply|refine|using)\\b[^.\\n;]{0,80}(?<![A-Za-z0-9_'])${q}(?![A-Za-z0-9_'])`).test(text)) {
    return 'APPLIES';
  }
  return 'USES_LEMMA';
}

export function extractClientProofRelations(proverEngine: ProverEngine, sourceCode: string): ClientParsedProofRelations {
  const stripped = proverEngine === 'ROCQ' ? stripRocqComments(sourceCode) : stripLeanComments(sourceCode);
  const re = proverEngine === 'ROCQ' ? new RegExp(ROCQ_DECL_RE.source, 'gm') : new RegExp(LEAN_DECL_RE.source, 'gm');

  const spans: { keyword: string; name: string; start: number }[] = [];
  let match: RegExpExecArray | null;
  while ((match = re.exec(stripped)) !== null) {
    const keyword = proverEngine === 'ROCQ' ? match[1] : match[2];
    const name = proverEngine === 'ROCQ' ? match[2] : match[3];
    spans.push({ keyword, name, start: match.index });
  }

  const definitions: ClientParsedProofRelations['definitions'] = [];
  const theorems: ClientParsedProofRelations['theorems'] = [];

  for (let i = 0; i < spans.length; i += 1) {
    const cur = spans[i];
    const nextStart = i + 1 < spans.length ? spans[i + 1].start : stripped.length;
    const rawBlock = stripped.slice(cur.start, nextStart).trim();
    if (!rawBlock) continue;

    if (DEF_KEYWORDS.has(cur.keyword)) {
      let sig = rawBlock.split(/\r?\n/)[0].trim();
      if (proverEngine === 'ROCQ') {
        const dot = findRocqSentenceEnd(rawBlock);
        const rawSig = dot > 0 ? rawBlock.slice(0, dot) : sig;
        const eq = findLeanAssignIndex(rawSig);
        sig = (eq > 0 ? rawSig.slice(0, eq) : rawSig).replace(/\s+/g, ' ').trim();
      } else {
        const eq = findLeanAssignIndex(rawBlock);
        if (eq > 0) sig = rawBlock.slice(0, eq).replace(/\s+/g, ' ').trim();
      }
      definitions.push({ name: cur.name, kind: cur.keyword, signature: sig });
    } else {
      let statement = rawBlock.split(/\r?\n/)[0].trim();
      let body = rawBlock;
      if (proverEngine === 'ROCQ') {
        const dot = findRocqSentenceEnd(rawBlock);
        if (dot > 0) {
          statement = rawBlock.slice(0, dot).replace(/\s+/g, ' ').trim();
          body = rawBlock.slice(dot + 1).trim();
        }
      } else {
        const eq = findLeanAssignIndex(rawBlock);
        if (eq > 0) {
          statement = rawBlock.slice(0, eq).replace(/\s+/g, ' ').trim();
          body = rawBlock.slice(eq + 2).trim();
        }
      }
      theorems.push({ name: cur.name, kind: cur.keyword.toUpperCase(), statement, body });
    }
  }

  const lemmaRelations: LemmaRelationInput[] = [];
  const definitionUsages: DefinitionUsageInput[] = [];

  for (const thm of theorems) {
    const combined = `${thm.statement}\n${thm.body}`;
    for (const def of definitions) {
      const inStmt = hasIdentifier(thm.statement, def.name);
      const inBody = hasIdentifier(thm.body, def.name);
      if (inStmt || inBody) {
        definitionUsages.push({
          theoremName: thm.name,
          symbolName: def.name,
          usageRole: inStmt && inBody ? 'BOTH' : inStmt ? 'IN_STATEMENT' : 'IN_PROOF',
          referenceContext: findReferenceLine(combined, def.name),
        });
      }
    }

    for (const candidate of theorems) {
      if (candidate.name === thm.name) continue;
      if (!hasIdentifier(combined, candidate.name)) continue;
      lemmaRelations.push({
        theoremName: thm.name,
        lemmaName: candidate.name,
        relationType: classifyLemmaRelationType(combined, candidate.name),
        referenceContext: findReferenceLine(combined, candidate.name),
      });
    }
  }

  return { definitions, theorems, lemmaRelations, definitionUsages };
}

function authHeaders(token?: string, json = false): Record<string, string> {
  const headers: Record<string, string> = {};
  if (json) headers['Content-Type'] = 'application/json';
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

export async function fetchProofKnowledgeSnapshot(workspaceKey?: string, token?: string): Promise<ProofKnowledgeSnapshot> {
  const query = workspaceKey ? `?workspaceKey=${encodeURIComponent(workspaceKey)}` : '';
  const response = await fetch(`/api/irade/proofs${query}`, {
    headers: authHeaders(token),
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    throw new Error(`정리 DB 조회 실패 (${response.status})`);
  }
  return (await response.json()) as ProofKnowledgeSnapshot;
}

export async function syncVerifiedProofToDb(payload: VerifiedProofSyncPayload, token?: string): Promise<ProofKnowledgeSnapshot> {
  const response = await fetch('/api/irade/proofs/verified', {
    method: 'POST',
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(12000),
  });
  if (!response.ok) {
    let msg = `정리 DB 저장 실패 (${response.status})`;
    try {
      const err = (await response.json()) as { message?: string };
      if (err.message) msg = err.message;
    } catch {
      // ignore non-json error body
    }
    throw new Error(msg);
  }
  return (await response.json()) as ProofKnowledgeSnapshot;
}

export async function deleteVerifiedTheoremFromDb(theoremId: number, token?: string): Promise<void> {
  const response = await fetch(`/api/irade/proofs/theorems/${theoremId}`, {
    method: 'DELETE',
    headers: authHeaders(token),
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    throw new Error(`정리 삭제 실패 (${response.status})`);
  }
}

function formatShortTime(iso: string): string {
  if (!iso) return '-';
  const dt = new Date(iso.endsWith('Z') ? iso : `${iso}Z`);
  if (Number.isNaN(dt.getTime())) return iso.slice(0, 16).replace('T', ' ');
  return dt.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function usageRoleLabel(role: string): string {
  switch (role) {
    case 'BOTH':
      return '명제+증명';
    case 'IN_STATEMENT':
      return '명제 내 사용';
    case 'IN_PROOF':
      return '증명 내 사용';
    default:
      return role;
  }
}

function relationTypeLabel(type: string): string {
  switch (type) {
    case 'REWRITES_WITH':
      return 'REWRITES_WITH';
    case 'APPLIES':
      return 'APPLIES';
    case 'DEPENDS_ON':
      return 'DEPENDS_ON';
    default:
      return 'USES_LEMMA';
  }
}

function buildTablesMarkup(snapshot: ProofKnowledgeSnapshot, compact: boolean): string {
  const theoremsRows = snapshot.theorems.length
    ? snapshot.theorems
        .map(
          t => `<tr>
            <td>
              <div class="proof-db-symbol">${escapeHtml(t.theoremName)}</div>
              <span class="proof-db-badge kind">${escapeHtml(t.statementKind)}</span>
              <span class="proof-db-badge engine">${escapeHtml(t.proverEngine)}</span>
            </td>
            <td><code class="proof-db-code">${escapeHtml(t.statementText)}</code></td>
            <td><span class="proof-db-badge verified">✓ ${escapeHtml(t.verificationStatus)}</span>${compact ? '' : `<small class="proof-db-time">${escapeHtml(formatShortTime(t.verifiedAt))}</small>`}</td>
            <td class="proof-db-actions-col"><button type="button" class="text-button proof-db-del-btn" data-delete-theorem-id="${t.theoremId}" title="정리 삭제">삭제</button></td>
          </tr>`,
        )
        .join('')
    : `<tr><td colspan="4" class="proof-db-empty">검증 완료된 정리(Lean 4 · Rocq)를 실행하면 자동 저장됩니다.</td></tr>`;

  const lemmaRows = snapshot.lemmaRelations.length
    ? snapshot.lemmaRelations
        .map(
          r => `<tr>
            <td><strong>${escapeHtml(r.theoremName)}</strong></td>
            <td>
              <code>${escapeHtml(r.lemmaName)}</code>
              ${r.lemmaVerifiedInDb ? '<span class="proof-db-badge linked">DB FK 연결됨</span>' : '<span class="proof-db-badge external">외부/미등록 참조</span>'}
            </td>
            <td><span class="proof-db-badge rel">${escapeHtml(relationTypeLabel(r.relationType))}</span></td>
            ${compact ? '' : `<td><code class="proof-db-code">${escapeHtml(r.referenceContext || '-')}</code></td>`}
          </tr>`,
        )
        .join('')
    : `<tr><td colspan="${compact ? 3 : 4}" class="proof-db-empty">기록된 정리 ↔ 보조 정리 관계가 아직 없습니다.</td></tr>`;

  const defById = new Map(snapshot.definitions.map(d => [d.definitionId, d]));
  const defRelationRows = compact
    ? (snapshot.definitionRelations.length
        ? snapshot.definitionRelations
            .map(r => {
              const def = defById.get(r.definitionId);
              return `<tr>
                <td>
                  <div class="proof-db-symbol">${escapeHtml(r.definitionName)}</div>
                  <span class="proof-db-badge kind">${escapeHtml(r.definitionKind)}</span>
                </td>
                <td><code class="proof-db-code">${escapeHtml(def?.signatureText || '-')}</code></td>
                <td><strong>${escapeHtml(r.theoremName)}</strong></td>
                <td><span class="proof-db-badge role">${escapeHtml(usageRoleLabel(r.usageRole))}</span></td>
              </tr>`;
            })
            .join('')
        : snapshot.definitions.length
          ? snapshot.definitions
              .map(
                d => `<tr>
                  <td>
                    <div class="proof-db-symbol">${escapeHtml(d.symbolName)}</div>
                    <span class="proof-db-badge kind">${escapeHtml(d.definitionKind)}</span>
                  </td>
                  <td><code class="proof-db-code">${escapeHtml(d.signatureText)}</code></td>
                  <td>-</td>
                  <td><span class="proof-db-badge external">정의됨</span></td>
                </tr>`,
              )
              .join('')
          : `<tr><td colspan="4" class="proof-db-empty">사용된 정의가 아직 기록되지 않았습니다.</td></tr>`)
    : snapshot.definitions.length
      ? snapshot.definitions
          .map(
            d => `<tr>
              <td><div class="proof-db-symbol">${escapeHtml(d.symbolName)}</div><span class="proof-db-badge kind">${escapeHtml(d.definitionKind)}</span></td>
              <td><span class="proof-db-badge engine">${escapeHtml(d.proverEngine)}</span></td>
              <td><code class="proof-db-code">${escapeHtml(d.signatureText)}</code></td>
              <td><small class="proof-db-time">${escapeHtml(formatShortTime(d.updatedAt))}</small></td>
            </tr>`,
          )
          .join('')
      : `<tr><td colspan="4" class="proof-db-empty">저장된 정의가 없습니다.</td></tr>`;

  const defRelFullRows = snapshot.definitionRelations.length
    ? snapshot.definitionRelations
        .map(
          r => `<tr>
            <td><strong>${escapeHtml(r.theoremName)}</strong></td>
            <td><code>${escapeHtml(r.definitionName)}</code> <span class="proof-db-badge kind">${escapeHtml(r.definitionKind)}</span></td>
            <td><span class="proof-db-badge role">${escapeHtml(usageRoleLabel(r.usageRole))}</span></td>
            <td><code class="proof-db-code">${escapeHtml(r.referenceContext || '-')}</code></td>
          </tr>`,
        )
        .join('')
    : `<tr><td colspan="4" class="proof-db-empty">기록된 정리-정의 사용 관계가 없습니다.</td></tr>`;

  return `
    <section class="proof-db-section">
      <div class="proof-db-section-head">
        <h4>1. 증명 완료 정리 테이블</h4>
        <span class="proof-db-table-tag">dbo.proof_theorems (${snapshot.theorems.length})</span>
      </div>
      <div class="proof-db-table-wrap">
        <table class="proof-db-table">
          <thead><tr><th>정리명</th><th>명제 (Statement)</th><th>상태</th><th>관리</th></tr></thead>
          <tbody>${theoremsRows}</tbody>
        </table>
      </div>
    </section>

    <section class="proof-db-section">
      <div class="proof-db-section-head">
        <h4>2. 보조 정리 관계 테이블</h4>
        <span class="proof-db-table-tag">dbo.proof_theorem_lemma_relations (${snapshot.lemmaRelations.length})</span>
      </div>
      <div class="proof-db-table-wrap">
        <table class="proof-db-table">
          <thead><tr><th>주 정리</th><th>보조 정리 (Lemma)</th><th>관계</th>${compact ? '' : '<th>참조 컨텍스트</th>'}</tr></thead>
          <tbody>${lemmaRows}</tbody>
        </table>
      </div>
    </section>

    ${
      compact
        ? `<section class="proof-db-section">
            <div class="proof-db-section-head">
              <h4>3. 사용된 정의 테이블</h4>
              <span class="proof-db-table-tag">dbo.proof_definitions ⋈ relations (${snapshot.definitions.length})</span>
            </div>
            <div class="proof-db-table-wrap">
              <table class="proof-db-table">
                <thead><tr><th>정의</th><th>시그니처</th><th>참조 정리</th><th>위치</th></tr></thead>
                <tbody>${defRelationRows}</tbody>
              </table>
            </div>
          </section>`
        : `<section class="proof-db-section">
            <div class="proof-db-section-head">
              <h4>3. 저장된 정의 테이블</h4>
              <span class="proof-db-table-tag">dbo.proof_definitions (${snapshot.definitions.length})</span>
            </div>
            <div class="proof-db-table-wrap">
              <table class="proof-db-table">
                <thead><tr><th>심볼</th><th>엔진</th><th>정의 시그니처</th><th>갱신 시각</th></tr></thead>
                <tbody>${defRelationRows}</tbody>
              </table>
            </div>
          </section>
          <section class="proof-db-section">
            <div class="proof-db-section-head">
              <h4>4. 정리 ↔ 정의 사용 관계 테이블</h4>
              <span class="proof-db-table-tag">dbo.proof_theorem_definition_relations (${snapshot.definitionRelations.length})</span>
            </div>
            <div class="proof-db-table-wrap">
              <table class="proof-db-table">
                <thead><tr><th>정리</th><th>사용된 정의</th><th>사용 역할</th><th>참조 컨텍스트</th></tr></thead>
                <tbody>${defRelFullRows}</tbody>
              </table>
            </div>
          </section>`
    }
  `;
}

export function renderProofDbInspectorHtml(
  snapshot: ProofKnowledgeSnapshot | undefined,
  loading: boolean,
  error?: string,
): string {
  const safe = snapshot ?? {
    workspaceKey: 'local-playground',
    theorems: [],
    definitions: [],
    lemmaRelations: [],
    definitionRelations: [],
  };

  return `
    <div class="proof-db-inspector">
      <div class="proof-db-toolbar">
        <div>
          <h3 class="context-heading">PROOF KNOWLEDGE RDBMS</h3>
          <small class="proof-db-scope">Scope: <code>${escapeHtml(safe.workspaceKey)}</code></small>
        </div>
        <div class="proof-db-toolbar-btns">
          <button type="button" class="button tiny" data-refresh-proof-db>${loading ? '조회 중…' : '↻ 새로고침'}</button>
          <button type="button" class="button tiny primary" data-open-proof-db-modal>전체 표 ↗</button>
        </div>
      </div>

      <div class="proof-db-kpis">
        <div class="proof-db-kpi"><span>완료된 정리</span><strong>${safe.theorems.length}</strong></div>
        <div class="proof-db-kpi"><span>보조정리 관계</span><strong>${safe.lemmaRelations.length}</strong></div>
        <div class="proof-db-kpi"><span>사용된 정의</span><strong>${safe.definitions.length}</strong></div>
      </div>

      ${error ? `<p class="proof-db-error">${escapeHtml(error)}</p>` : ''}
      ${buildTablesMarkup(safe, true)}
    </div>
  `;
}

export function renderProofDbModalHtml(snapshot: ProofKnowledgeSnapshot): string {
  return `
    <div class="proof-db-modal-wrap">
      <p class="context-copy">
        MS SQL Server(<code>irade_db</code>)의 4개 관계형 테이블에 저장된 증명 완료 정리, 보조 정리 의존 관계, 사용된 정의 내역입니다.
        Scope: <code>${escapeHtml(snapshot.workspaceKey)}</code>
      </p>
      <div class="proof-db-kpis modal">
        <div class="proof-db-kpi"><span>dbo.proof_theorems</span><strong>${snapshot.theorems.length}</strong></div>
        <div class="proof-db-kpi"><span>dbo.proof_theorem_lemma_relations</span><strong>${snapshot.lemmaRelations.length}</strong></div>
        <div class="proof-db-kpi"><span>dbo.proof_definitions</span><strong>${snapshot.definitions.length}</strong></div>
        <div class="proof-db-kpi"><span>dbo.proof_theorem_definition_relations</span><strong>${snapshot.definitionRelations.length}</strong></div>
      </div>
      ${buildTablesMarkup(snapshot, false)}
    </div>
  `;
}
