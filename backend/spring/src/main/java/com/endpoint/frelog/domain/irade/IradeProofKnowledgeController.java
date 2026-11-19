package com.endpoint.frelog.domain.irade;

import com.endpoint.frelog.global.security.CustomUserDetails;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@Validated
@RequestMapping("/api/irade/proofs")
public class IradeProofKnowledgeController {

    private final ProofKnowledgeService proofKnowledgeService;

    public IradeProofKnowledgeController(ProofKnowledgeService proofKnowledgeService) {
        this.proofKnowledgeService = proofKnowledgeService;
    }

    @PostMapping("/verified")
    public ResponseEntity<ProofKnowledgeService.ProofKnowledgeSnapshot> recordVerifiedProof(
            @AuthenticationPrincipal CustomUserDetails principal,
            @RequestBody ProofKnowledgeService.VerifiedProofSyncRequest request
    ) {
        Long ownerUserId = principal != null ? principal.getUserId() : null;
        return ResponseEntity.ok(proofKnowledgeService.recordVerifiedProof(request, ownerUserId));
    }

    @GetMapping
    public ResponseEntity<ProofKnowledgeService.ProofKnowledgeSnapshot> getProofKnowledgeSnapshot(
            @RequestParam(name = "workspaceKey", required = false) String workspaceKey
    ) {
        return ResponseEntity.ok(proofKnowledgeService.getSnapshot(workspaceKey));
    }

    @DeleteMapping("/theorems/{theoremId}")
    public ResponseEntity<Map<String, Object>> deleteTheorem(@PathVariable("theoremId") Long theoremId) {
        proofKnowledgeService.deleteTheorem(theoremId);
        return ResponseEntity.ok(Map.of("deletedTheoremId", theoremId));
    }
}
