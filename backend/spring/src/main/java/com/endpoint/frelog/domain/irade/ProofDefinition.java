package com.endpoint.frelog.domain.irade;

import jakarta.persistence.*;
import org.hibernate.annotations.Nationalized;

import java.time.LocalDateTime;
import java.time.ZoneOffset;

@Entity
@Table(
        name = "proof_definitions",
        uniqueConstraints = @UniqueConstraint(
                name = "UQ_proof_definitions_scope",
                columnNames = {"workspace_key", "prover_engine", "symbol_name"}
        )
)
public class ProofDefinition {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "definition_id")
    private Long definitionId;

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
    @Column(name = "symbol_name", length = 190, nullable = false)
    private String symbolName;

    @Column(name = "definition_kind", length = 32, nullable = false)
    private String definitionKind;

    @Nationalized
    @Column(name = "signature_text", length = 1000, nullable = false)
    private String signatureText;

    @Nationalized
    @Column(name = "source_code", length = 16384, nullable = false)
    private String sourceCode;

    @Column(name = "content_hash", length = 64, nullable = false)
    private String contentHash;

    @Nationalized
    @Column(name = "source_cell_id", length = 128)
    private String sourceCellId;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    protected ProofDefinition() {
    }

    public ProofDefinition(
            Integer workspaceId,
            String workspaceKey,
            Long ownerUserId,
            String proverEngine,
            String symbolName,
            String definitionKind,
            String signatureText,
            String sourceCode,
            String contentHash,
            String sourceCellId
    ) {
        this.workspaceId = workspaceId;
        this.workspaceKey = workspaceKey;
        this.ownerUserId = ownerUserId;
        this.proverEngine = proverEngine;
        this.symbolName = symbolName;
        this.definitionKind = definitionKind;
        this.signatureText = signatureText;
        this.sourceCode = sourceCode;
        this.contentHash = contentHash;
        this.sourceCellId = sourceCellId;
    }

    @PrePersist
    protected void onCreate() {
        LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
        if (this.createdAt == null) this.createdAt = now;
        this.updatedAt = now;
    }

    @PreUpdate
    protected void onUpdate() {
        this.updatedAt = LocalDateTime.now(ZoneOffset.UTC);
    }

    public Long getDefinitionId() {
        return definitionId;
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

    public String getSymbolName() {
        return symbolName;
    }

    public String getDefinitionKind() {
        return definitionKind;
    }

    public String getSignatureText() {
        return signatureText;
    }

    public String getSourceCode() {
        return sourceCode;
    }

    public String getContentHash() {
        return contentHash;
    }

    public String getSourceCellId() {
        return sourceCellId;
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
            String definitionKind,
            String signatureText,
            String sourceCode,
            String contentHash,
            String sourceCellId
    ) {
        if (workspaceId != null) this.workspaceId = workspaceId;
        if (ownerUserId != null) this.ownerUserId = ownerUserId;
        this.definitionKind = definitionKind;
        this.signatureText = signatureText;
        this.sourceCode = sourceCode;
        this.contentHash = contentHash;
        this.sourceCellId = sourceCellId;
        this.updatedAt = LocalDateTime.now(ZoneOffset.UTC);
    }
}
