package com.endpoint.frelog.domain.irade;

import jakarta.persistence.*;
import org.hibernate.annotations.Nationalized;

import java.time.LocalDateTime;
import java.time.ZoneOffset;

@Entity
@Table(
        name = "proof_theorem_lemma_relations",
        uniqueConstraints = @UniqueConstraint(
                name = "UQ_proof_thm_lemma_edge",
                columnNames = {"theorem_id", "lemma_name", "relation_type"}
        )
)
public class ProofTheoremLemmaRelation {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "relation_id")
    private Long relationId;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "theorem_id", nullable = false)
    private ProofTheorem theorem;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "lemma_theorem_id")
    private ProofTheorem lemmaTheorem;

    @Nationalized
    @Column(name = "lemma_name", length = 190, nullable = false)
    private String lemmaName;

    @Column(name = "relation_type", length = 32, nullable = false)
    private String relationType;

    @Nationalized
    @Column(name = "reference_context", length = 500)
    private String referenceContext;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    protected ProofTheoremLemmaRelation() {
    }

    public ProofTheoremLemmaRelation(
            ProofTheorem theorem,
            ProofTheorem lemmaTheorem,
            String lemmaName,
            String relationType,
            String referenceContext
    ) {
        this.theorem = theorem;
        this.lemmaTheorem = lemmaTheorem;
        this.lemmaName = lemmaName;
        this.relationType = relationType;
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

    public ProofTheorem getLemmaTheorem() {
        return lemmaTheorem;
    }

    public String getLemmaName() {
        return lemmaName;
    }

    public String getRelationType() {
        return relationType;
    }

    public String getReferenceContext() {
        return referenceContext;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void linkLemmaTheorem(ProofTheorem lemmaTheorem) {
        this.lemmaTheorem = lemmaTheorem;
    }
}
