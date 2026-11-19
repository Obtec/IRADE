package com.endpoint.frelog.domain.irade;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import java.time.LocalDateTime;
import java.util.*;

@Service
public class ProofKnowledgeService {

    public record LemmaRelationInput(
            String theoremName,
            String lemmaName,
            String relationType,
            String referenceContext
    ) {
    }

    public record DefinitionUsageInput(
            String theoremName,
            String symbolName,
            String usageRole,
            String referenceContext
    ) {
    }

    public record VerifiedProofSyncRequest(
            Integer workspaceId,
            String workspaceKey,
            String proverEngine,
            String verificationStatus,
            String cellId,
            String sourceCode,
            String verifierOutput,
            List<LemmaRelationInput> lemmaRelations,
            List<DefinitionUsageInput> definitionUsages
    ) {
    }

    public record TheoremRow(
            Long theoremId,
            Integer workspaceId,
            String workspaceKey,
            String proverEngine,
            String theoremName,
            String statementKind,
            String statementText,
            String proofCode,
            String verificationStatus,
            String verifierOutput,
            String sourceCellId,
            LocalDateTime verifiedAt,
            LocalDateTime updatedAt
    ) {
    }

    public record DefinitionRow(
            Long definitionId,
            Integer workspaceId,
            String workspaceKey,
            String proverEngine,
            String symbolName,
            String definitionKind,
            String signatureText,
            String sourceCode,
            String sourceCellId,
            LocalDateTime updatedAt
    ) {
    }

    public record LemmaRelationRow(
            Long relationId,
            Long theoremId,
            String theoremName,
            String theoremKind,
            Long lemmaTheoremId,
            String lemmaName,
            String relationType,
            String referenceContext,
            boolean lemmaVerifiedInDb,
            LocalDateTime createdAt
    ) {
    }

    public record DefinitionRelationRow(
            Long relationId,
            Long theoremId,
            String theoremName,
            Long definitionId,
            String definitionName,
            String definitionKind,
            String usageRole,
            String referenceContext,
            LocalDateTime createdAt
    ) {
    }

    public record ProofKnowledgeSnapshot(
            String workspaceKey,
            List<TheoremRow> theorems,
            List<DefinitionRow> definitions,
            List<LemmaRelationRow> lemmaRelations,
            List<DefinitionRelationRow> definitionRelations
    ) {
    }

    private static final Set<String> VALID_RELATION_TYPES = Set.of(
            "USES_LEMMA", "DEPENDS_ON", "REWRITES_WITH", "APPLIES"
    );

    private static final Set<String> VALID_USAGE_ROLES = Set.of(
            "IN_STATEMENT", "IN_PROOF", "BOTH", "REFERENCED"
    );

    private final ProofDefinitionRepository definitionRepository;
    private final ProofTheoremRepository theoremRepository;
    private final ProofTheoremLemmaRelationRepository lemmaRelationRepository;
    private final ProofTheoremDefinitionRelationRepository definitionRelationRepository;
    private final ProofKnowledgeParser parser;

    public ProofKnowledgeService(
            ProofDefinitionRepository definitionRepository,
            ProofTheoremRepository theoremRepository,
            ProofTheoremLemmaRelationRepository lemmaRelationRepository,
            ProofTheoremDefinitionRelationRepository definitionRelationRepository,
            ProofKnowledgeParser parser
    ) {
        this.definitionRepository = definitionRepository;
        this.theoremRepository = theoremRepository;
        this.lemmaRelationRepository = lemmaRelationRepository;
        this.definitionRelationRepository = definitionRelationRepository;
        this.parser = parser;
    }

