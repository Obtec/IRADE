package com.endpoint.frelog.domain.irade;

import jakarta.persistence.*;
import org.hibernate.annotations.Nationalized;

import java.time.LocalDateTime;
import java.time.ZoneOffset;

@Entity
@Table(
        name = "proof_theorem_definition_relations",
        uniqueConstraints = @UniqueConstraint(
                name = "UQ_proof_thm_def_edge",
                columnNames = {"theorem_id", "definition_id"}
        )
)
public class ProofTheoremDefinitionRelation {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "relation_id")
    private Long relationId;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "theorem_id", nullable = false)
    private ProofTheorem theorem;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "definition_id", nullable = false)
    private ProofDefinition definition;

    @Column(name = "usage_role", length = 32, nullable = false)
    private String usageRole;

    @Nationalized
    @Column(name = "reference_context", length = 500)
    private String referenceContext;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    protected ProofTheoremDefinitionRelation() {
    }

    public ProofTheoremDefinitionRelation(
            ProofTheorem theorem,
            ProofDefinition definition,
            String usageRole,
            String referenceContext
    ) {
        this.theorem = theorem;
        this.definition = definition;
        this.usageRole = usageRole;
        this.referenceContext = referenceContext;
    }

    @PrePersist
    protected void onCreate() {
        if (this.createdAt == null) this.createdAt = LocalDateTime.now(ZoneOffset.UTC);
    }

    public Long getRelationId() {
        return relationId;
    }

    public ProofTheorem getTheorem() {
        return theorem;
    }

    public ProofDefinition getDefinition() {
        return definition;
    }

    public String getUsageRole() {
        return usageRole;
    }

    public String getReferenceContext() {
        return referenceContext;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
