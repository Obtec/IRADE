package com.endpoint.frelog.domain.irade;

import jakarta.persistence.*;
import org.hibernate.annotations.Nationalized;

import java.time.LocalDateTime;
import java.time.ZoneOffset;

@Entity
@Table(
        name = "proof_theorems",
        uniqueConstraints = @UniqueConstraint(
                name = "UQ_proof_theorems_scope",
                columnNames = {"workspace_key", "prover_engine", "theorem_name"}
        )
)
public class ProofTheorem {

    public static final String STATUS_VERIFIED = "VERIFIED";

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "theorem_id")
    private Long theoremId;

    @Column(name = "workspace_id")
    private Integer workspaceId;

    @Nationalized
    @Column(name = "workspace_key", length = 128, nullable = false)
    private String workspaceKey;

    @Column(name = "owner_user_id")
    private Long ownerUserId;

    @Column(name = "prover_engine", length = 16, nullable = false)
    private String proverEngine;

    @Nationalized
    @Column(name = "theorem_name", length = 190, nullable = false)
    private String theoremName;

    @Column(name = "statement_kind", length = 32, nullable = false)
    private String statementKind;

    @Nationalized
    @Column(name = "statement_text", length = 1000, nullable = false)
    private String statementText;

    @Nationalized
    @Column(name = "proof_code", length = 16384, nullable = false)
    private String proofCode;

    @Column(name = "verification_status", length = 16, nullable = false)
    private String verificationStatus = STATUS_VERIFIED;

    @Nationalized
    @Column(name = "verifier_output", length = 4000, nullable = false)
    private String verifierOutput;

    @Column(name = "source_hash", length = 64, nullable = false)
    private String sourceHash;

    @Nationalized
    @Column(name = "source_cell_id", length = 128)
    private String sourceCellId;

    @Column(name = "verified_at", nullable = false)
    private LocalDateTime verifiedAt;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    protected ProofTheorem() {
    }

    public ProofTheorem(
            Integer workspaceId,
            String workspaceKey,
            Long ownerUserId,
            String proverEngine,
            String theoremName,
            String statementKind,
            String statementText,
            String proofCode,
            String verificationStatus,
            String verifierOutput,
            String sourceHash,
            String sourceCellId
    ) {
        this.workspaceId = workspaceId;
        this.workspaceKey = workspaceKey;
        this.ownerUserId = ownerUserId;
        this.proverEngine = proverEngine;
        this.theoremName = theoremName;
        this.statementKind = statementKind;
        this.statementText = statementText;
        this.proofCode = proofCode;
        this.verificationStatus = verificationStatus;
        this.verifierOutput = verifierOutput;
        this.sourceHash = sourceHash;
        this.sourceCellId = sourceCellId;
    }

    @PrePersist
    protected void onCreate() {
        LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
        if (this.verificationStatus == null) this.verificationStatus = STATUS_VERIFIED;
        if (this.verifiedAt == null) this.verifiedAt = now;
        if (this.createdAt == null) this.createdAt = now;
        this.updatedAt = now;
    }

    @PreUpdate
    protected void onUpdate() {
        this.updatedAt = LocalDateTime.now(ZoneOffset.UTC);
    }

    public Long getTheoremId() {
        return theoremId;
    }

    public Integer getWorkspaceId() {
        return workspaceId;
    }

    public String getWorkspaceKey() {
        return workspaceKey;
    }

    public Long getOwnerUserId() {
        return ownerUserId;
    }

    public String getProverEngine() {
        return proverEngine;
    }

    public String getTheoremName() {
        return theoremName;
    }

    public String getStatementKind() {
        return statementKind;
    }

    public String getStatementText() {
        return statementText;
    }

    public String getProofCode() {
        return proofCode;
    }

    public String getVerificationStatus() {
        return verificationStatus;
    }

    public String getVerifierOutput() {
        return verifierOutput;
    }

    public String getSourceHash() {
        return sourceHash;
    }

    public String getSourceCellId() {
        return sourceCellId;
    }

    public LocalDateTime getVerifiedAt() {
        return verifiedAt;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void refresh(
            Integer workspaceId,
            Long ownerUserId,
            String statementKind,
            String statementText,
            String proofCode,
            String verifierOutput,
            String sourceHash,
            String sourceCellId
    ) {
        if (workspaceId != null) this.workspaceId = workspaceId;
        if (ownerUserId != null) this.ownerUserId = ownerUserId;
        this.statementKind = statementKind;
        this.statementText = statementText;
        this.proofCode = proofCode;
        this.verificationStatus = STATUS_VERIFIED;
        this.verifierOutput = verifierOutput;
        this.sourceHash = sourceHash;
        this.sourceCellId = sourceCellId;
        LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
        this.verifiedAt = now;
        this.updatedAt = now;
    }
}