    @Transactional
    public ProofKnowledgeSnapshot recordVerifiedProof(VerifiedProofSyncRequest request, Long ownerUserId) {
        if (request == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Proof sync payload is required.");
        }
        String status = request.verificationStatus() == null ? "" : request.verificationStatus().trim().toUpperCase(Locale.ROOT);
        if (!ProofTheorem.STATUS_VERIFIED.equals(status)) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Only verified proofs (VERIFIED) can be persisted.");
        }
        if (request.sourceCode() == null || request.sourceCode().isBlank()) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Verified source code is required.");
        }

        String proverEngine = parser.normalizeEngine(request.proverEngine());
        if (parser.containsIncompleteProofMarker(proverEngine, request.sourceCode())) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Incomplete proofs containing sorry/Admitted cannot be persisted.");
        }

        String workspaceKey = normalizeWorkspaceKey(request.workspaceKey(), request.workspaceId());
        String cellId = trimToNull(request.cellId(), 128);
        String verifierOutput = truncate(request.verifierOutput() == null ? "Verified cleanly" : request.verifierOutput().trim(), 4000);
        if (verifierOutput.isEmpty()) verifierOutput = "Verified cleanly";

        ProofKnowledgeParser.ParsedProofBundle bundle = parser.parse(proverEngine, request.sourceCode());
        if (bundle.definitions().isEmpty() && bundle.theorems().isEmpty()) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "No formal definitions or theorems found in verified code.");
        }

        Map<String, ProofDefinition> workspaceDefinitions = new LinkedHashMap<>();
        for (ProofDefinition existing : definitionRepository.findByWorkspaceKeyOrderByUpdatedAtDescDefinitionIdDesc(workspaceKey)) {
            if (proverEngine.equals(existing.getProverEngine())) {
                workspaceDefinitions.put(existing.getSymbolName(), existing);
            }
        }

        for (ProofKnowledgeParser.ParsedDefinition parsedDef : bundle.definitions()) {
            ProofDefinition entity = definitionRepository
                    .findByWorkspaceKeyAndProverEngineAndSymbolName(workspaceKey, proverEngine, parsedDef.symbolName())
                    .orElse(null);
            if (entity == null) {
                entity = definitionRepository.save(new ProofDefinition(
                        request.workspaceId(),
                        workspaceKey,
                        ownerUserId,
                        proverEngine,
                        parsedDef.symbolName(),
                        parsedDef.definitionKind(),
                        parsedDef.signatureText(),
                        parsedDef.sourceCode(),
                        parsedDef.contentHash(),
                        cellId
                ));
            } else {
                entity.refresh(
                        request.workspaceId(),
                        ownerUserId,
                        parsedDef.definitionKind(),
                        parsedDef.signatureText(),
                        parsedDef.sourceCode(),
                        parsedDef.contentHash(),
                        cellId
                );
                entity = definitionRepository.save(entity);
            }
            workspaceDefinitions.put(entity.getSymbolName(), entity);
        }

        Map<String, ProofTheorem> workspaceTheorems = new LinkedHashMap<>();
        for (ProofTheorem existing : theoremRepository.findByWorkspaceKeyOrderByVerifiedAtDescTheoremIdDesc(workspaceKey)) {
            if (proverEngine.equals(existing.getProverEngine())) {
                workspaceTheorems.put(existing.getTheoremName(), existing);
            }
        }

        List<Map.Entry<ProofKnowledgeParser.ParsedTheorem, ProofTheorem>> savedTheorems = new ArrayList<>();
        for (ProofKnowledgeParser.ParsedTheorem parsedThm : bundle.theorems()) {
            ProofTheorem entity = theoremRepository
                    .findByWorkspaceKeyAndProverEngineAndTheoremName(workspaceKey, proverEngine, parsedThm.theoremName())
                    .orElse(null);
            if (entity == null) {
                entity = theoremRepository.save(new ProofTheorem(
                        request.workspaceId(),
                        workspaceKey,
                        ownerUserId,
                        proverEngine,
                        parsedThm.theoremName(),
                        parsedThm.statementKind(),
                        parsedThm.statementText(),
                        parsedThm.proofCode(),
                        ProofTheorem.STATUS_VERIFIED,
                        verifierOutput,
                        parsedThm.sourceHash(),
                        cellId
                ));
            } else {
                entity.refresh(
                        request.workspaceId(),
                        ownerUserId,
                        parsedThm.statementKind(),
                        parsedThm.statementText(),
                        parsedThm.proofCode(),
                        verifierOutput,
                        parsedThm.sourceHash(),
                        cellId
                );
                entity = theoremRepository.save(entity);
            }
            workspaceTheorems.put(entity.getTheoremName(), entity);
            savedTheorems.add(Map.entry(parsedThm, entity));

            List<ProofTheoremLemmaRelation> waitingLinks = lemmaRelationRepository.findUnlinkedReferences(
                    workspaceKey, proverEngine, entity.getTheoremName()
            );
            for (ProofTheoremLemmaRelation rel : waitingLinks) {
                if (!rel.getTheorem().getTheoremId().equals(entity.getTheoremId())) {
                    rel.linkLemmaTheorem(entity);
                    lemmaRelationRepository.save(rel);
                }
            }
        }

        for (Map.Entry<ProofKnowledgeParser.ParsedTheorem, ProofTheorem> entry : savedTheorems) {
            ProofKnowledgeParser.ParsedTheorem parsedThm = entry.getKey();
            ProofTheorem thmEntity = entry.getValue();

            lemmaRelationRepository.deleteByTheoremId(thmEntity.getTheoremId());
            definitionRelationRepository.deleteByTheoremId(thmEntity.getTheoremId());

            syncDefinitionRelations(thmEntity, parsedThm, workspaceDefinitions, request.definitionUsages());
            syncLemmaRelations(thmEntity, parsedThm, proverEngine, workspaceDefinitions, workspaceTheorems, request.lemmaRelations());
        }

        return getSnapshot(workspaceKey);
    }

    @Transactional(readOnly = true)
    public ProofKnowledgeSnapshot getSnapshot(String workspaceKey) {
        String cleanKey = trimToNull(workspaceKey, 128);
        boolean scoped = cleanKey != null && !"ALL".equalsIgnoreCase(cleanKey);

        List<ProofTheorem> theorems = scoped
                ? theoremRepository.findByWorkspaceKeyOrderByVerifiedAtDescTheoremIdDesc(cleanKey)
                : theoremRepository.findAllByOrderByVerifiedAtDescTheoremIdDesc();

        List<ProofDefinition> definitions = scoped
                ? definitionRepository.findByWorkspaceKeyOrderByUpdatedAtDescDefinitionIdDesc(cleanKey)
                : definitionRepository.findAllByOrderByUpdatedAtDescDefinitionIdDesc();

        List<ProofTheoremLemmaRelation> lemmaRelations = scoped
                ? lemmaRelationRepository.findByWorkspaceKey(cleanKey)
                : lemmaRelationRepository.findAllWithTheorems();

        List<ProofTheoremDefinitionRelation> defRelations = scoped
                ? definitionRelationRepository.findByWorkspaceKey(cleanKey)
                : definitionRelationRepository.findAllWithTargets();

        return new ProofKnowledgeSnapshot(
                scoped ? cleanKey : "ALL",
                theorems.stream().map(t -> new TheoremRow(
                        t.getTheoremId(),
                        t.getWorkspaceId(),
                        t.getWorkspaceKey(),
                        t.getProverEngine(),
                        t.getTheoremName(),
                        t.getStatementKind(),
                        t.getStatementText(),
                        t.getProofCode(),
                        t.getVerificationStatus(),
                        t.getVerifierOutput(),
                        t.getSourceCellId(),
                        t.getVerifiedAt(),
                        t.getUpdatedAt()
                )).toList(),
                definitions.stream().map(d -> new DefinitionRow(
                        d.getDefinitionId(),
                        d.getWorkspaceId(),
                        d.getWorkspaceKey(),
                        d.getProverEngine(),
                        d.getSymbolName(),
                        d.getDefinitionKind(),
                        d.getSignatureText(),
                        d.getSourceCode(),
                        d.getSourceCellId(),
                        d.getUpdatedAt()
                )).toList(),
                lemmaRelations.stream().map(r -> new LemmaRelationRow(
                        r.getRelationId(),
                        r.getTheorem().getTheoremId(),
                        r.getTheorem().getTheoremName(),
                        r.getTheorem().getStatementKind(),
                        r.getLemmaTheorem() != null ? r.getLemmaTheorem().getTheoremId() : null,
                        r.getLemmaName(),
                        r.getRelationType(),
                        r.getReferenceContext(),
                        r.getLemmaTheorem() != null,
                        r.getCreatedAt()
                )).toList(),
                defRelations.stream().map(r -> new DefinitionRelationRow(
                        r.getRelationId(),
                        r.getTheorem().getTheoremId(),
                        r.getTheorem().getTheoremName(),
                        r.getDefinition().getDefinitionId(),
                        r.getDefinition().getSymbolName(),
                        r.getDefinition().getDefinitionKind(),
                        r.getUsageRole(),
                        r.getReferenceContext(),
                        r.getCreatedAt()
                )).toList()
        );
    }

    @Transactional
    public void deleteTheorem(Long theoremId) {
        ProofTheorem theorem = theoremRepository.findById(theoremId)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Verified theorem not found."));
        // SQL Server forbids cascading multiple paths on self-referencing tables, so detach incoming edges first.
        lemmaRelationRepository.detachTargetTheorem(theoremId);
        theoremRepository.delete(theorem);
    }

    private void syncDefinitionRelations(
            ProofTheorem theorem,
            ProofKnowledgeParser.ParsedTheorem parsedThm,
            Map<String, ProofDefinition> workspaceDefinitions,
            List<DefinitionUsageInput> explicitInputs
    ) {
        Map<Long, ProofTheoremDefinitionRelation> byDefId = new LinkedHashMap<>();

        for (ProofDefinition def : workspaceDefinitions.values()) {
            Optional<String> detectedRole = parser.detectUsageRole(
                    def.getSymbolName(), parsedThm.statementText(), parsedThm.proofBody()
            );
            if (detectedRole.isPresent()) {
                String combined = parsedThm.statementText() + "\n" + parsedThm.proofBody();
                String context = parser.extractReferenceContext(combined, def.getSymbolName());
                byDefId.put(def.getDefinitionId(), new ProofTheoremDefinitionRelation(
                        theorem, def, detectedRole.get(), context
                ));
            }
        }

        if (explicitInputs != null) {
            for (DefinitionUsageInput input : explicitInputs) {
                if (input == null || input.symbolName() == null) continue;
                if (input.theoremName() != null && !input.theoremName().isBlank()
                        && !input.theoremName().trim().equals(theorem.getTheoremName())) {
                    continue;
                }
                ProofDefinition def = workspaceDefinitions.get(input.symbolName().trim());
                if (def == null) continue;
                String role = normalizeUsageRole(input.usageRole());
                String context = trimToNull(input.referenceContext(), 500);
                byDefId.putIfAbsent(def.getDefinitionId(), new ProofTheoremDefinitionRelation(
                        theorem, def, role, context
                ));
            }
        }

        if (!byDefId.isEmpty()) {
            definitionRelationRepository.saveAll(byDefId.values());
        }
    }

    private void syncLemmaRelations(
            ProofTheorem theorem,
            ProofKnowledgeParser.ParsedTheorem parsedThm,
            String proverEngine,
            Map<String, ProofDefinition> workspaceDefinitions,
            Map<String, ProofTheorem> workspaceTheorems,
            List<LemmaRelationInput> explicitInputs
    ) {
        record EdgeKey(String lemmaName, String relationType) {
        }
        Map<EdgeKey, ProofTheoremLemmaRelation> edges = new LinkedHashMap<>();
        String searchableText = parsedThm.statementText() + "\n" + parsedThm.proofBody();

        for (ProofTheorem candidate : workspaceTheorems.values()) {
            if (Objects.equals(candidate.getTheoremId(), theorem.getTheoremId())) continue;
            if (!parser.containsIdentifier(searchableText, candidate.getTheoremName())) continue;

            String relationType = parser.classifyLemmaRelationType(searchableText, candidate.getTheoremName());
            String context = parser.extractReferenceContext(searchableText, candidate.getTheoremName());
            edges.put(
                    new EdgeKey(candidate.getTheoremName(), relationType),
                    new ProofTheoremLemmaRelation(theorem, candidate, candidate.getTheoremName(), relationType, context)
            );
        }

        for (String qualifiedRef : parser.extractQualifiedReferences(proverEngine, parsedThm.proofBody())) {
            if (qualifiedRef.equals(theorem.getTheoremName()) || workspaceDefinitions.containsKey(qualifiedRef)) {
                continue;
            }
            ProofTheorem target = workspaceTheorems.get(qualifiedRef);
            if (target != null && Objects.equals(target.getTheoremId(), theorem.getTheoremId())) continue;
            boolean alreadyTracked = edges.keySet().stream().anyMatch(k -> k.lemmaName().equals(qualifiedRef));
            if (alreadyTracked) continue;

            String relationType = parser.classifyLemmaRelationType(parsedThm.proofBody(), qualifiedRef);
            String context = parser.extractReferenceContext(parsedThm.proofBody(), qualifiedRef);
            String truncatedName = truncate(qualifiedRef, 190);
            edges.put(
                    new EdgeKey(truncatedName, relationType),
                    new ProofTheoremLemmaRelation(theorem, target, truncatedName, relationType, context)
            );
        }

        if (explicitInputs != null) {
            for (LemmaRelationInput input : explicitInputs) {
                if (input == null || input.lemmaName() == null || input.lemmaName().isBlank()) continue;
                if (input.theoremName() != null && !input.theoremName().isBlank()
                        && !input.theoremName().trim().equals(theorem.getTheoremName())) {
                    continue;
                }
                String lemmaName = truncate(input.lemmaName().trim(), 190);
                if (lemmaName.equals(theorem.getTheoremName())) continue;
                String relationType = normalizeRelationType(input.relationType());
                ProofTheorem target = workspaceTheorems.get(lemmaName);
                if (target != null && Objects.equals(target.getTheoremId(), theorem.getTheoremId())) continue;
                String context = trimToNull(input.referenceContext(), 500);
                edges.putIfAbsent(
                        new EdgeKey(lemmaName, relationType),
                        new ProofTheoremLemmaRelation(theorem, target, lemmaName, relationType, context)
                );
            }
        }

        if (!edges.isEmpty()) {
            lemmaRelationRepository.saveAll(edges.values());
        }
    }

    private String normalizeWorkspaceKey(String rawKey, Integer workspaceId) {
        String trimmed = trimToNull(rawKey, 128);
        if (trimmed != null) return trimmed;
        if (workspaceId != null) return "workspace-" + workspaceId;
        return "studio-default";
    }

    private String normalizeRelationType(String raw) {
        if (raw == null) return "USES_LEMMA";
        String upper = raw.trim().toUpperCase(Locale.ROOT);
        return VALID_RELATION_TYPES.contains(upper) ? upper : "USES_LEMMA";
    }

    private String normalizeUsageRole(String raw) {
        if (raw == null) return "REFERENCED";
        String upper = raw.trim().toUpperCase(Locale.ROOT);
        return VALID_USAGE_ROLES.contains(upper) ? upper : "REFERENCED";
    }

    private String trimToNull(String value, int maxLen) {
        if (value == null) return null;
        String trimmed = value.trim();
        if (trimmed.isEmpty()) return null;
        return truncate(trimmed, maxLen);
    }

    private String truncate(String value, int maxLen) {
        if (value == null) return "";
        return value.length() <= maxLen ? value : value.substring(0, maxLen);
    }
}
